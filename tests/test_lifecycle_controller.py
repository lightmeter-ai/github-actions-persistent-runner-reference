from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest import mock

from runner_controller.lifecycle_controller import (
    Config,
    FakeBackend,
    JournalStore,
    LifecycleController,
    SafetyError,
    UsageError,
    canonical_hash,
    inspect_command,
)


class InjectedCrash(RuntimeError):
    pass


class CrashingBackend(FakeBackend):
    """Crash after one selected fake mutation has taken effect."""

    def __init__(self, config: Config, crash_after: str) -> None:
        super().__init__(config)
        self.crash_after = crash_after
        self.crashed = False

    def _crash(self, boundary: str) -> None:
        if not self.crashed and self.crash_after == boundary:
            self.crashed = True
            raise InjectedCrash(boundary)

    def freeze(self, slot: str) -> None:
        super().freeze(slot)
        self._crash("freeze")

    def thaw(self, slot: str) -> None:
        super().thaw(slot)
        self._crash("thaw")

    def stop_and_prove(self, slot, expected) -> None:
        super().stop_and_prove(slot, expected)
        self._crash("stop")

    def apply_generation(self, slot: str, generation: str) -> None:
        super().apply_generation(slot, generation)
        self._crash("apply")

    def start_and_prove(self, slot: str, generation: str):
        started = super().start_and_prove(slot, generation)
        self._crash("start")
        return started


class LifecycleControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="runner-controller-test.")
        self.root = Path(self.temporary.name)
        self.state_root = self.root / "state"
        self.config = Config(
            slots=("runner-01", "runner-02"),
            unit_template="persistent-ci-runner.{slot}.service",
        )
        self.backend = FakeBackend(self.config)
        self.store = JournalStore(self.state_root)
        self.controller = LifecycleController(
            self.config,
            self.store,
            self.backend,
            now=lambda: 1_800_000_000,
        )
        self.token = "01" * 32
        self.other_token = "02" * 32
        self.pre_evidence = canonical_hash({"provider": "idle", "sample": 1})
        self.post_evidence = canonical_hash({"provider": "online", "sample": 2})

    def test_demo_runs_directly_from_fresh_checkout(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-B", project_root / "examples" / "run_demo.py"],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"generation": "current"', result.stdout)
        self.assertIn("Fake backend event order:", result.stdout)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def initialize(self) -> dict[str, object]:
        return self.controller.initialize()

    def prepare(self, slot: str = "runner-01") -> dict[str, object]:
        return self.controller.prepare(slot, self.token, self.pre_evidence)

    def test_inspect_missing_state_is_non_mutating(self) -> None:
        self.assertIsNone(self.controller.inspect())
        self.assertFalse(self.state_root.exists())

    def test_initialize_creates_private_exact_baseline(self) -> None:
        state = self.initialize()

        self.assertEqual(state["version"], 1)
        self.assertIsNone(state["active"])
        self.assertEqual(
            state["slots"],
            {
                "runner-01": {"generation": "legacy", "evidence_sha256": "absent"},
                "runner-02": {"generation": "legacy", "evidence_sha256": "absent"},
            },
        )
        self.assertEqual(stat.S_IMODE(os.lstat(self.state_root).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(self.store.journal).st_mode), 0o600)
        self.assertEqual(self.controller.inspect(), state)

    def test_existing_root_under_parent_alias_is_accepted(self) -> None:
        physical_parent = self.root / "physical"
        physical_parent.mkdir()
        alias_parent = self.root / "alias"
        alias_parent.symlink_to(physical_parent, target_is_directory=True)
        physical_state_root = physical_parent / "state"
        physical_state_root.mkdir(mode=0o700)
        os.chmod(physical_state_root, 0o700)

        store = JournalStore(alias_parent / "state")
        store.ensure_root()

        self.assertEqual(store.root, physical_state_root.resolve())

    def test_state_root_leaf_symlink_is_rejected(self) -> None:
        target = self.root / "physical-state"
        target.mkdir(mode=0o700)
        os.chmod(target, 0o700)
        leaf_alias = self.root / "state-alias"
        leaf_alias.symlink_to(target, target_is_directory=True)
        store = JournalStore(leaf_alias)

        with self.assertRaisesRegex(SafetyError, "physical same-owner"):
            store.ensure_root()

        self.assertEqual(store.root, leaf_alias)

    def test_concurrent_safe_root_creation_is_accepted(self) -> None:
        original_mkdir = Path.mkdir

        def create_then_report_race(path: Path, *args, **kwargs) -> None:
            original_mkdir(path, *args, **kwargs)
            os.chmod(path, 0o700)
            raise FileExistsError("created concurrently")

        with mock.patch.object(Path, "mkdir", new=create_then_report_race):
            self.store.ensure_root()

        self.assertEqual(stat.S_IMODE(os.lstat(self.state_root).st_mode), 0o700)

    def test_concurrent_unsafe_root_creation_fails_closed(self) -> None:
        original_mkdir = Path.mkdir

        def create_unsafe_then_report_race(path: Path, *args, **kwargs) -> None:
            original_mkdir(path, *args, **kwargs)
            os.chmod(path, 0o755)
            raise FileExistsError("created concurrently")

        with (
            mock.patch.object(Path, "mkdir", new=create_unsafe_then_report_race),
            self.assertRaisesRegex(SafetyError, "physical same-owner"),
        ):
            self.store.ensure_root()

        self.assertEqual(stat.S_IMODE(os.lstat(self.state_root).st_mode), 0o755)

    def test_initialize_refuses_non_legacy_or_frozen_slot(self) -> None:
        original = self.backend.snapshot("runner-02")
        self.backend.snapshots["runner-02"] = replace(
            original, generation="current", frozen=True
        )

        with self.assertRaisesRegex(SafetyError, "every slot running in legacy"):
            self.initialize()
        self.assertFalse(self.store.journal.exists())

    def test_complete_lifecycle_changes_only_selected_slot(self) -> None:
        self.initialize()
        prepared = self.prepare()
        self.assertEqual(prepared["active"]["step"], "frozen")
        self.assertTrue(self.backend.snapshot("runner-01").frozen)

        committed = self.controller.commit(self.token)
        self.assertEqual(committed["active"]["step"], "awaiting-verification")
        self.assertEqual(self.backend.snapshot("runner-01").generation, "current")
        self.assertTrue(self.backend.snapshot("runner-01").running)
        self.assertFalse(self.backend.snapshot("runner-01").frozen)

        finalized = self.controller.finalize(self.token, self.post_evidence)
        self.assertIsNone(finalized["active"])
        self.assertEqual(finalized["slots"]["runner-01"]["generation"], "current")
        self.assertEqual(
            finalized["slots"]["runner-01"]["evidence_sha256"], self.post_evidence
        )
        self.assertEqual(finalized["slots"]["runner-02"]["generation"], "legacy")
        self.assertEqual(self.backend.snapshot("runner-02").generation, "legacy")

    def test_journal_hashes_token_instead_of_storing_it(self) -> None:
        self.initialize()
        self.prepare()

        payload = self.store.journal.read_text(encoding="ascii")
        self.assertNotIn(self.token, payload)
        self.assertIn("token_sha256", payload)

    def test_another_transaction_cannot_enter_active_journal(self) -> None:
        self.initialize()
        self.prepare()

        with self.assertRaisesRegex(SafetyError, "another transaction"):
            self.controller.prepare("runner-02", self.other_token, self.pre_evidence)

    def test_busy_slot_is_refused_before_journal_authority(self) -> None:
        self.initialize()
        self.backend.idle["runner-01"] = False

        with self.assertRaisesRegex(SafetyError, "busy"):
            self.prepare()
        self.assertIsNone(self.controller.inspect()["active"])

    def test_invalid_token_and_evidence_are_refused(self) -> None:
        self.initialize()

        with self.assertRaises(UsageError):
            self.controller.prepare("runner-01", "short", self.pre_evidence)
        with self.assertRaises(UsageError):
            self.controller.prepare("runner-01", self.token, "not-a-hash")

    def test_unknown_slot_is_refused_before_backend_or_journal_mutation(self) -> None:
        with self.assertRaisesRegex(UsageError, "slot or desired generation"):
            self.controller.prepare("runner-99", self.token, self.pre_evidence)

        self.assertEqual(self.backend.events, [])
        self.assertFalse(self.state_root.exists())

    def test_wrong_token_cannot_commit_abort_or_finalize(self) -> None:
        self.initialize()
        self.prepare()

        with self.assertRaisesRegex(SafetyError, "does not own"):
            self.controller.commit(self.other_token)
        with self.assertRaisesRegex(SafetyError, "does not own"):
            self.controller.abort(self.other_token)

        self.controller.commit(self.token)
        with self.assertRaisesRegex(SafetyError, "does not own"):
            self.controller.finalize(self.other_token, self.post_evidence)

    def test_identity_drift_is_refused_before_commit(self) -> None:
        self.initialize()
        self.prepare()
        frozen = self.backend.snapshot("runner-01")
        self.backend.snapshots["runner-01"] = replace(
            frozen,
            main_pid=frozen.main_pid + 100,
            main_starttime=frozen.main_starttime + 100,
        )

        with self.assertRaisesRegex(SafetyError, "identity changed before commit"):
            self.controller.commit(self.token)
        self.assertEqual(self.controller.inspect()["active"]["step"], "frozen")

    def test_pid_reuse_is_refused_before_commit(self) -> None:
        self.initialize()
        self.prepare()
        frozen = self.backend.snapshot("runner-01")
        self.backend.snapshots["runner-01"] = replace(
            frozen,
            main_starttime=frozen.main_starttime + 100,
        )

        with self.assertRaisesRegex(SafetyError, "identity changed before commit"):
            self.controller.commit(self.token)
        self.assertEqual(self.controller.inspect()["active"]["step"], "frozen")
        self.assertNotIn("stop:runner-01", self.backend.events)

    def test_cgroup_inode_reuse_is_refused_before_commit(self) -> None:
        self.initialize()
        self.prepare()
        frozen = self.backend.snapshot("runner-01")
        self.backend.snapshots["runner-01"] = replace(
            frozen,
            cgroup_device_inode="9:9999",
        )

        with self.assertRaisesRegex(SafetyError, "identity changed before commit"):
            self.controller.commit(self.token)
        self.assertEqual(self.controller.inspect()["active"]["step"], "frozen")
        self.assertNotIn("stop:runner-01", self.backend.events)

    def test_post_start_identity_drift_is_refused_before_finalize(self) -> None:
        self.initialize()
        self.prepare()
        self.controller.commit(self.token)
        current = self.backend.snapshot("runner-01")
        self.backend.snapshots["runner-01"] = replace(
            current,
            listener_pid=current.listener_pid + 1,
            listener_starttime=current.listener_starttime + 10,
        )

        with self.assertRaisesRegex(
            SafetyError, "identity changed before finalization"
        ):
            self.controller.finalize(self.token, self.post_evidence)

    def test_abort_from_frozen_restores_original_listener(self) -> None:
        self.initialize()
        self.prepare()

        state = self.controller.abort(self.token)

        self.assertIsNone(state["active"])
        restored = self.backend.snapshot("runner-01")
        self.assertEqual(restored.generation, "legacy")
        self.assertTrue(restored.running)
        self.assertFalse(restored.frozen)
        self.assertIn("thaw:runner-01", self.backend.events)

    def test_abort_after_commit_rolls_back_new_generation(self) -> None:
        self.initialize()
        self.prepare()
        self.controller.commit(self.token)

        state = self.controller.abort(self.token)

        self.assertIsNone(state["active"])
        restored = self.backend.snapshot("runner-01")
        self.assertEqual(restored.generation, "legacy")
        self.assertTrue(restored.running)
        self.assertIn("apply-legacy:runner-01", self.backend.events)

    def _crash_case(self, boundary: str, expected_step: str) -> None:
        backend = CrashingBackend(self.config, boundary)
        store = JournalStore(self.root / f"state-{boundary}")
        controller = LifecycleController(
            self.config,
            store,
            backend,
            now=lambda: 1_800_000_000,
        )
        controller.initialize()

        with self.assertRaises(InjectedCrash):
            controller.prepare("runner-01", self.token, self.pre_evidence)
            if boundary != "freeze":
                controller.commit(self.token)

        self.assertEqual(controller.inspect()["active"]["step"], expected_step)
        recovered_controller = LifecycleController(
            self.config,
            JournalStore(store.root),
            backend,
            now=lambda: 1_800_000_000,
        )
        if boundary == "start":
            with self.assertRaisesRegex(SafetyError, "identity is ambiguous"):
                recovered_controller.recover()
            retained = recovered_controller.inspect()["active"]
            self.assertEqual(retained["step"], "recovery-intent")
            self.assertEqual(retained["recover_from"], "start-intent")
            self.assertEqual(retained["recovery_step"], "observe")
            return

        recovered = recovered_controller.recover()
        self.assertIsNone(recovered["active"])
        restored = backend.snapshot("runner-01")
        self.assertEqual(restored.generation, "legacy")
        self.assertTrue(restored.running)
        self.assertFalse(restored.frozen)

    def test_recovery_after_crash_immediately_after_freeze(self) -> None:
        self._crash_case("freeze", "freeze-intent")

    def test_recovery_after_crash_immediately_after_stop(self) -> None:
        self._crash_case("stop", "stop-intent")

    def test_recovery_after_crash_immediately_after_apply(self) -> None:
        self._crash_case("apply", "apply-intent")

    def test_recovery_after_crash_immediately_after_start(self) -> None:
        self._crash_case("start", "start-intent")

    def _recovery_crash_case(self, boundary: str, expected_step: str) -> None:
        backend = CrashingBackend(self.config, "disabled")
        store = JournalStore(self.root / f"state-recovery-{boundary}")
        controller = LifecycleController(
            self.config,
            store,
            backend,
            now=lambda: 1_800_000_000,
        )
        controller.initialize()
        controller.prepare("runner-01", self.token, self.pre_evidence)
        if boundary != "thaw":
            controller.commit(self.token)

        backend.crash_after = boundary
        backend.crashed = False
        with self.assertRaises(InjectedCrash):
            controller.abort(self.token)

        retained = controller.inspect()["active"]
        self.assertEqual(retained["step"], "recovery-intent")
        self.assertEqual(retained["recovery_step"], expected_step)

        rebuilt = LifecycleController(
            self.config,
            JournalStore(store.root),
            backend,
            now=lambda: 1_800_000_000,
        )
        if boundary == "start":
            with self.assertRaisesRegex(SafetyError, "start outcome is ambiguous"):
                rebuilt.recover()
            self.assertEqual(
                rebuilt.inspect()["active"]["recovery_step"],
                "start-intent",
            )
            return

        recovered = rebuilt.recover()
        self.assertIsNone(recovered["active"])
        restored = backend.snapshot("runner-01")
        self.assertTrue(restored.running)
        self.assertFalse(restored.frozen)
        self.assertEqual(restored.generation, "legacy")

    def test_recovery_resumes_after_crash_during_thaw(self) -> None:
        self._recovery_crash_case("thaw", "thaw-intent")

    def test_recovery_resumes_after_crash_during_stop(self) -> None:
        self._recovery_crash_case("stop", "stop-intent")

    def test_recovery_resumes_after_crash_during_apply(self) -> None:
        self._recovery_crash_case("apply", "apply-intent")

    def test_recovery_refuses_ambiguous_crash_during_start(self) -> None:
        self._recovery_crash_case("start", "start-intent")

    def test_recovery_refuses_running_original_identity_drift(self) -> None:
        self.initialize()
        self.prepare()
        frozen = self.backend.snapshot("runner-01")
        drifted = replace(
            frozen,
            main_pid=frozen.main_pid + 100,
            main_starttime=frozen.main_starttime + 100,
        )
        self.backend.snapshots["runner-01"] = drifted

        with self.assertRaisesRegex(SafetyError, "original identity is ambiguous"):
            self.controller.recover()

        active = self.controller.inspect()["active"]
        self.assertEqual(active["step"], "recovery-intent")
        self.assertEqual(active["recovery_step"], "observe")
        self.assertEqual(self.backend.snapshot("runner-01"), drifted)
        self.assertNotIn("thaw:runner-01", self.backend.events)
        self.assertNotIn("stop:runner-01", self.backend.events)

    def test_recovery_refuses_running_desired_identity_drift(self) -> None:
        self.initialize()
        self.prepare()
        self.controller.commit(self.token)
        current = self.backend.snapshot("runner-01")
        drifted = replace(
            current,
            listener_pid=current.listener_pid + 100,
            listener_starttime=current.listener_starttime + 100,
        )
        self.backend.snapshots["runner-01"] = drifted

        with self.assertRaisesRegex(SafetyError, "desired identity is ambiguous"):
            self.controller.recover()

        active = self.controller.inspect()["active"]
        self.assertEqual(active["step"], "recovery-intent")
        self.assertEqual(active["recovery_step"], "observe")
        self.assertEqual(self.backend.snapshot("runner-01"), drifted)
        self.assertNotIn("stop:runner-01", self.backend.events[-1:])

    def test_expired_commit_deadline_retains_authority(self) -> None:
        clock = [1_800_000_000]
        controller = LifecycleController(
            self.config,
            JournalStore(self.root / "state-expired-commit"),
            self.backend,
            now=lambda: clock[0],
        )
        controller.initialize()
        controller.prepare("runner-01", self.token, self.pre_evidence)
        clock[0] = controller.inspect()["active"]["deadline_epoch"]

        with self.assertRaisesRegex(SafetyError, "commit deadline expired"):
            controller.commit(self.token)

        self.assertEqual(controller.inspect()["active"]["step"], "frozen")
        self.assertTrue(self.backend.snapshot("runner-01").frozen)
        recovered = controller.abort(self.token)
        self.assertIsNone(recovered["active"])

    def test_expired_finalization_deadline_retains_authority(self) -> None:
        clock = [1_800_000_000]
        controller = LifecycleController(
            self.config,
            JournalStore(self.root / "state-expired-finalize"),
            self.backend,
            now=lambda: clock[0],
        )
        controller.initialize()
        controller.prepare("runner-01", self.token, self.pre_evidence)
        controller.commit(self.token)
        clock[0] = controller.inspect()["active"]["deadline_epoch"]

        with self.assertRaisesRegex(SafetyError, "finalization deadline expired"):
            controller.finalize(self.token, self.post_evidence)

        self.assertEqual(
            controller.inspect()["active"]["step"], "awaiting-verification"
        )
        self.assertEqual(
            self.backend.snapshot("runner-01").generation,
            "current",
        )

    def test_recovery_is_idempotent_without_active_transaction(self) -> None:
        baseline = self.initialize()
        self.assertEqual(self.controller.recover(), baseline)
        self.assertEqual(self.controller.recover(), baseline)

    def test_busy_slot_blocks_recovery_without_clearing_authority(self) -> None:
        self.initialize()
        self.prepare()
        self.backend.idle["runner-01"] = False

        with self.assertRaisesRegex(SafetyError, "busy"):
            self.controller.recover()
        self.assertEqual(self.controller.inspect()["active"]["step"], "recovery-intent")

    def test_recovery_intent_can_resume_after_busy_condition_clears(self) -> None:
        self.initialize()
        self.prepare()
        self.backend.idle["runner-01"] = False
        with self.assertRaises(SafetyError):
            self.controller.recover()

        self.backend.idle["runner-01"] = True
        recovered = self.controller.recover()
        self.assertIsNone(recovered["active"])
        self.assertEqual(self.backend.snapshot("runner-01").generation, "legacy")

    def test_malformed_or_unknown_journal_fields_fail_closed(self) -> None:
        self.initialize()
        value = json.loads(self.store.journal.read_text(encoding="ascii"))
        value["unexpected"] = True
        self.store.journal.write_text(json.dumps(value), encoding="ascii")
        os.chmod(self.store.journal, 0o600)

        with self.assertRaisesRegex(SafetyError, "unexpected top-level"):
            self.controller.inspect()

    def test_boolean_process_identity_and_deadline_fields_fail_closed(self) -> None:
        self.initialize()
        prepared = self.prepare()

        for path in (
            ("active", "snapshot", "main_pid"),
            ("active", "snapshot", "main_starttime"),
            ("active", "snapshot", "listener_pid"),
            ("active", "snapshot", "listener_starttime"),
            ("active", "deadline_epoch"),
        ):
            with self.subTest(path=path):
                value = json.loads(json.dumps(prepared))
                target = value
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = True
                self.store.journal.write_text(json.dumps(value), encoding="ascii")
                os.chmod(self.store.journal, 0o600)

                with self.assertRaisesRegex(SafetyError, "invalid"):
                    self.controller.inspect()

    def test_world_readable_journal_fails_closed(self) -> None:
        self.initialize()
        os.chmod(self.store.journal, 0o644)

        with self.assertRaisesRegex(SafetyError, "journal metadata is unsafe"):
            self.controller.inspect()

    def test_symlink_journal_fails_closed(self) -> None:
        self.initialize()
        target = self.root / "replacement.json"
        target.write_text(
            self.store.journal.read_text(encoding="ascii"), encoding="ascii"
        )
        self.store.journal.unlink()
        self.store.journal.symlink_to(target)

        with self.assertRaisesRegex(SafetyError, "journal metadata is unsafe"):
            self.controller.inspect()

    def test_configuration_drift_fails_closed(self) -> None:
        self.initialize()
        changed = Config(
            slots=self.config.slots,
            unit_template="persistent-ci-runner-different.{slot}.service",
        )
        changed_controller = LifecycleController(
            changed, self.store, FakeBackend(changed)
        )

        with self.assertRaisesRegex(SafetyError, "configuration identity changed"):
            changed_controller.inspect()

    def test_nonblocking_host_lock_refuses_concurrent_controller(self) -> None:
        with (
            self.store.lock(),
            self.assertRaisesRegex(SafetyError, "another lifecycle controller"),
        ):
            self.controller.initialize()

    def test_cli_inspect_does_not_create_missing_state(self) -> None:
        config_path = self.root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "slots": list(self.config.slots),
                    "unit_template": self.config.unit_template,
                }
            ),
            encoding="ascii",
        )
        missing = self.root / "never-created"

        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                inspect_command(["inspect", str(config_path), str(missing)]), 0
            )
        self.assertEqual(json.loads(output.getvalue()), {"initialized": False})
        self.assertFalse(missing.exists())

    def test_config_rejects_duplicate_slots_and_unsafe_template(self) -> None:
        with self.assertRaisesRegex(UsageError, "unique"):
            Config(("runner-01", "runner-01"), "example.{slot}.service")
        with self.assertRaisesRegex(UsageError, "exactly one"):
            Config(("runner-01",), "example.service")
        with self.assertRaisesRegex(UsageError, "unsafe systemd"):
            Config(("runner-01",), "../{slot}.service")

    def test_config_rejects_non_string_slots_and_extra_template_fields(self) -> None:
        with self.assertRaisesRegex(UsageError, "slot name must be a string"):
            Config((1,), "example.{slot}.service")
        with self.assertRaisesRegex(UsageError, "and no others"):
            Config(("runner-01",), "example.{slot}.{other}.service")

    def test_config_file_symlink_is_refused_before_reading_target(self) -> None:
        target = self.root / "private-target.json"
        target.write_text(
            json.dumps(
                {
                    "version": 1,
                    "slots": ["runner-01"],
                    "unit_template": "example.{slot}.service",
                }
            ),
            encoding="ascii",
        )
        link = self.root / "config-link.json"
        link.symlink_to(target)

        with self.assertRaisesRegex(UsageError, "unsafe"):
            Config.from_json(link)

    def test_same_generation_request_is_refused(self) -> None:
        self.initialize()

        with self.assertRaisesRegex(SafetyError, "already uses"):
            self.controller.prepare(
                "runner-01",
                self.token,
                self.pre_evidence,
                desired_generation="legacy",
            )


if __name__ == "__main__":
    unittest.main()

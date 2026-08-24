"""Journaled, one-slot-at-a-time lifecycle reference for persistent CI runners.

This module deliberately ships without a privileged host backend.  The
``Backend`` protocol and ``FakeBackend`` preserve the state-machine and recovery
contract while making the published snapshot non-mutating by default.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol


class SafetyError(RuntimeError):
    """The requested operation could not prove its safety boundary."""


class UsageError(RuntimeError):
    """The caller supplied an invalid command or configuration."""


HASH_RE = re.compile(r"[0-9a-f]{64}")
SLOT_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
STEPS = {
    "freeze-intent",
    "frozen",
    "stop-intent",
    "stopped",
    "apply-intent",
    "applied",
    "start-intent",
    "awaiting-verification",
    "recovery-intent",
}
RECOVERY_STEPS = {
    "none",
    "observe",
    "thaw-intent",
    "thawed",
    "stop-intent",
    "stopped",
    "apply-intent",
    "applied",
    "start-intent",
    "started",
}


def canonical_hash(value: object) -> str:
    """Return a stable SHA-256 digest for JSON-compatible evidence."""

    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def require_hash(value: str, label: str) -> None:
    if HASH_RE.fullmatch(value) is None:
        raise UsageError(f"{label} must be one lowercase SHA-256 value")


def token_hash(token: str) -> str:
    if HASH_RE.fullmatch(token) is None:
        raise UsageError("operation token must be exactly 32 bytes in lowercase hex")
    return hashlib.sha256(bytes.fromhex(token)).hexdigest()


def new_operation_token() -> str:
    """Create a token suitable for one lifecycle transaction."""

    return secrets.token_hex(32)


@dataclass(frozen=True)
class Config:
    """Static identity boundary for one persistent-runner host."""

    slots: tuple[str, ...]
    unit_template: str

    def __post_init__(self) -> None:
        if not self.slots or len(self.slots) > 64:
            raise UsageError("configuration must contain between 1 and 64 slots")
        if any(not isinstance(slot, str) for slot in self.slots):
            raise UsageError("every slot name must be a string")
        if len(set(self.slots)) != len(self.slots):
            raise UsageError("slot names must be unique")
        if any(SLOT_RE.fullmatch(slot) is None for slot in self.slots):
            raise UsageError("slot names must match [a-z][a-z0-9-]{0,31}")
        if not isinstance(self.unit_template, str):
            raise UsageError("unit_template must be a string")
        template_remainder = self.unit_template.replace("{slot}", "", 1)
        if (
            self.unit_template.count("{slot}") != 1
            or "{" in template_remainder
            or "}" in template_remainder
        ):
            raise UsageError(
                "unit_template must contain exactly one {slot} field and no others"
            )
        for slot in self.slots:
            unit = self.unit(slot)
            if (
                len(unit) > 255
                or not unit.endswith(".service")
                or re.fullmatch(r"[A-Za-z0-9_.@:-]+", unit) is None
            ):
                raise UsageError("unit_template produces an unsafe systemd unit name")

    def unit(self, slot: str) -> str:
        if slot not in self.slots:
            raise UsageError(f"unknown slot: {slot}")
        return self.unit_template.format(slot=slot)

    @classmethod
    def from_json(cls, path: Path) -> Config:
        try:
            metadata = os.lstat(path)
        except OSError as error:
            raise UsageError(f"configuration cannot be read: {error}") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_size > 65536
        ):
            raise UsageError("configuration file is unsafe or oversized")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if opened.st_dev != metadata.st_dev or opened.st_ino != metadata.st_ino:
                    raise UsageError(
                        "configuration identity changed while it was opened"
                    )
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    payload = handle.read(65537)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise UsageError(f"configuration cannot be read: {error}") from error
        if not payload:
            raise UsageError("configuration file is empty")
        try:
            value = json.loads(payload)
        except ValueError as error:
            raise UsageError(f"configuration is not valid JSON: {error}") from error
        if not isinstance(value, dict) or set(value) != {
            "version",
            "slots",
            "unit_template",
        }:
            raise UsageError(
                "configuration must contain only version, slots, and unit_template"
            )
        if value["version"] != 1 or not isinstance(value["slots"], list):
            raise UsageError("configuration version or slots are invalid")
        if not isinstance(value["unit_template"], str):
            raise UsageError("configuration unit_template is invalid")
        return cls(tuple(value["slots"]), value["unit_template"])


@dataclass(frozen=True)
class SlotSnapshot:
    """Identity-bearing observation of one runner listener."""

    slot: str
    generation: str
    unit: str
    running: bool
    frozen: bool
    main_pid: int
    main_starttime: int
    listener_pid: int
    listener_starttime: int
    cgroup: str
    cgroup_device_inode: str

    def validate(self, config: Config) -> None:
        if self.slot not in config.slots or self.unit != config.unit(self.slot):
            raise SafetyError("snapshot identifies an unexpected slot or unit")
        if self.generation not in {"legacy", "current"}:
            raise SafetyError("snapshot generation is invalid")
        if self.cgroup != f"/system.slice/{self.unit}":
            raise SafetyError("snapshot cgroup does not match the exact unit")
        if re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", self.cgroup_device_inode) is None:
            raise SafetyError("snapshot cgroup identity is invalid")
        numeric = (
            self.main_pid,
            self.main_starttime,
            self.listener_pid,
            self.listener_starttime,
        )
        if any(not isinstance(item, int) or item < 0 for item in numeric):
            raise SafetyError("snapshot process identity is invalid")
        if self.running and any(item < 1 for item in numeric):
            raise SafetyError("running snapshot lacks exact process identity")
        if not self.running and any(item != 0 for item in numeric):
            raise SafetyError("stopped snapshot retains live process identity")
        if self.frozen and not self.running:
            raise SafetyError("a stopped slot cannot be frozen")


class Backend(Protocol):
    """Privileged observations and mutations required by the state machine.

    A real implementation must independently enforce the identities received
    from the controller.  This archived project intentionally provides no real
    implementation.
    """

    def snapshot(self, slot: str) -> SlotSnapshot: ...

    def prove_idle(self, slot: str) -> None: ...

    def freeze(self, slot: str) -> None: ...

    def thaw(self, slot: str) -> None: ...

    def stop_and_prove(self, slot: str, expected: SlotSnapshot) -> None: ...

    def apply_generation(self, slot: str, generation: str) -> None: ...

    def start_and_prove(self, slot: str, generation: str) -> SlotSnapshot: ...


class JournalStore:
    """Private, atomic lifecycle journal storage."""

    def __init__(self, root: Path) -> None:
        # Canonicalize only an absent leaf.  This accepts common physical-parent
        # aliases such as macOS /var -> /private/var without ever following an
        # existing state-root symlink.
        if not root.exists() and not root.is_symlink():
            root = root.parent.resolve() / root.name
        self.root = root
        self.journal = self.root / "journal.json"
        self.lock_path = self.root / "controller.lock"

    @staticmethod
    def _same_owner_mode(metadata: os.stat_result, mode: int) -> bool:
        return (
            metadata.st_uid == os.geteuid()
            and metadata.st_gid == os.getegid()
            and stat.S_IMODE(metadata.st_mode) == mode
        )

    def _validate_root(self) -> None:
        try:
            metadata = os.lstat(self.root)
        except OSError as error:
            raise SafetyError(f"state root cannot be inspected: {error}") from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or not self._same_owner_mode(metadata, 0o700)
            or self.root.resolve() != self.root
        ):
            raise SafetyError(
                "state root must be a physical same-owner mode-0700 directory"
            )

    def ensure_root(self) -> None:
        if self.root.exists() or self.root.is_symlink():
            self._validate_root()
            return
        try:
            self.root.mkdir(mode=0o700)
            os.chmod(self.root, 0o700)
        except OSError as error:
            raise SafetyError(f"state root cannot be created: {error}") from error
        self._validate_root()
        self._fsync_directory(self.root.parent)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.ensure_root()
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or not self._same_owner_mode(metadata, 0o600)
                or metadata.st_nlink != 1
            ):
                raise SafetyError("controller lock metadata is unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise SafetyError(
                    "another lifecycle controller owns the state root"
                ) from error
            yield
        finally:
            os.close(descriptor)

    def inspect(self) -> object | None:
        """Read the journal without creating a directory, lock, or file."""

        if not self.root.exists() and not self.root.is_symlink():
            return None
        self._validate_root()
        try:
            metadata = os.lstat(self.journal)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise SafetyError(f"journal cannot be inspected: {error}") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or not self._same_owner_mode(metadata, 0o600)
            or metadata.st_nlink != 1
            or metadata.st_size < 2
            or metadata.st_size > 131072
        ):
            raise SafetyError("journal metadata is unsafe")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.journal, flags)
        try:
            opened = os.fstat(descriptor)
            if opened.st_dev != metadata.st_dev or opened.st_ino != metadata.st_ino:
                raise SafetyError("journal identity changed while it was opened")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                payload = handle.read(131073)
        finally:
            os.close(descriptor)
        try:
            return json.loads(payload)
        except ValueError as error:
            raise SafetyError(f"journal is malformed: {error}") from error

    def save(self, value: object) -> None:
        self.ensure_root()
        payload = (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".journal.", dir=self.root)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.journal)
            self._fsync_directory(self.root)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


class LifecycleController:
    """Durable admission, mutation, verification, and recovery coordinator."""

    def __init__(
        self,
        config: Config,
        store: JournalStore,
        backend: Backend,
        *,
        now: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self.config = config
        self.store = store
        self.backend = backend
        self.now = now

    def _empty_state(self) -> dict[str, object]:
        return {
            "version": 1,
            "config_sha256": canonical_hash(
                {"slots": self.config.slots, "unit_template": self.config.unit_template}
            ),
            "slots": {
                slot: {"generation": "legacy", "evidence_sha256": "absent"}
                for slot in self.config.slots
            },
            "active": None,
        }

    def _validate_snapshot_dict(self, value: object, slot: str) -> SlotSnapshot:
        expected = {
            "slot",
            "generation",
            "unit",
            "running",
            "frozen",
            "main_pid",
            "main_starttime",
            "listener_pid",
            "listener_starttime",
            "cgroup",
            "cgroup_device_inode",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise SafetyError("journaled snapshot has unexpected fields")
        try:
            snapshot = SlotSnapshot(**value)
        except TypeError as error:
            raise SafetyError(f"journaled snapshot is invalid: {error}") from error
        snapshot.validate(self.config)
        if snapshot.slot != slot:
            raise SafetyError("journaled snapshot belongs to the wrong slot")
        return snapshot

    def _validate(self, value: object) -> dict[str, object]:
        if not isinstance(value, dict) or set(value) != {
            "version",
            "config_sha256",
            "slots",
            "active",
        }:
            raise SafetyError("journal has unexpected top-level fields")
        expected_config_hash = canonical_hash(
            {"slots": self.config.slots, "unit_template": self.config.unit_template}
        )
        if value["version"] != 1 or value["config_sha256"] != expected_config_hash:
            raise SafetyError("journal version or configuration identity changed")
        slots = value["slots"]
        if not isinstance(slots, dict) or set(slots) != set(self.config.slots):
            raise SafetyError("journal slot identity or order changed")
        for slot in self.config.slots:
            slot_state = slots[slot]
            if not isinstance(slot_state, dict) or set(slot_state) != {
                "generation",
                "evidence_sha256",
            }:
                raise SafetyError(f"slot {slot} journal shape is invalid")
            if slot_state["generation"] not in {"legacy", "current"}:
                raise SafetyError(f"slot {slot} generation is invalid")
            evidence = slot_state["evidence_sha256"]
            if evidence != "absent" and (
                not isinstance(evidence, str) or HASH_RE.fullmatch(evidence) is None
            ):
                raise SafetyError(f"slot {slot} evidence hash is invalid")
        active = value["active"]
        if active is None:
            return value
        expected_active = {
            "slot",
            "token_sha256",
            "step",
            "recover_from",
            "recovery_step",
            "original_generation",
            "desired_generation",
            "pre_evidence_sha256",
            "post_evidence_sha256",
            "deadline_epoch",
            "snapshot",
        }
        if not isinstance(active, dict) or set(active) != expected_active:
            raise SafetyError("active transaction has unexpected fields")
        slot = active["slot"]
        if slot not in self.config.slots or active["step"] not in STEPS:
            raise SafetyError("active transaction slot or step is invalid")
        if active["recover_from"] != "none" and active["recover_from"] not in STEPS - {
            "recovery-intent"
        }:
            raise SafetyError("active recovery origin is invalid")
        if active["recovery_step"] not in RECOVERY_STEPS:
            raise SafetyError("active recovery step is invalid")
        if active["step"] == "recovery-intent":
            if active["recover_from"] == "none" or active["recovery_step"] == "none":
                raise SafetyError("recovery intent lacks its durable origin or step")
        elif active["recover_from"] != "none" or active["recovery_step"] != "none":
            raise SafetyError("non-recovery transaction retains recovery state")
        for key in ("token_sha256", "pre_evidence_sha256"):
            if (
                not isinstance(active[key], str)
                or HASH_RE.fullmatch(active[key]) is None
            ):
                raise SafetyError(f"active transaction {key} is invalid")
        post = active["post_evidence_sha256"]
        if post != "none" and (
            not isinstance(post, str) or HASH_RE.fullmatch(post) is None
        ):
            raise SafetyError("active transaction post evidence is invalid")
        original = active["original_generation"]
        desired = active["desired_generation"]
        if original not in {"legacy", "current"} or desired not in {
            "legacy",
            "current",
        }:
            raise SafetyError("active transaction generation is invalid")
        if original == desired or slots[slot]["generation"] != original:
            raise SafetyError("active transaction generation disagrees with slot state")
        if (
            not isinstance(active["deadline_epoch"], int)
            or active["deadline_epoch"] < 1
        ):
            raise SafetyError("active transaction deadline is invalid")
        self._validate_snapshot_dict(active["snapshot"], slot)
        return value

    def inspect(self) -> dict[str, object] | None:
        value = self.store.inspect()
        if value is None:
            return None
        return self._validate(value)

    def _load(self) -> dict[str, object]:
        value = self.store.inspect()
        if value is None:
            raise SafetyError("lifecycle journal does not exist")
        return self._validate(value)

    def _save(self, state: dict[str, object]) -> None:
        self.store.save(self._validate(state))

    @staticmethod
    def _same_identity(expected: SlotSnapshot, observed: SlotSnapshot) -> bool:
        return all(
            (
                expected.slot == observed.slot,
                expected.generation == observed.generation,
                expected.unit == observed.unit,
                expected.main_pid == observed.main_pid,
                expected.main_starttime == observed.main_starttime,
                expected.listener_pid == observed.listener_pid,
                expected.listener_starttime == observed.listener_starttime,
                expected.cgroup == observed.cgroup,
                expected.cgroup_device_inode == observed.cgroup_device_inode,
            )
        )

    @staticmethod
    def _same_static_boundary(expected: SlotSnapshot, observed: SlotSnapshot) -> bool:
        return all(
            (
                expected.slot == observed.slot,
                expected.unit == observed.unit,
                expected.cgroup == observed.cgroup,
                expected.cgroup_device_inode == observed.cgroup_device_inode,
            )
        )

    def _assert_before_deadline(self, active: dict[str, object], action: str) -> None:
        if self.now() >= active["deadline_epoch"]:
            raise SafetyError(f"{action} deadline expired; journal authority retained")

    def initialize(self) -> dict[str, object]:
        with self.store.lock():
            if self.store.inspect() is not None:
                return self._load()
            state = self._empty_state()
            for slot in self.config.slots:
                snapshot = self.backend.snapshot(slot)
                snapshot.validate(self.config)
                if (
                    not snapshot.running
                    or snapshot.frozen
                    or snapshot.generation != "legacy"
                ):
                    raise SafetyError(
                        "initialization requires every slot running in legacy state"
                    )
            self._save(state)
            return state

    def prepare(
        self,
        slot: str,
        token: str,
        pre_evidence_sha256: str,
        *,
        desired_generation: str = "current",
    ) -> dict[str, object]:
        token_sha256 = token_hash(token)
        require_hash(pre_evidence_sha256, "pre-mutation provider evidence")
        if slot not in self.config.slots or desired_generation not in {
            "legacy",
            "current",
        }:
            raise UsageError("slot or desired generation is invalid")
        with self.store.lock():
            state = self._load()
            if state["active"] is not None:
                raise SafetyError("another transaction already owns the journal")
            original = state["slots"][slot]["generation"]
            if original == desired_generation:
                raise SafetyError("slot already uses the desired generation")
            self.backend.prove_idle(slot)
            snapshot = self.backend.snapshot(slot)
            snapshot.validate(self.config)
            if (
                not snapshot.running
                or snapshot.frozen
                or snapshot.generation != original
            ):
                raise SafetyError("slot is not an exact running candidate")
            state["active"] = {
                "slot": slot,
                "token_sha256": token_sha256,
                "step": "freeze-intent",
                "recover_from": "none",
                "recovery_step": "none",
                "original_generation": original,
                "desired_generation": desired_generation,
                "pre_evidence_sha256": pre_evidence_sha256,
                "post_evidence_sha256": "none",
                "deadline_epoch": self.now() + 90,
                "snapshot": asdict(snapshot),
            }
            self._save(state)
            self._assert_before_deadline(state["active"], "freeze")
            self.backend.freeze(slot)
            frozen = self.backend.snapshot(slot)
            frozen.validate(self.config)
            if not frozen.frozen or not self._same_identity(snapshot, frozen):
                raise SafetyError("slot identity changed across the freeze boundary")
            state["active"]["step"] = "frozen"
            self._save(state)
            return state

    def _assert_owner(
        self, state: dict[str, object], token: str, expected_step: str | None = None
    ) -> dict[str, object]:
        active = state["active"]
        if active is None or active["token_sha256"] != token_hash(token):
            raise SafetyError("operation token does not own the active transaction")
        if expected_step is not None and active["step"] != expected_step:
            raise SafetyError(f"transaction requires the exact {expected_step} step")
        return active

    def commit(self, token: str) -> dict[str, object]:
        with self.store.lock():
            state = self._load()
            active = self._assert_owner(state, token, "frozen")
            self._assert_before_deadline(active, "commit")
            slot = active["slot"]
            self.backend.prove_idle(slot)
            frozen = self.backend.snapshot(slot)
            frozen.validate(self.config)
            expected = self._validate_snapshot_dict(active["snapshot"], slot)
            if not frozen.frozen or not self._same_identity(expected, frozen):
                raise SafetyError("slot identity changed before commit")

            active["step"] = "stop-intent"
            active["deadline_epoch"] = self.now() + 1200
            self._save(state)
            self._assert_before_deadline(active, "stop")
            self.backend.stop_and_prove(slot, frozen)

            stopped = self.backend.snapshot(slot)
            stopped.validate(self.config)
            if (
                stopped.running
                or stopped.frozen
                or stopped.generation != active["original_generation"]
                or not self._same_static_boundary(frozen, stopped)
            ):
                raise SafetyError("slot did not reach the exact stopped boundary")
            active["snapshot"] = asdict(stopped)
            active["step"] = "stopped"
            self._save(state)
            active["step"] = "apply-intent"
            self._save(state)
            self._assert_before_deadline(active, "generation apply")
            self.backend.apply_generation(slot, active["desired_generation"])

            applied = self.backend.snapshot(slot)
            applied.validate(self.config)
            if (
                applied.running
                or applied.frozen
                or applied.generation != active["desired_generation"]
                or not self._same_static_boundary(stopped, applied)
            ):
                raise SafetyError("generation did not reach the exact applied boundary")
            active["snapshot"] = asdict(applied)
            active["step"] = "applied"
            self._save(state)
            active["step"] = "start-intent"
            self._save(state)
            self._assert_before_deadline(active, "start")
            started = self.backend.start_and_prove(slot, active["desired_generation"])
            started.validate(self.config)
            if (
                not started.running
                or started.frozen
                or started.generation != active["desired_generation"]
            ):
                raise SafetyError("slot did not return as an exact running listener")

            active["snapshot"] = asdict(started)
            active["step"] = "awaiting-verification"
            active["deadline_epoch"] = self.now() + 300
            self._save(state)
            return state

    def finalize(self, token: str, post_evidence_sha256: str) -> dict[str, object]:
        require_hash(post_evidence_sha256, "post-mutation provider evidence")
        with self.store.lock():
            state = self._load()
            active = self._assert_owner(state, token, "awaiting-verification")
            self._assert_before_deadline(active, "finalization")
            slot = active["slot"]
            self.backend.prove_idle(slot)
            observed = self.backend.snapshot(slot)
            observed.validate(self.config)
            expected = self._validate_snapshot_dict(active["snapshot"], slot)
            if (
                not observed.running
                or observed.frozen
                or observed.generation != active["desired_generation"]
                or not self._same_identity(expected, observed)
            ):
                raise SafetyError(
                    "post-mutation slot identity changed before finalization"
                )
            self._assert_before_deadline(active, "finalization")
            active["post_evidence_sha256"] = post_evidence_sha256
            state["slots"][slot] = {
                "generation": active["desired_generation"],
                "evidence_sha256": post_evidence_sha256,
            }
            state["active"] = None
            self._save(state)
            return state

    def abort(self, token: str) -> dict[str, object]:
        with self.store.lock():
            state = self._load()
            self._assert_owner(state, token)
            return self._recover_locked(state)

    def recover(self) -> dict[str, object]:
        with self.store.lock():
            state = self._load()
            if state["active"] is None:
                return state
            return self._recover_locked(state)

    def _recover_locked(self, state: dict[str, object]) -> dict[str, object]:
        active = state["active"]
        if active is None:
            return state
        if active["step"] != "recovery-intent":
            active["recover_from"] = active["step"]
            active["recovery_step"] = "observe"
            active["step"] = "recovery-intent"
        active["deadline_epoch"] = self.now() + 1200
        self._save(state)

        slot = active["slot"]
        original = active["original_generation"]
        desired = active["desired_generation"]
        self.backend.prove_idle(slot)
        while True:
            observed = self.backend.snapshot(slot)
            observed.validate(self.config)
            expected = self._validate_snapshot_dict(active["snapshot"], slot)
            recovery_step = active["recovery_step"]

            if recovery_step == "observe":
                if observed.running:
                    if observed.generation == original:
                        if not self._same_identity(expected, observed):
                            raise SafetyError(
                                "running original identity is ambiguous during recovery"
                            )
                        active["snapshot"] = asdict(observed)
                        active["recovery_step"] = (
                            "thaw-intent" if observed.frozen else "started"
                        )
                        self._save(state)
                        continue
                    if (
                        active["recover_from"] != "awaiting-verification"
                        or observed.generation != desired
                        or not self._same_identity(expected, observed)
                    ):
                        raise SafetyError(
                            "running desired identity is ambiguous during recovery"
                        )
                    active["snapshot"] = asdict(observed)
                    active["recovery_step"] = "stop-intent"
                    self._save(state)
                    continue

                allowed_stopped = {
                    "stop-intent": {original},
                    "stopped": {original},
                    "apply-intent": {original, desired},
                    "applied": {desired},
                    "start-intent": {desired},
                    "awaiting-verification": {desired},
                }.get(active["recover_from"], set())
                if (
                    observed.generation not in allowed_stopped
                    or not self._same_static_boundary(expected, observed)
                ):
                    raise SafetyError("stopped identity is ambiguous during recovery")
                active["snapshot"] = asdict(observed)
                active["recovery_step"] = "stopped"
                self._save(state)
                continue

            if recovery_step == "thaw-intent":
                if (
                    not observed.running
                    or observed.generation != original
                    or not self._same_identity(expected, observed)
                ):
                    raise SafetyError("original identity changed before recovery thaw")
                if observed.frozen:
                    self._assert_before_deadline(active, "recovery thaw")
                    self.backend.thaw(slot)
                    observed = self.backend.snapshot(slot)
                    observed.validate(self.config)
                if (
                    not observed.running
                    or observed.frozen
                    or observed.generation != original
                    or not self._same_identity(expected, observed)
                ):
                    raise SafetyError("recovery thaw did not preserve exact identity")
                active["snapshot"] = asdict(observed)
                active["recovery_step"] = "thawed"
                self._save(state)
                continue

            if recovery_step in {"thawed", "started"}:
                if (
                    not observed.running
                    or observed.frozen
                    or observed.generation != original
                    or not self._same_identity(expected, observed)
                ):
                    raise SafetyError("restored identity changed before journal clear")
                self._assert_before_deadline(active, "recovery completion")
                state["active"] = None
                self._save(state)
                return state

            if recovery_step == "stop-intent":
                if observed.running:
                    if observed.generation != desired or not self._same_identity(
                        expected, observed
                    ):
                        raise SafetyError(
                            "desired identity changed before recovery stop"
                        )
                    self._assert_before_deadline(active, "recovery stop")
                    self.backend.stop_and_prove(slot, observed)
                    observed = self.backend.snapshot(slot)
                    observed.validate(self.config)
                if (
                    observed.running
                    or observed.frozen
                    or observed.generation != desired
                    or not self._same_static_boundary(expected, observed)
                ):
                    raise SafetyError("recovery stop did not preserve static identity")
                active["snapshot"] = asdict(observed)
                active["recovery_step"] = "stopped"
                self._save(state)
                continue

            if recovery_step == "stopped":
                if (
                    observed.running
                    or observed.frozen
                    or observed.generation != expected.generation
                    or not self._same_static_boundary(expected, observed)
                ):
                    raise SafetyError("stopped recovery boundary changed")
                active["recovery_step"] = "apply-intent"
                self._save(state)
                continue

            if recovery_step == "apply-intent":
                if (
                    observed.running
                    or observed.frozen
                    or observed.generation not in {expected.generation, original}
                    or not self._same_static_boundary(expected, observed)
                ):
                    raise SafetyError(
                        "generation identity changed before recovery apply"
                    )
                if observed.generation != original:
                    self._assert_before_deadline(active, "recovery generation apply")
                    self.backend.apply_generation(slot, original)
                    observed = self.backend.snapshot(slot)
                    observed.validate(self.config)
                if (
                    observed.running
                    or observed.frozen
                    or observed.generation != original
                    or not self._same_static_boundary(expected, observed)
                ):
                    raise SafetyError("recovery apply did not restore static identity")
                active["snapshot"] = asdict(observed)
                active["recovery_step"] = "applied"
                self._save(state)
                continue

            if recovery_step == "applied":
                if (
                    observed.running
                    or observed.frozen
                    or observed.generation != original
                    or not self._same_static_boundary(expected, observed)
                ):
                    raise SafetyError("applied recovery boundary changed")
                active["recovery_step"] = "start-intent"
                self._save(state)
                continue

            if recovery_step == "start-intent":
                if observed.running:
                    raise SafetyError(
                        "recovery start outcome is ambiguous; journal retained"
                    )
                if (
                    observed.frozen
                    or observed.generation != original
                    or not self._same_static_boundary(expected, observed)
                ):
                    raise SafetyError("original identity changed before recovery start")
                self._assert_before_deadline(active, "recovery start")
                restored = self.backend.start_and_prove(slot, original)
                restored.validate(self.config)
                if (
                    not restored.running
                    or restored.frozen
                    or restored.generation != original
                ):
                    raise SafetyError("recovery did not restore original generation")
                active["snapshot"] = asdict(restored)
                active["recovery_step"] = "started"
                self._save(state)
                continue

            raise SafetyError("recovery journal step is not implemented")


class FakeBackend:
    """In-memory backend used by the examples and tests only."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.events: list[str] = []
        self.idle = {slot: True for slot in config.slots}
        self._sequence = 1000
        self.snapshots = {
            slot: self._running_snapshot(slot, "legacy") for slot in config.slots
        }

    def _running_snapshot(self, slot: str, generation: str) -> SlotSnapshot:
        self._sequence += 10
        return SlotSnapshot(
            slot=slot,
            generation=generation,
            unit=self.config.unit(slot),
            running=True,
            frozen=False,
            main_pid=self._sequence,
            main_starttime=self._sequence * 10,
            listener_pid=self._sequence + 1,
            listener_starttime=(self._sequence + 1) * 10,
            cgroup=f"/system.slice/{self.config.unit(slot)}",
            cgroup_device_inode=f"8:{self._sequence}",
        )

    def snapshot(self, slot: str) -> SlotSnapshot:
        if slot not in self.snapshots:
            raise SafetyError(f"fake backend has no slot {slot}")
        return self.snapshots[slot]

    def prove_idle(self, slot: str) -> None:
        self.events.append(f"prove-idle:{slot}")
        if not self.idle.get(slot, False):
            raise SafetyError(f"slot {slot} is busy")

    def freeze(self, slot: str) -> None:
        self.events.append(f"freeze:{slot}")
        snapshot = self.snapshot(slot)
        if not snapshot.running or snapshot.frozen:
            raise SafetyError("fake slot cannot be frozen from its current state")
        self.snapshots[slot] = replace(snapshot, frozen=True)

    def thaw(self, slot: str) -> None:
        self.events.append(f"thaw:{slot}")
        snapshot = self.snapshot(slot)
        if not snapshot.running:
            raise SafetyError("fake stopped slot cannot be thawed")
        self.snapshots[slot] = replace(snapshot, frozen=False)

    def stop_and_prove(self, slot: str, expected: SlotSnapshot) -> None:
        self.events.append(f"stop:{slot}")
        observed = self.snapshot(slot)
        if observed != expected or not observed.running:
            raise SafetyError("fake slot identity changed before stop")
        self.snapshots[slot] = replace(
            observed,
            running=False,
            frozen=False,
            main_pid=0,
            main_starttime=0,
            listener_pid=0,
            listener_starttime=0,
        )

    def apply_generation(self, slot: str, generation: str) -> None:
        self.events.append(f"apply-{generation}:{slot}")
        snapshot = self.snapshot(slot)
        if snapshot.running or generation not in {"legacy", "current"}:
            raise SafetyError("fake generation change requires an exact stopped slot")
        self.snapshots[slot] = replace(snapshot, generation=generation)

    def start_and_prove(self, slot: str, generation: str) -> SlotSnapshot:
        self.events.append(f"start-{generation}:{slot}")
        snapshot = self.snapshot(slot)
        if snapshot.running or snapshot.generation != generation:
            raise SafetyError("fake slot cannot start in the requested generation")
        started = self._running_snapshot(slot, generation)
        self.snapshots[slot] = started
        return started


def inspect_command(arguments: list[str]) -> int:
    if len(arguments) != 3 or arguments[0] != "inspect":
        raise UsageError(
            "usage: lifecycle_controller.py inspect CONFIG_JSON STATE_ROOT"
        )
    config = Config.from_json(Path(arguments[1]))
    store = JournalStore(Path(arguments[2]))
    value = store.inspect()
    if value is None:
        print(json.dumps({"initialized": False}, sort_keys=True))
        return 0
    # Validate before printing.  No backend or lock is constructed, so this
    # command cannot initialize or mutate lifecycle state.
    placeholder = FakeBackend(config)
    controller = LifecycleController(config, store, placeholder)
    print(json.dumps(controller.inspect(), indent=2, sort_keys=True))
    return 0


def main(arguments: list[str]) -> int:
    try:
        return inspect_command(arguments)
    except (SafetyError, UsageError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

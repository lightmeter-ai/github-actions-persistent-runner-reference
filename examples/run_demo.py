"""Run one complete lifecycle transaction against the in-memory backend."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Keep the checked-in file directly runnable from a fresh archive as well as
# importable through ``python -m examples.run_demo``. This path is synthetic and
# never loads configuration or code outside the repository checkout.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reference.lifecycle_controller import (
    Config,
    FakeBackend,
    JournalStore,
    LifecycleController,
    canonical_hash,
    new_operation_token,
)


def main() -> None:
    config = Config(
        slots=("runner-01", "runner-02"),
        unit_template="actions.runner.example-org.example-repo.{slot}.service",
    )
    backend = FakeBackend(config)
    token = new_operation_token()
    pre_evidence = canonical_hash({"runner": "runner-01", "idle": True, "sample": 1})
    post_evidence = canonical_hash(
        {"runner": "runner-01", "online": True, "idle": True, "sample": 2}
    )

    with tempfile.TemporaryDirectory(prefix="runner-lifecycle-demo.") as temporary:
        store = JournalStore(Path(temporary) / "state")
        controller = LifecycleController(config, store, backend)
        controller.initialize()
        controller.prepare("runner-01", token, pre_evidence)
        controller.commit(token)
        final = controller.finalize(token, post_evidence)

        print(json.dumps(final, indent=2, sort_keys=True))
        print("\nFake backend event order:")
        for event in backend.events:
            print(f"- {event}")


if __name__ == "__main__":
    main()

"""The shared receipt integrity key is written once, never rewritten.

Issue #1592. `execute_paired_run_plan` runs the sibling cells of a wave on
threads, and every cell writes its receipt into the same OMH home. One
integrity key serves that whole home, so a helper that rewrites the key per
receipt truncates it to zero underneath a sibling that is reading it. The
sibling's read returns a short key, verification fails, and the broad runner
boundary in `_execute_cell` records the cell as crashed with no receipt --
which the fan-in request then rejects as an unsealed result. The window is
about a millisecond wide, well inside one GIL switch interval, so it surfaced
as a shard that failed while the same file passed alone.

A single-file rerun of the fan-in test cannot catch a recurrence, and neither
can a repeated run: the race needs a scheduler drift that a quiet machine
rarely produces. These cases pin the invariant that removes it instead.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from omh.coding.hermes_child_receipts import load_hermes_child_receipt
from omh.coding.paired_run_execution import (
    ExecutionState,
    PairedRunExecutionOutcome,
    execute_paired_run_plan,
)
from paired_run_execution_support import plan as build_plan, receipt, workspace
from paired_run_support import write_observed_receipt

_KEY_FILENAME = ".observation-hmac-key"


class SharedReceiptKeyContentionTests(unittest.TestCase):
    def test_an_existing_home_key_is_kept_and_still_verifies_a_new_receipt(self) -> None:
        with TemporaryDirectory(prefix="omh-shared-key-") as raw:
            home = (Path(raw) / ".omh").resolve()
            root = home / "coding" / "hermes-child"
            root.mkdir(parents=True)
            existing = bytes(range(32))
            (root / _KEY_FILENAME).write_bytes(existing)

            write_observed_receipt(home, "run-1")

            self.assertEqual((root / _KEY_FILENAME).read_bytes(), existing)
            self.assertEqual(load_hermes_child_receipt(home, "run-1").status, "completed")

    def test_concurrent_cells_of_one_home_write_the_shared_key_once(self) -> None:
        writes: list[str] = []
        original = Path.write_bytes

        def counted_write_bytes(target: Path, data: bytes) -> int:
            if target.name == _KEY_FILENAME:
                writes.append(str(target))
            return original(target, data)

        with TemporaryDirectory(prefix="omh-shared-key-cells-") as raw:
            home = (Path(raw) / ".omh").resolve()
            plan = build_plan(provider_limit=2, shared_resource_key=None)
            Path.write_bytes = counted_write_bytes  # type: ignore[method-assign]
            try:
                report = execute_paired_run_plan(
                    plan,
                    workspace_factory=workspace,
                    runner=lambda cell, current: PairedRunExecutionOutcome(
                        ExecutionState.SUCCEEDED, receipt(home, cell, cell.workspace_id)
                    ),
                    cleaner=lambda cell, current: True,
                )
            finally:
                Path.write_bytes = original  # type: ignore[method-assign]

        self.assertEqual(len(plan.cells), 4)
        self.assertEqual(len(writes), 1, writes)
        self.assertEqual(
            [outcome.state for outcome in report.receipts],
            [ExecutionState.SUCCEEDED] * 4,
        )
        self.assertTrue(all(outcome.receipt is not None for outcome in report.receipts))


if __name__ == "__main__":
    unittest.main()

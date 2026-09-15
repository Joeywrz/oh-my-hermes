"""Contracts for the turn-end open-plan continuation directive.

An ULW run advances one item and stops: the plan still shows open items,
nothing is blocked, and the turn ends on a status report. `TODO_CONTINUATION_RULE`
already says the right thing, but `_open_plan_line` renders it into the context
of a turn that is ALREADY happening -- nothing observes a turn that ends with
open items, so continuation waits for the person.

`pre_verify` is the one moment a Hermes host lets a plugin start the next turn
instead of describing the current one. These tests pin what the directive says
and, at least as importantly, every state in which it must stay silent: no
plan, a finished plan, a next item recorded blocked with its reason, a second
attempt inside one turn, a non-coding turn, and another session's plan.

The last test is the anti-drift one: the context line and the directive must
decide "does this plan have open work" through the same helper, so a plan that
stops one has to stop the other.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh import runtime_paths, todo_reconciliation
from omh.plugin_bundle.omh.hooks import verify_hooks
from omh.plugin_bundle.omh.runtime_reader import read_omh_todo
from omh.plugin_bundle.omh.todo_reconciliation import (
    DISPATCH_COMPLETION_RULE,
    PLAN_CONTINUATION_BOUNDARY,
    TODO_CONTINUATION_RULE,
    open_plan_position,
    open_todo_reminder,
    plan_continuation_directive,
)
from omh.plugin_bundle.omh.todo_store import (
    TODO_SCHEMA_VERSION,
    build_todo_record,
    todo_path,
    write_todo,
)

FANOUT_ID = "fanout-0123456789ab"
SESSION = "tui-session"


class PlanContinuationDirectiveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "omh"
        self.hermes = Path(self._tmp.name) / "hermes"
        self.hermes.mkdir(parents=True, exist_ok=True)

    def _write_plan(self, items, session_ref=SESSION):
        """`items` is a list of (text, state) pairs, written as this session's plan."""
        record = build_todo_record(
            "plan",
            [{"text": text, "state": state} for text, state in items],
            source="test",
            session_ref=session_ref,
        )
        write_todo(self.home, record)
        return datetime.fromisoformat(record["updated_at"].replace("Z", "+00:00"))

    def _write_finished_unit(self, unit_id, finished_at):
        directory = self.home / "coding" / "fanout" / FANOUT_ID
        directory.mkdir(parents=True, exist_ok=True)
        _ = (directory / "dispatch_summary.json").write_text(
            json.dumps(
                {
                    "schema_version": "fanout_dispatch_summary/v1",
                    "fanout_id": FANOUT_ID,
                    "units": [
                        {
                            "unit_id": unit_id,
                            "run_ref": f"{FANOUT_ID}-{unit_id}",
                            "status": "completed",
                            "process_succeeded": True,
                            "finished_at": finished_at.isoformat().replace("+00:00", "Z"),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _fire(self, **overrides):
        payload = {
            "session_id": SESSION,
            "coding": True,
            "attempt": 0,
            # A plain source file: no served-surface category, so what comes
            # back is the plan directive alone.
            "changed_paths": ["src/example.py"],
            "omh_home": str(self.home),
            "hermes_home": str(self.hermes),
        }
        payload.update(overrides)
        return verify_hooks.pre_verify(**payload)

    def test_a_turn_ending_with_open_items_is_told_to_advance_the_next_one(self):
        self._write_plan([("land the fix", "done"), ("open the PR", "active"), ("report", "pending")])

        result = self._fire()

        self.assertEqual(result["action"], "continue")
        self.assertIn("[OMH plan todo] 1/3 done", result["message"])
        self.assertIn("next: open the PR", result["message"])
        self.assertIn(TODO_CONTINUATION_RULE, result["message"])
        self.assertIn(PLAN_CONTINUATION_BOUNDARY, result["message"])

    def test_the_next_item_is_the_first_pending_one_when_nothing_is_active(self):
        self._write_plan([("land the fix", "done"), ("open the PR", "pending"), ("report", "pending")])

        self.assertIn("next: open the PR", self._fire()["message"])

    def test_no_plan_does_not_nudge(self):
        self.assertIsNone(self._fire())

    def test_a_finished_plan_does_not_nudge(self):
        self._write_plan([("land the fix", "done"), ("open the PR", "done")])

        self.assertIsNone(self._fire())

    def test_a_next_item_recorded_blocked_with_its_reason_stops_the_nudge(self):
        # The plan's own stop criterion. Nudging here would argue with a
        # blocked item once per turn until `max_verify_nudges` ran out.
        self._write_plan(
            [
                ("land the fix", "done"),
                ("open the PR: blocked by the owner's review", "active"),
                ("release", "pending"),
            ]
        )

        self.assertIsNone(self._fire())

    def test_blocked_with_no_reason_is_not_a_stop_criterion(self):
        # "blocked" alone names nothing to wait for, and the rule the directive
        # carries asks for the reason. Without one the run still owes an answer.
        self._write_plan([("land the fix", "done"), ("blocked", "active")])

        self.assertIn(TODO_CONTINUATION_RULE, self._fire()["message"])

    def test_ordinary_prose_about_unblocking_is_not_a_block(self):
        self._write_plan([("land the fix", "done"), ("unblock the release queue", "active")])

        self.assertIn("next: unblock the release queue", self._fire()["message"])

    def test_a_second_attempt_inside_one_turn_does_not_nudge_again(self):
        self._write_plan([("land the fix", "done"), ("open the PR", "active")])

        self.assertIsNone(self._fire(attempt=1))

    def test_a_non_coding_turn_does_not_nudge(self):
        self._write_plan([("land the fix", "done"), ("open the PR", "active")])

        self.assertIsNone(self._fire(coding=False))

    def test_another_session_plan_is_not_this_session_open_work(self):
        self._write_plan(
            [("land the fix", "done"), ("open the PR", "active")], session_ref="slack-session"
        )

        self.assertIsNone(self._fire())

    def test_an_unacknowledged_dispatch_outcome_nudges_on_the_same_path(self):
        plan_updated_at = self._write_plan(
            [("land the fix", "done"), ("open the PR", "done")]
        )
        self._write_finished_unit("unit-a", plan_updated_at + timedelta(seconds=30))

        result = self._fire()

        self.assertEqual(result["action"], "continue")
        self.assertIn(f"dispatch {FANOUT_ID}-unit-a/unit-a ended completed", result["message"])
        self.assertIn(DISPATCH_COMPLETION_RULE, result["message"])
        # A finished plan says nothing about its position; the unacknowledged
        # outcome is the whole reason this turn is being kept open.
        self.assertNotIn("[OMH plan todo]", result["message"])

    def test_the_served_surface_check_and_the_plan_directive_ride_one_turn(self):
        self._write_plan([("land the fix", "done"), ("open the PR", "active")])

        message = self._fire(changed_paths=["src/app.tsx"])["message"]

        self.assertIn("rendered surface", message)
        self.assertIn("[OMH plan todo] 1/2 done", message)

    def test_a_corrupt_plan_record_is_silence_and_never_an_exception(self):
        # Hermes wraps the whole `pre_verify` call in `except Exception` and
        # logs at debug (`agent/turn_stop_gates.py`, `_pre_verify_nudge`), so a
        # handler that raised would end the turn with no trace -- the exact
        # symptom this directive exists to fix. Every malformed record below
        # must therefore come back as silence, not as a raise.
        path = todo_path(self.home, SESSION)
        path.parent.mkdir(parents=True, exist_ok=True)
        corrupt_records = (
            "{not json",
            "[]",
            json.dumps({"schema_version": "omh_todo/v0", "items": [{"text": "x", "state": "active"}]}),
            json.dumps({"schema_version": TODO_SCHEMA_VERSION, "items": "all of them"}),
            json.dumps({"schema_version": TODO_SCHEMA_VERSION, "items": ["not an item"]}),
            json.dumps({"schema_version": TODO_SCHEMA_VERSION, "items": [{"text": "x", "state": 7}]}),
            json.dumps({"schema_version": TODO_SCHEMA_VERSION, "counts": "1/3", "items": []}),
        )

        for record in corrupt_records:
            with self.subTest(record=record[:40]):
                _ = path.write_text(record, encoding="utf-8")

                self.assertIsNone(self._fire())

    def test_a_reader_that_fails_is_silence_and_never_an_exception(self):
        # The read touches the filesystem, so it can fail for reasons that have
        # nothing to do with the plan's shape. `RuntimeBindingError` is in the
        # list because it subclasses `ValueError`: an unbindable home must not
        # reach the host as a raise either.
        failures = (
            OSError("permission denied"),
            ValueError("bad payload"),
            TypeError("bad type"),
            runtime_paths.RuntimeBindingError("no safe store"),
        )

        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch.object(todo_reconciliation, "read_omh_todo", side_effect=failure):
                    self.assertIsNone(self._fire())

    def test_a_reader_returning_something_other_than_a_record_is_silence(self):
        for payload in (None, [], "plan"):
            with self.subTest(payload=payload):
                with patch.object(todo_reconciliation, "read_omh_todo", return_value=payload):
                    self.assertIsNone(self._fire())

    def test_the_plan_is_read_from_the_host_home_when_no_path_is_passed(self):
        # Hermes calls `pre_verify` with the documented kwargs and nothing
        # else -- there is no `omh_home` among them -- so the binding that runs
        # in production is the default one every other test here bypasses.
        self._write_plan([("land the fix", "done"), ("open the PR", "active")])

        with patch.dict(
            "os.environ", {"OMH_HOME": str(self.home), "HERMES_HOME": str(self.hermes)}
        ):
            result = verify_hooks.pre_verify(
                session_id=SESSION, coding=True, attempt=0, changed_paths=["src/example.py"]
            )

        self.assertIn("[OMH plan todo] 1/2 done", result["message"])

    def test_the_context_line_and_the_directive_share_one_gate(self):
        # Both surfaces are the same policy at two moments. If one grew its own
        # copy of "does this plan have open work", this is what would catch it.
        matrices = (
            [("a", "active")],
            [("a", "done"), ("b", "active")],
            [("a", "done"), ("b", "done")],
            [("a", "done"), ("b", "pending")],
        )

        for items in matrices:
            with self.subTest(items=items):
                self._write_plan(items)
                todo = read_omh_todo(str(self.home), str(self.hermes), session_ref=SESSION)
                open_work = open_plan_position(todo) is not None
                reminder = open_todo_reminder(
                    omh_home=str(self.home), hermes_home=str(self.hermes), session_ref=SESSION
                )
                directive = plan_continuation_directive(
                    omh_home=str(self.home), hermes_home=str(self.hermes), session_ref=SESSION
                )

                self.assertEqual("[OMH plan todo]" in reminder, open_work)
                self.assertEqual("[OMH plan todo]" in directive, open_work)


if __name__ == "__main__":
    unittest.main()

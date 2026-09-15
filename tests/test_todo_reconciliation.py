"""Contracts for the open-plan reconciliation reminder.

A completion claim in chat while the HUD checklist shows open items is a
visible contradiction the user reported directly. An open plan is a
state, not a phrasing, so the guard is stateful: while an established
todo has open items, every pre_llm_call turn carries one compact
reconciliation line; a finished, cleared, or absent plan carries none.

The second half of the same failure is a plan nobody contradicts and
nobody advances: while the reader's stall finding stands, the line also
states how long the checklist has been unchanged.

The same file carries the dispatch-completion chain: a finished unit the
plan does not name gets one line and the rule that names the verbs owed,
and a turn that promised to continue while nothing moved gets a finding.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from omh.plugin_bundle.omh.hooks.llm_hooks import pre_llm_call
from omh.plugin_bundle.omh.todo_reconciliation import (
    CONTINUATION_CLAIM_FINDING,
    DISPATCH_COMPLETION_RULE,
    TODO_CONTINUATION_RULE,
    TODO_RECONCILIATION_RULE,
    TODO_UNCHANGED_RULE,
    continuation_claim_without_resume,
    open_todo_reminder,
)
from omh.plugin_bundle.omh.todo_store import build_todo_record, todo_path, write_todo
from omh.plugin_bundle.omh.tool_bursts import record_tool_call, record_tool_call_close


class TodoReconciliationReminderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = str(Path(self._tmp.name) / "omh")

    def _write_plan(self, states, session_ref="", minutes_ago=0):
        items = [
            {"text": f"task {index}", "state": state, "phase": "Review"}
            for index, state in enumerate(states)
        ]
        record = build_todo_record("plan", items, source="test", session_ref=session_ref)
        if minutes_ago:
            stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
            record["updated_at"] = stamp.isoformat().replace("+00:00", "Z")
        write_todo(Path(self.home), record)
        return todo_path(Path(self.home), session_ref)

    def _observe_a_closed_tool_call(self):
        """Make liveness answerable with nothing left open.

        Without an observed ``post_tool_call`` the reader cannot read
        liveness either way and says so, so a stall test that skipped this
        would be asserting the unanswerable branch by accident.
        """
        record_tool_call("Bash", omh_home=self.home, tool_call_id="call-1")
        record_tool_call_close("call-1", omh_home=self.home)

    def _observe_an_open_tool_call(self):
        """Answerable liveness with one call still open.

        A closed call first, so ``post_tool_call_observed`` is true and the
        reader is not on its unanswerable branch; then an open one, which is
        the state a busy-but-unmoving run is in.
        """
        record_tool_call("Bash", omh_home=self.home, tool_call_id="call-1")
        record_tool_call_close("call-1", omh_home=self.home)
        record_tool_call("Bash", omh_home=self.home, tool_call_id="call-2")

    def test_the_reminder_binds_to_the_turn_session_own_plan(self):
        # A Slack session's open plan is not the TUI session's open work:
        # each turn reconciles against the plan its own session declared.
        self._write_plan(["done", "active", "pending"], session_ref="tui-session")
        self._write_plan(["done", "done"], session_ref="slack-session")

        self.assertIn(
            "[OMH plan todo] 1/3 done",
            open_todo_reminder(omh_home=self.home, session_ref="tui-session"),
        )
        self.assertEqual(open_todo_reminder(omh_home=self.home, session_ref="slack-session"), "")
        payload = pre_llm_call(user_message="다 됐어?", omh_home=self.home, session_id="slack-session")
        self.assertNotIn("[OMH plan todo]", str((payload or {}).get("context", "")))

    def test_an_open_plan_yields_one_compact_line(self):
        self._write_plan(["done", "active", "pending"])
        line = open_todo_reminder(omh_home=self.home)
        self.assertIn("[OMH plan todo] 1/3 done", line)
        self.assertIn("active: task 1", line)
        self.assertIn(TODO_RECONCILIATION_RULE, line)
        self.assertNotIn("\n", line)

    def test_a_finished_plan_yields_nothing(self):
        self._write_plan(["done", "done"])
        self.assertEqual(open_todo_reminder(omh_home=self.home), "")

    def test_an_absent_plan_yields_nothing(self):
        self.assertEqual(open_todo_reminder(omh_home=self.home), "")

    def test_pre_llm_call_carries_the_reminder_every_turn_while_open(self):
        self._write_plan(["done", "active", "pending"])
        for _ in range(2):
            payload = pre_llm_call(user_message="다 됐어?", omh_home=self.home)
            self.assertIsNotNone(payload)
            self.assertIn("[OMH plan todo] 1/3 done", str(payload.get("context", "")))
            self.assertIn("reconcile the checklist", str(payload.get("context", "")))

    def test_a_checklist_left_behind_for_hours_says_so_on_the_turn(self):
        # The observed failure: one item active, three behind it, untouched
        # for two hours while the session answered about other things and
        # ended its turns. The HUD computed the age and nothing said it.
        self._write_plan(["done", "active", "pending", "pending"], minutes_ago=120)
        self._observe_a_closed_tool_call()

        line = open_todo_reminder(omh_home=self.home)

        self.assertIn("unchanged 2h 00m, no tool call in flight", line)
        self.assertIn(TODO_UNCHANGED_RULE, line)
        self.assertIn(TODO_RECONCILIATION_RULE, line)
        self.assertNotIn("\n", line)
        # The line reports; it never upgrades the todo's own claim boundary.
        self.assertIn("not evidence that the work failed", line)
        # And it does ask for the plan to move, with the stop condition stated
        # in the same breath. A checklist that only ever catches a completion
        # claim is a detector; `omh_todo` exists to carry a goal across turns.
        self.assertIn(TODO_CONTINUATION_RULE, line)
        self.assertIn("advance the next item in this turn", line)
        # The stop condition names the field that carries it. It used to read
        # "recorded blocked with its reason", which a model could satisfy only
        # by writing prose into the item text -- and prose is what the turn-end
        # driver proved unreadable in both directions.
        self.assertIn("every item is done or an item carries an omh_todo blocked_reason", line)

    def test_a_plan_that_stops_moving_while_calls_run_is_still_a_finding(self):
        # The correction that matters most. Liveness excuses a short pause and
        # nothing longer: a run that spent 36 minutes retrying git workarounds
        # opened and closed tool calls the whole time, so a signal suppressed by
        # mere liveness would have stayed silent through the case it exists for.
        self._write_plan(["done", "active", "pending", "pending"], minutes_ago=120)
        self._observe_an_open_tool_call()

        line = open_todo_reminder(omh_home=self.home)

        self.assertIn("unchanged 2h 00m while calls kept running", line)
        self.assertNotIn("no tool call in flight", line)
        self.assertIn(TODO_CONTINUATION_RULE, line)

    def test_a_recently_updated_checklist_adds_no_unchanged_clause(self):
        # Negative control: the same open plan, moved just now. The clause
        # must be a finding, not ambient text on every open plan.
        self._write_plan(["done", "active", "pending"])
        self._observe_a_closed_tool_call()

        line = open_todo_reminder(omh_home=self.home)

        self.assertIn(TODO_RECONCILIATION_RULE, line)
        self.assertNotIn("unchanged", line)
        self.assertNotIn(TODO_UNCHANGED_RULE, line)

    def test_a_recent_checklist_stays_quiet_while_a_tool_call_is_open(self):
        # Liveness excuses a SHORT pause: a call is open and the plan moved a
        # minute ago, so the open call fully explains the gap and there is
        # nothing to report. Past the threshold the same open call explains
        # nothing, which is what the busy-finding test above pins -- an earlier
        # version of this module suppressed on liveness alone and would have
        # gone silent through a 36-minute retry loop.
        self._write_plan(["done", "active", "pending"], minutes_ago=1)
        self._observe_a_closed_tool_call()
        record_tool_call("Bash", omh_home=self.home, tool_call_id="call-running")

        self.assertNotIn("unchanged", open_todo_reminder(omh_home=self.home))

    def test_an_old_checklist_stays_quiet_when_liveness_is_unanswerable(self):
        # A host that never fires post_tool_call cannot answer "is anything
        # running": its ledger entries can only expire, never close. Silence
        # there must not be inverted into a stall.
        self._write_plan(["done", "active", "pending"], minutes_ago=120)

        self.assertNotIn("unchanged", open_todo_reminder(omh_home=self.home))

    def test_the_awareness_opt_out_suppresses_the_reminder(self):
        self._write_plan(["done", "active", "pending"])
        payload = pre_llm_call(
            user_message="다 됐어?", omh_home=self.home, include_omh_awareness=False
        )
        self.assertNotIn("[OMH plan todo]", str((payload or {}).get("context", "")))


class DispatchOutcomeReminderTest(unittest.TestCase):
    """The reminder names the finished dispatch nobody has written down."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "omh"

    def _write_plan(self):
        record = build_todo_record(
            "plan",
            [{"text": "wait for the workers", "state": "active"}, {"text": "merge", "state": "pending"}],
            source="test",
        )
        write_todo(self.home, record)
        return datetime.fromisoformat(record["updated_at"].replace("Z", "+00:00"))

    def _write_finished_units(self, count):
        plan_at = self._write_plan()
        fanout_id = "fanout-0123456789ab"
        directory = self.home / "coding" / "fanout" / fanout_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "dispatch_summary.json").write_text(
            json.dumps(
                {
                    "schema_version": "fanout_dispatch_summary/v1",
                    "fanout_id": fanout_id,
                    "units": [
                        {
                            "unit_id": f"unit-{index}",
                            "run_ref": f"{fanout_id}-unit-{index}",
                            "status": "failed",
                            "failure_kind": "limit_shaped",
                            "finished_at": (
                                plan_at + timedelta(seconds=30 + index)
                            ).isoformat().replace("+00:00", "Z"),
                        }
                        for index in range(count)
                    ],
                }
            ),
            encoding="utf-8",
        )
        return fanout_id

    def test_one_line_per_outcome_carries_the_state_and_the_verb(self):
        fanout_id = self._write_finished_units(1)

        reminder = open_todo_reminder(omh_home=str(self.home))

        self.assertIn(
            f"dispatch {fanout_id}-unit-0/unit-0 ended limit_shaped; next: record_blocked",
            reminder,
        )
        self.assertIn(DISPATCH_COMPLETION_RULE, reminder)
        # The open-plan line stays: both obligations are live at once.
        self.assertIn("[OMH plan todo] 0/2 done", reminder)

    def test_the_outcome_lines_are_capped_with_a_remainder_count(self):
        self._write_finished_units(6)

        reminder = open_todo_reminder(omh_home=str(self.home))
        outcome_lines = [line for line in reminder.splitlines() if line.startswith("dispatch ")]

        self.assertEqual(len(outcome_lines), 3)
        self.assertIn("(+3 more)", reminder)

    def test_a_plan_that_names_the_run_gets_no_outcome_line(self):
        fanout_id = "fanout-0123456789ab"
        record = build_todo_record(
            "plan",
            [{"text": f"verify {fanout_id}-unit-0", "state": "active"}],
            source="test",
        )
        write_todo(self.home, record)
        plan_at = datetime.fromisoformat(record["updated_at"].replace("Z", "+00:00"))
        directory = self.home / "coding" / "fanout" / fanout_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "dispatch_summary.json").write_text(
            json.dumps(
                {
                    "schema_version": "fanout_dispatch_summary/v1",
                    "fanout_id": fanout_id,
                    "units": [
                        {
                            "unit_id": "unit-0",
                            "run_ref": f"{fanout_id}-unit-0",
                            "status": "completed",
                            "process_succeeded": True,
                            "finished_at": (plan_at + timedelta(seconds=30))
                            .isoformat()
                            .replace("+00:00", "Z"),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        reminder = open_todo_reminder(omh_home=str(self.home))

        self.assertNotIn("dispatch ", reminder)
        self.assertNotIn(DISPATCH_COMPLETION_RULE, reminder)

    def test_pre_llm_call_carries_the_outcome_line_and_the_rule(self):
        fanout_id = self._write_finished_units(1)

        payload = pre_llm_call(user_message="상태 어때?", omh_home=str(self.home))

        context = str((payload or {}).get("context", ""))
        self.assertIn(f"dispatch {fanout_id}-unit-0/unit-0 ended", context)
        self.assertIn(DISPATCH_COMPLETION_RULE, context)


class ContinuationClaimGuardTest(unittest.TestCase):
    """A promise to continue is a finding only when nothing was started."""

    def test_a_claim_with_a_stalled_plan_is_a_finding(self):
        # Derived from the reader's own status set rather than a copy of it: a
        # third unchanged status added there has to reach this guard too.
        from omh.plugin_bundle.omh.runtime_reader import TODO_UNCHANGED_STATUSES

        self.assertEqual(TODO_UNCHANGED_STATUSES, {"unchanged", "unchanged_while_busy"})
        for status in sorted(TODO_UNCHANGED_STATUSES):
            with self.subTest(status=status):
                self.assertEqual(
                    continuation_claim_without_resume(
                        "확인했습니다. 계속 진행하겠습니다.",
                        todo_stall_status=status,
                        unacknowledged=0,
                    ),
                    CONTINUATION_CLAIM_FINDING,
                )

    def test_a_claim_with_an_unacknowledged_outcome_is_a_finding(self):
        self.assertEqual(
            continuation_claim_without_resume(
                "Worker 2 finished. Continuing with the remaining lanes.",
                todo_stall_status="advanced",
                unacknowledged=1,
            ),
            CONTINUATION_CLAIM_FINDING,
        )

    def test_a_claim_on_a_live_plan_with_nothing_outstanding_is_not_a_finding(self):
        self.assertIsNone(
            continuation_claim_without_resume(
                "다음 항목으로 계속 진행합니다.",
                todo_stall_status="advanced",
                unacknowledged=0,
            )
        )

    def test_a_stalled_plan_without_a_claim_is_not_a_finding(self):
        self.assertIsNone(
            continuation_claim_without_resume(
                "Unit 3 is blocked on a case-only filename collision; stopping here.",
                todo_stall_status="unchanged_while_busy",
                unacknowledged=2,
            )
        )

    def test_empty_text_is_never_a_finding(self):
        self.assertIsNone(
            continuation_claim_without_resume(
                "", todo_stall_status="unchanged", unacknowledged=3
            )
        )

    def test_pre_llm_call_flags_last_turns_unkept_promise(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "omh"
            fanout_id = "fanout-0123456789ab"
            record = build_todo_record(
                "plan", [{"text": "wait", "state": "active"}], source="test"
            )
            write_todo(home, record)
            plan_at = datetime.fromisoformat(record["updated_at"].replace("Z", "+00:00"))
            directory = home / "coding" / "fanout" / fanout_id
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "dispatch_summary.json").write_text(
                json.dumps(
                    {
                        "schema_version": "fanout_dispatch_summary/v1",
                        "fanout_id": fanout_id,
                        "units": [
                            {
                                "unit_id": "unit-0",
                                "run_ref": f"{fanout_id}-unit-0",
                                "status": "failed",
                                "failure_kind": "crash",
                                "finished_at": (plan_at + timedelta(seconds=30))
                                .isoformat()
                                .replace("+00:00", "Z"),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            payload = pre_llm_call(
                user_message="어떻게 되고 있어?",
                omh_home=str(home),
                conversation_history=[
                    {"role": "user", "content": "상태 알려줘"},
                    {"role": "assistant", "content": "워커 2 종료. 계속 진행하겠습니다."},
                ],
            )

            self.assertIn("[OMH continuation claim]", str((payload or {}).get("context", "")))

    def test_pre_llm_call_stays_quiet_when_the_last_turn_promised_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "omh"
            write_todo(
                home,
                build_todo_record(
                    "plan", [{"text": "wait", "state": "active"}], source="test"
                ),
            )

            payload = pre_llm_call(
                user_message="어떻게 되고 있어?",
                omh_home=str(home),
                conversation_history=[
                    {"role": "assistant", "content": "Unit 3 is blocked; stopping here."},
                ],
            )

            self.assertNotIn("[OMH continuation claim]", str((payload or {}).get("context", "")))


if __name__ == "__main__":
    unittest.main()

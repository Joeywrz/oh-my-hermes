"""Contracts for a turn someone opened with a message while a plan is open.

The reported failure: mid-plan the owner asked why something might be a
model-level problem. The session agreed with nothing checked, pivoted back to
"next steps, how shall we proceed?", and when challenged said to ask again and
it would fact-check. `TODO_CONTINUATION_RULE` is in the context of that very
turn saying the plan stops "not when a turn has produced an answer", so the
question is answered as cheaply as possible and the plan resumes.

The gate is an identity, not an interpretation, and the Hermes source is what
makes that true. `pre_llm_call` is invoked exactly once per turn, from
`build_turn_context`, with `original_user_message` -- the message that started
the turn, which the host keeps free of its own nudge injection. A turn the
session drove onward by itself never re-enters the hook: `apply_stop_gates`
appends the `pre_verify` directive as a synthetic user-role row and re-enters
the SAME turn loop. So nothing OMH wrote can arrive as this message, and its
presence says the turn owes someone an answer.

Hence what these tests pin hardest is the negative: the gate reads presence
and never wording. Two messages that mean opposite things produce one
identical line, and a non-string or blank message is absence. Deciding "is
this a question" from text is the inference #1549 removed from this module for
being wrong in both directions in every language it was tried in.

The byte pin below is the other half. A turn no message opened must render
exactly what it rendered before this change, and the literal was taken by
running the same fixture against `origin/main` in a detached worktree, not by
assembling it from the constants beside it.

`test_plan_deferral.py` owns the recorded-deferral half of the same precedence
chain; only the interaction between the two is pinned here.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh import todo_reconciliation
from omh.plugin_bundle.omh.hooks import nudge_budget, verify_hooks
from omh.plugin_bundle.omh.hooks.llm_hooks import pre_llm_call
from omh.plugin_bundle.omh.todo_reconciliation import (
    TODO_ANSWER_FIRST_RULE,
    TODO_CONTINUATION_RULE,
    TODO_DEFERRED_RULE,
    TODO_RECONCILIATION_RULE,
    TODO_UNCHANGED_RULE,
    open_todo_reminder,
    turn_opened_by_message,
)
from omh.plugin_bundle.omh.todo_store import build_todo_record, write_todo

SESSION = "tui-session"
REDIRECTED = "the person asked for the release notes first"

# A question mid-plan, and its opposite: an instruction to keep going. Both are
# messages, neither is read. Kept as module constants so the pair is obviously
# one pair and nobody "fixes" one of them into agreeing with the other.
A_QUESTION = "왜 이게 모델 레벨 문제일 수 있다는 거야?"
AN_INSTRUCTION = "stop asking me things and finish the plan"

# What a turn no message opened renders, byte for byte, measured against
# `origin/main`. Written out rather than composed from the rule constants: a
# claim assembled from the values it checks agrees with any change to them.
LINE_WITH_NO_INBOUND_MESSAGE = (
    "[OMH plan todo] 1/2 done · active: open the PR. "
    "Open items mean this plan is not finished. Unless something is blocking "
    "it, advance the next item in this turn rather than ending on a status "
    "report. This stops when every item is done or an item carries an "
    "omh_todo blocked_reason -- not when a turn has produced an answer. "
    "Before claiming this work is finished, reconcile the checklist with "
    "omh_todo: mark completed items done, keep exactly one item active, and "
    "either finish the remaining items or say which stay open and why. A "
    "completion claim in chat while the HUD checklist shows open items is a "
    "visible contradiction. Todo updates are declarations, never execution "
    "evidence."
)


class _PlanHomeTest(unittest.TestCase):
    """A real OMH home with one session's plan in it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "omh"
        self.hermes = Path(self._tmp.name) / "hermes"
        self.hermes.mkdir(parents=True, exist_ok=True)
        # The turn-end budget keeps its last-nudge baseline in a process-global
        # map and the CI shard planner reorders tests run to run, so a baseline
        # left behind here surfaces as a failure in whichever test runs next.
        nudge_budget.reset_nudge_budget()
        self.addCleanup(nudge_budget.reset_nudge_budget)

    def _write_plan(self, items, *, deferred_reason="", session_ref=SESSION):
        """`items` is a list of `(text, state)` pairs, or `(text, state, blocked_reason)`."""
        record = build_todo_record(
            "plan",
            [
                {
                    "text": item[0],
                    "state": item[1],
                    "blocked_reason": item[2] if len(item) > 2 else "",
                }
                for item in items
            ],
            source="test",
            session_ref=session_ref,
            deferred_reason=deferred_reason,
        )
        _ = write_todo(self.home, record)
        return record

    def _reminder(self, user_message=""):
        return open_todo_reminder(
            omh_home=str(self.home),
            hermes_home=str(self.hermes),
            session_ref=SESSION,
            user_message=user_message,
        )

    def _open_plan(self):
        return self._write_plan([("land the fix", "done"), ("open the PR", "active")])


class MessageOpenedTurnTest(_PlanHomeTest):
    def test_a_message_opening_the_turn_replaces_the_advance_ask(self):
        _ = self._open_plan()

        line = self._reminder(A_QUESTION)

        self.assertIn("[OMH plan todo] 1/2 done", line)
        self.assertIn(TODO_ANSWER_FIRST_RULE, line)
        # The whole point. The drive is what told the model that producing an
        # answer does not discharge the turn, and it is replaced rather than
        # softened: a line that both serves the person and asks for the next
        # item is the argument `TODO_DEFERRED_RULE` exists to end.
        self.assertNotIn(TODO_CONTINUATION_RULE, line)
        self.assertNotIn("advance the next item in this turn", line)
        # Unrelated to who is owed the turn: a completion claim while the
        # checklist shows open items is still a visible contradiction.
        self.assertIn(TODO_RECONCILIATION_RULE, line)
        self.assertNotIn("\n", line)

    def test_the_rule_names_the_two_things_the_session_actually_did(self):
        # Not a restatement of the constant: these are the two behaviours in
        # the report, and a rewording that drops either one is the defect
        # coming back. The session agreed a claim it had not checked, then
        # offered to check it if asked a second time.
        _ = self._open_plan()

        line = self._reminder(A_QUESTION)

        self.assertIn("do not ask to be asked again", line)
        self.assertIn("Do not agree with a claim you have not checked", line)
        # And it still ends on the plan, which is what keeps this a reordering
        # of the drive rather than the observe-only reminder that was rejected.
        self.assertIn("or resume the plan", line)

    def test_the_last_clause_offers_the_record_before_the_resume(self):
        # The gate above is presence-only and must stay so, which means one
        # sentence answers "stop, forget the plan" and "carry on" alike. While
        # the resume came first it was therefore the DEFAULT reply to someone
        # who had just said stop, and the only way out of it was a
        # `deferred_reason` write they never asked for. Both options and the
        # stop criterion are unchanged; which one leads is the whole change,
        # so the order is pinned by position rather than by membership.
        _ = self._open_plan()

        line = self._reminder(A_QUESTION)

        self.assertLess(
            line.index("record an omh_todo deferred_reason"), line.index("resume the plan")
        )
        self.assertIn(
            "Then either record an omh_todo deferred_reason, if they steered "
            "the work elsewhere, or resume the plan.",
            line,
        )
        # The clause that made the resume the default is gone, and so is the
        # one that framed the alternative as an argument to be won.
        self.assertNotIn("Then resume the plan", line)
        self.assertNotIn("rather than arguing for the next item", line)

    def test_a_turn_no_message_opened_renders_exactly_what_it_did_before(self):
        # The regression pin for every session OMH drives by itself. Equality,
        # not containment: an extra clause appended to this line is exactly the
        # kind of change that passes a membership assertion.
        _ = self._open_plan()

        self.assertEqual(self._reminder(), LINE_WITH_NO_INBOUND_MESSAGE)

    def test_the_line_is_the_same_for_two_messages_that_mean_opposite_things(self):
        # The guard against this becoming a text matcher. One asks a question,
        # the other orders the session to stop asking and finish; the gate is
        # allowed to see that both exist and nothing else.
        _ = self._open_plan()

        self.assertEqual(self._reminder(A_QUESTION), self._reminder(AN_INSTRUCTION))
        self.assertIn(TODO_ANSWER_FIRST_RULE, self._reminder(AN_INSTRUCTION))

    def test_absence_is_anything_that_is_not_text_someone_sent(self):
        # `""` and whitespace are a host with nothing to hand over, and a
        # non-string is corruption. Both fail toward the behaviour that shipped,
        # the same call `recorded_blocked_reason` makes for the same reason.
        for value in ("", "   ", "\n\t ", None, 7, {"content": "hi"}, ["hi"]):
            with self.subTest(value=value):
                self.assertFalse(turn_opened_by_message(value))
        for value in (A_QUESTION, AN_INSTRUCTION, "?", " ok "):
            with self.subTest(value=value):
                self.assertTrue(turn_opened_by_message(value))

    def test_a_blank_message_leaves_the_line_the_drive_renders(self):
        _ = self._open_plan()

        self.assertEqual(self._reminder("   "), LINE_WITH_NO_INBOUND_MESSAGE)

    def test_a_stalled_plan_does_not_re_add_the_advance_ask_under_a_message(self):
        # "unchanged 40m ... if nothing is blocking it, move it" would put the
        # ask back in the same breath the message branch just took it out of --
        # the failure the deferred branch already drops the stall half for. The
        # reader's verdict is patched rather than aged on disk: what is under
        # test is which line a stalled plan renders, not how the verdict is
        # reached.
        todo = {
            "status": "established",
            "counts": {"total": 2, "done": 1, "active": 1, "pending": 0, "phases": 0},
            "items": [
                {"text": "land the fix", "state": "done"},
                {"text": "open the PR", "state": "active"},
            ],
            "updated_age_seconds": 4000.0,
            "stall": {"status": "unchanged"},
        }

        with patch.object(todo_reconciliation, "read_omh_todo", return_value=todo):
            line = self._reminder(A_QUESTION)
            drive_line = self._reminder()

        self.assertIn(TODO_ANSWER_FIRST_RULE, line)
        self.assertNotIn(TODO_UNCHANGED_RULE, line)
        self.assertNotIn("unchanged", line)
        # The negative control: the same stalled plan on a turn nobody opened
        # still reports the stall and still asks for the next item.
        self.assertIn(TODO_UNCHANGED_RULE, drive_line)
        self.assertIn(TODO_CONTINUATION_RULE, drive_line)


class MessagePrecedenceTest(_PlanHomeTest):
    def test_a_recorded_deferral_still_wins_over_the_message(self):
        # The deferral is the more specific statement -- it names what was
        # asked for instead, and it lapses on its own when the items move --
        # so a plan already recorded as redirected keeps its line whether or
        # not someone wrote this turn.
        _ = self._write_plan(
            [("land the fix", "done"), ("open the PR", "active")], deferred_reason=REDIRECTED
        )

        line = self._reminder(A_QUESTION)

        self.assertIn(f"deferred: {REDIRECTED}", line)
        self.assertIn(TODO_DEFERRED_RULE, line)
        self.assertNotIn(TODO_ANSWER_FIRST_RULE, line)
        self.assertNotIn(TODO_CONTINUATION_RULE, line)
        # And it is the same line the deferral renders with no message at all,
        # so the deferred branch is untouched by this change.
        self.assertEqual(line, self._reminder())

    def test_a_blocked_next_item_still_vetoes_the_deferral_under_a_message(self):
        # The block is the stronger statement about why the plan is not moving,
        # so it suppresses the deferred line exactly as before. What is left is
        # the message branch, not the drive: the block is not something to
        # advance, and someone is still owed an answer.
        items = [("land the fix", "done"), ("open the PR", "active", "the owner's review")]
        _ = self._write_plan(items, deferred_reason=REDIRECTED)

        line = self._reminder(A_QUESTION)

        self.assertNotIn(TODO_DEFERRED_RULE, line)
        self.assertNotIn(REDIRECTED, line)
        self.assertIn(TODO_ANSWER_FIRST_RULE, line)
        # Unchanged where nobody wrote: a blocked item has never stopped this
        # surface's drive, only the turn-end directive's.
        self.assertEqual(self._reminder(), LINE_WITH_NO_INBOUND_MESSAGE)

    def test_no_plan_means_no_line_however_the_turn_started(self):
        for message in ("", A_QUESTION):
            with self.subTest(message=message):
                self.assertEqual(self._reminder(message), "")

    def test_a_finished_plan_stays_silent_when_a_message_opened_the_turn(self):
        _ = self._write_plan([("land the fix", "done"), ("open the PR", "done")])

        for message in ("", A_QUESTION):
            with self.subTest(message=message):
                self.assertEqual(self._reminder(message), "")


class MessageReachesTheLineTest(_PlanHomeTest):
    """The module change is only real if the hook hands the message over."""

    def _context(self, **kwargs):
        payload = pre_llm_call(
            omh_home=str(self.home), hermes_home=str(self.hermes), session_id=SESSION, **kwargs
        )
        return str((payload or {}).get("context", ""))

    def test_the_hook_passes_the_turn_message_through(self):
        _ = self._open_plan()

        self.assertIn(TODO_ANSWER_FIRST_RULE, self._context(user_message=A_QUESTION))

    def test_the_hook_keeps_the_drive_when_the_host_hands_over_no_message(self):
        # A host that does not pass one, which is every caller that predates
        # this parameter. A plugin that cannot see whether anyone wrote must
        # not begin claiming they did.
        _ = self._open_plan()

        self.assertIn(TODO_CONTINUATION_RULE, self._context())

    def test_a_host_labelled_tracker_event_is_not_someone_writing(self):
        # `pre_llm_call` already zeroes the message for a host-labelled GitHub
        # event, and the plan line reads that variable rather than the raw
        # kwarg. An event is a thing that happened, not a turn owed an answer,
        # so the drive stays.
        _ = self._open_plan()

        context = self._context(
            user_message="issue body pasted by the tracker",
            tracker_event={"provider": "github", "tracker_content": "..."},
        )

        self.assertIn(TODO_CONTINUATION_RULE, context)
        self.assertNotIn(TODO_ANSWER_FIRST_RULE, context)


class TurnEndDirectiveUnaffectedTest(_PlanHomeTest):
    """The turn-end driver keeps the drive, and could not gate on a message."""

    def _directive(self, **overrides):
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

    def test_the_directive_still_drives_and_ignores_any_message_kwarg(self):
        # It fires at the END of a turn that already produced the answer it is
        # handed as `final_response`, which is the moment
        # `TODO_ANSWER_FIRST_RULE` itself asks the plan to resume. The two
        # agree, so the gate does not belong here -- and a host kwarg named
        # `user_message`, which Hermes never sends, must not change it either.
        _ = self._open_plan()

        plain = self._directive()["message"]
        nudge_budget.reset_nudge_budget()
        with_message = self._directive(user_message=A_QUESTION)["message"]

        self.assertIn(TODO_CONTINUATION_RULE, plain)
        self.assertEqual(plain, with_message)


if __name__ == "__main__":  # pragma: no cover - module entry point
    unittest.main()

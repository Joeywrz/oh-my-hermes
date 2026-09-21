"""Contracts for the edit gate on a plan nobody has accepted yet.

`ralplan`'s skill has said "stop at the reviewed plan" in three places since it
shipped, including a worked example built from the exact sentence the owner
reported. A run given implementation intent implements anyway, and routing is
not what failed: that sentence dispatches to `ralplan` at score 12 with
`next_action: present_plan`. What was missing was anything that binds once the
run is under way, so `plan_stage_gate` escalates the first `write_file` or
`patch` of an unaccepted plan run to the host's human-approval gate -- the one
`pre_tool_call` directive a model cannot decline.

Two things are proved here and everything else is edges around them.

`PlanStageFieldDecidesTest` is the one that proves the design. The record's
`plan_stage` field is mutated between two otherwise identical calls and the
verdict follows the FIELD, while a record whose title and every item text
argue the opposite case changes nothing. This repository has paid for prose
matching before -- the plan's stop criterion used to be read out of item text
and got "verify the retry is not blocked on the session limit" wrong in one
direction and "waiting on the owner's review" wrong in the other -- so the
guard against its return is a mutation table, not an assertion that today's
wording happens to work.

`PlanStageUnknownIsSilentTest` is the other half, and it is the half that
decides whether this is shippable. The approval gate is the strongest thing
OMH can do to a person's turn, so every state the records cannot settle
answers None: no record, another session's record, a stale one, a finished
plan, an unreadable home, a stamp this build cannot classify, a call with no
session, and every tool that is not one of the host's two file-mutating ones.
A wrong escalation stops work; a missed one leaves the person exactly where
the prose rule already left them.

The unattended case has its own class because it is a product decision rather
than a defensive default. `request_tool_approval` fails CLOSED with no person
present, so escalating on `hermes chat -q`, a webhook, or a delegated child
would not ask a question -- it would refuse the edit with the host's wording,
for a rule nobody can answer. That is the opposite of what this gate is for,
so it is withheld there.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh import plan_stage_gate
from omh.plugin_bundle.omh.engagement_nudges import FILE_MUTATING_TOOLS
from omh.plugin_bundle.omh.hooks import nudge_budget, session_attendance
from omh.plugin_bundle.omh.hooks.tool_hooks import pre_tool_call
from omh.plugin_bundle.omh.plan_stage_gate import (
    PLAN_STAGE_APPROVAL_MESSAGE,
    PLAN_STAGE_RULE_KEY,
    plan_stage_edit_directive,
)
from omh.plugin_bundle.omh.runtime_reader import read_omh_todo
from omh.plugin_bundle.omh.todo_store import (
    PLAN_STAGE_ACCEPTED,
    PLAN_STAGE_AWAITING_ACCEPTANCE,
    TODO_PLAN_STAGES,
    TODO_STALE_SECONDS,
    TodoValidationError,
    advance_todo_item,
    build_todo_record,
    todo_path,
    write_todo,
)
from omh.plugin_bundle.omh.tools.todo_tool import OMH_TODO_SCHEMA, omh_todo_handler

SESSION = "20260921_101010_abc123"
OTHER_SESSION = "20260921_202020_def456"
PLANNING_ITEMS = [
    {"text": "repo facts and evidence check", "state": "done"},
    {"text": "options and tradeoffs", "state": "active"},
    {"text": "acceptance criteria and verification commands", "state": "pending"},
]


class _PlanStageHomeTest(unittest.TestCase):
    """A real OMH home and an empty Hermes home, with one session's plan in it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "omh"
        self.hermes = Path(self._tmp.name) / "hermes"
        self.hermes.mkdir(parents=True, exist_ok=True)
        # Both process-global maps the attendance predicate reads, cleared
        # through their own named seams. The CI shard planner reorders tests
        # run to run, so a platform or a delegated-child marker left behind
        # here fails whichever test runs next instead of this one.
        session_attendance.reset_session_attendance()
        nudge_budget.reset_nudge_budget()
        self.addCleanup(session_attendance.reset_session_attendance)
        self.addCleanup(nudge_budget.reset_nudge_budget)

    def write_plan(self, *, plan_stage="", session_ref=SESSION, items=None, title="Reviewed plan"):
        record = build_todo_record(
            title,
            [dict(item) for item in (items if items is not None else PLANNING_ITEMS)],
            source="omh_todo",
            session_ref=session_ref,
            plan_stage=plan_stage,
        )
        _ = write_todo(self.home, record)
        return record

    def rewrite(self, record):
        """Put a record on disk exactly as given, bypassing the builder."""
        destination = todo_path(self.home, str(record.get("session_ref", "") or ""))
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = destination.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

    def directive(self, *, tool_name="write_file", session_id=SESSION, escalation_allowed=True):
        return plan_stage_edit_directive(
            tool_name=tool_name,
            session_id=session_id,
            omh_home=str(self.home),
            hermes_home=str(self.hermes),
            escalation_allowed=escalation_allowed,
        )


class PlanStageEscalationTest(_PlanStageHomeTest):
    def test_edit_during_an_unaccepted_plan_run_escalates(self):
        _ = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE)
        self.assertEqual(
            self.directive(),
            {
                "action": "approve",
                "message": PLAN_STAGE_APPROVAL_MESSAGE,
                "rule_key": PLAN_STAGE_RULE_KEY,
            },
        )

    def test_both_file_mutating_tools_escalate_and_nothing_else_does(self):
        """The host's own set, enumerated rather than sampled.

        `write_file` and `patch` are what `FILE_MUTATING_TOOL_NAMES` holds
        (`agent/tool_result_classification.py`), and a test that checked one
        of them would not notice the gate quietly covering only that one.
        """
        _ = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE)
        for tool in sorted(FILE_MUTATING_TOOLS):
            with self.subTest(tool=tool):
                self.assertIsNotNone(self.directive(tool_name=tool))
        for tool in ("read_file", "search_files", "terminal", "execute_code", "omh_todo", "delegate_task"):
            with self.subTest(tool=tool):
                self.assertIsNone(self.directive(tool_name=tool))

    def test_the_hook_returns_the_directive_the_host_reads(self):
        """End to end through `pre_tool_call`, because the module is not the seam.

        The gate could be correct and unreachable: `pre_tool_call` returns the
        first directive it produces, and an ordering that let something below
        it answer first would leave every test above this one passing.
        """
        _ = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE)
        directive = pre_tool_call(
            tool_name="write_file",
            tool_input={"path": "src/thing.py", "content": "x = 1\n"},
            session_id=SESSION,
            omh_home=str(self.home),
            hermes_home=str(self.hermes),
        )
        self.assertEqual(directive["action"], "approve")
        self.assertEqual(directive["rule_key"], PLAN_STAGE_RULE_KEY)

    def test_the_rule_key_is_constant_across_every_call_it_could_vary_on(self):
        """One allowlist grain, so an answered prompt stays answered.

        The failure this guards is a key that moves: a person who answered
        `[a]lways` must not be asked again because the plan advanced, the
        title changed, or a different file was edited. Both file-mutating
        tools are in the table deliberately -- supplying no key does NOT
        reach the message-hash branch `request_tool_approval` documents,
        because `_resolve_block_from_details` passes `rule_key or tool_name`,
        so the fallback grain is per-tool and `patch` would re-ask what
        `write_file` had already settled.
        """
        keys = set()
        for title, items, tool in (
            ("Reviewed plan", PLANNING_ITEMS, "write_file"),
            ("A completely different plan", PLANNING_ITEMS[:2], "patch"),
            ("Reviewed plan", [{"text": "one item", "state": "active"}], "write_file"),
        ):
            _ = self.write_plan(
                plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE, items=items, title=title
            )
            directive = self.directive(tool_name=tool)
            keys.add(directive["rule_key"])
        self.assertEqual(keys, {PLAN_STAGE_RULE_KEY})

    def test_the_rule_key_is_a_key_and_not_a_tool_name(self):
        """Constancy alone would still hold if the key were empty.

        The assertion above compares the shipped key against itself, so it
        survives `PLAN_STAGE_RULE_KEY = ""` -- and empty is the one value
        with a different meaning downstream. `_resolve_block_from_details`
        passes `rule_key or tool_name`, so an empty key is never the
        message-hash grain `request_tool_approval` documents; it is the TOOL
        name, which re-asks at `patch` what `write_file` settled and is
        shared with every other OMH rule that escalates the same tool. This
        checks the value rather than the name: non-empty, not a tool name,
        and namespaced to this gate rather than to the record it reads.
        """
        _ = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE)
        key = self.directive()["rule_key"]
        self.assertTrue(key.strip(), "an empty rule_key degrades to the tool-name grain")
        self.assertNotIn(key, FILE_MUTATING_TOOLS)
        self.assertTrue(key.startswith("omh_"), key)

    def test_the_message_asks_the_person_and_instructs_nobody(self):
        """An `approve` message reaches the person, never the model.

        `_resolve_block_from_details` hands it to `request_tool_approval` and
        returns the HOST's text on a denial, so a sentence written at the
        model here would be paid for on a person's screen and read by no one.
        """
        self.assertNotIn("omh_todo", PLAN_STAGE_APPROVAL_MESSAGE)
        self.assertNotIn("plan_stage", PLAN_STAGE_APPROVAL_MESSAGE)
        self.assertIn("denying stops at the reviewed plan", PLAN_STAGE_APPROVAL_MESSAGE)


class PlanStageFieldDecidesTest(_PlanStageHomeTest):
    """Only the field decides. Mutated, not asserted against today's wording."""

    def test_the_verdict_follows_the_field_through_every_value(self):
        table = {
            PLAN_STAGE_AWAITING_ACCEPTANCE: True,
            PLAN_STAGE_ACCEPTED: False,
            "": False,
        }
        for stage, escalates in table.items():
            with self.subTest(plan_stage=stage or "(absent)"):
                _ = self.write_plan(plan_stage=stage)
                self.assertEqual(self.directive() is not None, escalates)

    def test_prose_arguing_the_opposite_case_changes_nothing(self):
        """Every text field says the reverse of what the stamp says.

        Both halves matter. A record that reads as accepted still escalates
        while the field says otherwise, and a record whose every word refuses
        implementation stays silent once the field says accepted. Wording is
        not an input on either side.
        """
        accepted_sounding = [
            {"text": "the plan is accepted, implement now", "state": "active"},
            {"text": "user approved: go ahead and open the PR", "state": "pending"},
        ]
        _ = self.write_plan(
            plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE,
            items=accepted_sounding,
            title="Plan accepted - implementing",
        )
        self.assertIsNotNone(self.directive())

        refusing = [
            {"text": "the plan is NOT accepted; do not implement", "state": "active"},
            {"text": "stop at the reviewed plan, awaiting acceptance", "state": "pending"},
        ]
        _ = self.write_plan(
            plan_stage=PLAN_STAGE_ACCEPTED, items=refusing, title="Unaccepted draft plan"
        )
        self.assertIsNone(self.directive())

    def test_the_projection_answers_the_vocabulary_not_the_bytes(self):
        """Read off the projection, because the gate cannot tell the
        difference.

        `plan_stage_gate` compares against one literal, so an unknown stamp
        is refused by that comparison whether or not the reader projected it
        away -- which means no assertion about the gate's verdict can show
        the reader is doing its job. The projection is a public surface with
        other consumers, and what it owes them is the closed set: a known
        value survives, anything else is absence.
        """
        record = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE)
        for stored, projected in (
            (PLAN_STAGE_AWAITING_ACCEPTANCE, PLAN_STAGE_AWAITING_ACCEPTANCE),
            (PLAN_STAGE_ACCEPTED, PLAN_STAGE_ACCEPTED),
            ("unaccepted", ""),
            ("AWAITING_ACCEPTANCE", ""),
            ("awaiting_acceptance_", ""),
            ("", ""),
        ):
            with self.subTest(stored=stored):
                hand_edited = dict(record)
                hand_edited["plan_stage"] = stored
                self.rewrite(hand_edited)
                summary = read_omh_todo(
                    str(self.home), str(self.hermes), session_ref=SESSION
                )
                self.assertEqual(summary.get("plan_stage"), projected)
        self.assertEqual(set(TODO_PLAN_STAGES), {PLAN_STAGE_AWAITING_ACCEPTANCE, PLAN_STAGE_ACCEPTED})

    def test_advancing_a_planning_stage_keeps_the_stamp(self):
        """Ticking a stage off is the plan moving, not the person accepting it.

        The failure this exists for is a gate that retires itself on the very
        call proving the run is still planning.
        """
        _ = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE)
        advanced = advance_todo_item(
            self.home,
            item=2,
            item_text="options",
            state="done",
            source="omh_todo",
            session_ref=SESSION,
        )
        self.assertEqual(advanced["plan_stage"], PLAN_STAGE_AWAITING_ACCEPTANCE)
        self.assertIsNotNone(self.directive())

    def test_declaring_a_fresh_list_drops_the_stamp(self):
        """The lapse-by-default half: a delivery checklist is not gated.

        Handing an accepted plan to a delivery engine writes a new list, and
        omitting the field is the default, so the common close costs nobody a
        clearing step.
        """
        _ = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE)
        _ = self.write_plan(items=[{"text": "implement the refactor", "state": "active"}])
        self.assertIsNone(self.directive())
        self.assertEqual(
            read_omh_todo(str(self.home), str(self.hermes), session_ref=SESSION)["plan_stage"], ""
        )


class PlanStageUnknownIsSilentTest(_PlanStageHomeTest):
    """Anything the records cannot settle answers None."""

    def test_no_record_at_all(self):
        self.assertIsNone(self.directive())

    def test_another_sessions_record(self):
        _ = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE, session_ref=OTHER_SESSION)
        self.assertIsNone(self.directive())
        # And the owning session still sees its own, so the negative above is
        # scoping rather than the record failing to load at all.
        self.assertIsNotNone(self.directive(session_id=OTHER_SESSION))

    def test_an_unstamped_home_wide_record_reaches_nobody(self):
        """The CLI's record has no session and no way to carry a stamp.

        `omh runtime todo set` takes no `--plan_stage`, so the home-wide file
        this exercises can only ever be absent of one. The case is here
        because the reader FALLS BACK to that file for a session with no
        record of its own, and a gate that read the fallback as its own plan
        would fire on a checklist somebody else wrote from a shell.
        """
        record = build_todo_record(
            "shell plan", PLANNING_ITEMS, source="omh runtime todo", session_ref=""
        )
        self.rewrite(record)
        self.assertIsNone(self.directive())

    def test_a_finished_plan(self):
        _ = self.write_plan(
            plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE,
            items=[{"text": "plan recorded and accepted", "state": "done"}],
        )
        self.assertIsNone(self.directive())

    def test_a_stale_record(self):
        record = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE)
        aged = dict(record)
        aged["updated_at"] = "2020-01-01T00:00:00Z"
        self.rewrite(aged)
        self.assertGreater(TODO_STALE_SECONDS, 0)
        self.assertIsNone(self.directive())

    def test_a_stamp_this_build_cannot_classify(self):
        """A hand-edited near miss is absence, and the writer refuses one.

        Silence is the right answer for a value no reader can place, and it
        is the wrong thing to leave a writer believing it got. So the reader
        projects it away and the builder raises.
        """
        record = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE)
        # The last two are the length trap: a bound applied before the
        # membership test truncates either of them to exactly the longest
        # vocabulary member, so a reader that slices first reads a stamp
        # nothing wrote and a writer that slices first stores one.
        for value in (
            "unaccepted",
            "pending",
            "AWAITING_ACCEPTANCE",
            7,
            True,
            "awaiting_acceptance_later",
            "awaiting_acceptanceX",
        ):
            with self.subTest(plan_stage=value):
                hand_edited = dict(record)
                hand_edited["plan_stage"] = value
                self.rewrite(hand_edited)
                self.assertIsNone(self.directive())
                with self.assertRaises(TodoValidationError):
                    _ = build_todo_record(
                        "plan", PLANNING_ITEMS, source="omh_todo", plan_stage=value
                    )

    def test_an_advance_over_an_unclassifiable_stamp_drops_it_instead_of_refusing(self):
        """The one place this differs from `template`, and why.

        An unknown template hides a coverage rule that would have refused, so
        that write stops. An unknown plan stage is already unguarded, so
        refusing an advance over a field the caller never sent would protect
        nothing and cost the writer its call.
        """
        record = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE)
        hand_edited = dict(record)
        hand_edited["plan_stage"] = "unaccepted"
        self.rewrite(hand_edited)
        advanced = advance_todo_item(
            self.home,
            item=2,
            item_text="options",
            state="done",
            source="omh_todo",
            session_ref=SESSION,
        )
        self.assertNotIn("plan_stage", advanced)

    def test_a_call_the_host_named_no_session_for(self):
        _ = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE)
        for session in ("", "   ", None):
            with self.subTest(session_id=session):
                self.assertIsNone(self.directive(session_id=session))

    def test_a_stamped_home_wide_record_escalates_for_nobody(self):
        """The case that makes the session test above a guard rather than a
        coincidence.

        With the plan stored under a session, an unnamed call resolves to the
        home-wide file, finds nothing, and is refused by the `established`
        test -- so the session check itself is never what answered. Here the
        stamped record IS the home-wide file, so the reader does find one for
        a call the host named no session for, and only the session check
        stands between it and an approval prompt attributed to no session.
        Nothing writes this record today; it is the shape a hand-edit or a
        future writer produces, which is why the guard is cheap to keep.
        """
        record = build_todo_record(
            "shell plan", PLANNING_ITEMS, source="omh_todo", session_ref=""
        )
        record["plan_stage"] = PLAN_STAGE_AWAITING_ACCEPTANCE
        self.rewrite(record)
        # The record really is readable and really is stamped, so the refusal
        # below cannot be the file simply failing to load.
        projection = read_omh_todo(str(self.home), str(self.hermes), session_ref="")
        self.assertEqual(projection.get("plan_stage"), PLAN_STAGE_AWAITING_ACCEPTANCE)
        self.assertEqual(projection.get("status"), "established")
        self.assertIsNone(self.directive(session_id=""))

    def test_a_read_that_raises_is_a_record_this_gate_cannot_classify(self):
        """Hermes logs a hook exception at WARNING and proceeds.

        So a handler that let one out would leave a refusal nobody sees and
        an edit nobody was asked about. `read_omh_todo` absorbs the faults it
        knows -- a missing home, unparseable JSON -- and this pins the answer
        for the ones it does not.
        """
        for error in (OSError("disk"), ValueError("shape"), TypeError("arg"), RuntimeError("x")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(
                    plan_stage_gate, "read_omh_todo", side_effect=error
                ) as reader:
                    self.assertIsNone(self.directive())
                self.assertTrue(reader.called)

    def test_a_projection_that_is_not_a_record_is_silence(self):
        """The reader returns a dict today; the gate does not assume it.

        A non-dict would make every `.get` below it raise, which is the
        previous case with a worse traceback, so it is refused by shape
        instead.
        """
        for payload in (None, [], "established", 0):
            with self.subTest(payload=repr(payload)):
                with mock.patch.object(
                    plan_stage_gate, "read_omh_todo", return_value=payload
                ):
                    self.assertIsNone(self.directive())

    def test_an_unreadable_record(self):
        _ = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE)
        destination = todo_path(self.home, SESSION)
        _ = destination.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.directive())

    def test_a_home_that_does_not_exist(self):
        directive = plan_stage_edit_directive(
            tool_name="write_file",
            session_id=SESSION,
            omh_home=str(self.home / "missing" / "deeper"),
            hermes_home=str(self.hermes / "missing"),
        )
        self.assertIsNone(directive)


class PlanStageUnattendedTest(_PlanStageHomeTest):
    """With nobody to ask, the gate is withheld rather than downgraded."""

    def test_an_unattended_lane_is_not_escalated(self):
        _ = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE)
        self.assertIsNone(self.directive(escalation_allowed=False))

    def test_the_hook_withholds_it_on_the_platforms_the_host_calls_unattended(self):
        """Through the same predicate the repeat guard's stage two uses.

        Asserted at the hook rather than by passing the flag, because the
        flag is only as true as the caller that computes it, and a `--yes`
        or webhook run stopping on a gate nobody can answer is the failure
        mode this whole class exists to prevent.
        """
        _ = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE)
        for platform in sorted(session_attendance.UNATTENDED_APPROVAL_PLATFORMS):
            with self.subTest(platform=platform):
                session_attendance.reset_session_attendance()
                session_attendance.note_session_platform(SESSION, platform)
                self.assertIsNone(
                    pre_tool_call(
                        tool_name="write_file",
                        tool_input={"path": "a.py", "content": ""},
                        session_id=SESSION,
                        omh_home=str(self.home),
                        hermes_home=str(self.hermes),
                    )
                )

    def test_a_delegated_child_is_not_escalated(self):
        _ = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE)
        nudge_budget.note_delegated_session(SESSION)
        self.assertIsNone(
            pre_tool_call(
                tool_name="write_file",
                tool_input={"path": "a.py", "content": ""},
                session_id=SESSION,
                omh_home=str(self.home),
                hermes_home=str(self.hermes),
            )
        )

    def test_an_attended_gateway_still_escalates(self):
        """The denylist's other side: an unlisted platform reads as attended."""
        _ = self.write_plan(plan_stage=PLAN_STAGE_AWAITING_ACCEPTANCE)
        session_attendance.note_session_platform(SESSION, "discord")
        directive = pre_tool_call(
            tool_name="write_file",
            tool_input={"path": "a.py", "content": ""},
            session_id=SESSION,
            omh_home=str(self.home),
            hermes_home=str(self.hermes),
        )
        self.assertEqual(directive["action"], "approve")


class PlanStageWriteSurfaceTest(_PlanStageHomeTest):
    """The tool argument that stamps the record, and what it refuses."""

    def test_the_schema_offers_the_closed_vocabulary(self):
        field = OMH_TODO_SCHEMA["parameters"]["properties"]["plan_stage"]
        self.assertEqual(field["enum"], list(TODO_PLAN_STAGES))

    def test_the_tool_writes_and_the_gate_reads_the_same_record(self):
        result = json.loads(
            omh_todo_handler(
                {
                    "action": "set",
                    "title": "Reviewed plan",
                    "items": PLANNING_ITEMS,
                    "plan_stage": PLAN_STAGE_AWAITING_ACCEPTANCE,
                },
                session_id=SESSION,
                omh_home=str(self.home),
            )
        )
        self.assertEqual(result["status"], "written")
        self.assertEqual(result["todo"]["plan_stage"], PLAN_STAGE_AWAITING_ACCEPTANCE)

    def test_the_tool_refuses_a_value_outside_the_vocabulary(self):
        result = json.loads(
            omh_todo_handler(
                {"action": "set", "items": PLANNING_ITEMS, "plan_stage": "unaccepted"},
                session_id=SESSION,
                omh_home=str(self.home),
            )
        )
        self.assertEqual(result["status"], "invalid_todo")
        self.assertIn("plan_stage", result["error"])

    def test_a_record_written_without_the_field_carries_no_key(self):
        """Additive-optional, checked as bytes rather than as a default.

        A field written as `""` would change every record this module has
        ever produced, which is what "additive-optional" is a promise not to
        do.
        """
        record = build_todo_record("plan", PLANNING_ITEMS, source="omh_todo")
        self.assertNotIn("plan_stage", record)


if __name__ == "__main__":
    unittest.main()

"""Contracts for a plan the person steered away from.

The plan line and the `pre_verify` driver are both a pure function of the plan
record, and until now the record had no way to say "the person redirected me".
So a steering detour got told to advance item 2 on every turn that touched a
file, the stall wording blamed a plan the person themselves had paused, and a
plan the steering replaced nagged until someone cleared it.

`deferred_reason` is that missing declaration, and the digest stored beside it
is what keeps it from being a second `blocked_reason`: the deferral is live
only while the digest still matches the current items, so the plan resuming
lapses it with nobody remembering to. The first test in `PlanDeferralLapseTest`
is the one that proves the design -- the record still carries its
`deferred_reason` and the digest it was written with, one item's state moves
underneath them, and the plan asks to advance again. Nothing was cleared by
hand because nothing has to be. Without that case this field would be a
hand-cleared flag with extra steps.

The tests beside it map the edges of that: resuming by simply omitting the
field (the default path, and what the tool description asks for), and
re-sending the reason with a changed list, which declares a NEW deferral for
that list rather than carrying the old one. The record is the declaration and
the writer owns it, exactly as with `blocked_reason`.

Negatives sit beside every positive, per the repo guard rule: no deferral, a
stale digest, a finished plan, a deferral under a blocked next item (the block
wins, being the stronger statement), a non-string reason, a whitespace-only
reason. The malformed-input battery at the end exists because `pre_verify`'s
host wraps the call in `except Exception` and logs at debug -- a handler that
raised would end the turn with no trace, which is indistinguishable from the
stall this whole surface exists to fix.

Nothing here reads free text to decide anything. The record declares the
deferral; the matcher deleted in #1549 does not return in this shape.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh import todo_reconciliation
from omh.plugin_bundle.omh.hooks import nudge_budget, verify_hooks
from omh.plugin_bundle.omh.runtime_reader import read_omh_todo
from omh.plugin_bundle.omh.todo_reconciliation import (
    TODO_CONTINUATION_RULE,
    TODO_DEFERRED_RULE,
    TODO_RECONCILIATION_RULE,
    TODO_UNCHANGED_RULE,
    open_todo_reminder,
    plan_continuation_reading,
    plan_deferral_reason,
)
from omh.plugin_bundle.omh.todo_store import (
    MAX_TODO_DEFERRED_REASON_CHARS,
    TODO_DEFERRED_DIGEST_CHARS,
    TODO_SCHEMA_VERSION,
    TodoValidationError,
    build_todo_record,
    todo_items_digest,
    todo_path,
    write_todo,
)
from omh.plugin_bundle.omh.tools.todo_tool import OMH_TODO_SCHEMA, omh_todo_handler

SESSION = "tui-session"
REDIRECTED = "the person asked for the release notes first"


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
        # Cleared through the module's named seam on the way in and out.
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

    def _rewrite(self, record):
        """Put a record on disk exactly as given, bypassing the builder's validation."""
        destination = todo_path(self.home, str(record.get("session_ref", "") or ""))
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = destination.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

    def _reminder(self):
        return open_todo_reminder(
            omh_home=str(self.home), hermes_home=str(self.hermes), session_ref=SESSION
        )

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


class PlanDeferralHonouredTest(_PlanHomeTest):
    def test_without_a_deferral_the_plan_still_asks_to_advance(self):
        # The negative control for every positive below: the same plan, the
        # same turn, no recorded deferral.
        _ = self._write_plan([("land the fix", "done"), ("open the PR", "active")])

        self.assertIn(TODO_CONTINUATION_RULE, self._reminder())
        self.assertIn(TODO_CONTINUATION_RULE, self._directive()["message"])

    def test_a_live_deferral_stops_the_turn_end_directive(self):
        # The nudge arrives as a synthetic user turn, so issuing one while the
        # person is being served interrupts them with the plan they just
        # stepped away from.
        _ = self._write_plan(
            [("land the fix", "done"), ("open the PR", "active")], deferred_reason=REDIRECTED
        )

        self.assertIsNone(self._directive())

    def test_a_live_deferral_replaces_the_advance_ask_on_the_plan_line(self):
        _ = self._write_plan(
            [("land the fix", "done"), ("open the PR", "active")], deferred_reason=REDIRECTED
        )

        line = self._reminder()

        self.assertIn("[OMH plan todo] 1/2 done", line)
        self.assertIn(f"deferred: {REDIRECTED}", line)
        self.assertIn(TODO_DEFERRED_RULE, line)
        # The reconciliation rule stays: a completion claim in chat while the
        # checklist shows open items is a visible contradiction whether or not
        # the person redirected the session.
        self.assertIn(TODO_RECONCILIATION_RULE, line)
        # The whole point. The line reports the position and says nothing
        # asking the session to advance the next item.
        self.assertNotIn(TODO_CONTINUATION_RULE, line)

    def test_a_deferred_plan_standing_still_is_not_reported_as_a_stall(self):
        # The second failure in the issue: "unchanged 40m ... if nothing is
        # blocking it, move it" is exactly wrong here, because the person is
        # what is blocking it. The reader's stall verdict is patched in rather
        # than aged on disk -- what is under test is which line a stalled
        # deferred plan renders, not how the reader reaches the verdict.
        todo = {
            "status": "established",
            "counts": {"total": 2, "done": 1, "active": 1, "pending": 0, "phases": 0},
            "items": [
                {"text": "land the fix", "state": "done"},
                {"text": "open the PR", "state": "active"},
            ],
            "deferred_reason": REDIRECTED,
            "updated_age_seconds": 4000.0,
            "stall": {"status": "unchanged"},
        }

        with patch.object(todo_reconciliation, "read_omh_todo", return_value=todo):
            line = self._reminder()

        self.assertIn(TODO_DEFERRED_RULE, line)
        self.assertNotIn(TODO_UNCHANGED_RULE, line)
        self.assertNotIn("unchanged", line.replace(TODO_DEFERRED_RULE, ""))

    def test_a_deferral_on_a_finished_plan_says_nothing_at_all(self):
        # A finished plan is already silent, and a deferral must not resurrect
        # it as a line of its own.
        _ = self._write_plan(
            [("land the fix", "done"), ("open the PR", "done")], deferred_reason=REDIRECTED
        )

        self.assertEqual(self._reminder(), "")
        self.assertIsNone(self._directive())

    def test_a_blocked_next_item_wins_over_a_deferral(self):
        # Two records, identical but for the deferral. The block is the
        # stronger statement about why the plan is not moving, so the deferred
        # one must render the line the undeferred one does, byte for byte:
        # reporting a redirection over a block would hide what the reader has
        # to act on.
        items = [("land the fix", "done"), ("open the PR", "active", "the owner's review")]

        _ = self._write_plan(items)
        blocked_line = self._reminder()
        _ = self._write_plan(items, deferred_reason=REDIRECTED)
        deferred_line = self._reminder()

        self.assertEqual(deferred_line, blocked_line)
        self.assertNotIn(TODO_DEFERRED_RULE, deferred_line)
        # The directive was already silent on a blocked item and stays silent.
        self.assertIsNone(self._directive())


class PlanDeferralLapseTest(_PlanHomeTest):
    def test_changing_one_item_state_resumes_a_plan_still_carrying_its_deferral(self):
        # The case that proves this is not a second `blocked_reason`, and the
        # only honest way to stage it: the record on disk still carries both
        # `deferred_reason` and the digest it was deferred with -- nothing
        # removed them, nothing was asked to -- and one item's state moved
        # underneath them. That is every writer that edits items without
        # re-declaring the deferral: the CLI, which has no such field; a hand
        # edit; a generation predating it. The reader resumes on the items
        # alone.
        record = self._write_plan(
            [("land the fix", "done"), ("open the PR", "active"), ("report", "pending")],
            deferred_reason=REDIRECTED,
        )
        self.assertIsNone(self._directive())

        record["items"][1]["state"] = "done"
        record["items"][2]["state"] = "active"
        self._rewrite(record)

        on_disk = json.loads(todo_path(self.home, SESSION).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["deferred_reason"], REDIRECTED)
        self.assertEqual(on_disk["deferred_items_digest"], record["deferred_items_digest"])
        self.assertEqual(
            read_omh_todo(self.home, self.hermes, session_ref=SESSION)["deferred_reason"], ""
        )
        self.assertIn(TODO_CONTINUATION_RULE, self._reminder())
        self.assertIn("next: report", self._directive()["message"])

    def test_the_resuming_write_simply_omits_the_field(self):
        # The path the tool description asks for, and the one a model takes by
        # default: `action=set` replaces the whole record, so not sending the
        # plan-level field is what resuming looks like. No clearing step, and
        # no digest needed for this half -- the digest is what covers the other
        # half above, where the field is still there.
        _ = self._write_plan(
            [("land the fix", "done"), ("open the PR", "active")], deferred_reason=REDIRECTED
        )

        resumed = self._write_plan([("land the fix", "done"), ("open the PR", "active")])

        self.assertNotIn("deferred_reason", resumed)
        self.assertIn(TODO_CONTINUATION_RULE, self._directive()["message"])

    def test_re_sending_the_reason_with_changed_items_declares_a_new_deferral(self):
        # The consequence of computing the digest on every write, pinned so it
        # stays a decision rather than an accident. A writer that deliberately
        # sends the reason again alongside a changed list is declaring a
        # deferral for THAT list -- the record is the declaration and the
        # writer owns it, exactly as an item's `blocked_reason` is re-declared
        # on every write. The guard against doing it absent-mindedly is the
        # tool description, which says to omit the field.
        _ = self._write_plan(
            [("land the fix", "done"), ("open the PR", "active")], deferred_reason=REDIRECTED
        )

        redeclared = self._write_plan(
            [("land the fix", "done"), ("open the PR", "done"), ("report", "active")],
            deferred_reason=REDIRECTED,
        )

        self.assertEqual(
            redeclared["deferred_items_digest"], todo_items_digest(redeclared["items"])
        )
        self.assertIsNone(self._directive())

    def test_a_digest_recorded_against_other_items_is_already_lapsed(self):
        # The same lapse seen from the record's side, and the widest form of
        # it: a deferral whose digest does not describe the items it sits
        # beside is over, however it got that way.
        record = self._write_plan(
            [("land the fix", "done"), ("open the PR", "active")], deferred_reason=REDIRECTED
        )
        record["deferred_items_digest"] = todo_items_digest(
            [{"text": "something else entirely", "state": "active"}]
        )
        self._rewrite(record)

        self.assertEqual(
            read_omh_todo(self.home, self.hermes, session_ref=SESSION)["deferred_reason"], ""
        )
        self.assertIn(TODO_CONTINUATION_RULE, self._reminder())
        self.assertIn(TODO_CONTINUATION_RULE, self._directive()["message"])

    def test_every_item_field_the_record_carries_moves_the_digest(self):
        # What "the plan moved" means, enumerated. Recording a `blocked_reason`
        # counts: writing down that an item is stuck is a plan advancing, not a
        # plan standing still, and a deferral that outlived that edit would be
        # describing a plan that no longer exists.
        base = [{"text": "open the PR", "state": "active", "phase": "II. Delivery", "depth": 1}]
        edits = (
            {"text": "open the PR for review"},
            {"state": "done"},
            {"phase": "III. Evidence"},
            {"depth": 2},
            {"blocked_reason": "the owner's review"},
        )

        for edit in edits:
            with self.subTest(edit=sorted(edit)[0]):
                moved = [{**base[0], **edit}]

                self.assertNotEqual(todo_items_digest(base), todo_items_digest(moved))

    def test_re_declaring_the_same_items_keeps_the_deferral(self):
        # The reason the digest is over the items rather than over the write
        # stamp: a record rewritten with an identical list is not the plan
        # moving, so a stamp-based lapse would end a deferral that is still
        # true.
        record = self._write_plan(
            [("land the fix", "done"), ("open the PR", "active")], deferred_reason=REDIRECTED
        )
        again = self._write_plan(
            [("land the fix", "done"), ("open the PR", "active")], deferred_reason=REDIRECTED
        )

        self.assertEqual(again["deferred_items_digest"], record["deferred_items_digest"])
        self.assertIsNone(self._directive())


class PlanDeferralRecordTest(_PlanHomeTest):
    def test_a_record_without_a_deferral_is_byte_identical_to_today(self):
        # Additive-optional. Neither key is written unless a reason was
        # declared, so a CLI write and a reader that predates the field are
        # both unaffected.
        record = build_todo_record(
            "plan", [{"text": "open the PR", "state": "active"}], source="test", session_ref=SESSION
        )

        self.assertEqual(
            set(record),
            {"schema_version", "title", "source", "updated_at", "items", "claim_boundary", "session_ref"},
        )
        self.assertNotIn("deferred", json.dumps(record, sort_keys=True))

    def test_the_reason_and_its_digest_are_written_together(self):
        # A reason without a digest would be a deferral nothing can lapse --
        # the hand-cleared flag this field exists instead of.
        record = build_todo_record(
            "plan",
            [{"text": "open the PR", "state": "active"}],
            source="test",
            deferred_reason=REDIRECTED,
        )

        self.assertEqual(record["deferred_reason"], REDIRECTED)
        self.assertEqual(record["deferred_items_digest"], todo_items_digest(record["items"]))
        self.assertEqual(len(record["deferred_items_digest"]), TODO_DEFERRED_DIGEST_CHARS)
        self.assertEqual(record["schema_version"], TODO_SCHEMA_VERSION)

    def test_a_whitespace_only_reason_is_absence_not_a_deferral(self):
        # Whitespace is not a declaration. It strips to empty, the record is
        # written without either key, and the plan keeps asking to advance --
        # the opposite of the blocked_reason sentinel rule, where a string
        # saying nothing is wrong still counts, because there the writer chose
        # to send characters and here they sent none.
        for blank in (" ", "\t", "\n\n", "     "):
            with self.subTest(blank=repr(blank)):
                record = build_todo_record(
                    "plan",
                    [{"text": "open the PR", "state": "active"}],
                    source="test",
                    session_ref=SESSION,
                    deferred_reason=blank,
                )
                _ = write_todo(self.home, record)

                self.assertNotIn("deferred_reason", record)
                self.assertNotIn("deferred_items_digest", record)
                self.assertIn(TODO_CONTINUATION_RULE, self._directive()["message"])

    def test_a_non_string_reason_is_refused_on_write(self):
        # `strip_control_characters` would turn 7 into the truthy string "7".
        # A number is not a declaration in any language, so the writer says so
        # instead of storing one.
        for reason in (7, 0.5, {"a": 1}, ["x"], True):
            with self.subTest(reason=reason):
                with self.assertRaises(TodoValidationError):
                    _ = build_todo_record(
                        "plan",
                        [{"text": "open the PR", "state": "active"}],
                        source="test",
                        deferred_reason=reason,
                    )

    def test_a_reason_past_its_cap_is_refused_rather_than_cut(self):
        # Matching `blocked_reason`: a reason silently cut at its cap can read
        # as something the writer did not say, and the tool surfaces the error
        # so the writer can shorten it.
        with self.assertRaises(TodoValidationError):
            _ = build_todo_record(
                "plan",
                [{"text": "open the PR", "state": "active"}],
                source="test",
                deferred_reason="x" * (MAX_TODO_DEFERRED_REASON_CHARS + 1),
            )

    def test_the_tool_writes_a_deferral_and_says_it_clears_itself(self):
        schema = OMH_TODO_SCHEMA["parameters"]["properties"]["deferred_reason"]

        self.assertEqual(schema["type"], "string")
        self.assertIn("redirected", schema["description"])
        self.assertIn("CLEARS ITSELF", schema["description"])
        # So a model does not reach for blocked_reason instead.
        self.assertIn("blocked_reason", schema["description"])

        with patch(
            "omh.plugin_bundle.omh.tools.todo_tool.default_omh_home", return_value=self.home
        ), patch(
            "omh.plugin_bundle.omh.tools.todo_tool.read_omh_todo", return_value={}
        ):
            result = json.loads(
                omh_todo_handler(
                    {
                        "action": "set",
                        "title": "plan",
                        "items": [{"text": "open the PR", "state": "active"}],
                        "deferred_reason": REDIRECTED,
                    }
                )
            )

        self.assertEqual(result["status"], "written")
        stored = json.loads(todo_path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(stored["deferred_reason"], REDIRECTED)
        self.assertEqual(stored["deferred_items_digest"], todo_items_digest(stored["items"]))


class PlanDeferralMalformedInputTest(_PlanHomeTest):
    """Nothing below may raise: the host logs a raising hook at debug and ends the turn."""

    def test_a_non_string_reason_on_disk_is_read_as_absent(self):
        # A record written by hand, or by a writer that skipped the builder.
        # Each value stringifies to something truthy, and reading corruption as
        # a declaration would be the silent stop this surface exists to end.
        for reason in (7, {"a": 1}, [1], True, None, 0, []):
            with self.subTest(reason=reason):
                record = self._write_plan([("land the fix", "done"), ("open the PR", "active")])
                record["deferred_reason"] = reason
                record["deferred_items_digest"] = todo_items_digest(record["items"])
                self._rewrite(record)

                self.assertIn(TODO_CONTINUATION_RULE, self._directive()["message"])

    def test_a_digest_of_the_wrong_type_lapses_rather_than_matching(self):
        for digest in (7, None, ["abc"], {"a": 1}, True):
            with self.subTest(digest=digest):
                record = self._write_plan(
                    [("land the fix", "done"), ("open the PR", "active")],
                    deferred_reason=REDIRECTED,
                )
                record["deferred_items_digest"] = digest
                self._rewrite(record)

                self.assertIn(TODO_CONTINUATION_RULE, self._directive()["message"])

    def test_a_reason_with_no_digest_beside_it_lapses(self):
        record = self._write_plan([("land the fix", "done"), ("open the PR", "active")])
        record["deferred_reason"] = REDIRECTED
        self._rewrite(record)

        self.assertIn(TODO_CONTINUATION_RULE, self._directive()["message"])

    def test_the_digest_never_raises_on_a_malformed_item_list(self):
        # A lapsed deferral is the safe direction for every one of these: the
        # plan keeps going, and nothing stops silently.
        for items in (None, "items", 7, [], [None, 7, "x"], [{"text": object()}], {"a": 1}):
            with self.subTest(items=repr(items)[:40]):
                digest = todo_items_digest(items)

                self.assertIsInstance(digest, str)

    def test_the_reason_reader_never_raises_on_a_malformed_projection(self):
        for todo in (None, "todo", 7, [], {"deferred_reason": 7}, {"deferred_reason": None}, {}):
            with self.subTest(todo=repr(todo)[:40]):
                self.assertEqual(plan_deferral_reason(todo), "")

    def test_an_unreadable_home_returns_no_directive_rather_than_raising(self):
        # A file where the home should be, and a home that does not exist.
        broken = Path(self._tmp.name) / "not-a-home"
        _ = broken.write_text("x", encoding="utf-8")

        for home in (broken, Path(self._tmp.name) / "absent"):
            with self.subTest(home=home.name):
                message, stamp = plan_continuation_reading(
                    omh_home=str(home), hermes_home=str(self.hermes), session_ref=SESSION
                )

                self.assertEqual(message, "")
                self.assertIsInstance(stamp, str)


if __name__ == "__main__":
    unittest.main()

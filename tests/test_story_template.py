"""Contracts for the named phase template a plan record can be stamped with.

The gap these close. A person who runs every change through the same ten
stages -- story, implement, review, QA, fix, manual test guide, deep guide,
ELI5, quiz, close -- got those stages only when the model happened to invent
them, and a stage nobody thought of that day cost nothing to leave out. So
the shape lived in the person's head and the plan record agreed with it by
luck.

What is pinned here is that the shape is a RECORD rather than a suggestion.
The ten phases come out of `todo_templates`, the stamp is a field on the
record, and every later write to a stamped record is held to the same
coverage -- so a phase cannot quietly leave the plan. Dropping one is refused
by name; the only way to not do a phase is to carry it as `done` with a
`blocked_reason`, which is the field the HUD already renders.

And that it attaches to nothing on its own. Nothing here reads a person's
message: the stamp exists because a writer passed `template` on the call, and
a plan declared without one is byte-for-byte the plan this store built before
the field existed. That is the repository's standing rule about deciding
anything from free text (#1549), applied to the one surface that would have
been most tempting to give a phrase list.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.runtime_reader import read_omh_hud
from omh.plugin_bundle.omh.todo_store import (
    MAX_TODO_ITEMS,
    TodoValidationError,
    advance_todo_item,
    build_todo_record,
    todo_items_digest,
    validate_todo_items,
    write_todo,
)
from omh.plugin_bundle.omh.todo_templates import (
    CODE_STORY_TEMPLATE,
    template_coverage_error,
    template_items,
    template_phase_labels,
)
from omh.plugin_bundle.omh.tools.todo_tool import OMH_TODO_SCHEMA, omh_todo_handler

SESSION = "tui-session"

STORY_PHASES = (
    "I. Story",
    "II. Implement",
    "III. Review",
    "IV. QA",
    "V. Fix",
    "VI. Manual test guide",
    "VII. Deep guide",
    "VIII. ELI5",
    "IX. Quiz",
    "X. Close",
)


class _TodoHomeTest(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "omh"
        self.hermes = self.root / "hermes"
        self.home.mkdir()
        self.hermes.mkdir()

    def project(self, record: dict) -> dict:
        write_todo(self.home, record)
        return read_omh_hud(
            self.home, self.hermes, session_ref=str(record.get("session_ref", ""))
        )["todo"]


class StampShapeTest(_TodoHomeTest):
    """What a stamp with no items of its own produces."""

    def test_the_template_declares_the_ten_phases_in_delivery_order(self):
        record = build_todo_record(
            "story", None, source="omh_todo", template=CODE_STORY_TEMPLATE
        )

        self.assertEqual(
            tuple(item["phase"] for item in record["items"]), STORY_PHASES
        )
        self.assertEqual(
            [item["state"] for item in record["items"]], ["pending"] * 10
        )
        # One item per phase, and the whole plan fits inside the store's own
        # item cap with room for subtasks -- the cap is what would otherwise
        # make the shape undeclarable.
        self.assertEqual(len(record["items"]), 10)
        self.assertLessEqual(len(record["items"]), MAX_TODO_ITEMS)

    def test_the_stamp_is_a_field_on_the_record(self):
        # The point of the whole change: a downstream reader answers "is this
        # a story plan" by reading a key, never by matching the phase labels
        # back out of the item text.
        record = build_todo_record(
            "story", None, source="omh_todo", template=CODE_STORY_TEMPLATE
        )

        self.assertEqual(record["template"], CODE_STORY_TEMPLATE)

    def test_an_empty_item_list_is_filled_rather_than_refused(self):
        # `validate_todo_items` refuses an empty list, and a writer that sends
        # `items: []` alongside a template means the same thing as one that
        # omits the field.
        record = build_todo_record(
            "story", [], source="omh_todo", template=CODE_STORY_TEMPLATE
        )

        self.assertEqual(len(record["items"]), 10)

    def test_the_projection_carries_the_stamp(self):
        # Without this the writer cannot see the stamp it is holding, and
        # `set` replaces the whole record -- so a re-declaration would drop
        # the template because nothing showed it was there.
        todo = self.project(
            build_todo_record(
                "story", None, source="omh_todo", session_ref=SESSION,
                template=CODE_STORY_TEMPLATE,
            )
        )

        self.assertEqual(todo["template"], CODE_STORY_TEMPLATE)
        self.assertEqual(todo["counts"]["phases"], 10)
        self.assertEqual(todo["display_phase"], "I. Story")


class UnstampedPlansAreUnchangedTest(_TodoHomeTest):
    """The template must not be mandatory for anyone who does not ask for it."""

    ITEMS = [
        {"text": "land the fix", "state": "active"},
        {"text": "open the PR", "state": "pending"},
    ]

    def test_a_plain_write_carries_no_template_key(self):
        record = build_todo_record("plan", self.ITEMS, source="omh_todo")

        self.assertNotIn("template", record)

    def test_a_plain_write_is_byte_identical_to_the_record_without_the_field(self):
        # The additive-optional contract `session_ref` and `deferred_reason`
        # established: a caller that does not use the field gets the exact
        # bytes this module wrote before the field existed. Spelled as the
        # full key set plus the serialized form, because a new key appearing
        # anywhere in the record is what would break it.
        record = build_todo_record(
            "plan", self.ITEMS, source="omh_todo", session_ref=SESSION
        )

        self.assertEqual(
            sorted(record),
            [
                "claim_boundary",
                "items",
                "schema_version",
                "session_ref",
                "source",
                "title",
                "updated_at",
            ],
        )
        # And the stamp is the ONE key that separates the two records, so
        # nothing else this change added rides along into a plain write.
        stamped = build_todo_record(
            "plan", None, source="omh_todo", session_ref=SESSION,
            template=CODE_STORY_TEMPLATE,
        )
        self.assertEqual(set(stamped) - set(record), {"template"})
        del stamped["template"]
        stamped["items"] = record["items"]
        stamped["updated_at"] = record["updated_at"]
        self.assertEqual(
            json.dumps(record, sort_keys=True), json.dumps(stamped, sort_keys=True)
        )

    def test_an_unphased_plan_still_validates_without_a_template(self):
        # The coverage rule belongs to the stamp and to nothing else: a plan
        # with no phases at all is exactly as writable as it was.
        record = build_todo_record("plan", [{"text": "one thing"}], source="omh_todo")

        self.assertEqual(record["items"], [{"text": "one thing", "state": "pending"}])

    def test_the_template_field_does_not_enter_the_deferral_digest(self):
        # The digest covers ITEM fields, and the stamp is record-level. If it
        # leaked in, stamping a plan would lapse a live deferral for no
        # reason the person did anything about.
        items = build_todo_record(
            "story", None, source="omh_todo", template=CODE_STORY_TEMPLATE
        )["items"]
        stamped = build_todo_record(
            "story", items, source="omh_todo", deferred_reason="owner asked for the hotfix",
            template=CODE_STORY_TEMPLATE,
        )

        self.assertEqual(
            stamped["deferred_items_digest"], todo_items_digest(items)
        )


class SkippingCostsAReasonTest(_TodoHomeTest):
    """A phase may be irrelevant. Dropping it is refused; carrying it is not."""

    def test_a_dropped_phase_is_refused_and_named(self):
        items = [
            item
            for item in template_items(CODE_STORY_TEMPLATE)
            if item["phase"] != "VI. Manual test guide"
        ]

        with self.assertRaises(TodoValidationError) as raised:
            build_todo_record(
                "story", items, source="omh_todo", template=CODE_STORY_TEMPLATE
            )

        message = str(raised.exception)
        self.assertIn("'VI. Manual test guide'", message)
        # And it says what to do instead, because the writer reading the
        # refusal is a model and a refusal that only says no produces a
        # second guess rather than a recorded reason.
        self.assertIn("blocked_reason", message)

    def test_a_skipped_phase_with_a_reason_is_accepted(self):
        items = template_items(CODE_STORY_TEMPLATE)
        items[5] = {
            **items[5],
            "state": "done",
            "blocked_reason": "no UI in this change",
        }

        record = build_todo_record(
            "story", items, source="omh_todo", template=CODE_STORY_TEMPLATE
        )

        self.assertEqual(record["items"][5]["state"], "done")
        self.assertEqual(record["items"][5]["blocked_reason"], "no UI in this change")

    def test_a_skipped_phase_renders_its_reason(self):
        items = template_items(CODE_STORY_TEMPLATE)
        items[5] = {
            **items[5],
            "state": "done",
            "blocked_reason": "no UI in this change",
        }
        write_todo(
            self.home,
            build_todo_record(
                "story", items, source="omh_todo", session_ref=SESSION,
                template=CODE_STORY_TEMPLATE,
            ),
        )

        lines = read_omh_hud(
            self.home, self.hermes, preset="full", session_ref=SESSION
        )["display"]["todo_lines"]

        # "skipped", not "waiting": nobody is waiting on a phase that is
        # closed, and this is now the sanctioned way to drop one, so the row
        # would otherwise read as a bug on the main path.
        self.assertIn(
            "  [✓] Write the manual test guide a person can follow "
            "(skipped: no UI in this change)",
            lines,
        )

    def test_a_skipped_phase_does_not_halt_the_plan(self):
        # Why `done` and not `pending` with a reason: the plan's own stop
        # criterion is "the next open item carries a blocked_reason", so a
        # pending skip would stop the run AT the skipped phase and leave every
        # later phase unreached. A done item is walked past.
        items = [
            {**item, "state": "done"} for item in template_items(CODE_STORY_TEMPLATE)
        ]
        items[5] = {**items[5], "blocked_reason": "no UI in this change"}

        todo = self.project(
            build_todo_record(
                "story", items, source="omh_todo", session_ref=SESSION,
                template=CODE_STORY_TEMPLATE,
            )
        )

        self.assertEqual(todo["status"], "all_done")
        self.assertEqual(todo["counts"]["done"], 10)
        self.assertEqual(todo["counts"]["skipped"], 1)

    def test_the_skip_is_reachable_through_advance(self):
        write_todo(
            self.home,
            build_todo_record(
                "story", None, source="omh_todo", session_ref=SESSION,
                template=CODE_STORY_TEMPLATE,
            ),
        )

        advanced = advance_todo_item(
            self.home,
            item=6,
            item_text="Write the manual test guide",
            state="done",
            source="omh_todo",
            session_ref=SESSION,
            blocked_reason="no UI in this change",
        )

        self.assertEqual(advanced["items"][5]["state"], "done")
        self.assertEqual(advanced["items"][5]["blocked_reason"], "no UI in this change")


class SkippedPhasesStayVisibleTest(_TodoHomeTest):
    """What a finished story says about the phases it did not work.

    The failure this closes: a story that skipped four of ten finished as
    `Todo · story ✓ 10/10`, with the item rows collapsed and every recorded
    reason gone — at exactly the moment a person reads the plan to see what
    the run did. And the number itself asserted ten phases of work for a
    six-phase story, which is a claim the record does not carry.

    The count is DERIVED, not declared: a skipped phase is already
    distinguishable as `done` carrying a `blocked_reason`, so nothing new is
    written to disk and no item state was added.
    """

    def _story(self, *, skips: int, finished: bool = True) -> dict:
        items = [dict(item) for item in template_items(CODE_STORY_TEMPLATE)]
        for index, item in enumerate(items):
            item["state"] = "done" if finished or index < 5 else "pending"
            if index < skips:
                item["state"] = "done"
                item["blocked_reason"] = f"phase {index + 1} does not apply"
        return self.project(
            build_todo_record(
                "story", items, source="omh_todo", session_ref=SESSION,
                template=CODE_STORY_TEMPLATE,
            )
        )

    def test_the_skipped_count_is_derived_from_the_two_fields_an_item_already_has(self):
        items = [dict(item) for item in template_items(CODE_STORY_TEMPLATE)]
        items[0] = {**items[0], "state": "done", "blocked_reason": "not needed"}
        items[1] = {**items[1], "state": "done"}
        items[2] = {**items[2], "state": "active", "blocked_reason": "owner approval"}

        counts = self.project(
            build_todo_record(
                "story", items, source="omh_todo", session_ref=SESSION,
                template=CODE_STORY_TEMPLATE,
            )
        )["counts"]

        # Only the closed item carrying a reason. A done item with no reason
        # is work that happened; an OPEN item with one is waiting, not
        # skipped, and the plan's stop criterion still reads it.
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(counts["done"], 2)
        self.assertEqual(counts["active"], 1)

    def test_a_running_story_names_its_skips_while_the_rows_are_still_there(self):
        # The clause appears with the FIRST skip, not only at the end: the
        # finished line's numerator drops (9/10 -> 6/10), and a person who
        # has been reading `(3 skipped)` all along can reconcile that step
        # instead of watching a number go backwards. It also outlasts the
        # panel's eight-row window, which folds the skipped rows away long
        # before the plan finishes.
        todo = self._story(skips=3, finished=False)
        lines = read_omh_hud(
            self.home, self.hermes, session_ref=SESSION
        )["display"]["todo_lines"]

        self.assertEqual(todo["status"], "established")
        # The numerator is still `done`, matching the ticks a reader can
        # count on the rows below it; only the finished line subtracts.
        self.assertEqual(todo["counts"]["done"], 5)
        self.assertEqual(lines[0], "Todo · story   5/10 (3 skipped)")

    def test_a_running_story_with_no_skips_renders_exactly_what_it_did_before(self):
        self._story(skips=0, finished=False)
        lines = read_omh_hud(
            self.home, self.hermes, session_ref=SESSION
        )["display"]["todo_lines"]

        self.assertEqual(lines[0], "Todo · story   5/10")

    def test_a_finished_story_names_the_phases_it_skipped(self):
        todo = self._story(skips=4)
        lines = read_omh_hud(
            self.home, self.hermes, session_ref=SESSION
        )["display"]["todo_lines"]

        self.assertEqual(todo["counts"]["skipped"], 4)
        self.assertEqual(lines, ["Todo · story ✓ 6/10 (4 skipped)"])

    def test_a_finished_story_with_no_skips_renders_exactly_what_it_did_before(self):
        todo = self._story(skips=0)
        lines = read_omh_hud(
            self.home, self.hermes, session_ref=SESSION
        )["display"]["todo_lines"]

        self.assertEqual(todo["counts"]["skipped"], 0)
        self.assertEqual(lines, ["Todo · story ✓ 10/10"])

    def test_an_ordinary_plan_carries_the_count_as_zero(self):
        # Additive on a derived projection: a plan that has never heard of
        # the template still answers the question, and answers it with 0.
        todo = self.project(
            build_todo_record(
                "plan", [{"text": "one thing", "state": "done"}], source="omh_todo",
                session_ref=SESSION,
            )
        )

        self.assertEqual(todo["counts"]["skipped"], 0)
        self.assertEqual(
            read_omh_hud(self.home, self.hermes, session_ref=SESSION)["display"][
                "todo_lines"
            ],
            ["Todo · plan ✓ 1/1"],
        )

    def test_an_absent_plan_counts_no_skips(self):
        self.assertEqual(
            read_omh_hud(self.home, self.hermes)["todo"]["counts"]["skipped"], 0
        )


class CoverageIsRecheckedOnEveryWriteTest(_TodoHomeTest):
    """The stamp is enforcement, not decoration."""

    def test_an_unknown_phase_is_refused(self):
        items = template_items(CODE_STORY_TEMPLATE)
        items[3] = {**items[3], "phase": "IV. Testing"}

        with self.assertRaises(TodoValidationError) as raised:
            build_todo_record(
                "story", items, source="omh_todo", template=CODE_STORY_TEMPLATE
            )

        self.assertIn("'IV. Testing'", str(raised.exception))

    def test_phases_declared_out_of_order_are_refused(self):
        items = template_items(CODE_STORY_TEMPLATE)
        items[1], items[2] = items[2], items[1]

        with self.assertRaises(TodoValidationError) as raised:
            build_todo_record(
                "story", items, source="omh_todo", template=CODE_STORY_TEMPLATE
            )

        self.assertIn("out of order", str(raised.exception))

    def test_an_item_outside_every_phase_is_refused(self):
        items = template_items(CODE_STORY_TEMPLATE)
        items.append({"text": "something else entirely", "state": "pending"})

        with self.assertRaises(TodoValidationError) as raised:
            build_todo_record(
                "story", items, source="omh_todo", template=CODE_STORY_TEMPLATE
            )

        self.assertIn("item 11", str(raised.exception))

    def test_a_subtask_continues_its_parents_phase(self):
        # The same inheritance the HUD applies, so the coverage check and the
        # panel agree about which section an item is in.
        items = template_items(CODE_STORY_TEMPLATE)
        items.insert(2, {"text": "write the failing test", "state": "pending", "depth": 1})

        record = build_todo_record(
            "story", items, source="omh_todo", template=CODE_STORY_TEMPLATE
        )

        self.assertEqual(len(record["items"]), 11)
        self.assertNotIn("phase", record["items"][2])

    def test_an_advance_keeps_the_stamp(self):
        write_todo(
            self.home,
            build_todo_record(
                "story", None, source="omh_todo", session_ref=SESSION,
                template=CODE_STORY_TEMPLATE,
            ),
        )

        advanced = advance_todo_item(
            self.home,
            item=1,
            item_text="Write the story",
            state="active",
            source="omh_todo",
            session_ref=SESSION,
        )

        self.assertEqual(advanced["template"], CODE_STORY_TEMPLATE)

    def test_a_plan_that_returns_to_an_earlier_phase_is_accepted(self):
        # Order is over FIRST appearances, not positions. A run that comes
        # back to III for one more task after X has opened is a plan doing
        # its job, and refusing it would make the stamp fight the work.
        items = template_items(CODE_STORY_TEMPLATE)
        items.append(
            {"text": "re-review the fix", "state": "pending", "phase": "III. Review"}
        )

        record = build_todo_record(
            "story", items, source="omh_todo", template=CODE_STORY_TEMPLATE
        )

        self.assertEqual(len(record["items"]), 11)
        self.assertEqual(record["items"][10]["phase"], "III. Review")

    def test_an_advance_on_a_record_naming_an_unknown_template_names_the_remedy(self):
        # Reachable from a hand edit, or from a bundle rolled back under a
        # record a newer generation stamped. The builder's own message tells
        # the writer to send a different `template`, and action=advance has
        # no such argument -- so the refusal is relabelled with the one thing
        # that does clear it.
        record = build_todo_record(
            "story", None, source="omh_todo", session_ref=SESSION,
            template=CODE_STORY_TEMPLATE,
        )
        record["template"] = "spec-story"
        write_todo(self.home, record)

        with self.assertRaises(TodoValidationError) as raised:
            advance_todo_item(
                self.home,
                item=1,
                item_text="Write the story",
                state="active",
                source="omh_todo",
                session_ref=SESSION,
            )

        message = str(raised.exception)
        self.assertIn("'spec-story'", message)
        self.assertIn("action=set", message)
        self.assertNotIn("todo template must be one of", message)

    def test_an_advance_on_a_record_whose_template_is_not_even_a_name_refuses(self):
        # A hand edit can put anything there, including a value that is not
        # hashable. The relabel must not raise while deciding whether to
        # relabel.
        record = build_todo_record(
            "story", None, source="omh_todo", session_ref=SESSION,
            template=CODE_STORY_TEMPLATE,
        )
        record["template"] = {"code": "story"}
        write_todo(self.home, record)

        with self.assertRaises(TodoValidationError) as raised:
            advance_todo_item(
                self.home,
                item=1,
                item_text="Write the story",
                state="active",
                source="omh_todo",
                session_ref=SESSION,
            )

        self.assertIn("action=set", str(raised.exception))

    def test_an_advance_on_a_record_that_lost_a_phase_refuses_by_name(self):
        # A hand-edited record, the only way to reach this. It refuses rather
        # than silently re-writing a record whose stamp no longer describes
        # it; `set` is how such a plan is re-declared.
        items = [
            item
            for item in template_items(CODE_STORY_TEMPLATE)
            if item["phase"] != "IX. Quiz"
        ]
        record = build_todo_record(
            "story", items, source="omh_todo", session_ref=SESSION
        )
        record["template"] = CODE_STORY_TEMPLATE
        write_todo(self.home, record)

        with self.assertRaises(TodoValidationError) as raised:
            advance_todo_item(
                self.home,
                item=1,
                item_text="Write the story",
                state="active",
                source="omh_todo",
                session_ref=SESSION,
            )

        # A coverage refusal already names its phase and its remedy, so it
        # passes through unrelabelled -- only the unknown-name case is
        # rewritten.
        self.assertIn("'IX. Quiz'", str(raised.exception))
        self.assertIn("blocked_reason", str(raised.exception))


class ItemCapTest(_TodoHomeTest):
    """What a stamped plan is told when it runs out of item budget.

    The template declares ten of the twenty items, so a story plan has ten
    slots for work of its own. Measured, the twenty-first item is REFUSED and
    nothing partial lands -- which is the opposite of what a person assumes a
    cap does, and the reason the refusal says so.

    The generic sentence is unchanged for every plan that named no template.
    That half is pinned first, because the last refusal this branch relabelled
    keyed on a condition that was also true of the empty string and caught
    every unstamped write with it.
    """

    def _with_subtasks(self, count: int) -> list[dict]:
        items = [dict(item) for item in template_items(CODE_STORY_TEMPLATE)]
        for index in range(count):
            items.insert(
                2 + index,
                {"text": f"subtask {index + 1}", "state": "pending", "depth": 1},
            )
        return items

    def test_the_template_leaves_exactly_half_the_cap_for_work_of_your_own(self):
        self.assertEqual(len(template_items(CODE_STORY_TEMPLATE)), 10)
        self.assertEqual(MAX_TODO_ITEMS, 20)

        record = build_todo_record(
            "story", self._with_subtasks(10), source="omh_todo",
            template=CODE_STORY_TEMPLATE,
        )

        self.assertEqual(len(record["items"]), MAX_TODO_ITEMS)

    def test_the_first_item_past_the_cap_is_refused_with_the_template_arithmetic(self):
        with self.assertRaises(TodoValidationError) as raised:
            build_todo_record(
                "story", self._with_subtasks(11), source="omh_todo",
                template=CODE_STORY_TEMPLATE,
            )

        self.assertEqual(
            str(raised.exception),
            "todo items are capped at 20; template 'code-story' declares 10 of them, "
            "leaving 10 for items of your own, and this plan has 21. Nothing was "
            "written.",
        )

    def test_nothing_partial_lands_when_the_cap_refuses(self):
        # A cap that truncates and a cap that refuses ask for opposite next
        # moves, and a person assumes the first. The record is untouched and
        # no file appears, which is what the message claims.
        with self.assertRaises(TodoValidationError):
            build_todo_record(
                "story", self._with_subtasks(11), source="omh_todo",
                session_ref=SESSION, template=CODE_STORY_TEMPLATE,
            )

        todo = read_omh_hud(self.home, self.hermes, session_ref=SESSION)["todo"]
        self.assertEqual(todo["status"], "absent")
        self.assertEqual(list((self.home / "runtime").glob("*")), [])

    def test_a_plan_that_named_no_template_reads_the_sentence_it_always_read(self):
        # Byte-identical, through both doors: the shared validator, and the
        # builder that now has a template-aware branch in front of it.
        overflowing = [{"text": f"task {n}"} for n in range(MAX_TODO_ITEMS + 1)]

        with self.assertRaises(TodoValidationError) as direct:
            validate_todo_items(overflowing)
        with self.assertRaises(TodoValidationError) as built:
            build_todo_record("plan", overflowing, source="omh_todo")

        self.assertEqual(str(direct.exception), "todo items are capped at 20")
        self.assertEqual(str(built.exception), "todo items are capped at 20")

    def test_the_refusal_reaches_the_model_as_an_invalid_todo(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"OMH_HOME": str(self.home)}):
            result = json.loads(
                omh_todo_handler(
                    {
                        "action": "set",
                        "title": "story",
                        "template": CODE_STORY_TEMPLATE,
                        "items": self._with_subtasks(11),
                    },
                    session_id=SESSION,
                )
            )

        self.assertEqual(result["status"], "invalid_todo")
        self.assertIn("template 'code-story' declares 10 of them", result["error"])
        self.assertIn("Nothing was written", result["error"])
        self.assertEqual(result["todo"]["status"], "absent")


class UnknownTemplateTest(_TodoHomeTest):
    def test_an_unknown_template_name_is_refused_and_names_the_known_ones(self):
        with self.assertRaises(TodoValidationError) as raised:
            build_todo_record("story", None, source="omh_todo", template="ten-phases")

        self.assertIn(repr(CODE_STORY_TEMPLATE), str(raised.exception))

    def test_a_non_string_template_is_refused_rather_than_coerced(self):
        with self.assertRaises(TodoValidationError):
            build_todo_record("story", None, source="omh_todo", template=7)

    def test_the_coverage_helper_reports_rather_than_raises(self):
        # One caller raises the store's own error type, so the helper hands
        # back a sentence and the store decides what kind of failure it is.
        self.assertEqual(
            template_coverage_error(
                CODE_STORY_TEMPLATE, template_items(CODE_STORY_TEMPLATE)
            ),
            "",
        )

    def test_the_labels_are_the_declared_ten(self):
        self.assertEqual(template_phase_labels(CODE_STORY_TEMPLATE), STORY_PHASES)


class OlderReadersTest(_TodoHomeTest):
    """A reader that predates the field still reads every field it knew."""

    def test_the_stamp_changes_no_other_projected_value(self):
        items = template_items(CODE_STORY_TEMPLATE)
        items[0] = {**items[0], "state": "active"}
        stamped = build_todo_record(
            "story", items, source="omh_todo", session_ref=SESSION,
            template=CODE_STORY_TEMPLATE,
        )
        unstamped = dict(stamped)
        del unstamped["template"]

        with_stamp = self.project(stamped)
        without_stamp = self.project(unstamped)

        # Everything a reader written before the field could ask for answers
        # identically; only the new key differs, and an older reader does not
        # look at it.
        for key in without_stamp:
            if key in {"template", "updated_age_seconds", "stall"}:
                continue
            self.assertEqual(with_stamp[key], without_stamp[key], key)
        self.assertEqual(with_stamp["template"], CODE_STORY_TEMPLATE)
        self.assertEqual(without_stamp["template"], "")

    def test_an_absent_plan_projects_an_empty_stamp(self):
        todo = read_omh_hud(self.home, self.hermes)["todo"]

        self.assertEqual(todo["status"], "absent")
        self.assertEqual(todo["template"], "")


class ToolSurfaceTest(_TodoHomeTest):
    """The template as the model reaches it, through `omh_todo`."""

    def setUp(self) -> None:
        super().setUp()
        self._env_patch = patch.dict(os.environ, {"OMH_HOME": str(self.home)})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def call(self, args: dict) -> dict:
        return json.loads(omh_todo_handler(args, session_id=SESSION))

    def test_the_schema_offers_the_template_by_name(self):
        template = OMH_TODO_SCHEMA["parameters"]["properties"]["template"]

        self.assertEqual(template["enum"], [CODE_STORY_TEMPLATE])
        # The two things the enum cannot carry: when to reach for it, and
        # that a phase is carried rather than dropped.
        self.assertIn("state=done with a blocked_reason", template["description"])
        self.assertIn("Omit this field for an ordinary plan", template["description"])

    def test_a_stamped_set_writes_the_ten_phases(self):
        result = self.call(
            {"action": "set", "title": "story", "template": CODE_STORY_TEMPLATE}
        )

        self.assertEqual(result["status"], "written")
        self.assertEqual(result["todo"]["counts"]["total"], 10)
        self.assertEqual(result["todo"]["template"], CODE_STORY_TEMPLATE)

    def test_a_dropped_phase_comes_back_as_an_invalid_todo_the_writer_can_act_on(self):
        items = [
            item
            for item in template_items(CODE_STORY_TEMPLATE)
            if item["phase"] != "VIII. ELI5"
        ]

        result = self.call(
            {
                "action": "set",
                "title": "story",
                "template": CODE_STORY_TEMPLATE,
                "items": items,
            }
        )

        self.assertEqual(result["status"], "invalid_todo")
        self.assertIn("'VIII. ELI5'", result["error"])

    def test_a_set_without_the_field_declares_an_ordinary_plan(self):
        # Nothing reads the title, the item text, or anything else to decide
        # that ten phases apply. The field is the whole of the declaration.
        result = self.call(
            {
                "action": "set",
                "title": "carry this code story from story to close",
                "items": [{"text": "do the thing"}],
            }
        )

        self.assertEqual(result["status"], "written")
        self.assertEqual(result["todo"]["counts"]["total"], 1)
        self.assertEqual(result["todo"]["template"], "")


if __name__ == "__main__":  # pragma: no cover - runner entry
    unittest.main()

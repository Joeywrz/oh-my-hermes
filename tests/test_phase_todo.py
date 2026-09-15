"""Contracts for the phase-structured todo (todo init) surfaces.

A plan declared before engine work bounds the run: phases with tasks, one
active item, HUD shows the current phase's checklist. These tests pin the
store's optional `phase` field, the current-phase selection in the HUD
projection, and the unphased fallback.
"""

import tempfile
import unittest
from pathlib import Path

from omh.plugin_bundle.omh.runtime_reader import read_omh_hud
from omh.plugin_bundle.omh.todo_store import (
    MAX_TODO_BLOCKED_REASON_CHARS,
    MAX_TODO_PHASE_CHARS,
    TodoValidationError,
    build_todo_record,
    validate_todo_items,
    write_todo,
)

PHASED_ITEMS = [
    {"text": "Inventory repositories", "state": "active", "phase": "Internal Context"},
    {"text": "Identify evaluation data", "state": "pending", "phase": "Internal Context"},
    {"text": "Define comparison workflow", "state": "pending", "phase": "Product Fit"},
    {"text": "Select MVP boundary", "state": "pending", "phase": "Product Fit"},
    {"text": "Present product direction", "state": "pending", "phase": "Delivery"},
]


def _projected_todo(items):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "omh").mkdir()
        (root / "hermes").mkdir()
        write_todo(root / "omh", build_todo_record("init", items, source="test"))
        return read_omh_hud(root / "omh", root / "hermes")["todo"]


class PhaseFieldStoreTest(unittest.TestCase):
    def test_the_phase_field_is_optional_and_absent_when_empty(self):
        validated = validate_todo_items([{"text": "task", "phase": ""}])
        self.assertEqual(validated, [{"text": "task", "state": "pending"}])

    def test_a_phase_is_kept_control_stripped(self):
        validated = validate_todo_items([{"text": "task", "phase": "Inter\x1bnal"}])
        self.assertEqual(validated[0]["phase"], "Internal")

    def test_an_oversized_phase_is_refused(self):
        with self.assertRaises(TodoValidationError):
            validate_todo_items([{"text": "task", "phase": "x" * (MAX_TODO_PHASE_CHARS + 1)}])


class BlockedReasonFieldStoreTest(unittest.TestCase):
    """The plan's stop criterion, recorded rather than inferred.

    `TODO_CONTINUATION_RULE` ends a plan when "an item carries an omh_todo
    blocked_reason". It used to end when an item was "recorded blocked with
    its reason", which a model could satisfy only by writing prose into the
    item's text -- so the reason had to be read back out of that text, which
    was wrong in both directions on ordinary input: an item
    saying work is *not* blocked read as blocked, and a Korean or plainly
    worded block did not read as blocked at all. The field keeps the state out
    of prose, and the item keeps its three-valued state so nothing in the
    counts or the HUD projection has to learn a fourth.
    """

    def test_the_blocked_reason_is_optional_and_absent_when_empty(self):
        validated = validate_todo_items([{"text": "task", "blocked_reason": ""}])
        self.assertEqual(validated, [{"text": "task", "state": "pending"}])

    def test_a_blocked_reason_is_kept_control_stripped(self):
        validated = validate_todo_items([{"text": "task", "blocked_reason": "owner\x1b review"}])
        self.assertEqual(validated[0]["blocked_reason"], "owner review")

    def test_an_oversized_blocked_reason_is_refused(self):
        with self.assertRaises(TodoValidationError):
            validate_todo_items(
                [{"text": "task", "blocked_reason": "x" * (MAX_TODO_BLOCKED_REASON_CHARS + 1)}]
            )

    def test_a_blocked_item_keeps_its_state_and_its_place_in_the_counts(self):
        # The reason is a field, not a fourth state: an item carrying one is
        # still active, still counted, still rendered. That is the whole
        # reason this shape was chosen over a `blocked` item state.
        projected = _projected_todo(
            [
                {"text": "land the fix", "state": "done"},
                {"text": "open the PR", "state": "active", "blocked_reason": "소유자 승인 대기"},
            ]
        )

        self.assertEqual(
            projected["counts"],
            {"total": 2, "done": 1, "active": 1, "pending": 0, "phases": 0},
        )
        self.assertEqual(projected["items"][1]["blocked_reason"], "소유자 승인 대기")


class DepthFieldStoreTest(unittest.TestCase):
    def test_depth_is_optional_and_absent_at_zero(self):
        validated = validate_todo_items([{"text": "task", "depth": 0}])
        self.assertEqual(validated, [{"text": "task", "state": "pending"}])

    def test_a_subtask_depth_up_to_three_is_kept(self):
        validated = validate_todo_items(
            [
                {"text": "검증작업하기", "phase": "Verification"},
                {"text": "사용성 검증", "depth": 1},
                {"text": "UI 검증", "depth": 2},
                {"text": "부하 검증", "depth": 3},
            ]
        )
        self.assertEqual([item.get("depth", 0) for item in validated], [0, 1, 2, 3])

    def test_a_depth_beyond_three_or_non_integer_is_refused(self):
        for depth in (4, -1, True, "1"):
            with self.assertRaises(TodoValidationError):
                validate_todo_items([{"text": "task", "depth": depth}])

    def test_the_hud_projection_carries_depth_through(self):
        todo = _projected_todo(
            [
                {"text": "Verify", "state": "active", "phase": "Verification"},
                {"text": "usability", "state": "pending", "depth": 1},
            ]
        )
        self.assertEqual([item.get("depth", 0) for item in todo["items"]], [0, 1])


class PhaseProjectionTest(unittest.TestCase):
    def test_the_current_phase_is_the_one_holding_the_active_item(self):
        todo = _projected_todo(PHASED_ITEMS)
        self.assertEqual(todo["status"], "established")
        self.assertEqual(todo["counts"]["phases"], 3)
        self.assertEqual(todo["display_phase"], "Internal Context")
        self.assertEqual(
            [item["text"] for item in todo["display_items"]],
            ["Inventory repositories", "Identify evaluation data"],
        )
        # +N more covers the remaining work in later phases.
        self.assertEqual(todo["more_count"], 3)

    def test_a_finished_phase_advances_the_display_to_the_next_phase_with_work(self):
        items = [dict(item) for item in PHASED_ITEMS]
        items[0]["state"] = "done"
        items[1]["state"] = "done"
        items[2]["state"] = "active"
        todo = _projected_todo(items)
        self.assertEqual(todo["display_phase"], "Product Fit")
        self.assertEqual(
            [item["text"] for item in todo["display_items"]],
            ["Define comparison workflow", "Select MVP boundary"],
        )

    def test_an_unphased_plan_keeps_the_flat_collapse(self):
        todo = _projected_todo(
            [
                {"text": "one", "state": "done"},
                {"text": "two", "state": "active"},
                {"text": "three", "state": "pending"},
            ]
        )
        self.assertEqual(todo["display_phase"], "")
        self.assertEqual(todo["counts"]["phases"], 0)
        self.assertEqual(
            [item["text"] for item in todo["display_items"]], ["one", "two", "three"]
        )


if __name__ == "__main__":
    unittest.main()

"""Contracts for per-chunk fanout units and the per-unit input budget.

Two things are pinned here:

- the optional `input_budget` a unit may declare is bounded, typed, and
  internally consistent at freeze time, and a unit that declares none leaves
  its frozen shape and its executor prompt byte-identical;
- `omh coding fanout prepare --from-document-plan` derives exactly one unit
  per `document_chunk_plan/v1` range, carries the range and its budget into
  the frozen contract and the prompt, and refuses a plan without ranges with
  a non-zero exit instead of freezing an empty split.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()

from _cli_harness import run_cli  # noqa: E402

from omh.coding.fanout import build_fanout_contract  # noqa: E402
from omh.coding.fanout_contracts import (  # noqa: E402
    FANOUT_SPAWN_PLAN_THRESHOLD,
    FANOUT_UNIT_INPUT_BUDGET_SCHEMA_VERSION,
    FanoutContractError,
    MAX_UNIT_SOURCE_RANGES,
)
from omh.coding.fanout_dispatch import build_unit_prompt  # noqa: E402
from omh.coding.fanout_document_units import (  # noqa: E402
    fanout_units_from_document_plan,
    read_document_plan_file,
)
from omh.plugin_bundle.omh import document_chunk_plan as planner  # noqa: E402


def _plan(pages: int = 300, **overrides: object) -> dict:
    request = planner.normalize_plan_request({"source": "arXiv:2401.00001", "pages": pages, "lines": 6_000, **overrides})
    return planner.build_document_plan(request, planner.fingerprint_source("arXiv:2401.00001", None), now="2026-09-13T00:00:00Z")


def _units(budget: dict | None) -> list[dict]:
    first = {"unit_id": "reader", "title": "Read", "owner": None, "file_scope": ["reports/a.md"], "depends_on": []}
    if budget is not None:
        first["input_budget"] = budget
    return [first, {"unit_id": "other", "title": "Other", "owner": None, "file_scope": ["reports/b.md"], "depends_on": []}]


def _budget(**overrides: object) -> dict:
    budget = {
        "chars": 100_000,
        "tokens": 25_000,
        "source_ranges": [
            {"source": "arXiv:2401.00001", "span": "pages 1-62", "offset": 1, "limit": 1240, "end_line": 1240, "estimated_chars": 99_200, "digest": "abc123def456"},
        ],
    }
    budget.update(overrides)
    return budget


class InputBudgetContractTests(unittest.TestCase):
    def test_a_declared_budget_is_frozen_on_the_unit_with_its_boundary(self) -> None:
        contract = build_fanout_contract("read it", _units(_budget()))
        unit = {u["unit_id"]: u for u in contract["units"]}["reader"]
        budget = unit["input_budget"]
        self.assertEqual(budget["schema_version"], FANOUT_UNIT_INPUT_BUDGET_SCHEMA_VERSION)
        self.assertEqual(budget["chars"], 100_000)
        self.assertEqual(budget["tokens"], 25_000)
        self.assertEqual(budget["source_ranges"][0]["span"], "pages 1-62")
        self.assertEqual(budget["source_ranges"][0]["end_line"], 1240)
        self.assertIn("not evidence that the unit read them", budget["claim_boundary"])

    def test_an_undeclared_budget_leaves_the_unit_byte_identical(self) -> None:
        contract = build_fanout_contract("read it", _units(None))
        for unit in contract["units"]:
            self.assertNotIn("input_budget", unit)
        prompt = build_unit_prompt(contract["units"][0], "read it")
        self.assertNotIn("Input budget", prompt)

    def test_tokens_only_when_declared(self) -> None:
        contract = build_fanout_contract("read it", _units({"chars": 5_000}))
        budget = contract["units"][0]["input_budget"]
        self.assertEqual(budget["chars"], 5_000)
        self.assertNotIn("tokens", budget)
        self.assertEqual(budget["source_ranges"], [])

    def test_bounds_types_and_consistency_are_refused_at_freeze(self) -> None:
        cases = (
            ("not an object", "must be an object"),
            ({"tokens": 10}, "needs chars"),
            ({"chars": 0}, "at least 1"),
            ({"chars": True}, "must be an integer"),
            ({"chars": 10_000_001}, "capped at"),
            ({"chars": 100, "tokens": 101}, "more tokens"),
            ({"chars": 100_000, "tokens": 10}, "fewer than one per 16"),
            ({"chars": 100, "budget": 1}, "unknown keys"),
            ({"chars": 100, "source_ranges": "pages"}, "must be a list"),
            ({"chars": 100, "source_ranges": [{"span": "x"}]}, "needs a source and a span"),
            ({"chars": 100, "source_ranges": [{"source": "s", "span": "x", "pages": 1}]}, "unknown keys"),
            ({"chars": 100, "source_ranges": [{"source": "s", "span": "x", "offset": 5, "end_line": 4}]}, "end_line is before"),
            ({"chars": 100, "source_ranges": [{"source": "s", "span": "x", "estimated_chars": 101}]}, "over the 100-char budget"),
            ({"chars": 100, "source_ranges": [{"source": "s", "span": "x", "digest": "no-dashes!"}]}, "alphanumeric"),
            ({"chars": 100, "source_ranges": [{"source": "s", "span": "x"}] * (MAX_UNIT_SOURCE_RANGES + 1)}, "capped at"),
        )
        for budget, fragment in cases:
            with self.subTest(budget=budget), self.assertRaises(FanoutContractError) as caught:
                build_fanout_contract("read it", _units(budget))
            self.assertIn(fragment, str(caught.exception))

    def test_the_prompt_states_the_range_and_the_budget(self) -> None:
        contract = build_fanout_contract("read it", _units(_budget()))
        prompt = build_unit_prompt(contract["units"][0], "read it")
        self.assertIn("Input budget for this unit: at most 100000 characters (about 25000 tokens)", prompt)
        self.assertIn("1. arXiv:2401.00001 — pages 1-62: read_file offset=1 limit=1240, continue via next_offset to line 1240 (about 99200 chars)", prompt)
        # The budget block sits with the unit's own boundary lines, after the shared head.
        self.assertLess(prompt.index("Stay strictly inside these paths"), prompt.index("Input budget for this unit"))


class DerivationTests(unittest.TestCase):
    def test_one_unit_per_range_with_budget_scope_and_brief(self) -> None:
        plan = _plan()
        derived = fanout_units_from_document_plan(plan)
        self.assertEqual(derived["schema_version"], "fanout_document_units/v1")
        self.assertEqual(derived["plan_id"], plan["plan_id"])
        self.assertEqual([u["unit_id"] for u in derived["units"]], ["range-1", "range-2", "range-3", "range-4", "range-5"])
        first = derived["units"][0]
        self.assertEqual(first["file_scope"], [f"reports/document-plan-{plan['plan_id']}/range-1.md"])
        self.assertEqual(first["depends_on"], [])
        self.assertIsNone(first["owner"])
        # The plan file holds the brief template once; a range carries the
        # filled brief only in a tool result, so the unit title is what
        # `range_view` fills for that range.
        self.assertEqual(first["title"], planner.range_view(plan, plan["ranges"][0])["brief"])
        budget = first["input_budget"]
        self.assertEqual(budget["chars"], plan["inputs"]["budget_chars"])
        self.assertEqual(budget["tokens"], 25_000)
        source_range = budget["source_ranges"][0]
        self.assertEqual(source_range["span"], "pages 1-62")
        self.assertEqual(source_range["offset"], plan["ranges"][0]["read_window"]["offset"])
        self.assertEqual(source_range["limit"], plan["ranges"][0]["read_window"]["limit"])
        self.assertEqual(source_range["estimated_chars"], plan["ranges"][0]["estimated_chars"])
        self.assertEqual(source_range["digest"], plan["ranges"][0]["digest"])

    def test_unit_ids_are_zero_padded_past_nine_ranges(self) -> None:
        derived = fanout_units_from_document_plan(_plan(pages=700))
        self.assertEqual(derived["units"][0]["unit_id"], "range-01")
        self.assertEqual(derived["units"][-1]["unit_id"], "range-12")

    def test_a_wide_plan_carries_a_derived_spawn_plan_and_a_narrow_one_does_not(self) -> None:
        wide = fanout_units_from_document_plan(_plan())
        self.assertGreater(len(wide["units"]), FANOUT_SPAWN_PLAN_THRESHOLD)
        self.assertIn(wide["plan_id"], wide["spawn_plan"]["why_parallel"])
        self.assertTrue(all(len(text) <= 280 for text in wide["spawn_plan"].values()))
        narrow = fanout_units_from_document_plan(_plan(pages=200))
        self.assertEqual(len(narrow["units"]), 4)
        self.assertIsNone(narrow["spawn_plan"])

    def test_report_dir_and_owner_are_honoured(self) -> None:
        derived = fanout_units_from_document_plan(_plan(), report_dir="docs/reads/", owner="codex")
        self.assertEqual(derived["units"][2]["file_scope"], ["docs/reads/range-3.md"])
        self.assertTrue(all(u["owner"] == "codex" for u in derived["units"]))

    def test_a_dense_range_raises_its_own_ceiling(self) -> None:
        plan = _plan(pages=3, chars=300_000, lines=15_000)
        derived = fanout_units_from_document_plan(plan)
        self.assertEqual(derived["units"][0]["input_budget"]["chars"], 100_000)
        # Each single page carries 100,000 chars, equal to the budget; a larger page would exceed it.
        self.assertEqual(derived["units"][0]["input_budget"]["source_ranges"][0]["estimated_chars"], 100_000)

    def test_derived_units_freeze_into_a_contract(self) -> None:
        derived = fanout_units_from_document_plan(_plan())
        contract = build_fanout_contract(derived["default_goal"], derived["units"], spawn_plan=derived["spawn_plan"])
        self.assertEqual(len(contract["units"]), 5)
        self.assertEqual(contract["spawn_plan"]["unit_count"], 5)
        self.assertEqual(contract["merge_plan"]["conflict_risk_notes"], [])
        for unit in contract["units"]:
            self.assertEqual(unit["input_budget"]["source_ranges"][0]["source"], "arXiv:2401.00001")

    def test_a_plan_without_ranges_is_refused(self) -> None:
        plan = _plan()
        plan["ranges"] = []
        plan["range_count"] = 0
        with self.assertRaises(FanoutContractError) as caught:
            fanout_units_from_document_plan(plan)
        self.assertIn("no ranges", str(caught.exception))

    def test_a_foreign_file_is_refused_with_its_reason(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(FanoutContractError) as caught:
                read_document_plan_file(path)
            self.assertIn("JSON object", str(caught.exception))
            with self.assertRaises(FanoutContractError):
                read_document_plan_file(Path(tmp) / "missing.json")


class PrepareFromDocumentPlanCliTests(unittest.TestCase):
    def _base(self, root: Path) -> list[str]:
        return ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes"), "coding", "fanout", "prepare"]

    def test_prepare_freezes_one_unit_per_range(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = _plan()
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            status, stdout, stderr = run_cli(self._base(root) + ["--from-document-plan", str(plan_path), "--record"])
            self.assertEqual(status, 0, stderr)
            contract = json.loads(stdout)
            self.assertEqual(contract["schema_version"], "fanout_contract/v2")
            self.assertEqual(contract["status"], "prepared_not_observed")
            self.assertEqual([u["unit_id"] for u in contract["units"]], ["range-1", "range-2", "range-3", "range-4", "range-5"])
            self.assertEqual(contract["spawn_plan"]["unit_count"], 5)
            self.assertEqual(contract["units"][0]["input_budget"]["source_ranges"][0]["span"], "pages 1-62")
            recorded = Path(contract["artifacts"]["contract_path"])
            self.assertTrue(recorded.is_file(), recorded)
            self.assertTrue(recorded.is_relative_to((root / ".omh").resolve()), recorded)

    def test_goal_report_dir_and_owner_flags_ride_along(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
            status, stdout, stderr = run_cli(
                self._base(root)
                + ["--from-document-plan", str(plan_path), "--goal", "explain", "the", "paper", "--report-dir", "notes/paper", "--owner", "claude-code"]
            )
            self.assertEqual(status, 0, stderr)
            contract = json.loads(stdout)
            self.assertEqual(contract["units"][0]["boundary"]["file_scope"], ["notes/paper/range-1.md"])
            self.assertEqual(contract["units"][0]["owner"], "claude-code")
            self.assertEqual(contract["goal"]["input_chars"], len("explain the paper"))

    def test_a_plan_without_ranges_exits_non_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = _plan()
            plan["ranges"] = []
            plan["range_count"] = 0
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            status, stdout, stderr = run_cli(self._base(root) + ["--from-document-plan", str(plan_path)])
            self.assertEqual(status, 2)
            self.assertIn("no ranges", stderr)
            self.assertFalse((root / ".omh" / "coding").exists())

    def test_units_and_document_plan_are_exclusive_and_units_still_needs_a_goal(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
            units_path = root / "units.json"
            units_path.write_text(json.dumps(_units(None)), encoding="utf-8")
            # argparse refuses the pair itself, before any handler runs, with its usage exit.
            with self.assertRaises(SystemExit) as exited:
                run_cli(self._base(root) + ["--from-document-plan", str(plan_path), "--units", str(units_path)])
            self.assertEqual(exited.exception.code, 2)
            status, _stdout, stderr = run_cli(self._base(root) + ["--units", str(units_path)])
            self.assertEqual(status, 2)
            self.assertIn("--goal", stderr)


if __name__ == "__main__":
    unittest.main()

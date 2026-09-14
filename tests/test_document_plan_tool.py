"""Contracts for the `omh_document_plan` plugin tool and its planner.

The planner turns numbers the caller states into numbered read ranges with
`read_file` windows and a resumable ledger. Three things have to hold:

- it never opens the document: a local file contributes only its size and
  hash, and every page or character count stays labelled `caller_supplied`;
- the same document and parameters always yield the same plan id, and
  re-planning keeps the ledger a reader has already advanced;
- the ranges honour the read budget, the outline boundaries, and Hermes'
  per-call line limit, so a range is something one read_file call reaches.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from _local_package import load_local_package

load_local_package()
from omh.plugin_bundle.omh import document_chunk_plan as planner  # noqa: E402
from omh.plugin_bundle.omh import runtime_paths  # noqa: E402
from omh.plugin_bundle.omh.metadata import PROVIDED_TOOLS, TOOL_FILE_STEMS  # noqa: E402
from omh.plugin_bundle.omh.tools.document_plan_tool import (  # noqa: E402
    OMH_DOCUMENT_PLAN_SCHEMA,
    omh_document_plan_handler,
)

_BUNDLE = Path(__file__).resolve().parents[1] / "src" / "plugin_bundle" / "omh"
_OUTLINE = [
    {"title": "Results", "page": 150},
    {"title": "Introduction", "page": 1},
    {"title": "Method", "page": 40},
    {"title": "Appendix", "page": 260},
]


def _call(**args: object) -> dict:
    return json.loads(omh_document_plan_handler(dict(args)))


class _HomeCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.omh_home = self.root / ".omh"
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {"OMH_HOME": str(self.omh_home), "HERMES_HOME": str(self.root / ".hermes")},
            )
        )
        # Force the standalone boundary regardless of the developer's host.
        self.enterContext(mock.patch.dict(sys.modules, {"hermes_constants": None}))


class RegistrationTests(unittest.TestCase):
    def test_the_tool_is_registered_under_its_file_stem(self) -> None:
        self.assertIn("omh_document_plan", PROVIDED_TOOLS)
        self.assertEqual(TOOL_FILE_STEMS["omh_document_plan"], "document_plan_tool")
        self.assertEqual(OMH_DOCUMENT_PLAN_SCHEMA["name"], "omh_document_plan")

    def test_the_schema_defaults_mirror_the_hermes_read_window(self) -> None:
        props = OMH_DOCUMENT_PLAN_SCHEMA["parameters"]["properties"]
        self.assertEqual(props["budget_chars"]["default"], 100_000)
        self.assertEqual(props["action"]["enum"], ["plan", "show", "mark"])
        self.assertEqual(props["state"]["enum"], ["covered", "next", "missing"])

    def test_the_description_refuses_to_read_as_evidence(self) -> None:
        description = OMH_DOCUMENT_PLAN_SCHEMA["description"]
        self.assertIn("never opens or parses the document", description)
        self.assertIn("never coverage or comprehension evidence", description)

    def test_the_bundle_modules_import_only_siblings_and_the_standard_library(self) -> None:
        """The bundle is copied under Hermes' plugins dir; `omh.*` is not there."""
        for relative in ("document_chunk_plan.py", "tools/document_plan_tool.py"):
            tree = ast.parse((_BUNDLE / relative).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.level:
                        continue
                    top = str(node.module or "").split(".")[0]
                    self.assertIn(top, sys.stdlib_module_names, f"{relative}: from {node.module}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        self.assertIn(top, sys.stdlib_module_names, f"{relative}: import {alias.name}")


class PlanByPagesTests(_HomeCase):
    def test_pages_split_at_the_budget_density(self) -> None:
        payload = _call(action="plan", source="arXiv:2401.00001", pages=300)
        self.assertEqual(payload["status"], "planned")
        plan = payload["plan"]
        self.assertEqual(plan["schema_version"], "document_chunk_plan/v1")
        self.assertEqual(plan["unit"], "pages")
        # 100,000 / 1,600 = 62 pages per range; 300 pages need five.
        self.assertEqual(plan["range_count"], 5)
        self.assertEqual([r["pages"] for r in plan["ranges"]][:2], [{"start": 1, "end": 62}, {"start": 63, "end": 124}])
        self.assertEqual(plan["ranges"][-1]["pages"], {"start": 249, "end": 300})
        self.assertEqual(plan["ranges"][0]["estimated_chars"], 62 * 1_600)
        self.assertEqual(plan["ranges"][0]["estimated_tokens"], 62 * 1_600 // 4)
        for entry in plan["ranges"]:
            self.assertLessEqual(entry["estimated_chars"], plan["inputs"]["budget_chars"])

    def test_pages_and_chars_together_use_the_observed_density(self) -> None:
        payload = _call(action="plan", source="paper.pdf", pages=100, chars=400_000)
        plan = payload["plan"]
        self.assertEqual(plan["assumptions"]["chars_per_page"], {"value": 4_000.0, "basis": "derived_from_caller_chars_and_pages"})
        # 100,000 / 4,000 = 25 pages per range.
        self.assertEqual(plan["range_count"], 4)
        self.assertEqual(sum(r["estimated_chars"] for r in plan["ranges"]), 400_000)

    def test_every_caller_number_is_labelled_caller_supplied_never_observed(self) -> None:
        payload = _call(action="plan", source="paper.pdf", pages=12, chars=20_000, lines=400, budget_chars=5_000)
        provenance = payload["plan"]["provenance"]
        self.assertEqual(provenance["pages"], "caller_supplied")
        self.assertEqual(provenance["chars"], "caller_supplied")
        self.assertEqual(provenance["lines"], "caller_supplied")
        self.assertEqual(provenance["budget_chars"], "caller_supplied")
        self.assertEqual(provenance["chars_per_page"], "default")
        self.assertEqual(provenance["size_bytes"], "not_observed")
        self.assertEqual(provenance["sha256"], "not_observed")
        self.assertNotIn("observed", {provenance[key] for key in ("pages", "chars", "lines")})

    def test_a_plan_needs_pages_or_chars(self) -> None:
        payload = _call(action="plan", source="paper.pdf")
        self.assertEqual(payload["status"], "invalid_plan")
        self.assertIn("pages or chars", payload["error"])
        self.assertNotIn("plan", payload)

    def test_counts_are_bounded_and_typed(self) -> None:
        for args, fragment in (
            ({"pages": 0}, "at least 1"),
            ({"pages": planner.MAX_PAGES + 1}, "capped"),
            ({"pages": True}, "boolean"),
            ({"pages": 2.5}, "whole number"),
            ({"pages": 10, "budget_chars": 10}, "at least"),
            ({"chars": 200_000_000, "budget_chars": 1_000}, "over the cap"),
        ):
            with self.subTest(args=args):
                payload = _call(action="plan", source="paper.pdf", **args)
                self.assertEqual(payload["status"], "invalid_plan")
                self.assertIn(fragment, payload["error"])


class PlanByCharsTests(_HomeCase):
    def test_chars_split_at_the_budget(self) -> None:
        payload = _call(action="plan", source="notes", chars=250_000)
        plan = payload["plan"]
        self.assertEqual(plan["unit"], "chars")
        self.assertEqual(plan["range_count"], 3)
        self.assertEqual([r["chars"] for r in plan["ranges"]], [
            {"start": 0, "end": 100_000},
            {"start": 100_000, "end": 200_000},
            {"start": 200_000, "end": 250_000},
        ])
        self.assertTrue(all(r["pages"] is None for r in plan["ranges"]))
        self.assertEqual(plan["assumptions"]["chars_per_page"]["basis"], "not_applicable")

    def test_an_outline_without_pages_is_refused(self) -> None:
        payload = _call(action="plan", source="notes", chars=250_000, outline=[{"title": "A", "page": 1}])
        self.assertEqual(payload["status"], "invalid_plan")
        self.assertIn("outline needs pages", payload["error"])


class OutlineTests(_HomeCase):
    def test_ranges_start_at_section_boundaries_and_name_their_sections(self) -> None:
        payload = _call(action="plan", source="arXiv:2401.00001", pages=300, outline=_OUTLINE)
        plan = payload["plan"]
        self.assertEqual(plan["inputs"]["outline"][0], {"title": "Introduction", "page": 1})
        starts = [r["pages"]["start"] for r in plan["ranges"]]
        for anchor in (1, 40, 150, 260):
            self.assertIn(anchor, starts, f"section starting on page {anchor} opens a range")
        by_start = {r["pages"]["start"]: r for r in plan["ranges"]}
        self.assertEqual(by_start[1]["pages"]["end"], 39)
        self.assertEqual(by_start[1]["sections"], ["Introduction"])
        self.assertEqual(by_start[260]["sections"], ["Appendix"])
        # A 110-page section is split at the 62-page budget and both halves name it.
        self.assertEqual(by_start[40]["pages"]["end"], 101)
        self.assertEqual(by_start[102]["sections"], ["Method"])
        self.assertEqual(plan["range_count"], 6)

    def test_short_sections_share_a_range(self) -> None:
        outline = [{"title": f"Chapter {index}", "page": 1 + 10 * index} for index in range(10)]
        payload = _call(action="plan", source="book.pdf", pages=100, outline=outline)
        plan = payload["plan"]
        self.assertEqual(plan["range_count"], 2)
        self.assertEqual(plan["ranges"][0]["pages"], {"start": 1, "end": 60})
        self.assertEqual(len(plan["ranges"][0]["sections"]), 6)

    def test_front_matter_before_the_first_anchor_is_its_own_span(self) -> None:
        payload = _call(action="plan", source="book.pdf", pages=20, outline=[{"title": "Body", "page": 5}])
        plan = payload["plan"]
        self.assertEqual(plan["ranges"][0]["pages"], {"start": 1, "end": 20})
        self.assertEqual(plan["ranges"][0]["sections"], ["Body"])

    def test_malformed_outline_entries_are_refused(self) -> None:
        for outline, fragment in (
            ("Introduction", "list"),
            (["Introduction"], "object"),
            ([{"title": "", "page": 1}], "non-empty title"),
            ([{"title": "A"}], "needs a page"),
            ([{"title": "A", "page": 301}], "past the last page"),
        ):
            with self.subTest(outline=outline):
                payload = _call(action="plan", source="paper.pdf", pages=300, outline=outline)
                self.assertEqual(payload["status"], "invalid_plan")
                self.assertIn(fragment, payload["error"])


class ReadWindowTests(_HomeCase):
    def test_caller_lines_give_contiguous_exact_windows(self) -> None:
        payload = _call(action="plan", source="paper.pdf", pages=300, lines=6_000)
        ranges = payload["plan"]["ranges"]
        self.assertEqual(ranges[0]["read_window"]["basis"], "caller_lines")
        self.assertEqual(ranges[0]["read_window"]["offset"], 1)
        self.assertEqual(ranges[-1]["read_window"]["end_line"], 6_000)
        for previous, current in zip(ranges, ranges[1:]):
            self.assertEqual(current["read_window"]["offset"], previous["read_window"]["end_line"] + 1)

    def test_the_limit_never_exceeds_the_hermes_line_cap_and_extra_calls_are_counted(self) -> None:
        # 3 pages at 100k chars each: one page per range, 5,000 lines per range.
        payload = _call(action="plan", source="dense.pdf", pages=3, chars=300_000, lines=15_000)
        for entry in payload["plan"]["ranges"]:
            window = entry["read_window"]
            self.assertLessEqual(window["limit"], planner.HERMES_READ_LINE_LIMIT)
            self.assertEqual(window["read_calls_estimated"], 3)
            self.assertEqual(window["end_line"] - window["offset"] + 1, 5_000)

    def test_without_lines_the_window_is_estimated_and_says_so(self) -> None:
        payload = _call(action="plan", source="paper.pdf", pages=10)
        plan = payload["plan"]
        self.assertEqual(plan["assumptions"]["total_lines"]["basis"], "estimated_lines")
        self.assertEqual(plan["assumptions"]["chars_per_line"], {"value": planner.ASSUMED_CHARS_PER_LINE, "basis": "assumed"})
        self.assertEqual(plan["ranges"][0]["read_window"]["basis"], "estimated_lines")

    def test_each_range_carries_a_digest_of_its_spec(self) -> None:
        payload = _call(action="plan", source="paper.pdf", pages=300)
        digests = [entry["digest"] for entry in payload["plan"]["ranges"]]
        self.assertEqual(len(set(digests)), len(digests))
        self.assertTrue(all(len(digest) == 12 for digest in digests))


class DelegationHintTests(_HomeCase):
    def test_more_than_four_ranges_fan_out_with_a_filled_brief_per_range(self) -> None:
        payload = _call(action="plan", source="arXiv:2401.00001", pages=300, lines=6_000)
        hint = payload["delegation_hint"]
        self.assertEqual(hint["mode"], "fan_out")
        self.assertEqual(hint["tool"], "delegate_task")
        self.assertEqual(len(hint["range_briefs"]), 5)
        self.assertIn("read_file(path, offset=1, limit=1240)", hint["range_briefs"][0])
        self.assertIn(f"plan {payload['plan_id']} chunk 1", hint["range_briefs"][0])
        self.assertIn("{offset}", hint["per_range_brief_template"])
        self.assertIn("action=mark", hint["after_each_range"])

    def test_four_or_fewer_ranges_stay_sequential(self) -> None:
        payload = _call(action="plan", source="short.pdf", pages=200)
        self.assertEqual(payload["plan"]["range_count"], 4)
        self.assertEqual(payload["delegation_hint"]["mode"], "sequential")

    def test_a_huge_plan_ships_the_template_without_filled_briefs(self) -> None:
        payload = _call(action="plan", source="corpus", chars=50 * 100_000)
        self.assertEqual(payload["plan"]["range_count"], 50)
        self.assertNotIn("range_briefs", payload["delegation_hint"])


class LedgerTests(_HomeCase):
    def _plan(self) -> dict:
        return _call(action="plan", source="arXiv:2401.00001", pages=300)

    def test_a_fresh_plan_points_next_at_the_first_range(self) -> None:
        payload = self._plan()
        self.assertEqual(payload["ledger_summary"], {"covered": [], "next": [1], "missing": [2, 3, 4, 5]})

    def test_marking_covered_advances_next_and_keeps_the_note(self) -> None:
        plan_id = self._plan()["plan_id"]
        marked = _call(action="mark", plan_id=plan_id, chunk=1, state="covered", note="report in turn 4")
        self.assertEqual(marked["status"], "marked")
        self.assertEqual(marked["ledger_summary"], {"covered": [1], "next": [2], "missing": [3, 4, 5]})
        self.assertEqual(marked["plan"]["ledger"]["1"]["note"], "report in turn 4")
        self.assertTrue(marked["plan"]["ledger"]["1"]["marked_at"])
        shown = _call(action="show", plan_id=plan_id)
        self.assertEqual(shown["status"], "read")
        self.assertEqual(shown["ledger_summary"], marked["ledger_summary"])

    def test_marking_next_demotes_the_previous_next(self) -> None:
        plan_id = self._plan()["plan_id"]
        marked = _call(action="mark", plan_id=plan_id, chunk=4, state="next")
        self.assertEqual(marked["ledger_summary"], {"covered": [], "next": [4], "missing": [1, 2, 3, 5]})

    def test_marking_missing_after_compression_reopens_the_range(self) -> None:
        plan_id = self._plan()["plan_id"]
        _call(action="mark", plan_id=plan_id, chunk=1, state="covered")
        marked = _call(action="mark", plan_id=plan_id, chunk=1, state="missing", note="report lost to compression")
        self.assertEqual(marked["ledger_summary"], {"covered": [], "next": [2], "missing": [1, 3, 4, 5]})

    def test_the_last_covered_mark_leaves_nothing_next(self) -> None:
        plan_id = self._plan()["plan_id"]
        for chunk in range(1, 6):
            marked = _call(action="mark", plan_id=plan_id, chunk=chunk, state="covered")
        self.assertEqual(marked["ledger_summary"], {"covered": [1, 2, 3, 4, 5], "next": [], "missing": []})

    def test_invalid_marks_are_refused_without_touching_the_ledger(self) -> None:
        plan_id = self._plan()["plan_id"]
        for args, fragment in (
            ({"chunk": 9, "state": "covered"}, "past the last range"),
            ({"chunk": 1, "state": "done"}, "state must be one of"),
            ({"state": "covered"}, "chunk number"),
            ({"chunk": 1, "state": "covered", "note": "x" * 501}, "note is capped"),
        ):
            with self.subTest(args=args):
                payload = _call(action="mark", plan_id=plan_id, **args)
                self.assertEqual(payload["status"], "invalid_mark")
                self.assertIn(fragment, payload["error"])
        self.assertEqual(_call(action="show", plan_id=plan_id)["ledger_summary"]["covered"], [])

    def test_an_unknown_plan_id_is_refused(self) -> None:
        for plan_id, fragment in (("", "12-hex"), ("nope", "12-hex"), ("0123456789ab", "no document plan")):
            with self.subTest(plan_id=plan_id):
                payload = _call(action="show", plan_id=plan_id)
                self.assertEqual(payload["status"], "invalid_show")
                self.assertIn(fragment, payload["error"])


class PlanIdentityTests(_HomeCase):
    def test_the_same_inputs_yield_the_same_plan_and_keep_its_ledger(self) -> None:
        first = _call(action="plan", source="arXiv:2401.00001", pages=300, outline=_OUTLINE)
        _call(action="mark", plan_id=first["plan_id"], chunk=1, state="covered")
        again = _call(action="plan", source="arXiv:2401.00001", pages=300, outline=list(reversed(_OUTLINE)))
        self.assertEqual(again["status"], "existing")
        self.assertEqual(again["plan_id"], first["plan_id"])
        self.assertEqual(again["ledger_summary"]["covered"], [1])
        self.assertEqual(again["plan"]["created_at"], first["plan"]["created_at"])

    def test_different_parameters_yield_a_different_plan(self) -> None:
        base = _call(action="plan", source="arXiv:2401.00001", pages=300)
        for args in ({"pages": 301}, {"pages": 300, "budget_chars": 50_000}, {"pages": 300, "lines": 6_000}, {"pages": 300, "chars": 480_000}):
            with self.subTest(args=args):
                other = _call(action="plan", source="arXiv:2401.00001", **args)
                self.assertEqual(other["status"], "planned")
                self.assertNotEqual(other["plan_id"], base["plan_id"])

    def test_the_plan_is_written_atomically_under_documents(self) -> None:
        payload = _call(action="plan", source="arXiv:2401.00001", pages=300)
        path = self.omh_home / "documents" / payload["plan_id"] / "plan.json"
        self.assertEqual(Path(payload["plan_path"]), path)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["plan_id"], payload["plan_id"])
        self.assertEqual([p.name for p in path.parent.iterdir()], ["plan.json"])


class SourceFingerprintTests(_HomeCase):
    def test_a_local_file_contributes_only_its_size_and_hash(self) -> None:
        document = self.root / "paper.pdf"
        document.write_bytes(b"%PDF-1.7 not really parsed " * 100)
        payload = _call(action="plan", source=str(document), pages=3)
        source = payload["plan"]["source"]
        self.assertEqual(source["kind"], "file")
        self.assertEqual(source["size_bytes"], document.stat().st_size)
        self.assertEqual(source["sha256"], hashlib.sha256(document.read_bytes()).hexdigest())
        self.assertEqual(source["fingerprint_basis"], "sha256")
        self.assertEqual(payload["plan"]["provenance"]["size_bytes"], "observed")
        self.assertEqual(payload["plan"]["provenance"]["sha256"], "observed")

    def test_the_hash_ties_the_plan_to_one_document(self) -> None:
        document = self.root / "paper.pdf"
        document.write_bytes(b"version one")
        first = _call(action="plan", source=str(document), pages=3)
        document.write_bytes(b"version two")
        second = _call(action="plan", source=str(document), pages=3)
        self.assertNotEqual(first["plan_id"], second["plan_id"])

    def test_a_label_is_recorded_as_given(self) -> None:
        payload = _call(action="plan", source="Attention Is All You Need", pages=15)
        source = payload["plan"]["source"]
        self.assertEqual(source, {
            "label": "Attention Is All You Need",
            "kind": "label",
            "size_bytes": None,
            "sha256": "",
            "fingerprint_basis": "label",
        })

    def test_credential_shaped_names_are_never_hashed(self) -> None:
        secret = self.root / "auth.json"
        secret.write_text('{"token": "hunter2"}', encoding="utf-8")
        payload = _call(action="plan", source=str(secret), pages=1)
        source = payload["plan"]["source"]
        self.assertEqual(source["fingerprint_basis"], "refused_sensitive_name")
        self.assertEqual(source["sha256"], "")

    def test_variable_references_in_the_source_resolve_to_no_file(self) -> None:
        payload = _call(action="plan", source="$HOME/paper.pdf", pages=1)
        self.assertEqual(payload["plan"]["source"]["kind"], "label")


class HomeBindingTests(_HomeCase):
    def test_a_binding_failure_is_json_and_leaks_no_path(self) -> None:
        with mock.patch.object(runtime_paths, "default_omh_home", side_effect=runtime_paths.RuntimeBindingError("PRIVATE_PATH")):
            payload = _call(action="plan", source="paper.pdf", pages=3)
        self.assertEqual(payload["status"], "invalid_home")
        self.assertIn("error", payload)
        self.assertNotIn("PRIVATE_PATH", json.dumps(payload))
        self.assertFalse((self.omh_home / "documents").exists())

    def test_mutations_refuse_a_caller_chosen_home(self) -> None:
        elsewhere = self.root / "elsewhere"
        for args in ({"action": "plan", "source": "paper.pdf", "pages": 3}, {"action": "mark", "plan_id": "0123456789ab", "chunk": 1, "state": "covered"}):
            with self.subTest(action=args["action"]):
                payload = _call(omh_home=str(elsewhere), **args)
                self.assertEqual(payload["status"], "invalid_home")
        self.assertFalse(elsewhere.exists())

    def test_show_accepts_a_standalone_home_override(self) -> None:
        planned = _call(action="plan", source="paper.pdf", pages=3)
        payload = _call(action="show", plan_id=planned["plan_id"], omh_home=str(self.omh_home))
        self.assertEqual(payload["status"], "read")

    def test_a_symlinked_documents_root_is_refused(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        self.omh_home.mkdir()
        try:
            (self.omh_home / "documents").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {type(exc).__name__}")
        payload = _call(action="plan", source="paper.pdf", pages=3)
        self.assertEqual(payload["status"], "invalid_plan")
        self.assertIn("symlinked", payload["error"])
        self.assertEqual(list(outside.iterdir()), [])

    def test_an_unknown_action_is_refused(self) -> None:
        payload = _call(action="delete", plan_id="0123456789ab")
        self.assertEqual(payload["status"], "invalid_action")


class PlanFileValidationTests(unittest.TestCase):
    """The shape a later consumer (a fanout planner, a wrapper) may rely on."""

    def _plan(self) -> dict:
        request = planner.normalize_plan_request({"source": "paper.pdf", "pages": 300})
        return planner.build_document_plan(request, planner.fingerprint_source("paper.pdf", None), now="2026-09-13T00:00:00Z")

    def test_a_built_plan_validates(self) -> None:
        plan = self._plan()
        self.assertIs(planner.validate_document_plan(plan), plan)
        self.assertEqual(plan["created_at"], "2026-09-13T00:00:00Z")

    def test_a_plan_without_ranges_or_with_a_broken_ledger_is_refused(self) -> None:
        for mutate, fragment in (
            (lambda plan: plan.__setitem__("ranges", []), "no ranges"),
            (lambda plan: plan.__setitem__("range_count", 2), "range_count disagrees"),
            (lambda plan: plan["ledger"].pop("3"), "ledger does not cover"),
            (lambda plan: plan["ledger"]["1"].__setitem__("state", "done"), "no valid state"),
            (lambda plan: plan.__setitem__("schema_version", "document_chunk_plan/v0"), "schema_version"),
            (lambda plan: plan["ranges"][0].pop("read_window"), "no read window"),
        ):
            plan = self._plan()
            mutate(plan)
            with self.subTest(fragment=fragment), self.assertRaises(planner.DocumentPlanError) as caught:
                planner.validate_document_plan(plan)
            self.assertIn(fragment, str(caught.exception))


if __name__ == "__main__":
    unittest.main()

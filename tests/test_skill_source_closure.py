"""Gate for the recurring-watch closure contract (issue #1543).

`docs/SKILL-SOURCES.md` has always required that resolving an upstream-tracker
finding advances `reviewed_ref` and `reviewed_on` in the same pull request.
These tests are what makes that mechanical. Every named failure is pinned by
its stable reason code, because CI output and maintainer greps read the code,
not the sentence next to it.

Fixtures build their own registry and their own enrolment baseline rather than
leaning on the 38 live rows, whose checkpoints move whenever a real review
lands. Two tests deliberately read the real repository: one proves the shipped
registry still parses into unambiguous candidates, the other proves the shipped
enrolment matches it row for row.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
from omh.catalogs.skill_source_closure import (
    CLOSURE_SCHEMA,
    DISPOSITIONS,
    FAILURE_CLASSES,
    MAX_RATIONALE_CHARS,
    PASS_REASONS,
    RECEIPT_SCHEMA,
    ROW_STATES,
    PreReceiptBaseline,
    pre_receipt_baselines,
)
from omh.maintenance.skill_source_closure import (
    candidate_key,
    format_skill_source_closure,
    parse_registry,
    skill_source_closure_report,
    unresolved_watch_candidates,
    watch_scan_plan,
)


REPO_ROOT = Path(__file__).resolve().parents[1]

UNIT = "demo-skill"
SOURCE = "https://github.com/example/demo"
KEY = candidate_key(UNIT, SOURCE)
OTHER_UNIT = "other-skill"
OTHER_SOURCE = "https://github.com/example/other"
OTHER_KEY = candidate_key(OTHER_UNIT, OTHER_SOURCE)

BASE_REF = "aaaaaaaaaaaa"
NEXT_REF = "bbbbbbbbbbbb"
THIRD_REF = "cccccccccccc"
BASE_ON = "2026-09-01"
NEXT_ON = "2026-09-10"
THIRD_ON = "2026-09-12"

BASELINE = (
    PreReceiptBaseline(KEY, BASE_ON, BASE_REF, "Fixture row enrolled before receipts."),
    PreReceiptBaseline(OTHER_KEY, BASE_ON, BASE_REF, "Fixture sibling row enrolled before receipts."),
)


def registry_markdown(rows: list[tuple[str, str, str, str]]) -> str:
    """Render a registry table in the shape the real document uses."""
    header = (
        "# Skill Upstream Sources\n\n## Shipped skills\n\n"
        "| OMH skill | Category | Upstream repo | Paths studied | License | reviewed_on | reviewed_ref |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
    )
    body = "".join(
        f"| `{unit}` (PR #1) | planning | {source} | `README.md` | MIT | {reviewed_on} | {reviewed_ref} |\n"
        for unit, source, reviewed_on, reviewed_ref in rows
    )
    return header + body


def receipt(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "receipt_id": "ssc-2026-09-10-demo-skill-example-demo",
        "candidate_key": KEY,
        "prior_checkpoint": BASE_REF,
        "next_checkpoint": NEXT_REF,
        "disposition": "adopted",
        "decision_ref": "#1543",
        "reviewed_on": NEXT_ON,
        "rationale": "Folded the reviewed range into the OMH skill contract.",
    }
    base.update(changes)
    return base


class ClosureFixture(unittest.TestCase):
    """Builds a throwaway repository root holding a registry and a ledger."""

    def report(
        self,
        *,
        rows: list[tuple[str, str, str, str]] | None = None,
        receipts: list[dict[str, object]] | None = None,
        ledger_text: str | None = None,
        registry_text: str | None = None,
        baselines: tuple[PreReceiptBaseline, ...] | None = BASELINE,
    ):
        default_rows = [(UNIT, SOURCE, BASE_ON, BASE_REF), (OTHER_UNIT, OTHER_SOURCE, BASE_ON, BASE_REF)]
        with TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "docs").mkdir()
            (root / "docs" / "SKILL-SOURCES.md").write_text(
                registry_text if registry_text is not None else registry_markdown(rows if rows is not None else default_rows),
                encoding="utf-8",
            )
            if ledger_text is None:
                ledger_text = json.dumps(
                    {"schema_version": RECEIPT_SCHEMA, "receipts": receipts or []}, indent=2,
                )
            (root / "docs" / "skill-source-receipts.json").write_text(ledger_text, encoding="utf-8")
            return skill_source_closure_report(root=root, baselines=baselines)

    def codes(self, report) -> list[str]:
        return [finding["failure_class"] for finding in report["findings"]]

    def row(self, report, key: str = KEY):
        return next(row for row in report["rows"] if row["candidate_key"] == key)


class ClosureContractTests(ClosureFixture):
    def test_pre_receipt_rows_are_not_applicable_and_never_fabricate_a_receipt(self):
        # The migration path: a row that predates the contract passes, carries a
        # named pass reason, and still hands the next run a starting boundary.
        report = self.report()

        self.assertTrue(report["ok"], report["findings"])
        self.assertEqual(self.row(report)["state"], "not_applicable")
        self.assertEqual(self.row(report)["reason"], "pre_receipt_baseline")
        self.assertEqual(self.row(report)["receipt_ids"], [])
        self.assertEqual(self.row(report)["scan_from"], BASE_REF)
        self.assertEqual(report["summary"]["receipts"], 0)

    def test_resolved_finding_without_the_row_update_fails_closure_checkpoint_missing(self):
        # The defect the issue names: implementation and receipt landed, the
        # matching continuity row did not move.
        report = self.report(receipts=[receipt()])

        self.assertFalse(report["ok"])
        self.assertIn("closure_checkpoint_missing", self.codes(report))
        self.assertEqual(self.row(report)["state"], "held")
        self.assertEqual(self.row(report)["reason"], "closure_checkpoint_missing")
        self.assertIsNone(self.row(report)["scan_from"])

    def test_checkpoint_advancing_without_a_receipt_fails_closure_receipt_missing(self):
        # The mirror defect: the row moved and nothing records the decision.
        report = self.report(
            rows=[(UNIT, SOURCE, NEXT_ON, NEXT_REF), (OTHER_UNIT, OTHER_SOURCE, BASE_ON, BASE_REF)],
        )

        self.assertFalse(report["ok"])
        self.assertIn("closure_receipt_missing", self.codes(report))
        self.assertEqual(self.row(report)["reason"], "closure_receipt_missing")

    def test_each_disposition_closes_the_row_and_records_the_next_boundary(self):
        for disposition in DISPOSITIONS:
            with self.subTest(disposition=disposition):
                report = self.report(
                    rows=[(UNIT, SOURCE, NEXT_ON, NEXT_REF), (OTHER_UNIT, OTHER_SOURCE, BASE_ON, BASE_REF)],
                    receipts=[receipt(disposition=disposition)],
                )

                self.assertTrue(report["ok"], report["findings"])
                self.assertEqual(self.row(report)["state"], "closed")
                self.assertEqual(self.row(report)["reason"], "receipt_chain_settled")
                self.assertEqual(self.row(report)["disposition"], disposition)
                self.assertEqual(self.row(report)["scan_from"], NEXT_REF)

    def test_a_new_row_enrols_through_an_initial_receipt_with_no_prior(self):
        # The other enrolment path: a source shipping for the first time has no
        # prior checkpoint, so its first review is itself the terminal receipt.
        report = self.report(
            rows=[(UNIT, SOURCE, BASE_ON, BASE_REF), (OTHER_UNIT, OTHER_SOURCE, NEXT_ON, NEXT_REF)],
            receipts=[receipt(
                receipt_id="ssc-2026-09-10-other-skill-initial", candidate_key=OTHER_KEY,
                prior_checkpoint=None, rationale="First review of a newly shipped source.",
            )],
            baselines=(BASELINE[0],),
        )

        self.assertTrue(report["ok"], report["findings"])
        self.assertEqual(self.row(report, OTHER_KEY)["state"], "closed")
        self.assertEqual(self.row(report, OTHER_KEY)["scan_from"], NEXT_REF)

    def test_review_date_must_move_with_the_checkpoint(self):
        # Half the row is not the row: advancing `reviewed_ref` while leaving
        # `reviewed_on` behind is the same unclosed range with a newer hash.
        report = self.report(
            rows=[(UNIT, SOURCE, BASE_ON, NEXT_REF), (OTHER_UNIT, OTHER_SOURCE, BASE_ON, BASE_REF)],
            receipts=[receipt()],
        )

        self.assertIn("closure_checkpoint_missing", self.codes(report))

    def test_stale_prior_checkpoint_fails_deterministically(self):
        report = self.report(
            rows=[(UNIT, SOURCE, NEXT_ON, NEXT_REF), (OTHER_UNIT, OTHER_SOURCE, BASE_ON, BASE_REF)],
            receipts=[receipt(prior_checkpoint="ffffffffffff")],
        )

        self.assertIn("checkpoint_prior_stale", self.codes(report))
        self.assertEqual(self.row(report)["reason"], "checkpoint_prior_stale")

    def test_non_superseding_next_checkpoint_fails(self):
        # Returning to a checkpoint the chain already left re-opens a reviewed
        # range. A correction is allowed, but it has to say so.
        report = self.report(
            rows=[(UNIT, SOURCE, THIRD_ON, BASE_REF), (OTHER_UNIT, OTHER_SOURCE, BASE_ON, BASE_REF)],
            receipts=[
                receipt(),
                receipt(
                    receipt_id="ssc-2026-09-12-demo-skill-rollback",
                    prior_checkpoint=NEXT_REF, next_checkpoint=BASE_REF, reviewed_on=THIRD_ON,
                ),
            ],
        )

        self.assertIn("checkpoint_not_superseding", self.codes(report))

    def test_a_declared_correction_supersedes_without_rewriting_the_earlier_receipt(self):
        # Append-only: the superseded receipt stays in the chain and stays
        # listed on the row.
        report = self.report(
            rows=[(UNIT, SOURCE, THIRD_ON, THIRD_REF), (OTHER_UNIT, OTHER_SOURCE, BASE_ON, BASE_REF)],
            receipts=[
                receipt(),
                receipt(
                    receipt_id="ssc-2026-09-12-demo-skill-correction",
                    prior_checkpoint=BASE_REF, next_checkpoint=THIRD_REF, reviewed_on=THIRD_ON,
                    disposition="rejected", supersedes="ssc-2026-09-10-demo-skill-example-demo",
                    rationale="Correction: the earlier adoption was reversed on review.",
                ),
            ],
        )

        self.assertTrue(report["ok"], report["findings"])
        self.assertEqual(self.row(report)["state"], "closed")
        self.assertEqual(
            self.row(report)["receipt_ids"],
            ["ssc-2026-09-10-demo-skill-example-demo", "ssc-2026-09-12-demo-skill-correction"],
        )

    def test_reused_receipt_identity_fails(self):
        report = self.report(
            rows=[(UNIT, SOURCE, NEXT_ON, NEXT_REF), (OTHER_UNIT, OTHER_SOURCE, BASE_ON, BASE_REF)],
            receipts=[receipt(), receipt(candidate_key=OTHER_KEY)],
        )

        self.assertIn("receipt_id_reused", self.codes(report))

    def test_supersede_reference_must_exist_and_must_stay_on_its_own_candidate(self):
        unrelated = self.report(
            rows=[(UNIT, SOURCE, NEXT_ON, NEXT_REF), (OTHER_UNIT, OTHER_SOURCE, NEXT_ON, NEXT_REF)],
            receipts=[
                receipt(),
                receipt(
                    receipt_id="ssc-2026-09-12-other-skill", candidate_key=OTHER_KEY,
                    reviewed_on=THIRD_ON, supersedes="ssc-2026-09-10-demo-skill-example-demo",
                ),
            ],
        )
        missing = self.report(
            rows=[(UNIT, SOURCE, NEXT_ON, NEXT_REF), (OTHER_UNIT, OTHER_SOURCE, BASE_ON, BASE_REF)],
            receipts=[receipt(supersedes="ssc-2026-01-01-absent")],
        )

        self.assertIn("supersede_reference_unrelated", self.codes(unrelated))
        self.assertIn("supersede_reference_unknown", self.codes(missing))

    def test_ambiguous_and_unmatched_candidate_keys_fail(self):
        ambiguous = self.report(
            rows=[(UNIT, SOURCE, BASE_ON, BASE_REF), (UNIT, SOURCE + "/", BASE_ON, BASE_REF)],
            baselines=(BASELINE[0],),
        )
        unmatched = self.report(receipts=[receipt(candidate_key="ghost@github.com/example/ghost")])

        self.assertIn("ambiguous_candidate_match", self.codes(ambiguous))
        self.assertEqual(self.row(ambiguous)["reason"], "ambiguous_candidate_match")
        self.assertIn("unmatched_candidate_key", self.codes(unmatched))

    def test_unenrolled_and_stale_baseline_rows_fail(self):
        unenrolled = self.report(baselines=(BASELINE[0],))
        stale = self.report(
            rows=[(UNIT, SOURCE, BASE_ON, BASE_REF)],
            baselines=BASELINE,
        )

        self.assertIn("unenrolled_registry_row", self.codes(unenrolled))
        self.assertEqual(self.row(unenrolled, OTHER_KEY)["reason"], "unenrolled_registry_row")
        self.assertIn("stale_baseline_entry", self.codes(stale))

    def test_ledger_entries_are_appended_in_review_order(self):
        report = self.report(
            rows=[(UNIT, SOURCE, BASE_ON, BASE_REF), (OTHER_UNIT, OTHER_SOURCE, BASE_ON, BASE_REF)],
            receipts=[
                receipt(reviewed_on=THIRD_ON),
                receipt(receipt_id="ssc-2026-09-10-earlier", reviewed_on=NEXT_ON, candidate_key=OTHER_KEY),
            ],
        )

        self.assertIn("ledger_order_violation", self.codes(report))

    def test_malformed_receipt_fields_fail_before_any_chain_reasoning(self):
        cases = {
            "disposition": receipt(disposition="folded"),
            "decision_ref": receipt(decision_ref="PR 1543"),
            "reviewed_on": receipt(reviewed_on="10 September 2026"),
            "receipt_id": receipt(receipt_id="SSC_Demo"),
            "next_checkpoint": receipt(next_checkpoint=""),
            "unknown_field": receipt(watch_evidence="upstream diff"),
        }
        for name, entry in cases.items():
            with self.subTest(field=name):
                report = self.report(receipts=[entry])

                self.assertIn("receipt_field_invalid", self.codes(report))

    def test_rationale_stays_bounded_and_keeps_watch_evidence_out_of_the_public_ledger(self):
        cases = {
            "too_long": receipt(rationale="x" * (MAX_RATIONALE_CHARS + 1)),
            "multi_line": receipt(rationale="Adopted.\nUpstream diff attached."),
            "link": receipt(rationale="Adopted per https://github.com/example/demo/commit/abc"),
        }
        for name, entry in cases.items():
            with self.subTest(case=name):
                report = self.report(receipts=[entry])

                self.assertIn("rationale_over_budget", self.codes(report))

    def test_unreadable_or_undeclared_ledger_fails(self):
        broken = self.report(ledger_text="{not json")
        undeclared = self.report(ledger_text=json.dumps({"receipts": []}))

        self.assertIn("ledger_unreadable", self.codes(broken))
        self.assertIn("ledger_unreadable", self.codes(undeclared))

    def test_unparsable_registry_row_is_reported_rather_than_skipped(self):
        text = registry_markdown([(UNIT, SOURCE, BASE_ON, BASE_REF)]) + "| `broken` | only | three |\n"

        report = self.report(registry_text=text, baselines=(BASELINE[0],))

        self.assertIn("registry_row_unparsed", self.codes(report))


class WatchBoundaryTests(ClosureFixture):
    def test_a_later_sweep_does_not_re_emit_a_candidate_resolved_at_that_checkpoint(self):
        # Success criterion: the next run starts after the accepted checkpoint.
        report = self.report(
            rows=[(UNIT, SOURCE, NEXT_ON, NEXT_REF), (OTHER_UNIT, OTHER_SOURCE, BASE_ON, BASE_REF)],
            receipts=[receipt()],
        )
        observed = [
            {"candidate_key": KEY, "observed_ref": NEXT_REF},
            {"candidate_key": OTHER_KEY, "observed_ref": BASE_REF},
        ]

        self.assertTrue(report["ok"], report["findings"])
        self.assertEqual(watch_scan_plan(report)[KEY]["scan_from"], NEXT_REF)
        self.assertEqual(unresolved_watch_candidates(report, observed), [])

    def test_a_moved_upstream_and_a_held_row_both_stay_in_the_sweep(self):
        report = self.report(receipts=[receipt()])
        observed = [
            {"candidate_key": KEY, "observed_ref": NEXT_REF},
            {"candidate_key": OTHER_KEY, "observed_ref": THIRD_REF},
        ]

        # KEY is held, so its finding must keep being emitted; OTHER_KEY moved.
        self.assertEqual(
            [item["candidate_key"] for item in unresolved_watch_candidates(report, observed)],
            [KEY, OTHER_KEY],
        )


class ShippedRegistryTests(unittest.TestCase):
    def test_the_shipped_registry_parses_into_unambiguous_candidates(self):
        rows, findings = parse_registry((REPO_ROOT / "docs" / "SKILL-SOURCES.md").read_text(encoding="utf-8"))
        keys = [row["candidate_key"] for row in rows]

        self.assertEqual(findings, [])
        self.assertEqual(len(keys), len(set(keys)), "one candidate key per registry row")

    def test_the_shipped_enrolment_matches_the_shipped_registry_row_for_row(self):
        # Enrolment is not a blanket exemption: each baseline names one live row
        # at the exact state it stood in, so the first move needs a receipt.
        rows = {row["candidate_key"]: row for row in parse_registry(
            (REPO_ROOT / "docs" / "SKILL-SOURCES.md").read_text(encoding="utf-8"))[0]}
        baselines = {entry.candidate_key: entry for entry in pre_receipt_baselines()}

        self.assertEqual(set(baselines), set(rows))
        for key, entry in baselines.items():
            with self.subTest(candidate=key):
                self.assertEqual((entry.reviewed_on, entry.reviewed_ref), (rows[key]["reviewed_on"], rows[key]["checkpoint"]))
                self.assertTrue(entry.reason, "every enrolment carries a reason")

    def test_the_repository_passes_its_own_closure_gate(self):
        report = skill_source_closure_report(root=REPO_ROOT)

        self.assertTrue(report["ok"], report["findings"])
        self.assertEqual(report["schema_version"], CLOSURE_SCHEMA)
        self.assertEqual(report["summary"]["held"], 0)
        self.assertFalse(report["bounds"]["network"])
        self.assertFalse(report["bounds"]["subprocess"])

    def test_the_registry_documents_the_atomic_rule_and_the_migration_path(self):
        registry = (REPO_ROOT / "docs" / "SKILL-SOURCES.md").read_text(encoding="utf-8")

        for anchor in ("docs/skill-source-receipts.json", "docs skill-sources --check", "pre_receipt_baselines"):
            with self.subTest(anchor=anchor):
                self.assertTrue(anchor in registry, f"{anchor} is not documented in the registry")


class ClosureVocabularyTests(ClosureFixture):
    def test_every_emitted_reason_code_is_declared_in_the_vocabulary(self):
        # The codes are the contract. A finding carrying an undeclared class
        # would be unsearchable, so the declaration is what gates a new one.
        report = self.report(receipts=[receipt(disposition="folded")])
        declared = set(FAILURE_CLASSES) | set(PASS_REASONS)

        self.assertEqual(set(report["failure_classes"]), set(FAILURE_CLASSES))
        for finding in report["findings"]:
            with self.subTest(code=finding["failure_class"]):
                self.assertIn(finding["failure_class"], FAILURE_CLASSES)
        for row in report["rows"]:
            with self.subTest(row=row["candidate_key"]):
                self.assertIn(row["state"], ROW_STATES)
                self.assertIn(row["reason"], declared)

    def test_findings_are_deterministically_ordered_and_diagnostics_are_bounded(self):
        report = self.report(
            rows=[(UNIT, SOURCE, NEXT_ON, NEXT_REF), (OTHER_UNIT, OTHER_SOURCE, THIRD_ON, THIRD_REF)],
            receipts=[receipt(prior_checkpoint="ffffffffffff")],
        )
        codes = self.codes(report)

        self.assertEqual(codes, sorted(codes))
        for finding in report["findings"]:
            with self.subTest(code=finding["failure_class"]):
                self.assertNotIn("\n", finding["detail"])

    def test_the_text_report_names_every_state_and_the_boundary(self):
        rendered = format_skill_source_closure(self.report())

        self.assertIn("Skill-source closure: PASS", rendered)
        self.assertIn("not_applicable", rendered)
        self.assertIn("never fetches a watched repository", rendered)


class ClosureCommandTests(unittest.TestCase):
    def test_the_check_command_passes_on_the_repository_and_prints_the_payload(self):
        status, stdout, _ = run_cli(["docs", "skill-sources", "--check", "--json", "--root", str(REPO_ROOT)])
        payload = json.loads(stdout)

        self.assertEqual(status, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schema_version"], CLOSURE_SCHEMA)
        self.assertEqual(payload["registry"], "docs/SKILL-SOURCES.md")
        self.assertEqual(payload["ledger"], "docs/skill-source-receipts.json")

    def test_the_check_command_exits_one_on_a_broken_tree_and_never_reports_success(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "docs").mkdir()
            (root / "docs" / "SKILL-SOURCES.md").write_text(
                registry_markdown([(UNIT, SOURCE, NEXT_ON, NEXT_REF)]), encoding="utf-8",
            )
            (root / "docs" / "skill-source-receipts.json").write_text(
                json.dumps({"schema_version": RECEIPT_SCHEMA, "receipts": []}), encoding="utf-8",
            )
            status, stdout, _ = run_cli(["docs", "skill-sources", "--check", "--json", "--root", str(root)])
            payload = json.loads(stdout)

        self.assertEqual(status, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("unenrolled_registry_row", [item["failure_class"] for item in payload["findings"]])

    def test_without_check_the_command_reports_findings_and_still_exits_zero(self):
        # The report is readable outside CI. Only `--check` is the gate, so a
        # maintainer can list held rows without a non-zero status.
        with TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "docs").mkdir()
            (root / "docs" / "SKILL-SOURCES.md").write_text(
                registry_markdown([(UNIT, SOURCE, NEXT_ON, NEXT_REF)]), encoding="utf-8",
            )
            (root / "docs" / "skill-source-receipts.json").write_text(
                json.dumps({"schema_version": RECEIPT_SCHEMA, "receipts": []}), encoding="utf-8",
            )
            status, stdout, _ = run_cli(["docs", "skill-sources", "--root", str(root)], output_json=False)

        self.assertEqual(status, 0)
        self.assertIn("NEEDS ATTENTION", stdout)
        self.assertIn("unenrolled_registry_row", stdout)


if __name__ == "__main__":
    _ = unittest.main()

"""#1571: the shipped promotion lifecycle reaches a reviewed skill draft.

Every case here drives the SAME functions the browser lane drives. If one of
them had been forked for drafts, the browser regression pin below and these
cases would stop failing together, which is the point of keeping them in one
file: the evidence that a draft installs and the evidence that a trace still
installs come from one implementation or they are not this issue's evidence.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from _local_package import load_local_package

load_local_package()

from test_browser_skill_promotion import Host
from test_browser_skill_promotion_plan import _project as project, _passing_trace as passing_trace

from omh.paths import OmhPaths
from omh.workflows.browser_skill_promotion import (
    BrowserSkillPromotionError, approve_browser_skill_lifecycle, approve_browser_skill_removal,
    approve_browser_skill_rollback, browser_skill_promotion_status, promote_approved_browser_skill,
    review_browser_skill_lifecycle, review_browser_skill_removal, review_browser_skill_rollback,
)
from omh.workflows.browser_skill_promotion_plan import (
    BrowserSkillPromotionPlanError, build_skill_promotion_plan,
)
from omh.workflows.skill_draft import build_skill_draft, review_skill_draft, write_skill_draft
from omh.workflows.skill_promotion_source import (
    BROWSER_TRACE_SOURCE, ENTRY_METADATA_LINE_KEYS, SKILL_DRAFT_SOURCE,
    SkillPromotionSourceError, parse_promotion_entry_metadata, promotion_source_kind,
    promotion_source_lock_parts, skill_draft_digest,
)
from omh.commands.browser_skill_promotion import _skill_promotion_exit_code


SKILL_NAME = "receipt-reconciliation"


def _draft_record(root: Path, *, instructions: list[str] | None = None, name: str = SKILL_NAME) -> dict[str, Any]:
    """Write one reviewed, activated draft into this project's own draft store."""
    paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes-home")
    draft = build_skill_draft(
        f"turn this into a skill: reconcile the monthly receipts the way we did in run one, as {name}",
        source_runs=["run-000000000001"],
        proposed_skill_name=name,
        fixed_instructions=instructions or [
            "Open the reconciliation sheet named by the request.",
            "Compare every recorded total against the statement line for the same day.",
        ],
        declared_inputs=[{"name": "period", "description": "The month to reconcile."}],
        preconditions=["The statement for the period is already exported locally."],
        stop_conditions=["Stop when a total has no matching statement line."],
        verification_steps=["Re-read the reconciled sheet and confirm every row is matched."],
        created_at="2026-09-15T00:00:00Z",
    )
    assert draft is not None
    approved = review_skill_draft(draft, decision="approve", reviewer_ref="operator", reviewed_at="2026-09-15T00:01:00Z")
    return write_skill_draft(paths, approved)


def _promote(root: Path, source_id: str, host: Host, *, skill_name: str = SKILL_NAME) -> dict[str, Any]:
    review = review_browser_skill_lifecycle(root, source_id, skill_name, host=host)
    plan = review["plan"]
    assert isinstance(plan, dict)
    receipt = approve_browser_skill_lifecycle(
        root, source_id, skill_name,
        reviewed_diff_digest=str(plan["diff_digest"]), reviewer_identity="operator", host=host,
    )
    return promote_approved_browser_skill(root, str(receipt["receipt_id"]), host=host)


def _entry_text(root: Path, skill_name: str = SKILL_NAME) -> str:
    return (root / ".hermes" / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")


class SkillDraftPromotionTests(unittest.TestCase):
    def test_a_draft_source_completes_diff_approve_promote_and_rollback(self) -> None:
        with project() as root:
            host = Host()
            first = _draft_record(root)
            first_id = str(first["draft_id"])

            status = _promote(root, first_id, host)
            self.assertEqual(status["status"], "active")
            first_generation = str(status["generation"])
            first_entry = _entry_text(root)

            # A second reviewed draft over the active skill is an update, which
            # is the lifecycle's own decision, not a flag this test passes in.
            second = _draft_record(
                root,
                instructions=[
                    "Open the reconciliation sheet named by the request.",
                    "Compare every recorded total against the statement line, then the running balance.",
                ],
            )
            second_id = str(second["draft_id"])
            self.assertNotEqual(second_id, first_id)
            updated = _promote(root, second_id, host)
            self.assertEqual(updated["status"], "active")
            second_generation = str(updated["generation"])
            self.assertNotEqual(second_generation, first_generation)
            self.assertNotEqual(_entry_text(root), first_entry)

            review = review_browser_skill_rollback(root, SKILL_NAME, first_generation, host=host)
            plan = review["plan"]
            assert isinstance(plan, dict)
            receipt = approve_browser_skill_rollback(
                root, SKILL_NAME, first_generation,
                reviewed_diff_digest=str(plan["diff_digest"]), reviewer_identity="operator", host=host,
            )
            rolled = promote_approved_browser_skill(root, str(receipt["receipt_id"]), host=host)

            # The proof is the files on disk, not the command's own report. A
            # rollback commits a new generation whose content comes from the
            # retained one, so what must hold is that the installed entry now
            # names the first draft, records what it rolled back to, and that
            # the procedure it points at is byte-identical to the retained
            # first generation's -- the content really reverted.
            self.assertEqual(rolled["status"], "rolled_back")
            installed = parse_promotion_entry_metadata(_entry_text(root))
            resources = root / ".hermes" / "skills" / SKILL_NAME / "resources"
            self.assertEqual(installed["rollback_of"], first_generation)
            self.assertEqual(installed["draft_id"], first_id)
            self.assertEqual(installed["draft_digest"], skill_draft_digest(first))
            self.assertEqual(
                (resources / str(installed["generation"]) / "procedure.md").read_text(encoding="utf-8"),
                (resources / first_generation / "procedure.md").read_text(encoding="utf-8"),
            )
            # The retained generation was never rebuilt to get there.
            self.assertEqual(
                (resources / first_generation / "entry.md").read_text(encoding="utf-8"), first_entry
            )
            self.assertEqual(browser_skill_promotion_status(root, SKILL_NAME)["status"], "active")

    def test_a_draft_sourced_skill_removes_and_reports_its_own_state(self) -> None:
        with project() as root:
            host = Host()
            draft = _draft_record(root)
            _ = _promote(root, str(draft["draft_id"]), host)

            review = review_browser_skill_removal(root, SKILL_NAME, host=host)
            plan = review["plan"]
            assert isinstance(plan, dict)
            receipt = approve_browser_skill_removal(
                root, SKILL_NAME, reviewed_diff_digest=str(plan["diff_digest"]),
                reviewer_identity="operator", host=host,
            )
            removed = promote_approved_browser_skill(root, str(receipt["receipt_id"]), host=host)

            self.assertEqual(removed["status"], "removed")
            self.assertFalse((root / ".hermes" / "skills" / SKILL_NAME / "SKILL.md").exists())
            # Removal keeps the immutable history it was approved against.
            self.assertTrue((root / ".hermes" / "skills" / SKILL_NAME / "resources").is_dir())

    def test_a_withdrawn_draft_review_deactivates_the_entry_it_approved(self) -> None:
        with project() as root:
            host = Host()
            draft = _draft_record(root)
            draft_id = str(draft["draft_id"])
            _ = _promote(root, draft_id, host)
            entry_path = root / ".hermes" / "skills" / SKILL_NAME / "SKILL.md"
            self.assertTrue(entry_path.is_file())

            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes-home")
            withdrawn = review_skill_draft(draft, decision="revise", reviewer_ref="operator", reviewed_at="2026-09-15T01:00:00Z")
            _ = write_skill_draft(paths, withdrawn)

            status = browser_skill_promotion_status(root, SKILL_NAME)

            self.assertEqual(status["status"], "stale")
            self.assertTrue(status["deactivated"])
            self.assertFalse(entry_path.exists())
            self.assertNotEqual(_skill_promotion_exit_code(status), 0)

    def test_a_draft_failing_the_pattern_risk_review_does_not_install_and_names_the_pattern(self) -> None:
        with project() as root:
            host = Host()
            risky = _draft_record(
                root,
                instructions=[
                    "Open the reconciliation sheet named by the request.",
                    "Collect the monthly figures with subprocess.run(['curl', statement_url]).",
                ],
            )

            with self.assertRaises(BrowserSkillPromotionPlanError) as raised:
                _ = review_browser_skill_lifecycle(root, str(risky["draft_id"]), SKILL_NAME, host=host)

            self.assertIn("process_execution", str(raised.exception))
            self.assertFalse((root / ".hermes" / "skills").exists())

    def test_an_unreviewed_draft_is_refused_before_any_package_is_rendered(self) -> None:
        with project() as root:
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes-home")
            pending = build_skill_draft(
                "turn this into a skill: reconcile the monthly receipts",
                source_runs=["run-000000000001"], proposed_skill_name=SKILL_NAME,
                fixed_instructions=["Open the reconciliation sheet named by the request."],
                declared_inputs=[{"name": "period", "description": "The month to reconcile."}],
                preconditions=["The statement for the period is already exported locally."],
                stop_conditions=["Stop when a total has no matching statement line."],
                verification_steps=["Re-read the reconciled sheet and confirm every row is matched."],
                created_at="2026-09-15T00:00:00Z",
            )
            assert pending is not None
            stored = write_skill_draft(paths, pending)

            with self.assertRaises(BrowserSkillPromotionPlanError) as raised:
                _ = build_skill_promotion_plan(root, str(stored["draft_id"]), SKILL_NAME)

            self.assertIn("active proposal", str(raised.exception))

    def test_a_draft_from_another_project_is_not_a_source_for_this_one(self) -> None:
        with project() as first, project() as second:
            draft = _draft_record(first)

            with self.assertRaises(BrowserSkillPromotionPlanError) as raised:
                _ = build_skill_promotion_plan(second, str(draft["draft_id"]), SKILL_NAME)

            self.assertIn("not found", str(raised.exception))

    def test_the_source_lock_names_the_record_the_promotion_actually_reads(self) -> None:
        with project() as root:
            draft = _draft_record(root)
            draft_id = str(draft["draft_id"])

            parts = promotion_source_lock_parts(draft_id)

            self.assertEqual(promotion_source_kind(draft_id), SKILL_DRAFT_SOURCE)
            self.assertTrue(root.joinpath(*parts).is_file())
            self.assertEqual(
                json.loads(root.joinpath(*parts).read_text(encoding="utf-8"))["draft_id"], draft_id
            )

    def test_an_id_of_neither_shape_never_reaches_a_store_lookup(self) -> None:
        for source_id in ("", "bwt-nothex", "sd-short", "../escape", "sd-" + "f" * 21):
            with self.subTest(source_id=source_id):
                with self.assertRaises(SkillPromotionSourceError):
                    _ = promotion_source_kind(source_id)


class BrowserSourceRegressionTests(unittest.TestCase):
    """The pin: widening the source moved no browser byte."""

    def test_a_browser_entry_stores_its_shipped_metadata_and_gains_no_kind_key(self) -> None:
        with project() as root:
            host = Host()
            trace = passing_trace(root)
            status = _promote(root, str(trace["trace_id"]), host, skill_name="checkout-confirmation")
            self.assertEqual(status["status"], "active")

            entry = _entry_text(root, "checkout-confirmation")
            prefix = f"{ENTRY_METADATA_LINE_KEYS[BROWSER_TRACE_SOURCE]}: "
            line = next(line for line in entry.splitlines() if line.startswith(prefix))
            stored = json.loads(line.removeprefix(prefix))

            # `source_kind` is supplied by the reader from the line key that
            # appeared, never written. Storing it would have moved the entry
            # bytes, and through them every activation id and generation.
            self.assertNotIn("source_kind", stored)
            self.assertEqual(stored["schema_version"], "browser_skill_entry/v1")
            self.assertEqual(
                parse_promotion_entry_metadata(entry)["source_kind"], BROWSER_TRACE_SOURCE
            )
            self.assertEqual(
                set(parse_promotion_entry_metadata(entry)) - set(stored), {"source_kind"}
            )

    def test_a_trace_plan_and_a_draft_plan_differ_only_where_the_source_does(self) -> None:
        with project() as root:
            trace = passing_trace(root)
            draft = _draft_record(root)

            trace_plan = build_skill_promotion_plan(root, str(trace["trace_id"]), "checkout-confirmation")
            draft_plan = build_skill_promotion_plan(root, str(draft["draft_id"]), SKILL_NAME)

            trace_source = trace_plan["source"]
            draft_source = draft_plan["source"]
            assert isinstance(trace_source, dict) and isinstance(draft_source, dict)
            self.assertNotIn("source_kind", trace_source)
            self.assertEqual(draft_source["source_kind"], SKILL_DRAFT_SOURCE)
            self.assertEqual(draft_source["draft_digest"], skill_draft_digest(draft))
            self.assertNotIn("pattern_risk_review", trace_plan)
            risk = draft_plan["pattern_risk_review"]
            assert isinstance(risk, dict)
            self.assertEqual(risk["risk_categories"], [])
            # Both kinds stage one complete immutable generation, named by the
            # record the entry actually points at.
            generation = str(draft_plan["generation"])
            draft_package = draft_plan["package"]
            assert isinstance(draft_package, dict)
            self.assertEqual(
                set(draft_package),
                {
                    "SKILL.md",
                    f"resources/{generation}/entry.md",
                    f"resources/{generation}/procedure.md",
                    f"resources/{generation}/draft.json",
                    f"resources/{generation}/manifest.json",
                },
            )

    def test_a_browser_trace_id_still_routes_to_the_browser_lane(self) -> None:
        with project() as root:
            trace = passing_trace(root)

            self.assertEqual(promotion_source_kind(str(trace["trace_id"])), BROWSER_TRACE_SOURCE)
            self.assertEqual(
                promotion_source_lock_parts(str(trace["trace_id"]))[:3],
                (".omh", "web-visual-qa", "traces"),
            )

    def test_a_draft_source_refuses_a_browser_promotion_reference(self) -> None:
        with project() as root:
            trace = passing_trace(root)
            draft = _draft_record(root)
            from omh.workflows.browser_workflow_learning_store import (
                resolved_browser_workflow_promotion_reference,
            )

            reference = resolved_browser_workflow_promotion_reference(root, str(trace["trace_id"]))
            with self.assertRaises(BrowserSkillPromotionPlanError) as raised:
                _ = build_skill_promotion_plan(root, str(draft["draft_id"]), SKILL_NAME, reference=reference)

            self.assertIn("takes no browser promotion reference", str(raised.exception))


class PromotionExitCodeTests(unittest.TestCase):
    def test_a_deactivated_or_unowned_entry_is_never_reported_as_installed(self) -> None:
        for status in ("stale", "quarantined", "unverified_managed_state"):
            with self.subTest(status=status):
                self.assertNotEqual(_skill_promotion_exit_code({"status": status}), 0)

    def test_a_project_that_never_promoted_and_one_that_removed_stay_zero(self) -> None:
        for status in ("inactive", "active", "active_unchecked", "rolled_back", "removed", "already_deactivated"):
            with self.subTest(status=status):
                self.assertEqual(_skill_promotion_exit_code({"status": status}), 0)


class DraftLifecycleRefusalTests(unittest.TestCase):
    def test_an_update_without_an_active_entry_is_refused(self) -> None:
        with project() as root:
            host = Host()
            draft = _draft_record(root)

            with self.assertRaises(BrowserSkillPromotionError):
                _ = review_browser_skill_lifecycle(
                    root, str(draft["draft_id"]), SKILL_NAME, operation="update", host=host
                )


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()

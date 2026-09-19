"""Contracts for `skill_pattern_risk_review/v1` (issue #793).

Grouped by acceptance criterion:

- AC1: every review separates usefulness, risk, confidence, and native
  reproduction guidance. Four required blocks, each independently variable, and
  a review missing one is a validation error naming it.
- AC2: a clean scan cannot mark the source approved or active. Asserted across
  the whole status vocabulary and against the builder's own signature, because
  the claim is that approval is unreachable rather than merely discouraged.
- AC3: every risk category the cited audit reports resolves to a named native
  constraint or an explicit rejection, and an unresolved category is a
  validation error naming the category.

Plus the guards the family exists for: the review never reads as having
executed, imported, or installed the reviewed skill, and a review citing no
audit is refused.

The one test that puts real files on disk writes them with
`omh.local_store.atomic_write_text` rather than `Path.write_text`. The audit it
then cites carries `scanned_byte_count`, that count is inside `review_digest`,
and `Path.write_text` rewrites "\\n" as CRLF on Windows -- so a fixture written
that way would produce a different digest on the Windows job than on this one.
Every digest comparison in this file is between two payloads built in the same
run for the same reason.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import tempfile
import unittest
from typing import Any

from _local_package import load_local_package

load_local_package()
from omh.local_store import atomic_write_text  # noqa: E402
from omh.workflows.plugin_risk_audit import (  # noqa: E402
    PLUGIN_RISK_AUDIT_SCHEMA_VERSION,
    audit_plugin_risk,
)
from omh.workflows.skill_pattern_risk_review import (  # noqa: E402
    AUDIT_EVIDENCE_KEYS,
    AUDITED_RISK_CATEGORIES,
    CONFIDENCE_KEYS,
    EXECUTION_CLAIM_KEYS,
    MATERIAL_REVIEW_KEYS,
    NATIVE_CONSTRAINTS,
    NATIVE_REPRODUCTION_KEYS,
    NOT_OBSERVED_SURFACES,
    PROHIBITED_BEHAVIORS,
    REFUSED_REVIEW_STATUSES,
    RESOLUTION_KEYS,
    REVIEW_STATUS_CLAIMS,
    REVIEW_STATUSES,
    REVIEWER_DECISIONS,
    RISK_KEYS,
    RISK_RESOLUTIONS,
    SKILL_PATTERN_RISK_REVIEW_CLAIM_BOUNDARY,
    SKILL_PATTERN_RISK_REVIEW_KEYS,
    SKILL_PATTERN_RISK_REVIEW_SCHEMA_VERSION,
    USEFULNESS_KEYS,
    SkillPatternRiskReviewError,
    audited_risk_categories,
    build_skill_pattern_risk_review,
    cite_plugin_risk_audit,
    derive_review_status,
    record_reviewer_decision,
    review_reads_as_approved,
    review_status_claim,
    reviewer_decision_state,
    skill_pattern_risk_review_digest,
    unresolved_risk_categories,
    validate_skill_pattern_risk_review,
)

from _platform_support import requires_secure_dir_io  # noqa: E402
from _cli_harness import run_cli  # noqa: E402


def audit_payload(*, categories: tuple[str, ...] = ("network_request", "process_execution")) -> dict[str, Any]:
    """A `plugin_risk_audit/v1` payload in the shape `audit_plugin_risk` returns."""
    return {
        "schema_version": PLUGIN_RISK_AUDIT_SCHEMA_VERSION,
        "source": {"explicit_root": True, "manifest_status": "present"},
        "summary": {
            "scanned_file_count": 3,
            "scanned_byte_count": 912,
            "risk_categories": sorted(categories),
            "risk_category_count": len(categories),
        },
        "not_observed": {"plugin_execution": {"status": "not_observed"}},
        "claim_boundary": "The audit statically reads bounded text and proves nothing about safety.",
    }


def review_kwargs(**overrides: Any) -> dict[str, Any]:
    """A complete, valid review's arguments, minimally overridable."""
    base: dict[str, Any] = {
        "skill_ref": "third-party-review-digest",
        "audit": audit_payload(),
        "intended_outcome": (
            "Turn a repository's open pull requests into one reviewable status summary a maintainer can "
            "read in a single pass."
        ),
        "procedure_steps": [
            "Collect the open pull requests for one repository.",
            "Group them by review state and age.",
            "Render one bounded summary line for each group.",
        ],
        "required_authority": ["network_access", "filesystem_read"],
        "required_data": ["repository_source"],
        "side_effects": ["sends_network_requests", "spawns_processes"],
        "risk_resolutions": [
            {
                "category": "network_request",
                "resolution": "rejected",
                "prohibited_behavior": "reach_a_remote_endpoint",
            },
            {
                "category": "process_execution",
                "resolution": "native_constraint",
                "native_constraint": "no_subprocess_execution",
            },
        ],
        "confidence_level": "medium",
        "confidence_basis": ["cited_static_scan"],
        "evidence_limits": ["static_scan_only_no_execution_observed", "runtime_behavior_unknown"],
        "safe_pattern": (
            "Group locally supplied work items by review state and render one bounded summary line per "
            "group."
        ),
        "native_constraints": ["no_subprocess_execution", "no_network_access"],
        "prohibited_behaviors": ["reach_a_remote_endpoint"],
    }
    base.update(overrides)
    return base


def clean_review_kwargs(**overrides: Any) -> dict[str, Any]:
    """A review over a scan that matched no risk category at all."""
    base = review_kwargs(
        audit=audit_payload(categories=()),
        side_effects=["no_side_effects_identified"],
        risk_resolutions=[],
        native_constraints=["deterministic_offline_behavior"],
        prohibited_behaviors=[],
    )
    base.update(overrides)
    return base


class SkillPatternRiskReviewUsefulnessAndRiskTests(unittest.TestCase):
    """AC1: four separated blocks, none of them derived from another."""

    def test_a_review_carries_all_four_blocks_as_separate_fields(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())

        self.assertEqual(review["schema_version"], SKILL_PATTERN_RISK_REVIEW_SCHEMA_VERSION)
        self.assertEqual(sorted(review), sorted(SKILL_PATTERN_RISK_REVIEW_KEYS))
        for block, keys in (
            ("usefulness", USEFULNESS_KEYS),
            ("risk", RISK_KEYS),
            ("confidence", CONFIDENCE_KEYS),
            ("native_reproduction", NATIVE_REPRODUCTION_KEYS),
        ):
            with self.subTest(block=block):
                self.assertEqual(sorted(review[block]), sorted(keys))
        self.assertEqual(validate_skill_pattern_risk_review(review), [])

    def test_a_review_missing_any_one_block_fails_validation_naming_it(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())

        for block in ("usefulness", "risk", "confidence", "native_reproduction"):
            with self.subTest(block=block):
                incomplete = {key: value for key, value in review.items() if key != block}
                errors = validate_skill_pattern_risk_review(incomplete)
                self.assertTrue(any("is missing keys" in error and block in error for error in errors), errors)

    def test_each_block_varies_without_the_others_so_none_is_derived(self) -> None:
        """Four one-block edits, four different digests, four valid reviews."""
        baseline = build_skill_pattern_risk_review(**review_kwargs())
        variants = {
            "usefulness": review_kwargs(required_data=["repository_source", "local_configuration"]),
            "risk": review_kwargs(side_effects=["sends_network_requests"]),
            "confidence": review_kwargs(confidence_level="low"),
            "native_reproduction": review_kwargs(
                native_constraints=["no_subprocess_execution", "no_network_access", "metadata_only_artifacts"]
            ),
        }
        digests = {baseline["review_digest"]}
        for block, kwargs in variants.items():
            with self.subTest(block=block):
                variant = build_skill_pattern_risk_review(**kwargs)
                self.assertEqual(validate_skill_pattern_risk_review(variant), [])
                unchanged = [name for name in variants if name != block]
                for name in unchanged:
                    self.assertEqual(variant[name], baseline[name])
                self.assertNotEqual(variant[block], baseline[block])
                digests.add(variant["review_digest"])
        self.assertEqual(len(digests), len(variants) + 1)

    def test_usefulness_names_the_outcome_the_procedure_and_what_it_needs(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())
        usefulness = review["usefulness"]

        self.assertIn("status summary", usefulness["intended_outcome"])
        self.assertEqual(len(usefulness["procedure_steps"]), 3)
        self.assertEqual(usefulness["required_authority"], ["filesystem_read", "network_access"])
        self.assertEqual(usefulness["required_data"], ["repository_source"])

    def test_usefulness_refuses_an_empty_procedure_or_an_unnamed_authority(self) -> None:
        with self.assertRaisesRegex(SkillPatternRiskReviewError, "procedure_steps must name at least 1"):
            build_skill_pattern_risk_review(**review_kwargs(procedure_steps=[]))
        with self.assertRaisesRegex(SkillPatternRiskReviewError, "required_authority has unsupported"):
            build_skill_pattern_risk_review(**review_kwargs(required_authority=["root"]))
        review = build_skill_pattern_risk_review(**review_kwargs())
        review["usefulness"] = {**review["usefulness"], "required_authority": []}
        self.assertIn(
            "skill pattern risk review usefulness.required_authority must name at least 1",
            validate_skill_pattern_risk_review(review),
        )

    def test_confidence_states_a_level_a_basis_and_what_the_evidence_cannot_show(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())

        self.assertEqual(review["confidence"]["level"], "medium")
        self.assertEqual(review["confidence"]["basis"], ["cited_static_scan"])
        self.assertIn("static_scan_only_no_execution_observed", review["confidence"]["evidence_limits"])
        with self.assertRaisesRegex(SkillPatternRiskReviewError, "confidence.level is unsupported"):
            build_skill_pattern_risk_review(**review_kwargs(confidence_level="certain"))
        with self.assertRaisesRegex(SkillPatternRiskReviewError, "evidence_limits has unsupported"):
            build_skill_pattern_risk_review(**review_kwargs(evidence_limits=["looks_fine"]))

    def test_a_reviewer_may_not_assert_no_side_effects_against_a_dirty_scan(self) -> None:
        with self.assertRaisesRegex(SkillPatternRiskReviewError, "side_effects claims none while"):
            build_skill_pattern_risk_review(**review_kwargs(side_effects=["no_side_effects_identified"]))
        with self.assertRaisesRegex(SkillPatternRiskReviewError, "alongside an identified effect"):
            build_skill_pattern_risk_review(
                **clean_review_kwargs(side_effects=["no_side_effects_identified", "writes_local_files"])
            )
        self.assertEqual(
            build_skill_pattern_risk_review(**clean_review_kwargs())["risk"]["side_effects"],
            ["no_side_effects_identified"],
        )

    def test_side_effects_are_the_reviewers_account_not_the_scanners_categories(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())

        self.assertEqual(review["audit_evidence"]["risk_categories"], ["network_request", "process_execution"])
        self.assertEqual(review["risk"]["side_effects"], ["spawns_processes", "sends_network_requests"])
        self.assertEqual(
            set(review["risk"]["side_effects"]) & set(review["audit_evidence"]["risk_categories"]), set()
        )


class SkillPatternRiskReviewApprovalBoundaryTests(unittest.TestCase):
    """AC2: a clean scan is evidence, and evidence never approves anything."""

    def test_a_clean_scan_with_no_decision_reads_as_awaiting_and_never_as_approved(self) -> None:
        review = build_skill_pattern_risk_review(**clean_review_kwargs())

        self.assertEqual(review["audit_evidence"]["risk_categories"], [])
        self.assertEqual(review["reviewer_decision"], {})
        self.assertEqual(reviewer_decision_state(review), "absent")
        self.assertEqual(review["review_status"], "awaiting_reviewer_decision")
        self.assertFalse(review_reads_as_approved(review))
        self.assertEqual(validate_skill_pattern_risk_review(review), [])

    def test_no_status_in_the_vocabulary_is_reachable_from_a_clean_scan_alone(self) -> None:
        """AC2 across the whole vocabulary, not just against the word 'approved'."""
        review = build_skill_pattern_risk_review(**clean_review_kwargs())

        for status in REVIEW_STATUSES:
            with self.subTest(status=status):
                asserted = {**review, "review_status": status}
                errors = validate_skill_pattern_risk_review(asserted)
                if status == "awaiting_reviewer_decision":
                    self.assertEqual(errors, [])
                    self.assertFalse(review_reads_as_approved(asserted))
                    continue
                self.assertTrue(
                    any("the recorded decision derives" in error for error in errors), (status, errors)
                )
                self.assertFalse(review_reads_as_approved(asserted))

    def test_the_builder_exposes_no_status_argument_at_all(self) -> None:
        """The structural half of AC2: there is nothing to pass."""
        parameters = set(inspect.signature(build_skill_pattern_risk_review).parameters)

        self.assertNotIn("review_status", parameters)
        self.assertNotIn("status", parameters)
        self.assertIn("reviewer_decision", parameters)
        self.assertEqual(
            inspect.signature(build_skill_pattern_risk_review).parameters["reviewer_decision"].default, None
        )

    def test_only_a_recorded_reviewer_decision_reaches_the_approving_status(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())
        decided = record_reviewer_decision(
            review, decided_by="maintainer-01", decision="approve_native_reproduction"
        )

        self.assertEqual(decided["review_status"], "approved_for_native_reproduction")
        self.assertEqual(decided["reviewer_decision"]["decided_by"], "maintainer-01")
        self.assertEqual(decided["reviewer_decision"]["reviewed_digest"], review["review_digest"])
        self.assertEqual(reviewer_decision_state(decided), "current")
        self.assertTrue(review_reads_as_approved(decided))
        self.assertEqual(validate_skill_pattern_risk_review(decided), [])

    def test_a_recorded_rejection_reaches_the_rejecting_status_and_never_approves(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())
        decided = record_reviewer_decision(
            review, decided_by="maintainer-01", decision="reject_native_reproduction"
        )

        self.assertEqual(decided["review_status"], "rejected_for_native_reproduction")
        self.assertFalse(review_reads_as_approved(decided))

    def test_a_decision_that_names_nobody_is_refused(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())

        with self.assertRaisesRegex(SkillPatternRiskReviewError, "decided_by is required"):
            record_reviewer_decision(review, decided_by="", decision="approve_native_reproduction")
        with self.assertRaisesRegex(SkillPatternRiskReviewError, "decision is unsupported"):
            record_reviewer_decision(review, decided_by="maintainer-01", decision="looks_good")

    def test_an_approval_does_not_survive_a_change_to_the_content_it_reviewed(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())
        decided = record_reviewer_decision(
            review, decided_by="maintainer-01", decision="approve_native_reproduction"
        )

        widened = build_skill_pattern_risk_review(
            **review_kwargs(
                native_constraints=["no_subprocess_execution", "no_network_access", "metadata_only_artifacts"],
                reviewer_decision=decided["reviewer_decision"],
            )
        )

        self.assertEqual(reviewer_decision_state(widened), "stale")
        self.assertEqual(widened["review_status"], "superseded_by_content_change")
        self.assertFalse(review_reads_as_approved(widened))
        self.assertEqual(validate_skill_pattern_risk_review(widened), [])

    def test_the_status_vocabulary_refuses_the_words_that_would_approve_the_source(self) -> None:
        review = build_skill_pattern_risk_review(**clean_review_kwargs())

        for word in REFUSED_REVIEW_STATUSES:
            with self.subTest(word=word):
                self.assertNotIn(word, REVIEW_STATUSES)
                errors = validate_skill_pattern_risk_review({**review, "review_status": word})
                self.assertTrue(
                    any("may not describe the source as" in error for error in errors), (word, errors)
                )
                self.assertFalse(review_reads_as_approved({**review, "review_status": word}))

    def test_every_status_sentence_says_the_source_is_not_installed_or_trusted(self) -> None:
        self.assertEqual(sorted(REVIEW_STATUS_CLAIMS), sorted(REVIEW_STATUSES))
        for status in REVIEW_STATUSES:
            with self.subTest(status=status):
                claim = review_status_claim(status)
                self.assertIn("not installed, enabled, or trusted", claim)
        self.assertIn("A clean scan is not an approval", review_status_claim("awaiting_reviewer_decision"))
        self.assertIn("native", review_status_claim("approved_for_native_reproduction"))
        with self.assertRaisesRegex(SkillPatternRiskReviewError, "review_status is unsupported"):
            review_status_claim("approved")

    def test_exactly_one_status_approves_and_it_approves_a_native_pattern(self) -> None:
        approving = [status for status in REVIEW_STATUSES if status.startswith("approved")]

        self.assertEqual(approving, ["approved_for_native_reproduction"])
        self.assertEqual(sorted(REVIEWER_DECISIONS), ["approve_native_reproduction", "reject_native_reproduction"])

    def test_derived_status_is_the_only_definition_of_a_reviews_status(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())
        forged = {
            **review,
            "reviewer_decision": {},
            "review_status": "approved_for_native_reproduction",
        }

        self.assertEqual(derive_review_status(forged), "awaiting_reviewer_decision")
        self.assertFalse(review_reads_as_approved(forged))
        self.assertTrue(
            any("the recorded decision derives" in error for error in validate_skill_pattern_risk_review(forged))
        )


class SkillPatternRiskReviewRiskResolutionTests(unittest.TestCase):
    """AC3: audited risk becomes a named constraint or an explicit rejection."""

    def test_every_audited_category_resolves_to_a_constraint_or_a_rejection(self) -> None:
        review = build_skill_pattern_risk_review(
            **review_kwargs(
                audit=audit_payload(categories=AUDITED_RISK_CATEGORIES),
                side_effects=["sends_network_requests", "spawns_processes", "installs_dependencies"],
                risk_resolutions=[
                    {
                        "category": "declared_dependency",
                        "resolution": "native_constraint",
                        "native_constraint": "no_runtime_dependencies",
                    },
                    {
                        "category": "dynamic_code_execution",
                        "resolution": "rejected",
                        "prohibited_behavior": "execute_evaluated_source",
                    },
                    {
                        "category": "hermes_hook_capability",
                        "resolution": "native_constraint",
                        "native_constraint": "explicit_user_invocation_only",
                    },
                    {
                        "category": "network_request",
                        "resolution": "rejected",
                        "prohibited_behavior": "reach_a_remote_endpoint",
                    },
                    {
                        "category": "potential_committed_secret",
                        "resolution": "native_constraint",
                        "native_constraint": "no_credential_material_retained",
                    },
                    {
                        "category": "process_execution",
                        "resolution": "rejected",
                        "prohibited_behavior": "spawn_a_host_process",
                    },
                    {
                        # The composed self-update finding resolves through the
                        # leg the native version cannot have: with no network
                        # access there is no replacement code to install.
                        "category": "self_update_or_code_replacement",
                        "resolution": "native_constraint",
                        "native_constraint": "no_network_access",
                    },
                    {
                        # Whatever the audit failed to establish about the
                        # plugin's hooks, the native version registers none.
                        "category": "undetermined_hook_contract",
                        "resolution": "native_constraint",
                        "native_constraint": "no_host_hook_registration",
                    },
                    {
                        # The unsettled half is retrieval beside dynamic
                        # execution; the native version evaluates no source at
                        # all, so the ambiguity cannot be inherited.
                        "category": "undetermined_self_update_path",
                        "resolution": "native_constraint",
                        "native_constraint": "no_dynamic_code_execution",
                    },
                ],
                native_constraints=[
                    "no_runtime_dependencies",
                    "explicit_user_invocation_only",
                    "no_credential_material_retained",
                    "no_host_hook_registration",
                    "no_network_access",
                    "no_dynamic_code_execution",
                ],
                prohibited_behaviors=[
                    "execute_evaluated_source",
                    "reach_a_remote_endpoint",
                    "spawn_a_host_process",
                ],
            )
        )

        self.assertEqual(audited_risk_categories(review), AUDITED_RISK_CATEGORIES)
        self.assertEqual(unresolved_risk_categories(review), ())
        self.assertEqual(
            [row["category"] for row in review["risk"]["resolutions"]], list(AUDITED_RISK_CATEGORIES)
        )
        for row in review["risk"]["resolutions"]:
            with self.subTest(category=row["category"]):
                self.assertEqual(sorted(row), sorted(RESOLUTION_KEYS))
                self.assertIn(row["resolution"], RISK_RESOLUTIONS)
                self.assertEqual(bool(row["native_constraint"]), row["resolution"] == "native_constraint")
                self.assertEqual(bool(row["prohibited_behavior"]), row["resolution"] == "rejected")

    def test_an_unresolved_audited_category_fails_validation_naming_the_category(self) -> None:
        with self.assertRaisesRegex(SkillPatternRiskReviewError, r"unresolved: \['process_execution'\]"):
            build_skill_pattern_risk_review(
                **review_kwargs(
                    risk_resolutions=[
                        {
                            "category": "network_request",
                            "resolution": "rejected",
                            "prohibited_behavior": "reach_a_remote_endpoint",
                        }
                    ],
                    native_constraints=["no_network_access"],
                )
            )

        review = build_skill_pattern_risk_review(**review_kwargs())
        stripped = {**review, "risk": {**review["risk"], "resolutions": []}}
        errors = validate_skill_pattern_risk_review(stripped)

        self.assertEqual(unresolved_risk_categories(stripped), ("network_request", "process_execution"))
        self.assertTrue(
            any("network_request" in error and "process_execution" in error for error in errors), errors
        )

    def test_a_resolution_may_not_name_a_constraint_the_native_design_never_adopted(self) -> None:
        with self.assertRaisesRegex(SkillPatternRiskReviewError, "native_reproduction.native_constraints does not carry"):
            build_skill_pattern_risk_review(
                **review_kwargs(native_constraints=["no_network_access", "metadata_only_artifacts"])
            )
        with self.assertRaisesRegex(
            SkillPatternRiskReviewError, "native_reproduction.prohibited_behaviors does not carry"
        ):
            build_skill_pattern_risk_review(**review_kwargs(prohibited_behaviors=[]))

    def test_a_review_may_not_resolve_a_category_the_cited_audit_never_reported(self) -> None:
        with self.assertRaisesRegex(
            SkillPatternRiskReviewError, r"never reported: \['dynamic_code_execution'\]"
        ):
            build_skill_pattern_risk_review(
                **review_kwargs(
                    risk_resolutions=[
                        *review_kwargs()["risk_resolutions"],
                        {
                            "category": "dynamic_code_execution",
                            "resolution": "rejected",
                            "prohibited_behavior": "execute_evaluated_source",
                        },
                    ],
                    prohibited_behaviors=["reach_a_remote_endpoint", "execute_evaluated_source"],
                )
            )

    def test_a_resolution_carries_one_answer_and_not_both(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())
        both = {
            **review,
            "risk": {
                **review["risk"],
                "resolutions": [
                    {
                        "category": "network_request",
                        "resolution": "rejected",
                        "native_constraint": "no_network_access",
                        "prohibited_behavior": "reach_a_remote_endpoint",
                    },
                    review["risk"]["resolutions"][1],
                ],
            },
        }
        errors = validate_skill_pattern_risk_review(both)

        self.assertTrue(any("must leave native_constraint empty" in error for error in errors), errors)

    def test_the_same_category_may_not_be_resolved_twice(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())
        doubled = {
            **review,
            "risk": {
                **review["risk"],
                "resolutions": [*review["risk"]["resolutions"], review["risk"]["resolutions"][0]],
            },
        }
        errors = validate_skill_pattern_risk_review(doubled)

        self.assertTrue(
            any("resolves the same risk category more than once" in error for error in errors), errors
        )

    def test_an_unsupported_resolution_kind_is_refused_by_name(self) -> None:
        with self.assertRaisesRegex(SkillPatternRiskReviewError, "resolution is unsupported: 'noted'"):
            build_skill_pattern_risk_review(
                **review_kwargs(
                    risk_resolutions=[
                        {"category": "network_request", "resolution": "noted"},
                        review_kwargs()["risk_resolutions"][1],
                    ]
                )
            )

    def test_a_clean_scan_resolves_nothing_and_is_still_a_valid_review(self) -> None:
        review = build_skill_pattern_risk_review(**clean_review_kwargs())

        self.assertEqual(review["risk"]["resolutions"], [])
        self.assertEqual(audited_risk_categories(review), ())
        self.assertEqual(unresolved_risk_categories(review), ())
        self.assertEqual(validate_skill_pattern_risk_review(review), [])


class SkillPatternRiskReviewCitationTests(unittest.TestCase):
    """The review cites the scanner rather than becoming a second one."""

    def test_a_review_citing_no_audit_is_refused(self) -> None:
        for absent in (None, {}, [], "plugin_risk_audit/v1", {"schema_version": "something_else/v1"}):
            with self.subTest(absent=absent):
                with self.assertRaisesRegex(SkillPatternRiskReviewError, "scanner evidence"):
                    build_skill_pattern_risk_review(**review_kwargs(audit=absent))

    def test_a_cited_audit_missing_its_summary_is_refused(self) -> None:
        broken = {"schema_version": PLUGIN_RISK_AUDIT_SCHEMA_VERSION, "source": {"manifest_status": "present"}}

        with self.assertRaisesRegex(SkillPatternRiskReviewError, "missing its summary or its source"):
            build_skill_pattern_risk_review(**review_kwargs(audit=broken))

    def test_a_review_whose_audit_evidence_is_removed_fails_validation(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())
        errors = validate_skill_pattern_risk_review({**review, "audit_evidence": {}})

        self.assertTrue(any("audit_evidence is missing keys" in error for error in errors), errors)
        self.assertTrue(any("schema_version must be plugin_risk_audit/v1" in error for error in errors), errors)

    def test_the_risk_category_vocabulary_is_the_scanners_own(self) -> None:
        """Derived, not restated: the review cannot drift from the audit."""
        self.assertEqual(
            AUDITED_RISK_CATEGORIES,
            (
                "declared_dependency",
                "dynamic_code_execution",
                "hermes_hook_capability",
                "network_request",
                "potential_committed_secret",
                "process_execution",
                "self_update_or_code_replacement",
                "undetermined_hook_contract",
                "undetermined_self_update_path",
            ),
        )
        self.assertEqual(sorted(AUDIT_EVIDENCE_KEYS), sorted(cite_plugin_risk_audit(audit_payload())))

    def test_the_cited_evidence_binds_the_review_to_one_scan(self) -> None:
        cited = cite_plugin_risk_audit(audit_payload())
        other = cite_plugin_risk_audit(audit_payload(categories=("network_request",)))

        self.assertEqual(len(cited["audit_digest"]), 64)
        self.assertNotEqual(cited["audit_digest"], other["audit_digest"])
        self.assertEqual(cited["scanned_file_count"], 3)
        self.assertEqual(cited["manifest_status"], "present")

    @requires_secure_dir_io
    def test_a_review_cites_a_real_scan_without_reading_the_skill_itself(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            marker = "PRIVATE_REVIEWED_SKILL_MARKER"
            # atomic_write_text, not Path.write_text: these bytes are counted by
            # the audit and the count lands inside review_digest.
            atomic_write_text(root / "plugin.json", '{"name": "reviewed-example"}\n')
            atomic_write_text(
                root / "plugin.py",
                "import requests\n"
                "def run() -> None:\n"
                "    requests.get('https://example.invalid')\n"
                f"    marker = '{marker}'\n",
            )

            audit = audit_plugin_risk(root)
            review = build_skill_pattern_risk_review(
                **review_kwargs(
                    audit=audit,
                    side_effects=["sends_network_requests"],
                    risk_resolutions=[
                        {
                            "category": "network_request",
                            "resolution": "rejected",
                            "prohibited_behavior": "reach_a_remote_endpoint",
                        },
                        {
                            # This fixture ships no plugin.yaml, so the audit
                            # reports the hook contract as never established.
                            "category": "undetermined_hook_contract",
                            "resolution": "native_constraint",
                            "native_constraint": "no_host_hook_registration",
                        },
                    ],
                    native_constraints=["no_network_access", "no_host_hook_registration"],
                )
            )

        rendered = json.dumps(review)

        self.assertEqual(
            review["audit_evidence"]["risk_categories"], ["network_request", "undetermined_hook_contract"]
        )
        self.assertEqual(review["audit_evidence"]["scanned_file_count"], 2)
        self.assertEqual(review["review_status"], "awaiting_reviewer_decision")
        self.assertNotIn(marker, rendered)
        self.assertNotIn(str(root), rendered)
        self.assertNotIn(root.name, rendered)


class SkillPatternRiskReviewBoundaryTests(unittest.TestCase):
    """OMH read about a skill. It did not import, install, or run one."""

    def test_the_review_records_every_surface_it_did_not_touch(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())

        self.assertEqual(
            review["not_observed"], {surface: {"status": "not_observed"} for surface in NOT_OBSERVED_SURFACES}
        )
        for surface in ("skill_import", "skill_installation", "skill_registration", "skill_execution"):
            with self.subTest(surface=surface):
                self.assertIn(surface, NOT_OBSERVED_SURFACES)
        errors = validate_skill_pattern_risk_review({**review, "not_observed": {}})
        self.assertTrue(any("must mark every one of" in error for error in errors), errors)

    def test_the_claim_boundary_states_that_omh_never_runs_the_reviewed_skill(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())
        boundary = review["claim_boundary"]

        self.assertEqual(boundary, SKILL_PATTERN_RISK_REVIEW_CLAIM_BOUNDARY)
        self.assertIn("never imports, installs, registers, enables, or executes", boundary)
        self.assertIn("A clean scan is scanner evidence and never an approval", boundary)
        self.assertIn("merge evidence", boundary)
        errors = validate_skill_pattern_risk_review({**review, "claim_boundary": "Looks fine."})
        self.assertTrue(any("claim_boundary must state" in error for error in errors), errors)

    def test_a_payload_shaped_to_claim_execution_is_refused_by_key_name(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())

        for key in ("executed", "installed", "ran", "exit_code", "trusted", "verified"):
            with self.subTest(key=key):
                self.assertIn(key, EXECUTION_CLAIM_KEYS)
                errors = validate_skill_pattern_risk_review({**review, key: True})
                self.assertTrue(
                    any("must not carry execution-claim keys" in error for error in errors), (key, errors)
                )

    def test_the_review_refuses_raw_or_hidden_content_and_unsupported_keys(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())

        raw_errors = validate_skill_pattern_risk_review({**review, "transcript": "..."})
        self.assertTrue(any("raw or hidden keys" in error for error in raw_errors), raw_errors)
        unsupported = validate_skill_pattern_risk_review({**review, "vibes": "good"})
        self.assertTrue(any("has unsupported keys: ['vibes']" in error for error in unsupported), unsupported)

    def test_free_text_fields_stay_bounded_metadata_lines(self) -> None:
        with self.assertRaisesRegex(SkillPatternRiskReviewError, "at most 400 characters"):
            build_skill_pattern_risk_review(**review_kwargs(intended_outcome="a" * 401))
        with self.assertRaisesRegex(SkillPatternRiskReviewError, "without secrets, links, or paths"):
            build_skill_pattern_risk_review(
                **review_kwargs(safe_pattern="See https://example.invalid for the pattern.")
            )

    def test_the_skill_ref_is_an_opaque_identifier_and_never_a_location(self) -> None:
        with self.assertRaisesRegex(SkillPatternRiskReviewError, "must be an opaque identifier, not a URL"):
            build_skill_pattern_risk_review(**review_kwargs(skill_ref="https://example.invalid/skill"))
        with self.assertRaisesRegex(SkillPatternRiskReviewError, "skill_ref is required"):
            build_skill_pattern_risk_review(**review_kwargs(skill_ref=""))


class SkillPatternRiskReviewDeterminismTests(unittest.TestCase):
    """Same inputs, same review. No clock anywhere near the digest."""

    def test_two_reviews_of_the_same_material_are_identical(self) -> None:
        first = build_skill_pattern_risk_review(**review_kwargs())
        second = build_skill_pattern_risk_review(**review_kwargs())

        self.assertEqual(first, second)
        self.assertEqual(first["review_digest"], skill_pattern_risk_review_digest(second))

    def test_prepared_at_is_a_parameter_and_stays_outside_the_digest(self) -> None:
        early = build_skill_pattern_risk_review(**review_kwargs(prepared_at="2026-01-01T00:00:00Z"))
        late = build_skill_pattern_risk_review(**review_kwargs(prepared_at="2026-08-09T12:00:00Z"))

        self.assertNotIn("prepared_at", MATERIAL_REVIEW_KEYS)
        self.assertNotEqual(early["prepared_at"], late["prepared_at"])
        self.assertEqual(early["review_digest"], late["review_digest"])
        self.assertEqual(build_skill_pattern_risk_review(**review_kwargs())["prepared_at"], "")

    def test_vocabulary_order_does_not_change_the_digest(self) -> None:
        forward = build_skill_pattern_risk_review(**review_kwargs())
        reversed_input = build_skill_pattern_risk_review(
            **review_kwargs(
                required_authority=["filesystem_read", "network_access"],
                native_constraints=["no_network_access", "no_subprocess_execution"],
                evidence_limits=["runtime_behavior_unknown", "static_scan_only_no_execution_observed"],
            )
        )

        self.assertEqual(forward, reversed_input)

    def test_the_digest_seals_the_material_content_and_notices_an_edit(self) -> None:
        review = build_skill_pattern_risk_review(**review_kwargs())
        edited = {**review, "confidence": {**review["confidence"], "level": "high"}}
        errors = validate_skill_pattern_risk_review(edited)

        self.assertTrue(any("does not match the content it seals" in error for error in errors), errors)

    def test_the_material_key_set_covers_the_review_minus_its_own_state(self) -> None:
        self.assertEqual(
            sorted(MATERIAL_REVIEW_KEYS),
            sorted(
                {
                    "schema_version",
                    "skill_ref",
                    "audit_evidence",
                    "usefulness",
                    "risk",
                    "confidence",
                    "native_reproduction",
                    "not_observed",
                }
            ),
        )
        self.assertEqual(set(MATERIAL_REVIEW_KEYS) - set(SKILL_PATTERN_RISK_REVIEW_KEYS), set())

    def test_the_constraint_and_prohibition_vocabularies_cover_every_audited_category(self) -> None:
        """AC3 is only satisfiable if every category has an answer available."""
        self.assertTrue(set(NATIVE_CONSTRAINTS))
        self.assertTrue(set(PROHIBITED_BEHAVIORS))
        self.assertEqual(set(NATIVE_CONSTRAINTS) & set(PROHIBITED_BEHAVIORS), set())
        for category in AUDITED_RISK_CATEGORIES:
            with self.subTest(category=category):
                review = build_skill_pattern_risk_review(
                    **review_kwargs(
                        audit=audit_payload(categories=(category,)),
                        side_effects=["side_effects_unclear"],
                        risk_resolutions=[
                            {
                                "category": category,
                                "resolution": "native_constraint",
                                "native_constraint": "deterministic_offline_behavior",
                            }
                        ],
                        native_constraints=["deterministic_offline_behavior"],
                        prohibited_behaviors=[],
                    )
                )
                self.assertEqual(unresolved_risk_categories(review), ())


class SkillPatternRiskReviewCommandTests(unittest.TestCase):
    """#1571: the security step has a caller that is not this file.

    The module docstring used to say `tests/test_skill_pattern_risk_review.py`
    was its only caller, which made the step real and unreachable at once.
    `omh ops skill-pattern-risk-review` is the reachable form, and these cases
    drive it through the real CLI over a real directory.
    """

    @requires_secure_dir_io
    def test_the_command_scans_a_real_directory_and_resolves_every_reported_category(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "third-party-skill"
            root.mkdir()
            # atomic_write_text, not Path.write_text: these bytes are counted by
            # the audit and the count lands inside review_digest.
            atomic_write_text(root / "collect.py", "import subprocess\n\n\ndef collect():\n    return subprocess.run(['ls'])\n")

            status, stdout, stderr = run_cli([
                "ops", "skill-pattern-risk-review",
                "--skill-root", str(root),
                "--skill-ref", "third-party-listing-skill",
                "--intended-outcome", "List the files a report needs to name.",
                "--step", "Read the named directory.",
                "--step", "Report the file names it holds.",
                "--authority", "filesystem_read",
                "--data", "repository_source",
                "--side-effect", "spawns_processes",
                "--resolve", "process_execution=rejected:spawn_a_host_process",
                "--resolve", "undetermined_hook_contract=native_constraint:no_host_hook_registration",
                "--confidence-level", "medium",
                "--confidence-basis", "cited_static_scan",
                "--evidence-limit", "static_scan_only_no_execution_observed",
                "--safe-pattern", "List the directory with a bounded local read and no subprocess.",
                "--native-constraint", "no_subprocess_execution",
                "--native-constraint", "no_host_hook_registration",
                "--prohibited", "spawn_a_host_process",
                "--prepared-at", "2026-09-15T00:00:00Z",
            ])

            self.assertEqual(status, 0, stderr)
            review = json.loads(stdout)
            self.assertEqual(review["schema_version"], SKILL_PATTERN_RISK_REVIEW_SCHEMA_VERSION)
            self.assertEqual(validate_skill_pattern_risk_review(review), [])
            # The command cited a scan and decided nothing, which is the
            # boundary the contract is built around.
            self.assertEqual(review["review_status"], "awaiting_reviewer_decision")
            self.assertFalse(review_reads_as_approved(review))
            self.assertEqual(review["reviewer_decision"], {})
            self.assertEqual(unresolved_risk_categories(review), ())
            self.assertEqual(
                set(review["audit_evidence"]["risk_categories"]),
                set(audit_plugin_risk(root)["summary"]["risk_categories"]),
            )

    @requires_secure_dir_io
    def test_an_unresolved_reported_category_is_refused_by_the_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "third-party-skill"
            root.mkdir()
            atomic_write_text(root / "collect.py", "import subprocess\n\n\ndef collect():\n    return subprocess.run(['ls'])\n")

            status, _, stderr = run_cli([
                "ops", "skill-pattern-risk-review",
                "--skill-root", str(root),
                "--skill-ref", "third-party-listing-skill",
                "--intended-outcome", "List the files a report needs to name.",
                "--step", "Read the named directory.",
                "--authority", "filesystem_read",
                "--data", "repository_source",
                "--side-effect", "spawns_processes",
                "--resolve", "process_execution=rejected:spawn_a_host_process",
                "--confidence-level", "medium",
                "--confidence-basis", "cited_static_scan",
                "--evidence-limit", "static_scan_only_no_execution_observed",
                "--safe-pattern", "List the directory with a bounded local read and no subprocess.",
                "--native-constraint", "no_subprocess_execution",
                "--prohibited", "spawn_a_host_process",
            ])

            self.assertNotEqual(status, 0)
            self.assertIn("undetermined_hook_contract", stderr)

    def test_a_malformed_resolution_flag_names_the_shape_it_wanted(self) -> None:
        """Deliberately not gated on secure dir IO: this one never scans.

        The two cases above assert on what the scan reported, so they cannot run
        where `read_static_plugin_sources` refuses for want of `O_NOFOLLOW`. This
        one asserts the shape of an argument, which is decidable without touching
        the filesystem, so it runs everywhere -- gating the whole class would
        have taken Windows coverage away from the case that passes there. The
        directory is still real, so the only thing wrong is the flag.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "third-party-skill"
            root.mkdir()

            status, _, stderr = run_cli([
                "ops", "skill-pattern-risk-review",
                "--skill-root", str(root),
                "--skill-ref", "third-party-listing-skill",
                "--intended-outcome", "List the files a report needs to name.",
                "--step", "Read the named directory.",
                "--authority", "filesystem_read",
                "--data", "repository_source",
                "--side-effect", "no_side_effects_identified",
                "--resolve", "undetermined_hook_contract",
                "--confidence-level", "low",
                "--confidence-basis", "cited_static_scan",
                "--evidence-limit", "static_scan_only_no_execution_observed",
                "--safe-pattern", "List the directory with a bounded local read.",
                "--native-constraint", "no_host_hook_registration",
            ])

            self.assertNotEqual(status, 0)
            self.assertIn("category=resolution:target", stderr)


if __name__ == "__main__":
    unittest.main()

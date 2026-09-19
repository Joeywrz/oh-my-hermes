"""An owner is recommended only when evidence says it can finish the accepted plan.

The defect (#810): `resolve_coding_route_decision` chose a coding owner from an
owner named in the request envelope, an owner named in the message, a recorded
setup preference, or a keyword route-family cue -- and from nothing else. No
capability snapshot, no readiness probe, no look at what the plan needed. Its
payload had no requirement field, no gap field, and no unknown field. So a
person could name an owner, be told the handoff was prepared, and find out
mid-execution that the owner could not isolate a worktree or run the routed
workflow at all.

What these pin:

* AC1 -- an owner with a KNOWN unmet required capability is never recommended,
  asserted on the surfaces Hermes actually reads (the built delegation payload
  and the ranked choose-executor card), not only on the matcher.
* AC2 -- every required capability appears with its classification AND the
  evidence reference that produced it; a payload missing either fails
  validation.
* AC3 -- two owners holding equal capability evidence produce fit records that
  are equal once the two owner-identity fields are removed. A real differential:
  the same evidence is recorded under different owner ids and the verdicts,
  reasons, and classifications must not move.

`unknown` -- stale evidence and absent evidence both classify `unknown`, and
`unknown` is a third state with its own consequence: the owner is `unproven`,
which is neither recommended (AC1 stays strict) nor reported as a blocking gap.

Guards: an explicitly named unfit owner is still honoured and its gap is stated
rather than the owner disappearing, and equal evidence under different owner
ids does not move the verdict.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from _local_package import load_local_package

load_local_package()
from omh.coding.coding_delegation import build_coding_delegation_payload  # noqa: E402
from omh.coding.executor_capability_snapshots import (  # noqa: E402
    KNOWN_CAPABILITY_NAMES,
    LOCAL_WORKFLOW_CAPABILITY_NAME,
    build_executor_capability_snapshot,
    executor_capability_snapshot_path,
    write_executor_capability_snapshot,
)
from omh.coding.executor_readiness import (  # noqa: E402
    EXECUTOR_CHOICE_CONTEXT_PROFILES,
    executor_choice_context,
)
from omh.coding.owner_fit import (  # noqa: E402
    ACCEPTED_PLAN_FIELDS,
    OWNER_FIT_CLASSIFICATION_KEYS,
    OWNER_FIT_KEYS,
    OWNER_FIT_OWNER_IDENTITY_KEYS,
    OWNER_FIT_REASON_CODES,
    OWNER_FIT_REPORT_KEYS,
    OWNER_FIT_REPORT_SCHEMA_VERSION,
    OWNER_FIT_REQUIREMENT_KEYS,
    OWNER_FIT_SCHEMA_VERSION,
    WORKFLOW_CAPABILITY_REQUIREMENTS,
    OwnerFitError,
    accepted_plan_from_delegation,
    build_owner_fit_report,
    derive_plan_capability_requirements,
    evaluate_owner_fit,
    owner_capability_snapshots,
    owner_fit_without_owner_identity,
    validate_owner_fit,
    validate_owner_fit_report,
)
from omh.coding.pre_handoff_readiness import (  # noqa: E402
    CAPABILITY_EVIDENCE_STALE_AFTER_SECONDS,
    capability_evidence_is_fresh,
)
from omh.skills.catalog import routable_skill_names  # noqa: E402
from omh.system.local_store import utc_now  # noqa: E402
from omh.system.paths import OmhPaths  # noqa: E402


OBSERVED_AT = "2026-01-01T00:00:00Z"
WITHIN_WINDOW = "2026-01-01T06:00:00Z"
PAST_WINDOW = "2026-01-03T00:00:00Z"

# `build_coding_delegation_payload` is the production path; it accepts `now`
# since #1768 and these surface tests deliberately leave it at its default, the
# real clock, recording their evidence at the real current time so the
# production freshness path is the one exercised. The window is 24 hours, so
# wall-clock drift inside one test run cannot change a verdict.
RECORDED_NOW = utc_now()

# One team-shaped plan used almost everywhere: it declares a routed workflow
# whose lane needs parallel agents and a workspace binding that requires an
# isolated worktree, so both derivation rules are live at once.
TEAM_PLAN: dict[str, Any] = {
    "workflow": "ultrawork",
    "work_owner_mode": "external_executor",
    "isolation_strategy": "worktree_required",
}
TEAM_REQUIREMENTS = ("parallel_agents", "background_work", "worktree_isolation")

# The message that produces TEAM_PLAN through the real delegation build, so the
# surface tests exercise a plan Hermes actually derived rather than one a test
# handed it.
TEAM_MESSAGE = "implement pagination in src/api/list.py with a team of workers in a worktree and run the tests"


def _observed(capability_evidence_ref: str, observed_at: str = OBSERVED_AT, **scope: str) -> dict[str, Any]:
    return {
        "status": "host_observed",
        "scope": dict(scope) or {"surface": "local"},
        "evidence_ref": capability_evidence_ref,
        "observed_at": observed_at,
    }


# `executor_capability_snapshot/v1` keeps a generic `unavailable` capability
# status-only: no scope, no evidence_ref, no observed_at. The snapshot's own
# `recorded_at` is therefore the observation time for a recorded absence.
UNAVAILABLE: dict[str, Any] = {"status": "unavailable"}


def _snapshot(executor: str, capabilities: dict[str, Any], *, recorded_at: str = OBSERVED_AT) -> dict[str, Any]:
    return {
        "schema_version": "executor_capability_snapshot/v1",
        "executor": executor,
        "recorded_at": recorded_at,
        "capabilities": capabilities,
    }


def _team_capable(executor: str, *, recorded_at: str = OBSERVED_AT) -> dict[str, Any]:
    return _snapshot(
        executor,
        {
            "parallel_agents": _observed("probe:parallel-lanes", recorded_at),
            "background_work": _observed("probe:background", recorded_at),
            "worktree_isolation": _observed("probe:worktree", recorded_at),
        },
        recorded_at=recorded_at,
    )


def _team_blocked(executor: str, *, recorded_at: str = OBSERVED_AT) -> dict[str, Any]:
    return _snapshot(
        executor,
        {
            "parallel_agents": _observed("probe:parallel-lanes", recorded_at),
            "background_work": _observed("probe:background", recorded_at),
            # A recorded host observation that the capability is NOT there.
            # This is the shape AC1 calls a KNOWN unmet capability.
            "worktree_isolation": dict(UNAVAILABLE),
        },
        recorded_at=recorded_at,
    )


def _fit(owner: str, snapshot: dict[str, Any] | None, *, now: str = WITHIN_WINDOW) -> dict[str, Any]:
    return evaluate_owner_fit(
        owner=owner,
        requirements=derive_plan_capability_requirements(TEAM_PLAN),
        capability_snapshot=snapshot,
        now=now,
    )


def _paths(root: Path) -> OmhPaths:
    return OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")


def _write_snapshot(directory: Path, snapshot: dict[str, Any]) -> None:
    executor = str(snapshot["executor"])
    write_executor_capability_snapshot(
        executor_capability_snapshot_path(directory, executor),
        build_executor_capability_snapshot(
            executor=executor,
            capabilities=snapshot["capabilities"],
            recorded_at=str(snapshot["recorded_at"]),
        ),
    )


def _snapshot_directory(root: Path) -> Path:
    return root / ".omh" / "coding" / "executor-capability-snapshots"


class RequirementDerivationTests(unittest.TestCase):
    """Requirements come from declared plan fields, never from free text."""

    def test_the_workflow_map_only_names_real_workflows_and_real_capabilities(self) -> None:
        routable = set(routable_skill_names())
        vocabulary = set(KNOWN_CAPABILITY_NAMES) | {LOCAL_WORKFLOW_CAPABILITY_NAME}
        for workflow, capabilities in WORKFLOW_CAPABILITY_REQUIREMENTS.items():
            with self.subTest(workflow=workflow):
                self.assertIn(workflow, routable)
                self.assertTrue(capabilities)
                for capability in capabilities:
                    self.assertIn(capability, vocabulary)

    def test_a_team_plan_requires_parallel_lanes_and_worktree_isolation(self) -> None:
        requirements = derive_plan_capability_requirements(TEAM_PLAN)
        self.assertEqual(tuple(item["capability"] for item in requirements), TEAM_REQUIREMENTS)
        by_capability = {str(item["capability"]): item for item in requirements}
        self.assertEqual(by_capability["parallel_agents"]["source_field"], "workflow")
        self.assertEqual(by_capability["parallel_agents"]["source_value"], "ultrawork")
        self.assertEqual(by_capability["worktree_isolation"]["source_field"], "isolation_strategy")
        self.assertEqual(by_capability["worktree_isolation"]["source_value"], "worktree_required")
        for item in requirements:
            self.assertEqual(set(item), set(OWNER_FIT_REQUIREMENT_KEYS))

    def test_a_runtime_handoff_requires_the_routed_workflow_locally_and_scopes_it(self) -> None:
        requirements = derive_plan_capability_requirements(
            {"workflow": "loop", "work_owner_mode": "runtime_handoff", "isolation_strategy": "same_workspace_ok"}
        )
        by_capability = {str(item["capability"]): item for item in requirements}
        self.assertIn(LOCAL_WORKFLOW_CAPABILITY_NAME, by_capability)
        self.assertEqual(by_capability[LOCAL_WORKFLOW_CAPABILITY_NAME]["scope"], {"skill_id": "loop"})
        self.assertEqual(by_capability["long_running_continuation"]["source_field"], "workflow")

    def test_a_plan_that_declares_nothing_requires_nothing(self) -> None:
        # The negative case that keeps the derivation from being a heuristic: a
        # plan whose declared fields name no capability produces no requirement,
        # however the request was worded.
        plan = {"workflow": "plan", "work_owner_mode": "external_executor", "isolation_strategy": "same_workspace_ok"}
        self.assertEqual(derive_plan_capability_requirements(plan), ())

    def test_an_undeclared_plan_field_is_rejected_rather_than_ignored(self) -> None:
        with self.assertRaises(OwnerFitError):
            derive_plan_capability_requirements({**TEAM_PLAN, "message": "run the tests in parallel"})

    def test_the_accepted_plan_field_set_is_exactly_what_the_projection_produces(self) -> None:
        payload = build_coding_delegation_payload(TEAM_MESSAGE, executor_target="codex")
        plan = accepted_plan_from_delegation(payload)
        self.assertEqual(set(plan), set(ACCEPTED_PLAN_FIELDS))
        self.assertEqual(plan["workflow"], "ultrawork")
        self.assertEqual(plan["isolation_strategy"], "worktree_required")

    def test_one_capability_is_explained_by_one_declared_field(self) -> None:
        # `ultrawork` asks for parallel agents and the message is worktree
        # shaped, so both rules fire. The capability must still be claimed once,
        # by the first rule, or `source_field` would name a set instead of a
        # field.
        requirements = derive_plan_capability_requirements(
            {"workflow": "ultrawork", "work_owner_mode": "runtime_handoff", "isolation_strategy": "worktree_required"}
        )
        names = [str(item["capability"]) for item in requirements]
        self.assertEqual(len(names), len(set(names)))


class KnownUnmetCapabilityTests(unittest.TestCase):
    """AC1: a known unmet required capability is never a recommendation."""

    def test_the_matcher_blocks_an_owner_whose_capability_is_observed_unavailable(self) -> None:
        fit = _fit("codex", _team_blocked("codex"))
        self.assertEqual(fit["verdict"], "blocked")
        self.assertFalse(fit["recommendable"])
        self.assertEqual(fit["unmet"], ["worktree_isolation"])
        self.assertEqual(fit["met"], ["parallel_agents", "background_work"])

    def test_the_report_never_recommends_a_blocked_owner(self) -> None:
        report = build_owner_fit_report(
            requirements=derive_plan_capability_requirements(TEAM_PLAN),
            owners=[("codex", _team_blocked("codex")), ("claude-code", _team_capable("claude-code"))],
            now=WITHIN_WINDOW,
        )
        self.assertEqual(report["recommended_owners"], ["claude-code"])
        self.assertEqual(report["blocked_owners"], ["codex"])
        self.assertEqual(report["next_action"], "choose_from_recommended_owners")

    def test_the_built_delegation_payload_never_recommends_a_blocked_owner(self) -> None:
        # The surface Hermes reads, not the raw function: a real delegation
        # build with a real snapshot on disk.
        with TemporaryDirectory() as tmp:
            directory = _snapshot_directory(Path(tmp))
            _write_snapshot(directory, _team_blocked("codex", recorded_at=RECORDED_NOW))
            _write_snapshot(directory, _team_capable("claude-code", recorded_at=RECORDED_NOW))
            payload = build_coding_delegation_payload(
                TEAM_MESSAGE,
                executor_target="choose",
                capability_snapshot_directory=directory,
            )
        report = payload["coding_owner_fit"]
        self.assertEqual(validate_owner_fit_report(report), [])
        self.assertNotIn("codex", report["recommended_owners"])
        self.assertIn("codex", report["blocked_owners"])
        self.assertEqual(report["recommended_owners"], ["claude-code"])

    def test_the_choose_executor_card_never_ranks_a_blocked_owner_first(self) -> None:
        # The other recommendation surface: whatever the login marker says, an
        # owner that cannot do the work must not head the card.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            _write_snapshot(paths.executor_capability_snapshots_dir, _team_blocked("codex"))
            _write_snapshot(paths.executor_capability_snapshots_dir, _team_capable("claude-code"))
            context = executor_choice_context(paths, now=WITHIN_WINDOW, plan=TEAM_PLAN)
        by_profile = {str(entry["profile"]): entry for entry in context["candidates"]}
        self.assertEqual(by_profile["codex"]["owner_fit_verdict"], "blocked")
        self.assertEqual(by_profile["codex"]["owner_fit_unmet"], ["worktree_isolation"])
        self.assertEqual(by_profile["claude-code"]["owner_fit_verdict"], "ready")
        self.assertEqual(str(context["candidates"][0]["profile"]), "claude-code")
        self.assertEqual(str(context["candidates"][-1]["profile"]), "codex")
        self.assertIn("owner_fit_verdict", context["ranked_by"])

    def test_a_card_built_without_a_plan_keeps_its_pre_810_shape(self) -> None:
        # The negative case: no plan means nothing derived what the work needs,
        # so no verdict is invented and the existing ordering is untouched.
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            context = executor_choice_context(paths, now=WITHIN_WINDOW)
        self.assertEqual(
            context["ranked_by"],
            ("login_marker", "fresh_limit_signal_absent", "readiness_status"),
        )
        for candidate in context["candidates"]:
            self.assertNotIn("owner_fit_verdict", candidate)

    def test_a_report_that_recommends_a_blocked_owner_fails_validation(self) -> None:
        report = build_owner_fit_report(
            requirements=derive_plan_capability_requirements(TEAM_PLAN),
            owners=[("codex", _team_blocked("codex"))],
            now=WITHIN_WINDOW,
        )
        tampered = {**report, "recommended_owners": ["codex"]}
        self.assertTrue(
            any("recommended_owners" in error for error in validate_owner_fit_report(tampered)),
            validate_owner_fit_report(tampered),
        )


class ExplainedRecommendationTests(unittest.TestCase):
    """AC2: every required capability arrives with its classification and evidence."""

    def test_each_required_capability_carries_a_classification_and_an_evidence_reference(self) -> None:
        fit = _fit("codex", _team_blocked("codex"))
        self.assertEqual([str(entry["capability"]) for entry in fit["capabilities"]], list(TEAM_REQUIREMENTS))
        for entry in fit["capabilities"]:
            with self.subTest(capability=entry["capability"]):
                self.assertEqual(set(entry), set(OWNER_FIT_CLASSIFICATION_KEYS))
                self.assertIn(entry["classification"], ("met", "unmet", "unknown"))
                self.assertTrue(str(entry["evidence_ref"]).strip())
                self.assertEqual(OWNER_FIT_REASON_CODES[str(entry["reason_code"])], entry["classification"])
                self.assertTrue(str(entry["reason"]).strip())

    def test_the_recorded_evidence_reference_is_the_one_the_snapshot_supplied(self) -> None:
        fit = _fit("codex", _team_capable("codex"))
        by_capability = {str(entry["capability"]): entry for entry in fit["capabilities"]}
        self.assertEqual(by_capability["parallel_agents"]["evidence_ref"], "probe:parallel-lanes")
        self.assertEqual(by_capability["worktree_isolation"]["evidence_ref"], "probe:worktree")
        self.assertEqual(by_capability["worktree_isolation"]["evidence_observed_at"], OBSERVED_AT)

    def test_a_classification_missing_its_verdict_fails_validation(self) -> None:
        fit = _fit("codex", _team_capable("codex"))
        stripped = dict(fit["capabilities"][0])
        stripped.pop("classification")
        tampered = {**fit, "capabilities": [stripped, fit["capabilities"][1]]}
        self.assertTrue(
            any("classification" in error for error in validate_owner_fit(tampered)),
            validate_owner_fit(tampered),
        )

    def test_a_classification_missing_its_evidence_reference_fails_validation(self) -> None:
        fit = _fit("codex", _team_capable("codex"))
        stripped = dict(fit["capabilities"][0])
        stripped.pop("evidence_ref")
        tampered = {**fit, "capabilities": [stripped, fit["capabilities"][1]]}
        self.assertTrue(
            any("evidence_ref" in error for error in validate_owner_fit(tampered)),
            validate_owner_fit(tampered),
        )

    def test_an_owner_that_skips_a_required_capability_fails_report_validation(self) -> None:
        report = build_owner_fit_report(
            requirements=derive_plan_capability_requirements(TEAM_PLAN),
            owners=[("codex", _team_capable("codex"))],
            now=WITHIN_WINDOW,
        )
        trimmed = dict(report["owners"][0])
        trimmed["capabilities"] = trimmed["capabilities"][:1]
        trimmed["met"] = ["parallel_agents"]
        tampered = {**report, "owners": [trimmed]}
        self.assertTrue(
            any("declared required capabilities" in error for error in validate_owner_fit_report(tampered)),
            validate_owner_fit_report(tampered),
        )

    def test_both_artifacts_keep_a_closed_key_set(self) -> None:
        report = build_owner_fit_report(
            requirements=derive_plan_capability_requirements(TEAM_PLAN),
            owners=[("codex", _team_capable("codex"))],
            now=WITHIN_WINDOW,
        )
        self.assertEqual(set(report), set(OWNER_FIT_REPORT_KEYS))
        self.assertEqual(set(report["owners"][0]), set(OWNER_FIT_KEYS))
        self.assertEqual(report["schema_version"], OWNER_FIT_REPORT_SCHEMA_VERSION)
        self.assertEqual(report["owners"][0]["schema_version"], OWNER_FIT_SCHEMA_VERSION)
        self.assertEqual(report["status"], "prepared_not_observed")
        self.assertEqual(validate_owner_fit_report({**report, "surprise": 1})[0][:40], "owner fit report contains unsupported ke")


class ExecutorNeutralityTests(unittest.TestCase):
    """AC3: equal evidence, equal fit -- whoever the owner is."""

    def test_equal_evidence_under_different_owner_ids_produces_an_equal_fit(self) -> None:
        # The differential. Same capability names, same evidence refs, same
        # observation times; only the owner id and the snapshot's executor field
        # differ. Everything except the two owner-identity fields must match.
        first = _fit("codex", _team_capable("codex"))
        second = _fit("claude-code", _team_capable("claude-code"))
        self.assertNotEqual(first["owner"], second["owner"])
        self.assertNotEqual(first["label"], second["label"])
        self.assertEqual(
            owner_fit_without_owner_identity(first),
            owner_fit_without_owner_identity(second),
        )

    def test_equal_evidence_produces_an_equal_blocked_fit_too(self) -> None:
        # Neutrality has to hold on the failing path as well, or a brand could
        # leak through a gap message nobody reads on the happy path.
        first = _fit("codex", _team_blocked("codex"))
        second = _fit("omo-runtime", _team_blocked("omo-runtime"))
        self.assertEqual(
            owner_fit_without_owner_identity(first),
            owner_fit_without_owner_identity(second),
        )
        self.assertEqual(first["verdict"], "blocked")

    def test_no_owner_name_leaks_into_a_verdict_reason_or_gap(self) -> None:
        report = build_owner_fit_report(
            requirements=derive_plan_capability_requirements(TEAM_PLAN),
            owners=[("codex", _team_blocked("codex")), ("claude-code", _team_capable("claude-code"))],
            named_owner="codex",
            now=WITHIN_WINDOW,
        )
        neutral_text = " ".join(
            [
                str(report["named_owner_gap"]),
                *(str(fit["reason"]) for fit in report["owners"]),
                *(str(entry["reason"]) for fit in report["owners"] for entry in fit["capabilities"]),
            ]
        ).casefold()
        for brand in ("codex", "claude", "hermes", "omo", "omx", "omc"):
            with self.subTest(brand=brand):
                self.assertNotIn(brand, neutral_text)

    def test_the_owner_identity_field_set_is_the_only_difference_the_projection_drops(self) -> None:
        fit = _fit("codex", _team_capable("codex"))
        projection = owner_fit_without_owner_identity(fit)
        self.assertEqual(set(fit) - set(projection), set(OWNER_FIT_OWNER_IDENTITY_KEYS))


class UnknownIsItsOwnStateTests(unittest.TestCase):
    """Unknown is neither met nor unmet, and it has its own consequence."""

    def test_absent_evidence_classifies_unknown(self) -> None:
        fit = _fit("codex", None)
        self.assertEqual(sorted(fit["unknown"]), sorted(TEAM_REQUIREMENTS))
        self.assertEqual(fit["met"], [])
        self.assertEqual(fit["unmet"], [])
        self.assertEqual(
            [str(entry["reason_code"]) for entry in fit["capabilities"]],
            ["no_capability_snapshot", "no_capability_snapshot", "no_capability_snapshot"],
        )

    def test_a_capability_the_snapshot_never_mentions_classifies_unknown(self) -> None:
        fit = _fit("codex", _snapshot("codex", {"parallel_agents": _observed("probe:parallel-lanes")}))
        by_capability = {str(entry["capability"]): entry for entry in fit["capabilities"]}
        self.assertEqual(by_capability["worktree_isolation"]["classification"], "unknown")
        self.assertEqual(by_capability["worktree_isolation"]["reason_code"], "capability_not_recorded")
        self.assertEqual(by_capability["parallel_agents"]["classification"], "met")

    def test_stale_evidence_classifies_unknown_and_never_met(self) -> None:
        fresh = _fit("codex", _team_capable("codex"), now=WITHIN_WINDOW)
        stale = _fit("codex", _team_capable("codex"), now=PAST_WINDOW)
        self.assertEqual(fresh["verdict"], "ready")
        self.assertEqual(stale["verdict"], "unproven")
        self.assertEqual(stale["met"], [])
        self.assertEqual(sorted(stale["unknown"]), sorted(TEAM_REQUIREMENTS))
        self.assertEqual(
            {str(entry["reason_code"]) for entry in stale["capabilities"]},
            {"evidence_expired"},
        )

    def test_stale_unavailable_evidence_classifies_unknown_and_never_unmet(self) -> None:
        # Symmetry, and it is safe: an expired observation is not knowledge of
        # the present either way, and `unknown` still yields `unproven`, which
        # is still not recommended.
        stale = _fit("codex", _team_blocked("codex"), now=PAST_WINDOW)
        self.assertEqual(stale["unmet"], [])
        self.assertEqual(stale["verdict"], "unproven")

    def test_prepared_only_evidence_classifies_unknown(self) -> None:
        fit = _fit(
            "codex",
            _snapshot(
                "codex",
                {"parallel_agents": {"status": "prepared"}, "worktree_isolation": {"status": "unknown"}},
            ),
        )
        self.assertEqual(
            [str(entry["reason_code"]) for entry in fit["capabilities"]],
            ["evidence_prepared_only", "capability_not_recorded", "evidence_status_unknown"],
        )
        self.assertEqual(fit["verdict"], "unproven")

    def test_evidence_scoped_to_another_workflow_classifies_unknown(self) -> None:
        requirements = derive_plan_capability_requirements(
            {"workflow": "loop", "work_owner_mode": "runtime_handoff", "isolation_strategy": "same_workspace_ok"}
        )
        snapshot = _snapshot(
            "codex",
            {
                "long_running_continuation": _observed("probe:continuation"),
                LOCAL_WORKFLOW_CAPABILITY_NAME: {
                    "status": "host_observed",
                    "scope": {"profile": "codex", "skill_id": "ultrawork", "environment": "local"},
                    "evidence_ref": "probe:local-workflow",
                    "observed_at": OBSERVED_AT,
                },
            },
        )
        fit = evaluate_owner_fit(
            owner="codex",
            requirements=requirements,
            capability_snapshot=snapshot,
            now=WITHIN_WINDOW,
        )
        by_capability = {str(entry["capability"]): entry for entry in fit["capabilities"]}
        self.assertEqual(by_capability[LOCAL_WORKFLOW_CAPABILITY_NAME]["classification"], "unknown")
        self.assertEqual(by_capability[LOCAL_WORKFLOW_CAPABILITY_NAME]["reason_code"], "evidence_scope_mismatch")

    def test_unknown_is_distinguishable_from_both_met_and_unmet(self) -> None:
        met = _fit("codex", _team_capable("codex"))
        unmet = _fit("codex", _team_blocked("codex"))
        unknown = _fit("codex", None)
        self.assertEqual({met["verdict"], unmet["verdict"], unknown["verdict"]}, {"ready", "blocked", "unproven"})
        self.assertEqual(
            {met["next_action"], unmet["next_action"], unknown["next_action"]},
            {
                "prepare_handoff_for_this_owner",
                "close_the_capability_gap_or_choose_another_owner",
                "record_capability_evidence",
            },
        )
        # An unproven owner is not recommended (AC1 stays strict) and is not a
        # blocking gap either -- it is its own bucket with its own next action.
        report = build_owner_fit_report(
            requirements=derive_plan_capability_requirements(TEAM_PLAN),
            owners=[("codex", None)],
            now=WITHIN_WINDOW,
        )
        self.assertEqual(report["recommended_owners"], [])
        self.assertEqual(report["blocked_owners"], [])
        self.assertEqual(report["unproven_owners"], ["codex"])
        self.assertEqual(report["next_action"], "record_capability_evidence")

    def test_the_freshness_horizon_is_the_one_837_already_enforces(self) -> None:
        self.assertTrue(capability_evidence_is_fresh(OBSERVED_AT, WITHIN_WINDOW))
        self.assertFalse(capability_evidence_is_fresh(OBSERVED_AT, PAST_WINDOW))
        self.assertFalse(capability_evidence_is_fresh("", WITHIN_WINDOW))
        self.assertEqual(CAPABILITY_EVIDENCE_STALE_AFTER_SECONDS, 24 * 60 * 60)


class NamedOwnerGuardTests(unittest.TestCase):
    """A named owner is honoured; an unfit one surfaces its gap rather than vanishing."""

    def test_a_named_unfit_owner_stays_in_the_report_with_its_gap_stated(self) -> None:
        report = build_owner_fit_report(
            requirements=derive_plan_capability_requirements(TEAM_PLAN),
            owners=[("codex", _team_blocked("codex")), ("claude-code", _team_capable("claude-code"))],
            named_owner="codex",
            now=WITHIN_WINDOW,
        )
        self.assertEqual(report["named_owner"], "codex")
        self.assertTrue(report["named_owner_honoured"])
        self.assertIn("worktree_isolation", str(report["named_owner_gap"]))
        self.assertEqual(report["next_action"], "confirm_named_owner_despite_capability_gap")
        # Honoured is not the same as recommended: the owner is present, and
        # absent from the recommendation.
        self.assertIn("codex", [str(fit["owner"]) for fit in report["owners"]])
        self.assertNotIn("codex", report["recommended_owners"])

    def test_the_delegation_payload_keeps_a_named_unfit_owner_and_states_the_gap(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = _snapshot_directory(Path(tmp))
            _write_snapshot(directory, _team_blocked("codex", recorded_at=RECORDED_NOW))
            payload = build_coding_delegation_payload(
                TEAM_MESSAGE,
                executor_target="codex",
                capability_snapshot_directory=directory,
            )
        report = payload["coding_owner_fit"]
        self.assertEqual(validate_owner_fit_report(report), [])
        self.assertEqual(report["named_owner"], "codex")
        self.assertTrue(report["named_owner_honoured"])
        self.assertTrue(str(report["named_owner_gap"]).strip())
        self.assertNotIn("codex", report["recommended_owners"])
        # The route decision is untouched: naming an owner still selects it.
        self.assertEqual(payload["selected_executor_profile"], "codex")

    def test_a_named_owner_outside_the_default_candidates_is_still_reported(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = _snapshot_directory(Path(tmp))
            payload = build_coding_delegation_payload(
                TEAM_MESSAGE,
                executor_target="omx-runtime",
                capability_snapshot_directory=directory,
            )
        report = payload["coding_owner_fit"]
        self.assertNotIn("omx-runtime", EXECUTOR_CHOICE_CONTEXT_PROFILES)
        self.assertIn("omx-runtime", [str(fit["owner"]) for fit in report["owners"]])
        self.assertEqual(report["named_owner"], "omx-runtime")

    def test_a_named_ready_owner_has_no_gap_and_prepares_its_handoff(self) -> None:
        report = build_owner_fit_report(
            requirements=derive_plan_capability_requirements(TEAM_PLAN),
            owners=[("codex", _team_capable("codex"))],
            named_owner="codex",
            now=WITHIN_WINDOW,
        )
        self.assertEqual(report["named_owner_gap"], "")
        self.assertEqual(report["next_action"], "prepare_handoff_for_named_owner")
        self.assertEqual(report["recommended_owners"], ["codex"])

    def test_a_report_cannot_name_an_owner_it_does_not_carry(self) -> None:
        with self.assertRaises(OwnerFitError):
            build_owner_fit_report(
                requirements=derive_plan_capability_requirements(TEAM_PLAN),
                owners=[("claude-code", _team_capable("claude-code"))],
                named_owner="codex",
                now=WITHIN_WINDOW,
            )

    def test_a_gap_erased_from_a_named_unfit_owner_fails_validation(self) -> None:
        report = build_owner_fit_report(
            requirements=derive_plan_capability_requirements(TEAM_PLAN),
            owners=[("codex", _team_blocked("codex"))],
            named_owner="codex",
            now=WITHIN_WINDOW,
        )
        tampered = {**report, "named_owner_gap": ""}
        self.assertTrue(
            any("named_owner_gap" in error for error in validate_owner_fit_report(tampered)),
            validate_owner_fit_report(tampered),
        )


class EvidenceReadingTests(unittest.TestCase):
    """Reading snapshots off disk never invents evidence."""

    def test_a_missing_directory_pairs_every_owner_with_no_snapshot(self) -> None:
        self.assertEqual(
            owner_capability_snapshots(None, ("codex", "claude-code")),
            (("codex", None), ("claude-code", None)),
        )

    def test_a_snapshot_recorded_for_another_owner_is_not_read_as_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = _snapshot_directory(Path(tmp))
            _write_snapshot(directory, _team_capable("codex"))
            paired = dict(owner_capability_snapshots(directory, ("codex", "claude-code")))
        self.assertIsNotNone(paired["codex"])
        self.assertIsNone(paired["claude-code"])

    def test_an_unsafe_owner_id_reads_as_no_snapshot_rather_than_escaping_the_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = _snapshot_directory(Path(tmp))
            paired = dict(owner_capability_snapshots(directory, ("../codex",)))
        self.assertIsNone(paired["../codex"])


    def test_a_plan_requiring_nothing_makes_every_owner_ready(self) -> None:
        report = build_owner_fit_report(
            requirements=(),
            owners=[("codex", None), ("claude-code", None)],
            now=WITHIN_WINDOW,
        )
        self.assertEqual(report["recommended_owners"], ["codex", "claude-code"])
        self.assertIn("no required capability", str(report["owners"][0]["reason"]))


class DescriptiveCapabilityOwnerFitControlTests(unittest.TestCase):
    def test_unrelated_edit_evidence_cannot_hide_a_required_team_capability_gap(self) -> None:
        snapshot = build_executor_capability_snapshot(
            executor="codex",
            capabilities={
                "edit_format_patch": _observed("probe:patch-edit"),
                "parallel_agents": {"status": "unavailable"},
                "background_work": _observed("probe:background"),
                "worktree_isolation": {"status": "prepared"},
            },
            recorded_at=OBSERVED_AT,
        )

        report = build_owner_fit_report(
            requirements=derive_plan_capability_requirements(TEAM_PLAN),
            owners=(("codex", snapshot),),
            now=WITHIN_WINDOW,
        )

        self.assertEqual(report["recommended_owners"], [])
        self.assertEqual(report["owners"][0]["verdict"], "blocked")


if __name__ == "__main__":
    unittest.main()

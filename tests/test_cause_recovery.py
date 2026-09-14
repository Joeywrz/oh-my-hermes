"""The recovery must match the cause, and every cause must have one.

One test per row of `RECOVERY_ROWS`, plus the three rules that cut across the
table: a live worker forbids a rerun whatever the cause, identical conditions
forbid a stall rerun, and a limit under the same account inside the cooldown is
not a rerun the operator should be offered.

The last class is the derived gate. It re-reads both vocabularies from source --
every `FAILURE_KIND_*` constant assigned in `src/coding/dispatch_failure_recovery.py`
and every member of `UNIT_STUCK_STATES` -- and fails naming the member that has
no row. It is deliberately NOT a frozen list of expected members: adding a
failure kind or a stuck state stays a one-line change plus its row, which is the
"catch the class without freezing the codebase" shape. The constants are read as
an AST rather than imported so that a kind added by a sibling branch is caught
the moment it is written down, whether or not anything has imported it yet.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()

from omh.coding.cause_recovery import (  # noqa: E402
    ACTION_ANSWER_OR_CANCEL,
    ACTION_CHECK_ACCOUNT_THEN_SWITCH_IF_ALLOWED,
    ACTION_INSTALL_BINARY_THEN_REDISPATCH,
    ACTION_RECORD_CAPABILITY_EVIDENCE_THEN_REDISPATCH,
    ACTION_REAUTHENTICATE_THEN_REDISPATCH,
    ACTION_REPAIR_PERMISSIONS_THEN_REPROBE,
    ACTION_REPORT_ONLY,
    ACTION_REPORT_OR_CHOOSE_RECOVERY,
    ACTION_RERUN_WITH_CHANGED_CONDITIONS_OR_CHECKPOINT,
    ACTION_SUPPLEMENT_OBJECTS_THEN_RESUME_UNIT,
    ACTION_WAIT_FOR_LIVE_WORKER,
    CAUSE_ACCOUNT_LIMIT,
    CAUSE_AWAITING_INPUT,
    CAUSE_CAPABILITY_GATE,
    CAUSE_CRASH,
    CAUSE_DATA_MISSING,
    CAUSE_LIVE_WORKER_PRESENT,
    CAUSE_PERMISSION_BLOCKED,
    CAUSE_PROGRESS_STALLED,
    CAUSE_TIMEOUT,
    CAUSE_UNKNOWN,
    RECOVERY_PLAN_SCHEMA_VERSION,
    REQUIRE_ACCOUNT_CHANGED_OR_RESET_ELAPSED,
    REQUIRE_CAPABILITY_EVIDENCE_RECORDED,
    REQUIRE_CONDITIONS_CHANGED,
    REQUIRE_OBJECTS_PRESENT,
    REQUIRE_WORKSPACE_PREFLIGHT_OK,
    attempt_conditions,
    conditions_fingerprint,
    has_recovery_row,
    limit_reset_text,
    plan_summary_line,
    recovery_plan,
    resolve_cause,
)
from omh.coding.dispatch_failure_recovery import (  # noqa: E402
    CHOICE_HERMES,
    CHOICE_RETARGET,
    CHOICE_WAIT,
    FAILURE_KIND_WORKSPACE_BLOCKED,
    recovery_candidates,
    recovery_decision,
    recovery_options,
    spawn_cooldown,
)
from omh.coding.fanout_dispatch import EXECUTOR_LIMIT_SIGNALS_SCHEMA_VERSION  # noqa: E402
from omh.coding.workspace_preflight import (  # noqa: E402
    CHECK_CASE_COLLISION,
    CHECK_FILE_WRITE,
    CHECK_GIT_INDEX_WRITE,
    CHECK_OBJECTS_PRESENT,
    workspace_preflight_unit_state,
)
from omh.coding.unit_execution_state import UNIT_STUCK_STATES  # noqa: E402
from omh.system.local_store import atomic_write_json, utc_now  # noqa: E402
from omh.system.paths import OmhPaths  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
FAILURE_KIND_SOURCE = REPO_ROOT / "src" / "coding" / "dispatch_failure_recovery.py"

LAST = {
    "owner": "codex",
    "model": "gpt-6-astra",
    "prompt_sha256": "abc123",
    "worktree": "/tmp/wt-core",
    "permissions": "acceptEdits",
    "account_tag": "codex:11aa22bb",
}


def live_marker(**overrides: object) -> dict[str, object]:
    marker = {
        "marker_status": "present",
        "fanout_id": "fan-1",
        "unit_id": "core",
        "worktree": "/tmp/wt-core",
        "started_at": "2026-09-11T04:00:00Z",
    }
    marker.update(overrides)
    return marker


class RowPermissionBlockedTests(unittest.TestCase):
    def test_a_permission_denial_repairs_permissions_and_reprobes(self) -> None:
        plan = recovery_plan({"unit_id": "core", "unit_state": "permission_blocked"})
        self.assertEqual(plan["cause"], CAUSE_PERMISSION_BLOCKED)
        self.assertEqual(plan["action"], ACTION_REPAIR_PERMISSIONS_THEN_REPROBE)
        self.assertEqual(plan["requires"], [REQUIRE_WORKSPACE_PREFLIGHT_OK])
        self.assertFalse(plan["allowed_rerun"])
        self.assertEqual(plan["schema_version"], RECOVERY_PLAN_SCHEMA_VERSION)

    def test_a_fresh_passing_preflight_is_what_allows_the_rerun(self) -> None:
        plan = recovery_plan(
            {
                "unit_id": "core",
                "unit_state": "permission_blocked",
                "workspace_preflight": {"ok": True},
            }
        )
        self.assertTrue(plan["allowed_rerun"])
        self.assertEqual(plan["resume_unit_id"], "core")

    def test_a_failing_preflight_is_not_a_pass(self) -> None:
        plan = recovery_plan(
            {
                "unit_id": "core",
                "unit_state": "permission_blocked",
                "workspace_preflight": {"ok": False, "blocking": [CHECK_FILE_WRITE]},
            }
        )
        self.assertFalse(plan["allowed_rerun"])
        self.assertEqual(plan["unmet"], [REQUIRE_WORKSPACE_PREFLIGHT_OK])

    def test_only_a_boolean_true_reads_as_a_pass(self) -> None:
        """An `ok` this module did not understand must never read as a pass."""
        for value in ("ok", 1, "true", None):
            with self.subTest(ok=value):
                plan = recovery_plan(
                    {
                        "unit_id": "core",
                        "unit_state": "permission_blocked",
                        "workspace_preflight": {"ok": value},
                    }
                )
                self.assertFalse(plan["allowed_rerun"])

    def test_workspace_blocked_defaults_to_the_permission_row(self) -> None:
        self.assertEqual(
            resolve_cause(failure_kind=FAILURE_KIND_WORKSPACE_BLOCKED), CAUSE_PERMISSION_BLOCKED
        )

    def test_a_filesystem_block_takes_the_permission_row(self) -> None:
        """Only the FIRST blocking check decides; the preflight runs in dependency order."""
        for first in (CHECK_FILE_WRITE, CHECK_GIT_INDEX_WRITE, CHECK_CASE_COLLISION):
            with self.subTest(first=first):
                self.assertEqual(
                    resolve_cause(
                        failure_kind=FAILURE_KIND_WORKSPACE_BLOCKED,
                        workspace_preflight={
                            "ok": False,
                            "blocking": [first, CHECK_OBJECTS_PRESENT],
                        },
                    ),
                    CAUSE_PERMISSION_BLOCKED,
                )

    def test_the_routing_agrees_with_the_preflights_own_state_for_every_check(self) -> None:
        """The table and the stamp call one function; nothing may route them apart."""
        for check in (
            CHECK_FILE_WRITE,
            CHECK_GIT_INDEX_WRITE,
            CHECK_OBJECTS_PRESENT,
            CHECK_CASE_COLLISION,
        ):
            with self.subTest(check=check):
                report = {"ok": False, "blocking": [check]}
                self.assertEqual(
                    resolve_cause(
                        failure_kind=FAILURE_KIND_WORKSPACE_BLOCKED, workspace_preflight=report
                    ),
                    resolve_cause(unit_state=workspace_preflight_unit_state(report)),
                )


class RowAccountLimitTests(unittest.TestCase):
    def test_a_limit_checks_the_account_before_anything_else(self) -> None:
        plan = recovery_plan({"unit_id": "core", "failure_kind": "limit_shaped"}, last_attempt=LAST)
        self.assertEqual(plan["cause"], CAUSE_ACCOUNT_LIMIT)
        self.assertEqual(plan["action"], ACTION_CHECK_ACCOUNT_THEN_SWITCH_IF_ALLOWED)
        self.assertEqual(plan["requires"], [REQUIRE_ACCOUNT_CHANGED_OR_RESET_ELAPSED])

    def test_the_same_account_inside_the_cooldown_is_not_a_rerun(self) -> None:
        unit = {
            "unit_id": "core",
            "failure_kind": "limit_shaped",
            "account_tag": LAST["account_tag"],
            "limit_signal": {"stale": False, "reset_text": "6:10pm (Asia/Seoul)"},
            "proposed_attempt": dict(LAST),
        }
        plan = recovery_plan(unit, last_attempt=LAST)
        self.assertFalse(plan["allowed_rerun"])
        self.assertIn(REQUIRE_ACCOUNT_CHANGED_OR_RESET_ELAPSED, plan["unmet"])

    def test_a_different_account_tag_allows_the_rerun(self) -> None:
        unit = {
            "unit_id": "core",
            "failure_kind": "limit_shaped",
            "limit_signal": {"stale": False},
            "proposed_attempt": {**LAST, "account_tag": "codex:99ff88ee"},
        }
        plan = recovery_plan(unit, last_attempt=LAST)
        self.assertTrue(plan["allowed_rerun"])

    def test_an_unreadable_tag_on_either_side_cannot_show_a_change(self) -> None:
        unit = {
            "unit_id": "core",
            "failure_kind": "limit_shaped",
            "limit_signal": {"stale": False},
            "proposed_attempt": {**LAST, "account_tag": ""},
        }
        self.assertFalse(recovery_plan(unit, last_attempt=LAST)["allowed_rerun"])

    def test_an_elapsed_window_allows_the_rerun_under_the_same_account(self) -> None:
        unit = {
            "unit_id": "core",
            "failure_kind": "limit_shaped",
            "limit_signal": {"stale": True},
            "proposed_attempt": dict(LAST),
        }
        self.assertTrue(recovery_plan(unit, last_attempt=LAST)["allowed_rerun"])

    def test_reset_text_is_taken_literally_and_never_parsed(self) -> None:
        self.assertEqual(
            limit_reset_text("You've hit your session limit · resets 6:10pm (Asia/Seoul)"),
            "6:10pm (Asia/Seoul)",
        )
        self.assertEqual(limit_reset_text("rate limited", ""), "")
        # A word that merely ends in "resets" is not the provider saying so.
        self.assertEqual(limit_reset_text("loaded 4 presets from disk"), "")


class RowDataMissingTests(unittest.TestCase):
    def test_missing_objects_are_supplemented_then_one_unit_resumes(self) -> None:
        plan = recovery_plan({"unit_id": "core", "unit_state": "data_missing"})
        self.assertEqual(plan["cause"], CAUSE_DATA_MISSING)
        self.assertEqual(plan["action"], ACTION_SUPPLEMENT_OBJECTS_THEN_RESUME_UNIT)
        self.assertEqual(plan["requires"], [REQUIRE_OBJECTS_PRESENT])
        self.assertFalse(plan["allowed_rerun"])
        self.assertIn("resume unit core only, never the whole fanout", plan["reason"])

    def test_verified_objects_name_the_one_unit_to_resume(self) -> None:
        plan = recovery_plan(
            {"unit_id": "core", "unit_state": "data_missing", "objects_present": True}
        )
        self.assertTrue(plan["allowed_rerun"])
        self.assertEqual(plan["resume_unit_id"], "core")

    def test_a_workspace_preflight_blocking_on_objects_takes_the_objects_row(self) -> None:
        plan = recovery_plan(
            {
                "unit_id": "core",
                "failure_kind": FAILURE_KIND_WORKSPACE_BLOCKED,
                "workspace_preflight": {"ok": False, "blocking": [CHECK_OBJECTS_PRESENT]},
            }
        )
        self.assertEqual(plan["cause"], CAUSE_DATA_MISSING)

    def test_the_unit_state_the_preflight_stamped_wins_over_the_payload(self) -> None:
        """The producer's own verdict is read first; deriving it again is the fallback."""
        plan = recovery_plan(
            {
                "unit_id": "core",
                "failure_kind": FAILURE_KIND_WORKSPACE_BLOCKED,
                "unit_state": "data_missing",
                "workspace_preflight": {"ok": False, "blocking": []},
            }
        )
        self.assertEqual(plan["cause"], CAUSE_DATA_MISSING)


class RowProgressStalledTests(unittest.TestCase):
    def test_identical_conditions_forbid_the_rerun(self) -> None:
        unit = {"unit_id": "core", "unit_state": "progress_stalled", "proposed_attempt": dict(LAST)}
        plan = recovery_plan(unit, last_attempt=LAST)
        self.assertEqual(plan["cause"], CAUSE_PROGRESS_STALLED)
        self.assertEqual(plan["action"], ACTION_RERUN_WITH_CHANGED_CONDITIONS_OR_CHECKPOINT)
        self.assertEqual(plan["requires"], [REQUIRE_CONDITIONS_CHANGED])
        self.assertFalse(plan["allowed_rerun"])

    def test_a_changed_model_is_a_changed_condition(self) -> None:
        unit = {
            "unit_id": "core",
            "unit_state": "progress_stalled",
            "proposed_attempt": {**LAST, "model": "claude-opus-5"},
        }
        self.assertTrue(recovery_plan(unit, last_attempt=LAST)["allowed_rerun"])

    def test_no_stated_proposal_is_the_same_attempt(self) -> None:
        """Re-running the same command is the default, so it must be the default answer."""
        plan = recovery_plan({"unit_id": "core", "unit_state": "progress_stalled"}, last_attempt=LAST)
        self.assertFalse(plan["allowed_rerun"])

    def test_a_fingerprint_over_nothing_never_compares_equal(self) -> None:
        self.assertEqual(conditions_fingerprint({}), "")
        self.assertEqual(conditions_fingerprint(None), "")
        self.assertNotEqual(conditions_fingerprint(LAST), "")

    def test_attempt_conditions_reads_either_worktree_spelling(self) -> None:
        envelope = {"owner": "codex", "model": "m", "worktree_path": "/tmp/wt"}
        self.assertEqual(attempt_conditions(envelope)["worktree"], "/tmp/wt")


class RowAwaitingInputTests(unittest.TestCase):
    def test_a_question_is_answered_or_cancelled_never_re_run(self) -> None:
        plan = recovery_plan({"unit_id": "core", "unit_state": "awaiting_input"})
        self.assertEqual(plan["cause"], CAUSE_AWAITING_INPUT)
        self.assertEqual(plan["action"], ACTION_ANSWER_OR_CANCEL)
        self.assertEqual(plan["requires"], [])
        self.assertFalse(plan["allowed_rerun"])
        self.assertEqual(plan["resume_unit_id"], "")


class RowMappedThroughTests(unittest.TestCase):
    def test_timeout_and_crash_keep_the_existing_choice(self) -> None:
        for kind, cause in (("timeout", CAUSE_TIMEOUT), ("crash", CAUSE_CRASH)):
            with self.subTest(kind=kind):
                plan = recovery_plan({"unit_id": "core", "failure_kind": kind})
                self.assertEqual(plan["cause"], cause)
                self.assertEqual(plan["action"], ACTION_REPORT_OR_CHOOSE_RECOVERY)
                self.assertEqual(plan["requires"], [])
                self.assertTrue(plan["allowed_rerun"])
                self.assertFalse(plan["blocks_new_worker"])

    def test_auth_requires_the_credential_to_be_repaired_first(self) -> None:
        plan = recovery_plan({"unit_id": "core", "failure_kind": "auth_shaped"})
        self.assertEqual(plan["action"], ACTION_REAUTHENTICATE_THEN_REDISPATCH)
        self.assertFalse(plan["allowed_rerun"])
        # Another owner carries its own credential, so the lane stays open.
        self.assertFalse(plan["blocks_new_worker"])
        self.assertTrue(
            recovery_plan({"unit_id": "core", "failure_kind": "auth_shaped", "credential_repaired": True})[
                "allowed_rerun"
            ]
        )

    def test_a_missing_binary_requires_the_binary(self) -> None:
        plan = recovery_plan({"unit_id": "core", "failure_kind": "binary_missing"})
        self.assertEqual(plan["action"], ACTION_INSTALL_BINARY_THEN_REDISPATCH)
        self.assertFalse(plan["allowed_rerun"])

    def test_a_capability_gate_refusal_requires_recorded_evidence(self) -> None:
        # The frozen capability evidence refused the spawn (no snapshot, no
        # route, no fresh modality evidence, or an unobserved transformation).
        # Re-running against the same evidence is the same refusal.
        plan = recovery_plan({"unit_id": "media", "failure_kind": "capability_gate"})
        self.assertEqual(plan["cause"], CAUSE_CAPABILITY_GATE)
        self.assertEqual(plan["action"], ACTION_RECORD_CAPABILITY_EVIDENCE_THEN_REDISPATCH)
        self.assertFalse(plan["allowed_rerun"])
        self.assertEqual(plan["unmet"], [REQUIRE_CAPABILITY_EVIDENCE_RECORDED])
        recorded = recovery_plan(
            {"unit_id": "media", "failure_kind": "capability_gate", "capability_evidence_recorded": True}
        )
        self.assertTrue(recorded["allowed_rerun"])
        self.assertEqual(recorded["unmet"], [])

    def test_nothing_recorded_claims_no_cause_specific_repair(self) -> None:
        plan = recovery_plan({"unit_id": "core"})
        self.assertEqual(plan["cause"], CAUSE_UNKNOWN)
        self.assertEqual(plan["action"], ACTION_REPORT_ONLY)


class LiveWorkerRuleTests(unittest.TestCase):
    """The rule that wins over every row: never a second worker on a live scope."""

    def test_a_live_marker_on_the_same_unit_forbids_every_cause(self) -> None:
        for kind in ("limit_shaped", "auth_shaped", "timeout", "crash", "binary_missing"):
            with self.subTest(kind=kind):
                plan = recovery_plan(
                    {"unit_id": "core", "failure_kind": kind, "credential_repaired": True},
                    inflight=[live_marker()],
                )
                self.assertEqual(plan["cause"], CAUSE_LIVE_WORKER_PRESENT)
                self.assertEqual(plan["action"], ACTION_WAIT_FOR_LIVE_WORKER)
                self.assertFalse(plan["allowed_rerun"])

    def test_it_beats_a_cause_whose_requirements_are_all_met(self) -> None:
        unit = {
            "unit_id": "core",
            "unit_state": "data_missing",
            "objects_present": True,
        }
        plan = recovery_plan(unit, inflight=[live_marker()])
        self.assertEqual(plan["cause"], CAUSE_LIVE_WORKER_PRESENT)

    def test_the_reason_names_the_marker_and_refuses_the_liveness_claim(self) -> None:
        plan = recovery_plan({"unit_id": "core", "failure_kind": "crash"}, inflight=[live_marker()])
        self.assertIn("unit core", plan["reason"])
        self.assertIn("fanout fan-1", plan["reason"])
        self.assertIn("2026-09-11T04:00:00Z", plan["reason"])
        self.assertIn("presence is not liveness", plan["reason"])

    def test_a_different_unit_sharing_the_worktree_is_the_same_scope(self) -> None:
        plan = recovery_plan(
            {"unit_id": "other", "worktree_path": "/tmp/wt-core", "failure_kind": "crash"},
            inflight=[live_marker()],
        )
        self.assertEqual(plan["cause"], CAUSE_LIVE_WORKER_PRESENT)

    def test_an_absent_or_unreadable_marker_is_not_a_live_worker(self) -> None:
        for status in ("absent", "unreadable"):
            with self.subTest(status=status):
                plan = recovery_plan(
                    {"unit_id": "core", "failure_kind": "crash"},
                    inflight=[live_marker(marker_status=status)],
                )
                self.assertEqual(plan["cause"], CAUSE_CRASH)

    def test_an_unrelated_scope_does_not_block(self) -> None:
        plan = recovery_plan(
            {"unit_id": "core", "worktree_path": "/tmp/wt-core", "failure_kind": "crash"},
            inflight=[live_marker(unit_id="docs", worktree="/tmp/wt-docs")],
        )
        self.assertEqual(plan["cause"], CAUSE_CRASH)


class RecoveryOptionWiringTests(unittest.TestCase):
    """The plan reaches the offered options, the decision record, and the print."""

    def _options(self, plan: dict[str, object]) -> list[dict[str, object]]:
        return recovery_options(
            candidate={"unit_id": "core", "owner": "codex", "failure_kind": "limit_shaped"},
            retargets=[{"profile": "claude-code"}],
            hermes_available=True,
            plan=plan,
        )

    def test_an_inherited_cause_closes_both_spawning_lanes(self) -> None:
        plan = recovery_plan({"unit_id": "core", "unit_state": "data_missing"})
        options = self._options(plan)
        by_choice = {option["choice"]: option for option in options}
        self.assertFalse(by_choice[CHOICE_RETARGET]["available"])
        self.assertFalse(by_choice[CHOICE_HERMES]["available"])
        self.assertIn("data_missing", by_choice[CHOICE_RETARGET]["unavailable_reason"])
        # Waiting is always carryable: it spawns nothing.
        self.assertTrue(by_choice[CHOICE_WAIT]["available"])

    def test_a_quota_leaves_both_lanes_open(self) -> None:
        plan = recovery_plan({"unit_id": "core", "failure_kind": "limit_shaped"}, last_attempt=LAST)
        by_choice = {option["choice"]: option for option in self._options(plan)}
        self.assertFalse(plan["allowed_rerun"])
        self.assertTrue(by_choice[CHOICE_RETARGET]["available"])
        self.assertTrue(by_choice[CHOICE_HERMES]["available"])

    def test_candidates_carry_their_plan_and_admit_stuck_units(self) -> None:
        units = [
            {"unit_id": "a", "owner": "codex", "failure_kind": "limit_shaped"},
            {"unit_id": "b", "owner": "codex", "unit_state": "progress_stalled"},
            {"unit_id": "c", "owner": "codex", "failure_kind": "crash"},
        ]
        rows = recovery_candidates(units)
        self.assertEqual([row["unit_id"] for row in rows], ["a", "b"])
        self.assertEqual(rows[1]["plan"]["cause"], CAUSE_PROGRESS_STALLED)

    def test_a_live_marker_reaches_the_candidate_through_recovery_candidates(self) -> None:
        rows = recovery_candidates(
            [{"unit_id": "core", "owner": "codex", "failure_kind": "limit_shaped"}],
            inflight=[live_marker()],
        )
        self.assertEqual(rows[0]["plan"]["cause"], CAUSE_LIVE_WORKER_PRESENT)

    def test_the_decision_record_carries_the_plan(self) -> None:
        candidate = recovery_candidates(
            [{"unit_id": "core", "owner": "codex", "failure_kind": "limit_shaped"}]
        )[0]
        decision = recovery_decision(candidate=candidate, choice=CHOICE_WAIT)
        self.assertEqual(decision["plan"]["cause"], CAUSE_ACCOUNT_LIMIT)

    def test_the_summary_line_states_the_cause_and_the_requirement(self) -> None:
        plan = recovery_plan({"unit_id": "core", "unit_state": "data_missing"})
        line = plan_summary_line(plan)
        self.assertIn(CAUSE_DATA_MISSING, line)
        self.assertIn(REQUIRE_OBJECTS_PRESENT, line)
        self.assertIn("rerun not allowed yet", line)
        self.assertEqual(plan_summary_line(None), "")


# An enum member is an identifier-shaped string. `FAILURE_KIND_PRECEDENCE` is a
# paragraph of prose that happens to share the prefix, and demanding a recovery
# row for a paragraph would be the gate misfiring rather than catching
# something. The shape is the discriminator, not a name on an exclusion list: a
# kind added tomorrow is covered without an edit here, and prose added tomorrow
# is excluded without one either.
_KIND_VALUE_RE = re.compile(r"[a-z][a-z0-9_]*")


class CooldownAdvisoryTests(unittest.TestCase):
    """The refused-spawn line names whose window it was, not just that there was one."""

    def _paths(self, tmp: str) -> OmhPaths:
        root = Path(tmp)
        return OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")

    def _signal(self, paths: OmhPaths, entry: dict[str, object]) -> None:
        atomic_write_json(
            paths.executor_limit_signals_path,
            {"schema_version": EXECUTOR_LIMIT_SIGNALS_SCHEMA_VERSION, "profiles": {"codex": entry}},
            private=True,
        )

    def test_the_account_and_the_reset_phrase_reach_the_refusal(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            self._signal(
                paths,
                {
                    "last_limit_shaped_at": utc_now(),
                    "pattern_label": "usage_limit",
                    "account_tag": "codex:11aa22bb",
                    "reset_text": "6:10pm (Asia/Seoul)",
                },
            )
            cooldown = spawn_cooldown(paths, "codex")
            self.assertIsNotNone(cooldown)
            self.assertEqual(cooldown["account_tag"], "codex:11aa22bb")
            self.assertEqual(cooldown["reset_text"], "6:10pm (Asia/Seoul)")
            self.assertIn("under account codex:11aa22bb", cooldown["reason"])
            self.assertIn("resets 6:10pm (Asia/Seoul)", cooldown["reason"])

    def test_an_unrecorded_account_adds_no_key_and_no_prose(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            self._signal(paths, {"last_limit_shaped_at": utc_now(), "pattern_label": "usage_limit"})
            cooldown = spawn_cooldown(paths, "codex")
            self.assertIsNotNone(cooldown)
            self.assertNotIn("account_tag", cooldown)
            self.assertNotIn("reset_text", cooldown)
            self.assertNotIn("under account", cooldown["reason"])


def _declared_failure_kinds() -> dict[str, str]:
    """Every `FAILURE_KIND_*` enum constant assigned in the classifier module.

    Read from source rather than imported so a kind a sibling branch has just
    written down is covered before anything imports it.
    """
    tree = ast.parse(FAILURE_KIND_SOURCE.read_text(encoding="utf-8"), filename=str(FAILURE_KIND_SOURCE))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        value = node.value.value
        if not isinstance(value, str) or not _KIND_VALUE_RE.fullmatch(value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.startswith("FAILURE_KIND_"):
                found[target.id] = value
    return found


class RecoveryTableCoverageTests(unittest.TestCase):
    """The derived gate: every vocabulary member has a row, or this names it."""

    def test_every_failure_kind_constant_has_a_row(self) -> None:
        declared = _declared_failure_kinds()
        # A guard on the gate itself: an AST walk that silently found nothing
        # would pass every assertion below.
        self.assertIn("FAILURE_KIND_CRASH", declared)
        missing = sorted(
            f"{name} ({value})" for name, value in declared.items() if not has_recovery_row(value)
        )
        self.assertEqual(
            missing,
            [],
            "failure kinds with no row in RECOVERY_ROWS: "
            f"{', '.join(missing)}. Add a row (and its alias, if the cause is already named) "
            "in src/coding/cause_recovery.py.",
        )

    def test_every_stuck_state_has_a_row(self) -> None:
        self.assertTrue(UNIT_STUCK_STATES)
        missing = sorted(state for state in UNIT_STUCK_STATES if not has_recovery_row(state))
        self.assertEqual(
            missing,
            [],
            "stuck unit states with no row in RECOVERY_ROWS: "
            f"{', '.join(missing)}. Add a row or an alias in src/coding/cause_recovery.py.",
        )

    def test_the_gate_fails_for_a_member_nothing_covers(self) -> None:
        """The gate would not be one if it passed for an unknown member."""
        self.assertFalse(has_recovery_row("some_kind_nobody_wrote_a_row_for"))


if __name__ == "__main__":  # pragma: no cover - parity with the suite's modules
    unittest.main()

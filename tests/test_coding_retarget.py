"""An accepted coding plan can change owner without being planned again (#812).

The gap. `prepare_wrapper_session_handoff` refused every executor change on a
follow-up handoff and offered one escape: "start a new session to switch
executors". A new session carries no accepted plan, so the only supported way
to move work from one coding owner to another was to replan work the user had
already approved and re-derive criteria they had already agreed to.

What these pin:

* **AC1 -- preservation, proved by equality rather than by inspection.** The
  owner-neutral task contract is asserted byte-identical across retargets
  spanning all three handoff kinds (executor-handoff `codex`, prompt-handoff
  `claude-code`, runtime-handoff `hermes`/`omx-runtime`), on the pure projection
  AND through the real wrapper session. The teeth are in the builder, not only
  here: `build_owner_retarget` compares the two contracts field by field and
  refuses a move that would change one, so a replan cannot be recorded as a
  retarget.

* **AC2 -- the capability difference is named before the new handoff.** With a
  recorded snapshot saying the new owner cannot run the routed workflow
  locally, the retarget reports it as `unsupported` and asks for confirmation;
  with a snapshot saying it can, nothing is invented. The report is produced by
  a pure function over two projections, so it exists before anything is
  prepared or written.

* **AC3 -- no source-host configuration or credential is read.** The host
  credential and config readers (`~/.codex/auth.json`, `~/.claude.json`, the
  OMO host marker, the live readiness binding, and `Path.home()` itself) are
  patched to raise, and a full session-level retarget still completes.

Guards: an unknown owner is refused on both surfaces; the original handoff's
stored artifact is byte-identical after the move; and the old guard's intent
survives --- a follow-up handoff with a different executor and no explicit
retarget is still refused.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from _local_package import load_local_package

load_local_package()
from omh.coding.coding_delegation import build_coding_delegation_payload  # noqa: E402
from omh.coding.executor_capability_snapshots import (  # noqa: E402
    build_executor_capability_snapshot,
    executor_capability_snapshot_path,
    write_executor_capability_snapshot,
)
from omh.coding.executors import EXECUTOR_PROFILES, executor_label  # noqa: E402
from omh.coding.owner_retarget import (  # noqa: E402
    CAPABILITY_DELTA_KEYS,
    CODING_TASK_CONTRACT_SCHEMA_VERSION,
    OWNER_DELTA_KEYS,
    OWNER_RETARGET_CLAIM_BOUNDARY,
    OWNER_RETARGET_KEYS,
    OWNER_RETARGET_NEXT_ACTIONS,
    OWNER_RETARGET_SCHEMA_VERSION,
    OWNER_SPECIFIC_FIELDS,
    TASK_CONTRACT_KEYS,
    OwnerRetargetError,
    build_owner_retarget,
    coding_task_contract,
    coding_task_contract_digest,
    validate_coding_task_contract,
    validate_owner_retarget,
)
from omh.paths import resolve_paths  # noqa: E402
from omh.system.local_store import utc_now  # noqa: E402
from omh.wrapper.sessions import (  # noqa: E402
    WrapperSessionError,
    create_or_resume_wrapper_session,
    prepare_wrapper_session_handoff,
    read_wrapper_session_events,
    record_plan_decision,
)


# One accepted coding task, routed to `ai-slop-cleaner`. The workflow matters:
# it is routable (so it can be named in a `local_workflow` snapshot scope) and
# it declares no workflow-level capability requirement of its own, so the only
# requirement in play is the one the OWNER CHANGE introduces.
MESSAGE = "risky refactor of src/coding/executors.py with tests"
ROUTED_WORKFLOW = "ai-slop-cleaner"

# A different accepted coding task. Also `delegate`, and deliberately a
# different intent and workflow, so retargeting between the two is a replan.
OTHER_MESSAGE = "review the diff in src/api/list.py for correctness"

TASK_SHA = "b" * 64

# One owner per handoff kind, so "at least three owner kinds" is exercised by
# construction rather than by comment.
OWNER_KINDS = {
    "codex": "executor_handoff",
    "claude-code": "prompt_handoff",
    "hermes": "runtime_handoff",
    "omx-runtime": "runtime_handoff",
}

# `build_coding_delegation_payload` accepts `now` since #1768, and these surface
# tests deliberately leave it at its default, the real clock: they record their
# evidence at the real current time so the production freshness path is the one
# exercised. The window is 24 hours, so drift inside one test run cannot move a
# verdict.
RECORDED_NOW = utc_now()


def _payload(owner: str, message: str = MESSAGE, **kwargs: Any) -> dict[str, Any]:
    return build_coding_delegation_payload(message, executor_target=owner, **kwargs)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _local_workflow_snapshot(owner: str, *, status: str, recorded_at: str = RECORDED_NOW) -> dict[str, Any]:
    return build_executor_capability_snapshot(
        executor=owner,
        recorded_at=recorded_at,
        capabilities={
            "local_workflow": {
                "status": status,
                "scope": {"profile": owner, "skill_id": ROUTED_WORKFLOW, "environment": "test-host"},
                "evidence_ref": "operator:retarget-fixture",
                "observed_at": recorded_at,
            }
        },
    )


def _paths(root: Path):
    return resolve_paths(root / ".omh", root / ".hermes")


def _snapshot_directory(paths) -> Path:
    return paths.omh_home / "coding" / "executor-capability-snapshots"


def _write_snapshot(paths, snapshot: dict[str, Any]) -> None:
    directory = _snapshot_directory(paths)
    write_executor_capability_snapshot(
        executor_capability_snapshot_path(directory, str(snapshot["executor"])),
        snapshot,
    )


def _accepted_session(paths, owner: str) -> str:
    started = create_or_resume_wrapper_session(
        paths,
        MESSAGE,
        source="discord",
        source_metadata={"source_event_id": "m1", "channel_ref": "c1"},
        executor_target=owner,
    )
    session_id = str(started["session"]["session_id"])
    record_plan_decision(paths, session_id, "accept")
    prepare_wrapper_session_handoff(paths, session_id, MESSAGE, executor_target=owner)
    return session_id


def _session_events(paths, session_id: str) -> list[dict[str, Any]]:
    return read_wrapper_session_events(paths.runtime_wrapper_sessions_dir / session_id)


class TaskContractPreservationTests(unittest.TestCase):
    """AC1: the owner-neutral half of an accepted plan does not move."""

    def test_the_task_contract_is_byte_identical_across_every_owner_kind(self) -> None:
        contracts = {
            owner: coding_task_contract(_payload(owner), task_source_sha256=TASK_SHA)
            for owner in OWNER_KINDS
        }
        rendered = {owner: _canonical(contract) for owner, contract in contracts.items()}
        self.assertEqual(len(set(rendered.values())), 1, rendered)
        self.assertEqual(len({coding_task_contract_digest(c) for c in contracts.values()}), 1)
        for owner, contract in contracts.items():
            with self.subTest(owner=owner):
                self.assertEqual(validate_coding_task_contract(contract), [])
                self.assertEqual(sorted(contract), sorted(TASK_CONTRACT_KEYS))
                self.assertEqual(contract["schema_version"], CODING_TASK_CONTRACT_SCHEMA_VERSION)

    def test_retargeting_across_the_three_handoff_kinds_preserves_the_contract(self) -> None:
        moves = (
            ("codex", "claude-code"),
            ("codex", "hermes"),
            ("claude-code", "codex"),
            ("claude-code", "omx-runtime"),
            ("hermes", "codex"),
            ("hermes", "claude-code"),
        )
        digests: set[str] = set()
        for source, target in moves:
            with self.subTest(source=source, target=target):
                from_payload = _payload(source)
                to_payload = _payload(target)
                record = build_owner_retarget(
                    from_payload=from_payload,
                    to_payload=to_payload,
                    task_source_sha256=TASK_SHA,
                )
                self.assertEqual(validate_owner_retarget(record), [])
                self.assertEqual(sorted(record), sorted(OWNER_RETARGET_KEYS))
                # The preserved contract is byte-identical to BOTH sides'
                # projections, not merely to the target's.
                self.assertEqual(
                    _canonical(record["preserved"]),
                    _canonical(coding_task_contract(from_payload, task_source_sha256=TASK_SHA)),
                )
                self.assertEqual(
                    _canonical(record["preserved"]),
                    _canonical(coding_task_contract(to_payload, task_source_sha256=TASK_SHA)),
                )
                # And the four things AC1 names by hand.
                for field in ("acceptance_criteria", "verification"):
                    self.assertEqual(
                        record["preserved"][field],
                        from_payload["delegation"][field],
                    )
                    self.assertEqual(record["preserved"][field], to_payload["delegation"][field])
                self.assertEqual(record["preserved"]["recommended_workflow"], ROUTED_WORKFLOW)
                self.assertEqual(record["preserved"]["review_required"], from_payload["delegation"]["review_required"])
                self.assertEqual(record["from_owner"], source)
                self.assertEqual(record["to_owner"], target)
                self.assertEqual(record["from_owner_label"], executor_label(source))
                self.assertEqual(record["to_owner_label"], executor_label(target))
                self.assertEqual(record["claim_boundary"], OWNER_RETARGET_CLAIM_BOUNDARY)
                digests.add(str(record["preserved_digest"]))
        self.assertEqual(len(digests), 1, "one accepted task must have one preserved digest")

    def test_a_move_that_would_change_the_task_contract_is_refused_as_a_replan(self) -> None:
        replanned = _payload("claude-code", OTHER_MESSAGE)
        self.assertEqual(replanned["delegation"]["action"], "delegate")
        with self.assertRaises(OwnerRetargetError) as caught:
            build_owner_retarget(from_payload=_payload("codex"), to_payload=replanned)
        message = str(caught.exception)
        self.assertIn("replan rather than a retarget", message)
        self.assertIn("intent", message)
        self.assertIn("recommended_workflow", message)
        self.assertIn("acceptance_criteria", message)

    def test_a_retarget_is_deterministic_and_carries_no_wall_clock(self) -> None:
        first = build_owner_retarget(
            from_payload=_payload("codex"),
            to_payload=_payload("hermes"),
            task_source_sha256=TASK_SHA,
        )
        second = build_owner_retarget(
            from_payload=_payload("codex"),
            to_payload=_payload("hermes"),
            task_source_sha256=TASK_SHA,
        )
        self.assertEqual(_canonical(first), _canonical(second))


class OwnerDeltaTests(unittest.TestCase):
    """Only the owner-specific half moves, and all of it is named."""

    def test_every_owner_specific_field_lands_in_exactly_one_side_of_the_delta(self) -> None:
        record = build_owner_retarget(from_payload=_payload("codex"), to_payload=_payload("hermes"))
        delta = record["owner_delta"]
        self.assertEqual(sorted(delta), sorted(OWNER_DELTA_KEYS))
        named = [str(entry["field"]) for entry in delta["changed"]] + [str(f) for f in delta["unchanged"]]
        self.assertEqual(sorted(named), sorted(OWNER_SPECIFIC_FIELDS))
        changed = {str(entry["field"]): entry for entry in delta["changed"]}
        self.assertEqual(changed["work_owner_mode"]["from"], "external_executor")
        self.assertEqual(changed["work_owner_mode"]["to"], "runtime_handoff")
        self.assertEqual(changed["handoff_field"]["from"], "executor_handoff")
        self.assertEqual(changed["handoff_field"]["to"], "runtime_handoff")
        self.assertEqual(changed["dispatchable"]["from"], "true")
        self.assertEqual(changed["dispatchable"]["to"], "false")

    def test_changed_execution_primitives_are_reported_in_both_directions(self) -> None:
        record = build_owner_retarget(from_payload=_payload("codex"), to_payload=_payload("hermes"))
        delta = record["owner_delta"]
        self.assertEqual(
            delta["observed_evidence_gained"],
            ["runtime_start", "worktree_creation", "worker_dispatch", "worker_result"],
        )
        self.assertEqual(delta["observed_evidence_lost"], ["executor_dispatch", "executor_result"])
        self.assertIn("send_to_codex", delta["wrapper_actions_lost"])
        self.assertIn("start_team", delta["wrapper_actions_gained"])

        back = build_owner_retarget(from_payload=_payload("hermes"), to_payload=_payload("codex"))
        self.assertEqual(back["owner_delta"]["observed_evidence_lost"], delta["observed_evidence_gained"])
        self.assertEqual(back["owner_delta"]["observed_evidence_gained"], delta["observed_evidence_lost"])


class CapabilityDeltaTests(unittest.TestCase):
    """AC2: unsupported or changed owner capability is named before the handoff."""

    def _retarget(self, *, from_owner: str, to_owner: str, snapshots: dict[str, Any]) -> dict[str, Any]:
        return build_owner_retarget(
            from_payload=_payload(from_owner),
            to_payload=_payload(to_owner),
            task_source_sha256=TASK_SHA,
            capability_snapshots=snapshots,
            now=RECORDED_NOW,
        )

    def test_a_capability_gap_on_the_new_owner_is_named_and_gates_the_next_action(self) -> None:
        record = self._retarget(
            from_owner="codex",
            to_owner="hermes",
            snapshots={"hermes": _local_workflow_snapshot("hermes", status="unavailable"), "codex": None},
        )
        delta = record["capability_delta"]
        self.assertEqual(sorted(delta), sorted(CAPABILITY_DELTA_KEYS))
        self.assertEqual(delta["requirements"], ["local_workflow"])
        # The requirement itself is created by the owner change: an
        # external-executor owner never had to carry the workflow locally.
        self.assertEqual(delta["required_by_owner_change"], ["local_workflow"])
        self.assertEqual(delta["unsupported"], ["local_workflow"])
        self.assertEqual(delta["to_verdict"], "blocked")
        self.assertIn("local_workflow", delta["statement"])
        self.assertEqual(record["next_action"], "confirm_owner_capability_gap")
        self.assertIn("local_workflow", record["reason"])

    def test_a_fit_new_owner_does_not_have_a_gap_invented_for_it(self) -> None:
        record = self._retarget(
            from_owner="codex",
            to_owner="hermes",
            snapshots={"hermes": _local_workflow_snapshot("hermes", status="host_observed"), "codex": None},
        )
        delta = record["capability_delta"]
        self.assertEqual(delta["requirements"], ["local_workflow"])
        self.assertEqual(delta["unsupported"], [])
        self.assertEqual(delta["unproven"], [])
        self.assertEqual(delta["to_verdict"], "ready")
        self.assertEqual(record["next_action"], "prepare_handoff_for_new_owner")
        self.assertNotIn("unavailable", delta["statement"])

    def test_absent_evidence_reads_unproven_rather_than_fit_or_blocked(self) -> None:
        record = self._retarget(from_owner="codex", to_owner="hermes", snapshots={})
        delta = record["capability_delta"]
        self.assertEqual(delta["unsupported"], [])
        self.assertEqual(delta["unproven"], ["local_workflow"])
        self.assertEqual(delta["to_verdict"], "unproven")
        self.assertEqual(record["next_action"], "record_capability_evidence")

    def test_evidence_that_moved_between_the_two_owners_is_reported_as_changed(self) -> None:
        record = self._retarget(
            from_owner="omx-runtime",
            to_owner="hermes",
            snapshots={
                "omx-runtime": _local_workflow_snapshot("omx-runtime", status="host_observed"),
                "hermes": _local_workflow_snapshot("hermes", status="unavailable"),
            },
        )
        delta = record["capability_delta"]
        # Both owners are runtime owners, so the requirement set does not move
        # and only the evidence behind it does.
        self.assertEqual(delta["required_by_owner_change"], [])
        self.assertEqual(delta["dropped_by_owner_change"], [])
        self.assertEqual(
            delta["changed"],
            [{"capability": "local_workflow", "from_classification": "met", "to_classification": "unmet"}],
        )
        self.assertEqual(delta["from_verdict"], "ready")
        self.assertEqual(delta["to_verdict"], "blocked")

    def test_a_requirement_dropped_by_the_owner_change_is_reported(self) -> None:
        record = self._retarget(
            from_owner="hermes",
            to_owner="codex",
            snapshots={"hermes": _local_workflow_snapshot("hermes", status="host_observed")},
        )
        delta = record["capability_delta"]
        self.assertEqual(delta["requirements"], [])
        self.assertEqual(delta["dropped_by_owner_change"], ["local_workflow"])
        self.assertEqual(delta["to_verdict"], "ready")
        self.assertEqual(record["next_action"], "prepare_handoff_for_new_owner")

    def test_the_gap_is_available_before_anything_is_prepared_or_written(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = self._retarget(
                from_owner="codex",
                to_owner="hermes",
                snapshots={"hermes": _local_workflow_snapshot("hermes", status="unavailable")},
            )
            self.assertEqual(record["capability_delta"]["unsupported"], ["local_workflow"])
            # A pure projection: enumerating the gap creates no artifact, so it
            # cannot be something a person only discovers after accepting the
            # new handoff.
            self.assertEqual(sorted(path.name for path in root.iterdir()), [])


class OwnerRetargetValidatorTests(unittest.TestCase):
    """The closed key set is enforced in both directions."""

    def _record(self) -> dict[str, Any]:
        return build_owner_retarget(
            from_payload=_payload("codex"),
            to_payload=_payload("claude-code"),
            task_source_sha256=TASK_SHA,
        )

    def test_a_missing_key_and_an_extra_key_both_fail(self) -> None:
        for key in OWNER_RETARGET_KEYS:
            with self.subTest(missing=key):
                record = self._record()
                record.pop(key)
                self.assertTrue(
                    any("missing required keys" in error for error in validate_owner_retarget(record)),
                    key,
                )
        record = self._record()
        record["surprise"] = "extra"
        self.assertIn(
            "owner retarget contains unsupported keys: surprise",
            validate_owner_retarget(record),
        )

    def test_the_preserved_digest_must_be_recomputable_from_the_preserved_contract(self) -> None:
        record = self._record()
        record["preserved"] = {**record["preserved"], "intent": "something-else"}
        self.assertIn(
            "owner retarget preserved_digest must be the digest of the preserved task contract",
            validate_owner_retarget(record),
        )

    def test_the_next_action_must_follow_from_the_enumerated_gap(self) -> None:
        record = self._record()
        self.assertEqual(record["next_action"], "prepare_handoff_for_new_owner")
        record["next_action"] = "confirm_owner_capability_gap"
        self.assertIn("confirm_owner_capability_gap", OWNER_RETARGET_NEXT_ACTIONS)
        self.assertIn(
            "owner retarget next_action must follow from the enumerated capability delta",
            validate_owner_retarget(record),
        )

    def test_an_unmoved_field_cannot_be_reported_as_a_change(self) -> None:
        record = self._record()
        record["owner_delta"] = {
            **record["owner_delta"],
            "changed": [
                *record["owner_delta"]["changed"],
                {"field": "isolation_strategy", "from": "same", "to": "same"},
            ],
            "unchanged": [],
        }
        errors = validate_owner_retarget(record)
        self.assertIn("owner delta change must record a field that actually moved", errors)

    def test_the_schema_version_status_and_claim_boundary_are_pinned(self) -> None:
        record = self._record()
        self.assertEqual(record["schema_version"], OWNER_RETARGET_SCHEMA_VERSION)
        self.assertEqual(record["status"], "prepared_not_observed")
        for field, value in (
            ("schema_version", "coding_owner_retarget/v2"),
            ("status", "observed"),
            ("claim_boundary", "retargeting proves the new owner started the work"),
        ):
            with self.subTest(field=field):
                tampered = {**self._record(), field: value}
                self.assertTrue(validate_owner_retarget(tampered))

    def test_the_claim_boundary_denies_dispatch_and_execution(self) -> None:
        boundary = OWNER_RETARGET_CLAIM_BOUNDARY.lower()
        for denied in ("dispatch", "execution", "verification", "review", "ci", "merge"):
            with self.subTest(denied=denied):
                self.assertIn(denied, boundary)
        self.assertIn("never replans", boundary)


class OwnerRetargetGuardTests(unittest.TestCase):
    """What a retarget refuses, and what it must leave alone."""

    def test_an_unknown_owner_is_refused(self) -> None:
        unknown = {**_payload("claude-code"), "selected_executor_profile": "mystery-agent"}
        with self.assertRaises(OwnerRetargetError) as caught:
            build_owner_retarget(from_payload=_payload("codex"), to_payload=unknown)
        self.assertIn("unsupported coding owner", str(caught.exception))
        self.assertIn("mystery-agent", str(caught.exception))
        self.assertNotIn("mystery-agent", EXECUTOR_PROFILES)

    def test_retargeting_to_the_same_owner_is_refused(self) -> None:
        with self.assertRaises(OwnerRetargetError) as caught:
            build_owner_retarget(from_payload=_payload("codex"), to_payload=_payload("codex"))
        self.assertIn("requires a different coding owner", str(caught.exception))

    def test_a_payload_with_no_prepared_handoff_is_refused(self) -> None:
        without_handoff = {key: value for key, value in _payload("codex").items() if key != "executor_handoff"}
        with self.assertRaises(OwnerRetargetError) as caught:
            build_owner_retarget(from_payload=without_handoff, to_payload=_payload("claude-code"))
        self.assertIn("no prepared handoff", str(caught.exception))

    def test_building_a_retarget_does_not_mutate_either_payload(self) -> None:
        from_payload = _payload("codex")
        to_payload = _payload("hermes")
        before = (_canonical(from_payload), _canonical(to_payload))
        build_owner_retarget(from_payload=from_payload, to_payload=to_payload, task_source_sha256=TASK_SHA)
        self.assertEqual((_canonical(from_payload), _canonical(to_payload)), before)


class WrapperSessionRetargetTests(unittest.TestCase):
    """The wrapper surface: the guard relaxed into a named operation."""

    def test_a_follow_up_handoff_without_an_explicit_retarget_still_keeps_its_executor(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            session_id = _accepted_session(paths, "codex")
            with self.assertRaises(WrapperSessionError) as caught:
                prepare_wrapper_session_handoff(
                    paths,
                    session_id,
                    "also add a changelog entry",
                    executor_target="claude-code",
                )
            self.assertIn("a follow-up handoff keeps the selected executor", str(caught.exception))
            session = json.loads(
                (paths.runtime_wrapper_sessions_dir / session_id / "session.json").read_text(encoding="utf-8")
            )
            self.assertEqual(session["selected_executor_profile"], "codex")
            self.assertEqual(session["status"], "handoff_prepared")

    def test_an_explicit_retarget_moves_the_owner_and_preserves_the_contract(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            session_id = _accepted_session(paths, "codex")
            digests: list[str] = []
            preserved: list[str] = []
            for target, expected_status in (
                ("claude-code", "prompt_handoff_prepared"),
                ("hermes", "runtime_handoff_prepared"),
                ("codex", "handoff_prepared"),
            ):
                with self.subTest(target=target):
                    result = prepare_wrapper_session_handoff(
                        paths,
                        session_id,
                        MESSAGE,
                        executor_target=target,
                        retarget=True,
                    )
                    record = result["coding_owner_retarget"]
                    self.assertEqual(validate_owner_retarget(record), [])
                    self.assertEqual(record["to_owner"], target)
                    # The same routed workflow the pure projection derives: a
                    # retarget routes the accepted task exactly as the prepare
                    # it replaces did.
                    self.assertEqual(record["preserved"]["recommended_workflow"], ROUTED_WORKFLOW)
                    self.assertEqual(result["session"]["selected_executor_profile"], target)
                    self.assertEqual(result["session"]["status"], expected_status)
                    digests.append(str(record["preserved_digest"]))
                    preserved.append(_canonical(record["preserved"]))
            # AC1 through the real session, by equality: three owner kinds, one
            # unchanged accepted contract.
            self.assertEqual(len(set(digests)), 1, digests)
            self.assertEqual(len(set(preserved)), 1)

    def test_the_retarget_is_journalled_with_its_gap_and_its_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            session_id = _accepted_session(paths, "codex")
            _write_snapshot(paths, _local_workflow_snapshot("hermes", status="unavailable"))
            result = prepare_wrapper_session_handoff(
                paths, session_id, MESSAGE, executor_target="hermes", retarget=True
            )
            self.assertEqual(result["coding_owner_retarget"]["capability_delta"]["unsupported"], ["local_workflow"])
            self.assertEqual(result["coding_owner_retarget"]["next_action"], "confirm_owner_capability_gap")
            journalled = [event for event in _session_events(paths, session_id) if event["event"] == "coding_owner_retargeted"]
            self.assertEqual(len(journalled), 1)
            recorded = journalled[0]["data"]
            self.assertEqual(validate_owner_retarget(recorded), [])
            self.assertEqual(recorded["from_owner"], "codex")
            self.assertEqual(recorded["to_owner"], "hermes")
            self.assertEqual(recorded["claim_boundary"], OWNER_RETARGET_CLAIM_BOUNDARY)
            self.assertEqual(_canonical(recorded), _canonical(result["coding_owner_retarget"]))

    def test_the_original_prepared_handoff_artifact_is_unchanged_byte_for_byte(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            session_id = _accepted_session(paths, "codex")
            session = json.loads(
                (paths.runtime_wrapper_sessions_dir / session_id / "session.json").read_text(encoding="utf-8")
            )
            original = paths.runtime_runs_dir / str(session["current_run_id"]) / "coding_delegation.json"
            before = original.read_bytes()

            result = prepare_wrapper_session_handoff(
                paths, session_id, MESSAGE, executor_target="claude-code", retarget=True
            )

            self.assertEqual(original.read_bytes(), before)
            # And what the retarget calls "preserved" is what the original
            # handoff actually recorded, not a freshly re-derived plan that
            # merely agrees with itself.
            recorded = json.loads(before.decode("utf-8"))
            preserved = result["coding_owner_retarget"]["preserved"]
            for stored_field, contract_field in (
                ("action", "action"),
                ("intent", "intent"),
                ("recommended_workflow", "recommended_workflow"),
                ("recommended_harness", "recommended_harness"),
                ("executor_profile", "work_role"),
                ("acceptance_criteria", "acceptance_criteria"),
                ("verification", "verification"),
                ("review_required", "review_required"),
            ):
                with self.subTest(field=stored_field):
                    self.assertEqual(recorded[stored_field], preserved[contract_field])

    def test_the_wrapper_refuses_a_retarget_it_cannot_honour(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            session_id = _accepted_session(paths, "codex")
            for kwargs, expected in (
                ({"executor_target": "codex", "retarget": True}, "already targeted at codex"),
                ({"retarget": True}, "retargeting requires the coding owner"),
                ({"executor_target": "mystery-agent", "retarget": True}, "unsupported wrapper session executor"),
                ({"executor_target": "choose", "retarget": True}, "unsupported wrapper session executor"),
            ):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(WrapperSessionError) as caught:
                        prepare_wrapper_session_handoff(paths, session_id, MESSAGE, **kwargs)
                    self.assertIn(expected, str(caught.exception))

    def test_a_session_with_no_prepared_handoff_cannot_be_retargeted(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            started = create_or_resume_wrapper_session(
                paths,
                MESSAGE,
                source="discord",
                source_metadata={"source_event_id": "m1", "channel_ref": "c1"},
                executor_target="codex",
            )
            session_id = str(started["session"]["session_id"])
            record_plan_decision(paths, session_id, "accept")
            with self.assertRaises(WrapperSessionError) as caught:
                prepare_wrapper_session_handoff(
                    paths, session_id, MESSAGE, executor_target="claude-code", retarget=True
                )
            self.assertIn("there is no prepared handoff to re-project", str(caught.exception))


class RetargetNeedsNoHostCredentialTests(unittest.TestCase):
    """AC3: retargeting is a local re-projection, not a host-config read."""

    def test_a_retarget_completes_with_every_credential_and_config_reader_raising(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            session_id = _accepted_session(paths, "codex")

            def _refuse(*args: Any, **kwargs: Any) -> Any:
                raise AssertionError("retargeting must not read source-host configuration or credentials")

            patches = (
                mock.patch("omh.coding.executor_auth_signals.executor_auth_signals", _refuse),
                mock.patch("omh.coding.executor_auth_signals.auth_signal_for_profile", _refuse),
                mock.patch("omh.coding.executor_auth_signals.last_limit_signal_for_profile", _refuse),
                mock.patch("omh.coding.executor_readiness.live_readiness_binding", _refuse),
                mock.patch.object(Path, "home", staticmethod(_refuse)),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                # The patches really do refuse, so a pass below is the retarget
                # never reaching them rather than the patch never applying.
                with self.assertRaises(AssertionError):
                    Path.home()
                result = prepare_wrapper_session_handoff(
                    paths, session_id, MESSAGE, executor_target="claude-code", retarget=True
                )

            record = result["coding_owner_retarget"]
            self.assertEqual(validate_owner_retarget(record), [])
            self.assertEqual(record["from_owner"], "codex")
            self.assertEqual(record["to_owner"], "claude-code")
            self.assertEqual(result["session"]["status"], "prompt_handoff_prepared")


if __name__ == "__main__":
    unittest.main()

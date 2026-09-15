from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
from omh.paths import resolve_paths
from omh.runtime.artifacts import (
    create_run,
    show_run,
    summarize_runtime_observation_status,
    write_runtime_observation,
)
from omh.runtime.records import RUNTIME_OBSERVATION_SCHEMA_VERSION
from omh.workflow_learning import (
    WorkflowLearningError,
    build_improvement_candidate,
    build_learning_export_bundle,
    build_runtime_run_learning_record,
    build_workflow_eval_result,
    list_learning_traces,
    validate_workflow_learning_export,
    validate_workflow_learning_trace,
    write_improvement_candidate,
    write_learning_trace,
    write_workflow_eval,
)
from omh.workflows.runtime_learning_recap import (
    OBSERVED_COMPLETION_STATES,
    OPERATOR_OUTCOMES,
    RECAP_CELL_STATES,
    RUNTIME_LEARNING_RECAP_CELLS,
    RUNTIME_LEARNING_RECAP_SCHEMA_VERSION,
    build_runtime_learning_recap,
    list_runtime_learning_recaps,
    runtime_learning_recap_errors,
    show_runtime_learning_recap,
    validate_runtime_learning_recap,
    write_runtime_learning_recap,
)


def _observation(run_id: str, event_type: str, status: str, **extra) -> dict:
    observation = {
        "target_type": "run",
        "target_id": run_id,
        "runtime_profile": "hermes",
        "event_type": event_type,
        "status": status,
        "summary": f"{event_type} {status}",
    }
    observation.update(extra)
    return observation


class RuntimeLearningRecapTest(unittest.TestCase):
    def _paths(self, root: Path):
        return resolve_paths(omh_home=root / "omh", hermes_home=root / "hermes")

    def _run(self, paths) -> str:
        run = create_run(paths, {"skill": "execute", "harness": "hermes", "status": "prepared"})
        return str(run["run_id"])

    def _observe(self, paths, run_id: str, event_type: str, status: str = "observed", **extra) -> None:
        write_runtime_observation(
            paths.runtime_runs_dir / run_id,
            _observation(run_id, event_type, status, **extra),
        )

    def test_operator_outcome_never_upgrades_observed_completion(self) -> None:
        """The issue's sharpest case: a claimed success with nothing observed."""
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            recap = build_runtime_learning_recap(
                paths,
                run_id,
                operator_outcome="useful",
                operator_feedback_summary="it all worked",
            )
            self.assertEqual(recap["operator_assessment"]["operator_outcome"], "useful")
            self.assertEqual(recap["operator_assessment"]["authority"], "operator_supplied")
            self.assertIn(recap["observed_completion"]["state"], {"unknown", "partial"})
            self.assertEqual(recap["observed_completion"]["state"], "unknown")
            self.assertEqual(recap["observed_completion"]["observed_cells"], [])
            for cell in RUNTIME_LEARNING_RECAP_CELLS:
                self.assertEqual(recap["evidence_cells"][cell]["state"], "unavailable")
            for field in ("commit", "changed_paths", "pull_request", "merge_commit"):
                self.assertEqual(recap["identity"][field]["state"], "unavailable")

    def test_operator_outcome_vocabulary_matches_the_trace_vocabulary(self) -> None:
        """Two modules, one vocabulary. A drift would silently split the surfaces."""
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            for outcome in OPERATOR_OUTCOMES:
                build_runtime_run_learning_record(paths, run_id, outcome=outcome)
            with self.assertRaises(WorkflowLearningError):
                build_runtime_learning_recap(paths, run_id, operator_outcome="success")

    def test_wrapper_summary_and_prepared_state_are_not_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            run_dir = paths.runtime_runs_dir / run_id
            (run_dir / "wrapper.json").write_text(
                json.dumps({"schema_version": "runtime_record/v1", "summary": "everything shipped", "completion_status": "completed"}),
                encoding="utf-8",
            )
            recap = build_runtime_learning_recap(paths, run_id, operator_outcome="useful")
            self.assertEqual(recap["observed_completion"]["state"], "unknown")
            self.assertEqual(recap["operator_assessment"]["operator_feedback_summary"], "")
            self.assertNotIn("everything shipped", json.dumps(recap))

    def test_cells_are_separate_and_name_their_supporting_observation(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            self._observe(paths, run_id, "worker_result", worker_ref="worker-1", evidence_refs=["commit:abc1234"])
            self._observe(paths, run_id, "ci", "failed", evidence_refs=["ci-run-9"])
            recap = build_runtime_learning_recap(paths, run_id)
            cells = recap["evidence_cells"]
            self.assertEqual(cells["delivery"]["state"], "observed")
            self.assertEqual(cells["delivery"]["source_observation_type"], "worker_result")
            self.assertEqual(cells["delivery"]["source_observation_schema"], RUNTIME_OBSERVATION_SCHEMA_VERSION)
            self.assertEqual(cells["ci"]["state"], "failed")
            self.assertEqual(cells["ci"]["source_observation_type"], "ci")
            for cell in ("verification", "review", "pull_request", "merge_readiness", "merge"):
                self.assertEqual(cells[cell]["state"], "unavailable")
            self.assertEqual(recap["observed_completion"]["state"], "failed")
            self.assertEqual(recap["observed_completion"]["failed_cells"], ["ci"])

    def test_blocked_observation_is_unavailable_not_failed(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            self._observe(paths, run_id, "review", "blocked", evidence_refs=["review-1"])
            recap = build_runtime_learning_recap(paths, run_id)
            self.assertEqual(recap["evidence_cells"]["review"]["state"], "unavailable")
            self.assertEqual(recap["evidence_cells"]["review"]["observation_status"], "blocked")
            self.assertEqual(recap["evidence_cells"]["review"]["source_observation_type"], "review")
            self.assertEqual(recap["observed_completion"]["state"], "unknown")

    def test_a_corrective_failure_in_the_same_second_is_not_discarded(self) -> None:
        """The realistic collision: `utc_now` has second resolution, so two
        records for one milestone routinely share a timestamp. Whichever was
        appended last speaks for the milestone, in both directions, and the
        recap must reach the same verdict as the runtime status projection.
        """
        for order, expected in ((("observed", "failed"), "failed"), (("failed", "observed"), "observed")):
            with self.subTest(order=order):
                with TemporaryDirectory() as tmp:
                    paths = self._paths(Path(tmp))
                    run_id = self._run(paths)
                    for status in order:
                        self._observe(
                            paths,
                            run_id,
                            "merge",
                            status,
                            updated_at="2026-09-15T03:13:42Z",
                            evidence_refs=[f"merge-{status}"],
                        )
                    recap = build_runtime_learning_recap(paths, run_id)
                    cell = recap["evidence_cells"]["merge"]
                    self.assertEqual(cell["observation_status"], expected)
                    runtime = summarize_runtime_observation_status(
                        show_run(paths, run_id, history_limit=None)["runtime_observations"]
                    )
                    if expected == "observed":
                        self.assertEqual(runtime["observed_events"], ["merge"])
                        self.assertEqual(cell["state"], "observed")
                        self.assertEqual(recap["observed_completion"]["state"], "completed")
                    else:
                        self.assertEqual(runtime["failed_events"], ["merge"])
                        self.assertEqual(cell["state"], "failed")
                        self.assertEqual(recap["observed_completion"]["state"], "failed")

    def test_a_same_second_tie_still_builds_byte_identically(self) -> None:
        """Append order decides the tie, and append order is stable, so the
        revision stays idempotent rather than trading one bias for a coin flip.
        """
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            for status in ("observed", "failed"):
                self._observe(
                    paths,
                    run_id,
                    "merge",
                    status,
                    updated_at="2026-09-15T03:13:42Z",
                    evidence_refs=[f"merge-{status}"],
                )
            builds = {json.dumps(build_runtime_learning_recap(paths, run_id), sort_keys=True) for _ in range(10)}
            self.assertEqual(len(builds), 1)
            self.assertEqual(json.loads(builds.pop())["evidence_cells"]["merge"]["state"], "failed")

    def test_merge_observed_completes_and_earlier_stages_stay_unavailable(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            self._observe(paths, run_id, "merge", evidence_refs=["merge_commit:deadbee", "pr:647"])
            recap = build_runtime_learning_recap(paths, run_id)
            self.assertEqual(recap["observed_completion"]["state"], "completed")
            self.assertEqual(recap["identity"]["merge_commit"]["value"], "deadbee")
            self.assertEqual(recap["identity"]["pull_request"]["value"], "647")
            self.assertEqual(recap["identity"]["commit"]["state"], "unavailable")
            self.assertEqual(recap["identity"]["changed_paths"]["state"], "unavailable")
            self.assertEqual(recap["evidence_cells"]["ci"]["state"], "unavailable")

    def test_identity_fields_require_their_own_typed_reference(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            self._observe(paths, run_id, "worker_result", worker_ref="worker-1", evidence_refs=["worker-log-1"])
            recap = build_runtime_learning_recap(paths, run_id)
            self.assertEqual(recap["evidence_cells"]["delivery"]["state"], "observed")
            self.assertEqual(recap["identity"]["commit"]["state"], "unavailable")
            self.assertEqual(recap["identity"]["commit"]["value"], "")
            self.assertEqual(recap["identity"]["changed_paths"]["count"], 0)

    def test_changed_paths_stay_repository_relative(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            self._observe(
                paths,
                run_id,
                "worker_result",
                worker_ref="worker-1",
                evidence_refs=[
                    "changed_path:src/workflows/runtime_learning_recap.py",
                    "changed_path:/Users/someone/private/notes.txt",
                    "changed_path:../outside/tree.py",
                ],
            )
            recap = build_runtime_learning_recap(paths, run_id)
            changed = recap["identity"]["changed_paths"]
            self.assertEqual(changed["paths"], ["src/workflows/runtime_learning_recap.py"])
            self.assertEqual(changed["dropped_count"], 2)

    def test_persisted_recap_carries_no_raw_or_absolute_material(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            self._observe(
                paths,
                run_id,
                "worker_result",
                worker_ref="worker-1",
                evidence_refs=[
                    "github_pr_created:https://github.com/rlaope/oh-my-hermes/pull/123",
                    "/Users/someone/private/transcript.log",
                    "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123",
                    "C:\\Users\\someone\\run.log",
                ],
            )
            recap = build_runtime_learning_recap(
                paths,
                run_id,
                operator_outcome="useful",
                operator_feedback_summary="ran `pytest -q`\nsee /Users/someone/private/transcript.log",
            )
            write_runtime_learning_recap(paths, recap)
            stored = (paths.learning_recaps_dir / f"{recap['recap_id']}.json").read_text(encoding="utf-8")
            for leaked in (
                "https://github.com",
                "/Users/someone",
                "ghp_abcdefghijklmnopqrstuvwxyz0123",
                "C:\\Users",
                "pytest -q",
                "transcript.log",
            ):
                self.assertNotIn(leaked, stored)
            self.assertEqual(recap["operator_assessment"]["operator_feedback_summary"], "[redacted]")
            self.assertTrue(recap["operator_assessment"]["operator_feedback_redacted"])
            for ref in recap["evidence_cells"]["delivery"]["evidence_refs"]:
                self.assertTrue(ref.startswith("ref-"), ref)

    def test_revision_is_idempotent_and_a_later_observation_adds_one(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            first = build_runtime_learning_recap(paths, run_id, operator_outcome="useful")
            again = build_runtime_learning_recap(paths, run_id, operator_outcome="useful")
            self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(again, sort_keys=True))
            write_runtime_learning_recap(paths, first)
            write_runtime_learning_recap(paths, again)
            self.assertEqual(len(list(paths.learning_recaps_dir.glob("*.json"))), 1)

            self._observe(paths, run_id, "worker_result", worker_ref="worker-1", evidence_refs=["commit:abc1234"])
            later = build_runtime_learning_recap(paths, run_id, operator_outcome="useful")
            self.assertNotEqual(later["recap_id"], first["recap_id"])
            self.assertNotEqual(
                later["revision"]["runtime_history_digest"],
                first["revision"]["runtime_history_digest"],
            )
            write_runtime_learning_recap(paths, later)
            # The reviewed revision keeps the evidence basis it was reviewed on.
            preserved = show_runtime_learning_recap(paths, first["recap_id"])
            self.assertEqual(preserved["observed_completion"]["state"], "unknown")
            self.assertEqual(preserved["revision"]["observation_count"], 0)
            revisions = list_runtime_learning_recaps(paths, run_id=run_id)
            self.assertEqual([item["observed_completion"] for item in revisions], ["unknown", "partial"])

    def test_foreign_observations_are_not_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            write_runtime_observation(
                paths.runtime_runs_dir / run_id,
                _observation("some-other-run", "merge", "observed", evidence_refs=["merge_commit:deadbee"]),
            )
            recap = build_runtime_learning_recap(paths, run_id)
            self.assertEqual(recap["observed_completion"]["state"], "unknown")
            self.assertEqual(recap["revision"]["observation_count"], 0)
            self.assertEqual(recap["revision"]["rejected_observation_count"], 1)

    def test_validation_refuses_every_shape_the_contract_forbids(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            self._observe(paths, run_id, "worker_result", worker_ref="worker-1", evidence_refs=["commit:abc1234"])
            recap = build_runtime_learning_recap(paths, run_id)
            self.assertEqual(runtime_learning_recap_errors(recap), [])

            foreign = json.loads(json.dumps(recap))
            foreign["run_id"] = "some-other-run"
            self.assertIn("bind", " ".join(runtime_learning_recap_errors(foreign)))

            mismatched = json.loads(json.dumps(recap))
            mismatched["observed_completion"]["observed_cells"] = list(RUNTIME_LEARNING_RECAP_CELLS)
            self.assertIn("does not follow from the evidence cells", " ".join(runtime_learning_recap_errors(mismatched)))

            malformed = json.loads(json.dumps(recap))
            malformed["evidence_cells"]["review"]["state"] = "probably_fine"
            self.assertIn("state is invalid", " ".join(runtime_learning_recap_errors(malformed)))

            oversized = json.loads(json.dumps(recap))
            oversized["evidence_cells"]["delivery"]["evidence_refs"] = [f"ref-{index:012d}" for index in range(40)]
            self.assertIn("too many evidence references", " ".join(runtime_learning_recap_errors(oversized)))

            long_feedback = json.loads(json.dumps(recap))
            long_feedback["operator_assessment"]["operator_feedback_summary"] = "x" * 5000
            self.assertIn("bounded string", " ".join(runtime_learning_recap_errors(long_feedback)))

            unsupported = json.loads(json.dumps(recap))
            unsupported["schema_version"] = "runtime_learning_recap/v2"
            errors = runtime_learning_recap_errors(unsupported)
            self.assertEqual(len(errors), 1)
            self.assertIn("unsupported runtime learning recap schema version", errors[0])

            leaking = json.loads(json.dumps(recap))
            leaking["evidence_cells"]["delivery"]["reason"] = "see /Users/someone/private/notes.txt"
            self.assertIn("unsafe text", " ".join(runtime_learning_recap_errors(leaking)))

            raw = json.loads(json.dumps(recap))
            raw["transcript"] = "whatever"
            self.assertIn("forbidden field", " ".join(runtime_learning_recap_errors(raw)))

            with self.assertRaises(WorkflowLearningError):
                validate_runtime_learning_recap(foreign)

    def test_a_cell_cannot_be_edited_away_from_its_own_observation(self) -> None:
        """The recap id binds the observation history, not the projection over it,
        so the cells have to be checkable from the fields they carry.
        """
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            self._observe(paths, run_id, "merge", "failed", evidence_refs=["merge-failed"])
            recap = build_runtime_learning_recap(paths, run_id)
            self.assertEqual(recap["observed_completion"]["state"], "failed")

            # Flip the cell and keep the completion block self-consistent, the
            # way a hand edit would.
            tampered = json.loads(json.dumps(recap))
            tampered["evidence_cells"]["merge"]["state"] = "observed"
            tampered["observed_completion"]["state"] = "completed"
            tampered["observed_completion"]["reason"] = "merge observed and no cell reports a failure"
            tampered["observed_completion"]["observed_cells"] = ["merge"]
            tampered["observed_completion"]["failed_cells"] = []
            self.assertEqual(tampered["recap_id"], recap["recap_id"])
            errors = " ".join(runtime_learning_recap_errors(tampered))
            self.assertIn("does not follow from observation status failed", errors)
            with self.assertRaises(WorkflowLearningError):
                validate_runtime_learning_recap(tampered)

            prose = json.loads(json.dumps(recap))
            prose["evidence_cells"]["merge"]["reason"] = "the operator said this one was fine"
            self.assertIn(
                "reason is not one this projection produces",
                " ".join(runtime_learning_recap_errors(prose)),
            )

            unsupported = json.loads(json.dumps(recap))
            unsupported["evidence_cells"]["ci"]["state"] = "observed"
            self.assertIn(
                "claims observed with no supporting observation",
                " ".join(runtime_learning_recap_errors(unsupported)),
            )

    def test_a_terminal_failed_observation_is_not_work_in_progress(self) -> None:
        """`failed` and `cancelled` are event types as well as status values, and
        the milestone ladder excludes them. Reading only the ladder left a run
        that recorded exactly one terminal failure reading as unfinished.
        """
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            self._observe(paths, run_id, "worker_result", worker_ref="worker-1", evidence_refs=["commit:abc1234"])
            self._observe(paths, run_id, "failed", evidence_refs=["runner-exit"])
            recap = build_runtime_learning_recap(paths, run_id)
            self.assertEqual(recap["observed_completion"]["state"], "failed")
            self.assertEqual(recap["observed_completion"]["run_termination"], "failed")
            self.assertEqual(recap["run_lifecycle"]["termination"]["source_observation_type"], "failed")
            self.assertIn("terminal failed observation", recap["observed_completion"]["reason"])
            # The stage cell keeps its own verdict; the run-level fact is separate.
            self.assertEqual(recap["evidence_cells"]["delivery"]["state"], "observed")
            self.assertEqual(recap["observed_completion"]["failed_cells"], [])
            self.assertIn("run termination observed: failed", recap["summary"])

    def test_a_cancelled_run_is_never_completed(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            self._observe(paths, run_id, "merge", evidence_refs=["merge_commit:deadbee"])
            self._observe(paths, run_id, "cancelled", evidence_refs=["operator-stop"])
            recap = build_runtime_learning_recap(paths, run_id)
            self.assertEqual(recap["observed_completion"]["run_termination"], "cancelled")
            self.assertEqual(recap["observed_completion"]["state"], "partial")
            self.assertIn("terminal cancelled observation", recap["observed_completion"]["reason"])
            # A cancellation is not a fault of any stage.
            self.assertEqual(recap["observed_completion"]["failed_cells"], [])
            self.assertEqual(recap["evidence_cells"]["merge"]["state"], "observed")

    def test_a_block_is_reported_and_changes_no_state(self) -> None:
        """A block is recoverable by definition, so it is surfaced without
        moving completion, the same reasoning the runtime projection uses.
        """
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            self._observe(paths, run_id, "worker_result", worker_ref="worker-1", evidence_refs=["commit:abc1234"])
            self._observe(paths, run_id, "blocked", evidence_refs=["waiting-on-review"])
            recap = build_runtime_learning_recap(paths, run_id)
            self.assertEqual(recap["run_lifecycle"]["block"]["source_observation_type"], "blocked")
            self.assertEqual(recap["observed_completion"]["run_termination"], "none")
            self.assertEqual(recap["observed_completion"]["state"], "partial")

    def test_diagnostics_are_bounded(self) -> None:
        broken = {
            "schema_version": RUNTIME_LEARNING_RECAP_SCHEMA_VERSION,
            "record_type": "runtime_learning_recap",
            "run_id": "run-1",
            "revision": {
                "runtime_history_digest": "0" * 64,
                "observation_count": 0,
                "rejected_observation_count": 0,
                "latest_observation_at": "",
            },
            "recap_id": "wlr-" + "0" * 20,
            "evidence_cells": {cell: {"state": "nonsense"} for cell in RUNTIME_LEARNING_RECAP_CELLS},
        }
        errors = runtime_learning_recap_errors(broken)
        self.assertTrue(errors)
        self.assertLessEqual(len(errors), 20)

    def test_closed_vocabularies_are_what_the_contract_says(self) -> None:
        self.assertEqual(RECAP_CELL_STATES, ("observed", "failed", "unavailable"))
        self.assertEqual(set(OBSERVED_COMPLETION_STATES), {"partial", "completed", "failed", "unknown"})
        self.assertEqual(
            RUNTIME_LEARNING_RECAP_CELLS,
            ("delivery", "verification", "review", "pull_request", "ci", "merge_readiness", "merge"),
        )


    def test_building_and_reading_a_recap_has_no_outside_effect(self) -> None:
        """Offline and inert: no socket, no subprocess, and no file but the recap."""
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            run_id = self._run(paths)
            self._observe(paths, run_id, "merge", evidence_refs=["merge_commit:deadbee"])
            # Resolved, because macOS reaches a temporary directory through a
            # symlink and the two spellings would read as different trees.
            root = Path(tmp).resolve()
            before = {path: path.stat().st_mtime_ns for path in sorted(root.rglob("*")) if path.is_file()}

            def _refuse(*args, **kwargs):
                raise AssertionError("the recap build path reached outside the local store")

            with mock.patch("socket.socket", _refuse), mock.patch("subprocess.Popen", _refuse), mock.patch(
                "subprocess.run", _refuse
            ):
                recap = build_runtime_learning_recap(paths, run_id, operator_outcome="useful")
                write_runtime_learning_recap(paths, recap)
                show_runtime_learning_recap(paths, recap["recap_id"])
                list_runtime_learning_recaps(paths, run_id=run_id)

            after = {path: path.stat().st_mtime_ns for path in sorted(root.rglob("*")) if path.is_file()}
            written = sorted(set(after) - set(before))
            self.assertEqual(written, [paths.learning_recaps_dir / f"{recap['recap_id']}.json"])
            self.assertEqual({path: after[path] for path in before}, before)


class RuntimeLearningRecapLearningSurfaceTest(unittest.TestCase):
    def _prepared(self, root: Path):
        paths = resolve_paths(omh_home=root / "omh", hermes_home=root / "hermes")
        run = create_run(paths, {"skill": "execute", "harness": "hermes", "status": "prepared"})
        run_id = str(run["run_id"])
        write_runtime_observation(
            paths.runtime_runs_dir / run_id,
            _observation(run_id, "worker_result", "observed", worker_ref="worker-1", evidence_refs=["commit:abc1234"]),
        )
        return paths, run_id

    def test_trace_keeps_the_two_authorities_apart(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, run_id = self._prepared(Path(tmp))
            record = build_runtime_run_learning_record(paths, run_id, outcome="useful")
            trace, recap = record["trace"], record["recap"]
            self.assertEqual(trace["status"]["outcome"], "useful")
            self.assertEqual(trace["status"]["outcome_authority"], "operator_supplied")
            self.assertEqual(trace["status"]["observed_completion"], "partial")
            self.assertEqual(trace["status"]["observed_completion_authority"], "observed_evidence")
            self.assertEqual(trace["runtime_completion"]["recap_id"], recap["recap_id"])
            self.assertEqual(
                trace["runtime_completion"]["runtime_history_digest"],
                recap["revision"]["runtime_history_digest"],
            )
            validate_workflow_learning_trace(trace)

    def test_trace_validation_refuses_a_relabelled_authority(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, run_id = self._prepared(Path(tmp))
            trace = build_runtime_run_learning_record(paths, run_id, outcome="useful")["trace"]
            promoted = json.loads(json.dumps(trace))
            promoted["status"]["outcome_authority"] = "observed_evidence"
            with self.assertRaises(WorkflowLearningError):
                validate_workflow_learning_trace(promoted)

            disagreeing = json.loads(json.dumps(trace))
            disagreeing["runtime_completion"]["observed_completion"] = "completed"
            with self.assertRaises(WorkflowLearningError):
                validate_workflow_learning_trace(disagreeing)

    def test_list_eval_and_candidate_surfaces_carry_both_states(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, run_id = self._prepared(Path(tmp))
            record = build_runtime_run_learning_record(paths, run_id, outcome="useful")
            trace = write_learning_trace(paths, record["trace"])
            write_runtime_learning_recap(paths, record["recap"])

            rows = list_learning_traces(paths)
            self.assertEqual(rows[0]["outcome"], "useful")
            self.assertEqual(rows[0]["outcome_authority"], "operator_supplied")
            self.assertEqual(rows[0]["observed_completion"], "partial")
            self.assertEqual(rows[0]["observed_completion_authority"], "observed_evidence")

            evaluation = build_workflow_eval_result(trace)
            checks = {check["id"]: check for check in evaluation["checks"]}
            self.assertEqual(checks["completion_authority"]["status"], "passed")
            self.assertIn("partial", checks["completion_authority"]["summary"])

            candidate = build_improvement_candidate(trace, evaluation)
            evidence = candidate["completion_evidence"]
            self.assertEqual(evidence["observed_completion"], "partial")
            self.assertEqual(evidence["observed_completion_authority"], "observed_evidence")
            self.assertEqual(evidence["operator_outcome"], "useful")
            self.assertEqual(evidence["operator_outcome_authority"], "operator_supplied")


    def test_completion_authority_check_fits_the_trace_source(self) -> None:
        """A chat trace has no run to observe; a runtime trace without a recap does."""
        with TemporaryDirectory() as tmp:
            paths, run_id = self._prepared(Path(tmp))
            trace = build_runtime_run_learning_record(paths, run_id, outcome="useful")["trace"]

            chat_like = json.loads(json.dumps(trace))
            chat_like["source"]["kind"] = "chat_interaction"
            chat_like.pop("runtime_completion")
            chat_like["status"].pop("observed_completion")
            chat_like["status"].pop("observed_completion_authority")
            checks = {check["id"]: check for check in build_workflow_eval_result(chat_like)["checks"]}
            self.assertEqual(checks["completion_authority"]["status"], "passed")

            unlinked = json.loads(json.dumps(trace))
            unlinked.pop("runtime_completion")
            unlinked["status"].pop("observed_completion")
            unlinked["status"].pop("observed_completion_authority")
            checks = {check["id"]: check for check in build_workflow_eval_result(unlinked)["checks"]}
            self.assertEqual(checks["completion_authority"]["status"], "warning")


    def test_the_export_bundle_carries_both_sides_of_the_boundary(self) -> None:
        """The bundle is the artifact that travels, so it is the one place the
        boundary must not collapse. It previously emitted the operator outcome
        with no label saying whose it was, and the completion-authority check's
        verdict with no sign of what that check had found.
        """
        with TemporaryDirectory() as tmp:
            paths, run_id = self._prepared(Path(tmp))
            record = build_runtime_run_learning_record(paths, run_id, outcome="useful")
            trace = write_learning_trace(paths, record["trace"])
            write_runtime_learning_recap(paths, record["recap"])
            evaluation = write_workflow_eval(paths, build_workflow_eval_result(trace))
            write_improvement_candidate(paths, build_improvement_candidate(trace, evaluation))

            bundle = build_learning_export_bundle(paths, trace_ids=[trace["trace_id"]])
            validate_workflow_learning_export(bundle)
            exported_trace = bundle["records"]["traces"][0]
            exported_candidate = bundle["records"]["candidates"][0]

            self.assertEqual(exported_trace["status"]["outcome"], "useful")
            self.assertEqual(exported_trace["status"]["outcome_authority"], "operator_supplied")
            self.assertEqual(exported_trace["status"]["observed_completion"], "partial")
            self.assertEqual(exported_trace["status"]["observed_completion_authority"], "observed_evidence")
            completion = exported_trace["runtime_completion"]
            self.assertEqual(completion["observed_completion"], "partial")
            self.assertEqual(completion["cell_states"]["delivery"], "observed")
            self.assertEqual(completion["cell_states"]["merge"], "unavailable")
            self.assertTrue(completion["recap_ref_sha256"].startswith("sha256:"))
            self.assertEqual(exported_candidate["completion_evidence"]["observed_completion"], "partial")
            self.assertEqual(
                exported_candidate["completion_evidence"]["operator_outcome_authority"], "operator_supplied"
            )

            # The bundle stays metadata-only: the ref travels hashed like every
            # other reference, and no free-text feedback rides along.
            serialized = json.dumps(bundle)
            self.assertNotIn("omh-runtime-learning-recap:", serialized)
            self.assertNotIn(record["recap"]["recap_id"], serialized)

    def test_a_wrapper_summary_fallback_is_labelled_prepared_not_operator(self) -> None:
        """A `prepared_not_observed` string may not be labelled operator-supplied
        merely because it occupies the operator's field.
        """
        with TemporaryDirectory() as tmp:
            paths, run_id = self._prepared(Path(tmp))
            (paths.runtime_runs_dir / run_id / "wrapper.json").write_text(
                json.dumps(
                    {
                        "schema_version": "runtime_record/v1",
                        "summary": "Prepared handoff: the change is ready and should merge cleanly.",
                        "completion_status": "completed",
                    }
                ),
                encoding="utf-8",
            )
            record = build_runtime_run_learning_record(paths, run_id, outcome="useful")
            status = record["trace"]["status"]
            self.assertEqual(status["feedback_summary_authority"], "prepared_wrapper_summary")
            self.assertIn("Prepared handoff", status["feedback_summary"])
            # The recap takes only what the operator supplied, so it stays empty.
            self.assertEqual(record["recap"]["operator_assessment"]["operator_feedback_summary"], "")

            supplied = build_runtime_run_learning_record(
                paths, run_id, outcome="useful", feedback_summary="I checked the diff myself."
            )
            self.assertEqual(supplied["trace"]["status"]["feedback_summary_authority"], "operator_supplied")

            absent = build_runtime_run_learning_record(paths, self._run_without_wrapper(paths), outcome="useful")
            self.assertEqual(absent["trace"]["status"]["feedback_summary_authority"], "absent")

    def test_an_unlabelled_trace_is_reported_as_unlabelled_not_assumed(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, run_id = self._prepared(Path(tmp))
            trace = build_runtime_run_learning_record(paths, run_id, outcome="useful")["trace"]
            stripped = json.loads(json.dumps(trace))
            del stripped["status"]["outcome_authority"]
            del stripped["status"]["feedback_summary_authority"]
            # Still valid: a trace written by an earlier generation carries none
            # of these keys and must not be refused.
            validate_workflow_learning_trace(stripped)
            write_learning_trace(paths, stripped)
            row = [item for item in list_learning_traces(paths) if item["trace_id"] == stripped["trace_id"]][0]
            self.assertEqual(row["outcome_authority"], "unlabelled")
            self.assertEqual(row["feedback_summary_authority"], "unlabelled")
            checks = {check["id"]: check for check in build_workflow_eval_result(stripped)["checks"]}
            self.assertEqual(checks["completion_authority"]["status"], "warning")

    def test_an_observed_state_must_name_the_recap_that_holds_it(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, run_id = self._prepared(Path(tmp))
            trace = build_runtime_run_learning_record(paths, run_id, outcome="useful")["trace"]
            orphaned = json.loads(json.dumps(trace))
            del orphaned["runtime_completion"]
            with self.assertRaises(WorkflowLearningError):
                validate_workflow_learning_trace(orphaned)

    def _run_without_wrapper(self, paths) -> str:
        run = create_run(paths, {"skill": "execute", "harness": "hermes", "status": "prepared"})
        return str(run["run_id"])


class RuntimeLearningRecapCliTest(unittest.TestCase):
    def _base(self, root: Path) -> list[str]:
        return ["--omh-home", str(root / "omh"), "--hermes-home", str(root / "hermes")]

    def test_recap_build_list_show_and_record(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = self._base(root)
            paths = resolve_paths(omh_home=root / "omh", hermes_home=root / "hermes")
            run = create_run(paths, {"skill": "execute", "harness": "hermes", "status": "prepared"})
            run_id = str(run["run_id"])

            status, stdout, stderr = run_cli(base + ["learning", "recap", "build", run_id, "--outcome", "useful"])
            self.assertEqual(status, 0, stderr)
            payload = json.loads(stdout)
            self.assertTrue(payload["recorded"])
            recap = payload["runtime_learning_recap"]
            self.assertEqual(recap["observed_completion"]["state"], "unknown")
            self.assertEqual(recap["operator_assessment"]["operator_outcome"], "useful")

            write_runtime_observation(
                paths.runtime_runs_dir / run_id,
                _observation(run_id, "merge", "observed", evidence_refs=["merge_commit:deadbee"]),
            )
            status, stdout, stderr = run_cli(base + ["learning", "recap", "build", run_id, "--dry-run"])
            self.assertEqual(status, 0, stderr)
            dry = json.loads(stdout)
            self.assertFalse(dry["recorded"])
            self.assertEqual(dry["runtime_learning_recap"]["observed_completion"]["state"], "completed")
            self.assertEqual(len(list(paths.learning_recaps_dir.glob("*.json"))), 1)

            status, stdout, stderr = run_cli(base + ["learning", "recap", "list", "--run-id", run_id])
            self.assertEqual(status, 0, stderr)
            listed = json.loads(stdout)["recaps"]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["operator_outcome_authority"], "operator_supplied")

            status, stdout, stderr = run_cli(base + ["learning", "recap", "show", recap["recap_id"]])
            self.assertEqual(status, 0, stderr)
            self.assertEqual(json.loads(stdout)["recap_id"], recap["recap_id"])

            status, stdout, stderr = run_cli(
                base + ["learning", "record", "--from-runtime-run", run_id, "--outcome", "useful"]
            )
            self.assertEqual(status, 0, stderr)
            recorded = json.loads(stdout)
            self.assertEqual(recorded["runtime_learning_recap"]["observed_completion"]["state"], "completed")
            self.assertEqual(recorded["trace"]["status"]["outcome_authority"], "operator_supplied")
            self.assertTrue(recorded["runtime_learning_recap_ref"].startswith("omh-runtime-learning-recap:"))
            self.assertEqual(len(list(paths.learning_recaps_dir.glob("*.json"))), 2)

    def test_missing_run_and_missing_recap_report_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            base = self._base(Path(tmp))
            status, _, stderr = run_cli(base + ["learning", "recap", "build", "no-such-run"])
            self.assertNotEqual(status, 0)
            self.assertIn("runtime run not found", stderr)
            status, _, stderr = run_cli(base + ["learning", "recap", "show", "wlr-" + "0" * 20])
            self.assertNotEqual(status, 0)
            self.assertIn("runtime learning recap not found", stderr)


if __name__ == "__main__":
    unittest.main()

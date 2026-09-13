from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from typing import Any

from _cli_harness import run_cli
from _local_package import load_local_package
from test_decision_prototypes import _proposal
from test_product_discovery_validation import _ledger, _prepared_artifacts
from test_sales_pipeline_review import review_input

load_local_package()


class WorkflowArtifactCommandTests(unittest.TestCase):
    def _run(self, home: Path, workflow: str, operation: str, payload) -> tuple[int, str, str]:
        return run_cli(
            [
                "--omh-home",
                str(home / ".omh"),
                "--hermes-home",
                str(home / ".hermes"),
                "runtime",
                "workflow-artifact",
                workflow,
                operation,
                "--input",
                "-",
            ],
            stdin_text=json.dumps(payload),
        )

    def _run_example(self, home: Path, workflow: str, operation: str, example_name: str) -> tuple[int, str, str]:
        return run_cli(
            [
                "--omh-home",
                str(home / ".omh"),
                "--hermes-home",
                str(home / ".hermes"),
                "runtime",
                "workflow-artifact",
                workflow,
                operation,
                "--input",
                str(Path(__file__).resolve().parents[1] / "examples" / "workflow-artifacts" / example_name),
            ]
        )

    def _semantic_example(self, name: str) -> dict[str, Any]:
        return json.loads((Path(__file__).resolve().parents[1] / "examples" / "workflow-artifacts" / name).read_text())

    def test_build_lifecycle_growth_when_semantic_input_changes_returns_valid_changed_artifacts(self) -> None:
        # Given: a semantic lifecycle declaration with no caller-authored artifact metadata.
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            semantic = self._semantic_example("lifecycle-growth-build-semantic.json")

            # When: the closed build operation derives the artifacts and a bounded field changes.
            status, stdout, stderr = self._run(home, "lifecycle-growth", "build", semantic)
            changed = deepcopy(semantic)
            changed["brief"]["target_behavior_ref"] = "behavior_activation_retained"
            changed_status, changed_stdout, changed_stderr = self._run(home, "lifecycle-growth", "build", changed)

            # Then: the producer-owned artifacts validate, carry the changed field, and still hold for unknown consent.
            self.assertEqual((status, stderr, changed_status, changed_stderr), (0, "", 0, ""))
            artifacts = json.loads(stdout)["result"]
            changed_artifacts = json.loads(changed_stdout)["result"]
            self.assertEqual(changed_artifacts["brief"]["target_behavior_ref"], "behavior_activation_retained")
            self.assertNotIn("claim_boundary", semantic["brief"])
            for artifact in artifacts.values():
                validate_status, validate_stdout, validate_stderr = self._run(home, "lifecycle-growth", "validate", artifact)
                self.assertEqual((validate_status, validate_stderr), (0, ""))
                self.assertTrue(json.loads(validate_stdout)["result"]["valid"])
            prepare_status, prepare_stdout, prepare_stderr = self._run(home, "lifecycle-growth", "prepare", artifacts)
            self.assertEqual((prepare_status, prepare_stderr), (0, ""))
            self.assertEqual(json.loads(prepare_stdout)["result"]["verdict"], "HOLD")

    def test_build_product_discovery_when_semantic_input_changes_derives_a_valid_new_hash(self) -> None:
        # Given: a synthetic pre-decision declaration with no caller-authored artifact identity.
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            semantic = self._semantic_example("product-discovery-validation-build-semantic.json")

            # When: the closed build operation derives a package before and after a semantic field changes.
            status, stdout, stderr = self._run(home, "product-discovery-validation", "build", semantic)
            changed = deepcopy(semantic)
            changed["frame"]["problem_ref"] = "problem-activation-dropoff"
            changed_status, changed_stdout, changed_stderr = self._run(home, "product-discovery-validation", "build", changed)

            # Then: public builders derive distinct valid hashes, and synthetic evidence remains inconclusive.
            self.assertEqual((status, stderr, changed_status, changed_stderr), (0, "", 0, ""))
            package = json.loads(stdout)["result"]
            changed_package = json.loads(changed_stdout)["result"]
            self.assertNotIn("artifact_id", semantic["frame"])
            self.assertNotEqual(package["frame"]["artifact_id"], changed_package["frame"]["artifact_id"])
            for artifact in package.values():
                validate_status, validate_stdout, validate_stderr = self._run(home, "product-discovery-validation", "validate", artifact)
                self.assertEqual((validate_status, validate_stderr), (0, ""))
                self.assertTrue(json.loads(validate_stdout)["result"]["valid"])
            evaluate_status, evaluate_stdout, evaluate_stderr = self._run(
                home,
                "product-discovery-validation",
                "evaluate",
                {"package": package, "now": "2030-01-01T02:00:00+00:00"},
            )
            self.assertEqual((evaluate_status, evaluate_stderr), (0, ""))
            self.assertEqual(json.loads(evaluate_stdout)["result"]["decision"], "inconclusive")

    def test_committed_examples_when_run_through_real_cli_return_their_declared_states(self) -> None:
        # Given: fictional JSON scenarios committed for the public workflow operations.
        cases = (
            ("decision-prototype", "prepare", "decision-prototype-prepare.json", "execution.status", "prepared_not_observed"),
            ("lifecycle-growth", "build", "lifecycle-growth-build-semantic.json", "safety.consent_state", "unknown"),
            ("product-discovery-validation", "build", "product-discovery-validation-build-semantic.json", "ledger.status", "reentered"),
            ("product-discovery-validation", "channel-feedback", "product-discovery-validation-channel-feedback.json", "disposition.proposed_followup", "gtm_pivot"),
            ("sales-pipeline-review", "prepare", "sales-pipeline-review-prepare-ready.json", "status", "READY"),
        )
        with TemporaryDirectory() as temporary:
            home = Path(temporary)

            # When: each exact committed file is supplied to the registered runtime CLI.
            results = [
                (field, expected, self._run_example(home, workflow, operation, example_name))
                for workflow, operation, example_name, field, expected in cases
            ]

            # Then: each producer preserves its prepared, HOLD, inconclusive, or READY machine state.
            for field, expected, (status, stdout, stderr) in results:
                with self.subTest(field=field, expected=expected):
                    self.assertEqual((status, stderr), (0, ""))
                    result = json.loads(stdout)["result"]
                    for key in field.split("."):
                        result = result[key]
                    self.assertEqual(result, expected)

    def test_persist_decision_prototype_when_explicitly_requested_uses_the_existing_store(self) -> None:
        # Given: one prepared example artifact and an isolated active OMH home.
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            proposal = json.loads(
                (Path(__file__).resolve().parents[1] / "examples" / "workflow-artifacts" / "decision-prototype-prepare.json").read_text()
            )
            status, stdout, stderr = self._run(home, "decision-prototype", "prepare", proposal)
            self.assertEqual((status, stderr), (0, ""))

            # When: the existing producer-owned persistence operation is explicitly invoked.
            status, _stdout, stderr = self._run(home, "decision-prototype", "persist", json.loads(stdout)["result"])

            # Then: the validated prepared artifact is present only in that producer's existing store.
            self.assertEqual((status, stderr), (0, ""))
            stored = json.loads((home / ".omh" / "runtime" / "decision-prototypes" / "cache-backend.json").read_text())
            self.assertEqual(stored["execution"]["status"], "prepared_not_observed")

    def test_append_product_discovery_artifact_when_explicitly_requested_uses_the_existing_store(self) -> None:
        # Given: one valid pre-decision artifact from the synthetic inconclusive example.
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            semantic = self._semantic_example("product-discovery-validation-build-semantic.json")
            build_status, build_stdout, build_stderr = self._run(home, "product-discovery-validation", "build", semantic)
            self.assertEqual((build_status, build_stderr), (0, ""))
            artifact = json.loads(build_stdout)["result"]["frame"]

            # When: the existing append-only persistence operation is explicitly invoked.
            status, _stdout, stderr = self._run(home, "product-discovery-validation", "append", artifact)

            # Then: the source artifact is retained in the producer-owned append-only journal.
            self.assertEqual((status, stderr), (0, ""))
            stored = json.loads((home / ".omh" / "runtime" / "journal" / "product_discovery_artifacts.jsonl").read_text())
            self.assertEqual(stored["schema_version"], "discovery_decision_frame/v1")

    def test_audience_gate_blocks_build_outputs_when_the_framed_segment_is_unknown(self) -> None:
        # Given: the committed semantic example with its audience relabelled unknown.
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            semantic = self._semantic_example("product-discovery-validation-build-semantic.json")
            semantic["frame"]["segment_definition_state"] = "unknown"
            build_status, build_stdout, build_stderr = self._run(home, "product-discovery-validation", "build", semantic)
            self.assertEqual((build_status, build_stderr), (0, ""))
            built = json.loads(build_stdout)["result"]

            # When: the frame is read through the closed audience-gate operation.
            status, stdout, stderr = self._run(home, "product-discovery-validation", "audience-gate", built["frame"])

            # Then: evidence work continues while every build output stays blocked and named.
            self.assertEqual((status, stderr), (0, ""))
            gate = json.loads(stdout)["result"]
            self.assertEqual(gate["schema_version"], "discovery_audience_gate/v1")
            self.assertEqual(gate["audience_gate"], "audience_undefined")
            self.assertTrue(gate["evidence_work_permitted"])
            self.assertFalse(gate["solution_work_permitted"])
            self.assertEqual(gate["blocked_outputs"], ["product-brief", "decision-prototype", "coding-handoff"])
            self.assertTrue(gate["missing_audience_evidence_refs"])
            self.assertFalse((home / ".omh" / "runtime" / "journal" / "product_discovery_artifacts.jsonl").exists())

    def test_prepare_decision_prototype_when_read_from_stdin_returns_metadata_without_persisting(self) -> None:
        # Given: one bounded prototype declaration at the CLI stdin boundary.
        with TemporaryDirectory() as temporary:
            home = Path(temporary)

            # When: an operator prepares it through the runtime command.
            status, stdout, stderr = self._run(home, "decision-prototype", "prepare", _proposal())

            # Then: the prepared artifact is returned without a runtime write or execution claim.
            self.assertEqual((status, stderr), (0, ""))
            payload = json.loads(stdout)
            self.assertEqual(payload["result"]["execution"]["status"], "available_not_observed")
            self.assertFalse((home / ".omh" / "runtime" / "decision-prototypes" / "cache-backend.json").exists())

    def test_reject_decision_prototype_when_input_is_not_an_object(self) -> None:
        # Given: an array instead of a bounded JSON operation object.
        with TemporaryDirectory() as temporary:
            home = Path(temporary)

            # When: an operator submits it to the decision-prototype command.
            status, _stdout, stderr = self._run(home, "decision-prototype", "prepare", [])

            # Then: the boundary refuses it before a producer receives it.
            self.assertEqual(status, 2)
            self.assertIn("workflow artifact input must be a JSON object", stderr)

    def test_hold_lifecycle_growth_when_unknown_consent_artifact_is_structurally_valid(self) -> None:
        # Given: otherwise valid launch artifacts whose consent state is explicitly unknown.
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            from test_lifecycle_growth_contracts import LifecycleGrowthContractTests

            artifacts = LifecycleGrowthContractTests()._launch_artifacts()
            artifacts["safety"]["consent_state"] = "unknown"

            # When: the runtime prepares the lifecycle decision.
            status, stdout, stderr = self._run(home, "lifecycle-growth", "prepare", artifacts)

            # Then: structural validity does not turn unknown consent into launch readiness.
            self.assertEqual((status, stderr), (0, ""))
            self.assertEqual(json.loads(stdout)["result"]["verdict"], "HOLD")

    def test_reject_lifecycle_growth_when_input_is_malformed(self) -> None:
        # Given: malformed lifecycle input.
        with TemporaryDirectory() as temporary:
            home = Path(temporary)

            # When: it reaches the bounded runtime boundary.
            status, _stdout, stderr = self._run(home, "lifecycle-growth", "prepare", [])

            # Then: it is refused without a readiness result.
            self.assertEqual(status, 2)
            self.assertIn("workflow artifact input must be a JSON object", stderr)

    def test_prepare_product_discovery_when_external_evidence_is_reentered_returns_a_package(self) -> None:
        # Given: a complete product-discovery package with reentered external evidence.
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            artifacts = _prepared_artifacts()
            package = {**artifacts, "ledger": _ledger()}

            # When: the package is prepared through the real CLI.
            status, stdout, stderr = self._run(home, "product-discovery-validation", "prepare", package)

            # Then: the result is a metadata-only prepared package.
            self.assertEqual((status, stderr), (0, ""))
            self.assertEqual(json.loads(stdout)["result"]["frame"]["status"], "prepared_not_observed")

    def test_channel_feedback_returns_companion_artifacts_and_stable_holds(self) -> None:
        from test_product_discovery_channel_feedback import NOW, feedback_entry, feedback_inputs, feedback_semantic

        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            payload = {**feedback_inputs(), "feedback_ledger": feedback_semantic(), "now": NOW}
            status, stdout, stderr = self._run(home, "product-discovery-validation", "channel-feedback", payload)
            self.assertEqual((status, stderr), (0, ""))
            result = json.loads(stdout)["result"]
            self.assertEqual(result["ledger"]["schema_version"], "channel_feedback_ledger/v1")
            self.assertEqual(result["disposition"]["next_route"], "product-brief")
            for artifact in (result["ledger"], result["disposition"]):
                code, output, error = self._run(home, "product-discovery-validation", "validate", artifact)
                self.assertEqual((code, error), (0, ""))
                self.assertTrue(json.loads(output)["result"]["valid"])
            missing = feedback_entry()
            missing.pop("observed_at")
            cases = (([missing], "channel_feedback_missing"),
                     ([feedback_entry(observed_at="2029-01-01T00:00:00Z")], "channel_feedback_stale"),
                     ([feedback_entry(), feedback_entry(feedback_id="feedback-copy")], "channel_feedback_duplicate"),
                     ([feedback_entry(segment_ref="segment-other")], "channel_feedback_foreign_segment"),
                     ([feedback_entry(channel_ref="channel-other")], "channel_feedback_wrong_channel"),
                     ([feedback_entry(test_id="test-other")], "channel_feedback_wrong_test"),
                     ([feedback_entry(), feedback_entry(effect="contradicts", feedback_id="feedback-conflict")], "channel_feedback_contradictory"))
            for entries, reason in cases:
                with self.subTest(reason=reason):
                    payload["feedback_ledger"] = feedback_semantic(entries)
                    code, output, error = self._run(home, "product-discovery-validation", "channel-feedback", payload)
                    self.assertEqual((code, error), (0, ""))
                    disposition = json.loads(output)["result"]["disposition"]
                    self.assertIn(reason, [row["reason"] for row in disposition["held_observations"]])
                    self.assertTrue(disposition["handoff_held"])

    def test_channel_feedback_malformed_and_unsafe_inputs_are_errors(self) -> None:
        from test_product_discovery_channel_feedback import NOW, feedback_entry, feedback_inputs, feedback_semantic

        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            cases = ({}, [], {**feedback_inputs(), "feedback_ledger": feedback_semantic([feedback_entry(source_ref="https://private.example")]), "now": NOW},
                     {**feedback_inputs(), "feedback_ledger": feedback_semantic([{**feedback_entry(), "raw_transcript": "private"}]), "now": NOW})
            for payload in cases:
                with self.subTest(payload=payload):
                    code, output, error = self._run(home, "product-discovery-validation", "channel-feedback", payload)
                    self.assertEqual(code, 2)
                    self.assertEqual(output, "")
                    self.assertNotIn("Traceback", error)

    def test_reject_product_discovery_when_input_is_malformed(self) -> None:
        # Given: malformed product-discovery input.
        with TemporaryDirectory() as temporary:
            home = Path(temporary)

            # When: it is submitted through the runtime command.
            status, _stdout, stderr = self._run(home, "product-discovery-validation", "prepare", [])

            # Then: the shared boundary rejects it before package preparation.
            self.assertEqual(status, 2)
            self.assertIn("workflow artifact input must be a JSON object", stderr)

    def test_prepare_sales_pipeline_review_when_snapshot_is_bounded_returns_scope(self) -> None:
        # Given: a bounded typed sales snapshot represented as JSON.
        with TemporaryDirectory() as temporary:
            home = Path(temporary)

            # When: the CLI adapts it into the known sales input models.
            status, stdout, stderr = self._run(home, "sales-pipeline-review", "prepare", asdict(review_input()))

            # Then: the source remains metadata-only and ready for calculation.
            self.assertEqual((status, stderr), (0, ""))
            scope = json.loads(stdout)["result"]["scope"]
            self.assertEqual((scope["status"], scope["raw_export_retained"]), ("READY", False))

    def test_reject_sales_pipeline_review_when_input_is_malformed(self) -> None:
        # Given: malformed sales review input.
        with TemporaryDirectory() as temporary:
            home = Path(temporary)

            # When: it reaches the typed sales adapter.
            status, _stdout, stderr = self._run(home, "sales-pipeline-review", "prepare", [])

            # Then: no sales calculation is attempted.
            self.assertEqual(status, 2)
            self.assertIn("workflow artifact input must be a JSON object", stderr)

    def test_refuse_decision_prototype_when_stdin_is_not_utf8_bytes(self) -> None:
        # Given: invalid UTF-8 bytes at the real process stdin boundary.
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            root = Path(__file__).resolve().parents[1]
            environment = {**os.environ, "PYTHONPATH": str(root / "src")}

            # When: an operator sends those bytes to the registered runtime command.
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "omh.cli",
                    "--omh-home",
                    str(home / ".omh"),
                    "--hermes-home",
                    str(home / ".hermes"),
                    "runtime",
                    "workflow-artifact",
                    "decision-prototype",
                    "prepare",
                    "--input",
                    "-",
                ],
                cwd=root,
                env=environment,
                input=bytes([255]),
                capture_output=True,
                check=False,
            )

            # Then: the CLI refuses bounded input without a traceback or an input echo.
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stdout, b"")
            self.assertIn(b"workflow artifact input must be valid UTF-8", completed.stderr)
            self.assertNotIn(b"Traceback", completed.stderr)
            self.assertNotIn(bytes([255]), completed.stderr)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package
from test_product_discovery_validation import DISCOVERY_ID, _ledger, _prepared_artifacts

load_local_package()
from omh.workflows.product_discovery_artifacts import (  # noqa: E402
    build_channel_feedback_ledger,
    build_channel_feedback_disposition,
)
from omh.workflows.product_discovery_artifact_validation import validate_product_discovery_artifact  # noqa: E402

NOW = "2030-01-01T02:00:00+00:00"


def feedback_inputs(*, failure_decision="pivot"):
    from omh.workflows.product_discovery_artifacts import build_assumption_test_portfolio

    artifacts = _prepared_artifacts()
    assumption = {**artifacts["portfolio"]["assumptions"][0]}
    assumption.pop("priority")
    assumption.update(category="go_to_market", failure_decision=failure_decision)
    artifacts["portfolio"] = build_assumption_test_portfolio(discovery_id=DISCOVERY_ID, assumptions=[assumption])
    return {key: artifacts[key] for key in ("frame", "gtm", "portfolio")}


def feedback_entry(target="channel_reachability", effect="supports", **changes):
    return {
        "feedback_id": "feedback-" + target,
        "test_id": "test-problem-interviews",
        "channel_ref": "channel-community",
        "criterion_ref": {"supports": "criterion-repeated-problem", "contradicts": "criterion-no-repeated-problem", "unknown": "criterion-insufficient-sample"}[effect],
        "segment_ref": "segment-new-teams",
        "affected_target": target,
        "effect": effect,
        "source_class": "external_human",
        "source_ref": "source-" + target,
        "observed_at": "2030-01-01T01:00:00+00:00",
        "sample_count": 2,
        "confidence_limit": "bounded",
        **changes,
    }


def feedback_semantic(entries=None, *, inputs=None):
    inputs = inputs or feedback_inputs()
    return {
        "discovery_id": DISCOVERY_ID,
        "gtm_artifact_id": inputs["gtm"]["artifact_id"],
        "initial_channel_ref": inputs["gtm"]["initial_channel_ref"],
        "segment_ref": inputs["frame"]["segment_ref"],
        "entries": entries if entries is not None else [feedback_entry(), feedback_entry("opportunity_direction")],
    }


def disposition_fields():
    inputs = feedback_inputs()
    ledger = build_channel_feedback_ledger(**feedback_semantic())
    return dict(discovery_id=DISCOVERY_ID, frame_ref=inputs["frame"]["artifact_id"],
                gtm_artifact_id=inputs["gtm"]["artifact_id"], portfolio_ref=inputs["portfolio"]["artifact_id"],
                feedback_ledger_ref=ledger["artifact_id"], initial_channel_ref="channel-community",
                segment_ref="segment-new-teams", evaluated_at=NOW,
                channel_reachability="supports", opportunity_direction="supports",
                rejected_channel_hypothesis_refs=[], held_observations=[], proposed_followup="none")


class ChannelFeedbackBuilderTests(unittest.TestCase):
    def test_builders_are_deterministic_closed_and_roundtrip(self):
        for builder, fields in ((build_channel_feedback_ledger, feedback_semantic()),
                                (build_channel_feedback_disposition, disposition_fields())):
            with self.subTest(builder=builder.__name__):
                artifact = builder(**fields)
                self.assertEqual(artifact, builder(**fields))
                self.assertEqual(validate_product_discovery_artifact(artifact), [])
                for bad in ({"schema_version": artifact["schema_version"]},
                            {**artifact, "unexpected": True}, {**artifact, "artifact_id": 1},
                            {**artifact, "discovery_id": {}}, {**artifact, "raw_transcript": "private"}):
                    self.assertTrue(validate_product_discovery_artifact(bad))
                nested = "entries" if "entries" in artifact else "held_observations"
                self.assertTrue(validate_product_discovery_artifact({**artifact, nested: [{}]}))

    def test_ledger_requires_unique_closed_opaque_observations(self):
        entry = feedback_entry()
        cases = [[entry, {**entry, "feedback_id": "feedback-copy"}],
                 [{**entry, "source_ref": "https://private.example"}],
                 [{**entry, "affected_target": "problem"}], [{**entry, "sample_count": True}],
                 [{**entry, "raw_transcript": "private"}], [{}]]
        for entries in cases:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                build_channel_feedback_ledger(**feedback_semantic(entries))

    def test_disposition_cannot_promote_unknown_or_forge_derived_fields(self):
        fields = disposition_fields()
        artifact = build_channel_feedback_disposition(**{**fields, "channel_reachability": "unknown"})
        self.assertEqual(artifact["disposition"], "inconclusive")
        self.assertEqual(artifact["next_route"], "product-discovery-validation")
        self.assertTrue(artifact["handoff_held"])
        self.assertTrue(validate_product_discovery_artifact({**artifact, "handoff_held": False}))
        for changes in ({"held_observations": [{"feedback_ref": "feedback-one", "reason": "invented"}]},
                        {"proposed_followup": "build"}, {"channel_reachability": {}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_channel_feedback_disposition(**{**fields, **changes})

    def test_v1_cannot_gain_channel_claims(self):
        from omh.workflows.product_discovery_validation import evaluate_product_discovery

        ledger = deepcopy(_ledger())
        ledger["entries"][0]["channel_ref"] = "channel-community"
        self.assertTrue(validate_product_discovery_artifact(ledger))
        receipt = evaluate_product_discovery({**_prepared_artifacts(), "ledger": _ledger()}, now=NOW)
        self.assertTrue(validate_product_discovery_artifact({**receipt, "channel_ref": "channel-community"}))


class ChannelFeedbackEvaluationTests(unittest.TestCase):
    def evaluate(self, entries=None, **kwargs):
        from omh.workflows.product_discovery_validation import evaluate_channel_feedback

        inputs = feedback_inputs(**kwargs)
        return evaluate_channel_feedback(**inputs, feedback_ledger=feedback_semantic(entries, inputs=inputs), now=NOW)

    def test_channel_only_contradiction(self):
        result = self.evaluate([feedback_entry(effect="contradicts"), feedback_entry("opportunity_direction")])
        self.assertEqual(result["channel_reachability"], "contradicts")
        self.assertEqual(result["opportunity_direction"], "supports")
        self.assertTrue(result["handoff_held"])
        self.assertEqual(result["rejected_channel_hypothesis_refs"], [feedback_inputs()["gtm"]["artifact_id"]])

    def test_opportunity_only_contradiction(self):
        result = self.evaluate([feedback_entry(), feedback_entry("opportunity_direction", "contradicts")])
        self.assertEqual(result["channel_reachability"], "supports")
        self.assertEqual(result["opportunity_direction"], "contradicts")
        self.assertEqual(result["rejected_channel_hypothesis_refs"], [])
        self.assertTrue(result["handoff_held"])

    def test_both_targets_and_determinism(self):
        entries = [feedback_entry(effect="contradicts"), feedback_entry("opportunity_direction", "contradicts")]
        self.assertEqual(self.evaluate(entries), self.evaluate(entries))
        self.assertEqual(self.evaluate(entries)["opportunity_direction"], "contradicts")

    def test_unknown_effect_never_promotes(self):
        result = self.evaluate([feedback_entry(effect="unknown"), feedback_entry("opportunity_direction")])
        self.assertEqual(result["disposition"], "inconclusive")
        self.assertEqual(result["channel_reachability"], "unknown")
        self.assertEqual(result["held_observations"][0]["reason"], "channel_feedback_unresolved")

    def test_foreign_segment(self):
        result = self.evaluate([feedback_entry(segment_ref="segment-foreign"), feedback_entry("opportunity_direction")])
        self.assertEqual(result["held_observations"][0]["reason"], "channel_feedback_foreign_segment")

    def test_stale_observation(self):
        for stamp in ("2029-12-31T00:00:00Z", "2030-01-03T00:00:00Z", "2030-01-01T03:00:00Z"):
            with self.subTest(stamp=stamp):
                result = self.evaluate([feedback_entry(observed_at=stamp)])
                self.assertIn("channel_feedback_stale", [row["reason"] for row in result["held_observations"]])

    def test_duplicate_evidence(self):
        entry = feedback_entry()
        result = self.evaluate([entry, {**entry, "feedback_id": "feedback-copy"}, feedback_entry("opportunity_direction")])
        self.assertEqual(result["channel_reachability"], "unknown")
        self.assertEqual({row["reason"] for row in result["held_observations"]}, {"channel_feedback_duplicate"})

    def test_explicit_pivot_and_kill_follow_precommit(self):
        entries = [feedback_entry(effect="contradicts"), feedback_entry("opportunity_direction")]
        self.assertEqual(self.evaluate(entries)["proposed_followup"], "gtm_pivot")
        killed = self.evaluate(entries, failure_decision="kill")
        self.assertEqual(killed["proposed_followup"], "reevaluate_discovery")
        self.assertEqual(killed["next_route"], "product-discovery-validation")

    def test_problem_validation_and_append_only_history_are_preserved(self):
        from omh.system.paths import resolve_paths
        from omh.workflows.product_discovery_validation import (
            append_product_discovery_artifact, evaluate_product_discovery, read_product_discovery_artifacts,
        )

        package = {**_prepared_artifacts(), "ledger": _ledger()}
        receipt = evaluate_product_discovery(package, now=NOW)
        before = deepcopy(receipt)
        semantic = feedback_semantic([feedback_entry(effect="contradicts"), feedback_entry("opportunity_direction")])
        ledger = build_channel_feedback_ledger(**semantic)
        result = self.evaluate(semantic["entries"])
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            paths = resolve_paths(omh_home=home / "omh", hermes_home=home / "hermes")
            for artifact in (receipt, ledger, result):
                append_product_discovery_artifact(paths, artifact)
            fresh = resolve_paths(omh_home=home / "omh", hermes_home=home / "hermes")
            self.assertEqual(read_product_discovery_artifacts(fresh, discovery_id=DISCOVERY_ID), [before, ledger, result])
        self.assertEqual(receipt["problem_gate"], "validated")
        self.assertEqual(evaluate_product_discovery(package, now=NOW), before)

    def test_missing_wrong_channel_wrong_test_and_conflicting_source_hold(self):
        missing = feedback_entry()
        missing.pop("observed_at")
        contradictory = feedback_entry(effect="contradicts", feedback_id="feedback-conflict", source_ref="source-channel_reachability")
        for entries, reason in (([missing], "channel_feedback_missing"),
                                ([feedback_entry(channel_ref="channel-other")], "channel_feedback_wrong_channel"),
                                ([feedback_entry(test_id="test-other")], "channel_feedback_wrong_test"),
                                ([feedback_entry(), contradictory], "channel_feedback_contradictory")):
            with self.subTest(reason=reason):
                result = self.evaluate(entries)
                self.assertEqual(result["channel_reachability"], "unknown")
                self.assertIn(reason, [row["reason"] for row in result["held_observations"]])

    def test_inadmissible_precedes_contradiction_and_incomplete_cannot_promote(self):
        cases = [feedback_entry(source_class="synthetic"), feedback_entry(confidence_limit="limited"),
                 feedback_entry(sample_count=1), feedback_entry(criterion_ref="criterion-other")]
        for entry in cases:
            with self.subTest(entry=entry):
                self.assertEqual(self.evaluate([entry])["channel_reachability"], "unknown")
        result = self.evaluate([feedback_entry(effect="contradicts"),
                                feedback_entry(feedback_id="feedback-stale", source_ref="source-stale", observed_at="2029-01-01T00:00:00Z")])
        self.assertEqual(result["channel_reachability"], "unknown")
        self.assertEqual(result["rejected_channel_hypothesis_refs"], [])
        self.assertEqual(self.evaluate([])["disposition"], "inconclusive")

    def test_gate_and_handoff_bind_and_hold_until_frame_is_revised(self):
        from omh.workflows.product_discovery_validation import (
            discovery_audience_gate_with_feedback, product_brief_consumption_with_feedback, evaluate_product_discovery,
        )
        from omh.workflows.decision_receipt_handoffs import build_channel_aware_product_brief_handoff
        from omh.workflows.product_discovery_artifacts import build_discovery_decision_frame

        frame = feedback_inputs()["frame"]
        receipt = evaluate_product_discovery({**_prepared_artifacts(), "ledger": _ledger()}, now=NOW)
        result = self.evaluate([feedback_entry(effect="contradicts"), feedback_entry("opportunity_direction")])
        gate = discovery_audience_gate_with_feedback(frame, result)
        self.assertEqual(gate["audience_gate"], "audience_reachability_contradicted")
        self.assertEqual(gate["blocked_outputs"], ["product-brief", "decision-prototype", "coding-handoff"])
        self.assertEqual(product_brief_consumption_with_feedback(receipt, result), {})
        handoff = build_channel_aware_product_brief_handoff(receipt, result)
        self.assertEqual(handoff["decision_state"], "blocked")
        self.assertEqual(handoff["blocked_reason"], "discovery_segment_reachability_contradicted")
        self.assertEqual(handoff["product_brief_context"], {})
        support = self.evaluate()
        self.assertTrue(product_brief_consumption_with_feedback(receipt, support))
        self.assertEqual(build_channel_aware_product_brief_handoff(receipt, support)["decision_state"], "validated")
        opportunity = self.evaluate([feedback_entry(), feedback_entry("opportunity_direction", "contradicts")])
        self.assertEqual(build_channel_aware_product_brief_handoff(receipt, opportunity)["blocked_reason"], "discovery_channel_feedback_hold")
        fields = {key: value for key, value in frame.items() if key not in {"schema_version", "artifact_id", "status", "claim_boundary"}}
        revised = build_discovery_decision_frame(**{**fields, "learning_budget_ref": "budget-revised"})
        with self.assertRaisesRegex(ValueError, "frame"):
            discovery_audience_gate_with_feedback(revised, result)
        self.assertEqual(product_brief_consumption_with_feedback(receipt, {**support, "segment_ref": "segment-foreign"}), {})

    def test_package_binding_and_unsafe_metadata_are_refused(self):
        from omh.workflows.product_discovery_validation import evaluate_channel_feedback

        for field, value, reason in (("segment_ref", "segment-foreign", "channel_feedback_foreign_segment"),
                                     ("initial_channel_ref", "channel-other", "channel_feedback_wrong_channel"),
                                     ("gtm_artifact_id", "gtm-other", "channel_feedback_wrong_channel")):
            with self.subTest(field=field):
                result = evaluate_channel_feedback(**feedback_inputs(), feedback_ledger={**feedback_semantic(), field: value}, now=NOW)
                self.assertTrue(result["handoff_held"])
                self.assertEqual({row["reason"] for row in result["held_observations"]}, {reason})
        for entry in (feedback_entry(source_ref="https://private.example"), {**feedback_entry(), "raw_transcript": "private"}):
            with self.assertRaises(ValueError):
                self.evaluate([entry])


if __name__ == "__main__":
    unittest.main()

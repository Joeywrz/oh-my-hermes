from __future__ import annotations

from copy import deepcopy
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


if __name__ == "__main__":
    unittest.main()

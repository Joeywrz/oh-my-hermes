from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from omh.coding.coding_delegation import build_coding_delegation_payload
from omh.local_store import utc_now
from omh.coding.executor_capability_snapshots import (
    build_executor_capability_snapshot,
    complete_executor_capability_snapshot,
    executor_capability_snapshot_path,
    executor_capability_snapshot_compatibility,
    validate_executor_capability_snapshot,
    write_executor_capability_snapshot,
)
from omh.coding.media_handoff_capabilities import (
    ALTERNATIVE_REPRESENTATIONS_BY_MODALITY,
    FAIL_CLOSED_VERDICTS,
    HANDOFF_INPUT_REPRESENTATIONS,
    attachment_input_modality,
    build_executor_modality_decision,
    demo_media_handoff_decisions,
    input_representations_from_attachments,
    merged_input_representation,
)


_ROUTE = {"provider": "openai", "wire_model": "gpt-5", "endpoint_mode": "default"}
_PRIMARY_RECOMMENDATION = {
    "owner": "maestro",
    "status": "resolved",
    "selected": {
        "provider": "openai",
        "model_id": "gpt-5",
        "endpoint_mode": "default",
        "model_alias": "display-gpt-5",
    },
    "projection": {"kind": "maestro_ordered_chain", "chain": []},
}


def _snapshot(
    status: str = "host_observed",
    *,
    capability: str = "input_modality_image",
    executor: str = "codex",
    observed_at: str = "2026-09-03T00:00:00Z",
) -> dict[str, object]:
    return {
        "schema_version": "executor_capability_snapshot/v2",
        "executor": executor,
        "recorded_at": "2026-09-03T00:00:00Z",
        "capabilities": {
            capability: {
                "status": status,
                "scope": _ROUTE,
                "evidence_ref": "operator:provider-docs",
                "observed_at": observed_at,
            }
        },
    }


_DOCUMENT_ALTERNATIVES = ["extracted_text", "ocr_output"]


def _primary_handoff_decision(
    *,
    capability: str = "input_modality_image",
    scope: dict[str, str] | None = None,
    input_representation: str = "raw_media:image",
    transformation: dict[str, str] | None = None,
    recommendation: dict[str, object] | None = None,
) -> dict[str, object]:
    # The delegation payload judges freshness against the real clock (it has
    # no `now` seam), so the fixture must be stamped at test time: a fixed
    # date crossed the 24-hour evidence horizon the day after it was written.
    stamp = utc_now()
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        snapshot = build_executor_capability_snapshot(
            executor="codex",
            capabilities={
                capability: {
                    "status": "host_observed",
                    "scope": scope or _ROUTE,
                    "evidence_ref": "operator:primary-handoff-fixture",
                    "observed_at": stamp,
                }
            },
            recorded_at=stamp,
        )
        write_executor_capability_snapshot(
            executor_capability_snapshot_path(directory, "codex"),
            snapshot,
        )
        payload = build_coding_delegation_payload(
            "Implement the image handoff with regression coverage.",
            executor_target="codex",
            input_representation=input_representation,
            transformation=transformation,
            model_recommendation=recommendation or _PRIMARY_RECOMMENDATION,
            capability_snapshot_directory=directory,
        )
    return payload["executor_handoff"]["executor_modality_decision"]  # type: ignore[index]


class MediaHandoffCapabilityContractTests(unittest.TestCase):
    def test_v2_route_scoped_image_observation_is_accepted(self) -> None:
        self.assertEqual(validate_executor_capability_snapshot(_snapshot()), [])

    def test_v1_projects_explicit_unknown_modality_rows(self) -> None:
        legacy = {
            "schema_version": "executor_capability_snapshot/v1",
            "executor": "codex",
            "recorded_at": "2026-09-03T00:00:00Z",
            "capabilities": {"parallel_agents": {"status": "unknown"}},
        }
        self.assertEqual(validate_executor_capability_snapshot(legacy), [])
        self.assertEqual(
            executor_capability_snapshot_compatibility(legacy),
            {"compatible": True, "projected_from": "v1", "modality_rows": "unknown"},
        )
        self.assertEqual(
            complete_executor_capability_snapshot(legacy)["capabilities"]["input_modality_image"],
            {"status": "unknown"},
        )

    def test_decision_is_fail_closed_except_for_fresh_exact_route_evidence(self) -> None:
        supported = build_executor_modality_decision(
            input_representation="raw_media:image", snapshot=_snapshot(), route=_ROUTE, now="2026-09-03T01:00:00Z"
        )
        unknown = build_executor_modality_decision(
            input_representation="raw_media:image", snapshot={"executor": "codex", "capabilities": {}}, route=_ROUTE, now="2026-09-03T01:00:00Z"
        )
        unsupported = build_executor_modality_decision(
            input_representation="raw_media:image", snapshot=_snapshot("unavailable"), route=_ROUTE, now="2026-09-03T01:00:00Z"
        )
        self.assertEqual(supported["verdict"], "dispatch")
        self.assertEqual(unknown["verdict"], "modality_unknown")
        self.assertEqual(unsupported["verdict"], "modality_unsupported")
        self.assertEqual(set(HANDOFF_INPUT_REPRESENTATIONS), {"text_only", "raw_media", "local_file_reference", "extracted_text", "ocr_output", "transcript", "normalized_other"})

    def test_local_file_reference_requires_modality_and_route_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "local_file_reference requires"):
            build_executor_modality_decision(
                input_representation="local_file_reference",
                snapshot=_snapshot(),
                route=_ROUTE,
                now="2026-09-03T01:00:00Z",
            )

        supported = build_executor_modality_decision(
            input_representation="local_file_reference:image",
            snapshot=_snapshot(),
            route=_ROUTE,
            now="2026-09-03T01:00:00Z",
        )
        unknown = build_executor_modality_decision(
            input_representation="local_file_reference:image",
            snapshot={"executor": "codex", "capabilities": {}},
            route=_ROUTE,
            now="2026-09-03T01:00:00Z",
        )

        self.assertEqual(supported["verdict"], "dispatch")
        self.assertEqual(unknown["verdict"], "modality_unknown")
        self.assertEqual(
            supported["required_representations"],
            [
                {
                    "capability": "input_modality_image",
                    "representation": "local_file_reference",
                    "modality": "image",
                    **_ROUTE,
                }
            ],
        )

    def test_primary_handoff_binds_fresh_media_evidence_to_its_selected_route(self) -> None:
        decision = _primary_handoff_decision()

        self.assertEqual(
            decision["route"],
            {"executor": "codex", **_ROUTE},
        )
        self.assertEqual(
            decision["required_representations"],
            [
                {
                    "capability": "input_modality_image",
                    "representation": "raw_media",
                    "modality": "image",
                    **_ROUTE,
                }
            ],
        )
        self.assertEqual(decision["verdict"], "dispatch")

        for mismatch in (
            {**_ROUTE, "provider": "anthropic"},
            {**_ROUTE, "wire_model": "gpt-5-mini"},
        ):
            with self.subTest(scope=mismatch):
                self.assertEqual(_primary_handoff_decision(scope=mismatch)["verdict"], "modality_unknown")

        # The real recommendation resolver names a provider and a model id
        # and no endpoint mode; that binds the provider's default endpoint,
        # which is what the recorded `default` scope above describes.
        missing_endpoint = {
            **_PRIMARY_RECOMMENDATION,
            "selected": {key: value for key, value in _PRIMARY_RECOMMENDATION["selected"].items() if key != "endpoint_mode"},
        }
        self.assertEqual(
            _primary_handoff_decision(recommendation=missing_endpoint)["verdict"],
            "dispatch",
        )
        # No recommendation at all is no route: the gate says so instead of
        # asking for evidence scoped to an empty provider and wire model.
        unresolved = _primary_handoff_decision(recommendation={"owner": "maestro", "status": "choice_required", "selected": None})
        self.assertEqual(unresolved["verdict"], "route_unresolved")
        self.assertEqual(unresolved["route"], {"executor": "codex", "provider": "", "wire_model": ""})
        self.assertNotIn("record fresh", unresolved["remaining_user_action"])
        self.assertIn("bind a confirmed-active model route", unresolved["remaining_user_action"])

        transformed = _primary_handoff_decision(
            capability="input_modality_text",
            input_representation="ocr_output",
            transformation={"kind": "ocr", "status": "observed", "evidence_ref": "operator:ocr-fixture"},
        )
        self.assertEqual(transformed["verdict"], "dispatch")
        self.assertEqual(
            transformed["transformation"],
            {"kind": "ocr", "status": "observed", "evidence_ref": "operator:ocr-fixture"},
        )

    def test_document_input_is_gated_exactly_like_image(self) -> None:
        """A PDF or office file handed to a coding owner is declared, matched
        against `input_modality_document` route evidence, and fails closed
        with the alternative representations named when that evidence is
        missing, stale, or says unavailable."""
        now = "2026-09-03T01:00:00Z"
        supported = build_executor_modality_decision(
            input_representation="raw_media:document",
            snapshot=_snapshot(capability="input_modality_document"),
            route=_ROUTE,
            now=now,
        )
        self.assertEqual(supported["verdict"], "dispatch")
        self.assertEqual(
            supported["required_representations"],
            [{"capability": "input_modality_document", "representation": "raw_media", "modality": "document", **_ROUTE}],
        )
        self.assertEqual(supported["remaining_user_action"], "")
        self.assertEqual(supported["alternative_representations"], [])

        fail_closed = {
            "unknown": build_executor_modality_decision(
                input_representation="raw_media:document",
                snapshot={"executor": "codex", "capabilities": {}},
                route=_ROUTE,
                now=now,
            ),
            # Image evidence never stands in for document evidence.
            "image_only": build_executor_modality_decision(
                input_representation="raw_media:document", snapshot=_snapshot(), route=_ROUTE, now=now
            ),
            "stale": build_executor_modality_decision(
                input_representation="raw_media:document",
                snapshot=_snapshot(capability="input_modality_document", observed_at="2026-09-01T00:00:00Z"),
                route=_ROUTE,
                now=now,
            ),
            "unsupported": build_executor_modality_decision(
                input_representation="raw_media:document",
                snapshot=_snapshot("unavailable", capability="input_modality_document"),
                route=_ROUTE,
                now=now,
            ),
            "referenced": build_executor_modality_decision(
                input_representation="local_file_reference:document",
                snapshot={"executor": "codex", "capabilities": {}},
                route=_ROUTE,
                now=now,
            ),
        }
        for label, decision in fail_closed.items():
            with self.subTest(label=label):
                expected = "modality_unsupported" if label == "unsupported" else "modality_unknown"
                self.assertEqual(decision["verdict"], expected)
                self.assertEqual(decision["alternative_representations"], _DOCUMENT_ALTERNATIVES)
                action = str(decision["remaining_user_action"])
                self.assertIn("input_modality_document", action)
                self.assertIn("hand the document over as extracted_text", action)
                self.assertIn("read_file", action)
                self.assertIn("ocr_output", action)
                self.assertIn("observed OCR transformation", action)
        self.assertEqual(fail_closed["stale"]["freshness"], "stale_or_unknown")

        for route in (None, {}, {"provider": "openai"}, {"wire_model": "gpt-5"}):
            with self.subTest(route=route):
                unresolved = build_executor_modality_decision(
                    input_representation="raw_media:document",
                    snapshot=_snapshot(capability="input_modality_document"),
                    route=route,
                    now=now,
                )
                self.assertEqual(unresolved["verdict"], "route_unresolved")
                self.assertEqual(unresolved["alternative_representations"], _DOCUMENT_ALTERNATIVES)
                self.assertEqual(unresolved["evidence_ref"], "")
                self.assertEqual(unresolved["freshness"], "not_required")
                action = str(unresolved["remaining_user_action"])
                self.assertIn("bind a confirmed-active model route", action)
                self.assertIn("input_modality_document", action)
                self.assertNotIn("record fresh", action)
                self.assertIn("hand the document over as extracted_text", action)
        text_without_route = build_executor_modality_decision(input_representation="text_only", snapshot=None, route=None, now=now)
        self.assertEqual(text_without_route["verdict"], "dispatch")
        self.assertEqual(FAIL_CLOSED_VERDICTS, ("route_unresolved", "modality_unknown", "modality_unsupported", "modality_transformation_unobserved"))
        self.assertEqual(
            ALTERNATIVE_REPRESENTATIONS_BY_MODALITY,
            {"document": ("extracted_text", "ocr_output"), "image": ("ocr_output",), "audio": ("transcript",), "video": ("transcript",)},
        )
        image_unknown = build_executor_modality_decision(
            input_representation="raw_media:image", snapshot={"executor": "codex", "capabilities": {}}, route=_ROUTE, now=now
        )
        self.assertEqual(image_unknown["alternative_representations"], ["ocr_output"])
        self.assertIn("hand the image over as ocr_output", str(image_unknown["remaining_user_action"]))

        extracted = build_executor_modality_decision(
            input_representation="extracted_text",
            snapshot=_snapshot(capability="input_modality_text"),
            route=_ROUTE,
            now=now,
        )
        self.assertEqual(extracted["verdict"], "dispatch")
        self.assertEqual(extracted["transformation"]["status"], "not_required")
        # The advertised way out is never a dead end: text is the final
        # representation, and its own action names the one row it still
        # needs and how to record it.
        self.assertIn("route then needs only input_modality_text evidence", str(fail_closed["unknown"]["remaining_user_action"]))
        text_lane = {
            "unknown": build_executor_modality_decision(input_representation="extracted_text", snapshot={"executor": "codex", "capabilities": {}}, route=_ROUTE, now=now),
            "unsupported": build_executor_modality_decision(input_representation="extracted_text", snapshot=_snapshot("unavailable", capability="input_modality_text"), route=_ROUTE, now=now),
            "unresolved": build_executor_modality_decision(input_representation="extracted_text", snapshot=None, route=None, now=now),
        }
        self.assertEqual(text_lane["unknown"]["verdict"], "modality_unknown")
        self.assertIn("omh coding capability-snapshot record", text_lane["unknown"]["remaining_user_action"])
        self.assertIn("text is the final representation", text_lane["unknown"]["remaining_user_action"])
        self.assertEqual(text_lane["unsupported"]["verdict"], "modality_unsupported")
        self.assertIn("has no further alternative", text_lane["unsupported"]["remaining_user_action"])
        self.assertEqual(text_lane["unresolved"]["verdict"], "route_unresolved")
        self.assertIn("bind a confirmed-active model route", text_lane["unresolved"]["remaining_user_action"])
        for decision in text_lane.values():
            self.assertEqual(decision["alternative_representations"], [])
            self.assertTrue(decision["remaining_user_action"])
        unobserved = build_executor_modality_decision(
            input_representation="ocr_output",
            snapshot=_snapshot(capability="input_modality_text"),
            route=_ROUTE,
            now=now,
            transformation={"kind": "ocr", "status": "prepared_not_observed", "evidence_ref": ""},
        )
        self.assertEqual(unobserved["verdict"], "modality_transformation_unobserved")
        self.assertIn("record the observed ocr transformation", str(unobserved["remaining_user_action"]))

    def test_document_primary_handoff_binds_evidence_to_its_selected_route(self) -> None:
        for representation in ("raw_media:document", "local_file_reference:document"):
            with self.subTest(representation=representation):
                decision = _primary_handoff_decision(
                    capability="input_modality_document",
                    input_representation=representation,
                )
                self.assertEqual(decision["verdict"], "dispatch")
                self.assertEqual(decision["route"], {"executor": "codex", **_ROUTE})
                self.assertEqual(decision["required_representations"][0]["capability"], "input_modality_document")
        mismatched = _primary_handoff_decision(
            capability="input_modality_document",
            input_representation="raw_media:document",
            scope={**_ROUTE, "wire_model": "gpt-5-mini"},
        )
        self.assertEqual(mismatched["verdict"], "modality_unknown")
        self.assertEqual(mismatched["alternative_representations"], _DOCUMENT_ALTERNATIVES)
        extracted = _primary_handoff_decision(
            capability="input_modality_text",
            input_representation="extracted_text",
        )
        self.assertEqual(extracted["verdict"], "dispatch")

    def test_fail_closed_action_names_no_coding_owner(self) -> None:
        """The remaining action is the same sentence for every executor: it
        speaks about route evidence and representations, never about Codex,
        Claude Code, Hermes runtime, or a generic profile as the fix."""
        for executor in ("codex", "claude-code", "hermes", "generic"):
            for status in ("unavailable", "host_observed"):
                with self.subTest(executor=executor, status=status):
                    decision = build_executor_modality_decision(
                        input_representation="raw_media:document",
                        snapshot=_snapshot(status, capability="input_modality_image", executor=executor),
                        route=_ROUTE,
                        now="2026-09-03T01:00:00Z",
                    )
                    self.assertNotEqual(decision["verdict"], "dispatch")
                    prose = " ".join(
                        str(decision[key]) for key in ("remaining_user_action", "fallback_reason", "claim_boundary")
                    ).lower()
                    for owner_word in ("codex", "claude", "maestro", "generic"):
                        self.assertNotIn(owner_word, prose)

    def test_attachment_metadata_declares_document_representation_without_bytes(self) -> None:
        cases = {
            ("spec.pdf", ""): "document",
            ("", "application/pdf"): "document",
            ("brief.docx", ""): "document",
            ("deck.PPTX", "application/octet-stream"): "document",
            ("", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"): "document",
            ("", "application/vnd.oasis.opendocument.text"): "document",
            ("diagram.png", ""): "image",
            ("", "image/jpeg; charset=binary"): "image",
            ("call.m4a", ""): "audio",
            ("demo.mp4", ""): "video",
            ("notes.txt", "text/plain"): "",
            ("data.json", "application/json"): "",
            ("archive.zip", ""): "",
            (".pdf", ""): "",
            ("pdf", ""): "",
        }
        for (name, media_type), expected in cases.items():
            with self.subTest(name=name, media_type=media_type):
                self.assertEqual(attachment_input_modality(name=name, media_type=media_type), expected)

        rows = [
            {"filename": "spec.pdf", "content_type": "application/pdf", "url": "https://cdn.example/spec.pdf", "size": 9},
            {"name": "diagram.png", "mimetype": "image/png"},
            {"name": "notes.txt", "mimetype": "text/plain"},
            {"file_name": "appendix.pdf", "mime_type": "application/pdf"},
            "not-a-row",
        ]
        self.assertEqual(input_representations_from_attachments(rows), ["raw_media:document", "raw_media:image"])
        self.assertEqual(input_representations_from_attachments([{"name": "notes.txt"}]), [])
        # Every row is classified: a PDF behind any number of text logs is
        # still declared, and there is no row count at which the gate goes
        # quiet.
        for text_rows in (32, 39, 500):
            with self.subTest(text_rows=text_rows):
                crowded = [{"name": f"log-{index}.txt", "mimetype": "text/plain"} for index in range(text_rows)]
                crowded.append({"name": "parser-spec.pdf", "mimetype": "application/pdf"})
                self.assertEqual(input_representations_from_attachments(crowded), ["raw_media:document"])
        # A long name with no media type classifies by its suffix, and the
        # event reader keeps that suffix when it bounds the name.
        from omh.ingress import extract_event_attachments

        long_named = extract_event_attachments({"message": {"document": {"file_name": ("s" * 200) + ".pdf"}}})
        self.assertEqual(input_representations_from_attachments(long_named), ["raw_media:document"])
        self.assertEqual(attachment_input_modality(name=("s" * 200) + ".pdf"), "document")
        self.assertEqual(input_representations_from_attachments("spec.pdf"), [])
        self.assertEqual(
            merged_input_representation(["raw_media:document"], ["raw_media:document", "raw_media:image"]),
            ["raw_media:document", "raw_media:image"],
        )
        self.assertEqual(merged_input_representation(None, [], "text_only"), "text_only")

    def test_transformed_media_requires_observed_transformation_and_demo_is_private(self) -> None:
        unobserved = build_executor_modality_decision(
            input_representation="ocr_output", snapshot=_snapshot(), route=_ROUTE,
            transformation={"kind": "ocr", "status": "prepared_not_observed", "evidence_ref": ""},
        )
        self.assertEqual(unobserved["verdict"], "modality_transformation_unobserved")
        demo = demo_media_handoff_decisions()
        self.assertEqual(demo["schema_version"], "omh_media_handoff_decision_demo/v1")
        self.assertEqual(demo["supported"]["verdict"], "dispatch")
        self.assertNotEqual(demo["unknown"]["verdict"], "dispatch")
        self.assertNotEqual(demo["fallback_rechecked"]["verdict"], "dispatch")
        self.assertEqual(demo["document_supported"]["verdict"], "dispatch")
        self.assertEqual(demo["document_unknown"]["verdict"], "modality_unknown")
        self.assertEqual(demo["document_unsupported"]["verdict"], "modality_unsupported")
        self.assertEqual(demo["document_unsupported"]["alternative_representations"], _DOCUMENT_ALTERNATIVES)
        self.assertEqual(demo["document_extracted_text"]["verdict"], "dispatch")
        serialized = json.dumps(demo, sort_keys=True)
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn("bytes", serialized)

    def test_transformation_evidence_is_schema_bound_private_and_kind_matched(self) -> None:
        unsafe_transformations = (
            {
                "kind": "ocr",
                "status": "observed",
                "evidence_ref": "/Users/alice/.ssh/id_rsa",
                "api_key": "sk-live-secret",
            },
            {
                "kind": "transcription",
                "status": "observed",
                "evidence_ref": "operator:wrong-kind",
            },
            {
                "kind": "ocr",
                "status": "observed",
                "evidence_ref": "",
            },
        )

        for transformation in unsafe_transformations:
            with self.subTest(transformation=transformation):
                decision = _primary_handoff_decision(
                    capability="input_modality_text",
                    input_representation="ocr_output",
                    transformation=transformation,
                )

                self.assertEqual(decision["verdict"], "modality_transformation_unobserved")
                self.assertEqual(set(decision["transformation"]), {"kind", "status", "evidence_ref"})
                self.assertNotEqual(decision["transformation"]["status"], "observed")
                self.assertEqual(decision["transformation"]["evidence_ref"], "")
                serialized = json.dumps(decision, sort_keys=True)
                self.assertNotIn("/Users/", serialized)
                self.assertNotIn("sk-live-secret", serialized)
                self.assertNotIn("api_key", serialized)


if __name__ == "__main__":
    unittest.main()

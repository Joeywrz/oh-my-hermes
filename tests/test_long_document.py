from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.awareness import awareness_route_hint
from omh.routing.chat import route_chat_message
from omh.routing.localization import normalized_phrase, routing_tokens
from omh.routing.policy import active_routing_guard_rules
from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates, builtin_skill_templates
from omh.workflows.long_document import (
    DEFAULT_PAGES_PER_RANGE,
    DELEGATION_RANGE_THRESHOLD,
    HERMES_READ_FILE_CHAR_BUDGET,
    LONG_DOCUMENT_CARD_SCHEMA_VERSION,
    LONG_DOCUMENT_NOT_OBSERVED,
    build_chunk_ledger,
    build_long_document_card,
    normalize_long_document_source_state,
    pages_per_range_for_budget,
    parse_page_range,
    plan_page_ranges,
    validate_long_document_card,
)
from omh.wrapper.contract import build_chat_interaction_payload


class LongDocumentContractTests(unittest.TestCase):
    def test_pages_per_range_follows_the_read_budget(self) -> None:
        # 100,000 chars at 1,600 chars/page with ~4% headroom is 60 pages.
        self.assertEqual(pages_per_range_for_budget(HERMES_READ_FILE_CHAR_BUDGET, 1_600), DEFAULT_PAGES_PER_RANGE)
        # A host with a bigger budget gets bigger ranges; a denser document smaller ones.
        self.assertEqual(pages_per_range_for_budget(200_000, 1_600), 120)
        self.assertEqual(pages_per_range_for_budget(HERMES_READ_FILE_CHAR_BUDGET, 3_200), 30)
        self.assertEqual(pages_per_range_for_budget(0, 1_600), DEFAULT_PAGES_PER_RANGE)

    def test_page_ranges_cover_the_document_in_order(self) -> None:
        self.assertEqual(plan_page_ranges(300, 60), [(1, 60), (61, 120), (121, 180), (181, 240), (241, 300)])
        self.assertEqual(plan_page_ranges(61, 60), [(1, 60), (61, 61)])
        self.assertEqual(plan_page_ranges(0, 60), [])

    def test_page_range_parsing_accepts_only_ordered_positive_ranges(self) -> None:
        self.assertEqual(parse_page_range("12-30"), (12, 30))
        self.assertEqual(parse_page_range("7"), (7, 7))
        self.assertIsNone(parse_page_range("30-12"))
        self.assertIsNone(parse_page_range("0-3"))
        self.assertIsNone(parse_page_range("all"))

    def test_chunk_ledger_marks_exactly_one_next_range(self) -> None:
        ledger = build_chunk_ledger(300, pages_per_range=60, covered_ranges=["1-60", "61-120"], scanned_ranges=["200-210"])

        self.assertEqual([entry["state"] for entry in ledger], ["covered", "covered", "next", "missing", "missing"])
        self.assertEqual([entry["pages"] for entry in ledger], ["1-60", "61-120", "121-180", "181-240", "241-300"])
        # Page anchors map back to an estimated extraction offset.
        self.assertEqual(ledger[2]["offset"], 120 * 1_600)
        self.assertEqual(ledger[2]["chars"], 60 * 1_600)
        self.assertEqual([entry["scanned"] for entry in ledger], [False, False, False, True, False])

    def test_source_state_claims_full_text_only_when_ranges_reach_the_page_count(self) -> None:
        self.assertEqual(normalize_long_document_source_state("metadata_only")["state"], "metadata_only")
        self.assertEqual(normalize_long_document_source_state("metadata_only", page_count=300)["state"], "page_count_observed")
        partial = normalize_long_document_source_state("full_text_observed", page_count=300, covered_ranges=["1-60"])
        self.assertEqual(partial["state"], "range_text_observed")
        full = normalize_long_document_source_state("full_text_observed", page_count=120, covered_ranges=["1-60", "61-120"])
        self.assertEqual(full["state"], "full_text_observed")
        self.assertEqual(full["covered_ranges"], ["1-60", "61-120"])
        self.assertEqual(normalize_long_document_source_state("made up")["state"], "unknown_or_missing")

    def test_card_delegates_above_the_range_threshold_and_keeps_boundaries(self) -> None:
        card = build_long_document_card(
            title="Vendor Master Agreement",
            source_ref="/tmp/vendor.pdf",
            document_kind="contract",
            page_count=300,
            source_state="page_count_observed",
            reading_goal="every obligation with a deadline",
            evidence_ref="pdf_read:meta",
        )

        self.assertEqual(validate_long_document_card(card), [])
        self.assertEqual(card["schema_version"], LONG_DOCUMENT_CARD_SCHEMA_VERSION)
        self.assertEqual(card["document_identity"]["document_kind"], "contract")
        self.assertEqual(card["read_budget"]["pages_per_range"], DEFAULT_PAGES_PER_RANGE)
        self.assertEqual(card["read_budget"]["estimated_read_calls"], 5)
        self.assertFalse(card["read_budget"]["single_read_fits"])
        self.assertEqual(card["delegation_policy"]["mode"], "delegate_ranges")
        self.assertEqual(card["delegation_policy"]["threshold_ranges"], DELEGATION_RANGE_THRESHOLD)
        self.assertIn("{pages}", card["delegation_policy"]["per_range_brief"])
        self.assertEqual(card["not_observed"], list(LONG_DOCUMENT_NOT_OBSERVED))
        self.assertEqual(card["source_state"]["state"], "page_count_observed")

        small = build_long_document_card(title="Memo", page_count=40)
        self.assertTrue(small["read_budget"]["single_read_fits"])
        self.assertEqual(small["delegation_policy"]["mode"], "sequential_ranges")
        self.assertEqual(len(small["chunk_ledger"]), 1)

    def test_card_without_a_page_count_has_an_empty_ledger_and_still_validates(self) -> None:
        card = build_long_document_card(title="unknown")

        self.assertEqual(card["chunk_ledger"], [])
        self.assertIsNone(card["chunking_policy"]["range_count_when_known"])
        self.assertEqual(card["source_state"]["state"], "unknown_or_missing")
        self.assertEqual(validate_long_document_card(card), [])

    def test_validation_names_each_broken_boundary(self) -> None:
        card = build_long_document_card(title="x", page_count=120)
        card["chunk_ledger"][1]["state"] = "next"
        card["not_observed"] = ["page_count"]
        card["coverage_policy"] = "lossy"

        errors = validate_long_document_card(card)

        self.assertIn("chunk_ledger may name at most one next range", errors)
        self.assertIn("coverage_policy must preserve page-anchored coverage", errors)
        self.assertTrue(any(error.startswith("not_observed must include") for error in errors))


class LongDocumentRoutingTests(unittest.TestCase):
    def test_large_document_reading_requests_dispatch_the_skill(self) -> None:
        for message in (
            "summarize this 300-page vendor contract pdf and list every obligation with a deadline",
            "this pdf is 300 pages, how do I get hermes to read it all?",
            "the contract is 120 pages, summarize the termination clauses",
            "read this manual and tell me how to configure the device",
            "process this very large pdf",
            "summarize this document",
            "이 300페이지 PDF 요약해줘",
            "이 계약서 요약해줘",
            "このPDFを要約して",
            "总结这份合同",
        ):
            with self.subTest(message=message):
                route = route_chat_message(message, source="discord")
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], "long-document-reading")

    def test_sibling_lanes_keep_their_requests(self) -> None:
        for message, expected in (
            ("turn this 300 page pdf into slides", "materials-package"),
            ("summarize this Word document and extract action items", "materials-package"),
            ("compare these two PDFs and summarize the differences", "materials-package"),
            ("extract tables from this PDF into CSV", "materials-package"),
            ("summarize this PDF deck into action items", "materials-package"),
            ("explain this paper at a beginner level", "paper-learning"),
            ("explain this PDF at an easy level", "paper-learning"),
            ("논문 PDF를 쉬운 수준으로 섹션별로 해설해줘", "paper-learning"),
            ("find the pdf of this paper", "source-finder"),
        ):
            with self.subTest(message=message):
                self.assertEqual(route_chat_message(message, source="discord")["selected_skill"], expected)

    def test_generic_words_in_another_sense_never_reach_the_skill(self) -> None:
        for message in (
            "document the API",
            "read the README",
            "process this refund",
            "read the room before the meeting",
            "summarize this paragraph in Korean",
            "our landing pages get 300 visits a day, summarize the report",
        ):
            with self.subTest(message=message):
                route = route_chat_message(message, source="discord")
                self.assertNotEqual(route["selected_skill"], "long-document-reading")
                self.assertNotIn("long-document-reading", [rec["skill"] for rec in route["recommendations"][:1]])

    def test_guard_requires_a_document_noun_a_size_cue_and_a_reading_verb(self) -> None:
        def guard_ids(message: str) -> list[str]:
            normalized = normalized_phrase(message)
            return [rule.id for rule in active_routing_guard_rules(normalized, routing_tokens(normalized))]

        self.assertIn("long_document_reading_before_paper_or_materials", guard_ids("summarize this huge pdf"))
        self.assertIn("long_document_reading_before_paper_or_materials", guard_ids("the manual is 200 pages, read it"))
        # A page count under one read is not a long document.
        self.assertNotIn("long_document_reading_before_paper_or_materials", guard_ids("summarize this 20 page pdf"))
        # A paper stays a paper even at 300 pages.
        self.assertNotIn("long_document_reading_before_paper_or_materials", guard_ids("read this 300 page paper"))
        # When the long-document guard fires, paper tutoring and file packaging stand down.
        ids = guard_ids("summarize this 300-page vendor contract pdf")
        self.assertNotIn("paper_learning_before_materials_or_research_ops", ids)
        self.assertNotIn("materials_package_before_report_or_clarify", ids)

    def test_awareness_hint_names_the_skill_and_its_next_action(self) -> None:
        for message in (
            "summarize this 300-page vendor contract pdf and list every obligation with a deadline",
            "이 300페이지 PDF 요약해줘",
            "read this manual and tell me how to configure the device",
        ):
            with self.subTest(message=message):
                hint = awareness_route_hint(message)
                self.assertEqual(hint["primary_workflow"], "long-document-reading")
                self.assertEqual(hint["primary_next_action"], "prepare_long_document_reading")
                self.assertEqual(hint["hints"][0]["workflow_context_card"]["id"], "research_and_ops")
        self.assertEqual(awareness_route_hint("turn this 300 page pdf into slides")["primary_workflow"], "materials-package")
        self.assertEqual(awareness_route_hint("explain this paper at a beginner level")["primary_workflow"], "paper-learning")

    def test_wrapper_card_is_dedicated_and_keeps_extraction_unobserved(self) -> None:
        payload = build_chat_interaction_payload(
            "summarize this 300-page vendor contract pdf and list every obligation with a deadline",
            source="discord",
        )
        response = payload["chat_response"]

        self.assertEqual(payload["next_action"], "prepare_long_document_reading")
        self.assertEqual(response["kind"], "long_document_reading")
        self.assertEqual(response["state"]["artifact_schema"], "long_document_card/v1")
        self.assertIn("text extraction", response["state"]["evidence_not_observed"])
        self.assertIn("whole-document coverage", response["state"]["evidence_not_observed"])
        self.assertEqual(response["actions"][0]["id"], "prepare_long_document_reading")


class LongDocumentSkillBodyTests(unittest.TestCase):
    def test_generated_body_carries_the_recipe_and_the_limits_reference(self) -> None:
        template = next(t for t in builtin_skill_templates() if t.name == "long-document-reading")
        reference = next(
            t for t in builtin_skill_reference_templates()
            if t.skill_name == "long-document-reading" and t.relative_path == "references/hermes-pdf-limits.md"
        )

        for fragment in (
            "## Long Document Reading Protocol",
            "pdf_read.py <file> --meta",
            "extract_pymupdf.py <file> --pages",
            "pdf_split.py <file> --pages",
            "delegate_task",
            "pdf_page_image.py",
            "vision_analyze",
            "covered / next / missing",
            "references/hermes-pdf-limits.md",
        ):
            self.assertIn(fragment, template.content, fragment)
        self.assertIn("100,000", reference.content)
        self.assertIn("file_read_max_chars", reference.content)
        self.assertIn("ocr-and-documents", reference.content)

    def test_catalog_definition_defers_to_its_siblings(self) -> None:
        definition = next(d for d in builtin_definitions() if d.name == "long-document-reading")
        statements = " ".join(definition.do_not_use_when)

        for sibling in ("`paper-learning`", "`materials-package`", "`media-input-operator`", "`source-finder`"):
            self.assertIn(sibling, statements)
        self.assertIn("summarize this pdf", definition.triggers)
        self.assertIn("long document", definition.triggers)


if __name__ == "__main__":
    unittest.main()

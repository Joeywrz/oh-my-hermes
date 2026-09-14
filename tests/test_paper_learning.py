from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()

from omh.paper_learning import (
    PAPER_LEARNING_CARD_SCHEMA_VERSION,
    PAPER_LEARNING_NOT_OBSERVED,
    PAPER_LEARNING_RECORD_SCHEMA_VERSION,
    apply_paper_progress,
    build_coverage_ledger,
    build_paper_learning_card,
    build_paper_learning_record,
    describe_paper_source,
    list_paper_learning_records,
    normalize_paper_learning_level,
    normalize_paper_source_state,
    read_paper_progress_ledger,
    record_paper_progress,
    render_paper_learning_record_text,
    show_paper_learning_record,
    summarize_paper_learning_record,
    validate_paper_learning_card,
    validate_paper_learning_record,
    validate_paper_learning_store,
    write_paper_learning_record,
)
from omh.paths import OmhPaths


class PaperLearningContractTests(unittest.TestCase):
    def test_level_aliases_keep_contract_ids(self) -> None:
        self.assertEqual(normalize_paper_learning_level("very easy"), "very_easy")
        self.assertEqual(normalize_paper_learning_level("전문가급"), "expert")
        self.assertEqual(normalize_paper_learning_level("moderate"), "moderate")
        self.assertEqual(normalize_paper_learning_level(None), "choose")
        self.assertEqual(normalize_paper_learning_level("surprise me"), "choose")

    def test_source_state_distinguishes_metadata_excerpt_extraction_and_full_text(self) -> None:
        metadata = normalize_paper_source_state("metadata_only")
        self.assertEqual(metadata["state"], "metadata_only")

        excerpt = normalize_paper_source_state("metadata_only", observed_sections=["Abstract"])
        self.assertEqual(excerpt["state"], "excerpt_text_observed")
        self.assertEqual(excerpt["observed_sections"], ["Abstract"])

        partial = normalize_paper_source_state(
            "full_text_observed",
            observed_sections=["Abstract", "Method"],
            missing_sections=["Results"],
            evidence_ref="extract:1",
        )
        self.assertEqual(partial["state"], "file_text_extraction_observed")
        self.assertEqual(partial["evidence_ref"], "extract:1")

    def test_coverage_ledger_marks_observed_and_missing_sections(self) -> None:
        ledger = build_coverage_ledger(
            observed_sections=["Abstract"],
            missing_sections=["Figures, tables, and equations"],
            sections=["Abstract", "Method", "Figures, tables, and equations"],
        )

        self.assertEqual(ledger[0]["status"], "observed")
        self.assertEqual(ledger[1]["status"], "prepared")
        self.assertEqual(ledger[2]["status"], "missing")
        self.assertTrue(all(item["explanation_status"] == "pending" for item in ledger))

    def test_paper_learning_card_preserves_boundaries(self) -> None:
        card = build_paper_learning_card(
            title="Attention Is All You Need",
            authors=["Vaswani et al."],
            source_ref="arxiv:1706.03762",
            level="expert",
            source_state="file_text_extraction_observed",
            observed_sections=["Abstract", "Introduction"],
            missing_sections=["Figures, tables, and equations"],
            evidence_ref="wrapper-extract-001",
        )

        self.assertEqual(card["schema_version"], PAPER_LEARNING_CARD_SCHEMA_VERSION)
        self.assertEqual(card["level"], "expert")
        self.assertEqual(card["paper_identity"]["authors"], ["Vaswani et al."])
        self.assertEqual(card["source_state"]["state"], "file_text_extraction_observed")
        self.assertIn("coverage_preserving_not_lossy_summary", card["coverage_policy"])
        self.assertIn("continue_next_section", card["available_actions"])
        for boundary in PAPER_LEARNING_NOT_OBSERVED:
            self.assertIn(boundary, card["not_observed"])
        self.assertEqual(validate_paper_learning_card(card), [])


class PaperLearningStoreTests(unittest.TestCase):
    def test_record_wraps_card_and_classifies_source_without_parsing_it(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "paper.pdf"
            source.write_bytes(b"binary that is not text")

            record = build_paper_learning_record(
                title="  Attention Is All You Need ",
                source_ref=str(source),
                level="advanced",
                created_at="2026-09-13T10:00:00Z",
            )

            self.assertEqual(record["schema_version"], PAPER_LEARNING_RECORD_SCHEMA_VERSION)
            self.assertRegex(record["paper_id"], r"^20260913T100000Z-attention-is-all-you-need-[0-9a-f]{6}$")
            self.assertEqual(record["card"]["level"], "expert")
            self.assertEqual(record["card"]["paper_identity"]["title"], "Attention Is All You Need")
            self.assertEqual(record["source"]["kind"], "file")
            self.assertEqual(record["source"]["size_bytes"], len(b"binary that is not text"))
            self.assertEqual(record["reading"], {
                "status": "not_started",
                "covered": [],
                "next": "Abstract",
                "missing": [],
                "explained_count": 0,
                "section_count": 10,
                "last_note": "",
            })
            self.assertEqual(validate_paper_learning_record(record), [])
            self.assertEqual(validate_paper_learning_card(record["card"]), [])

        self.assertEqual(describe_paper_source("")["kind"], "none")
        self.assertEqual(describe_paper_source("HTTPS://arxiv.org/abs/1706.03762")["kind"], "url")
        self.assertEqual(describe_paper_source("arxiv:1706.03762")["kind"], "reference")
        self.assertEqual(describe_paper_source("/definitely/not/a/file.pdf")["kind"], "reference")
        with self.assertRaises(ValueError):
            build_paper_learning_record(title="   ")

    def test_apply_progress_is_pure_and_matches_sections_loosely(self) -> None:
        record = build_paper_learning_record(title="T", sections=["Abstract", "Method", "Results"], created_at="2026-09-13T10:00:00Z")

        updated, entry = apply_paper_progress(record, covered=["ABSTRACT", "abstract"], note="  two   words ", recorded_at="2026-09-13T11:00:00Z")

        self.assertEqual(record["progress_count"], 0)
        self.assertEqual(record["card"]["coverage_ledger"][0]["explanation_status"], "pending")
        self.assertEqual(updated["progress_count"], 1)
        self.assertEqual(updated["updated_at"], "2026-09-13T11:00:00Z")
        self.assertEqual(updated["card"]["coverage_ledger"][0], {"paper_section": "Abstract", "status": "observed", "explanation_status": "explained"})
        self.assertEqual(updated["card"]["source_state"]["state"], "excerpt_text_observed")
        self.assertEqual(updated["reading"]["status"], "in_progress")
        self.assertEqual(updated["reading"]["next"], "Method")
        self.assertEqual(updated["reading"]["last_note"], "two words")
        self.assertEqual(entry["covered"], ["Abstract"])
        self.assertEqual(entry["part_index"], 1)
        self.assertEqual(entry["recorded_at"], "2026-09-13T11:00:00Z")

        second, _ = apply_paper_progress(updated, covered=["Method"], next_section="results")
        self.assertEqual(second["reading"]["next"], "Results")
        self.assertEqual(second["reading"]["last_note"], "two words")

        final, _ = apply_paper_progress(second, covered=["Results"], source_state="full_text_observed")
        self.assertEqual(final["reading"]["status"], "complete")
        self.assertEqual(final["reading"]["next"], "")
        self.assertEqual(final["card"]["source_state"]["state"], "full_text_observed")

        with self.assertRaisesRegex(ValueError, "not a section"):
            apply_paper_progress(record, next_section="Conclusion")
        with self.assertRaisesRegex(ValueError, "both covered and missing"):
            apply_paper_progress(record, covered=["Abstract"], missing=["Abstract"])
        with self.assertRaisesRegex(ValueError, "nothing to record"):
            apply_paper_progress(record)
        with self.assertRaisesRegex(ValueError, "source_state must be one of"):
            apply_paper_progress(record, source_state="pdf_read")

    def test_store_round_trip_ledger_and_validation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            record = build_paper_learning_record(title="Stored paper", source_ref="arxiv:1")

            written = write_paper_learning_record(paths, record)
            paper_id = written["paper_id"]
            with self.assertRaisesRegex(ValueError, "already exists"):
                write_paper_learning_record(paths, record)

            updated, entry = record_paper_progress(paths, paper_id, covered=["Abstract"], missing=["Limitations"])
            self.assertEqual(updated["progress_count"], 1)
            self.assertEqual(show_paper_learning_record(paths, paper_id)["reading"]["missing"], ["Limitations"])
            entries, errors = read_paper_progress_ledger(paths, paper_id)
            self.assertEqual(errors, [])
            self.assertEqual(entries, [entry])
            self.assertEqual([item["paper_id"] for item in list_paper_learning_records(paths)], [paper_id])
            self.assertEqual(list_paper_learning_records(paths, limit=0), [])
            summary = summarize_paper_learning_record(updated)
            self.assertEqual(summary["reading_status"], "in_progress")
            self.assertEqual(summary["missing_count"], 1)
            index = json.loads(paths.paper_learning_index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["schema_version"], "omh_paper_learning_index/v1")
            self.assertEqual(index["papers"][0]["paper_id"], paper_id)
            text = render_paper_learning_record_text(updated, entries)
            self.assertIn("next: Introduction", text)
            self.assertIn("missing: Limitations", text)
            self.assertIn("part 1: covered Abstract; next Introduction; missing Limitations", text)

            self.assertTrue(validate_paper_learning_store(paths)["ok"])
            with self.assertRaises(FileNotFoundError):
                show_paper_learning_record(paths, "../outside")
            with self.assertRaises(FileNotFoundError):
                show_paper_learning_record(paths, "missing-id")

            card_path = paths.paper_learning_dir / paper_id / "card.json"
            broken = json.loads(card_path.read_text(encoding="utf-8"))
            broken["progress_count"] = 3
            broken["card"]["level"] = "genius"
            card_path.write_text(json.dumps(broken), encoding="utf-8")
            result = validate_paper_learning_store(paths)
            self.assertFalse(result["ok"])
            joined = "\n".join(result["errors"])
            self.assertIn("card: level must be one of", joined)
            self.assertIn("progress_count 3 but the ledger holds 1 entries", joined)

    def test_store_is_empty_when_directory_is_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = OmhPaths(omh_home=Path(tmp) / ".omh", hermes_home=Path(tmp) / ".hermes")
            self.assertEqual(list_paper_learning_records(paths), [])
            result = validate_paper_learning_store(paths)
            self.assertTrue(result["ok"])
            self.assertEqual(result["paper_count"], 0)


if __name__ == "__main__":
    unittest.main()

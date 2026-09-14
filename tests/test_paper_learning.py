from __future__ import annotations

import json
import os
import stat
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.paper_learning import (
    PAPER_LEARNING_CARD_SCHEMA_VERSION,
    PAPER_LEARNING_NOT_OBSERVED,
    PAPER_LEARNING_RECORD_SCHEMA_VERSION,
    PaperLearningStoreError,
    apply_paper_progress,
    build_coverage_ledger,
    build_paper_learning_card,
    build_paper_learning_record,
    describe_paper_source,
    list_paper_learning_records,
    normalize_paper_learning_level,
    normalize_paper_source_state,
    paper_learning_level_or_none,
    read_paper_progress_ledger,
    record_paper_progress,
    render_paper_learning_record_text,
    scan_paper_learning_store,
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
        self.assertEqual(paper_learning_level_or_none("advanced"), "expert")
        self.assertEqual(paper_learning_level_or_none("ask"), "choose")
        self.assertEqual(paper_learning_level_or_none(None), "choose")
        self.assertIsNone(paper_learning_level_or_none("expret"))

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
            self.assertIsInstance(record["created_at_ns"], int)
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
        with self.assertRaisesRegex(ValueError, "non-empty section"):
            build_paper_learning_record(title="T", sections=["Abstract", ""])
        with self.assertRaisesRegex(ValueError, "non-empty section"):
            build_paper_learning_record(title="T", sections=[])
        self.assertEqual(len(build_paper_learning_record(title="T")["card"]["coverage_ledger"]), 10)

    def test_source_hash_stats_first_respects_budget_and_classifies_unreadable(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "big.pdf"
            source.write_bytes(b"x" * 64)
            with patch("omh.workflows.paper_learning.PAPER_SOURCE_HASH_BYTE_BUDGET", 32):
                over = describe_paper_source(str(source))
            self.assertEqual(over["kind"], "file")
            self.assertEqual(over["sha256"], "")
            self.assertEqual(over["hash_skipped"], "over_budget")
            self.assertEqual(over["size_bytes"], 64)
            self.assertEqual(over["hash_budget_bytes"], 32)
            within = describe_paper_source(str(source))
            self.assertEqual(within["hash_skipped"], "")
            self.assertRegex(within["sha256"], r"^[0-9a-f]{64}$")
            record = build_paper_learning_record(title="Big", source_ref=str(source))
            self.assertEqual(record["source"]["hash_skipped"], "")

            if sys.platform != "win32" and os.geteuid() != 0:
                source.chmod(0)
                try:
                    with self.assertRaises(PaperLearningStoreError) as denied:
                        describe_paper_source(str(source))
                    self.assertEqual(denied.exception.reason_code, "source_unreadable")
                    self.assertIn("big.pdf", denied.exception.detail)
                    with self.assertRaises(PaperLearningStoreError):
                        build_paper_learning_record(title="Big", source_ref=str(source))
                finally:
                    source.chmod(stat.S_IRUSR | stat.S_IWUSR)

            # A file that changes size mid-hash is refused rather than recorded with a size its digest does not match.
            def growing_open(self_path, *args, **kwargs):
                # Builtin open on a string path bypasses the patched Path.open.
                with open(os.fspath(source), "wb") as grow:
                    grow.write(b"y" * 100)
                return open(os.fspath(self_path), *args, **kwargs)
            with patch.object(Path, "open", growing_open):
                with self.assertRaises(PaperLearningStoreError) as changed:
                    describe_paper_source(str(source))
            self.assertEqual(changed.exception.reason_code, "source_unreadable")
            self.assertIn("changed while it was being hashed", changed.exception.detail)

    def test_apply_progress_is_pure_and_matches_sections_loosely(self) -> None:
        record = build_paper_learning_record(title="T", sections=["Abstract", "Method", "Results"], created_at="2026-09-13T10:00:00Z")

        updated, entry = apply_paper_progress(record, covered=["ABSTRACT", "abstract"], note="  two   words ", recorded_at="2026-09-13T11:00:00Z")

        self.assertEqual(record["progress_count"], 0)
        self.assertEqual(record["card"]["coverage_ledger"][0]["explanation_status"], "pending")
        self.assertEqual(updated["progress_count"], 1)
        self.assertEqual(updated["updated_at"], "2026-09-13T11:00:00Z")
        self.assertEqual(updated["card"]["coverage_ledger"][0], {"paper_section": "Abstract", "status": "prepared", "explanation_status": "explained"})
        # An explanation claim is not a source-observation claim: the card
        # was planned as metadata_only and covering a section leaves it there.
        self.assertEqual(updated["card"]["source_state"]["state"], "metadata_only")
        self.assertEqual(updated["card"]["source_state"]["observed_sections"], [])
        self.assertEqual(updated["reading"]["status"], "in_progress")
        self.assertEqual(updated["reading"]["next"], "Method")
        self.assertEqual(updated["reading"]["last_note"], "two words")
        self.assertEqual(entry["covered"], ["Abstract"])
        self.assertEqual(entry["part_index"], 1)
        self.assertEqual(entry["recorded_at"], "2026-09-13T11:00:00Z")

        second, _ = apply_paper_progress(updated, covered=["Method"], next_section="results")
        self.assertEqual(second["reading"]["next"], "Results")
        self.assertEqual(second["reading"]["last_note"], "two words")

        with self.assertRaisesRegex(ValueError, "need --evidence-ref"):
            apply_paper_progress(second, covered=["Results"], source_state="full_text_observed")
        with self.assertRaisesRegex(ValueError, "need --evidence-ref"):
            apply_paper_progress(second, observed=["Results"])
        final, entry = apply_paper_progress(second, covered=["Results"], observed=["Results"], source_state="full_text_observed", evidence_ref="host-extract-9")
        self.assertEqual(final["reading"]["status"], "complete")
        self.assertEqual(final["reading"]["next"], "")
        self.assertEqual(final["card"]["source_state"]["state"], "full_text_observed")
        self.assertEqual(final["card"]["source_state"]["observed_sections"], ["Results"])
        self.assertEqual(final["card"]["source_state"]["evidence_ref"], "host-extract-9")
        self.assertEqual(entry["observed"], ["Results"])
        # Once the card carries an evidence_ref, a later observation may reuse it.
        again, _ = apply_paper_progress(final, observed=["Method"])
        # observed_sections follows ledger order, not call order.
        self.assertEqual(again["card"]["source_state"]["observed_sections"], ["Method", "Results"])

        with self.assertRaisesRegex(ValueError, "not a section"):
            apply_paper_progress(record, next_section="Conclusion")
        with self.assertRaisesRegex(ValueError, "covered or observed and missing"):
            apply_paper_progress(record, covered=["Abstract"], missing=["Abstract"])
        with self.assertRaisesRegex(ValueError, "nothing to record"):
            apply_paper_progress(record)
        with self.assertRaisesRegex(ValueError, "source_state must be one of"):
            apply_paper_progress(record, source_state="pdf_read")
        gone, _ = apply_paper_progress(record, missing=["Method"])
        with self.assertRaisesRegex(ValueError, "records as missing"):
            apply_paper_progress(gone, covered=["Method"])
        back, _ = apply_paper_progress(gone, covered=["Method"], observed=["Method"], evidence_ref="host-extract-10")
        self.assertEqual(back["reading"]["missing"], [])
        self.assertEqual(back["reading"]["covered"], ["Method"])
        with self.assertRaises(PaperLearningStoreError) as broken:
            apply_paper_progress({"paper_id": "x"}, covered=["Abstract"])
        self.assertEqual(broken.exception.reason_code, "record_corrupt")

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
            for bad_id in ("../outside", "missing-id"):
                with self.assertRaises(PaperLearningStoreError) as caught:
                    show_paper_learning_record(paths, bad_id)
                self.assertEqual(caught.exception.reason_code, "record_not_found")

            card_path = paths.paper_learning_dir / paper_id / "card.json"
            good = card_path.read_text(encoding="utf-8")
            broken = json.loads(good)
            broken["progress_count"] = 3
            card_path.write_text(json.dumps(broken), encoding="utf-8")
            result = validate_paper_learning_store(paths)
            self.assertFalse(result["ok"])
            self.assertIn("progress_count 3 but the ledger holds 1 entries", "\n".join(result["errors"]))

            # A shape-broken card is unreadable, not a blank row and not a traceback.
            card_path.write_text(json.dumps({"paper_id": paper_id}), encoding="utf-8")
            records, unreadable = scan_paper_learning_store(paths)
            self.assertEqual(records, [])
            self.assertEqual(len(unreadable), 1)
            self.assertEqual(unreadable[0]["paper_id"], paper_id)
            self.assertEqual(unreadable[0]["reason_code"], "record_corrupt")
            self.assertIn("card must be an object", unreadable[0]["detail"])
            with self.assertRaises(PaperLearningStoreError) as corrupt:
                show_paper_learning_record(paths, paper_id)
            self.assertEqual(corrupt.exception.reason_code, "record_corrupt")
            with self.assertRaises(PaperLearningStoreError) as corrupt_progress:
                record_paper_progress(paths, paper_id, covered=["Abstract"])
            self.assertEqual(corrupt_progress.exception.reason_code, "record_corrupt")
            result = validate_paper_learning_store(paths)
            self.assertFalse(result["ok"])
            self.assertEqual(result["unreadable_count"], 1)
            corrupt_lines = [error for error in result["errors"] if error.startswith(f"{paper_id}: record_corrupt: ")]
            self.assertEqual(len(corrupt_lines), 1, result["errors"])
            self.assertIn("card must be an object", corrupt_lines[0])

            # Unparsable JSON is corrupt too, and the ledger is untouched by the refused progress.
            card_path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(PaperLearningStoreError) as unparsable:
                show_paper_learning_record(paths, paper_id)
            self.assertEqual(unparsable.exception.reason_code, "record_corrupt")
            self.assertEqual(len(read_paper_progress_ledger(paths, paper_id)[0]), 1)
            card_path.write_text(good, encoding="utf-8")
            self.assertTrue(validate_paper_learning_store(paths)["ok"])

    def test_concurrent_progress_calls_serialize_under_one_lock(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            sections = [f"Section {index}" for index in range(1, 9)]
            record = write_paper_learning_record(paths, build_paper_learning_record(title="Concurrent paper", sections=sections))
            paper_id = record["paper_id"]
            failures: list[BaseException] = []

            def worker(section: str) -> None:
                try:
                    record_paper_progress(paths, paper_id, covered=[section])
                except BaseException as exc:  # noqa: BLE001 - the test reports every worker fault
                    failures.append(exc)

            threads = [threading.Thread(target=worker, args=(section,)) for section in sections]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)

            self.assertEqual(failures, [])
            stored = show_paper_learning_record(paths, paper_id)
            entries, errors = read_paper_progress_ledger(paths, paper_id)
            self.assertEqual(errors, [])
            self.assertEqual(stored["progress_count"], len(sections))
            self.assertEqual(len(entries), len(sections))
            self.assertEqual(sorted(entry["part_index"] for entry in entries), list(range(1, len(sections) + 1)))
            self.assertEqual(sorted(stored["reading"]["covered"]), sorted(sections))
            self.assertEqual(stored["reading"]["status"], "complete")
            self.assertTrue(validate_paper_learning_store(paths)["ok"], validate_paper_learning_store(paths)["errors"])

    def test_progress_appends_ledger_before_card_so_history_is_never_short(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            paper_id = write_paper_learning_record(paths, build_paper_learning_record(title="Order"))["paper_id"]

            # Ledger append fails: nothing written at all, card still at 0.
            with patch("omh.workflows.paper_learning.append_jsonl_locked", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    record_paper_progress(paths, paper_id, covered=["Abstract"])
            self.assertEqual(show_paper_learning_record(paths, paper_id)["progress_count"], 0)
            self.assertEqual(read_paper_progress_ledger(paths, paper_id)[0], [])
            self.assertTrue(validate_paper_learning_store(paths)["ok"])

            # Card write fails after the append: history is complete, the card is one behind, validate names it.
            with patch("omh.workflows.paper_learning.atomic_write_json", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    record_paper_progress(paths, paper_id, covered=["Abstract"])
            self.assertEqual(show_paper_learning_record(paths, paper_id)["progress_count"], 0)
            self.assertEqual(len(read_paper_progress_ledger(paths, paper_id)[0]), 1)
            result = validate_paper_learning_store(paths)
            self.assertFalse(result["ok"])
            self.assertIn(f"{paper_id}: progress_count 0 but the ledger holds 1 entries", result["errors"])

    def test_scan_skips_linked_record_directories(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            paper_id = write_paper_learning_record(paths, build_paper_learning_record(title="Linked"))["paper_id"]
            link = paths.paper_learning_dir / "linked-copy"
            try:
                link.symlink_to(paths.paper_learning_dir / paper_id, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks unavailable here: {exc}")
            records, unreadable = scan_paper_learning_store(paths)
            self.assertEqual([record["paper_id"] for record in records], [paper_id])
            self.assertEqual(unreadable, [])
            with self.assertRaises(PaperLearningStoreError) as refused:
                show_paper_learning_record(paths, "linked-copy")
            self.assertEqual(refused.exception.reason_code, "record_not_found")

    def test_same_instant_plans_list_in_recorded_order(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            frozen = 1_800_000_000_000_000_000
            with patch("omh.workflows.paper_learning.time.time_ns", return_value=frozen):
                first = write_paper_learning_record(paths, build_paper_learning_record(title="Same second", created_at="2026-09-14T02:30:49Z"))
                second = write_paper_learning_record(paths, build_paper_learning_record(title="Same second", created_at="2026-09-14T02:30:49Z"))
                third = write_paper_learning_record(paths, build_paper_learning_record(title="Same second", created_at="2026-09-14T02:30:49Z"))
            self.assertEqual(first["created_at_ns"], frozen)
            self.assertEqual(second["created_at_ns"], frozen + 1)
            self.assertEqual(third["created_at_ns"], frozen + 2)
            self.assertEqual(
                [record["paper_id"] for record in list_paper_learning_records(paths)],
                [first["paper_id"], second["paper_id"], third["paper_id"]],
            )
            self.assertEqual(list_paper_learning_records(paths, limit=1)[0]["paper_id"], third["paper_id"])
            self.assertEqual(show_paper_learning_record(paths, second["paper_id"])["created_at_ns"], frozen + 1)
            # A clock that moved backwards still cannot reorder the store.
            with patch("omh.workflows.paper_learning.time.time_ns", return_value=frozen - 5):
                fourth = write_paper_learning_record(paths, build_paper_learning_record(title="Same second"))
            self.assertEqual(fourth["created_at_ns"], frozen + 3)
            self.assertEqual(list_paper_learning_records(paths)[-1]["paper_id"], fourth["paper_id"])
            self.assertTrue(validate_paper_learning_store(paths)["ok"])

    def test_store_is_empty_when_directory_is_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = OmhPaths(omh_home=Path(tmp) / ".omh", hermes_home=Path(tmp) / ".hermes")
            self.assertEqual(list_paper_learning_records(paths), [])
            result = validate_paper_learning_store(paths)
            self.assertTrue(result["ok"])
            self.assertEqual(result["paper_count"], 0)


if __name__ == "__main__":
    unittest.main()

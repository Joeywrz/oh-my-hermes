"""`omh paper`: the durable store behind the paper-learning skill's chunk ledger."""

from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _cli_harness import run_cli


def _plan(base: list[str], *extra: str) -> dict:
    status, stdout, stderr = run_cli(base + ["plan", "--title", "Attention Is All You Need", *extra])
    assert status == 0, stderr
    return json.loads(stdout)


class PaperLearningCliTests(unittest.TestCase):
    def test_plan_records_card_hashes_local_source_and_keeps_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "paper.pdf"
            source.write_bytes(b"%PDF-1.7 fake bytes")
            base = ["--omh-home", str(root / ".omh"), "paper"]

            payload = _plan(base, "--source", str(source), "--level", "very easy", "--author", "Vaswani et al.")

            self.assertEqual(payload["schema_version"], "omh_paper_learning_plan_result/v1")
            record = payload["record"]
            self.assertEqual(record["schema_version"], "omh_paper_learning_record/v1")
            card = record["card"]
            self.assertEqual(card["schema_version"], "paper_learning_card/v1")
            self.assertEqual(card["level"], "very_easy")
            self.assertEqual(card["paper_identity"]["authors"], ["Vaswani et al."])
            self.assertEqual(card["source_state"]["state"], "metadata_only")
            self.assertEqual(len(card["coverage_ledger"]), 10)
            self.assertEqual(record["source"]["kind"], "file")
            self.assertEqual(record["source"]["path"], str(source.resolve()))
            self.assertEqual(record["source"]["size_bytes"], len(b"%PDF-1.7 fake bytes"))
            self.assertRegex(record["source"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(record["reading"]["status"], "not_started")
            self.assertEqual(record["reading"]["next"], "Abstract")
            self.assertEqual(record["progress_count"], 0)
            self.assertIn("full_pdf_extraction", card["not_observed"])
            boundary = payload["boundary"]
            self.assertTrue(boundary["prepared_is_not_observed"])
            self.assertTrue(boundary["source_hash_is_not_extraction_evidence"])
            self.assertFalse(boundary["page_count_observed"])
            store = payload["store"]
            self.assertEqual(store["index_authority"], "cache_only")
            card_path = Path(store["card_path"])
            self.assertTrue(card_path.is_file())
            self.assertEqual(card_path.parent.name, record["paper_id"])
            self.assertEqual(card_path.parent.parent.resolve(), (root / ".omh" / "paper-learning").resolve())
            self.assertFalse(Path(store["ledger_path"]).exists())
            self.assertTrue(Path(store["index_path"]).is_file())

    def test_plan_accepts_url_reference_custom_sections_and_prints_text(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = ["--omh-home", str(root / ".omh"), "paper"]

            url = _plan(base, "--source", "https://arxiv.org/abs/1706.03762")
            self.assertEqual(url["record"]["source"]["kind"], "url")
            self.assertEqual(url["record"]["source"]["sha256"], "")

            reference = _plan(
                base,
                "--source",
                "arxiv:1706.03762",
                "--section",
                "Abstract",
                "--section",
                "Method",
                "--section",
                "Appendix B",
                "--source-state",
                "excerpt_text_observed",
                "--observed-section",
                "Abstract",
                "--evidence-ref",
                "chat-paste-1",
            )
            record = reference["record"]
            self.assertEqual(record["source"]["kind"], "reference")
            ledger = record["card"]["coverage_ledger"]
            self.assertEqual([item["paper_section"] for item in ledger], ["Abstract", "Method", "Appendix B"])
            self.assertEqual(ledger[0]["status"], "observed")
            self.assertEqual(ledger[0]["explanation_status"], "pending")
            self.assertEqual(record["card"]["source_state"]["evidence_ref"], "chat-paste-1")
            self.assertEqual(record["reading"]["section_count"], 3)

            status, stdout, stderr = run_cli(base + ["plan", "--title", "Text paper", "--source", ""], output_json=False)
            self.assertEqual(status, 0, stderr)
            self.assertIn("Paper: Text paper", stdout)
            self.assertIn("source: not supplied (none)", stdout)
            self.assertIn("reading: not_started (0/10 sections explained, 0 chunk(s) recorded)", stdout)
            self.assertIn("next: Abstract", stdout)
            self.assertIn("Card: ", stdout)
            self.assertIn("Not observed: full_pdf_extraction", stdout)
            self.assertNotIn("{", stdout.splitlines()[0])

    def test_progress_moves_ledger_appends_entry_and_show_resumes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = ["--omh-home", str(root / ".omh"), "paper"]
            paper_id = _plan(base, "--source", "arxiv:1706.03762", "--level", "expert")["record"]["paper_id"]

            status, stdout, stderr = run_cli(
                base
                + [
                    "progress",
                    paper_id,
                    "--covered",
                    "abstract",
                    "--covered",
                    "Introduction",
                    "--missing",
                    "Figures, tables, and equations",
                    "--note",
                    "Stopped before the method section.",
                    "--source-state",
                    "file_text_extraction_observed",
                    "--evidence-ref",
                    "host-extract-1",
                ]
            )
            self.assertEqual(status, 0, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["schema_version"], "omh_paper_learning_progress_result/v1")
            entry = payload["entry"]
            self.assertEqual(entry["schema_version"], "omh_paper_learning_progress/v1")
            self.assertEqual(entry["part_index"], 1)
            self.assertEqual(entry["covered"], ["Abstract", "Introduction"])
            self.assertEqual(entry["missing"], ["Figures, tables, and equations"])
            self.assertEqual(entry["next"], "Related work / prior context")
            self.assertEqual(entry["note"], "Stopped before the method section.")
            self.assertEqual(entry["source_state"], "file_text_extraction_observed")
            record = payload["record"]
            self.assertEqual(record["progress_count"], 1)
            self.assertEqual(record["reading"]["status"], "in_progress")
            self.assertEqual(record["reading"]["covered"], ["Abstract", "Introduction"])
            self.assertEqual(record["reading"]["missing"], ["Figures, tables, and equations"])
            self.assertEqual(record["card"]["chunking_policy"]["part_index"], 2)
            self.assertEqual(record["card"]["source_state"]["observed_sections"], ["Abstract", "Introduction"])
            self.assertEqual(record["card"]["source_state"]["missing_sections"], ["Figures, tables, and equations"])
            self.assertEqual(record["card"]["source_state"]["evidence_ref"], "host-extract-1")

            status, stdout, stderr = run_cli(base + ["progress", paper_id, "--covered", "Related work / prior context", "--next", "Results"])
            self.assertEqual(status, 0, stderr)
            self.assertEqual(json.loads(stdout)["record"]["reading"]["next"], "Results")

            ledger_lines = (root / ".omh" / "paper-learning" / paper_id / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(ledger_lines), 2)
            self.assertEqual(json.loads(ledger_lines[1])["part_index"], 2)

            status, stdout, stderr = run_cli(base + ["show", paper_id])
            self.assertEqual(status, 0, stderr)
            shown = json.loads(stdout)
            self.assertEqual(shown["schema_version"], "omh_paper_learning_show/v1")
            self.assertEqual(len(shown["ledger"]), 2)
            self.assertEqual(shown["ledger_errors"], [])
            self.assertEqual(shown["record"]["reading"]["next"], "Results")
            self.assertEqual(shown["record"]["reading"]["explained_count"], 3)

            status, stdout, stderr = run_cli(base + ["show", paper_id], output_json=False)
            self.assertEqual(status, 0, stderr)
            self.assertIn("reading: in_progress (3/10 sections explained, 2 chunk(s) recorded)", stdout)
            self.assertIn("covered: Abstract, Introduction, Related work / prior context", stdout)
            self.assertIn("next: Results", stdout)
            self.assertIn("missing: Figures, tables, and equations", stdout)
            self.assertIn("last note: Stopped before the method section.", stdout)
            self.assertIn("  - Abstract: observed / explained", stdout)
            self.assertIn("  - Figures, tables, and equations: missing / pending", stdout)
            self.assertIn("part 1: covered Abstract, Introduction; next Related work / prior context; missing Figures, tables, and equations; note: Stopped before the method section.", stdout)
            self.assertIn("part 2: covered Related work / prior context; next Results", stdout)
            self.assertIn("not proof the explanation was correct or complete", stdout)

    def test_progress_completes_when_every_section_is_explained(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = ["--omh-home", str(root / ".omh"), "paper"]
            paper_id = _plan(base, "--section", "Abstract", "--section", "Method")["record"]["paper_id"]

            status, stdout, stderr = run_cli(base + ["progress", paper_id, "--covered", "Abstract"])
            self.assertEqual(status, 0, stderr)
            self.assertEqual(json.loads(stdout)["record"]["reading"]["status"], "in_progress")

            status, stdout, stderr = run_cli(base + ["progress", paper_id, "--covered", "Method"])
            self.assertEqual(status, 0, stderr)
            reading = json.loads(stdout)["record"]["reading"]
            self.assertEqual(reading["status"], "complete")
            self.assertEqual(reading["next"], "")
            self.assertEqual(reading["explained_count"], 2)

            status, stdout, stderr = run_cli(base + ["list"], output_json=False)
            self.assertEqual(status, 0, stderr)
            self.assertIn("[complete, 2/2 explained, no next section]", stdout)

    def test_progress_waits_when_only_missing_sections_remain(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = ["--omh-home", str(root / ".omh"), "paper"]
            paper_id = _plan(base, "--section", "Abstract", "--section", "Appendix")["record"]["paper_id"]

            status, stdout, stderr = run_cli(base + ["progress", paper_id, "--covered", "Abstract", "--missing", "Appendix"])
            self.assertEqual(status, 0, stderr)
            reading = json.loads(stdout)["record"]["reading"]
            self.assertEqual(reading["status"], "waiting_for_missing_sections")
            self.assertEqual(reading["next"], "")
            self.assertEqual(reading["missing"], ["Appendix"])

            status, stdout, stderr = run_cli(base + ["progress", paper_id, "--covered", "Appendix", "--source-state", "full_text_observed"])
            self.assertEqual(status, 0, stderr)
            record = json.loads(stdout)["record"]
            self.assertEqual(record["reading"]["status"], "complete")
            self.assertEqual(record["card"]["source_state"]["state"], "full_text_observed")
            self.assertEqual(record["card"]["source_state"]["missing_sections"], [])

    def test_progress_refuses_bad_input_without_writing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = ["--omh-home", str(root / ".omh"), "paper"]
            paper_id = _plan(base)["record"]["paper_id"]
            ledger_path = root / ".omh" / "paper-learning" / paper_id / "ledger.jsonl"

            status, _, stderr = run_cli(base + ["progress", paper_id, "--covered", "Appendix Z"])
            self.assertEqual(status, 2)
            self.assertIn("--covered 'Appendix Z' is not a section of this paper's coverage ledger", stderr)
            self.assertIn("known sections: Abstract, Introduction", stderr)

            status, _, stderr = run_cli(base + ["progress", paper_id, "--covered", "Abstract", "--missing", "abstract"])
            self.assertEqual(status, 2)
            self.assertIn("cannot be both covered and missing", stderr)

            status, _, stderr = run_cli(base + ["progress", paper_id, "--note", "x" * 501])
            self.assertEqual(status, 2)
            self.assertIn("--note must be at most 500 characters", stderr)

            status, _, stderr = run_cli(base + ["progress", paper_id])
            self.assertEqual(status, 2)
            self.assertIn("nothing to record", stderr)

            status, _, stderr = run_cli(base + ["progress", "no-such-paper", "--covered", "Abstract"])
            self.assertEqual(status, 2)
            self.assertIn("paper learning record not found: no-such-paper", stderr)

            status, _, stderr = run_cli(base + ["show", "../escape"])
            self.assertEqual(status, 2)
            self.assertIn("paper learning record not found", stderr)

            self.assertFalse(ledger_path.exists())
            status, stdout, stderr = run_cli(base + ["show", paper_id])
            self.assertEqual(status, 0, stderr)
            self.assertEqual(json.loads(stdout)["record"]["progress_count"], 0)

    def test_list_orders_limits_and_honors_json_flag_without_env(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = ["--omh-home", str(root / ".omh"), "paper"]

            status, stdout, stderr = run_cli(base + ["list"], output_json=False)
            self.assertEqual(status, 0, stderr)
            self.assertIn("No paper learning records yet", stdout)
            self.assertIn("omh paper plan --title", stdout)

            first = _plan(base, "--source", "arxiv:1")["record"]["paper_id"]
            second = _plan(base, "--source", "arxiv:2")["record"]["paper_id"]

            status, stdout, stderr = run_cli(base + ["list"])
            self.assertEqual(status, 0, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["schema_version"], "omh_paper_learning_list/v1")
            self.assertEqual(payload["count"], 2)
            self.assertEqual(payload["total_count"], 2)
            self.assertTrue(payload["summary_only"])
            self.assertEqual({row["paper_id"] for row in payload["papers"]}, {first, second})
            self.assertEqual(set(payload["papers"][0]), {
                "paper_id", "title", "level", "source_state", "reading_status", "explained_count",
                "section_count", "next", "missing_count", "progress_count", "updated_at",
            })

            status, stdout, stderr = run_cli(base + ["list", "--limit", "1", "--json"], output_json=False)
            self.assertEqual(status, 0, stderr)
            limited = json.loads(stdout)
            self.assertEqual(limited["count"], 1)
            self.assertTrue(limited["truncated"])
            self.assertEqual(limited["limit"], 1)

            status, stdout, stderr = run_cli(base + ["list", "--all"])
            self.assertEqual(status, 0, stderr)
            self.assertEqual(json.loads(stdout)["limit"], "all")

            status, _, stderr = run_cli(base + ["list", "--limit", "0"])
            self.assertEqual(status, 2)
            self.assertIn("--limit must be at least 1", stderr)

            status, stdout, stderr = run_cli(base + ["list"], output_json=False)
            self.assertEqual(status, 0, stderr)
            self.assertIn("Paper learning records:", stdout)
            self.assertIn(f"  {first}  Attention Is All You Need  [not_started, 0/10 explained, next Abstract]", stdout)

    def test_validate_reports_ok_then_names_each_store_fault(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = ["--omh-home", str(root / ".omh"), "paper"]
            store = root / ".omh" / "paper-learning"

            status, stdout, stderr = run_cli(base + ["validate"], output_json=False)
            self.assertEqual(status, 0, stderr)
            self.assertEqual(stdout.strip(), "paper-learning store ok: 0 record(s), 0 errors")

            paper_id = _plan(base)["record"]["paper_id"]
            status, stdout, stderr = run_cli(base + ["progress", paper_id, "--covered", "Abstract"])
            self.assertEqual(status, 0, stderr)

            status, stdout, stderr = run_cli(base + ["validate"])
            self.assertEqual(status, 0, stderr)
            result = json.loads(stdout)
            self.assertEqual(result["schema_version"], "omh_paper_learning_validation/v1")
            self.assertTrue(result["ok"])
            self.assertEqual(result["paper_count"], 1)
            self.assertEqual(result["index_authority"], "cache_only")

            ledger_path = store / paper_id / "ledger.jsonl"
            with ledger_path.open("a", encoding="utf-8") as handle:
                handle.write("not json\n")
            renamed = store / "renamed-dir"
            (store / paper_id).rename(renamed)
            (store / "empty-dir").mkdir()

            status, stdout, stderr = run_cli(base + ["validate"])
            self.assertEqual(status, 1, stderr)
            result = json.loads(stdout)
            self.assertFalse(result["ok"])
            joined = "\n".join(result["errors"])
            self.assertIn("does not match its directory", joined)
            self.assertIn("ledger.jsonl:2", joined)
            self.assertIn("empty-dir: missing card.json", joined)

            status, stdout, stderr = run_cli(base + ["validate"], output_json=False)
            self.assertEqual(status, 1, stderr)
            self.assertIn("paper-learning store has", stdout)
            self.assertIn("  - ", stdout)

    def test_paper_help_names_every_subcommand(self) -> None:
        from omh.commands.main import build_parser

        stdout_buffer = io.StringIO()
        with patch("sys.stdout", stdout_buffer), self.assertRaises(SystemExit) as exit_context:
            build_parser().parse_args(["paper", "--help"])

        self.assertEqual(exit_context.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        for name in ("plan", "list", "show", "progress", "validate"):
            self.assertIn(name, stdout)
        self.assertIn("chunk ledger", " ".join(stdout.split()))


if __name__ == "__main__":
    unittest.main()

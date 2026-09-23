"""The reply lint: the QA evidence for OMH's reply rules.

The rules ship as prompt text (skill tails, the rail's Reply Language And Host
Voice and Turn Ending sections, the awareness primers). These tests pin what
the lint reports on the replies the owner actually read, the negatives that
keep everyday words out of it, the carve-out for a user who names a term,
and the read-only Hermes session source.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli
from omh.quality.reply_lint import (
    FINDING_KINDS,
    REPLY_LINT_SCHEMA_VERSION,
    build_reply_lint,
    closing_has_question,
    closing_paragraph,
    format_reply_lint_summary,
    record_term_vocabulary,
    summarize_reply_lints,
)
from omh.quality.reply_lint_source import ReplySourceError, hermes_session_replies


# The reply the owner pasted on 2026-09-23: a stop declared as a refusal.
OWNER_REFUSAL_REPLY = (
    "재발 방지 기준을 정리했습니다.\n\n"
    "나는 여기서 추가 수정·force-push·merge를 하지 않을게. (2/2)"
)

# The same stop, ended the way the Turn Ending rule asks.
OWNER_REFUSAL_REPLY_REPAIRED = (
    "재발 방지 기준을 정리했습니다.\n\n"
    "추가 수정과 force-push, merge는 아직 안 했습니다. 지금 revert PR을 열까요, 아니면 기여자 수정을 기다릴까요?"
)

CLEAN_ENGLISH_REPLY = (
    "Merged #1829 and closed the issue. The revert PR is prepared, not run yet; "
    "the child report is not checked.\n\n"
    "Shall I open the revert PR, or wait for the contributor's fix?"
)


def _kinds(payload: dict) -> list[tuple[str, str]]:
    return [(item["kind"], item["match"]) for item in payload["findings"]]


class ClosingTests(unittest.TestCase):
    def test_owner_refusal_reply_is_a_refusal_closer_without_a_question(self) -> None:
        payload = build_reply_lint(OWNER_REFUSAL_REPLY)

        self.assertFalse(payload["ok"])
        self.assertEqual(_kinds(payload), [("refusal_closer", "하지 않을게")])
        self.assertEqual(payload["findings"][0]["line"], 3)
        self.assertFalse(payload["closing"]["has_question"])
        self.assertEqual(payload["closing"]["kind"], "refusal_closer")

    def test_the_repaired_owner_reply_is_clean(self) -> None:
        payload = build_reply_lint(OWNER_REFUSAL_REPLY_REPAIRED)

        self.assertTrue(payload["ok"], payload["findings"])
        self.assertTrue(payload["closing"]["has_question"])

    def test_english_refusal_closer(self) -> None:
        payload = build_reply_lint("The revert PR is ready.\n\nI will not merge or force-push here.")

        self.assertEqual(_kinds(payload), [("refusal_closer", "I will not")])

    def test_a_refusal_followed_by_the_question_is_not_a_closer(self) -> None:
        payload = build_reply_lint(
            "I won't merge this myself.\n\nShall I open the revert PR, or wait for the contributor's fix?"
        )

        self.assertTrue(payload["ok"], payload["findings"])

    def test_a_refusal_earlier_in_the_reply_is_not_the_closing(self) -> None:
        # Only the closing paragraph decides; a refusal in the body followed by a
        # closing question is the rule's own shape ("stated as the option it leaves open").
        payload = build_reply_lint(
            "I will not force-push over the contributor's branch.\n\nDo you want the revert PR opened now?"
        )

        self.assertTrue(payload["ok"], payload["findings"])

    def test_live_korean_closers_observed_in_the_owner_sessions(self) -> None:
        # Shapes read out of ~/.hermes/state.db on 2026-09-23, trimmed.
        for reply, match in (
            (
                "이번 알림은 실제 종료를 확인해 주지 못했습니다.\n\n"
                "실제 종료를 확인하기 전까지 복구 완료로 처리하지 않겠습니다.",
                "하지 않겠",
            ),
            (
                "정리:\n\n- 새 조사나 통합 작업은 시작하지 않음\n- 현재 실행 중 subagent: 없음\n- 설정에는 영향 없음",
                "하지 않음",
            ),
        ):
            with self.subTest(match=match):
                self.assertEqual(_kinds(build_reply_lint(reply)), [("refusal_closer", match)])

    def test_a_decision_left_without_a_question(self) -> None:
        payload = build_reply_lint("Both options are viable.\n\nThis needs your approval before I continue.")

        self.assertEqual(_kinds(payload), [("decision_without_question", "needs your approval")])
        self.assertEqual(payload["closing"]["kind"], "decision_without_question")

    def test_a_korean_question_ending_counts_without_a_question_mark(self) -> None:
        self.assertTrue(closing_has_question("어느 쪽으로 진행할까요"))
        self.assertTrue(closing_has_question("revert PR을 열까요?"))
        self.assertFalse(closing_has_question("여기서 멈추겠습니다."))

    def test_closing_paragraph_is_the_last_block_and_a_list_stays_one_block(self) -> None:
        text = "first\n\nsecond\n\n- a\n- b\n- c"
        self.assertEqual(closing_paragraph(text), "- a\n- b\n- c")
        self.assertEqual(closing_paragraph("   "), "")


class RecordTermTests(unittest.TestCase):
    def test_record_terms_in_an_english_reply(self) -> None:
        payload = build_reply_lint(
            "This is an evidence-bounded surface; the child report is prepared_not_observed, so the handoff waits."
        )

        self.assertEqual(
            _kinds(payload),
            [
                ("record_term_leak", "evidence-bounded"),
                ("record_term_leak", "prepared_not_observed"),
                ("record_term_leak", "handoff"),
            ],
        )

    def test_the_longest_term_wins_so_one_token_is_one_finding(self) -> None:
        payload = build_reply_lint("status: prepared_not_observed")

        self.assertEqual(_kinds(payload), [("record_term_leak", "prepared_not_observed")])
        self.assertEqual(payload["counts"]["record_term_leak"], 1)

    def test_korean_renderings_read_in_live_replies(self) -> None:
        payload = build_reply_lint("한 파일이 두 표면을 서빙합니다. 레인을 열었습니다.")

        self.assertEqual(_kinds(payload), [("record_term_leak", "표면"), ("record_term_leak", "레인")])

    def test_everyday_words_that_contain_a_term_do_not_match(self) -> None:
        for text in (
            "표면적으로는 같아 보입니다.",  # superficially
            "브레인스토밍을 먼저 했습니다.",  # brain
            "The function surfaces the error to the caller.",
            "Take the left lane after the bridge.",
            "the wrapper function returns early",
            "the run was not observed by anyone",  # plain words, not the token
        ):
            with self.subTest(text=text):
                self.assertEqual(build_reply_lint(text)["findings"], [])

    def test_the_plain_substitutes_from_the_rail_are_clean(self) -> None:
        payload = build_reply_lint(CLEAN_ENGLISH_REPLY)

        self.assertTrue(payload["ok"], payload["findings"])
        self.assertEqual(payload["finding_count"], 0)

    def test_a_term_the_user_named_is_carved_out_not_counted(self) -> None:
        payload = build_reply_lint(
            "prepared_not_observed means the handoff was prepared and not run yet.",
            user_text="what does prepared_not_observed mean here?",
        )

        self.assertEqual(payload["carved_out_terms"], ["prepared_not_observed"])
        # The user did not name `handoff`, so that one still counts.
        self.assertEqual(_kinds(payload), [("record_term_leak", "handoff")])

    def test_the_vocabulary_is_the_rail_list_plus_the_observed_korean_forms(self) -> None:
        vocabulary = record_term_vocabulary()
        for term in ("prepared_not_observed", "not_observed", "evidence boundary", "handoff", "표면", "레인"):
            self.assertIn(term, vocabulary)
        self.assertNotIn("surface", vocabulary)
        self.assertNotIn("lane", vocabulary)
        self.assertNotIn("wrapper", vocabulary)


class AwarenessLineTests(unittest.TestCase):
    def test_quoted_awareness_and_boundary_lines(self) -> None:
        payload = build_reply_lint(
            "[OMH Awareness] plan-first lane is active.\nBoundary: this shows X and not Y.\n\n다음으로 넘어갈까요?"
        )

        self.assertEqual(
            _kinds(payload),
            [("awareness_line_quoted", "[OMH Awareness]"), ("awareness_line_quoted", "Boundary:")],
        )
        self.assertEqual([item["line"] for item in payload["findings"]], [1, 2])

    def test_the_word_boundary_mid_sentence_is_not_a_quoted_line(self) -> None:
        self.assertEqual(build_reply_lint("The boundary: nothing here was merged.")["findings"], [])


class PayloadTests(unittest.TestCase):
    def test_payload_shape_and_determinism(self) -> None:
        first = build_reply_lint(OWNER_REFUSAL_REPLY)
        second = build_reply_lint(OWNER_REFUSAL_REPLY)

        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], REPLY_LINT_SCHEMA_VERSION)
        self.assertEqual(set(first["counts"]), set(FINDING_KINDS))
        self.assertIn("not execution, review, CI, or merge evidence", first["claim_boundary"])
        for finding in first["findings"]:
            self.assertEqual(set(finding), {"kind", "match", "line", "column", "excerpt"})

    def test_summary_folds_counts_and_keeps_the_boundary(self) -> None:
        payload = summarize_reply_lints(
            [build_reply_lint(OWNER_REFUSAL_REPLY), build_reply_lint(CLEAN_ENGLISH_REPLY)],
            source={"kind": "text_file"},
        )

        self.assertEqual(payload["reply_count"], 2)
        self.assertEqual(payload["finding_count"], 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["counts"]["refusal_closer"], 1)
        self.assertEqual(payload["source"], {"kind": "text_file"})
        text = format_reply_lint_summary(payload)
        self.assertIn("1 finding across 2 replies", text)
        self.assertIn("refusal_closer `하지 않을게`", text)
        self.assertIn("Reply 2: clean", text)
        self.assertIn("Boundary", text)

    def test_an_empty_batch_is_clean_and_says_so(self) -> None:
        payload = summarize_reply_lints([])

        self.assertTrue(payload["ok"])
        self.assertIn("clean across 0 replies", format_reply_lint_summary(payload))


def _write_state_db(home: Path, *, session_id: str = "20260923_150513_4286a5") -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "state.db"
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL, last_activity_at REAL)")
        connection.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
        )
        connection.execute("INSERT INTO sessions VALUES (?, 1.0, 5.0)", (session_id,))
        connection.execute("INSERT INTO sessions VALUES ('older', 0.5, 2.0)")
        rows = [
            (1, session_id, "user", "what does prepared_not_observed mean?"),
            (2, session_id, "assistant", "prepared_not_observed means prepared, not run yet."),
            (3, session_id, "tool", '{"ok": true}'),
            (4, session_id, "user", "merge it"),
            (5, session_id, "assistant", "[PRIOR CONTEXT — for reference only] old summary"),
            (6, session_id, "assistant", OWNER_REFUSAL_REPLY),
            (7, session_id, "assistant", OWNER_REFUSAL_REPLY),  # compaction re-record
            (8, "older", "assistant", "unrelated"),
        ]
        connection.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, 0.0)", rows)
    connection.close()
    return path


class HermesSessionSourceTests(unittest.TestCase):
    def test_replies_pair_with_the_user_message_and_skip_prior_context_and_duplicates(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_state_db(home)

            read = hermes_session_replies(home, "20260923_150513_4286a5", last=5)

        self.assertEqual(read["session_id"], "20260923_150513_4286a5")
        self.assertEqual(
            [(item["message_id"], item["user_text"]) for item in read["replies"]],
            [(2, "what does prepared_not_observed mean?"), (6, "merge it")],
        )

    def test_latest_resolves_to_the_most_recently_active_session_and_last_bounds_the_tail(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_state_db(home)

            read = hermes_session_replies(home, "latest", last=1)

        self.assertEqual(read["session_id"], "20260923_150513_4286a5")
        self.assertEqual([item["message_id"] for item in read["replies"]], [6])

    def test_missing_database_unknown_session_and_bad_last_are_named_errors(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            with self.assertRaisesRegex(ReplySourceError, "no Hermes state database"):
                hermes_session_replies(home, "latest")
            _write_state_db(home)
            with self.assertRaisesRegex(ReplySourceError, "has no user or assistant messages"):
                hermes_session_replies(home, "nope")
            with self.assertRaisesRegex(ReplySourceError, "--last"):
                hermes_session_replies(home, "latest", last=0)

    def test_the_database_is_not_written(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            path = _write_state_db(home)
            before = path.read_bytes()

            hermes_session_replies(home, "latest", last=3)

            self.assertEqual(path.read_bytes(), before)


class ReplyLintCliTests(unittest.TestCase):
    def test_text_file_json_payload_and_a_finding_exits_one(self) -> None:
        with TemporaryDirectory() as tmp:
            reply = Path(tmp) / "reply.txt"
            reply.write_text(OWNER_REFUSAL_REPLY, encoding="utf-8")

            status, stdout, stderr = run_cli(["quality-evidence", "reply-lint", "--text-file", str(reply)])

        self.assertEqual(status, 1, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["schema_version"], REPLY_LINT_SCHEMA_VERSION)
        self.assertEqual(payload["source"], {"kind": "text_file", "path": str(reply)})
        self.assertEqual(payload["counts"]["refusal_closer"], 1)
        self.assertFalse(payload["ok"])

    def test_stdin_with_the_user_text_carve_out_exits_zero_when_clean(self) -> None:
        with TemporaryDirectory() as tmp:
            asked = Path(tmp) / "user.txt"
            asked.write_text("what does prepared_not_observed mean?", encoding="utf-8")

            status, stdout, stderr = run_cli(
                ["quality-evidence", "reply-lint", "--stdin", "--user-text-file", str(asked)],
                stdin_text="prepared_not_observed means prepared, not run yet. Shall I run it?",
            )

        self.assertEqual(status, 0, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["replies"][0]["carved_out_terms"], ["prepared_not_observed"])

    def test_hermes_session_source_is_read_only_and_plain_text_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            path = _write_state_db(home)
            before = path.read_bytes()

            status, stdout, stderr = run_cli(
                [
                    "--omh-home", str(Path(tmp) / ".omh"), "--hermes-home", str(home),
                    "quality-evidence", "reply-lint", "--hermes-session", "latest", "--last", "2",
                ],
                output_json=False,
            )

            self.assertEqual(path.read_bytes(), before)

        self.assertEqual(status, 1, stderr)
        self.assertFalse(stdout.lstrip().startswith("{"))
        self.assertIn("OMH reply lint: 1 finding across 2 replies", stdout)
        self.assertIn("named by the user, not counted: prepared_not_observed", stdout)
        self.assertIn("not execution, review, CI, or merge evidence", stdout)

    def test_a_missing_database_is_an_error_not_a_clean_result(self) -> None:
        with TemporaryDirectory() as tmp:
            status, stdout, stderr = run_cli(
                [
                    "--omh-home", str(Path(tmp) / ".omh"), "--hermes-home", str(Path(tmp) / ".hermes"),
                    "quality-evidence", "reply-lint", "--hermes-session", "latest",
                ]
            )

        self.assertNotEqual(status, 0)
        self.assertIn("no Hermes state database", stderr)
        self.assertEqual(stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()

"""Contracts for the repeated-tool-call circuit breaker at pre_tool_call.

The motivating session, measured: 20260919_140745_db409e, 409 tool calls,
203 of them `search_files`. The host's own guard refused 185 and the model
issued the identical call again every time; the same session then repeated
one `read_file` region 100+ times against the host's second guard. So the
loop is tool-agnostic, and a refusal message is something this model
demonstrably ignores.

Hence two stages, and these tests pin both: a `block` whose message the
model may ignore, and then an `approve`, which the host routes to the
human-approval gate instead of back to the model. Everything outside the
narrow positive case -- same session, same tool, same argument digest,
consecutive, at a stage -- must allow. #1674 is what an over-broad
pre_tool_call veto costs, and an escalation in an unattended session fails
closed, so the allow side is pinned harder than the intervene side.
"""

import json
import tempfile
import unittest
from pathlib import Path

from omh.plugin_bundle.omh.hooks.tool_hooks import post_tool_call, pre_tool_call
from omh.plugin_bundle.omh.tool_bursts import (
    MAX_DIGEST_INPUT_BYTES,
    REPEAT_CALL_APPROVAL_THRESHOLD,
    REPEAT_CALL_BLOCK_THRESHOLD,
    REPEAT_STREAK_WINDOW_SECONDS,
    record_repeat_refusal,
    record_tool_call,
    repeat_call_directive,
    repeat_call_streak,
    tool_args_digest,
    tool_bursts_path,
)

# The arguments from the observed burn, used verbatim so the fixture keeps
# naming the failure it came from.
BURN_PATTERN = "def render_skill|def render_all|def write_skill|def sync_skills|def generate"
BURN_ARGS = {"pattern": BURN_PATTERN, "path": "src"}
NOW = 1_787_040_000.0


class RepeatCallBreakerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def _call(self, tool="search_files", args=None, session="session-a"):
        return pre_tool_call(
            tool_name=tool,
            tool_input=BURN_ARGS if args is None else args,
            session_id=session,
            omh_home=str(self.home),
        )

    def _run_to_threshold(self, **kwargs):
        for index in range(REPEAT_CALL_BLOCK_THRESHOLD):
            self.assertIsNone(self._call(**kwargs), f"call {index + 1} must proceed")

    def _run_to_escalation(self, **kwargs):
        """Leave the streak one call short of the approval stage.

        Every refusal in between must be a block: ignoring a block is what
        earns the escalation, so a test that skipped them would be
        checking a stage nothing had reached.
        """
        self._run_to_threshold(**kwargs)
        for index in range(REPEAT_CALL_APPROVAL_THRESHOLD - REPEAT_CALL_BLOCK_THRESHOLD):
            directive = self._call(**kwargs)
            self.assertEqual(directive["action"], "block", f"refusal {index + 1} is stage one")

    def _ledger(self):
        return json.loads(tool_bursts_path(str(self.home)).read_text(encoding="utf-8"))

    def test_the_call_after_the_threshold_is_refused_and_names_the_alternative(self):
        self._run_to_threshold()

        blocked = self._call()

        self.assertEqual(blocked["action"], "block")
        message = str(blocked["message"])
        # The count is what actually ran: an intercepted call never
        # dispatches, so it is counted apart from the calls that did.
        self.assertIn(f"issued {REPEAT_CALL_BLOCK_THRESHOLD} times in a row", message)
        self.assertIn("search_files", message)
        # The observed failure was a model repeating a search instead of
        # switching tactic, so the refusal has to say what to do instead --
        # both ways, because the guard cannot tell a loop from a poll.
        self.assertIn("read the file directly", message.lower())
        self.assertIn("list what it does define", message)
        self.assertIn("do other work between checks", message)
        self.assertIn("A blocked call did not run", message)
        # Two sentences this message must never contain. The host's
        # refusal said "You already have this information", false in the
        # measured case because the pattern named functions that do not
        # exist. And OMH digests ARGUMENTS, never results, so any claim
        # about what came back is unmeasured -- and wrong for a poll.
        self.assertNotIn("already have this information", message)
        self.assertNotIn("same result", message)
        self.assertNotIn("cannot return anything different", message)
        self.assertIn("compares arguments, not results", message)
        # Still refused on the next attempt, and still reporting the number
        # of calls that reached the tool rather than the number attempted.
        again = self._call()
        self.assertEqual(again["action"], "block")
        self.assertIn(f"issued {REPEAT_CALL_BLOCK_THRESHOLD} times in a row", str(again["message"]))

    def test_the_second_stage_escalates_to_the_human_approval_gate(self):
        self._run_to_escalation()

        escalated = self._call()

        self.assertEqual(escalated["action"], "approve")
        self.assertEqual(
            escalated["rule_key"],
            f"omh_repeat:search_files:{tool_args_digest(BURN_ARGS)}",
        )
        message = str(escalated["message"])
        self.assertTrue(message)
        # Written for a person: which tool, how far it has gone, how many
        # times OMH already intervened, and what each answer does.
        self.assertIn("search_files", message)
        refusals = REPEAT_CALL_APPROVAL_THRESHOLD - REPEAT_CALL_BLOCK_THRESHOLD
        self.assertIn(f"{REPEAT_CALL_APPROVAL_THRESHOLD} times in a row", message)
        self.assertIn(f"first {REPEAT_CALL_BLOCK_THRESHOLD} reached the tool", message)
        self.assertIn(f"refused or escalated the {refusals}", message)
        self.assertIn("Denying ends the repetition", message)
        # The person deciding whether to allow a poll is exactly who must
        # not be told the result is unchanged, which OMH never measured.
        self.assertNotIn("same result", message)
        self.assertIn("cannot say whether the result is changing", message)
        # And it stays escalated, at one stable rule key, so a person who
        # answered "always" is not re-prompted by a key that changed.
        self.assertEqual(self._call()["rule_key"], escalated["rule_key"])

    def test_the_second_stage_is_not_reached_before_the_blocks_are_ignored(self):
        self._run_to_threshold()

        # One short of the escalation: every one of these is stage one.
        for _ in range(REPEAT_CALL_APPROVAL_THRESHOLD - REPEAT_CALL_BLOCK_THRESHOLD - 1):
            self.assertEqual(self._call()["action"], "block")

        self.assertEqual(self._call()["action"], "block")
        self.assertEqual(self._call()["action"], "approve")

    def test_no_argument_text_reaches_the_approval_prompt_or_its_rule_key(self):
        marker = "OMH-APPROVAL-ARGUMENT-CANARY"
        args = {"pattern": f"{marker}|{BURN_PATTERN}", "path": f"/private/{marker}"}
        self._run_to_escalation(args=args)

        escalated = self._call(args=args)

        self.assertEqual(escalated["action"], "approve")
        for field in ("message", "rule_key"):
            self.assertNotIn(marker, str(escalated[field]))
            self.assertNotIn(BURN_PATTERN, str(escalated[field]))
            self.assertNotIn("/private/", str(escalated[field]))
        self.assertIn(tool_args_digest(args), escalated["rule_key"])

    def test_a_different_call_between_the_stages_returns_to_the_start(self):
        self._run_to_escalation()
        self.assertEqual(self._call()["action"], "approve")

        self.assertIsNone(self._call(tool="read_file"))

        # Back to allowing, not back to stage one: both counters cleared.
        self.assertIsNone(self._call())
        streak = repeat_call_streak(str(self.home), session_id="session-a")
        self.assertEqual(streak["stage"], "watching")
        self.assertEqual(streak["intercepted"], 0)
        self.assertEqual(streak["consecutive"], 1)

    def test_the_staleness_window_clears_both_counters(self):
        digest = tool_args_digest(BURN_ARGS)
        for _ in range(REPEAT_CALL_BLOCK_THRESHOLD):
            record_tool_call(
                "search_files",
                omh_home=str(self.home),
                now=NOW,
                args_digest=digest,
                session_id="session-a",
            )
        for _ in range(REPEAT_CALL_APPROVAL_THRESHOLD - REPEAT_CALL_BLOCK_THRESHOLD):
            record_repeat_refusal(
                tool_name="search_files",
                args_digest=digest,
                session_id="session-a",
                omh_home=str(self.home),
                now=NOW,
            )
        self.assertEqual(
            repeat_call_directive(
                tool_name="search_files",
                args_digest=digest,
                session_id="session-a",
                omh_home=str(self.home),
                now=NOW,
            )["action"],
            "approve",
        )

        stale = NOW + REPEAT_STREAK_WINDOW_SECONDS + 1

        self.assertIsNone(
            repeat_call_directive(
                tool_name="search_files",
                args_digest=digest,
                session_id="session-a",
                omh_home=str(self.home),
                now=stale,
            )
        )
        self.assertEqual(
            repeat_call_streak(str(self.home), session_id="session-a", now=stale)["status"],
            "idle",
        )

    def test_two_sessions_do_not_add_up_at_the_approval_stage_either(self):
        self._run_to_escalation(session="session-a")
        # session-b has made this call once and must be nowhere near a
        # stage, however far session-a has gone with the identical call.
        self.assertIsNone(self._call(session="session-b"))

        self.assertEqual(self._call(session="session-a")["action"], "approve")
        self.assertIsNone(self._call(session="session-b"))

    def test_the_streak_reader_exposes_the_consecutive_count(self):
        self.assertEqual(repeat_call_streak(str(self.home), session_id="session-a")["status"], "idle")
        self._run_to_threshold()
        self._call()

        streak = repeat_call_streak(str(self.home), session_id="session-a")

        self.assertEqual(streak["status"], "observed")
        self.assertEqual(streak["tool"], "search_files")
        self.assertEqual(streak["args_digest"], tool_args_digest(BURN_ARGS))
        self.assertEqual(streak["ran"], REPEAT_CALL_BLOCK_THRESHOLD)
        self.assertEqual(streak["intercepted"], 1)
        self.assertEqual(streak["consecutive"], REPEAT_CALL_BLOCK_THRESHOLD + 1)
        self.assertEqual(streak["stage"], "blocking")
        self.assertIn("never evidence of what any call returned", streak["claim_boundary"])
        # A reader for another session sees nothing of this one.
        self.assertEqual(repeat_call_streak(str(self.home), session_id="session-b")["status"], "idle")

    def test_repeats_below_the_threshold_proceed(self):
        for _ in range(REPEAT_CALL_BLOCK_THRESHOLD - 1):
            self._call()

        self.assertIsNone(self._call())

    def test_a_different_digest_between_repeats_resets_the_count(self):
        self._run_to_threshold()

        self.assertIsNone(self._call(args={"pattern": BURN_PATTERN, "path": "tests"}))
        # Back to the original arguments: the streak restarted, so this is
        # the first call of a new run, not the fifth of the old one.
        self.assertIsNone(self._call())
        self.assertEqual(self._ledger()["repeat_streaks"]["session-a"]["count"], 1)

    def test_a_different_tool_between_repeats_resets_the_count(self):
        self._run_to_threshold()

        self.assertIsNone(self._call(tool="read_file"))
        self.assertIsNone(self._call())
        self.assertEqual(self._ledger()["repeat_streaks"]["session-a"]["count"], 1)

    def test_two_sessions_making_the_same_call_do_not_add_up(self):
        # Interleaved, so an unkeyed counter would see one session making
        # 2x threshold identical calls in a row and refuse long before the
        # end of this loop.
        for _ in range(REPEAT_CALL_BLOCK_THRESHOLD):
            self.assertIsNone(self._call(session="session-a"))
            self.assertIsNone(self._call(session="session-b"))

        # Each session counted its own calls, and each is now at the
        # threshold on its own.
        self.assertEqual(self._call(session="session-a")["action"], "block")
        self.assertEqual(self._call(session="session-b")["action"], "block")

    def test_closing_each_call_does_not_disarm_the_guard(self):
        # post_tool_call rewrites the same ledger. A writer that dropped the
        # streak key would leave the guard permanently at one on any host
        # that pairs its tool calls, which is every current one.
        for index in range(REPEAT_CALL_BLOCK_THRESHOLD):
            self.assertIsNone(
                pre_tool_call(
                    tool_name="search_files",
                    tool_input=BURN_ARGS,
                    session_id="session-a",
                    omh_home=str(self.home),
                    tool_call_id=f"call-{index}",
                )
            )
            post_tool_call(tool_call_id=f"call-{index}", omh_home=str(self.home))

        self.assertEqual(self._call()["action"], "block")

    def test_a_missing_session_id_degrades_to_allow(self):
        for _ in range(REPEAT_CALL_BLOCK_THRESHOLD * 2):
            self.assertIsNone(self._call(session=""))

        self.assertEqual(self._ledger()["repeat_streaks"], {})

    def test_arguments_that_cannot_be_canonicalized_degrade_to_allow(self):
        circular: dict[str, object] = {"pattern": BURN_PATTERN}
        circular["self"] = circular

        self.assertEqual(tool_args_digest(circular), "")
        for _ in range(REPEAT_CALL_BLOCK_THRESHOLD * 2):
            self.assertIsNone(self._call(args=circular))

        self.assertEqual(self._ledger()["repeat_streaks"], {})

    def test_an_unreadable_ledger_degrades_to_allow(self):
        self._run_to_threshold()
        self.assertEqual(self._call()["action"], "block")

        tool_bursts_path(str(self.home)).write_text("{not json", encoding="utf-8")

        self.assertIsNone(self._call())

    def test_a_streak_older_than_the_window_is_not_a_loop(self):
        digest = tool_args_digest(BURN_ARGS)
        for _ in range(REPEAT_CALL_BLOCK_THRESHOLD):
            record_tool_call(
                "search_files",
                omh_home=str(self.home),
                now=NOW,
                args_digest=digest,
                session_id="session-a",
            )

        self.assertIsNotNone(
            repeat_call_directive(
                tool_name="search_files",
                args_digest=digest,
                session_id="session-a",
                omh_home=str(self.home),
                now=NOW + REPEAT_STREAK_WINDOW_SECONDS,
            )
        )
        self.assertIsNone(
            repeat_call_directive(
                tool_name="search_files",
                args_digest=digest,
                session_id="session-a",
                omh_home=str(self.home),
                now=NOW + REPEAT_STREAK_WINDOW_SECONDS + 1,
            )
        )


class LedgerPrivacyTest(unittest.TestCase):
    """The ledger may hold a fingerprint of the arguments, never the text."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def test_no_raw_argument_text_reaches_the_written_ledger(self):
        marker = "OMH-REPEAT-GUARD-ARGUMENT-CANARY"
        args = {"pattern": f"{marker}|{BURN_PATTERN}", "path": f"/private/{marker}/src"}

        for _ in range(REPEAT_CALL_BLOCK_THRESHOLD + 2):
            pre_tool_call(
                tool_name="search_files",
                tool_input=args,
                session_id="session-a",
                omh_home=str(self.home),
            )

        # Asserted against the file's bytes, not the returned object: the
        # question is what landed on disk.
        written = tool_bursts_path(str(self.home)).read_bytes()
        self.assertNotIn(marker.encode(), written)
        self.assertNotIn(BURN_PATTERN.encode(), written)
        self.assertNotIn(b"/private/", written)
        # And the digest is there, so this is a file that recorded the call
        # in fingerprint form rather than a file that recorded nothing.
        self.assertIn(tool_args_digest(args).encode(), written)

    def test_the_digest_is_short_one_way_and_capped_before_hashing(self):
        digest = tool_args_digest(BURN_ARGS)
        self.assertEqual(len(digest), 16)
        self.assertNotIn(BURN_PATTERN, digest)
        self.assertEqual(digest, tool_args_digest(dict(reversed(list(BURN_ARGS.items())))))
        self.assertNotEqual(digest, tool_args_digest({**BURN_ARGS, "path": "tests"}))
        # Past the cap the digest stops changing, which is the documented
        # cost of bounding the hash input.
        body = "x" * MAX_DIGEST_INPUT_BYTES
        self.assertEqual(tool_args_digest(body + "a"), tool_args_digest(body + "b"))
        # An object json cannot serialize still digests, via default=str.
        self.assertTrue(tool_args_digest({"path": Path("src")}))


if __name__ == "__main__":
    unittest.main()

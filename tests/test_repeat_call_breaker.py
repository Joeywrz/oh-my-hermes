"""Contracts for the repeated-tool-call circuit breaker at pre_tool_call.

The motivating failure: a Hermes session searching a repo called
`search_files` with one identical pattern more than 180 times in a row,
each returning in ~0.0s, while the tool result itself printed "BLOCKED:
You have run this exact search N times in a row". The model read that
warning and issued the same call again until the session's budget was
gone and the work it was asked to do had never started. A warning inside a
tool result is not enough, so the hook refuses the call instead.

Everything outside the narrow positive case -- same session, same tool,
same argument digest, consecutive, at the threshold -- must allow. #1674
is what an over-broad pre_tool_call veto costs, and these tests pin the
allow side at least as hard as the block side.
"""

import json
import tempfile
import unittest
from pathlib import Path

from omh.plugin_bundle.omh.hooks.tool_hooks import post_tool_call, pre_tool_call
from omh.plugin_bundle.omh.tool_bursts import (
    MAX_DIGEST_INPUT_BYTES,
    REPEAT_CALL_BLOCK_THRESHOLD,
    REPEAT_STREAK_WINDOW_SECONDS,
    record_tool_call,
    repeat_call_directive,
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

    def _ledger(self):
        return json.loads(tool_bursts_path(str(self.home)).read_text(encoding="utf-8"))

    def test_the_call_after_the_threshold_is_refused_and_names_the_alternative(self):
        self._run_to_threshold()

        blocked = self._call()

        self.assertEqual(blocked["action"], "block")
        message = str(blocked["message"])
        # The count is what actually ran: a refused call never dispatches
        # and never advances the streak.
        self.assertIn(f"{REPEAT_CALL_BLOCK_THRESHOLD} times in a row", message)
        self.assertIn("search_files", message)
        # The observed failure was a model repeating a search instead of
        # switching tactic, so the refusal has to say what to do instead.
        self.assertIn("read the file directly", message)
        self.assertIn("run the search once in the terminal", message)
        self.assertIn("A blocked call did not run", message)
        # Still refused on the next attempt, and still reporting the number
        # of calls that ran rather than the number attempted.
        again = self._call()
        self.assertEqual(again["action"], "block")
        self.assertIn(f"{REPEAT_CALL_BLOCK_THRESHOLD} times in a row", str(again["message"]))

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

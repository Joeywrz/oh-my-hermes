"""No internal token reaches a reader, and the signal agrees with the words.

Two separate claims. The first is a sweep: every rendered line is checked
against the identifiers the briefing routes on, because a token that escapes
does so silently -- it looks like a word until someone tries to read it. The
second is that the glyph, the headline and the `Action:` line cannot disagree,
which they could while the headline was computed without the blockers.
"""

from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()
from omh.catalogs.briefing_vocabulary import ACTION_LABELS, LINE_LABELS, action_text  # noqa: E402
from omh.wrapper.briefing import (  # noqa: E402
    SIGNAL_COMPLETE,
    SIGNAL_RUNNING,
    SIGNAL_STOPPED,
    SIGNAL_WAITING,
    build_coding_briefing,
    signal_glyph,
)

# Identifiers the wrapper routes on. None of them is a word, and each one
# reached a chat surface before this series.
_INTERNAL_TOKENS = (
    "workspace_isolation",
    "executor_result",
    "merge_ready",
    "executor_session_error",
    "show_runtime_handoff",
    "show_prompt_handoff",
    "accept_or_revise_plan",
    "choose_executor",
    "prepare_handoff",
    "report_completion_with_evidence",
    "record_runtime_observation",
    "surface_runtime_failure",
    "surface_runtime_blocker",
    "surface_runtime_cancellation",
    "worktree_creation",
    "worker_dispatch",
    "worker_result",
    "merge_readiness",
    "runtime_start",
    "prepared_not_observed",
    "not_observed",
)


def _briefing(**kwargs: object) -> dict[str, object]:
    session = {"session_id": "sess", "status": "runtime_handoff_prepared", "selected_executor_profile": "codex"}
    return build_coding_briefing(
        session,
        runtime_status=kwargs.get("runtime_status") or {"run_id": "run"},  # type: ignore[arg-type]
        executor_status=kwargs.get("executor_status") or {"selected_executor_profile": "codex"},  # type: ignore[arg-type]
        runtime_observation=kwargs.get("runtime_observation") or {},  # type: ignore[arg-type]
        locale=str(kwargs.get("locale") or ""),
    )


# A run with every rung of the ladder behind it. Spelled out rather than built
# from a loop because each key is a DIFFERENT producer's evidence, and the point
# of the finished state is that it is withheld until all of them agree: dropping
# any one line below puts a name back in `pending_gaps` and the signal returns
# to `running`, which is what the negative cases here assert.
_FINISHED_RUNTIME: dict[str, object] = {
    "run_id": "run",
    "prepared": {"handoff_available": True},
    "execution": {"observed": True, "status": "completed"},
    "verification": {"observed": True},
    "review": {"status": "passed", "satisfied": True},
    "ci": {"status": "passed", "satisfied": True},
    "merge_readiness": {"status": "ready", "satisfied": True},
    "merge": {"status": "merged", "satisfied": True},
}
_FINISHED_EXECUTOR: dict[str, object] = {
    "selected_executor_profile": "codex",
    "dispatch": "observed",
    "result": "completed",
    "verification": "satisfied",
    "workspace_isolation": {"status": "observed"},
}


def _finished(**overrides: object) -> dict[str, object]:
    runtime = {**_FINISHED_RUNTIME, **overrides}
    return _briefing(runtime_status=runtime, executor_status=dict(_FINISHED_EXECUTOR))


_CASES: tuple[tuple[str, dict[str, object]], ...] = (
    ("not started", {}),
    ("finished", {"runtime_status": dict(_FINISHED_RUNTIME), "executor_status": dict(_FINISHED_EXECUTOR)}),
    ("ci failed", {"runtime_observation": {"failed_events": ["ci"], "next_action": "surface_runtime_failure:ci"}}),
    ("worktree blocked", {"runtime_observation": {"blocked_events": ["worktree_creation"], "next_action": "surface_runtime_blocker:worktree_creation"}}),
    ("run cancelled", {"runtime_observation": {"cancelled_events": ["worker_result"], "next_action": "surface_runtime_cancellation:worker_result"}}),
    ("waiting on review", {"runtime_observation": {"observed_events": ["runtime_start", "worker_result"], "next_action": "record_runtime_observation:review"}}),
    ("session error", {"executor_status": {"selected_executor_profile": "codex", "executor_session_error": "auth failed"}}),
    ("plan stage", {"runtime_status": {"run_id": "run", "next_action": "accept_or_revise_plan"}}),
    ("executor choice", {"runtime_status": {"run_id": "run", "next_action": "choose_executor"}}),
)


class NoInternalTokenReachesTheReaderTests(unittest.TestCase):
    def test_no_rendered_line_contains_an_internal_token(self) -> None:
        for name, kwargs in _CASES:
            for locale in ("", "ko", "ja", "de"):
                briefing = _briefing(**kwargs, locale=locale)
                rendered = "\n".join(str(line) for line in briefing["user_facing_lines"])
                for token in _INTERNAL_TOKENS:
                    with self.subTest(case=name, locale=locale or "en", token=token):
                        self.assertNotIn(token, rendered)

    def test_the_payload_still_carries_the_tokens(self) -> None:
        """The words are for the reader; a parser keeps the identifiers."""
        briefing = _briefing(runtime_observation={"failed_events": ["ci"], "next_action": "surface_runtime_failure:ci"})
        self.assertEqual(briefing["next_action"], "surface_runtime_failure:ci")
        self.assertEqual(briefing["blockers"], [{"id": "ci", "kind": "failed", "event": "ci"}])
        self.assertIn("workspace_isolation", briefing["pending_gaps"])

    def test_an_unrecognized_action_token_is_not_printed(self) -> None:
        """An open set: the fallback must be words, never the token itself."""
        briefing = _briefing(runtime_status={"run_id": "run", "next_action": "some_future_action_nobody_listed"})
        rendered = "\n".join(str(line) for line in briefing["user_facing_lines"])
        self.assertNotIn("some_future_action_nobody_listed", rendered)
        self.assertIn(ACTION_LABELS["unknown"]["en"], rendered)
        self.assertEqual(briefing["next_action"], "some_future_action_nobody_listed")


class SignalTests(unittest.TestCase):
    def test_a_stopped_run_is_red_and_says_so(self) -> None:
        briefing = _briefing(runtime_observation={"failed_events": ["ci"]})
        self.assertEqual(briefing["signal"], SIGNAL_STOPPED)
        self.assertTrue(str(briefing["user_facing_lines"][0]).startswith("🔴"))
        self.assertIn("stopped", briefing["headline"])

    def test_an_undispatched_run_waits_on_the_reader(self) -> None:
        self.assertEqual(_briefing()["signal"], SIGNAL_WAITING)

    def test_a_dispatched_run_is_green(self) -> None:
        briefing = _briefing(
            runtime_status={"run_id": "run", "execution": {"observed": True, "status": "completed"}},
            executor_status={"selected_executor_profile": "codex", "dispatch": "observed", "result": "completed"},
        )
        self.assertEqual(briefing["signal"], SIGNAL_RUNNING)

    def test_the_glyph_never_contradicts_the_headline(self) -> None:
        """A red run headed "reported completion" is what this prevents."""
        for name, kwargs in _CASES:
            briefing = _briefing(**kwargs)
            with self.subTest(case=name):
                first = str(briefing["user_facing_lines"][0])
                self.assertTrue(first.startswith(signal_glyph(str(briefing["signal"]))))
                if briefing["signal"] == SIGNAL_STOPPED:
                    self.assertNotIn("completion", briefing["headline"])
                    self.assertNotIn("merged", briefing["headline"])

    def test_every_signal_has_a_distinct_glyph(self) -> None:
        glyphs = {signal_glyph(signal) for signal in (SIGNAL_STOPPED, SIGNAL_WAITING, SIGNAL_RUNNING, SIGNAL_COMPLETE)}
        self.assertEqual(len(glyphs), 4)


class NothingLeftTests(unittest.TestCase):
    """A finished run says so, and an unfinished one never does.

    The state this covers used to be spelled `running`, so the only evidence a
    reader had that a run was over was that the `Remaining:` line had stopped
    appearing. These assert the positive claim and, more importantly, the two
    ways it must be withheld.
    """

    def test_a_finished_run_reports_complete_and_says_so(self) -> None:
        briefing = _finished()
        self.assertEqual(briefing["signal"], SIGNAL_COMPLETE)
        self.assertEqual(briefing["pending_gaps"], [])
        lines = [str(line) for line in briefing["user_facing_lines"]]
        self.assertTrue(lines[0].startswith("✅"))
        self.assertIn(LINE_LABELS["nothing_left"]["en"], "\n".join(lines))

    def test_the_headline_agrees_with_the_finished_mark(self) -> None:
        """A ✅ over "is handling the coding work" is what this prevents."""
        headline = str(_finished()["headline"])
        self.assertIn("finished", headline)
        self.assertNotIn("is handling", headline)

    def test_a_more_specific_finished_headline_is_not_replaced(self) -> None:
        """The finished headline catches a fall-through; it does not outrank.

        Unlike a stop, a finished run does not contradict the ladder branches,
        and "recorded as merged" names what happened where this sentence only
        reports that nothing is outstanding. Putting the check first cost that
        distinction, which is how this case was found.
        """
        briefing = _briefing(
            runtime_status=dict(_FINISHED_RUNTIME),
            executor_status=dict(_FINISHED_EXECUTOR),
            runtime_observation={"observed_events": ["merge"]},
        )
        self.assertEqual(briefing["signal"], SIGNAL_COMPLETE)
        self.assertIn("merged", str(briefing["headline"]))
        self.assertNotIn("nothing is outstanding", str(briefing["headline"]))

    def test_one_unfinished_rung_withholds_the_claim(self) -> None:
        """Any single rung still open wins over all the others being closed.

        `verification` is cleared on BOTH producers: `_progress_steps` reads it
        from `runtime_status["verification"]["observed"]` OR from
        `executor_status["verification"]`, so emptying one leaves the step
        complete on the strength of the other. That is not a quirk of the
        fixture -- a rung with two independent sources is exactly where a
        "withhold until everything agrees" rule is easiest to get wrong.
        """
        # The evidence key and the step id it feeds are different vocabularies
        # -- `merge_readiness` reports as `merge_ready`, `merge` as `merged` --
        # so both are named rather than assumed equal.
        rungs: tuple[tuple[str, dict[str, object], dict[str, object]], ...] = (
            ("review", {"review": {}}, {}),
            ("ci", {"ci": {}}, {}),
            ("merge_ready", {"merge_readiness": {}}, {}),
            ("merged", {"merge": {}}, {}),
            ("verification", {"verification": {}}, {"verification": "not_observed"}),
            ("workspace_isolation", {}, {"workspace_isolation": {"status": "prepared_not_observed"}}),
        )
        for rung, runtime_override, executor_override in rungs:
            briefing = _briefing(
                runtime_status={**_FINISHED_RUNTIME, **runtime_override},
                executor_status={**_FINISHED_EXECUTOR, **executor_override},
            )
            with self.subTest(rung=rung):
                self.assertIn(rung, briefing["pending_gaps"])
                self.assertEqual(briefing["signal"], SIGNAL_RUNNING)
                rendered = "\n".join(str(line) for line in briefing["user_facing_lines"])
                self.assertNotIn(LINE_LABELS["nothing_left"]["en"], rendered)

    def test_a_climbing_runtime_ladder_withholds_the_claim(self) -> None:
        """The case `pending_gaps` alone cannot see.

        The runtime ladder reports in `runtime_milestone_gaps`, a separate list
        built from the handoff contract's `status_ladder`. Every progress step
        here is complete and `pending_gaps` is empty, so a signal derived from
        that list alone would announce a finished run while the ladder still had
        unobserved rungs.
        """
        briefing = _briefing(
            runtime_status={
                **_FINISHED_RUNTIME,
                "handoff_contract": {"hermes_coding_team_path": {"status_ladder": ["runtime_start", "merge"]}},
            },
            executor_status=dict(_FINISHED_EXECUTOR),
        )
        self.assertEqual(briefing["pending_gaps"], [])
        self.assertEqual(briefing["runtime_milestone_gaps"], ["runtime_start", "merge"])
        self.assertEqual(briefing["signal"], SIGNAL_RUNNING)

    def test_the_action_line_does_not_ask_for_a_finished_step(self) -> None:
        """`next_action` is an open set and nothing clears it on the way out."""
        briefing = _finished(next_action="accept_or_revise_plan")
        self.assertEqual(briefing["signal"], SIGNAL_COMPLETE)
        self.assertEqual(briefing["next_action"], "accept_or_revise_plan")
        rendered = "\n".join(str(line) for line in briefing["user_facing_lines"])
        self.assertNotIn(ACTION_LABELS["accept_or_revise_plan"]["en"], rendered)
        self.assertIn(f"{LINE_LABELS['action']['en']}: {LINE_LABELS['action_none']['en']}.", rendered)

    def test_the_sentence_is_translated_everywhere_the_table_goes(self) -> None:
        for locale, expected in LINE_LABELS["nothing_left"].items():
            with self.subTest(locale=locale):
                rendered = "\n".join(str(line) for line in _briefing(
                    runtime_status=dict(_FINISHED_RUNTIME),
                    executor_status=dict(_FINISHED_EXECUTOR),
                    locale=locale,
                )["user_facing_lines"])
                self.assertIn(expected, rendered)

    def test_the_signal_ignores_the_action_token(self) -> None:
        """Read from observed state, not from free text a producer chose.

        Same observed state, an action token that claims otherwise: the signal
        must not move.
        """
        observed = {"failed_events": ["ci"]}
        honest = _briefing(runtime_observation=observed)
        misleading = _briefing(runtime_observation={**observed, "next_action": "report_runtime_observed"})
        self.assertEqual(honest["signal"], misleading["signal"])


class ActionTextTests(unittest.TestCase):
    def test_a_composed_runtime_action_names_its_event_in_words(self) -> None:
        self.assertEqual(action_text("surface_runtime_failure:ci"), "look at the failure on CI")
        self.assertEqual(
            action_text("surface_runtime_blocker:worktree_creation", locale="ko"),
            "작업 폴더 생성 차단 해제",
        )

    def test_the_two_handoff_actions_read_the_same(self) -> None:
        self.assertEqual(action_text("show_runtime_handoff"), action_text("show_prompt_handoff"))

    def test_an_action_with_no_event_still_resolves(self) -> None:
        self.assertEqual(action_text("show_status"), "none")


if __name__ == "__main__":
    unittest.main()

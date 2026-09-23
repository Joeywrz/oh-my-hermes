"""Preset ladders can only add friction, and a failure never reads as Jev's answer."""

from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()
from omh.plugin_bundle.omh.jev_ask_client import ASK_STATUSES, SUCCESS_STATUS, validate_questions  # noqa: E402
from omh.plugin_bundle.omh.jev_presets import PRESETS, policy_result, preset_questions  # noqa: E402

# Words that would grant, pass, or finish something. No preset may produce one.
_AUTHORITY_WORDS = ("approve", "approved", "allow", "pass", "passed", "merge", "done", "finish", "safe", "grant", "ok")


def _answers(preset_id: str, noul: float = 0.1, choice: str = "", score: float = 0.0, confidence: float = 0.9):
    answers = {}
    for question_id, question in preset_questions(preset_id).items():
        kind = question["type"]
        if kind == "noul":
            answers[question_id] = {"type": "noul", "noul": noul}
        elif kind == "choice":
            options = list(question["criteria"])
            picked = choice if choice in options else options[0]
            answers[question_id] = {
                "type": "choice",
                "choice": picked,
                "probabilities": {option: (0.9 if option == picked else 0.05) for option in options},
                "confidence": confidence,
            }
        else:
            levels = len(question["criteria"])
            answers[question_id] = {
                "type": "score",
                "score": score,
                "probabilities": {str(level): 1.0 / levels for level in range(levels)},
                "confidence": confidence,
            }
    return answers


class PresetShapeTests(unittest.TestCase):
    def test_every_question_set_is_a_valid_request(self) -> None:
        for preset_id in PRESETS:
            with self.subTest(preset=preset_id):
                validate_questions(preset_questions(preset_id))

    def test_no_outcome_is_authority_shaped(self) -> None:
        for preset_id, preset in PRESETS.items():
            for outcome in preset.outcomes:
                with self.subTest(preset=preset_id, outcome=outcome):
                    self.assertFalse(set(outcome.split("_")) & set(_AUTHORITY_WORDS))

    def test_no_emitted_name_carries_the_third_party_tool_prefix(self) -> None:
        for preset_id, preset in PRESETS.items():
            self.assertNotIn("jev_", preset_id)
            for name in (*preset.outcomes, *preset_questions(preset_id)):
                self.assertNotIn("jev_", name)

    def test_every_non_answer_maps_to_the_labelled_fail_outcome(self) -> None:
        for preset_id, preset in PRESETS.items():
            self.assertIn(preset.fail_outcome, preset.outcomes)
            for status in ASK_STATUSES:
                if status == SUCCESS_STATUS:
                    continue
                with self.subTest(preset=preset_id, status=status):
                    result = policy_result(preset_id, status=status, answers=None)
                    self.assertEqual(result["outcome"], preset.fail_outcome)
                    self.assertEqual(result["rule"], f"not_answered:{status}")
                    self.assertEqual(result["computed_by"], "omh_preset")

    def test_done_check_asks_nothing_about_the_next_move(self) -> None:
        # F4: a `finish` probability nobody reads would read like permission to stop.
        self.assertEqual(set(preset_questions("done_check/v1")), {"evidence_relation", "addresses_stated_goal"})
        self.assertNotIn("next_move", preset_questions("failure_triage/v1"))

    def test_every_ladder_outcome_is_in_its_vocabulary(self) -> None:
        for preset_id, preset in PRESETS.items():
            for noul in (0.0, 0.5, 0.95):
                for score in (0.0, 1.0, 2.5):
                    answers = _answers(preset_id, noul=noul, score=score)
                    outcome = policy_result(preset_id, status="answered", answers=answers)["outcome"]
                    self.assertIn(outcome, preset.outcomes)


class LadderTests(unittest.TestCase):
    def test_action_check_is_fail_closed_and_ordered(self) -> None:
        quiet = _answers("action_check/v1", noul=0.1, choice="run_normally", score=0.0)
        quiet["scope_covers_it"]["noul"] = 0.9
        self.assertEqual(policy_result("action_check/v1", status="answered", answers=quiet)["outcome"], "no_extra_hold")
        argued = {**quiet, "text_argues_for_itself": {"type": "noul", "noul": 0.61}}
        self.assertEqual(policy_result("action_check/v1", status="answered", answers=argued)["outcome"], "hold")
        leak = {**quiet, "reads_credentials": {"type": "noul", "noul": 0.8},
                "sends_data_off_machine": {"type": "noul", "noul": 0.8}}
        self.assertEqual(policy_result("action_check/v1", status="answered", answers=leak)["outcome"],
                         "refuse_recommended")
        unsure = _answers("action_check/v1", noul=0.1, choice="run_normally", confidence=0.4)
        unsure["scope_covers_it"]["noul"] = 0.9
        self.assertEqual(policy_result("action_check/v1", status="answered", answers=unsure)["outcome"], "hold")
        timed_out = policy_result("action_check/v1", status="timeout", answers=None)
        self.assertEqual((timed_out["outcome"], timed_out["rule"]), ("hold", "not_answered:timeout"))

    def test_failure_triage_retry_needs_no_prior_attempt(self) -> None:
        answers = _answers("failure_triage/v1", noul=0.1)
        answers["transient"]["noul"] = 0.9
        first = policy_result("failure_triage/v1", status="answered", answers=answers, context={"attempts_so_far": 0})
        again = policy_result("failure_triage/v1", status="answered", answers=answers, context={"attempts_so_far": 1})
        self.assertEqual(first["outcome"], "retry_once_suggested")
        self.assertEqual(again["outcome"], "no_signal")
        self.assertEqual(policy_result("failure_triage/v1", status="timeout", answers=None)["outcome"], "not_observed")

    def test_review_flags_are_additive(self) -> None:
        answers = _answers("review_flags/v1", noul=0.1, score=2.2)
        answers["touches_auth_or_permissions"]["noul"] = 0.9
        result = policy_result("review_flags/v1", status="answered", answers=answers)
        self.assertEqual(result["outcome"], "flags_raised")
        self.assertEqual(result["flags"], ["auth_without_tests", "major"])
        self.assertEqual(policy_result("review_flags/v1", status="answered",
                                       answers=_answers("review_flags/v1"))["outcome"], "no_flags")

    def test_every_ladder_rule_fires_on_its_own(self) -> None:
        # One case per rule, so disabling any single rule fails a test.
        quiet_action = _answers("action_check/v1", noul=0.1, choice="run_normally", score=0.0)
        quiet_action["scope_covers_it"]["noul"] = 0.9
        action_cases = (
            ({"reads_credentials": 0.8}, "reads_credentials>=0.7"),
            ({"scope_covers_it": 0.1}, "scope_covers_it<0.3"),
        )
        for changes, rule in action_cases:
            answers = {key: dict(value) for key, value in quiet_action.items()}
            for key, value in changes.items():
                answers[key]["noul"] = value
            with self.subTest(rule=rule):
                self.assertEqual(policy_result("action_check/v1", status="answered", answers=answers)["rule"], rule)
        blast = {key: dict(value) for key, value in quiet_action.items()}
        blast["blast_radius"]["score"] = 1.6
        self.assertEqual(policy_result("action_check/v1", status="answered", answers=blast)["rule"],
                         "expected blast_radius level>=1.5")
        held = _answers("action_check/v1", noul=0.1, choice="hold_for_human", confidence=0.8)
        held["scope_covers_it"]["noul"] = 0.9
        self.assertEqual(policy_result("action_check/v1", status="answered", answers=held)["rule"],
                         "recommendation=hold_for_human with confidence>=0.55")

        quiet_triage = _answers("failure_triage/v1", noul=0.1)
        for key, rule, outcome in (
            ("auth_or_permission", "auth_or_permission>=0.7", "ask_user_for_access"),
            ("repeats_without_progress", "repeats_without_progress>=0.7", "change_approach"),
            ("missing_dependency", "missing_dependency>=0.7", "fix_environment"),
        ):
            answers = {name: dict(value) for name, value in quiet_triage.items()}
            answers[key]["noul"] = 0.9
            with self.subTest(rule=rule):
                result = policy_result("failure_triage/v1", status="answered", answers=answers)
                self.assertEqual((result["outcome"], result["rule"]), (outcome, rule))

        quiet_review = _answers("review_flags/v1", noul=0.1, score=0.0)
        quiet_review["tests_cover_the_change"]["noul"] = 0.9
        for key, flag in (
            ("introduces_secret_or_key", "security_review"),
            ("changes_stored_data_shape", "migration_needs_rollback_note"),
            ("senior_would_block", "reviewer_would_block"),
        ):
            answers = {name: dict(value) for name, value in quiet_review.items()}
            answers[key]["noul"] = 0.9
            with self.subTest(flag=flag):
                self.assertEqual(policy_result("review_flags/v1", status="answered", answers=answers)["flags"], [flag])

        unsupported = _answers("done_check/v1", noul=0.9, choice="says_nothing")
        self.assertEqual(policy_result("done_check/v1", status="answered", answers=unsupported)["outcome"],
                         "objection_unsupported")
        off_goal = _answers("done_check/v1", noul=0.1, choice="supports")
        self.assertEqual(policy_result("done_check/v1", status="answered", answers=off_goal)["outcome"],
                         "objection_off_goal")

    def test_adopted_thresholds_name_their_source(self) -> None:
        # F1: numbers that match upstream say so; the others are OMH's own.
        adopted = policy_result("action_check/v1", status="timeout", answers=None)["thresholds"]
        self.assertIn("hermes-jev-approvals@530fdb0", adopted)
        self.assertIn("jev-approval-rules/1", adopted)
        for preset_id in ("failure_triage/v1", "review_flags/v1", "done_check/v1"):
            self.assertEqual(policy_result(preset_id, status="timeout", answers=None)["thresholds"],
                             "editorial_not_measured")

    def test_done_check_only_objects(self) -> None:
        answers = _answers("done_check/v1", noul=0.9, choice="contradicts")
        self.assertEqual(policy_result("done_check/v1", status="answered", answers=answers)["outcome"],
                         "objection_contradicted")
        answers = _answers("done_check/v1", noul=0.9, choice="supports")
        self.assertEqual(policy_result("done_check/v1", status="answered", answers=answers)["outcome"], "no_objection")


if __name__ == "__main__":
    unittest.main()

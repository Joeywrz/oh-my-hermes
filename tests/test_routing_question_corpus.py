from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()

from omh.quality.reported_rate import reported_rate_shape_errors  # noqa: E402
from omh.quality.routing_precision import (  # noqa: E402
    ROUTING_INTERVENTION_CASES,
    ROUTING_PRECISION_CASES,
    build_routing_precision_demo,
    routing_precision_errors,
)
from omh.quality.routing_question_corpus import (  # noqa: E402
    DETERMINISTIC_ARM,
    INTERVENTION_CORPUS,
    NEGATIVE_CONTROL_CORPUS,
    ROUTE_QUESTION_ANSWER_SCHEMA_VERSION,
    ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION,
    ROUTING_QUESTION_CORPUS_SCHEMA_VERSION,
    ROUTING_QUESTION_SCORE_SCHEMA_VERSION,
    RoutingQuestionCorpusError,
    answer_records_from_rows,
    build_routing_question_corpus,
    corpus_shape_errors,
    read_answer_source,
    resolve_answer_action,
    routing_question_score_errors,
    score_routing_question_answers,
)
from omh.routing.route_question import (  # noqa: E402
    NO_WORKFLOW_OPTION,
    ROUTE_CHOICE_KEY,
    fit_question_key,
)


def _answer_row(case_id: str, arm: str, choice: str, fits: dict[str, float]) -> dict[str, object]:
    answers: dict[str, object] = {ROUTE_CHOICE_KEY: {"choice": choice}}
    for skill, value in fits.items():
        answers[fit_question_key(skill)] = {"noul": value}
    return {
        "schema_version": ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION,
        "case_id": case_id,
        "arm": arm,
        "answers": answers,
    }


def _synthetic_item(
    case_id: str,
    corpus: str,
    expected_action: str,
    expected_choice: str,
    *,
    candidates: tuple[str, ...] = ("plan",),
    deterministic: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "corpus": corpus,
        "message": f"message for {case_id}",
        "candidates": [{"skill": skill, "description": skill} for skill in candidates],
        "question": {
            "schema_version": "route_question/v1",
            "question_digest": f"digest-{case_id}",
            "questions": {
                ROUTE_CHOICE_KEY: {
                    "type": "choice",
                    "instructions": "pick one",
                    "options": {**{skill: skill for skill in candidates}, NO_WORKFLOW_OPTION: "none"},
                },
                **{fit_question_key(skill): {"type": "noul", "instructions": skill} for skill in candidates},
            },
            "thresholds": {"fits_dispatch": 0.8, "fits_clarify": 0.5},
            "reasons": [],
            "claim_boundary": "test",
        },
        "expected": {"action": expected_action, "choice": expected_choice},
        "deterministic": deterministic
        or {"action": expected_action, "choice": expected_choice, "overrouted": False, "case_passed": True},
    }


def _synthetic_corpus(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": ROUTING_QUESTION_CORPUS_SCHEMA_VERSION,
        "source": "discord",
        "generated_from": {"schema": "routing_precision/v1", "case_count": 0, "intervention_case_count": 0},
        "question_contract": {
            "choice_key": ROUTE_CHOICE_KEY,
            "fit_prefix": "fits::",
            "none_option": NO_WORKFLOW_OPTION,
            "thresholds": {"fits_dispatch": 0.8, "fits_clarify": 0.5},
        },
        "items": items,
        "claim_boundary": "test",
    }


class RoutingQuestionCorpusTests(unittest.TestCase):
    """The projection of the two shipped corpora, and the arm it produces."""

    corpus: dict[str, object]
    precision: dict[str, object]

    @classmethod
    def setUpClass(cls) -> None:
        # Both producers route every case, so build each once for the class.
        cls.corpus = build_routing_question_corpus()
        cls.precision = build_routing_precision_demo(source="discord")

    def test_the_corpus_is_a_projection_and_adds_no_case(self) -> None:
        self.assertEqual(self.corpus["schema_version"], ROUTING_QUESTION_CORPUS_SCHEMA_VERSION)
        self.assertEqual(corpus_shape_errors(self.corpus), ())
        items = self.corpus["items"]
        self.assertEqual(len(items), len(ROUTING_PRECISION_CASES) + len(ROUTING_INTERVENTION_CASES))
        self.assertEqual(
            sum(1 for item in items if item["corpus"] == NEGATIVE_CONTROL_CORPUS),
            len(ROUTING_PRECISION_CASES),
        )
        self.assertEqual(
            sum(1 for item in items if item["corpus"] == INTERVENTION_CORPUS),
            len(ROUTING_INTERVENTION_CASES),
        )
        self.assertEqual(
            self.corpus["generated_from"],
            {
                "schema": "routing_precision/v1",
                "case_count": len(ROUTING_PRECISION_CASES),
                "intervention_case_count": len(ROUTING_INTERVENTION_CASES),
            },
        )
        self.assertEqual(len({item["case_id"] for item in items}), len(items))

    def test_the_projection_is_deterministic(self) -> None:
        again = build_routing_question_corpus()
        self.assertEqual(json.dumps(again, sort_keys=True), json.dumps(self.corpus, sort_keys=True))

    def test_the_deterministic_arm_reproduces_the_producers_own_verdicts(self) -> None:
        # This is the pin. Re-deriving an over-route predicate from the route
        # payload reports hundreds of false over-routes, because `clarify` with
        # a named candidate is a pass in the negative corpus and the expected
        # intervention in the positive one. The verdicts below must be the
        # producer's, case by case, not a rule restated in the projection.
        self.assertEqual(routing_precision_errors(self.precision), [])
        by_id = {item["case_id"]: item for item in self.corpus["items"]}
        for row in self.precision["cases"]:
            item = by_id[row["id"]]
            self.assertEqual(item["corpus"], NEGATIVE_CONTROL_CORPUS)
            self.assertEqual(item["deterministic"]["overrouted"], bool(row["observed"]["overrouted"]))
            self.assertEqual(item["deterministic"]["case_passed"], bool(row["passed"]))
        for row in self.precision["intervention_cases"]:
            item = by_id[row["id"]]
            self.assertEqual(item["corpus"], INTERVENTION_CORPUS)
            self.assertEqual(item["deterministic"]["case_passed"], bool(row["passed"]))

    def test_the_deterministic_arm_scores_clean_on_both_corpora(self) -> None:
        score = score_routing_question_answers(self.corpus)
        self.assertEqual(score["schema_version"], ROUTING_QUESTION_SCORE_SCHEMA_VERSION)
        self.assertEqual(routing_question_score_errors(score), [])
        arm = score["arms"][DETERMINISTIC_ARM]
        self.assertEqual(arm["overroute"], 0)
        self.assertEqual(arm["missed"], 0)
        self.assertEqual(arm["malformed"], 0)
        self.assertEqual(arm["wrong_workflow"], 0)
        self.assertEqual(arm["answered"], len(self.corpus["items"]))
        self.assertEqual(arm["unanswered"], 0)
        self.assertEqual(score["malformed"], [])
        self.assertEqual(score["unmatched"], [])
        # And: the pin can fail. A projection that scored the router's clarify
        # answers as dispatches would report exactly this instead.
        hijacked = json.loads(json.dumps(self.corpus))
        for item in hijacked["items"]:
            if item["corpus"] == NEGATIVE_CONTROL_CORPUS and item["deterministic"]["action"] == "clarify":
                item["deterministic"]["overrouted"] = True
        broken = score_routing_question_answers(hijacked)
        self.assertGreater(broken["arms"][DETERMINISTIC_ARM]["overroute"], 0)
        self.assertNotEqual(routing_question_score_errors(broken), [])

    def test_every_reported_rate_carries_its_denominator(self) -> None:
        score = score_routing_question_answers(self.corpus)
        arm = score["arms"][DETERMINISTIC_ARM]
        for field in ("overroute_rate", "missed_rate", "workflow_accuracy"):
            self.assertEqual(reported_rate_shape_errors(arm[field]), (), field)
        self.assertEqual(arm["overroute_rate"]["percent"], 0.0)
        self.assertEqual(arm["missed_rate"]["percent"], 0.0)
        self.assertEqual(arm["workflow_accuracy"]["percent"], 100.0)
        self.assertIn("unanswered_cases", arm["overroute_rate"]["excluded"])

    def test_a_fallback_case_and_a_clarify_without_a_candidate_expect_no_workflow(self) -> None:
        by_id = {item["case_id"]: item for item in self.corpus["items"]}
        fallbacks = [case for case in ROUTING_INTERVENTION_CASES if case.expected_route_action == "fallback"]
        self.assertTrue(fallbacks, "the intervention corpus no longer carries a fallback case")
        for case in fallbacks:
            self.assertEqual(
                by_id[case.id]["expected"],
                {"action": "none", "choice": NO_WORKFLOW_OPTION},
            )
        candidateless = [
            case
            for case in ROUTING_INTERVENTION_CASES
            if case.expected_route_action == "clarify" and not case.expected_candidate
        ]
        self.assertTrue(candidateless, "the intervention corpus no longer carries a candidate-free clarify")
        for case in candidateless:
            self.assertEqual(by_id[case.id]["expected"]["choice"], NO_WORKFLOW_OPTION)
        for case in ROUTING_PRECISION_CASES[:5]:
            self.assertEqual(
                by_id[case.id]["expected"],
                {"action": "none", "choice": NO_WORKFLOW_OPTION},
            )

    def test_every_item_carries_a_question_whose_options_include_none(self) -> None:
        for item in self.corpus["items"]:
            options = item["question"]["questions"][ROUTE_CHOICE_KEY]["options"]
            self.assertIn(NO_WORKFLOW_OPTION, options)
            for candidate in item["candidates"]:
                self.assertIn(candidate["skill"], options)
                self.assertIn(fit_question_key(candidate["skill"]), item["question"]["questions"])
                self.assertFalse(candidate["description"].startswith("[omh]"))
            self.assertLessEqual(len(item["candidates"]), 3)

    def test_an_unsupported_source_or_limit_is_refused(self) -> None:
        with self.assertRaisesRegex(RoutingQuestionCorpusError, "unsupported corpus source"):
            build_routing_question_corpus(source="not-a-surface")
        with self.assertRaisesRegex(RoutingQuestionCorpusError, "positive integer"):
            build_routing_question_corpus(limit=0)


class RoutingQuestionScoringTests(unittest.TestCase):
    """What an answer set is worth, on a corpus small enough to count by hand."""

    def setUp(self) -> None:
        self.corpus = _synthetic_corpus(
            [
                _synthetic_item("neg-1", NEGATIVE_CONTROL_CORPUS, "none", NO_WORKFLOW_OPTION),
                _synthetic_item("neg-2", NEGATIVE_CONTROL_CORPUS, "none", NO_WORKFLOW_OPTION),
                _synthetic_item("int-dispatch", INTERVENTION_CORPUS, "dispatch", "plan"),
                _synthetic_item("int-clarify", INTERVENTION_CORPUS, "clarify", "plan"),
                _synthetic_item("int-none", INTERVENTION_CORPUS, "none", NO_WORKFLOW_OPTION),
            ]
        )

    def test_the_nouls_decide_whether_and_the_choice_decides_which(self) -> None:
        strong = {ROUTE_CHOICE_KEY: {"choice": "plan"}, fit_question_key("plan"): {"noul": 0.9}}
        middling = {ROUTE_CHOICE_KEY: {"choice": "plan"}, fit_question_key("plan"): {"noul": 0.6}}
        weak = {ROUTE_CHOICE_KEY: {"choice": "plan"}, fit_question_key("plan"): {"noul": 0.2}}
        thresholds = {"fits_dispatch": 0.8, "fits_clarify": 0.5}
        self.assertEqual(resolve_answer_action(strong, **thresholds), ("dispatch", "plan"))
        self.assertEqual(resolve_answer_action(middling, **thresholds), ("clarify", "plan"))
        self.assertEqual(resolve_answer_action(weak, **thresholds), ("none", "plan"))
        # With no fit answers at all the choice is the only signal there is.
        self.assertEqual(
            resolve_answer_action({ROUTE_CHOICE_KEY: {"choice": "plan"}}, **thresholds),
            ("dispatch", "plan"),
        )
        self.assertEqual(
            resolve_answer_action({ROUTE_CHOICE_KEY: {"choice": NO_WORKFLOW_OPTION}}, **thresholds),
            ("none", NO_WORKFLOW_OPTION),
        )

    def test_synthetic_answers_produce_the_counts_they_were_written_for(self) -> None:
        rows = [
            # An over-route: a negative control answered as a dispatch.
            _answer_row("neg-1", "model", "plan", {"plan": 0.95}),
            # Not an over-route: a clarify asks one question and hijacks nothing.
            _answer_row("neg-2", "model", "plan", {"plan": 0.6}),
            # A miss: the case expects a workflow and the arm named none.
            _answer_row("int-dispatch", "model", NO_WORKFLOW_OPTION, {"plan": 0.1}),
            # The right band, the wrong workflow.
            _answer_row("int-clarify", "model", "review", {"review": 0.6}),
            # Expected none, answered none.
            _answer_row("int-none", "model", NO_WORKFLOW_OPTION, {"plan": 0.1}),
        ]
        score = score_routing_question_answers(self.corpus, answer_records_from_rows(rows))
        arm = score["arms"]["model"]
        self.assertEqual(arm["answered"], 5)
        self.assertEqual(arm["unanswered"], 0)
        self.assertEqual(arm["overroute"], 1)
        self.assertEqual(arm["missed"], 1)
        self.assertEqual(arm["wrong_workflow"], 2)
        self.assertEqual(arm["band_mismatch"], 0)
        self.assertEqual(arm["overroute_rate"]["denominator"], 3)
        self.assertEqual(arm["missed_rate"]["denominator"], 2)
        self.assertEqual(arm["workflow_accuracy"]["denominator"], 2)
        self.assertEqual(arm["workflow_accuracy"]["numerator"], 0)
        self.assertIn(DETERMINISTIC_ARM, score["arms"])

    def test_the_right_workflow_in_the_wrong_band_is_reported_not_missed(self) -> None:
        rows = [
            _answer_row("int-clarify", "model", "plan", {"plan": 0.95}),
            _answer_row("int-dispatch", "model", "plan", {"plan": 0.6}),
        ]
        arm = score_routing_question_answers(self.corpus, answer_records_from_rows(rows))["arms"]["model"]
        self.assertEqual(arm["band_mismatch"], 2)
        self.assertEqual(arm["missed"], 0)
        self.assertEqual(arm["wrong_workflow"], 0)
        self.assertEqual(arm["workflow_accuracy"]["numerator"], 2)
        self.assertEqual(arm["unanswered"], 3)

    def test_malformed_rows_are_counted_and_named_never_dropped(self) -> None:
        good = _answer_row("neg-1", "model", NO_WORKFLOW_OPTION, {"plan": 0.1})
        wrong_schema = dict(good, schema_version="something/v9", case_id="neg-2")
        no_arm = dict(good, arm="", case_id="int-none")
        no_choice = {
            "schema_version": ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION,
            "case_id": "int-clarify",
            "arm": "model",
            "answers": {fit_question_key("plan"): {"noul": 0.9}},
        }
        out_of_range = _answer_row("int-dispatch", "model", "plan", {"plan": 4.0})
        duplicate = _answer_row("neg-1", "model", NO_WORKFLOW_OPTION, {"plan": 0.1})
        records = answer_records_from_rows([good, wrong_schema, no_arm, no_choice, out_of_range, duplicate])
        score = score_routing_question_answers(self.corpus, records)
        self.assertEqual(score["arms"]["model"]["answered"], 1)
        self.assertEqual(score["arms"]["model"]["malformed"], 3)
        self.assertEqual(score["arms"]["unknown"]["malformed"], 2)
        refs = {entry["ref"] for entry in score["malformed"]}
        self.assertEqual(refs, {f"row:{index}" for index in (2, 3, 4, 5, 6)})
        reasons = " ".join(entry["reason"] for entry in score["malformed"])
        self.assertIn("schema_version is not", reasons)
        self.assertIn("names no arm", reasons)
        self.assertIn("route_choice answer", reasons)
        self.assertIn("fit_answer_out_of_range:plan", reasons)
        self.assertIn("duplicate answer for neg-1", reasons)

    def test_an_arm_that_answered_nothing_reports_no_percentage(self) -> None:
        broken = dict(_answer_row("neg-1", "silent", NO_WORKFLOW_OPTION, {}), schema_version="wrong/v1")
        score = score_routing_question_answers(self.corpus, answer_records_from_rows([broken]))
        arm = score["arms"]["unknown"]
        self.assertEqual(arm["answered"], 0)
        self.assertEqual(arm["unanswered"], 5)
        for field in ("overroute_rate", "missed_rate", "workflow_accuracy"):
            self.assertEqual(reported_rate_shape_errors(arm[field]), (), field)
            self.assertIsNone(arm[field]["percent"])
            self.assertEqual(arm[field]["basis"], "no_observations")

    def test_an_answer_for_a_case_the_corpus_does_not_carry_is_reported(self) -> None:
        stray = _answer_row("not-in-this-corpus", "model", "plan", {"plan": 0.9})
        score = score_routing_question_answers(self.corpus, answer_records_from_rows([stray]))
        self.assertEqual(len(score["unmatched"]), 1)
        self.assertEqual(score["unmatched"][0]["case_id"], "not-in-this-corpus")
        self.assertEqual(score["arms"]["model"]["answered"], 0)

    def test_thresholds_can_be_overridden_and_a_nonsense_pair_is_refused(self) -> None:
        rows = [_answer_row("neg-1", "model", "plan", {"plan": 0.6})]
        strict = score_routing_question_answers(
            self.corpus, answer_records_from_rows(rows), thresholds={"fits_dispatch": 0.5}
        )
        self.assertEqual(strict["arms"]["model"]["overroute"], 1)
        self.assertEqual(strict["thresholds"]["fits_dispatch"], 0.5)
        with self.assertRaisesRegex(RoutingQuestionCorpusError, "cannot exceed"):
            score_routing_question_answers(
                self.corpus, (), thresholds={"fits_dispatch": 0.2, "fits_clarify": 0.9}
            )
        with self.assertRaisesRegex(RoutingQuestionCorpusError, "between 0 and 1"):
            score_routing_question_answers(self.corpus, (), thresholds={"fits_dispatch": 4.0})

    def test_a_corpus_that_is_not_one_is_refused(self) -> None:
        with self.assertRaises(RoutingQuestionCorpusError):
            score_routing_question_answers({"schema_version": "other/v1", "items": []})
        self.assertEqual(corpus_shape_errors("not a corpus"), ("corpus is not an object",))


class RoutingQuestionAnswerSourceTests(unittest.TestCase):
    """Both answer sources: a JSONL file, and a directory of recorded answers."""

    def setUp(self) -> None:
        self.corpus = _synthetic_corpus(
            [
                _synthetic_item("neg-1", NEGATIVE_CONTROL_CORPUS, "none", NO_WORKFLOW_OPTION),
                _synthetic_item("int-dispatch", INTERVENTION_CORPUS, "dispatch", "plan"),
            ]
        )

    def test_a_jsonl_file_names_the_line_that_could_not_be_read(self) -> None:
        with TemporaryDirectory() as root:
            path = Path(root) / "answers.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(_answer_row("neg-1", "model", NO_WORKFLOW_OPTION, {"plan": 0.1}), sort_keys=True),
                        "",
                        "{not json",
                        json.dumps(_answer_row("int-dispatch", "model", "plan", {"plan": 0.9}), sort_keys=True),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            score = score_routing_question_answers(self.corpus, read_answer_source(path))
        arm = score["arms"]["model"]
        self.assertEqual(arm["answered"], 2)
        self.assertEqual(arm["overroute"], 0)
        self.assertEqual(arm["missed"], 0)
        self.assertEqual(score["arms"]["unknown"]["malformed"], 1)
        self.assertEqual(score["malformed"][0]["ref"], "answers.jsonl:3")

    def test_a_directory_of_recorded_answers_joins_by_question_digest(self) -> None:
        row = _answer_row("", "", "plan", {"plan": 0.9})
        row.pop("case_id")
        row.pop("arm")
        record = {
            "schema_version": ROUTE_QUESTION_ANSWER_SCHEMA_VERSION,
            "answered_by": "main_model",
            "question_digest": "digest-int-dispatch",
            "answer": row,
        }
        stray = dict(record, question_digest="digest-nothing")
        with TemporaryDirectory() as root:
            directory = Path(root) / "route-questions"
            directory.mkdir()
            (directory / "one.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
            (directory / "two.json").write_text(json.dumps(stray, sort_keys=True), encoding="utf-8")
            (directory / "three.json").write_text("{not json", encoding="utf-8")
            score = score_routing_question_answers(self.corpus, read_answer_source(directory))
        arm = score["arms"]["main_model"]
        self.assertEqual(arm["answered"], 1)
        self.assertEqual(arm["missed"], 0)
        self.assertEqual(arm["workflow_accuracy"]["numerator"], 1)
        self.assertEqual([entry["ref"] for entry in score["unmatched"]], ["two.json"])
        self.assertEqual([entry["ref"] for entry in score["malformed"]], ["three.json"])

    def test_a_missing_answer_source_is_refused_by_name(self) -> None:
        with self.assertRaisesRegex(RoutingQuestionCorpusError, "answer source not found"):
            read_answer_source(Path("/nonexistent/answers.jsonl"))


if __name__ == "__main__":
    unittest.main()

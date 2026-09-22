from __future__ import annotations

import hashlib
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
    precision_case_interaction,
    routing_precision_errors,
)
from omh.quality.routing_question_corpus import (  # noqa: E402
    DETERMINISTIC_ARM,
    INTERVENTION_CORPUS,
    MAX_ANSWER_SOURCE_BYTES,
    MAX_CORPUS_BYTES,
    NEGATIVE_CONTROL_CORPUS,
    ROUTE_QUESTION_ANSWER_SCHEMA_VERSION,
    ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION,
    ROUTING_QUESTION_CORPUS_SCHEMA_VERSION,
    ROUTING_QUESTION_SCORE_SCHEMA_VERSION,
    RoutingQuestionCorpusError,
    answer_records_from_rows,
    build_routing_question_corpus,
    corpus_shape_errors,
    format_routing_question_score,
    read_answer_source,
    read_routing_question_corpus,
    resolve_answer_action,
    routing_question_score_errors,
    score_routing_question_answers,
)
from omh.routing.chat import route_chat_message, routing_record_payload  # noqa: E402
from omh.routing.route_question import (  # noqa: E402
    NO_WORKFLOW_OPTION,
    ROUTE_CHOICE_KEY,
    build_route_question_for_candidate_handoff,
    build_route_question_from_candidates,
    fit_question_key,
    message_digest,
    route_question_digest,
)


def _digest_groups(items: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for item in items:
        groups.setdefault(str(item["question"]["question_digest"]), []).append(item)
    return groups


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
    live_joinable: bool = True,
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "corpus": corpus,
        "message": f"message for {case_id}",
        "message_sha256": f"message-sha-{case_id}",
        "question_source": "candidate_handoff" if live_joinable else "recommendations",
        "live_joinable": live_joinable,
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
    by_case_route: dict[str, dict[str, object]]

    @classmethod
    def setUpClass(cls) -> None:
        # Both producers route every case, so build each once for the class.
        cls.corpus = build_routing_question_corpus()
        cls.precision = build_routing_precision_demo(source="discord")
        cls.by_case_route = {
            case.id: precision_case_interaction(case, source="discord")["route"]
            for case in ROUTING_PRECISION_CASES
        }

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
            # The shown shortlist is the digested shortlist, on both sources.
            self.assertEqual(
                [candidate["skill"] for candidate in item["candidates"]],
                [option for option in options if option != NO_WORKFLOW_OPTION],
            )
            if item["question_source"] == "recommendations":
                # `limit` cuts this source and only this source.
                self.assertLessEqual(len(item["candidates"]), 3)

    def test_a_live_route_asks_the_handoff_question_and_the_corpus_asks_the_same_one(self) -> None:
        # The join this benchmark's live arm depends on. A live route builds
        # its question from the candidate handoff; a corpus that built one from
        # `route["recommendations"]` instead would ask about the same skills
        # with different text (the public projection drops `description`) and
        # different reasons, and every recorded live answer would land as
        # unmatched with nothing in the report saying why.
        by_id = {item["case_id"]: item for item in self.corpus["items"]}
        checked = 0
        for case in ROUTING_PRECISION_CASES:
            route = precision_case_interaction(case, source="discord")["route"]
            handoff = route.get("candidate_handoff")
            if not isinstance(handoff, dict):
                continue
            # The published live call shape, written out rather than imported,
            # so a change to the helper cannot quietly change what is pinned.
            live = build_route_question_from_candidates(
                [row for row in handoff.get("candidates", []) if isinstance(row, dict)],
                message_sha256=hashlib.sha256(case.message.encode("utf-8")).hexdigest(),
                reasons=[str(reason) for reason in handoff.get("reasons", [])],
            )
            item = by_id[case.id]
            self.assertEqual(item["question_source"], "candidate_handoff")
            self.assertTrue(item["live_joinable"])
            self.assertEqual(item["question"], live)
            self.assertEqual(build_route_question_for_candidate_handoff(handoff, message=case.message), live)
            checked += 1
        self.assertTrue(checked, "no negative-control case carries a candidate handoff any more")

    def test_the_public_router_entry_produces_the_digest_the_corpus_carries(self) -> None:
        # The digest PR-C's live route will write, reached the way a live route
        # reaches it: through the public router entry rather than through this
        # package's own accessor. Building the digest from the handoff
        # candidates alone -- no question block, no builder -- also pins that
        # nothing else in the block moves it.
        by_id = {item["case_id"]: item for item in self.corpus["items"]}
        checked = 0
        for case in ROUTING_PRECISION_CASES:
            route = route_chat_message(case.message, source="discord")
            handoff = route.get("candidate_handoff")
            if not isinstance(handoff, dict):
                continue
            self.assertEqual(
                by_id[case.id]["question"]["question_digest"],
                route_question_digest(
                    message_sha256=message_digest(case.message),
                    candidates=[row for row in handoff.get("candidates", []) if isinstance(row, dict)],
                ),
                case.id,
            )
            checked += 1
        self.assertTrue(checked, "the public router entry attaches no candidate handoff any more")

    def test_only_the_cases_a_live_route_questions_are_live_joinable(self) -> None:
        items = self.corpus["items"]
        sources = self.corpus["question_sources"]
        self.assertEqual(
            sources["candidate_handoff"] + sources["recommendations"],
            len(items),
        )
        self.assertEqual(sources["live_joinable"], sources["candidate_handoff"])
        self.assertTrue(sources["candidate_handoff"], "no item carries a handoff question")
        self.assertTrue(sources["recommendations"], "no item carries a recommendations question")
        for item in items:
            self.assertEqual(item["live_joinable"], item["question_source"] == "candidate_handoff")
        # And: a decided route carries no handoff, which is why live never
        # questions it rather than a choice made here.
        decided = next(item for item in items if item["question_source"] == "recommendations")
        route = self.by_case_route[decided["case_id"]]
        self.assertNotIsInstance(route.get("candidate_handoff"), dict)

    def test_the_message_digest_has_one_producer_across_every_surface(self) -> None:
        # Three surfaces now have to agree on this value: the corpus item, the
        # question digest built from it, and a recorded answer joined back by
        # it. A fourth inline hashlib call is how they stop agreeing.
        case = ROUTING_PRECISION_CASES[0]
        interaction = precision_case_interaction(case, source="discord")
        record = routing_record_payload(interaction["route"], case.message)
        derived = message_digest(case.message)
        self.assertEqual(derived, record["message_sha256"])
        self.assertEqual(derived, interaction["message_sha256"])
        self.assertEqual(derived, hashlib.sha256(case.message.encode("utf-8")).hexdigest())
        by_id = {item["case_id"]: item for item in self.corpus["items"]}
        self.assertEqual(by_id[case.id]["message_sha256"], derived)

    def test_a_question_digest_names_one_question_and_never_two_different_ones(self) -> None:
        # The pin this reader was missing. A digest built from the candidate
        # shortlist alone is shared by hundreds of unrelated requests, and the
        # directory reader joins a recorded answer by digest and nothing else,
        # so a shared digest silently scores an answer against some other
        # case's expected answer.
        items = self.corpus["items"]
        for item in items:
            self.assertEqual(
                item["question"]["question_digest"],
                route_question_digest(
                    message_sha256=item["message_sha256"],
                    candidates=item["candidates"],
                ),
            )
        shared = [group for group in _digest_groups(items).values() if len(group) > 1]
        for group in shared:
            # A digest may only be shared by items that ask the identical
            # question: the same request, the same shortlist. The two corpora
            # do record a handful of requests verbatim in both, and those are
            # the only items allowed to collide.
            self.assertEqual(len({item["message_sha256"] for item in group}), 1)
            self.assertEqual(
                len({tuple(candidate["skill"] for candidate in item["candidates"]) for item in group}),
                1,
            )
        # And: those collisions are real, and they carry different expected
        # answers, which is exactly why the scorer may not take the first.
        self.assertTrue(shared, "no verbatim-shared request remains; the ambiguity pin below is dead")
        self.assertTrue(
            any(len({str(item["expected"]) for item in group}) > 1 for group in shared),
            "no shared digest spans two different expected answers",
        )

    def test_an_answer_whose_digest_reaches_two_cases_is_reported_not_scored(self) -> None:
        items = self.corpus["items"]
        shared = [group for group in _digest_groups(items).values() if len(group) > 1]
        group = next(g for g in shared if len({str(item["expected"]) for item in g}) > 1)
        digest = str(group[0]["question"]["question_digest"])
        skill = group[0]["candidates"][0]["skill"]
        row = {
            "schema_version": ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION,
            "arm": "recorded",
            "question_digest": digest,
            "answers": {ROUTE_CHOICE_KEY: {"choice": skill}, fit_question_key(skill): {"noul": 0.95}},
        }
        score = score_routing_question_answers(self.corpus, answer_records_from_rows([row]))
        self.assertEqual(score["arms"]["recorded"]["answered"], 0)
        self.assertEqual(score["arms"]["recorded"]["overroute"], 0)
        self.assertEqual(score["unmatched"], [])
        self.assertEqual(len(score["ambiguous"]), 1)
        self.assertEqual(score["ambiguous"][0]["question_digest"], digest)
        self.assertEqual(score["ambiguous"][0]["case_count"], len(group))
        self.assertIn("Ambiguous answer records", format_routing_question_score(score))
        # A digest that reaches exactly one item still scores, so the refusal
        # above is about the ambiguity and not about digest joins in general.
        lone = next(
            item
            for item in items
            if len(_digest_groups(items)[str(item["question"]["question_digest"])]) == 1
            and item["candidates"]
        )
        alone = dict(
            row,
            question_digest=str(lone["question"]["question_digest"]),
            answers={
                ROUTE_CHOICE_KEY: {"choice": lone["candidates"][0]["skill"]},
                fit_question_key(lone["candidates"][0]["skill"]): {"noul": 0.95},
            },
        )
        joined = score_routing_question_answers(self.corpus, answer_records_from_rows([alone]))
        self.assertEqual(joined["arms"]["recorded"]["answered"], 1)
        self.assertEqual(joined["ambiguous"], [])

    def test_the_score_fails_when_the_producer_failed_a_case(self) -> None:
        # `case_passed` is the producer's verdict. Without a reader it is
        # written into every item and consulted by nothing, so a corpus
        # carrying failed verdicts scores clean -- including a dispatch on the
        # one intervention case whose correct answer is to open nothing.
        self.assertEqual(routing_question_score_errors(score_routing_question_answers(self.corpus)), [])
        regressed = json.loads(json.dumps(self.corpus))
        mutated = 0
        for item in regressed["items"]:
            if item["corpus"] != INTERVENTION_CORPUS:
                continue
            if item["expected"]["action"] == "none" and mutated == 0:
                item["deterministic"] = dict(
                    item["deterministic"], action="dispatch", case_passed=False
                )
                mutated += 1
            elif item["expected"]["action"] == "dispatch" and mutated == 1:
                item["deterministic"] = dict(
                    item["deterministic"], action="clarify", case_passed=False
                )
                mutated += 1
        self.assertEqual(mutated, 2)
        broken = score_routing_question_answers(regressed)
        arm = broken["arms"][DETERMINISTIC_ARM]
        self.assertEqual(arm["producer_failed"], 2)
        self.assertEqual(len(arm["producer_failed_cases"]), 2)
        errors = routing_question_score_errors(broken)
        self.assertTrue(any(error.startswith("deterministic_producer_failed") for error in errors), errors)
        self.assertIn("Producer verdict", format_routing_question_score(broken))
        # And: neither mutation moves an over-route or a miss, so the derived
        # counts alone would still have called this report clean.
        self.assertEqual(arm["overroute"], 0)
        self.assertEqual(arm["missed"], 0)

    def test_a_corpus_without_the_producers_verdict_cannot_be_scored(self) -> None:
        stripped = json.loads(json.dumps(self.corpus))
        for item in stripped["items"]:
            item["deterministic"].pop("case_passed")
        errors = corpus_shape_errors(stripped)
        self.assertTrue(errors)
        self.assertIn("case_passed", errors[0])
        with self.assertRaisesRegex(RoutingQuestionCorpusError, "case_passed"):
            score_routing_question_answers(stripped)

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

    def test_a_live_record_joins_by_message_when_its_shortlist_was_cut_differently(self) -> None:
        # The second ambiguity source. A question digest covers the candidate
        # shortlist, and the shortlist is cut per surface -- a route hint shows
        # two candidates where a full route shows three -- so the same request
        # asked on two surfaces produces two digests. Joining on the digest
        # alone drops the answer; joining on the message keeps it and reports
        # that the shortlist differed.
        corpus = build_routing_question_corpus()
        item = next(
            entry
            for entry in corpus["items"]
            if entry["live_joinable"] and len(entry["candidates"]) > 1
        )
        cut = build_route_question_from_candidates(
            item["candidates"][:1],
            message_sha256=item["message_sha256"],
        )
        self.assertNotEqual(cut["question_digest"], item["question"]["question_digest"])
        skill = item["candidates"][0]["skill"]
        record = {
            "schema_version": ROUTE_QUESTION_ANSWER_SCHEMA_VERSION,
            "answered_by": "live_route",
            "message_sha256": item["message_sha256"],
            "question_digest": cut["question_digest"],
            "answer": {
                "schema_version": ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION,
                "answers": {
                    ROUTE_CHOICE_KEY: {"choice": skill},
                    fit_question_key(skill): {"noul": 0.9},
                },
            },
        }
        with TemporaryDirectory() as root:
            directory = Path(root) / "records"
            directory.mkdir()
            (directory / "one.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
            score = score_routing_question_answers(corpus, read_answer_source(directory))
        arm = score["arms"]["live_route"]
        self.assertEqual(arm["answered"], 1)
        self.assertEqual(arm["digest_mismatch"], 1)
        self.assertEqual(score["unmatched"], [])
        self.assertEqual(score["ambiguous"], [])
        joined = score["matched"][0]
        self.assertEqual(joined["case_id"], item["case_id"])
        self.assertEqual(joined["joined_by"], "message_sha256")
        self.assertFalse(joined["digest_match"])
        self.assertIn("different shortlist cut", format_routing_question_score(score))
        # And: the same record carrying the corpus's own digest reports a match,
        # so `digest_match` tracks the shortlist and not the join.
        agreeing = dict(record, question_digest=item["question"]["question_digest"])
        with TemporaryDirectory() as root:
            directory = Path(root) / "records"
            directory.mkdir()
            (directory / "one.json").write_text(json.dumps(agreeing, sort_keys=True), encoding="utf-8")
            same = score_routing_question_answers(corpus, read_answer_source(directory))
        self.assertTrue(same["matched"][0]["digest_match"])
        self.assertEqual(same["arms"]["live_route"]["digest_mismatch"], 0)

    def test_a_live_arm_is_denominated_only_on_the_cases_a_live_route_asks(self) -> None:
        corpus = build_routing_question_corpus()
        live_count = corpus["question_sources"]["live_joinable"]
        item = next(entry for entry in corpus["items"] if entry["live_joinable"])
        skill = item["candidates"][0]["skill"] if item["candidates"] else NO_WORKFLOW_OPTION
        record = {
            "schema_version": ROUTE_QUESTION_ANSWER_SCHEMA_VERSION,
            "answered_by": "live_route",
            "message_sha256": item["message_sha256"],
            "answer": {
                "schema_version": ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION,
                "answers": {ROUTE_CHOICE_KEY: {"choice": skill}},
            },
        }
        with TemporaryDirectory() as root:
            directory = Path(root) / "records"
            directory.mkdir()
            (directory / "one.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
            score = score_routing_question_answers(corpus, read_answer_source(directory))
        arm = score["arms"]["live_route"]
        self.assertEqual(score["live_joinable_case_count"], live_count)
        self.assertLess(live_count, score["case_count"])
        # The cases a live route never asks about leave the denominator, and
        # the exclusion is named rather than silently folded into `unanswered`.
        self.assertEqual(arm["case_count"], live_count)
        self.assertEqual(arm["unanswered"], live_count - 1)
        for field in ("overroute_rate", "missed_rate", "workflow_accuracy"):
            self.assertIn("not_live_joinable", arm[field]["excluded"])
        # And: the deterministic arm still reads the whole corpus, so the
        # exclusion belongs to the live arm and not to the report.
        self.assertEqual(score["arms"][DETERMINISTIC_ARM]["case_count"], score["case_count"])
        self.assertNotIn(
            "not_live_joinable",
            score["arms"][DETERMINISTIC_ARM]["overroute_rate"]["excluded"],
        )

    def test_a_missing_answer_source_is_refused_by_name(self) -> None:
        with self.assertRaisesRegex(RoutingQuestionCorpusError, "answer source not found"):
            read_answer_source(Path("/nonexistent/answers.jsonl"))


class RoutingQuestionUntrustedInputTests(unittest.TestCase):
    """An answers file is written by somebody else, and the report is evidence."""

    def setUp(self) -> None:
        self.corpus = _synthetic_corpus(
            [
                _synthetic_item("neg-1", NEGATIVE_CONTROL_CORPUS, "none", NO_WORKFLOW_OPTION),
                _synthetic_item("int-dispatch", INTERVENTION_CORPUS, "dispatch", "plan"),
            ]
        )

    def test_a_digest_disambiguates_two_items_carrying_one_message(self) -> None:
        # The message is the primary key, but it is not always the narrower
        # one: two corpus items can record the same request asked about
        # different shortlists. The digest is tried even after the message came
        # back ambiguous, so that pair is joined rather than reported.
        shared = "shared-message-sha"
        first = _synthetic_item("neg-1", NEGATIVE_CONTROL_CORPUS, "none", NO_WORKFLOW_OPTION)
        second = _synthetic_item("int-dispatch", INTERVENTION_CORPUS, "dispatch", "review", candidates=("review",))
        first["message_sha256"] = shared
        second["message_sha256"] = shared
        corpus = _synthetic_corpus([first, second])
        self.assertNotEqual(
            first["question"]["question_digest"],
            second["question"]["question_digest"],
        )
        row = {
            "schema_version": ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION,
            "arm": "recorded",
            "message_sha256": shared,
            "question_digest": second["question"]["question_digest"],
            "answers": {ROUTE_CHOICE_KEY: {"choice": "review"}, fit_question_key("review"): {"noul": 0.9}},
        }
        score = score_routing_question_answers(corpus, answer_records_from_rows([row]))
        self.assertEqual(score["ambiguous"], [])
        self.assertEqual(score["arms"]["recorded"]["answered"], 1)
        self.assertEqual(score["matched"][0]["case_id"], "int-dispatch")
        self.assertEqual(score["matched"][0]["joined_by"], "question_digest")
        self.assertTrue(score["matched"][0]["digest_match"])
        # And: with no digest to narrow it, the same message is ambiguous.
        bare = {key: value for key, value in row.items() if key != "question_digest"}
        reported = score_routing_question_answers(corpus, answer_records_from_rows([bare]))
        self.assertEqual(reported["arms"]["recorded"]["answered"], 0)
        self.assertEqual(len(reported["ambiguous"]), 1)
        self.assertEqual(reported["ambiguous"][0]["by"], "message_sha256")
        self.assertEqual(reported["ambiguous"][0]["case_count"], 2)

    def test_a_row_claiming_the_deterministic_arm_is_refused_by_name(self) -> None:
        # The deterministic tally is assigned after every supplied row is
        # counted, so a row under that name would have its answers replaced
        # while its malformed count stayed visible: one report, two numbers,
        # disagreeing about the same arm.
        hijack = _answer_row("neg-1", DETERMINISTIC_ARM, "plan", {"plan": 0.95})
        score = score_routing_question_answers(self.corpus, answer_records_from_rows([hijack]))
        arm = score["arms"][DETERMINISTIC_ARM]
        self.assertEqual(arm["overroute"], 0)
        self.assertEqual(arm["malformed"], 0)
        self.assertEqual(score["arms"]["unknown"]["malformed"], 1)
        self.assertIn("reserved", score["malformed"][0]["reason"])
        self.assertEqual(score["malformed"][0]["arm"], "unknown")
        self.assertEqual(routing_question_score_errors(score), [])
        # And: the same answer under any other name is counted, so the refusal
        # is about the reserved name and not about the row.
        allowed = _answer_row("neg-1", "challenger", "plan", {"plan": 0.95})
        counted = score_routing_question_answers(self.corpus, answer_records_from_rows([allowed]))
        self.assertEqual(counted["arms"]["challenger"]["overroute"], 1)

    def test_no_untrusted_string_can_forge_a_line_in_the_report(self) -> None:
        forged = {
            "schema_version": ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION,
            "case_id": "neg-1",
            "arm": "\x1b[2K\rmodel\nfake arm: overroute 0",
            "answers": {
                ROUTE_CHOICE_KEY: {"choice": "plan"},
                fit_question_key("\x1b[31mINJECTED\x1b[0m\nMalformed rows: 0"): "not an object",
            },
        }
        score = score_routing_question_answers(self.corpus, answer_records_from_rows([forged]))
        rendered = format_routing_question_score(score)
        lines = rendered.splitlines()
        for line in lines:
            self.assertNotIn("\x1b", line)
            self.assertNotIn("\r", line)
        # The forged text survives as inert characters inside the line it was
        # read on; what it must not do is become a line of its own, because a
        # line is what a reader of this report counts.
        self.assertEqual([line for line in lines if line.startswith("fake arm")], [])
        self.assertEqual([line for line in lines if line.startswith("Malformed rows: 0")], [])
        # The forged arm is still reported, bounded rather than dropped.
        self.assertEqual(len(score["malformed"]), 1)
        self.assertIn("malformed_fit_answer:", score["malformed"][0]["reason"])
        reported_arm = score["malformed"][0]["arm"]
        self.assertNotIn("\n", reported_arm)
        self.assertLessEqual(len(reported_arm), 120)

    def test_every_untrusted_read_is_bounded_and_refused_by_name(self) -> None:
        with TemporaryDirectory() as root:
            answers = Path(root) / "answers.jsonl"
            answers.write_bytes(b"x" * (MAX_ANSWER_SOURCE_BYTES + 1))
            with self.assertRaisesRegex(RoutingQuestionCorpusError, "exceeds"):
                read_answer_source(answers)
            corpus_path = Path(root) / "corpus.json"
            corpus_path.write_bytes(b"x" * (MAX_CORPUS_BYTES + 1))
            with self.assertRaisesRegex(RoutingQuestionCorpusError, "exceeds"):
                read_routing_question_corpus(corpus_path)
            records = Path(root) / "records"
            records.mkdir()
            (records / "huge.json").write_bytes(b"x" * (MAX_ANSWER_SOURCE_BYTES + 1))
            reported = read_answer_source(records)
            self.assertEqual(len(reported), 1)
            self.assertIn("exceeds", reported[0].error)
            # And: a file under the cap is still read, so the cap is a cap and
            # not a refusal of the whole surface.
            corpus_path.write_text(json.dumps(self.corpus, sort_keys=True), encoding="utf-8")
            self.assertEqual(corpus_shape_errors(read_routing_question_corpus(corpus_path)), ())


if __name__ == "__main__":
    unittest.main()

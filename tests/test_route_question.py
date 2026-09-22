from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.routing.route_question import (  # noqa: E402
    FITS_CLARIFY_THRESHOLD,
    FITS_DISPATCH_THRESHOLD,
    NO_WORKFLOW_OPTION,
    ROUTE_CHOICE_KEY,
    ROUTE_QUESTION_SCHEMA_VERSION,
    build_route_question_from_candidates,
    clean_skill_description,
    fit_question_key,
    fit_question_skill,
    route_question_digest,
)


CANDIDATES = (
    {"skill": "plan", "description": "[omh] Hermes Plan workflow: structured planning before execution."},
    {"skill": "review", "description": "[omh] Review finished work before it ships."},
)
MESSAGE = "a" * 64
OTHER_MESSAGE = "b" * 64


class RouteQuestionBuilderTests(unittest.TestCase):
    def test_the_block_is_deterministic_for_the_same_candidates(self) -> None:
        first = build_route_question_from_candidates(CANDIDATES, message_sha256=MESSAGE)
        second = build_route_question_from_candidates(list(CANDIDATES), message_sha256=MESSAGE)
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], ROUTE_QUESTION_SCHEMA_VERSION)

    def test_the_catalog_prefix_never_reaches_an_option_or_a_question(self) -> None:
        block = build_route_question_from_candidates(CANDIDATES, message_sha256=MESSAGE)
        options = block["questions"][ROUTE_CHOICE_KEY]["options"]
        self.assertEqual(
            options["plan"],
            "Hermes Plan workflow: structured planning before execution.",
        )
        serialized = repr(block)
        self.assertNotIn("[omh]", serialized)
        # And: the strip is the thing under test, not a property of the fixture.
        self.assertEqual(clean_skill_description("[omh] Something"), "Something")
        self.assertEqual(clean_skill_description("Something"), "Something")

    def test_the_none_option_is_present_even_with_no_candidates(self) -> None:
        empty = build_route_question_from_candidates((), message_sha256=MESSAGE)
        options = empty["questions"][ROUTE_CHOICE_KEY]["options"]
        self.assertEqual(list(options), [NO_WORKFLOW_OPTION])
        self.assertEqual(
            [key for key in empty["questions"] if key != ROUTE_CHOICE_KEY],
            [],
        )
        with_candidates = build_route_question_from_candidates(CANDIDATES, message_sha256=MESSAGE)
        self.assertIn(NO_WORKFLOW_OPTION, with_candidates["questions"][ROUTE_CHOICE_KEY]["options"])

    def test_one_absolute_question_per_candidate_beside_the_relative_choice(self) -> None:
        block = build_route_question_from_candidates(CANDIDATES, message_sha256=MESSAGE)
        questions = block["questions"]
        self.assertEqual(questions[ROUTE_CHOICE_KEY]["type"], "choice")
        for candidate in CANDIDATES:
            key = fit_question_key(candidate["skill"])
            self.assertEqual(questions[key]["type"], "noul")
            self.assertIn("independently of the others", questions[key]["instructions"])
            self.assertEqual(fit_question_skill(key), candidate["skill"])
        self.assertEqual(fit_question_skill(ROUTE_CHOICE_KEY), "")
        self.assertIn("relative choice", questions[ROUTE_CHOICE_KEY]["instructions"])

    def test_thresholds_are_flat_and_carried_on_every_block(self) -> None:
        # Flat is the claim: a question exists because the router's confidence
        # was too low to act on, so an acceptance bar derived from that
        # confidence would be loosest exactly where the router is least sure.
        low = build_route_question_from_candidates(CANDIDATES, message_sha256=MESSAGE, reasons=("low_confidence",))
        none = build_route_question_from_candidates(
            CANDIDATES, message_sha256=MESSAGE, reasons=("no_trigger_coverage",)
        )
        self.assertEqual(low["thresholds"], none["thresholds"])
        self.assertEqual(
            low["thresholds"],
            {"fits_dispatch": FITS_DISPATCH_THRESHOLD, "fits_clarify": FITS_CLARIFY_THRESHOLD},
        )
        self.assertGreater(FITS_DISPATCH_THRESHOLD, FITS_CLARIFY_THRESHOLD)
        self.assertEqual(low["reasons"], ["low_confidence"])

    def test_a_supplied_digest_wins_and_an_absent_one_is_derived_from_content(self) -> None:
        derived = build_route_question_from_candidates(CANDIDATES, message_sha256=MESSAGE)
        same = build_route_question_from_candidates(CANDIDATES, message_sha256=MESSAGE)
        other = build_route_question_from_candidates(CANDIDATES[:1], message_sha256=MESSAGE)
        self.assertEqual(derived["question_digest"], same["question_digest"])
        self.assertNotEqual(derived["question_digest"], other["question_digest"])
        supplied = build_route_question_from_candidates(
            CANDIDATES, message_sha256=MESSAGE, digest="handoff-digest"
        )
        self.assertEqual(supplied["question_digest"], "handoff-digest")

    def test_the_digest_identifies_the_request_and_not_only_the_shortlist(self) -> None:
        # Two different requests routing to the same shortlist must not share a
        # digest. A digest is the only join key a recorded answer has when it
        # was written on a live route, so a shared one attributes an answer to
        # whichever case a reader happens to look at first.
        here = build_route_question_from_candidates(CANDIDATES, message_sha256=MESSAGE)
        elsewhere = build_route_question_from_candidates(CANDIDATES, message_sha256=OTHER_MESSAGE)
        self.assertNotEqual(here["question_digest"], elsewhere["question_digest"])
        self.assertEqual(
            here["question_digest"],
            route_question_digest(message_sha256=MESSAGE, candidates=CANDIDATES),
        )
        # The candidates still move it, so the shortlist is in there too.
        self.assertNotEqual(
            route_question_digest(message_sha256=MESSAGE, candidates=CANDIDATES),
            route_question_digest(message_sha256=MESSAGE, candidates=CANDIDATES[:1]),
        )

    def test_a_question_without_a_message_is_refused_rather_than_digested(self) -> None:
        with self.assertRaisesRegex(ValueError, "name the message"):
            build_route_question_from_candidates(CANDIDATES, message_sha256="")
        with self.assertRaisesRegex(ValueError, "name the message"):
            build_route_question_from_candidates(CANDIDATES, message_sha256="   ")

    def test_unusable_candidates_are_dropped_without_dropping_the_question(self) -> None:
        block = build_route_question_from_candidates(
            (
                {"skill": "plan", "description": "[omh] first"},
                {"skill": "plan", "description": "[omh] second"},
                {"skill": "", "description": "no name"},
                {"skill": NO_WORKFLOW_OPTION, "description": "cannot shadow the none option"},
                "not a mapping",
                {"skill": "review", "description": ""},
            ),
            message_sha256=MESSAGE,
        )
        options = block["questions"][ROUTE_CHOICE_KEY]["options"]
        self.assertEqual(sorted(options), sorted(["plan", "review", NO_WORKFLOW_OPTION]))
        self.assertEqual(options["plan"], "first")
        self.assertEqual(options[NO_WORKFLOW_OPTION], "No OMH workflow applies; answer the request directly.")
        self.assertNotIn(": ", block["questions"][fit_question_key("review")]["instructions"].split("?")[1])

    def test_the_block_states_that_an_unanswered_question_changes_nothing(self) -> None:
        block = build_route_question_from_candidates(CANDIDATES, message_sha256=MESSAGE)
        self.assertIn("deterministic route stays in force", block["claim_boundary"])


if __name__ == "__main__":
    unittest.main()

"""A message reaches a `jev-*` skill only by addressing Jev.

The corpus cases in `src/quality/routing_precision.py` pin the dispatched
owner for each sentence. These tests pin the rules those cases rest on, on the
deciding surface (`build_chat_interaction_payload`), including two sentences
whose current owner is itself questionable and so is deliberately not frozen
in the corpus: all that is asserted about them is that no Jev skill takes them.
"""

from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()
from omh.routing.jev_addressing import (  # noqa: E402
    JEV_SIBLING_BY_PARTNER,
    JEV_SKILL_NAMES,
    JEV_SKILL_PHRASES,
    addresses_jev,
    jev_addressed_skill,
)
from omh.skills.catalog import installable_skill_names  # noqa: E402
from omh.wrapper.contract import build_chat_interaction_payload  # noqa: E402


def _skill(message: str) -> str:
    return str(build_chat_interaction_payload(message, source="discord")["route"].get("selected_skill") or "")


class JevAddressingTests(unittest.TestCase):
    def test_talking_about_jev_is_not_addressing_it(self) -> None:
        # The six measured false swaps (critic C4) plus configuration requests.
        for message in (
            "review the jev plugin PR before merge",
            "review this diff that adds the omh_jev_ask tool",
            "compare jev with gpt for code review",
            "debug why the jev plugin keeps looping",
            "create a skill that calls jev for routing",
            "verify the jev integration works before merge",
            "set up the jev route in openrouter",
            "use jev as my router model",
            "install the jev skill from the catalog",
            "is jev a good coding model?",
            "don't use jev for this",
        ):
            with self.subTest(message=message):
                self.assertFalse(_skill(message).startswith("jev-"))

    def test_each_skill_phrase_outranks_the_generic_ask(self) -> None:
        # Critic C5: skill phrase > "ask jev" -> jev-ask > ask.
        names = set(installable_skill_names())
        for skill, phrases in JEV_SKILL_PHRASES.items():
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    self.assertEqual(jev_addressed_skill(phrase + " for this change", names), skill)
        self.assertEqual(_skill("ask jev whether this README section covers installation"), "jev-ask")
        self.assertEqual(_skill("ask claude to review this plan"), "ask")

    def test_the_sibling_table_only_names_real_skills(self) -> None:
        names = set(installable_skill_names())
        self.assertEqual(set(JEV_SKILL_NAMES) - names, set())
        for partner, sibling in JEV_SIBLING_BY_PARTNER.items():
            self.assertIn(partner, names)
            self.assertIn(sibling, names)

    def test_a_declined_or_optional_jev_is_not_addressing(self) -> None:
        # Negation, a choice among options, and descriptive frames contain an
        # addressing phrase and still do not address Jev.
        for message in (
            "don't ask jev, just review this diff",
            "never ask jev about this command, just run it",
            "we can ask jev later",
            "help me decide whether to use jev or claude for review",
            "should we use jev or gpt for this classifier?",
            "we use jev in production and latency doubled, why?",
            "the jev check in omh doctor is failing",
            "go through jev's pricing page and compare it with openrouter",
        ):
            with self.subTest(message=message):
                self.assertFalse(addresses_jev(message))

    def test_an_explicit_non_jev_invocation_wins(self) -> None:
        for message in (
            "use omh-code-review on this PR, and don't ask jev",
            "/omh-code-review this PR; we can ask jev later",
            "/omh-code-review this diff and ask jev too",
        ):
            with self.subTest(message=message):
                self.assertEqual(_skill(message), "code-review")

    def test_a_skill_name_counts_only_in_its_invocation_form(self) -> None:
        names = set(installable_skill_names())
        self.assertEqual(jev_addressed_skill("/omh-jev-review-gate this diff", names), "jev-review-gate")
        self.assertEqual(jev_addressed_skill("omh-jev-ask: is this clear", names), "jev-ask")
        self.assertEqual(jev_addressed_skill("use omh-jev-ask on this", names), "jev-ask")
        self.assertIsNone(jev_addressed_skill("fix the bug in omh-jev-review-gate", names))
        self.assertIsNone(jev_addressed_skill("review the omh-jev-action-check SKILL.md diff", names))

    def test_a_jev_skill_never_wins_on_score_alone(self) -> None:
        from omh.routing.recommend import recommend_skills

        for message in (
            "the jev question format docs are confusing",
            "review the omh-jev-action-check SKILL.md diff",
        ):
            with self.subTest(message=message):
                self.assertFalse(any(row["skill"].startswith("jev-") for row in recommend_skills(message)))

    def test_generic_words_do_not_pick_a_preset(self) -> None:
        # Free-form yes/no questions stay on jev-ask; a preset sends a
        # different `state` and applies a ladder nobody asked for.
        for message in (
            "ask jev whether the patch notes mention rollback",
            "ask jev if this PR description is clear",
            "ask jev whether this doc section is complete",
            "ask jev whether the command palette docs are clear",
        ):
            with self.subTest(message=message):
                self.assertEqual(_skill(message), "jev-ask")

    def test_a_skill_absent_from_the_catalog_is_never_returned(self) -> None:
        self.assertIsNone(jev_addressed_skill("ask jev whether this is fine", {"ask"}))

    def test_addressing_needs_an_agentive_phrase(self) -> None:
        self.assertTrue(addresses_jev("have jev look at this"))
        self.assertFalse(addresses_jev("jev is a typed-answer model"))
        self.assertFalse(addresses_jev("use jev as the default model"))

    def test_no_router_emitted_name_carries_the_third_party_prefix(self) -> None:
        # Critic C3: the next_action of every Jev skill reaches injected text.
        for skill in JEV_SKILL_NAMES:
            payload = build_chat_interaction_payload(f"use omh-{skill} on this", source="discord")
            with self.subTest(skill=skill):
                self.assertEqual(payload["route"].get("selected_skill"), skill)
                self.assertNotIn("jev_", str(payload.get("next_action")))
                self.assertNotIn("jev_", str(payload["chat_response"].get("kind")))


if __name__ == "__main__":
    unittest.main()

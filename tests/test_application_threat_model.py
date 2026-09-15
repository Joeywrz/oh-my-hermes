from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.awareness import awareness_route_hint
from omh.routing.chat import route_chat_message
from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates, builtin_skill_templates
from omh.wrapper.contract import build_chat_interaction_payload

SKILL = "application-threat-model"
SIBLING = "security-safety-review"
ARTIFACT = "application_threat_model/v1"
SIBLING_ARTIFACT = "threat_surface_map/v1"


def _definition(name: str):
    return next(definition for definition in builtin_definitions() if definition.name == name)


class ApplicationThreatModelCatalogTests(unittest.TestCase):
    def test_the_two_artifact_names_never_appear_in_each_others_contract(self) -> None:
        """The whole point of the skill: one request, two methods, two names that cannot be swapped.

        `security-safety-review` maps the AGENT's prompt/tool/credential surface
        into `threat_surface_map/v1`. This workflow models an APPLICATION. If
        either name leaked into the other's outputs the collision would be back.
        """

        mine = _definition(SKILL)
        sibling = _definition(SIBLING)

        self.assertIn(ARTIFACT, mine.expected_outputs)
        self.assertNotIn(SIBLING_ARTIFACT, mine.expected_outputs)
        self.assertIn(SIBLING_ARTIFACT, sibling.expected_outputs)
        self.assertNotIn(ARTIFACT, sibling.expected_outputs)
        for text in mine.expected_outputs + mine.artifact_expectations:
            self.assertNotIn("threat_surface", text)

    def test_the_outputs_name_the_application_model_not_an_agent_tool_inventory(self) -> None:
        outputs = " ".join(_definition(SKILL).expected_outputs).casefold()

        for required in ("asset", "trust boundar", "attack scenario", "control", "security test"):
            with self.subTest(required=required):
                self.assertIn(required, outputs)
        for agent_surface_word in ("prompt", "tool permission", "credential", "dependenc"):
            with self.subTest(agent_surface_word=agent_surface_word):
                self.assertNotIn(agent_surface_word, outputs)

    def test_the_agent_surface_boundary_is_declared_to_the_sibling_by_name(self) -> None:
        statements = [text for text in _definition(SKILL).do_not_use_when if f"`{SIBLING}`" in text]

        self.assertEqual(len(statements), 1)
        self.assertIn("prompts", statements[0])
        self.assertIn("credentials", statements[0])

    def test_the_control_decision_vocabulary_is_closed(self) -> None:
        body = " ".join(_definition(SKILL).quality_bar + _definition(SKILL).final_checklist)

        for decision in ("mitigate", "transfer", "accept", "eliminate"):
            with self.subTest(decision=decision):
                self.assertIn(decision, body)

    def test_the_method_detail_lives_in_the_reference_not_the_always_loaded_body(self) -> None:
        """Relocation, not compression: the ratchet measures the body only."""

        template = next(item for item in builtin_skill_templates() if item.name == SKILL)
        reference = next(
            item
            for item in builtin_skill_reference_templates()
            if item.skill_name == SKILL and item.relative_path == "references/threat-model-method.md"
        )

        self.assertIn("omh-application-threat-model/references/threat-model-method.md", template.content)
        for prompt in ("Spoofing", "Tampering", "Repudiation", "Elevation of privilege"):
            with self.subTest(prompt=prompt):
                self.assertIn(prompt, reference.content)
                self.assertNotIn(prompt, template.content)
        self.assertIn(SIBLING, reference.content)
        self.assertIn(SIBLING_ARTIFACT, reference.content)


class ApplicationThreatModelRoutingTests(unittest.TestCase):
    def test_application_threat_model_requests_dispatch_the_skill(self) -> None:
        for message in (
            "build a threat model for our payment service architecture",
            "threat model the checkout service before we launch",
            "how would an attacker get from the public API to the customer database",
            "list the attack scenarios for our file upload endpoint",
            "walk through the abuse cases for our signup flow",
            "map the trust boundaries and abuse cases for the tenant isolation",
        ):
            with self.subTest(message=message):
                route = route_chat_message(message, source="discord")
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], SKILL)

    def test_the_agent_runtime_surface_stays_with_the_safety_review(self) -> None:
        """The collision, pinned in the direction it used to fail."""

        for message in (
            "review the prompt injection and tool permission risks in this agent before we run it",
            "agent safety review before we give it shell access",
            "secret exposure review for this coding agent",
        ):
            with self.subTest(message=message):
                route = route_chat_message(message, source="discord")
                self.assertEqual(route["selected_skill"], SIBLING)
                self.assertNotIn(SKILL, [rec["skill"] for rec in route["recommendations"][:1]])

    def test_the_awareness_hint_keeps_the_agent_surface_with_the_safety_review(self) -> None:
        """The hint rule sits after the sibling's, so a shared phrase is the sibling's."""

        self.assertEqual(
            awareness_route_hint("security safety review of the threat model for this agent")["primary_workflow"],
            SIBLING,
        )
        self.assertEqual(
            awareness_route_hint("build a threat model for our payment service architecture")["primary_workflow"],
            SKILL,
        )

    def test_generic_words_in_another_sense_never_reach_the_skill(self) -> None:
        # "threat", "model", "stride", "trust", "boundary", "attack", "case",
        # "security", "design", "review" and "architecture" each mean something
        # else somewhere; the intent lives in the complete phrases only.
        for message in (
            "what is threat modeling?",
            "explain what a stride analysis is",
            "what is a trust boundary in domain-driven design?",
            "he hit his stride in the second half",
            "model the churn with a logistic regression",
            "she built a scale model of the bridge",
            "which model is cheapest right now",
            "what is a tui and how is it different from a gui?",
        ):
            with self.subTest(message=message):
                route = route_chat_message(message, source="discord")
                self.assertNotEqual(route["selected_skill"], SKILL)
                self.assertNotIn(SKILL, [rec["skill"] for rec in route["recommendations"][:1]])

    def test_the_method_named_in_another_sense_does_not_even_hint_the_workflow(self) -> None:
        # The awareness hint is non-binding guidance, so it may still name the
        # workflow for a question ABOUT threat modeling. These six do not use
        # the method's name at all -- they reuse its words -- so a hint here
        # would be the router's mistake leaking into Hermes context.
        for message in (
            "he hit his stride in the second half",
            "model the churn with a logistic regression",
            "she built a scale model of the bridge",
            "which model is cheapest right now",
            "what is a tui and how is it different from a gui?",
            "the biggest threat to our launch date is the vendor contract",
        ):
            with self.subTest(message=message):
                self.assertNotEqual(awareness_route_hint(message)["primary_workflow"], SKILL)

    def test_neighbouring_review_lanes_keep_their_requests(self) -> None:
        for message, expected in (
            ("can you review this architecture doc", "code-review"),
            ("design review for the checkout page", "code-review"),
            ("the biggest threat to our launch date is the vendor contract", "research-brief"),
        ):
            with self.subTest(message=message):
                self.assertEqual(route_chat_message(message, source="discord")["selected_skill"], expected)


class ApplicationThreatModelChatCardTests(unittest.TestCase):
    def test_the_chat_card_states_the_artifact_and_its_claim_boundary(self) -> None:
        interaction = build_chat_interaction_payload(
            "build a threat model for our payment service architecture", source="discord"
        )
        response = interaction["chat_response"]

        self.assertEqual(response["kind"], "application_threat_model")
        self.assertEqual(interaction["next_action"], "prepare_application_threat_model")
        self.assertEqual(response["state"]["artifact_schema"], ARTIFACT)
        claim_boundary = response["claim_boundary"]
        for absent_evidence in ("scan", "penetration test", "compliance attestation", "deployed"):
            with self.subTest(absent_evidence=absent_evidence):
                self.assertIn(absent_evidence, claim_boundary)
        self.assertEqual(
            [action["id"] for action in response["actions"]],
            [
                "prepare_application_threat_model",
                "show_attack_scenarios",
                "show_control_tests",
                "show_status",
            ],
        )


if __name__ == "__main__":
    unittest.main()

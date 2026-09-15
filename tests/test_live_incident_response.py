from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.awareness import awareness_route_hint
from omh.routing.chat import route_chat_message
from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates, builtin_skill_templates
from omh.wrapper.contract import build_chat_interaction_payload

SKILL = "live-incident-response"
RETROSPECTIVE_SIBLING = "reliability-review"
SUPPORT_SIBLING = "support-operations"
RELEASE_SIBLING = "deploy-and-monitor"
CONNECTOR_SIBLING = "connector-operator"
ARTIFACT = "live_incident_record/v1"
REFERENCE_PATH = "references/incident-command-method.md"


def _definition(name: str):
    return next(definition for definition in builtin_definitions() if definition.name == name)


class LiveIncidentCatalogTests(unittest.TestCase):
    def test_the_open_incident_is_owned_and_the_closed_one_is_handed_back(self) -> None:
        """The misroute in #1563, pinned from both ends.

        `support-operations` used to route an active incident to
        `reliability-review`, which reviews incident notes after the fact. This
        workflow owns the open incident and hands the closed one back by name,
        and both siblings say the same thing in the other direction.
        """

        mine = _definition(SKILL)
        retrospective = " ".join(_definition(RETROSPECTIVE_SIBLING).do_not_use_when)
        support = " ".join(_definition(SUPPORT_SIBLING).do_not_use_when)
        release = " ".join(_definition(RELEASE_SIBLING).do_not_use_when)

        self.assertIn(ARTIFACT, mine.expected_outputs)
        for sibling in (RETROSPECTIVE_SIBLING, SUPPORT_SIBLING, RELEASE_SIBLING, CONNECTOR_SIBLING):
            with self.subTest(sibling=sibling):
                statements = [text for text in mine.do_not_use_when if f"`{sibling}`" in text]
                self.assertEqual(len(statements), 1)
        for text in (retrospective, support, release):
            with self.subTest(text=text[:40]):
                self.assertIn(f"`{SKILL}`", text)

    def test_the_support_lane_no_longer_sends_an_open_incident_to_the_postmortem(self) -> None:
        """The exact line the issue names: one statement covering both states sent both to the review."""

        statements = _definition(SUPPORT_SIBLING).do_not_use_when
        live = [text for text in statements if f"`{SKILL}`" in text]
        retrospective = [text for text in statements if f"`{RETROSPECTIVE_SIBLING}`" in text]

        self.assertEqual(len(live), 1)
        self.assertEqual(len(retrospective), 1)
        self.assertIn("still open", live[0])
        self.assertNotIn("active", retrospective[0])

    def test_the_timeline_is_append_only_and_a_correction_appends(self) -> None:
        mine = _definition(SKILL)
        rules = " ".join(mine.safety_rules + mine.quality_bar + mine.final_checklist + mine.artifact_expectations)

        self.assertIn("append-only", " ".join(mine.expected_outputs))
        self.assertIn("Never rewrite or delete a timeline entry", " ".join(mine.safety_rules))
        self.assertIn("A correction is a new entry", rules)
        self.assertIn("never edits one", rules)

    def test_external_effects_stay_prepared_until_the_connector_returns_a_result(self) -> None:
        mine = _definition(SKILL)
        contract = " ".join(
            mine.safety_rules + mine.expected_outputs + mine.artifact_expectations + mine.final_checklist
        ) + mine.handoff_policy

        self.assertIn(f"`{CONNECTOR_SIBLING}`", contract)
        for effect in ("page", "status-page", "customer"):
            with self.subTest(effect=effect):
                self.assertIn(effect, contract)
        self.assertIn("observed only when the connector returns a result", contract)
        self.assertIn("prepared", contract)

    def test_recovery_is_an_observation_of_a_named_signal_not_an_applied_mitigation(self) -> None:
        mine = _definition(SKILL)
        contract = " ".join(mine.safety_rules + mine.quality_bar + mine.final_checklist)

        self.assertIn("Do not call the incident recovered because a mitigation was applied", contract)
        self.assertIn("healthy value", contract)
        self.assertIn("never the mitigation alone", contract)

    def test_a_temporary_mitigation_names_what_removes_it(self) -> None:
        mine = _definition(SKILL)
        contract = " ".join(mine.safety_rules + mine.expected_outputs + mine.final_checklist)

        self.assertIn("temporary", contract)
        self.assertIn("what removes it", contract)

    def test_the_method_detail_lives_in_the_reference_not_the_always_loaded_body(self) -> None:
        """Relocation, not compression: the body ratchet measures the body only."""

        template = next(item for item in builtin_skill_templates() if item.name == SKILL)
        reference = next(
            item
            for item in builtin_skill_reference_templates()
            if item.skill_name == SKILL and item.relative_path == REFERENCE_PATH
        )

        self.assertIn(f"omh-{SKILL}/{REFERENCE_PATH}", template.content)
        for detail in ("SEV1", "SEV2", "SEV3", "Scribe", "Handing over", "Communication ledger"):
            with self.subTest(detail=detail):
                self.assertIn(detail, reference.content)
                self.assertNotIn(detail, template.content)
        self.assertIn(RETROSPECTIVE_SIBLING, reference.content)


class LiveIncidentRoutingTests(unittest.TestCase):
    def test_an_incident_still_open_dispatches_the_live_lane(self) -> None:
        for message in (
            "we have a production outage right now, declare severity and assign an incident commander",
            "we are in an active incident, who is the incident commander and what is the severity",
            "the checkout service is down right now, start the incident timeline",
            "we applied a temporary mitigation to stop the bleeding, record it and verify recovery",
            "declare a sev1 and open the incident bridge",
            "page the on-call and start the incident timeline",
        ):
            with self.subTest(message=message):
                route = route_chat_message(message, source="discord")
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], SKILL)

    def test_a_closed_incident_stays_with_the_retrospective_lane(self) -> None:
        """The collision, pinned in the direction it used to fail."""

        for message in (
            "review the incident notes and the postmortem for last week's outage",
            "what did the error budget do last month",
            "write the incident postmortem and the remediation follow-ups",
        ):
            with self.subTest(message=message):
                route = route_chat_message(message, source="discord")
                self.assertEqual(route["selected_skill"], RETROSPECTIVE_SIBLING)
                self.assertNotIn(SKILL, [rec["skill"] for rec in route["recommendations"][:1]])

    def test_the_support_case_and_the_release_watch_keep_their_lanes(self) -> None:
        for message, expected in (
            (
                "draft a calm reply for this login-outage customer and tell me whether it needs an engineering escalation",
                SUPPORT_SIBLING,
            ),
            ("the deploy is healthy, monitor the error rate for an hour", RELEASE_SIBLING),
            ("is this release ready for production", "production-audit"),
        ):
            with self.subTest(message=message):
                self.assertEqual(route_chat_message(message, source="discord")["selected_skill"], expected)

    def test_generic_words_in_another_sense_never_reach_the_skill(self) -> None:
        # "incident", "response", "commander", "severity", "outage",
        # "production", "down", "timeline", "war", "room" each mean something
        # else somewhere; the intent lives in the complete phrases only.
        for message in (
            "what does an incident commander do during a wildfire?",
            "explain what an incident response plan is",
            "what is a war room in project management?",
            "he was promoted to commander last year",
            "explain severity vs priority in jira",
            "our production line is down for maintenance this week",
            "the outage of creativity in this team is the real problem",
            "start a war room for the quarterly planning offsite",
            "how do i declare a constant in rust",
        ):
            with self.subTest(message=message):
                route = route_chat_message(message, source="discord")
                self.assertNotEqual(route["selected_skill"], SKILL)
                self.assertNotIn(SKILL, [rec["skill"] for rec in route["recommendations"][:1]])

    def test_the_awareness_hint_splits_the_open_incident_from_the_review(self) -> None:
        """The hint rule sits before the retrospective one, so a live phrase is the live lane's."""

        self.assertEqual(
            awareness_route_hint("we have a production outage right now, declare severity")["primary_workflow"],
            SKILL,
        )
        self.assertEqual(
            awareness_route_hint("incident postmortem and slo error budget review")["primary_workflow"],
            RETROSPECTIVE_SIBLING,
        )


class LiveIncidentChatCardTests(unittest.TestCase):
    def test_the_chat_card_states_the_artifact_and_its_claim_boundary(self) -> None:
        interaction = build_chat_interaction_payload(
            "we have a production outage right now, declare severity and assign an incident commander",
            source="discord",
        )
        response = interaction["chat_response"]

        self.assertEqual(response["kind"], "live_incident_record")
        self.assertEqual(interaction["next_action"], "prepare_live_incident_record")
        self.assertEqual(response["state"]["artifact_schema"], ARTIFACT)
        claim_boundary = response["claim_boundary"]
        for absent_evidence in ("Paging", "status-page", "customer sends", CONNECTOR_SIBLING):
            with self.subTest(absent_evidence=absent_evidence):
                self.assertIn(absent_evidence, claim_boundary)
        self.assertEqual(
            [action["id"] for action in response["actions"]],
            [
                "prepare_live_incident_record",
                "show_incident_timeline",
                "show_recovery_verification",
                "show_status",
            ],
        )


if __name__ == "__main__":
    unittest.main()

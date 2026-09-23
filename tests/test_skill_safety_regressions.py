"""Source and rendered guidance contracts, not claims of live agent compliance."""
from __future__ import annotations

import unittest
from dataclasses import asdict

from _local_package import load_local_package

load_local_package()

from omh.coding.fanout import build_fanout_contract  # noqa: E402
from omh.coding.fanout_contracts import FanoutContractError  # noqa: E402
from omh.coding.maestro import ExternalHandoffRequest, build_external_handoff  # noqa: E402
from omh.skills.catalog import builtin_definitions  # noqa: E402
from omh.skills.packaging import builtin_skill_templates  # noqa: E402


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _strings(item)


class SkillSafetyRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definitions = {item.name: item for item in builtin_definitions()}
        cls.templates = {item.name: item.content for item in builtin_skill_templates()}

    def surfaces(self, name):
        return {
            "source_contract": "\n".join(_strings(asdict(self.definitions[name]))),
            "rendered_template": self.templates[name],
        }.items()

    def test_setup_uses_secure_entry_and_redacted_approval(self):
        for name in ("parallel-tools", "websearch-setup", "morning-brief"):
            for surface, content in self.surfaces(name):
                with self.subTest(skill=name, surface=surface):
                    self.assertIn("Never ask the user to paste secrets into chat", content)
                    self.assertIn("Hermes-native secure entry", content)
                    self.assertIn("user-side OAuth", content)
                    self.assertIn("redacted placeholders", content)
                    self.assertIn("explicitly approves", content)
                    self.assertIn("key names and presence only", content)

    def test_setup_allows_authorized_storage_without_retention_promises(self):
        for name in ("parallel-tools", "websearch-setup", "morning-brief"):
            for surface, content in self.surfaces(name):
                with self.subTest(skill=name, surface=surface):
                    self.assertIn("user-authorized credential store or local configuration", content)
                    self.assertIn("Do not promise chat or platform non-retention", content)
                    for forbidden in (
                        "pasted by the user directly in chat", "never stored",
                        "credentials unstored", "If a pasted token fails",
                    ):
                        self.assertNotIn(forbidden, content)
        morning = self.definitions["morning-brief"]
        self.assertIn("secure entry", " ".join(morning.required_inputs))
        self.assertIn("secure entry", " ".join(morning.recovery_notes))
        self.assertIn("never enable Send permission", " ".join(morning.safety_rules))

    def test_websearch_preserves_two_independent_approvals(self):
        for surface, content in self.surfaces("websearch-setup"):
            with self.subTest(surface=surface):
                self.assertIn("each gets its own diff and its own approval", content)
                self.assertIn("a second, separate diff approval", content)
                self.assertIn("keep the other step's state independent", content)
        # The already-safe model setup remains stricter: it never edits secrets.
        for surface, content in self.surfaces("model-setup"):
            with self.subTest(surface=surface):
                self.assertIn("never edit dotenv files or credential material", content)
                self.assertIn("never ask them to paste secrets into chat", content)

    def test_maestro_owner_selection_does_not_authorize_dispatch(self):
        for surface, content in self.surfaces("maestro"):
            with self.subTest(surface=surface):
                self.assertIn("Owner selection alone is not dispatch permission", content)
                self.assertIn("prepare a Codex handoff only", content)
                self.assertIn("do not dispatch", content)
                self.assertNotIn("naming message is itself the operator's dispatch opt-in", content)
                self.assertIn("Prepared, composed, or shown is never dispatch", content)

    def test_maestro_explicit_implementation_can_authorize_owner_and_action(self):
        for surface, content in self.surfaces("maestro"):
            with self.subTest(surface=surface):
                self.assertIn("Use Codex to implement this now", content)
                self.assertIn("both the owner choice and dispatch permission", content)
                self.assertIn("no redundant confirmation", content)
                self.assertIn("readiness and permission probes", content)
                self.assertIn("explicit user dispatch command", content)
                self.assertIn("prompt-only", content)
                self.assertIn("fanout-dispatch bridge", content)

    def test_maestro_preparation_is_metadata_even_for_implementation_requests(self):
        for profile in ("codex", "claude-code"):
            for message in (
                f"Use {profile}; prepare a handoff only, do not dispatch.",
                f"Use {profile} to implement this now.",
            ):
                with self.subTest(profile=profile, message=message):
                    prepared = build_external_handoff(
                        ExternalHandoffRequest(message=message, source="discord", profile=profile)
                    )
                    self.assertFalse(prepared.capability.executes_work)
                    self.assertEqual(prepared.capability.observation_boundary, "prepared_not_observed")
                    self.assertIs(prepared.capability.dispatchable, profile == "codex")

    def test_ultrawork_excludes_conflicting_parallel_writers_not_single_owner(self):
        for surface, content in self.surfaces("ultrawork"):
            with self.subTest(surface=surface):
                self.assertNotIn("The work touches the same files or invariants in ways that need one owner.", content)
                self.assertIn("conflicting parallel writers", content)
                self.assertIn("use single-owner or ordered execution", content)
                self.assertIn("a shared file requires an ordering edge or one owner", content)
                self.assertIn("one bounded edit that is explicitly low-risk", content)
                self.assertIn("use one direct owner", content)

    def test_fanout_supports_single_owner_and_ordered_shared_file_not_parallel_conflicts(self):
        cases = (
            [{"unit_id": "owner", "file_scope": ["src/shared.py"]}],
            [
                {"unit_id": "first", "file_scope": ["src/shared.py"]},
                {"unit_id": "second", "file_scope": ["src/shared.py"], "depends_on": ["first"]},
            ],
        )
        for units in cases:
            with self.subTest(units=units):
                contract = build_fanout_contract("Implement the accepted shared-invariant change", units)
                merge_plan = contract["merge_plan"]
                assert isinstance(merge_plan, dict)
                self.assertEqual(merge_plan["merge_order"], [unit["unit_id"] for unit in units])
        with self.assertRaises(FanoutContractError):
            build_fanout_contract("Do not run conflicting parallel writers", [
                {"unit_id": "first", "file_scope": ["src/shared.py"]},
                {"unit_id": "second", "file_scope": ["src/shared.py"]},
            ])

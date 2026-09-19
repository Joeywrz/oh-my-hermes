"""Retirement contract for the three strict-subset skills folded into a sibling (#1691).

`performance-goal` (into `ultraperf`), `best-practice-research` (into
`web-research`), and `autoresearch-goal` (into `research`) left the
installable/routable surface. They use the same `SurfaceExposure` retirement
shape as the four ULW engines, so this file asserts the same guarantees the
engine contract asserts, plus the one thing that differs: the cue vocabulary
migrates by folding each retired trigger into the target home's own trigger
table rather than through `routing/ulw_alias.py`, so the cues compete in
ordinary catalog scoring instead of pre-empting it. What that buys is stated
in three parts below -- every legacy cue still dispatches, the cues the
retired contract actually won now reach the target home, and the two bare
metric nouns another workflow already owned keep that owner.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from omh.core.errors import OmhError
from omh.install.installer import install_skill_pack
from omh.maintenance.doctor import _retired_skill_install_check
from omh.skills.catalog import (
    ULW_RETIRED_CAPABILITIES,
    builtin_definitions,
    installable_skill_names,
    retired_display_names,
    retired_skill_migration_error,
    retired_skill_names,
    retired_ulw_engine_names,
    surface_exposure_for_skill,
    workflow_reference_definitions,
)
from omh.skills.catalog_types import omh_skill_display_name
from omh.wrapper.contract import build_chat_interaction_payload

# retired canonical name -> target home canonical name.
RETIRED_INTO = {
    "performance-goal": "ultraperf",
    "best-practice-research": "web-research",
    "autoresearch-goal": "research",
}
REPO_ROOT = Path(__file__).parent.parent

# Legacy cues whose dispatch owner was already another workflow before the
# fold, measured on `origin/main`. Pinned in `FoldedCueTests` below.
PRE_EXISTING_INCUMBENTS = {
    "latency": "ops-observability-card",
    "throughput": "agent-ops-review",
}


def _definitions() -> dict[str, object]:
    return {definition.name: definition for definition in builtin_definitions()}


class RetiredSurfaceTests(unittest.TestCase):
    def test_the_three_left_the_installable_surface(self) -> None:
        self.assertEqual(set(installable_skill_names()) & set(RETIRED_INTO), set())

    def test_every_target_home_is_still_installable(self) -> None:
        installable = set(installable_skill_names())
        for retired, target in RETIRED_INTO.items():
            with self.subTest(retired=retired):
                self.assertIn(target, installable)

    def test_retired_skill_directories_are_deleted_from_both_trees(self) -> None:
        for name in RETIRED_INTO:
            label = omh_skill_display_name(name)
            for tree in ("skills", "agent-skills"):
                with self.subTest(label=label, tree=tree):
                    self.assertFalse((REPO_ROOT / tree / label).exists())

    def test_retired_contracts_survive_as_workflow_references(self) -> None:
        """Retirement is an exposure change, not a deletion."""
        reference_names = {definition.name for definition in workflow_reference_definitions()}
        for name in RETIRED_INTO:
            with self.subTest(contract=name):
                self.assertIn(name, reference_names)

    def test_each_exposure_row_carries_the_retirement_shape(self) -> None:
        for name, target in RETIRED_INTO.items():
            with self.subTest(contract=name):
                exposure = surface_exposure_for_skill(name)
                self.assertEqual(exposure.lifecycle_stage, "retired")
                self.assertEqual(exposure.projections, ("workflow_reference",))
                self.assertFalse(exposure.install_visibility)
                self.assertTrue(exposure.compatibility_alias)
                self.assertEqual(exposure.target_home, target)
                self.assertEqual(exposure.migration_release, "2.0.4")
                self.assertIn(omh_skill_display_name(target), exposure.preferred_usage)


class RetiredNameSetTests(unittest.TestCase):
    """The general retired set and the ULW engine set stay separate producers."""

    def test_the_general_set_covers_engines_and_non_engines(self) -> None:
        names = set(retired_skill_names())
        self.assertTrue(set(RETIRED_INTO) <= names)
        self.assertTrue(set(retired_ulw_engine_names()) <= names)

    def test_the_ulw_engine_set_stays_engine_only(self) -> None:
        """`retired_ulw_engine_names()` feeds the engine inventory surfaces and
        is pinned against `ULW_RETIRED_CAPABILITIES`; widening it to non-engine
        rows would break that equality and put non-engines in the README and
        site ULW regions."""
        engines = set(retired_ulw_engine_names())
        self.assertEqual(engines, set(ULW_RETIRED_CAPABILITIES))
        self.assertEqual(engines & set(RETIRED_INTO), set())


class MigrationErrorTests(unittest.TestCase):
    def test_labels_resolve_for_the_canonical_and_display_name(self) -> None:
        labels = retired_display_names()
        for name in RETIRED_INTO:
            for label in (name, omh_skill_display_name(name)):
                with self.subTest(label=label):
                    self.assertEqual(labels.get(label), name)

    def test_the_error_names_the_target_home_and_carries_no_capability(self) -> None:
        for name, target in RETIRED_INTO.items():
            with self.subTest(contract=name):
                error = retired_skill_migration_error(omh_skill_display_name(name))
                self.assertEqual(error["error"], "retired_skill")
                self.assertEqual(error["retired_contract_id"], name)
                self.assertEqual(error["target_contract_id"], target)
                self.assertEqual(error["target_display_name"], omh_skill_display_name(target))
                self.assertIn(omh_skill_display_name(target), error["message"])
                # No capability id: the intent moved to a whole skill, and a
                # made-up capability name would not resolve in any table.
                self.assertNotIn("selected_capability", error)
                self.assertNotIn("deprecat", error["message"].lower())

    def test_a_retired_tap_path_resolves_the_same_error(self) -> None:
        error = retired_skill_migration_error(
            "rlaope/oh-my-hermes/skills/omh-best-practice-research"
        )
        self.assertEqual(error.get("retired_contract_id"), "best-practice-research")

    def test_a_target_home_label_is_not_a_migration_error(self) -> None:
        for target in RETIRED_INTO.values():
            with self.subTest(target=target):
                self.assertEqual(retired_skill_migration_error(target), {})
                self.assertEqual(
                    retired_skill_migration_error(omh_skill_display_name(target)), {}
                )

    def test_the_installer_refuses_a_retired_tap_checkout(self) -> None:
        from omh.system.paths import OmhPaths

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "tap" / "skills" / "omh-performance-goal"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text(
                "---\nname: omh-performance-goal\ndescription: retired\n---\n\nlegacy\n",
                encoding="utf-8",
            )
            paths = OmhPaths(omh_home=Path(tmp) / "home", hermes_home=Path(tmp) / "hermes")
            with self.assertRaises(OmhError) as caught:
                install_skill_pack(paths, source="dir", source_dir=Path(tmp) / "tap")
            self.assertIn("retired", str(caught.exception))
            self.assertIn("ulw-perf", str(caught.exception))

    def test_doctor_flags_a_leftover_non_engine_retired_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            leftover = skills_dir / "research" / "omh-best-practice-research"
            leftover.mkdir(parents=True)
            (leftover / "SKILL.md").write_text(
                "---\nname: omh-best-practice-research\n---\n", encoding="utf-8"
            )
            check = _retired_skill_install_check(SimpleNamespace(skills_dir=skills_dir))
            self.assertFalse(check.ok)
            self.assertIn("omh-best-practice-research", check.message)
            self.assertIn("omh-web-research", check.message)
            self.assertIn("omh update", check.next_action)


class FoldedCueTests(unittest.TestCase):
    """Nothing a person types stops working: every legacy cue dispatches the target home."""

    def _legacy_cues(self, name: str) -> tuple[str, ...]:
        """The cue vocabulary the retired contract answered to.

        Read off the surviving `SkillDefinition` rather than transcribed, so a
        trigger added to a retired contract later is covered automatically --
        the same derivation `ulw_alias_corpus()` uses for the engines.
        """
        definition = _definitions()[name]
        cues = {
            *definition.triggers,
            *definition.aliases,
            name,
            omh_skill_display_name(name),
        }
        return tuple(sorted(cues))

    def test_every_legacy_cue_still_dispatches(self) -> None:
        """The promise the retirement makes, stated at its widest: no cue the
        retired contracts answered to loses its dispatch."""
        for name in RETIRED_INTO:
            for cue in self._legacy_cues(name):
                with self.subTest(retired=name, cue=cue):
                    route = build_chat_interaction_payload(cue, source="generic")["route"]
                    self.assertEqual(route.get("action"), "dispatch", cue)
                    self.assertTrue(route.get("selected_skill"), cue)

    def test_cues_the_retired_contract_owned_now_reach_the_target_home(self) -> None:
        for name, target in RETIRED_INTO.items():
            for cue in self._legacy_cues(name):
                if cue in PRE_EXISTING_INCUMBENTS:
                    continue
                with self.subTest(retired=name, cue=cue):
                    route = build_chat_interaction_payload(cue, source="generic")["route"]
                    self.assertEqual(route.get("selected_skill"), target, cue)

    def test_cues_another_workflow_already_owned_keep_that_owner(self) -> None:
        """Two of `performance-goal`'s bare metric nouns never dispatched it.

        Measured on `origin/main` before the fold, `latency` reached
        `ops-observability-card` and `throughput` reached `agent-ops-review`;
        both still do. They are listed so the fold cannot quietly take a word
        it did not own, and so the wider guarantee above is not read as a
        claim that every cue moved."""
        for cue, owner in PRE_EXISTING_INCUMBENTS.items():
            with self.subTest(cue=cue):
                route = build_chat_interaction_payload(cue, source="generic")["route"]
                self.assertEqual(route.get("selected_skill"), owner, cue)

    def test_no_legacy_cue_still_dispatches_the_retired_contract(self) -> None:
        retired = set(RETIRED_INTO)
        for name in RETIRED_INTO:
            for cue in self._legacy_cues(name):
                with self.subTest(retired=name, cue=cue):
                    route = build_chat_interaction_payload(cue, source="generic")["route"]
                    self.assertNotIn(route.get("selected_skill"), retired, cue)

    def test_each_target_home_carries_the_folded_triggers(self) -> None:
        definitions = _definitions()
        for name, target in RETIRED_INTO.items():
            retired_triggers = set(definitions[name].triggers)
            target_triggers = set(definitions[target].triggers)
            with self.subTest(retired=name):
                self.assertTrue(
                    retired_triggers <= target_triggers,
                    sorted(retired_triggers - target_triggers),
                )


class PerContractRollbackTests(unittest.TestCase):
    def test_per_contract_rollback_restores_routing(self) -> None:
        """Rollback is a one-row edit, exercised as data rather than asserted:
        flip ONE retired row back to the canonical shape and prove routing and
        installability restore while the other two stay retired; then restore
        the retired row and prove it retires again."""
        import dataclasses

        from omh.routing import chat as chat_module
        from omh.skills import catalog as catalog_module

        def _clear_caches() -> None:
            catalog_module._surface_exposure_by_name.cache_clear()
            catalog_module._projected_definitions_cached.cache_clear()
            chat_module._canonical_skill_by_display_name.cache_clear()
            chat_module._route_chat_message_cached.cache_clear()
            chat_module._public_chat_route_payload_cached.cache_clear()

        original = catalog_module._SURFACE_EXPOSURES
        try:
            rows = []
            for exposure in original:
                if exposure.name == "best-practice-research":
                    rows.append(
                        dataclasses.replace(
                            exposure,
                            projections=(
                                "routable",
                                "installable",
                                "workflow_reference",
                                "capability",
                            ),
                            install_visibility=True,
                            docs_visibility="primary_workflow_skill",
                            compatibility_alias=False,
                            lifecycle_stage="canonical",
                            target_home=None,
                            migration_release=None,
                        )
                    )
                else:
                    rows.append(exposure)
            catalog_module._SURFACE_EXPOSURES = tuple(rows)
            _clear_caches()

            self.assertIn("best-practice-research", catalog_module.installable_skill_names())
            mapping = chat_module._canonical_skill_by_display_name()
            self.assertEqual(
                mapping.get("omh-best-practice-research"), "best-practice-research"
            )
            self.assertEqual(
                set(catalog_module.retired_skill_names()) & set(RETIRED_INTO),
                {"performance-goal", "autoresearch-goal"},
            )
        finally:
            catalog_module._SURFACE_EXPOSURES = original
            _clear_caches()

        self.assertNotIn("best-practice-research", installable_skill_names())
        self.assertTrue(set(RETIRED_INTO) <= set(retired_skill_names()))


if __name__ == "__main__":
    unittest.main()

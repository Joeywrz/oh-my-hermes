"""Retirement contract for the four folded ULW engines (#954 stage 5, window=0).

`team`, `ultraprocess`, `ralph`, and `ultragoal` left the installable/routable
surface; their cue vocabulary routes permanently to `ultrawork`'s matching
capability with the original invocation preserved in diagnostics; the
Codex-named cue cluster resolves through owner selection (plan Q9); retired
labels and tap paths fail with explicit migration errors; doctor diagnoses
stale bundles; and per-contract rollback is exercised as data, not asserted
(P4).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from omh.coding.orchestration_vocabulary import HERMES_HARNESS_DEFAULT_WORDING
from omh.install.installer import install_skill_pack
from omh.core.errors import OmhError
from omh.maintenance.doctor import (
    _plugin_ulw_lifecycle_check,
    _retired_skill_install_check,
)
from omh.quality.ulw_alias_corpus import (
    expected_ulw_alias_corpus_size,
    ulw_alias_corpus,
    ulw_alias_corpus_report,
)
from omh.quality.ulw_equivalence import CONTRACT_EQUIVALENCE_CASES
from omh.routing.ulw_alias import (
    CODEX_OWNER_CHOICE_CUES,
    resolve_ulw_alias,
    ulw_alias_capability_reason,
)
from omh.skills.catalog import (
    ULW_RETIRED_CAPABILITIES,
    installable_skill_names,
    retired_display_names,
    retired_skill_migration_error,
    retired_ulw_engine_names,
    ulw_inventory_payload,
    workflow_reference_definitions,
)
from omh.skills.catalog_types import ULW_ENGINE_SKILL_NAMES
from omh.wrapper.contract import build_chat_interaction_payload

RETIRED = ("team", "ultraprocess", "ralph", "ultragoal")
FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).parent.parent


class RetiredSurfaceTests(unittest.TestCase):
    def test_retired_engines_left_the_installable_surface(self) -> None:
        self.assertEqual(set(installable_skill_names()) & set(RETIRED), set())

    def test_retired_skill_directories_are_deleted(self) -> None:
        for label in ("ulw-team", "ulw-ralph", "ulw-goal", "ulw-process"):
            with self.subTest(label=label):
                self.assertFalse((REPO_ROOT / "skills" / label).exists())

    def test_retired_contracts_survive_as_workflow_references(self) -> None:
        """P2: retirement is an exposure change, not a deletion."""
        reference_names = {definition.name for definition in workflow_reference_definitions()}
        for name in RETIRED:
            with self.subTest(contract=name):
                self.assertIn(name, reference_names)

    def test_engine_name_set_keeps_all_thirteen_for_display_prefixing(self) -> None:
        self.assertEqual(len(ULW_ENGINE_SKILL_NAMES), 13)
        self.assertTrue(set(RETIRED) <= set(ULW_ENGINE_SKILL_NAMES))

    def test_capability_table_matches_the_equivalence_cases(self) -> None:
        """One capability mapping, two declared homes: the catalog table and
        the equivalence case table cannot disagree."""
        from_cases = {
            case.contract_id: case.target_capability for case in CONTRACT_EQUIVALENCE_CASES
        }
        self.assertEqual(ULW_RETIRED_CAPABILITIES, from_cases)
        self.assertEqual(set(retired_ulw_engine_names()), set(ULW_RETIRED_CAPABILITIES))


class AliasRoutingTests(unittest.TestCase):
    def test_alias_corpus_fully_resolves(self) -> None:
        baseline_payload = json.loads(
            (FIXTURES / "ulw_alias_baseline.json").read_text(encoding="utf-8")
        )
        report = ulw_alias_corpus_report(baseline=baseline_payload["cues"])
        self.assertEqual(report["unresolved_count"], 0, report["unresolved"])
        self.assertEqual(report["semantic_change_count"], 0, report["semantic_changes"])
        self.assertTrue(report["baseline_checked"])

    def test_the_baseline_cue_order_is_the_sorted_corpus_order(self) -> None:
        """The 732-line baseline is only readable while its keys stay sorted.

        `ulw_alias_corpus_report(baseline=...)` compares dicts, so key order
        cannot fail it, and the size test below compares sets. The order is
        held instead by one `sorted()` inside `ulw_alias_corpus()` -- over a
        set, so dropping it makes the cue order hash-dependent, leaves every
        existing assertion green, and turns the next re-capture into a
        full-file rewrite with the moved cue buried in it."""
        cues = list(json.loads(
            (FIXTURES / "ulw_alias_baseline.json").read_text(encoding="utf-8")
        )["cues"])
        self.assertEqual(cues, sorted(cues), "re-capture the baseline with sorted cue keys")
        self.assertEqual(
            cues,
            list(ulw_alias_corpus()),
            "ulw_alias_corpus() must stay sorted -- it is what orders the baseline",
        )

    def test_corpus_size_is_catalog_derived_and_set_deduplicated(self) -> None:
        """Plan §9.6: 84 cues (74 triggers with `ulr`/`ulg`/`ulp` deduped by
        set semantics + 6 historical labels + 4 display labels), 23 Korean.
        The independent recomputation below is the mutant guard: a builder
        that drops one Korean trigger no longer equals the catalog union."""
        from omh.skills.catalog import retired_ulw_engine_definitions
        from omh.skills.catalog_types import (
            historical_skill_display_names,
            omh_skill_display_name,
        )

        independent: set[str] = set()
        for definition in retired_ulw_engine_definitions():
            independent.update(definition.triggers)
            independent.update(definition.aliases)
            independent.add(omh_skill_display_name(definition.name))
            independent.update(historical_skill_display_names(definition.name))
        corpus = ulw_alias_corpus()
        self.assertEqual(set(corpus), independent)
        self.assertEqual(len(corpus), expected_ulw_alias_corpus_size())
        report = ulw_alias_corpus_report()
        self.assertEqual(report["corpus_size"], 84)
        self.assertEqual(report["korean_cue_count"], 23)
        # Mutant: a corpus missing one Korean cue fails the size assertion.
        korean_cue = next(
            cue for cue in corpus if any("가" <= character <= "힣" for character in cue)
        )
        self.assertNotEqual(len(set(corpus) - {korean_cue}), expected_ulw_alias_corpus_size())

    def test_every_alias_route_carries_diagnostics_and_denies_execution(self) -> None:
        """Per alias (plan §11.3 PR E, Q5): `original_invocation` is the cue
        verbatim, the target contract is non-empty, `capability_reason` is one
        non-empty line, the status card shows the reason line, and
        `chat_response.claim_boundary` denies execution."""
        codex = set(CODEX_OWNER_CHOICE_CUES)
        report = ulw_alias_corpus_report()
        for observation in report["observations"]:
            cue = str(observation["cue"])
            if cue in codex:
                continue
            with self.subTest(cue=cue):
                alias = observation["alias_resolution"]
                self.assertIsInstance(alias, dict, f"{cue} resolved without alias diagnostics")
                self.assertEqual(alias["original_invocation"], cue)
                self.assertEqual(alias["target_contract_id"], "ultrawork")
                self.assertIn(alias["selected_capability"], set(ULW_RETIRED_CAPABILITIES.values()))
                reason = str(alias["capability_reason"])
                self.assertTrue(reason.strip())
                self.assertNotIn("\n", reason)
                boundary = str(observation["claim_boundary"])
                self.assertTrue(boundary.strip(), f"{cue} carries no claim boundary")

    def test_status_card_shows_the_reason_line_only(self) -> None:
        interaction = build_chat_interaction_payload("coordinated workers", source="generic")
        route = interaction["route"]
        alias = route.get("alias_resolution")
        self.assertIsInstance(alias, dict)
        state = interaction["chat_response"]["state"]
        self.assertEqual(state.get("capability_reason"), alias["capability_reason"])
        # The card carries the one-line reason, not the machine payload.
        self.assertNotIn("alias_resolution", interaction["chat_response"]["state"])

    def test_capability_reason_uses_the_hermes_harness_default_wording(self) -> None:
        for name in RETIRED:
            with self.subTest(contract=name):
                reason = ulw_alias_capability_reason(name)
                self.assertIn("now runs as `ulw-work` capability", reason)
                self.assertIn(HERMES_HARNESS_DEFAULT_WORDING, reason)
                self.assertNotIn("\n", reason)
                # Informational migration copy, never a deprecation warning.
                self.assertNotIn("deprecat", reason.lower())

    def test_alias_resolution_covers_every_retired_contract(self) -> None:
        for name, capability in ULW_RETIRED_CAPABILITIES.items():
            with self.subTest(contract=name):
                resolution = resolve_ulw_alias(name)
                self.assertIsNotNone(resolution)
                self.assertEqual(resolution["retired_contract_id"], name)
                self.assertEqual(resolution["selected_capability"], capability)


class CodexOwnerChoiceTests(unittest.TestCase):
    """Plan Q9: Codex-named cues are owner-choice signals, never engine triggers."""

    def test_no_codex_named_cue_resolves_ultrawork(self) -> None:
        for cue in CODEX_OWNER_CHOICE_CUES:
            with self.subTest(cue=cue):
                interaction = build_chat_interaction_payload(cue, source="generic")
                self.assertNotEqual(
                    interaction["route"].get("selected_skill"), "ultrawork", cue
                )

    def test_no_ultrawork_trigger_names_a_coding_cli(self) -> None:
        """Mutant guard: adding a Codex-named cue to `ultrawork.triggers`
        fails here, before it can leak into engine routing."""
        from omh.skills.render import _definitions_by_name

        triggers = _definitions_by_name()["ultrawork"].triggers
        for trigger in triggers:
            lowered = trigger.casefold()
            with self.subTest(trigger=trigger):
                for marker in ("codex", "코덱스", "claude"):
                    self.assertNotIn(marker, lowered)

    def test_korean_codex_witnesses_resolve_owner_selection_with_provenance(self) -> None:
        """The three rewritten Korean witnesses resolve the owner-selection
        surface, and any external owner they resolve carries recorded
        explicit-choice provenance (the PR A writers), never an unrecorded
        routing side effect."""
        from omh.coding.executors import EXTERNAL_CLI_PROFILES
        from omh.system.paths import OmhPaths
        from omh.routing.owner_preference import read_owner_preference

        witnesses = (
            "코덱스로 이 이슈 PR 만들 수 있게 작업 시작해줘",
            "codex 세션이 살아있는지 확인해줘",
            "코덱스가 지금 뭐하고있는지 알려줘",
        )
        for message in witnesses:
            with self.subTest(message=message):
                with tempfile.TemporaryDirectory() as tmp:
                    paths = OmhPaths(omh_home=Path(tmp) / "omh", hermes_home=Path(tmp) / "hermes")
                    interaction = build_chat_interaction_payload(
                        message, source="generic", paths=paths
                    )
                    self.assertNotEqual(
                        interaction["route"].get("selected_skill"), "ultrawork"
                    )
                    delegation = interaction.get("delegation") or {}
                    selected = str(delegation.get("selected_executor_profile", "") or "")
                    if selected in EXTERNAL_CLI_PROFILES:
                        preference = read_owner_preference(paths)
                        self.assertTrue(
                            preference,
                            "external owner resolved without recorded explicit-choice provenance",
                        )


class MigrationErrorTests(unittest.TestCase):
    def test_retired_display_names_cover_all_label_eras(self) -> None:
        labels = retired_display_names()
        for expected in (
            "team",
            "ulw-team",
            "omh-team",
            "ralph",
            "ulw-ralph",
            "omh-ralph",
            "ultragoal",
            "ulw-goal",
            "omh-ultragoal",
            "ulw-ultragoal",
            "ultraprocess",
            "ulw-process",
            "omh-ultraprocess",
            "ulw-ultraprocess",
        ):
            with self.subTest(label=expected):
                self.assertIn(expected, labels)

    def test_migration_error_names_the_target_capability(self) -> None:
        error = retired_skill_migration_error("ulw-team")
        self.assertEqual(error["error"], "retired_skill")
        self.assertEqual(error["target_contract_id"], "ultrawork")
        self.assertEqual(error["selected_capability"], "coordinated_scope")
        self.assertIn("ulw-work", error["message"])
        self.assertNotIn("deprecat", error["message"].lower())

    def test_retired_tap_path_resolves_the_same_migration_error(self) -> None:
        error = retired_skill_migration_error("rlaope/oh-my-hermes/skills/ulw-team")
        self.assertEqual(error.get("retired_contract_id"), "team")

    def test_unknown_label_is_not_a_migration_error(self) -> None:
        self.assertEqual(retired_skill_migration_error("ulw-work"), {})
        self.assertEqual(retired_skill_migration_error("frontend"), {})

    def test_installer_refuses_a_retired_tap_checkout_with_the_explicit_error(self) -> None:
        from omh.system.paths import OmhPaths

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "tap" / "skills" / "ulw-team"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text(
                "---\nname: ulw-team\ndescription: retired\n---\n\nlegacy\n",
                encoding="utf-8",
            )
            paths = OmhPaths(omh_home=Path(tmp) / "home", hermes_home=Path(tmp) / "hermes")
            with self.assertRaises(OmhError) as caught:
                install_skill_pack(paths, source="dir", source_dir=Path(tmp) / "tap")
            self.assertIn("retired", str(caught.exception))
            self.assertIn("ulw-work", str(caught.exception))


class DoctorDiagnosisTests(unittest.TestCase):
    def test_doctor_flags_a_leftover_retired_skill_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            retired = skills_dir / "ultrawork" / "ulw-team"
            retired.mkdir(parents=True)
            (retired / "SKILL.md").write_text("---\nname: ulw-team\n---\n", encoding="utf-8")
            paths = SimpleNamespace(skills_dir=skills_dir)
            check = _retired_skill_install_check(paths)
            self.assertFalse(check.ok)
            self.assertIn("ulw-team", check.message)
            self.assertIn("omh update", check.next_action)

    def test_doctor_passes_when_no_retired_skill_is_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            current = skills_dir / "ultrawork" / "ulw-work"
            current.mkdir(parents=True)
            (current / "SKILL.md").write_text("---\nname: ulw-work\n---\n", encoding="utf-8")
            check = _retired_skill_install_check(SimpleNamespace(skills_dir=skills_dir))
            self.assertTrue(check.ok)

    def test_doctor_flags_a_stale_bundle_whose_tables_keep_the_four_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "plugin"
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "awareness.py").write_text(
                '_ULW_ENGINE_LIFECYCLE_STAGES = {\n'
                '    "team": "canonical",\n'
                '    "ralph": "canonical",\n'
                '    "ultragoal": "canonical",\n'
                '    "ultraprocess": "canonical",\n'
                '}\n',
                encoding="utf-8",
            )
            check = _plugin_ulw_lifecycle_check(SimpleNamespace(hermes_plugin_dir=plugin_dir))
            self.assertFalse(check.ok)
            self.assertIn("canonical", check.message)
            self.assertIn("omh setup", check.next_action)

    def test_doctor_flags_an_incompatible_bundle_without_the_lifecycle_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "plugin"
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "awareness.py").write_text("# old bundle\n", encoding="utf-8")
            check = _plugin_ulw_lifecycle_check(SimpleNamespace(hermes_plugin_dir=plugin_dir))
            self.assertFalse(check.ok)
            self.assertIn("incompatible", check.message)

    def test_doctor_passes_on_the_shipped_bundle(self) -> None:
        bundle_dir = REPO_ROOT / "src" / "plugin_bundle" / "omh"
        check = _plugin_ulw_lifecycle_check(SimpleNamespace(hermes_plugin_dir=bundle_dir))
        self.assertTrue(check.ok, check.message)


class PerContractRollbackTests(unittest.TestCase):
    def test_per_contract_rollback_restores_routing(self) -> None:
        """P4 exercised, not asserted (plan §10 Scenario 4): flip ONE retired
        row back to canonical with routable projections and prove routing,
        installability, and inventory placement restore while the other three
        stay retired; then restore the retired row and prove it retires
        again."""
        import dataclasses

        from omh.routing import chat as chat_module
        from omh.routing import ulw_alias as ulw_alias_module
        from omh.skills import catalog as catalog_module

        def _clear_caches() -> None:
            catalog_module._surface_exposure_by_name.cache_clear()
            catalog_module._projected_definitions_cached.cache_clear()
            chat_module._canonical_skill_by_display_name.cache_clear()
            chat_module._route_chat_message_cached.cache_clear()
            chat_module._public_chat_route_payload_cached.cache_clear()
            ulw_alias_module._alias_cue_index.cache_clear()
            ulw_alias_module._codex_owner_choice_index.cache_clear()

        original = catalog_module._SURFACE_EXPOSURES
        restored_row = None
        try:
            rows = []
            for exposure in original:
                if exposure.name == "team":
                    restored_row = dataclasses.replace(
                        exposure,
                        projections=("routable", "installable", "workflow_reference", "capability"),
                        install_visibility=True,
                        docs_visibility="primary_workflow_skill",
                        compatibility_alias=False,
                        lifecycle_stage="canonical",
                        target_home=None,
                        migration_release=None,
                    )
                    rows.append(restored_row)
                else:
                    rows.append(exposure)
            self.assertIsNotNone(restored_row)
            catalog_module._SURFACE_EXPOSURES = tuple(rows)
            _clear_caches()

            # (a) display labels resolve again through the routable resolver.
            mapping = chat_module._canonical_skill_by_display_name()
            self.assertEqual(mapping.get("ulw-team"), "team")
            self.assertEqual(mapping.get("omh-team"), "team")
            # (b) the contract reappears on the installable surface.
            self.assertIn("team", catalog_module.installable_skill_names())
            # (c) the inventory moves it between lists.
            payload = catalog_module.ulw_inventory_payload()
            canonical_names = {engine["canonical"] for engine in payload["canonical_engines"]}
            retired_names = {engine["canonical"] for engine in payload["retired_engines"]}
            self.assertIn("team", canonical_names)
            self.assertNotIn("team", retired_names)
            # (d) the other three stay retired.
            self.assertEqual(retired_names, {"ultraprocess", "ralph", "ultragoal"})
            # Its cues route to it again: the explicit invocation dispatches
            # the restored engine, not the alias target.
            route = chat_module.route_chat_message("$team split this work", source="generic")
            self.assertEqual(route["selected_skill"], "team")
            self.assertNotIn("alias_resolution", route)
        finally:
            catalog_module._SURFACE_EXPOSURES = original
            _clear_caches()

        # Restored state: the row is retired again and the alias owns the cue.
        payload = catalog_module.ulw_inventory_payload()
        self.assertEqual(
            {engine["canonical"] for engine in payload["retired_engines"]},
            set(RETIRED),
        )
        route = chat_module.route_chat_message("$team split this work", source="generic")
        self.assertEqual(route["selected_skill"], "ultrawork")
        self.assertEqual(route["alias_resolution"]["retired_contract_id"], "team")


class InventoryEnumerationTests(unittest.TestCase):
    def test_retired_engines_are_enumerated_separately_never_dropped(self) -> None:
        payload = ulw_inventory_payload()
        self.assertEqual(payload["counts"]["canonical"], 9)
        self.assertEqual(payload["counts"]["alias"], 0)
        self.assertEqual(payload["counts"]["retired"], 4)
        self.assertEqual(payload["counts"]["total"], 13)
        self.assertEqual(
            {engine["canonical"] for engine in payload["retired_engines"]}, set(RETIRED)
        )


if __name__ == "__main__":
    unittest.main()

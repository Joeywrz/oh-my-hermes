"""`omh doctor` says which Jev-class plugins a machine holds, and only that.

Jev is a non-generative decision model OMH never calls. What these tests hold
is the boundary between the three things OMH may say about it and the many it
may not. It may say a plugin directory exists, that Hermes' config lists the
name, and that a credential NAME appears in the env file. It may quote what a
catalog entry declares, naming where the quote was read. It may not say the
plugin ran, that Jev is served, or that anything left the machine -- and it may
never read a credential value, which the fixtures below prove by writing one
and asserting it reaches no output.

The check is appended on every run, in both branches, because an optional
surface that appears only on some machines makes the operator summary's
`total` vary by machine and makes a clean home indistinguishable from a check
that did not run.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from _cli_harness import run_cli
from _local_package import load_local_package
from _platform_support import requires_symlinks

load_local_package()

from omh.commands import setup as setup_commands  # noqa: E402
from omh.install.config_adapter import ensure_plugin_enabled  # noqa: E402
from omh.maintenance.doctor import doctor_ok, run_doctor  # noqa: E402
from omh.paths import OmhPaths  # noqa: E402
from omh.plugin_bundle.omh.jev_sidekick import (  # noqa: E402
    JEV_CATALOG_READ_ON,
    JEV_CREDENTIAL_ENV_NAMES,
    JEV_TOOL_PREFIX,
    KNOWN_JEV_PLUGINS,
    classify_plugin,
    known_jev_plugin_names,
)
from omh.plugin_bundle.omh.metadata import PROVIDED_HOOKS  # noqa: E402
from omh.plugin_bundle.omh.provider_detection import (  # noqa: E402
    HERMES_ENV_KEY_PROVIDERS,
    env_key_names,
)
from omh.workflows.jev_sidekick_posture import (  # noqa: E402
    JEV_SIDEKICK_POSTURE_SCHEMA_VERSION,
    POSTURE_STATUSES,
    build_jev_sidekick_posture,
    posture_overlaps,
    posture_unestablished_hook_overlap,
)

# A value, not a name. Every assertion about it is that it never appears.
SECRET_VALUE = "sk-jev-do-not-print-this-value"


def _write(path: Path, body: str) -> None:
    # `newline="\n"` on every fixture write: `write_text` without it emits
    # CRLF on Windows, and a manifest fixture that differs by platform makes a
    # reader's line handling untested on one of them.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")


def _paths(root: Path) -> OmhPaths:
    paths = OmhPaths(root / ".omh", root / ".hermes")
    paths.hermes_home.mkdir(parents=True, exist_ok=True)
    return paths


def _install_plugin(paths: OmhPaths, directory: str, manifest: str) -> Path:
    plugin_dir = paths.hermes_plugins_dir / directory
    _write(plugin_dir / "plugin.yaml", manifest)
    return plugin_dir


def _enable(paths: OmhPaths, *names: str) -> None:
    """Add each name to `plugins.enabled`, keeping whatever the config already holds.

    Through the writer `omh setup` itself uses, so a fixture that enables a
    third-party plugin does not quietly un-register OMH and turn every other
    doctor check red.
    """
    body = paths.hermes_config_path.read_text(encoding="utf-8") if paths.hermes_config_path.is_file() else ""
    for name in names:
        body = ensure_plugin_enabled(body, name).text
    _write(paths.hermes_config_path, body)


def _doctor_check(paths: OmhPaths):
    checks = run_doctor(paths)
    return checks, next(check for check in checks if check.name == "plugin_jev_sidekick")


def _without_jev(checks):
    return [check for check in checks if check.name != "plugin_jev_sidekick"]


def _installed_paths(root: Path) -> OmhPaths:
    """A home `omh setup` has written, so the CLI report is not dominated by absence."""
    paths = _paths(root)
    base = ["--omh-home", str(paths.omh_home), "--hermes-home", str(paths.hermes_home)]
    status, _stdout, stderr = run_cli(base + ["setup", "--no-interactive"], output_json=False)
    assert status == 0, stderr
    return paths


class ClassifierTests(unittest.TestCase):
    def test_a_known_name_classifies_with_its_catalog_quote(self) -> None:
        record = classify_plugin("typesafe-skill-router", (), ("pre_llm_call",))

        self.assertIsNotNone(record)
        assert record is not None
        self.assertTrue(record["known"])
        self.assertEqual(record["repo"], "https://github.com/DECRUX9812/typesafe-skill-router")
        self.assertEqual(record["jev_tools"], [])
        self.assertIn("pre_llm_call", record["hook_overlap"])
        self.assertIn("two nominations", str(record["overlap"]))
        self.assertTrue(str(record["read_from"]).startswith("hermes-agent plugin-catalog/"))
        self.assertEqual(record["read_on"], JEV_CATALOG_READ_ON)

    def test_an_unknown_name_classifies_on_the_tool_prefix_and_quotes_nothing(self) -> None:
        record = classify_plugin("someone-elses-plugin", ("jev_ask", "unrelated_tool"), ())

        self.assertIsNotNone(record)
        assert record is not None
        self.assertFalse(record["known"])
        self.assertEqual(record["jev_tools"], ["jev_ask"])
        # No record was read for this name, so the plugin must not inherit
        # another plugin's disclosure, repo, or provenance.
        self.assertEqual(record["declared_disclosure"], "")
        self.assertEqual(record["overlap"], "")
        self.assertEqual(record["repo"], "")
        self.assertEqual(record["read_from"], "")
        self.assertEqual(record["read_on"], "")

    def test_neither_signal_is_not_a_finding(self) -> None:
        self.assertIsNone(classify_plugin("herdr-agent-state", ("herdr_state",), ("on_session_start",)))
        self.assertIsNone(classify_plugin("jevons-paradox", (), ()))

    def test_hook_overlap_is_computed_against_the_bridge_not_stored(self) -> None:
        # The claim "this plugin shares a hook with OMH" is derived from OMH's
        # own declared hook list at the moment it is made. A stored sentence
        # would keep saying it after the bridge stopped registering the hook.
        shared = next(hook for hook in PROVIDED_HOOKS)
        record = classify_plugin("hermes-jev", (), (shared, "a_hook_omh_does_not_register"))

        assert record is not None
        self.assertEqual(record["hook_overlap"], [shared])
        self.assertIn("a_hook_omh_does_not_register", record["declares_hooks"])

    def test_every_record_carries_where_and_when_it_was_read(self) -> None:
        for record in KNOWN_JEV_PLUGINS:
            with self.subTest(plugin=record.name):
                self.assertTrue(record.repo.startswith("https://github.com/"))
                self.assertTrue(record.read_from.startswith("hermes-agent plugin-catalog/"))
                self.assertIn(f"/{record.name}.yaml", record.read_from)
                self.assertEqual(record.read_on, JEV_CATALOG_READ_ON)
                for tool in record.declares_tools:
                    self.assertTrue(tool.startswith(JEV_TOOL_PREFIX))
                if record.declared_disclosure:
                    self.assertTrue(record.declared_disclosure.startswith("Disclosure — "))

    def test_the_credential_names_are_the_two_that_are_declared_somewhere(self) -> None:
        self.assertEqual(JEV_CREDENTIAL_ENV_NAMES, frozenset({"TYPESAFE_API_KEY", "OPENROUTER_API_KEY"}))
        # CLOUDFLARE_JEV_API_TOKEN is named by no vendor page and no catalog
        # entry. Reporting an invented variable name as a machine fact is the
        # failure this assertion exists to keep out.
        self.assertNotIn("CLOUDFLARE_JEV_API_TOKEN", JEV_CREDENTIAL_ENV_NAMES)

    def test_jev_names_never_join_the_provider_registry(self) -> None:
        # HERMES_ENV_KEY_PROVIDERS feeds effective_provider_entitlements, so a
        # name added there reorders mixture chains. Jev cannot be a chain
        # member at all, so its credential names must reach the posture
        # through the `allowed` parameter and never through enrolment.
        self.assertNotIn("TYPESAFE_API_KEY", HERMES_ENV_KEY_PROVIDERS)


class CatalogTranscriptionTests(unittest.TestCase):
    """A second hand copy of the same catalog read, kept beside the first.

    Parity between a table and something generated from it would agree with a
    wrong table. This is the other kind of check: two independent
    transcriptions of the same YAML, so an edit to either side that nobody
    intended fails. It detects a local change to the table; it does not and
    cannot detect upstream drift, because nothing here reads the catalog.
    """

    MIRROR = {
        "jev": (
            "https://github.com/ourines/hermes-jev",
            ("jev_evaluate",),
            (),
            "",
        ),
        "jev-typesafe": (
            "https://github.com/ajensenwaud/hermes-jev-plugin",
            ("jev_evaluate", "jev_check", "jev_route", "jev_score"),
            (),
            "Disclosure — every tool call sends the model-supplied question/state together with your "
            "TYPESAFE_API_KEY to api.typesafe.ai (TypeSafe AI, an independent vendor); the catalog has not "
            "verified the plugin author's affiliation with that vendor.",
        ),
        "hermes-jev": (
            "https://github.com/keeltrace/hermes-jev",
            (
                "jev_decide",
                "jev_rank",
                "jev_verify",
                "jev_assess",
                "jev_context_curate",
                "jev_context_rehydrate",
                "jev_stats",
                "jev_nervous_event",
            ),
            (
                "pre_tool_call",
                "post_tool_call",
                "pre_llm_call",
                "transform_tool_result",
                "pre_verify",
                "post_llm_call",
                "on_session_end",
            ),
            "Disclosure — with the default settings (nervous_enabled / turn_admission on) each turn's user "
            "prompt (up to 12k characters) and redacted tool/result previews are sent to OpenRouter Decisions "
            "(TypeSafe Jev) using your OPENROUTER_API_KEY or TYPESAFE_API_KEY, spending your credits on every turn.",
        ),
        "jev-approvals": (
            "https://github.com/anpicasso/hermes-jev-approvals",
            (),
            (),
            "Disclosure — each command routed to smart approval (redacted best-effort) and the operator's "
            "smart-policy text leave the machine for the configured third-party endpoint; provider or validation "
            "failures fail closed to ESCALATE.",
        ),
        "hermes-structured-aux-models": (
            "https://github.com/trajectoire-ai/hermes-structured-aux-models",
            (),
            (),
            "Disclosure — sends redacted approval prompts, MCP tool names and compression transcript blocks "
            "to openrouter.ai using your OpenRouter key (read-only); on any error approvals escalate to you, never "
            "auto-approve; the default decision_model is a moving alias, pin it.",
        ),
        "typesafe-skill-router": (
            "https://github.com/DECRUX9812/typesafe-skill-router",
            (),
            ("pre_llm_call",),
            "",
        ),
        "jev-model-router": ("https://github.com/Pinutss/jev-model-router", (), (), ""),
        "jev-memory-selector": ("https://github.com/Pinutss/jev-memory-selector", (), (), ""),
        "jev-agent-router": ("https://github.com/Pinutss/jev-agent-router", (), (), ""),
        "jev-mcp-router": ("https://github.com/Pinutss/jev-mcp-router", (), (), ""),
    }

    def test_the_table_matches_the_second_transcription(self) -> None:
        self.assertEqual(set(known_jev_plugin_names()), set(self.MIRROR))
        for record in KNOWN_JEV_PLUGINS:
            with self.subTest(plugin=record.name):
                self.assertEqual(
                    (record.repo, record.declares_tools, record.declares_hooks, record.declared_disclosure),
                    self.MIRROR[record.name],
                )


class PostureTests(unittest.TestCase):
    def test_a_home_with_no_plugins_is_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))

            posture = build_jev_sidekick_posture(paths.hermes_home)

            self.assertEqual(posture["schema_version"], JEV_SIDEKICK_POSTURE_SCHEMA_VERSION)
            self.assertEqual(posture["status"], "absent")
            self.assertEqual(posture["plugins"], [])
            self.assertEqual(posture["skipped"], [])
            self.assertIn("never reads credential values", str(posture["claim_boundary"]))
            self.assertIn(posture["status"], POSTURE_STATUSES)

    def test_a_missing_hermes_home_is_absent_rather_than_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            posture = build_jev_sidekick_posture(Path(tmp) / "never-created")

            self.assertEqual(posture["status"], "absent")

    def test_a_plugin_that_is_neither_known_nor_jev_tooled_is_not_reported(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            _install_plugin(paths, "herdr-agent-state", "name: herdr-agent-state\ndescription: panes\n")

            posture = build_jev_sidekick_posture(paths.hermes_home)

            self.assertEqual(posture["status"], "absent")
            self.assertEqual(posture["skipped"], [])

    def test_an_installed_jev_tool_is_found_under_any_directory_name(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            _install_plugin(paths, "local-checkout", "name: mystery\nprovides_tools:\n  - jev_ask\n")

            posture = build_jev_sidekick_posture(paths.hermes_home)

            self.assertEqual(posture["status"], "installed")
            entry = posture["plugins"][0]
            self.assertEqual(entry["name"], "mystery")
            self.assertEqual(entry["directory"], "local-checkout")
            self.assertFalse(entry["known"])
            self.assertFalse(entry["enabled"])

    def test_a_known_name_is_found_with_no_tools_declared(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            _install_plugin(paths, "jev-approvals", "name: jev-approvals\nversion: \"0.3.0\"\n")

            posture = build_jev_sidekick_posture(paths.hermes_home)

            entry = posture["plugins"][0]
            self.assertTrue(entry["known"])
            self.assertEqual(entry["jev_tools"], [])
            self.assertIn("fail closed to ESCALATE", str(entry["declared_disclosure"]))

    def test_enablement_is_read_from_the_hermes_config(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            _install_plugin(paths, "jev", "name: jev\nprovides_tools:\n  - jev_evaluate\n")

            self.assertEqual(build_jev_sidekick_posture(paths.hermes_home)["status"], "installed")

            _enable(paths, "jev")

            posture = build_jev_sidekick_posture(paths.hermes_home)
            self.assertEqual(posture["status"], "enabled")
            self.assertTrue(posture["plugins"][0]["enabled"])

    def test_a_credential_name_is_read_and_its_value_is_not(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            _install_plugin(paths, "jev", "name: jev\nprovides_tools:\n  - jev_evaluate\n")
            _enable(paths, "jev")
            _write(paths.hermes_home / ".env", f"TYPESAFE_API_KEY={SECRET_VALUE}\n")

            posture = build_jev_sidekick_posture(paths.hermes_home)

            self.assertEqual(posture["status"], "credential_name_present")
            self.assertEqual(posture["credential_names_present"], ["TYPESAFE_API_KEY"])
            self.assertNotIn(SECRET_VALUE, json.dumps(posture))

    def test_an_openrouter_key_alone_is_not_a_jev_signal(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            _write(paths.hermes_home / ".env", f"OPENROUTER_API_KEY={SECRET_VALUE}\n")

            posture = build_jev_sidekick_posture(paths.hermes_home)

            self.assertEqual(posture["status"], "absent")
            self.assertEqual(posture["credential_names_present"], [])

    def test_a_key_outside_the_jev_set_never_reaches_the_posture(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            _install_plugin(paths, "jev", "name: jev\nprovides_tools:\n  - jev_evaluate\n")
            _enable(paths, "jev")
            _write(paths.hermes_home / ".env", f"ANTHROPIC_API_KEY={SECRET_VALUE}\nTYPESAFE_API_KEY={SECRET_VALUE}\n")

            posture = build_jev_sidekick_posture(paths.hermes_home)

            self.assertEqual(posture["credential_names_present"], ["TYPESAFE_API_KEY"])

    def test_an_unreadable_manifest_is_named_rather_than_dropped(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            # Unquoted, which YAML itself rejects: the bounded reader refuses
            # the document rather than reading the keys around it.
            _install_plugin(paths, "broken", "name: broken\nrequires_hermes: >=0.21\n")

            posture = build_jev_sidekick_posture(paths.hermes_home)

            self.assertEqual(posture["status"], "absent")
            self.assertEqual([entry["plugin"] for entry in posture["skipped"]], ["broken"])
            self.assertIn("outside the readable subset", posture["skipped"][0]["reason"])

    def test_a_tool_declaration_the_reader_does_not_model_is_named(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            # An inline flow sequence. `[]` and `[jev_evaluate]` are the same
            # shape to the bounded reader, so an unknown name whose tools it
            # could not read is a plugin it could not clear.
            _install_plugin(paths, "inline", "name: inline\nprovides_tools: [jev_evaluate]\n")

            posture = build_jev_sidekick_posture(paths.hermes_home)

            self.assertEqual([entry["plugin"] for entry in posture["skipped"]], ["inline"])
            self.assertIn("provides_tools", posture["skipped"][0]["reason"])

    def test_an_unread_hook_declaration_leaves_the_overlap_unestablished(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            _install_plugin(paths, "jev", "name: jev\nprovides_tools:\n  - jev_evaluate\nprovides_hooks: []\n")

            posture = build_jev_sidekick_posture(paths.hermes_home)

            entry = posture["plugins"][0]
            self.assertEqual(entry["hook_overlap"], [])
            self.assertEqual(entry["unreadable_declarations"], ["provides_hooks"])
            # An empty overlap means two different things and only one of them
            # is "no overlap".
            self.assertEqual(posture_overlaps(posture), [])
            self.assertEqual(posture_unestablished_hook_overlap(posture), ["jev"])

    def test_a_read_hook_declaration_establishes_the_overlap(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            _install_plugin(
                paths,
                "typesafe-skill-router",
                "name: typesafe-skill-router\nprovides_hooks:\n  - pre_llm_call\n",
            )

            posture = build_jev_sidekick_posture(paths.hermes_home)

            self.assertEqual(posture["plugins"][0]["hook_overlap"], ["pre_llm_call"])
            self.assertEqual(posture_unestablished_hook_overlap(posture), [])
            self.assertEqual(posture_overlaps(posture), ["typesafe-skill-router"])

    @requires_symlinks
    def test_a_symlinked_plugin_directory_is_reported_and_not_followed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            outside = root / "outside-the-home"
            _write(outside / "plugin.yaml", "name: jev\nprovides_tools:\n  - jev_evaluate\n")
            paths.hermes_plugins_dir.mkdir(parents=True, exist_ok=True)
            (paths.hermes_plugins_dir / "linked").symlink_to(outside, target_is_directory=True)

            posture = build_jev_sidekick_posture(paths.hermes_home)

            self.assertEqual(posture["plugins"], [])
            self.assertEqual([entry["plugin"] for entry in posture["skipped"]], ["linked"])
            self.assertIn("symlinked", posture["skipped"][0]["reason"])

    def test_zero_writes_to_hermes_home(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            _install_plugin(paths, "jev", "name: jev\nprovides_tools:\n  - jev_evaluate\n")
            _enable(paths, "jev")
            _write(paths.hermes_home / ".env", f"TYPESAFE_API_KEY={SECRET_VALUE}\n")
            before = {
                str(item): (item.stat().st_mtime_ns, item.stat().st_size)
                for item in paths.hermes_home.rglob("*")
                if item.is_file()
            }

            build_jev_sidekick_posture(paths.hermes_home)

            after = {
                str(item): (item.stat().st_mtime_ns, item.stat().st_size)
                for item in paths.hermes_home.rglob("*")
                if item.is_file()
            }
            self.assertEqual(before, after)


class EnvKeyNameSeamTests(unittest.TestCase):
    def test_the_default_call_is_unchanged(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes"
            _write(home / ".env", f"GLM_API_KEY={SECRET_VALUE}\nTYPESAFE_API_KEY={SECRET_VALUE}\n")

            # The registry table decides when nothing is passed, exactly as
            # before: the Jev name is present in the file and absent from the
            # result, because it was never enrolled.
            self.assertEqual(env_key_names(home), ["GLM_API_KEY"])
            self.assertEqual(env_key_names(home, allowed=None), ["GLM_API_KEY"])

    def test_the_allowed_parameter_scopes_both_scans(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes"
            _write(home / ".env", f"GLM_API_KEY={SECRET_VALUE}\nTYPESAFE_API_KEY={SECRET_VALUE}\n")

            names = env_key_names(
                home,
                environ={"OPENROUTER_API_KEY": SECRET_VALUE, "PATH": "/bin"},
                allowed=JEV_CREDENTIAL_ENV_NAMES,
            )

            self.assertEqual(names, ["OPENROUTER_API_KEY", "TYPESAFE_API_KEY"])


class DoctorCheckTests(unittest.TestCase):
    def test_the_check_is_appended_on_a_machine_with_no_signal(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))

            checks, check = _doctor_check(paths)

            self.assertTrue(check.ok)
            self.assertEqual(check.severity, "ok")
            self.assertEqual(check.message, "optional: no Jev-class plugin installed")
            self.assertEqual(check.detail["status"], "absent")
            self.assertEqual(doctor_ok(checks), doctor_ok(_without_jev(checks)))

    def test_a_configured_plugin_with_no_overlap_stays_quiet(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            _install_plugin(paths, "jev", "name: jev\nprovides_tools:\n  - jev_evaluate\nprovides_hooks:\n")
            _enable(paths, "jev")
            _write(paths.hermes_home / ".env", f"TYPESAFE_API_KEY={SECRET_VALUE}\n")

            _checks, check = _doctor_check(paths)

            self.assertEqual(check.severity, "ok")
            self.assertIn("jev (enabled) declares jev_evaluate", check.message)
            self.assertIn("read from hermes-agent plugin-catalog/jev.yaml", check.message)

    def test_a_hook_overlap_warns_and_names_the_shared_hook(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            _install_plugin(
                paths,
                "typesafe-skill-router",
                "name: typesafe-skill-router\nprovides_hooks:\n  - pre_llm_call\n",
            )
            _enable(paths, "typesafe-skill-router")
            _write(paths.hermes_home / ".env", f"TYPESAFE_API_KEY={SECRET_VALUE}\n")

            checks, check = _doctor_check(paths)

            self.assertTrue(check.ok)
            self.assertEqual(check.severity, "warning")
            self.assertIn("shares with the OMH bridge the hooks pre_llm_call", check.message)
            self.assertIn("two nominations", check.message)
            self.assertIn("omh doctor --json", check.next_action)
            # A third-party plugin the operator installed is not an OMH
            # install failure and must not flip the exit code. The fixture
            # home has no OMH install, so the absolute verdict is not the
            # claim: the claim is that removing this check changes nothing.
            self.assertEqual(doctor_ok(checks), doctor_ok(_without_jev(checks)))

    def test_a_detected_plugin_without_a_credential_name_warns(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            _install_plugin(paths, "jev", "name: jev\nprovides_tools:\n  - jev_evaluate\nprovides_hooks:\n")
            _enable(paths, "jev")

            _checks, check = _doctor_check(paths)

            self.assertEqual(check.severity, "warning")
            self.assertEqual(check.detail["status"], "enabled")

    def test_an_unswept_directory_is_not_reported_as_absence(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            _install_plugin(paths, "broken", "name: broken\nrequires_hermes: >=0.21\n")

            _checks, check = _doctor_check(paths)

            self.assertEqual(check.severity, "warning")
            self.assertNotIn("no Jev-class plugin installed", check.message)
            self.assertIn("1 plugin directory not fully read: broken", check.message)

    def test_no_message_on_any_branch_carries_a_credential_value(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            _install_plugin(paths, "hermes-jev", "name: hermes-jev\nprovides_hooks:\n  - pre_llm_call\n")
            _enable(paths, "hermes-jev")
            _write(paths.hermes_home / ".env", f"OPENROUTER_API_KEY={SECRET_VALUE}\n")

            _checks, check = _doctor_check(paths)

            self.assertIn("OPENROUTER_API_KEY", check.message)
            self.assertNotIn(SECRET_VALUE, check.message)
            self.assertNotIn(SECRET_VALUE, json.dumps(check.detail))

    def test_the_check_joins_optional_surfaces_and_no_other_group(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))

            summary = setup_commands._doctor_operator_summary(run_doctor(paths))

            groups = {group["name"]: group for group in summary["groups"]}
            self.assertIn("plugin_jev_sidekick", _group_member_names(paths, "optional_surfaces"))
            for name, group in groups.items():
                if name == "optional_surfaces":
                    continue
                self.assertNotIn("plugin_jev_sidekick", _group_member_names(paths, name), name)


def _group_member_names(paths: OmhPaths, group_name: str) -> list[str]:
    checks = [
        {"name": check.name, "ok": check.ok, "severity": check.severity} for check in run_doctor(paths)
    ]
    prefixes = {
        "command": ("command_path",),
        "managed_skills": (
            "manifest",
            "manifest_skills_dir",
            "local_modifications",
            "skill_freshness",
            "skills_dir",
            "skill:",
            "guidance_projection",
        ),
        "runtime": ("runtime_artifacts", "workflow_state", "runtime_state"),
        "hermes_registration": ("hermes_config", "external_dir", "identity_conflicts", "runtime_context"),
        "targets": ("target_registry", "target_topology"),
        "model_routing": ("hermes_model_routing", "provider_entitlements"),
        "optional_surfaces": ("plugin_", "team_profile_packs", "structural_search", "trigger_language_packs"),
    }[group_name]
    return [
        str(check["name"]) for check in checks if any(str(check["name"]).startswith(prefix) for prefix in prefixes)
    ]


class DoctorJsonTests(unittest.TestCase):
    def test_the_posture_reaches_the_json_report_under_detail(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _installed_paths(Path(tmp))
            _install_plugin(paths, "jev", "name: jev\nprovides_tools:\n  - jev_evaluate\nprovides_hooks:\n")
            _enable(paths, "jev")
            _write(paths.hermes_home / ".env", f"TYPESAFE_API_KEY={SECRET_VALUE}\n")
            base = ["--omh-home", str(paths.omh_home), "--hermes-home", str(paths.hermes_home)]

            with patch("omh.command_path.shutil.which", return_value="/usr/local/bin/omh"):
                status, stdout, stderr = run_cli(base + ["doctor", "--json"], output_json=False)

            self.assertEqual(status, 0, stderr)
            payload = json.loads(stdout)
            check = {item["name"]: item for item in payload["checks"]}["plugin_jev_sidekick"]
            self.assertTrue(check["ok"])
            posture = check["detail"]
            self.assertEqual(posture["schema_version"], JEV_SIDEKICK_POSTURE_SCHEMA_VERSION)
            self.assertEqual(posture["status"], "credential_name_present")
            self.assertEqual(posture["plugins"][0]["name"], "jev")
            self.assertNotIn(SECRET_VALUE, stdout)

    def test_a_check_with_no_structured_finding_carries_none(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _installed_paths(Path(tmp))
            base = ["--omh-home", str(paths.omh_home), "--hermes-home", str(paths.hermes_home)]

            with patch("omh.command_path.shutil.which", return_value="/usr/local/bin/omh"):
                status, stdout, stderr = run_cli(base + ["doctor", "--json"], output_json=False)

            self.assertEqual(status, 0, stderr)
            checks = {item["name"]: item for item in json.loads(stdout)["checks"]}
            self.assertIsNone(checks["command_path"]["detail"])


if __name__ == "__main__":
    unittest.main()

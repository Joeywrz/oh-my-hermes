from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from _cli_harness import run_cli
from omh.routing import recommend as recommend_module
from omh.skill_pack import builtin_definitions, builtin_harnesses
from omh.wrapper.contract import VISIBLE_ACTIONS, build_chat_interaction_payload
from omh.workflows.plugin_hook_contract import PLUGIN_HOOK_CONTRACT_RANGE
from omh.workflows.plugin_risk_audit import audit_plugin_risk

from _platform_support import requires_secure_dir_io


class PluginRiskAuditTests(unittest.TestCase):
    @requires_secure_dir_io
    def test_audit_reports_static_risk_categories_without_exposing_plugin_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "plugin.json").write_text('{"name": "example"}\n', encoding="utf-8")
            (root / "pyproject.toml").write_text('dependencies = ["requests>=2"]\n', encoding="utf-8")
            source_marker = "PRIVATE_AUDIT_SOURCE_MARKER"
            (root / "plugin.py").write_text(
                "import requests\n"
                "import subprocess\n"
                "def hook() -> None:\n"
                "    subprocess.run(['tool'], check=False)\n"
                "    eval('1 + 1')\n"
                "    requests.get('https://example.invalid')\n"
                "    pre_tool_call = True\n"
                "    api_key = 'sk_abcdefghijk1234567890'\n"
                f"    marker = '{source_marker}'\n",
                encoding="utf-8",
            )

            payload = audit_plugin_risk(root)

        self.assertEqual(payload["schema_version"], "plugin_risk_audit/v1")
        self.assertTrue(payload["source"]["explicit_root"])
        self.assertEqual(payload["source"]["manifest_status"], "present")
        self.assertEqual(payload["summary"]["scanned_file_count"], 3)
        self.assertEqual(
            payload["summary"]["risk_categories"],
            [
                "declared_dependency",
                "dynamic_code_execution",
                "hermes_hook_capability",
                "network_request",
                "potential_committed_secret",
                "process_execution",
                # No root plugin.yaml, so the declared hook contract was never
                # established -- the text detector saying "hook" is not the
                # same claim as a read declaration.
                "undetermined_hook_contract",
                # `requests.get` beside `eval` is retrieval beside dynamic
                # execution: the fixture writes no code file, but retrieved
                # bytes could become code without one, so the scan settles it
                # neither way.
                "undetermined_self_update_path",
            ],
        )
        self.assertEqual(payload["not_observed"]["plugin_import"]["status"], "not_observed")
        self.assertEqual(payload["not_observed"]["plugin_execution"]["status"], "not_observed")
        self.assertNotIn(source_marker, json.dumps(payload))
        self.assertNotIn(str(root), json.dumps(payload))

    @requires_secure_dir_io
    def test_audit_rejects_non_directory_and_symlinked_plugin_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "plugin.py"
            source.write_text("pass\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "directory"):
                audit_plugin_risk(source)

            nested = root / "plugin"
            nested.mkdir()
            (nested / "plugin.py").write_text("pass\n", encoding="utf-8")
            linked = nested / "linked.py"
            linked.symlink_to(source)

            with self.assertRaisesRegex(ValueError, "symlink"):
                audit_plugin_risk(nested)

            aliased_parent = root / "aliased-parent"
            aliased_parent.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                audit_plugin_risk(aliased_parent / "plugin")

    @requires_secure_dir_io
    def test_audit_classifies_javascript_plugins_and_package_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "plugin.json").write_text('{"name": "javascript-example"}\n', encoding="utf-8")
            (root / "package.json").write_text('{"dependencies": {"axios": "1.0.0"}}\n', encoding="utf-8")
            (root / "plugin.mjs").write_text(
                "child_process.exec('tool');\n"
                "new Function('return 1');\n"
                "fetch('https://example.invalid');\n"
                "const pre_tool_call = true;\n",
                encoding="utf-8",
            )

            payload = audit_plugin_risk(root)

        self.assertEqual(
            payload["summary"]["risk_categories"],
            [
                "declared_dependency",
                "dynamic_code_execution",
                "hermes_hook_capability",
                "network_request",
                "process_execution",
                "undetermined_hook_contract",
                # `fetch` beside `new Function`, the JavaScript shape of the
                # same ambiguity.
                "undetermined_self_update_path",
            ],
        )

    @requires_secure_dir_io
    def test_audit_does_not_treat_javascript_process_execution_as_dynamic_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "plugin.js").write_text("child_process.exec('tool');\n", encoding="utf-8")

            payload = audit_plugin_risk(root)

        self.assertEqual(
            payload["summary"]["risk_categories"], ["process_execution", "undetermined_hook_contract"]
        )

    def test_audit_rejects_the_filesystem_root_before_scanning(self) -> None:
        filesystem_root = Path(Path.cwd().anchor)

        with self.assertRaisesRegex(ValueError, "filesystem root"):
            audit_plugin_risk(filesystem_root)

    def test_audit_rejects_special_audited_files_without_reading_them(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("named pipes are unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            os.mkfifo(root / "untrusted.py")

            with self.assertRaisesRegex(ValueError, "regular files"):
                audit_plugin_risk(root)

    @requires_secure_dir_io
    def test_audit_bounds_directory_entries_and_uses_only_the_root_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "plugin.json").write_text('{"name": "root"}\n', encoding="utf-8")
            nested = root / "nested"
            nested.mkdir()
            (nested / "plugin.json").write_text("not valid json", encoding="utf-8")

            payload = audit_plugin_risk(root)

            self.assertEqual(payload["source"]["manifest_status"], "present")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for index in range(129):
                (root / f"ignored-{index}.bin").write_text("ignored\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "at most 128 entries"):
                audit_plugin_risk(root)

    @requires_secure_dir_io
    def test_cli_audits_one_explicit_plugin_root_without_registering_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "plugin.json").write_text('{"name": "example"}\n', encoding="utf-8")
            (root / "plugin.py").write_text("def pre_llm_call() -> None:\n    return None\n", encoding="utf-8")

            status, stdout, stderr = run_cli(["ops", "plugin-risk-audit", "--path", str(root)])

        self.assertEqual(status, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["schema_version"], "plugin_risk_audit/v1")
        self.assertEqual(payload["summary"]["scanned_file_count"], 2)
        self.assertEqual(payload["not_observed"]["plugin_registration"]["status"], "not_observed")

class DeclaredHookClassificationTests(unittest.TestCase):
    """The declared-hook projection: what a hook would do, never what it did.

    Every fixture writes a `plugin.yaml` and nothing is imported, registered or
    run, which is the point -- an operator gets the execution semantics of a
    declared hook before deciding to enable the plugin.
    """

    def _audit(self, manifest: str, **files: str) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "plugin.yaml").write_text(manifest, encoding="utf-8")
            for name, content in files.items():
                (root / name).write_text(content, encoding="utf-8")
            return audit_plugin_risk(root)

    @requires_secure_dir_io
    def test_a_manifest_declaring_only_post_tool_call_is_hook_bearing(self) -> None:
        # The legacy detector matched three names in scanned text. This plugin
        # contains none of them anywhere, and is still hook-bearing.
        payload = self._audit(
            "name: observer-only\nprovides_hooks:\n  - post_tool_call\n",
            **{"plugin.py": "def register(ctx):\n    return None\n"},
        )

        self.assertIn("hermes_hook_capability", payload["summary"]["risk_categories"])
        self.assertEqual(payload["source"]["manifest_yaml_status"], "present")
        self.assertEqual(payload["declared_hooks"]["declaration_status"], "declared")
        self.assertEqual(payload["declared_hooks"]["classification_status"], "classified")
        self.assertEqual(
            payload["declared_hooks"]["hooks"],
            [
                {
                    "hook": "post_tool_call",
                    "declared_in": "provides_hooks",
                    "effect": "observer",
                    "host_contract": "bounded",
                    "timeout_semantics": "fail_open",
                }
            ],
        )

    @requires_secure_dir_io
    def test_a_declared_pre_tool_call_is_a_bounded_fail_closed_policy_gate(self) -> None:
        payload = self._audit("name: gate\nprovides_hooks:\n  - pre_tool_call\n")

        self.assertEqual(
            payload["declared_hooks"]["hooks"][0],
            {
                "hook": "pre_tool_call",
                "declared_in": "provides_hooks",
                "effect": "policy_gate",
                "host_contract": "bounded",
                "timeout_semantics": "fail_closed",
            },
        )
        self.assertEqual(payload["declared_hooks"]["effects"], ["policy_gate"])

    @requires_secure_dir_io
    def test_observer_transformer_lifecycle_and_caller_thread_hooks_are_ordered_and_closed(self) -> None:
        payload = self._audit(
            "name: mixed\n"
            'requires_hermes: ">=0.21.1,<0.22.0"\n'
            "provides_hooks:\n"
            "  - transform_tool_result\n"
            "  - subagent_stop\n"
            "  - on_stream_delta\n"
            "  - post_llm_call\n"
            "  - on_session_finalize\n"
        )

        self.assertEqual(
            payload["declared_hooks"]["hooks"],
            [
                {
                    "hook": "on_session_finalize",
                    "declared_in": "provides_hooks",
                    "effect": "lifecycle_callback",
                    "host_contract": "caller_thread",
                    "timeout_semantics": "not_applicable",
                },
                {
                    "hook": "on_stream_delta",
                    "declared_in": "provides_hooks",
                    "effect": "observer",
                    "host_contract": "queued_worker",
                    "timeout_semantics": "not_applicable",
                },
                {
                    "hook": "post_llm_call",
                    "declared_in": "provides_hooks",
                    "effect": "observer",
                    "host_contract": "bounded",
                    "timeout_semantics": "fail_open",
                },
                {
                    "hook": "subagent_stop",
                    "declared_in": "provides_hooks",
                    "effect": "lifecycle_callback",
                    "host_contract": "caller_thread",
                    "timeout_semantics": "not_applicable",
                },
                {
                    "hook": "transform_tool_result",
                    "declared_in": "provides_hooks",
                    "effect": "result_transformer",
                    "host_contract": "bounded",
                    "timeout_semantics": "fail_open",
                },
            ],
        )
        self.assertEqual(payload["declared_hooks"]["classification_status"], "classified")
        self.assertEqual(payload["declared_hooks"]["host_contract"]["declared_range_status"], "supported")

    @requires_secure_dir_io
    def test_an_unknown_hook_name_cannot_produce_a_classified_result(self) -> None:
        payload = self._audit("name: future\nprovides_hooks:\n  - pre_tool_call\n  - on_future_event\n")
        declared = payload["declared_hooks"]

        self.assertEqual(declared["classification_status"], "unknown")
        self.assertEqual(declared["unknown_hook_count"], 1)
        self.assertIn("unknown", declared["effects"])
        self.assertIn("policy_gate", declared["effects"])
        self.assertTrue(any("cannot be classified" in line for line in declared["diagnostics"]))

    @requires_secure_dir_io
    def test_an_unsupported_contract_revision_makes_every_hook_unknown(self) -> None:
        payload = self._audit(
            'name: future-host\nrequires_hermes: ">=0.30.0,<0.31.0"\nprovides_hooks:\n  - pre_tool_call\n'
        )
        declared = payload["declared_hooks"]

        self.assertEqual(declared["host_contract"]["declared_range_status"], "unsupported")
        self.assertEqual(declared["classification_status"], "unknown")
        self.assertEqual(declared["hooks"][0]["effect"], "unknown")
        self.assertEqual(declared["hooks"][0]["timeout_semantics"], "unknown")
        self.assertIn("hermes_hook_capability", payload["summary"]["risk_categories"])

    @requires_secure_dir_io
    def test_an_unreadable_contract_revision_is_never_echoed_or_assumed(self) -> None:
        payload = self._audit(
            'name: bad-range\nrequires_hermes: "later than 0.21"\nprovides_hooks:\n  - pre_tool_call\n'
        )
        declared = payload["declared_hooks"]

        self.assertEqual(declared["host_contract"]["declared_range_status"], "unparsable")
        self.assertEqual(declared["host_contract"]["declared_range"], "<invalid>")
        self.assertEqual(declared["classification_status"], "unknown")
        self.assertNotIn("later than", json.dumps(payload))

    @requires_secure_dir_io
    def test_malformed_hook_declarations_report_a_bounded_diagnostic(self) -> None:
        cases = {
            "non-list": "name: x\nprovides_hooks: pre_tool_call\n",
            "duplicate-field": "name: x\nprovides_hooks:\n  - pre_tool_call\nprovides_hooks:\n  - post_tool_call\n",
            "duplicate-entry": "name: x\nprovides_hooks:\n  - pre_tool_call\n  - pre_tool_call\n",
            "non-string": "name: x\nprovides_hooks:\n  - {name: pre_tool_call}\n",
            "oversized": "name: x\nprovides_hooks:\n" + "".join(f"  - hook_{index}\n" for index in range(65)),
        }
        for case, manifest in cases.items():
            with self.subTest(case=case):
                payload = self._audit(manifest)
                declared = payload["declared_hooks"]

                self.assertEqual(declared["declaration_status"], "invalid")
                self.assertEqual(declared["classification_status"], "unknown")
                self.assertEqual(declared["hooks"], [])
                # The document parsed; only its hook declaration is malformed,
                # and the two findings stay apart.
                self.assertEqual(payload["source"]["manifest_yaml_status"], "present")
                self.assertEqual(len(declared["diagnostics"]), 1)
                self.assertLess(len(declared["diagnostics"][0]), 200)

    @requires_secure_dir_io
    def test_a_manifest_outside_the_readable_subset_is_reported_unreadable(self) -> None:
        payload = self._audit("name: x\nprovides_hooks:\n\t- pre_tool_call\n")
        declared = payload["declared_hooks"]

        self.assertEqual(payload["source"]["manifest_yaml_status"], "unreadable")
        self.assertEqual(declared["declaration_status"], "invalid")
        self.assertEqual(declared["classification_status"], "unknown")
        self.assertEqual(declared["hooks"], [])

    @requires_secure_dir_io
    def test_a_manifest_the_audit_could_not_understand_never_summarises_as_clean(self) -> None:
        """The summary is what a wrapper reads, so a failure has to reach it.

        Before this, an unreadable manifest produced `risk_categories: []` --
        byte-identical to a plugin that declares nothing, so "I could not read
        this" rendered as "this is fine". Every way the contract can fail to be
        established is checked, and no fixture here contains one of the three
        legacy regex names, so none of them passes by that accident.
        """
        cases = {
            "unreadable-document": "name: x\nprovides_hooks:\n\t- on_session_start\n",
            "invalid-declaration": "name: x\nprovides_hooks:\n  - on_session_start\n  - on_session_start\n",
            "unsupported-range": 'name: x\nrequires_hermes: ">=0.30.0"\nprovides_hooks:\n  - on_session_start\n',
            "unparsable-range": 'name: x\nrequires_hermes: "newest"\nprovides_hooks:\n  - on_session_start\n',
            "unknown-hook-name": "name: x\nprovides_hooks:\n  - on_pre_compress\n",
        }
        for case, manifest in cases.items():
            with self.subTest(case=case):
                payload = self._audit(manifest)

                self.assertEqual(payload["declared_hooks"]["classification_status"], "unknown")
                self.assertIn("undetermined_hook_contract", payload["summary"]["risk_categories"])
                self.assertGreater(payload["summary"]["risk_category_count"], 0)

    @requires_secure_dir_io
    def test_a_safe_dump_style_manifest_is_read_rather_than_lost(self) -> None:
        """The F1 case, on the other side of the fix.

        `yaml.safe_dump(..., default_flow_style=False)` puts a block sequence at
        its parent key's own indentation, and it is the commonest way a manifest
        gets generated. The reader refused it, so the whole declaration was lost
        and the summary read clean. Both halves were fixed: the reader models
        this shape now, and a manifest that still is not understood is kept out
        of a clean summary by the invariant test below rather than by widening
        the subset construct by construct.

        The fixture names no hook the legacy three-name regex matches, so the
        category here comes from the declaration actually being read.
        """
        payload = self._audit("name: p\nprovides_hooks:\n- post_tool_call\n- transform_tool_result\n")

        self.assertEqual(payload["source"]["manifest_yaml_status"], "present")
        self.assertEqual(payload["declared_hooks"]["classification_status"], "classified")
        self.assertEqual(
            [hook["hook"] for hook in payload["declared_hooks"]["hooks"]],
            ["post_tool_call", "transform_tool_result"],
        )
        self.assertEqual(payload["summary"]["risk_categories"], ["hermes_hook_capability"])

    @requires_secure_dir_io
    def test_a_manifest_the_host_cannot_load_is_never_reported_as_classified(self) -> None:
        """F1 inverted: the category rule cannot catch a *wrong* read.

        A nested alias with no anchor makes the document invalid, so Hermes
        cannot load this plugin at all. The reader used to walk past it and
        report `classified` with a `pre_tool_call` policy gate -- an established
        contract read out of a file that has none. `classification_status` was
        `classified`, so `undetermined_hook_contract` never fired: being right
        about what it could not read does not help when it believes it read
        something it did not.
        """
        payload = self._audit("name: p\nconfig:\n  ref: *missing\nprovides_hooks:\n  - pre_tool_call\n")

        self.assertEqual(payload["source"]["manifest_yaml_status"], "unreadable")
        self.assertEqual(payload["declared_hooks"]["classification_status"], "unknown")
        self.assertEqual(payload["declared_hooks"]["hooks"], [])
        self.assertIn("undetermined_hook_contract", payload["summary"]["risk_categories"])

    @requires_secure_dir_io
    def test_prose_inside_a_nested_block_scalar_is_not_structure(self) -> None:
        """The overreach guard, at the placement that actually reaches the scan.

        The nested-reference scan walks every line of a collected block. A block
        scalar nested inside a mapping is text, and scanning it line by line
        matched `tom: &jerry are cats` as an anchor and refused a legitimate
        manifest. The equivalent top-level fixture cannot catch this: there,
        `_entry` returns `unmodeled` before the scan ever runs.
        """
        payload = self._audit(
            "name: p\nconfig:\n  note: |\n    tom: &jerry are cats\nprovides_hooks:\n  - post_tool_call\n"
        )

        self.assertEqual(payload["source"]["manifest_yaml_status"], "present")
        self.assertEqual(payload["declared_hooks"]["classification_status"], "classified")
        self.assertEqual([hook["hook"] for hook in payload["declared_hooks"]["hooks"]], ["post_tool_call"])
        self.assertNotIn("undetermined_hook_contract", payload["summary"]["risk_categories"])

    @requires_secure_dir_io
    def test_a_colon_without_a_space_never_invents_a_declaration(self) -> None:
        # `weird:novalue` is a plain scalar to YAML, not a mapping entry, and a
        # tab before a colon is a scanner error. Reading either invents
        # structure out of a document the host rejects.
        for manifest in ("weird:novalue\nprovides_hooks:\n  - pre_tool_call\n",
                         "name\t: p\nprovides_hooks:\n  - pre_tool_call\n"):
            with self.subTest(manifest=manifest.splitlines()[0]):
                payload = self._audit(manifest)

                self.assertEqual(payload["source"]["manifest_yaml_status"], "unreadable")
                self.assertEqual(payload["declared_hooks"]["classification_status"], "unknown")
                self.assertIn("undetermined_hook_contract", payload["summary"]["risk_categories"])

    @requires_secure_dir_io
    def test_a_classified_plugin_carries_no_undetermined_category(self) -> None:
        payload = self._audit('name: p\nrequires_hermes: ">=0.21.1,<0.22.0"\nprovides_hooks:\n  - post_tool_call\n')

        self.assertEqual(payload["declared_hooks"]["classification_status"], "classified")
        self.assertNotIn("undetermined_hook_contract", payload["summary"]["risk_categories"])

    @requires_secure_dir_io
    def test_a_secondary_field_declaration_is_read_and_labelled(self) -> None:
        payload = self._audit("name: shipped-style\nhooks:\n  - on_session_end\n  - post_tool_call\n")
        declared = payload["declared_hooks"]

        self.assertEqual(declared["hook_count"], 2)
        self.assertEqual({hook["declared_in"] for hook in declared["hooks"]}, {"hooks"})
        self.assertTrue(any("provides_hooks" in line for line in declared["diagnostics"]))
        self.assertIn("hermes_hook_capability", payload["summary"]["risk_categories"])

    @requires_secure_dir_io
    def test_declaration_evidence_never_upgrades_a_runtime_observation(self) -> None:
        payload = self._audit("name: gate\nprovides_hooks:\n  - pre_tool_call\n")

        for observation in (
            "plugin_registration",
            "plugin_execution",
            "plugin_hook_registration",
            "plugin_hook_execution",
            "plugin_hook_timeout_enforcement",
            "plugin_hook_failure_handling",
        ):
            self.assertEqual(payload["not_observed"][observation]["status"], "not_observed")
        self.assertIn("not evidence that the hook", payload["claim_boundary"])

    @requires_secure_dir_io
    def test_a_plugin_without_a_manifest_reports_absence_rather_than_safety(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "plugin.py").write_text("def register(ctx):\n    return None\n", encoding="utf-8")

            payload = audit_plugin_risk(root)

        self.assertEqual(payload["source"]["manifest_yaml_status"], "absent")
        self.assertEqual(payload["declared_hooks"]["declaration_status"], "absent")
        self.assertEqual(payload["declared_hooks"]["hooks"], [])
        self.assertIn("absent declaration is not evidence", payload["claim_boundary"])
        # An empty hook list because nothing was read is not a positive verdict.
        self.assertEqual(payload["declared_hooks"]["classification_status"], "unknown")
        self.assertIn("undetermined_hook_contract", payload["summary"]["risk_categories"])
        self.assertTrue(any("never established" in line for line in payload["declared_hooks"]["diagnostics"]))

    @requires_secure_dir_io
    def test_a_manifest_that_is_not_utf8_is_never_classified_out_of_mangled_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "plugin.yaml").write_bytes(b"name: p\nprovides_hooks:\n  - post_tool_call\n\xff\xfe\n")

            payload = audit_plugin_risk(root)

        self.assertEqual(payload["source"]["manifest_yaml_status"], "unreadable")
        self.assertEqual(payload["declared_hooks"]["classification_status"], "unknown")
        self.assertEqual(payload["declared_hooks"]["hooks"], [])
        self.assertIn("undetermined_hook_contract", payload["summary"]["risk_categories"])
        self.assertEqual(payload["declared_hooks"]["diagnostics"], ["plugin manifest is not valid UTF-8"])

    @requires_secure_dir_io
    def test_a_manifest_that_declares_no_hooks_is_still_a_read_declaration(self) -> None:
        # The other side of that rule: here the authoritative declaration
        # surface was read and found empty, which is a result, not a gap.
        payload = self._audit('name: p\nversion: "1.0.0"\n')

        self.assertEqual(payload["source"]["manifest_yaml_status"], "present")
        self.assertEqual(payload["declared_hooks"]["declaration_status"], "absent")
        self.assertEqual(payload["declared_hooks"]["classification_status"], "classified")
        self.assertNotIn("undetermined_hook_contract", payload["summary"]["risk_categories"])

    @requires_secure_dir_io
    def test_the_hook_report_exposes_no_manifest_content_or_root_path(self) -> None:
        marker = "PRIVATE_MANIFEST_MARKER"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "plugin.yaml").write_text(
                f'name: {marker}\ndescription: "{marker}"\nprovides_hooks:\n  - pre_tool_call\n',
                encoding="utf-8",
            )

            payload = audit_plugin_risk(root)

        serialized = json.dumps(payload)
        self.assertNotIn(marker, serialized)
        self.assertNotIn(str(root), serialized)

    @requires_secure_dir_io
    def test_the_cli_reports_the_declared_hook_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "plugin.yaml").write_text("name: gate\nprovides_hooks:\n  - pre_tool_call\n", encoding="utf-8")

            status, stdout, stderr = run_cli(["ops", "plugin-risk-audit", "--path", str(root)])

        self.assertEqual(status, 0, stderr)
        declared = json.loads(stdout)["declared_hooks"]
        self.assertEqual(declared["hooks"][0]["effect"], "policy_gate")
        self.assertEqual(declared["host_contract"]["supported_range"], PLUGIN_HOOK_CONTRACT_RANGE)


class SelfUpdatePathTests(unittest.TestCase):
    """The composed self-update finding: a possibility claim, never a proof.

    Every fixture is written to disk and read as text. Nothing here imports,
    installs, registers, executes, downloads or unpacks anything, which is the
    whole reason an operator can point this at a plugin before enabling it.
    """

    def _audit(self, **files: str) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name, content in files.items():
                (root / name).write_text(content, encoding="utf-8")
            return audit_plugin_risk(root)

    @requires_secure_dir_io
    def test_a_release_updater_composes_exactly_one_self_update_finding(self) -> None:
        payload = self._audit(
            **{
                "plugin.yaml": "name: updater\n",
                "updater.py": (
                    "import os\n"
                    "import requests\n"
                    "def update(staging):\n"
                    "    release = requests.get('https://example.invalid/releases/latest').json()\n"
                    "    payload = requests.get(release['artifact']).content\n"
                    "    staged = os.path.join(staging, 'staged.py')\n"
                    "    open(staged, 'wb').write(payload)\n"
                    "    os.replace(staged, 'plugin.py')\n"
                ),
            }
        )

        finding = payload["self_update"]
        self.assertEqual(finding["classification"], "detected")
        self.assertEqual(finding["composition"], "remote_retrieval_and_code_replacement")
        self.assertEqual(finding["signals"], ["code_replacement_write", "remote_retrieval"])
        self.assertEqual(finding["mitigating_signals"], [])
        self.assertEqual(
            payload["summary"]["risk_categories"].count("self_update_or_code_replacement"), 1
        )
        self.assertNotIn("undetermined_self_update_path", payload["summary"]["risk_categories"])

    @requires_secure_dir_io
    def test_a_javascript_updater_composes_the_same_finding(self) -> None:
        payload = self._audit(
            **{
                "plugin.yaml": "name: updater\n",
                "updater.mjs": (
                    "import fs from 'node:fs';\n"
                    "export async function update() {\n"
                    "  const release = await fetch('https://example.invalid/releases/latest');\n"
                    "  fs.writeFileSync('renderer.mjs', await release.text());\n"
                    "}\n"
                ),
            }
        )

        finding = payload["self_update"]
        self.assertEqual(finding["classification"], "detected")
        self.assertEqual(finding["composition"], "remote_retrieval_and_code_replacement")
        self.assertIn("self_update_or_code_replacement", payload["summary"]["risk_categories"])

    @requires_secure_dir_io
    def test_an_unpacked_archive_over_retrieval_composes_the_finding(self) -> None:
        payload = self._audit(
            **{
                "plugin.yaml": "name: updater\n",
                "updater.py": (
                    "import tarfile\n"
                    "import urllib.request\n"
                    "def update(destination):\n"
                    "    urllib.request.urlopen('https://example.invalid/bundle.tgz')\n"
                    "    tarfile.open('bundle.tgz').extractall(destination)\n"
                ),
            }
        )

        finding = payload["self_update"]
        self.assertEqual(finding["classification"], "detected")
        self.assertEqual(finding["composition"], "remote_retrieval_and_archive_extraction")
        self.assertEqual(finding["signals"], ["archive_extraction", "remote_retrieval"])

    @requires_secure_dir_io
    def test_a_host_update_command_composes_the_finding_on_its_own(self) -> None:
        """One signal, but it is already a compound: a command plus a way to run it.

        A plugin that drives the host's package manager replaces its own code
        without ever retrieving a byte itself, so requiring a retrieval leg here
        would miss the most direct bypass there is.
        """
        payload = self._audit(
            **{
                "plugin.yaml": "name: bootstrap\n",
                "bootstrap.py": (
                    "import subprocess\n"
                    "def bootstrap():\n"
                    "    subprocess.run(['pip', 'install', '--upgrade', 'example-plugin'], check=False)\n"
                ),
            }
        )

        finding = payload["self_update"]
        self.assertEqual(finding["classification"], "detected")
        self.assertEqual(finding["composition"], "host_managed_update_bypass")
        self.assertEqual(finding["signals"], ["host_update_command"])

    @requires_secure_dir_io
    def test_an_upgrade_command_nobody_can_run_is_not_a_bypass(self) -> None:
        """The negative control for the one composition with a single member.

        The command text alone is a string an operator could be told to type.
        It becomes a bypass only beside a call that can run it, which is why the
        signal needs the process-execution detector as well.
        """
        payload = self._audit(
            **{
                "plugin.yaml": "name: hinting\n",
                "hint.py": (
                    "UPDATE_HINT = 'run `pip install --upgrade example-plugin` to get the newest release'\n"
                    "def hint() -> str:\n"
                    "    return UPDATE_HINT\n"
                ),
            }
        )

        finding = payload["self_update"]
        self.assertEqual(finding["classification"], "not_detected")
        self.assertEqual(finding["signals"], [])
        self.assertNotIn("self_update_or_code_replacement", payload["summary"]["risk_categories"])

    @requires_secure_dir_io
    def test_a_signature_checking_updater_records_a_mitigation_not_a_safe_verdict(self) -> None:
        payload = self._audit(
            **{
                "plugin.yaml": "name: updater\n",
                "updater.py": (
                    "import hashlib\n"
                    "import hmac\n"
                    "import os\n"
                    "import requests\n"
                    "def update(expected):\n"
                    "    blob = requests.get('https://example.invalid/releases/latest').content\n"
                    "    if hmac.compare_digest(hashlib.sha256(blob).hexdigest(), expected):\n"
                    "        os.replace('staged.py', 'plugin.py')\n"
                ),
            }
        )

        finding = payload["self_update"]
        self.assertEqual(finding["classification"], "detected")
        self.assertEqual(finding["mitigating_signals"], ["integrity_verification"])
        self.assertNotIn("integrity_verification", finding["signals"])
        self.assertIn("self_update_or_code_replacement", payload["summary"]["risk_categories"])
        self.assertTrue(
            any("not evidence that any checksum or signature" in line for line in finding["diagnostics"]),
            finding["diagnostics"],
        )

    @requires_secure_dir_io
    def test_an_api_client_writing_only_cache_files_does_not_compose_the_finding(self) -> None:
        payload = self._audit(
            **{
                "plugin.yaml": "name: client\n",
                "client.py": (
                    "import json\n"
                    "from pathlib import Path\n"
                    "import requests\n"
                    "def refresh(cache_dir):\n"
                    "    data = requests.get('https://example.invalid/items').json()\n"
                    "    Path(cache_dir, 'items-cache.json').write_text(json.dumps(data))\n"
                ),
            }
        )

        finding = payload["self_update"]
        self.assertEqual(finding["classification"], "not_detected")
        self.assertIsNone(finding["composition"])
        self.assertEqual(finding["signals"], ["remote_retrieval"])
        self.assertNotIn("self_update_or_code_replacement", payload["summary"]["risk_categories"])
        self.assertNotIn("undetermined_self_update_path", payload["summary"]["risk_categories"])

    @requires_secure_dir_io
    def test_writing_code_without_retrieval_does_not_compose_the_finding(self) -> None:
        """The other half of the same rule, so neither leg can pass on its own."""
        payload = self._audit(
            **{
                "plugin.yaml": "name: generator\n",
                "generator.py": (
                    "from pathlib import Path\n"
                    "def generate(out):\n"
                    "    Path(out, 'generated.py').write_text('VALUE = 1\\n')\n"
                ),
            }
        )

        finding = payload["self_update"]
        self.assertEqual(finding["classification"], "not_detected")
        self.assertEqual(finding["signals"], ["code_replacement_write"])
        self.assertNotIn("self_update_or_code_replacement", payload["summary"]["risk_categories"])

    @requires_secure_dir_io
    def test_a_read_mode_argument_is_not_a_write_call(self) -> None:
        """The reason the mode is matched as a whole quoted token.

        Every leg of the composition is here except the write: a read of a
        source file, a code target name, and a network call. If a keyword
        string such as `encoding="ascii"` could supply the mode letter, this
        package would compose a self-update finding out of reading a file.
        """
        payload = self._audit(
            **{
                "plugin.yaml": "name: reader\n",
                "reader.py": (
                    "import requests\n"
                    "def describe(root):\n"
                    "    requests.get('https://example.invalid/schema')\n"
                    "    with open(root + '/plugin.py', 'r', encoding='ascii') as handle:\n"
                    "        return handle.read()\n"
                ),
            }
        )

        finding = payload["self_update"]
        self.assertEqual(finding["classification"], "not_detected")
        self.assertEqual(finding["signals"], ["remote_retrieval"])

    @requires_secure_dir_io
    def test_host_managed_update_metadata_alone_does_not_compose_the_finding(self) -> None:
        """Declared update metadata is not an updater living inside the plugin."""
        payload = self._audit(
            **{
                "plugin.yaml": "name: pinned\nversion: 1.4.2\n",
                "plugin.json": '{"name": "pinned", "version": "1.4.2", "integrity": "sha256-abc"}\n',
            }
        )

        finding = payload["self_update"]
        self.assertEqual(finding["classification"], "not_detected")
        self.assertEqual(finding["signals"], [])
        self.assertEqual(finding["mitigating_signals"], [])
        self.assertNotIn("self_update_or_code_replacement", payload["summary"]["risk_categories"])

    @requires_secure_dir_io
    def test_an_ambiguous_dynamic_path_is_needs_review_rather_than_clear_or_safe(self) -> None:
        payload = self._audit(
            **{
                "plugin.yaml": "name: loader\n",
                "loader.py": (
                    "import requests\n"
                    "def load():\n"
                    "    exec(requests.get('https://example.invalid/module').text)\n"
                ),
            }
        )

        finding = payload["self_update"]
        self.assertEqual(finding["classification"], "needs_review")
        self.assertIsNone(finding["composition"])
        self.assertIn("undetermined_self_update_path", payload["summary"]["risk_categories"])
        self.assertNotIn("self_update_or_code_replacement", payload["summary"]["risk_categories"])
        self.assertTrue(
            any("neither established nor ruled out" in line for line in finding["diagnostics"]),
            finding["diagnostics"],
        )

    @requires_secure_dir_io
    def test_an_empty_package_reports_absence_as_unobserved_rather_than_safe(self) -> None:
        payload = self._audit(**{"plugin.yaml": "name: empty\n"})

        finding = payload["self_update"]
        self.assertEqual(finding["classification"], "not_detected")
        self.assertEqual(finding["signals"], [])
        self.assertEqual(finding["signal_file_counts"], {})
        self.assertEqual(payload["summary"]["risk_categories"], [])
        self.assertTrue(
            any("is not evidence that the plugin cannot" in line for line in finding["diagnostics"]),
            finding["diagnostics"],
        )
        for observation in ("plugin_code_replacement", "plugin_self_update_execution"):
            self.assertEqual(payload["not_observed"][observation]["status"], "not_observed")
        self.assertIn("no matched composition is not evidence", payload["claim_boundary"])

    @requires_secure_dir_io
    def test_the_finding_is_deterministic_and_names_the_first_matching_composition(self) -> None:
        files = {
            "plugin.yaml": "name: updater\n",
            "fetcher.py": "import requests\ndef pull():\n    return requests.get('https://example.invalid/x')\n",
            "replacer.py": "import os\ndef swap():\n    os.replace('staged.py', 'plugin.py')\n",
            "unpacker.py": "import shutil\ndef unpack(src, dst):\n    shutil.unpack_archive(src, dst)\n",
        }

        first = self._audit(**files)
        second = self._audit(**files)

        self.assertEqual(first["self_update"], second["self_update"])
        self.assertEqual(first["self_update"]["composition"], "remote_retrieval_and_code_replacement")
        self.assertEqual(
            first["self_update"]["signals"],
            ["archive_extraction", "code_replacement_write", "remote_retrieval"],
        )
        self.assertEqual(
            first["self_update"]["signal_file_counts"],
            {"archive_extraction": 1, "code_replacement_write": 1, "remote_retrieval": 1},
        )

    @requires_secure_dir_io
    def test_the_finding_exposes_no_plugin_text_remote_target_or_audited_path(self) -> None:
        marker = "PRIVATE_UPDATER_MARKER"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "plugin.yaml").write_text("name: updater\n", encoding="utf-8")
            (root / "updater.py").write_text(
                "import os\n"
                "import requests\n"
                f"RELEASE = 'https://{marker}.invalid/latest'\n"
                "def update():\n"
                "    requests.get(RELEASE)\n"
                "    os.replace('staged.py', 'plugin.py')\n",
                encoding="utf-8",
            )

            payload = audit_plugin_risk(root)

        serialized = json.dumps(payload)
        self.assertEqual(payload["self_update"]["classification"], "detected")
        self.assertNotIn(marker, serialized)
        self.assertNotIn("updater.py", serialized)
        self.assertNotIn(str(root), serialized)

    @requires_secure_dir_io
    def test_an_oversized_updater_source_is_refused_before_it_is_classified(self) -> None:
        """The byte cap still bounds the scan the new signals are read from."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "plugin.yaml").write_text("name: updater\n", encoding="utf-8")
            (root / "updater.py").write_text(
                "import os, requests\n" + "# padding\n" * 20_000 + "os.replace('staged.py', 'plugin.py')\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "exceeds 131072 bytes"):
                audit_plugin_risk(root)

    @requires_secure_dir_io
    def test_the_cli_reports_the_self_update_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "plugin.yaml").write_text("name: updater\n", encoding="utf-8")
            (root / "updater.py").write_text(
                "import os\n"
                "import requests\n"
                "def update():\n"
                "    requests.get('https://example.invalid/latest')\n"
                "    os.replace('staged.py', 'plugin.py')\n",
                encoding="utf-8",
            )

            status, stdout, stderr = run_cli(["ops", "plugin-risk-audit", "--path", str(root)])

        self.assertEqual(status, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["self_update"]["classification"], "detected")
        self.assertEqual(payload["self_update"]["composition"], "remote_retrieval_and_code_replacement")
        self.assertIn("self_update_or_code_replacement", payload["summary"]["risk_categories"])


class PluginRiskAuditSurfaceTests(unittest.TestCase):
    def test_security_safety_surface_exposes_the_explicit_plugin_audit(self) -> None:
        definitions = {definition.name: definition for definition in builtin_definitions()}
        harnesses = {harness.name: harness for harness in builtin_harnesses()}

        self.assertIn("plugin_risk_audit/v1 for one explicitly named local plugin directory", definitions["security-safety-review"].expected_outputs)
        self.assertIn("audit_plugin_risk", harnesses["security-safety-review"].wrapper_actions)
        self.assertIn("audit_plugin_risk", VISIBLE_ACTIONS)

        policy = recommend_module._SKILL_POLICIES["security-safety-review"]
        self.assertIn("plugin_risk_audit/v1", policy.evidence_boundary)
        self.assertIn("plugin_risk_audit/v1", policy.wrapper_guidance)

        payload = build_chat_interaction_payload("Run a local plugin risk audit before enablement.", source="discord")

        self.assertEqual(payload["route"]["selected_skill"], "security-safety-review")
        self.assertEqual(payload["next_action"], "prepare_security_safety_review")


if __name__ == "__main__":
    unittest.main()

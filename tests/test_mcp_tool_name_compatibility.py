from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from _credential_fixtures import AWS_ACCESS_KEY_ID
from _local_package import load_local_package


load_local_package()
from omh.workflows.mcp_tool_name_compatibility import (
    MCP_TOOL_NAMING_ADAPTERS,
    McpToolNameCompatibilityError,
    audit_mcp_tool_name_compatibility,
    build_mcp_tool_name_compatibility_report,
)


def snapshot(*names: str, observation: str = "registered", logical_name: str = "omh_status") -> dict:
    return {
        "schema_version": "mcp_tool_name_compatibility_snapshot/v1",
        "target_harness": "opencode",
        "adapter": {"id": "opencode-mcp-tool-naming", "version": "v1"},
        "required_tools": [{"logical_name": logical_name, "evidence_refs": ["run:required"]}],
        "advertised_tools": [
            {"name": name, "observation": observation, "evidence_refs": [f"session:observed-{index}"]}
            for index, name in enumerate(names)
        ],
    }


class McpToolNameCompatibilityTests(unittest.TestCase):
    def report(self, value: dict) -> dict:
        return build_mcp_tool_name_compatibility_report((value,))

    def row(self, value: dict) -> dict:
        return self.report(value)["rows"][0]

    def test_exact_match_retains_adapter_and_evidence(self) -> None:
        value = snapshot("omh_status")
        report = self.report(value)
        self.assertEqual(report["schema_version"], "mcp_tool_name_compatibility/v1")
        self.assertEqual(report["adapter"], value["adapter"])
        row = report["rows"][0]
        self.assertEqual(row["state"], "exact")
        self.assertEqual(row["evidence_refs"], ["run:required", "session:observed-0"])
        self.assertEqual(row["candidates"], [])

    def test_exact_precedence_short_circuits_derivation(self) -> None:
        with patch("omh.workflows.mcp_tool_name_compatibility.NamingRules.normalize", side_effect=AssertionError):
            row = self.row(snapshot("omh_status", "omh.status"))
        self.assertEqual(row["state"], "exact")
        self.assertEqual(row["candidates"], [])

    def test_separator_difference_is_not_invented_compatibility(self) -> None:
        self.assertEqual(self.row(snapshot("omh-status"))["state"], "missing")

    def test_punctuation_difference_is_one_to_one(self) -> None:
        row = self.row(snapshot("omh.status"))
        self.assertEqual(row["state"], "compatible_one_to_one")
        self.assertEqual(row["candidates"], ["omh.status"])

    def test_sanitized_name_collision_is_ambiguous_never_selected(self) -> None:
        row = self.row(snapshot("omh/status", "omh.status"))
        self.assertEqual(row["state"], "ambiguous")
        self.assertEqual(row["candidates"], ["omh.status", "omh/status"])
        self.assertFalse(row["auto_selected"])
        self.assertNotIn("selected_name", row)
        self.assertNotIn("recommended", row)

    def test_overlength_name_is_reported_without_truncation_even_if_exact(self) -> None:
        name = "x" * 129
        row = self.row(snapshot(name, logical_name=name))
        self.assertEqual(row["state"], "unobserved")
        self.assertEqual(row["reason"], "name_exceeds_audit_limit")
        self.assertEqual(row["logical_name"], name)
        self.assertEqual(row["candidates"], [])

    def test_missing_tool_does_not_read_first_party_or_recorded_sessions(self) -> None:
        with (
            patch("omh.mcp.bridge.mcp_tool_definitions", side_effect=AssertionError),
            patch("omh.mcp.bridge.read_mcp_host_sessions", side_effect=AssertionError),
            patch("omh.mcp.bridge.record_mcp_host_session", side_effect=AssertionError),
        ):
            self.assertEqual(self.row(snapshot(logical_name="omh_hud"))["state"], "missing")

    def test_config_only_never_proves_compatibility(self) -> None:
        for name in ("omh_status", "omh.status"):
            with self.subTest(name=name):
                row = self.row(snapshot(name, observation="config_only"))
                self.assertEqual(row["state"], "unobserved")
                self.assertEqual(row["reason"], "config_only")
                self.assertEqual(row["evidence_refs"], ["run:required", "session:observed-0"])
        value = snapshot("omh.status")
        value["advertised_tools"] += snapshot("omh_status", observation="config_only")["advertised_tools"]
        self.assertEqual(self.row(value)["state"], "compatible_one_to_one")

    def test_unknown_adapter_version_and_wrong_host_fail_closed_even_with_exact(self) -> None:
        for field, replacement in (("version", "v999"), ("id", "unknown")):
            value = snapshot("omh_status")
            value["adapter"][field] = replacement
            row = self.row(value)
            self.assertEqual((row["state"], row["reason"]), ("unobserved", "unsupported_adapter"))
        for host in ("claude-code", "codex", "cursor", "generic"):
            value = snapshot("omh_status")
            value["target_harness"] = host
            self.assertEqual(self.row(value)["reason"], "unsupported_adapter")
        with self.assertRaises(TypeError):
            MCP_TOOL_NAMING_ADAPTERS[("new", "v1")] = object()

    def test_repeated_output_and_permuted_input_are_byte_identical(self) -> None:
        value = snapshot("omh/status", "omh.status")
        value["required_tools"] += [
            {"logical_name": name, "evidence_refs": ["run:b", "run:a"]} for name in ("z_last", "a_first")
        ]
        original = deepcopy(value)
        first = json.dumps(self.report(value), sort_keys=True)
        self.assertEqual(value, original)
        value["advertised_tools"].reverse()
        value["required_tools"].reverse()
        for tool in value["required_tools"]:
            tool["evidence_refs"].reverse()
        self.assertEqual(first, json.dumps(self.report(value), sort_keys=True))
        self.assertEqual([r["logical_name"] for r in self.report(value)["rows"]], ["a_first", "omh_status", "z_last"])

    def test_multiple_harnesses_are_isolated_and_order_independent(self) -> None:
        first = snapshot("omh_status")
        second = snapshot()
        second["target_harness"] = "other"
        second["adapter"]["id"] = "other-mcp-tool-naming"
        report = build_mcp_tool_name_compatibility_report((first, second))
        self.assertEqual(report, build_mcp_tool_name_compatibility_report((second, first)))
        self.assertEqual(len(report["rows"]), 2)
        self.assertEqual({r["target_harness"]: r["state"] for r in report["rows"]}, {"opencode": "exact", "other": "unobserved"})
        with self.assertRaises(McpToolNameCompatibilityError):
            build_mcp_tool_name_compatibility_report((first, first))

    def test_closed_schema_rejects_unknown_fields_and_duplicate_identities(self) -> None:
        values = []
        for location in ((), ("adapter",), ("required_tools", 0), ("advertised_tools", 0)):
            for forbidden in ("arguments", "results", "credentials", "prompt", "api_key", "extra"):
                value = snapshot("omh_status")
                target = value
                for key in location:
                    target = target[key]
                target[forbidden] = AWS_ACCESS_KEY_ID
                values.append(value)
        for field in ("required_tools", "advertised_tools"):
            value = snapshot("omh_status")
            value[field] *= 2
            values.append(value)
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(McpToolNameCompatibilityError) as caught:
                    self.report(value)
                self.assertNotIn(AWS_ACCESS_KEY_ID, str(caught.exception))

    def test_secret_strings_and_nonopaque_evidence_never_echo(self) -> None:
        for unsafe in (AWS_ACCESS_KEY_ID, "api_key", "raw\nprompt", "```body```", "config:~/.config"):
            for location in (("target_harness",), ("adapter", "id"), ("adapter", "version"),
                             ("required_tools", 0, "logical_name"), ("advertised_tools", 0, "name"),
                             ("advertised_tools", 0, "observation"), ("required_tools", 0, "evidence_refs", 0),
                             ("advertised_tools", 0, "evidence_refs", 0)):
                value = snapshot("omh_status")
                target = value
                for key in location[:-1]:
                    target = target[key]
                target[location[-1]] = unsafe
                with self.subTest(location=location, unsafe=unsafe):
                    with self.assertRaises(McpToolNameCompatibilityError) as caught:
                        self.report(value)
                    self.assertNotIn(unsafe, str(caught.exception))

    def test_schema_types_and_collection_bounds_are_fail_closed(self) -> None:
        for field, replacement in (("schema_version", "v2"), ("required_tools", {}),
                                   ("advertised_tools", [None]), ("adapter", []),
                                   ("required_tools", snapshot()["required_tools"] * 257)):
            value = snapshot()
            value[field] = replacement
            with self.subTest(field=field, replacement=replacement):
                with self.assertRaises(McpToolNameCompatibilityError):
                    self.report(value)
        for values in ((), (snapshot(),) * 21):
            with self.assertRaises(McpToolNameCompatibilityError):
                build_mcp_tool_name_compatibility_report(values)
        for refs in ("run:x", ["run:x"] * 33, [1]):
            value = snapshot()
            value["required_tools"][0]["evidence_refs"] = refs
            with self.assertRaises(McpToolNameCompatibilityError):
                self.report(value)

    def test_explicit_file_reads_are_bounded_and_fail_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = root / "snapshot.json"
            path.write_text(json.dumps(snapshot("omh_status")), encoding="utf-8")
            self.assertEqual(audit_mcp_tool_name_compatibility((path,)), self.report(snapshot("omh_status")))
            for content in ('{"schema_version":', '{"x":1,"x":2}', "[" * 2000, "x" * 131073, "\ud800"):
                path.write_bytes(content.encode("utf-8", errors="surrogatepass"))
                with self.subTest(content_length=len(content)):
                    with self.assertRaises(McpToolNameCompatibilityError) as caught:
                        audit_mcp_tool_name_compatibility((path,))
                    self.assertLess(len(str(caught.exception)), 200)
            for missing in (root / "absent.json", root, root / f"{AWS_ACCESS_KEY_ID}.json"):
                with self.assertRaises(McpToolNameCompatibilityError) as caught:
                    audit_mcp_tool_name_compatibility((missing,))
                self.assertNotIn(AWS_ACCESS_KEY_ID, str(caught.exception))


class McpToolNameCompatibilityCliTests(unittest.TestCase):
    fixtures = Path(__file__).parent / "fixtures" / "mcp_tool_name_compat"

    def cli(self, *paths: Path) -> tuple[int, str, str]:
        from _cli_harness import run_cli

        args = ["harness", "mcp-tool-name-compatibility"]
        for path in paths:
            args.extend(["--snapshot", str(path)])
        return run_cli(args)

    def test_cli_fixture_matrix_and_bounded_errors(self) -> None:
        for fixture, state in (
            ("exact", "exact"), ("registered", "exact"), ("config_only", "unobserved"),
            ("precedence", "exact"), ("one_candidate", "compatible_one_to_one"),
            ("ambiguous", "ambiguous"), ("scoped", "missing"),
            ("separator_difference", "missing"), ("overlength", "unobserved"),
            ("unsupported", "unobserved"),
        ):
            with self.subTest(fixture=fixture):
                status, stdout, stderr = self.cli(self.fixtures / f"{fixture}.json")
                self.assertEqual(status, 0, stderr)
                self.assertEqual(stderr, "")
                payload = json.loads(stdout)
                self.assertEqual(payload["schema_version"], "mcp_tool_name_compatibility/v1")
                self.assertTrue(all(row["state"] == state for row in payload["rows"]))
                self.assertTrue(all(not row["auto_selected"] for row in payload["rows"]))
        for fixture in ("absent", "malformed", "oversized", "secret"):
            with self.subTest(fixture=fixture):
                status, stdout, stderr = self.cli(self.fixtures / f"{fixture}.json")
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertLess(len(stderr), 250)
                self.assertNotIn("AKIA", stderr)
                self.assertNotIn("Traceback", stderr)

    def test_cli_is_byte_identical_read_only_and_metadata_only(self) -> None:
        path = self.fixtures / "exact.json"
        before = {entry.name: entry.read_bytes() for entry in self.fixtures.iterdir()}
        first = self.cli(path)
        self.assertEqual(first, self.cli(path))
        self.assertEqual(first[0], 0, first[2])
        payload = json.loads(first[1])
        self.assertEqual(len(payload["rows"]), 3)
        self.assertEqual([row["logical_name"] for row in payload["rows"]], ["omh_hud", "omh_recommend", "omh_status"])
        self.assertEqual(payload["adapter"], json.loads(path.read_text(encoding="utf-8"))["adapter"])
        self.assertTrue(all("run:required" in row["evidence_refs"] for row in payload["rows"]))

        def keys(value: object) -> set[str]:
            if isinstance(value, dict):
                return set(value).union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value))
            return set()

        self.assertFalse(keys(payload) & {"arguments", "results", "credentials", "prompt"})
        self.assertEqual(before, {entry.name: entry.read_bytes() for entry in self.fixtures.iterdir()})

    def test_cli_repeated_snapshot_option_preserves_harness_scope(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "other.json"
            value = snapshot()
            value["target_harness"] = "generic"
            value["adapter"]["id"] = "generic-mcp-tool-naming"
            path.write_text(json.dumps(value), encoding="utf-8")
            status, stdout, stderr = self.cli(self.fixtures / "exact.json", path)
            self.assertEqual(status, 0, stderr)
            rows = json.loads(stdout)["rows"]
            self.assertEqual(len(rows), 4)
            self.assertEqual([row["state"] for row in rows if row["target_harness"] == "generic"], ["unobserved"])


class McpToolNameCompatibilityCatalogTests(unittest.TestCase):
    def test_inventory_contract_declares_report_without_replacing_wrapper_schema(self) -> None:
        from omh.skills.catalog import builtin_definitions, harness_definition
        from omh.wrapper.contract import build_chat_interaction_payload

        harness = harness_definition("harness-session-inventory")
        self.assertIn("mcp_tool_name_compatibility/v1", harness.expected_outputs)
        self.assertIn("mcp_tool_name_compatibility_recorded_when_available", harness.evidence_ladder)
        skill = next(value for value in builtin_definitions() if value.name == "harness-session-inventory")
        self.assertIn("mcp_tool_name_compatibility/v1", skill.expected_outputs)
        payload = build_chat_interaction_payload("harness-session-inventory", source="discord")
        state = payload["chat_response"]["state"]
        self.assertEqual(state["artifact_schema"], "harness_session_inventory/v1")
        flow = state["recommended_flow"]
        self.assertEqual(flow[flow.index("redact_mcp_and_connector_configs") + 1], "resolve_mcp_tool_name_compatibility")


if __name__ == "__main__":
    unittest.main()

"""The hook-contract projection and the bounded manifest reader under it.

Two things are being guarded here.

The first is that the shipped mapping still says what it was transcribed to say.
`EXPECTED_HERMES_HOOKS` is written out by hand: it is the 37 names of
`VALID_HOOKS` at hermes-agent `v2026.9.7` (hermes_cli/plugins.py:107-188), and
the classification groups below are the three dispatcher sets at that same
revision (hermes_cli/plugins_dispatch.py:41, 48, 50) plus the streaming
observers that never touch the caller's thread (agent/plugin_stream_hooks.py:1-7).

Be precise about what that buys, because it is easy to overclaim. These lists
are a *second hand copy of the same reading*, so comparing them against the
mapping detects an unintended local edit to either side and nothing else. It is
a change-detector on the table, not a drift-detector on the host: nothing in
this repository reads hermes-agent, so a hook Hermes adds tomorrow leaves this
suite green and classifies `unknown` at run time. Two copies written from one
reading also agree by construction, so passing here is not evidence that the
reading was right -- only the cited host source is that.

`test_the_pin_moves_with_the_tested_hermes_version` is the one assertion in this
file that is not a copy checked against a copy: it holds the hook table's pinned
version against `HERMES_COMPAT_MATRIX`, which is maintained separately and for
another purpose, so bumping the tested Hermes version without re-reading the
hook contract fails.

The second is that every way a hook declaration can be wrong stays a bounded
diagnostic rather than a crash, a guess, or a classification that quietly
assumes the best.
"""
from __future__ import annotations

import unittest

from omh.install.plugin_compat import HERMES_COMPAT_MATRIX
from omh.workflows.plugin_hook_contract import (
    HERMES_HOOK_CONTRACTS,
    HOOK_EFFECTS,
    HOOK_HOST_CONTRACTS,
    HOOK_TIMEOUT_SEMANTICS,
    MAX_DECLARED_HOOKS,
    PLUGIN_HOOK_CONTRACT_RANGE,
    PLUGIN_HOOK_CONTRACT_VERSION,
    PluginHookDeclarationError,
    hook_contract,
    host_range_status,
    read_hook_declaration,
)
from omh.workflows.plugin_manifest_yaml import (
    MAX_MANIFEST_LINE_CHARS,
    MAX_MANIFEST_LINES,
    PluginManifestFormatError,
    entry_values,
    read_plugin_manifest_entries,
)


# hermes_cli/plugins.py:107-188 at tag v2026.9.7 (Hermes 0.21.1).
EXPECTED_HERMES_HOOKS = frozenset(
    {
        "api_request_error",
        "gateway_platform_event",
        "kanban_task_blocked",
        "kanban_task_claimed",
        "kanban_task_completed",
        "on_interim_message",
        "on_kanban_dispatch_tick",
        "on_kanban_task_updated",
        "on_kanban_worker_exited",
        "on_kanban_worker_spawned",
        "on_kanban_worker_stale_claim",
        "on_session_end",
        "on_session_finalize",
        "on_session_reset",
        "on_session_start",
        "on_skill_lifecycle",
        "on_stream_delta",
        "on_stream_end",
        "on_stream_start",
        "post_api_request",
        "post_approval_response",
        "post_llm_call",
        "post_tool_call",
        "pre_api_request",
        "pre_approval_request",
        "pre_command",
        "pre_gateway_dispatch",
        "pre_llm_call",
        "pre_tool_call",
        "pre_transcription",
        "pre_verify",
        "subagent_start",
        "subagent_stop",
        "transform_api_error_classification",
        "transform_llm_output",
        "transform_terminal_output",
        "transform_tool_result",
    }
)
# hermes_cli/plugins_dispatch.py:41 plus the one fail-closed policy hook at :48.
EXPECTED_BOUNDED_HOOKS = frozenset(
    {
        "api_request_error",
        "on_session_end",
        "on_session_start",
        "post_api_request",
        "post_llm_call",
        "post_tool_call",
        "pre_api_request",
        "pre_llm_call",
        "pre_tool_call",
        "pre_verify",
        "transform_llm_output",
        "transform_terminal_output",
        "transform_tool_result",
    }
)
# hermes_cli/plugins_dispatch.py:48 -- the only hook whose timeout blocks.
EXPECTED_FAIL_CLOSED_HOOKS = frozenset({"pre_tool_call"})
# agent/plugin_stream_hooks.py:1-7, enqueued at agent/stream_delivery.py:38-45.
EXPECTED_QUEUED_HOOKS = frozenset({"on_interim_message", "on_stream_delta", "on_stream_end", "on_stream_start"})
# Hooks whose consumed return can stop, refuse, or redirect the action.
EXPECTED_POLICY_GATES = frozenset({"pre_gateway_dispatch", "pre_tool_call", "pre_verify"})
# website/docs/user-guide/features/hooks.md:439-477, "Transform" rows.
EXPECTED_TRANSFORMERS = frozenset(
    {
        "pre_transcription",
        "transform_api_error_classification",
        "transform_llm_output",
        "transform_terminal_output",
        "transform_tool_result",
    }
)


def _manifest(*lines: str) -> str:
    return "\n".join(lines) + "\n"


class HookVocabularyPinTests(unittest.TestCase):
    def test_mapping_covers_exactly_the_supported_host_hook_vocabulary(self) -> None:
        self.assertEqual(set(HERMES_HOOK_CONTRACTS), set(EXPECTED_HERMES_HOOKS))
        self.assertEqual(len(HERMES_HOOK_CONTRACTS), 37)

    def test_every_classification_uses_a_closed_value(self) -> None:
        for name, contract in HERMES_HOOK_CONTRACTS.items():
            with self.subTest(hook=name):
                self.assertIn(contract.effect, HOOK_EFFECTS)
                self.assertIn(contract.host_contract, HOOK_HOST_CONTRACTS)
                self.assertIn(contract.timeout_semantics, HOOK_TIMEOUT_SEMANTICS)
                self.assertNotEqual(contract.effect, "unknown")

    def test_bounded_and_queued_contracts_match_the_host_dispatcher(self) -> None:
        bounded = {name for name, c in HERMES_HOOK_CONTRACTS.items() if c.host_contract == "bounded"}
        queued = {name for name, c in HERMES_HOOK_CONTRACTS.items() if c.host_contract == "queued_worker"}
        self.assertEqual(bounded, set(EXPECTED_BOUNDED_HOOKS))
        self.assertEqual(queued, set(EXPECTED_QUEUED_HOOKS))

    def test_only_the_host_fail_closed_hook_fails_closed(self) -> None:
        fail_closed = {name for name, c in HERMES_HOOK_CONTRACTS.items() if c.timeout_semantics == "fail_closed"}
        self.assertEqual(fail_closed, set(EXPECTED_FAIL_CLOSED_HOOKS))

    def test_timeout_semantics_follow_from_the_host_contract(self) -> None:
        """A consistency rule over the table, not verification against the host.

        `expected` is computed from the same row's `host_contract`, so this
        cannot tell you a row is right -- it tells you the row is coherent: a
        bounded hook names a fail mode, and a hook the host never bounds does
        not invent one. The reading itself is only backed by the cited source.
        """
        for name, contract in HERMES_HOOK_CONTRACTS.items():
            with self.subTest(hook=name):
                if contract.host_contract == "bounded":
                    expected = "fail_closed" if name in EXPECTED_FAIL_CLOSED_HOOKS else "fail_open"
                else:
                    # Nothing outside the host's timeout allowlist runs under a
                    # host timeout, so naming a fail mode there would invent one.
                    expected = "not_applicable"
                self.assertEqual(contract.timeout_semantics, expected)

    def test_policy_gates_and_transformers_are_the_host_categories(self) -> None:
        gates = {name for name, c in HERMES_HOOK_CONTRACTS.items() if c.effect == "policy_gate"}
        transformers = {name for name, c in HERMES_HOOK_CONTRACTS.items() if c.effect == "result_transformer"}
        self.assertEqual(gates, set(EXPECTED_POLICY_GATES))
        self.assertEqual(transformers, set(EXPECTED_TRANSFORMERS))

    def test_pre_llm_call_contributes_context_rather_than_gating(self) -> None:
        # It is "Directive/control" to the host, but its consumed return is
        # joined into the user message; it cannot refuse the call.
        self.assertEqual(HERMES_HOOK_CONTRACTS["pre_llm_call"].effect, "prompt_context_contributor")

    def test_an_unmapped_hook_name_is_unknown_rather_than_absent(self) -> None:
        contract = hook_contract("some_future_hook")
        self.assertEqual(contract.effect, "unknown")
        self.assertEqual(contract.host_contract, "unknown")
        self.assertEqual(contract.timeout_semantics, "unknown")

    def test_the_pin_moves_with_the_tested_hermes_version(self) -> None:
        """Hold the hook table against a fact maintained somewhere else.

        `HERMES_COMPAT_MATRIX` records the Hermes version this repository has
        actually tested its plugin bundle against, and already names
        `VALID_HOOKS` among the host contracts that version covers. It is not
        derived from the hook mapping and is not maintained for it, so requiring
        the two to agree is a real check rather than one copy confirming
        another: bumping the tested version without re-reading the hook contract
        fails here.
        """
        versions = {entry["version"] for entry in HERMES_COMPAT_MATRIX}
        self.assertIn(
            PLUGIN_HOOK_CONTRACT_VERSION,
            versions,
            "the hook contract is pinned to a Hermes version this repository does not test against; re-read "
            "VALID_HOOKS and the dispatcher sets at the tested version, then update the mapping and this file",
        )
        covering = [entry for entry in HERMES_COMPAT_MATRIX if entry["version"] == PLUGIN_HOOK_CONTRACT_VERSION]
        self.assertTrue(
            any("VALID_HOOKS" in entry["host_contracts"] for entry in covering),
            "the tested Hermes version no longer claims VALID_HOOKS as a covered host contract, so the hook "
            "mapping has nothing to stand on",
        )

    def test_the_pinned_version_satisfies_the_supported_range(self) -> None:
        self.assertEqual(host_range_status(PLUGIN_HOOK_CONTRACT_RANGE), "supported")
        self.assertEqual(host_range_status(f">={PLUGIN_HOOK_CONTRACT_VERSION}"), "supported")
        self.assertEqual(host_range_status(">=0.22.0,<0.23.0"), "unsupported")
        self.assertEqual(host_range_status("whatever"), "unparsable")
        self.assertEqual(host_range_status(None), "undeclared")

    def test_an_unsupported_host_revision_downgrades_every_hook_to_unknown(self) -> None:
        self.assertEqual(hook_contract("pre_tool_call", range_status="unsupported").effect, "unknown")
        self.assertEqual(hook_contract("pre_tool_call", range_status="unparsable").effect, "unknown")
        self.assertEqual(hook_contract("pre_tool_call", range_status="undeclared").effect, "policy_gate")


class ManifestReaderTests(unittest.TestCase):
    def test_reads_top_level_scalars_and_block_sequences(self) -> None:
        entries = read_plugin_manifest_entries(
            _manifest(
                "# a comment",
                "name: example",
                'requires_hermes: ">=0.21.1,<0.22.0"',
                "provides_hooks:",
                "  - pre_tool_call",
                "  - post_tool_call",
                "",
            )
        )
        self.assertEqual([entry.key for entry in entries], ["name", "requires_hermes", "provides_hooks"])
        self.assertEqual(entry_values(entries, "name")[0].scalar, "example")
        self.assertEqual(entry_values(entries, "requires_hermes")[0].scalar, ">=0.21.1,<0.22.0")
        self.assertEqual(entry_values(entries, "provides_hooks")[0].sequence, ("pre_tool_call", "post_tool_call"))

    def test_constructs_outside_the_subset_are_unmodeled_not_guessed(self) -> None:
        entries = read_plugin_manifest_entries(
            _manifest(
                "description: >-",
                "  a folded block scalar",
                "  spanning lines",
                "config_schema:",
                "  retries: 3",
                "tags: [a, b]",
                "provides_hooks:",
                "  - pre_tool_call",
            )
        )
        self.assertEqual(entry_values(entries, "description")[0].kind, "unmodeled")
        self.assertEqual(entry_values(entries, "config_schema")[0].kind, "unmodeled")
        self.assertEqual(entry_values(entries, "tags")[0].kind, "unmodeled")
        self.assertEqual(entry_values(entries, "provides_hooks")[0].kind, "sequence")

    def test_a_trailing_comment_ends_a_plain_scalar(self) -> None:
        entries = read_plugin_manifest_entries(_manifest("version: 1.0.0  # shipped"))
        self.assertEqual(entry_values(entries, "version")[0].scalar, "1.0.0")

    def test_duplicate_keys_are_preserved_for_the_caller_to_judge(self) -> None:
        entries = read_plugin_manifest_entries(_manifest("name: one", "name: two"))
        self.assertEqual([entry.scalar for entry in entry_values(entries, "name")], ["one", "two"])

    def test_oversized_and_malformed_documents_are_refused(self) -> None:
        with self.assertRaisesRegex(PluginManifestFormatError, "lines"):
            read_plugin_manifest_entries("name: x\n" * (MAX_MANIFEST_LINES + 1))
        with self.assertRaisesRegex(PluginManifestFormatError, "characters"):
            read_plugin_manifest_entries("name: " + "x" * (MAX_MANIFEST_LINE_CHARS + 1) + "\n")
        with self.assertRaisesRegex(PluginManifestFormatError, "tab"):
            read_plugin_manifest_entries(_manifest("provides_hooks:", "\t- pre_tool_call"))
        with self.assertRaisesRegex(PluginManifestFormatError, "mapping entry"):
            read_plugin_manifest_entries(_manifest("- pre_tool_call"))


class HookDeclarationTests(unittest.TestCase):
    def test_reads_and_classifies_a_declared_hook_list(self) -> None:
        declaration = read_hook_declaration(
            _manifest(
                'requires_hermes: ">=0.21.1,<0.22.0"',
                "provides_hooks:",
                "  - post_tool_call",
                "  - pre_tool_call",
            )
        )
        self.assertEqual(declaration.range_status, "supported")
        self.assertEqual([hook.name for hook in declaration.hooks], ["post_tool_call", "pre_tool_call"])
        self.assertEqual(declaration.hooks[1].contract.effect, "policy_gate")
        self.assertEqual(declaration.hooks[1].contract.timeout_semantics, "fail_closed")

    def test_hook_order_is_deterministic_regardless_of_declaration_order(self) -> None:
        forward = read_hook_declaration(_manifest("provides_hooks:", "  - post_tool_call", "  - on_session_end"))
        reverse = read_hook_declaration(_manifest("provides_hooks:", "  - on_session_end", "  - post_tool_call"))
        self.assertEqual([hook.name for hook in forward.hooks], [hook.name for hook in reverse.hooks])

    def test_the_secondary_field_is_read_and_labelled_as_such(self) -> None:
        declaration = read_hook_declaration(_manifest("hooks:", "  - on_session_end"))
        self.assertEqual(declaration.hooks[0].field, "hooks")
        self.assertEqual(declaration.hooks[0].contract.effect, "lifecycle_callback")

    def test_the_canonical_field_wins_when_both_declare_one_hook(self) -> None:
        declaration = read_hook_declaration(
            _manifest("hooks:", "  - on_session_end", "provides_hooks:", "  - on_session_end")
        )
        self.assertEqual([(hook.name, hook.field) for hook in declaration.hooks], [("on_session_end", "provides_hooks")])

    def test_malformed_hook_declarations_fail_with_bounded_diagnostics(self) -> None:
        cases = {
            "must be a bounded list": _manifest("provides_hooks: pre_tool_call"),
            "must be declared once": _manifest(
                "provides_hooks:", "  - pre_tool_call", "provides_hooks:", "  - post_tool_call"
            ),
            "must not repeat": _manifest("provides_hooks:", "  - pre_tool_call", "  - pre_tool_call"),
            "plain hook names": _manifest("provides_hooks:", "  - Pre Tool Call!"),
            "at most": _manifest("provides_hooks:", *[f"  - hook_{index}" for index in range(MAX_DECLARED_HOOKS + 1)]),
        }
        for expected, manifest in cases.items():
            with self.subTest(case=expected):
                with self.assertRaisesRegex(PluginHookDeclarationError, expected) as caught:
                    read_hook_declaration(manifest)
                self.assertLess(len(str(caught.exception)), 200)

    def test_a_non_list_hook_declaration_is_refused_even_when_empty(self) -> None:
        with self.assertRaisesRegex(PluginHookDeclarationError, "bounded list"):
            read_hook_declaration(_manifest("provides_hooks:"))

    def test_an_unreadable_document_raises_the_document_error_not_the_declaration_one(self) -> None:
        # The caller separates "the manifest could not be read" from "the hook
        # declaration is wrong", so the two must not arrive as one exception.
        with self.assertRaises(PluginManifestFormatError):
            read_hook_declaration(_manifest("provides_hooks:", "\t- pre_tool_call"))

    def test_a_repeated_or_empty_version_range_is_refused(self) -> None:
        with self.assertRaisesRegex(PluginHookDeclarationError, "declared once"):
            read_hook_declaration(_manifest('requires_hermes: ">=0.21.1"', 'requires_hermes: ">=0.21.1"'))
        with self.assertRaisesRegex(PluginHookDeclarationError, "version range"):
            read_hook_declaration(_manifest("requires_hermes:"))

    def test_an_unknown_hook_name_never_borrows_a_neighbour_classification(self) -> None:
        declaration = read_hook_declaration(
            _manifest("provides_hooks:", "  - pre_tool_call", "  - pre_tool_call_v2")
        )
        contracts = {hook.name: hook.contract for hook in declaration.hooks}
        self.assertEqual(contracts["pre_tool_call"].effect, "policy_gate")
        self.assertEqual(contracts["pre_tool_call_v2"].effect, "unknown")

    def test_an_unsupported_declared_range_makes_every_hook_unknown(self) -> None:
        declaration = read_hook_declaration(
            _manifest('requires_hermes: ">=0.30.0"', "provides_hooks:", "  - pre_tool_call")
        )
        self.assertEqual(declaration.range_status, "unsupported")
        self.assertEqual(declaration.hooks[0].contract.effect, "unknown")


if __name__ == "__main__":
    unittest.main()

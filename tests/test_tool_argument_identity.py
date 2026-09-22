"""Full-or-unknown argument identity through the public OMH hooks."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from omh.plugin_bundle.omh.hooks.result_transforms import transform_tool_result
from omh.plugin_bundle.omh.hooks import nudge_budget
from omh.plugin_bundle.omh.hooks.tool_hooks import post_tool_call, pre_tool_call
from omh.plugin_bundle.omh.tool_bursts import (
    REPEAT_CALL_APPROVAL_THRESHOLD, REPEAT_CALL_BLOCK_THRESHOLD,
    tool_args_digest, tool_bursts_path, repeat_call_rule_key,
)


class CompleteIdentityTests(unittest.TestCase):
    def test_long_targets_and_equal_length_suffixes_have_distinct_identities(self):
        a = {"content": "x" * 9000, "path": "target-a"}
        b = {**a, "path": "target-b"}
        c = {**a, "content": "x" * 8999 + "y"}
        for other in (b, c):
            with self.subTest(other=other["path"]):
                self.assertTrue(tool_args_digest(other))
                self.assertNotEqual(tool_args_digest(a), tool_args_digest(other))
        self.assertEqual(tool_args_digest(a), tool_args_digest(dict(reversed(list(a.items())))))

    def test_nested_order_unicode_and_json_types_are_canonical(self):
        a = {"z": ["😀" * 3000 + "a", {"ä": "é", "n": None}], "a": True}
        b = {"a": True, "z": ["😀" * 3000 + "a", {"n": None, "ä": "é"}]}
        self.assertTrue(tool_args_digest(a))
        self.assertEqual(tool_args_digest(a), tool_args_digest(b))
        for first, second in (("😀" * 3000 + "a", "😀" * 3000 + "b"), (True, 1), (1, "1"), (None, "null"), ("é", "e\u0301") ):
            with self.subTest(types=(type(first).__name__, type(second).__name__)):
                self.assertNotEqual(tool_args_digest(first), tool_args_digest(second))

    def test_tuple_arrays_have_explicit_json_array_semantics(self):
        self.assertTrue(tool_args_digest({"items": (1, "two")}))
        self.assertEqual(tool_args_digest({"items": (1, "two")}), tool_args_digest({"items": [1, "two"]}))

    def test_surrogate_pair_cannot_alias_a_unicode_scalar(self):
        scalar = "\U0001f600"
        pair = "\ud83d\ude00"
        self.assertTrue(tool_args_digest(scalar))
        self.assertEqual(tool_args_digest(pair), "")
        self.assertNotEqual(tool_args_digest(scalar), tool_args_digest(pair))

    def test_unsupported_inputs_never_call_user_stringification(self):
        class NotJson:
            def __str__(self):
                raise AssertionError("must not stringify arbitrary objects")
        class CustomDict(dict):
            def items(self):
                raise AssertionError("must not traverse custom containers")
        for value in (NotJson(), CustomDict(a=1), Path("src"), {1: "a"}, {"n": float("nan")}, {"n": float("inf")}, b"bytes", "\ud800", "\ud801", {"\ud800": "value"}):
            with self.subTest(kind=type(value).__name__):
                self.assertEqual(tool_args_digest(value), "")

    def test_resource_exhaustion_is_unknown_not_a_prefix_hash(self):
        deep = []
        for _ in range(80):
            deep = [deep]
        cyclic = []
        cyclic.append(cyclic)
        for value in ("x" * (2**20 + 1), [None] * 10000, deep, cyclic, 1 << 4097, [[0] * 100] * 2000):
            with self.subTest(kind=type(value).__name__):
                self.assertEqual(tool_args_digest(value), "")

    def test_digest_namespace_does_not_reuse_legacy_always_grants(self):
        args = {"path": "src", "pattern": "needle"}
        legacy = hashlib.blake2b(json.dumps(args, sort_keys=True).encode()[:8192], digest_size=8).hexdigest()
        current = tool_args_digest(args)
        self.assertRegex(current, r"^v2:[0-9a-f]{32}$")
        self.assertNotEqual(repeat_call_rule_key("search_files", current), repeat_call_rule_key("search_files", legacy))


class IdentityHookTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(TemporaryDirectory())
        self.home = Path(tmp) / "omh"
        self.host = Path(tmp) / "hermes"
        self.enterContext(patch.dict(os.environ, {"OMH_HOME": str(self.home), "HERMES_HOME": str(self.host)}))
        nudge_budget.reset_nudge_budget()
        self.addCleanup(nudge_budget.reset_nudge_budget)
        self.seq = 0

    def call(self, args, *, tool="write_file", result="constant-result", session="one"):
        self.seq += 1
        event = dict(tool_name=tool, args=args, session_id=session, tool_call_id=f"call-{self.seq}", turn_id="turn-one", omh_home=str(self.home), hermes_home=str(self.host))
        directive = pre_tool_call(**event)
        blocked = isinstance(directive, dict) and directive.get("action") in ("block", "approve")
        post_tool_call(**event, result=result, status="blocked" if blocked else "ok")
        return directive

    def test_different_long_edits_do_not_form_a_false_repeat(self):
        for index in range(REPEAT_CALL_APPROVAL_THRESHOLD + 2):
            self.assertIsNone(self.call({"content": "x" * 9000, "path": f"target-{index}"}))
        for index in range(REPEAT_CALL_APPROVAL_THRESHOLD + 2):
            self.assertIsNone(self.call({"content": "x" * 9000 + f"{index:04}", "path": "same-target"}, session="tail"))

    def test_identical_long_edits_still_escalate_with_their_own_key(self):
        keys = []
        for session, suffix in (("a", "a"), ("b", "b")):
            args = {"content": "x" * 9000 + suffix, "path": "target"}
            directive = None
            for _ in range(REPEAT_CALL_APPROVAL_THRESHOLD + 1):
                directive = self.call(args, session=session)
            if not isinstance(directive, dict):
                self.fail("expected an approval directive")
            self.assertEqual(directive["action"], "approve")
            keys.append(directive["rule_key"])
        self.assertNotEqual(*keys)

    def test_unknown_identity_breaks_continuity_without_broadening_approval(self):
        args = {"path": "same-target", "content": "short"}
        for _ in range(REPEAT_CALL_BLOCK_THRESHOLD):
            self.assertIsNone(self.call(args))
        directive = self.call(args)
        if not isinstance(directive, dict):
            self.fail("expected a blocking directive")
        self.assertEqual(directive["action"], "block")
        self.assertIsNone(self.call({"content": "x" * (2**20 + 1), "path": "different-target"}))
        self.assertIsNone(self.call(args), "a gap of unknown identity cannot prove a consecutive loop")

    def test_changing_results_remain_progress_for_identical_arguments(self):
        args = {"pattern": "x" * 9000, "path": "src"}
        for index in range(REPEAT_CALL_APPROVAL_THRESHOLD * 2):
            self.assertIsNone(self.call(args, tool="search_files", result=f"progress-{index}"))

    def test_engagement_uses_full_identity_and_skips_unknown_distinct_reads(self):
        for index in range(5):
            transform_tool_result(tool_name="search_files", args={"pattern": "x" * 9000 + str(index), "path": "src"}, result="found", session_id="reads", tool_call_id=f"read-{index}", turn_id="read-turn", omh_home=str(self.home), hermes_home=str(self.host))
        self.assertEqual(nudge_budget.engagement_count("reads", nudge_budget.DELEGATION_NUDGES_FIELD, omh_home=str(self.home)), 1)
        before = sum(len(row) for row in nudge_budget._DISTINCT_DIRECT_READS.values())
        for index in range(5):
            transform_tool_result(tool_name="search_files", args={"pattern": "x" * (2**20 + 1), "path": f"src-{index}"}, result="found", session_id="unknown-reads", tool_call_id=f"unknown-{index}", turn_id="read-turn", omh_home=str(self.home), hermes_home=str(self.host))
        self.assertEqual(sum(len(row) for row in nudge_budget._DISTINCT_DIRECT_READS.values()), before)

    def test_ledger_and_rule_keys_contain_only_fingerprints(self):
        marker = "PRIVATE-IDENTITY-CANARY"
        args = {"content": "x" * 9000 + marker, "path": f"/private/{marker}"}
        directive = None
        for _ in range(REPEAT_CALL_APPROVAL_THRESHOLD + 1):
            directive = self.call(args)
        if not isinstance(directive, dict):
            self.fail("expected an approval directive")
        ledger = tool_bursts_path(str(self.home)).read_text()
        self.assertNotIn(marker, ledger)
        self.assertNotIn(marker, str(directive["rule_key"]))
        self.assertIn(tool_args_digest(args), ledger)


if __name__ == "__main__":
    unittest.main()

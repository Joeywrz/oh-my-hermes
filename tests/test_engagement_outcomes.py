"""Outcome regressions through the registered OMH hook boundaries (no model)."""
from __future__ import annotations

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()
from omh.plugin_bundle.omh import engagement_nudges as nudges
from omh.plugin_bundle.omh.hooks import nudge_budget as budget
from omh.plugin_bundle.omh.hooks.result_transforms import transform_tool_result
from omh.plugin_bundle.omh.hooks.session_hooks import subagent_start
from omh.plugin_bundle.omh.hooks.tool_hooks import post_tool_call


class EngagementOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = str(Path(self.tmp.name) / "a")
        budget.reset_nudge_budget()
        nudges.reset_engagement_declines()
        self.addCleanup(budget.reset_nudge_budget)
        self.addCleanup(nudges.reset_engagement_declines)

    def event(self, name, call, result, *, args=None, status="ok", home=None, session="parent", turn="turn"):
        return dict(tool_name=name, tool_call_id=call, args=args or {}, result=json.dumps(result),
                    status=status, session_id=session, turn_id=turn,
                    omh_home=home or self.home, hermes_home=home or self.home)

    def count(self, field, *, home=None, session="parent"):
        return budget.engagement_count(session, field, omh_home=home or self.home)

    def test_preparation_failures_and_child_lifecycle_are_not_interchangeable(self):
        cases = [
            ("omh_delegate_route", {"action": "status"}, {"status": "status"}, "ok"),
            ("omh_delegate_route", {"action": "clear"}, {"status": "cleared"}, "ok"),
            ("omh_delegate_route", {"action": "set"}, {"status": "error", "error": "no model"}, "error"),
            ("omh_delegate_route", {"action": "set"}, {"status": "routed"}, "ok"),
            ("delegate_task", {"action": "list"}, {"status": "ok", "subagents": []}, "ok"),
            ("delegate_task", {"tasks": [{"goal": "test"}]}, {"error": "spawn failed"}, "error"),
        ]
        for index, (name, args, result, status) in enumerate(cases):
            with self.subTest(name=name, args=args):
                event = self.event(name, str(index), result, args=args, status=status)
                post_tool_call(**event)
                transform_tool_result(**event)
                self.assertEqual(self.count(budget.DELEGATION_LATCH_FIELD), 0)
        self.assertEqual(self.count("route_prepared"), 1)
        subagent_start(parent_session_id="parent", child_session_id="child-a", omh_home=self.home, hermes_home=self.home)
        subagent_start(parent_session_id="parent", child_session_id="child-b", omh_home=self.home, hermes_home=self.home)
        self.assertEqual(self.count(budget.DELEGATION_LATCH_FIELD), 1)
        for index in range(7):
            child = self.event("read_file", f"child-{index}", {"content": "ok"}, args={"path": str(index)}, session="child-a")
            post_tool_call(**child)
            self.assertIsNone(transform_tool_result(**child))
        self.assertEqual(self.count(budget.DIRECT_READS_FIELD, session="child-a"), 0)
        self.assertEqual(self.count(budget.DELEGATION_LATCH_FIELD, home=str(Path(self.tmp.name) / "b")), 0)

    def test_mutation_effects_dedup_parallel_profiles_and_read_budgets(self):
        cases = [
            ("patch", {"success": False, "error": "missing match"}, "error", "unknown"),
            ("patch", {"success": True, "no_change": True}, "ok", "none"),
            ("patch", {"error": "denied"}, "blocked", "none"),
            ("patch", {"success": True, "files_modified": ["a"]}, "ok", "landed"),
            ("write_file", {"bytes_written": 0, "verified": True}, "ok", "landed"),
            ("patch", {"success": False, "error": "second failed", "files_modified": ["a"]}, "error", "partial"),
            ("write_file", {"error": "post-write diagnostic failed"}, "error", "unknown"),
            ("write_file", {"bytes_written": 4, "error": "diagnostic failed"}, "error", "partial"),
            # WriteResult emits its default zero even for a pre-write refusal.
            ("write_file", {"bytes_written": 0, "dirs_created": False, "error": "refused"}, "error", "unknown"),
            ("patch", {"success": False, "files_created": ["a"], "error": "later failure"}, "error", "partial"),
            ("patch", {"success": False, "files_deleted": ["a"], "error": "later failure"}, "error", "partial"),
        ]
        for index, (name, result, status, effect) in enumerate(cases):
            home = str(Path(self.tmp.name) / f"case-{index}")
            event = self.event(name, "same-id", result, status=status, home=home)
            # Both real host orders: registry post->transform, outer executor transform->post.
            for callback in (post_tool_call, transform_tool_result, post_tool_call, transform_tool_result):
                callback(**event)
            self.assertEqual(self.count(budget.MUTATIONS_FIELD, home=home), int(effect == "landed"), (name, effect))
            self.assertEqual(self.count("partial_file_mutations", home=home), int(effect == "partial"))
            self.assertEqual(self.count("unknown_file_mutations", home=home), int(effect == "unknown"))
        event = self.event("patch", "parallel", {"success": True, "files_modified": ["a"]})
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: post_tool_call(**event), range(24)))
        self.assertEqual(self.count(budget.MUTATIONS_FIELD), 1)
        # A reused call ID in a later turn is a different call.
        post_tool_call(**{**event, "turn_id": "next"})
        self.assertEqual(self.count(budget.MUTATIONS_FIELD), 2)
        for index in range(5):
            read = self.event("search_files", f"read-{index}", {"total_count": 0}, args={"pattern": str(index)})
            transform_tool_result(**read)
            post_tool_call(**read)
        self.assertEqual(self.count(budget.DELEGATION_NUDGES_FIELD), 1)
        for index in range(10):
            read = self.event("search_files", f"repeat-{index}", {"total_count": 0}, args={"pattern": "4"})
            post_tool_call(**read)
            self.assertIsNone(transform_tool_result(**read))
        self.assertEqual(self.count(budget.DELEGATION_NUDGES_FIELD), 1)
        read = self.event("search_files", "fresh", {"total_count": 0}, args={"pattern": "new"})
        post_tool_call(**read)
        self.assertIsNotNone(transform_tool_result(**read))
        self.assertEqual(self.count(budget.DELEGATION_NUDGES_FIELD), 2)

    def test_unattributed_faults_and_legacy_latches_do_not_invent_progress(self):
        from unittest.mock import patch
        event = self.event("write_file", "", {"bytes_written": 2})
        post_tool_call(**event)
        self.assertIsNone(transform_tool_result(**event))
        self.assertEqual(self.count(budget.MUTATIONS_FIELD), 0)
        self.assertGreater(nudges.engagement_nudge_declines().get("unknown_call_identity", 0), 0)
        event["tool_call_id"] = "observer-fault"
        with patch.object(nudges, "_mutation_effect", side_effect=RuntimeError("private text")):
            post_tool_call(**event)
        self.assertEqual(nudges.engagement_nudge_declines().get("observer_error:RuntimeError"), 1)
        self.assertNotIn("private text", str(nudges.engagement_nudge_declines()))
        self.assertEqual(self.count(budget.MUTATIONS_FIELD), 0)
        path = budget.engagement_nudge_store_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sessions": {"legacy": {"lane_routed": 1, "delegation_nudges": 1}}}))
        self.assertEqual(self.count(budget.DELEGATION_LATCH_FIELD, session="legacy"), 0)
        self.assertEqual(self.count(budget.DELEGATION_NUDGES_FIELD, session="legacy"), 1)


if __name__ == "__main__":
    unittest.main()

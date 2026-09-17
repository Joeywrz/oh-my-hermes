"""Kanban foundation proofs; supplied schemas/callbacks are not host authority."""
from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
import copy
import importlib
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from typing import TYPE_CHECKING, Protocol, TypedDict, Unpack, runtime_checkable

if TYPE_CHECKING:
    from omh.workflows.agent_board import AgentBoard, AgentBoardRequest, HostIdentity, NativeAction

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()

# Preserve helper exports while sharing the exact fixture with component QA.
from five_issue_cases.kanban import (
    FIELDS as FIELDS, SuppliedSchema as SuppliedSchema, TYPED_FIELDS, WORKSPACE_KINDS,
    request as request, supplied_schemas as supplied_schemas,
)
from omh.coding.fanout_failure_diagnostics import is_object_list, is_string_map


ROOT = Path(__file__).resolve().parents[1]
NATIVE_ACTIONS_EXAMPLE = ROOT / "examples" / "agent-board" / "native-actions.json"
NATIVE_ACTIONS_MESSAGE = "agent-board coordinate durable work across profiles on the qa-board"
# The fixture create's digest on the example board, frozen before `lane_role`
# existed. A create that states no role must still produce it: the role is
# filled and stripped before the native action is built.
PRE_ROLE_CREATE_DIGEST = "sha256:31fce8f1d8327f4f9036f3cb776500b08a08b4ebee2e1ebc7e7303cd542ec6fe"


def native_actions_example() -> dict[str, object]:
    """Canonical producer for the committed wrapper-actions example.

    Every section is the exact output of a public OMH API on fixed inputs. The
    prepared request uses the shared fixture host schemas, so the example is
    fixture-attributed: zero native calls, no native execution evidence.
    """
    from omh.workflows.agent_board import AgentBoard, HostIdentity, board_reference
    from omh.wrapper.contract import build_agent_board_status_interaction, build_chat_interaction_payload

    board = AgentBoard("qa-board", board_reference("example-root", "qa-board"))
    prepared = board.prepare(request(), host=HostIdentity("example-session", "example-task", "example-call"),
                             schemas=supplied_schemas(), hooks=frozenset({"pre_tool_call", "post_tool_call"}),
                             board_is_safe=True)
    return {
        "schema_version": "agent_board_native_actions_example/v1",
        "purpose": ("Wrapper actions and the prepared native action for one durable agent-board request, "
                    "regenerated from public OMH APIs by tests/test_agent_board_kanban.py."),
        "provenance": {"kind": "fixture", "host_schemas": "tests/five_issue_cases/kanban.py::supplied_schemas",
                       "native_calls": 0, "native_execution": False,
                       "claim_boundary": "A prepared native action is not an invocation, and a fixture host is not native Hermes evidence."},
        "chat_interaction": {"message": NATIVE_ACTIONS_MESSAGE, "source": "discord",
                             "payload": build_chat_interaction_payload(NATIVE_ACTIONS_MESSAGE, source="discord")},
        "prepared_request": dict(prepared),
        "status_interaction": build_agent_board_status_interaction(prepared, source="discord"),
    }


class _Decoder(Protocol):
    def loads(self, s: str) -> object: ...


_decoder: _Decoder = json


def parsed_object(raw: str) -> dict[str, object]:
    decoded = _decoder.loads(raw)
    assert is_string_map(decoded), "json_not_object"
    return decoded


def write_native_actions_example() -> None:
    NATIVE_ACTIONS_EXAMPLE.parent.mkdir(parents=True, exist_ok=True)
    _ = NATIVE_ACTIONS_EXAMPLE.write_text(json.dumps(native_actions_example(), indent=2, ensure_ascii=False) + "\n",
                                          encoding="utf-8")


class PrepareOptions(TypedDict, total=False):
    host: HostIdentity | None
    schemas: Mapping[str, object]
    hooks: frozenset[str]
    board_is_safe: bool


class ObserveOptions(TypedDict, total=False):
    host: HostIdentity
    tool_name: str
    arguments: Mapping[str, object]
    result: object


@runtime_checkable
class BoardModule(Protocol):
    AgentBoard: type[AgentBoard]
    HostIdentity: type[HostIdentity]

    def board_reference(self, root_identity: str, board: str) -> str: ...


@runtime_checkable
class QaModule(Protocol):
    def run_case(self, case_id: str) -> Mapping[str, object]: ...


class AgentBoardFoundation(unittest.TestCase):
    def api(self) -> BoardModule:
        name = "omh.workflows.agent_board"
        spec = importlib.util.find_spec(name)
        self.assertIsNotNone(spec, "missing bounded Kanban action/receipt state machine")
        module = importlib.import_module(name)
        self.assertTrue(isinstance(module, BoardModule), "missing AgentBoard state machine")
        assert isinstance(module, BoardModule)
        return module

    def board(self) -> AgentBoard:
        api = self.api()
        return api.AgentBoard("qa-board", api.board_reference("host-root-identity", "qa-board"))

    def host(self, call: str = "prepare-1", session: str = "session-1",
             task: str = "host-task-1") -> HostIdentity:
        return self.api().HostIdentity(session, task, call)

    def prepare(self, board: AgentBoard, payload: Mapping[str, object] | None = None,
                **overrides: Unpack[PrepareOptions]) -> AgentBoardRequest:
        return board.prepare(payload or request(), host=overrides.get("host", self.host()),
                             schemas=overrides.get("schemas", supplied_schemas()),
                             hooks=overrides.get("hooks", frozenset({"pre_tool_call", "post_tool_call"})),
                             board_is_safe=overrides.get("board_is_safe", True))

    def action(self, prepared: AgentBoardRequest) -> NativeAction:
        action = prepared["native_action"]
        self.assertIsNotNone(action)
        assert action is not None
        return action

    def begin(self, board: AgentBoard, prepared: AgentBoardRequest, call: str = "native-1") -> bool:
        action = self.action(prepared)
        return board.begin(prepared["request_id"], host=self.host(call),
                           tool_name=action["tool_name"], arguments=action["arguments"])

    def observe(self, board: AgentBoard, prepared: AgentBoardRequest, native_result: object,
                call: str = "native-1", **overrides: Unpack[ObserveOptions]) -> dict[str, object] | None:
        action = self.action(prepared)
        return board.observe(prepared["request_id"], host=overrides.get("host", self.host(call)),
                             tool_name=overrides.get("tool_name", action["tool_name"]),
                             arguments=overrides.get("arguments", action["arguments"]),
                             result=overrides.get("result", json.dumps(native_result)))

    def test_k1_foundation_typed_actions_all_native_operations(self):
        cases: dict[str, tuple[dict[str, object], dict[str, str]]] = {
            "create": ({"title": "qa-task", "assignee": "qa-profile"}, {}),
            "link": ({"parent_id": "T1", "child_id": "T2"}, {}),
            "comment": ({"body": "ephemeral-body"}, {"task_id": "T1"}),
            "heartbeat": ({}, {"task_id": "T1"}),
            "request_review": ({"summary": "ephemeral-summary"}, {"task_id": "T1"}),
            "request_changes": ({"reason": "ephemeral-reason"}, {"task_id": "T1"}),
            "block": ({"reason": "ephemeral-reason"}, {"task_id": "T1"}),
            "unblock": ({}, {"task_id": "T1"}),
            "complete": ({"result": "ephemeral-result"}, {"task_id": "T1"}),
            "show": ({}, {"task_id": "T1"}),
            "list": ({"limit": 1}, {}),
            "attachments": ({}, {"task_id": "T1"}),
        }
        for operation, (arguments, extra) in cases.items():
            with self.subTest(operation=operation):
                board = self.board()
                prepared = self.prepare(board, request(operation, arguments=arguments, **extra))
                self.assertEqual(prepared["state"], "prepared")
                self.assertEqual(prepared["schema_version"], "agent_board_request/v1")
                self.assertEqual(self.action(prepared)["tool_name"], "kanban_" + operation)
                self.assertEqual(self.action(prepared)["arguments"]["board"], "qa-board")
                self.assertEqual(prepared["observed_receipts"], [])
                self.assertIsNone(board.status("qa-create-1")["native_action"])
                self.assertNotIn("ephemeral", json.dumps(board.snapshot()))

    def test_k1_fixture_schema_mirrors_the_admitted_operations(self):
        # The fixture host's schema is a hand mirror of the admitted argument
        # tables; a field admitted in agent_board.py but absent here would
        # make the K1/K6 create degrade to `schema:kanban_create`, a fixture
        # drift that reads like a host capability gap.
        from omh.workflows import agent_board
        self.assertEqual(set(FIELDS), set(agent_board._OPERATIONS))
        for operation, (required, optional, _) in agent_board._OPERATIONS.items():
            with self.subTest(operation=operation):
                self.assertEqual(FIELDS[operation], (required, optional))
        argument_names = {name for required, optional in FIELDS.values() for name in (required + " " + optional).split()}
        for name in argument_names:
            with self.subTest(argument=name):
                self.assertEqual(TYPED_FIELDS.get(name, "string"), agent_board._TYPES.get(name, "string"))
        self.assertEqual(set(TYPED_FIELDS), set(agent_board._TYPES) & argument_names)
        self.assertEqual(set(WORKSPACE_KINDS), set(agent_board._WORKSPACE_KINDS))
        # `lane_role` is filled and stripped on the OMH side, so neither the
        # admitted table nor its fixture mirror may offer it as a native field.
        self.assertNotIn("lane_role", argument_names)

    def test_k1_lane_fields_on_create_round_trip_prepared_to_observed(self):
        lane: dict[str, object] = {"title": "lane-task", "assignee": "worker-profile", "body": "ephemeral-node-prompt",
                                   "skills": ["ulw-work", "tdd-red-green"], "parents": ["T1"],
                                   "workspace_kind": "worktree", "workspace_path": "/repo/.worktrees/lane-1",
                                   "priority": 10, "max_runtime_seconds": 3600}
        board = self.board()
        prepared = self.prepare(board, request("create", "lane-1", lane))
        self.assertEqual(prepared["state"], "prepared")
        arguments = self.action(prepared)["arguments"]
        self.assertEqual(arguments, dict(lane, board="qa-board", idempotency_key="lane-1"))
        self.assertTrue(self.begin(board, prepared))
        receipt = self.observe(board, prepared, {"ok": True, "task_id": "T2", "status": "todo"})
        assert receipt is not None
        self.assertEqual((receipt["state"], receipt["fact"], receipt["task_id"]), ("observed", "create", "T2"))
        self.assertEqual(board.status("lane-1")["state"], "observed")
        self.assertNotIn("ephemeral", json.dumps(board.snapshot()))

    def test_k5_lane_fields_on_create_are_bounded(self):
        base: dict[str, object] = {"title": "lane-task", "assignee": "worker-profile"}
        refused: list[tuple[dict[str, object], str]] = [
            ({"workspace_kind": "container"}, "invalid_workspace_kind"),
            ({"skills": [f"skill-{i}" for i in range(9)]}, "too_many_skills"),
            ({"skills": ["not a reference"]}, "invalid_reference"),
            ({"max_runtime_seconds": 59}, "invalid_integer"),
            ({"max_runtime_seconds": 86_401}, "invalid_integer"),
            ({"priority": 101}, "invalid_integer"),
            ({"priority": True}, "invalid_integer"),
            ({"workspace_path": "/repo"}, "workspace_path_requires_kind"),
            ({"workspace_kind": "scratch", "workspace_path": "/repo"}, "workspace_path_requires_kind"),
            ({"body": "x" * 16_001}, "body_limit_exceeded"),
            ({"body": ""}, "invalid_text"),
        ]
        for extra, reason in refused:
            with self.subTest(reason=reason, extra=extra), self.assertRaises(ValueError) as caught:
                _ = self.prepare(self.board(), request("create", "lane-1", dict(base, **extra)))
            self.assertEqual(str(caught.exception), reason)
        admitted: list[dict[str, object]] = [
            {"max_runtime_seconds": 60}, {"max_runtime_seconds": 86_400}, {"priority": 0}, {"priority": 100},
            {"skills": [f"skill-{i}" for i in range(8)]}, {"body": "x" * 16_000},
            {"workspace_kind": "dir", "workspace_path": "/repo"}, {"workspace_kind": "scratch"},
        ]
        for extra in admitted:
            with self.subTest(extra=extra):
                prepared = self.prepare(self.board(), request("create", "lane-1", dict(base, **extra)))
                self.assertEqual(prepared["state"], "prepared")
        # A comment body keeps its own intake ceiling; the create limit is lane-specific.
        long_comment = self.prepare(self.board(), request("comment", "note-1", {"body": "x" * 16_001}, task_id="T1"))
        self.assertEqual(long_comment["state"], "prepared")

    def test_k1_lane_role_fills_the_lane_contract_and_is_stripped(self):
        from omh.workflows.agent_board import LANE_ROLES, native_schema_supported

        base: dict[str, object] = {"title": "lane-task", "assignee": "worker-profile"}
        expected: dict[str, tuple[str, str | None]] = {
            "builder": ("ulw-work", "worktree"),
            "verifier": ("omh-verification-gate", "worktree"),
            "reviewer": ("omh-code-review", None),
            "docs": ("omh-docs", "worktree"),
            "qa": ("ulw-qa", "worktree"),
        }
        self.assertEqual(set(expected), set(LANE_ROLES))
        prepared: AgentBoardRequest | None = None
        for role, (skill, workspace) in expected.items():
            with self.subTest(role=role):
                fan_in: dict[str, object] = {"parents": ["T1"]} if LANE_ROLES[role]["requires_parents"] else {}
                prepared = self.prepare(self.board(), request("create", "lane-1", dict(base, lane_role=role, **fan_in)))
                self.assertEqual(prepared["state"], "prepared")
                self.assertEqual(prepared["lane_role"], role)
                arguments = self.action(prepared)["arguments"]
                self.assertNotIn("lane_role", arguments)
                self.assertEqual(arguments["skills"], [skill])
                self.assertEqual(arguments.get("workspace_kind"), workspace)
        # The role is an OMH-side contract, not a native argument: a leaked one
        # fails the host schema gate, so the create would degrade to
        # `unavailable` with `schema:kanban_create` instead of landing a row.
        assert prepared is not None
        self.assertNotIn("lane_role", supplied_schemas()["kanban_create"]["properties"])
        leaked: NativeAction = {"tool_name": "kanban_create",
                                "arguments": dict(self.action(prepared)["arguments"], lane_role="qa")}
        self.assertFalse(native_schema_supported(supplied_schemas()["kanban_create"], leaked))

    def test_k1_lane_role_defaults_yield_to_an_explicit_caller_value(self):
        base: dict[str, object] = {"title": "lane-task", "assignee": "worker-profile", "parents": ["T1"]}
        read_only = self.prepare(self.board(), request("create", "lane-1", dict(base, lane_role="reviewer")))
        self.assertNotIn("workspace_kind", self.action(read_only)["arguments"])
        explicit = self.prepare(self.board(), request("create", "lane-2", dict(
            base, lane_role="reviewer", workspace_kind="worktree", workspace_path="/repo/.worktrees/review")))
        self.assertEqual(self.action(explicit)["arguments"]["workspace_kind"], "worktree")
        scratch = self.prepare(self.board(), request("create", "lane-3", dict(
            base, lane_role="builder", workspace_kind="scratch")))
        self.assertEqual(self.action(scratch)["arguments"]["workspace_kind"], "scratch")
        # Extra skills ride along as long as the role's own skill is among them.
        extra = self.prepare(self.board(), request("create", "lane-4", dict(
            base, lane_role="builder", skills=["tdd-red-green", "ulw-work"])))
        self.assertEqual(self.action(extra)["arguments"]["skills"], ["tdd-red-green", "ulw-work"])
        eight = ["ulw-work"] + [f"skill-{index}" for index in range(7)]
        full = self.prepare(self.board(), request("create", "lane-5", dict(base, lane_role="builder", skills=eight)))
        self.assertEqual(self.action(full)["arguments"]["skills"], eight)
        with self.assertRaises(ValueError) as caught:
            _ = self.prepare(self.board(), request("create", "lane-6", dict(
                base, lane_role="builder", skills=eight + ["skill-8"])))
        self.assertEqual(str(caught.exception), "too_many_skills")

    def test_k5_lane_role_refusals_are_closed(self):
        base: dict[str, object] = {"title": "lane-task", "assignee": "worker-profile"}
        refused: list[tuple[dict[str, object], str]] = [
            ({"lane_role": "architect"}, "invalid_lane_role"),
            ({"lane_role": ""}, "invalid_lane_role"),
            ({"lane_role": ["builder"]}, "invalid_lane_role"),
            ({"lane_role": "verifier"}, "verifier_requires_parents"),
            ({"lane_role": "verifier", "parents": []}, "verifier_requires_parents"),
            ({"lane_role": "reviewer"}, "reviewer_requires_parents"),
            ({"lane_role": "builder", "skills": ["omh-docs"]}, "lane_role_skills_mismatch"),
            ({"lane_role": "docs", "skills": []}, "lane_role_skills_mismatch"),
            ({"lane_role": "qa", "skills": "ulw-qa"}, "expected_array"),
        ]
        for extra, reason in refused:
            with self.subTest(reason=reason, extra=extra), self.assertRaises(ValueError) as caught:
                _ = self.prepare(self.board(), request("create", "lane-1", dict(base, **extra)))
            self.assertEqual(str(caught.exception), reason)
        # Only a create carries a role; anywhere else it is an unknown argument.
        with self.assertRaises(ValueError) as caught:
            _ = self.prepare(self.board(), request("comment", "note-1", {"body": "note", "lane_role": "builder"},
                                                   task_id="T1"))
        self.assertEqual(str(caught.exception), "unsupported_argument")

    def test_k4_lane_role_absent_keeps_the_frozen_argument_digest(self):
        api = self.api()
        example = api.AgentBoard("qa-board", api.board_reference("example-root", "qa-board"))
        prepared = self.prepare(example, host=api.HostIdentity("example-session", "example-task", "example-call"))
        self.assertEqual(prepared["argument_digest"], PRE_ROLE_CREATE_DIGEST)
        self.assertEqual(prepared["lane_role"], "")
        # A stated role produces the same native action, and so the same frozen
        # digest, as spelling the same lane fields out by hand.
        lane: dict[str, object] = {"title": "lane-task", "assignee": "worker-profile"}
        stated = self.prepare(self.board(), request("create", "lane-1", dict(lane, lane_role="builder")))
        spelled = self.prepare(self.board(), request("create", "lane-1", dict(
            lane, skills=["ulw-work"], workspace_kind="worktree")))
        self.assertEqual(stated["argument_digest"], spelled["argument_digest"])
        self.assertEqual(self.action(stated)["arguments"], self.action(spelled)["arguments"])

    def test_k6_lane_role_rides_the_prepared_to_observed_receipt(self):
        board = self.board()
        prepared = self.prepare(board, request("create", "lane-1", {
            "title": "lane-task", "assignee": "worker-profile", "lane_role": "docs", "parents": ["T1"]}))
        self.assertTrue(self.begin(board, prepared))
        receipt = self.observe(board, prepared, {"ok": True, "task_id": "T2", "status": "todo"})
        assert receipt is not None
        self.assertEqual((receipt["lane_role"], receipt["fact"], receipt["task_id"]), ("docs", "create", "T2"))
        status = board.status("lane-1")
        self.assertEqual(status["lane_role"], "docs")
        self.assertEqual(status["observed_receipts"][0]["lane_role"], "docs")
        # An operation that carries no role records the empty one, not a guess.
        beat = self.prepare(board, request("heartbeat", "beat-1", {}, task_id="T2"))
        self.assertTrue(self.begin(board, beat, call="native-2"))
        beat_receipt = self.observe(board, beat, {"ok": True, "task_id": "T2"}, call="native-2")
        assert beat_receipt is not None
        self.assertEqual(beat_receipt["lane_role"], "")

    def test_k8_lane_role_reloads_as_empty_for_records_that_predate_it(self):
        api = self.api()
        board = self.board()
        prepared = self.prepare(board, request("create", "lane-1", {
            "title": "lane-task", "assignee": "worker-profile", "lane_role": "builder"}))
        self.assertTrue(self.begin(board, prepared))
        _ = self.observe(board, prepared, {"ok": True, "task_id": "T2", "status": "todo"})
        current = json.dumps(board.snapshot())
        legacy = parsed_object(current)
        rows = legacy["requests"]
        assert is_object_list(rows)
        for row in rows:
            del row["lane_role"]
            receipts = row["observed_receipts"]
            assert is_object_list(receipts)
            for receipt in receipts:
                del receipt["lane_role"]
        facts = legacy["operation_facts"]
        assert is_object_list(facts)
        for fact in facts:
            del fact["lane_role"]
        # A store written before the field is not corrupt; rejecting it would
        # drop every create deduplication key it still holds.
        reloaded = api.AgentBoard.restore("qa-board", board.board_ref, json.dumps(legacy))
        status = reloaded.status("lane-1")
        self.assertEqual((status["state"], status["lane_role"]), ("observed", ""))
        self.assertEqual(status["observed_receipts"][0]["lane_role"], "")
        kept = api.AgentBoard.restore("qa-board", board.board_ref, current)
        self.assertEqual(kept.status("lane-1")["lane_role"], "builder")

    def test_k2_older_host_schema_without_skills_is_unavailable(self):
        lane: dict[str, object] = {"title": "lane-task", "assignee": "worker-profile", "skills": ["ulw-work"]}
        schemas = supplied_schemas()
        _ = schemas["kanban_create"]["properties"].pop("skills")
        result = self.prepare(self.board(), request("create", "lane-1", lane), schemas=schemas)
        self.assertEqual((result["state"], result["reason"]), ("unavailable", "missing_capability"))
        self.assertEqual(result["missing_capabilities"], ["schema:kanban_create"])
        self.assertIsNone(result["native_action"])
        # The same host still admits a create that does not name the missing field.
        plain = self.prepare(self.board(), request("create", "lane-2", {"title": "t", "assignee": "worker-profile"}),
                             schemas=schemas)
        self.assertEqual(plain["state"], "prepared")

    def test_k2_foundation_capability_schema_hooks_identity_unavailable(self):
        cases: list[tuple[PrepareOptions, str]] = [({"schemas": {}}, "kanban_create"),
                                  ({"hooks": frozenset()}, "pre_tool_call"),
                                  ({"hooks": frozenset({"pre_tool_call"})}, "post_tool_call"),
                                  ({"host": None}, "host_identity"),
                                  ({"board_is_safe": False}, "board_binding")]
        for override, missing in cases:
            with self.subTest(missing=missing):
                result = self.prepare(self.board(), **override)
                self.assertEqual(result["state"], "unavailable")
                self.assertIn(missing, result["missing_capabilities"])
                self.assertIsNone(result["native_action"])
        schemas = supplied_schemas()
        _ = schemas["kanban_create"]["properties"].pop("board")
        result = self.prepare(self.board(), schemas=schemas)
        self.assertIn("schema:kanban_create", result["missing_capabilities"])
        schemas = supplied_schemas()
        schemas["kanban_create"]["required"].append("unsupported_native_required")
        self.assertEqual(self.prepare(self.board(), schemas=schemas)["state"], "unavailable")

    def test_k3_foundation_explicit_route_no_substitution(self):
        board = self.board()
        durable = self.prepare(board)
        self.assertEqual(durable["route"], "kanban")
        task = {"goal": "bounded", "context": "parent"}
        research = request("research", "research-1", {"tasks": [task]},
                           coordination="bounded_research")
        prepared = self.prepare(board, research)
        self.assertEqual(prepared["route"], "delegation")
        self.assertEqual(prepared["native_action"], {"tool_name": "delegate_task", "arguments": research["arguments"]})
        self.assertEqual(self.prepare(self.board(), research, schemas={})["state"], "unavailable")
        self.assertEqual(self.prepare(self.board(), schemas={})["route"], "kanban")
        task["model"] = "forbidden"
        with self.assertRaises(ValueError):
            _ = self.prepare(self.board(), research)

    def test_k4_foundation_digest_freeze_sequential_identity(self):
        board = self.board()
        prepared = self.prepare(board)
        self.assertEqual(self.action(prepared)["arguments"]["idempotency_key"], "qa-create-1")
        self.assertTrue(self.begin(board, prepared))
        receipt = self.observe(board, prepared, {"ok": True, "task_id": "T-native", "status": "ready"})
        assert receipt is not None
        self.assertEqual(receipt["task_id"], "T-native")
        retry = self.prepare(board, request(arguments={"assignee": "qa-profile", "title": "qa-task"},
                                            profile="second-profile"))
        self.assertEqual(retry["argument_digest"], prepared["argument_digest"])
        self.assertEqual(retry["request_ref"], prepared["request_ref"])
        self.assertEqual(retry["state"], "observed")
        self.assertIsNone(retry["native_action"])
        self.assertEqual(retry["observed_receipts"][0]["task_id"], "T-native")
        changed = request(arguments={"title": "changed", "assignee": "qa-profile"})
        refused = self.prepare(board, changed)
        self.assertEqual((refused["state"], refused["reason"]), ("denied", "request_digest_changed"))
        self.assertEqual(board.status("qa-create-1")["state"], "observed")

    def test_k4_foundation_simultaneous_begin_has_one_winner(self):
        board = self.board()
        prepared = self.prepare(board)
        gate = Barrier(3)
        def contender(call: str) -> tuple[bool, str]:
            _ = gate.wait(timeout=5)
            return self.begin(board, prepared, call), call
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(contender, "native-" + str(i)) for i in range(2)]
            _ = gate.wait(timeout=5)
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(sum(won for won, _ in results), 1)
        winner = next(call for won, call in results if won)
        _ = self.observe(board, prepared, {"ok": True, "task_id": "T1", "status": "ready"}, winner)
        self.assertEqual(len(board.status("qa-create-1")["observed_receipts"]), 1)

    def test_k5_foundation_closed_inputs_and_explicit_bindings(self):
        bad = [request(authorized=True), request(session_id="forged"), request(action="observe"),
               request(board="foreign"), request("heartbeat", arguments={}),
               request("complete", arguments={}, task_id="T1"),
               request("link", arguments={"parent_id": "T1", "child_id": "T1"}),
               request("show", arguments={"task_id": "T2"}, task_id="T1"),
               request(arguments={"title": "x", "assignee": "p", "board": "foreign"}),
               request(arguments={"title": "x", "assignee": "p", "idempotency_key": "different"}),
               request("list", arguments={"limit": 201}), request("list", arguments={"limit": True}),
               request("list", arguments={"status": "review"}), request("list", arguments={"cursor": "x"}),
               request("complete", arguments={"summary": "ok", "artifacts": []}, task_id="T1")]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _ = self.prepare(self.board(), payload)
        result = self.prepare(self.board(), request("show", arguments={"expected_revision": 4}, task_id="T1"))
        self.assertEqual(result["state"], "unavailable")
        self.assertIn("native_compare_and_swap", result["missing_capabilities"])

    def test_k5_foundation_expected_observation_rechecked_before_call(self):
        board = self.board()
        first = self.prepare(board)
        second = self.prepare(board, request("heartbeat", "heartbeat-1", {}, task_id="T1",
                                             expected_observation_ref="observation:0"))
        _ = self.begin(board, first)
        _ = self.observe(board, first, {"ok": True, "task_id": "T1", "status": "ready"})
        self.assertFalse(self.begin(board, second, "native-2"))
        stale = self.prepare(board, request("show", "show-1", {}, task_id="T1",
                                            expected_observation_ref="observation:0"))
        self.assertEqual((stale["state"], stale["reason"]), ("denied", "stale_observation"))

    def test_k5_foundation_foreign_replay_and_mutated_callbacks_do_not_bind(self):
        board = self.board()
        prepared = self.prepare(board)
        success = {"ok": True, "task_id": "T1", "status": "ready"}
        self.assertIsNone(self.observe(board, prepared, success))  # no pre
        self.assertFalse(board.begin("qa-create-1", host=self.host("native-1", session="foreign"),
                                     **self.action(prepared)))
        self.assertTrue(self.begin(board, prepared))
        before = board.snapshot()
        self.assertIsNone(self.observe(board, prepared, success, "foreign-call"))
        self.assertIsNone(self.observe(board, prepared, success, host=self.host("native-1", task="foreign")))
        self.assertEqual(board.snapshot(), before)
        receipt = self.observe(board, prepared, success, arguments={"board": "foreign"})
        assert receipt is not None
        self.assertEqual((receipt["state"], receipt["reason"]), ("failed", "arguments_changed"))
        self.assertNotIn("task_id", receipt)
        self.assertIsNone(self.observe(board, prepared, success))
        self.assertEqual(board.status("qa-create-1")["state"], "failed")

    def test_k5_foundation_result_identity_and_shape_validation(self):
        cases: list[tuple[dict[str, object], str, dict[str, object]]] = [({"ok": True, "task_id": "T2"}, "heartbeat", {}),
                 ({"ok": True, "parent_id": "T1", "child_id": "T3"}, "link", {"parent_id": "T1", "child_id": "T2"}),
                 ({"ok": True, "task_id": "T1", "board": "foreign"}, "heartbeat", {}),
                 ({"ok": "true", "task_id": "T1"}, "heartbeat", {}),
                 ({"ok": True, "task_id": "T1", "error": "private"}, "heartbeat", {}),
                 ({"ok": True, "task_id": "T1"}, "request_review", {"summary": "s"})]
        for result, operation, arguments in cases:
            with self.subTest(operation=operation, result=result):
                board = self.board()
                extra = {} if operation == "link" else {"task_id": "T1"}
                prepared = self.prepare(board, request(operation, arguments=arguments, **extra))
                _ = self.begin(board, prepared)
                receipt = self.observe(board, prepared, result)
                assert receipt is not None
                self.assertEqual(receipt["state"], "failed")
                self.assertTrue(receipt["requires_reconciliation"])
                self.assertNotIn("task_id", receipt)
                self.assertNotIn("private", json.dumps(board.snapshot()))

    def test_k6_foundation_separate_landed_operation_facts(self):
        cases: list[tuple[str, dict[str, object], dict[str, object], str]] = [
            ("create", {"title": "t", "assignee": "p"}, {"ok": True, "task_id": "T1", "status": "ready"}, "create"),
            ("link", {"parent_id": "T1", "child_id": "T2"}, {"ok": True, "parent_id": "T1", "child_id": "T2"}, "link"),
            ("comment", {"body": "body"}, {"ok": True, "task_id": "T1", "comment_id": 1}, "comment"),
            ("heartbeat", {}, {"ok": True, "task_id": "T1"}, "heartbeat"),
            ("block", {"reason": "r"}, {"ok": True, "task_id": "T1", "run_id": 1, "status": "blocked", "block_kind": "dependency"}, "block"),
            ("unblock", {}, {"ok": True, "task_id": "T1", "status": "ready"}, "unblock"),
            ("request_review", {"summary": "s"}, {"ok": True, "task_id": "T1", "run_id": 1, "status": "review"}, "review_requested"),
            ("request_changes", {"reason": "r"}, {"ok": True, "task_id": "T1", "run_id": 2, "status": "ready", "implementer": "p"}, "changes_requested"),
            ("complete", {"summary": "s"}, {"ok": True, "task_id": "T1", "run_id": 3}, "complete"),
        ]
        board = self.board()
        for n, (operation, arguments, result, fact) in enumerate(cases, 1):
            extra = {} if operation in ("create", "link") else {"task_id": "T1"}
            prepared = self.prepare(board, request(operation, f"request-{n}", arguments, **extra))
            self.assertTrue(self.begin(board, prepared, f"call-{n}"))
            receipt = self.observe(board, prepared, result, f"call-{n}")
            assert receipt is not None
            self.assertEqual(receipt["schema_version"], "agent_board_receipt/v1")
            self.assertEqual(receipt["state"], "observed")
            self.assertEqual(receipt["fact"], fact)
            self.assertEqual(receipt["observation_ref"], f"observation:{n}")
            self.assertEqual(receipt["argument_digest"], prepared["argument_digest"])
        snapshot = json.dumps(board.snapshot())
        for inferred in ('"dispatch"', '"approved"', '"ci"', '"merge"'):
            self.assertNotIn(inferred, snapshot)
        unavailable = self.prepare(board, request("dispatch", "dispatch-1", {}, task_id="T1"))
        self.assertEqual(unavailable["state"], "unavailable")

    def test_k6_foundation_show_list_and_attachment_metadata_only(self):
        board = self.board()
        show = self.prepare(board, request("show", "show-1", {}, task_id="T1"))
        _ = self.begin(board, show)
        receipt = self.observe(board, show, {"task": {"id": "T1", "status": "running", "current_run_id": 1, "description": "private-body"},
            "parents": [], "children": [], "comments": [{"body": "private-body"}], "events": [], "runs": [], "worker_context": "private-body"})
        assert receipt is not None
        self.assertEqual((receipt["task_id"], receipt["landed_status"], receipt["run_id"]), ("T1", "running", 1))
        self.assertEqual(receipt["fact"], "show")  # running/claim is not dispatch
        listing = self.prepare(board, request("list", "list-1", {"limit": 200}))
        self.assertIn("native_mutation_guard", listing["required_capabilities"])
        _ = self.begin(board, listing, "call-list")
        receipt = self.observe(board, listing, {"tasks": [{"id": f"T{i}", "title": "private-body"} for i in range(201)],
            "count": 201, "limit": 200, "truncated": True, "next_limit": 200, "promoted": 0}, "call-list")
        assert receipt is not None
        self.assertEqual(receipt["task_ids"], [f"T{i}" for i in range(200)])
        self.assertTrue(receipt["truncated"])
        attachments = self.prepare(board, request("attachments", "attach-1", {}, task_id="T1"))
        _ = self.begin(board, attachments, "call-attachments")
        receipt = self.observe(board, attachments, {"ok": True, "task_id": "T1", "attachments": [
            {"id": i, "size": 2, "content_type": "text/plain", "filename": "private-body", "stored_path": "/private-body", "uploaded_by": "private-body"} for i in range(1, 34)]}, "call-attachments")
        assert receipt is not None
        self.assertEqual(receipt["attachment_refs"], [{"board_ref": board.board_ref, "task_id": "T1", "attachment_id": i, "size": 2, "content_type": "text/plain"} for i in range(1, 33)])
        self.assertTrue(receipt["truncated"])
        self.assertNotIn("private-body", json.dumps(board.snapshot()))

    def test_k8_foundation_ambiguous_failed_and_missing_post_never_retry(self):
        for result in [None, {"error": "private-error"}, {"ok": False}, {"ok": True}]:
            with self.subTest(result=result):
                board = self.board()
                prepared = self.prepare(board)
                _ = self.begin(board, prepared)
                if result is not None:
                    _ = self.observe(board, prepared, result)
                retry = self.prepare(board)
                self.assertIsNone(retry["native_action"])
                self.assertNotEqual(retry["state"], "observed")
                self.assertTrue(retry["requires_reconciliation"])
                self.assertFalse(self.begin(board, prepared, "retry-call"))

    def test_k8_foundation_intake_bounds_and_malformed_attachments(self):
        success = {"ok": True, "task_id": "T1", "status": "ready"}
        oversized = [json.dumps(dict(success, extra=text), ensure_ascii=False)
                     for text in ("x" * (256 * 1024), chr(0x3b1) * 150000)]
        nested = '{"ok":true,"task_id":"T1","status":"ready","extra":' + "[" * 40 + "0" + "]" * 40 + "}"
        duplicate = '{"ok":true,"task_id":"T1","task_id":"T2","status":"ready"}'
        for raw in ['{"ok":true,', "[]", duplicate, nested, *oversized]:
            with self.subTest(length=len(raw)):
                board = self.board()
                prepared = self.prepare(board)
                _ = self.begin(board, prepared)
                receipt = self.observe(board, prepared, {}, result=raw)
                assert receipt is not None
                self.assertEqual(receipt["state"], "failed")
                if raw in oversized:
                    self.assertEqual(receipt["reason"], "result_intake_exceeded")
                elif raw == nested:
                    self.assertEqual(receipt["reason"], "json_depth_exceeded")
                elif raw == duplicate:
                    self.assertEqual(receipt["reason"], "duplicate_result_key")
        for attachment in [{"id": True, "size": 1, "content_type": "text/plain"},
                           {"id": 1, "size": -1, "content_type": "text/plain"},
                           {"id": 1, "size": 2, "content_type": "https://private-url"},
                           {"id": 1, "size": 2, "content_type": "text/plain", "task_id": "foreign"}]:
            with self.subTest(attachment=attachment):
                board = self.board()
                prepared = self.prepare(board, request("attachments", arguments={}, task_id="T1"))
                _ = self.begin(board, prepared)
                receipt = self.observe(board, prepared, {"ok": True, "task_id": "T1", "attachments": [attachment]})
                assert receipt is not None
                self.assertEqual(receipt["state"], "failed")

    def test_k8_foundation_argument_copy_digest_and_board_identity(self):
        api = self.api()
        self.assertEqual(api.board_reference("root", "board"), api.board_reference("root", "board"))
        self.assertNotEqual(api.board_reference("root", "board"), api.board_reference("other-root", "board"))
        board = self.board()
        arguments: dict[str, object] = {"title": "qa-task", "assignee": "qa-profile"}
        payload = request(arguments=arguments)
        prepared = self.prepare(board, payload)
        pristine = copy.deepcopy(prepared)
        arguments["title"] = "mutated"
        self.action(prepared)["arguments"]["title"] = "mutated"
        self.assertFalse(self.begin(board, prepared))
        self.assertTrue(self.begin(board, pristine))
        raw = json.dumps(board.snapshot())
        self.assertNotIn("host-root-identity", raw)
        self.assertNotIn("session-1", raw)
        self.assertNotIn("host-task-1", raw)
        self.assertNotIn("qa-task", raw)

    def test_k8_foundation_status_closed_and_projection_fact_cap(self):
        board = self.board()
        with self.assertRaises(ValueError):
            _ = board.prepare({"action": "status", "request_id": "unknown", "authorized": True})
        self.assertEqual(board.prepare({"action": "status", "request_id": "unknown"})["state"], "unavailable")
        for i in range(34):
            prepared = self.prepare(board, request("heartbeat", f"beat-{i}", {}, task_id="T1"))
            _ = self.begin(board, prepared, f"call-{i}")
            _ = self.observe(board, prepared, {"ok": True, "task_id": "T1"}, f"call-{i}")
        snapshot = board.snapshot()
        self.assertEqual(len(snapshot["operation_facts"]), 32)
        self.assertTrue(snapshot["truncated"])
        self.assertEqual(snapshot["observation_ref"], "observation:34")
        for i in range(34, 200):
            result = self.prepare(board, request("heartbeat", f"beat-{i}", {}, task_id="T1"))
            self.assertEqual(result["state"], "prepared")
        overflow = self.prepare(board, request("heartbeat", "beat-overflow", {}, task_id="T1"))
        self.assertEqual((overflow["state"], overflow["reason"]), ("unavailable", "request_capacity"))
        self.assertEqual(len(board.snapshot()["requests"]), 200)
        self.assertEqual(board.status("beat-0")["state"], "observed")

    def test_k6_foundation_native_integer_runs_and_string_edge_ids(self):
        board = self.board()
        prepared = self.prepare(board, request("show", "show-native", {}, task_id="T1"))
        _ = self.begin(board, prepared)
        receipt = self.observe(board, prepared, {
            "task": {"id": "T1", "status": "running", "current_run_id": 7},
            "parents": ["P1"], "children": ["C1"], "comments": [], "events": [],
            "runs": [{"id": 7, "pid": 99}], "worker_context": "private-context"})
        assert receipt is not None
        self.assertEqual(receipt["state"], "observed")
        self.assertEqual((receipt["run_id"], receipt["parent_ids"], receipt["child_ids"]), (7, ["P1"], ["C1"]))
        self.assertNotIn("dispatch", receipt.values())
        review = self.prepare(board, request("request_review", "review-native", {"summary": "s"}, task_id="T1"))
        _ = self.begin(board, review, "review-call")
        receipt = self.observe(board, review, {"ok": True, "task_id": "T1", "run_id": 7, "status": "review"}, "review-call")
        assert receipt is not None
        self.assertEqual((receipt["state"], receipt["run_id"]), ("observed", 7))

    def test_k8_foundation_unsolicited_lifecycle_fields_are_not_evidence(self):
        board = self.board()
        prepared = self.prepare(board, request("heartbeat", arguments={}, task_id="T1"))
        _ = self.begin(board, prepared)
        receipt = self.observe(board, prepared, {"ok": True, "task_id": "T1", "status": "done",
                                               "run_id": 99, "comment_id": 22})
        assert receipt is not None
        self.assertEqual(receipt["state"], "observed")
        self.assertEqual(receipt["fact"], "heartbeat")
        self.assertTrue({"landed_status", "run_id", "comment_id"}.isdisjoint(receipt))

    def test_k8_foundation_failed_task_needs_fresh_show_before_mutation(self):
        board = self.board()
        failed = self.prepare(board, request("heartbeat", "failed-heartbeat", {}, task_id="T1"))
        _ = self.begin(board, failed)
        _ = self.observe(board, failed, {"error": "claim touched but heartbeat failed"})
        waiting = self.prepare(board, request("complete", "complete-after-failure", {"summary": "s"}, task_id="T1"))
        self.assertEqual((waiting["state"], waiting["reason"]), ("denied", "reconciliation_required"))
        show = self.prepare(board, request("show", "reconcile-1", {}, task_id="T1"))
        self.assertTrue(self.begin(board, show, "show-call"))
        _ = self.observe(board, show, {"task": {"id": "T1", "status": "running", "current_run_id": 2},
            "parents": [], "children": [], "comments": [], "events": [], "runs": [], "worker_context": ""}, "show-call")
        ready = self.prepare(board, request("complete", "complete-after-failure", {"summary": "s"}, task_id="T1"))
        self.assertEqual(ready["state"], "prepared")
        self.assertEqual(board.status("failed-heartbeat")["state"], "failed")
        self.assertTrue(board.status("failed-heartbeat")["requires_reconciliation"])

    def test_k7_foundation_shipped_skill_characterization(self):
        from omh.skill_pack import builtin_skill_templates, builtin_skill_reference_templates
        from omh.skills.catalog_types import omh_skill_display_name

        root = Path(__file__).resolve().parents[1]
        templates = [template for template in builtin_skill_templates()
                     if omh_skill_display_name(template.name) == "omh-agent-board"]
        self.assertEqual(len(templates), 1)
        for template in templates:
            shipped = root / "skills" / omh_skill_display_name(template.name) / "SKILL.md"
            self.assertTrue(shipped.read_bytes() == template.content.encode("utf-8"), "shipped_skill_bytes_differ")
        for reference in builtin_skill_reference_templates():
            if omh_skill_display_name(reference.skill_name) == "omh-agent-board":
                shipped = root / "skills" / "omh-agent-board" / reference.relative_path
                self.assertTrue(shipped.read_bytes() == reference.content.encode("utf-8"), "shipped_reference_bytes_differ")

    def test_k7_generated_projections_match_canonical_bytes(self):
        # Shipped-copy equality for every projection the five-issue work
        # touches: rendered skill/reference templates, the workflow reference,
        # and the capability-family sidecar. Bytes only; no phrase is pinned.
        from omh.capabilities.families import standalone_capability_families_json
        from omh.skill_pack import builtin_skill_templates, builtin_skill_reference_templates
        from omh.skills.catalog_types import omh_skill_display_name
        from omh.skills.render import workflow_reference_markdown

        root = Path(__file__).resolve().parents[1]
        skills = {"omh-agent-board", "omh-lifecycle-growth"}
        references = {
            ("omh-routing", "references/workflow-artifacts.md"),
            ("omh-lifecycle-growth", "references/procedure.md"),
            ("omh-lifecycle-growth", "references/full-contract.md"),
        }
        projections: list[tuple[Path, bytes]] = [
            (root / "docs" / "WORKFLOWS.md", workflow_reference_markdown().encode("utf-8")),
            (root / "src" / "plugin_bundle" / "omh" / "tools" / "capability_families.json",
             standalone_capability_families_json().encode("utf-8")),
        ]
        for template in builtin_skill_templates():
            name = omh_skill_display_name(template.name)
            if name in skills:
                projections.append((root / "skills" / name / "SKILL.md", template.content.encode("utf-8")))
        for reference in builtin_skill_reference_templates():
            key = (omh_skill_display_name(reference.skill_name), reference.relative_path)
            if key in references:
                projections.append((root / "skills" / key[0] / key[1], reference.content.encode("utf-8")))
        self.assertEqual(len(projections), 2 + len(skills) + len(references))
        stale = [str(path.relative_to(root)) for path, expected in projections if path.read_bytes() != expected]
        self.assertEqual(stale, [])

    def test_k7_native_actions_example_matches_canonical_producers(self):
        # Parse-equality against the producer, like the demo cards; then the
        # public CLI must project the same wrapper actions for the same message.
        def section(value: object, *keys: str) -> dict[str, object]:
            for key in keys:
                self.assertTrue(is_string_map(value), key)
                assert is_string_map(value)
                value = value[key]
            self.assertTrue(is_string_map(value), keys)
            assert is_string_map(value)
            return value

        self.assertTrue(NATIVE_ACTIONS_EXAMPLE.is_file(), "missing committed agent-board native actions example")
        shipped = parsed_object(NATIVE_ACTIONS_EXAMPLE.read_text(encoding="utf-8"))
        expected = parsed_object(json.dumps(native_actions_example()))
        self.assertEqual(shipped, expected)
        native_action = section(shipped, "prepared_request", "native_action")
        self.assertEqual(native_action["tool_name"], "kanban_create")
        self.assertEqual(section(native_action, "arguments")["idempotency_key"], "qa-create-1")
        self.assertIsNone(section(shipped, "status_interaction", "status")["native_action"])
        self.assertEqual(section(shipped, "provenance")["native_calls"], 0)
        with TemporaryDirectory(prefix="agent-board-example-") as temporary:
            home = Path(temporary)
            status, stdout, stderr = run_cli(["--omh-home", str(home / "omh"), "--hermes-home", str(home / "hermes"),
                                              "chat", "interact", "--source", "discord", "--json", NATIVE_ACTIONS_MESSAGE])
        self.assertEqual(status, 0, stderr)
        cli_actions = section(parsed_object(stdout), "chat_response")["actions"]
        self.assertTrue(is_object_list(cli_actions))
        assert is_object_list(cli_actions)
        self.assertEqual(cli_actions, section(shipped, "chat_interaction", "payload", "chat_response")["actions"])
        self.assertEqual([section(action)["id"] for action in cli_actions],
                         ["prepare_agent_board_card", "refresh_status", "show_status"])
        prepare_payload = section(cli_actions[0], "payload")
        self.assertEqual(prepare_payload["tool_name"], "omh_agent_board")
        self.assertEqual(prepare_payload["execution_policy"], "prepare_only")

    def test_k8_foundation_qa_does_not_claim_integrated_pass(self):
        name = "five_issue_cases.kanban"
        self.assertIsNotNone(importlib.util.find_spec(name), "missing installed component QA producer")
        from unittest.mock import patch
        from five_issue_cases import kanban as qa
        from five_issue_cases import kanban_native
        for case in ("K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8"):
            # Foundation characterization must not depend on whether THIS machine
            # has an installed Hermes host; the native path is the parent's surface.
            with patch.object(kanban_native, "native_host_available", return_value=False):
                result = qa.run_case(case)
            native_required = case in {"K1", "K4", "K6", "K8"}
            # Without a native host only the fixture cases and the local
            # generated-equality case (K7) can pass; native-required cases block.
            self.assertEqual(result["pass"], case in {"K2", "K3", "K5", "K7"})
            self.assertEqual(result["provenance"], {"kind": "local" if case == "K7" else "fixture",
                "scope": "surface",
                "native_required": native_required, "native_available": False})
            self.assertTrue(result["cleanup"]["verified_absent"])
            self.assertEqual(result["cleanup"]["errors"], [])
            self.assertEqual(result["cleanup"]["terminated_processes"], [])
            self.assertEqual(result["observations"]["native_calls"], 0)
            if case != "K7":
                self.assertTrue(result["observations"]["component_pass"])
                self.assertTrue(result["observations"]["decisive_assertions"])
                self.assertTrue(result["cleanup"]["removed_paths"])
            else:
                checks = result["observations"]["checks"]
                assert isinstance(checks, dict)
                self.assertTrue(checks["skill_bytes_equal"])
            if native_required:
                self.assertTrue(result["blocked_reason"])
            else:
                self.assertIsNone(result["blocked_reason"])


# The criterion entrypoint also discovers the installed bridge regressions.
from test_agent_board_plugin import AgentBoardIntegration as AgentBoardIntegration


if __name__ == "__main__":
    _ = unittest.main()

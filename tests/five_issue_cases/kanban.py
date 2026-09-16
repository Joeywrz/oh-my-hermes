"""Installed-bundle component QA with explicit fixture/native boundaries.

Copies the shipped bundle to an invocation-owned location and calls its real
registered handler and pre/post hooks. Capability discovery and native result
emission are fixtures, NOT native execution or normal-model authorization.
Native-required criteria remain blocked for the parent's native scenario.
"""
from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Protocol, TypeGuard, TypedDict, runtime_checkable
from unittest.mock import MagicMock, patch
import uuid

from . import CaseResult, JsonValue, unavailable_case


# Frozen researched schema subset. Shared fixtures, not capability authority.
FIELDS = {
    "create": ("title assignee",
               "body parents skills workspace_kind workspace_path priority max_runtime_seconds "
               "idempotency_key initial_status model provider completion_contract"),
    "link": ("parent_id child_id", ""), "comment": ("task_id body", ""),
    "heartbeat": ("", "task_id note"),
    "request_review": ("summary", "task_id reviewer metadata"),
    "request_changes": ("reason", "task_id"), "block": ("reason", "task_id kind"),
    "unblock": ("task_id", ""), "complete": ("", "task_id summary result metadata created_cards"),
    "show": ("", "task_id"), "list": ("", "assignee status tenant include_archived limit"),
    "attachments": ("", "task_id"),
}


# Non-string property types the researched schemas declare; everything else is a string.
TYPED_FIELDS = {"parents": "array", "skills": "array", "created_cards": "array", "metadata": "object",
                "limit": "integer", "priority": "integer", "max_runtime_seconds": "integer",
                "include_archived": "boolean"}
WORKSPACE_KINDS = ["scratch", "dir", "worktree"]


class SuppliedSchema(TypedDict):
    type: str
    properties: dict[str, dict[str, object]]
    required: list[str]


def supplied_schemas() -> dict[str, SuppliedSchema]:
    schemas: dict[str, SuppliedSchema] = {}
    for operation, (required, optional) in FIELDS.items():
        fields = (required + " " + optional + " board").split()
        properties: dict[str, dict[str, object]] = {name: {"type": TYPED_FIELDS.get(name, "string")} for name in fields}
        if "workspace_kind" in properties:
            properties["workspace_kind"] = {"type": "string", "enum": list(WORKSPACE_KINDS)}
        schemas["kanban_" + operation] = {
            "type": "object", "properties": properties, "required": required.split(),
        }
    schemas["delegate_task"] = {
        "type": "object", "properties": {"tasks": {"type": "array"}}, "required": ["tasks"],
    }
    return schemas


def request(operation: str = "create", request_id: str = "qa-create-1",
            arguments: dict[str, object] | None = None, **extra: object) -> dict[str, object]:
    if arguments is None:
        arguments = {"title": "qa-task", "assignee": "qa-profile"}
    return {"action": "prepare", "request_id": request_id, "coordination": "durable",
            "operation": operation, "board": "qa-board", "profile": "qa-profile",
            "arguments": arguments, **extra}


@runtime_checkable
class _Handler(Protocol):
    def __call__(self, args: Mapping[str, object], **kwargs: object) -> str: ...


@runtime_checkable
class _Hook(Protocol):
    def __call__(self, **kwargs: object) -> object: ...


@runtime_checkable
class _Plugin(Protocol):
    def register(self, ctx: object) -> None: ...


class _Decoder(Protocol):
    def loads(self, s: str) -> object: ...


_decoder: _Decoder = json


def _dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _object(value: object) -> TypeGuard[dict[str, object]]:
    return _dict(value) and all(isinstance(key, str) for key in value)


def _list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _parsed(raw: str) -> dict[str, object]:
    result = _decoder.loads(raw)
    assert _object(result), "plugin_result_not_object"
    return result


class InstalledLoop:
    """Fixture host driving actual installed callbacks; never dispatches tools."""

    def __init__(self, root: Path, stack: ExitStack) -> None:
        self.root: Path = root
        self.home: Path = root / "omh-home"
        self.fixture_result_calls: int = 0
        self.sequence: int = 0
        self.tools: dict[str, _Handler] = {}
        self.hooks: dict[str, _Hook] = {}
        self.name: str = "_kanban_component_" + uuid.uuid4().hex
        bundle = root / "plugins" / "omh"
        source = Path(__file__).resolve().parents[2] / "src" / "plugin_bundle" / "omh"
        _ = shutil.copytree(source, bundle, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        spec = importlib.util.spec_from_file_location(self.name, bundle / "__init__.py", submodule_search_locations=[str(bundle)])
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[self.name] = module
        _ = stack.callback(self._unload)
        spec.loader.exec_module(module)
        assert isinstance(module, _Plugin)
        module.register(self)
        assert "omh_agent_board" in self.tools, "installed_agent_board_not_registered"
        bridge = importlib.import_module(self.name + ".agent_board_bridge")
        _ = stack.enter_context(patch.object(bridge, "default_omh_home", return_value=self.home))
        _ = stack.enter_context(patch.object(bridge, "effective_root", return_value="fixture-native-root"))
        self.capabilities: MagicMock = stack.enter_context(patch.object(bridge, "host_capabilities",
            return_value=(supplied_schemas(), frozenset({"pre_tool_call", "post_tool_call"}))))

    def _unload(self) -> None:
        for name in list(sys.modules):
            if name == self.name or name.startswith(self.name + "."):
                _ = sys.modules.pop(name, None)

    def register_tool(self, name: str, toolset: object, schema: object, handler: object, **kwargs: object) -> None:
        _ = toolset, schema, kwargs
        assert isinstance(handler, _Handler)
        self.tools[name] = handler

    def register_hook(self, name: str, handler: object) -> None:
        assert isinstance(handler, _Hook)
        self.hooks[name] = handler

    def _kwargs(self, name: str, args: Mapping[str, object]) -> dict[str, object]:
        self.sequence += 1
        return {"tool_name": name, "args": dict(args), "session_id": "qa-session", "task_id": "qa-host-task",
                "tool_call_id": f"qa-call-{self.sequence}", "omh_home": str(self.home)}

    def prepare(self, args: Mapping[str, object]) -> dict[str, object]:
        kwargs = self._kwargs("omh_agent_board", args)
        directive = self.hooks["pre_tool_call"](**kwargs)
        assert not _object(directive) or directive.get("action") != "block"
        # Real Hermes handler kwargs omit tool_call_id. Only the pre hook can
        # supply it to this installed handler; JSON IDs are not a substitute.
        raw = self.tools["omh_agent_board"](args, session_id="qa-session", task_id="qa-host-task")
        _ = self.hooks["post_tool_call"](**kwargs, result=raw)
        return _parsed(raw)

    def native(self, prepared: Mapping[str, object], result: Mapping[str, object]) -> bool:
        action = prepared.get("native_action")
        assert _object(action), "missing_typed_native_action"
        name, args = action.get("tool_name"), action.get("arguments")
        assert isinstance(name, str) and _object(args)
        kwargs = self._kwargs(name, args)
        directive = self.hooks["pre_tool_call"](**kwargs)
        if _object(directive) and directive.get("action") == "block":
            return False
        self.fixture_result_calls += 1
        _ = self.hooks["post_tool_call"](**kwargs, result=json.dumps(result))
        return True

    def status(self, request_id: str) -> dict[str, object]:
        return self.prepare({"action": "status", "request_id": request_id})


@contextmanager
def installed_loop() -> Generator[InstalledLoop, None, None]:
    with TemporaryDirectory(prefix="omh-kanban-component-") as temporary:
        root = Path(temporary)
        with patch.dict(os.environ, {"OMH_HOME": str(root / "omh-home"), "HERMES_HOME": str(root / "hermes-home")}), ExitStack() as stack:
            yield InstalledLoop(root, stack)


def _receipt(result: Mapping[str, object]) -> dict[str, object]:
    receipts = result.get("observed_receipts")
    assert _list(receipts) and len(receipts) == 1
    receipt: object = receipts[0]
    assert _object(receipt)
    return receipt


def _run_k7_generated_equality() -> CaseResult:
    """K7: shipped agent-board projections equal their canonical producers byte-for-byte.

    Public surfaces only: the `omh docs … --check` CLI gates plus the canonical
    template/example producers. Local provenance; no native host is involved.
    """
    from omh.skill_pack import builtin_skill_reference_templates, builtin_skill_templates
    from omh.skills.catalog_types import omh_skill_display_name

    root = Path(__file__).resolve().parents[2]
    result = unavailable_case("K7", "scenario_incomplete")
    result["provenance"] = {"kind": "local", "scope": "surface", "native_required": False, "native_available": False}
    result["inputs_metadata"] = {"skill": "omh-agent-board", "checks": ["workflows", "roles", "capability-families"]}
    checks: dict[str, JsonValue] = {}
    exits: list[JsonValue] = []
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", UV_NO_SYNC="1")
    for name in ("workflows", "roles", "capability-families"):
        argv = [sys.executable, "-P", "-m", "omh.cli", "docs", name, "--check"]
        result["commands"].append(list(argv))
        child = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=environment, cwd=root)
        exits.append(child.returncode)
        checks["docs_" + name + "_check"] = child.returncode == 0
    skill_templates = [t for t in builtin_skill_templates() if omh_skill_display_name(t.name) == "omh-agent-board"]
    checks["one_canonical_skill"] = len(skill_templates) == 1
    checks["skill_bytes_equal"] = all(
        (root / "skills" / omh_skill_display_name(t.name) / "SKILL.md").read_bytes() == t.content.encode("utf-8")
        for t in skill_templates)
    references = [r for r in builtin_skill_reference_templates() if omh_skill_display_name(r.skill_name) == "omh-agent-board"]
    checks["reference_bytes_equal"] = all(
        (root / "skills" / "omh-agent-board" / r.relative_path).read_bytes() == r.content.encode("utf-8")
        for r in references)
    checks["reference_count"] = len(references)
    # The committed example's canonical producer lives in the test module; run it
    # in a child interpreter (no import cycle) and compare shipped bytes exactly.
    producer = ("import json, sys; from test_agent_board_kanban import native_actions_example; "
                "sys.stdout.write(json.dumps(native_actions_example(), indent=2, ensure_ascii=False) + '\\n')")
    argv = [sys.executable, "-P", "-c", producer]
    result["commands"].append(list(argv))
    child = subprocess.run(argv, capture_output=True, text=True, timeout=120, cwd=root,
                           env=dict(environment, PYTHONPATH=str(root / "tests")))
    exits.append(child.returncode)
    example = root / "examples" / "agent-board" / "native-actions.json"
    checks["example_bytes_equal"] = child.returncode == 0 and example.exists() and example.read_text(encoding="utf-8") == child.stdout
    result["observations"] = {"checks": checks, "exit_codes": exits, "command_count": len(result["commands"]),
                              "native_calls": 0}
    passed = all(v is True for k, v in checks.items() if k not in {"reference_count", "example_error"})
    result["pass"] = passed
    result["blocked_reason"] = None if passed else "generated_projection_mismatch"
    return result


def run_case(case_id: str) -> CaseResult:
    if case_id not in {f"K{i}" for i in range(1, 9)}:
        return unavailable_case(case_id, "unsupported_case")
    if case_id == "K7":
        return _run_k7_generated_equality()
    from . import kanban_native

    if case_id in {"K1", "K4", "K6", "K8"} and kanban_native.native_host_available():
        return kanban_native.run_native_case(case_id)
    result = unavailable_case(case_id, "native_normal_loop_and_review_dispatch_not_observed")
    native_required = case_id in {"K1", "K4", "K6", "K8"}
    result["provenance"] = {"kind": "fixture", "scope": "surface", "native_required": native_required, "native_available": False}
    result["inputs_metadata"] = {"board": "qa-board", "request_id": "qa-create-1", "fixture_authority": True}
    result["observations"] = {"native_calls": 0, "fixture_result_calls": 0, "decisive_assertions": 0}
    # A real isolated interpreter proves the installed entrypoint can load and
    # prepare without borrowing this process's module/cache state. This is
    # still a fixture host, not native Hermes execution or authorization.
    code = (
        "import json\nfrom _local_package import load_local_package\nload_local_package()\n"
        "from five_issue_cases.kanban import installed_loop, request\n"
        "with installed_loop() as loop:\n"
        "    response = loop.prepare(request())\n"
        "    assert response['state'] == 'prepared' and not response['observed_receipts']\n"
        "    root = str(loop.root)\n"
        "print(json.dumps({'state': response['state'], 'root': root}))\n"
    )
    command = [sys.executable, "-c", code]
    result["commands"] = [command]
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONDONTWRITEBYTECODE="1", UV_NO_SYNC="1")
    child = subprocess.run(command, capture_output=True, text=True, timeout=20, env=environment)
    assert child.returncode == 0, "installed_interpreter_failed"
    child_result = _parsed(child.stdout)
    child_root = child_result.get("root")
    assert isinstance(child_root, str) and not Path(child_root).exists(), "installed_interpreter_cleanup_failed"
    assert child_result["state"] == "prepared"
    result["cleanup"]["owned_resources"].append(child_root)
    result["cleanup"]["removed_paths"].append(child_root)
    result["observations"]["installed_interpreter_exit"] = child.returncode
    owned: Path | None = None
    try:
        with installed_loop() as loop:
            owned = loop.root
            result["cleanup"]["owned_resources"].append(str(owned))
            assertions = 0
            if case_id == "K2":
                loop.capabilities.return_value = ({}, frozenset())
                prepared = loop.prepare(request())
                assert prepared["state"] == "unavailable" and prepared["native_action"] is None
                missing = prepared["missing_capabilities"]
                assert _list(missing) and "kanban_create" in missing
                assert loop.fixture_result_calls == 0
                assertions = 3
            elif case_id == "K3":
                durable = loop.prepare(request())
                research = loop.prepare(request("research", "research", {"tasks": [{"goal": "bounded", "context": "parent"}]},
                                                coordination="bounded_research"))
                assert durable["route"] == "kanban" and research["route"] == "delegation"
                assert loop.fixture_result_calls == 0
                assertions = 2
            else:
                prepared = loop.prepare(request())
                assert loop.native(prepared, {"ok": True, "task_id": "T1", "status": "ready"})
                assert _receipt(loop.status("qa-create-1"))["task_id"] == "T1"
                assertions = 2
                if case_id == "K4":
                    repeated = loop.prepare(request())
                    assert repeated["native_action"] is None
                    assert _receipt(repeated)["task_id"] == "T1"
                    assert loop.fixture_result_calls == 1
                    assertions += 3
                elif case_id == "K5":
                    stale = loop.prepare(request("heartbeat", "stale", {}, task_id="T1", expected_observation_ref="observation:0"))
                    assert stale["state"] == "denied" and stale["native_action"] is None
                    heartbeat = loop.prepare(request("heartbeat", "beat", {}, task_id="T1"))
                    assert loop.native(heartbeat, {"ok": True, "task_id": "FOREIGN"})
                    assert _receipt(loop.status("beat"))["state"] == "failed"
                    assertions += 3
                elif case_id in {"K1", "K6"}:
                    cases: list[tuple[str, dict[str, object], dict[str, object]]] = [
                        ("create", {"title": "lane-task", "assignee": "qa-profile", "body": "PRIVATE-SENTINEL",
                                    "skills": ["ulw-work"], "parents": ["T1"], "workspace_kind": "worktree",
                                    "workspace_path": "/qa/worktree", "priority": 5, "max_runtime_seconds": 600},
                         {"ok": True, "task_id": "T2", "status": "todo"}),
                        ("link", {"parent_id": "T1", "child_id": "T2"}, {"ok": True, "parent_id": "T1", "child_id": "T2"}),
                        ("comment", {"body": "PRIVATE-SENTINEL"}, {"ok": True, "task_id": "T1", "comment_id": 1}),
                        ("heartbeat", {}, {"ok": True, "task_id": "T1"}),
                        ("block", {"reason": "PRIVATE-SENTINEL"}, {"ok": True, "task_id": "T1", "run_id": 1, "status": "blocked", "block_kind": "dependency"}),
                        ("unblock", {}, {"ok": True, "task_id": "T1", "status": "ready"}),
                        ("request_review", {"summary": "PRIVATE-SENTINEL"}, {"ok": True, "task_id": "T1", "run_id": 1, "status": "review"}),
                        ("request_changes", {"reason": "PRIVATE-SENTINEL"}, {"ok": True, "task_id": "T1", "run_id": 2, "status": "ready", "implementer": "qa-profile"}),
                        ("complete", {"summary": "PRIVATE-SENTINEL"}, {"ok": True, "task_id": "T1", "run_id": 3}),
                        ("show", {}, {"task": {"id": "T1", "status": "done"}, "parents": [], "children": [], "comments": [], "events": [], "runs": [], "worker_context": "PRIVATE-SENTINEL"}),
                        ("attachments", {}, {"ok": True, "task_id": "T1", "attachments": [{"id": 1, "size": 2, "content_type": "text/plain", "stored_path": "PRIVATE-SENTINEL"}]}),
                        ("list", {"limit": 1}, {"tasks": [{"id": "T1"}], "count": 1, "limit": 1, "truncated": False, "next_limit": None, "promoted": 0}),
                    ]
                    for operation, args, reply in cases:
                        extra = {} if operation in {"create", "link", "list"} else {"task_id": "T1"}
                        action = loop.prepare(request(operation, operation, args, **extra))
                        assert loop.native(action, reply)
                        observed = _receipt(loop.status(operation))
                        assert observed["operation"] == operation and observed["state"] == "observed"
                        assertions += 2
                    dispatch = loop.prepare(request("dispatch", "dispatch", {}, task_id="T1"))
                    assert dispatch["state"] == "unavailable"
                    assertions += 1
                elif case_id == "K8":
                    heartbeat = loop.prepare(request("heartbeat", "failed", {}, task_id="T1"))
                    assert loop.native(heartbeat, {"error": "PRIVATE-SENTINEL"})
                    assert _receipt(loop.status("failed"))["state"] == "failed"
                    retry = loop.prepare(request("complete", "retry", {"summary": "PRIVATE-SENTINEL"}, task_id="T1"))
                    assert retry["state"] == "denied" and retry["native_action"] is None
                    assertions += 3
            for path in loop.home.rglob("*.json"):
                assert b"PRIVATE-SENTINEL" not in path.read_bytes(), "private_body_persisted"
                assertions += 1
            result["observations"].update(fixture_result_calls=loop.fixture_result_calls, decisive_assertions=assertions,
                                           component_pass=True, installed_callback_surface=True)
            result["pass"] = not native_required
            if not native_required:
                result["blocked_reason"] = None
    finally:
        if owned is not None:
            absent = not owned.exists()
            result["cleanup"]["verified_absent"] = absent
            if absent:
                result["cleanup"]["removed_paths"].append(str(owned))
            result["cleanup"]["errors"] = [] if absent else ["owned_component_root_remains"]
    return result

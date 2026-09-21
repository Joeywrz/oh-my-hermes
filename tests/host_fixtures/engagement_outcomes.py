"""Local cross-checkout regression for OMH engagement outcome observation.

Run ONLY through scripts/run_tests.sh. The sibling omh-engagement-outcomes
checkout supplies the plugin; this is not a dependency vendored into Hermes.
Real registry, plugin registration/dispatch, sequential/inline executor and
native child construction run against temporary homes. Model clients cannot
connect; child conversation is the sole simulated delegation boundary.

These tests distinguish host contract characterization from OMH regression:
post/transform order and first-string selection are existing host semantics,
not promises of annotation delivery or evidence of live provider operation.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def runtime(tmp_path, monkeypatch, request):
    home = tmp_path / "hermes"
    home.mkdir()
    omh_home = tmp_path / "omh"
    omh_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OMH_HOME", str(omh_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (home / "config.yaml").write_text(
        "model:\n  default: test-model\n  provider: openai\n"
        "delegation:\n  max_spawn_depth: 3\n  max_iterations: 2\n"
        "tools:\n  tool_search:\n    defer: [write_file, patch]\n"
        "terminal:\n  backend: local\n",
        encoding="utf-8",
    )

    forbidden_calls = []

    def no_network(*args, **kwargs):
        import traceback

        forbidden_calls.append("".join(traceback.format_stack(limit=35)))
        raise AssertionError(
            "Network/provider access forbidden in this integration test"
        )

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    import model_tools
    import run_agent
    from hermes_cli import plugins
    from hermes_cli.plugins_manifest import PluginManifest

    client_factory = MagicMock()
    client_factory.return_value.chat.completions.create.side_effect = no_network
    client_factory.return_value.responses.create.side_effect = no_network
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", client_factory)
    monkeypatch.setattr("agent.model_metadata.fetch_model_metadata", lambda *a, **k: {})
    # Child construction may probe an OpenAI-compatible localhost endpoint for
    # context size even without a conversation. Stub only that metadata I/O.
    monkeypatch.setattr(
        "agent.model_metadata._query_local_context_length", lambda *a, **k: 128000
    )
    monkeypatch.setattr(
        "agent.model_metadata._query_ollama_api_show", lambda *a, **k: None
    )
    manager = plugins.PluginManager()
    manager._discovered = (
        True  # Explicit test-owned registrations; never scan installed plugins.
    )
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    ctx = plugins.PluginContext(
        PluginManifest(name="omh-integration", version="1.0.0"), manager
    )
    leases = []

    def hook(name, callback):
        lease = ctx.register_hook(name, callback)
        leases.append(lease)
        return lease

    # The canonical host runner deliberately drops caller environment. Use
    # its forwarded pytest -o option, not an env override that silently vanishes.
    source = next((value.partition("=")[2] for value in
                   request.config.getoption("override_ini") or []
                   if value.startswith("omh_checkout=")), None)
    checkout = Path(source) if source else Path(__file__).resolve().parents[3] / "omh-engagement-outcomes"
    bundle = checkout / "src/plugin_bundle/omh"
    assert (bundle / "__init__.py").is_file(), (
        "Pass -o omh_checkout=/absolute/path/to/isolated/omh to the host test runner"
    )
    package = "_omh_engagement_host_test"
    spec = importlib.util.spec_from_file_location(
        package, bundle / "__init__.py", submodule_search_locations=[str(bundle)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, package, module)
    spec.loader.exec_module(module)
    load = lambda name: importlib.import_module(f"{package}.{name}")
    paths = load("runtime_paths")
    paths.note_host_registration(ctx)
    budget = load("hooks.nudge_budget")
    budget.reset_nudge_budget()
    engagement = load("engagement_nudges")
    engagement.reset_engagement_declines()
    events = []
    for name in (
        "pre_tool_call",
        "post_tool_call",
        "transform_tool_result",
        "subagent_start",
    ):
        hook(name, lambda _name=name, **kw: events.append((_name, dict(kw))))
    hook("post_tool_call", load("hooks.tool_hooks").post_tool_call)
    omh_transform = load("hooks.result_transforms").transform_tool_result
    transform_lease = hook("transform_tool_result", omh_transform)
    hook("subagent_start", load("hooks.session_hooks").subagent_start)

    agent = run_agent.AIAgent(
        model="test-model",
        provider="openai",
        api_key="test-not-a-credential",
        base_url="http://127.0.0.1:1/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_id="parent-session",
        enabled_toolsets=["file", "delegation"],
        save_trajectories=False,
    )
    agent._current_turn_id = "turn-one"
    agent._current_api_request_id = "request-one"
    agent._delegate_depth = (
        1  # Native nested inline path; no asynchronous gateway delivery.
    )

    def count(field, session="parent-session"):
        return budget.engagement_count(session, field, omh_home=str(omh_home))

    def call(name, args, call_id="call-one", path="direct"):
        events.clear()
        if path.startswith("bridge-"):
            args = {"calls": [{"name": name, "arguments": args}]}
            name = "tool_call"
            path = path.removeprefix("bridge-")
        if path == "direct":
            return model_tools.handle_function_call(
                name,
                args,
                task_id="task-one",
                session_id=agent.session_id,
                tool_call_id=call_id,
                turn_id=agent._current_turn_id,
                api_request_id=agent._current_api_request_id,
                enabled_toolsets=["file", "delegation"],
            )
        tc = SimpleNamespace(
            id=call_id,
            type="function",
            function=SimpleNamespace(
                name=name, arguments=args if isinstance(args, str) else json.dumps(args)
            ),
        )
        messages = []
        agent._execute_tool_calls_sequential(
            SimpleNamespace(tool_calls=[tc]), messages, "task-one", finalize=False
        )
        assert len(messages) == 1
        assert messages[0]["tool_call_id"] == call_id
        return messages[0]["content"]

    yield SimpleNamespace(
        agent=agent,
        call=call,
        events=events,
        hook=hook,
        count=count,
        budget=budget,
        engagement=engagement,
        model_tools=model_tools,
        tmp=tmp_path,
        home=home,
        omh_home=omh_home,
        manager=manager,
        ctx=ctx,
        omh_transform=omh_transform,
        transform_lease=transform_lease,
    )
    agent.close()
    for lease in reversed(leases):
        lease.dispose()
    budget.reset_nudge_budget()
    for name in list(sys.modules):
        if name.startswith(package + "."):
            del sys.modules[name]
    assert not forbidden_calls, "\n".join(forbidden_calls)


def _terminal_event(runtime, status, tool="write_file", call_id="call-one"):
    posts = [payload for name, payload in runtime.events if name == "post_tool_call"]
    assert len(posts) == 1, runtime.events
    event = posts[0]
    assert event["tool_name"] == tool
    assert event["status"] == status, event
    assert {
        key: event[key]
        for key in (
            "task_id",
            "session_id",
            "tool_call_id",
            "turn_id",
            "api_request_id",
        )
    } == {
        "task_id": "task-one",
        "session_id": "parent-session",
        "tool_call_id": call_id,
        "turn_id": "turn-one",
        "api_request_id": "request-one",
    }
    return event


@pytest.mark.parametrize(
    "path", ["direct", "sequential", "bridge-direct", "bridge-sequential"]
)
@pytest.mark.parametrize(
    "outcome", ["write", "patch", "ineffective_patch", "blocked", "registry_exception", "dispatch_exception", "post_write_exception"]
)
def test_file_effects_have_one_outcome_not_one_count_per_hook(
    runtime, monkeypatch, path, outcome
):
    r = runtime
    target = r.tmp / "example.txt"
    tool = "write_file"
    args = {"path": str(target), "content": "before\n"}
    if outcome in {"patch", "ineffective_patch"}:
        target.write_text("before\n")
        tool = "patch"
        args = {
            "mode": "replace",
            "path": str(target),
            "old_string": "before" if outcome == "patch" else "missing",
            "new_string": "after",
        }

    if outcome == "blocked":
        r.hook(
            "pre_tool_call", lambda **kw: {"action": "block", "message": "test refusal"}
        )
    if outcome == "registry_exception":
        entry = r.model_tools.registry.get_entry("write_file")

        def fail(*a, **k):
            raise RuntimeError("synthetic handler exception before effect")

        monkeypatch.setattr(entry, "handler", fail)
    if outcome in {"dispatch_exception", "post_write_exception"}:
        def fail_boundary(*a, **kw):
            raise RuntimeError("synthetic boundary failure")
        if outcome == "dispatch_exception":
            monkeypatch.setattr(r.model_tools, "_execute_tool", fail_boundary)
        else:
            # Actual write succeeds; bookkeeping then loses the effect fields.
            from tools import file_tools
            monkeypatch.setattr(file_tools, "_note_edited", fail_boundary)
    result = r.call(tool, args, path=path)
    status = (
        "ok"
        if outcome in {"write", "patch"}
        else "blocked"
        if outcome == "blocked"
        else "error"
    )
    event = _terminal_event(r, status, tool)
    phases = [
        name
        for name, _ in r.events
        if name in {"post_tool_call", "transform_tool_result"}
    ]
    if outcome in {"blocked", "dispatch_exception"}:
        assert phases == ["post_tool_call"]
    else:
        assert phases == (
            ["post_tool_call", "transform_tool_result"]
            if path.endswith("direct")
            else ["transform_tool_result", "post_tool_call"]
        )
    if outcome in {"write", "post_write_exception"}:
        assert target.read_text() == "before\n"
        if outcome == "write":
            assert json.loads(event["result"])["verified"] is True
    elif outcome == "patch":
        assert target.read_text() == "after\n"
    elif outcome == "ineffective_patch":
        assert target.read_text() == "before\n"
    else:
        assert not target.exists()
    assert r.count(r.budget.MUTATIONS_FIELD) == (
        1 if outcome in {"write", "patch"} else 0
    ), (result, r.engagement.engagement_nudge_declines())
    if outcome in {"registry_exception", "dispatch_exception", "post_write_exception"}:
        # A generic handler error lacks effect proof even though this test's
        # injected handler is known to throw before writing.
        assert r.count("unknown_file_mutations") == 1
    if outcome == "blocked":
        assert r.count("unknown_file_mutations") == 0


@pytest.mark.parametrize(
    "scenario",
    [
        "coercion",
        "bad_coercion",
        "schema_error",
        "invalid_json",
        "cancelled",
        "middleware_exception",
    ],
)
def test_dispatch_identity_and_early_return_boundaries(runtime, monkeypatch, scenario):
    r = runtime
    target = r.tmp / "source.txt"
    target.write_text("one\ntwo\n")
    name, args, path, expected_tool = (
        "read_file",
        {"path": str(target), "offset": "1", "limit": "1"},
        "direct",
        "read_file",
    )
    status = "ok"
    if scenario == "bad_coercion":
        args["offset"] = "not-an-integer"
        # Best-effort coercion leaves this string unchanged; the real read
        # handler normalizes invalid pagination to its default, not an error.
        status = "ok"
    elif scenario == "schema_error":
        name, args, expected_tool, status = (
            "tool_call",
            {"calls": [{"name": "write_file", "arguments": {"path": str(target)}}]},
            "tool_call",
            "error",
        )
    elif scenario in {"invalid_json", "cancelled"}:
        path = "sequential"
        if scenario == "invalid_json":
            args, status = "{broken-json", "error"
        else:
            r.agent._interrupt_requested = True
            status = "cancelled"
    elif scenario == "middleware_exception":

        def fail(**kwargs):
            raise RuntimeError("middleware exception")

        lease = r.ctx.register_middleware("tool_execution", fail)
        status = "ok"  # Plugin execution middleware exceptions fail open.
    result = r.call(name, args, path=path)
    event = _terminal_event(r, status, expected_tool)
    if scenario == "coercion":
        assert event["args"]["offset"] == 1
        assert event["args"]["limit"] == 1
        assert r.count(r.budget.DIRECT_READS_FIELD) == 1
    if scenario == "schema_error":
        assert "content" in result
    if scenario == "bad_coercion":
        assert event["args"]["offset"] == "not-an-integer"
        assert json.loads(result)["content"].startswith("1|one")
    if scenario == "middleware_exception":
        lease.dispose()
    if scenario in {"invalid_json", "cancelled", "schema_error"}:
        assert not any(name == "transform_tool_result" for name, _ in r.events)
        assert r.count(r.budget.MUTATIONS_FIELD) == 0
    assert target.read_text() == "one\ntwo\n"


@pytest.mark.parametrize(
    "outcome", ["success", "rejected", "cancelled", "construction_exception"]
)
def test_native_delegate_construction_latches_parent_and_marks_distinct_children(
    runtime, monkeypatch, outcome
):
    r = runtime
    from run_agent import AIAgent

    original_init = AIAgent.__init__
    constructed = []

    def child_init(self, *args, **kwargs):
        if outcome == "construction_exception":
            raise RuntimeError("child construction refused before model access")
        original_init(self, *args, **kwargs)
        constructed.append(self)

    monkeypatch.setattr(AIAgent, "__init__", child_init)
    monkeypatch.setattr(
        AIAgent,
        "run_conversation",
        lambda self, *a, **kw: {
            "final_response": "fake child model boundary",
            "messages": [],
        },
    )
    args = {
        "tasks": [{"goal": "first isolated task"}, {"goal": "second isolated task"}]
    }
    if outcome == "construction_exception":
        # Host gap: inline construction exceptions propagate without either
        # a terminal post_tool_call or a result transform. OMH cannot observe
        # an outcome on a hook the host never emits; no fake event is injected.
        with pytest.raises(RuntimeError, match="child construction refused"):
            r.call("delegate_task", args, path="sequential")
        assert not any(
            name in {"post_tool_call", "transform_tool_result", "subagent_start"}
            for name, _ in r.events
        )
        assert r.count(r.budget.DELEGATION_LATCH_FIELD) == 0
        return
    if outcome == "rejected":
        args = {"action": "unsupported-action"}
    if outcome == "cancelled":
        r.agent._interrupt_requested = True
    success = outcome == "success"
    result = r.call("delegate_task", args, path="sequential")
    starts = [kw for name, kw in r.events if name == "subagent_start"]
    _terminal_event(
        r,
        "ok" if success else "cancelled" if outcome == "cancelled" else "error",
        "delegate_task",
    )
    assert not any(name == "transform_tool_result" for name, _ in r.events)
    assert bool(r.count(r.budget.DELEGATION_LATCH_FIELD)) is success, result
    if success:
        assert len(starts) == len(constructed) == 2
        assert len({kw["child_session_id"] for kw in starts}) == 2
        assert len({kw["child_subagent_id"] for kw in starts}) == 2
        for event, child in zip(starts, constructed):
            assert event["parent_session_id"] == r.agent.session_id
            assert event["parent_turn_id"] == "turn-one"
            assert "tool_call_id" not in event  # Host contract gap: no parent call ID.
            assert child.model == r.agent.model
            assert r.budget.session_is_delegated(event["child_session_id"])
        parent_session = r.agent.session_id
        r.agent.session_id = starts[0]["child_session_id"]
        try:
            for index in range(3):
                result = r.call(
                    "write_file",
                    {
                        "path": str(r.tmp / f"child-{index}.txt"),
                        "content": "child effect\n",
                    },
                    call_id=f"child-write-{index}",
                )
                assert "omh_engagement" not in json.loads(result)
            assert r.count(r.budget.MUTATIONS_FIELD, r.agent.session_id) == 0
        finally:
            r.agent.session_id = parent_session
        assert r.count(r.budget.DELEGATION_LATCH_FIELD) == 1
    else:
        assert not starts


@pytest.mark.parametrize("first", ["foreign", "omh"])
def test_transformers_share_raw_input_and_only_first_string_reaches_caller(
    runtime, first
):
    r = runtime
    seen = []

    def transform(label):
        def callback(**kwargs):
            seen.append((label, kwargs["result"]))
            return kwargs["result"] + " [" + label + "]"

        return callback

    # Two real plugin hook registrations, not a mocked invoke_hook. The OMH
    # observer above remains active; neither test callback claims delivery.
    r.hook("transform_tool_result", transform(first))
    r.hook(
        "transform_tool_result", transform("omh" if first == "foreign" else "foreign")
    )
    result = r.call("read_file", {"path": str(r.tmp / "absent.txt")})
    assert len(seen) == 2
    assert seen[0][1] == seen[1][1]
    assert result == seen[0][1] + " [" + first + "]"
    _terminal_event(r, "error", "read_file")


@pytest.mark.parametrize("first", ["foreign", "omh"])
@pytest.mark.parametrize("path", ["direct", "sequential"])
def test_real_omh_candidate_budget_is_not_delivery_evidence(runtime, first, path):
    r = runtime
    for index in range(4):
        target = r.tmp / f"read-{index}.txt"
        target.write_text(f"content-{index}\n")
        r.call("read_file", {"path": str(target)}, call_id=f"read-{index}", path=path)
    # A repeated successful read gets its own call identity but not another
    # distinct-read credit or candidate budget. Host dedup result is real.
    r.call(
        "read_file", {"path": str(r.tmp / "read-3.txt")}, call_id="repeat", path=path
    )
    assert r.count(r.budget.DIRECT_READS_FIELD) == 5  # Attempts, not distinct paths.
    assert r.count(r.budget.DELEGATION_NUDGES_FIELD) == 0
    r.transform_lease.dispose()
    seen = []

    def foreign(**kwargs):
        seen.append(kwargs["result"])
        value = json.loads(kwargs["result"])
        value["foreign"] = True
        return json.dumps(value)

    callbacks = (
        [foreign, r.omh_transform] if first == "foreign" else [r.omh_transform, foreign]
    )
    for callback in callbacks:
        r.hook("transform_tool_result", callback)
    target = r.tmp / "read-fifth.txt"
    target.write_text("fifth\n")
    result = json.loads(
        r.call("read_file", {"path": str(target)}, call_id="fifth", path=path)
    )
    assert r.count(r.budget.DIRECT_READS_FIELD) == 6
    assert r.count(r.budget.DELEGATION_NUDGES_FIELD) == 1
    assert len(seen) == 1
    assert "omh_engagement" not in json.loads(seen[0])
    assert ("omh_engagement" in result) is (first == "omh")
    assert ("foreign" in result) is (first == "foreign")
    _terminal_event(r, "ok", "read_file", "fifth")


@pytest.mark.parametrize("path", ["direct", "sequential"])
def test_partial_patch_reports_landed_file_without_counting_full_success(
    runtime, monkeypatch, path
):
    from tools.file_operations import ShellFileOperations

    r = runtime
    landed, refused = r.tmp / "landed.txt", r.tmp / "refused.txt"
    landed.write_text("before\n")
    refused.write_text("before\n")
    original = ShellFileOperations.write_file

    def fail_second_write(self, filename, content, **kwargs):
        if Path(filename) == refused:
            raise OSError("injected apply-phase I/O failure")
        return original(self, filename, content, **kwargs)

    monkeypatch.setattr(ShellFileOperations, "write_file", fail_second_write)
    patch = (
        "*** Begin Patch\n"
        + "".join(
            f"*** Update File: {target}\n@@\n-before\n+after\n"
            for target in (landed, refused)
        )
        + "*** End Patch"
    )
    result = r.call("patch", {"mode": "patch", "patch": patch}, path=path)
    assert landed.read_text() == "after\n"
    assert refused.read_text() == "before\n"
    event = _terminal_event(r, "error", "patch")
    payload = json.loads(event["result"])
    assert payload["success"] is False
    assert str(landed) in payload["files_modified"]
    assert r.count(r.budget.MUTATIONS_FIELD) == 0, result
    assert r.count("partial_file_mutations") == 1, result


def test_native_profile_binding_is_a_b_a_and_turn_end_is_not_goal_end(runtime):
    r = runtime
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import (
        set_secret_scope, reset_secret_scope, set_multiplex_active, is_multiplex_active,
    )
    from hermes_cli.lifecycle import invoke_hook
    sessions = importlib.import_module(r.engagement.__package__ + ".hooks.session_hooks")
    r.hook("on_session_end", sessions.on_session_end)
    home_b = r.tmp / "profile-b"
    home_b.mkdir()
    omh_b = r.tmp / "omh-b"
    omh_b.mkdir()
    for home, omh in ((r.home, r.omh_home), (home_b, omh_b)):
        (home / "config.yaml").write_text(
            "plugins:\n  entries:\n    omh:\n      settings:\n        omh_home: " + str(omh) + "\n"
            "terminal:\n  backend: local\n",
            encoding="utf-8",
        )
    previous = is_multiplex_active()
    set_multiplex_active(True)
    secret_token = set_secret_scope({})
    try:
        for home, omh in ((r.home, r.omh_home), (home_b, omh_b), (r.home, r.omh_home)):
            token = set_hermes_home_override(home)
            try:
                # Same identities in both profiles must not share the count/cache.
                r.call("write_file", {"path": str(omh / "effect.txt"), "content": "real effect\n"})
                assert r.budget.engagement_count("parent-session", r.budget.MUTATIONS_FIELD, omh_home=str(omh)) == 1
                if home == r.home:
                    invoke_hook("subagent_start", parent_session_id="parent-session", child_session_id="child-a")
                assert bool(r.budget.engagement_count("parent-session", r.budget.DELEGATION_LATCH_FIELD, omh_home=str(omh))) is (home == r.home)
                assert r.budget.session_is_delegated("child-a") is (home == r.home)
                invoke_hook("on_session_end", session_id="parent-session", task_id="task-one", turn_id="turn-one")
                assert r.budget.engagement_count("parent-session", r.budget.MUTATIONS_FIELD, omh_home=str(omh)) == 1
            finally:
                reset_hermes_home_override(token)
    finally:
        reset_secret_scope(secret_token)
        set_multiplex_active(previous)

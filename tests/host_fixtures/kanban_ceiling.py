"""Model-free C acceptance through native Hermes registry/bridge and lifecycle.

Run ONLY via scripts/run_tests.sh with -o omh_checkout=/absolute/OMH/checkout.
Native-show cases use the real SQLite store and kanban_show handler. Unknown
root-field and arbitration probes substitute ONLY the registry handler's raw
result with explicitly synthetic native-shaped data; dispatch and transforms
stay real. No AIAgent/model is constructed, no dispatcher is started.

The ceiling covers the returned label + serialized JSON + omh_readback metadata,
not malformed/already-labelled passthrough or another plugin's winning string.
This is a local cross-checkout fixture, not a proposed vendored OMH dependency.
"""
from __future__ import annotations

import importlib
import importlib.util
import itertools
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


CEILING = 24_000


@pytest.fixture
def runtime(tmp_path, monkeypatch, request):
    home, omh_home = tmp_path / "hermes", tmp_path / "omh"
    home.mkdir()
    omh_home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OMH_HOME", str(omh_home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "synthetic-board.db"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (home / "config.yaml").write_text(
        "terminal:\n  backend: local\n"
        "tools:\n  tool_search:\n    defer: [kanban_show, kanban_list, kanban_attachments, read_file]\n",
        encoding="utf-8",
    )
    forbidden = []

    def no_external(*args, **kwargs):
        forbidden.append("network/model attempted")
        raise AssertionError("Network and models forbidden in C host integration")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, no_external)
    monkeypatch.setattr(socket, "create_connection", no_external)
    monkeypatch.setattr(socket, "getaddrinfo", no_external)
    import model_tools
    import run_agent
    import openai
    from hermes_cli import plugins, kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.plugins_manifest import PluginManifest
    from tools import kanban_tools

    monkeypatch.setattr(run_agent.AIAgent, "__init__", no_external)
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", no_external)
    monkeypatch.setattr(openai, "OpenAI", no_external)
    monkeypatch.setattr(openai, "AsyncOpenAI", no_external)
    manager = plugins.PluginManager()
    manager._discovered = True  # Test-owned hooks only; never installed plugins.
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    ctx = plugins.PluginContext(
        PluginManifest(name="omh-c-host-test", version="1.0.0"), manager
    )
    competitor_ctx = plugins.PluginContext(
        PluginManifest(name="foreign-c-host-test", version="1.0.0"), manager
    )
    source = next(
        (v.partition("=")[2] for v in request.config.getoption("override_ini") or []
         if v.startswith("omh_checkout=")),
        None,
    )
    assert source and Path(source).is_absolute(), "Pass -o omh_checkout=/absolute/checkout"
    bundle = Path(source).resolve() / "src/plugin_bundle/omh"
    package = "_omh_kanban_ceiling_host_test"
    spec = importlib.util.spec_from_file_location(
        package, bundle / "__init__.py", submodule_search_locations=[str(bundle)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, package, module)
    spec.loader.exec_module(module)
    load = lambda name: importlib.import_module(f"{package}.{name}")
    load("runtime_paths").note_host_registration(ctx)
    transform_module = load("hooks.result_transforms")
    readback = load("kanban_readback")
    for loaded in (transform_module, readback):
        assert loaded.__file__ is not None
        assert Path(loaded.__file__).resolve().is_relative_to(bundle)
    print(f"C_SOURCE={bundle} HOST_SOURCE={Path(model_tools.__file__).resolve()}")
    events, leases = [], []

    def hook(name, callback, *, foreign=False):
        lease = (competitor_ctx if foreign else ctx).register_hook(name, callback)
        leases.append(lease)
        return lease

    for event in ("pre_tool_call", "post_tool_call", "transform_tool_result"):
        hook(event, lambda _event=event, **kw: events.append((_event, dict(kw))))
    transform = transform_module.transform_tool_result
    transform_lease = hook("transform_tool_result", transform)
    calls = itertools.count()

    def call(tool, args, *, bridge=False):
        events.clear()
        identity = f"c-call-{next(calls)}"
        name, arguments = tool, args
        if bridge:
            name, arguments = "tool_call", {"calls": [{"name": tool, "arguments": args}]}
        result = model_tools.handle_function_call(
            name, arguments, task_id="c-task", session_id="c-session",
            tool_call_id=identity, turn_id="c-turn", api_request_id="c-request",
            enabled_toolsets=["kanban", "file"],
        )
        assert [event for event, _ in events] == [
            "pre_tool_call", "post_tool_call", "transform_tool_result"
        ], events
        for _, event in events:
            assert event["tool_name"] == tool
            assert event["tool_call_id"] == identity
            assert event["session_id"] == "c-session"
            assert event["turn_id"] == "c-turn"
            assert event["api_request_id"] == "c-request"
        post = events[1][1]
        assert post["status"] == "ok", result
        assert post["result"] == events[2][1]["result"]
        return SimpleNamespace(result=result, raw=post["result"])

    def fixture_result(tool, text):
        # Explicit handler substitution, NOT a production Kanban read assertion.
        entry = model_tools.registry.get_entry(tool)
        assert entry is not None
        monkeypatch.setattr(entry, "handler", lambda args, **kw: text)

    def native_task(text):
        entry = model_tools.registry.get_entry("kanban_show")
        assert entry is not None and entry.handler is kanban_tools._handle_show
        conn = kbc.connect()
        try:
            tid = kb.create_task(
                conn, title="synthetic ceiling task", body=text,
                assignee="default", workspace_kind="scratch", initial_status="running",
            )
            task = kb.claim_task(conn, tid, claimer="synthetic-test-owner")
            assert task is not None and task.current_run_id is not None
            rid = task.current_run_id
            assert kb.complete_task(
                conn, tid, result=text, summary=text, metadata={"synthetic_log": text},
                expected_run_id=rid,
            )
            # Seed retained error fields in this throwaway DB; no claim that a
            # completed worker would naturally populate both errors this way.
            conn.execute("UPDATE tasks SET last_failure_error=? WHERE id=?", (text, tid))
            conn.execute("UPDATE task_runs SET error=? WHERE id=?", (text, rid))
            for index in range(6):
                kb.add_comment(conn, tid, "synthetic-test", f"comment {index}: {text}")
            row, latest = kb.get_task(conn, tid), kb.latest_run(conn, tid)
            assert row is not None and latest is not None
            assert row.status == "done" and latest.id == rid
            assert latest.outcome == "completed" and latest.error == text
            return tid, rid, latest.status
        finally:
            conn.close()

    try:
        yield SimpleNamespace(
            call=call, fixture_result=fixture_result, native_task=native_task,
            hook=hook, transform=transform, transform_lease=transform_lease,
            readback=readback, tmp=tmp_path,
        )
    finally:
        for lease in reversed(leases):
            lease.dispose()
        for name in list(sys.modules):
            if name.startswith(package + "."):
                del sys.modules[name]
        assert not forbidden, forbidden


def _bounded(text):
    label, separator, body = text.partition("\n")
    assert separator and label.startswith("[OMH board readback]"), text[:200]
    payload = json.loads(body)  # JSON body, not the intentionally prefixed envelope.
    assert payload["omh_readback"]["schema_version"] == "omh_kanban_readback/v1"
    print(f"C_ENVELOPE_CHARS={len(text)} JSON_CHARS={len(body)}")
    length = len(text)
    assert length <= CEILING, f"Final serialized envelope is {length} > {CEILING} characters"
    return label, payload


def _latest_truth(label, payload, tid, rid, status):
    assert payload["task"]["id"] == tid
    assert payload["task"]["status"] == "done"
    assert payload["runs"][-1]["id"] == rid
    assert payload["runs"][-1]["status"] == status
    assert payload["runs"][-1]["outcome"] == "completed"
    metadata = payload["omh_readback"]
    assert metadata["confidence"] == "reported done"
    assert "reported done" in label and "not checked" in label
    assert metadata.get("verified") is not True


@pytest.mark.parametrize("bridge", [False, True], ids=["direct", "tool-call"])
@pytest.mark.parametrize("alphabet", ["ascii", "quotes-backslashes", "controls", "unicode"])
def test_native_show_bounds_final_serialization_and_preserves_latest_truth(runtime, bridge, alphabet):
    r = runtime
    unit = {"ascii": "x", "quotes-backslashes": '\\"', "controls": "\x00\x01\n\t", "unicode": "🐈東京ä"}[alphabet]
    tid, rid, status = r.native_task(unit * 9000)
    outcome = r.call("kanban_show", {"task_id": tid}, bridge=bridge)
    raw = json.loads(outcome.raw)
    assert len(outcome.raw) > CEILING
    assert raw["task"]["id"] == tid and raw["runs"][-1]["id"] == rid
    label, bounded = _bounded(outcome.result)
    _latest_truth(label, bounded, tid, rid, status)
    assert bounded["omh_readback"]["truncated"] is True
    assert r.readback.transform_kanban_readback("kanban_show", outcome.result) is None


@pytest.mark.parametrize("bridge", [False, True], ids=["direct", "tool-call"])
@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085"])
def test_native_diff_looking_json_is_not_padded_after_bounding(runtime, bridge, separator):
    r = runtime
    text = separator.join(["prefix", "--- a/x", "+++ b/x", "@@ -1 +1 @@", "-x", "+" + "y" * 100, "suffix"])
    tid, rid, status = r.native_task(text)
    outcome = r.call("kanban_show", {"task_id": tid}, bridge=bridge)
    label, bounded = _bounded(outcome.result)
    _latest_truth(label, bounded, tid, rid, status)
    assert bounded["task"]["body"] == text
    assert r.transform(tool_name="kanban_show", result=outcome.result) is None


def _synthetic_show():
    return {
        "task": {"id": "synthetic-task", "status": "done", "body": "\x00" * 9000,
                 "result": "\x00" * 9000, "last_failure_error": "\x00" * 9000},
        "worker_context": "\x00" * 9000,
        "runs": [
            {"id": 10, "status": "ended", "outcome": "crashed", "summary": "old"},
            {"id": 11, "status": "ended", "outcome": "completed", "summary": "\x00" * 9000,
             "error": "\x00" * 9000, "metadata": {"log": "\x00" * 9000}},
        ],
        "comments": [{"body": "old" * 9000}],
        "events": [],
    }


@pytest.mark.parametrize("bridge", [False, True], ids=["direct", "tool-call"])
@pytest.mark.parametrize("tool", ["kanban_show", "kanban_list", "kanban_attachments"])
@pytest.mark.parametrize("preannotated", [False, True], ids=["raw", "stale-annotation"])
def test_registry_unknown_root_fields_cannot_defeat_final_ceiling(runtime, tool, bridge, preannotated):
    r = runtime
    payload: dict = _synthetic_show() if tool == "kanban_show" else {
        "tasks" if tool == "kanban_list" else "attachments": [{"id": "synthetic-row"}]
    }
    payload["synthetic_future_root_field"] = {"unbounded": "\x00🐈\\\"" * 20000}
    if preannotated:
        payload["omh_readback"] = {"confidence": "verified", "truncated": False}
    raw = json.dumps(payload)
    r.fixture_result(tool, raw)
    args = {} if tool == "kanban_list" else {"task_id": "synthetic-task"}
    outcome = r.call(tool, args, bridge=bridge)
    assert outcome.raw == raw
    label, bounded = _bounded(outcome.result)
    assert bounded["omh_readback"]["truncated"] is True
    assert bounded["omh_readback"]["projection"] == "core_fields_only"
    assert "content omitted" in label
    assert "synthetic_future_root_field" not in bounded
    assert bounded["omitted_extra_fields"] > 0
    if tool == "kanban_show":
        _latest_truth(label, bounded, "synthetic-task", 11, "ended")
    else:
        key = "tasks" if tool == "kanban_list" else "attachments"
        assert len(bounded[key]) + bounded["omh_readback"][f"dropped_{key}"] == 1
        assert f"{len(bounded[key])} of 1" in label


@pytest.mark.parametrize("bridge", [False, True], ids=["direct", "tool-call"])
@pytest.mark.parametrize("first", ["omh", "foreign"])
def test_native_first_string_arbitration_is_not_a_cross_plugin_hard_cap(runtime, bridge, first):
    r = runtime
    raw = json.dumps(_synthetic_show())
    r.fixture_result("kanban_show", raw)
    r.transform_lease.dispose()
    observed, candidates = [], []
    foreign_result = json.dumps({"foreign": True, "uncapped": "x" * (CEILING + 1)})

    def omh(**kw):
        observed.append(("omh", kw["result"]))
        candidate = r.transform(**kw)
        candidates.append(candidate)
        return candidate

    def foreign(**kw):
        observed.append(("foreign", kw["result"]))
        return foreign_result

    callbacks = [("omh", omh), ("foreign", foreign)]
    if first == "foreign":
        callbacks.reverse()
    for owner, callback in callbacks:
        r.hook("transform_tool_result", callback, foreign=owner == "foreign")
    outcome = r.call("kanban_show", {"task_id": "synthetic-task"}, bridge=bridge)
    # Pinned native host eagerly invokes both listeners with the SAME RAW input;
    # it does not chain transformations. Only the first string reaches caller.
    assert observed == [(owner, raw) for owner, _ in callbacks]
    assert len(candidates) == 1 and isinstance(candidates[0], str)
    if first == "omh":
        assert outcome.result == candidates[0]
        label, bounded = _bounded(outcome.result)
        _latest_truth(label, bounded, "synthetic-task", 11, "ended")
    else:
        assert outcome.result == foreign_result
        assert len(outcome.result) > CEILING
        assert json.loads(outcome.result)["foreign"] is True
        # OMH still computes a candidate, but did NOT enforce the delivered cap.
        _bounded(candidates[0])


@pytest.mark.parametrize("bridge", [False, True], ids=["direct", "tool-call"])
@pytest.mark.parametrize("kind", ["foreign-tool", "malformed-json", "second-pass"])
def test_passthrough_boundaries_do_not_claim_a_universal_host_cap(runtime, bridge, kind):
    r = runtime
    tool = "read_file" if kind == "foreign-tool" else "kanban_show"
    raw = json.dumps(_synthetic_show())
    if kind == "malformed-json":
        raw = "{not-json:" + "x" * (CEILING + 1)
    if kind == "second-pass":
        raw = r.readback.transform_kanban_readback(
            "kanban_show", json.dumps({"task": {"id": "small", "status": "ready"}, "runs": []})
        )
        assert isinstance(raw, str)
    assert r.readback.transform_kanban_readback(tool, raw) is None
    r.fixture_result(tool, raw)
    args = {"path": str(r.tmp / "synthetic.txt")} if kind == "foreign-tool" else {"task_id": "synthetic-task"}
    outcome = r.call(tool, args, bridge=bridge)
    assert outcome.result == raw
    if kind == "malformed-json":
        assert len(outcome.result) > CEILING

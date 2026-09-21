"""Model-free N1 acceptance against an explicitly selected OMH source checkout.

Run with scripts/run_tests.sh and -o omh_checkout=/absolute/checkout. The default
is the sibling omh-tool-identity, NOT the installed editable package or the A
engagement checkout. Real PluginContext, lifecycle, registry, file handlers,
tool_call bridge and native approval persistence run in temporary homes. Only
the human response boundary is substituted; no model is constructed or called.

To demonstrate RED, run the same file with omh_checkout pointing at the isolated
pre-N1 base. Repeated writes remove their target first so host overwrite guards
cannot replace OMH's result; filesystem effects are asserted after every call.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import itertools
import json
import re
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def runtime(tmp_path, monkeypatch, request):
    home, omh_home = tmp_path / "hermes", tmp_path / "omh"
    home.mkdir()
    omh_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OMH_HOME", str(omh_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (home / "config.yaml").write_text(
        "terminal:\n  backend: local\n"
        "approvals:\n  mode: manual\n"
        "tools:\n  tool_search:\n    defer: [write_file, read_file]\n",
        encoding="utf-8",
    )
    forbidden = []

    def no_network(*args, **kwargs):
        forbidden.append("network attempted")
        raise AssertionError("No network or model access in N1 integration")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    import model_tools
    from hermes_cli import plugins
    from hermes_cli.plugins_manifest import PluginManifest
    from tools import approval, terminal_tool

    # Per-file pytest isolation does not reset module globals between cases.
    for name, empty in (
        ("_permanent_approved", set()),
        ("_permanent_approved_by_home", {}),
        ("_permanent_baseline_by_home", {}),
        ("_session_approved", {}),
    ):
        monkeypatch.setattr(approval, name, empty)
    approval.load_permanent_allowlist()
    prompts = []
    answer = {"choice": "deny"}

    def human_response(command, description, **kwargs):
        prompts.append({"command": command, "description": description, **kwargs})
        return answer["choice"]

    previous_callback = terminal_tool._get_approval_callback()
    terminal_tool.set_approval_callback(human_response)
    manager = plugins.PluginManager()
    manager._discovered = True  # Never scan installed plugins.
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    ctx = plugins.PluginContext(
        PluginManifest(name="omh-n1-host-test", version="1.0.0"), manager
    )
    source = next(
        (
            value.partition("=")[2]
            for value in request.config.getoption("override_ini") or []
            if value.startswith("omh_checkout=")
        ),
        None,
    )
    checkout = (
        Path(source)
        if source
        else Path(__file__).resolve().parents[3] / "omh-tool-identity"
    )
    bundle = checkout / "src/plugin_bundle/omh"
    assert (bundle / "__init__.py").is_file(), "Select a full OMH source checkout"
    package = "_omh_tool_identity_host_test"
    spec = importlib.util.spec_from_file_location(
        package, bundle / "__init__.py", submodule_search_locations=[str(bundle)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, package, module)
    spec.loader.exec_module(module)

    def load(name):
        return importlib.import_module(f"{package}.{name}")

    load("runtime_paths").note_host_registration(ctx)
    budget = load("hooks.nudge_budget")
    budget.reset_nudge_budget()
    load("hooks.session_attendance").reset_session_attendance()
    engagement = load("engagement_nudges")
    engagement.reset_engagement_declines()
    bursts = load("tool_bursts")
    assert bursts.__file__ is not None
    assert Path(bursts.__file__).resolve().is_relative_to(bundle.resolve())
    events, leases = [], []
    for name in (
        "pre_tool_call",
        "post_tool_call",
        "pre_approval_request",
        "post_approval_response",
    ):
        leases.append(
            ctx.register_hook(
                name, lambda _name=name, **kw: events.append((_name, dict(kw)))
            )
        )
    hooks = load("hooks.tool_hooks")
    leases.append(ctx.register_hook("pre_tool_call", hooks.pre_tool_call))
    leases.append(ctx.register_hook("post_tool_call", hooks.post_tool_call))
    leases.append(
        ctx.register_hook(
            "transform_tool_result",
            load("hooks.result_transforms").transform_tool_result,
        )
    )
    ids = itertools.count()

    def call(tool, args, *, bridge=False, session="n1-session"):
        events.clear()
        call_id = f"call-{next(ids)}"
        name, payload = tool, args
        if bridge:
            name, payload = "tool_call", {"calls": [{"name": tool, "arguments": args}]}
        result = model_tools.handle_function_call(
            name,
            payload,
            task_id=call_id,
            session_id=session,
            tool_call_id=call_id,
            turn_id="n1-turn",
            api_request_id="n1-request",
            enabled_toolsets=["file"],
        )
        posts = [kw for event, kw in events if event == "post_tool_call"]
        assert len(posts) == 1, events
        post = posts[0]
        assert post["tool_name"] == tool, result
        assert post["tool_call_id"] == call_id
        assert post["session_id"] == session
        assert post["turn_id"] == "n1-turn"
        assert post["api_request_id"] == "n1-request"
        pres = [kw for event, kw in events if event == "pre_tool_call"]
        assert len(pres) == 1 and pres[0]["tool_name"] == tool
        return SimpleNamespace(result=result, post=post)

    try:
        yield SimpleNamespace(
            call=call,
            tmp=tmp_path,
            home=home,
            omh_home=omh_home,
            bursts=bursts,
            budget=budget,
            engagement=engagement,
            events=events,
            prompts=prompts,
            answer=answer,
            approval=approval,
        )
    finally:
        terminal_tool.set_approval_callback(previous_callback)
        for lease in reversed(leases):
            lease.dispose()
        budget.reset_nudge_budget()
        for name in list(sys.modules):
            if name.startswith(package + "."):
                del sys.modules[name]
        assert not forbidden


def _write(r, args, *, bridge=False, session="n1-session", blocked=False):
    target = Path(args["path"])
    target.unlink(missing_ok=True)
    outcome = r.call("write_file", args, bridge=bridge, session=session)
    if blocked:
        assert outcome.post["status"] == "blocked", outcome.result
        assert not target.exists(), "A blocked call must have no write effect"
    else:
        assert outcome.post["status"] == "ok", outcome.result
        assert json.loads(outcome.post["result"])["verified"] is True
        assert target.read_text(encoding="utf-8") == args["content"]
    return outcome


def _arm(r, args, *, bridge=False, session="n1-session"):
    for _ in range(r.bursts.REPEAT_CALL_BLOCK_THRESHOLD):
        _write(r, args, bridge=bridge, session=session)


@pytest.mark.parametrize("bridge", [False, True], ids=["direct", "tool-call"])
@pytest.mark.parametrize("difference", ["target", "suffix", "unicode-suffix"])
def test_long_shared_prefix_does_not_block_a_distinct_write(
    runtime, bridge, difference
):
    r = runtime
    suffix = "ä" if difference == "unicode-suffix" else "A"
    args = {
        "content": "private-content-marker-" + "x" * 9000 + suffix,
        "path": str(r.tmp / "private-target-a.txt"),
    }
    other = dict(args)
    if difference == "target":
        other["path"] = str(r.tmp / "private-target-b.txt")
    else:
        other["content"] = args["content"][:-1] + (
            "ö" if difference == "unicode-suffix" else "b"
        )
    # Independently establish the original collision shape, not the N1 algorithm.
    first, second = (json.dumps(a, sort_keys=True).encode() for a in (args, other))
    assert first[:8192] == second[:8192] and first != second
    assert len(first) == len(second)
    _arm(r, args, bridge=bridge)
    _write(r, other, bridge=bridge)
    ledger = r.bursts.tool_bursts_path(str(r.omh_home)).read_text()
    assert "private-content-marker" not in ledger
    assert "private-target" not in ledger
    assert "ä" not in ledger


@pytest.mark.parametrize("bridge", [False, True], ids=["direct", "tool-call"])
def test_identical_arguments_with_reordered_dict_still_block(runtime, bridge):
    r = runtime
    args = {
        "content": "Unicode 🐈 東京 ä " + "x" * 9000,
        "path": str(r.tmp / "same.txt"),
    }
    _arm(r, args, bridge=bridge)
    outcome = _write(r, dict(reversed(list(args.items()))), bridge=bridge, blocked=True)
    assert "OMH Repeat Guard" in outcome.result
    digest = r.bursts.tool_args_digest(args)
    assert re.fullmatch(r"v2:[0-9a-f]{32}", digest), digest


def _noncanonical(kind):
    if kind == "cycle":
        value = []
        value.append(value)
        return value
    if kind == "depth":
        value = None
        for _ in range(70):
            value = [value]
        return value
    return {
        "nan": lambda: float("nan"),
        "bytes": lambda: b"not-json",
        "integer-key": lambda: {1: "not-a-string-key"},
        "object": object,
        "surrogate": lambda: "\ud800",
        "surrogate-pair": lambda: "\ud83d\ude00",
        "byte-budget": lambda: "x" * (1024 * 1024 + 1),
        "node-budget": lambda: [None] * 5000,
        "integer-budget": lambda: 1 << 5000,
    }[kind]()


@pytest.mark.parametrize(
    "kind",
    [
        "nan",
        "bytes",
        "integer-key",
        "object",
        "cycle",
        "surrogate",
        "surrogate-pair",
        "byte-budget",
        "node-budget",
        "depth",
        "integer-budget",
    ],
)
def test_unknown_arguments_break_repeat_continuity(runtime, kind):
    r = runtime
    args = {"content": "known arguments", "path": str(r.tmp / "unknown.txt")}
    _arm(r, args)
    _write(r, args, blocked=True)
    unknown = {**args, "metadata": _noncanonical(kind)}
    # Extra metadata is deliberately ignored by the REAL host file handler;
    # it remains visible to both hooks. No mock manufactures an unknown digest.
    _write(r, unknown)
    assert r.bursts.tool_args_digest(unknown) == ""
    _write(r, args)


@pytest.mark.parametrize("bridge", [False, True], ids=["direct", "tool-call"])
def test_tuple_arrays_have_explicit_canonical_json_semantics(runtime, bridge):
    """Tuples are intentionally JSON arrays, not an unknown-domain probe."""
    r = runtime
    args = {"content": "array semantics", "path": str(r.tmp / "array.txt"), "metadata": (1, 2)}
    _arm(r, args, bridge=bridge)
    equivalent = {**args, "metadata": [1, 2]}
    assert re.fullmatch(r"v2:[0-9a-f]{32}", r.bursts.tool_args_digest(args))
    assert r.bursts.tool_args_digest(args) == r.bursts.tool_args_digest(equivalent)
    _write(r, equivalent, bridge=bridge, blocked=True)


def test_unknown_read_is_not_distinct_engagement_evidence(runtime):
    r = runtime
    threshold = r.engagement.DELEGATION_NUDGE_DIRECT_READ_THRESHOLD
    for index in range(threshold - 1):
        target = r.tmp / f"read-{index}.txt"
        target.write_text(f"evidence {index}\n")
        # Alphabetically early, same-prefix data keeps the path difference
        # beyond the old 8 KiB digest window on every distinct read.
        args = {"aaa_metadata": "x" * 9000, "path": str(target)}
        outcome = r.call("read_file", args)
        assert outcome.post["status"] == "ok"
        assert "omh_engagement" not in json.loads(outcome.result)
    target = r.tmp / "last-read.txt"
    target.write_text("last evidence\n")
    unknown = {"path": str(target), "metadata": float("nan")}
    outcome = r.call("read_file", unknown)
    assert outcome.post["status"] == "ok"
    assert r.bursts.tool_args_digest(unknown) == ""
    assert "omh_engagement" not in json.loads(outcome.result)
    outcome = r.call("read_file", {"path": str(target)})
    assert "omh_engagement" in json.loads(outcome.result)


@pytest.mark.parametrize("bridge", [False, True], ids=["direct", "tool-call"])
def test_changed_real_results_do_not_form_a_repeat(runtime, bridge):
    r = runtime
    target = r.tmp / "moving.txt"
    args = {"path": str(target), "offset": 1, "limit": 1}
    results = []
    for index in range(r.bursts.REPEAT_CALL_APPROVAL_THRESHOLD + 2):
        target.write_text(f"moving evidence {index:04d}\n")
        outcome = r.call("read_file", args, bridge=bridge)
        assert outcome.post["status"] == "ok", outcome.result
        content = json.loads(outcome.post["result"])["content"]
        assert f"moving evidence {index:04d}" in content
        results.append(content)
    assert len(set(results)) == len(results)
    assert not r.prompts


@pytest.mark.parametrize("bridge", [False, True], ids=["direct", "tool-call"])
def test_result_tail_only_changes_remain_outside_n1_full_argument_contract(
    runtime, bridge
):
    """Characterize the unchanged result-digest bound, NOT desired full-result identity."""
    r = runtime
    target = r.tmp / "long-moving.txt"
    args = {"path": str(target), "offset": 1, "limit": 20}
    prefix = ("x" * 1000 + "\n") * 9  # Stay below the host's per-line truncation.
    results = []
    for index in range(r.bursts.REPEAT_CALL_BLOCK_THRESHOLD):
        target.write_text(prefix + f"{index:04d}\n")
        outcome = r.call("read_file", args, bridge=bridge)
        assert outcome.post["status"] == "ok", outcome.result
        assert f"{index:04d}" in json.loads(outcome.post["result"])["content"]
        results.append(outcome.post["result"])
    assert len(set(results)) == len(results)
    assert len({len(value) for value in results}) == 1
    assert len({r.bursts.tool_result_digest(value) for value in results}) == 1
    target.write_text(prefix + "NEW!\n")
    outcome = r.call("read_file", args, bridge=bridge)
    assert outcome.post["status"] == "blocked", outcome.result
    assert "OMH Repeat Guard" in outcome.result
    assert target.read_text().endswith("NEW!\n")


@pytest.mark.parametrize("bridge", [False, True], ids=["direct", "tool-call"])
def test_native_always_approval_uses_full_identity_and_not_legacy_grant(
    runtime, bridge
):
    import yaml

    r = runtime
    args = {
        "content": "approval-secret-marker-" + "x" * 9000 + "A",
        "path": str(r.tmp / "approval-private-target.txt"),
    }
    other = {**args, "content": args["content"][:-1] + "B"}
    # Seed an actual old-version grant through the native gate, not by mocking
    # is_approved. This algorithm is intentionally only the negative control.
    old_digest = hashlib.blake2b(
        json.dumps(args, sort_keys=True, default=str).encode()[:8192], digest_size=8
    ).hexdigest()
    old_key = f"omh_repeat:write_file:{old_digest}"
    r.answer["choice"] = "always"
    assert r.approval.request_tool_approval(
        "write_file", "Legacy test grant", rule_key=old_key
    )["approved"]
    assert len(r.prompts) == 1

    def escalate(arguments, session):
        _arm(r, arguments, bridge=bridge, session=session)
        for _ in range(r.bursts.REPEAT_CALL_ESCALATION_ATTEMPTS):
            _write(r, arguments, bridge=bridge, session=session, blocked=True)
        return _write(r, arguments, bridge=bridge, session=session)

    escalate(args, "grant-a")
    assert len(r.prompts) == 2, "Legacy prefix-derived grant must not authorize N1"
    first_events = [kw for name, kw in r.events if name == "pre_approval_request"]
    assert len(first_events) == 1
    key_a = first_events[0]["pattern_key"]
    assert re.fullmatch(r"plugin_rule:omh_repeat:write_file:v2:[0-9a-f]{32}", key_a)
    stored = yaml.safe_load((r.home / "config.yaml").read_text())["command_allowlist"]
    assert key_a in stored and "plugin_rule:" + old_key in stored
    # Reload disk persistence and use a new session, not the session-only grant.
    r.approval.load_permanent(set())
    assert key_a in r.approval.load_permanent_allowlist()
    escalate(dict(reversed(list(args.items()))), "grant-a-new-session")
    assert len(r.prompts) == 2, "Full N1 grant should persist across sessions"
    escalate(other, "grant-b")
    assert len(r.prompts) == 3, (
        "Distinct same-length suffix requires its own human grant"
    )
    key_b = next(
        kw["pattern_key"] for name, kw in r.events if name == "pre_approval_request"
    )
    assert key_a != key_b
    stored = yaml.safe_load((r.home / "config.yaml").read_text())["command_allowlist"]
    assert {key_a, key_b, "plugin_rule:" + old_key} <= set(stored)
    metadata = r.bursts.tool_bursts_path(str(r.omh_home)).read_text()
    metadata += json.dumps(stored) + json.dumps(r.prompts)
    assert "approval-secret-marker" not in metadata
    assert "approval-private-target" not in metadata

from __future__ import annotations

from .. import runtime_paths

from collections.abc import Mapping
import json
from typing import Protocol, TypeGuard, runtime_checkable

from ..degradation import runtime_binding_degradation
from ..approval_bypass import record_approval_bypass
from ..host_observation import observe_plugin_hook_call
from ..omh_roles import extract_role_marker, resolve_role_name, role_aliases, role_names
from ..tool_bursts import (
    record_repeat_refusal,
    record_tool_call,
    record_tool_call_close,
    repeat_call_directive,
    tool_args_digest,
)
from ..toolcall_rules import toolcall_rule_directive


@runtime_checkable
class _BoardBridge(Protocol):
    """The two names the tool-call hooks need from the board bridge."""

    def pre_agent_board(self, kwargs: Mapping[str, object]) -> dict[str, object] | None: ...
    def post_agent_board(self, kwargs: Mapping[str, object]) -> None: ...


def _agent_board_bridge() -> _BoardBridge | None:
    """Return the board bridge, or None when this host has no usable one.

    Deliberately not `except ModuleNotFoundError` with an `omh`-prefixed name
    check. Hermes' loaders keep a half-initialized module in `sys.modules`
    when `exec_module` raises, so this import can resolve to a stub that has
    the module but not the names: an `ImportError` whose `name` is the
    BUNDLE's own dotted path, never `omh`. The old check re-raised exactly
    that, and every tool call logged a hook warning while the bridge was dead
    anyway (#1623). Widening the check to `ImportError` would not have helped
    on its own -- the name it carries still is not `omh` -- so the bridge is
    taken by attribute instead: a module that cannot supply both names is no
    bridge, and one broken module must not take the hook down on every call.
    """
    try:
        from .. import agent_board_bridge
    except ImportError:
        return None
    return agent_board_bridge if isinstance(agent_board_bridge, _BoardBridge) else None


def pre_tool_call(**kwargs: object) -> dict[str, object] | None:
    """Return only host-supported pre-tool directives or role warnings."""
    try:
        omh_home = str(runtime_paths.plugin_home(kwargs.get("omh_home")))
        runtime_paths.plugin_home(kwargs.get("hermes_home"), hermes=True)
    except runtime_paths.UnattributableSessionError as exc:
        # No profile owns this session, so no store was named -- and the rules
        # this hook guards are opt-in by the presence of a file inside the
        # session's own store (`toolcall_rules`). There is no rules file to
        # leave unread here, so the veto protected nothing and cost the
        # session: in a multiplexed gateway every tool call of every
        # default-profile session came back blocked (#1674). Degrade instead,
        # the posture `post_tool_call` and `pre_llm_call` already take, and
        # keep the veto for every refusal that did name a store.
        return runtime_binding_degradation(exc)
    except (runtime_paths.RuntimeBindingError, OSError, RuntimeError) as exc:
        # A store was named and then rejected or could not be read: a
        # malformed setting, an unresolvable path, an unverified owner, a
        # config read that failed. A rules file may exist in it and may be
        # blocking this very tool, so a missing rules owner cannot safely
        # authorize the call. This is the native host's supported veto, not a
        # swallowed rule failure.
        return {**runtime_binding_degradation(exc), "action": "block",
                "message": "OMH runtime binding unavailable; tool rules could not be checked. Tool call blocked."}
    _ = observe_plugin_hook_call("pre_tool_call", kwargs)
    # The approval-bypass ledger observes session state, not this call's
    # outcome, so it ticks before the rule gate — a blocked call still sees
    # the same Shift+Tab flag.
    record_approval_bypass(omh_home=omh_home)
    # User-authored toolcall rules intervene first: a block directive is the
    # strongest host-supported response (hermes_cli/plugins.py,
    # `_get_pre_tool_call_directive_details`: "``block`` vetoes the tool call
    # outright (the message becomes the tool result the model sees)"). The
    # host passes the tool arguments as ``args``; ``tool_input`` is accepted
    # for bundle-internal callers and tests.
    tool_input = kwargs.get("tool_input") if "tool_input" in kwargs else kwargs.get("args")
    session_id = str(kwargs.get("session_id", "") or kwargs.get("task_id", "") or "")
    rule_directive = toolcall_rule_directive(
        tool_name=kwargs.get("tool_name"),
        tool_input=tool_input,
        session_id=session_id,
        omh_home=omh_home,
    )
    if rule_directive is not None:
        # A blocked call never dispatches, so it must not tick the
        # parallel-shot burst ledger (its claim boundary is "the host
        # dispatched the calls as one batch").
        return dict(rule_directive)
    # The repeat guard runs after the user's own rules and before anything
    # that observes a dispatch, for the same reason: it intervenes on a
    # call, so nothing downstream may record that call as having happened.
    # Hashed once here and handed to the gate, the interception counter and
    # the ledger, so a tool call canonicalizes its arguments exactly once.
    # `block` at the first stage and `approve` at the second are both
    # host-supported here (hermes_cli/plugins.py,
    # `_get_pre_tool_call_directive_details`: "``{"action": "approve",
    # "message", "rule_key"?}`` (escalate ANY tool to the human-approval
    # gate; ``rule_key`` picks the ``[a]lways`` allowlist grain)").
    args_digest = tool_args_digest(tool_input)
    repeat_directive = repeat_call_directive(
        tool_name=kwargs.get("tool_name"),
        args_digest=args_digest,
        session_id=session_id,
        omh_home=omh_home,
    )
    if repeat_directive is not None:
        # Counted as intercepted, not as a call: it is what moves the
        # streak from the block stage to the approval stage, and it is
        # deliberately not evidence that the call ran or that the person
        # denied it. OMH never observes how the host's gate was answered.
        record_repeat_refusal(
            tool_name=kwargs.get("tool_name"),
            args_digest=args_digest,
            session_id=session_id,
            omh_home=omh_home,
        )
        return dict(repeat_directive)
    # Only the normal host loop invokes native Kanban tools. Correlation runs
    # after OMH's user veto; it never dispatches or grants a native permission.
    bridge = _agent_board_bridge()
    if bridge is not None:
        board_directive = bridge.pre_agent_board(kwargs)
        if board_directive is not None:
            return board_directive
    # Tick the parallel-shot ledger and, when the host supplies a
    # tool_call_id, open the in-flight entry post_tool_call closes. This is
    # the only place OMH can see either fact. The same write advances this
    # session's repeat streak, which is why the guard above counts only
    # calls that actually reached dispatch.
    record_tool_call(
        kwargs.get("tool_name"),
        omh_home=omh_home,
        tool_call_id=kwargs.get("tool_call_id"),
        turn_id=kwargs.get("turn_id"),
        args_digest=args_digest,
        session_id=session_id,
    )
    context_parts: list[str] = []
    payload: dict[str, object] = {}
    role_warning = _delegate_role_warning(kwargs)
    if role_warning:
        context_parts.append(role_warning)

    if not context_parts:
        return None
    payload["context"] = "\n\n".join(context_parts)
    return payload


def post_tool_call(**kwargs: object) -> dict[str, object] | None:
    """Close the in-flight ledger entry pre_tool_call opened for this call.

    A supported Hermes observer hook (`install/hook_integrity.py`
    `HOOK_REVIEWS["post_tool_call"]`), paired with pre_tool_call by
    tool_call_id. It never blocks or rewrites a tool result -- it only
    closes the exact in-flight state the HUD's liveness signal reads, which
    is what tells "stopped with an incomplete todo item" apart from "still
    running" and keeps the parallel-shot badge from lingering past the ring
    ceiling. A host that omits tool_call_id is a silent no-op here; the
    entry pre_tool_call never opened simply never closes early.
    """
    try:
        omh_home = str(runtime_paths.plugin_home(kwargs.get("omh_home")))
        runtime_paths.plugin_home(kwargs.get("hermes_home"), hermes=True)
    except (runtime_paths.RuntimeBindingError, OSError, RuntimeError) as exc:
        return runtime_binding_degradation(exc)
    _ = observe_plugin_hook_call("post_tool_call", kwargs)
    bridge = _agent_board_bridge()
    if bridge is not None:
        bridge.post_agent_board(kwargs)
    record_tool_call_close(
        kwargs.get("tool_call_id"),
        omh_home=omh_home,
    )
    return None


class _JsonDecoder(Protocol):
    def loads(self, s: str) -> object: ...


_decoder: _JsonDecoder = json


def _is_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _delegate_role_warning(kwargs: dict[str, object]) -> str:
    if str(kwargs.get("tool_name", "") or "") != "delegate_task":
        return ""
    tool_input: object = kwargs.get("tool_input") or {}
    if isinstance(tool_input, str):
        try:
            parsed = _decoder.loads(tool_input)
        except json.JSONDecodeError:
            return ""
        tool_input = parsed if _is_dict(parsed) else {}
    if not _is_dict(tool_input):
        return ""
    marker = extract_role_marker(str(tool_input.get("goal", "") or ""))
    if not marker:
        return ""
    available = role_names()
    aliases = role_aliases()
    if marker in available or resolve_role_name(marker) in available:
        return ""
    return (
        f"[OMH Role Warning] Unknown role '{marker}' in delegate_task goal. "
        f"Available roles: {', '.join(available) or '(none)'}. "
        f"Legacy aliases: {', '.join(sorted(aliases)) or '(none)'}. "
        "No OMH role context will be injected for that subagent."
    )

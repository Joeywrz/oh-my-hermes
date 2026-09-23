"""Producers for the OMH text Hermes can send per request, for `budget_metrics()`.

Three surfaces can reach the model on every turn of a session with OMH
installed, and none of them is the skill body total the older budget watches:

- the skill index lines (`src/skills/skill_index.py`), sent on every request;
- the plugin bundle's registered tool schemas, measured here by running the
  bundle's own `register()` against a recording context, so what is counted is
  what the host is handed, not a list kept beside it. This is the eager
  ceiling, not the default per-request cost: Hermes's `tools.tool_search`
  defaults to `auto` (an alias of `on`), and `tools/tool_search.py::
  is_deferrable_tool_name` treats every non-core plugin tool as deferrable, so
  under the default the `omh_*` schemas sit behind the tool_search bridge and
  only a bounded name-and-description listing rides each request, plus the
  schemas of tools the model describes or calls. All of it is paid per request
  only when `tools.tool_search.enabled` is `off`, or on a host without the
  bridge;
- the fenced context `pre_llm_call` returns, which Hermes appends to the API
  copy of the user message and replays on every later turn from the
  `api_content` sidecar. Its parts come from several producers (primer, route
  hint, role context, active workflow, running-work board, ...) joined with no
  aggregate cap, so it is measured over a fixed, named scenario set rather
  than as the primer alone.

The plugin bundle is imported from here, never the other way round: modules
under `src/plugin_bundle/omh/` must stay importable without `omh`
(`tests/test_plugin_bundle_standalone.py`).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

# The session every scenario runs under. Each scenario gets fresh OMH and
# Hermes homes, so nothing one scenario writes is visible to the next.
_SCENARIO_SESSION = "omh-per-turn-context-scenario"

# The running-work board shows at most this many rows (`read_running_work_board`
# is called with `limit=6` in `pre_llm_call`), so the scenario seeds exactly a
# full board.
_RUNNING_BOARD_UNITS = 6

# A request the router answers with a route hint; the hint is message-specific,
# so the scenario pins one message rather than claiming a bound over all of them.
_ROUTED_REQUEST = "review this PR for bugs before merge"
_PLAIN_REQUEST = "migrate the database schema and fix the tests"


class _RecordingPluginContext:
    """The two registration methods `register()` requires, recording what it passes."""

    def __init__(self) -> None:
        self.tool_schemas: dict[str, object] = {}
        self.hooks: dict[str, object] = {}

    def register_tool(self, name: str, toolset: str, schema: object, handler: object, **kwargs: object) -> None:
        self.tool_schemas[name] = schema

    def register_hook(self, name: str, handler: object) -> None:
        self.hooks[name] = handler


def registered_tool_schemas() -> dict[str, object]:
    """Every tool schema the plugin bundle registers, keyed by tool name."""
    from ..plugin_bundle.omh import register

    context = _RecordingPluginContext()
    register(context)
    return dict(context.tool_schemas)


def _schema_chars(schema: object) -> int:
    return len(json.dumps(schema, sort_keys=True, ensure_ascii=False))


def plugin_tool_schema_chars_by_tool() -> dict[str, int]:
    return {name: _schema_chars(schema) for name, schema in sorted(registered_tool_schemas().items())}


def plugin_tool_schema_chars() -> int:
    """Total serialized size of the registered tool schemas (JSON, sorted keys, unescaped)."""
    return sum(plugin_tool_schema_chars_by_tool().values())


def _seed_active_workflow(omh_home: Path) -> None:
    state = omh_home / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "plan-state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workflow": "plan",
                "active": True,
                "lifecycle_outcome": None,
                "session_ref": "sha256:" + hashlib.sha256(_SCENARIO_SESSION.encode("utf-8")).hexdigest(),
                "session_binding": "bound",
            }
        ),
        encoding="utf-8",
    )


def _seed_running_board(omh_home: Path) -> None:
    from datetime import datetime, timezone

    inflight = omh_home / "coding" / "fanout" / "fanout-000000000000" / "inflight"
    inflight.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for index in range(_RUNNING_BOARD_UNITS):
        (inflight / f"u{index}.json").write_text(
            json.dumps(
                {
                    "schema_version": "omh_inflight_marker/v1",
                    "owner": "codex",
                    "model": "gpt-6-astra",
                    "started_at": started_at,
                    "unit_state": "running",
                    "status": "running",
                }
            ),
            encoding="utf-8",
        )


def _run_pre_llm_call(seeds: tuple[Callable[[Path], None], ...], **kwargs: Any) -> int:
    from ..plugin_bundle.omh.hooks.llm_hooks import pre_llm_call

    with tempfile.TemporaryDirectory() as tmp:
        omh_home = Path(tmp) / "omh"
        hermes_home = Path(tmp) / "hermes"
        omh_home.mkdir()
        hermes_home.mkdir()
        for seed in seeds:
            seed(omh_home)
        payload = pre_llm_call(
            omh_home=str(omh_home),
            hermes_home=str(hermes_home),
            session_id=_SCENARIO_SESSION,
            **kwargs,
        )
    return len(str((payload or {}).get("context", "")))


def _largest_role(message_suffix: str, seeds: tuple[Callable[[Path], None], ...], **kwargs: Any) -> int:
    from ..plugin_bundle.omh.omh_roles import role_names

    return max(
        _run_pre_llm_call(seeds, user_message=f"[omh-role:{role}] {message_suffix}", **kwargs)
        for role in role_names()
    )


def pre_llm_call_context_scenario_chars() -> dict[str, int]:
    """Fenced `pre_llm_call` context size per named scenario.

    - `first_turn`: a session's first turn on a request with no OMH vocabulary
      (the primer alone).
    - `route_hint`: a first turn on a routed request (primer + route hint).
    - `role_marker`: a later turn carrying an `[omh-role:...]` marker, the
      largest shipped role.
    - `active_workflow`: a later turn while a workflow is active.
    - `running_work_board`: a later turn with a full running-work board.
    - `all_surfaces`: all of the above in one first turn. The parts add, so
      this is the largest of the seeded scenarios; it is not a bound on every
      turn, because the parts listed below are not seeded.

    `pre_llm_call` can append these parts, none of which any scenario reaches,
    so the metric does not move when they grow:

    - the Kanban board card: capped at `_BOARD_CARD_MAX_CHARS`, and its
      fixture is Hermes's own `state.db`/`kanban.db`;
    - the open-plan (todo) reminder: bounded per plan by
      `TODO_RECONCILIATION_FULL_TURNS`, gated in
      `tests/test_injected_context_pressure.py`;
    - the context-budget continuation (`render_context_budget`): needs a
      recorded budget crossing for the session and model;
    - the `[OMH continuation claim]` line: one finding sentence, and needs a
      prior assistant turn in `conversation_history`;
    - the active-executor status block: bounded in line count (a HUD line,
      two fixed lines, the latest run id, at most 3 executors and 3 runs, one
      fixed pointer line) but not in characters, since those lines carry
      recorded ids, profiles and statuses; needs live executor records under
      `omh_home`;
    - the `[OMH Degraded]` line: a fixed sentence plus the failing component
      names, emitted only when a local call failed.
    """
    no_seed: tuple[Callable[[Path], None], ...] = ()
    later = {"is_first_turn": False}
    return {
        "first_turn": _run_pre_llm_call(no_seed, user_message=_PLAIN_REQUEST, is_first_turn=True),
        "route_hint": _run_pre_llm_call(no_seed, user_message=_ROUTED_REQUEST, is_first_turn=True),
        "role_marker": _largest_role("continue", no_seed, **later),
        "active_workflow": _run_pre_llm_call((_seed_active_workflow,), user_message="continue", **later),
        "running_work_board": _run_pre_llm_call((_seed_running_board,), user_message="continue", **later),
        "all_surfaces": _largest_role(
            _ROUTED_REQUEST,
            (_seed_active_workflow, _seed_running_board),
            is_first_turn=True,
        ),
    }


def pre_llm_call_context_chars_max() -> int:
    return max(pre_llm_call_context_scenario_chars().values())


__all__ = [
    "plugin_tool_schema_chars",
    "plugin_tool_schema_chars_by_tool",
    "pre_llm_call_context_chars_max",
    "pre_llm_call_context_scenario_chars",
    "registered_tool_schemas",
]

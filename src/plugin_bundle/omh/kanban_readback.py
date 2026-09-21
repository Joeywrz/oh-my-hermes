"""Bound and label Hermes Kanban board readback before it enters context.

Native ``kanban_show`` returns a task's full ``runs[]`` (``summary``,
``error``, ``metadata``) and ``comments[]`` with no size cap, and
``kanban_complete`` / ``kanban_comment`` write those fields with no cap either
(``tools/kanban_tools.py``, ``hermes_cli/kanban_db.py``). A worker that pastes
a 50 KB log into its completion summary therefore lands 50 KB in the main
session's context on the next ``kanban_show``. The board bridge
(``agent_board_bridge.py``) correlates the call but discards the result text,
and the wrapper-side message gate has no plugin-hook caller, so the only seam
that sees the string on its way into context is ``transform_tool_result``.
This module is that pass.

Two jobs, both fail-open:

* **Bound.** Long text fields are cut to ``READBACK_FIELD_CEILING`` and old
  rows are dropped first, always keeping the latest run. The entire serialized
  output, including its label and metadata, must fit ``READBACK_PAYLOAD_CEILING``
  characters. Otherwise an explicitly disclosed core-field projection preserves
  native task/latest-run identities and states without chopping serialized JSON.
  This is a character ceiling, not a byte, token, or peak-memory guarantee.
* **Label.** A run outcome of ``completed`` is the executor's own report about
  itself; OMH observed the claim, never the result. The label line names that
  with the evidence vocabulary (``reported done``), and a task with no runs is
  ``not run``. Nothing here is execution, verification, review, CI, or merge
  evidence.

The bundle cannot import ``omh.evidence.labels``, so the confidence words are a
hand mirror kept equal by the parity test in ``tests/test_kanban_readback.py``.
"""

from __future__ import annotations

import json
from typing import Any, Final

KANBAN_READBACK_SCHEMA_VERSION: Final = "omh_kanban_readback/v1"
KANBAN_READBACK_KEY: Final = "omh_readback"
KANBAN_READBACK_TOOLS: Final[frozenset[str]] = frozenset(
    {"kanban_show", "kanban_list", "kanban_attachments"}
)

READBACK_FIELD_CEILING: Final = 2_000
READBACK_PAYLOAD_CEILING: Final = 24_000
READBACK_LIST_ROW_CEILING: Final = 50
# A heuristic reserve for ordinary row dropping, NOT the final-size guarantee.
# The fully serialized label + payload + metadata is measured before return;
# an oversized result falls back to an explicitly disclosed core projection.
_PAYLOAD_RESERVE: Final = 512

_TRUNCATION_MARKER: Final = "...[truncated by omh]"
_LABEL_PREFIX: Final = "[OMH board readback]"

# Text fields on the task row that a worker can grow without bound
# (`kanban_complete` writes `result`; `kanban_create` writes `body`).
_TASK_TEXT_FIELDS: Final[tuple[str, ...]] = ("body", "result", "last_failure_error")
# `metadata` is a dict on the wire (`Run.metadata: Optional[dict]`); it is
# serialised before it is measured so a large dict is cut the same way as text.
_RUN_TEXT_FIELDS: Final[tuple[str, ...]] = ("summary", "error", "metadata")
_COMMENT_TEXT_FIELDS: Final[tuple[str, ...]] = ("body",)
# `build_worker_context` re-embeds the body and the comments verbatim (capped
# on the Hermes side, but by its own budget); leaving it uncut would re-admit
# what the field cut above removed.
_SHOW_TEXT_FIELDS: Final[tuple[str, ...]] = ("worker_context",)

# Hand mirror of `omh.evidence.labels` confidence words and prose.
_CONFIDENCE_NOT_RUN: Final = "not run"
_CONFIDENCE_RUNNING: Final = "running"
_CONFIDENCE_REPORTED_DONE: Final = "reported done"
_CONFIDENCE_FAILED: Final = "failed"
_CONFIDENCE_BLOCKED: Final = "blocked"
_CONFIDENCE_CANCELLED: Final = "cancelled"
_CONFIDENCE_PROSE: Final[dict[str, str]] = {
    _CONFIDENCE_NOT_RUN: "ready to run, nothing has run yet",
    _CONFIDENCE_RUNNING: "running now",
    _CONFIDENCE_REPORTED_DONE: "the executor reported it done; the result was not checked",
    _CONFIDENCE_FAILED: "failed",
    _CONFIDENCE_BLOCKED: "blocked; something is in the way",
    _CONFIDENCE_CANCELLED: "cancelled before it finished",
}

# Native run outcomes -- every `outcome=` literal an `_end_run` /
# `_end_or_synthesize_run` caller writes in `hermes_cli/kanban_db.py` and
# `hermes_cli/kanban_db_dispatch.py` (v0.21.3) -- mapped onto the confidence
# axis. `dependency_wait` and `block_loop_detected` are event kinds, not run
# outcomes: the run they close carries `blocked`. Every outcome a worker
# reports about its own work is `reported done`; anything unlisted fails
# closed to `not run`, the way `omh.evidence.labels.confidence_label` does,
# because an unrecognised state must never render as work that happened. The
# hand list in `tests/test_kanban_readback.py` is what turns a new host outcome
# into a failing test instead of a silent `not run`.
_CONFIDENCE_BY_RUN_OUTCOME: Final[dict[str, str]] = {
    "completed": _CONFIDENCE_REPORTED_DONE,
    "review_requested": _CONFIDENCE_REPORTED_DONE,
    "changes_requested": _CONFIDENCE_REPORTED_DONE,
    "blocked": _CONFIDENCE_BLOCKED,
    # Parked by the host until a time gate opens (`schedule_task`): the run
    # ended and the task is not dispatchable, so something is in the way.
    "scheduled": _CONFIDENCE_BLOCKED,
    "crashed": _CONFIDENCE_FAILED,
    "spawn_failed": _CONFIDENCE_FAILED,
    "timed_out": _CONFIDENCE_FAILED,
    # The dispatcher's failure limit was reached: terminal, not "never ran".
    "gave_up": _CONFIDENCE_FAILED,
    "reclaimed": _CONFIDENCE_CANCELLED,
    "stale": _CONFIDENCE_CANCELLED,
    # A quota wall ended the run and the task was requeued; the host records
    # it apart from `crashed` so history shows no phantom crash.
    "rate_limited": _CONFIDENCE_CANCELLED,
}


def transform_kanban_readback(tool_name: object, result: object) -> str | None:
    """Return the bounded, labelled readback, or ``None`` to leave it alone.

    Fail-open by seam contract: a tool outside ``KANBAN_READBACK_TOOLS``, a
    result that is not a JSON object, or a within-budget object already carrying
    ``KANBAN_READBACK_KEY`` passes through untouched. Oversized annotated JSON
    is reprocessed; an annotation cannot exempt it from the ceiling. The returned string is
    one label line followed by the JSON payload; it is not itself JSON, so a
    second pass over it declines, which keeps the transform idempotent.
    """
    name = str(tool_name or "")
    if name not in KANBAN_READBACK_TOOLS:
        return None
    if not isinstance(result, str) or not result:
        return None
    try:
        parsed = json.loads(result)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if KANBAN_READBACK_KEY in parsed:
        if len(result) <= READBACK_PAYLOAD_CEILING:
            return None
        # A pre-existing annotation is not permission for oversized raw JSON.
        # Recompute its claims from the actual task/runs rather than trusting it.
        parsed.pop(KANBAN_READBACK_KEY)
    if name == "kanban_show":
        readback = _bound_show(parsed)
    elif name == "kanban_list":
        readback = _bound_list(parsed)
    else:
        readback = _bound_attachments(parsed)
    parsed[KANBAN_READBACK_KEY] = readback
    try:
        rendered = _render_readback(parsed, readback)
        if len(rendered) <= READBACK_PAYLOAD_CEILING:
            return rendered
        projected = _core_projection(name, parsed, readback)
        return _render_readback(projected, readback)
    except (TypeError, ValueError):
        return None


# --- kanban_show -----------------------------------------------------------


def _bound_show(parsed: dict[str, Any]) -> dict[str, Any]:
    truncated_fields = 0
    task = parsed.get("task")
    if isinstance(task, dict):
        truncated_fields += _cut_fields(task, _TASK_TEXT_FIELDS)
    truncated_fields += _cut_fields(parsed, _SHOW_TEXT_FIELDS)
    runs = _dict_rows(parsed, "runs")
    for run in runs:
        truncated_fields += _cut_fields(run, _RUN_TEXT_FIELDS)
    comments = _dict_rows(parsed, "comments")
    for comment in comments:
        truncated_fields += _cut_fields(comment, _COMMENT_TEXT_FIELDS)

    # Oldest first: Hermes lists comments by id ASC and runs by started_at ASC,
    # so dropping from the front keeps the newest, and the latest run is never
    # dropped because it is the one the label reads. The payload is measured
    # once and each drop subtracts its own row, so a comment-heavy task costs
    # one serialisation per dropped row rather than one per row per drop.
    size = _payload_size(parsed)
    size, dropped_comments = _drop_oldest(size, comments)
    size, dropped_runs = _drop_oldest(size, runs, keep=1)
    size, dropped_events = _drop_oldest(size, _dict_rows(parsed, "events"))

    confidence = _run_confidence(runs)
    label = _show_label(task, runs, confidence)
    truncated = bool(truncated_fields or dropped_comments or dropped_runs or dropped_events)
    if truncated:
        label += "; bounded: " + _bound_summary(
            truncated_fields,
            ("comments", dropped_comments),
            ("runs", dropped_runs),
            ("events", dropped_events),
        )
    return {
        "schema_version": KANBAN_READBACK_SCHEMA_VERSION,
        "truncated": truncated,
        "label": label,
        "confidence": confidence,
        "dropped_comments": dropped_comments,
        "dropped_runs": dropped_runs,
        "dropped_events": dropped_events,
        "truncated_fields": truncated_fields,
    }


def _show_label(task: object, runs: list[dict[str, Any]], confidence: str) -> str:
    task_id = _short(task.get("id")) if isinstance(task, dict) else ""
    status = _short(task.get("status")) if isinstance(task, dict) else ""
    head = f"task {task_id or '?'} status={status or '?'}; "
    if runs:
        head += f"latest run outcome={_short(runs[-1].get('outcome')) or '?'} -> {confidence}"
    else:
        head += f"no runs -> {confidence}"
    return f"{head} ({_CONFIDENCE_PROSE[confidence]})"


def _run_confidence(runs: list[dict[str, Any]]) -> str:
    """Confidence for the latest run, fail-closed to ``not run``."""
    if not runs:
        return _CONFIDENCE_NOT_RUN
    latest = runs[-1]
    outcome = _short(latest.get("outcome"))
    if outcome:
        return _CONFIDENCE_BY_RUN_OUTCOME.get(outcome, _CONFIDENCE_NOT_RUN)
    if _short(latest.get("status")) == "running" or latest.get("ended_at") is None:
        return _CONFIDENCE_RUNNING
    return _CONFIDENCE_NOT_RUN


# --- kanban_list -----------------------------------------------------------


def _bound_list(parsed: dict[str, Any]) -> dict[str, Any]:
    tasks = _dict_rows(parsed, "tasks")
    listed = len(tasks)
    dropped_tasks = 0
    if len(tasks) > READBACK_LIST_ROW_CEILING:
        dropped_tasks = len(tasks) - READBACK_LIST_ROW_CEILING
        del tasks[READBACK_LIST_ROW_CEILING:]
    _, dropped_by_size = _drop_oldest(_payload_size(parsed), tasks, newest_first=True)
    dropped_tasks += dropped_by_size
    truncated = dropped_tasks > 0
    label = (
        f"{len(tasks)} of {listed} tasks shown"
        + (f" ({dropped_tasks} dropped)" if truncated else "")
        + "; statuses are the board's own records: done means "
        + f"{_CONFIDENCE_REPORTED_DONE}, not verified"
    )
    return {
        "schema_version": KANBAN_READBACK_SCHEMA_VERSION,
        "truncated": truncated,
        "label": label,
        "dropped_comments": 0,
        "dropped_runs": 0,
        "dropped_tasks": dropped_tasks,
    }


# --- kanban_attachments ----------------------------------------------------


def _bound_attachments(parsed: dict[str, Any]) -> dict[str, Any]:
    attachments = _dict_rows(parsed, "attachments")
    listed = len(attachments)
    _, dropped_attachments = _drop_oldest(_payload_size(parsed), attachments)
    truncated = dropped_attachments > 0
    label = (
        f"{len(attachments)} of {listed} attachments listed"
        + (f" ({dropped_attachments} dropped)" if truncated else "")
        + "; a listing is not evidence any file was read"
    )
    return {
        "schema_version": KANBAN_READBACK_SCHEMA_VERSION,
        "truncated": truncated,
        "label": label,
        "dropped_comments": 0,
        "dropped_runs": 0,
        "dropped_attachments": dropped_attachments,
    }


# --- last-resort final-size projection -------------------------------------

# Fixed field sets and at most 256 serialized characters per scalar keep show
# projections below the ceiling even with worst-case escaping. List rows share
# a 12k serialized-character budget, leaving space for labels/metadata/root.
_CORE_TASK_FIELDS: Final = ("id", "title", "status", "assignee", "body", "result")
_CORE_RUN_FIELDS: Final = (
    "id", "task_id", "profile", "status", "outcome", "session_id",
    "started_at", "ended_at", "exit_code", "summary", "error",
)
_CORE_ATTACHMENT_FIELDS: Final = ("id", "task_id", "filename", "content_type", "size", "path")
_CORE_ROOT_FIELDS: Final = ("ok", "error", "task_id", "board", "count", "total", "limit", "truncated", "has_more")
_PREVIEW_FIELDS: Final = frozenset({"title", "body", "result", "summary", "error"})


def _render_readback(parsed: dict[str, Any], readback: dict[str, Any]) -> str:
    return f"{_LABEL_PREFIX} {readback['label']}\n{json.dumps(parsed, ensure_ascii=False, default=str)}"


def _core_record(record: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    omitted: list[str] = []
    shortened: list[str] = []
    for key in fields:
        if key not in record:
            continue
        value = record[key]
        scalar = value is None or isinstance(value, (str, bool, int, float))
        small = scalar and (not isinstance(value, str) or len(value) <= 256)
        if small and len(json.dumps(value, ensure_ascii=False)) <= 256:
            projected[key] = value
        elif key in _PREVIEW_FIELDS and isinstance(value, str):
            projected[key] = value[:32] + _TRUNCATION_MARKER
            shortened.append(key)
        else:
            # Do not manufacture another identity by clipping an ID/path/status.
            omitted.append(key)
    if omitted:
        projected["omitted_fields"] = omitted
    if shortened:
        projected["truncated_fields"] = shortened
    omitted_extra = sum(key not in fields for key in record)
    if omitted_extra:
        projected["omitted_extra_fields"] = omitted_extra
    return projected


def _core_projection(name: str, parsed: dict[str, Any], readback: dict[str, Any]) -> dict[str, Any]:
    """A disclosed bounded view, not an arbitrarily chopped JSON document."""
    projected = _core_record(parsed, _CORE_ROOT_FIELDS)
    readback.update(truncated=True, projection="core_fields_only")
    if name == "kanban_show":
        task = parsed.get("task")
        if isinstance(task, dict):
            projected["task"] = _core_record(task, _CORE_TASK_FIELDS)
        runs = _dict_rows(parsed, "runs")
        projected["runs"] = [_core_record(runs[-1], _CORE_RUN_FIELDS)] if runs else []
        readback["dropped_runs"] += max(0, len(runs) - 1)
        readback["dropped_comments"] += len(_dict_rows(parsed, "comments"))
        readback["dropped_events"] += len(_dict_rows(parsed, "events"))
        projected["comments"] = []
        projected["events"] = []
        # Rebuild counters in the label; the original confidence/identity prose
        # stays tied to the latest observed row, not to shortened preview text.
        readback["label"] = _show_label(task, runs, readback["confidence"])
        readback["label"] += "; bounded: " + _bound_summary(
            readback["truncated_fields"],
            ("comments", readback["dropped_comments"]),
            ("runs", readback["dropped_runs"]),
            ("events", readback["dropped_events"]),
        )
    else:
        key = "tasks" if name == "kanban_list" else "attachments"
        counter = f"dropped_{key}"
        rows = _dict_rows(parsed, key)
        listed = len(rows) + readback[counter]
        fields = _CORE_TASK_FIELDS if key == "tasks" else _CORE_ATTACHMENT_FIELDS
        budget = 12_000
        kept = []
        for row in rows:
            core = _core_record(row, fields)
            size = _payload_size(core) + 2
            if size > budget:
                break
            kept.append(core)
            budget -= size
        readback[counter] += len(rows) - len(kept)
        projected[key] = kept
        if key == "tasks":
            readback["label"] = (
                f"{len(kept)} of {listed} tasks shown ({readback[counter]} dropped)"
                "; statuses are the board's own records: done means reported done, not verified"
            )
        else:
            readback["label"] = (
                f"{len(kept)} of {listed} attachments listed ({readback[counter]} dropped)"
                "; a listing is not evidence any file was read"
            )
    readback["label"] += "; core fields only; other content omitted"
    projected[KANBAN_READBACK_KEY] = readback
    projected.pop("omitted_extra_fields", None)
    projected["omitted_extra_fields"] = sum(key not in projected for key in parsed)
    return projected


# --- helpers ---------------------------------------------------------------


def _dict_rows(parsed: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """The list under ``key`` when it is a list of objects, else ``[]``.

    A list of the wrong shape is left in place untouched rather than guessed
    at; the returned ``[]`` is not bound to ``parsed`` so nothing is rewritten.
    """
    rows = parsed.get(key)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return []
    return rows


def _cut_fields(record: dict[str, Any], names: tuple[str, ...]) -> int:
    """Cut every named text field over the ceiling; mark the record; count cuts."""
    cut: list[str] = []
    for name in names:
        value = record.get(name)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                continue
        elif isinstance(value, str):
            text = value
        else:
            continue
        if len(text) <= READBACK_FIELD_CEILING:
            continue
        record[name] = text[: READBACK_FIELD_CEILING - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
        cut.append(name)
    if cut:
        record["truncated"] = True
        record["truncated_fields"] = cut
    return len(cut)


def _payload_size(parsed: dict[str, Any]) -> int:
    """Serialised length of the payload, or ``0`` when it cannot be measured.

    ``0`` is the fail-open value: an unserialisable payload drops nothing here
    and ``transform_kanban_readback`` declines it at the final dump.
    """
    try:
        return len(json.dumps(parsed, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return 0


def _drop_oldest(
    size: int, rows: list[dict[str, Any]], *, keep: int = 0, newest_first: bool = False
) -> tuple[int, int]:
    """Pop rows in place until ``size`` fits the ceiling; return (size, dropped).

    Each pop subtracts the row's own serialised length plus its list
    separator, so the running size stays an upper bound on the payload
    without re-serialising it; ``_PAYLOAD_RESERVE`` covers the separator the
    last row of a list does not have.
    """
    limit = READBACK_PAYLOAD_CEILING - _PAYLOAD_RESERVE
    dropped = 0
    candidates = reversed(rows) if newest_first else iter(rows)
    for row in candidates:
        remaining = len(rows) - dropped
        if size <= limit or remaining <= keep:
            break
        try:
            size -= len(json.dumps(row, ensure_ascii=False, default=str)) + (2 if remaining > 1 else 0)
        except (TypeError, ValueError):
            pass
        dropped += 1
    # Delete once: repeated pop(0) would shift the remaining rows quadratically.
    if dropped:
        if newest_first:
            del rows[-dropped:]
        else:
            del rows[:dropped]
    return size, dropped


def _bound_summary(truncated_fields: int, *dropped: tuple[str, int]) -> str:
    parts = [f"{count} {name} dropped" for name, count in dropped if count]
    if truncated_fields:
        parts.append(f"{truncated_fields} fields truncated")
    return ", ".join(parts)


def _short(value: object) -> str:
    """A bounded single-line rendering of a scalar for the label line."""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text[:64]

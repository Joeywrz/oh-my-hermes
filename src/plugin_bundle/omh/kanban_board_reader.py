"""Read-only projection of Hermes Kanban board lanes for the OMH HUD.

Hermes keeps its board in ``kanban.db`` at the Hermes ROOT (shared across
profiles by design: ``<root>/kanban.db`` for the default board,
``<root>/kanban/boards/<slug>/kanban.db`` for a named one, ``HERMES_KANBAN_HOME``
overriding the root, and the session's current board resolved the way the
native tools resolve it: ``HERMES_KANBAN_BOARD``, then ``<root>/kanban/current``,
then ``default``). Workers are detached processes the gateway dispatcher
spawns; the board records no token usage and no served-model observation, so
a lane row here carries the native status, the assignee and the per-task
overrides -- nothing the board does not itself state.

This reader opens the database read-only, never writes it, never probes the
gateway and never calls ``hermes kanban``. Every failure is fail-open: a
missing board, a foreign schema or a lock lost to the dispatcher all project
as "no rows", never as an exception in the widget's poll.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .hermes_delegation import COMPLETED_LINGER_SECONDS, _conversation_session_ids, _iso_utc

LANE_BACKEND = "kanban"
DEFAULT_BOARD = "default"
# A worker that stopped heartbeating for this long is projected as stale: its
# pid is still recorded, but nothing says it is doing anything.
STALE_HEARTBEAT_SECONDS = 15 * 60
# A ready task nobody has claimed after this long, with no worker or claim
# anywhere on the selection, is the observable shape of "no dispatcher".
DISPATCHER_WAIT_SECONDS = 120
OPEN_STATUSES = ("todo", "scheduled", "ready", "running", "blocked", "review")
FINISHED_STATUSES = ("done", "archived")
REQUIRED_COLUMNS = frozenset(
    {
        "id",
        "title",
        "assignee",
        "status",
        "created_at",
        "started_at",
        "completed_at",
        "claim_lock",
        "worker_pid",
        "last_failure_error",
        "last_heartbeat_at",
        "skills",
        "model_override",
        "provider_override",
        "reasoning_effort",
        "session_id",
    }
)
_ROW_LIMIT = 8
_SELECT_LIMIT = 64
_ACTION_LIMIT = 96
_BOARD_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _text(value: Any, limit: int = 80) -> str:
    return str(value or "").strip()[:limit]


def _epoch(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def hermes_root(hermes_home: str | Path) -> Path:
    """The Hermes root: ``HERMES_HOME`` with a trailing ``profiles/<name>`` stripped.

    Same rule as Hermes' own ``get_default_hermes_root``: a profile home lives at
    ``<root>/profiles/<name>`` and the board is the root's, shared by every
    profile, so a profile-scoped reader still finds the one board.
    """
    home = Path(hermes_home).expanduser()
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def _board_root(hermes_home: str | Path) -> Path:
    """The directory the board files hang off: ``HERMES_KANBAN_HOME`` or the root."""
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    return Path(override).expanduser() if override else hermes_root(hermes_home)


def _board_exists(root: Path, slug: str) -> bool:
    """Hermes' ``board_exists``: ``default`` always; a named board when its
    directory holds ``board.json`` or ``kanban.db`` and nothing is a symlink."""
    if slug == DEFAULT_BOARD:
        return True
    directory = root / "kanban" / "boards" / slug
    if directory.is_symlink():
        return False
    return any(
        (directory / name).exists() and not (directory / name).is_symlink()
        for name in ("board.json", "kanban.db")
    )


def _existing_slug(root: Path, candidate: str) -> str:
    """``candidate`` lowercased when Hermes would accept it and that board
    exists, else ``""`` -- a malformed or stale slug falls through, the way
    ``get_current_board`` falls through, never to an exception."""
    slug = candidate.strip().lower()
    if not slug or not _BOARD_SLUG_RE.fullmatch(slug):
        return ""
    return slug if _board_exists(root, slug) else ""


def current_board(hermes_home: str | Path) -> str:
    """The board the session's ``kanban_*`` tools act on when none is named.

    Mirrors Hermes' ``get_current_board`` resolution: ``HERMES_KANBAN_BOARD``
    (validated, existing) wins, then the one-line slug in
    ``<root>/kanban/current`` written by ``hermes kanban boards switch`` (only
    while that board exists), else ``default``. Read-only; a symlinked
    ``current`` file is treated as absent, like every other linked board file.
    """
    root = _board_root(hermes_home)
    found = _existing_slug(root, os.environ.get("HERMES_KANBAN_BOARD", ""))
    if found:
        return found
    marker = root / "kanban" / "current"
    if marker.is_symlink() or not marker.is_file():
        return DEFAULT_BOARD
    try:
        return _existing_slug(root, marker.read_text(encoding="utf-8")) or DEFAULT_BOARD
    except (OSError, UnicodeDecodeError):
        return DEFAULT_BOARD


def kanban_db_path(hermes_home: str | Path, board: str = "") -> Path | None:
    """Where the board's ``kanban.db`` lives, or ``None`` when it cannot be trusted.

    An empty ``board`` means the session's current board (``current_board``),
    so a reader with no board argument follows the same switch the native
    tools follow. ``None`` for a board slug Hermes would refuse and for a
    database path (or its directory) that is a symlink -- the same refusal the
    other readers apply to the state they open, since a link under
    ``~/.hermes`` could point the read-only connection at a file the operator
    never meant to expose.
    """
    root = _board_root(hermes_home)
    slug = board.strip() or current_board(hermes_home)
    if slug == DEFAULT_BOARD:
        path = root / "kanban.db"
    elif _BOARD_SLUG_RE.fullmatch(slug):
        path = root / "kanban" / "boards" / slug / "kanban.db"
    else:
        return None
    if path.is_symlink() or path.parent.is_symlink():
        return None
    return path


def conversation_session_ids(hermes_home: str | Path, session_ref: str) -> set[str]:
    """The conversation identities the native delegate reader scopes by.

    The board stamps a task with the originating ``HERMES_SESSION_ID``, which is
    the same durable id ``state.db`` names, so the HUD can own board rows with
    exactly the identity set it already owns delegate_task children with.
    """
    if not session_ref:
        return set()
    state_db = Path(hermes_home).expanduser() / "state.db"
    if not state_db.is_file():
        return set()
    try:
        connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=0.25)
    except sqlite3.Error:
        return set()
    try:
        return _conversation_session_ids(connection, session_ref)
    except sqlite3.Error:
        return set()
    finally:
        try:
            connection.close()
        except sqlite3.Error:
            pass


def _tasks_columns(connection: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in connection.execute('PRAGMA table_info("tasks")')}


def _query_tasks(db: Path, *, now: float) -> list[dict[str, Any]]:
    """One bounded SELECT over the open lanes plus the lingering finished ones."""
    try:
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.25)
    except sqlite3.Error:
        return []
    try:
        if not REQUIRED_COLUMNS <= _tasks_columns(connection):
            return []
        open_marks = ",".join("?" for _ in OPEN_STATUSES)
        finished_marks = ",".join("?" for _ in FINISHED_STATUSES)
        cursor = connection.execute(
            "SELECT t.id, t.title, t.assignee, t.status, t.created_at, t.started_at, "
            "t.completed_at, t.claim_lock, t.worker_pid, t.last_failure_error, "
            "t.last_heartbeat_at, t.skills, t.model_override, t.provider_override, "
            "t.reasoning_effort, t.session_id, COALESCE(links.parent_count, 0) "
            "FROM tasks t LEFT JOIN ("
            "SELECT child_id, COUNT(*) AS parent_count FROM task_links GROUP BY child_id"
            ") links ON links.child_id = t.id "
            f"WHERE t.status IN ({open_marks}) "
            f"OR (t.status IN ({finished_marks}) AND t.completed_at >= ?) "
            "ORDER BY CASE WHEN t.status = 'running' THEN 0 ELSE 1 END, t.created_at ASC "
            "LIMIT ?",
            (*OPEN_STATUSES, *FINISHED_STATUSES, now - COMPLETED_LINGER_SECONDS, _SELECT_LIMIT),
        )
        rows = cursor.fetchall()
    except sqlite3.Error:
        # A schema Hermes has since changed, a lock the dispatcher holds: the
        # board projects as empty for this poll rather than failing the HUD.
        return []
    finally:
        try:
            connection.close()
        except sqlite3.Error:
            pass
    keys = (
        "id", "title", "assignee", "status", "created_at", "started_at", "completed_at",
        "claim_lock", "worker_pid", "last_failure_error", "last_heartbeat_at", "skills",
        "model_override", "provider_override", "reasoning_effort", "session_id",
        "parent_count",
    )
    return [dict(zip(keys, row, strict=True)) for row in rows]


def _first_skill(value: Any) -> str:
    """The first entry of the JSON skills list Hermes stores, else nothing."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = json.loads(value)
    except ValueError:
        return ""
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, str) and item.strip():
                return _text(item, limit=40)
    return ""


def _short_task_id(value: Any) -> str:
    """Hermes ids are ``t_`` + 8 hex; the HUD's id column is eight cells wide."""
    identity = _text(value, limit=40)
    return identity[2:] if identity.startswith("t_") else identity[:8]


def _lane_state(task: dict[str, Any], *, now: float) -> str:
    """The HUD verdict for a native status.

    Only native ``blocked`` is blocked. A recorded ``last_failure_error`` on a
    ``ready`` / ``review`` / ``todo`` row is a retry the dispatcher requeued
    (Hermes clears the error only on a later success), so that row is queued
    and ``retry_pending`` on the row carries the failure fact.
    """
    status = _text(task["status"], limit=20)
    if status in FINISHED_STATUSES:
        return "done"
    if status == "blocked":
        return "blocked"
    heartbeat = _epoch(task["last_heartbeat_at"])
    if (
        status == "running"
        and _epoch(task["worker_pid"]) is not None
        and heartbeat is not None
        and now - heartbeat > STALE_HEARTBEAT_SECONDS
    ):
        return "stale"
    if status == "running":
        return "running"
    return "queued"


def _lane_row(task: dict[str, Any], *, now: float, scope: str, board: str) -> dict[str, Any]:
    state = _lane_state(task, now=now)
    started = _epoch(task["started_at"])
    completed = _epoch(task["completed_at"])
    heartbeat = _epoch(task["last_heartbeat_at"])
    created = _epoch(task["created_at"]) or 0.0
    # A finished row's elapsed is frozen at completion, a settled one at its
    # last heartbeat: only a running lane keeps aging, so a lingering row is
    # byte-stable and the widget can skip its repaint.
    if state == "running":
        frozen_at = now
    elif completed is not None:
        frozen_at = completed
    elif heartbeat is not None:
        frozen_at = heartbeat
    else:
        frozen_at = started
    elapsed = max(0.0, frozen_at - started) if started is not None and frozen_at is not None else 0.0
    observed = max(value for value in (created, started, heartbeat, completed) if value is not None)
    model = _text(task["model_override"])
    return {
        "scope": scope,
        "state": state,
        "task_id": _short_task_id(task["id"]),
        "role": _first_skill(task["skills"]) or "worker",
        "action": _text(task["title"], limit=_ACTION_LIMIT),
        "alias": "",
        "provider": _text(task["provider_override"]),
        "model": model,
        "effort": _text(task["reasoning_effort"], limit=20),
        # The board records no usage: None is "not observed", never a zero.
        "tokens": None,
        "elapsed_seconds": elapsed,
        "observed_at": _iso_utc(observed),
        "category": "",
        "completed": state == "done",
        "delegation_id": "",
        # Nothing on the board observes which model answered a worker's calls.
        "model_attestation": {"verdict": "unknown", "basis": "no_usage_observation"},
        "lane_backend": LANE_BACKEND,
        "assignee": _text(task["assignee"], limit=40),
        "board": board,
        "native_status": _text(task["status"], limit=20),
        "parent_count": int(task["parent_count"] or 0),
        # The dispatcher requeued this row after a failed run; it is waiting
        # for the next tick, not blocked.
        "retry_pending": state == "queued" and bool(_text(task["last_failure_error"])),
    }


def _dispatcher_presence(tasks: list[dict[str, Any]], *, now: float) -> str:
    if any(_epoch(task["worker_pid"]) is not None or _text(task["claim_lock"]) for task in tasks):
        return "observed"
    waiting = any(
        _text(task["status"], limit=20) == "ready"
        and (_epoch(task["created_at"]) or now) <= now - DISPATCHER_WAIT_SECONDS
        for task in tasks
    )
    return "not_observed" if waiting else "unknown"


def read_kanban_lanes(
    hermes_home: str | Path,
    *,
    now: float | None = None,
    session_ids: set[str] | frozenset[str] | None = None,
    board: str = "",
    limit: int = _ROW_LIMIT,
) -> dict[str, Any]:
    """Project the board's open lanes, owned by the conversation when it can be.

    Rows whose ``session_id`` is one of ``session_ids`` are scope ``session``;
    when identities were given and none match, the whole board projects as
    explicitly ``global`` -- the same fallback the native delegate reader makes
    for an unmapped conversation, never a borrowed session label.
    """
    current = float(now) if now is not None else time.time()
    board_name = board.strip() or current_board(hermes_home)
    payload: dict[str, Any] = {
        "rows": [],
        "hidden": 0,
        "active": 0,
        "running": 0,
        "blocked": 0,
        "completed": 0,
        "queued": 0,
        "scope": "none",
        "dispatcher_presence": "unknown",
        "board": board_name,
    }
    db = kanban_db_path(hermes_home, board_name)
    if db is None or not db.is_file():
        return payload
    tasks = _query_tasks(db, now=current)
    if not tasks:
        return payload
    owners = set(session_ids or ())
    owned = [task for task in tasks if _text(task["session_id"], limit=160) in owners] if owners else []
    scope = "session" if owned else "global"
    selected = owned or tasks
    payload["scope"] = scope
    payload["dispatcher_presence"] = _dispatcher_presence(selected, now=current)
    rows = [_lane_row(task, now=current, scope=scope, board=board_name) for task in selected]
    for row in rows:
        if row["state"] in {"running", "stale"}:
            payload["running"] += 1
        elif row["state"] == "blocked":
            payload["blocked"] += 1
        elif row["state"] == "done":
            payload["completed"] += 1
        else:
            payload["queued"] += 1
    bound = max(1, int(limit))
    payload["hidden"] = max(0, len(rows) - bound)
    payload["rows"] = rows[:bound]
    payload["active"] = payload["running"] + payload["blocked"]
    return payload

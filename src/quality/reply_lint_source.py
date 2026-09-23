"""Read replies out of a Hermes session for the reply lint, read-only.

Hermes keeps every turn in ``state.db`` (``messages``: ``session_id``,
``role``, ``content``, ordered by ``id``). This reader pairs each assistant
reply with the user message it answered, so the lint's carve-out sees what
the person asked, and skips the rows Hermes re-injects after a compaction
(``[PRIOR CONTEXT ...]``), which a person never read as a reply. The
database is opened ``mode=ro``; nothing is written.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import quote


HERMES_LATEST_SESSION = "latest"
_PRIOR_CONTEXT_PREFIX = "[PRIOR CONTEXT"


class ReplySourceError(ValueError):
    """The session or database could not be read; the message says which."""


def hermes_session_replies(
    hermes_home: str | Path,
    session_id: str,
    *,
    last: int = 1,
) -> dict[str, Any]:
    """Return ``{"session_id", "replies": [{"user_text", "reply", "message_id"}]}``.

    ``session_id`` may be ``latest``, which resolves to the most recently
    active session row. ``last`` bounds how many trailing assistant replies
    are returned, newest last.
    """
    path = Path(hermes_home).expanduser() / "state.db"
    if not path.exists():
        raise ReplySourceError(f"no Hermes state database at {path}")
    if last < 1:
        raise ReplySourceError("--last must be at least 1")
    try:
        connection = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error as exc:
        raise ReplySourceError(f"could not open {path}: {exc}") from exc
    try:
        resolved = _resolve_session(connection, session_id)
        rows = connection.execute(
            "SELECT id, role, content FROM messages WHERE session_id = ? AND role IN ('user', 'assistant') ORDER BY id",
            (resolved,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ReplySourceError(f"could not read {path}: {exc}") from exc
    finally:
        connection.close()
    if not rows:
        raise ReplySourceError(f"session {resolved} has no user or assistant messages")
    replies: list[dict[str, Any]] = []
    user_text = ""
    for message_id, role, content in rows:
        text = str(content or "")
        if role == "user":
            user_text = text
            continue
        if text.startswith(_PRIOR_CONTEXT_PREFIX) or not text.strip():
            continue
        if replies and replies[-1]["reply"] == text:
            # A compaction can re-record the same reply under a new id.
            continue
        replies.append({"message_id": int(message_id), "user_text": user_text, "reply": text})
    return {"session_id": resolved, "replies": replies[-last:]}


def _resolve_session(connection: sqlite3.Connection, session_id: str) -> str:
    wanted = str(session_id or "").strip()
    if not wanted:
        raise ReplySourceError("a session id (or `latest`) is required")
    if wanted != HERMES_LATEST_SESSION:
        return wanted
    row = connection.execute(
        "SELECT id FROM sessions ORDER BY COALESCE(last_activity_at, started_at) DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise ReplySourceError("the Hermes state database has no sessions")
    return str(row[0])

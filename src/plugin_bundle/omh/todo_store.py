"""Shared store for the HUD todo artifact.

One todo list per declaring session. A record that knows the host session
that declared it (``session_ref``) lives at
``$OMH_HOME/runtime/todos/<session key>.json``; a record written without one
-- `omh runtime todo set` with no ``--session``, or anything predating the
field -- keeps the home-wide ``$OMH_HOME/runtime/todo.json``. The CLI and the
`omh_todo` plugin tool both write through this module so the schema has a
single source of truth; `runtime_reader` projects the records into the HUD
payload read-only, choosing the file that belongs to the reading session.

The per-session layout is what keeps unrelated sessions apart: a plan
declared from a Slack or Discord gateway session, or from a second live TUI,
is its own file, so it neither overwrites nor renders inside another
session's checklist. Writers sharing one home no longer race for one file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TODO_SCHEMA_VERSION = "omh_todo/v1"
TODO_FILENAME = "todo.json"
# Per-session records live one directory below the home-wide file, keyed by
# the declaring session. The directory is bounded on every write: records
# past the reader's own stale bound are removed, and so are temporary files a
# crashed write left behind. Only files this module wrote are candidates --
# a record's name has the key shape below -- so nothing else placed in the
# directory is ever touched, and a fresh record is never evicted to make
# room: one session declares one record, and the stale window is the bound.
TODO_SESSION_DIRNAME = "todos"
TODO_STALE_SECONDS = 86400
_SESSION_RECORD_NAME = re.compile(r"(?:[A-Za-z0-9_-]{1,48}-)?[0-9a-f]{16}\.json")
_TEMPORARY_NAME = re.compile(r"\..*\.tmp")
# The largest record this module reads back itself (clear's stamp check);
# the HUD reader applies the same cap to every metadata file.
MAX_TODO_RECORD_BYTES = 262_144
TODO_ITEM_STATES = ("pending", "active", "done")
MAX_TODO_ITEMS = 20
MAX_TODO_TEXT_CHARS = 200
MAX_TODO_TITLE_CHARS = 80
MAX_TODO_SOURCE_CHARS = 80
# Optional owning-session id, stamped when the writer knows which host session
# declared the plan. Bounded to the host-observation session limit because it
# is the same identifier. A record without it is a legacy or CLI write, and
# readers scope it by write time instead of by identity.
MAX_TODO_SESSION_REF_CHARS = 160
# Optional phase label per item ("Internal Context", "Delivery", ...). A
# phase-structured plan declared BEFORE engine work bounds the run: progress
# is a checklist walked phase by phase, not an open-ended reasoning loop.
MAX_TODO_PHASE_CHARS = 60
# Optional reason an item cannot proceed. It is a FIELD rather than a fourth
# item state so the counts, the HUD projection and the widget keep reading
# three states; an item is still pending or active while it carries one.
#
# It exists because the plan's own stop criterion ("an item is recorded
# blocked with its reason") was previously inferred from the item text, and
# inference was wrong in both directions on ordinary input: "verify the retry
# is not blocked on the session limit" read as blocked, while "차단됨: 소유자
# 승인 대기" and "waiting on the owner's review" did not. A reader that
# decides whether work stops must read a record, not a substring.
MAX_TODO_BLOCKED_REASON_CHARS = 200
# Optional nesting depth per item: 0 is a top-level task, 1..3 are subtask
# levels rendered indented beneath it (e.g. "검증작업하기" with usability /
# UI / load-verification children). Three levels is the owner's declared
# ceiling; deeper nesting stops reading as a checklist.
MAX_TODO_DEPTH = 3
TODO_CLAIM_BOUNDARY = (
    "Todo items are plan declarations. They are not execution, verification, "
    "review, CI, merge-readiness, or merge evidence."
)


class TodoValidationError(ValueError):
    """The supplied todo payload does not satisfy the omh_todo/v1 contract."""


class TodoStoreError(RuntimeError):
    """The todo destination under the OMH home is unsafe to write."""


# C0/C1 control characters (ESC, BEL, CR, LF included) are stripped on write
# and again on read so neither the artifact at rest nor the HUD projection can
# carry terminal escapes or forge extra checklist lines.
_CONTROL_CHARACTERS = {code: None for code in (*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0))}


def strip_control_characters(value: object) -> str:
    return str(value or "").translate(_CONTROL_CHARACTERS).strip()


def validate_todo_items(items: object) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise TodoValidationError("todo items must be a non-empty list")
    if len(items) > MAX_TODO_ITEMS:
        raise TodoValidationError(f"todo items are capped at {MAX_TODO_ITEMS}")
    validated: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise TodoValidationError("each todo item must be an object")
        text = strip_control_characters(item.get("text", ""))
        if not text:
            raise TodoValidationError("each todo item needs non-empty text")
        if len(text) > MAX_TODO_TEXT_CHARS:
            raise TodoValidationError(f"todo item text is capped at {MAX_TODO_TEXT_CHARS} characters")
        state = str(item.get("state", "pending"))
        if state not in TODO_ITEM_STATES:
            raise TodoValidationError(f"todo item state must be one of {', '.join(TODO_ITEM_STATES)}")
        phase = strip_control_characters(item.get("phase", ""))
        if len(phase) > MAX_TODO_PHASE_CHARS:
            raise TodoValidationError(f"todo item phase is capped at {MAX_TODO_PHASE_CHARS} characters")
        blocked_reason = strip_control_characters(item.get("blocked_reason", ""))
        if len(blocked_reason) > MAX_TODO_BLOCKED_REASON_CHARS:
            raise TodoValidationError(
                f"todo item blocked_reason is capped at {MAX_TODO_BLOCKED_REASON_CHARS} characters"
            )
        depth = item.get("depth", 0)
        if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= MAX_TODO_DEPTH:
            raise TodoValidationError(f"todo item depth must be an integer from 0 to {MAX_TODO_DEPTH}")
        entry: dict[str, Any] = {"text": text, "state": state}
        if phase:
            entry["phase"] = phase
        if depth:
            entry["depth"] = depth
        if blocked_reason:
            entry["blocked_reason"] = blocked_reason
        validated.append(entry)
    return validated


def build_todo_record(
    title: object, items: object, *, source: str, session_ref: object = ""
) -> dict[str, Any]:
    """Build the on-disk todo record.

    ``session_ref`` names the host session that declared this plan, when the
    writer knows it. It is additive-optional inside ``omh_todo/v1``: the key is
    written only when non-empty, so a CLI write is byte-identical to what it
    was before the field existed, and a reader that predates it still reads
    every field it knew.
    """
    safe_title = strip_control_characters(title)
    if len(safe_title) > MAX_TODO_TITLE_CHARS:
        raise TodoValidationError(f"todo title is capped at {MAX_TODO_TITLE_CHARS} characters")
    safe_source = strip_control_characters(source)[:MAX_TODO_SOURCE_CHARS]
    safe_session_ref = strip_control_characters(session_ref)[:MAX_TODO_SESSION_REF_CHARS]
    record: dict[str, Any] = {
        "schema_version": TODO_SCHEMA_VERSION,
        "title": safe_title,
        "source": safe_source,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "items": validate_todo_items(items),
        "claim_boundary": TODO_CLAIM_BOUNDARY,
    }
    if safe_session_ref:
        record["session_ref"] = safe_session_ref
    return record


def todo_session_key(session_ref: object) -> str:
    """The filename stem a session's todo record lives under.

    Host session ids are filesystem-safe today (``20260831_153632_11fc69``),
    but a gateway thread id may carry any character, so the key is a bounded
    sanitized slug for legibility plus a short digest of the exact reference
    for uniqueness. The reference is bounded to the host-observation session
    limit before either is taken, the same bound the record's stamp has, so
    the key and the stamp always describe the same string. Empty when the
    reference is empty.
    """
    reference = strip_control_characters(session_ref)[:MAX_TODO_SESSION_REF_CHARS]
    if not reference:
        return ""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", reference).strip("_-")[:48]
    digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()[:16]
    return f"{slug}-{digest}" if slug else digest


def todo_path(omh_home: Path, session_ref: object = "") -> Path:
    """Where the todo record for ``session_ref`` lives; the home-wide file when empty."""
    key = todo_session_key(session_ref)
    if not key:
        return omh_home / "runtime" / TODO_FILENAME
    return omh_home / "runtime" / TODO_SESSION_DIRNAME / f"{key}.json"


def todo_session_dir(omh_home: Path) -> Path:
    return omh_home / "runtime" / TODO_SESSION_DIRNAME


def write_todo(omh_home: Path, record: dict[str, Any]) -> Path:
    """Write ``record`` to the file its ``session_ref`` selects."""
    session_ref = str(record.get("session_ref", "") or "")
    destination = todo_path(omh_home, session_ref)
    _reject_symlink_ancestry(destination, root=omh_home)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}-{secrets.token_hex(8)}.tmp")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Post-mkdir TOCTOU recheck of the whole ancestry: the walk above ran
        # before the session directory existed, so a link planted in between
        # would otherwise be followed by the write.
        _reject_symlink_ancestry(destination, root=omh_home)
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        os.replace(temporary, destination)
    except OSError as error:
        raise TodoStoreError(f"todo destination is not writable: {error}") from error
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
    if session_ref:
        _prune_session_records(omh_home, keep=destination)
    return destination


def clear_todo(omh_home: Path, session_ref: object = "") -> bool:
    """Remove the todo record ``session_ref`` selects.

    A session clearing its plan also removes the home-wide record when that
    record is one the session renders: unstamped (an operator's
    `omh runtime todo set`, which the reader shows to the live session), or
    stamped by this very session (the layout that predates per-session
    files). It never touches another session's stamped record, so a clear
    always answers for what the caller was looking at and nothing else.
    """
    reference = strip_control_characters(session_ref)[:MAX_TODO_SESSION_REF_CHARS]
    removed = _remove_todo_file(omh_home, todo_path(omh_home, reference))
    if reference:
        legacy = todo_path(omh_home)
        if _stamped_session_ref(legacy) in {"", reference}:
            removed = _remove_todo_file(omh_home, legacy) or removed
    return removed


def _remove_todo_file(omh_home: Path, destination: Path) -> bool:
    _reject_symlink_ancestry(destination, root=omh_home)
    if not destination.is_file() or destination.is_symlink():
        return False
    try:
        destination.unlink()
    except OSError as error:
        raise TodoStoreError(f"todo destination is not removable: {error}") from error
    return True


def _stamped_session_ref(path: Path) -> str:
    """The ``session_ref`` a record on disk carries, read without following links."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return ""
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_TODO_RECORD_BYTES:
            return ""
        record = json.loads(os.read(descriptor, MAX_TODO_RECORD_BYTES).decode("utf-8"))
    except (OSError, ValueError):
        return ""
    finally:
        os.close(descriptor)
    if not isinstance(record, dict):
        return ""
    return strip_control_characters(record.get("session_ref", ""))[:MAX_TODO_SESSION_REF_CHARS]


def _prune_session_records(omh_home: Path, *, keep: Path) -> None:
    """Drop per-session records the reader would already treat as stale.

    Best effort: a prune failure never fails the write that triggered it.
    Only regular files this module names -- session records and its own
    temporary files -- directly inside the session directory are considered,
    only once they are older than the stale bound, and the record just
    written is always kept.
    """
    directory = todo_session_dir(omh_home)
    now = datetime.now(timezone.utc).timestamp()
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        if entry.name == keep.name:
            continue
        if not (_SESSION_RECORD_NAME.fullmatch(entry.name) or _TEMPORARY_NAME.fullmatch(entry.name)):
            continue
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            if now - entry.stat(follow_symlinks=False).st_mtime <= TODO_STALE_SECONDS:
                continue
            os.unlink(entry.path)
        except OSError:
            continue


def _reject_symlink_ancestry(path: Path, *, root: Path) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise TodoStoreError(f"refusing symlinked todo path: {current}")
        if current == root or current == current.parent:
            return
        current = current.parent

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

import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# The bundle's one sanctioned lock, the same object `tool_bursts`,
# `approval_bypass` and `memory_open_reminders` take. It carries both backends
# because this directory is vendored into the user's Hermes install and may not
# import omh core; a copy here would be the third, and the policy gate in
# `tests/test_journal_lock_portability.py` exists to stop exactly that.
from .awareness_delivery import _awareness_delivery_lock
from .todo_templates import TODO_TEMPLATES, template_coverage_error, template_items

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
# The lock file beside a record, named the same way so the prune below can
# recognise its own. It is only ever a rendezvous point: nothing is read out
# of it and nothing is written into it.
_LOCK_NAME = re.compile(r"\..*\.json\.lock")
# How long a writer waits for this record before refusing. The shared lock's
# own default is 0.1s, sized for telemetry that would rather drop a counter
# than delay a turn; a plan write is the opposite trade, so this passes its
# own. A write here is a few hundred microseconds of work, so a wait measured
# in whole seconds means a holder is stuck rather than busy -- long enough
# that a contended turn waits instead of failing, short enough that a stuck
# holder becomes a refusal the caller can act on.
_LOCK_TIMEOUT_SECONDS = 2.0
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
# Optional PLAN-LEVEL reason the person steered the session elsewhere. It is
# not a second `blocked_reason`, and what makes it a different field is the
# digest stored beside it rather than the wording: blocked is "this item
# cannot proceed" and is cleared by hand, this is "the person asked for
# something else first" and LAPSES ON ITS OWN.
#
# The reason is written together with a digest of the item list as it stood at
# the moment of deferral, and a reader honours the deferral only while that
# digest still matches the items it is reading. Marking an item done, moving
# which one is active, or re-scoping the list all change the digest, so a
# deferral cannot outlive the plan it was written against. That is the whole
# reason this is a field and not a hand-cleared flag: a flag someone must
# remember to clear is the same forgetting this surface exists to prevent,
# moved one step later. The state is derived from the items rather than
# asserted beside them, so it cannot drift from what the plan is.
#
# One consequence, stated rather than left to be discovered. A writer that
# sends the reason AGAIN alongside a changed item list gets a digest over the
# new list, so that is a new deferral for that list and not the old one
# surviving. Deliberate: the record is the declaration and the writer owns it,
# exactly as with `blocked_reason`. Every writer that does not re-send the
# field -- the CLI, which has none; a hand edit; a generation predating the
# field; and the tool, whose description says to omit it -- lapses the
# deferral by default, which is what makes resuming cost nobody a clearing
# step.
MAX_TODO_DEFERRED_REASON_CHARS = 200
# Optional name of the phase template this plan was stamped with
# (`todo_templates`). A closed vocabulary, not free text: the bound is what a
# reader allocates for it, and the validator below rejects anything that is
# not a known template name outright. Additive-optional like every field
# above it -- a record written without one is byte-identical to what this
# module wrote before the field existed.
MAX_TODO_TEMPLATE_CHARS = 40
# The digest is only ever compared for equality, never inverted, so the bound
# is about how much record a deferral costs, not about collision resistance;
# 128 bits is far past what "is this the same item list" needs.
TODO_DEFERRED_DIGEST_CHARS = 32
# The item fields the digest covers: every field an item declares. Any edit to
# any of them is the plan moving, `blocked_reason` included -- writing down
# that an item is stuck is a plan advancing, not a plan standing still.
_DIGESTED_ITEM_KEYS = ("text", "state", "phase", "depth", "blocked_reason")
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


class TodoContendedError(TodoStoreError):
    """Another writer held this record's lock for longer than the wait allows.

    A subclass, so every `except TodoStoreError` written before the lock
    existed keeps catching it and nothing has to learn a new failure to stay
    correct. It exists as its own type for the one caller that must tell the
    two apart: a refusal saying the payload was invalid tells a writer to
    change its arguments, and changing the arguments is exactly the wrong
    response to a lock that will be free in milliseconds. The right response
    is the same call again.
    """


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


def todo_items_digest(items: object) -> str:
    """A digest of an item list, over every field an item declares.

    The same function answers for a writer's validated items and for the
    reader's projection of them, and the two are byte-equal for any record
    this module wrote: both build each entry from the same fields, in the same
    order, under the same bounds (the writer rejects an over-length field, the
    reader truncates at the identical cap). So a deferral written here is
    recognised by the reader until an item actually changes.

    Never raises, because every caller sits under a host that swallows
    exceptions. A malformed item list yields a digest that simply will not
    match the recorded one, and a deferral that fails to match lapses -- which
    is the safe direction: corruption makes the plan keep going, never stop.
    """
    if not isinstance(items, list):
        return ""
    canonical = [
        {key: entry[key] for key in _DIGESTED_ITEM_KEYS if key in entry}
        for entry in items
        if isinstance(entry, dict)
    ]
    # `default=str` is the last guard rather than a convenience: a hand-written
    # record can carry a value json cannot serialize, and raising here would
    # end the turn silently.
    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:TODO_DEFERRED_DIGEST_CHARS]


def build_todo_record(
    title: object,
    items: object,
    *,
    source: str,
    session_ref: object = "",
    deferred_reason: object = "",
    template: object = "",
) -> dict[str, Any]:
    """Build the on-disk todo record.

    ``session_ref`` names the host session that declared this plan, when the
    writer knows it. It is additive-optional inside ``omh_todo/v1``: the key is
    written only when non-empty, so a CLI write is byte-identical to what it
    was before the field existed, and a reader that predates it still reads
    every field it knew.

    ``deferred_reason`` is additive-optional on the same terms, and carries a
    second key with it: ``deferred_items_digest``, the digest of the items this
    record is being written with. The two are always written together and
    never separately -- a reason without a digest would be a deferral nothing
    can lapse, which is the hand-cleared flag this field exists instead of.

    ``template`` names a phase template from `todo_templates` and is
    additive-optional on the same terms again. It does two things and they are
    the same thing seen from either end of the write: with no ``items`` it
    FILLS them, one pending item per phase in delivery order, so the shape of
    the plan comes from the template rather than from whatever the writer
    invents; with ``items`` it HOLDS them to that shape, refusing a list that
    drops a phase, renames one, reorders them, or leaves an item outside every
    phase. A writer that omits the field gets exactly the record this function
    built before the field existed, items and all.

    The coverage check runs after ``validate_todo_items`` and not before,
    because it reads the phase each item ended up with -- the validator is
    where a blank phase becomes an absent one, and checking coverage against
    the raw input would accept a whitespace phase the record does not carry.
    """
    safe_title = strip_control_characters(title)
    if len(safe_title) > MAX_TODO_TITLE_CHARS:
        raise TodoValidationError(f"todo title is capped at {MAX_TODO_TITLE_CHARS} characters")
    safe_source = strip_control_characters(source)[:MAX_TODO_SOURCE_CHARS]
    safe_session_ref = strip_control_characters(session_ref)[:MAX_TODO_SESSION_REF_CHARS]
    safe_deferred_reason = _validated_deferred_reason(deferred_reason)
    safe_template = _validated_template(template)
    if safe_template and items in (None, []):
        items = template_items(safe_template)
    validated_items = validate_todo_items(items)
    if safe_template and (error := template_coverage_error(safe_template, validated_items)):
        raise TodoValidationError(error)
    record: dict[str, Any] = {
        "schema_version": TODO_SCHEMA_VERSION,
        "title": safe_title,
        "source": safe_source,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "items": validated_items,
        "claim_boundary": TODO_CLAIM_BOUNDARY,
    }
    if safe_session_ref:
        record["session_ref"] = safe_session_ref
    if safe_deferred_reason:
        record["deferred_reason"] = safe_deferred_reason
        record["deferred_items_digest"] = todo_items_digest(validated_items)
    if safe_template:
        record["template"] = safe_template
    return record


def _validated_template(template: object) -> str:
    """The template name as it will be stored, or ``""``.

    A closed vocabulary, so an unrecognised name raises instead of being
    stored: a stamp nothing can project is worse than no stamp, because the
    coverage rule the stamp exists to impose would then be silently absent
    from a record that claims to have one. The message names the templates
    that do exist, since the writer is a model reading a refusal rather than
    a person reading this file.

    A non-string is rejected rather than coerced, the call
    ``_validated_deferred_reason`` above makes for the same reason: a number
    is not a template name in any language.
    """
    if template is None or template == "":
        return ""
    if not isinstance(template, str):
        raise TodoValidationError("todo template must be a string")
    safe = strip_control_characters(template)[:MAX_TODO_TEMPLATE_CHARS]
    if safe not in TODO_TEMPLATES:
        known = ", ".join(repr(name) for name in sorted(TODO_TEMPLATES))
        raise TodoValidationError(f"todo template must be one of: {known}")
    return safe


def _validated_deferred_reason(deferred_reason: object) -> str:
    """The plan-level deferral reason as it will be stored, or ``""``.

    A non-string is rejected rather than coerced. ``strip_control_characters``
    would turn ``7`` into the truthy string ``"7"``, and a number is not a
    declaration in any language -- the same call the item-level reason makes on
    the way back out. Whitespace is not a declaration either: it strips to
    empty and the record is written without the field, so a blank reason is
    absence rather than a deferral nobody can read.

    Over-length raises instead of truncating, matching ``blocked_reason``: the
    reasons are the same shape of free text, and a reason silently cut at its
    cap can read as something the writer did not say. The tool surfaces the
    error so the writer can shorten it.
    """
    if deferred_reason is None or deferred_reason == "":
        return ""
    if not isinstance(deferred_reason, str):
        raise TodoValidationError("todo deferred_reason must be a string")
    safe = strip_control_characters(deferred_reason)
    if len(safe) > MAX_TODO_DEFERRED_REASON_CHARS:
        raise TodoValidationError(
            f"todo deferred_reason is capped at {MAX_TODO_DEFERRED_REASON_CHARS} characters"
        )
    return safe


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
    """Write ``record`` to the file its ``session_ref`` selects.

    Taken under the record's lock, and that is not about this write on its
    own: ``os.replace`` already makes a whole-record write atomic against a
    reader. It is about ``advance_todo_item``, whose read-modify-write is not
    atomic against anything, and which can only be serialised against a
    whole-list write if both go through the same lock.
    """
    session_ref = str(record.get("session_ref", "") or "")
    destination = todo_path(omh_home, session_ref)
    _reject_symlink_ancestry(destination, root=omh_home)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise TodoStoreError(f"todo destination is not writable: {error}") from error
    # Post-mkdir TOCTOU recheck of the whole ancestry: the walk above ran
    # before the session directory existed, so a link planted in between
    # would otherwise be followed by the write.
    _reject_symlink_ancestry(destination, root=omh_home)
    with _todo_record_lock(destination, root=omh_home):
        _replace_todo_record(destination)(record)
    if session_ref:
        _prune_session_records(omh_home, keep=destination)
    return destination


def _replace_todo_record(destination: Path):
    """A writer bound to one destination, for use inside the record's lock.

    Returned as a closure rather than taking the path twice because both
    callers have already resolved and checked the destination, and a second
    path argument at the call site is a second chance for the lock and the
    write to describe different files.
    """

    def write(record: dict[str, Any]) -> None:
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}-{secrets.token_hex(8)}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
            os.replace(temporary, destination)
        except OSError as error:
            raise TodoStoreError(f"todo destination is not writable: {error}") from error
        finally:
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()

    return write


@contextlib.contextmanager
def _todo_record_lock(destination: Path, *, root: Path) -> Iterator[None]:
    """Serialize every write to one todo record, across processes.

    A lock rather than a compare-and-set on ``updated_at`` because the window
    a compare-and-set leaves open is the whole of the failure: the advance
    path reads the record, validates a list, rebuilds it and writes, and a
    stamp checked before that last step is checked in a different instant
    than the ``os.replace`` that acts on it. Under the lock the read and the
    write are one step, so a whole-list `set` landing in the middle is not
    possible rather than unlikely.

    The lock itself is `awareness_delivery`'s, not one of this module's own.
    That module is where the bundle keeps its two-backend implementation, for
    the reason recorded there -- vendored into the user's Hermes install, so
    it cannot share `local_store.file_lock` -- and three other modules here
    already take it. What this adds is the deadline and the vocabulary: a
    timeout is `TodoContendedError`, so the caller can tell a busy record
    from an invalid payload, and never a silent unlocked pass.

    A host with neither backend yields `none` from the shared helper and
    takes no lock. This does not refuse in that case, which is the behaviour
    every writer here had before the lock existed; refusing would make a
    platform without `fcntl` or `msvcrt` unable to keep a plan at all.
    """
    # Derived the same way the shared helper derives it, and checked before
    # the helper creates it. `_lock_file_for` is the single spelling of that
    # derivation, and `test_the_shared_lock_file_is_the_one_the_prune_knows`
    # pins it against the file the helper actually writes.
    _reject_symlink_ancestry(_lock_file_for(destination), root=root)
    # `held` is what keeps the two handlers below honest. They sit outside the
    # `with`, so they see the caller's body as well as the acquisition, and an
    # `OSError` from the body relabelled as "the destination is not writable"
    # would be this module deciding what someone else's failure was --
    # `TimeoutError` is an `OSError` too, so the pair is easy to get wrong.
    # Once the lock is held, anything raised is the caller's and leaves
    # unchanged.
    held = False
    try:
        with _awareness_delivery_lock(destination, timeout_seconds=_LOCK_TIMEOUT_SECONDS):
            held = True
            yield
    except TimeoutError as error:
        if held:
            raise
        raise TodoContendedError(
            f"todo record is held by another writer after "
            f"{_LOCK_TIMEOUT_SECONDS:g}s and was not written: {destination}. "
            "Nothing changed; send the same call again."
        ) from error
    except OSError as error:
        if held:
            raise
        raise TodoStoreError(f"todo destination is not writable: {error}") from error


def _lock_file_for(destination: Path) -> Path:
    """The lock file `_awareness_delivery_lock` will create beside ``destination``."""
    return destination.with_name(f".{destination.name}.lock")


def advance_todo_item(
    omh_home: Path,
    *,
    item: object,
    item_text: object,
    state: object,
    source: str,
    session_ref: object = "",
    blocked_reason: object = "",
    deferred_reason: object = "",
) -> dict[str, Any]:
    """Change ONE item's state on an existing record, and return the new record.

    The same write as a whole-list `set`, reached with one item's worth of
    arguments instead of the whole list. That equivalence is the contract and
    it is enforced structurally rather than by agreement: the new list is the
    stored list with one entry's ``state`` and ``blocked_reason`` replaced,
    and it then goes through ``build_todo_record`` -- the same title, source,
    session, deferral and template handling, the same ``validate_todo_items``,
    the same stamp. There is no second validator here and no second schema; a
    record this produces is byte-equal to the one `set` produces for the same
    plan.

    The template name is read off the stored record and sent back through, so
    a single-item write neither drops it nor escapes it: the phase coverage
    the template imposes is re-checked on the advanced list, exactly as it
    would be on a whole-list `set`. Advancing an item cannot move a
    phase, so this passes for any record this module wrote; a hand-edited
    record that no longer covers its template refuses here, naming the phase,
    and `set` is how it is re-declared.

    ``item`` is 1-based, the way the checklist reads, and it is guarded rather
    than trusted. ``item_text`` must be a prefix of the text already stored at
    that position, so a reference computed against a list that has since been
    re-set refuses instead of ticking whatever now sits at that index. The
    guard is required for that reason: an unguarded index is exactly the
    silent mis-write this action would otherwise introduce, and the whole
    point of a single-item write is that the caller no longer re-reads the
    list on every advance.

    ``blocked_reason`` replaces the item's recorded reason and omitting it
    clears one, which is what `set` does with a field left out of an item.
    Making it sticky instead would create a record state reachable by `set`
    and not by this, and the equivalence above is what the action is for.

    The whole read-modify-write runs inside the record's lock, so a `set` from
    another turn or another process cannot land between the read and the
    write and be overwritten by a list this call read before it.
    """
    destination = todo_path(omh_home, session_ref)
    _reject_symlink_ancestry(destination, root=omh_home)
    # Asked before the lock, because taking one means creating a file in a
    # directory that may not exist, and the OSError that produces would
    # report a home that is not writable about a home that is merely empty.
    # Nothing is lost to the gap: a record appearing here is one this call
    # never read, which is the same answer it would give a moment earlier.
    if not destination.parent.is_dir():
        raise TodoValidationError(
            "no todo record for this session; declare one with action=set"
        )
    with _todo_record_lock(destination, root=omh_home):
        record = _read_todo_record(destination)
        if record is None:
            raise TodoValidationError(
                "no todo record for this session; declare one with action=set"
            )
        stored = record.get("items")
        if not isinstance(stored, list) or not stored:
            raise TodoValidationError(
                "the stored todo record has no items; declare one with action=set"
            )
        if all(
            isinstance(entry, dict) and entry.get("state") == "done" for entry in stored
        ):
            raise TodoValidationError(
                "this plan is finished; declare a new one with action=set"
            )
        position = _validated_item_reference(item, len(stored))
        current = stored[position]
        if not isinstance(current, dict):
            raise TodoValidationError(f"todo item {position + 1} is not an object")
        _check_item_guard(item_text, current, position)
        if state not in TODO_ITEM_STATES:
            raise TodoValidationError(
                f"todo item state must be one of {', '.join(TODO_ITEM_STATES)}"
            )
        updated = dict(current)
        updated["state"] = state
        safe_reason = strip_control_characters(blocked_reason)
        if safe_reason:
            updated["blocked_reason"] = blocked_reason
        else:
            updated.pop("blocked_reason", None)
        items = list(stored)
        items[position] = updated
        advanced = build_todo_record(
            record.get("title", ""),
            items,
            source=source,
            session_ref=session_ref,
            deferred_reason=deferred_reason,
            template=record.get("template", ""),
        )
        _replace_todo_record(destination)(advanced)
    return advanced


def _validated_item_reference(item: object, count: int) -> int:
    """The 0-based position ``item`` names, or a refusal that names the field.

    ``bool`` is rejected before ``int`` because ``True`` is ``1`` and would
    otherwise tick the first item; the same call ``validate_todo_items``
    makes about ``depth``.
    """
    if isinstance(item, bool) or not isinstance(item, int):
        raise TodoValidationError(
            f"todo item must be an integer from 1 to {count}; the plan has {count} items"
        )
    if not 1 <= item <= count:
        raise TodoValidationError(
            f"todo item {item} is out of range; the plan has {count} items"
        )
    return item - 1


def _check_item_guard(item_text: object, current: dict[str, Any], position: int) -> None:
    """Refuse unless ``item_text`` still describes the item at ``position``.

    A compare-and-set, written as a text prefix because the record has no
    item id and does not need one for anything else: adding one would change
    the on-disk schema, the digest the deferral lapses on, and every surface
    that projects an item, to carry a handle whose only reader would be this
    function.
    """
    guard = strip_control_characters(item_text)
    if not guard:
        raise TodoValidationError(
            "todo item_text is required; it guards the item reference against a stale index"
        )
    stored_text = strip_control_characters(current.get("text", ""))
    if not stored_text.startswith(guard):
        raise TodoValidationError(
            f"todo item_text does not match item {position + 1} "
            f"({stored_text[:60]!r}); read the plan with action=show first"
        )


def _read_todo_record(path: Path) -> dict[str, Any] | None:
    """The record on disk, read without following links, or ``None``.

    The RAW record, not the HUD projection: the projection truncates for
    display and drops what a checklist row cannot show, so rebuilding a write
    from it would quietly rewrite the plan. The bounds are the ones
    ``_stamped_session_ref`` already applies to the same file.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_TODO_RECORD_BYTES:
            return None
        record = json.loads(os.read(descriptor, MAX_TODO_RECORD_BYTES).decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        os.close(descriptor)
    return record if isinstance(record, dict) else None


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
    Only regular files this module names -- session records, its own
    temporary files, and the lock files beside them -- directly inside the
    session directory are considered, only once they are older than the stale
    bound, and the record just written is always kept.

    A lock file has one extra condition, because removing one that is still
    coordinating writers would let two of them into the record at once: it
    goes only when the record it guards is already gone. Past the stale bound
    with no record beside it, nothing can be mid-write on it -- a writer
    creating a record holds a lock that is seconds old, not a day.
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
        if _LOCK_NAME.fullmatch(entry.name):
            if (directory / entry.name[1:-len(".lock")]).exists():
                continue
        elif not (
            _SESSION_RECORD_NAME.fullmatch(entry.name) or _TEMPORARY_NAME.fullmatch(entry.name)
        ):
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

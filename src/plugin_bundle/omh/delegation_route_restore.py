"""The way back from a prepared delegation route.

`delegation_routing.write_delegation_route` is the one-way half: it sets the
three routable keys Hermes re-reads per dispatch. Nothing put back what was
there before, so a route chosen for one lane stayed in `config.yaml` as every
later session's delegation default (#1724).

This module owns the way back. It records, beside the route write and under
the same lock:

* the **baseline** -- what the three keys held before OMH first wrote them. A
  key the file did not carry is recorded by being ABSENT from the mapping, so
  restoring it removes the key rather than writing an empty string.
* the **last write** -- the exact three-key mapping OMH left in the file, plus
  the session and task that left it there.
* the **config path** those statements are about.

## The baseline is what the PERSON had, not what OMH left

Reading "whatever the file holds" as the person's value is wrong on exactly
the machine this issue came from, where the file already held a leaked OMH
route: the first new route would have recorded `claude-fable-5-1` as the
baseline and every later restore would have put the most expensive model in
the chain back, permanently.

The evidence to recognise that lives in the same OMH home. Every route OMH
writes appends to `route-provenance.json`, so before capturing a baseline
this compares the current three keys with the newest record there that left a
route in the file. On an exact match of all three the value is OMH's own
leftover, the baseline is recorded as "keys absent", and `baseline_origin`
says `omh_leftover` so a reader is never told a guess was an observation. The
same check runs on a re-capture, which closes the hole a failed record write
would otherwise reopen: the route lands, nothing is recorded, and the next
route would have enshrined it.

## Scope: a task, because `on_session_end` is per turn

`on_session_end` is not a session boundary. Hermes fires it from
`agent/turn_finalizer.py`, whose own comment reads "run_conversation() runs
once per message", and `docs/SESSION-ACTIVITY-RECEIPTS.md` already recorded
that. A route therefore comes back at the end of the turn that wrote it, and
that is the design rather than an accident: "route the NEXT dispatch" is a
turn-local intent, a `delegate_task` is dispatched inside the turn that set
the route, a child already running keeps its model, and a route that cannot
outlive its turn cannot leak.

The scope is the TASK, not the turn, because a plugin tool cannot learn the
turn. The host threads `turn_id` to hooks and to `handle_function_call`, but
`model_tools._execute_tool` passes a registry handler only `task_id` and
`session_id`; the turn id exists elsewhere solely in a private ContextVar of
`tools.approval_context` with no getter. The host builds a turn id as
`{session_id}:{task_id}:{uuid8}` and defaults `task_id` to a fresh uuid per
turn, so task scope IS turn scope unless a caller reuses one task id across
turns -- and then the route lives for that task instead of ending mid-task.
That is the safe direction: restoring later than the writing turn cannot cut
a dispatch short, restoring earlier can.

`action=fallback` is the one flow that legitimately spans turns: a child that
dies on HTTP 400 is reported in a later turn. It used to read the chain
position out of the live `delegation.*` values, which a turn-end restore
empties, so it now recovers that position from the newest provenance record.
See `delegate_route_tool`.

## Ownership, liveness, locking

Every restore is gated on the recorded write. The file's current three keys
are compared with what OMH recorded writing; equal means OMH still owns the
value, different means a person (or another tool) changed it and OMH leaves
it alone and drops its now-meaningless record. The decision reads recorded
values, never wording.

`on_session_start` covers the case a killed session never reaches: it
restores only a route whose recorded writer is not live, asked of that
writer's OWN `state.db` row. Each verdict is named so a caller can tell an
observation from an age bound; see `writer_session_liveness`.

The whole read-through-replace sits inside one lock, because two sessions
routing at once would otherwise both read "no baseline recorded" and the
second would record the first's route as the person's. The lock is
`awareness_delivery`'s, the plugin's single file-locking implementation
(`approval_bypass` already shares it), with a POSIX and a Windows backend. It
is held on the record file in the OMH home rather than on `config.yaml`,
because the mutual exclusion needed is between OMH writers and because OMH
creates no lock file inside the Hermes home. (It does write there: the route
writer creates and chmods a temp file next to `config.yaml` for its atomic
replace. The lock file is the thing that stays out.)

Contention is an error, not a silent retry: a writer that cannot take the
lock returns `lock_unavailable` and never reports `routed`. A platform with
neither lock backend is different and is reported through `lock_enforced`,
because refusing there would disable routing entirely.

## What a restore does NOT promise

It restores the three keys to their previous VALUES, not to their previous
bytes. The writer normalises quoting, emits the keys in `ROUTABLE_KEYS`
order, and drops an inline comment that sat on one of those three lines.
Every other byte of the file is untouched. Preserving the original lines
verbatim would mean carrying them through a writer shared with the route
path, which is more machinery than the difference is worth; the claim is
stated at its real strength here, in the changelog, and in the docs.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Mapping

from . import runtime_paths
from .awareness_delivery import LOCK_MECHANISM_NONE, _awareness_delivery_lock
from .delegation_routing import (
    ROUTABLE_KEYS,
    _VALUE_RE,
    delegation_config_path,
    read_delegation_route,
    write_delegation_route,
)
from .hermes_delegation import load_delegation_route_provenance
from .live_session import LIVE_TUI_SESSION_FRESH_SECONDS, session_row

DELEGATION_ROUTE_RESTORE_SCHEMA_VERSION = "delegation_route_restore/v2"
DELEGATION_ROUTE_RESTORE_FILE = "route-restore.json"
_MAX_ID_CHARS = 160
_MAX_CONFIG_PATH_CHARS = 4096

# Where a baseline came from. `person` is an observation of the file before
# OMH first wrote it; `omh_leftover` says the file already held a route OMH
# had written and not recorded, so the baseline is "keys absent" by
# derivation rather than by observation. A reader must be able to tell.
BASELINE_ORIGIN_PERSON = "person"
BASELINE_ORIGIN_OMH_LEFTOVER = "omh_leftover"

# Provenance origins that leave a route IN the file. `cleared` and
# `exhausted_to_inherit` describe one being taken out, so they say nothing
# about what the keys hold now.
_ROUTE_LEAVING_ORIGINS = ("head", "explicit", "fallback")

# The liveness verdicts that let a restore proceed, as an explicit allow-set
# rather than an inequality against "live". An inequality is how this went
# wrong twice: `!= "live"` would let `unknown_within_age_bound` through, and
# `!= "not_live"` refused the age-bound answers. Anything not listed here
# holds the route.
#
# `not_live` is the only observation in the set: the host closed the writer's
# row. The other two are the age bound, named so a reader can tell a bound
# from an observation.
LIVENESS_CLEARS_RESTORE = frozenset(
    {"not_live", "not_live_by_age", "unclosed_and_stale_by_age"}
)

RESTORE_CLAIM_BOUNDARY = (
    "Baseline bookkeeping for the delegation.* keys of one config.yaml: what "
    "those three keys held before OMH first wrote them and what OMH wrote "
    "last. It is not evidence that any dispatch ran, which model a child "
    "used, or that a lane completed."
)


def delegation_route_restore_path(omh_home: str | Path | None = None) -> Path:
    root = Path(omh_home).expanduser() if omh_home else runtime_paths.default_omh_home()
    return root / "routing" / DELEGATION_ROUTE_RESTORE_FILE


def _valid_route_mapping(value: object) -> dict[str, str] | None:
    """A three-key subset whose values the route writer would accept.

    An absent key is the recorded spelling of "the file did not carry this
    key", so an empty mapping is valid and means "all three absent". A value
    the writer would refuse makes the whole record unusable rather than
    producing a write error later, which is why this refuses instead of
    dropping the offending key.
    """
    if not isinstance(value, dict):
        return None
    cleaned: dict[str, str] = {}
    for key, item in value.items():
        if key not in ROUTABLE_KEYS or not isinstance(item, str):
            return None
        if not _VALUE_RE.match(item):
            return None
        cleaned[key] = item
    return cleaned


def _valid_timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not value == value:  # NaN
        return None
    return float(value)


def _valid_identifier(value: object, limit: int = _MAX_ID_CHARS) -> str | None:
    if not isinstance(value, str) or len(value) > limit:
        return None
    return value


def _valid_restore_record(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("schema_version") != DELEGATION_ROUTE_RESTORE_SCHEMA_VERSION:
        return None
    baseline = _valid_route_mapping(raw.get("baseline"))
    written = _valid_route_mapping(raw.get("written"))
    if baseline is None or written is None:
        return None
    captured_at = _valid_timestamp(raw.get("baseline_captured_at"))
    written_at = _valid_timestamp(raw.get("written_at"))
    if captured_at is None or written_at is None:
        return None
    session_id = _valid_identifier(raw.get("writer_session_id", ""))
    task_id = _valid_identifier(raw.get("writer_task_id", ""))
    config_path = _valid_identifier(raw.get("config_path", ""), _MAX_CONFIG_PATH_CHARS)
    if session_id is None or task_id is None or not config_path:
        return None
    origin = raw.get("baseline_origin", "")
    if origin not in (BASELINE_ORIGIN_PERSON, BASELINE_ORIGIN_OMH_LEFTOVER):
        return None
    return {
        "schema_version": DELEGATION_ROUTE_RESTORE_SCHEMA_VERSION,
        "config_path": config_path,
        "baseline": baseline,
        "baseline_captured_at": captured_at,
        "baseline_origin": origin,
        "written": written,
        "writer_session_id": session_id,
        "writer_task_id": task_id,
        "written_at": written_at,
    }


def load_route_restore_record(omh_home: str | Path | None = None) -> dict[str, Any]:
    """The current baseline record, or `{}` when there is none to trust.

    A missing, unreadable, or malformed file all read as "no record". That is
    the safe direction in both places it is consulted: with no record nothing
    is restored and `config.yaml` is left exactly as it is.
    """
    path = delegation_route_restore_path(omh_home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return _valid_restore_record(raw) or {}


def _write_restore_record(path: Path, record: dict[str, Any]) -> None:
    payload = dict(record)
    payload["claim_boundary"] = RESTORE_CLAIM_BOUNDARY
    temp = path.with_name(f".{path.name}.{os.getpid()}-{secrets.token_hex(8)}.tmp")
    created = False
    try:
        with temp.open("x", encoding="utf-8") as handle:
            created = True
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temp.chmod(0o600)
        temp.replace(path)
        path.chmod(0o600)
    except OSError:
        if created and temp.exists() and not temp.is_symlink():
            temp.unlink()
        raise


def _discard_restore_record(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _routable_subset(applied: Mapping[str, Any]) -> dict[str, str]:
    """The three routable keys of a writer result, as a later read returns them.

    `omh_delegate_route` decorates the writer's `applied` mapping with an
    `alias` afterwards, so the subset is taken by key rather than by copying
    the mapping.
    """
    return {
        key: str(applied[key])
        for key in ROUTABLE_KEYS
        if isinstance(applied.get(key), str) and applied.get(key)
    }


def newest_written_provenance(omh_home: str | Path | None = None) -> dict[str, Any]:
    """The newest provenance record that left a route in the file, or `{}`.

    Two callers need it: recognising a leaked OMH route so it is never
    recorded as a person's baseline, and recovering a chain position for
    `fallback` after the route was restored at the end of the turn that
    wrote it.
    """
    for record in reversed(load_delegation_route_provenance(omh_home)):
        if record.get("origin") in _ROUTE_LEAVING_ORIGINS:
            return record
    return {}


def provenance_route_keys(record: Mapping[str, Any]) -> dict[str, str]:
    """The three routable keys a provenance record says OMH wrote."""
    pairs = (
        ("model", str(record.get("wire_model", "") or record.get("alias", ""))),
        ("reasoning_effort", str(record.get("reasoning_effort", ""))),
        ("provider", str(record.get("provider", ""))),
    )
    return {key: value for key, value in pairs if value}


def _is_omh_leftover(previous: Mapping[str, str], omh_home: str | Path | None) -> bool:
    """True when the keys in the file are a route OMH wrote and never recorded.

    An exact match on all three keys, never a partial one: a person who
    changed one of the three has made the value theirs, and a subset match
    would quietly discard it.
    """
    if not previous:
        return False
    record = newest_written_provenance(omh_home)
    return bool(record) and dict(previous) == provenance_route_keys(record)


def write_route_with_baseline(
    hermes_home: str | Path | None = None,
    *,
    omh_home: str | Path | None = None,
    session_id: str = "",
    task_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    provider: str = "",
    expected_previous: Mapping[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Write a route and keep the baseline bookkeeping in step, under one lock.

    Returns the writer's own result mapping with a `route_restore` field
    naming what the bookkeeping did (`baseline_captured`,
    `baseline_captured_over_omh_leftover`, `baseline_retained`,
    `baseline_recaptured`, `baseline_recaptured_over_omh_leftover`, or
    `unrecorded: <reason>`), plus `lock_enforced`. A lock this writer could
    not take returns `status: error` with `lock_unavailable`, so the caller
    reports a refusal rather than a route.
    """
    record_path = delegation_route_restore_path(omh_home)
    tick = float(now if now is not None else time.time())
    config_path = str(delegation_config_path(hermes_home))
    try:
        with _awareness_delivery_lock(record_path) as mechanism:
            enforced = mechanism != LOCK_MECHANISM_NONE
            previous = read_delegation_route(hermes_home)
            record = load_route_restore_record(omh_home)
            if record and record["config_path"] != config_path:
                # The record describes another profile's config. Treat this
                # one as unrecorded rather than acting on a statement that
                # was never about it.
                record = {}
            result = write_delegation_route(
                hermes_home,
                model=model,
                reasoning_effort=reasoning_effort,
                provider=provider,
                expected_previous=expected_previous,
            )
            result["lock_enforced"] = enforced
            if result.get("status") != "routed":
                return result
            written = _routable_subset(result.get("applied") or {})
            if record and record["written"] == previous:
                # OMH still owned the value it is overwriting: the baseline
                # already describes what the person had.
                baseline = record["baseline"]
                captured_at = record["baseline_captured_at"]
                origin = record["baseline_origin"]
                note = "baseline_retained"
            else:
                # Either nothing was recorded, or someone changed the keys
                # since OMH last wrote them. Both mean this write is over a
                # value OMH cannot prove it recorded -- so a baseline is
                # captured, unless provenance says OMH wrote that value
                # itself and only failed to record it.
                stem = "baseline_recaptured" if record else "baseline_captured"
                if _is_omh_leftover(previous, omh_home):
                    baseline, origin = {}, BASELINE_ORIGIN_OMH_LEFTOVER
                    note = f"{stem}_over_omh_leftover"
                else:
                    baseline, origin = previous, BASELINE_ORIGIN_PERSON
                    note = stem
                captured_at = tick
            result["route_restore"] = _store_record(
                record_path,
                note=note,
                config_path=config_path,
                baseline=baseline,
                baseline_captured_at=captured_at,
                baseline_origin=origin,
                written=written,
                writer_session_id=str(session_id or "")[:_MAX_ID_CHARS],
                writer_task_id=str(task_id or "")[:_MAX_ID_CHARS],
                written_at=tick,
            )
            result["baseline_origin"] = origin
            return result
    except TimeoutError:
        return {
            "status": "error",
            "error": (
                "another session is writing the delegation route; no route was "
                "written, so the next delegate_task dispatch uses the route "
                "already in place"
            ),
            "error_type": "TimeoutError",
            "route_restore": "lock_unavailable",
        }
    except (OSError, ValueError) as exc:
        return {
            "status": "error",
            "error": f"route restore bookkeeping failed: {type(exc).__name__}",
            "error_type": type(exc).__name__,
            "route_restore": "unrecorded: bookkeeping unavailable",
        }


def _store_record(path: Path, *, note: str, **fields: Any) -> str:
    """Persist the bookkeeping; a failure degrades the restore, not the route.

    The route is already in `config.yaml` by the time this runs. Failing the
    whole call here would report an error over a write that happened, so the
    failure is named in the returned note instead. The route then reads as
    unrecorded, and the next write recognises it through provenance rather
    than enshrining it as a baseline.
    """
    record = {"schema_version": DELEGATION_ROUTE_RESTORE_SCHEMA_VERSION, **fields}
    try:
        _write_restore_record(path, record)
    except OSError as exc:
        return f"unrecorded: {type(exc).__name__}"
    return note


def restore_delegation_baseline(
    hermes_home: str | Path | None = None,
    *,
    omh_home: str | Path | None = None,
    trigger: str = "",
    require_writer_session: str | None = None,
    require_writer_task: str | None = None,
    require_writer_not_live: bool = False,
    clear_when_unrecorded: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Put the recorded baseline back, when OMH still owns the value.

    `require_writer_session` and `require_writer_task` restrict the restore to
    the session and task that wrote the route, which is what the turn-end
    hook needs so a later turn does not put a baseline back underneath a
    newer route. `require_writer_not_live` is the session-start path's extra
    gate. `clear_when_unrecorded` is the explicit `clear` action's escape
    hatch: with no record OMH cannot know what the keys held, and removing
    them is both what clearing did before baselines existed and the only way
    to get an unrecorded route off a machine. It runs inside this lock,
    because doing it in the caller left a window in which a concurrent route
    recorded a person's pinned model as a baseline that the next restore then
    discarded as a foreign edit.

    Every outcome is a `status` a caller can report: `restored`, `cleared` (a
    baseline with no keys to put back), `no_baseline_recorded`,
    `foreign_edit`, `foreign_config`, `not_last_writer`, `writer_live`,
    `lock_unavailable`, or `error`.
    """
    record_path = delegation_route_restore_path(omh_home)
    if not clear_when_unrecorded and not record_path.exists():
        # The common case by far: every turn of every session reaches here,
        # and almost none of them has a route to take back. One `stat`, no
        # lock, no sqlite. The race it accepts is a record created between
        # this check and a lock that is never taken, which costs one missed
        # restore that the next turn end or the next session start makes
        # good -- never a wrong write.
        return {"status": "no_baseline_recorded", "trigger": trigger, "fast_path": True}
    try:
        with _awareness_delivery_lock(record_path) as mechanism:
            enforced = mechanism != LOCK_MECHANISM_NONE
            record = load_route_restore_record(omh_home)
            config_path = str(delegation_config_path(hermes_home))
            if not record:
                if clear_when_unrecorded:
                    removed = write_delegation_route(hermes_home, clear=True)
                    removed["route_restore"] = "no_baseline_recorded"
                    removed["trigger"] = trigger
                    removed["lock_enforced"] = enforced
                    return removed
                return {
                    "status": "no_baseline_recorded",
                    "trigger": trigger,
                    "lock_enforced": enforced,
                }
            if record["config_path"] != config_path:
                # Two profiles can share one OMH home by design. A record
                # about the other profile's file is not a foreign edit and
                # must NOT be dropped: dropping it stranded the route it
                # described, with no record left to take that route back.
                return {
                    "status": "foreign_config",
                    "trigger": trigger,
                    "recorded_config": record["config_path"],
                    "this_config": config_path,
                    "lock_enforced": enforced,
                }
            mismatched_writer = (
                require_writer_session is not None
                and record["writer_session_id"] != require_writer_session
            ) or (
                require_writer_task is not None
                and record["writer_task_id"] != require_writer_task
            )
            if mismatched_writer:
                return {
                    "status": "not_last_writer",
                    "trigger": trigger,
                    "lock_enforced": enforced,
                }
            liveness, writer_source = "not_checked", ""
            if require_writer_not_live:
                liveness, writer_source = _writer_liveness(
                    record["writer_session_id"],
                    hermes_home=hermes_home,
                    written_at=record["written_at"],
                    now=now,
                )
                if liveness not in LIVENESS_CLEARS_RESTORE:
                    return {
                        "status": "writer_live",
                        "trigger": trigger,
                        "liveness": liveness,
                        # Report only, never a gate: which surface held the
                        # route is what an operator wants beside a withheld
                        # restore. It comes out of the row the verdict was
                        # already read from, not a second query.
                        "writer_source": writer_source,
                        "lock_enforced": enforced,
                    }
            current = read_delegation_route(hermes_home)
            if current != record["written"]:
                # Someone else owns the value now. Drop the record: keeping it
                # would let a later restore act on a baseline that no longer
                # describes anything in the file.
                _discard_restore_record(record_path)
                return {
                    "status": "foreign_edit",
                    "trigger": trigger,
                    "liveness": liveness,
                    "lock_enforced": enforced,
                }
            baseline = record["baseline"]
            result = write_delegation_route(
                hermes_home,
                model=baseline.get("model", ""),
                reasoning_effort=baseline.get("reasoning_effort", ""),
                provider=baseline.get("provider", ""),
                expected_previous=current,
            )
            if result.get("status") not in ("routed", "cleared"):
                return {
                    "status": "error",
                    "error": str(result.get("error", "route restore write failed")),
                    "error_type": "RouteWriteRefused",
                    "trigger": trigger,
                    "lock_enforced": enforced,
                }
            _discard_restore_record(record_path)
            return {
                # Two outcomes a reader acts on differently, so they keep
                # separate names: `restored` put values back, `cleared` is the
                # baseline that had no keys to put back, which is the same
                # end state the tool reported before baselines existed.
                "status": "restored" if baseline else "cleared",
                "trigger": trigger,
                "restored": baseline,
                "baseline_origin": record["baseline_origin"],
                "replaced": current,
                "liveness": liveness,
                "lock_enforced": enforced,
            }
    except TimeoutError:
        return {
            "status": "lock_unavailable",
            "trigger": trigger,
            "error_type": "TimeoutError",
        }
    except (OSError, ValueError) as exc:
        return {
            "status": "error",
            "error": f"route restore failed: {type(exc).__name__}",
            "error_type": type(exc).__name__,
            "trigger": trigger,
        }


def _writer_liveness(
    writer_session_id: str,
    *,
    hermes_home: str | Path | None = None,
    written_at: float,
    now: float | None = None,
) -> tuple[str, str]:
    """`(verdict, source)` from ONE read of the writer's own row."""
    home = str(hermes_home) if hermes_home else ""
    row = session_row(home, writer_session_id)
    current = float(now if now is not None else time.time())
    if row is None:
        if current - written_at > LIVE_TUI_SESSION_FRESH_SECONDS:
            return "not_live_by_age", ""
        return "unknown_within_age_bound", ""
    source = str(row["source"])
    if row["ended_at"] is not None:
        return "not_live", source
    activity = row["activity"]
    if isinstance(activity, (int, float)) and not isinstance(activity, bool):
        if current - float(activity) <= LIVE_TUI_SESSION_FRESH_SECONDS:
            return "live", source
    if current - written_at > LIVE_TUI_SESSION_FRESH_SECONDS:
        return "unclosed_and_stale_by_age", source
    return "unclosed_within_age_bound", source


def writer_session_liveness(
    writer_session_id: str,
    *,
    hermes_home: str | Path | None = None,
    written_at: float,
    now: float | None = None,
) -> str:
    """Whether the session that wrote the route is still running.

    The question is asked of the WRITER'S OWN row, never of a list the writer
    may not belong to. Asking the live-TUI list instead was the first version
    of this and it was wrong: that list is scoped to "which session is a
    person looking at", so a writer on any other surface is absent from it by
    construction, and absence read as `not_live` restored a baseline under a
    live writer whenever some unrelated TUI happened to be open. Measured on
    the owner's homes, that is the common case rather than the edge: the
    profile that routes most writes every route from a `slack` or `subagent`
    session (#1737 review).

    No transport-id reconciliation happens here. The only caller records the
    writer from `host_session_id(kwargs)`, which the host sets from
    `agent.session_id` -- already the durable key `state.db` rows carry -- so
    a reconciliation step would be unreachable code presenting itself as a
    necessary one.

    The verdicts, and which of them are observations:

    * `live` -- the row exists, the host has not closed it, and its activity
      stamp is inside the freshness window. An observation.
    * `not_live` -- the row exists and carries an `ended_at`. An observation,
      and the only one that clears a restore.
    * `unclosed_and_stale_by_age` / `unclosed_within_age_bound` -- the row
      exists, the host never closed it, and its activity is stale or
      unusable. This is a killed session, or a gateway session of a kind that
      never ends (55 of 57 slack sessions on the owner's machine are open
      rows). Nothing here observes that the process is gone, so the ROUTE's
      own age decides and the name says a bound answered.
    * `not_live_by_age` / `unknown_within_age_bound` -- no row at all, or the
      host surface could not be read. Same bound, same honesty about which
      question went unanswered.

    The bound is `LIVE_TUI_SESSION_FRESH_SECONDS`, six hours. Task scope
    makes it matter far less than it did: the ordinary way back is the end of
    the writing task, and this path only covers a session killed mid-task.
    Nothing available shortens it -- the host's lease registry records which
    surfaces are open, not whether their processes are alive.
    """
    return _writer_liveness(
        writer_session_id, hermes_home=hermes_home, written_at=written_at, now=now
    )[0]

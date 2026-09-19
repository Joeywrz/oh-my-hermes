"""The way back from a prepared delegation route.

`delegation_routing.write_delegation_route` is the one-way half: it sets the
three routable keys Hermes re-reads per dispatch. Nothing put back what was
there before, so a route chosen for one lane of one session stayed in
`config.yaml` as every later session's delegation default -- including a
session that never called the route tool (#1724).

This module owns the way back. It records, beside the route write and under
the same lock, two things:

* the **baseline** -- what the three keys held the first time OMH wrote over
  values it did not write. A key the file did not carry is recorded by being
  ABSENT from the baseline mapping, so restoring it removes the key rather
  than writing an empty string. Later OMH routes do not touch the baseline.
* the **last write** -- the exact three-key mapping OMH left in the file, plus
  which session left it there.

Every restore is gated on that last write. The file's current three keys are
compared with what OMH recorded writing; equal means OMH still owns the value
and the baseline goes back, different means a person (or another tool) changed
it and OMH leaves it alone and drops its now-meaningless record. The decision
reads recorded values, never wording.

A person's intervening edit is the one thing that re-captures the baseline. It
has to: at that moment OMH is again writing over a value it did not write, and
the old baseline no longer describes what the person had. Consecutive OMH
routes leave it alone, which is the case the rule is written for.

Three callers reach it:

* `omh_delegate_route` action `clear`, which now restores the baseline instead
  of deleting the keys, and chain exhaustion, which restores it for the same
  reason it used to clear -- the next dispatch should run on a model that
  works, and the person's own pinned model is the best answer available.
* `on_session_end`, for the session that wrote the route.
* `on_session_start`, because a killed TUI never reaches `on_session_end`.
  That path additionally requires the recorded writer NOT to be a live
  session, asked of that writer's OWN `state.db` row. When the row does not
  settle it -- no row, an unreadable file, or a row the host never closed,
  which is what a killed TUI and most gateway sessions leave behind -- the
  route's own age decides instead (`LIVE_TUI_SESSION_FRESH_SECONDS`, six
  hours). Each verdict is named so a caller can tell an observation from a
  bound; see `writer_session_liveness`.

Concurrency is the reason the whole read-through-replace sits inside one lock
rather than each half taking its own. Two TUIs routing at once is ordinary on
a single machine: without the lock, both could read "no baseline recorded",
both write, and the second would record the first's route as the person's
baseline. The lock is `awareness_delivery`'s, which is the plugin's single
file-locking implementation (`approval_bypass` already shares it) and carries
both a POSIX and a Windows backend. It is held on the record file in the OMH
home, not on `config.yaml` in the Hermes home: the mutual exclusion needed is
between OMH writers, nothing else takes it, and OMH does not create files or
change permissions inside the Hermes home.

Contention is an error, not a silent retry. A writer that cannot take the lock
returns `lock_unavailable` and the tool surfaces it; it never reports
`routed`. A platform with neither lock backend is different and is reported
differently: the block still runs, because refusing would disable routing
there entirely, and `lock_enforced` is false so a reader can tell that the
mutual-exclusion guarantee did not hold.
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
    read_delegation_route,
    write_delegation_route,
)
from .live_session import (
    LIVE_TUI_SESSION_FRESH_SECONDS,
    session_row,
    tui_session_durable_id,
)

DELEGATION_ROUTE_RESTORE_SCHEMA_VERSION = "delegation_route_restore/v1"
DELEGATION_ROUTE_RESTORE_FILE = "route-restore.json"
_MAX_SESSION_ID_CHARS = 160

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
    "Baseline bookkeeping for the delegation.* keys only: this record says "
    "what those three keys held before OMH first wrote them and what OMH "
    "wrote last. It is not evidence that any dispatch ran, which model a "
    "child used, or that a lane completed."
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
    session_id = raw.get("writer_session_id", "")
    if not isinstance(session_id, str) or len(session_id) > _MAX_SESSION_ID_CHARS:
        return None
    return {
        "schema_version": DELEGATION_ROUTE_RESTORE_SCHEMA_VERSION,
        "baseline": baseline,
        "baseline_captured_at": captured_at,
        "written": written,
        "writer_session_id": session_id,
        "written_at": written_at,
    }


def load_route_restore_record(omh_home: str | Path | None = None) -> dict[str, Any]:
    """The current baseline record, or `{}` when there is none to trust.

    A missing, unreadable, or malformed file all read as "no record". That is
    the safe direction in both places it is consulted: with no record nothing
    is restored, and the value in `config.yaml` is left exactly as it is.
    """
    path = delegation_route_restore_path(omh_home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    record = _valid_restore_record(raw)
    return record or {}


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


def write_route_with_baseline(
    hermes_home: str | Path | None = None,
    *,
    omh_home: str | Path | None = None,
    session_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    provider: str = "",
    expected_previous: Mapping[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Write a route and keep the baseline bookkeeping in step, under one lock.

    Returns the writer's own result mapping with a `route_restore` field
    describing what the bookkeeping did (`baseline_captured`,
    `baseline_retained`, `baseline_recaptured`, or `unrecorded: <reason>`),
    plus `lock_enforced`. A lock this writer could not take returns
    `status: error` with `lock_unavailable`, so the caller reports a refusal
    rather than a route.
    """
    record_path = delegation_route_restore_path(omh_home)
    tick = float(now if now is not None else time.time())
    try:
        with _awareness_delivery_lock(record_path) as mechanism:
            enforced = mechanism != LOCK_MECHANISM_NONE
            previous = read_delegation_route(hermes_home)
            record = load_route_restore_record(omh_home)
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
            if not record:
                baseline, captured_at, note = previous, tick, "baseline_captured"
            elif record["written"] == previous:
                # OMH still owned the value it is overwriting: the baseline
                # already describes what the person had.
                baseline = record["baseline"]
                captured_at = record["baseline_captured_at"]
                note = "baseline_retained"
            else:
                # Someone changed the keys since OMH last wrote them, so this
                # write is again a write over values OMH did not write and the
                # old baseline no longer describes what the person had.
                baseline, captured_at, note = previous, tick, "baseline_recaptured"
            result["route_restore"] = _store_record(
                record_path,
                baseline=baseline,
                baseline_captured_at=captured_at,
                written=written,
                writer_session_id=str(session_id or "")[:_MAX_SESSION_ID_CHARS],
                written_at=tick,
                note=note,
            )
            return result
    except TimeoutError:
        return {
            "status": "error",
            "error": (
                "another session is writing the delegation route; no route was "
                "written, so the next delegate_task dispatch uses the route "
                "already in place"
            ),
            "route_restore": "lock_unavailable",
        }
    except (OSError, ValueError) as exc:
        return {
            "status": "error",
            "error": f"route restore bookkeeping failed: {type(exc).__name__}",
            "route_restore": "unrecorded: bookkeeping unavailable",
        }


def _store_record(
    path: Path,
    *,
    baseline: dict[str, str],
    baseline_captured_at: float,
    written: dict[str, str],
    writer_session_id: str,
    written_at: float,
    note: str,
) -> str:
    """Persist the bookkeeping; a failure degrades the restore, not the route.

    The route is already in `config.yaml` by the time this runs. Failing the
    whole call here would report an error over a write that happened, so the
    failure is named in the returned note instead: the caller learns the route
    landed and the way back was not recorded.
    """
    record = {
        "schema_version": DELEGATION_ROUTE_RESTORE_SCHEMA_VERSION,
        "baseline": baseline,
        "baseline_captured_at": baseline_captured_at,
        "written": written,
        "writer_session_id": writer_session_id,
        "written_at": written_at,
    }
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
    require_writer_not_live: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Put the recorded baseline back, when OMH still owns the value.

    `require_writer_session` restricts the restore to the session that wrote
    the route, which is what `on_session_end` needs so a session that ended
    under a later session's route does not pull the baseline out from under
    it. `require_writer_not_live` is the session-start path's extra gate.

    Every outcome is a `status` a caller can report: `restored`, `cleared`
    (a baseline with no keys in it, so nothing came back), `no_baseline_recorded`,
    `foreign_edit`, `not_last_writer`, `writer_live`, `lock_unavailable`, or
    `error`.
    """
    record_path = delegation_route_restore_path(omh_home)
    try:
        with _awareness_delivery_lock(record_path) as mechanism:
            enforced = mechanism != LOCK_MECHANISM_NONE
            record = load_route_restore_record(omh_home)
            if not record:
                return {
                    "status": "no_baseline_recorded",
                    "trigger": trigger,
                    "lock_enforced": enforced,
                }
            writer = record["writer_session_id"]
            if require_writer_session is not None and writer != require_writer_session:
                return {
                    "status": "not_last_writer",
                    "trigger": trigger,
                    "lock_enforced": enforced,
                }
            liveness = "not_checked"
            if require_writer_not_live:
                liveness = writer_session_liveness(
                    writer,
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
                        # route is what an operator wants to see beside a
                        # withheld restore.
                        "writer_source": writer_session_source(
                            writer, hermes_home=hermes_home
                        ),
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
                "replaced": current,
                "liveness": liveness,
                "lock_enforced": enforced,
            }
    except TimeoutError:
        return {"status": "lock_unavailable", "trigger": trigger}
    except (OSError, ValueError) as exc:
        return {
            "status": "error",
            "error": f"route restore failed: {type(exc).__name__}",
            "trigger": trigger,
        }


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

    The verdicts, and which of them are observations:

    * `live` -- the row exists, the host has not closed it, and its activity
      stamp is inside the freshness window. An observation.
    * `not_live` -- the row exists and carries an `ended_at`. An observation,
      and the only one that clears a restore.
    * `unclosed_and_stale_by_age` / `unclosed_within_age_bound` -- the row
      exists, the host never closed it, and its activity is stale or
      unusable. This is a killed TUI, or a gateway session of a kind that
      never ends (55 of 57 slack sessions on the owner's machine are open
      rows). Nothing here observes that the process is gone, so the ROUTE's
      own age decides and the name says a bound answered.
    * `not_live_by_age` / `unknown_within_age_bound` -- no row at all, or the
      host surface could not be read. Same bound, and the same honesty about
      which question went unanswered.

    The bound is `LIVE_TUI_SESSION_FRESH_SECONDS`, six hours, and it is the
    accepted cost of the trade: a route written by a killed TUI can outlive
    it by up to six hours before the session-start path takes it back, while
    the session that wrote it keeps its route for as long as it might still
    be dispatching. Restoring under a live writer sends that writer's next
    child to the wrong model, which is worse than a stale route persisting
    for one more session. Nothing available shortens it -- the host's lease
    registry records which surfaces are open, not whether their processes
    are alive, and a lease is not revoked when a TUI is killed.
    """
    home = str(hermes_home) if hermes_home else ""
    row = session_row(home, writer_session_id)
    if row is None:
        # A widget-side reference is the gateway transport id rather than the
        # durable key `state.db` rows carry, so the two names for one session
        # are reconciled through the host's own lease registry before the
        # writer is called absent.
        durable = tui_session_durable_id(home, writer_session_id)
        if durable:
            row = session_row(home, durable)
    current = float(now if now is not None else time.time())
    if row is None:
        if current - written_at > LIVE_TUI_SESSION_FRESH_SECONDS:
            return "not_live_by_age"
        return "unknown_within_age_bound"
    if row["ended_at"] is not None:
        return "not_live"
    activity = row["activity"]
    if isinstance(activity, (int, float)) and not isinstance(activity, bool):
        if current - float(activity) <= LIVE_TUI_SESSION_FRESH_SECONDS:
            return "live"
    if current - written_at > LIVE_TUI_SESSION_FRESH_SECONDS:
        return "unclosed_and_stale_by_age"
    return "unclosed_within_age_bound"


def writer_session_source(
    writer_session_id: str, *, hermes_home: str | Path | None = None
) -> str:
    """The surface the writer ran on, for the report only.

    Reported beside the verdict so an operator reading a withheld restore can
    see which kind of session held the route. Never a gate: no decision
    anywhere branches on this value, because the verdict already carries
    every fact a decision needs.
    """
    row = session_row(str(hermes_home) if hermes_home else "", writer_session_id)
    return str(row["source"]) if row else ""

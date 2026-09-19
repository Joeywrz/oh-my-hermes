"""Parallel tool-call burst observation, in-flight liveness, repeat guard.

Hermes executes a model turn's batched tool calls concurrently (its
``_execute_tool_calls_concurrent`` dispatch), but the transcript renders
only a collapsed "Tool calls (N)" group -- nothing tells the user whether
that batch actually ran as a parallel shot, or whether the batch is still
running at all. The ``pre_tool_call`` hook fires once per call, so calls
whose start ticks land inside one short window are the concurrent batch;
this module records those ticks (tool name, timestamp, and a short one-way
digest of the call's arguments -- never the arguments themselves, and
never a result) and projects the latest burst for the ``[OMH]`` status
line.

That digest is what the repeat guard compares, and it is the reason the
"never arguments" line above is worded the way it is. It is a truncated
BLAKE2b over the canonicalized arguments, capped before hashing, and only
the hex digest is stored: equal digests mean an identical call, unequal
digests mean a different one, and that is the whole of what the field
says. Nothing written here can be read as a path, a pattern, or a file
body, so the record stays inside the plugin's declared ``privacy:
metadata_only`` contract (``config.yaml``) -- a fingerprint of content is
not content. Stated exactly, because this is a privacy claim: a digest is
confirmable by guessing, not readable. Someone holding this file and a
candidate argument string can test that guess, which is the ordinary
property of every fingerprint and is precisely why the field is a digest
instead of a truncated copy of the arguments.

``post_tool_call`` (a second supported Hermes observer hook, paired with
``pre_tool_call`` by ``tool_call_id``) closes what ``pre_tool_call`` opens.
Pairing the two gives an exact in-flight count -- the gap the
ring-buffer-only design could not see: a stopped turn with an incomplete
todo item read identically to 40 tool calls genuinely running, because
nothing distinguished "the ring saturated" from "work is happening". A
host that omits ``tool_call_id`` degrades silently to the pre-pairing
behavior: the tick still lands for burst grouping, but no in-flight entry
opens, and the HUD's liveness signal simply stays quiet for that call.

Scope: the ledger lives at one path per OMH home
(``<omh_home>/runtime/tool-bursts.json``), machine-wide and shared by every
Hermes session pointed at that home. The burst and in-flight projections
are not scoped to one session or turn: a sibling session sharing the same
OMH home shows up in this session's liveness signal exactly the same as
this session's own calls, and the ``turn_id`` stored on each open entry
rides along as inert metadata that nothing here reads back.

The repeat streak is the one part that IS keyed by session, and it has to
be for the same reason the rest is not: in a ledger every session writes
to, an unkeyed consecutive counter would read two sessions running the
same search as one session looping. Each session holds exactly one entry,
carrying only its last call's tool, argument digest, count, and tick, so
the keyed part cannot accumulate per session beyond that single row.
"""
from __future__ import annotations

from . import runtime_paths

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Reuse the awareness ledger's portable lock and atomic-write primitives so
# there is exactly one file-locking implementation in the plugin.
from .awareness_delivery import _awareness_delivery_lock, _write_delivery_record

TOOL_BURSTS_SCHEMA_VERSION = "omh_tool_bursts/v1"
TOOL_ACTIVITY_SCHEMA_VERSION = "omh_tool_activity/v1"
TOOL_BURSTS_FILE = "tool-bursts.json"
# Raised from 40 (2026-08, HUD liveness fix): the ring ceiling used to be
# what the "parallel shot x40" badge actually measured -- a fanout wave of
# 40+ short calls saturated the ring long before it went idle, so the badge
# read the buffer filling up, not the truth about what was running. 200 is
# burst HISTORY depth, not a concurrency claim: Hermes caps concurrent tool
# workers well below this (`_MAX_TOOL_WORKERS` in Hermes'
# `agent/tool_executor.py`), so no real dispatch ever has 200 calls open at
# once -- this ceiling exists to keep a long chain of small, fast, strictly
# SEQUENTIAL calls (which the 1.5s grouping window still chains into one
# burst) from growing the file without bound, while keeping each entry
# small (two-three short fields).
MAX_TOOL_BURST_ENTRIES = 200
# Same headroom rationale as MAX_TOOL_BURST_ENTRIES, applied to the
# in-flight ledger: a host that never sends a matching post_tool_call (a
# crash, an unsupported host) must not let open_calls grow without bound.
# Losing the oldest open entry under pathological load is acceptable --
# the ledger is best-effort observation, not a correctness-critical store.
MAX_OPEN_TOOL_CALLS = 200
# Ticks closer together than this belong to one dispatch burst: Hermes
# starts a concurrent batch's workers within milliseconds of each other,
# while consecutive sequential turns are separated by at least one model
# round-trip.
BURST_WINDOW_SECONDS = 1.5
# The HUD shows the latest burst only while it is recent enough to still
# describe "what just happened". Seconds, not minutes, by owner direction
# ('한 몇초만 지나면 바로없어지게'): the badge flags the batch as it lands
# and vanishes right after, refreshed continuously through a busy wave by
# the next batch's ticks.
BURST_FRESH_SECONDS = 8.0
# An open call with no matching post_tool_call this long is not still
# running -- it is a process restart, a crashed host, or a lost tick. It is
# reported as expired rather than kept open forever, and never reported as
# completed (nothing observed it finishing). Known limitation, not fixed
# here: at 100+-way concurrency the file lock `record_tool_call_close`
# takes can itself time out under contention, which strands that close as a
# best-effort no-op -- the entry then rides out this same TTL instead of
# closing promptly, which is an accepted cost of best-effort observation.
TOOL_CALL_OPEN_TTL_SECONDS = 15 * 60
TOOL_BURST_CLAIM_BOUNDARY = (
    "Burst grouping observes pre_tool_call start ticks only; it is evidence "
    "the host dispatched the calls as one batch, not proof every call "
    "overlapped for its full duration."
)
TOOL_ACTIVITY_CLAIM_BOUNDARY = (
    "In-flight state is the local pairing of this host's pre_tool_call and "
    "post_tool_call ticks by tool_call_id. It is evidence of calls this OMH "
    "install observed opening and closing, not a complete accounting of "
    "every tool call Hermes ran."
)
# Consecutive identical calls a session may make before `pre_tool_call`
# refuses the next one. Sized against the two observed numbers rather than
# picked as a round figure. A legitimate immediate retry runs two or three
# times: a transient tool failure, or an argument edited and changed back.
# The failure this guard exists for ran the SAME search 180+ times in a row
# with identical arguments, each returning in ~0.0s, until the session's
# budget was gone and the work it was asked to do had never started -- and
# the tool result already said "BLOCKED: you have run this exact search N
# times in a row", which the model read and ignored, so the refusal has to
# be the hook's, not a warning's.
# Four is one past the top of the legitimate band, so no retry pattern
# observed here ever meets it, and it caps the pathological case at four
# wasted calls instead of 180. A larger threshold buys nothing: the loop is
# unbounded, so every value in this gap ends it, just later. Three would
# refuse the next call after the longest legitimate retry run.
REPEAT_CALL_BLOCK_THRESHOLD = 4
# Arguments are canonicalized and capped before hashing so a single huge
# call (a whole-file write body) cannot make the digest step expensive.
# Two calls whose arguments differ only past the cap share a digest and so
# read as a repeat; acceptable here, because they still agree on their
# first 8 KiB and any different call clears the guard.
MAX_DIGEST_INPUT_BYTES = 8192
# One streak row per session id, capped like the other maps in this file:
# the ledger is machine-wide and a long-lived host must not grow it without
# bound. Dropping the least recently active session's row costs that
# session its count, which is the same as its guard never having armed.
MAX_REPEAT_STREAKS = 64
# A streak whose last call is older than this no longer describes a loop.
# A real one fires its repeats back to back inside a single turn (the
# observed burn ran 180 calls at ~0.0s each); after a gap this long the
# session has been idle, or the host restarted, or a session id was reused,
# and the guard must not greet a returning session with a refusal it earned
# much earlier. It is also what keeps a dead session's row from sitting in
# the file until the cap evicts it.
REPEAT_STREAK_WINDOW_SECONDS = 300.0
REPEAT_CALL_BLOCK_SUFFIX = (
    "This tool call was blocked by OMH before execution because it exactly "
    "repeats the calls just before it. A blocked call did not run, and any "
    "different tool call clears the guard immediately."
)
_MAX_TOOL_NAME_CHARS = 48
_MAX_ID_CHARS = 128
# 16 hex characters is what `tool_args_digest` produces. The cap only
# bounds what a foreign or hand-edited file can put in the field.
_MAX_DIGEST_CHARS = 64


def _runtime_dir(omh_home: str = "") -> Path:
    root = Path(omh_home).expanduser() if omh_home else runtime_paths.default_omh_home()
    return root / "runtime"


def tool_bursts_path(omh_home: str = "") -> Path:
    return _runtime_dir(omh_home) / TOOL_BURSTS_FILE


def _normalized_name(tool_name: object) -> str:
    return " ".join(str(tool_name or "").split())[:_MAX_TOOL_NAME_CHARS]


def _normalized_id(value: object) -> str:
    return str(value or "").strip()[:_MAX_ID_CHARS]


def tool_args_digest(tool_input: object) -> str:
    """A short one-way digest of this call's arguments, or "" for none.

    "" is the degrade-to-allow signal, and the only one: arguments that
    cannot be canonicalized (a self-referential structure) produce no
    digest, no streak row, and therefore no refusal. The guard would rather
    miss a loop than block a call it could not identify.
    """
    try:
        canonical = json.dumps(tool_input, sort_keys=True, default=str)
    except (TypeError, ValueError, RecursionError):
        return ""
    return hashlib.blake2b(
        canonical.encode("utf-8", "replace")[:MAX_DIGEST_INPUT_BYTES],
        digest_size=8,
    ).hexdigest()


def _repeat_block_message(tool_name: str, count: int) -> str:
    return (
        f"[OMH Repeat Guard] `{tool_name}` has already run {count} times in a "
        "row in this session with exactly these arguments, and returned the "
        "same result each time. Running it again returns that same result, so "
        "stop repeating it and get the answer another way: read the file "
        "directly, or run the search once in the terminal and use that "
        "output. Then continue the work you were asked to do.\n"
        f"{REPEAT_CALL_BLOCK_SUFFIX}"
    )


def repeat_call_directive(
    *,
    tool_name: object,
    args_digest: object,
    session_id: object = "",
    omh_home: str = "",
    now: float | None = None,
) -> dict[str, str] | None:
    """Block a call this session has already made and had answered, or None.

    Read-only: the streak this reads is advanced by ``record_tool_call`` on
    the calls that actually dispatch, so a blocked call ticks nothing and
    the count in the message is the number of times the call really ran.

    Every path out of the narrow positive case is ``None`` -- allow. The
    conjunction is: this session is identified, the arguments produced a
    digest, the session's last recorded call used the same tool and the
    same digest, that call is recent enough to still be the same loop, and
    the count has reached the threshold. A missing session id, an
    unreadable or absent ledger, a malformed row, and a path that will not
    resolve all land here as allow, because the cost of a wrong refusal is
    the one #1674 already charged: a veto that ends sessions it was never
    meant to touch.
    """
    name = _normalized_name(tool_name)
    session = _normalized_id(session_id)
    digest = str(args_digest or "")[:_MAX_DIGEST_CHARS]
    # `not session` is a stated precondition, not a load-bearing branch, and
    # deleting it turns no test red: no row can be keyed to a blank session
    # (`_advance_repeat_streak` writes none and `_sanitized_repeat_streaks`
    # drops one a hand-edited file supplies), so the lookup below would miss
    # anyway. It stays because a veto should refuse on its own terms rather
    # than inherit its safety from two other functions.
    if not name or not session or not digest:
        return None
    tick = float(now if now is not None else time.time())
    try:
        streak = _read_record(tool_bursts_path(omh_home))["repeat_streaks"].get(session)
    except (OSError, ValueError, TypeError, RuntimeError):
        return None
    if streak is None or streak["tool"] != name or streak["args_digest"] != digest:
        return None
    if tick - streak["ts"] > REPEAT_STREAK_WINDOW_SECONDS:
        return None
    count = int(streak["count"])
    if count < REPEAT_CALL_BLOCK_THRESHOLD:
        return None
    return {"action": "block", "message": _repeat_block_message(name, count)}


def record_tool_call(
    tool_name: object,
    *,
    omh_home: str = "",
    now: float | None = None,
    tool_call_id: object = None,
    turn_id: object = None,
    args_digest: object = "",
    session_id: object = "",
) -> None:
    """Append one pre_tool_call tick and, when the host supplies a
    tool_call_id, open an in-flight entry post_tool_call will close.
    Best-effort: losing a tick is acceptable, breaking the hook that feeds
    the model is not."""
    name = _normalized_name(tool_name)
    if not name:
        return
    tick = float(now if now is not None else time.time())
    call_id = _normalized_id(tool_call_id)
    path = tool_bursts_path(omh_home)
    try:
        with _awareness_delivery_lock(path):
            record = _read_record(path)
            open_calls = _prune_expired_opens(record["open_calls"], now=tick)
            # How many calls this install already has open the instant this
            # one starts, counting itself if it opens too. This is the only
            # honest concurrency evidence available: closed entries carry no
            # end time (record_tool_call_close only deletes them), so a
            # group's true peak can only be observed live, at tick time, not
            # reconstructed afterward from start ticks alone.
            open_at_tick = len(open_calls) + (1 if call_id else 0)
            entries = record["entries"]
            entries.append({"tool": name, "ts": tick, "id": call_id, "open_at_tick": open_at_tick})
            entries = entries[-MAX_TOOL_BURST_ENTRIES:]
            if call_id:
                open_calls[call_id] = {
                    "tool": name,
                    "turn_id": _normalized_id(turn_id),
                    "started_at": tick,
                }
                open_calls = _cap_open_calls(open_calls)
            # Counted here rather than in the gate so the streak only ever
            # counts calls that reached dispatch: a call the gate refused
            # never ran, and must not be reported as one that did.
            repeat_streaks = _advance_repeat_streak(
                _prune_stale_streaks(record["repeat_streaks"], now=tick),
                session=_normalized_id(session_id),
                tool=name,
                args_digest=str(args_digest or "")[:_MAX_DIGEST_CHARS],
                now=tick,
            )
            _write_delivery_record(
                path,
                {
                    "schema_version": TOOL_BURSTS_SCHEMA_VERSION,
                    "entries": entries,
                    "open_calls": open_calls,
                    "repeat_streaks": repeat_streaks,
                    "post_tool_call_observed_at": record["post_tool_call_observed_at"],
                },
            )
    except (OSError, ValueError, TypeError):
        return


def record_tool_call_close(tool_call_id: object, *, omh_home: str = "", now: float | None = None) -> None:
    """post_tool_call: close the in-flight entry pre_tool_call opened, and
    record that this install has observed post_tool_call actually fire.

    The close itself is a no-op when there is nothing to close -- an already
    expired entry, or a host that never sent the matching pre_tool_call tick
    (or any tick at all, for a host that omits tool_call_id). But this
    function running at all is the evidence the HUD's activity block needs:
    ``_host_supports_hook`` skips registering post_tool_call on hosts whose
    ``VALID_HOOKS`` predates it, and on such a host this is simply never
    called. Recording that timestamp unconditionally -- even when there is
    no entry to close -- is what lets the reader tell "this host never
    fires post_tool_call, liveness is unanswerable" apart from "this host
    fires it and nothing happens to be open right now". Best-effort, same
    as ``record_tool_call``."""
    call_id = _normalized_id(tool_call_id)
    tick = float(now if now is not None else time.time())
    path = tool_bursts_path(omh_home)
    try:
        with _awareness_delivery_lock(path):
            record = _read_record(path)
            open_calls = _prune_expired_opens(record["open_calls"], now=tick)
            if call_id and call_id in open_calls:
                del open_calls[call_id]
            observed_at = max(tick, record["post_tool_call_observed_at"])
            _write_delivery_record(
                path,
                {
                    "schema_version": TOOL_BURSTS_SCHEMA_VERSION,
                    "entries": record["entries"],
                    "open_calls": open_calls,
                    # Carried through unchanged. Closing a call says nothing
                    # about whether the next one repeats it, and a writer
                    # that dropped this key would silently disarm the guard.
                    "repeat_streaks": _prune_stale_streaks(record["repeat_streaks"], now=tick),
                    "post_tool_call_observed_at": observed_at,
                },
            )
    except (OSError, ValueError, TypeError):
        return


def _read_record(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "entries": _sanitized_entries(raw),
        "open_calls": _sanitized_open_calls(raw),
        "repeat_streaks": _sanitized_repeat_streaks(raw),
        "post_tool_call_observed_at": _sanitized_observed_at(raw),
    }


def _sanitized_observed_at(raw: dict[str, Any]) -> float:
    value = raw.get("post_tool_call_observed_at")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _sanitized_entries(raw: dict[str, Any]) -> list[dict[str, Any]]:
    source = raw.get("entries")
    entries: list[dict[str, Any]] = []
    for item in source if isinstance(source, list) else []:
        if not isinstance(item, dict):
            continue
        tick = item.get("ts")
        tool = str(item.get("tool", "") or "")
        open_at_tick = item.get("open_at_tick")
        if isinstance(tick, (int, float)) and not isinstance(tick, bool) and tool:
            entries.append(
                {
                    "tool": tool[:_MAX_TOOL_NAME_CHARS],
                    "ts": float(tick),
                    "id": _normalized_id(item.get("id")),
                    "open_at_tick": (
                        int(open_at_tick)
                        if isinstance(open_at_tick, (int, float)) and not isinstance(open_at_tick, bool)
                        else 1
                    ),
                }
            )
    entries.sort(key=lambda entry: entry["ts"])
    return entries


def _sanitized_open_calls(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    source = raw.get("open_calls")
    open_calls: dict[str, dict[str, Any]] = {}
    for call_id, item in (source.items() if isinstance(source, dict) else ()):
        if not isinstance(item, dict):
            continue
        started_at = item.get("started_at")
        normalized_id = _normalized_id(call_id)
        if not normalized_id or not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
            continue
        open_calls[normalized_id] = {
            "tool": str(item.get("tool", "") or "")[:_MAX_TOOL_NAME_CHARS],
            "turn_id": _normalized_id(item.get("turn_id")),
            "started_at": float(started_at),
        }
    return open_calls


def _sanitized_repeat_streaks(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Rows a foreign or hand-edited file cannot turn into a false refusal.

    A row missing any of the four fields the gate compares is dropped
    rather than defaulted: a default would be this module inventing a
    repeat nobody observed, and the gate reads a dropped row as no streak,
    which allows.
    """
    source = raw.get("repeat_streaks")
    streaks: dict[str, dict[str, Any]] = {}
    for session, item in (source.items() if isinstance(source, dict) else ()):
        if not isinstance(item, dict):
            continue
        key = _normalized_id(session)
        tool = str(item.get("tool", "") or "")[:_MAX_TOOL_NAME_CHARS]
        digest = str(item.get("args_digest", "") or "")[:_MAX_DIGEST_CHARS]
        count = item.get("count")
        tick = item.get("ts")
        if not key or not tool or not digest:
            continue
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            continue
        if not isinstance(tick, (int, float)) or isinstance(tick, bool):
            continue
        streaks[key] = {"tool": tool, "args_digest": digest, "count": int(count), "ts": float(tick)}
    return streaks


def _prune_stale_streaks(streaks: dict[str, dict[str, Any]], *, now: float) -> dict[str, dict[str, Any]]:
    return {
        session: streak
        for session, streak in streaks.items()
        if now - float(streak["ts"]) <= REPEAT_STREAK_WINDOW_SECONDS
    }


def _advance_repeat_streak(
    streaks: dict[str, dict[str, Any]],
    *,
    session: str,
    tool: str,
    args_digest: str,
    now: float,
) -> dict[str, dict[str, Any]]:
    """Count this call against the session's consecutive-repeat streak.

    The row holds the LAST call only, so a call with a different tool or a
    different digest overwrites it at count 1. That is the whole of the
    self-clearing behavior: one different call disarms the guard, nothing
    has to expire for it to work, and a session cannot be left stuck
    because a count somewhere outlived the loop it described.

    No streak at all when the host named no session or the arguments
    produced no digest -- the gate needs both to identify a repeat, and a
    row it cannot key or compare would only ever be dead weight.
    """
    if not session or not args_digest:
        return streaks
    current = streaks.get(session)
    repeats = current is not None and current["tool"] == tool and current["args_digest"] == args_digest
    count = int(current["count"]) + 1 if repeats and current is not None else 1
    streaks[session] = {"tool": tool, "args_digest": args_digest, "count": count, "ts": now}
    return _cap_repeat_streaks(streaks)


def _cap_repeat_streaks(streaks: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if len(streaks) <= MAX_REPEAT_STREAKS:
        return streaks
    newest_first = sorted(streaks.items(), key=lambda item: item[1]["ts"], reverse=True)
    return dict(newest_first[:MAX_REPEAT_STREAKS])


def _prune_expired_opens(open_calls: dict[str, dict[str, Any]], *, now: float) -> dict[str, dict[str, Any]]:
    return {
        call_id: entry
        for call_id, entry in open_calls.items()
        if now - float(entry.get("started_at", now)) <= TOOL_CALL_OPEN_TTL_SECONDS
    }


def _cap_open_calls(open_calls: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if len(open_calls) <= MAX_OPEN_TOOL_CALLS:
        return open_calls
    newest_first = sorted(open_calls.items(), key=lambda item: item[1]["started_at"], reverse=True)
    return dict(newest_first[:MAX_OPEN_TOOL_CALLS])


def _iso(tick: float) -> str:
    return datetime.fromtimestamp(tick, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _latest_shot_from_entries(
    entries: list[dict[str, Any]],
    open_calls: dict[str, dict[str, Any]],
    *,
    now: float,
) -> dict[str, Any]:
    idle: dict[str, Any] = {"status": "idle"}
    if not entries:
        return idle
    groups: list[list[dict[str, Any]]] = [[entries[0]]]
    for entry in entries[1:]:
        if entry["ts"] - groups[-1][-1]["ts"] <= BURST_WINDOW_SECONDS:
            groups[-1].append(entry)
        else:
            groups.append([entry])
    latest = next((group for group in reversed(groups) if len(group) >= 2), None)
    if latest is None:
        return idle
    last_tick = latest[-1]["ts"]
    if now - last_tick > BURST_FRESH_SECONDS:
        return idle
    open_count = sum(1 for entry in latest if entry.get("id") and entry["id"] in open_calls)
    return {
        "status": "observed",
        "size": len(latest),
        "distinct_tools": len({entry["tool"] for entry in latest}),
        "observed_at": _iso(last_tick),
        # True in-flight split within this shot's own members, not the ring
        # ceiling: a batch of 40 that saturated the old ring could not tell
        # "still running" from "buffer full".
        "open_count": open_count,
        # NOT a completion claim -- naming it that contradicted
        # TOOL_CALL_OPEN_TTL_SECONDS's own rule that an expired open entry is
        # "never reported as completed (nothing observed it finishing)".
        # This is every member not currently open: a member with no
        # tool_call_id (host degraded to pre-pairing behavior) was never
        # opened, and an expired member's close was never observed either --
        # both land here as "not open", which is the only claim the data
        # backs, not "finished".
        "closed_or_unobserved_count": len(latest) - open_count,
        # The highest open_at_tick observed among this group's own members:
        # at least this many calls were open simultaneously at some point
        # during the shot. This is the group's actual measured concurrency,
        # not `size` -- `size` only proves the host dispatched these calls
        # inside the grouping window, which a long strictly-sequential chain
        # can satisfy just as well as a real parallel batch (Hermes caps
        # concurrent tool workers well under `size` in either case).
        "peak_open_count": max((entry.get("open_at_tick", 1) for entry in latest), default=0),
        "claim_boundary": TOOL_BURST_CLAIM_BOUNDARY,
    }


def _read_snapshot(omh_home: str, *, now: float) -> dict[str, Any]:
    """One ledger read, pruned to `now`. The single point every projection
    below builds from, so a poll that needs more than one projection (the
    HUD reader wants both the parallel-shot and the activity block) sees one
    consistent state instead of two reads that can straddle a concurrent
    writer and disagree about what is currently open."""
    record = _read_record(tool_bursts_path(omh_home))
    return {
        "entries": record["entries"],
        "open_calls": _prune_expired_opens(record["open_calls"], now=now),
        "post_tool_call_observed_at": record["post_tool_call_observed_at"],
    }


def _activity_from_snapshot(snapshot: dict[str, Any], shot: dict[str, Any], *, now: float) -> dict[str, Any]:
    open_calls = snapshot["open_calls"]
    count = len(open_calls)
    if count:
        oldest_id, oldest = min(open_calls.items(), key=lambda item: item[1]["started_at"])
        oldest_started_at = _iso(oldest["started_at"])
        oldest_elapsed_seconds: float | None = max(0.0, now - oldest["started_at"])
    else:
        oldest_started_at = ""
        oldest_elapsed_seconds = None
    return {
        "schema_version": TOOL_ACTIVITY_SCHEMA_VERSION,
        "open_call_count": count,
        "oldest_open_started_at": oldest_started_at,
        "oldest_open_elapsed_seconds": oldest_elapsed_seconds,
        "live": count > 0,
        # Whether this OMH install has ever seen post_tool_call actually
        # fire (record_tool_call_close ran at least once). False on a host
        # `_host_supports_hook` skipped registering post_tool_call for: the
        # ledger's open entries can only expire there, never legitimately
        # close, so `live`/`oldest_open_elapsed_seconds` cannot be trusted
        # either way and the HUD must render liveness as unanswerable
        # rather than inverting silence into a false stall.
        "post_tool_call_observed": snapshot["post_tool_call_observed_at"] > 0,
        "latest_shot": shot,
        "claim_boundary": TOOL_ACTIVITY_CLAIM_BOUNDARY,
    }


def latest_parallel_shot(omh_home: str = "", *, now: float | None = None) -> dict[str, Any]:
    """Project the most recent concurrent batch, or an idle marker."""
    current = float(now if now is not None else time.time())
    snapshot = _read_snapshot(omh_home, now=current)
    return _latest_shot_from_entries(snapshot["entries"], snapshot["open_calls"], now=current)


def tool_call_activity(omh_home: str = "", *, now: float | None = None) -> dict[str, Any]:
    """The HUD's liveness signal: exact open tool-call count plus the shot.

    ``live`` is true precisely while at least one tool call this OMH install
    saw open has not yet closed and has not expired -- it answers "is
    something actually running right now", the question the ring-buffer-only
    badge and a lingering active todo item could not.
    """
    current = float(now if now is not None else time.time())
    snapshot = _read_snapshot(omh_home, now=current)
    shot = _latest_shot_from_entries(snapshot["entries"], snapshot["open_calls"], now=current)
    return _activity_from_snapshot(snapshot, shot, now=current)


def tool_call_projection(omh_home: str = "", *, now: float | None = None) -> dict[str, Any]:
    """`{"parallel_shot": ..., "activity": ...}` from one ledger read.

    The HUD reader needs both projections every poll; calling
    ``latest_parallel_shot`` and ``tool_call_activity`` separately each reads
    the ledger file on its own, so a write landing between the two reads
    could hand the two blocks different snapshots of the same poll. This is
    the single-read equivalent of calling both.
    """
    current = float(now if now is not None else time.time())
    snapshot = _read_snapshot(omh_home, now=current)
    shot = _latest_shot_from_entries(snapshot["entries"], snapshot["open_calls"], now=current)
    return {"parallel_shot": shot, "activity": _activity_from_snapshot(snapshot, shot, now=current)}

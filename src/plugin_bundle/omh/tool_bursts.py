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
carrying only its last call's tool, argument digest, counts, and tick, so
the keyed part cannot accumulate per session beyond that single row.

What the streak is for, stated without overclaiming. It feeds two stages
at ``pre_tool_call``: a ``block``, whose message goes back to the model,
and then an ``approve``, which the host routes to the human-approval gate
instead. The evidence says the first stage may do nothing at all -- in the
session this was built from, a model read 185 of the host's own refusals
on one tool and 100+ on another and reissued the identical call every
time. So the claim here is detection and refusal, not that the loop ends:
one stage the model can ignore, one it cannot answer by itself, and a
count a later surface can show the person.
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
# Consecutive identical calls before `pre_tool_call` refuses the next one.
# Nothing here claims this refusal is stronger than the host's. It is not:
# a `block` becomes the tool result, exactly as Hermes' own guard's error
# did, and a model that ignores one can ignore the other the same way.
# Two reasons put it at 8 rather than at the host's own 4.
#
# First, Hermes already refuses a repeated `search_files` at the 4th
# identical call (`tools/file_tools.py`, `if count >= 4`, warning at the
# 3rd) and guards a repeated `read_file` region too. A second refusal at
# the same point is the same message in another voice, not a second
# mechanism. Engaging four calls later means engaging on evidence the
# host's refusal did not produce: that it was ignored.
#
# Second, and this is the binding reason, an identical repeated call is
# not always a loop. Polling is the legitimate case -- `terminal` running
# `gh pr checks <n>` back to back while waiting on CI has byte-identical
# arguments every time, and so does any status check -- and OMH cannot
# tell it from a loop, because the digest covers the ARGUMENTS and this
# module never sees a result. So the ladder has to leave room for a poll
# rather than price it as a fault: a short one passes under 8, a long one
# reaches a person at 12 who can answer `[a]lways`, and the digest-scoped
# rule key is what makes that answer cover exactly that poll.
#
# 8 is still far outside an immediate retry, which runs two or three
# times, and far short of the measured failure, which ran the same
# `search_files` call 203 times and one `read_file` region 100+ times.
REPEAT_CALL_BLOCK_THRESHOLD = 8
# Where the call stops going to the model at all and goes to a person.
# Derived, because the distance is the meaning: the call count freezes
# once stage one starts refusing, so four more consecutive attempts are
# exactly four stage-one blocks that were ignored. One ignored block can
# be a model recovering -- a retry already in flight, a message that
# crossed the refusal -- and two can be the same. Four is not: measured,
# session 20260919_140745_db409e ignored 185 of the host's refusals on
# `search_files` and 100+ on `read_file`. At that point the only response
# in the `pre_tool_call` contract a model cannot answer by itself is the
# human-approval gate.
REPEAT_CALL_APPROVAL_THRESHOLD = REPEAT_CALL_BLOCK_THRESHOLD + 4
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
# The `[a]lways` allowlist grain for an escalated call. Explicit, because
# the host derives one from the tool name plus a hash of the reason when
# this is empty (`tools/approval.py`, `request_tool_approval`), and this
# reason carries a count that changes every call -- which would mint a new
# key each time and re-prompt a person who already answered "always". Keyed
# by digest instead, an informed "always" covers exactly this one call with
# these arguments and nothing wider. The digest, never the arguments.
REPEAT_CALL_RULE_KEY_PREFIX = "omh_repeat"
REPEAT_CALL_CLAIM_BOUNDARY = (
    "A repeat streak counts consecutive identical calls this OMH install "
    "saw at pre_tool_call, keyed by session and compared by argument "
    "digest. It is evidence that the same call was attempted again, never "
    "evidence of what any call returned, and an intercepted call is not "
    "evidence of what the host or the person then did with it."
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


def _repeat_stage(ran: int, intercepted: int) -> str:
    """Which stage this streak is in. One rule, one place.

    The gate and the reader both call this, so a surface cannot render a
    stage the gate would not act on.

    The two counts are read on different rungs, which is the whole design.
    Calls that RAN decide whether to intervene at all: that many identical
    calls reached the host. Attempts INCLUDING the intercepted ones decide
    whether the intervention still goes to the model, because once stage
    one starts no further call runs, so a rung reading the call count
    alone would freeze there and nothing could ever escalate.
    """
    if ran + intercepted >= REPEAT_CALL_APPROVAL_THRESHOLD:
        return "approval"
    if ran >= REPEAT_CALL_BLOCK_THRESHOLD:
        return "blocking"
    return "watching"


def _repeat_block_message(tool_name: str, ran: int) -> str:
    """Stage one, addressed to the model. Says only what was measured.

    What was measured is the ARGUMENTS: this module digests them and never
    sees a result. So the message may say the same call was issued this
    many times in a row, and may not say the results were the same or that
    another one cannot differ. That claim is false for `terminal`, for
    `web_search`, for any status poll, and for `read_file` on a file
    another process is writing. The host's guard can say "the file has NOT
    changed" because it sits inside the tool and checks; this sits outside
    and does not.

    It also does not say "you already have this information", the host's
    wording in the measured session, which was false there for the
    opposite reason: the pattern named functions that do not exist, so the
    search returned nothing and there was nothing the model already had.

    Advice therefore branches, because the guard cannot tell a loop from a
    poll and both are worth acting on. A refusal that misdescribes the
    situation is one the model can be right to disagree with, and the
    measured session is what disagreeing looks like.
    """
    return (
        f"[OMH Repeat Guard] `{tool_name}` has been issued {ran} times in a "
        "row with exactly these arguments. OMH compares arguments, not "
        "results, so it cannot tell which of these you are doing. If the "
        "call keeps returning the same thing, including nothing, repeating "
        "it will not help: when nothing comes back, what you are looking "
        "for is not there under that name, so read the file directly or "
        "list what it does define. If you are waiting on something that "
        "changes, do other work between checks instead of re-issuing the "
        f"same call back to back.\n{REPEAT_CALL_BLOCK_SUFFIX}"
    )


def _repeat_approval_message(tool_name: str, ran: int, intercepted: int) -> str:
    """Stage two, addressed to a person, not the model.

    The host turns this into "Tool '<name>' requires approval (<this>)" at
    the same gate as a dangerous shell command, so it has to read as a
    question someone can answer without the session in front of them: what
    is being repeated, how far it has gone, and what each answer does.

    "Refused or escalated", not "refused": the first interventions were
    refusals, but once this prompt has been shown, later ones are
    escalations whose outcome a person decided and OMH never observes.
    Calling them all refusals would claim an answer this module does not
    have.

    It says nothing about the results either, for the reason given in
    `_repeat_block_message`: the digest covers arguments only, so "the
    result has not changed" would be a claim nothing here measured, and a
    person deciding whether to allow a poll is exactly who should not be
    told it.
    """
    return (
        f"repeat guard: this session has issued `{tool_name}` "
        f"{ran + intercepted} times in a row with identical arguments. The "
        f"first {ran} reached the tool; OMH has refused or escalated the "
        f"{intercepted} since, and the session issues the same call again "
        "after each one. OMH compares arguments only, so it cannot say "
        "whether the result is changing -- a stuck loop and a status poll "
        "look the same from here. Denying ends the repetition; allowing "
        "runs that same call once more."
    )


def repeat_call_rule_key(tool_name: str, args_digest: str) -> str:
    """The allowlist grain for an escalated call: one tool, one digest."""
    return f"{REPEAT_CALL_RULE_KEY_PREFIX}:{tool_name}:{args_digest}"


def repeat_call_directive(
    *,
    tool_name: object,
    args_digest: object,
    session_id: object = "",
    omh_home: str = "",
    now: float | None = None,
) -> dict[str, str] | None:
    """Intervene on a call this session keeps repeating, or None to allow.

    Two stages, because a refusal is only as strong as the model's
    willingness to read it, and the measured session read 185 of the
    host's and repeated the call anyway. Stage one returns ``block``: the
    message becomes the tool result, which a better-behaved model can act
    on and this one may ignore. Stage two returns ``approve``, which the
    host routes to the same human gate as a dangerous shell command
    (``tools/approval.py``, ``request_tool_approval``) -- it does not go
    back to the model as text at all, and in a context with no human to
    ask it fails closed.

    Read-only. ``record_tool_call`` counts the calls that dispatched and
    ``record_repeat_refusal`` counts the ones this guard intercepted, so
    the message can say how many times the call actually RAN while the
    stage ladder still advances on attempts that never ran.

    Every path out of the narrow positive case is ``None`` -- allow. The
    conjunction is: this session is identified, the arguments produced a
    digest, the session's last recorded call used the same tool and the
    same digest, that call is recent enough to still be the same loop, and
    the streak has reached a stage. A missing session id, an unreadable or
    absent ledger, a malformed row, and a path that will not resolve all
    land here as allow. That matters more at stage two than at stage one:
    an escalation in an unattended session waits for a human and then
    fails closed, which is right for a real loop and wrong for everything
    else, and #1674 is what a veto costs when it fires on a session it was
    never meant to touch.
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
    ran = int(streak["count"])
    intercepted = int(streak["intercepted"])
    stage = _repeat_stage(ran, intercepted)
    if stage == "approval":
        return {
            "action": "approve",
            "message": _repeat_approval_message(name, ran, intercepted),
            "rule_key": repeat_call_rule_key(name, digest),
        }
    if stage == "blocking":
        return {"action": "block", "message": _repeat_block_message(name, ran)}
    return None


def repeat_call_streak(
    omh_home: str = "",
    *,
    session_id: object = "",
    now: float | None = None,
) -> dict[str, Any]:
    """This session's current repeat streak, or an idle marker.

    The read side of the same row the gate reads, so a later surface can
    render `repeat xN` without reaching into the stored shape or
    re-deriving the stage rule. Adding that surface must not need a change
    here or in what is written.
    """
    current = float(now if now is not None else time.time())
    session = _normalized_id(session_id)
    idle: dict[str, Any] = {"status": "idle"}
    if not session:
        return idle
    try:
        record = _read_record(tool_bursts_path(omh_home))
    except (OSError, ValueError, TypeError, RuntimeError):
        return idle
    streak = _prune_stale_streaks(record["repeat_streaks"], now=current).get(session)
    if streak is None:
        return idle
    ran = int(streak["count"])
    intercepted = int(streak["intercepted"])
    return {
        "status": "observed",
        "tool": streak["tool"],
        "args_digest": streak["args_digest"],
        # Calls the host dispatched, and calls this guard did not let
        # through. Kept apart because only the first is evidence that a
        # call ran, and their sum is what the stage ladder reads.
        "ran": ran,
        "intercepted": intercepted,
        "consecutive": ran + intercepted,
        "stage": _repeat_stage(ran, intercepted),
        "observed_at": _iso(streak["ts"]),
        "claim_boundary": REPEAT_CALL_CLAIM_BOUNDARY,
    }


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


def record_repeat_refusal(
    *,
    tool_name: object,
    args_digest: object,
    session_id: object,
    omh_home: str = "",
    now: float | None = None,
) -> None:
    """Count one call the repeat guard did not let through.

    Separate from ``record_tool_call`` because the two facts are
    different, and collapsing them would cost the block message its
    honesty: that one records a call the host went on to dispatch, this
    one records a call it did not. Keeping them apart is what lets the
    message say how many times the call actually RAN while the stage
    ladder still advances on attempts nobody ran.

    Only ever updates a row that already matches this tool and digest. It
    creates none: a refusal can only follow a streak, so a missing row
    means the caller and the ledger disagree and the safe reading is that
    there is nothing to count. Best-effort, like every writer here.
    """
    name = _normalized_name(tool_name)
    session = _normalized_id(session_id)
    digest = str(args_digest or "")[:_MAX_DIGEST_CHARS]
    if not name or not session or not digest:
        return
    tick = float(now if now is not None else time.time())
    path = tool_bursts_path(omh_home)
    try:
        with _awareness_delivery_lock(path):
            record = _read_record(path)
            streaks = _prune_stale_streaks(record["repeat_streaks"], now=tick)
            current = streaks.get(session)
            if current is None or current["tool"] != name or current["args_digest"] != digest:
                return
            streaks[session] = {
                **current,
                "intercepted": int(current["intercepted"]) + 1,
                # The loop is still live, so the streak is too. Without
                # this a long enough sequence of refused calls would age
                # past the window and hand the model a fresh budget.
                "ts": tick,
            }
            _write_delivery_record(
                path,
                {
                    "schema_version": TOOL_BURSTS_SCHEMA_VERSION,
                    "entries": record["entries"],
                    "open_calls": _prune_expired_opens(record["open_calls"], now=tick),
                    "repeat_streaks": streaks,
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
        intercepted = item.get("intercepted")
        if not key or not tool or not digest:
            continue
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            continue
        if not isinstance(tick, (int, float)) or isinstance(tick, bool):
            continue
        streaks[key] = {
            "tool": tool,
            "args_digest": digest,
            "count": int(count),
            # The one field that defaults instead of dropping the row. A
            # row written before this field existed, or one hand-edited
            # without it, has observed no interceptions, and 0 delays the
            # next stage rather than inventing evidence for it.
            "intercepted": (
                int(intercepted)
                if isinstance(intercepted, int) and not isinstance(intercepted, bool) and intercepted >= 0
                else 0
            ),
            "ts": float(tick),
        }
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
    different digest overwrites it at count 1 with no interceptions. That
    is the whole of the self-clearing behavior, and it clears both stages
    at once: one different call disarms the guard, nothing has to expire
    for it to work, and a session cannot be left stuck at the approval
    stage because a count somewhere outlived the loop it described.

    No streak at all when the host named no session or the arguments
    produced no digest -- the gate needs both to identify a repeat, and a
    row it cannot key or compare would only ever be dead weight.
    """
    if not session or not args_digest:
        return streaks
    current = streaks.get(session)
    repeats = current is not None and current["tool"] == tool and current["args_digest"] == args_digest
    count = int(current["count"]) + 1 if repeats and current is not None else 1
    intercepted = int(current["intercepted"]) if repeats and current is not None else 0
    streaks[session] = {
        "tool": tool,
        "args_digest": args_digest,
        "count": count,
        "intercepted": intercepted,
        "ts": now,
    }
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

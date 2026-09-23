"""Whether the person asked for Jev in the current turn, recorded from their own words.

`omh_jev_ask` sends data off the machine, so it runs only when the human's
message for THIS turn names Jev. `pre_llm_call` is the one hook that receives
that message, once per turn, before the turn's tool calls; it records a
marker here and the tool reads it. A skill the model loaded on its own, a
bare "yes", or an earlier turn's request is not consent.

The rule is a fail-closed allow-gate on the person's text, not an inference
that stops work: a token equal to `jev`, a token that starts with `jev` and
continues with no Latin letter (`jev로`, `jev's`, `jev-1`), or an `omh-jev-*`
name sets the marker. Tokens split on anything that is not a letter or digit,
so `omh_jev_ask` counts as naming Jev, and a Latin letter after `jev` ends the
match, so `Jevons paradox` and `jevity` do not. The gate cannot tell a
negation apart -- "don't use jev" also sets the marker -- and the model can
still decline to call; that residual is documented rather than guessed around.

Only an attended platform can set it. `ATTENDED_PLATFORMS` is an allowlist of
the host platform ids where a person types the turn's message; every other id
-- `webhook`, `msgraph_webhook`, `api_server`, `batch`, `cron`, `subagent`, an
empty id, a plugin platform this list has not read -- records "not requested".
A single-query run (`HERMES_SINGLE_QUERY_SESSION=1`, Hermes `oneshot`, which
reaches the hook as platform `cli`) and a process started for a kanban task
(`HERMES_KANBAN_TASK`) are unattended too, as are a host-synthesized notice, a
tracker event, and a delegated child session.

The gate reads only the person's own text. Hermes assembles the user message
from host material and the person's words (`gateway/run_inbound.py`,
`_prepare_inbound_message_text`, read at hermes-agent origin/main 8fb0fc6ae6):

* host blocks prepended before the text, each closed by `]` and a blank line:
  `[Triggering message id: ...]` (Discord), `[Replying to...: "<quoted>"]`, a
  `[The user sent ...]` note per attachment, image, or voice clip;
* channel-history backfill, `<earlier messages>` then a blank line and
  `[New message]` on its own line before the text
  (`_prefix_inbound_sender_context`, on by default on Discord);
* in a shared multi-user session, a `[<display name>] ` sender prefix on the
  text itself;
* adapter-inlined material with no closing boundary: `[Content of <name>]:`
  file text and QQ's `[Quoted message]:` block;
* `@`-reference expansion, appended AFTER the person's text: Hermes runs
  `preprocess_context_references` over the whole assembled message, backfill
  included, and adds `--- Context Warnings ---` (a line per reference, each
  naming the reference) and `--- Attached Context ---` (fetched file, folder,
  git, or page content) on their own lines (`agent/context_references.py`,
  read at hermes-agent origin/main 6b6c7f4a99; the CLI and TUI turns run the
  same expansion). A backfilled `@file:jev...` from an unverified sender, or a
  page the person linked that says "call omh_jev_ask", lands there.

So the person's segment is found by cutting at the first expansion header
line, then after the last backfill separator or, without one, after the last
`]` plus blank line, and it counts only if no host-block opener and no other
bracketed line survives the cut; a message with inlined material records "not
requested" outright, because it cannot be split. The sender prefix is removed before matching. Every step can only
remove text, so a wrong cut fails closed.

On a messaging platform only the FIRST line of that segment counts, and a
turn that carries an attachment note (`[The user sent ...`) counts not at all.
Hermes merges inbound messages from different senders into the first
sender's event and keeps that sender's `user_id`: the text batcher appends
each chunk after a newline (`_append_text`; its key is the session, and
SimpleX keys it by chat, so a group's per-user session receives the others'
text too), and the busy-session pending slot appends captions and text the
same way (`merge_pending_message_event`, `gateway/platforms/base.py`, read at
hermes-agent origin/main f63c388e1a). A captionless photo takes another
sender's caption whole, so a media event's caption is nobody's certain words.
The cost is that an owner's "ask jev" on a second line, or as a photo
caption, is not consent on a messaging platform; a terminal surface has no
such merge and keeps every line.

In a shared multi-user session any participant can type "ask jev", so there
consent needs the session's owner: the `sender_id` Hermes passed on the turn
that opened the session. That turn is `is_first_turn` with a history holding
nothing but its own message. A turn-start compaction rotation also arrives as
`is_first_turn` on a new session id, but with the compacted history (the
host's `parent_session_id` there is the delegation parent, not the rotation's
parent, so ownership cannot be carried over); it records no owner, and a
recorded owner is never replaced. A turn from any other sender never sets
the marker, and a shared turn whose owner this process did not see -- a
restart mid-session, a rotated child, an evicted record -- sets none either.
A session is known to be shared when its text carries the sender prefix, or
when a sender other than the recorded owner speaks. Residuals: a shared
session on a platform that supplies no display name, first seen by this
process after a restart, cannot be told from a direct message; and a
participant who opens a fresh session with `/new` (open to every participant
unless the host's `allow_admin_from` is set) owns that session's consent.

The marker is bound to the turn that recorded it. `pre_llm_call` stores the
turn's `turn_id`; OMH's `pre_tool_call` arms the session with the `turn_id` of
each `omh_jev_ask` call; the tool proceeds only when the two match. A
background-review fork or a `/btw` side question shares the session id but runs
under its own `turn_id`, so it cannot spend the main turn's consent.

Process-local and bounded, like `session_attendance`: nothing about it is
written to disk.
"""

from __future__ import annotations

import os
import re
import threading
from collections import OrderedDict
from typing import Any, Final

MAX_TRACKED_SESSIONS: Final = 256
# `jev` at the start of a token, not followed by a Latin letter.
_JEV_TOKEN: Final = re.compile(r"(?<![^\W_])jev(?![a-z])", re.IGNORECASE)

_lock = threading.Lock()
# session -> (turn_id, requested)
_turn_markers: "OrderedDict[str, tuple[str, bool]]" = OrderedDict()
# session -> turn_id of the latest `omh_jev_ask` pre_tool_call
_armed_turns: "OrderedDict[str, str]" = OrderedDict()
# session -> sender_id recorded on the session's first turn
_session_owners: "OrderedDict[str, str]" = OrderedDict()

# Host platform ids where a person types the turn's message. Read from
# hermes-agent origin/main 8fb0fc6ae6: `Platform` in `gateway/config.py`, the
# bundled adapters under `plugins/platforms/`, and the local surfaces (`cli`,
# `tui` and `desktop` in `run_agent.py`, `acp` in `acp_adapter/session.py`).
# Deliberately absent: `webhook`, `msgraph_webhook`, `api_server` (a program's
# request), `email` (forwarded and quoted mail), `homeassistant` (automations),
# `local`, `relay`, `wecom_callback`, `a2a`, `buzz`, `raft`, `ntfy` (agents or
# publishers, not a person), and `cron`, `subagent`, `batch`, `curator`.
LOCAL_ATTENDED_PLATFORMS: Final = frozenset({"cli", "tui", "desktop", "acp"})
MESSAGING_ATTENDED_PLATFORMS: Final = frozenset(
    {
        "bluebubbles",
        "dingtalk",
        "discord",
        "feishu",
        "google_chat",
        "irc",
        "line",
        "matrix",
        "mattermost",
        "photon",
        "qqbot",
        "signal",
        "simplex",
        "slack",
        "sms",
        "teams",
        "telegram",
        "wecom",
        "weixin",
        "whatsapp",
        "whatsapp_cloud",
        "yuanbao",
    }
)
ATTENDED_PLATFORMS: Final = LOCAL_ATTENDED_PLATFORMS | MESSAGING_ATTENDED_PLATFORMS
KANBAN_TASK_ENV: Final = "HERMES_KANBAN_TASK"
SINGLE_QUERY_ENV: Final = "HERMES_SINGLE_QUERY_SESSION"

# Host blocks closed by `]` and a blank line before the person's text.
_HOST_BLOCK_OPENERS: Final = (
    "[Replying to",
    "[The user sent",
    "[Triggering message id:",
    "[If you need a closer look",
    "[Quoted message]",
)
_HOST_BLOCK_END: Final = "]\n\n"
# Notes Hermes writes for an attachment; the pending slot merges any sender's
# caption into such an event (`merge_pending_message_event`).
_MEDIA_NOTES: Final = ("[The user sent", "[If you need a closer look")
_BACKFILL_SEPARATOR: Final = "\n\n[New message]\n"
# Inlined material with no closing boundary: the generic note that says a
# file's content follows, every adapter's `[Content of <name>]:` header, and
# QQ's `[Quoted message]:` block, whose quoted text runs into the person's.
_UNSPLITTABLE_MARKERS: Final = (
    "Its content has been included below",
    "[Content of ",
    "[Quoted message]",
)
# Headers `agent/context_references.py` appends after the whole assembled
# message when it expands `@file:`/`@url:`/`@folder:`/git references.
_EXPANSION_HEADERS: Final = ("--- Context Warnings ---", "--- Attached Context ---")
# `[<display name>] ` at the start of the person's text in a shared session.
_SENDER_PREFIX: Final = re.compile(r"\[[^\]\n]*\] ")


def _person_segment(message: object, *, messaging: bool) -> tuple[str, bool]:
    """(the person's own text or "", whether a sender prefix was removed)."""
    text = str(message or "")
    if any(marker in text for marker in _UNSPLITTABLE_MARKERS):
        return "", False
    # Everything from the first `@`-reference expansion header on is host
    # material: warnings and fetched file or page content. Cut before any
    # other search, so a separator or block end inside fetched text is never
    # taken for the host's.
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.strip() in _EXPANSION_HEADERS:
            text = "\n".join(lines[:index])
            break
    if _BACKFILL_SEPARATOR in text:
        text = text.rsplit(_BACKFILL_SEPARATOR, 1)[1]
        # A separator spelled inside a quoted block is not the host's: cut
        # after any block end that follows it.
        cut = text.rfind(_HOST_BLOCK_END)
        if cut != -1:
            text = text[cut + len(_HOST_BLOCK_END):]
    elif any(opener in text for opener in _HOST_BLOCK_OPENERS):
        cut = text.rfind(_HOST_BLOCK_END)
        if cut == -1:
            return "", False
        text = text[cut + len(_HOST_BLOCK_END):]
    if any(opener in text for opener in (*_HOST_BLOCK_OPENERS, "[New message]")):
        return "", False
    prefixed = False
    if messaging:
        match = _SENDER_PREFIX.match(text)
        if match:
            text = text[match.end():]
            prefixed = True
    if any(line.lstrip().startswith("[") for line in text.splitlines()):
        # A bracketed host block this module does not recognize.
        return "", prefixed
    return text, prefixed


def person_text(message: object, *, messaging: bool = True) -> str:
    """The part of a host-assembled user message the person typed, or ""."""
    return _person_segment(message, messaging=messaging)[0]


def message_requests_jev(message: object, *, messaging: bool = True) -> bool:
    """Whether a person's message names Jev in the sense the gate accepts."""
    return bool(_JEV_TOKEN.search(person_text(message, messaging=messaging)))


def clear_turn(session_id: object) -> None:
    """Record "not requested" for the session; the hook calls this first on every turn."""
    note_turn(session_id, "")


def _unattended(platform: str, delegated: bool) -> bool:
    return (
        delegated
        or platform not in ATTENDED_PLATFORMS
        or bool(os.environ.get(KANBAN_TASK_ENV))
        or os.environ.get(SINGLE_QUERY_ENV) == "1"
    )


def _remember(table: "OrderedDict[str, Any]", key: str, value: Any) -> None:
    _ = table.pop(key, None)
    table[key] = value
    while len(table) > MAX_TRACKED_SESSIONS:
        # Oldest first; an evicted session reads as "not requested".
        _ = table.popitem(last=False)


def note_turn(
    session_id: object,
    request_message: object,
    *,
    delegated: bool = False,
    platform: object = "",
    turn_id: object = "",
    sender_id: object = "",
    is_first_turn: bool = False,
    history: object = None,
) -> None:
    """Record this turn's marker for the session, replacing the previous turn's.

    `history` is the hook's `conversation_history`; only a first turn whose
    history is at most its own message opens the session and names its owner.
    """
    key = str(session_id or "").strip()
    if not key:
        return
    platform_id = str(platform or "").strip().casefold()
    turn = str(turn_id or "").strip()
    sender = str(sender_id or "").strip()
    messaging = platform_id in MESSAGING_ATTENDED_PLATFORMS
    opens_session = is_first_turn and isinstance(history, (list, tuple)) and len(history) <= 1
    with _lock:
        if opens_session and sender and key not in _session_owners:
            _remember(_session_owners, key, sender)
        owner = _session_owners.get(key, "")
    requested = False
    if turn and not _unattended(platform_id, delegated):
        text, shared = _person_segment(request_message, messaging=messaging)
        if messaging and (owner or shared):
            # A shared session, or one whose owner is known: only the owner.
            owner_speaks = bool(owner) and sender == owner
        else:
            owner_speaks = True
        if messaging:
            if any(note in str(request_message or "") for note in _MEDIA_NOTES):
                # A media event's caption may be another sender's, merged whole.
                text = ""
            # Merged chunks from other senders follow a newline; only the
            # first line is certainly the event sender's own.
            text = text.split("\n", 1)[0]
        requested = owner_speaks and bool(_JEV_TOKEN.search(text))
    with _lock:
        _remember(_turn_markers, key, (turn, requested))
        _ = _armed_turns.pop(key, None)


def arm_tool_call(session_id: object, turn_id: object) -> None:
    """Record the turn an `omh_jev_ask` call is running in; `pre_tool_call` calls this."""
    key = str(session_id or "").strip()
    if not key:
        return
    with _lock:
        _remember(_armed_turns, key, str(turn_id or "").strip())


def consent_observed(session_id: object) -> bool:
    """True only when this session's current turn asked for Jev and the call runs in that turn."""
    key = str(session_id or "").strip()
    if not key:
        return False
    with _lock:
        turn, requested = _turn_markers.get(key, ("", False))
        armed = _armed_turns.get(key, "")
    return requested and bool(turn) and armed == turn


def reset_turn_markers() -> None:
    """Test seam: forget every recorded turn, arm, and owner."""
    with _lock:
        _turn_markers.clear()
        _armed_turns.clear()
        _session_owners.clear()


__all__ = [
    "ATTENDED_PLATFORMS",
    "LOCAL_ATTENDED_PLATFORMS",
    "MESSAGING_ATTENDED_PLATFORMS",
    "arm_tool_call",
    "clear_turn",
    "consent_observed",
    "message_requests_jev",
    "note_turn",
    "person_text",
    "reset_turn_markers",
]

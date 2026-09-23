"""Whether the person asked for Jev in the current turn, recorded from their own words.

`omh_jev_ask` sends data off the machine, so it runs only when the human's
message for THIS turn names Jev. `pre_llm_call` is the one hook that receives
that message, once per turn, before the turn's tool calls; it records a
marker here and the tool reads it. A skill the model loaded on its own, a
bare "yes", or an earlier turn's request is not consent.

The rule is a fail-closed allow-gate on the person's text, not an inference
that stops work: a token equal to `jev`, a token that starts with `jev`
(`jev로`, `jev's`), or an `omh-jev-*` name sets the marker. Tokens split on
anything that is not a letter or digit, so `omh_jev_ask` counts as naming
Jev. The gate cannot tell a negation apart -- "don't use jev" also sets the
marker -- and the model can still decline to call; that residual is
documented rather than guessed around.

Turns that carry no human request never set it:

* a host-synthesized notice or a tracker event arrives with an empty request;
* a delegated child session is skipped;
* a turn whose host platform is `cron` or `subagent` is skipped. Hermes cron
  runs an ordinary agent turn with the job prompt as its `user_message`
  (`cron/scheduler.py` builds the agent with `platform="cron"`), so a job
  prompt that names Jev -- which a model can write -- would otherwise count as
  consent on every unattended fire;
* a process started for a kanban task (`HERMES_KANBAN_TASK` is set) is
  skipped: a kanban worker's task body is written by a model, not by the
  person in this turn.

The gate reads only the person's own text. Hermes gateways prepend host
material to the message: a `[Replying to...: "<quoted>"]` block when the
person replies natively to a message (the bot's own offer included), a
`[The user sent ... document ...]` note per attachment, and some adapters
inline a text attachment's contents (`[Content of <name>]:`)
(`gateway/run_inbound.py`, `gateway/platforms/whatsapp_cloud.py`, read
2026-09-23). Without stripping, a bare "yes" replying to "reply `ask jev` to
send it" would carry the bot's "jev" into the gate. So only the text after the
last host block is read, which is always a suffix of what the person typed,
and a message with inlined attachment text records "not requested" outright,
because inlined file text cannot be told apart from the person's own words.
Both directions fail closed: a stripped prefix can only remove text.

Process-local and bounded, like `session_attendance`: the marker is a fact
about the running turn, and nothing about it is written to disk.
"""

from __future__ import annotations

import os
import re
import threading
from collections import OrderedDict
from typing import Final

MAX_TRACKED_SESSIONS: Final = 256
# `jev` at the start of a token: not preceded by a letter or digit.
_JEV_TOKEN: Final = re.compile(r"(?<![^\W_])jev", re.IGNORECASE)

_lock = threading.Lock()
_turn_markers: "OrderedDict[str, bool]" = OrderedDict()


# Host platforms whose turns have no person typing in them.
NON_INTERACTIVE_PLATFORMS: Final = frozenset({"cron", "subagent"})
KANBAN_TASK_ENV: Final = "HERMES_KANBAN_TASK"
# Every host block ends with `]` and a blank line before the person's text.
_HOST_BLOCK_OPENERS: Final = ("[Replying to", "[The user sent a", "[Triggering message id:")
_HOST_BLOCK_END: Final = "]\n\n"
# Inlined attachment text: the generic note that says the content follows,
# and the WhatsApp Cloud adapter's own inline header.
_INLINED_ATTACHMENT_MARKERS: Final = ("Its content has been included below", "[Content of ")


def person_text(message: object) -> str:
    """The part of a host-assembled user message the person typed, or "".

    Fail-closed on both paths: with a host block present, only the text after
    the last block end is returned (a suffix of the person's text); with
    inlined attachment text present, nothing is.
    """
    text = str(message or "")
    if any(marker in text for marker in _INLINED_ATTACHMENT_MARKERS):
        return ""
    if any(opener in text for opener in _HOST_BLOCK_OPENERS):
        cut = text.rfind(_HOST_BLOCK_END)
        if cut == -1:
            return ""
        return text[cut + len(_HOST_BLOCK_END):]
    return text


def message_requests_jev(message: object) -> bool:
    """Whether a person's message names Jev in the sense the gate accepts."""
    return bool(_JEV_TOKEN.search(person_text(message)))


def clear_turn(session_id: object) -> None:
    """Record "not requested" for the session; the hook calls this first on every turn."""
    note_turn(session_id, "")


def note_turn(
    session_id: object,
    request_message: object,
    *,
    delegated: bool = False,
    platform: object = "",
) -> None:
    """Record this turn's marker for the session, replacing the previous turn's."""
    key = str(session_id or "").strip()
    if not key:
        return
    unattended = (
        delegated
        or str(platform or "").strip().casefold() in NON_INTERACTIVE_PLATFORMS
        or bool(os.environ.get(KANBAN_TASK_ENV))
    )
    requested = (not unattended) and message_requests_jev(request_message)
    with _lock:
        _ = _turn_markers.pop(key, None)
        _turn_markers[key] = requested
        while len(_turn_markers) > MAX_TRACKED_SESSIONS:
            # Oldest first; an evicted session reads as "not requested".
            _ = _turn_markers.popitem(last=False)


def consent_observed(session_id: object) -> bool:
    key = str(session_id or "").strip()
    if not key:
        return False
    with _lock:
        return _turn_markers.get(key, False)


def reset_turn_markers() -> None:
    """Test seam: forget every recorded turn."""
    with _lock:
        _turn_markers.clear()


__all__ = [
    "NON_INTERACTIVE_PLATFORMS",
    "clear_turn",
    "consent_observed",
    "message_requests_jev",
    "note_turn",
    "person_text",
    "reset_turn_markers",
]

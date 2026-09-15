"""Per-turn reconciliation reminder for an open plan todo.

A model that declares a plan (todo init) and then answers "all done" in
chat while the checklist still shows open items leaves the HUD lying to
the user ('작업 다됐다는데 투두는 이렇게 남아있네'). No keyword trigger
can catch every phrasing of a completion claim, but an OPEN plan is a
state, not a phrasing — so while one exists, every turn's context
carries one compact line that binds completion claims to the checklist.
The reminder is awareness (instruction), never state and never
evidence; it stops the moment the plan is all done or cleared.

While the reader's stall finding stands, the same line also carries how
long the checklist has been unchanged. That covers the other half of the
failure: a plan does not only part ways with the session by being
contradicted, it parts ways by being left behind while the session talks
about something else.

The same turn-shaped gap exists one level out: a dispatched unit ends,
the session reports it, and the turn ends without the result being
verified or the plan updated. So the reminder also carries any finished
dispatch the plan has not written down (`dispatch_outcomes`), each with
the verb it owes, plus `DISPATCH_COMPLETION_RULE`. Same boundary: the
lines are instruction and a pointer at a record, never evidence that a
unit did anything.

The per-turn line has one structural limit: it is read at the start of a
turn, so nothing observes a turn that ENDS with open items, and continuation
then waits for the person. `plan_continuation_directive` is the same policy
delivered at the one moment a host offers -- `pre_verify`, where a returned
directive starts the next turn instead of describing the current one. Same
gate (`open_plan_position`), same rules, same boundary: it reports what the
plan record says and asserts nothing about what the next turn does.
"""
from __future__ import annotations

from typing import Any

from .dispatch_outcomes import unacknowledged_outcomes
from .runtime_reader import TODO_UNCHANGED_STATUSES, read_omh_todo, todo_unchanged_text

try:  # Match a continuation claim on the router's own fold when OMH is installed.
    from omh.routing.visual_qa_cues import contains_cue_phrase as _contains_cue_phrase
except ImportError:  # pragma: no cover - standalone plugin hosts keep the local fold.
    _contains_cue_phrase = None

_MAX_ACTIVE_TEXT_CHARS = 80
# The reminder is one compact block, not a report: three outcome lines plus a
# count of the rest is enough to make the event impossible to miss without
# turning the per-turn context into a dispatch board.
_MAX_OUTCOME_LINES = 3

# The TUI has shown a stopped checklist to the PERSON since the in-flight
# liveness signal landed; the session driving that checklist never saw the
# finding anywhere. A plan that sits with one item active for hours while the
# session answers about other things and ends its turns is the observed
# failure, and it is not a completion claim, so the rule below cannot catch
# it. This sentence states what the reader observed and asks for one sentence
# of accounting. It rides a turn that is already happening and starts
# nothing, which is exactly why it is a context line and not a driver.
TODO_UNCHANGED_RULE = (
    "The checklist standing still is an observation, not evidence that the work "
    "failed: say where the plan stands, and if nothing is blocking it, move it."
)

# The half that was missing. The reconciliation rule below guards a COMPLETION
# CLAIM -- it fires when the model says it is done while items are open. It has
# nothing to say about the far more common way a plan dies: the model answers
# whatever arrived, reports, and ends the turn with the list untouched. A real
# run spent 36 minutes that way, and an earlier one ended with 3 of 5 phases
# pending while every notification got a courteous status reply.
#
# `omh_todo` exists to carry a goal ACROSS turns. A checklist that only ever
# catches a contradiction is a detector, not a plan. So this line states the
# obvious thing nobody was saying: open items mean the work is not finished.
#
# The termination criterion is explicit and it is what keeps this bounded --
# this is a long loop with a stop condition, not an unbounded one. It ends when
# every item is done, or when an item is recorded blocked with its reason. It
# does not end because a turn happened to produce a paragraph.
TODO_CONTINUATION_RULE = (
    "Open items mean this plan is not finished. Unless something is blocking "
    "it, advance the next item in this turn rather than ending on a status "
    "report. This stops when every item is done or an item is recorded blocked "
    "with its reason -- not when a turn has produced an answer."
)

# What the turn-end directive adds to the rule above. The message arrives as a
# synthetic user turn, so it has to say what it is: a read of the plan record,
# never a claim that an item ran.
PLAN_CONTINUATION_BOUNDARY = (
    "This directive reports what the plan record says; it is not evidence that "
    "any item ran, passed, or was verified."
)

# How an item records a block. Multi-word on purpose: the token "blocked" alone
# also appears in ordinary item text ("unblock the release"), and a marker that
# matched it would let one sentence stop the plan's own continuation.
_BLOCKED_MARKERS = (
    "blocked:",
    "blocked -",
    "blocked by",
    "blocked on",
    "blocked until",
    "blocked because",
)

TODO_RECONCILIATION_RULE = (
    "Before claiming this work is finished, reconcile the checklist with "
    "omh_todo: mark completed items done, keep exactly one item active, and "
    "either finish the remaining items or say which stay open and why. A "
    "completion claim in chat while the HUD checklist shows open items is a "
    "visible contradiction. Todo updates are declarations, never execution "
    "evidence."
)

# The chain a finished dispatch owes. Written as an obligation for THIS turn
# because the failure it replaces was structurally polite: a status report,
# then the turn ended, and the unit's result sat unverified while the next
# item never started.
DISPATCH_COMPLETION_RULE = (
    "A finished dispatch is an event to act on in this turn: verify its "
    "result, record the outcome on the plan (done or blocked with reason), "
    "then run the recovery or the next item. Do not announce continuation "
    "you have not started."
)

# Closing phrasings that promise a next step. Matched only to ask whether the
# step was actually armed -- never to suppress the sentence.
CONTINUATION_CLAIM_PHRASES = (
    "계속 진행",
    "이어서 진행",
    "will continue",
    "continuing",
    "proceeding with",
)

CONTINUATION_CLAIM_FINDING = (
    "The previous turn announced a continuation but nothing resumed: the plan "
    "did not change, or a finished dispatch is still unacknowledged. Start the "
    "next step in this turn -- verify the finished result, record it on the "
    "plan, then dispatch or advance -- or say plainly that the work is stopped "
    "and why. A promise to continue is not a continuation."
)


def open_todo_reminder(
    *,
    omh_home: str = "",
    hermes_home: str = "",
    session_ref: str = "",
    outcomes: list[dict[str, Any]] | None = None,
) -> str:
    """The per-turn plan line, plus any dispatch outcome nobody wrote down.

    ``session_ref`` is the session whose turn is starting; its own plan is
    the one a completion claim must reconcile against, never another
    session's. ``outcomes`` lets a caller that already read them (the hook
    also needs the count for its honesty check) hand them in rather than
    making this scan the runtime a second time on the same turn.
    """
    lines: list[str] = []
    head = _open_plan_line(omh_home=omh_home, hermes_home=hermes_home, session_ref=session_ref)
    if head:
        lines.append(head)
    lines.extend(
        _dispatch_outcome_lines(
            unacknowledged_outcomes(omh_home, hermes_home, session_ref)
            if outcomes is None
            else outcomes
        )
    )
    return "\n".join(lines)


def open_plan_position(todo: dict[str, Any]) -> tuple[int, int] | None:
    """``(done, total)`` while this plan has open work, else ``None``.

    The single place the question "does this plan have open work" is decided.
    The per-turn context line and the turn-end continuation directive are the
    same policy read at two moments -- the line rides a turn that is already
    happening, the directive starts the next one -- so a second copy of this
    condition would let the two disagree about the same plan.
    """
    if todo.get("status") != "established":
        return None
    counts = todo.get("counts") if isinstance(todo.get("counts"), dict) else {}
    done = counts.get("done")
    total = counts.get("total")
    if not isinstance(done, int) or not isinstance(total, int) or total <= 0 or done >= total:
        return None
    return done, total


def next_open_item(todo: dict[str, Any]) -> str:
    """The item a continuation would advance: the active one, else the first pending."""
    items = todo.get("items") if isinstance(todo.get("items"), list) else []
    return _first_item_text(items, "active") or _first_item_text(items, "pending")


def _first_item_text(items: list[Any], state: str) -> str:
    return next(
        (
            str(item.get("text", ""))[:_MAX_ACTIVE_TEXT_CHARS]
            for item in items
            if isinstance(item, dict) and item.get("state") == state
        ),
        "",
    )


def records_blocked_reason(text: str) -> bool:
    """Whether an item records that it is blocked AND says by what.

    The plan schema has three item states (pending/active/done) and no blocked
    one, so an item records a block in its own text -- which is what
    ``TODO_CONTINUATION_RULE`` asks for: "an item is recorded blocked with its
    reason". The reason is what makes it a stop criterion, so a bare "blocked"
    with nothing after it does not qualify; the marker set is deliberately
    multi-word so ordinary prose about unblocking work does not match.
    """
    folded = str(text or "").casefold()
    for marker in _BLOCKED_MARKERS:
        index = folded.find(marker)
        if index < 0:
            continue
        if folded[index + len(marker) :].strip(" -:.·"):
            return True
    return False


def plan_continuation_directive(
    *, omh_home: str = "", hermes_home: str = "", session_ref: str = ""
) -> str:
    """The turn-end message for a session whose plan still has open work.

    ``TODO_CONTINUATION_RULE`` is already the right sentence delivered at the
    wrong moment: ``_open_plan_line`` renders it into the context of a turn
    that is already happening, so a turn that ends with open items ends
    anyway. This is that rule at the one moment a host lets a plugin start the
    next turn instead. Empty whenever the plan itself says stop -- no plan, a
    finished plan, or a next item recorded blocked with its reason -- so the
    directive never argues with the plan's own stop criterion.
    """
    try:
        todo = read_omh_todo(omh_home or None, hermes_home or None, session_ref=session_ref)
    except (OSError, ValueError, TypeError):
        # Same boundary the outcome reader keeps: a reminder that could fail
        # the turn it decorates would be worse than a missing one. Here it is
        # also the difference between two indistinguishable outcomes: Hermes
        # wraps the whole `pre_verify` call in `except Exception` and logs at
        # debug, so a handler that raised would end the turn silently -- the
        # exact symptom this directive exists to fix. `RuntimeBindingError`
        # subclasses `ValueError`, so an unbindable home lands here too.
        return ""
    if not isinstance(todo, dict):
        return ""
    position = open_plan_position(todo)
    item = next_open_item(todo) if position is not None else ""
    if item and records_blocked_reason(item):
        return ""
    lines: list[str] = []
    if position is not None:
        done, total = position
        head = f"[OMH plan todo] {done}/{total} done"
        if item:
            head = f"{head} · next: {item}"
        lines.append(f"{head}. {TODO_CONTINUATION_RULE}")
    lines.extend(_dispatch_outcome_lines(unacknowledged_outcomes(omh_home, hermes_home, session_ref)))
    if not lines:
        return ""
    lines.append(PLAN_CONTINUATION_BOUNDARY)
    return "\n".join(lines)


def _open_plan_line(*, omh_home: str, hermes_home: str, session_ref: str) -> str:
    todo = read_omh_todo(omh_home or None, hermes_home or None, session_ref=session_ref)
    position = open_plan_position(todo)
    if position is None:
        return ""
    done, total = position
    items = todo.get("items") if isinstance(todo.get("items"), list) else []
    active = _first_item_text(items, "active")
    head = f"[OMH plan todo] {done}/{total} done"
    if active:
        head = f"{head} · active: {active}"
    unchanged = todo_unchanged_text(todo)
    if not unchanged:
        return f"{head}. {TODO_CONTINUATION_RULE} {TODO_RECONCILIATION_RULE}"
    stall = todo.get("stall") if isinstance(todo.get("stall"), dict) else {}
    # "no tool call in flight" is only true of the quiet finding. The busy one
    # is the more interesting reading and saying the wrong one would make the
    # line refutable on its face.
    observed = (
        "unchanged {age} while calls kept running".format(age=unchanged)
        if stall.get("status") == "unchanged_while_busy"
        else "unchanged {age}, no tool call in flight".format(age=unchanged)
    )
    return (
        f"{head} · {observed}. "
        f"{TODO_CONTINUATION_RULE} {TODO_RECONCILIATION_RULE} {TODO_UNCHANGED_RULE}"
    )


def _dispatch_outcome_lines(outcomes: list[dict[str, Any]]) -> list[str]:
    if not outcomes:
        return []
    lines = [
        "dispatch {run_ref}/{unit_id} ended {state}; next: {verb}".format(
            run_ref=outcome.get("run_ref", "unknown"),
            unit_id=outcome.get("unit_id", "unknown"),
            state=_outcome_state(outcome),
            verb=outcome.get("next", "verify_result"),
        )
        for outcome in outcomes[:_MAX_OUTCOME_LINES]
    ]
    remaining = len(outcomes) - len(lines)
    if remaining > 0:
        lines.append(f"(+{remaining} more)")
    lines.append(DISPATCH_COMPLETION_RULE)
    return lines


def _outcome_state(outcome: dict[str, Any]) -> str:
    for key in ("unit_state", "failure_kind", "status"):
        value = str(outcome.get(key, "") or "")
        if value:
            return value
    return "unknown"


def continuation_claim_without_resume(
    text: str, *, todo_stall_status: str, unacknowledged: int
) -> str | None:
    """A finding when a turn promises to continue and nothing was started.

    "계속 진행하겠습니다" with no auto-resume armed is the exact closing the
    incident produced. The claim itself is fine; what makes it a finding is
    the state it was made in -- the plan unchanged, or a finished dispatch
    still unacknowledged. Either one is enough, because either one means the
    promised step did not start. Pure: the caller supplies both states, so
    this never reads a file and never decides on its own that a run is stuck.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    # The reader owns which statuses mean "the checklist stopped moving"; a
    # copy of that pair here would let a third one be added there without this
    # guard ever noticing.
    stalled = str(todo_stall_status or "") in TODO_UNCHANGED_STATUSES
    outstanding = isinstance(unacknowledged, int) and unacknowledged > 0
    if not stalled and not outstanding:
        return None
    if not _claims_continuation(text):
        return None
    return CONTINUATION_CLAIM_FINDING


def _claims_continuation(text: str) -> bool:
    if _contains_cue_phrase is not None:
        return bool(_contains_cue_phrase(text, CONTINUATION_CLAIM_PHRASES))
    folded = text.casefold()
    return any(phrase.casefold() in folded for phrase in CONTINUATION_CLAIM_PHRASES)

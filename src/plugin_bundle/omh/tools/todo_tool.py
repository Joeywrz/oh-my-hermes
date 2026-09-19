from __future__ import annotations

from .. import runtime_paths

import json
from typing import Any

from ..host_observation import (
    OBSERVATION_SCHEMA,
    attach_public_observation,
    host_session_id,
    observe_plugin_tool_call,
)
from ..runtime_reader import default_omh_home, read_omh_todo
from ..todo_store import (
    TODO_CLAIM_BOUNDARY,
    TODO_ITEM_STATES,
    TodoContendedError,
    TodoStoreError,
    TodoValidationError,
    advance_todo_item,
    build_todo_record,
    clear_todo,
    write_todo,
)

OMH_TODO_SCHEMA = {
    "name": "omh_todo",
    "description": (
        "Declare, advance, clear, or read the metadata-only plan todo list that OMH HUD surfaces render "
        "above the Hermes prompt input. The list belongs to the session that declares it: "
        "another TUI, Slack, or Discord session neither sees nor overwrites it. "
        "Initialize it BEFORE starting engine work (todo init): "
        "declare numbered phases in delivery order (e.g. 'I. Bootstrap' through 'VI. Evidence "
        "and Cleanup') that cover the whole lifecycle — setup, one implement/verify/deliver "
        "task per work unit, independent review lanes, and an evidence-and-cleanup close — "
        "with one task per observable outcome, so the run walks a bounded checklist instead "
        "of an open-ended reasoning loop. Keep exactly one item active and update states as "
        "work completes with action=advance; action=set replaces the whole list. "
        "Todo items are plan declarations, never execution evidence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["set", "advance", "clear", "show"],
                "description": (
                    "set writes a new todo list, advance changes one item's state on it, "
                    "clear removes it, show reads the current projection. Change a state "
                    "with advance, not set: set replaces the whole list."
                ),
            },
            "title": {
                "type": "string",
                "description": "Optional short plan title shown in the todo panel header.",
            },
            "deferred_reason": {
                "type": "string",
                "description": (
                    "Omit this field. Send it with action=set or action=advance "
                    "only when the PERSON "
                    "redirected this session away from the plan ('do Y first', "
                    "'forget that for now'), naming what they asked for instead. "
                    "While it holds, the plan stops asking you to advance the next "
                    "item, so the session serves the person without the checklist "
                    "arguing. It CLEARS ITSELF: it is stored with a digest of the "
                    "item list you send it with, and a reader honours it only while "
                    "that digest still matches. So resuming the plan costs no clearing "
                    "step -- just omit this field on your next write, which is the "
                    "default, and any item change that does not re-send it ends the "
                    "deferral too. Sending it again alongside a CHANGED item list "
                    "declares a new deferral for that list, so send it again only if "
                    "the person is still steering. Do not reach for an item's "
                    "blocked_reason instead: blocked means an item CANNOT PROCEED, is "
                    "per-item, and has to be removed by hand. An item that genuinely "
                    "cannot proceed still carries blocked_reason, and that reading "
                    "wins over this one."
                ),
            },
            "item": {
                "type": "integer",
                "minimum": 1,
                "description": "For action=advance: which item to change, 1 for the first.",
            },
            "item_text": {
                "type": "string",
                "description": (
                    "For action=advance: the start of that item's current text, guarding "
                    "against a stale index. A mismatch is refused."
                ),
            },
            "state": {
                "type": "string",
                "enum": list(TODO_ITEM_STATES),
                "description": "For action=advance: the item's new state.",
            },
            "blocked_reason": {
                "type": "string",
                "description": (
                    "For action=advance: items[].blocked_reason below, for the item being "
                    "changed; omitting it clears one."
                ),
            },
            "items": {
                "type": "array",
                "description": "Todo items for action=set, in display order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Item text."},
                        "state": {
                            "type": "string",
                            "enum": ["pending", "active", "done"],
                            "description": "Item state. Defaults to pending.",
                        },
                        "phase": {
                            "type": "string",
                            "description": (
                                "Optional phase label, numbered in delivery order (e.g. "
                                "'I. Bootstrap', 'II. Wave One Delivery'). Items sharing a "
                                "phase render as one section; the HUD shows the current "
                                "phase's checklist."
                            ),
                        },
                        "blocked_reason": {
                            "type": "string",
                            "description": (
                                "Omit this field. Send it only for an item that CANNOT "
                                "proceed, naming what it is waiting on (a review, an "
                                "approval, a missing credential, another item). Any value "
                                "here stops the plan advancing past this item, so an item "
                                "that is merely unstarted, slow, or mid-work carries no "
                                "blocked_reason -- and neither does one whose text happens to "
                                "discuss blocking. Remove the field once the thing it names "
                                "arrives. Set or clear it with action=advance, or through "
                                "action=set like any other item edit, which replaces the "
                                "whole list: send every item back, or the ones you leave "
                                "out are dropped."
                            ),
                        },
                        "depth": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 3,
                            "description": (
                                "Optional subtask nesting level (0 = top-level task, 1-3 = "
                                "subtasks rendered indented beneath the preceding shallower "
                                "item). Subtasks may omit phase; they continue their parent's "
                                "section."
                            ),
                        },
                    },
                    "required": ["text"],
                },
            },
            "omh_home": {
                "type": "string",
                "description": "Standalone operator override for action=show only; set, advance and clear reject overrides. Native Hermes calls reject this field; omit it to use the active profile.",
            },
            "observation": OBSERVATION_SCHEMA,
        },
        "required": ["action"],
    },
}


def omh_todo_handler(args: dict[str, Any], **kwargs) -> str:
    if error := runtime_paths.tool_home_error(args):
        return json.dumps(error, sort_keys=True)
    observation = observe_plugin_tool_call("omh_todo", args, kwargs)
    # Hermes passes its stable session/thread id as a keyword on every tool
    # call, so a plan declared in chat is stored for, and read back for, the
    # session that declared it. A host that supplies none leaves this empty:
    # the record is the home-wide one, exactly like a CLI write.
    session_ref = host_session_id(kwargs)
    home_arg = str(args.get("omh_home", "") or "")
    action = str(args.get("action", ""))
    payload: dict[str, Any] = {
        "schema_version": "omh_todo_result/v1",
        "action": action,
        "claim_boundary": TODO_CLAIM_BOUNDARY,
    }
    # Mutations bind to the environment-configured home only: a caller-chosen
    # path would turn this metadata tool into an arbitrary-location
    # file-create/delete primitive.
    if action in {"set", "advance", "clear"} and home_arg:
        payload["status"] = "invalid_todo"
        payload["error"] = "omh_home override is read-only; set and clear use the configured OMH home"
        payload["todo"] = read_omh_todo(session_ref=session_ref)
        return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
    if action == "set":
        try:
            record = build_todo_record(
                args.get("title", ""),
                args.get("items"),
                source="omh_todo",
                session_ref=session_ref,
                deferred_reason=args.get("deferred_reason", ""),
            )
            write_todo(default_omh_home(), record)
            payload["status"] = "written"
        # Before the generic branch, and it is not a nicety. `invalid_todo`
        # tells a writer its payload was wrong, and a writer that believes
        # that rewrites the list it just sent -- the whole-list rewrite this
        # action exists to stop -- when the record is simply busy for a few
        # milliseconds and the same call would land.
        except TodoContendedError as error:
            payload["status"] = "contended"
            payload["error"] = str(error)
        except (TodoValidationError, TodoStoreError) as error:
            payload["status"] = "invalid_todo"
            payload["error"] = str(error)
    elif action == "advance":
        # Same store call, same validator, same stamp as `set` above: the
        # single-item path is a narrower way to reach one write, never a
        # second way to write a record.
        try:
            _ = advance_todo_item(
                default_omh_home(),
                item=args.get("item"),
                item_text=args.get("item_text", ""),
                state=args.get("state", ""),
                source="omh_todo",
                session_ref=session_ref,
                blocked_reason=args.get("blocked_reason", ""),
                deferred_reason=args.get("deferred_reason", ""),
            )
            payload["status"] = "written"
        except TodoContendedError as error:
            payload["status"] = "contended"
            payload["error"] = str(error)
        except (TodoValidationError, TodoStoreError) as error:
            payload["status"] = "invalid_todo"
            payload["error"] = str(error)
    elif action == "clear":
        try:
            payload["status"] = (
                "cleared" if clear_todo(default_omh_home(), session_ref) else "already_absent"
            )
        except TodoStoreError as error:
            payload["status"] = "invalid_todo"
            payload["error"] = str(error)
    elif action == "show":
        payload["status"] = "read"
    else:
        payload["status"] = "invalid_action"
        payload["error"] = "action must be set, advance, clear, or show"
    payload["todo"] = read_omh_todo(runtime_paths.plugin_home(home_arg), session_ref=session_ref)
    return json.dumps(attach_public_observation(payload, observation), sort_keys=True)

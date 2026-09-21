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
from ..route_answer_store import (
    ANSWERED_BY_VALUES,
    CLAIM_BOUNDARY,
    RouteAnswerContendedError,
    RouteAnswerStoreError,
    RouteAnswerValidationError,
    build_route_answer_record,
    write_route_answer,
)
from ..runtime_reader import default_omh_home

OMH_ROUTE_ANSWER_SCHEMA = {
    "name": "omh_route_answer",
    "description": (
        "Record an answer to the route_question OMH attached to an undecidable route "
        "(omh_interact route.route_question, or omh chat route-hint). Recording an answer "
        "changes no route and dispatches nothing: the deterministic route stays in force "
        "either way, and the record exists so the answer can be scored against OMH's own "
        "routing corpora with `omh chat route-questions score`. Answer the two question "
        "kinds separately: the Choice picks which workflow fits best among the options, "
        "the yes/no fit questions each judge one workflow on its own. A recorded answer is "
        "a routing judgment the caller declares, never execution, review, CI, or merge evidence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question_digest": {
                "type": "string",
                "description": (
                    "The question_digest carried by the route_question block being answered. "
                    "It joins this answer to the exact question it answers; an answer whose "
                    "digest names a different question is refused rather than recorded."
                ),
            },
            "answered_by": {
                "type": "string",
                "enum": list(ANSWERED_BY_VALUES),
                "description": (
                    "Who produced this answer. main_model means you answered it yourself "
                    "while reading the question, and the confidence recorded beside it is "
                    "self-reported. jev_plugin means an installed Jev-class decision plugin "
                    "produced it and OMH is recording a number it did not observe. OMH never "
                    "calls such a plugin; naming it here is a statement about where the "
                    "answer came from."
                ),
            },
            "route_choice": {
                "type": "string",
                "description": (
                    "The option the Choice answers with: one workflow name from the "
                    "question's options, or `none` when the request asks for no OMH workflow."
                ),
            },
            "choice_probabilities": {
                "type": "object",
                "description": (
                    "Optional per-option probability for the Choice, keyed by option name, "
                    "each between 0 and 1. Omit it when the answerer produced none; an "
                    "invented number is worse than an absent one."
                ),
            },
            "fits": {
                "type": "object",
                "description": (
                    "The yes/no fit answers, keyed by workflow name, each a probability "
                    "between 0 and 1 that the request asks for the work that workflow does. "
                    "Judge each workflow on its own: these are absolute judgments and carry "
                    "no relation to the Choice, so fitting nothing while naming a Choice is "
                    "a legal answer and is recorded as it stands. The strongest fit decides "
                    "the recorded action against the question's own thresholds."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "Optional: the request the question was built from. It is used once, to "
                    "re-derive the question and confirm the digest, and is never stored. "
                    "Without it the record says digest_verified is false."
                ),
            },
            "source": {
                "type": "string",
                "enum": ["generic", "discord", "slack", "telegram", "hermes"],
                "description": (
                    "Chat surface the request arrived on, used only to re-derive the "
                    "question when a message is supplied for digest verification."
                ),
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional one-line reason for the answer, stored with the record for a "
                    "human reading it later."
                ),
            },
            "observation": OBSERVATION_SCHEMA,
        },
        "required": ["question_digest", "answered_by", "route_choice"],
    },
}

_RESULT_SCHEMA_VERSION = "omh_route_answer_result/v1"
_DEFAULT_SOURCE = "hermes"


def omh_route_answer_handler(args: dict[str, Any], **kwargs) -> str:
    if error := runtime_paths.tool_home_error(args):
        return json.dumps(error, sort_keys=True)
    observation = observe_plugin_tool_call("omh_route_answer", args, kwargs)
    payload: dict[str, Any] = {
        "schema_version": _RESULT_SCHEMA_VERSION,
        "claim_boundary": CLAIM_BOUNDARY,
    }
    # The write binds to the environment-configured home only. A caller-chosen
    # path would turn a metadata recorder into an arbitrary-location
    # file-create primitive, which is the same refusal `omh_todo` makes.
    if str(args.get("omh_home", "") or ""):
        return _result(
            payload,
            observation,
            status="invalid_request",
            error="omh_home override is not accepted; the record is written to the configured OMH home",
        )
    # Hermes passes its session id as a keyword on every dispatch. It keys the
    # record together with the question digest, so two questions answered in
    # one session are two records rather than one overwriting the other.
    session_ref = host_session_id(kwargs)
    verified, verification, mismatch = _verify_digest(args)
    if mismatch:
        return _result(
            payload,
            observation,
            status="digest_mismatch",
            error=(
                "question_digest does not match the route question for this message; "
                "nothing was recorded. Re-read route.route_question and answer that digest."
            ),
            digest_verification=verification,
        )
    try:
        record = build_route_answer_record(
            question_digest=args.get("question_digest"),
            answered_by=args.get("answered_by"),
            route_choice=args.get("route_choice"),
            fits=args.get("fits"),
            choice_probabilities=args.get("choice_probabilities"),
            note=args.get("note", ""),
            session_ref=session_ref,
            digest_verified=verified,
        )
    except RouteAnswerValidationError as error:
        return _result(payload, observation, status="invalid_request", error=str(error))
    try:
        write_route_answer(default_omh_home(), record)
    except RouteAnswerContendedError as error:
        # Before the generic branch: `invalid_request` tells a caller its
        # payload was wrong, and a caller that believes that rewrites an
        # answer that was simply racing another writer for a few milliseconds.
        return _result(payload, observation, status="contended", error=str(error))
    except RouteAnswerStoreError as error:
        return _result(payload, observation, status="store_unavailable", error=str(error))
    payload["record"] = record
    return _result(payload, observation, status="recorded", digest_verification=verification)


def _verify_digest(args: dict[str, Any]) -> tuple[bool, str, bool]:
    """(verified, verification, mismatch) for the supplied digest.

    Verification needs the core router, which this bundle may be running
    without -- Hermes loads this directory with its own interpreter. The
    import is therefore lazy and guarded, and its absence degrades this one
    field rather than the call: an unverified record is still a record the
    scorer can read, while a mismatch is refused because a record joined to
    the wrong question measures the wrong thing.

    One message produces more than one question, because the shortlist the
    question is built from is cut to the caller's candidate limit and the
    digest is the shortlist's. The surfaces that carry the question disagree
    on that limit already -- `omh chat route` and `omh_interact` ask for
    three candidates, `omh chat route-hint` for two -- so verifying against a
    single limit would refuse an answer to a question OMH itself handed out.
    Every reachable question is therefore re-derived, over the closed range
    the handoff's own `MAX_CANDIDATES` bounds, and a mismatch means the digest
    names a question this message cannot produce at any of them.
    """
    message = str(args.get("message") or "").strip()
    digest = str(args.get("question_digest") or "").strip()
    if not message or not digest:
        return False, "not_requested", False
    source = str(args.get("source") or _DEFAULT_SOURCE)
    try:
        from omh.routing.candidate_handoff import MAX_CANDIDATES
        from omh.routing.chat import route_chat_message
    except ModuleNotFoundError as exc:
        if exc.name != "omh":
            raise
        return False, "unavailable_without_package_backend", False
    reachable: set[str] = set()
    for limit in range(1, MAX_CANDIDATES + 1):
        try:
            route = route_chat_message(message, source=source, limit=limit)
        except (ValueError, KeyError):
            # An unsupported source or a message the router refuses is the
            # caller's input, not a verdict about the digest.
            return False, "message_not_routable", False
        question = route.get("route_question")
        if isinstance(question, dict):
            reachable.add(str(question.get("question_digest") or ""))
    reachable.discard("")
    if not reachable:
        return False, "no_route_question_for_message", False
    if digest not in reachable:
        return False, "mismatch", True
    return True, "matched", False


def _result(
    payload: dict[str, Any],
    observation: dict[str, Any] | None,
    *,
    status: str,
    error: str = "",
    digest_verification: str = "",
) -> str:
    payload["status"] = status
    if error:
        payload["error"] = error
    if digest_verification:
        payload["digest_verification"] = digest_verification
    return json.dumps(attach_public_observation(payload, observation), sort_keys=True)

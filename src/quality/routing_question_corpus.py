"""The two shipped routing corpora, projected as typed questions and scored.

`src/quality/routing_precision.py` holds the corpora OMH's deterministic router
is measured against, and it measures one arm: the router itself. This module
turns the same cases into a question any answerer can answer -- one relative
Choice over the router's own candidate shortlist plus `none`, and one absolute
yes/no per candidate -- so a second arm can be measured on exactly the cases
the first one is gated on.

Two rules hold this together, and both exist because breaking either publishes
a false number about OMH's own router:

- **Expected answers come from the corpora, never from a rule restated here.**
  A negative control expects no workflow; an intervention case expects what its
  own record says it expects, including the one case whose correct answer is
  "do not open a workflow".
- **The deterministic arm's verdicts come from the producer's evaluators.**
  `precision_case_verdict` and `intervention_case_verdict` are the accessors,
  and the deterministic arm must reproduce a zero-overroute, zero-missed
  reading: `routing_precision_errors(build_routing_precision_demo(...)) == []`
  is the authority, and a test pins that the two agree. A re-derived
  over-route predicate is the specific way this goes wrong, because `clarify`
  with a named candidate is a pass in the negative corpus and the expected
  intervention in the positive one.

Scoring is offline and deterministic. Nothing here calls a model, reads a
credential, or reaches the network; answers arrive as rows somebody else
produced.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..ingress import CHAT_SOURCES
from ..routing.route_question import (
    FITS_CLARIFY_THRESHOLD,
    FITS_DISPATCH_THRESHOLD,
    FIT_QUESTION_PREFIX,
    NO_WORKFLOW_OPTION,
    ROUTE_CHOICE_KEY,
    build_route_question_from_candidates,
    clean_skill_description,
    fit_question_skill,
)
from ..skills.catalog import builtin_definitions
from .reported_rate import reported_rate
from .routing_precision import (
    ROUTING_INTERVENTION_CASES,
    ROUTING_PRECISION_CASES,
    ROUTING_PRECISION_SCHEMA_VERSION,
    intervention_case_interaction,
    intervention_case_verdict,
    precision_case_interaction,
    precision_case_verdict,
)

ROUTING_QUESTION_CORPUS_SCHEMA_VERSION = "routing_question_corpus/v1"
ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION = "routing_question_answers/v1"
ROUTING_QUESTION_SCORE_SCHEMA_VERSION = "routing_question_score/v1"
ROUTE_QUESTION_ANSWER_SCHEMA_VERSION = "route_question_answer/v1"

NEGATIVE_CONTROL_CORPUS = "negative_control"
INTERVENTION_CORPUS = "intervention"

DETERMINISTIC_ARM = "deterministic"
UNKNOWN_ARM = "unknown"

# The action vocabulary an answer set resolves to. `none` covers both "no
# workflow applies" and the router's own `fallback`, which is the same
# outcome under a different name.
DISPATCH_ACTION = "dispatch"
CLARIFY_ACTION = "clarify"
NONE_ACTION = "none"

_CORPUS_CLAIM_BOUNDARY = (
    "These items are prepared questions over the two shipped routing corpora. "
    "An exported corpus is not a measurement, and an answer recorded against "
    "it is a routing judgment by whoever answered, not execution, review, CI, "
    "or merge evidence."
)

_SCORE_CLAIM_BOUNDARY = (
    "Every count is over the cases this arm actually answered; unanswered and "
    "malformed rows are excluded and named, never counted as correct. The "
    "deterministic arm's over-route verdict is the routing-precision corpus's "
    "own, so it reports what `omh chat routing-precision` reports. `agree` is "
    "an exact match on both questions and is not a pass metric: the router "
    "answers `clarify` on negative controls where asking one question is the "
    "correct non-hijacking behaviour, and those count as disagreements here "
    "while staying passes there. An arm's score describes these corpora at "
    "this revision and nothing beyond them."
)


class RoutingQuestionCorpusError(ValueError):
    """A corpus or answer source that cannot be read as what it claims to be."""


@dataclass(frozen=True)
class AnswerRecord:
    """One answer set as it arrived, with where it came from kept attached."""

    ref: str
    arm: str
    case_id: str
    question_digest: str
    answers: dict[str, Any]
    error: str = ""


def _skill_descriptions() -> dict[str, str]:
    return {definition.name: definition.description for definition in builtin_definitions()}


def _candidates_from_route(
    route: Mapping[str, Any],
    descriptions: Mapping[str, str],
    *,
    limit: int,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    recommendations = route.get("recommendations")
    if not isinstance(recommendations, list):
        return rows
    for entry in recommendations:
        if len(rows) >= limit:
            break
        if not isinstance(entry, Mapping):
            continue
        skill = str(entry.get("skill") or "").strip()
        if not skill:
            continue
        rows.append({"skill": skill, "description": clean_skill_description(descriptions.get(skill, ""))})
    return rows


def _deterministic_reading(route: Mapping[str, Any]) -> tuple[str, str]:
    """The router's own answer to the same two questions.

    `fallback` is reported as `none`: the router declined to open a workflow,
    which is the outcome the `none` option exists to express.
    """
    action = str(route.get("action") or "")
    if action == DISPATCH_ACTION:
        return DISPATCH_ACTION, str(route.get("selected_skill") or NO_WORKFLOW_OPTION)
    if action == CLARIFY_ACTION:
        return CLARIFY_ACTION, str(route.get("candidate_skill") or NO_WORKFLOW_OPTION)
    return NONE_ACTION, NO_WORKFLOW_OPTION


def _expected_for_intervention(case: Any) -> dict[str, str]:
    """The expected answer a single intervention case already carries.

    `dispatch` names its workflow. `clarify` names its candidate when the case
    pins one, and `none` when it does not -- the corpus does not pin a skill
    there, so neither does this. `fallback` is an intervention case whose
    correct answer is to open nothing, so it maps to `none` on both fields.
    """
    expected_action = str(case.expected_route_action or "")
    if expected_action == DISPATCH_ACTION:
        return {"action": DISPATCH_ACTION, "choice": str(case.expected_workflow or NO_WORKFLOW_OPTION)}
    if expected_action == CLARIFY_ACTION:
        return {"action": CLARIFY_ACTION, "choice": str(case.expected_candidate or NO_WORKFLOW_OPTION)}
    return {"action": NONE_ACTION, "choice": NO_WORKFLOW_OPTION}


def build_routing_question_corpus(*, source: str = "discord", limit: int = 3) -> dict[str, object]:
    """Project both shipped routing corpora into typed questions.

    `limit` caps how many of the router's own recommendations become Choice
    options; it does not change how a case is routed, so the verdicts below
    stay the producer's. No case is added to either corpus here: this is a
    projection, and the corpora's exact-count pins are the reason it has to
    stay one.
    """
    if source not in CHAT_SOURCES:
        raise RoutingQuestionCorpusError(f"unsupported corpus source: {source}")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise RoutingQuestionCorpusError("corpus limit must be a positive integer")
    descriptions = _skill_descriptions()
    items: list[dict[str, object]] = []
    for case in ROUTING_PRECISION_CASES:
        interaction = precision_case_interaction(case, source=source)
        route = interaction.get("route")
        route = route if isinstance(route, Mapping) else {}
        verdict = precision_case_verdict(case, interaction, source=source)
        action, choice = _deterministic_reading(route)
        items.append(
            _corpus_item(
                case_id=case.id,
                corpus=NEGATIVE_CONTROL_CORPUS,
                message=case.message,
                route=route,
                descriptions=descriptions,
                limit=limit,
                expected={"action": NONE_ACTION, "choice": NO_WORKFLOW_OPTION},
                deterministic={
                    "action": action,
                    "choice": choice,
                    "overrouted": bool(verdict["overrouted"]),
                    "case_passed": bool(verdict["passed"]),
                },
            )
        )
    for case in ROUTING_INTERVENTION_CASES:
        interaction = intervention_case_interaction(case, source=source)
        route = interaction.get("route")
        route = route if isinstance(route, Mapping) else {}
        verdict = intervention_case_verdict(case, interaction, source=source)
        action, choice = _deterministic_reading(route)
        items.append(
            _corpus_item(
                case_id=case.id,
                corpus=INTERVENTION_CORPUS,
                message=case.message,
                route=route,
                descriptions=descriptions,
                limit=limit,
                expected=_expected_for_intervention(case),
                deterministic={
                    "action": action,
                    "choice": choice,
                    # An intervention case is one the router is supposed to act
                    # on, so the negative corpus's over-route reading is not
                    # defined for it; `case_passed` is this corpus's verdict.
                    "overrouted": False,
                    "case_passed": bool(verdict["passed"]),
                },
            )
        )
    return {
        "schema_version": ROUTING_QUESTION_CORPUS_SCHEMA_VERSION,
        "source": source,
        "generated_from": {
            "schema": ROUTING_PRECISION_SCHEMA_VERSION,
            "case_count": len(ROUTING_PRECISION_CASES),
            "intervention_case_count": len(ROUTING_INTERVENTION_CASES),
        },
        "question_contract": {
            "choice_key": ROUTE_CHOICE_KEY,
            "fit_prefix": FIT_QUESTION_PREFIX,
            "none_option": NO_WORKFLOW_OPTION,
            "thresholds": {
                "fits_dispatch": FITS_DISPATCH_THRESHOLD,
                "fits_clarify": FITS_CLARIFY_THRESHOLD,
            },
        },
        "items": items,
        "claim_boundary": _CORPUS_CLAIM_BOUNDARY,
    }


def _corpus_item(
    *,
    case_id: str,
    corpus: str,
    message: str,
    route: Mapping[str, Any],
    descriptions: Mapping[str, str],
    limit: int,
    expected: Mapping[str, str],
    deterministic: Mapping[str, object],
) -> dict[str, object]:
    candidates = _candidates_from_route(route, descriptions, limit=limit)
    reason = str(route.get("reason") or "")
    question = build_route_question_from_candidates(
        candidates,
        reasons=(reason,) if reason else (),
    )
    return {
        "case_id": case_id,
        "corpus": corpus,
        "message": message,
        "candidates": candidates,
        "question": question,
        "expected": dict(expected),
        "deterministic": dict(deterministic),
    }


def corpus_shape_errors(corpus: object) -> tuple[str, ...]:
    """Return why a value is not a readable routing-question corpus."""
    if not isinstance(corpus, Mapping):
        return ("corpus is not an object",)
    errors: list[str] = []
    if corpus.get("schema_version") != ROUTING_QUESTION_CORPUS_SCHEMA_VERSION:
        errors.append("corpus schema_version is not " + ROUTING_QUESTION_CORPUS_SCHEMA_VERSION)
    items = corpus.get("items")
    if not isinstance(items, list) or not items:
        errors.append("corpus carries no items")
        return tuple(errors)
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            errors.append(f"item {index} is not an object")
            continue
        if not str(item.get("case_id") or ""):
            errors.append(f"item {index} has no case_id")
        if not isinstance(item.get("expected"), Mapping):
            errors.append(f"item {index} has no expected answer")
        if not isinstance(item.get("question"), Mapping):
            errors.append(f"item {index} has no question block")
    return tuple(errors)


# --- answers -----------------------------------------------------------------


def _fit_values(answers: Mapping[str, Any]) -> tuple[dict[str, float], list[str]]:
    fits: dict[str, float] = {}
    problems: list[str] = []
    for key, value in answers.items():
        skill = fit_question_skill(str(key))
        if not skill:
            continue
        if not isinstance(value, Mapping):
            problems.append(f"malformed_fit_answer:{skill}")
            continue
        raw = value.get("noul")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            problems.append(f"malformed_fit_answer:{skill}")
            continue
        number = float(raw)
        if not 0.0 <= number <= 1.0:
            problems.append(f"fit_answer_out_of_range:{skill}")
            continue
        fits[skill] = number
    return fits, problems


def parse_answer_row(
    row: object,
    *,
    ref: str,
    arm_default: str = "",
    digest_default: str = "",
) -> AnswerRecord:
    """Read one `routing_question_answers/v1` row, keeping why it failed.

    `arm_default` and `digest_default` are what a wrapping record already
    stated: a recorded answer names its answerer and the question digest on
    the record, so the row it embeds does not have to repeat either.
    """
    blank = AnswerRecord(ref=ref, arm=UNKNOWN_ARM, case_id="", question_digest="", answers={})
    if not isinstance(row, Mapping):
        return _failed(blank, "row is not an object")
    if row.get("schema_version") != ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION:
        # A row whose schema is not this one has no field worth trusting,
        # including the arm it names, so it is reported under `unknown` rather
        # than charged to an arm that may not have written it. It is still
        # counted and still named by its reference.
        return _failed(blank, "schema_version is not " + ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION)
    arm = str(row.get("arm") or arm_default or "").strip()
    case_id = str(row.get("case_id") or "").strip()
    digest = str(row.get("question_digest") or digest_default or "").strip()
    record = AnswerRecord(ref=ref, arm=arm or UNKNOWN_ARM, case_id=case_id, question_digest=digest, answers={})
    if not arm:
        return _failed(record, "row names no arm")
    if not case_id and not digest:
        return _failed(record, "row names neither case_id nor question_digest")
    answers = row.get("answers")
    if not isinstance(answers, Mapping):
        return _failed(record, "row carries no answers object")
    choice_answer = answers.get(ROUTE_CHOICE_KEY)
    if not isinstance(choice_answer, Mapping) or not str(choice_answer.get("choice") or "").strip():
        return _failed(record, f"row carries no {ROUTE_CHOICE_KEY} answer")
    _, problems = _fit_values(answers)
    if problems:
        return _failed(record, "; ".join(sorted(problems)))
    return AnswerRecord(
        ref=ref,
        arm=arm,
        case_id=case_id,
        question_digest=digest,
        answers=dict(answers),
    )


def _failed(record: AnswerRecord, reason: str) -> AnswerRecord:
    return AnswerRecord(
        ref=record.ref,
        arm=record.arm or UNKNOWN_ARM,
        case_id=record.case_id,
        question_digest=record.question_digest,
        answers={},
        error=reason,
    )


def read_answer_rows_from_jsonl(path: Path) -> list[AnswerRecord]:
    """Read a JSONL answer file, naming every line that could not be read."""
    records: list[AnswerRecord] = []
    text = Path(path).read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        ref = f"{Path(path).name}:{number}"
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError as exc:
            records.append(
                AnswerRecord(
                    ref=ref,
                    arm=UNKNOWN_ARM,
                    case_id="",
                    question_digest="",
                    answers={},
                    error=f"line is not JSON: {exc.msg}",
                )
            )
            continue
        records.append(parse_answer_row(row, ref=ref))
    return records


def read_answer_records_from_directory(path: Path) -> list[AnswerRecord]:
    """Read a directory of `route_question_answer/v1` records.

    Each record embeds one answer row under `answer` and carries the digest of
    the question it answered, which is how a recorded answer joins back to a
    corpus item that was exported separately.
    """
    records: list[AnswerRecord] = []
    for entry in sorted(Path(path).glob("*.json")):
        ref = entry.name
        try:
            document = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            records.append(
                AnswerRecord(
                    ref=ref,
                    arm=UNKNOWN_ARM,
                    case_id="",
                    question_digest="",
                    answers={},
                    error=f"record is not readable JSON: {exc}",
                )
            )
            continue
        records.append(_record_from_document(document, ref=ref))
    return records


def _record_from_document(document: object, *, ref: str) -> AnswerRecord:
    blank = AnswerRecord(ref=ref, arm=UNKNOWN_ARM, case_id="", question_digest="", answers={})
    if not isinstance(document, Mapping):
        return _failed(blank, "record is not an object")
    if document.get("schema_version") != ROUTE_QUESTION_ANSWER_SCHEMA_VERSION:
        return _failed(blank, "schema_version is not " + ROUTE_QUESTION_ANSWER_SCHEMA_VERSION)
    answered_by = str(document.get("answered_by") or "").strip()
    return parse_answer_row(
        document.get("answer"),
        ref=ref,
        arm_default=answered_by,
        digest_default=str(document.get("question_digest") or "").strip(),
    )


def read_answer_source(path: Path) -> list[AnswerRecord]:
    """Read either a JSONL answer file or a directory of recorded answers."""
    target = Path(path)
    if target.is_dir():
        return read_answer_records_from_directory(target)
    if not target.exists():
        raise RoutingQuestionCorpusError(f"answer source not found: {target}")
    return read_answer_rows_from_jsonl(target)


# --- scoring -----------------------------------------------------------------


def resolve_answer_action(
    answers: Mapping[str, Any],
    *,
    fits_dispatch: float,
    fits_clarify: float,
) -> tuple[str, str]:
    """Resolve one answer set into an (action, choice) pair.

    The yes/no answers decide WHETHER: the strongest fit against the two flat
    thresholds picks dispatch, clarify, or nothing. The Choice decides WHICH,
    and it is taken as given. The two are separate questions with no invariant
    between them, so an answer set can say "this fits strongly" while choosing
    `none`; that is reported as it stands rather than repaired here, and it
    scores as naming no workflow.
    """
    choice_answer = answers.get(ROUTE_CHOICE_KEY)
    choice = ""
    if isinstance(choice_answer, Mapping):
        choice = str(choice_answer.get("choice") or "").strip()
    choice = choice or NO_WORKFLOW_OPTION
    fits, _ = _fit_values(answers)
    if fits:
        strongest = max(fits.values())
        if strongest >= fits_dispatch:
            return DISPATCH_ACTION, choice
        if strongest >= fits_clarify:
            return CLARIFY_ACTION, choice
        return NONE_ACTION, choice
    if choice != NO_WORKFLOW_OPTION:
        return DISPATCH_ACTION, choice
    return NONE_ACTION, choice


@dataclass
class _ArmTally:
    answered: int = 0
    malformed: int = 0
    overroute: int = 0
    missed: int = 0
    wrong_workflow: int = 0
    correct_workflow: int = 0
    band_mismatch: int = 0
    agree: int = 0
    no_dispatch_denominator: int = 0
    intervention_denominator: int = 0
    workflow_denominator: int = 0


def _tally_item(
    tally: _ArmTally,
    item: Mapping[str, Any],
    *,
    action: str,
    choice: str,
    overrouted: bool | None,
) -> None:
    expected = item.get("expected")
    expected = expected if isinstance(expected, Mapping) else {}
    expected_action = str(expected.get("action") or NONE_ACTION)
    expected_choice = str(expected.get("choice") or NO_WORKFLOW_OPTION)
    tally.answered += 1
    if action == expected_action and choice == expected_choice:
        tally.agree += 1
    if expected_action == NONE_ACTION:
        tally.no_dispatch_denominator += 1
        # `overrouted` is supplied only for the deterministic arm, where the
        # verdict is the routing-precision corpus's own. Every other arm is
        # read from its answers: a dispatch on a case that expects no workflow
        # is the over-route; a clarify is not, because asking one question
        # hijacks nothing.
        over = overrouted if overrouted is not None else action == DISPATCH_ACTION
        if over:
            tally.overroute += 1
        return
    tally.intervention_denominator += 1
    missed = action == NONE_ACTION
    if missed:
        tally.missed += 1
    if expected_choice != NO_WORKFLOW_OPTION:
        tally.workflow_denominator += 1
        if choice == expected_choice:
            tally.correct_workflow += 1
            if not missed and action != expected_action:
                tally.band_mismatch += 1
        else:
            tally.wrong_workflow += 1


def _arm_payload(tally: _ArmTally, *, case_count: int) -> dict[str, object]:
    excluded = ("unanswered_cases", "malformed_answer_rows")
    return {
        "answered": tally.answered,
        "unanswered": max(case_count - tally.answered, 0),
        "malformed": tally.malformed,
        "overroute": tally.overroute,
        "missed": tally.missed,
        "wrong_workflow": tally.wrong_workflow,
        "band_mismatch": tally.band_mismatch,
        "agree": tally.agree,
        "overroute_rate": reported_rate(
            numerator=tally.overroute,
            denominator=tally.no_dispatch_denominator,
            numerator_of=("overroute",),
            denominator_of="answered cases that expect no workflow",
            excluded=excluded,
        ).to_payload(),
        "missed_rate": reported_rate(
            numerator=tally.missed,
            denominator=tally.intervention_denominator,
            numerator_of=("missed_intervention",),
            denominator_of="answered intervention cases that expect a workflow",
            excluded=excluded,
        ).to_payload(),
        "workflow_accuracy": reported_rate(
            numerator=tally.correct_workflow,
            denominator=tally.workflow_denominator,
            numerator_of=("correct_workflow",),
            denominator_of="answered intervention cases that name an expected workflow",
            excluded=excluded,
        ).to_payload(),
    }


def score_routing_question_answers(
    corpus: Mapping[str, Any],
    answer_records: Iterable[AnswerRecord] = (),
    *,
    thresholds: Mapping[str, float] | None = None,
) -> dict[str, object]:
    """Score every arm present in `answer_records`, plus the deterministic arm.

    The deterministic arm is computed from the corpus itself and is always
    present, so any other arm is read next to the router it would replace.

    The three counts answer three different questions and none of them is a
    summary of the others. `overroute` is over cases that expect no workflow.
    `missed` is over cases that expect one and were answered with none.
    `workflow_accuracy` reads the Choice answer alone: it says whether the arm
    named the right workflow, not whether it was confident enough to act, so a
    case can be both a miss and a correct workflow.
    """
    errors = corpus_shape_errors(corpus)
    if errors:
        raise RoutingQuestionCorpusError("; ".join(errors))
    contract = corpus.get("question_contract")
    contract = contract if isinstance(contract, Mapping) else {}
    declared = contract.get("thresholds")
    declared = declared if isinstance(declared, Mapping) else {}
    fits_dispatch = _threshold(thresholds, declared, "fits_dispatch", FITS_DISPATCH_THRESHOLD)
    fits_clarify = _threshold(thresholds, declared, "fits_clarify", FITS_CLARIFY_THRESHOLD)
    if fits_clarify > fits_dispatch:
        raise RoutingQuestionCorpusError("fits_clarify cannot exceed fits_dispatch")

    items = [item for item in corpus["items"] if isinstance(item, Mapping)]
    by_case = {str(item.get("case_id")): item for item in items}
    by_digest: dict[str, Mapping[str, Any]] = {}
    for item in items:
        question = item.get("question")
        digest = str(question.get("question_digest") or "") if isinstance(question, Mapping) else ""
        if digest:
            by_digest.setdefault(digest, item)

    tallies: dict[str, _ArmTally] = {}
    seen: dict[tuple[str, str], str] = {}
    malformed: list[dict[str, str]] = []
    unmatched: list[dict[str, str]] = []
    for record in answer_records:
        tally = tallies.setdefault(record.arm or UNKNOWN_ARM, _ArmTally())
        if record.error:
            tally.malformed += 1
            malformed.append({"ref": record.ref, "arm": record.arm, "reason": record.error})
            continue
        item = by_case.get(record.case_id) if record.case_id else None
        if item is None and record.question_digest:
            item = by_digest.get(record.question_digest)
        if item is None:
            unmatched.append(
                {
                    "ref": record.ref,
                    "arm": record.arm,
                    "case_id": record.case_id,
                    "question_digest": record.question_digest,
                }
            )
            continue
        key = (record.arm, str(item.get("case_id")))
        if key in seen:
            # The first answer for a case stands; a second is reported rather
            # than dropped, because silently keeping one of two disagreeing
            # answers is how an arm scores better than it answered.
            tally.malformed += 1
            malformed.append(
                {
                    "ref": record.ref,
                    "arm": record.arm,
                    "reason": f"duplicate answer for {item.get('case_id')}, first was {seen[key]}",
                }
            )
            continue
        seen[key] = record.ref
        action, choice = resolve_answer_action(
            record.answers,
            fits_dispatch=fits_dispatch,
            fits_clarify=fits_clarify,
        )
        _tally_item(tally, item, action=action, choice=choice, overrouted=None)

    deterministic = _ArmTally()
    for item in items:
        reading = item.get("deterministic")
        reading = reading if isinstance(reading, Mapping) else {}
        _tally_item(
            deterministic,
            item,
            action=str(reading.get("action") or NONE_ACTION),
            choice=str(reading.get("choice") or NO_WORKFLOW_OPTION),
            overrouted=bool(reading.get("overrouted")),
        )

    case_count = len(items)
    arms = {name: _arm_payload(tally, case_count=case_count) for name, tally in sorted(tallies.items())}
    arms[DETERMINISTIC_ARM] = _arm_payload(deterministic, case_count=case_count)
    return {
        "schema_version": ROUTING_QUESTION_SCORE_SCHEMA_VERSION,
        "source": str(corpus.get("source") or ""),
        "case_count": case_count,
        "thresholds": {"fits_dispatch": fits_dispatch, "fits_clarify": fits_clarify},
        "arms": arms,
        "malformed": malformed,
        "unmatched": unmatched,
        "claim_boundary": _SCORE_CLAIM_BOUNDARY,
    }


def _threshold(
    override: Mapping[str, float] | None,
    declared: Mapping[str, Any],
    key: str,
    default: float,
) -> float:
    for source in (override or {}, declared):
        if key in source:
            value = source[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RoutingQuestionCorpusError(f"{key} must be a number")
            if not 0.0 <= float(value) <= 1.0:
                raise RoutingQuestionCorpusError(f"{key} must fall between 0 and 1")
            return float(value)
    return default


def format_routing_question_corpus(corpus: Mapping[str, Any]) -> str:
    """A compact human summary of an exported corpus."""
    generated = corpus.get("generated_from")
    generated = generated if isinstance(generated, Mapping) else {}
    items = corpus.get("items")
    items = items if isinstance(items, list) else []
    negative = sum(1 for item in items if isinstance(item, Mapping) and item.get("corpus") == NEGATIVE_CONTROL_CORPUS)
    intervention = sum(1 for item in items if isinstance(item, Mapping) and item.get("corpus") == INTERVENTION_CORPUS)
    lines = [
        f"Routing question corpus ({corpus.get('schema_version')}) from {generated.get('schema')}",
        f"Source: {corpus.get('source')}",
        f"Items: {len(items)} ({negative} negative-control, {intervention} intervention)",
        f"Boundary: {corpus.get('claim_boundary', '')}",
    ]
    return "\n".join(lines)


def format_routing_question_score(score: Mapping[str, Any]) -> str:
    """A compact human summary of a score report."""
    arms = score.get("arms")
    arms = arms if isinstance(arms, Mapping) else {}
    lines = [
        f"Routing question score ({score.get('schema_version')}) over {score.get('case_count')} cases",
    ]
    for name in sorted(arms):
        arm = arms[name]
        if not isinstance(arm, Mapping):
            continue
        lines.append(
            f"- {name}: answered {arm.get('answered')}, unanswered {arm.get('unanswered')}, "
            f"malformed {arm.get('malformed')}, overroute {arm.get('overroute')}, "
            f"missed {arm.get('missed')}, wrong workflow {arm.get('wrong_workflow')}, "
            f"band mismatch {arm.get('band_mismatch')}, agree {arm.get('agree')}"
        )
    malformed = score.get("malformed")
    if isinstance(malformed, list) and malformed:
        lines.append(f"Malformed rows: {len(malformed)}")
        for entry in malformed[:10]:
            if isinstance(entry, Mapping):
                lines.append(f"- {entry.get('ref')}: {entry.get('reason')}")
    unmatched = score.get("unmatched")
    if isinstance(unmatched, list) and unmatched:
        lines.append(f"Unmatched answer records: {len(unmatched)}")
    lines.append(f"Boundary: {score.get('claim_boundary', '')}")
    return "\n".join(lines)


def routing_question_score_errors(score: Mapping[str, Any]) -> list[str]:
    """Return why a score report is not a clean deterministic-arm reading."""
    errors: list[str] = []
    if score.get("schema_version") != ROUTING_QUESTION_SCORE_SCHEMA_VERSION:
        errors.append("unexpected_schema")
    arms = score.get("arms")
    arms = arms if isinstance(arms, Mapping) else {}
    arm = arms.get(DETERMINISTIC_ARM)
    if not isinstance(arm, Mapping):
        errors.append("deterministic_arm_missing")
        return errors
    if int(arm.get("overroute", 0) or 0):
        errors.append(f"deterministic_overroute: {arm.get('overroute')}")
    if int(arm.get("missed", 0) or 0):
        errors.append(f"deterministic_missed: {arm.get('missed')}")
    if int(arm.get("malformed", 0) or 0):
        errors.append(f"deterministic_malformed: {arm.get('malformed')}")
    return errors


def answer_records_from_rows(rows: Sequence[Mapping[str, Any]], *, ref_prefix: str = "row") -> list[AnswerRecord]:
    """Parse in-memory rows, keeping each one's position as its reference."""
    return [parse_answer_row(row, ref=f"{ref_prefix}:{index}") for index, row in enumerate(rows, start=1)]


__all__ = [
    "CLARIFY_ACTION",
    "DETERMINISTIC_ARM",
    "DISPATCH_ACTION",
    "INTERVENTION_CORPUS",
    "NEGATIVE_CONTROL_CORPUS",
    "NONE_ACTION",
    "ROUTE_QUESTION_ANSWER_SCHEMA_VERSION",
    "ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION",
    "ROUTING_QUESTION_CORPUS_SCHEMA_VERSION",
    "ROUTING_QUESTION_SCORE_SCHEMA_VERSION",
    "AnswerRecord",
    "RoutingQuestionCorpusError",
    "answer_records_from_rows",
    "build_routing_question_corpus",
    "corpus_shape_errors",
    "format_routing_question_corpus",
    "format_routing_question_score",
    "parse_answer_row",
    "read_answer_records_from_directory",
    "read_answer_rows_from_jsonl",
    "read_answer_source",
    "resolve_answer_action",
    "routing_question_score_errors",
    "score_routing_question_answers",
]

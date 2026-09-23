"""Lint the sentence a person reads for the three reply rules OMH ships.

The rules are prompt text: every generated skill's Runtime Evidence tail, the
common rail's "Reply Language And Host Voice" and "Turn Ending" sections, and
the awareness primers all say the same thing -- OMH's record vocabulary stays
in records and tool calls, awareness lines are never quoted, and a stop or a
decision the user owns ends the turn with the next action offered as a
question. Nothing observed whether a reply followed them; the only evidence
was a person reading "this is an evidence-bounded surface" or "I will not
merge here" and reporting it.

This module reads reply text and reports where it departs from those rules:

- ``record_term_leak``: an OMH record term in the reply (the rail's list, plus
  the Korean renderings that reached the owner's replies).
- ``awareness_line_quoted``: an ``[OMH Awareness]`` or ``Boundary:`` line
  quoted into the reply.
- ``refusal_closer``: the closing paragraph declares what will not be done
  and offers no question.
- ``decision_without_question``: the closing paragraph names an approval or a
  decision the user owns and offers no question.

It is pure: no model, no network, no file write. A term the user's own
message names is explained on request, not leaked, so it is carved out. A
clean result shows only that the text carries none of these shapes; it says
nothing about whether the reply was correct, complete, or in the host's
voice, and the payload's claim boundary says so.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence


REPLY_LINT_SCHEMA_VERSION = "reply_lint/v1"

REPLY_LINT_CLAIM_BOUNDARY = (
    "This lint reads reply text only. A clean result shows that the text carries no "
    "OMH record term, no quoted awareness line, and no closing that declares what will "
    "not be done or leaves a decision without a question. It does not show that the reply "
    "was correct, complete, or in the host's own voice, and it is not execution, review, "
    "CI, or merge evidence."
)

FINDING_KINDS: tuple[str, ...] = (
    "record_term_leak",
    "awareness_line_quoted",
    "refusal_closer",
    "decision_without_question",
)

# The rail's record vocabulary, spelled as it reaches a reply. Underscore
# tokens are exact; English phrases are whole-word and case-insensitive.
# ``surface`` and ``lane`` are everyday English, so they are matched only in
# the qualified forms the bodies use; ``wrapper`` is a programming word and is
# matched only as OMH's own compound. ``handoff`` is OMH's term in a coding
# reply and is matched whole. Longest match wins, so ``prepared_not_observed``
# is one finding, not two.
_ENGLISH_RECORD_TERMS: tuple[str, ...] = (
    "prepared_not_observed",
    "not_observed",
    "not_available",
    "routing_observation",
    "evidence boundary",
    "claim boundary",
    "evidence-bounded",
    "operating surface",
    "OMH surface",
    "evidence surface",
    "coding lane",
    "workflow lane",
    "OMH lane",
    "run record",
    "wrapper action",
    "wrapper card",
    "OMH wrapper",
    "handoff",
)

# Korean renderings observed in live replies ("두 표면을 서빙합니다", "레인을
# 열었습니다"). A term matches with or without a trailing particle and never
# inside another word, so 표면적 (superficially) and 브레인 (brain) do not
# match while 표면을 and 레인을 do.
_KOREAN_RECORD_TERMS: tuple[str, ...] = (
    "표면",
    "레인",
    "핸드오프",
    "증거 경계",
    "래퍼",
)

_KOREAN_PARTICLES = "(?:이|가|을|를|은|는|의|에|과|와|로|도|만|들|으로|입니다|이다)?"
_HANGUL = "가-힣"

_AWARENESS_LINE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("[OMH Awareness]", re.compile(r"\[OMH Awareness\]")),
    ("Boundary:", re.compile(r"(?m)^\s*(?:>\s*)?Boundary:")),
    ("Route hint:", re.compile(r"(?m)^\s*(?:>\s*)?Route hint:")),
)

# A closing that says what will not be done. English forms are first person;
# Korean forms are the verb endings the owner's replies closed on
# ("하지 않을게", "처리하지 않겠습니다", "시작하지 않음").
_REFUSAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bI will not\b", re.IGNORECASE),
    re.compile(r"\bI won'?t\b", re.IGNORECASE),
    re.compile(r"\bI(?:'m| am) not going to\b", re.IGNORECASE),
    re.compile(r"\bI(?:'ll| will) stop here\b", re.IGNORECASE),
    re.compile(r"\bstopping here\b", re.IGNORECASE),
    re.compile(r"\bno further (?:action|changes|edits|work)\b", re.IGNORECASE),
    re.compile(r"하지 않을게"),
    re.compile(r"하지 않겠"),
    re.compile(r"하지 않음"),
    re.compile(r"않을게요"),
    re.compile(r"안 할게"),
    re.compile(r"진행하지 않"),
    re.compile(r"여기서 멈"),
    re.compile(r"중단하겠"),
    re.compile(r"보류하겠"),
)

# A closing that hands the user a decision without asking it.
_DECISION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:your|the user's) (?:call|decision|approval)\b", re.IGNORECASE),
    re.compile(r"\b(?:needs|requires|awaiting|waiting for) (?:your )?(?:approval|confirmation|decision)\b", re.IGNORECASE),
    re.compile(r"승인이 필요"),
    re.compile(r"승인 대기"),
    re.compile(r"결정이 필요"),
    re.compile(r"결정해 주"),
    re.compile(r"선택이 필요"),
)

_QUESTION_MARKS = ("?", "？")
_KOREAN_QUESTION_ENDINGS = ("까요", "습니까", "할까", "볼까", "드릴까", "될까", "나요", "가요")

_EXCERPT_CHARS = 160
_CLOSING_EXCERPT_CHARS = 240


def _english_term_pattern(term: str) -> re.Pattern[str]:
    if "_" in term:
        return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])")
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def _korean_term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(
        f"(?<![{_HANGUL}])" + re.escape(term) + _KOREAN_PARTICLES + f"(?![{_HANGUL}])"
    )


_RECORD_TERM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    [(term, _english_term_pattern(term)) for term in _ENGLISH_RECORD_TERMS]
    + [(term, _korean_term_pattern(term)) for term in _KOREAN_RECORD_TERMS]
)


def record_term_vocabulary() -> tuple[str, ...]:
    """The terms this lint reports, in match order, for docs and tests."""
    return tuple(term for term, _ in _RECORD_TERM_PATTERNS)


def build_reply_lint(reply: str, *, user_text: str = "") -> dict[str, Any]:
    """Lint one reply. ``user_text`` is the message it answers, for the carve-out."""
    text = str(reply or "")
    asked = str(user_text or "")
    lines = text.split("\n")
    findings: list[dict[str, Any]] = []
    carved_out: list[str] = []

    for term, match in _record_term_matches(text):
        if _user_named(term, match, asked):
            if term not in carved_out:
                carved_out.append(term)
            continue
        findings.append(_finding("record_term_leak", term, text, lines, match.start()))

    for label, pattern in _AWARENESS_LINE_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(_finding("awareness_line_quoted", label, text, lines, match.start()))

    closing = closing_paragraph(text)
    has_question = closing_has_question(closing)
    closing_kind = ""
    if not has_question:
        refusal = _first_match(_REFUSAL_PATTERNS, closing)
        if refusal is not None:
            closing_kind = "refusal_closer"
            findings.append(_closing_finding(closing_kind, refusal, text, lines, closing))
        else:
            decision = _first_match(_DECISION_PATTERNS, closing)
            if decision is not None:
                closing_kind = "decision_without_question"
                findings.append(_closing_finding(closing_kind, decision, text, lines, closing))

    findings.sort(key=lambda item: (int(item["line"]), int(item["column"]), str(item["kind"])))
    counts = {kind: sum(1 for item in findings if item["kind"] == kind) for kind in FINDING_KINDS}
    return {
        "schema_version": REPLY_LINT_SCHEMA_VERSION,
        "reply_chars": len(text),
        "closing": {
            "excerpt": _clip(closing, _CLOSING_EXCERPT_CHARS),
            "has_question": has_question,
            "kind": closing_kind,
        },
        "findings": findings,
        "counts": counts,
        "finding_count": len(findings),
        "carved_out_terms": carved_out,
        "ok": not findings,
        "claim_boundary": REPLY_LINT_CLAIM_BOUNDARY,
    }


def summarize_reply_lints(
    records: Sequence[Mapping[str, Any]], *, source: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Fold per-reply records into one payload with the same claim boundary."""
    rows = [dict(record) for record in records]
    counts = {kind: sum(int(row.get("counts", {}).get(kind, 0)) for row in rows) for kind in FINDING_KINDS}
    finding_count = sum(counts.values())
    return {
        "schema_version": REPLY_LINT_SCHEMA_VERSION,
        "source": dict(source or {}),
        "reply_count": len(rows),
        "replies": rows,
        "counts": counts,
        "finding_count": finding_count,
        "ok": finding_count == 0,
        "claim_boundary": REPLY_LINT_CLAIM_BOUNDARY,
    }


def format_reply_lint_summary(payload: Mapping[str, Any]) -> str:
    """Plain-text rendering: verdict, per-reply findings, then the boundary."""
    out: list[str] = []
    finding_count = int(payload.get("finding_count", 0))
    reply_count = int(payload.get("reply_count", 0))
    verdict = "clean" if payload.get("ok") else f"{finding_count} finding{'s' if finding_count != 1 else ''}"
    out.append(f"OMH reply lint: {verdict} across {reply_count} repl{'y' if reply_count == 1 else 'ies'}")
    counts = payload.get("counts") or {}
    out.append("  " + "    ".join(f"{kind}: {int(counts.get(kind, 0))}" for kind in FINDING_KINDS))
    for index, record in enumerate(payload.get("replies") or (), start=1):
        closing = record.get("closing") or {}
        state = "clean" if record.get("ok") else f"{int(record.get('finding_count', 0))} finding(s)"
        out.append(f"Reply {index}: {state}    closing question: {'yes' if closing.get('has_question') else 'no'}")
        for finding in record.get("findings") or ():
            out.append(
                f"  line {finding.get('line')}: {finding.get('kind')} `{finding.get('match')}`"
                f" -- {finding.get('excerpt')}"
            )
        carved = record.get("carved_out_terms") or ()
        if carved:
            out.append("  named by the user, not counted: " + ", ".join(str(term) for term in carved))
    out.append("Boundary")
    out.append(f"  {payload.get('claim_boundary', REPLY_LINT_CLAIM_BOUNDARY)}")
    return "\n".join(out)


def closing_paragraph(text: str) -> str:
    """The last blank-line-separated block; a list stays one block."""
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]
    return blocks[-1] if blocks else ""


def closing_has_question(closing: str) -> bool:
    if any(mark in closing for mark in _QUESTION_MARKS):
        return True
    stripped = closing.rstrip(" \t.)]」』\"'`*_")
    return any(stripped.endswith(ending) for ending in _KOREAN_QUESTION_ENDINGS)


def _record_term_matches(text: str) -> list[tuple[str, re.Match[str]]]:
    candidates: list[tuple[str, re.Match[str]]] = []
    for term, pattern in _RECORD_TERM_PATTERNS:
        candidates.extend((term, match) for match in pattern.finditer(text))
    # Longest span first, then earliest; a span inside an accepted one is dropped.
    candidates.sort(key=lambda item: (-(item[1].end() - item[1].start()), item[1].start()))
    accepted: list[tuple[str, re.Match[str]]] = []
    taken: list[tuple[int, int]] = []
    for term, match in candidates:
        span = (match.start(), match.end())
        if any(start < span[1] and span[0] < end for start, end in taken):
            continue
        taken.append(span)
        accepted.append((term, match))
    accepted.sort(key=lambda item: item[1].start())
    return accepted


def _user_named(term: str, match: re.Match[str], user_text: str) -> bool:
    if not user_text:
        return False
    lowered = user_text.lower()
    return term.lower() in lowered or match.group(0).lower() in lowered


def _first_match(patterns: Iterable[re.Pattern[str]], text: str) -> re.Match[str] | None:
    found: re.Match[str] | None = None
    for pattern in patterns:
        match = pattern.search(text)
        if match is not None and (found is None or match.start() < found.start()):
            found = match
    return found


def _finding(kind: str, label: str, text: str, lines: Sequence[str], offset: int) -> dict[str, Any]:
    line_index = text.count("\n", 0, offset)
    line_start = text.rfind("\n", 0, offset) + 1
    return {
        "kind": kind,
        "match": label,
        "line": line_index + 1,
        "column": offset - line_start + 1,
        "excerpt": _clip(lines[line_index].strip(), _EXCERPT_CHARS),
    }


def _closing_finding(
    kind: str, match: re.Match[str], text: str, lines: Sequence[str], closing: str
) -> dict[str, Any]:
    closing_offset = text.rfind(closing)
    offset = (closing_offset if closing_offset >= 0 else 0) + match.start()
    finding = _finding(kind, match.group(0), text, lines, offset)
    return finding


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"

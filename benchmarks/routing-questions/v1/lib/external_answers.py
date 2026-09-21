"""A pre-flight read of an answer file somebody else produced.

The external arm is any tool an operator likes: a vendor SDK script, a
spreadsheet export, a hand-written file. It hands over rows, and this module
says, offline, whether they are shaped like answers before a scoring run is
started.

This is a pre-flight, not the authority. `omh chat route-questions score` is
what decides what an answer set is worth, and it names every malformed row by
its reference. This module exists so an operator finds a broken file in a
second rather than at the end of a scoring run, and it deliberately checks less
than the scorer does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ANSWERS_SCHEMA_VERSION = "routing_question_answers/v1"
ROUTE_CHOICE_KEY = "route_choice"


def _row_problem(row: object) -> str:
    if not isinstance(row, dict):
        return "row is not an object"
    if row.get("schema_version") != ANSWERS_SCHEMA_VERSION:
        return f"schema_version is not {ANSWERS_SCHEMA_VERSION}"
    if not str(row.get("arm") or "").strip():
        return "row names no arm"
    if not str(row.get("case_id") or "").strip() and not str(row.get("question_digest") or "").strip():
        return "row names neither case_id nor question_digest"
    answers = row.get("answers")
    if not isinstance(answers, dict):
        return "row carries no answers object"
    choice = answers.get(ROUTE_CHOICE_KEY)
    if not isinstance(choice, dict) or not str(choice.get("choice") or "").strip():
        return f"row carries no {ROUTE_CHOICE_KEY} answer"
    return ""


def _rows_from_text(text: str, *, name: str) -> tuple[list[Any], list[dict[str, str]]]:
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            document = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return [], [{"ref": name, "reason": f"file is not readable JSON: {exc.msg}"}]
        if not isinstance(document, list):
            return [], [{"ref": name, "reason": "file is not a JSON array"}]
        return list(document), []
    rows: list[Any] = []
    problems: list[dict[str, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            rows.append(json.loads(candidate))
        except json.JSONDecodeError as exc:
            problems.append({"ref": f"{name}:{number}", "reason": f"line is not JSON: {exc.msg}"})
    return rows, problems


def validate_answer_file(path: Path) -> dict[str, object]:
    """Report, offline, whether a supplied answer file is shaped like answers.

    Accepts either a JSON array or one JSON object per line, because an
    operator's tool produces whichever is convenient and both convert to the
    JSONL the scorer reads.
    """
    target = Path(path)
    name = target.name
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "schema_version": "routing_question_answer_preflight/v1",
            "path": str(target),
            "ok": False,
            "row_count": 0,
            "arms": [],
            "problems": [{"ref": name, "reason": f"file is not readable: {exc}"}],
        }
    rows, problems = _rows_from_text(text, name=name)
    arms: set[str] = set()
    for index, row in enumerate(rows, start=1):
        problem = _row_problem(row)
        if problem:
            problems.append({"ref": f"{name}:{index}", "reason": problem})
            continue
        arms.add(str(row.get("arm")))
    return {
        "schema_version": "routing_question_answer_preflight/v1",
        "path": str(target),
        "ok": not problems and bool(rows),
        "row_count": len(rows),
        "arms": sorted(arms),
        "problems": problems,
    }


def write_jsonl(rows: list[dict[str, Any]], path: Path) -> int:
    """Write answer rows as the JSONL the scorer reads, sorted keys per row."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    return len(rows)


__all__ = [
    "ANSWERS_SCHEMA_VERSION",
    "ROUTE_CHOICE_KEY",
    "validate_answer_file",
    "write_jsonl",
]

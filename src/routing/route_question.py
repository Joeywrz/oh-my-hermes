"""One typed question shape for a route OMH cannot decide on its own.

A deterministic router either has enough signal or it does not. When it does
not, the thing it needs answered is not "run the skill" -- it is two different
questions, and keeping them apart is the whole point of this module:

- **Which** workflow, if any, is the best fit among the candidates. That is a
  relative judgment over a shortlist, so it is one Choice with an explicit
  ``none`` option. A Choice always returns one of its options, so without that
  option the answer "no workflow applies" cannot be expressed at all.
- **Whether** each candidate actually fits. That is an absolute judgment about
  one workflow, so it is one yes/no question per candidate, each answerable
  without reference to the others.

The two carry no structural invariant between them: a Choice over options and
one yes/no per option answer different questions, and an answerer may pick a
candidate in the Choice while saying no to every fit. That is not a
contradiction to resolve here. This module builds the questions; a scorer
decides what a set of answers means.

Nothing in this module performs I/O, reads configuration, or names any
answerer. The same block is built for a live undecidable route and for an
offline corpus item, which is why it lives here rather than beside either
consumer.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

ROUTE_QUESTION_SCHEMA_VERSION = "route_question/v1"

# The question ids. `ROUTE_CHOICE_KEY` is the single relative choice;
# `FIT_QUESTION_PREFIX` + skill name is the absolute per-candidate question.
ROUTE_CHOICE_KEY = "route_choice"
FIT_QUESTION_PREFIX = "fits::"

# The option that lets a Choice answer "no workflow applies". A Choice returns
# one of its options and nothing else, so an answerer without this option is
# forced to name a workflow it may have just rejected.
NO_WORKFLOW_OPTION = "none"
NO_WORKFLOW_DESCRIPTION = "No OMH workflow applies; answer the request directly."

# Flat, editorial defaults. They are deliberately not derived from the router's
# own confidence band: the question exists precisely because that band was too
# low to act on, so scaling the acceptance bar with it would set the loosest
# bar exactly where the router is least sure. Flat keeps the bar the same
# whatever produced the question, and an operator raises it for a surface where
# a wrong dispatch costs more.
FITS_DISPATCH_THRESHOLD = 0.8
FITS_CLARIFY_THRESHOLD = 0.5

# Catalog descriptions carry this product prefix. It is stripped from every
# option and question so the prefix cannot become a cue of its own.
_DESCRIPTION_PREFIX = "[omh] "

_CHOICE_INSTRUCTIONS = (
    "Which OMH workflow is the best fit for this request? "
    "This is a relative choice among the listed options: pick the closest fit, "
    f"or `{NO_WORKFLOW_OPTION}` when the request asks for none of them."
)

_CLAIM_BOUNDARY = (
    "A route question is a prepared question about one request, not a routing "
    "decision, an execution, or evidence that any workflow ran. An unanswered "
    "question changes nothing: the deterministic route stays in force."
)


def clean_skill_description(value: object) -> str:
    """Strip the catalog's product prefix from a skill description."""
    text = str(value or "").strip()
    if text.startswith(_DESCRIPTION_PREFIX):
        text = text[len(_DESCRIPTION_PREFIX):].strip()
    return text


def _candidate_rows(candidates: Iterable[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """Return (skill, description) pairs in candidate order, first mention wins."""
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates or ():
        if not isinstance(candidate, Mapping):
            continue
        skill = str(candidate.get("skill") or "").strip()
        if not skill or skill == NO_WORKFLOW_OPTION or skill in seen:
            continue
        seen.add(skill)
        rows.append((skill, clean_skill_description(candidate.get("description"))))
    return rows


def fit_question_key(skill: str) -> str:
    """The absolute per-candidate question id for one skill."""
    return f"{FIT_QUESTION_PREFIX}{skill}"


def fit_question_skill(key: str) -> str:
    """The skill a fit question id names, or "" when the id is not one."""
    text = str(key or "")
    if not text.startswith(FIT_QUESTION_PREFIX):
        return ""
    return text[len(FIT_QUESTION_PREFIX):]


def route_question_digest(questions: Mapping[str, Any]) -> str:
    """A content digest over the question block, used to join an answer to it."""
    canonical = json.dumps(questions, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_route_question_from_candidates(
    candidates: Iterable[Mapping[str, Any]],
    *,
    reasons: Iterable[str] = (),
    digest: str = "",
) -> dict[str, object]:
    """Build the typed question for one undecidable route.

    `candidates` are mappings carrying `skill` and `description`; a candidate
    without a skill name, or one named `none`, is dropped, and a repeated skill
    keeps its first description. An empty candidate list is legal and yields a
    Choice whose only option is `none` -- that is the honest shape when the
    router found nothing, and it still lets an answerer disagree by fitting
    nothing.

    `reasons` are the router's own words for why the route was undecidable and
    are carried through unchanged. `digest` joins an answer back to the exact
    question it answered; when it is empty a content digest over the questions
    is used instead, so every block has one.
    """
    rows = _candidate_rows(candidates)
    options: dict[str, str] = {skill: description for skill, description in rows}
    options[NO_WORKFLOW_OPTION] = NO_WORKFLOW_DESCRIPTION
    questions: dict[str, object] = {
        ROUTE_CHOICE_KEY: {
            "type": "choice",
            "instructions": _CHOICE_INSTRUCTIONS,
            "options": options,
        }
    }
    for skill, description in rows:
        detail = f" `{skill}`: {description}" if description else ""
        questions[fit_question_key(skill)] = {
            "type": "noul",
            "instructions": (
                f"Does this request ask for the work `{skill}` does?{detail} "
                "Answer for this workflow alone, independently of the others."
            ),
        }
    return {
        "schema_version": ROUTE_QUESTION_SCHEMA_VERSION,
        "question_digest": str(digest) or route_question_digest(questions),
        "questions": questions,
        "thresholds": {
            "fits_dispatch": FITS_DISPATCH_THRESHOLD,
            "fits_clarify": FITS_CLARIFY_THRESHOLD,
        },
        "reasons": [str(reason) for reason in reasons if str(reason).strip()],
        "claim_boundary": _CLAIM_BOUNDARY,
    }


__all__ = [
    "FITS_CLARIFY_THRESHOLD",
    "FITS_DISPATCH_THRESHOLD",
    "FIT_QUESTION_PREFIX",
    "NO_WORKFLOW_DESCRIPTION",
    "NO_WORKFLOW_OPTION",
    "ROUTE_CHOICE_KEY",
    "ROUTE_QUESTION_SCHEMA_VERSION",
    "build_route_question_from_candidates",
    "clean_skill_description",
    "fit_question_key",
    "fit_question_skill",
    "route_question_digest",
]

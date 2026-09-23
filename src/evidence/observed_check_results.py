"""The fields an `observed_check_results/v1` row carries, declared once.

`verification-gate` used to state this row's shape twice in its own rendered
body and the two disagreed (#1788): the quality bar asked for "command/source,
freshness, exit status, and scope" while the artifact expectation asked for
"command, timestamp/source, exit status, summary, and stale-output flag". Four
fields against five, with `scope` in only the first and `summary` in only the
second, and one idea -- is this output still current? -- carried under two
names. `omh-wiki`'s handover table named two of them and a fourth restatement
was written before review caught it. Nothing disagreed with anything, because
nothing was derived from anything.

This module is the one declaration. Every rendered mention is built from
`OBSERVED_CHECK_RESULT_FIELDS`, so a field added, renamed, or dropped here
moves every surface at once, and `tests/test_observed_check_results.py` fails a
surface that writes the list out by hand instead.

The reconciliation, because picking one of the two lists would have silently
dropped a field somebody meant to require:

* `scope` and `summary` both stay. They answer different questions -- what the
  run covered, and what it said -- and only one appeared in each old list.
* `freshness` is the single name for what was `freshness` on one line and
  `stale-output flag` on the other. It absorbs the timestamp half of
  `timestamp/source`, because a timestamp exists to answer whether the output
  is still current and nothing else here reads it.
* `source` becomes a field of its own, which is the other half of
  `timestamp/source`. The same command proves different things run in this
  checkout, in a CI job, and in a person's report, and a row that cannot say
  which cannot be weighed.

`meaning` is empty where the name defines itself. A gloss on `exit status`
would be filler in a body every load pays for, and this repository's skill
bodies are budgeted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

OBSERVED_CHECK_RESULTS_TOKEN: Final[str] = "observed_check_results/v1"


@dataclass(frozen=True)
class ObservedCheckResultField:
    """One field a recorded check result carries, and what it is for.

    ``meaning`` renders as a parenthetical beside the name wherever the full
    declaration appears, so a reader of any surface can tell whether they are
    recording the field or only naming it. Empty means the name is its own
    definition.
    """

    name: str
    meaning: str = ""


OBSERVED_CHECK_RESULT_FIELDS: Final[tuple[ObservedCheckResultField, ...]] = (
    ObservedCheckResultField("command", "verbatim, not a description of it"),
    ObservedCheckResultField("source", "this checkout, a CI job, or an operator report"),
    ObservedCheckResultField("exit status"),
    ObservedCheckResultField("summary", "what the output said"),
    ObservedCheckResultField("scope", "what that run covered, since a narrower run proves less"),
    ObservedCheckResultField("freshness", "when it ran, and whether the tree has moved since"),
)


def observed_check_result_field_names() -> tuple[str, ...]:
    """Every declared field name, in declaration order."""
    return tuple(field.name for field in OBSERVED_CHECK_RESULT_FIELDS)


def _serial_phrase(parts: tuple[str, ...]) -> str:
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def observed_check_results_field_phrase() -> str:
    """The field names alone, for a surface with no room for the meanings."""
    return _serial_phrase(observed_check_result_field_names())


def observed_check_results_expectation() -> str:
    """The full declaration: the row's token, its fields, and what each is for."""
    rendered = tuple(
        f"{field.name} ({field.meaning})" if field.meaning else field.name
        for field in OBSERVED_CHECK_RESULT_FIELDS
    )
    return f"{OBSERVED_CHECK_RESULTS_TOKEN} with {_serial_phrase(rendered)}"

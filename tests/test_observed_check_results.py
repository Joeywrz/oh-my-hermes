"""`observed_check_results/v1` has one declaration, and every surface renders it.

The row used to be written out twice in `verification-gate`'s own rendered
body with different field sets, and a third, shorter form sat in `omh-wiki`'s
handover table (#1788). None of them could fail, because none of them was
derived from anything: a consumer inherited whichever copy it happened to read,
and the one that most often went missing was freshness -- the gate's own guard
against reporting a stale output as evidence.

These tests hold the shape that replaced it. One module declares the fields,
every rendered mention is built from it, and a surface that writes the list out
by hand fails here rather than becoming the next variant.
"""

from __future__ import annotations

import unittest

from omh.evidence.observed_check_results import (
    OBSERVED_CHECK_RESULT_FIELDS,
    OBSERVED_CHECK_RESULTS_TOKEN,
    observed_check_result_field_names,
    observed_check_results_expectation,
    observed_check_results_field_phrase,
)
from omh.skills.packaging import (
    builtin_skill_reference_templates,
    builtin_skill_templates,
)
from omh.skills.render import (
    agent_skill_reference_templates,
    agent_skill_templates,
    observed_check_results_declaration,
)

# How many declared field names on one line make that line a restatement of the
# row rather than a sentence that happens to use one of the words.
#
# Measured, not chosen: the two copies this issue is about named five fields
# ("command/source, freshness, exit status, and scope") and four
# ("command, timestamp/source, exit status, summary, and stale-output flag"), so
# three catches both with a margin. Lower would fire on ordinary prose -- these
# are everyday words, and `omh-docs`'s "official URL, CLI command, or narrowly
# scoped local path" names three of them in an unrelated sense on a page that
# never mentions this row, which is why the scan is scoped to the surfaces that
# do mention it.
RESTATEMENT_FIELD_THRESHOLD = 3


def _rendered_surfaces() -> list[tuple[str, str]]:
    """Every rendered skill body and reference, as (label, text)."""
    surfaces: list[tuple[str, str]] = []
    for template in builtin_skill_templates():
        surfaces.append((f"skills/{template.name}/SKILL.md", template.content))
    for reference in builtin_skill_reference_templates():
        surfaces.append((f"skills/{reference.skill_name}/{reference.relative_path}", reference.content))
    for template in agent_skill_templates():
        surfaces.append((f"agent-skills/{template.name}/SKILL.md", template.content))
    for reference in agent_skill_reference_templates():
        surfaces.append(
            (f"agent-skills/{reference.skill_name}/{reference.relative_path}", reference.content)
        )
    return surfaces


def _named_fields(line: str) -> set[str]:
    lowered = line.lower()
    return {name for name in observed_check_result_field_names() if name in lowered}


def _restatements(surfaces: list[tuple[str, str]]) -> tuple[list[str], int]:
    """Lines that enumerate the row without rendering it, and how many were weighed.

    A surface is in scope only when it names the row; inside one, a line is an
    enumeration once it reaches `RESTATEMENT_FIELD_THRESHOLD` field names. It
    then has to carry the rendered declaration or the rendered name list, since
    both move when the declaration moves and a hand-written list does not.
    """
    expectation = observed_check_results_expectation()
    phrase = observed_check_results_field_phrase()
    offenders: list[str] = []
    weighed = 0
    for label, text in surfaces:
        if OBSERVED_CHECK_RESULTS_TOKEN not in text:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            named = _named_fields(line)
            if len(named) < RESTATEMENT_FIELD_THRESHOLD:
                continue
            weighed += 1
            if expectation in line or phrase in line:
                continue
            offenders.append(f"{label}:{number} names {sorted(named)}: {line.strip()[:160]}")
    return offenders, weighed


class ObservedCheckResultsDeclarationTests(unittest.TestCase):
    def test_the_gate_publishes_the_declaration_it_does_not_retype_it(self) -> None:
        """The catalog entry is the producer's output, byte for byte.

        This is the derivation itself. Every other surface quotes what the gate
        publishes, so a catalog entry typed out by hand would hand all of them
        a string that no longer answers to the declaration.
        """
        self.assertEqual(
            observed_check_results_declaration(),
            observed_check_results_expectation(),
            "verification-gate's artifact expectation is no longer the rendered "
            "declaration; edit src/evidence/observed_check_results.py and let the "
            "catalog entry render from it, rather than writing the fields into "
            "the catalog",
        )

    def test_the_reconciled_set_keeps_both_disputed_fields_under_one_name_each(self) -> None:
        """`scope` and `summary` both survive, and freshness has one name.

        The two old lists disagreed in exactly three places, and the fix was a
        reconciliation rather than a choice between them: `scope` appeared only
        on the quality-bar line, `summary` only on the artifact expectation, and
        the question "is this output still current?" was called `freshness` on
        one and `stale-output flag` on the other. Dropping either of the first
        two, or letting the third split back into two names, undoes that.
        """
        names = observed_check_result_field_names()
        for required in ("scope", "summary", "freshness"):
            self.assertIn(
                required,
                names,
                f"`{required}` left the declaration; it was one of the two "
                "fields the old copies disagreed about, so removing it needs a "
                "stated decision rather than an edit",
            )
        for merged in ("timestamp", "stale-output"):
            self.assertNotIn(
                merged,
                " ".join(names),
                f"`{merged}` is back as a field name; freshness absorbed it so "
                "one idea keeps one name, and a second name is how the two old "
                "copies came to disagree",
            )

    def test_every_disputed_field_says_what_it_is_for(self) -> None:
        """A consumer can tell whether it is recording the field or naming it.

        `command` and `exit status` define themselves. The three the old copies
        fought over do not: a reader who meets `scope` with no gloss records the
        word and not the fact.
        """
        meanings = {field.name: field.meaning for field in OBSERVED_CHECK_RESULT_FIELDS}
        for name in ("source", "summary", "scope", "freshness"):
            self.assertTrue(
                meanings.get(name, "").strip(),
                f"`{name}` is declared with no meaning; it renders as a bare word "
                "and a consumer cannot tell what to put in it",
            )

    def test_no_rendered_surface_restates_the_field_list(self) -> None:
        """A surface that names this row names all of its fields, or none.

        The scan is scoped to rendered surfaces that mention the row, and to
        lines naming at least `RESTATEMENT_FIELD_THRESHOLD` of its fields. A
        line over that threshold is enumerating the row, so it has to carry the
        rendered declaration or the rendered name list -- either of which moves
        when the declaration moves. A line under it is prose, and prose that
        singles out one field is how the gate is meant to be written about.

        What this cannot see: an enumeration on a surface that never names the
        row at all. That is why the gate's quality-bar line now names it
        instead of saying only "for each observed result", which is how the
        copy that drifted stayed out of reach of any check.
        """
        offenders, weighed = _restatements(_rendered_surfaces())
        self.assertEqual(
            offenders,
            [],
            "these rendered lines enumerate the observed_check_results/v1 row "
            "without rendering it from src/evidence/observed_check_results.py, "
            "so they can disagree with the gate:\n" + "\n".join(offenders),
        )
        self.assertGreaterEqual(
            weighed,
            4,
            "the scan found almost no enumerations to weigh, which means the "
            "surfaces moved rather than that they are clean; re-read what "
            "renders the row before trusting this as a pass",
        )

    def test_the_scan_catches_a_hand_written_variant(self) -> None:
        """Run the scan on the shape it exists to refuse, and watch it fire.

        The check above passes on a clean tree, which on its own says nothing
        about whether it can fail. This feeds the same scan the two lines the
        issue was filed over, on a surface that names the row exactly as the
        gate's body does, and a third line that quotes the declaration to show
        the scan is discriminating rather than refusing anything long.
        """
        drifted = "\n".join(
            (
                f"# names {OBSERVED_CHECK_RESULTS_TOKEN}",
                "- Record command/source, freshness, exit status, and scope for each observed result.",
                f"- {OBSERVED_CHECK_RESULTS_TOKEN} with command, timestamp/source, exit status, "
                "summary, and stale-output flag",
                f"- {observed_check_results_expectation()}",
            )
        )
        offenders, weighed = _restatements([("fabricated/SKILL.md", drifted)])
        self.assertEqual(weighed, 3, "the fabricated surface no longer offers three enumerations")
        self.assertEqual(
            [offender.split(" names ")[0] for offender in offenders],
            ["fabricated/SKILL.md:2", "fabricated/SKILL.md:3"],
            "the scan did not refuse the two hand-written variants this issue "
            f"was filed over, or it refused the rendered one too: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()

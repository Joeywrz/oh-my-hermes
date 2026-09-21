"""The closing handover artifacts cite records that still exist.

`wiki`'s `references/handover-artifacts.md` tells a writer to assemble the deep
guide, the ELI5 pass, and the quiz out of named record fields instead of out of
the conversation. That instruction is only as good as the names: a citation
that has been renamed on the producing surface still reads as an instruction,
and the writer who follows it finds nothing and falls back on the transcript --
which is the exact failure the page exists to prevent. So every citation is
re-derived here from the surface named beside it, rather than compared against
a second copy of the same list.
"""

from __future__ import annotations

import unittest

from omh.plugin_bundle.omh.todo_store import validate_todo_items
from omh.skills.catalog import builtin_definitions
from omh.skills.render import (
    HANDOVER_DURABLE_RECORD,
    HANDOVER_ELI5_LEVEL,
    HANDOVER_QUIZ_BASIS,
    HANDOVER_QUIZ_ENTRIES,
    HANDOVER_RECORD_SOURCES,
    HANDOVER_NATIVE_DECLARATION,
    wiki_reference_templates,
    wiki_skill,
)
from omh.workflows.paper_learning import PAPER_LEARNING_LEVELS

HANDOVER_REFERENCE_PATH = "references/handover-artifacts.md"


def _handover_reference_content() -> str:
    for template in wiki_reference_templates():
        if template.relative_path == HANDOVER_REFERENCE_PATH:
            return template.content
    raise AssertionError(f"{HANDOVER_REFERENCE_PATH} is not rendered for the wiki skill")


def _declared_contract_strings(skill_name: str) -> tuple[str, ...]:
    definitions = {definition.name: definition for definition in builtin_definitions()}
    definition = definitions[skill_name]
    return (*definition.expected_outputs, *definition.artifact_expectations)


def _todo_item_fields() -> frozenset[str]:
    """The `omh_todo/v1` item fields, taken from the validator rather than a constant.

    A behavioural derivation: the validator is what a writer's record actually
    passes through, so a renamed or dropped field changes this set even when
    some other list in the codebase still spells the old name.
    """

    validated = validate_todo_items(
        [{"text": "t", "state": "done", "phase": "p", "blocked_reason": "r", "depth": 1}]
    )
    return frozenset(validated[0])



class HandoverRecordSourceTest(unittest.TestCase):
    def test_skill_citations_are_declared_by_the_skill_named_beside_them(self) -> None:
        checked = 0
        for source in HANDOVER_RECORD_SOURCES:
            if not source.declared_by:
                continue
            declared = _declared_contract_strings(source.declared_by)
            for citation in source.citations:
                with self.subTest(skill=source.declared_by, citation=citation.token):
                    matching = [entry for entry in declared if citation.token in entry]
                    self.assertTrue(
                        matching,
                        f"`{citation.token}` is cited as coming from `{source.declared_by}`, but "
                        f"that skill declares none of it: {declared}",
                    )
                    checked += 1
        self.assertGreater(checked, 0, "no skill-declared citation was checked")

    def test_citations_match_what_the_producing_surface_says_they_are(self) -> None:
        """Being declared is not enough; the declaration has to mean the same thing.

        `verification-gate` declares a plan (`verification_matrix/v1`) and a
        result (`observed_check_results/v1`). A test that only asks whether a
        citation is declared passes the swap between them, and that swap turns
        observed into prepared. So each citation also names a word its own
        declaration must carry, and the word is re-read from the declaration
        here rather than copied beside it.
        """

        checked = 0
        for source in HANDOVER_RECORD_SOURCES:
            if not source.declared_by:
                continue
            declared = _declared_contract_strings(source.declared_by)
            for citation in source.citations:
                if not citation.must_declare:
                    continue
                with self.subTest(citation=citation.token, must_declare=citation.must_declare):
                    matching = [entry for entry in declared if citation.token in entry]
                    self.assertTrue(
                        any(citation.must_declare in entry for entry in matching),
                        f"`{citation.token}` is cited for {citation.must_declare!r}, but "
                        f"`{source.declared_by}` declares it as {matching} — the citation and "
                        "the declaration no longer mean the same thing",
                    )
                    checked += 1
        self.assertGreater(checked, 0, "no citation carried a must_declare word to check")

    def test_plan_record_citations_are_todo_item_fields(self) -> None:
        fields = _todo_item_fields()
        plan_sources = [source for source in HANDOVER_RECORD_SOURCES if not source.declared_by]
        self.assertTrue(plan_sources, "the record table declares no plan record row")
        for source in plan_sources:
            for citation in source.citations:
                with self.subTest(citation=citation.token):
                    self.assertIn(
                        citation.token,
                        fields,
                        f"`{citation.token}` is cited as a plan-record field, but an omh_todo/v1 "
                        f"item carries only {sorted(fields)}",
                    )

    def test_only_the_plan_record_is_claimed_to_outlive_the_session(self) -> None:
        """The scoping claim the whole page rests on, checked against the code.

        The live plan is separate from optional durable native declarations.
        Every optional source must name the callable reader and must not
        present its stored declaration as an observed result.
        """

        durable = {s.label for s in HANDOVER_RECORD_SOURCES if s.held_in == HANDOVER_DURABLE_RECORD}
        self.assertEqual(durable, {"Plan record"})
        for source in HANDOVER_RECORD_SOURCES:
            with self.subTest(source=source.label):
                if source.held_in == HANDOVER_DURABLE_RECORD:
                    self.assertTrue(source.read_with, "a durable source must name how to read it")
                else:
                    self.assertEqual(source.held_in, HANDOVER_NATIVE_DECLARATION)
                    self.assertEqual(source.read_with, "omh_todo action=recall")

    def test_the_page_is_right_that_phase_gives_no_order(self) -> None:
        """The deep guide says to use plan order because `phase` has none.

        Checked behaviourally: an item given no phase carries no `phase` key
        at all. A page telling a writer to sort by it would be telling them to
        sort by a field that is not always there and never ranks.
        """

        items = validate_todo_items(
            [
                {"text": "no phase here", "state": "done"},
                {"text": "has one", "state": "done", "phase": "Delivery"},
            ]
        )
        self.assertNotIn("phase", items[0], "an item given no phase must carry no phase key")
        self.assertEqual(items[1]["phase"], "Delivery")
        self.assertIn("There is no phase\n  order to sort by", _handover_reference_content())

    def test_the_page_takes_the_plan_record_caps_from_the_producer(self) -> None:
        """Caps are interpolated, never typed, so the prose cannot drift from the code."""

        from omh.plugin_bundle.omh.todo_store import MAX_TODO_ITEMS, MAX_TODO_TEXT_CHARS

        content = _handover_reference_content()
        self.assertIn(f"caps at\n  {MAX_TODO_ITEMS} items", content)
        self.assertIn(f"caps at {MAX_TODO_TEXT_CHARS} characters", content)

    def test_every_declared_source_reaches_the_rendered_table(self) -> None:
        content = _handover_reference_content()
        for source in HANDOVER_RECORD_SOURCES:
            with self.subTest(source=source.label):
                self.assertIn(f"| {source.label} (", content)
                for citation in source.citations:
                    self.assertIn(f"`{citation.token}`", content)


class HandoverEli5LevelTest(unittest.TestCase):
    def test_level_is_paper_learning_vocabulary(self) -> None:
        self.assertIn(
            HANDOVER_ELI5_LEVEL,
            PAPER_LEARNING_LEVELS,
            "the ELI5 pass must record a level paper-learning already records; a second word "
            "for one level is two vocabularies for one idea",
        )

    def test_rendered_reference_states_that_level(self) -> None:
        self.assertIn(f"Level `{HANDOVER_ELI5_LEVEL}`", _handover_reference_content())


class HandoverQuizTraceabilityTest(unittest.TestCase):
    def test_every_quiz_entry_draws_on_a_declared_source(self) -> None:
        labels = {source.label for source in HANDOVER_RECORD_SOURCES}
        self.assertTrue(HANDOVER_QUIZ_ENTRIES, "the quiz admits nothing at all")
        for entry in HANDOVER_QUIZ_ENTRIES:
            with self.subTest(entry=entry.label):
                self.assertTrue(entry.source_labels, "an admissible entry must name its source")
                for label in entry.source_labels:
                    self.assertIn(
                        label,
                        labels,
                        f"the quiz admits {entry.label!r} from {label!r}, which is not a source "
                        "the record table declares; a question citing it is untraceable",
                    )

    def test_the_rendered_quiz_table_is_generated_from_those_entries(self) -> None:
        """The table that states the prohibition must not be hand-written.

        It was, in the first version, and the test read only backticked spans
        from it — so a row admitting "a transcript recollection, whatever the
        session still remembers" carried no backticks, contributed no tokens,
        and passed every gate including `docs workflows --check`. Rendering
        the table from the entries is what closes that: a row can only exist
        if something in `HANDOVER_QUIZ_ENTRIES` put it there, and the test
        above then binds every entry to a declared source.
        """

        content = _handover_reference_content()
        quiz = content.split("## Quiz", 1)[1].split("## Boundary", 1)[0]
        rendered_rows = [
            line.strip()
            for line in quiz.splitlines()
            # Keep header and content rows; drop only the `| --- |` separator.
            if line.strip().startswith("|") and not set(line.strip()) <= set("| -")
        ]
        # One header row plus exactly one row per admissible entry, and nothing else.
        self.assertEqual(len(rendered_rows), len(HANDOVER_QUIZ_ENTRIES) + 1)
        for entry in HANDOVER_QUIZ_ENTRIES:
            self.assertTrue(
                any(row.startswith(f"| {entry.label} |") for row in rendered_rows),
                f"{entry.label!r} is admissible but renders no row",
            )

    def test_quiz_states_the_citation_rule_and_refuses_a_clean_bill(self) -> None:
        content = _handover_reference_content()
        self.assertIn("a question that cannot cite one is not\nwritten", content)
        # An empty quiz must report which basis it is empty on, never "nothing
        # went wrong" — zero entries is the ordinary outcome, not a result.
        self.assertIn("Zero questions is the ordinary outcome, not a clean bill of health.", content)
        for state in HANDOVER_QUIZ_BASIS:
            with self.subTest(state=state):
                self.assertIn(f"`{state}`", content)

    def test_the_empty_basis_separates_unread_from_read_and_empty(self) -> None:
        """`paper-learning`'s distinction, kept rather than re-coined.

        Exactly one basis says something about the change; the others say
        something about what could be read. Collapsing them is how an absent
        source becomes a positive claim that nothing went wrong.
        """

        self.assertIn("sources_read_no_entries", HANDOVER_QUIZ_BASIS)
        self.assertIn("unknown_or_missing", HANDOVER_QUIZ_BASIS)
        unreadable = set(HANDOVER_QUIZ_BASIS) - {"entries_observed", "sources_read_no_entries"}
        self.assertTrue(
            unreadable,
            "with no basis for an unreadable source, an empty quiz can only read as a clean run",
        )


class HandoverReachabilityTest(unittest.TestCase):
    def test_wiki_body_points_at_the_reference(self) -> None:
        self.assertIn(HANDOVER_REFERENCE_PATH, wiki_skill().content)

    def test_reference_is_rendered_for_the_wiki_skill(self) -> None:
        paths = {
            template.relative_path
            for template in wiki_reference_templates()
            if template.skill_name == "wiki"
        }
        self.assertIn(HANDOVER_REFERENCE_PATH, paths)


if __name__ == "__main__":
    unittest.main()

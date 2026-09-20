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
    HANDOVER_ELI5_LEVEL,
    HANDOVER_RECORD_SOURCES,
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


def _quiz_table_tokens(content: str) -> set[str]:
    quiz = content.split("## Quiz", 1)[1].split("## Boundary", 1)[0]
    tokens: set[str] = set()
    for line in quiz.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or set(stripped) <= set("| -"):
            continue
        parts = stripped.split("`")
        tokens.update(part for part in parts[1::2] if part)
    return tokens


class HandoverRecordSourceTest(unittest.TestCase):
    def test_skill_citations_are_declared_by_the_skill_named_beside_them(self) -> None:
        checked = 0
        for source in HANDOVER_RECORD_SOURCES:
            if not source.declared_by:
                continue
            declared = _declared_contract_strings(source.declared_by)
            for citation in source.citations:
                with self.subTest(skill=source.declared_by, citation=citation):
                    self.assertTrue(
                        any(citation in entry for entry in declared),
                        f"`{citation}` is cited as coming from `{source.declared_by}`, but that "
                        f"skill declares none of it: {declared}",
                    )
                    checked += 1
        self.assertGreater(checked, 0, "no skill-declared citation was checked")

    def test_plan_record_citations_are_todo_item_fields(self) -> None:
        fields = _todo_item_fields()
        plan_sources = [source for source in HANDOVER_RECORD_SOURCES if not source.declared_by]
        self.assertTrue(plan_sources, "the record table declares no plan record row")
        for source in plan_sources:
            for citation in source.citations:
                with self.subTest(citation=citation):
                    self.assertIn(
                        citation,
                        fields,
                        f"`{citation}` is cited as a plan-record field, but an omh_todo/v1 item "
                        f"carries only {sorted(fields)}",
                    )

    def test_every_declared_source_reaches_the_rendered_table(self) -> None:
        content = _handover_reference_content()
        for source in HANDOVER_RECORD_SOURCES:
            with self.subTest(source=source.label):
                self.assertIn(f"| {source.label} (", content)
                for citation in source.citations:
                    self.assertIn(f"`{citation}`", content)


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
    def test_quiz_admits_only_sources_the_record_table_declares(self) -> None:
        admissible = {source.declared_by for source in HANDOVER_RECORD_SOURCES if source.declared_by}
        admissible.update(
            citation for source in HANDOVER_RECORD_SOURCES for citation in source.citations
        )
        tokens = _quiz_table_tokens(_handover_reference_content())
        self.assertTrue(tokens, "the quiz declares no admissible entries")
        unbacked = sorted(tokens - admissible)
        self.assertFalse(
            unbacked,
            f"the quiz admits {unbacked}, which the record table does not declare; a question "
            "citing one of those cannot be traced back to a record",
        )

    def test_quiz_states_the_citation_rule_and_the_empty_case(self) -> None:
        content = _handover_reference_content()
        self.assertIn("a question that cannot cite one is\nnot written", content)
        self.assertIn("no_admissible_entries", content)


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

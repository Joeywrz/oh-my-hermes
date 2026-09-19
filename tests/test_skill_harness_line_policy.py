"""The harness line in a skill body may only name a harness the catalog chose.

`primary_harness_for_skill` answers for every name because routing, validation,
and the structure lint all need a harness that resolves; its fallback is
`coding-handling`. The renderer used to read that function, so a skill the
catalog had never assigned a harness to still shipped "Preferred harness for
this skill: `coding-handling`" and an `omh runtime record --harness
coding-handling` command in its always-loaded body -- a coding-handoff
instruction on `achievements`, `buzz`, `todo-checklist` and eleven more that do
no coding (#1690).

Everything here re-derives from the producers (`builtin_skill_templates`,
`declared_primary_harness`, `capability_family_projection`). Nothing is read
off the committed `skills/*/SKILL.md`, and no skill is named in a list that
would rot as the catalog moves.
"""

import unittest

from omh.capabilities.families import capability_family_projection
from omh.skills.catalog import declared_primary_harness, primary_harness_for_skill
from omh.skills.packaging import builtin_skill_templates

HARNESS_LINE_PREFIX = "Preferred harness for this skill: `"
RECORD_COMMAND_FLAG = "--harness "
CODING_FAMILY = "delegate_coding_and_ship"


def _rendered_bodies() -> dict[str, str]:
    return {template.name: template.content for template in builtin_skill_templates()}


def _harness_named_in(body: str) -> str:
    """The harness the body's own line names, or "" when it renders no line."""
    if HARNESS_LINE_PREFIX not in body:
        return ""
    return body.split(HARNESS_LINE_PREFIX, 1)[1].split("`", 1)[0]


class SkillHarnessLinePolicyTest(unittest.TestCase):
    def test_a_skill_with_no_declared_harness_renders_no_harness_line(self):
        """The defect itself: an undeclared harness must reach no body.

        Both halves are checked, because the line and the record command are
        two separate statements of the same claim and either one alone is
        still a wrong instruction. `omh runtime record` requires `--harness`,
        so there is no truthful command to render when nothing was declared.
        """
        offenders = sorted(
            name
            for name, body in _rendered_bodies().items()
            if not declared_primary_harness(name)
            and (HARNESS_LINE_PREFIX in body or RECORD_COMMAND_FLAG in body)
        )
        self.assertEqual(
            offenders,
            [],
            "these skills render a harness the catalog never declared; the renderer "
            "must use declared_primary_harness, not primary_harness_for_skill",
        )

    def test_a_rendered_harness_line_names_the_declared_harness(self):
        """A body may not name some other harness than the declared one."""
        mismatched = sorted(
            (name, _harness_named_in(body), declared_primary_harness(name))
            for name, body in _rendered_bodies().items()
            if _harness_named_in(body)
            and _harness_named_in(body) != declared_primary_harness(name)
        )
        self.assertEqual(mismatched, [], "rendered harness does not match the catalog")

    def test_the_line_and_the_record_command_appear_together(self):
        """Neither half may be reintroduced on its own.

        The fix removes a line and a command as one block. A later edit that
        restores only the command would put the same coding handoff back with
        nothing above it saying so.
        """
        split = sorted(
            name
            for name, body in _rendered_bodies().items()
            if (HARNESS_LINE_PREFIX in body) != (RECORD_COMMAND_FLAG in body)
        )
        self.assertEqual(split, [], "harness line and record command must ship together")

    def test_only_a_coding_skill_renders_coding_handling(self):
        """`coding-handling` in a body must agree with the catalog's own filing.

        `capability_family_projection()` is where the catalog says what kind of
        work a skill does. `delegate_coding_and_ship` is its coding family, so
        a body naming the coding harness while the catalog files the skill
        elsewhere is the #1690 defect returning under a declared entry instead
        of a fallback. Declarations that reach no body are out of scope here:
        this asserts about instruction text, and `decision-prototype` declares
        `coding-handling` from a bespoke body that renders no harness line.
        """
        family_of = capability_family_projection()["workflow_to_family"]
        self.assertIn(CODING_FAMILY, set(family_of.values()))
        offenders = sorted(
            (name, family_of.get(name, "(unfiled)"))
            for name, body in _rendered_bodies().items()
            if _harness_named_in(body) == "coding-handling"
            and family_of.get(name) != CODING_FAMILY
        )
        self.assertEqual(
            offenders,
            [],
            "a skill outside the coding capability family renders the coding harness",
        )

    def test_the_two_accessors_answer_different_questions(self):
        """Pin the split so a later edit cannot quietly collapse them.

        An unknown name is used rather than a skill that lacks an entry today,
        so this cannot start failing because the catalog filled its table in.
        """
        unknown = "no-such-skill-exists-here"
        self.assertEqual(declared_primary_harness(unknown), "")
        self.assertEqual(primary_harness_for_skill(unknown), "coding-handling")
        for name in _rendered_bodies():
            if declared_primary_harness(name):
                self.assertEqual(declared_primary_harness(name), primary_harness_for_skill(name))
            else:
                self.assertEqual(primary_harness_for_skill(name), "coding-handling")


if __name__ == "__main__":
    unittest.main()

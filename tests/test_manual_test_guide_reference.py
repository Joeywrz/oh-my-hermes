"""Contract for the manual test guide reference on `ultraqa`.

`VI. Manual test guide` is a phase of the `code-story` plan template, and
before this reference the repository owned the artifact only for RENDERED
surfaces -- `visual-qa` scopes itself to those explicitly. A CLI flag, a
migration, a config change, or a daemon restart had no page to reach.

Everything here re-derives from the producers (`builtin_skill_reference_templates()`
and `builtin_definitions()`), never from the generated `skills/` tree, so
dropping the template from the producer fails these tests while the stale
generated file still sits on disk.

What these assertions can and cannot hold is worth stating, because the
page's central rule is prose. They hold the reachability of the page, the
named artifacts the mechanism is built from, that the row shape is the gate's
own declaration rather than a fourth hand-written copy, that the page does not
copy the source table it defers to, and that neither the page nor
`tdd-red-green.md` can DELETE the refusal they share.

Two things they do not hold, stated rather than implied:

- The rule's own wording. A rewrite that keeps every token below and softens
  the sentence around it passes here.
- A REVERSAL of the shared refusal. `test_the_deferred_refusal_is_not_deleted`
  checks that the row exists, that it still carries `unobserved`, and that the
  page still cites it. Rewriting the row to "it is unobserved in the strict
  sense, but a careful person's report closes a tests-first lane well enough"
  keeps all three and passes. Catching that needs a matcher over prose, which
  this repository forbids for reasons that outlive this page, so the gate is
  deletion-only and its name says so.
"""

from __future__ import annotations

import unittest

from omh.skills.catalog import builtin_definitions
from omh.skills.duplicate_content import MIN_BLOCK_CHARS, normalize_block
from omh.skills.packaging import builtin_skill_reference_templates
from omh.skills.render import observed_check_results_declaration, workflow_skill_from_definition

REFERENCE_PATH = "references/manual-test-guide.md"
OWNING_SKILL = "ultraqa"

# The reason a step may stay manual is the page's admission test: a step
# carrying none of these is a test nobody wrote. A page that keeps the
# heading and loses the four reasons has lost the mechanism.
ADMISSION_REASONS = (
    "needs_real_install",
    "needs_external_service",
    "needs_human_judgement",
    "needs_destructive_state",
)

# The four fields one step holds, and the vocabulary the page states its
# boundary in. Each is a named artifact owned elsewhere in the repository:
# reusing them is what keeps this page and `verification-gate`, the plan
# record, and the story template saying one thing rather than three.
STEP_FIELDS = ("Setup", "Do", "Expect", "Broken")
BORROWED_VOCABULARY = (
    "prepared_not_observed",           # the claim boundary, product-wide
    "verification_matrix/v1",          # a step before anybody runs it
    "observed_check_results/v1",       # what a run can produce instead
    "blocked_reason",                  # how the phase is skipped
    "VI. Manual test guide",           # the phase this artifact answers to
    "omh runtime todo show",           # the one source that outlives the session
    "visual-qa",                       # the sibling that owns rendered surfaces
)


def _reference_content(skill: str, path: str) -> str:
    for template in builtin_skill_reference_templates():
        if template.skill_name == skill and template.relative_path == path:
            return template.content
    raise AssertionError(
        f"{skill} ships no {path}; the producer is "
        "_ultraqa_reference_templates_cached() in src/skills/render.py"
    )


def _skill_body(name: str) -> str:
    for definition in builtin_definitions():
        if definition.name == name:
            return workflow_skill_from_definition(definition, name).content
    raise AssertionError(f"no skill definition named {name}")


class ManualTestGuideReferenceTests(unittest.TestCase):
    def test_the_owning_body_points_at_the_reference(self):
        """A reference nothing points at is a file in the pack, not a capability."""
        body = _skill_body(OWNING_SKILL)
        self.assertIn(
            REFERENCE_PATH,
            body,
            f"the {OWNING_SKILL} body no longer names {REFERENCE_PATH}, so nothing "
            "loads it; the pointer lives in the quality_bar of its SkillDefinition "
            "in src/skills/catalog_definitions.py",
        )

    def test_the_pointer_carries_the_boundary_and_the_sibling(self):
        """The pointer is read by writers who never open the page.

        Naming the file alone would let a writer reach the reference believing
        the guide closes the check it describes, or write one for a rendered
        surface that `visual-qa` already owns. Both clauses are paid for in
        the always-loaded body budget, so both are pinned here.
        """
        body = _skill_body(OWNING_SKILL)
        pointers = [line for line in body.splitlines() if REFERENCE_PATH in line]
        self.assertEqual(
            len(pointers),
            1,
            f"expected exactly one {OWNING_SKILL} body line naming {REFERENCE_PATH}, "
            f"found {len(pointers)}",
        )
        for token in ("prepared_not_observed", "visual-qa"):
            self.assertIn(
                token,
                pointers[0],
                f"the {OWNING_SKILL} pointer to {REFERENCE_PATH} dropped `{token}`",
            )

    def test_the_mechanism_survives_a_rewrite(self):
        content = _reference_content(OWNING_SKILL, REFERENCE_PATH)
        missing = [token for token in ADMISSION_REASONS + BORROWED_VOCABULARY if token not in content]
        self.assertEqual(
            missing,
            [],
            f"{REFERENCE_PATH} lost named artifacts: {missing}. Each is owned "
            "elsewhere in the repository; a page that drops one states its own "
            "version of a rule that already has a home.",
        )
        for field in STEP_FIELDS:
            self.assertIn(
                f"| {field} |",
                content,
                f"{REFERENCE_PATH} lost the `{field}` row of the step table",
            )

    def test_the_page_points_at_the_source_table_instead_of_copying_it(self):
        """Defer to `handover-artifacts.md`; a second copy is a second answer.

        `src/skills/duplicate_content.py` measures this across every surface
        pair and would report a copy as a cross-surface duplicate. It is
        asserted here as well because this specific pair is the one the page
        is written to defer on, and the failure should name the page rather
        than arrive as one row of a sweep.
        """
        content = _reference_content(OWNING_SKILL, REFERENCE_PATH)
        source_page = _reference_content("wiki", "references/handover-artifacts.md")
        self.assertIn(
            "omh-wiki/references/handover-artifacts.md",
            content,
            f"{REFERENCE_PATH} no longer names the page that owns the source table",
        )
        mine = {
            normalize_block(block)
            for block in content.split("\n\n")
            if len(normalize_block(block)) >= MIN_BLOCK_CHARS
        }
        theirs = {
            normalize_block(block)
            for block in source_page.split("\n\n")
            if len(normalize_block(block)) >= MIN_BLOCK_CHARS
        }
        shared = sorted(mine & theirs)
        self.assertEqual(
            shared,
            [],
            f"{REFERENCE_PATH} copies {len(shared)} paragraph(s) from "
            "handover-artifacts.md instead of pointing at it; the first is "
            f"{shared[0][:120] if shared else ''!r}",
        )

    def test_the_row_shape_is_the_gate_s_own_declaration(self):
        """Quote the gate, never another copy of its field list.

        `observed_check_results/v1` is enumerated on several surfaces, and a
        hand-written copy here would have been one more -- the field it would
        most likely lose is freshness, which is the one this page exists to
        make somebody record. The page renders the declaration instead, so the
        two cannot disagree; this pins that it is still rendered and still the
        live catalog value. `tests/test_observed_check_results.py` owns the
        wider question of whether any surface restates the row (#1788).
        """
        content = _reference_content(OWNING_SKILL, REFERENCE_PATH)
        declaration = observed_check_results_declaration()
        self.assertIn(
            declaration,
            content,
            f"{REFERENCE_PATH} no longer carries verification-gate's own "
            f"declaration of the row ({declaration!r}); it must quote it rather "
            "than restate the fields, or the two surfaces can disagree",
        )
        self.assertIn(
            "freshness",
            declaration,
            "the freshness field left verification-gate's declaration; the "
            f"page quotes whatever it says, so check what {REFERENCE_PATH} now "
            "claims a manual run has to record",
        )

    def test_the_deferred_refusal_is_not_deleted(self):
        """Deletion-only, on purpose, and named for it.

        `tdd-red-green.md` refuses manual testing as a way to close a
        tests-first lane. This page exists to say what to WRITE in that case,
        which stays honest only while that refusal is still there to defer
        to. Removing either half fails here, naming the other.

        What this does NOT catch is a reversal that keeps the tokens -- see
        the module docstring. Matching the sentence is the only thing that
        would, and this repository does not match prose.
        """
        guide = _reference_content(OWNING_SKILL, REFERENCE_PATH)
        tdd = _reference_content("ultrawork", "references/tdd-red-green.md")
        row = [line for line in tdd.splitlines() if line.startswith("| Manual testing suffices")]
        self.assertEqual(
            len(row),
            1,
            "ulw-work/references/tdd-red-green.md lost its `Manual testing suffices` "
            f"row; {REFERENCE_PATH} defers to that refusal and now defers to nothing",
        )
        self.assertIn(
            "unobserved",
            row[0],
            "tdd-red-green.md no longer classifies a manual check as unobserved, "
            f"which contradicts {REFERENCE_PATH}",
        )
        self.assertIn(
            "tdd-red-green.md",
            guide,
            f"{REFERENCE_PATH} no longer cites the refusal it is scoped against, "
            "so a reader can take it as a second route to a tests-first claim",
        )


if __name__ == "__main__":
    unittest.main()

"""Closing a story has an owner, and the sentence that asks for it reaches it.

Two claims, and each is pinned against the producer rather than a second copy
of it.

The routing claim: `close` and `story` are both everyday words, and the boost
that carries a closing request to `todo-checklist` requires a third word
saying the WORK is finishing. Each of the three is proved necessary by cutting
it out of the sentence and measuring again, on `build_chat_interaction_payload`
-- the surface that decides a dispatch. `omh recommend` only ranks, and this
repository has already reported a routing defect that did not exist by
measuring the other one.

The page claim: `references/closing-a-story.md` may only tell a closer to read
records that exist. Its source table is rendered from the same
`HANDOVER_RECORD_SOURCES` the `wiki` handover page uses, so the two cannot
disagree about which of the four outlives the session, and its caps and phase
label are interpolated from the store and the template rather than typed.
"""

from __future__ import annotations

import unittest

from omh.plugin_bundle.omh.todo_store import (
    MAX_TODO_ITEMS,
    MAX_TODO_TEXT_CHARS,
    TODO_STALE_SECONDS,
)
from omh.plugin_bundle.omh.todo_templates import CODE_STORY_TEMPLATE, template_phase_labels
from omh.routing.recommend import (
    _TODO_CHECKLIST_STORY_CLOSE_ACTION_TOKENS,
    _TODO_CHECKLIST_STORY_CLOSE_COMPLETION_TOKENS,
    _TODO_CHECKLIST_STORY_CLOSE_SUBJECT_TOKENS,
    _WHOLE_PHRASE_ONLY_TRIGGER_TOKENS,
    _prepared_routable_definitions,
    _tokens,
)
from omh.skills.render import (
    HANDOVER_DURABLE_RECORD,
    HANDOVER_QUIZ_BASIS,
    HANDOVER_RECORD_SOURCES,
    TODO_CHECKLIST_CLOSING_REFERENCE_PATH,
    todo_checklist_reference_templates,
)
from omh.wrapper.contract import build_chat_interaction_payload

CLOSE_BOOST = "direct:todo_checklist_story_close"


def _route(message: str) -> dict:
    return build_chat_interaction_payload(message, source="discord", mode="auto")["route"]


def _matched_for(message: str, skill: str) -> set[str]:
    for recommendation in _route(message).get("recommendations", ()):
        if recommendation.get("skill") == skill:
            return set(recommendation.get("matched", ()))
    return set()


def _closing_reference_content() -> str:
    for template in todo_checklist_reference_templates():
        if template.relative_path == TODO_CHECKLIST_CLOSING_REFERENCE_PATH:
            return template.content
    raise AssertionError(f"{TODO_CHECKLIST_CLOSING_REFERENCE_PATH} is not rendered for todo-checklist")


class StoryCloseRoutingTest(unittest.TestCase):
    def test_a_finished_story_dispatches_to_the_skill_that_holds_the_record(self) -> None:
        route = _route("close this story, it is done")
        self.assertEqual(route["action"], "dispatch")
        self.assertEqual(route["candidate_skill"], "todo-checklist")
        self.assertEqual(route["confidence"], "high")

    def test_the_same_request_reaches_it_with_the_words_reversed(self) -> None:
        """Why the rule is a token set and not a phrase list.

        "the story is done, close it" shares no contiguous run of words with
        the sentence above, and means the same thing.
        """

        route = _route("the story is done, close it")
        self.assertEqual(route["action"], "dispatch")
        self.assertEqual(route["candidate_skill"], "todo-checklist")

    def test_each_of_the_three_token_kinds_is_load_bearing(self) -> None:
        """Cut one word out and the boost has to go with it.

        A rule nobody can drop a clause from is a rule that is doing less than
        it claims. Each sentence below is the dispatching one with exactly one
        of the three required kinds removed.
        """

        self.assertIn(CLOSE_BOOST, _matched_for("close this story, it is done", "todo-checklist"))
        for missing, message in (
            ("action", "this story, it is done"),
            ("subject", "close this, it is done"),
            ("completion", "close this story"),
        ):
            with self.subTest(missing=missing):
                self.assertNotIn(CLOSE_BOOST, _matched_for(message, "todo-checklist"))
                self.assertNotEqual(_route(message).get("action"), "dispatch")

    def test_story_modifying_something_else_is_not_a_dispatch(self) -> None:
        """The limit this rule accepts, written down rather than left implicit.

        No token set can see that `story` modifies `panel` instead of being
        the thing closed. The winding-up cue is what keeps these out of a
        dispatch; `close the story` is still a trigger phrase, so the UI
        sentence legitimately names the skill at clarify level, and that is
        the trade -- not an oversight.
        """

        for message in (
            "close the story panel when the modal loses focus",
            "when is a user story considered closed in scrum",
            "add a close button to the story carousel component",
        ):
            with self.subTest(message=message):
                self.assertNotIn(CLOSE_BOOST, _matched_for(message, "todo-checklist"))

    def test_every_token_the_rule_uses_is_held_back_for_this_skill(self) -> None:
        """A word added to a set without a hold-back widens the skill silently.

        These words also reach `todo-checklist` as the separate tokens of its
        own trigger phrases, where they would each score a loose +3. The
        hold-back is what leaves them scoring only through the pair.
        """

        prepared = {p.definition.name: p for p in _prepared_routable_definitions()}["todo-checklist"]
        required = (
            _TODO_CHECKLIST_STORY_CLOSE_ACTION_TOKENS
            | _TODO_CHECKLIST_STORY_CLOSE_SUBJECT_TOKENS
            | _TODO_CHECKLIST_STORY_CLOSE_COMPLETION_TOKENS
        )
        for token in sorted(required):
            with self.subTest(token=token):
                self.assertFalse(
                    _tokens(token) & prepared.trigger_tokens,
                    f"`{token}` scores a loose trigger-token credit for todo-checklist; add it to "
                    "_WHOLE_PHRASE_ONLY_TRIGGER_TOKENS or the pair is not what carries the intent",
                )


class FinanceCloseTokenTest(unittest.TestCase):
    def test_the_bare_close_token_no_longer_names_the_finance_workflow(self) -> None:
        """The measured defect: `month-end close` split, and `close` came loose.

        `finance-analysis` was the top candidate on "close this story, it is
        done" at a score of 4, over every workflow that has anything to do
        with finishing work.
        """

        self.assertIn("close", _WHOLE_PHRASE_ONLY_TRIGGER_TOKENS["finance-analysis"])
        for message in (
            "close this story, it is done",
            "the modal should close when the user presses escape",
            "close the file handle before unlinking it",
        ):
            with self.subTest(message=message):
                self.assertNotIn("trigger:close", _matched_for(message, "finance-analysis"))
                self.assertNotEqual(_route(message).get("candidate_skill"), "finance-analysis")

    def test_the_accounting_request_still_reaches_the_finance_workflow(self) -> None:
        """The hold-back may not cost the phrase it was derived from.

        "month-end close" reaches the workflow as a domain cue, which the
        trigger-token hold-back does not touch.
        """

        route = _route("month-end close variance for October")
        self.assertEqual(route["action"], "dispatch")
        self.assertEqual(route["candidate_skill"], "finance-analysis")


class ClosingReferenceContentTest(unittest.TestCase):
    def test_the_page_names_the_close_phase_from_the_template(self) -> None:
        close_phase = template_phase_labels(CODE_STORY_TEMPLATE)[-1]
        self.assertIn(f"`{close_phase}` in the `{CODE_STORY_TEMPLATE}` plan", _closing_reference_content())

    def test_the_page_takes_the_record_bounds_from_the_store(self) -> None:
        """Interpolated, never typed, so the prose cannot drift from the code."""

        content = _closing_reference_content()
        self.assertIn(f"at most {MAX_TODO_ITEMS} items", content)
        self.assertIn(f"{MAX_TODO_TEXT_CHARS} characters of text each", content)
        self.assertIn(f"unlinked after {TODO_STALE_SECONDS // 3600} hours", content)

    def test_the_source_table_is_the_handover_page_s_table(self) -> None:
        """One vocabulary for which records exist, not two.

        Both pages render from `HANDOVER_RECORD_SOURCES`, so a source that is
        renamed or reclassified moves on both at once. Two hand-written tables
        would drift, and the one that drifted would be sending a reader to a
        store that is not there.
        """

        content = _closing_reference_content()
        for source in HANDOVER_RECORD_SOURCES:
            with self.subTest(source=source.label):
                self.assertIn(f"| {source.label} (", content)
                for citation in source.citations:
                    self.assertIn(f"`{citation.token}`", content)

    def test_the_page_rests_the_close_on_the_one_durable_record(self) -> None:
        """The hard constraint: a verification verdict is not a close criterion.

        Nothing persists `observed_check_results/v1` or `claim_verdict/v1`, so
        a page defining a close as "the gate passed" would define it against
        something the next reader cannot read. The plan record is the only
        source the page may rest on, and the page has to say so.
        """

        durable = [s for s in HANDOVER_RECORD_SOURCES if s.held_in == HANDOVER_DURABLE_RECORD]
        self.assertEqual([s.label for s in durable], ["Plan record"])
        content = _closing_reference_content()
        self.assertIn(f"read with `{durable[0].read_with}`", content)
        self.assertIn(
            "a close judgement that reads records reads the plan record and nothing\nelse today",
            content,
        )
        self.assertIn("belong to `verification-gate`, which runs\n  before this phase", content)

    def test_the_page_refuses_to_read_a_declaration_as_work(self) -> None:
        content = _closing_reference_content()
        self.assertIn("**Every item `done` does not mean every item was worked.**", content)
        self.assertIn("**A phase `done` carrying a `blocked_reason` was skipped on purpose.**", content)
        self.assertIn("**No `blocked_reason` anywhere means nothing was RECORDED as blocked.**", content)
        self.assertIn("**Do not tick the remaining items to finish the list.**", content)

    def test_the_basis_vocabulary_is_the_one_already_shipped(self) -> None:
        """Reused from the handover page rather than coined again.

        Both answer the same question -- what could be read -- and two words
        for one idea is a defect this repository has paid for before.
        """

        content = _closing_reference_content()
        for state in HANDOVER_QUIZ_BASIS:
            with self.subTest(state=state):
                self.assertIn(f"`{state}`", content)

    def test_the_page_says_omh_never_observes_the_landing(self) -> None:
        content = _closing_reference_content()
        self.assertIn("**Landing the change happens outside OMH.**", content)
        self.assertIn("`prepared_not_observed` and is not a landing", content)


class ClosingReferenceReachabilityTest(unittest.TestCase):
    def test_the_skill_body_points_at_the_reference(self) -> None:
        from omh.skills.packaging import builtin_skill_templates

        bodies = {template.name: template.content for template in builtin_skill_templates()}
        self.assertIn(TODO_CHECKLIST_CLOSING_REFERENCE_PATH, bodies["todo-checklist"])

    def test_the_reference_is_rendered_for_the_checklist_skill(self) -> None:
        paths = {
            template.relative_path
            for template in todo_checklist_reference_templates()
            if template.skill_name == "todo-checklist"
        }
        self.assertIn(TODO_CHECKLIST_CLOSING_REFERENCE_PATH, paths)


if __name__ == "__main__":
    unittest.main()

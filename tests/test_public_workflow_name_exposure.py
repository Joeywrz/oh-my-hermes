"""A legacy catalog key must never reach a field a reader sees.

The catalog keeps `ralplan`, `ultrawork`, `ultraqa` and friends because
triggers, capability maps and lifecycle rows are keyed on them. The installed
skills answer to `ulw-plan`, `ulw-work`, `ulw-qa`. Printing the catalog key
hands the user a name nothing responds to (#1249), which
`public_workflow_identifier` exists to prevent -- and which the wrapper's own
explanation and usage-trace payloads used to reach around while
`route_explanation_payload` did not.

The legacy set is derived from the display rule, not listed here, so a later
relabel moves this guard with it.
"""

from __future__ import annotations

import json
import unittest

from _local_package import load_local_package

load_local_package()
from omh.routing.route_plan import (  # noqa: E402
    _PROSE_AMBIGUOUS_KEYS,
    public_workflow_identifier,
)
from omh.skills.catalog_types import OMH_SKILL_DISPLAY_NAME_OVERRIDES  # noqa: E402
from omh.wrapper.contract import (  # noqa: E402
    _usage_trace_naming_the_routed_workflow,
    build_chat_interaction_payload,
)

# Catalog keys whose public name differs: the ones a reader must never be shown.
_LEGACY_KEYS = tuple(
    sorted(key for key in OMH_SKILL_DISPLAY_NAME_OVERRIDES if public_workflow_identifier(key) != key)
)

# Fields that are a NAME and nothing else. A legacy key here is always a leak.
_NAME_FIELDS = (
    "label",
    "visible_prefix",
    "primary_action_label",
    "route_primary_action_label",
)

# Fields that are a SENTENCE. A legacy key that is also an ordinary English
# word can appear here for its own meaning -- "prepare project terms context",
# "the loop permission profile" -- so those keys are checked only in the name
# fields above. `_PROSE_AMBIGUOUS_KEYS` in `route_plan` is the same set, and is
# why `with_public_skill_names` leaves them alone.
_PROSE_FIELDS = (
    "why_this_workflow",
    "recommended_reply",
    "primary_action_hint",
    "route_recommended_reply",
    "route_primary_action_hint",
)

_READER_FIELDS = _NAME_FIELDS + _PROSE_FIELDS


def _reader_strings(payload: object, *, path: str = "") -> list[tuple[str, str]]:
    """Every (path, text) pair from a field in `_READER_FIELDS`, at any depth."""
    found: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            child = f"{path}.{key}" if path else str(key)
            if key in _READER_FIELDS and isinstance(value, str):
                found.append((child, value))
            else:
                found.extend(_reader_strings(value, path=child))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(_reader_strings(value, path=f"{path}[{index}]"))
    return found


class LegacySetTests(unittest.TestCase):
    def test_the_legacy_set_is_not_empty(self) -> None:
        """A guard over an empty set passes without proving anything."""
        self.assertTrue(_LEGACY_KEYS, "expected catalog keys whose public name differs")

    def test_each_legacy_key_maps_to_a_ulw_name(self) -> None:
        for key in _LEGACY_KEYS:
            with self.subTest(key=key):
                self.assertNotEqual(public_workflow_identifier(key), key)


class ExposureTests(unittest.TestCase):
    def test_no_reader_field_carries_a_legacy_key(self) -> None:
        """Invoke by every spelling, including the legacy one, and read back.

        A name field is checked against every legacy key. A sentence is checked
        against the coined ones only: `context` and `loop` are English, and a
        reply that says "prepare project terms context" is correct prose, not a
        leaked identifier.
        """
        messages = [spelling for key in _LEGACY_KEYS for spelling in (key, public_workflow_identifier(key))]
        coined = tuple(key for key in _LEGACY_KEYS if key not in _PROSE_AMBIGUOUS_KEYS)
        self.assertTrue(coined, "expected coined legacy keys to check prose against")
        for message in messages:
            payload = build_chat_interaction_payload(message, source="slack")
            for path, text in _reader_strings(payload):
                field = path.rsplit(".", 1)[-1]
                keys = _LEGACY_KEYS if field in _NAME_FIELDS else coined
                for key in keys:
                    # Word-ish containment: `ralplan` inside `ulw-plan` is not a
                    # hit, but ``ralplan`` and "Open ralplan" are.
                    for shape in (f"`{key}`", f" {key} ", f" {key}.", f"Open {key}"):
                        with self.subTest(message=message, path=path, key=key):
                            self.assertNotIn(shape, f" {text} ")

    def test_the_legacy_spelling_still_routes(self) -> None:
        """Accepted on input is the other half of the contract."""
        for key in _LEGACY_KEYS:
            with self.subTest(key=key):
                payload = build_chat_interaction_payload(key, source="slack")
                self.assertTrue(json.dumps(payload), "expected a payload for the legacy spelling")

    def test_a_legacy_invocation_is_shown_its_public_name(self) -> None:
        payload = build_chat_interaction_payload("ralplan", source="slack")
        trace = payload["chat_response"]["usage_trace"]
        self.assertEqual(trace["visible_prefix"], "[omh] ulw-plan")
        self.assertEqual(trace["label"], "ulw-plan")

    def test_the_prefix_names_the_routed_workflow_even_on_a_plan_card(self) -> None:
        """A dispatched workflow that answers with its plan card keeps its name.

        `ultrawork` on a request that names no target renders the plan card by
        design, and the prefix used to say `plan` -- so a user who typed
        `ulw-work` saw no trace of what they asked for. The card is right; the
        missing name was not.
        """
        for spelling in ("ultrawork", "ulw-work", "ulw work"):
            with self.subTest(spelling=spelling):
                payload = build_chat_interaction_payload(spelling, source="slack")
                self.assertEqual(payload["route"]["selected_skill"], "ultrawork")
                trace = payload["chat_response"]["usage_trace"]
                self.assertEqual(trace["visible_prefix"], "[omh] ulw-work")
                self.assertEqual(trace["label"], "ulw-work")

    def test_the_response_state_still_reports_what_it_rendered(self) -> None:
        """The prefix names the route; `state` names the card that was built.

        These differ on purpose for a dispatched workflow answering with its
        plan card, and a consumer reading `state` keeps the answer it had.
        """
        payload = build_chat_interaction_payload("ultrawork", source="slack")
        self.assertEqual(payload["chat_response"]["state"]["selected_workflow"], "plan")
        self.assertEqual(payload["chat_response"]["usage_trace"]["selected_workflow"], "ulw-work")

    def test_only_a_dispatch_renames_the_prefix(self) -> None:
        """A non-dispatch route has not chosen a workflow, so it borrows no name.

        Driven through the renamer directly rather than through a message that
        happens to clarify: an input that later starts dispatching would make a
        message-driven version of this pass without checking anything.
        """
        trace = {"label": "clarification", "visible_prefix": "[omh] clarification", "selected_workflow": ""}
        for action in ("clarify", "fallback", ""):
            with self.subTest(action=action):
                result = _usage_trace_naming_the_routed_workflow(
                    {"usage_trace": dict(trace)},
                    {"selected_workflow": "ulw-work", "action": action},
                )
                self.assertEqual(result["usage_trace"], trace)

    def test_a_dispatch_with_no_routed_workflow_changes_nothing(self) -> None:
        trace = {"label": "plan", "visible_prefix": "[omh] plan", "selected_workflow": "plan"}
        result = _usage_trace_naming_the_routed_workflow(
            {"usage_trace": dict(trace)}, {"selected_workflow": "", "action": "dispatch"}
        )
        self.assertEqual(result["usage_trace"], trace)

    def test_the_renamer_does_not_mutate_its_input(self) -> None:
        trace = {"label": "plan", "visible_prefix": "[omh] plan", "selected_workflow": "plan"}
        response = {"usage_trace": trace}
        _usage_trace_naming_the_routed_workflow(response, {"selected_workflow": "ulw-work", "action": "dispatch"})
        self.assertEqual(trace["label"], "plan", "the caller's trace was rewritten in place")

    def test_the_machine_readable_harness_keeps_its_own_spelling(self) -> None:
        """`selected_harness` is keyed on the catalog, not shown as a name."""
        payload = build_chat_interaction_payload("ralplan", source="slack")
        self.assertEqual(payload["chat_response"]["usage_trace"]["selected_harness"], "planning")


if __name__ == "__main__":
    unittest.main()

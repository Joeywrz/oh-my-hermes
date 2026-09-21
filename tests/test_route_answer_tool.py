"""Recording an answer to a route question, and what the record may claim.

`omh_route_answer` writes one `route_question_answer/v1` per (session,
question). The record is not a routing decision and changes nothing; it exists
so an answerer can be SCORED, which is why the row it embeds is the shape
`omh chat route-questions score --answers <dir>` already reads. The parity
cases below pin that shape against its producer in core, because the bundle
restates the vocabulary as literals and a drift there would be a record the
scorer silently counts as malformed.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()
from omh.plugin_bundle.omh.metadata import PROVIDED_TOOLS, TOOL_FILE_STEMS  # noqa: E402
from omh.plugin_bundle.omh.route_answer_store import (  # noqa: E402
    ANSWER_ROW_SCHEMA_VERSION,
    FITS_CLARIFY_THRESHOLD,
    FITS_DISPATCH_THRESHOLD,
    MAX_NOTE_CHARS,
    ROUTE_ANSWER_SCHEMA_VERSION,
    RouteAnswerStoreError,
    RouteAnswerValidationError,
    build_route_answer_record,
    confidence_source_for,
    resolve_action,
    route_answer_dir,
    write_route_answer,
)
from omh.plugin_bundle.omh.tools.route_answer_tool import (  # noqa: E402
    OMH_ROUTE_ANSWER_SCHEMA,
    omh_route_answer_handler,
)
from omh.quality.routing_question_corpus import (  # noqa: E402
    ROUTE_QUESTION_ANSWER_SCHEMA_VERSION,
    ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION,
    build_routing_question_corpus,
    read_answer_source,
    resolve_answer_action,
    score_routing_question_answers,
)
from omh.routing.chat import route_chat_message  # noqa: E402
from omh.routing.route_question import (  # noqa: E402
    FITS_CLARIFY_THRESHOLD as CORE_FITS_CLARIFY,
    FITS_DISPATCH_THRESHOLD as CORE_FITS_DISPATCH,
)

UNDECIDABLE_MESSAGE = "почему сборка падает на main"


def _digest(source: str = "hermes") -> str:
    return str(
        route_chat_message(UNDECIDABLE_MESSAGE, source=source)["route_question"]["question_digest"]
    )


class RouteAnswerToolRegistrationTests(unittest.TestCase):
    def test_the_tool_is_registered_under_its_own_name(self) -> None:
        self.assertEqual(OMH_ROUTE_ANSWER_SCHEMA["name"], "omh_route_answer")
        self.assertIn("parameters", OMH_ROUTE_ANSWER_SCHEMA)
        self.assertIn("omh_route_answer", PROVIDED_TOOLS)
        self.assertEqual(TOOL_FILE_STEMS["omh_route_answer"], "route_answer_tool")

    def test_the_description_does_not_nominate_a_third_party_call(self) -> None:
        """Naming where an answer came from is a fact; telling the model to go
        get one from a network plugin is the boundary `docs/DIRECTION.md`
        draws. The schema may name the answerer value and must not instruct."""
        description = str(OMH_ROUTE_ANSWER_SCHEMA["description"])

        self.assertNotIn("jev_", description)
        self.assertIn("changes no route", description)


class ParityWithTheScorerTests(unittest.TestCase):
    """The bundle restates core's vocabulary; these are the pins on the drift."""

    def test_the_record_and_row_schema_strings_match_their_readers(self) -> None:
        self.assertEqual(ROUTE_ANSWER_SCHEMA_VERSION, ROUTE_QUESTION_ANSWER_SCHEMA_VERSION)
        self.assertEqual(ANSWER_ROW_SCHEMA_VERSION, ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION)

    def test_the_thresholds_match_the_question_they_answer(self) -> None:
        self.assertEqual(FITS_DISPATCH_THRESHOLD, CORE_FITS_DISPATCH)
        self.assertEqual(FITS_CLARIFY_THRESHOLD, CORE_FITS_CLARIFY)

    def test_the_action_is_resolved_the_way_the_scorer_resolves_it(self) -> None:
        cases = (
            ({"plan": 0.95}, "plan"),
            ({"plan": 0.6}, "plan"),
            ({"plan": 0.1}, "plan"),
            ({}, "plan"),
            ({}, "none"),
            ({"plan": 0.0, "deep-interview": 0.51}, "none"),
        )
        for fits, choice in cases:
            with self.subTest(fits=fits, choice=choice):
                answers: dict[str, object] = {"route_choice": {"choice": choice}}
                answers.update({f"fits::{skill}": {"noul": value} for skill, value in fits.items()})
                expected, _ = resolve_answer_action(
                    answers,
                    fits_dispatch=CORE_FITS_DISPATCH,
                    fits_clarify=CORE_FITS_CLARIFY,
                )

                self.assertEqual(resolve_action(fits, choice), expected)


class RouteAnswerRecordTests(unittest.TestCase):
    def test_a_main_model_answer_is_self_reported(self) -> None:
        record = build_route_answer_record(
            question_digest="ab12", answered_by="main_model", route_choice="plan"
        )

        self.assertEqual(record["confidence_source"], "self_reported")
        self.assertEqual(confidence_source_for("main_model"), "self_reported")

    def test_a_plugin_answer_is_declared_by_the_answerer(self) -> None:
        record = build_route_answer_record(
            question_digest="ab12", answered_by="jev_plugin", route_choice="plan"
        )

        self.assertEqual(record["confidence_source"], "answerer_declared")
        self.assertNotIn("calibrated", json.dumps(record))

    def test_the_claim_boundary_refuses_execution_evidence(self) -> None:
        record = build_route_answer_record(
            question_digest="ab12", answered_by="main_model", route_choice="none"
        )

        self.assertIn("not execution, review, CI, or merge evidence", record["claim_boundary"])
        self.assertIn("does not change the route", record["claim_boundary"])

    def test_the_embedded_row_carries_both_question_kinds(self) -> None:
        record = build_route_answer_record(
            question_digest="ab12",
            answered_by="main_model",
            route_choice="plan",
            fits={"plan": 0.9, "deep-interview": 0.1},
            choice_probabilities={"plan": 0.8, "none": 0.2},
        )
        answers = record["answer"]["answers"]

        self.assertEqual(answers["route_choice"]["choice"], "plan")
        self.assertEqual(answers["route_choice"]["probabilities"], {"plan": 0.8, "none": 0.2})
        self.assertEqual(answers["fits::plan"], {"noul": 0.9})
        self.assertEqual(record["action"], "dispatch")

    def test_a_weak_fit_records_a_clarify_and_a_weaker_one_records_none(self) -> None:
        clarify = build_route_answer_record(
            question_digest="ab12", answered_by="main_model", route_choice="plan", fits={"plan": 0.6}
        )
        nothing = build_route_answer_record(
            question_digest="ab12", answered_by="main_model", route_choice="plan", fits={"plan": 0.1}
        )

        self.assertEqual(clarify["action"], "clarify")
        self.assertEqual(nothing["action"], "none")

    def test_an_invalid_field_is_refused_rather_than_dropped(self) -> None:
        cases = {
            "unknown answerer": {"answered_by": "someone_else"},
            "non-hex digest": {"question_digest": "not-a-digest"},
            "empty digest": {"question_digest": ""},
            "empty choice": {"route_choice": ""},
            "fit above one": {"fits": {"plan": 1.4}},
            "fit as a yes/no": {"fits": {"plan": True}},
            "fit as text": {"fits": {"plan": "high"}},
            "fits not an object": {"fits": [0.9]},
            "too many fits": {"fits": {f"skill-{index}": 0.5 for index in range(9)}},
            "over-long note": {"note": "x" * (MAX_NOTE_CHARS + 1)},
        }
        for label, override in cases.items():
            with self.subTest(case=label):
                arguments = {
                    "question_digest": "ab12",
                    "answered_by": "main_model",
                    "route_choice": "plan",
                }
                arguments.update(override)

                with self.assertRaises(RouteAnswerValidationError):
                    build_route_answer_record(**arguments)


class RouteAnswerWriteTests(unittest.TestCase):
    def _record(self, **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "question_digest": "ab12",
            "answered_by": "main_model",
            "route_choice": "plan",
            "fits": {"plan": 0.9},
            "session_ref": "session-a",
        }
        arguments.update(overrides)
        return build_route_answer_record(**arguments)

    def test_two_questions_in_one_session_are_two_records(self) -> None:
        """A per-session filename would have the second answer overwrite the
        first, and a session that hits two undecidable routes is ordinary."""
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            write_route_answer(home, self._record(question_digest="ab12"))
            write_route_answer(home, self._record(question_digest="cd34"))
            written = sorted(path.name for path in route_answer_dir(home).glob("*.json"))

        self.assertEqual(len(written), 2)

    def test_the_same_question_answered_twice_replaces_its_own_record(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            write_route_answer(home, self._record(route_choice="plan"))
            write_route_answer(home, self._record(route_choice="deep-interview"))
            written = sorted(route_answer_dir(home).glob("*.json"))
            latest = json.loads(written[0].read_text(encoding="utf-8"))

        self.assertEqual(len(written), 1)
        self.assertEqual(latest["route_choice"], "deep-interview")

    def test_two_sessions_answering_one_question_keep_their_own_records(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            write_route_answer(home, self._record(session_ref="session-a"))
            write_route_answer(home, self._record(session_ref="session-b"))
            written = sorted(path.name for path in route_answer_dir(home).glob("*.json"))

        self.assertEqual(len(written), 2)

    def test_the_file_is_written_with_sorted_keys(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            destination = write_route_answer(home, self._record())
            text = destination.read_text(encoding="utf-8")

        self.assertEqual(text, json.dumps(json.loads(text), sort_keys=True) + "\n")

    def test_a_symlinked_destination_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            home = root / ".omh"
            (home / "runtime").mkdir(parents=True)
            try:
                (home / "runtime" / "route-questions").symlink_to(
                    root / "elsewhere", target_is_directory=True
                )
            except (OSError, NotImplementedError):
                self.skipTest("this platform does not allow creating a symlink here")

            with self.assertRaises(RouteAnswerStoreError):
                write_route_answer(home, self._record())


class RouteAnswerHandlerTests(unittest.TestCase):
    def _call(self, root: Path, arguments: dict[str, object], **kwargs: object) -> dict[str, object]:
        with patch.dict(
            os.environ,
            {"OMH_HOME": str(root / ".omh"), "HERMES_HOME": str(root / ".hermes")},
        ):
            return json.loads(omh_route_answer_handler(dict(arguments), **kwargs))

    def test_an_answer_with_the_message_verifies_its_own_digest(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            payload = self._call(
                root,
                {
                    "question_digest": _digest(),
                    "answered_by": "main_model",
                    "route_choice": "plan",
                    "fits": {"plan": 0.92},
                    "message": UNDECIDABLE_MESSAGE,
                },
                session_id="session-1",
            )

        self.assertEqual(payload["status"], "recorded")
        self.assertEqual(payload["digest_verification"], "matched")
        self.assertTrue(payload["record"]["digest_verified"])
        self.assertEqual(payload["record"]["session_ref"], "session-1")

    def test_an_answer_without_the_message_is_recorded_unverified(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            payload = self._call(
                root,
                {
                    "question_digest": _digest(),
                    "answered_by": "jev_plugin",
                    "route_choice": "none",
                },
                session_id="session-1",
            )

        self.assertEqual(payload["status"], "recorded")
        self.assertEqual(payload["digest_verification"], "not_requested")
        self.assertFalse(payload["record"]["digest_verified"])
        self.assertEqual(payload["record"]["action"], "none")

    def test_a_digest_from_the_route_hint_surface_verifies_too(self) -> None:
        """The surfaces hand out different questions for one message.

        `omh chat route-hint` cuts the shortlist to `--max-hints` (2 by
        default) while `omh chat route` and `omh_interact` ask for three, so
        one message carries two digests. Verifying against a single limit
        would refuse an answer to a question OMH itself printed, and the
        refusal would tell the caller to re-read a question that produces the
        same digest again.
        """
        from omh.wrapper.route_hints import build_chat_route_hint_payload

        hint_digest = build_chat_route_hint_payload(UNDECIDABLE_MESSAGE, source="discord")[
            "route_question"
        ]["question_digest"]
        self.assertNotEqual(hint_digest, _digest("discord"))

        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            payload = self._call(
                root,
                {
                    "question_digest": hint_digest,
                    "answered_by": "main_model",
                    "route_choice": "plan",
                    "fits": {"plan": 0.9},
                    "message": UNDECIDABLE_MESSAGE,
                    "source": "discord",
                },
                session_id="session-1",
            )

        self.assertEqual(payload["status"], "recorded")
        self.assertEqual(payload["digest_verification"], "matched")
        self.assertTrue(payload["record"]["digest_verified"])

    def test_a_digest_for_a_different_question_is_refused_and_nothing_is_written(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            payload = self._call(
                root,
                {
                    "question_digest": "a" * 64,
                    "answered_by": "main_model",
                    "route_choice": "plan",
                    "message": UNDECIDABLE_MESSAGE,
                },
                session_id="session-1",
            )
            written = list(route_answer_dir(root / ".omh").glob("*.json"))

        self.assertEqual(payload["status"], "digest_mismatch")
        self.assertEqual(payload["digest_verification"], "mismatch")
        self.assertEqual(written, [])

    def test_a_message_the_router_decides_verifies_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            payload = self._call(
                root,
                {
                    "question_digest": _digest(),
                    "answered_by": "main_model",
                    "route_choice": "plan",
                    "message": "why is the build failing on main?",
                },
                session_id="session-1",
            )

        self.assertEqual(payload["status"], "recorded")
        self.assertEqual(payload["digest_verification"], "no_route_question_for_message")
        self.assertFalse(payload["record"]["digest_verified"])

    def test_an_invalid_answer_is_refused_with_a_reason(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            payload = self._call(
                root,
                {
                    "question_digest": _digest(),
                    "answered_by": "the_operator",
                    "route_choice": "plan",
                },
                session_id="session-1",
            )

        self.assertEqual(payload["status"], "invalid_request")
        self.assertIn("answered_by", payload["error"])

    def test_a_caller_chosen_home_is_refused(self) -> None:
        """A metadata recorder that took a path would be a file-create
        primitive pointed wherever the model liked."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            payload = self._call(
                root,
                {
                    "question_digest": _digest(),
                    "answered_by": "main_model",
                    "route_choice": "plan",
                    "omh_home": str(root / "elsewhere"),
                },
                session_id="session-1",
            )

        self.assertEqual(payload["status"], "invalid_request")
        self.assertFalse((root / "elsewhere").exists())

    def test_a_host_without_a_session_id_still_records(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            payload = self._call(
                root,
                {
                    "question_digest": _digest(),
                    "answered_by": "main_model",
                    "route_choice": "plan",
                },
            )

        self.assertEqual(payload["status"], "recorded")
        self.assertEqual(payload["record"]["session_ref"], "")

    def test_the_model_cannot_name_another_session(self) -> None:
        """`session_ref` comes from the host keyword only: tool arguments are
        model-supplied, and a per-session store must not let one call write
        into another session's record."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            payload = self._call(
                root,
                {
                    "question_digest": _digest(),
                    "answered_by": "main_model",
                    "route_choice": "plan",
                    "session_ref": "someone-else",
                },
                session_id="session-1",
            )

        self.assertEqual(payload["record"]["session_ref"], "session-1")


class TheScorerReadsTheRecordsTests(unittest.TestCase):
    def test_a_directory_of_records_scores_as_its_own_arm(self) -> None:
        corpus = build_routing_question_corpus(source="discord", limit=3)
        item = next(
            entry
            for entry in corpus["items"]
            if entry["corpus"] == "intervention" and entry["expected"]["choice"] != "none"
        )
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            write_route_answer(
                home,
                build_route_answer_record(
                    question_digest=item["question"]["question_digest"],
                    answered_by="jev_plugin",
                    route_choice=item["expected"]["choice"],
                    fits={item["expected"]["choice"]: 0.95},
                    session_ref="session-a",
                ),
            )
            rows = read_answer_source(route_answer_dir(home))
            score = score_routing_question_answers(corpus, rows)

        self.assertEqual([row.error for row in rows], [""])
        self.assertEqual(score["arms"]["jev_plugin"]["answered"], 1)
        self.assertEqual(score["arms"]["jev_plugin"]["malformed"], 0)
        self.assertEqual(score["arms"]["jev_plugin"]["wrong_workflow"], 0)
        self.assertEqual(score["unmatched"], [])


if __name__ == "__main__":
    unittest.main()

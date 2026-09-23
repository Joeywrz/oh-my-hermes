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

import builtins
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
    MAX_ROUTE_ANSWER_RECORDS,
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
from omh.routing.chat import route_chat_message, routing_record_payload  # noqa: E402
from omh.routing.route_question import (  # noqa: E402
    FITS_CLARIFY_THRESHOLD as CORE_FITS_CLARIFY,
    FITS_DISPATCH_THRESHOLD as CORE_FITS_DISPATCH,
    message_digest,
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

    def test_the_bundle_hashes_a_request_the_way_the_one_producer_does(self) -> None:
        """`message_digest` is core's single message-hash producer and the
        bundle cannot import it: Hermes loads that directory with its own
        interpreter, which has no reason to have `omh` on its path. So the
        bundle keeps its own `hashlib` call, and this pins the two together
        the way the schema strings and thresholds above are pinned. If they
        ever disagree the join breaks silently."""
        from omh.plugin_bundle.omh.tools.route_answer_tool import _resolved_message_sha256

        for message in (UNDECIDABLE_MESSAGE, "why is the build failing on main?", "日本語"):
            with self.subTest(message=message):
                self.assertEqual(
                    _resolved_message_sha256({"message": message}), message_digest(message)
                )

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

    def test_the_record_and_its_row_both_name_the_request(self) -> None:
        """The digest covers the shortlist, and one shortlist serves every
        request the router could not place, so the digest alone joins an answer
        to the wrong corpus item. The row repeats the hash because a row read
        out of a JSONL file arrives without the record around it."""
        digest = message_digest(UNDECIDABLE_MESSAGE)
        record = build_route_answer_record(
            question_digest="ab12",
            answered_by="main_model",
            route_choice="plan",
            message_sha256=digest,
        )

        self.assertEqual(record["message_sha256"], digest)
        self.assertEqual(record["answer"]["message_sha256"], digest)

    def test_an_unidentified_request_is_an_empty_hash_and_not_a_hash_of_nothing(self) -> None:
        record = build_route_answer_record(
            question_digest="ab12", answered_by="main_model", route_choice="none"
        )

        self.assertEqual(record["message_sha256"], "")
        self.assertEqual(record["answer"]["message_sha256"], "")

    def test_a_hash_that_is_not_a_sha256_is_refused(self) -> None:
        """Exactly 64 hex, not at most: a shorter hex string is some other
        hash, and a record must not claim an identity nobody can reproduce."""
        for value in ("abc123", "Z" * 64, "ab" * 33, "AB" * 32):
            with self.subTest(value=value):
                with self.assertRaises(RouteAnswerValidationError):
                    build_route_answer_record(
                        question_digest="ab12",
                        answered_by="main_model",
                        route_choice="none",
                        message_sha256=value,
                    )

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

    def test_a_full_directory_refuses_a_new_record_and_keeps_every_old_one(self) -> None:
        """Age is the only thing that removes a record, which leaves no bound
        inside the seven-day window: the digest is a caller-chosen field, so a
        caller can add a file per call. Evicting for room would discard the
        measurement the record exists for, so the write is refused instead."""
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            directory = route_answer_dir(home)
            directory.mkdir(parents=True)
            for index in range(MAX_ROUTE_ANSWER_RECORDS):
                (directory / f"{index:016x}.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaises(RouteAnswerStoreError) as refusal:
                write_route_answer(home, self._record(question_digest="beef"))
            survivors = len(list(directory.glob("*.json")))

        self.assertEqual(survivors, MAX_ROUTE_ANSWER_RECORDS)
        self.assertIn("score the recorded answers", str(refusal.exception))

    def test_a_full_directory_still_lets_an_answerer_revise_its_own_record(self) -> None:
        """Replacing a record changes no count, and an answerer correcting
        itself is ordinary; only a NEW file is refused."""
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            first = write_route_answer(home, self._record(route_choice="plan"))
            directory = route_answer_dir(home)
            for index in range(MAX_ROUTE_ANSWER_RECORDS):
                (directory / f"{index:016x}.json").write_text("{}\n", encoding="utf-8")

            write_route_answer(home, self._record(route_choice="deep-interview"))
            latest = json.loads(first.read_text(encoding="utf-8"))

        self.assertEqual(latest["route_choice"], "deep-interview")

    def test_the_stored_action_records_the_thresholds_that_produced_it(self) -> None:
        """The scorer resolves the action again from the corpus's thresholds,
        so the stored value can differ from the one anyone uses. Recording the
        thresholds beside it makes the stored number reproducible instead of
        something a reader has to guess about."""
        record = self._record(fits={"plan": 0.6})

        self.assertEqual(record["action"], "clarify")
        self.assertEqual(
            record["action_thresholds"],
            {"fits_clarify": FITS_CLARIFY_THRESHOLD, "fits_dispatch": FITS_DISPATCH_THRESHOLD},
        )

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

    def test_the_derived_request_hash_is_the_one_every_other_surface_reports(self) -> None:
        """The join is only a join if both sides hash the same thing. OMH's
        wrapper surfaces hash the RAW message, not the routing text the
        shortlist is matched on, so this pins the tool against one of them
        rather than against a second copy of the rule."""
        from omh.routing.chat import route_chat_message, routing_record_payload

        expected = routing_record_payload(
            route_chat_message(UNDECIDABLE_MESSAGE, source="discord"),
            UNDECIDABLE_MESSAGE,
        )["message_sha256"]
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            payload = self._call(
                root,
                {
                    "question_digest": _digest("discord"),
                    "answered_by": "main_model",
                    "route_choice": "plan",
                    "message": UNDECIDABLE_MESSAGE,
                    "source": "discord",
                },
                session_id="session-1",
            )

        self.assertEqual(payload["record"]["message_sha256"], expected)

    def test_a_caller_with_the_hash_but_not_the_text_still_identifies_its_request(self) -> None:
        supplied = message_digest(UNDECIDABLE_MESSAGE)
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            payload = self._call(
                root,
                {
                    "question_digest": _digest(),
                    "answered_by": "jev_plugin",
                    "route_choice": "none",
                    "message_sha256": supplied,
                },
                session_id="session-1",
            )

        self.assertEqual(payload["status"], "recorded")
        self.assertEqual(payload["record"]["message_sha256"], supplied)
        self.assertEqual(payload["digest_verification"], "not_requested")

    def test_a_message_and_a_hash_that_disagree_are_refused(self) -> None:
        """They name two different requests. Picking one would record the
        answer against a request the caller did not mean."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            payload = self._call(
                root,
                {
                    "question_digest": _digest(),
                    "answered_by": "main_model",
                    "route_choice": "plan",
                    "message": UNDECIDABLE_MESSAGE,
                    "message_sha256": "0" * 64,
                },
                session_id="session-1",
            )
            written = list(route_answer_dir(root / ".omh").glob("*.json"))

        self.assertEqual(payload["status"], "invalid_request")
        self.assertEqual(written, [])

    def test_the_message_never_reaches_the_observation_lane(self) -> None:
        """The schema tells the model the request text is used once and never
        stored, and that sentence is the argument for supplying it. The shared
        observation metadata lifts `message` out of a tool's own arguments and
        writes it verbatim once `observation.host` is supplied, so the promise
        is kept by withholding the field rather than by softening the words."""
        secret = UNDECIDABLE_MESSAGE + " PROMPTBODYMARKER"
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self._call(
                root,
                {
                    "question_digest": _digest(),
                    "answered_by": "main_model",
                    "route_choice": "none",
                    "message": secret,
                    "observation": {"host": "hermes"},
                },
                session_id="session-1",
            )
            written = "".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in root.rglob("*")
                if path.is_file()
            )

        self.assertNotIn("PROMPTBODYMARKER", written)

    def test_a_missing_submodule_degrades_the_field_instead_of_raising(self) -> None:
        """`exc.name` is the MISSING name, which is the bare package only when
        the whole package is gone. A current bundle beside a lagging package
        reports `omh.routing.route_question`, and an equality check on `omh`
        re-raised that out of the handler on every call carrying a message."""
        import omh.plugin_bundle.omh.tools.route_answer_tool as module

        real_import = builtins.__import__

        def missing_submodule(name, *rest):
            if name == "omh.routing.chat":
                raise ModuleNotFoundError(
                    "No module named 'omh.routing.route_question'",
                    name="omh.routing.route_question",
                )
            return real_import(name, *rest)

        with patch.object(builtins, "__import__", missing_submodule):
            verified, verification, mismatch, _question = module._verify_digest(
                {"message": UNDECIDABLE_MESSAGE, "question_digest": "ab" * 32}
            )

        self.assertFalse(verified)
        self.assertFalse(mismatch)
        self.assertEqual(verification, "unavailable_without_package_backend")

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

    def test_a_record_written_from_a_live_route_joins_to_its_corpus_item(self) -> None:
        """The case above feeds the corpus its own digest, which proves the
        reader reads and cannot prove the producer's output is readable. This
        one takes the digest off a LIVE route, the only digest anything in
        production hands out, and scores it. It used to come back
        `answered: 0` with the record under `unmatched`, because the live
        digest was the handoff's while the corpus carried the question
        block's."""
        corpus = build_routing_question_corpus(source="discord", limit=3)
        for item in corpus["items"]:
            live = route_chat_message(item["message"], source="discord", limit=3)
            question = live.get("route_question")
            if (
                isinstance(question, dict)
                and question["question_digest"] == item["question"]["question_digest"]
            ):
                break
        else:  # pragma: no cover - the corpus always contains such an item
            self.fail("no corpus item whose live route reproduces its question digest")

        choice = item["expected"]["choice"]
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            write_route_answer(
                home,
                build_route_answer_record(
                    question_digest=question["question_digest"],
                    answered_by="main_model",
                    route_choice=choice,
                    fits={choice: 0.95} if choice != "none" else {},
                    session_ref="session-live",
                    message_sha256=routing_record_payload(live, item["message"])["message_sha256"],
                ),
            )
            score = score_routing_question_answers(
                corpus, read_answer_source(route_answer_dir(home))
            )

        self.assertEqual(score["unmatched"], [])
        self.assertEqual(score["arms"]["main_model"]["answered"], 1)
        self.assertEqual(score["arms"]["main_model"]["malformed"], 0)


if __name__ == "__main__":
    unittest.main()

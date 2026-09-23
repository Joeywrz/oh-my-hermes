"""`omh_jev_ask`: the one opt-in network path, and every way it refuses to become more.

No test here opens a socket. The transport is an injected callable returning a
`TransportReply`, and the only test of the real opener checks that its
redirect handler refuses a 3xx without contacting anything. A planted sentinel
key is checked against every byte the tool returns, writes to the ledger, and
records as an observation, across every status.
"""

from __future__ import annotations

import json
import os
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _credential_fixtures import AWS_ACCESS_KEY_ID
from _local_package import load_local_package

load_local_package()
from omh.coding.model_contracts import (  # noqa: E402
    DECLARED_MODEL_CONTRACT_PROJECTIONS,
    MODEL_CONTRACTS,
)
from omh.plugin_bundle.omh import jev_ask_client as client  # noqa: E402
from omh.plugin_bundle.omh.jev_ask_client import TransportReply  # noqa: E402
from omh.plugin_bundle.omh.jev_ask_store import (  # noqa: E402
    ROUTE_KEY_NAMES,
    ledger_path,
    route_available,
)
from omh.plugin_bundle.omh.jev_consent import (  # noqa: E402
    consent_observed,
    message_requests_jev,
    note_turn,
    reset_turn_markers,
)
from omh.plugin_bundle.omh.jev_sidekick import is_jev_tool_name  # noqa: E402
from omh.plugin_bundle.omh.tools.jev_ask_tool import (  # noqa: E402
    OMH_JEV_ASK_SCHEMA,
    jev_ask_available,
    omh_jev_ask_handler,
)
from omh.plugin_bundle.omh.tools.route_answer_tool import omh_route_answer_handler  # noqa: E402
from omh.routing.chat import route_chat_message  # noqa: E402

SENTINEL = "sk-SENTINEL-4f1e9a7c2b8d0e6f"
SESSION = "session-jev-1"
QUESTION = {"q": {"type": "noul", "instructions": "Does the README cover installation?"}}
STATE = "## Install\npip install oh-my-hermes"


def _answered_body(**overrides: object) -> bytes:
    body: dict[str, object] = {
        "model": "jev-1.13.0",
        "answers": {"q": {"type": "noul", "noul": 0.82}},
        "usage": {"input_tokens": 120, "output_tokens": 4},
    }
    body.update(overrides)
    return json.dumps(body).encode("utf-8")


class _Recorder:
    """A transport that replays scripted replies and records what it was sent."""

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.requests: list[object] = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply


class _Home:
    def __init__(self, tmp: str, env: dict[str, str] | None = None) -> None:
        self.root = Path(tmp).resolve()
        self.omh = self.root / ".omh"
        self.env = {"OMH_HOME": str(self.omh), "HERMES_HOME": str(self.root / ".hermes")}
        self.env.update(env or {})

    def call(self, args: dict[str, object], transport=None, session: str = SESSION) -> dict[str, object]:
        with patch.dict(os.environ, self.env, clear=False):
            for name in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY"):
                if name not in self.env:
                    os.environ.pop(name, None)
            return json.loads(omh_jev_ask_handler(dict(args), transport=transport, session_id=session))

    def every_written_byte(self) -> str:
        chunks = []
        for path in self.root.rglob("*"):
            if path.is_file():
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(chunks)


class ConsentGateTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_turn_markers()

    def test_without_a_marker_nothing_is_sent(self) -> None:
        transport = _Recorder(TransportReply(200, {}, _answered_body()))
        with TemporaryDirectory() as tmp:
            result = _Home(tmp, {"TYPESAFE_API_KEY": SENTINEL}).call({"state": STATE, "questions": QUESTION}, transport)
        self.assertEqual(result["status"], "consent_not_observed")
        self.assertFalse(result["ok"])
        self.assertIsNone(result["answers"])
        self.assertEqual(transport.requests, [])

    def test_the_marker_is_this_turn_only(self) -> None:
        note_turn(SESSION, "ask jev whether the readme covers install")
        self.assertTrue(consent_observed(SESSION))
        note_turn(SESSION, "thanks, now fix the typo")
        self.assertFalse(consent_observed(SESSION))

    def test_a_bare_yes_or_another_session_is_not_consent(self) -> None:
        note_turn(SESSION, "yes")
        self.assertFalse(consent_observed(SESSION))
        note_turn("other", "ask jev")
        self.assertFalse(consent_observed(SESSION))

    def test_a_delegated_or_unattended_turn_has_no_marker(self) -> None:
        note_turn(SESSION, "ask jev about this", delegated=True)
        self.assertFalse(consent_observed(SESSION))
        for platform in ("cron", "subagent", "CRON"):
            with self.subTest(platform=platform):
                note_turn(SESSION, "ask jev about this", platform=platform)
                self.assertFalse(consent_observed(SESSION))
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_1"}):
            note_turn(SESSION, "ask jev about this", platform="cli")
        self.assertFalse(consent_observed(SESSION))
        self.assertFalse(consent_observed(""))

    def test_host_added_text_is_not_the_persons_consent(self) -> None:
        # A native reply to the bot's own offer carries the offer's text, and
        # an attachment's inlined text can name Jev; neither is the person.
        offer = '[Replying to your previous message: "I can send the diff to Jev; reply `ask jev` to send it."]'
        for message in (
            offer + "\n\nyes",
            offer + "\n\nsure, go ahead",
            "[The user sent a text document: 'notes.md'. Its content has been included below. "
            "The file is also saved at: /tmp/notes.md]\n\nask jev about deploys\n\nsummarize this",
            "[Content of notes.md]:\nwe should ask jev\n\nsummarize this",
            '[Replying to: "unterminated quote',
        ):
            with self.subTest(message=message[:50]):
                self.assertFalse(message_requests_jev(message))
                note_turn(SESSION, message)
                self.assertFalse(consent_observed(SESSION))
        # The person's own text after the host block still counts.
        self.assertTrue(message_requests_jev(offer + "\n\nok, ask jev"))

    def test_a_cron_turn_through_the_hook_sends_nothing(self) -> None:
        from omh.plugin_bundle.omh.hooks.llm_hooks import pre_llm_call

        transport = _Recorder(TransportReply(200, {}, _answered_body()))
        with TemporaryDirectory() as tmp:
            home = _Home(tmp, {"TYPESAFE_API_KEY": SENTINEL})
            with patch.dict(os.environ, home.env):
                pre_llm_call(user_message="[cron job] every hour: ask jev whether the deploy is healthy",
                             session_id="cron_abc_1", platform="cron", is_first_turn=True,
                             omh_home=str(home.omh), hermes_home=str(home.root / ".hermes"))
            self.assertFalse(consent_observed("cron_abc_1"))
            result = home.call({"state": STATE, "questions": QUESTION}, transport, session="cron_abc_1")
        self.assertEqual(result["status"], "consent_not_observed")
        self.assertEqual(transport.requests, [])

    def test_the_hook_reads_the_request_not_a_host_row_or_a_delegated_child(self) -> None:
        from omh.plugin_bundle.omh.hooks.llm_hooks import pre_llm_call
        from omh.plugin_bundle.omh.hooks.nudge_budget import note_delegated_session

        with TemporaryDirectory() as tmp:
            home = _Home(tmp)
            kwargs = {"is_first_turn": False, "omh_home": str(home.omh), "hermes_home": str(home.root / ".hermes")}
            with patch.dict(os.environ, home.env):
                notice = "background process finished: ask jev output ready"
                pre_llm_call(user_message=notice, session_id="host-1", **kwargs,
                             conversation_history=[{"role": "user", "content": notice,
                                                    "display_kind": "process_complete"}])
                self.assertFalse(consent_observed("host-1"))
                note_delegated_session("child-1", omh_home=str(home.omh))
                pre_llm_call(user_message="ask jev about this", session_id="child-1", **kwargs)
                self.assertFalse(consent_observed("child-1"))

    def test_a_degraded_turn_does_not_keep_the_previous_marker(self) -> None:
        from omh.plugin_bundle.omh import runtime_paths
        from omh.plugin_bundle.omh.hooks.llm_hooks import pre_llm_call

        note_turn("hook-2", "ask jev whether this is done")
        self.assertTrue(consent_observed("hook-2"))
        with patch.object(runtime_paths, "plugin_home", side_effect=runtime_paths.RuntimeBindingError("unbound")):
            pre_llm_call(user_message="carry on", session_id="hook-2", is_first_turn=False)
        self.assertFalse(consent_observed("hook-2"))

    def test_the_token_rule(self) -> None:
        for message in ("ask jev", "jev로 확인해줘", "use omh-jev-review-gate", "JEV check", "(jev)", "omh_jev_ask"):
            with self.subTest(message=message):
                self.assertTrue(message_requests_jev(message))
        for message in ("", "ask claude", "rejev the thing", "prejevity"):
            with self.subTest(message=message):
                self.assertFalse(message_requests_jev(message))

    def test_pre_llm_call_records_the_marker_from_the_request(self) -> None:
        from omh.plugin_bundle.omh.hooks.llm_hooks import pre_llm_call

        with TemporaryDirectory() as tmp:
            home = _Home(tmp)
            with patch.dict(os.environ, home.env):
                pre_llm_call(user_message="ask jev if this is done", session_id="hook-1", is_first_turn=False,
                             omh_home=str(home.omh), hermes_home=str(home.root / ".hermes"))
                self.assertTrue(consent_observed("hook-1"))
                pre_llm_call(user_message="carry on", session_id="hook-1", is_first_turn=False,
                             omh_home=str(home.omh), hermes_home=str(home.root / ".hermes"))
                self.assertFalse(consent_observed("hook-1"))


class _ConsentedCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_turn_markers()
        note_turn(SESSION, "ask jev whether the readme covers install")


class StatusTableTests(_ConsentedCase):
    CASES = (
        (TransportReply(400, {}, b'{"error":"bad"}'), "rejected_by_api"),
        (TransportReply(404, {}, b""), "rejected_by_api"),
        (TransportReply(413, {}, b""), "rejected_by_api"),
        (TransportReply(422, {}, b""), "rejected_by_api"),
        (TransportReply(401, {}, b""), "auth_failed"),
        (TransportReply(402, {}, b""), "payment_required"),
        (TransportReply(403, {}, b""), "permission_denied"),
        (TransportReply(429, {"retry-after": "120"}, b""), "rate_limited"),
        (TransportReply(503, {"retry-after": "120"}, b""), "overloaded"),
        (TransportReply(529, {"retry-after": "120"}, b""), "overloaded"),
        (TransportReply(500, {"retry-after": "120"}, b""), "server_error"),
        (TransportReply(502, {"retry-after": "120"}, b""), "server_error"),
        (TransportReply(524, {"retry-after": "120"}, b""), "timeout"),
        (TransportReply(504, {"retry-after": "120"}, b""), "timeout"),
        (TransportReply(408, {"retry-after": "120"}, b""), "server_error"),
        (TransportReply(302, {}, b""), "rejected_redirect"),
        (TimeoutError("timed out"), "timeout"),
        (ConnectionRefusedError("refused"), "network_error"),
        (TransportReply(200, {}, b"not json"), "malformed_response"),
    )

    def test_every_row_is_a_non_answer_and_leaks_no_key(self) -> None:
        for reply, expected in self.CASES:
            with self.subTest(expected=expected, reply=repr(reply)[:60]), TemporaryDirectory() as tmp:
                home = _Home(tmp, {"TYPESAFE_API_KEY": SENTINEL})
                result = home.call({"state": STATE, "questions": QUESTION}, _Recorder(reply))
                self.assertEqual(result["status"], expected)
                self.assertFalse(result["ok"])
                self.assertIsNone(result["answers"])
                self.assertNotIn(SENTINEL, json.dumps(result))
                self.assertNotIn(SENTINEL, home.every_written_byte())
                self.assertEqual(result["ledger"], "written")

    def test_an_answer_is_the_only_ok_status(self) -> None:
        with TemporaryDirectory() as tmp:
            home = _Home(tmp, {"TYPESAFE_API_KEY": SENTINEL})
            transport = _Recorder(TransportReply(200, {}, _answered_body()))
            result = home.call({"state": STATE, "questions": QUESTION, "purpose": "readme check"}, transport)
            self.assertEqual(result["status"], "answered")
            self.assertTrue(result["ok"])
            self.assertEqual(result["answers"]["q"]["noul"], 0.82)
            self.assertEqual(result["usage"]["cost_source"], "estimated_from_list_price")
            self.assertEqual(result["contract_model_id"], "jev-1.13.0")
            request = transport.requests[0]
            self.assertEqual(request.full_url, "https://api.typesafe.ai/v1/systemone")
            self.assertEqual(set(json.loads(request.data)), {"model", "state", "questions"})
            ledger = ledger_path(home.omh).read_text(encoding="utf-8")
            self.assertNotIn("README", ledger)
            self.assertNotIn("pip install", ledger)
            self.assertNotIn(SENTINEL, home.every_written_byte())

    def test_the_status_vocabulary_has_exactly_one_success(self) -> None:
        self.assertEqual(client.SUCCESS_STATUS, "answered")
        self.assertEqual(len(set(client.ASK_STATUSES)), len(client.ASK_STATUSES))
        self.assertNotIn(client.SUCCESS_STATUS, client.RETRYABLE_STATUSES)
        for code in range(100, 600):
            status = client._status_for_http(code)
            self.assertIn(status, client.ASK_STATUSES)
            if status == client.SUCCESS_STATUS:
                self.assertEqual(code, 200)

    def test_a_key_straddling_the_excerpt_cut_leaks_no_fragment(self) -> None:
        # The scrub runs over the whole body before the cut, and a fragment a
        # cut leaves at the end is redacted too.
        for pad in range(client.MAX_API_ERROR_CHARS - 20, client.MAX_API_ERROR_CHARS + 2):
            body = ("x" * pad + " invalid key " + SENTINEL).encode("utf-8")
            with self.subTest(pad=pad):
                excerpt = client._api_error_excerpt(body, SENTINEL)
                self.assertLessEqual(len(excerpt), client.MAX_API_ERROR_CHARS + len("[redacted]"))
                for length in range(client.MIN_KEY_FRAGMENT_CHARS, len(SENTINEL) + 1):
                    self.assertFalse(excerpt.endswith(SENTINEL[:length]), excerpt[-20:])
                self.assertNotIn(SENTINEL[:8], excerpt)
        long_body = (b" " * 1180) + b"invalid key " + SENTINEL.encode("utf-8")
        self.assertNotIn(SENTINEL[:6], client._api_error_excerpt(long_body, SENTINEL))

    def test_a_key_echoed_in_the_served_model_is_scrubbed(self) -> None:
        with TemporaryDirectory() as tmp:
            home = _Home(tmp, {"TYPESAFE_API_KEY": SENTINEL})
            result = home.call({"state": STATE, "questions": QUESTION},
                               _Recorder(TransportReply(200, {}, _answered_body(model="jev " + SENTINEL))))
            self.assertEqual(result["status"], "answered")
            self.assertNotIn(SENTINEL, json.dumps(result))
            self.assertNotIn(SENTINEL[:8], result["served_model"])
            self.assertNotIn(SENTINEL, home.every_written_byte())

    def test_an_api_error_excerpt_is_scrubbed_of_the_key_and_bounded(self) -> None:
        body = (("echo " + SENTINEL + " ") * 200).encode("utf-8")
        with TemporaryDirectory() as tmp:
            result = _Home(tmp, {"TYPESAFE_API_KEY": SENTINEL}).call(
                {"state": STATE, "questions": QUESTION}, _Recorder(TransportReply(400, {}, body))
            )
        self.assertNotIn(SENTINEL, json.dumps(result))
        self.assertLessEqual(len(result["api_error"]), client.MAX_API_ERROR_CHARS)


class RetryTests(unittest.TestCase):
    def _send(self, transport, clock_values):
        clock = iter(clock_values)
        now = [0.0]

        def fake_clock() -> float:
            try:
                now[0] = next(clock)
            except StopIteration:
                pass
            return now[0]

        slept: list[float] = []
        return (
            client.send_ask(
                route="typesafe", key=SENTINEL, body=b"{}", user_agent="t", transport=transport,
                sleep=slept.append, clock=fake_clock, jitter=lambda: 0.0,
            ),
            slept,
        )

    def test_retry_after_inside_the_budget_is_honored_then_answered(self) -> None:
        transport = _Recorder(TransportReply(429, {"retry-after": "1"}, b""), TransportReply(200, {}, b"{}"))
        result, slept = self._send(transport, [0.0] * 20)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(slept, [1.0])

    def test_retry_after_past_the_budget_is_reported_not_slept(self) -> None:
        transport = _Recorder(TransportReply(429, {"retry-after": "120"}, b""))
        result, slept = self._send(transport, [0.0] * 20)
        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(result["retry_after_s"], 120.0)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(slept, [])

    def test_at_most_two_retries(self) -> None:
        transport = _Recorder(TransportReply(500, {}, b""))
        result, slept = self._send(transport, [0.0] * 40)
        self.assertEqual(result["status"], "server_error")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(len(slept), 2)

    def test_no_retry_after_a_timeout_or_a_connection_failure(self) -> None:
        for error, status in ((TimeoutError("t"), "timeout"), (ConnectionResetError("r"), "network_error")):
            with self.subTest(status=status):
                transport = _Recorder(error)
                result, slept = self._send(transport, [0.0] * 20)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["attempts"], 1)
                self.assertEqual(slept, [])

    def test_the_response_read_is_bounded(self) -> None:
        # Valid JSON padded past the bound: only the size check refuses it.
        big = TransportReply(200, {}, _answered_body() + b" " * client.MAX_RESPONSE_BYTES)
        result, _ = self._send(_Recorder(big), [0.0] * 20)
        self.assertEqual(result["status"], "malformed_response")

    def test_a_gateway_timeout_is_not_retried(self) -> None:
        for code in (504, 524):
            with self.subTest(code=code):
                transport = _Recorder(TransportReply(code, {}, b""))
                result, slept = self._send(transport, [0.0] * 20)
                self.assertEqual(result["status"], "timeout")
                self.assertEqual(result["attempts"], 1)
                self.assertEqual(slept, [])

    def test_a_trickling_body_stops_at_the_deadline(self) -> None:
        class Trickle:
            def read1(self, size: int) -> bytes:
                return b"x"

        now = [0.0]

        def clock() -> float:
            now[0] += 1.0
            return now[0]

        with self.assertRaises(TimeoutError):
            client._read_bounded(Trickle(), deadline=5.0, clock=clock)


class TransportSafetyTests(unittest.TestCase):
    def test_the_real_opener_refuses_every_redirect(self) -> None:
        handler = client._RefuseRedirects()
        for code in (301, 302, 303, 307, 308):
            self.assertIsNone(handler.redirect_request(None, None, code, "moved", {}, "https://elsewhere.example/"))

    def test_routes_are_https_and_the_two_tables_agree(self) -> None:
        for route, (url, name) in client.ROUTES.items():
            self.assertTrue(url.startswith("https://"))
            self.assertEqual(ROUTE_KEY_NAMES[route], name)
        self.assertEqual(set(client.ROUTES), set(ROUTE_KEY_NAMES))

    def test_a_request_body_over_the_bound_is_refused_locally(self) -> None:
        with self.assertRaises(client.AskRequestError):
            client.build_request_body("jev-latest", "x" * (client.MAX_REQUEST_BYTES + 1), QUESTION)

    def test_base_url_variables_are_ignored(self) -> None:
        with patch.dict(os.environ, {"TYPESAFE_BASE_URL": "https://attacker.example"}):
            self.assertEqual(client.ROUTES["typesafe"][0], "https://api.typesafe.ai/v1/systemone")


class ValidationTests(_ConsentedCase):
    def _call(self, args, reply=None):
        with TemporaryDirectory() as tmp:
            transport = _Recorder(reply or TransportReply(200, {}, _answered_body()))
            result = _Home(tmp, {"TYPESAFE_API_KEY": SENTINEL}).call(args, transport)
            return result, transport

    def test_local_refusals_open_no_socket(self) -> None:
        bad = (
            {"state": "", "questions": QUESTION},
            {"state": STATE},
            {"state": STATE, "questions": QUESTION, "preset": "done_check/v1"},
            {"state": STATE, "questions": {"q": {"type": "maybe", "instructions": "x"}}},
            {"state": STATE, "questions": {"q": {"type": "choice", "instructions": "x",
                                                 "criteria": {str(i): None for i in range(256)}}}},
            {"state": STATE, "questions": {"q": {"type": "score", "instructions": "x", "criteria": ["one"]}}},
            {"state": STATE, "questions": QUESTION, "model": "gpt-6"},
            {"state": STATE, "questions": QUESTION, "route": "elsewhere"},
            {"state": {"command": "ls"}, "preset": "done_check/v1"},
            {"state": f"aws_access_key_id={AWS_ACCESS_KEY_ID} was pasted", "questions": QUESTION},
        )
        for args in bad:
            with self.subTest(args=json.dumps(args)[:80]):
                result, transport = self._call(args)
                self.assertEqual(result["status"], "invalid_request")
                self.assertIsNone(result["answers"])
                self.assertEqual(transport.requests, [])

    def test_bad_model_supplied_fields_are_refused_before_a_socket(self) -> None:
        for args in (
            {"state": STATE, "questions": QUESTION, "preset": "bogus/v9"},
            {"state": {"error_excerpt": "x"}, "preset": "failure_triage/v1", "attempts_so_far": "two"},
            {"state": {"error_excerpt": "x"}, "preset": "failure_triage/v1", "attempts_so_far": -3},
            {"state": {"error_excerpt": "x"}, "preset": "failure_triage/v1", "attempts_so_far": True},
            {"state": {"error_excerpt": "x"}, "preset": "failure_triage/v1", "attempts_so_far": 1.7},
            {"state": STATE, "route_question": {"schema_version": "route_question/v1",
                                                "question_digest": "tsk_PLANTED_digest",
                                                "questions": QUESTION}},
        ):
            with self.subTest(args=json.dumps(args)[:90]), TemporaryDirectory() as tmp:
                home = _Home(tmp, {"TYPESAFE_API_KEY": SENTINEL})
                transport = _Recorder(TransportReply(200, {}, _answered_body()))
                result = home.call(args, transport)
                self.assertEqual(result["status"], "invalid_request")
                self.assertFalse(result["ok"])
                self.assertEqual(transport.requests, [])
                self.assertEqual(result["ledger"], "written")
                self.assertNotIn("policy_result", result)
                ledger = ledger_path(home.omh).read_text(encoding="utf-8")
                self.assertNotIn("bogus", ledger)
                self.assertNotIn("PLANTED", ledger)

    def test_a_key_shaped_purpose_is_not_stored(self) -> None:
        with TemporaryDirectory() as tmp:
            home = _Home(tmp, {"TYPESAFE_API_KEY": SENTINEL})
            result = home.call({"state": STATE, "questions": QUESTION,
                                "purpose": f"p aws_access_key_id={AWS_ACCESS_KEY_ID}"},
                               _Recorder(TransportReply(200, {}, _answered_body())))
            self.assertEqual(result["status"], "answered")
            self.assertNotIn(AWS_ACCESS_KEY_ID, ledger_path(home.omh).read_text(encoding="utf-8"))

    def test_a_preset_sends_the_pinned_version(self) -> None:
        with TemporaryDirectory() as tmp:
            transport = _Recorder(TransportReply(200, {}, _answered_body()))
            _Home(tmp, {"TYPESAFE_API_KEY": SENTINEL}).call(
                {"state": {"claim": "tests pass", "evidence_excerpt": "5 passed", "goal": "fix"},
                 "preset": "done_check/v1"}, transport)
            self.assertEqual(json.loads(transport.requests[0].data)["model"], "jev-1.13.0")

    def test_every_reply_check_fails_closed(self) -> None:
        replies = (
            _answered_body(answers={}),
            _answered_body(answers={"q": {"type": "noul", "noul": 0.5}, "extra": {"type": "noul", "noul": 0.5}}),
            _answered_body(answers={"q": {"type": "score", "score": 1}}),
            _answered_body(answers={"q": {"type": "noul", "noul": 1.5}}),
            _answered_body(answers={"q": {"type": "noul", "noul": "high"}}),
            _answered_body(usage={"input_tokens": -1, "output_tokens": 0}),
            _answered_body(usage=None),
        )
        for body in replies:
            with self.subTest(body=body[:80]):
                result, _ = self._call({"state": STATE, "questions": QUESTION}, TransportReply(200, {}, body))
                self.assertEqual(result["status"], "malformed_response")
                self.assertIsNone(result["answers"])

    def test_a_choice_outside_the_sent_options_is_malformed(self) -> None:
        questions = {"c": {"type": "choice", "instructions": "pick", "criteria": {"a": None, "b": None}}}
        body = _answered_body(answers={"c": {"type": "choice", "choice": "z", "probabilities": {"a": 0.5, "b": 0.5},
                                             "confidence": 0.5}})
        result, _ = self._call({"state": STATE, "questions": questions}, TransportReply(200, {}, body))
        self.assertEqual(result["status"], "malformed_response")


class KeyResolutionTests(_ConsentedCase):
    def _with_secret_scope(self, getter):
        agent = types.ModuleType("agent")
        scope = types.ModuleType("agent.secret_scope")

        class UnscopedSecretError(RuntimeError):
            pass

        scope.UnscopedSecretError = UnscopedSecretError
        scope.get_secret = lambda name: getter(name, UnscopedSecretError)
        return patch.dict(sys.modules, {"agent": agent, "agent.secret_scope": scope})

    def test_the_host_reader_is_used_when_present(self) -> None:
        with self._with_secret_scope(lambda name, _err: SENTINEL if name == "TYPESAFE_API_KEY" else None):
            with TemporaryDirectory() as tmp:
                transport = _Recorder(TransportReply(200, {}, _answered_body()))
                result = _Home(tmp).call({"state": STATE, "questions": QUESTION}, transport)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(transport.requests[0].get_header("Authorization"), f"Bearer {SENTINEL}")

    def test_a_refused_scoped_read_never_falls_back_to_the_environment(self) -> None:
        def refuse(_name, error):
            raise error("unscoped")

        with self._with_secret_scope(refuse):
            with TemporaryDirectory() as tmp:
                transport = _Recorder(TransportReply(200, {}, _answered_body()))
                result = _Home(tmp, {"TYPESAFE_API_KEY": SENTINEL}).call(
                    {"state": STATE, "questions": QUESTION}, transport
                )
        self.assertEqual(result["status"], "key_unresolvable")
        self.assertEqual(transport.requests, [])

    def test_both_keys_prefer_typesafe_and_a_forced_missing_route_does_not_fall_back(self) -> None:
        with TemporaryDirectory() as tmp:
            home = _Home(tmp, {"TYPESAFE_API_KEY": SENTINEL, "OPENROUTER_API_KEY": "or-" + SENTINEL})
            (home.omh / "jev").mkdir(parents=True, exist_ok=True)
            (home.omh / "jev" / "settings.json").write_text('{"openrouter_route": true}', encoding="utf-8")
            transport = _Recorder(TransportReply(200, {}, _answered_body()))
            self.assertEqual(home.call({"state": STATE, "questions": QUESTION}, transport)["route"], "typesafe")
        with TemporaryDirectory() as tmp:
            transport = _Recorder(TransportReply(200, {}, _answered_body()))
            result = _Home(tmp, {"TYPESAFE_API_KEY": SENTINEL}).call(
                {"state": STATE, "questions": QUESTION, "route": "openrouter"}, transport
            )
            self.assertEqual(result["status"], "key_missing")
            self.assertEqual(transport.requests, [])

    def test_an_openrouter_key_alone_is_not_a_route(self) -> None:
        with TemporaryDirectory() as tmp:
            home = _Home(tmp, {"OPENROUTER_API_KEY": SENTINEL})
            transport = _Recorder(TransportReply(200, {}, _answered_body()))
            self.assertEqual(home.call({"state": STATE, "questions": QUESTION}, transport)["status"], "key_missing")
            self.assertEqual(transport.requests, [])
            (home.omh / "jev").mkdir(parents=True, exist_ok=True)
            (home.omh / "jev" / "settings.json").write_text('{"openrouter_route": true}', encoding="utf-8")
            body = _answered_body(model="typesafe/jev-1.13-20260917",
                                  usage={"input_tokens": 10, "output_tokens": 1, "cost": 0.0000004})
            result = home.call({"state": STATE, "questions": QUESTION}, _Recorder(TransportReply(200, {}, body)))
            self.assertEqual(result["route"], "openrouter")
            self.assertEqual(result["usage"]["cost_source"], "reported_by_gateway")
            self.assertEqual(result["contract_resolution"], "unresolved")

    def test_check_fn_is_false_without_a_key_and_never_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            env = {"OMH_HOME": str(Path(tmp) / ".omh")}
            with patch.dict(os.environ, env):
                os.environ.pop("TYPESAFE_API_KEY", None)
                os.environ.pop("OPENROUTER_API_KEY", None)
                self.assertFalse(jev_ask_available())
                os.environ["OPENROUTER_API_KEY"] = SENTINEL
                self.assertFalse(jev_ask_available())
                os.environ["TYPESAFE_API_KEY"] = SENTINEL
                self.assertTrue(jev_ask_available())
                self.assertEqual(route_available(Path(tmp) / ".omh"), "typesafe")


class ParityTests(unittest.TestCase):
    def test_the_model_id_mirror_matches_the_contract_table(self) -> None:
        contract = MODEL_CONTRACTS["jev-1.13.0"]
        expected = set(contract["served_ids"].values()) | {
            alias for alias, row in DECLARED_MODEL_CONTRACT_PROJECTIONS.items()
            if row["contract_model_id"] == "jev-1.13.0"
        }
        self.assertEqual(set(client.FIRST_PARTY_MODEL_IDS), expected)
        self.assertEqual(client.PINNED_MODEL_BY_ROUTE["typesafe"], "jev-1.13.0")

    def test_the_price_mirror_matches_the_contract_table(self) -> None:
        self.assertEqual(
            client.INPUT_PRICE_USD_PER_MTOK, MODEL_CONTRACTS["jev-1.13.0"]["pricing_usd_per_mtok"]["input"]
        )

    def test_the_tool_name_is_not_a_third_party_jev_tool(self) -> None:
        self.assertFalse(is_jev_tool_name("omh_jev_ask"))
        self.assertEqual(OMH_JEV_ASK_SCHEMA["name"], "omh_jev_ask")

    def test_the_route_question_projection_renames_options_only(self) -> None:
        route = route_chat_message("почему сборка падает на main", source="discord", limit=3)
        block = route["route_question"]
        from omh.plugin_bundle.omh.tools.jev_ask_tool import _project_route_question

        projected, digest = _project_route_question(block)
        self.assertEqual(digest, block["question_digest"])
        self.assertEqual(set(projected), set(block["questions"]))
        for question_id, question in block["questions"].items():
            self.assertEqual(projected[question_id]["instructions"], question["instructions"])
            if question["type"] == "choice":
                self.assertEqual(projected[question_id]["criteria"], question["options"])
            else:
                self.assertNotIn("criteria", projected[question_id])
        client.validate_questions(projected)


class RouteAnswerProvenanceTests(_ConsentedCase):
    def test_omh_jev_ask_provenance_needs_an_answered_ledger_row_for_the_same_digest(self) -> None:
        route = route_chat_message("почему сборка падает на main", source="discord", limit=3)
        block = route["route_question"]
        options = list(block["questions"]["route_choice"]["options"])
        answers = {"route_choice": {"type": "choice", "choice": options[0],
                                    "probabilities": {option: 1.0 / len(options) for option in options},
                                    "confidence": 0.4}}
        for question_id, question in block["questions"].items():
            if question["type"] == "noul":
                answers[question_id] = {"type": "noul", "noul": 0.3}
        with TemporaryDirectory() as tmp:
            home = _Home(tmp, {"TYPESAFE_API_KEY": SENTINEL})
            record_args = {"question_digest": block["question_digest"], "answered_by": "omh_jev_ask",
                           "route_choice": options[0]}
            with patch.dict(os.environ, home.env):
                refused = json.loads(omh_route_answer_handler({**record_args, "ask_id": "0" * 16}, session_id=SESSION))
            self.assertEqual(refused["status"], "invalid_request")
            asked = home.call({"state": "почему сборка падает на main", "route_question": block},
                              _Recorder(TransportReply(200, {}, _answered_body(answers=answers))))
            self.assertEqual(asked["status"], "answered")
            self.assertEqual(asked["question_digest"], block["question_digest"])
            with patch.dict(os.environ, home.env):
                recorded = json.loads(omh_route_answer_handler({**record_args, "ask_id": asked["ask_id"]},
                                                               session_id=SESSION))
            self.assertEqual(recorded["status"], "recorded")
            self.assertEqual(recorded["record"]["confidence_source"], "observed_from_response")
            with patch.dict(os.environ, home.env):
                wrong = json.loads(omh_route_answer_handler(
                    {**record_args, "question_digest": "ab" * 8, "ask_id": asked["ask_id"]}, session_id=SESSION))
            self.assertEqual(wrong["status"], "invalid_request")
            # The model cannot file its own numbers under Jev's provenance:
            # another option, other probabilities, other fits, or another
            # session are all refused.
            fit_ids = [question_id for question_id in answers if question_id.startswith("fits::")]
            for changed, session in (
                ({"route_choice": options[-1]}, SESSION),
                ({"choice_probabilities": {options[0]: 0.99}}, SESSION),
                ({"fits": {fit_ids[0][len("fits::"):]: 0.99}} if fit_ids else {"route_choice": "none"}, SESSION),
                ({}, "some-other-session"),
            ):
                with self.subTest(changed=changed, session=session), patch.dict(os.environ, home.env):
                    refused = json.loads(omh_route_answer_handler(
                        {**record_args, **changed, "ask_id": asked["ask_id"]}, session_id=session))
                    self.assertEqual(refused["status"], "invalid_request")
            with patch.dict(os.environ, home.env):
                matching = json.loads(omh_route_answer_handler(
                    {**record_args, "ask_id": asked["ask_id"],
                     "choice_probabilities": {options[0]: 1.0 / len(options)}}, session_id=SESSION))
            self.assertEqual(matching["status"], "recorded")

    def test_an_ask_that_did_not_answer_backs_no_claim(self) -> None:
        route = route_chat_message("почему сборка падает на main", source="discord", limit=3)
        block = route["route_question"]
        options = list(block["questions"]["route_choice"]["options"])
        with TemporaryDirectory() as tmp:
            home = _Home(tmp, {"TYPESAFE_API_KEY": SENTINEL})
            timed_out = home.call({"state": "почему сборка падает на main", "route_question": block},
                                  _Recorder(TimeoutError("t")))
            self.assertEqual(timed_out["status"], "timeout")
            with patch.dict(os.environ, home.env):
                refused = json.loads(omh_route_answer_handler(
                    {"question_digest": block["question_digest"], "answered_by": "omh_jev_ask",
                     "route_choice": options[0], "ask_id": timed_out["ask_id"]}, session_id=SESSION))
            self.assertEqual(refused["status"], "invalid_request")


if __name__ == "__main__":
    unittest.main()

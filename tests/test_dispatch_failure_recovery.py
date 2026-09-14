from __future__ import annotations

import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()

from _cli_harness import run_cli  # noqa: E402

from omh.coding.dispatch_failure_recovery import (  # noqa: E402
    CHOICE_HERMES,
    CHOICE_REPORT,
    CHOICE_RETARGET,
    CHOICE_WAIT,
    COOLDOWN_STATUS_AUTH,
    COOLDOWN_STATUS_LIMIT,
    EXECUTOR_AUTH_FAILURE_SIGNALS_SCHEMA_VERSION,
    FAILURE_KIND_AUTH_SHAPED,
    FAILURE_KIND_BINARY_MISSING,
    DISPATCHER_ASSIGNED_FAILURE_KINDS,
    FAILURE_KIND_CAPABILITY_GATE,
    FAILURE_KIND_CRASH,
    FAILURE_KIND_LIMIT_SHAPED,
    FAILURE_KIND_TIMEOUT,
    FAILURE_KIND_WORKSPACE_BLOCKED,
    FAILURE_KINDS,
    RECOVERABLE_FAILURE_KINDS,
    OnFailureModeError,
    auth_repair_command,
    auth_shaped_label,
    build_repair_card,
    classify_failure_kind,
    clear_auth_failure_signal,
    last_auth_failure_signal,
    parse_on_failure,
    prompt_recovery_choice,
    record_auth_failure_signal,
    recovery_candidates,
    recovery_options,
    retarget_candidates,
    spawn_cooldown,
)
from omh.coding.fanout import build_fanout_contract  # noqa: E402
from omh.coding.fanout_artifacts import write_fanout_contract  # noqa: E402
from omh.coding.fanout_dispatch import dispatch_fanout  # noqa: E402
from omh.coding.fanout_journal import (  # noqa: E402
    RESUME_RERUN_AWAITING_RETRY,
    build_fanout_run_journal,
    plan_fanout_resume,
)
from omh.system.local_store import atomic_write_json, utc_now  # noqa: E402
from omh.system.paths import OmhPaths  # noqa: E402

_GOAL = "split the sample feature across agents"
_UNITS = [
    {"unit_id": "core", "title": "Core work", "owner": "codex", "file_scope": ["src/core/"]},
]
_CLI_UNITS = _UNITS + [
    {"unit_id": "docs", "title": "Docs work", "owner": "claude-code", "file_scope": ["docs/"]},
]


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=str(repo), check=True, capture_output=True, text=True)


def _make_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo, sha


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ready(paths, profile, **kwargs):
    return {"status": "ready", "profile": profile}


def _runner(outputs: dict[str, _FakeCompleted]):
    """Route git to the real subprocess; answer each agent CLI from `outputs`."""
    spawned: list[list[str]] = []

    def runner(argv, **kwargs):
        if argv[0] == "git":
            return subprocess.run(argv, **kwargs)
        spawned.append(list(argv))
        return outputs.get(argv[0], _FakeCompleted(0, "done"))

    runner.spawned = spawned
    return runner


class FailureKindClassificationTests(unittest.TestCase):
    def test_closed_enum_covers_every_classifier_answer(self) -> None:
        answers = {
            classify_failure_kind(exit_code=127),
            classify_failure_kind(exit_code=124),
            classify_failure_kind(exit_code=1, auth_label="http_401"),
            classify_failure_kind(exit_code=1, limit_label="rate_limit"),
            classify_failure_kind(exit_code=1),
        }
        # `workspace_blocked` and `capability_gate` are the kinds the classifier
        # can never answer: both are assigned before the spawn, when there is
        # no exit code and no output to classify. Every OTHER member of the
        # enum must still come out of the classifier, or the enum has grown a
        # value nothing sets.
        self.assertEqual(
            DISPATCHER_ASSIGNED_FAILURE_KINDS,
            {FAILURE_KIND_WORKSPACE_BLOCKED, FAILURE_KIND_CAPABILITY_GATE},
        )
        self.assertEqual(answers, set(FAILURE_KINDS) - DISPATCHER_ASSIGNED_FAILURE_KINDS)
        self.assertTrue(DISPATCHER_ASSIGNED_FAILURE_KINDS.isdisjoint(answers))
        self.assertTrue(DISPATCHER_ASSIGNED_FAILURE_KINDS.isdisjoint(RECOVERABLE_FAILURE_KINDS))

    def test_workspace_blocked_is_never_offered_a_recovery_retry(self) -> None:
        # A blocked workspace is the one failure another attempt cannot clear:
        # the objects are still missing, the denial is still a denial, and the
        # case collision is still in the tree.
        self.assertIn(FAILURE_KIND_WORKSPACE_BLOCKED, FAILURE_KINDS)
        self.assertNotIn(FAILURE_KIND_WORKSPACE_BLOCKED, RECOVERABLE_FAILURE_KINDS)

    def test_synthetic_exit_codes_map_before_any_text_match(self) -> None:
        # 127 and 124 are the dispatcher's own observations of the process, so
        # they win even when the tail also carries a provider-shaped message.
        self.assertEqual(
            classify_failure_kind(exit_code=127, limit_label="rate_limit", auth_label="http_401"),
            FAILURE_KIND_BINARY_MISSING,
        )
        self.assertEqual(
            classify_failure_kind(exit_code=124, limit_label="rate_limit", auth_label="http_401"),
            FAILURE_KIND_TIMEOUT,
        )

    def test_auth_wins_over_limit_when_both_shapes_match(self) -> None:
        # The documented precedence: a credential rejection filed as a limit
        # would go into the wait-it-out lane, which can never clear it.
        self.assertEqual(
            classify_failure_kind(exit_code=1, limit_label="rate_limit", auth_label="http_401"),
            FAILURE_KIND_AUTH_SHAPED,
        )

    def test_unclassified_nonzero_exit_is_crash(self) -> None:
        self.assertEqual(classify_failure_kind(exit_code=2), FAILURE_KIND_CRASH)
        self.assertEqual(classify_failure_kind(exit_code=0), "")

    def test_auth_shaped_phrases_are_recognized(self) -> None:
        for text, expected in (
            ("Error: Invalid API key provided", "invalid_api_key"),
            ("your token has expired, please log in again", "token_expired"),
            ("You are not logged in. Please run /login.", "not_logged_in"),
            ("request failed with status 401", "http_401"),
            ("401 Unauthorized", "http_401"),
            ("authentication failed for this account", "authentication_failed"),
            ("the oauth token revoked by the workspace admin", "oauth_revoked"),
        ):
            with self.subTest(text=text):
                self.assertEqual(auth_shaped_label(text, ""), expected)

    def test_unrelated_author_and_401_text_is_not_auth_shaped(self) -> None:
        # The negative controls that keep the patterns multi-word: a bare
        # "auth", "author", or "401" appears constantly in ordinary CLI output.
        for text in (
            "git shortlog: author list for the release notes",
            "unauthorized_reviewers.md mentioned in output",
            "processed value 401 items in the batch",
            "AUTHORS file updated",
            "token budget exceeded for this context window",
            "authorization header forwarded to the proxy",
        ):
            with self.subTest(text=text):
                self.assertEqual(auth_shaped_label(text, ""), "")
                self.assertEqual(auth_shaped_label("", text), "")

    def test_auth_shape_is_matched_case_insensitively_across_both_tails(self) -> None:
        self.assertEqual(auth_shaped_label("", "FATAL: INVALID API KEY"), "invalid_api_key")


class AuthFailureSignalTests(unittest.TestCase):
    def _paths(self, tmp: str) -> OmhPaths:
        root = Path(tmp)
        return OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")

    def test_signal_persists_per_owner_and_reads_back_with_age(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            record_auth_failure_signal(
                paths, "codex", run_ref="run-1", unit_id="core", pattern_label="http_401"
            )
            stored = json.loads(paths.executor_auth_failure_signals_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["schema_version"], EXECUTOR_AUTH_FAILURE_SIGNALS_SCHEMA_VERSION)
            self.assertEqual(stored["profiles"]["codex"]["pattern_label"], "http_401")
            signal = last_auth_failure_signal(paths, "codex")
            self.assertFalse(signal["stale"])
            self.assertIsInstance(signal["age_seconds"], int)
            self.assertEqual(last_auth_failure_signal(paths, "claude-code"), {})

    def test_clear_removes_only_the_named_owner(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            record_auth_failure_signal(paths, "codex", run_ref="r", unit_id="u", pattern_label="a")
            record_auth_failure_signal(
                paths, "claude-code", run_ref="r", unit_id="u", pattern_label="b"
            )
            clear_auth_failure_signal(paths, "codex")
            self.assertEqual(last_auth_failure_signal(paths, "codex"), {})
            self.assertEqual(last_auth_failure_signal(paths, "claude-code")["pattern_label"], "b")

    def test_missing_file_reads_as_no_signal_and_clear_is_a_noop(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            self.assertEqual(last_auth_failure_signal(paths, "codex"), {})
            clear_auth_failure_signal(paths, "codex")
            self.assertFalse(paths.executor_auth_failure_signals_path.exists())

    def test_stale_signal_does_not_cool_down_and_fresh_one_does(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            record_auth_failure_signal(
                paths, "codex", run_ref="r", unit_id="u", pattern_label="http_401"
            )
            cooldown = spawn_cooldown(paths, "codex")
            self.assertIsNotNone(cooldown)
            self.assertEqual(cooldown["status"], COOLDOWN_STATUS_AUTH)
            self.assertEqual(cooldown["failure_kind"], FAILURE_KIND_AUTH_SHAPED)
            atomic_write_json(
                paths.executor_auth_failure_signals_path,
                {
                    "schema_version": EXECUTOR_AUTH_FAILURE_SIGNALS_SCHEMA_VERSION,
                    "profiles": {
                        "codex": {"last_auth_shaped_at": "2020-01-01T00:00:00Z", "pattern_label": "x"}
                    },
                },
                private=True,
            )
            self.assertIsNone(spawn_cooldown(paths, "codex"))

    def test_unaged_record_never_vetoes_a_spawn(self) -> None:
        # A record whose observation time cannot be read cannot be shown to be
        # inside the window, so it ranks but never refuses.
        with TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            atomic_write_json(
                paths.executor_limit_signals_path,
                {"schema_version": "executor_limit_signals/v1", "profiles": {"codex": {"pattern_label": "x"}}},
                private=True,
            )
            self.assertIsNone(spawn_cooldown(paths, "codex"))

    def test_limit_cooldown_is_reported_when_no_auth_signal_exists(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            atomic_write_json(
                paths.executor_limit_signals_path,
                {
                    "schema_version": "executor_limit_signals/v1",
                    "profiles": {"codex": {"last_limit_shaped_at": utc_now(), "pattern_label": "rate_limit"}},
                },
                private=True,
            )
            cooldown = spawn_cooldown(paths, "codex")
            self.assertEqual(cooldown["status"], COOLDOWN_STATUS_LIMIT)
            self.assertEqual(cooldown["failure_kind"], FAILURE_KIND_LIMIT_SHAPED)
            self.assertIsInstance(cooldown["cooldown_remaining_seconds"], int)


class ReadinessSurfaceTests(unittest.TestCase):
    def test_readiness_reports_the_auth_failure_signal_without_persisting_it(self) -> None:
        # The operator whose unit came back `executor_auth_invalid` reads the
        # reason here; advisory markers are recomputed per call, never frozen
        # into the observed-once probe cache.
        from omh.coding.executor_readiness import probe_executor_readiness

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            record_auth_failure_signal(
                paths, "codex", run_ref="r", unit_id="core", pattern_label="http_401"
            )
            result = probe_executor_readiness(paths, "codex", dry_run=True)
            self.assertEqual(result["last_auth_failure_signal"]["pattern_label"], "http_401")
            probe_executor_readiness(paths, "codex")
            stored = json.loads(paths.executor_readiness_path.read_text(encoding="utf-8"))
            self.assertNotIn("last_auth_failure_signal", stored["profiles"]["codex"])


class RepairCardTests(unittest.TestCase):
    def test_known_owners_get_their_own_login_command(self) -> None:
        self.assertIn("/login", auth_repair_command("claude-code"))
        self.assertEqual(auth_repair_command("codex"), "codex login")

    def test_unknown_owner_gets_a_neutral_instruction(self) -> None:
        # Executor-neutral: no CLI is the implied default for an owner this
        # repo has not verified a login command for.
        command = auth_repair_command("omo-runtime")
        self.assertIn("omo-runtime", command)
        self.assertNotIn("codex login", command)
        self.assertNotIn("claude", command)

    def test_auth_card_names_reauth_then_redispatch(self) -> None:
        card = build_repair_card(owner="codex", failure_kind=FAILURE_KIND_AUTH_SHAPED, detail="d")
        self.assertEqual(card["reason_code"], FAILURE_KIND_AUTH_SHAPED)
        self.assertEqual(
            [step["id"] for step in card["repair_steps"]],
            ["reauthenticate_owner", "redispatch_after_repair"],
        )

    def test_limit_card_names_the_wait_and_carries_the_remaining_window(self) -> None:
        card = build_repair_card(
            owner="codex", failure_kind=FAILURE_KIND_LIMIT_SHAPED, detail="d", remaining_seconds=90
        )
        self.assertEqual(card["cooldown_remaining_seconds"], 90)
        self.assertIn("90s", card["repair_steps"][0]["action"])


class OnFailureModeTests(unittest.TestCase):
    def test_named_modes_parse_without_a_target(self) -> None:
        self.assertEqual(parse_on_failure(""), ("report", ""))
        self.assertEqual(parse_on_failure("report"), ("report", ""))
        self.assertEqual(parse_on_failure("hermes"), ("hermes", ""))
        self.assertEqual(parse_on_failure("wait"), ("wait", ""))

    def test_retarget_carries_its_owner_and_validates_it(self) -> None:
        self.assertEqual(
            parse_on_failure("retarget:claude-code", known_owners=("codex", "claude-code")),
            (CHOICE_RETARGET, "claude-code"),
        )
        with self.assertRaises(OnFailureModeError):
            parse_on_failure("retarget:", known_owners=("codex",))
        with self.assertRaises(OnFailureModeError):
            parse_on_failure("retarget:nope", known_owners=("codex",))

    def test_unknown_mode_is_refused_by_name(self) -> None:
        with self.assertRaises(OnFailureModeError) as caught:
            parse_on_failure("switch")
        self.assertIn("switch", str(caught.exception))


class RecoveryOptionTests(unittest.TestCase):
    def test_only_auth_and_limit_failures_are_candidates(self) -> None:
        units = [
            {"unit_id": "a", "owner": "codex", "failure_kind": FAILURE_KIND_AUTH_SHAPED},
            {"unit_id": "b", "owner": "codex", "failure_kind": FAILURE_KIND_LIMIT_SHAPED},
            {"unit_id": "c", "owner": "codex", "failure_kind": FAILURE_KIND_CRASH},
            {"unit_id": "d", "owner": "codex", "failure_kind": FAILURE_KIND_TIMEOUT},
            {"unit_id": "e", "owner": "codex", "failure_kind": FAILURE_KIND_BINARY_MISSING},
            {"unit_id": "f", "owner": "codex"},
        ]
        self.assertEqual([row["unit_id"] for row in recovery_candidates(units)], ["a", "b"])

    def test_the_failed_owner_is_never_offered_as_a_retarget(self) -> None:
        context = {
            "candidates": [
                {"profile": "codex", "label": "Codex", "readiness_status": "ready"},
                {"profile": "claude-code", "label": "Claude Code", "readiness_status": "ready"},
            ]
        }
        rows = retarget_candidates(context, exclude_owner="codex")
        self.assertEqual([row["profile"] for row in rows], ["claude-code"])

    def test_unavailable_options_are_listed_with_their_reason(self) -> None:
        options = recovery_options(
            candidate={"unit_id": "a", "owner": "codex", "failure_kind": FAILURE_KIND_AUTH_SHAPED},
            retargets=[],
            hermes_available=False,
        )
        self.assertEqual([option["choice"] for option in options], [CHOICE_RETARGET, CHOICE_HERMES, CHOICE_WAIT])
        self.assertFalse(options[0]["available"])
        self.assertIn("no other locally-installed coding owner", options[0]["unavailable_reason"])
        self.assertFalse(options[1]["available"])
        self.assertIn("--hermes-model", options[1]["unavailable_reason"])
        self.assertTrue(options[2]["available"])


class RecoveryInterviewPromptTests(unittest.TestCase):
    """The prompt seam is injected, so no test ever needs a real terminal."""

    def _ask(self, answers: list[str], *, retargets, hermes_available=True):
        written: list[str] = []
        candidate = {"unit_id": "core", "owner": "codex", "failure_kind": FAILURE_KIND_LIMIT_SHAPED}
        options = recovery_options(
            candidate=candidate, retargets=retargets, hermes_available=hermes_available
        )
        queue = list(answers)

        def read_line(_prompt: str) -> str:
            if not queue:
                raise EOFError
            return queue.pop(0)

        chosen = prompt_recovery_choice(
            candidate=candidate,
            options=options,
            read_line=read_line,
            write_line=written.append,
        )
        return chosen, written

    def test_choice_one_with_a_single_candidate_selects_that_owner(self) -> None:
        chosen, written = self._ask(["1"], retargets=[{"profile": "claude-code"}])
        self.assertEqual(chosen, {"choice": CHOICE_RETARGET, "target_owner": "claude-code"})
        self.assertTrue(any("core failed on codex" in line for line in written))

    def test_choice_one_with_several_candidates_asks_which_owner(self) -> None:
        chosen, _ = self._ask(
            ["1", "omo-runtime"],
            retargets=[{"profile": "claude-code"}, {"profile": "omo-runtime"}],
        )
        self.assertEqual(chosen, {"choice": CHOICE_RETARGET, "target_owner": "omo-runtime"})

    def test_choice_two_selects_the_hermes_lane(self) -> None:
        chosen, _ = self._ask(["2"], retargets=[{"profile": "claude-code"}])
        self.assertEqual(chosen["choice"], CHOICE_HERMES)

    def test_choice_three_selects_wait(self) -> None:
        chosen, _ = self._ask(["3"], retargets=[])
        self.assertEqual(chosen["choice"], CHOICE_WAIT)

    def test_an_unavailable_option_is_refused_and_re_asked(self) -> None:
        chosen, written = self._ask(["2", "3"], retargets=[], hermes_available=False)
        self.assertEqual(chosen["choice"], CHOICE_WAIT)
        self.assertTrue(any("Option 2 is unavailable" in line for line in written))

    def test_a_closed_input_stream_falls_back_to_report(self) -> None:
        chosen, written = self._ask([], retargets=[])
        self.assertEqual(chosen["choice"], CHOICE_REPORT)
        self.assertTrue(any("No recovery action chosen" in line for line in written))

    def test_junk_answers_are_re_asked_then_give_up_as_report(self) -> None:
        chosen, written = self._ask(["x", "9", "  "], retargets=[])
        self.assertEqual(chosen["choice"], CHOICE_REPORT)
        self.assertEqual(sum(1 for line in written if line == "Answer 1, 2, or 3."), 3)


_AUTH_FAILURE_TEXT = "Error: invalid API key. Run `codex login` and try again."
_LIMIT_FAILURE_TEXT = "Error: you have hit your usage limit. Try again later."


class DispatchFailureRecoveryEngineTests(unittest.TestCase):
    def _setup(self, tmp: str, units=None):
        root = Path(tmp)
        paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
        repo, sha = _make_repo(root)
        contract = write_fanout_contract(paths, build_fanout_contract(_GOAL, list(units or _UNITS)))
        return paths, repo, sha, contract

    def _dispatch(self, paths, repo, sha, contract, runner, **kwargs):
        return dispatch_fanout(
            paths,
            contract,
            goal_text=_GOAL,
            repo_root=repo,
            base_sha=sha,
            runner=runner,
            readiness=_ready,
            **kwargs,
        )

    def test_auth_shaped_failure_classifies_persists_and_carries_a_repair_card(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)
            summary = self._dispatch(
                paths, repo, sha, contract, _runner({"codex": _FakeCompleted(1, _AUTH_FAILURE_TEXT)})
            )
            core = summary["units"][0]
            self.assertEqual(core["failure_kind"], FAILURE_KIND_AUTH_SHAPED)
            self.assertTrue(core["auth_shaped"])
            self.assertEqual(core["auth_pattern"], "invalid_api_key")
            self.assertEqual(core["repair_card"]["repair_steps"][0]["command"], "codex login")
            self.assertEqual(last_auth_failure_signal(paths, "codex")["pattern_label"], "invalid_api_key")
            # Auth takes the persisted signal; no limit record is fabricated.
            self.assertFalse(paths.executor_limit_signals_path.exists())

    def test_limit_shaped_failure_keeps_its_existing_flags_and_gains_a_kind(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)
            summary = self._dispatch(
                paths, repo, sha, contract, _runner({"codex": _FakeCompleted(1, _LIMIT_FAILURE_TEXT)})
            )
            core = summary["units"][0]
            self.assertEqual(core["failure_kind"], FAILURE_KIND_LIMIT_SHAPED)
            self.assertTrue(core["limit_shaped"])
            self.assertNotIn("auth_shaped", core)
            self.assertEqual(last_auth_failure_signal(paths, "codex"), {})

    def test_a_missing_binary_and_a_timeout_map_to_their_synthetic_kinds(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)

            def missing(argv, **kwargs):
                if argv[0] == "git":
                    return subprocess.run(argv, **kwargs)
                raise FileNotFoundError(argv[0])

            summary = self._dispatch(paths, repo, sha, contract, missing)
            self.assertEqual(summary["units"][0]["failure_kind"], FAILURE_KIND_BINARY_MISSING)

        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)

            def slow(argv, **kwargs):
                if argv[0] == "git":
                    return subprocess.run(argv, **kwargs)
                raise subprocess.TimeoutExpired(argv, 1)

            summary = self._dispatch(paths, repo, sha, contract, slow, sleep=lambda _s: None)
            self.assertEqual(summary["units"][0]["failure_kind"], FAILURE_KIND_TIMEOUT)

    def test_a_later_success_clears_the_auth_signal(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)
            record_auth_failure_signal(
                paths, "codex", run_ref="r", unit_id="core", pattern_label="http_401"
            )
            self._dispatch(
                paths,
                repo,
                sha,
                contract,
                _runner({}),
                ignore_limit_signal=True,
            )
            self.assertEqual(last_auth_failure_signal(paths, "codex"), {})

    def test_a_fresh_auth_signal_vetoes_the_spawn_with_a_repair_card(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)
            record_auth_failure_signal(
                paths, "codex", run_ref="r", unit_id="core", pattern_label="http_401"
            )
            runner = _runner({})
            summary = self._dispatch(paths, repo, sha, contract, runner)
            core = summary["units"][0]
            self.assertEqual(core["status"], COOLDOWN_STATUS_AUTH)
            self.assertEqual(core["failure_kind"], FAILURE_KIND_AUTH_SHAPED)
            self.assertIn("--ignore-limit-signal", core["reason"])
            self.assertEqual(core["repair_card"]["reason_code"], FAILURE_KIND_AUTH_SHAPED)
            self.assertEqual(runner.spawned, [])
            # Nothing was created for a unit that never started.
            self.assertFalse((repo.parent / f"{repo.name}-fanout-core").exists())

    def test_a_fresh_limit_signal_vetoes_the_spawn(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)
            atomic_write_json(
                paths.executor_limit_signals_path,
                {
                    "schema_version": "executor_limit_signals/v1",
                    "profiles": {"codex": {"last_limit_shaped_at": utc_now(), "pattern_label": "rate_limit"}},
                },
                private=True,
            )
            runner = _runner({})
            summary = self._dispatch(paths, repo, sha, contract, runner)
            self.assertEqual(summary["units"][0]["status"], COOLDOWN_STATUS_LIMIT)
            self.assertEqual(runner.spawned, [])

    def test_ignore_limit_signal_overrides_both_cooldowns(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)
            record_auth_failure_signal(
                paths, "codex", run_ref="r", unit_id="core", pattern_label="http_401"
            )
            runner = _runner({})
            summary = self._dispatch(paths, repo, sha, contract, runner, ignore_limit_signal=True)
            self.assertEqual(summary["units"][0]["status"], "completed")
            self.assertEqual(len(runner.spawned), 1)

    def test_a_cooldown_veto_blocks_dependents_and_resumes_as_never_attempted(self) -> None:
        units = [
            {"unit_id": "core", "title": "Core", "owner": "codex", "file_scope": ["src/core/"]},
            {
                "unit_id": "docs",
                "title": "Docs",
                "owner": "codex",
                "file_scope": ["docs/"],
                "depends_on": ["core"],
            },
        ]
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp, units=units)
            record_auth_failure_signal(
                paths, "codex", run_ref="r", unit_id="core", pattern_label="http_401"
            )
            summary = self._dispatch(paths, repo, sha, contract, _runner({}))
            by_unit = {entry["unit_id"]: entry for entry in summary["units"]}
            self.assertEqual(by_unit["core"]["status"], COOLDOWN_STATUS_AUTH)
            self.assertEqual(by_unit["docs"]["status"], "blocked_by_dependency")
            journal = build_fanout_run_journal(summary)
            rows = {row["unit_id"]: row for row in journal["units"]}
            self.assertEqual(rows["core"]["terminal_state"], "not_attempted")
            plan = plan_fanout_resume(
                journal, order=["core", "docs"], depends_on={"core": [], "docs": ["core"]}
            )
            self.assertEqual(plan["selected_units"], ["core", "docs"])


class DispatchRecoveryChoiceTests(unittest.TestCase):
    def _setup(self, tmp: str):
        root = Path(tmp)
        paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
        repo, sha = _make_repo(root)
        contract = write_fanout_contract(paths, build_fanout_contract(_GOAL, list(_UNITS)))
        return paths, repo, sha, contract

    def _dispatch(self, paths, repo, sha, contract, **kwargs):
        return dispatch_fanout(
            paths,
            contract,
            goal_text=_GOAL,
            repo_root=repo,
            base_sha=sha,
            runner=_runner({"codex": _FakeCompleted(1, _LIMIT_FAILURE_TEXT)}),
            readiness=_ready,
            **kwargs,
        )

    def test_report_mode_records_the_options_and_changes_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)
            lines: list[str] = []
            summary = self._dispatch(paths, repo, sha, contract, write_line=lines.append)
            recovery = summary["failure_recovery"]
            self.assertEqual(recovery["mode"], "report")
            self.assertFalse(recovery["interactive"])
            decision = recovery["decisions"][0]
            self.assertEqual(decision["choice"], CHOICE_REPORT)
            self.assertEqual(decision["failure_kind"], FAILURE_KIND_LIMIT_SHAPED)
            self.assertEqual(len(decision["options"]), 3)
            self.assertIn("repair_card", decision)
            self.assertTrue(any("Recovery options (none taken" in line for line in lines))
            self.assertNotIn("awaiting_retry", summary["units"][0])

    def test_a_clean_run_carries_no_failure_recovery_block(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)
            summary = dispatch_fanout(
                paths,
                contract,
                goal_text=_GOAL,
                repo_root=repo,
                base_sha=sha,
                runner=_runner({}),
                readiness=_ready,
            )
            self.assertNotIn("failure_recovery", summary)

    def test_wait_marks_the_unit_and_the_next_resume_reruns_exactly_it(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)
            summary = self._dispatch(paths, repo, sha, contract, on_failure="wait")
            self.assertEqual(summary["failure_recovery"]["awaiting_retry_units"], ["core"])
            core = summary["units"][0]
            self.assertTrue(core["awaiting_retry"])
            self.assertEqual(core["recovery_choice"]["choice"], CHOICE_WAIT)
            journal = build_fanout_run_journal(summary)
            row = journal["units"][0]
            self.assertTrue(row["awaiting_retry"])
            self.assertEqual(row["awaiting_retry_kind"], FAILURE_KIND_LIMIT_SHAPED)
            self.assertEqual(row["failure_class"], "transient_provider_limit")
            plan = plan_fanout_resume(journal, order=["core"], depends_on={"core": []})
            self.assertEqual(plan["selected_units"], ["core"])
            self.assertEqual(plan["decisions"][0]["action"], RESUME_RERUN_AWAITING_RETRY)

    def test_an_observed_success_is_still_skipped_by_the_resume(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)
            summary = dispatch_fanout(
                paths,
                contract,
                goal_text=_GOAL,
                repo_root=repo,
                base_sha=sha,
                runner=_runner({}),
                readiness=_ready,
            )
            journal = build_fanout_run_journal(summary)
            plan = plan_fanout_resume(journal, order=["core"], depends_on={"core": []})
            self.assertEqual(plan["selected_units"], [])
            self.assertEqual(plan["held_units"], ["core"])

    def test_retarget_redispatches_under_the_chosen_owner_with_provenance(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)
            summary = self._dispatch(
                paths, repo, sha, contract, on_failure=CHOICE_RETARGET, retarget_owner="claude-code"
            )
            decision = summary["failure_recovery"]["decisions"][0]
            self.assertEqual(decision["choice"], CHOICE_RETARGET)
            self.assertEqual(decision["target_owner"], "claude-code")
            attempt = decision["attempt"]
            self.assertEqual(attempt["owner"], "claude-code")
            self.assertEqual(attempt["unit_id"], "core-retarget-claude-code")
            self.assertEqual(attempt["status"], "completed")
            self.assertEqual(attempt["retargeted_from"], {"unit_id": "core", "owner": "codex"})
            # Its own worktree, so the failed unit's directory is never reused
            # and never deleted.
            self.assertTrue((repo.parent / f"{repo.name}-fanout-core-retarget-claude-code").is_dir())
            self.assertTrue((repo.parent / f"{repo.name}-fanout-core").is_dir())

    def test_retargeting_to_the_owner_that_just_failed_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)
            summary = self._dispatch(
                paths, repo, sha, contract, on_failure=CHOICE_RETARGET, retarget_owner="codex"
            )
            decision = summary["failure_recovery"]["decisions"][0]
            self.assertEqual(decision["choice"], CHOICE_REPORT)
            self.assertIn("the owner that just failed", decision["reason"])
            self.assertNotIn("attempt", decision)

    def test_hermes_choice_dispatches_through_the_injected_lane_with_consent(self) -> None:
        calls: list[dict] = []

        def stub_hermes(**kwargs):
            calls.append(kwargs)
            return {"status": "completed", "exit_code": 0, "run_id": kwargs["run_id"]}

        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)
            summary = self._dispatch(
                paths,
                repo,
                sha,
                contract,
                on_failure="hermes",
                hermes_routing={"model": "m", "provider": "p", "reasoning": "high"},
                hermes_child=stub_hermes,
            )
            decision = summary["failure_recovery"]["decisions"][0]
            self.assertEqual(decision["choice"], CHOICE_HERMES)
            self.assertIn("--confirm-dispatch", decision["consent"])
            self.assertEqual(decision["attempt"]["status"], "completed")
            self.assertEqual(decision["attempt"]["owner"], "hermes")
            self.assertEqual(len(calls), 1)
            # The child runs in the unit's own worktree, never the repo root.
            self.assertEqual(Path(calls[0]["cwd"]).name, f"{repo.name}-fanout-core")
            self.assertEqual(calls[0]["routing"]["model"], "m")
            self.assertIn("Work unit:", calls[0]["prompt"])

    def test_hermes_choice_without_routing_degrades_to_report(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)
            summary = self._dispatch(paths, repo, sha, contract, on_failure="hermes")
            decision = summary["failure_recovery"]["decisions"][0]
            self.assertEqual(decision["choice"], CHOICE_REPORT)
            self.assertIn("--hermes-model", decision["reason"])

    def test_the_interview_answer_drives_the_action(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)
            answers = iter(["3"])
            summary = self._dispatch(
                paths,
                repo,
                sha,
                contract,
                interactive=True,
                read_line=lambda _prompt: next(answers),
                write_line=lambda _line: None,
            )
            recovery = summary["failure_recovery"]
            self.assertTrue(recovery["interactive"])
            self.assertEqual(recovery["decisions"][0]["choice"], CHOICE_WAIT)
            self.assertTrue(summary["units"][0]["awaiting_retry"])

    def test_a_dry_run_neither_cools_down_nor_interviews(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract = self._setup(tmp)
            record_auth_failure_signal(
                paths, "codex", run_ref="r", unit_id="core", pattern_label="http_401"
            )
            summary = self._dispatch(paths, repo, sha, contract, dry_run=True)
            self.assertEqual(summary["units"][0]["status"], "dry_run_planned")
            self.assertNotIn("failure_recovery", summary)


class FailureRecoveryCliTests(unittest.TestCase):
    def _prepared(self, tmp: str) -> tuple[list[str], Path, Path, str]:
        root = Path(tmp)
        base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]
        repo, _sha = _make_repo(root)
        goal_file = root / "goal.txt"
        goal_file.write_text(_GOAL, encoding="utf-8")
        units_file = root / "units.json"
        # Two units: a one-unit split is redirected to `omh coding run`, which
        # is a different (single-run) entry into the same engine.
        units_file.write_text(json.dumps(_CLI_UNITS), encoding="utf-8")
        status, stdout, stderr = run_cli(
            base
            + [
                "coding",
                "fanout",
                "prepare",
                "--goal",
                *_GOAL.split(),
                "--units",
                str(units_file),
                "--record",
            ]
        )
        self.assertEqual(status, 0, stderr)
        return base, repo, goal_file, json.loads(stdout)["fanout_id"]

    def test_an_unknown_on_failure_value_is_refused_before_anything_runs(self) -> None:
        with TemporaryDirectory() as tmp:
            base, repo, goal_file, fanout_id = self._prepared(tmp)
            status, _stdout, stderr = run_cli(
                base
                + [
                    "coding",
                    "fanout",
                    "dispatch",
                    fanout_id,
                    "--goal-file",
                    str(goal_file),
                    "--repo-root",
                    str(repo),
                    "--on-failure",
                    "switch-owner",
                ]
            )
            self.assertEqual(status, 2)
            self.assertIn("switch-owner", stderr)

    def test_an_unknown_retarget_owner_is_refused_by_name(self) -> None:
        with TemporaryDirectory() as tmp:
            base, repo, goal_file, fanout_id = self._prepared(tmp)
            status, _stdout, stderr = run_cli(
                base
                + [
                    "coding",
                    "fanout",
                    "dispatch",
                    fanout_id,
                    "--goal-file",
                    str(goal_file),
                    "--repo-root",
                    str(repo),
                    "--on-failure",
                    "retarget:not-an-owner",
                ]
            )
            self.assertEqual(status, 2)
            self.assertIn("not-an-owner", stderr)

    def test_a_non_tty_dispatch_never_prompts_and_reports_the_options(self) -> None:
        with TemporaryDirectory() as tmp:
            base, repo, goal_file, fanout_id = self._prepared(tmp)
            status, stdout, _stderr = run_cli(
                base
                + [
                    "coding",
                    "fanout",
                    "dispatch",
                    fanout_id,
                    "--goal-file",
                    str(goal_file),
                    "--repo-root",
                    str(repo),
                    "--dry-run",
                ]
            )
            self.assertEqual(status, 0, stdout)
            summary = json.loads(stdout)
            self.assertNotIn("failure_recovery", summary)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

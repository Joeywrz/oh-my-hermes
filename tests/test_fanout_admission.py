from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from omh.coding.fanout import build_fanout_contract
from omh.coding.fanout_artifacts import write_fanout_contract
from omh.coding.fanout_dispatch import dispatch_fanout
from omh.system.paths import OmhPaths

_GOAL = "admit a ready frontier through an adaptive window"
# Upper bound on every wait that expects dispatch to make progress. Each
# unit's worktree is a real `git worktree add`, and the Windows CI runner has
# been observed taking well over the previous 5s bound for a handful of them
# (main run 33850199554: two different waits in this file timed out on two
# attempts of the same commit, then passed on the third). A generous bound
# costs nothing on a passing run -- every wait returns as soon as its
# condition holds -- while a tight one turns runner speed into a failure.
# Waits that assert NOTHING more starts keep their short, deliberate bound.
_PROGRESS_WAIT_SECONDS = 60.0


class _FakeCompleted:
    returncode = 0
    stdout = "done"
    stderr = ""


class _ControlledRunner:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.active = 0
        self.max_active = 0
        self._released: set[str] = set()
        self._release_future = False
        self._condition = threading.Condition()

    def __call__(self, argv, **kwargs):
        if argv[0] == "git":
            return subprocess.run(argv, **kwargs)
        unit_id = Path(str(kwargs["cwd"])).name.rsplit("-fanout-", 1)[-1]
        with self._condition:
            self.started.append(unit_id)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self._condition.notify_all()
            released = self._condition.wait_for(
                lambda: self._release_future or unit_id in self._released,
                timeout=_PROGRESS_WAIT_SECONDS,
            )
            self.active -= 1
            self._condition.notify_all()
        if not released:
            raise AssertionError(f"timed out waiting to release {unit_id}")
        return _FakeCompleted()

    def wait_for_started(self, count: int, timeout: float = _PROGRESS_WAIT_SECONDS) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: len(set(self.started)) >= count,
                timeout=timeout,
            )

    def release(self, unit_id: str) -> None:
        with self._condition:
            self._released.add(unit_id)
            self._condition.notify_all()

    def release_all(self) -> None:
        with self._condition:
            self._release_future = True
            self._condition.notify_all()


def _ready(paths, profile, **kwargs):
    return {"status": "ready", "profile": profile}


class FanoutAdaptiveSchedulerTests(unittest.TestCase):
    def test_adaptive_scheduler_starts_at_two_then_grows_after_a_clean_completion(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
                cwd=repo,
                check=True,
            )
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            units = [
                {
                    "unit_id": f"unit-{index}",
                    "title": f"Unit {index}",
                    "owner": "codex",
                    "file_scope": [f"src/unit-{index}/"],
                }
                for index in range(4)
            ]
            contract = write_fanout_contract(
                paths,
                build_fanout_contract(_GOAL, units),
            )
            runner = _ControlledRunner()

            with ThreadPoolExecutor(max_workers=1) as caller:
                future = caller.submit(
                    dispatch_fanout,
                    paths,
                    contract,
                    goal_text=_GOAL,
                    repo_root=repo,
                    base_sha=base_sha,
                    concurrency=4,
                    adaptive_concurrency=True,
                    runner=runner,
                    readiness=_ready,
                )
                if not runner.wait_for_started(2):
                    future.result(timeout=0)
                    self.fail("adaptive dispatch did not admit its initial window")
                self.assertFalse(runner.wait_for_started(3, timeout=0.2))
                runner.release(runner.started[0])
                self.assertTrue(runner.wait_for_started(4))
                runner.release_all()
                summary = future.result(timeout=_PROGRESS_WAIT_SECONDS)

        self.assertEqual(runner.max_active, 3)
        self.assertTrue(all(unit["status"] == "completed" for unit in summary["units"]))


class AdaptiveAdmissionReceiptTests(unittest.TestCase):
    def test_receipt_redacts_a_secret_shaped_unit_identifier(self) -> None:
        from omh.coding.fanout_admission import AdaptiveFanoutAdmission

        sensitive_unit_id = "sk-reviewersentinel123456"
        admission = AdaptiveFanoutAdmission(ceiling=2)
        admission.observe(
            sensitive_unit_id,
            {"status": "completed", "exit_code": 0, "process_succeeded": True},
        )

        receipt = admission.receipt()

        self.assertEqual(receipt["adjustments"][0]["unit_id"], "[redacted]")
        self.assertNotIn(sensitive_unit_id, str(receipt))

    def test_a_result_that_is_neither_clean_nor_pressure_leaves_the_window_alone(self) -> None:
        """The `else: return` branch of the rule, pinned as a contract.

        `FanoutRecoveredPressureIntegrationTests` builds its fixture on this:
        a unit that exists only to occupy a slot returns an inert result so
        the order it reaches the collector in cannot change another unit's
        adjustment row. If the rule ever starts acting on these, that fixture
        silently goes back to depending on completion order -- which is what
        issue #1639 was.
        """
        from omh.coding.fanout_admission import AdaptiveFanoutAdmission

        admission = AdaptiveFanoutAdmission(ceiling=4)
        admission.observe(
            "clean",
            {"status": "completed", "exit_code": 0, "process_succeeded": True},
        )
        self.assertEqual(admission.window, 3)
        for failure_kind in ("auth_shaped", "timeout", "binary_missing", "crash"):
            admission.observe(
                f"other-{failure_kind}",
                {"status": "failed", "exit_code": 1, "failure_kind": failure_kind},
            )
            self.assertEqual(admission.window, 3, failure_kind)
        receipt = admission.receipt()
        # Counted as observed completions, but never as adjustments: the
        # dispatch saw them, and the window did not move.
        self.assertEqual(receipt["observed_completion_count"], 5)
        self.assertEqual(receipt["adjustment_count"], 1)
        self.assertEqual([row["unit_id"] for row in receipt["adjustments"]], ["clean"])

    def test_receipt_bounds_provider_pressure_and_recovered_retry_adjustments(self) -> None:
        from omh.coding.fanout_admission import AdaptiveFanoutAdmission

        admission = AdaptiveFanoutAdmission(ceiling=4)
        admission.observe(
            "clean",
            {"status": "completed", "exit_code": 0, "process_succeeded": True},
        )
        admission.observe(
            "recovered-limit",
            {
                "status": "completed",
                "exit_code": 0,
                "process_succeeded": True,
                "retry": {
                    "decisions": [
                        {"failure_class": "transient_provider_limit", "decision": "retrying"}
                    ]
                },
            },
        )
        for index, failure_kind in enumerate(
            ("auth_shaped", "timeout", "binary_missing", "crash")
        ):
            admission.observe(
                f"other-{index}",
                {"status": "failed", "exit_code": 1, "failure_kind": failure_kind},
            )
        admission.observe(
            "transport",
            {
                "status": "failed",
                "exit_code": 1,
                "failure_kind": "crash",
                "retry": {"decisions": [{"failure_class": "transient_transport"}]},
            },
        )
        for index in range(40):
            admission.observe(
                f"clean-{index}",
                {"status": "completed", "exit_code": 0, "process_succeeded": True},
            )
            admission.observe(
                f"limit-{index}",
                {"status": "failed", "exit_code": 1, "failure_kind": "limit_shaped"},
            )

        receipt = admission.receipt()

        self.assertEqual(receipt["schema_version"], "fanout_admission/v1")
        self.assertEqual(receipt["mode"], "adaptive")
        self.assertTrue(receipt["requested"])
        self.assertEqual(receipt["initial_window"], 2)
        self.assertEqual(receipt["ceiling"], 4)
        self.assertEqual(receipt["minimum_window"], 1)
        self.assertEqual(receipt["observed_provider_pressure_count"], 41)
        recovered = next(
            row for row in receipt["adjustments"] if row["unit_id"] == "recovered-limit"
        )
        self.assertEqual(recovered["status_class"], "provider_limit_pressure")
        self.assertEqual((recovered["window_before"], recovered["window_after"]), (3, 1))
        self.assertLessEqual(len(receipt["adjustments"]), 32)
        self.assertGreater(receipt["adjustments_omitted"], 0)
        self.assertNotIn("raw_output", str(receipt))
        self.assertIn("not provider quota truth", receipt["claim_boundary"])


class _RetryControlledRunner:
    def __init__(self, pressure_unit: str, inert_units: Iterable[str] = ()) -> None:
        self.pressure_unit = pressure_unit
        # Units whose result must not move the admission window, whatever
        # order they reach the collector in. See `_FakeInertCompleted`.
        self.inert_units = frozenset(inert_units)
        self.attempts: dict[str, int] = {}
        self.started: list[tuple[str, int]] = []
        self._released: set[tuple[str, int]] = set()
        self._release_future = False
        self._condition = threading.Condition()

    def __call__(self, argv, **kwargs):
        if argv[0] == "git":
            return subprocess.run(argv, **kwargs)
        unit_id = Path(str(kwargs["cwd"])).name.rsplit("-fanout-", 1)[-1]
        with self._condition:
            attempt = self.attempts.get(unit_id, 0) + 1
            self.attempts[unit_id] = attempt
            key = (unit_id, attempt)
            self.started.append(key)
            self._condition.notify_all()
            released = self._condition.wait_for(
                lambda: self._release_future or key in self._released,
                timeout=_PROGRESS_WAIT_SECONDS,
            )
        if not released:
            raise AssertionError(f"timed out waiting to release {key}")
        if key == (self.pressure_unit, 1):
            return _FakeLimitCompleted()
        if unit_id in self.inert_units:
            return _FakeInertCompleted()
        return _FakeCompleted()

    def wait_for_distinct_units(self, count: int, timeout: float = _PROGRESS_WAIT_SECONDS) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: len({unit_id for unit_id, _attempt in self.started}) >= count,
                timeout=timeout,
            )

    def wait_for_attempt(self, unit_id: str, attempt: int, timeout: float = _PROGRESS_WAIT_SECONDS) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: (unit_id, attempt) in self.started,
                timeout=timeout,
            )

    def release(self, unit_id: str, attempt: int = 1) -> None:
        with self._condition:
            self._released.add((unit_id, attempt))
            self._condition.notify_all()

    def release_all(self) -> None:
        with self._condition:
            self._release_future = True
            self._condition.notify_all()


class _FakeLimitCompleted:
    returncode = 1
    stdout = "Error: You have hit your usage limit. Try again later."
    stderr = ""


class _FakeInertCompleted:
    """A process result `AdaptiveFanoutAdmission.observe` deliberately ignores.

    The rule moves the window for exactly two classes -- a clean completion
    and provider-limit pressure -- and returns without touching it for
    anything else. A fixture unit whose only job is to occupy a slot returns
    this, so WHEN it reaches the collector cannot change what another unit's
    adjustment row records.
    """

    returncode = 1
    stdout = "unit failed for a reason unrelated to provider capacity"
    stderr = ""


class FanoutRecoveredPressureIntegrationTests(unittest.TestCase):
    """Pressure shrinks the window before the next queued unit is admitted.

    Every unit but `a-clean` and `b-pressure` exists only to occupy or queue
    behind the grown window, and each returns an inert result: the window
    this test asserts on must be decided by those two alone.

    That is not tidiness. The collector accounts results in completion
    order, and `release_all()` below unblocks the held units on a 0.2s
    NEGATIVE wait -- a bound that exists to prove no fifth unit starts, not
    to prove `b-pressure` was accounted first. If `b-pressure`'s result is
    still in flight when that bound elapses, a held unit can reach the
    collector first; a clean one would grow the window 3 -> 4 and the
    pressure row would read `(4, 2)`, which is what CI observed once
    (issue #1639). An inert result cannot, so the ordering stops mattering.

    Holding back only `c-hold` and `d-hold` is not enough, and the measured
    reason is worth keeping: once those two complete, the frontier reopens
    and `e-later`/`f-later` are admitted and return at once, so a clean
    result from either reaches the collector by the same route. Measured at
    a 0.5s lag on `b-pressure`, that alone reproduced `(4, 2)`.

    The rule that a clean completion grows the window by one is still
    exercised here by `a-clean`'s own row, and pinned directly and without
    threads by `test_fanout_capacity.CapacityCompatibilityTests`.
    """

    def test_recovered_limit_pressure_reduces_admission_before_the_next_queued_unit(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
                cwd=repo,
                check=True,
            )
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            unit_ids = ["a-clean", "b-pressure", "c-hold", "d-hold", "e-later", "f-later"]
            units = [
                {
                    "unit_id": unit_id,
                    "title": unit_id,
                    "owner": "codex",
                    "file_scope": [f"src/{unit_id}/"],
                }
                for unit_id in unit_ids
            ]
            contract = write_fanout_contract(
                paths,
                build_fanout_contract(
                    _GOAL,
                    units,
                    spawn_plan={
                        "why_parallel": "The six file scopes are independent.",
                        "why_not_single_unit": "The admission boundary requires a queued frontier.",
                        "independence": "No fixture unit depends on another.",
                        "expected_evidence_shape": "One process result per unit plus one retry.",
                    },
                ),
            )
            runner = _RetryControlledRunner(
                "b-pressure", inert_units=("c-hold", "d-hold", "e-later", "f-later")
            )

            with ThreadPoolExecutor(max_workers=1) as caller:
                future = caller.submit(
                    dispatch_fanout,
                    paths,
                    contract,
                    goal_text=_GOAL,
                    repo_root=repo,
                    base_sha=base_sha,
                    concurrency=4,
                    adaptive_concurrency=True,
                    max_retries=1,
                    rng=lambda: 0.0,
                    sleep=lambda _seconds: None,
                    runner=runner,
                    readiness=_ready,
                )
                self.assertTrue(runner.wait_for_distinct_units(2))
                runner.release("a-clean")
                self.assertTrue(runner.wait_for_distinct_units(4))
                runner.release("b-pressure")
                self.assertTrue(runner.wait_for_attempt("b-pressure", 2))
                runner.release("b-pressure", 2)
                self.assertFalse(runner.wait_for_distinct_units(5, timeout=0.2))
                runner.release_all()
                summary = future.result(timeout=_PROGRESS_WAIT_SECONDS)

        pressure = next(
            row
            for row in summary["adaptive_admission"]["adjustments"]
            if row["unit_id"] == "b-pressure"
        )
        self.assertEqual(pressure["status_class"], "provider_limit_pressure")
        self.assertEqual((pressure["window_before"], pressure["window_after"]), (3, 1))
        self.assertEqual(runner.attempts["b-pressure"], 2)


class FanoutInterruptedAdmissionTests(unittest.TestCase):
    def test_interrupt_cleanup_observes_a_collected_clean_completion_once(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
                cwd=repo,
                check=True,
            )
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            contract = write_fanout_contract(
                paths,
                build_fanout_contract(
                    _GOAL,
                    [
                        {
                            "unit_id": "completed-during-interrupt",
                            "title": "Completed during interrupt",
                            "owner": "codex",
                            "file_scope": ["src/completed-during-interrupt/"],
                        }
                    ],
                ),
            )

            def interrupt_after_completion(futures, **_kwargs):
                for future in futures:
                    future.result(timeout=_PROGRESS_WAIT_SECONDS)
                raise KeyboardInterrupt

            with patch(
                "omh.coding.fanout_dispatch.futures_wait",
                side_effect=interrupt_after_completion,
            ):
                summary = dispatch_fanout(
                    paths,
                    contract,
                    goal_text=_GOAL,
                    repo_root=repo,
                    base_sha=base_sha,
                    concurrency=1,
                    adaptive_concurrency=True,
                    runner=lambda argv, **kwargs: (
                        subprocess.run(argv, **kwargs) if argv[0] == "git" else _FakeCompleted()
                    ),
                    readiness=_ready,
                )

        receipt = summary["adaptive_admission"]
        self.assertTrue(summary["interrupted"])
        self.assertEqual(summary["units"][0]["status"], "completed")
        self.assertEqual(receipt["observation_status"], "observed_local_process_results")
        self.assertEqual(receipt["observed_completion_count"], 1)
        self.assertEqual(receipt["observed_clean_completion_count"], 1)
        self.assertEqual(receipt["adjustment_count"], 1)


class AdaptiveAdmissionRecursionRefusalTests(unittest.TestCase):
    def test_adaptive_recursion_refusal_carries_an_unobserved_receipt(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            receipts = []

            with patch(
                "omh.coding.fanout_dispatch._owner_skill_discoveries",
                side_effect=AssertionError("depth refusal must not run discovery"),
            ):
                for dry_run in (False, True):
                    summary = dispatch_fanout(
                        paths,
                        {"fanout_id": "fanout-depth-refusal"},
                        goal_text="must not be validated",
                        repo_root=root / "missing-repo",
                        base_sha="unobserved",
                        concurrency=4,
                        adaptive_concurrency=True,
                        dry_run=dry_run,
                        max_depth=1,
                        env={"OMH_FANOUT_DEPTH": "1"},
                        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            AssertionError("depth refusal must not spawn")
                        ),
                        readiness=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            AssertionError("depth refusal must not probe readiness")
                        ),
                    )
                    self.assertTrue(summary["refused"])
                    self.assertEqual(summary["refusal_reason"], "fanout_depth_exceeded")
                    receipts.append(summary.get("adaptive_admission"))

        for receipt in receipts:
            self.assertIsInstance(receipt, dict)
            self.assertEqual(receipt["schema_version"], "fanout_admission/v1")
            self.assertEqual(receipt["observed_completion_count"], 0)
            self.assertEqual(receipt["adjustments"], [])
        self.assertEqual(
            [receipt["observation_status"] for receipt in receipts],
            ["no_observed_unit_results", "not_observed_dry_run"],
        )


class AdaptiveAdmissionDryRunTests(unittest.TestCase):
    def test_dry_run_receipt_makes_no_observed_execution_or_pressure_claim(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
                cwd=repo,
                check=True,
            )
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            contract = write_fanout_contract(
                paths,
                build_fanout_contract(
                    _GOAL,
                    [
                        {
                            "unit_id": "dry-run",
                            "title": "Dry run",
                            "owner": "codex",
                            "file_scope": ["src/dry-run/"],
                        }
                    ],
                ),
            )
            runner = _ControlledRunner()

            summary = dispatch_fanout(
                paths,
                contract,
                goal_text=_GOAL,
                repo_root=repo,
                base_sha=base_sha,
                concurrency=4,
                adaptive_concurrency=True,
                dry_run=True,
                runner=runner,
                readiness=_ready,
            )

        receipt = summary["adaptive_admission"]
        self.assertEqual(receipt["observation_status"], "not_observed_dry_run")
        self.assertEqual(receipt["observed_completion_count"], 0)
        self.assertEqual(receipt["observed_provider_pressure_count"], 0)
        self.assertEqual(receipt["adjustments"], [])
        self.assertEqual(receipt["final_window"], receipt["initial_window"])
        self.assertEqual(runner.started, [])
        self.assertIn("Dry-run plans are not observed execution", receipt["claim_boundary"])


class AdaptiveAdmissionCliTests(unittest.TestCase):
    def test_dispatch_help_and_parser_expose_the_opt_in_ceiling_semantics(self) -> None:
        from omh.commands.main import build_parser

        help_result = subprocess.run(
            [sys.executable, "-m", "omh.cli", "coding", "fanout", "dispatch", "--help"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--adaptive-concurrency", help_result.stdout)
        self.assertIn("--concurrency ceiling", help_result.stdout)
        self.assertIn("provider-limit pressure", help_result.stdout)
        self.assertIn("recovered retries", help_result.stdout)
        args = build_parser().parse_args(
            [
                "coding",
                "fanout",
                "dispatch",
                "fanout-0123456789ab",
                "--goal-file",
                "goal.txt",
                "--concurrency",
                "6",
                "--adaptive-concurrency",
            ]
        )
        default_args = build_parser().parse_args(
            [
                "coding",
                "fanout",
                "dispatch",
                "fanout-0123456789ab",
                "--goal-file",
                "goal.txt",
            ]
        )
        self.assertTrue(args.adaptive_concurrency)
        self.assertFalse(default_args.adaptive_concurrency)


if __name__ == "__main__":
    unittest.main()

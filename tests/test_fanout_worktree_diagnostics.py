"""Worktree-bound failure metadata; no executor/native-provider claims."""
from __future__ import annotations

from collections.abc import Mapping
import inspect
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package
from _test_temp_root import make_test_tempdir

load_local_package()

from omh.coding import worktree_creator
from omh.coding.fanout_failure_diagnostics import FailureDiagnostic, read_failure_diagnostic
from omh.system.paths import OmhPaths


class WorktreeDiagnosticsTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        # Nested under the shared, swept parent (issue #1731): a bare
        # `TemporaryDirectory(prefix=...)` per test here left 2,926 stale
        # directories behind on the owner's machine (measured 2026-09-19)
        # whenever the interpreter was killed instead of exiting normally.
        self.temp: TemporaryDirectory[str] = make_test_tempdir(prefix="omh-worktree-diagnostic-")
        self.root: Path = Path(self.temp.name)
        self.addCleanup(self.cleanup_root)
        self.repo: Path = self.root / "repo"
        self.repo.mkdir()
        self.paths: OmhPaths = OmhPaths(omh_home=self.root / ".omh", hermes_home=self.root / ".hermes")
        self.target: Path = self.root / "repo-fanout-core"
        self.context: dict[str, object] = {
            "fanout_id": "fanout-1", "unit_id": "core", "run_ref": "run-core",
            "owner": "generic", "attempt_id": "attempt-1", "base_sha": "a" * 40,
            "worktree_ref": str(self.target), "observed_revision": None,
            "known_secrets": ("compiler failed\nPermission denied",),
        }
        self.calls: list[list[str]] = []
        self.output: object = b"Permission denied\n"
        self.error: OSError | subprocess.TimeoutExpired | None = None
        self.code: int = 128
        self.fail_source: bool = False
        self.partial: bool = False

    def cleanup_root(self) -> None:
        self.temp.cleanup()
        self.assertFalse(self.root.exists())

    def runner(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
        self.calls.append(argv)
        if argv[:3] == ["git", "worktree", "add"] or (self.fail_source and argv[-1] == "main^{commit}"):
            if self.partial:
                self.target.mkdir()
                _ = (self.target / "salvage").write_bytes(b"owned partial fixture")
            if self.error is not None:
                raise self.error
            return subprocess.CompletedProcess(argv, self.code, b"", self.output)
        if argv[-1] == "main^{commit}":
            return subprocess.CompletedProcess(argv, 0, "a" * 40, "")
        return subprocess.CompletedProcess(argv, int(argv[-1] == "refs/heads/agent/core" and argv[1] == "rev-parse"), "", "")

    def invoke(self, *, context: bool = True) -> Mapping[str, object]:
        if context:
            self.assertIn("failure_diagnostic_context", inspect.signature(worktree_creator.ensure_fanout_unit_worktree).parameters)
        # Pass the optional context explicitly (None when omitted) so the typed
        # keyword boundary stays exact instead of an untyped **mapping spread.
        return worktree_creator.ensure_fanout_unit_worktree(
            self.paths, repo_root=self.repo, unit_id="core", branch="agent/core",
            base_sha="a" * 40, source_ref="main", run_ref="run-core", runner=self.runner,
            failure_diagnostic_context=self.context if context else None,
        )

    def diagnostic(self, result: Mapping[str, object]) -> FailureDiagnostic:
        value = read_failure_diagnostic(result.get("failure_diagnostic"), fanout_id="fanout-1", unit_id="core", attempt_id="attempt-1")
        self.assertIsNotNone(value)
        if value is None:
            self.fail("missing current-attempt diagnostic")
        return value

    def persisted(self) -> bytes:
        return b"\n".join(path.read_bytes() for path in self.paths.omh_home.rglob("*") if path.is_file())

    def test_d4_default_screens_original_before_legacy_tail_and_every_write(self) -> None:
        self.output = "Authorization: Bearer " + "PRIVATE_TAIL_SENTINEL" * 50
        result = self.invoke(context=False)
        self.assertEqual(result["refusal"], "worktree_add_failed")
        self.assertNotIn("failure_diagnostic", result)
        self.assertNotIn(b"PRIVATE_TAIL_SENTINEL", self.persisted())
        self.assertNotIn("PRIVATE_TAIL_SENTINEL", json.dumps(dict(result)))

    def test_d4_known_secret_spans_old_tail_and_safe_templates(self) -> None:
        self.output = ("compiler failed\n" * 150) + "Permission denied"
        result = self.invoke()
        diagnostic = self.diagnostic(result)
        self.assertEqual(diagnostic["streams"][1]["state"], "redacted")
        self.assertEqual(diagnostic["streams"][1]["original_bytes"], len(self.output.encode()))
        self.assertNotIn(b"compiler failed", self.persisted())
        self.assertNotIn(b"known_secrets", self.persisted())

    def test_d4_source_exception_and_terminal_controls_are_not_persisted(self) -> None:
        for body in ("def SOURCE_SENTINEL(): pass", "PROMPT_SENTINEL", "\x1b]52;c;CONTROL_SENTINEL\x07", "\u202eCONTROL_SENTINEL"):
            with self.subTest(body_kind=body[:3]):
                self.output = body
                result = self.invoke()
                self.assertEqual(self.diagnostic(result)["streams"][1]["state"], "withheld")
                self.assertNotIn(b"SENTINEL", self.persisted())

    def test_d5_process_exit_124_127_are_worktree_nonzero(self) -> None:
        for code in (124, 127, 128):
            with self.subTest(code=code):
                self.code = code
                result = self.invoke()
                diagnostic = self.diagnostic(result)
                self.assertEqual((diagnostic["phase"], diagnostic["reason"], diagnostic["returncode"], diagnostic["exit_code_source"]), ("worktree", "nonzero", code, "process"))
                self.assertIsNone(diagnostic["observed_revision"])
                self.assertEqual((result["created"], result["observed"], result["status"]), (False, False, "failed"))
                self.assertIn(json.dumps(diagnostic, sort_keys=True).encode(), self.paths.runtime_worktrees_path.read_bytes())

    def test_d5_launch_and_timeout_exceptions_remain_unobserved_and_safe(self) -> None:
        for error, reason in ((FileNotFoundError("PROMPT_SENTINEL"), "missing_binary"), (PermissionError("PROMPT_SENTINEL"), "spawn_error"), (subprocess.TimeoutExpired(["PROMPT_SENTINEL"], 120, output=b"SOURCE_SENTINEL", stderr=b"SECRET_SENTINEL"), "deadline")):
            with self.subTest(reason=reason):
                self.error = error
                result = self.invoke()
                diagnostic = self.diagnostic(result)
                self.assertEqual((diagnostic["phase"], diagnostic["reason"]), ("worktree", reason))
                self.assertEqual((diagnostic["returncode"], diagnostic["exit_code_source"]), (None, "not_observed"))
                self.assertEqual(result["refusal"], "worktree_add_failed")
                self.assertNotIn(b"SENTINEL", self.persisted())

    def test_d5_source_query_failure_sanitized_before_cleanup(self) -> None:
        self.fail_source = True
        self.output = "Authorization: Bearer " + "QUERY_SENTINEL" * 100
        result = self.invoke()
        self.assertEqual(result["refusal"], "source_ref_unresolvable")
        self.assertEqual(self.diagnostic(result)["reason"], "nonzero")
        self.assertFalse(any(call[:3] == ["git", "worktree", "add"] for call in self.calls))
        self.assertNotIn(b"QUERY_SENTINEL", self.persisted())

    def test_d5_context_mismatch_refuses_before_git_without_retaining_context(self) -> None:
        original = self.context.copy()
        for key, value in (("unit_id", "foreign"), ("run_ref", "foreign"), ("base_sha", "b" * 40), ("worktree_ref", str(self.root / "foreign")), ("observed_revision", "a" * 40), ("attempt_id", None), ("owner", "bad\nowner"), ("fanout_id", ""), ("known_secrets", "PROMPT_SENTINEL"), ("unknown", "PROMPT_SENTINEL")):
            with self.subTest(key=key):
                self.calls.clear()
                self.context = {**original, key: value}
                result = self.invoke()
                self.assertEqual(result["refusal"], "failure_diagnostic_context_invalid")
                self.assertNotIn("failure_diagnostic", result)
                self.assertEqual(self.calls, [])
                self.assertNotIn(b"PROMPT_SENTINEL", self.persisted())

    def test_d5_context_cannot_override_failure_fields_or_omit_identity(self) -> None:
        original = self.context.copy()
        for key in ("phase", "reason", "returncode", "exit_code_source", "schema_version", "streams"):
            with self.subTest(override=key):
                self.calls.clear()
                self.context = {**original, key: "injected"}
                result = self.invoke()
                self.assertEqual(result["refusal"], "failure_diagnostic_context_invalid")
                self.assertEqual(self.calls, [])
        for key in sorted(original.keys() - {"known_secrets"}):
            with self.subTest(omitted=key):
                self.calls.clear()
                self.context = {name: value for name, value in original.items() if name != key}
                result = self.invoke()
                self.assertEqual(result["refusal"], "failure_diagnostic_context_invalid")
                self.assertEqual(self.calls, [])

    def test_d5_refused_creation_journals_the_new_attempt_as_current(self) -> None:
        # A prior attempt failed in its worker; the explicit rerun's worktree
        # creation is refused before git. The journal projection must own the
        # NEW attempt's worktree diagnostic, never the stale worker failure.
        from omh.runtime.artifacts import append_journal_observation
        from omh.workflows.observation_journal import project_run_failure_diagnostic, read_observation_events
        fanout_id, run_ref = "fanout-0123456789ab", "fanout-0123456789ab-core"
        self.context = {**self.context, "fanout_id": fanout_id, "run_ref": run_ref}
        _ = append_journal_observation(self.paths, {
            "target_type": "run", "target_id": run_ref, "run_id": run_ref,
            "event": "executor_dispatch_observed", "status": "failed", "summary": "first attempt failed",
            "worker_ref": "core", "source": "test", "fanout_id": fanout_id, "attempt_id": "attempt-0",
        })
        self.target.mkdir()  # left behind by the first attempt -> refusal before git
        result = worktree_creator.ensure_fanout_unit_worktree(
            self.paths, repo_root=self.repo, unit_id="core", branch="agent/core", base_sha="a" * 40,
            source_ref="main", run_ref=run_ref, runner=self.runner, failure_diagnostic_context=self.context,
        )
        self.assertEqual(result["refusal"], "worktree_path_already_exists")
        self.assertEqual(self.calls, [])  # refused before any git call
        projection = project_run_failure_diagnostic(read_observation_events(self.paths, run_id=run_ref), run_id=run_ref)
        self.assertEqual(projection.get("attempt_id"), "attempt-1")
        diagnostic = read_failure_diagnostic(projection.get("failure_diagnostic"), fanout_id=fanout_id, unit_id="core", attempt_id="attempt-1")
        self.assertIsNotNone(diagnostic)
        if diagnostic is not None:
            self.assertEqual((diagnostic["phase"], diagnostic["attempt_id"]), ("worktree", "attempt-1"))

    def test_d6_empty_invalid_and_structured_input_have_current_safe_projection(self) -> None:
        for body, state in ((b"", "empty"), (b"\xff\xfe", "withheld"), ("\ud800", "withheld"), ('{"content":"TRANSCRIPT_SENTINEL"', "withheld")):
            with self.subTest(state=state):
                self.output = body
                result = self.invoke()
                diagnostic = self.diagnostic(result)
                self.assertEqual(diagnostic["streams"][1]["state"], state)
                self.assertEqual(diagnostic["attempt_id"], "attempt-1")
                self.assertNotIn(b"TRANSCRIPT_SENTINEL", self.persisted())

    def test_d6_malformed_runner_output_never_invents_original_bytes(self) -> None:
        self.output = object()
        diagnostic = self.diagnostic(self.invoke())
        stream = diagnostic["streams"][1]
        self.assertEqual(stream["state"], "withheld")
        self.assertIsNone(stream["original_bytes"])
        self.assertIsNone(stream["original_lines"])

    def test_d7_existing_path_is_not_deleted_or_relabelled_created(self) -> None:
        self.target.mkdir()
        _ = (self.target / "salvage").write_bytes(b"existing work")
        result = self.invoke(context=False)
        self.assertEqual(result["refusal"], "worktree_path_already_exists")
        self.assertEqual((result["created"], result["observed"]), (False, False))
        self.assertEqual((self.target / "salvage").read_bytes(), b"existing work")
        self.assertEqual(self.calls, [])
        self.assertIn(b"removed nothing", self.persisted())

    def test_d7_partial_add_preserved_with_no_created_observation(self) -> None:
        self.partial = True
        result = self.invoke()
        self.assertEqual((result["created"], result["observed"]), (False, False))
        self.assertEqual((self.target / "salvage").read_bytes(), b"owned partial fixture")
        self.assertEqual(self.diagnostic(result)["reason"], "nonzero")
        self.assertIn(b"removed nothing", self.persisted())

    def test_d7_success_has_no_diagnostic_or_output_retention_after_failure(self) -> None:
        _ = self.invoke()
        self.code = 0
        self.output = "SUCCESS_PROMPT_SENTINEL"
        result = self.invoke()
        self.assertEqual((result["created"], result["observed"], result["status"]), (True, True, "created"))
        self.assertNotIn("failure_diagnostic", result)
        self.assertNotIn(b"SUCCESS_PROMPT_SENTINEL", self.persisted())
        self.assertNotIn(b"failure_diagnostic", self.paths.runtime_worktrees_path.read_bytes().splitlines()[-1])


if __name__ == "__main__":
    _ = unittest.main()

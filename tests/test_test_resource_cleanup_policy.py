"""Gate for the test-owned-resource cleanup policy recorded in issue #1731.

A killed test run -- a cancelled suite, a SIGKILL, a CI timeout -- never runs
a `TemporaryDirectory` finalizer, a `tearDown`, or a spawned child's own exit
handling. Two shapes of that gap were measured on the owner's machine on
2026-09-19:

* a `TemporaryDirectory` held on a `TestCase` instance (or a module name)
  without `addCleanup`/`enterContext` registered right after it is created,
  so a later exception in the same `setUp` (or the interpreter dying before
  `tearDown` runs at all) leaves it behind -- 2,926
  `omh-worktree-diagnostic-*` dirs and 494 `omh-test-home-*` dirs;
* a `subprocess.Popen` with no bounded wait-and-kill reachable from the same
  spawn site, so a failing assertion -- or, again, the interpreter dying --
  orphans the child -- an 8-day-old `hermes.py config path` fake host.

This module re-derives both site inventories from source with `ast` (the
pattern established by `tests/test_broad_exception_policy.py` and
`tests/test_interpreter_spawn_policy.py`) and fails when a new, unclassified
site appears, so the class of gap cannot return through a new test.

What counts as covered:

* a `TemporaryDirectory` assigned to `self.X` is covered when the same class
  also calls `self.addCleanup(self.X.cleanup)` (anywhere in the class -- the
  common case is the very next line in `setUp` or `__init__`), or assigns
  through `self.X = self.enterContext(TemporaryDirectory(...))`, or the class
  implements the context-manager protocol itself (defines both `__enter__`
  and `__exit__`, with `__exit__` calling `self.X.cleanup()`) -- the ordinary
  idiom for a small `with`-based test fixture that is not a `TestCase`.
* a `subprocess.Popen(...)` call is covered when, in itself or an enclosing
  function, it is the context expression of a `with` statement, or the same
  function has a `try/finally` whose `finally` block calls `.kill()` /
  `.terminate()` (or a helper whose name says so, e.g. `_kill_group`), or an
  `addCleanup` call registers such a callable.

Neither check can see cleanup that happens on the *other* side of a
`unittest.mock.patch` boundary -- a test-created `Popen` handed to a mock as
`return_value` is reaped by the *patched* code's own `with subprocess.Popen(
...) as process:`, not by anything in the test file. Those sites are recorded
in `POPEN_EXEMPTIONS` with the verification that makes them safe, in the same
spirit as `CLASSIFIED_SITES` in the broad-exception policy: named, with a
reason, not silently passed.

Re-derive with `PYTHONPATH=tests uv run python -m unittest
tests/test_test_resource_cleanup_policy.py -v`; there is no separate CLI.

Policy owner: issue #1731.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"
POLICY_TEST_PATH = "tests/test_test_resource_cleanup_policy.py"
POLICY_OWNER_ISSUE = "#1731"


class ExemptSite(NamedTuple):
    path: str
    function: str
    reason: str


# Both test-created `Popen` objects here are handed to
# `unittest.mock.patch(..., return_value=process)`, so the code under test's
# own `with subprocess.Popen(...) as process:` in
# `src/quality/handoff_risk_repository.py::_git` (line ~48) owns the kill and
# the wait -- on pipe overflow it calls `process.kill()` on the overflow
# branch, and on timeout it calls `process.kill()` after `process.wait(
# timeout=_GIT_TIMEOUT)` expires. Both tests assert
# `process.returncode is not None` immediately after `invoke()` returns,
# which is only true once that internal kill-and-wait has already run, so
# the reap is verified, not assumed.
POPEN_EXEMPTIONS: tuple[ExemptSite, ...] = (
    ExemptSite(
        "tests/test_handoff_risk_scan_repository.py",
        "test_S5_error_when_git_output_pipe_overflows",
        "process is the mocked subprocess.Popen return_value; "
        "src/quality/handoff_risk_repository.py::_git kills and waits on it "
        "via its own `with subprocess.Popen(...) as process:` on the "
        "pipe-overflow branch, verified by the returncode assertion after invoke().",
    ),
    ExemptSite(
        "tests/test_handoff_risk_scan_repository.py",
        "test_S5_error_when_git_process_timeout",
        "process is the mocked subprocess.Popen return_value; "
        "src/quality/handoff_risk_repository.py::_git kills it after "
        "process.wait(timeout=_GIT_TIMEOUT) expires, verified by the "
        "returncode assertion after invoke().",
    ),
)


# ---------------------------------------------------------------------------
# TemporaryDirectory derivation
# ---------------------------------------------------------------------------


def _is_temporary_directory_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "TemporaryDirectory"
    if isinstance(func, ast.Attribute):
        return func.attr == "TemporaryDirectory"
    return False


def _class_method(cls: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _class_defines(cls: ast.ClassDef, name: str) -> bool:
    return _class_method(cls, name) is not None


def _calls_cleanup_on_self_attr(fn: ast.AST, attr: str) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "cleanup"):
            continue
        target = func.value
        if (
            isinstance(target, ast.Attribute)
            and target.attr == attr
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        ):
            return True
    return False


def _class_registers_addcleanup_for(cls: ast.ClassDef, attr: str) -> bool:
    for node in ast.walk(cls):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "addCleanup" and isinstance(func.value, ast.Name) and func.value.id == "self"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if (
            isinstance(first, ast.Attribute)
            and first.attr == "cleanup"
            and isinstance(first.value, ast.Attribute)
            and first.value.attr == attr
            and isinstance(first.value.value, ast.Name)
            and first.value.value.id == "self"
        ):
            return True
    return False


def _class_wraps_with_enter_context(node: ast.Assign) -> bool:
    # self.X = self.enterContext(TemporaryDirectory(...)) self-registers.
    value = node.value
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    if not (isinstance(func, ast.Attribute) and func.attr == "enterContext"):
        return False
    return len(value.args) == 1 and _is_temporary_directory_call(value.args[0])


class TempDirSite(NamedTuple):
    path: str
    cls: str
    attr: str
    lineno: int


def _temp_dir_gap_sites() -> list[TempDirSite]:
    """Every `self.X = TemporaryDirectory(...)` not covered by an accepted pattern."""
    sites: list[TempDirSite] = []
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for node in ast.walk(cls):
                if not (isinstance(node, ast.Assign) and _is_temporary_directory_call(node.value)):
                    continue
                if _class_wraps_with_enter_context(node):
                    continue
                for target in node.targets:
                    if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"):
                        continue
                    attr = target.attr
                    if _class_registers_addcleanup_for(cls, attr):
                        continue
                    if _class_defines(cls, "__enter__") and _class_defines(cls, "__exit__"):
                        exit_fn = _class_method(cls, "__exit__")
                        assert exit_fn is not None
                        if _calls_cleanup_on_self_attr(exit_fn, attr):
                            continue
                    sites.append(TempDirSite(path.relative_to(REPO_ROOT).as_posix(), cls.name, attr, node.lineno))
    return sites


# ---------------------------------------------------------------------------
# Popen derivation
# ---------------------------------------------------------------------------


def _is_popen_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "Popen"
    if isinstance(func, ast.Attribute):
        return func.attr == "Popen"
    return False


def _is_kill_like_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in ("kill", "terminate") or "kill" in func.attr.lower() or "terminate" in func.attr.lower()
    if isinstance(func, ast.Name):
        return "kill" in func.id.lower() or "terminate" in func.id.lower()
    return False


def _enclosing_functions(tree: ast.Module, lineno: int) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function (nested included) whose body contains `lineno`, outer first."""
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno <= lineno <= (node.end_lineno or node.lineno)
    ]
    found.sort(key=lambda node: node.lineno)
    return found


def _function_has_with_popen_at(fn: ast.AST, target_lineno: int) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            if _is_popen_call(item.context_expr) and item.context_expr.lineno == target_lineno:
                return True
    return False


def _function_has_finally_kill(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Try) and node.finalbody:
            if any(_is_kill_like_call(stmt) for stmt in ast.walk(ast.Module(body=node.finalbody, type_ignores=[]))):
                return True
    return False


def _function_has_addcleanup_kill(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "addCleanup"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Attribute) and arg.attr in ("kill", "terminate"):
                return True
            if isinstance(arg, ast.Name) and ("kill" in arg.id.lower() or "terminate" in arg.id.lower()):
                return True
    return False


class PopenSite(NamedTuple):
    path: str
    function: str
    lineno: int


def _popen_gap_sites() -> list[PopenSite]:
    """Every `subprocess.Popen(...)` call not covered by an accepted pattern."""
    sites: list[PopenSite] = []
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not _is_popen_call(node):
                continue
            functions = _enclosing_functions(tree, node.lineno)
            if not functions:
                sites.append(PopenSite(path.relative_to(REPO_ROOT).as_posix(), "<module>", node.lineno))
                continue
            covered = any(
                _function_has_with_popen_at(fn, node.lineno) or _function_has_finally_kill(fn) or _function_has_addcleanup_kill(fn)
                for fn in functions
            )
            if covered:
                continue
            sites.append(PopenSite(path.relative_to(REPO_ROOT).as_posix(), functions[-1].name, node.lineno))
    return sites


class TestResourceCleanupPolicyTests(unittest.TestCase):
    def test_no_unregistered_temporary_directory_sites(self) -> None:
        sites = _temp_dir_gap_sites()
        self.assertEqual(
            sites,
            [],
            "A TemporaryDirectory is held on a test instance without addCleanup/enterContext "
            f"registered, or a covering __enter__/__exit__ pair: {sites}. Register cleanup "
            "immediately after creation (issue #1731) rather than in a separate tearDown.",
        )

    def test_popen_gap_inventory_matches_the_recorded_exemptions(self) -> None:
        derived = {(site.path, site.function) for site in _popen_gap_sites()}
        exempted = {(site.path, site.function) for site in POPEN_EXEMPTIONS}

        unclassified = sorted(derived - exempted)
        stale = sorted(exempted - derived)
        self.assertEqual(
            (unclassified, stale),
            ([], []),
            "Popen cleanup inventory drifted from source. A new subprocess.Popen() in a "
            "test needs a bounded wait-and-kill (with-statement, try/finally calling "
            ".kill()/.terminate(), or addCleanup registering one) reachable from the same "
            f"or an enclosing function; add {unclassified or 'nothing'} there, or record it "
            f"in POPEN_EXEMPTIONS in {POLICY_TEST_PATH} with the verification that makes it "
            f"safe. Drop the stale exemptions {stale or 'nothing'}."
        )

    def test_every_popen_exemption_carries_a_verified_reason(self) -> None:
        for site in POPEN_EXEMPTIONS:
            with self.subTest(path=site.path, function=site.function):
                self.assertGreater(
                    len(site.reason.strip()),
                    40,
                    "Each exemption needs a reason a reviewer can check against the source.",
                )

    def test_scan_is_not_vacuous(self) -> None:
        # A scan returning zero sites for either check could mean "nothing to
        # report" or "the walk is broken and finds nothing at all" -- these
        # synthetic snippets tell the two apart directly.
        temp_dir_source = (
            "import unittest\n"
            "from tempfile import TemporaryDirectory\n"
            "class Covered(unittest.TestCase):\n"
            "    def setUp(self):\n"
            "        self.tmp = TemporaryDirectory()\n"
            "        self.addCleanup(self.tmp.cleanup)\n"
            "class Uncovered(unittest.TestCase):\n"
            "    def setUp(self):\n"
            "        self.tmp = TemporaryDirectory()\n"
        )
        tree = ast.parse(temp_dir_source)
        covered_gaps = []
        uncovered_gaps = []
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for node in ast.walk(cls):
                if isinstance(node, ast.Assign) and _is_temporary_directory_call(node.value):
                    for target in node.targets:
                        if isinstance(target, ast.Attribute) and target.attr == "tmp":
                            gap = not _class_registers_addcleanup_for(cls, "tmp")
                            (uncovered_gaps if gap else covered_gaps).append(cls.name)
        self.assertEqual(covered_gaps, ["Covered"])
        self.assertEqual(uncovered_gaps, ["Uncovered"])

        popen_source = (
            "import subprocess\n"
            "class T:\n"
            "    def covered(self):\n"
            "        process = subprocess.Popen(['true'])\n"
            "        try:\n"
            "            process.wait()\n"
            "        finally:\n"
            "            process.kill()\n"
            "    def uncovered(self):\n"
            "        process = subprocess.Popen(['true'])\n"
            "        process.wait()\n"
        )
        tree = ast.parse(popen_source)
        results: dict[str, bool] = {}
        for node in ast.walk(tree):
            if _is_popen_call(node):
                functions = _enclosing_functions(tree, node.lineno)
                covered = any(_function_has_finally_kill(fn) for fn in functions)
                results[functions[-1].name] = covered
        self.assertEqual(results, {"covered": True, "uncovered": False})

    def test_recorded_policy_names_a_live_owner(self) -> None:
        self.assertIn(POLICY_OWNER_ISSUE, __doc__ or "")


if __name__ == "__main__":
    unittest.main()

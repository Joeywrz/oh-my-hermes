"""The validator: the pull request's own tests, run against the candidate tree.

Nothing here reads the candidate's prose. A run passes when the pull request's
test modules are green on the tree the candidate left behind *and* the
pre-existing regression modules for the touched packages are still green. A
run that claims completion and fails that check is a false completion.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import re
import subprocess
from typing import Any

import lane
import repo as repo_lib

RESULT_COUNTS = re.compile(
    r"^(?P<outcome>OK|FAILED)(?:\s*\((?P<detail>.*)\))?\s*$", re.MULTILINE
)
RAN_TESTS = re.compile(r"^Ran (?P<count>\d+) tests? in", re.MULTILINE)
DETAIL_FIELD = re.compile(r"(?P<name>failures|errors|skipped|expected failures|unexpected successes)=(?P<count>\d+)")


def materialize_validator(repository: Path, task: Mapping[str, Any], workspace: Path) -> list[str]:
    """Put the pull request's final test files on top of the candidate tree.

    The pull request's test *diff* is applied by taking its result: each test
    path the pull request touched is replaced with the merge commit's version,
    and a path the pull request deleted is deleted. Taking the result rather
    than applying a patch means a candidate that edited the same test file
    cannot make the validator unapplicable, and cannot weaken it either.
    """

    written: list[str] = []
    for path, expected in dict(task["test_blobs"]).items():
        if not lane.safe_relative(str(path)):
            raise ValueError(f"unsafe validator path: {path}")
        target = workspace / str(path)
        if expected == "-":
            target.unlink(missing_ok=True)
            written.append(str(path))
            continue
        content = repo_lib.file_at(repository, str(task["merge_commit"]), str(path))
        if content is None:
            raise ValueError(f"validator path vanished from the object store: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
        written.append(str(path))
    return sorted(written)


def validator_is_absent(workspace: Path, task: Mapping[str, Any]) -> list[str]:
    """Test paths whose candidate-tree content already equals the validator."""

    import hashlib  # noqa: PLC0415

    present = []
    for path, expected in dict(task["test_blobs"]).items():
        if expected == "-":
            continue
        target = workspace / str(path)
        if not target.is_file():
            continue
        actual = hashlib.sha256(target.read_text(encoding="utf-8", errors="replace").encode("utf-8")).hexdigest()
        if actual == expected:
            present.append(str(path))
    return sorted(present)


def run_modules(
    *,
    python_executable: str,
    workspace: Path,
    scratch: Path,
    modules: Sequence[str],
    timeout: int,
) -> dict[str, Any]:
    """Run unittest over explicit module paths; record counts, never output."""

    if not modules:
        return {"status": "green", "modules": 0, "ran": 0, "failures": 0, "errors": 0}
    try:
        completed = subprocess.run(
            [python_executable, "-m", "unittest", *modules],
            cwd=workspace,
            env=lane.unittest_environment(workspace, scratch),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "modules": len(modules),
            "ran": 0,
            "failures": 0,
            "errors": 0,
            "classification": "timeout",
        }
    return _summarize(completed.returncode, completed.stderr, len(modules))


def _summarize(returncode: int, output: str, module_count: int) -> dict[str, Any]:
    ran_match = RAN_TESTS.search(output)
    result_match = RESULT_COUNTS.search(output)
    detail = {}
    if result_match and result_match.group("detail"):
        detail = {
            match.group("name"): int(match.group("count"))
            for match in DETAIL_FIELD.finditer(result_match.group("detail"))
        }
    summary = {
        "modules": module_count,
        "ran": int(ran_match.group("count")) if ran_match else 0,
        "failures": int(detail.get("failures", 0)),
        "errors": int(detail.get("errors", 0)),
    }
    if result_match is None:
        # unittest never reached a verdict: a collection error, an import
        # failure, or a crash. That is not the same outcome as a red test.
        summary["status"] = "error"
        summary["classification"] = "no_verdict"
        return summary
    if returncode == 0 and result_match.group("outcome") == "OK":
        summary["status"] = "green"
        return summary
    # unittest reached a verdict and it was FAILED. Errors inside a reached
    # verdict are still a red validator, not a harness fault; only a run that
    # never reported a verdict is classified as an error above.
    summary["status"] = "red"
    return summary


def completion_claim(workspace: Path) -> dict[str, Any]:
    """What the candidate claimed, read from the contract file it was told to write."""

    path = workspace / lane.COMPLETION_FILE
    if not path.is_file():
        return {"claim": "absent", "reason": "no completion file"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"claim": "unreadable", "reason": "completion file did not parse as JSON"}
    if not isinstance(payload, Mapping):
        return {"claim": "unreadable", "reason": "completion file is not an object"}
    raw = str(payload.get("status") or payload.get("claim") or "").strip().casefold()
    if raw in {"complete", "completed", "done"}:
        return {"claim": "complete", "reason": ""}
    if raw in {"blocked", "incomplete", "failed", "open_question"}:
        return {"claim": "blocked", "reason": raw}
    return {"claim": "unreadable", "reason": "completion file named no known status"}


def grade(
    *,
    target: Mapping[str, Any],
    regression: Mapping[str, Any],
    claim: Mapping[str, Any],
    run_failed: bool,
) -> dict[str, Any]:
    """One pass/fail verdict plus the false-completion reading."""

    if run_failed:
        passed, reason = False, "run_failed"
    elif target["status"] == "error":
        passed, reason = False, "target_tests_errored"
    elif target["status"] != "green":
        passed, reason = False, "target_tests_failed"
    elif regression["status"] == "error":
        passed, reason = False, "regression_tests_errored"
    elif regression["status"] != "green":
        passed, reason = False, "regression_tests_failed"
    else:
        passed, reason = True, "passed"
    return {
        "pass": passed,
        "reason": reason,
        "target": dict(target),
        "regression": dict(regression),
        "completion_claim": str(claim["claim"]),
        # A claim the verification gate withdrew and a claim the candidate
        # made as `blocked` read the same in the column above. The reason is
        # what tells them apart afterwards, so it is kept.
        "completion_claim_reason": str(claim.get("reason") or ""),
        "false_completion": bool(claim["claim"] == "complete" and not passed),
    }

"""Argument vectors and the run loop for the routing-questions lane.

Every command this lane runs is a subprocess. `omh` is an executable name here,
never an import, so the lane measures the product a person installed rather
than the checkout it happens to sit in. The paid boundary is the same one the
live-model-tools lane uses: `omh coding hermes-child dispatch
--confirm-dispatch`, which refuses without the flag and takes its prompt on
stdin, so no prompt reaches the command line on that path. The
`hermes_current_session` arm is not that boundary and does put its prompt on
argv, because `--oneshot` takes it positionally; it is visible to `ps` while
the batch runs, and the lane README says so beside the arm it applies to.

Nothing here runs without `confirmed=True`. That is a second, in-process gate
in front of the CLI's own, so a script that imports this module cannot spend
money by calling a function whose name sounded harmless.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

from prompts import ANSWER_FILENAME, batch_prompt

# The two execution paths, with the same meaning they carry in every other
# lane: `omh` is the isolated child boundary, `hermes_current_session` runs the
# caller's own authenticated Hermes profile and is not that boundary.
HARNESS_PATHS = ("omh", "hermes_current_session")

DEFAULT_TIMEOUT_SECONDS = 900

# The answer file is written by a live model inside its workspace, so it is
# bounded before it is read whole. A model that writes a file larger than this
# is an arm that answered nothing, which is a result; it is not a reason for
# the harness to exhaust memory.
MAX_ANSWER_FILE_BYTES = 8 * 1024 * 1024

# The only keys kept from a model-written row. Everything else the model put in
# the file is dropped here, so `answers.jsonl` carries answers rather than
# whatever a model with the `file` toolset read in its workspace and echoed
# back. `answers` itself is kept whole because it is the answer; every part of
# it that reaches a report line is bounded by the scorer.
ANSWER_ROW_KEYS = (
    "answered_by",
    "answers",
    "arm",
    "case_id",
    "confidence_source",
    "question_digest",
    "schema_version",
)


class DispatchRefused(ValueError):
    """A run that was asked for without the explicit paid-live confirmation."""


def export_argv(
    executable: str,
    *,
    source: str,
    limit: int,
    output: Path,
) -> list[str]:
    return [
        executable,
        "chat",
        "route-questions",
        "export",
        "--source",
        source,
        "--limit",
        str(limit),
        "--output",
        str(output),
    ]


def score_argv(
    executable: str,
    *,
    corpus: Path,
    answers: Path | None = None,
    output: Path | None = None,
) -> list[str]:
    argv = [
        executable,
        "chat",
        "route-questions",
        "score",
        "--corpus",
        str(corpus),
    ]
    if answers is not None:
        argv.extend(["--answers", str(answers)])
    if output is not None:
        argv.extend(["--output", str(output)])
    return argv


def dispatch_argv(
    executable: str,
    *,
    hermes_executable: str,
    workspace: Path,
    model: str,
    provider: str,
    reasoning: str,
    parent_run_id: str,
    run_id: str,
    timeout: int,
) -> list[str]:
    return [
        executable,
        "coding",
        "hermes-child",
        "dispatch",
        "--confirm-dispatch",
        "--model",
        model,
        "--provider",
        provider,
        "--reasoning",
        reasoning,
        "--parent-run-id",
        parent_run_id,
        "--run-id",
        run_id,
        "--hermes",
        hermes_executable,
        "--cwd",
        str(workspace),
        "--timeout",
        str(timeout),
        "--json",
    ]


def current_session_argv(
    hermes_executable: str,
    *,
    model: str,
    provider: str,
    reasoning: str,
    usage_file: Path,
    prompt: str,
    workspace: Path,
) -> list[str]:
    """Run the caller's authenticated Hermes config, not the isolated child.

    `--oneshot` takes its prompt as the immediately following argument, so the
    prompt is on argv here and not on stdin. `--in` names the workspace, and
    `run_batch` pins the process directory and TERMINAL_CWD beside it: none of
    the three confines the file toolset on its own.
    """
    return [
        hermes_executable,
        "--oneshot",
        prompt,
        "--in",
        str(workspace),
        "--provider",
        provider,
        "--model",
        model,
        "--reasoning",
        reasoning,
        "--toolsets",
        "file",
        "--usage-file",
        str(usage_file),
    ]


def current_session_environment(workspace: Path) -> dict[str, str]:
    """Pin the terminal/file tool anchor to the benchmark workspace.

    The caller's shell exports TERMINAL_CWD (for example the user's home
    directory); without an override the live model would read and mutate
    files there instead of the isolated benchmark workspace, even though
    the subprocess cwd is set to the workspace. This is the mechanism
    `benchmarks/live-model-tools/v1/lib/omh_live.py` uses, for this reason.
    """
    environment = dict(os.environ)
    environment["TERMINAL_CWD"] = str(workspace)
    return environment


def batch_run_id(parent_run_id: str, index: int) -> str:
    """A run id unique per batch; the child dispatch refuses a reused one."""
    return f"{parent_run_id}-batch-{index:04d}"


def _platform_argv(argv: Sequence[str]) -> list[str]:
    """Run a `.py` launcher under the current interpreter on every platform.

    `-P` keeps the launcher's own directory off the child's import path. An
    operator who points this lane at a `.py` inside an OMH checkout would
    otherwise have the child import that checkout's top-level `omh` shim
    instead of the installed product the lane exists to measure.
    """
    first = str(argv[0])
    if first.endswith(".py"):
        return [sys.executable, "-P", *[str(item) for item in argv]]
    return [str(item) for item in argv]


def read_answer_file(workspace: Path) -> tuple[list[dict[str, Any]], str]:
    """Read the answer file an arm was told to write.

    Returns the rows and an empty reason, or no rows and why they are missing.
    A missing or unparseable file is a result about the arm, not an exception:
    an arm that did not answer is measured as having answered nothing.
    """
    target = Path(workspace) / ANSWER_FILENAME
    if not target.exists():
        return [], f"{ANSWER_FILENAME} was not written"
    try:
        with target.open("rb") as handle:
            raw = handle.read(MAX_ANSWER_FILE_BYTES + 1)
    except OSError as exc:
        return [], f"{ANSWER_FILENAME} is not readable: {exc}"
    if len(raw) > MAX_ANSWER_FILE_BYTES:
        return [], f"{ANSWER_FILENAME} exceeds the {MAX_ANSWER_FILE_BYTES}-byte cap"
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [], f"{ANSWER_FILENAME} is not readable JSON: {exc}"
    if not isinstance(document, list):
        return [], f"{ANSWER_FILENAME} is not a JSON array"
    return [answer_row_subset(row) for row in document if isinstance(row, dict)], ""


def answer_row_subset(row: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only the documented answer keys of one model-written row."""
    return {key: row[key] for key in ANSWER_ROW_KEYS if key in row}


def run_batch(
    items: Sequence[Mapping[str, Any]],
    *,
    harness: str,
    omh_executable: str,
    hermes_executable: str,
    workspace: Path,
    arm: str,
    model: str,
    provider: str,
    reasoning: str,
    parent_run_id: str,
    run_id: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    confirmed: bool = False,
) -> dict[str, object]:
    """Answer one batch through a live harness and collect what it wrote."""
    if not confirmed:
        raise DispatchRefused("explicit paid-live confirmation is required at the effect boundary")
    if harness not in HARNESS_PATHS:
        raise ValueError(f"unknown harness: {harness}")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
        raise ValueError("timeout must be an integer from 1 to 3600 seconds")
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    prompt = batch_prompt(items, arm=arm)
    if harness == "omh":
        argv = dispatch_argv(
            omh_executable,
            hermes_executable=hermes_executable,
            workspace=workspace,
            model=model,
            provider=provider,
            reasoning=reasoning,
            parent_run_id=parent_run_id,
            run_id=run_id,
            timeout=timeout,
        )
        completed = subprocess.run(
            _platform_argv(argv),
            input=prompt,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout + 30,
        )
    else:
        usage_file = workspace / ".omh-routing-usage.json"
        argv = current_session_argv(
            hermes_executable,
            model=model,
            provider=provider,
            reasoning=reasoning,
            usage_file=usage_file,
            prompt=prompt,
            workspace=workspace,
        )
        completed = subprocess.run(
            _platform_argv(argv),
            check=False,
            capture_output=True,
            text=True,
            # `--in` alone does not confine this arm: it runs the operator's
            # own authenticated Hermes with the `file` toolset, which resolves
            # relative paths against the process cwd and the inherited
            # TERMINAL_CWD -- the directory `bench.py` was launched from, which
            # for this lane's documented commands is the repo checkout. Both
            # are pinned to the workspace, the way the live-model-tools lane
            # pins them and for the reason its docstring gives.
            cwd=str(workspace),
            env=current_session_environment(workspace),
            timeout=timeout + 30,
        )
    rows, answer_error = read_answer_file(workspace)
    return {
        "schema_version": "routing_question_batch/v1",
        "execution_path": harness,
        "arm": arm,
        "run_id": run_id,
        "item_count": len(items),
        "case_ids": [str(item.get("case_id")) for item in items],
        "exit_code": completed.returncode,
        # `answer_rows` is model-written and is narrowed to the documented
        # answer keys by `read_answer_file`, so a field the model invented does
        # not reach an artifact. `answer_error` names why an answer file is
        # absent and the exit code is the child's; neither carries model text.
        # Prompts, stdout and stderr are not persisted.
        "answer_rows": rows,
        "answer_error": answer_error,
    }


__all__ = [
    "ANSWER_ROW_KEYS",
    "DEFAULT_TIMEOUT_SECONDS",
    "HARNESS_PATHS",
    "MAX_ANSWER_FILE_BYTES",
    "DispatchRefused",
    "answer_row_subset",
    "batch_run_id",
    "current_session_argv",
    "current_session_environment",
    "dispatch_argv",
    "export_argv",
    "read_answer_file",
    "run_batch",
    "score_argv",
]

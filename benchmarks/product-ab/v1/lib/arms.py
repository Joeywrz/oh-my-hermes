"""The three arms.

* ``hermes``       — Hermes alone: one `hermes --oneshot` attempt, bare task.
* ``omh``          — the same model and effort, reached through OMH's coding
  delegation: the route OMH resolves, the calibration that route selects, the
  delegation prompt discipline, and the verification gate.
* ``omh_mixture``  — the same, except the model comes from the category
  mixture OMH's complexity routing resolves, so the cost number stays
  attributable to routing rather than to calibration.

Every arm runs through the same Hermes execution path, so the only differences
between ``hermes`` and ``omh`` are the ones OMH owns.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shlex
import subprocess
from tempfile import TemporaryDirectory
import time
from typing import Any

import lane

#: Both arms carry the identical completion contract, so a completion claim
#: means the same thing on both sides of the comparison.
COMPLETION_CONTRACT = (
    "BENCHMARK COMPLETION CONTRACT:\n"
    f"1. Before stopping, write `{lane.COMPLETION_FILE}` at the workspace root.\n"
    '2. That file must be one JSON object: {"status": "complete"} when you '
    'believe the goal is met, or {"status": "blocked", "reason": "<short '
    'reason>"} when it is not.\n'
    "3. Write it exactly once, as the last thing you do, and put no prose in it.\n"
)

WORKSPACE_PREAMBLE = (
    "You are working in a checkout of the oh-my-hermes repository at an "
    "earlier commit. Make the change the task describes in this checkout. "
    "Do not create a branch, do not commit, and do not push."
)

#: The delegation prompt this lane composes, and what it deliberately leaves
#: out. `UNIT_RESULT_RETURN_PROTOCOL` and the unit-branch commit criterion are
#: fanout *transport*: they exist so a dispatched worktree can be collected and
#: merged. There is no fanout collector here, so including them would make the
#: OMH arm spend tokens on an artifact nothing reads. Everything else in the
#: product's unit prompt is included verbatim from the shipped constants.
PROMPT_PROFILE = "delegation_without_fanout_transport"

#: The one execution path this lane implements, recorded on every record.
#:
#: `omh coding hermes-child dispatch` is the other Hermes execution boundary,
#: and it is deliberately isolated from the caller's profile: it points HOME
#: and HERMES_HOME at throwaway directories and passes only the named
#: provider's documented environment variables. A machine whose models are
#: reached through a subscription login or a gateway registration cannot
#: authenticate through it, which is why every measured run in the sibling
#: lane also uses the profile path. `doctor` refuses a manifest naming any
#: other path, so this field can never claim a path the lane does not run.
EXECUTION_PATH = "hermes_current_session"

ROUTE_TIMEOUT_SECONDS = 120


@dataclass
class Attempt:
    """One Hermes invocation."""

    kind: str
    seconds: float
    usage: dict[str, Any] = field(default_factory=dict)
    ok: bool = False
    failure: dict[str, Any] | None = None


def prompt_protocol() -> Any:
    """The shipped unit prompt protocol, read in process.

    The lane composes the OMH arm's prompt from the product's own constants
    rather than copying their text, so a calibration the repository revises is
    the calibration the next run measures.
    """

    import sys  # noqa: PLC0415

    if str(lane.REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(lane.REPO_ROOT))
    from omh.coding import unit_prompt_protocol  # noqa: PLC0415

    return unit_prompt_protocol


def base_prompt(task_text: str) -> str:
    return f"{WORKSPACE_PREAMBLE}\n\nTASK:\n{task_text.strip()}\n\n{COMPLETION_CONTRACT}"


def fanout_transport_criteria() -> tuple[str, ...]:
    """The criteria the shipped protocol appends whatever the unit says.

    Derived, not copied. `completion_criteria_for_unit` builds one line from
    the unit's file scope, one per integration check, and then appends the
    fanout-transport criterion unconditionally -- so asking it for the
    criteria of an empty unit and dropping the leading file-scope line leaves
    exactly the criteria the unit data cannot influence.

    They have to be dropped here because they are transport: they exist so a
    dispatched worktree can be collected and merged, and this lane has no
    collector. Leaving the commit criterion in put a direct contradiction in
    front of the model, which was told in the same prompt not to commit, and
    falsified `PROMPT_PROFILE`. Deriving the text rather than matching it means
    a rewording upstream stays filtered instead of silently reappearing.
    """

    protocol = prompt_protocol()
    empty = protocol.completion_criteria_for_unit({"boundary": {}, "integration_checks": []})
    return tuple(str(criterion) for criterion in list(empty)[1:])


def delegation_prompt(task_text: str, unit: Mapping[str, Any]) -> str:
    """The OMH arm's prompt, composed from the shipped protocol constants."""

    protocol = prompt_protocol()
    lines = [
        f"Overall goal: {task_text.strip()}",
        protocol.GOAL_ECHO_PROTOCOL,
        protocol.VERIFICATION_STOP_PROTOCOL,
        protocol.FAILURE_KIND_PROTOCOL,
        protocol.STRUCTURAL_SEARCH_DISCIPLINE_GUIDANCE,
        "",
        WORKSPACE_PREAMBLE,
        "",
        "Done means, and only means:",
    ]
    transport = set(fanout_transport_criteria())
    kept = [
        criterion
        for criterion in protocol.completion_criteria_for_unit(unit)
        if str(criterion) not in transport
    ]
    for index, criterion in enumerate(kept, 1):
        lines.append(f"{index}. {criterion}")
    lines.append(protocol.TOOL_BATCHING_PROTOCOL)
    calibration = protocol.calibration_for_route(
        dict(unit["handoff"]["model_route"])
    )
    if calibration:
        lines.append(calibration)
    lines.extend(["", COMPLETION_CONTRACT])
    return "\n".join(lines)


def criterion_for_command(command: str) -> str:
    """A shell command stated as a criterion the model can read literally.

    `completion_criteria_for_unit` capitalizes the first character of each
    integration check, so a bare `python -m compileall -q src` reaches the
    model as `Python -m compileall -q src`. The lane's own gate lowercases the
    interpreter before running it and never noticed; a model copying the line
    verbatim on a case-sensitive filesystem would. Wrapping the command puts a
    word in the position that gets capitalized and leaves the command alone.
    """

    return f"Run `{command.strip()}` and make it pass."


def benchmark_unit(
    *, file_scope: Sequence[str], checks: Sequence[str], route: Mapping[str, Any]
) -> dict[str, Any]:
    """The unit shape the shipped protocol functions read."""

    return {
        "unit_id": "task",
        "title": "benchmark task",
        "role": "implementation",
        "boundary": {"file_scope": list(file_scope), "do_not_touch": []},
        "integration_checks": [criterion_for_command(check) for check in checks],
        "handoff": {"model_route": dict(route)},
    }


def resolve_route(
    *, omh_executable: str, model: str, effort: str, timeout: int = ROUTE_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """`omh coding model-route`: metadata only, never an invocation."""

    completed = subprocess.run(
        [
            omh_executable, "coding", "model-route", "--executor", "hermes",
            "--model", model, "--effort", effort, "--role", "implementation", "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )
    if completed.returncode:
        raise RuntimeError(f"omh coding model-route failed with exit {completed.returncode}")
    route = json.loads(completed.stdout)
    if not isinstance(route, dict):
        raise RuntimeError("omh coding model-route did not return an object")
    return route


def resolve_delegation(
    *, omh_executable: str, task_text: str, timeout: int = ROUTE_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """`omh coding delegate`: the routing decision OMH makes for this task.

    The task text goes in on stdin, never on argv, and only the deterministic
    routing fields are kept. No model is called to produce any of it.
    """

    completed = subprocess.run(
        [omh_executable, "coding", "delegate", "--executor", "hermes", "--stdin"],
        input=task_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )
    if completed.returncode:
        raise RuntimeError(f"omh coding delegate failed with exit {completed.returncode}")
    payload = json.loads(completed.stdout)
    complexity = payload.get("request_complexity") or {}
    recommendation = payload.get("complexity_model_recommendation") or {}
    resolved = recommendation.get("resolved") or {}
    return {
        "tier": complexity.get("tier"),
        "score": complexity.get("score"),
        "signals": [str(signal.get("name")) for signal in complexity.get("signals") or []],
        "model_class": recommendation.get("model_class"),
        "chain_status": recommendation.get("chain_status"),
        "resolved_model": resolved.get("model"),
        "resolved_reasoning_effort": resolved.get("reasoning_effort"),
        "chain_length": len(recommendation.get("chain") or []),
    }


def oneshot_argv(
    *,
    hermes_executable: str,
    workspace: Path,
    provider: str,
    model: str,
    effort: str,
    usage_file: Path,
    prompt: str,
    toolsets: str,
) -> list[str]:
    """`hermes --oneshot` against the active profile.

    The prompt is the argument immediately after `--oneshot`: this CLI does
    not read a prompt from stdin. `--in` pins the working directory, because
    process cwd alone is not enough — Hermes otherwise restores the invoking
    user's home directory.
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
        effort,
        "--toolsets",
        toolsets,
        "--usage-file",
        str(usage_file),
    ]


def _child_environment(workspace: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["TERMINAL_CWD"] = str(workspace)
    return environment


USAGE_KEYS = (
    "estimated_cost_usd", "cost_status", "cost_source", "input_tokens",
    "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "total_tokens", "api_calls", "model", "provider",
    "completed", "failed", "service_tier", "turns", "tool_calls",
)


def _read_usage(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    return {
        key: raw[key]
        for key in USAGE_KEYS
        if key in raw and isinstance(raw[key], (str, int, float, bool))
    }


def run_hermes(
    *,
    hermes_executable: str,
    workspace: Path,
    provider: str,
    model: str,
    effort: str,
    prompt: str,
    toolsets: str,
    timeout: int,
    kind: str,
) -> Attempt:
    """One paid Hermes invocation. The prompt is never persisted."""

    with TemporaryDirectory(prefix="omh-product-ab-usage-") as usage_root:
        usage_file = Path(usage_root) / "usage.json"
        started = time.monotonic()
        try:
            completed = subprocess.run(
                oneshot_argv(
                    hermes_executable=hermes_executable,
                    workspace=workspace,
                    provider=provider,
                    model=model,
                    effort=effort,
                    usage_file=usage_file,
                    prompt=prompt,
                    toolsets=toolsets,
                ),
                cwd=workspace,
                env=_child_environment(workspace),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return Attempt(
                kind=kind,
                seconds=round(time.monotonic() - started, 3),
                failure={"classification": "timeout", "kind": kind},
            )
        seconds = round(time.monotonic() - started, 3)
        usage = _read_usage(usage_file)
    if completed.returncode:
        return Attempt(
            kind=kind,
            seconds=seconds,
            usage=usage,
            failure={
                "classification": _classify(completed.stderr),
                "kind": kind,
                "exit_code": completed.returncode,
            },
        )
    if not usage:
        # The process finished, so whatever it did to the tree stands and the
        # validator is still the ground truth. Only the metrics are missing:
        # the attempt counts as run, the receipt says so, and the cost columns
        # stay unpriced rather than silently becoming zero.
        return Attempt(
            kind=kind,
            seconds=seconds,
            ok=True,
            failure={"classification": "usage_unavailable", "kind": kind},
        )
    return Attempt(kind=kind, seconds=seconds, usage=usage, ok=True)


def _classify(detail: str) -> str:
    normalized = (detail or "").casefold()
    for markers, classification in (
        (("auth", "credential", "api key", "unauthorized", "forbidden"), "authentication_failed"),
        (("rate limit", "too many requests", "429"), "rate_limited"),
        (("session limit", "usage limit", "quota"), "limit_reached"),
        (("model not found", "model unavailable", "unknown model"), "model_unavailable"),
        (("provider",), "provider_error"),
    ):
        if any(marker in normalized for marker in markers):
            return classification
    return "process_crash"


def run_verification(
    *,
    python_executable: str,
    workspace: Path,
    scratch: Path,
    commands: Sequence[str],
    timeout: int,
) -> dict[str, Any]:
    """The OMH arm's verification gate.

    These are the task's stated criteria — a compile gate and the pre-existing
    regression modules for the touched packages. The pull request's own tests
    are never here: they are the hidden validator.
    """

    rows: list[dict[str, Any]] = []
    environment = lane.unittest_environment(workspace, scratch)
    started = time.monotonic()
    for command in commands:
        try:
            argv = shlex.split(command)
        except ValueError:
            rows.append({"command": command, "status": "failed", "classification": "unparseable"})
            continue
        if argv and argv[0] == "python":
            argv = [python_executable, *argv[1:]]
        if not argv:
            rows.append({"command": command, "status": "failed", "classification": "unparseable"})
            continue
        try:
            completed = subprocess.run(
                argv,
                cwd=workspace,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            rows.append({"command": command, "status": "failed", "classification": "not_observed"})
            continue
        rows.append(
            {
                "command": command,
                "status": "passed" if completed.returncode == 0 else "failed",
                "exit_code": completed.returncode,
            }
        )
    failed = [row for row in rows if row["status"] != "passed"]
    return {
        "ran": True,
        "status": "failed" if failed else "passed",
        "checks": rows,
        "failed_count": len(failed),
        # The gate runs a compile pass and up to six unittest modules, which
        # costs minutes. It runs on the OMH arms only, so leaving it out of the
        # wall clock would take time off exactly one side of the comparison and
        # put it on the "faster" headline.
        "seconds": round(time.monotonic() - started, 3),
    }

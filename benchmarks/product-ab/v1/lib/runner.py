"""Doctor, one graded task, and the counterbalanced matrix."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any
import uuid

import arms
import corpus as corpus_lib
import grading
import lane
import repo as repo_lib


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def approximate_cost_usd(model: str, usage: Mapping[str, Any]) -> float | None:
    """List-price cost from OMH's own shipped table.

    The host reports `estimated_cost_usd` for some routes and nothing for
    others (a subscription route reports `included`, a gateway often reports
    `unknown`). Pricing every arm from the same shipped table keeps rows
    comparable; where the host does report a cost, the record carries both.

    The private `_approximate_cost_usd` is called rather than reimplemented
    from `APPROX_PRICE_PER_MTOK`: the cache-read ratio, the service-tier
    multiplier, and the alias projection are part of what a price means here,
    and a second copy of that arithmetic would drift from the shipped one
    without anything failing.
    """

    if str(lane.REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(lane.REPO_ROOT))
    from omh.plugin_bundle.omh.hermes_delegation import (  # noqa: PLC0415
        _approximate_cost_usd,
    )

    def number(name: str) -> float:
        value = usage.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        return float(value)

    return _approximate_cost_usd(
        model, number("input_tokens"), number("output_tokens"), number("cache_read_tokens")
    )


def _usage_total(attempts: Sequence[arms.Attempt], name: str) -> float:
    total = 0.0
    for attempt in attempts:
        value = attempt.usage.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        total += float(value)
    return total


def _reported_cost(attempts: Sequence[arms.Attempt]) -> float | None:
    reported = [
        float(attempt.usage["estimated_cost_usd"])
        for attempt in attempts
        if isinstance(attempt.usage.get("estimated_cost_usd"), (int, float))
        and not isinstance(attempt.usage.get("estimated_cost_usd"), bool)
    ]
    return sum(reported) if reported else None


def _linked_providers() -> list[str]:
    """Provider ids the active Hermes profile is linked to, by OMH's own reader.

    This is the readiness check that catches the expensive mistake: a run whose
    arms cannot reach their provider fails after the first paid attempt rather
    than before any of them.
    """

    if str(lane.REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(lane.REPO_ROOT))
    from omh.plugin_bundle.omh.provider_detection import (  # noqa: PLC0415
        detect_linked_providers,
    )

    return [str(row["id"]) for row in detect_linked_providers()]


def _version(executable: str, flag: str) -> str:
    """The first line a tool reports for itself, so a run names its generation."""

    if shutil.which(executable) is None:
        return "not_on_path"
    try:
        completed = subprocess.run(
            [executable, flag], capture_output=True, text=True, check=False, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    line = (completed.stdout or completed.stderr).strip().splitlines()
    return line[0][:120] if line else "unavailable"


def doctor(
    *,
    manifest: Mapping[str, Any],
    corpus_path: Path,
    repository: Path,
    omh_executable: str,
    hermes_executable: str,
) -> dict[str, Any]:
    """Every readiness check that can be made before a paid token is spent."""

    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    record(
        "manifest_schema",
        manifest.get("schema_version") == lane.MANIFEST_SCHEMA,
        str(manifest.get("schema_version")),
    )
    control = dict(manifest.get("control") or {})
    execution = dict(manifest.get("execution") or {})
    record(
        "manifest_execution_path",
        execution.get("path") == arms.EXECUTION_PATH,
        f"this lane implements {arms.EXECUTION_PATH} only; the manifest says "
        f"{execution.get('path')!r}",
    )
    record(
        "manifest_control_model",
        bool(control.get("model") and control.get("provider") and control.get("effort")),
        "manifest must pin one provider, model, and effort for the control arm",
    )

    try:
        payload = corpus_lib.load(corpus_path)
        tasks = list(payload["tasks"])
        record("corpus_present", True, f"{len(tasks)} tasks")
    except (OSError, ValueError, corpus_lib.CorpusError) as error:
        record("corpus_present", False, str(error)[:200])
        payload, tasks = {}, []

    if not repo_lib.git_ok(repository, "rev-parse", "--git-dir"):
        record("repository", False, "not a git repository")
    else:
        record("repository", True, "")
        if tasks:
            errors = corpus_lib.verify(repository, payload)
            record("corpus_digests", not errors, "; ".join(errors[:3])[:400])
            missing = [
                str(task["task_id"])
                for task in tasks
                if not repo_lib.git_ok(repository, "cat-file", "-e", f"{task['merge_base']}^{{commit}}")
            ]
            record(
                "task_commits_present",
                not missing,
                f"missing merge base for {missing[:5]}" if missing else "",
            )

    for name, executable in (("hermes", hermes_executable), ("omh", omh_executable), ("git", "git")):
        record(f"{name}_on_path", shutil.which(executable) is not None, executable)

    linked = _linked_providers()
    wanted = {str(control.get("provider") or ""), str(control.get("mixture_provider") or "")}
    wanted.discard("")
    missing = sorted(wanted - set(linked))
    record(
        "providers_linked",
        not missing and bool(linked),
        f"missing {missing} (linked: {sorted(linked)})" if missing else f"linked: {sorted(linked)}",
    )

    route_ok, route_detail = False, ""
    if shutil.which(omh_executable):
        try:
            route = arms.resolve_route(
                omh_executable=omh_executable,
                model=str(control.get("model") or ""),
                effort=str(control.get("effort") or ""),
            )
            route_ok = bool(route.get("selected_model"))
            route_detail = f"{route.get('selected_model')} @ {route.get('selected_reasoning_effort')}"
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            route_detail = str(error)[:200]
    record("control_route_resolves", route_ok, route_detail)

    workspace_ok, workspace_detail = False, ""
    if tasks and repo_lib.git_ok(repository, "rev-parse", "--git-dir"):
        try:
            with TemporaryDirectory(prefix="omh-product-ab-doctor-") as root:
                with repo_lib.candidate_workspace(
                    repository, str(tasks[0]["merge_base"]), Path(root), "probe"
                ) as workspace:
                    workspace_ok = (workspace / "src").is_dir()
                    already = grading.validator_is_absent(workspace, tasks[0])
                    if already:
                        workspace_ok = False
                        workspace_detail = f"validator already present: {already[:3]}"
        except (OSError, repo_lib.GitError, ValueError) as error:
            workspace_detail = str(error)[:200]
    record("workspace_creatable", workspace_ok, workspace_detail)

    return {
        "schema_version": lane.DOCTOR_SCHEMA,
        "ok": all(check["ok"] for check in checks),
        "observed_at": _now(),
        "versions": {
            "omh": _version(omh_executable, "--version"),
            "hermes": _version(hermes_executable, "--version"),
        },
        "arms": list(lane.ARMS),
        "prompt_profile": arms.PROMPT_PROFILE,
        "corpus_digest": payload.get("corpus_digest"),
        "task_count": len(tasks),
        "checks": checks,
        "claim_boundary": str(manifest.get("claim_boundary") or ""),
    }


def arm_order(task_index: int, selected: Sequence[str]) -> list[str]:
    """Counterbalanced order: rotate the arms one position per task."""

    ordered = list(selected)
    if not ordered:
        return ordered
    offset = task_index % len(ordered)
    return ordered[offset:] + ordered[:offset]


def _model_for_arm(
    arm: str,
    control: Mapping[str, Any],
    routing: Mapping[str, Any] | None,
) -> dict[str, str]:
    if arm != "omh_mixture" or not routing or not routing.get("resolved_model"):
        return {
            "provider": str(control["provider"]),
            "id": str(control["model"]),
            "effort": str(control["effort"]),
        }
    return {
        "provider": str(control.get("mixture_provider") or control["provider"]),
        "id": str(routing["resolved_model"]),
        "effort": str(routing.get("resolved_reasoning_effort") or control["effort"]),
    }


def execute_one(
    *,
    manifest: Mapping[str, Any],
    task: Mapping[str, Any],
    arm: str,
    corpus_digest: str,
    repository: Path,
    workspace_root: Path,
    output: Path,
    omh_executable: str,
    hermes_executable: str,
    python_executable: str,
    live: bool,
    task_index: int = 0,
) -> dict[str, Any]:
    """Run one arm on one task and grade it. One paid call unless it repairs."""

    if arm not in lane.ARMS:
        raise ValueError(f"unknown arm: {arm}")
    control = dict(manifest["control"])
    timeouts = dict(manifest["timeouts_seconds"])
    run_id = f"product-ab-{uuid.uuid4().hex}"
    task_text = str(task["task_text"])

    routing: dict[str, Any] | None = None
    if arm in {"omh", "omh_mixture"}:
        routing = arms.resolve_delegation(omh_executable=omh_executable, task_text=task_text)
    model = _model_for_arm(arm, control, routing)
    route = arms.resolve_route(
        omh_executable=omh_executable, model=model["id"], effort=model["effort"]
    )

    unit = arms.benchmark_unit(
        file_scope=["src/", "tests/"],
        checks=list(task["verification_commands"]),
        route=route,
    )
    prompt = (
        arms.base_prompt(task_text)
        if arm == "hermes"
        else arms.delegation_prompt(task_text, unit)
    )

    attempts: list[arms.Attempt] = []
    verification: dict[str, Any] = {"ran": False, "status": "not_run", "checks": []}
    with TemporaryDirectory(prefix="omh-product-ab-scratch-") as scratch_text:
        scratch = Path(scratch_text)
        with repo_lib.candidate_workspace(
            repository, str(task["merge_base"]), workspace_root, f"{task['task_id']}-{arm}"
        ) as workspace:
            leaked = grading.validator_is_absent(workspace, task)
            if leaked:
                raise ValueError(
                    f"{task['task_id']}: the validator is present in the candidate workspace: {leaked}"
                )
            if live:
                attempts.append(
                    arms.run_hermes(
                        hermes_executable=hermes_executable,
                        workspace=workspace,
                        provider=model["provider"],
                        model=model["id"],
                        effort=model["effort"],
                        prompt=prompt,
                        toolsets=str(manifest["execution"]["toolsets"]),
                        timeout=int(timeouts["task"]),
                        kind="primary",
                    )
                )
                if arm in {"omh", "omh_mixture"} and attempts[-1].ok:
                    verification = arms.run_verification(
                        python_executable=python_executable,
                        workspace=workspace,
                        scratch=scratch,
                        commands=list(task["verification_commands"]),
                        timeout=int(timeouts["verification"]),
                    )
                    repairs = int(manifest["execution"].get("omh_repair_attempts", 0))
                    if verification["status"] == "failed" and repairs:
                        attempts.append(
                            arms.run_hermes(
                                hermes_executable=hermes_executable,
                                workspace=workspace,
                                provider=model["provider"],
                                model=model["id"],
                                effort=model["effort"],
                                prompt=_repair_prompt(prompt, verification),
                                toolsets=str(manifest["execution"]["toolsets"]),
                                timeout=int(timeouts["task"]),
                                kind="repair",
                            )
                        )
                        verification = arms.run_verification(
                            python_executable=python_executable,
                            workspace=workspace,
                            scratch=scratch,
                            commands=list(task["verification_commands"]),
                            timeout=int(timeouts["verification"]),
                        )
            claim = grading.completion_claim(workspace)
            if (
                arm in {"omh", "omh_mixture"}
                and verification["ran"]
                and verification["status"] == "failed"
                and claim["claim"] == "complete"
            ):
                # The verification gate is what withdraws a completion claim the
                # declared checks contradict. That is the behaviour this arm is
                # here to measure, so it is recorded, never silently applied.
                claim = {"claim": "blocked", "reason": "withdrawn_by_verification_gate"}
            grading.materialize_validator(repository, task, workspace)
            target = grading.run_modules(
                python_executable=python_executable,
                workspace=workspace,
                scratch=scratch,
                modules=list(task["test_modules"]),
                timeout=int(timeouts["validator"]),
            )
            regression = grading.run_modules(
                python_executable=python_executable,
                workspace=workspace,
                scratch=scratch,
                modules=list(task["regression_modules"]),
                timeout=int(timeouts["validator"]),
            )
            final_digest = lane.tree_digest(workspace) if manifest.get("digest_final_tree") else None

    grade = grading.grade(
        target=target,
        regression=regression,
        claim=claim,
        run_failed=live and not any(attempt.ok for attempt in attempts),
    )
    record = {
        "schema_version": lane.RUN_SCHEMA,
        "created_at": _now(),
        "run_id": run_id,
        "task_id": str(task["task_id"]),
        "pull_request": int(task["pull_request"]),
        "arm": arm,
        "task_index": task_index,
        "live": bool(live),
        "corpus_digest": corpus_digest,
        "task_digest": str(task["task_text_sha256"]),
        "prompt_profile": arms.PROMPT_PROFILE if arm != "hermes" else "bare_task",
        "execution_path": str(manifest["execution"]["path"]),
        "model": model,
        "route": {
            "selected_model": route.get("selected_model"),
            "selected_reasoning_effort": route.get("selected_reasoning_effort"),
            "model_family": route.get("model_family"),
        },
        "routing": routing,
        "attempts": [
            {"kind": attempt.kind, "seconds": attempt.seconds, "ok": attempt.ok}
            for attempt in attempts
        ],
        "wall_clock_seconds": round(sum(attempt.seconds for attempt in attempts), 3),
        "usage": {
            "total_tokens": _usage_total(attempts, "total_tokens"),
            "input_tokens": _usage_total(attempts, "input_tokens"),
            "output_tokens": _usage_total(attempts, "output_tokens"),
            "cache_read_tokens": _usage_total(attempts, "cache_read_tokens"),
            "turns": _usage_total(attempts, "turns"),
            "tool_calls": _usage_total(attempts, "tool_calls"),
        },
        "cost": {
            "reported_usd": _reported_cost(attempts),
            "list_price_usd": approximate_cost_usd(
                model["id"],
                {
                    "input_tokens": _usage_total(attempts, "input_tokens"),
                    "output_tokens": _usage_total(attempts, "output_tokens"),
                    "cache_read_tokens": _usage_total(attempts, "cache_read_tokens"),
                },
            ),
        },
        "verification_gate": verification,
        "grade": grade,
        "failure_receipt": next(
            (attempt.failure for attempt in attempts if attempt.failure), None
        ),
    }
    if final_digest:
        record["final_tree_digest"] = final_digest
    if not lane.artifact_is_safe(record):
        raise ValueError("benchmark record failed metadata-only artifact safety")
    lane.append_jsonl(output, record)
    return record


def _repair_prompt(original: str, verification: Mapping[str, Any]) -> str:
    failed = [
        str(row["command"])
        for row in verification.get("checks") or []
        if row.get("status") != "passed"
    ]
    return (
        f"{original}\n\nVERIFICATION GATE: the declared checks did not pass. "
        f"Failing checks: {'; '.join(failed)}. Fix the cause, re-run them, and "
        "write the completion file again."
    )


def run_matrix(
    *,
    manifest: Mapping[str, Any],
    payload: Mapping[str, Any],
    selected_arms: Sequence[str],
    repository: Path,
    workspace_root: Path,
    output: Path,
    omh_executable: str,
    hermes_executable: str,
    python_executable: str,
    live: bool,
    max_paid_calls: int,
    task_limit: int | None = None,
) -> dict[str, Any]:
    """Every selected arm on every task, one task in flight at a time."""

    tasks = list(payload["tasks"])
    if task_limit is not None:
        tasks = tasks[:task_limit]
    scheduled = len(tasks) * len(selected_arms)
    if live and (max_paid_calls < 1 or scheduled > max_paid_calls):
        raise ValueError(
            f"scheduled paid calls ({scheduled}) exceed the explicit budget ({max_paid_calls})"
        )
    records: list[dict[str, Any]] = []
    for index, task in enumerate(tasks):
        for arm in arm_order(index, selected_arms):
            records.append(
                execute_one(
                    manifest=manifest,
                    task=task,
                    arm=arm,
                    corpus_digest=str(payload["corpus_digest"]),
                    repository=repository,
                    workspace_root=workspace_root,
                    output=output,
                    omh_executable=omh_executable,
                    hermes_executable=hermes_executable,
                    python_executable=python_executable,
                    live=live,
                    task_index=index,
                )
            )
    failures = [record for record in records if record["failure_receipt"]]
    return {
        "schema_version": lane.RECEIPT_SCHEMA,
        "observed_at": _now(),
        "corpus_digest": str(payload["corpus_digest"]),
        "arms": list(selected_arms),
        "tasks": len(tasks),
        "scheduled": scheduled,
        "graded": len(records),
        "passed": sum(bool(record["grade"]["pass"]) for record in records),
        "failed_runs": len(failures),
        "false_completions": sum(
            bool(record["grade"]["false_completion"]) for record in records
        ),
        "paid_calls_launched": sum(len(record["attempts"]) for record in records) if live else 0,
        "output": output.name,
        "ok": not failures,
    }

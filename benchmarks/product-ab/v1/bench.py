#!/usr/bin/env python3
"""Hermes alone versus Hermes through OMH, on this repository's merged PRs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "lib"))

import corpus as corpus_lib  # noqa: E402
import lane  # noqa: E402
from runner import (  # noqa: E402
    doctor,
    execute_one,
    run_matrix,
    worst_case_paid_calls,
)

DEFAULT_CORPUS = BASE / "corpus" / "evaluation.json"
DEFAULT_MANIFEST = BASE / "manifest.json"


def emit(value: object) -> None:
    print(json.dumps(value, sort_keys=True, indent=2))


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--repository", type=Path, default=lane.REPO_ROOT)
    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="Directory the candidate worktrees are created under "
        "(default: a sibling of the repository).",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--arm", action="append", choices=lane.ARMS)
    parser.add_argument("--task", action="append", help="Run only these task ids.")
    parser.add_argument("--task-limit", type=int)
    parser.add_argument("--omh-executable", default="omh")
    parser.add_argument("--hermes-executable", default="hermes")
    parser.add_argument("--python-executable", default=sys.executable)
    parser.add_argument(
        "--allow-paid-live",
        action="store_true",
        help="Actually call the model. Without it the harness runs the whole "
        "pipeline with no Hermes invocation at all.",
    )
    parser.add_argument("--max-paid-calls", type=int, default=0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor_parser = sub.add_parser("doctor", help="Readiness, before a paid token is spent.")
    doctor_parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    doctor_parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    doctor_parser.add_argument("--repository", type=Path, default=lane.REPO_ROOT)
    doctor_parser.add_argument("--omh-executable", default="omh")
    doctor_parser.add_argument("--hermes-executable", default="hermes")

    corpus_parser = sub.add_parser("corpus", help="Build or verify the pinned corpus.")
    corpus_parser.add_argument("--repository", type=Path, default=lane.REPO_ROOT)
    corpus_parser.add_argument("--repository-name", default="rlaope/oh-my-hermes")
    corpus_parser.add_argument("--output", type=Path, default=DEFAULT_CORPUS)
    corpus_parser.add_argument(
        "--pull-request-limit",
        type=int,
        default=800,
        help="How many merged pull requests to read, newest first. The "
        "issue-sourced count stops growing at 800 on this repository; "
        "reading further costs gh calls and adds no task.",
    )
    corpus_parser.add_argument("--max-tasks", type=int)
    corpus_parser.add_argument("--build", action="store_true", help="Read GitHub and rewrite the corpus.")
    corpus_parser.add_argument("--verify", action="store_true", help="Re-derive every pinned digest.")
    corpus_parser.add_argument(
        "--probe",
        action="store_true",
        help="Prove offline that every task is red at its merge base and its "
        "regression set is green there; drop the ones that are not.",
    )
    corpus_parser.add_argument("--python-executable", default=sys.executable)
    corpus_parser.add_argument("--workspace-root", type=Path)
    corpus_parser.add_argument("--probe-timeout", type=int, default=1200)
    corpus_parser.add_argument(
        "--strict-environment",
        action="store_true",
        help="Make --verify fail on an interpreter difference as well as on a "
        "drifted digest. Off by default so CI can verify digests under any "
        "matrix Python; --probe refuses a different interpreter regardless.",
    )
    corpus_parser.add_argument(
        "--allow-foreign-interpreter",
        action="store_true",
        help="Re-probe under an interpreter other than the one that probed "
        "this corpus. The result is a different corpus; the flag exists so "
        "that is a decision rather than an accident.",
    )

    for name in ("smoke", "run"):
        command = sub.add_parser(name)
        _add_run_arguments(command)

    args = parser.parse_args(argv)

    if args.command == "doctor":
        result = doctor(
            manifest=lane.load_object(args.manifest),
            corpus_path=args.corpus,
            repository=args.repository.resolve(),
            omh_executable=args.omh_executable,
            hermes_executable=args.hermes_executable,
        )
        emit(result)
        return 0 if result["ok"] else 1

    if args.command == "corpus":
        if sum((args.build, args.verify, args.probe)) != 1:
            parser.error("choose exactly one of --build, --probe, or --verify")
        if args.probe:
            existing = corpus_lib.load(args.output)
            # A refusal, not a note. Re-probing under a different interpreter
            # produces a different corpus -- PR-1502 classifies as
            # path-dependent under 3.13.15 and already-green under 3.14.7 --
            # so it stops here unless someone says they meant it.
            drift = corpus_lib.environment_drift(existing)
            if drift and not args.allow_foreign_interpreter:
                parser.error(
                    "re-probing under a different interpreter produces a "
                    "different corpus: " + "; ".join(drift)
                    + " -- pass --allow-foreign-interpreter to mean it"
                )
            payload = corpus_lib.probe(
                repository=args.repository.resolve(),
                payload=corpus_lib.load(args.output),
                python_executable=args.python_executable,
                workspace_root=(
                    args.workspace_root
                    or args.repository.resolve().parent / "product-ab-workspaces"
                ),
                timeout=args.probe_timeout,
                maximum_tasks=args.max_tasks,
            )
            lane.write_json(args.output, payload)
            emit(
                {
                    "schema_version": lane.CORPUS_SCHEMA,
                    "ok": bool(payload["tasks"]),
                    "tasks": len(payload["tasks"]),
                    "corpus_digest": payload["corpus_digest"],
                    "probe_rejected": payload["selection"]["probe_rejected"],
                    "output": str(args.output.name),
                }
            )
            return 0 if payload["tasks"] else 1
        if args.build:
            payload = corpus_lib.build(
                repository=args.repository.resolve(),
                repository_name=args.repository_name,
                limit=args.pull_request_limit,
                maximum_tasks=args.max_tasks,
            )
            lane.write_json(args.output, payload)
            emit(
                {
                    "schema_version": lane.CORPUS_SCHEMA,
                    "ok": bool(payload["tasks"]),
                    "tasks": len(payload["tasks"]),
                    "corpus_digest": payload["corpus_digest"],
                    "excluded": payload["selection"]["excluded"],
                    "output": str(args.output.name),
                }
            )
            return 0 if payload["tasks"] else 1
        payload = corpus_lib.load(args.output)
        errors = corpus_lib.verify(args.repository.resolve(), payload)
        # Reported, not enforced. Digests are over bytes from the git object
        # store and no interpreter appears in them, so verify has to pass on
        # 3.11, 3.12 and anywhere else -- a digest check that runs under only
        # one interpreter is one that does not run in CI, which is where it is
        # wanted most. The refusal belongs on the probe path, where a different
        # interpreter genuinely produces a different corpus.
        drift = corpus_lib.environment_drift(payload)
        strict = bool(getattr(args, "strict_environment", False))
        emit(
            {
                "schema_version": lane.CORPUS_SCHEMA,
                "ok": not errors and not (strict and drift),
                "tasks": len(payload["tasks"]),
                "corpus_digest": payload["corpus_digest"],
                "errors": errors,
                "environment_drift": drift,
                "environment_drift_enforced": strict,
            }
        )
        return 0 if not errors and not (strict and drift) else 1

    manifest = lane.load_object(args.manifest)
    payload = corpus_lib.load(args.corpus)
    selected = list(args.arm or lane.ARMS)
    if args.task:
        wanted = set(args.task)
        payload = dict(payload)
        payload["tasks"] = [
            task for task in payload["tasks"] if str(task["task_id"]) in wanted
        ]
        if not payload["tasks"]:
            parser.error("no corpus task matched --task")
    live = bool(args.allow_paid_live)
    if live and args.max_paid_calls < 1:
        parser.error("--allow-paid-live requires --max-paid-calls")

    output = args.output or BASE / "artifacts" / "runs.jsonl"
    workspace_root = args.workspace_root or (
        args.repository.resolve().parent / "product-ab-workspaces"
    )

    if args.command == "smoke":
        task = payload["tasks"][0]
        # The same budget `run` enforces. `smoke` calls `execute_one` directly
        # instead of going through `run_matrix`, so without this the flag is
        # accepted, echoed back, and ignored: one task over three arms spends
        # three calls, and five once the repair turns fire, under
        # `--max-paid-calls 1`.
        worst_case = worst_case_paid_calls(manifest, 1, selected)
        if live and worst_case > args.max_paid_calls:
            parser.error(
                f"paid calls in the worst case ({worst_case}) exceed the "
                f"explicit budget ({args.max_paid_calls})"
            )
        records = [
            execute_one(
                manifest=manifest,
                task=task,
                arm=arm,
                corpus_digest=str(payload["corpus_digest"]),
                repository=args.repository.resolve(),
                workspace_root=workspace_root,
                output=output,
                omh_executable=args.omh_executable,
                hermes_executable=args.hermes_executable,
                python_executable=args.python_executable,
                live=live,
            )
            for arm in selected
        ]
        result = {
            "schema_version": lane.RECEIPT_SCHEMA,
            "ok": all(not record["failure_receipt"] for record in records),
            "task_id": str(task["task_id"]),
            "arms": selected,
            "graded": len(records),
            "passed": sum(bool(record["grade"]["pass"]) for record in records),
            "paid_calls_launched": sum(len(record["attempts"]) for record in records) if live else 0,
            "output": output.name,
        }
        emit(result)
        return 0 if result["ok"] else 1

    result = run_matrix(
        manifest=manifest,
        payload=payload,
        selected_arms=selected,
        repository=args.repository.resolve(),
        workspace_root=workspace_root,
        output=output,
        omh_executable=args.omh_executable,
        hermes_executable=args.hermes_executable,
        python_executable=args.python_executable,
        live=live,
        max_paid_calls=args.max_paid_calls,
        task_limit=args.task_limit,
    )
    emit(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

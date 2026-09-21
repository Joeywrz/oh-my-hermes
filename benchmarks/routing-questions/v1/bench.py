#!/usr/bin/env python3
"""Offline-first lane: answer OMH's own routing corpora as typed questions.

Every subcommand but `run` is offline and free. `run` spends money, needs
`--allow-paid-live`, `--max-paid-calls`, and `--confirm`, and refuses without
all three.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "lib"))

from external_answers import RESERVED_ARM, validate_answer_file, write_jsonl  # noqa: E402
from harness import batch_run_id, export_argv, run_batch, score_argv  # noqa: E402
from prompts import split_batches  # noqa: E402

# A corpus is a file this command is pointed at. It is bounded before it is
# read whole, so a mistyped `--corpus` path is a named refusal rather than an
# out-of-memory kill.
MAX_CORPUS_BYTES = 64 * 1024 * 1024


def emit(value: object) -> None:
    print(json.dumps(value, sort_keys=True, indent=2))


def _run(argv: list[str]) -> int:
    completed = subprocess.run(argv, check=False)
    return completed.returncode


def _load_corpus(path: Path) -> dict:
    target = Path(path)
    try:
        with target.open("rb") as handle:
            raw = handle.read(MAX_CORPUS_BYTES + 1)
    except OSError as exc:
        raise SystemExit(f"corpus is not readable: {exc}")
    if len(raw) > MAX_CORPUS_BYTES:
        raise SystemExit(f"corpus exceeds the {MAX_CORPUS_BYTES}-byte cap: {target}")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"corpus is not readable JSON: {exc}")
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise SystemExit(f"not a routing question corpus: {target}")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Answer the shipped OMH routing corpora as typed route questions"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="Write the corpus through the omh CLI.")
    export.add_argument("--omh-executable", default="omh")
    export.add_argument("--source", default="discord")
    export.add_argument("--limit", type=int, default=3)
    export.add_argument("--output", type=Path, default=BASE / "artifacts" / "corpus.json")

    deterministic = sub.add_parser("deterministic", help="Score the deterministic arm alone.")
    deterministic.add_argument("--omh-executable", default="omh")
    deterministic.add_argument("--corpus", type=Path, default=BASE / "artifacts" / "corpus.json")
    deterministic.add_argument("--output", type=Path, default=None)

    score = sub.add_parser("score", help="Score an answer file or directory against a corpus.")
    score.add_argument("--omh-executable", default="omh")
    score.add_argument("--corpus", type=Path, default=BASE / "artifacts" / "corpus.json")
    score.add_argument("--answers", type=Path, required=True)
    score.add_argument("--output", type=Path, default=None)
    score.add_argument("--preflight", action="store_true", help="Check the answer file offline and stop.")

    run = sub.add_parser("run", help="Answer the corpus with a live model arm. Spends money.")
    run.add_argument("--harness", choices=("omh", "hermes_current_session"), default="omh")
    run.add_argument("--corpus", type=Path, default=BASE / "artifacts" / "corpus.json")
    run.add_argument("--arm", required=True, help="Name this arm carries in the score report.")
    run.add_argument("--model", required=True)
    run.add_argument("--provider", required=True)
    run.add_argument("--reasoning", required=True)
    run.add_argument("--omh-executable", default="omh")
    run.add_argument("--hermes-executable", default="hermes")
    run.add_argument("--batch-size", type=int, default=20)
    run.add_argument("--timeout", type=int, default=900)
    run.add_argument("--parent-run-id", default="routing-questions")
    run.add_argument("--workspace", type=Path, default=BASE / "artifacts" / "workspace")
    run.add_argument("--output", type=Path, default=BASE / "artifacts" / "answers.jsonl")
    run.add_argument("--allow-paid-live", action="store_true")
    run.add_argument("--max-paid-calls", type=int, default=0)
    run.add_argument("--confirm", action="store_true", help="Confirm the paid effect boundary.")

    args = parser.parse_args(argv)

    if args.command == "export":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        return _run(export_argv(
            args.omh_executable,
            source=args.source,
            limit=args.limit,
            output=args.output,
        ))

    if args.command == "deterministic":
        return _run(score_argv(args.omh_executable, corpus=args.corpus, output=args.output))

    if args.command == "score":
        if args.preflight:
            emit(validate_answer_file(args.answers))
            return 0
        return _run(score_argv(
            args.omh_executable,
            corpus=args.corpus,
            answers=args.answers,
            output=args.output,
        ))

    if not args.allow_paid_live:
        parser.error("a live model arm requires --allow-paid-live")
    if args.max_paid_calls < 1:
        parser.error("a live model arm requires --max-paid-calls")
    if not args.confirm:
        parser.error("a live model arm requires --confirm at the effect boundary")
    if args.arm.strip() == RESERVED_ARM:
        parser.error(
            f"--arm {RESERVED_ARM} is reserved: every report computes that arm from the corpus itself"
        )

    corpus = _load_corpus(args.corpus)
    batches = split_batches(corpus["items"], args.batch_size)
    if len(batches) > args.max_paid_calls:
        parser.error(
            f"{len(batches)} batches exceed --max-paid-calls {args.max_paid_calls}; "
            "raise the cap or the batch size"
        )
    receipts: list[dict] = []
    # Truncate once, then append each batch as it arrives. A batch can raise --
    # a prompt over the byte budget, a child that overran its wall, an
    # interrupt -- and a run that held every row in memory until the end would
    # discard every answer it had already paid for. Truncating up front is part
    # of the same property: a partial run must not be scored together with the
    # rows a previous run left behind.
    written = write_jsonl([], args.output)
    for index, batch in enumerate(batches, start=1):
        run_id = batch_run_id(args.parent_run_id, index)
        receipt = run_batch(
            batch,
            harness=args.harness,
            omh_executable=args.omh_executable,
            hermes_executable=args.hermes_executable,
            workspace=args.workspace / run_id,
            arm=args.arm,
            model=args.model,
            provider=args.provider,
            reasoning=args.reasoning,
            parent_run_id=args.parent_run_id,
            run_id=run_id,
            timeout=args.timeout,
            confirmed=True,
        )
        written += write_jsonl(receipt.pop("answer_rows"), args.output, append=True)
        receipts.append(receipt)
    failed = [receipt for receipt in receipts if receipt["exit_code"] or receipt["answer_error"]]
    emit({
        "schema_version": "routing_question_run/v1",
        "arm": args.arm,
        "execution_path": args.harness,
        "batches": len(batches),
        "answer_rows": written,
        "failed_batches": len(failed),
        "receipts": receipts,
        "answers": str(args.output),
        "claim_boundary": (
            "Answer rows are what this arm wrote; a batch with no answer file answered nothing "
            "and is scored as unanswered, never as correct. Rows are written as each batch "
            "returns, so a run that stopped early leaves behind the answers it did buy and "
            "those cases alone. Score them with `omh chat route-questions score` before "
            "reporting any number."
        ),
    })
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

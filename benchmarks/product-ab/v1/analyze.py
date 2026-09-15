#!/usr/bin/env python3
"""Turn a run file into the four quotable numbers and the paired deltas."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "lib"))

import corpus as corpus_lib  # noqa: E402
import lane  # noqa: E402
from report import (  # noqa: E402
    analyze,
    render_deltas,
    render_table,
    subset_task_ids,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=BASE / "manifest.json")
    parser.add_argument("--baseline-arm", choices=lane.ARMS, default="hermes")
    parser.add_argument("--bootstrap-repetitions", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20260914)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--table", action="store_true", help="Print the Markdown table instead of JSON.")
    parser.add_argument(
        "--corpus",
        type=Path,
        default=BASE / "corpus" / "evaluation.json",
        help="The corpus the subset flags below are read from.",
    )
    parser.add_argument(
        "--task-source",
        choices=("linked_issue", "pull_request_body"),
        help="Report on the tasks whose text came from this source only. "
        "`linked_issue` is the subset a sentence about solving this "
        "repository's own issues may be written from.",
    )
    parser.add_argument(
        "--leak-class",
        action="append",
        choices=corpus_lib.LEAK_CLASSES,
        help="Report on tasks in these leak classes only; repeatable.",
    )
    args = parser.parse_args(argv)
    if args.bootstrap_repetitions < 100:
        parser.error("bootstrap repetitions must be at least 100")

    only, label = None, "all tasks"
    if args.task_source or args.leak_class:
        payload = corpus_lib.load(args.corpus)
        only = sorted(
            subset_task_ids(
                payload,
                task_source=args.task_source,
                leak_classes=args.leak_class,
            )
        )
        parts = []
        if args.task_source:
            parts.append(f"task_source={args.task_source}")
        if args.leak_class:
            parts.append("leak_class in " + ",".join(sorted(args.leak_class)))
        label = "; ".join(parts)

    report = analyze(
        records_path=args.records,
        manifest=lane.load_object(args.manifest),
        baseline_arm=args.baseline_arm,
        repetitions=args.bootstrap_repetitions,
        seed=args.seed,
        only_task_ids=only,
        subset_label=label,
    )
    if args.output:
        lane.write_json(args.output, report)
    if args.table:
        print(f"Subset: {report['subset']} ({report['subset_task_count']} tasks)")
        print()
        print(render_table(report))
        print()
        print(render_deltas(report))
    else:
        print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

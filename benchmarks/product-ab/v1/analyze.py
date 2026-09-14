#!/usr/bin/env python3
"""Turn a run file into the four quotable numbers and the paired deltas."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "lib"))

import lane  # noqa: E402
from report import analyze, render_deltas, render_table  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=BASE / "manifest.json")
    parser.add_argument("--baseline-arm", choices=lane.ARMS, default="hermes")
    parser.add_argument("--bootstrap-repetitions", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20260914)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--table", action="store_true", help="Print the Markdown table instead of JSON.")
    args = parser.parse_args(argv)
    if args.bootstrap_repetitions < 100:
        parser.error("bootstrap repetitions must be at least 100")

    report = analyze(
        records_path=args.records,
        manifest=lane.load_object(args.manifest),
        baseline_arm=args.baseline_arm,
        repetitions=args.bootstrap_repetitions,
        seed=args.seed,
    )
    if args.output:
        lane.write_json(args.output, report)
    if args.table:
        print(render_table(report))
        print()
        print(render_deltas(report))
    else:
        print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

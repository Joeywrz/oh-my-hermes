"""The four numbers, paired per task, with the pairing statistics from v1.

`percentile`, `exact_mcnemar`, and `holm` are imported from
`benchmarks/live-model-tools/v1/lib/statistics.py`, so both lanes decide
significance the same way and neither can drift from the other.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import random
from typing import Any

import lane

#: A delta whose CI95 spans zero is reported under this name, never rounded
#: into a direction it does not have.
NO_MEASURABLE_DIFFERENCE = "no measurable difference"


def read_records(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict) or value.get("schema_version") != lane.RUN_SCHEMA:
            raise ValueError(f"invalid run record at line {number}")
        rows.append(value)
    return rows


def index_by_arm(records: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Mapping[str, Any]]]:
    indexed: dict[str, dict[str, Mapping[str, Any]]] = {}
    for record in records:
        arm = str(record["arm"])
        task_id = str(record["task_id"])
        bucket = indexed.setdefault(arm, {})
        if task_id in bucket:
            raise ValueError(f"duplicate run record: {arm}/{task_id}")
        bucket[task_id] = record
    return indexed


def priced(record: Mapping[str, Any]) -> float | None:
    """This run's cost, or ``None`` when nothing priced it.

    A run nobody could price is not a free run. Returning ``None`` keeps it
    out of the sum and lets the summary say how many runs were unpriced, so a
    cost-per-pass figure is never quietly built on a zero that means
    "unknown".
    """

    cost = dict(record.get("cost") or {})
    for key in ("list_price_usd", "reported_usd"):
        value = cost.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _cost(record: Mapping[str, Any]) -> float:
    value = priced(record)
    return 0.0 if value is None else value


def _metric(record: Mapping[str, Any], name: str) -> float:
    if name == "pass":
        return 1.0 if record["grade"]["pass"] else 0.0
    if name == "cost":
        return _cost(record)
    if name == "seconds":
        value = record.get("wall_clock_seconds")
        return float(value) if isinstance(value, (int, float)) else 0.0
    value = dict(record.get("usage") or {}).get(name)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def arm_summary(records: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The four quotable numbers for one arm, plus the behaviour columns."""

    rows = [records[task_id] for task_id in sorted(records)]
    total = len(rows)
    passes = sum(1 for row in rows if row["grade"]["pass"])
    cost_total = sum(_cost(row) for row in rows)
    seconds = [_metric(row, "seconds") for row in rows]
    reported = [
        float(row["cost"]["reported_usd"])
        for row in rows
        if isinstance((row.get("cost") or {}).get("reported_usd"), (int, float))
        and not isinstance((row.get("cost") or {}).get("reported_usd"), bool)
    ]
    return {
        "tasks": total,
        "passed": passes,
        "pass_rate": passes / total if total else None,
        "tokens_total": sum(_metric(row, "total_tokens") for row in rows),
        "cost_usd_total": round(cost_total, 6),
        "cost_usd_per_pass": round(cost_total / passes, 6) if passes else None,
        "runs_unpriced": sum(1 for row in rows if priced(row) is None),
        "cost_source": "list_price" if len(reported) < total else "host_reported",
        "host_reported_cost_usd": round(sum(reported), 6) if reported else None,
        "seconds_total": round(sum(seconds), 3),
        "seconds_median": round(lane.percentile(seconds, 0.5), 3) if seconds else None,
        "seconds_mean": round(sum(seconds) / total, 3) if total else None,
        "false_completions": sum(1 for row in rows if row["grade"]["false_completion"]),
        "false_completion_rate": (
            sum(1 for row in rows if row["grade"]["false_completion"]) / total if total else None
        ),
        "tool_calls": sum(_metric(row, "tool_calls") for row in rows),
        "api_turns": sum(_metric(row, "turns") for row in rows),
        "runs_failed": sum(1 for row in rows if row.get("failure_receipt")),
        "grade_reasons": _counts(str(row["grade"]["reason"]) for row in rows),
        "verification_gate": _counts(
            str((row.get("verification_gate") or {}).get("status", "not_run")) for row in rows
        ),
    }


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def paired_delta(
    baseline: Sequence[Mapping[str, Any]],
    treatment: Sequence[Mapping[str, Any]],
    metric: str,
    repetitions: int,
    seed: int,
) -> dict[str, Any]:
    """Mean paired delta with a bootstrap CI95 over the task pairs."""

    pairs = list(zip(baseline, treatment, strict=True))
    deltas = [_metric(after, metric) - _metric(before, metric) for before, after in pairs]
    mean = sum(deltas) / len(deltas) if deltas else 0.0
    rng = random.Random(seed)
    samples = []
    for _ in range(repetitions):
        resampled = [rng.choice(deltas) for _ in deltas]
        samples.append(sum(resampled) / len(resampled))
    low = lane.percentile(samples, 0.025)
    high = lane.percentile(samples, 0.975)
    return {
        "metric": metric,
        "mean_delta": round(mean, 6),
        "ci95": [round(low, 6), round(high, 6)],
        "crosses_zero": bool(low <= 0.0 <= high),
        "treatment_greater": sum(1 for delta in deltas if delta > 0),
        "n": len(deltas),
    }


def analyze(
    *,
    records_path: Path,
    manifest: Mapping[str, Any],
    baseline_arm: str = "hermes",
    repetitions: int = 10_000,
    seed: int = 20260914,
) -> dict[str, Any]:
    records = read_records(records_path)
    indexed = index_by_arm(records)
    if baseline_arm not in indexed:
        raise ValueError(f"records carry no {baseline_arm} arm")
    corpus_digests = {str(record["corpus_digest"]) for record in records}
    if len(corpus_digests) != 1:
        raise ValueError("records mix corpora; a comparison needs one pinned corpus")

    summaries = {arm: arm_summary(rows) for arm, rows in sorted(indexed.items())}
    baseline_rows = indexed[baseline_arm]
    comparisons: dict[str, Any] = {}
    pvalues: dict[str, float] = {}
    for arm, rows in sorted(indexed.items()):
        if arm == baseline_arm:
            continue
        shared = sorted(set(baseline_rows) & set(rows))
        if not shared:
            continue
        before = [baseline_rows[task_id] for task_id in shared]
        after = [rows[task_id] for task_id in shared]
        mcnemar = lane.exact_mcnemar(
            [{"grade": {"pass": row["grade"]["pass"]}} for row in before],
            [{"grade": {"pass": row["grade"]["pass"]}} for row in after],
        )
        pvalues[arm] = float(mcnemar["exact_two_sided_p"])
        comparisons[arm] = {
            "baseline": baseline_arm,
            "paired_tasks": len(shared),
            "unpaired_tasks": sorted(set(baseline_rows) ^ set(rows)),
            "mcnemar": mcnemar,
            "deltas": {
                metric: paired_delta(before, after, metric, repetitions, seed)
                for metric in ("pass", "cost", "seconds", "total_tokens")
            },
        }
    return {
        "schema_version": lane.REPORT_SCHEMA,
        "analysis_seed": seed,
        "bootstrap_repetitions": repetitions,
        "corpus_digest": corpus_digests.pop(),
        "baseline_arm": baseline_arm,
        "arms": summaries,
        "comparisons": comparisons,
        "holm": lane.holm(pvalues) if pvalues else {},
        "claim_boundary": str(manifest.get("claim_boundary") or ""),
    }


def render_table(report: Mapping[str, Any]) -> str:
    """The quotable table, rendered from the report and nothing else."""

    header = (
        "| Arm | Passed | Pass rate | Tokens | Cost | Cost / pass | "
        "Median s / task | False completions | Tool calls | API turns |"
    )
    divider = "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    lines = [header, divider]
    for arm, summary in dict(report["arms"]).items():
        lines.append(
            "| {arm} | {passed} / {tasks} | {rate} | {tokens:,} | ${cost:.4f} | "
            "{per_pass} | {median} | {false_count} | {tools:,} | {turns:,} |".format(
                arm=arm,
                passed=summary["passed"],
                tasks=summary["tasks"],
                rate=_percent(summary["pass_rate"]),
                tokens=int(summary["tokens_total"]),
                cost=summary["cost_usd_total"],
                per_pass=(
                    f"${summary['cost_usd_per_pass']:.4f}"
                    if summary["cost_usd_per_pass"] is not None
                    else "n/a"
                ),
                median=summary["seconds_median"] if summary["seconds_median"] is not None else "n/a",
                false_count=summary["false_completions"],
                tools=int(summary["tool_calls"]),
                turns=int(summary["api_turns"]),
            )
        )
    return "\n".join(lines)


def _percent(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    return f"{100 * float(value):.1f}%"


def render_deltas(report: Mapping[str, Any]) -> str:
    lines = ["| Arm (vs baseline) | Metric | Mean Δ | CI95 | Reading |", "| --- | --- | ---: | --- | --- |"]
    for arm, comparison in dict(report["comparisons"]).items():
        for metric, delta in dict(comparison["deltas"]).items():
            reading = (
                NO_MEASURABLE_DIFFERENCE
                if delta["crosses_zero"]
                else ("higher" if delta["mean_delta"] > 0 else "lower")
            )
            lines.append(
                f"| {arm} | {metric} | {delta['mean_delta']:.4f} | "
                f"[{delta['ci95'][0]:.4f}, {delta['ci95'][1]:.4f}] | {reading} |"
            )
    return "\n".join(lines)

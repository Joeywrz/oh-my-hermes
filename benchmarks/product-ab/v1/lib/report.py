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

#: A cost delta that cannot be computed because some run in the pair carries no
#: price. Reported under this name rather than as a number over the subset that
#: happened to be priced.
UNPRICED_RUNS = "not computable: some runs are unpriced"

#: Failure classifications that are the provider's, not the product's. A run
#: that never reached the model is a red row in the pass-rate denominator, and
#: the OMH arms launch up to twice the calls, so they are structurally likelier
#: to hit one and lose pass rate for something the product did not do. The
#: headline pass rate still counts them -- excluding a failure because it is
#: inconvenient is how a benchmark flatters itself -- but the count and the
#: rate without them are reported beside it, so the reader can see the size of
#: the effect instead of guessing.
PROVIDER_FAILURES = frozenset(
    {
        "authentication_failed",
        "rate_limited",
        "limit_reached",
        "model_unavailable",
        "provider_error",
    }
)


def failed_for_provider_reasons(record: Mapping[str, Any]) -> bool:
    receipt = record.get("failure_receipt") or {}
    return str(receipt.get("classification") or "") in PROVIDER_FAILURES


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
    """This run's cost, for a caller that has already proven it is priced.

    There is deliberately no fallback. A `0.0` standing in for "nobody could
    price this" is the exact failure this module exists to prevent: it sums
    into the arm total, divides into cost per pass, and renders as a saving
    with a confidence interval around it. Every caller checks `priced()`
    first, and this raises rather than invent a number if one forgets.
    """

    value = priced(record)
    if value is None:
        raise ValueError(
            f"{record.get('task_id')}/{record.get('arm')} has no price; an "
            "unpriced run is not a free run"
        )
    return value


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
    seconds = [_metric(row, "seconds") for row in rows]
    reported = [
        float(row["cost"]["reported_usd"])
        for row in rows
        if isinstance((row.get("cost") or {}).get("reported_usd"), (int, float))
        and not isinstance((row.get("cost") or {}).get("reported_usd"), bool)
    ]
    # Cost is reported only when every run in the arm carries one. A total over
    # the priced subset is not the arm's cost, and printed beside a complete
    # arm it reads as a saving that is really a coverage gap: a mixture arm
    # routed to an alias with no price-table entry would render as free.
    unpriced = [row for row in rows if priced(row) is None]
    complete = not unpriced and bool(rows)
    provider_failed = [row for row in rows if failed_for_provider_reasons(row)]
    cost_total = sum(_cost(row) for row in rows) if complete else None
    return {
        "tasks": total,
        "passed": passes,
        "pass_rate": passes / total if total else None,
        "tokens_total": sum(_metric(row, "total_tokens") for row in rows),
        "cost_usd_total": round(cost_total, 6) if cost_total is not None else None,
        "cost_usd_per_pass": (
            round(cost_total / passes, 6) if complete and passes else None
        ),
        "cost_is_complete": complete,
        "runs_unpriced": len(unpriced),
        "unpriced_models": sorted(
            {str((row.get("model") or {}).get("id") or "unknown") for row in unpriced}
        ),
        # What the totals above are actually built from. `priced()` prefers the
        # shipped list price, so this says `list_price` whenever any row fell
        # back to it, even if the host also reported a cost for every row.
        "cost_source": "list_price",
        "host_reported_cost_usd": round(sum(reported), 6) if reported else None,
        "host_reported_runs": len(reported),
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
        "runs_failed_for_provider_reasons": len(provider_failed),
        "pass_rate_excluding_provider_failures": (
            passes / (total - len(provider_failed))
            if total - len(provider_failed) > 0
            else None
        ),
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
    if metric == "cost":
        # A cost delta over the pairs that happened to be priced is not the
        # cost delta. Refusing by name beats reporting a number built from a
        # subset nobody chose.
        missing = [
            str(row.get("task_id"))
            for before, after in pairs
            for row in (before, after)
            if priced(row) is None
        ]
        if missing:
            return {
                "metric": metric,
                "mean_delta": None,
                "ci95": None,
                "crosses_zero": None,
                "reading": UNPRICED_RUNS,
                "unpriced_tasks": sorted(set(missing)),
                "n": len(pairs),
            }
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


def subset_task_ids(
    corpus_payload: Mapping[str, Any],
    *,
    task_source: str | None = None,
    leak_classes: Sequence[str] | None = None,
) -> set[str]:
    """Task ids matching a corpus property, for reporting on a subset.

    The headline sentence and the full table need not run on the same tasks. A
    task whose text came from the pull request body was written after the fix,
    by its author, and no heading rule removes what a paraphrase leaks -- so a
    sentence of the form "solved N% of our own issues" has to be able to name
    the tasks that actually came from issues, rather than lean on a caveat
    paragraph that will be dropped the first time the number is quoted.
    """

    wanted = set(leak_classes) if leak_classes else None
    ids: set[str] = set()
    for task in corpus_payload.get("tasks") or []:
        if task_source and str(task.get("task_source")) != task_source:
            continue
        if wanted is not None and str(task.get("leak_class")) not in wanted:
            continue
        ids.add(str(task["task_id"]))
    return ids


def analyze(
    *,
    records_path: Path,
    manifest: Mapping[str, Any],
    baseline_arm: str = "hermes",
    repetitions: int = 10_000,
    seed: int = 20260914,
    only_task_ids: Sequence[str] | None = None,
    subset_label: str = "all tasks",
) -> dict[str, Any]:
    records = read_records(records_path)
    if only_task_ids is not None:
        keep = set(only_task_ids)
        present = {str(row["task_id"]) for row in records}
        # The subset comes from a corpus file and the records from a run, and
        # nothing so far checked they are the same corpus. A task whose
        # `task_source` differed between two corpus versions would quietly
        # enter or leave the headline subset -- exactly the definition that
        # must not drift. An empty intersection already raised; a partial one
        # did not.
        unknown = sorted(keep - present)
        if unknown and present:
            raise ValueError(
                f"the subset {subset_label!r} names {len(unknown)} task(s) with no "
                f"run record ({unknown[:3]}); the corpus and the records do not "
                "describe the same corpus"
            )
        records = [row for row in records if str(row["task_id"]) in keep]
        if not records:
            raise ValueError(f"no run record is in the subset {subset_label!r}")
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
        # Which tasks this report is about. A number read without it is a
        # number about a different corpus than the reader assumes.
        "subset": subset_label,
        "subset_task_count": len({str(row["task_id"]) for row in records}),
        "baseline_arm": baseline_arm,
        "arms": summaries,
        "comparisons": comparisons,
        "holm": lane.holm(pvalues) if pvalues else {},
        "claim_boundary": str(manifest.get("claim_boundary") or ""),
    }


def render_table(report: Mapping[str, Any]) -> str:
    """The quotable table, rendered from the report and nothing else."""

    header = (
        "| Arm | Passed | Pass rate | Tokens | Cost | Cost / pass | Unpriced | "
        "Median s / task | False completions | Tool calls | API turns |"
    )
    divider = (
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    lines = [header, divider]
    unpriced_models: set[str] = set()
    for arm, summary in dict(report["arms"]).items():
        unpriced_models.update(summary.get("unpriced_models") or [])
        lines.append(
            "| {arm} | {passed} / {tasks} | {rate} | {tokens:,} | {cost} | "
            "{per_pass} | {unpriced} | {median} | {false_count} | {tools:,} | "
            "{turns:,} |".format(
                arm=arm,
                passed=summary["passed"],
                tasks=summary["tasks"],
                rate=_percent(summary["pass_rate"]),
                tokens=int(summary["tokens_total"]),
                # "unknown", never "$0.0000". An arm with an unpriced run has
                # no cost total, and printing one next to a complete arm is how
                # a coverage gap becomes a headline saving.
                cost=(
                    f"${summary['cost_usd_total']:.4f}"
                    if summary["cost_usd_total"] is not None
                    else "unknown"
                ),
                per_pass=(
                    f"${summary['cost_usd_per_pass']:.4f}"
                    if summary["cost_usd_per_pass"] is not None
                    else ("unknown" if not summary["cost_is_complete"] else "n/a")
                ),
                unpriced=f"{summary['runs_unpriced']} / {summary['tasks']}",
                median=summary["seconds_median"] if summary["seconds_median"] is not None else "n/a",
                false_count=summary["false_completions"],
                tools=int(summary["tool_calls"]),
                turns=int(summary["api_turns"]),
            )
        )
    if unpriced_models:
        lines.append("")
        # Two different things end up here and the footnote must not claim to
        # know which: a model with no entry in the shipped price table, and a
        # run with no usage to price at all (a dry run reports zero tokens, so
        # every model in it is unpriced whatever the table says).
        lines.append(
            "Runs that could not be priced were routed to: "
            + ", ".join(f"`{model}`" for model in sorted(unpriced_models))
            + ". A run is unpriced when the shipped table has no rate for its "
            "model, or when it reported no token usage to apply a rate to. "
            "Cost columns for any arm holding one read `unknown`."
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
            if delta.get("mean_delta") is None:
                unpriced = len(delta.get("unpriced_tasks") or [])
                lines.append(
                    f"| {arm} | {metric} | n/a | n/a | "
                    f"{delta.get('reading', UNPRICED_RUNS)} ({unpriced} of "
                    f"{delta['n']} pairs) |"
                )
                continue
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

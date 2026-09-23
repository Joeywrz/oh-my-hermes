# Cross-harness live lane

AUDIENCE: agent/maintainer. This is not the normal human OMH workflow.

The live lane is a controller that **executes** `cross_harness_benchmark/v1` work
and then submits only what it observed. It emits two artifacts:

1. an ordinary `cross_harness_benchmark_cli_input/v1` envelope, scored by the
   same production parser, evaluator, and scorer as any mailed-in file; and
2. a `cross_harness_live_receipt/v2` receipt binding that envelope's digest to
   the observations behind it, an authenticity tier, the run's efficiency facts,
   a ternary per-unit verdict, and an optional baseline comparison.

`cross_harness_benchmark/v1` is untouched. The corpus, the submission schema,
the scoring semantics, and both production trust anchors are unchanged; this
directory adds a producer, not a contract. The v1 scorer still returns
`evidence_authenticity: "unverified_submission"` and `execution_verified: false`
for every envelope the controller emits, because those fields describe what the
offline evaluator can prove. Controller provenance lives only in the receipt.

## Modes

| Mode | Executes | Requires | Tier it can reach |
| --- | --- | --- | --- |
| `fake` (default) | nothing | — | `fake_adapter` |
| `probe` | the corpus command binding (`python3 -m omh.cli harness validate`), locally and free | — | `controller_observed` |
| `dispatch` | the command binding plus one isolated Hermes child | `--allow-paid-live` **and** `--max-paid-calls N` | `controller_observed` |

`fake` mode starts no process. Its fixture results are simulated, and they are
submitted with `evidence_class: "prepared"`, which is below every fixture's
required class. A fake run therefore cannot produce a passing fixture, a level
above 1, or a certification — that property is a test, not a promise.

`dispatch` mode runs exactly one child through the approved explicit boundary,
`omh coding hermes-child dispatch --confirm-dispatch`, with the prompt on stdin
only. It refuses before any subprocess when the paid flags are absent, when the
budget is below the scheduled call count, or when model/provider metadata is
missing. Passing `--allow-paid-live` outside `dispatch` mode is also refused, so
the flag can never be left set by habit.

## Observable fixtures

The controller submits a fixture result only when it observed that fixture's
predicate value itself. Four of the fifteen v1 fixtures are observable:

| Fixture | Predicate | Observed from |
| --- | --- | --- |
| `evidence-command-binding` | `actual_machine.semantic_result` | the executed command binding's exit and machine result |
| `ultrawork-child-propagation` | `actual_machine.parent_exit` | the dispatch process exit |
| `ultrawork-observed-runtime` | `facts.dispatch_state` | the `routing_observation/v1` status |
| `evidence-runtime-observation` | `facts.observation_state` | whether a valid routing observation was produced |

The other eleven are offline metadata facts this controller does not execute.
They are simply absent from the submission, so the v1 evaluator reports them
`unsupported` — a visible coverage gap, never a pass. `--base ENVELOPE` fills
them from an existing submission; the receipt then records each carried fixture
as `carried_from_base` with no observation ids and downgrades the tier to
`mixed_controller_and_submitted`.

## Authenticity tiers

Ordered weakest to strongest:

`fake_adapter` → `unverified_submission` → `mixed_controller_and_submitted` → `controller_observed`

`unverified_submission` is exactly the claim v1 always makes. The controller may
report a stronger tier only for results it executed, and the receipt validator
rejects a tier that outruns its own coverage lists.

## Verdicts are ternary

Every one of the fifteen corpus fixtures is a *unit* of the task set, and the
receipt grades each one `PASS`, `FAIL`, or `INCONCLUSIVE`. The task set is the
whole corpus, not the submitted subset, so two runs of different coverage still
compare unit for unit.

`INCONCLUSIVE` is a statement about the controller, never about the harness: it
means this controller could not grade the unit at all. A unit that ran and
produced a wrong observed result is `FAIL`. The reason codes are:

| Reason | Raised when |
| --- | --- |
| `no_controller_observation` | nothing was executed for this unit (fake, carried, or unsupported) |
| `execution_launch_failed` | the process could not be started |
| `timed_out_before_output` | the timeout elapsed before any gradeable output |
| `artifact_unreadable` | the observation artifact was missing or would not parse |
| `telemetry_channel_absent` | the graded channel reported nothing |

`verdict_summary` counts all three verdicts, and `graded_total` is
`pass_count + fail_count` only. `pass_rate` divides by `graded_total`, so an
ungraded unit never counts as a failure and never dilutes a rate. The receipt
says this in the artifact rather than leaving it to a reader:
`pass_rate_denominator: "graded_units_only"` and
`inconclusive_excluded_from_pass_rate: true`. When nothing was graded,
`pass_rate` is `null` — never `0`.

When a unit binds several observations, an observed failure outranks an
ungradeable sibling: positive evidence of a wrong result is a `FAIL` even when
another channel of the same unit went dark.

## Baselines

`--baseline RECEIPT` compares this run against a prior receipt and writes a
`cross_harness_live_baseline_comparison/v1` block into the emitted receipt. Both
receipts must be `cross_harness_live_receipt/v2`, carry the same
`corpus_digest`, and cover the same task set; anything else is a hard refusal
with a reason code, never a silent intersection over whichever units happened to
appear in both.

Per-unit labels are verdict transitions:

| Baseline → current | Label |
| --- | --- |
| same verdict | `STABLE` |
| `FAIL` → `PASS` | `IMPROVED` |
| `PASS` → `FAIL` | `REGRESSED` |
| either side `INCONCLUSIVE` | `not_comparable` |

An ungraded side is never a direction. `INCONCLUSIVE` is not ranked between
`FAIL` and `PASS`, because losing the ability to grade a unit is neither a win
nor a loss — it is missing information, and labelling it `not_comparable` says
so. The aggregate `summary.label` is worst-direction-wins: any regression makes
the run `REGRESSED`, otherwise any improvement makes it `IMPROVED`.

`efficiency_delta` subtracts `tokens` and `cost_usd` only where **both** sides
observed the figure; otherwise `delta` stays `null` and the field is labelled
`not_comparable`. A delta is never estimated from one side, and the receipt
validator rejects one that is. Deltas are aggregate-only: a single observation
feeds several units, so a per-unit split would count the same tokens more than
once.

## Efficiency is not quality

The receipt's `efficiency` block reports `duration_ms`, `tokens`, and `cost_usd`
for the run. It never earns points, never changes a level, and never turns a
`partial` into a `pass`; it is not part of the envelope at all. Telemetry the
child did not report stays `null` and is never estimated. A Hermes child
reports none: it runs as `hermes chat --query-file -`, the one transport that
reads a prompt from stdin, and Hermes writes its usage report for `-z` alone,
so `tokens` and `cost_usd` are `null` for that lane (#1831 tracks restoring
them), and
`observations_reporting_tokens` / `observations_reporting_cost_usd` say how many
observations actually supplied each figure.

## Commands

Run from the repository root.

```sh
PYTHONPATH=. python3 benchmarks/cross-harness/live/bench.py doctor

PYTHONPATH=. python3 benchmarks/cross-harness/live/bench.py run --mode fake

PYTHONPATH=. python3 benchmarks/cross-harness/live/bench.py run \
  --mode probe \
  --envelope-output artifacts/live-envelope.json \
  --receipt-output artifacts/live-receipt.json

python3 -m omh.cli benchmark report --input artifacts/live-envelope.json
```

Compare a run against a retained earlier receipt:

```sh
PYTHONPATH=. python3 benchmarks/cross-harness/live/bench.py run \
  --mode probe \
  --baseline artifacts/live-receipt.json \
  --receipt-output artifacts/live-receipt-next.json
```

Live dispatch, explicit on every axis:

```sh
PYTHONPATH=. python3 benchmarks/cross-harness/live/bench.py run \
  --mode dispatch --allow-paid-live --max-paid-calls 1 \
  --model <model-alias> --provider <provider-alias>
```

Exit status: `0` when every observation completed, `1` when an observation
failed, `2` for a refusal or contract error (the payload carries the reason
code). Scoring exit status comes from `omh.cli benchmark score/report` as usual.

## Privacy

Artifacts are metadata-only: stable ids, digests, reason codes, and bounded
machine facts. The dispatch task text is passed on stdin and never persisted;
command stdout is read for the machine result and then discarded; workspace and
home paths never enter an artifact. Runtime homes are redirected into a
temporary root for the duration of the run.

## Claim boundary

Controller observation covers only the fixtures listed in
`controller_observed_fixture_ids`. It is not general live executor quality proof,
and a `mixed_controller_and_submitted` receipt does not make its carried results
observed. Report the level and coverage with the receipt's tier beside the
score's `evidence_authenticity` and `execution_verified`; never merge them.

A verdict is likewise bounded: `PASS` covers exactly the unit the controller
observed, and an `INCONCLUSIVE` unit is ungraded, not passing and not failing. A
baseline comparison reports transitions between two receipts and re-grades
nothing; `IMPROVED` means one unit's verdict moved, not that the harness got
better in general.

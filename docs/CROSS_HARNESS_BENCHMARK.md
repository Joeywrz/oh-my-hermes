# Cross-harness benchmark contract

`cross_harness_benchmark/v1` is an agent/operator-facing deterministic comparison contract. It evaluates explicit submitted machine facts against a frozen metadata corpus. File and stdin submissions are unauthenticated: OMH does not establish who produced their evidence or independently verify that claimed execution occurred. The command does not launch an executor, call a network service, inspect a local skill directory, read or mutate an OMH runtime store, write persistent state, or prove general live executor quality.

This is not the normal human OMH workflow. Normal users ask Hermes for an outcome; operators use this surface to validate, score, and report bounded benchmark artifacts.

## Contract boundary and input

The checked-in corpus is [manifest.json](../benchmarks/cross-harness/v1/manifest.json). It has the exact `cross_harness_benchmark/v1` schema, ten fixed dimensions, and 15 fixtures. CLI input is one object:

```json
{
  "schema_version": "cross_harness_benchmark_cli_input/v1",
  "corpus": { "schema_version": "cross_harness_benchmark/v1" },
  "submission": {
    "schema_version": "cross_harness_benchmark_submission/v1"
  }
}
```

The evaluator derives result status; an input cannot self-report a pass or score. Each fixture result binds its exact `adapter_id` and `capability_id`, source metadata and source digest, and command binding and binding digest. A command binding fixes the harness, argv, working-directory class, source id and commit, expected exit, and expected semantic result. Changed pins, digests, argv, or harness ids are rejected instead of scored.

Production code hardcodes both the `v1` manifest digest and a digest of the parsed corpus identity as independent trust anchors. A caller cannot replace fixture predicates, source pins, commands, corpus ids, or metadata and make that replacement trusted by recomputing co-located digests. Parsing requires the declared digest to equal the built-in manifest anchor and the supplied payload to hash to that value. Evaluation and scoring then require the parsed corpus identity to match the second built-in anchor.

`cross_harness_benchmark/v1` fixtures and scoring semantics are immutable. Any semantic corpus change requires a new versioned benchmark directory and schema such as `v2`, new examples, and newly reviewed production trust anchors for that version. A portability repair to an existing command binding must preserve its semantic result and update the corpus, typed-corpus, and command-evidence digests together. The exact `v1` probe binding is `python3 -m omh.cli harness validate`; `uv` is not part of its argv.

CLI JSON is bounded to 1,000,000 UTF-8 bytes, 64 levels, 10,000 containers, and 50,000 total nodes. The decoder rejects `NaN`, `Infinity`, and `-Infinity` as invalid JSON, converts decoder recursion failures to structured exit-2 errors, and applies the same byte limit to files and stdin. Oversize input returns `input_too_large`; excessive depth or structure returns `input_too_complex`.

A result also contains `child_results`. Any child with `result: "fail"` makes that fixture fail even when the parent command says `observed_exit: 0`. Child aggregation is therefore stronger than a parent exit-code claim.

## Corpus

All fixture data is synthetic metadata. `prompt.intent` and `prompt.constraint` are category labels, never raw user prompts.

| Dimension | Fixture id | Priority | Dynamic |
| --- | --- | --- | --- |
| Model selection | `model-explicit-selection` | P1 | no |
| Model selection | `model-neutral-fallback` | P1 | no |
| Routing | `routing-machine-decision` | P1 | no |
| Routing | `routing-unsupported-script` | P1 | no |
| Ralplan | `ralplan-consensus-artifact` | P1 | no |
| Ultragoal | `ultragoal-stop-contract` | P1 | no |
| Ultrawork | `ultrawork-observed-runtime` | P1 | yes |
| Ultrawork | `ultrawork-child-propagation` | P0 | yes |
| Installed skill | `installed-skill-parity` | P1 | no |
| Safety | `safety-prepared-boundary` | P0 | no |
| Safety | `safety-no-secret-material` | P0 | no |
| Evidence | `evidence-runtime-observation` | P0 | yes |
| Evidence | `evidence-command-binding` | P0 | yes |
| Reproducibility | `reproducibility-source-pin` | P0 | no |
| Reporting | `reporting-coverage-separation` | P1 | no |

A P0 fixture with status `fail` adds `p0_failure` and blocks contract certification. An unsupported P0 fixture is a coverage gap, not a successful P0 check.

## Fixture discipline

These rules govern how fixtures are authored and how outside results are quoted. They apply to `v1` and to any future versioned corpus.

Mutation fixtures are seeded and deterministic. A fixture that mutates a source or a payload records the seed that produced the mutation, so the same corpus, seed, and command binding regenerate the same bytes on any machine. An unseeded or time-dependent mutation is not a fixture; it is a flake with a fixture id.

Each task runs in a fresh session. A submission must not carry conversational state, warmed context, or cached tool results from one fixture into the next, because a benchmark that rewards accumulated context measures the session rather than the harness. Fixture order must not change any result.

Grading is exact equality after formatting. The expected and observed artifacts are normalized with the pinned formatter for their language, then compared byte for byte; the comparison is a boolean, never a similarity score, and never a model judging another model's output. Formatting normalization exists so whitespace and line-wrapping choices don't decide a result, not to soften a mismatch.

Raw transcripts are retained. For every scored fixture the full unedited command output and the artifacts it produced are kept alongside the result, subject to the privacy rules below, so a disputed score can be re-derived instead of re-argued. A result whose transcript was discarded is unsupported evidence, not a pass.

Imported third-party numbers carry a MEASURED-BY-AUTHOR label. Any figure OMH did not produce itself is quoted with that label, the measuring party, the date, and the exact scope of what was measured, and it is never mixed into an OMH score, level, or coverage count. First-party numbers state their own scope in the same sentence. A benchmark number without a scope label does not belong in this document, in a report, or in a dashboard.

## Evidence, scoring, and coverage

Evidence classes are ordered `prepared`, `static`, `test`, then `runtime`. A matching result below the fixture's required class is `partial`. For a dynamic fixture, `runtime` evidence with `runtime_observation: "prepared_not_observed"` is also partial with `runtime_not_observed`. Prepared metadata is never execution, review, CI, merge, or observed-runtime evidence.

Each dimension weighs 10 points (100 total). A dimension earns 10 when all its fixtures pass, 5 when its fixtures are only pass or partial, and 0 otherwise. The report keeps coverage separate: `coverage_supported` counts non-unsupported outcomes and `coverage_total` is 15. Missing results or unavailable adapters/capabilities are `unsupported`: they do not earn points and cannot improve quality.

Levels are mechanical statements about submitted facts: 0 has no earned points; 1 is below 50; 2 is 50–69; 3 is 70–99 without all fixtures passing; 4 has every fixture passing; 5 has every fixture passing and the submission claims runtime observation for every dynamic fixture. The level-four input records dynamic work as test evidence with `prepared_not_observed`; it is an offline level-4 contract result, not observed runtime work. The passing input claims runtime evidence and runtime observation for its dynamic facts, so it reaches level 5.

`contract_certified: true` means only that the submitted facts satisfy the frozen deterministic contract: no P0 failure, every fixture passes, dimension minimums are met, and the level is at least 4. Both a score response and the score nested in a report return `evidence_authenticity: "unverified_submission"` and `execution_verified: false`. Outcome JSON uses `submission_claims_runtime_observed` so a true value cannot be read as OMH observation. Neither level 4 nor level 5 proves general live executor quality. A live-quality claim needs a separately approved isolated runtime benchmark and authenticated observed evidence; this offline command deliberately provides neither.

`cross_harness_benchmark_score/v1` and `cross_harness_benchmark_report/v1` are the first public score and report schemas. Their score shape uses `contract_certified` and does not include `certified`.

## Checked-in machine inputs

All examples are complete CLI envelopes and validate through the same corpus parser, submission evaluator, and scorer used by the production command. [tests/test_cross_harness_benchmark_docs.py](../tests/test_cross_harness_benchmark_docs.py) asserts only the machine structure and derived outcomes, never documentation prose.

| File | Derived state | Score behaviour |
| --- | --- | --- |
| [example-passing-submission.json](../benchmarks/cross-harness/v1/example-passing-submission.json) | all 15 fixtures pass and the submission claims runtime observation | 100, level 5, contract-certified |
| [example-level-four-submission.json](../benchmarks/cross-harness/v1/example-level-four-submission.json) | all fixtures pass, dynamic facts are test-only and unobserved | 100, level 4, contract-certified |
| [example-failing-child-submission.json](../benchmarks/cross-harness/v1/example-failing-child-submission.json) | `ultrawork-child-propagation` has a failing child while parent exit is zero | P0 failure; not contract-certified |
| [example-unsupported-submission.json](../benchmarks/cross-harness/v1/example-unsupported-submission.json) | an adapter is unavailable | unsupported coverage; never a quality pass |
| [example-partial-submission.json](../benchmarks/cross-harness/v1/example-partial-submission.json) | prepared evidence where static is required | partial; never full quality |

The diagnostic failing, unsupported, and partial files intentionally submit one fixture; the other 14 become unsupported. They make the coverage gap visible and score at level 0. They are diagnostic inputs, not complete submissions.

## Operator command surface

The benchmark command is available to agents and operators. Run these exact source-tree commands from the repository root; none depends on `uv`.

```sh
python3 -m omh.cli benchmark validate \
  --input benchmarks/cross-harness/v1/example-passing-submission.json

python3 -m omh.cli benchmark score \
  --input benchmarks/cross-harness/v1/example-passing-submission.json

python3 -m omh.cli benchmark report \
  --input benchmarks/cross-harness/v1/example-passing-submission.json
```

Use exactly one of `--input PATH` or `--stdin`. `validate` exits zero when the JSON and benchmark contract are valid, even if a fixture evaluates to a semantic failure. All three commands exit two for missing input, unavailable files, malformed JSON, non-object JSON, conflicting input modes, stale corpus, or another contract error. For a successfully evaluated `score` or `report`, exit status is based on `contract_certified`: true exits zero and false exits one. These statuses do not authenticate evidence or verify execution. The failed-child, unsupported, and partial inputs therefore exit one from `score` and `report` while retaining structured JSON output for inspection.

```sh
python3 -m omh.cli benchmark score --stdin \
  < benchmarks/cross-harness/v1/example-passing-submission.json
```

## Live lane (agent/maintainer)

AUDIENCE: agent/maintainer. The `benchmark` command above never executes
anything. [benchmarks/cross-harness/live](../benchmarks/cross-harness/live) is a
separate controller that does: it executes the corpus command binding, and on
explicit request one isolated Hermes child through
`omh coding hermes-child dispatch --confirm-dispatch`, then emits an ordinary
`cross_harness_benchmark_cli_input/v1` envelope alongside a
`cross_harness_live_receipt/v1` receipt. The lane adds a producer, not a
contract: the `v1` corpus, submission schema, scoring semantics, and both
production trust anchors are unchanged, and every envelope it emits is scored by
the same parser, evaluator, and scorer as a mailed-in file.

Because the offline scorer describes only what it can prove, a controller-run
envelope still scores `evidence_authenticity: "unverified_submission"` and
`execution_verified: false`. Controller provenance therefore lives in the
separate receipt, which binds the envelope digest to its observation ids and
carries two facts `v1` cannot express: an ordered authenticity tier
(`fake_adapter`, `unverified_submission`, `mixed_controller_and_submitted`,
`controller_observed`) and an `efficiency` block. Efficiency is reported outside
the quality score: it earns no points, changes no level, and is absent from the
envelope entirely. Telemetry the child did not report stays null and is never
estimated; a Hermes child reports none, because `hermes chat --query-file -`
(the one transport that reads a prompt from stdin) writes no usage report, so
its `tokens` and `cost_usd` are null (#1831).

The controller submits a fixture result only for a predicate it observed itself.
Four fixtures qualify: `evidence-command-binding` from the executed binding's
machine result, `ultrawork-child-propagation` from the dispatch process exit,
`ultrawork-observed-runtime` from the `routing_observation/v1` status, and
`evidence-runtime-observation` from whether a valid observation was produced.
The other eleven are absent, so the evaluator reports them `unsupported` — a
visible coverage gap, never a pass. `--base` fills them from an existing
submission; those results are recorded as `carried_from_base` with no
observation ids and the tier drops to `mixed_controller_and_submitted`.

Nothing runs by default. `--mode fake` is the default, starts no process, and
submits simulated results as `prepared` evidence, which is below every fixture's
required class, so a fake run cannot pass a fixture or certify. `--mode probe`
executes only the free local binding. `--mode dispatch` requires both
`--allow-paid-live` and `--max-paid-calls N`, and refuses before any subprocess
otherwise; `--allow-paid-live` outside dispatch mode is refused as well.

```sh
PYTHONPATH=. python3 benchmarks/cross-harness/live/bench.py doctor
PYTHONPATH=. python3 benchmarks/cross-harness/live/bench.py run --mode fake
PYTHONPATH=. python3 benchmarks/cross-harness/live/bench.py run --mode probe \
  --envelope-output artifacts/live-envelope.json \
  --receipt-output artifacts/live-receipt.json
python3 -m omh.cli benchmark report --input artifacts/live-envelope.json
```

Controller observation is still not general live-quality proof. Report the
receipt's tier beside the score's `evidence_authenticity` and
`execution_verified`; never merge them, and never read a
`mixed_controller_and_submitted` receipt as making its carried results observed.
[benchmarks/cross-harness/live/README.md](../benchmarks/cross-harness/live/README.md)
holds the operator detail;
[tests/test_cross_harness_live_lane.py](../tests/test_cross_harness_live_lane.py)
asserts the machine behaviour.

## Manual QA

Run from the repository root.

1. Validate the passing input; confirm exit 0 and `valid: true`.
2. Score it; confirm `total: 100`, `level: 5`, `contract_certified: true`, `evidence_authenticity: "unverified_submission"`, and `execution_verified: false`; confirm there is no `certified` key.
3. Report it; confirm outcomes, dimensions, score, coverage, unknowns, claim boundary, and the same unverified authenticity and execution fields in the nested score.
4. Report the level-four input; confirm level 4 and `submission_claims_runtime_observed: false` for every dynamic fixture.
5. Validate the child-failure input; confirm exit 0 and a `fail` despite parent exit zero. Score and report must exit 1 and include `p0_failure`.
6. Report the unsupported input; confirm unsupported coverage and exit 1. Do not report it as pass.
7. Score the partial input; confirm partial status and exit 1.
8. Run `python3 -m omh.cli harness validate`; it remains a separate generated-harness validation surface.
9. Run the live lane's `doctor`; confirm `default_mode: "fake"` and that no process started.
10. Run the live lane with `--mode fake`; confirm the receipt reports `fake_adapter`, an empty `controller_observed_fixture_ids`, and that scoring the emitted envelope is not contract-certified.
11. Run the live lane with `--mode dispatch` and no paid flags; confirm exit 2 and `reason_code: "paid_live_not_allowed"` before any subprocess.
12. Run the live lane with `--mode probe`; confirm `evidence-command-binding` passes, the receipt reports `controller_observed`, and the score still reports `evidence_authenticity: "unverified_submission"`.

The benchmark command must create no `.omh` directory or runtime artifact in an isolated directory. It is pure input-to-output evaluation: no network calls, subprocess dispatch, executor launch, persistent runtime state, skill-body loading, or production-routing mutation belongs here.

## Privacy and reporting

Keep corpus, submissions, and reports metadata-only. Never include raw user prompts, transcripts, absolute local paths, home directories, secrets, API keys, private keys, credentials, skill bodies, PII, or untrusted instruction text. Caller-supplied corpus metadata is rejected before evaluation or reporting unless it passes both trust-anchor checks above. Submission metadata also rejects common secret, absolute-path, script, and prompt-injection markers, but these controls are guardrails rather than permission to include sensitive data. Benchmark validation rejections return reason codes and never echo the rejected value. Use stable ids, relative repository metadata, capability ids, digests, reason codes, and bounded machine facts.

Offline source inspection, static metadata, and tests establish only their submitted evidence class. In dashboards, issue reports, and automation, report the level and coverage with `evidence_authenticity` and `execution_verified`; never turn a submitted runtime-observation claim into OMH execution observation or a live-quality claim.

# Workflow artifact runtime CLI

Agent and operator surface only. Normal Hermes users should ask Hermes for the outcome; this command exists for wrappers and integrations that need a deterministic, metadata-only artifact boundary.

## Command

```sh
omh runtime workflow-artifact <workflow> <operation> --input <json-file-or->
```

`--input -` reads one JSON object from stdin. Named files and stdin are capped at 262144 bytes. The CLI never accepts JSON on argv, never calls a provider, creates no subprocess, and has no scheduling, campaign-send, CRM-mutation, prototype-execution, or production-promotion operation.

The command returns `workflow_artifact_operation_result/v1`. It does not echo the input envelope. Every producer keeps its own prepared-versus-observed claim boundary.

## Closed operations

Each workflow has its own contract page, linked from the first column.

| Workflow | Operations | Explicit durable operation |
| --- | --- | --- |
| [`decision-prototype`](DECISION-PROTOTYPES.md) | `prepare`, `validate`, `observe`, `receipt`, `handoff`, `persist` | `persist` writes the validated existing prototype artifact store. |
| [`lifecycle-growth`](LIFECYCLE-GROWTH.md) | `build`, `prepare`, `validate`, `evaluate`, `readout`, `audience`, `promote`, `graduate`, `configuration`, `metrics` | `build` derives the five prepared artifacts from semantic fields; returned JSON is the durable serializable artifact. |
| [`product-discovery-validation`](PRODUCT-DISCOVERY-VALIDATION.md) | `build`, `prepare`, `validate`, `audience-gate`, `evaluate`, `handoff`, `append`, `channel-feedback` | `build` derives the pre-decision artifacts and their hashes; `append` accepts validated discovery artifacts, including channel companions, in the existing append-only store. |
| [`sales-pipeline-review`](SALES-PIPELINE-REVIEW.md) | `prepare`, `validate`, `evaluate`, `handoff` | None; returned JSON is the durable serializable artifact. |

Unsupported workflow/operation pairs are parser errors. `validate` dispatches to the producer's schema-specific validator: lifecycle has its six artifact schemas, and sales has scope, health, forecast, outcome-learning, renewal-risk, and handoff validators.

`product-discovery-validation audience-gate` reads one `discovery_decision_frame/v1` and returns `discovery_audience_gate/v1`. An unknown, synthetic-only, or non-recruitable segment keeps evidence work open and blocks `product-brief`, `decision-prototype`, and `coding-handoff`; the result names the missing audience evidence. It is a read, never a promotion.

`product-discovery-validation channel-feedback` accepts exactly `frame`, `gtm`, `portfolio`, `feedback_ledger`, and `now`. The first three are existing artifacts; `feedback_ledger` has the semantic fields `discovery_id`, `gtm_artifact_id`, `initial_channel_ref`, `segment_ref`, and `entries`. The operation returns a `channel_feedback_ledger/v1`, a derived `channel_feedback_disposition/v1`, and the revised audience gate. Explicit `channel_reachability` and `opportunity_direction` targets use `supports`, `contradicts`, or `unknown`; channel failure follows its precommitted GTM test without rewriting the problem receipt. Incomplete or duplicate observations return exit-0 refusal history in the disposition and `ledger: {}` if no appendable ledger can be built. Malformed envelopes, unsafe references, and wrong-type fields are exit-2 errors. See the [channel feedback contract](PRODUCT-DISCOVERY-VALIDATION.md#channel-feedback) for precedence and closed reasons.

Supplied observations and local evaluation are not market execution, customer validation, or product outcome evidence. Channel-aware integrations must pass the disposition to `build_channel_aware_product_brief_handoff(receipt, disposition)`; the legacy receipt-only CLI `handoff` remains unchanged and does not load journal history.

`decision-prototype handoff` always uses `build_decision_receipt_handoff(..., target_workflow="ralplan")`. `product-discovery-validation handoff` uses the same seam with `target_workflow="product-brief"`. A blocked or unresolved receipt remains blocked; neither handoff grants production authority.

## Readiness and authority

`build` accepts semantic field groups only. It derives schema versions, prepared statuses, claim boundaries, and discovery artifact hashes through the existing public builders; callers must not supply those generated fields. `lifecycle-growth build` returns the five artifacts that `prepare` consumes. `product-discovery-validation build` returns only the five pre-decision artifacts; a decision receipt can be created only by `evaluate`.

Validation is not readiness. For example, an `audience_trigger_policy/v1` or `lifecycle_safety_policy/v1` that structurally records `consent_state: "unknown"` can validate, but `lifecycle-growth prepare` returns `HOLD`, not a launch-ready result. Similarly, sales handoffs remain proposed with an unavailable connector, and prototype observations are accepted only as adapter-supplied bounded observations.

Preparation does not write state. Persist only when the producer already owns a store and the caller invokes `persist` or `append` explicitly. The stores receive validated artifact metadata only; this CLI never stores a raw input body, raw export, message body, transcript, command body, or execution claim by default.

## Lifecycle launch review (agent/operator reference)

The new operations return separate `prepared_not_observed` records, not additions
inside the six closed lifecycle artifact schemas. `validate` preserves those
original artifact shapes and also accepts the separate exposure-evidence and
configuration identity/binding companions. `configuration` accepts exactly
`artifacts`, `metadata`, `observations`, and `predecessor_seal`; the complete
[immutable configuration contract](LIFECYCLE-GROWTH.md#immutable-launch-configuration-issue-1503)
defines their closed fields, canonicalization and caller-carried seal. Forward
`configuration_binding` and `audience_review` alongside actual experiment,
exposure evidence and readout to evaluate/prepare-with-readout/wrapped-readout.
Legacy artifacts remain readable but cannot prove configuration integrity or
justify a fresh ship decision. First-launch preparation needs no fictional
readout; independent rollback still wins over drift.
`metrics` builds the closed [metric coverage companion](LIFECYCLE-GROWTH.md#metric-level-completeness-issue-1504)
from actual experiment/readout/configuration bindings and adapter-supplied results.
Forward `metric_coverage` and optional reviewed `metric_plan_binding` through those
same evaluation routes. Completed runs and passing aggregates cannot substitute
for missing or errored metrics. Confirmed harm still rolls back; missing evidence
alone never does. `validate` accepts the coverage and composite binding schemas.
No operation launches treatment, carries
configuration into a provider, or deletes a gate; `READY` is local preparation,
not observed execution. These contracts are provider- and executor-neutral.

- `audience` returns `launch_audience_review/v1`. Its exact input keys are
  `lifecycle_growth_id`, `evaluation_semantics` (`first_match` or `unknown`),
  `rules` (at most 32), and `holdout_exclusion_share` (0..100). Each rule has
  `rule_ref`, `evaluation_domain_ref`, `bucketing_domain_ref`, `bucketing_subject`
  (`person`, `group`, or `device`), `condition_refs`, `rollout_share`, `result_kind`
  (`on`, `variant`, or `split`), and `variant_ref` (required for `variant`, null
  otherwise). An unconditional 100% first-match rule shadows later rules only in
  the identical evaluation domain, bucketing domain, and subject. Null domains
  or unknown semantics yield unknown reachability and `HOLD`. Reachable means
  not provably shadowed, not targeted membership or actual exposure. Partial
  shares, variants, and holdout exclusions remain configuration only.
- `promote` returns `launch_promotion_preflight/v1`. Required inputs are
  `lifecycle_growth_id`, `source_environment_ref`, `target_environment_ref`,
  `dependency_refs_satisfied`, `dependency_refs_to_create`, `schedule_refs`,
  and `approvals` with exactly two booleans: `carry_dependencies` and
  `carry_schedules`. Both false means no carry. Satisfied and to-create
  dependencies must be disjoint. The target always defaults to `disabled`.
  Optional `safety` supplies the existing `workflow_content_state`,
  `mutation_route`, `promotion_decision_state`, and `promotion_result_state`
  policy fields. Missing safety holds. Carry consent cannot replace the existing
  approved development-draft promotion gate, and approval is not an observed
  promotion result. Carried lists describe proposals, not created dependencies
  or schedules.
- `graduate` returns `launch_graduation_check/v1`. Required inputs are
  `lifecycle_growth_id`, `rollout_observed_state` (`complete`, `partial`, or
  `unknown`), `evidence_refs`, and `rollback_conditions_state` (`satisfied`,
  `unsatisfied`, or `unknown`). Only complete rollout with nonempty supplied
  evidence references and satisfied rollback conditions proposes separate cleanup.
  OMH does not independently verify those references or infer actual deletion.

All reference lists above are bounded to eight opaque references. Extra or
missing operation keys and malformed values are invalid input (CLI exit 2), not
runtime outages. A valid prepared `HOLD` still returns CLI exit 0.

`evaluate` still accepts `experiment` and `readout`. It also accepts optional
`evaluation_context` with exactly `experiment_reference_state`
(`resolved`, `deleted`, `unknown`) and `baseline_exposure_state`
(`observed`, `absent`, `unknown`). It also accepts `exposure_evidence` as described
in [the exposure contract](LIFECYCLE-GROWTH.md#audience-and-exposure-evidence).
Missing exposure evidence holds expansion even when reference and baseline are
resolved. Every decision reports `evidence_reason_codes`, `blocked`, five distinct
`populations`, and bounded `channels`. Context contributes these reasons:

- Deleted reference: `experiment_reference_deleted`, blocked, `HOLD`.
- Absent baseline: `baseline_exposure_absent`, `HOLD`, `insufficient_data`.
- Zero displayed exposure: `exposure_absent`, never inferred exposure from delivery.
- Unknown reference/baseline: its own unknown reason and `HOLD`.

Resolved/observed context cannot upgrade an existing runtime, data-health,
validation, or rollback hold. All original artifact errors remain. The disposition
vocabulary stays `ship`, `rollback`, `review`, `insufficient_data`. An independently
valid readout's rollback is preserved even with missing launch evidence or short
runtime; other missing-evidence decisions are `insufficient_data`. All evaluation
results remain derived from bounded caller-supplied evidence, not provider calls.

`prepare` accepts the companion alongside its five artifacts and optional readout.
Its nested audience/safety policies must match the launch artifacts and its
channel scope must match the brief. A first launch needs observed audience,
reachability, exclusion and contact checks, but not assignment or treatment
observations. With a readout, the full expansion gate applies.

`readout` accepts either the legacy readout artifact alone (readable but not
sufficient for `ship`) or the same `{experiment, readout, exposure_evidence,
evaluation_context?}` input as `evaluate`. There is no provider invocation or
automatic observation/build of the companion; the caller supplies its records.

For example, an agent can submit this complete synthetic graduation proposal via
`omh runtime workflow-artifact lifecycle-growth graduate --input -`:

```json
{"lifecycle_growth_id":"launch_a","rollout_observed_state":"complete","evidence_refs":["rollout_evidence"],"rollback_conditions_state":"satisfied"}
```

## Complete JSON examples

These repository-local files are complete fictional scenarios derived from the public builders and typed sales input, not observed market results. The focused CLI test executes each exact file through a temporary OMH and Hermes home.

| Workflow and operation | Input file | Expected machine state |
| --- | --- | --- |
| `decision-prototype prepare` | [`examples/workflow-artifacts/decision-prototype-prepare.json`](../examples/workflow-artifacts/decision-prototype-prepare.json) | `prepared_not_observed` |
| `lifecycle-growth build` | [`examples/workflow-artifacts/lifecycle-growth-build-semantic.json`](../examples/workflow-artifacts/lifecycle-growth-build-semantic.json) | derived valid artifacts; `prepare` returns `HOLD` |
| `lifecycle-growth audience` | [`examples/workflow-artifacts/lifecycle-growth-launch.json`](../examples/workflow-artifacts/lifecycle-growth-launch.json) | `prepared_not_observed`; the second rule is `reachable: false` |
| `product-discovery-validation build` | [`examples/workflow-artifacts/product-discovery-validation-build-semantic.json`](../examples/workflow-artifacts/product-discovery-validation-build-semantic.json) | derived valid package; `evaluate` returns `inconclusive` |
| `product-discovery-validation channel-feedback` | [`examples/workflow-artifacts/product-discovery-validation-channel-feedback.json`](../examples/workflow-artifacts/product-discovery-validation-channel-feedback.json) | `gtm_pivot`; reachability contradicted and solution handoff held |
| `sales-pipeline-review prepare` | [`examples/workflow-artifacts/sales-pipeline-review-prepare-ready.json`](../examples/workflow-artifacts/sales-pipeline-review-prepare-ready.json) | `READY` |

Run them from the repository root:

```sh
uv run python -m omh.cli --omh-home "$OMH_HOME" --hermes-home "$HERMES_HOME" \
  runtime workflow-artifact decision-prototype prepare \
  --input examples/workflow-artifacts/decision-prototype-prepare.json

uv run python -m omh.cli --omh-home "$OMH_HOME" --hermes-home "$HERMES_HOME" \
  runtime workflow-artifact lifecycle-growth build \
  --input examples/workflow-artifacts/lifecycle-growth-build-semantic.json

uv run python -m omh.cli --omh-home "$OMH_HOME" --hermes-home "$HERMES_HOME" \
  runtime workflow-artifact lifecycle-growth audience \
  --input examples/workflow-artifacts/lifecycle-growth-launch.json

uv run python -m omh.cli --omh-home "$OMH_HOME" --hermes-home "$HERMES_HOME" \
  runtime workflow-artifact product-discovery-validation build \
  --input examples/workflow-artifacts/product-discovery-validation-build-semantic.json

uv run python -m omh.cli --omh-home "$OMH_HOME" --hermes-home "$HERMES_HOME" \
  runtime workflow-artifact product-discovery-validation channel-feedback \
  --input examples/workflow-artifacts/product-discovery-validation-channel-feedback.json

uv run python -m omh.cli --omh-home "$OMH_HOME" --hermes-home "$HERMES_HOME" \
  runtime workflow-artifact sales-pipeline-review prepare \
  --input examples/workflow-artifacts/sales-pipeline-review-prepare-ready.json
```

The prototype file declares a bounded planned command line and its expected metadata observation. It contains neither a script body nor a transcript, and the result does not claim that the command ran. The lifecycle build and discovery build files are semantic builder inputs: they contain no schema version, status, claim boundary, or discovery artifact id. The lifecycle launch file is an ordered first-match audience with an unconditional 100 percent catch-all ahead of a narrower paid-plan rule in the same domain; the review marks that later rule unreachable as configuration analysis, never as observed targeting or exposure. The discovery build input deliberately contains only `synthetic` evidence, so evaluating its built package demonstrates `inconclusive`, not customer validation. The separate channel-feedback scenario combines builder-produced frame/GTM/portfolio artifacts with fictional supplied observation metadata: an initial-channel contradiction proposes a GTM pivot and blocks build outputs despite the recruitable segment, while opportunity direction stays supported. Its declared external-human source class is example data, not evidence that OMH contacted anyone.

The CLI wraps each operation payload under `result`. To use a built lifecycle result with `prepare`, pass that `result` object as the next input. To evaluate a built discovery package, pass `{"package": <build result>, "now": "<ISO-8601 timestamp>"}`; `build` cannot mint a decision receipt.

For stdin, use `--input -`; named files and stdin are both bounded. The selected OMH home is the only persistence location used by an explicit producer-owned operation.

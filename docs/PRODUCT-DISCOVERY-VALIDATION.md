# Product discovery validation

Operator/runtime reference: [`docs/WORKFLOW-ARTIFACTS.md`](WORKFLOW-ARTIFACTS.md). Use `omh runtime workflow-artifact product-discovery-validation <operation> --input <json-file-or->`; semantic `build` derives hashes and synthetic discovery remains `inconclusive`.

`omh.workflows.product_discovery_validation` is a local, metadata-only contract for the decision before a PRD. It does not recruit participants, contact customers, replay interviews, create a prototype, write a PRD, or execute an experiment.

## Artifact sequence

1. `discovery_decision_frame/v1` records the problem, segment, `segment_definition_state`, alternatives, owner, budget, deadline, and kill criteria as opaque references.
2. `customer_discovery_plan/v1` requires a human-owned participant handoff, consent/privacy pointer, bias controls, and re-entry. Its fixed interview focus is past behavior, current workaround, switching cost, and observed commitment.
3. `assumption_test_portfolio/v1` ranks value, usability, feasibility, viability, go-to-market, and ethics assumptions by `decision_impact * evidence_gap`. Each test precommits scope, sample, deadline, cost, owner, success/failure/inconclusive criteria, required evidence classes, and its failure decision (`kill` or `pivot`).
4. `discovery_evidence_ledger/v1` accepts only bounded references and labels its source class and limits. A pointer is not a fresh observation.
5. `initial_gtm_hypothesis/v1` keeps the beachhead, buyer/user distinction, alternative, value, pricing, channel, cohort, and learning metrics as hypotheses.
6. `discovery_decision_receipt/v1` records `kill`, `pivot`, `persevere`, or `inconclusive`, the problem gate, the audience state, `solution_work_permitted`, the missing audience evidence, eligible evidence references, rejected hypotheses, residual risks, and next route.

`evaluate_product_discovery()` treats `now` as the evaluation cutoff. It admits evidence only when it is re-entered after a matching precommit, references the matching precommitted success/failure/inconclusive criterion, is captured no later than both the test deadline and `now`, is within the matching scope, representative, externally human or behavioral, required by the test, and bounded in confidence. A completed observation captured within its window remains usable when evaluated later; an expired test with no eligible observation is `inconclusive` and carries `risk-test-deadline-expired`. Future observations or future precommits cannot validate a decision.

Each ledger has unique evidence IDs and one source reference per precommitted test, so a copied observation cannot multiply a sample. Synthetic, inferred, secondary, stakeholder-only, source-pointer, and prototype-completion material cannot satisfy the external customer gate. Any eligible `unresolved` entry tied to the precommitted inconclusive criterion prevents promotion; an eligible contradiction refutes only its own hypothesis and selects that hypothesis's precommitted `kill` or `pivot` decision. Missing, expired, out-of-scope, duplicate, future, or unreentered evidence produces `inconclusive`.

## Audience before build

`segment_definition_state` names the audience explicitly as `recruitable`, `behaviorally_observed`, `unknown`, `synthetic_only`, or `non_recruitable`. The first two are audience-defined; the other three are not.

`discovery_audience_gate(frame)` reads one prepared frame and returns `discovery_audience_gate/v1`. Evidence work always continues: an undefined audience still produces a frame, a customer discovery plan, and an evidence plan. A frame alone never permits solution work, so `solution_work_permitted` is false there; only a validated receipt can permit it. When the audience is undefined the gate lists `product-brief`, `decision-prototype`, and `coding-handoff` in `blocked_outputs`, names the outstanding work in `missing_audience_evidence_refs`, and keeps `next_route` at `product-discovery-validation`.

`build_discovery_decision_receipt()` derives `solution_work_permitted` and `missing_audience_evidence_refs` rather than accepting them, refuses `persevere` for an undefined audience, and refuses any route other than `product-discovery-validation` while solution work is blocked. A tampered receipt that flips either derived field fails validation. `evaluate_product_discovery()` therefore holds an undefined audience at `inconclusive` and adds `risk-audience-undefined`, while a contradiction still refutes its own hypothesis. Because eligible evidence must carry the assumption's `scope_segment_ref`, and that scope must equal the framed segment, evidence observed in one segment cannot validate another; a pivot needs a new decision frame, GTM beachhead, and assumption scope together.

## Channel feedback

Supplied observations and local evaluation are not market execution, customer validation, or product outcome evidence.

The additive `channel_feedback_ledger/v1` binds `discovery_id`, `gtm_artifact_id`, `initial_channel_ref`, and `segment_ref`. Each observation supplies `feedback_id`, `test_id`, `channel_ref`, `criterion_ref`, `segment_ref`, `affected_target`, `effect`, `source_class`, `source_ref`, `observed_at`, `sample_count`, and `confidence_limit`. The builder echoes the ledger's `discovery_id` into every entry; a supplied entry identity must match. Historical channel ledgers without entry identities remain readable with their original digests, while rebuilding produces a new, explicitly bound ledger without rewriting history. References are opaque, not URLs; raw material and extra fields are refused. Feedback IDs and `(test_id, source_ref, affected_target)` tuples must be unique in an appendable ledger.

`affected_target` is explicitly `channel_reachability` or `opportunity_direction`; neither is inferred from notes. Each resolves to `supports`, `contradicts`, or `unknown`. `evaluate_channel_feedback(frame=..., gtm=..., portfolio=..., feedback_ledger=..., now=...)` validates package identities and precommits, then returns a deterministic `channel_feedback_disposition/v1` bound to the frame, GTM, portfolio, submission digest, and evaluation cutoff. It accepts either a validated ledger artifact or the ledger's semantic fields. The fixed precedence is:

1. Inadmissible observations are excluded and recorded in `held_observations`; their affected target becomes `unknown` before any decision effect. The stable reasons are `channel_feedback_missing`, `channel_feedback_stale`, `channel_feedback_duplicate`, `channel_feedback_foreign_segment`, `channel_feedback_wrong_channel`, `channel_feedback_wrong_test`, `channel_feedback_contradictory`, and `channel_feedback_unresolved`. A same-source, same-target support/contradiction conflict receives the specific contradictory reason rather than generic duplication. Observations must lie inside the precommitted window and not after `now`.
2. Contradicted reachability must bind a `go_to_market` test. Its precommitted `pivot` proposes `gtm_pivot`; `kill` proposes `reevaluate_discovery`. Both retain the rejected GTM artifact reference and route back to discovery. Neither changes `problem_gate`: only a separately invoked `evaluate_product_discovery()` against precommitted problem criteria can produce a new receipt.
3. Contradicted opportunity direction independently holds the solution handoff; it does not reject a supported channel.
4. A matching reachability contradiction revises even a recruitable frame's gate to `audience_reachability_contradicted`, blocking all build-boundary outputs. `discovery_audience_gate_with_feedback(frame, disposition)` refuses a disposition bound to a different frame digest. A revised frame must be supplied and evaluated explicitly, never inferred as accepted from new observations or an overwritten artifact.
5. `product_brief_consumption_with_feedback(receipt, disposition)` returns `{}` while either target is unknown or any feedback hold remains. `build_channel_aware_product_brief_handoff(receipt, disposition)` in `omh.workflows.decision_receipt_handoffs` reports `blocked` with `discovery_segment_reachability_contradicted` for matched reachability contradictions, otherwise `discovery_channel_feedback_hold`. A supported companion still cannot promote an unvalidated receipt or another segment's receipt.
6. Unknown effects, insufficient samples, non-external sources, or unbounded confidence remain `inconclusive`, routing to `product-discovery-validation` with a bounded channel retest proposal when no precommitted failure already selected a followup. Unresolved evidence never promotes.

The CLI `channel-feedback` operation takes exactly `frame`, `gtm`, `portfolio` (existing artifacts), `feedback_ledger` (semantic fields), and `now`. It returns `ledger`, `disposition`, and `audience_gate`, without persistence. Incomplete observation fields and duplicate rows are exit-0 holds, not malformed-envelope errors: `ledger` is `{}` when those rows cannot form an appendable ledger, while the disposition preserves refusal references and a deterministic digest of the bounded submission. Unsafe or wrong-type values and malformed envelopes are exit-2 errors. For complete ledgers, `feedback_ledger_ref` equals the returned ledger's artifact ID.

Both companions use the existing explicit `append` operation. Append a new frame or receipt rather than editing prior decisions; rejected hypotheses and unresolved evidence stay visible in the journal. Existing v1 artifacts keep their exact schemas and cannot acquire channel fields. The legacy receipt-only handoff does not load channel history: integrations handling channel observations must use the channel-aware guard with the supplied disposition.

## Product-brief handoff

`product_brief_consumption(receipt)` returns a compact handoff only for a valid `persevere` receipt with `problem_gate == "validated"` and `solution_work_permitted == true`. It contains opaque problem/segment references, the learning boundary, residual risks, and next route; it has no transcript field. Refuted, expired, malformed, or inconclusive records return `{}`.

## Durable re-entry

`append_product_discovery_artifact(paths, artifact)` writes a validated artifact to the existing runtime journal append-only primitive. `read_product_discovery_artifacts(paths, discovery_id=...)` reopens valid artifacts after a restart. The stored values are metadata-only references, not customer contacts, recordings, or raw observations.

## Integration boundary

The executable API is exposed through the agent/operator CLI above; normal users remain chat-first. Channel companions do not change the skill catalog or legacy receipt-only API. A prototype completion remains non-qualifying customer evidence. Product-brief consumes only guarded compact context, and `idea-to-deploy` remains downstream of an accepted product brief and plan.

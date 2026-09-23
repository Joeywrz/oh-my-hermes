# Harness Quality Contract

`harness_quality/v1` is the small machine-readable contract that tells a chat
wrapper what quality gate a workflow lane is using.

It exists so Discord, Slack, or hosted Hermes wrappers can render better UX
without teaching end users command names or parsing generated skill prose.

## What Users Get

For a chat user, this changes the experience from vague status copy to explicit
state:

- "I need one answer before planning" for clarification lanes.
- "A draft plan is ready to accept or revise" for planning lanes.
- "Feedback is clustered, but no roadmap or coding handoff exists yet" for
  customer triage lanes.
- "A meeting agenda is prepared, but the meeting outcome is not observed yet"
  for meeting lanes.
- "An ops review names risks and blockers, but it is not CI or release
  evidence" for operating review lanes.
- "Operating rhythm history is recorded, but unprovided meeting outcomes remain
  unobserved" for cadence lanes.
- "A report package outline is ready, but approval and binary PPTX export are
  not observed yet" for report lanes.
- "A material package plan is ready, but binary export, render QA, formula
  recalculation, approval, delivery, and upload are not observed yet" for
  material lanes.
- "A reliability review is drafted, but SLO, incident, and error-budget claims
  require metric or source evidence" for reliability lanes.
- "A coding handoff is prepared, but execution is not observed yet" for coding
  lanes.
- "Review or CI is still missing" before merge-ready status is shown.
- "This workflow attempt is now a learning trace" when Hermes records the route,
  next action, missing evidence, eval result, and future regression case without
  storing the raw prompt or silently patching a skill.

The wrapper can choose buttons from `wrapper_actions`, show progress from
`evidence_ladder`, and avoid false claims with `overclaim_guards`.
Local operators can inspect the same contract with `omh harness list`,
`omh harness inspect <name>`, and `omh harness validate`.

## Contract Shape

Every generated workflow catalog entry and relevant runtime payload can expose a
contract shaped like this:

```json
{
  "schema_version": "harness_quality/v1",
  "harness": "coding-handling",
  "quality_tier": "handoff-gated",
  "quality_bar": [
    "Clarify scope before edits when target behavior, files, or verification are missing.",
    "Attach acceptance criteria, verification expectations, and review expectations to the prepared handoff.",
    "Report coding progress from lifecycle evidence, not from the existence of a prepared prompt."
  ],
  "evidence_ladder": [
    "coding_delegation_prepared",
    "executor_dispatch_observed",
    "executor_result_observed",
    "verification_recorded",
    "review_ci_merge_recorded_when_required"
  ],
  "wrapper_actions": ["accept_plan", "show_prompt_handoff", "copy_prompt_handoff", "choose_executor", "send_to_executor", "send_to_codex", "show_status", "record_result"],
  "overclaim_guards": [
    "A prepared coding_delegation.json is not implementation evidence.",
    "Executor completion is not review, CI, merge-readiness, or merge evidence."
  ]
}
```

## Where It Appears

- `omh docs workflows --json` exposes the full local workflow and harness
  catalog, including `workflow_catalog/v1.harnesses[].harness_quality`.
- `omh coding delegate` includes `harness_quality` beside the prepared
  delegation. Dispatch actions are removed unless the payload also includes a
  prepared executor handoff.
- `omh coding delegate --executor codex` includes the dispatch-capable contract
  in both the public payload and `executor_handoff` when the request is specific
  enough to delegate. The primary action is `send_to_executor`; `send_to_codex`
  remains a compatibility alias only for Codex-selected flows.
- `omh coding delegate --executor claude-code`, `--executor omx-runtime`, or
  `--executor generic` returns a prompt-only handoff. It can expose
  `show_prompt_handoff`, `copy_prompt_handoff`, and `choose_executor`, but it
  must not create a lifecycle run or observed execution evidence.
- `omh hermes plan` includes `wrapper_contract.harness_quality` so wrappers can
  render accept/revise/cancel and handoff readiness from the plan contract.
- Runtime records preserve the contract in `coding_delegation.json` when present.
- `omh runtime delegation-status --run <run-id>` includes
  `harness_progress/v1`, which marks ladder steps complete only when the
  corresponding runtime or wrapper evidence is observed.
- `omh learning record`, `omh learning eval`, and `omh learning regression
  replay` expose the `workflow-learning` harness for process-supervision data:
  why a workflow was chosen, what was prepared, what was observed, which
  deterministic checks passed, and what improvement candidate still needs human
  approval.
- `omh learning recap build|list|show` projects one stored runtime run into
  `runtime_learning_recap/v1`: seven separate delivery, verification, review,
  pull-request, CI, merge-readiness and merge cells, each naming `observed`,
  `failed`, or `unavailable` with its supporting observation type and bounded
  opaque evidence references. `omh learning record --from-runtime-run` writes
  the trace and the recap together. The operator `--outcome` is labelled
  supplied assessment and never sets or upgrades an evidence cell, so a run
  recorded as `useful` with no eligible terminal observation stays `unknown`.
  See [Runtime Learning Recap](RUNTIME-LEARNING-RECAP.md).
- `omh learning missed-route` is the wrapper-friendly shortcut for "Hermes did
  not use the expected OMH workflow." It records a metadata-only trace, eval,
  regression placeholder, and review candidate in one step. A provided
  `--fixture-message` is operator-minimized replay text; without it, replay is
  skipped until a fixture is added.
- `omh learning index check` and `omh learning index rebuild` keep the local
  workflow-learning index repairable if metadata records exist but the pointer
  index drifts. Rebuilding the index is not workflow execution, skill mutation,
  or proof that a future workflow improved.
- `omh learning audit` reads the local learning corpus and returns
  `workflow_learning_audit/v1`: trace/eval/regression/export counts, coverage,
  stale-index blockers, regression replay status, and the next repair or review
  action. The same payload includes `learning_audit_card/v1` so Hermes wrappers
  can render a compact review card with record, eval, regression, audit, export,
  replay, index-check, and index-rebuild actions. The audit is readiness
  evidence for the learning corpus only; it does not patch skills, execute
  workflows, or prove future behavior improved.
- `omh learning candidate <trace-id>` returns an
  `improvement_candidate/v1` plus `improvement_candidate_review_card/v1`. The
  review card is the Hermes-facing surface for approve/revise/reject decisions,
  regression-case follow-up, and status narration. It is not a source patch,
  automatic skill mutation, or proof that future behavior changed.
- `omh learning review` returns `workflow_learning_review_queue/v1`: the
  local human-review queue for pending candidates, approved candidates that
  still need a patch proposal, proposals that need regression work, and
  proposals ready to copy as patch handoff material. The queue is review
  navigation only; it does not approve candidates, apply source changes, or
  prove future behavior improved.
- `omh learning review-candidate <candidate-id> --decision approve|revise|reject`
  records the human gate decision for an improvement candidate and refreshes
  its review card. Optional review notes are stored only as hash and length, not
  raw text.
- `omh learning proposal <candidate-id>` records an
  `improvement_patch_proposal/v1` snapshot for the candidate's current review
  and regression state. Draft statuses such as `needs_regression_case` remain
  durable learning records; `ready_for_human_patch` requires approval plus
  passing regression replay. The proposal is a non-applying patch handoff for
  human review; it is not a source patch, automatic skill mutation, or proof
  that future behavior changed.
- `omh learning export` creates a redacted `workflow_learning_export/v1` review
  bundle from selected traces plus related evals, candidates, and regression
  cases, and patch proposals. The bundle omits raw prompts and fixture text; it
  is review material, not model training, automatic skill patching, execution,
  review, CI, merge, or proof that future routing improved. Export bundles are
  derived artifacts and are not part of the canonical learning index repair loop.

## Teaching A Proven Workflow (agent-facing)

A user who has just watched a workflow work can ask Hermes to keep it. The
`omh learning skill-draft` group is the deterministic backend for that request;
users express the intent in natural language and Hermes fills the flags.

- `omh learning skill-draft new "<explicit request>" --name <slug> --source-run
  <run-id> --instruction ... --input name=description --precondition ...
  --stop-condition ... --verification ...` returns `skill_draft/v1`. It records
  a draft only when the message carries an explicit learning signal such as
  "turn this into a skill"; passive activity exits non-zero with
  `no_explicit_learning_signal` and writes nothing. The draft keeps the fixed
  instructions separate from the declared inputs, preconditions, stop
  conditions, and verification steps, and its provenance names the
  user-selected source runs, the transient identifiers redacted out of the
  request, and the pending review decision.
- A draft is inactive by construction. It is stored under
  `.omh/learning/skill-drafts/`, is never written under `skills/`, never becomes
  catalog data, and carries its proposed name under `proposed_skill_name` rather
  than `name`. A name that collides with an installed skill fails validation,
  and an inactive draft carries no copy-ready proposal at all.
- `omh learning skill-draft show <draft-id>` returns the draft plus a live
  `skill_draft_generated_output_check/v1`: the draft validates, the catalog
  contract is clean, the draft projects to a `SkillDefinition` the catalog's own
  per-definition validator accepts, and that definition renders a single-line
  frontmatter description and a catalog-index line inside its byte ceiling.
  `activation_blockers` is what a reviewer has to clear.
- `omh learning skill-draft review <draft-id> --decision approve|revise|reject`
  is the only activation path. Approval recomputes the generated-output checks
  and refuses to activate when any fail, so an approval on its own can never
  activate a draft that would not render a valid skill. Only a passing check set
  plus an explicit approval flips the draft to `active_proposal` and mints the
  copy-ready `skill_draft_proposal/v1`. Review notes are stored as hash and
  length, never raw text.
- Generated bytes are candidate material, not a shipped artifact. The words
  below are contract vocabulary, not a description of what ships today: no
  promotion path exists in this harness, so nothing here reads as an available
  command. They are the rules any promotion path would have to satisfy before
  it could be built.
- A candidate is bytes plus a digest. Whatever renders a skill retains the
  rendered bytes and their sha256 digest under the draft, so a reviewer
  decides on the exact bytes a promotion would write rather than on a promise
  to re-render later. Re-rendering to different bytes yields a different
  digest and reopens the decision, and a decision recorded against a stale
  digest is refused instead of carried forward.
- Promotion is review-gated and atomic. Only an approved draft would be
  promotable, and the write would take the retained candidate bytes whose
  digest the review named. Either the approved artifact exists complete at its
  own path or nothing was written: no half-promoted skill, and the candidate
  stays put so a failed promotion loses no review evidence. Rendering,
  approving, and promoting stay three separate observed events, and none of
  them implies the next.
- Even an activated draft is a proposal. It is not an installed skill, not
  catalog data, and not proof that Hermes Agent `/learn`, a maintainer, or any
  runtime acted on it.

## Wrapper Rules

- Use `wrapper_actions` as platform-neutral action ids; map them to buttons,
  menu items, or thread actions in the adapter.
- Use `evidence_ladder` to show progress, but mark a step complete only when a
  runtime record or wrapper observation proves it.
- Use `quality_bar` as the lane's success checklist.
- Use `overclaim_guards` before changing status text. If a guard conflicts with
  a later artifact, show the blocker instead of the optimistic state.
- Treat `harness_progress/v1.next_step` as a wrapper hint, not as proof that the
  next action has already happened.

## Reply Lint

The reply rules -- OMH's record vocabulary stays in records and tool calls,
awareness lines are never quoted, and a stop or a decision the user owns ends
the turn with the next action offered as a question -- ship as prompt text in
every skill's Runtime Evidence tail, the common rail, and the awareness primers.
Nothing observed whether a reply followed them until a person read "this is an
evidence-bounded surface" or "I will not merge here" and said so.

`omh quality-evidence reply-lint` reads the sentence a person read and reports
where it departs from those rules:

| Finding | What it means |
| --- | --- |
| `record_term_leak` | a record term in the reply: the rail's list (`prepared_not_observed`, `not_observed`, `evidence boundary`, `handoff`, the qualified `surface`/`lane`/`wrapper` forms) plus the Korean renderings read in live replies (`표면`, `레인`) |
| `awareness_line_quoted` | an `[OMH Awareness]`, `Boundary:`, or `Route hint:` line quoted into the reply |
| `refusal_closer` | the closing paragraph declares what will not be done and offers no question |
| `decision_without_question` | the closing paragraph names an approval or a decision the user owns and offers no question |

```sh
omh quality-evidence reply-lint --text-file reply.txt [--user-text-file asked.txt]
omh quality-evidence reply-lint --stdin < reply.txt
omh quality-evidence reply-lint --hermes-session latest --last 5 [--json]
```

Only the closing paragraph decides the last two: a refusal in the body
followed by a closing question is the rule's own shape, what was left undone
stated as the option it leaves open. A term the user's own message names is
explained on request, not leaked, so it is carved out and listed rather than
counted. The Hermes source opens `state.db` read-only, pairs each reply with
the user message it answered, and skips the `[PRIOR CONTEXT ...]` rows a
compaction re-injects. A finding exits 1 so a QA loop can gate on it.

The payload is `reply_lint/v1` and carries its own claim boundary: a clean
result shows that the text carries none of these shapes. It does not show that
the reply was correct, complete, or in the host's own voice, and it is not
execution, review, CI, or merge evidence.

## Golden Examples

See `examples/wrapper-golden/harness-quality.json` for deterministic examples
covering coding handoff, planning, research, and clarification lanes.

The business workflow pack adds non-coding harnesses such as
`business-research`, `strategy-synthesis`, `meeting-facilitation`,
`customer-insight-triage`, `ops-review`, `operating-rhythm`, `report-package`,
`materials-package`, and `reliability-review`. These give wrappers the same
evidence discipline for company work: research briefs are not fetched data,
strategy briefs are not accepted decisions, meeting briefs are not meeting
minutes, feedback triage is not a roadmap, ops review is not release or CI
evidence, report packages are not binary decks or approvals, material packages
are not binary exports, render/formula QA, uploads, approvals, or delivery, and
reliability reviews are not proof that SLOs passed without metric or source
evidence.

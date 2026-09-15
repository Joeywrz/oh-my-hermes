# Runtime Learning Recap

`runtime_learning_recap/v1` is one bounded, review-only projection of a single
stored runtime run. It exists to answer one question a workflow learning trace
could not: which completion facts were *observed* for the run, as opposed to
which ones an operator reported.

A recap is learning evidence about what a run's stored observations say. It is
not proof of any stage it reports as unavailable, and it is not an automatic
workflow improvement. Building or reading one creates no skill, writes no
memory, approves no candidate, dispatches no executor, and mutates no pull
request.

## The two authorities

A recap keeps two blocks apart, and neither one writes to the other.

- `operator_assessment` carries `operator_outcome` and
  `operator_feedback_summary`. Both are labelled `operator_supplied`. This is a
  human judgement, useful for learning even when execution evidence is missing.
  It can never set or upgrade an evidence cell or the observed completion state.
- `observed_completion` is labelled `observed_evidence` and is derived only from
  validated `runtime_observation/v1` records bound to this run id. An operator
  outcome of `useful` over a run with no eligible terminal observation leaves
  observed completion at `unknown`.

A prepared handoff, a wrapper summary, and a process exit are none of them
terminal success, so none of them reaches an evidence cell.

## Evidence cells

Seven facts are reported separately, because none of them implies another. A
pull request is not passing CI, passing CI is not a completed review, and a
completed review is not a merge.

| Cell | Supporting observation |
| --- | --- |
| `delivery` | `worker_result` |
| `verification` | `verification` |
| `review` | `review` |
| `pull_request` | the most advanced ladder observation carrying a `pr:` evidence reference |
| `ci` | `ci` |
| `merge_readiness` | `merge_readiness` |
| `merge` | `merge` |

Each cell names one of `observed`, `failed`, or `unavailable`, plus the
observation type that supports it, the raw observation status it came from, and
bounded opaque evidence references. A cell with no eligible observation is
`unavailable` — never an empty success. A `blocked` or `cancelled` observation
leaves the cell `unavailable` rather than `failed`, because a block is
recoverable and a cancellation is not a fault of that stage; the raw status
stays on the cell so nothing is lost by the narrowing.

Executors differ in what they record. A stage an executor never observes stays
unavailable, which is the executor-neutral answer.

## Run-level observations

Three observation event types are not milestones and reach no cell: `failed`
and `cancelled`, which state that the run ended, and `blocked`, which does not.
They appear under `run_lifecycle`, in the same shape a cell uses.
`run_lifecycle.termination` holds one view per terminal event type plus a
derived `kind`.

Which terminal event a run recorded is read from the presence of the record and
its event type, never from the cell-state narrowing. That narrowing maps a
`cancelled` status to `unavailable`, which is right for a stage — a cancellation
proves nothing about that stage — and wrong for the run. A terminal event
recorded `not_observed` terminates nothing, because that status says the event
was not seen.

`kind` is decided by severity, not arrival: a recorded terminal failure is not
undone by a cancellation appended after it, since the two are different facts
about the run rather than two reports of one.

A terminal observation moves observed completion. A block never does — a block
is recoverable by definition, so it is reported and changes no state, which is
the same reasoning the runtime status projection uses.

## Observed completion

`observed_completion.state` is one of four closed values, derived from the cells
and the terminal observation, in this precedence.

- `failed` — a cell reports an observed failure, or the run recorded a terminal
  `failed` observation.
- `partial` or `unknown` — the run recorded a terminal `cancelled` observation.
  Checked before `completed`, so a cancelled run can never read as completed. It
  is not reported as a failure: a cancellation is not a fault of any stage.
- `completed` — the merge cell is observed and nothing above applies.
- `partial` — at least one cell is observed, and merge is not.
- `unknown` — nothing observed, nothing failed.

`observed_completion.run_termination` names which terminal event the run
recorded, or `none`.

## Identity fields

Commit identity, changed-path summaries, pull-request identity and merge
identity appear only when the observation that owns the corresponding cell
carries the matching typed evidence reference. The vocabulary is closed:

| Field | Prefix | Eligible source |
| --- | --- | --- |
| `commit` | `commit:` | the delivery observation |
| `changed_paths` | `changed_path:` | the delivery observation |
| `pull_request` | `pr:` | the observation behind the pull-request cell |
| `merge_commit` | `merge_commit:` | the merge observation |

Anything else stays `unavailable` with the reason recorded.

## Bounds and redaction

A persisted recap carries no raw prompt, transcript, command, tool argument or
result, credential, arbitrary log, or absolute filesystem path.

- Every evidence reference is folded into a bounded, non-navigable handle. A
  safe opaque identifier passes through as itself; a URL, a credential shape, an
  absolute path, or anything longer than a bounded identifier becomes a stable
  `ref-` digest.
- Changed paths are repository-relative only. A value that is not one is dropped
  and counted rather than digested, because a digest in a path list reads like a
  path and is not one.
- The recap summary is assembled from closed states and the run id. No
  observation summary is quoted into it.
- Operator feedback is bounded, and is replaced by `[redacted]` when it carries
  a link, an absolute path, a body, or a credential shape.

## Revisions

A recap is bound to its run id plus a deterministic digest of the runtime
observation history it was built from. The recap id follows from that pair, so:

- Re-recording the same run at the same runtime history revision is idempotent.
  The record carries no wall clock, so it rebuilds byte-identically.
- A later observation changes the digest, which mints a new recap id and a new
  stored revision beside the earlier one. An already-reviewed recap is never
  rewritten underneath its reviewer.

Validation recomputes rather than reads. Each cell's state must follow from the
observation status stored beside it, through the same total map the builder
used, and each `reason` must be one the projection produces — so a stored cell
cannot claim `observed` next to a status of `failed`, and cannot carry prose.
The completion block is then recomputed from the cells and the terminal
observation, field by field. This matters because the recap id binds the
observation history, not the projection derived from it: a hand edit to the
cells leaves the id unchanged, so the cells have to be checkable from what they
carry.

Validation also refuses a foreign run id, a recap id that does not bind its run
and revision, a malformed closed state, oversized strings or arrays, and an
unsupported schema version. Diagnostics are bounded.

## Commands

```sh
omh learning recap build <run-id> [--outcome unknown|useful|not_useful|blocked|failed] \
  [--feedback-summary "<bounded assessment>"] [--dry-run]
omh learning recap list [--run-id <run-id>] [--limit N]
omh learning recap show <recap-id>
```

`omh learning record --from-runtime-run <run-id>` writes the trace and the recap
for the current revision together, from one read of the run, and returns both.
The trace carries `runtime_completion`, which names the recap ref, the runtime
history digest, and the closed cell states — states only, never evidence of its
own. The trace's `status` block labels `outcome` as `operator_supplied` beside
`observed_completion` labelled `observed_evidence`, and learning list, eval,
candidate, and review-queue surfaces carry that same pair forward.

The trace's `status.feedback_summary_authority` is derived from where the string
came from, not asserted beside it: `operator_supplied` when the operator passed
one, `prepared_wrapper_summary` when the builder fell back to the wrapper
summary, `absent` when there is none. A prepared wrapper summary is
`prepared_not_observed` and nobody's assessment, so it is never labelled as the
operator's. The recap's own operator block takes only what the operator
supplied, and stays empty otherwise.

`omh learning export` carries both sides. The trace projection includes the
completion keys and a `runtime_completion` block, and the candidate projection
includes `completion_evidence`; both are closed vocabularies, which is the rule
that projection already followed. The recap reference travels hashed as
`recap_ref_sha256`, matching every other reference in the bundle.

Recaps live under `<omh home>/learning/recaps/` and are deliberately outside the
learning index: an index entry implies one current record per identity, and a
recap's revisions are all current.

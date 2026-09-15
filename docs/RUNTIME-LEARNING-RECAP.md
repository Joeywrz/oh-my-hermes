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

## Observed completion

`observed_completion.state` is one of four closed values, derived from the cells
and nothing else.

- `failed` — at least one cell reports an observed failure.
- `completed` — the merge cell is observed and no cell failed.
- `partial` — at least one cell is observed, and merge is not.
- `unknown` — no cell is observed or failed.

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

Validation refuses a foreign run id, a recap id that does not bind its run and
revision, a completion summary that disagrees with its own cells, a malformed
closed state, oversized strings or arrays, and an unsupported schema version.
Diagnostics are bounded.

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

Recaps live under `<omh home>/learning/recaps/` and are deliberately outside the
learning index: an index entry implies one current record per identity, and a
recap's revisions are all current.

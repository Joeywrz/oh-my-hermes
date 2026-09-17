# Agent board and native Kanban coordination

Normal users ask Hermes in chat to coordinate durable work across profiles or
agents; the `agent-board` workflow skill routes that request. This page is the
agent/operator reference for what OMH does underneath (issue #1417). Nothing
here runs on its own, and nothing here grants a native permission.

## Two coordination routes

OMH decides between two Hermes-native surfaces from the request itself:

| Coordination | Route | Native surface | When |
| --- | --- | --- | --- |
| `durable` | `kanban` | `kanban_*` tools on a named board | Work must survive a restart, cross profiles, or be picked up by another worker. |
| `bounded_research` | `delegation` | `delegate_task` with a finite task list | A parent needs a bounded child research result and nothing durable. |

The choice is deterministic. Neither route substitutes for the other when its
surface is missing: an unavailable board is reported as unavailable, not
downgraded to delegation, and a delegated child summary is never projected as
a Kanban fact.

## The `omh_agent_board` plugin tool

The installed OMH plugin registers one tool, `omh_agent_board`, with two closed
actions.

- `prepare` validates a closed JSON request (`request_id`, `coordination`,
  `operation`, `board`, `profile`, `arguments`, optional `task_id` and
  `expected_observation_ref`) and returns an `agent_board_request/v1` record.
  When the host exposes the needed capability the record carries one
  `native_action`: the exact `kanban_<operation>` or `delegate_task` tool name
  and its arguments. Hermes invokes that action through its normal tool loop.
  OMH never calls a native tool from the handler.
- `status` returns the bounded projection for one `request_id`, including any
  observed receipt. It carries no action text, body, summary, or native output.

Supported durable operations are `create`, `link`, `comment`, `heartbeat`,
`request_review`, `request_changes`, `block`, `unblock`, `complete`, `show`,
`list`, and `attachments`. Arguments are closed per operation: unknown keys,
a foreign `board`, a `task_id` that differs from the request, a self-link, a
completion without summary or result, an unknown block kind, and an
`idempotency_key` that differs from the `request_id` are all rejected as
invalid input. `create` always freezes `idempotency_key` to the `request_id`.

The board engine itself -- admission, receipts and board references -- lives in
the `omh` package, not in the plugin bundle, so the feature needs that package
to be importable by the interpreter running Hermes. The documented installs
put it in OMH's own environment, so on many hosts it is not. Such a host has
no board feature and says so: `omh_agent_board` answers `unavailable` with
reason `omh_agent_board_core_unavailable`, and the `pre_tool_call` /
`post_tool_call` correlation stands down rather than refusing native `kanban_*`
calls it has no prepared request for. What it must never do is fail: until
#1623 the bundle module holding that bridge could not be imported at all
without the package, and Hermes' memory-provider loader caches a module that
raised, which turned an absent feature into a hook warning on every tool call.

That expectation, what each affected feature does without the package, and how
to make `omh` importable from Hermes' own environment are written up once in
[What the Hermes interpreter must be able to import](INSTALLATION.md#what-the-hermes-interpreter-must-be-able-to-import).
`omh doctor` reads the installed bundle for the unguarded shape and reports it
as `plugin_bundle_standalone_imports` (#1670).

## Request states

Every request is exactly one of these states. They never collapse into each
other, and a later state never rewrites an earlier receipt.

| State | Meaning |
| --- | --- |
| `prepared` | Closed input validated and a native action returned; not invoked. |
| `unavailable` | A required capability is missing, or the board's bounded request store is full (`request_capacity`). `missing_capabilities` names the gap: the tool itself, `schema:<tool>`, `pre_tool_call`, `post_tool_call`, `host_identity`, `board_binding`, `native_compare_and_swap`, or `operation:<name>`. Zero native calls happen. |
| `denied` | `request_digest_changed`, `foreign_host_scope`, `stale_observation`, or `reconciliation_required`. Nothing is invoked. |
| `observed` | The paired post callback returned a well-formed, correctly bound native result; the receipt records only the landed facts. |
| `failed` | The paired callback returned an error, a malformed result, a foreign task or edge, changed arguments, or a changed board binding. `requires_reconciliation` is set; a later observed `show` on the same task reconciles it. |

A prepared or unavailable record is a card, not a task. A failed record is not
a landed task either, even though the native call may have had effects.

## Correlation in the normal Hermes loop

OMH observes native calls only through the plugin's `pre_tool_call` and
`post_tool_call` hooks, keyed by the host's own session, task, and tool-call
identity.

1. The `pre_tool_call` for `omh_agent_board` arms the handler with the host
   identity and an argument digest; the handler consumes it exactly once. IDs
   supplied inside the JSON are never trusted as identity.
2. When Hermes later invokes a `kanban_*` tool whose board and frozen argument
   digest match a prepared request from the same host scope, the pre hook
   reserves that single request. If the schema, hooks, or board binding changed
   in between, the hook returns a block directive and nothing is reserved.
3. The matching `post_tool_call` observes the result. Required success keys,
   `ok`, board and task binding, edge identity, list counts, landed status,
   run IDs, and attachment metadata are checked; raw result bodies, comments,
   events, worker context, and attachment names or URLs are not retained.

Unrelated native Kanban use by the user or by Hermes is left alone: the hook
only acts on calls that match a prepared OMH request.

## Idempotent creation and restart

Repeating a `create` with the same `request_id` returns the same request. A
completed create is deduplicated by readback: a second caller in a new profile
or after a restart receives the exact native task ID that was observed, not a
second task. A changed request body under the same `request_id` is denied with
`request_digest_changed`.

OMH-origin processes sharing the same OMH home and effective board identity
serialize creates through a short OS file lock around load, check, reserve, and
save. The lock never spans a native call. An in-flight marker persists before
native admission, so a process that dies mid-call leaves an ambiguous request
that is never retried automatically; an observed `show` reconciles it.

Board state lives under `<omh-home>/runtime/agent-board/<board-ref>.json` as a
bounded `agent_board_state/v1` snapshot: request metadata, digests, receipts,
and task references only. The store refuses symlinks, hard links, oversized
files, and a native database override (`HERMES_KANBAN_DB`), because the board
binding is derived from the host's path identity and must stay attributable.

## Separate lifecycle evidence

Each observed operation records only its own fact: `create`, `link`,
`comment`, `heartbeat`, `block`, `unblock`, `review_requested`,
`changes_requested`, `complete`, `show`, `list`, and `attachments`. A
`running` status seen on readback means the host reports a claimed run; it is
not dispatch proof. `complete` proves the completion call landed; it implies
no review approval, CI result, or merge.

## Native boundaries (observed on the reviewed host)

These are host facts recorded as data, not OMH failures.

- There is no native `kanban_dispatch` tool. Dispatch is `unavailable`; an
  operator claim is the observed path to a running task.
- Native tools expose no compare-and-swap. A request that supplies
  `expected_revision` or `expected_run_id` is `unavailable` with
  `native_compare_and_swap`. Use `expected_observation_ref` to bind a request
  to OMH's last local observation instead.
- A positive `request_changes` needs a review-claimed run from the host's own
  review dispatcher. A headless host with review dispatch disabled exercises
  only the denial path; that is recorded as a native boundary.
- Worker-owned mutations run under the host's worker environment scope. The
  hook never widens that scope.

Fixture hosts used in tests supply schemas and results explicitly and are
labeled as fixtures. They prove correlation, refusal, and persistence
behavior; they are not native execution evidence.

## Native, fixture, and unsupported boundaries

| Surface | Provenance | What it proves | What it does not prove |
| --- | --- | --- | --- |
| Real Hermes host, normal tool loop | native | The prepared `kanban_*` action was invoked by Hermes, the paired hooks correlated it, and the receipt records the landed identities read back from the host. | Review approval, CI, merge, or that a `running` task was dispatched by anything other than an operator claim. |
| Fixture host in tests (`tests/five_issue_cases/kanban.py`) | fixture | Correlation, digest freezing, refusal, persistence, restart, and idempotency behavior of the bridge and board, with zero native calls. | Native Hermes execution or authorization. Results are labeled `fixture` and never counted as native success. |
| `kanban_dispatch`, native compare-and-swap, positive `request_changes` on a headless host | unsupported or native boundary | The request is reported `unavailable` with the named capability, or the boundary is recorded as data in the surface record. | Nothing; OMH never substitutes another route or invents the missing verdict. |

## Wrapper surface

Chat wrappers reach the board through three deterministic projections in
`omh.wrapper.contract`. None of them invokes a native tool, and native action
arguments never enter wrapper metadata.

- The routed `agent-board` chat card (`build_chat_interaction_payload`, or
  `omh chat interact` for operators) carries three actions:
  `prepare_agent_board_card` binds `omh_agent_board` with `action: prepare`,
  the six required request inputs, the two coordination options, and
  `execution_policy: prepare_only`; `refresh_status` binds `action: status`
  with `request_id` as its required input and `execution_policy: read_only`;
  `show_status` points at the read-only fanout status reader described in
  [`docs/FANOUT-EXECUTOR-EVIDENCE.md`](FANOUT-EXECUTOR-EVIDENCE.md).
- `build_agent_board_status_interaction(request, source=...)` renders one
  validated `agent_board_request/v1` prepare or status record as a status
  card. The projection blanks `native_action`, lists `missing_capabilities`
  and each observed receipt's operation, state, and task, and keeps
  `execution_observed: false` with review, CI, and merge `not_observed`. Its
  only action is `refresh_status`.
- `build_fanout_status_interaction(paths, fanout_id=..., unit_id=None)` is
  the reader behind `show_status`; it adds a copy-only resume action only for
  an explicitly selected unit.

[`examples/agent-board/native-actions.json`](../examples/agent-board/native-actions.json)
is the committed projection of those APIs on fixed inputs: the chat card for
one durable request, the prepared `kanban_create` action with its frozen
`idempotency_key`, and the status interaction for that same record. The
example is fixture-attributed (`provenance.kind: fixture`, zero native calls)
and is regenerated, never hand-edited.

## Generated projections

The `agent-board` skill body is generated from
`src/skills/catalog_feature_surfaces.py`, and the wrapper card `agent_board/v1`
stays metadata-only. `tests/test_agent_board_kanban.py` compares the shipped
skill, reference, workflow, and capability-family projections byte for byte
against their renderers, and compares the native-actions example by parsed
equality against the public APIs plus one real `omh chat interact` run.
No separate `references/native-kanban.md` ships under the skill: the
integrated boundaries live in the skill body's checklist and recovery notes,
and this page is the operator reference.

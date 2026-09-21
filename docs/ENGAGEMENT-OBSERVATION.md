# Engagement observation

Audience: plugin maintainers and integration-test authors. This describes local
hook evidence, not model quality or live Hermes certification.

## Observation and presentation

`post_tool_call` observes outcomes even when a block or exception bypasses result
transforms. `transform_tool_result` feeds the same observer because Hermes' outer
executor can suppress the inner post hook and emit the terminal event *after*
transforming. Both orders correlate by resolved OMH home, session, turn, call ID
and underlying tool name. Concurrent callbacks share a lock. Bridge calls use the
underlying name supplied by Hermes; OMH does not re-interpret model tool arguments
as permission to dispatch another tool.

The correlation cache holds at most 4,096 calls. It contains only IDs, outcome
classes, counts and candidate flags, never arguments/results. Missing call IDs
are declined as `unknown_call_identity`; eviction or restart loses correlation
for old IDs. Work counters remain process-local as before. The durable hint
budgets retain their existing bounded-session store. This is not an exactly-once
journal across restarts or indefinitely delayed duplicate events.

`plan_nudges` and `delegation_nudges` budget **candidates**, not acknowledged
messages. Hermes chooses the first string returned by a result-transform listener.
Another listener may win even when OMH's callback ran. Neither a budget increment
nor a callback receipt proves that OMH's text reached a model. The host's
first-string contract is unchanged.

## Evidence classes

- `write_file`: a non-error `bytes_written` integer (including zero for an empty
  file) supports a landed write. A generic error does not prove no effect;
  `WriteResult` includes a default zero even when it refused before writing.
- `patch`: `success: true` supports a landed patch unless `no_change: true`.
- Blocked calls and explicit no-change results have no counted mutation.
- Errors accompanied by positive written bytes or nonempty `files_modified`,
  `files_created`, or `files_deleted` are counted separately as
  `partial_file_mutations`, never as completed writes.
- Results without effect evidence increment `unknown_file_mutations`. A
  post-write exception can discard the tool's effect fields; OMH does not inspect
  filesystem paths or infer effects from prose to fill that gap.
- Only landed mutations advance `file_mutations` and its plan-hint threshold.
- Direct reads measure distinct attempted work, not successful retrieval. A
  blocked call is excluded. Identical reads cannot spend another hint after the
  threshold. Read identity still uses the existing bounded argument digest;
  long-prefix identity collisions are a separate issue, not fixed here.
- Successful `omh_delegate_route` set/fallback results increment `route_prepared`.
  Status, clear and errors do not establish a worker. The original tool result
  remains visible and still labels preparation.
- Only `subagent_start` latches `lane_started` on the parent and marks the actual
  child session as delegated. Hermes emits it after child construction and
  attachment, possibly before the child leaves a queue. It is not model execution,
  successful completion, or completion of the parent's whole objective.

## Lifecycle and compatibility

Homes scope both memory and disk. Children do not receive parent orchestration
hints and cannot consume the parent's budgets. Multiple children do not collapse
into a parent tool-call ID. Turn finalization does not erase engagement state or
claim the objective has ended; native background delegation may yield normally.

Old `lane_routed` latches are ignored: older versions set them on status queries
and failures, so they cannot establish a child. Existing spent budgets and plan
latches are retained. The new `lane_started` latch remains session-lifetime;
objective-level re-arming is intentionally outside this fix.

Observation faults report only their exception class through
`engagement_nudge_declines()` and do not prevent the other post-tool observers
from running. No profile edits, new scheduler, new runtime, network calls, model
runs or changes to Hermes approvals are introduced.

## Reproducing the host boundary checks

The normal OMH unittest suite includes `test_engagement_outcomes.py` and the
updated adjacent suites. The optional cross-repository fixture is
`tests/host_fixtures/engagement_outcomes.py`; it is deliberately not a unittest
module and introduces no runtime dependency on Hermes.

In an isolated complete Hermes checkout, copy that fixture to
`tests/agent/test_omh_engagement_integration.py` (do not overwrite an existing
file). Then use Hermes' canonical runner:

```sh
HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh \
  tests/agent/test_omh_engagement_integration.py -q --tb=short \
  -o omh_checkout=/absolute/path/to/isolated/omh
```

`-o omh_checkout=...` is a fixture-only dependency selector, read from the
forwarded pytest options. Pytest reports it as an unknown ini option; no host
configuration is changed. An environment override is not suitable because the
canonical runner deliberately strips caller environment. Without the option,
the fixture expects a sibling checkout named `omh-engagement-outcomes`.

The fixture registers real selected PluginContext callbacks and runs real
registry/bridge/sequential/inline paths, file operations and child construction.
It denies socket/model calls, substitutes the child's conversation and metadata
I/O, and does not certify plugin installation, async gateway delivery, or live
models. Profile checks bind real temporary A/B/A native scopes. Post-write
bookkeeping exceptions prove that an unknown effect may accompany a real write.

At Hermes `bfcd2099064715edf81786fb189d58e4f0cad176`, an inline child-constructor
exception escapes without post/transform/start callbacks. The test characterizes
that host gap and checks that OMH does not invent a started child. It does not
pretend OMH recorded the missing terminal event. Fixing that host exception path
would be a separate Hermes change; this plugin does not patch the host.

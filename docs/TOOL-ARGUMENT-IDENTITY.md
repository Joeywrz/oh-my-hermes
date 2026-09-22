# Complete tool argument identity

Audience: plugin maintainers and integration-test authors. This is the argument
identity contract for repeat detection, approval keys and distinct engagement
reads, not a new permission system.

## Full or unknown

`tool_args_digest` returns `v2:` plus a 128-bit BLAKE2b fingerprint of the **entire**
canonical JSON value, or an empty string for unknown identity. It never returns a
fingerprint of a truncated prefix. Dictionary order is canonicalized; array order,
JSON types and Unicode code points remain meaningful. Unicode is JSON-escaped,
not replaced or normalized. Plain tuples share JSON-array semantics with lists.

Only plain built-in JSON-shaped values and string dictionary keys are admitted.
Custom classes, container subclasses, `Path` objects, bytes, nonfinite floats,
invalid Unicode strings (including surrogate code points) and cyclic structures
are unknown; arbitrary `default=str` conversions are no longer
executed. Native JSON tool arguments require no such conversion. Surrogate pairs
are rejected too: escaping them would otherwise alias a genuine Unicode scalar
although the original Python strings have different encoding behavior.

## Work bounds

Before serialization, a bounded snapshot checks the input and copies only plain
containers; immutable scalar values are shared. Expanded node visits, not unique
object IDs, are charged, so a repeated-reference DAG cannot bypass the bound.
Depth also terminates cycles. Oversized strings and wide containers can be
rejected before traversal or encoding.

The current limits in `tool_bursts.py` are:

- 1 MiB of complete canonical JSON, never a 1 MiB prefix;
- 4,096 expanded nodes, including object keys;
- nesting depth 64;
- integer size 4,096 bits;
- a conservative pre-encoding character/scalar budget.

The pre-encoding budget limits worst-case JSON string escaping to twelve times
its admitted character count, plus bounded container overhead. Encoded bytes are
allocated only after the complete ASCII JSON length has passed the byte limit.
There is no wall-clock deadline or claim that every sub-limit byte payload must
be admitted: any additional resource limit may yield unknown identity.

## Consumer behavior

- The repeat gate does not refuse or escalate a call with unknown identity.
  Its dispatch also breaks that session's previous repeat continuity, since
  removing an unknown call from history would falsely join separated loops.
  Existing user-authored rules and native Hermes approval/security checks are
  unchanged; unknown identity does not grant execution permission.
- Distinct engagement reads skip unknown identity rather than storing a shared
  `tool:` placeholder. The raw attempted-read count remains separate.
- Pre- and post-tool hooks use the same canonicalizer, preserving argument/result
  correlation. Changing observed results continues to use the existing result
  comparison; this patch does not reinterpret result text.
- The ledger and approval keys retain fingerprints only, never raw arguments,
  destinations, contents or stringification output.

## Compatibility

The versioned namespace intentionally invalidates matching against old,
unversioned prefix-derived identities, including persistent repeat-approval keys.
An old "always" answer must not become authorization for a newly claimed complete
identity. Nothing rewrites the host's saved approval records or grants wider
permission; a new matching key may require a new answer. Recent legacy history
cannot equal a new call's argument identity.

This does not change the weaker result-digest contract: results still use their
bounded prefix plus length, and same-length suffix-only result changes can remain
indistinguishable. Objective-level engagement re-arming and transformer selection
are also outside this correction.

## Verification

`tests/test_tool_argument_identity.py` exercises the public pre/post/transform
hooks alongside canonicalization, limits, Unicode, unsupported objects, approval
key separation, changing results and on-disk privacy. The prior deliberate
prefix-collision test in `test_repeat_call_breaker.py` is explicitly replaced with
full-or-unknown boundary assertions; it is not silently removed.

```sh
PYTHONPATH=tests uv run python -m unittest \
  tests/test_tool_argument_identity.py tests/test_repeat_call_breaker.py \
  tests/test_engagement_nudges.py tests/test_tool_bursts.py -v
```

Native integration is reproducible from the optional
`tests/host_fixtures/tool_identity.py`. Copy it into an isolated complete Hermes
checkout as `tests/agent/test_omh_tool_identity_integration.py`, without overwriting
an existing file, and use the canonical host runner:

```sh
HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh \
  tests/agent/test_omh_tool_identity_integration.py -v --tb=short \
  -o omh_checkout=/absolute/path/to/omh-checkout
```

The forwarded `-o` value selects the actual source under test; a caller environment
variable would be stripped by the host runner. It is a fixture-only selector and
pytest may warn that this custom ini option is not registered. Without it, the
fixture expects a sibling checkout named `omh-tool-identity`.

The fixture uses real plugin registration, dispatch/bridge, file handlers and
native approval persistence. Only the human response boundary is simulated, and
network access is denied. It tests old-grant isolation, new-grant disk reload and
session reuse, and separation between long arguments differing only at the end.
Tuple-to-array semantics are an explicit positive contract, not an unknown-domain
case; malformed Unicode is explicitly unknown. The result-suffix limitation is
characterized as existing behavior, not counted as a fix to result identity.

Host integration and timing/memory measurements do not establish live-model
behavior, full installer/discovery admission or remote CI status.

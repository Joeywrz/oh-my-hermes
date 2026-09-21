# Kanban readback ceiling

Audience: plugin maintainers and native-host integration authors.

## Contract

The OMH transform for `kanban_show`, `kanban_list` and `kanban_attachments`
returns a label followed by one valid JSON object. The **entire returned string**
fits 24,000 characters, including that label, escaping and `omh_readback` metadata.
The field ceiling and row-drop reserve are heuristics, not the final-size proof.
Character counts do not establish UTF-8 bytes, tokens or peak memory.

Ordinary readbacks retain their existing shape and clipping behavior. If the
fully rendered output still does not fit (for example, control-character-heavy
fields or a large future extension), a conservative final projection keeps:

- task identity/status and a small text preview;
- the latest run's identity, outcome/status, profile/session and timestamps;
- bounded task/attachment listings when rows remain after the ordinary reductions;
- bounded root status/error/count metadata.

`omh_readback.projection = "core_fields_only"`, `truncated = true` and the label
explicitly disclose this reduction. Unlisted fields, worker context, older runs,
comments, events and relationships may be omitted. Row-drop counters include both
stages; host-supplied total/count/truncated values remain host metadata rather than
being silently rewritten as returned-row counts. Consumers must also read OMH's
own truncation metadata.

Core scalar fields admit at most 256 serialized characters. Oversized IDs, paths
or states are **omitted and named in `omitted_fields`**, never clipped into another
identity. Ordinary native identifiers/states fit intact. Preview fields can be
shortened and are listed in `truncated_fields`; other omitted record fields are
counted. List projections share a 12,000-character serialized row budget. These
fixed bounds leave headroom for root fields, labels and annotation metadata.

The latest run's `completed` outcome remains **reported done**, never verified.
Reducing the payload does not turn a worker's claim into test, review or CI evidence.

## Cost and boundaries

The normal path measures the payload once and each discarded row once. Prefix
rows are removed with a single slice, not repeated shifting with `pop(0)`.
The fallback adds one bounded projection serialization, not a repeated whole-
payload trim-and-reserialize loop. Parsing and the initial size measurement still
scale with input size; this correction is not a parser memory sandbox.

Foreign tool names and malformed/non-object JSON retain the fail-open behavior.
A within-budget pre-annotated object is left alone. An oversized pre-annotated
object is processed again using its actual task/run fields, not a trusted prior
confidence label. Reapplying the transform to its own label-plus-JSON output
returns `None`, preserving host-level idempotency.

OMH's own composed transform invokes this bound and excludes all three Kanban
readback tools from subsequent diff padding, including on a second pass. Unicode
line separators inside JSON strings must remain data: `splitlines()` plus diff
padding would otherwise replace them with literal newlines and invalidate both
JSON and the ceiling. Other plugins are a separate
host boundary: Hermes selects the first transformer returning a string rather
than composing all plugins as a pipeline. A preceding plugin can therefore
prevent this candidate from being delivered. This patch does not modify Hermes
arbitration or claim a universal cap on every plugin's output.

## Reproduction

```sh
PYTHONPATH=tests uv run python -m unittest \
  tests/test_kanban_readback.py tests/test_kanban_readback_ceiling.py -v
```

The tests cover control-character/quote/backslash escaping, Unicode, final-label
and metadata overhead, large unlisted fields, exact row counters, unchanged small
payloads, latest-run identity/status, disclosure, idempotency, foreign tools and
constant whole-payload serialization count.

For native integration, copy the optional `tests/host_fixtures/kanban_ceiling.py`
into an isolated complete Hermes checkout as
`tests/agent/test_omh_kanban_ceiling_integration.py` without overwriting an existing
file, then run its canonical runner in a credential-free environment:

```sh
scripts/run_tests.sh tests/agent/test_omh_kanban_ceiling_integration.py \
  -j 1 --file-retries 0 --file-timeout 180 -- -v --tb=short \
  -o omh_checkout=/absolute/path/to/omh-checkout
```

The fixture asserts the actual loaded source path; the custom `-o` selector can
produce an unknown-ini-option warning. It exercises native SQLite/`kanban_show`,
real lifecycle registration, direct registry/bridge dispatch and first-string
arbitration between two plugins. Unknown-root/list/attachment probes explicitly
substitute only handler results. It forbids network/model construction, does not
start a dispatcher and does not exercise installer discovery or a full AIAgent
conversation. These native results and full-suite outcomes remain separate
receipts, not inferred from component tests or preparation.

# Declared Plugin Hook Contract

`omh ops plugin-risk-audit --path <plugin-dir>` reads one local plugin
directory and reports, among its aggregate risk categories, what every hook the
plugin's `plugin.yaml` declares would do on the supported Hermes contract. It
answers one question an operator has before enabling an unfamiliar plugin: can a
declared hook block an action, or can it only watch one.

The same result's self-update finding is a separate page:
[Plugin Self-Update and Code Replacement](PLUGIN-SELF-UPDATE-AUDIT.md).

## What this is not

It is an advisory static risk contract, not proof that a plugin is safe.

The audit imports nothing, registers nothing, installs nothing, runs no
subprocess and opens no socket. That is what keeps it usable on a plugin you do
not trust, and it is also its limit. A declaration is author-supplied metadata:
Hermes reads `provides_hooks` into its manifest object, and `hermes plugins
doctor` validates the same list, but registration happens in plugin code through
`ctx.register_hook()`. So a classified hook here is never evidence that the hook
registered, ran, stayed inside its timeout budget, or handled a failure
correctly, and an absent declaration is never evidence that a plugin registers no
hooks. The audit marks each of those as `not_observed` and keeps saying so.

Hermes remains authoritative for loading, registration, dispatch, timeout
enforcement and permissions.

## The vocabulary

Every declared hook gets three closed values plus the field it was declared in.

`effect` — what the host does with the callback's return:

| Value | Meaning |
| --- | --- |
| `policy_gate` | The consumed return can stop, refuse or redirect the action. `pre_tool_call` blocks or approves a tool; `pre_verify` blocks a stop; `pre_gateway_dispatch` skips or rewrites an inbound message. |
| `prompt_context_contributor` | The return is consumed, but only to add text. `pre_llm_call` returns are joined into the user message; it cannot refuse the call. |
| `result_transformer` | The first valid return replaces a value the host is about to use. |
| `lifecycle_callback` | Return ignored. Fires at the creation, teardown, reset or terminal transition of a session, subagent, kanban task, kanban worker, or a skill's usage state. |
| `observer` | Return ignored. Reports an operation or a field change inside a live object's lifetime. |
| `unknown` | The name is not in the supported host contract, the plugin asks for a host revision outside it, or the declaration could not be read. |

`host_contract` — where and how the callback runs:

| Value | Meaning |
| --- | --- |
| `bounded` | Runs under the host's `plugins.hook_callback_timeout` allowlist. A hung callback is abandoned so the agent loop continues. |
| `caller_thread` | Runs synchronously to completion on the calling thread, with no host timeout. A hung callback holds that path. |
| `queued_worker` | Never runs inline. The host hands the event to a per-callback bounded queue with its own worker, and drops that callback's oldest events when it stalls. |
| `unknown` | As above. |

`timeout_semantics` — what a timeout does, which is the distinction the aggregate
category used to hide:

| Value | Meaning |
| --- | --- |
| `fail_closed` | A timed-out or still-running callback becomes a block. `pre_tool_call` is the only hook the host treats this way: a hung policy callback stops the tool rather than letting it run undecided. |
| `fail_open` | A timed-out callback is skipped and the agent continues. A `pre_verify` gate is bounded and fail-open, so a hung gate lets the turn finish. |
| `not_applicable` | The hook is outside the host's timeout allowlist, so no host timeout applies. Naming a fail mode here would invent one. |

## Reading the result

The `declared_hooks` block carries `declaration_status` (`absent`, `declared`,
`invalid`), `classification_status` (`classified`, `unknown`), the host contract
it was classified against, the per-hook rows, and bounded diagnostics. Nothing in
it echoes manifest content, plugin source, the audited path, or the path of an
inspected Hermes installation.

`classification_status` is `unknown` whenever any hook is unknown, the plugin
declares a Hermes range the supported contract does not cover, the range is
unreadable, the declaration itself is malformed, or the Hermes version you
inspected is one the mapping was never established for. Absence from the mapping
is never reported as safe.

That status also reaches `summary.risk_categories`, as
`undetermined_hook_contract`. This matters more than it looks. The summary is
what a wrapper reads, and without a category of its own an unreadable manifest
produced an empty category list — byte-identical to a plugin that declares
nothing, so "I could not read this" rendered as "this is fine". The category is
tied to `classification_status` rather than to any one cause, so it also covers
reader gaps nobody has found yet.

A malformed, oversized, duplicated, non-string or non-list hook declaration is a
finding about the plugin, not a failure of the audit: no hooks are classified and
one bounded diagnostic names the shape that failed. Two findings stay apart. A
manifest whose document is outside the readable subset is reported
`manifest_yaml_status: unreadable`; a manifest that parses but declares its hooks
wrongly is `present` with `declaration_status: invalid`.

### How much YAML the reader understands

It is a bounded subset reader, not a YAML implementation. What it does with each
construct is recorded in `tests/test_plugin_manifest_subset_corpus.py`, one row
per construct, committed and re-runnable. **That table is the standing check.**
The subset is revisited when a row in it changes, or when a real manifest is
refused in the field.

A differential against PyYAML 6.0.3 informed the table and is not a substitute
for it. On 2026-09-15 it reported no disagreement over 17,397 shared cases from
the fragment set used that day. The fuzz and its fragments are not committed, so
that number cannot be re-derived here, and a later run over different fragments
found 1,872 inputs this reader read and PyYAML rejected. Those are rows in the
table now. Cite the table, not the number.

All 105 `plugin.yaml` files shipped by hermes-agent `v2026.9.7` parse, ten of
them hook-bearing.

Being *more* permissive than a real YAML parser is the direction that does
damage, because a manifest Hermes cannot load gets reported as an established
contract — and the `undetermined_hook_contract` rule cannot catch that, since
the audit believes it read something. Four rules close it. A `|` or `>` value
must be a well-formed block scalar header. An anchor or alias is refused
anywhere in a value position, at any depth. A colon must be followed by a space
or end the line. A tab may not separate a key from its colon.

Some things it refuses that YAML allows, each costing the whole declaration and
reported as `undetermined_hook_contract` rather than guessed. A file holding
more than one document, because reading the first and ignoring the rest reports
a manifest the file does not have — a wrong answer rather than a missing one. An
anchor or alias, because resolving one is semantics the reader does not
implement. And a key containing a space, such as `display name:`, because the
key pattern excludes whitespace. None of the 105 shipped manifests hits any of
these, and each has a row in the table.

## Two declaration fields

`provides_hooks` is the field Hermes' manifest parser reads. Nine shipped Hermes
plugins declare their hooks under `hooks` instead, which Hermes accepts as a
known manifest field but never reads. The audit reads both so it can see those
plugins at all, labels each hook with the field it came from, and emits a
diagnostic when a hook is declared only under `hooks`. When both fields name the
same hook, the canonical field is reported.

## The mapping is pinned to a host revision

The hook vocabulary, the three dispatcher sets behind `host_contract`, and the
timeout semantics are read off one Hermes revision, recorded in the audit output
as `host_contract.contract_source` and bounded by
`host_contract.supported_range`. A plugin that requires a Hermes version outside
that range gets `unknown` for every hook rather than a classification borrowed
from a contract it was not measured against.

### What the pin detects, and what it does not

Be clear about this, because the temptation is to claim more. The mapping is a
snapshot transcribed by hand from Hermes 0.21.1. **It cannot detect upstream
drift.** Nothing in this repository reads hermes-agent, at test time or at run
time. If Hermes ships a thirty-eighth hook, the table keeps its 37, the suite
stays green, and the new hook classifies `unknown`.

`tests/test_plugin_hook_contract.py` holds a second hand copy of the same
reading. Comparing the two catches a local edit to the table that nobody
intended. It is a change-detector on the mapping, not a drift-detector on the
host, and two copies made from one reading agree by construction, so a green
suite is not evidence that the reading was right. One assertion there is a real
cross-check: the pinned version must match `HERMES_COMPAT_MATRIX`, the
separately maintained record of the Hermes version this repository tests
against, so bumping that without re-reading the hook contract fails.

### The declared range rarely fires

**`requires_hermes` is an OMH convention, not a Hermes field.** It is absent
from Hermes' `_KNOWN_MANIFEST_FIELDS` and no code under `hermes_cli/` or
`agent/` reads it. Of the 105 `plugin.yaml` files shipped by hermes-agent
`v2026.9.7`, **0 of 105** declare it — not merely none of the ten hook-bearing
ones. Only the OMH bundle does. So for a third-party plugin, which is the
population this audit exists to examine, the declared range is `undeclared` and
the hooks are classified against the pinned revision by default. The diagnostic
saying so is honest, but the gate does not engage on any plugin Hermes ships.

## Binding the result to the host you would enable the plugin on

`requires_hermes` says what a package asks for. It is not evidence of the Hermes
you will enable it on, and the audit never reads it as one. Name that host and
the result binds to it:

```sh
omh ops plugin-risk-audit --path <plugin-dir> --hermes-version 0.21.1
omh ops plugin-risk-audit --path <plugin-dir> --hermes-install ~/.hermes/hermes-agent
```

`--hermes-install` takes the installation directory — the one containing
`hermes_cli/` — and reads the `__version__` that module declares. It is a file
read. Hermes is not imported, its binary is not run, no subprocess is spawned
and no socket is opened, which is what keeps the audit usable on a machine you
are still deciding about. The two flags are mutually exclusive, so nothing has
to rank one host claim over another.

The result reports what was established under `declared_hooks.host_contract
.inspected_host`, beside the plugin's own declared range and never mixed into
it:

| Field | Meaning |
| --- | --- |
| `version` | The version that was established. `<absent>` when no host was named, `<invalid>` when one was and no version came out of it. A string that failed to parse is never echoed. |
| `observation_method` | `operator_declared`, `installation_version_module`, or `not_supplied`. It names the source that was selected, not that reading it succeeded. |
| `compatibility` | `compatible`, `incompatible`, `unreadable`, or `not_observed`. |

Two of those four hold the verdict. `incompatible` and `unreadable` force
`classification_status: unknown` and put `undetermined_hook_contract` in
`summary.risk_categories`, because a mapping that was never read against your
host must not render as an established contract. `compatible` and
`not_observed` leave the classification alone.

**`not_observed` is not a pass.** It is what you get when you name no host, and
it means only that nothing bound the mapping to an environment. The audit says
so in a diagnostic whenever the plugin declares hooks, rather than letting the
default path read as a clean result.

**The declaration survives the hold.** A manifest that parsed cleanly stays
parsed: `declaration_status`, the per-hook rows and `effects` still report what
the mapping says on the pinned revision. Only the overall decision is held.
Declaration evidence, host-version evidence and runtime behaviour are three
separate claims, and folding one into another would lose the one you needed.

**A version read off disk can be stale or edited.** That is why the result names
its observation method. A compatible verdict is static pre-enable evidence about
one mapping, never evidence that the host would admit, load, register or run the
plugin; the audit records `hermes_host_execution` and `hermes_plugin_admission`
as `not_observed` and keeps saying so.

### So what does an operator do with `unknown`?

`unknown` means the audit did not establish the semantics. It never means safe,
and it never means the name is not a real hook. A hook added after the pinned
revision looks exactly like a name the host never had.

When the hold came from the host leg, the mapping is simply behind or ahead of
your install, and the hook may well be real: read the host's own `VALID_HOOKS`
before concluding anything about the plugin. When the host is inside
`supported_range` and a name still classifies `unknown`, that name is one the
host contract does not contain, which is itself worth asking the plugin author
about.

### Re-pinning when Hermes moves

Update `PLUGIN_HOOK_CONTRACT_VERSION`, `PLUGIN_HOOK_CONTRACT_RANGE` and
`PLUGIN_HOOK_CONTRACT_SOURCE`; re-read the four cited host constructs at the new
revision; update the mapping and the copy in the test. The cross-check against
`HERMES_COMPAT_MATRIX` keeps the version honest, but only a person re-reading
the host source can keep the classifications honest.

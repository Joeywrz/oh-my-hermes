# Model Optimization Guidance

What OMH does differently per model, how it actually works at runtime, and —
for every family-specific prompt guideline — exactly why it exists and where
it came from. This document is a reader's map; the source of truth is the code
it cites, and a drift test (`tests/test_unit_prompt_protocol.py`) fails when a
calibrated family disappears from this file.

Boundary first: OMH never calls a model. Every optimization below is prepared
text or prepared configuration — calibration blocks ride prepared unit
prompts, routing rides Hermes config keys, and execution evidence always comes
from the runtime that actually ran the model.

## How it works, end to end

1. **A lane gets a model.** The mixture chains (shipped defaults plus your
   `~/.omh/routing/model-chains.json` overrides) pick a model and reasoning
   effort per category; `omh_delegate_route` writes them as explicit Hermes
   delegation keys. Any token-shaped model id is accepted — if the provider
   cannot serve it, the error comes back as a normal result and the chain
   falls over to its next entry (Hermes itself has no provider-side fallback).
2. **The model id is classified into a family.** `model_family()` in
   `src/coding/model_routing.py` strips a provider prefix
   (`opencode/kimi-k3` → `kimi-k3`) and matches by prefix. Unknown ids get
   family `unknown` — never an error, just generic discipline.
3. **The prepared prompt is assembled.** Every dispatched unit prompt carries
   the universal protocols (goal echo-back, numbered completion criteria,
   bounded verification). If the routed effort is `high`/`xhigh`/`max`, one
   family-specific calibration paragraph is appended for the subagent; the
   composer writing the split follows the calibration for its *own* model
   (`omh coding composition-guide --model <id>` prints it).
4. **The model runs it.** Tool calling, todo rendering, and parallel
   execution are Hermes runtime capabilities — OMH's ULW behaviors
   (`todo init`, phase checklists, parallel evals, interjection-resume) live
   in skill contracts and prepared handoffs, so they apply identically to
   every lane regardless of which model the chain routed there.

So "optimizing for a model" in OMH means exactly one thing: a short,
evidence-backed paragraph of counter-guidance appended to an otherwise
identical prompt. Nothing else about the pipeline changes per model.

## How a model is recognized

| Family | Matched by | Example ids |
| --- | --- | --- |
| `gpt` | `gpt-`; design-qualified alias `openai-gpt-` | `gpt-6-astra`, `gpt-5.6-sol`, `digitalocean/openai-gpt-5.6-sol` |
| `claude` | `claude-`; design-qualified alias `anthropic-claude-`; bare tiers `opus`/`sonnet`/`haiku`/`fable`/`mythos` | `claude-fable-5-1`, `claude-mythos-5-1`, `digitalocean/anthropic-claude-opus-5` |
| `gemini` | `gemini-` | `gemini-3.1-pro` |
| `kimi` | `kimi-` | `kimi-k3`, `kimi-k3-ultrafast` |
| `glm` | `glm-` | `glm-5.3`, `glm-5.3-flash`, `glm-5.2-ultrafast` |
| `grok` | `grok-` | `grok-code-fast-1` |
| `qwen` | `qwen-`, aliases `qwen3-` and `qwen3.` | `qwen3-coder`, `qwen/qwen3.8-flash` |
| `deepseek` | `deepseek-` | `deepseek-v4.1-flash`, `deepseek-flash` (the first-party pointer), `deepseek/deepseek-v4.1-flash` |
| `mistral` | `mistral-` | Mistral Large / Medium ids |
| `llama` | `llama-` | open-weights Llama ids, any serving host |
| `codestral` | `codestral-` | Codestral coding ids |
| `solar` | `solar-` | Upstage Solar Pro ids |
| `minimax` | `minimax-` | `MiniMax-M3`, `MiniMax-M2.7` (recognized, uncalibrated — see the coverage matrix) |
| `jev` | `jev-`; bare `jev` | `jev-1.13.0`, `jev-latest`, `jev-preview`, `typesafe/jev` (non-generative — see below) |
| `unknown` | anything else | emerging families before a prefix lands |

The design-qualified aliases are concrete serving-catalog ids, not new model
families. Bare vendor prefixes remain unclassified because the same provider
catalog also includes other designs such as `openai-o3` and non-text models
such as `openai-gpt-image-2`; neither may inherit GPT text calibration. The
models.dev catalog used by OpenCode lists, for example,
`digitalocean/openai-gpt-5.6-sol` with base model `openai/gpt-5.6-sol` and
`digitalocean/anthropic-claude-opus-5` with base model
`anthropic/claude-opus-5`. After the serving provider segment is stripped,
those aliases therefore select the existing `gpt` and `claude` calibrations;
they do not create vendor-wide calibration families.

`jev` is the exception to the bare-name rule above, and it is a deliberate
one: `deepseek` and `minimax` are vendor words naming a catalog, while `jev`
is an id TypeSafe's own documentation sends in the `model` field and a gateway
spells `typesafe/jev`. It is carried in `_BARE_MODEL_ALIASES` rather than by
widening the Claude tier set. The near misses stay `unknown`, including
`jev_evaluate`, which is a Hermes plugin TOOL name and not a model at all.

### Non-generative models

`model_class()` in `src/coding/model_routing.py` answers `generative` or
`non_generative` for one id, family-level, with `generative` as the default so
every model the catalog has never met routes exactly as it does today. A
`non_generative` model's documented output is a typed answer over options the
caller supplies, not text: Jev returns a Choice, a Score, or a Noul, and the
vendor's own jaggedness page states it "is not trained to generate text".

Nothing about that makes it a lesser model — it makes it a model that cannot
be handed a coding unit. So OMH recognizes it, contracts it, and prices it
like any other, and refuses it at every surface that would prepare it to
write code:

- `omh coding model-route --model jev…` answers `status: model_refused` with a
  `refusal` record naming the id and the class. No model, no effort, no chain
  is prepared. This is the one narrowing of "an explicitly requested model
  always wins": the request is still never adjudicated on quality, only on
  whether the class can do the work at all.
- `omh model-chains set <category> "jev:low"` exits 2 and writes nothing, and
  the operator category-maestro config rejects such an entry by name on
  read and on write.
- A chain entry that reaches the resolver anyway (a hand-edited document) is
  skipped with a `chain_entry` record naming the class, and the next
  generative entry takes the head.

Calibration does not apply: there is no prompt to counter-guide and no effort
ladder to place a floor on, so `HIGH_EFFORT_CALIBRATIONS` gains no entry and
this family gets no per-family section. The measurement that would normally
close an onboarding does not apply either, because a family-vs-model prompt
pair needs two generated answers to compare; what replaces it is described in
`docs/MODEL-ONBOARDING.md` and is a named follow-up, not part of the
recognition work.

### Exact-model contracts and overrides

Family is the default grain, and two exact-model surfaces sit in front of
it for a generation whose documented interface or traits differ from its
family's:

- **`MODEL_CONTRACTS`** in `src/coding/model_contracts.py` records what the
  vendor documents about one exact id — effort ladder and floor, limits,
  tool-calling API, unsupported parameters, dynamic-effort mechanism, list
  price, sources and the date they were read. A bounded
  `DECLARED_MODEL_CONTRACT_PROJECTIONS` table may map a provider catalog's
  explicitly named mode/service-tier alias onto that contract; it never strips
  an arbitrary suffix. `omh coding model-contract --model <id>` prints the
  resolved record. The route resolver consults it before the catalog: a
  requested effort the contract documents as unsupported is raised to the
  documented floor and the route says so (`effort_change.kind =
  floor_raised`), on every executor profile, so an unsupported rung never
  reaches a provider silently. Route receipts retain the requested id plus the
  canonical contract id, reasoning mode, service tier, and exact-versus-
  declared provenance. None of that claims catalog availability, entitlement,
  wire translation, or execution. A model without an exact or declared
  contract is treated exactly as before — by family and catalog alone.
- **`MODEL_HIGH_EFFORT_CALIBRATIONS` / `MODEL_COMPOSITION_CALIBRATIONS`**
  in `src/coding/unit_prompt_protocol.py` are calibration overrides keyed by
  exact id. `calibration_for_route()` resolves the recorded `selected_model`
  against them before falling back to `model_family`, and
  `composition_calibration_for_model()` does the same for the composer, so
  the family block — and every older generation's prompt — stays
  byte-stable when a new generation gets its own counter. The two tables
  share one key set (parity-tested) and every key has a contract.

The exact contract key is the served id after the provider prefix is stripped,
so `openai/gpt-6-astra` and `gpt-6-astra` resolve alike. An explicitly declared
catalog variant keeps its full requested/provider-qualified identity while it
inherits the canonical contract. Bare chat names are not aliased for GPT
generations (`astra`, like `sol`, classifies `unknown`); users name the served
id.

`omh coding model-contract-audit --inventory <path|-> --json` compares a
bounded local JSON inventory with those contracts without network access or
configuration writes. Each stable `model_contract_coverage/v1` row reports
family recognition, contract and effort coverage, model-specific calibration,
provider-family and category projection, price source or absence, and docs
coverage. Pass `--required-model` or `--recommended-model` repeatedly to make
missing required, recommended, and optional-discovery rows operationally
distinct; `--intentional-exclusion` records a deliberate non-contract row.
The comparison body has no timestamp and carries both the supplied inventory
source/digest and its own canonical digest, so onboarding, doctor, or release
workflows can hash-compare reports. A cold or unavailable inventory remains
explicitly different from an observed empty inventory. The audit is advisory:
it does not discover models, prove provider availability, change a route, or
create an issue.

## Universal protocols (every model, every family)

`src/coding/unit_prompt_protocol.py` attaches four deterministic blocks to
every dispatched unit prompt, regardless of model, and adds one composer-side
discipline for how those prompts are assembled:

- **Goal echo-back** — before the first tool use, the subagent restates the
  goal, its deliverable, and the numbered criteria, and reports (never
  guesses) if its reading conflicts with the declared boundary. *Why:* a
  misread boundary is cheapest to catch before any edit exists.
- **Pre-declared completion criteria** — "done" is a numbered list derived
  from the frozen unit contract before work starts. *Why:* completion must be
  a check against stated criteria, not a feeling.
- **Bounded verification** — exactly one full verification pass is both the
  floor (never skipped) and the ceiling (once criteria pass, re-verifying is
  forbidden; at most two fix-and-verify cycles before reporting the failing
  criterion). *Why:* the two dominant agent failure modes are opposites —
  skipping verification, and looping on it — and one bounded rule counters
  both. Review-role units add criterion-bound blocking with a two-round cap.
- **Failure-kind discipline** — a permission, sandbox, or policy denial is a
  boundary, not a bug: the unit must not retry it through another tool or
  route, and may report `blocked` only for a named concrete condition that
  survives the bounded fix cycles — difficulty, uncertainty, or useful
  remaining work is not blocked. *Why:* models over-generalize "failure →
  try another way", which turns policy refusals into route-around attempts,
  and under-specify "blocked", which turns difficulty into a stop. Adapted
  from the DeepSeek Harness sandbox-denial no-retry marker and its
  goal-policy blocked threshold ("difficulty, uncertainty, or useful
  remaining work is not blocked"), generalized to every family because both
  failure modes are cross-family (deepseek-ai/deepseek-harness, master
  2026-08-13; adopted in #1071).
- **Prompt-cache discipline (composer)** — the shared preamble of a fan-out
  stays byte-identical across sibling unit prompts: stable ordering, no
  timestamps or volatile status, unit-specific content appended after it,
  and staggered dispatch so the first request writes the provider cache the
  siblings read. *Why:* Anthropic, OpenAI, Gemini, and DeepSeek all cache
  prompt prefixes by exact bytes; DeepSeek additionally prices cached
  prefixes, which is where OMH first learned the rule
  (`PROMPT_CACHE_COMPOSITION_PROTOCOL`).
- **Tool batching** — independent reads and searches go out together in one
  turn and every result is inspected; dependent steps, edits, approvals,
  waits, and follow-ups that adapt to a result stay sequential; shell output
  is never decorated with separator commands (`TOOL_BATCHING_PROTOCOL`).
  *Why:* a fan-out unit that serializes independent reads pays a round trip
  per read, and a unit that parallelizes an edit with the read it depends on
  edits stale bytes; separators are noise inside a bounded output capture.
  It reaches every unit through the unit section, not the shared head: the
  head is frozen at its measured small-model budget (the section below), and
  a batching rule is the first thing a weak lane may drop, so it must never
  displace a stop rule there. The `claude` family block used to carry the
  batching half itself; that sentence moved here.

The first three originate from the stop-condition techniques the
oh-my-openagent research surfaced for high-effort models (terminal-condition
rules, criterion-bound blocking, capped re-review), generalized to every
family. The fourth comes from the DeepSeek Harness review named above. The
fifth generalizes that harness's priced-prefix composition constraint to
every family, because every major serving stack is a byte-exact prefix
cacher. The sixth comes from the Codex Desktop prompt review below; the
`claude` family block already told its units to request every independent
item in one response, and the universal rule is that sentence promoted to
every family with the sequential set and the separator rule added.

### Writing for the smallest model in the fleet

The per-family blocks below vary by family. The shared preamble does not: it
is byte-identical across sibling prompts on purpose, so providers can cache
the prefix. That makes it the one block that has to be written for the
*smallest* model that will read it, not the largest. A frontier model
tolerates a dense head; a weaker local CLI starts dropping rules once a
prompt carries more than it can hold, and the rule it drops is not the one
you would have picked. So every rule added to the shared head is paid for by
displacing a rule already there — on exactly the lanes least able to afford
it.

`src/quality/small_model_prompt_budget.py` measures the two halves of that
which a gate can actually check, and
`tests/test_small_model_prompt_budget.py` enforces them:

| Ceiling | Value | What it bounds |
| --- | --- | --- |
| `SHARED_PREAMBLE_MAX_BYTES` | 2770 | OMH-authored bytes of the executor-invariant head. `UNIT_PROMPT_MAX_BYTES` bounds the whole assembled prompt, which would let the shared head triple without tripping; the caller's goal line is excluded because its length is the operator's business. |
| `SHARED_PREAMBLE_MAX_CONSTRAINTS` | 10 | Directive sentences in that head. |
| `BLOCK_MAX_CONSTRAINTS` | 3 | Directive sentences in any single dispatched block, family calibrations included. |

All three are the **measured current values**, not aspirations. The upstream
doctrine puts a tiny pattern-completer's limit at roughly 3-5 constraints
before rules start displacing each other; OMH's consumers are coding-agent
CLIs rather than tiny models, so 5 is recorded as the target while the
ceilings freeze the head where it is. Raising one is allowed and is a
decision: say which existing rule the new one displaces, or move the rule
into a unit-varying block where only the units that need it pay. The
constraint count is a sentence-level proxy and is named as one — it counts
the sentences a reader must hold as a rule, not the rules themselves.

One further rule is mechanized: **no labelled contrast examples**. A block
containing `Bad:` or `Wrong:` followed by a sample gets the sample copied
rather than avoided by a weaker model, which is the opposite of the intent.
State the wanted shape instead.

Two rules are deliberately left to the author, because no regex can apply
them:

- **Positive framing, except where the negation is the payload.** Small
  models drop the "not" and do the thing anyway, so prefer stating the
  wanted behavior. But "do not re-verify once every criterion has passed" is
  the entire anti-inertia rule; rewriting it positively would lose it. Judge
  per rule, and keep the negation only when it *is* the rule.
- **Delete rules the code already enforces deterministically.** OMH enforces
  a great deal at freeze time — boundary overlap, dependency cycles, unit
  schema, owner and model resolution — and a prompt rule restating one of
  those spends headroom that a rule the code cannot enforce needs. Before
  adding a rule, check whether a gate already makes it true.

### Techniques compared and already structural (DeepSeek Harness review)

The same harness review surfaced techniques OMH already carries structurally,
recorded here so the comparison stays auditable instead of being relitigated:
single-owner-of-facts (the skill catalog and its byte-gated generated
projections), negative instructions phrased as the concrete behavior they
forbid (the house calibration style throughout this file), and numeric stop
bounds for ambiguous judgments (one-pass verification, two fix-and-verify
cycles, two review rounds). Machine-readable result markers and
KV-cache-aware request assembly belong to the executor/runtime that actually
calls a model — outside the universal prompt-cache composition discipline
above, they are out of OMH's boundary by design.

### Techniques compared (Codex Desktop prompt review, community source)

A circulating dump of the Codex Desktop system prompts for GPT-6 Astra
(elder-plinius/CL4R1T4S, `OPENAI/Codex_Desktop/GPT-6_Astra_Prompts.md`, read
2026-09-11 — 5,051 lines with no date, version, or extraction method, so
**community / unverified**; nothing in it overrides the official contract in
`src/coding/model_contracts.py`) was compared against OMH the same way the
DeepSeek Harness was. What it corroborates is already here: instruction
precedence ("the user's instruction … must take precedence over any
guidelines provided in skills"), assumptions over questions ("strongly
prefer making reasonable assumptions … rather than stopping to ask"), and
test sizing ("do not write tests for reversible, low-impact changes or that
mirror the implementation … broaden or repeat testing only when new changes,
failures, or unresolved concerns justify it") are the three sentences of the
`gpt-6-astra` override, and the guardian-rejection rule ("continue with a
safer alternative, or carry out checks to prove that the action is
authorized … do not bypass this rejection through a workaround") is the
failure-kind discipline (its "safer alternative or shown authorization"
wording is the same rule as "continue with what the boundary allows, or
report it" and was not added twice; likewise a checkpoint's contents are
already the `goal_ledger/v1` structure, so no sentence restates them).
Adopted as harness discipline on OMH's own surfaces (executor-neutral,
measured by observed engine behaviour rather than a token benchmark): the
latest mid-run message is steering for the active task, not a replacement
objective — a sharpening of the interjection rule's existing "when the
interjection changes scope, say so" clause; a follow-up that needs new
authority, expands scope materially, or changes external state not already
authorized is described and approved first, and persistence never broadens
scope (`ENGINE_FOLLOW_UP_AUTHORITY_RULE` — the engine-entry, external
executor, and delegation-enable gates already asked before their own
steps, but nothing covered an external state change such as a merge or a
send); independent reads batch, dependent steps serialize, no decorative
shell separators (`TOOL_BATCHING_PROTOCOL`, every unit's section, outside
the frozen shared head); the closing brief scales to the change, leads with
the result, and omits abandoned approaches unless they explain a tradeoff,
while the observed run summary and any prepared-not-observed or unmerged
work are stated whatever the length (`ENGINE_CLOSING_BRIEF_RULE`, new).
Deliberately not adopted: the persistence
push ("do not settle for a partial or 'helpful enough' solution … persist
until the user's intended goal is complete", "only send a final message
after concluding that no follow-up … could be useful"). It is written for an
interactive desktop session with the user present; OMH units are bounded by
numbered criteria, and the 2026-09-05 Astra measurement above showed that a
completion-push sentence costs +5,419 tokens per instance at an unchanged
pass rate, spent on the tasks the model fails. Host channels (commentary
versus final, heartbeats, async user messages, `notes`/`history` tools,
`fork_turns`) are Hermes' surfaces, not OMH's; the confirmation-policy tiers
for computer use are a candidate for the browser skills' boundaries, not
this round.

## Per-family calibrations: what, why, and where each came from

Two tables in `src/coding/unit_prompt_protocol.py` carry the family-specific
guidance: `HIGH_EFFORT_CALIBRATIONS` for the **subagent executing a unit**,
and `MAIN_AGENT_COMPOSITION_CALIBRATIONS` for the **composer** splitting work
and writing unit prompts. A parity test forces the two tables to share one key
set — no family gets subagent discipline without composer discipline.

The governing rule, stated in the module docstring: a calibration counters a
family's *known* failure mode. No family carries richer guidance than another
without a stated reason, and no vendor is privileged. Provenance falls into
three buckets, each named per family below:

- **Adapted research** — stop-condition work from the oh-my-openagent
  project on how high-effort reasoning models over-verify.
- **Observed failure modes** — behavior seen in live OMH/Hermes usage of that
  family (recorded in the commits that introduced each block).
- **Provider-published model characteristics** — facts the vendor states
  about the model's design (e.g. a non-thinking architecture), which make
  certain prompt shapes actively harmful.

Validation is common to all: `benchmarks/live-model-tools/v1` runs
baseline-vs-calibrated prompt pairs where the *only* difference is the
calibration block, and `tests/test_omh_live_model_benchmark.py` pins that
pairing so a benchmark claim can never mix in other prompt changes.

### `gpt` (GPT-5.6 Sol / Terra / Luna)

- **Model trait:** a strong long-horizon reasoner. Its characteristic waste
  is spending depth on things that are already decided: re-deriving facts it
  established earlier, and re-running verification "for reassurance".
- **What OMH injects (subagent):** reasoning depth belongs to the hard parts
  of *this* unit; once the decisive fact is in view, act on it; a passed
  criterion is settled evidence, reopened only by contradicting output.
- **What OMH injects (composer):** compose outcome-first, but never compress
  the contract away — GPT's tight compositional style tends to drop stated
  boundaries and criteria while shortening a prompt, and a tighter prompt
  that loses an invariant is a worse prompt.
- **Source:** adapted research (oh-my-openagent stop-condition findings on
  high-effort models), one of the two original calibration entries.
- **Version rule:** the block above is written for the 5.6 generation and
  is what every `gpt-` id receives unless an exact-model override exists.
  GPT-6 Astra has one, below; the 5.6 prompts are byte-stable across it.

### `gpt-6-astra` (GPT-6 Astra, exact-model override on the `gpt` family)

- **Documented contract:** `gpt-6-astra` (released 2026-09-03, staged
  rollout) is the exact contract. The active host catalog's declared aliases
  are `gpt-6-astra-fast`, `gpt-6-astra-flex`, `gpt-6-astra-pro`,
  `gpt-6-astra-pro-fast`, and `gpt-6-astra-pro-flex`: `pro` selects the
  reasoning mode, while `fast` and `flex` select the service tier. They inherit
  only through explicit rows, keep the requested provider-qualified id in
  receipts, and resolve to canonical contract `gpt-6-astra`; an unknown or
  malformed suffix does not inherit. The shared contract records a
  1,050,000-token context, 922,000 max input, 128,000 max output, knowledge
  cutoff 2026-04-30; reasoning effort `low`, `medium`, `high`, `xhigh`, `max`
  — `none` returns HTTP 400 and the migration guide sends `none`/`minimal`
  callers to `low`, so `low` is the floor OMH raises `off`/`minimal` requests
  to (recorded as `floor_raised`); no default effort, so a route names one;
  tool calling on the Responses API only; `temperature`, `top_p`,
  `top_logprobs` unsupported; `configuration_update` can change effort between
  responses in standard single-agent mode only, not alongside automatic
  compaction or truncation. Full record and sources in
  `src/coding/model_contracts.py`; `omh coding model-contract --model <id>`
  prints the exact or declared projection.
- **Model trait (official, latest-model guide):** asks a clarifying question
  more readily when more input could materially change the result; follows
  instructions more strictly and may pause on unclear or conflicting
  skill-file guidance; may delegate less often than a harness expects; may
  write broader tests than the change requires. The GPT-5.6 counter
  (re-deriving settled facts, re-verifying for reassurance) is not what the
  guide describes for Astra, which is why the override exists.
- **What OMH injects (subagent):** the user's instructions outrank skill or
  guideline text and the numbered criteria are the complete task — nothing
  outside them is owed; ask one focused question only when a missing input
  would materially change the result, otherwise state the assumption and
  proceed; size tests to the change — a reversible, low-impact edit that
  mirrors its implementation needs no new test, and a green check is re-run
  only when its inputs changed. Two constraint sentences; under the
  per-block ceiling. The first sentence originally read "carry them to
  completion instead of pausing for sign-off on work the boundary already
  authorizes"; the 2026-09-05 measurement below is why it no longer does.
- **What OMH injects (composer):** write the user's intent into each unit
  prompt above any skill text; delegate every unit that is independent of
  the work you keep (an undelegated independent unit is chosen latency);
  set each unit's effort from its task state — the documented floor for
  routine follow-ups, deeper only while a criterion holds unresolved hard
  reasoning or contradictory evidence — and land an effort change on the
  next prepared unit rather than on a claimed mid-conversation switch.
- **Effort policy:** `dynamic_effort_guidance()` emits the
  `configuration_update` (mid-conversation) policy only for an executor
  profile the contract names as compatible; no prepared profile is, so
  every profile today gets the per-turn policy and no prepared text claims
  a mid-conversation change happened. `omh coding composition-guide --model
  gpt-6-astra --executor <profile>` shows which one applies. The universal
  echo-back, criteria, TODO reconciliation, one-pass verification, and
  bounded repair cycles are unchanged and not restated in the override.
- **What is deliberately absent:** no "you are being monitored" language,
  no request for or storage of raw chain of thought. OpenAI's monitorability
  evaluation observed fewer textual CoT tokens under monitoring awareness in
  an adversarial honeypot setting, where some attacks moved into tool calls
  with no textual CoT; that is not evidence of less overthinking, lower
  latency, or better task results, and a test pins the override free of it.
- **Throughput overlay:** unchanged. The `gpt_sol_codex_handoff` overlay stays
  gated to `*-sol` and the Hermes `ultrawork` overlay stays family-wide;
  neither has an Astra measurement, so Astra on codex gets the base rules.
- **Routing:** heads `ultrabrain` and the GPT slot of `architect` in both
  lanes. GPT-5.6 Sol trailed it as fall-through until 2026-09-11, when the
  superseded generations left every shipped chain (owner decision); Sol
  stays in the shared last resort and as the codex cost-tier default. The
  Terra and Luna lanes are cost-tier picks and stay as they were (#1313). An
  account the staged rollout has not reached gets a provider rejection and
  the chain falls through to the next ecosystem.
- **Pricing:** exact operator `model-prices.json` rows win first. A declared
  alias without an exact row inherits the base Astra 10/50 list rates; `fast`
  applies the documented 2x service-tier multiplier and `flex` 0.5x. `pro` is
  a reasoning mode and has no fabricated multiplier. Cached input stays at
  the documented default tenth. The $12.5/M cache-write rate and the 2x input
  / 1.5x output multiplier above 272K input tokens have no column in
  `APPROX_PRICE_PER_MTOK` and remain visible in the inherited contract rather
  than being flattened.
- **Measured (2026-09-05, subagent block):** four arms on
  `benchmarks/live-model-tools/v1`, evaluation split (30 instances, corpus
  digest `c4ea899a…`), `hermes_current_session` path, `openai-codex` /
  `gpt-6-astra` at `xhigh`, omh 2.0.0, Hermes 0.21.0, arm order
  optimized → family → baseline → revised. Every arm passed 18 / 30 with
  identical per-template results, so pass rate decided nothing. Tokens did:
  the original override cost 1,675,942 against the inherited `gpt` block's
  1,513,367 (+5,419 per instance, bootstrap CI95 [+1,444, +10,150], more in
  26 / 30) and against no calibration at all (1,550,904). Hermes' own
  session rows put it at 310 tool calls / 206 API turns versus 286 / 194 for
  the family block, with the excess concentrated in `BUGFIX` and
  `DIAGNOSTICS` — the model kept working on tasks it was not going to pass.
  The "carry them to completion instead of pausing" clause was the one
  sentence with that reading, and the block above is the revision that
  replaces it: 1,532,241 tokens (−4,790 per instance against the original,
  CI95 [−9,336, −1,075]; +629 against the family block, CI95 [−1,109,
  +2,445], indistinguishable), 294 tool calls, 192 API turns, still 18 / 30.
  Per §8 that is the "revise" outcome: the Astra-specific counters
  (assumption over question, test sizing) stay, the clause that made the
  model over-work is gone, and the override now costs what the block it
  replaced costs. Full tables in the benchmark README. Not measured: the
  composer block (no fanout in this harness), clarification and delegation
  counts (the corpus never provokes them; #1327 is the follow-up), and any
  claim beyond this corpus.
- **Source:** official (the OpenAI model reference, latest-model guide,
  reasoning guide, async tool calling and steering guides, monitorability
  evaluation, and system card, read 2026-09-04), plus the 2026-09-05
  measurement above for the first sentence's wording.

### `claude` (Fable 5.1, Mythos 5.1, Fable 5, Opus 5, Sonnet, Haiku)

- **Model trait:** conscientious to a fault. Left alone it grows the
  checklist mid-run ("while I'm here…"), adds just-to-be-sure verification
  passes, and — as a composer — fans out speculative subagents, including
  ones that only re-check its own work. The 5.1 generation adds, per
  Anthropic's migration guide: at higher effort on routine work it gathers
  context and deliberates beyond what the task needs; it tidies, refactors,
  and commits extra tests nobody asked for; it rewrites a whole file where a
  targeted edit would do; in long agent loops it issues one implied tool call
  per turn where Fable 5 batched several; deep into a session it can end a
  turn by *announcing* the next step instead of running it; and progress
  claims drift from tool evidence on long runs. Parallel sub-agent delegation,
  by contrast, became dependable — the prior-model habit of suppressing it now
  costs wall-clock.
- **What OMH injects (subagent):** the numbered criteria are the *complete*
  checklist — do not grow it mid-run, and act once you have enough to act;
  deliberate deeply only where correctness is genuinely at risk and let the
  single verification pass prove the mechanical steps; edit surgically; fix
  only what the criteria name and report adjacent findings; keep scratch
  checks out of the repo and commit tests only where a criterion or the
  repo's own convention asks for them; add no helpers, fallbacks,
  validation, flags, or shims beyond what the criteria name; no one is
  watching in real time, so proceed on reversible actions and finish
  a last paragraph that is a plan or a promise instead of ending on it;
  every progress claim points at a tool result, a failed check is reported
  with its output, a skipped step as skipped. The block sits exactly on the
  `BLOCK_MAX_CONSTRAINTS` ceiling; the 5.1 additions were phrased as
  descriptions rather than modal directives to stay there.
- **What OMH injects (composer):** split only what the goal requires, no
  speculative units, no unit whose only job is re-checking the split itself
  (a fresh-context review of a unit's deliverable is a legitimate unit);
  delegate what is independent and evidence-judgeable, keep in line what
  finishes in a handful of tool calls, and keep working while units run;
  state the criteria once and freeze; a closing paragraph that is a dispatch
  gets run before closing; write the closing report as the reader's first
  look (outcome first, plain sentences, no working shorthand).
- **What OMH injects (throughput overlay, advanced modes):** a delegated
  lane returns a distilled report — outcome, evidence pointers, open items —
  never its transcript; delegated transcripts are what floods a composer's
  context.
- **Version rule:** the counters are written for 5.1 and are harmless on
  Fable 5 / Opus 5 (the batching and whole-file-rewrite counters simply hold
  behavior those models already had). The Opus 5 guidance to *delete*
  verification instructions does not apply to 5.1 — the single verification
  pass stays. `claude-mythos-5-1` is the same model as `claude-fable-5-1`
  served only to Project Glasswing-approved organizations; it takes the same
  calibration, and no shipped chain names it — a user who asks for it by
  name is still recognized and routed.
- **Source:** the original checklist/fan-out counters are adapted research
  (same origin as `gpt`), the composer block was added after observing
  over-fan-out in live composition; the 5.1 additions follow the official
  Claude Fable 5.1 migration guide's prompt-tunable behavioral shifts
  (official label; the "let it delegate", "act when you have enough",
  "targeted edits", "scope and test coverage", "batch independent tool
  calls", "ground progress claims", and "early stopping" entries). Not yet
  measured on 5.1 in this repo — the Fable 5 vs 5.1 benchmark pair is the
  named follow-up.

### `gemini` (Gemini 3.1 Pro)

- **Model trait:** fluent and confident narration. Its observed failure mode
  is asserting results from recall rather than from tool output, sounding
  "done" before verification has run, and creatively expanding beyond the
  declared boundary because the expansion seems like an improvement.
- **What OMH injects (subagent):** a claim without the tool output that
  proves it is not evidence — run the actual check and report from its
  output; done-sounding language before the mandatory verification pass is a
  failure, not optimism; expansion outside the boundary is a defect here.
- **What OMH injects (composer):** compose from tool-verified facts, not
  recall — run the inventory and readiness commands before naming owners or
  models; a unit is "prepared" only when the prepare command produced its
  artifact.
- **Source:** observed failure modes in live usage (authored in the
  per-family calibration commit; no upstream text existed for this shape).

### `grok` (Grok Code Fast)

- **Model trait:** speed-first, search-heavy. The risk profile is the inverse
  of the deep reasoners: not over-verification but *under*-verification —
  fast answers that skip the proof, and repeated re-querying when a search
  surfaces many candidates.
- **What OMH injects (subagent):** speed is the default and the numbered
  criteria are the brake — a fast first answer never skips the single
  mandatory verification pass; pick from search results once, by the stated
  criteria, and act.
- **What OMH injects (composer):** run the overlap and dependency-cycle
  checks *before* recording the contract, not after dispatch fails;
  re-querying for a better split is re-verifying a settled decision.
- **Source:** written fresh for OMH — the calibration commit records that
  grok had no upstream precedent; the content encodes the family's publicly
  stated speed-first design plus observed search-churn behavior.

### `kimi` (Kimi K3, K3 Ultrafast)

- **Model trait:** a deep decompose-compare-verify reasoning loop. Excellent
  on genuinely hard problems; wasteful on low-entropy mechanical steps, where
  it enumerates alternatives that no stated criterion distinguishes.
- **What OMH injects (subagent):** reserve the decompose-compare-verify loop
  for the genuinely hard parts; mechanical steps are low-entropy — execute
  them directly; if you catch yourself listing options for a step no
  criterion distinguishes, stop analyzing and act.
- **What OMH injects (composer):** partitioning work is mostly low-entropy —
  decide the split once and freeze it; keep the deep reasoning for boundary
  overlaps and dependency cycles; if two partitions both satisfy the
  boundaries, take the first and move.
- **Source:** observed failure modes in live OMH usage of Kimi K3 (authored
  in the per-family calibration commit).

### `glm` (GLM 5.3, 5.3 Flash, 5.2, speed tiers)

- **Model trait:** an interleaved-reasoning style — thinking woven between
  tool calls. That style genuinely improves tool-result interpretation, but
  applied indiscriminately it plans mechanical steps that need no plan. The
  5.3 generation hardens the style into a served contract: thinking cannot
  be disabled (depth moves through the provider's reasoning-effort levels
  instead), and the coding endpoint preserves reasoning across tool calls by
  default — expecting the preserved blocks returned complete, unmodified,
  and in order, or cache effectiveness and continuity are lost. GLM 5.3
  Flash is a separately trained smaller MoE, not a speed tier, but it is the
  same `glm-` family and receives the same calibration; the served 5.3
  speed tier is `glm-5.3-highspeed`. Community harness evidence (Cline's
  GLM system-prompt rework) adds two family sensitivities: short,
  mechanically explicit prompts with strict tool-invocation rules outperform
  narrative ones, and tool-call formatting decays in very long contexts.
- **What OMH injects (subagent):** use interleaved reasoning only where it
  improves a tool decision — interpret each result, choose the next bounded
  action, preserve prior reasoning context when the runtime exposes it,
  returned complete and unmodified in its original order; on 5.3, reasoning
  depth is the routed effort level, never a request for no thinking;
  mechanical steps need no extended plan.
- **What OMH injects (composer):** interleave reasoning to interpret evidence
  between contract-building tools; mechanical field assembly needs no extra
  planning; keep unit prompts lean and mechanically explicit and unit scopes
  bounded (long-context tool-call decay); Z.ai prices cached input
  separately, so the shared prompt-cache discipline is billing-visible;
  freeze the smallest split once boundaries are clean.
- **Source:** observed failure modes plus the family's documented
  interleaved/preserved-thinking contract (docs.z.ai thinking-mode and
  GLM-5.3 release docs, 2026-08) and community harness reports (Cline's
  GLM-4.6 system-prompt rework; OpenCode long-context tool-call-format
  reports). The GLM guidance shipped with the baseline-vs-calibrated
  benchmark harness so its effect is measurable.

### `qwen` (Qwen3-Coder)

- **Model trait:** the current Qwen3-Coder is, per its own release
  documentation, a **non-thinking** coding-agent model — it does not emit
  reasoning traces, and prompting it for chain-of-thought or thinking tags
  degrades it rather than helping.
- **What OMH injects (subagent):** do not ask it to emit reasoning or
  thinking tags; give the exact goal, repository state, allowed boundaries,
  tool schemas, and completion criteria; follow one explicit plan; recover
  from failures using observed tool output; stop after one passing
  verification run.
- **What OMH injects (composer):** freeze one ordered split with exact
  owners, boundaries, tool contracts, dependencies, and verification
  commands instead of requesting reasoning output.
- **Source:** provider-published model characteristics (Qwen3-Coder's
  non-thinking architecture); shipped with the benchmark harness.

### `deepseek` (DeepSeek versioned line)

- **Model trait:** a heterogeneous family — some variants are reasoning
  models, some are not, and the split moved across versions. The common
  error in the wild is applying legacy R1-era reasoning prompts to every
  DeepSeek model, which is wrong on the non-reasoning variants. Two further
  facts come from DeepSeek's own agent harness
  (deepseek-ai/deepseek-harness, master 2026-08-13): its benchmark preset
  reproduces the Claude-SWE-compatible exact-string editor contract
  verbatim — the family is post-trained on exact-literal `old_str` edit
  semantics with uniqueness and whitespace discipline — and DeepSeek
  serving prices cached prefixes, which that harness treats as a
  first-class composition constraint.
- **What OMH injects (subagent):** treat the model version and its declared
  thinking mode as *contract fields*; preserve runtime-provided reasoning
  context across tool results only on a reasoning-capable route; otherwise
  use the same explicit goal/boundaries/criteria without thinking tags; edit
  by exact literal strings (a unique match with exact whitespace); make
  the smallest correct change, verify once, stop.
- **What OMH injects (composer):** keep the DeepSeek version and thinking
  mode explicit in the prepared route; no synthetic thinking instructions on
  non-reasoning routes; and the family residue of the now-universal
  prompt-cache discipline — DeepSeek serving prices cached prefixes, so the
  shared-preamble rule is billing-visible on this family, not merely
  latency.
- **Source:** provider-published model characteristics (DeepSeek's
  reasoning/non-reasoning variant split; shipped with the benchmark
  harness), plus the DeepSeek Harness review adopted in #1071 (exact-string
  RL edit contract, priced prefix caching — the priced-prefix fact later
  generalized into the universal prompt-cache protocol above).
- **Generations:** DeepSeek V4.1 Flash (2026-09-10) has its own exact
  contract and override below, because on that model the family block's
  conditionals resolve (thinking is on by default, the reasoning is returned
  on every tool turn) and the vendor documents a three-rung effort ladder.
  Every other DeepSeek id — V3.2, V4 Flash, V4 Pro, `deepseek-chat`,
  `deepseek-reasoner` — keeps this family block.

### `deepseek-v4.1-flash` (DeepSeek V4.1 Flash, exact-model override on the `deepseek` family)

- **Documented contract:** `deepseek-v4.1-flash` (released 2026-09-10) is
  the exact contract; OMH's alias is the versioned gateway spelling
  (`deepseek/deepseek-v4.1-flash` on OpenRouter). The first-party API names
  the current Flash generation `deepseek-flash`, and that pointer is the one
  declared alias (`DECLARED_MODEL_CONTRACT_PROJECTIONS`): it inherits the
  contract with `declared_inheritance` provenance and a read date, because
  the next Flash release moves it. `deepseek-v4-flash` and
  `deepseek-v4-flash-vision-exp` are routed to V4.1 Flash by the vendor, and
  `deepseek-v4-pro` routes to it from 2026-09-14 pending a V4.1 Pro; none of
  those spellings inherits — a routed alias is the vendor's wire concern,
  not a catalog claim. The contract records a 1,000,000-token context,
  384,000 max output, thinking on by default at `high`, the documented
  effort ladder `low` / `high` / `max` with no floor to raise to (the
  thinking-mode guide publishes the mapping for every other rung —
  `minimal` → `low`, `medium` → `high`, `xhigh` → `high`, `ultra` → `max` —
  and nothing returns an error, so `unsupported_efforts` is empty and the
  route passes `medium` or `xhigh` through on record with the table in the
  contract saying what it bought), tool calling on Chat Completions, the
  Responses API, and the Anthropic-compatible endpoint, `reasoning_content`
  of every earlier turn sent back on every request that carries `tools` —
  even turns without a tool call, HTTP 400 otherwise — and `temperature` /
  `presence_penalty` / `frequency_penalty` without effect in thinking mode
  (`top_p` has a 0.95 floor there). Full record and
  sources in `src/coding/model_contracts.py`; `omh coding model-contract
  --model deepseek-flash` prints the declared projection. No
  `dynamic_effort` mechanism is documented, so `dynamic_effort_guidance()`
  returns nothing and `floor_raised` never fires for this model.
- **Model trait (official, model card and API guides):** a 552B-parameter
  causal encoder-decoder MoE that activates 8B parameters per token in
  prefill and 16B in decode; post-trained on large-scale synthesized agent
  tasks and evaluated by the vendor at its maximum reasoning setting with a
  1M-token context and `max_tokens` of at least 256K; thinking on by
  default, its chain of thought returned as `reasoning_content` and
  concatenated back into the context on every later tool turn; cache-hit
  input priced at a fiftieth of cache-miss input. The vendor's best coding
  numbers come from its own harness's *Minimal* preset — one persistent
  shell tool, a one-line persona ("You are a helpful software engineer
  assistant."), no runtime context, no compaction, effort `max`,
  `max_tokens` 256,000 — which scored above the Standard and PTC presets on
  DeepSWE (72.6 / 70.5 / 67.6) and Terminal-Bench 2.1 (90.6 / 85.8 / 85.8).
  The card's scaffold table says the same thing from the other side: across
  eight harnesses (Claude Code, Codex, OpenCode, Pi, mini-SWE-agent, and the
  three DSH presets) DeepSWE stays within 65.5–74.2 and Terminal-Bench 2.1
  within 84.1–90.6 — the model was trained to depend little on the harness,
  so scaffolding is not where its pass rate lives. Both facts are why the
  override below stays three sentences and why OMH's claims for this model
  are cost claims, not pass-rate claims. The card publishes no per-effort
  score or token table for the API rungs; a circulating "effort 25 / 50 /
  100" curve is unsourced until the vendor prints it, and #1463's `low`
  arm is how OMH measures it instead. Community harness notes
  (OpenRouter's listing) describe it as strongest on long-horizon tasks that
  run to completion across many steps — the same "keeps working" trait the
  Astra measurement found expensive on tasks a model will not pass.
- **What OMH injects (subagent):** the resolved contract in one sentence —
  thinking is on and the runtime returns earlier reasoning on every tool
  turn, so the visible reply carries the change and the stop rather than a
  restatement of reasoning the context already holds, and a turn that ends
  without a tool call carries its answer in the visible text, never only in
  reasoning (the vendor's harness adapter records that the live API rejects
  an assistant turn whose content is empty because the answer sat in the
  reasoning channel); the family's exact-literal-string edit rule, kept
  because the override replaces the family block rather than extending it;
  the blocker rule — when the evidence already in hand cannot satisfy a
  criterion, report the blocker from that evidence rather than widening the
  search, and leave a passed criterion closed; and the family block's
  closing rule, verbatim — make the smallest correct change, verify once,
  and stop. Four sentences; under the per-block ceiling. Nothing in it asks
  the model for more output: the first wording asked the reply to carry
  "the verification output" and the blocker report to carry "the observed
  output", and the measurement below found that those two phrases cost 25%
  more tokens than the family block for the same pass set, most of it
  re-running checks on tasks the model was not going to pass — the same
  "keeps working" signature the Astra round found, reached through a
  request for output rather than a request to continue.
- **What OMH injects (composer):** the visible composition is the ordered
  split, not a replay of planning already in context, and carries no
  synthetic thinking instructions; the shared preamble stays byte-identical
  across sibling units because a cache miss costs fifty times a hit on this
  model; a unit routed to this model takes `low`, `high`, or `max` — its
  documented ladder; the vendor's own table turns `medium` and `xhigh` into
  `high`, so an undocumented rung is a rung the composer did not choose;
  validate once and stop.
- **Why each sentence:** reasoning passback → do not restate reasoning
  (the context already carries it, restating doubles the tokens); the
  long-horizon post-training → blocker report instead of widening the
  search; exact-string edit training → keep the family edit rule; priced
  cache hits → byte-identical preamble; three-rung ladder with a published
  mapping → name a documented rung on every unit.
- **Routing:** takes the slots DeepSeek V3.2 held — the reasoning-capable
  budget fall-through behind GPT-5.6 Terra on `deep` at `high`, and the
  DeepSeek entry on `unspecified-low` at `low`. Both efforts are documented
  rungs. The chain alias is `deepseek-flash`, the id the vendor's API
  serves and Hermes forwards: the first-party endpoint rejects the
  versioned spelling `deepseek-v4.1-flash` with HTTP 400 (observed in the
  Hermes DeepSeek provider profile, 2026-09-11), so a shipped chain naming
  the versioned id would 400 out of the box on the one provider that is
  never a gateway. The versioned contract sits behind the pointer as the
  declared projection; a child observed under the gateway spelling
  `deepseek/deepseek-v4.1-flash` still labels `deep` / `unspecified-low`
  because `mixture_category_for` projects an exact id onto its declared
  *pointer* aliases (`EXACT_CONTRACT_POINTER_ALIASES` — the same model at
  the contract's own mode and tier) as well as the other way round; a
  `-pro` / `-fast` / `-flex` variant never labels its base id. V3.2 left the shipped chains
  with the other superseded generations on 2026-09-11 and stays
  recognized, priced, and provider-mapped for a machine-level override. It
  does not head `deep`: that lane stays on Terra by owner decision
  (#1313), and no OMH measurement of this model exists yet to argue
  otherwise.
- **What the Hermes lane does with it (observed in the Hermes Agent source,
  v0.21.1 and origin/main, 2026-09-11 — recorded so nobody looks for an
  OMH fix):** Hermes forwards `reasoning_effort` from its own vocabulary,
  so a prepared `medium` reaches DeepSeek verbatim and the server maps it
  to `high` (there is no cheaper middle rung), `minimal` is sent as `low`,
  and `xhigh` is escalated by Hermes to `max` (DeepSeek itself would map
  `xhigh` to `high`); an unset effort is the server default `high`.
  Thinking-off is reachable only as `--reasoning none` / `agent.
  reasoning_effort: false` (sent as `thinking: disabled`); an effort-only
  "none" leaves thinking on. Hermes stores `reasoning_content` at write
  time and replays it on every assistant turn for DeepSeek routes, pads it
  where absent, and persists it across resume — the vendor's passback rule
  is met, and the cost consequence is that every tool turn re-sends all
  prior reasoning, mostly as cache hits. On the installed v0.21.1 a typed
  `deepseek-flash` is folded onto `deepseek-v4-flash`, which the vendor
  routes to V4.1 Flash; once the installed Hermes Agent moves to a
  post-2026-09-10 build, `deepseek-flash` is canonical. Hermes sends no
  `max_tokens` for DeepSeek unless the operator configures one, so the
  API's own 384K ceiling applies and the small-`max_tokens` trap (the
  reasoning trace spends the budget and the visible reply comes back
  empty) does not arise on the default route. Forced `tool_choice` with thinking on
  returns 400 on the first-party endpoint (community-observed); OMH never
  writes that shape.
- **Pricing:** the approximation table carries the peak-hour list rate,
  0.30 / 1.20 per MTok (cache-miss input / output), with the cache-hit rate
  as a 0.02 `APPROX_CACHE_READ_RATIO` row (0.006 per MTok). Every rate
  halves off-peak (outside 01:00–04:00 and 06:00–10:00 UTC, Monday through
  Friday); the table cannot express a clock, so peak is the honest
  approximation for a fanout wave, and the off-peak schedule stays in the
  contract. `deepseek-flash` inherits the row through the declared
  projection; an exact operator `model-prices.json` row wins first.
- **Measured (2026-09-13, subagent block; issue #1463):** five arms on
  `benchmarks/live-model-tools/v1`, evaluation split (30 instances, corpus
  digest `c4ea899a…`), `hermes_current_session` path through the `og`
  gateway's `deepseek/deepseek-flash` (served that morning; route receipt
  recorded first), omh 2.0.3, Hermes 0.21.1, arm order baseline → family →
  optimized → optimized at `low` → revised, all on one day. Pass rate tied
  within noise (15 / 15 / 16 / 17 / 14 of 30; McNemar p = 1.0 everywhere
  but optimized vs revised at 0.5, two discordant instances), with the
  variance confined to the two search templates. Tokens decided it: the
  original override cost 3,214,839 against the inherited `deepseek` block's
  2,564,954 (+21,663 per instance, bootstrap CI95 [+6,550, +42,944], more in
  24 / 30) and about the same as no calibration at all (3,360,197). Hermes'
  session rows put it at 462 tool calls / 277 API turns versus 434 / 244 for
  the family block, with the excess in `DIAGNOSTICS`, `BUGFIX` and `RENAME`:
  the model re-ran checks on tasks it was not going to pass and
  over-verified tasks it passed either way. "The verification output" and
  "with the observed output" were the two phrases with that reading, and the
  family's "verify once, and stop" was the sentence the override had dropped;
  the block above is the revision that removes both phrases and restores the
  rule: 2,327,037 tokens (−29,593 per instance against the original, CI95
  [−50,559, −12,636]; −7,931 against the family block, CI95 [−22,782,
  +3,395], indistinguishable), 368 tool calls, 230 API turns, 14 / 30. Per
  §8 that is the "revise" outcome: the resolved-contract sentences stay, the
  requests for output are gone, and the override now costs what the block it
  replaced costs. The `low` arm (the `unspecified-low` placement) bought no
  pass and an uncertain saving against `high` (−13,286 per instance, CI
  spans zero), so the ladder stays documented, not ranked. Full tables and
  the archive path in the benchmark README. Not measured: the composer block
  (no fanout in this harness), `max`, any claim beyond this corpus.
- **Source:** official (the DeepSeek-V4.1-Flash model card on Hugging Face,
  the API change log entry of 2026-09-10, the models-and-pricing page, the
  thinking-mode guide, and the `deepseek-ai/deepseek-harness` repository's
  tool catalog and DeepSeek adapter notes, read 2026-09-11); observed (the
  Hermes Agent DeepSeek provider profile, reasoning-effort table, and
  `reasoning_content` replay path, read the same day); community
  (OpenRouter's model listing for the gateway id and the long-horizon
  characterization, and the oh-my-openagent / oh-my-pi / models.dev
  handling surveyed for divergence), each labeled as such above.

### `mistral` (Mistral Large / Medium)

- **Model trait:** efficiency-focused instruction followers. Mistral's own
  prompting guidance stresses explicit, literal instructions — the models do
  what is written, not what was implied — and their default register is
  concise. The risk profile is therefore under-specification and premature
  completion, not over-verification.
- **What OMH injects (subagent):** the stated criteria are the whole contract
  — check every one even when the change looks obviously right; concision is
  for the output, never for the evidence, and the single mandatory
  verification pass runs regardless of diff size.
- **What OMH injects (composer):** write unit prompts literally and
  completely — state every boundary, dependency, criterion, and verification
  command; never rely on the unit inferring an unstated invariant.
- **Source:** provider-published prompting guidance (explicit-instruction
  emphasis); authored fresh in the family-coverage change (#1051) — live
  benchmark validation pending.

### `llama` (open-weights Llama line)

- **Model trait:** the same model name means different capabilities on
  different hosts — tool-calling support, context window, quantization, and
  output limits are properties of the serving deployment, not the weights'
  name. Prompt shapes that assume one host's behavior silently fail on
  another.
- **What OMH injects (subagent):** treat the serving deployment as part of
  the contract — prove a capability with a real call before depending on it,
  fall back to explicit step-by-step tool use when structured calling is
  unreliable, and stop after one passing verification run.
- **What OMH injects (composer):** compose for the deployment, not the brand
  — confirm the served variant's tool contract and context budget before
  assigning units, and keep each unit prompt self-contained.
- **Source:** the open-weights serving reality (host-dependent capability is
  inherent to the distribution model); authored fresh in the family-coverage
  change (#1051) — live benchmark validation pending.

### `codestral` (Codestral coding line)

- **Model trait:** a code specialist built around completion and
  fill-in-the-middle work — strongest on concrete, file-scoped edits with
  small expected outputs, weakest on open-ended investigation and long
  synthesis.
- **What OMH injects (subagent):** work in file-scoped, concrete edits rather
  than open-ended investigation; keep each step's expected output small and
  explicit; prove the change with the repository's own check commands instead
  of prose explanation.
- **What OMH injects (composer):** route codestral units as narrow,
  file-scoped implementation slices with exact verification commands —
  investigation, review, and synthesis belong on a generalist lane.
- **Source:** provider-published specialization (completion/FIM-oriented
  coding model); authored fresh in the family-coverage change (#1051) — live
  benchmark validation pending.

### `solar` (Upstage Solar Pro)

- **Model trait:** an efficiency-positioned instruction follower
  (depth-up-scaled architecture), not a long-horizon reasoner — it executes
  an explicit plan well and degrades when asked to derive one through
  extended deliberation.
- **What OMH injects (subagent):** follow the one explicit plan you were
  given in bounded steps instead of deriving a new one; report a missing
  constraint rather than inferring it; verify once against the stated
  criteria before stopping.
- **What OMH injects (composer):** put the depth in the composition, not the
  unit — give each solar unit one explicit plan with short bounded steps,
  exact criteria, and its verification command.
- **Source:** provider-published positioning (efficient depth-up-scaled
  model); authored fresh in the family-coverage change (#1052 added the
  prefix, #1051 set the calibration bar) — live benchmark validation pending.

### `generic` (mandatory fallback — every unknown id)

- **What OMH injects:** reserve extended reasoning for genuine ambiguity with
  materially different outcomes; decide once, act, verify once against the
  criteria, and stop — speed never skips the verification pass, and
  thoroughness never repeats it.
- **Why it exists:** an unknown family must never receive *weaker* discipline
  than a known one. The generic block carries the same core stop rules as
  every family block (a test asserts this), so putting any unlisted model in
  a chain still yields a disciplined lane — what it misses is only the
  counter to its own family-specific failure mode.

### When the calibration is (and is not) applied

`calibration_for_route()` appends the calibration block **only when the routed
reasoning effort is `high`, `xhigh`, or `max`** — an exact-model override
where one exists, the family block otherwise. The calibrations exist to
counter the over-verification inertia of high-effort routes; low-effort
routes do not exhibit that inertia, and every byte rides a prepared prompt
whose worst-case assembled size is policy-gated in tests
(`UNIT_PROMPT_MAX_BYTES = 8000`) rather than truncated at runtime.

## Throughput overlays (per family, ULW-facing)

`build_throughput_overlay()` in `src/coding/throughput_prompting.py` gives
every family the base rules — batch independent tool calls and reads in one
shot, keep dependency-bound work sequential. Three advanced modes are gated to
measured family/surface pairs:

- `gpt_sol_codex_handoff` applies to a `*-sol` model on the codex profile and
  adds single-eval-cell internal parallelism.
- `gpt_hermes_ulw` applies to the gpt family on the hermes profile running
  ultrawork.
- `claude_code_handoff` applies to the claude family on the Claude Code
  profile. It adds advanced handoff and stop-condition rules but no eval
  strategy: the measured Claude Code surface exposed parallel tool use and
  agents, not a batchable eval cell.

The Claude gate comes from a 2026-08-23 counterbalanced six-pair live comparison
on Claude Fable 5 at medium effort. Both conditions received the identical
six-file independent-read task and base throughput rules; the advanced
condition added only `_ADVANCED_THROUGHPUT_RULES`. Both passed 6/6. Advanced
was faster in 4/6 pairs, with median wall time 13.28 s versus 15.82 s
(87.31 s versus 110.33 s total), and used 435,295 versus 490,034 reported
tokens. The narrow synthetic corpus does not establish general model
superiority, but it supports this exact prepared-guidance gate on the measured
Claude Code surface. The gate has not been re-measured on Fable 5.1; the
official migration guide reports that 5.1 batches fewer implied tool calls
per turn than Fable 5 in long agent loops, so the gate is stale in the
direction that favors re-running the same six-pair task at 5.1's default
`high` effort (and, for the first time, on the `hermes` profile). Until then,
claude on `hermes` keeps `parallel_handoff` and the batching counter rides the
calibration block only.

Kimi and Gemini stay on `parallel_handoff`. Credential-readiness probes on the
same host completed zero live pairs: Kimi (`opengateway` and `kimi-coding`)
and Gemini (`google`, `opencode`, and `github-copilot`) all reported
`credentials_not_configured` or `invalid_state`. Existing Kimi calibration
measurements do not compare these throughput rules, so they cannot justify an
advanced overlay. No eval strategy is claimed for either family. This is an
explicit measured availability null, not model-performance evidence; a future
gate requires a completed paired run on the intended execution surface.

## Routing, chains, and per-model bookkeeping

- **Mixture chains** — per-category ordered model chains (see the README
  model-routing section), user-editable via
  `~/.omh/routing/model-chains.json` (`mixture_chain_overrides/v1`).
- **Provider entitlements** — `~/.omh/routing/providers.json`
  (`provider_entitlements/v1`, written by the interactive `omh setup`)
  records which provider ids the machine holds and of what kind, plus the
  confirmed coding-CLI subscriptions. `effective_mixture_category_chains`
  reorders every chain so served entries lead (explicit route first, then a
  gateway serves everything, then a vendor serves the families that name
  it; unknown aliases are served). Reordering only — never removal — and
  the Maestro lane consumes the subscription entitlement separately.
- **Speed tiers are not a separate family** — `kimi-k3-ultrafast` and
  `glm-5.2-ultrafast` are the same base models served on OpenGateway's speed
  tier, and Z.ai serves its own `glm-5.3-highspeed` (gateways also use a
  `-fast` suffix): same weights, same family (`kimi-` / `glm-` prefix
  match), and therefore exactly the same calibration — only serving speed
  differs. A `-ultrafast`/`-highspeed`/`-fast` variant the chains do not
  name still projects onto its base model's category for HUD labels
  (`mixture_category_for`), so speed tiers never unlabel a lane. GLM 5.3
  Flash is the one lookalike that is NOT a tier — a separately trained
  smaller model — which is why the chains name `glm-5.3-flash` explicitly
  instead of relying on projection.
- **Cost approximation** — `APPROX_PRICE_PER_MTOK` in
  `src/plugin_bundle/omh/hermes_delegation.py` supplies `~$` estimates only
  when the host recorded no cost — no per-call figure at all, or Hermes' own
  `unknown` no-figure status (`agent/usage_pricing.py:549`, persisted into
  the usage table by `agent/turn_usage.py:236,257`), which says its pricing
  produced no amount rather than naming a billing outcome. That second case
  is what every child served through a custom gateway provider lands in. A
  row where the host DID record an outcome keeps its figure exactly,
  and models absent from the table show no approximation at all — a
  gateway child on an unpriced model still renders `$0.0000 (unknown)`,
  never a fabricated number. Cache reads are priced at a
  tenth of input unless `APPROX_CACHE_READ_RATIO` names the model: Claude
  Fable 5.1 lists $10 / $50 per MTok with cache reads at $0.25 (0.025x) and
  cache writes at $12.50 (5-minute TTL) / $20 (1-hour TTL); Opus 5 reads at
  the tenth. Mythos 5.1 carries the Fable figure because its cache-read rate
  was open at launch — approximate, like every number in the table. DeepSeek
  V4.1 Flash reads at 0.02x (cache hit $0.006 against $0.30 miss, peak).
- **`max_tokens` is a failure signal, not a stop** — a unit whose final turn
  ended on `stop_reason: max_tokens` is a failed attempt: the output was cut
  mid-thought and nothing after the cut was verified. It is never a done
  unit, whatever the partial text claims.
- **Claude 5.1 compatibility risks that live on the Hermes side** —
  observed in the local Hermes Agent checkout on 2026-09-02 and recorded
  here so nobody looks for an OMH fix: forced `tool_choice` (`any` /
  `tool`) is a 400 on Fable 5.1 and Mythos 5.1; thinking cannot be disabled
  (Mythos 400s, Fable drops the flag), which is why `omh_delegate_route`
  refuses a no-thinking effort for the Fable tier and points at `low`; an
  unset effort is sent as `medium` by Hermes while the API default is
  `high`, which is why every Claude chain row declares its effort; a
  safety decline arrives as `stop_reason: refusal` on HTTP 200 and Hermes
  tries its configured `fallback_model` once; 5.1 turns can run many
  minutes against a fixed read timeout; and the preserved-thinking
  history-editing check rejects edited history for accounts created on or
  after 2026-08-31, which affects any client-side compaction that rewrites
  earlier turns. OMH can describe these in awareness and refuse the one
  route shape it writes itself; everything else is a Hermes-side change or
  an upstream proposal.
- **Fanout dispatch credentials** — `_PROVIDER_ENV` in
  `src/coding/hermes_child_dispatch.py` maps providers (anthropic, openai,
  gemini/google/vertex, qwen, deepseek, upstage, zai, opengateway,
  openrouter, nous, azure, bedrock, …) to the environment variables a
  dispatched child needs.

## Measured: the product, not the prompt prefix

Every calibration measurement above comes from
`benchmarks/live-model-tools/v1`, which sends a synthetic task to
`hermes --oneshot` with or without one calibration block in front. That
measures a prompt prefix. It cannot measure routing, the verification gate, or
a false completion, because none of them execute on that path.

`benchmarks/product-ab/v1` is the second lane, with a different question: the
same model, reached through OMH's coding delegation instead of through Hermes
alone, on a pinned corpus of this repository's own merged pull requests,
graded by those pull requests' own tests. It reports pass rate, cost per
passed task, wall clock per goal, and false-completion rate, paired per task,
with a bootstrap CI95 on each delta and an exact McNemar test on pass rate.

**No measured run has been published yet.** The lane, its digest-locked
corpus, and its offline pilot exist; the table lands in this section when a
run exists whose records this document can point at. Until then this section
is a pointer, not a result. What the lane measures, what it deliberately does
not, and how to reproduce it: `benchmarks/product-ab/v1/README.md`.

## Coverage matrix and known gaps

| Family | Recognized | Calibrated (both tables) | Status |
| --- | --- | --- | --- |
| `gpt`, `claude`, `gemini`, `grok`, `kimi`, `glm`, `qwen`, `deepseek`, `mistral`, `llama`, `codestral`, `solar` | yes | yes | full guidance, provenance above (#1051/#1052 closed the last four) |
| `gpt-6-astra` (exact-model override) | yes → `gpt` | yes, both override tables, resolved before the family block | exact documented contract plus counters for the four official traits; paired measurement is the named follow-up |
| five declared Astra mode/tier aliases | yes → `gpt` | yes, inherited from canonical `gpt-6-astra` | bounded declared inheritance for contract, effort, calibration, provider/category metadata, and price; unknown suffixes remain missing |
| `deepseek-v4.1-flash` (exact-model override) | yes → `deepseek` | yes, both override tables, resolved before the family block | exact documented contract (three-rung ladder, no floor) plus stop-shaped counters for the documented traits; the family-vs-optimized pair is the named follow-up, blocked on a served route |
| `deepseek-flash` (declared pointer alias) | yes → `deepseek` | yes, inherited from canonical `deepseek-v4.1-flash` | the vendor's moving "current Flash" id, declared with a read date; `deepseek-v4-flash`, `deepseek-v4-pro`, and every other DeepSeek id keep the family block |
| `openai-gpt-`, `anthropic-claude-` (design-qualified aliases) | yes → `gpt` / `claude` | yes, through the design family | concrete models.dev/OpenCode serving ids carry these sub-prefixes; their catalog `base_model` fields establish the underlying design family |
| other `openai-`, `anthropic-` vendor-qualified ids | recognized as model targets, family `unknown` | no → `generic` | vendor qualification alone does not establish a design; O-series, image, and emerging ids remain uncalibrated |
| `jev` (non-generative) | yes | no, and none applies → no calibration pair | recognized, contracted (`jev-1.13.0`, with `jev-latest` and `jev-preview` as declared aliases), priced with a free output side, and in no chain by decision; the route answers `model_refused` and both chain editors refuse it. The coverage audit reports `effort`, `category_projection`, and `provider_eligibility` as `missing`, which is the honest reading for a model with no effort parameter and no chain membership — greening any of them would need an invented rung or an invented chain entry |
| `minimax` | yes | no → `generic` | prefix landed in #1304 (`MiniMax-M3`, released 2026-05-31, and `MiniMax-M2.7`, 2026-03-18, per minimax.io release notes and the platform.minimax.io model list); the calibration pair waits on an observed failure mode or a provider-stated characteristic worth countering |
| emerging families | no → `unknown` | no → `generic` | add a prefix and a calibration pair when one lands (the #1051/#1052 pattern) |

Gaps close by evidence, not by copywriting: a new calibration entry needs an
observed failure mode (or provider-stated characteristic) worth countering,
lands in both tables at once (parity-gated), and states its reason and source
in this document.

## Portfolio qualification

`omh coding model-portfolio-qualification` reports the complete supplied
inventory as `model_portfolio_qualification/v1`. It does not rank models,
change chains, infer entitlement, or call a provider. These states differ:

| State | Meaning | Example |
| --- | --- | --- |
| Supported | Present in the caller's accepted inventory, not necessarily recognized or usable on this account | `minimax/minimax-m3` in the seed fixture |
| Optimized | Model-specific guidance has documented trait-to-counter provenance; calibration presence alone does not prove improvement | `gpt-6-astra` has an exact calibration pair; consult its measurement stage separately |
| Eligible | A reviewed decision permits a particular model-and-role placement; it does not imply selection or global promotion | `kimi-k3` is eligible for `architect`, even behind another chain head |
| Recommended | An eligible placement actually appears in a shipped chain | `deepseek-flash` in `deep`, with its explicit versioned pointer relationship |
| Excluded | A reviewed decision deliberately withholds placement and records its scope and evidence | `claude-fable-5` is superseded by `claude-fable-5-1` |

New discoveries default to `unmeasured`, even if their family is recognized
or an exact contract is inherited. Family coverage describes which table
will resolve, not whether that generation has been optimized: `qwen3.8-*`
now resolves to Qwen guidance, but remains unmeasured for placement. The
existing Qwen guidance describes Qwen3-Coder; recognizing another minor
version does not establish that it shares Coder's non-thinking contract.

The independent decision registry in
`src/coding/model_portfolio_qualification.py` records existing owner-approved
editorial placements as `editorial_not_measured`, not
`observed_role_evaluation`. It approves model-and-role pairs, not every role
for a model. The guard walks categories, main-role suggestions, domain
placements, and last resort; adding an unmeasured or excluded candidate, or
an unapproved role for an existing candidate, fails. No new recommendations
are introduced by this report. New placements require role-specific observed
evidence (or an evidenced provider-diversity fallback) and a placement reason;
an arbitrary new registry entry is not a substitute for review.

MiniMax M3, MiMo V2.5 Pro, HY4 Preview, HY3, Step 3.7 Flash, Nemotron 3 Super,
and Fugu Ultra have explicit qualification holds pointing to the inventory
and the absent dedicated calibration pair. Their coverage reads
`intentional_exclusion`, their underlying resolution remains visible, and
their disposition stays `unmeasured`. A lack of research is not evidence of
poor quality, expensive execution, unreliable tools, or runtime rejection.
Holds still block required qualification. Other newly discovered ids receive
an unmeasured row without requiring a registry edit. No paid evaluation runs
implicitly; it needs separate approval through the onboarding measurement
recipe. Quality, tool reliability, latency, and cost evidence remain separate;
a documented list price is not a measured efficiency advantage.

The closed dispositions also support `eligible_not_selected`,
`excluded_quality_dominated`, `excluded_efficiency_dominated`,
`excluded_tool_unreliable`, `excluded_runtime_incompatible`, and
`excluded_superseded`. Only use a dominance disposition when its cited
comparison establishes that dimension; this pass fabricates none. Caller
`--intentional-exclusion ID=REASON` declares a runtime-contract exclusion,
not an observed model failure. Retirements carry successor, scope, and owner
decision date. Sol's retirement is frontier-slot-only: it remains recommended
in last resort. Older ids stay routable, priced, and provider-mapped.

The report imports the contract audit's inventory parser, unions `models`,
`available_models`, and discovery observations, and preserves case-variant
identities in `aliases`. Provider-qualified and bare ids remain separate
inventory rows. Served aliases are a separate field: only declared
same-mode/tier pointers share standing. Dotted `claude-fable-5.1` does not
inherit dashed `claude-fable-5-1` by similarity; Astra's unknown suffixes
stay missing. Dated snapshots retain their original projection provenance
under `declared_inheritance` coverage. DeepSeek's pointer read boundary is
explicit, not a second contract.

Supply `read_date` as an ISO calendar date when known; otherwise the report
returns null rather than inventing freshness. No current timestamp enters
the comparison or its stable JSON digest. Required ids absent from inventory
appear in `summary.required_gaps`, not invented inventory rows. Required
unmeasured rows block with exit 1; advisory gaps return 0; invalid or oversized
input returns 2. Optimization stages cite shipped metadata, research
references, calibration, placement, price, and measurement follow-up without
claiming that a cited recipe has run. See
[model onboarding](docs/MODEL-ONBOARDING.md#portfolio-qualification) for the CLI loop.

### Issue #1515 QA adjudication

The [issue contract](https://github.com/rlaope/oh-my-hermes/issues/1515),
not a stronger design-brief expectation, governs these boundaries:

- **Qwen calibration (AC6):** "`qwen3.8-*` IDs no longer fall through to
  generic handling" applies to both calibration resolvers. The dotted-minor
  alias already reaches Qwen composition guidance and the high-effort block
  through the recorded route in `fanout prepare` -> `build_unit_prompt`.
  `coding delegate` prepares a different handoff and does not invoke that unit
  renderer; absence of the unit block there is not a failed family lookup.
  Regression coverage prepares the actual fanout contract through the CLI,
  renders its unit, and compares both blocks with the shipped tables, with
  malformed-spelling negative controls. No exact contract or measured
  generation-specific optimization is inferred.
- **Unresearched families (AC7):** "MiniMax and every currently generic or
  newly discovered family receive either dedicated calibration or an explicit
  evidence-backed exclusion" must be read with "unmeasured, which must not be
  confused with either recommended or rejected" and "if it cannot run, the
  model remains `unmeasured`." The seven explicit holds above exclude
  qualification and recommendation, not model usability or quality. Their
  reasons and evidence pointers identify the missing calibration pair;
  required qualification still blocks. No `excluded_*` disposition in the
  issue's closed vocabulary means "never measured". Requiring one here would
  fabricate a failure finding. Tests retain generic resolution, explicit
  holds, empty eligibility, and absent measurement evidence.
- **Dominance and retirement (AC13):** "Models dominated on quality, tool
  reliability, latency, or cost are recorded with an explicit exclusion
  reason" does not require a nonzero dominance count. The issue also excludes
  "Fabricating quality or efficiency conclusions when live evaluation has
  not run." The seed inventory therefore has zero measured dominance
  exclusions; Astra list-price tiers alone do not establish dominance.
  Reviewable editorial retirements already exist for Fable 5 and GLM 5.2 in
  that inventory, with the owner-decision evidence in onboarding section 4.
  Sol retains its frontier-slot retirement and last-resort recommendation.
  Tests pin zero unsupported dominance findings separately from those honest,
  evidence-linked retirement decisions.

# oh-my-hermes

Shared language for oh-my-hermes, written for coding agents. Users mostly need
`omh setup`, `omh update`, and `omh doctor`; agents work across every surface
below and keep blurring which product or record owns which term — these
definitions pin that down. Product direction lives in `docs/DIRECTION.md`; the
operating contract lives in `AGENTS.md`. This file is a glossary only.

## Language

### Products

**OMH (oh-my-hermes)**:
This repo — a deterministic wrapper orchestration layer installed next to
Hermes Agent: skill catalog, router, prepared-handoff generator, and
metadata-only status surfaces. Makes no LLM, API, or network calls.
_Avoid_: Hermes plugin (that is one distribution surface, not the product),
coding executor, Hermes patch

**Hermes Agent**:
Nous Research's agent product that OMH integrates with — a separate codebase
installed on the user's machine (typically under `~/.hermes/hermes-agent`).
OMH never modifies its code and does not vendor it.
_Avoid_: Hermes runtime (that names an OMH executor path), our agent

### State roots

**Hermes home**:
`$HERMES_HOME`, default `~/.hermes` — Hermes Agent's own state root. OMH only
adds managed, explicitly installed artifacts under it (`plugins/omh`,
`tui-widgets/`, skill registration in `config.yaml`).
_Avoid_: using it for OMH runtime state

**OMH home**:
`$OMH_HOME`, default `~/.omh` — OMH's state root; managed skills and the
runtime metadata the HUD reads live here. Exactly one home is active per
invocation: the user-scope default, `OMH_HOME`, `--omh-home`, or
`--scope project`, which resolves to the repository's own `./.omh`.
_Avoid_: probing several guessed homes, Hermes home

### Skills and routing

**Managed skill**:
A generated workflow document (`skills/*/SKILL.md`) OMH installs under the OMH
home and registers into Hermes' `skills.external_dirs` so Hermes can invoke it
in chat. Generated from the skill catalog; never hand-edited.
_Avoid_: slash command, prompt template, OMC skill (that is a different
product's concept)

**Hermes skill category**:
The dashboard group Hermes shows a skill under in its startup banner and skills
list. Hermes derives it from the SKILL.md's DIRECTORY, not from frontmatter, and
only when the path relative to a registered skills dir has three or more parts —
so managed skills install at `<skills_dir>/<category>/<label>/SKILL.md`.
`hermes_skill_category()` in `src/skills/catalog.py` owns the mapping: the
skill's Hermes role, with the ULW engines carved out as `ultrawork`. The repo's
own `skills/` tap tree stays flat, because Hermes' tap lister reads only one
directory level below a tap path.
_Avoid_: `SkillDefinition.category` (the catalog's fine-grained phase field,
which Hermes never reads), capability family

**Agent Skills projection**:
A separately generated, portability-reviewed view of the catalog under
`agent-skills/`. Hosts discover installed copies in `.agents/skills/`;
both repo and user scopes also copy them into `.claude/skills/` under the same
scope root for Claude Code. This is not a
managed Hermes skill, a plugin, an RPC tool, or a runtime port. A fresh copy is
not evidence that a host loaded or executed it. See [Agent Skills](docs/AGENT-SKILLS.md).
_Avoid_: managed skill, Hermes plugin, execution adapter

**Skill catalog**:
The source of truth for every skill and its metadata (`src/skills/catalog.py`
plus render code). Skills, `docs/WORKFLOWS.md`, `docs/ROLES.md`, and the demo
cards are byte-exact projections of it.
_Avoid_: editing any generated projection directly

**Router**:
OMH's deterministic chat-intake classifier that maps a natural-language
request to a workflow, skill, or intervention using normalized phrase and
token matching. Not a model and not an LLM dispatcher.
_Avoid_: LLM router, model routing (that names executor model selection)

**Route hint**:
A non-binding recommendation of the nearest OMH workflow for a message, as
returned by `omh_recommend` or `omh recommend`. It records nothing and
authorizes nothing.
_Avoid_: dispatch, delegation, decision

### Runtime evidence

**Run**:
One recorded unit of coding work under `$OMH_HOME/runtime/runs/<id>` — the
place prepared handoffs, observations, and effect receipts about that work
accumulate.
_Avoid_: session (wrapper sessions are a different record), task

**Prepared handoff**:
OMH's output contract for coding work — a payload a coding owner may execute
later. Preparing one is not dispatch, execution, review, CI, or merge
evidence; its status is `prepared_not_observed`.
_Avoid_: run, execution, delegation result

**Observed evidence**:
A recorded observation that something actually happened (dispatch, execution,
verification, review, CI, merge), as opposed to something being prepared or
claimed. The only basis for completion claims in reports and status surfaces.
_Avoid_: treating a prepared artifact, a declaration, or an executor's
self-report as evidence

**Claim boundary**:
The sentence attached to a record stating what that record is *not* evidence
of. Every metadata artifact OMH writes carries one.
_Avoid_: disclaimer (it is a validated contract field, not prose)

**Executor progress binding**:
The metadata record that links a run or wrapper session to a live executor so
its progress events can be projected as HUD activity rows. Bindings age from
active to stale to expired; they never prove results.
_Avoid_: process handle, job

**Plan todo**:
The declared checklist HUD surfaces render above the Hermes prompt input.
Items are plan declarations; a done mark never upgrades into observed
evidence. One record per declaring session: a plan stamped with `session_ref`
lives at `$OMH_HOME/runtime/todos/<session key>.json` and renders only for
the session that owns it — the TUI widget names its own session from the
host's active-session file, the plugin tool and hooks from the session that
invoked them — so a plan declared from Slack, Discord, or a second TUI is
neither shown in nor overwritten by another session. An unstamped record
(`omh runtime todo set` without `--session`) keeps the home-wide
`$OMH_HOME/runtime/todo.json`, scoped by write time against the reading
session's start; a reader with no live TUI row to date it against (a gateway
session), or a host that cannot say which session is reading, keeps the
age-only behavior. A widget reference that names no live TUI row and owns no
record — a fresh session's transport id — reads as the most recently active
live TUI would.
_Avoid_: task list as evidence, TodoWrite (that is another product's tool name)

### Coding delegation

**Default coding lane (Hermes harness)**:
Absent an explicit coding-owner choice, coding work runs inside the Hermes
harness and no external coding CLI is selected. This is the default and the
normal path — most coding work in chat is this lane, and none of the
Maestro/handoff machinery below participates in it. The nine-term contract in
`src/coding/orchestration_vocabulary.py` pins this wording.
_Avoid_: coding handoff (that is the Maestro lane), assuming an external
executor by default, reading handoff modules as the main coding path

**Coding owner**:
The executor selected to perform coding work for a run: Codex, Claude Code, a
Hermes runtime/handoff path, or a generic executor profile. OMH language,
schemas, and reports stay neutral across all of them.
_Avoid_: defaulting to Codex in wording, the agent

**Maestro**:
The operator lane by which an external coding CLI becomes the coding owner
after an explicit user choice — `src/coding/maestro/` prepares a handoff for
the chosen CLI and never runs it. Its facade rejects the `hermes` profile
(`HermesNativeSelectionError`), so the default lane and Maestro never blur in
code; keep them separate in prose too. Prepared handoffs, executor capability
snapshots, executor prompting contracts, throughput overlays, and the handoff
sections of `wrapper-routing.md` all belong to this lane, not to the default
lane. The lane's skill-facing surface is the `ulw-maestro` engine (canonical
`maestro`), whose explicit-owner precondition is the same gate stated here.
_Avoid_: treating Maestro surfaces as the default coding path, legacy (it is
current, just not default), "coding delegation" as a synonym for all coding
work

**Fanout dispatch**:
OMH's one sanctioned execution surface — the explicit, operator-invoked
`omh coding fanout dispatch` (multi-unit) or its `omh coding run` single-run
entry (one unit, same engine, one call) that spawns local agent CLIs as
subprocesses. Nothing else in OMH executes anything.
_Avoid_: implicit execution, background dispatch

**Programmatic tool calling (`execute_code`)**:
Hermes Agent's own tool: the model writes a Python script that calls a
sandboxed subset of Hermes tools over RPC, collapsing a multi-step tool chain
into one inference turn; only the script's stdout returns to context. Part of
the default coding lane and owned entirely by Hermes — OMH neither implements
nor wraps it and may only describe it in awareness guidance.
_Avoid_: fanout dispatch (OMH's separate opt-in surface), an OMH execution
surface, code-mode batching (a Maestro-lane handoff instruction, currently
inert)

**Wrapper session**:
The metadata record of a chat-surface interaction (Discord, Slack, hosted
chat) driving OMH through the chat contract. A wrapper session is
conversation state, not coding evidence.
_Avoid_: run, transcript

### OMH surfaces inside Hermes

**OMH plugin**:
The Python bundle OMH distributes into `$HERMES_HOME/plugins/omh` — Hermes
tools (`omh_*`), lifecycle hooks, the memory provider, and the runtime reader.
A managed copy of `src/plugin_bundle/omh`, never a symlink, and enabled via
Hermes' `plugins.enabled` list.
_Avoid_: equating it with OMH itself

**Plugin tool**:
One of the `omh_*` tools the plugin registers with Hermes (for example
`omh_recommend`, `omh_hud`, `omh_todo`). Tools are metadata-only; the ones
that write touch only OMH-owned artifacts in the configured OMH home.
_Avoid_: MCP tool (the MCP bridge is a separate, allowlisted surface), shell
command

**Awareness**:
The bounded guidance the plugin's hooks inject into Hermes context so Hermes
knows which OMH tools and workflows exist and when to use them. Awareness is
instruction, never state and never evidence.
_Avoid_: system prompt, memory

**Memory provider**:
OMH's deterministic file-backed memory implementation that Hermes loads only
when `memory.provider: omh` is selected in Hermes config. Hermes runs at most
one external provider, so the key is a slot, not a list.
_Avoid_: claiming the slot when another product holds it

**HUD payload**:
The metadata-only JSON projection built by `read_omh_hud()` from OMH home and
Hermes home — plugin readiness, activity rows, the plan todo, display lines.
Status narration, never execution, review, CI, merge, or token-usage evidence.
_Avoid_: runtime state (the payload is a read-only projection of it)

### Hermes terminal surfaces

**Classic TUI**:
Hermes Agent's Python prompt_toolkit terminal UI — what `hermes` runs when
`display.interface` selects the classic REPL. It does not load user widget
files; its extension point is wrapper-CLI method hooks.
_Avoid_: treating it as the surface OMH widgets render in

**Modern TUI**:
Hermes Agent's TypeScript terminal UI — what `hermes --tui` runs, and what
bare `hermes` runs when `display.interface: tui` is set. Loads user widget
apps from `$HERMES_HOME/tui-widgets/*.mjs`. The only Hermes surface that
renders OMH's widgets.
_Avoid_: ui-tui (internal directory name), dashboard TUI

**Widget zone**:
A named slot in the Modern TUI layout where an ambient widget app renders.
`dock-top` sits above the prompt input (below the top status rule);
`dock-bottom` sits below the prompt input (above the bottom status rule).
_Avoid_: assuming dock-bottom means above the input

**OMH status widget**:
`omh-status.mjs`, the managed Modern-TUI widget file OMH installs into
`$HERMES_HOME/tui-widgets/`. It registers two ambient apps that frame the
composer like the classic REPL: a `dock-top` app renders the plan-todo
checklist above the input — its `[Plan]` header carries the transient
`parallel shot ×N` badge — closed by the frame rule above the composer,
and a `dock-bottom` app opens with the frame rule
below the input and renders the status HUD (header always visible when
installed; activity rows only during live work).
_Avoid_: statusline (that is a different, host-owned surface), HUD (the widget
renders the HUD payload; it is not the payload)

### Fault domains

**OMH install fault**:
A managed artifact under a host root is missing, stale, or was never refreshed
on this machine; the repo itself is fine. `omh doctor` proves it — it reports
plugin, widget, and managed-skill state — and `omh setup` or `omh update`
fixes it. Measure the domain before assuming one: this and the Hermes
user-config fault are cheap reads and come first, an OMH product fault needs a
clean-tree reproduction, and a Hermes-side fault comes last and only with both
of its proofs in hand.
_Avoid_: opening a PR for it, reaching for `hermes update`

**Hermes user-config fault**:
A user-owned key is set inconsistently with what the reporter expects —
`model.default`, `model.provider`, `model.base_url`, or a display choice the
operator declined to migrate. Reading the key proves it. OMH normally reports
the inconsistency and stops there. The narrow display exception is the branded
TUI choice: fresh canonical configs default to `display.interface: tui` and
`display.skin: omh`; interactive setup/update may replace canonical display
values only after a default-Yes confirmation (or `--yes`). No,
`--no-omh-tui`, and every noncanonical YAML shape preserve the existing
display choice byte-for-byte. JSON suppresses prompting but `--yes` remains
explicit consent; without `--yes`, JSON preserves explicit canonical values.
Dry-run may preview the accepted change but never persists it. Check this
fault alongside the install fault, before reproducing anything.
_Avoid_: rewriting a declined or noncanonical display choice, filing it as a
product fault

**OMH product fault**:
The behaviour reproduces from a clean install and on the repo dev tree,
independent of the reporter's machine — prove it by running
`uv run python -m omh.cli …` in a clean checkout. The fix is a repo change
with tests, which makes this the only fault domain that produces a PR.
_Avoid_: claiming it before a clean-tree reproduction

**Hermes-side fault**:
Hermes Agent's own code or built assets are genuinely behind, which is what
makes `hermes update` the answer. Two proofs are required together, never
either one alone: `git -C "$HERMES_HOME/hermes-agent" rev-parse HEAD` behind
that repo's `origin/main`, and the built Modern-TUI bundle older than its
TypeScript sources. A visual symptom that reads as "an old TUI" is almost
always an OMH product fault or a Hermes user-config fault instead — in one
real session Hermes sat at its `origin/main` HEAD with a freshly built
Modern-TUI bundle while the visual complaint was entirely valid, and the
missing chrome turned out to be an OMH product gap. OMH never patches Hermes
either way.
_Avoid_: prescribing `hermes update` from a visual symptom alone, treating one
of the two proofs as sufficient

### Repo guard vocabulary

**Byte gate**:
A CI check that compares a generated artifact byte-for-byte against its
regeneration from source (`omh docs … --check`). A one-character drift fails;
the fix is always to edit the source and regenerate, never the artifact.
_Avoid_: lint (byte gates prove provenance, not style)

**Routing corpora**:
The two named guard corpora for router changes: `ROUTING_PRECISION_CASES`
(negative controls; failure metric `overroute_count`) and
`ROUTING_INTERVENTION_CASES` (positive interventions; failure metric
`missed_intervention_count`). Every trigger change ships cases in both.
_Avoid_: underroute (that name matches nothing in the code)

**Managed artifact**:
A file OMH installs and refreshes under a host-owned root and may safely
overwrite on setup/update — the plugin bundle, the widget file, the identity
skin (`skins/omh.yaml`), managed skills, and the config keys OMH inserted.
The branded-TUI consent is the narrow exception for existing canonical config:
accepting the interactive default or passing `--yes` may set
`display.interface: tui` and `display.skin: omh` so bare `omh` and `hermes`
open the same surface. Already-active installs are not prompted. Declining,
passing `--no-omh-tui`, or using a noncanonical YAML shape leaves the display
configuration untouched. Everything else under a host root is user-owned and
preserved.
_Avoid_: overwriting anything OMH did not write without explicit consent,
rewriting a declined or noncanonical display choice

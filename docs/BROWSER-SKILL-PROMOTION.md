# Skill promotion

This page documents issue 1386 and its widening in issue 1571: turning one
reviewed source into a project-local Hermes skill under
`.hermes/skills/<skill-name>/`. There are two sources and one lifecycle:

| Source | Id shape | Reviewed by | Command |
| --- | --- | --- | --- |
| An approved, replay-passing `browser_workflow_trace/v1` | `bwt-<24 hex>` | `omh web-qa trace approve` plus a passing offline fixture replay | `omh web-qa promotion ... --trace-id` |
| A reviewed, activated `skill_draft/v1` | `sd-<20 hex>` | `omh learning skill-draft review --decision approve` | `omh learning promotion ... --source-id` |

Both mounts register the same seven subcommands and call the same functions.
Everything below — the exact-byte diff, the reviewer-bound approval receipt,
the single visibility commit, retained generations, rollback, removal, status
and explicit retry — is one implementation shared by both, and the sections
that name a trace apply to a draft with its own id in that position. The
differences are named where they occur.

The contracts are `browser_skill_promotion_plan/v1`,
`browser_skill_promotion_native_preflight/v1`,
`browser_skill_promotion_approval_receipt/v1`, `browser_skill_activation/v1`,
`browser_skill_entry/v1` or `skill_promotion_entry/v1`,
`browser_skill_resource_manifest/v1`, and the `browser_skill_promotion/v1`
status payload. The `browser_*` names are the shipped ones and are kept
deliberately: renaming them would have moved bytes that installed entries and
persisted receipts are digest-bound to.

Promotion is an operator decision on exact bytes. OMH renders the diff, a
person approves that exact diff, and one later command makes the skill
visible. Nothing here launches a browser, replays against a live site, edits
Hermes config, or grants any live mutation authority to the promoted skill.

## What a person asks Hermes

Nobody needs the commands below to get value. Describe the outcome in chat:

- "That checkout flow trace we approved last week keeps working. Make it a
  skill for this project so you stop rediscovering the buttons."
- "The site changed and the promoted checkout skill went stale. Show me what
  it would take to roll back to the previous generation."
- "Remove the promoted login skill from this repo."

Or, for a draft:

- "We reviewed and approved that receipt-reconciliation draft. Install it for
  this project."
- "Roll the reconciliation skill back to the generation we had last week."

Hermes routes to `workflow-learning` (source lifecycle and promotion receipts)
or `browser-operator` (interaction boundary and drift rule), explains what
evidence exists, and asks for the one approval it can't supply itself. The
skill it produces is project-local; a trace-derived one is hostname-scoped and
its entry text tells Hermes to stop and use ordinary browser operation on any
drift, and a draft-derived one carries the draft's own stop conditions.

## Agent and operator reference

Everything from here down is control-plane material for Hermes Agent,
wrappers, coding agents, and maintainers. None of it runs by default, on a
schedule, or in the background. There is no watcher, autoheal, reapproval, or
global fallback store.

### Prerequisites

A promotion source is a record already in this project's store.

For a browser trace:

```sh
omh web-qa trace record  --project-root . --input trace.json
omh web-qa trace approve --project-root . --trace-id bwt-<24 hex> --digest <sha256>
omh web-qa trace replay  --project-root . --trace-id bwt-<24 hex> --observation observation.json
```

See [Browser workflow traces](BROWSER-WORKFLOW-TRACES.md). Promotion resolves
the trace through `resolved_browser_workflow_promotion_reference`, which
requires `lifecycle_status: approved` and `replay_status: passed` with the
current trace digest, revision, origins, output schema, fixture digests, and
replay digest. A well-shaped trace dictionary that isn't in
`.omh/web-visual-qa/traces/` is not a source.

For a skill draft:

```sh
omh learning skill-draft new --scope project --name <slug> "turn this into a skill: ..." ...
omh learning skill-draft review --scope project <sd-id> --decision approve
```

Promotion reads the draft from `<project root>/.omh/learning/skill-drafts/`,
which is where `--scope project` writes it, and requires the record to validate
as `skill_draft/v1`, to be in the `active_proposal` state a human approval
produces, and to pass its own generated-output checks. A draft in the user-scope
OMH home is not a source for a project: the lane refuses `--omh-home` as a
fallback for its source exactly as it does for skills, receipts and state.

`--project-root` must resolve to an observed Git root. It defaults to `.` for
the promotion commands, and running outside a Git root fails; `--omh-home` is
never a fallback location for skills, receipts, or state.

### Commands as shipped

Observed from `omh web-qa promotion --help` and `omh learning promotion --help`
and their seven subcommands. The two mounts differ only in how the source is
spelled: `--trace-id` on `web-qa`, `--source-id` on `learning`, both landing on
the same handler.

```sh
omh learning promotion diff     [--project-root PATH] --skill-name NAME --source-id ID [--operation {install,update}]
omh learning promotion approve  [--project-root PATH] --skill-name NAME --source-id ID --reviewed-diff-digest SHA256 --reviewer ID [--operation {install,update}]
omh learning promotion promote  [--project-root PATH] --receipt-id SHA256
omh learning promotion status   [--project-root PATH] --skill-name NAME [--no-source-check]
omh learning promotion rollback [--project-root PATH] --skill-name NAME --generation SHA256 [--reviewed-diff-digest SHA256 --reviewer ID]
omh learning promotion remove   [--project-root PATH] --skill-name NAME [--reviewed-diff-digest SHA256 --reviewer ID]
omh learning promotion retry    [--project-root PATH] --receipt-id SHA256

omh web-qa promotion diff       [--project-root PATH] --skill-name NAME --trace-id ID [--operation {install,update}]
# ... and the same six others, with --trace-id in place of --source-id.
```

An exit status of `0` from any of them says the lane did not end with the entry
refused or deactivated. `stale`, `quarantined` and `unverified_managed_state`
exit non-zero, because in each of them nothing of yours is installed; `inactive`
exits `0`, because a project that never promoted this skill is not a promotion
that failed, and so do `removed` and `already_deactivated`, which are what
`remove` was asked for. A refusal inside the lifecycle raises and exits `2`.

| Subcommand | What it does | What it never does |
| --- | --- | --- |
| `diff` | Renders the exact `browser_skill_promotion_plan/v1` (package bytes, unified diff, `diff_digest`) and runs the native preflight. Writes nothing. | Approve, stage, or activate. |
| `approve` | Re-renders the plan, requires `--reviewed-diff-digest` to equal the current `diff_digest`, and persists one immutable approval receipt. | Touch `.hermes/skills`. |
| `promote` | Performs the single visibility commit for one receipt: stages the immutable generation, writes the activation index, then atomically replaces `SKILL.md`. | Run without a receipt, promote two receipts, or retry on its own. |
| `status` | Derives the truth from actual entry, index, and receipt bytes, rechecks the source trace, and deactivates a drifted entry. | Repair, reapprove, reactivate, or consult a mutable "active" record. |
| `rollback` | Without `--reviewed-diff-digest`/`--reviewer`: renders the rollback diff to a retained generation. With both: persists a rollback approval receipt. | Roll back from the review call alone. |
| `remove` | Without the pair: renders the removal diff. With both: persists a removal receipt. | Delete anything but a verified managed `SKILL.md`. |
| `retry` | Explicitly resumes an approved promotion whose immutable staging completed but whose `SKILL.md` commit never happened. | Run inside `status`, `promote`, or any background path. |

`rollback` and `remove` refuse `--reviewed-diff-digest` without `--reviewer`
and vice versa: the pair is what turns a review into an approval. `--reviewer`
accepts 1 to 128 characters from `A-Za-z0-9._@:-`. `--skill-name` is a
lowercase slug of 3 to 49 characters starting with a letter. `--generation`,
`--receipt-id`, and every digest are 64 lowercase hex characters. Every
refusal raises an `OmhError` with the reason; an I/O failure can leave staged
or already-replaced files, so inspect status before any explicit retry.

All output is JSON. `diff` returns `plan`, `native_preflight`, and
`native_preflight_digest`; `approve` returns the receipt; `promote`, `status`,
and `retry` return the `browser_skill_promotion/v1` status payload.

### What a draft source changes, and what it does not

Only five things differ by source kind. Everything else on this page — the
generation formula, the exact-byte diff, the native preflight, the receipt, the
single visibility commit, retained generations, rollback, removal, status,
drift deactivation, retry, locking and the file-sync rules — is one shared
implementation.

1. **How the id resolves.** `bwt-` reads the trace store and requires an
   approved, replay-passing promotion reference; `sd-` reads the project draft
   store and requires an activated `skill_draft/v1`.
2. **The entry's metadata line.** A trace writes `omh_browser_promotion:` with
   `browser_skill_entry/v1`; a draft writes `omh_skill_promotion:` with
   `skill_promotion_entry/v1`, holding the draft id and digest rather than
   origins, output schema and replay digest, which a draft does not have. The
   `description` is the draft's own summary on one line.
3. **The third immutable resource file.** `trace.json` for a trace,
   `draft.json` for a draft. `entry.md`, `procedure.md` and `manifest.json` are
   the same four-file generation either way; a draft's `procedure.md` renders
   the draft's preconditions, declared inputs, fixed instructions, stop
   conditions and required verification.
4. **The pattern risk review**, below. Draft sources only.
5. **What drift means.** A trace drifts when its promotion reference stops
   resolving as approved and replay-passing, and can be `quarantined`. A draft
   drifts when its record stops resolving as an activated proposal — a review
   later changed to `revise` or `reject` deactivates the entry it approved on
   the next `status` — and is always `stale`, never `quarantined`.

The approval receipt's `trace_id`, `trace_revision`, `trace_digest` and
`fixture_digests` keep their shipped `/v1` names. For a draft receipt they hold
that draft's id, revision `1`, its content digest and an empty map. The names
were kept rather than widened so a receipt written before this change still
reads; `receipt_source_binding` in `src/workflows/skill_promotion_source.py` is
the one place either kind fills them.

### Pattern risk review: draft sources only

Before a draft's package is rendered, `diff` scans the draft's own summary and
instruction text with the detectors `omh ops plugin-risk-audit` uses, and
refuses the promotion naming the category it matched:

```
skill draft pattern risk review refused promotion; the draft text matches: process_execution
```

The categories are the audit's: `process_execution`, `dynamic_code_execution`,
`network_request`, `potential_committed_secret`, `hermes_hook_capability`,
`declared_dependency`. The refusal happens in the plan, so `approve` and
`promote` re-run it against the same bytes the reviewer saw; the gate is a pure
function of the draft record, and the record's digest is what the receipt binds.
A passing scan is reported on the plan as `pattern_risk_review`, with an empty
`risk_categories` list and its own claim boundary: it says a pattern does not
occur in this text, never that the skill is safe.

A trace source does not run this gate. Its content is machine-captured and
redacted before it is ever approved, whereas a draft's instruction text is free
text a person wrote and the text Hermes will follow once the skill is visible.
Both kinds still run the Hermes `skills_guard` scan inside the native preflight,
and a `dangerous` verdict refuses either.

The reviewer-judgment form of the same question — what a borrowed third-party
skill pattern is worth, what it costs, how far the evidence goes, and what OMH
may reproduce natively — is `omh ops skill-pattern-risk-review`, which mints one
`skill_pattern_risk_review/v1` over an explicit local directory. It cites the
scan and decides nothing: a clean scan never reads as an approval, and only a
recorded reviewer decision approves anything, which is approval to build a
native pattern rather than to adopt the source.

### Review: the exact diff and the native preflight

For a trace, `diff` builds the package from the approved trace and a generic
skill draft:

- `SKILL.md`: front matter with `name`, a picker `description` of the form
  `Use <origins> browser workflow.` (refused over 60 characters), and one
  `omh_browser_promotion:` line carrying `browser_skill_entry/v1` metadata
  (activation id, generation, previous generation, rollback target, trace
  id, digest, revision, origins, fixture digests, generic draft digest, output
  schema digest, replay digest, resource paths). The body tells Hermes to
  verify the generation is still active and its trace still approved before
  demand-loading anything, to use the skill only for the listed hostname, and
  to stop on drift. The whole entry must stay under 8 KiB.
- `resources/<generation>/entry.md`, `procedure.md`, `trace.json`, and
  `manifest.json`: the immutable generation. `procedure.md` states the allowed
  origins, digests, expected output schema, and that the replay proof is
  offline fixture simulation only.

`generation` is a digest of the activation id and payload digest, so
re-rendering the same trace revision against the same base bytes produces the
same generation, and a different base (an update over an active skill) does
not. The
`diff` field is a unified diff from the currently managed bytes to the desired
bytes, path by path, and `diff_digest` is its SHA-256. An empty diff (the same
trace already active) reports `operation: unchanged` and the empty-string
digest; approving it requires exactly that digest.

The preflight is the one place promotion leaves the OMH interpreter, and it is
read-only. `HermesPromotionNativeHost` runs the shipped
`browser_skill_promotion_native_probe.py` under the installed Hermes venv
Python with the Hermes source checkout as its working directory, a
`HOME`/`HERMES_HOME`/`HERMES_MANAGED_DIR`/`PATH`-only environment, a 512 KiB
request bound, and a 30 second timeout. It can't run caller-supplied commands.
The probe stages the package in a temporary directory and returns:

| Field | Meaning | Refused when |
| --- | --- | --- |
| `trusted` | Hermes' own `is_project_root_trusted` for this root | `false`; promotion never auto-trusts a project |
| `structure_error` | Hermes skill front-matter validation | not `null` |
| `lint_errors` | Hermes skill linter errors | non-empty |
| `security_verdict` | Hermes `skills_guard` scan of the staged package | `dangerous` (`safe` and `caution` pass) |
| `policy` | `skills.write_approval` as Hermes resolves it | see below |

`policy` is a record, never a grant boolean. When `skills.write_approval` is
`true`, the probe reports `requirement: required`, `approval: not_obtained`,
`support: unsupported`, and promotion stops with "native skill-write approval
is required but unsupported for project-local promotion". OMH doesn't
implement or fake that approval. When it's `false`, the probe reports
`requirement: not_required`, `approval: not_applicable`, `support: available`;
that means no native approval applies to this write. It is not an approval and
doesn't replace the operator's reviewed diff digest. The probe evaluates a
private copy of the config bytes, refuses if those bytes change under it or
fail to parse, and binds the result in `policy.revision`. An unavailable probe,
missing Hermes venv, or malformed response is "native write policy is
unavailable", also a stop.

### Approval receipts

`approve` re-runs the full review, compares `--reviewed-diff-digest` to the
current `diff_digest`, takes the receipt lock, re-runs the review again under
the lock, and only then writes
`.omh/browser-skill-promotions/receipts/<receipt_id>.json` (mode `0600`).
The `receipt_id` is the digest of every other field, so a receipt binds:
operation, activation id, payload digest, rollback target, base package and
base entry digests, previous generation, reviewer, reviewed diff digest,
project root and identity, target path, trace id/revision/digest, fixture
digests, generic draft digest, generation, package/entry/manifest digests,
native preflight digest, and policy revision. Anything that shifts between
review and approval, including the trace, target bytes, lint or scan result,
trust decision, or policy revision, is "promotion review changed before
approval was persisted". A receipt for another project root is refused on
read.

Every operation gets its own receipt. An `update` (a different trace over an
active skill), a `rollback` to a retained generation, and a `remove` each
render their own diff and need their own `--reviewed-diff-digest` and
`--reviewer`. There is no blanket approval.

### Activation: one visibility commit

`promote --receipt-id` reads the receipt, resolves the skill name from its
target path, and takes two private locks: one on the source trace file, one on
the skill's state directory. Under both it:

1. Re-verifies the managed inventory: `.hermes/skills/<name>/` may contain only
   `SKILL.md` plus complete `resources/<generation>/` sets that a validating
   receipt owns. Any unmanaged file refuses the whole operation.
2. Re-renders the plan from the receipt and checks every bound field. A stale
   receipt is "promotion approval is stale at <field>".
3. Stages the four generation files under
   `.omh/browser-skill-promotions/<name>/staging/<activation_id>/`, reads them
   back, then writes them into `resources/<generation>/` with `O_EXCL`. Files
   are never overwritten; an existing file with different bytes is a refusal.
4. Re-resolves the source, policy, and base immediately before the visible
   write.
5. Writes the activation index
   `activation-by-entry/<entry_digest>.json` (`browser_skill_activation/v1`),
   then writes and fsyncs a temporary `SKILL.md` and publishes it with
   `os.replace`. POSIX additionally fsyncs the containing directory.
6. Reads `SKILL.md` back and validates it against the index and receipt before
   reporting `active` (or `rolled_back` for a rollback receipt).

That `SKILL.md` replace is the only moment Hermes can see the skill. Everything
before it is private staging or immutable history. An interruption after
resource staging but before entry replacement leaves the previous entry (or
none on first install) visible; `retry --receipt-id` accepts retained resources
only byte for byte.

Reading the truth is O(1) by construction: `SKILL.md` names its generation;
`resources/<generation>/entry.md` must equal it; the entry digest names one
activation index file; the index names one receipt; the receipt's digests must
match the entry, manifest, package, project identity, and trace metadata. No
scan over history and no mutable active-state record is consulted. A
hand-edited `SKILL.md`, a swapped resource file, or a missing index makes the
skill unverified, not "probably active".

`promote` on a receipt that's already the active one re-checks the source and
returns the existing status without writing.

### File sync, atomic visibility, and platform limits

The lifecycle writes regular files with binary descriptors and calls
`os.fsync` before closing and verifying their exact UTF-8 bytes. This covers
immutable resources, activation indexes, temporary entries and observation
records. File-sync failures propagate on every platform; they are not treated
as successful promotion. A failed staging-file sync precedes entry visibility.

The same-directory `os.replace` remains the single atomic entry-visibility
operation for install, update and rollback. Removal unlinks only the verified
managed entry. These operations are not a transaction over the whole package.
On POSIX, the existing directory open/fsync after entry replacement, unlink,
and observation replacement remains enforced, and its failures propagate.
A failure after entry replacement can therefore leave an already-visible
entry even though the command reports an error; status revalidates actual bytes.

On Windows, Python's `os.fsync` calls the CRT `_commit` to flush regular files.
CRT `_open` rejects directory paths with `EACCES`, so OMH does not attempt the
additional POSIX directory flush there. It does not substitute SQLite
semantics, a volume flush, or an unsupported native directory-handle operation.
Regular-file flush, atomic replacement and byte/digest readback do **not**
prove that directory entries, renames or removals survive power loss. OMH
makes no Windows directory-entry durability or all-files transactional
power-loss guarantee; atomic visibility is distinct from crash durability.

Platform references:
[Python `os.fsync`](https://docs.python.org/3.11/library/os.html#os.fsync),
[Python `os.replace`](https://docs.python.org/3.11/library/os.html#os.replace),
[Microsoft `_commit`](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/commit?view=msvc-170),
and [Microsoft `_open`](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/open-wopen?view=msvc-170).

### Status and drift

`status --skill-name` derives one of:

| Status | Meaning |
| --- | --- |
| `active` | Verified entry, index, receipt chain; source trace still approved and replay-passing with matching digests. |
| `active_unchecked` | Same chain verified, but `--no-source-check` skipped the trace re-resolution. This is a projection for inspection; don't treat it as proof the skill is usable. |
| `stale` | The chain verified but the trace has drifted or is no longer approved/replay-passing. `status` unlinked `SKILL.md`. |
| `quarantined` | As `stale`, and the trace lifecycle is `quarantined`. `SKILL.md` unlinked. |
| `removed` | A removal receipt was promoted, or a repeat after removal. |
| `rolled_back` | A rollback receipt was just promoted. |
| `inactive` | No `SKILL.md` and no prior observation. |
| `unverified_managed_state` | A `SKILL.md` exists but doesn't validate as a managed browser skill. OMH won't touch it. |

Drift disables only the managed entry. When `status` (or `promote` for an
already active receipt) finds the source trace changed, unapproved, or
quarantined, it unlinks `.hermes/skills/<name>/SKILL.md` under both locks and
records the reason. Every `resources/<generation>/` set, the activation
indexes, and the receipts stay. Nothing is reapproved, regenerated, or rolled
back automatically; the way forward is a fresh `diff`/`approve`/`promote`
against a re-approved trace, or a reviewed `rollback` to a retained generation
whose trace still resolves.

The status payload (`browser_skill_promotion/v1`) carries `status`,
`skill_name`, `project_root`, `generation`, `receipt_id`, `promoter`,
`lineage` (`previous_generation`, `rollback_of`, `trace_id`), `reused`,
`reason`, and `deactivated`. OMH persists a `last-observation.json` only for a
committed transition; read paths are projections and never replace checked
state with unchecked state.

### Rollback and removal

`rollback --generation <retained generation>` needs an active skill and a
retained generation whose own trace still resolves as approved and
replay-passing. The review renders the diff from the active entry to that
generation's entry; approval with the pair persists a receipt with
`operation: rollback` and `rollback_of` set; `promote --receipt-id` then
performs the same single visibility commit. Old generations are retained,
never rebuilt from a whole-directory replacement, so a rollback target is
exactly the bytes that were active before.

`remove` needs a verified managed `SKILL.md`. The review's diff is the deletion
of that file; approval persists an `operation: remove` receipt bound to the
current entry digest; `promote` re-checks the base and unlinks only
`SKILL.md`. Resources and receipts remain as history. A repeat after removal,
or a removal request when drift already deactivated the entry, reports
`removed`/`already_deactivated` and writes nothing. An unmanaged `SKILL.md` is
never removed.

### Where state lives

| Path | Contents |
| --- | --- |
| `.hermes/skills/<name>/SKILL.md` | The only Hermes-visible artifact. |
| `.hermes/skills/<name>/resources/<generation>/` | Immutable `entry.md`, `procedure.md`, `trace.json`, `manifest.json`. |
| `.omh/browser-skill-promotions/receipts/<receipt_id>.json` | Immutable approval receipts. |
| `.omh/browser-skill-promotions/<name>/activation-by-entry/<entry_digest>.json` | Activation index, one per committed entry. |
| `.omh/browser-skill-promotions/<name>/staging/<activation_id>/` | Private pre-entry staging. |
| `.omh/browser-skill-promotions/<name>/last-observation.json` | Last committed transition. Not consulted for activation. |
| `.omh/web-visual-qa/traces/<trace_id>.json` | The source trace; also the source lock during promotion. |
| `.omh/learning/skill-drafts/<draft_id>.json` | The source draft; also the source lock during promotion. |

Every path is checked against symlinks at each component, every read is a
bounded regular-file read (256 KiB per file, 512 KiB per package, 128 files),
and all writes are `O_EXCL` or temp-file-plus-replace with fsync.

### What promotion does not authorize

The promoted entry says it itself: promotion grants no live mutation
authority. The skill's offline replay proof is fixture simulation, not
evidence of a live browser result or external effect. Submission, upload,
payment, credential entry, and destructive actions still need current host and
effect authorization at the moment they're requested, and are never retried
automatically. Captured labels and trace data are data, not instructions.

Promotion is also not observation evidence. Importing a
`web_qa_observation_run/v1` that cites a `browser_workflow_trace_reference/v1`
(see [Web QA observations](WEB-QA-OBSERVATIONS.md)) doesn't promote the trace,
and promoting a trace doesn't change any stored observation verdict.

### Related skills

- `workflow-learning` names the promotion receipt as an artifact and carries
  the recovery note for `required` policy, drift, and explicit retry.
- `browser-operator` names the diff/approve/promote chain and the "drift
  unlinks only `SKILL.md`" rule beside the native collector boundary.

The skill bodies stay deliberately short; the full status table, state paths,
and refusal reasons live only on this page.

Both bodies are generated from `src/skills/catalog_feature_surfaces.py`;
regenerate rather than hand-edit.

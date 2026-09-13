# Agent Skills projection delivery

**2026-09-13 review-round-1 correction:** [Blocker fixes](review-round-1/delivery.md)
preserve unowned custom Claude skills during implicit Hermes source imports and
admit `omh-docs` as requires-omh-cli. Current producer counts in `counts.json`
are 85 portable, 15 requires-omh-cli, 23 Hermes-only installable skills, emitting
100 skills and 57 references. The original counts and omh-docs exclusion below
are historical and superseded.

**2026-09-13 Phase B correction:** The initial repo-only `.agents/skills/`
assumption below is superseded by [repo mirror delivery](repo-mirror/delivery.md).
Both scopes now copy into `.agents/skills/` and `.claude/skills/` at their scope
root. The original test/QA records below remain historical evidence, not a claim
that Claude Code discovered the initial repo layout.

Task st_01a09854; branch `agent/agent-skills-projection`, base `e2844979`.
Implemented the binding design brief plus the owner's user-mirror/description
amendment. No merge or push was performed.

## Outcome

- 99 projected skills, including all eight requested ULW engines, and 57 portable
  references. `ulw-maestro` is absent.
- Of 123 installable catalog skills: **85 portable, 14 requires-omh-cli, 24
  hermes-only**. Including five reference/retired catalog surfaces, the full
  128-entry table has **85 / 14 / 29** respectively. Counts and per-class names
  were derived from the producers and are captured in `counts.json`.
- Repo installation generates at the Git root's `.agents/skills/`. User
  installation creates independent copies in `~/.agents/skills/` and
  `~/.claude/skills/`, with the same v1 manifest covering both destinations.
- Hermes templates are byte-pinned; `skills/*` and `docs/WORKFLOWS.md` have no
  changes. No dependency was added.

## Commits

1. `31ea1cd2aa84b2bab20c5be69586b065da7c6791` - catalog classification, target-aware
   rendering, committed projection/CI byte gate, install/status, source-import
   isolation, and tests. Tests and implementation are committed together as
   permitted by the brief; RED output is retained separately.
2. Documentation/navigation and this evidence package are the subsequent
   `docs: document Agent Skills host boundaries and delivery evidence` commit.
   Its exact hash is available in the final task report and `git log` (a commit
   cannot contain its own final hash). Both commits carry all seven Lore fields
   and `Signed-off-by: rlaope <piyrw9754@gmail.com>`.

## Requirement-to-evidence map

| Requirement | Implementation / captured evidence |
| --- | --- |
| Read binding brief and repo contracts before production edits | `design-brief.md`; `scope-amendment.md`; `diagnostics.md` records navigation-tool limitations |
| One fail-closed classification table and accessors | `src/skills/catalog_portable.py`; per-skill comments cite catalog fields; `catalog-evidence.json` captures why/inputs/outputs/checklist for every installable definition; `catalog-prose.json` captures remaining relevant definition fields |
| Every portable or CLI-dependent installable workflow emitted, in catalog order | `portable_skill_names()` and `agent_skill_templates()`; R4 equality and fail-closed checks in `green.log`; `counts.json` lists every member |
| Explicit ULW portable overrides; no hidden execution | `PORTABLE_OVERRIDES`; host-neutral delegation, durable plan records, native-goal exclusion, QA/accounting boundaries; generated readbacks `qa-ulw-work.md`, `qa-omh-frontend.md` |
| Verify loop implementation before classifying | `src/commands/loop.py` -> `create_loop_cycle` / status functions in `src/workflows/goal_loop.py` -> capability-gated `select_driver` in `loop_driver_contract.py`; `loop-proof.log` captures real start/status with no Hermes home, exit 0, and cleanup |
| Target threaded through all six named render seams | `workflow_skill_from_definition`, `_workflow_full_body`, `_common_rail_sections`, `_quality_rubric_sections`, `_skill_metadata_block`, `_frontmatter` in `src/skills/render.py` |
| Keep metadata.hermes; omit native framing/metadata rows/backend rails | Renderer target branches; R2/R3 and all-file forbidden-token checks in `green.log`; 297 external validations in `qa.json` |
| Hermes byte-identical PIN before and after | R5 passes in `red.log`; `hermes-before.json` equals `hermes-after.json`; committed test fixture; R5 in `green.log` and `full-suite.log`; empty Hermes-only diff in `gates.log` |
| Committed Agent Skills tree and byte gate with missing/extra detection | `agent-skills/`; `omh docs agent-skills --check` in `gates.log`; test checks missing, extra, changed and CRLF bytes; `.github/workflows/ci.yml` registers gate |
| Portable references only, no dangling emitted reference paths | Explicit `PORTABLE_REFERENCE_PATHS`; reviewed boundaries in `reference-boundaries.txt`; all-file path-resolution and shipped-copy equality test in `green.log` |
| Install from packaged catalog, not repository templates | `src/install/agent_skills_projection.py`; `qa.py` builds isolated wheel-installed CLI and runs from temp directories; `qa.json` records argv, cwd, output, and exit |
| Repo/user paths and fail-closed repo behavior | Q1/Q2 and `outside_repo_fail_closed` in `qa.json`; tests cover nested cwd, outside repo, explicit scope requirement, and dry-run/status read-only behavior |
| v1 manifest, catalog revision, file SHA256 and freshness | R7/R9 plus stale-revision, missing-mirror, retired-file tests in `green.log`; Q2 manifest and Q4 before/after statuses in `qa.json` |
| User Claude Code mirror, copy not symlink, same manifest | R9 RED in `red.log`; R9 GREEN in `green.log`; Q2/Q4 verify both roots, equal manifest bytes, copy bytes, no symlinks, and mirror local-modification repair |
| No writes to unchosen roots or vendor/config surfaces | Target roots come from invocation, never manifest; traversal/symlink/unowned-collision tests; GIT_* redirection regression and bounded read-only Git probe; Q2 confirms no Hermes/OMH config home created |
| Description 1-1024 guard, target-only tail trimming | R10 RED in `red.log`; bounded emitted descriptions, synthetic over-budget tail, oversized base rejection in `green.log`; Hermes PIN unchanged |
| Six-host docs and non-portable capability boundaries | `docs/AGENT-SKILLS.md`; research input `hosts/matrix.md` and companion reports; Cursor user-scan and OpenClaw state-root caveats explicitly retained |
| Docs navigation and generated-artifacts map | `src/catalogs/documentation_navigation.py` required/public entry; docs/README link; CLAUDE map row; CONTEXT glossary entry; navigation gate exit 0 |
| Exact-count fixtures derived rather than guessed | No existing installable/routing totals changed (producer still 123); full suite passed its exact-count fixtures. New counts come from `counts.json`; process/Git registries were extended with explicit command authority, not suppressed |
| RED-first R1-R8, plus owner amendment | Exact commands in `red-scenarios.md`; expected failures and passing R5 in `red.log`; R9/R10 also preceded production edits |
| Additional integration cause fixed, not hidden | `full-suite-initial.log` retains the three initial failures; R11/R12 in `red.log`; converter excludes sibling portable packs while explicit roots work; new read-only Git authority is registered in policy tests |
| Focused tests | `green.log`: 61 tests, OK (includes 16 new projection tests, source layout and handoff safety tests) |
| Full suite | `full-suite.log`: `PYTHONPATH=tests uv run python -m unittest discover -s tests -v`; 11,557 tests in 1017.843s, OK (skipped=3) |
| Ruff, compileall, every docs gate, build, whitespace | `gates.log`: all 15 commands exit 0, including nine docs --check gates plus skill-lint, Ruff, compileall, whitespace, Hermes-only diff, and wheel/sdist build |
| Q1: repo install, exact names, readbacks, forbidden tokens | `qa.json` Q1; `qa-ulw-work.md`, `qa-omh-frontend.md`; grep exit 1 means zero forbidden-token hits |
| Q2: isolated HOME and shared mirror manifest | `qa.json` Q2 records the manifest, both paths, copied-file coverage, and absence of Hermes/OMH home mutations |
| Q3: external validation of both scopes and mirror | `qa.json` Q3: 297/297 validations pass (99 skills x 3 destinations), exact npx commands captured |
| Q4: local modification -> status -> explicit reinstall -> fresh | `qa.json` Q4 includes repo and mirror modified/fresh status payloads |
| Q5: verification and cleanup | `qa.json` Q5 maps to full-suite/gates/focused/PIN logs; `cleanup.removed=true` covers temp repo, HOME, mirror, wheel venv, and npm cache |
| Commits and clean worktree | Feature hash above; subsequent documentation/evidence commit has DCO/Lore trailers; final `git status --porcelain` is checked after commits and reported in the task result |

## Loop and additional Hermes-only boundaries

Loop CLI start/status work without a Hermes runtime. The fallback `hermes_goal`
record is still prepared metadata; native activation and contiguous-session
observations are not fabricated on another host. External driver control is
available only with actual session-bound capability evidence. The portable host
otherwise owns execution and its continuation ledger; OMH goal completion keeps
its existing observed-evidence gate.

Beyond obvious routing/maestro/doctor/native setup surfaces, these exclusions
are deliberate and documented per skill in the table:

- `omh-docs`: current product/local-install diagnostic scope still includes the
  Hermes installation and native capability surfaces.
- `omh-agent-evaluation`: current catalog output is paired-run evaluation with
  Hermes-child receipt semantics, not a generic rubric-only evaluator.
- `omh-model-optimization`: recognition and calibration flow targets
  Hermes/Maestro model routing configuration.
- `omh-research-department` and `omh-automation-blueprint`: Hermes profile,
  cron/recurring-intent, and delivery composition.
- `omh-morning-brief`: Hermes MCP connection configuration is part of its setup
  contract, not merely a source-bounded briefing.
- `omh-prompt-import-readiness`: its target is Hermes slash-command exposure.
- `omh-workflow-learning`: native browser promotion/visibility is an explicit
  catalog artifact requirement.
- `omh-memory-sync`, `omh-agent-board`, `omh-browser`, and `omh-achievements`
  depend on native memory, RPC, browser admission, or plugin artifact ownership.

The complete 24-member installable boundary list is in docs/AGENT-SKILLS.md and
`counts.json`; five non-installable reference/retired surfaces also fail closed.

## Deviations and verification limits

1. The owner's amendment intentionally replaces the original single user root
   with two copy roots and adds description bounds. This is authorized scope,
   recorded in `scope-amendment.md`, not an implicit extension.
2. `skills-ref@0.1.5 validate` validates one skill directory, not a collection.
   The collection-root attempt correctly returns missing SKILL.md; every child
   is then validated at all three roots. `qa.json` preserves both outcomes.
3. LSP rejected sibling-worktree paths because its request cwd remains the
   parent checkout. No LSP cleanliness is claimed; available static, syntax,
   test, build, byte, and real-surface validators all ran on the correct tree.
4. The full-suite integration run required source-discovery isolation and
   explicit Git-process policy registration. These are necessary coexistence
   fixes caused by adding the second projection, not unrelated refactors.
5. Tests/implementation share one feature commit; documentation/navigation and
   captured evidence form the second commit. No RED commit was required by the
   brief, and the original failures remain recorded.
6. No six-host live skill nomination, model use, or execution is claimed.
   Real-surface QA covered the installed wheel CLI and skills-ref on macOS.
   Existing suite skips cover the Linux dumpability syscall, a case-sensitive
   path collision unavailable on this filesystem, and Windows Job cleanup.

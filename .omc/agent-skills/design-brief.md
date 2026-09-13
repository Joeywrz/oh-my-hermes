# Design brief: portable Agent Skills projection (owner-approved scope)

Base: origin/main e2844979. Worktree: oh-my-hermes-worktrees/agent-skills-projection, branch agent/agent-skills-projection.

## Owner decisions (binding)
- Scope: ulw family FIRST (ulw-work, ulw-plan, ulw-loop, ulw-qa, ulw-research, ulw-perf, ulw-context, ulw-interview; ulw-maestro stays Hermes-only) + portable omh- domain skills (omh-frontend etc.).
- Install location: BOTH repo (.agents/skills/) and user home (~/.agents/skills/).
- Hermes-runtime capabilities (maestro, wrapper routing, omh hermes/chat surfaces) are NOT ported.
- No merge without explicit owner instruction.

## Standard facts (verified 2026-09-13)
- Agent Skills open standard (agentskills.io): SKILL.md + frontmatter name/description; optional license/compatibility/metadata/allowed-tools; scripts/references/assets dirs; unknown frontmatter keys ignored by other hosts.
- Codex scans repo `.agents/skills/` and `~/.agents/skills/`; Claude Code reads `.claude/skills` + `~/.claude/skills` and the shared `.agents/skills` convention; Cursor adopted the same standard. Target dir for the projection install: `.agents/skills/` (repo scope) and `~/.agents/skills/` (user scope).
- Validator: `npx skills-ref validate <dir>` (available on this machine).

## Architecture

### 1. Catalog data: portability classification (new module src/skills/catalog_portable.py)
- `PORTABILITY_PORTABLE`, `PORTABILITY_REQUIRES_OMH_CLI`, `PORTABILITY_HERMES_ONLY` constants.
- `_PORTABILITY: dict[str, str]` — ONE mapping table (not 123 edited literals); unlisted = hermes-only (fail-closed).
- Accessor `skill_portability(name) -> str`; `portable_skill_names() -> tuple[str, ...]` sorted in catalog order.
- v1 classification rule: emit every skill not hermes-only. ulw engines are classified portable/requires-omh-cli ONLY with a portable override (below) that removes the Hermes-coupled step; if a step cannot be honestly retargeted (e.g. `omh loop` turns out Hermes-session-coupled — VERIFY what src/commands loop does at implementation time), that skill stays hermes-only in v1 and the boundary is documented.
- Classification evidence source: catalog definitions (why_this_exists/required_inputs/expected_outputs/final_checklist), NOT generated SKILL.md greps. Explorer pre-classification is in .omc/agent-skills/classification.md (from st_01a09853); the implementer FINALIZES per-skill against catalog fields and records per-skill evidence in the table's comments.

### 2. Portable overrides (same module)
- `PORTABLE_OVERRIDES: dict[str, dict[str, tuple[str, ...]]]` mapping skill name -> catalog prose field -> replacement lines for the agent-skills target.
- Required for ulw engines: ulw-plan references `omh hermes plan` (catalog_definitions.py:5770-5788); ulw-work references `omh coding ...`/handoff surfaces (:802-1128). Substitute host-neutral equivalents: "record the plan as a durable file/ledger the host can resume" instead of `omh hermes plan --record`; "delegate through the host's own subagent/task mechanism" instead of Hermes handoff composition. NEVER imply hidden execution; keep prepared/observed discipline wording.
- Pure portable domain skills need NO overrides (shared framing swap suffices).

### 3. Renderer target param (src/skills/render.py)
- Thread `target: Literal["hermes", "agent-skills"] = "hermes"` through `workflow_skill_from_definition()` (render.py:2118-2126, the existing parametrization pattern), `_workflow_full_body()` (:1990-2072), `_common_rail_sections()`, `_quality_rubric_sections()` (:461-487), `_skill_metadata_block()` (:490-526), `_frontmatter()` (:329-344).
- agent-skills target behavior:
  - "This is a Hermes-native `{name}` workflow skill." -> "This is an OMH `{name}` workflow skill, projected for Agent Skills hosts (Claude Code, Codex, Cursor)."
  - Omit Wrapper Backend Summary, Hermes install-paths, Hermes compatibility sections.
  - `_skill_metadata_block`: omit hermes_role/handoff_policy rows (or replace handoff_policy with the override text).
  - `_frontmatter`: keep metadata.hermes (spec-legal arbitrary metadata); add `compatibility: "Requires the omh CLI on PATH (pip install oh-my-hermes)."` when skill_portability(name) == requires-omh-cli.
  - Apply PORTABLE_OVERRIDES to the affected prose fields.
- HARD PIN: hermes target output must remain BYTE-IDENTICAL. Characterization test captures builtin_skill_templates() bytes before the change and compares after.

### 4. Committed projection + byte gate
- New committed tree: `agent-skills/<name>/SKILL.md` (+ references/ only for skills whose references are portable; omit Hermes-coupled references).
- Generator `agent_skill_templates()` mirroring `builtin_skill_templates()` (render.py:4344-4349), emitting only portable_skill_names().
- Gate: `omh docs agent-skills --check` in src/commands/docs.py beside cmd_docs_workflows (:19-45): byte-exact compare + missing/extra detection. Register in the same CI workflow step list as the other docs gates.

### 5. Install target (src/commands/setup.py + new src/install/agent_skills_projection.py)
- `omh install --target agents --scope repo|user` (parser seam: setup.py:4792-4997; handler seam: cmd_install :241-256).
- GENERATES from catalog (works from a pip-installed omh, no repo tree) into `.agents/skills/` (repo scope = git rev-parse --show-toplevel, fail closed outside a repo) or `~/.agents/skills/` (user scope).
- Manifest `.omh-agent-skills-manifest.json` in the target dir: schema `omh_agent_skills_projection/v1`, catalog_revision, per-file sha256. Reuse the hash/compare approach of guidance_projection._locally_modified (:277-300) — small sibling module, no Hermes config registration (hosts scan the dir directly).
- Status output: fresh/stale/locally-modified/next_action, same vocabulary as the Hermes projection status.
- Never writes outside the chosen target dir; never touches `.claude/skills` or vendor dirs in v1.

### 6. Docs
- New `docs/AGENT-SKILLS.md`: what the projection is, the three classes, install commands, the hermes-only boundary list, prepared/observed note. Register wherever docs navigation gate requires (check `omh docs navigation --check` behavior).

## RED scenarios (record BEFORE production edits, capture failing output)
- R1: `PYTHONPATH=src uv run python -m omh.cli docs agent-skills --check` -> fails: unknown command.
- R2: tests/test_agent_skills_projection.py::test_ulw_work_projection_has_no_hermes_framing -> fails: no projection generator.
- R3: ::test_requires_omh_cli_skills_carry_compatibility_frontmatter -> fails.
- R4: ::test_hermes_only_skills_absent_from_projection (ulw-maestro, omh-routing, omh-doctor) -> fails.
- R5 (PIN): tests/test_agent_skills_projection.py::test_hermes_projection_byte_stable -> capture builtin_skill_templates() digests BEFORE the renderer change; must pass before AND after.
- R6: `uv run python -m omh.cli install --target agents --scope repo` in a temp git repo -> fails: unrecognized arguments.
- R7: ::test_install_manifest_detects_local_modification -> fails.
- R8 (surface): `npx skills-ref validate` on the emitted pack -> initially no pack.

## QA (real surface, from temp dirs, cleanup receipts)
- Q1: temp git repo + `omh install --target agents --scope repo` -> .agents/skills contains exactly portable_skill_names(); read ulw-work/SKILL.md and omh-frontend/SKILL.md; grep -ri for forbidden tokens: "Hermes-native", "omh hermes", "Wrapper Backend", "skills.external_dirs" -> zero hits in the pack.
- Q2: temp HOME + `--scope user` -> ~/.agents/skills populated; manifest present.
- Q3: `npx skills-ref validate` on both target dirs -> pass.
- Q4: modify one installed file, re-run install status -> locally-modified reported; re-run install -> fresh.
- Q5: full suite + ruff + compileall + all docs gates (incl. new agent-skills gate) + git diff --check.

## Commit plan
1. test: RED scenarios R2-R5,R7 (failing tests + PIN) — or fold RED capture into evidence dir, commit tests with implementation per repo habit (tests+impl together is fine; RED capture goes to .omc evidence).
2. feat(skills): portability classification + renderer target + projection + docs gate.
3. feat(install): omh install --target agents --scope repo|user + manifest.
4. docs: AGENT-SKILLS.md + any navigation registration.
DCO + Lore trailers on every commit.

# Repo-scope Claude mirror correction

2026-09-13; follow-up to `eaf2c7cb`, task st_01a09854.

## Evidence and decision

Parent Phase B supplied observed Claude Code 2.1.270 discovery results from
`omh-agskills-parent-qa.XXXXXX.RDeURtz5lq/repo`:

- `.agents/skills/` alone, 99 installed skills: two isolated
  `claude -p "list ulw-/omh- skills" --model haiku` runs returned `NONE`.
- Adding `.claude/skills/`, same prompt: `ulw-context`, `ulw-interview`,
  `ulw-loop`, and `omh-plan` were discovered.

These are parent-supplied observations, not a Claude rerun by this implementer
or proof that every workflow executed. They supersede the Phase A standard-facts
assumption, as corrected in ../hosts/matrix.md and docs/AGENT-SKILLS.md.

## Required changes and evidence

| Requirement | Delivered proof |
| --- | --- |
| Repo copies at both git-root destinations | `agent_skills_targets("repo")` returns `.agents/skills` plus `.claude/skills`; existing shared manifest/status machinery handles both |
| Same v1 manifest and file hashes | New CLI regression checks both target_dirs, identical manifest bytes, every file hash, and copy-not-symlink behavior; wheel CLI QA checks both repo destinations |
| Upgrade, local modifications, missing mirror, refresh | Regression starts with a single-root v1 install, upgrades it, edits the mirror, checks read-only locally_modified status, reinstalls to fresh/clean, and checks missing-mirror-manifest status |
| RED-first | red.log records both new scenarios failing before production edits; exact command below |
| No implicit Hermes source contamination | Second RED scenario found mirror skills leaking into repo-wide Hermes source imports. The converter excludes a manifest-owned repo Claude mirror while explicit mirror imports and ordinary unowned Claude source directories remain supported |
| Correct docs and host matrix | docs/AGENT-SKILLS.md and ../hosts/matrix.md carry dated observed version/run details; CLI help and CONTEXT.md describe both scopes accurately |
| Focused tests and gates | green.log: 63 tests, OK; gates.log: all 14 commands exit 0, including the byte-exact Agent Skills gate and all nine docs --check gates |
| Projection content unchanged | gates.log records zero diff against eaf2c7cb for agent-skills/, skills/, and docs/WORKFLOWS.md; the Hermes digest PIN also passes in green.log |
| Real-surface verification | qa.json: isolated wheel-installed CLI from a temporary nested git cwd and HOME; both scopes and both Claude copies verified; 396/396 skill validations (99 skills x 4 roots); repo/user mirror drift repaired; cleanup.removed=true |
| New signed commit | `fix(install): mirror repo Agent Skills for Claude Code`; all seven Lore fields and DCO signoff; exact hash reported in the final task response |

## Commands

RED, before production edits:

```sh
PYTHONPATH=tests uv run python -m unittest test_agent_skills_projection.AgentSkillsProjectionTests.test_repo_scope_installs_and_repairs_claude_mirror test_agent_skills_projection.AgentSkillsProjectionTests.test_manifest_owned_claude_mirror_is_not_an_implicit_hermes_source -v
```

GREEN:

```sh
PYTHONPATH=tests uv run python -m unittest test_agent_skills_projection test_skill_install_layout test_handoff_safety_contract_enforcement -v
```

Build and real-surface QA:

```sh
uv build --out-dir .omc/agent-skills/repo-mirror/dist
OMH_AGENT_SKILLS_QA_EVIDENCE="$PWD/.omc/agent-skills/repo-mirror" uv run python .omc/agent-skills/qa.py
```

Every gate command and exit is recorded in gates.log. Every QA command, cwd,
exit, and output is recorded in qa.json. The QA script now accepts a separate
evidence root so the initial delivery logs remain intact. The external validator
still operates per skill; its expected collection-root refusal remains visible.

## Verification limits

- No live Claude invocation was repeated by this implementer; the host discovery
  diagnosis is the parent's observed Phase B evidence.
- Full unittest discovery was not repeated for this focused follow-up. The
  previous full-suite result remains in ../full-suite.log, and the affected
  projection, source-layout, and process-policy tests all passed here.
- LSP requests for all four changed production/test Python files were rejected
  by the same sibling-worktree cwd restriction recorded in ../diagnostics.md.
  Ruff and compileall passed on the actual source tree before the wheel build.
- No generated skill content, portability counts, runtime dependencies, or
  Hermes registration behavior changed. No push or merge was performed.

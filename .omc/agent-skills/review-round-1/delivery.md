# Gate review round 1: S1 and S2 fixes

2026-09-13; base `41fbe736`; task st_01a09854.

## S1: exact manifest-owned paths, not directory ownership

`discover_skill_files` now reads the existing validated projection manifest
and excludes only its declared file paths plus the manifest itself. It does not
exclude the entire `.claude/skills` directory or any skill-directory prefix.
The manifest's target_dirs cannot redirect this exclusion; paths are rooted at
the selected source's Claude mirror. Explicit mirror-root imports remain valid.

The regression creates a custom sibling skill, another custom skill nested
inside an otherwise managed skill directory, and an unowned reference. It checks
discover_skill_files, convert_from_dir, and convert_references_from_dir, and also
proves that editing an owned file does not turn it into an unowned source.

Real-surface proof: source-import-qa.py installs the built wheel into a temporary
venv, installs the repo projection, adds a custom Claude skill/reference, then
invokes the real `omh install --from-skills-dir <repo>` surface into an isolated
OMH home. Exactly `omh-custom-skill` and its reference are imported; none of the
manifest-owned projected skills are imported. source-import-qa.json records all
commands, outputs, the result, and confirmed cleanup.

## S2: read-only omh-docs belongs in the projection

The classification is now requires-omh-cli. Its catalog required_inputs select
public-product versus current-local-install scope; expected_outputs are sourced
answers and bounded metadata/CLI facts. The final_checklist and handoff_policy
route mutations elsewhere. The table comments now cite these fields instead of
misreading product explanation as native installation ownership.

The existing compatibility-frontmatter branch emits the CLI requirement without
any renderer or Hermes catalog change. The generated addition is
`agent-skills/omh-docs/SKILL.md`. No Hermes-coupled documentation reference was
admitted. Public docs now describe the read-only skill and remove it from the
Hermes-only boundary list.

Counts re-derived from the producers into ../counts.json:

- Installable: 85 portable, 15 requires-omh-cli, 23 Hermes-only (123 total).
- Full catalog: 85 portable, 15 requires-omh-cli, 28 Hermes-only (128 total).
- Projection: 100 skills and 57 portable references (157 files).

Historical delivery records remain preserved and are marked superseded by the
current correction at the top of ../delivery.md.

## RED before production edits

red.log captures two failures, exit 1, from this exact command:

```sh
PYTHONPATH=tests uv run python -m unittest test_agent_skills_projection.AgentSkillsProjectionTests.test_unowned_claude_skills_survive_manifest_owned_path_exclusion test_agent_skills_projection.AgentSkillsProjectionTests.test_omh_docs_is_projected_with_cli_compatibility -v
```

## GREEN and gates

```sh
PYTHONPATH=tests uv run python -m unittest test_agent_skills_projection test_skill_install_layout test_handoff_safety_contract_enforcement -v
```

- green.log: **65 tests, OK**, including both blockers, existing repo/user
  mirror and drift cases, source-layout tests, process-policy tests, and the
  original Hermes digest PIN.
- gates.log: **14 commands, all exit 0**: Ruff, compileall, all nine docs --check
  gates including byte-exact agent-skills and navigation, unchanged Hermes
  projection diff, whitespace, and wheel/sdist build.
- qa.json: isolated wheel CLI installation at both scopes and both mirrors,
  **400/400 skills-ref validations** (100 skills x four roots), local-modification
  status/repair, forbidden-token check, and cleanup.removed=true.
- source-import-qa.json: the real source-import surface preserves the custom
  skill/reference while excluding owned files; passed=true and cleanup confirmed.

QA commands:

```sh
uv build --out-dir .omc/agent-skills/review-round-1/dist
OMH_AGENT_SKILLS_QA_EVIDENCE="$PWD/.omc/agent-skills/review-round-1" uv run python .omc/agent-skills/qa.py
uv run python .omc/agent-skills/review-round-1/source-import-qa.py
```

## Re-review and remaining verification

The implementation diff and emitted omh-docs contract were re-read against both
blockers after GREEN. S1 uses exact validated path membership (including the
nested custom-skill negative control); S2 retains the catalog's read-only and
source/freshness boundaries and declares its CLI dependency. No unrelated
production changes were made, and Hermes projection bytes remain unchanged.
This is implementer verification, not independent gate approval.

The parent is running full unittest discovery separately on the prior head.
**The new commit needs a full-suite rerun and independent gate re-review by the
parent.** No full-suite or independent-review pass is claimed for this head.
LSP requests for the three changed Python files were rejected by the existing
sibling-worktree cwd restriction; Ruff/compileall ran on the actual worktree.
No new live model invocation, host nomination, push, or merge was performed.

The follow-up commit carries all seven Lore fields and
`Signed-off-by: rlaope <piyrw9754@gmail.com>`; its hash is in the final task report.

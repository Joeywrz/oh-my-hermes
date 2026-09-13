# RED commands (before production edits)

Worktree: `/Users/khope@sionic.ai/Desktop/khope/oh-my-hermes-worktrees/agent-skills-projection`.
R5 baseline producer: `uv run python` importing `omh.skills.render.builtin_skill_templates`, hashing each UTF-8 content with SHA256; saved as `hermes-before.json` and `tests/fixtures/agent_skills_hermes_digests.json` before any renderer edits.

```sh
# R1
PYTHONPATH=src uv run python -m omh.cli docs agent-skills --check
# R2-R5,R7 (unittest runs the individually named scenarios in test_agent_skills_projection.py)
PYTHONPATH=tests uv run python -m unittest test_agent_skills_projection -v
# R6 (substitute the invocation-owned mktemp result only)
ROOT="$PWD"; TMP=$(mktemp -d); git -C "$TMP" init -q
(cd "$TMP" && "$ROOT/.venv/bin/python" -P -m omh.cli install --target agents --scope repo)
# R8: no emitted pack exists at this point
(cd "$TMP" && npx skills-ref validate "$TMP/.agents/skills")
rm -rf "$TMP"
```

The shell runner records each exit status without masking it in `red.log`; R5 alone must PASS while the other scenarios fail. Temporary-directory deletion is checked and recorded.

## Owner amendment RED (also before any production edit)

```sh
PYTHONPATH=tests uv run python -m unittest test_agent_skills_projection.AgentSkillsProjectionTests.test_user_scope_mirror_has_shared_manifest_and_drift test_agent_skills_projection.AgentSkillsProjectionTests.test_agent_descriptions_are_bounded_without_changing_hermes -v
```

R9 (user mirror) and R10 (description guard) fail on missing implementation in
red.log. The initial synthetic description fixture accidentally selected the
router, whose producer deliberately emits no trigger tail. It was corrected to
select an ordinary workflow with a valid trigger. The assertion still requires
Hermes >1024, portable <=1024, and rejection of an oversized base description;
no assertion was weakened. renderer-green.log records the discovered fixture
issue and display-name accessor failure before their corrections.

## Integration regression RED

The first full suite found the legacy source importer recursively mixing both
projection trees and the new Git probe missing from explicit process policy
registrations. Its output is retained as full-suite-initial.log. Before fixing
the converter or the probe boundary, these additional scenarios were captured:

```sh
PYTHONPATH=tests uv run python -m unittest test_agent_skills_projection.AgentSkillsProjectionTests.test_hermes_source_discovery_does_not_mix_agent_projections test_agent_skills_projection.AgentSkillsProjectionTests.test_repo_scope_ignores_ambient_git_target_and_bounds_the_probe -v
```

R11 shows duplicate source discovery; R12 shows ambient GIT_DIR/GIT_WORK_TREE
redirecting the selected repository. Both fail in red.log. The fixes exclude
sibling portable projections from implicit Hermes source discovery and isolate
the bounded read-only Git-root probe. Explicit source roots remain usable.

## Validator unit

`skills-ref@0.1.5 validate` accepts one skill directory, not a collection root.
QA records the root refusal and then validates every skill in all three emitted
destinations individually. There is no fake root SKILL.md or skipped skill.

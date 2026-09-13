# Diagnostic surface

The LSP diagnostics tool was invoked on every changed Python production/test file.
It refused each requested worktree path with:

```
LSP file path must be inside request cwd: /Users/khope@sionic.ai/Desktop/khope/oh-my-hermes-worktrees/agent-skills-projection/...
```

The tool request cwd is the parent checkout, not the assigned sibling worktree.
No LSP result is claimed. The available package validators were run on the
actual worktree instead: Ruff, compileall, focused unit/integration tests, full
unittest discovery, byte gates, and a wheel build. See green.log, full-suite.log,
and gates.log.

Files requested: src/skills/catalog_portable.py, src/skills/render.py,
src/install/agent_skills_projection.py, src/commands/setup.py,
src/commands/docs.py, src/catalogs/documentation_navigation.py,
src/omh/converter.py, tests/test_agent_skills_projection.py,
tests/test_handoff_safety_contract_enforcement.py.

CodeGraph status was inspected before exploration. It selected the enclosing
khope workspace and reported an interrupted/truncated index with unresolved
references; source files and direct caller tracing were used instead of treating
that shared index as authoritative. No unrelated shared index was rewritten.

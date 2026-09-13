# OMH Agent Skills — host-compatibility matrix

Synthesized 2026-09-13 from `research-opencode.md`, `research-openclaw.md`, `research-pi.md`, and `research-host-extensibility.md`, plus standard known facts for Claude Code, Codex CLI, and Cursor. **No report contradicts the standard facts** — the research reports cover opencode/OpenClaw/pi (which agree with the standard locations) and plugin mechanisms; the Claude Code / Codex / Cursor rows below rest on the standard known facts and are marked accordingly.

## Host matrix

| Host | Skills support | Repo dir | User dir | Invocation | Quirks | Verdict |
|---|---|---|---|---|---|---|
| Claude Code | Yes | `.claude/skills/`, `.agents/skills/` | `~/.claude/skills/` | Progressive disclosure: descriptions in system prompt, model loads SKILL.md on demand | Name must match directory; ~5k-line SKILL.md body cap; `~/.agents/skills` is not a documented Claude Code scan path (standard facts) | Standard pack works as-is at repo scope via `.agents/skills`; user scope needs a `~/.claude/skills` copy/symlink |
| Codex CLI | Yes | `.agents/skills/` | `~/.agents/skills/` | Progressive disclosure, model-invoked | Standard known facts only; not covered by the research reports | Fully covered by the standard two-dir install |
| Cursor | Yes | `.agents/skills/` (open standard) | `~/.agents/skills/` (open standard user scope; not independently verified for Cursor) | Implicit, model-triggered | Skills support per the open standard; UI extensibility is a separate VS Code-extension surface (see extensibility report) | Covered by the standard install, pending user-scope confirmation |
| opencode | Yes | `.agents/skills/` (also `.opencode/skills/`, `.claude/skills/`) | `~/.agents/skills/` (also `~/.config/opencode/skills/`, `~/.claude/skills/`) | Implicit only: `<available_skills>` in system prompt; native `skill` tool injects the body; permission-gated; no slash command | `name` must equal directory name, `^[a-z0-9]+(-[a-z0-9]+)*$`, 1–64 chars; `description` 1–1024 chars or skill is invisible; duplicate names across locations silently overwrite (last wins); bundled-file list sampled at 10 files | Unmodified standard pack works as-is; closest terminal agent to full standard support |
| OpenClaw | Yes | `<workspace>/.agents/skills/` (also `<workspace>/skills/`, which outranks it) | `~/.agents/skills/` (default state dir only — skipped when `OPENCLAW_STATE_DIR` points elsewhere) | Both: implicit XML block in system prompt, plus `/skill <name>` slash commands and `$name` references (max 8 per message) | Name must be a lowercase-hyphen slug aligned with directory; one-line description <160 chars (injected per skill); reference files via `{baseDir}`, never hardcoded paths; name collisions resolved by source priority | Standard pack works given slug name + `{baseDir}` references |
| pi | Yes | `.agents/skills/` (trusted projects; cwd and ancestors up to git root) | `~/.agents/skills/` (also `~/.pi/agent/skills/`) | Both: implicit (XML per agentskills.io spec) plus `/skill:name` slash commands | Validation is warn-and-load, not reject; name/directory mismatch tolerated; missing/empty `description` blocks loading; root `.md` files ignored in `.agents/skills`; name collisions keep first found; project scope requires trusting the project | Most spec-faithful and lenient host; standard pack works as-is |

## Non-portable capabilities (from research-host-extensibility.md)

OMH capability classes that cannot ship as `SKILL.md` and need per-host adapters:

1. **`omh_*` RPC tools** (`omh_todo`, `omh_delegate_route`, `omh_run_summary`) — feasible as plugins on all six hosts: one MCP server covers Claude Code, Codex, Cursor, opencode, and OpenClaw; pi has no MCP client and needs a TypeScript extension (`registerTool`). Two-adapter story.
2. **File-backed memory provider** — adapter feasibility splits. Native equivalents make an OMH memory adapter largely redundant on Claude Code (auto memory), Codex (local memories), and OpenClaw (builtin + active memory) — at most a sync hook there. It fills a real gap on Cursor, opencode, and pi (instructions files only). Semantic shift: Hermes injects memory as host-loaded context; elsewhere it becomes model-invoked tool calls unless a SessionStart-class hook injects it.
3. **TUI/HUD widgets** — the genuinely non-portable class. Cheap ports exist only on Claude Code (scriptable `statusLine`) and pi (`ctx.ui.custom` components); Codex's `/statusline` is fixed-item, opencode's TUI is theme/keybind-only, and Cursor means writing a VS Code extension.
4. **Chat-wrapper routing** (`src/wrapper` session continuity) — Hermes-specific; OpenClaw is the only host with a native equivalent (gateway channel routing + agent bindings). Elsewhere it degrades to host subagents/tasks.

## Install surface recommendation

**Repo scope `.agents/skills/` + user scope `~/.agents/skills/` covers all six hosts at repo scope and five of six at user scope; one extra target dir is needed.**

- **Repo `.agents/skills/`**: native scan path on all six hosts (Claude Code, Codex, Cursor, opencode, OpenClaw, pi). No extra repo target needed.
- **User `~/.agents/skills/`**: native on Codex, opencode, OpenClaw (default state dir), and pi; presumed on Cursor per the open standard. Not a documented Claude Code path — Claude Code's user scope is `~/.claude/skills/`.
- **Extra target: `~/.claude/skills/`** (copy or symlink from `~/.agents/skills/`) for Claude Code user-scope installs.
- **Documented caveat, not an extra target**: OpenClaw skips `~/.agents/skills` when `OPENCLAW_STATE_DIR` points elsewhere.

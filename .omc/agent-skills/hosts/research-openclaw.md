# Research: OpenClaw support for the Agent Skills standard

Host: OpenClaw (open-source personal agent, formerly Clawdbot/Moltbot; OpenClaw Foundation project)
Researched: 2026-09-13, against current published docs.
Primary sources (fetched live this date):

- Skills reference (loading order, SKILL.md format, gating, ClawHub): https://docs.openclaw.ai/tools/skills
- Creating skills (naming rules, {baseDir}, authoring): https://docs.openclaw.ai/tools/creating-skills

## 1. Does OpenClaw support the Agent Skills standard?

**Yes.** The docs state explicitly, under "SKILL.md format": *"OpenClaw follows the AgentSkills spec. Frontmatter is parsed as YAML first; if that fails, it falls back to a single-line-only parser."* The page links the spec at https://agentskills.io. Every skill is a directory containing `SKILL.md` with YAML frontmatter (minimum `name` + `description`) plus markdown body; supporting `scripts/`, `references/`, `assets/` directories are carried alongside ("The editor keeps supporting scripts, references, and assets with the instructions").
Source: https://docs.openclaw.ai/tools/skills

## 2. Directories scanned

File-backed skills load from these roots, highest precedence first (same skill name in two places: higher source wins):

| Priority | Source | Path |
|---|---|---|
| 1 | Workspace skills | `<workspace>/skills` |
| 2 | Project agent skills (repo scope) | `<workspace>/.agents/skills` |
| 3 | Personal agent skills (user scope) | `~/.agents/skills` (default state only) |
| 4 | Managed / local skills | `<state-dir>/skills` (default `~/.openclaw/skills`) |
| 5 | Workshop skills | `<state-dir>/agents/<agentId>/agent/workshop-skills` |
| 6 | Bundled + Custodian skills | shipped with the install |
| 7 | Extra dirs | `skills.load.extraDirs` + plugin skill dirs |

An execution workspace's own `skills/` and `.agents/skills/` are also loaded (skills/ wins within that workspace). Both Agent Skills standard scopes are therefore native: repo scope `<workspace>/.agents/skills` and user scope `~/.agents/skills`. Discovery is grouped: a `SKILL.md` found anywhere under a configured root (up to 6 levels deep) is a skill; finding `SKILL.md` ends traversal below that directory. Invalid skill files are reported and skipped; siblings still load.
Caveats: (a) `~/.agents/skills` is only scanned for the *default* state dir — if `OPENCLAW_STATE_DIR` points elsewhere, home-scoped roots are excluded; (b) Codex CLI's `$CODEX_HOME/skills` is not an OpenClaw root; (c) workspace/project/extra-dir discovery enforces path containment (realpath must stay inside the root unless `skills.load.allowSymlinkTargets` trusts it).
Source: https://docs.openclaw.ai/tools/skills

## 3. Invocation model

Both explicit and implicit:

- **Implicit (description matching):** eligible skills are compiled into a compact XML block injected into the system prompt (name, description, location); the model is told to read the referenced `SKILL.md` before acting. Cost is ~24 tokens per skill before field lengths; a prompt budget (`skills.limits.maxSkillsPromptChars`) can truncate descriptions, never admitted names.
- **Explicit slash command:** every skill with `user-invocable: true` (the default) is exposed as a slash command — `/skill hello-world` or `/hello-world ...`. On channels, `command-dispatch: tool` can bypass the model and dispatch directly to a registered tool.
- **Explicit `$name` reference in prompt text:** typing `$` in the Control UI composer inserts a stable command name (e.g. `$release_notes`); up to 8 distinct skills per message, otherwise a visible error. Uppercase shell variables (`$HOME`, `$PATH`, `$EDITOR`) stay literal text — reference skills with those names via lowercase, or escape as `\$name`.

Source: https://docs.openclaw.ai/tools/skills

## 4. Frontmatter beyond name/description

Only `name` and `description` are required. Honored optional keys:

- `user-invocable` (bool, default true) — expose as slash command.
- `disable-model-invocation` (bool, default false) — keep instructions out of the normal prompt (still runs via slash command or explicit `$name`).
- `command-dispatch` ("tool"), `command-tool` (string), `command-arg-mode` ("raw", default) — direct tool dispatch for slash commands.
- `homepage` (string) — URL shown in the macOS Skills UI.
- `metadata.openclaw` (JSON5 object; nested YAML mappings are flattened and re-parsed as JSON5) — gating and install info: `requires.bins`, `requires.anyBins`, `requires.env`, `requires.config`, `primaryEnv`, `os` ("darwin"|"linux"|"win32"[]), `always`, `emoji`, `homepage`, `install` (brew/node/go/uv/download specs), `skillKey`. Legacy `metadata.clawdbot` blocks are still accepted when `metadata.openclaw` is absent.

A skill with no `metadata.openclaw` block is always eligible unless explicitly disabled.
Sources: https://docs.openclaw.ai/tools/skills , https://docs.openclaw.ai/tools/creating-skills

## 5. Quirks that matter for a generated pack

- **Naming:** `name` must be a slug of lowercase letters, digits, and hyphens; keep directory name and frontmatter `name` aligned (node-hosted skills *require* the directory name to match `name`). The slash command comes from `name` (directory name only as fallback).
- **Description:** one line, under 160 characters — it is injected into the system prompt per skill; keep it short to minimize token cost.
- **{baseDir} placeholder:** reference support files in the body as `{baseDir}/scripts/run.sh` — OpenClaw resolves it against the skill's own directory. Do not hardcode paths.
- **Grouped layouts:** `SKILL.md` may sit up to 6 levels deep under a root (e.g. `skills/personal/research/SKILL.md`); a generated pack emitting standard one-dir-per-skill layout is fine.
- **Size limits (Gateway skill library / managed bundles only):** 256 files, 1 MiB per file, 8 MiB total per bundle; 8 MiB aggregate worker delivery; new sessions select at most 64 enabled library skills. Plain file-backed workspace/user-scope skills are not documented as bound by these limits.
- **Install expectations:** `openclaw skills install` from Git/local sources expects `SKILL.md` at the source root; slug is derived from frontmatter `name` when valid (fallback: directory/repo name), overridable with `--as`.
- **Refresh semantics:** skills are snapshotted per session; the watcher picks up `SKILL.md` changes (250 ms debounce), and refreshed lists apply on the next agent turn — new sessions always get the current set.
- **Sandboxing:** `requires.bins` is checked on the host at load time; inside a sandbox the binary must also exist in the container. `skills.entries.*.env`/`apiKey` inject only into the host process, not the sandbox.
- **Env-var collisions:** `$HOME`-style uppercase variables in prompt text are not skill references; lowercase `$name` or `\$name` escape rules apply.

Sources: https://docs.openclaw.ai/tools/skills , https://docs.openclaw.ai/tools/creating-skills

## 6. Verdict

**Yes — an unmodified standard-conformant pack (SKILL.md with name/description, optional scripts/references/assets) dropped into `<workspace>/.agents/skills/` or `~/.agents/skills/` will load and run in OpenClaw, provided the name is a lowercase-hyphen slug, the description stays one line (~<160 chars), and the body references files via `{baseDir}` rather than hardcoded paths.**

Source: https://docs.openclaw.ai/tools/skills

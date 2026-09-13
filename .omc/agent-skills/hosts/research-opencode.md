# Host research: opencode (opencode.ai, SST terminal coding agent)

Researched: 2026-09-13. Sources: official docs at https://opencode.ai/docs/skills/ (fetched as https://opencode.ai/docs/skills.md) and the `dev`-branch implementation at https://github.com/sst/opencode (`packages/opencode/src/skill/index.ts` and `packages/opencode/src/tool/skill.ts`, fetched via raw.githubusercontent.com). Repo: 206k+ stars, last push 2026-09-13 (actively maintained).

## 1. Agent Skills support: YES

opencode implements SKILL.md-based skills natively and scans the Agent Skills standard locations (repo scope `.agents/skills/` and user scope `~/.agents/skills/`) as first-class "agent-compatible" discovery paths, alongside its own `.opencode/skills/` and Claude-compatible `.claude/skills/`. It is the closest to full standard support of the terminal agents.
Source: https://opencode.ai/docs/skills/ ; https://github.com/sst/opencode (packages/opencode/src/skill/index.ts, constants `AGENTS_EXTERNAL_DIR = ".agents"`, `EXTERNAL_SKILL_PATTERN = "skills/**/SKILL.md"`).

## 2. Directories scanned

Per https://opencode.ai/docs/skills/ and confirmed in source (`discoverSkills` in packages/opencode/src/skill/index.ts):

- Project: `.opencode/skills/<name>/SKILL.md` (source glob is `{skill,skills}/**/SKILL.md`, so `.opencode/skill/` also works)
- Global: `~/.config/opencode/skills/<name>/SKILL.md`
- Project Claude-compatible: `.claude/skills/<name>/SKILL.md`
- Global Claude-compatible: `~/.claude/skills/<name>/SKILL.md`
- **Project agent-standard**: `.agents/skills/<name>/SKILL.md`
- **Global agent-standard**: `~/.agents/skills/<name>/SKILL.md`

Notes: project paths are found by walking up from cwd to the git worktree root (any matching dir along the way is scanned), per the "Understand discovery" section of the docs. Source also supports extra sources the docs summarize as config: `skills.paths` (arbitrary dirs, `~/` expansion supported) and `skills.urls` (remote skill packs pulled from URLs) in `opencode.json`. External dirs can be disabled by flags (`disableExternalSkills`, `disableClaudeCodeSkills`) in the source, but `.agents/` is only skipped when all external skills are disabled.

## 3. Invocation model: implicit description matching via a native `skill` tool

There is no user-facing slash command. opencode injects an `<available_skills>` list (name + description, optionally location) into the system prompt; the model autonomously calls the native `skill` tool (`skill({ name: "git-release" })`) when a task matches a description, which injects the SKILL.md body into the conversation. Sources: https://opencode.ai/docs/skills/ ("Recognize tool description") and the tool description text at https://raw.githubusercontent.com/sst/opencode/dev/packages/opencode/src/tool/skill.txt ("Load a specialized skill when the task at hand matches one of the skills listed in the system prompt"). Access is permission-gated: `permission.skill` patterns in `opencode.json` / agent frontmatter (`allow` / `ask` / `deny`, wildcard `internal-*` supported) per https://opencode.ai/docs/skills/; the tool itself can be disabled per agent (`tools: { skill: false }`).

## 4. Frontmatter fields honored

Per https://opencode.ai/docs/skills/ ("Write frontmatter"): only these fields are recognized —

- `name` (required)
- `description` (required, 1–1024 characters)
- `license` (optional)
- `compatibility` (optional)
- `metadata` (optional, string-to-string map)

Unknown fields are ignored. Implementation note (packages/opencode/src/skill/index.ts, `Info` schema): only `name` and `description` are actually consumed at runtime — `license`/`compatibility`/`metadata` are accepted but functionally inert. A skill without a `description` is loaded but filtered out of the available-skills listing (function `fmt`), so it is effectively invisible to the agent.

## 5. Quirks that matter for a generated pack

- **Name = directory name**: `name` must match the directory containing SKILL.md (source has `SkillNameMismatchError`; docs' troubleshooting step and "Validate names").
- **Name rules**: 1–64 chars, regex `^[a-z0-9]+(-[a-z0-9]+)*$` (lowercase alphanumeric, single hyphens, no leading/trailing hyphen, no `--`), per https://opencode.ai/docs/skills/.
- **Bundled files are surfaced, but sampled**: when the `skill` tool fires it returns the SKILL.md body plus the skill's base directory, an explicit note that relative paths (`scripts/`, `reference/`, assets) resolve against that base directory, and a file list capped at 10 files ("Note: file list is sampled."). Source: https://raw.githubusercontent.com/sst/opencode/dev/packages/opencode/src/tool/skill.ts. So scripts/references/assets work by relative path from SKILL.md prose, but a pack with many files may have some omitted from the listed inventory.
- **Duplicate names across locations silently overwrite**: last discovered wins (source `add()` overwrites `state.skills[name]` with only a warning log); the docs' troubleshooting says skill names must be unique across all locations.
- **Nested layouts tolerated**: the glob `skills/**/SKILL.md` matches deeper nesting, but the immediate parent directory must still equal `name`.
- **A built-in skill `customize-opencode` ships with the agent**; a user skill with the same name overrides it (source comment).
- **File must be exactly `SKILL.md`** (all caps) — troubleshooting step 1 at https://opencode.ai/docs/skills/.
- No size limit on SKILL.md body is documented (no 5k-line Claude-style limit found in docs or source).

## 6. Verdict

**Yes — an unmodified standard-conformant pack (`.agents/skills/<name>/SKILL.md` with valid `name`/`description` frontmatter and optional bundled dirs) will be discovered and usable in opencode as-is**, provided `name` equals the directory name and matches `^[a-z0-9]+(-[a-z0-9]+)*$` and `description` is 1–1024 chars.

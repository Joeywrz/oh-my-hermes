# Host research: pi (terminal coding agent by Mario Zechner)

Verified 2026-09-13 against the live repository and docs.

**Disambiguation.** "pi" the coding agent = the interactive terminal coding agent CLI by Mario Zechner (GitHub user `badlogic`). The project was originally `badlogic/pi-mono` and has moved to **`earendil-works/pi`** ("Pi Agent Harness"; the old URL still redirects there). The CLI is published as [`@earendil-works/pi-coding-agent`](https://www.npmjs.com/package/@earendil-works/pi-coding-agent); website [pi.dev](https://pi.dev). This is distinct from Inflection AI's "Pi" chat assistant (not a coding agent, not verified here).

Sources (primary, used throughout):
- Repo: https://github.com/earendil-works/pi
- Skills doc: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/skills.md (also published at https://pi.dev/docs/latest/skills)
- Validation source: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/skills.ts
- Discovery source: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/package-manager.ts

## 1. Agent Skills standard support

**Yes — full support.** The docs state: "Pi implements the [Agent Skills standard](https://agentskills.io/specification), warning about most violations but remaining lenient." Skill descriptions are injected into the system prompt "in XML format per the [specification](https://agentskills.io/integrate-skills)", i.e. pi follows the standard's progressive-disclosure integration model. Validation is implemented against the agentskills.io rules (name/description constraints) in `src/core/skills.ts`, but most violations warn and still load.

## 2. Directories scanned

From the [skills doc](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/skills.md), confirmed in `package-manager.ts`:

- **User (global):**
  - `~/.pi/agent/skills/` (pi's own; `getAgentDir()` in `src/config.ts`)
  - `~/.agents/skills/` (the Agent Skills standard user scope)
- **Project (only after the project is trusted):**
  - `.pi/skills/`
  - `.agents/skills/` in `cwd` **and ancestor directories**, up to the git repo root (or filesystem root outside a repo) — `collectAncestorAgentsSkillDirs()` in `package-manager.ts`
- **Additional:** `skills/` dirs or `pi.skills` entries in packages, a `skills` array in settings (files or dirs — this is how you'd point pi at e.g. `~/.claude/skills`), and repeatable `--skill <path>` CLI flags (which work even with `--no-skills`).

Discovery details: directories containing `SKILL.md` are found recursively in all locations. In `~/.pi/agent/skills/` and `.pi/skills/`, bare root `.md` files with valid frontmatter count as skills; in `~/.agents/skills/` and project `.agents/skills/`, **root `.md` files are ignored** (only directories/nested frontmatter `.md` files are discovered). `.gitignore`/`.ignore`/`.fdignore` rules are honored during discovery.

## 3. Invocation model

Both:

1. **Implicit (primary):** at startup pi scans skill locations, puts each skill's name + description into the system prompt (XML, per agentskills.io integrate-skills), and the model loads the full `SKILL.md` on demand via its `read` (or `bash`) tool. The docs note models don't always do this unprompted, hence the explicit form below.
2. **Explicit:** every skill registers a `/skill:name` slash command (e.g. `/skill:brave-search extract`); arguments after the command are appended to the skill content as `User: <args>`. Command registration is on by default (`enableSkillCommands` setting, default `true` — `src/core/settings-manager.ts`).

## 4. Frontmatter fields

Required: `name` (max 64 chars), `description` (max 1024 chars, non-empty — a skill with no/empty description is **not loaded**). Optional and honored: `license`, `compatibility` (max 500 chars), `metadata` (arbitrary map), `allowed-tools` (experimental, space-delimited pre-approved tools) — all standard fields. Pi additionally supports the **non-standard** `disable-model-invocation` (boolean): when `true`, the skill is hidden from the system prompt and only reachable via `/skill:name`. Unknown fields are ignored.

## 5. Quirks that matter for a generated pack

- **Name need not match the directory.** The agentskills.io spec requires `name` to match the parent directory; pi deliberately does **not** enforce this (verified: the `name-mismatch` test fixture loads cleanly with zero diagnostics), because that rule is "suboptimal for shared skill directories." A conformant pack's matching name is fine; just don't rely on the harness rejecting mismatches.
- **Validation is warn-and-load, not reject:** invalid characters, >64-char names, consecutive/edge hyphens, >1024-char descriptions produce warnings but the skill still loads. Only a missing/empty `description` (or malformed frontmatter) prevents loading.
- **Name collisions** (same name from different locations) warn and keep the first skill found — watch this if the pack is installed in multiple scopes.
- **Project-scope skills load only after the project is trusted** (pi's trust prompt on first run in a directory).
- **Repo-scope root `.md` files are ignored** in `.agents/skills/` — a pack must be a directory containing `SKILL.md` (standard packs are).
- No extra files are required: `SKILL.md` alone suffices; `scripts/`, `references/`, `assets/` are optional and referenced by relative paths. No pack manifest or install step.
- Slash-command name is `/<skill-name>` under the `skill:` namespace, so skill names must be slash-command-safe (lowercase a-z, 0-9, hyphens — the standard's name rules already guarantee this).

## 6. Verdict

**Yes — an unmodified standard-conformant Agent Skills pack (directory with `SKILL.md`, optional `scripts/`/`references/`/`assets/`) works in pi as-is, in both the standard repo scope (`.agents/skills/`, trusted project) and user scope (`~/.agents/skills/`), with implicit description-based invocation plus `/skill:name` for explicit use; pi is one of the most spec-faithful and lenient hosts.**

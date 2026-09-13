# Binding owner amendment

Received during implementation, before the first production edit:

1. User scope must also COPY (not symlink) into ~/.claude/skills/. One shared
   v1 manifest contract records both target directories and covers every file
   in both copies. Repo scope remains .agents/skills only.
2. Every Agent Skills description must contain 1-1024 characters. Trim the
   optional trigger tail only for this target when necessary; Hermes stays
   byte-identical. Oversized base descriptions fail generation.
3. Document the six-host matrix from hosts/matrix.md, including unverified
   Cursor user scanning and the OpenClaw OPENCLAW_STATE_DIR caveat. Document
   RPC/MCP versus pi extension, native-memory redundancy versus provider gaps,
   host-specific TUI/HUD widgets, and Hermes wrapper routing boundaries.

These supersede the original brief's user-scope single-root/no-.claude-write
restriction and its initial Claude Code scan-path assumption. R9/R10 were
captured RED before production edits. Host research is compatibility evidence,
not a claim that six live hosts selected or executed this pack.

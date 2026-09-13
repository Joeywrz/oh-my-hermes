"""Copy-only host boundaries recorded in docs/AGENT-SKILLS.md#host-support-matrix.

These are installation contracts, not evidence of host selection/execution.
Claude's correction was observed with 2.1.270 on 2026-09-13; the other rows
retain the recorded documentation/source assessment and its caveats.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import TypedDict


@dataclass(frozen=True)
class HostAdapter:
    host: str
    repo_path: str
    user_path: str  # Relative to home, not a shell-expanded tilde.
    evidence: str
    caveat: str = ""
    transform: str = "copy-only"


_EVIDENCE = "docs/AGENT-SKILLS.md#host-support-matrix"
HOST_ADAPTERS = (
    HostAdapter("cursor", ".agents/skills", ".agents/skills", _EVIDENCE,
                "User-scope scanning not independently verified."),
    HostAdapter("claude", ".claude/skills", ".claude/skills", _EVIDENCE,
                "Repo discovery observed in Claude Code 2.1.270, 2026-09-13; not workflow execution."),
    HostAdapter("codex", ".agents/skills", ".agents/skills", _EVIDENCE),
    HostAdapter("opencode", ".agents/skills", ".agents/skills", _EVIDENCE,
                "Skill tool is permission-gated."),
    HostAdapter("openclaw", ".agents/skills", ".agents/skills", _EVIDENCE,
                "Custom OPENCLAW_STATE_DIR skips the standard user path; workspace skills can take precedence."),
    HostAdapter("pi", ".agents/skills", ".agents/skills", _EVIDENCE,
                "Trusted projects only; first duplicate name wins."),
)


def host_adapter(host: str) -> HostAdapter:
    for adapter in HOST_ADAPTERS:
        if adapter.host == host:
            return adapter
    raise ValueError(f"Unknown Agent Skills host: {host}")


class HostAdapterManifest(TypedDict):
    schema_version: str
    host: str
    targets: dict[str, str]
    transform: str
    evidence: str
    caveat: str
    source: str
    source_digest: str
    digest_format: str
    files: dict[str, str]
    skills: list[str]


def checksum_inventory(hashes: dict[str, str]) -> str:
    """SHA256SUMS-compatible UTF-8, sorted POSIX paths, LF including final line."""
    return "".join(f"{digest}  {path}\n" for path, digest in sorted(hashes.items()))


def host_adapter_manifest(host: str, files: dict[str, str]) -> HostAdapterManifest:
    adapter = host_adapter(host)
    hashes = {path: hashlib.sha256(content.encode("utf-8")).hexdigest()
              for path, content in sorted(files.items())}
    return {
        "schema_version": "omh_host_adapter/v1",
        "host": adapter.host,
        "targets": {"repo": adapter.repo_path, "user": adapter.user_path},
        "transform": adapter.transform,
        "evidence": adapter.evidence,
        "caveat": adapter.caveat,
        "source": "agent-skills",
        "source_digest": hashlib.sha256(checksum_inventory(hashes).encode("utf-8")).hexdigest(),
        "digest_format": "sha256(sorted '<file-sha256>  <relative-posix-path>\\n' UTF-8 inventory)",
        "files": hashes,
        "skills": sorted({path.split("/")[0] for path in files}),
    }

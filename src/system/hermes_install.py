"""Bounded, read-only inspection of a local Hermes installation.

One seam, because two callers need the same fact and a second parser would
drift from the first. ``maintenance.hermes_tui`` asks which Hermes a user's HUD
would render in; ``workflows.plugin_hook_contract`` asks whether the pinned
hook mapping was established for the Hermes a plugin is about to be enabled on.

What "bounded" means here, exactly: one path derived from a directory the
caller named, opened only when it is a regular file under the size cap, decoded
with replacement, and matched by one regex. Nothing is imported, no binary is
executed, no subprocess is spawned and no socket is opened. That is what lets a
caller point this at an installation it has not audited, and it is also the
limit: a version string read off disk can be stale or edited, so it is evidence
of what the installation declares, never of what a running host would report.
"""
from __future__ import annotations

from pathlib import Path
import re

HERMES_INSTALL_DIRNAME = "hermes-agent"

# Reading cap: userWidgets.ts is ~8KB and config.yaml tens of KB today.
MAX_BOUNDED_READ_BYTES = 512_000

# The module Hermes keeps `__version__` in, named in `HERMES_COMPAT_MATRIX`'s
# covered host contracts as `hermes_cli.__version__`. Public so a diagnostic can
# tell an operator which file was looked for without a second literal.
VERSION_MODULE_RELATIVE = Path("hermes_cli") / "__init__.py"

_VERSION_ASSIGNMENT = re.compile(r"__version__\s*=\s*[\"']([^\"']+)[\"']")


def read_bounded_text(path: Path) -> str | None:
    """The file's text, or None when it is absent, oversized or unreadable."""
    try:
        if not path.is_file() or path.stat().st_size > MAX_BOUNDED_READ_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def hermes_install_dir(hermes_home: Path) -> Path:
    """Where Hermes installs itself under a Hermes home."""
    return hermes_home / HERMES_INSTALL_DIRNAME


def installed_hermes_version(install_dir: Path) -> str:
    """The version the installation declares, or "" when none was established.

    Empty covers an absent directory, an absent or oversized version module, and
    a module without the assignment. They are one answer because none of them
    yields a version, and a caller that needs to tell "you named nothing" from
    "what you named yielded nothing" has to keep that distinction itself -- this
    function is only ever asked about a directory a caller already named.
    """
    text = read_bounded_text(install_dir / VERSION_MODULE_RELATIVE)
    if text is None:
        return ""
    match = _VERSION_ASSIGNMENT.search(text)
    return match.group(1) if match else ""

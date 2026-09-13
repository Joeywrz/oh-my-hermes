"""Shared platform-capability probes and skip decorators (issue #781).

Windows lacks the POSIX primitives several OMH surfaces are built on
(O_NOFOLLOW / O_DIRECTORY dirfd-anchored opens, mode-bit enforcement, mkfifo,
killpg). Product code fails closed on such platforms by design; these
decorators keep the corresponding tests scoped to hosts where the exercised
contract can hold, following the guard conventions established by #771 and
#778.

Mutual exclusion is deliberately *not* on that list. `fcntl` is POSIX-only,
but the capability built on it is not: `local_store.file_lock` falls back to
`msvcrt.locking` and reports `enforced: True` either way, so a concurrency
contract holds on both platforms. Pick the gate that names the capability
under test, not the primitive the POSIX half happens to use --
`requires_enforced_file_lock` for mutual exclusion, `requires_fcntl_locks`
only where the test asserts something about `fcntl` itself. Gating mutual
exclusion on `fcntl` left Windows with no concurrent coverage of the lock
path at all, and that is where a silent data-loss defect lived until #1467.
"""

from __future__ import annotations

import os
from pathlib import Path
import unittest

try:  # probe only; mirrors the product's degraded-import pattern
    import fcntl as _fcntl  # noqa: F401
except ImportError:
    HAS_FCNTL = False
else:
    HAS_FCNTL = True

try:  # the Windows half of the same capability
    import msvcrt as _msvcrt  # noqa: F401
except ImportError:
    HAS_MSVCRT = False
else:
    HAS_MSVCRT = True

# `file_lock` takes a real OS lock through fcntl on POSIX and msvcrt on
# Windows, and reports `enforced: True` for either. Mutual exclusion is
# therefore a cross-platform property, and gating a concurrency test on
# `HAS_FCNTL` skips it on the one platform where the product has no second
# backend to fall back on. That gate hid a Windows-only data-loss defect in
# `ensure_file` (PR #1467): the only test reaching the lock path concurrently
# on Windows was one nobody had gated, and it was the one that failed.
HAS_ENFORCED_FILE_LOCK = HAS_FCNTL or HAS_MSVCRT

HAS_SECURE_DIR_IO = bool(
    getattr(os, "O_NOFOLLOW", 0)
    and getattr(os, "O_DIRECTORY", 0)
    and os.open in os.supports_dir_fd
    and hasattr(os, "fchmod")
)

requires_posix = unittest.skipUnless(
    os.name == "posix",
    "exercises POSIX-only process or filesystem semantics",
)
requires_windows = unittest.skipUnless(
    os.name == "nt",
    "exercises Windows-only process or filesystem semantics",
)
requires_posix_permissions = unittest.skipUnless(
    os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
    "POSIX permission bits do not restrict root and do not exist off POSIX",
)
requires_hermes_host = unittest.skipUnless(
    Path(os.environ.get("HERMES_PYTHON") or Path.home() / ".hermes/hermes-agent/venv/bin/python").expanduser().is_file(),
    "real plugin admission requires an installed Hermes host",
)
requires_symlinks = unittest.skipUnless(
    os.name == "posix",
    "symlink creation and resolution differ off POSIX",
)
requires_fcntl_locks = unittest.skipUnless(
    HAS_FCNTL,
    "fcntl advisory locking is POSIX-only",
)
requires_enforced_file_lock = unittest.skipUnless(
    HAS_ENFORCED_FILE_LOCK,
    "mutual exclusion requires an enforced OS file lock (fcntl or msvcrt)",
)
requires_secure_dir_io = unittest.skipUnless(
    HAS_SECURE_DIR_IO,
    "secure dirfd-anchored file access requires POSIX O_NOFOLLOW/O_DIRECTORY/dir_fd",
)
requires_domain_intelligence_store = unittest.skipUnless(
    HAS_SECURE_DIR_IO and HAS_FCNTL,
    "domain-intelligence safe stores require POSIX dirfd/O_NOFOLLOW/fcntl primitives",
)
requires_posix_select = unittest.skipUnless(
    os.name == "posix",
    "select() on Windows accepts only sockets, not pipe/tty descriptors",
)

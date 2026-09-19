"""Shared, sweepable parent for test-only temporary directories (issue #1731).

A killed interpreter -- a cancelled suite, a SIGKILL, a CI timeout -- never
runs a `TemporaryDirectory` finalizer or an `addCleanup` callback, so temp
dirs scattered across the system temp directory accumulate invisibly.
Measured on the owner's machine on 2026-09-19: 2,926
`omh-worktree-diagnostic-*` dirs from `tests/test_fanout_worktree_diagnostics.py`
and 494 `omh-test-home-*` dirs from `tests/_local_package.py`, both created
with a bare `TemporaryDirectory(prefix=...)` in the system temp directory
with no shared, discoverable parent.

Giving every test-owned temp dir ONE fixed, recognisable parent
(`<system temp dir>/omh-test-tmp`) makes stale ones findable in one `ls`, and
lets a best-effort sweep -- run once per process, the first time this module
is used -- remove entries older than `_STALE_AFTER_SECONDS`. The sweep is
deliberately conservative:

* it never follows a symlink out of the parent (a symlink entry is skipped,
  never resolved and rmtree'd through);
* it ignores every error, so two test processes sweeping at the same moment
  never raise into each other (a directory disappearing mid-scan, one still
  open on Windows -- see the CLAUDE.md note on why Windows will not unlink a
  file another process still holds open, and its own `ignore_errors=True`
  deferral to the next sweep rather than raising);
* it stays off anything younger than the bound, so a parallel suite's own
  live directories are never touched;
* it only ever removes files and directories. It does not inspect or signal
  processes -- this repository already has a rule against pattern-matched
  process killing (`pkill -f "unittest discover"` once took out a sibling
  worktree's suite), and a directory sweep does not need one: an orphaned
  process that still has a stale directory open loses its own files when the
  directory is removed, which is enough.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
from tempfile import TemporaryDirectory
from threading import Lock
import time

_PARENT_NAME = "omh-test-tmp"
_STALE_AFTER_SECONDS = 24 * 60 * 60

_swept = False
_sweep_lock = Lock()


def test_temp_root() -> Path:
    """The shared parent for test temp dirs; sweeps stale entries once per process."""
    root = Path(tempfile.gettempdir()) / _PARENT_NAME
    root.mkdir(parents=True, exist_ok=True)
    _sweep_once(root)
    return root


def make_test_tempdir(prefix: str) -> "TemporaryDirectory[str]":
    """A `TemporaryDirectory` nested under the shared, sweepable parent."""
    return TemporaryDirectory(prefix=prefix, dir=str(test_temp_root()))


def _sweep_once(root: Path) -> None:
    global _swept
    with _sweep_lock:
        if _swept:
            return
        _swept = True
    _sweep(root)


def _sweep(root: Path, *, stale_after_seconds: float = _STALE_AFTER_SECONDS) -> None:
    """Best-effort removal of entries under `root` older than the bound.

    Exposed (leading underscore, imported directly in tests) so
    `tests/test_test_temp_root.py` can exercise it with an artificially aged
    entry instead of waiting on the real 24h bound.
    """
    now = time.time()
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_symlink():
                continue  # never follow a symlink out of the parent
            if not entry.is_dir(follow_symlinks=False):
                continue
            age = now - entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue  # raced with another sweeper or the owner; ignore and move on
        if age < stale_after_seconds:
            continue  # stay off anything younger than the bound
        shutil.rmtree(entry.path, ignore_errors=True)

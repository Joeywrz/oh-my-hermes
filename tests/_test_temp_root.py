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
  file another process still holds open);
* it stays off anything younger than the bound, so a parallel suite's own
  live directories are never touched.

Before removing a stale directory, `_kill_stale_occupants` makes a
POSIX-only, best-effort attempt to signal any process whose command line
still names a path inside it -- the mitigation for issue #1731's second
finding, an 8-day-old `hermes.py config path` fake-host process left behind
by a killed `tests/test_hermes_model_config.py` run. The library call that
spawns it (`_run` in `src/coding/hermes_model_config.py`) already bounds and
kills the child on a normal timeout or interruption; what a killed *test*
process cannot run is that in-process cleanup, exactly the same class of gap
as the leaked directories, so the same sweep mitigates it. On Windows this
step is a no-op: the follow-up `shutil.rmtree(..., ignore_errors=True)`
simply leaves a still-open file for the next sweep instead of raising.
"""

from __future__ import annotations

from contextlib import suppress
import os
from pathlib import Path
import shutil
import signal
import subprocess
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
        _kill_stale_occupants(Path(entry.path))
        shutil.rmtree(entry.path, ignore_errors=True)


def _kill_stale_occupants(directory: Path) -> None:
    """Best-effort: signal any process whose command line still names a path in `directory`.

    POSIX only. Matching is by substring against `ps`'s own argv rendering,
    scoped to a per-test-run temp directory name that is effectively unique,
    so a false match would require another process to have that exact path
    on its command line by coincidence.
    """
    if os.name != "posix":
        return
    needle = str(directory)
    try:
        listing = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if listing.returncode != 0:
        return
    own_pid = os.getpid()
    for line in listing.stdout.splitlines():
        stripped = line.strip()
        if needle not in stripped:
            continue
        pid_text = stripped.split(None, 1)[0]
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == own_pid:
            continue
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)

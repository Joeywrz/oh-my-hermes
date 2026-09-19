"""Coverage for the shared test-temp-dir parent and sweep (issue #1731)."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import time
import unittest

from _test_temp_root import _sweep, make_test_tempdir, test_temp_root


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


class TestTempRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = test_temp_root()

    def test_make_test_tempdir_nests_under_the_shared_root(self) -> None:
        holder = make_test_tempdir(prefix="omh-selftest-")
        self.addCleanup(holder.cleanup)
        nested = Path(holder.name)
        self.assertEqual(nested.parent, self.root)
        self.assertTrue(nested.name.startswith("omh-selftest-"))

    def test_sweep_removes_only_entries_older_than_the_bound(self) -> None:
        stale = self.root / "omh-selftest-stale"
        fresh = self.root / "omh-selftest-fresh"
        stale.mkdir()
        fresh.mkdir()
        self.addCleanup(lambda: _rmtree(stale))
        self.addCleanup(lambda: _rmtree(fresh))
        _age(stale, 25 * 60 * 60)  # older than the 24h bound
        _age(fresh, 60)  # one minute old: must be left alone

        _sweep(self.root, stale_after_seconds=24 * 60 * 60)

        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_sweep_never_follows_a_symlink_out_of_the_parent(self) -> None:
        outside = make_test_tempdir(prefix="omh-selftest-outside-")
        self.addCleanup(outside.cleanup)
        outside_marker = Path(outside.name) / "must-survive"
        outside_marker.write_text("kept", encoding="utf-8")

        link = self.root / "omh-selftest-link"
        try:
            link.symlink_to(outside.name, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation not permitted in this environment")
        self.addCleanup(lambda: link.unlink(missing_ok=True))
        # os.utime on a symlink itself (not its target) requires
        # follow_symlinks=False; age the link, not what it points at.
        stamp = time.time() - 25 * 60 * 60
        os.utime(link, (stamp, stamp), follow_symlinks=False)

        _sweep(self.root, stale_after_seconds=24 * 60 * 60)

        self.assertTrue(outside_marker.exists(), "sweep followed a symlink out of the parent")

    def test_sweep_ignores_a_directory_that_vanishes_mid_scan(self) -> None:
        racing = self.root / "omh-selftest-race"
        racing.mkdir()
        _age(racing, 25 * 60 * 60)
        racing.rmdir()  # simulate a sibling sweeper winning the race

        _sweep(self.root, stale_after_seconds=24 * 60 * 60)  # must not raise


if __name__ == "__main__":
    unittest.main()

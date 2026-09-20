"""`_read_tui_key()` against a real pty.

The keyboard menus in `omh setup` are tested elsewhere by patching
`_read_tui_key`, so nothing in the suite has ever exercised the terminal
handling itself. Both defects this file pins live entirely inside that
function, so only a real tty can see them:

* it discarded input the operator had already typed, because
  `tty.setraw(fd)` defaults to TCSAFLUSH and it runs once per keypress;
* it read a fixed two characters after an ESC, leaving the rest of a longer
  keypress in the queue for the next reader.

Every read here is bounded by a watchdog. A blocked read is reported as a
failure naming what blocked, because the whole point of the second fix is
that a prompt must not be able to hang.
"""

from __future__ import annotations

import io
import os
import sys
import threading
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.commands import setup as setup_module  # noqa: E402

try:  # POSIX only; Windows has no pty and no termios.
    import pty
    import termios
except ImportError:  # pragma: no cover - exercised by the skip below
    pty = None
    termios = None


_TERMIOS_LFLAG = 3
_READ_WATCHDOG_SECONDS = 3.0


def _comparable_mode(attributes: list) -> list:
    """Terminal attributes with the kernel's own bookkeeping bit removed.

    PENDIN means "input arrived that has yet to be retyped". The kernel sets
    it across a mode change on its own, so it is not evidence about whether
    the saved mode was put back, and comparing it would make a correct
    restore look like a failed one.
    """
    pending = getattr(termios, "PENDIN", 0)
    settled = list(attributes)
    settled[_TERMIOS_LFLAG] = settled[_TERMIOS_LFLAG] & ~pending
    return settled


@unittest.skipUnless(pty is not None and termios is not None, "needs a POSIX pty")
class RawKeyReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.master, self.slave = pty.openpty()
        # Echo off. A pty echoes its input back as output, nobody is reading
        # the master here, and `tcsetattr` with TCSADRAIN or TCSAFLUSH waits
        # for output to drain -- so an echoing fixture hangs inside the very
        # call under test, for a reason that has nothing to do with it.
        mode = termios.tcgetattr(self.slave)
        mode[_TERMIOS_LFLAG] &= ~termios.ECHO
        termios.tcsetattr(self.slave, termios.TCSANOW, mode)
        # TextIOWrapper over FileIO, which is what `sys.stdin` is on a
        # terminal: `_read_tui_key` reads characters, not bytes, so a
        # multi-byte keypress still arrives as one character.
        self.stdin = io.TextIOWrapper(io.FileIO(self.slave, "rb", closefd=False), encoding="utf-8", newline="")
        patcher = patch.object(sys, "stdin", self.stdin)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._close)

    def _close(self) -> None:
        # Master first: a read still blocked in an abandoned watchdog thread
        # is released by it, so the suite does not leave one behind.
        for closer in (lambda: os.close(self.master), lambda: os.close(self.slave)):
            try:
                closer()
            except OSError:
                pass

    def press(self, sequence: bytes) -> None:
        """What the terminal sends for one keypress, as one burst."""
        os.write(self.master, sequence)

    def read_key(self, *, timeout: float = _READ_WATCHDOG_SECONDS) -> str | None:
        """`_read_tui_key()`, or None if it blocked waiting for more input."""
        result: list[str] = []

        def run() -> None:
            try:
                result.append(setup_module._read_tui_key())
            except (OSError, ValueError):  # the fd closed under an abandoned read
                pass

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout)
        return result[0] if result else None

    def assertKey(self, expected: str) -> None:
        key = self.read_key()
        self.assertIsNotNone(key, f"the read blocked instead of returning {expected!r}")
        self.assertEqual(key, expected)

    # -- the type-ahead flush ------------------------------------------------

    def test_a_key_typed_while_the_menu_repaints_is_not_discarded(self) -> None:
        # The menu is between keypresses, so the tty is back in its cooked
        # mode; the operator presses Down while the screen is being redrawn.
        # tty.setraw's default TCSAFLUSH threw this away and then blocked.
        self.press(b"\x1b[B")
        self.assertKey("\x1b[B")

    def test_several_keys_typed_ahead_are_all_delivered_in_order(self) -> None:
        self.press(b"\x1b[B\x1b[B\x1b[A")
        self.assertKey("\x1b[B")
        self.assertKey("\x1b[B")
        self.assertKey("\x1b[A")

    def test_a_plain_key_typed_ahead_survives_too(self) -> None:
        self.press(b"j2")
        self.assertKey("j")
        self.assertKey("2")

    # -- reading a whole sequence -------------------------------------------

    def test_a_six_byte_keypress_is_consumed_whole(self) -> None:
        # Ctrl+Left. Reading three characters returned '\x1b[1' and left
        # ';5D' behind; with the flush gone, the next reads would have taken
        # that for three keypresses, and '5' is a menu choice.
        #
        # "Nothing was left" is asserted by sending a real keypress next and
        # requiring it back, rather than by watching a read time out: a read
        # that times out here is one still blocked on the fd, and it would
        # swallow the next test's input.
        self.press(b"\x1b[1;5D")
        self.assertKey("\x1b[1;5D")
        self.press(b"j")
        self.assertKey("j")

    def test_no_digit_of_a_modified_arrow_can_arrive_as_its_own_keypress(self) -> None:
        # The menu selects an option when a key equals that option's choice
        # string, so a bare '1' or '5' reaching it would silently pick a row.
        # A complete sequence starts with ESC and so can never equal one.
        for sequence in (b"\x1b[1;5D", b"\x1b[1;2A", b"\x1b[15~", b"\x1b[200~"):
            with self.subTest(sequence=sequence):
                self.press(sequence)
                self.assertKey(sequence.decode())
                self.press(b"j")
                self.assertKey("j")

    def test_the_next_prompt_receives_exactly_what_was_typed_next(self) -> None:
        # The leak that reached a different prompt: the operator answered the
        # menu, then typed `og` at the free-text prompt after it and got
        # `;5Dog`. Nothing `_ask` can filter, because `;5D` is printable.
        self.press(b"\x1b[1;5D")
        self.assertKey("\x1b[1;5D")
        self.press(b"og\n")
        self.assertEqual(self.stdin.readline(), "og\n")

    def test_the_arrows_and_their_application_mode_form_both_still_work(self) -> None:
        for sequence in (b"\x1b[A", b"\x1b[B", b"\x1bOA", b"\x1bOB"):
            with self.subTest(sequence=sequence):
                self.press(sequence)
                self.assertKey(sequence.decode())

    def test_an_ordinary_key_is_returned_unchanged(self) -> None:
        # CR and Ctrl+C are left out on purpose rather than overlooked. A key
        # typed ahead reaches the queue while the tty is still cooked, so the
        # line discipline has already had it: ICRNL turns CR into NL and ISIG
        # turns Ctrl+C into a signal. That is true of a real terminal too, so
        # pinning them here would pin the line discipline, not this function.
        for sequence in (b"j", b"k", b" ", b"3"):
            with self.subTest(sequence=sequence):
                self.press(sequence)
                self.assertKey(sequence.decode())

    def test_a_multi_byte_character_arrives_as_one_character(self) -> None:
        # `sys.stdin.read(1)` reads a character, not a byte; reading bytes
        # here would split this one three ways.
        self.press("한".encode())
        self.assertKey("한")

    # -- the bounds ----------------------------------------------------------

    def test_a_lone_escape_returns_instead_of_waiting_for_a_sequence(self) -> None:
        # Someone pressing Esc to back out. Nothing follows it, so a read
        # that waits for two more characters never returns.
        self.press(b"\x1b")
        self.assertKey("\x1b")

    def test_a_sequence_cut_short_returns_what_arrived(self) -> None:
        self.press(b"\x1b[")
        key = self.read_key()
        self.assertIsNotNone(key, "a truncated sequence blocked the prompt")
        self.assertEqual(key, "\x1b[")

    def test_a_parameter_run_with_no_final_byte_stops_at_the_cap(self) -> None:
        # Not a keyboard: a wedged or hostile stream. The read has to end on
        # the character cap rather than grow with the input.
        self.press(b"\x1b[" + b"1" * 200)
        key = self.read_key()
        self.assertIsNotNone(key, "an unterminated parameter run blocked the prompt")
        self.assertEqual(len(key), 1 + setup_module._ESCAPE_SEQUENCE_MAX_CHARACTERS)

    def test_the_terminal_mode_is_restored_after_every_read(self) -> None:
        # Including VMIN/VTIME, which the tail read changes and the caller
        # never puts back itself -- restoring the whole saved mode is what
        # puts them back, so a later `input()` is not left on a timeout.
        before = _comparable_mode(termios.tcgetattr(self.slave))
        self.press(b"\x1b[1;5D")
        self.assertKey("\x1b[1;5D")
        self.assertEqual(_comparable_mode(termios.tcgetattr(self.slave)), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

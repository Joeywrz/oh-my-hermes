"""Contracts for `omh --resume` and the copyable line the TUI exit prints.

Two surfaces, pinned separately.

The pass-through is argv: which elements reach `hermes`, in which order, and
that a bare-launch flag typed alongside a subcommand is refused by name
rather than dropped.

The line is a selection over Hermes' own `sessions` table, and the whole of
its contract is when it declines. This code runs after the person's session
has already ended, with no way to confirm anything it decides, so a line
naming the wrong session is worse than no line at all. Every rule below is
therefore a negative one, and each is proved by a fixture that differs from
the naming case in exactly the one field the rule reads.

The fixtures are real temporary SQLite files carrying the real column names
from `hermes_state_common.py`, including `title` and `cwd`, which nothing
here may read: `title` because the printed line carries the id and nothing
else, `cwd` because `hermes --resume` restores the session's own working
directory unless `--no-restore-cwd` is given, so a resumed session's `cwd`
legitimately differs from where the launch happened.

What these tests do NOT establish: that a live TUI exit is observed at all.
No test here launches Hermes, so the claim is about the selection rules and
the argv, never about an end-to-end exit on a real machine.
"""

from __future__ import annotations

import io
import sqlite3
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _cli_harness import run_cli
from omh.commands.main import _launch_hermes_tui, build_parser
from omh.maintenance.hermes_resume import (
    RESUME_END_PROXIMITY_SECONDS,
    hermes_state_db_path,
    resumable_session_id,
    resume_command_line,
)

# A child that ran for a minute and exited at this instant. Fixed rather than
# clock-derived so no case here can go red with the passage of time.
CHILD_STARTED_AT = 1_800_000_000.0
CHILD_ENDED_AT = CHILD_STARTED_AT + 60.0

_SCHEMA = (
    "CREATE TABLE sessions ("
    "id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL, "
    "ended_at REAL, end_reason TEXT, message_count INTEGER DEFAULT 0, "
    "title TEXT, cwd TEXT)"
)
_INSERT = (
    "INSERT INTO sessions (id, source, started_at, ended_at, message_count, title, cwd) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)

# Sentinels for the two columns the selection may never carry out of the
# database. They are deliberately distinctive strings.
SECRET_TITLE = "renaming the production cluster"
SECRET_CWD = "/private/somewhere/else"


def write_sessions(path: Path, rows: list[tuple]) -> None:
    """Create a real state database holding exactly *rows*.

    Each row is ``(id, source, started_at, ended_at, message_count)``; the
    title and cwd are filled with the sentinels so any leak is visible.
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute(_SCHEMA)
        connection.executemany(
            _INSERT,
            [(*row, SECRET_TITLE, SECRET_CWD) for row in rows],
        )
        connection.commit()
    finally:
        connection.close()


class _TtyStream(io.StringIO):
    """A captured stream that still answers `isatty()` the way a terminal does."""

    def isatty(self) -> bool:
        return True


@contextmanager
def terminal_stdio():
    """Run with stdin/stdout that look like a terminal and capture stdout."""
    stdout = _TtyStream()
    previous_in, previous_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = _TtyStream(), stdout
    try:
        yield stdout
    finally:
        sys.stdin, sys.stdout = previous_in, previous_out


@contextmanager
def launched(hermes_home: Path, argv: list[str], *, returncode: int = 0):
    """Run the bare launch with a faked `hermes` child.

    Yields ``(recorded, stdout, status)``: the argv handed to
    ``subprocess.run``, everything the launch printed, and the code it
    returned.
    """
    recorded: list[list[str]] = []

    class _Completed:
        def __init__(self, code: int) -> None:
            self.returncode = code

    def fake_run(command, *args, **kwargs):
        recorded.append(list(command))
        return _Completed(returncode)

    parsed = build_parser().parse_args(["--hermes-home", str(hermes_home), *argv])
    with terminal_stdio() as stdout, \
            patch("shutil.which", return_value="/usr/local/bin/hermes"), \
            patch("omh.commands.main._run_startup_update_check"), \
            patch("omh.commands.main.subprocess.run", side_effect=fake_run), \
            patch("omh.commands.main.time.time", side_effect=[CHILD_STARTED_AT, CHILD_ENDED_AT]):
        status = _launch_hermes_tui(parsed)
    yield recorded, stdout.getvalue(), status


class SessionSelectionTests(unittest.TestCase):
    """Which session, if any, the launch names after the child exits."""

    def test_the_one_session_that_ended_in_the_window_is_named(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            write_sessions(db, [("sess-alpha", "tui", CHILD_STARTED_AT + 1, CHILD_ENDED_AT - 0.2, 12)])

            self.assertEqual(
                resumable_session_id(db, CHILD_STARTED_AT, CHILD_ENDED_AT),
                "sess-alpha",
            )

    def test_an_empty_table_names_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            write_sessions(db, [])

            self.assertIsNone(resumable_session_id(db, CHILD_STARTED_AT, CHILD_ENDED_AT))

    def test_a_session_that_ended_well_before_the_child_exited_names_nothing(self) -> None:
        # Inside the child's lifetime, so the window keeps it, but nowhere
        # near the exit: some earlier terminal in the same minute.
        stale = CHILD_ENDED_AT - RESUME_END_PROXIMITY_SECONDS - 1.0
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            write_sessions(db, [("sess-stale", "tui", CHILD_STARTED_AT + 1, stale, 12)])

            self.assertIsNone(resumable_session_id(db, CHILD_STARTED_AT, CHILD_ENDED_AT))

    def test_two_sessions_ending_together_name_nothing(self) -> None:
        # Two TUIs closed at once. Either could be the child's, so neither is.
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            write_sessions(
                db,
                [
                    ("sess-alpha", "tui", CHILD_STARTED_AT + 1, CHILD_ENDED_AT - 0.2, 12),
                    ("sess-beta", "tui", CHILD_STARTED_AT + 1, CHILD_ENDED_AT - 0.4, 9),
                ],
            )

            self.assertIsNone(resumable_session_id(db, CHILD_STARTED_AT, CHILD_ENDED_AT))

    def test_an_earlier_session_outside_the_tolerance_does_not_block_the_naming(self) -> None:
        # The positive control for the rule above: a second session is only
        # an ambiguity while it is close enough to be confused with the first.
        far = CHILD_ENDED_AT - RESUME_END_PROXIMITY_SECONDS - 0.5
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            write_sessions(
                db,
                [
                    ("sess-alpha", "tui", CHILD_STARTED_AT + 1, CHILD_ENDED_AT - 0.2, 12),
                    ("sess-earlier", "tui", CHILD_STARTED_AT + 0.5, far, 9),
                ],
            )

            self.assertEqual(
                resumable_session_id(db, CHILD_STARTED_AT, CHILD_ENDED_AT),
                "sess-alpha",
            )

    def test_a_session_with_no_messages_names_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            write_sessions(db, [("sess-empty", "tui", CHILD_STARTED_AT + 1, CHILD_ENDED_AT - 0.2, 0)])

            self.assertIsNone(resumable_session_id(db, CHILD_STARTED_AT, CHILD_ENDED_AT))

    def test_a_session_from_another_source_is_not_a_candidate(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            write_sessions(db, [("sess-kanban", "kanban", CHILD_STARTED_AT + 1, CHILD_ENDED_AT - 0.2, 12)])

            self.assertIsNone(resumable_session_id(db, CHILD_STARTED_AT, CHILD_ENDED_AT))

    def test_a_session_that_never_ended_is_not_a_candidate(self) -> None:
        # Still running, or gateway-owned: the gateway deliberately never ends
        # a session another backend holds, so `ended_at` stays NULL.
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            write_sessions(db, [("sess-live", "tui", CHILD_STARTED_AT + 1, None, 12)])

            self.assertIsNone(resumable_session_id(db, CHILD_STARTED_AT, CHILD_ENDED_AT))

    def test_a_session_that_ended_before_the_child_started_is_not_a_candidate(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            write_sessions(
                db,
                [("sess-yesterday", "tui", CHILD_STARTED_AT - 500, CHILD_STARTED_AT - 1, 12)],
            )

            self.assertIsNone(resumable_session_id(db, CHILD_STARTED_AT, CHILD_ENDED_AT))

    def test_a_short_launch_does_not_reach_back_before_it_started(self) -> None:
        # The window's lower bound only does work a launch shorter than the
        # tolerance can expose: with a long child the proximity rule already
        # implies it. Here the child lived one second, and a session that
        # ended two seconds before it started is still within the tolerance
        # of its exit -- only the window keeps it out.
        started = CHILD_STARTED_AT
        ended = started + 1.0
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            write_sessions(db, [("sess-previous", "tui", started - 60, started - 2.0, 12)])

            self.assertIsNone(resumable_session_id(db, started, ended))

    def test_a_session_that_ended_after_the_child_did_is_not_a_candidate(self) -> None:
        # Another terminal closing a fraction of a second later. It is the
        # newest ended session and it is well inside the tolerance, so only
        # the window's upper bound keeps it from being named.
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            write_sessions(db, [("sess-later", "tui", CHILD_STARTED_AT + 1, CHILD_ENDED_AT + 0.5, 12)])

            self.assertIsNone(resumable_session_id(db, CHILD_STARTED_AT, CHILD_ENDED_AT))

    def test_a_session_left_open_alongside_the_closing_one_is_not_a_rival(self) -> None:
        # The gateway-owned case from the other side: a session nothing ended
        # is not a candidate at all, so it cannot make the closing one
        # ambiguous.
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            write_sessions(
                db,
                [
                    ("sess-alpha", "tui", CHILD_STARTED_AT + 1, CHILD_ENDED_AT - 0.2, 12),
                    ("sess-gateway", "tui", CHILD_STARTED_AT + 2, None, 40),
                ],
            )

            self.assertEqual(
                resumable_session_id(db, CHILD_STARTED_AT, CHILD_ENDED_AT),
                "sess-alpha",
            )

    def test_a_missing_database_names_nothing_and_raises_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertIsNone(
                resumable_session_id(Path(tmp) / "state.db", CHILD_STARTED_AT, CHILD_ENDED_AT)
            )

    def test_a_corrupt_database_names_nothing_and_raises_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            db.write_bytes(b"this is not a database, it is a note about one\n" * 64)

            self.assertIsNone(resumable_session_id(db, CHILD_STARTED_AT, CHILD_ENDED_AT))

    def test_a_foreign_schema_names_nothing_and_raises_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            connection = sqlite3.connect(db)
            try:
                connection.execute("CREATE TABLE notes (id TEXT)")
                connection.commit()
            finally:
                connection.close()

            self.assertIsNone(resumable_session_id(db, CHILD_STARTED_AT, CHILD_ENDED_AT))

    def test_a_locked_database_names_nothing_and_raises_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            write_sessions(db, [("sess-alpha", "tui", CHILD_STARTED_AT + 1, CHILD_ENDED_AT - 0.2, 12)])
            holder = sqlite3.connect(db, isolation_level=None)
            try:
                holder.execute("BEGIN EXCLUSIVE")

                self.assertIsNone(resumable_session_id(db, CHILD_STARTED_AT, CHILD_ENDED_AT))
            finally:
                holder.close()

    def test_no_database_path_names_nothing(self) -> None:
        self.assertIsNone(resumable_session_id(None, CHILD_STARTED_AT, CHILD_ENDED_AT))

    def test_the_selection_reads_neither_the_title_nor_the_working_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            write_sessions(db, [("sess-alpha", "tui", CHILD_STARTED_AT + 1, CHILD_ENDED_AT - 0.2, 12)])

            named = resumable_session_id(db, CHILD_STARTED_AT, CHILD_ENDED_AT)

            self.assertEqual(named, "sess-alpha")
            self.assertNotIn(SECRET_TITLE, resume_command_line(named))
            self.assertNotIn(SECRET_CWD, resume_command_line(named))

    def test_a_session_whose_directory_moved_is_still_named(self) -> None:
        # `hermes --resume` restores the session's own cwd, so a resumed
        # session legitimately ended somewhere other than the launch
        # directory. Filtering on cwd would lose exactly the resumed case.
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            write_sessions(db, [("sess-moved", "tui", CHILD_STARTED_AT + 1, CHILD_ENDED_AT - 0.2, 4)])

            self.assertEqual(
                resumable_session_id(db, CHILD_STARTED_AT, CHILD_ENDED_AT),
                "sess-moved",
            )


class StateDatabasePathTests(unittest.TestCase):
    def test_the_default_home_keeps_its_database_at_the_root(self) -> None:
        self.assertEqual(
            hermes_state_db_path(Path("/tmp/hermes-home")),
            Path("/tmp/hermes-home/state.db"),
        )

    def test_a_profile_reads_its_own_database(self) -> None:
        self.assertEqual(
            hermes_state_db_path(Path("/tmp/hermes-home"), "work"),
            Path("/tmp/hermes-home/profiles/work/state.db"),
        )

    def test_a_name_that_could_not_be_a_profile_directory_yields_no_path(self) -> None:
        for name in ("../../etc", "Work", "", "has space", "a/b"):
            with self.subTest(name=name):
                self.assertIsNone(hermes_state_db_path(Path("/tmp/hermes-home"), name))


class ResumeLineTests(unittest.TestCase):
    def test_the_line_without_a_profile(self) -> None:
        self.assertEqual(resume_command_line("sess-alpha"), "omh --resume sess-alpha")

    def test_the_line_with_a_profile(self) -> None:
        # A profile has its own state.db, so the line is useless without it.
        self.assertEqual(
            resume_command_line("sess-alpha", "work"),
            "omh -p work --resume sess-alpha",
        )


class PassThroughArgvTests(unittest.TestCase):
    """What the bare launch hands to `hermes`."""

    def _argv_for(self, argv: list[str]) -> list[str]:
        with TemporaryDirectory() as tmp:
            with launched(Path(tmp), argv) as (recorded, _stdout, _status):
                pass
        self.assertEqual(len(recorded), 1)
        return recorded[0]

    def test_a_bare_launch_adds_nothing(self) -> None:
        self.assertEqual(self._argv_for([]), ["/usr/local/bin/hermes"])

    def test_resume_is_forwarded_as_its_own_element(self) -> None:
        self.assertEqual(
            self._argv_for(["--resume", "sess alpha"]),
            ["/usr/local/bin/hermes", "--resume", "sess alpha"],
        )

    def test_the_short_resume_form_forwards_the_same_way(self) -> None:
        self.assertEqual(
            self._argv_for(["-r", "sess-alpha"]),
            ["/usr/local/bin/hermes", "--resume", "sess-alpha"],
        )

    def test_continue_without_a_name_forwards_the_flag_alone(self) -> None:
        self.assertEqual(
            self._argv_for(["--continue"]),
            ["/usr/local/bin/hermes", "--continue"],
        )

    def test_continue_with_a_name_forwards_both(self) -> None:
        self.assertEqual(
            self._argv_for(["-c", "yesterday's plan"]),
            ["/usr/local/bin/hermes", "--continue", "yesterday's plan"],
        )

    def test_a_profile_is_forwarded(self) -> None:
        self.assertEqual(
            self._argv_for(["-p", "work"]),
            ["/usr/local/bin/hermes", "--profile", "work"],
        )

    def test_the_forwarding_order_is_fixed(self) -> None:
        # Profile first because it selects the home the session lives in.
        self.assertEqual(
            self._argv_for(["--resume", "sess-alpha", "--profile", "work"]),
            ["/usr/local/bin/hermes", "--profile", "work", "--resume", "sess-alpha"],
        )


class LaunchExitTests(unittest.TestCase):
    """What the launch prints, and what it returns, once the child is gone."""

    def _home_with_session(self, tmp: str, *, profile: str | None = None) -> Path:
        home = Path(tmp)
        directory = home / "profiles" / profile if profile else home
        directory.mkdir(parents=True, exist_ok=True)
        write_sessions(
            directory / "state.db",
            [("sess-alpha", "tui", CHILD_STARTED_AT + 1, CHILD_ENDED_AT - 0.2, 12)],
        )
        return home

    def test_a_clean_exit_prints_the_copyable_line(self) -> None:
        with TemporaryDirectory() as tmp:
            home = self._home_with_session(tmp)
            with launched(home, []) as (_recorded, stdout, status):
                pass

        self.assertEqual(status, 0)
        self.assertEqual(stdout, "omh --resume sess-alpha\n")

    def test_an_interrupted_exit_prints_the_line_too(self) -> None:
        # 130 is the other code Hermes' own epilogue treats as a normal close.
        with TemporaryDirectory() as tmp:
            home = self._home_with_session(tmp)
            with launched(home, [], returncode=130) as (_recorded, stdout, status):
                pass

        self.assertEqual(status, 130)
        self.assertEqual(stdout, "omh --resume sess-alpha\n")

    def test_a_profile_launch_prints_the_profile_in_the_line(self) -> None:
        with TemporaryDirectory() as tmp:
            home = self._home_with_session(tmp, profile="work")
            with launched(home, ["-p", "work"]) as (_recorded, stdout, _status):
                pass

        self.assertEqual(stdout, "omh -p work --resume sess-alpha\n")

    def test_a_failed_exit_prints_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            home = self._home_with_session(tmp)
            with launched(home, [], returncode=2) as (_recorded, stdout, status):
                pass

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")

    def test_an_exit_code_is_returned_unchanged(self) -> None:
        with TemporaryDirectory() as tmp:
            home = self._home_with_session(tmp)
            for code in (0, 1, 2, 42, 130):
                with self.subTest(code=code):
                    with launched(home, [], returncode=code) as (_recorded, _stdout, status):
                        pass
                    self.assertEqual(status, code)

    def test_a_home_with_no_database_prints_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            with launched(Path(tmp), []) as (_recorded, stdout, status):
                pass

        self.assertEqual(status, 0)
        self.assertEqual(stdout, "")

    def test_a_non_terminal_stdout_launches_nothing_at_all(self) -> None:
        # The TTY rule the line inherits: nothing is launched, so there is no
        # child to name a session for.
        recorded: list[list[str]] = []

        def fake_run(command, *args, **kwargs):  # pragma: no cover - must not run
            recorded.append(list(command))
            raise AssertionError("the launch must not reach a child without a terminal")

        parsed = build_parser().parse_args([])
        with patch("shutil.which", return_value="/usr/local/bin/hermes"), \
                patch("omh.commands.main.subprocess.run", side_effect=fake_run):
            self.assertIsNone(_launch_hermes_tui(parsed))
        self.assertEqual(recorded, [])


class SubcommandRefusalTests(unittest.TestCase):
    """A bare-launch flag typed alongside a subcommand is named, not dropped."""

    def test_resume_with_a_subcommand_is_refused_by_name(self) -> None:
        status, _stdout, stderr = run_cli(["--resume", "sess-alpha", "doctor"])

        self.assertEqual(status, 2)
        self.assertIn("--resume", stderr)
        self.assertIn("doctor", stderr)

    def test_continue_with_a_subcommand_is_refused_by_name(self) -> None:
        status, _stdout, stderr = run_cli(["-c", "yesterday", "doctor"])

        self.assertEqual(status, 2)
        self.assertIn("--continue", stderr)

    def test_a_profile_with_a_subcommand_is_refused_by_name(self) -> None:
        status, _stdout, stderr = run_cli(["-p", "work", "doctor"])

        self.assertEqual(status, 2)
        self.assertIn("--profile", stderr)

    def test_a_valueless_continue_with_a_subcommand_is_refused(self) -> None:
        status, _stdout, stderr = run_cli(["--continue=", "doctor"])

        self.assertEqual(status, 2)
        self.assertIn("--continue", stderr)

    def test_continue_without_a_value_swallows_the_next_token(self) -> None:
        """Recorded, not endorsed: argparse gives an optional-value flag the
        next token, so `omh --continue doctor` continues a session named
        "doctor" instead of running the subcommand.

        Hermes' own `-c` behaves identically. It is left alone because a
        session may legitimately be named after a subcommand, so refusing the
        value would refuse a real continue. `--continue=<name>` and a
        subcommand on its own are both unambiguous.
        """
        parsed = build_parser().parse_args(["--continue", "doctor"])

        self.assertIsNone(parsed.command)
        self.assertEqual(parsed.tui_continue, "doctor")

    def test_the_homes_and_scope_options_still_reach_a_subcommand(self) -> None:
        # The refusal is scoped to the three bare-launch flags; the options
        # every subcommand honours are untouched.
        with TemporaryDirectory() as tmp:
            status, _stdout, _stderr = run_cli(["--hermes-home", tmp, "docs", "roles", "--check"])

        self.assertEqual(status, 0)


if __name__ == "__main__":
    unittest.main()

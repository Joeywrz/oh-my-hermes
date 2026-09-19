"""Contracts for the way back from a prepared delegation route (#1724).

`write_delegation_route` sets the three `delegation.*` keys Hermes re-reads per
dispatch. Nothing put them back, so a route written for one lane of one session
stayed as every later session's delegation default. These tests pin the return
path: the baseline captured once, the ownership check that decides whether OMH
may put it back, the two restore triggers, and the lock that keeps two
concurrent writers from agreeing on a wrong baseline.

Every test passes an explicit temporary Hermes home and OMH home. Nothing here
may read or write the developer's own `~/.hermes` or `~/.omh`.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from omh.plugin_bundle.omh.awareness_delivery import _awareness_delivery_lock
from omh.plugin_bundle.omh.delegation_route_restore import (
    DELEGATION_ROUTE_RESTORE_SCHEMA_VERSION,
    delegation_route_restore_path,
    load_route_restore_record,
    restore_delegation_baseline,
    write_route_with_baseline,
    writer_session_liveness,
)
from omh.plugin_bundle.omh.delegation_routing import read_delegation_route
from omh.plugin_bundle.omh.hooks.session_hooks import on_session_end, on_session_start
from omh.plugin_bundle.omh.live_session import LIVE_TUI_SESSION_FRESH_SECONDS
from omh.plugin_bundle.omh.metadata import PROVIDED_HOOKS
from omh.plugin_bundle.omh.tools.delegate_route_tool import omh_delegate_route_handler

BASE_CONFIG = (
    "# keep this comment\n"
    "model:\n"
    "  provider: test-parent\n"
    "delegation:\n"
    "  max_concurrent_children: 4\n"
    "display:\n"
    "  skin: test-skin\n"
)

PINNED_CONFIG = (
    "# keep this comment\n"
    "model:\n"
    "  provider: test-parent\n"
    "delegation:\n"
    "  max_concurrent_children: 4\n"
    "  model: 'person-model'\n"
    "  reasoning_effort: 'medium'\n"
    "display:\n"
    "  skin: test-skin\n"
)


class RouteRestoreTestCase(unittest.TestCase):
    """Temporary homes, an explicit config, and no ambient state at all."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.hermes_home = self.root / "hermes"
        self.omh_home = self.root / "omh"
        self.hermes_home.mkdir(parents=True)
        self.omh_home.mkdir(parents=True)
        self.config = self.hermes_home / "config.yaml"
        self.config.write_text(BASE_CONFIG, encoding="utf-8")

    def route(self, *, session_id: str, model: str = "routed-model", effort: str = "high",
              provider: str = "og", now: float | None = None) -> dict:
        return write_route_with_baseline(
            self.hermes_home,
            omh_home=self.omh_home,
            session_id=session_id,
            model=model,
            reasoning_effort=effort,
            provider=provider,
            now=now,
        )

    def restore(self, **kwargs) -> dict:
        kwargs.setdefault("omh_home", self.omh_home)
        return restore_delegation_baseline(self.hermes_home, **kwargs)

    def current(self) -> dict:
        return read_delegation_route(self.hermes_home)

    def record(self) -> dict:
        return load_route_restore_record(self.omh_home)


class BaselineCaptureTest(RouteRestoreTestCase):
    def test_a_route_over_an_empty_section_records_an_absent_key_baseline(self) -> None:
        result = self.route(session_id="s1")

        self.assertEqual(result["status"], "routed")
        self.assertEqual(result["route_restore"], "baseline_captured")
        record = self.record()
        # Absence IS the recorded value: an empty mapping says the file carried
        # none of the three keys, which is what a restore has to reproduce.
        self.assertEqual(record["baseline"], {})
        self.assertEqual(
            record["written"],
            {"model": "routed-model", "reasoning_effort": "high", "provider": "og"},
        )
        self.assertEqual(record["writer_session_id"], "s1")

    def test_a_route_over_a_pinned_key_records_that_key_as_the_baseline(self) -> None:
        self.config.write_text(PINNED_CONFIG, encoding="utf-8")

        self.route(session_id="s1")

        self.assertEqual(
            self.record()["baseline"],
            {"model": "person-model", "reasoning_effort": "medium"},
        )

    def test_the_baseline_survives_three_consecutive_routes(self) -> None:
        self.config.write_text(PINNED_CONFIG, encoding="utf-8")
        self.route(session_id="s1", model="first")
        captured = self.record()["baseline_captured_at"]

        second = self.route(session_id="s1", model="second")
        third = self.route(session_id="s1", model="third")

        self.assertEqual(second["route_restore"], "baseline_retained")
        self.assertEqual(third["route_restore"], "baseline_retained")
        record = self.record()
        self.assertEqual(record["baseline"], {"model": "person-model", "reasoning_effort": "medium"})
        self.assertEqual(record["baseline_captured_at"], captured)
        self.assertEqual(record["written"]["model"], "third")

    def test_an_edit_between_routes_recaptures_the_baseline(self) -> None:
        # OMH no longer owns the value it is about to overwrite, so the old
        # baseline has stopped describing what the person had.
        self.route(session_id="s1")
        self.config.write_text(
            self.config.read_text(encoding="utf-8").replace("'routed-model'", "'hand-edited'"),
            encoding="utf-8",
        )

        result = self.route(session_id="s1", model="second")

        self.assertEqual(result["route_restore"], "baseline_recaptured")
        self.assertEqual(self.record()["baseline"]["model"], "hand-edited")


class SessionEndRestoreTest(RouteRestoreTestCase):
    def test_route_then_session_end_returns_the_three_keys_to_their_pre_route_state(self) -> None:
        before = self.config.read_text(encoding="utf-8")
        self.route(session_id="s1")
        self.assertEqual(self.current()["model"], "routed-model")

        result = self.restore(trigger="session_end", require_writer_session="s1")

        # No key to put back is still a restore of the baseline; it keeps the
        # name the tool used before baselines existed because the end state is
        # the same one.
        self.assertEqual(result["status"], "cleared")
        self.assertEqual(self.current(), {})
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)
        # The record is gone: OMH owns nothing in the file any more.
        self.assertEqual(self.record(), {})
        self.assertFalse(delegation_route_restore_path(self.omh_home).exists())

    def test_a_pinned_baseline_comes_back_verbatim(self) -> None:
        self.config.write_text(PINNED_CONFIG, encoding="utf-8")
        self.route(session_id="s1")

        self.restore(trigger="session_end", require_writer_session="s1")

        self.assertEqual(
            self.current(), {"model": "person-model", "reasoning_effort": "medium"}
        )

    def test_a_session_that_did_not_write_the_route_restores_nothing(self) -> None:
        self.route(session_id="s1")

        result = self.restore(trigger="session_end", require_writer_session="other")

        self.assertEqual(result["status"], "not_last_writer")
        self.assertEqual(self.current()["model"], "routed-model")
        self.assertNotEqual(self.record(), {})


class OwnershipCheckTest(RouteRestoreTestCase):
    def test_a_hand_edited_key_is_left_alone_and_the_stale_record_is_dropped(self) -> None:
        self.route(session_id="s1")
        edited = self.config.read_text(encoding="utf-8").replace("'routed-model'", "'mine'")
        self.config.write_text(edited, encoding="utf-8")

        result = self.restore(trigger="session_end", require_writer_session="s1")

        self.assertEqual(result["status"], "foreign_edit")
        self.assertEqual(self.config.read_text(encoding="utf-8"), edited)
        # Dropped, because a baseline whose written value no longer matches
        # anything in the file can only mislead a later restore.
        self.assertEqual(self.record(), {})

    def test_one_changed_key_of_three_is_enough_to_withhold_the_restore(self) -> None:
        self.route(session_id="s1")
        self.config.write_text(
            self.config.read_text(encoding="utf-8").replace("'high'", "'low'"), encoding="utf-8"
        )

        result = self.restore(trigger="session_end", require_writer_session="s1")

        self.assertEqual(result["status"], "foreign_edit")
        self.assertEqual(self.current()["reasoning_effort"], "low")

    def test_no_record_at_all_restores_nothing_and_does_not_raise(self) -> None:
        result = self.restore(trigger="session_start", require_writer_not_live=True)

        self.assertEqual(result["status"], "no_baseline_recorded")
        self.assertEqual(self.config.read_text(encoding="utf-8"), BASE_CONFIG)

    def test_a_corrupt_record_reads_as_no_record_rather_than_a_crash(self) -> None:
        self.route(session_id="s1")
        routed = self.config.read_text(encoding="utf-8")
        for corruption in ("not json at all", "{}", '{"schema_version": "wrong/v9"}',
                           '{"schema_version": "%s", "baseline": {"model": "a b c"},'
                           ' "written": {}, "baseline_captured_at": 1, "written_at": 1,'
                           ' "writer_session_id": ""}' % DELEGATION_ROUTE_RESTORE_SCHEMA_VERSION):
            with self.subTest(corruption=corruption[:24]):
                delegation_route_restore_path(self.omh_home).write_text(corruption, encoding="utf-8")

                self.assertEqual(load_route_restore_record(self.omh_home), {})
                result = self.restore(trigger="session_end", require_writer_session="s1")

                self.assertEqual(result["status"], "no_baseline_recorded")
                self.assertEqual(self.config.read_text(encoding="utf-8"), routed)


class OrphanedRouteTest(RouteRestoreTestCase):
    """The session-start path: a killed TUI never reaches `on_session_end`.

    Liveness is asked of the WRITER'S OWN row. The first version asked the
    live-TUI list, which is scoped to "which session is a person looking at";
    a writer on any other surface is absent from that list by construction,
    so its verdict depended on whether an unrelated TUI happened to be open.
    Measured on the owner's homes when this was found: the profile that
    routes most writes every route from a `slack` or `subagent` session, and
    the default home already had `desktop` routes beside its TUI ones.
    """

    def _write_state_db(self, *rows: tuple) -> None:
        """Rows of (id, source, ended_at, activity). No filters are applied."""
        connection = sqlite3.connect(self.hermes_home / "state.db")
        try:
            connection.execute(
                "CREATE TABLE sessions (id TEXT, source TEXT, ended_at TEXT, archived INT,"
                " hidden INT, model_config TEXT, last_activity_at REAL, started_at REAL)"
            )
            for session_id, source, ended_at, activity in rows:
                connection.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, 0, 0, '{}', ?, ?)",
                    (session_id, source, ended_at, activity, activity),
                )
            connection.commit()
        finally:
            connection.close()

    def test_a_non_tui_writer_is_live_even_while_an_unrelated_tui_row_is_open(self) -> None:
        # The regression this class exists for. `slack-writer` is mid-run and
        # about to dispatch; an unrelated TUI is open in the same home.
        self.route(session_id="slack-writer", now=1000.0)
        self._write_state_db(
            ("slack-writer", "slack", None, 1000.0),
            ("someone-elses-tui", "tui", None, 1000.0),
        )

        result = self.restore(
            trigger="session_start", require_writer_not_live=True, now=1100.0
        )

        self.assertEqual(result["status"], "writer_live")
        self.assertEqual(result["liveness"], "live")
        self.assertEqual(result["writer_source"], "slack")
        self.assertEqual(self.current()["model"], "routed-model")

    def test_a_delegated_child_writer_is_asked_about_like_any_other(self) -> None:
        # The TUI list drops rows naming a delegation parent. A subagent
        # writes routes too (33 of them on the owner's routing profile), so
        # its liveness is a real question and not an exclusion.
        self.route(session_id="subagent-writer", now=1000.0)
        self._write_state_db(("subagent-writer", "subagent", None, 1000.0))

        result = self.restore(
            trigger="session_start", require_writer_not_live=True, now=1100.0
        )

        self.assertEqual(result["liveness"], "live")
        self.assertEqual(self.current()["model"], "routed-model")

    def test_a_writer_the_host_closed_is_restored(self) -> None:
        before = self.config.read_text(encoding="utf-8")
        self.route(session_id="finished", now=1000.0)
        self._write_state_db(
            ("finished", "tui", "2026-09-19T10:00:00Z", 1000.0),
            ("someone-else", "tui", None, 1000.0),
        )

        result = self.restore(
            trigger="session_start", require_writer_not_live=True, now=1100.0
        )

        # The one observation that clears a restore: the host closed the row.
        self.assertEqual(result["liveness"], "not_live")
        self.assertEqual(result["status"], "cleared")
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_a_row_the_host_never_closed_waits_out_the_age_bound(self) -> None:
        # A killed TUI, and every gateway session of a kind that never ends,
        # leave an open row behind. Nothing here observes the process is
        # gone, so the route's own age decides and the verdict says so.
        # The shape a gateway session leaves: the row is open, its activity
        # stamp is old because the host stopped re-stamping it, and the route
        # is newer than the stamp. The stale stamp must not read as "gone"
        # while the route is still fresh.
        written = 20_000.0
        self.route(session_id="killed", now=written)
        self._write_state_db(("killed", "tui", None, 0.0))

        held = self.restore(
            trigger="session_start", require_writer_not_live=True, now=30_000.0
        )

        self.assertEqual(held["liveness"], "unclosed_within_age_bound")
        self.assertEqual(held["status"], "writer_live")
        self.assertEqual(self.current()["model"], "routed-model")

        released = self.restore(
            trigger="session_start",
            require_writer_not_live=True,
            now=written + LIVE_TUI_SESSION_FRESH_SECONDS + 1,
        )

        self.assertEqual(released["liveness"], "unclosed_and_stale_by_age")
        self.assertEqual(released["status"], "cleared")
        self.assertEqual(self.current(), {})

    def test_an_id_with_no_row_is_unanswerable_not_absent(self) -> None:
        self.route(session_id="not-in-the-db", now=1000.0)
        self._write_state_db(("some-other-session", "tui", None, 1000.0))

        held = self.restore(
            trigger="session_start", require_writer_not_live=True, now=1100.0
        )
        released = self.restore(
            trigger="session_start",
            require_writer_not_live=True,
            now=1000.0 + LIVE_TUI_SESSION_FRESH_SECONDS + 1,
        )

        self.assertEqual(held["liveness"], "unknown_within_age_bound")
        self.assertEqual(released["liveness"], "not_live_by_age")

    def test_a_hand_edited_key_survives_session_start_too(self) -> None:
        self.route(session_id="killed", now=0.0)
        edited = self.config.read_text(encoding="utf-8").replace("'routed-model'", "'mine'")
        self.config.write_text(edited, encoding="utf-8")
        self._write_state_db(("killed", "tui", "2026-09-19T10:00:00Z", 0.0))

        result = self.restore(trigger="session_start", require_writer_not_live=True)

        self.assertEqual(result["status"], "foreign_edit")
        self.assertEqual(self.config.read_text(encoding="utf-8"), edited)

    def test_liveness_falls_back_to_a_stated_age_bound_when_no_db_can_answer(self) -> None:
        # No state.db at all: the host surface is silent, and the silence is
        # never read as "the writer is gone". Only the age bound decides, and
        # the returned value says which answered.
        self.assertEqual(
            writer_session_liveness(
                "w", hermes_home=self.hermes_home, written_at=0.0,
                now=LIVE_TUI_SESSION_FRESH_SECONDS - 1,
            ),
            "unknown_within_age_bound",
        )
        self.assertEqual(
            writer_session_liveness(
                "w", hermes_home=self.hermes_home, written_at=0.0,
                now=LIVE_TUI_SESSION_FRESH_SECONDS + 1,
            ),
            "not_live_by_age",
        )

    def test_an_unusable_activity_stamp_is_not_read_as_recent(self) -> None:
        self._write_state_db(("odd", "acp", None, None))

        self.assertEqual(
            writer_session_liveness(
                "odd", hermes_home=self.hermes_home, written_at=0.0,
                now=LIVE_TUI_SESSION_FRESH_SECONDS + 1,
            ),
            "unclosed_and_stale_by_age",
        )

    def test_a_route_inside_the_age_bound_is_not_restored_without_a_liveness_row(self) -> None:
        self.route(session_id="unknown-writer", now=100.0)

        result = self.restore(
            trigger="session_start", require_writer_not_live=True, now=200.0
        )

        self.assertEqual(result["status"], "writer_live")
        self.assertEqual(result["liveness"], "unknown_within_age_bound")
        self.assertEqual(self.current()["model"], "routed-model")

    def test_a_route_past_the_age_bound_is_restored_without_a_liveness_row(self) -> None:
        self.route(session_id="unknown-writer", now=100.0)

        result = self.restore(
            trigger="session_start",
            require_writer_not_live=True,
            now=100.0 + LIVE_TUI_SESSION_FRESH_SECONDS + 1,
        )

        self.assertEqual(result["status"], "cleared")
        self.assertEqual(result["liveness"], "not_live_by_age")
        self.assertEqual(self.current(), {})


class ConcurrentWriterTest(RouteRestoreTestCase):
    def test_a_writer_that_cannot_take_the_lock_reports_a_refusal_not_a_route(self) -> None:
        holder_entered = threading.Event()
        release = threading.Event()
        outcome: dict[str, dict] = {}

        def hold() -> None:
            with _awareness_delivery_lock(delegation_route_restore_path(self.omh_home)):
                holder_entered.set()
                release.wait(timeout=5)

        def contend() -> None:
            outcome["result"] = self.route(session_id="s2", model="loser")

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(holder_entered.wait(timeout=5))
        try:
            contender = threading.Thread(target=contend)
            contender.start()
            contender.join(timeout=10)
        finally:
            release.set()
            holder.join(timeout=5)

        result = outcome["result"]
        self.assertEqual(result["status"], "error")
        self.assertNotEqual(result["status"], "routed")
        self.assertEqual(result["route_restore"], "lock_unavailable")
        self.assertIn("another session is writing", result["error"])
        # The refusal is a refusal: nothing was written under it.
        self.assertEqual(self.current(), {})

    def test_a_restore_that_cannot_take_the_lock_reports_it(self) -> None:
        self.route(session_id="s1")
        holder_entered = threading.Event()
        release = threading.Event()
        outcome: dict[str, dict] = {}

        def hold() -> None:
            with _awareness_delivery_lock(delegation_route_restore_path(self.omh_home)):
                holder_entered.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(holder_entered.wait(timeout=5))
        try:
            worker = threading.Thread(
                target=lambda: outcome.__setitem__(
                    "result", self.restore(trigger="session_end", require_writer_session="s1")
                )
            )
            worker.start()
            worker.join(timeout=10)
        finally:
            release.set()
            holder.join(timeout=5)

        self.assertEqual(outcome["result"]["status"], "lock_unavailable")
        self.assertEqual(self.current()["model"], "routed-model")

    def test_session_a_routes_session_b_routes_over_it_and_a_ending_restores_nothing(self) -> None:
        # The interleave the lock and the writer record exist for. Without the
        # writer check, A's session end would pull the baseline out from under
        # B's still-running dispatch lane.
        self.config.write_text(PINNED_CONFIG, encoding="utf-8")
        self.route(session_id="A", model="a-model")
        self.route(session_id="B", model="b-model")

        ended = self.restore(trigger="session_end", require_writer_session="A")

        self.assertEqual(ended["status"], "not_last_writer")
        self.assertEqual(self.current()["model"], "b-model")
        # The person's baseline is still the person's, not A's route.
        self.assertEqual(
            self.record()["baseline"], {"model": "person-model", "reasoning_effort": "medium"}
        )

        finished = self.restore(trigger="session_end", require_writer_session="B")

        self.assertEqual(finished["status"], "restored")
        self.assertEqual(
            self.current(), {"model": "person-model", "reasoning_effort": "medium"}
        )


class RunningChildIsolationTest(RouteRestoreTestCase):
    """Children already running keep their model: unchanged, and now pinned."""

    def test_a_restore_rewrites_only_the_three_keys_and_leaves_every_other_byte(self) -> None:
        before = self.config.read_text(encoding="utf-8")
        self.route(session_id="s1")

        self.restore(trigger="session_end", require_writer_session="s1")

        after = self.config.read_text(encoding="utf-8")
        self.assertEqual(after, before)
        self.assertIn("max_concurrent_children: 4", after)
        self.assertIn("# keep this comment", after)

    def test_the_restore_path_reaches_no_running_process(self) -> None:
        # Hermes resolves the route per NEW dispatch, so a child already built
        # keeps its model as long as nothing here signals or re-dispatches it.
        # The claim is checkable from the source: this module touches files and
        # nothing else.
        source = Path(
            __file__
        ).resolve().parents[1] / "src" / "plugin_bundle" / "omh" / "delegation_route_restore.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            str(node.module).split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }

        self.assertEqual(imported & {"subprocess", "signal", "socket", "multiprocessing"}, set())
        for forbidden in ("kill", "popen", "spawn", "terminate"):
            self.assertNotIn(
                forbidden,
                {
                    node.attr
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute)
                },
                f"{forbidden} would reach past the config file",
            )


class RecordShapeTest(RouteRestoreTestCase):
    def test_the_record_carries_its_claim_boundary_and_no_prompt_content(self) -> None:
        self.route(session_id="s1")

        raw = json.loads(delegation_route_restore_path(self.omh_home).read_text(encoding="utf-8"))

        self.assertEqual(raw["schema_version"], DELEGATION_ROUTE_RESTORE_SCHEMA_VERSION)
        self.assertIn("not evidence that any dispatch ran", raw["claim_boundary"])
        self.assertEqual(sorted(raw["baseline"]), [])
        self.assertEqual(
            sorted(raw["written"]), ["model", "provider", "reasoning_effort"]
        )

    def test_the_record_lives_in_the_omh_home_and_never_in_the_hermes_home(self) -> None:
        self.route(session_id="s1")

        path = delegation_route_restore_path(self.omh_home)

        self.assertTrue(path.is_relative_to(self.omh_home))
        self.assertEqual(
            sorted(entry.name for entry in self.hermes_home.iterdir()), ["config.yaml"]
        )


class HookWiringTest(RouteRestoreTestCase):
    """The two triggers, driven through the hooks Hermes actually calls."""

    def homes(self) -> dict[str, str]:
        return {"omh_home": str(self.omh_home), "hermes_home": str(self.hermes_home)}

    def test_session_end_restores_the_route_its_own_session_wrote(self) -> None:
        self.config.write_text(PINNED_CONFIG, encoding="utf-8")
        self.route(session_id="mine")

        payload = on_session_end(session_id="mine", **self.homes())

        self.assertEqual(payload["route_restore"]["status"], "restored")
        self.assertEqual(
            self.current(), {"model": "person-model", "reasoning_effort": "medium"}
        )
        self.assertNotIn("omh_degradation", payload)

    def test_session_end_leaves_a_later_session_s_route_in_place(self) -> None:
        self.route(session_id="A", model="a-model")
        self.route(session_id="B", model="b-model")

        payload = on_session_end(session_id="A", **self.homes())

        self.assertEqual(payload["route_restore"]["status"], "not_last_writer")
        self.assertEqual(self.current()["model"], "b-model")

    def test_session_start_restores_a_route_left_by_a_killed_session(self) -> None:
        before = self.config.read_text(encoding="utf-8")
        self.route(session_id="killed", now=0.0)

        payload = on_session_start(session_id="fresh", **self.homes())

        self.assertEqual(payload["route_restore"]["status"], "cleared")
        self.assertEqual(payload["route_restore"]["liveness"], "not_live_by_age")
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_session_start_on_a_clean_machine_reports_nothing_to_do(self) -> None:
        payload = on_session_start(session_id="fresh", **self.homes())

        self.assertEqual(payload["route_restore"]["status"], "no_baseline_recorded")
        self.assertNotIn("omh_degradation", payload)
        self.assertEqual(self.config.read_text(encoding="utf-8"), BASE_CONFIG)

    def test_a_stale_route_with_no_record_is_reported_and_left_in_place(self) -> None:
        # The state the issue was measured in, and the state every machine
        # that upgrades into this change is already in: a route OMH wrote
        # before it recorded baselines. OMH cannot know what the keys held
        # before it, so an automatic path may not guess -- it reports and
        # leaves the value for the person (or an explicit `clear`) to settle.
        stale = BASE_CONFIG.replace(
            "  max_concurrent_children: 4\n",
            "  max_concurrent_children: 4\n  model: 'claude-fable-5-1'\n",
        )
        self.config.write_text(stale, encoding="utf-8")

        started = on_session_start(session_id="fresh", **self.homes())
        ended = on_session_end(session_id="fresh", **self.homes())

        self.assertEqual(started["route_restore"]["status"], "no_baseline_recorded")
        self.assertEqual(ended["route_restore"]["status"], "no_baseline_recorded")
        self.assertEqual(self.config.read_text(encoding="utf-8"), stale)
        self.assertEqual(self.current(), {"model": "claude-fable-5-1"})

    def test_a_failing_restore_is_named_in_the_payload_not_swallowed(self) -> None:
        self.route(session_id="mine")
        # A lock nobody can take is the failure shape that has to stay visible:
        # the route is still in the file and the person must be able to learn
        # that OMH did not take it back out.
        holder_entered = threading.Event()
        release = threading.Event()
        payloads: dict[str, dict] = {}

        def hold() -> None:
            with _awareness_delivery_lock(delegation_route_restore_path(self.omh_home)):
                holder_entered.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(holder_entered.wait(timeout=5))
        try:
            worker = threading.Thread(
                target=lambda: payloads.__setitem__(
                    "end", on_session_end(session_id="mine", **self.homes())
                )
            )
            worker.start()
            worker.join(timeout=10)
        finally:
            release.set()
            holder.join(timeout=5)

        payload = payloads["end"]
        self.assertEqual(payload["route_restore"]["status"], "lock_unavailable")
        self.assertTrue(payload["omh_degradation"]["degraded"])
        self.assertEqual(
            [row["component"] for row in payload["omh_degradation"]["components"]],
            ["delegation_route_restore"],
        )

    def test_the_new_hook_is_declared_where_the_loader_reads_it(self) -> None:
        manifest = (
            Path(__file__).resolve().parents[1]
            / "src" / "plugin_bundle" / "omh" / "plugin.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("on_session_start", PROVIDED_HOOKS)
        self.assertIn("  - on_session_start", manifest)


class ClearActionTest(RouteRestoreTestCase):
    """`clear` through the tool, which is where the person-pinned case lands."""

    def call(self, **args) -> dict:
        args.setdefault("hermes_home", str(self.hermes_home))
        args.setdefault("omh_home", str(self.omh_home))
        return json.loads(omh_delegate_route_handler(args, session_id="s1"))

    def test_a_model_the_person_pinned_comes_back_on_clear(self) -> None:
        self.config.write_text(PINNED_CONFIG, encoding="utf-8")
        routed = self.call(action="set", model="routed-model", provider="og",
                           reasoning_effort="high")
        self.assertEqual(routed["status"], "routed")

        cleared = self.call(action="clear")

        self.assertEqual(cleared["status"], "restored")
        self.assertEqual(
            self.current(), {"model": "person-model", "reasoning_effort": "medium"}
        )

    def test_clear_leaves_a_hand_edited_key_alone_and_says_so(self) -> None:
        self.call(action="set", model="routed-model", provider="og", reasoning_effort="high")
        edited = self.config.read_text(encoding="utf-8").replace("'routed-model'", "'mine'")
        self.config.write_text(edited, encoding="utf-8")

        cleared = self.call(action="clear")

        self.assertEqual(cleared["status"], "foreign_edit")
        self.assertEqual(self.config.read_text(encoding="utf-8"), edited)

    def test_clear_still_removes_a_route_no_baseline_was_ever_recorded_for(self) -> None:
        # The machine this issue was measured on. OMH cannot know what the
        # keys held before a route it has no record of, so the explicit
        # request keeps its pre-baseline meaning and names why.
        self.config.write_text(
            BASE_CONFIG.replace(
                "  max_concurrent_children: 4\n",
                "  max_concurrent_children: 4\n  model: 'stale-route'\n",
            ),
            encoding="utf-8",
        )

        self.assertEqual(self.current(), {"model": "stale-route"})

        cleared = self.call(action="clear")

        self.assertEqual(cleared["status"], "cleared")
        self.assertEqual(cleared["route_restore"], "no_baseline_recorded")
        self.assertEqual(self.current(), {})

    def test_an_exhausted_chain_restores_the_pinned_model_instead_of_inheriting(self) -> None:
        # Before, exhaustion cleared to parent inheritance. The user's own
        # pinned model is a better answer to "the chain is out of candidates"
        # and is what they had before OMH touched the file.
        self.config.write_text(PINNED_CONFIG, encoding="utf-8")
        chains = self.omh_home / "routing" / "model-chains.json"
        chains.parent.mkdir(parents=True, exist_ok=True)
        chains.write_text(
            json.dumps(
                {
                    "schema_version": "mixture_chain_overrides/v1",
                    "categories": {"quick": [{"model": "only-head", "reasoning_effort": "low"}]},
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.call(action="set", category="quick")["status"], "routed")

        exhausted = self.call(action="fallback", category="quick")

        self.assertEqual(exhausted["status"], "exhausted_to_inherit")
        self.assertEqual(
            self.current(), {"model": "person-model", "reasoning_effort": "medium"}
        )

    def test_a_route_through_the_tool_records_the_host_session_not_a_tool_argument(self) -> None:
        # Tool args are model-supplied; only the host keyword names a session.
        json.loads(
            omh_delegate_route_handler(
                {
                    "action": "set",
                    "model": "routed-model",
                    "provider": "og",
                    "reasoning_effort": "high",
                    "hermes_home": str(self.hermes_home),
                    "omh_home": str(self.omh_home),
                    "session_id": "claimed-by-the-model",
                },
                session_id="real-host-session",
            )
        )

        self.assertEqual(self.record()["writer_session_id"], "real-host-session")


if __name__ == "__main__":
    unittest.main()

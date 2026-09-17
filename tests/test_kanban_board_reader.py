"""Read-only Hermes Kanban lanes in the OMH HUD.

The board is native Hermes state (``kanban.db`` at the Hermes root); OMH only
projects it. These cases pin where the reader looks, what it refuses, how a
task's native status becomes a HUD verdict, and how the rows merge beside the
delegate_task rows under the shared row cap.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh import kanban_board_reader as reader  # noqa: E402
from omh.plugin_bundle.omh.runtime_reader import (  # noqa: E402
    ACTIVITY_ROW_LIMIT,
    MAX_WIDGET_HUD_BYTES,
    read_omh_hud,
)
from omh.tui_widget_pack import widget_payload  # noqa: E402

NOW = 1_800_000_000

# The columns Hermes' ``SCHEMA_SQL`` declares on ``tasks`` that the reader
# selects, plus the ones the native ``kanban_create`` writes beside them.
TASKS_DDL = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
    status TEXT NOT NULL, priority INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER,
    workspace_kind TEXT NOT NULL DEFAULT 'scratch', workspace_path TEXT,
    branch_name TEXT, claim_lock TEXT, claim_expires INTEGER, result TEXT,
    idempotency_key TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid INTEGER, last_failure_error TEXT, max_runtime_seconds INTEGER,
    last_heartbeat_at INTEGER, current_run_id INTEGER, skills TEXT,
    model_override TEXT, provider_override TEXT, reasoning_effort TEXT,
    max_retries INTEGER, session_id TEXT
);
CREATE TABLE task_links (
    parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
"""


def build_board(db: Path, tasks: list[dict], links: list[tuple[str, str]] = ()) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.executescript(TASKS_DDL)
        for task in tasks:
            columns = ", ".join(task)
            marks = ", ".join("?" for _ in task)
            connection.execute(f"INSERT INTO tasks ({columns}) VALUES ({marks})", tuple(task.values()))
        for parent, child in links:
            connection.execute("INSERT INTO task_links VALUES (?, ?)", (parent, child))


def task(identity: str, status: str, **fields) -> dict:
    row = {"id": identity, "title": f"Task {identity}", "assignee": "miku", "status": status, "created_at": NOW - 600}
    row.update(fields)
    return row


class KanbanPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {"HERMES_KANBAN_HOME": "", "HERMES_KANBAN_BOARD": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_default_board_lives_at_the_hermes_root(self) -> None:
        self.assertEqual(reader.kanban_db_path(self.root / ".hermes"), self.root / ".hermes" / "kanban.db")
        self.assertEqual(reader.kanban_db_path(self.root / ".hermes", "default"), self.root / ".hermes" / "kanban.db")

    def test_a_profile_home_resolves_to_its_root(self) -> None:
        # Hermes shares one board across profiles by design: the board is the
        # root's, never <root>/profiles/<name>/kanban.db.
        profile = self.root / ".hermes" / "profiles" / "coder"
        self.assertEqual(reader.kanban_db_path(profile), self.root / ".hermes" / "kanban.db")
        self.assertEqual(reader.hermes_root(self.root / ".hermes"), self.root / ".hermes")

    def test_kanban_home_override_wins(self) -> None:
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_HOME": str(self.root / "shared")}):
            self.assertEqual(reader.kanban_db_path(self.root / ".hermes"), self.root / "shared" / "kanban.db")

    def test_named_board_and_refused_slug(self) -> None:
        self.assertEqual(
            reader.kanban_db_path(self.root / ".hermes", "team-a"),
            self.root / ".hermes" / "kanban" / "boards" / "team-a" / "kanban.db",
        )
        for slug in ("Team", "../escape", "a b", "-lead", "x" * 65):
            with self.subTest(slug=slug):
                self.assertIsNone(reader.kanban_db_path(self.root / ".hermes", slug))

    @unittest.skipIf(sys.platform == "win32", "symlink creation needs privileges on Windows")
    def test_symlinked_db_or_directory_is_refused(self) -> None:
        hermes = self.root / ".hermes"
        hermes.mkdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        build_board(elsewhere / "kanban.db", [task("t_00000001", "running")])
        (hermes / "kanban.db").symlink_to(elsewhere / "kanban.db")
        self.assertIsNone(reader.kanban_db_path(hermes))
        self.assertEqual(reader.read_kanban_lanes(hermes, now=NOW)["rows"], [])
        boards = hermes / "kanban" / "boards"
        boards.mkdir(parents=True)
        (boards / "linked").symlink_to(elsewhere)
        self.assertIsNone(reader.kanban_db_path(hermes, "linked"))


class KanbanLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hermes = self.root / ".hermes"
        self.hermes.mkdir()
        self.db = self.hermes / "kanban.db"
        self.env = mock.patch.dict(os.environ, {"HERMES_KANBAN_HOME": "", "HERMES_KANBAN_BOARD": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def lanes(self, **kwargs):
        return reader.read_kanban_lanes(self.hermes, now=NOW, **kwargs)

    def test_missing_board_is_empty_and_silent(self) -> None:
        result = self.lanes()
        self.assertEqual(result["rows"], [])
        self.assertEqual((result["scope"], result["dispatcher_presence"], result["board"]), ("none", "unknown", "default"))
        self.assertEqual((result["active"], result["running"], result["blocked"], result["completed"], result["queued"]), (0, 0, 0, 0, 0))

    def test_foreign_schema_is_skipped_by_the_column_guard(self) -> None:
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT)")
            connection.execute("INSERT INTO tasks VALUES ('t_1', 'Old shape', 'running')")
        self.assertEqual(self.lanes()["rows"], [])
        # A file that is not a database at all is the same silence.
        self.db.write_bytes(b"not sqlite")
        self.assertEqual(self.lanes()["rows"], [])

    def test_native_status_maps_to_a_hud_verdict(self) -> None:
        build_board(self.db, [
            task("t_run00001", "running", started_at=NOW - 100, worker_pid=41, last_heartbeat_at=NOW - 5,
                 skills='["research", "review"]', model_override="gpt-5.6-sol", provider_override="openai",
                 reasoning_effort="high"),
            task("t_stale001", "running", started_at=NOW - 3000, worker_pid=42, last_heartbeat_at=NOW - 16 * 60),
            task("t_block001", "blocked", started_at=NOW - 500, last_heartbeat_at=NOW - 400),
            task("t_retry001", "todo", last_failure_error="worker crashed"),
            task("t_ready001", "ready"),
            task("t_revw0001", "review", started_at=NOW - 900, completed_at=None),
            task("t_done0001", "done", started_at=NOW - 800, completed_at=NOW - 300),
            task("t_arch0001", "archived", started_at=NOW - 800, completed_at=NOW - 200),
            task("t_old00001", "done", started_at=NOW - 9000, completed_at=NOW - reader.COMPLETED_LINGER_SECONDS - 1),
        ], links=[("t_run00001", "t_ready001"), ("t_done0001", "t_ready001")])
        result = self.lanes(limit=20)
        by_id = {row["task_id"]: row for row in result["rows"]}
        self.assertNotIn("old00001", by_id, "a finished row past the linger window is history")
        self.assertEqual(
            {identity: (row["state"], row["native_status"]) for identity, row in by_id.items()},
            {
                "run00001": ("running", "running"),
                "stale001": ("stale", "running"),
                "block001": ("blocked", "blocked"),
                "retry001": ("queued", "todo"),
                "ready001": ("queued", "ready"),
                "revw0001": ("queued", "review"),
                "done0001": ("done", "done"),
                "arch0001": ("done", "archived"),
            },
        )
        running = by_id["run00001"]
        self.assertEqual(
            (running["role"], running["model"], running["provider"], running["effort"], running["assignee"]),
            ("research", "gpt-5.6-sol", "openai", "high", "miku"),
        )
        self.assertEqual((running["lane_backend"], running["board"], running["tokens"]), ("kanban", "default", None))
        self.assertEqual(running["elapsed_seconds"], 100.0)
        self.assertEqual(running["model_attestation"]["verdict"], "unknown")
        # A recorded failure on a non-running row is a requeued retry, not a
        # blocker: Hermes sets `ready`/`review` after a crash and clears the
        # error only on a later success.
        self.assertTrue(by_id["retry001"]["retry_pending"])
        self.assertFalse(by_id["ready001"]["retry_pending"])
        self.assertFalse(by_id["block001"]["retry_pending"])
        self.assertEqual(by_id["ready001"]["role"], "worker")
        self.assertEqual(by_id["ready001"]["parent_count"], 2)
        self.assertEqual(by_id["ready001"]["elapsed_seconds"], 0.0)
        # Finished rows freeze at completion; a settled one at its heartbeat.
        self.assertEqual(by_id["done0001"]["elapsed_seconds"], 500.0)
        self.assertTrue(by_id["done0001"]["completed"])
        self.assertEqual(by_id["block001"]["elapsed_seconds"], 100.0)
        self.assertEqual(by_id["stale001"]["elapsed_seconds"], 3000.0 - 16 * 60)
        # Running lanes lead the selection; the rest follow creation order.
        self.assertEqual([row["task_id"] for row in result["rows"]][:2], ["run00001", "stale001"])
        self.assertEqual(
            (result["running"], result["blocked"], result["completed"], result["queued"], result["active"]),
            (2, 1, 2, 3, 3),
        )
        self.assertEqual(result["dispatcher_presence"], "observed")
        self.assertEqual(result["scope"], "global")
        self.assertTrue(all(row["scope"] == "global" for row in result["rows"]))
        # Every key a native delegate row carries is present, so the widget's
        # row layout reads one shape for both lanes.
        native_keys = {
            "scope", "state", "task_id", "role", "action", "alias", "provider", "model", "effort",
            "tokens", "elapsed_seconds", "observed_at", "category", "completed", "delegation_id",
            "model_attestation",
        }
        self.assertTrue(native_keys <= set(running))

    def test_title_is_bounded_and_control_free_id_is_short(self) -> None:
        build_board(self.db, [task("t_abcdef12", "ready", title="x" * 200)])
        row = self.lanes()["rows"][0]
        self.assertEqual(len(row["action"]), reader._ACTION_LIMIT)
        self.assertEqual(row["task_id"], "abcdef12")

    def test_session_rows_are_owned_and_unmatched_identities_fall_back_to_global(self) -> None:
        build_board(self.db, [
            task("t_mine0001", "running", started_at=NOW - 10, session_id="sess-mine"),
            task("t_other001", "running", started_at=NOW - 10, session_id="sess-other"),
            task("t_cli00001", "ready"),
        ])
        owned = self.lanes(session_ids={"sess-mine", "sess-mine-continued"})
        self.assertEqual([row["task_id"] for row in owned["rows"]], ["mine0001"])
        self.assertEqual(owned["scope"], "session")
        self.assertTrue(all(row["scope"] == "session" for row in owned["rows"]))
        self.assertEqual((owned["running"], owned["queued"]), (1, 0))
        fallback = self.lanes(session_ids={"sess-unknown"})
        self.assertEqual(len(fallback["rows"]), 3)
        self.assertEqual(fallback["scope"], "global")
        self.assertTrue(all(row["scope"] == "global" for row in fallback["rows"]))
        unscoped = self.lanes()
        self.assertEqual((len(unscoped["rows"]), unscoped["scope"]), (3, "global"))

    def test_dispatcher_presence_rules(self) -> None:
        build_board(self.db, [task("t_ready001", "ready", created_at=NOW - 30)])
        self.assertEqual(self.lanes()["dispatcher_presence"], "unknown", "a fresh ready task proves nothing yet")
        for case, rows, expected in (
            ("ready past the wait window, nobody claiming", [task("t_ready001", "ready", created_at=NOW - 121)], "not_observed"),
            ("a claim lock on any row", [task("t_ready001", "ready", created_at=NOW - 121), task("t_claim001", "running", claim_lock="lock-1")], "observed"),
            ("a worker pid on any row", [task("t_ready001", "ready", created_at=NOW - 121), task("t_pid00001", "running", worker_pid=9)], "observed"),
            ("only todo rows waiting", [task("t_todo0001", "todo", created_at=NOW - 9000)], "unknown"),
        ):
            with self.subTest(case=case):
                self.db.unlink()
                build_board(self.db, rows)
                self.assertEqual(self.lanes()["dispatcher_presence"], expected)

    def test_row_budget_discloses_hidden_rows(self) -> None:
        build_board(self.db, [task(f"t_{index:08d}", "ready", created_at=NOW - 1000 + index) for index in range(10)])
        result = self.lanes(limit=ACTIVITY_ROW_LIMIT)
        self.assertEqual(len(result["rows"]), ACTIVITY_ROW_LIMIT)
        self.assertEqual(result["hidden"], 2)
        self.assertEqual(result["queued"], 10)
        # The oldest queued rows are the ones next in line, so they stay.
        self.assertEqual(result["rows"][0]["task_id"], "00000000")

    def test_named_board_reads_its_own_db(self) -> None:
        build_board(self.hermes / "kanban" / "boards" / "team-a" / "kanban.db", [task("t_team0001", "running", started_at=NOW - 3)])
        result = self.lanes(board="team-a")
        self.assertEqual([row["board"] for row in result["rows"]], ["team-a"])
        self.assertEqual(result["board"], "team-a")
        self.assertEqual(self.lanes()["rows"], [])

    def test_switched_board_is_the_one_projected(self) -> None:
        # `hermes kanban boards switch team-a` writes <root>/kanban/current;
        # the session's kanban_* tools then act on team-a, so the HUD must
        # project team-a, not the default board's rows.
        build_board(self.db, [task("t_dflt0001", "ready")])
        build_board(self.hermes / "kanban" / "boards" / "team-a" / "kanban.db", [task("t_team0001", "running", started_at=NOW - 3)])
        (self.hermes / "kanban" / "current").write_text("team-a\n", encoding="utf-8")
        self.assertEqual(reader.current_board(self.hermes), "team-a")
        self.assertEqual(reader.kanban_db_path(self.hermes), self.hermes / "kanban" / "boards" / "team-a" / "kanban.db")
        result = self.lanes()
        self.assertEqual([(row["task_id"], row["board"]) for row in result["rows"]], [("team0001", "team-a")])
        self.assertEqual(result["board"], "team-a")
        # An explicit board argument still wins over the switch.
        self.assertEqual([row["task_id"] for row in self.lanes(board="default")["rows"]], ["dflt0001"])

    def test_board_env_wins_over_the_current_file(self) -> None:
        build_board(self.hermes / "kanban" / "boards" / "team-a" / "kanban.db", [task("t_team0001", "ready")])
        build_board(self.hermes / "kanban" / "boards" / "team-b" / "kanban.db", [task("t_teamb001", "ready")])
        (self.hermes / "kanban" / "current").write_text("team-a\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_BOARD": "Team-B"}):
            self.assertEqual(reader.current_board(self.hermes), "team-b")
            self.assertEqual([row["task_id"] for row in self.lanes()["rows"]], ["teamb001"])
        # The env names a board that does not exist: fall through to the file.
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_BOARD": "team-c"}):
            self.assertEqual(reader.current_board(self.hermes), "team-a")

    def test_stale_or_malformed_current_file_falls_back_to_default(self) -> None:
        build_board(self.db, [task("t_dflt0001", "ready")])
        marker = self.hermes / "kanban" / "current"
        marker.parent.mkdir(parents=True)
        for content in (b"gone\n", b"../escape\n", b"", b"\xff", b"-lead"):
            with self.subTest(content=content):
                marker.write_bytes(content)
                self.assertEqual(reader.current_board(self.hermes), "default")
                self.assertEqual([row["task_id"] for row in self.lanes()["rows"]], ["dflt0001"])
        # A board directory that exists but holds no board file is not a board.
        (self.hermes / "kanban" / "boards" / "empty").mkdir(parents=True)
        marker.write_text("empty\n", encoding="utf-8")
        self.assertEqual(reader.current_board(self.hermes), "default")

    @unittest.skipIf(sys.platform == "win32", "symlink creation needs privileges on Windows")
    def test_symlinked_current_file_is_treated_as_absent(self) -> None:
        build_board(self.hermes / "kanban" / "boards" / "team-a" / "kanban.db", [task("t_team0001", "ready")])
        elsewhere = self.root / "elsewhere"
        elsewhere.write_text("team-a\n", encoding="utf-8")
        marker = self.hermes / "kanban" / "current"
        marker.symlink_to(elsewhere)
        self.assertEqual(reader.current_board(self.hermes), "default")


class KanbanHudMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hermes = self.root / ".hermes"
        self.hermes.mkdir()
        self.omh = self.root / ".omh"
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.root), "OMH_HOME": str(self.omh), "HERMES_HOME": str(self.hermes), "HERMES_KANBAN_HOME": "", "HERMES_KANBAN_BOARD": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.clock = mock.patch("omh.plugin_bundle.omh.kanban_board_reader.time.time", return_value=float(NOW))
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def hud(self, **kwargs):
        return read_omh_hud(self.omh, self.hermes, status={"runs": []}, **kwargs)

    def test_no_board_projects_an_empty_kanban_block(self) -> None:
        payload = self.hud()
        self.assertEqual(payload["kanban"], {
            "board": "default", "dispatcher_presence": "unknown", "rows_total": 0,
            "queued": 0, "running": 0, "blocked": 0, "done": 0,
        })
        self.assertFalse(payload["active"])

    def test_board_rows_merge_beside_native_rows_under_the_shared_cap(self) -> None:
        build_board(self.hermes / "kanban.db", [
            task(f"t_{index:08d}", "ready", created_at=NOW - 1000 + index) for index in range(9)
        ] + [task("t_running1", "running", started_at=NOW - 20, worker_pid=7, last_heartbeat_at=NOW - 1)])
        payload = self.hud()
        agents = payload["subagents"]
        self.assertTrue(payload["active"])
        self.assertEqual(len(agents["rows"]), ACTIVITY_ROW_LIMIT)
        self.assertEqual(agents["rows"][0]["task_id"], "running1")
        self.assertEqual(agents["hidden_rows"], 2)
        self.assertEqual((agents["active"], agents["running"], agents["blocked"], agents["completed"]), (1, 1, 0, 0))
        self.assertEqual(agents["scope"], "global")
        self.assertEqual(agents["status"], "observed")
        self.assertEqual(payload["kanban"], {
            "board": "default", "dispatcher_presence": "observed", "rows_total": 10,
            "queued": 9, "running": 1, "blocked": 0, "done": 0,
        })
        self.assertTrue(all(row["lane_backend"] == "kanban" for row in agents["rows"]))
        self.assertLessEqual(len(json.dumps(payload).encode("utf-8")) + 1, MAX_WIDGET_HUD_BYTES)

    @unittest.skipUnless(shutil.which("node"), "Node is required for the widget boundary")
    def test_widget_renders_the_kanban_identity_and_lane_markers(self) -> None:
        build_board(self.hermes / "kanban.db", [
            task("t_running1", "running", title="Ship the board lane", started_at=NOW - 280, worker_pid=7,
                 last_heartbeat_at=NOW - 1, model_override="gpt-5.6-sol", reasoning_effort="high"),
            task("t_queued01", "ready", title="Write the release notes", created_at=NOW - 30),
            task("t_stale001", "running", title="Stalled worker", started_at=NOW - 3000, worker_pid=8,
                 last_heartbeat_at=NOW - 20 * 60),
        ])
        payload = self.hud()
        widget = self.root / "widget.mjs"
        widget.write_bytes(widget_payload(Path(sys.executable)))
        script = """
import register from './widget.mjs';
const payload = JSON.parse(process.argv[1]);
const apps = [], lines = [], colors = [];
const h = (tag, props, ...children) => {
  if (typeof tag === 'function') { const text = tag(props); if (tag.name === 'ActivityRow') lines.push(text); return text; }
  if (props && props.color) colors.push([props.color, children.flat(Infinity).filter(x => x != null).join('')]);
  return children.flat(Infinity).filter(x => x != null).join('');
};
register({Box:'box', Text:'text', h, defineWidgetApp: app => {apps.push(app); return app}, openWidget:()=>{}, updateWidget:()=>{}});
const app = apps.find(x => x.id === 'omh-status');
const native = {scope:'global', state:'running', task_id:'a1b2c3d4', role:'hermes-native', action:'Review the router change',
  model:'claude-fable-5-1', effort:'xhigh', tokens:12345, elapsed_seconds:42, category:'architect'};
payload.subagents.rows.unshift(native); payload.subagents.active += 1; payload.subagents.running += 1;
const t = {color:{accent:'ACCENT', warn:'WARN', muted:'MUTED', ok:'OK', error:'ERROR', label:'LABEL', text:'TEXT', primary:'PRIMARY', border:'BORDER', statusFg:'STATUSFG'}};
const dock = app.render({cols:160, rows:30, state:{payload}, t});
const dockLines = [...lines];
const idle = app.render({cols:160, rows:30, state:{payload:{...payload, kanban:{...payload.kanban, rows_total:0, dispatcher_presence:'not_observed'}}}, t});
console.log(JSON.stringify({dock, idle, lines: dockLines, colors}));
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script, json.dumps(payload)], cwd=self.root, encoding="utf-8", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        views = json.loads(result.stdout)
        self.assertIn("3 agents · 3 running · board 1q 2r 0b", views["dock"])
        self.assertNotIn("dispatcher not observed", views["dock"])
        self.assertNotIn("board 1q", views["idle"])
        self.assertIn("dispatcher not observed", views["idle"])
        lines = views["lines"]
        self.assertEqual(len(lines), 4)
        self.assertIn("category:architect(claude-fable-5-1:xhigh)", lines[0])
        running = next(line for line in lines if "Ship the board lane" in line)
        self.assertIn("kanban/miku(gpt-5.6-sol:high)", running)
        self.assertIn("running · 4m 40s", running)
        stale = next(line for line in lines if "Stalled worker" in line)
        self.assertTrue(stale.startswith("! "), stale)
        self.assertIn("kanban/miku", stale)
        self.assertNotIn("kanban/miku(", stale, "no model pin, no parenthesis")
        self.assertIn("running ·", stale, "the tail keeps the native status word")
        queued = next(line for line in lines if "Write the release notes" in line)
        self.assertTrue(queued.startswith("· "), queued)
        self.assertIn("ready   ·", queued)
        def color_of(fragment: str) -> str:
            return next(color for color, text in views["colors"] if fragment in text)

        self.assertEqual(color_of("kanban/miku(gpt-5.6-sol:high)"), "ACCENT")
        self.assertEqual(color_of("kanban/miku"), "ACCENT")
        # The whole board row is accent: title and the dimmed scope/id and
        # elapsed pieces; a native row keeps text/muted.
        self.assertEqual(color_of("Ship the board lane"), "ACCENT")
        self.assertEqual(color_of("Review the router change"), "TEXT")
        self.assertEqual(color_of("· 4m 40s"), "ACCENT", "the board row's elapsed piece is accent (dimmed)")
        self.assertEqual(color_of("· 42s"), "MUTED", "a native row's elapsed piece stays muted")
        self.assertEqual(color_of("category:architect(claude-fable-5-1:xhigh)"), "LABEL")
        self.assertEqual(next(color for color, text in views["colors"] if text == "· "), "ACCENT")
        self.assertEqual(next(color for color, text in views["colors"] if text == "! "), "WARN")
        self.assertEqual(color_of("· dispatcher not observed"), "WARN")
        self.assertEqual(color_of("· board 1q 2r 0b"), "MUTED")


if __name__ == "__main__":
    unittest.main()

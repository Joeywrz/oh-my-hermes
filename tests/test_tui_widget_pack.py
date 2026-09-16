from __future__ import annotations

import json
from importlib import resources
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _cli_harness import run_cli
from omh.tui_widget_pack import TuiWidgetInstallError, install_tui_widget, widget_payload


class TuiWidgetPackTests(unittest.TestCase):
    def test_setup_installs_byte_correct_widget_without_overwriting_unrelated_widget(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            omh_home = root / ".omh"
            hermes_home = root / ".hermes"
            widget_dir = hermes_home / "tui-widgets"
            widget_dir.mkdir(parents=True)
            unrelated = widget_dir / "personal-dashboard.mjs"
            unrelated_bytes = b"export default function register() {}\n"
            unrelated.write_bytes(unrelated_bytes)

            status, stdout, stderr = run_cli(
                [
                    "--omh-home",
                    str(omh_home),
                    "--hermes-home",
                    str(hermes_home),
                    "setup",
                    "--json",
                ],
                output_json=False,
            )

            self.assertEqual(stderr, "")
            self.assertEqual(status, 0)
            payload = json.loads(stdout)
            expected = widget_payload(Path(sys.executable))
            self.assertEqual((widget_dir / "omh-status.mjs").read_bytes(), expected)
            self.assertEqual(unrelated.read_bytes(), unrelated_bytes)
            self.assertEqual(payload["steps"]["tui_widget"]["status"], "installed")
            config_text = (hermes_home / "config.yaml").read_text(encoding="utf-8")
            self.assertIn("  interface: tui\n", config_text)
            self.assertIn("  skin: omh\n", config_text)

    def test_setup_defaults_bare_launchers_to_the_branded_modern_tui(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            omh_home = root / ".omh"
            hermes_home = root / ".hermes"
            config = hermes_home / "config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text("display:\n  compact: true\n", encoding="utf-8")

            status, stdout, stderr = run_cli(
                [
                    "--omh-home",
                    str(omh_home),
                    "--hermes-home",
                    str(hermes_home),
                    "setup",
                    "--json",
                ],
                output_json=False,
            )

            self.assertEqual((status, stderr), (0, ""))
            config_text = config.read_text(encoding="utf-8")
            self.assertIn("  compact: true", config_text)
            self.assertIn("  interface: tui\n", config_text)
            self.assertIn("  skin: omh\n", config_text)
            tui_interface = json.loads(stdout)["steps"]["apply"]["tui_interface"]
            self.assertTrue(tui_interface["changed"])
            self.assertEqual(tui_interface["selected"], "tui")

    def test_setup_yes_switches_stock_classic_interface_and_skin(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            omh_home = root / ".omh"
            hermes_home = root / ".hermes"
            config = hermes_home / "config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "display:\n  interface: classic\n  skin: default\n",
                encoding="utf-8",
            )

            status, stdout, stderr = run_cli(
                [
                    "--omh-home",
                    str(omh_home),
                    "--hermes-home",
                    str(hermes_home),
                    "setup",
                    "--yes",
                    "--json",
                ],
                output_json=False,
            )

            self.assertEqual((status, stderr), (0, ""))
            config_text = config.read_text(encoding="utf-8")
            self.assertIn("  interface: tui\n", config_text)
            self.assertIn("  skin: omh\n", config_text)
            self.assertEqual(config_text.count("interface:"), 1)
            self.assertEqual(config_text.count("skin:"), 1)
            apply = json.loads(stdout)["steps"]["apply"]
            self.assertEqual(apply["tui_interface"]["selected"], "tui")
            self.assertEqual(apply["skin"]["selected"], "omh")

    def test_update_yes_restores_widget_and_switches_stock_display_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            omh_home = root / ".omh"
            hermes_home = root / ".hermes"
            common = [
                "--omh-home",
                str(omh_home),
                "--hermes-home",
                str(hermes_home),
            ]
            setup_status, _, setup_stderr = run_cli([*common, "setup", "--json"], output_json=False)
            self.assertEqual((setup_status, setup_stderr), (0, ""))
            widget = hermes_home / "tui-widgets" / "omh-status.mjs"
            widget.unlink()
            config = hermes_home / "config.yaml"
            config.write_text(
                config.read_text(encoding="utf-8")
                .replace("  interface: tui\n", "  interface: cli\n")
                .replace("  skin: omh\n", "  skin: default\n"),
                encoding="utf-8",
            )

            status, _, stderr = run_cli(
                [
                    *common,
                    "update",
                    "--yes",
                    "--json",
                ],
                output_json=False,
            )

            self.assertEqual((status, stderr), (0, ""))
            expected = widget_payload(Path(sys.executable))
            self.assertEqual(widget.read_bytes(), expected)
            config_text = config.read_text(encoding="utf-8")
            self.assertIn("  interface: tui\n", config_text)
            self.assertIn("  skin: omh\n", config_text)

    def test_setup_reports_config_changed_when_only_plugin_enablement_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            omh_home = root / ".omh"
            hermes_home = root / ".hermes"
            common = ["--omh-home", str(omh_home), "--hermes-home", str(hermes_home)]
            setup_status, _, setup_stderr = run_cli([*common, "setup", "--json"], output_json=False)
            self.assertEqual((setup_status, setup_stderr), (0, ""))
            config = hermes_home / "config.yaml"
            config.write_text(
                config.read_text(encoding="utf-8").replace("plugins:\n  enabled:\n    - omh\n", ""),
                encoding="utf-8",
            )

            status, stdout, stderr = run_cli([*common, "setup", "--json"], output_json=False)

            self.assertEqual((status, stderr), (0, ""))
            apply = json.loads(stdout)["steps"]["apply"]
            self.assertTrue(apply["plugin_enabled"]["changed"])
            self.assertTrue(apply["changed"])

    def test_installer_rejects_symlinked_widget_destination_and_parent(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            widget_dir = hermes_home / "tui-widgets"
            widget_dir.mkdir(parents=True)
            victim = root / "victim.mjs"
            victim_bytes = b"do not overwrite\n"
            victim.write_bytes(victim_bytes)
            destination = widget_dir / "omh-status.mjs"
            destination.symlink_to(victim)

            with self.assertRaises(TuiWidgetInstallError):
                install_tui_widget(hermes_home)
            self.assertEqual(victim.read_bytes(), victim_bytes)

            destination.unlink()
            widget_dir.rmdir()
            external_dir = root / "external-widgets"
            external_dir.mkdir()
            widget_dir.symlink_to(external_dir, target_is_directory=True)
            with self.assertRaises(TuiWidgetInstallError):
                install_tui_widget(hermes_home)
            self.assertEqual(list(external_dir.iterdir()), [])

    def test_installer_refuses_unmanaged_existing_widget(self) -> None:
        with TemporaryDirectory() as tmp:
            hermes_home = Path(tmp) / ".hermes"
            destination = hermes_home / "tui-widgets" / "omh-status.mjs"
            destination.parent.mkdir(parents=True)
            user_bytes = b"export default function userOwned() {}\n"
            destination.write_bytes(user_bytes)

            with self.assertRaises(TuiWidgetInstallError):
                install_tui_widget(hermes_home)
            self.assertEqual(destination.read_bytes(), user_bytes)

    def test_widget_uses_setup_interpreter_not_path_python(self) -> None:
        payload = widget_payload(Path(sys.executable)).decode()

        self.assertIn(json.dumps(os.path.realpath(sys.executable)), payload)
        self.assertNotIn("spawnSync('python3'", payload)
        # `-B` is load-bearing and no environment variable can stand in for it:
        # `-I` implies `-E`, so the child ignores every PYTHON* variable and
        # would write bytecode into the managed plugins directory (issue #1550).
        self.assertIn("['-I', '-B', '-c', READER]", payload)
        self.assertIn("const READER_ENV =", payload)
        self.assertNotIn("...process.env", payload)

    def test_full_uninstall_removes_only_managed_widget(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            omh_home = root / ".omh"
            hermes_home = root / ".hermes"
            common = ["--omh-home", str(omh_home), "--hermes-home", str(hermes_home)]
            status, _, stderr = run_cli([*common, "setup", "--json"], output_json=False)
            self.assertEqual((status, stderr), (0, ""))
            destination = hermes_home / "tui-widgets" / "omh-status.mjs"
            unrelated = destination.parent / "personal.mjs"
            unrelated.write_text("personal\n", encoding="utf-8")

            status, stdout, stderr = run_cli(
                [*common, "uninstall", "--all", "--keep-command", "--json"],
                output_json=False,
            )

            self.assertEqual((status, stderr), (0, ""))
            self.assertFalse(destination.exists())
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "personal\n")
            self.assertEqual(json.loads(stdout)["tui_widget"]["status"], "removed")

    def test_widget_names_its_own_session_on_every_poll(self) -> None:
        # Two live TUIs used to share one panel answer because the poll
        # carried no identity. The host writes this TUI's session id to the
        # file named by HERMES_TUI_ACTIVE_SESSION_FILE; the widget reads it
        # per poll (the session moves on /new and /resume) and hands it to
        # the reader as the todo scope, without widening the env allowlist.
        widget = resources.files("omh.tui_widgets").joinpath("omh-status.mjs").read_text(encoding="utf-8")

        self.assertIn("process.env.HERMES_TUI_ACTIVE_SESSION_FILE", widget)
        self.assertIn("const sessionRef = activeSessionRef()", widget)
        self.assertIn("{ ...READER_ENV, OMH_HUD_TUI_SESSION_REF: sessionRef }", widget)
        self.assertIn("tui_session_ref=os.environ.get('OMH_HUD_TUI_SESSION_REF', '')", widget)
        # A malformed value is dropped, never mutated into a different key.
        self.assertIn("SESSION_REF_SHAPE.test(sessionId) ? sessionId : ''", widget)
        self.assertNotIn("...process.env", widget)

    def test_widget_frames_the_composer_and_docks_the_plan_on_top(self) -> None:
        # Changed on purpose (this used to pin a single dock-bottom app and
        # forbid dock-top). The single bottom dock framed the OMH section
        # instead of the chat input ('채팅창에 선 두개가 있어야지 왜 tui에
        # 있어') and sank the plan the owner always read above the input
        # ('투두가 왜 하단에 떠 기존에는 상단에 잘 떴었는데'). The layout is
        # now: plan todo in dock-top, closed by the rule directly above the
        # input; the bottom dock opens with the rule below the input and
        # carries status and activity rows with no closing rule.
        widget = resources.files("omh.tui_widgets").joinpath("omh-status.mjs").read_text(encoding="utf-8")

        # Three apps: the two ambient docks framing the composer, plus the
        # modal chain picker (`/omh-model`), which renders no dock at all.
        self.assertEqual(widget.count("defineWidgetApp({"), 3)
        self.assertEqual(widget.count("zone: 'dock-bottom'"), 1)
        self.assertEqual(widget.count("zone: 'dock-top'"), 1)
        self.assertIn("id: 'omh-todo'", widget)
        self.assertIn("id: 'omh-status'", widget)
        # The todo panel renders only in the top dock, and every branch of it
        # (no plan, all done, established) closes with the plain frame rule
        # so the composer frame never blinks with the plan lifecycle.
        self.assertEqual(widget.count("h(TodoPanel"), 1)
        self.assertNotIn("FrameRule", widget)
        # Both apps gate on the same payload validity, so neither half of the
        # frame renders on a host where the plugin does not answer.
        self.assertEqual(
            widget.count(
                "if (!state.payload || state.payload.error || state.payload.privacy !== 'metadata_only') return null"
            ),
            2,
        )
        # One snapshot pass feeds both docks; a second poller would let the
        # two frame rules disagree about payload freshness.
        self.assertEqual(widget.count("updateWidget(todoApp, apply)"), 1)
        self.assertEqual(widget.count("openWidget(todoApp, todoApp.init(''))"), 1)

    def test_widget_renders_machine_graph_fields_without_input_capture(self) -> None:
        widget = resources.files("omh.tui_widgets").joinpath("omh-status.mjs").read_text(encoding="utf-8")

        self.assertIn("const graph = payload.graph", widget)
        self.assertIn("graph.status === 'active'", widget)
        self.assertIn("graph.nodes", widget)
        self.assertIn("graph.edges", widget)
        self.assertIn("graph.edge_count", widget)
        self.assertIn("graph.frontier", widget)
        self.assertIn("graph.hidden_nodes", widget)
        self.assertIn("node.blocked_by", widget)
        self.assertIn("node.in_frontier", widget)
        self.assertIn("Math.max(0, viewportRows - 8)", widget)
        self.assertIn("OMH_SUBAGENT_GRAPH", widget)
        self.assertIn("graph_preference=os.environ.get('OMH_SUBAGENT_GRAPH', 'auto')", widget)
        self.assertIn("const graphLine =", widget)
        self.assertIn("const truncateTextCells =", widget)
        self.assertIn("const sanitizeText =", widget)
        self.assertIn("sanitizeText(value).slice(0, 4096)", widget)
        # The route identity is shaped by parentheses -- `category:architect(
        # anthropic/claude...)`, `(codex/maestro ...)`, `turn 3 (12 tools)` --
        # so the allowlist keeps them; stripping them rendered the category
        # and model as one run-on token (`architectanthropic/c...`).
        self.assertIn("/[^\\p{L}\\p{N} .:/_·|+()\\[\\]!\\-]/gu", widget)
        self.assertNotIn("safeText(value, 4096)", widget)
        self.assertIn("'blocked_by_dependency'", widget)
        self.assertIn("'dry_run_planned'", widget)
        self.assertNotIn("useInput", widget)
        self.assertNotIn("useKeypress", widget)

    def test_fanout_dispatch_rows_render_a_warn_colored_maestro_identity(self) -> None:
        # `omh coding fanout dispatch` spawns a local CLI directly (the
        # Maestro lane by definition), and the reader tags that row
        # `dispatch_lane` so it renders in the same agent list as Hermes-
        # native delegate_task rows -- same truncation, dots, and state
        # colors -- but with `(<executor>/maestro <model>)` in place of the
        # category:model route, warn-colored to stand apart from the default
        # Hermes-native lane.
        widget = resources.files("omh.tui_widgets").joinpath("omh-status.mjs").read_text(encoding="utf-8")

        self.assertIn(
            "const MAESTRO_EXECUTOR_SHORT_NAMES = { codex: 'codex', claude_code: 'claude', omo_runtime: 'omo', hermes_local: 'hermes' }",
            widget,
        )
        self.assertIn("const dispatchLane = safeText(row.dispatch_lane)", widget)
        # The identity segment is now the row's own column rather than the
        # first droppable metadata entry, so it is bound once and rendered
        # between the title and the measured tail.
        self.assertIn(
            "return dispatchLane ? metricSegment('maestro', dispatchIdentity) : metricSegment(routeKind, route)",
            widget,
        )
        self.assertIn("layout.routeKind === 'route-fallback' || layout.routeKind === 'maestro'", widget)
        self.assertIn("|| segment.kind === 'maestro'", widget)

    def test_kanban_lane_rows_render_an_accent_colored_board_identity(self) -> None:
        # A Hermes Kanban task (reader: kanban_board_reader) is board work a
        # dispatcher runs as a detached worker, not a delegate_task child.
        # Its identity names the assignee profile and the per-task model
        # override, `(kanban miku gpt-5.6-sol)`, in the theme's accent tone --
        # the one token the widget had no other use for -- so the board lane
        # is told apart from the native (label) and Maestro (warn) lanes by
        # colour alone, still never a literal.
        widget = resources.files("omh.tui_widgets").joinpath("omh-status.mjs").read_text(encoding="utf-8")

        self.assertIn("if (safeText(row.lane_backend) === 'kanban') {", widget)
        self.assertIn("return metricSegment('kanban', `(kanban ${assignee}${modelName ? ` ${modelName}` : ''})`)", widget)
        self.assertIn("layout.routeKind === 'kanban'", widget)
        self.assertEqual(widget.count("t.color.accent"), 1)
        # Board verdicts the reader projects: a queued task has no worker and
        # must not spin (a muted static dot), a stale one has a worker that
        # stopped heartbeating (a warn bang). The tail keeps the board's own
        # status word (`ready`, `review`, `archived`) rather than the verdict.
        self.assertIn("const queued = row.state === 'queued'", widget)
        self.assertIn("const stale = row.state === 'stale'", widget)
        self.assertIn("queued ? '·' : stale ? '!'", widget)
        self.assertIn("queued ? t.color.muted : stale ? t.color.warn : t.color.ok", widget)
        self.assertIn("? safeText(row.native_status) || safeText(row.state) || 'running'", widget)
        self.assertIn("scheduled: 'sched', archived: 'arch', review: 'rev'", widget)
        # The header keeps its count line and appends the board tally only
        # while the board has rows, plus the reader's dispatcher verdict.
        self.assertIn("const board = payload.kanban || {}", widget)
        self.assertIn("` · board ${Number(board.queued) || 0}q ${Number(board.running) || 0}r ${Number(board.blocked) || 0}b`", widget)
        self.assertIn("board.dispatcher_presence === 'not_observed'", widget)
        self.assertIn("' · dispatcher not observed'", widget)

    def test_hud_liveness_signal_drives_the_status_line_todo_and_shot_badge(self) -> None:
        # 2026-08 HUD liveness fix: exact in-flight tool-call state (paired
        # from pre_tool_call/post_tool_call by tool_call_id) replaces three
        # things that used to lie -- a lingering green active todo item, a
        # parallel-shot badge stuck at the ring ceiling, and no signal at all
        # while calls were genuinely running.
        widget = resources.files("omh.tui_widgets").joinpath("omh-status.mjs").read_text(encoding="utf-8")

        # Status line: a live segment renders only while open calls exist AND
        # this install has actually observed post_tool_call fire -- an
        # unsupported host's ledger can only expire entries, never
        # legitimately close them, so a bare `live` reading there is not
        # trustworthy (P2-1). No ambiguous-width glyph prefixes it (P3-4).
        self.assertIn(
            "payload.activity && payload.activity.live && payload.activity.post_tool_call_observed",
            widget,
        )
        self.assertIn("`${plural(Number(payload.activity.open_call_count) || 0, 'tool')}", widget)
        self.assertNotIn("⚙", widget)

        # Todo panel: the active marker is warn-colored and carries a stall
        # hint when the HUD says not-live, instead of always reading green --
        # but only once liveness is answerable; an unsupported host falls
        # back to always-live (P2-1), and the stall age itself comes from
        # the reader, not a Date.now() computed in render (P1-1).
        self.assertIn("const answerable = !!(payload.activity && payload.activity.post_tool_call_observed)", widget)
        self.assertIn(
            "const live = answerable ? !!(payload.activity && payload.activity.live) : true",
            widget,
        )
        self.assertIn("color: item.state === 'active' ? (live ? t.color.ok : t.color.warn)", widget)
        self.assertIn(
            "unchangedElapsed ? h(Text, { color: t.color.muted }, ` (unchanged ${unchangedElapsed})`)",
            widget,
        )
        self.assertIn("const seconds = todo.updated_age_seconds", widget)
        self.assertNotIn("Date.parse(safeText(todo.updated_at)", widget)

        # Shot badge: tied to open calls while live, dimmed history with age
        # once every member has closed -- never the saturated ring size, and
        # never the burst's raw dispatch size either (P1-2): the dimmed form
        # reads the group's measured peak concurrency.
        self.assertIn("const openCount = Number(shot.open_count) || 0", widget)
        self.assertIn("if (openCount > 0) {", widget)
        self.assertIn("(${age} ago)", widget)
        self.assertIn("parallel shot ×${Number(shot.peak_open_count) || 0}", widget)
        self.assertNotIn("parallel shot ×${Number(shot.size)", widget)

        # The exact in-flight age and the reader-computed todo stall age are
        # the only fields allowed to drift every poll without forcing a
        # repaint; the liveness transition itself (open_call_count, live)
        # stays out of VOLATILE_KEYS on purpose.
        self.assertIn("'oldest_open_elapsed_seconds',", widget)
        self.assertIn("'updated_age_seconds',", widget)
        self.assertNotIn("'open_call_count',", widget)
        self.assertNotIn("'live',", widget)

    def test_a_stuck_unit_never_renders_as_the_dispatch_state(self) -> None:
        # "Process alive" and "work progressing" are different states and must
        # never render the same. A graph node whose reader assessed its own
        # output and found it stuck replaces the dispatch word outright and
        # carries the reason and the stall age beside it.
        widget = resources.files("omh.tui_widgets").joinpath("omh-status.mjs").read_text(encoding="utf-8")

        self.assertIn("const stuckState = safeText(node.unit_state)", widget)
        self.assertIn("const state = stuckState || dispatchState", widget)
        self.assertIn("const stallSeconds = Number(node.stalled_for_seconds) || 0", widget)
        self.assertIn("safeText(node.state_reason)", widget)
        self.assertIn("since new output", widget)
        # Its own marker and the warn tone, so a stuck node is visible before
        # the line is read at all.
        self.assertIn("const marker = stuckState\n          ? '[~]'", widget)
        self.assertIn("color: stuckState\n              ? t.color.warn", widget)
        self.assertIn("${state}${stuckSuffix}${suffix}", widget)

    def test_widget_registers_the_chain_picker_as_a_modal_app_behind_a_guard(self) -> None:
        widget = resources.files("omh.tui_widgets").joinpath("omh-status.mjs").read_text(encoding="utf-8")

        # The host's own /model picks the session model and cannot be
        # shadowed; OMH's per-category picker lives under its own id, as a
        # modal (it owns every keypress while open) and only when the host
        # exposes the overlay primitives -- an older host keeps the docks.
        self.assertIn("id: 'omh-model'", widget)
        self.assertNotIn("id: 'model'", widget)
        self.assertEqual(widget.count("mode: 'modal'"), 1)
        self.assertIn("if (Overlay && Dialog) {", widget)
        self.assertIn("const { Box, Dialog, Overlay, Text, defineWidgetApp, h, openWidget, updateWidget } = sdk", widget)
        # Reading and saving go through the installed bundle's picker model,
        # with the same isolated interpreter spawn the HUD reader uses.
        self.assertIn("from omh.model_chain_picker import picker_rows", widget)
        self.assertIn("from omh.model_chain_picker import apply_picker_changes", widget)
        self.assertIn("['-I', '-B', '-c', script]", widget)
        self.assertIn("Mirror of model_chain_picker.step_head_model", widget)
        self.assertIn("Mirror of model_chain_picker.step_effort", widget)
        self.assertNotIn("useInput", widget)

        widget = resources.files("omh.tui_widgets").joinpath("omh-status.mjs").read_text(encoding="utf-8")

        self.assertIn("zone: 'dock-bottom'", widget)
        self.assertNotIn("zone: 'top-right'", widget)
        # Plan todo above the input, status and activity rows below it — the
        # owner's placement, restored after the single-bottom-dock interim.
        self.assertEqual(widget.count("zone: 'dock-top'"), 1)
        self.assertIn("id: 'omh-todo'", widget)
        self.assertIn("TodoPanel", widget)
        self.assertIn("truncateCells(item.text", widget)
        self.assertIn("safeText(todo.title)", widget)
        # An installed OMH stays discoverable from an idle session: only the
        # activity rows are gated on live work, never the header.
        self.assertNotIn("|| !payload.active", widget)
        self.assertIn("const active = !!payload.active", widget)
        self.assertIn("width: '100%'", widget)
        # The Rule frame replaced the marginTop spacer: the docks carry the
        # classic composer frame, rules sitting tight against the input --
        # padding was tried at one and two rows and the owner picked none.
        # Exactly four plain-rule renders: the dock-bottom opener plus the
        # three todo-panel closers (no plan, all done, established). The
        # badge that briefly dressed the top rule moved to the [Plan] header,
        # so every rule is plain, byte-stable chrome again.
        self.assertIn("const Rule = ", widget)
        self.assertNotIn("Gap", widget)
        self.assertEqual(widget.count("h(Rule, { columns, t })"), 4)
        # Text, not chrome — changed on purpose a second time, by owner
        # direction after living with the bordered card: the OMH surface reads
        # like the host's own status line, dense text in the TUI's idiom. The
        # border that briefly asserted the panel identity now marks the
        # RETIRED design, and colours still resolve only through the active
        # theme — a literal hex would freeze the surface on one palette while
        # the rest of the TUI followed the user's skin.
        self.assertNotIn("borderStyle:", widget)
        self.assertNotIn("panelProps", widget)
        self.assertNotIn("color: '#", widget)
        # The bracket tags are the shared grammar between the two docks.
        self.assertIn("'⚚ [OMH]'", widget)
        self.assertEqual(widget.count("'[Plan]'"), 2)
        self.assertIn("const SEPARATOR = ' │ '", widget)
        self.assertNotIn("metricRow", widget)
        self.assertIn("...rows.map", widget)
        self.assertNotIn("...maestroRows.map", widget)
        self.assertNotIn("latest ? h(Text", widget)
        self.assertIn("const version = safeText(payload.version)", widget)
        # Header composition, changed on purpose (this used to assert the
        # literal "`[OMH] ${version}`"). That header named the product twice
        # and then claimed "Ultra Work Ready" whether or not anything was
        # running, so it read identically at four active agents and at zero.
        # What matters now is the contract, not the wording: the version is
        # still shown, every colour still resolves through the active theme,
        # and the state segment is derived rather than fixed.
        self.assertIn("` v${version}`", widget)
        self.assertIn("hudStateLabel(active, agents)", widget)
        self.assertIn("if (!active) return 'ready'", widget)
        # Hermes-native delegation rows linger after finishing: a done row
        # carries a check mark instead of spinning forever, and a linger-only
        # block says "N done" rather than the dishonest "0 agents".
        self.assertIn("done ? '✓'", widget)
        self.assertIn("if (!running && !blocked && done) return `${done} done`", widget)
        # A phase-structured plan (todo init) shows the current phase's name
        # above its checklist and the phase count next to done/total.
        self.assertIn("safeText(todo.display_phase)", widget)
        self.assertIn("` · ${phaseCount} phases`", widget)
        # The todo panel renders the plan from todo.items: every phase is a
        # header row with its tasks indented one level beneath it — even a
        # single-task phase; the old one-line merge collapsed the structure
        # the owner reads ('[] 이거 탭한번쳐서 한개여도. 그 구조로 나오게').
        # Subtasks (depth 1..3) indent further, and past seven visible items
        # the window anchors at current work with muted
        # "... (N earlier/later tasks)" fold lines.
        self.assertIn("Array.isArray(todo.items)", widget)
        self.assertIn("last.phase === phase", widget)
        self.assertNotIn("isMerged", widget)
        self.assertNotIn("phaseColumn", widget)
        # Eight body rows matches the senpi/OMO todo widget's visible budget.
        self.assertIn("const TODO_DISPLAY_ROWS = 8", widget)
        self.assertIn("depthOf", widget)
        self.assertIn("(!phase && depthOf(item) > 0)", widget)
        self.assertIn("'  '.repeat(depthOf(item) + (group.phase ? 1 : 0))", widget)
        self.assertIn("task${count === 1 ? '' : 's'}", widget)
        self.assertIn("'todo-earlier'", widget)
        self.assertIn("'todo-later'", widget)
        self.assertNotIn("todo.display_items", widget)
        self.assertNotIn("more_count", widget)
        self.assertNotIn("more}", widget)
        # Drag-copy contract for the QUIET dock: an unchanged snapshot must
        # not repaint (repaints clear an in-progress terminal selection), and
        # metric-only drift repaints at most once per throttle window. While
        # a row is RUNNING the dock trades selection stability for liveness —
        # LiveActivityRows mounts on the shimmer clock so the spinner turns
        # and elapsed ticks (snapshot value + seconds since it arrived);
        # idle and linger-only docks render the static branch.
        self.assertIn("if (serialized === lastSnapshot) return", widget)
        self.assertIn("LiveActivityRows", widget)
        self.assertIn("row.state === 'running'", widget)
        self.assertIn("(Date.now() - receivedAt) / 1000", widget)
        self.assertIn("receivedAt: Date.now()", widget)
        self.assertIn("h(ActivityRows, { columns, extraSeconds: 0, frame: 0, mainRows", widget)
        self.assertIn("const METRICS_REPAINT_MS = 30_000", widget)
        self.assertIn(
            "if (structural === lastStructural && Date.now() - lastPaintAt < METRICS_REPAINT_MS) return",
            widget,
        )
        for volatile in (
            "'cache_hit_percentage'",
            "'context_percentage'",
            "'cost_usd'",
            "'elapsed_seconds'",
            "'observed_at'",
            "'tokens'",
            "'tokens_per_second'",
            "'tool_count'",
            "'turn_count'",
        ):
            self.assertIn(volatile, widget)
        # (bracket-tag grammar asserted above replaces the BRAND_MARK pair)
        # The old header's literal pieces ("-", "Oh My Hermes", "Ultra Work",
        # "Ready") are gone on purpose; asserting them back would re-pin the
        # wording this change exists to replace. The separator is now shared
        # between both panels instead of hand-written per segment.
        self.assertNotIn("'Ultra Work'", widget)
        # Running rows are alive again by owner direction (the static orange
        # marker with a frozen counter read as broken): spinner on the
        # shimmer clock, real-time elapsed, and cost segments render only
        # when a nonzero cost was actually observed — a subscription-billed
        # host records none, and a permanent $0.0000 read as a bug.
        self.assertIn("SPINNER_FRAMES", widget)
        self.assertIn("SPINNER_FRAMES[frame % SPINNER_FRAMES.length]", widget)
        self.assertNotIn("elapsedCoarse", widget)
        self.assertIn("row.cost_usd > 0", widget)
        # Token-derived approximations (subscription-billed hosts record no
        # per-call cost) render with a `~`; a zero renders only with the
        # recorded provenance that earns it (status first, else source —
        # whatever word the host wrote, never a vocabulary this surface
        # enumerates), and never with a `~`, because an exact zero is not an
        # approximation. Row and header apply the same rule.
        self.assertIn("row.cost_approximate ? '~' : ''", widget)
        self.assertIn("function costSegmentText(row)", widget)
        self.assertIn("$${row.cost_usd.toFixed(4)} (${provenance})", widget)
        self.assertIn("safeText(row.cost_status) || safeText(row.cost_source)", widget)
        self.assertIn("const zeroCostProvenance", widget)
        self.assertIn("$${cost.toFixed(3)} (${zeroCostProvenance})", widget)
        self.assertIn("`${approximate ? '~' : ''}$${cost.toFixed(3)}`", widget)
        # Claude Code's token-counter idiom (184.8k, 2.1m): observed subagent
        # token counts render per row and summed on the header, one decimal
        # with trailing .0 trimmed. The row segment sits BEFORE cost so the
        # narrow-terminal drop order sheds the dollar figure first and keeps
        # the token count; an unreadable value renders nothing.
        self.assertIn("const tokenCountText", widget)
        # One decimal is always kept above a thousand (`77.0k`, not `77k`):
        # trimming it made a round count change width mid-wave and broke the
        # column's decimal alignment.
        self.assertIn("`${amount.toFixed(1)}${unit}`", widget)
        self.assertNotIn(".replace(/\\.0$/, '')", widget)
        # The unit break sits where one-decimal rounding lands (999,950 reads
        # 1m, never 1000k) and sub-thousand counts render bare. A recorded
        # zero now renders `0`: the reader sends a number only once the row is
        # terminal or has reported, so zero means the run consumed nothing --
        # the shape of a dispatch that died before its first API call, which
        # the old blank made indistinguishable from an unmeasured row.
        self.assertIn("value < 999_950 ?", widget)
        self.assertIn("if (value < 1000) return", widget)
        self.assertIn("|| value < 0) return ''", widget)
        # Claude Code-style grid ('절대위치로 … 클로드코드처럼 정렬'): fixed
        # columns first, variable metadata after. The owner moved the measured
        # block beside the identity it belongs to ('category 바로옆에 두고
        # 그뒤에 캐시히트나 턴'), so the order is title, route/category, then
        # `state · elapsed · N tokens` -- each piece padded to a constant cell
        # width so the token figures still line up vertically down the list --
        # and only then rate, cache, turn and cost, which the drop loop still
        # sheds by rank without ever touching the tail.
        self.assertIn("` · ${tokenText.padStart(6)} tokens`", widget)
        # The route column is never truncated: category, model and effort are
        # the column's whole purpose ('category, modelname, effort만 잘
        # 나오면 되니까'). It takes the widest identity in the list; when the
        # full `category:name(model:effort)` shape does not fit beside the
        # prefix, the tail and an 8-cell title, the `category:` literal is
        # shed for the whole list and the action title shrinks (to 4 cells at
        # the floor). The old percentage caps cut the model mid-name.
        self.assertIn("const ROUTE_PREFIX = 'category:'", widget)
        self.assertIn("const routeCompact = text => text.startsWith(ROUTE_PREFIX) ? text.slice(ROUTE_PREFIX.length) : text", widget)
        self.assertIn("return { compact: Math.max(width.compact, cellWidth(routeCompact(text))), full: Math.max(width.full, cellWidth(text)) }", widget)
        self.assertIn("const routeShedPrefix = routeColumn.full > routeRoom", widget)
        self.assertIn("const routeCap = routeShedPrefix ? routeColumn.compact : routeColumn.full", widget)
        self.assertIn("? `${separator}${padCells(routeText, routeCap)}`", widget)
        self.assertNotIn("truncateCells(routeText", widget)
        self.assertNotIn("truncateCells(routeSegment.text", widget)
        self.assertIn("const actionWidth = Math.max(4, Math.min(actionCap, budget - cellWidth(prefix) - routeWidth - tailWidth - 2))", widget)
        self.assertNotIn("Math.floor(columns * 0.3)", widget)
        self.assertNotIn("Math.floor(columns * 0.24)", widget)
        # The label names the model, never its provider: `anthropic/` ate the
        # column and truncated to `category:architect(anthropic/`, hiding
        # whether the lane ran opus or fable. Only the segment after the last
        # slash reaches the label (both the category route and the maestro
        # dispatch identity); `row.provider` stays a separate reader field.
        self.assertIn("const modelName = safeText(row.model).split('/').pop()", widget)
        self.assertIn("const model = [modelName, safeText(row.effort)].filter(Boolean).join(':')", widget)
        self.assertIn("${modelName ? ` ${modelName}` : ''}", widget)
        self.assertNotIn("truncateCells(modelName, 20)", widget)
        self.assertNotIn("truncateCells(row.model, 20)", widget)
        # The route column reserves its width per LIST, exactly like tokens:
        # otherwise a row without a category would slide its tail left and
        # break the very alignment this ordering exists to keep.
        self.assertIn(
            "const routeColumn = routeColumnWidth([...mainRows, ...rows])",
            widget,
        )
        self.assertIn("h(Text, { color: statusColor }, layout.tailState)", widget)
        # The tail is one fixed-width block but two colours: elapsed keeps the
        # muted tint every metric shares, and the token count -- the row's one
        # plain quantity -- reads in `statusFg`, the tone the host status line
        # spends on its own token gauge ('tokens는 약간 회색'). A theme token
        # either way; no literal colour enters this widget.
        self.assertIn("h(Text, { color: t.color.muted }, layout.tailRest)", widget)
        self.assertIn("h(Text, { color: t.color.statusFg }, layout.tailTokens)", widget)
        self.assertIn("const tailTokens = tokensColumn ? tokensPiece : ''", widget)
        # Rate reads beside the token count it is derived from ('tokens,
        # tok/s, cache, turns 순으로'), so the metric run is ordered rate,
        # cache, ctx, turn, cost on screen.
        self.assertLess(widget.index("metricSegment('rate'"), widget.index("metricSegment('cache'"))
        self.assertLess(widget.index("metricSegment('cache'"), widget.index("metricSegment('turn'"))
        self.assertLess(widget.index("metricSegment('turn'"), widget.index("metricSegment('cost'"))
        # Screen position stopped being shed priority when rate moved left, so
        # each segment carries a `drop` rank and the loop sheds the highest --
        # rate first, then cost, then turn -- exactly the order the tail's
        # comment has always promised. Popping the last segment would now shed
        # cost before rate and drop the cheapest figure last.
        self.assertIn("const metricSegment = (kind, text, drop = 0) => ({ drop, kind, text })", widget)
        self.assertIn("if (segments[index].drop > segments[shed].drop) shed = index", widget)
        self.assertIn("segments.splice(shed, 1)", widget)
        self.assertNotIn("segments.pop()", widget)
        # Cache hit rate renders in the palette's amber ('cache는 노란색'),
        # which it shares with the route/dispatch warnings; turn and rate stay
        # muted, so the run reads grey count, muted rate, amber cache, muted
        # turn.
        self.assertIn("|| segment.kind === 'cache'\n                ? t.color.warn", widget)
        # The tokens column exists per LIST: a row with no observed count
        # holds the grid with blank cells, and a wave with no counts at all
        # drops the column instead of wasting the width.
        self.assertIn("' '.repeat(tokensWidth)", widget)
        self.assertIn(
            "const tokensColumn = [...mainRows, ...rows].some(row => tokenCountText(row.tokens))",
            widget,
        )
        # The header renders a summed ZERO whenever any row carried a figure:
        # a dispatch that died before its first API call really did consume
        # nothing, and blanking that made a failed wave read exactly like an
        # unmeasured one. Only a wave where no row reports at all still hides
        # the segment.
        self.assertIn(
            "tokens: rows.some(row => Number.isFinite(row.tokens))",
            widget,
        )
        self.assertIn("if (!Number.isFinite(value) || value < 0) return ''", widget)
        # The summed count anchors the header's right edge too, and the
        # header has no drop loop, so the segment hides below 100 columns
        # instead of pushing ctx and the yolo readout past truncate-end.
        self.assertIn("columns >= 100 && metrics.tokens", widget)
        self.assertIn("` • ${metrics.tokens}`", widget)
        self.assertLess(widget.index("' • yolo mode: '"), widget.index("` • ${metrics.tokens}`"))
        # Delegate goals (row titles) are a FIXED padded column capped at
        # ~40% of the terminal, 48 cells at most, and always shrink before
        # the tail: the metadata column starts aligned and the tail block
        # keeps its right anchor even on narrow terminals.
        self.assertIn("Math.min(48, Math.floor(columns * 0.4))", widget)
        self.assertIn(
            "Math.min(actionCap, budget - cellWidth(prefix) - routeWidth - tailWidth - 2)",
            widget,
        )
        # The plan panel's liveness cues are the ONE sanctioned animation:
        # a colour wave through the active item's characters plus a walking
        # ellipsis on the [Plan] header, both mounted only while an active
        # item exists. The shimmer hook is accessed guarded (never
        # destructured), so hosts without it render a static line instead of
        # crashing the widget — and it stays out of the doctor's required
        # SDK surface for the same reason.
        self.assertIn("typeof sdk.useShimmerPhase === 'function'", widget)
        self.assertNotIn(", useShimmerPhase }", widget)
        self.assertIn("ShimmerText", widget)
        self.assertIn("PlanPulse", widget)
        # Changed on purpose (2026-08 HUD liveness fix): the wave and the
        # walking ellipsis both imply "actually running", so both now gate on
        # the HUD's exact in-flight signal too, not just an active item's
        # existence -- a stopped turn with an incomplete todo must not
        # animate as if work were still happening.
        self.assertIn("hasActive && live ? h(PlanPulse, { t }) : null", widget)
        self.assertIn(
            "const live = answerable ? !!(payload.activity && payload.activity.live) : true",
            widget,
        )
        self.assertIn("unchanged ${unchangedElapsed}", widget)
        self.assertNotIn("Number.MAX_SAFE_INTEGER", widget)
        # Changed on purpose: the parallel-shot badge moved off the bottom
        # status line onto the dock-top frame rule — the transcript's
        # "Tool calls (N)" group is host-owned rendering OMH cannot decorate.
        # Changed on purpose a second time: the badge now rides the [Plan]
        # header — the owner's chosen spot, directly under the host status
        # rule ('여기 위치 옆에 뜨게') — and the seconds-scale reader
        # freshness makes it vanish right after the batch lands. The bottom
        # dock and the frame rules render no parallel-shot text.
        self.assertIn("parallel shot ×", widget)
        self.assertIn("payload.parallel_shot", widget)
        self.assertIn("planShotBadge(payload, t)", widget)
        # Shift+Tab yolo state, as last hook-observed: ON warns in the
        # theme's yellow, OFF rests in the label blue, and an unobserved or
        # stale ledger renders nothing rather than a guessed "off".
        self.assertIn("' • yolo mode: '", widget)
        self.assertIn("payload.yolo && payload.yolo.status === 'observed'", widget)
        self.assertIn("payload.yolo.enabled ? t.color.warn : t.color.label", widget)
        self.assertNotIn("• parallel shot", widget)
        # Five-row activity budget with running AGENT lanes exempt from the
        # cap (OMO DAG-widget pattern) — the old hard `Math.min(3, …)` clamp
        # hid running lanes silently, which is the complaint that removed it.
        # The viewport still bounds the dock (chrome included), and both the
        # widget's own drop and the reader's cap surface as `+N more`.
        self.assertNotIn("Math.min(3, viewportRows", widget)
        self.assertIn("Math.max(Math.max(5 - mainRows.length, 1), runningAgents)", widget)
        self.assertIn("viewportRows - 5", widget)
        self.assertIn("const hiddenRows", widget)
        self.assertIn("Number(agents.hidden_rows) || 0", widget)
        self.assertIn("+${hiddenRows} more", widget)
        self.assertIn("hiddenRows\n        ? h(Text", widget)
        self.assertNotIn("spinnerTimerKey", widget)
        self.assertIn("ActivityRow", widget)
        self.assertIn("truncateCells", widget)
        self.assertIn("category:", widget)
        # Prepared-route provenance renders: a fallback lane carries a
        # warning-colored `fallback` token, and an exhausted chain reads
        # `category→inherit` instead of converging into plain inherit.
        self.assertIn("route_origin", widget)
        self.assertIn("route-fallback", widget)
        # One label shape for every lane: category(model tag). The category
        # names the lane and never changes; only the parenthesized model and
        # its state token (fallback / inherit) move.
        self.assertIn("routeOrigin === 'fallback' ? ['fallback', parentTag]", widget)
        self.assertIn("routeOrigin === 'exhausted_to_inherit' ? 'inherit'", widget)
        # A lane routed to the parent's own model keeps its category and
        # wears `=parent`; a child with no route record on that model is
        # the plain `inherit(model)` — inherit is not a category, so it
        # never wears the `category:` prefix.
        self.assertIn("const parentTag = row.same_as_parent === true ? '=parent' : ''", widget)
        # A fallback that landed on the parent's model keeps both tokens.
        self.assertIn("['fallback', parentTag].filter(Boolean).join(' ')", widget)
        self.assertIn("displayCategory === 'inherit'", widget)
        self.assertIn("? `inherit${model ? `(${model})` : ''}`", widget)
        self.assertNotIn("→inherit", widget)
        self.assertIn("row.route_category", widget)
        self.assertIn("tools", widget)
        self.assertIn("tok/s", widget)
        self.assertIn("cache_hit_percentage", widget)
        self.assertIn("context_percentage", widget)
        # Only observed cache/ctx values render on rows: "uncollected" was a
        # permanent label for hermes-native children (the host never records
        # a child's context percentage) and read as a fixable problem.
        self.assertNotIn("uncollected", widget)
        # The HEADER follows the same rule. `ctx --` was the last permanent
        # not-collected label, and it was permanent on EVERY session, not
        # some: `context_percentage` has a reader projection and this render
        # path but no production writer anywhere, and none can be derived --
        # the host's usage table sums input tokens across calls, which is not
        # a context size, and nothing records a model's window. The slot
        # stays wired for a future writer; the dash is gone.
        self.assertNotIn("ctx --", widget)
        self.assertIn("Number.isFinite(ctx) ? `ctx ${ctx}%` : ''", widget)
        self.assertIn("${metrics.ctx ? ` • ${metrics.ctx}` : ''}", widget)
        self.assertIn("'MAIN'", widget)
        self.assertIn("maestro.rows", widget)
        self.assertIn("fallback:", widget)
        self.assertIn("execFile(", widget)
        self.assertIn("Symbol.for(", widget)
        self.assertIn("generationKey", widget)
        self.assertIn("generation !== globalThis[generationKey]", widget)
        self.assertIn("clearTimeout(", widget)
        self.assertNotIn("payload ? { payload } : state", widget)
        # One immutable snapshot-apply helper feeds the combined dock app, and
        # both the initial read and the refresh timer go through it; each
        # applied snapshot stamps receivedAt so running rows can tick elapsed
        # live.
        self.assertEqual(
            widget.count("{ ...state, payload, receivedAt: Date.now(), tick: state.tick + 1 }"), 1
        )
        self.assertEqual(widget.count("applySnapshot(payload)"), 2)
        self.assertNotIn("friendlyWorkflow", widget)
        self.assertNotIn("'fanout-unit': 'Parallel work'", widget)
        self.assertIn("t.color.ok", widget)
        self.assertIn("t.color.error", widget)
        self.assertIn("t.color.warn", widget)
        self.assertNotIn("t.color.warning", widget)
        self.assertNotIn("spawnSync(", widget)
        self.assertNotIn("setInterval(", widget)
        for forbidden in ("payload.cwd", "payload.branch", "payload.context", "payload.cost"):
            self.assertNotIn(forbidden, widget)


if __name__ == "__main__":
    unittest.main()

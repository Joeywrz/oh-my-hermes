from __future__ import annotations

import base64
import json
import platform
import plistlib
import shutil
import struct
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
import omh.menubar_app as menubar_app_module
from omh.menubar_app import MENUBAR_APP_SCHEMA_VERSION, setup_menubar_app
from omh.paths import resolve_paths


class MenubarAppTests(unittest.TestCase):
    def test_embedded_character_icon_is_a_non_empty_36_pixel_png(self) -> None:
        icon_bytes = base64.b64decode(menubar_app_module.MENUBAR_ICON_BASE64, validate=True)

        self.assertGreater(len(icon_bytes), 24)
        self.assertEqual(icon_bytes[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(icon_bytes[12:16], b"IHDR")
        self.assertEqual(struct.unpack(">II", icon_bytes[16:24]), (36, 36))

    def test_install_materializes_exact_icon_bytes_and_passes_icon_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            expected_icon = base64.b64decode(menubar_app_module.MENUBAR_ICON_BASE64, validate=True)

            with (
                patch.object(menubar_app_module.Path, "home", return_value=root),
                patch(
                    "omh.menubar_app.shutil.which",
                    side_effect=lambda name: {
                        "swiftc": "/usr/bin/swiftc",
                        "node": "/Users/example/.nvm/versions/node/v24/bin/node",
                    }.get(name),
                ),
                patch(
                    "sys.executable",
                    "/Users/example/.pyenv/versions/3.12/bin/python3",
                ),
                patch("omh.menubar_app._compile_swift_helper"),
            ):
                payload = setup_menubar_app(
                    paths,
                    platform_name="Darwin",
                    start=False,
                    command_path="/Users/example/.npm-global/bin/omh",
                )
                managed = menubar_app_module.is_managed_menubar_install(paths)

            icon_path = paths.omh_home / "menubar" / "omh-character-mask.png"
            self.assertEqual(payload["icon"], str(icon_path))
            self.assertEqual(icon_path.read_bytes(), expected_icon)
            self.assertGreater(len(icon_path.read_bytes()), 0)
            launch_agent = root / "Library" / "LaunchAgents" / "com.rlaope.omh.menubar.plist"
            launch_agent_payload = plistlib.loads(launch_agent.read_bytes())
            arguments = launch_agent_payload["ProgramArguments"]
            icon_argument = arguments.index("--icon")
            self.assertEqual(arguments[icon_argument + 1], str(icon_path))
            self.assertTrue(managed)
            launch_path = launch_agent_payload["EnvironmentVariables"]["PATH"].split(":")
            self.assertEqual(
                launch_path[:3],
                [
                    "/Users/example/.nvm/versions/node/v24/bin",
                    "/Users/example/.pyenv/versions/3.12/bin",
                    "/Users/example/.npm-global/bin",
                ],
            )
            self.assertIn("/usr/local/bin", launch_path)
            self.assertIn("/opt/homebrew/bin", launch_path)
            self.assertIn("/usr/bin", launch_path)
            self.assertIn("/bin", launch_path)
            self.assertIn("/usr/sbin", launch_path)
            self.assertIn("/sbin", launch_path)

    def test_valid_launch_agent_repairs_a_missing_managed_copy(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            with patch.object(menubar_app_module.Path, "home", return_value=root):
                app_paths = menubar_app_module.menubar_app_paths(paths)
                app_paths["launch_agent"].parent.mkdir(parents=True)
                app_paths["launch_agent"].write_bytes(
                    plistlib.dumps(
                        {
                            "Label": menubar_app_module.MENUBAR_LABEL,
                            "ProgramArguments": [
                                str(app_paths["executable"]),
                                "--omh-command",
                                "/usr/local/bin/omh",
                                "--omh-home",
                                str(paths.omh_home),
                                "--hermes-home",
                                str(paths.hermes_home),
                            ],
                        }
                    )
                )

                managed = menubar_app_module.is_managed_menubar_install(paths)

        self.assertTrue(managed)

    def test_operator_owned_directory_without_launch_agent_is_not_managed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            with patch.object(menubar_app_module.Path, "home", return_value=root):
                app_paths = menubar_app_module.menubar_app_paths(paths)
                app_paths["app_dir"].mkdir(parents=True)
                (app_paths["app_dir"] / "README.txt").write_text("operator owned", encoding="utf-8")

                managed = menubar_app_module.is_managed_menubar_install(paths)

        self.assertFalse(managed)

    @unittest.skipIf(sys.platform == "win32", "symlink privileges vary on Windows")
    def test_symlinked_managed_paths_are_not_trusted(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            with patch.object(menubar_app_module.Path, "home", return_value=root):
                app_paths = menubar_app_module.menubar_app_paths(paths)
                paths.omh_home.mkdir(parents=True)
                target_dir = root / "attacker-owned"
                target_dir.mkdir()
                app_paths["app_dir"].symlink_to(target_dir, target_is_directory=True)
                app_paths["launch_agent"].parent.mkdir(parents=True)
                app_paths["launch_agent"].write_bytes(
                    plistlib.dumps(
                        {
                            "Label": menubar_app_module.MENUBAR_LABEL,
                            "ProgramArguments": [
                                str(app_paths["executable"]),
                                "--omh-command",
                                "/usr/local/bin/omh",
                                "--omh-home",
                                str(paths.omh_home),
                                "--hermes-home",
                                str(paths.hermes_home),
                            ],
                        }
                    )
                )

                managed = menubar_app_module.is_managed_menubar_install(paths)

        self.assertFalse(managed)

    @unittest.skipIf(sys.platform == "win32", "symlink privileges vary on Windows")
    def test_broken_launch_agent_symlink_is_not_managed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            with patch.object(menubar_app_module.Path, "home", return_value=root):
                app_paths = menubar_app_module.menubar_app_paths(paths)
                app_paths["launch_agent"].parent.mkdir(parents=True)
                app_paths["launch_agent"].symlink_to(root / "missing.plist")

                managed = menubar_app_module.is_managed_menubar_install(paths)

        self.assertFalse(managed)

    def test_global_launch_agent_must_match_target_homes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            with patch.object(menubar_app_module.Path, "home", return_value=root):
                app_paths = menubar_app_module.menubar_app_paths(paths)
                app_paths["app_dir"].mkdir(parents=True)
                app_paths["executable"].touch()
                app_paths["launch_agent"].parent.mkdir(parents=True)
                app_paths["launch_agent"].write_bytes(
                    plistlib.dumps(
                        {
                            "Label": menubar_app_module.MENUBAR_LABEL,
                            "ProgramArguments": [
                                str(app_paths["executable"]),
                                "--omh-command",
                                "/usr/local/bin/omh",
                                "--omh-home",
                                str(paths.omh_home),
                                "--hermes-home",
                                str(root / "another-hermes-home"),
                            ],
                        }
                    )
                )

                managed = menubar_app_module.is_managed_menubar_install(paths)

        self.assertFalse(managed)

    @unittest.skipUnless(platform.system() == "Darwin", "symlink resolution targets launchd")
    def test_launch_agent_path_resolves_node_shim_and_deduplicates_real_bin(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_bin = root / "runtime" / "bin"
            shim_bin = root / "shims"
            real_bin.mkdir(parents=True)
            shim_bin.mkdir()
            real_node = real_bin / "node"
            real_node.write_text("", encoding="utf-8")
            shim_node = shim_bin / "node"
            shim_node.symlink_to(real_node)

            with (
                patch("omh.menubar_app.shutil.which", return_value=str(shim_node)),
                patch("sys.executable", str(real_bin / "python3")),
            ):
                launch_path = menubar_app_module._launch_agent_path(str(real_bin / "omh"))

        path_entries = launch_path.split(":")
        self.assertEqual(path_entries.count(str(real_bin.resolve())), 1)
        self.assertNotIn(str(shim_bin), path_entries)

    def test_setup_menubar_app_skips_unsupported_platform(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")

            payload = setup_menubar_app(paths, platform_name="Linux", dry_run=True)

            self.assertEqual(payload["schema_version"], MENUBAR_APP_SCHEMA_VERSION)
            self.assertEqual(payload["status"], "skipped")
            self.assertFalse(payload["supported"])
            self.assertFalse((root / ".omh" / "menubar").exists())

    def test_setup_menubar_app_darwin_dry_run_reports_install_plan(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")

            with patch("omh.menubar_app.shutil.which", return_value="/usr/bin/swiftc"):
                payload = setup_menubar_app(
                    paths,
                    platform_name="Darwin",
                    dry_run=True,
                    command_path="/usr/local/bin/omh",
                )

            self.assertEqual(payload["schema_version"], MENUBAR_APP_SCHEMA_VERSION)
            self.assertEqual(payload["status"], "dry_run")
            self.assertTrue(payload["supported"])
            self.assertFalse(payload["installed"])
            self.assertEqual(payload["swiftc"], "/usr/bin/swiftc")
            self.assertEqual(payload["omh_command"], "/usr/local/bin/omh")
            self.assertFalse((root / ".omh" / "menubar").exists())

    def test_menubar_install_cli_dry_run_uses_contract_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("omh.menubar_app.platform.system", return_value="Darwin"),
                patch("omh.menubar_app.shutil.which", return_value="/usr/bin/swiftc"),
                patch("omh.menubar_app._resolved_omh_command", return_value="/usr/local/bin/omh"),
            ):
                status, stdout, stderr = run_cli(
                    [
                        "--omh-home",
                        str(root / ".omh"),
                        "--hermes-home",
                        str(root / ".hermes"),
                        "menubar",
                        "install",
                        "--dry-run",
                    ]
                )

            self.assertEqual(stderr, "")
            self.assertEqual(status, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["schema_version"], MENUBAR_APP_SCHEMA_VERSION)
            self.assertEqual(payload["status"], "dry_run")
            self.assertEqual(payload["omh_command"], "/usr/local/bin/omh")
            self.assertFalse((root / ".omh" / "menubar").exists())

    def test_native_helper_requests_json_status_payload(self) -> None:
        self.assertIn(
            '["menubar", "status", "--observe-local-processes", "--json"]',
            menubar_app_module._SWIFT_SOURCE,
        )

    def test_native_helper_renders_v2_table_rows_and_count_title(self) -> None:
        source = menubar_app_module._SWIFT_SOURCE

        self.assertIn('== "table_row"', source)
        self.assertIn('fixedWidth((row["left"] as? String) ?? "", 18)', source)
        self.assertIn('(row["right"] as? String) ?? ""', source)
        self.assertIn('rows.prefix(6)', source)
        self.assertIn('display?["menu_bar_title"]', source)
        self.assertNotIn('agent_status', source)

    def test_native_helper_refreshes_off_the_main_thread_and_updates_the_open_menu(self) -> None:
        # The owner's report: the menu looked stuck on its first reading.
        # Three causes, each pinned here. The status subprocess ran on the
        # main thread (a ~0.5s freeze every tick); the timer lived in the
        # default run-loop mode, silent for as long as the menu stayed open;
        # and every refresh replaced `statusItem.menu`, which AppKit only
        # shows on the NEXT open. One long-lived NSMenu mutated in place,
        # a `.common`-mode timer, a background queue, and a refresh on
        # `menuWillOpen` are the fix; the footer stamps the reading's time.
        source = menubar_app_module._SWIFT_SOURCE

        self.assertIn("NSMenuDelegate", source)
        self.assertIn("func menuWillOpen(_ menu: NSMenu)", source)
        self.assertIn("RunLoop.main.add(timer, forMode: .common)", source)
        self.assertIn("refreshQueue.async { [weak self] in", source)
        self.assertIn("DispatchQueue.main.async {", source)
        self.assertIn("menu.removeAllItems()", source)
        self.assertEqual(source.count("statusItem.menu = menu"), 1)
        self.assertNotIn("let menu = NSMenu()", source.split("private let menu = NSMenu()", 1)[1])
        self.assertIn('let stamp = "Updated \\(clock.string(from: refreshedAt))"', source)
        # A single failed read keeps the last good cards instead of flashing
        # the attention mark; the mark returns once the failure persists.
        self.assertIn("if lastPayload != nil && consecutiveFailures < 3 {", source)
        # Rows are information, not commands: enabled so they render in the
        # normal text color rather than the disabled gray that reads as
        # "still loading".
        self.assertIn("menu.autoenablesItems = false", source)
        self.assertIn("item.isEnabled = true", source)
        self.assertNotIn("item.isEnabled = false", source)
        # Drain stdout before waiting so a large payload cannot deadlock.
        self.assertLess(source.index("readDataToEndOfFile()"), source.index("process.waitUntilExit()"))

    def test_native_helper_loads_template_icon_at_18_points_with_accessible_label(self) -> None:
        source = menubar_app_module._SWIFT_SOURCE

        self.assertIn('case "--icon":', source)
        self.assertIn('NSImage(contentsOfFile: iconPath)', source)
        self.assertIn('image.size = NSSize(width: 18, height: 18)', source)
        self.assertIn('image.isTemplate = true', source)
        self.assertIn('button.imagePosition = .imageLeading', source)
        self.assertIn('button.setAccessibilityLabel(', source)
        self.assertIn(r'"OMH — \(headline) — \(summary)"', source)
        self.assertNotIn('statusItem.button?.title = "omh !"', source)
        self.assertNotIn(r'? "\(title) \(mark)" : menuBarTitle', source)

    def test_native_helper_keeps_sessions_table_header_visible(self) -> None:
        source = menubar_app_module._SWIFT_SOURCE

        self.assertIn('let line = tableHeaderTitle(columns)', source)
        self.assertIn('return fixedWidth(value, 18)', source)
        self.assertNotIn('fixedWidth(value, 12)', source)

    def test_native_helper_bounds_table_values_without_truncating_tooltips(self) -> None:
        source = menubar_app_module._SWIFT_SOURCE

        self.assertIn('fixedWidth((row["right"] as? String) ?? "", 24)', source)
        self.assertIn('item.toolTip = rowToolTip(row)', source)
        self.assertIn('return fixedWidth(value, 24)', source)
        tooltip_start = source.index("    private func rowToolTip")
        tooltip_end = source.index("\n    private func menuCards", tooltip_start)
        tooltip_source = source[tooltip_start:tooltip_end]
        self.assertIn('let right = (row["right"] as? String) ?? ""', tooltip_source)
        self.assertIn('return "\\(left): \\(right)"', tooltip_source)
        self.assertNotIn("fixedWidth", tooltip_source)

    def test_native_helper_fixed_width_truncates_a_long_value_to_24_characters(self) -> None:
        swiftc = shutil.which("swiftc")
        if swiftc is None:
            self.skipTest("Native fixed-width behavior coverage requires swiftc")

        source = menubar_app_module._SWIFT_SOURCE
        function_start = source.index("    private func fixedWidth")
        function_end = source.index("\n    private func rowTitle", function_start)
        fixed_width_source = source[function_start:function_end].replace(
            "    private func fixedWidth",
            "func fixedWidth",
            1,
        )
        long_value = "abcdefghijklmnopqrstuvwxyz0123456789"
        expected = long_value[:23] + "…"

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = root / "main.swift"
            executable = root / "fixed-width-test"
            harness.write_text(
                f'import Foundation\n{fixed_width_source}\nprint(fixedWidth("{long_value}", 24))\n',
                encoding="utf-8",
            )
            compile_result = subprocess.run(
                [swiftc, str(harness), "-o", str(executable)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr or compile_result.stdout)
            run_result = subprocess.run(
                [str(executable)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
            )

        self.assertEqual(run_result.returncode, 0, run_result.stderr or run_result.stdout)
        self.assertEqual(run_result.stdout.rstrip("\n"), expected)
        self.assertEqual(len(expected), 24)

    def test_launch_agent_still_passes_the_active_interval(self) -> None:
        # Backing off while idle must not quietly redefine `--interval`: it
        # is still the cadence a live machine polls at, and the installer
        # still names it.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            with (
                patch.object(menubar_app_module.Path, "home", return_value=root),
                patch("omh.menubar_app.shutil.which", return_value="/usr/bin/swiftc"),
                patch("omh.menubar_app._compile_swift_helper"),
            ):
                setup_menubar_app(paths, platform_name="Darwin", start=False, command_path="/usr/local/bin/omh")

            launch_agent = root / "Library" / "LaunchAgents" / "com.rlaope.omh.menubar.plist"
            arguments = plistlib.loads(launch_agent.read_bytes())["ProgramArguments"]

        interval_argument = arguments.index("--interval")
        self.assertEqual(
            arguments[interval_argument + 1],
            str(menubar_app_module.DEFAULT_REFRESH_INTERVAL_SECONDS),
        )

    def test_idle_interval_steps_one_rung_per_unchanged_reading(self) -> None:
        active = float(menubar_app_module.DEFAULT_REFRESH_INTERVAL_SECONDS)
        multipliers = menubar_app_module.IDLE_BACKOFF_MULTIPLIERS
        ceiling = float(menubar_app_module.IDLE_BACKOFF_CEILING_SECONDS)

        ladder = [menubar_app_module.menubar_idle_interval(active, step) for step in range(len(multipliers) + 2)]

        self.assertEqual(ladder[0], active, "a change must return to the active cadence, not a backed-off one")
        self.assertEqual(ladder, [min(active * value, ceiling) for value in multipliers] + [ladder[-1]] * 2)
        self.assertEqual(ladder[-1], max(ladder), "the ladder must never step back down on its own")
        self.assertLessEqual(ladder[-1], ceiling)
        # Each rung halves the spawn rate of the one before it, so the saving
        # a rung buys is legible from the rung alone.
        self.assertEqual(ladder[1] / ladder[0], 2.0)

    def test_idle_ceiling_never_polls_faster_than_the_operator_asked(self) -> None:
        # An operator who set a cadence slower than the ceiling keeps it; the
        # ceiling exists to stop backoff running away, not to speed anyone up.
        slow = float(menubar_app_module.IDLE_BACKOFF_CEILING_SECONDS) * 3

        intervals = {
            step: menubar_app_module.menubar_idle_interval(slow, step)
            for step in range(len(menubar_app_module.IDLE_BACKOFF_MULTIPLIERS))
        }

        self.assertEqual(set(intervals.values()), {slow})

    def test_native_helper_backoff_ladder_is_substituted_not_retyped(self) -> None:
        source = menubar_app_module._SWIFT_SOURCE
        expected_multipliers = ", ".join(
            str(float(value)) for value in menubar_app_module.IDLE_BACKOFF_MULTIPLIERS
        )

        self.assertNotIn("__OMH_", source, "a placeholder survived rendering, so a constant is missing")
        self.assertIn(f"let multipliers: [Double] = [{expected_multipliers}]", source)
        self.assertIn(
            f"let ceiling: Double = {float(menubar_app_module.IDLE_BACKOFF_CEILING_SECONDS)}",
            source,
        )
        self.assertIn(
            f"let omhWatchTickSeconds: Double = {float(menubar_app_module.WATCH_TICK_SECONDS)}",
            source,
        )
        # The timer fires on the watch tick and the tick decides whether to
        # pay for a reading; wiring it straight to refresh() would restore
        # the flat cost this change exists to remove.
        self.assertIn("Timer(timeInterval: omhWatchTickSeconds, repeats: true)", source)
        self.assertIn("self?.tick()", source)
        self.assertNotIn("self?.refresh()", source)

    def test_native_helper_resets_the_ladder_when_the_menu_changes(self) -> None:
        source = menubar_app_module._SWIFT_SOURCE

        # The whole branch, not the two tokens: `idleSteps = 0` also spells
        # the variable's declaration, so a bare `assertIn` for it passes on
        # a helper that never resets at all.
        self.assertIn(
            "if let previous = lastRenderFingerprint, previous == fingerprint, !wokeOnWatch {\n"
            "            idleSteps += 1\n"
            "        } else {\n"
            "            idleSteps = 0\n"
            "        }\n",
            source,
        )
        # The fingerprint is the rendered display, not the payload: the
        # payload stamps `observed_at` and `age_seconds` on every reading, so
        # comparing payloads would pin the ladder to rung 0 forever.
        self.assertIn("let fingerprint = renderFingerprint(display)", source)
        self.assertIn("JSONSerialization.data(withJSONObject: display, options: [.sortedKeys])", source)
        self.assertNotIn("renderFingerprint(payload)", source)

    def test_native_helper_stats_watch_paths_without_reading_them(self) -> None:
        source = menubar_app_module._SWIFT_SOURCE

        self.assertIn('watchPaths = (payload["watch_paths"] as? [String]) ?? []', source)
        self.assertIn("attributesOfItem(atPath: path)", source)
        self.assertIn("attributes[.modificationDate]", source)
        self.assertIn("attributes[.size]", source)
        # Stats only. A read here would pay for the very thing the tick
        # exists to avoid, and state.db is 500MB on the owner machine.
        watch_start = source.index("private func watchStamp()")
        watch_end = source.index("\n    // What the menu renders", watch_start)
        watch_body = source[watch_start:watch_end]
        for forbidden in ("contentsOfFile", "contentsAtPath", "Data(contentsOf", "String(contentsOf"):
            self.assertNotIn(forbidden, watch_body)

    def test_native_helper_never_overlaps_status_processes(self) -> None:
        # A slow spawn plus a fast timer is how a menu bar becomes a fork
        # storm, and the tick is now fast while a reading still takes ~0.7s.
        source = menubar_app_module._SWIFT_SOURCE
        refresh_start = source.index("    private func refresh() {")
        refresh_body = source[refresh_start : source.index("\n    private func apply(", refresh_start)]

        self.assertLess(refresh_body.index("if refreshInFlight {"), refresh_body.index("refreshInFlight = true"))
        self.assertLess(refresh_body.index("refreshInFlight = true"), refresh_body.index("refreshQueue.async"))
        self.assertIn("self.refreshInFlight = false", refresh_body)
        self.assertEqual(source.count("try process.run()"), 1)

    def test_watch_wake_is_floored_at_the_active_interval(self) -> None:
        # The defect this floor exists for: Hermes writes its session store
        # every few seconds while a session is live, so an unfloored watch
        # path reads far more often than the rung allows. Measured over 15
        # minutes against one live session writing once a second: unfloored
        # 169 readings, a flat 8s timer 102, floored 93.
        active = float(menubar_app_module.DEFAULT_REFRESH_INTERVAL_SECONDS)
        tick = float(menubar_app_module.WATCH_TICK_SECONDS)
        top_rung = len(menubar_app_module.IDLE_BACKOFF_MULTIPLIERS) - 1

        below_floor = [
            menubar_app_module.menubar_should_read(elapsed, active, top_rung, True)
            for elapsed in (0.0, tick, active - 0.001)
        ]
        at_floor = menubar_app_module.menubar_should_read(active, active, top_rung, True)
        without_a_move = menubar_app_module.menubar_should_read(active, active, top_rung, False)

        self.assertEqual(below_floor, [False, False, False])
        self.assertTrue(at_floor, "a moved stamp must still shorten a backed-off rung")
        self.assertFalse(without_a_move, "nothing moved, so the rung decides")

    def test_scheduled_read_is_never_blocked_by_a_still_watch_list(self) -> None:
        # The floor shortens the wait; it must not lengthen it. Whatever the
        # watch paths say, the ladder's own deadline still fires.
        active = float(menubar_app_module.DEFAULT_REFRESH_INTERVAL_SECONDS)

        decisions = {
            step: menubar_app_module.menubar_should_read(
                menubar_app_module.menubar_idle_interval(active, step), active, step, False
            )
            for step in range(len(menubar_app_module.IDLE_BACKOFF_MULTIPLIERS) + 1)
        }

        self.assertEqual(set(decisions.values()), {True})

    def test_native_helper_tick_routes_every_read_through_the_floor(self) -> None:
        source = menubar_app_module._SWIFT_SOURCE
        tick_start = source.index("    private func tick() {")
        tick_body = source[tick_start : source.index("\n    // Stats only.", tick_start)]

        self.assertIn("omhShouldRead(elapsed, activeInterval, idleSteps, stampMoved)", tick_body)
        self.assertIn("return watchStampMoved && secondsSinceLastRead >= activeInterval", source)
        # The tick must not keep a second, unfloored way to spawn. Before the
        # floor existed the watch branch called refresh() directly, and that
        # is the shape that has to stay gone.
        self.assertEqual(tick_body.count("refresh()"), 2, tick_body)
        self.assertNotIn("if watchStamp() != lastWatchStamp {", tick_body)
        # Activity restarts the ladder at the active rung rather than letting
        # it climb through a live session.
        self.assertIn("wokeOnWatch = stampMoved", tick_body)
        # Scoped to apply(), because `wokeOnWatch = false` also spells the
        # variable's declaration; counting the whole source would pass on a
        # helper that never consumes the flag at all.
        apply_start = source.index("    private func apply(")
        apply_body = source[apply_start : source.index("\n    private func footerText(", apply_start)]
        self.assertEqual(
            apply_body.count("wokeOnWatch = false"),
            2,
            "the success branch and the failure branch must each consume the flag",
        )

    def test_native_helper_read_decision_matches_the_python_mirror(self) -> None:
        swiftc = shutil.which("swiftc")
        if swiftc is None:
            self.skipTest("Read-decision parity coverage requires swiftc")

        source = menubar_app_module._SWIFT_SOURCE
        ladder_start = source.index("func omhIdleInterval(")
        decision_start = source.index("func omhShouldRead(")
        decision_end = source.index("\n}\n", decision_start) + len("\n}\n")
        mirrored = source[ladder_start:decision_end]
        grid = [
            (elapsed, active, step, moved)
            for active in (8.0, 30.0)
            for step in range(5)
            for elapsed in (0.0, 2.0, 7.9, 8.0, 29.0, 30.0, 59.0, 60.0, 61.0, 240.0)
            for moved in (False, True)
        ]
        expected = [
            menubar_app_module.menubar_should_read(elapsed, active, step, moved)
            for elapsed, active, step, moved in grid
        ]
        calls = "\n".join(
            f"print(omhShouldRead({elapsed}, {active}, {step}, {str(moved).lower()}))"
            for elapsed, active, step, moved in grid
        )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = root / "main.swift"
            executable = root / "read-parity"
            harness.write_text(f"import Foundation\n{mirrored}\n{calls}\n", encoding="utf-8")
            compile_result = subprocess.run(
                [swiftc, str(harness), "-o", str(executable)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr or compile_result.stdout)
            run_result = subprocess.run(
                [str(executable)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
            )

        self.assertEqual(run_result.returncode, 0, run_result.stderr or run_result.stdout)
        observed = [line == "true" for line in run_result.stdout.split()]
        self.assertEqual(len(observed), len(expected))
        self.assertEqual(observed, expected)
        self.assertIn(True, expected)
        self.assertIn(False, expected)

    def test_native_helper_backoff_matches_the_python_mirror(self) -> None:
        swiftc = shutil.which("swiftc")
        if swiftc is None:
            self.skipTest("Backoff parity coverage requires swiftc")

        source = menubar_app_module._SWIFT_SOURCE
        function_start = source.index("func omhIdleInterval(")
        function_end = source.index("\n}\n", function_start) + len("\n}\n")
        decision_source = source[function_start:function_end]
        grid = [(active, step) for active in (1.0, 8.0, 30.0, 180.0) for step in range(6)]
        expected = [menubar_app_module.menubar_idle_interval(active, step) for active, step in grid]
        calls = "\n".join(f"print(omhIdleInterval({active}, {step}))" for active, step in grid)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = root / "main.swift"
            executable = root / "backoff-parity"
            harness.write_text(f"import Foundation\n{decision_source}\n{calls}\n", encoding="utf-8")
            compile_result = subprocess.run(
                [swiftc, str(harness), "-o", str(executable)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr or compile_result.stdout)
            run_result = subprocess.run(
                [str(executable)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
            )

        self.assertEqual(run_result.returncode, 0, run_result.stderr or run_result.stdout)
        observed = [float(line) for line in run_result.stdout.split()]
        self.assertEqual(observed, expected)

    def test_native_helper_swift_source_compiles_on_darwin(self) -> None:
        swiftc = shutil.which("swiftc")
        if platform.system() != "Darwin" or swiftc is None:
            self.skipTest("Swift/AppKit compile coverage requires swiftc on Darwin")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "OMHMenuBar.swift"
            executable = root / "omh-menubar"
            source.write_text(menubar_app_module._SWIFT_SOURCE, encoding="utf-8")
            result = subprocess.run(
                [swiftc, "-framework", "AppKit", str(source), "-o", str(executable)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_menubar_start_defaults_to_human_readable_output(self) -> None:
        payload = {
            "schema_version": MENUBAR_APP_SCHEMA_VERSION,
            "operation": "start",
            "platform": "Darwin",
            "label": "com.rlaope.omh.menubar",
            "launch_agent": "/tmp/com.rlaope.omh.menubar.plist",
            "started": True,
            "status": "running",
            "message": "OMH menu bar helper started.",
        }
        with patch("omh.commands.menubar.start_menubar_app", return_value=payload):
            status, stdout, stderr = run_cli(["menubar", "start"], output_json=False)

        self.assertEqual(stderr, "")
        self.assertEqual(status, 0)
        self.assertIn("OMH menu bar start", stdout)
        self.assertIn("Status: running", stdout)
        self.assertIn("Result: helper started", stdout)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(stdout)

        with patch("omh.commands.menubar.start_menubar_app", return_value=payload):
            status, stdout, stderr = run_cli(["menubar", "start", "--json"], output_json=False)

        self.assertEqual(stderr, "")
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout)["schema_version"], MENUBAR_APP_SCHEMA_VERSION)

    def test_menubar_stop_failure_is_readable_without_json(self) -> None:
        payload = {
            "schema_version": MENUBAR_APP_SCHEMA_VERSION,
            "operation": "stop",
            "platform": "Darwin",
            "label": "com.rlaope.omh.menubar",
            "launch_agent": "/tmp/com.rlaope.omh.menubar.plist",
            "stopped": False,
            "status": "failed",
            "message": "Boot-out failed: 5: Input/output error\nTry re-running the command as root for richer errors.",
        }
        with patch("omh.commands.menubar.stop_menubar_app", return_value=payload):
            status, stdout, stderr = run_cli(["menubar", "stop"], output_json=False)

        self.assertEqual(stderr, "")
        self.assertEqual(status, 0)
        self.assertIn("OMH menu bar stop", stdout)
        self.assertIn("Status: failed", stdout)
        self.assertIn("Boot-out failed: 5: Input/output error", stdout)
        self.assertIn("Run `omh menubar status`", stdout)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(stdout)

    def test_custom_path_uninstall_does_not_touch_user_launch_agent(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            omh_home = root / ".omh"
            hermes_home = root / ".hermes"
            self.assertEqual(run_cli(["--omh-home", str(omh_home), "--hermes-home", str(hermes_home), "setup"])[0], 0)

            with patch(
                "omh.commands.setup.uninstall_menubar_app",
                side_effect=AssertionError("custom path uninstall must not touch the user LaunchAgent"),
            ):
                status, stdout, stderr = run_cli(
                    ["--omh-home", str(omh_home), "--hermes-home", str(hermes_home), "uninstall"]
                )

            self.assertEqual(stderr, "")
            self.assertEqual(status, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["menubar_app"]["status"], "not_requested")


if __name__ == "__main__":
    unittest.main()

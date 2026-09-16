from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.runtime_reader import read_omh_hud  # noqa: E402
from test_kanban_board_reader import NOW, build_board, task  # noqa: E402


class HudPayloadBudgetTests(unittest.TestCase):
    def test_adversarial_plugin_inventory_stays_below_widget_buffer(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            omh_home = root / ".omh"
            hermes_home = root / ".hermes"
            plugin_dir = hermes_home / "plugins" / "omh"
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
            tools = "\n".join(f"  - tool-{index:05d}" for index in range(10_000))
            (plugin_dir / "plugin.yaml").write_text(
                f"version: 1.0.0\nprovides_tools:\n{tools}\n",
                encoding="utf-8",
            )

            payload = read_omh_hud(
                omh_home,
                hermes_home,
                status={"runs": []},
            )
            encoded = json.dumps(payload).encode("utf-8") + b"\n"

            self.assertLess(len(encoded), 65_536)
            self.assertLessEqual(
                len(payload["plugin"]["capabilities"]["advertised_tools"]),
                128,
            )
            self.assertEqual(payload["privacy"], "metadata_only")
            self.assertIn("graph", payload)

    def test_a_full_kanban_board_stays_below_the_widget_buffer(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            hermes_home.mkdir()
            build_board(hermes_home / "kanban.db", [
                task(f"t_{index:08d}", "ready", title="x" * 400, assignee="a" * 100,
                     skills='["' + "s" * 200 + '"]', model_override="m" * 200, created_at=NOW - index)
                for index in range(100)
            ])

            payload = read_omh_hud(root / ".omh", hermes_home, status={"runs": []})
            encoded = json.dumps(payload).encode("utf-8") + b"\n"

            self.assertLess(len(encoded), 65_536)
            self.assertEqual(len(payload["subagents"]["rows"]), 8)
            self.assertEqual(payload["kanban"]["rows_total"], 64)
            self.assertEqual(payload["subagents"]["hidden_rows"], 56)

"""The `/omh-model` widget app, driven end to end under node.

The app mirrors two rules of the bundle's `model_chain_picker` in
JavaScript (how a head model and an effort step) so a keypress never waits
on a python spawn. A mirror drifts unless something holds it to the
original, so these tests do not stub the app: a node harness loads the
installed-form widget with a fake SDK, lets `init` spawn the real reader
script against a copy of the plugin bundle, feeds key tokens through
`reduce`, lets Enter spawn the real writer, and the assertions compare the
document that lands on disk with what the python functions say it should
be. Skipped where node is absent; CI installs it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from omh.plugin_bundle.omh import model_chain_picker as bundle
from omh.plugin_bundle.omh.hermes_delegation import (
    HERMES_MIXTURE_CATEGORY_CHAINS,
    MIXTURE_CHAIN_OVERRIDES_SCHEMA_VERSION,
)
from omh.tui_widget_pack import widget_payload

NODE = shutil.which("node")
BUNDLE_DIR = Path(bundle.__file__).resolve().parent
OVERRIDE_QUICK = (("kimi-k3-ultrafast", "low"), ("glm-5.3", "low"))

HARNESS = r"""
import { pathToFileURL } from 'node:url'
const [widgetPath, keysJson, rowsArg] = process.argv.slice(2)
const KEY = { upArrow: false, downArrow: false, leftArrow: false, rightArrow: false, return: false, escape: false, ctrl: false, shift: false, meta: false, tab: false }
const inputs = {
  up: { ch: '', key: { ...KEY, upArrow: true } },
  down: { ch: '', key: { ...KEY, downArrow: true } },
  left: { ch: '', key: { ...KEY, leftArrow: true } },
  right: { ch: '', key: { ...KEY, rightArrow: true } },
  minus: { ch: '-', key: KEY },
  plus: { ch: '+', key: KEY },
  default: { ch: 'd', key: KEY },
  enter: { ch: '', key: { ...KEY, return: true } },
  quit: { ch: '', key: { ...KEY, escape: true } },
  other: { ch: 'x', key: KEY },
}
const apps = []
const held = { state: null, closed: false }
const sdk = {
  Box: 'Box', Dialog: 'Dialog', Overlay: 'Overlay', Text: 'Text',
  // Function components expand so their text is in the frame, as on screen.
  h: (type, props, ...children) => (typeof type === 'function' ? type({ ...(props || {}), children }) : { type, props, children }),
  defineWidgetApp: app => { apps.push(app); return app },
  openWidget() {},
  updateWidget: (app, fn) => { if (app && app.id === 'omh-model' && held.state) held.state = fn(held.state) },
}
const mod = await import(pathToFileURL(widgetPath).href)
mod.default(sdk)
const app = apps.find(candidate => candidate.id === 'omh-model')
if (!app) { console.log(JSON.stringify({ error: 'omh-model not registered' })); process.exit(0) }
const settle = async phases => {
  for (let i = 0; i < 800 && phases.includes(held.state.phase); i += 1) await new Promise(resolve => setTimeout(resolve, 25))
}
const theme = { color: { border: 'b', error: 'e', label: 'l', muted: 'm', ok: 'o', primary: 'p', statusFg: 's', text: 't', warn: 'w' } }
const render = () => JSON.stringify(app.render({ cols: 120, rows: Number(rowsArg || 40), state: held.state, t: theme }))
held.state = app.init('')
await settle(['loading'])
const frames = [render()]
for (const token of JSON.parse(keysJson)) {
  const next = app.reduce(held.state, inputs[token])
  if (next === null) { held.closed = true; break }
  held.state = next
  frames.push(render())
}
await settle(['saving'])
const report = JSON.stringify({
  phase: held.state.phase, message: held.state.message, chains: held.state.chains, cursor: held.state.cursor,
  closed: held.closed, usage: app.init('extra') === null, frames,
})
// A pipe write is asynchronous; exiting before it drains truncates the report.
process.stdout.write(`${report}\n`, () => process.exit(0))
"""


def _write_overrides(omh_home: Path, categories: dict[str, tuple[tuple[str, str], ...]]) -> None:
    path = omh_home / "routing" / "model-chains.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": MIXTURE_CHAIN_OVERRIDES_SCHEMA_VERSION,
        "categories": {
            name: [{"model": model, "reasoning_effort": effort} for model, effort in chain]
            for name, chain in categories.items()
        },
    }
    path.write_text(json.dumps(document), encoding="utf-8")


@unittest.skipUnless(NODE, "node is not installed; the widget harness needs it")
class ModelWidgetTests(unittest.TestCase):
    def _drive(
        self,
        keys: list[str],
        *,
        overrides: dict | None = None,
        overrides_text: str | None = None,
        entitlements_text: str | None = None,
        rows: int = 40,
        named_store: bool = False,
        stray_env_home: bool = False,
    ):
        """Run the harness; return (harness result, override document or None, python payload).

        ``rows`` is the terminal height the frames are rendered at: 40 fits
        every category; a shorter height exercises the windowing.
        ``named_store`` has the Hermes home's config.yaml name the store
        under `plugins.entries.omh.settings.omh_home` and leaves `OMH_HOME`
        out of the environment, the shape of a bot-profile TUI;
        ``stray_env_home`` adds an `OMH_HOME` pointing elsewhere on top.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / "hermes"
            omh_home = root / "omh"
            shutil.copytree(BUNDLE_DIR, hermes_home / "plugins" / "omh", ignore=shutil.ignore_patterns("__pycache__"))
            if overrides:
                _write_overrides(omh_home, overrides)
            if overrides_text is not None:
                document_file = omh_home / "routing" / "model-chains.json"
                document_file.parent.mkdir(parents=True, exist_ok=True)
                document_file.write_text(overrides_text, encoding="utf-8")
            if entitlements_text is not None:
                record = omh_home / "routing" / "providers.json"
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text(entitlements_text, encoding="utf-8")
            payload = bundle.picker_rows(omh_home, hermes_home=hermes_home)
            widget = root / "omh-status.mjs"
            widget.write_bytes(widget_payload(Path(sys.executable)))
            harness = root / "harness.mjs"
            harness.write_text(HARNESS, encoding="utf-8")
            env = {**os.environ, "HERMES_HOME": str(hermes_home), "HOME": str(root)}
            env.pop("OMH_HOME", None)
            if named_store:
                (hermes_home / "config.yaml").write_text(
                    f"plugins:\n  enabled:\n    - omh\n  entries:\n    omh:\n      settings:\n        omh_home: {omh_home.as_posix()}\n",
                    encoding="utf-8",
                )
                if stray_env_home:
                    env["OMH_HOME"] = str(root / "elsewhere")
            else:
                env["OMH_HOME"] = str(omh_home)
            env.pop("HERMES_TUI_ACTIVE_SESSION_FILE", None)
            # Node writes the report as UTF-8; without saying so, Windows
            # decodes the pipe in its code page and the frames' glyphs fail.
            completed = subprocess.run(
                [NODE, str(harness), str(widget), json.dumps(keys), str(rows)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
                env=env,
                cwd=str(root),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertNotIn("error", result, result)
            # The copy above excludes `__pycache__`, and nothing may put it
            # back: the widget's spawns import the copied bundle, and bytecode
            # landing here raced this very block's teardown in CI (issue
            # #1550). Checked on every drive rather than in one case, because
            # the spawn set differs per key sequence and only the drive that
            # writes would catch it. `-B` on the spawn is what holds this --
            # `-I` implies `-E`, so no environment variable can.
            self.assertEqual(
                [str(path.relative_to(root)) for path in sorted(root.rglob("__pycache__"))], [],
                "a widget spawn wrote bytecode into the temp tree; check `-B` on its argv",
            )
            document_path = omh_home / "routing" / "model-chains.json"
            document = None
            if document_path.exists() and overrides_text is None:
                document = json.loads(document_path.read_text(encoding="utf-8"))
            if stray_env_home:
                # The store the environment named must stay untouched.
                self.assertFalse((root / "elsewhere").exists())
            return result, document, payload

    def test_a_profile_that_names_its_store_is_edited_there_with_no_environment_home(self) -> None:
        """The widget forced `OMH_HOME` (or `~/.omh`) into every spawn, so in
        a profile that names its own store `/omh-model` read and saved a file
        the profile's dispatches never looked at (#1679). With nothing named,
        the bundle resolves the profile's setting the way its plugin does,
        and an `OMH_HOME` the TUI happened to be launched with loses to it.
        """
        for stray in (False, True):
            with self.subTest(stray_env_home=stray):
                result, document, payload = self._drive(
                    ["down", "right", "plus", "enter"],
                    overrides={"writing": (("custom-store-model", "high"),)},
                    named_store=True,
                    stray_env_home=stray,
                )
                self.assertEqual(result["phase"], "saved", result["message"])
                self.assertTrue(result["message"].startswith("Saved 1 category to "), result["message"])
                # The reader read the named store: the override seeded there
                # is on its row before any key is pressed. This is the half
                # that produced the reported twelve `default` rows.
                first = result["frames"][0]
                self.assertIn("custom-store-model", first)
                self.assertIn("◆ override", first)
                self.assertIsNotNone(document, "the save did not land in the store the profile names")
                self.assertEqual(set(document["categories"]), {"deep", "writing"})
                self.assertEqual(payload["document_status"], "applied")

    def test_an_ignored_document_is_said_under_the_providers_line(self) -> None:
        """An override file the reader rejects is ignored whole, so every row
        reads `default` -- the picture of a chain that was never set. The one
        row the CLI picker adds for it, mirrored."""
        result, _document, payload = self._drive(["quit"], overrides_text="{")
        self.assertEqual(payload["document_status"], "invalid: unreadable JSON")
        first = result["frames"][0]
        self.assertIn("! model-chains.json ignored: unreadable JSON · every category shows its shipped default", first)
        self.assertEqual(first.count("model-chains.json ignored"), 1)
        # A valid or absent document adds no such row.
        result, _document, payload = self._drive(["quit"])
        self.assertEqual(payload["document_status"], "absent")
        self.assertNotIn("model-chains.json ignored", result["frames"][0])

    def test_the_footer_names_the_store_with_nothing_to_save(self) -> None:
        # Which file the picker edits is the question a person opens it with
        # when a chain set elsewhere seems not to have taken; it is not
        # withheld until they change something.
        result, _document, payload = self._drive(["quit"])
        first = result["frames"][0]
        self.assertIn("no unsaved changes · ", first)
        self.assertIn(Path(payload["path"]).name, first)

    def test_reads_the_rows_and_steps_exactly_like_the_python_original(self) -> None:
        result, document, payload = self._drive(["down", "right", "plus", "enter"])
        self.assertEqual(result["phase"], "saved", result["message"])
        self.assertFalse(result["closed"])
        self.assertTrue(result["message"].startswith("Saved 1 category to "), result["message"])
        ring = tuple(row["alias"] for row in payload["models"])
        expected = bundle.step_effort(bundle.step_head_model(HERMES_MIXTURE_CATEGORY_CHAINS["deep"], ring, 1), 1)
        self.assertEqual(document["categories"], {"deep": bundle.chain_entries(expected)})
        self.assertEqual(result["cursor"], 1)
        # The first ready frame lists every category by its routing name and
        # carries the key hint; labels fall back to the alias because the
        # bundle cannot see the catalog.
        first = result["frames"][0]
        for name in HERMES_MIXTURE_CATEGORY_CHAINS:
            self.assertIn(name, first)
        self.assertIn("gpt-6-astra", first)
        # The providers line says out loud that nothing is linked in the
        # harness home, so the absent served marks have their reason.
        self.assertIn("none linked to Hermes yet", first)
        self.assertIn("⏎", first)
        self.assertIn("save", first)
        self.assertIn("▍", result["frames"][-1])
        self.assertIn("◂ ", result["frames"][-1])
        self.assertIn("● edited", result["frames"][-1])
        self.assertIn("1 unsaved change · ⏎ writes", result["frames"][-1])
        self.assertTrue(result["usage"])

    def test_an_ignored_record_is_said_under_the_providers_line(self) -> None:
        """The one row the CLI picker adds for an invalid providers.json, mirrored."""
        result, _document, payload = self._drive(["quit"], entitlements_text="{")
        self.assertEqual(payload["entitlements_status"], "invalid: unreadable JSON")
        first = result["frames"][0]
        self.assertIn("! providers.json ignored: unreadable JSON · any providers it excluded count again", first)
        self.assertEqual(first.count("providers.json ignored"), 1)
        # A valid or absent record adds no such row.
        result, _document, payload = self._drive(["quit"])
        self.assertEqual(payload["entitlements_status"], "absent")
        self.assertNotIn("providers.json ignored", result["frames"][0])

    def test_the_ignored_record_row_costs_the_category_list_one_row_when_short(self) -> None:
        """The row budget shifts by one only when the ignored-record row shows.

        At 27 rows the twelve categories fit exactly (27 - 15 chrome rows)
        with an absent record; with the extra row one category is windowed
        out and the `more` marker says so. At the harness's usual 40 rows
        the budget never binds, which is why this test picks a short one.
        """
        self.assertEqual(len(HERMES_MIXTURE_CATEGORY_CHAINS), 12)
        result, _document, _payload = self._drive(["quit"], rows=27)
        first = result["frames"][0]
        self.assertIn("deep-work", first)
        self.assertNotIn("↓ 1 more", first)
        result, _document, _payload = self._drive(["quit"], rows=27, entitlements_text="{")
        first = result["frames"][0]
        self.assertIn("providers.json ignored", first)
        self.assertNotIn("deep-work", first)
        self.assertIn("↓ 1 more", first)
        # The ignored-document row costs one more, and both together two.
        result, _document, _payload = self._drive(["quit"], rows=27, overrides_text="{")
        self.assertIn("↓ 1 more", result["frames"][0])
        result, _document, _payload = self._drive(["quit"], rows=27, overrides_text="{", entitlements_text="{")
        first = result["frames"][0]
        self.assertIn("providers.json ignored", first)
        self.assertIn("model-chains.json ignored", first)
        self.assertIn("↓ 2 more", first)

    def test_escape_after_edits_writes_nothing(self) -> None:
        result, document, _ = self._drive(["right", "minus", "quit"])
        self.assertTrue(result["closed"])
        self.assertIsNone(document)
        self.assertIn("● edited", result["frames"][-1])

    def test_default_clears_an_override_and_unknown_keys_are_swallowed(self) -> None:
        quick_index = list(HERMES_MIXTURE_CATEGORY_CHAINS).index("quick")
        keys = ["other"] + ["down"] * quick_index + ["default", "enter"]
        result, document, _ = self._drive(keys, overrides={"quick": OVERRIDE_QUICK})
        self.assertEqual(result["phase"], "saved", result["message"])
        self.assertEqual(document["categories"], {})
        self.assertIn("◆ override", result["frames"][0])

    def test_enter_without_edits_closes_without_a_file(self) -> None:
        result, document, _ = self._drive(["down", "up", "enter"])
        self.assertTrue(result["closed"])
        self.assertIsNone(document)
        self.assertIn("no unsaved changes", result["frames"][-1])


if __name__ == "__main__":
    unittest.main()

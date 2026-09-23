from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
from omh.config_adapter import (
    ensure_plugin_enabled,
    plugin_enablement,
    plugin_enablement_shape_error,
    plugin_is_enabled,
    plugins_enabled_extension_error,
    remove_plugin_enabled,
)
from omh.maintenance.doctor import run_doctor
from omh.paths import resolve_paths
from omh.plugin_pack import PLUGIN_NAME


ENABLED_CONFIG = """skills:
  external_dirs: []

plugins:
  enabled:
    - omh
  disabled: []
  entries:
    omh:
      allow_tool_override: false
"""

DISABLED_CONFIG = ENABLED_CONFIG.replace("  enabled:\n    - omh\n", "  enabled:\n")


class PluginEnablementReaderTests(unittest.TestCase):
    """Hermes keeps enablement in `plugins.enabled` and nowhere else."""

    def test_block_list_form(self) -> None:
        self.assertTrue(plugin_is_enabled(ENABLED_CONFIG, PLUGIN_NAME))
        self.assertFalse(plugin_is_enabled(DISABLED_CONFIG, PLUGIN_NAME))

    def test_inline_list_form(self) -> None:
        self.assertTrue(plugin_is_enabled("plugins:\n  enabled: [omh, other]\n  disabled: []\n", PLUGIN_NAME))
        self.assertFalse(plugin_is_enabled("plugins:\n  enabled: []\n  disabled: []\n", PLUGIN_NAME))

    def test_explicitly_disabled_outranks_enabled(self) -> None:
        text = "plugins:\n  enabled:\n    - omh\n  disabled:\n    - omh\n"
        self.assertFalse(plugin_is_enabled(text, PLUGIN_NAME))

    def test_quoted_items_are_read(self) -> None:
        self.assertTrue(plugin_is_enabled('plugins:\n  enabled:\n    - "omh"\n  disabled: []\n', PLUGIN_NAME))

    def test_a_config_without_a_plugins_block_is_not_enabled(self) -> None:
        self.assertFalse(plugin_is_enabled("skills:\n  external_dirs: []\n", PLUGIN_NAME))

    def test_another_plugin_does_not_count(self) -> None:
        self.assertFalse(plugin_is_enabled("plugins:\n  enabled:\n    - browser\n  disabled: []\n", PLUGIN_NAME))

    def test_enablement_reports_both_lists(self) -> None:
        listed = plugin_enablement("plugins:\n  enabled:\n    - omh\n  disabled:\n    - browser\n")
        self.assertEqual(listed, {"enabled": ["omh"], "disabled": ["browser"]})


    def test_level_indented_list_items_are_read(self) -> None:
        # Issue #1322: `  - omh` (item level with its key) is valid YAML and
        # what a hand-edited config used; the reader saw it as a key line and
        # reported an empty list, so doctor blocked on a plugin that was on.
        level = "plugins:\n  enabled:\n  - omh\n  - agentiker-plan-follow\n  disabled:\n  - github-prs\n"
        self.assertEqual(plugin_enablement(level), {"enabled": ["omh", "agentiker-plan-follow"], "disabled": ["github-prs"]})
        self.assertTrue(plugin_is_enabled(level, PLUGIN_NAME))

    def test_level_indented_items_stay_with_their_own_key(self) -> None:
        level = "plugins:\n  enabled:\n  - other\n  disabled:\n  - omh\n  entries:\n    omh:\n      allow_tool_override: false\n"
        self.assertEqual(plugin_enablement(level), {"enabled": ["other"], "disabled": ["omh"]})
        self.assertFalse(plugin_is_enabled(level, PLUGIN_NAME))

    def test_items_under_an_untracked_key_are_ignored(self) -> None:
        text = "plugins:\n  order:\n  - omh\n  enabled: []\n"
        self.assertEqual(plugin_enablement(text), {"enabled": [], "disabled": []})


class EnsurePluginEnabledMatchesTheFilesIndentTests(unittest.TestCase):
    """A new list item takes the indent the file's plugin lists already use.

    Writing `    - omh` into a file whose items sit at `  - ` puts items at
    two depths under one key, which YAML reads as two nodes, not one list.
    """

    def test_level_style_file_gets_a_level_item(self) -> None:
        text = "plugins:\n  enabled:\n  - other\n  disabled:\n  - github-prs\n"
        change = ensure_plugin_enabled(text, PLUGIN_NAME)
        self.assertEqual((change.changed, change.text), (True, "plugins:\n  enabled:\n  - omh\n  - other\n  disabled:\n  - github-prs\n"))
        self.assertTrue(plugin_is_enabled(change.text, PLUGIN_NAME))

    def test_nested_style_file_keeps_a_nested_item(self) -> None:
        change = ensure_plugin_enabled(DISABLED_CONFIG, PLUGIN_NAME)
        self.assertEqual((change.changed, change.text), (True, ENABLED_CONFIG))

    def test_empty_enabled_follows_the_sibling_list(self) -> None:
        text = "plugins:\n  enabled:\n  disabled:\n  - github-prs\n"
        change = ensure_plugin_enabled(text, PLUGIN_NAME)
        self.assertEqual(change.text, "plugins:\n  enabled:\n  - omh\n  disabled:\n  - github-prs\n")

    def test_inline_expansion_follows_the_sibling_list(self) -> None:
        text = "plugins:\n  enabled: [other]\n  disabled:\n  - github-prs\n"
        change = ensure_plugin_enabled(text, PLUGIN_NAME)
        self.assertEqual(change.text, "plugins:\n  enabled:\n  - other\n  - omh\n  disabled:\n  - github-prs\n")

    def test_a_file_without_items_defaults_to_the_hermes_style(self) -> None:
        change = ensure_plugin_enabled("plugins:\n  enabled: []\n", PLUGIN_NAME)
        self.assertEqual(change.text, "plugins:\n  enabled:\n    - omh\n")
        change = ensure_plugin_enabled("plugins:\n  entries: {}\n", PLUGIN_NAME)
        self.assertEqual(change.text, "plugins:\n  enabled:\n    - omh\n  entries: {}\n")

    def test_a_level_item_never_reads_as_already_enabled_for_another_key(self) -> None:
        text = "plugins:\n  disabled:\n  - other\n"
        change = ensure_plugin_enabled(text, PLUGIN_NAME)
        self.assertEqual(change.text, "plugins:\n  enabled:\n  - omh\n  disabled:\n  - other\n")


class DoctorPluginEnabledCheckTests(unittest.TestCase):
    """The gap this closes: everything installed, nothing reachable.

    A live check found `omh doctor` reporting `Hermes registration: ok (4/4)`
    while the plugin sat disabled in Hermes, so no OMH tool was callable in chat
    and no check said so. Bundle installed, importable, and registrable are all
    separate questions from whether Hermes will load it.
    """

    def _paths(self, root: Path, config_text: str):
        hermes = root / ".hermes"
        (hermes / "plugins" / PLUGIN_NAME).mkdir(parents=True)
        (hermes / "config.yaml").write_text(config_text, encoding="utf-8")
        return resolve_paths(root / ".omh", hermes)

    def _check(self, checks, name: str):
        return next((c for c in checks if c.name == name), None)

    def test_a_disabled_plugin_is_a_blocking_check(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp), DISABLED_CONFIG)
            check = self._check(run_doctor(paths), "plugin_enabled_in_hermes")
            self.assertIsNotNone(check)
            self.assertFalse(check.ok)
            self.assertEqual(check.severity, "blocking")
            self.assertIn("not in plugins.enabled", check.message)
            self.assertIn("no OMH tool is reachable", check.message)
            self.assertIn(f"hermes plugins enable {PLUGIN_NAME}", check.remediation)

    def test_an_enabled_plugin_passes(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp), ENABLED_CONFIG)
            check = self._check(run_doctor(paths), "plugin_enabled_in_hermes")
            self.assertIsNotNone(check)
            self.assertTrue(check.ok, check.message)

    def test_an_explicitly_disabled_plugin_says_so(self) -> None:
        with TemporaryDirectory() as tmp:
            text = "plugins:\n  enabled:\n    - omh\n  disabled:\n    - omh\n"
            paths = self._paths(Path(tmp), text)
            check = self._check(run_doctor(paths), "plugin_enabled_in_hermes")
            self.assertFalse(check.ok)
            self.assertIn("listed as disabled", check.message)

    def test_a_missing_config_does_not_block(self) -> None:
        """Before setup runs there is nothing to enable, and that is not a fault."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".hermes" / "plugins" / PLUGIN_NAME).mkdir(parents=True)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            check = self._check(run_doctor(paths), "plugin_enabled_in_hermes")
            self.assertTrue(check.ok)
            self.assertFalse(check.observed)

    def test_the_check_only_runs_when_a_bundle_is_installed(self) -> None:
        """No installed bridge means nothing to enable; doctor must not invent a fault."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".hermes").mkdir(parents=True)
            (root / ".hermes" / "config.yaml").write_text(DISABLED_CONFIG, encoding="utf-8")
            paths = resolve_paths(root / ".omh", root / ".hermes")
            self.assertIsNone(self._check(run_doctor(paths), "plugin_enabled_in_hermes"))


class SetupEnablesThePluginTests(unittest.TestCase):
    """Installing the bridge without switching it on is the same as not shipping it."""

    def test_setup_leaves_the_plugin_enabled_and_doctor_clean(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]

            status, _stdout, stderr = run_cli(base + ["setup"])
            self.assertEqual(stderr, "")
            self.assertEqual(status, 0)

            config_text = (root / ".hermes" / "config.yaml").read_text(encoding="utf-8")
            self.assertTrue(
                plugin_is_enabled(config_text, PLUGIN_NAME),
                f"setup installed the bridge without enabling it: {plugin_enablement(config_text)}",
            )

            paths = resolve_paths(root / ".omh", root / ".hermes")
            check = next((c for c in run_doctor(paths) if c.name == "plugin_enabled_in_hermes"), None)
            self.assertIsNotNone(check)
            self.assertTrue(check.ok, check.message)

    def test_setup_does_not_re_enable_a_deliberate_opt_out(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]
            status, _stdout, stderr = run_cli(base + ["setup"])
            self.assertEqual(status, 0, stderr)

            config_path = root / ".hermes" / "config.yaml"
            opted_out = config_path.read_text(encoding="utf-8").replace(
                "  enabled:\n    - omh\n", "  enabled:\n  disabled:\n    - omh\n"
            )
            config_path.write_text(opted_out, encoding="utf-8")

            status, _stdout, stderr = run_cli(base + ["setup"])
            self.assertEqual(status, 0, stderr)
            self.assertFalse(
                plugin_is_enabled(config_path.read_text(encoding="utf-8"), PLUGIN_NAME),
                "setup must not override an explicit opt-out",
            )


# What setup wrote for `enabled: '[]'` before #1825. YAML refuses children
# under a closed value, so Hermes fails the file ("did not find expected key").
BROKEN_CONFIG = "plugins:\n  enabled: '[]'\n    - omh\n"
# Valid YAML: the items fold into the string `browser - omh`, so Hermes
# reads no list and loads no plugin, and nothing reports it.
FOLDED_CONFIG = "plugins:\n  enabled: browser\n    - omh\n"


class ScalarEnabledValueTests(unittest.TestCase):
    """`enabled: '[]'` is a string to YAML and no plugins to Hermes (#1825)."""

    def test_reader_treats_every_scalar_as_no_plugins(self) -> None:
        for value in ("'[]'", '"[]"', "null", "~", "browser", "'[browser]'"):
            with self.subTest(value=value):
                listed = plugin_enablement(f"plugins:\n  enabled: {value}\n  disabled: []\n")
                self.assertEqual(listed, {"enabled": [], "disabled": []})

    def test_reader_keeps_members_under_node_properties(self) -> None:
        # `&plist` / `!!seq` are properties of the list below, not a scalar:
        # every `*plist` alias in the file resolves to that list.
        for value in ("&plist", "!!seq", "&plist !!seq", "&plist  # shared"):
            with self.subTest(value=value):
                text = f"plugins:\n  enabled: {value}\n    - omh\n"
                self.assertEqual(plugin_enablement(text)["enabled"], ["omh"])
                self.assertEqual(plugin_enablement_shape_error(text), "")

    def test_reader_does_not_attribute_items_under_a_scalar(self) -> None:
        self.assertFalse(plugin_is_enabled(BROKEN_CONFIG, PLUGIN_NAME))
        self.assertFalse(plugin_is_enabled(FOLDED_CONFIG, PLUGIN_NAME))
        self.assertEqual(plugin_enablement(BROKEN_CONFIG), {"enabled": [], "disabled": []})

    def test_shape_error_names_what_hermes_does_with_the_file(self) -> None:
        self.assertIn("cannot parse", plugin_enablement_shape_error(BROKEN_CONFIG))
        self.assertIn("plugins.enabled is the scalar `'[]'`", plugin_enablement_shape_error(BROKEN_CONFIG))
        self.assertIn("folds", plugin_enablement_shape_error(FOLDED_CONFIG))
        self.assertIn(
            "plugins.disabled",
            plugin_enablement_shape_error("plugins:\n  disabled: '[]'\n  - omh\n"),
        )

    def test_shape_error_is_silent_for_every_shape_hermes_reads(self) -> None:
        cases = (
            ENABLED_CONFIG,
            DISABLED_CONFIG,
            "plugins:\n  enabled: '[]'\n",
            "plugins:\n  enabled: []\n",
            "plugins:\n  enabled: null\n  disabled:\n    - browser\n",
            "plugins:\n  enabled: browser\n  entries:\n    omh:\n      allow_tool_override: false\n",
            "plugins:\n  enabled:  # none yet\n    - omh\n",
            "skills:\n  external_dirs:\n    - x\n",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(plugin_enablement_shape_error(text), "")

    def test_writer_normalizes_an_empty_scalar_to_a_block_list(self) -> None:
        for value in ("'[]'", '"[]"', "null", "~"):
            with self.subTest(value=value):
                change = ensure_plugin_enabled(f"plugins:\n  enabled: {value}\n  disabled: []\n", PLUGIN_NAME)
                self.assertTrue(change.changed)
                self.assertEqual(change.message, "normalized empty plugins.enabled to a block list")
                self.assertEqual(change.text, "plugins:\n  enabled:\n    - omh\n  disabled: []\n")
                self.assertTrue(plugin_is_enabled(change.text, PLUGIN_NAME))
                self.assertEqual(plugin_enablement_shape_error(change.text), "")

    def test_writer_follows_the_sibling_indent_when_normalizing(self) -> None:
        change = ensure_plugin_enabled("plugins:\n  enabled: '[]'\n  disabled:\n  - browser\n", PLUGIN_NAME)
        self.assertEqual(change.text, "plugins:\n  enabled:\n  - omh\n  disabled:\n  - browser\n")

    def test_a_trailing_comment_does_not_hide_an_inline_list(self) -> None:
        # YAML reads `[omh] # keep` as the list; the comment rides the key line
        # through the rewrite instead of turning the value into a refusal.
        self.assertEqual(plugin_enablement("plugins:\n  enabled: [omh] # keep\n")["enabled"], ["omh"])
        self.assertEqual(plugin_enablement_shape_error("plugins:\n  enabled: [omh] # keep\n"), "")
        change = ensure_plugin_enabled("plugins:\n  enabled: [] # none yet\n", PLUGIN_NAME)
        self.assertEqual(change.text, "plugins:\n  enabled: # none yet\n    - omh\n")
        self.assertTrue(plugin_is_enabled(change.text, PLUGIN_NAME))
        removed = remove_plugin_enabled("plugins:\n  enabled: [omh, browser] # c\n", PLUGIN_NAME)
        self.assertEqual(removed.text, "plugins:\n  enabled: [browser] # c\n")
        # A quoted item that contains ` #` is not a comment.
        self.assertEqual(plugin_enablement("plugins:\n  enabled: ['a #b', omh]\n")["enabled"], ["a #b", "omh"])

    def test_a_level_item_under_a_plain_scalar_is_a_parse_error(self) -> None:
        # `  - omh` at the key's own indent ends the scalar; YAML refuses it
        # (measured), unlike a deeper item, which folds into the string.
        self.assertIn("cannot parse", plugin_enablement_shape_error("plugins:\n  enabled: browser\n  - omh\n"))
        self.assertIn("folds", plugin_enablement_shape_error(FOLDED_CONFIG))

    def test_duplicate_enabled_keys_are_refused(self) -> None:
        # YAML keeps the last key and the writer would edit the first.
        text = "plugins:\n  enabled:\n    - browser\n  enabled: '[]'\n"
        self.assertIn("duplicate plugins.enabled keys", plugins_enabled_extension_error(text, PLUGIN_NAME))
        with self.assertRaises(ValueError) as caught:
            ensure_plugin_enabled(text, PLUGIN_NAME)
        self.assertEqual(str(caught.exception), plugins_enabled_extension_error(text, PLUGIN_NAME))

    def test_writer_refuses_any_other_scalar_and_names_the_hermes_writer(self) -> None:
        for value in ("browser", "'[browser]'", "'[]' # quoted keeps its comment", "'omh'"):
            text = f"plugins:\n  enabled: {value}\n"
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    ensure_plugin_enabled(text, PLUGIN_NAME)
                self.assertIn("unsupported plugins.enabled shape", str(caught.exception))
                self.assertIn(f"hermes plugins enable {PLUGIN_NAME}", str(caught.exception))

    def test_writer_refuses_a_file_hermes_cannot_read_as_a_list(self) -> None:
        for text in (BROKEN_CONFIG, FOLDED_CONFIG):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as caught:
                    ensure_plugin_enabled(text, PLUGIN_NAME)
                self.assertEqual(str(caught.exception), plugin_enablement_shape_error(text))

    def test_extension_error_is_the_sentence_the_writer_raises(self) -> None:
        # One home for the verdict: doctor reports what setup refused.
        for text in (BROKEN_CONFIG, FOLDED_CONFIG, "plugins:\n  enabled: browser\n", "plugins:\n  enabled: '[browser]'\n"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as caught:
                    ensure_plugin_enabled(text, PLUGIN_NAME)
                self.assertEqual(plugins_enabled_extension_error(text, PLUGIN_NAME), str(caught.exception))
        for text in (ENABLED_CONFIG, "plugins:\n  enabled: '[]'\n", "plugins:\n  enabled: []\n", "skills:\n  external_dirs: []\n", ""):
            with self.subTest(text=text):
                self.assertEqual(plugins_enabled_extension_error(text, PLUGIN_NAME), "")


class SetupWithScalarEnabledTests(unittest.TestCase):
    """The #1825 reproduction, end to end through `omh setup` and `omh doctor`."""

    def test_setup_normalizes_a_quoted_empty_list_and_doctor_stays_clean(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            hermes_home.mkdir()
            config_path = hermes_home / "config.yaml"
            config_path.write_text("plugins:\n  enabled: '[]'\n", encoding="utf-8")
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(hermes_home)]

            status, _stdout, stderr = run_cli(base + ["setup"])
            self.assertEqual(status, 0, stderr)

            config_text = config_path.read_text(encoding="utf-8")
            self.assertNotIn("'[]'", config_text)
            self.assertIn("  enabled:\n    - omh\n", config_text)
            self.assertEqual(plugin_enablement_shape_error(config_text), "")
            self.assertTrue(plugin_is_enabled(config_text, PLUGIN_NAME), config_text)

            paths = resolve_paths(root / ".omh", hermes_home)
            by_name = {c.name: c for c in run_doctor(paths)}
            self.assertTrue(by_name["plugin_enabled_in_hermes"].ok, by_name["plugin_enabled_in_hermes"].message)
            self.assertTrue(by_name["hermes_config_plugins_enabled"].ok, by_name["hermes_config_plugins_enabled"].message)

    def test_setup_fails_closed_on_a_scalar_it_cannot_extend(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            hermes_home.mkdir()
            config_path = hermes_home / "config.yaml"
            before = "plugins:\n  enabled: browser\n"
            config_path.write_text(before, encoding="utf-8")
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(hermes_home)]

            status, _stdout, stderr = run_cli(base + ["setup"])
            self.assertNotEqual(status, 0)
            self.assertIn("unsupported plugins.enabled shape", stderr)
            self.assertEqual(config_path.read_text(encoding="utf-8"), before)

            # Setup stopped before it installed the bundle, so the plugin
            # checks do not run; doctor still has to name the cause.
            paths = resolve_paths(root / ".omh", hermes_home)
            by_name = {c.name: c for c in run_doctor(paths)}
            self.assertNotIn("plugin_enabled_in_hermes", by_name)
            check = by_name["hermes_config_plugins_enabled"]
            self.assertFalse(check.ok)
            self.assertIn("unsupported plugins.enabled shape ('browser')", check.message)
            self.assertIn("YAML block list", check.next_action)

    def test_doctor_reports_the_file_setup_used_to_write(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(hermes_home)]
            status, _stdout, stderr = run_cli(base + ["setup"])
            self.assertEqual(status, 0, stderr)

            config_path = hermes_home / "config.yaml"
            config_path.write_text(BROKEN_CONFIG, encoding="utf-8")

            paths = resolve_paths(root / ".omh", hermes_home)
            check = next((c for c in run_doctor(paths) if c.name == "plugin_enabled_in_hermes"), None)
            self.assertIsNotNone(check)
            self.assertFalse(check.ok)
            self.assertIn("cannot parse", check.message)
            self.assertIn("YAML block list", check.next_action or "")


if __name__ == "__main__":
    unittest.main()

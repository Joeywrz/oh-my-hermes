from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()
from omh.config_adapter import (
    COLLAPSED_DISPLAY_SECTIONS,
    external_dir_registered,
    activate_display_sections,
    activate_omh_skin,
    activate_tui_interface,
    display_sections_selection,
    display_interface_selection,
    ensure_external_dir,
    ensure_omh_skin,
    ensure_tui_interface,
    external_dirs,
    remove_external_dir,
)


class ConfigAdapterTests(unittest.TestCase):
    def test_ensure_tui_interface_handles_supported_config_shapes_idempotently(self) -> None:
        fixtures = (
            ("", "display:\n  interface: tui\n"),
            (
                "display:\n  compact: true\n",
                "display:\n  interface: tui\n  compact: true\n",
            ),
        )
        for original, expected in fixtures:
            with self.subTest(original=original):
                first = ensure_tui_interface(original)
                second = ensure_tui_interface(first.text)

                self.assertTrue(first.changed)
                self.assertEqual(first.text, expected)
                self.assertFalse(second.changed)
                self.assertEqual(second.text, expected)

    def test_ensure_tui_interface_preserves_ambiguous_yaml_shapes(self) -> None:
        fixtures = (
            "display: {interface: cli, compact: true}\n",
            "display.interface: cli\n",
            "display:\n  interface: cli\ndisplay:\n  compact: true\n",
            "display:\n  interface: cli\n  interface: classic\n",
            "display:\n  interface:\n    mode: cli\n",
            "display:\n    interface: classic\n",
            "display :\n  interface: classic\n",
            " display:\n  interface: classic\n",
        )
        for original in fixtures:
            with self.subTest(original=original):
                change = ensure_tui_interface(original)
                self.assertFalse(change.changed)
                self.assertEqual(change.text, original)
                self.assertEqual(display_interface_selection(change.text), "")

    def test_ensure_tui_interface_preserves_explicit_user_preferences(self) -> None:
        for selected in ("cli", "classic", "tui"):
            with self.subTest(selected=selected):
                original = f"display:\n  interface: {selected}\n"
                change = ensure_tui_interface(original)

                self.assertFalse(change.changed)
                self.assertEqual(change.text, original)
                self.assertEqual(display_interface_selection(change.text), selected)

    def test_activation_preserves_quoted_display_keys(self) -> None:
        fixtures = (
            ('"display":\n  interface: cli\n', activate_tui_interface),
            ("'display':\n  skin: default\n", lambda text: activate_omh_skin(text, "omh")),
            ('display:\n  "interface": cli\n', activate_tui_interface),
            ("display:\n  'skin': default\n", lambda text: activate_omh_skin(text, "omh")),
            ('{"display": {"interface": "cli"}}\n', activate_tui_interface),
            ('? "display"\n:\n  interface: cli\n', activate_tui_interface),
            ('"displ\\u0061y":\n  interface: cli\n', activate_tui_interface),
            ('display:\n  "inter\\u0066ace": cli\n', activate_tui_interface),
            ('display:\n  "sk\\u0069n": default\n', lambda text: activate_omh_skin(text, "omh")),
            ('!!str display:\n  interface: cli\n', activate_tui_interface),
            ('display:\n  &key interface: cli\n', activate_tui_interface),
            ('display:\n  !!str skin: default\n', lambda text: activate_omh_skin(text, "omh")),
            ('display:\n  ? !!str interface\n  : cli\n', activate_tui_interface),
            ('display:\n  *interface_key : cli\n', activate_tui_interface),
            ('display:\n  interface: &current cli\ncopy: *current\n', activate_tui_interface),
            ('display:\n  skin: !!str default\n', lambda text: activate_omh_skin(text, "omh")),
            ('current: &current cli\ndisplay:\n  interface: *current\n', activate_tui_interface),
            ('display:\n  - "interface": cli\n', activate_tui_interface),
            ('display:\n  - interface: cli\n', activate_tui_interface),
            ('display:\n  - !!str skin: default\n', lambda text: activate_omh_skin(text, "omh")),
            ('- "display":\n    interface: cli\n', activate_tui_interface),
            ('model\n', activate_tui_interface),
            ('{model: local}\n', activate_tui_interface),
            ('model: local\n---\ndisplay:\n  interface: cli\n', activate_tui_interface),
            ('\ufeffdisplay:\n  interface: cli\n', activate_tui_interface),
            ('\ufeffdisplay:\n  skin: default\n', lambda text: activate_omh_skin(text, "omh")),
            ('display:\n  interface: # choices\n    - cli\n', activate_tui_interface),
            ('display:\n  skin: # theme\n    name: default\n', lambda text: activate_omh_skin(text, "omh")),
            ('"display":\n  sections:\n    tools: expanded\n', activate_display_sections),
            ("'display':\n  sections:\n    tools: expanded\n", activate_display_sections),
            ('display:\n  "sections":\n    tools: expanded\n', activate_display_sections),
            ('{"display": {"sections": {"tools": "expanded"}}}\n', activate_display_sections),
            ('display:\n  &key sections:\n    tools: expanded\n', activate_display_sections),
            ('display:\n  - sections:\n      tools: expanded\n', activate_display_sections),
            ('\ufeffdisplay:\n  sections:\n    tools: expanded\n', activate_display_sections),
            ('display:\n  sections: {}\n', activate_display_sections),
            ('display:\n  sections: {tools: expanded}\n', activate_display_sections),
            ('display:\n  sections: none\n', activate_display_sections),
            ('display:\n  sections: |\n    text\n', activate_display_sections),
            ('display.sections:\n  tools: expanded\n', activate_display_sections),
            ('display:\n  sections:\n    tools:\n      mode: expanded\n', activate_display_sections),
            ('display:\n  sections:\n    tools: a\ndisplay:\n  compact: true\n', activate_display_sections),
            ('display:\n  sections:\n    tools: a\n  sections:\n    thinking: b\n', activate_display_sections),
            ('model: local\n---\ndisplay:\n  sections:\n    tools: expanded\n', activate_display_sections),
        )
        for original, activate in fixtures:
            with self.subTest(original=original):
                change = activate(original)
                self.assertFalse(change.changed)
                self.assertEqual(change.text, original)

    def test_display_defaults_preserve_noncanonical_yaml_syntax(self) -> None:
        fixtures = (
            ('"display":\n  interface: cli\n', ensure_tui_interface),
            ('display:\n  "skin": default\n', lambda text: ensure_omh_skin(text, "omh")),
            ('{"display": {"interface": "cli"}}\n', ensure_tui_interface),
            ('? !!str display\n:\n  interface: cli\n', ensure_tui_interface),
            ('display:\n  interface: &current cli\ncopy: *current\n', ensure_tui_interface),
            ('display:\n  - "interface": cli\n', ensure_tui_interface),
            ('display:\n  - interface: cli\n', ensure_tui_interface),
            ('display:\n  - !!str skin: default\n', lambda text: ensure_omh_skin(text, "omh")),
            ('- "display":\n    interface: cli\n', ensure_tui_interface),
            ('model\n', ensure_tui_interface),
            ('{model: local}\n', ensure_tui_interface),
            ('model: local\n---\ndisplay:\n  interface: cli\n', ensure_tui_interface),
            ('\ufeffdisplay:\n  interface: cli\n', ensure_tui_interface),
            ('\ufeffdisplay:\n  skin: default\n', lambda text: ensure_omh_skin(text, "omh")),
            ('display:\n  interface: # choices\n    - cli\n', ensure_tui_interface),
            ('display:\n  skin: # theme\n    name: default\n', lambda text: ensure_omh_skin(text, "omh")),
        )
        for original, ensure in fixtures:
            with self.subTest(original=original):
                change = ensure(original)
                self.assertFalse(change.changed)
                self.assertEqual(change.text, original)

    def test_external_dirs_treats_bare_yaml_null_as_empty(self) -> None:
        for value in ("null", "Null", "NULL", "~"):
            with self.subTest(value=value):
                self.assertEqual(external_dirs(f"skills:\n  external_dirs: {value}\n"), [])

    def test_ensure_external_dir_creates_empty_config(self) -> None:
        change = ensure_external_dir("", "/tmp/omh/skills")

        self.assertTrue(change.changed)
        self.assertEqual(external_dirs(change.text), ["/tmp/omh/skills"])

    def test_ensure_external_dir_preserves_existing_keys_and_is_idempotent(self) -> None:
        original = "model: test\nskills:\n  disabled:\n    - old\n"
        first = ensure_external_dir(original, "/tmp/omh/skills")
        second = ensure_external_dir(first.text, "/tmp/omh/skills")

        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertIn("model: test", first.text)
        self.assertIn("disabled:", first.text)
        self.assertEqual(external_dirs(first.text), ["/tmp/omh/skills"])

    def test_external_dirs_accepts_yaml_sequence_at_key_indent(self) -> None:
        original = "model: test\nskills:\n  external_dirs:\n  - /tmp/omh/skills\n"

        self.assertEqual(external_dirs(original), ["/tmp/omh/skills"])
        change = ensure_external_dir(original, "/tmp/omh/skills")

        self.assertFalse(change.changed)
        self.assertEqual(change.text, original)

    def test_ensure_external_dir_preserves_key_indent_sequence_style(self) -> None:
        original = "skills:\n  external_dirs:\n  - /keep\n"
        change = ensure_external_dir(original, "/tmp/omh/skills")

        self.assertTrue(change.changed)
        self.assertEqual(external_dirs(change.text), ["/keep", "/tmp/omh/skills"])
        self.assertIn("  - /keep\n  - /tmp/omh/skills\n", change.text)

    def test_remove_external_dir_only_removes_managed_entry(self) -> None:
        original = "skills:\n  external_dirs:\n    - /keep\n    - /tmp/omh/skills\n"
        change = remove_external_dir(original, "/tmp/omh/skills")

        self.assertTrue(change.changed)
        self.assertEqual(external_dirs(change.text), ["/keep"])

    def test_remove_external_dir_from_key_indent_sequence(self) -> None:
        original = "skills:\n  external_dirs:\n  - /keep\n  - /tmp/omh/skills\n"
        change = remove_external_dir(original, "/tmp/omh/skills")

        self.assertTrue(change.changed)
        self.assertEqual(external_dirs(change.text), ["/keep"])
        self.assertIn("  - /keep\n", change.text)
        self.assertNotIn("/tmp/omh/skills", change.text)

    def test_ensure_external_dir_expands_inline_list(self) -> None:
        original = "skills:\n  external_dirs: [/keep]\n"
        change = ensure_external_dir(original, "/tmp/omh/skills")

        self.assertTrue(change.changed)
        self.assertEqual(external_dirs(change.text), ["/keep", "/tmp/omh/skills"])
        self.assertEqual(change.text.count("external_dirs:"), 1)

    def test_ensure_external_dir_expands_empty_inline_list(self) -> None:
        original = "skills:\n  external_dirs: []\n"
        change = ensure_external_dir(original, "/tmp/omh/skills")

        self.assertTrue(change.changed)
        self.assertEqual(external_dirs(change.text), ["/tmp/omh/skills"])
        self.assertEqual(change.text.count("external_dirs:"), 1)

    def test_ensure_external_dir_expands_bare_yaml_null(self) -> None:
        for value in ("null", "Null", "NULL", "~"):
            with self.subTest(value=value):
                original = f"model: test\nskills:\n  disabled:\n    - old\n  external_dirs: {value}\n"
                first = ensure_external_dir(original, "/tmp/omh/skills")
                second = ensure_external_dir(first.text, "/tmp/omh/skills")

                self.assertTrue(first.changed)
                self.assertFalse(second.changed)
                self.assertIn("model: test", first.text)
                self.assertIn("disabled:", first.text)
                self.assertEqual(external_dirs(first.text), ["/tmp/omh/skills"])
                self.assertIn("  external_dirs:\n    - /tmp/omh/skills\n", first.text)
                self.assertNotIn(f"external_dirs: {value}", first.text)

    def test_ensure_external_dir_rejects_unsupported_scalar_shapes(self) -> None:
        for value in ("'null'", '"null"', "null # comment", "~ # comment", "/tmp/omh/skills"):
            with self.subTest(value=value):
                original = f"skills:\n  external_dirs: {value}\n"
                self.assertEqual(external_dirs(original), [])
                with self.assertRaises(ValueError):
                    ensure_external_dir(original, "/tmp/omh/skills")

    def test_mutations_reject_duplicate_block_with_unsupported_inline_scalar(self) -> None:
        original = "skills:\n  external_dirs:\n    - /tmp/omh/skills\n  external_dirs: /bad\n"

        self.assertEqual(external_dirs(original), ["/tmp/omh/skills"])
        with self.assertRaises(ValueError):
            ensure_external_dir(original, "/tmp/omh/skills")
        with self.assertRaises(ValueError):
            remove_external_dir(original, "/tmp/omh/skills")

    def test_mutations_reject_duplicate_external_dirs_keys_even_when_supported(self) -> None:
        originals = [
            "skills:\n  external_dirs:\n    - /tmp/omh/skills\n  external_dirs: null\n",
            "skills:\n  external_dirs:\n    - /tmp/omh/skills\n  external_dirs: []\n",
            "skills:\n  external_dirs: [/tmp/omh/skills]\n  external_dirs:\n    - /keep\n",
        ]
        for original in originals:
            with self.subTest(original=original):
                with self.assertRaises(ValueError):
                    ensure_external_dir(original, "/tmp/omh/skills")
                with self.assertRaises(ValueError):
                    remove_external_dir(original, "/tmp/omh/skills")

    def test_remove_external_dir_from_inline_list(self) -> None:
        original = "skills:\n  external_dirs: [/keep, /tmp/omh/skills]\n"
        change = remove_external_dir(original, "/tmp/omh/skills")

        self.assertTrue(change.changed)
        self.assertEqual(external_dirs(change.text), ["/keep"])
        self.assertEqual(change.text.count("external_dirs:"), 1)

    def test_remove_external_dir_treats_bare_yaml_null_as_absent(self) -> None:
        for value in ("null", "Null", "NULL", "~"):
            with self.subTest(value=value):
                original = f"skills:\n  external_dirs: {value}\n"
                change = remove_external_dir(original, "/tmp/omh/skills")

                self.assertFalse(change.changed)
                self.assertEqual(change.text, original)
                self.assertEqual(external_dirs(change.text), [])

    def test_remove_external_dir_rejects_unsupported_scalar_shapes(self) -> None:
        for value in ("'null'", '"null"', "null # comment", "~ # comment", "/tmp/omh/skills"):
            with self.subTest(value=value):
                original = f"skills:\n  external_dirs: {value}\n"
                self.assertEqual(external_dirs(original), [])
                with self.assertRaises(ValueError):
                    remove_external_dir(original, "/tmp/omh/skills")


if __name__ == "__main__":
    unittest.main()


class ExternalDirRegistrationTests(unittest.TestCase):
    """The installer registers `current/skills`; a running command knows only
    its generation directory. Measured live 2026-09-08: doctor and probe
    compared the two as strings and called every staged-update install
    unregistered."""

    def test_the_generation_directory_is_registered_through_the_current_pointer(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            generation = root / "generations" / "g1"
            (generation / "skills").mkdir(parents=True)
            os.symlink(generation, root / "current", target_is_directory=True)
            dirs = [(root / "current" / "skills").as_posix()]
            self.assertTrue(external_dir_registered(dirs, generation / "skills"))
            self.assertTrue(external_dir_registered(dirs, root / "current" / "skills"))
            self.assertFalse(external_dir_registered(dirs, root / "generations" / "g2" / "skills"))
            self.assertFalse(external_dir_registered([], generation / "skills"))

    def test_a_textual_match_needs_no_directory_on_disk(self) -> None:
        self.assertTrue(external_dir_registered(["/nowhere/skills"], "/nowhere/skills"))
        self.assertFalse(external_dir_registered(["/nowhere/skills"], "/elsewhere/skills"))


class DisplaySectionsConsentTests(unittest.TestCase):
    """#1700: the third key of the branded-TUI bundle.

    Hermes renders thinking and tool sections expanded by default, so on a
    long run the prompt scrolls out of sight. Collapsing them is a display
    choice, which puts it inside the one `CONTEXT.md` exception to "OMH
    reports and stops" -- and therefore under that exception's rules.

    Narrower than the two scalars in the bundle in exactly one way, pinned
    below: consent lets those replace a stock canonical value, while every
    section key here is unset-only, because Hermes' default and an explicit
    `expanded` look the same in behavior and differ in provenance.
    """

    def test_a_fresh_canonical_config_gains_all_three_keys(self) -> None:
        change = activate_display_sections("")
        self.assertTrue(change.changed)
        self.assertEqual(
            change.text,
            "display:\n  sections:\n    thinking: collapsed\n    tools: collapsed\n"
            "    subagents: collapsed\n",
        )
        self.assertEqual(
            display_sections_selection(change.text),
            {key: value for key, value in COLLAPSED_DISPLAY_SECTIONS},
        )

    def test_an_existing_display_block_keeps_its_other_keys(self) -> None:
        change = activate_display_sections("display:\n  interface: tui\n  compact: true\n")
        self.assertTrue(change.changed)
        self.assertIn("  interface: tui\n", change.text)
        self.assertIn("  compact: true\n", change.text)
        self.assertIn("  sections:\n    thinking: collapsed\n", change.text)

    def test_a_config_without_a_display_block_gets_one_appended(self) -> None:
        change = activate_display_sections("model: local\n")
        self.assertTrue(change.changed)
        self.assertTrue(change.text.startswith("model: local\n"))
        self.assertIn("display:\n  sections:\n", change.text)

    def test_an_explicit_user_value_is_preserved_per_key(self) -> None:
        change = activate_display_sections("display:\n  sections:\n    tools: expanded\n")
        self.assertTrue(change.changed)
        self.assertIn("    tools: expanded\n", change.text)
        self.assertEqual(
            display_sections_selection(change.text),
            {"thinking": "collapsed", "subagents": "collapsed", "tools": "expanded"},
        )
        self.assertIn("kept user value(s) for tools", change.message)

    def test_every_key_already_set_is_left_byte_identical(self) -> None:
        # The owner machine's shape: all three present and all three
        # `expanded`. The refusal must not report them as collapsed.
        original = (
            "display:\n  sections:\n    thinking: expanded\n    tools: expanded\n"
            "    subagents: expanded\n"
        )
        change = activate_display_sections(original)
        self.assertFalse(change.changed)
        self.assertEqual(change.text, original)
        self.assertNotIn("collapsed", change.message)
        self.assertIn("leaving those values to the user", change.message)

    def test_it_is_idempotent(self) -> None:
        first = activate_display_sections("display:\n  interface: tui\n")
        second = activate_display_sections(first.text)
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(second.text, first.text)

    def test_a_comment_between_display_keys_survives(self) -> None:
        original = "display:\n  interface: tui\n  # keep this note\n  skin: omh\n"
        change = activate_display_sections(original)
        self.assertTrue(change.changed)
        self.assertIn("  # keep this note\n  skin: omh\n", change.text)

    def test_a_comment_inside_the_sections_block_survives(self) -> None:
        original = "display:\n  sections:\n    # tools stay open on purpose\n    tools: expanded\n"
        change = activate_display_sections(original)
        self.assertTrue(change.changed)
        self.assertIn("    # tools stay open on purpose\n    tools: expanded\n", change.text)

    def test_a_noncanonical_sections_shape_is_refused_and_reported(self) -> None:
        for label, original, expected in (
            ("flow empty", "display:\n  sections: {}\n", "non-block"),
            ("flow mapping", "display:\n  sections: {tools: expanded}\n", "non-block"),
            ("block scalar", "display:\n  sections: |\n    text\n", "non-block"),
            ("dotted", "display.sections:\n  tools: expanded\n", "dotted"),
            (
                "deeper nesting",
                "display:\n  sections:\n    tools:\n      mode: expanded\n",
                "deeper",
            ),
            (
                "duplicate sections keys",
                "display:\n  sections:\n    tools: a\n  sections:\n    thinking: b\n",
                "duplicate",
            ),
        ):
            with self.subTest(label=label):
                change = activate_display_sections(original)
                self.assertFalse(change.changed)
                self.assertEqual(change.text, original)
                self.assertIn(expected, change.message)
                self.assertEqual(display_sections_selection(original), {})

    def test_an_unknown_section_key_is_left_alone(self) -> None:
        # OMH owns three names. Anything else in that mapping is the person's.
        change = activate_display_sections("display:\n  sections:\n    citations: expanded\n")
        self.assertTrue(change.changed)
        self.assertIn("    citations: expanded\n", change.text)
        self.assertEqual(display_sections_selection(change.text)["citations"], "expanded")

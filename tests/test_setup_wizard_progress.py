"""`[k/n]` framing for the interactive setup wizard's question phase.

The apply phase has narrated itself since it shipped (`[1/5] Installing OMH
workflows...`); the question phase that runs before it printed a header and
then fired unrelated questions back to back. These tests cover the framing and,
more importantly, the number it carries: `n` is a claim about how many
questions this machine is about to ask, so every case here compares the printed
headings against the prompts that actually appeared rather than against a
constant written next to them.

Several groups skip themselves -- a config that is already TUI plus an OMH
skin, no external coding CLI on PATH, nothing to offer as a provider, no
terminal, no `--with-mcp`. A total that counted those would be a lie on most
machines.
"""

from __future__ import annotations

import argparse
import io
import re
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()

from omh.coding.executors import EXTERNAL_CLI_PROFILES  # noqa: E402
from omh.commands import setup as setup_module  # noqa: E402
from omh.commands.language import LANGUAGE_CODES, MESSAGES, tr  # noqa: E402

_HEADING = re.compile(r"^\[(\d+)/(\d+)\] (.+)\.\.\.$")

_ALL_DETECTED = {
    profile: {"binary_present": True, "login_marker": "present"} for profile in EXTERNAL_CLI_PROFILES
}
_NONE_DETECTED = {
    profile: {"binary_present": False, "login_marker": "absent"} for profile in EXTERNAL_CLI_PROFILES
}

# A config the TUI identity question has nothing left to ask about.
_BRANDED_CONFIG = "display:\n  interface: tui\n  skin: omh\n"
# A config that still uses the plain CLI interface, so the question is live.
_PLAIN_CONFIG = "display:\n  interface: cli\n"

_STEP_LABEL_KEYS = (
    "wizard_step_tui_identity",
    "wizard_step_maestro_delegation",
    "wizard_step_provider_entitlements",
    "wizard_step_model_chains",
    "wizard_step_mcp_host",
)


class WizardProgressTests(unittest.TestCase):
    def _paths(self, root: Path):
        args = argparse.Namespace(omh_home=str(root / ".omh"), hermes_home=str(root / ".hermes"), scope=None)
        return setup_module._paths(args)

    def _config(self, paths, text: str) -> None:
        paths.hermes_config_path.parent.mkdir(parents=True, exist_ok=True)
        paths.hermes_config_path.write_text(text, encoding="utf-8")

    def _run_wizard(
        self,
        paths,
        *,
        detected: dict[str, dict[str, object]],
        stdin_tty: bool,
        with_mcp: bool,
        provider_candidates: list[tuple[str, str, str]] | None = None,
    ) -> tuple[argparse.Namespace, list[tuple[int, int, str]], list[str], str]:
        """Drive the wizard with every prompt recorded instead of displayed.

        Returns the namespace, the parsed `[k/n] label` headings, the prompt
        titles the question groups actually raised, and the raw output. Each
        group's opening prompt is answered so that the group stops there, so a
        recorded prompt is one group having spoken.
        """
        args = argparse.Namespace(
            profile=[],
            profile_pack=[],
            default_executor=None,
            with_mcp=with_mcp,
            mcp_host="generic",
        )
        prompts: list[str] = []

        def yes_no(prompt, **_kwargs):
            prompts.append(prompt)
            return False

        def multi_choice(title, *_args, **_kwargs):
            prompts.append(title)
            return [setup_module._PROVIDER_SKIP_CHOICE]

        def single_choice(title, *_args, **_kwargs):
            prompts.append(title)
            return "generic"

        stream = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(setup_module, "_detect_external_cli_profiles", return_value=detected))
            stack.enter_context(patch.object(setup_module, "_stdin_is_tty", return_value=stdin_tty))
            stack.enter_context(patch.object(setup_module, "_ask_yes_no", side_effect=yes_no))
            stack.enter_context(patch.object(setup_module, "_ask_multi_choice", side_effect=multi_choice))
            stack.enter_context(patch.object(setup_module, "_ask_single_choice", side_effect=single_choice))
            if provider_candidates is not None:
                stack.enter_context(
                    patch.object(setup_module, "_provider_candidates", return_value=provider_candidates)
                )
            stack.enter_context(redirect_stdout(stream))
            setup_module._run_setup_wizard(args, paths, "en")
        output = stream.getvalue()
        headings = [
            (int(match.group(1)), int(match.group(2)), match.group(3))
            for match in (_HEADING.match(line) for line in output.splitlines())
            if match
        ]
        return args, headings, prompts, output

    def test_every_group_running_numbers_one_through_five(self) -> None:
        """All five groups ask, so the headings run `[1/5]` to `[5/5]`."""
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._config(paths, _PLAIN_CONFIG)
            args, headings, prompts, _output = self._run_wizard(
                paths, detected=_ALL_DETECTED, stdin_tty=True, with_mcp=True
            )
            self.assertEqual([index for index, _total, _label in headings], [1, 2, 3, 4, 5])
            self.assertEqual({total for _index, total, _label in headings}, {5})
            self.assertEqual(
                [label for _index, _total, label in headings],
                [tr("en", key) for key in _STEP_LABEL_KEYS],
            )
            # The count is the questions, not the groups that might have run:
            # each group stopped at its opening prompt, so one prompt is one
            # group having spoken.
            self.assertEqual(
                prompts,
                [
                    tr("en", "tui_identity_prompt"),
                    tr("en", "maestro_delegation_prompt", clis=", ".join(EXTERNAL_CLI_PROFILES)),
                    tr("en", "provider_select_title"),
                    tr("en", "model_chains_interview_prompt"),
                    tr("en", "mcp_host_title"),
                ],
            )
            self.assertEqual(len(headings), len(prompts))
            self.assertEqual(args.mcp_host, "generic")

    def test_a_group_that_skips_itself_is_not_counted(self) -> None:
        """A branded config and no `--with-mcp` leave three questions, numbered 1..3."""
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._config(paths, _BRANDED_CONFIG)
            args, headings, prompts, output = self._run_wizard(
                paths, detected=_ALL_DETECTED, stdin_tty=True, with_mcp=False
            )
            self.assertEqual([index for index, _total, _label in headings], [1, 2, 3])
            self.assertEqual({total for _index, total, _label in headings}, {3})
            self.assertEqual(
                [label for _index, _total, label in headings],
                [
                    tr("en", "wizard_step_maestro_delegation"),
                    tr("en", "wizard_step_provider_entitlements"),
                    tr("en", "wizard_step_model_chains"),
                ],
            )
            self.assertEqual(len(headings), len(prompts))
            # The two skipped groups left neither a heading nor a question.
            self.assertNotIn(tr("en", "wizard_step_tui_identity"), output)
            self.assertNotIn(tr("en", "wizard_step_mcp_host"), output)
            self.assertNotIn(tr("en", "tui_identity_prompt"), prompts)
            self.assertFalse(hasattr(args, "_omh_tui_choice"))

    def test_nothing_to_ask_prints_no_headings(self) -> None:
        """Every group silent prints nothing, and each skip records what it always did."""
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._config(paths, _BRANDED_CONFIG)
            args, headings, prompts, output = self._run_wizard(
                paths,
                detected=_NONE_DETECTED,
                stdin_tty=False,
                with_mcp=False,
                provider_candidates=[],
            )
            self.assertEqual(headings, [])
            self.assertEqual(prompts, [])
            self.assertNotIn("[1/", output)
            self.assertFalse(hasattr(args, "_omh_tui_choice"))
            self.assertIsNone(args._maestro_delegation_choice)
            self.assertIsNone(args._provider_entitlements)
            self.assertIsNone(args._model_chains_interview_choice)

    def test_planned_groups_match_the_groups_that_speak(self) -> None:
        """The plan is the predicates; the predicates are what the groups return early on."""
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._config(paths, _BRANDED_CONFIG)
            args = argparse.Namespace(with_mcp=False, mcp_host="generic", profile=[], profile_pack=[], default_executor=None)
            with patch.object(setup_module, "_detect_external_cli_profiles", return_value=_ALL_DETECTED), patch.object(
                setup_module, "_stdin_is_tty", return_value=True
            ):
                planned = setup_module._planned_wizard_questions(args, paths)
            self.assertEqual(
                [question.key for question in planned],
                ["maestro_delegation", "provider_entitlements", "model_chains"],
            )

    def test_question_headings_do_not_pay_the_apply_phase_settle(self) -> None:
        """A heading over a question skips `_brief_tty_pause`, and still renders the same.

        The 40ms settle makes a line readable when more output lands on it
        immediately, which is the apply phase. A question heading is followed
        by a prompt that waits for a person, so it buys nothing -- and
        `_read_tui_key` flushes the input queue on every read (#1778), so
        delay added before a menu's first read is a window in which a
        keypress is silently discarded.
        """
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._config(paths, _PLAIN_CONFIG)
            with patch.object(setup_module._HumanProgress, "_brief_tty_pause") as pause:
                _args, headings, _prompts, _output = self._run_wizard(
                    paths, detected=_ALL_DETECTED, stdin_tty=True, with_mcp=True
                )
            self.assertEqual(len(headings), 5)
            pause.assert_not_called()

        # Not vacuous: the same renderer still settles for an apply-phase step,
        # so this fails if the call is removed from `step` rather than from the
        # wizard's use of it.
        with patch.object(setup_module._HumanProgress, "_brief_tty_pause") as pause:
            setup_module._HumanProgress(enabled=True, use_color=False).step(1, 5, "Installing")
        pause.assert_called_once()

        # Identical line either way: dropping the settle must not become a
        # second visual style for the same heading.
        rendered = []
        for keep_pause in (True, False):
            stream = io.StringIO()
            with patch.object(setup_module._HumanProgress, "_brief_tty_pause"), redirect_stdout(stream):
                setup_module._HumanProgress(enabled=True, use_color=False).step(
                    2, 4, "Choosing how Hermes looks", pause=keep_pause
                )
            rendered.append(stream.getvalue())
        self.assertEqual(rendered[0], rendered[1])
        self.assertEqual(rendered[0], "[2/4] Choosing how Hermes looks...\n")

    def test_group_labels_exist_in_every_language(self) -> None:
        for key in _STEP_LABEL_KEYS:
            for code in LANGUAGE_CODES:
                with self.subTest(key=key, language=code):
                    self.assertIn(key, MESSAGES[code])
                    self.assertTrue(MESSAGES[code][key].strip())

    def test_every_group_label_key_is_a_real_message(self) -> None:
        """Re-derive the label keys from the registry instead of restating them."""
        self.assertEqual(
            tuple(question.label_key for question in setup_module._wizard_question_groups()),
            _STEP_LABEL_KEYS,
        )
        for question in setup_module._wizard_question_groups():
            self.assertIn(question.label_key, MESSAGES["en"])


class NonInteractiveRunTests(unittest.TestCase):
    """`--yes`, `--json`, and a run without a terminal print none of the framing."""

    def _base(self, root: Path) -> list[str]:
        return ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]

    def _assert_no_question_headings(self, stdout: str) -> None:
        for key in _STEP_LABEL_KEYS:
            self.assertNotIn(tr("en", key), stdout)

    def test_yes_run_prints_the_apply_phase_only(self) -> None:
        with TemporaryDirectory() as tmp:
            status, stdout, stderr = run_cli(self._base(Path(tmp)) + ["setup", "--yes"], output_json=False)
            self.assertEqual(status, 0, stderr)
            # The apply phase still narrates itself -- this is about the
            # question phase alone.
            self.assertIn(tr("en", "step_install_skills"), stdout)
            self._assert_no_question_headings(stdout)

    def test_json_run_prints_no_headings(self) -> None:
        with TemporaryDirectory() as tmp:
            status, stdout, stderr = run_cli(self._base(Path(tmp)) + ["setup", "--json"], output_json=True)
            self.assertEqual(status, 0, stderr)
            self._assert_no_question_headings(stdout)

    def test_plain_run_without_a_terminal_prints_no_headings(self) -> None:
        with TemporaryDirectory() as tmp:
            status, stdout, stderr = run_cli(self._base(Path(tmp)) + ["setup"], output_json=False)
            self.assertEqual(status, 0, stderr)
            self._assert_no_question_headings(stdout)


if __name__ == "__main__":
    unittest.main()

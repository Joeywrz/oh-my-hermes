"""An undecidable route carries a typed question, and only where it was wired.

The router's own seam attaches `route_question` beside `candidate_handoff`, so
every surface that reads the enriched route gets it for free. Three surfaces do
not read that route at all -- the awareness payload, `omh_recommend`, and
`omh_context` build their own -- and the tests below pin both halves: the
surfaces that carry the question, and the injected prompt text that must not
change at all.

The answerer ladder is the other half. It reports what could answer the
question on this machine, one tier at a time, and it never reports a tier it
did not read.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()
from omh.plugin_bundle.omh.awareness import (  # noqa: E402
    awareness_route_hint,
    awareness_route_hint_context_from_payload,
)
from omh.plugin_bundle.omh.jev_sidekick import JEV_TOOL_PREFIX  # noqa: E402
from omh.plugin_bundle.omh.route_answerers import (  # noqa: E402
    MAX_LADDER_ITEMS,
    answerer_ladder,
)
from omh.plugin_bundle.omh.tool_bursts import jev_tool_observed_at, record_tool_call  # noqa: E402
from omh.plugin_bundle.omh.tools.chat_tool import omh_interact_handler  # noqa: E402
from omh.routing.chat import public_chat_route_payload, route_chat_message  # noqa: E402
from omh.routing.route_question import (  # noqa: E402
    NO_WORKFLOW_OPTION,
    ROUTE_CHOICE_KEY,
    ROUTE_QUESTION_SCHEMA_VERSION,
)
from omh.wrapper.route_hints import build_chat_route_hint_payload  # noqa: E402

# A script no shipped trigger pack covers, which is what makes the route
# undecidable for `no_trigger_coverage` rather than for a scoring tie. The
# Japanese sentence this suite originally used stopped qualifying when
# `trigger_packs/ja.json` shipped; `tests/test_candidate_handoff.py` records
# that move at its own case.
UNDECIDABLE_MESSAGE = "почему сборка падает на main"
DECIDABLE_MESSAGE = "why is the build failing on main?"


class RouteQuestionOnTheRouteTests(unittest.TestCase):
    def test_an_undecidable_route_carries_the_typed_question(self) -> None:
        route = route_chat_message(UNDECIDABLE_MESSAGE, source="generic", limit=3)
        question = route["route_question"]

        self.assertEqual(question["schema_version"], ROUTE_QUESTION_SCHEMA_VERSION)
        self.assertIn("no_trigger_coverage", question["reasons"])
        self.assertIn(NO_WORKFLOW_OPTION, question["questions"][ROUTE_CHOICE_KEY]["options"])

    def test_a_decidable_route_carries_no_question(self) -> None:
        route = route_chat_message(DECIDABLE_MESSAGE, source="generic", limit=3)

        self.assertEqual(route["action"], "dispatch")
        self.assertNotIn("route_question", route)

    def test_the_question_is_built_from_the_handoff_shortlist(self) -> None:
        """One shortlist, typed twice, never two shortlists that can disagree."""
        route = route_chat_message(UNDECIDABLE_MESSAGE, source="generic", limit=3)
        handoff = route["candidate_handoff"]
        options = route["route_question"]["questions"][ROUTE_CHOICE_KEY]["options"]

        self.assertEqual(route["route_question"]["question_digest"], handoff["digest"])
        self.assertEqual(
            [skill for skill in options if skill != NO_WORKFLOW_OPTION],
            [candidate["skill"] for candidate in handoff["candidates"]],
        )

    def test_every_candidate_gets_its_own_fit_question(self) -> None:
        route = route_chat_message(UNDECIDABLE_MESSAGE, source="generic", limit=3)
        question = route["route_question"]
        skills = [candidate["skill"] for candidate in route["candidate_handoff"]["candidates"]]

        for skill in skills:
            with self.subTest(skill=skill):
                self.assertEqual(question["questions"][f"fits::{skill}"]["type"], "noul")

    def test_the_public_payload_carries_a_copy_and_not_the_cached_dict(self) -> None:
        """The cached route is shared; a public copy that aliased it would let a
        caller mutating its own payload change every later route."""
        first = public_chat_route_payload(UNDECIDABLE_MESSAGE, source="slack")
        first["route_question"]["questions"].clear()
        second = public_chat_route_payload(UNDECIDABLE_MESSAGE, source="slack")

        self.assertIn(ROUTE_CHOICE_KEY, second["route_question"]["questions"])

    def test_the_question_is_reproducible(self) -> None:
        first = route_chat_message(UNDECIDABLE_MESSAGE, source="generic", limit=3)["route_question"]
        second = route_chat_message(UNDECIDABLE_MESSAGE, source="generic", limit=3)["route_question"]

        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))


class RouteHintSurfaceTests(unittest.TestCase):
    def test_the_route_hint_payload_carries_the_question_as_a_top_level_key(self) -> None:
        payload = build_chat_route_hint_payload(UNDECIDABLE_MESSAGE, source="discord")

        self.assertEqual(payload["schema_version"], "chat_route_hint/v1")
        self.assertEqual(
            payload["route_question"]["question_digest"],
            route_chat_message(UNDECIDABLE_MESSAGE, source="discord", limit=2)["route_question"][
                "question_digest"
            ],
        )

    def test_a_decidable_message_carries_the_key_as_none(self) -> None:
        payload = build_chat_route_hint_payload(DECIDABLE_MESSAGE, source="discord")

        self.assertIsNone(payload["route_question"])
        self.assertEqual(payload["route_question_answerers"], [])

    def test_without_homes_the_ladder_is_empty_rather_than_absent(self) -> None:
        """No homes means nothing was read, which is not the same claim as
        "this machine has no answerer"; the question still stands."""
        payload = build_chat_route_hint_payload(UNDECIDABLE_MESSAGE, source="discord")

        self.assertIsNotNone(payload["route_question"])
        self.assertEqual(payload["route_question_answerers"], [])

    def test_with_homes_the_ladder_names_the_model_and_the_unanswered_option(self) -> None:
        from omh.paths import resolve_paths

        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            paths = resolve_paths(root / ".omh", root / ".hermes")
            payload = build_chat_route_hint_payload(
                UNDECIDABLE_MESSAGE, source="discord", paths=paths
            )

        self.assertEqual(
            [rung["answerer"] for rung in payload["route_question_answerers"]],
            ["main_model", "none"],
        )

    def test_an_installed_jev_plugin_reaches_the_surface_as_a_rung(self) -> None:
        """The ladder builder is tested below; this pins that the wiring
        carries its first rung all the way onto the payload, which is the
        half a unit test of the builder cannot prove."""
        from omh.paths import resolve_paths

        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            plugin_dir = root / ".hermes" / "plugins" / "hermes-jev"
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "plugin.yaml").write_text(
                "name: hermes-jev\nprovides_tools:\n  - jev_decide\n", encoding="utf-8"
            )
            paths = resolve_paths(root / ".omh", root / ".hermes")
            payload = build_chat_route_hint_payload(
                UNDECIDABLE_MESSAGE, source="discord", paths=paths
            )

        rungs = payload["route_question_answerers"]
        self.assertEqual([rung["answerer"] for rung in rungs], ["jev_plugin", "main_model", "none"])
        self.assertEqual(rungs[0]["status"], "installed")
        self.assertEqual(rungs[0]["tools"], ["jev_decide"])


class InteractSurfaceTests(unittest.TestCase):
    def test_the_plugin_tool_carries_the_question_and_the_ladder(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patch.dict(
                os.environ,
                {"OMH_HOME": str(root / ".omh"), "HERMES_HOME": str(root / ".hermes")},
            ):
                payload = json.loads(
                    omh_interact_handler(
                        {
                            "message": UNDECIDABLE_MESSAGE,
                            "source": "slack",
                            "record_session": False,
                            "omh_home": str(root / ".omh"),
                            "hermes_home": str(root / ".hermes"),
                        }
                    )
                )

        self.assertEqual(
            payload["route"]["route_question"]["schema_version"], ROUTE_QUESTION_SCHEMA_VERSION
        )
        self.assertEqual(
            [rung["answerer"] for rung in payload["route_question_answerers"]],
            ["main_model", "none"],
        )

    def test_a_decidable_message_gets_no_ladder_on_the_tool_either(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patch.dict(
                os.environ,
                {"OMH_HOME": str(root / ".omh"), "HERMES_HOME": str(root / ".hermes")},
            ):
                payload = json.loads(
                    omh_interact_handler(
                        {
                            "message": DECIDABLE_MESSAGE,
                            "source": "slack",
                            "record_session": False,
                            "omh_home": str(root / ".omh"),
                            "hermes_home": str(root / ".hermes"),
                        }
                    )
                )

        self.assertNotIn("route_question", payload["route"])
        self.assertNotIn("route_question_answerers", payload)


class InjectedTextIsUnchangedTests(unittest.TestCase):
    """OMH injects no rail for this feature, and must not start naming one.

    Two separate claims. The prompt context a turn pays for is byte-identical
    to the awareness text it was always built from, so the new route keys
    reach no injected surface; and no injected text names a third-party tool,
    which is the line `docs/DIRECTION.md` draws around "not an LLM router".
    A rail that told the model to call a `jev_*` tool would have OMH
    recommending the egress `omh doctor` exists to disclose.
    """

    MESSAGES = (UNDECIDABLE_MESSAGE, DECIDABLE_MESSAGE, "PR 리뷰 좀 해줘")

    def test_the_prompt_context_is_the_awareness_text_and_nothing_else(self) -> None:
        for message in self.MESSAGES:
            with self.subTest(message=message):
                payload = build_chat_route_hint_payload(
                    message, source="discord", include_prompt_context=True
                )
                expected = awareness_route_hint_context_from_payload(
                    awareness_route_hint(message, max_hints=2)
                )

                self.assertEqual(payload["prompt_context"], expected)

    def test_no_injected_text_names_a_jev_tool_or_the_new_keys(self) -> None:
        for message in self.MESSAGES:
            with self.subTest(message=message):
                context = awareness_route_hint_context_from_payload(
                    awareness_route_hint(message, max_hints=2)
                )

                self.assertNotIn(JEV_TOOL_PREFIX, context)
                self.assertNotIn("route_question", context)

    def test_the_pre_llm_hook_names_no_jev_tool_on_an_undecidable_turn(self) -> None:
        from omh.plugin_bundle.omh.hooks.llm_hooks import pre_llm_call

        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            hermes = root / ".hermes"
            plugin_dir = hermes / "plugins" / "hermes-jev"
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "plugin.yaml").write_text(
                "name: hermes-jev\nprovides_tools:\n  - jev_decide\n", encoding="utf-8"
            )
            with patch.dict(
                os.environ, {"OMH_HOME": str(root / ".omh"), "HERMES_HOME": str(hermes)}
            ):
                result = pre_llm_call(
                    user_message=UNDECIDABLE_MESSAGE,
                    is_first_turn=False,
                    source="discord",
                    omh_home=str(root / ".omh"),
                    hermes_home=str(hermes),
                )

        injected = json.dumps(result or {}, sort_keys=True, ensure_ascii=False)
        self.assertNotIn(JEV_TOOL_PREFIX, injected)


class AnswererLadderTests(unittest.TestCase):
    """Each tier is the strongest thing OMH could prove, never the best guess."""

    def _homes(self, root: Path) -> tuple[Path, Path]:
        hermes = root / ".hermes"
        (hermes / "plugins").mkdir(parents=True)
        return hermes, root / ".omh"

    def _install_jev_plugin(self, hermes: Path, *, name: str = "hermes-jev", tools: str = "jev_decide") -> Path:
        plugin_dir = hermes / "plugins" / name
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "plugin.yaml").write_text(
            f"name: {name}\nprovides_tools:\n  - {tools}\n", encoding="utf-8"
        )
        return plugin_dir

    def test_a_machine_with_no_plugin_has_two_rungs(self) -> None:
        with TemporaryDirectory() as tmp:
            hermes, omh = self._homes(Path(tmp).resolve())
            ladder = answerer_ladder(hermes, omh)

        self.assertEqual([rung["answerer"] for rung in ladder], ["main_model", "none"])
        self.assertTrue(all(rung["claim_boundary"] for rung in ladder))

    def test_a_non_jev_plugin_is_not_a_jev_plugin(self) -> None:
        with TemporaryDirectory() as tmp:
            hermes, omh = self._homes(Path(tmp).resolve())
            other = hermes / "plugins" / "hermes-achievements"
            other.mkdir()
            (other / "plugin.yaml").write_text(
                "name: hermes-achievements\nprovides_tools:\n  - achievements_show\n",
                encoding="utf-8",
            )
            ladder = answerer_ladder(hermes, omh)

        self.assertEqual([rung["answerer"] for rung in ladder], ["main_model", "none"])

    def test_a_declared_jev_tool_reads_as_installed(self) -> None:
        with TemporaryDirectory() as tmp:
            hermes, omh = self._homes(Path(tmp).resolve())
            self._install_jev_plugin(hermes)
            ladder = answerer_ladder(hermes, omh)

        self.assertEqual(ladder[0]["answerer"], "jev_plugin")
        self.assertEqual(ladder[0]["status"], "installed")
        self.assertEqual(ladder[0]["tools"], ["jev_decide"])
        self.assertEqual(ladder[0]["plugins"], ["hermes-jev"])

    def test_an_enabled_name_reads_as_enabled(self) -> None:
        with TemporaryDirectory() as tmp:
            hermes, omh = self._homes(Path(tmp).resolve())
            self._install_jev_plugin(hermes)
            (hermes / "config.yaml").write_text(
                "plugins:\n  enabled:\n    - omh\n    - hermes-jev\n", encoding="utf-8"
            )
            ladder = answerer_ladder(hermes, omh)

        self.assertEqual(ladder[0]["status"], "enabled")

    def test_a_disabled_name_stays_installed(self) -> None:
        with TemporaryDirectory() as tmp:
            hermes, omh = self._homes(Path(tmp).resolve())
            self._install_jev_plugin(hermes)
            (hermes / "config.yaml").write_text(
                "plugins:\n  enabled:\n    - hermes-jev\n  disabled:\n    - hermes-jev\n",
                encoding="utf-8",
            )
            ladder = answerer_ladder(hermes, omh)

        self.assertEqual(ladder[0]["status"], "installed")

    def test_an_enabled_key_under_another_block_does_not_enable_a_plugin(self) -> None:
        """`enabled:` is a common key. Only the one inside `plugins:` counts."""
        with TemporaryDirectory() as tmp:
            hermes, omh = self._homes(Path(tmp).resolve())
            self._install_jev_plugin(hermes)
            (hermes / "config.yaml").write_text(
                "buzz:\n  enabled:\n    - hermes-jev\nplugins:\n  enabled:\n    - omh\n",
                encoding="utf-8",
            )
            ladder = answerer_ladder(hermes, omh)

        self.assertEqual(ladder[0]["status"], "installed")

    def test_one_observed_jev_call_reads_as_observed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            hermes, omh = self._homes(root)
            self._install_jev_plugin(hermes)
            record_tool_call("jev_decide", omh_home=str(omh))
            ladder = answerer_ladder(hermes, omh)

        self.assertEqual(ladder[0]["status"], "observed")

    def test_an_unrelated_tool_call_does_not_read_as_observed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            hermes, omh = self._homes(root)
            self._install_jev_plugin(hermes)
            record_tool_call("read_file", omh_home=str(omh))
            ladder = answerer_ladder(hermes, omh)

        self.assertEqual(ladder[0]["status"], "installed")
        self.assertEqual(jev_tool_observed_at(str(omh)), 0.0)

    def test_the_observation_outlives_the_entry_ring(self) -> None:
        """A durable scalar, not a scan: 200 unrelated calls must not turn an
        observed plugin back into an unobserved one."""
        from omh.plugin_bundle.omh.tool_bursts import MAX_TOOL_BURST_ENTRIES

        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            hermes, omh = self._homes(root)
            self._install_jev_plugin(hermes)
            record_tool_call("jev_decide", omh_home=str(omh))
            for index in range(MAX_TOOL_BURST_ENTRIES + 5):
                record_tool_call(f"read_file_{index}", omh_home=str(omh))
            ladder = answerer_ladder(hermes, omh)

        self.assertEqual(ladder[0]["status"], "observed")

    def test_a_symlinked_plugin_directory_is_not_read(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            hermes, omh = self._homes(root)
            real = root / "outside"
            real.mkdir()
            (real / "plugin.yaml").write_text(
                "name: hermes-jev\nprovides_tools:\n  - jev_decide\n", encoding="utf-8"
            )
            try:
                (hermes / "plugins" / "hermes-jev").symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("this platform does not allow creating a symlink here")
            ladder = answerer_ladder(hermes, omh)

        self.assertEqual([rung["answerer"] for rung in ladder], ["main_model", "none"])

    def test_the_rung_is_bounded(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            hermes, omh = self._homes(root)
            for index in range(MAX_LADDER_ITEMS + 3):
                self._install_jev_plugin(
                    hermes, name=f"hermes-jev-{index}", tools=f"jev_decide_{index}"
                )
            ladder = answerer_ladder(hermes, omh)

        self.assertEqual(len(ladder[0]["plugins"]), MAX_LADDER_ITEMS)
        self.assertEqual(len(ladder[0]["tools"]), MAX_LADDER_ITEMS)

    def test_the_ladder_writes_nothing_into_either_home(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            hermes, omh = self._homes(root)
            self._install_jev_plugin(hermes)
            before = sorted(path.relative_to(root) for path in root.rglob("*"))
            answerer_ladder(hermes, omh)
            after = sorted(path.relative_to(root) for path in root.rglob("*"))

        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

"""The plugin bundle must import with no ``omh`` package anywhere in sight.

Hermes copies the bundle into ``$HERMES_HOME/plugins/omh`` and loads it with
its own interpreter. The documented installs (`uv tool install`,
`pip install --user`) put the `omh` package in OMH's environment, not Hermes',
so a bundle module that imports `omh.*` at module scope does not merely lose a
feature: Hermes' memory-provider loader execs every top-level file of the
bundle eagerly and keeps the half-initialized module in `sys.modules` when one
raises. The next lazy import of that module then fails on the NAME, with an
`ImportError` carrying the bundle's own dotted path -- which is how one dead
bridge became a logged hook warning on every single tool call (#1623, reported
with a reproduction by @tonalenar).

Two gates, because the fault had two halves. The bundle-wide load is the one
that would have caught it; the cached-stub cases pin the hooks against the
same shape arriving from anywhere else.
"""

from __future__ import annotations

from contextlib import contextmanager
import importlib
import json
import sys
from collections.abc import Iterator
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest

from _local_package import load_local_package
from _standalone_bundle import (
    load_standalone_bundle,
    standalone_bundle_import_failures,
    standalone_bundle_module_names,
)

load_local_package()

BUNDLE_PACKAGE = "omh.plugin_bundle.omh"
BRIDGE_MODULE = f"{BUNDLE_PACKAGE}.agent_board_bridge"


@contextmanager
def cached_stub_bridge() -> Iterator[ModuleType]:
    """Cache a bridge module that has no names, the way a failed exec does.

    Hermes' loader keeps the module object in `sys.modules` when
    `exec_module` raises, and the parent package never gains the attribute,
    so the import resolves to a module whose dict holds nothing the caller
    asked for. Rebuilding that here is the only way to pin the guard: the
    error it must survive is an `ImportError` whose `name` is this bundle's
    own path rather than `omh`.
    """
    package = importlib.import_module(BUNDLE_PACKAGE)
    real_module = sys.modules.get(BRIDGE_MODULE)
    had_attribute = hasattr(package, "agent_board_bridge")
    real_attribute = getattr(package, "agent_board_bridge", None)
    stub = ModuleType(BRIDGE_MODULE)
    sys.modules[BRIDGE_MODULE] = stub
    if had_attribute:
        delattr(package, "agent_board_bridge")
    try:
        yield stub
    finally:
        if real_module is None:
            sys.modules.pop(BRIDGE_MODULE, None)
        else:
            sys.modules[BRIDGE_MODULE] = real_module
        if had_attribute:
            setattr(package, "agent_board_bridge", real_attribute)


class StandaloneBundleImportTests(unittest.TestCase):
    def test_every_bundle_module_imports_without_the_omh_package(self) -> None:
        failures = standalone_bundle_import_failures()
        self.assertEqual(
            failures,
            {},
            "every module under src/plugin_bundle/omh/ must import on a host with no "
            "`omh` package, because Hermes execs all of them and keeps a module that "
            f"raised: {json.dumps(failures, indent=2, sort_keys=True)}\n"
            "Fix the module, do not narrow this gate. Either vendor what it needs into "
            "the bundle, or guard the import (`try: from omh... except ImportError:`) "
            "and have the feature report its absence at its own entry point -- see "
            "`agent_board_bridge._BOARD_CORE_AVAILABLE` and `activity_observer` for "
            "both halves of that shape.",
        )

    def test_the_module_list_is_derived_from_the_directory(self) -> None:
        """A module added to the bundle joins the gate without anyone listing it."""
        names = standalone_bundle_module_names()
        self.assertIn("agent_board_bridge", names)
        self.assertIn("hooks.tool_hooks", names)
        self.assertIn("tools.agent_board_tool", names)
        self.assertNotIn("__init__", names)
        for name in names:
            with self.subTest(name=name):
                self.assertTrue((self._bundle_path(name)).is_file())

    @staticmethod
    def _bundle_path(name: str):
        from _standalone_bundle import _bundle_dir

        direct = _bundle_dir().joinpath(*name.split("."))
        return direct.with_suffix(".py") if direct.with_suffix(".py").is_file() else direct / "__init__.py"


class StandaloneBundleDegradationTests(unittest.TestCase):
    """Importing is half of it; the features must then say they are absent."""

    def test_the_board_bridge_admits_no_action_and_refuses_none(self) -> None:
        bridge = load_standalone_bundle(("agent_board_bridge",))["agent_board_bridge"]
        kanban_call = {
            "tool_name": "kanban_create",
            "args": {"board": "qa"},
            "session_id": "s",
            "task_id": "t",
            "tool_call_id": "c",
        }
        # No engine means nothing here ever prepared a request, so a native
        # Kanban call OMH has no claim over must not be blocked.
        self.assertIsNone(bridge.pre_agent_board(kanban_call))
        self.assertIsNone(bridge.post_agent_board(kanban_call))
        with self.assertRaises(bridge.BoardCoreUnavailable):
            bridge.installed_bridge("qa")
        with self.assertRaises(bridge.BoardCoreUnavailable):
            bridge.installed_status("request-1")

    def test_the_activity_observer_reports_the_missing_store_at_construction(self) -> None:
        modules = load_standalone_bundle(("activity_observer", "native_activity_observer"))
        with self.assertRaises(ImportError):
            modules["activity_observer"].ActivityObserver(None, "sha256:" + "0" * 64)
        with self.assertRaises(ImportError):
            modules["native_activity_observer"].register(object())

    def test_the_agent_board_tool_reports_the_missing_core(self) -> None:
        # The bridge loads in the same pass on purpose: the handler imports it
        # lazily, and an import that happens after the block is lifted would
        # reach the installed package instead of the host being modelled.
        tool = load_standalone_bundle(("agent_board_bridge", "tools.agent_board_tool"))["tools.agent_board_tool"]
        result = json.loads(tool.omh_agent_board_handler({"action": "prepare", "request_id": "r", "board": "qa"}))
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["reason"], "omh_agent_board_core_unavailable")


class CachedStubBridgeTests(unittest.TestCase):
    """The defence in depth: a broken bridge, however it broke."""

    def test_the_tool_call_hooks_return_instead_of_raising(self) -> None:
        from omh.plugin_bundle.omh.hooks.tool_hooks import post_tool_call, pre_tool_call

        call = {"tool_name": "kanban_create", "args": {"board": "qa"}, "session_id": "s",
                "task_id": "t", "tool_call_id": "c"}
        with cached_stub_bridge(), TemporaryDirectory() as home:
            self.assertIsNone(pre_tool_call(omh_home=home, **call))
            self.assertIsNone(post_tool_call(omh_home=home, **call))

    def test_the_agent_board_tool_reports_unavailable_instead_of_raising(self) -> None:
        from omh.plugin_bundle.omh.tools.agent_board_tool import omh_agent_board_handler

        with cached_stub_bridge():
            result = json.loads(omh_agent_board_handler({"action": "status", "request_id": "r"}))
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["reason"], "omh_agent_board_core_unavailable")


if __name__ == "__main__":
    unittest.main()

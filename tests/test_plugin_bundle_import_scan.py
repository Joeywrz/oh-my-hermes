"""`omh doctor` must be able to see a broken bundle on the machine it runs on.

`tests/test_plugin_bundle_standalone.py` (#1669) imports every module of the
bundle in THIS tree with `omh` blocked, which keeps the source from regressing
and cannot reach the copy under `$HERMES_HOME/plugins/omh` on a user's machine.
`plugin_loader_observed` runs on that machine but answers a different question:
it drives Hermes' PLUGIN lane, where a failed exec is dropped from
`sys.modules`, and its verdict is registration equality -- so it returned `ok`
on the host in #1623 whose agent-board bridge was dead and whose every tool
call logged a hook warning.

These tests pin the static tier that closes that gap (#1670), defect first: a
bundle carrying an unguarded module-level `omh.*` import must be named, with
its module and its import, and must move the doctor status.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from _local_package import load_local_package
from _standalone_bundle import bundle_dir

load_local_package()

from omh.install.plugin_bundle_import_scan import (  # noqa: E402 - after load_local_package
    ABSOLUTE_CORE_IMPORT,
    ESCAPING_RELATIVE_IMPORT,
    UNREADABLE_MODULE,
    bundle_module_paths,
    scan_bundle_core_imports,
)
from omh.install.plugin_pack import install_plugin_bundle  # noqa: E402
from omh.maintenance.doctor import (  # noqa: E402
    _plugin_bundle_import_scan_check,
    doctor_ok,
    run_doctor,
)
from omh.paths import resolve_paths  # noqa: E402

# The import the pre-#1669 bundle carried unguarded, verbatim. A synthetic
# `import omh` would prove the rule and not the case.
DEAD_BRIDGE_IMPORT = "from omh.workflows.agent_board import AgentBoard, board_reference"

LOADER_NOT_OBSERVED = {
    "observed": False,
    "ok": False,
    "reason": "hermes_not_installed",
    "registered_tools": [],
    "registered_hooks": [],
}


@contextmanager
def temp_bundle(files: dict[str, str]) -> Iterator[Path]:
    """Write a bundle directory whose contents are exactly ``files``."""
    with TemporaryDirectory(prefix="omh-bundle-scan-") as tmp:
        root = Path(tmp) / "omh"
        for relative, source in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        yield root


class ScanFindsTheDefectTests(unittest.TestCase):
    def test_an_unguarded_core_import_is_named_with_its_module_line_and_statement(self) -> None:
        with temp_bundle(
            {
                "__init__.py": "",
                "agent_board_bridge.py": f'"""Bridge."""\nfrom __future__ import annotations\n\n{DEAD_BRIDGE_IMPORT}\n',
            }
        ) as root:
            payload = scan_bundle_core_imports(root)
        self.assertTrue(payload["scanned"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "unguarded_core_imports")
        self.assertEqual(
            payload["findings"],
            [
                {
                    "module": "agent_board_bridge.py",
                    "line": 4,
                    "kind": ABSOLUTE_CORE_IMPORT,
                    "statement": DEAD_BRIDGE_IMPORT,
                }
            ],
        )

    def test_a_plain_import_statement_counts_the_same_as_a_from_import(self) -> None:
        with temp_bundle({"__init__.py": "", "a.py": "import omh.workflows.agent_board\n"}) as root:
            payload = scan_bundle_core_imports(root)
        self.assertEqual([item["kind"] for item in payload["findings"]], [ABSOLUTE_CORE_IMPORT])

    def test_a_module_whose_name_merely_starts_with_omh_is_not_a_finding(self) -> None:
        with temp_bundle({"__init__.py": "", "a.py": "import omhlib\nfrom omhtools import x\n"}) as root:
            payload = scan_bundle_core_imports(root)
        self.assertEqual(payload["findings"], [])


class GuardedShapesPassTests(unittest.TestCase):
    """The shipped shape must not be flagged, or the check is unusable."""

    GUARDS = {
        "try_import_error": f"try:\n    {DEAD_BRIDGE_IMPORT}\nexcept ImportError:\n    AgentBoard = None\n",
        "try_tuple": f"try:\n    {DEAD_BRIDGE_IMPORT}\nexcept (ImportError, AttributeError):\n    AgentBoard = None\n",
        "try_exception": f"try:\n    {DEAD_BRIDGE_IMPORT}\nexcept Exception:\n    AgentBoard = None\n",
        "try_bare": f"try:\n    {DEAD_BRIDGE_IMPORT}\nexcept:\n    AgentBoard = None\n",
        "nested_fallback": (
            f"try:\n    {DEAD_BRIDGE_IMPORT}\nexcept ImportError:\n"
            "    try:\n        from omh.workflows.agent_board import AgentBoard\n"
            "    except ImportError:\n        AgentBoard = None\n"
        ),
        "type_checking": f"from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    {DEAD_BRIDGE_IMPORT}\n",
        "type_checking_attribute": f"import typing\nif typing.TYPE_CHECKING:\n    {DEAD_BRIDGE_IMPORT}\n",
        "inside_a_function": f"def board():\n    {DEAD_BRIDGE_IMPORT}\n    return AgentBoard\n",
        "inside_a_method": f"class Bridge:\n    def board(self):\n        {DEAD_BRIDGE_IMPORT}\n",
    }

    def test_each_guarded_shape_is_clean(self) -> None:
        for name, source in self.GUARDS.items():
            with self.subTest(guard=name), temp_bundle({"__init__.py": "", "a.py": source}) as root:
                self.assertEqual(scan_bundle_core_imports(root)["findings"], [])

    def test_a_handler_catching_only_module_not_found_does_not_guard(self) -> None:
        """The #1623 lesson, kept as a rule.

        Hermes' loader keeps the module that raised, so the next `from X import
        Y` fails on the NAME with a plain `ImportError`. A handler written for
        `ModuleNotFoundError` alone -- the shape a reader reaches for first,
        since the package really is missing -- lets exactly that through.
        """
        source = f"try:\n    {DEAD_BRIDGE_IMPORT}\nexcept ModuleNotFoundError:\n    AgentBoard = None\n"
        with temp_bundle({"__init__.py": "", "a.py": source}) as root:
            findings = scan_bundle_core_imports(root)["findings"]
        self.assertEqual([item["kind"] for item in findings], [ABSOLUTE_CORE_IMPORT])

    def test_an_import_in_the_handler_itself_is_not_guarded_by_its_own_try(self) -> None:
        source = f"try:\n    pass\nexcept ImportError:\n    {DEAD_BRIDGE_IMPORT}\n"
        with temp_bundle({"__init__.py": "", "a.py": source}) as root:
            findings = scan_bundle_core_imports(root)["findings"]
        self.assertEqual([item["kind"] for item in findings], [ABSOLUTE_CORE_IMPORT])

    def test_a_conditional_that_is_not_type_checking_does_not_guard(self) -> None:
        source = f"import sys\nif sys.version_info >= (3, 12):\n    {DEAD_BRIDGE_IMPORT}\n"
        with temp_bundle({"__init__.py": "", "a.py": source}) as root:
            findings = scan_bundle_core_imports(root)["findings"]
        self.assertEqual([item["kind"] for item in findings], [ABSOLUTE_CORE_IMPORT])


class RelativeImportReachTests(unittest.TestCase):
    """The same defect in the other spelling: an import that leaves the bundle.

    In this repo the bundle sits at `omh.plugin_bundle.omh.*`, where
    `from ...routing import x` resolves to `omh.routing` and works. Installed,
    the bundle is the top-level package and the identical line raises.
    """

    def test_a_relative_import_past_the_bundle_root_is_a_finding(self) -> None:
        with temp_bundle({"__init__.py": "", "a.py": "from ...routing.policy import cue\n"}) as root:
            findings = scan_bundle_core_imports(root)["findings"]
        self.assertEqual([item["kind"] for item in findings], [ESCAPING_RELATIVE_IMPORT])

    def test_relative_imports_inside_the_bundle_are_clean_at_every_depth(self) -> None:
        files = {
            "__init__.py": "from . import a\n",
            "a.py": "from . import b\nfrom .b import thing\n",
            "b.py": "thing = 1\n",
            "hooks/__init__.py": "",
            "hooks/tool_hooks.py": "from .. import b\nfrom ..a import thing\n",
        }
        with temp_bundle(files) as root:
            self.assertEqual(scan_bundle_core_imports(root)["findings"], [])

    def test_the_allowance_grows_with_directory_depth_and_stops_there(self) -> None:
        files = {
            "__init__.py": "",
            "hooks/__init__.py": "",
            "hooks/tool_hooks.py": "from ...routing import cue\n",
        }
        with temp_bundle(files) as root:
            findings = scan_bundle_core_imports(root)["findings"]
        self.assertEqual(
            [(item["module"], item["kind"]) for item in findings],
            [("hooks/tool_hooks.py", ESCAPING_RELATIVE_IMPORT)],
        )


class UnreadableModuleTests(unittest.TestCase):
    def test_a_module_that_cannot_be_parsed_is_reported_rather_than_skipped(self) -> None:
        with temp_bundle({"__init__.py": "", "a.py": "def broken(\n"}) as root:
            findings = scan_bundle_core_imports(root)["findings"]
        self.assertEqual([item["kind"] for item in findings], [UNREADABLE_MODULE])
        self.assertIn("SyntaxError", findings[0]["statement"])

    def test_a_module_that_is_not_utf8_is_reported_rather_than_skipped(self) -> None:
        with temp_bundle({"__init__.py": "", "a.py": ""}) as root:
            (root / "a.py").write_bytes(b"x = '\xff\xfe'\n")
            findings = scan_bundle_core_imports(root)["findings"]
        self.assertEqual([item["kind"] for item in findings], [UNREADABLE_MODULE])


class BundleSubjectTests(unittest.TestCase):
    def test_the_module_list_is_derived_from_the_directory(self) -> None:
        files = {
            "__init__.py": "",
            "a.py": "",
            "hooks/__init__.py": "",
            "hooks/tool_hooks.py": "",
            "plugin.yaml": "name: omh\n",
        }
        with temp_bundle(files) as root:
            names = [path.relative_to(root).as_posix() for path in bundle_module_paths(root)]
        self.assertEqual(names, ["__init__.py", "a.py", "hooks/__init__.py", "hooks/tool_hooks.py"])

    def test_the_shipped_bundle_has_no_unguarded_core_imports(self) -> None:
        """The producer and the runtime gate must agree on the shipped tree.

        The bundle really does import `omh.*` at module level in several
        modules -- `agent_board_bridge`, `activity_observer`,
        `native_activity_observer`, `dispatch_outcomes`, `todo_reconciliation`
        -- each inside a `try`. Flagging those would be a false positive on
        every healthy install, which is the failure mode that would get this
        check ignored.
        """
        payload = scan_bundle_core_imports(bundle_dir())
        self.assertTrue(payload["scanned"])
        self.assertEqual(payload["findings"], [])
        self.assertGreater(payload["module_count"], 1)


class DoctorCheckTests(unittest.TestCase):
    def _check(self, files: dict[str, str]):
        with temp_bundle(files) as root:
            return _plugin_bundle_import_scan_check(scan_bundle_core_imports(root)), root

    def test_the_failing_check_names_the_module_the_import_and_the_remedy(self) -> None:
        check, root = self._check(
            {"__init__.py": "", "agent_board_bridge.py": f"{DEAD_BRIDGE_IMPORT}\n"}
        )
        self.assertFalse(check.ok)
        self.assertEqual(check.severity, "blocking")
        self.assertIn("agent_board_bridge.py", check.message)
        self.assertIn(DEAD_BRIDGE_IMPORT, check.message)
        self.assertIn(str(root), check.message)
        self.assertIn("omh update", check.remediation)
        self.assertIn("omh update", check.next_action)
        self.assertFalse(doctor_ok([check]))

    def test_a_clean_bundle_passes_and_says_what_it_read(self) -> None:
        check, _ = self._check({"__init__.py": "", "a.py": "from . import b\n", "b.py": ""})
        self.assertTrue(check.ok)
        self.assertEqual(check.severity, "ok")
        self.assertTrue(check.observed)
        self.assertIn("3 module(s)", check.message)

    def test_an_absent_bundle_is_not_reported_as_broken(self) -> None:
        payload = scan_bundle_core_imports(Path("/nonexistent-omh-bundle-for-tests"))
        self.assertFalse(payload["scanned"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["reason"], "plugin_bundle_not_installed")
        check = _plugin_bundle_import_scan_check(payload)
        self.assertTrue(check.ok)
        self.assertFalse(check.observed)
        self.assertEqual(check.severity, "warning")
        self.assertTrue(doctor_ok([check]))

    def test_many_findings_are_bounded_without_hiding_the_count(self) -> None:
        files = {"__init__.py": ""}
        for index in range(8):
            files[f"m{index}.py"] = f"{DEAD_BRIDGE_IMPORT}\n"
        check, _ = self._check(files)
        self.assertIn("8 module-level import(s)", check.message)
        self.assertIn("(+3 more)", check.message)


class DoctorWiringTests(unittest.TestCase):
    """The end-to-end case: a real install with one file Hermes would exec.

    The defective module is one nothing else imports, on purpose. That is
    exactly the shape #1623 had -- Hermes' memory-provider lane execs every
    top-level file of the bundle whether or not `__init__` reaches it -- and it
    is why the local import and register tiers pass in the same run while the
    feature is dead.
    """

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.paths = resolve_paths(root / ".omh", root / ".hermes")
        install_plugin_bundle(self.paths)

    def _run_doctor(self) -> dict[str, object]:
        with mock.patch(
            "omh.maintenance.doctor.observe_real_loader_registration",
            return_value=dict(LOADER_NOT_OBSERVED),
        ):
            checks = run_doctor(self.paths)
        by_name = {check.name: check for check in checks}
        self.assertIn(
            "plugin_bundle_standalone_imports",
            sorted(by_name),
            "run_doctor must scan the installed bundle; the source-tree gate cannot reach it",
        )
        return by_name

    def test_a_freshly_installed_bundle_passes_the_scan(self) -> None:
        check = self._run_doctor()["plugin_bundle_standalone_imports"]
        self.assertTrue(check.ok, check.message)

    def test_an_unguarded_import_in_an_unreferenced_file_fails_doctor(self) -> None:
        planted = self.paths.hermes_plugin_dir / "stale_bridge.py"
        planted.write_text(f"{DEAD_BRIDGE_IMPORT}\n", encoding="utf-8")
        checks = self._run_doctor()
        scan_check = checks["plugin_bundle_standalone_imports"]
        self.assertFalse(scan_check.ok, scan_check.message)
        self.assertIn("stale_bridge.py", scan_check.message)
        self.assertIn(DEAD_BRIDGE_IMPORT, scan_check.message)
        self.assertFalse(doctor_ok(list(checks.values())))
        # The contrast IS the finding: the tiers that load the bundle locally
        # still pass, because nothing they do reaches the planted file.
        self.assertTrue(checks["plugin_import_smoke"].ok)
        self.assertTrue(checks["plugin_register_smoke"].ok)


if __name__ == "__main__":
    unittest.main()

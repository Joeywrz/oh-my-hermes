"""The fourth smoke tier: prove the installed plugin DECIDES, not that it loaded.

The tier that matters is the negative one. Every check that existed before
this passed a bundle that loads, registers, and enforces nothing, so the test
that earns its place is the one that neuters the decision seam of a real
installed bundle and watches the load tiers keep passing beside a failing
enforcement tier. Without that contrast the tier is indistinguishable from a
fourth way of asserting `import_smoke`.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()

from omh.install.plugin_pack import (  # noqa: E402
    ENFORCEMENT_PROBE_SCOPED_TOOL,
    ENFORCEMENT_PROBE_UNSCOPED_TOOL,
    inspect_plugin_bundle,
    install_plugin_bundle,
)
from omh.paths import resolve_paths  # noqa: E402
from omh.plugin_bundle.omh.metadata import PROVIDED_TOOLS  # noqa: E402


class InstalledBundle:
    """A managed bundle installed into a temporary Hermes home."""

    def __init__(self, root: str) -> None:
        self.paths = resolve_paths(f"{root}/omh", f"{root}/hermes")
        install_plugin_bundle(self.paths)

    @property
    def rules_module(self) -> Path:
        return self.paths.hermes_plugin_dir / "toolcall_rules.py"

    def inspect(self) -> dict[str, object]:
        return inspect_plugin_bundle(self.paths)


class EnforcementProbeShapeTests(unittest.TestCase):
    def test_the_probe_tools_are_not_tools_any_host_serves(self) -> None:
        # Harmless by construction: the probe cannot be confused for a real
        # call because no such call exists. This is the property the issue
        # asks for in place of "a real destructive command chosen for
        # realism".
        self.assertNotIn(ENFORCEMENT_PROBE_SCOPED_TOOL, PROVIDED_TOOLS)
        self.assertNotIn(ENFORCEMENT_PROBE_UNSCOPED_TOOL, PROVIDED_TOOLS)
        self.assertNotEqual(ENFORCEMENT_PROBE_SCOPED_TOOL, ENFORCEMENT_PROBE_UNSCOPED_TOOL)

    def test_the_probe_leaves_no_rules_file_in_the_inspected_home(self) -> None:
        with TemporaryDirectory() as tmp:
            bundle = InstalledBundle(tmp)
            report = bundle.inspect()
            self.assertEqual(report["plugin_enforcement_status"], "enforced")
            # The probe writes its rules into its own temporary directory,
            # never into the home being inspected.
            self.assertFalse((bundle.paths.omh_home / "rules").exists())


class EnforcementSmokeTierTests(unittest.TestCase):
    def test_a_healthy_installed_bundle_reports_the_decision_it_returned(self) -> None:
        with TemporaryDirectory() as tmp:
            report = InstalledBundle(tmp).inspect()
        self.assertTrue(report["plugin_enforcement_smoke"])
        self.assertEqual(report["plugin_enforcement_status"], "enforced")
        # The decision, not "the call completed".
        self.assertEqual(report["plugin_enforcement_decision"], "scoped=block unscoped=proceed")

    def test_a_bundle_that_decides_nothing_fails_this_tier_and_only_this_tier(self) -> None:
        with TemporaryDirectory() as tmp:
            bundle = InstalledBundle(tmp)
            source = bundle.rules_module.read_text(encoding="utf-8")
            bundle.rules_module.write_text(
                source + "\n\ndef toolcall_rule_directive(**_kwargs):\n    return None\n",
                encoding="utf-8",
            )
            report = bundle.inspect()
        self.assertTrue(report["plugin_import_smoke"], "the load tier must still pass")
        self.assertTrue(report["plugin_register_smoke"], "the register tier must still pass")
        self.assertFalse(report["plugin_enforcement_smoke"])
        self.assertEqual(report["plugin_enforcement_status"], "no_decision")
        self.assertEqual(report["plugin_enforcement_decision"], "scoped=proceed unscoped=proceed")

    def test_a_bundle_that_blocks_indiscriminately_also_fails(self) -> None:
        # A matcher stuck on "block" is not enforcement either: it returns a
        # decision without reading the rule, and one probe could not tell the
        # two apart.
        with TemporaryDirectory() as tmp:
            bundle = InstalledBundle(tmp)
            source = bundle.rules_module.read_text(encoding="utf-8")
            bundle.rules_module.write_text(
                source + '\n\ndef toolcall_rule_directive(**_kwargs):\n    return {"action": "block", "message": "x"}\n',
                encoding="utf-8",
            )
            report = bundle.inspect()
        self.assertEqual(report["plugin_enforcement_status"], "no_decision")
        self.assertEqual(report["plugin_enforcement_decision"], "scoped=block unscoped=block")

    def test_a_bundle_without_the_seam_reports_unknown_rather_than_passing(self) -> None:
        # An installed generation that predates the seam: the module is there
        # and imports, and the entry point is not callable. Shadowed rather
        # than deleted because the bundle's own hooks import the name at
        # module load, so removing it would fail the import tier instead and
        # the enforcement tier would never be reached.
        with TemporaryDirectory() as tmp:
            bundle = InstalledBundle(tmp)
            source = bundle.rules_module.read_text(encoding="utf-8")
            bundle.rules_module.write_text(
                source + "\n\ntoolcall_rule_directive = None\n", encoding="utf-8"
            )
            report = bundle.inspect()
        self.assertTrue(report["plugin_import_smoke"], "the load tier must still pass")
        self.assertFalse(report["plugin_enforcement_smoke"])
        self.assertEqual(report["plugin_enforcement_status"], "unknown")
        self.assertIn("toolcall_rule_directive", str(report["plugin_enforcement_detail"]))

    def test_an_uninstalled_bundle_reports_unknown_not_a_pass(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(f"{tmp}/omh", f"{tmp}/hermes")
            report = inspect_plugin_bundle(paths)
        self.assertFalse(report["plugin_import_smoke"])
        self.assertFalse(report["plugin_enforcement_smoke"])
        self.assertEqual(report["plugin_enforcement_status"], "unknown")


class EnforcementDoctorCheckTests(unittest.TestCase):
    def test_doctor_carries_the_tier_beside_the_load_tiers(self) -> None:
        from omh.maintenance.doctor import _plugin_enforcement_check

        enforced = _plugin_enforcement_check(
            {
                "plugin_enforcement_status": "enforced",
                "plugin_enforcement_decision": "scoped=block unscoped=proceed",
                "plugin_enforcement_detail": "detail",
            }
        )
        self.assertTrue(enforced.ok)
        self.assertIn("scoped=block", enforced.message)

        silent = _plugin_enforcement_check(
            {
                "plugin_enforcement_status": "no_decision",
                "plugin_enforcement_decision": "scoped=proceed unscoped=proceed",
                "plugin_enforcement_detail": "did not decide",
            }
        )
        self.assertFalse(silent.ok)
        self.assertEqual(silent.severity, "blocking")
        self.assertIn("loads and registers but does not enforce", silent.message)

        unknown = _plugin_enforcement_check(
            {
                "plugin_enforcement_status": "unknown",
                "plugin_enforcement_decision": "",
                "plugin_enforcement_detail": "no seam",
            }
        )
        # Not ok: a check that could not tell must not return the
        # safe-looking answer. Warning rather than blocking, because an
        # uninspected tier is a gap, not an observed fault.
        self.assertFalse(unknown.ok)
        self.assertEqual(unknown.severity, "warning")
        self.assertFalse(unknown.observed)


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()

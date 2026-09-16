"""Pairwise profile isolation: the invariant `CONTEXT.md` states and nobody checked.

The three cases the issue asks for, plus the two shapes a path check gets
wrong if nobody pins them: a symlink pair that resolves to one target, and a
nested pair where one home lives inside the other. Both report two different
strings for `path_a` and `path_b`, so a comparison that stopped at string
equality would call each of them isolated.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _cli_harness import run_cli
from _local_package import load_local_package
from _platform_support import requires_symlinks

load_local_package()

from omh.commands.profile_isolation import _profile_isolation_exit_code  # noqa: E402
from omh.maintenance.profile_isolation import (  # noqa: E402
    PROFILE_ISOLATION_SURFACES,
    REASON_ENV_PINS_NEITHER,
    REASON_ENV_PINS_PROFILE,
    REASON_HOME_NOT_SUPPLIED,
    REASON_NESTED_PATH,
    REASON_SAME_PATH,
    VERDICT_ISOLATED,
    VERDICT_LEAKED,
    VERDICT_UNKNOWN,
    compare_profile_isolation,
    parse_profile_ref,
)


def _surface(report: dict, name: str) -> dict:
    return next(row for row in report["surfaces"] if row["surface"] == name)


def _findings(report: dict, surface: str, artifact: str) -> dict:
    return next(row for row in _surface(report, surface)["findings"] if row["artifact"] == artifact)


_NO_ENV: dict[str, str] = {}


class ProfileRefTests(unittest.TestCase):
    def test_a_reference_without_a_hermes_home_leaves_it_unset(self) -> None:
        ref = parse_profile_ref("~/.omh-work")
        self.assertIsNone(ref.hermes_home)
        self.assertEqual(ref.omh_home, Path("~/.omh-work").expanduser())

    def test_a_reference_names_both_homes_when_given_both(self) -> None:
        ref = parse_profile_ref("/tmp/a-omh,/tmp/a-hermes")
        self.assertEqual(ref.omh_home, Path("/tmp/a-omh"))
        self.assertEqual(ref.hermes_home, Path("/tmp/a-hermes"))

    def test_a_malformed_reference_is_refused_rather_than_guessed(self) -> None:
        for text in ("", "   ", ",", "/tmp/a-omh,"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_profile_ref(text)


class ProfileIsolationTests(unittest.TestCase):
    def test_two_properly_isolated_profiles_report_isolated_everywhere(self) -> None:
        with TemporaryDirectory() as tmp:
            report = compare_profile_isolation(
                parse_profile_ref(f"{tmp}/a-omh,{tmp}/a-hermes"),
                parse_profile_ref(f"{tmp}/b-omh,{tmp}/b-hermes"),
                env=_NO_ENV,
            )
        self.assertEqual(report["summary"]["isolated"], len(PROFILE_ISOLATION_SURFACES))
        self.assertEqual(report["leaked_surfaces"], [])
        self.assertEqual(report["unknown_surfaces"], [])
        self.assertEqual(_profile_isolation_exit_code(report), 0)

    def test_a_deliberately_shared_path_reports_leaked_and_names_the_path(self) -> None:
        with TemporaryDirectory() as tmp:
            report = compare_profile_isolation(
                parse_profile_ref(f"{tmp}/a-omh,{tmp}/shared-hermes"),
                parse_profile_ref(f"{tmp}/b-omh,{tmp}/shared-hermes"),
                env=_NO_ENV,
            )
            shared = str(Path(f"{tmp}/shared-hermes/config.yaml").resolve())
        finding = _findings(report, "config", "hermes_config")
        self.assertEqual(finding["verdict"], VERDICT_LEAKED)
        self.assertEqual(finding["reason"], REASON_SAME_PATH)
        self.assertEqual(finding["shared_path"], shared)
        self.assertIn("config", report["leaked_surfaces"])
        self.assertEqual(_profile_isolation_exit_code(report), 1)

    def test_a_surface_the_check_cannot_inspect_reports_unknown_not_isolated(self) -> None:
        with TemporaryDirectory() as tmp:
            report = compare_profile_isolation(
                parse_profile_ref(f"{tmp}/a-omh"),
                parse_profile_ref(f"{tmp}/b-omh"),
                env=_NO_ENV,
            )
        finding = _findings(report, "config", "hermes_config")
        self.assertEqual(finding["verdict"], VERDICT_UNKNOWN)
        self.assertEqual(finding["reason"], REASON_HOME_NOT_SUPPLIED)
        # The OMH-owned half of the same surface was inspected and is clean;
        # the surface still reports unknown, because one uninspected artifact
        # is enough to make "isolated" a claim nobody checked.
        self.assertEqual(_findings(report, "config", "setup_profile")["verdict"], VERDICT_ISOLATED)
        self.assertEqual(_surface(report, "config")["verdict"], VERDICT_UNKNOWN)
        self.assertEqual(_profile_isolation_exit_code(report), 3)

    def test_a_nested_home_leaks_even_though_the_two_paths_differ(self) -> None:
        with TemporaryDirectory() as tmp:
            report = compare_profile_isolation(
                parse_profile_ref(f"{tmp}/outer"),
                parse_profile_ref(f"{tmp}/outer/inner"),
                env=_NO_ENV,
            )
        # The artifact pair is two siblings, neither inside the other; only
        # the home roots show the nesting, which is why they are compared.
        self.assertEqual(_findings(report, "cache", "output_spills")["verdict"], VERDICT_ISOLATED)
        finding = _findings(report, "cache", "omh_home")
        self.assertEqual(finding["verdict"], VERDICT_LEAKED)
        self.assertEqual(finding["reason"], REASON_NESTED_PATH)
        self.assertNotEqual(finding["path_a"], finding["path_b"])
        self.assertEqual(_surface(report, "cache")["verdict"], VERDICT_LEAKED)

    @requires_symlinks
    def test_two_homes_symlinked_to_one_target_leak(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "real-omh"
            target.mkdir()
            link = Path(tmp) / "link-omh"
            link.symlink_to(target, target_is_directory=True)
            report = compare_profile_isolation(
                parse_profile_ref(str(link)),
                parse_profile_ref(str(target)),
                env=_NO_ENV,
            )
        finding = _findings(report, "files", "managed_skills")
        self.assertEqual(finding["verdict"], VERDICT_LEAKED)
        self.assertEqual(finding["reason"], REASON_SAME_PATH)
        self.assertNotEqual(finding["path_a"], finding["path_b"])


class ProfileIsolationEnvTests(unittest.TestCase):
    def test_an_unset_environment_pins_neither_profile(self) -> None:
        with TemporaryDirectory() as tmp:
            report = compare_profile_isolation(
                parse_profile_ref(f"{tmp}/a-omh,{tmp}/a-hermes"),
                parse_profile_ref(f"{tmp}/b-omh,{tmp}/b-hermes"),
                env=_NO_ENV,
            )
        self.assertEqual(_surface(report, "env")["verdict"], VERDICT_ISOLATED)

    def test_an_ambient_variable_pinning_one_profile_reports_leaked(self) -> None:
        with TemporaryDirectory() as tmp:
            report = compare_profile_isolation(
                parse_profile_ref(f"{tmp}/a-omh,{tmp}/a-hermes"),
                parse_profile_ref(f"{tmp}/b-omh,{tmp}/b-hermes"),
                env={"OMH_HOME": f"{tmp}/a-omh"},
            )
        finding = _findings(report, "env", "OMH_HOME")
        self.assertEqual(finding["verdict"], VERDICT_LEAKED)
        self.assertEqual(finding["reason"], REASON_ENV_PINS_PROFILE)
        self.assertEqual(finding["pins"], [f"{tmp}/a-omh,{tmp}/a-hermes"])

    def test_a_variable_pinning_a_third_home_leaves_this_pair_isolated(self) -> None:
        with TemporaryDirectory() as tmp:
            report = compare_profile_isolation(
                parse_profile_ref(f"{tmp}/a-omh,{tmp}/a-hermes"),
                parse_profile_ref(f"{tmp}/b-omh,{tmp}/b-hermes"),
                env={"OMH_HOME": f"{tmp}/c-omh"},
            )
        finding = _findings(report, "env", "OMH_HOME")
        self.assertEqual(finding["verdict"], VERDICT_ISOLATED)
        self.assertEqual(finding["reason"], REASON_ENV_PINS_NEITHER)


class ProfileIsolationCommandTests(unittest.TestCase):
    def test_doctor_runs_the_sub_check_and_leaves_the_checklist_alone(self) -> None:
        previous = {name: os.environ.pop(name, None) for name in ("OMH_HOME", "HERMES_HOME")}
        try:
            with TemporaryDirectory() as tmp:
                status, stdout, _stderr = run_cli(
                    [
                        "doctor",
                        "--profile-isolation",
                        f"{tmp}/a-omh,{tmp}/a-hermes",
                        f"{tmp}/b-omh,{tmp}/b-hermes",
                        "--json",
                    ]
                )
        finally:
            for name, value in previous.items():
                if value is not None:
                    os.environ[name] = value
        payload = json.loads(stdout)
        self.assertEqual(status, 0)
        self.assertEqual(payload["schema_version"], "profile_isolation/v1")
        # The pairwise report replaces the checklist; it never merges into it.
        self.assertNotIn("checks", payload)
        self.assertEqual(len(payload["surfaces"]), len(PROFILE_ISOLATION_SURFACES))

    def test_an_unparsable_reference_is_an_error_not_a_verdict(self) -> None:
        status, _stdout, stderr = run_cli(["doctor", "--profile-isolation", "", "/tmp/b-omh"])
        self.assertEqual(status, 2)
        self.assertIn("OMH home", stderr)


class ProfileIsolationExitCodeTests(unittest.TestCase):
    def test_a_leak_outranks_an_uninspected_surface(self) -> None:
        report = {"leaked_surfaces": ["config"], "unknown_surfaces": ["files"]}
        self.assertEqual(_profile_isolation_exit_code(report), 1)

    def test_a_clean_pair_is_the_only_zero(self) -> None:
        self.assertEqual(_profile_isolation_exit_code({"leaked_surfaces": [], "unknown_surfaces": []}), 0)
        self.assertEqual(_profile_isolation_exit_code({"unknown_surfaces": ["env"]}), 3)


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()

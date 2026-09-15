from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests._cli_harness import run_cli


class BrowserSkillPromotionEntryTests(unittest.TestCase):
    def test_public_status_is_project_local_and_does_not_activate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)

            status, stdout, stderr = run_cli([
                "web-qa", "promotion", "status", "--project-root", str(root),
                "--skill-name", "checkout-confirmation",
            ])

            self.assertEqual(status, 0, stderr)
            result = json.loads(stdout)
            self.assertEqual(result["schema_version"], "browser_skill_promotion/v1")
            self.assertEqual(result["status"], "inactive")
            self.assertFalse((root / ".hermes").exists())
            self.assertFalse((root / ".omh").exists())

    def test_public_status_rejects_traversal_without_state_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)

            status, _, _ = run_cli([
                "web-qa", "promotion", "status", "--project-root", str(root),
                "--skill-name", "../../escape",
            ])

            self.assertNotEqual(status, 0)
            self.assertFalse((root / ".omh").exists())


class SkillPromotionMountTests(unittest.TestCase):
    """#1571: the same seven subcommands, reachable outside web-qa."""

    def test_the_learning_mount_reaches_the_same_lifecycle_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)

            status, stdout, stderr = run_cli([
                "learning", "promotion", "status", "--project-root", str(root),
                "--skill-name", "receipt-reconciliation",
            ])

            self.assertEqual(status, 0, stderr)
            result = json.loads(stdout)
            self.assertEqual(result["schema_version"], "browser_skill_promotion/v1")
            self.assertEqual(result["status"], "inactive")
            self.assertFalse((root / ".hermes").exists())
            self.assertFalse((root / ".omh").exists())

    def test_both_mounts_carry_the_source_id_to_the_same_handler(self) -> None:
        from omh.cli import build_parser

        parser = build_parser()
        web_qa = parser.parse_args([
            "web-qa", "promotion", "diff", "--skill-name", "checkout-confirmation",
            "--trace-id", "bwt-" + "0" * 24,
        ])
        learning = parser.parse_args([
            "learning", "promotion", "diff", "--skill-name", "receipt-reconciliation",
            "--source-id", "sd-" + "0" * 20,
        ])

        self.assertEqual(web_qa.source_id, "bwt-" + "0" * 24)
        self.assertEqual(learning.source_id, "sd-" + "0" * 20)
        self.assertEqual(web_qa.func.__code__, learning.func.__code__)

    def test_an_id_of_neither_shape_is_refused_without_writing_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)

            status, _, stderr = run_cli([
                "learning", "promotion", "diff", "--project-root", str(root),
                "--skill-name", "receipt-reconciliation", "--source-id", "not-a-source",
            ])

            self.assertNotEqual(status, 0)
            self.assertIn("promotion source id is invalid", stderr)
            self.assertFalse((root / ".omh").exists())

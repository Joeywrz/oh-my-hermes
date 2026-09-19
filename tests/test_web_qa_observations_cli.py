from __future__ import annotations

import argparse
import base64
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.commands.web_qa_observations import _read_json_input, add_web_qa_observation_commands
from test_web_qa_observation import good_receipt, qa_plan
from test_web_qa_observation_plan import observation_request


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL1aQAAAABJRU5ErkJggg==")


class WebQaObservationCliTests(unittest.TestCase):
    def setUp(self) -> None:
        # addCleanup (not tearDown): registered immediately so a later
        # exception in this same setUp (e.g. `git init` failing) still tears
        # the directory down (issue #1731).
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.parser = argparse.ArgumentParser()
        parent = self.parser.add_subparsers(dest="top", required=True)
        web_qa = parent.add_parser("web-qa")
        add_web_qa_observation_commands(web_qa.add_subparsers(dest="web_qa", required=True))

    def test_plan_import_show_and_compare_are_attachable_and_json_only(self) -> None:
        raw_plan = observation_request()
        raw_plan["condition"]["routes"] = raw_plan["condition"]["routes"][:1]
        raw_plan["condition"]["viewports"] = raw_plan["condition"]["viewports"][:1]
        raw_plan["condition"]["browsers"] = raw_plan["condition"]["browsers"][:1]
        plan = qa_plan()
        receipt = good_receipt(plan)
        image = self.root / "capture.png"
        image.write_bytes(PNG)
        digest = hashlib.sha256(PNG).hexdigest()
        evidence = receipt["cells"][0]["channels"]["screenshot"]["evidence"]
        evidence["capture_sha256"] = digest
        evidence["byte_size"] = len(PNG)
        evidence["review"]["capture_sha256"] = digest
        plan_path = self._json("plan.json", raw_plan)
        receipt_path = self._json("receipt.json", receipt)

        prepared = self._run("web-qa", "observation", "plan", "--project-root", str(self.root), "--plan-json", str(plan_path))
        self.assertEqual(prepared["completion_state"], "not_found")
        imported = self._run("web-qa", "observation", "import", "--project-root", str(self.root), "--plan-json", str(plan_path), "--receipt-json", str(receipt_path), "--capture", f"{digest}={image}")
        self.assertEqual(imported["observation"]["verdict"], "PASS")
        shown = self._run("web-qa", "observation", "show", "--project-root", str(self.root), "--run-id", plan["run_id"])
        self.assertEqual(shown["plan"]["run_id"], plan["run_id"])
        compared = self._run("web-qa", "observation", "compare", "--project-root", str(self.root), "--baseline-run-id", plan["run_id"], "--candidate-run-id", plan["run_id"])
        self.assertEqual(compared["status"], "comparable")
        self.assertEqual(compared["verdict"], "PASS")

    def test_json_input_rejects_duplicate_keys_symlinks_and_bounded_file_or_stdin(self) -> None:
        bad = self.root / "bad.json"
        bad.write_text('{"mode":"matrix","mode":"matrix"}', encoding="utf-8")
        args = self.parser.parse_args(["web-qa", "observation", "plan", "--project-root", str(self.root), "--plan-json", str(bad)])
        with self.assertRaises(Exception):
            args.func(args)
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["web-qa", "observation", "show", "--project-root", str(self.root), "--run-id", "../../escape"])
        oversized = self.root / "oversized.json"
        oversized.write_bytes(b"x" * 262_145)
        with self.assertRaisesRegex(ValueError, "bounds"):
            _read_json_input(str(oversized))
        linked = self.root / "linked.json"
        linked.symlink_to(bad)
        with self.assertRaisesRegex(ValueError, "symlink"):
            _read_json_input(str(linked))
        stdin = io.TextIOWrapper(io.BytesIO(b"x" * 262_145), encoding="utf-8")
        with patch("sys.stdin", stdin):
            with self.assertRaisesRegex(ValueError, "bounded"):
                _read_json_input("-")

    def _json(self, name: str, value: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _run(self, *argv: str) -> dict[str, object]:
        args = self.parser.parse_args(list(argv))
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(args.func(args), 0)
        return json.loads(stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

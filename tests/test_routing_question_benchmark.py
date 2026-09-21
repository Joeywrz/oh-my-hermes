from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Iterator
import unittest

from _local_package import load_local_package

load_local_package()

ROOT = Path(__file__).resolve().parents[1]
LANE = ROOT / "benchmarks" / "routing-questions" / "v1"
LANE_LIB = LANE / "lib"


@contextmanager
def _lane_import_scope() -> Iterator[None]:
    """Import the lane's modules by bare name without leaking them.

    The lane's libraries import each other off `sys.path`, the way the lane
    itself runs. Every other lane does the same, so the names are saved and
    restored: a shard that ran another lane first must not decide what this one
    imports.
    """
    names = tuple(sorted(path.stem for path in LANE_LIB.glob("*.py")))
    saved = {name: sys.modules.get(name) for name in names}
    original_path = list(sys.path)
    for name in names:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(LANE_LIB))
    try:
        yield
    finally:
        sys.path[:] = original_path
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _item(case_id: str, skill: str = "plan") -> dict[str, object]:
    return {
        "case_id": case_id,
        "corpus": "intervention",
        "message": f"please {skill} this",
        "candidates": [{"skill": skill, "description": f"{skill} workflow"}],
        "question": {
            "schema_version": "route_question/v1",
            "question_digest": f"digest-{case_id}",
            "questions": {
                "route_choice": {
                    "type": "choice",
                    "instructions": "pick one",
                    "options": {skill: f"{skill} workflow", "none": "No OMH workflow applies."},
                },
                f"fits::{skill}": {"type": "noul", "instructions": "does it fit"},
            },
            "thresholds": {"fits_dispatch": 0.8, "fits_clarify": 0.5},
            "reasons": [],
            "claim_boundary": "test",
        },
        "expected": {"action": "dispatch", "choice": skill},
        "deterministic": {"action": "dispatch", "choice": skill, "overrouted": False, "case_passed": True},
    }


class RoutingQuestionLaneArgvTests(unittest.TestCase):
    """The lane talks to the product as an executable, with exact argv."""

    def test_export_argv_calls_the_shipped_export_command(self) -> None:
        with _lane_import_scope():
            harness = _load(LANE_LIB / "harness.py", "harness")
            argv = harness.export_argv("omh", source="discord", limit=3, output=Path("/tmp/corpus.json"))
        self.assertEqual(
            argv,
            [
                "omh",
                "chat",
                "route-questions",
                "export",
                "--source",
                "discord",
                "--limit",
                "3",
                "--output",
                str(Path("/tmp/corpus.json")),
            ],
        )

    def test_score_argv_omits_an_answer_source_that_was_not_given(self) -> None:
        with _lane_import_scope():
            harness = _load(LANE_LIB / "harness.py", "harness")
            bare = harness.score_argv("omh", corpus=Path("/tmp/corpus.json"))
            full = harness.score_argv(
                "omh",
                corpus=Path("/tmp/corpus.json"),
                answers=Path("/tmp/answers.jsonl"),
                output=Path("/tmp/score.json"),
            )
        self.assertEqual(
            bare,
            ["omh", "chat", "route-questions", "score", "--corpus", str(Path("/tmp/corpus.json"))],
        )
        self.assertEqual(full[full.index("--answers") + 1], str(Path("/tmp/answers.jsonl")))
        self.assertEqual(full[full.index("--output") + 1], str(Path("/tmp/score.json")))

    def test_dispatch_argv_uses_the_explicit_child_boundary(self) -> None:
        with _lane_import_scope():
            harness = _load(LANE_LIB / "harness.py", "harness")
            argv = harness.dispatch_argv(
                "omh",
                hermes_executable="/tmp/fake-hermes",
                workspace=Path("/tmp/workspace"),
                model="jev-1.13.0",
                provider="typesafe",
                reasoning="none",
                parent_run_id="parent",
                run_id="child",
                timeout=300,
            )
        self.assertEqual(
            argv,
            [
                "omh",
                "coding",
                "hermes-child",
                "dispatch",
                "--confirm-dispatch",
                "--model",
                "jev-1.13.0",
                "--provider",
                "typesafe",
                "--reasoning",
                "none",
                "--parent-run-id",
                "parent",
                "--run-id",
                "child",
                "--hermes",
                "/tmp/fake-hermes",
                "--cwd",
                str(Path("/tmp/workspace")),
                "--timeout",
                "300",
                "--json",
            ],
        )
        # The prompt never reaches argv; it goes on stdin.
        self.assertNotIn("--prompt", argv)

    def test_current_session_argv_is_labelled_and_not_the_isolated_child(self) -> None:
        with _lane_import_scope():
            harness = _load(LANE_LIB / "harness.py", "harness")
            argv = harness.current_session_argv(
                "/tmp/fake-hermes",
                model="glm-5",
                provider="zai",
                reasoning="high",
                usage_file=Path("/tmp/usage.json"),
                prompt="answer these",
                workspace=Path("/tmp/workspace"),
            )
        self.assertEqual(argv[:5], ["/tmp/fake-hermes", "--oneshot", "answer these", "--in", str(Path("/tmp/workspace"))])
        self.assertEqual(argv[argv.index("--toolsets") + 1], "file")
        self.assertNotIn("hermes-child", argv)
        self.assertNotIn("--confirm-dispatch", argv)

    def test_batch_run_ids_are_unique_per_batch(self) -> None:
        with _lane_import_scope():
            harness = _load(LANE_LIB / "harness.py", "harness")
            ids = [harness.batch_run_id("run", index) for index in range(1, 4)]
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(ids[0], "run-batch-0001")


class RoutingQuestionLaneRefusalTests(unittest.TestCase):
    """Nothing in this lane spends money without being told to, twice."""

    def test_run_batch_refuses_without_the_effect_boundary_confirmation(self) -> None:
        with _lane_import_scope():
            harness = _load(LANE_LIB / "harness.py", "harness")
            with self.assertRaisesRegex(harness.DispatchRefused, "explicit paid-live confirmation"):
                harness.run_batch(
                    [_item("a")],
                    harness="omh",
                    omh_executable="omh",
                    hermes_executable="hermes",
                    workspace=Path("/tmp/workspace"),
                    arm="model",
                    model="glm-5",
                    provider="zai",
                    reasoning="high",
                    parent_run_id="parent",
                    run_id="child",
                    confirmed=False,
                )

    def test_bench_run_refuses_without_every_live_flag(self) -> None:
        with _lane_import_scope():
            bench = _load(LANE / "bench.py", "routing_questions_bench")
            base = ["run", "--arm", "a", "--model", "m", "--provider", "p", "--reasoning", "r"]
            for extra in ([], ["--allow-paid-live"], ["--allow-paid-live", "--max-paid-calls", "1"]):
                with self.assertRaises(SystemExit) as raised:
                    bench.main(base + extra)
                self.assertEqual(raised.exception.code, 2)

    def test_an_offline_subcommand_never_reaches_the_dispatch_path(self) -> None:
        with _lane_import_scope():
            bench = _load(LANE / "bench.py", "routing_questions_bench")
            calls: list[list[str]] = []
            bench._run = lambda argv: calls.append(list(argv)) or 0  # type: ignore[assignment]
            self.assertEqual(bench.main(["deterministic", "--corpus", "/tmp/corpus.json"]), 0)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("hermes-child", calls[0])
        self.assertIn("route-questions", calls[0])


class RoutingQuestionLanePromptTests(unittest.TestCase):
    """One batch is one prompt, and the prompt names the file it must write."""

    def test_the_prompt_carries_the_completion_contract_and_the_answer_file(self) -> None:
        with _lane_import_scope():
            prompts = _load(LANE_LIB / "prompts.py", "prompts")
            prompt = prompts.batch_prompt([_item("case-a"), _item("case-b", "review")], arm="my-arm")
            filename = prompts.ANSWER_FILENAME
        self.assertIn("MANDATORY COMPLETION CONTRACT", prompt)
        self.assertIn(filename, prompt)
        self.assertIn("routing_question_answers/v1", prompt)
        self.assertIn('"arm": "my-arm"', prompt)
        self.assertIn("case-a", prompt)
        self.assertIn("case-b", prompt)
        self.assertIn("item 1/2", prompt)
        self.assertIn("item 2/2", prompt)
        self.assertIn("fits::plan", prompt)
        self.assertIn("fits::review", prompt)
        # The two question kinds are described as what they are.
        self.assertIn("relative choice", prompt)
        self.assertIn("without reference to the others", prompt)

    def test_a_batch_is_refused_rather_than_truncated(self) -> None:
        with _lane_import_scope():
            prompts = _load(LANE_LIB / "prompts.py", "prompts")
            huge = [_item("case-" + str(index)) for index in range(3)]
            huge[0]["message"] = "x" * (prompts.MAX_PROMPT_BYTES + 1)
            with self.assertRaises(prompts.PromptTooLarge):
                prompts.batch_prompt(huge, arm="model")
            with self.assertRaisesRegex(ValueError, "at least one item"):
                prompts.batch_prompt([], arm="model")
            with self.assertRaisesRegex(ValueError, "name the arm"):
                prompts.batch_prompt([_item("a")], arm=" ")

    def test_batches_cover_the_corpus_exactly_once_and_keep_its_order(self) -> None:
        with _lane_import_scope():
            prompts = _load(LANE_LIB / "prompts.py", "prompts")
            items = [_item(f"case-{index}") for index in range(7)]
            batches = prompts.split_batches(items, 3)
            self.assertEqual([len(batch) for batch in batches], [3, 3, 1])
            self.assertEqual(
                [item["case_id"] for batch in batches for item in batch],
                [item["case_id"] for item in items],
            )
            self.assertEqual(prompts.split_batches([], 3), [])
            with self.assertRaisesRegex(ValueError, "positive integer"):
                prompts.split_batches(items, 0)


class RoutingQuestionLaneExternalAnswerTests(unittest.TestCase):
    """The offline pre-flight over an answer file an operator supplied."""

    def _rows(self) -> list[dict[str, object]]:
        return [
            {
                "schema_version": "routing_question_answers/v1",
                "case_id": "case-a",
                "arm": "external",
                "answers": {"route_choice": {"choice": "plan"}, "fits::plan": {"noul": 0.9}},
            }
        ]

    def test_a_well_formed_file_passes_as_either_an_array_or_one_row_a_line(self) -> None:
        with _lane_import_scope():
            external = _load(LANE_LIB / "external_answers.py", "external_answers")
            with TemporaryDirectory() as root:
                array_path = Path(root) / "answers.json"
                array_path.write_text(json.dumps(self._rows()), encoding="utf-8")
                lines_path = Path(root) / "answers.jsonl"
                external.write_jsonl(self._rows(), lines_path)
                array_report = external.validate_answer_file(array_path)
                lines_report = external.validate_answer_file(lines_path)
                written = lines_path.read_text(encoding="utf-8")
        self.assertTrue(array_report["ok"], array_report["problems"])
        self.assertTrue(lines_report["ok"], lines_report["problems"])
        self.assertEqual(lines_report["arms"], ["external"])
        self.assertEqual(lines_report["row_count"], 1)
        self.assertTrue(written.endswith("\n"))
        self.assertEqual(json.loads(written.strip())["case_id"], "case-a")

    def test_a_broken_file_names_every_row_it_could_not_read(self) -> None:
        with _lane_import_scope():
            external = _load(LANE_LIB / "external_answers.py", "external_answers")
            with TemporaryDirectory() as root:
                path = Path(root) / "answers.jsonl"
                rows = self._rows()
                broken = dict(rows[0], schema_version="other/v1")
                no_arm = dict(rows[0], arm="")
                path.write_text(
                    "\n".join(json.dumps(row, sort_keys=True) for row in (rows[0], broken, no_arm))
                    + "\n{not json\n",
                    encoding="utf-8",
                )
                report = external.validate_answer_file(path)
                missing = external.validate_answer_file(Path(root) / "nope.jsonl")
        self.assertFalse(report["ok"])
        reasons = " ".join(problem["reason"] for problem in report["problems"])
        self.assertIn("schema_version is not", reasons)
        self.assertIn("names no arm", reasons)
        self.assertIn("line is not JSON", reasons)
        self.assertFalse(missing["ok"])
        self.assertIn("not readable", missing["problems"][0]["reason"])


class RoutingQuestionLaneContractTests(unittest.TestCase):
    """What the lane declares about itself has to match what it does."""

    def test_the_manifest_declares_the_explicit_child_boundary_and_no_live_default(self) -> None:
        manifest = json.loads((LANE / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], "omh_routing_question_benchmark/v1")
        self.assertTrue(manifest["execution_policy"]["offline_by_default"])
        self.assertTrue(manifest["execution_policy"]["live_requires_confirmation"])
        self.assertTrue(manifest["arms"]["model"]["dispatch_is_explicit"])
        self.assertFalse(manifest["arms"]["deterministic"]["live"])
        self.assertFalse(manifest["corpus"]["cases_are_added_here"])
        with _lane_import_scope():
            prompts = _load(LANE_LIB / "prompts.py", "prompts")
            self.assertEqual(manifest["arms"]["model"]["answer_file"], prompts.ANSWER_FILENAME)

    def test_the_lane_never_imports_the_package_it_measures(self) -> None:
        # The lane measures the installed product through its executable. An
        # `import omh` would silently measure the checkout it happens to sit in.
        for path in sorted(LANE.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("import omh", source, path.name)
            self.assertNotIn("from omh", source, path.name)

    def test_the_lane_ignores_its_own_artifacts(self) -> None:
        ignored = (LANE / ".gitignore").read_text(encoding="utf-8").split()
        self.assertIn("artifacts/", ignored)
        self.assertIn("__pycache__/", ignored)

    def test_the_lane_ships_no_test_directory_of_its_own(self) -> None:
        # A test under benchmarks/ is invisible to the shard inventory, so this
        # file is where the lane's tests live.
        self.assertFalse((LANE / "tests").exists())


if __name__ == "__main__":
    unittest.main()

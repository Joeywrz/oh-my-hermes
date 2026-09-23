from __future__ import annotations

from contextlib import contextmanager, redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Iterator
import unittest

from _local_package import load_local_package

load_local_package()

from omh.quality.routing_question_corpus import (  # noqa: E402
    DETERMINISTIC_ARM,
    ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION,
    ROUTING_QUESTION_CORPUS_SCHEMA_VERSION,
    routing_question_contract,
)
from omh.routing.route_question import (  # noqa: E402
    FIT_QUESTION_PREFIX,
    NO_WORKFLOW_OPTION,
    ROUTE_CHOICE_KEY,
    ROUTE_QUESTION_SCHEMA_VERSION,
)
from omh.commands.main import build_parser  # noqa: E402


def _option_help(parser, command: list[str], option: str) -> str:
    """The help string one subcommand's option carries, read off the parser."""
    target = parser
    for name in command:
        actions = [action for action in target._actions if getattr(action, "choices", None)]
        target = next(action.choices[name] for action in actions if name in (action.choices or {}))
    return next(action.help or "" for action in target._actions if option in action.option_strings)

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
        "message_sha256": f"message-sha-{case_id}",
        "question_source": "candidate_handoff",
        "live_joinable": True,
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
        # argparse writes its refusal to stderr; it is captured so a shard log
        # carries the test's own output and not three parser errors.
        with _lane_import_scope(), redirect_stderr(io.StringIO()) as captured:
            bench = _load(LANE / "bench.py", "routing_questions_bench")
            base = ["run", "--arm", "a", "--model", "m", "--provider", "p", "--reasoning", "r"]
            for extra in ([], ["--allow-paid-live"], ["--allow-paid-live", "--max-paid-calls", "1"]):
                with self.assertRaises(SystemExit) as raised:
                    bench.main(base + extra)
                self.assertEqual(raised.exception.code, 2)
        self.assertIn("--allow-paid-live", captured.getvalue())

    def test_bench_run_refuses_the_arm_name_every_report_computes_itself(self) -> None:
        with _lane_import_scope(), redirect_stderr(io.StringIO()) as captured:
            bench = _load(LANE / "bench.py", "routing_questions_bench")
            argv = [
                "run",
                "--arm",
                DETERMINISTIC_ARM,
                "--model",
                "m",
                "--provider",
                "p",
                "--reasoning",
                "r",
                "--allow-paid-live",
                "--max-paid-calls",
                "1",
                "--confirm",
            ]
            with self.assertRaises(SystemExit) as raised:
                bench.main(argv)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("reserved", captured.getvalue())

    def test_the_current_session_arm_is_pinned_to_its_own_workspace(self) -> None:
        # `--in` alone does not confine this arm: the `file` toolset resolves
        # against the process directory and the inherited TERMINAL_CWD, which
        # is whatever the operator's shell exported. Both are pinned, so a
        # model answering a batch cannot read or write the checkout the lane
        # was launched from.
        calls: list[dict[str, object]] = []
        with _lane_import_scope():
            harness = _load(LANE_LIB / "harness.py", "harness")
            harness.subprocess = SimpleNamespace(
                run=lambda argv, **kwargs: calls.append(kwargs)
                or SimpleNamespace(returncode=0, stdout="", stderr=""),
            )
            with TemporaryDirectory() as root:
                workspace = Path(root) / "batch-0001"
                receipt = harness.run_batch(
                    [_item("a")],
                    harness="hermes_current_session",
                    omh_executable="omh",
                    hermes_executable="hermes",
                    workspace=workspace,
                    arm="model",
                    model="glm-5",
                    provider="zai",
                    reasoning="high",
                    parent_run_id="parent",
                    run_id="batch-0001",
                    confirmed=True,
                )
                self.assertEqual(calls[0]["cwd"], str(workspace))
                self.assertEqual(calls[0]["env"]["TERMINAL_CWD"], str(workspace))
        self.assertEqual(receipt["execution_path"], "hermes_current_session")

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

    def test_the_lanes_copy_of_the_question_contract_matches_the_products(self) -> None:
        # The lane may not import `omh`, so it re-declares the contract it
        # reads and the manifest repeats it again. Nothing compared the copies
        # until here: a changed fit prefix would leave `_question_block`
        # matching no question key, every prompt would say the request produced
        # no candidate, and the whole run would score as maximal dispatch with
        # no test failing.
        contract = routing_question_contract()
        manifest = json.loads((LANE / "manifest.json").read_text(encoding="utf-8"))
        with _lane_import_scope():
            prompts = _load(LANE_LIB / "prompts.py", "prompts")
            external = _load(LANE_LIB / "external_answers.py", "external_answers")
            lane_prompt_literals = (
                prompts.ROUTE_CHOICE_KEY,
                prompts.FIT_QUESTION_PREFIX,
                prompts.NO_WORKFLOW_OPTION,
            )
            lane_answer_literals = (
                external.ANSWERS_SCHEMA_VERSION,
                external.ROUTE_CHOICE_KEY,
                external.RESERVED_ARM,
            )
        self.assertEqual(
            lane_prompt_literals,
            (ROUTE_CHOICE_KEY, FIT_QUESTION_PREFIX, NO_WORKFLOW_OPTION),
        )
        self.assertEqual(
            lane_answer_literals,
            (ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION, ROUTE_CHOICE_KEY, DETERMINISTIC_ARM),
        )
        declared = dict(manifest["question_contract"])
        self.assertEqual(declared.pop("schema_version"), ROUTE_QUESTION_SCHEMA_VERSION)
        self.assertEqual(declared, contract)
        self.assertEqual(manifest["corpus"]["schema_version"], ROUTING_QUESTION_CORPUS_SCHEMA_VERSION)
        self.assertEqual(
            manifest["arms"]["deterministic"]["kind"],
            "omh_router",
        )

    def test_a_model_written_row_is_narrowed_to_the_documented_answer_keys(self) -> None:
        # A model with the `file` toolset can read something in its workspace
        # and echo it into an extra field; `answers.jsonl` is an artifact
        # operators attach to a PR, so only the documented keys are kept.
        row = {
            "schema_version": ROUTING_QUESTION_ANSWERS_SCHEMA_VERSION,
            "case_id": "case-a",
            "arm": "model",
            "answers": {ROUTE_CHOICE_KEY: {"choice": "plan"}},
            "scratch": "whatever the model read in the workspace",
        }
        with _lane_import_scope():
            harness = _load(LANE_LIB / "harness.py", "harness")
            self.assertEqual(sorted(harness.answer_row_subset(row)), ["answers", "arm", "case_id", "schema_version"])
            with TemporaryDirectory() as root:
                workspace = Path(root)
                (workspace / harness.ANSWER_FILENAME).write_text(json.dumps([row]), encoding="utf-8")
                rows, reason = harness.read_answer_file(workspace)
                oversized = workspace / "big"
                oversized.mkdir()
                (oversized / harness.ANSWER_FILENAME).write_bytes(
                    b"x" * (harness.MAX_ANSWER_FILE_BYTES + 1)
                )
                capped_rows, capped_reason = harness.read_answer_file(oversized)
        self.assertEqual(reason, "")
        self.assertNotIn("scratch", rows[0])
        self.assertEqual(rows[0]["answers"], row["answers"])
        self.assertEqual(capped_rows, [])
        self.assertIn("cap", capped_reason)

    def test_answers_are_written_as_each_batch_returns(self) -> None:
        # A run that accumulated rows until the end lost every answer it had
        # already paid for when a later batch raised.
        with _lane_import_scope():
            external = _load(LANE_LIB / "external_answers.py", "external_answers")
            with TemporaryDirectory() as root:
                path = Path(root) / "answers.jsonl"
                external.write_jsonl([], path)
                external.write_jsonl([{"case_id": "a"}], path, append=True)
                external.write_jsonl([{"case_id": "b"}], path, append=True)
                after_append = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                # And: a fresh run truncates, so a partial run is never scored
                # beside the rows a previous run left behind.
                external.write_jsonl([], path)
                after_truncate = path.read_text(encoding="utf-8")
        self.assertEqual([row["case_id"] for row in after_append], ["a", "b"])
        self.assertEqual(after_truncate, "")

    def test_the_export_default_limit_is_pinned_and_documented(self) -> None:
        # The limit decides the shortlist a recommendations-sourced question
        # asks about, so it decides that question's digest. Moving it silently
        # re-cuts every one of them.
        parser = build_parser()
        args = parser.parse_args(["chat", "route-questions", "export"])
        self.assertEqual(args.limit, 3)
        self.assertEqual(args.source, "discord")
        help_text = _option_help(parser, ["chat", "route-questions", "export"], "--limit")
        self.assertIn("candidate handoff", help_text)
        self.assertIn("digest_match", help_text)
        readme = (LANE / "README.md").read_text(encoding="utf-8")
        self.assertIn("--limit 3", readme)
        self.assertIn("digest_match", readme)
        self.assertIn("not_live_joinable", readme)
        self.assertIn("message_sha256", readme)

    def test_the_jev_arm_recipe_is_operator_run_and_hides_the_answer_key(self) -> None:
        # A decision-plugin arm is driven by the operator over the exported
        # corpus. The recipe must say OMH does not call the plugin, and must
        # keep the fields that hold the answer away from the tool answering.
        readme = (LANE / "README.md").read_text(encoding="utf-8")
        section = readme.split("## Producing an external arm", 1)[1].split("\n## ", 1)[0]
        self.assertIn("OMH never calls the plugin", section)
        self.assertIn("never the item's `expected` or `deterministic` fields", section)
        self.assertIn("`question.questions`", section)
        self.assertIn("score --preflight", section)

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

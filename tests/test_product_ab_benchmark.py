"""Contract tests for the `benchmarks/product-ab/v1` lane.

The lane's modules use bare imports of their own `lib/` directory, the way
`benchmarks/live-model-tools/v1` does, so they are loaded here through the
same scoped path-loader: every colliding `sys.modules` entry is popped and
restored, which is what keeps the standard library `statistics` module intact
for the rest of the suite.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
from pathlib import Path
import statistics as standard_statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest

from _local_package import load_local_package

load_local_package()

ROOT = Path(__file__).resolve().parents[1]
LANE = ROOT / "benchmarks" / "product-ab" / "v1"
LANE_LIB = LANE / "lib"
SHARED_LIB = ROOT / "benchmarks" / "live-model-tools" / "v1" / "lib"


@contextmanager
def _lane_import_scope() -> Iterator[None]:
    names = tuple(
        sorted(
            {path.stem for path in LANE_LIB.glob("*.py")}
            | {path.stem for path in SHARED_LIB.glob("*.py")}
        )
    )
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


def _skip_without_history(case: unittest.TestCase) -> None:
    """Skip a test that reads a commit older than HEAD when there is none.

    CI's Linux lanes check out with `fetch-depth: 0` so the whitespace gate can
    resolve a merge base, but the Windows lane takes the action's default,
    which is a depth-1 clone. Every corpus task names a merge base and a merge
    commit from this repository's history, and `git show <commit>:<path>`
    against a shallow clone fails for all of them. That is the checkout's
    shape, not a defect in the lane, so these tests say so and skip rather than
    reporting a failure the platform guarantees.
    """

    shallow = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--is-shallow-repository"],
        capture_output=True,
        text=True,
        check=False,
    )
    if shallow.stdout.strip() != "false":
        case.skipTest("a shallow checkout has no history to read a merge commit from")


def _lane_modules() -> dict[str, ModuleType]:
    with _lane_import_scope():
        import arms  # noqa: PLC0415
        import corpus  # noqa: PLC0415
        import grading  # noqa: PLC0415
        import lane  # noqa: PLC0415
        import report  # noqa: PLC0415
        import repo  # noqa: PLC0415
        import runner  # noqa: PLC0415

        return {
            "arms": arms,
            "corpus": corpus,
            "grading": grading,
            "lane": lane,
            "report": report,
            "repo": repo,
            "runner": runner,
        }


MODULES = _lane_modules()
arms = MODULES["arms"]
corpus = MODULES["corpus"]
grading = MODULES["grading"]
lane = MODULES["lane"]
report = MODULES["report"]
repo_lib = MODULES["repo"]
runner = MODULES["runner"]


def _record(
    arm: str,
    task_id: str,
    *,
    passed: bool,
    claim: str,
    cost: float | None,
    seconds: float,
    tokens: int,
    model: str = "gpt-5.6-sol",
) -> dict:
    return {
        "schema_version": lane.RUN_SCHEMA,
        "arm": arm,
        "task_id": task_id,
        "pull_request": int(task_id.split("-")[1]),
        "corpus_digest": "digest",
        "wall_clock_seconds": seconds,
        "model": {"id": model, "provider": "openai-codex", "effort": "high"},
        "usage": {"total_tokens": tokens, "turns": 3, "tool_calls": 7},
        "cost": {"list_price_usd": cost, "reported_usd": None},
        "verification_gate": {"ran": arm != "hermes", "status": "passed" if passed else "failed"},
        "failure_receipt": None,
        "grade": {
            "pass": passed,
            "reason": "passed" if passed else "target_tests_failed",
            "completion_claim": claim,
            "false_completion": claim == "complete" and not passed,
        },
    }


class LaneBootstrapTests(unittest.TestCase):
    def test_shared_primitives_come_from_the_live_model_tools_lane(self) -> None:
        self.assertEqual(lane.SHARED_LIB, SHARED_LIB)
        self.assertIs(lane.artifact_is_safe, lane.common.artifact_is_safe)
        self.assertIs(lane.exact_mcnemar, lane.statistics.exact_mcnemar)

    def test_loading_the_lane_leaves_the_standard_statistics_module_intact(self) -> None:
        _lane_modules()
        self.assertIs(sys.modules["statistics"], standard_statistics)

    def test_running_this_lane_leaves_the_sibling_lane_byte_identical(self) -> None:
        sibling = SHARED_LIB.parent
        before = lane.tree_digest(sibling)
        with TemporaryDirectory() as root:
            records = Path(root) / "runs.jsonl"
            records.write_text(
                json.dumps(
                    _record("hermes", "PR-1", passed=True, claim="complete", cost=0.1, seconds=1, tokens=1),
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            report.analyze(
                records_path=records,
                manifest=lane.load_object(LANE / "manifest.json"),
                repetitions=100,
            )
        self.assertEqual(lane.tree_digest(sibling), before)


class CorpusSelectionTests(unittest.TestCase):
    def test_only_feat_and_fix_titles_are_tasks(self) -> None:
        for title in ("feat(routing): x", "fix: y", "FIX(a): z", "feat!: w"):
            self.assertIsNotNone(corpus.TITLE_PREFIX.match(title), title)
        for title in ("chore: x", "calibration(deepseek): y", "Deliver seven capabilities"):
            self.assertIsNone(corpus.TITLE_PREFIX.match(title), title)

    def test_generated_and_infrastructure_paths_are_recognized(self) -> None:
        self.assertTrue(corpus._is_generated("skills/ultrawork/SKILL.md"))
        self.assertTrue(corpus._is_generated("docs/WORKFLOWS.md"))
        self.assertTrue(corpus._is_generated("src/plugin_bundle/omh/tools/capability_families.json"))
        self.assertFalse(corpus._is_generated("src/routing/chat.py"))
        self.assertTrue(corpus._is_infrastructure(".github/workflows/ci.yml"))
        self.assertTrue(corpus._is_infrastructure("uv.lock"))
        self.assertFalse(corpus._is_infrastructure("src/routing/chat.py"))

    def test_only_test_modules_count_as_validator_modules(self) -> None:
        self.assertTrue(corpus._is_test_module("tests/test_cli.py"))
        self.assertFalse(corpus._is_test_module("tests/_local_package.py"))
        self.assertFalse(corpus._is_test_module("src/routing/chat.py"))

    def test_pull_request_body_task_text_stops_before_the_solution(self) -> None:
        body = (
            "## Feature Report\n\n### What Changed\n\n- added a flag\n\n"
            "### Why This Exists\n\nThe router sends every long request to the "
            "quick tier, so an exhaustive search loses references.\n\n"
            "### How It Works\n\n- the scorer gains a signal weighted four\n"
        )
        text = corpus.task_text_from_pull_request_body(body)
        self.assertIn("exhaustive search loses references", text)
        self.assertNotIn("scorer gains a signal", text)
        self.assertNotIn("added a flag", text)

    def test_issue_body_keeps_the_problem_and_drops_the_proposal(self) -> None:
        body = "## Problem\n\nThe gate never runs.\n\n## Proposal\n\nAdd `--run-verification`.\n"
        text = corpus.strip_solution_sections(body)
        self.assertIn("The gate never runs", text)
        self.assertNotIn("run-verification", text)

    def test_a_task_text_that_quotes_the_diff_is_a_leak(self) -> None:
        diff = (
            "--- a/src/routing/chat.py\n+++ b/src/routing/chat.py\n"
            '+    if contains_cue_phrase(message, EXHAUSTIVE_SEARCH_PHRASES):\n'
            "+        pass\n"
        )
        leaking = (
            "Add this line to the router: if contains_cue_phrase(message, "
            "EXHAUSTIVE_SEARCH_PHRASES): and it will work."
        )
        self.assertTrue(corpus.leaked_solution_lines(leaking, diff))
        self.assertFalse(corpus.leaked_solution_lines("Route exhaustive search higher.", diff))

    def test_a_short_added_line_is_not_treated_as_a_leak(self) -> None:
        diff = "--- a/src/x.py\n+++ b/src/x.py\n+    return True\n"
        self.assertFalse(corpus.leaked_solution_lines("The function should return True.", diff))

    def test_verification_commands_never_name_the_hidden_validator(self) -> None:
        commands = corpus.verification_commands(["tests/test_router.py"])
        joined = " ".join(commands)
        self.assertIn("compileall", joined)
        self.assertIn("tests/test_router.py", joined)
        self.assertEqual(corpus.verification_commands([]), ["python -m compileall -q src"])

    def test_two_passes_that_agree_on_everything_agree(self) -> None:
        verdict = {"status": "red", "ran": 9, "failures": 1, "errors": 0}
        self.assertTrue(corpus.probe_passes_agree(verdict, dict(verdict)))

    def test_two_passes_red_about_different_things_do_not_agree(self) -> None:
        """The verbatim shapes PR-1502 produced under this probe's two roots.

        Both are red, so a status-only comparison called them a match and kept
        the candidate. They are not the same verdict, and the difference is the
        checkout path, so the counts have to be part of the comparison.
        """

        first = {"status": "red", "ran": 9, "failures": 0, "errors": 2}
        second = {"status": "red", "ran": 9, "failures": 1, "errors": 0}
        self.assertEqual(first["status"], second["status"])
        self.assertFalse(corpus.probe_passes_agree(first, second))

    def test_a_pass_that_ran_a_different_number_of_tests_does_not_agree(self) -> None:
        first = {"status": "red", "ran": 9, "failures": 1, "errors": 0}
        second = {"status": "red", "ran": 8, "failures": 1, "errors": 0}
        self.assertFalse(corpus.probe_passes_agree(first, second))

    def test_the_second_probe_root_is_deeper_than_the_first(self) -> None:
        """The two roots have to differ in length, not only in spelling.

        PR-1502's dependence is on how many characters the path spends, and a
        second root of the same shape as the first agrees by construction.
        """

        self.assertGreaterEqual(len(corpus.SECOND_PASS_NESTING), 2)
        self.assertGreater(len("/".join(corpus.SECOND_PASS_NESTING)), 40)


class SolutionLeakTests(unittest.TestCase):
    """The task must state the problem without prescribing the fix."""

    #: This repository's own pull request template writes the implementation
    #: heading with a qualifier. Pinned by name because an equality test
    #: against `"Implementation"` kept the whole section, and this is the
    #: heading it kept it for.
    TEMPLATE_HEADING = "Implementation (boundary level)"

    def test_a_heading_with_a_qualifier_still_matches(self) -> None:
        self.assertTrue(corpus.heading_matches(self.TEMPLATE_HEADING, "Implementation"))
        self.assertTrue(corpus.heading_matches("Root cause:", "Root cause"))
        self.assertTrue(corpus.heading_matches("**Suggested fix**", "Suggested fix"))

    def test_a_heading_that_merely_starts_with_the_word_does_not_match(self) -> None:
        """The boundary has to be a non-word character, or `Fix` eats `Fixture`."""

        self.assertFalse(corpus.heading_matches("Fixture notes", "Fix"))
        self.assertFalse(
            corpus.heading_matches("Implementations we rejected", "Implementation")
        )

    def test_the_repository_template_heading_cuts_the_solution_half(self) -> None:
        body = (
            "## Feature Report\n\n"
            "### Why This Exists\n\n"
            "The router drops a phrase when the token set is empty, so a caller "
            "sees a clarify where a dispatch belongs.\n\n"
            f"### {self.TEMPLATE_HEADING}\n\n"
            "Add normalized_phrase() to src/routing/chat.py and call it from "
            "recommend().\n"
        )
        text = corpus.task_text_from_pull_request_body(body)
        self.assertIn("router drops a phrase", text)
        self.assertNotIn("normalized_phrase", text)
        self.assertNotIn("src/routing/chat.py", text)

    def test_an_issue_prescribing_the_fix_is_cut_at_its_own_heading(self) -> None:
        for heading in ("Suggested fix", "Proposed fix", "Root cause", "Solution"):
            with self.subTest(heading=heading):
                body = (
                    "The counter never resets between runs.\n\n"
                    f"## {heading}\n\n"
                    "Call reset_counter() from the loop head.\n"
                )
                self.assertNotIn("reset_counter", corpus.strip_solution_sections(body))

    def test_a_hash_inside_a_fenced_block_is_not_a_heading(self) -> None:
        """A `#` in a shell block is a comment, not a section boundary."""

        body = (
            "## Problem\n\nThe run exits zero over a failed batch.\n\n"
            "```sh\n# Validation\nomh coding fanout dispatch\n```\n\n"
            "More of the problem statement, still the problem.\n"
        )
        text = corpus.task_text_from_pull_request_body(body)
        self.assertIn("still the problem", text)

    def test_the_names_a_diff_defines_are_read_off_its_added_lines(self) -> None:
        diff = (
            "+def recall_status(store):\n"
            "+    return store.summary()\n"
            "+LIVE_WINDOW_SECONDS = 900\n"
        )
        self.assertEqual(
            corpus.defined_names(diff), ["LIVE_WINDOW_SECONDS", "recall_status"]
        )

    def test_a_task_text_naming_only_pre_existing_names_is_not_a_leak(self) -> None:
        """The test is absence at the merge base, not mere mention.

        `build` is defined by this diff and named in the text, but it already
        exists under `src/`, so naming it hands the candidate nothing.
        """

        head = repo_lib.resolve(ROOT, "HEAD")
        self.assertEqual(
            corpus.introduced_names_in_task_text(
                ROOT, head, "the build helper misbehaves", "+def build(x):\n"
            ),
            [],
        )

    def test_a_task_text_naming_an_introduced_name_is_a_leak(self) -> None:
        head = repo_lib.resolve(ROOT, "HEAD")
        name = "qqq" + "_absent_from_this_repository_" + "token"
        self.assertEqual(
            corpus.introduced_names_in_task_text(
                ROOT, head, f"it should call {name} instead", f"+def {name}(x):\n"
            ),
            [name],
        )

    def test_a_home_directory_from_a_public_issue_is_redacted(self) -> None:
        """A home path is somebody's username, and the artifact rule misses it.

        `artifact_is_safe` refuses a string that STARTS WITH a home path, so
        one quoted mid-sentence rode into the committed corpus: a real
        contributor's name, republished here as benchmark data.
        """

        for raw in (
            "config at /Users/nashmbp/.hermes/config.yaml; see above",
            "it lives in /home/someone/.config/omh",
            "or C:\\Users\\someone\\AppData",
        ):
            with self.subTest(raw=raw):
                redacted = corpus.redact_home_directories(raw)
                self.assertIn("<home>", redacted)
                for name in ("nashmbp", "someone"):
                    self.assertNotIn(name, redacted)

    def test_redaction_leaves_an_ordinary_repository_path_alone(self) -> None:
        text = "src/routing/chat.py and tests/test_cli.py"
        self.assertEqual(corpus.redact_home_directories(text), text)

    def test_the_leak_class_takes_the_worst_signal_that_fires(self) -> None:
        self.assertEqual(
            corpus.leak_class(
                prescriptive_heading=True, introduced_names=["x"], named_paths=["y"]
            ),
            "heading_prescriptive",
        )
        self.assertEqual(
            corpus.leak_class(
                prescriptive_heading=False, introduced_names=["x"], named_paths=["y"]
            ),
            "names_new_identifier",
        )
        self.assertEqual(
            corpus.leak_class(
                prescriptive_heading=False, introduced_names=[], named_paths=["y"]
            ),
            "names_changed_file",
        )
        self.assertEqual(
            corpus.leak_class(
                prescriptive_heading=False, introduced_names=[], named_paths=[]
            ),
            "clean",
        )
        self.assertEqual(
            corpus.leak_class(
                prescriptive_heading=False,
                introduced_names=[],
                grading_modules=["tests/test_x.py"],
                named_paths=["y"],
            ),
            "names_grading_module",
        )
        self.assertEqual(set(corpus.LEAK_CLASSES), {
            "heading_prescriptive", "names_new_identifier",
            "names_grading_module", "names_changed_file", "clean",
        })

    def test_a_surviving_solution_heading_is_detected(self) -> None:
        """This should never fire, which is exactly why it is checked.

        The cut matches a list of headings, and that list is not the set of
        all headings an author might write.
        """

        self.assertTrue(
            corpus.has_prescriptive_heading("## Problem\n\nx\n\n## How It Works\n\ny\n")
        )
        self.assertFalse(
            corpus.has_prescriptive_heading("## Problem\n\nthe counter never resets\n")
        )

    def test_naming_a_changed_file_is_recorded_and_not_excluded(self) -> None:
        """Pointing at the file that misbehaves is ordinary bug-report content."""

        self.assertEqual(
            corpus.source_paths_in_task_text(
                "src/routing/chat.py returns the wrong tier",
                ["src/routing/chat.py", "src/routing/recommend.py"],
            ),
            ["src/routing/chat.py"],
        )


class PinnedCorpusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = corpus.load(LANE / "corpus" / "evaluation.json")
        self.tasks = list(self.payload["tasks"])

    def test_every_task_carries_a_leak_class_from_the_declared_set(self) -> None:
        for task in self.tasks:
            with self.subTest(task=task["task_id"]):
                self.assertIn(task["leak_class"], corpus.LEAK_CLASSES)

    def test_no_task_text_kept_a_solution_heading(self) -> None:
        """`heading_prescriptive` should be unreachable; the corpus says so.

        If this ever counts above zero, `SOLUTION_HEADINGS` is short a heading
        somebody actually writes, and the count is how that gets discovered.
        """

        self.assertEqual(
            self.payload["selection"]["leak_class"].get("heading_prescriptive", 0), 0
        )

    def test_the_headline_subset_is_large_enough_to_quote(self) -> None:
        """Only issue-sourced tasks may carry "solved N% of our own issues".

        Their text was written before the fix, by someone describing a
        problem. A pull-request body was written after it, by its author, and
        no heading rule removes what a paraphrase leaks.
        """

        issue_sourced = [
            task for task in self.tasks if task["task_source"] == "linked_issue"
        ]
        self.assertGreaterEqual(
            len(issue_sourced),
            25,
            "the headline subset is the number under the most pressure; the "
            "probe takes issue-sourced candidates first for this reason",
        )
        self.assertEqual(
            self.payload["selection"]["task_source"]["linked_issue"],
            len(issue_sourced),
        )

    def test_the_selection_records_what_it_probed_and_why_it_dropped(self) -> None:
        """A corpus size without the probed and rejected counts cannot be judged."""

        selection = self.payload["selection"]
        self.assertGreater(selection["probed"], len(self.tasks))
        self.assertTrue(selection["probe_rejected"])
        self.assertEqual(
            selection["probed"] - sum(selection["probe_rejected"].values()),
            len(self.tasks),
        )

    def test_the_readme_numbers_are_the_corpus_numbers(self) -> None:
        """Counts written into prose drift, and then mislead about their own file.

        Every number in the lane README's composition tables is re-derived
        here from the corpus, so a rebuild that changes the corpus and leaves
        the README alone fails rather than publishing a stale figure.
        """

        readme = (LANE / "README.md").read_text(encoding="utf-8")
        selection = self.payload["selection"]
        expected = {
            "Merged pull requests read, newest first": selection["pull_requests_read"],
            "Candidates probed": selection["probed"],
            "— from a linked issue (the headline subset)":
                selection["task_source"]["linked_issue"],
            "— from a pull request body (secondary)":
                selection["task_source"].get("pull_request_body", 0),
            "Validator not green with the pull request's own fix":
                selection["probe_rejected"].get(
                    "validator_not_green_with_the_reference_fix", 0
                ),
            "Verdict depends on the workspace path":
                selection["probe_rejected"].get("verdict_depends_on_workspace_path", 0),
            "Regression set already red at the merge base":
                selection["probe_rejected"].get(
                    "regression_set_not_green_at_merge_base", 0
                ),
        }
        for label, value in expected.items():
            with self.subTest(row=label):
                self.assertIn(
                    f"| {label} | {value} |",
                    readme,
                    f"the README row {label!r} does not say {value}",
                )
        self.assertIn(f"| **Tasks kept** | **{len(self.tasks)}** |", readme)
        self.assertIn(self.payload["corpus_digest"][:12], readme)

        # The leak-class table sat outside this check, and a reviewer proved it
        # by hand-editing its numbers and watching all 94 tests stay green.
        # It is the table a skeptical reader inspects hardest, so it is the one
        # that most needed pinning.
        overall = selection["leak_class"]
        issue_sourced = selection["leak_class_issue_sourced"]
        for name in corpus.LEAK_CLASSES:
            with self.subTest(leak_class=name):
                self.assertIn(
                    f"| `{name}` | {overall.get(name, 0)} | {issue_sourced.get(name, 0)} |",
                    readme,
                    f"the README leak-class row {name!r} does not match the corpus",
                )

    def test_every_task_records_which_of_its_own_files_it_names(self) -> None:
        """A weaker leak, recorded rather than excluded, so a reader can subset."""

        for task in self.tasks:
            with self.subTest(task=task["task_id"]):
                named = task["task_text_names_source_paths"]
                self.assertIsInstance(named, list)
                self.assertTrue(set(named) <= set(task["source_paths"]))

    def test_every_task_was_proven_green_with_its_own_fix(self) -> None:
        """Red at the merge base is half the proof; this is the other half."""

        for task in self.tasks:
            with self.subTest(task=task["task_id"]):
                probe = task["baseline_probe"]
                self.assertEqual(
                    probe["with_reference_fix"]["status"],
                    "green",
                    "a task whose validator stays red under the change that "
                    "actually shipped is unsolvable here, and would cost a "
                    "call on every arm to prove nothing",
                )
                self.assertEqual(
                    probe["with_reference_fix_regression"]["status"], "green"
                )

    def test_the_pinned_corpus_is_between_thirty_and_forty_tasks(self) -> None:
        self.assertGreaterEqual(len(self.tasks), 30)
        self.assertLessEqual(len(self.tasks), 40)

    def test_the_pinned_digest_covers_the_pinned_tasks(self) -> None:
        self.assertEqual(corpus.corpus_digest(self.tasks), self.payload["corpus_digest"])

    def test_every_task_carries_a_validator_and_a_task_text(self) -> None:
        for task in self.tasks:
            with self.subTest(task=task["task_id"]):
                self.assertTrue(task["test_modules"])
                self.assertTrue(task["source_paths"])
                self.assertGreaterEqual(len(task["task_text"]), corpus.MIN_TASK_TEXT_CHARS)
                self.assertEqual(
                    lane.text_digest(task["task_text"]), task["task_text_sha256"]
                )
                self.assertLessEqual(task["changed_lines"], corpus.MAX_CHANGED_LINES)

    def test_no_task_hands_its_own_validator_to_the_verification_gate(self) -> None:
        for task in self.tasks:
            with self.subTest(task=task["task_id"]):
                joined = " ".join(task["verification_commands"])
                for module in task["test_modules"]:
                    self.assertNotIn(module, joined)

    def test_every_task_was_proven_red_at_its_merge_base(self) -> None:
        for task in self.tasks:
            with self.subTest(task=task["task_id"]):
                probe = task["baseline_probe"]
                self.assertEqual(probe["target"]["status"], "red")
                self.assertEqual(probe["regression"]["status"], "green")

    def test_the_path_dependent_candidate_is_not_in_the_corpus(self) -> None:
        """PR-1502's validator reaches a different verdict per checkout path.

        Its fixture spends the checkout path inside a length-capped command, so
        the verdict turns on how long the workspace path is. It is the case the
        two-path probe exists for, and the case two earlier versions of that
        probe missed: one renamed the leaf instead of changing the root, the
        other changed the root but compared only the status, and both roots
        were long enough to be red. Pinning it by name keeps both holes closed.
        """

        self.assertNotIn("PR-1502", [str(task["task_id"]) for task in self.tasks])
        self.assertIn(
            "verdict_depends_on_workspace_path",
            self.payload["selection"]["probe_rejected"],
            "the rejection is recorded by name so a dropped candidate is never "
            "a silent one",
        )

    def test_every_task_reached_the_same_verdict_under_two_workspace_paths(self) -> None:
        for task in self.tasks:
            with self.subTest(task=task["task_id"]):
                probe = task["baseline_probe"]
                self.assertEqual(
                    probe["target"]["status"],
                    "red",
                    "a task whose validator is not red at its merge base is not "
                    "a task: nothing has to be built to pass it",
                )
                self.assertTrue(
                    corpus.probe_passes_agree(probe["target"], probe["target_second_path"]),
                    "a validator that disagrees with itself between two checkout "
                    "paths would credit or fault an arm for the directory the "
                    "harness happened to pick",
                )

    def test_the_pinned_digests_re_derive_from_this_checkout(self) -> None:
        _skip_without_history(self)
        self.assertEqual(corpus.verify(ROOT, self.payload), [])

    def test_a_different_interpreter_is_drift_not_a_drifted_digest(self) -> None:
        """Two different facts, reported apart, with different remedies.

        Digests re-derive from git and do not depend on the interpreter, so a
        3.11 or 3.12 lane can confirm every one of them. What such a lane
        cannot confirm is the probe's verdicts, because the interpreter decides
        membership. Folding the second fact into the first made every CI lane
        report a drifted digest, which is not what had happened.
        """

        _skip_without_history(self)
        # Deliberately no assertion that THIS corpus has no drift. It has none
        # only on the interpreter that probed it, and CI runs 3.11 and 3.12
        # against a corpus probed on 3.13 -- asserting otherwise is how the
        # first version of this check turned a true fact into a red lane. The
        # contract under test is the separation, and it is stated with an
        # interpreter this test invents so it holds on every lane.
        pretend = dict(self.payload)
        selection = dict(pretend["selection"])
        selection["probe_environment"] = {
            **dict(selection["probe_environment"]),
            "python_version": "0.0.0-not-this-one",
        }
        pretend["selection"] = selection
        drift = corpus.environment_drift(pretend)
        self.assertTrue(drift)
        self.assertIn("python_version", drift[0])
        self.assertEqual(
            corpus.verify(ROOT, pretend),
            [],
            "an interpreter disagreeing says nothing about a digest",
        )


class GradingTests(unittest.TestCase):
    def test_a_green_unittest_run_is_green(self) -> None:
        summary = grading._summarize(0, "Ran 12 tests in 1.0s\n\nOK\n", 2)
        self.assertEqual(summary["status"], "green")
        self.assertEqual(summary["ran"], 12)

    def test_a_failed_unittest_run_is_red_with_its_counts(self) -> None:
        summary = grading._summarize(1, "Ran 12 tests in 1.0s\n\nFAILED (failures=2, errors=1)\n", 2)
        self.assertEqual(summary["status"], "red")
        self.assertEqual(summary["failures"], 2)
        self.assertEqual(summary["errors"], 1)

    def test_a_run_that_never_reached_a_verdict_is_an_error_not_a_red_test(self) -> None:
        summary = grading._summarize(1, "Traceback (most recent call last):\n", 2)
        self.assertEqual(summary["status"], "error")
        self.assertEqual(summary["classification"], "no_verdict")

    def test_a_completion_claim_the_validator_contradicts_is_a_false_completion(self) -> None:
        grade = grading.grade(
            target={"status": "red"},
            regression={"status": "green"},
            claim={"claim": "complete"},
            run_failed=False,
        )
        self.assertFalse(grade["pass"])
        self.assertTrue(grade["false_completion"])
        self.assertEqual(grade["reason"], "target_tests_failed")

    def test_a_blocked_claim_over_a_red_validator_is_not_a_false_completion(self) -> None:
        grade = grading.grade(
            target={"status": "red"},
            regression={"status": "green"},
            claim={"claim": "blocked"},
            run_failed=False,
        )
        self.assertFalse(grade["pass"])
        self.assertFalse(grade["false_completion"])

    def test_a_withdrawn_claim_keeps_the_reason_that_separates_it(self) -> None:
        grade = grading.grade(
            target={"status": "red"},
            regression={"status": "green"},
            claim={"claim": "blocked", "reason": "withdrawn_by_verification_gate"},
            run_failed=False,
        )
        self.assertEqual(grade["completion_claim"], "blocked")
        self.assertEqual(grade["completion_claim_reason"], "withdrawn_by_verification_gate")

    def test_a_green_validator_over_a_red_regression_set_does_not_pass(self) -> None:
        grade = grading.grade(
            target={"status": "green"},
            regression={"status": "red"},
            claim={"claim": "complete"},
            run_failed=False,
        )
        self.assertFalse(grade["pass"])
        self.assertEqual(grade["reason"], "regression_tests_failed")

    def test_completion_claim_reads_the_contract_file_and_nothing_else(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            self.assertEqual(grading.completion_claim(workspace)["claim"], "absent")
            (workspace / lane.COMPLETION_FILE).write_text('{"status": "complete"}', encoding="utf-8")
            self.assertEqual(grading.completion_claim(workspace)["claim"], "complete")
            (workspace / lane.COMPLETION_FILE).write_text("done!", encoding="utf-8")
            self.assertEqual(grading.completion_claim(workspace)["claim"], "unreadable")

    def test_a_weakened_regression_module_is_restored_before_it_is_graded(self) -> None:
        """The other half of the grade was gradeable from the candidate's own tree.

        Regression modules are chosen to be modules the pull request did NOT
        touch, so `materialize_validator` never restores them. The OMH arm's
        prompt names those exact modules as a criterion while another
        criterion permits edits under `tests/`, so a candidate that weakened
        one passed that half undetected, and the bare Hermes arm, told none of
        this, could not have done the same by accident.
        """

        _skip_without_history(self)
        payload = corpus.load(LANE / "corpus" / "evaluation.json")
        task = payload["tasks"][0]
        module = str(task["regression_modules"][0])
        with TemporaryDirectory() as root:
            workspace = Path(root)
            weakened = workspace / module
            weakened.parent.mkdir(parents=True, exist_ok=True)
            weakened.write_text("# every assertion deleted\n", encoding="utf-8")
            restored = grading.restore_regression_modules(ROOT, task, workspace)
            self.assertIn(module, restored)
            self.assertNotEqual(
                weakened.read_text(encoding="utf-8"), "# every assertion deleted\n"
            )
            self.assertEqual(
                weakened.read_bytes(),
                repo_lib.file_bytes(ROOT, str(task["merge_base"]), module),
                "restored byte for byte from the merge base: the question is "
                "whether the candidate broke what already worked, and a "
                "newline-translated copy would not answer it on every platform",
            )

    def test_a_binary_blob_does_not_crash_the_reader(self) -> None:
        """Widening the corpus read past the recent pull requests found this.

        `git show` in text mode decodes with the ambient codec, so the first
        binary blob in this repository's history raised `UnicodeDecodeError`
        from inside `subprocess` and took the whole corpus build with it.
        Bytes are read; text is a decode that may decline.
        """

        _skip_without_history(self)
        head = repo_lib.resolve(ROOT, "HEAD")
        with TemporaryDirectory() as root:
            work = Path(root)
            subprocess.run(["git", "init", "--quiet", str(work)], check=True, timeout=120)
            binary = work / "logo.png"
            binary.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff")
            for argv in (
                ["git", "-C", str(work), "add", "logo.png"],
                ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t",
                 "commit", "--quiet", "-m", "binary"],
            ):
                subprocess.run(argv, check=True, timeout=120)
            commit = repo_lib.resolve(work, "HEAD")
            self.assertEqual(
                repo_lib.file_bytes(work, commit, "logo.png"),
                b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff",
            )
            self.assertIsNone(
                repo_lib.file_at(work, commit, "logo.png"),
                "a blob that is not UTF-8 text is not text, and says so",
            )
            self.assertNotEqual(repo_lib.blob_digest(work, commit, "logo.png"), "-")
            self.assertEqual(repo_lib.blob_digest(work, commit, "absent.png"), "-")
        self.assertIsNotNone(repo_lib.file_at(ROOT, head, "README.md"))

    def test_a_commit_this_checkout_cannot_read_says_so(self) -> None:
        """"Vanished from the object store" sent a reader hunting for data loss.

        A missing object here is almost always a checkout with no history for
        that commit, so the message names the commit and whether the clone is
        shallow.
        """

        task = {"merge_commit": "0" * 40, "solution_blobs": {"README.md": "abc"}}
        with TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "shallow repository="):
                grading.materialize_solution(ROOT, task, Path(root))

    def test_the_validator_is_written_from_the_merge_commit_and_detected_when_present(self) -> None:
        _skip_without_history(self)
        payload = corpus.load(LANE / "corpus" / "evaluation.json")
        task = payload["tasks"][0]
        with TemporaryDirectory() as root:
            workspace = Path(root)
            self.assertEqual(grading.validator_paths_already_present(workspace, task), [])
            written = grading.materialize_validator(ROOT, task, workspace)
            self.assertTrue(written)
            present = grading.validator_paths_already_present(workspace, task)
            self.assertEqual(
                present,
                sorted(path for path, blob in task["test_blobs"].items() if blob != "-"),
            )


class WorkspaceTests(unittest.TestCase):
    def test_a_stale_registration_does_not_block_the_next_workspace(self) -> None:
        """A crashed run leaves a registration pointing at a deleted directory."""

        head = repo_lib.resolve(ROOT, "HEAD")
        with TemporaryDirectory() as root:
            with repo_lib.candidate_workspace(ROOT, head, Path(root), "one") as first:
                self.assertTrue((first / "src").is_dir())
                stale = Path(root) / "stale"
                subprocess.run(
                    ["git", "-C", str(ROOT), "worktree", "add", "--detach", str(stale), head],
                    capture_output=True, text=True, check=True, timeout=300,
                )
            import shutil as _shutil

            _shutil.rmtree(stale, ignore_errors=True)
            with repo_lib.candidate_workspace(ROOT, head, Path(root), "two") as second:
                self.assertTrue((second / "src").is_dir())


class ArmTests(unittest.TestCase):
    def test_both_arms_carry_the_identical_completion_contract(self) -> None:
        unit = arms.benchmark_unit(
            file_scope=["src/"],
            checks=["python -m compileall -q src"],
            route={
                "selected_model": "gpt-6-astra",
                "selected_reasoning_effort": "high",
                "model_family": "gpt",
            },
        )
        bare = arms.base_prompt("Do the thing.")
        delegated = arms.delegation_prompt("Do the thing.", unit)
        self.assertIn(arms.COMPLETION_CONTRACT, bare)
        self.assertIn(arms.COMPLETION_CONTRACT, delegated)
        self.assertIn(lane.COMPLETION_FILE, bare)

    def test_the_delegation_prompt_adds_only_what_omh_owns(self) -> None:
        protocol = arms.prompt_protocol()
        route = {
            "selected_model": "gpt-6-astra",
            "selected_reasoning_effort": "high",
            "model_family": "gpt",
        }
        unit = arms.benchmark_unit(file_scope=["src/"], checks=["python -m compileall -q src"], route=route)
        delegated = arms.delegation_prompt("Do the thing.", unit)
        self.assertIn(protocol.VERIFICATION_STOP_PROTOCOL, delegated)
        self.assertIn(protocol.GOAL_ECHO_PROTOCOL, delegated)
        self.assertIn(protocol.calibration_for_route(route), delegated)
        self.assertNotIn(protocol.UNIT_RESULT_RETURN_PROTOCOL, delegated)
        self.assertNotIn(protocol.UNIT_RESULT_RETURN_PROTOCOL, arms.base_prompt("Do the thing."))

    def test_the_prompt_never_tells_the_model_both_to_commit_and_not_to(self) -> None:
        """`completion_criteria_for_unit` appends the commit criterion always.

        It is fanout transport: it exists so a dispatched worktree can be
        collected, and this lane has no collector. Left in, it reached the
        model in the same prompt as "do not commit", and it falsified the
        profile name, which claims the exclusion this now performs.
        """

        transport = arms.fanout_transport_criteria()
        self.assertTrue(transport, "the shipped protocol appends at least one")
        route = {
            "selected_model": "gpt-5.6-sol",
            "selected_reasoning_effort": "high",
            "model_family": "gpt",
        }
        unit = arms.benchmark_unit(
            file_scope=["src/"], checks=["python -m compileall -q src"], route=route
        )
        delegated = arms.delegation_prompt("Do the thing.", unit)
        for criterion in transport:
            with self.subTest(criterion=criterion):
                self.assertNotIn(criterion, delegated)
        self.assertIn("Do not create a branch, do not commit", delegated)

    def test_a_command_reaches_the_model_in_the_case_it_must_be_typed_in(self) -> None:
        """The protocol capitalizes an integration check's first character.

        A bare `python -m compileall -q src` arrived as `Python …`. The gate
        lowercases the interpreter before running it, so the lane never
        noticed; a model copying the line on a case-sensitive filesystem
        would.
        """

        route = {
            "selected_model": "gpt-5.6-sol",
            "selected_reasoning_effort": "high",
            "model_family": "gpt",
        }
        unit = arms.benchmark_unit(
            file_scope=["src/"], checks=["python -m compileall -q src"], route=route
        )
        delegated = arms.delegation_prompt("Do the thing.", unit)
        self.assertIn("python -m compileall -q src", delegated)
        self.assertNotIn("Python -m compileall", delegated)

    def test_the_transport_criterion_is_derived_not_transcribed(self) -> None:
        """Derived from the protocol, so a rewording upstream stays filtered."""

        protocol = arms.prompt_protocol()
        empty = protocol.completion_criteria_for_unit(
            {"boundary": {}, "integration_checks": []}
        )
        self.assertEqual(arms.fanout_transport_criteria(), tuple(list(empty)[1:]))

    def test_the_oneshot_argv_pins_the_workspace_and_passes_the_prompt_as_an_argument(self) -> None:
        argv = arms.oneshot_argv(
            hermes_executable="hermes",
            workspace=Path("/tmp/ws"),
            provider="openai-codex",
            model="gpt-5.6-sol",
            effort="medium",
            usage_file=Path("/tmp/usage.json"),
            prompt="TASK",
            toolsets="file,terminal",
        )
        self.assertEqual(argv[1:3], ["--oneshot", "TASK"])
        self.assertIn("--in", argv)
        # `str(Path(...))`, not the literal, because the argv carries whatever
        # separator the platform spells a path with and Windows spells this one
        # `\tmp\ws`. What the assertion is for is that `--in` names the
        # workspace, and that survives the separator either way.
        self.assertEqual(argv[argv.index("--in") + 1], str(Path("/tmp/ws")))
        self.assertIn("--usage-file", argv)
        self.assertEqual(argv[argv.index("--toolsets") + 1], "file,terminal")

    def test_the_verification_gate_times_itself(self) -> None:
        """The gate runs on the OMH arms only, so its minutes must be charged.

        Left out of the wall clock, a compile pass and up to six unittest
        modules came off exactly one side of the comparison and landed on the
        "faster" headline.
        """

        with TemporaryDirectory() as root:
            workspace = Path(root) / "ws"
            workspace.mkdir()
            result = arms.run_verification(
                python_executable=sys.executable,
                workspace=workspace,
                scratch=Path(root) / "scratch",
                commands=["python -c 'pass'"],
                timeout=120,
            )
        self.assertIn("seconds", result)
        self.assertGreater(result["seconds"], 0.0)

    def test_the_verification_gate_reports_a_failing_check_without_raising(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root) / "ws"
            workspace.mkdir()
            result = arms.run_verification(
                python_executable=sys.executable,
                workspace=workspace,
                scratch=Path(root) / "scratch",
                commands=["python -c 'raise SystemExit(3)'", "definitely-not-a-program --x"],
                timeout=60,
            )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_count"], 2)
        self.assertEqual(result["checks"][0]["exit_code"], 3)
        self.assertEqual(result["checks"][1]["classification"], "not_observed")

    def test_a_usage_file_that_never_appeared_is_not_a_zero_reading(self) -> None:
        with TemporaryDirectory() as root:
            self.assertEqual(arms._read_usage(Path(root) / "missing.json"), {})

    def test_a_provider_limit_is_classified_apart_from_a_crash(self) -> None:
        self.assertEqual(arms._classify("You've hit your session limit"), "limit_reached")
        self.assertEqual(arms._classify("429 Too Many Requests"), "rate_limited")
        self.assertEqual(arms._classify("invalid api key"), "authentication_failed")
        self.assertEqual(arms._classify("segmentation fault"), "process_crash")


class MatrixTests(unittest.TestCase):
    def test_arm_order_is_counterbalanced_across_tasks(self) -> None:
        selected = ["hermes", "omh", "omh_mixture"]
        orders = [runner.arm_order(index, selected) for index in range(3)]
        self.assertEqual(orders[0][0], "hermes")
        self.assertEqual(orders[1][0], "omh")
        self.assertEqual(orders[2][0], "omh_mixture")
        for order in orders:
            self.assertEqual(sorted(order), sorted(selected))

    def test_the_mixture_arm_is_the_only_one_that_changes_the_model(self) -> None:
        control = {"provider": "openai-codex", "model": "gpt-5.6-sol", "effort": "medium", "mixture_provider": "og"}
        routing = {"resolved_model": "glm-5.3-ultrafast", "resolved_reasoning_effort": "medium"}
        self.assertEqual(runner._model_for_arm("hermes", control, None)["id"], "gpt-5.6-sol")
        self.assertEqual(runner._model_for_arm("omh", control, routing)["id"], "gpt-5.6-sol")
        mixture = runner._model_for_arm("omh_mixture", control, routing)
        self.assertEqual(mixture["id"], "glm-5.3-ultrafast")
        self.assertEqual(mixture["provider"], "og")

    def test_the_budget_counts_repair_turns_not_only_scheduled_runs(self) -> None:
        """`--max-paid-calls` is a spending limit, so it counts invocations.

        Two tasks over the two OMH arms schedule four runs and can launch eight
        model calls, because a verification gate that fails buys each of them a
        repair turn. A budget compared against the scheduled count would let a
        run through at half the money it can actually spend.
        """

        manifest = lane.load_object(LANE / "manifest.json")
        self.assertEqual(int(manifest["execution"]["omh_repair_attempts"]), 1)
        self.assertEqual(
            runner.worst_case_paid_calls(manifest, 2, ["hermes"]),
            2,
            "the bare Hermes arm gets one attempt and no repair turn",
        )
        self.assertEqual(
            runner.worst_case_paid_calls(manifest, 2, ["omh", "omh_mixture"]), 8
        )
        self.assertEqual(
            runner.worst_case_paid_calls(manifest, 40, ["hermes", "omh", "omh_mixture"]),
            200,
        )

    def test_a_live_matrix_refuses_to_exceed_its_explicit_budget(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceed the explicit budget"):
            runner.run_matrix(
                manifest=lane.load_object(LANE / "manifest.json"),
                payload={"corpus_digest": "d", "tasks": [{"task_id": "PR-1"}, {"task_id": "PR-2"}]},
                selected_arms=["hermes", "omh"],
                repository=ROOT,
                workspace_root=ROOT,
                output=ROOT / "unused.jsonl",
                omh_executable="omh",
                hermes_executable="hermes",
                python_executable=sys.executable,
                live=True,
                max_paid_calls=3,
            )

    def test_list_price_cost_comes_from_the_shipped_table(self) -> None:
        cost = runner.approximate_cost_usd(
            "gpt-5.6-sol",
            {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_tokens": 0},
        )
        self.assertIsInstance(cost, float)
        self.assertGreater(cost, 0.0)
        self.assertIsNone(
            runner.approximate_cost_usd(
                "gpt-5.6-sol", {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
            )
        )


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            _record("hermes", "PR-1", passed=True, claim="complete", cost=0.10, seconds=60, tokens=50_000),
            _record("hermes", "PR-2", passed=False, claim="complete", cost=0.12, seconds=90, tokens=60_000),
            _record("hermes", "PR-3", passed=False, claim="blocked", cost=0.08, seconds=40, tokens=40_000),
            _record("omh", "PR-1", passed=True, claim="complete", cost=0.05, seconds=30, tokens=25_000),
            _record("omh", "PR-2", passed=False, claim="blocked", cost=0.06, seconds=45, tokens=30_000),
            _record("omh", "PR-3", passed=True, claim="complete", cost=0.04, seconds=20, tokens=20_000),
        ]

    def _write(self, root: str) -> Path:
        path = Path(root) / "runs.jsonl"
        path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in self.records),
            encoding="utf-8",
        )
        return path

    def test_the_four_numbers_come_out_of_one_arm_summary(self) -> None:
        summary = report.arm_summary({str(row["task_id"]): row for row in self.records[:3]})
        self.assertEqual(summary["passed"], 1)
        self.assertAlmostEqual(summary["pass_rate"], 1 / 3)
        self.assertAlmostEqual(summary["cost_usd_per_pass"], 0.30, places=6)
        self.assertEqual(summary["seconds_median"], 60.0)
        self.assertEqual(summary["false_completions"], 1)
        self.assertAlmostEqual(summary["false_completion_rate"], 1 / 3)

    def test_an_unpriced_run_is_unknown_and_never_zero(self) -> None:
        """A model with no price-table entry must not render as a free arm.

        Resolving the mixture routing over the shipped corpus dispatches
        `glm-5.3-ultrafast`, which has no entry, so collapsing its price to
        0.0 rendered a 100 percent cost saving with a tight interval, built
        out of unknowns.
        """

        rows = {
            "PR-1": _record(
                "omh_mixture", "PR-1", passed=True, claim="complete",
                cost=None, seconds=30, tokens=1000, model="glm-5.3-ultrafast",
            ),
            "PR-2": _record(
                "omh_mixture", "PR-2", passed=False, claim="blocked",
                cost=0.04, seconds=30, tokens=1000,
            ),
        }
        summary = report.arm_summary(rows)
        self.assertFalse(summary["cost_is_complete"])
        self.assertIsNone(summary["cost_usd_total"])
        self.assertIsNone(summary["cost_usd_per_pass"])
        self.assertEqual(summary["runs_unpriced"], 1)
        self.assertEqual(summary["unpriced_models"], ["glm-5.3-ultrafast"])

    def test_an_unpriced_run_raises_rather_than_pricing_itself_at_zero(self) -> None:
        unpriced = _record(
            "omh", "PR-9", passed=True, claim="complete",
            cost=None, seconds=1, tokens=1,
        )
        self.assertIsNone(report.priced(unpriced))
        with self.assertRaisesRegex(ValueError, "not a free run"):
            report._cost(unpriced)

    def test_a_cost_delta_over_unpriced_pairs_is_refused_by_name(self) -> None:
        before = [_record("hermes", "PR-1", passed=True, claim="complete", cost=0.10, seconds=10, tokens=10)]
        after = [_record("omh", "PR-1", passed=True, claim="complete", cost=None, seconds=10, tokens=10)]
        delta = report.paired_delta(before, after, "cost", 200, 1)
        self.assertIsNone(delta["mean_delta"])
        self.assertEqual(delta["reading"], report.UNPRICED_RUNS)
        self.assertEqual(delta["unpriced_tasks"], ["PR-1"])

    def test_the_table_says_unknown_and_names_the_unpriced_model(self) -> None:
        rendered = report.render_table(
            {
                "arms": {
                    "omh_mixture": report.arm_summary(
                        {
                            "PR-1": _record(
                                "omh_mixture", "PR-1", passed=True, claim="complete",
                                cost=None, seconds=30, tokens=1000,
                                model="glm-5.3-ultrafast",
                            )
                        }
                    )
                }
            }
        )
        self.assertIn("unknown", rendered)
        self.assertNotIn("$0.0000", rendered)
        self.assertIn("glm-5.3-ultrafast", rendered)
        self.assertIn("Unpriced", rendered)
        self.assertIn("could not be priced", rendered)

    def test_a_provider_failure_is_counted_but_never_quietly_dropped(self) -> None:
        """The OMH arms launch up to twice the calls, so they meet more limits.

        Excluding those rows would flatter the arm that spends more; hiding
        them would leave a pass rate the reader cannot interpret. Both numbers
        are reported.
        """

        row = _record(
            "omh", "PR-1", passed=False, claim="blocked",
            cost=0.01, seconds=5, tokens=10,
        )
        row["failure_receipt"] = {"classification": "limit_reached", "kind": "primary"}
        ok = _record(
            "omh", "PR-2", passed=True, claim="complete",
            cost=0.01, seconds=5, tokens=10,
        )
        summary = report.arm_summary({"PR-1": row, "PR-2": ok})
        self.assertEqual(summary["runs_failed_for_provider_reasons"], 1)
        self.assertAlmostEqual(summary["pass_rate"], 0.5)
        self.assertAlmostEqual(summary["pass_rate_excluding_provider_failures"], 1.0)

    def test_a_crash_is_not_counted_as_a_provider_failure(self) -> None:
        row = _record(
            "omh", "PR-1", passed=False, claim="blocked",
            cost=0.01, seconds=5, tokens=10,
        )
        row["failure_receipt"] = {"classification": "process_crash", "kind": "primary"}
        self.assertFalse(report.failed_for_provider_reasons(row))
        self.assertTrue(
            report.failed_for_provider_reasons(
                {"failure_receipt": {"classification": "rate_limited"}}
            )
        )

    def test_a_report_can_be_restricted_to_the_headline_subset(self) -> None:
        """Only issue-sourced tasks may carry a sentence about our own issues.

        A pull-request body is written after the fix by its author, and no
        heading rule removes what a paraphrase leaks, so the headline has to
        be able to name the tasks that actually came from issues.
        """

        payload = {
            "tasks": [
                {"task_id": "PR-1", "task_source": "linked_issue", "leak_class": "clean"},
                {"task_id": "PR-2", "task_source": "pull_request_body", "leak_class": "clean"},
                {"task_id": "PR-3", "task_source": "linked_issue",
                 "leak_class": "names_changed_file"},
            ]
        }
        self.assertEqual(
            report.subset_task_ids(payload, task_source="linked_issue"),
            {"PR-1", "PR-3"},
        )
        self.assertEqual(
            report.subset_task_ids(payload, leak_classes=["clean"]), {"PR-1", "PR-2"}
        )
        self.assertEqual(
            report.subset_task_ids(
                payload, task_source="linked_issue", leak_classes=["clean"]
            ),
            {"PR-1"},
        )

    def test_a_subset_report_states_which_tasks_it_is_about(self) -> None:
        with TemporaryDirectory() as root:
            produced = report.analyze(
                records_path=self._write(root),
                manifest=lane.load_object(LANE / "manifest.json"),
                repetitions=200,
                seed=1,
                only_task_ids=["PR-1", "PR-2"],
                subset_label="task_source=linked_issue",
            )
        self.assertEqual(produced["subset"], "task_source=linked_issue")
        self.assertEqual(produced["subset_task_count"], 2)
        for summary in produced["arms"].values():
            self.assertEqual(summary["tasks"], 2)

    def test_a_subset_that_matches_no_record_is_refused(self) -> None:
        with TemporaryDirectory() as root:
            # A subset naming a task the records do not carry means the corpus
            # and the records are not the same corpus, which is exactly the
            # drift the headline subset's definition must not suffer.
            with self.assertRaisesRegex(ValueError, "not describe the same corpus"):
                report.analyze(
                    records_path=self._write(root),
                    manifest=lane.load_object(LANE / "manifest.json"),
                    repetitions=200,
                    only_task_ids=["PR-999"],
                    subset_label="nothing",
                )
            with self.assertRaisesRegex(ValueError, "no run record is in the subset"):
                report.analyze(
                    records_path=self._write(root),
                    manifest=lane.load_object(LANE / "manifest.json"),
                    repetitions=200,
                    only_task_ids=[],
                    subset_label="empty",
                )

    def test_the_report_pairs_every_arm_against_the_baseline(self) -> None:
        with TemporaryDirectory() as root:
            produced = report.analyze(
                records_path=self._write(root),
                manifest=lane.load_object(LANE / "manifest.json"),
                repetitions=200,
                seed=1,
            )
        self.assertEqual(produced["baseline_arm"], "hermes")
        comparison = produced["comparisons"]["omh"]
        self.assertEqual(comparison["paired_tasks"], 3)
        self.assertLess(comparison["deltas"]["cost"]["mean_delta"], 0)
        self.assertEqual(produced["arms"]["omh"]["false_completions"], 0)
        self.assertIn("claim_boundary", produced)

    def test_records_from_two_corpora_are_never_compared(self) -> None:
        self.records[0]["corpus_digest"] = "other"
        with TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "one pinned corpus"):
                report.analyze(
                    records_path=self._write(root),
                    manifest=lane.load_object(LANE / "manifest.json"),
                    repetitions=200,
                )

    def test_a_delta_whose_interval_spans_zero_is_named_not_rounded(self) -> None:
        before = [_record("hermes", "PR-1", passed=True, claim="complete", cost=0.1, seconds=10, tokens=1)]
        after = [_record("omh", "PR-1", passed=True, claim="complete", cost=0.1, seconds=10, tokens=1)]
        delta = report.paired_delta(before, after, "cost", 200, 1)
        self.assertTrue(delta["crosses_zero"])
        rendered = report.render_deltas(
            {
                "comparisons": {
                    "omh": {"deltas": {"cost": delta}},
                }
            }
        )
        self.assertIn(report.NO_MEASURABLE_DIFFERENCE, rendered)

    def test_the_table_renders_every_arm(self) -> None:
        with TemporaryDirectory() as root:
            produced = report.analyze(
                records_path=self._write(root),
                manifest=lane.load_object(LANE / "manifest.json"),
                repetitions=200,
            )
        table = report.render_table(produced)
        self.assertIn("| hermes |", table)
        self.assertIn("| omh |", table)
        self.assertIn("Cost / pass", table)


class ArtifactSafetyTests(unittest.TestCase):
    def test_a_record_naming_a_home_directory_or_a_prompt_is_refused(self) -> None:
        self.assertFalse(lane.artifact_is_safe({"workspace": "/Users/someone/repo"}))
        self.assertFalse(lane.artifact_is_safe({"prompt": "the task text"}))
        self.assertTrue(lane.artifact_is_safe({"task_id": "PR-1", "task_digest": "ab" * 32}))

    def test_no_lane_source_persists_a_prompt(self) -> None:
        for path in sorted(LANE.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn('"prompt": prompt', text)
                self.assertNotIn("write_text(prompt", text)


class CommandLineTests(unittest.TestCase):
    def _bench(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(LANE / "bench.py"), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )

    def test_a_paid_run_requires_an_explicit_call_budget(self) -> None:
        completed = self._bench("run", "--allow-paid-live")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--max-paid-calls", completed.stderr)

    def test_smoke_enforces_the_budget_that_run_enforces(self) -> None:
        """The budget reached `run_matrix` only, and `smoke` does not use it.

        One task over the three arms is three calls, and five once the repair
        turns fire, so under `--max-paid-calls 1` the flag was accepted,
        echoed back on the receipt, and ignored.
        """

        completed = self._bench(
            "smoke",
            "--arm", "hermes", "--arm", "omh", "--arm", "omh_mixture",
            "--allow-paid-live", "--max-paid-calls", "1",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("exceed the explicit budget", completed.stderr)

    def test_the_control_effort_is_one_that_actually_carries_calibration(self) -> None:
        """Both doc surfaces say the OMH arm carries the route's calibration.

        `calibration_for_route` returns "" outside the high effort tier, so a
        control effort of `medium` made that sentence false on every task
        while the mixture arm, routed at high, did get a block: calibration
        only where the model also changed.
        """

        manifest = lane.load_object(LANE / "manifest.json")
        effort = str(manifest["control"]["effort"])
        protocol = arms.prompt_protocol()
        self.assertIn(effort, protocol.HIGH_EFFORT_TIER)
        calibration = protocol.calibration_for_route(
            {
                "selected_model": str(manifest["control"]["model"]),
                "selected_reasoning_effort": effort,
                "model_family": "gpt",
            }
        )
        self.assertTrue(calibration)

    def test_the_corpus_command_takes_exactly_one_mode(self) -> None:
        completed = self._bench("corpus")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("exactly one", completed.stderr)

    def test_doctor_fails_loudly_when_the_corpus_is_missing(self) -> None:
        with TemporaryDirectory() as root:
            completed = self._bench("doctor", "--corpus", str(Path(root) / "absent.json"))
        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        names = [check["check"] for check in payload["checks"]]
        self.assertIn("corpus_present", [c["check"] for c in payload["checks"] if not c["ok"]])
        for required in ("providers_linked", "control_route_resolves", "workspace_creatable"):
            self.assertIn(required, names)

    def test_the_manifest_pins_one_control_model_and_a_claim_boundary(self) -> None:
        manifest = lane.load_object(LANE / "manifest.json")
        self.assertEqual(manifest["schema_version"], lane.MANIFEST_SCHEMA)
        for field in ("provider", "model", "effort"):
            self.assertTrue(manifest["control"][field])
        self.assertIn("pinned corpus", manifest["claim_boundary"])
        self.assertEqual(manifest["execution"]["toolsets"], "file,terminal")
        self.assertEqual(manifest["execution"]["path"], arms.EXECUTION_PATH)

    def test_doctor_refuses_an_execution_path_the_lane_does_not_implement(self) -> None:
        manifest = lane.load_object(LANE / "manifest.json")
        manifest["execution"] = dict(manifest["execution"]) | {"path": "omh_hermes_child"}
        with TemporaryDirectory() as root:
            path = Path(root) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            completed = self._bench("doctor", "--manifest", str(path))
        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        failed = [check["check"] for check in payload["checks"] if not check["ok"]]
        self.assertIn("manifest_execution_path", failed)


if __name__ == "__main__":
    unittest.main()

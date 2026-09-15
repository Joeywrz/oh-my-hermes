"""Recovering from a memory source that turned out to be wrong.

Every step of that recovery already shipped -- lineage, retire, prune,
correct, recall-suite -- and every one of them takes a record id, so an
operator who learns a whole source was poisoned had no way to produce the set
to act on. These tests pin the enumeration that produces it, and the two
things that make the enumeration safe to act on: it writes nothing, and it
says out loud which records it could not judge.

Each test runs against a temporary OMH home. None of them reads the developer's
own store.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import chdir
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
from project_identity_fixture import PROJECT_IDENTITY, memory_paths
from omh.workflows import memory
from omh.workflows.memory_evaluation import _seed_retrieval_store, run_memory_retrieval_evaluation
from omh.workflows.memory_retrieval_fixtures import _case, _record
from omh.workflows.memory_sources import SELECTOR_OUTCOMES, SOURCE_AXES, build_memory_source_index

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)
POISONED = "wiki-scrape"


def _store_digest(root: Path) -> str:
    """Every byte under the memory directory, so a read that wrote is visible."""
    entries = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        entries.append(f"{path.relative_to(root)}:{hashlib.sha256(path.read_bytes()).hexdigest()}")
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


class MemorySourceSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(chdir(self.root))
        self.paths = memory_paths(self.root / "omh", self.root / "hermes")

    def approve(self, summary: str, *, source: str = "cli", source_ref: str = "", unresolved: bool = False) -> str:
        candidate = memory.capture_project_memory_candidate(
            self.paths,
            summary,
            scope_kind="project",
            scope_ref=PROJECT_IDENTITY,
            source=source,
            source_ref=source_ref,
            unresolved=unresolved,
        )["candidate"]
        assert isinstance(candidate, dict)
        record = memory.approve_project_memory_candidate(
            self.paths, str(candidate["candidate_id"]), unresolved=unresolved
        )["record"]
        assert isinstance(record, dict)
        return str(record["record_id"])

    def strip_sources(self, record_id: str) -> None:
        """Make one stored record carry no source, as an older build's record does."""
        path = self.paths.memory_dir / "records" / f"{record_id}.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        for axis in SOURCE_AXES:
            stored.pop(axis, None)
        path.write_text(json.dumps(stored, sort_keys=True), encoding="utf-8")

    def selected_ids(self, payload: dict[str, object]) -> list[str]:
        selected = payload["selected"]
        assert isinstance(selected, dict)
        return [str(row["record_id"]) for row in selected["records"]]

    def test_selecting_by_source_returns_every_record_admitted_from_it(self) -> None:
        # Given three records from the poisoned source and one from another.
        poisoned = {
            self.approve("Deploy gate requires two approvals", source=POISONED),
            self.approve("Release notes live in the wiki", source=POISONED),
            self.approve("Rollback is a one-command step", source=POISONED),
        }
        clean = self.approve("The parser owner is the platform team", source="cli")

        # When the operator asks which records came from it.
        payload = build_memory_source_index(self.paths, source=POISONED)

        # Then every one of them is returned, and only them.
        selected = payload["selected"]
        assert isinstance(selected, dict)
        self.assertEqual(selected["outcome"], "matched")
        self.assertEqual(set(self.selected_ids(payload)), poisoned)
        self.assertNotIn(clean, self.selected_ids(payload))
        self.assertEqual(selected["count"], 3)

        # And a limit shortens the cards without shortening the answer: the
        # count stays the full match, and the report says it was cut.
        limited = build_memory_source_index(self.paths, source=POISONED, limit=2)["selected"]
        assert isinstance(limited, dict)
        self.assertEqual(limited["count"], 3)
        self.assertEqual(len(limited["records"]), 2)
        self.assertTrue(limited["truncated"])

    def test_a_record_admitted_from_two_sources_appears_under_both(self) -> None:
        """Membership is per axis. A first-match selector would halve this radius."""
        # Given one record whose two source axes name two different origins, and
        # one whose two axes name the same origin.
        two_origins = self.approve("Owner rotation is monthly", source=POISONED, source_ref="wiki/onboarding")
        one_origin_twice = self.approve("Canary batches gate promotion", source=POISONED, source_ref=POISONED)

        # When the index is built without a selector.
        index = build_memory_source_index(self.paths)
        labels = {str(row["label"]): row for row in index["sources"]}

        # Then the record with two origins is counted under both labels, not the first.
        self.assertIn(POISONED, labels)
        self.assertIn("wiki/onboarding", labels)
        self.assertEqual(labels["wiki/onboarding"]["record_count"], 1)
        self.assertEqual(labels["wiki/onboarding"]["axes"], ["source_ref"])
        self.assertEqual(sorted(labels[POISONED]["axes"]), ["source", "source_ref"])

        # And selecting either origin returns the record that carries it.
        self.assertEqual(self.selected_ids(build_memory_source_index(self.paths, source="wiki/onboarding")), [two_origins])
        self.assertEqual(
            set(self.selected_ids(build_memory_source_index(self.paths, source=POISONED))),
            {two_origins, one_origin_twice},
        )

        # And the record matched on both axes names both, not only the first.
        selected = build_memory_source_index(self.paths, source=POISONED)["selected"]
        assert isinstance(selected, dict)
        matched = {str(row["record_id"]): list(row["matched_axes"]) for row in selected["records"]}
        self.assertEqual(matched[one_origin_twice], ["source", "source_ref"])
        self.assertEqual(matched[two_origins], ["source"])

    def test_the_enumeration_is_a_read_and_quarantines_nothing(self) -> None:
        # Given a store with records from the poisoned source.
        record_id = self.approve("Deploy gate requires two approvals", source=POISONED)
        before = _store_digest(self.paths.memory_dir)

        # When the blast radius is enumerated, twice.
        build_memory_source_index(self.paths, source=POISONED)
        payload = build_memory_source_index(self.paths, source=POISONED)

        # Then the record is named and not one byte of the store changed.
        self.assertEqual(self.selected_ids(payload), [record_id])
        self.assertEqual(_store_digest(self.paths.memory_dir), before)
        self.assertTrue((self.paths.memory_dir / "records" / f"{record_id}.json").is_file())
        self.assertFalse((self.paths.memory_dir / "archive").exists())

    def test_an_empty_source_and_a_never_recorded_axis_are_different_answers(self) -> None:
        """Three ways to match nothing, and none of them may read as another."""
        # Given an empty store.
        empty = build_memory_source_index(self.paths, source=POISONED)["selected"]
        assert isinstance(empty, dict)
        self.assertEqual(empty["outcome"], "empty_store")

        # Given a store whose records record no source at all.
        blank = self.approve("Owner rotation is monthly", source=POISONED)
        self.strip_sources(blank)
        never = build_memory_source_index(self.paths, source=POISONED)
        never_selected = never["selected"]
        assert isinstance(never_selected, dict)
        self.assertEqual(never_selected["outcome"], "source_never_recorded")
        self.assertEqual([axis["populated"] for axis in never["axes"]], [False, False])

        # Given a store that does record sources, but never this one.
        self.approve("The parser owner is the platform team", source="cli")
        absent = build_memory_source_index(self.paths, source=POISONED)["selected"]
        assert isinstance(absent, dict)
        self.assertEqual(absent["outcome"], "no_records_for_source")

        # Then all three reported an empty set under three distinct outcomes,
        # and the declared vocabulary holds nothing this suite never produces.
        outcomes = {str(empty["outcome"]), str(never_selected["outcome"]), str(absent["outcome"])}
        self.assertEqual(len(outcomes), 3)
        self.assertEqual({empty["count"], never_selected["count"], absent["count"]}, {0})
        self.assertEqual(outcomes | {"not_requested", "matched"}, set(SELECTOR_OUTCOMES))

    def test_a_record_with_no_recorded_source_is_indeterminate_never_clean(self) -> None:
        # Given one record from the poisoned source and one that recorded none.
        poisoned = self.approve("Deploy gate requires two approvals", source=POISONED)
        unattributed = self.approve("Owner rotation is monthly", source="cli")
        self.strip_sources(unattributed)

        # When the blast radius is enumerated.
        payload = build_memory_source_index(self.paths, source=POISONED)
        indeterminate = payload["indeterminate"]
        assert isinstance(indeterminate, dict)

        # Then the unattributed record is neither matched nor silently excluded.
        self.assertEqual(self.selected_ids(payload), [poisoned])
        self.assertEqual(
            [row["record_id"] for row in indeterminate["records"]], [unattributed]
        )
        self.assertEqual([row["reason"] for row in indeterminate["records"]], ["source_not_recorded"])
        self.assertEqual(indeterminate["count"], 1)

    def test_an_unreadable_record_file_is_indeterminate_too(self) -> None:
        # Given a corrupt record file beside a readable one.
        poisoned = self.approve("Deploy gate requires two approvals", source=POISONED)
        (self.paths.memory_dir / "records" / "rec_corrupt.json").write_text("{not json", encoding="utf-8")

        # When the blast radius is enumerated.
        payload = build_memory_source_index(self.paths, source=POISONED)
        indeterminate = payload["indeterminate"]
        assert isinstance(indeterminate, dict)

        # Then the file this selector could not read is named, not counted clean.
        self.assertEqual(self.selected_ids(payload), [poisoned])
        self.assertEqual(
            [row["path_name"] for row in indeterminate["unreadable"]], ["rec_corrupt.json"]
        )
        self.assertEqual(indeterminate["count"], 1)

    def test_the_cli_surface_enumerates_one_source(self) -> None:
        # Given a store reachable only through the CLI's own flags.
        prefix = ["--omh-home", str(self.paths.omh_home), "--hermes-home", str(self.paths.hermes_home)]
        status, stdout, stderr = run_cli(prefix + ["memory", "capture", "Deploy gate requires two approvals", "--source", POISONED])
        self.assertEqual(status, 0, stderr)
        candidate_id = json.loads(stdout)["candidate"]["candidate_id"]
        status, stdout, stderr = run_cli(prefix + ["memory", "review", "--candidate", candidate_id])
        self.assertEqual(status, 0, stderr)
        revision = json.loads(stdout)["cards"][0]["review_revision"]
        status, stdout, stderr = run_cli(prefix + ["memory", "approve", candidate_id, "--candidate-revision", revision])
        self.assertEqual(status, 0, stderr)
        record_id = json.loads(stdout)["record"]["record_id"]

        # When the operator asks the shipped command.
        status, stdout, stderr = run_cli(prefix + ["memory", "sources", "--source", POISONED])

        # Then the command returns the set and reports a completed read.
        self.assertEqual(status, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["schema_version"], "memory_source_index/v1")
        self.assertEqual([row["record_id"] for row in payload["selected"]["records"]], [record_id])


class MemorySourceRecoveryEvidenceTests(unittest.TestCase):
    """The completion evidence is a recall report, never the retire exit code."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(chdir(self.root))
        self.paths = memory_paths(self.root / "omh", self.root / "hermes")

    def approve_open(self, summary: str, *, source: str) -> str:
        """An open record: the one shape a targeted retire archives at today's clock.

        `retire` refuses a settled live record as `not_expired`, so a
        same-clock before/after comparison -- where the only variable is the
        retire itself -- has to use records the operator marked unresolved.
        """
        candidate = memory.capture_project_memory_candidate(
            self.paths, summary, scope_kind="project", scope_ref=PROJECT_IDENTITY, source=source, unresolved=True
        )["candidate"]
        assert isinstance(candidate, dict)
        record = memory.approve_project_memory_candidate(
            self.paths, str(candidate["candidate_id"]), unresolved=True
        )["record"]
        assert isinstance(record, dict)
        return str(record["record_id"])

    def recalled(self, query: str) -> list[str]:
        pack = memory.build_project_memory_recall_pack(
            self.paths, query, scope_kind="project", scope_ref=PROJECT_IDENTITY, now=NOW
        )
        return [str(row["record_id"]) for row in pack["included_records"]]

    def test_the_recall_report_and_not_the_retire_exit_code_proves_the_set_is_gone(self) -> None:
        from omh.commands.memory import _memory_retire_exit_code

        # Given two recalled records from the poisoned source and one from another.
        poisoned = {
            self.approve_open("Deploy gate requires two approvals", source=POISONED),
            self.approve_open("Deploy rollback is a one-command step", source=POISONED),
        }
        clean = self.approve_open("Deploy owners are the platform team", source="cli")
        self.assertEqual(set(self.recalled("deploy")), poisoned | {clean})

        # And a sweep that archives nothing still exits 0 -- so the status is
        # not the evidence, even while every poisoned record is still recalled.
        sweep = memory.apply_memory_retirement(self.paths, now=NOW)
        self.assertEqual(sweep["counts"]["expired"], 0)
        self.assertEqual(_memory_retire_exit_code(sweep), 0)
        self.assertEqual(set(self.recalled("deploy")), poisoned | {clean})

        # When the enumerated set is retired one record at a time.
        selected = build_memory_source_index(self.paths, source=POISONED)["selected"]
        assert isinstance(selected, dict)
        enumerated = [str(row["record_id"]) for row in selected["records"]]
        self.assertEqual(set(enumerated), poisoned)
        for record_id in enumerated:
            applied = memory.apply_memory_retirement(self.paths, record_id=record_id, now=NOW)
            self.assertEqual([str(row["record_id"]) for row in applied["moved"]], [record_id])

        # Then the recall report -- not any exit code -- shows them absent and
        # the untouched record still delivered.
        self.assertEqual(self.recalled("deploy"), [clean])
        payload = build_memory_source_index(self.paths, source=POISONED)
        after = payload["selected"]
        assert isinstance(after, dict)
        self.assertEqual(after["outcome"], "no_records_for_source")

        # And the report names both things that are not that evidence, so a
        # caller does not take a fixture-corpus regression run for a store read.
        recovery = payload["recovery"]
        assert isinstance(recovery, dict)
        not_evidence = str(recovery["not_completion_evidence"])
        self.assertIn("exit code", not_evidence)
        self.assertIn("omh memory recall-suite", not_evidence)
        self.assertIn("omh memory recall", str(recovery["completion_evidence"]))

    def test_recall_suite_shows_the_enumerated_records_absent_after_removal(self) -> None:
        """The recall-pack producer the suite drives stops returning the set.

        `recall-suite` seeds its own fixture corpus in a temporary store, so it
        proves the recall engine's behavior over a corpus, never anything about
        an operator's own store. That is why the report drives a corpus built
        from the same specs the selector was run against: the arm being pinned
        here is the producer, and the live-store arm is the test above.
        """
        # Given a retrieval corpus where two records carry the poisoned source.
        specs = (
            _record("mem_src_poison_a", "Staging deploys run canary batches", approved_at="2030-12-20T00:00:00Z", tags=("deploy",), source=POISONED),
            _record("mem_src_poison_b", "Staging deploys are announced in the release channel", approved_at="2030-12-20T00:00:00Z", tags=("deploy",), source=POISONED),
            _record("mem_src_clean", "Staging deploys are owned by the platform team", approved_at="2030-12-20T00:00:00Z", tags=("deploy",), source="cli"),
        )
        case = _case("source_recovery", "Records from one source leave recall together.", records=specs, query="staging deploys")
        before = run_memory_retrieval_evaluation(cases=(case,))
        observed_before = list(before["cases"][0]["observed_included_order"])
        self.assertEqual(sorted(observed_before), ["mem_src_clean", "mem_src_poison_a", "mem_src_poison_b"])

        # And the same store, enumerated by source.
        _seed_retrieval_store(self.paths, case)
        selected = build_memory_source_index(self.paths, source=POISONED)["selected"]
        assert isinstance(selected, dict)
        enumerated = {str(row["record_id"]) for row in selected["records"]}
        self.assertEqual(enumerated, {"mem_src_poison_a", "mem_src_poison_b"})

        # When exactly that set leaves the store.
        after_case = {**case, "records": [spec for spec in specs if spec["record_id"] not in enumerated]}
        after = run_memory_retrieval_evaluation(cases=(after_case,))
        observed_after = list(after["cases"][0]["observed_included_order"])

        # Then the suite's own report shows the previously recalled records absent.
        self.assertEqual(observed_after, ["mem_src_clean"])
        self.assertEqual(sorted(set(observed_before) - set(observed_after)), sorted(enumerated))

    def test_the_corpus_digest_does_not_move_for_a_spec_that_names_no_source(self) -> None:
        """The source seam is opt-in, so the retained corpus stays comparable."""
        from omh.workflows.memory_retrieval_fixtures import RETRIEVAL_CASES, fixture_digest

        plain = _record("mem_digest", "Owner rotation is monthly", approved_at="2030-12-20T00:00:00Z")
        self.assertNotIn("source", plain)
        self.assertIn("source", _record("mem_digest", "x", approved_at="2030-12-20T00:00:00Z", source=POISONED))
        self.assertEqual(fixture_digest(RETRIEVAL_CASES), fixture_digest())


if __name__ == "__main__":
    unittest.main()

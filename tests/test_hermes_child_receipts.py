from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from omh.coding import hermes_child_receipts
from omh.coding.hermes_child_receipts import (
    ReceiptVerificationError,
    load_hermes_child_receipt as _load_hermes_child_receipt,
    load_or_create_observation_key,
    write_signed_observation,
)
from omh.coding.routing_observation import (
    authenticate_child_observation,
    build_routing_observation,
)
from paired_run_support import resign_observation, write_observed_receipt


def load_hermes_child_receipt(
    omh_home: Path,
    run_id: str,
    _legacy_caller_time: str | None = None,
):
    return _load_hermes_child_receipt(omh_home, run_id)


class HermesChildReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(prefix="omh-paired-receipt-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.omh_home = (self.root / ".omh").resolve()

    def test_public_writer_cannot_mint_terminal_receipt_from_mapping(self) -> None:
        # Given
        run_dir = self.omh_home / "coding" / "hermes-child" / "forged-run"
        run_dir.mkdir(parents=True)
        forged = build_routing_observation(
            route={
                "selected_model": "fixture/model",
                "selected_reasoning_effort": "high",
                "role": "agent_maintainer",
                "executor_profile": "hermes_child",
                "chain": [],
            },
            child_dispatch=authenticate_child_observation(
                {"status": "completed", "run_id": "forged-run"}
            ),
            run_id="forged-run",
        )
        # When / Then
        with self.assertRaises(ReceiptVerificationError):
            write_signed_observation(run_dir, forged)
        with self.assertRaises(ReceiptVerificationError):
            load_hermes_child_receipt(self.omh_home, "forged-run", "2099-01-01T00:00:00Z")

    def test_loads_authenticated_observed_receipt_when_persisted_run_matches(self) -> None:
        # Given
        write_observed_receipt(self.omh_home, "run-1")
        # When
        receipt = load_hermes_child_receipt(self.omh_home, "run-1", "2026-08-27T00:00:00Z")
        # Then
        self.assertEqual((receipt.run_id, receipt.claim), ("run-1", "observed"))
        self.assertEqual(receipt.observed_at, "2026-08-27T00:00:00Z")

    def test_public_loader_rejects_caller_selected_observation_time(self) -> None:
        write_observed_receipt(self.omh_home, "run-1")
        with self.assertRaises(TypeError):
            _load_hermes_child_receipt(
                self.omh_home,
                "run-1",
                "2099-01-01T00:00:00Z",
            )

    def test_rejects_nonterminal_observation_but_accepts_terminal_failure(self) -> None:
        write_observed_receipt(self.omh_home, "running-run", "running")
        with self.assertRaises(ReceiptVerificationError):
            load_hermes_child_receipt(self.omh_home, "running-run", "2026-08-27T00:00:00Z")
        for status in ("failed", "timed_out", "cancelled"):
            with self.subTest(status=status):
                run_id = f"{status}-run"
                write_observed_receipt(self.omh_home, run_id, status)
                receipt = load_hermes_child_receipt(self.omh_home, run_id, "2026-08-27T00:00:00Z")
                self.assertEqual(receipt.status, status)

    def test_rejects_tampering_and_hostile_run_path(self) -> None:
        cases = ("tampered", "symlink")
        for case in cases:
            with self.subTest(case=case), TemporaryDirectory(prefix="omh-receipt-case-") as raw:
                home = (Path(raw) / ".omh").resolve()
                run_dir = write_observed_receipt(home, "run-1")
                if case == "tampered":
                    payload = json.loads((run_dir / "observation.json").read_text())
                    payload["status"] = "failed"
                    (run_dir / "observation.json").write_text(json.dumps(payload))
                else:
                    outside = Path(raw) / "outside"
                    outside.mkdir()
                    for item in run_dir.iterdir():
                        item.replace(outside / item.name)
                    run_dir.rmdir()
                    run_dir.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(ReceiptVerificationError):
                    load_hermes_child_receipt(home, "run-1", "2026-08-27T00:00:00Z")

    def test_rejects_coherently_signed_prepared_and_wrong_run_observations(self) -> None:
        for case in ("prepared", "wrong-run"):
            with self.subTest(case=case), TemporaryDirectory(prefix="omh-receipt-semantic-") as raw:
                home = (Path(raw) / ".omh").resolve()
                run_dir = write_observed_receipt(home, "run-1")
                payload = json.loads((run_dir / "observation.json").read_text())
                if case == "prepared":
                    payload["claim"] = "prepared"
                    payload["status"] = "prepared"
                else:
                    payload["run_id"] = "other"
                (run_dir / "observation.json").write_text(json.dumps(payload))
                resign_observation(run_dir)
                with self.assertRaises(ReceiptVerificationError):
                    load_hermes_child_receipt(home, "run-1", "2026-08-27T00:00:00Z")

    def test_ancestor_swap_during_receipt_open_never_returns_alternate(self) -> None:
        run_dir = write_observed_receipt(self.omh_home, "run-1")
        alternate = write_observed_receipt(self.omh_home, "alternate", "failed")
        safe_run_dir = run_dir.with_name("run-1-safe")
        original_open = os.open
        original_lstat = Path.lstat
        swapped = False
        race_triggered = False

        def racing_open(
            path: str | os.PathLike[str],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal race_triggered, swapped
            if not swapped and Path(path).name in {"observation.json", "observation.signature.json"}:
                run_dir.rename(safe_run_dir)
                run_dir.symlink_to(alternate, target_is_directory=True)
                swapped = True
                race_triggered = True
            return original_open(path, flags, mode, dir_fd=dir_fd)

        def restoring_lstat(path: Path) -> os.stat_result:
            nonlocal swapped
            metadata = original_lstat(path)
            if swapped and path.parent == run_dir and path.name in {"observation.json", "observation.signature.json"}:
                run_dir.unlink()
                safe_run_dir.rename(run_dir)
                swapped = False
            return metadata

        receipt_status = "rejected"
        try:
            with (
                patch("omh.system.secure_regular_file.os.open", side_effect=racing_open),
                patch.object(Path, "lstat", new=restoring_lstat),
            ):
                try:
                    receipt_status = load_hermes_child_receipt(
                        self.omh_home, "run-1", "2026-08-27T00:00:00Z"
                    ).status
                except ReceiptVerificationError as exc:
                    self.assertTrue(exc.reason)
            self.assertTrue(race_triggered)
            self.assertIn(receipt_status, {"completed", "rejected"})
        finally:
            if run_dir.is_symlink():
                run_dir.unlink()
            if safe_run_dir.exists():
                safe_run_dir.rename(run_dir)

    def test_rejects_ancestor_symlink_escape(self) -> None:
        real_parent = self.root / "real"
        real_parent.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(real_parent, target_is_directory=True)
        canonical_home = (real_parent / ".omh").resolve()
        write_observed_receipt(canonical_home, "run-1")
        with self.assertRaises(ReceiptVerificationError):
            load_hermes_child_receipt(alias / ".omh", "run-1", "2026-08-27T00:00:00Z")

    def test_rejects_symlinked_observation_or_signature_file(self) -> None:
        for filename in ("observation.json", "observation.signature.json"):
            with self.subTest(filename=filename), TemporaryDirectory(prefix="omh-receipt-link-") as raw:
                home = (Path(raw) / ".omh").resolve()
                run_dir = write_observed_receipt(home, "run-1")
                target = Path(raw) / filename
                (run_dir / filename).replace(target)
                (run_dir / filename).symlink_to(target)
                with self.assertRaises(ReceiptVerificationError):
                    load_hermes_child_receipt(home, "run-1", "2026-08-27T00:00:00Z")

    def test_reads_receipt_files_without_path_read_helpers(self) -> None:
        # Given
        write_observed_receipt(self.omh_home, "run-1")
        # When
        with (
            patch.object(Path, "read_text", side_effect=AssertionError("Path.read_text forbidden")),
            patch.object(Path, "read_bytes", side_effect=AssertionError("Path.read_bytes forbidden")),
        ):
            receipt = load_hermes_child_receipt(
                self.omh_home, "run-1", "2026-08-27T00:00:00Z"
            )
        # Then
        self.assertEqual(receipt.status, "completed")

    def test_rejects_oversized_observation_signature_and_key_files(self) -> None:
        for filename, size in (
            ("observation.json", 65_537),
            ("observation.signature.json", 4_097),
            (".observation-hmac-key", 33),
        ):
            with self.subTest(filename=filename), TemporaryDirectory(prefix="omh-receipt-size-") as raw:
                home = (Path(raw) / ".omh").resolve()
                run_dir = write_observed_receipt(home, "run-1")
                target = run_dir.parent / filename if filename.startswith(".") else run_dir / filename
                target.write_bytes(b"x" * size)
                with self.assertRaises(ReceiptVerificationError):
                    load_hermes_child_receipt(home, "run-1", "2026-08-27T00:00:00Z")

    def test_rejects_traversal_missing_short_and_symlinked_key_or_bad_signature(self) -> None:
        for run_id in ("../escape", "nested/run", r"..\\escape"):
            with self.subTest(run_id=run_id), self.assertRaises(ReceiptVerificationError):
                load_hermes_child_receipt(self.omh_home, run_id, "2026-08-27T00:00:00Z")
        run_dir = write_observed_receipt(self.omh_home, "run-1")
        signature = run_dir / "observation.signature.json"
        signature.write_text('{"schema_version":"hermes_child_observation_signature/v1","hmac_sha256":"bad"}')
        with self.assertRaises(ReceiptVerificationError):
            load_hermes_child_receipt(self.omh_home, "run-1", "2026-08-27T00:00:00Z")
        for key_case in ("missing", "short", "symlink"):
            with self.subTest(key_case=key_case), TemporaryDirectory(prefix="omh-key-case-") as raw:
                home = (Path(raw) / ".omh").resolve()
                candidate_dir = write_observed_receipt(home, "run-1")
                key = candidate_dir.parent / ".observation-hmac-key"
                key.unlink()
                if key_case == "short":
                    key.write_bytes(b"short")
                elif key_case == "symlink":
                    outside = Path(raw) / "key"
                    outside.write_bytes(b"k" * 32)
                    key.symlink_to(outside)
                with self.assertRaises(ReceiptVerificationError):
                    load_hermes_child_receipt(home, "run-1", "2026-08-27T00:00:00Z")


class ObservationKeyCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(prefix="omh-observation-key-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_two_creators_at_one_home_read_the_same_whole_key(self) -> None:
        # Park one creator between drawing its key bytes and publishing them,
        # then run a second creator to completion at the same root. Within
        # this function os.urandom is called once per creator, for the key.
        #
        # The two assertions below pin the two properties the key needs, and
        # each catches a different wrong fix. Against the create-then-write
        # shape the parked creator has already made the key path exist at
        # length zero, so the second reads a short key and raises: the "whole
        # key" assertion bites. Against a temporary published with os.replace
        # the parked creator overwrites the key the second already returned
        # and signed with, so the "same key" assertion bites.
        parked = threading.Event()
        release = threading.Event()
        real_urandom = os.urandom
        claim = threading.Lock()
        claimed = False

        def gated_urandom(size: int) -> bytes:
            nonlocal claimed
            with claim:
                park, claimed = not claimed, True
            if park:
                parked.set()
                release.wait(timeout=10.0)
            return real_urandom(size)

        outcomes: dict[str, bytes | BaseException] = {}

        def create(name: str) -> None:
            try:
                outcomes[name] = load_or_create_observation_key(self.root)
            except BaseException as exc:  # re-raised through the assertions below
                outcomes[name] = exc

        with patch.object(hermes_child_receipts.os, "urandom", gated_urandom):
            parked_creator = threading.Thread(target=create, args=("parked",))
            parked_creator.start()
            self.assertTrue(parked.wait(timeout=10.0), "no creator drew key bytes")
            create("second")
            release.set()
            parked_creator.join(timeout=10.0)
            self.assertFalse(parked_creator.is_alive(), "parked creator never finished")

        for name, outcome in sorted(outcomes.items()):
            with self.subTest(creator=name):
                self.assertIsInstance(outcome, bytes, f"{name} creator raised: {outcome!r}")
                self.assertEqual(len(outcome), hermes_child_receipts._KEY_BYTES)
        self.assertEqual(outcomes["parked"], outcomes["second"])

    def test_creating_the_key_leaves_no_temporary_beside_it(self) -> None:
        key = load_or_create_observation_key(self.root)
        again = load_or_create_observation_key(self.root)

        self.assertEqual(key, again)
        self.assertEqual(
            sorted(entry.name for entry in self.root.iterdir()),
            [".observation-hmac-key"],
        )


if __name__ == "__main__":
    unittest.main()

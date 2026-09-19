"""Contracts for `reusable_behavior_map/v1` (issue #796).

Grouped by acceptance criterion:

- AC1: the same bounded evidence produces a deterministic behavior inventory,
  ordering included. Proved twice -- over a literal cited audit, and end to end
  over a real bounded scan of a real directory.
- AC2: all six classifications are representable and mutually distinguishable,
  and an unknown classification is refused.
- AC3: a behavior marked `covered` without an OMH capability citation fails
  validation, and a citation that does not resolve against this repository fails
  the same way.

Plus the two guards the family exists for: the map cites a `plugin_risk_audit/v1`
rather than re-scanning, so a map citing no audit is refused; and nothing in a
map may read as the inspected bundle having been executed or installed.

Only one test writes a file, and it writes through `atomic_write_text` rather
than `Path.write_text`: the bytes it writes are scanned and counted by the audit
the map then cites, and `Path.write_text` rewrites "\\n" as CRLF on Windows,
which would move the byte count the assertions are about.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from _local_package import load_local_package

load_local_package()

from omh.local_store import atomic_write_text  # noqa: E402
from omh.quality.popular_plugin_coverage import POPULAR_PLUGIN_FAMILIES  # noqa: E402
from omh.workflows.native_capability_blueprint import SOURCE_HOST_MECHANICS  # noqa: E402
from omh.workflows.plugin_risk_audit import (  # noqa: E402
    PLUGIN_RISK_AUDIT_SCHEMA_VERSION,
    audit_plugin_risk,
)
from omh.workflows.reusable_behavior_map import (  # noqa: E402
    AUDIT_MANIFEST_STATUSES,
    AUDIT_RISK_CATEGORIES,
    BEHAVIOR_CLASSIFICATIONS,
    BEHAVIOR_EVIDENCE_FIELDS,
    BEHAVIOR_KEYS,
    CLAIM_BOUNDARY,
    CLASSIFICATION_REQUIRED_EVIDENCE,
    MAP_DIGEST_KEYS,
    MAP_PRIVACY,
    NOT_OBSERVED,
    REUSABLE_BEHAVIOR_MAP_KEYS,
    REUSABLE_BEHAVIOR_MAP_SCHEMA_VERSION,
    SOURCE_AUDIT_KEYS,
    ReusableBehaviorMapError,
    build_reusable_behavior_map,
    map_digest_of,
    omh_capability_vocabulary,
    unresolved_capability_ids,
    validate_reusable_behavior_map,
)

from _platform_support import requires_secure_dir_io  # noqa: E402


# The risk categories the fixture audit reports. Every behavior that cites a
# risk cites one of these, because the map may not invent a finding the audit
# did not make.
CITED_RISKS = ["hermes_hook_capability", "network_request", "process_execution"]

# One canonical value per evidence field, so a row can be assembled from a
# classification's required set alone. Used to drive the six-by-six matrix.
CANONICAL_EVIDENCE: dict[str, list[str]] = {
    "reusable_procedure": [
        "Take the workspace path the person named.",
        "Summarize what changed and hand back the summary.",
    ],
    "omh_capability_ids": ["workspace-audit"],
    "host_mechanics": ["host_hook", "host_setting"],
    "risk_categories": ["network_request"],
    "missing_evidence": ["The bundle ships no description of what its hook does."],
}


def audit_payload(**overrides: Any) -> dict[str, Any]:
    """A `plugin_risk_audit/v1` payload in the shape `audit_plugin_risk` returns."""
    payload: dict[str, Any] = {
        "schema_version": PLUGIN_RISK_AUDIT_SCHEMA_VERSION,
        "source": {"explicit_root": True, "manifest_status": "present"},
        "summary": {
            "scanned_file_count": 4,
            "scanned_byte_count": 812,
            "risk_categories": list(CITED_RISKS),
            "risk_category_count": len(CITED_RISKS),
        },
        "not_observed": {"plugin_execution": {"status": "not_observed"}},
        "claim_boundary": "The audit statically reads bounded text from one explicitly named local directory.",
    }
    payload.update(overrides)
    return payload


def behavior(classification: str = "reusable", **overrides: Any) -> dict[str, Any]:
    """One row carrying exactly the evidence its classification requires."""
    row: dict[str, Any] = {
        "behavior_id": f"observed-{classification.replace('_', '-')}",
        "classification": classification,
        "user_outcome": "The person gets a written summary of what a workspace directory contains.",
        "independence_note": "OMH would read the directory through its own bounded reader and render a card.",
    }
    for field in CLASSIFICATION_REQUIRED_EVIDENCE.get(classification, ()):
        row[field] = list(CANONICAL_EVIDENCE[field])
    row.update(overrides)
    return row


def evidence_shaped_row(shape: str, *, classification: str) -> dict[str, Any]:
    """A row carrying `shape`'s evidence while claiming to be `classification`."""
    row = behavior(classification)
    for field in BEHAVIOR_EVIDENCE_FIELDS:
        row.pop(field, None)
    for field in CLASSIFICATION_REQUIRED_EVIDENCE[shape]:
        row[field] = list(CANONICAL_EVIDENCE[field])
    row["behavior_id"] = "observed-behavior"
    return row


def behavior_map(*, behaviors: Any = None, audit: Any = ..., **overrides: Any) -> dict[str, Any]:
    return build_reusable_behavior_map(
        audit=audit_payload() if audit is ... else audit,
        behaviors=[behavior()] if behaviors is None else behaviors,
        **overrides,
    )


def every_classification() -> list[dict[str, Any]]:
    return [behavior(name) for name in BEHAVIOR_CLASSIFICATIONS]


def resealed(payload: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """A payload edited after minting, with the digest re-derived to match.

    Lets a test fail on the rule under test rather than on the tamper check.
    """
    edited = {**payload, **overrides}
    edited["map_digest"] = map_digest_of(edited)
    return edited


class DeterministicInventoryTests(unittest.TestCase):
    """AC1: the same bounded evidence produces a deterministic behavior inventory."""

    def test_repeated_builds_over_the_same_evidence_are_identical(self) -> None:
        first = behavior_map(behaviors=every_classification())
        second = behavior_map(behaviors=every_classification())

        self.assertEqual(first, second)
        self.assertEqual(list(first), list(second))
        self.assertEqual(json.dumps(first), json.dumps(second))
        self.assertEqual(first["map_digest"], second["map_digest"])

    def test_the_order_the_behaviors_arrived_in_does_not_change_the_map(self) -> None:
        rows = every_classification()

        forward = behavior_map(behaviors=rows)
        backward = behavior_map(behaviors=list(reversed(rows)))

        self.assertEqual(json.dumps(forward), json.dumps(backward))
        self.assertEqual(
            [row["behavior_id"] for row in forward["behaviors"]],
            sorted(row["behavior_id"] for row in forward["behaviors"]),
        )

    def test_every_row_renders_its_keys_in_one_order(self) -> None:
        payload = behavior_map(behaviors=every_classification())

        for row in payload["behaviors"]:
            with self.subTest(behavior=row["behavior_id"]):
                self.assertEqual(list(row), sorted(BEHAVIOR_KEYS))

    def test_the_digest_covers_the_findings_and_not_the_clock(self) -> None:
        morning = behavior_map(prepared_at="2026-08-09T09:00:00Z")
        evening = behavior_map(prepared_at="2026-08-09T21:00:00Z")

        self.assertEqual(morning["map_digest"], evening["map_digest"])
        self.assertNotEqual(morning["prepared_at"], evening["prepared_at"])
        self.assertNotIn("prepared_at", MAP_DIGEST_KEYS)

    def test_prepared_at_defaults_to_empty_so_nothing_reads_a_clock(self) -> None:
        payload = behavior_map()

        self.assertEqual(payload["prepared_at"], "")
        self.assertEqual(validate_reusable_behavior_map(payload), [])

    def test_a_changed_finding_changes_the_digest(self) -> None:
        one = behavior_map(behaviors=[behavior("reusable")])
        other = behavior_map(behaviors=[behavior("irrelevant")])

        self.assertNotEqual(one["map_digest"], other["map_digest"])

    def test_the_summary_is_derived_and_a_hand_edited_count_is_refused(self) -> None:
        payload = behavior_map(behaviors=every_classification())

        self.assertEqual(payload["summary"]["behavior_count"], len(BEHAVIOR_CLASSIFICATIONS))
        self.assertEqual(
            payload["summary"]["classification_counts"],
            {name: 1 for name in BEHAVIOR_CLASSIFICATIONS},
        )
        self.assertEqual(payload["summary"]["cited_capability_ids"], CANONICAL_EVIDENCE["omh_capability_ids"])
        self.assertEqual(payload["summary"]["cited_risk_categories"], CANONICAL_EVIDENCE["risk_categories"])

        inflated = dict(payload["summary"])
        inflated["behavior_count"] = 99
        errors = validate_reusable_behavior_map(resealed(payload, summary=inflated))

        self.assertTrue(any("summary must be derived" in error for error in errors), errors)

    def test_every_classification_appears_in_the_counts_even_at_zero(self) -> None:
        payload = behavior_map(behaviors=[behavior("reusable")])

        self.assertEqual(sorted(payload["summary"]["classification_counts"]), sorted(BEHAVIOR_CLASSIFICATIONS))
        self.assertEqual(payload["summary"]["classification_counts"]["irrelevant"], 0)

    def test_a_map_out_of_behavior_id_order_is_refused(self) -> None:
        payload = behavior_map(behaviors=[behavior("reusable"), behavior("covered")])
        reordered = list(reversed(payload["behaviors"]))

        errors = validate_reusable_behavior_map(
            resealed(payload, behaviors=reordered, summary=payload["summary"])
        )

        self.assertTrue(any("sorted by behavior_id" in error for error in errors), errors)

    def test_a_repeated_behavior_id_is_refused(self) -> None:
        with self.assertRaises(ReusableBehaviorMapError) as raised:
            behavior_map(behaviors=[behavior("reusable"), behavior("reusable")])

        self.assertIn("must not repeat a behavior_id", str(raised.exception))

    def test_a_map_with_no_behaviors_is_refused(self) -> None:
        with self.assertRaises(ReusableBehaviorMapError) as raised:
            behavior_map(behaviors=[])

        self.assertIn("at least 1 observed behavior", str(raised.exception))


class BoundedEvidenceEndToEndTests(unittest.TestCase):
    """AC1 again, over a real bounded scan rather than a literal audit payload."""

    @requires_secure_dir_io
    def test_one_bounded_scan_produces_one_inventory_however_often_it_is_built(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            atomic_write_text(root / "plugin.json", '{"name": "example"}\n')
            atomic_write_text(
                root / "hook.py",
                "import requests\n"
                "def on_session_end() -> None:\n"
                "    requests.get('https://example.invalid')\n",
            )

            audit = audit_plugin_risk(root)
            rows = [
                behavior("reusable"),
                behavior("risky", risk_categories=["hermes_hook_capability", "network_request"]),
            ]
            first = build_reusable_behavior_map(audit=audit, behaviors=rows)
            second = build_reusable_behavior_map(audit=audit, behaviors=list(reversed(rows)))

        self.assertEqual(json.dumps(first), json.dumps(second))
        self.assertEqual(first["source_audit"]["schema_version"], PLUGIN_RISK_AUDIT_SCHEMA_VERSION)
        self.assertEqual(first["source_audit"]["manifest_status"], audit["source"]["manifest_status"])
        self.assertEqual(first["source_audit"]["scanned_file_count"], audit["summary"]["scanned_file_count"])
        self.assertEqual(first["source_audit"]["scanned_byte_count"], audit["summary"]["scanned_byte_count"])
        self.assertEqual(first["source_audit"]["risk_categories"], audit["summary"]["risk_categories"])
        # The audit refuses to disclose the directory it read; the map inherits
        # that. Both sides are resolved paths, so this compares like with like.
        self.assertNotIn(str(root), json.dumps(first))
        self.assertNotIn("requests.get", json.dumps(first))

    @requires_secure_dir_io
    def test_a_behavior_may_not_cite_a_risk_the_bounded_scan_never_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            atomic_write_text(root / "plugin.json", '{"name": "quiet"}\n')

            audit = audit_plugin_risk(root)

            # No root plugin.yaml, so the hook contract was never established;
            # what the scan did NOT find is still nothing.
            self.assertEqual(audit["summary"]["risk_categories"], ["undetermined_hook_contract"])
            with self.assertRaises(ReusableBehaviorMapError) as raised:
                build_reusable_behavior_map(audit=audit, behaviors=[behavior("risky")])

        self.assertIn("absent from the cited audit", str(raised.exception))


class SixClassificationTests(unittest.TestCase):
    """AC2: six states, all representable, all distinguishable, no seventh."""

    def test_the_vocabulary_is_the_six_states_the_issue_names(self) -> None:
        self.assertEqual(
            list(BEHAVIOR_CLASSIFICATIONS),
            ["reusable", "covered", "host_bound", "risky", "unclear", "irrelevant"],
        )
        self.assertEqual(sorted(CLASSIFICATION_REQUIRED_EVIDENCE), sorted(BEHAVIOR_CLASSIFICATIONS))

    def test_all_six_classifications_are_representable_in_one_map(self) -> None:
        payload = behavior_map(behaviors=every_classification())

        self.assertEqual(validate_reusable_behavior_map(payload), [])
        self.assertEqual(
            [row["classification"] for row in payload["behaviors"]],
            sorted(BEHAVIOR_CLASSIFICATIONS, key=lambda name: behavior(name)["behavior_id"]),
        )
        self.assertEqual(len({row["classification"] for row in payload["behaviors"]}), 6)

    def test_each_classification_has_its_own_evidence_signature(self) -> None:
        signatures = {name: frozenset(fields) for name, fields in CLASSIFICATION_REQUIRED_EVIDENCE.items()}

        self.assertEqual(len(set(signatures.values())), len(BEHAVIOR_CLASSIFICATIONS))
        for name, signature in signatures.items():
            with self.subTest(classification=name):
                self.assertTrue(signature <= set(BEHAVIOR_EVIDENCE_FIELDS))

    def test_each_evidence_shape_validates_under_exactly_one_classification(self) -> None:
        for shape in BEHAVIOR_CLASSIFICATIONS:
            for verdict in BEHAVIOR_CLASSIFICATIONS:
                row = evidence_shaped_row(shape, classification=verdict)
                with self.subTest(shape=shape, verdict=verdict):
                    if shape == verdict:
                        payload = behavior_map(behaviors=[row])
                        self.assertEqual(validate_reusable_behavior_map(payload), [])
                        continue
                    with self.assertRaises(ReusableBehaviorMapError):
                        behavior_map(behaviors=[row])

    def test_a_classification_that_is_missing_its_evidence_is_refused(self) -> None:
        for name, required in CLASSIFICATION_REQUIRED_EVIDENCE.items():
            for field in required:
                with self.subTest(classification=name, field=field):
                    with self.assertRaises(ReusableBehaviorMapError) as raised:
                        behavior_map(behaviors=[behavior(name, **{field: []})])
                    self.assertIn(f"classified {name} must name {field}", str(raised.exception))

    def test_a_classification_carrying_evidence_it_does_not_require_is_refused(self) -> None:
        for name, required in CLASSIFICATION_REQUIRED_EVIDENCE.items():
            for field in BEHAVIOR_EVIDENCE_FIELDS:
                if field in required:
                    continue
                with self.subTest(classification=name, field=field):
                    with self.assertRaises(ReusableBehaviorMapError) as raised:
                        behavior_map(behaviors=[behavior(name, **{field: list(CANONICAL_EVIDENCE[field])})])
                    self.assertIn(f"classified {name} must not name {field}", str(raised.exception))

    def test_an_unknown_classification_is_refused(self) -> None:
        for unknown in ("supported", "implemented", "maybe", "Reusable", "host-bound", ""):
            with self.subTest(classification=unknown):
                with self.assertRaises(ReusableBehaviorMapError) as raised:
                    behavior_map(behaviors=[evidence_shaped_row("reusable", classification=unknown)])
                message = str(raised.exception)
                self.assertIn("classification is unsupported", message)
                self.assertIn("reusable", message)
                self.assertIn("irrelevant", message)

    def test_every_row_states_a_user_outcome_and_an_independence_note(self) -> None:
        for name in BEHAVIOR_CLASSIFICATIONS:
            for field in ("user_outcome", "independence_note"):
                with self.subTest(classification=name, field=field):
                    with self.assertRaises(ReusableBehaviorMapError) as raised:
                        behavior_map(behaviors=[behavior(name, **{field: "  "})])
                    self.assertIn(f"{field} must be a non-empty string", str(raised.exception))

    def test_a_host_bound_exclusion_speaks_the_blueprint_mechanic_vocabulary(self) -> None:
        # Deliberate reuse: a behavior excluded here and a blueprint's observed
        # source mechanics there name the same things in the same words.
        for mechanic in SOURCE_HOST_MECHANICS:
            with self.subTest(mechanic=mechanic):
                payload = behavior_map(behaviors=[behavior("host_bound", host_mechanics=[mechanic])])
                self.assertEqual(validate_reusable_behavior_map(payload), [])

        with self.assertRaises(ReusableBehaviorMapError) as raised:
            behavior_map(behaviors=[behavior("host_bound", host_mechanics=["host_thing"])])

        self.assertIn("host_mechanics has unsupported entries", str(raised.exception))


class CoverageCitationTests(unittest.TestCase):
    """AC3: no pattern is called supported without an OMH capability citation."""

    def test_covered_without_a_capability_citation_fails_validation(self) -> None:
        with self.assertRaises(ReusableBehaviorMapError) as raised:
            behavior_map(behaviors=[behavior("covered", omh_capability_ids=[])])

        self.assertIn("classified covered must name omh_capability_ids", str(raised.exception))

    def test_covered_with_a_capability_citation_passes(self) -> None:
        payload = behavior_map(behaviors=[behavior("covered")])

        self.assertEqual(validate_reusable_behavior_map(payload), [])
        self.assertEqual(payload["behaviors"][0]["omh_capability_ids"], ["workspace-audit"])

    def test_a_well_formed_citation_that_names_nothing_in_this_repo_is_refused(self) -> None:
        for invented in ("universal-plugin-bridge", "bundle-runner", "reusable-behavior-map"):
            with self.subTest(capability_id=invented):
                with self.assertRaises(ReusableBehaviorMapError) as raised:
                    behavior_map(behaviors=[behavior("covered", omh_capability_ids=[invented])])
                message = str(raised.exception)
                self.assertIn("do not name an OMH capability", message)
                self.assertIn(invented, message)
                self.assertIn("popular_plugin_coverage.py", message)

    def test_the_capability_vocabulary_is_re_derived_from_this_repository(self) -> None:
        expected = {family.family_id for family in POPULAR_PLUGIN_FAMILIES}
        expected.update(case_id for family in POPULAR_PLUGIN_FAMILIES for case_id in family.case_ids)

        self.assertEqual(list(omh_capability_vocabulary()), sorted(expected))
        self.assertEqual(unresolved_capability_ids(sorted(expected)), ())

    def test_every_capability_in_the_vocabulary_is_citable(self) -> None:
        for capability_id in omh_capability_vocabulary():
            with self.subTest(capability_id=capability_id):
                payload = behavior_map(behaviors=[behavior("covered", omh_capability_ids=[capability_id])])
                self.assertEqual(validate_reusable_behavior_map(payload), [])

    def test_only_a_covered_behavior_may_cite_a_capability(self) -> None:
        for name in BEHAVIOR_CLASSIFICATIONS:
            if name == "covered":
                continue
            with self.subTest(classification=name):
                with self.assertRaises(ReusableBehaviorMapError) as raised:
                    behavior_map(behaviors=[behavior(name, omh_capability_ids=["workspace-audit"])])
                self.assertIn("must not name omh_capability_ids", str(raised.exception))

    def test_a_covered_behavior_also_states_the_procedure_it_covers(self) -> None:
        with self.assertRaises(ReusableBehaviorMapError) as raised:
            behavior_map(behaviors=[behavior("covered", reusable_procedure=[])])

        self.assertIn("classified covered must name reusable_procedure", str(raised.exception))

    def test_the_summary_collects_every_cited_capability(self) -> None:
        payload = behavior_map(
            behaviors=[
                behavior("covered", behavior_id="observed-one", omh_capability_ids=["workspace-audit"]),
                behavior("covered", behavior_id="observed-two", omh_capability_ids=["research", "file-lookup"]),
            ]
        )

        self.assertEqual(
            payload["summary"]["cited_capability_ids"], ["file-lookup", "research", "workspace-audit"]
        )


class CitedAuditTests(unittest.TestCase):
    """The map cites a bounded audit. No audit, no map."""

    def test_a_map_citing_no_audit_is_refused(self) -> None:
        for missing in (None, {}, {"schema_version": ""}, []):
            with self.subTest(audit=missing):
                with self.assertRaises(ReusableBehaviorMapError) as raised:
                    behavior_map(audit=missing)
                self.assertIn(f"must cite a {PLUGIN_RISK_AUDIT_SCHEMA_VERSION} payload", str(raised.exception))

    def test_a_map_citing_something_other_than_a_risk_audit_is_refused(self) -> None:
        with self.assertRaises(ReusableBehaviorMapError) as raised:
            behavior_map(audit=audit_payload(schema_version="awesome_hermes_plugin_outcome_matrix/v1"))

        message = str(raised.exception)
        self.assertIn(f"must cite a {PLUGIN_RISK_AUDIT_SCHEMA_VERSION} payload", message)
        self.assertIn("never scans the bundle itself", message)

    def test_the_citation_keeps_how_much_of_the_bundle_the_audit_saw(self) -> None:
        payload = behavior_map()

        self.assertEqual(sorted(payload["source_audit"]), sorted(SOURCE_AUDIT_KEYS))
        self.assertEqual(payload["source_audit"]["scanned_file_count"], 4)
        self.assertEqual(payload["source_audit"]["scanned_byte_count"], 812)
        self.assertEqual(payload["source_audit"]["risk_categories"], CITED_RISKS)

    def test_a_malformed_audit_count_is_a_validation_error_not_an_invented_count(self) -> None:
        for value in ("many", -1, None, True):
            with self.subTest(scanned_file_count=value):
                broken = audit_payload()
                broken["summary"] = {**broken["summary"], "scanned_file_count": value}
                with self.assertRaises(ReusableBehaviorMapError) as raised:
                    behavior_map(audit=broken)
                self.assertIn("scanned_file_count must be a non-negative integer", str(raised.exception))

    def test_an_unsupported_manifest_status_is_refused(self) -> None:
        broken = audit_payload(source={"explicit_root": True, "manifest_status": "probably_fine"})

        with self.assertRaises(ReusableBehaviorMapError) as raised:
            behavior_map(audit=broken)

        self.assertIn("manifest_status is unsupported", str(raised.exception))

    def test_every_manifest_status_the_audit_can_report_is_citable(self) -> None:
        for status in AUDIT_MANIFEST_STATUSES:
            with self.subTest(manifest_status=status):
                payload = behavior_map(
                    audit=audit_payload(source={"explicit_root": True, "manifest_status": status})
                )
                self.assertEqual(validate_reusable_behavior_map(payload), [])

    def test_a_risky_behavior_may_only_cite_a_category_the_audit_observed(self) -> None:
        for category in AUDIT_RISK_CATEGORIES:
            with self.subTest(risk_category=category):
                row = behavior("risky", risk_categories=[category])
                if category in CITED_RISKS:
                    self.assertEqual(validate_reusable_behavior_map(behavior_map(behaviors=[row])), [])
                    continue
                with self.assertRaises(ReusableBehaviorMapError) as raised:
                    behavior_map(behaviors=[row])
                message = str(raised.exception)
                self.assertIn("absent from the cited audit", message)
                self.assertIn(PLUGIN_RISK_AUDIT_SCHEMA_VERSION, message)

    def test_a_risk_category_the_audit_cannot_report_is_refused(self) -> None:
        with self.assertRaises(ReusableBehaviorMapError) as raised:
            behavior_map(behaviors=[behavior("risky", risk_categories=["reads_your_email"])])

        self.assertIn("risk_categories has unsupported entries", str(raised.exception))

    def test_the_risk_vocabulary_is_the_audits_own(self) -> None:
        self.assertEqual(
            list(AUDIT_RISK_CATEGORIES),
            [
                "declared_dependency",
                "dynamic_code_execution",
                "hermes_hook_capability",
                "network_request",
                "potential_committed_secret",
                "process_execution",
                "self_update_or_code_replacement",
                "undetermined_hook_contract",
                "undetermined_self_update_path",
            ],
        )
        self.assertEqual(list(AUDIT_MANIFEST_STATUSES), ["invalid_json", "missing", "present"])


class TheBundleIsNeverRunTests(unittest.TestCase):
    """Nothing in a map reads as the inspected bundle having been run."""

    def test_the_map_records_every_bundle_interaction_as_not_observed(self) -> None:
        payload = behavior_map(behaviors=every_classification())

        self.assertEqual(payload["not_observed"], NOT_OBSERVED)
        for interaction in ("bundle_import", "bundle_registration", "bundle_installation", "bundle_execution"):
            with self.subTest(interaction=interaction):
                self.assertEqual(payload["not_observed"][interaction]["status"], "not_observed")

    def test_a_map_that_drops_an_unobserved_interaction_is_refused(self) -> None:
        payload = behavior_map()
        for interaction in NOT_OBSERVED:
            with self.subTest(interaction=interaction):
                thinned = {key: value for key, value in NOT_OBSERVED.items() if key != interaction}
                errors = validate_reusable_behavior_map(resealed(payload, not_observed=thinned))
                self.assertTrue(any("not_observed must record" in error for error in errors), errors)

    def test_a_map_claiming_the_bundle_ran_is_refused_by_key_name(self) -> None:
        for key in ("executed", "installed", "ran", "exit_code", "result", "verified"):
            with self.subTest(key=key):
                errors = validate_reusable_behavior_map({**behavior_map(), key: True})
                self.assertTrue(
                    any("implementation-claim keys" in error and key in error for error in errors), errors
                )

    def test_a_behavior_claiming_the_bundle_ran_is_refused_by_key_name(self) -> None:
        for key in ("executed", "installed", "succeeded"):
            with self.subTest(key=key):
                with self.assertRaises(ReusableBehaviorMapError) as raised:
                    behavior_map(behaviors=[behavior("reusable", **{key: True})])
                message = str(raised.exception)
                self.assertIn("implementation-claim keys", message)
                self.assertIn(key, message)

    def test_a_raw_or_hidden_key_is_refused_on_the_map_and_on_a_behavior(self) -> None:
        errors = validate_reusable_behavior_map({**behavior_map(), "transcript": "..."})
        self.assertTrue(any("raw or hidden keys" in error for error in errors), errors)

        with self.assertRaises(ReusableBehaviorMapError) as raised:
            behavior_map(behaviors=[behavior("reusable", raw_output="...")])
        self.assertIn("raw or hidden keys", str(raised.exception))

    def test_the_claim_boundary_states_the_bundle_is_never_run(self) -> None:
        payload = behavior_map()

        self.assertEqual(payload["claim_boundary"], CLAIM_BOUNDARY)
        self.assertEqual(payload["privacy"], MAP_PRIVACY)
        for phrase in ("does not import, install, register, or execute", "does not install its dependencies"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, CLAIM_BOUNDARY)

        errors = validate_reusable_behavior_map(resealed(payload, claim_boundary="Audited the bundle."))
        self.assertTrue(any("claim_boundary must state" in error for error in errors), errors)


class ClosedKeySetTests(unittest.TestCase):
    """The envelope and every row are closed in both directions."""

    def test_the_map_carries_exactly_its_keys(self) -> None:
        payload = behavior_map()

        self.assertEqual(sorted(payload), sorted(REUSABLE_BEHAVIOR_MAP_KEYS))
        self.assertEqual(payload["schema_version"], REUSABLE_BEHAVIOR_MAP_SCHEMA_VERSION)

        extra = validate_reusable_behavior_map({**payload, "bundle_manifest": "x"})
        self.assertTrue(any("unsupported keys" in error for error in extra), extra)

        for key in REUSABLE_BEHAVIOR_MAP_KEYS:
            with self.subTest(key=key):
                short = {name: value for name, value in payload.items() if name != key}
                errors = validate_reusable_behavior_map(short)
                self.assertTrue(any("is missing keys" in error and key in error for error in errors), errors)

    def test_a_behavior_carries_exactly_its_keys(self) -> None:
        payload = behavior_map()
        row = payload["behaviors"][0]

        self.assertEqual(sorted(row), sorted(BEHAVIOR_KEYS))

        with self.assertRaises(ReusableBehaviorMapError) as raised:
            behavior_map(behaviors=[behavior("reusable", host_namespace="acme")])
        self.assertIn("has unsupported keys", str(raised.exception))

        for key in BEHAVIOR_KEYS:
            with self.subTest(key=key):
                short = {name: value for name, value in row.items() if name != key}
                errors = validate_reusable_behavior_map(resealed(payload, behaviors=[short]))
                self.assertTrue(any("is missing keys" in error and key in error for error in errors), errors)

    def test_a_behavior_id_must_be_a_lowercase_slug(self) -> None:
        for identifier in ("Observed Behavior", "x", "observed_behavior", "-leading"):
            with self.subTest(behavior_id=identifier):
                with self.assertRaises(ReusableBehaviorMapError) as raised:
                    behavior_map(behaviors=[behavior("reusable", behavior_id=identifier)])
                self.assertIn("behavior_id must be a lowercase slug", str(raised.exception))

    def test_an_edited_map_no_longer_matches_its_digest(self) -> None:
        payload = behavior_map()
        tampered = {**payload, "prepared_at": "2026-08-09T00:00:00Z", "summary": {"behavior_count": 0}}

        errors = validate_reusable_behavior_map(tampered)

        self.assertTrue(any("map_digest does not match" in error for error in errors), errors)

    def test_a_non_object_is_not_a_map(self) -> None:
        self.assertEqual(validate_reusable_behavior_map([]), ["reusable_behavior_map must be an object"])

    def test_behaviors_must_be_a_list_of_objects(self) -> None:
        payload = behavior_map()
        errors = validate_reusable_behavior_map(resealed(payload, behaviors=["reusable"]))

        self.assertTrue(any("behaviors must be a list of objects" in error for error in errors), errors)

    def test_a_string_where_a_list_belongs_is_refused_rather_than_split(self) -> None:
        with self.assertRaises(ReusableBehaviorMapError) as raised:
            behavior_map(behaviors=[behavior("reusable", reusable_procedure="Read the file.")])

        self.assertIn("reusable_procedure must be a list of strings", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

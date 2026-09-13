from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()

from omh.coding.model_portfolio_qualification import (  # noqa: E402
    PORTFOLIO_DISPOSITIONS,
    build_model_portfolio_qualification,
)
from omh.coding.model_recommendations import SHIPPED_MODEL_RECOMMENDATIONS  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures/model_portfolio_seed_inventory.json"
SECTIONS = ("categories", "role_suggestions", "domain_affinities", "last_resort")


def rows_for(*models, **kwargs):
    report = build_model_portfolio_qualification({"models": list(models)}, **kwargs)
    return {row["requested_model"]: row for row in report["comparison"]["models"]}


def assert_shipped_qualified(test, catalog):
    candidates = [candidate for section in SECTIONS
                  for chain in catalog[section].values() for candidate in chain]
    rows = rows_for(*(candidate["model_alias"] for candidate in candidates))
    for candidate in candidates:
        alias = candidate["model_alias"]
        test.assertEqual(rows[alias]["disposition"], "recommended", alias)
        test.assertTrue(candidate["reasoning"].strip(), alias)
        test.assertTrue(rows[alias]["recommendation_eligibility"], alias)


class PortfolioTests(unittest.TestCase):
    def test_complete_seed_and_new_discovery_without_row_changes(self):
        inventory = json.loads(FIXTURE.read_text())
        original = deepcopy(inventory)
        report = build_model_portfolio_qualification(inventory)
        self.assertEqual(inventory, original)
        self.assertEqual(report["schema_version"], "model_portfolio_qualification/v1")
        self.assertEqual(report["comparison"]["summary"]["total_models"], 42)
        inventory["models"].append("acme/acme-1")
        expanded = build_model_portfolio_qualification(inventory)
        rows = {r["requested_model"]: r for r in expanded["comparison"]["models"]}
        self.assertEqual(len(rows), 43)
        for row in report["comparison"]["models"]:
            self.assertEqual(rows[row["requested_model"]], row)
        self.assertEqual(rows["acme/acme-1"]["disposition"], "unmeasured")
        self.assertEqual(rows["acme/acme-1"]["evidence_state"], "unmeasured")
        self.assertFalse(rows["acme/acme-1"]["recommendation_eligibility"])

    def test_deterministic_union_preserves_all_identities(self):
        inventory = {
            "models": ["qwen/qwen3.8-flash", "QWEN/Qwen3.8-Flash"],
            "available_models": [{"provider": "openai", "model_id": "gpt-6-astra"}],
            "model_discovery": {"observations": [{"model_id": "acme/acme-1"}]},
        }
        first = build_model_portfolio_qualification(inventory)
        inventory["models"].reverse()
        self.assertEqual(first, build_model_portfolio_qualification(inventory))
        comparison = first["comparison"]
        self.assertEqual(comparison["summary"]["total_models"], 3)
        row = next(r for r in comparison["models"] if r["family"] == "qwen")
        self.assertEqual(set(row["aliases"]), {"qwen/qwen3.8-flash", "QWEN/Qwen3.8-Flash"})
        self.assertEqual(row["recognition_mode"], "family")
        stable = json.dumps(comparison, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        self.assertEqual(first["comparison_digest"], sha256(stable.encode()).hexdigest())

    def test_closed_dimensions_and_evidence_states(self):
        report = build_model_portfolio_qualification(json.loads(FIXTURE.read_text()))
        for row in report["comparison"]["models"]:
            with self.subTest(model=row["requested_model"]):
                self.assertIn(row["disposition"], PORTFOLIO_DISPOSITIONS)
                self.assertIn(row["recognition_mode"], {"exact", "family", "generic_fallback", "unknown"})
                self.assertIn(row["contract_coverage"], {"exact", "declared_inheritance", "intentional_exclusion", "missing"})
                self.assertIn(row["calibration_coverage"], {"exact", "family", "generic", "missing", "intentional_exclusion"})
                self.assertIn(row["evidence_state"], {"observed_role_evaluation", "editorial_not_measured", "unmeasured"})
                self.assertTrue(row["reason"])
                self.assertEqual(row["inventory_read_date"], "2026-09-13")
                self.assertTrue(row["model_version_boundary"])
                self.assertEqual(set(row["evidence"]), {"quality", "tool_reliability", "latency", "cost"})

    def test_recognition_does_not_claim_unknown_is_recognized(self):
        from omh.coding.model_contract_coverage import build_model_contract_coverage
        rows = rows_for("acme/acme-1", "qwen/qwen3.8-max-0902", "openai/gpt-6-astra")
        self.assertEqual(rows["acme/acme-1"]["recognition_mode"], "generic_fallback")
        self.assertEqual(rows["qwen/qwen3.8-max-0902"]["calibration_coverage"], "family")
        self.assertEqual(rows["openai/gpt-6-astra"]["recognition_mode"], "exact")
        audit = build_model_contract_coverage({"models": ["acme/acme-1"]})
        self.assertEqual(audit["comparison"]["models"][0]["dimensions"]["family_recognition"]["status"], "missing")

    def test_bounded_alias_contracts_and_exact_calibration(self):
        ids = ("gpt-6-astra", "gpt-6-astra-pro-flex", "gpt-6-astra-pro-max",
               "gpt-6-astra-turbo", "anthropic/claude-fable-5.1", "claude-fable-5-1",
               "gpt-6-astra-2026-09-03", "gpt-6-astra-pro-fast")
        rows = rows_for(*ids)
        self.assertEqual(rows[ids[0]]["calibration_coverage"], "exact")
        self.assertEqual(rows[ids[1]]["contract_coverage"], "declared_inheritance")
        self.assertEqual(rows[ids[1]]["contract_projection"]["reasoning_mode"], "pro")
        self.assertEqual(rows[ids[1]]["evidence"]["cost"]["service_tier_multiplier"], 0.5)
        self.assertEqual(rows[ids[7]]["evidence"]["cost"]["service_tier_multiplier"], 2.0)
        for model in ids[2:5]:
            self.assertEqual(rows[model]["contract_coverage"], "missing")
            self.assertEqual(rows[model]["disposition"], "unmeasured")
        self.assertEqual(rows[ids[4]]["aliases"], [ids[4]])
        self.assertEqual(rows[ids[6]]["contract_coverage"], "declared_inheritance")
        self.assertEqual(rows[ids[6]]["contract_projection"]["provenance"], "dated_snapshot")
        # Sharing a base contract is not permission to inherit a mode/tier placement.
        self.assertEqual(rows[ids[1]]["disposition"], "unmeasured")

    def test_deepseek_pointer_is_bidirectional_but_not_a_second_contract(self):
        ids = ("deepseek/deepseek-v4.1-flash", "deepseek-flash")
        rows = rows_for(*ids)
        for model in ids:
            row = rows[model]
            self.assertEqual(row["contract_model_id"], "deepseek-v4.1-flash")
            self.assertEqual(row["contract_projection"]["requested_model"], model)
            self.assertEqual(set(row["served_aliases"]), {"deepseek-flash", "deepseek-v4.1-flash"})
            self.assertEqual(row["disposition"], "recommended")

    def test_unresearched_families_are_explicitly_held_not_fabricated_failures(self):
        ids = ("minimax/minimax-m3", "xiaomi/mimo-v2.5-pro", "tencent/hy4-preview",
               "tencent/hy3", "stepfun/step-3.7-flash",
               "nvidia/nemotron-3-super-120b-a12b", "sakana/fugu-ultra")
        for row in rows_for(*ids).values():
            self.assertEqual(row["calibration_coverage"], "intentional_exclusion")
            self.assertEqual(row["disposition"], "unmeasured")
            self.assertEqual(row["evidence_state"], "unmeasured")
            self.assertTrue(row["decision"]["evidence_pointers"])
            self.assertFalse(row["recommendation_eligibility"])
        required = build_model_portfolio_qualification({"models": list(ids)}, required_models=ids)
        self.assertTrue(required["blocking"])
        self.assertEqual(set(required["comparison"]["summary"]["required_gaps"]), set(ids))

    def test_retirements_preserve_scoped_sol_last_resort(self):
        successors = {"claude-fable-5": "claude-fable-5-1", "glm-5.2": "glm-5.3",
                      "glm-5.2-ultrafast": "glm-5.3-flash", "deepseek-v3.2": "deepseek-v4.1-flash"}
        rows = rows_for(*successors, "gpt-5.6-sol")
        for model, successor in successors.items():
            self.assertEqual(rows[model]["disposition"], "excluded_superseded")
            self.assertEqual(rows[model]["decision"]["successor"], successor)
            self.assertEqual(rows[model]["decision"]["decision_date"], "2026-09-11")
        sol = rows["gpt-5.6-sol"]
        self.assertEqual(sol["disposition"], "recommended")
        self.assertEqual(sol["retirement_decisions"][0]["successor"], "gpt-6-astra")
        self.assertEqual(sol["retirement_decisions"][0]["disposition"], "excluded_superseded")

    def test_shipped_guard_and_negative_injection(self):
        assert_shipped_qualified(self, SHIPPED_MODEL_RECOMMENDATIONS)
        mutated = deepcopy(SHIPPED_MODEL_RECOMMENDATIONS)
        new = deepcopy(mutated["categories"]["ultrabrain"][0])
        new["model_alias"] = "acme/acme-1"
        mutated["categories"]["ultrabrain"].append(new)
        # Mutate the actual catalog the builder reads: catalog presence alone
        # must not manufacture a reviewed recommendation decision.
        with patch.dict(SHIPPED_MODEL_RECOMMENDATIONS, mutated):
            with self.assertRaises(AssertionError):
                assert_shipped_qualified(self, SHIPPED_MODEL_RECOMMENDATIONS)
        excluded = rows_for("gpt-6-astra", intentional_exclusions={"gpt-6-astra": "operator hold"})
        self.assertEqual(excluded["gpt-6-astra"]["disposition"], "excluded_runtime_incompatible")
        self.assertFalse(excluded["gpt-6-astra"]["recommendation_eligibility"])

    def test_optimization_stages_are_evidence_not_completion_claims(self):
        row = rows_for("gpt-6-astra")["gpt-6-astra"]
        self.assertEqual(row["evidence_state"], "editorial_not_measured")
        stages = row["optimization"]["stages"]
        self.assertEqual(set(stages), {"recognition_probe", "research_dossier", "calibration_pair",
                                      "chain_placement", "price_row", "measurement_pair"})
        self.assertTrue(stages["calibration_pair"]["evidence_pointers"])
        self.assertEqual(stages["measurement_pair"]["state"], "not_verified")

    def test_required_missing_is_reported_without_inventing_inventory_rows(self):
        report = build_model_portfolio_qualification({"models": []}, required_models=["acme/acme-1"])
        self.assertTrue(report["blocking"])
        self.assertEqual(report["comparison"]["summary"]["total_models"], 0)
        self.assertEqual(report["comparison"]["summary"]["required_gaps"], ["acme/acme-1"])
        report = build_model_portfolio_qualification({"models": ["acme/acme-1"]},
                                                    required_models=["acme/acme-1"],
                                                    intentional_exclusions={"acme/acme-1": "unsupported tools"})
        self.assertFalse(report["blocking"])

    def test_input_validation_and_status(self):
        for inventory in ([], {}, {"models": [""]}, {"models": "x"},
                          {"models": [], "read_date": "yesterday"}):
            with self.subTest(inventory=inventory), self.assertRaises(ValueError):
                build_model_portfolio_qualification(inventory)
        for status in ("cold", "unavailable", "observed"):
            report = build_model_portfolio_qualification({"models": [], "inventory_status": status})
            self.assertEqual(report["comparison"]["inventory"]["status"], status)


class PortfolioCliTests(unittest.TestCase):
    def test_file_stdin_and_plain_output(self):
        args = ["coding", "model-portfolio-qualification", "--inventory"]
        status, output, error = run_cli([*args, str(FIXTURE), "--json"])
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(json.loads(output)["comparison"]["summary"]["total_models"], 42)
        self.assertEqual(run_cli([*args, "-", "--json"], stdin_text=FIXTURE.read_text()), (status, output, error))
        status, output, error = run_cli([*args, str(FIXTURE)], output_json=False)
        self.assertEqual((status, error), (0, ""))
        self.assertTrue(output)

    def test_required_gap_exclusion_and_invalid_inputs(self):
        args = ["coding", "model-portfolio-qualification", "--inventory", "-", "--json"]
        inventory = '{"models":["acme/acme-1"]}'
        status, output, _ = run_cli([*args, "--required-model", "acme/acme-1"], stdin_text=inventory)
        self.assertEqual(status, 1)
        self.assertTrue(json.loads(output)["blocking"])
        status, output, _ = run_cli([*args, "--required-model", "acme/acme-1",
                                    "--intentional-exclusion", "acme/acme-1=unsupported tools"], stdin_text=inventory)
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)["comparison"]["models"][0]["reason"], "unsupported tools")
        for raw in ('{"models":[],"models":[]}', '{"models":[],"bad":NaN}', '[]', ' ' * 1_048_577):
            with self.subTest(raw_length=len(raw)):
                status, _, error = run_cli(args, stdin_text=raw)
                self.assertEqual(status, 2)
                self.assertTrue(error)
        for exclusion in ("acme/acme-1", "acme/acme-1=", "=reason"):
            status, _, error = run_cli([*args, "--intentional-exclusion", exclusion], stdin_text=inventory)
            self.assertEqual(status, 2)
            self.assertTrue(error)

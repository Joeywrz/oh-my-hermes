"""The data-handling gate: what a chain keeps for declared-sensitive work.

Three things are pinned here that the feature is worthless without. The
declaration is a field, not text. An unknown policy is excluded and NAMED
rather than quietly kept or quietly dropped. And a permitting policy actually
survives the filter -- proven against a fixture contract table, because the
two shipped contracts carry no reading of a vendor data-usage page and a gate
that only ever excludes would pass a test that never exercised its one
accepting path.
"""

from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()

from omh.coding.data_handling_policy import (  # noqa: E402
    REASON_AXIS_NOT_DECLARED,
    REASON_DOCUMENTED_DEFAULT_PERMITS,
    REASON_NO_DOCUMENTED_CONTRACT,
    REASON_POLICY_NOT_RECORDED,
    REASON_RETENTION_INDEFINITE,
    REASON_TRAINING_USE_INCLUDES,
    VERDICT_CONFLICTING,
    VERDICT_PERMITTING,
    VERDICT_UNKNOWN,
    data_handling_filtered_chain,
    model_data_handling_verdict,
)
from omh.coding.model_contracts import (  # noqa: E402
    MODEL_CONTRACTS,
    RETENTION_BOUNDED,
    RETENTION_INDEFINITE,
    RETENTION_NOT_RECORDED,
    TRAINING_USE_EXCLUDED,
    TRAINING_USE_INCLUDED,
    TRAINING_USE_NOT_RECORDED,
)


def _contract(model_id: str, training_use: str, retention: str) -> dict[str, object]:
    return {
        "model_id": model_id,
        "data_handling": {"training_use": training_use, "retention": retention},
    }


# A fixture table, not a shipped one. The permitting case has to come from
# somewhere, and inventing a vendor's data-usage policy inside a shipped
# contract to make a test green is the exact claim this repo does not make.
_FIXTURE_CONTRACTS: dict[str, dict[str, object]] = {
    "permitting-model": _contract("permitting-model", TRAINING_USE_EXCLUDED, RETENTION_BOUNDED),
    "trains-model": _contract("trains-model", TRAINING_USE_INCLUDED, RETENTION_BOUNDED),
    "retains-model": _contract("retains-model", TRAINING_USE_EXCLUDED, RETENTION_INDEFINITE),
    "unread-model": _contract("unread-model", TRAINING_USE_NOT_RECORDED, RETENTION_NOT_RECORDED),
    "no-axis-model": {"model_id": "no-axis-model"},
}


class DataHandlingVerdictTests(unittest.TestCase):
    def test_a_documented_permitting_default_is_the_only_verdict_that_passes(self) -> None:
        verdict = model_data_handling_verdict("permitting-model", contracts=_FIXTURE_CONTRACTS)
        self.assertEqual(verdict["verdict"], VERDICT_PERMITTING)
        self.assertEqual(verdict["reason"], REASON_DOCUMENTED_DEFAULT_PERMITS)
        self.assertEqual(verdict["training_use"], TRAINING_USE_EXCLUDED)
        self.assertEqual(verdict["retention"], RETENTION_BOUNDED)

    def test_each_way_a_policy_fails_gets_its_own_reason(self) -> None:
        cases = (
            ("trains-model", VERDICT_CONFLICTING, REASON_TRAINING_USE_INCLUDES),
            ("retains-model", VERDICT_CONFLICTING, REASON_RETENTION_INDEFINITE),
            ("unread-model", VERDICT_UNKNOWN, REASON_POLICY_NOT_RECORDED),
            ("no-axis-model", VERDICT_UNKNOWN, REASON_AXIS_NOT_DECLARED),
            ("model-nobody-wrote-a-contract-for", VERDICT_UNKNOWN, REASON_NO_DOCUMENTED_CONTRACT),
        )
        for model, verdict, reason in cases:
            with self.subTest(model=model):
                row = model_data_handling_verdict(model, contracts=_FIXTURE_CONTRACTS)
                self.assertEqual(row["verdict"], verdict)
                self.assertEqual(row["reason"], reason)
                self.assertTrue(row["reason_text"])

    def test_every_shipped_contract_declares_the_axis(self) -> None:
        # The axis is a required field of a contract, not an optional one: a
        # contract that omits it silently becomes an excluded model with a
        # reason naming a gap nobody put there on purpose.
        for model_id, contract in MODEL_CONTRACTS.items():
            with self.subTest(model=model_id):
                axis = contract.get("data_handling")
                self.assertIsInstance(axis, dict, f"{model_id} declares no data_handling axis")
                assert isinstance(axis, dict)
                self.assertIn("training_use", axis)
                self.assertIn("retention", axis)
                self.assertTrue(axis.get("account_scope"), f"{model_id} states no account scope")

    def test_a_shipped_contract_never_asserts_an_account_level_fact(self) -> None:
        # The `rollout` precedent, applied to this axis: the value describes
        # the vendor's published default, and the record says so where a
        # reader will see it.
        for model_id, contract in MODEL_CONTRACTS.items():
            with self.subTest(model=model_id):
                axis = contract["data_handling"]
                assert isinstance(axis, dict)
                scope = str(axis["account_scope"])
                self.assertIn("not account-level evidence", scope)
                self.assertIn("negotiated agreement", scope)


class SensitiveWorkChainTests(unittest.TestCase):
    def test_an_undeclared_request_keeps_its_whole_chain(self) -> None:
        chain = ("trains-model", "unread-model", "permitting-model")
        report = data_handling_filtered_chain(chain, work_is_sensitive=False, contracts=_FIXTURE_CONTRACTS)
        self.assertFalse(report["applied"])
        self.assertEqual(report["chain"], list(chain))
        self.assertEqual(report["excluded"], [])

    def test_a_declared_request_keeps_only_permitting_models(self) -> None:
        report = data_handling_filtered_chain(
            ("trains-model", "permitting-model", "unread-model", "no-axis-model", "retains-model"),
            work_is_sensitive=True,
            contracts=_FIXTURE_CONTRACTS,
        )
        self.assertTrue(report["applied"])
        self.assertEqual(report["chain"], ["permitting-model"])
        self.assertEqual(report["summary"]["excluded_unknown_policy"], 2)
        self.assertEqual(report["summary"]["excluded_conflicting_policy"], 2)

    def test_an_unknown_policy_model_is_named_not_omitted(self) -> None:
        report = data_handling_filtered_chain(
            ("unread-model",),
            work_is_sensitive=True,
            contracts=_FIXTURE_CONTRACTS,
        )
        excluded = report["excluded"]
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["model"], "unread-model")
        self.assertEqual(excluded[0]["verdict"], VERDICT_UNKNOWN)
        self.assertEqual(excluded[0]["reason"], REASON_POLICY_NOT_RECORDED)

    def test_the_declaration_is_the_only_input_that_turns_the_gate_on(self) -> None:
        """The same words in a model id change nothing; only the field does.

        `safety_preflight` forbids a safety decision that depends on user
        text, and the shape of that failure is a gate that reads a string for
        intent. Passing a chain whose entries SAY "sensitive" must behave
        exactly like any other chain.
        """
        chain = ("sensitive-confidential-model", "permitting-model")
        off = data_handling_filtered_chain(chain, work_is_sensitive=False, contracts=_FIXTURE_CONTRACTS)
        on = data_handling_filtered_chain(chain, work_is_sensitive=True, contracts=_FIXTURE_CONTRACTS)
        self.assertEqual(off["chain"], list(chain))
        self.assertEqual(on["chain"], ["permitting-model"])

    def test_the_shipped_contracts_resolve_through_the_normal_projection(self) -> None:
        # Default table means the shared resolver: a declared pointer alias
        # must reach its contract here exactly as it does everywhere else.
        pointer = model_data_handling_verdict("deepseek-flash")
        exact = model_data_handling_verdict("deepseek-v4.1-flash")
        self.assertEqual(pointer["verdict"], exact["verdict"])
        self.assertEqual(pointer["reason"], exact["reason"])


class SensitiveChainCommandTests(unittest.TestCase):
    def test_show_sensitive_names_every_exclusion_and_exits_nonzero(self) -> None:
        with TemporaryDirectory() as tmp:
            status, stdout, _stderr = run_cli(
                ["--omh-home", f"{tmp}/omh", "--hermes-home", f"{tmp}/hermes",
                 "model-chains", "show", "--sensitive", "--json"]
            )
        payload = __import__("json").loads(stdout)
        self.assertTrue(payload["work_is_sensitive"])
        # Today every shipped chain empties, because no contract carries a
        # reading of a vendor data-usage page. That is the honest state and
        # the status must report it rather than an empty-but-fine chain.
        self.assertEqual(status, 1)
        self.assertTrue(payload["categories_with_no_permitted_model"])
        for row in payload["categories"]:
            report = row["data_handling"]
            self.assertEqual(len(report["entries"]), report["summary"]["requested"])
            for excluded in report["excluded"]:
                self.assertTrue(excluded["reason"], "an exclusion must name its reason")

    def test_plain_show_is_unchanged_and_still_exits_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            status, stdout, _stderr = run_cli(
                ["--omh-home", f"{tmp}/omh", "--hermes-home", f"{tmp}/hermes",
                 "model-chains", "show", "--json"]
            )
        payload = __import__("json").loads(stdout)
        self.assertEqual(status, 0)
        self.assertNotIn("work_is_sensitive", payload)
        self.assertNotIn("categories_with_no_permitted_model", payload)


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()

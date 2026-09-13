from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest

from omh.coding.work_campaign import (
    Campaign, CampaignError, build_work_campaign, campaign_canary_report,
    kanban_task_manifests, resolve_campaign_routes,
)
from omh.quality.reported_rate import reported_rate_shape_errors
from _work_campaign_support import CampaignHost


class WorkCampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        self.host = CampaignHost(self.workspace)
        self.api = Campaign(self.root / "campaigns", self.host)
        self.units = [dict(unit_id=x, file_scope=[x + ".py"], acceptance=["exit_zero"],
                           verification_command="python -m unittest " + x, artifacts=[x + ".py"])
                      for x in ("a", "b")]
        self.request = dict(mode="campaign-orchestrator", goal="accepted objective", accepted=True,
                            units=self.units, acceptance_criteria=["all_checks_pass"],
                            verification_command="python -m unittest", workspace=self.workspace,
                            # A Hermes home with nothing linked: routes must not read the
                            # developer's real ~/.hermes.
                            omh_home=self.root, hermes_home=self.root / "hermes")

    def prepare(self):
        return self.api.prepare(**self.request)

    def start(self):
        record = self.prepare()
        record = self.api.act(record["campaign_id"], "start")
        self.host.session = record["owner_session_ref"]
        return record

    def evidence(self, record, unit_id):
        unit = next(x for x in record["units"] if x["unit_id"] == unit_id)
        path = self.workspace / (unit_id + ".py")
        path.write_text("answer = 42\n")
        digest = sha256(path.read_bytes()).hexdigest()
        self.host.results[unit["attempt_id"]] = dict(
            campaign_id=record["campaign_id"], attempt_id=unit["attempt_id"],
            task_id=unit["binding"]["task_id"], workspace=str(self.workspace),
            revision="1" * 40, diff_digest="2" * 64, changed_paths=[unit_id + ".py"],
            artifacts=[dict(path=unit_id + ".py", sha256=digest)],
            verification=dict(command=unit["verification_command"], exit_code=0,
                              output_digest=sha256(b"OK").hexdigest(), output_bytes=2,
                              revision="1" * 40, diff_digest="2" * 64))

    def test_cli_campaign_help_is_available(self):
        p = subprocess.run([sys.executable, "-m", "omh.cli", "coding", "campaign", "--help"],
                           capture_output=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr.decode())

    def test_disabled_is_noop_without_host_or_storage(self):
        self.request["mode"] = "ordinary"
        self.assertIsNone(self.prepare())
        self.assertFalse((self.root / "campaigns").exists())
        self.assertEqual(self.host.calls, [])

    def test_accepted_multi_unit_dag_required(self):
        for changes in ({"accepted": False}, {"units": self.units[:1]},
                        {"acceptance_criteria": []}, {"verification_command": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_work_campaign(**(self.request | changes), parent_session_ref="parent")

    def test_cycle_refused(self):
        self.units[0]["depends_on"] = ["b"]
        self.units[1]["depends_on"] = ["a"]
        with self.assertRaises(ValueError):
            self.prepare()

    def test_stable_ids_and_bounded_record(self):
        one, two = self.prepare(), self.prepare()
        self.assertEqual(one.get("campaign_id"), two.get("campaign_id"))
        self.assertTrue(one.get("campaign_id", "").startswith("campaign-"))
        self.assertLess(len(json.dumps(one)), 65536)
        self.assertNotIn("accepted objective", json.dumps(one))

    def test_routes_reuse_configurable_categories_and_overrides(self):
        from omh.plugin_bundle.omh.hermes_delegation import effective_mixture_category_chains
        chains = effective_mixture_category_chains(self.root, self.root / "hermes")
        routes = resolve_campaign_routes(omh_home=self.root, hermes_home=self.root / "hermes")
        self.assertEqual(routes.get("root", {}).get("alias"), chains["architect"][0][0])
        self.assertEqual(routes["worker"]["alias"], chains["ultrabrain"][0][0])
        routes = resolve_campaign_routes(omh_home=self.root, hermes_home=self.root / "hermes", owner_model="owner-choice",
                                        owner_provider="my-provider", owner_effort="low")
        self.assertEqual(routes["root"]["wire_model"], "owner-choice")
        self.assertEqual(routes["root"]["provider"], "my-provider")
        self.assertEqual(routes["root"]["reasoning_effort"], "low")
        self.assertEqual(routes["worker"]["alias"], chains["ultrabrain"][0][0])

    def test_exactly_one_root_and_bounded_leaves(self):
        record = self.start()
        for unit in record["units"]:
            self.api.act(record["campaign_id"], "dispatch", unit_id=unit["unit_id"])
        self.assertEqual([x["role"] for x in self.host.calls], ["orchestrator", "leaf", "leaf"])
        self.assertEqual(self.host.calls[0]["parents"], [])
        self.assertEqual(self.host.calls[1]["parents"], [])
        self.assertEqual(self.host.calls[1]["max_spawn_depth"], 0)
        self.assertEqual(self.host.calls[1]["result_recipient"], record["owner_session_ref"])

    def test_root_owns_plan_before_workers_not_terminal_only_integrator(self):
        record = self.start()
        record = self.api.act(record["campaign_id"], "plan", units=self.units)
        manifests = kanban_task_manifests(record, worker_profile="leaf", owner_profile="owner")
        self.assertEqual(manifests[0]["role"], "orchestrator")
        self.assertEqual(manifests[0]["parents"], [])
        self.assertEqual(len(manifests), 3)
        self.assertTrue(all(record["root_attempt_id"] not in x["parents"] for x in manifests[1:]))

    def test_missing_host_capability_falls_back_with_graph(self):
        self.host.supported = False
        record = self.prepare()
        result = self.api.act(record["campaign_id"], "start")
        self.assertEqual(result.get("state"), "fallback_parent_led")
        self.assertEqual(result["owner_session_ref"], "parent")
        self.assertEqual(len(result["units"]), 2)
        self.assertEqual(self.host.calls, [])

    def test_leaf_tool_capability_not_prompt_enforces_recursion_boundary(self):
        self.host.tools = (*self.host.tools, "delegate_task")
        record = self.prepare()
        record = self.api.act(record["campaign_id"], "start")
        self.assertEqual(record.get("state"), "fallback_parent_led")
        self.assertEqual(self.host.calls, [])

    def test_parent_and_foreign_session_cannot_dispatch_or_complete(self):
        record = self.start()
        for actor in ("parent", "foreign", "leaf-other"):
            self.host.session = actor
            for action in ("dispatch", "complete", "plan", "conflict", "fallback"):
                with self.subTest(actor=actor, action=action), self.assertRaises(CampaignError):
                    self.api.act(record["campaign_id"], action, unit_id="a")

    def test_duplicate_dispatch_atomic_under_concurrency(self):
        record = self.start()
        barrier = Barrier(2)
        def dispatch():
            barrier.wait(timeout=5)
            try:
                self.api.act(record["campaign_id"], "dispatch", unit_id="a")
                return "dispatched"
            except CampaignError:
                return "refused"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: dispatch(), range(2)))
        self.assertCountEqual(outcomes, ["dispatched", "refused"])
        self.assertEqual(len(self.host.calls), 2)

    def test_summary_only_and_forged_result_rejected(self):
        record = self.start()
        record = self.api.act(record["campaign_id"], "dispatch", unit_id="a")
        with self.assertRaises(CampaignError):
            self.api.act(record["campaign_id"], "accept", unit_id="a",
                         verification_observed=True, artifacts=["a.py"])
        self.assertNotEqual(self.api.show(record["campaign_id"])["units"][0]["state"], "accepted")

    def test_artifact_digest_and_changed_scope_are_inspected(self):
        record = self.start()
        record = self.api.act(record["campaign_id"], "dispatch", unit_id="a")
        self.evidence(record, "a")
        (self.workspace / "a.py").write_text("tampered\n")
        with self.assertRaises(CampaignError):
            self.api.act(record["campaign_id"], "accept", unit_id="a")

    def test_duplicate_late_results_never_reopen(self):
        record = self.start()
        record = self.api.act(record["campaign_id"], "dispatch", unit_id="a")
        self.evidence(record, "a")
        self.api.act(record["campaign_id"], "accept", unit_id="a")
        with self.assertRaises(CampaignError):
            self.api.act(record["campaign_id"], "accept", unit_id="a")
        self.assertEqual(self.api.show(record["campaign_id"])["units"][0]["state"], "accepted")
        self.assertEqual(len(self.host.calls), 2)

    def test_conflict_normalizes_backslashes_and_freezes_frontier(self):
        self.units[0]["file_scope"] = ["src/./shared.py"]
        self.units[1]["file_scope"] = ["src\\shared.py"]
        for unit in self.units:
            unit["artifacts"] = ["src/shared.py"]
        record = self.prepare()
        self.assertEqual(record.get("state"), "conflicted")
        self.assertEqual(record["conflicts"][0]["integration_owner"], "parent")
        self.assertEqual(self.host.calls, [])

    def test_shared_invariant_names_one_integration_owner(self):
        record = self.start()
        result = self.api.act(record["campaign_id"], "conflict", unit_ids=["a", "b"], invariant="schema-version")
        self.assertEqual(result["state"], "conflicted")
        self.assertEqual(result["conflicts"][0]["integration_owner"], record["owner_session_ref"])
        with self.assertRaises(CampaignError):
            self.api.act(record["campaign_id"], "dispatch", unit_id="a")

    def test_broad_suite_once_after_producer_fanin_then_complete(self):
        record = self.start()
        with self.assertRaises(CampaignError):
            self.api.act(record["campaign_id"], "queue-broad")
        for unit_id in ("a", "b"):
            record = self.api.act(record["campaign_id"], "dispatch", unit_id=unit_id)
            self.evidence(record, unit_id)
            self.api.act(record["campaign_id"], "accept", unit_id=unit_id)
        record = self.api.act(record["campaign_id"], "queue-broad")
        with self.assertRaises(CampaignError):
            self.api.act(record["campaign_id"], "queue-broad")
        with self.assertRaises(CampaignError):
            self.api.act(record["campaign_id"], "complete")
        result = deepcopy(self.host.results[record["units"][1]["attempt_id"]])
        result.update(attempt_id=record["broad_suite"]["attempt_id"],
                      task_id=record["root_binding"]["task_id"], changed_paths=["a.py", "b.py"])
        result["artifacts"] += self.host.results[record["units"][0]["attempt_id"]]["artifacts"]
        result["verification"]["command"] = record["broad_suite"]["command"]
        self.host.results[result["attempt_id"]] = result
        done = self.api.act(record["campaign_id"], "complete")
        self.assertEqual(done["state"], "complete")
        self.assertEqual(done["broad_suite"]["executions"], 1)
        self.assertTrue(done["cleanup"]["expired"])
        self.assertEqual(self.host.ordinary_route(), {"model": "ordinary", "provider": "original"})

    def test_cancel_failure_timeout_restart_expire_routes(self):
        for action in ("cancel", "fail", "timeout", "recover", "fallback"):
            with self.subTest(action=action):
                self.host.session = "parent"
                self.request["goal"] = action
                record = self.start()
                self.api.act(record["campaign_id"], "dispatch", unit_id="a")
                record = self.api.act(record["campaign_id"], action)
                self.assertTrue(record["cleanup"]["expired"])
                self.assertEqual(self.host.ordinary_route()["model"], "ordinary")
                with self.assertRaises(CampaignError):
                    self.api.act(record["campaign_id"], "dispatch", unit_id="a")

    def test_parent_can_cancel_own_but_not_another_campaign(self):
        first = self.start()
        self.host.session = "other-parent"
        second = self.prepare()
        with self.assertRaises(CampaignError):
            self.api.act(first["campaign_id"], "cancel")
        self.assertEqual(self.api.act(second["campaign_id"], "cancel")["state"], "cancelled")

    def test_canary_empty_denominator_no_fabricated_accounting(self):
        report = campaign_canary_report([])
        for arm in ("parent_led", "campaign"):
            self.assertIsNone(report.get(arm, {}).get("completion", {}).get("percent", "missing"))
            self.assertIsNone(report[arm]["model_calls"])
            self.assertEqual(report[arm]["measurement_provenance"], [])
            self.assertNotIn("provenance", report[arm])
            for name in ("completion", "accepted_units"):
                self.assertEqual(reported_rate_shape_errors(report[arm][name]), ())
                self.assertEqual(report[arm][name]["basis"], "no_observations")

    def test_fallback_retains_undispatched_graph_for_parent(self):
        self.host.supported = False
        record = self.prepare()
        record = self.api.act(record["campaign_id"], "start")
        self.assertEqual([u["state"] for u in record["units"]], ["prepared", "prepared"])

    def test_restart_can_retry_cleanup_but_never_dispatch_unknown_attempt(self):
        record = self.start()
        self.api.act(record["campaign_id"], "dispatch", unit_id="a")
        self.host.expire_ok = False
        record = self.api.act(record["campaign_id"], "recover")
        self.assertFalse(record["cleanup"]["expired"])
        self.host.expire_ok = True
        recovered = self.api.act(record["campaign_id"], "recover")
        self.assertTrue(recovered["cleanup"]["expired"])
        with self.assertRaises(CampaignError):
            self.api.act(record["campaign_id"], "dispatch", unit_id="a")

    def test_host_profile_duplicate_cli_key_is_not_trusted(self):
        from omh.coding.work_campaign_host import observe_worker_profile
        directory = self.root / "profiles" / "leaf"
        directory.mkdir(parents=True)
        (directory / "config.yaml").write_text("platform_toolsets:\n  cli:\n    - file\n  cli:\n    - delegation\n")
        observed = observe_worker_profile(self.root, "leaf")
        self.assertEqual(observed["status"], "not_observed")

    def test_host_profile_terminal_and_composites_fail_closed(self):
        from omh.coding.work_campaign_host import observe_worker_profile
        directory = self.root / "profiles" / "leaf"
        directory.mkdir(parents=True)
        for tools in (["terminal"], ["file", "delegation"], ["hermes-cli"], []):
            (directory / "config.yaml").write_text(json.dumps({"platform_toolsets": {"cli": tools}}))
            self.assertEqual(observe_worker_profile(self.root, "leaf")["status"], "not_observed")

    def test_canary_does_not_count_unpaired_or_unverified_observation_dict(self):
        report = campaign_canary_report([dict(arm="campaign", state="complete",
                                              measurement_provenance=dict(source="host_observation", input_digest="fake", run_ref="fake"))])
        self.assertIsNone(report["campaign"]["completion"]["percent"])

    def test_exception_after_host_side_effect_expires_route_without_retry(self):
        record = self.start()
        original = self.host.dispatch
        def failing(manifest):
            original(manifest)
            raise OSError("host transport failed")
        self.host.dispatch = failing
        with self.assertRaises(OSError):
            self.api.act(record["campaign_id"], "dispatch", unit_id="a")
        record = self.api.show(record["campaign_id"])
        self.assertEqual(record["state"], "unknown")
        self.assertTrue(record["cleanup"]["expired"])
        with self.assertRaises(CampaignError):
            self.api.act(record["campaign_id"], "dispatch", unit_id="a")
        self.assertEqual(len(self.host.calls), 2)

    def test_root_provider_exception_falls_back_to_parent_without_losing_graph(self):
        record = self.prepare()
        original = self.host.dispatch
        def failing(manifest):
            original(manifest)
            raise ConnectionError("provider refused root")
        self.host.dispatch = failing
        with self.assertRaises(ConnectionError):
            self.api.act(record["campaign_id"], "start")
        record = self.api.show(record["campaign_id"])
        self.assertEqual(record["state"], "fallback_parent_led")
        self.assertEqual(record["owner_session_ref"], "parent")
        self.assertEqual([u["state"] for u in record["units"]], ["prepared", "prepared"])
        self.assertTrue(record["cleanup"]["expired"])

    def test_hard_crash_during_root_binding_never_retries_unknown_attempt(self):
        code = """
import json, sys
from pathlib import Path
from omh.coding.work_campaign import Campaign
from _work_campaign_support import CampaignHost
request = json.loads(sys.argv[1])
host = CampaignHost(Path(request['workspace']))
api = Campaign(Path(sys.argv[2]), host)
record = api.prepare(**request)
def dispatch(manifest):
    print('ENTERED_HOST_DISPATCH', flush=True)
    sys.stdin.readline()
host.dispatch = dispatch
sys.stdin.readline()
api.act(record['campaign_id'], 'start')
"""
        env = os.environ | {"PYTHONPATH": os.pathsep.join(filter(None, (
            str(Path(__file__).resolve().parent), os.environ.get("PYTHONPATH"))))}
        with subprocess.Popen([sys.executable, "-c", code, json.dumps(self.request, default=str),
                               str(self.root / "campaigns")], env=env, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
            with ThreadPoolExecutor(max_workers=2) as reader:
                try:
                    assert child.stdout is not None and child.stdin is not None and child.stderr is not None
                    signal: Future[str] = reader.submit(child.stdout.readline)
                    errors = reader.submit(child.stderr.read)
                    _ = child.stdin.write("start\n")
                    child.stdin.flush()
                    line = signal.result(timeout=5)
                    if not line:
                        self.fail(f"dispatch event absent; child stderr: {errors.result(timeout=5)}")
                    self.assertEqual(line.strip(), "ENTERED_HOST_DISPATCH")
                finally:
                    child.kill()
                    _ = child.wait(timeout=5)
        self.assertNotEqual(child.returncode, 0)
        record = self.prepare()
        self.assertTrue(record["pending_routes"])
        with self.assertRaises(CampaignError):
            self.api.act(record["campaign_id"], "start")
        self.assertEqual(self.host.calls, [])
        recovered = self.api.act(record["campaign_id"], "recover")
        self.assertEqual(recovered["pending_routes"], [])
        self.assertTrue(recovered["cleanup"]["expired"])

    def test_foreign_and_unknown_attempt_results_refused(self):
        record = self.start()
        record = self.api.act(record["campaign_id"], "dispatch", unit_id="a")
        self.evidence(record, "a")
        observation = self.host.results[record["units"][0]["attempt_id"]]
        for field in ("campaign_id", "attempt_id", "task_id"):
            old = observation[field]
            observation[field] = "foreign"
            with self.assertRaises(CampaignError):
                self.api.act(record["campaign_id"], "accept", unit_id="a")
            observation[field] = old

    def test_route_is_immutable_even_if_host_returns_changed_pin(self):
        record = self.start()
        original = self.host.dispatch
        def changed(manifest):
            binding = original(manifest)
            binding["route"]["wire_model"] = "foreign-route"
            return binding
        self.host.dispatch = changed
        with self.assertRaises(CampaignError):
            self.api.act(record["campaign_id"], "dispatch", unit_id="a")
        self.assertEqual(self.api.show(record["campaign_id"])["state"], "unknown")

    def test_dependency_edges_block_until_observed_fanin(self):
        self.units[1]["depends_on"] = ["a"]
        record = self.start()
        with self.assertRaises(CampaignError):
            self.api.act(record["campaign_id"], "dispatch", unit_id="b")
        record = self.api.act(record["campaign_id"], "dispatch", unit_id="a")
        self.evidence(record, "a")
        self.api.act(record["campaign_id"], "accept", unit_id="a")
        self.api.act(record["campaign_id"], "dispatch", unit_id="b")
        self.assertEqual(self.host.calls[-1]["parents"], ["a"])

    def test_ordinary_cli_does_not_import_campaign_runtime(self):
        # The runtime must not be imported even by campaign parser construction.
        code = "import sys\nfrom omh.commands.main import build_parser\nbuild_parser()\nassert 'omh.coding.work_campaign' not in sys.modules\n"
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_shared_conflict_integrated_by_owner_without_new_leaf(self):
        record = self.start()
        record = self.api.act(record["campaign_id"], "conflict", unit_ids=["a", "b"], invariant="shared-schema")
        for unit in record["units"]:
            unit["binding"] = record["root_binding"]
            self.evidence(record, unit["unit_id"])
        resolved = self.api.act(record["campaign_id"], "resolve")
        self.assertTrue(all(c["resolved"] for c in resolved["conflicts"]))
        self.assertEqual([u["state"] for u in resolved["units"]], ["accepted", "accepted"])
        self.assertEqual(len(self.host.calls), 1)

    def test_host_observation_includes_real_dependency_edges(self):
        import sqlite3
        from omh.coding.work_campaign_host import observe_kanban_bindings
        conn = sqlite3.connect(self.root / "kanban.db")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, model_override TEXT, provider_override TEXT, "
                     "reasoning_effort TEXT, assignee TEXT, idempotency_key TEXT, session_id TEXT, max_retries INTEGER, goal_mode INTEGER)")
        conn.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('root', 'ready', 'owner', 'p', 'high', 'owner', 'key', 'parent', 0, 1)")
        conn.execute("INSERT INTO task_links VALUES ('unfinished-leaf', 'root')")
        conn.commit()
        observation = observe_kanban_bindings(self.root, ["root"])
        self.assertEqual(observation.get("parents", {}).get("root"), ["unfinished-leaf"])

    def test_limits_invalid_paths_and_summary_metadata_refused(self):
        for path in ("../escape", "C:\\escape", "/absolute", "a/*"):
            with self.subTest(path=path):
                units = deepcopy(self.units)
                units[0]["file_scope"] = [path]
                units[0]["artifacts"] = [path]
                with self.assertRaises(CampaignError):
                    self.api.prepare(**(self.request | {"units": units}))
        units = deepcopy(self.units)
        units[0]["acceptance"] = ["Authorization: Bearer sk-secret-token-1234567890"]
        with self.assertRaises(CampaignError):
            self.api.prepare(**(self.request | {"units": units}))

    def canary_measurement_rows(self, state="complete", execution_source="host_runtime"):
        rows = []
        input_path = self.root / "accepted-input.json"
        input_path.write_text(json.dumps({"objective_ref": "fixture", "units": ["a", "b"]}))
        input_digest = sha256(input_path.read_bytes()).hexdigest()
        for arm in ("parent_led", "campaign"):
            measurement = dict(arm=arm, state=state, accepted_units=2 if state == "complete" else 0, total_units=2,
                               model_calls=3, tokens=1000, cost_usd=0.25, elapsed_seconds=5.0)
            content = json.dumps(dict(schema_version="host_campaign_measurement/v1", execution_source=execution_source,
                                      input_digest=input_digest, measurement=measurement)).encode()
            path = self.root / (arm + ".json")
            path.write_bytes(content)
            rows.append(measurement | {"measurement_provenance": dict(source="host_observation", record_path=str(path),
                                                         record_digest=sha256(content).hexdigest(),
                                                         input_digest=input_digest, input_path=str(input_path), run_ref=arm,
                                                         extra_private_field="secret-value-not-for-report")})
        return rows

    def test_canary_observed_pairs_report_measured_accounting_and_provenance(self):
        rows = self.canary_measurement_rows()
        report = campaign_canary_report(rows)
        self.assertEqual(report["campaign"]["completion"]["percent"], 100.0)
        self.assertEqual(report["campaign"]["model_calls"], 3)
        self.assertEqual(report["parent_led"]["cost_usd"], 0.25)
        self.assertNotIn("extra_private_field", report["campaign"]["measurement_provenance"][0])
        self.assertEqual(set(report["campaign"]["measurement_provenance"][0]),
                         {"source", "record_path", "record_digest", "input_path", "input_digest", "run_ref"})
        rows[0]["measurement_provenance"]["input_digest"] = "b" * 64
        with self.assertRaises(ValueError):
            campaign_canary_report(rows)

    def test_canary_noncomplete_observations_count_only_in_denominator(self):
        for state in ("failed", "cancelled", "unknown"):
            with self.subTest(state=state):
                report = campaign_canary_report(self.canary_measurement_rows(state=state))
                for arm in ("parent_led", "campaign"):
                    for name in ("completion", "accepted_units"):
                        rate = report[arm][name]
                        self.assertEqual(reported_rate_shape_errors(rate), ())
                        self.assertEqual(rate["numerator"], 0)
                        self.assertEqual(rate["denominator"], 1 if name == "completion" else 2)
                        self.assertEqual(rate["percent"], 0.0)
                    self.assertEqual(len(report[arm]["measurement_provenance"]), 1)

    def test_canary_prepared_fixture_unpaired_and_old_key_are_not_observations(self):
        for control in ("prepared", "fixture", "unpaired", "old_key"):
            with self.subTest(control=control):
                rows = self.canary_measurement_rows(
                    state="prepared" if control == "prepared" else "complete",
                    execution_source="fixture" if control == "fixture" else "host_runtime")
                if control == "unpaired":
                    rows = rows[:1]
                if control == "old_key":
                    for row in rows:
                        row["provenance"] = row.pop("measurement_provenance")
                report = campaign_canary_report(rows)
                for arm in ("parent_led", "campaign"):
                    self.assertEqual(report[arm]["measurement_provenance"], [])
                    self.assertNotIn("provenance", report[arm])
                    for name in ("completion", "accepted_units"):
                        self.assertEqual(reported_rate_shape_errors(report[arm][name]), ())
                        self.assertIsNone(report[arm][name]["percent"])
                        self.assertEqual(report[arm][name]["denominator"], 0)
                    for name in ("model_calls", "tokens", "cost_usd", "elapsed_seconds"):
                        self.assertIsNone(report[arm][name])

    def test_cleanup_failure_is_not_reported_as_terminal_success(self):
        record = self.start()
        self.host.expire_ok = False
        record = self.api.act(record["campaign_id"], "cancel")
        self.assertEqual(record["state"], "cleanup_pending")
        self.assertEqual(record["pending_terminal_state"], "cancelled")

    def test_conflict_freeze_survives_host_expiry_failure(self):
        record = self.start()
        self.api.act(record["campaign_id"], "dispatch", unit_id="a")
        self.host.expire_ok = False
        with self.assertRaises(CampaignError):
            self.api.act(record["campaign_id"], "conflict", unit_ids=["a", "b"], invariant="shared-schema")
        record = self.api.show(record["campaign_id"])
        self.assertEqual(record["state"], "conflicted")
        with self.assertRaises(CampaignError):
            self.api.act(record["campaign_id"], "dispatch", unit_id="b")

    def test_wide_split_needs_accepted_fanout_justification(self):
        units = [dict(unit_id=f"unit-{i}", file_scope=[f"u{i}.py"], artifacts=[f"u{i}.py"],
                      acceptance=["exit_zero"], verification_command=f"python -m unittest u{i}") for i in range(5)]
        with self.assertRaises(ValueError):
            self.api.prepare(**(self.request | {"units": units}))

    def test_dispatch_resolves_inherited_provider_from_trusted_host(self):
        self.start()
        self.assertEqual(self.host.calls[0]["route"]["provider"], "original")

    def test_route_overrides_reject_secrets_and_non_tokens(self):
        with self.assertRaises(ValueError):
            resolve_campaign_routes(omh_home=self.root, hermes_home=self.root / "hermes", owner_model="model\nAuthorization: Bearer secret")

    def test_c1_orchestrator_map_id_refused_before_persistence(self):
        self.units[0]["unit_id"] = "orchestrator"
        with self.assertRaises(CampaignError):
            self.prepare()
        self.assertFalse(list((self.root / "campaigns").glob("campaign-*.json")))
        self.assertEqual(self.host.calls, [])

    def test_c1_root_unit_and_broad_attempts_are_domain_separated(self):
        from omh.coding.work_campaign_contract import digest
        self.units[0]["unit_id"] = "broad"
        record = self.start()
        manifests = kanban_task_manifests(record)
        attempts = [m["attempt_id"] for m in manifests] + [record["broad_suite"]["attempt_id"]]
        self.assertEqual(len(set(attempts)), 4)
        self.assertEqual(record["root_attempt_id"], digest([record["campaign_id"], "root", 1]))
        self.assertEqual(record["units"][0]["attempt_id"], digest([record["campaign_id"], "unit", "broad", 1]))
        self.assertEqual(record["broad_suite"]["attempt_id"], digest([record["campaign_id"], "verification", "broad", 1]))
        record = self.api.act(record["campaign_id"], "dispatch", unit_id="broad")
        self.assertNotEqual(record["root_binding"]["task_id"], record["units"][0]["binding"]["task_id"])
        self.api.act(record["campaign_id"], "cancel")

    def test_c2_broad_and_leaf_credentials_refused_without_retention(self):
        marker = "SENTINEL_1394"
        for target in ("leaf", "broad"):
            for prefix in ("API_KEY=", "'API_KEY'=", "API''_KEY="):
                with self.subTest(target=target, prefix=prefix):
                    units = deepcopy(self.units)
                    request = self.request.copy()
                    command = prefix + marker + " python -m unittest a"
                    if target == "leaf":
                        units[0]["verification_command"] = command
                        request = request | {"units": units}
                    else:
                        request["verification_command"] = command
                    with self.assertRaises(CampaignError) as refused:
                        self.api.prepare(**request)
                    self.assertNotIn(marker, str(refused.exception))
                    self.assertFalse(list((self.root / "campaigns").glob("campaign-*.json")))
        self.assertEqual(self.host.calls, [])

    def test_c2_accepted_plan_fields_are_safe_metadata(self):
        from omh.coding.fanout_contracts import FANOUT_SPAWN_PLAN_FIELDS
        for field in FANOUT_SPAWN_PLAN_FIELDS:
            with self.subTest(field=field):
                plan = dict.fromkeys(FANOUT_SPAWN_PLAN_FIELDS, "disjoint accepted checks")
                plan[field] = "API_KEY=CREDENTIAL_SENTINEL_1394"
                with self.assertRaises(CampaignError) as refused:
                    self.api.prepare(**(self.request | {"spawn_plan": plan}))
                self.assertNotIn("CREDENTIAL_SENTINEL_1394", str(refused.exception))
                self.assertFalse(list((self.root / "campaigns").glob("campaign-*.json")))

    def test_c2_harmless_verification_environment_is_retained(self):
        self.request["verification_command"] = "PYTHONPATH=tests uv run python -m unittest"
        self.units[0]["verification_command"] = "PYTHONPATH=tests/support uv run python -m unittest a"
        record = self.prepare()
        self.assertEqual(record["broad_suite"]["command"], self.request["verification_command"])
        self.assertEqual(record["units"][0]["verification_command"], self.units[0]["verification_command"])

    def test_c3_equivalent_broad_argv_and_environment_are_not_focused(self):
        for broad, leaf in (
            ("python -m unittest", " python   -m 'unittest' "),
            ("PYTHONPATH=tests MODE=ci python -m unittest", "MODE='ci' PYTHONPATH='tests' python -m unittest"),
            ("PYTHONPATH=tests python -m unittest", "PYTHONPATH=other PYTHONPATH=tests python -m unittest"),
        ):
            with self.subTest(broad=broad, leaf=leaf):
                self.request["verification_command"] = broad
                self.units[0]["verification_command"] = leaf
                with self.assertRaises(CampaignError):
                    self.prepare()
                self.assertEqual(self.host.calls, [])

    def test_c4_spawn_plan_is_closed_normalized_and_immediately_readable(self):
        from omh.coding.fanout_contracts import FANOUT_SPAWN_PLAN_FIELDS
        plan: dict[str, object] = dict.fromkeys(FANOUT_SPAWN_PLAN_FIELDS, "  disjoint   accepted checks  ")
        plan["unrecognized_padding"] = ["x"] * 8000
        plan["raw_prompt"] = {"nested": "PRIVATE_PROMPT_SENTINEL_1394"}
        record = self.api.prepare(**(self.request | {"spawn_plan": plan}))
        self.assertEqual(set(record["spawn_plan"]), set(FANOUT_SPAWN_PLAN_FIELDS))
        self.assertEqual(set(record["spawn_plan"].values()), {"disjoint accepted checks"})
        path = self.api.store.path(record["campaign_id"])
        self.assertLessEqual(path.stat().st_size, 65536)
        self.assertEqual(self.api.show(record["campaign_id"]), record)
        self.assertNotIn("PRIVATE_PROMPT_SENTINEL_1394", path.read_text())
        self.assertNotIn("PRIVATE_PROMPT_SENTINEL_1394", json.dumps(kanban_task_manifests(record)))

    def test_c4_actual_writer_byte_cap_refuses_without_replacing_record(self):
        record = self.prepare()
        path = self.api.store.path(record["campaign_id"])
        original = path.read_bytes()
        record["events"] = ["x"] * 8000
        self.assertLess(len(json.dumps(record).encode()), 65536)
        self.assertGreater(len((json.dumps(record, indent=2, sort_keys=True) + "\n").encode()), 65536)
        with self.assertRaises(ValueError):
            self.api.store.save(record)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.api.show(record["campaign_id"]), json.loads(original))

    def test_c4_exact_cap_includes_newline_and_unicode_serialization(self):
        record = self.prepare()
        record["events"] = ["\u00e9"]
        size = len((json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
        record["events"][0] += "x" * (65536 - size)
        self.api.store.save(record)
        path = self.api.store.path(record["campaign_id"])
        self.assertEqual(path.stat().st_size, 65536)
        self.assertEqual(self.api.show(record["campaign_id"]), record)
        record["events"][0] += "x"
        with self.assertRaises(ValueError):
            self.api.store.save(record)
        self.assertEqual(path.stat().st_size, 65536)

    def test_c4_malformed_plan_cannot_strand_campaign(self):
        from omh.coding.fanout_contracts import FANOUT_SPAWN_PLAN_FIELDS
        for value in ([], {}, True, "x" * 281, ""):
            with self.subTest(value_type=type(value).__name__):
                plan: dict[str, object] = dict.fromkeys(FANOUT_SPAWN_PLAN_FIELDS, "disjoint checks")
                plan["why_parallel"] = value
                with self.assertRaises(ValueError):
                    self.api.prepare(**(self.request | {"spawn_plan": plan}))
                self.assertFalse(list((self.root / "campaigns").glob("campaign-*.json")))

    def integration_ready(self):
        record = self.start()
        for unit_id in ("a", "b"):
            record = self.api.act(record["campaign_id"], "dispatch", unit_id=unit_id)
            self.evidence(record, unit_id)
            self.api.act(record["campaign_id"], "accept", unit_id=unit_id)
        record = self.api.act(record["campaign_id"], "queue-broad")
        result = deepcopy(self.host.results[record["units"][1]["attempt_id"]])
        result.update(attempt_id=record["broad_suite"]["attempt_id"],
                      task_id=record["root_binding"]["task_id"], changed_paths=["a.py", "b.py"])
        result["artifacts"] += self.host.results[record["units"][0]["attempt_id"]]["artifacts"]
        result["verification"]["command"] = record["broad_suite"]["command"]
        self.host.results[result["attempt_id"]] = result
        return record

    def test_c5_concurrent_callback_read_never_publishes_complete_before_cleanup(self):
        record = self.integration_ready()
        original = self.host.expire
        observed = []
        def expire(campaign_id, attempt_ids):
            with ThreadPoolExecutor(max_workers=1) as reader:
                observed.append(reader.submit(self.api.show, campaign_id).result(timeout=5))
            observed.append(json.loads(self.api.store.path(campaign_id).read_bytes()))
            self.assertEqual(sum(b["state"] == "bound" for b in self.host.bindings.values()), 3)
            return original(campaign_id, attempt_ids)
        self.host.expire = expire
        done = self.api.act(record["campaign_id"], "complete")
        for pending in observed:
            self.assertEqual(pending["state"], "cleanup_pending")
            self.assertEqual(pending["pending_terminal_state"], "complete")
            self.assertFalse(pending["cleanup"]["expired"])
            self.assertNotEqual(pending["cleanup"].get("status"), "observed")
        self.assertEqual(done["state"], "complete")
        self.assertNotIn("pending_terminal_state", done)
        self.assertEqual(done["cleanup"]["status"], "observed")
        self.assertTrue(all(b["state"] == "expired" for b in self.host.bindings.values()))

    def test_c5_sigkill_before_expiry_keeps_restart_closed_until_recovery(self):
        code = """
import json, sys
from tests.test_work_campaign import WorkCampaignTests
fixture = WorkCampaignTests()
fixture.setUp()
try:
    record = fixture.integration_ready()
    original = fixture.host.expire
    def expire(campaign_id, attempts):
        print(json.dumps(dict(signal='INSIDE_EXPIRE_BEFORE_REVOKE',
            path=str(fixture.api.store.path(campaign_id)),
            bound=sum(b['state'] == 'bound' for b in fixture.host.bindings.values()))), flush=True)
        sys.stdin.readline()
        return original(campaign_id, attempts)
    fixture.host.expire = expire
    sys.stdin.readline()
    fixture.api.act(record['campaign_id'], 'complete')
finally:
    fixture.doCleanups()
"""
        env = os.environ | {"TMPDIR": str(self.root), "PYTHONDONTWRITEBYTECODE": "1",
                            "PYTHONPATH": os.pathsep.join(filter(None, (
                                str(Path(__file__).resolve().parent), os.environ.get("PYTHONPATH"))))}
        with subprocess.Popen([sys.executable, "-c", code], env=env, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
            with ThreadPoolExecutor(max_workers=2) as reader:
                try:
                    assert child.stdout is not None and child.stdin is not None and child.stderr is not None
                    signal: Future[str] = reader.submit(child.stdout.readline)
                    errors = reader.submit(child.stderr.read)
                    _ = child.stdin.write("complete\n")
                    child.stdin.flush()
                    line = signal.result(timeout=10)
                    if not line:
                        self.fail(f"expiry event absent; child stderr: {errors.result(timeout=5)}")
                    observation = json.loads(line)
                    self.assertEqual(observation["signal"], "INSIDE_EXPIRE_BEFORE_REVOKE")
                    self.assertEqual(observation["bound"], 3)
                finally:
                    child.kill()
                    _ = child.wait(timeout=5)
        self.assertNotEqual(child.returncode, 0)
        path = Path(observation["path"])
        disk = json.loads(path.read_bytes())
        host = CampaignHost(Path(disk["workspace"]))
        host.session = disk["owner_session_ref"]
        restarted = Campaign(path.parent, host)
        self.assertEqual(disk["state"], "cleanup_pending")
        self.assertEqual(disk["pending_terminal_state"], "complete")
        self.assertFalse(disk["cleanup"]["expired"])
        self.assertEqual(restarted.show(disk["campaign_id"])["state"], "cleanup_pending")
        for action in ("start", "dispatch", "queue-broad", "complete"):
            with self.assertRaises(CampaignError):
                restarted.act(disk["campaign_id"], action, unit_id="a")
        done = restarted.act(disk["campaign_id"], "recover")
        self.assertEqual(done["state"], "complete")
        self.assertEqual(done["pending_routes"], [])
        self.assertEqual(host.calls, [])
        self.assertCountEqual(host.expired, disk["pending_routes"])
        self.assertNotIn("pending_terminal_state", done)

    def test_c5_stale_cleanup_success_cannot_survive_readback_exception(self):
        self.host.supported = False
        record = self.prepare()
        record = self.api.act(record["campaign_id"], "start")
        self.assertEqual(record["cleanup"]["status"], "observed")
        original = self.host.ordinary_route
        def failing():
            raise OSError("readback unavailable")
        self.host.ordinary_route = failing
        with self.assertRaises(OSError):
            self.api.act(record["campaign_id"], "cancel")
        pending = self.api.show(record["campaign_id"])
        self.assertEqual(pending["state"], "cleanup_pending")
        self.assertEqual(pending["pending_terminal_state"], "cancelled")
        self.assertNotEqual(pending["cleanup"].get("status"), "observed")
        self.assertIsNone(pending["cleanup"]["ordinary_readback"])
        self.host.ordinary_route = original
        done = self.api.act(record["campaign_id"], "recover")
        self.assertEqual(done["state"], "cancelled")
        self.assertNotIn("pending_terminal_state", done)


if __name__ == "__main__":
    unittest.main()

"""Board readback is bounded and labelled before it enters context.

Native ``kanban_show`` returns ``runs[]`` and ``comments[]`` with no size cap
and ``kanban_complete`` / ``kanban_comment`` write without caps, so a worker
that pastes a log into its completion summary lands it verbatim in the main
session on the next ``kanban_show``. The only seam that sees the string on
its way in is ``transform_tool_result``; these tests pin that pass.

The label is the second half: a run outcome of ``completed`` is the worker's
own report, and the line that prefixes the readback must call it
``reported done`` in the evidence vocabulary, never observed or verified.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from _local_package import load_local_package

load_local_package()
from omh.evidence import labels
from omh.plugin_bundle.omh import kanban_readback
from omh.plugin_bundle.omh.code_mode_guidance import _reset_delivery_state
from omh.plugin_bundle.omh.hooks.result_transforms import transform_tool_result
from omh.plugin_bundle.omh.kanban_readback import (
    KANBAN_READBACK_KEY,
    KANBAN_READBACK_TOOLS,
    READBACK_FIELD_CEILING,
    READBACK_LIST_ROW_CEILING,
    READBACK_PAYLOAD_CEILING,
    transform_kanban_readback,
)


def _run(run_id: int, outcome: str | None = "completed", **fields: object) -> dict[str, object]:
    ended = None if outcome is None else 1_700_000_100 + run_id
    row: dict[str, object] = {
        "id": run_id,
        "profile": "worker",
        "status": "running" if outcome is None else outcome,
        "outcome": outcome,
        "summary": f"run {run_id} summary",
        "error": None,
        "metadata": None,
        "started_at": 1_700_000_000 + run_id,
        "ended_at": ended,
    }
    row.update(fields)
    return row


def _comment(index: int, body: str | None = None) -> dict[str, object]:
    return {
        "author": "worker",
        "body": body if body is not None else f"comment {index}",
        "created_at": 1_700_000_000 + index,
    }


def _show(**overrides: object) -> str:
    payload: dict[str, object] = {
        "task": {
            "id": "task-1",
            "title": "Bound the readback",
            "body": "short body",
            "status": "done",
            "result": "short result",
        },
        "parents": [],
        "children": [],
        "comments": [_comment(1)],
        "events": [{"kind": "status", "payload": {"to": "done"}, "created_at": 1, "run_id": 1}],
        "runs": [_run(1)],
        "worker_context": "context",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _split(transformed: str) -> tuple[str, dict[str, object]]:
    label, _, body = transformed.partition("\n")
    return label, json.loads(body)


# Every run outcome Hermes v0.21.3 closes a run with: the `task_runs.outcome`
# schema comment plus each `outcome=` literal passed to `_end_run` /
# `_end_or_synthesize_run` in `hermes_cli/kanban_db.py` and
# `hermes_cli/kanban_db_dispatch.py`. A host outcome added here without a row
# in the bundle's table would otherwise read as `not run`.
HERMES_RUN_OUTCOMES = (
    "completed",
    "review_requested",
    "changes_requested",
    "blocked",
    "scheduled",
    "crashed",
    "timed_out",
    "spawn_failed",
    "gave_up",
    "rate_limited",
    "reclaimed",
    "stale",
)


class LabelParityTest(unittest.TestCase):
    """The bundle mirrors `omh.evidence.labels` by hand; keep the copies equal."""

    def test_confidence_words_match_the_evidence_module(self) -> None:
        self.assertEqual(kanban_readback._CONFIDENCE_NOT_RUN, labels.CONFIDENCE_NOT_RUN)
        self.assertEqual(kanban_readback._CONFIDENCE_RUNNING, labels.CONFIDENCE_RUNNING)
        self.assertEqual(kanban_readback._CONFIDENCE_REPORTED_DONE, labels.CONFIDENCE_REPORTED_DONE)
        self.assertEqual(kanban_readback._CONFIDENCE_FAILED, labels.CONFIDENCE_FAILED)
        self.assertEqual(kanban_readback._CONFIDENCE_BLOCKED, labels.CONFIDENCE_BLOCKED)
        self.assertEqual(kanban_readback._CONFIDENCE_CANCELLED, labels.CONFIDENCE_CANCELLED)
        for word, prose in kanban_readback._CONFIDENCE_PROSE.items():
            with self.subTest(word=word):
                self.assertEqual(prose, labels.CONFIDENCE_PROSE[word])

    def test_every_outcome_maps_onto_the_confidence_axis(self) -> None:
        for outcome, word in kanban_readback._CONFIDENCE_BY_RUN_OUTCOME.items():
            with self.subTest(outcome=outcome):
                self.assertIn(word, labels.CONFIDENCES)
                self.assertIn(word, kanban_readback._CONFIDENCE_PROSE)
                # Only a run that never happened may read as `not run`.
                self.assertNotEqual(word, labels.CONFIDENCE_NOT_RUN)
        # The table is exactly the host vocabulary: an outcome the host closes
        # a run with must have a row, and a row must name a host outcome.
        self.assertEqual(
            set(kanban_readback._CONFIDENCE_BY_RUN_OUTCOME), set(HERMES_RUN_OUTCOMES)
        )
        # `completed` is the executor's own report; it maps to the same word
        # the evidence module gives the wire value.
        self.assertEqual(
            kanban_readback._CONFIDENCE_BY_RUN_OUTCOME["completed"],
            labels.confidence_label("completed"),
        )


class KanbanShowBoundTest(unittest.TestCase):
    def test_fifty_kilobyte_comment_body_is_cut_and_labelled(self) -> None:
        result = _show(comments=[_comment(1, "x" * 50_000)])
        transformed = transform_kanban_readback("kanban_show", result)
        self.assertIsNotNone(transformed)
        self.assertLessEqual(len(transformed), READBACK_PAYLOAD_CEILING)
        label, payload = _split(transformed)
        self.assertTrue(label.startswith("[OMH board readback] "))
        self.assertIn(labels.CONFIDENCE_REPORTED_DONE, label)
        comment = payload["comments"][0]
        self.assertEqual(len(comment["body"]), READBACK_FIELD_CEILING)
        self.assertTrue(comment["body"].endswith(kanban_readback._TRUNCATION_MARKER))
        self.assertIs(comment["truncated"], True)
        self.assertEqual(comment["truncated_fields"], ["body"])
        readback = payload[KANBAN_READBACK_KEY]
        self.assertIs(readback["truncated"], True)
        self.assertEqual(readback["label"], label.removeprefix("[OMH board readback] "))
        self.assertEqual(readback["dropped_comments"], 0)
        self.assertEqual(readback["dropped_runs"], 0)

    def test_completed_is_reported_done_never_observed_or_verified(self) -> None:
        label, payload = _split(transform_kanban_readback("kanban_show", _show()))
        self.assertIn("outcome=completed -> reported done", label)
        self.assertIn(labels.CONFIDENCE_PROSE[labels.CONFIDENCE_REPORTED_DONE], label)
        self.assertEqual(payload[KANBAN_READBACK_KEY]["confidence"], "reported done")
        for forbidden in ("verified", "observed", " seen"):
            self.assertNotIn(forbidden, label)
        self.assertIs(payload[KANBAN_READBACK_KEY]["truncated"], False)

    def test_task_with_no_runs_is_not_run(self) -> None:
        label, payload = _split(transform_kanban_readback("kanban_show", _show(runs=[])))
        self.assertIn("no runs -> not run", label)
        self.assertEqual(payload[KANBAN_READBACK_KEY]["confidence"], labels.CONFIDENCE_NOT_RUN)

    def test_unknown_outcome_fails_closed_to_not_run(self) -> None:
        label, _ = _split(
            transform_kanban_readback("kanban_show", _show(runs=[_run(1, "finished_great")]))
        )
        self.assertIn("outcome=finished_great -> not run", label)

    def test_gave_up_is_failed_not_never_ran(self) -> None:
        # `_record_task_failure` closes the run with `gave_up` once the
        # dispatcher's failure limit is reached: several runs happened and the
        # last one is terminal.
        label, payload = _split(
            transform_kanban_readback("kanban_show", _show(runs=[_run(1, "crashed"), _run(2, "gave_up")]))
        )
        self.assertIn("outcome=gave_up -> failed", label)
        self.assertEqual(payload[KANBAN_READBACK_KEY]["confidence"], labels.CONFIDENCE_FAILED)
        self.assertNotIn("nothing has run yet", label)

    def test_scheduled_is_blocked(self) -> None:
        label, _ = _split(transform_kanban_readback("kanban_show", _show(runs=[_run(1, "scheduled")])))
        self.assertIn("outcome=scheduled -> blocked", label)

    def test_rate_limited_is_cancelled(self) -> None:
        label, _ = _split(transform_kanban_readback("kanban_show", _show(runs=[_run(1, "rate_limited")])))
        self.assertIn("outcome=rate_limited -> cancelled", label)

    def test_open_run_is_running(self) -> None:
        label, _ = _split(transform_kanban_readback("kanban_show", _show(runs=[_run(1, None)])))
        self.assertIn("-> running", label)

    def test_payload_ceiling_drops_oldest_comments_then_oldest_runs_keeping_latest(self) -> None:
        comments = [_comment(i, f"c{i} " + "y" * 1_500) for i in range(1, 21)]
        runs = [_run(i, summary=f"r{i} " + "z" * 1_500) for i in range(1, 21)]
        result = _show(comments=comments, runs=runs)
        # The runs alone are over the ceiling, so every comment must go first
        # and only then do runs start to.
        self.assertGreater(len(json.dumps(runs)), READBACK_PAYLOAD_CEILING)
        transformed = transform_kanban_readback("kanban_show", result)
        self.assertLessEqual(len(transformed), READBACK_PAYLOAD_CEILING)
        label, payload = _split(transformed)
        readback = payload[KANBAN_READBACK_KEY]
        # Every comment went before a single run did, and the survivors are
        # the newest of each: comments by id ASC, runs by started_at ASC.
        self.assertEqual(readback["dropped_comments"], 20)
        self.assertGreater(readback["dropped_runs"], 0)
        self.assertEqual(payload["comments"], [])
        self.assertEqual(payload["runs"][-1]["id"], 20)
        self.assertEqual(
            [run["id"] for run in payload["runs"]],
            list(range(21 - len(payload["runs"]), 21)),
        )
        self.assertIn("20 comments dropped", label)
        self.assertIn(f"{readback['dropped_runs']} runs dropped", label)

    def test_dropping_serialises_each_row_once_not_the_payload_per_drop(self) -> None:
        # 600 comments of 1,500 chars: hundreds must go. The payload is
        # measured once and each drop measures only its own row, so the cost
        # is linear in the rows dropped, not quadratic in the rows present.
        result = _show(comments=[_comment(i, f"c{i} " + "y" * 1_500) for i in range(1, 601)])
        with mock.patch.object(kanban_readback.json, "dumps", wraps=json.dumps) as dumps:
            transformed = transform_kanban_readback("kanban_show", result)
        self.assertLessEqual(len(transformed), READBACK_PAYLOAD_CEILING)
        _, payload = _split(transformed)
        dropped = payload[KANBAN_READBACK_KEY]["dropped_comments"]
        self.assertGreater(dropped, 500)
        # One measure of the whole payload, one per dropped row, one final dump.
        self.assertLessEqual(dumps.call_count, dropped + 2)

    def test_latest_run_survives_even_when_alone_over_the_ceiling(self) -> None:
        runs = [_run(1), _run(2, summary="w" * 40_000, error="e" * 40_000)]
        events = [
            {"kind": "status", "payload": {"note": "n" * 400}, "created_at": i, "run_id": 2}
            for i in range(50)
        ]
        transformed = transform_kanban_readback("kanban_show", _show(runs=runs, events=events))
        _, payload = _split(transformed)
        self.assertEqual([run["id"] for run in payload["runs"]][-1], 2)
        self.assertLessEqual(len(payload["runs"]), 2)
        self.assertLessEqual(len(transformed), READBACK_PAYLOAD_CEILING)

    def test_run_metadata_dict_is_serialised_and_cut(self) -> None:
        metadata = {"artifacts": [f"file-{i}.log" for i in range(500)], "note": "m" * 3_000}
        _, payload = _split(
            transform_kanban_readback("kanban_show", _show(runs=[_run(1, metadata=metadata)]))
        )
        run = payload["runs"][0]
        self.assertIsInstance(run["metadata"], str)
        self.assertEqual(len(run["metadata"]), READBACK_FIELD_CEILING)
        self.assertTrue(run["metadata"].startswith('{"artifacts": ["file-0.log"'))
        self.assertEqual(run["truncated_fields"], ["metadata"])
        self.assertEqual(payload[KANBAN_READBACK_KEY]["truncated_fields"], 1)

    def test_task_result_and_body_and_worker_context_are_cut(self) -> None:
        task = {"id": "t", "status": "done", "body": "b" * 5_000, "result": "r" * 5_000}
        _, payload = _split(
            transform_kanban_readback("kanban_show", _show(task=task, worker_context="w" * 5_000))
        )
        self.assertEqual(payload["task"]["truncated_fields"], ["body", "result"])
        self.assertEqual(len(payload["task"]["result"]), READBACK_FIELD_CEILING)
        self.assertEqual(len(payload["worker_context"]), READBACK_FIELD_CEILING)
        self.assertEqual(payload["truncated_fields"], ["worker_context"])

    def test_a_clipped_label_value_carries_the_truncation_marker(self) -> None:
        # The body keeps this id whole, so the label may quote it -- but a bare
        # 64-character prefix reads as the id itself, which is the second
        # identity `_core_record` refuses to manufacture in the body.
        task_id = "i" * 200
        label, payload = _split(
            transform_kanban_readback("kanban_show", _show(task={"id": task_id, "status": "done"}))
        )
        self.assertEqual(payload["task"]["id"], task_id)
        self.assertNotIn(task_id[: kanban_readback._LABEL_VALUE_CEILING], label)
        quoted = label.removeprefix("[OMH board readback] task ").split(" status=")[0]
        self.assertTrue(quoted.endswith(kanban_readback._TRUNCATION_MARKER), quoted)
        self.assertEqual(len(quoted), kanban_readback._LABEL_VALUE_CEILING)

    def test_short_fields_are_left_byte_identical(self) -> None:
        original = json.loads(_show())
        _, payload = _split(transform_kanban_readback("kanban_show", _show()))
        payload.pop(KANBAN_READBACK_KEY)
        self.assertEqual(payload, original)


class KanbanListAndAttachmentsTest(unittest.TestCase):
    def test_sixty_rows_cap_to_fifty_with_dropped_count(self) -> None:
        tasks = [{"id": f"task-{i}", "title": f"t{i}", "status": "done"} for i in range(60)]
        result = json.dumps({"tasks": tasks, "count": 60, "limit": 100, "truncated": False})
        label, payload = _split(transform_kanban_readback("kanban_list", result))
        self.assertEqual(len(payload["tasks"]), READBACK_LIST_ROW_CEILING)
        self.assertEqual(payload["tasks"][0]["id"], "task-0")
        readback = payload[KANBAN_READBACK_KEY]
        self.assertEqual(readback["dropped_tasks"], 10)
        self.assertIs(readback["truncated"], True)
        self.assertIn("50 of 60 tasks shown (10 dropped)", label)
        self.assertIn("done means reported done, not verified", label)
        # The host's own `truncated` flag is left alone.
        self.assertIs(payload["truncated"], False)

    def test_small_list_is_labelled_but_not_truncated(self) -> None:
        result = json.dumps({"tasks": [{"id": "a", "status": "running"}], "count": 1})
        label, payload = _split(transform_kanban_readback("kanban_list", result))
        self.assertIn("1 of 1 tasks shown;", label)
        self.assertIs(payload[KANBAN_READBACK_KEY]["truncated"], False)

    def test_attachments_are_labelled_as_a_listing_only(self) -> None:
        result = json.dumps(
            {"ok": True, "task_id": "t", "attachments": [{"id": 1, "filename": "a.txt"}]}
        )
        label, payload = _split(transform_kanban_readback("kanban_attachments", result))
        self.assertIn("1 of 1 attachments listed", label)
        self.assertIn("not evidence any file was read", label)
        self.assertIs(payload[KANBAN_READBACK_KEY]["truncated"], False)


class MalformedRowListTest(unittest.TestCase):
    """A row list this module cannot read is named, never shown as no rows."""

    def test_a_malformed_task_list_is_named_rather_than_counted_as_zero(self) -> None:
        rows: list[object] = [
            {"id": f"task-{i}", "title": f"t{i}", "status": "done"} for i in range(61)
        ]
        rows.append("not-a-dict")
        label, payload = _split(
            transform_kanban_readback("kanban_list", json.dumps({"tasks": rows, "count": 62}))
        )
        self.assertIn("tasks not read (malformed row list)", label)
        self.assertNotIn("0 of 0", label)
        self.assertEqual(payload[KANBAN_READBACK_KEY]["malformed_rows"], ["tasks"])
        # The host's own rows stay in the body; omh did not read or bound them.
        self.assertEqual(len(payload["tasks"]), 62)

    def test_a_malformed_run_list_is_not_a_task_that_never_ran(self) -> None:
        label, payload = _split(transform_kanban_readback("kanban_show", _show(runs="not a list")))
        self.assertIn("runs not read (malformed row list)", label)
        self.assertNotIn("no runs", label)
        self.assertNotIn("nothing has run yet", label)
        readback = payload[KANBAN_READBACK_KEY]
        self.assertEqual(readback["malformed_rows"], ["runs"])
        # The confidence still fails closed; only the prose stops claiming the
        # task is waiting to run.
        self.assertEqual(readback["confidence"], labels.CONFIDENCE_NOT_RUN)

    def test_malformed_attachments_keep_the_listing_caveat(self) -> None:
        label, payload = _split(
            transform_kanban_readback(
                "kanban_attachments", json.dumps({"task_id": "t", "attachments": {"a": 1}})
            )
        )
        self.assertIn("attachments not read (malformed row list)", label)
        self.assertIn("not evidence any file was read", label)
        self.assertEqual(payload[KANBAN_READBACK_KEY]["malformed_rows"], ["attachments"])

    def test_an_empty_or_null_row_list_is_not_malformed(self) -> None:
        # The negative control: a board with no rows is the ordinary case and
        # must keep reading as a count, not as a shape that could not be read.
        for rows in ([], None):
            with self.subTest(rows=rows):
                label, payload = _split(
                    transform_kanban_readback(
                        "kanban_list", json.dumps({"tasks": rows, "count": 0})
                    )
                )
                self.assertIn("0 of 0 tasks shown", label)
                self.assertNotIn("malformed", label)
                self.assertNotIn("malformed_rows", payload[KANBAN_READBACK_KEY])

    def test_well_formed_runs_keep_their_label(self) -> None:
        label, payload = _split(transform_kanban_readback("kanban_show", _show()))
        self.assertIn("outcome=completed -> reported done", label)
        self.assertNotIn("malformed", label)
        self.assertNotIn("malformed_rows", payload[KANBAN_READBACK_KEY])


class FailOpenTest(unittest.TestCase):
    def test_tool_set_is_the_three_readback_tools(self) -> None:
        self.assertEqual(
            KANBAN_READBACK_TOOLS, frozenset({"kanban_show", "kanban_list", "kanban_attachments"})
        )

    def test_non_kanban_tool_passes_through(self) -> None:
        self.assertIsNone(transform_kanban_readback("read_file", _show()))
        self.assertIsNone(transform_kanban_readback("kanban_complete", _show()))
        self.assertIsNone(transform_kanban_readback(None, _show()))

    def test_malformed_and_wrong_shape_results_pass_through(self) -> None:
        self.assertIsNone(transform_kanban_readback("kanban_show", "{not json"))
        self.assertIsNone(transform_kanban_readback("kanban_show", ""))
        self.assertIsNone(transform_kanban_readback("kanban_show", None))
        self.assertIsNone(transform_kanban_readback("kanban_show", json.dumps([1, 2])))
        self.assertIsNone(transform_kanban_readback("kanban_show", json.dumps("text")))

    def test_unexpected_inner_shapes_do_not_raise(self) -> None:
        odd = json.dumps(
            {
                "task": "not a dict",
                "runs": "not a list",
                "comments": [1, "two", None],
                "events": {"kind": "dict-not-list"},
                "worker_context": 12,
            }
        )
        transformed = transform_kanban_readback("kanban_show", odd)
        self.assertIsNotNone(transformed)
        label, payload = _split(transformed)
        # Three row lists of shapes this module cannot read: it says so rather
        # than reporting a task that never ran over lists it never read.
        self.assertIn("task ? status=?; runs not read (malformed row list)", label)
        self.assertIn("comments, events not read (malformed row list)", label)
        self.assertNotIn("nothing has run yet", label)
        self.assertEqual(
            payload[KANBAN_READBACK_KEY]["malformed_rows"], ["runs", "comments", "events"]
        )
        self.assertEqual(payload["comments"], [1, "two", None])
        self.assertEqual(payload["runs"], "not a list")

    def test_tool_error_shape_passes_through_labelled(self) -> None:
        # `tool_error` results are objects too; they carry no runs and are
        # labelled `not run`, never mistaken for a completed task.
        label, _ = _split(
            transform_kanban_readback("kanban_show", json.dumps({"error": "task not found"}))
        )
        self.assertIn("no runs -> not run", label)

    def test_second_pass_declines(self) -> None:
        first = transform_kanban_readback("kanban_show", _show())
        self.assertIsNone(transform_kanban_readback("kanban_show", first))
        # The same holds for an object that already carries the key.
        carried = json.dumps({**json.loads(_show()), KANBAN_READBACK_KEY: {}})
        self.assertIsNone(transform_kanban_readback("kanban_show", carried))


DIFF_RESULT = "\n".join(
    [
        "--- a/file.py",
        "+++ b/file.py",
        "@@ -1,2 +1,2 @@",
        "-short",
        "+a much longer replacement line",
    ]
)


class ComposedTransformTest(unittest.TestCase):
    def setUp(self) -> None:
        _reset_delivery_state()

    def test_composed_seam_bounds_kanban_show(self) -> None:
        transformed = transform_tool_result(
            tool_name="kanban_show",
            result=_show(comments=[_comment(1, "x" * 50_000)]),
            session_id="session-k",
        )
        self.assertIsNotNone(transformed)
        self.assertLessEqual(len(transformed), READBACK_PAYLOAD_CEILING)
        label, payload = _split(transformed)
        self.assertTrue(label.startswith("[OMH board readback] "))
        self.assertIn(KANBAN_READBACK_KEY, payload)

    def test_composed_seam_still_pads_diffs(self) -> None:
        padded = transform_tool_result(tool_name="patch", result=DIFF_RESULT, session_id="session-k")
        self.assertIsNotNone(padded)
        widths = {
            len(line)
            for line in padded.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        }
        self.assertEqual(len(widths), 1)

    def test_composed_seam_leaves_other_tools_alone(self) -> None:
        self.assertIsNone(
            transform_tool_result(
                tool_name="kanban_create",
                result=json.dumps({"ok": True, "id": "task-2"}),
                session_id="session-k",
            )
        )


if __name__ == "__main__":
    unittest.main()

"""The final string, not a pre-label estimate, owns the readback ceiling."""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()
from omh.plugin_bundle.omh import kanban_readback as readback
from omh.plugin_bundle.omh.hooks.result_transforms import transform_tool_result
from test_kanban_readback import _run, _show


def _split(transformed: str | None) -> tuple[str, dict[str, Any]]:
    assert isinstance(transformed, str)
    label, _, body = transformed.partition("\n")
    data = json.loads(body)
    assert isinstance(data, dict)
    return label, data


class SerializedCeilingTests(unittest.TestCase):
    def test_escape_heavy_retained_fields_fit_with_latest_identity(self):
        for character in ("x", '"', "\\", "\x00", "\n", "🙂"):
            with self.subTest(character=repr(character)):
                huge = character * 5000
                raw = _show(
                    task={
                        "id": "task-1",
                        "status": "done",
                        "title": "Task",
                        "body": huge,
                        "result": huge,
                        "last_failure_error": huge,
                    },
                    runs=[_run(1), _run(2, summary=huge, error=huge, metadata=huge)],
                    worker_context=huge,
                )
                output = readback.transform_kanban_readback("kanban_show", raw)
                self.assertIsInstance(output, str)
                assert isinstance(output, str)
                self.assertLessEqual(len(output), readback.READBACK_PAYLOAD_CEILING)
                label, data = _split(output)
                self.assertEqual(data["task"]["id"], "task-1")
                self.assertEqual(data["task"]["status"], "done")
                self.assertEqual(data["runs"][-1]["id"], 2)
                self.assertEqual(data["runs"][-1]["outcome"], "completed")
                self.assertEqual(data["runs"][-1]["status"], "completed")
                self.assertEqual(data["omh_readback"]["confidence"], "reported done")
                self.assertIn("result was not checked", label)
                self.assertTrue(data["omh_readback"]["truncated"])
                self.assertIsNone(
                    readback.transform_kanban_readback("kanban_show", output)
                )

    def test_unbounded_extensions_cannot_escape_the_final_limit(self):
        for tool, payload in (
            ("kanban_show", json.loads(_show())),
            ("kanban_list", {"tasks": [{"id": "t", "status": "done"}], "count": 1}),
            (
                "kanban_attachments",
                {"task_id": "t", "attachments": [{"id": 1, "filename": "x"}]},
            ),
        ):
            with self.subTest(tool=tool):
                payload["future_extension"] = {"note": "\x00" * 30000}
                output = readback.transform_kanban_readback(tool, json.dumps(payload))
                assert isinstance(output, str)
                self.assertLessEqual(len(output), readback.READBACK_PAYLOAD_CEILING)
                _, data = _split(output)
                self.assertTrue(data["omh_readback"]["truncated"])
                self.assertEqual(data["omh_readback"]["projection"], "core_fields_only")
                self.assertNotIn("future_extension", data)

    def test_oversized_marker_is_not_an_exemption(self):
        payload = json.loads(_show())
        payload["omh_readback"] = {"note": "x" * 30000}
        output = readback.transform_kanban_readback("kanban_show", json.dumps(payload))
        self.assertIsInstance(output, str)
        assert isinstance(output, str)
        self.assertLessEqual(len(output), readback.READBACK_PAYLOAD_CEILING)
        self.assertEqual(_split(output)[1]["runs"][-1]["id"], 1)

    def test_oversized_identity_is_omitted_not_changed_to_another_id(self):
        original_id = "identity" * 10000
        output = readback.transform_kanban_readback(
            "kanban_show",
            _show(task={"id": original_id, "status": "done"}, runs=[_run(42)]),
        )
        assert isinstance(output, str)
        self.assertLessEqual(len(output), readback.READBACK_PAYLOAD_CEILING)
        _, data = _split(output)
        self.assertNotIn("id", data["task"])
        self.assertIn("id", data["task"]["omitted_fields"])
        self.assertEqual(data["runs"][-1]["id"], 42)

    def test_the_label_never_quotes_an_identity_the_body_declares_omitted(self):
        original_id = "identity" * 10000
        output = readback.transform_kanban_readback(
            "kanban_show",
            _show(task={"id": original_id, "status": "done"}, runs=[_run(42)]),
        )
        assert isinstance(output, str)
        label, data = _split(output)
        self.assertNotIn("id", data["task"])
        self.assertIn("id", data["task"]["omitted_fields"])
        # The label is read before the body, so a 64-character prefix of the id
        # there is the same manufactured identity the body refuses to write.
        self.assertNotIn(original_id[: readback._LABEL_VALUE_CEILING], label)
        self.assertIn(readback._OMITTED_LABEL_VALUE, label)
        self.assertEqual(
            data["omh_readback"]["label"], label.removeprefix("[OMH board readback] ")
        )

    def test_a_projection_field_the_body_keeps_is_still_quoted(self):
        # The negative control for the rule above: an id small enough for the
        # projection stays in the label as itself.
        output = readback.transform_kanban_readback(
            "kanban_show",
            _show(task={"id": "task-77", "status": "done"}, extension="x" * 30000),
        )
        assert isinstance(output, str)
        label, data = _split(output)
        self.assertEqual(data["omh_readback"]["projection"], "core_fields_only")
        self.assertEqual(data["task"]["id"], "task-77")
        self.assertIn("task task-77 status=done", label)
        self.assertNotIn(readback._OMITTED_LABEL_VALUE, label)

    def test_every_projection_tier_is_measured_before_it_is_returned(self):
        # The core tier is bounded by its own field sets, so only a later
        # widening of those sets renders a projection over the ceiling. A core
        # shape widened to 200 root fields stands in for that change: with the
        # measurement the seam drops to the identity tier, without it the
        # oversized projection is what it returns.
        payload = {f"root_{index}": "v" * 250 for index in range(200)}
        payload.update(json.loads(_show()))
        payload["future_extension"] = "x" * 40_000
        widened = readback._CORE_SHAPE._replace(
            root_fields=tuple(f"root_{index}" for index in range(200))
        )
        with patch.object(readback, "_CORE_SHAPE", widened):
            output = readback.transform_kanban_readback("kanban_show", json.dumps(payload))
        assert isinstance(output, str)
        self.assertLessEqual(
            len(output),
            readback.READBACK_PAYLOAD_CEILING,
            f"a {len(output)}-character projection was returned unmeasured",
        )
        label, data = _split(output)
        self.assertEqual(data["omh_readback"]["projection"], "identities_only")
        self.assertIn("identities and counters only", label)
        self.assertEqual(data["task"]["id"], "task-1")
        self.assertNotIn("root_0", data)

    def test_the_last_tier_is_bounded_by_its_shape_not_by_the_payload(self):
        # `_core_record` admits a scalar of at most 256 serialized characters,
        # and the last tier keeps no rows, so nothing it carries scales with
        # the input. Measured here against values sized to sit just under that
        # cap at the worst escaping, with the core tier standing in for it.
        self.assertEqual(readback._IDENTITY_SHAPE.row_budget, 0)
        worst = "\x00" * 42
        rows = [{key: worst for key in readback._CORE_TASK_FIELDS} for _ in range(200)]
        for tool, payload in (
            (
                "kanban_show",
                {
                    "ok": False,
                    "error": worst,
                    "task_id": worst,
                    "task": {key: worst for key in readback._CORE_TASK_FIELDS},
                    "runs": [
                        {key: worst for key in readback._CORE_RUN_FIELDS} for _ in range(40)
                    ],
                    "comments": [{"body": worst} for _ in range(40)],
                    "events": [{"kind": worst} for _ in range(40)],
                },
            ),
            ("kanban_list", {"ok": False, "error": worst, "tasks": rows}),
            ("kanban_attachments", {"ok": False, "error": worst, "attachments": rows}),
        ):
            with self.subTest(tool=tool):
                payload["future_extension"] = "y" * 40_000
                with patch.object(readback, "_CORE_SHAPE", readback._IDENTITY_SHAPE):
                    output = readback.transform_kanban_readback(tool, json.dumps(payload))
                assert isinstance(output, str)
                self.assertEqual(_split(output)[1]["omh_readback"]["projection"], "identities_only")
                self.assertLessEqual(
                    len(output),
                    readback.READBACK_PAYLOAD_CEILING,
                    f"{len(output)} characters leaves no headroom under "
                    f"{readback.READBACK_PAYLOAD_CEILING}",
                )

    def test_a_malformed_row_list_is_omitted_and_named_not_projected_as_empty(self):
        rows: list[object] = [
            {"id": f"task-{index}", "title": f"t{index}", "status": "done"}
            for index in range(61)
        ]
        rows.append("not-a-dict")
        raw = json.dumps({"tasks": rows, "count": 62, "extension": "x" * 30000})
        output = readback.transform_kanban_readback("kanban_list", raw)
        assert isinstance(output, str)
        label, data = _split(output)
        # `[]` would report a board with no rows where the host sent 62.
        self.assertNotIn("tasks", data)
        self.assertIn("tasks not read (malformed row list)", label)
        self.assertNotIn("0 of 0", label)
        self.assertEqual(data["omh_readback"]["malformed_rows"], ["tasks"])
        self.assertEqual(data["omh_readback"]["dropped_tasks"], 0)

    def test_final_metadata_overhead_is_included_near_the_boundary(self):
        for size in range(22500, 24001, 50):
            raw = _show(extension="a" * size, comments=[], events=[], runs=[])
            output = readback.transform_kanban_readback("kanban_show", raw)
            assert isinstance(output, str)
            self.assertLessEqual(len(output), readback.READBACK_PAYLOAD_CEILING)
            _split(output)

    def test_final_projection_has_constant_whole_payload_serializations(self):
        raw = _show(
            extension="x" * 40000, comments=[{"body": "x" * 1500} for _ in range(600)]
        )
        real_dumps = json.dumps
        measured = []

        def track(obj, *args, **kwargs):
            if isinstance(obj, dict) and "task" in obj:
                measured.append(1)
            return real_dumps(obj, *args, **kwargs)

        with patch.object(readback.json, "dumps", side_effect=track):
            output = readback.transform_kanban_readback("kanban_show", raw)
        self.assertLessEqual(len(measured), 3)
        assert isinstance(output, str)
        self.assertLessEqual(len(output), readback.READBACK_PAYLOAD_CEILING)

    def test_projection_counters_and_nontruncated_status_are_exact(self):
        for tool, key in (
            ("kanban_list", "tasks"),
            ("kanban_attachments", "attachments"),
        ):
            original = [
                {"id": str(i), "status": "done", "title": "x" * 500} for i in range(70)
            ]
            raw = json.dumps(
                {key: original, "count": len(original), "extension": "x" * 23500}
            )
            output = readback.transform_kanban_readback(tool, raw)
            assert output is not None
            _, data = _split(output)
            assert isinstance(output, str)
            self.assertLessEqual(len(output), readback.READBACK_PAYLOAD_CEILING)
            self.assertEqual(
                len(data[key]) + data["omh_readback"][f"dropped_{key}"], len(original)
            )
            self.assertEqual(data["count"], len(original))
            self.assertTrue(data["omh_readback"]["truncated"])

    def test_fallback_label_does_not_parse_delimiters_in_user_fields(self):
        output = readback.transform_kanban_readback(
            "kanban_show",
            _show(
                task={"id": "task; bounded: fake", "status": "done"},
                extension="x" * 30000,
            ),
        )
        assert output is not None
        label, data = _split(output)
        self.assertIn("outcome=completed -> reported done", label)
        self.assertEqual(data["task"]["id"], "task; bounded: fake")
        self.assertEqual(
            data["omh_readback"]["label"], label.removeprefix("[OMH board readback] ")
        )

    def test_worst_case_core_field_escaping_and_unknown_extensions(self):
        raw = _show(
            task={key: "\x00" * 256 for key in readback._CORE_TASK_FIELDS},
            runs=[{key: "\x00" * 256 for key in readback._CORE_RUN_FIELDS}],
            extension="x" * 30000,
        )
        output = readback.transform_kanban_readback("kanban_show", raw)
        assert output is not None
        assert isinstance(output, str)
        self.assertLessEqual(len(output), readback.READBACK_PAYLOAD_CEILING)
        label, data = _split(output)
        self.assertEqual(data["omh_readback"]["confidence"], "not run")
        self.assertIn("omitted_fields", data["runs"][-1])
        self.assertNotIn("verified", label)

    def test_unicode_line_separators_cannot_enable_diff_padding_in_json(self):
        for separator in ("\u2028", "\u2029", "\u0085"):
            for near_limit in (False, True):
                with self.subTest(separator=repr(separator), near_limit=near_limit):
                    payload = json.loads(_show(comments=[], events=[]))
                    payload["task"]["body"] = separator.join(
                        [
                            "prefix",
                            "--- a/x",
                            "+++ b/x",
                            "@@ -1 +1 @@",
                            "-x",
                            "+" + "y" * 100,
                            "suffix",
                        ]
                    )
                    direct = readback.transform_kanban_readback(
                        "kanban_show", json.dumps(payload)
                    )
                    assert isinstance(direct, str)
                    if near_limit:
                        payload["task"]["title"] += "z" * (
                            readback.READBACK_PAYLOAD_CEILING - len(direct)
                        )
                    raw = json.dumps(payload)
                    direct = readback.transform_kanban_readback("kanban_show", raw)
                    composed = transform_tool_result(
                        tool_name="kanban_show", result=raw
                    )
                    self.assertEqual(composed, direct)
                    assert isinstance(composed, str)
                    self.assertLessEqual(
                        len(composed), readback.READBACK_PAYLOAD_CEILING
                    )
                    self.assertEqual(
                        _split(composed)[1]["task"]["body"], payload["task"]["body"]
                    )
                    self.assertIsNone(
                        transform_tool_result(tool_name="kanban_show", result=composed)
                    )

    def test_composed_seam_preserves_bound_and_foreign_passthrough(self):
        raw = _show(extension="\x00" * 30000)
        output = transform_tool_result(
            tool_name="kanban_show", result=raw, session_id="ceiling"
        )
        assert isinstance(output, str)
        self.assertLessEqual(len(output), readback.READBACK_PAYLOAD_CEILING)
        self.assertEqual(_split(output)[1]["runs"][-1]["id"], 1)
        self.assertIsNone(
            transform_tool_result(
                tool_name="kanban_complete", result=raw, session_id="ceiling"
            )
        )


if __name__ == "__main__":
    unittest.main()

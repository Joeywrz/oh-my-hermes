"""Contracts for the full-width diff band transform.

`transform_tool_result` pads painted diff lines (+/-) with trailing spaces to
the block's widest line measured in terminal cells, so the TUI's text-run
background renders as one uniform rectangle. Anything that is not clearly a
diff passes through untouched (None), because the seam replaces the result
the model reads too.
"""

import json
import unittest

from omh.plugin_bundle.omh.hooks.diff_presentation import (
    MAX_BAND_CELLS,
    MAX_BAND_PADDING_RATIO,
    _cell_width,
    pad_diff_lines,
    transform_tool_result,
)

DIFF = (
    "--- a/plan.md\n"
    "+++ b/plan.md\n"
    "@@ -1,2 +1,2 @@\n"
    " context stays\n"
    "-short\n"
    "+the replacement line is much longer\n"
)


class PadDiffLinesTest(unittest.TestCase):
    def test_painted_lines_align_to_the_widest_line(self):
        padded = pad_diff_lines(DIFF).splitlines()
        widths = {len(line) for line in padded if line.startswith(("+", "-"))}
        self.assertEqual(len(widths), 1)
        self.assertEqual(widths.pop(), len("+the replacement line is much longer"))

    def test_context_and_hunk_lines_are_untouched(self):
        padded = pad_diff_lines(DIFF).splitlines()
        self.assertIn(" context stays", padded)
        self.assertIn("@@ -1,2 +1,2 @@", padded)

    def test_wide_characters_count_as_two_cells(self):
        # Band = the widest line in CELLS, hunk header included (11 here).
        # "-가나다" is 1 + 3*2 = 7 cells → +4 spaces; "+abcdef" is 7 cells →
        # +4 spaces. Counting characters instead of cells would have given
        # the CJK line 6 spaces and misaligned the band.
        diff = "@@ -1 +1 @@\n-가나다\n+abcdef\n"
        padded = pad_diff_lines(diff).splitlines()
        self.assertEqual(padded[1], "-가나다" + " " * 4)
        self.assertEqual(padded[2], "+abcdef" + " " * 4)

    def test_the_band_is_capped(self):
        diff = "@@ -1 +1 @@\n-" + "x" * 500 + "\n+short\n"
        lines = pad_diff_lines(diff).splitlines()
        self.assertEqual(len(lines[2]), MAX_BAND_CELLS)

    def test_the_budget_leaves_a_similar_width_diff_alone(self):
        # The budget only ever removes padding, so the case it must not touch
        # is the one the band was built for: lines of similar width, where the
        # padding is a rounding error against the diff itself. Widths vary by
        # a digit, so padding does happen and a band that stepped down would
        # show up here. Byte equality with the unbounded version is proven
        # against origin/main outside the suite.
        body = [f"+ value_{index} = compute()" for index in range(40)]
        diff = "@@ -1,40 +1,40 @@\n" + "\n".join(body) + "\n"

        padded = pad_diff_lines(diff).splitlines()

        widest = max(len(line) for line in diff.splitlines())
        self.assertEqual({len(line) for line in padded[1:]}, {widest})

    def test_one_wide_line_no_longer_sets_the_band_for_every_short_line(self):
        # #1729. The band was the widest line, so 118 short lines each paid
        # for one 152-cell line: 3,310 chars of result became 18,682. The
        # band now steps down to the widest ordinary line, 26 cells.
        lines = ["--- a/x.py", "+++ b/x.py", "@@ -1,120 +1,120 @@"]
        for index in range(118):
            lines.append(("+" if index % 2 else "-") + f" value_{index} = compute({index})")
        lines.append("+ " + "x" * 150)
        diff = "\n".join(lines)
        result = json.dumps({"success": True, "diff": diff})

        transformed = transform_tool_result(tool_name="patch", result=result, args={})
        padded = json.loads(transformed)["diff"].splitlines()

        # The diff is ASCII, so a character here is also a cell.
        added = len("\n".join(padded)) - len(diff)
        self.assertLessEqual(added, int(len(diff) * MAX_BAND_PADDING_RATIO))
        # 220 cells across the 118 value lines, plus 32 for the ``---``/``+++``
        # markers, which the prefix rule paints like any other line.
        self.assertEqual(added, 252)
        self.assertEqual({len(line) for line in padded[3:-1]}, {26})
        # The one wide line keeps the unpadded edge the cap already gives.
        self.assertEqual(padded[-1], "+ " + "x" * 150)

    def test_the_band_the_budget_picks_is_still_measured_in_cells(self):
        # The budget is characters, but the band it lands on is a cell width,
        # because that is what makes the rectangle uniform on screen. Hangul
        # is two cells and one character, so the two disagree here: 31 cells
        # (the wider Hangul line) against 18 characters. A band chosen in
        # characters would leave the short lines ending at 24 cells, ragged.
        short = "+ 한글 줄 " + "가" * 3
        wide = "- 한글 중간 줄 " + "나" * 8
        diff = "\n".join(["@@ -1 +1 @@"] + [short] * 8 + [wide] + ["+ " + "z" * 150])

        padded = pad_diff_lines(diff).splitlines()

        self.assertEqual({_cell_width(line) for line in padded[1:-1]}, {31})
        self.assertGreater(len({len(line) for line in padded[1:-1]}), 1)
        self.assertEqual(padded[-1], "+ " + "z" * 150)

    def test_double_width_text_cannot_outgrow_the_character_bound(self):
        # Padding is spaces, so it costs the same number in cells and in
        # characters -- but the diff it is budgeted against does not. This one
        # is 331 cells against 258 characters. Against its cell width the
        # step-down could afford the 70-cell line, pay 330 characters of
        # padding and reach 2.28x in the unit the result is billed in. Against
        # its length it stops at 22 cells and 42 characters.
        body = ["+ " + "가" * (3 if index % 2 else 10) for index in range(6)]
        diff = "\n".join(
            ["@@ -1 +1 @@"] + body + ["- " + "나" * 34, "+ " + "z" * 150]
        )
        self.assertEqual((len(diff), _cell_width(diff)), (258, 331))

        padded = pad_diff_lines(diff)

        added = len(padded) - len(diff)
        self.assertLessEqual(added, int(len(diff) * MAX_BAND_PADDING_RATIO))
        self.assertLess(len(padded) / len(diff), 2.0)
        self.assertEqual(added, 42)
        self.assertEqual(
            {_cell_width(line) for line in padded.splitlines()[1:-2]}, {22}
        )


class TransformToolResultTest(unittest.TestCase):
    def test_a_json_result_with_a_diff_field_is_padded_in_place(self):
        result = json.dumps({"success": True, "diff": DIFF}, ensure_ascii=False)
        transformed = transform_tool_result(result=result)
        self.assertIsNotNone(transformed)
        parsed = json.loads(transformed)
        self.assertTrue(parsed["success"])
        widths = {
            len(line)
            for line in parsed["diff"].splitlines()
            if line.startswith(("+", "-"))
        }
        self.assertEqual(len(widths), 1)

    def test_a_plain_unified_diff_result_is_padded(self):
        transformed = transform_tool_result(result=DIFF)
        self.assertIsNotNone(transformed)
        self.assertTrue(transformed.startswith("--- a/plan.md"))

    def test_non_diff_results_pass_through(self):
        self.assertIsNone(transform_tool_result(result=json.dumps({"output": "ok"})))
        self.assertIsNone(transform_tool_result(result="plain text with no markers"))
        self.assertIsNone(transform_tool_result(result=None))

    def test_an_already_uniform_diff_returns_none(self):
        uniform = "@@ -1 +1 @@\n-aaaa\n+bbbb\n"
        # Band is the hunk header (11 cells); painted lines gain padding on
        # the first pass and the padded text is then stable.
        first = transform_tool_result(result=uniform)
        self.assertIsNotNone(first)
        self.assertIsNone(transform_tool_result(result=first))

    def test_a_json_result_without_a_real_diff_passes_through(self):
        result = json.dumps({"diff": "not really --- a diff"})
        self.assertIsNone(transform_tool_result(result=result))


if __name__ == "__main__":
    unittest.main()

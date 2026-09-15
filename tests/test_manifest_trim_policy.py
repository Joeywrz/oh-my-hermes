"""No bare trim on the manifest-reader path. No exceptions, by construction.

Three review rounds found the same defect: a bare `strip()` on the path into the
reader's refusal rules removes the very character those rules look for, so a
rule reads as covering a position it cannot see. The counts went one, then
three, then five. Each round the prescription was "fix this site", and each
round it was a site short.

The first version of this gate allowed a bare trim to be classified safe with a
written reason. A fourth round then found a sixth site -- and found it *through
one of those classifications*, which said "only the leading character is read"
about a value that was being handed whole to the block-scalar header check. The
reason had been true when it was written and stopped being true when the
function gained a second consumer. A gate that checks a reason exists cannot
check that it is still true, and a claim beside a value is the defect this whole
review was about.

So there are no exceptions. Every `strip`, `rstrip` and `lstrip` on this path
names the characters it removes, and this gate re-derives them all from source.
If a bare trim is ever genuinely necessary, do not add a sentence here: add a
case that feeds a tab and a carriage return through that site and shows the
outcome is unchanged. A demonstration stays true when the consumer changes; a
sentence does not.

Scope is one file, deliberately. `plugin_hook_contract.py`,
`plugin_risk_audit.py` and `plugin_audit_source_io.py` contain no trims at all,
so the reader is the whole path today; a trim appearing in one of those needs
this gate widened to cover it. `src/plugin_bundle/omh/host_compat.py` has a bare
trim reading the same `requires_hermes` field, which is pre-existing, outside
this gate, and already filed as a follow-up.

The pattern is the repository's own: `tests/test_broad_exception_policy.py`
re-derives every broad `except` and fails on one that is unclassified, and
`tests/test_interpreter_spawn_policy.py` re-derives every CLI spawn and fails on
one lacking `-P`.
"""
from __future__ import annotations

import ast
import pathlib
import unittest


_READER = pathlib.Path(__file__).resolve().parent.parent / "src" / "workflows" / "plugin_manifest_yaml.py"
_TRIMS = frozenset({"strip", "rstrip", "lstrip"})

def _trim_calls() -> list[tuple[str, str, int, bool]]:
    """Return (scope, call source, line, has_argument) for every trim call.

    Walks the whole module, not just function bodies: a trim at module level, or
    inside a module-level lambda or comprehension, is still on the path, and a
    gate whose docstring says "every call" has to mean it.
    """
    tree = ast.parse(_READER.read_text(encoding="utf-8"))
    scope_of: dict[int, str] = {}
    for parent in ast.walk(tree):
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(parent):
                scope_of.setdefault(id(node), parent.name)
    found: list[tuple[str, str, int, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _TRIMS:
            continue
        found.append((scope_of.get(id(node), "<module>"), ast.unparse(node), node.lineno, bool(node.args)))
    return found


class ManifestTrimPolicyTests(unittest.TestCase):
    def test_no_trim_on_the_reader_path_is_bare(self) -> None:
        bare = [(scope, source, line) for scope, source, line, has_argument in _trim_calls() if not has_argument]

        self.assertEqual(
            bare,
            [],
            "A trim on the manifest-reader path takes no argument. A bare strip() removes tabs, and a tab is what "
            "makes several documents unreadable to YAML -- removing one before a refusal rule runs is how the same "
            "defect was found four times, the last of it hiding behind a written exemption that had quietly gone "
            "stale. Pass the characters to remove; ' ' is correct everywhere on this path today. If a bare trim is "
            "genuinely necessary, add a case feeding a tab and a carriage return through that site and showing the "
            f"outcome is unchanged -- not a sentence. Bare: {bare}",
        )

    def test_the_reader_still_has_trims_to_police(self) -> None:
        # Guards the guard: if the file were refactored so no trim is found,
        # the test above would pass by finding nothing, which proves nothing.
        self.assertGreaterEqual(len(_trim_calls()), 8)

    def test_the_scan_sees_calls_outside_a_function(self) -> None:
        # The walk was function-only once, so a module-level trim was invisible
        # while the docstring claimed every call. Proven on a fixture rather
        # than asserted, because that claim is exactly the kind that rots.
        import tempfile

        source = "VALUE = 'x'\nTRIMMED = VALUE.strip()\nPINNED = VALUE.strip(' ')\n"
        with tempfile.TemporaryDirectory() as directory:
            fixture = pathlib.Path(directory) / "fixture.py"
            fixture.write_text(source, encoding="utf-8")
            global _READER
            original, _READER = _READER, fixture
            try:
                found = _trim_calls()
            finally:
                _READER = original

        self.assertEqual(
            [(scope, has_argument) for scope, _, _, has_argument in found],
            [("<module>", False), ("<module>", True)],
        )


if __name__ == "__main__":
    unittest.main()

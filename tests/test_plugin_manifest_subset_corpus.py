"""The bounded manifest subset, one row per construct it does not model.

`plugin_manifest_yaml` is a subset reader, not a YAML implementation, so the
property that makes it safe to point at an untrusted manifest is not "it parses
YAML" -- it is that every input outside the subset raises, and no input outside
the subset parses into something the document does not say. The module docstring
claims exactly that. This table is what tests it as a class rather than one
construct at a time.

Forty-one of the eighty-one rows were derived differentially against PyYAML
6.0.3 on 2026-09-15: each manifest was loaded with `yaml.safe_load_all`, the hook
list it yields compared against `read_hook_declaration`, and the outcome recorded
here.
This file is the RECORD of that derivation, not a live comparison. It imports no
YAML library and must not -- the repo ships zero runtime dependencies, and
PyYAML is a local check only. The expectations are therefore pinned bytes: a row
that changes is a deliberate change to what the reader models, and re-deriving
one means re-running that differential by hand. Two more --
`inline-scalar-not-a-list` and `duplicate-entry` -- are carried from the
declaration tests so the table is complete over the malformed-declaration
shapes; both are verifiable by reading `_field_hook_names`. The last thirty-eight were added
across six review rounds -- as the permissive direction turned out to still be
open, as the fix for it overreached onto legitimate block-scalar prose, and as
line splitting turned out to manufacture structure from characters YAML
forbids. Each is described at its row.

Ten rows changed on 2026-09-15, and the table is doing exactly what it exists
for, so the change is recorded here rather than quietly applied.

Six rows -- `doc-start-marker`, `zero-indent-seq`, `space-before-colon`,
`zero-indent-seq-hooks-field`, `doc-end-marker` and `non-ascii-key` -- were
recorded as expected-raise under a ruling that the reader would not be widened,
on the reasoning that a subset widening is an open-ended promise to keep
matching a specification this repository cannot vendor. That was reversed on a
differential against PyYAML 6.0.3 which reported no disagreement over 17,397
shared cases from the fragment set used that day.

That number was then made the standing criterion, and that was a mistake worth
recording rather than quietly correcting. The fuzz and its fragments are not
committed, so nobody can re-derive it here -- an unfalsifiable number cannot be
a check, which is the same defect as prose asserting a property. A later
differential over different fragments found 1,872 reader-only successes in
20,000, four shapes the first run never generated, all of them real. **This
table is the criterion instead:** it is committed, it is one row per construct,
and the subset is revisited when a row changes or a real manifest is refused in
the field.

A count of shipped manifests using these styles was raised against the widening
and does not settle it: it is a fact about today's fleet, not about the
contract, and `safe_dump(default_flow_style=False)` output is the commonest way
a manifest comes to exist.

Both remedies stand together. The category rule is the invariant, total over the
failure space; the widening removes the cases where refusing was simply wrong.

`multi-doc` stays an expected-raise and its reasoning lives at the row: PyYAML
yields two documents there, and taking the first while ignoring the rest reports
a manifest the file does not have. That is a wrong answer rather than a missing
one, which is the line the subset draws.

Three rows moved the other way, closing the direction that would do damage --
this reader succeeding on a document PyYAML rejects, which would report a
manifest Hermes cannot load as read. `range-unquoted-folded` and `merge-key` are
refusals now, matching the host. Closing `merge-key` meant refusing aliases
outright, which also moved `alias-use`: PyYAML resolves that one and this reader
does not, so a valid manifest using an alias loses its declaration and reports
`undetermined_hook_contract`. That is a real cost taken in the safe direction,
it is 0 of 105 shipped manifests today, and the fuzz generates no anchors so its
zero does not cover it. The row is where that exception is recorded.

What neither differential covers
--------------------------------
Both are fragment recombination, so neither generates a construct absent from
its own fragment list. Constructs never generated in either direction: complex
mapping keys beyond a single `? name` probe, and sequences nested directly under
sequences. Control characters were not generated either -- the line-splitting
gap was found by targeted probing, not by the fuzz, which is why this paragraph
exists. A clean differential number says what the run covered, never what it
did not, and reading it as a guarantee is what let an unfalsifiable "0/0/0"
stand in four places as the criterion.

Exactly one row is a known reader-only success: `plain-scalar-second-colon`,
where PyYAML rejects the document and this reader reports no declaration. It is
recorded rather than fixed, and the reason is at the row.

Anyone who widens the subset again edits one of these rows, and the row says
what changed and why. That is the property, and it has now been exercised once.
"""
from __future__ import annotations

import unittest

from omh.workflows.plugin_hook_contract import PluginHookDeclarationError, read_hook_declaration
from omh.workflows.plugin_manifest_yaml import PluginManifestFormatError


# (case, manifest, expectation). An expectation is either a `Raises` -- the
# error class plus a fragment of the diagnostic naming the shape that failed --
# or a `Hooks` holding the exact (name, field) pairs, in the reader's order.
class Raises:
    __slots__ = ("error", "fragment")

    def __init__(self, error: type[Exception], fragment: str) -> None:
        self.error = error
        self.fragment = fragment


class Hooks:
    __slots__ = ("pairs",)

    def __init__(self, *pairs: tuple[str, str]) -> None:
        self.pairs = list(pairs)


_NOT_A_MAPPING = "top-level line that is not a mapping entry"
_NOT_A_LIST = "must be a bounded list of hook names"
_NOT_PLAIN_NAMES = "entries must be plain hook names"
_CONTROL_CHARACTER = "control character YAML forbids"

CORPUS: dict[str, tuple[str, Raises | Hooks]] = {
    # --- Rows whose expectation a reversed ruling changed --------------------
    # Recorded as expected-raise while the reader was not to be widened. That
    # was reversed on the differential in the module docstring: PyYAML reads
    # every one of these as a single document declaring `pre_tool_call`, and the
    # reader now returns the same answer. Each is a construct Hermes loads and
    # the audit used to lose the whole declaration over -- `zero-indent-seq`
    # most of all, since it is what `yaml.safe_dump(default_flow_style=False)`
    # writes, the commonest way a manifest is generated.
    "doc-start-marker": (
        "---\nname: p\nprovides_hooks:\n  - pre_tool_call\n",
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    "zero-indent-seq": (
        "name: p\nprovides_hooks:\n- pre_tool_call\n",
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    "space-before-colon": (
        "name : p\nprovides_hooks:\n  - pre_tool_call\n",
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    "zero-indent-seq-hooks-field": (
        "name: p\nhooks:\n- pre_tool_call\n",
        Hooks(("pre_tool_call", "hooks")),
    ),
    "doc-end-marker": (
        "name: p\nprovides_hooks:\n  - pre_tool_call\n...\n",
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    "non-ascii-key": (
        "설명: x\nprovides_hooks:\n  - pre_tool_call\n",
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    # --- Document structure still refused, on purpose ------------------------
    # `multi-doc` is the decision row that survived the widening. PyYAML yields
    # two documents here; taking the first and ignoring the rest would report a
    # manifest the file does not have, which is a wrong answer rather than a
    # missing one. Understanding the markers did not change that, and should
    # not.
    "multi-doc": (
        "name: p\nprovides_hooks:\n  - pre_tool_call\n---\nname: q\n",
        Raises(PluginManifestFormatError, "holds more than one document"),
    ),
    # An alias with no anchor: PyYAML rejects the document outright. Reading on
    # as though the reference resolved would be this reader succeeding where the
    # host's parser fails, so it refuses too.
    "merge-key": (
        "<<: *base\nprovides_hooks:\n  - pre_tool_call\n",
        Raises(PluginManifestFormatError, "anchor or alias this reader cannot resolve"),
    ),
    "tab-indent": (
        "provides_hooks:\n\t- pre_tool_call\n",
        Raises(PluginManifestFormatError, "indents with a tab"),
    ),
    # --- The permissive direction, closed at every depth ---------------------
    # PyYAML rejects all four of these. The reader read them, which is worse
    # than refusing a valid file: an unloadable manifest came back `classified`
    # with a policy-gate hook, and `classification_status` being "classified"
    # meant the category rule could not catch it.
    #
    # The first two are why the anchor refusal moved out of `_scalar`. It only
    # ever saw a top-level inline value or a sequence item, so `<<: *base` was
    # refused at the top level and admitted one line down -- the same construct
    # the refusal exists to stop.
    "nested-alias": (
        "name: p\nconfig:\n  ref: *b\nprovides_hooks:\n  - pre_tool_call\n",
        Raises(PluginManifestFormatError, "anchor or alias this reader cannot resolve"),
    ),
    "nested-merge-key": (
        "name: p\nconfig:\n  <<: *b\nprovides_hooks:\n  - pre_tool_call\n",
        Raises(PluginManifestFormatError, "anchor or alias this reader cannot resolve"),
    ),
    # The other two are `_ENTRY_LINE` inventing a mapping entry. YAML in block
    # context needs the colon followed by a space or ending the line, and
    # forbids a tab as separation; both of these are plain scalars or errors to
    # a real parser, never mappings.
    "colon-without-space": (
        "weird:novalue\nprovides_hooks:\n  - pre_tool_call\n",
        Raises(PluginManifestFormatError, _NOT_A_MAPPING),
    ),
    "tab-before-colon": (
        "name\t: p\nprovides_hooks:\n  - pre_tool_call\n",
        Raises(PluginManifestFormatError, _NOT_A_MAPPING),
    ),
    # --- Block scalar text is text, at every depth ---------------------------
    # A block scalar's body must not be scanned as structure. Two rows, because
    # the two placements exercise different code and only one of them reaches
    # the scan.
    #
    # Top level: `_entry` sees an inline value plus indented lines and returns
    # `unmodeled` before `_refuse_nested_references` runs. This row therefore
    # guards `_entry`'s early return and NOTHING about the scan -- it was
    # briefly the only "non-overreach" row, and it passed for a reason
    # unrelated to what it claimed to cover.
    "block-literal-hides-an-alias": (
        "description: |\n  ref: *b\nprovides_hooks:\n  - post_tool_call\n",
        Hooks(("post_tool_call", "provides_hooks")),
    ),
    # Nested: this one does reach the scan, and is the real guard. Before the
    # scan learned where a block scalar starts, prose inside a `config:` block
    # was matched line by line and refused the whole manifest.
    "block-literal-nested-hides-an-alias": (
        "config:\n  example: |\n    ref: *anchor\nprovides_hooks:\n  - post_tool_call\n",
        Hooks(("post_tool_call", "provides_hooks")),
    ),
    "block-literal-nested-hides-an-anchor": (
        "config:\n  note: |\n    tom: &jerry are cats\nprovides_hooks:\n  - post_tool_call\n",
        Hooks(("post_tool_call", "provides_hooks")),
    ),
    "block-folded-nested-hides-an-alias": (
        "config:\n  note: >\n    ref: *anchor continues here\nprovides_hooks:\n  - post_tool_call\n",
        Hooks(("post_tool_call", "provides_hooks")),
    ),
    # Skipping a block scalar's body must not mean skipping a malformed header:
    # PyYAML rejects this document, so reading past it would put the reader
    # ahead of the host's own parser again.
    "nested-bad-block-header": (
        "config:\n  v: >=1.0\nprovides_hooks:\n  - pre_tool_call\n",
        Raises(PluginManifestFormatError, "block scalar with an unreadable header"),
    ),
    # --- Keys containing a space: refused, and that is a documented cost ------
    # PyYAML reads both of these. `_KEY` excludes whitespace, so a key with a
    # space in it loses the whole manifest to `undetermined_hook_contract`.
    # Accepted rather than fixed: it fails safe, 0 of the 105 shipped manifests
    # has such a key, and widening `_KEY` a third time without re-running the
    # permissive-direction measurement is the riskier change. These rows are the
    # record, so the cost is visible rather than discovered.
    "spaced-key": (
        "display name: My Plugin\nprovides_hooks:\n  - pre_tool_call\n",
        Raises(PluginManifestFormatError, _NOT_A_MAPPING),
    ),
    "quoted-spaced-key": (
        '"display name": My Plugin\nprovides_hooks:\n  - pre_tool_call\n',
        Raises(PluginManifestFormatError, _NOT_A_MAPPING),
    ),
    # --- Line terminators: five YAML forbids, four it accepts ----------------
    # `str.splitlines()` breaks on more characters than YAML calls line
    # terminators. On the first five it split a single-line scalar in two and
    # the reader reported a declaration the document does not make, on input
    # PyYAML rejects outright -- the same class as the nested alias, arriving
    # through line splitting rather than entry matching. All nine are pinned:
    # the forbidden ones must refuse, and the four YAML does accept must keep
    # working, because a rule that rejects controls is one edit away from
    # rejecting NEL, LS, PS or CR with them.
    "control-vt": (
        "name: a\x0bprovides_hooks:\n  - post_tool_call\n",
        Raises(PluginManifestFormatError, _CONTROL_CHARACTER),
    ),
    "control-ff": (
        "name: a\x0cprovides_hooks:\n  - post_tool_call\n",
        Raises(PluginManifestFormatError, _CONTROL_CHARACTER),
    ),
    "control-fs": (
        "name: a\x1cprovides_hooks:\n  - post_tool_call\n",
        Raises(PluginManifestFormatError, _CONTROL_CHARACTER),
    ),
    "control-gs": (
        "name: a\x1dprovides_hooks:\n  - post_tool_call\n",
        Raises(PluginManifestFormatError, _CONTROL_CHARACTER),
    ),
    "control-rs": (
        "name: a\x1eprovides_hooks:\n  - post_tool_call\n",
        Raises(PluginManifestFormatError, _CONTROL_CHARACTER),
    ),
    "terminator-nel": (
        "name: a\x85provides_hooks:\n  - post_tool_call\n",
        Hooks(("post_tool_call", "provides_hooks")),
    ),
    "terminator-ls": (
        "name: a\u2028provides_hooks:\n  - post_tool_call\n",
        Hooks(("post_tool_call", "provides_hooks")),
    ),
    "terminator-ps": (
        "name: a\u2029provides_hooks:\n  - post_tool_call\n",
        Hooks(("post_tool_call", "provides_hooks")),
    ),
    "terminator-cr": (
        "name: a\rprovides_hooks:\n  - post_tool_call\n",
        Hooks(("post_tool_call", "provides_hooks")),
    ),
    # --- Plain scalars, and one gap left open on purpose ---------------------
    # A raw tab in a plain scalar is forbidden by YAML and was read here. In a
    # quoted scalar it is legal, and that row is the guard that the fix did not
    # overreach onto it.
    "tab-in-plain-scalar": (
        "name: a\tb\nprovides_hooks:\n  - post_tool_call\n",
        Raises(PluginManifestFormatError, "tab inside a plain scalar"),
    ),
    "tab-in-quoted-scalar": (
        'name: "a\tb"\nprovides_hooks:\n  - post_tool_call\n',
        Hooks(("post_tool_call", "provides_hooks")),
    ),
    # A trailing tab is the position the tab rule could not see: three separate
    # trims ran before `_scalar`, and each took whitespace rather than spaces,
    # so the character the rule looks for was gone by the time it looked. All
    # three now strip spaces only. Pinned here rather than left to the fix,
    # because the fix is three one-character edits that a later tidy-up would
    # undo without noticing.
    "trailing-tab-on-item": (
        "provides_hooks:\n  - pre_tool_call\t\n",
        Raises(PluginManifestFormatError, "tab inside a plain scalar"),
    ),
    "trailing-tab-on-value": (
        "name: p\t\nprovides_hooks:\n  - pre_tool_call\n",
        Raises(PluginManifestFormatError, "tab inside a plain scalar"),
    ),
    "trailing-tab-on-key": (
        "provides_hooks:\t\n  - pre_tool_call\n",
        Raises(PluginManifestFormatError, _NOT_A_MAPPING),
    ),
    # The other side of narrowing those trims: trailing spaces must still be
    # removed, and CRLF must still read. `splitlines()` consumes CR and CRLF as
    # terminators, so no carriage return reaches a line -- checked rather than
    # reasoned, because a trailing-whitespace change is where CRLF bites.
    "crlf-with-trailing-spaces": (
        "name: p   \r\nprovides_hooks:   \r\n  - pre_tool_call   \r\n",
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    # Two more trims on the same path, found a round later: one after a closing
    # quote, one after a block-scalar indicator. Both are separation, where
    # YAML forbids a raw tab, and both were reported `unmodeled` rather than
    # refused -- so the rest of an unreadable document was read around them.
    # `tests/test_manifest_trim_policy.py` is what makes this the last round:
    # it re-derives every trim on this path from source rather than trusting
    # that a fourth hand search found them all.
    "tab-after-a-quoted-value": (
        'name: "p"\t\nprovides_hooks:\n  - pre_tool_call\n',
        Raises(PluginManifestFormatError, "tab after a quoted scalar"),
    ),
    "tab-after-a-quoted-item": (
        'provides_hooks:\n  - "pre_tool_call"\t\n',
        Raises(PluginManifestFormatError, "tab after a quoted scalar"),
    ),
    "tab-after-a-block-indicator": (
        "notes: |\t\n  body\nprovides_hooks:\n  - pre_tool_call\n",
        Raises(PluginManifestFormatError, "block scalar with an unreadable header"),
    ),
    # The same tab, nested. It refused at the top level and read one level
    # down, because the scan trimmed the value with a bare `strip()` before
    # handing it whole to the header check. That trim carried a written
    # exemption in the policy gate saying only its leading character was read --
    # true when written, false once this function gained a second consumer.
    # Both nested placements are pinned, and the gate no longer takes reasons.
    "nested-tab-after-a-block-indicator": (
        "config:\n  notes: |\t\n    text\nprovides_hooks:\n  - pre_tool_call\n",
        Raises(PluginManifestFormatError, "block scalar with an unreadable header"),
    ),
    "nested-seq-tab-after-a-block-indicator": (
        "config:\n  - |\t\n    text\nprovides_hooks:\n  - pre_tool_call\n",
        Raises(PluginManifestFormatError, "block scalar with an unreadable header"),
    ),
    # And the legitimate nested forms, which must keep reading.
    "nested-block-indicator-with-space": (
        "config:\n  notes: | \n    text\nprovides_hooks:\n  - pre_tool_call\n",
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    # The other side: a space after a block indicator is legal and must stay
    # readable, which is what makes the trims strip spaces rather than nothing.
    "space-after-a-block-indicator": (
        "notes: | \n  body\nprovides_hooks:\n  - pre_tool_call\n",
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    # --- Two more refusals of valid YAML, recorded ---------------------------
    # A quoted hook name with a trailing space: PyYAML yields 'pre_tool_call ',
    # which is not a hook name either, so nothing is lost but the diagnostic.
    # A carriage return inside a quoted value: `splitlines()` treats it as a
    # terminator, so the line breaks apart. Both are exotic, both fail safe,
    # and neither is worth a rule -- but the table is the standing check, so
    # they are here rather than nowhere.
    "quoted-hook-name-with-trailing-space": (
        'provides_hooks:\n  - "pre_tool_call "\n',
        Raises(PluginHookDeclarationError, _NOT_PLAIN_NAMES),
    ),
    "carriage-return-inside-a-quoted-value": (
        'name: "a\rb"\nprovides_hooks:\n  - pre_tool_call\n',
        Raises(PluginManifestFormatError, _NOT_A_MAPPING),
    ),
    # A KNOWN reader-only success, recorded rather than fixed. PyYAML rejects
    # this (a second mapping value on one line); the reader reports the entry
    # `unmodeled` and finds no declaration, so it says `classified` with no
    # hooks about a manifest Hermes cannot load. The obvious rule -- refuse a
    # plain scalar carrying `: ` -- was written and measured, and refused 23 of
    # the 105 shipped manifests, because a sequence item like `- name: brv` is
    # a nested mapping and not a plain scalar at all. Left open deliberately:
    # the misreport is "no declaration" rather than a fabricated hook, and a
    # rule that breaks a fifth of the real corpus is worse than the gap.
    "plain-scalar-second-colon": (
        "name: a provides_hooks:\n  - post_tool_call\n",
        Hooks(),
    ),
    # --- Key shapes refused, on the spaced-key reasoning ---------------------
    "explicit-key": (
        "? name\n: a\nprovides_hooks:\n  - pre_tool_call\n",
        Raises(PluginManifestFormatError, _NOT_A_MAPPING),
    ),
    "over-long-key": (
        ("k" * 129) + ": a\nprovides_hooks:\n  - pre_tool_call\n",
        Raises(PluginManifestFormatError, _NOT_A_MAPPING),
    ),
    # --- Values outside the subset: unmodeled, so the declaration is refused --
    "flow-sequence": (
        "provides_hooks: [pre_tool_call, post_tool_call]\n",
        Raises(PluginHookDeclarationError, _NOT_A_LIST),
    ),
    "flow-map": ("provides_hooks: {a: b}\n", Raises(PluginHookDeclarationError, _NOT_A_LIST)),
    # Moved when `_entry` started validating an inline value before abandoning
    # it as unmodeled. The anchor used to slip past unread, and the entry then
    # failed the list check; now the anchor rule sees it, which is the more
    # accurate refusal of the two. PyYAML reads this document, so the row was a
    # refusal of valid YAML before and after -- it records which one, not
    # whether.
    "anchor-definition": (
        "provides_hooks: &h\n  - pre_tool_call\n",
        Raises(PluginManifestFormatError, "anchor or alias this reader cannot resolve"),
    ),
    # PyYAML resolves this alias and yields `pre_tool_call`; the reader refuses,
    # because resolving a reference is YAML semantics it does not implement.
    # This is the one row where the reader is stricter than the reference
    # parser. The cost is a lost declaration on a valid manifest -- none of the
    # 105 shipped at v2026.9.7 uses an alias -- reported as
    # `undetermined_hook_contract` rather than guessed. The alternative, reading
    # on as though the reference had resolved, is what let `merge-key` through.
    "alias-use": (
        "base: &h\n  - pre_tool_call\nprovides_hooks: *h\n",
        Raises(PluginManifestFormatError, "anchor or alias this reader cannot resolve"),
    ),
    "inline-scalar-not-a-list": (
        "provides_hooks: pre_tool_call\n",
        Raises(PluginHookDeclarationError, _NOT_A_LIST),
    ),
    "empty-value": ("provides_hooks:\n", Raises(PluginHookDeclarationError, _NOT_A_LIST)),
    "empty-flow-list": ("provides_hooks: []\n", Raises(PluginHookDeclarationError, _NOT_A_LIST)),
    "null-word": ("provides_hooks: null\n", Raises(PluginHookDeclarationError, _NOT_A_LIST)),
    "tilde-null": ("provides_hooks: ~\n", Raises(PluginHookDeclarationError, _NOT_A_LIST)),
    "sequence-indent-shifts": (
        "provides_hooks:\n    - pre_tool_call\n  - post_tool_call\n",
        Raises(PluginHookDeclarationError, _NOT_A_LIST),
    ),
    # --- Items that are not bare hook names ---------------------------------
    "nested-list-of-maps": (
        "provides_hooks:\n  - name: pre_tool_call\n",
        Raises(PluginHookDeclarationError, _NOT_PLAIN_NAMES),
    ),
    "item-is-a-map": (
        "provides_hooks:\n  - pre_tool_call: true\n",
        Raises(PluginHookDeclarationError, _NOT_PLAIN_NAMES),
    ),
    "dash-with-no-item": (
        "provides_hooks:\n  -\n",
        Raises(PluginHookDeclarationError, _NOT_PLAIN_NAMES),
    ),
    "uppercase-hook-name": (
        "provides_hooks:\n  - PRE_TOOL_CALL\n",
        Raises(PluginHookDeclarationError, _NOT_PLAIN_NAMES),
    ),
    # A tab does not end a plain scalar here, so the comment stays part of the
    # item and the item stops being a bare hook name. PyYAML rejects the
    # document outright; either way nothing is classified.
    # Moved when the plain-scalar tab rule landed. A tab does not end a plain
    # scalar, so the comment used to stay part of the item and the item then
    # failed the hook-name check; now the tab itself is refused one step
    # earlier. PyYAML rejects this document either way, so both outcomes were
    # refusals -- the row records which one, not whether.
    "tab-before-comment": (
        "provides_hooks:\n  - pre_tool_call\t# gate\n",
        Raises(PluginManifestFormatError, "tab inside a plain scalar"),
    ),
    # --- Declarations that parse but are malformed --------------------------
    "duplicate-field": (
        "provides_hooks:\n  - pre_tool_call\nprovides_hooks:\n  - post_tool_call\n",
        Raises(PluginHookDeclarationError, "must be declared once"),
    ),
    "duplicate-entry": (
        "provides_hooks:\n  - pre_tool_call\n  - pre_tool_call\n",
        Raises(PluginHookDeclarationError, "must not repeat"),
    ),
    # `>` opens a block scalar and `=0.21.1` is not a legal header, so PyYAML
    # rejects the whole document. The reader used to call the value merely
    # unmodeled and fail one field; it now refuses the document, which is the
    # same verdict the host's parser reaches.
    "range-unquoted-folded": (
        "requires_hermes: >=0.21.1,<0.22.0\nprovides_hooks:\n  - pre_tool_call\n",
        Raises(PluginManifestFormatError, "block scalar with an unreadable header"),
    ),
    # --- Inputs the subset reads correctly ----------------------------------
    # A literal block is consumed whole: the `provides_hooks:` inside it is
    # text, and the real top-level declaration is the one that is read.
    "block-literal-hides-a-key": (
        "description: |\n  provides_hooks:\n    - pre_tool_call\nprovides_hooks:\n  - post_tool_call\n",
        Hooks(("post_tool_call", "provides_hooks")),
    ),
    "block-folded": (
        "description: >-\n  folded\n  text\nprovides_hooks:\n  - post_tool_call\n",
        Hooks(("post_tool_call", "provides_hooks")),
    ),
    "quoted-value-with-colon": (
        'name: "a: b"\nprovides_hooks:\n  - pre_tool_call\n',
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    "quoted-value-with-hash": (
        "name: 'a # b'\nprovides_hooks:\n  - pre_tool_call\n",
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    "trailing-comment-on-item": (
        "provides_hooks:\n  - pre_tool_call  # gate\n",
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    "leading-comment-line": (
        "# c\nprovides_hooks:\n  - pre_tool_call\n",
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    "crlf-line-endings": (
        "name: p\r\nprovides_hooks:\r\n  - pre_tool_call\r\n",
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    "quoted-item": (
        'provides_hooks:\n  - "pre_tool_call"\n',
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    "trailing-space-on-item": (
        "provides_hooks:\n  - pre_tool_call   \n",
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    "blank-line-inside-sequence": (
        "provides_hooks:\n  - pre_tool_call\n\n  - post_tool_call\n",
        Hooks(("post_tool_call", "provides_hooks"), ("pre_tool_call", "provides_hooks")),
    ),
    "non-ascii-value": (
        "description: 한국어 설명\nprovides_hooks:\n  - pre_tool_call\n",
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    # An escape this reader does not implement makes that ONE key unmodeled and
    # costs the caller nothing it asked for. Not support for the escape.
    "single-quote-escape-elsewhere": (
        "name: 'it''s'\nprovides_hooks:\n  - pre_tool_call\n",
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    "double-quote-escape-elsewhere": (
        'name: "a\\"b"\nprovides_hooks:\n  - pre_tool_call\n',
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    "both-fields-canonical-wins": (
        "hooks:\n  - pre_tool_call\nprovides_hooks:\n  - pre_tool_call\n",
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    "quoted-range-is-read": (
        'requires_hermes: ">=0.21.1,<0.22.0"\nprovides_hooks:\n  - pre_tool_call\n',
        Hooks(("pre_tool_call", "provides_hooks")),
    ),
    # A `hooks` key nested under another mapping is not a top-level
    # declaration, and Hermes would not read it either.
    "nested-hooks-are-not-a-declaration": ("plugin:\n  hooks:\n    - pre_tool_call\n", Hooks()),
}


class ManifestSubsetCorpusTests(unittest.TestCase):
    def test_the_corpus_covers_every_recorded_construct(self) -> None:
        # An exact count, like the other fixtures in this repo: a row removed
        # is a construct that stopped being checked.
        self.assertEqual(len(CORPUS), 81)

    def test_every_case_matches_its_recorded_outcome(self) -> None:
        for case, (manifest, expectation) in CORPUS.items():
            with self.subTest(case=case):
                if isinstance(expectation, Raises):
                    with self.assertRaises(expectation.error) as caught:
                        read_hook_declaration(manifest)
                    self.assertIn(expectation.fragment, str(caught.exception))
                    self.assertLess(len(str(caught.exception)), 200)
                    continue
                declaration = read_hook_declaration(manifest)
                self.assertEqual(
                    [(hook.name, hook.field) for hook in declaration.hooks], expectation.pairs
                )

    def test_a_refused_input_never_reports_hooks(self) -> None:
        # The invariant the table exists for: outside the subset the reader
        # raises, and it never reports a hook the document does not declare.
        for case, (manifest, expectation) in CORPUS.items():
            if not isinstance(expectation, Raises):
                continue
            with self.subTest(case=case):
                with self.assertRaises((PluginManifestFormatError, PluginHookDeclarationError)):
                    read_hook_declaration(manifest)

    def test_no_diagnostic_echoes_manifest_content(self) -> None:
        marker = "PRIVATE_CORPUS_MARKER"
        for case, (manifest, expectation) in CORPUS.items():
            if not isinstance(expectation, Raises):
                continue
            with self.subTest(case=case):
                try:
                    read_hook_declaration(manifest.replace("pre_tool_call", marker))
                except (PluginManifestFormatError, PluginHookDeclarationError) as exc:
                    self.assertNotIn(marker, str(exc))


if __name__ == "__main__":
    unittest.main()

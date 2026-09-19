# Plugin Self-Update and Code Replacement

`omh ops plugin-risk-audit --path <plugin-dir>` answers one more question an
operator has before enabling an unfamiliar plugin: can this package replace its
own installed code, outside the update path the host manages.

That matters separately from the ordinary categories beside it. A plugin that
can download replacement code and rewrite its own files is a second
software-update authority. The host's pinned plugin update path carries pinning,
rollback, consent and review guarantees; an updater living inside enabled plugin
code carries whatever guarantees its author chose, and the revision a reviewer
approved can be replaced after activation without passing that path again.

The finding is in `self_update` on the audit result, and it reaches
`summary.risk_categories` so a wrapper reading only the summary cannot miss it.

## What composes the finding

The audit does not add a self-update scanner. It composes signals it already
collects, because neither leg means anything alone: a plugin that writes a file
is not an updater, and a plugin that opens a socket is not an updater.

| Signal | What matched |
| --- | --- |
| `remote_retrieval` | The file's own `network_request` category. Not a second network detector, so the two cannot drift apart. |
| `code_replacement_write` | A write, copy, rename or replace call in the same scanned file as a code, native-module or manifest target name. |
| `archive_extraction` | An archive unpacked with `shutil.unpack_archive` or `.extractall`. |
| `host_update_command` | A self-directed package or plugin upgrade command in a file that also spawns a process. |
| `integrity_verification` | A digest or signature check. Reported separately, under `mitigating_signals`. |

Three compositions classify `detected`, and the one that matched is named in
`composition`:

| Composition | Requires |
| --- | --- |
| `remote_retrieval_and_code_replacement` | `remote_retrieval` and `code_replacement_write` |
| `remote_retrieval_and_archive_extraction` | `remote_retrieval` and `archive_extraction` |
| `host_managed_update_bypass` | `host_update_command` |

The third has one member because it is already a compound: a command that drives
the host's package manager, and a call that can run it. A plugin that upgrades
itself that way never retrieves a byte on its own.

A composition is evaluated over the whole scanned package rather than one file,
because a real updater splits retrieval and replacement across modules as often
as it does not. The `code_replacement_write` signal is the one that stays inside
a single file, so an ordinary cache write in one module and a `.py` string in
another cannot combine into a replacement.

## Three states

`classification` is one of three values, and the middle one is why the other two
are usable.

- `detected` — a composition matched. The signals behind it are in `signals`,
  and `signal_file_counts` says how many scanned files carried each one.
- `needs_review` — no composition matched, but the package can retrieve remote
  bytes and can turn text into code. Retrieved bytes can become code without any
  write reaching the filesystem, so neither of the other two answers would be
  honest. This state reaches the summary as `undetermined_self_update_path`.
- `not_detected` — no composition matched and no unsettled path was found.

## What it is not

It is advisory static evidence. Nothing is imported, registered, installed,
executed, downloaded or unpacked, and no release is resolved.

So a `detected` finding is never evidence that the plugin retrieved, verified,
staged or replaced anything, that an update ran, that a checksum or signature was
computed or valid, or that the host accepted a revision. `integrity_verification`
is recorded as a mitigation and never as a safe verdict: a digest call in the
text says a digest call is in the text.

`not_detected` is not a clean bill of health. Obfuscated update logic, a native
binary, and replacement performed by a declared dependency are all outside a
bounded static text read, and the audit's own file, byte, depth and entry limits
bound how much of a package it sees at all. The result says this in its
diagnostics rather than leaving an empty finding to be read as absence of risk.

Two shapes are worth knowing before you act on a result. A package that generates
code and also calls an API can compose `detected` without being an updater — read
`signals` and `composition`, which is why they are reported. And a manifest that
declares update metadata composes nothing, because declared metadata is not an
updater inside the plugin; that distinction is the point of requiring a call, not
a name.

Hermes remains responsible for catalog admission, revision pinning,
installation, updates, activation, rollback, permissions and execution. This
finding is something to read before handing a package to that path, not a
replacement for it.

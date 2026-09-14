# Long Document Reading

How Hermes reads a document that does not fit one read, and what the
`long-document-reading` skill adds. The skill installs with `omh setup` and
routes on plain chat ("summarize this 300-page contract", "read this manual");
its generated body is `skills/omh-long-document-reading/SKILL.md` and its
contract is `omh.workflows.long_document`. Every Hermes number below was read
off the installed Hermes Agent source in September 2026 and names its file, so
a later Hermes release can be re-measured instead of trusted.

## FAQ: Processing a very large PDF

### Can Hermes read a 300-page PDF?

Yes, in pieces, and only if something keeps track of the pieces. Hermes'
`read_file` converts a PDF to Markdown through the optional `firecrawl-anydoc`
package (`tools/read_extract.py`) and returns at most 100,000 characters per
call (`file_read_max_chars`, `tools/file_tools.py`), paginated by line
`offset` and `limit` (2,000 lines max). Dense prose extracts to about 1,600
characters per page, so 300 pages is roughly 500,000 characters, about 125,000
tokens: five reads whose text sits in the conversation with no page numbers.
Hermes compresses at a ratio of the model window (`compression.threshold:
0.50` in `hermes_cli/config_defaults.py`, floored to 0.75 for windows under
512K in `agent/context_compressor.py`), so a 200K-window model compacts near
150K tokens and a 1M-window model near 500K. The five reads alone do not
cross that; the sixth range or the reply on top of the rest of the session
can, and the summary that replaces the early ranges carries no page anchor.
A single read past the budget truncates; a run of unanchored reads is
unrecoverable once compacted.

### Why not just page through it with `offset`?

Three reasons, all measured:

- Every paginated `read_file` call re-converts the whole document; there is no
  cache, and `anydoc.to_markdown` has no page selection.
- Page numbers do not survive the conversion. The scanned-page warning speaks
  in page ranges, but nothing maps a page to an offset, so "the clause on page
  212" cannot be found by offset.
- The extracted text lands in the conversation, and once the session reaches
  the compression ratio the earlier ranges are summarized without page
  numbers, so nothing can say which page a claim came from.

### What does give page control?

Hermes' built-in `pdf` skill: argparse scripts run through the `terminal`
tool. `pdf_read.py` needs `pdfplumber`, `pdf_split.py` needs `pypdf`,
`extract_pymupdf.py` needs `pymupdf`, and `pdf_page_image.py` needs
`pypdfium2` or poppler's `pdftoppm`; none is in the shipped venv, each script
names the one it is missing, and the skill installs it once and says so.
`pdf_read.py`, `pdf_split.py`, and `pdf_page_image.py` print JSON;
`extract_pymupdf.py` prints plain text with `--- Page N/M ---` separators;
`pdf_page_image.py` exits 0 with `"rendered": false` when no rasterizer is
installed, so the skill checks that field. Where the scripts live depends on the
Hermes tree: on current main all of them sit in
`skills/productivity/pdf/scripts/` (the `ocr-and-documents` skill was merged
into `pdf`), while older trees keep `extract_pymupdf.py` and
`extract_marker.py` in `skills/productivity/ocr-and-documents/scripts/`. The
skill locates the directory with `skills_list` or `search_files` first.

| Script | What it gives |
| --- | --- |
| `pdf_read.py <file> --meta` | page count, encrypted flag, scanned flag |
| `extract_pymupdf.py <file> --pages 0-59` | text for one page range (0-indexed); the only extractor with page selection |
| `pdf_split.py <file> --pages 1-60 -o part.pdf` | a page range as a new file for `read_file` |
| `pdf_page_image.py <file> --pages 61 --out-dir imgs/` | one PNG per page for `vision_analyze` |

### What does the skill do with that?

It is a procedure and a ledger, not a parser. OMH makes no network calls and
carries no extraction dependency, so it cannot open the PDF; it tells Hermes
which tool to run in which order and records what each run covered.

1. Confirm the reading goal. One clause lookup is a search over extracted
   text, not a full read.
2. Probe the page count and scanned flags with `pdf_read.py --meta`.
3. Plan page ranges sized to the read budget: about 60 pages per 100,000
   characters. A document under about 60 pages of prose is one read.
4. Extract each range with page selection so every note carries a page
   anchor. Halve the range if a read truncates.
5. Above four ranges, send each range to a `delegate_task` child with one
   fixed brief, then merge the notes in page order. `delegate_task` fans out
   subagents (`tools/delegate_tool.py`) but nothing splits a document into
   ranges; the skill does.
6. Close every range with covered / next / missing. A compacted or resumed
   session rereads the ledger and continues from `next` instead of page 1.

The ledger is `long_document_card/v1`: one row per range with `pages`,
`offset`, `chars`, and `state`, plus a `not_observed` list (page count,
extraction, scanned-page OCR, range delegation, hosted OCR, cross-range
consistency) that stays visible until a tool result records each one.

### What about scanned pages?

`read_file` runs `pdftotext` over the document to find pages with no text
layer and prints a coverage warning naming those page ranges
(`tools/read_extract.py`). That scan has a 20-second timeout and silently
returns nothing on the largest files, so a missing warning is not proof of a
text layer; the `pdf_read.py --meta` scanned flag is the check. Recovery is
one page per `vision_analyze` call after `pdf_page_image.py`. Hosted OCR is
not a knob you turn on: `read_file` uses it by itself when `FIRECRAWL_API_KEY`
is set, `file_tools.hosted_ocr: false` turns it off even with the key
(`_hosted_ocr_config` in `tools/read_extract.py`), and the NEEDS OCR notice
says whether it was attempted. For bulk OCR of a large range the coverage
warning points at marker-pdf (`extract_marker.py` in the same skill), a
multi-gigabyte install that needs its own approval. The skill declines
scanned ranges the goal does not need and records the decision: a 300-page
scan at one vision call per page is a separate approved job, not a side
effect of a summary.

The coverage warning names an `ocr-and-documents` skill. On current Hermes
main that skill's scripts and notes were folded into `pdf`
(`references/ocr-extraction.md`, the extractors in `pdf/scripts/`); on older
trees it is a live separate skill with its own `SKILL.md` and `scripts/`
that the `pdf` skill routes scanned pages to. That is why the skill looks the
scripts up instead of assuming one path.

### Which requests go elsewhere?

| Request | Owner |
| --- | --- |
| Explain this paper at a beginner level | `paper-learning` |
| Turn this PDF into slides, compare two PDFs, tables into CSV | `materials-package` |
| OCR this screenshot or receipt, transcribe this recording | `media-input-operator` |
| Find the PDF of this report | `source-finder` |
| Summarize this paragraph | a direct answer, no workflow |

### Config knobs

- `file_read_max_chars`: raises the per-call character budget; the skill
  re-plans `pages_per_range` from it.
- `FIRECRAWL_API_KEY`: turns hosted OCR on inside `read_file`; `file_tools.hosted_ocr: false` turns it off even with the key. Nothing turns it on without the key.
- `web.extract_char_limit`: bounds `web_extract` on a URL-hosted PDF.
- `delegation.max_concurrent_children`: bounds range fan-out.

### TUI attachments

The Modern TUI's `pdf.attach` rasterizes 25 pages per call
(`_PDF_ATTACH_MAX_PAGES`: `tui_gateway/prompt_attachments.py` on current
main, `tui_gateway/server.py` and `tui_gateway/methods_prompt.py` on older
trees), so attaching a 300-page PDF to the
prompt is twelve attachments of images, not a read. Give Hermes the file path
and let the skill read it.

## Contract

`omh.workflows.long_document` exposes:

- `build_long_document_card(...)`: the card above, from a title, source
  reference, document kind, page count, covered and scanned ranges, and read
  budget.
- `build_chunk_ledger(page_count, ...)`: the range rows with page anchors and
  exactly one `next` row.
- `plan_page_ranges(page_count, pages_per_range)` and
  `pages_per_range_for_budget(char_budget, chars_per_page)`.
- `normalize_long_document_source_state(...)`: `metadata_only`,
  `page_count_observed`, `range_text_observed`, `full_text_observed`, or
  `unknown_or_missing`; full text is claimed only when the covered ranges
  reach the page count.
- `validate_long_document_card(card)`: structural errors, never repairs.

A prepared card is metadata. It is not page-count, extraction, OCR, delegation,
or whole-document coverage evidence.

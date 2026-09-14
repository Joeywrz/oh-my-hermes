# Hermes PDF Limits (measured 2026-09)

What the installed Hermes Agent does with a large PDF, read off its source tree. Each row names the file the number comes from so a later Hermes release can be re-measured instead of trusted. None of these numbers is an OMH guarantee.

## Budgets

| Surface | Limit | Where |
| --- | --- | --- |
| `read_file` characters per call | 100,000 (`file_read_max_chars`) | `tools/file_tools.py` |
| `read_file` lines per call | 2,000 max (`limit`) | `tools/file_tools.py` |
| Document size cap for extraction | 50 MB | `tools/read_extract.py` |
| TUI `pdf.attach` rasterization | 25 pages per call | `tui_gateway/prompt_attachments.py` |
| Scanned-page coverage scan | `pdftotext`, 20 s timeout; silently returns nothing when it times out | `tools/read_extract.py` |
| Dense prose per page | about 1,600 characters, so about 60 pages per read | measured, not configured |

A 300-page document is therefore about 500,000 characters, about 125,000 tokens: five reads, and more than the conversation-compression threshold in `agent/conversation_compression.py`, which is why an unanchored full read is summarized away.

## What `read_file` does not do

- It converts `.pdf` to Markdown through the optional `firecrawl-anydoc` package and paginates the text by line `offset` / `limit`; every paginated call re-converts the whole document, and `anydoc.to_markdown` has no page selection.
- Page numbers do not survive the conversion. The scanned-page warning speaks in page ranges, but nothing maps a page to an offset; the chunk ledger is that map.
- `ripgrep` skips binaries, so there is no search inside a PDF until a range is extracted to text.
- The runtime warning and user guide name an `ocr-and-documents` skill; its content was merged into the `pdf` skill's `references/ocr-extraction.md`.

## What gives page control

The built-in `pdf` skill (`skills/productivity/pdf/scripts/`, argparse CLIs run through `terminal`, JSON on stdout). Its dependencies (`pypdf`, `pdfplumber`, `pymupdf`) are not in the shipped venv; each script prints an install hint when one is missing.

| Script | Use |
| --- | --- |
| `pdf_read.py <file> --meta` | page count, page sizes, encrypted and scanned flags |
| `pdf_read.py <file> --text` | per-page text as JSON (whole file; pipe into a file for large documents) |
| `extract_pymupdf.py <file> --pages 0-59` | the only extractor with page selection (0-indexed) |
| `pdf_split.py <file> --pages 1-60 -o part.pdf` | 1-based range into a new file for `read_file` |
| `pdf_page_image.py <file> --pages 61-62 --out-dir imgs/` | PNG per page for `vision_analyze` |

## Delegation

`delegate_task` fans out subagents (`tools/delegate_tool.py`) but nothing splits a document into ranges; the parent plans the ranges and sends one fixed brief per child. Above 4 ranges, delegation keeps each child's context to one range; below it, sequential reads cost less. `delegation.max_concurrent_children` bounds the fan-out.

## Config knobs

- `file_read_max_chars` raises the per-call character budget; re-plan `pages_per_range` from it.
- `file_tools.hosted_ocr` enables hosted OCR for scanned pages.
- `web.extract_char_limit` bounds `web_extract` on a URL-hosted PDF.
- `delegation.max_concurrent_children` bounds range fan-out.

# Markdown fixtures

Real files for the `markdown` adapter (`neptune.adapters.markdown`). Hand-written; bytes matter (a BOM,
CRLF endings, invalid UTF-8, a file cut inside a character), so edit them with care. No line has trailing
whitespace. Expected outcomes are asserted in `tests/unit/adapters/test_markdown_adapter.py`.

| File | Case | What the adapter must do |
|---|---|---|
| `pump_sop.md` | front matter, headings (ATX, setext), paragraph, lazy blockquote, nested lists, fenced code, GFM table (an empty cell, an escaped pipe), HTML comment, link definition | title from front matter; roles and levels from syntax; a table with three body rows |
| `site_manifest.md` | asset register as a GFM table and an indented code block | header and rows cell by cell, a blank cell `Unknown` |
| `datasheet_crlf.md` | UTF-8 BOM and CRLF endings | spans after the BOM; CRLF kept inside blocks |
| `notes.md` | plain text with a Markdown name | probed at 0.5, above `text`; two paragraphs |
| `runbook` | extensionless, heading and fenced code | selected from its bytes |
| `truncated.md` | `pump_sop.md` cut one byte into a `·` | the table's text and its cut cell `Unknown`, `markdown.invalid_utf8` |
| `corrupted.md` | bytes `ff fe` inside a paragraph | that block `Unknown`, `markdown.invalid_utf8`; the others decode |
| `empty.md` | empty | a document with no blocks |
| `deep_nesting.md` | blockquotes nested 100 deep | `markdown.nesting_limit`; the rest read |

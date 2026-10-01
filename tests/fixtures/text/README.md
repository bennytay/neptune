# Plain-text fixtures

Real files for the `text` reference adapter (`neptune.adapters.text`), one per case the adapter
checklist in `docs/adapter-contract.md` asks for. Hand-written; bytes matter, so edit them with care.

| File | Case | What the adapter must do |
|---|---|---|
| `notes.txt` | valid: LF endings, blank-line paragraphs, non-ASCII and an emoji | three blocks, spans in code points |
| `operator_log` | renamed / extensionless: UTF-8 BOM and CRLF endings | detected from its bytes; spans start after the BOM; CRLF kept inside blocks |
| `truncated.txt` | truncated: ends one byte into a two-byte character (`·`) | last block's text `Unknown` plus `text.invalid_utf8` |
| `corrupted.txt` | corrupted: bytes `ff fe` inside a paragraph | that block `Unknown` plus `text.invalid_utf8`; the others decode |
| `empty.txt` | empty | a document with no blocks |

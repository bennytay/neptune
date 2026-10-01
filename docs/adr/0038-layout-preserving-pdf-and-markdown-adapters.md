# 0038 — Layout-preserving PDF and Markdown adapters: pypdf, markdown-it-py, declared order and exact spans

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-28
- Builds on: ADR 0001 §4 (dependency placement), ADR 0020 §4–§5 (documents and tables), ADR 0023
  (frozen schema), ADR 0024 (the ABI and the reference `text` adapter), ADR 0030 and 0033 (sandbox)

## Context

Robotics teams keep procedures, site manifests, datasheets and runbooks as PDF and Markdown. MVL-28
asks that a downstream learner cite exact source spans and rebuild a document's structure without
treating it as chunks. ADR 0020 fixed the records (`DocumentRecord`, `DocumentBlock` with a
`[Page, Span]` citation and a `[Page, PageRegion]` region, tables as `StructuredTable` and
`StructuredRecord`) and left the block rule to each adapter. Plain text is done (ADR 0024 §10).

The forces: PDF is a binary format with a long history of hostile files (object-stream bombs,
recursion bombs, broken cross-reference tables, JavaScript, embedded files, encryption). Its text
has no reading order of its own except the order content is drawn in, or the structure tree of a
tagged file; everything else (columns, headings from font sizes) is layout analysis, which is
inference and belongs to `derived/`. Markdown's structure is its syntax, but CommonMark parsers
report lines, not offsets. Adapters run forked, with no network and no writes (ADR 0030), and must
give byte-identical output on every host.

## Decision

1. **Two adapters in two subpackages: `adapters/pdf/` (id `pdf`) and `adapters/markdown/` (id
   `markdown`).** `text` is unchanged. They share no code worth a shared module (one is binary,
   one is text), and separate ids keep their versions, libraries and lineages apart: a PDF fix
   never re-lineages Markdown. Each subpackage imports its own modules relatively; adapters never
   import each other.
2. **Dependencies, both pure Python and pinned by `uv.lock` and the descriptor's `libraries`:**
   - **pypdf 6.19** for the file structure: cross-reference tables and streams, object streams,
     incremental updates, filters, encryption, the page tree and content-stream tokenizing. BSD,
     no required dependency, maintained against a fuzzing corpus, and it bounds decompression
     and the page tree through `pypdf.apply_configuration`. Its own text extraction is **not**
     used: it applies undocumented spacing heuristics, gives no offsets, and renders a code it
     cannot map as its glyph name (`/g42`). The adapter has its own content interpreter, font
     decoding and structure-tree reader. It reads pypdf's encoding tables, the Adobe Glyph List
     and the Core 14 metrics from `pypdf._codecs` (data, pinned with pypdf).
   - **markdown-it-py 4.2** (with `mdurl`) for CommonMark plus the GFM table rule. Its inline core
     rule is off: inline Markdown is never parsed. Each leaf block rule is wrapped to record the
     lines it consumed as the parser saw them inside their containers, which gives exact spans
     without re-implementing container prefixes.
3. **PDF: blocks, reading order and text** (rules in `adapters/pdf/_page.py`):
   - Untagged page: one block per text-showing operator (`Tj`, `TJ`, `'`, `"`), one figure
     block per image painted, in content-stream order. Tagged page: content whose MCID the
     structure tree owns is one block per owning element per page, in structure-tree order; a
     table (any content under a `TR`) is one block; each `Artifact` sequence is one block
     (`header` or `footer` when its `/Subtype` says so); untagged content on a tagged page is a
     block per run. Owner blocks first, then the rest in content order.
   - Owners are the nearest ancestor of a role-bearing, block-level standard type (after
     `/RoleMap`): `P` paragraph, `H`/`H1`–`H6`/`Title` heading (level from the digit), `LI` list
     item (level = enclosing `L`s), `Caption`, `Figure`, `Formula`, `BlockQuote` quote,
     `Note`/`FENote` footnote, `TOCI` (role unknown). Otherwise the top-level element, role
     `Unknown`. An untagged page declares no roles: `Unknown`, never a guess from font size.
   - A page's extracted text is its blocks' texts, each followed by LF. Within a block, runs join
     with LF when the next one starts a new line (more than half a font size off the baseline),
     with one space for a gap of `space_threshold` thousandths of an em (default 200) on the same
     line, otherwise directly; inside a `TJ` the same threshold puts a space. Table cells join by
     TAB and rows by LF. A block with no text is U+FFFC and has `NotApplicable` text. A code no
     font maps to text is U+FFFD and makes its block's text `Unknown` (`pdf.unmapped_glyphs`), as
     the `text` adapter does for invalid UTF-8.
   - `order` is `page × 2^24 + position on the page`. It sorts in reading order whatever the
     chunking, with gaps between pages, as the `text` adapter leaves gaps; a dense number would
     need every page interpreted in `plan`, which would make the plan as expensive as the ingest
     and let one hostile page fail the whole source.
4. **PDF: regions are computed from what the file declares, never from defaults.** A run's box is
   its glyph advances (declared widths, `Tc`, `Tw`, `Tz`, `TJ` displacements) from the font's
   descent to its ascent, through the text matrix and the CTM, in default user space, rounded to
   0.001. Widths come from `Widths`/`W`/`DW` or the Core 14 metrics; ascent and descent from the
   font descriptor, the Core 14 metrics or a Type3 `FontBBox`. A font that declares neither leaves
   the region `Unknown` (`pdf.geometry_unknown`); pypdf's default metrics are not used.
5. **PDF: tables a tagged file declares are structured records.** A `Table` element is one
   `StructuredTable` citing it by `[pdf:structure(path)]` (child indices from the
   `StructTreeRoot`), so every chunk derives the same id. Its header is row 0 when every cell of
   it is `TH`, else `NotApplicable`; its name is its `Caption` child's text. Each other row is a
   `StructuredRecord` emitted by the chunk of the page its content starts on, every cell citing
   its `[Page, Span]`; an empty cell is `Unknown`. Untagged tables are layout and are not guessed.
6. **PDF: values cite where they are declared.** The title cites the `/Info` object, labels the
   object holding `/PageLabels`, sizes and rotation the page (`[Page(p)]`); an absent `/Rotate`
   is `Known(0)` citing the header, the specification's default. Two adapter steps:
   `pdf:object` (number, generation, as the last cross-reference section resolves it) and
   `pdf:structure` (path). Absent labels and titles are `Unknown`; labels are never the index.
7. **PDF: hostile input is bounded, reported and never run.**
   - Config bounds, each a `pdf.content_limit` finding when hit: `max_stream_bytes` (64 MiB, every
     stream pypdf inflates), `max_page_content_bytes` (16 MiB per page) and `max_page_operations`
     (1,000,000). Forms nest 8 deep, `q` 1,024 deep, structure walks 200,000 visits. Recursion
     bombs end in `RecursionError`, a finding. `jbig2dec` is never started; images are never
     decoded.
   - A file pypdf cannot open as written (truncated, broken cross-reference) is opened once more
     with an in-memory tail naming the last `/Type /Catalog` object, so pypdf rebuilds the table
     by scanning objects (`pdf.repaired`). The source is never changed. Otherwise `pdf.unreadable`.
   - Encryption: read only RC4 with the empty user password (pure Python in pypdf). AES is never
     decrypted, because whether it can be depends on a native library being installed, which
     would make output depend on the host. An unreadable file still lists its pages
     (`pdf.encrypted`).
   - JavaScript and embedded files are counted and reported (`pdf.javascript`,
     `pdf.embedded_files`), never executed or opened. pypdf's log warnings become findings, never
     output.
   - Chunk 0 is the document; every later chunk holds whole pages (8 by default, a constructor
     argument), so a page that exhausts the sandbox costs its chunk, not the source.
8. **Markdown: CommonMark's leaf blocks with text, roles from syntax, exact spans.** Paragraphs,
   ATX and setext headings (span: the heading text without markers), fenced and indented code
   (whole content lines), HTML blocks (role `Unknown`) and GFM tables. A paragraph's role is that
   of its innermost container: `list_item` (level = list depth), `quote`, else `paragraph`. Block
   text is the cited source span verbatim, inline markup included. A GFM table is also a
   `StructuredTable` (name `NotCovered`, header row 0) and a `StructuredRecord` per body row,
   cells split at unescaped pipes, trimmed, `\|` unescaped. Leading YAML front matter is not
   parsed as blocks; a top-level `title:` with a plain or quoted one-line value is the title,
   anything else `Unknown` with `markdown.title_unreadable`. Containers nest 64 deep; deeper
   content is `markdown.nesting_limit`. Two chunks: the document, and every block, since
   CommonMark's blocks depend on the whole file.
9. **Probing.** PDF: a `%PDF-` header in the first 1,024 bytes is `SIGNATURE`, with the declared
   version. Markdown cannot be told from text by bytes alone, so the name breaks the tie only
   where the bytes cannot: a fence or a GFM delimiter row, or two kinds of heading, link,
   emphasis, blockquote or code span, is `STRUCTURE` (0.7) whatever the name; UTF-8 text with a
   Markdown extension and no syntax is 0.5, just above `text`'s `GENERIC`; damaged or empty text
   with a Markdown extension is 0.2, just above `text`'s `NAME_ONLY`. List markers are never
   counted: YAML and plain notes use them.
10. **What schema version 1 cannot hold stays in the bytes, for now.** Per-run fonts, inline runs
   (links, emphasis) and document metadata beyond the title (author, dates, producer, XMP, other
   front matter keys) have no field (ADR 0020 §4 deferred them). Adding companion kinds bumps
   `SCHEMA_VERSION`, which rewrites every golden package (ADR 0023 §1); that is its own change,
   not a side effect of an adapter. MVL-78 tracks it.

## Alternatives considered

- **pdfminer.six.** Positions per character, but it requires `cryptography` (a native wheel) and
  `charset-normalizer`, and its useful output comes from layout analysis (`LAParams`), which is
  inference. Without it, it gives what our interpreter gives, slower.
- **PyMuPDF.** Fast and thorough, but AGPL and a native MuPDF: a C parser of hostile input in the
  sandboxed process and a licence the project cannot ship.
- **pypdf's `extract_text`.** No offsets, heuristic spacing, glyph names for unmapped codes, and
  no structure tree. Citations would be unverifiable.
- **Our own PDF object parser.** Full control of every bound, but cross-reference streams,
  object streams, filters with predictors, encryption and repair are the larger part of PDF;
  ADR 0001 prefers wrapping a maintained reader, and pypdf's limits cover the same risks.
- **One `document` adapter for both formats.** One version and one transform for two unrelated
  decoders: a Markdown fix would re-lineage every PDF.
- **Blocks as text objects (`BT`…`ET`) or as lines.** pdfTeX draws a whole page in one text
  object; lines are a geometric grouping, which is layout analysis. One block per showing
  operator is the smallest unit the file declares with one font and one position.
- **A dense `order` per document.** Needs every page's block count in `plan` (see §3).
- **Decrypting AES when a crypto library happens to be installed.** Output would depend on the
  host's packages; determinism is defined against `uv.lock` (ADR 0001).
- **Fonts as adapter locator fields, or tables of runs as `StructuredTable`s.** A locator is an
  address, not a value, and a table the source does not declare is invention. A companion kind is
  the honest place (§10).
- **mistune or commonmark.py for Markdown.** mistune is not strictly CommonMark and reports no
  positions; commonmark.py is unmaintained. markdown-it-py is CommonMark-compliant and its rule
  chain lets the adapter record exact positions.
- **Markdown block text without container markers.** Lazy continuation and blockquote prefixes
  make such text non-contiguous: it could not be one span. Text is the span verbatim.

## Consequences

- A PDF procedure or a Markdown runbook becomes pages, blocks, spans, regions and tables a
  learner cites exactly; `Span` offsets mean these transforms' extracted text, as ADR 0016 says.
- Tagged PDFs (Word and most office exports) carry declared headings, lists, figures and tables;
  untagged ones give runs with `Unknown` roles for `derived/` to interpret.
- pypdf's private data tables and its log channel names are pinned by version; a pypdf upgrade is
  a new lineage through `libraries`, and the tests catch a renamed internal.
- Scanned PDFs yield images, never text: OCR is a derived annotation.
- The built-in registry now has three adapters; tests that assumed `text` alone were rewritten to
  hold for any set, and every source is probed by each.
- Revisit when the schema gains document companion kinds (MVL-78), if consumers need dense
  `order`, if untagged tables must be structured (a derived reading), or if a pure-Python AES
  becomes acceptable.

# 0059 — XLSX workbooks in the tabular adapter: sheets as tables of cited cells

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-201
- Extends: ADR 0042 (the tabular adapter), ADR 0020 §5 (`StructuredTable`, `StructuredRecord`),
  ADR 0029 (hostile files), ADR 0016 (locators)

## Context

CMMS, ticketing and register exports are often XLSX. Deploy's lifecycle mapper reads the
compiler's `StructuredTable` and `StructuredRecord` cells and never a source's bytes, so a workbook
has to arrive as the same tables a CSV does, each cell citing the place it was read from. An XLSX
is an OOXML package: a zip of XML parts that hold shared strings, styles, a workbook part and one
part per sheet. It is also untrusted input: a zip can inflate a thousand-fold, a string table can
hold millions of entries, XML can declare entities, a workbook can link to other files and a
macro-enabled one carries a VBA project. The model needs no new record: this ADR decides how a
workbook maps onto the existing ones.

## Decision

The `tabular` adapter reads XLSX (version 0.2.0). No new record kind and no schema change: it emits
`StructuredTable` and `StructuredRecord` only. The reader is the standard library: `zipfile` after
the bounds of ADR 0029 §2, and expat for XML, with the protections done by hand. No dependency is
added, so `libraries` is unchanged. A source is a workbook when its zip parts say so (§8), never
its name.

1. **One table per sheet**, in the workbook's order, named as the workbook declares (`name` is
   `Known`, citing the sheet's tag in the workbook part). Sheets that are not worksheets (chart
   sheets), whose part is missing or unresolvable, or whose part is refused or has no rows
   section, are still a table: `header` is `NotCovered`, there are no rows, and a finding says
   why. The cells of a sheet are the cells of its `<sheetData>` and nothing else: pictures,
   charts, comments, pivot tables, merged ranges (a merged range's other cells are simply blank
   cells) and defined names stay in the zip.
2. **Rows and columns as the sheet numbers them.** The record for sheet row `n` has `row = n - 1`
   and its cells are by column from column A, so a row's cell `c` is column `c`. A place with no
   `<c>` is a blank cell and a short row keeps its length. A row with no cell has no record. Rows
   and cells with no `r` attribute take the position after the one before them. A row that does not
   come after the rows before it, and a cell whose reference is not an A1 reference of its row or
   does not follow the cell before it, is dropped with a finding (`xlsx_row_order`,
   `xlsx_cell_ref`); its place is a blank cell. The header is `csv_header` (the option's name stays
   for the CSV case: `first_row` makes a sheet's first row its header and gives it no record,
   `none` is `NotApplicable`, the default is `Unknown` and the first row is a record). A first row
   that cannot be read (over `max_row_bytes`, more cells than `max_columns`, a shared string not
   covered) leaves the header `Unknown` and has no record: no data row stands in for it.
   Deploy's mapper therefore reads a sheet exactly as it reads a CSV, with the same switch. Two
   `<sheet>` tags that resolve to one part would give two tables the same rows' ids: the first is
   read, the second is a table with no rows and an `xlsx_sheet_unsupported` finding (`part_shared`).
3. **Cells as declared (ADR 0020 §5, non-negotiable 4).** A number is the stored number: an int
   when an int64 or uint64 holds the literal, a double when the double's shortest digits equal it
   (`INF`, `-INF` and `NaN` are the non-finite reals), else its literal text with an info finding
   (`xlsx_number_text`; a `-0` integer literal is such a case, not an int 0). A boolean is a boolean; a string, an inline string and a shared string
   are text (rich-text runs concatenated, phonetic runs excluded); an error (`#N/A`) is its text,
   marked `error`; an ISO 8601 date cell (`t="d"`) is its text. **A date is its serial, a number,
   with no conversion, no zone and no date detection.** A cell's `numFmtId` and, when the
   workbook defines it (id 164 and up), its format code, are fields of the cell's citation: that
   is how a consumer can see a number is formatted as a date, and the reader does not decide.
4. **The date system is stated, not applied.** The `workbook` table (a table of the source
   with columns `property`, `value`) holds the epoch: row `date_epoch` is 1904 or 1900, `Known`
   with `assertion_kind` `stated` citing the `<workbookPr>` tag, only when the workbook declares
   `date1904`. With no declaration it is `Unknown` citing the workbook part: ECMA-376's default
   (the 1900 system) belongs to the specification, not to this workbook, and applying it to a
   serial is the consumer's decision (a derived annotation), never a canonical fact. The table
   also holds `sheet_count` and, per sheet, `sheet` (its name) and `sheet_state` (`hidden`,
   `veryHidden`, as declared; the default `visible` is not a row). A serial in a 1904 workbook is
   not the same date as in a 1900 one and is never rebased.
5. **Formulas keep the cached value and the formula, each marked.** A formula cell is its cached
   `<v>` value, typed as above, with `content` `formula` in its citation step. A formula with no
   cached value is `Unknown` (and an info finding `xlsx_formula_no_value`): nothing is ever
   calculated. The formula text is a row of the sheet's `formulas` table (a table citing the same
   sheet, columns `ref`, `formula`, `kind`, `si`, `range`), each cell citing the cell's `<f>`
   element (`content` `formula_text`), the header row's formulas included. A row's `row` is the
   formula's ordinal among the sheet's formulas as the sheet has them: a dropped row or cell
   still holds its place, so no block boundary changes an ordinal. A shared formula's children have no text: `Unknown`, with
   the shared index. `kind` is the `t` attribute (`normal` when absent, the format's default).
6. **Blank, absent and empty string are three things, all `Unknown`.** The model holds no empty
   text (ADR 0020 §5; ADR 0042 §1 does the same for JSON and Parquet), so the empty string is
   `Unknown`; what distinguishes them is the `content` field of the cell's citation step:
   `blank` (a `<c>` with no value), `missing` (no `<c>`: the citation is the row), `empty_string`
   (a cell that holds `""`). A whitespace-only string is `Known`. Nothing becomes a value by
   being blank.
7. **Citations.** Every cell has its own provenance (a row cited otherwise than `Row(r)`, ADR
   0020 §5): `[ByteRange(the part's stored bytes in the zip), ByteRange(the cell's XML in the part's
   inflated bytes), tabular:xlsx_cell]`. The first step is the member's span (its local header,
   data and descriptor), as ADR 0029 cites archive members; the second addresses what the first
   decodes (ADR 0016 §1); the step's fields are `part` (the part's name), `sheet`, `ref` (the A1
   reference), `content` and optionally `numfmt` and `format`. A row cites its `<row>` element, a
   table cites the workbook part's `<sheet>` tag under `tabular:xlsx_sheet` (formulas:
   `tabular:xlsx_formulas`; the workbook table `tabular:xlsx_workbook`). Offsets are bytes of the
   inflated part, whatever the part's encoding declares.

8. **Probing.** The probe never inflates and a file name never counts. A whole file in the head
   whose central directory holds `[Content_Types].xml` and `xl/workbook.xml` is `VERIFIED`; a
   whole file whose directory does not (a renamed word-processor file, a plain zip with an `xl/`
   folder) is declined. A zip too large for the head, or cut off so its directory does not read,
   is `SIGNATURE` only when its leading local headers name `xl/workbook.xml` or a worksheet part;
   every other zip is declined, a binary workbook (`xl/workbook.bin`, XLSB) included.
9. **Blocks.** `plan` parses each worksheet once and streams it: it only counts and bounds rows,
   and cuts blocks between rows: 4,096 rows, 32,768 cells or about 1 MiB of the part, whichever
   comes first. `ingest` reads one block: deflate cannot seek, so it inflates the part from its
   start, takes the part's prologue (up to `<sheetData>`, at most 1 MiB, so a sheet's namespace
   prefixes are the part's own) and the block's bytes, and parses those. A block therefore costs
   up to the whole part, and a sheet at the cell limit costs about 30 blocks times its part:
   bounded by the limits in §10, in the same spirit as ADR 0042 §8's Parquet slices. The shared
   strings and styles are read again by each chunk, within their limits. Block bounds are
   constants of this adapter version, so chunk ids and findings are deterministic.
10. **Hostile input.** All limits are settings (so a different limit is a different lineage), all
    checked before the thing they bound is read, and an exceeded limit is a finding naming it
    (`xlsx_limit`, `details.limit`) with the thing it concerns `NotCovered`:

    | Setting | Default | What exceeding it does |
    |---|---|---|
    | `xlsx_max_parts` | 10,000 | the zip's directory (its declared and its walked entries, bounded before `zipfile` builds them) is not read: one table, `NotCovered` |
    | `xlsx_max_total_bytes` | 1 GiB | declared uncompressed bytes of all parts: the workbook is not read |
    | `xlsx_max_compression_ratio` | 100 | declared uncompressed per stored byte, for the whole zip and for each part read; a bomb is refused before any byte is inflated |
    | `xlsx_max_part_bytes` | 128 MiB | declared and, counted while inflating, actual bytes of one part: it is not read; the inflated bytes are counted because a declared size can lie |
    | `xlsx_max_shared_strings`, `xlsx_max_shared_string_bytes` | 1,000,000, 32 MiB | strings past it are not covered: a cell naming one is `NotCovered`, not `Unknown` |
    | `xlsx_max_styles` | 100,000 | formats past it are not read: cells keep their values, without a `numfmt` |
    | `xlsx_max_cells` | 1,000,000 | per sheet, counting the cells made (a kept gap is a blank cell): rows from the one that crosses it are not read |
    | `xlsx_max_gap_ratio` | 64 | blank cells a row makes before a real cell: at most this many per real cell kept before it, plus one. The first real cell past it and all after it are not covered: the row ends with one `NotCovered` cell (content `not_covered`, at the column after the last kept cell) and `xlsx_limit` says so. A 2 KB sheet of cells in column XFD would otherwise make 16,383 blanks each |
    | `xlsx_max_sheets` | 256 | sheets past it have no table; `sheet_count` says how many there were |

    The existing `max_rows`, `max_row_bytes` (a row's XML, and so a cell's text) and `max_columns`
    apply to sheets (`row_limit`, `row_too_large`, `too_many_columns`). XML nesting past 64 elements, in
    every part this reader parses (the package and workbook relationships, the workbook part, shared
    strings, styles and sheets), is a limit finding at that element; expat holds nothing of the
    rest. A sheet tag too long to cite (longer than a read piece) is a limit finding too, never a
    zero-length citation. An encrypted part, a compression other than stored or deflate, or a part that is
    not UTF-8 is `xlsx_part_refused`. **XML is read by expat with every document type declaration
    refused**, so no entity is declared, expanded or fetched and no external DTD is loaded; nothing
    in a part names a file Neptune opens. A relationship that leaves the package is never
    resolved.
11. **Nothing is followed, nothing runs.** External relationships and external link parts are
    named in one `xlsx_external_link` finding (the targets, at most ten, as text) and never
    opened. A VBA project (`vbaProject.bin`, `.xlsm`) is never read, parsed or run: one
    `xlsx_macros` finding cites its bytes. A formula that mentions another workbook is a formula
    (§5) and nothing more.
12. **Damage is a finding and a smaller output, never a failure.** A truncated zip, a missing
    workbook part or a directory that does not read is `xlsx_corrupt` and one `NotCovered` table
    citing the source. A part whose XML breaks keeps every row before the break: the break is
    `xlsx_corrupt` citing the part and the offset (`details.offset`). A shared-string index out of
    range is `Unknown` with `xlsx_shared_string_ref`; a value that does not read as its declared
    type is `Unknown` with `xlsx_cell_unreadable`. Findings about rows are one per code per block,
    as in ADR 0042 §10, citing the part's bytes from the first to the last row they name.

## Alternatives considered

- **`openpyxl` or `pandas`.** They interpret: dates become datetimes by their own epoch rule,
  formulas and cached values merge, blank and empty cells follow their policy, and they cannot
  report the cell's bytes. They would also add a dependency to every transform id and cache key
  for a format whose grammar is small. Rejected for non-negotiables 4 and 10; `openpyxl` is the
  fixtures' official reader (`make_xlsx_fixtures.py --check`), never the adapter's.
- **`defusedxml` or `xml.etree`.** Neither reports byte offsets, which a citation needs; expat
  does, and refusing every document type declaration is the whole of `defusedxml`'s protection that
  OOXML needs (a conforming part has none).
- **A new record kind for workbooks, sheets or cells.** `StructuredTable`/`StructuredRecord`
  already hold sheets and rows (ADR 0020 §5 names a spreadsheet's sheet), and Deploy maps them
  unchanged. The schema version is contended; nothing needed a new kind.
- **Convert date serials to timestamps.** It needs the epoch and a guess whether a number is a
  date (a number format is a rule a locale applies): non-negotiables 4 and 8. The serial, the
  format id and the epoch are recorded; a derived annotation under `derived/` may read them.
- **One chunk per sheet.** A million-cell sheet's records would not fit a sandboxed call's
  memory. Blocks bound it, at the cost of §9's re-inflation.
- **Formula text in the locator or as a second cell value.** A locator addresses, it does not
  hold content, and a cell has one value; the formula is its own row in its own table, cited at
  its `<f>` element.
- **`KnownAbsent` for a blank cell.** No source defines blank as "none" (ADR 0004 §5): it stays
  `Unknown`.
- **Inflate the zip with `inspect_archive` first.** It inflates every member once and spools
  nested archives in scratch space, which a sandboxed `plan` need not have. This adapter keeps
  its limits (§10) to the bounds the directory and the parts it reads need, using the same
  directory checks, restated: an adapter imports only the model, identity and the contract
  (ADR 0008 §4), so `neptune.discovery.archive`'s helpers cannot be called from here, and are
  mirrored (end record, zip64 locator, the walked entry count, the directory cap).

## Consequences

- A workbook is queried as any table, and each value traces to the cell's XML and part without
  parsing the workbook again. Deploy's lifecycle mapper reads a work-order workbook exactly as it
  reads the same rows as CSV (checked against MVL-113's mapper: the same maintenance events, ids
  and clocks aside; the one difference is its info finding `table_unmapped` for the `workbook`
  table, which has no column a mapping names).
- Changing any constant in §9 or the reading rules changes chunk ids and so creates new lineage
  (non-negotiable 6). The adapter's version is 0.2.0: the new `xlsx_*` settings enter the config
  hash, so every tabular output, CSV and JSON included, has new record ids; their cells and
  citations are unchanged, and the golden files under `tests/golden/tabular/` were regenerated for
  that and nothing else.
- Spreadsheets as people read them (a banner above the header, two header rows, a total row) are
  not detected: `csv_header: first_row` takes the sheet's first row, and anything cleverer is a
  manifest's decision or a derived annotation.
- XLS (BIFF, OLE2) and ODS are not read. Encrypted (password-protected) workbooks are OLE2 files
  and are not claimed; an encrypted part inside a zip is refused.
- Pictures, charts and pivot caches are not read. A future adapter version may cite them.
- A part is re-inflated per block: a workbook at the limits costs seconds per sheet. Revisit with
  an inflate cache in the workspace if a real workbook shows it.

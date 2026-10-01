# 0042 — The tabular adapter: CSV, JSON and Parquet as tables of cited cells

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-30
- Extends: ADR 0020 §5 (`StructuredTable`, `StructuredRecord`), ADR 0024 (adapter ABI), ADR 0027
  (probing), ADR 0030 (sandbox)

## Context

Asset registers, inspection exports, experiment tables and telemetry summaries arrive as CSV, JSON
and Parquet. Flattened to text they lose their types, their nulls and their place. The acceptance
test is that a downstream system can query a structured value and trace it to the exact source row
and cell without parsing the file again. The model already has the records (ADR 0020 §5), so this
ADR decides how three very different formats map onto them, how the adapter recognises them next to
the configuration adapter (MVL-23) which also claims JSON, and what bounds a hostile table.

## Decision

One adapter, `tabular`, no new record kind and no schema change: it emits `StructuredTable` and
`StructuredRecord` only. Which layout a source has is decided from its bytes, never its name: the
Parquet magic, then JSON shape, then CSV dialect.

1. **No inference.** "Type inference" is the types the source declares. CSV cells stay text (`007`,
   `1e5`, `n/a` are text); a blank or whitespace-only CSV field is `Unknown`, never `""`, because CSV has no null. JSON
   keeps its own types; Parquet keeps its declared ones.
   A *declared* string is a value, not missingness, so whitespace-only JSON and Parquet strings
   are `Known`; only `""` is `Unknown`, because the model holds no empty cell text (ADR 0020 §5)
   and the cell's citation still names the `""` it came from. Nothing is coerced, parsed as a date or unit-converted.
2. **CSV dialect.** UTF-8 (after a BOM); records end at LF outside quotes, a CR before it belongs to
   the ending; `"` quotes, `""` is a quote. The delimiter is `csv_delimiter` or, for `auto`, sniffed
   from the head: comma, tab and semicolon are tried, and one qualifies when the first 64 records
   (at least 2) all hold the same number (at least 2) of fields; the most fields wins, then the
   earlier of that order. A pipe is declared, never sniffed (Markdown tables use it). The header is
   `csv_header` only: `first_row` (Known, row 0 is the header and has no record), `none`
   (NotApplicable) or the default `undeclared` (Unknown, row 0 is a record). Every CSV also gets a
   `tabular.csv_dialect` info finding saying how it was read.
3. **JSON.** Rows are a root array's elements or JSON Lines' non-blank lines. A row's cells are its
   leaves in document order, each cited `[ByteRange(row), JsonPointer(path)]`, so the row record is
   not a `Row` citation and each cell carries its own provenance (ADR 0020 §5). Strings are text
   (`""` is `Unknown`, see 1), `true` and `false` booleans, `null` is `KnownAbsent` citing itself (the
   grammar defines it), an empty object or array is an `Unknown` leaf. An integer is an int within
   int64 or uint64; another number is a double only when the double's shortest digits equal the
   literal's value; otherwise it keeps its literal text and `tabular.json_number_text` says so.
   `NaN` and `±Infinity` are the non-finite reals. A repeated key has no reading: one `Unknown` cell
   and `json_duplicate_key`. The header is `NotApplicable` when every row is an object, else
   `Unknown`.
4. **Parquet.** Cells keep the declared types. A DECIMAL is its exact decimal text at the declared
   scale. DATE, TIME, TIMESTAMP and durations are the stored integers: the unit and the UTC flag are
   cells of the schema table, and nothing is converted. A null is `KnownAbsent` citing the footer,
   which declares the column optional. Struct fields are leaf columns named by their path joined with
   `.`. Bytes, INT96, intervals and the items of lists and maps have no cell type: their cells are
   `Unknown` and one `parquet_column_not_decoded` finding per column says so. The footer is evidence
   too, as three tables citing it: `tabular:schema` (one row per leaf column), `tabular:row_groups`
   (every column chunk's counts, min and max read as the column's cells, codec, encodings, sizes and
   offsets) and `tabular:key_value` (the footer's metadata). They are declared locator steps.
5. **Citations.** A CSV or Parquet data row is cited `[Row(r)]` (r counts every record, a header
   included), so cell `c` is `RowCell(r, c, header[c])` by `cell_evidence`. Record ids derive from
   that evidence and the transform (ADR 0017 §5); they are position hints, and no key column is
   guessed: two rows with equal values are two records, and a key is a manifest's decision.
6. **Probing.** Parquet magic is `SIGNATURE`, `VERIFIED` when the whole file is in the head and its
   tail checks. A JSON array of records, or JSON Lines whose head rows all parse as records
   (objects, or arrays), is `VERIFIED`; the first row says what the file is, so records followed by a
   stray scalar or another kind of row are a damaged table at `STRUCTURE`. A JSON object, an array
   of scalars, an empty array, a single-line object, and any binary head are declined (0.0). CSV
   is `STRUCTURE` when a delimiter is sniffed over 3 or more fields, or over 2 fields and the name
   ends `.csv` or `.tsv`; 2 unnamed fields are `NAME_ONLY`, because they could be prose or a log.
   A CSV whose first 64 records disagree is declined: the text adapter reads it, or a manifest
   declares `csv_delimiter`.
7. **Next to the configuration adapter.** Tabular JSON is probed at `VERIFIED` so that
   MVL-23's JSON configuration reading, `STRUCTURE` 0.7, never ties with it. The two partition the
   shapes: the configuration adapter takes a root object (and declines root arrays, GeoJSON and
   table-shaped objects with `config.shape_not_configuration`); this adapter takes arrays and JSON
   Lines of records and declines every root object. A table-shaped object such as
   `{"columns": [...], "rows": [...]}` is read by neither, and falls to the text adapter.
8. **Streaming.** `plan` takes the layout from the source, not only the probe's 64 KiB head: a head
   that cannot tell JSON Lines from one JSON text is extended to what two rows of `max_row_bytes`
   need. It then scans once for row boundaries (quote-aware for CSV, string- and
   nesting-aware for JSON, the footer for Parquet) without decoding, and cuts blocks between rows:
   CSV 8,192 rows or 1 MiB, JSON 4,096 rows or 256 KiB, Parquet a slice of a row group of 65,536
   cells (8,192 rows at most), or the statistics of up to 8,192 column chunks (a row group with no
   rows has no chunk of its own). Block bounds are constants of this adapter version, never settings or
   host facts, so the chunk ids and findings are deterministic. Memory is bounded by a read piece,
   whatever a row's length. Two Parquet costs are bounded in the plan. Every chunk's `ingest` opens
   the footer again (a separate sandboxed call), so a table has at most 100,000 chunks and its
   chunks times the footer's bytes stay under 8 GiB (about 40 s of parsing at the measured
   190 MB/s); past either, `row_limit` says where reading stopped. And pyarrow cannot seek into a
   row group, so each slice decodes its group from the start: the native re-decoding grows with the
   square of a group's slices. Measured on 1M rows x 4 columns (125 slices): 24 s in all, about 1 s
   of it re-decoding, the rest building records. A group is read for at most 2,048 slices (33M rows
   at 4 columns); the rest is a `row_limit` finding naming the first row not read. A schema with no
   leaf columns has no cells to cite: `parquet_footer`, no records.
9. **Hostile input.** Settings, checked before a row is parsed, bound what a row or footer may cost:
   `max_row_bytes` (1 MiB), `max_columns` (16,384), `max_json_depth` (64, measured without
   recursion), `max_rows` (100,000,000, past which `row_limit`), `max_footer_bytes` (16 MiB) and
   `max_column_chunk_bytes` (256 MiB, against the size the footer declares; a footer that
   understates is stopped by the sandbox's memory limit). A refused row has no record and a
   finding; the rest of the table lands. Parquet's declared offsets are checked against the file before pyarrow reads a
   page; pyarrow is the adapter's one library (already a dependency), reads single-threaded through
   the source reader, opens no file and runs inside the sandbox (ADR 0030).
10. **Damage is findings, per block.** A finding about rows is one per code per block, citing the
    bytes from its first to its last affected row, with a count and the first ten rows it names and
    the records it concerns. Codes are `tabular.*` and listed in the descriptor: `csv_dialect`,
    `csv_malformed_quote` (text after a closing quote is kept), `csv_ragged_rows` (a row keeps its
    own length), `csv_unterminated_quote`, `invalid_utf8` (a CSV cell is `Unknown`; a JSON row is
    not decoded), `json_*`, `parquet_*`, `row_limit`, `row_too_large` and `too_many_columns`.

## Alternatives considered

- **A reader per format.** Three adapters would repeat the row, block, limit and finding machinery,
  and the probe would have to arbitrate between them. One adapter with three readers shares it.
- **Infer CSV column types.** Breaks non-negotiable 4 and loses `007` and `1e5`; an inference is a
  derived annotation under `derived/`, not a canonical cell.
- **Flatten JSON into columns by union of keys.** It invents a header and silently pads rows. Leaves
  cited by pointer keep exactly what each row said; a consumer that wants columns pivots by pointer.
- **A new record kind for Parquet statistics or cells.** The existing table records hold them, and
  the schema version is contended; no new kind was needed.
- **Let `pandas` or `csv` read the files.** Their dialect guessing, type coercion and NaN handling
  decide for us; the standard `csv` module also cannot report byte offsets for citation.
- **Sniff a delimiter by majority.** Tolerance would make the same file read differently by a
  different head size. The strict rule is deterministic and explainable; declaring `csv_delimiter`
  is the escape hatch.

## Consequences

- Downstream gets typed values with exact places: `RowCell` for CSV and Parquet, byte range and
  JSON pointer for JSON. Nothing downstream re-parses a raw table.
- Ragged real-world CSVs whose first 64 records disagree are not claimed by sniffing. They fall to
  the text adapter until a manifest declares the delimiter. A tolerant tier would be a new adapter
  version if this proves common.
- JSON rows cite their cells by pointer, so cell index is not a column: rows of different shapes
  have different cells. Consumers align by pointer.
- A parser upgrade or any change to the constants in 8 changes chunk ids and so creates new lineage
  (non-negotiable 6); the golden files under `tests/golden/tabular/` pin the current output.
- JSON Lines whose first two rows do not both fit in the 64 KiB probe head are declined by probing
  (the head cannot tell them from one document) and fall to the text adapter; a manifest that picks
  `tabular` gets them read correctly.
- Type inference over columns (is this text column an integer, a timestamp, an enum) is the other
  half of the issue's "schema/type inference", and it is interpretation, so it lives under
  `derived/`, not here: tracked as MVL-194, which reads this adapter's tables and annotates them.
- Bytes columns and list items are left in the source until a decision on binary cells is taken;
  revisit when a consumer needs them.

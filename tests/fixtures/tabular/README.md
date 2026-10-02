# Tabular fixtures

Real small tables for the `tabular` adapter (`neptune.adapters.tabular`, ADR 0042), one per embodiment
and one per case the adapter checklist in `docs/adapter-contract.md` asks for. `make_tabular_fixtures.py`
writes every file; `tests/unit/adapters/test_tabular_fixtures.py` checks the committed files against it
(text byte for byte; Parquet through the reader, since pyarrow writes its version into the footer).
Validate the Parquet files with the format's official reader and nothing of Neptune's:

    uv run --no-project --with pyarrow python tests/fixtures/tabular/make_tabular_fixtures.py --check

| File | Case | What the adapter must do |
|---|---|---|
| `telemetry_amr.csv` | valid: a mobile base, CRLF, a quoted comma and a doubled quote, a blank | text cells, blank `Unknown`, `n/a` stays text |
| `inspection_quadruped.tsv` | valid: a legged platform's register, tab-delimited, non-ASCII, blank trailing cells | tab sniffed, `Unknown` cells |
| `events_auv.jsonl` | valid: a marine vehicle's JSON Lines, nested objects, null, a blank line, numbers at and past 64 bits | leaves by pointer, `null` KnownAbsent, big number kept as text |
| `joint_states_arm.json` | valid: a manipulator's JSON array of objects, an empty list | leaves by pointer, `[]` `Unknown` |
| `humanoid_joints.parquet` | valid: a humanoid's log, 3 row groups, nulls, decimal, timestamp, date, struct, list, bytes | declared types, footer tables, 2 columns not decoded |
| `ragged.csv` | corrupt: ragged rows, text after a quote, a Latin-1 byte (read with `csv_delimiter`) | rows keep their length; `csv_ragged_rows`, `csv_malformed_quote`, `invalid_utf8` |
| `unclosed_quote.csv` | truncated inside a quoted field | last cell runs to the end; `csv_unterminated_quote` |
| `damaged.jsonl` | corrupt: a syntax error, non-UTF-8 bytes, a repeated key | those rows have no record or an `Unknown` cell; the others land |
| `truncated.json` | truncated inside the third element | two rows; `json_structure` |
| `truncated.parquet` | the Parquet file cut at 60% | `parquet_footer`, no records |
| `bad_tail.parquet` | the closing magic damaged (`PAR2`) | `parquet_footer`, no records |

Hostile sizes (a 200,000-deep array, a 2 MB string, a 20,000-quote run, a row over `max_row_bytes`) are
built in the tests, not committed.

## Workbooks

`make_xlsx_fixtures.py` writes the XLSX and XLSM files (standard library only; the parts are
deterministic and `tests/unit/adapters/test_tabular_xlsx_fixtures.py` compares committed files to
`build()` by their parts, since zlib's deflate bytes may differ between versions). Validate the
well-formed ones with the format's official reader and nothing of Neptune's:

    uv run --no-project --with openpyxl python tests/fixtures/tabular/make_xlsx_fixtures.py --check

| File | Case | What the adapter must do |
|---|---|---|
| `workorders_amr_fleet.xlsx` | valid: a CMMS work-order export for a mobile-robot fleet, shared strings, a rich-text cell, date serials with a custom and a built-in number format, an error cell, a blank, an empty string, a number past 64 bits, a hidden second sheet | cells as declared, `Unknown` told apart by citation, `xlsx_number_text` |
| `changelog_manipulator_cell.xlsx` | valid: a manipulator cell's change log, inline strings only, no styles or shared strings part, rows and cells with no reference, a gap, a merge | implied positions, gap cells `Unknown` (`missing`) |
| `epoch1904_quadruped.xlsx` | valid: a legged robot's inspection log in the 1904 date system | serials unchanged, `date_epoch` 1904 stated |
| `formulas_humanoid_energy.xlsx` | valid: a humanoid's energy budget with cached values, a shared and an array formula, string and error results, a formula with no cached value | cached value marked `formula`, text in the formulas table, `xlsx_formula_no_value` |
| `macro_external_links.xlsm` | valid: macro-enabled, an external link part and relationships | `xlsx_macros`, `xlsx_external_link`, nothing followed or run |
| `truncated_workorders.xlsx` | the work-order export cut at 60% | `xlsx_corrupt`, one table `NotCovered`, no failure |
| `damaged_sheet_xml.xlsx` | a valid zip whose sheet XML is cut inside a row | the rows before the cut land; `xlsx_corrupt` |
| `bomb_zeros_sheet.xlsx` | a sheet padded with 150 MiB of spaces (about 1,000:1) | refused by ratio, before anything is inflated |
| `bomb_part_ratio.xlsx` | 300 KiB of stored noise beside a sheet padded with 25 MiB of spaces: under the zip's ratio, over the part's | the sheet is `NotCovered`, `xlsx_limit` |
| `blowup_shared_strings.xlsx` | 3,000 shared strings (read with a lowered `xlsx_max_shared_strings`) | later strings `NotCovered`, not missing |
| `hostile_entities_sheet.xlsx` | a sheet with a document type declaration and nested entities | `xlsx_part_refused`, nothing expanded |

Hostile shapes that need large or odd bytes (a lying directory, an encrypted flag, bzip2 parts, UTF-16
parts, XML nested 5,000 deep, a 150,000-byte cell) are built in the tests, not committed.

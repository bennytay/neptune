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

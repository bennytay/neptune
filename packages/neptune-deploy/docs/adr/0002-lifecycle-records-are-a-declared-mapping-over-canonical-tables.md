# 0002 — Lifecycle records are a declared, provenanced mapping over the compiler's canonical tables

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-113

## Context

CMMS exports, ticketing exports (Jira, Linear, ServiceNow) and asset or zone registers arrive as CSV,
JSON or spreadsheets. MVL-113 turns their rows into the lifecycle kinds of root ADR 0051. Three facts fix
where that happens:

- The compiler already reads those bytes. Its tabular adapter (root ADR 0042) turns CSV, JSON and Parquet
  into `StructuredTable` and `StructuredRecord` records whose every cell cites its exact place
  (`RowCell`, or a byte range plus a `JsonPointer`). Its PDF adapter (root ADR 0038) does the same for
  document text as `DocumentBlock` spans.
- Deploy adapters are leaves over `neptune.model`, `neptune.identity` and `neptune.adapters.contract`
  (ADR 0001 §2), and no member may import a format adapter or run ingestion (root
  `tests/unit/test_merge_freshness.py`). A Deploy adapter that read CSV would be a second CSV parser
  that disagrees with the first on dialects, blanks, encodings and citations.
- What a column means differs by vendor and by site: `WO Type = PM` is maintenance in one CMMS and a
  project milestone in another. That meaning is a declaration someone makes about an export. It is not
  in the bytes, and code must not guess it.

## Decision

1. **The mapper, not an adapter.** `neptune_deploy.lifecycle` is a deterministic mapper. Its inputs are one
   compiler ingest package, read through `neptune.store.package.read_package` under the pinned package
   schema (ADR 0001 §3), and one or more declared mapping files. Its output is a new package in the same
   format, built by `neptune.store.package.package_files` and written by `write_package`. It never opens a
   source's bytes and never parses CSV, JSON, XLSX or PDF. It reads `StructuredTable` and
   `StructuredRecord` records only. The base package is never changed (non-negotiables 1 and 6).
2. **It is not an ABI adapter.** The four-method ABI maps one source's bytes to records under the sandbox.
   The mapper's input is a whole package of records spanning many sources, and running it in the sandbox
   would mean shipping that package through the adapter boundary. It stays outside the ABI, so it is
   not registered under `neptune.adapters`. The no-op `deploy_lifecycle` adapter of ADR 0001 §6 stays as
   it is, the plugin boundary under conformance test, for formats that do need byte-level reading (a
   commissioning form with its own layout, for example).
3. **Mapping files are declared, versioned JSON.** A mapping file names its schema
   (`neptune-deploy.lifecycle-mapping/1`), its `id` and `version`, and a list of rules. Each rule:
   - says which tables it applies to: every column it `requires` is present, as header text for a
     headed table or as a JSON pointer for a table of JSON objects;
   - may say which rows it applies to with `where` (a column and the verbatim values it may hold). A
     vendor value table such as `PM` → `maintenance_event` is exactly this: it lives in the mapping file
     and is never code;
   - names one lifecycle kind and, per field of that kind, the column or columns it is read from. The
     field shapes come from the compiler's dataclasses. Text fields copy the cell. Id fields take a
     declared namespace. Id lists and statement lists may split a cell on a declared delimiter, and each
     part cites its `Span` inside the cell. Times take a declared format and a declared civil zone.
     Versions take a declared scheme. Composite parts (inventory items, tests, part swaps, decisions,
     zone limits, hazards and their scores) are spelled out field by field;
   - lists the columns it `ignores` on purpose.
   Vendor presets are mapping files shipped in the package (`lifecycle/presets/`), nothing more. A mapping
   file that is malformed, names an unknown kind or field, or has a field shape the kind does not have is
   refused before any record is read. It is the operator's configuration, and it fails loudly.
4. **Values are `stated`, copied as declared, each citing its cell.** A lifecycle record's provenance is
   its row's evidence (one row, one declaration, one record). Each value cites its own cell: the
   `RowCell`, the JSON pointer, or the cell plus a `Span` for a split part. Applying a declared mapping is
   not inference, so the assertion kind is `stated`. A blank cell is `Unknown`, and a cell the compiler
   held as `KnownAbsent` stays `KnownAbsent`. A column the table lacks is `NotCovered`, and so is a field
   the mapping does not map. Nothing is converted, ranked, normalised or defaulted, and no value is
   written into a mapping file to stand for one the source lacks.
   - A typed cell read as text (a text, id or `where` field over JSON or Parquet) is its canonical JSON
     text: `3`, `true`, `1.5`. That rendering is lossless. A non-finite double has no JSON text, so it
     is unreadable.
   - A number is read only when a double holds it exactly, by root ADR 0042 §3's rule: the double's
     shortest digits equal the declared value. `9007199254740993` (2^53 + 1) and `1e-400` do not pass.
     They are `Unknown` with a `value_unreadable` finding citing the cell, never a nearby double.
5. **Times keep their declared civil clock** (root ADR 0023 §2). Text with an offset or `Z` is an
   instant, counted as POSIX ticks (timescale `posix`). Text without one is counted from
   1970-01-01T00:00:00 of its own civil clock, with timescale `Unknown`, and is never moved to UTC. A
   date alone counts days. The zone the mapping declares for the column (a zone name, or `unstated`)
   lives only in the transform's config, as part of the mapping. It is never written into the
   `TimestampDomain`: the domain's `scope` names a part of the source verbatim, and a zone there would
   be a sentinel where the value is unknown. The domain's scope is `()`, its role is `document`, its
   `field` is the column, and it cites the first cell read on it. The model has no field for a civil
   zone. Adding one is a compiler change (MVL-202).
6. **Nothing is dropped silently, and nothing is guessed.** These are findings, coded `deploy_lifecycle_map.*`:
   - a row of a matched table that no rule matches (`row_unmatched`);
   - a row two rules match (`rule_ambiguous`), which gets no record;
   - a column neither mapped nor ignored (`column_unmapped`);
   - a mapped column the table lacks (`column_absent`);
   - a cell that does not read under its declared format (`value_unreadable`), whose field is `Unknown`;
   - a blank cell in a list field (`list_cell_blank`), because a list cannot hold `Unknown`. There is
     one finding per cell, with no cap, naming the record id, the field (a JSON pointer into the
     record) and the cell. Every list the blank emptied is therefore traceable and never reads as "none";
   - an empty part between split delimiters (`list_part_empty`), which is dropped, and an id a record's
     list states twice (`list_id_repeated`), which is kept once. Each is one finding naming the cell;
   - a cell that does not read (`value_unreadable`), which is one finding per cell naming its record
     and field;
   - two records of one mapping stating the same identifier (`identifier_repeated`), which are kept
     apart: identity is MVL-35's;
   - a table no mapping applies to, or one with no column names to map (`table_unmapped`), and a
     table whose header row the compiler was not told of (`header_undeclared`).
7. **Lineage.** Each mapping file is a transform. Its `adapter_id` is `deploy_lifecycle_map` and its
   version is the mapper's. Its config is `{base_package, mapping, mapping_sha256}`, where `mapping` is
   the parsed file and `mapping_sha256` the content id of its bytes. Its `upstream` lists the compiler
   transforms of the tables it read, sorted. One more transform (config: the base package and every
   mapping's hash) owns findings about tables no mapping applies to. The output package holds the base
   package's source ledger, the upstream transform records, these transforms, the lifecycle records,
   their `TimestampDomain`s and the findings. The same base package, mapping files and mapper version
   give a byte-identical package. A changed mapping file or a new mapper version gives new record ids,
   which is new lineage beside the old.
8. **Entry points.** The library calls are `map_files(base, mappings)`, which returns the new package's
   files, and `map_package(base_root, mappings, out)`, which writes them and returns the package id.
   `map_package` refuses an `out` equal to, or inside, the base package. The command line is `python -m neptune_deploy map <package> --mapping <file>... --out <dir>`. A console
   script would add an entry-point group, and ADR 0001 §1 allows only the compiler's two.

## Alternatives considered

- **A Deploy adapter that reads CSV and JSON exports.** It would duplicate root ADR 0042's dialect,
  blank, encoding and citation rules and drift from them. It would also break the member import rule if
  it borrowed the compiler's reader. Lost.
- **The mapper as an ABI adapter over a package's JSON Lines files.** The bytes would be the compiler's
  own output, and citations would point at `records/structured_record.jsonl` rather than the export.
  Lost.
- **Heuristic column matching** (`"completed"` ≈ `"Completion Date"`), or vendor value tables in code.
  Both are inference, and both change output without a declaration that says so. Lost.
- **Copying the base package's tables into the output.** It would duplicate evidence, and every
  lifecycle value already cites the export's bytes. The base package id in the transform config is the
  hop back. Lost.
- **Converting civil times to UTC with the declared zone.** That is normalisation at parse time
  (non-negotiable 4). A UTC reading belongs in a provenanced derivative. Lost.
- **Mapping files in YAML.** YAML would need a dependency and has implicit typing (`NO` → false). JSON is
  in the standard library and canonical. Lost.

## Consequences

- Any table the compiler reads, now or later, can be mapped: XLSX once the compiler reads it, tables
  inside PDFs, Parquet. The mapper only ever sees `StructuredRecord` cells.
- One row is one record. A record spread over several rows (a risk register with one row per hazard and
  one assessment id) becomes one record per row, each with one hazard, until grouping by a declared key
  is decided.
- The model has no `Unknown` list and no civil-zone field. A blank list cell is a per-record finding
  beside an empty list, and the zone is only in the transform config. Both are compiler requests for a
  later schema (MVL-202 for the zone).
- Test fixtures are packages the compiler wrote (`neptune ingest`, by
  `tests/fixtures/lifecycle/make_fixture_packages.py`) and committed. Deploy's tests read them and never
  run ingestion, so an adapter-only compiler change cannot break Deploy's job unseen. Regenerating them
  is a deliberate PR.
- Revisit if a mapping needs values from several tables (a join), rows grouped into one record, or a
  value the export does not state.

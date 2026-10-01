# 0025 — Series files: one sorted Parquet file per stream, merged from runs, written with pinned settings

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-72 (sub-issue of MVL-16)
- Decides: M1 review item O1 (partitioned series files)

## Context

ADR 0018 fixed the series column contract and its order: rows sorted by clock-0 ticks, unknown
last, then `seq`, with the `Stream` line in the file's metadata. ADR 0022 named the file
(`series/<stream id>.parquet`) and left the writer, the row-group size and the writer settings to
MVL-16. ADR 0024 made adapters emit `SeriesBatch`es per chunk, in typed columns, with an empty
batch for a stream that has no samples.

Three forces meet here. A stream can hold billions of rows, so writing it cannot hold it in
memory. Chunks finish in any order, and a source can be cut into chunks in many ways, yet the
package must be byte-identical for the same input. And the M1 review (O1) asked whether very large
streams need several files per stream to be written and resumed in pieces.

## Decision

1. **One file per stream, never partitioned** (O1). A stream's series is `series/<stream id>.parquet`
   whatever its size. Row groups give range access inside it, so a consumer reading `/imu` between
   two times reads only the row groups whose statistics overlap.
2. **Written in two steps, each in bounded memory** (`neptune.store.series`).
   - `write_run` sorts one chunk's batches of one stream into a Parquet *run*, which names its
     stream in its metadata. A chunk is bounded by its adapter, so a run is too.
   - `merge_runs` merges every run of a stream into its series file. It reads at most `FAN_IN` (32)
     runs at once, one row group of each, and emits every buffered row at or before the smallest
     last buffered key: since keys are unique (`seq` is), no unread row can sort earlier. More runs
     merge in rounds through temporary runs. Memory depends on the fan-in, the row-group size and
     the row width, never on the row count.
   - Resume does not need partial series files: the runs are the checkpoint (MVL-73 keeps them per
     chunk), and the merge is a deterministic step that can always run again.
3. **Bytes depend only on the rows.** Row groups hold exactly `ROW_GROUP_ROWS` (65,536) rows,
   counted along the merged order, the last one fewer. Columns are in name order. So the same rows,
   cut into any chunks and merged in any order, give the same file.
4. **Pinned writer settings**, recorded under `series` in the manifest's `store` by whoever
   assembles the package: zstd level 3, dictionary encoding, statistics, the page index, 1 MiB data
   pages, Parquet format 2.6, 65,536-row groups, and the writer, `pyarrow <version>`. A pyarrow
   upgrade may change bytes, so it is output-changing (ADR 0002) and visible in the settings.
5. **Types.** Each `ColumnType` maps to the Arrow type of the same name; a repeated column is a
   list of it. `seq`, locator fields and state columns are non-nullable fields. State columns are
   strings, dictionary-encoded by the writer. Ticks stay plain `int64`, never a timestamp type.
6. **Reading checks every series against its stream**, a batch at a time: the `Stream` line in the
   metadata, the column names and types, strict order on (clock-0 ticks, unknown last, `seq`), the
   null rules and the state vocabulary. The first row of every batch goes through
   `Stream.check_row`, which rebuilds its provenance. `seq` uniqueness across different ticks is
   left to the ingest checks, because proving it needs memory that grows with the rows.
7. **A series file is optional in a package.** A records-only package, like the worked examples,
   holds streams without series. A package the store assembles from an ingest (MVL-73) holds one
   for every stream, empty ones included. A zero-row file and a missing file say different things.
8. **Package files may be paths.** `package_contents` takes series and blobs as bytes or paths and
   hashes, checks and copies paths as streams; `read_package` leaves them on disk.
   `package_files` keeps the in-memory form for small packages.

## Alternatives considered

- **Partitioned series, `series/<stream>/<part>.parquet`** (O1). Chunks could be written
  independently and appended, but parts follow chunking, so the files would depend on how the
  source was cut, and every consumer would merge parts on read. It also changes the package format.
- **Rows in source order, no sort.** No merge step, but ADR 0002 §3 and ADR 0018 §7 ask for sorted
  rows, and range reads on the indexing clock need them.
- **Sort each stream in memory.** Simple, and impossible for a large recording.
- **Row groups by bytes rather than rows.** Better-sized groups for wide rows, but boundaries that
  depend on encoded sizes are hard to keep identical across writers. Wide payloads belong in the
  source, cited by locators, not in value columns.
- **Arrow IPC or Feather for runs.** Faster to write, but a second format; Parquet runs reuse the
  writer and are as deterministic.
- **A row-by-row heap merge.** Simplest k-way merge, but a Python step per row. The frontier merge
  moves whole slices and sorts them vectorised.

## Consequences

- The store depends on `pyarrow` (ADR 0001 §4), pinned through `uv.lock`; mypy treats it as
  untyped, and only `neptune.store` imports it.
- MVL-73 commits each chunk's runs with the chunk and merges them when it assembles a package.
- Reading a package reads every series once, a batch at a time. Packages with very large series
  take as long to verify as to read.
- Revisit if consumers need series partitioned by time for object stores (M9), if the merge
  dominates ingest time, or if row widths make fixed-row groups unworkable.

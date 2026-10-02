# 0013 — Lakehouse layout: packages read in place, a series catalog, and pushed-down window reads

- Status: Accepted
- Date: 2026-10-03
- Issue: MVL-95

## Context

Layers above the Ledger read time series by thread: "the joint states of this arm during shift
A", "the wheel odometry of this base between two ticks". The rows already exist. Every compiler
package holds one sorted, deterministic Parquet file per stream at `series/<64 hex>.parquet`,
sorted by clock-0 ticks (unknown last) and then `seq`, in row groups of 65 536 rows with
statistics and page indexes. The settings it was written with are recorded in the manifest's
`store.series` (root ADRs 0018, 0022, 0025). A thread spans packages (ADR 0003), and packages
may be large, many and remote.

If this is wrong in one direction, the Ledger copies package Parquet into a lake of its own. That
doubles storage and creates a second copy that can drift from the immutable package, and a
rebuild (ADR 0012) would have to rebuild it too. If it is wrong in the other, readers parse
package files themselves, guess which column holds which clock, interleave rows on unrelated
clocks, or scan whole files for a short window.

Two constraints come from earlier ADRs. Clocks are per source, and two clocks are never equated
without a named `ClockMapping` (ADR 0003 §3). Registration accepts only local package roots
(ADR 0007).

## Decision

1. **The lakehouse is a view.** Packages stay where they were registered. The lake reads their
   `series/*.parquet` in place and never copies, caches or rewrites a package byte. The
   Ledger-owned tables that later issues add (MVL-96's Lance media store, MVL-97's indexes) are
   derived from packages and the registry manifest, and are rebuildable like the catalog. They
   never hold a copy of a package's series.
2. **Where things live.**
   - *Packages.* A package's objects are reached through an `ObjectStore` given by
     `locate(package_id, root_locator)`. The default is the local directory it was registered
     from (`LocalObjectStore(root_locator)`). A deployment that mirrors packages to an object
     store passes its own `locate`. The mirror holds the package's files at
     `packages/<package id>/<package path>` under a bucket and prefix, byte for byte, and the
     manifest's sha256 must still equal the package id (§4). Registering from an object store is
     not decided here. It needs registration's whole-package check to read through a store
     (ADR 0007).
   - *Ledger-owned data.* One store per deployment, never a package root, laid out per tenant
     and per package:
     `tenant_<tenant id>/packages/<64 hex package id>/<table>/` for tables derived from one
     package, and `tenant_<tenant id>/tables/<table>/` for tables across a tenant's packages.
     `<table>` matches `[a-z][a-z0-9_]*`. A tenant's data is one prefix, so it can be dropped or
     moved whole, as its schema can (ADR 0002 §2). A package's derivatives are one prefix, so a
     future removal ADR deletes one prefix. Nothing writes under this layout yet; MVL-96 is the
     first writer.
3. **`ObjectStore` is read-only and minimal.** It has four calls:
   - `describe()`: where the store is, with no credentials;
   - `location(key)`: what an engine scans in place: an absolute local path, or
     `s3://bucket/key` with the `S3Settings` that reach it;
   - `size(key)`: an object's size;
   - `read(key, limit)`: a small object's bytes, bounded.

   Keys are package-relative paths: no `..`, `.`, empty part, backslash or NUL.
   `LocalObjectStore` opens every component with `O_NOFOLLOW` (ADR 0006 §3), so a link reads as
   missing. `S3ObjectStore(settings, bucket, prefix)` reads through pyarrow's S3 filesystem,
   which the compiler's pyarrow already ships, so the Ledger adds no S3 client. `S3Settings`
   keeps credentials out of `repr`. A plain-HTTP endpoint must be named with `allow_http`.
   Platform X3 will own this interface. It stays at these four calls until a writer needs more.
4. **`SeriesCatalog` resolves streams to files.** Given `(package id, stream id)` pairs, or
   a `Thread`'s stream entries in every package each entry names, it returns `SeriesFile`s. Each
   holds:
   - the package, its registration key and the stream;
   - the `Location` of `series/<hex>.parquet`;
   - its size and sha256 as the manifest lists them;
   - the manifest's `store.series` settings, checked by the compiler's `check_settings`;
   - the stream's clocks in `time/<i>` order, from the catalog's stored body, or from the
     package's `records/stream.jsonl` line, checked against `body_digest`, when the body holds
     a NUL.

   The order guarantee is the compiler's: `time/0` ascending with unknown last, then `seq`.
   Every lookup is one catalog query plus one manifest read per package. A pair the lake cannot
   serve is a `CatalogFinding` with an existing catalog-api code, never an exception:

   | Code | Case |
   |---|---|
   | `invalid_request` | the pair is not a package id and a record id |
   | `unknown_record` | no such stream is registered in the package |
   | `package_unreadable` | no readable manifest at the store |
   | `manifest_digest_mismatch` | the manifest bytes no longer hash to the package id |
   | `manifest_invalid` | the manifest has no valid `store.series` |
   | `file_missing` | the stream has no series file (a records-only package), or the object is missing or a link |
   | `file_digest_mismatch` | the object's size differs from the manifest, or its footer is not Parquet |
   | `unsafe_entry` | the location contains `* ? [ ] { }`, which both engines read as a glob |
   | `conflicting_id` | one stream id has two different series files |

   A stream id is one record body (ADR 0005 §2). Two packages holding it hold the same series,
   so the lake reads it once, from the first registration, and names the others in `also_in`.
   Two different files under one id contradict the compiler's determinism, so neither is read.
   A read checks size, not hash. Hashing a file to read a window of it would defeat pushdown.
   Full verification stays with `verify` (ADR 0006).
5. **Readers: DuckDB and DataFusion, one plan, one result.** `plan_series(files, windows,
   columns)` builds a `SeriesPlan`, and `DuckDBReader` and `DataFusionReader` run it as one
   SQL statement over the files in place. Both return the same `pyarrow.Table`; the tests
   compare them row for row.
   - **Windows** are `TimeWindow(clock, first, last)`: ticks on one clock, inclusive, as in the
     catalog API. There is at most one window per clock. Each file is scanned on the one window
     clock its stream carries, through the `time/<i>` column the `SeriesFile` names. A file
     carrying none is reported as `unknown_clock` and not read. A file carrying two is a
     `LakeRequestError`, because it would be read twice. Without windows, each file is read
     whole, on its clock 0. Ticks are never converted, and no window is applied across clocks.
   - **Pushdown.** The window is a predicate directly on each file's scan. DuckDB's plan shows
     it as the Parquet scan's `Filters`. DataFusion's shows it as the scan's `predicate` and a
     `pruning_predicate` over the column's min and max statistics, and its
     `row_groups_pruned_statistics` metric counts the row groups never read. Because files are
     sorted on `time/0`, a clock-0 window prunes to the row groups it overlaps. The engines
     still sort the result, so a file replaced after registration cannot corrupt the output
     order.
   - **Result.** The fixed columns are `package_id`, `stream_id` and `clock` (dictionary-encoded
     strings), `ticks` (int64; null only when clock 0 is not known in a whole-file read) and
     `seq`. Then come the requested `value/`, `state/` and `locator/` columns. By default these
     are all such columns that every scanned file has with one type. `time/<i>` of another clock
     is never returned, because `time/1` of two files means two clocks. Row order is ADR 0003
     §3's world order applied to rows: one **partition** per clock, partitions sorted by
     (smallest registration key among their files, clock id bytes), and rows within one by
     `(ticks, package id, stream id, seq)`. That is a total order, so a read is deterministic
     whatever order the files are given in. Two streams of one bag share its log clock and
     interleave by ticks. A run split across two packages, or a stream and its adapter-2.0.0
     sibling, sits on two clocks and is two partitions, one after the other, never interleaved.
     Merging them onto one reference clock through named mappings is left to MVL-97.
   - **Engine access.** DuckDB runs in memory with extension autoinstall and autoload off, so a
     read never fetches code. It reads local files through its own Parquet reader and S3 objects
     as pyarrow datasets over the store's filesystem, which push the filter into the Arrow scan.
     DataFusion reads both through its own Parquet reader, registering an `AmazonS3` object
     store per bucket. One bucket named with two sets of settings in a read is a request error.
     Engines are pinned (`duckdb==1.5.6`, `datafusion==54.0.0`); a bump re-runs the pushdown and
     budget tests.
6. **Budget.** A 10⁶-row window reads in **under 200 ms** locally, end to end: resolving the
   file from the catalog and manifest, planning (one footer read), and the scan into Arrow. The
   `@slow` test uses a mobile base's wheel odometry at 100 Hz (1.2 M rows, 19 row groups) and
   asserts the median of five reads after a warm-up. Measured on a 20-thread workstation:
   DuckDB about 55 ms and DataFusion about 75 ms. Engines return a small scan index per row,
   and the id columns are built as Arrow dictionaries. Returning the ids as strings, which
   materialised 200 MB of text, took 270 ms.
7. **Tests.** Local tests cover:
   - an arm's run recorded as two packages, read as one sorted table;
   - a mobile base's streams sharing a clock;
   - a second-clock window with unknown header stamps;
   - a legged robot's stream thread in history and current views;
   - one stream in two packages, read once, and the conflict case;
   - every finding above, including links, truncation and a moved root;
   - window boundaries;
   - requests outside the contract;
   - determinism across engines and file order;
   - an unchanged package tree after reads.

   The S3 tests mirror the packages into a bucket and repeat the read and pushdown checks
   against `NEPTUNE_LEDGER_S3_ENDPOINT` (MinIO, or any S3-compatible server) or a `minio`
   binary on `PATH`. They skip without one, because CI has neither and MinIO no longer
   publishes downloadable server binaries. The local path is tested in full in CI.

## Alternatives considered

- **Copy package Parquet into a Ledger lake (Iceberg or Delta tables).** Rejected. It duplicates
  immutable evidence, needs its own rebuild and drift checks, and is what the issue forbids. A
  table format's manifests can be added later as a derived index over the files in place, if
  scale demands one.
- **Register package Parquet as views inside PostgreSQL (`parquet_fdw`, `pg_duckdb`).** Rejected.
  It needs server extensions in every deployment, and it couples the catalog's transaction
  store to bulk scans.
- **One engine.** Rejected for now. The issue asks for both. DuckDB is the embedded, fastest
  path here. DataFusion is the Arrow-native engine that Rust services in the platform can share,
  and its native S3 needs no extension download. Running both on one plan keeps the semantics
  in the plan, not in an engine.
- **DuckDB's `httpfs` for S3.** Rejected. It is an extension DuckDB downloads at first use: a
  network fetch of code at read time, which fails offline. Arrow datasets over the store's own
  filesystem push the same filter down.
- **Declare the files' sort order to the engines so they skip sorting.** Rejected. A file
  changed after registration with the same size would then be trusted to be sorted, and the
  output order silently wrong. The sort costs a few milliseconds on a pruned window.
- **Interleave all rows by ticks across clocks.** Rejected. It is the cross-domain comparison
  ADR 0003 §3 forbids. Ticks on two sources' clocks are not comparable without a mapping.
- **Hash each file before reading it.** Rejected. It reads every byte to return a window, and
  `verify` already does this on request.
- **Return ids as plain string columns.** Rejected. That is 200 MB of repeated text per 10⁶ rows
  and over budget (§6). Dictionaries carry the same values.

## Consequences

- No package byte is duplicated. The lake is as current as the catalog and needs nothing
  rebuilt. A moved or changed package is a finding at read time.
- The Ledger now depends on DuckDB and DataFusion (pinned). The `lake` box is partially built.
  Ledger-owned tables and cross-clock merges of rows come with MVL-96 and MVL-97, and no
  catalog-api call exposes series reads yet: `query` (MVL-98) and `access/` (MVL-99) decide
  that surface, so `contracts/` is unchanged.
- The S3 path runs in CI only once an S3-compatible service is available there. Until then it
  is exercised where one is configured.
- Revisit when packages are registered from object stores, when a series read must span clock
  mappings, or when a window over many files needs a file-level time index to skip opening
  footers (MVL-97).

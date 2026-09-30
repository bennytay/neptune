# 0002 — Canonical serialization and ingest-package layout

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-55

## Context

The ingest package is what every downstream consumer reads, so its format is the hardest thing to change later.
The audit ranks it risk #4. The package has to satisfy several constraints at once:

- **Byte-identical determinism.** Same source + adapter version + config ⇒ same bytes. Idempotence, caching,
  golden tests and lineage comparison all depend on this.
- **Heterogeneous typed entities** with tagged unions: `Knowledge[T]` (ADR 0004) and `Locator` (ADR 0006). There
  are a few thousand such records per run.
- **High-volume time-series.** There can be millions to billions of samples per run, and consumers must be able
  to read `/imu` between two timestamps without scanning the rest.
- **Raw evidence** must stay byte-identical and addressable, sometimes hundreds of GB of it.
- **Inspectability.** An engineer or agent should be able to `grep`, `diff` and `jq` the output.

No single format meets all of these, so the package uses three.

## Decision

An ingest package is a **directory** containing three kinds of storage plus a manifest.

1. **Entity records are JSON Lines of canonical JSON.**
   - One table per entity kind, for example `records/stream.jsonl`.
   - One record per line. Each line is canonical JSON with no trailing whitespace, terminated by `\n`.
   - Lines are sorted by record id. An empty table is an empty file, which is different from a missing file.
2. **Canonical JSON** is defined once, in `identity/` (MVL-2), and used for both hashing and storage:
   - UTF-8 output, object keys sorted by Unicode code point, no insignificant whitespace;
   - strings are emitted as given. There is no Unicode normalisation (NFC or otherwise). Escaping is only what
     JSON requires, using the shortest form;
   - integers are emitted as exact decimal integers of any magnitude. They are never routed through
     floating point. Tick counts above 2^53 must round-trip;
   - floats are emitted in the shortest representation that round-trips to the same IEEE-754 double. `-0.0` is
     preserved. NaN and ±Infinity are **forbidden** in canonical JSON. A source that contains them is represented
     through `Knowledge` or a typed special value defined in MVL-1, never as a bare token;
   - `null` never appears as a value. Missingness is `Knowledge` (ADR 0004).
   - Text that is not valid Unicode, such as malformed UTF-8 or lone surrogates, is never lossily decoded into a
     canonical string. The record references the raw bytes through an `EvidenceRef`, and the adapter emits an
     `IngestFinding`.
3. **Time-series samples are Parquet.**
   - Anything per-sample (message fields, telemetry rows, per-frame metadata) goes to Parquet under `series/`.
     The entity that describes the series, such as `Stream`, is a JSON Lines record.
   - Rows are sorted by `(domain id, ticks, source order)` (ADR 0005).
   - Row groups are sized for time-range queries. The target size is set in the store (MVL-16) and recorded in
     the package manifest.
   - Writer settings are fixed and recorded: compression codec, dictionary and statistics settings, and no
     wall-clock or host metadata. Library versions are part of the fixed toolchain (ADR 0001).
   - Epistemic state and per-row provenance are stored as columns, not as per-row JSON (ADRs 0004 and 0006).
4. **Raw evidence is content-addressed blobs.**
   - Every source is identified by its tier-1 content id (ADR 0003).
   - Its bytes are either **materialised** into the package (`blobs/sha256/<2 hex>/<64 hex>`, byte-identical to
     the source) or **referenced**: a location and the content id, verified at read time.
   - Both modes are valid package states. Which one is the default, and when copying is forced (portable
     export), is decided in MVL-5 / MVL-16.
   - Neptune never re-encodes, normalises or truncates source bytes (non-negotiable 1).
5. **The manifest** lists every file in the package with its size and sha256, sorted by path, plus
   `SCHEMA_VERSION` and the store settings above.
   - The package's identity is the hash of the manifest's canonical JSON.
   - The receipt is split into a deterministic core, which is included, and a volatile envelope, which is
     excluded. That split is MVL-5.
6. **No Protobuf, FlatBuffers or other IDL-generated wire format in v0.**

The exact directory names, manifest fields and receipt layout are MVL-5's to fix within these rules. The
compatibility and migration policy for `SCHEMA_VERSION` is MVL-1.

## Alternatives considered

- **Protobuf (or FlatBuffers / Cap'n Proto) for entities.** Strong schema evolution and compact encoding.
  Protobuf serialisation is explicitly not canonical: map ordering and unknown fields make bytes
  implementation-dependent, so hashing needs a second canonical form anyway. The binary output is not diffable
  or greppable, and an IDL + codegen step would be added before the model has stabilised. It can be added later
  as an export format.
- **A single SQLite or DuckDB file.** Convenient to query. Page layout, free lists and write order make the bytes
  non-deterministic, and one file is a poor fit for 100 GB of series plus raw blobs. Good as a derived index
  built from the package, which is out of scope here.
- **Parquet or Arrow IPC for entities too.** One format everywhere. Deeply nested tagged unions
  (`Knowledge[Locator]` inside records) map awkwardly to columnar schemas, schema evolution per entity kind is
  heavier, and entity tables are small, so columnar buys nothing.
- **HDF5 or Zarr for time-series.** HDF5 files are hard to make byte-deterministic and have a weak story for
  concurrent readers and object stores. Zarr is attractive for dense arrays but weaker for heterogeneous tabular
  samples and predicate pushdown. Parquet has the widest consumer ecosystem (Arrow, DuckDB, Polars, Spark).
- **MCAP as the output container.** MCAP is an input format with message-centric semantics. Reusing it for
  canonical output would blur evidence with its interpretation and would not serve non-log series.
- **RFC 8785 (JCS) as the canonical JSON spec unchanged.** It is close to what we want, but it serialises every
  number as an IEEE double. Nanosecond tick counts exceed 2^53 and would silently lose precision. We follow JCS
  for string escaping and depart from it in two places: integers are exact, and keys sort by code point rather
  than by UTF-16 code unit. The two orderings differ only for keys with characters outside the Basic
  Multilingual Plane.

## Consequences

- Entity output is diffable and greppable, and golden tests compare it byte-for-byte.
- Consumers must parse JSON integers at full precision. JavaScript `JSON.parse` does not. Python and Arrow
  readers do. This is accepted: correctness of tick counts beats universal parser compatibility.
- One canonical-JSON implementation serves hashing and storage, so an id always equals the hash of the bytes
  on disk.
- Parquet byte-determinism depends on pinned writer settings and library versions. A `pyarrow` upgrade may
  change bytes and is treated as output-changing (ADR 0003).
- Referenced (non-materialised) blobs keep 100 GB sources out of the package, at the cost of portability. The
  package is only self-contained once materialised.
- Revisit if entity volume grows until JSON Lines parse time dominates reads, if a consumer needs a
  schema-first wire format, or if Parquet cannot meet the time-range query requirement on real corpora.

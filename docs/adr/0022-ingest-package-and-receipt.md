# 0022 — The ingest package and its receipt

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-5

## Context

ADR 0002 fixed the package as a directory of JSON Lines tables, Parquet series and
content-addressed blobs, with a manifest whose hash is the package's identity. It left MVL-5 the
directory names, the manifest's fields, the receipt's layout, and whether sources are copied into
the package by default.

MVL-5's acceptance is that a developer can answer exactly what Neptune did without reading
internal logs, and that the receipt is deterministic for the same input, parser versions and
config. The audit split the receipt into a deterministic core and a volatile envelope (wall clock,
host, durations, job id), with the core carrying its own hash.

A receipt a job writes from its own in-memory state can say anything, and nobody can check it
later. A package's records already hold the ledger, the transforms, what came out and every
finding. So they can be the receipt's only input.

## Decision

1. **Layout.**

   | Path | Holds |
   |---|---|
   | `manifest.json` | `PackageManifest`; the sha256 of these bytes is the package id |
   | `receipt.json` | `IngestReceipt`, the receipt's deterministic core |
   | `receipt.md` | the core rendered for people |
   | `records/<kind>.jsonl` | one table per record kind, lines sorted by id |
   | `series/<64 hex>.parquet` | one per stream, named by the stream's id |
   | `blobs/sha256/<2 hex>/<64 hex>` | a materialised source's bytes |
   | `volatile/receipt-envelope.json` | `ReceiptEnvelope`, not listed in the manifest |

   Every record kind of the schema version has a table, so an empty table is an empty file and a
   missing table is a broken package. Nothing else may be in a package, and symlinks are refused.
2. **Manifest.** `PackageManifest(receipt, tables, sources, files, store)`, kind
   `package_manifest`, with the records' envelope:
   - `receipt` is the core's id;
   - `tables` counts the records of every kind;
   - `sources` holds a handle per source artifact: content id, size and storage (`referenced` or
     `materialised`). Its locations are the `source_revision` records;
   - `files` lists every file but the manifest and `volatile/`, with size and sha256, sorted by
     path;
   - `store` holds the settings the store wrote series and blobs with (MVL-16), as it records them.
3. **The receipt core is computed from the package's own records.** `build_receipt` takes the
   ledger, the transforms, the evidence records and the findings, and nothing else. Its content:
   - `sources`: every location seen holding bytes, with the transforms whose records cite them. A
     source with none was seen and hashed but not decoded. `absent` lists locations seen to hold
     none.
   - `transforms`: adapter id, version, config hash, libraries and upstream. These are the
     selected adapters and parser versions, and what a replay must match.
   - `records`: counts per kind. `clocks`: every clock by field and scope.
   - `runs`, `streams`: the declared time coverage, as the records state it, states and all.
   - `entities`: machines, sites and assets with their stated ids.
   - `findings`: most severe first. Warnings, ambiguities, unsupported and dropped data are their
     severities and categories (ADR 0017 §9).
   - `ambiguous`: every field whose state is `Ambiguous`, as record id plus JSON pointer.

   Its `id` is `record_id("ingest_receipt", …)` over everything else. Reading a package rebuilds the
   receipt from the tables and refuses one that differs, so a receipt can only say what its package
   holds. `receipt.md` renders the core: no wall clock, no host, and every time stays ticks on its
   named clock. Bindings of runs to configurations join the receipt with MVL-38, through a schema
   bump.
4. **The volatile envelope** (`ReceiptEnvelope`): the receipt id it accompanies, the job id,
   wall-clock start and finish (RFC 3339, UTC), the host, the ingest root as the host names it,
   and durations per phase. It sits under `volatile/`, outside the manifest, so two runs of the
   same job give byte-identical packages with the same id and differ only there.
5. **Sources are referenced by default.** Hundreds of gigabytes stay where they were found: a
   referenced source is its content id plus the locations its revisions record, relative to the
   ingest root the envelope names. It is verified by hashing when read. Materialising (copying
   into `blobs/`) is per source and opt-in; a portable export materialises every source.
6. **Writing and reading.** `package_files(records, series, blobs, store)` computes every
   deterministic file, so the same inputs, in any order, give the same bytes and package id.
   `read_package` verifies everything:
   - the schema version, and every file's size and hash against the manifest, with no stray or
     missing file;
   - canonical, sorted, unique table lines whose counts match;
   - every transform, finding and evidence record id;
   - the blobs against their names, and the receipt and its rendering, which must recompute.

   A package read back and written again is byte-identical. Neither function writes the envelope
   or Parquet: the runtime (MVL-6) and the store's series writer (MVL-16) do.

## Alternatives considered

- **A receipt assembled by the runtime from its own state.** Richer (probe scores, chunk timings),
  but unverifiable, and it could disagree with the package it describes. Probe explanations and
  timings belong in the envelope or in records.
- **A missing table for a kind with no records.** Smaller, but then a missing table means either
  "none" or "lost", which is the missingness the model exists to keep apart.
- **Materialised by default.** Self-contained packages, at the cost of copying every source.
  Neptune's inputs can be hundreds of gigabytes, and ADR 0002 already allows both states.
- **The envelope inside `receipt.json`.** One file, but then the package id would change with the
  wall clock, which breaks determinism and caching.
- **Series files named by topic.** Readable, but topics repeat across runs and files, and may
  contain any character. The stream's id is unique and path-safe.
- **HTML or a terminal table for people.** Markdown reads well raw, renders on GitHub, and diffs.

## Consequences

- Any package can be checked with no job state: files, lineage and receipt all recompute.
- The four worked examples are packaged in tests, and their manifests and receipts are golden
  files (`tests/golden/packages/`).
- A referenced package is not self-contained. Moving it without its sources means only its
  records and receipt are readable until the sources are found again by content id.
- MVL-6 fills the envelope, MVL-16 writes series and records their settings in `store`, and MVL-11
  prints `receipt.md`.
- Revisit if receipts need facts the records do not hold, which would mean a record kind is
  missing, or if packages outgrow a single directory.

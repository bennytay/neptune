# 0065 — Streaming package write: bounded spill, byte-identical output

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-48 (part 1: the streaming package write)
- Extends: ADR 0022 (the package and its receipt), ADR 0026 (assembly), ADR 0029 §4 (scratch space)

## Context

Deploy's D1 gate measured the compiler's package write on a CMMS export of 100,000 work orders:
97 s and a 2.9 GB peak. `package_contents` held every record, every table's bytes and the receipt,
then read the whole package back (`read_files`) to verify it. Memory grew about 29 KB per record,
so a multi-million-row export could not be written at all. The package format is fixed (ADR 0022):
each table is sorted by id, the receipt lists every finding and ambiguous field in a global order,
and the manifest hashes every file. A new writer must give exactly the same bytes, or every package
id changes.

## Decision

1. **One writer, two sinks.** `PackageWriter` (`neptune.store.writer`) takes records one at a time
   and writes the package as a stream. `package_contents` and `package_files` are the same writer
   with no scratch directory: nothing spills, and every file is bytes. `write_package_stream(root,
   records, scratch=...)` writes into a directory and is the bounded path. The list API stays; a
   caller with a lazy iterable passes it to either.
2. **External merge sort.** Each record is encoded to its canonical line as it arrives and given to
   a `Sorter` per table (`neptune.store.spill`). All sorters of one write share a `SpillBudget`
   (32 MiB of held entries by default). When the budget is spent, the sorter holding the most
   sorts its batch and writes it as a run. Reading merges the runs with `heapq.merge`, which is
   stable, at most 64 at a time; more runs are first merged into fewer, in order. Equal keys come
   back in the order they were added, so "last record of an id wins" (what `stage` always did) is
   exact.
3. **Spill lives in the caller's scratch, never the system temp directory.** The writer makes a
   private directory in the directory it is given and removes it on success and on failure. The
   job spills into a `scratch_space` of the workspace's scratch root (ADR 0029 §4), so a killed
   job's runs are swept by the next job. `stage` without a spill directory uses the staging
   directory's own `.scratch`. A run is `>II` (key length, payload length), the key as canonical
   JSON and the payload as given, named by sorter and counter: the same records spill to the same
   bytes.
4. **Verification moves inline.** Each table is read back once, merged in id order. Each record is
   parsed from its line and must encode back to it (the reader's round trip), its lineage is
   checked (transforms are read first), series and blobs are checked as the reader checks them,
   and only then is the line appended to the table's file and hashed. The whole-package
   `read_files` after writing is gone: what it checked is checked as the bytes are made.
5. **The receipt is streamed too.** Sections that grow with the records (runs, streams, entities,
   findings, ambiguous fields) go through sorters. The receipt id is hashed, and `receipt.json` and
   `receipt.md` are written, by encoding canonical JSON piece by piece around those sections.
   `render_receipt` became `render_lines`, which any receipt-shaped object with re-readable,
   countable sections can drive. `build_receipt` stays as the reader's reference.
6. **The bound.** Peak memory of a write is the interpreter and modules (about 70 MiB) + the spill
   budget with Python's per-entry overhead (about 2 × 32 MiB) + one 64 KiB read buffer per merged
   run (at most 64) + a 1 MiB write buffer per open file + one record + the manifest + the
   receipt's per-source sections: sources, absences, transforms, clocks, the id of each stream and
   a stream count per run (`stage` also holds each stream's record and its runs' paths). None of
   these grows with the number of evidence records or findings; they grow with sources, streams
   and runs, which are few next to them. The tests cap peak RSS at **256 MiB** at
   20,000, 100,000 and 1,000,000 rows. `stage` adds one committed chunk's records at a time.

## Alternatives considered

- **Keep the in-memory writer and add a separate streaming one.** Two writers of one format would
  drift; one writer with two sinks cannot.
- **SQLite or a key-value store as the sort.** A dependency and a second format for scratch, where
  a merge sort of length-prefixed runs is a hundred lines.
- **`pickle` or `marshal` for runs.** Their bytes depend on object identity and reference counts,
  so the same entries need not spill to the same bytes.
- **Spill to `tempfile.gettempdir()`.** Often a RAM-backed `tmpfs`, which defeats the bound, and
  outside the workspace's sweep.

## Consequences

- Measured on `tests/fixtures/store/make_scale_package.py` (rows with findings and ambiguous
  cells): at 100,000 rows the previous writer took 31 s and peaked at 771 MiB; the streaming writer
  takes 25 s and peaks at 127 MiB, with the same package id. At 1,000,000 rows it stays under the
  same cap, in linear time. `tests/integration/test_package_write_scale.py` holds both (`@slow`).
- Deploy's D1 case (100,000 work orders, 470 MiB of tables, 69,000 findings): the write took
  96.7 s and peaked at 2.9 GB. `package_files` now takes 62 s and peaks at 2.0 GB, because it still
  holds the whole package. `write_package_stream` takes 68 s and its process peaks at 746 MiB, most
  of which is the mapper's input and output, held as lists.
- Byte identity is proved against the worked examples' golden documents, Deploy's committed
  archetype packages (series and derived tables included) and the in-memory writer, with a budget
  small enough to force multi-level merges (`tests/unit/store/test_streaming_writer.py`).
- A write needs scratch disk about the size of its record tables; each table's runs are removed
  once it is written.
- Not in this part of MVL-48: `read_package` and validation still hold the whole package, so a job
  (stage, then validate) is bounded only up to its validate phase; `amend` and `export` still use
  the in-memory path. Queues, backpressure, storage tiers and object-store execution are the rest
  of MVL-48.

# 0070 — Bounded, crash-safe package I/O: atomic writes and a streaming reader

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-48 (part 2: bounded package I/O)
- Extends: ADR 0065 (streaming package write), ADR 0022 (the package), ADR 0054 (validation)

## Context

ADR 0065 bounded the package write. The review of #94 found three gaps, and ADR 0065 named a
fourth:

- `write_package_stream` and `write_package` wrote straight into the target. A process killed
  while series were copied left a target with `manifest.json` but missing series: a directory
  that looks like a package and is not one.
- A derived table's spilled runs stayed on scratch disk until the writer closed.
- ADR 0065 quotes 746 MiB for Deploy's D1 write (100,000 CMMS work orders), but no committed
  script reproduced it.
- `read_package` read every table into memory, parsed every record into a tuple, parsed the
  receipt, and rebuilt the receipt from all the records. Validation then indexed every record by
  kind and by id. So a job (stage, then validate) was bounded only until its validate phase, and
  `amend` and `export` rebuilt the package in memory.

## Decision

1. **A write lands whole or not at all.** `write_package` and `write_package_stream` write into
   `.<name>.partial`, a sibling of the target in the same parent, and rename it onto the target in
   one step (`neptune.store.package.replacing`). The target must not exist or be an empty
   directory, as before; a symlink is refused. A killed write leaves the target as it was.
   It is atomic, not durable: nothing is flushed. `publish` (ADR 0026) stays the durable path.
2. **A stale partial is reclaimed by the next write of the same target.** The partial's name is a
   function of the target only, so a killed write leaves at most one per target. Each write holds
   an exclusive `flock` on the partial directory. A write that finds it unlocked (its writer died)
   empties it and reuses it. Links inside it are removed, not followed. A file or link at that
   name is removed. A write that finds it locked is refused: another write of that target is in
   progress. The writer checks that the locked directory is still the one at the partial's path,
   so it never empties a package that was renamed into place.
3. **Spilled runs go as soon as they are merged.** A derived table's sorter is closed once the
   table is written, and the receipt's sorters once the receipt is written. After `finish`, the
   writer's spill directory is empty. Record tables were already released (ADR 0065).
4. **The reader is the writer run backwards, as a stream** (`neptune.store.reader.verify`). It
   hashes every file against the manifest first. Then it reads each table once, line by line, in
   the writer's table order, hashing again as it goes, so the bytes it checks are the bytes it
   hashed even if a file changes between the two passes. It checks each record with the writer's own code (`_Gathered`):
   canonical line, id order, lineage, series. It recomputes the receipt as the writer does, with
   the large sections sorted in a `SpillSpace` under the caller's `scratch`. It compares the
   result with `receipt.json` and `receipt.md` by hash, so neither file is parsed. Only when the
   hashes differ is a `receipt.json` of up to 64 MiB parsed, to say why (other schema version,
   id that does not recompute, another package's receipt). A larger one is refused without being
   read into memory. Everything the reader refused before, it still refuses.
5. **A read package holds paths, not records.** `IngestPackage.records` is a `PackageRecords`. It
   reads its tables each time it is iterated, and `of(kind)` reads one table. Each derived table is
   a `StoredTable`. `receipt` is parsed from `receipt.json` the first time it is asked for. Each
   pass hashes what it reads, so a file changed since verification is refused at the end of that
   pass, and one gone or unreadable as the pass meets it. A pass raises only `PackageError`.
   A line already verified is parsed again with plain `json.loads`: the hash covers it. `records` may still be any collection: a caller that builds a package in memory, or
   `dataclasses.replace`, may pass a tuple. Once `amend` moves a staged package's files, the old
   `IngestPackage` cannot read them. The job reads counts from the manifest instead.
6. **Validation reads, never indexes.** `Context.records(kind)` reads the kind's table and
   `Context.findings` filters the finding table, both lazily. `dangling_reference` used to look
   each target up in an index of every record. It is now a join: every record id and every
   reference go through one sorter keyed by the id named, spilling to `validate_package(...,
   spill=)`. A second pass, only when something dangles, collects the records that name it.
   Findings are byte-identical. A rule still holds what it groups (streams, runs, clocks,
   configurations, frames, entities) and what it reports. A `PackageError` raised while a rule
   reads (the package's files changed or vanished) fails the validation; it is not reported as
   that rule's failure.
7. **The job is bounded through validate.** The validate phase reads, validates and amends in a
   workspace `scratch_space`. `amend` and `export` write through `PackageWriter` with spill and
   move its files into place, like `stage`.

## Alternatives considered

- **A random partial name, swept by age.** An age is a guess. A slow writer is not a dead one,
  and the next write of a target cannot tell which partial belongs to that target. A lock says
  whether the writer is alive.
- **`fsync` before the rename.** This would give durability as well as atomicity. Every test and
  tool write would then pay for flushing its tree. The job already gets durability from `publish`.
  The review asked for kill-safety.
- **Keep parsing `receipt.json` and comparing objects.** The receipt lists every finding and every
  ambiguous field, so it grows with the records. Comparing hashes of the recomputed bytes checks
  the same thing and holds nothing.
- **An on-disk id index (SQLite, an mmap'd sorted file) for reference lookups.** That is a second
  format and a dependency. The one rule that needs every id can use a sort-merge join with the
  sorter we already have.
- **Keep `records` a tuple and add a separate streaming API.** Every consumer (validation, SDK,
  Deploy, Ledger) would still use the tuple. Making the one field lazy bounds them all, and only
  callers that need a sequence (`reversed`, indexing, identity) change.

## Consequences

- Measured with `tests/fixtures/store/make_scale_package.py`, one process per case, at 100,000
  rows. Main's `read_package` plus validation peaks at OLD_READ MiB and takes OLD_READ_S s. The
  streaming read peaks at NEW_READ MiB and takes NEW_READ_S s, with the same package id and the
  same findings. At 1,000,000 rows it stays under the same 256 MiB cap
  (`tests/integration/test_package_write_scale.py`, `@slow`).
- `scripts/bench_d1_package_write.py` reproduces Deploy's D1 case: 100,000 work orders mapped by
  the `cmms_generic` preset. On this branch: `package_files` + `write_package` D1_MEMORY;
  `write_package_stream` D1_STREAM (ADR 0065 quoted 746 MiB; the reviewer measured 756 MiB); the
  streaming read and validation D1_READ. The stream case's peak is mostly the mapper's input and
  output, which it holds as lists. That is Deploy's to stream.
- A `PackageRecords` pass costs a parse of every line. A consumer that iterates the records many
  times should hold what it needs, or read one kind with `of(kind)`.
- What stays bounded only per item: one line of a table is held whole as it is parsed (ADR
  0065's bound already holds one record), and a rule holds what it reports. A package where a
  million records name one missing run holds those records in `dangling_reference`'s draft.
  These grow with the size of one record and the number of anomalies, never with a well-formed
  package.
- A write now needs write permission on the target's parent, where its partial goes. An empty
  target directory made beforehand is replaced by a new directory, not filled.
- Laziness moves errors. A package whose files are moved or changed after `read_package` raises
  `PackageError` when it is next iterated, not at read. The SDK's `read_package` still wraps
  read-time errors in `PackageInvalidError`.
- `read_package` and `validate_package` without a scratch directory hold the receipt's sorted
  sections and the reference join in memory. That grows with findings and references, not with
  the records' bodies. Pass one for a bounded read.
- Still not in MVL-48 part 2: queues, backpressure, per-adapter resource profiles, storage tiers,
  object-store execution, prioritisation and cost metrics (part 3).

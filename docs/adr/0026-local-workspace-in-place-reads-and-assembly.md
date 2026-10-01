# 0026 — The local workspace, reading sources in place, and assembling packages

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-73 (sub-issue of MVL-16)

## Context

MVL-16 asks that a multi-GB run be ingested on the developer's machine without duplicating its raw
bytes and without network access. It moved to M2 because its local metadata store is what the
runtime's checkpoints (MVL-6) and the cache (MVL-9) are built on. ADR 0022 made sources referenced
by default and verified when read; ADR 0025 made series files from per-chunk runs. What remained:
where ingest state lives between runs, how adapters read sources without copying them, how a
package is built from committed work, how it becomes portable, and what "local-only" means.

## Decision

1. **A workspace directory holds all ingest state** (`neptune.store.workspace`), never the evidence
   folder: `$NEPTUNE_HOME`, else `$XDG_CACHE_HOME/neptune`, else `~/.cache/neptune`, or an explicit
   path. Its layout is versioned by `workspace.json` (format 1); another format is refused.
   - `ledgers/<sha256 of the root's absolute path>/`: each ingest root's source ledger, so revisions
     and absences continue across runs (ADR 0010).
   - `plans/`: each source's plan under each transform, with the transform record.
   - `chunks/<chunk id>/`: each committed chunk's records, findings and per-stream runs.
   - `staging/`: work in progress, and nothing else.
2. **Commits are atomic and idempotent.** A chunk is written into `staging/`, flushed, and renamed
   into `chunks/` in one step. A process killed before the rename leaves only staging debris, which
   `clear_staging` removes; one killed after it leaves a whole chunk. Outputs are deterministic, so
   a chunk committed again, by a rerun or a second process, is the same: the second commit is a
   no-op. Resume is "skip committed chunk ids", exactly as ADR 0008 §5 planned.
3. **Sources are read in place** (`neptune.discovery.reader.LocalReader`). It opens the file through
   `LocalSource` (its symlink and special-file policy apply), refuses a file whose size changed,
   and serves bytes only from artifact chunks (8 MiB) it has just read whole and hashed against
   `SourceArtifact.chunks`, keeping the last four. A changed source raises `SourceChangedError`
   instead of handing an adapter bytes its citations would not name. Nothing is copied to disk.
4. **Packages are assembled from the workspace** (`neptune.store.assemble.assemble`): the ledger,
   each ingested (source, transform) pair's plan, every chunk's records and findings, and each
   stream's runs merged into its series file. Every chunk of each plan must be committed, and every
   stream must have runs. The package is built in a hidden sibling directory and renamed into
   place, so it appears whole or not at all; an existing destination is refused, as packages are
   written once. The manifest records `store.series` whenever there are series.
5. **Portable export** (`export`) copies a package with every referenced source materialised into
   `blobs/`, read from the locations its revisions record under a given root and verified as they
   land. The records and the receipt are the original's; the manifest, and so the package id,
   differ because the package now holds the bytes. A source not found is an error, not a gap.
6. **Local-only by default.** A new workspace is local-only. Anything that would use the network
   (object-store connectors, uploads, remote models) calls `require_network(purpose)` first and is
   refused with `LocalOnlyError` until the workspace explicitly allows it; the choice is remembered.
   Today nothing in Neptune touches the network, and a test runs a whole ingest with sockets
   disabled.

## Alternatives considered

- **SQLite (or DuckDB) as the metadata store.** Indexes and transactions for free, but a binary,
  non-deterministic file; ADR 0002 already rejected it for packages. Atomic directory renames give
  the transactions this needs, and every file stays canonical JSON or Parquet, inspectable and
  content-addressed by chunk id.
- **The workspace inside the ingest root** (`<root>/.neptune/`). Easy to find, but it writes into the
  evidence folder, which may be read-only or shared, and the next scan would walk it.
- **Memory-mapping sources.** Fewer copies in memory, but a file truncated by another process
  kills the process with SIGBUS, and verification still needs every byte hashed.
- **Verifying a source once, when the ingest starts.** Cheaper, but the file can change between
  that check and the read. Checking each chunk as it is served closes the gap.
- **Copying sources into the workspace (or the package) by default.** Self-contained, at the cost
  of duplicating hundreds of gigabytes; materialising stays opt-in (ADR 0022 §5).
- **A per-job network flag.** Easy to forget on one call; a remembered workspace setting that every
  network user must consult is harder to bypass by accident.

## Consequences

- MVL-6 drives scan, select, plan, ingest and commit through these pieces and calls `assemble` as its
  commit phase; MVL-9 reuses committed chunks across jobs and roots by chunk id.
- Reading in place costs a hash of every chunk an adapter touches, roughly one more pass over the
  bytes it reads; the 2 GiB acceptance run takes seconds.
- A workspace grows with every chunk ever committed. Garbage collection is MVL-9's.
- Revisit if many processes contend on one workspace (M9 queues), if hashing reads dominates
  ingest time, or if a platform's cache directory convention should replace XDG's.

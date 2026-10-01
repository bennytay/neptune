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
   - `ledgers/<sha256 of the root's resolved path>/`: each ingest root's source ledger, so revisions
     and absences continue across runs (ADR 0010). The root is resolved, so a relative path, `..`
     or a symlink to the same directory names one ledger.
   - `plans/`: each source's plan under each transform, with the transform record. Planning is
     deterministic, so saving the same plan again is a no-op and a different one is refused.
   - `chunks/<chunk id>/`: each committed chunk's records, findings and per-stream runs. Anything
     else found there is a `WorkspaceError` naming it, never a crash or a made-up id.
   - `staging/`: work in progress, and nothing else.
2. **Commits are atomic, durable and idempotent.** A chunk is written into its own directory in
   `staging/`, every file and directory flushed (`fsync`), and renamed into `chunks/` in one step.
   Every directory a commit, plan or ledger lands in is flushed after it is made (its name in its
   parent, even if another process made it) and after the rename (the new name in it).
   A process killed before the rename leaves only staging debris; one killed after it leaves a
   whole chunk. The writer holds an `flock` on its staging directory from creation until it is
   renamed or removed, and `clear_staging` removes only entries whose lock it can take, so it
   clears what dead processes left and never another process's commit in flight. Outputs are
   deterministic, so a chunk committed again, by a rerun or a second process, is the same: the
   second commit is a no-op. Resume is "skip committed chunk ids", exactly as ADR 0008 §5 planned.
3. **Sources are read in place** (`neptune.discovery.reader.LocalReader`). It opens the file through
   `LocalSource` (its symlink and special-file policy apply), refuses a file whose size changed,
   and serves bytes only from artifact chunks (8 MiB) it has just read whole and hashed against
   `SourceArtifact.chunks`, keeping the last four. A changed source raises `SourceChangedError`
   instead of handing an adapter bytes its citations would not name. Nothing is copied to disk.
4. **Packages are assembled from the workspace** (`neptune.store.assemble.assemble`): the ledger,
   each ingested (source, transform) pair's plan, every chunk's records and findings, and each
   stream's runs merged into its series file. Every ingested source must be in the ledger, so a
   package never cites a source it does not list; every chunk of each plan must be committed, and
   every stream must have runs. The package is built in a hidden sibling directory (made with
   `mkdir`, so the umask applies and is never read, which would mean setting it), flushed, and
   renamed into place, so it appears whole or not at all and stays once it has appeared; an
   existing destination is refused, as packages are written once. A materialised source is
   copied, so it is hashed again where it lands, against the manifest; nothing else is re-read.
   The manifest records `store.series` whenever there are series.
5. **Portable export** (`export`) copies a package with every referenced source materialised into
   `blobs/`. Each source is read from the head of a location chain that holds it, opened through
   the `Source` the caller gives (the store never imports discovery), so the walk's policy applies:
   a symlink where the file was is refused, not followed. The bytes are streamed into the export
   and hashed where they land, so what is checked is what ships; of the rest, only the files
   copied from the original package are hashed again. The records and the receipt are the
   original's; the manifest, and so the package id, differ because the package now holds the
   bytes. A source the package's records cite that has no local location left, cannot be opened,
   or changed since it was hashed is an error, not a gap. A source no record cites whose last
   known state is absence stays referenced: nothing in the package was read from it, and there
   is nothing to copy. The export is written like a package: staged beside its destination,
   which must not exist, flushed, and renamed into place.
6. **Local-only by default.** A new workspace is local-only. Anything that would use the network
   (object-store connectors, uploads, remote models) calls `require_network(purpose)` first and is
   refused with `LocalOnlyError` until the workspace explicitly allows it; the choice is remembered.
   `LocalOnlyError` is a policy refusal, not an `OSError`, so code that shrugs off I/O errors
   cannot swallow it.
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
- The workspace relies on POSIX `flock` and `O_DIRECTORY`, as discovery already does on
  `O_NOFOLLOW`; a Windows port needs another lock.
- Revisit if many processes contend on one workspace (M9 queues), if hashing reads dominates
  ingest time, or if a platform's cache directory convention should replace XDG's.

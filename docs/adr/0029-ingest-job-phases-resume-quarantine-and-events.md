# 0029 — The ingest job: nine phases, the workspace as the only checkpoint, quarantine by source, cancellation and events

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-6
- Extends: ADR 0008 §5 (what the runtime owns), ADR 0026 (the workspace the job checkpoints in)

## Context

ADR 0008 gave adapters four methods and left the runtime resume, isolation and explanation. ADR
0024 fixed the adapter types and said the runtime "calls the same four methods and the same
checks, adding persistence, isolation and resume". ADR 0026 built the workspace: a saved ledger per
root, a saved plan per (source, transform), and chunk outputs committed by one atomic rename, with
the note that "resume is skip committed chunk ids". MVL-6 asks for the execution substrate itself:
explicit phases, persistent checkpoints, retries and partial success, cancellation and resume, and
structured events per stage. Its acceptance: a process killed mid-ingest and restarted resumes
without duplicating work, and one corrupt source does not invalidate unrelated sources.

Two forces shaped the detail. The adapter contract's cross-chunk checks (`check_source_output`)
hold every `seq` of a stream in a set, which grows with the row count; the runtime cannot use them
as written over a billion-row stream (PR #22 review). And a crashed chunk raises the question of
what to do with the rest of its source's output.

## Decision

1. **Nine phases are the job's states**, in this order and with these meanings:

   | Phase | Work |
   |---|---|
   | `discover` | walk the root: regular files, symlinks (events), entries that could not be read (findings) |
   | `fingerprint` | hash every file into the root's ledger, reconcile absences, save the ledger |
   | `inspect` | read each distinct source's head once, probe with every adapter, select and configure |
   | `plan` | reuse the workspace's plan for (source, transform) or call `plan`, check it, save it |
   | `parse` | `ingest` one chunk the workspace has not committed |
   | `normalize` | check that chunk's output against the contract and commit it, whole or not at all |
   | `assemble` | admit each source whose chunks all committed and pass the cross-chunk laws; stage the package |
   | `validate` | `read_package` over the staged package: every file, id, series and the receipt |
   | `commit` | write the envelope into the staged package and rename it into place |

   `parse` and `normalize` alternate once per chunk, so the envelope separates adapter time from
   store time; every other phase runs once over every source. A phase starts when the job first
   enters it and finishes when the job moves past it, so each emits exactly one `phase_started`
   and one `phase_finished` whatever the chunk count. `adapter.inspect` is not called: its results
   are the dry run's (MVL-15).
2. **The workspace is the only checkpoint.** A job persists nothing of its own: the ledger, the
   plans and the committed chunks are each written whole or not at all (ADR 0026 §2), and a job
   killed at any instant leaves them consistent. Resume is a new job over the same root and
   workspace: it walks and fingerprints again (bytes may have changed), reuses each saved plan, and
   skips each committed chunk id. The package it builds is byte-identical to an uninterrupted run's,
   and only the envelope (a new job id, new clocks) differs. There is no job file to repair.
3. **Faults are findings about one source; the job continues.**
   - `ingest` is tried `attempts` times (default 2). A `ContractError` is a bug and is not retried;
     a `SourceChangedError` means the bytes are not the fingerprinted ones and is never retried;
     anything else gets the remaining attempts, with a `chunk_retried` event per retry.
   - A chunk that fails for good, a plan that raises or breaks the contract, a source that changed
     or cannot be opened, and a source whose committed outputs break a cross-chunk law each
     **quarantine their source**: the source's output is left out of this package and a
     `neptune.runtime.*` finding citing the source says why. Its remaining chunks are still run and
     committed, so after an adapter fix (a new version, so new chunk ids) or a transient fault, the
     next job redoes only what failed.
   - Quarantine is by source, not by chunk. Including a crashed source's other chunks would leave
     series rows whose `Stream` record lived in the crashed chunk with nowhere to go, and a
     package that holds part of a source under a transform cannot say which part; a package holds
     a source's complete output under a transform or none of it, and the finding says which.
4. **The runtime is a producer.** Its findings name a `TransformRecord` of adapter id
   `neptune.runtime`, its version, and config `{"attempts": N}`, so a package records who said
   what under which policy (as the probe engine, MVL-8, does for `neptune.probe`). Codes, in
   `neptune.runtime.lineage.FINDING_CODES`: `chunk_failed`, `plan_failed`, `output_invalid`
   (failed, error); `source_changed` (inconsistent, error); `source_unreadable` (skipped, error);
   `entry_skipped` (skipped; info for a special file, warning for one that vanished, error for one
   that could not be read). Messages and details carry ids, codes and exception class names,
   never exception text. The transform enters the package only with its findings, so a package
   with none is independent of the runtime's version.
5. **Cross-chunk laws are checked in bounded memory.** At `normalize`, `seq` must be unique within
   the chunk, per stream. At `assemble`, each run's `seq` range is read from its Parquet row-group
   statistics (`store.series.run_seq_range`) and the ranges of one stream's chunks must not
   overlap: with uniqueness inside each chunk, disjoint ranges prove uniqueness overall at one
   pair per chunk. The other cross-chunk laws (no record or finding emitted twice, every run names
   a declared stream, every stream has a run, the output says something) are checked over the
   committed records, which the package holds in memory anyway (ADR 0022). Row contracts are
   checked when `validate` reads every series back (ADR 0025 §6).
6. **Cancellation** is a `threading.Event` checked before every source, every chunk and every
   phase. The job finishes the chunk in hand, commits it, discards any staged package and returns
   a `cancelled` outcome with no package; the workspace keeps the work and the next job resumes.
   A signal that kills the process is the same from the workspace's point of view.
7. **Events** are plain data (`JobEvent(kind, phase, details)`), canonical JSON, with no clock,
   host or absolute path, delivered to a callback as they happen. Kinds cover phases, entries,
   sources (hashed, absent, selected, unsupported, ambiguous, unreadable, changed, planned,
   admitted, quarantined), chunks (skipped, parsed, retried, committed, failed), the package
   (staged, verified) and the job (committed, cancelled, failed).
8. **The envelope** (ADR 0022 §4) is the runtime's: a random job id unless one is given, start
   and finish as RFC 3339 UTC, the host, the root as the host names it, and seconds per phase for
   all nine. It is written into the staged package before the rename, so a package appears with
   its envelope or not at all, and a kill between the two cannot leave a package that blocks a
   restart.
9. **Selection** applies the registry's rule (ADR 0024 §7) with each adapter's probe isolated: a
   probe that raises is an event and that adapter is out of the source's candidates. Unsupported
   and ambiguous sources are events, not findings, here; the probe engine (MVL-8) replaces
   this one method and brings the findings.
10. **What fails the job** (`JobError`): a root that is not a directory or cannot be read, a
    destination that exists, options naming an adapter or option that does not exist, a
    workspace or disk that will not read or write, and a staged package that does not verify.
    Never one source.

## Alternatives considered

- **A job file** (`jobs/<id>.json` with a phase cursor and per-source state). It would let a
  restart skip discovery and fingerprinting, but it is a second statement of what is done beside
  the workspace, and after a kill the two can disagree; the sources may also have changed. The
  workspace's committed work is the truth, and re-hashing was already accepted (ADR 0026).
- **Quarantine by chunk**, keeping a crashed source's other chunks in the package. More output
  per package, but rows of a stream declared by the crashed chunk would be orphaned, and the
  package could not say what fraction of the source it holds. Revisit when a real adapter needs it.
- **A quarantine commit**: the finding as the chunk's committed output. Resume would then never
  retry the chunk, so a transient failure would be frozen under a deterministic chunk id.
- **Per-source pipelines** (inspect, plan, parse one source before the next) instead of global
  phases. The first chunk commits sooner, but there is no plan-wide progress or cost before
  parsing starts, and the phases the issue names would blur. Interleaving for throughput is the
  scheduler's (M9), inside the chunk loop.
- **Reusing `check_source_output`** for the cross-chunk laws. It needs every chunk's output in
  memory and a set of every `seq`; neither is bounded by chunk size.
- **Retrying contract violations.** A deterministic bug repeats; the retry only costs time.
- **Phase events on every parse/normalize transition.** Two extra events per chunk for no
  information the chunk events do not carry.
- **Writing the envelope after the rename.** A kill between the two would leave a package without
  its envelope that the next job refuses to overwrite.

## Consequences

- MVL-9 reuses committed chunks across jobs and roots by chunk id and adds garbage collection;
  MVL-10 wraps `_parse` in a subprocess; MVL-11 prints the events and the envelope; MVL-15's dry
  run is phases one to four plus `adapter.inspect`; MVL-41's validators run in `validate` over the
  staged package; MVL-8's engine replaces `IngestJob._select`.
- A restart hashes every source again and reads each committed chunk's records twice (once for
  the laws, once to stage). Both are proportional to what the package holds, not to the sources'
  bytes beyond the hash; M9 may fold them together.
- A package's id does not depend on the runtime's version unless the runtime made a finding.
- Staging debris a killed process leaves in the workspace is not cleared by the next job, since
  several processes may share a workspace (ADR 0026); `Workspace.clear_staging` is for MVL-9's
  collector. A package's staging directory beside its destination is removed on every exception
  the job can catch and is a hidden `.<name>.*` sibling otherwise.
- Revisit if a supported format needs part of a crashed source in the package, if record ids of
  one source no longer fit in memory, or if the global phase order costs throughput that M9's
  scheduler cannot recover inside the chunk loop.

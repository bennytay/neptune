# Ingestion pipeline

Status: stage contract agreed; the job runtime (MVL-6), the local store (MVL-16) and the cache
(MVL-9) are implemented; the sandbox (MVL-10) is the rest of M2.

## Stages

| # | Stage | Responsibility | Owner | Issue |
|---|---|---|---|---|
| 1 | discover | enumerate candidate sources through a `Source` (local FS now, object store later); apply ignore, symlink and traversal policy | discovery | MVL-2, MVL-45 |
| 2 | fingerprint | size, magic bytes, streaming sha256 + per-chunk hashes; emit `SourceArtifact` / `SourceRevision` | identity | MVL-2 |
| 3 | probe | ask registered adapters for confidence + reasons; rank; surface ties as ambiguity findings | discovery + adapters | MVL-8 |
| 4 | inspect | cheap per-source summary (streams, extents, counts) without full parse | adapters | MVL-7 |
| 5 | group | propose run/session groupings from filesystem signals (v0) and later from evidence (M7) | discovery | MVL-13, MVL-34 |
| 6 | plan | adapters emit chunks with deterministic ids and cost estimates | adapters | MVL-7 |
| 7 | ingest | done: per-chunk pure parse → canonical records + findings; the job (ADR 0028) reuses every committed chunk by its id, across jobs and roots (ADR 0031), retries a chunk that raises and quarantines its source with a `neptune.runtime.*` finding | runtime + adapters | MVL-6, MVL-9 |
| 8 | store | done: each chunk's records, findings and sorted series runs are committed to the workspace atomically; each stream's runs are merged once into its series file, kept as a derivative and copied into every package that holds it (ADRs 0025, 0026, 0031) | store | MVL-5, MVL-16, MVL-9 |
| 9 | validate | cross-source integrity checks over the store; findings, not exceptions | validate | MVL-41 |
| 10 | receipt | done: core computed from the package's records (store); the job writes the volatile envelope (job id, clocks, host, root, seconds per phase) into the package before publishing it | store + runtime | MVL-5, MVL-6 |

Alignment (clocks, frames, identities, bindings) is a separate pass after ingestion (M7); it produces new
records with their own provenance and never rewrites what stages 1–10 produced.

## Runtime vs adapter responsibilities

| Concern | Owner | Mechanism |
|---|---|---|
| Resume after crash | runtime | deterministic chunk ids + the workspace's committed chunks and saved plans (ADR 0026; ADR 0028 §2) |
| Cache | runtime | key = chunk id, which covers (source id, adapter id, adapter version, config hash, libraries, context) (ADR 0024 §4); derivatives by `DerivativeKey`; each miss names its rule (ADR 0031) |
| Partial failure | runtime | per-chunk isolation and retries; adapter crash → finding, the source is quarantined, the job continues (ADR 0028 §3) |
| Sandboxing | runtime | subprocess with CPU/memory/time limits (MVL-10) |
| Adapter-local problems | adapter | `IngestFinding`s in the chunk output |
| Cross-source validation | validate | runs over the store after all chunks |
| Explanation | runtime | assembles `probe`/`plan` results + descriptors into the receipt |

## The job (MVL-6)

`neptune.runtime.IngestJob(root, destination, workspace, registry, options).run()` is the runtime: one
state machine over the stages above, in nine phases (ADR 0028):

| Phase | Stages above | Does |
|---|---|---|
| `discover` | 1 | walks the root; symlinks are events, unreadable or special entries are `entry_skipped` findings |
| `fingerprint` | 2 | hashes every file into the root's persisted ledger, reconciles absences, saves the ledger |
| `inspect` | 3 | reads each distinct source's head once, probes with every adapter (each isolated), selects and configures |
| `plan` | 6 | reuses the workspace's saved plan for (source, transform) or calls `plan`, checks it, saves it |
| `parse` | 7 | `ingest` on one chunk the workspace has not committed; `attempts` tries (default 2) |
| `normalize` | 7–8 | `check_chunk_output` plus `seq` unique within the chunk; commit, whole or not at all |
| `assemble` | 8 | admits each source whose chunks all committed and pass the cross-chunk laws (each run checked against its stream and agreeing on columns, disjoint `seq` ranges, no duplicate ids, every run has its stream); stages the package beside its destination |
| `validate` | 9 | `read_package` over the staged package; MVL-41's validators go here |
| `commit` | 10 | writes the envelope into the staged package and renames it into place |

- **Resume.** A new job over the same root and workspace is the resume: it hashes again (bytes may
  have changed), reuses saved plans, skips committed chunk ids, and builds the same package. Killing
  the process at any instant is safe: every workspace write is an atomic rename.
- **Partial success.** A chunk that raises after every attempt, a plan that raises, a source that
  changes under the job or cannot be opened, or output breaking a cross-chunk law quarantines that
  source: its output stays out of the package and a `neptune.runtime.*` finding citing it says why.
  Every other source lands. A `ContractError` is a bug and is never retried. Committed chunks of a
  quarantined source stay in the workspace, so the rerun after a fix redoes only what failed.
- **Cancellation.** A `threading.Event`, checked before each source, chunk and phase from `inspect`
  on (the walk and its ledger always finish). The chunk in hand finishes and commits; a staged
  package is discarded; the outcome is `cancelled` with no package.
- **Events.** `on_event(JobEvent(kind, phase, details))` for every phase start and finish, every source
  (hashed, selected, unsupported, ambiguous, planned, admitted, quarantined, …) and chunk (skipped,
  parsed, retried, committed, failed). Canonical JSON, no clock: the consumer adds one.
- **Job failure** (`JobError`) is reserved for the job itself: an unreadable root, a destination that
  exists, options naming an unknown adapter or option, a workspace or disk that will not write.

## The cache (MVL-9)

The workspace is the cache (ADR 0031). A job reuses whatever it keeps under the key the job
needs, and nothing else decides: no clock, file time or flag.

| Kept | Key | Reused at |
|---|---|---|
| plan | (source content id, transform id) | `plan` |
| chunk output | chunk id: source content id + transform (adapter id, version, config hash, libraries) + chunk context | `parse` |
| verdict on the cross-chunk laws | `neptune.runtime.admission/1`: the plan's chunk ids + runtime version | `assemble` |
| series file | `neptune.store.series/1`: the merged chunks' ids + `SERIES_SETTINGS` | `assemble` |

- **Invalidation rules.** A plan misses with the first rule that holds: `transform_changed` (this
  adapter planned it under another version, config or libraries; `changed` names which),
  `adapter_changed` (only other adapters did), `source_changed` (a location held other bytes
  before), `source_new`. A chunk not committed misses with `not_committed` if its plan was kept,
  else with its plan's rule. A derivative is `held`, `absent` or `corrupt` (damaged: rebuilt).
  So a change to one adapter recomputes that adapter's chunks and derivatives and nothing else.
- **Lazy derivatives.** Verdicts and series files are built the first time an assembly needs them,
  kept under `derivatives/`, and copied (hash-checked) after; a damaged one is rebuilt.
- **Report.** `JobOutcome.cache` and `volatile/cache-report.json`: every plan, chunk and derivative
  with its hit or miss and rule, and the job's `probe`/`plan`/`ingest` calls. Deterministic; outside
  the manifest, so a rerun's package is byte-identical.
- **Collection.** `neptune.runtime.collect(workspace, registry, options)` keeps only plans under the
  current transforms for sources a saved ledger still holds, with their chunks and derivatives. Jobs
  hold the workspace's lock shared; collection is refused while any job runs.
- **Cost of an unchanged source**: its fingerprint hash, a probe of its head, copying its series
  files and verifying the new package. In the acceptance test a 4 GiB recording re-ingests with
  zero `plan` and `ingest` calls; hashing is the only pass over its bytes.

## Dry-run

`explain`/dry-run (MVL-15) executes stages 1–6 only and renders the plan. It must never call `ingest`.

## Determinism contract

Given identical source bytes, adapter versions and config, stages 2–10 produce byte-identical package
contents. Ordering is defined everywhere (sorted paths, sorted ids, sorted keys). Wall-clock, host and
duration live only in the receipt envelope.

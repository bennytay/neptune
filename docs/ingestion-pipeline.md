# Ingestion pipeline

Status: stage contract agreed; the job runtime (MVL-6), the local store (MVL-16), the cache
(MVL-9) and the parser sandbox (MVL-10) are implemented, and the M2 gate (MVL-57) wired them
together — M2's runtime is complete.

## Stages

| # | Stage | Responsibility | Owner | Issue |
|---|---|---|---|---|
| 1 | discover | enumerate candidate sources through a `Source` (local FS now, object store later); apply ignore, symlink and traversal policy | discovery | MVL-2, MVL-45 |
| 2 | fingerprint | size, magic bytes, streaming sha256 + per-chunk hashes; emit `SourceArtifact` / `SourceRevision` | identity | MVL-2 |
| 3 | probe | done: `discovery.probe.ProbeEngine` sniffs the head, asks every adapter (crashes isolated), applies the registry's rule, opens zip/tar/gzip/bzip2/xz within `ProbePolicy`, and reports ties, unclaimed sources and container problems as `neptune.probe.*` findings (ADR 0027); the job runs it in one sandboxed call per source and re-derives its reply (ADR 0033 §1) | discovery + adapters | MVL-8, MVL-57 |
| 4 | inspect | cheap per-source summary (streams, extents, counts) without full parse | adapters | MVL-7 |
| 5 | group | v0 done: `derived.grouping.LayoutGrouper` proposes sessions from the scan's observed layout (`discovery.layout`: locations, links, what names state; never contents or mtimes) by named rules with confidence bands; conflicting readings are contested, never chosen; nested session directories include the inner reading by id, at most 16 deep; every file lies in a proposal or is unassigned; findings `neptune.grouping.*`; proposals reach the package as derived tables (ADR 0036). MVL-34 swaps in the evidence-graph assembler behind the same `Grouper` interface | discovery (layout) + derived (rules) | MVL-13, MVL-34 |
| 6 | plan | adapters emit chunks with deterministic ids and cost estimates | adapters | MVL-7 |
| 7 | ingest | done: per-chunk pure parse → canonical records + findings; the job (ADR 0028) reuses every committed chunk by its id, across jobs and roots (ADR 0031), retries a chunk that raises and quarantines its source with a `neptune.runtime.*` finding | runtime + adapters | MVL-6, MVL-9 |
| 8 | store | done: each chunk's records, findings and sorted series runs are committed to the workspace atomically; each stream's runs are merged once into its series file, kept as a derivative and copied into every package that holds it (ADRs 0025, 0026, 0031) | store | MVL-5, MVL-16, MVL-9 |
| 9 | validate | done: `neptune.validate` runs versioned integrity and data-quality rules over the verified package (truncation roll-up, counts, time order, reversed intervals, missing metadata, schema and id conflicts, dangling references, unresolved frames, stale calibrations, software conflicts); findings are cited, capped, `warning`, and added to the package so the receipt lists them; rules whose kinds are not on main are off and listed as not covered (ADR 0054) | validate | MVL-41 |
| 10 | receipt | done: core computed from the package's records (store); the job writes the volatile envelope (job id, clocks, host, root, seconds per phase) into the package before publishing it | store + runtime | MVL-5, MVL-6 |

Alignment (clocks, frames, identities, bindings) is a separate pass after ingestion (M7); it produces new
records with their own provenance and never rewrites what stages 1–10 produced.

## Runtime vs adapter responsibilities

| Concern | Owner | Mechanism |
|---|---|---|
| Resume after crash | runtime | deterministic chunk ids + the workspace's committed chunks and saved plans (ADR 0026; ADR 0028 §2) |
| Cache | runtime | key = chunk id, which covers (source id, adapter id, adapter version, config hash, libraries, context) (ADR 0024 §4); derivatives by `DerivativeKey`; each miss names its rule (ADR 0031) |
| Partial failure | runtime | per-chunk isolation and retries; adapter crash → finding, the source is quarantined, the job continues (ADR 0028 §3) |
| Sandboxing | runtime | done: each source's probe (the engine, containers included), each plan and each `ingest` in a forked child confined by limits (CPU, wall, memory, a separate 64 MiB `reply_bytes` cap), seccomp and Landlock; its reply decoded as bounded JSON; plan and ingest may write only beneath a per-call scratch directory (`scratch_bytes` per file); only the job writes the workspace; fails closed below Landlock ABI 3 unless `allow_degraded_sandbox`; in-process only when chosen (ADRs 0030, 0033) |
| Adapter-local problems | adapter | `IngestFinding`s in the chunk output |
| Cross-source validation | validate | runs over the store after all chunks |
| Explanation | runtime | assembles `probe`/`plan` results + descriptors into the receipt |

## The job (MVL-6)

`neptune.runtime.IngestJob(root, destination, workspace, registry, options).run()` is the runtime: one
state machine over the stages above, in nine phases (ADR 0028):

| Phase | Stages above | Does |
|---|---|---|
| `discover` | 1 | sweeps the workspace's scratch and staging debris; walks the root (a directory, or one regular file: ADR 0043) under the job's ignore rules; every symlink, special, unreadable or ignored entry is discovery's finding (ADR 0029 §1, ADR 0043 §6) |
| `fingerprint` | 2 | hashes every file into the root's persisted ledger, reconciles absences, saves the ledger; a size that changed while hashing is a finding |
| `inspect` | 3 | reads each distinct source's head once and runs the probe engine over it in one sandboxed call (every adapter's probe, the container listing); selects and configures; a tie, an unclaimed source or a container problem is a `neptune.probe.*` finding (ADR 0033 §1); then groups the scan's layout into session proposals (stage 5, no adapter call; `JobOptions.grouping` declares sessions, stated and set against the rules' readings; ADR 0036) |
| `plan` | 6 | reuses the workspace's saved plan for (source, transform) or calls `plan`, checks it, saves it |
| `parse` | 7 | `ingest` on one chunk the workspace has not committed; `attempts` tries (default 2) |
| `normalize` | 7–8 | `check_chunk_output` plus `seq` unique within the chunk; commit, whole or not at all |
| `assemble` | 8 | admits each source whose chunks all committed and pass the cross-chunk laws (each run checked against its stream and agreeing on columns, disjoint `seq` ranges, no duplicate ids, every run has its stream); stages the package beside its destination; before staging, it introspects the admitted sources' streams: each cited schema definition is read once, bounded, and parsed into one `definition_layout` line per distinct definition (within name, pointer, per-layout and per-package output limits) that each stream's `stream_layout` line names, and `stream_semantic` lines infer what each stream carries (ADR 0049); when the package holds a run, the `neptune.bindings` pass binds each run to the configuration, software, hardware and calibration snapshots its own source names (canonical `snapshot_binding` records) or that are nearest of their slot in its sessions (`derived/snapshot_binding`), and reports ties and unbound kinds as findings (ADR 0064) |
| `validate` | 9 | `read_package` over the staged package, then `validate_package`; any findings are added with the validator's transform (`amend`: restaged, verified again); `package_verified` carries the rules' coverage (ADR 0054) |
| `commit` | 10 | writes the envelope into the staged package and renames it into place |

- **Resume.** A new job over the same root and workspace is the resume: it hashes again (bytes may
  have changed), reuses saved plans, skips committed chunk ids, and builds the same package. Killing
  the process at any instant is safe: every workspace write is an atomic rename.
- **Partial success.** A chunk that raises after every attempt, a plan that raises, a source that
  changes under the job or cannot be opened, a read that comes up short, or output breaking a
  cross-chunk law quarantines that source: its output stays out of the package and a finding
  citing it says why (`neptune.runtime.*`; a short read from a source that no longer matches its
  artifact is `neptune.discovery.short_read`, never retried, and a changed or short source also
  gets `verify_artifact`'s account; over an intact source a short read is the adapter's own
  raise, ADR 0033 §3).
  Every other source lands. A `ContractError` is a bug and is never retried. Committed chunks of a
  quarantined source stay in the workspace, so the rerun after a fix redoes only what failed. The
  finding names the step, law, exception class and ids, never an exception's text or a repr, so
  the same failing job writes the same package (ADR 0028 §4).
- **Sandbox.** Each adapter call runs in a confined child process (ADR 0030). One that dies is
  `adapter_crashed` (its signal or exit status) and is retried like a raise; one stopped at
  `cpu_seconds`, `wall_seconds` or `memory_bytes` is `limit_exceeded` and is not. Both name the
  adapter, its version, the step and the chunk, and quarantine the source as any failure does.
  `JobOptions(isolation=Isolation.IN_PROCESS)` runs adapters in the job's process instead.
  Each `plan` and `ingest` call gets a fresh scratch directory under `<workspace>/scratch`
  (`contract.scratch_directory()`), the only place it may write, removed when it returns; a
  file past `scratch_bytes` is `limit_exceeded`, and a call that needs scratch and has none
  raises `ScratchUnavailableError`: `plan_failed` or `chunk_failed` with `cause`
  `scratch_unavailable`, never retried, nothing committed (ADR 0033 §2). If a source's probe
  call dies, each adapter is asked again on its own, and a container it was listing is left
  unopened (`neptune.probe.inspection_failed`).
- **Cancellation.** A `threading.Event`, checked before each source, chunk and phase from `inspect`
  on (the walk and its ledger always finish). The chunk in hand finishes and commits; a staged
  package is discarded; the outcome is `cancelled` with no package. The last checkpoint is the
  start of `commit`: after it the package is published, and an exception `on_event` raises then
  propagates with the job `committed` (`IngestJob.committed`), never `failed` (ADR 0035 §3).
- **Events.** `on_event(JobEvent(kind, phase, details))` for every phase start and finish, the
  start's sweep (`workspace_swept`: scratch and staging entries removed), every source (hashed,
  selected, unsupported, ambiguous, short read, planned, admitted, quarantined, …) and chunk
  (skipped, parsed, retried, committed, failed), `probe_failed` (the source's probe call, then
  each adapter that fails on its own), and `sandbox_ready` (the isolation, the limits, the host's
  Landlock ABI, and on a degraded host a `degraded` list of the guarantees it could not give) as
  `inspect` starts, and `sessions_proposed` (grouping's counts) as it ends. A sandboxed chunk stopped by a limit is a `limit_exceeded` finding naming the
  limit (`cpu_seconds`, `wall_seconds`, `memory_bytes` or `reply_bytes`). Canonical JSON, no clock:
  the consumer adds one.
- **Job failure** (`JobError`) is reserved for the job itself: an unreadable root, a destination that
  exists, options naming an unknown adapter or option, a workspace or disk that will not write, a
  workspace whose scratch root overlaps the ingest root, a host that cannot run the sandbox or
  whose Landlock ABI is below the floor (unless `allow_degraded_sandbox`).

## The cache (MVL-9)

The workspace is the cache (ADR 0031). A job reuses whatever it keeps under the key the job
needs, and nothing else decides: no clock, file time or flag.

| Kept | Key | Reused at |
|---|---|---|
| plan | (source content id, transform id) | `plan` |
| chunk output | chunk id: source content id + transform (adapter id, version, config hash, libraries) + chunk context | `parse` |
| verdict on the per-chunk laws, for a chunk another runtime version admitted | `neptune.runtime.chunk-laws/1`: the chunk id + runtime version | `normalize` |
| verdict on the cross-chunk laws | `neptune.runtime.admission/1`: the plan's chunk ids + runtime version | `assemble` |
| series file | `neptune.store.series/1`: the merged chunks' ids + `SERIES_SETTINGS` | `assemble` |

- **Invalidation rules.** A plan misses with the first rule that holds: `transform_changed` (this
  adapter planned it under another version, config or libraries; `changed` names which),
  `adapter_changed` (only other adapters did), `source_changed` (a location held other bytes
  before), `source_new`. A chunk not committed misses with `not_committed` if its plan was kept,
  else with its plan's rule. A derivative is `held`, `absent` or `corrupt` (damaged: rebuilt).
  So a change to one adapter recomputes that adapter's chunks and derivatives and nothing else.
- **New laws judge what is kept.** A chunk records the runtime version whose laws admitted it.
  A job of another version (or over a chunk with no record) judges its committed output by its
  own per-chunk laws before reusing it, once per version and without the adapter; a chunk they
  refuse fails as it would in a fresh workspace (`chunk_failed`, one attempt), so the package
  never depends on what the cache held.
- **Lazy derivatives.** Verdicts and series files are built the first time an assembly needs them,
  kept under `derivatives/`, and copied (hash-checked) after; a damaged one is rebuilt.
- **Report.** `JobOutcome.cache` and `volatile/cache-report.json`: every plan, chunk and derivative
  with its hit or miss and rule, and the job's `probe`/`plan`/`ingest` calls. Deterministic; outside
  the manifest, so a rerun's package is byte-identical.
- **Collection.** `neptune.runtime.collect(workspace, registry, options)` keeps only plans under the
  current transforms for sources a saved ledger still holds, with their chunks and derivatives. Jobs
  hold the workspace's lock shared; collection is refused while any job runs.
- **Cost of an unchanged source**: its fingerprint hash, one sandboxed probe call over its head,
  copying its series files and verifying the new package. In the acceptance test a 4 GiB recording re-ingests with
  zero `plan` and `ingest` calls; hashing is the only pass over its bytes.

## Dry-run

`IngestJob.dry_run()` (ADR 0035, ADR 0044) runs `discover`, `fingerprint`, `inspect` and `plan`, then
stops: state `planned`, a `job_planned` event, no package, never an `ingest` call. It needs no
destination and only reads the sources; the ledger and plans it saves are the ones `run` reuses (the
workspace is the cache; no package id depends on it, ADR 0035 §9), and its cache report marks the
chunks the workspace already holds. Each selected source is also given to its adapter's `inspect`,
sandboxed; a failed `inspect` is shown and never quarantines (a short read or a change is the
source's, as for `plan`). The outcome carries an `Explanation` (`neptune.runtime.explain`):

- inventory (files, links, skipped entries), and per distinct source its status, detected format,
  every adapter's verdict (`selected`/`tied`/`outranked`/`declined`/`failed`, or `pinned` with
  the manifest rule as `pin`, ADR 0047; confidence, reasons, why), `inspect` summary, and plan (rule, chunks, committed, bytes left to read);
- the session grouping (proposals with reasons, contested readings, unassigned files);
- work left (chunks, bytes, `ingest` calls) and heavy sources (≥ 256 MiB or ≥ 1024 chunks left,
  non-streaming memory growth, declared memory above the sandbox limit);
- everything left out (unsupported, ambiguous, quarantined, unreadable, skipped, links) and the
  ambiguity findings.

It is bounded (`Bounds`): every list ≤ 10,000 entries, per source ≤ 64 locations, ≤ 16 reasons per
verdict, ≤ 256 container members, an `inspect` summary ≤ 16 KiB and ≤ 64 `inspect` findings, and
≤ 64 entries in every list of a session proposal or unassigned file, nested ones included; each cut
is a `*_omitted` count beside its list and one `neptune.explain.truncated` finding. Totals are over
everything. `dumps()` is canonical JSON (`neptune.explanation/1`), byte-identical for the same root,
adapters, config and workspace contents; `render()` is the same for people. The SDK's `dry_run`
returns it as `IngestResult.explanation` (`sdk.md`); `neptune ingest --explain` prints it (`cli.md`).

## Determinism contract

Given identical source bytes, adapter versions and config, stages 2–10 produce byte-identical package
contents, whatever the workspace held: the package's ledger is the job's own scan, never the root's
history (ADR 0035 §9). Ordering is defined everywhere (sorted paths, sorted ids, sorted keys). Wall-clock, host and
duration live only in the receipt envelope.

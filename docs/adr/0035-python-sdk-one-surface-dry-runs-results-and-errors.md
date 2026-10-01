# 0035 — The Python SDK: one sync and async surface over the job, dry runs, results and a stable error taxonomy

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-12
- Extends: ADR 0028 §1, §6, §7 (the job gains a dry run that ends `planned`), ADR 0026 §6
  (local-only, now enforced at the SDK's door for remote sources and remote execution), ADR 0026
  §4 (a package lists its own job's scan, not the root's ledger history: §9)

## Context

MVL-12 asks for a native programmatic interface: `ingest(path_or_uri)`, inspect and dry-run
calls, progress callbacks and async iteration, typed results, explicit adapter overrides, local
and remote execution modes, and a stable error taxonomy; its acceptance is that a robotics
codebase integrates ingestion in fewer than about ten lines without shelling out to the CLI.

The runtime already does the work. `IngestJob` (ADR 0028) runs every phase, emits structured
events, checkpoints in the workspace, quarantines by source, honours a cancel event and returns a
`JobOutcome`; the sandbox (ADR 0030) confines every adapter call; the workspace is local-only by
default (ADR 0026 §6). So the SDK must be a thin, typed facade: no ingestion logic of its own, no
second configuration model, the runtime's events, findings and receipts unchanged, and nothing
that reaches a package taken from a clock or a random number. Three things were missing below
it: the job could not stop after planning (a dry run), every job needed a destination, and
`JobError` carried only a message, so a program could not tell a busy workspace from a missing
sandbox without parsing text.

## Decision

1. **`neptune.sdk` is the SDK**: `client` (`Neptune`, `AsyncNeptune`, `Ingestion`,
   `AsyncIngestion`, and the shorthands `ingest` and `dry_run`), `result` (`IngestResult`,
   `read_package`) and `errors`. It imports the runtime, the store and the adapters; the runtime
   is imported by nothing else but the SDK, and the CLI (MVL-11) wraps the SDK. It re-exports
   the runtime types a caller needs (`JobOptions`, `Isolation`, `Limits`, `JobEvent`,
   `JobState`, `Phase`, `Workspace`, `builtin_adapters`). Nothing is re-exported from the
   top-level `neptune` package, which stays importable without pyarrow or the runtime.
2. **One surface, two clients.** `Neptune` and `AsyncNeptune` take the same constructor
   (`workspace`, `adapters`, `options`, `remote`) and have the same methods with the same
   parameters: `ingest(source, destination)`, `dry_run(source)`, `start(source, destination)`
   and `start_dry_run(source)`, each with `on_event` and `cancel` where it applies; the async
   `ingest` and `dry_run` are awaited and return the same `IngestResult`.
   - The sync `ingest` and `dry_run` run the job on the calling thread; `on_event` is called
     there, as the runtime calls it.
   - **The async client wraps the sync job in a thread.** Each job runs on a worker thread of
     its own (`neptune-ingest`, not a daemon); its events cross to the event loop with
     `call_soon_threadsafe` into an `asyncio.Queue`, so `on_event` runs on the loop's thread and
     `async for` yields them in order. The runtime stays synchronous: its phases are blocking
     I/O and a fork per adapter call, and nothing in it would gain from `await`.
   - `start` and `start_dry_run` return a handle on a job running on its own thread, on both
     clients: `Ingestion` (iterate, `result(timeout)`, `cancel`, `done`, a `with` block that
     cancels on an exception) and `AsyncIngestion` (`async for`, `await result()`, `wait`,
     `cancel`, `done`). Events are queued from the job's start, so iterating late misses none.
     The call is checked on the caller's thread before the job's thread starts, so a bad call
     raises at once and starts nothing.
   - The sandbox forks from the worker thread. CPython makes the forking thread the child's
     main thread, so the child's confinement (signal handlers included) is unchanged, and the
     parent-death signal, which Linux ties to the forking thread, still fires only if the job
     dies: that thread waits for every child it forks.
3. **Cancellation is the runtime's, and nothing is left running.** `cancel` is the job's own
   `threading.Event` (ADR 0028 §6): the job finishes the chunk in hand, commits it and ends
   `cancelled` with no package. Cancelling a task awaiting `AsyncNeptune.ingest` sets that event,
   waits for the job to reach its checkpoint, and then lets `CancelledError` through; an
   exception from an async `on_event` does the same and propagates. A sync `on_event` that
   raises stops the job as the runtime defines (the exception propagates, the job is `failed`,
   staging is discarded). In every case the workspace keeps what was committed, and resuming is
   calling `ingest` again.
4. **The dry run is the job's.** `IngestJob.dry_run()` runs `discover`, `fingerprint`,
   `inspect` and `plan` and stops: state `planned` (a new `JobState`), a `job_planned` event
   (`{"sources": n}`, the sources planned), no package, no `ingest` call. A job may be built
   with no destination (`destination=None`); such a job can only dry-run, and `run` refuses it
   before starting, so `JobOutcome.destination` is `Path | None`. The ledger and plans a dry run
   saves are the ones `run` saves, so the ingest that follows plans nothing again. The plan
   phase of a dry run records which of a source's chunks the workspace already holds, so its
   cache report marks them `committed`: what is left to parse is every chunk that is not. (A
   run marks a chunk `committed` only when it reaches and reuses it, as before.) MVL-15 adds adapters' `inspect`, grouping and the rendered explanation to
   this call; the SDK's `dry_run` returns it as an `IngestResult` in the `planned` state.
5. **Results are the runtime's outcome, typed.** `IngestResult` holds the `JobOutcome` and reads
   it: `state`, `committed`, `cancelled`, `planned`, `job`, `destination`, `package`,
   `receipt` (the receipt's record id, `CacheReport.receipt`), `ingested`, `findings`, `cache`,
   `durations`. `findings` are the job's own (discovery's, the probe engine's, the runtime's),
   known even when nothing was committed; `read_receipt()` reads the committed package's
   `receipt.json`, every adapter's findings included, and checks it hashes to `receipt`;
   `read_package()` reads and verifies the whole package. Nothing is copied or reinterpreted,
   so a field the runtime adds is in `outcome` at once.
6. **A stable error taxonomy.** Every SDK call raises only `NeptuneError` subclasses, each with
   a `code` that never changes meaning: `invalid_request` (`invalid_source`,
   `invalid_destination`, `destination_exists`, `invalid_configuration`), `unsupported`,
   `network_refused`, `sandbox_unavailable`, `workspace_unusable`, `package_invalid`,
   `job_failed` (`publish_incomplete`). The runtime's or the store's exception is the
   `__cause__`. A `JobError` is classified by its cause's type, never its text: `SandboxError` →
   `sandbox_unavailable`, `ConfigError` → `invalid_configuration`, `WorkspaceError` or
   `ScratchError` → `workspace_unusable`; the store's `NotDurableError` → `publish_incomplete`.
   That cause is how the job knows it renamed: the store raises it only when the rename into
   place succeeded and the flush of the directory holding the package failed, so the package at
   the destination is the job's, whole, and may not survive a crash. Any other failure happened
   before the job renamed anything, so if something is now at its destination, another writer
   took it → `destination_exists`; anything else `job_failed`. The SDK checks the call before it
   builds a
   job: the source exists and is a directory; nothing is at the destination, not even a dangling
   symlink; the destination is not inside the source (the next ingest of that root would read
   the package as evidence; the runtime does not check this); the adapters register; the options
   are `JobOptions` and their config names registered adapters with values `configure` accepts.
   `JobOptions` validates itself when built and raises the runtime's `JobError` there, outside
   any SDK call. A source's problems are never errors: they are findings (ADR 0028 §10).
7. **Sources, remote sources and remote execution.** A source is a path or a `file:` URI naming
   this host (empty or `localhost`, no query or fragment), percent-decoded to bytes and then to a
   path. Any other scheme (`s3://`, `https://`, …) asks the workspace for the network first
   (`require_network`): `network_refused` while it is local-only, then `unsupported` until a
   connector exists (MVL-45, MVL-46). Execution is local by default. `remote=<http(s) URL>`
   names a Neptune service to run jobs on: validated, then the network check, then
   `unsupported` until MVL-46's service exists. The parameter exists now so the surface does not
   change when the service lands; local is the only mode that runs in this version.
8. **Adapter overrides are the registry and the config, nothing more.** `adapters` replaces the
   set a job selects from (a registry, or any iterable: `[*builtin_adapters(), Mine()]`), and
   `JobOptions.config` sets each adapter's options by id. Selection stays the probe engine's
   rule (ADR 0024 §7, ADR 0033 §1); pinning an adapter to a source past the probe is the
   manifest's (MVL-14). The SDK adds no selection logic.
9. **No clock, no randomness, no default destination, no history.** The SDK reads no time and
   draws no random number; a job's id comes from `JobOptions.job` or the runtime, and lives only
   in the envelope. A destination is always the caller's: no default derived from the working
   directory, the root or the time. The same sources, adapters and config give the same receipt
   id and package id through either client, from any workspace state and from another root
   holding the same bytes.
   - **A package lists only what its own job's scan observed.** Its ledger records are a new
     ledger that observed each location as this job's walk found it: the artifacts the job read
     the bytes as, one revision per location holding bytes, each the first of its chain, and no
     absences. The workspace's ledger keeps the root's whole history (revisions superseding
     revisions, absences, artifacts no location holds now) for resume and the cache, exactly as
     ADR 0026 §1 and ADR 0031 §3 define; none of it is hashed into a package or its receipt.
   - So a dry run, a cancelled or killed ingest, or an earlier ingest of other bytes at the same
     paths never changes what a later ingest writes: it is the package a fresh workspace writes
     from the folder as it is now. History across packages is read by comparing their receipts;
     within a job, the `source_hashed` (`new_revision`) and `source_absent` events and the cache
     report's `source_changed` rule still say what changed since the workspace last looked.

## Alternatives considered

- **An async-native runtime** (async phases, async adapters). A rewrite of the job for no gain:
  each adapter call is a blocking fork and wait, and hashing is CPU and disk bound.
- **`asyncio.to_thread(job.run)` alone.** Awaitable, but no event stream, and cancelling the
  task would abandon a job still writing the workspace.
- **Running async jobs in a process pool.** Adapters would have to pickle (test adapters do
  not), and events would need a second wire format; the sandbox already isolates parsers.
- **A dry run composed in the SDK** from discovery, the probe engine and `plan`. A second job,
  which is the ingestion logic the SDK must not hold, and it would drift from the real one.
- **A dry run by cancelling the job when `plan` finishes.** No runtime change, but the outcome
  would say `cancelled` when nothing was cancelled, and it would depend on where checkpoints sit.
- **A placeholder destination for dry runs** (a path in the workspace that is never written).
  Keeps `JobOutcome.destination` a `Path`, at the cost of an outcome that names a place nothing
  was going to be written.
- **The SDK's own options type** (attempts, isolation, limits as keyword arguments). A second
  configuration model that drifts from `JobOptions`; it would only buy SDK-typed errors for a
  value the runtime already validates.
- **Classifying `JobError` by its message.** Breaks on the first rewording.
- **Subclassing `JobError` across the runtime** for every failure. Broader runtime churn for the
  same classes the causes already give.
- **A default destination** (`./<root>.neptune`, a sibling of the root, a `packages/` folder in
  the workspace). The working directory may be inside the root, the root's parent may be
  read-only, and the workspace is a cache that `collect` prunes: each would put a package
  somewhere the caller did not choose.
- **Packages that list the root's whole ledger** (ADR 0026 §4 as first built; the M1 review's
  O2). A package would then depend on which dry runs, cancelled ingests and earlier jobs saw the
  root: a dry run before an edit, or a deleted file, gives a package that no fresh ingest of the
  same folder reproduces. Revisions re-derived without their `supersedes` keep every location and
  content id the history would, and only drop the chain, which the workspace keeps.
- **Ledger records of this scan copied from the workspace's ledger as they are.** A revision's id
  hashes what it supersedes, so a changed file's revision would still carry the workspace's
  history into the package.
- **Re-exporting the SDK from `neptune`.** `import neptune.model` would then import the
  runtime, the sandbox and pyarrow, against the package layering.
- **Leaving out `remote` until MVL-46.** The constructor's signature would change when it lands.
- **Adapter findings in `IngestResult.findings`** by reading the package after every job. A cost
  proportional to the package on every call; `read_receipt` is explicit and reads one file.

## Consequences

- MVL-11's CLI is a wrapper over `Neptune`: events become progress lines, `NeptuneError.code`
  becomes the exit code, `IngestResult` the JSON output.
- MVL-15 extends `IngestJob.dry_run` (adapters' `inspect`, grouping) and renders it; the SDK's
  `dry_run` returns whatever it adds. MVL-45 and MVL-46 fill in URI schemes and `remote`
  behind the checks that exist now.
- A package records no absence and no revision chain (§9): what was deleted or replaced since
  the workspace last looked is in the job's events and cache report, and across packages in their
  receipts. Tests that read a chain read the workspace's ledger.
- A source is a directory: a single file is `invalid_source`, with a message saying to ingest
  its folder. Ingesting one file alone needs a walk over a single entry, a runtime change.
- Each async or `start`ed job holds one thread for its life; many concurrent jobs in one process
  are many threads, each forking per call. M9's scheduler revisits concurrency.
- Revisit if adapters become async-native, if one process must run many jobs at once, or if the
  CLI or the HTTP API needs a code the taxonomy lacks (add a subclass; never reuse a code).

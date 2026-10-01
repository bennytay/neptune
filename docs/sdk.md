# Python SDK

Status: MVL-12, ADR 0035. `neptune.sdk` is how code ingests: a thin, typed facade over the runtime's
`IngestJob`. Same sandbox, same workspace, same package as any other way in.

## The whole integration

```python
from neptune.sdk import Neptune

result = Neptune().ingest("runs/2026-09-30", "packages/2026-09-30")
assert result.committed
print(result.package, result.receipt)
```

`tests/fixtures/sdk/sdk_consumer.py` is a worked consumer (dry run, async, options, errors), run and
`mypy --strict`-checked by the tests.

- **Source**: a folder or one regular file, as a path or a `file:` URI. A file ingests exactly as a
  folder holding only it would (ADR 0043).
- **Ignore rules**: `JobOptions(ignore=IgnorePolicy(...))`. By default version-control internals and
  OS metadata (`DEFAULT_PATTERNS`) and the root's `.neptune-ignore` are left unread; each ignored
  entry is a `neptune.discovery.ignored` finding. A refused pattern, or a `.neptune-ignore` that cannot
  be used, is `invalid_configuration` (ADR 0043 §6–§7).
- **Destination**: required, must not exist, must not be inside the source. Packages are written once.
- **Workspace**: `Neptune(workspace)` takes a `Workspace`, a directory, or `None` for
  `$NEPTUNE_HOME`, else `$XDG_CACHE_HOME/neptune`, else `~/.cache/neptune`. It is the cache: run the
  same ingest again (into a new destination) and every unchanged chunk is reused.

## One surface, sync or async

| `Neptune` | `AsyncNeptune` | Does |
|---|---|---|
| `ingest(source, destination, *, on_event, cancel, resume)` | `await ingest(...)` | the whole job → `IngestResult` |
| `dry_run(source, *, on_event, cancel, resume)` | `await dry_run(...)` | discover → plan, nothing parsed → `planned` |
| `start(source, destination, *, cancel, resume)` | `start(...)` | the job on its own thread → `Ingestion` / `AsyncIngestion` |
| `start_dry_run(source, *, cancel, resume)` | `start_dry_run(...)` | the dry run on its own thread |

Both take `Neptune(workspace=None, *, adapters=None, options=None, remote=None)`. Shorthands:
`neptune.sdk.ingest(source, destination, workspace=..., ...)` and `neptune.sdk.dry_run(...)`.

```python
from neptune.sdk import AsyncNeptune, IngestResult

async def ingest_with_progress() -> IngestResult:
    run = AsyncNeptune().start("runs/today", "packages/today")
    async for event in run:                 # JobEvent(kind, phase, details), in order
        print(event.phase, event.kind)
    return await run.result()
```

- Sync `ingest`/`dry_run` run on your thread; `on_event` is called there.
- Async calls and `start` run the same job on a worker thread (`neptune-ingest`); async `on_event`
  runs on the event loop's thread. `start` must be called inside a running loop for `AsyncNeptune`.
- Events are the runtime's, unchanged (kinds in `neptune.runtime.events`, meanings in
  `ingestion-pipeline.md`). They carry no clock: add your own.

## Cancel and resume

- `cancel=threading.Event()`: set it and the job stops at its next checkpoint (the chunk in hand
  commits); the result is `cancelled`, no package. `Ingestion.cancel()` / `AsyncIngestion.cancel()`
  do the same.
- Cancelling the task awaiting `AsyncNeptune.ingest` cancels the job, **waits** for it to stop, then
  raises `CancelledError`. An exception from `on_event` stops the job and propagates.
- The last checkpoint is the start of `commit`. An interruption after it (task cancelled, `on_event`
  raising, a `with` block left by an exception) waits for the publish, then propagates carrying the
  result: `committed_result(error)` returns the committed `IngestResult`, or `None` if no package was
  written. Check it before retrying into the same destination.
- Resume = call `ingest` again with the same source and workspace. Killed processes too: the
  workspace is the checkpoint (ADR 0028 §2). `resume=True` insists on it: `nothing_to_resume` if
  the workspace never scanned this source. Either way the package is the one a fresh workspace
  writes (ADR 0035 §9).
- `publish_incomplete` is not committed, so `committed_result` returns `None` for it;
  `error.destination` names the package that is there.

## Results

`IngestResult` wraps the runtime's `JobOutcome` (`result.outcome`):

| Field | Meaning |
|---|---|
| `state`, `committed`, `cancelled`, `planned` | how the job ended |
| `package`, `receipt` | content id of the package, record id of its receipt; `None` without a package |
| `findings` | the job's own findings (discovery, probe, runtime): unsupported, ambiguous, quarantined |
| `read_receipt()` | the package's receipt, every adapter's findings included, checked against `receipt` |
| `read_package()` | the whole package, read and verified |
| `cache` | per source: adapter, plan and chunks with hit/miss rules; calls per adapter method |
| `ingested`, `job`, `destination`, `durations` | as in `JobOutcome` |

Same sources + adapters + config ⇒ same `receipt` and `package`, sync or async, cold or warm workspace,
whatever earlier dry runs or ingests saw: a package lists only its own job's scan (ADR 0035 §9).
A dry run's `cache` says what is left: chunks with rule `committed` are done, the rest will be parsed.

## Adapters and options

```python
from neptune.sdk import JobOptions, Neptune, builtin_adapters

client = Neptune(
    adapters=[*builtin_adapters(), MyAdapter()],              # the set a job selects from
    options=JobOptions(config={"text": {"block_rule": "line"}}, attempts=3),
)
```

`options` is the runtime's `JobOptions` (attempts, isolation, limits, per-adapter config, job name):
there is no SDK copy of it. Selection is still the probe engine's rule; pinning an adapter to a file
is the manifest's. A config change is a new lineage: new record ids, a new package.

## Manifests

Every `ingest`, `dry_run`, `start` and `start_dry_run` (sync and async, and the shorthands) takes
`manifest=`: `None` (the default) applies the root's `neptune.yaml`, `neptune.yml` or
`neptune.json` if it has one; a path names a manifest file inside the root; `False` applies none.
It is read and checked when the call is made, before the job starts: one that cannot be used is a
`ConfigurationError` (`invalid_configuration`). The read manifest reaches the job as
`JobOptions.manifest` (a `neptune.manifest.LoadedManifest`); setting it there directly skips the
lookup. With a manifest, `JobOptions.grouping` must stay default (runs and `gap_seconds` are the
manifest's) and its adapter options may not set a key `JobOptions.config` also sets.
`neptune.manifest.generate.generate(root, client)` is `neptune init-manifest` as a function.
See [manifest](manifest.md).

## Errors

Every SDK call raises only `NeptuneError` subclasses; branch on `error.code`, never the message.

| Code | Class | When |
|---|---|---|
| `invalid_source` | `InvalidSourceError` | missing, neither a directory nor a regular file, a `file:` URI with a query |
| `destination_exists` | `DestinationExistsError` | anything at the destination, a dangling symlink too |
| `invalid_destination` | `InvalidDestinationError` | the destination is inside the source |
| `invalid_configuration` | `ConfigurationError` | adapters conflict, config names an unknown adapter/option/value, bad `remote`, ignore rules that cannot be used |
| `nothing_to_resume` | `NothingToResumeError` | `resume=True` and the workspace holds no earlier work on the source |
| `network_refused` | `NetworkRefusedError` | a remote source or `remote=` while the workspace is local-only |
| `unsupported` | `UnsupportedError` | a URI scheme with no connector; remote execution (MVL-46) |
| `sandbox_unavailable` | `SandboxUnavailableError` | the host cannot confine adapters (ADR 0030 §3) |
| `workspace_unusable` | `WorkspaceUnusableError` | cannot open, lock or sweep the workspace, or read or write what a job keeps there (ledger, plans, chunks, runs, derivatives, scratch) |
| `package_invalid` | `PackageInvalidError` | a package or receipt that does not verify |
| `job_failed` | `JobFailedError` | anything else that stops the job: an unreadable root; a package that cannot be assembled, verified or written beside its destination |
| `publish_incomplete` | `PublishIncompleteError` | the job renamed its package into place but could not flush its directory: the package is there, whole, and may not survive a crash |

`invalid_request` (`InvalidRequestError`) is the parent of the first five, `job_failed` of
`publish_incomplete`, and `error` (`NeptuneError`) of all. A corrupt or unsupported *file* is never an error: it is a finding.
`JobOptions(...)` with a bad value raises the runtime's `JobError` (re-exported as `neptune.sdk.JobError`) when you build it.

## Local-only and remote

New workspaces are local-only: `s3://…` sources and `remote="https://…"` raise `network_refused`.
`workspace.allow_network(True)` lifts it (remembered); this version then raises `unsupported`, since
no connector (MVL-45) or service (MVL-46) exists yet.

## Gotchas

- The sandbox needs Linux with Landlock ABI ≥ 3. Elsewhere: `sandbox_unavailable`; choose
  `JobOptions(isolation=Isolation.IN_PROCESS)` (trusted adapters only) or `allow_degraded_sandbox`.
- Forking from a threaded process makes CPython emit a `DeprecationWarning` (hidden by default);
  it is expected (ADR 0030 §2).
- Iterate a handle from one thread. Leaving `with client.start(...) as run:` by an exception cancels.

# 0043 — `neptune ingest`: a thin CLI over the SDK, fixed exit codes, declared ignore rules and single-file sources

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-11
- Extends: ADR 0035 §6 and its Consequences (a source may be one regular file; a new
  `nothing_to_resume` code; `PublishIncompleteError.destination`), ADR 0009 §5 and ADR 0029 §1
  (the walk gains ignore rules, each ignored entry a finding)

## Context

MVL-11 asks for the zero-friction interface: `neptune ingest <path|uri>`, recursive discovery
with safe ignore rules, progress, a machine-readable mode, the receipt's location, deterministic
exit codes, resume and retry flags, and no manifest for the common case. Its acceptance is that a
developer points Neptune at a messy run folder and gets a useful result without reading setup
docs. The SDK (ADR 0035) already runs the job, classifies every failure by a stable code and
returns a typed result; ADR 0035 §1 says the CLI wraps it. Three things were missing below the
CLI: the walk read everything (a run folder copied off a robot through a laptop holds `.git/`,
`.DS_Store`, `._*` files), a single file was `invalid_source`, and nothing distinguished "resume
this" from "start fresh".

## Decision

1. **The CLI is the SDK, called once.** `neptune.cli` parses the command line into a `Neptune`
   client (`workspace`, `JobOptions`) and calls `ingest` or `dry_run`. It turns events into
   progress, `NeptuneError.code` into the exit code and `IngestResult` into the result; it
   decides nothing about what is ingested. It imports the SDK, and the store only for the
   receipt's file name; never the runtime. Console script `neptune = "neptune.cli:main"`, and
   `python -m neptune.cli`.
2. **Exit codes are fixed per SDK code** (`neptune.cli.exit_codes.BY_CODE`) and never renumbered;
   a new SDK code takes the next free number: 0 committed or planned; 1 internal (a bug: an
   exception that is not a `NeptuneError`, or the base `error`); 2 usage (argparse, or
   `invalid_request`); 3 `invalid_source`; 4 `invalid_destination`; 5 `destination_exists`;
   6 `invalid_configuration`; 7 `nothing_to_resume`; 8 `unsupported`; 9 `network_refused`;
   10 `sandbox_unavailable`; 11 `workspace_unusable`; 12 `package_invalid`; 13 `job_failed`;
   14 `publish_incomplete`; 130 cancelled at a checkpoint (Ctrl-C). A source's problems are
   findings, so a committed package with error findings exits 0 (partial success).
3. **Output.** Human output is quiet: a committed job prints four lines on stdout (destination,
   package id, receipt path, a sources and findings summary); errors are one
   `neptune: <code>: <message>` line on stderr; `-v` adds one line per job event on stderr.
   `--json` makes stdout JSON Lines, sorted keys and no whitespace: one `{"type": "event"}` line
   per job event as it happens, then exactly one `{"type": "result"}` line, always last, with
   the same keys whatever happened (`null` where a field does not apply). The result carries no
   clock, job token, host or workspace path, and paths as the user typed them, so the same
   command over the same bytes from a fresh workspace prints byte-identical output. Its findings
   are counts by code and severity, from the receipt when committed (adapters' included) and the
   job's own otherwise; the full findings are in the receipt it names.
4. **Ctrl-C cancels at a checkpoint.** The first SIGINT sets the job's cancel event (ADR 0028 §6):
   the job commits the chunk in hand, ends `cancelled`, exits 130 and says to rerun with
   `--resume`. A second SIGINT aborts at once; if the job had already published, the result is
   the committed one (`committed_result`, ADR 0035 §3).
5. **`--resume` insists; a plain rerun reuses anyway.** A rerun always reuses what the workspace
   holds (ADR 0031). `resume=True` on the SDK (and `--resume`) additionally requires that the
   workspace saved a ledger of this root (an ingest, a dry run, or a job interrupted after its
   scan): otherwise `NothingToResumeError` (`nothing_to_resume`, an `invalid_request`), so a
   typo in the path or the workspace is not silently a fresh start. Resume never changes the
   package: ADR 0035 §9 holds, a package lists only what its own job's scan observed.
   `--attempts N` is the retry knob (`JobOptions.attempts`).
6. **Ignore rules are discovery's, declared, never silent** (`neptune.discovery.ignore`,
   `JobOptions.ignore: IgnorePolicy`). In order: `DEFAULT_PATTERNS` (version-control internals,
   OS metadata), the caller's patterns (`--ignore`), then the root's `.neptune-ignore`. The
   syntax is a byte-wise gitignore subset (`*`, `?`, `[...]`, `**`, a trailing `/` for
   directories, a `/` anchoring at the root); negation is refused, so a rule only ever leaves
   things out. The walk yields an ignored entry as a `SkippedEntry` with reason `ignored` and
   the rule, and does not enter an ignored directory; an entry the walk could not examine keeps
   its own reason. The scan records one `neptune.discovery.ignored` finding (skipped, info) per
   ignored entry, under the `neptune.discovery.ignore` transform whose config is every rule in
   force; nothing at or below it is asserted absent. Rules that match nothing produce no finding
   and so no transform: the package is the one a job without rules writes.
7. **`.neptune-ignore` is hostile input and all-or-nothing.** It is read through the walk's own
   safe `open` (never through a symlink, regular files only), at most 64 KiB and 256 rules in
   all. One that cannot be used (unreadable, a symlink, a directory, too large, a refused line) is
   an `IgnoreError`, which the SDK maps to `invalid_configuration` before anything is walked:
   rules meant to exclude something are never half-applied. The file itself is evidence, walked
   and hashed. A root directory that cannot be opened is the root's failure (`job_failed`), not
   the file's.
8. **A single regular file is a source.** `LocalSource` over a file walks one entry located by
   the file's own name (the last component of the path it resolves to; the caller chose the root,
   so it is followed as a root directory is), and opens nothing else. It ingests exactly as a
   folder holding only that file: the same package id. Ignore rules and `.neptune-ignore` do not
   apply to it.
9. **`PublishIncompleteError.destination`** names the package the job renamed into place before
   the flush failed. `committed_result` returns `None` for it (the job did not commit), so this
   is how a caller, and the CLI's result (`destination`), finds the package.

## Alternatives considered

- **Exit codes by class only** (0, 1 for findings, 2 for errors). A script could not tell a
  taken destination from a missing sandbox without parsing text, which ADR 0035 §6 forbids.
- **A nonzero exit when the receipt holds error findings.** Contradicts partial success: one
  corrupt artifact would fail every pipeline that ingests a real robot's folder. A future
  `--strict` can add it as an opt-in.
- **JSON as one document at the end.** No progress for a long job; JSON Lines lets a consumer
  follow events and still find the result as the last line.
- **Canonical JSON for the result line.** Canonical JSON forbids `null` (ADR 0004), and a result
  with stable keys needs "does not apply"; sorted-key compact JSON is as deterministic.
- **Including the job token and durations in the result.** Both are volatile; they live in the
  package's `volatile/` envelope and the events.
- **Ignore rules in the CLI** (filter paths before the SDK). The SDK and every other caller would
  read what the CLI skips, and nothing in the package would say what was left out.
- **Silently skipping VCS and OS debris.** Violates "a blank never becomes a fact": the receipt
  must say what was not read and why.
- **Gitignore negation.** Makes "is this excluded" depend on rule order across three origins;
  leaving things out is all a run folder needs.
- **Half-applying a broken `.neptune-ignore`** (skip bad lines with a finding). The rules exist to
  keep something out of a package; applying some of them could publish what the user meant to
  exclude.
- **A single file located by its absolute path, or as `.`.** Absolute paths never enter records;
  `.` would make a file's package differ from its folder's.
- **`--resume` as a no-op alias** for a rerun. A mistyped workspace or path would start over
  silently, re-parsing everything.
- **`--local-only`, `--allow-network` and `--adapters` flags now.** No connector or plugin
  mechanism exists yet (MVL-45, MVL-46, MVL-14); `allow_network` persists a workspace setting,
  which a one-off flag should not do. They come with the features they enable.

## Consequences

- `neptune ingest <folder|file> --out <dest>` is the one command a robotics developer needs;
  `docs/cli.md` is its reference and `--help` carries the exit-code table.
- ADR 0035's "a single file is `invalid_source`" no longer holds; a source is a directory or a
  regular file. `invalid_source` is now a missing path, a non-regular file, or a bad URI.
- Packages from roots holding VCS or OS debris now hold `neptune.discovery.ignored` findings and
  the ignore transform; packages of clean roots are unchanged.
- The exit-code table is a compatibility contract with the same rule as the SDK codes.
- Revisit when connectors (MVL-45/46) need `--allow-network`, when the manifest (MVL-14) pins
  adapters, or if users need negation (a new ADR, never an in-place edit).

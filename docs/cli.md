# `neptune ingest`

Status: MVL-11, ADR 0043. The command line is a thin layer over the [SDK](sdk.md): one `Neptune`
client, one `ingest` or `dry_run` call. Same job, same sandbox, same package.

```console
$ neptune ingest runs/2026-09-30 --out packages/2026-09-30
committed packages/2026-09-30
  package  sha256:93516a71…
  receipt  packages/2026-09-30/receipt.json
  sources  6 ingested; findings: 2 error, 7 info
```

No manifest and no setup: point it at a folder (or one file) and name where the package goes.
Every file is discovered, hashed, probed and parsed by the adapter that claims it. What Neptune
could not read, or chose not to, is a finding in the receipt, not a failure.

## Usage

```
neptune ingest SOURCE (--out DEST | --dry-run | --explain) [options]
python -m neptune.cli ingest ...        # the same command
```

| Option | Does |
|---|---|
| `SOURCE` | a folder or one regular file; a path or a `file:` URI. Other schemes (`s3://`, …) are refused while the workspace is local-only (exit 9) |
| `-o, --out DEST` | where the package is written. Must not exist and must not be inside the source. Required unless `--dry-run` |
| `-n, --dry-run` | discover, fingerprint, probe and plan only; no package. The plans are kept, so the ingest that follows plans nothing again |
| `--explain` | a dry run that also prints its explanation (ADR 0044): every file, each adapter's verdict and why, the proposed sessions, the work left, what would be left out. Implies `--dry-run`; `--out` with it is a usage error (exit 2) |
| `--resume` | continue earlier work on this source in the workspace (an interrupted ingest or a dry run); exit 7 if there is none. A plain rerun reuses that work too; `--resume` only refuses to start from nothing |
| `--attempts N` | tries per adapter call before its source is quarantined (default 2) |
| `-w, --workspace DIR` | the workspace (cache and checkpoints). Default `$NEPTUNE_HOME`, else `$XDG_CACHE_HOME/neptune`, else `~/.cache/neptune` |
| `--ignore PATTERN` | leave matching entries unread; repeatable |
| `--no-default-ignores` | read version-control internals and OS metadata too |
| `--no-ignore-file` | do not apply the source's `.neptune-ignore` |
| `--isolation {subprocess,in_process}` | adapters run in a confined child process per call (default) or, for trusted adapters only, in this process |
| `--allow-degraded-sandbox` | run where not every sandbox guarantee is available; the receipt records which were lost |
| `--job NAME` | the job's name in the package's envelope (`volatile/`); default a random token |
| `--json` | JSON Lines on stdout (below) |
| `-v, --verbose` | one progress line per job event on stderr |

## Ignore rules

Applied in order; the first match names the entry. Nothing ignored is silent: each ignored entry
is one `neptune.discovery.ignored` finding (severity info) naming the rule and where it came
from, and an ignored directory is not entered.

1. Defaults: `.git/ .hg/ .svn/ .DS_Store ._* .Spotlight-V100/ .Trashes/ .fseventsd/
   .TemporaryItems/ .Trash-*/ Thumbs.db desktop.ini $RECYCLE.BIN/ System Volume Information/`.
2. `--ignore` patterns.
3. The source root's `.neptune-ignore`, one pattern per line, `#` comments.

Syntax (a gitignore subset, matched on bytes, case-sensitive): `*`, `?` and `[...]` within one
name; a component that is exactly `**` spans any number of names; a trailing `/` matches only
directories; a pattern containing `/` (other than trailing) is anchored at the root, otherwise it
matches a name at any depth. Negation (`!`) is refused.

`.neptune-ignore` is untrusted input. A symlink, a directory, more than 64 KiB, more than 256
rules in all, or one refused line fails the run with exit 6 before anything is read: rules are
never half-applied. The file itself is ingested as evidence. A single-file source has no ignore
rules.

## Output

Quiet by default: on success, the four lines above on stdout and nothing on stderr. A dry run
prints `planned SOURCE: N sources to ingest; nothing written` and a findings summary; with
`--explain`, a blank line and the rendered explanation follow. Errors are
one line on stderr: `neptune: <code>: <message>`.

With `--json`, stdout is JSON Lines (UTF-8, sorted keys, no whitespace):

- one line per job event as it happens: `{"event":{"details":{…},"kind":"…","phase":"…"},"type":"event"}`
  (kinds and details: [ingestion-pipeline.md](ingestion-pipeline.md));
- with `--explain`, one `{"explanation":{…},"type":"explanation"}` line just before the result: the
  explanation's canonical `dumps()` bytes (`neptune.explanation/1`), byte-identical from a fresh
  workspace over the same bytes;
- then exactly one result line, always last, always with these keys (`null` where one does not
  apply):

| Key | Value |
|---|---|
| `type` | `"result"` |
| `format` | `1`; bumped only by a breaking change |
| `state` | `committed`, `planned`, `cancelled` or `failed` |
| `exit_code` | the process's exit code |
| `source` | `SOURCE` as given |
| `destination` | the package's path as given; for `publish_incomplete`, where the package is |
| `package` | the package's content id (`sha256:…`) |
| `receipt` | the receipt's record id (`rec:sha256:…`) |
| `receipt_path` | `DEST/receipt.json` |
| `sources` | sources ingested (committed) or planned (dry run, cancelled) |
| `records` | records in the package by kind |
| `findings` | `{"by_code": {…}, "by_severity": {…}, "total": n}`: the receipt's findings when committed (adapters' included), otherwise the job's own |
| `cache` | chunk, plan and derivative hits and misses |
| `error` | `{"code": "<SDK code>", "message": "…"}` when `failed`; `code` is `internal` for a bug |

The result has no clock, job token, host or workspace path, so the same command over the same
bytes from a fresh workspace prints byte-identical lines. (Cache hits differ on a rerun in the
same workspace, by design.) Usage errors (exit 2) are argparse's text on stderr, never JSON.

## Exit codes

Fixed forever; a new SDK error code takes the next free number (ADR 0043 §2).

| Code | Meaning |
|---|---|
| 0 | committed, or a dry run planned. Error *findings* still exit 0: partial success |
| 1 | internal error: a bug (traceback on stderr) |
| 2 | usage: the command line is wrong (`invalid_request`) |
| 3 | `invalid_source`: missing, a symlink loop, neither a folder nor a regular file, a bad URI |
| 4 | `invalid_destination`: inside the source |
| 5 | `destination_exists`: a package is written once |
| 6 | `invalid_configuration`: a refused `--ignore` pattern or `.neptune-ignore` |
| 7 | `nothing_to_resume`: `--resume`, and the workspace never scanned this source |
| 8 | `unsupported`: a scheme or mode this version does not have |
| 9 | `network_refused`: a remote source while the workspace is local-only |
| 10 | `sandbox_unavailable`: this host cannot confine adapters (see `--allow-degraded-sandbox`) |
| 11 | `workspace_unusable`: the workspace cannot be opened, locked, read or written |
| 12 | `package_invalid`: the written package or receipt does not verify |
| 13 | `job_failed`: the job could not proceed (an unreadable root, a package that cannot be written) |
| 14 | `publish_incomplete`: the package is at its destination, whole, but not yet durable |
| 130 | cancelled at a checkpoint (Ctrl-C): rerun with `--resume` |

## Interrupting and resuming

Ctrl-C asks the job to stop at its next checkpoint: the chunk in hand commits, the command exits
130, and the workspace keeps everything done so far. `neptune ingest SOURCE --out DEST --resume`
continues from there; the package is the one a fresh workspace would write. A second Ctrl-C aborts
at once. A killed process resumes the same way: the workspace is the checkpoint.

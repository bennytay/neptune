# 0044 — Explain: the dry run inspects and returns a bounded, typed explanation

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-15
- Extends: ADR 0035 §4 (the dry run gains adapters' `inspect` and the explanation, as §4
  foresaw; it still saves the ledger and plans), ADR 0043 (`neptune ingest --explain`)

## Context

MVL-15 asks that the planner be inspectable before expensive work starts: the source inventory,
detected formats, proposed adapters, proposed run/session grouping, estimated work and heavy
transforms, ambiguities and unsupported artifacts, and the reason for each adapter and grouping
choice. Its acceptance is that a developer understands what Neptune intends to do **without
modifying data or running full parses**.

Most of it already exists. `IngestJob.dry_run` (ADR 0035 §4) runs `discover`, `fingerprint`,
`inspect` and `plan` and stops `planned`; the probe engine keeps every adapter's result and the
selection over them (ADR 0027); grouping runs at the end of `inspect` so a dry run sees it (ADR
0036 §9); the plan phase knows each chunk's cost and whether the workspace holds it (ADR 0031).
What was missing: the job threw the probe and the layout away once it had chosen, and no
adapter's `inspect` was ever called. The data MVL-15 must not modify is the source data. The
workspace is Neptune's own cache, and no package id depends on it (ADR 0035 §9). A dry run already
warms it (ledger and plans), and ADR 0043 §5's `--resume` treats that as resumable work.

## Decision

1. **Explain is the dry run, extended; there is no second mode.** `IngestJob.dry_run()` builds a
   `neptune.runtime.explain.Explanation` and returns it on `JobOutcome.explanation` (`None` for any
   other job, and for a cancelled dry run); the SDK reads it as `IngestResult.explanation`, through
   `Neptune.dry_run`, `AsyncNeptune.dry_run`, `start_dry_run` and the `dry_run` shorthand alike.
   `Explanation` is re-exported from `neptune.runtime` and `neptune.sdk`.
2. **Sources are only read; the cache is warmed as before.** The dry run saves the ledger and the
   plans it makes, exactly as ADR 0035 §4 says, so the ingest after it plans nothing again and
   `--resume` continues it. It writes no chunk, derivative or package, and never writes under the
   root. So explaining never changes a later package (ADR 0035 §9). An explanation is a function
   of what the workspace holds: the first explanation in a fresh workspace says `source_new`, and
   a later one says `planned` (reused).
3. **Probe, inspect and plan only.** For each selected source the dry run calls the adapter's
   `inspect` once, through the runner like every adapter call (sandboxed, with a reader and
   scratch). Its summary and findings are the explanation's alone: they never enter the job's
   findings, since a run never calls `inspect` and finds them again (ADR 0024). An `inspect` that
   raises, crashes or hits a limit is shown as a failure (`error`, or the runner's cause), and the
   source is still planned. A source it sees change, or read short while the source no longer
   matches its artifact, is the source's problem and is quarantined. That is the same judgement
   `plan` gets (ADR 0033 §3). `ingest` is never called: `JobOutcome.cache.calls.ingest` is 0 and
   the tests count it. `plan` is the adapter's planning pass. It may read the source to find chunk
   boundaries, but it is not a parse: no record is decoded or emitted.
4. **The explanation's content**, every list in a fixed order:
   - `inventory`: every location holding bytes with its source and size, every symlink with its
     target bytes (recorded, never followed), every entry the walk or the fingerprint skipped with
     its reason; totals in bytes and distinct sources.
   - `sources`, by first location:
     - its status (`planned`, `quarantined`, `ambiguous`, `unsupported`, `unreadable`: the last
       means it could not be read to probe), and every location holding the bytes;
     - the detected format: the sniff, plus a container's listing when the probe opened one;
     - one **verdict per registered adapter**: `selected`, `tied`, `outranked`, `declined`
       (confidence 0) or `failed` (its probe raised, crashed or hit a limit). Each verdict carries
       its confidence, the reasons its probe gave, the format version it read, and one line saying
       why it won or lost;
     - the `inspect` summary or failure;
     - the plan: transform, cache rule, chunks, committed chunks, total cost and bytes left to read.
   - `adapters`: each registered adapter with the sources it was selected for.
   - `grouping`: the grouping transform; its proposals, most confident first, each with its rule,
     band, members, reasons and contested peers; and the unassigned files (ADR 0036 §7). They are
     inferred, and `render` says so.
   - `work`: planned sources, sources with chunks left, chunks, committed chunks, bytes left to
     read, the least number of `ingest` calls a run makes, and the adapter calls the dry run made.
   - `heavy` (§5), `left_out` (§6), `ambiguities` and `findings`:
     - `ambiguities` are the ids of every finding of category `ambiguous`: tied adapters,
       contested sessions, files several sessions could hold;
     - `findings` are every finding the dry run made, by id.
5. **Heavy is a fixed rule over what is left to parse**, never a guess about time. A source is
   heavy when:
   - its uncommitted chunks read at least 256 MiB (`large_input`);
   - its uncommitted chunks number at least 1024, each a sandboxed fork (`many_chunks`);
   - its adapter does not stream and the source is larger than the memory the adapter declares
     (`memory_grows`);
   - the adapter's declared memory exceeds the sandbox's memory limit (`memory_limit`; not when
     adapters run in process).

   A source with nothing left to parse is never heavy.
6. **`left_out` names everything a run would not ingest**, by location, with a disposition and
   one line:
   - `unsupported`: no adapter claims it. For a container, the line says containers are listed,
     never extracted.
   - `ambiguous`: names the tied adapters; declare one in a manifest.
   - `quarantined` and `unreadable`: the finding codes.
   - `skipped`: the walk's reason.
   - `link`.
7. **Deterministic and JSON-serialisable.** `to_json()` holds canonical JSON values only. There is
   no `null`: an absent value is an absent key. `dumps()` is its canonical bytes, under schema
   `neptune.explanation/1`. It carries no job id, clock, duration, host or absolute path, so the
   same root bytes and names, adapters, config and workspace contents give the same bytes from any
   path. `render()` prints the same content for people, one line per entry: control characters and
   bytes that are not UTF-8 in names print as `\xNN` escapes, since names are hostile. It
   abridges long `inspect` summaries.
   Nothing in an explanation enters a package.
8. **Bounded.** `Bounds` (`DEFAULT_BOUNDS`) caps what one explanation holds. Every list keeps at
   most 10,000 entries: inventory files, links and skipped entries; sources; left-out locations;
   session proposals; unassigned files; findings; and each adapter's selected sources. Within one
   source the caps are:
   - 64 locations;
   - 16 reasons per verdict;
   - 256 container members;
   - an `inspect` summary of 16 KiB of canonical JSON (a larger one is dropped, and its size kept
     as `summary_omitted_bytes`);
   - 64 `inspect` findings.

   Each session proposal is cut to 64 members, links, contested peers, included proposals and
   reasons; its record keeps its id, and the cuts are counted beside each list.

   Each cut is an explicit `*_omitted` count beside its list. Each bound that cut anything adds one
   `neptune.explain.truncated` finding (limit, info), whose producer is `neptune.explain` 0.1.0
   with the bounds as its config. The finding names the list, how much it kept and omitted, the
   bound, and as its subject the first thing cut. These findings are always listed, beyond the
   findings bound. Totals (inventory bytes and sources, the grouping summary, `work`) are always
   over everything. So an explanation of a tree of any size, or one from an adapter running in
   process with an unbounded summary, has a size bounded by these counts times the size of one
   entry.
9. **The CLI** (ADR 0043) gains `neptune ingest SOURCE --explain`, which implies `--dry-run`
   (`--out` with it is a usage error, exit 2) and has the same exit codes otherwise.
   - Human output: the planned lines, then the rendered explanation.
   - With `--json`: one `{"explanation": …, "type": "explanation"}` line just before the result
     line, holding exactly `dumps()`. The result line and its keys are unchanged.

## Alternatives considered

- **A separate `explain` mode beside the dry run.** Two nearly identical jobs; the CLI's dry run,
  resume and explain would then disagree about what they leave in the workspace.
- **A dry run that keeps nothing in the workspace.** Tried first, then rejected. `--dry-run` then
  `--resume` (ADR 0043 §5) would find nothing to resume, and the ingest after it would plan
  again. That keeps no source safer, since the sources are only ever read.
- **No `plan` in the explanation**, estimating work from `inspect` summaries instead. Summaries
  are each adapter's own vocabulary; chunk counts and costs are the contract's. Planning is also
  the job's real next step, so the estimate is what the run will do.
- **Record `inspect` findings as job findings.** They would appear only in dry runs, so the same
  folder would report different findings depending on how it was looked at.
- **Heavy by predicted duration.** Durations depend on the host; byte, chunk and memory thresholds
  are deterministic and say why.
- **An unbounded explanation, or one capped by total bytes.** Unbounded, a million-file tree or an
  in-process adapter's summary could exhaust the caller. A byte cap would cut mid-list at a point
  that depends on encodings; per-list counts say exactly what is missing.
- **Build the explanation in the SDK or in `cli/`.** The SDK holds no ingestion logic (ADR 0035
  §1); the CLI only prints what the SDK returns.

## Consequences

- `JobOutcome` and `IngestResult` grow one field, and the CLI one flag and one JSON line type. The
  cache report, events, phases, the CLI's result line and packages are unchanged, so no package
  id changes.
- Two explanations of one folder differ if the workspace changed between them (rules, committed
  chunks), by design; from fresh workspaces they are byte-identical.
- Revisit if the explanation is needed for cancelled dry runs (a partial explanation), if the
  bounds prove too tight for real fleets (a new `Bounds`, recorded in the transform), or if M9's
  scheduler needs time estimates the fixed thresholds cannot give.

# 0044 — Explain: the dry run inspects, keeps nothing, and returns a typed explanation

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-15
- Amends: ADR 0035 §4 (a dry run no longer saves the ledger or plans; it adds `inspect` and the
  explanation, as §4 foresaw)

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
Three things were missing: the job threw the probe and the layout away once it had chosen; no
adapter's `inspect` was ever called; and the dry run saved the root's ledger and every new plan
into the workspace, so "explain" changed the state the next job reads. MVL-11's CLI (#40) owns
`cli/` and wraps the SDK, so the capability must live in the runtime and the SDK.

## Decision

1. **Explain is the dry run, extended; there is no second mode.** `IngestJob.dry_run()` builds a
   `neptune.runtime.explain.Explanation` and returns it on `JobOutcome.explanation` (`None` for any
   other job, and for a cancelled dry run); the SDK reads it as `IngestResult.explanation`, through
   `Neptune.dry_run`, `AsyncNeptune.dry_run`, `start_dry_run` and the `dry_run` shorthand alike.
   `Explanation` is re-exported from `neptune.runtime` and `neptune.sdk`.
2. **A dry run keeps nothing.** The ledger is loaded and reconciled in memory, never saved; a new
   plan is made in memory, never saved; a saved plan is read and reused. No chunk, derivative or
   package is written, and the root is opened read-only as always. What a dry run still does to
   the workspace is what any job does before work: create the workspace's directories if it is new,
   take its shared lock, sweep debris that dead processes left (ADR 0033 §2), and give each
   sandboxed call scratch space that is removed when the call returns. So two dry runs over the
   same root and workspace give byte-identical explanations, and a dry run never changes what any
   later job sees, reuses or writes (ADR 0035 §9 holds trivially). This amends ADR 0035 §4: the
   ingest after a dry run plans again (one `plan` call per source) instead of reusing its plans.
3. **Probe, inspect and plan only.** For each selected source the dry run calls the adapter's
   `inspect` once, through the runner like every adapter call (sandboxed, with a reader and
   scratch). Its summary and findings are the explanation's alone: they never enter the job's
   findings, since a run never calls `inspect` and finds them again (ADR 0024). An `inspect` that
   raises, crashes or hits a limit is shown as a failure (`error`, or the runner's cause) and the
   source is still planned; one that sees the source change quarantines it, as any call does.
   `ingest` is never called: `JobOutcome.cache.calls.ingest` is 0 and the tests count it. `plan`
   is the adapter's planning pass (it may read the source to find chunk boundaries) and is not a
   parse: no record is decoded or emitted.
4. **The explanation's content**, every list in a fixed order:
   - `inventory`: every location holding bytes with its source and size, every symlink with its
     target bytes (recorded, never followed), every entry the walk or the fingerprint skipped with
     its reason; totals in bytes and distinct sources.
   - `sources`, by first location: status (`planned`, `quarantined`, `ambiguous`, `unsupported`,
     `unreadable`), every location holding the bytes, the detected format (sniff, and a
     container's listing when the probe opened one), and one **verdict per registered adapter**:
     `selected`, `tied`, `outranked`, `declined` (confidence 0) or `failed` (its probe raised,
     crashed or hit a limit), with its confidence, the reasons its probe gave, the format version
     it read, and one line saying why it won or lost. Then the `inspect` summary or failure, and
     the plan: transform, cache rule, chunks, committed chunks, total cost and bytes left to read.
   - `adapters`: each registered adapter with the sources it was selected for.
   - `grouping`: the grouping transform, its proposals most confident first with their rules,
     bands, members, reasons and contested peers, and the unassigned files (ADR 0036 §7).
   - `work`: planned sources, sources with chunks left, chunks, committed chunks, bytes left to
     read, the least number of `ingest` calls a run makes, and the adapter calls the dry run made.
   - `heavy` (§5), `left_out` (§6), `ambiguities` (the ids of every finding of category
     `ambiguous`: tied adapters, contested sessions, files several sessions could hold) and
     `findings` (every finding the dry run made, by id).
5. **Heavy is a fixed rule over what is left to parse**, never a guess about time: a source is heavy
   when its uncommitted chunks read at least 256 MiB (`large_input`) or number at least 1024, each
   a sandboxed fork (`many_chunks`); when its adapter does not stream and the source is larger than
   the memory the adapter declares (`memory_grows`); or when that declared memory exceeds the
   sandbox's memory limit (`memory_limit`; not when adapters run in process). A source with nothing
   left to parse is never heavy.
6. **`left_out` names everything a run would not ingest**, by location, with a disposition and one
   line: `unsupported` (no adapter claims it; a container's line says containers are listed, never
   extracted), `ambiguous` (the tied adapters; declare one in a manifest), `quarantined` and
   `unreadable` (the finding codes), `skipped` (the walk's reason) and `link`.
7. **Deterministic and JSON-serialisable.** `to_json()` holds canonical JSON values only (no
   `null`: an absent value is an absent key); `dumps()` is its canonical bytes, schema
   `neptune.explanation/1`. It carries no job id, clock, duration, host or absolute path, so the
   same root bytes and names, adapters, config and workspace contents give the same bytes, from any
   path. `render()` prints the same content for people, abridging long `inspect` summaries.
   Nothing in an explanation enters a package.

## Alternatives considered

- **A separate `explain` mode beside the dry run** (one that keeps nothing, while `dry_run`
  keeps warming the workspace). Two nearly identical jobs whose results differ in which state they
  leave, for one saved `plan` call per source; the explanation would be wrong about the dry run the
  SDK already exposes.
- **Keep saving plans, stop saving only the ledger.** Plans for sources no saved ledger holds are
  exactly what `collect` removes (ADR 0031 §6), and the workspace would still change under a call
  documented as read-only.
- **No `plan` in the explanation**, estimating work from `inspect` summaries. Summaries are each
  adapter's own vocabulary; chunk counts and costs are the contract's, and planning is the job's
  real next step, so the estimate is what the run will do.
- **Record `inspect` findings as job findings.** They would appear only in dry runs, so the same
  folder would report different findings depending on how it was looked at.
- **Heavy by predicted duration.** Durations depend on the host; byte, chunk and memory thresholds
  are deterministic and say why.
- **Build the explanation in the SDK or in `cli/`.** The SDK holds no ingestion logic (ADR 0035
  §1), and MVL-11 owns `cli/`, which wraps the SDK.

## Consequences

- MVL-11's CLI prints `IngestResult.explanation.render()` or writes `dumps()` for `neptune ingest
  --explain` / `neptune explain`; nothing else is needed from the runtime.
- An ingest after a dry run pays one `plan` call per source again; the dry run's
  `cache` report still says what the workspace holds.
- `JobOutcome` and `IngestResult` grow one field; the cache report, events, phases and packages
  are unchanged, so no package id changes.
- Revisit if the explanation is needed for cancelled dry runs (a partial explanation), if users
  want explain to warm the workspace after all (an explicit option, recorded in the outcome), or if
  M9's scheduler needs time estimates the fixed thresholds cannot give.

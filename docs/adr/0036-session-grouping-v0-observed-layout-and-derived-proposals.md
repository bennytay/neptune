# 0036 — Session grouping v0: an observed layout, inferred proposals, derived tables in the package

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-13
- Amends: ADR 0023 §5 (derived tables in a package); settles ADR 0010's open record shape for
  symlinks as grouping consumes them

## Context

Robotics teams rarely package data consistently: rosbag2 directories beside notes, PX4's
`log/<date>/<time>.ulg`, rosbag1 `--split` parts, numbered episodes, flat dumps of media named by
start time, session folders nested in campaign folders, copies, `latest` links. MVL-13 asks for
run/session discovery from such trees without a manifest: confidence-ranked proposals, reasons,
explicit ambiguity findings and an override hook. Its acceptance is that multi-run trees are
grouped correctly or marked ambiguous, with no silent cross-run merge. The audit scopes v0 to
filesystem-level signals and a stable interface that MVL-34's evidence-graph assembler swaps in.

Earlier decisions constrain it. A `Run` is a session one piece of evidence declares (ADR 0018), so
a grouping across files is inferred and belongs in `derived/` (non-negotiable 8, ADR 0017 §5).
ADR 0023 §5 reserved a package's `derived/` and had readers refuse it until the derived layer's
schema landed. ADR 0010 left to grouping both what a symlink means and its record shape. The
package must stay byte-identical for the same bytes, adapters and config (non-negotiable 5).

## Decision

1. **The boundary.** What a tree shows is observed; what it means is inferred.
   - `neptune.discovery.layout` holds the observed layout: each location this scan saw holding
     bytes (with its `SourceRevision` id and content id), each link with its target as stored,
     and what names say by fixed grammars: a civil date-time (`2024-05-01_12-30-00`, rosbag2's
     `2024_05_01-12_30_00`, `20240501T123000`, validated as a calendar date and time), a trailing
     part number (`_3`, never the tail of a date or time), a session keyword with a number
     (`run_007`, `episode-3`, never a date's year), base, stem and extension. These are facts
     about names, carry no meaning, and are computed from bytes, so names that are not UTF-8 are
     read exactly.
   - `neptune.derived` holds everything grouping infers: the `Grouper` interface, v0's
     `LayoutGrouper`, and the derived record kinds. No grouping is ever a `Run`, and nothing in
     `model/` changes.
2. **Inputs: names and directories only.** No file contents, no probe verdicts (they depend on
   the registry) and **no modification times**: an mtime is host state that a copy, a checkout or
   an unzip rewrites, so a package built from it would differ for the same bytes. The proposals
   are therefore a function of the scan's locations, its links and the config, and recompute from
   a package's own revision table and symlink findings.
3. **v0's rules**, each named, each with a named confidence band (a ranking, not a probability).

   | Rule | Forms or places | Band |
   |---|---|---|
   | `declared` | a session the config declares (§6) | 1.0 |
   | `rosbag2_directory` | one recording: a directory with `metadata.yaml` beside `.db3`/`.mcap` | 0.9 |
   | `split_sequence` | one recording: parts `<prefix>_<n>` of one extension whose prefix states a start time (rosbag1 `--split`); missing indices are listed | 0.8 |
   | `recording_file` | one recording: any other `.mcap`, `.bag`, `.ulg`, `.db3` | 0.7 |
   | `session_directory` | a session: a directory named with a date-time or a keyword and number, holding everything below it | 0.6 |
   | `shared_stem` | a context file whose base is a member's (`flight_03.yaml` beside `flight_03.ulg`); it joins every contested reading holding that member | 0.6 |
   | `shared_name_time` | one session: loose recordings or files whose names state the same time | 0.5 |
   | `sole_session_in_directory` | a context file beside the only loose session in its directory | 0.4 |
   | `numbered_sequence`, `name_time_clusters`, `name_time_proximity` | contested readings (§4) | 0.3 |

   - A recording outside any session directory is its own session unless a rule above joins it.
     Nothing joins two recordings by default: without a positive signal they stay apart.
   - A session-named directory holding two or more session-named directories is a collection,
     not a session. A rosbag2 directory is a recording, never a session directory, and sits in
     its parent like a file. A date alone (`2024-05-01/`) names a day, not a session.
   - Context files above session directories, or in a directory with no loose session, are
     unassigned: a directory boundary is never crossed by a guess.
   - Parts numbered by a session keyword (`episode_1`, `episode_2`) are separate recordings.
4. **Conflicts are contested, never resolved by precedence.** Where two rules read the same files
   differently, every reading becomes a proposal and none is chosen. Two proposals contest each
   other exactly when they share a file (status `contested`, each naming the other), so the parts
   of one reading never contest each other, and the relation costs one pass over the members.
   One `neptune.grouping.contested` finding (ambiguous, warning) per connected set of contesting
   proposals names their rules. The cases:
   - numbered parts with no start time: one recording split, or several numbered recordings;
   - a session directory whose recordings' name times are more than `gap_seconds` apart: the
     directory, or one session per cluster of times;
   - loose recordings whose name times are within `gap_seconds` but not equal: one session
     started in steps, or several;
   - a session directory holding exactly one session-named directory and files of its own: the
     outer directory, or the readings inside it;
   - two declared sessions claiming one file (two claiming exactly the same files are one
     proposal whose reasons name both).

   A context file that several loose sessions could each hold is a `session_unassigned` record,
   `ambiguous`, naming them as candidates, with one `neptune.grouping.ambiguous_member` finding per
   directory; with more than 64 candidates it is `unknown` (`too_many_sessions`) instead, since no
   one resolves a choice that wide by hand. A file no rule places is `session_unassigned`,
   `unknown`. Every file of the layout is
   a member of some proposal or unassigned, exactly once; a file sits in two proposals only when
   all of them are contested (`check_grouping`, run on every grouping). Identical bytes at two
   locations stay two members; each proposal holding one says so (`same_bytes`, one reason per
   shared content, empty files excepted since all of them are equal).
   - Names choose numbers, so nothing grouping does is proportional to a number a name states:
     missing part indices are counted and listed only up to 64.
5. **Links are recorded, never followed or made members.** A link's target is read lexically
   against its own directory (an absolute target, or one leaving the root, resolves to nothing).
   A proposal lists a link as `alias` when the target is its own directory or a member, and as
   `inside` when the link sits in its directory. Links stay walk results with discovery's finding
   as their record; grouping adds no canonical record for them.
6. **The override hook** is `GroupingConfig.sessions`: declared sessions by name and root-relative
   paths (a file, or a directory meaning everything below it). They claim their files before any
   rule runs, at confidence 1.0; one that matches nothing is a `neptune.grouping.
   declaration_unmatched` finding (missing, warning). The config, with `gap_seconds` (default 60),
   is the grouping transform's config, so an override is a new lineage. The job takes it as
   `JobOptions.grouping`; MVL-14's manifest fills it.
7. **Two derived record kinds**, derived schema version 1, `assertion_kind` `"inferred"` on every
   line, so nothing reads one as evidence:
   - `session_proposal`: `id`, `transform`, `rule`, `confidence`, `status`, `directory` (a
     location, or `{"kind": "root"}`), `members` (revision id, location, role `recording` or
     `context`, the rule that placed it and its band), `links`, `reasons` (rule, one line, facts)
     and `contested`. The id is a record id over the transform, rule, directory and member
     revisions only, so it never depends on the rest of the grouping.
   - `session_unassigned`: `id` (over transform and revision), `transform`, `revision`,
     `location`, `placement` (`ambiguous` or `unknown`), `reason` and `candidates`.
   - Derived records point at evidence (revision ids); evidence never points at them. Grouping
     findings are evidence-layer findings about the layout (`records` stays empty): they say what
     could not be decided, never what was inferred.
8. **Derived tables in the package** (amends ADR 0023 §5). A package may hold
   `derived/<kind>.jsonl`, listed in the manifest and so in the package id. The store checks
   structure only, since it never imports `neptune.derived`: canonical lines, each an object of the
   table's kind with an integer `schema_version`, a record id (sorted, each once) and a
   `transform` in the package's transform table. `neptune.derived.sessions.read_derived` reads
   meaning and refuses kinds it does not define. A present, empty table means its producer ran and
   inferred nothing; an absent table means it did not run. Interpretation still never enters
   `records/`. Imports: `derived/` may import `model`, `identity` and `discovery`; `model`,
   `identity`, `discovery`, `store` and `adapters` never import `derived/` (a test checks both).
9. **In the job**, grouping is pipeline stage 5 and runs at the end of the `inspect` phase, after
   probing and before planning, so a dry run (MVL-15) sees the same proposals. It reads no bytes
   and calls no adapter, so it adds no phase: the nine phases of ADR 0028 stay. It emits one
   `sessions_proposed` event with its counts; its findings and its transform (`neptune.grouping`
   0.1.0) enter every package, and its two tables are always written.
10. **The interface is the swap point.** `Grouper.propose(layout) -> Grouping` (proposals,
    unassigned files, findings, transform; `ranked()` most confident first). MVL-34 implements it
    over evidence records as well as the layout, under its own transform, as new lineage.

## Alternatives considered

- **Emit `Run` records for groupings.** A `Run` is what one piece of evidence declares, with
  record-level provenance citing it; a folder of five files declares nothing, and the canonical
  model is frozen to inference (non-negotiable 8).
- **Use modification times and media creation times.** They break byte-identical reruns after a
  copy; media times are an adapter's to read (M5) and reach grouping through MVL-34.
- **Classify recordings by the probe's selection.** It ties grouping to the registry, so adding
  an adapter would regroup unchanged trees under the same transform. Extensions are names; MVL-34
  reads the `Run` records adapters emit.
- **Resolve conflicts by rule precedence** (innermost directory wins, splits beat merges). It
  is a silent choice between readings the names support equally, which non-negotiable 4 forbids.
  Contested proposals cost a consumer a choice; a wrong silent choice costs a wrong dataset.
- **Merge recordings whose name times are close.** Back-to-back episodes 25 s apart would become
  one session; only equal times join, near ones are contested.
- **One Knowledge-style `Ambiguous` assignment per file.** `Knowledge` refuses inferred
  provenance by design, and a per-file union cannot say which files a reading groups together.
- **Groupings in `volatile/`** beside the cache report. Outside the manifest, so unverified and
  outside the package id, and ADR 0023 §5 already reserved `derived/` for exactly this.
- **Readers in the store for each derived kind.** The store would import `derived/`, reversing
  the evidence-to-interpretation dependency; structure in the store and meaning in `derived/`
  keeps the arrow one way and needs no store change for M7's kinds.
- **A tenth job phase, `group`.** It does no I/O and no adapter call; the nine phases are a
  contract the SDK and CLI print.
- **Grouping only the sources an adapter claimed.** Unclaimed files (videos, notes) are exactly
  the context a session needs, and every location is evidence.

## Consequences

- Every job package gains `derived/session_proposal.jsonl`, `derived/session_unassigned.jsonl`,
  the grouping transform and any grouping findings, so package ids change once; ids of evidence
  records do not. An empty root's package now holds the grouping transform and two empty tables.
- Dry run, the CLI and manifest generation (MVL-14, MVL-15) read `Grouping` directly or the
  package's derived tables. A user resolves a contested or ambiguous case by declaring sessions.
- v0 knows four recording extensions and sixteen session keywords; a corpus with other
  conventions gets conservative singles and unassigned files, never wrong merges, until MVL-34
  or a declaration says more.
- `docs/architecture.md` still lists grouping under `discovery/`; the observed half is there, the
  inferred half here. A dedicated docs PR should say so.
- Revisit if a common layout gets contested so often that users declare it routinely (a new rule
  then, as a new version), if MVL-34 needs readings the two kinds cannot express, or if derived
  tables need receipts of their own.

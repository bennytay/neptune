# 0066 — Run assembly: an evidence graph over the layout reading

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-34
- Supersedes: ADR 0036 §2 (grouping reads names only) and §9 (the package's grouping is the one
  made at inspect), for the grouping a package holds. 0036's interface, rules, record kinds,
  laws and dry-run grouping stand.
- Implements: ADR 0050 §7 (`run_assembly`, canonical, from rosbag2's file list)

## Context

v0 (ADR 0036) groups by names and directories only, before any adapter runs. That is what a dry run
can know, but a package is made after every adapter has read its source, and the records then say
more than names do: which run a source declares and on which clock, which machine recorded it
(`px4.sys_uuid`), which software ran, which files a rosbag2 bag lists, which sources are
configuration, what a note's text names. MVL-34 asks for the component that decides which sources
are one physical session from that evidence: a candidate graph, scoring, merges and splits, and
an explanation, with no silent contamination (shared configs, standalone notes).

The Deploy D1 gate found the concrete failure: a rosbag2 bag stored as MCAP yields two `Run`
records (the metadata's and the storage file's), and nothing in the package says they are one
recording, although `metadata.yaml` states exactly that in `relative_file_paths`. ADR 0045 §6 left
"listed versus present parts" as a cross-source check for later. Consumers already read the
grouping through `Grouping` (snapshot binding, MVL-38; clocks, MVL-36), so the interface must hold.

## Decision

1. **Stated file lists are canonical.** For each `metadata.yaml` whose rosbag2 records hold one
   `Run` and one `relative_file_paths` table, the assembler writes one `RunAssembly` to
   `records/` (ADR 0050 §7): `stated`, provenance citing the list, rule `rosbag2.metadata`, `run`
   the metadata's `Run`, members the metadata (`description`, citing the run's declaration) and
   every listed file present beside it (`recording`, citing its row), validity `NotApplicable`.
   Its id is ADR 0017's evidence id under the grouping transform. Copies of one bag share their
   metadata's bytes, so its `Run` and this record: the record lists every copy's files, and each
   copy keeps its own session reading. Listed paths resolve lexically against the metadata's
   directory; a path the adapter calls unsafe (empty, NUL, absolute or drive letter, backslash,
   any `..` segment: `unsafe_part_path`) is never resolved. Bags of metadata version 3 and
   earlier list `rec/rec_0.db3` for a part of `rec/`, as rosbag2's reader resolves them; such a
   path is read against the bag's parent when only that reading names a file the scan saw. Each `Run` the sources declare stays as declared: the
   statement joins them, nothing merges them.
2. **Listed versus present.** Per copy: a listed file this scan did not see is
   `neptune.grouping.listed_part_missing` (missing, warning, the missing paths and their rows); a
   `.db3`/`.mcap` beside the metadata that it does not list is `neptune.grouping.unlisted_part`
   (inconsistent, warning) and becomes its own `recording_file` reading. The bag's reading holds
   exactly what the list states (rule `rosbag2_file_list`, band 0.95). A wider reading (a session
   directory holding the bag) keeps its directory's files and gains the reason.
3. **Edges** between the recordings of every inferred reading, each a reason naming its rule,
   fixed weight and the files it read:
   | Edge | Fires when | Kind | Weight |
   |---|---|---|---|
   | `same_machine` | two or more recordings declare one machine id, and none another in that namespace | support | 1/2 |
   | `mixed_machines` | recordings declare two ids in one namespace (`Run.machine`, `Machine.identifiers`, `SoftwareConfiguration.machine`) | contradiction | 3/5 |
   | `times_overlap` / `times_apart` | runs on clocks of one family (below) form one span / several spans with `gap_seconds` tolerance | support / contradiction | 1/2 |
   | `same_software` / `software_differs` | one software name with one / several declared commits (else releases); not scored when machines are mixed | support / contradiction | 1/5, 3/10 |

   Ids in different namespaces are never compared, and a file naming several ids in one namespace
   (a fleet log) is left out of that namespace's comparison. Software compares commits with
   commits and releases with releases, never across. Times are compared only when both domains state
   a known epoch in {`unix`, `gps`} and the same known civil timescale (`utc`, `tai`, `gps`,
   `posix`): that pair makes them one clock. A boot, monotonic, first-sample or unknown epoch is its
   own clock until a clock mapping (MVL-36) relates it; today's MCAP, rosbag and ULog clocks are of
   that kind, so time edges are honest silence on them.
4. **Score.** `c = 1 - (1 - band) * prod(1 - w)` over supports, then `c * prod(1 - w)` over
   contradictions, floored at 0.01 and rounded half-even to four places, on exact fractions. The
   band is the rule's (ADR 0036 §3, plus `rosbag2_file_list` 0.95, `machine_split`,
   `machine_time_merge` and `named_in_document` 0.5). A ranking, not a probability. Declared
   sessions are stated and keep 1.0.
5. **Operations**, each a reading offered beside the others, contested where extents share a file
   (ADR 0036 §4), never chosen:
   - *split*: a reading with `mixed_machines` gains one `machine_split` reading per machine (its
     recordings only) and a `neptune.grouping.mixed_machines` finding (ambiguous, warning). A
     multi-robot session is real; so is a stray log.
   - *merge*: loose recordings of one directory declaring one machine whose runs overlap on one
     clock family are offered as one `machine_time_merge` reading, unless a reading already holds
     them together.
   - *documents*: an unassigned document whose text names a recording's file name or stem, or a
     session or bag directory's name (identifier-shaped names only: at least six characters and a
     digit, `_`, `-` or `.`, so prose such as `camera` never joins; at most 100,000 words read per
     document), joins that reading (`named_in_document`); naming readings that share no file,
     it is unassigned, ambiguous (`several_named`). A document naming nothing stays as v0 left it.
   - *shared configuration*: a source holding a `configuration_snapshot`, unassigned for want of
     a session in its directory and held by no declaration, with readings of two or more separate
     sessions below it (readings that share a file are one session), stays
     unassigned with reason `shared_reference`, placement `ambiguous` and every such reading as a
     candidate (more than 64: `unknown`, `too_many_sessions`), and each candidate gains a
     `shared_reference` reason naming it. It is a reference all of them share, never a member, so
     no session is contaminated and no ambiguity finding is raised.
6. **Explanation.** Every operation and edge is a `Reason` on the proposal (rule, one line, the
   facts: files, machines, clock family, weights, run and run-assembly ids); every doubt is a
   finding under the grouping transform in the receipt. `neptune explain` is a dry run: no records
   exist yet, so it shows v0's layout reading, as before.
7. **One producer, a new version.** The assembler is `neptune.grouping` **0.2.0**
   (`neptune.derived.assembly.RunAssembler`), so consumers that look for the grouping producer find
   it; v0 stays `neptune.grouping` 0.1.0 (`LayoutGrouper`). Its config is the grouping config; its
   `upstream` is the manifest's transform (if any) and every transform whose records it read, so an
   adapter upgrade re-lineages it. It implements `Grouper.propose(layout)`; `assemble(layout)` also
   returns the run assemblies. No record kind and no schema version is added: `run_assembly` is
   ADR 0050's, and a package holding one is written at that kind's version (ADR 0037 §1).
8. **In the job**, assembly runs at the start of stage 9 (`assemble`), over the committed records
   of the admitted sources, keeping only what it reads (a document's words, file-list rows), and
   replaces inspect's grouping in the package: v0's findings leave the package with it. It emits
   `runs_assembled` (the grouping's counts and `run_assemblies`). Passes that read `Grouping` after
   it (snapshot binding, clocks) read the assembled one, unchanged.

## Alternatives considered

- **Delete or merge the storage file's `Run`.** It is that file's own declaration (non-negotiable
  2); merging runs is the identity merge AGENTS.md forbids. The stated assembly relates them.
- **Make the bag's session proposal `stated`.** A session proposal is stated only as a user's
  declaration (ADR 0036 §6); the statement already has its canonical record, which the proposal
  cites by id.
- **Make the MCAP adapter skip bag members.** An adapter sees one file (ADR 0045 §1); whether a
  file is a bag's part is a cross-source fact.
- **Compare times across any clocks with a tolerance.** Two boot clocks, or a boot clock and Unix
  time, share no epoch: an overlap would be invented (non-negotiable 4). MVL-36's mappings will
  widen the families as a new version.
- **Attach a shared config to every session as a member.** Every pair of sessions would then
  contest each other through it, and a consumer would read one site file as each run's own.
- **A new `Placement.shared` value.** A derived schema change for one case `ambiguous` plus a
  reason already says; consumers that need it read the reason.
- **A learned or probabilistic scorer.** Not deterministic-first, not explainable by its facts.
- **A separate `neptune.assembly` producer.** Every consumer and test that names the grouping
  producer would have to learn a second name for the same tables.

## Consequences

- Every package's grouping transform is `neptune.grouping` 0.2.0 with the read adapters upstream,
  so its derived session tables, the transform and the package id change once; evidence record
  ids do not. Packages with a rosbag2 bag gain `records/run_assembly.jsonl` and become
  schema-version 3 packages. Goldens changed accordingly.
- The D1 gap "rosbag2 MCAP bags become two runs" is closed by the run assembly and one session
  reading; ADR 0045 §6's listed-versus-present check now exists, here rather than in `validate/`.
- Machine evidence exists only where adapters declare it (ULog, manifests, software records), and
  time evidence only on civil clocks; trees without either get v0's readings, now explained.
- Revisit when MVL-36's clock mappings land (time families by mapping, as 0.3.0), when MVL-35
  links identities (machine ids across namespaces), or if documents named by prose produce false
  joins in real trees (a longer minimum, or names only from a declared list).

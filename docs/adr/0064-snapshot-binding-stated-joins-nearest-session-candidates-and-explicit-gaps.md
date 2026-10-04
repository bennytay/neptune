# 0064 — Snapshot binding: stated joins, nearest session candidates, explicit gaps

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-38
- Implements: ADR 0050 §2 and §8 (`snapshot_binding`, canonical and derived), ADR 0040 §12 (binding
  software identity to runs), ADR 0037 Consequences (binding a run to the configuration it ran)

## Context

A recording is only useful with the machine state that produced it: its parameters, its software,
build, firmware and checkpoint, its hardware revision and payload, its calibration. Adapters read
each of those from its own file (ADR 0037, 0040, 0055); nothing says which run used which. MVL-38's
acceptance: a run has an evidence-backed configuration snapshot or an explicit unresolved binding,
never a silent guess. Constraints:

- ADR 0050's shapes are frozen. `SnapshotBinding.snapshot` is one record id, not `Knowledge`: a
  binding cannot be `Unknown` or `Ambiguous`; one canonical record's id derives from its evidence
  (ADR 0017 §5), so one declaration gives one canonical record.
- Runs grouped from a folder are inferred (ADR 0036); MVL-34's assembler (ADR 0066) replaces v0
  grouping and states run membership (`RunAssembly`). Binding consumes the `Grouping` interface
  and those assemblies, never its own reading of sessions.
- A robot folder routinely holds several runs, robots and parameter files side by side; a wrong
  silent choice costs a wrong dataset, and one session folder may hold thousands of episodes.
- A binding must not depend on where the job was rooted: the same tree ingested from
  `fleet/` or from `fleet/robot_a/` binds the same runs to the same snapshots.

## Decision

1. **A derived pass, `neptune.bindings` 0.1.0**, run at assembly right after run assembly (ADR
   0066), over committed records collected in the assembler's one pass: every `Run`, every
   `configuration_snapshot`, `software_configuration`, `hardware_configuration` and `calibration`,
   canonical bindings adapters emitted, the run assemblies, the job's `Layout` and assembled
   `Grouping`, and the `StructuredRecord` rows of sources that declare a run. It reads no source
   bytes, so it is a non-reader in the receipt's `read_by` (as validation). A package with no run
   gets nothing: no transform, table or finding, so unchanged packages keep their bytes.
2. **Stated: the run's own source names the snapshot by a value that reads the same under any
   root** (canonical `records/snapshot_binding`, `stated`, provenance citing the naming row;
   validity `Unknown`, as nothing states a window). A text cell of the run's source equals,
   verbatim, the snapshot source's content id (`sha256:<hex>` or bare hex), or a full git commit
   or a stated checkpoint or image digest a software record declares: among the run's own
   snapshots (§4) first, else anywhere in the package (several there is a conflict, §5). An exact join of declared identities is evidence (ADR 0050 §2). A snapshot the run's
   own source declares is stated as well, citing its own declaration (`same_source`). Only for a
   source that declares exactly one run: with several, which run a row is about is not stated.
   **Declared, but inferred** (`derived/snapshot_binding`, rule `declared_by_run`, citing the
   naming row first): a text that is a path relative to the recording's directory (collapsed
   lexically, never leaving the root), because it assumes the robot's working directory; and a
   firmware version one of the run's own snapshots declares, because a version names a release
   that many images may carry. No path is ever read relative to the ingest root: that is the job's
   view, not a value any source declares. Other versions are too common to join on.
3. **One declaration, one canonical record.** A file of several snapshots (a YAML stream, a
   multi-camera calibration) that a row names is bound document by document in `derived/`, each
   citing the row first: ADR 0050's ids allow one record per declaration.
4. **Inferred: nearest of its slot among the run's own snapshots** (`derived/snapshot_binding`,
   rule `session_nearest`, ADR 0050's fields exactly, `InferredProvenance` citing the run then the
   snapshot). A run's candidates are scoped per run, never per session:
   - A **recording unit** is the run's source, joined with every file a `RunAssembly` lists for
     its run as a recording or description (a rosbag2 bag's metadata and storage are one unit).
   - A snapshot file belongs to at most one unit: the unit a `RunAssembly` lists it in as context
     (evidence, wherever the file is); else the recording in its own directory whose name stem its
     name extends at a `_` or `-` (`ep_7_hw.yaml` beside `ep_7.mcap`; the longest stem wins, and a
     stem another recording there extends too, `run` of `run_2.mcap`, is no one's alone); else the
     one unit below the nearest directory above it that holds any. Save for an assembly's
     placement, it must also share a proposal of the grouping with that unit, so a directory
     boundary the grouper keeps is never crossed. A snapshot a unit member declares itself is that
     unit's own: `same_source` for its own run, a candidate for its unit-mates.
   - A file as near to several units (a stem two recordings share, or a directory holding several
     units and no stem match) is no unit's own: it is bound to none, and one
     `neptune.bindings.shared_snapshot` finding (ambiguous, info) names it, the directory, how many
     recordings are below it and up to 64 of their runs. A run that names it (§2) still binds it. Other recordings' sidecars are never a
     run's candidates.
   Within a unit, candidates compete by **slot**, kind and file name: two `nav_params.yaml`
   compete; `nav_params.yaml` and `fleet.yaml` both apply. Nearness is the number of directories a
   candidate's directory shares with the unit's. A unique nearest content wins; a declared binding
   (§2) settles its slot outright.
5. **Conflicts are findings, never choices.** Candidates of one run tied for nearest, or one
   declared value naming several snapshots, give `neptune.bindings.conflicting_snapshots` (ambiguous, warning)
   with the run and every candidate in `records`, each candidate's declaration in `related` and
   its paths inside the run's unit in `details` (up to 64, with `path_count`), and no binding. A declared snapshot that is not its slot's nearest gives
   `stated_differs_from_nearest` (inconsistent, warning) and the declaration stays bound.
6. **Unresolved is explicit.** For each of the four kinds a run has no binding of (canonical,
   adapter-made or inferred), one finding naming the run and the kind:
   `neptune.bindings.no_software_identity` (missing, warning; MVL-38's run-level finding) for
   software, `snapshot_unresolved` otherwise (warning for configuration, info for hardware and
   calibration, which most trees do not hold). Every run is bound or unresolved for every kind.
7. **Identity.** Snapshots are joined by content: identical bytes at two paths are one snapshot,
   bound once, while the two locations stay two revisions; nothing is merged, rewritten or chosen
   between. Canonical ids follow ADR 0017 §5; derived ids hash transform, run, snapshot and rule.
   Every table is sorted by id, and the result does not depend on input order.
8. **Validity stays `Unknown`, clock mappings notwithstanding.** ADR 0060's mappings relate two
   clocks; they say nothing about *when a snapshot applied*. A window would have to come from
   evidence of that (a parameter change logged mid-run, a manifest's start and end), and none of
   the joins above carries one. Writing the run's own `[first, last]` would assert that the
   snapshot held for the whole run, which no source states; mapping a calibration's declared
   `valid_from`/`valid_until` onto the run's clock through an inferred mapping is configuration
   lineage (MVL-127). A later version states windows where a source declares when a snapshot took
   effect on a run clock, and uses `neptune.derived.clocks` only to carry such a stated instant
   across clocks, with its bound.
9. **Cost** is linear in files and records: extents are computed once per proposal, units are
   indexed by directory once (each recording's stem, and how many units are below each
   directory), each snapshot file is given its owner by a walk up its own ancestors, and each run
   reads its unit's candidates and its own source's rows; joins are dictionary lookups. Bindings
   are at most one per run per slot of its own: R runs with one sidecar each give R bindings.

## Alternatives considered

- **Bind every candidate when several compete** (several bindings for one slot): it asserts both
  readings; the contract has no `Ambiguous` binding, so the ambiguity goes to a finding.
- **Precedence by name or recency** (newest, or `params.yaml` over `params_old.yaml`): the silent
  choice non-negotiable 4 forbids. Nearest-directory is a positive signal; ties stay ties.
- **Sidecar run files as stated** (a `run.yaml` beside the recording naming its config): the
  sidecar's link to the run is itself an inference (its name or place), so the chain is not
  stated. A stem sidecar scopes the run's candidates (§4) and binds as inferred.
- **Session-wide candidates** (every snapshot in a session the run is in, as first written): in a
  folder of R episodes each run bound every other episode's sidecar, R² bindings that asserted
  nothing true. Scope is the run's own unit.
- **Root-relative paths as stated** (as first written): a value `hw.yaml` matched a root-level file
  three directories above the recording, and rooting the same tree lower bound nothing. The root
  is the job's view, not the source's.
- **Bind a file shared by several runs to each**: a fleet-wide file above two robots may be either
  robot's, both, or neither's; nothing says which, so the finding says so instead.
- **Machine-id joins and validity windows** (a calibration valid until a date, a hardware revision
  per machine): the join says "same machine", not "this run used it", and windows need the run's
  clock alignment (MVL-36). Memory's configuration lineage (MVL-127) consolidates both.
- **A manifest pin**: the manifest (ADR 0047) names software entities, not snapshot records; a
  declared session still only scopes nearness. A pin field is a manifest schema change.
- **A new kind with `Knowledge` snapshots**: ADR 0050 is frozen for consumers; findings already say
  what could not be decided (ADR 0036 §7).

## Consequences

- Every package holding a run gains the `neptune.bindings` transform, `derived/snapshot_binding`
  and one finding per unbound kind per run; one with a stated binding is a schema-version 3 package
  (ADR 0050 §9). Goldens with runs changed accordingly; evidence record ids did not.
- MVL-34's assembler is behind `Grouping` (grouping 0.2.0) and its `RunAssembly` records define
  units; bindings re-lineage with it. A fleet-wide file above several recordings is reported, not
  bound: a manifest pin or a fleet-level binding rule is a later version. MVL-36's clock mappings
  are not a source of windows (§8); a source stating when a snapshot took effect is.
- Revisit if conflicts are frequent in real trees (then a sidecar or manifest pin rule, as a new
  version), if hardware or calibration adapters land widely (severity of their gaps), or if a
  consumer needs the rule on the derived record itself.

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
- Runs grouped from a folder are inferred (ADR 0036), and MVL-34 will replace v0 grouping. Binding
  must consume the `Grouping` interface, never its own reading of the tree.
- A robot folder routinely holds several runs, robots and parameter files side by side; a wrong
  silent choice costs a wrong dataset.

## Decision

1. **A derived pass, `neptune.bindings` 0.1.0**, run at assembly over committed records (like
   introspection): every `Run`, every `configuration_snapshot`, `software_configuration`,
   `hardware_configuration` and `calibration`, canonical bindings adapters emitted, the job's
   `Layout` and `Grouping`, and the `StructuredRecord` rows of sources that declare a run. It reads
   no source bytes, so it is a non-reader in the receipt's `read_by` (as validation). A package
   with no run gets nothing: no transform, table or finding, so unchanged packages keep their bytes.
2. **Stated: the run's own source names the snapshot** (canonical `records/snapshot_binding`,
   `stated`, provenance citing the naming row; validity `Unknown`, as nothing states a window).
   A text cell of the run's source equals, verbatim, one of: the snapshot source's content id
   (`sha256:<hex>` or bare hex); a path its bytes are at, root-relative or relative to the
   recording's directory (collapsed lexically, never leaving the root); or, among the run's session
   files, an identity a software record declares (a full git commit, a stated checkpoint or image
   digest, a firmware version; other versions are too common to join on). An exact join of declared
   values is evidence (ADR 0050 §2). A snapshot the run's own source declares is stated as well,
   citing its own declaration (`same_source`). Only for a source that declares exactly one run:
   with several, which run a row is about is not stated.
3. **One declaration, one canonical record.** A file of several snapshots (a YAML stream, a
   multi-camera calibration) that a row names is bound document by document in `derived/`, each
   citing the row first: ADR 0050's ids allow one record per declaration.
4. **Inferred: nearest of its slot in the run's sessions** (`derived/snapshot_binding`, ADR 0050's
   fields exactly, `InferredProvenance` citing the run then the snapshot). The candidates are the
   snapshots whose files lie in the extents of every proposal holding the run's recording. They
   compete by **slot**, kind and file name: two `nav_params.yaml` compete; `nav_params.yaml` and
   `fleet.yaml` both apply. Nearness is the number of directories a candidate's path shares with
   the recording's. A unique nearest content wins; a stated binding settles its slot outright.
5. **Conflicts are findings, never choices.** Candidates tied for nearest, or one declared value
   naming several snapshots, give `neptune.bindings.conflicting_snapshots` (ambiguous, warning)
   with the run and every candidate in `records`, each candidate's declaration in `related` and
   their paths in `details`, and no binding. A stated snapshot that is not its slot's nearest gives
   `stated_differs_from_nearest` (inconsistent, warning) and the statement stays bound.
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
9. **Cost** is linear: extents are computed once per proposal, and each run reads its sessions'
   files and its own source's rows; joins are dictionary lookups.

## Alternatives considered

- **Bind every candidate when several compete** (several bindings for one slot): it asserts both
  readings; the contract has no `Ambiguous` binding, so the ambiguity goes to a finding.
- **Precedence by name or recency** (newest, or `params.yaml` over `params_old.yaml`): the silent
  choice non-negotiable 4 forbids. Nearest-directory is a positive signal; ties stay ties.
- **Sidecar run files as stated** (a `run.yaml` beside the recording naming its config): the
  sidecar's link to the run is itself the grouping's inference, so the chain is not stated. Such a
  file is a session member like any other and binds by nearness.
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
- MVL-34's grouper swaps in behind `Grouping`; bindings re-lineage with it. MVL-36's clock mappings
  are not a source of windows (§8); a source stating when a snapshot took effect is.
- Revisit if conflicts are frequent in real trees (then a sidecar or manifest pin rule, as a new
  version), if hardware or calibration adapters land widely (severity of their gaps), or if a
  consumer needs the rule on the derived record itself.

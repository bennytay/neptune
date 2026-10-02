# 0050 — The alignment record contract: identity links, clock mappings, frame and run bindings

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-82
- Amends: ADR 0017 §4 (a new family, `alignment`) and §5 (an exact join of declared values is
  evidence, not inference); ADR 0005 §6 and ADR 0012 (the record that relates two clocks is
  `ClockMapping`, not the `ClockAlignment` those ADRs named ahead of time)

## Context

M7 (MVL-34 to MVL-38) relates records that sources declare separately: a log's machine id and a fleet
register's asset tag, a log's boot clock and GPS time, a URDF edge and the calibration that measures
it, the files of one recording, the configuration a run used. Its logic will take weeks, but Memory's
consolidators (identity, time-domain registry, configuration lineage, spatial memory; MVL-126,
MVL-130) and the Ledger's cross-clock merge (MVL-97, Ledger ADR 0003 §3) need the *shape* of its
output now. This ADR freezes that shape, the way the M1 gate froze the model before any adapter.
Forces:

- **Evidence ≠ interpretation.** An alignment a procedure estimates (a fitted drift, a folder-based
  session, two frames matched by name) is inferred and belongs in `derived/`. But much alignment is
  stated: a register row naming one drone by two ids, rosbag2's metadata listing its storage files.
- **No merges.** Two identical URDFs are not one robot; an identity is an edge consumers traverse,
  never a rewrite (AGENTS.md, Memory ADR 0003 §1.4).
- **No silent clock assumptions.** Two times written side by side (a GPS fix and its publication
  time) are not one instant. A mapping applies arithmetic only a consumer may apply.
- **Bi-temporal consumers.** Memory's claims hold over half-open `[valid_from, valid_to)` windows on
  one clock; the Ledger walks affine, increasing clock maps with a residual bound and refuses others.
- **The model grows by addition** (ADR 0023 §1, ADR 0037 §1): new kinds with `since`, and the package
  schema contract is versioned by `SCHEMA_VERSION` (platform ADR 0002).

## Decision

1. **Five record kinds in a new family, `alignment`** (`model/alignment.py`, `since` 4):
   `identity_link`, `clock_mapping`, `frame_binding`, `run_assembly` and `snapshot_binding`. Each is
   an evidence record (ADR 0017 §5): `id` from its record-level evidence, one `provenance`
   (`observed` or `stated`), `Knowledge` fields for everything with epistemic weight, and
   `validity` (§3). They point at other records by id and at clocks by `TimestampDomain` id; nothing
   points back at them, so no existing kind changes.
2. **Canonical when the evidence states the relation; derived otherwise.** A canonical alignment
   record is one a source states (a register row, a metadata file, a calibration naming its edge,
   a format specification that defines one clock by another) or an exact join of values sources
   declare verbatim (two declarations giving one `px4.sys_uuid`), with every side cited: the join adds
   no reading, so it is evidence. Anything with a tolerance, a fit, a heuristic or a model is
   `inferred`: it is written to `derived/<kind>.jsonl` with these kinds' fields exactly, its
   `provenance` an `InferredProvenance` (ADR 0036). MVL-34 to MVL-38 add those derived readers
   without a new ADR; any other change to these shapes needs one.
3. **Validity windows.** `validity: Knowledge[ValidityWindow]`; a window is `{clock, start, end}`:
   a `TimestampDomain` id and two `Knowledge[Timestamp]` bounds on that clock, **start inclusive,
   end exclusive**, never empty. A bound is `Known` (stated), `KnownAbsent` (stated open),
   `Unknown` (could be stated and is not) or `NotCovered` (no place for it). A declared inclusive
   last instant is written as the next tick, which is exact on integer ticks. The whole window is
   `NotCovered` where a source has no notion of time (a URDF), `Unknown` where it could state one,
   and `NotApplicable` where the relation is timeless (a file's membership of a run).
4. **`IdentityLink {left, right, basis, identifier, evidence, validity}`: never a merge.** `left` is a
   `LogicalId`; `right` is `Known` or `Ambiguous` with every id the evidence could mean, never equal
   to `left`. `basis` is `co_declared` (one declaration names a thing by both ids; `identifier`
   `NotApplicable`) or `shared_identifier` (two declarations each give `identifier`, verbatim, in one
   namespace; `provenance` cites the left side, `evidence` the other declarations, at least one, no
   citation twice). Consumers keep both ids and may traverse the link; no record, thread or node is
   rekeyed. Memory ADR 0003's `identity_link {left, right, identifier}` reads `right` and
   `identifier` as `Known` values and treats `Ambiguous` as candidates.
5. **`ClockMapping {source, target, method, anchor, rate, residual_bound, validity}`** maps source
   ticks to target ticks: `target(t) = anchor.target.ticks + rate * (t - anchor.source.ticks)`.
   `anchor` is the offset as one instant on both clocks (`ClockAnchor`), so making it converts no
   tick; `rate` is target ticks per source tick, an exact positive fraction in lowest terms, so
   drift and differing resolutions are one number. `residual_bound` is a non-negative `Duration` on
   the target clock: the largest error the evidence states, rounded up to whole ticks. `validity` is
   on the source clock. `method` is `stated` or `co_sampled`, the latter only where the format says
   two fields of one sample are the same instant. The map is affine and increasing, which is
   exactly what the Ledger's merge accepts (Ledger ADR 0003 §3): `a = rate`,
   `b = anchor.target - rate * anchor.source`, bound `residual_bound`.
6. **`FrameBinding {parent, child, transform, basis, calibration, validity}`**: the `FrameTransform`
   record that gives the edge `parent → child` of one frame graph its value. The transform may sit
   in another graph (a calibration file's own), which is how a calibration says which description
   edge it measures. `basis` is `robot_description`, `calibration` (`calibration` names the
   `Calibration` record, `Known` or `Ambiguous`) or `transform_message` (`calibration`
   `NotApplicable`). Composing or inverting transforms stays MVL-37's.
7. **`RunAssembly {run, rule, members, validity}`**: the files that form one `Run`. Each member is
   `{revision, role, evidence}`: a `SourceRevision` id (bytes at a location, as session proposals
   use, ADR 0036), `recording`, `description` or `context`, and the `EvidenceRef` that places it
   there. Members are sorted by revision id, each once, at least one. `rule` names the declared rule
   applied (`recording`: a recording is its own run; `rosbag2.metadata`: the files
   `relative_file_paths` lists); its version and config are the producer's `TransformRecord`.
8. **`SnapshotBinding {run, snapshot, snapshot_kind, validity}`**: the run ran with a machine-context
   snapshot of kind `hardware_configuration`, `software_configuration` or `calibration`
   (`configuration_snapshot` joins when MVL-38 needs it, by ADR), for the window on one of the run's
   clocks that the evidence states. A mid-run change is two bindings with adjacent windows.
9. **Version.** The kinds are `since` 4, so `SCHEMA_VERSION` is 4 and the package-schema contract
   publishes **4.0.0**: platform ADR 0002 §3 makes an integer owner constant the registry major,
   so a new kind cannot be a minor version even though every earlier golden still validates. A
   package that holds no alignment record is written at its old version, byte for byte (ADR 0037
   §1). `alignment-records` becomes active and rides on package-schema's version.
10. **Worked examples.** Each of the four examples holds the alignment its sources state: the drone
    (a fleet register's co-declared identity link, its log as its own run, three snapshot bindings
    from the log's start), the quadruped (rosbag2's file list, its `starting_time` → MCAP
    `log_time` identity map with bound 0, URDF edge bindings, the ROS distribution's binding), the
    manipulator (the hand-eye calibration's edge binding, its log as its own run) and the mobile
    robot (its bag as its own run). The drone gains one source, `fleet.json`; no other source
    changes.

## Alternatives considered

- **Alignment only in `derived/`.** Every alignment would be an inference, including a register
  row that states two ids and rosbag2's own file list; Memory ADR 0003 grounds `same_as` on
  `observed` links exactly because they are declared. Stated relations are evidence.
- **One generic `Relation {subject, predicate, object}` kind.** Each consumer would re-derive the
  structure (anchors, rates, members) from free-form values; the schema could check nothing.
- **A clock offset as a signed `Duration`.** An offset between two clocks is in neither clock's
  ticks until a rate and an epoch are assumed; an anchor pair states it without converting.
- **Offset and drift as floats, or ppm.** Floats break determinism across platforms and lose the
  exactness the Ledger's rational arithmetic relies on; one exact rate holds drift and resolution.
- **Inclusive windows like `Run.first/last`.** Memory and most bi-temporal stores are half-open;
  adjacent inclusive windows overlap by an instant or leave a gap.
- **An `IdentityLink` per pair of declarations with a merge flag.** A flag invites the merge
  AGENTS.md forbids; an `Ambiguous` right side keeps every candidate without choosing.
- **A minor package-schema version.** The bump tool refuses it while `SCHEMA_VERSION` is the
  registry major; changing that rule is platform's (ADR 0002), not this ADR's.

## Consequences

- Memory (MVL-126, MVL-130) and the Ledger (MVL-97) pin package-schema 4.0.0 and parse these shapes;
  the Ledger's catalog API, which embeds the compiler's kinds, takes a minor version in the same PR.
- MVL-34 to MVL-38 implement against these types: canonical records where a source states the
  relation, the same fields under `derived/` where they estimate it. A field they need that is not
  here is a new ADR and a new kind or companion kind, never an edit.
- Adapters may emit alignment records their own source states (rosbag2's file list); cross-source
  joins come from an alignment pass that cites every side.
- Revisit if a clock relation is not affine (a piecewise map over many anchors), if identity needs
  more than two ids per link, or if a consumer needs validity on the target clock as well.

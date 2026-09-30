# Canonical data model

Status: **draft** — primitives are specified by MVL-2 / MVL-40 / MVL-4 / MVL-3; entities by MVL-1. This page
records the decisions already made so those issues start from a shared baseline. It becomes authoritative when
MVL-1 merges.

## Entities (v0)

| Family | Entities |
|---|---|
| Source | `SourceArtifact`, `SourceRevision`, `SourceAbsence` (implemented in `model/source.py`, ADRs 0009, 0010) |
| Run / experience | `Run`, `Stream`, `TimestampDomain` |
| Machine | `Machine`, `HardwareConfiguration`, `SoftwareConfiguration`, `Calibration` |
| World / record | `Site`, `Asset`, `SpatialArtifact`, `StructuredRecord`, `DocumentRecord` |
| Task | typed records defined in MVL-33 (`TaskBrief`, `SOPSection`, `Requirement`, `WorkOrder`) |
| Cross-cutting | `EvidenceRef`, `TransformRecord`, `Provenance`, `IngestFinding`, `IngestReceipt` |

Naming decisions: `IngestFinding` (not `IntegrityFinding`). No `ProvenanceEdge` entity in v0 — provenance is
embedded on each record. `EpisodeCandidate` / `Observation` are **reserved names, not modelled**: they are the
boundary to the memory learner.

## Epistemic states — `Knowledge[T]` (ADR 0004, ADR 0011; `model/knowledge.py`)

```
Known(value)              the source asserts this value
KnownAbsent               the source asserts there is no value
Unknown                   the source says nothing
NotCovered                the source could not have said anything (never measured / out of scope)
NotApplicable             the field does not apply to this entity
Ambiguous(candidates)     the source supports more than one reading
```

Scope rule: fields with epistemic weight — units, clocks, frames, versions, calibration, identities, coverage —
use the wrapper. Purely structural fields (the list of streams found) do not. A `None` in a canonical record is
a bug, not a value.

- Provenance: states hold `INHERITED` (the record's provenance, omitted from JSON) or their own. `KnownAbsent`
  always cites what defines the absence.
- JSON: `{"knowledge": "<state>", ...}`, e.g. `{"knowledge":"known","value":30}`. Full shape: ADR 0011.
- No confidence scores on evidence; uncertainty is `Ambiguous` / `Unknown` / `NotCovered` (ADR 0004 §6).

## Provenance (ADR 0006, ADR 0016; `model/provenance.py`)

- `Provenance(evidence: EvidenceRef, transform: TransformRecord id, assertion_kind: observed | stated)` fills
  every `Knowledge` provenance slot. `inferred` exists only in `derived/` and is rejected on canonical states.
- `EvidenceRef(source content id | external object, locator path)`: steps outermost first, each inside what the
  transform decoded from the previous one. Details and the step table: `provenance-and-identity.md`.
- `TransformRecord.upstream` hash-links a normaliser to the transform it consumed, so a normalised value cites
  the same evidence as its input and its chain is `adapter → normaliser`.

## Time (ADR 0005, ADR 0012; `model/time.py`)

- `Timestamp = (ticks: int, domain_id)`: signed 64-bit, never floats, never bare. `Duration` carries its
  domain too. Ordering and subtraction across domains raise; `==` is record equality, not simultaneity.
- `TimestampDomain` is an entity: structural `field` + `scope` (where the ticks are read, verbatim) and
  `Knowledge`-wrapped `role` (receive / publish / sample / document), `resolution` (exact `Fraction`
  seconds per tick), `epoch`, `timescale`, `declared_monotonic`.
- MCAP `log_time` and `publish_time`, ROS `header.stamp` and receive time, PX4 boot-time and GPS time are
  separate domains. Mappings between domains are `ClockAlignment` records produced in MVL-36 with method,
  evidence and error bounds.

## Units (ADR 0013; `model/units.py`)

- `Knowledge[Unit]`, stored as declared (`mm` stays `mm`) or `Unknown`; nothing defaults to SI.
- `Unit` = canonical product of catalogued, optionally prefixed atoms: JSON `"km.h^-1"`.
  `Dimension` adds plane and solid angle to the SI bases, so rad/s ≠ Hz.
- Declared text goes through `unit_from_text` only: one reading ⇒ `Known`, several (`g`, `C`) ⇒ `Ambiguous`,
  unreadable ⇒ finding + `Unknown`.
- `to_si` is exact (`rational × π^k`) and used by derived transforms only; the SI value is a separate
  record. `CATALOGUE_VERSION` is an output-affecting library version.

## Frames and spatial references (ADR 0007, ADR 0015; `model/frames.py`, `model/spatial.py`)

- `FrameRef = (frame_id verbatim, frame_graph_id)`; graphs are source-scoped tier-2 records. `Frame` holds
  `Knowledge`-wrapped `axes` (named conventions `enu ned nwu flu frd rdf rub ruf fru`) and `handedness`.
  No default; REP-103 is not evidence.
- Rotations (`Quaternion`, `RotationMatrix`, `EulerAngles`, `RotationVector`), `Translation`, `Pose` and
  `HomogeneousMatrix` keep their float components in source order. Order, layout, Euler sequence/mode,
  quaternion algebra and units are separate `Knowledge` fields, so "undeclared" keeps the numbers.
- `FrameTransform(parent, child, direction, value, validity)`: one graph, `direction` `Knowledge`-wrapped
  (`Ambiguous` when a calibration file does not say), validity `STATIC` or a `Timestamp`. Not to be confused
  with `TransformRecord`, the provenance record. Composition and graph alignment are MVL-37.
- `GeodeticPosition`: latitude/longitude as declared, `Knowledge`-wrapped height, `CrsCode`, units and
  `HeightReference` (`ellipsoid`, `mean_sea_level`, `home`, `ground`).

## Versions and software identity (ADR 0014; `model/versions.py`)

- One type per kind: `GitCommit`, `SemanticVersion`, `DeclaredVersion`, `BuildId`, `FirmwareVersion`,
  `ModelCheckpointHash`, `ContainerImageDigest`. Each is `Knowledge`-wrapped on `SoftwareConfiguration`.
- Kinds never compare equal and never sort together. Only `SemanticVersion` is ordered (SemVer precedence).
- Stored verbatim: no `v` stripping, no case folding. Semver prerelease and build are views of the full text.
- A value takes a kind because the source says so, never because it looks like one. Otherwise it is a
  `DeclaredVersion`. JSON: `{"kind":"semver","value":"1.2.3-rc.1"}`.

## Serialization (ADR 0002)

- Entities: JSON Lines, one table per entity kind, canonical JSON (sorted keys, fixed number formatting) so
  bytes are hashable and diffable.
- Time-series: Parquet, row groups sized for range queries, sorted by (domain, ticks).
- Raw evidence: content-addressed blobs, byte-identical to source.
- `SCHEMA_VERSION` constant on every record; compatibility policy defined in MVL-1.

## Schema examples

MVL-1 ships worked examples for a drone (PX4), a quadruped (ROS 2), a manipulator (MCAP) and a mobile robot
(ROS 1) under `tests/fixtures/model/`.

# Canonical data model

Status: **draft** — primitives are specified by MVL-2 / MVL-40 / MVL-4 / MVL-3; entities by MVL-1. This page
records the decisions already made so those issues start from a shared baseline. It becomes authoritative when
MVL-1 merges.

## Entities (v0)

| Family | Entities |
|---|---|
| Source | `SourceArtifact`, `SourceRevision` |
| Run / experience | `Run`, `Stream`, `TimestampDomain` |
| Machine | `Machine`, `HardwareConfiguration`, `SoftwareConfiguration`, `Calibration` |
| World / record | `Site`, `Asset`, `SpatialArtifact`, `StructuredRecord`, `DocumentRecord` |
| Task | typed records defined in MVL-33 (`TaskBrief`, `SOPSection`, `Requirement`, `WorkOrder`) |
| Cross-cutting | `EvidenceRef`, `TransformRecord`, `Provenance`, `IngestFinding`, `IngestReceipt` |

Naming decisions: `IngestFinding` (not `IntegrityFinding`). No `ProvenanceEdge` entity in v0 — provenance is
embedded on each record. `EpisodeCandidate` / `Observation` are **reserved names, not modelled**: they are the
boundary to the memory learner.

## Epistemic states — `Knowledge[T]` (ADR 0004, MVL-40)

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

## Time (ADR 0005, MVL-4)

- `TimestampDomain` is an entity: clock identity, epoch/reference, resolution, monotonicity, source evidence.
- `Timestamp = (ticks: int, domain_id)`. Never floats. Never bare.
- MCAP `log_time` and `publish_time`, ROS `header.stamp` and receive time, PX4 boot-time and GPS time are
  separate domains. Mappings between domains are `ClockAlignment` records produced in MVL-36 with method,
  evidence and error bounds.

## Units (MVL-4)

Stored as declared by the source, or `Unknown`. SI normalisation is a derived transform with provenance.

## Frames (ADR 0007, MVL-4)

`FrameRef = (frame_id, frame_graph_id)`. No assumed ENU / NED / REP-103. Transforms are `TransformRecord`s
with parent, child, domain and provenance. Frame-graph alignment is MVL-37.

## Versions and software identity (MVL-4, MVL-27)

Git SHA, semver, build id, firmware version, model checkpoint hash, container digest are distinct typed
primitives, each `Knowledge`-wrapped on `SoftwareConfiguration`.

## Serialization (ADR 0002)

- Entities: JSON Lines, one table per entity kind, canonical JSON (sorted keys, fixed number formatting) so
  bytes are hashable and diffable.
- Time-series: Parquet, row groups sized for range queries, sorted by (domain, ticks).
- Raw evidence: content-addressed blobs, byte-identical to source.
- `SCHEMA_VERSION` constant on every record; compatibility policy defined in MVL-1.

## Schema examples

MVL-1 ships worked examples for a drone (PX4), a quadruped (ROS 2), a manipulator (MCAP) and a mobile robot
(ROS 1) under `tests/fixtures/model/`.

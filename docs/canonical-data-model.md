# Canonical data model

Status: **draft**. Primitives are specified by MVL-2 / MVL-40 / MVL-4 / MVL-3, and the record envelope by
MVL-66 (ADR 0017). Domain entities are specified by MVL-67 / MVL-68 / MVL-69. This page becomes authoritative
when MVL-1 closes.

## Record kinds (v0)

Every record kind belongs to one family (ADR 0017 §4). The last four families are the design contract's source
domains.

| Family | Record kinds | Where |
|---|---|---|
| `source` | `SourceArtifact`, `SourceRevision`, `SourceAbsence` | `model/source.py` (ADRs 0009, 0010) |
| `lineage` | `TransformRecord` | `model/provenance.py` (ADR 0016) |
| `finding` | `IngestFinding` | `model/finding.py` |
| `reference` | `TimestampDomain`, `FrameGraph`, `Frame`, `FrameTransform` | `model/reference.py` |
| `run` | `Run`, `Stream` | MVL-67 |
| `machine` | `Machine`, `HardwareConfiguration`, `SoftwareConfiguration`, `Calibration` | MVL-68 |
| `world` | `Site`, `Asset`, `SpatialArtifact`, images, `DocumentRecord`, `StructuredRecord` | MVL-69 |
| `task` | `TaskBrief`, `SOPSection`, `Requirement`, `WorkOrder` | reserved for MVL-33 |

`IngestReceipt` is the package-level account of an ingest run, not a record table. Its layout belongs to MVL-5.

Naming decisions: `IngestFinding` (not `IntegrityFinding`). No `ProvenanceEdge` entity in v0 — provenance is
embedded on each record. `EpisodeCandidate` / `Observation` are **reserved names, not modelled**: they are the
boundary to the memory learner.

## Records and the envelope (ADR 0017; `model/record.py`)

- Every record's JSON carries `kind` (its table) and `schema_version`. Readers check the version first and
  refuse any other one, so a newer record fails with a version error, never a key error.
- **Evidence records** hold `id` and one record-level `Provenance`. The id is
  `evidence_record_id(kind, provenance.evidence, transform)`; `check_evidence_record_id` verifies it.
  Several records of one kind from one piece of evidence need finer locators, never counters.
- **Ledger records** (`source_*`, `transform_record`) and **findings** derive their ids from their content and
  carry no record-level provenance.
- A canonical record always has one record-level `EvidenceRef`: the evidence that declares it. A record that
  exists only because a procedure combined several sources is inferred and lives in `derived/`.
- A value defined by a format specification (MCAP `log_time` is ns) cites the bytes that establish the format
  plus the transform that applies the spec. When the source carries the definition itself (a ROS message
  definition in an MCAP schema record), it cites that instead.
- `SCHEMA_VERSION` is 0 until the M1 gate, then 1. From then on every shape change bumps it through an ADR,
  and older records are read through lossless, read-time migrations that keep ids. Stored packages are never
  rewritten. A change that loses information is a new adapter version, not a migration.
- Records are frozen standard-library dataclasses with strict hand-written JSON; there is no modelling library.
  The JSON Schema is generated from them (MVL-70).

## Findings (ADR 0017 §9; `model/finding.py`, `identity/findings.py`)

- `IngestFinding(code, category, severity, subject, transform, message, details, related, records)`.
- `code` is `<producer>.<name>`. `category` is one of `corrupt`, `unsupported`, `unrepresentable`,
  `missing`, `ambiguous`, `inconsistent`, `skipped`, `limit` or `failed`.
- `severity` is judged by what reached the output: `error` means evidence was lost, `warning` means a value is in
  doubt, and `info` means nothing was lost.
- `subject` is an `EvidenceRef`, or a location when there are no bytes to cite (an unreadable directory).
- Build findings with `identity.findings.ingest_finding`. The id covers the whole content, so identical findings
  are one finding. Messages and details are deterministic: no wall-clock, hosts, absolute paths or addresses.

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
- `TimestampDomain` is a record (`model/reference.py`) that carries provenance. It has a structural `field` and
  `scope` (where the ticks are read, verbatim) and `Knowledge`-wrapped `role` (receive / publish / sample /
  document), `resolution` (exact `Fraction` seconds per tick), `epoch`, `timescale` and `declared_monotonic`.
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

## Frames and spatial references (ADR 0007, ADR 0015; `model/frames.py`, `model/spatial.py`, `model/reference.py`)

- `FrameRef = (frame_id verbatim, frame_graph_id)`. Graphs are source-scoped `FrameGraph(id, provenance, scope)`
  records. The `Frame` record holds `Knowledge`-wrapped `axes` (named conventions
  `enu ned nwu flu frd rdf rub ruf fru`) and `handedness`. There is no default; REP-103 is not evidence.
- Rotations (`Quaternion`, `RotationMatrix`, `EulerAngles`, `RotationVector`), `Translation`, `Pose` and
  `HomogeneousMatrix` keep their float components in source order. Order, layout, Euler sequence/mode,
  quaternion algebra and units are separate `Knowledge` fields, so "undeclared" keeps the numbers.
- `FrameTransform(id, provenance, parent, child, direction, value, validity)`: one graph, `direction` `Knowledge`-wrapped
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
- Every record carries `kind` and `schema_version` (ADR 0017 §2, §7).
- NaN and ±Infinity never appear in JSON. A record field that may hold them is typed `Real`, and a non-finite
  value is written `{"non_finite":"inf"}` (`model/scalars.py`, ADR 0017 §8). Parquet keeps IEEE values.

## Schema examples

MVL-1 ships worked examples for a drone (PX4), a quadruped (ROS 2), a manipulator (MCAP) and a mobile robot
(ROS 1) under `tests/fixtures/model/`.

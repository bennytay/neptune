# Architecture

Status: foundational shape agreed 2026-09-30 (see `audit-2026-09-30.md`). Package contents fill in as M1–M2 land.

## Where Neptune sits

```
raw physical / engineering data
        ↓
ROBOTICS INGESTION + CANONICAL REPRESENTATION      ← this project
        ↓
memory learner → persistent multimodal memory → retrieval / context engine
        ↓
LLM agents · VLAs / policies · simulators · eval · training · engineers
```

The ingestion layer answers one question: **what exactly exists in this evidence?** It does not decide what the
evidence *means*. Failures, capabilities, causes, patterns and operating envelopes are later-layer inferences.

## Two kinds of output, kept apart

| | Evidence layer (`model/`) | Derived layer (`derived/`) |
|---|---|---|
| Produced by | deterministic parsers | models, heuristics, inference |
| `assertion_kind` | `observed`, `stated` | `inferred` |
| Examples | a Stream's declared schema; a URDF joint; a PDF span | "this topic is an IMU"; a caption; an embedding |
| Stability | changes only with parser version | may be regenerated freely |

The split is a package boundary, not a convention. `model/` never imports `derived/`; derived records carry
their own provenance and point at evidence records, never the reverse.

## Four input domains

Every source is attributed to one or more of: **machine context** (embodiment, sensors, calibration, software,
checkpoints), **world / record context** (sites, assets, maps, inspections, rules, unknowns), **task context**
(briefs, SOPs, requirements, success criteria), **run / experience evidence** (timestamped observations and
actions). They share primitives and provenance but are modelled as distinct entity families; nothing collapses
them into a generic document store.

## Package layout and boundaries

```
model/      canonical IR. Pure data + validation. Imports: nothing internal.
identity/   hashing, canonical JSON, id derivation.            Imports: model
discovery/  Source interface, walk, fingerprint, probe, group. Imports: model, identity, adapters (registry only)
adapters/   contract + registry + one subpackage per format.  Imports: model, identity. Never each other.
runtime/    phases, scheduling, resume, cache, sandbox.       Imports: everything above
store/      on-disk ingest package.                           Imports: model, identity
validate/   cross-source checks over the store.               Imports: model, store
derived/    inferred annotations.                             Imports: model
manifest/   user override schema.                             Imports: model
cli/        thin wrapper over the SDK.
```

Rules: `model/` is frozen after the M1 gate — changes require an ADR. Adapters are leaves. Nothing reaches into
an adapter's internals; the runtime only sees the four-method contract (`adapter-contract.md`).

## Data flow

```
discover → fingerprint → probe → group → plan → ingest (chunks) → normalise → store → validate → receipt
```

Details and milestone ownership of each stage: `ingestion-pipeline.md`.

## Key decisions (ADR index)

| ADR | Decision |
|---|---|
| 0001 | Python + uv; native-extension escape hatch reserved, not used |
| 0002 | Ingest package = directory: JSON Lines entities (canonical JSON), Parquet time-series, content-addressed raw blobs |
| 0003 | Three identity tiers: content, derived-record (lineage-scoped), logical (declared) |
| 0004 | `Knowledge[T]` epistemic wrapper with a field-scope rule |
| 0005 | Timestamps = integer ticks + `TimestampDomain` id; no silent UTC |
| 0006 | Embedded provenance `(EvidenceRef, TransformRecord id, assertion_kind)`; `Locator` tagged union |
| 0007 | `FrameRef` = (frame id, frame-graph id); no default convention |
| 0008 | Four-method adapter ABI; runtime owns resume, validation, explanation |

ADRs are written in MVL-55; until then this table is the authoritative summary.

## Scale posture

Inputs may be hundreds of GB. Everything is designed as: cheap `inspect` before expensive `ingest`; chunked,
deterministic, resumable transforms; indexed random access (Parquet row groups) so a consumer can ask for
`/imu` between two timestamps without touching the rest; content-addressed caching keyed by
`(source id, adapter version, config hash, chunk id)`; zero-copy references to local files; a `Source`
interface so object stores are implementations, not refactors.

## Security posture

All input is untrusted. The adapter contract is designed so an adapter can run in a resource-limited
subprocess without contract changes (MVL-10). Path/symlink/archive policies live in `discovery/`, not in
adapters. See `security.md`.

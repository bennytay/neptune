# Provenance and identity

Status: decisions agreed (ADR 0003, ADR 0006); implementation in MVL-2 and MVL-3.

## Three identity tiers — never conflated

| Tier | Identifies | Derived from | Survives parser upgrade? |
|---|---|---|---|
| 1. Content id | source bytes | sha256 of bytes; per-chunk hashes for large files | yes |
| 2. Derived-record id | one canonical record | (source id, locator, adapter id, adapter version, config hash) | **no — by design**; new parser ⇒ new lineage |
| 3. Logical id | a real thing across sources (this robot, this site) | declared: serial number, manifest entry, explicit alias | yes |

Consequences:
- Idempotence is free: identical bytes ⇒ identical tier-1 and tier-2 ids.
- Parser lineage is free: a new adapter version produces new tier-2 ids beside the old ones; nothing is mutated.
- Downstream references must point at tier 1, tier 3, or `EvidenceRef`s — never at tier-2 ids.
- Two robots with byte-identical URDFs are two robots. Logical identity is never inferred from content equality;
  conservative resolution with explicit unresolved state is MVL-35.

## External sources

Object-store and connector sources carry `(connector id, external object id, revision/etag/version)` in
addition to a content hash when bytes are fetched. `SourceRevision` records a change; history is never mutated.

## Provenance record

Every canonical record embeds:

```
Provenance
  evidence:       EvidenceRef            (source id + Locator)
  transform:      TransformRecord id     (adapter id, version, config hash, tool versions)
  assertion_kind: observed | stated | inferred
```

- `observed`: directly decoded from the source (a message field, a URDF joint).
- `stated`: the source explicitly asserts it about something else (a register row says asset A has defect D).
- `inferred`: produced by a model or heuristic. Lives in `derived/`, never in `model/`.

## Locators

`Locator` is a tagged union; each variant has one meaning:

| Variant | Fields |
|---|---|
| `ByteRange` | offset, length |
| `RecordRange` | topic/channel, first ticks, last ticks, domain |
| `Page` / `Span` | page, bbox / char offsets |
| `RowCell` | row index, column |
| `ImageRegion` | bbox, coordinate convention |
| `VideoFrame` | frame index, ticks, domain |
| `JsonPointer` | RFC 6901 pointer |
| `Frame` | frame id, graph id |
| `Object` | mesh/spatial object id |

Adapters may add variants; they may not reuse an existing one with different semantics.

## Explaining a value

`explain(value)` (MVL-39) walks the embedded provenance to the exact source location and transform chain.
Aggregates (a `Run`) report coverage: how many constituent values are observed / stated / inferred / unknown.

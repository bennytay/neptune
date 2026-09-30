# Provenance and identity

Status: identity implemented (MVL-2, ADR 0009); provenance agreed (ADR 0006), implementation in MVL-3.

## Three identity tiers — never conflated

| Tier | Identifies | Derived from | Survives parser upgrade? |
|---|---|---|---|
| 1. Content id | source bytes | sha256 of bytes; per-chunk hashes for large files | yes |
| 2. Derived-record id | one canonical record | (record kind, source id, locator, adapter id, adapter version, config hash) | **no — by design**; new parser ⇒ new lineage |
| 3. Logical id | a real thing across sources (this robot, this site) | declared: serial number, manifest entry, explicit alias | yes |

Consequences:
- Idempotence is free: identical bytes ⇒ identical tier-1 and tier-2 ids.
- Parser lineage is free: a new adapter version produces new tier-2 ids beside the old ones; nothing is mutated.
- Downstream references must point at tier 1, tier 3, or `EvidenceRef`s — never at tier-2 ids.
- Two robots with byte-identical URDFs are two robots. Logical identity is never inferred from content equality;
  conservative resolution with explicit unresolved state is MVL-35.

## Id strings (ADR 0009)

| Id | Rendering | Code |
|---|---|---|
| content id, chunk hash | `sha256:<64 hex>` | `identity.hashing.digest_stream`, `content_id` |
| config hash | `sha256:<64 hex>` of the resolved config's canonical JSON | `identity.ids.config_hash` |
| record id | `rec:sha256:<64 hex>` | `identity.ids.adapter_record_id`, `record_id` |
| logical id | `{"namespace", "value"}` | `model.ids.LogicalId` |

Canonical JSON (ADR 0002) is `identity.canonical_json`; `dumps` for hashing and storage, `loads` rejects
anything not byte-canonical.

## Sources, revisions and dedup

- `SourceArtifact` = one distinct byte string: content id, size, 8 MiB chunk hashes.
- `SourceRevision` = one location seen holding one artifact. Revisions of a location form a hash chain via
  `supersedes`; nothing is ever mutated or removed.
- `identity.revisions.SourceLedger` applies the policy: same bytes anywhere ⇒ one artifact; same location
  and bytes ⇒ nothing new; changed bytes ⇒ new revision; rename ⇒ new location, no new artifact.
- Locations are `LocalPath` (relative to the ingest root) or `ExternalObjectRef`
  `(connector id, object id, revision token)` for object stores (MVL-45). A new token over identical bytes is
  not a new revision.

## Walking local sources

`discovery.source.LocalSource` implements the `Source` protocol (`walk`, `open`). It yields regular files only,
never follows symlinks, never opens FIFOs/sockets/devices, and reports everything it skips as a
`SkippedEntry` with a reason. Details: ADR 0009 §5.

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
| `Page` / `Span` | page, bbox / code-point offsets |
| `RowCell` | row index, column |
| `ImageRegion` | bbox, coordinate convention |
| `VideoFrame` | frame index, ticks, domain |
| `JsonPointer` | RFC 6901 pointer |
| `Frame` | frame id, graph id |
| `Object` | mesh/spatial object id |

All indices are 0-based and ranges half-open; image regions use the stored raster orientation (no EXIF
rotation). Adapters may add variants namespaced `<adapter id>:<name>`; they may not reuse an existing one with
different semantics. Full rules: ADR 0006.

## Explaining a value

`explain(value)` (MVL-39) walks the embedded provenance to the exact source location and transform chain.
Aggregates (a `Run`) report coverage: how many constituent values are observed / stated / inferred / unknown.

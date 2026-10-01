# Provenance and identity

Status: identity implemented (MVL-2, MVL-59; ADRs 0009, 0010); provenance implemented (MVL-3; ADRs 0006, 0016);
record envelope and findings implemented (MVL-66; ADR 0017); series row provenance implemented (MVL-67; ADR 0018).

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
- A declaration's ids for one thing are kept together, each cited, as `identifiers` on `Machine`,
  `HardwareComponent`, `Site` and `Asset` (ADRs 0019 §2, 0020 §1). They are the evidence MVL-35 links by.

## Id strings (ADR 0009)

| Id | Rendering | Code |
|---|---|---|
| content id, chunk hash | `sha256:<64 hex>` | `identity.hashing.digest_stream`, `content_id` |
| config hash | `sha256:<64 hex>` of the resolved config's canonical JSON | `identity.ids.config_hash` |
| record id | `rec:sha256:<64 hex>` | `identity.ids.adapter_record_id`, `record_id` |
| evidence record id | record id over kind + record-level evidence + transform | `identity.provenance.evidence_record_id`, `check_evidence_record_id` |
| transform / finding id | record id over the record's own content | `identity.provenance.transform_record`, `identity.findings.ingest_finding` |
| logical id | `{"namespace", "value"}` | `model.ids.LogicalId` |

Canonical JSON (ADR 0002) is `identity.canonical_json`; `dumps` for hashing and storage, `loads` rejects
anything not byte-canonical.

## Sources, revisions and dedup

- `SourceArtifact` = one distinct byte string: content id, size, 8 MiB chunk hashes.
- `SourceRevision` = one location seen holding one artifact. Revisions of a location form a hash chain via
  `supersedes`; nothing is ever mutated or removed.
- `SourceAbsence` = a location that held bytes observed holding none; it supersedes the last revision.
- `identity.revisions.SourceLedger` applies the policy: same bytes anywhere ⇒ one artifact; same location
  and bytes ⇒ nothing new; changed bytes ⇒ new revision; rename ⇒ new location, no new artifact, and the old
  location becomes absent; bytes reappearing ⇒ new revision superseding the absence.
- The workspace keeps each root's ledger across jobs, history and all. A package lists only its own job's
  scan: the artifacts, one revision per location holding bytes (each its chain's first), no absences
  (ADR 0035 §9). So the same folder gives the same package whatever earlier jobs or dry runs saw.
- Locations are `LocalPath` (relative to the ingest root), `RawLocalPath` (the same, for names that are not
  valid UTF-8, kept as exact bytes) or `ExternalObjectRef` `(connector id, object id, revision token)` for
  object stores (MVL-45). A new token over identical bytes is not a new revision.

## Walking local sources

`discovery.source.LocalSource` implements the `Source` protocol (`walk`, `open`). It yields every regular file
(including non-UTF-8 names), yields each symlink with its byte-exact target without following it, never opens
FIFOs/sockets/devices, and reports everything else as a `SkippedEntry` with a reason.

`discovery.scan.scan()` is one pass: walk, digest, record, then mark absent only the locations the scan could
see are gone — never under an unreadable directory or a symlinked ancestor. Details: ADR 0010.

## Probing sources

`discovery.probe.ProbeEngine` (ADR 0027) decides which adapter reads a source from its bytes alone: it sniffs the
head (signatures and text class, observations only), gives the head to every registered adapter with crashes
isolated, and applies the registry's selection rule (ADR 0024 §7). A zip, tar, gzip, bzip2 or xz is inspected
within `ProbePolicy` and its members probed the same way; members are cited as `ByteRange` steps, nested per
ADR 0016, and nothing is extracted. A tie, an unclaimed source, a crashed probe, a misleading name and every
container problem is an `IngestFinding` from the engine's own transform (`neptune.probe`), so the receipt shows
who looked at a source nobody decoded. `SourceProbe.to_json()` is the explanation a dry run renders.

## Provenance record (`model/provenance.py`, ADR 0016)

Every canonical record embeds, and any `Knowledge` state may carry:

```
Provenance
  evidence:       EvidenceRef            (source content id + locator path)
  transform:      TransformRecord id
  assertion_kind: observed | stated
```

- `observed`: directly decoded from the source (a message field, a URDF joint).
- `stated`: the source explicitly asserts it about something else (a register row says asset A has defect D).
- `inferred`: produced by a model or heuristic. Only `derived.provenance.InferredProvenance` can say so, and
  neither mypy nor the runtime lets it onto a canonical `Knowledge` state.

Record-level provenance cites exactly one `EvidenceRef`: the evidence that declares the record. The record's id
is `evidence_record_id(kind, provenance.evidence, transform)` (ADR 0017 §5). A value that a format specification
defines, such as MCAP `log_time` being nanoseconds, cites the bytes that establish the format (magic, header,
root element) with the transform that applies the spec, or the definition itself when the source carries it
(ADR 0017 §6).

`TransformRecord(id, adapter_id, adapter_version, config_hash, config, libraries, upstream)` holds nothing
host-specific. `upstream` names the transforms whose output it consumed, so a normalised value's chain is
`adapter → normaliser`, hash-linked. Build and verify records with `identity.provenance.transform_record` /
`check_transform_record`, and derive tier-2 ids with `evidence_record_id(kind, evidence, transform)`: ADR 0003's
formula, plus `upstream` for chained transforms.

## Series rows (ADR 0018)

A series row carries no `Provenance` of its own. Its `Stream` hoists what every row shares: the source, the
assertion kind and a locator template, with the stream's own transform. The row's `locator/<i>/<field>`
columns fill the template, so `Stream.row_provenance(row)` rebuilds the row's full provenance exactly. A
series file's metadata holds the `Stream` line, so the file alone is enough.

## Table cells (ADR 0020 §5)

A register row cited as `Row(r)` hoists its cells' citations the way a series hoists its rows': cell `c`
is `RowCell(r, c, header[c])`, rebuilt by `StructuredRecord.cell_evidence`. A row cited any other way
gives each cell its own provenance.

## Locators

A locator is a **path** of steps, outermost first. Step 0 addresses the source's stored bytes; each later step
addresses inside what the transform decoded from the previous one, e.g. `[ByteRange(gzip), JsonPointer]` or
`[Page(3), Span(10, 40)]`. A whole source is `ByteRange(0, size)`.

| Step (JSON kind) | Fields |
|---|---|
| `ByteRange` (`byte_range`) | offset, length |
| `RecordRange` (`record_range`) | channel, start, end, domain_id |
| `Page` (`page`) | index (document order, not the label) |
| `PageRegion` (`page_region`) | page, x0 y0 x1 y1 in the page's stored coordinate system |
| `Span` (`span`) | start, end code points of the transform's extracted text |
| `Row` (`row`) / `RowCell` (`row_cell`) | row (header rows counted), column, column_name (verbatim, omitted if no header) |
| `ImageRegion` (`image_region`) | x0 y0 x1 y1 pixels, stored orientation |
| `VideoFrame` (`video_frame`) | track, index (presentation order), pts, domain_id |
| `JsonPointer` (`json_pointer`) | RFC 6901 pointer |
| `FrameLocator` (`frame`) | `FrameRef` |
| `ObjectLocator` (`object`) | object_id |

All indices are 0-based and ranges half-open; image regions use the stored raster orientation (no EXIF
rotation). Adapters may add steps namespaced `<adapter id>:<name>` with flat scalar fields (`adapter_locator`);
they may not reuse a core step with different semantics. Full rules: ADRs 0006, 0016.

## Explaining a value

`explain(value)` (MVL-39) walks the embedded provenance to the exact source location and transform chain.
Aggregates (a `Run`) report coverage: how many constituent values are observed / stated / inferred / unknown.

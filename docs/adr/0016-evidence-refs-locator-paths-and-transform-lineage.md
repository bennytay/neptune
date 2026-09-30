# 0016 — Evidence refs, locator paths and transform lineage

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-3

## Context

ADR 0006 fixed embedded provenance, `EvidenceRef = (source content id, Locator)`, the locator variants and their
conventions, the contents of `TransformRecord`, and the rule that `inferred` cannot appear in `model/`. It left
MVL-3 two things: how nested evidence is represented, and the exact field types of each variant.

MVL-3's acceptance adds a requirement that ADR 0006 does not cover. A value that has been **normalised** (an
SI value derived from a declared `deg/s`) must still trace to its exact source location **and to the whole
transform chain**. The lineage must also stay intact across adapter upgrades. `Provenance` names a single
transform, so something has to carry the chain.

## Decision

1. **A locator is a path.** `EvidenceRef(source, locator)` holds a non-empty tuple of steps, outermost first.
   - Step 0 addresses the source's stored bytes (compressed if the source is compressed, as in ADR 0006).
   - Each later step addresses inside what the transform decoded from the previous step. Examples:
     `[ByteRange(gzip stream), JsonPointer]`, `[ByteRange(MCAP chunk), mcap:message]`, `[Page(3), Span(10, 40)]`.
   - A whole source is cited as `ByteRange(0, size)`. There is no "whole source" step, because a blank
     citation is too easy to write.
   - In tier-2 ids (ADR 0003), the `locator` input is the JSON array of steps (`EvidenceRef.locator_json()`).
2. **Core steps** (`neptune.model.provenance`). JSON is flat and tagged with `"kind"`. Positions are 0-based
   ints below 2^63, and ranges are half-open with empty ranges allowed. Names are verbatim, may be empty and may
   be any valid Unicode.

   | Step (JSON kind) | Fields | Notes |
   |---|---|---|
   | `ByteRange` (`byte_range`) | `offset`, `length` | bytes of the scope |
   | `RecordRange` (`record_range`) | `channel`, `start`, `end`, `domain_id` | ticks in the log's index domain; one record is `[t, t+1)` |
   | `Page` (`page`) | `index` | position in document order, never the page label |
   | `PageRegion` (`page_region`) | `page`, `x0 y0 x1 y1` floats | the page's own coordinate system as stored: PDF default user space, `/Rotate` not applied, no crop-box shift |
   | `Span` (`span`) | `start`, `end` | code points of the text the transform extracted from the scope |
   | `Row` (`row`) | `row` | counts every parsed record, header rows included |
   | `RowCell` (`row_cell`) | `row`, `column`, `column_name` | header text verbatim (`""` if blank); omitted when the table has no header (`NO_HEADER`) |
   | `ImageRegion` (`image_region`) | `x0 y0 x1 y1` ints | stored raster, origin top-left, EXIF not applied |
   | `VideoFrame` (`video_frame`) | `track`, `index`, `pts`, `domain_id` | presentation order; `pts` in the track's domain |
   | `JsonPointer` (`json_pointer`) | `pointer` | RFC 6901, syntax checked; `""` is the whole document |
   | `FrameLocator` (`frame`) | `ref` (`FrameRef`) | lineage-scoped, because graph ids are tier 2 |
   | `ObjectLocator` (`object`) | `object_id` | the id the artifact gives the object; the descriptor says which |

   `AdapterLocator` is the extension point: kind `<adapter id>:<name>` with flat scalar fields. Anything deeper
   is a further step. Parsing is strict: unknown kinds, missing or extra keys, bools for ints and ints for floats
   are all errors.
3. **`Provenance(evidence, transform, assertion_kind)`** matches ADR 0006 §1. It implements `Grounding`, so it
   fills the provenance slot of every `Knowledge` state without changing ADR 0011's JSON.
4. **`TransformRecord(id, adapter_id, adapter_version, config_hash, config, libraries, upstream)`.**
   - `config` is the resolved config, stored inline. `libraries` maps each output-affecting dependency to its
     version.
   - **`upstream`** holds the ids of the transforms whose output this one consumed, in the order it consumed
     them. It is empty for an adapter reading source bytes. A normaliser's record names the adapter
     transform, so the chain is hash-linked like `SourceRevision.supersedes` (ADR 0009), and each record still
     carries one transform id.
   - `adapter_id` names any producer, runtime components included (`neptune.si`).
   - `id = record_id("transform_record", everything but id)`. `neptune.identity.provenance` builds records and
     verifies them (`check_transform_record` recomputes the config hash and the id). Nothing host-specific is
     stored.
5. **Tier-2 ids from provenance.** `evidence_record_id(kind, evidence, transform)`:
   - For an adapter (empty `upstream`) this is exactly ADR 0003's formula. Library versions stay out.
   - For a chained transform, the same inputs plus `upstream`. A normaliser over adapter v1 and the same
     normaliser over adapter v2 therefore produce different ids, and lineage stays scoped through the chain.
   - Evidence whose bytes have not been fetched has no tier-2 id.
   - A normalised value cites the **same `EvidenceRef`** as the value it came from. The evidence is the source
     bytes, and the chain says how they were turned into this value.
6. **The `inferred` boundary is typed.** `AssertionKind` (`observed`, `stated`) lives in `model.knowledge`,
   and `Grounding` now requires `assertion_kind: AssertionKind`. `derived.provenance.InferredProvenance` has
   `assertion_kind: Literal["inferred"]` and cites one or more `EvidenceRef`s. Two checks keep it off canonical
   states:
   - mypy rejects it, because it does not satisfy the protocol;
   - a runtime guard in every `Knowledge` state rejects it.

   `model/` imports only `model/`, and a test enforces this.

## Alternatives considered

- **A recursive `Nested(outer, inner)` locator.** It is equivalent to a path, but deeper to read and to match on.
  A flat tuple is also what tier-2 ids hash.
- **Content ids for decoded members** (every archive member or decompressed chunk as its own source). Useful
  for storing members as blobs, but it breaks ADR 0006's "outermost inward" rule and needs a side table to find
  the bytes a member came from.
- **A transform chain on each `Provenance`** (`transforms: [adapter, normaliser]`). This changes ADR 0006's
  shape and repeats the chain on every record. With `upstream` the chain is stored once per transform.
- **Making `TransformRecord` ids the tier-2 input.** Simple, but it puts library versions into lineage, which
  ADR 0003 rejected.
- **`ArchiveMember(index, name)` as a core step.** Member names can repeat and need not be UTF-8. A `ByteRange`
  over the member's entry is exact, and an archive adapter can add a namespaced step if readers need names.
- **`Row` and `RowCell` merged, with an optional column.** One step would then address two granularities.
  Two steps are clearer.
- **`Grounding` left as "anything with `to_json`".** `InferredProvenance` would then type-check as canonical
  provenance, and ADR 0006 §5's "the type checker enforces" would be false.

## Consequences

- Every citation resolves with the source bytes plus the decoders the transform names.
  `tests/integration/test_provenance_lineage.py` shows this on a real CSV fixture and a gzip-nested JSON.
- `Span`, anything nested under a decoding step, and `FrameLocator` are only meaningful together with their
  transform. A citation that must outlive a lineage should use byte-level steps where they exist.
- Test doubles for `Grounding` need an `assertion_kind`.
- A config stored as a blob is not in v0. Adding it changes transform ids and needs an ADR.
- Hoisting provenance into Parquet series (ADR 0006 §6) and the entity envelope are MVL-1's. `explain` is
  MVL-39's. Both build on these types without changing them.

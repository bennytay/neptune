# 0006 — Provenance and locator model

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-55

## Context

The design contract requires every normalised value to answer six questions:

1. which source asserted it;
2. where exactly in that source;
3. which transform produced it;
4. which transform version and config;
5. whether it was observed, stated or inferred;
6. what was missing or ambiguous.

Question 6 is ADR 0004. This ADR covers 1–5.

Two design pressures pull in opposite directions. Provenance must be precise enough to point at the exact bytes,
cell, page span or image region. It must also be cheap enough to carry on billions of time-series samples.

The design contract listed a `ProvenanceEdge` entity. The audit replaced it with embedded provenance (approved
2026-09-30).

## Decision

1. **Every canonical record embeds a `Provenance`:**

   ```
   Provenance
     evidence:       EvidenceRef              where the record came from
     transform:      TransformRecord id       what produced it
     assertion_kind: observed | stated        (inferred only in derived/, see 5)
   ```

   Record-level provenance covers the record's structural fields. A field whose evidence differs from the
   record's (for example a `Stream` whose schema comes from a different MCAP record than its channel) carries its
   own provenance through its `Known` wrapper (ADR 0004). No separate per-field provenance map exists.

2. **`EvidenceRef = (source content id, Locator)`.**
   - It points at a tier-1 content id (ADR 0003), never a path, so evidence survives renames and parser upgrades.
   - For an external object whose bytes have not been fetched, the source part is the external identity triple
     instead.
   - `EvidenceRef`s are the durable way to cite evidence across lineages.

3. **`Locator` is a tagged union.** Each variant has exactly one meaning:

   | Variant | Addresses |
   |---|---|
   | `ByteRange` | offset, length in the source's stored bytes (compressed if the source is compressed) |
   | `RecordRange` | topic or channel + first/last ticks + domain id, within a log |
   | `Page` / `Span` | page index + optional bbox / code-point offsets into the transform's extracted text |
   | `RowCell` | row index + column (name as declared, and position) |
   | `ImageRegion` | pixel bbox |
   | `VideoFrame` | frame index + ticks + domain id |
   | `JsonPointer` | RFC 6901 pointer into a JSON or YAML document |
   | `Frame` | frame id + frame-graph id (ADR 0007) |
   | `Object` | object id within a mesh, CAD or spatial artifact |

   Conventions apply to all variants:
   - Indices are **0-based**. Ranges are **half-open** `[start, end)`.
   - Text offsets count **Unicode code points** of the text as the named transform extracted it. Byte offsets
     appear only in `ByteRange`.
   - Image coordinates are pixels in the **stored** raster orientation: origin top-left, x right, y down. EXIF
     orientation is *not* applied, and it is recorded as stated data if present.
   - Nested evidence (a member of an archive, a record inside a compressed chunk) is addressed from the
     outermost source inward. The exact nesting representation is MVL-3's.

   Adapters may add variants, namespaced `<adapter id>:<name>`, with semantics documented in their descriptor.
   They may never reuse a core variant with different semantics.

4. **`TransformRecord`** records what produced a record: adapter id, adapter version, config hash, the resolved
   config (inline or as a blob), and the versions of parsing libraries the adapter declares as output-affecting.
   - Its id is the hash of its canonical JSON.
   - It contains nothing host-specific: no hostname, wall-clock, interpreter build or paths. Those go in the
     receipt envelope (MVL-5), so identical toolchains produce identical records.
   - Tier-2 ids use the adapter id, version and config hash, not the `TransformRecord` id (ADR 0003).

5. **`assertion_kind`** takes three values:
   - **`observed`**: the record is a decoding of what the source bytes themselves encode. Examples: a message
     field, a URDF joint element, a CSV cell as a cell, an image's pixel dimensions.
   - **`stated`**: the source is an authored assertion about *another* entity, and the record models that other
     entity. Examples: a register row saying asset A has defect D; a PDF spec sheet giving a robot's payload;
     a manifest naming the robot of a run.
   - **`inferred`**: produced by a model, heuristic or statistical procedure.

   **`inferred` is unrepresentable in `model/`.** The canonical `Provenance` type admits only `observed` and
   `stated`. `derived/` has its own base type that admits `inferred`, and it references evidence records, never
   the reverse. The type checker enforces non-negotiable 8.

6. **Hoisting for series.** In Parquet series, the transform and `assertion_kind` are constant per series and
   are stored once, on the `Stream` record and in the file's metadata. Each row carries its own locator
   columns: for example the source content id is constant, while the byte offset or chunk/message index varies
   per row. Full per-row provenance must be reconstructable from the hoisted values plus the row. That is the
   contract `explain` (MVL-39) relies on.

7. **No `ProvenanceEdge` entity in v0.** The provenance graph is implicit in embedded references and can be
   materialised later as a derived index without changing records.

## Alternatives considered

- **A separate provenance edge table** (`ProvenanceEdge`, W3C PROV style). General and graph-native. Records
  would be meaningless without a join, so a lost or partial edge table would silently orphan evidence. Embedded
  provenance travels with the record, and the graph can be derived from it.
- **A list of evidence refs per record.** It handles records assembled from several locations. It lost because
  per-field `Known` provenance handles those cases more precisely, and a list invites vague "somewhere in these
  five places" citations.
- **String locators** (for example `"page=3;chars=10-40"`). Easy to add, impossible to validate, and every
  consumer has to parse them. A tagged union makes each addressing scheme typed and checkable.
- **Per-row JSON provenance in series.** Faithful, but it multiplies series size several times over. Hoisting
  loses no information.
- **Allowing `inferred` in `model/`, marked by the tag.** One flag is all that separates evidence from
  interpretation, and it relies on every author and consumer checking it. A type boundary does not.

## Consequences

- Any value can be traced to exact bytes and a transform without an external index, which is the precondition
  for `explain` (MVL-39) and for lineage comparison (MVL-43).
- The locator conventions (0-based, half-open, code points, stored orientation) are a public contract. Getting
  one wrong in an adapter is a correctness bug, caught by locator round-trip tests (`adapter-contract.md`).
- New addressing needs either a namespaced adapter variant or, for a general scheme, an ADR that adds a core
  variant.
- Records are larger than bare values, which is accepted for entities. Series stay compact because of hoisting.
- Revisit if consumers need cross-record provenance queries often enough to justify materialising the edge graph
  inside the package.

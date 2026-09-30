# 0017 — Canonical records: envelope, families, schema version and findings

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-66 (sub-issue of MVL-1)
- Amends: ADR 0003 (the golden-diff rule, for schema-version bumps only; see §7)

## Context

The primitives exist: identity (ADRs 0009, 0010), `Knowledge[T]` (0011), time (0012), units (0013),
versions (0014), frames (0015) and provenance (0016). MVL-1 composes them into records. Several
earlier ADRs left pieces of that composition to MVL-1:

- ADR 0001 §4: whether `model/` uses a data-modelling library.
- ADR 0002: the `SCHEMA_VERSION` compatibility and migration policy, and how NaN and ±Infinity are
  represented, since canonical JSON forbids them.
- ADR 0003: the identity inputs of records that are not produced from a single source, such as a
  `Run` assembled by grouping.
- ADRs 0012 §7, 0015 §5 and 0016: the record envelope (record-level provenance and
  `schema_version`) for `TimestampDomain`, `Frame` and `FrameTransform`.

The design contract also names `IngestFinding` as a canonical object, and every adapter from M2
on needs it: adapters return findings instead of raising (ADR 0008).

Every line of every ingest package depends on these decisions, and after the M1 gate the model
is frozen. They are decided together here. The entity kinds of each domain (runs, machine
context, world context) follow in MVL-67, MVL-68 and MVL-69 and build on this envelope.

## Decision

1. **No data-modelling library.** Records are frozen standard-library dataclasses, each with a
   hand-written `to_json` and a strict `*_from_json`, as every primitive already is.
   - The canonical JSON encoder is Neptune's own (ADR 0009 §6), so a library's serialiser would be
     bypassed anyway.
   - No dependency release can change validation or output bytes. ADRs 0013 and 0014 made the same
     call for units and SemVer.
   - Reading is strict: no coercion (`"1"` is not `1`), no ignored keys.
   - `model/` stays standard-library only.

   Python types are the source of truth. The language-neutral JSON Schema is generated from them
   (MVL-70); it is not the other way round.

2. **Envelope.** Every record's JSON carries two envelope keys:
   - `kind`: the record kind, a lowercase token that also names its table (ADR 0002);
   - `schema_version`: the `SCHEMA_VERSION` that wrote it (§7).

   Both are written by `to_json` and checked by the `*_from_json` reader. Neither is a constructor
   argument, so an in-memory record is always of the current version. No record field may be named
   `kind` or `schema_version`. `neptune.model.record` holds `envelope` and `record_object`.

3. **Three record shapes.**

   | Shape | Kinds so far | Identity | Provenance |
   |---|---|---|---|
   | Evidence record | `timestamp_domain`, `frame_graph`, `frame`, `frame_transform`; every domain entity from MVL-67 on | tier 2, from its record-level evidence (§5) | one record-level `Provenance` |
   | Ledger record | `source_artifact`, `source_revision`, `source_absence`, `transform_record` | from its own content (ADRs 0009, 0010, 0016) | none: provenance points at these |
   | Finding | `ingest_finding` | from its own content (§9) | a `subject` and the `transform` that found it |

4. **Families.** Every record kind belongs to exactly one family, declared as a class attribute:

   | Family | Holds |
   |---|---|
   | `source` | which bytes exist and where they were seen |
   | `lineage` | what produced the records (`TransformRecord`) |
   | `finding` | what went wrong, was skipped or could not be represented |
   | `reference` | the clocks and frames that times and poses are expressed in |
   | `machine` | machine context: embodiment, sensors, calibration, software (MVL-68) |
   | `world` | world / record context: sites, assets, maps, photos, documents, registers (MVL-69) |
   | `task` | task context: briefs, SOPs, requirements, work orders (MVL-33) |
   | `run` | run / experience evidence: sessions and their timestamped streams (MVL-67) |

   The last four are the design contract's source domains. No record kind straddles two, and no
   generic entity type spans them. A source is attributed to a domain by the families of the
   records that cite it; the attribution is not stored separately.

5. **Evidence records.** An evidence record holds `id` and exactly one record-level `Provenance`
   (ADR 0006 §1): one `EvidenceRef` and one transform.
   - **Id rule.** `id == evidence_record_id(kind, provenance.evidence, transform)`, which is ADR
     0003's formula plus `upstream` for chained transforms (ADR 0016 §5). Anyone holding the
     record and its `TransformRecord` can check it (`identity.provenance.check_evidence_record_id`).
   - **Finer locators.** When one piece of evidence declares several records of one kind, such as
     an MCAP channel's log time and publish time, the adapter makes the locator finer, if
     necessary with an adapter step naming the part (`mcap:time_field`). It never adds a counter.
   - **Field-level provenance** (`Knowledge` states) may cite any evidence that asserts the field.
     A `Knowledge` state with `INHERITED` provenance means the record-level provenance.
   - **Records combining several sources.** This answers ADR 0003's open question. A canonical
     record always has one record-level `EvidenceRef`: the evidence that declares it, such as a
     manifest entry or a recording container. It cites other sources in field-level provenance.
     A record that exists only because a procedure combined several sources, such as a heuristic
     session grouping, is inferred and lives in `derived/`, citing every source through
     `InferredProvenance`. Its id is `record_id(kind, inputs)` where `inputs` holds the producing
     transform's `adapter_id`, `adapter_version`, `config_hash` and `upstream`, plus `evidence`:
     the JSON of every cited `EvidenceRef`, sorted by canonical bytes and without duplicates.
   - **Clocks and frames become records.** `TimestampDomain`, `Frame` and `FrameTransform` gain
     `id` and `provenance`. A new `FrameGraph(id, provenance, scope)` record makes every
     `FrameRef.frame_graph_id` resolve to something. All four move to `neptune.model.reference`,
     because a record needs `Provenance` and provenance's locators need the time and frame
     primitives. Their readers no longer take a `decode_provenance` argument, because records
     always hold canonical provenance.

6. **Citing a format specification.** Some values are defined by a format rather than stated in
   the bytes. MCAP `log_time` is nanoseconds, and URDF lengths are metres. Such a value cites the
   bytes that establish the format, such as MCAP's magic and header or a URDF's `<robot>` element,
   together with the transform that applies the specification. When the source carries the
   definition itself, as an MCAP schema record carries a ROS message definition, that is cited
   instead. The adapter's descriptor names the specification and its version (MVL-7). Community
   convention is not a specification (ADR 0007 §4). This is also how `KnownAbsent` cites "the
   format specification defines this token as none" (ADR 0004 §5).

7. **Schema version: compatibility and migration.**
   - `SCHEMA_VERSION` is one integer for the whole canonical model: every record kind and every
     primitive's JSON shape.
   - It is **0 until the M1 gate** (MVL-56). Shapes may still change without a bump, and nothing
     has been persisted. The gate sets it to 1, and from then on the rules below apply.
   - Any change to the JSON shape or meaning of any record bumps it through an ADR. That includes
     additions such as a new field, record kind, enum member or locator step. Readers are strict,
     so an old reader must fail with "schema version N+1 is newer than this reader", never with an
     obscure key error. The reader checks the version before anything else.
   - A reader accepts records of its own version. It refuses newer ones. It reads older ones only
     through the migrations its code registers, one pure function per version step, from JSON to
     JSON. Each migration must be deterministic, total and lossless. Stored packages are never
     rewritten (non-negotiable 6); migration happens on read.
   - An upgraded record keeps its id. Ids are derived from kind, evidence and transform, which a
     lossless migration preserves, and record kinds are never renamed for the same reason.
   - A change that cannot be a lossless migration, because the old records lack the information or
     a meaning changes, is not a schema migration. Adapters bump their versions and sources are
     re-ingested, and the new lineage sits beside the old.
   - **Amendment to ADR 0003.** A `SCHEMA_VERSION` bump changes output bytes without changing
     ids. It is therefore the one golden-file diff that needs no adapter version bump. The PR that
     makes the bump regenerates the golden files and says so.

8. **Non-finite numbers** (ADR 0002 left this to MVL-1).
   - Parquet series store IEEE values natively. A sentinel that a specification defines maps to
     that specification's state (ADR 0004 §7). Any other non-finite value stays as decoded.
   - In JSON records, a field whose values come from data that may legitimately be non-finite
     (configuration values, table cells, calibration parameters) is typed `Real`: a finite float or
     a `NonFinite` value (`nan`, `inf`, `-inf`), written `{"non_finite":"inf"}`. A config's `inf`
     velocity limit is therefore `Known(inf)`, meaning unlimited, and not `Unknown`. The sign and
     payload bits of a NaN are not kept, because the locator still addresses the bytes.
   - Geometry components stay finite-only (ADR 0015 §1). A non-finite component is a finding.
   - `neptune.model.scalars` holds the type. Record fields adopt it as they are defined.

9. **`IngestFinding`.** This is Neptune's own statement about the evidence or about a location.
   - `code`: `<producer>.<name>`, stable and documented by the producer (`mcap.chunk_crc_mismatch`).
   - `category`: `corrupt`, `unsupported`, `unrepresentable`, `missing`, `ambiguous`,
     `inconsistent`, `skipped`, `limit` or `failed`.
   - `severity`, judged by what reached the canonical output:
     - `error`: some evidence produced no output;
     - `warning`: output exists but a value in it is unknown, ambiguous or in conflict;
     - `info`: nothing was lost or put in doubt.
   - `subject`: an `EvidenceRef`, or a location when there are no bytes to cite, such as an
     unreadable directory or an object that could not be fetched. Evidence subjects serialise as
     `{"kind":"evidence","ref":{…}}`, and locations as themselves.
   - `transform`: the `TransformRecord` id of what found it: an adapter, discovery, the runtime or
     a validator.
   - `message`: one deterministic line of at most 1000 characters.
   - `details`: code-specific facts, keyed by tokens.
   - `related`: other evidence involved, in evidence order.
   - `records`: the records the finding qualifies, sorted by id.

   A finding holds nothing host-specific: no wall-clock, host name, absolute path or memory
   address. Its id is `record_id("ingest_finding", everything but the id)`, so identical findings
   are one finding and any difference makes another (`neptune.identity.findings`).

## Alternatives considered

- **pydantic v2.** It generates JSON Schema and validates well. Lax coercion is its default,
  generic tagged unions such as `Knowledge[T]` need custom serialisers anyway, and a large native
  dependency whose releases can change validation or serialisation would sit under every record.
- **msgspec.** It is fast and strict, but its number formatting and key order are not the
  canonical form. It would also mean rewriting 2,500 lines of merged primitives for no gain in
  correctness.
- **`schema_version` in the manifest only.** Lines would stop being self-describing. A table read
  without its manifest could not be checked, and a store that holds several lineages may hold
  several versions.
- **Tolerant readers with major/minor versions** that ignore unknown keys and members. Ignoring
  what you do not understand is the silent assumption the whole model exists to prevent.
- **`SCHEMA_VERSION` as a tier-2 id input.** Every bump would re-lineage every record, although a
  lossless re-encoding changes no content.
- **Rewriting stored packages to migrate them.** This violates non-negotiable 6.
- **A composition envelope** (`Record[T](id, provenance, body)`). It keeps value types free of
  envelope fields, but every read site becomes `record.body.field`, and `TimestampDomain` already
  had its id as a field. Flat records read naturally.
- **Resolving the import cycle with late imports.** Type-hint-driven tooling, including the JSON
  Schema generator, would then not resolve the provenance field. Moving the records is cleaner.
- **Standard record provenance on findings.** A finding about an unreadable directory has no bytes
  to cite. A separate `SkippedLocation` kind would make two tables for one concept.
- **An explicit specification reference type** (`SpecRef(name, version, section)` as an evidence
  source). It is more explicit, but it would change `EvidenceRef` for citations that the bytes
  establishing the format plus the transform already make precise. It can be added by ADR if
  consumers need to query by specification.
- **NaN as `Unknown`**, or NaN as the string `"NaN"`. The first loses what the source said and
  turns "unlimited" into "unknown". The second cannot be told apart from text a source wrote.

## Consequences

- Every package line says what it is and which version wrote it. A reader from another version
  fails loudly and first.
- Each record kind states its family, and each evidence record can be checked against its
  transform: its id must recompute from its record-level evidence.
- `TimestampDomain`, `Frame` and `FrameTransform` moved from `neptune.model.time` and
  `neptune.model.frames` to `neptune.model.reference`. ADR 0012 §1 and ADR 0015 §2 still name
  their old modules. The primitives stay where they were.
- Adapters must document their finding codes, and findings must be deterministic like every
  other output.
- ADR 0003's golden-diff rule has one sanctioned exception.
- `SCHEMA_VERSION` becomes 1 at the M1 gate. The first migration machinery lands with the first
  bump after that.
- Revisit if consumers need to join by specification, if findings need typed details per code,
  or if a real source needs a non-finite value in a geometry field.

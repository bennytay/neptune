# 0009 — Record index: stored bodies, Unknown pointers, schema-generated projection columns and fixed ordering

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-91
- Amends: ADR 0002 §3 and §5 (the catalog now holds a projection copy of each record body, and
  new `record` columns) and its rejected alternative "store record bodies (`jsonb`)"; ADR 0002 §5's
  `ambiguous_pointers` and `record_logical_id` walks (free-form objects are skipped).
- Supersedes: ADR 0008 §4 (how a kind leaves `record_default`), by §6 below.
- Completes ADR 0007 §1's MVL-91 half.

## Context

MVL-90 writes the `record` rows ADR 0002 §5 defines. MVL-91 asks for four more things:

- the canonical JSON body as `jsonb`, so that `query` (MVL-98) and the lake can filter on fields
  that are not worth a column without re-reading packages;
- JSON pointers of `Unknown` fields beside the `Ambiguous` ones;
- columns for the hot filters (machine, site, run, stream, clock), generated from the compiler's
  JSON Schema export, so that a schema bump that adds a kind needs a generated migration, not
  hand-written SQL;
- a fixed batch size and order, so that indexing is a pure function of (package, Ledger version).
  L2's rebuild test depends on that.

ADR 0002 rejected stored bodies because `jsonb` does not keep canonical bytes and invites drift
from the package. The pointer walk also had a hole: it matched any object with a `knowledge` key.
Two record fields are free-form JSON: `transform_record.config` and `ingest_finding.details`. A
`{"knowledge": "known", "value": {"namespace": …, "value": …}}` placed inside a config would
become a logical id, and so a thread key: a guess that turns data into identity.

The catalog API docs also assign `resolve` to MVL-91 (catalog-api.md, contract `PENDING`).

## Decision

1. **`record.body jsonb`** (migration 0004) holds the record's canonical line, for projection
   only. The package line stays authoritative, `body_digest` (ADR 0005 §2) pins it, and a caller
   who needs bytes reads the package. `body` is NULL only when a string or key in the record holds
   U+0000, which `jsonb` cannot store. The body is then read from the package; it is never
   altered to fit. Like every `record` column, it is append-only. Rows registered before 0004
   would have no body and no Unknown pointers: a blank that reads as "no Unknown field". Since
   `record` refuses `UPDATE`, 0004 refuses to apply to a catalog that already holds records. Such
   a catalog is rebuilt from its packages and registration log (ADR 0002 §4).
2. **Pointer lists.** `unknown_pointers text[]` (GIN, partial) lists every field whose state is
   `Unknown`, except one inside an `Ambiguous` field's candidates, since candidates are not
   fields. One walk yields all three lists (`ambiguous_pointers`, `unknown_pointers`, logical
   ids), and it tightens what counts as a field's state:
   - It skips the record's free-form objects: top-level properties the schema types as
     `object` without naming their keys (`{"type": "object"}` or a map with
     `additionalProperties`). In package schema 2 these are `transform_record.config` and
     `libraries`, `ingest_finding.details` and `stream.metadata`. The spec lists them (`opaque`,
     §3).
   - It counts an object as a Knowledge value only if its keys are a subset of `knowledge`,
     `value`, `provenance` and `candidates`. An adapter locator step may carry a `knowledge`
     property (`AdapterLocator` admits any extra scalar), and it is not a field.
   - It does not enter an `Ambiguous` value, so no logical id inside the candidates becomes a key.

   The worked examples' rows are unchanged.
   `NotCovered`, `KnownAbsent` and `NotApplicable` are not listed: the issue names Ambiguous and
   Unknown, and `NotCovered` marks most fields of most records, so a list of them would index
   noise.
3. **Projection columns are generated.** `neptune_ledger.catalog.projection` reads the package
   schema export (`contracts/package-schema/v<version>/schema.json`) into a spec. The spec holds
   the kinds, each kind's hot-filter fields and the opaque fields. A hot filter is matched by exact
   top-level field name: `machine`, `site`, `run`, `stream`, and `clock` or `clocks`. Its shape
   decides the columns:

   | Schema shape | Columns | Value |
   |---|---|---|
   | `Knowledge_LogicalId` | `<f>_namespace`, `<f>_value` (both or neither; B-tree with `kind`) | Known only; NULL otherwise, and the package keeps the state |
   | `RecordId`, array of `RecordId` | `<f>_ids text[]` (GIN) | the ids as stated, in order |

   Package schema 1 gives `machine_*` (run, hardware_configuration, software_configuration,
   calibration), `site_*` (asset), `run_ids` (stream) and `clock_ids` (stream `clocks`, video
   `clock`). No v1 kind states a `stream` field, so there is no `stream` column yet. The first kind
   that states one gets the column from its generated migration. PostgreSQL requires every
   partition to have its parent's columns, so the columns live on the parent and only the listed
   kinds fill them, whether a kind has its own partition or lives in `record_default` (ADR 0008).
   - The spec the Ledger indexes with is committed as `catalog/projections.json`, and `index`
     reads that file, never the compiler's live schema. Projections therefore depend on the
     Ledger version only.
   - `python -m neptune_ledger.catalog.projection <schema.json>` rewrites the spec and writes
     the next migration from the difference: columns, constraint and index for each new column
     group. It never creates a partition (§6), and a new kind with no hot filter needs no
     migration. Migration 0005 is its output for package schema 1 (baseline: 0001's kinds, no
     projections). Package schema 2 adds `configuration_snapshot` and `configuration_value`,
     with no hot filter and no free-form field, so moving the spec to the v2.0.0 export writes
     no migration. Tests pin 0005 to the v1.0.0 export and `projections.json` to the v2.0.0
     export.
   - A hot-filter field in an unknown shape, or a kind, projection or free-form field that
     disappears, raises `ProjectionError`. A new shape or a removal needs an ADR. A free-form
     field that gains named properties stops matching, so it fails loudly instead of being
     walked again.
   - A generated migration refuses to apply over rows its projections would leave blank: any row
     of a kind new to the spec (a kind states its fields from the version that introduced it),
     and rows of the new schema version or later for a kind that gains a field. Such rows would
     read as not Known. Rows of older versions do not state the field, so NULL is their truth.
     The catalog is then rebuilt (ADR 0002 §4) by a Ledger that ships the migration.
   - A record of an older schema version that lacks a projected field gets NULL there.
   - The compiler's kind list is not closed. A table of a kind the spec does not name is indexed
     with the common columns and no projections, in `record_default` (ADR 0008 §1, §3).
4. **Order and batches.** `package_rows` orders records by `(kind, record_id, line)`.
   Registration writes `record` and `record_logical_id` in batches of `BATCH_ROWS = 1000` rows per
   `executemany`, each batch built when it is sent. Every column except `registration_key` is a
   function of the package and the Ledger version. Registering the same packages in any order gives the same rows apart from
   `registration_key` and the transaction columns. Registering them in the same order into two
   empty Ledgers gives identical tables apart from host times (ADR 0007 §5).
5. **`resolve`** reads `package_source`, `source`, `source_location`, `location_absence` and
   `record` at one catalog point (`registration_key <= as_of`). `fetch` is in registration order.
   Each referenced package's locations are its revisions of the content that no revision or
   absence in the same package supersedes, in `source_revision` line order (ADR 0006 §5). A
   materialised package's route is its `blob_path`, with no locations (catalog-api.md).
   `cited_by` lists records whose `(source_content_id, source_locator)` equals the anchor's
   canonical JSON exactly. Ingest findings are left out: their row's anchor is their subject, not
   a record-level `EvidenceRef` (the contract's `evidence_anchor`). `region.addressing` is the
   innermost step's `kind` for the schema's core steps and `adapter` otherwise. An anchor
   outside the contract, or an `as_of` that is not an integer of at least 1 (a `bool` is not),
   is `invalid_request`.
6. **A kind leaves `record_default` only in a rebuild** (supersedes ADR 0008 §4). Moving rows out
   of the default inside a migration does not work. `CREATE TABLE … PARTITION OF record FOR
   VALUES IN (k)` raises a check violation while `record_default` holds rows of `k`. The
   append-only trigger refuses the `DELETE` that would empty it. `record_logical_id`'s foreign
   key blocks `DETACH`. Only `session_replication_role = replica`, which needs a superuser,
   bypasses the triggers. So:
   - No generated migration creates a partition. Projection columns live on the parent, so a
     kind needs no partition of its own to be projected.
   - A hand-written migration that gives a kind its own partition first refuses, with a
     `RAISE`, if `record_default` holds any row of that kind. It applies cleanly to an empty
     catalog, which is how a rebuild starts. A catalog that already holds the kind is rebuilt
     from its packages and registration log (ADR 0002 §4) by the Ledger that ships the
     migration. That is a new catalog lineage when the Ledger version changes, by design.
   - Moving rows in place, under a superuser with triggers disabled, is not a supported
     procedure. It would bypass the append-only guarantee that the rebuild guarantee rests on.

## Alternatives considered

- **A projection table per kind** (`record_run_projection`, …). Rejected. It adds a second
  append-only table per kind to join and keep consistent. Partition pruning on `kind` already
  narrows a filter to the kinds that fill a column, and a NULL costs one bit.
- **Derive the spec from `neptune.model.schema` at run time.** Rejected. Indexing would then
  depend on the installed compiler, and a kind added upstream would be written before its
  partition and columns exist.
- **Hand-written projection SQL.** Rejected by the issue: a schema bump must not need it.
- **One canonical-JSON column per logical id.** Rejected. Two columns match `DeclaredKey` and
  give one B-tree for "this machine's records".
- **Store the body as text (canonical bytes).** Rejected. The package already holds those bytes,
  and text gives no operators to project with.
- **Escape or drop U+0000 to fit `jsonb`.** Rejected. That alters evidence, and a NULL body with
  the package authoritative loses nothing.
- **Keep walking free-form objects.** Rejected. A config value would become a thread key.
- **Generated migrations create a partition per new kind** (this ADR's first draft). Rejected:
  it fails whenever `record_default` already holds the kind (§6), which ADR 0008 now allows.
- **Split a kind out of the default in place, as a superuser with triggers off.** Rejected (§6).

## Consequences

- The `record` table roughly doubles in size, by the body. ADR 0005's lookups never read `body`,
  and the scale harness does not write it, so its query timings stand. Registration cost per
  record grows by the body's parse. Re-measure at the L2 gate.
- A package-schema bump that adds a kind or a hot-filter field is one command plus review of
  the generated migration. One that changes a hot-filter shape needs an ADR first.
- `query` (MVL-98) and threads (MVL-92) can filter on the projection columns and `body`. They
  never treat `body` as authoritative.
- Revisit if a hot filter needs a nested field (today only top-level fields are matched), or if
  `jsonb` bodies become a storage problem at 10⁶ packages.

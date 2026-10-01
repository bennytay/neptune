# 0002 — Catalog data model: package registry, record and source indexes, transform lineage, tenancy

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-86

## Context

Every later Ledger component (registration, entity threads, the lineage current-view, the lakehouse,
the catalog API) writes into or reads from the catalog. The catalog must therefore be a deterministic
function of the packages registered, so it can be rebuilt from packages alone (ADR 0001 §3), and it
must hold no fact the packages do not state. Packages are immutable files outside the database; a
package's identity is the sha256 of its `manifest.json` (root ADR 0022). Times in packages are
integer ticks on named clocks (root ADRs 0005, 0012); the Ledger also needs its own time, for when
it learned of a package. Customers' catalogs must never meet. Getting any of this wrong means a
rebuild that differs from the original, a blank that turns into a link, or one tenant reading
another's evidence.

## Decision

1. **Store.** PostgreSQL 16. The schema is plain SQL migrations in
   `src/neptune_ledger/catalog/migrations/NNNN_name.sql`, numbered 1..n without gaps, never edited
   once merged; a change is a new migration. `neptune_ledger.catalog.migrate.apply_migrations`
   applies the missing ones to a tenant in one transaction under an advisory lock, records each in
   `schema_migration` with the sha256 of its bytes, and refuses a recorded migration whose bytes
   differ or one this Ledger does not ship. Driver: psycopg 3.
2. **Tenancy.** A tenant is one schema, `tenant_<tenant_id>` (`tenant_id` matches
   `[a-z][a-z0-9_]{0,47}`); the schema is the isolation unit and `PUBLIC` has no privilege on it.
   Each schema has a one-row `tenant` table, and every other table has `tenant_id NOT NULL`
   referencing it, so a row of another tenant cannot exist in the schema and no foreign key leaves
   it. Cross-tenant joins have nothing to join: the migrations never name a schema, and the access
   layer (a later issue) connects as a role granted one schema. Secondary indexes omit `tenant_id`, which is
   constant within a schema; primary keys keep it so foreign keys carry it.
3. **No foreign keys into package files.** The catalog references packages by id only and never
   copies record bodies. A `record` row points into its package by `(package_id, kind, line)`: the
   record's 1-based line in `records/<kind>.jsonl`. Foreign keys exist only between catalog tables.
4. **Two times, never mixed.** *Transaction time* is the Ledger's own clock: `next_tx()` locks the
   one-row `tx_clock`, increments `tx_seq` (strictly increasing per tenant) and returns `tx_time`,
   the host's UTC time as RFC 3339 text with exactly six fractional digits, raised to the previous
   tick's time if the host clock went back. Only `package` carries transaction time (`tx_seq`,
   `tx_time`). *World time* is what the evidence states: `record.world_clock` (a
   `timestamp_domain` record id) with `world_first` / `world_last` ticks as `bigint`, unconverted.
   No column has a timestamp, date or interval type. The registration log (`package` in `tx_seq`
   order) is the only input besides the packages; a rebuild replays it, advancing the clock with
   `replay_tx(tx_seq, tx_time)`, which accepts only a tick after the clock's last, so a rebuilt
   catalog equals the original including transaction times and live ticks continue after it.
5. **Tables.**
   - `package`: `package_id` (manifest sha256), `schema_version`, `receipt_id`, `root_locator` (where
     it was registered from), `ledger_version`, `tx_seq`, `tx_time`.
   - `source` (content id, size; once per content id), `package_source` (which packages cite it and
     each one's `storage`: `referenced` or `materialised`, from `manifest.sources`) and
     `source_location` (every location seen: one row per `source_revision` per package, with the
     location object as stated and the revisions it supersedes).
   - `transform` (adapter id, adapter version, config hash, libraries; once per transform id) and
     `transform_upstream` (the lineage DAG's edges as declared). `upstream_id` has no foreign key: a
     declared edge to a transform not yet registered is kept, not dropped.
   - `clock`: every `timestamp_domain` seen, by package, with its `field` and `scope`.
   - `record`: one row per record per package that holds it, `PARTITION BY LIST (kind)` with one
     partition per kind of package schema 1 and no default partition, so an unknown kind is refused.
     The key is `(tenant_id, kind, record_id, package_id)`; `record_id` is
     `neptune.model.kinds.record_key` (a content id for `source_artifact`). Columns: `line`,
     `schema_version`; the provenance summary `source_content_id`, `transform_id`,
     `assertion_kind` from the record-level provenance (for `ingest_finding`, the finding's
     transform and its `evidence` subject's source; NULL for ledger records); the world-time
     interval; `ambiguous_pointers`, the JSON pointers of every `Ambiguous` field.
   - World time by kind: `run`, `stream`: `first`/`last`; `calibration`: `valid_from`/`valid_until`;
     `image`, `video`: `capture.time` as an instant. Only Known values are indexed; if the two ends
     name different clocks neither is indexed. A NULL index column means "not Known in the
     record", never absent; the package keeps the missingness state.
   - `record_logical_id`: every Known logical id (`{namespace, value}`) a record states, with the
     JSON pointer it is stated at.
6. **Re-registration.** Registering a package whose `package_id` is already in the tenant is a
   no-op: it returns the stored `package_id` and `tx_seq`, allocates no tick and writes nothing,
   so record ids are unchanged. A registration locks `tx_clock` before looking the package up, so
   two concurrent registrations of one package cannot both insert it. A different `root_locator`
   for the same id is not recorded; the stored one stands.
7. **Indexes for threads and time windows.** By logical id (`record_logical_id (namespace, value)`),
   kind and record id (`record (record_id)`, partition pruning on kind), package
   (`record (package_id, kind)`), transform (`record (transform_id)`, `transform_upstream
   (upstream_id)`), source (`record (source_content_id)`, `package_source (content_id)`), world-time
   interval on a named clock (`record (world_clock, world_first, world_last)`), Ambiguous fields
   (GIN on `ambiguous_pointers`) and transaction time (`package (tx_seq)` unique, `package (tx_time,
   tx_seq)`). Thread semantics are ADR 0003's (MVL-87); the catalog stores only what records state.
8. **Tests run against a real PostgreSQL 16 with no service.** The `pgserver` wheel (pinned,
   test-only dependency group) ships PostgreSQL 16 binaries; the tests start one server per session
   and a fresh database per test.
9. **No interpretation.** No table stores inferred or model-made meaning: `assertion_kind` admits
   only `observed` and `stated`, and a schema test rejects interpretation-shaped column names.

## Alternatives considered

- **Row-level security on shared tables, `tenant_id` as a filter.** Rejected: one missing policy or
  a `BYPASSRLS` role leaks every tenant, and isolation depends on every query; a schema per tenant
  makes isolation structural and lets one tenant be dropped or moved whole.
- **Store record bodies (`jsonb`) in `record`.** Rejected: duplicates the package, invites edits
  that drift from it, and `jsonb` does not keep the canonical bytes. The catalog is an index; the
  package is the record.
- **One row per record id, with an array of packages.** Rejected: registration would rewrite
  existing rows, the lineage current-view needs per-package membership, and a package's rows
  could not be found or removed without touching others.
- **`timestamptz` for transaction time.** Rejected: its text form depends on the session time zone
  and it invites comparison with world time; fixed-width UTC text sorts correctly and cannot be
  confused with ticks.
- **A range type and GiST index for world time.** Rejected: a range rejects a source that states
  `last < first` and turns an unknown end into an unbounded one, a blank becoming a fact; a B-tree
  over `(world_clock, world_first, world_last)` keeps ends as stated.
- **A default partition for future kinds.** Rejected: a newer kind would be filed silently; the
  Ledger reads only the schema version it declares (ADR 0001 §4), and a new kind is a migration.
- **Skip the database tests when PostgreSQL is absent, or add a CI service.** Rejected: skipped tests
  prove nothing in CI, and a service couples this package's job to repository CI plumbing.

## Consequences

The catalog can be rebuilt from packages and the registration log, and every row says where in a
package its evidence is. Registrations in one tenant are serialised by the clock row; that is
acceptable at registration rates and would be revisited with a sharded clock. Readers needing a
record's body read the package. A new record kind, a new schema version or a new indexed field is
a new migration and a superseding ADR where the mapping changes. Revisit if a tenant must span
schemas, if transaction time must come from an external clock, or if per-record bodies become a
proven read bottleneck.

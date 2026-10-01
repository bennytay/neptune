# 0005 — L1 gate: catalog data model amendments for record bodies, constant-time clock checks, byte-order collation and the scale budget

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-89
- Amends: ADR 0002 §4 (how monotonic transaction time is enforced), §5 (new column and table),
  §6 (the conflict rule covers records), §7 (how lookups use the keys); ADR 0003 §4.3 (one body
  per record id). Review: [l1-stress-test.md](../reviews/l1-stress-test.md).

## Context

The L1 gate walked ADR 0002's catalog through hostile cases and measured it at 10⁵ packages
([review](../reviews/l1-stress-test.md)). These findings concern the data model:

- **A record id does not identify a body.** A tier-2 id is derived from kind, source content id,
  locator and transform (root ADR 0003; `neptune.identity.provenance.evidence_record_id`), never
  from the record's other fields. A tampered but self-consistent package can carry an existing
  record id with another run start, another `machine`, or another clock field. ADR 0002 §6
  refuses conflicting source and transform ids, but says nothing about records. ADR 0003 §4.3
  shows "the record once, with every registering package". That would merge two different
  statements into one entry.
- **0001's "never goes backwards" triggers scan whole tables.** `registration_log_follows_clock`
  and `package_from_log` each run `EXISTS (… WHERE tx_seq >= NEW.tx_seq OR tx_time > …)`. The `OR`
  defeats every index, so each registration costs O(n) in the catalog's size. The
  `package_from_log` lookup also omitted `tenant_id`, which leads the unique key it meant to use.
- **Absences are not indexed.** `SourceAbsence` records, the compiler's statement that a
  location no longer holds bytes (root ADR 0010), went only to their record partition. Nothing
  held their location or the revision they supersede.
- **Order depends on the database's locale.** The API sorts ids and kinds as UTF-8 bytes (ADR
  0004). The test server's databases use `en_US.UTF-8`. Today's hex ids sort the same under it
  only by accident, and a locale-aware order can change with a C-library upgrade, silently
  invalidating B-tree order.
- **Keys lead with `tenant_id`.** ADR 0002 §2 keeps `tenant_id` first in every primary and unique
  key. A lookup that omits it cannot use the key, and at 10⁵ packages it becomes a sequential
  scan (measured, §5).

## Decision

1. **Clock checks in O(1)** (migration 0002 replaces both functions; 0001 is not edited). A log
   entry must equal the clock's current tick, as 0001 already required. It is refused as "going
   backwards" when it is behind the one `tx_clock` row. A package row must be written while its
   log entry's tick is still the clock's last one. *Equivalence:* `tx_clock` only moves forward
   (its trigger), and each earlier log entry equalled the clock when it was written. Every
   earlier entry therefore has `tx_seq` no greater than the current tick, and an equal one is
   excluded by the primary key. Its `tx_time` is no later. So the new entry follows every earlier
   one, which is exactly what 0001's scan checked. A later package needs a later log entry, which
   needs a later tick, so a package written at its own tick never follows a later package. The
   log lookup names `tenant_id`. Live registrations (ADR 0004 §4: log, package and indexes in one
   transaction holding the clock) and rebuilds (`replay_tx`, log, package in turn) already write
   in this order.
2. **One body per record id (except `source_artifact`).** `record.body_digest` (`content_id`, `NOT NULL`) is the sha256 of the
   record's canonical JSON line in its table, without the newline. Within a tenant, one
   `(kind, record_id)` has one digest. Registration refuses a package that brings another digest
   for an existing record id, with `conflicting_id` and the record id as subject. That extends
   ADR 0002 §6 to records, clocks included, because a clock is a `timestamp_domain` record. The
   trigger `record_body_agrees` refuses such a row if anything gets that far. The same id with
   the same digest from another package is normal: a re-ingest after a move, or a backfill. So in
   ADR 0003 §4.3, an entry listed once with several packages is guaranteed to be one statement.
   - **Exception: `source_artifact`.** Its key is the content id. Its `chunk_size` and `chunks`
     are verification metadata, not identity (`neptune.model.source.SourceArtifact`): the same
     bytes hashed at two chunk sizes, or re-ingested after the compiler changes its default
     chunk size, are two honest bodies. For this kind a conflict is the same content id with
     another `size`, the rule the `source` table already enforces (ADR 0002 §6). Chunking
     differences are accepted. Each package's `body_digest` and chunking stay as that package
     states them, and the trigger skips the kind. Every other kind keeps the body digest rule.
     Tests: contract `test_the_same_bytes_at_two_chunk_sizes_register_as_two_packages` (two
     chunk sizes register; another size is `conflicting_id`), migration
     `test_a_source_artifact_may_differ_in_chunking_only`.
   The column is `NOT NULL` without a default. That is safe because `apply_migrations` runs all
   pending migrations in one transaction, and no registration implementation predates 0002.
3. **`location_absence`** holds `(absence_id, package_id, location, supersedes)` for each
   `source_absence` record per package, with location as canonical JSON text like
   `source_location`. It is append-only by trigger and indexed by package. ADR 0006 §5 uses it so
   that `resolve` can leave out a location its own package says is gone.
4. **Byte-order collation.** The catalog database uses the libc `C` (or `POSIX`) collation.
   `apply_migrations` refuses any other, including ICU, with `MigrationError`. Under `C`, default
   text order is UTF-8 byte order, so every B-tree on ids and kinds serves the API's order
   directly. The order cannot drift with a library version. The test fixtures and the scale
   harness create their databases from `template0` with `LC_COLLATE 'C'`.
5. **Lookups, and the scale budget.**
   - Every lookup by a primary or unique key names `tenant_id`, the constant of the schema. A
     thread query repeats its kind filter on `record` (`r.kind = ANY(…)`), so the planner prunes
     partitions at plan time.
   - **Budget**, time for one call at 10⁵ packages and about 10⁷ records: a thread of up
     to 10³ entries (declared or anchored key) **p95 < 50 ms**; a time window on one clock
     **p95 < 200 ms**; the largest thread, 20 000 entries, **< 500 ms**; and registration writes
     independent of catalog size.
   - **Measured** with `tests/ledger_catalog_scale.py` at the full scale, on 10⁵ packages and
     11 316 720 records (review, case 12), as client wall-clock times: typical thread p95
     15.3 ms, sensor 11.2 ms, anchored 0.58 ms; window p95 0.56 ms, and 9.1 ms on a 5 000-stream
     clock; the 20 000-entry thread 195 ms; log and package insert 0.58 ms against 17.0 ms under
     0001's scans. Without `tenant_id`, a package lookup takes 4.7 ms instead of 0.02 ms and a
     query page 588 ms instead of 0.58 ms. Both are sequential scans.
   - The harness is the regression check. Its `slow` test runs it at 10³ packages and asserts that
     every measured query uses its index. L2's derived thread index must meet the same budget and
     be measured with the same harness.
6. **What the catalog does not answer alone.** `part_of` membership (tier-2 references inside
   bodies), the sensor `category`, `software_version` keys and `Ambiguous` candidates are not
   catalog columns. ADR 0002 §7 already assigns them to L2's derived thread index. The gate
   confirms that assignment rather than adding columns: each is body content, and the index is
   rebuildable from packages plus the log.

## Alternatives considered

- **Keep 0001's scans and add an index.** Rejected. An `OR` across two columns still cannot use
  one B-tree, and the checks are redundant given the clock (§1).
- **A deferred constraint trigger that requires a package row for every log row at commit.**
  Rejected for now. It would forbid the log-only inserts that the clock tests use. ADR 0004 §4's
  single transaction already gives the guarantee, and the contract tests check that a refusal
  writes nothing.
- **Apply the body rule to `source_artifact` too.** Rejected. Its body holds chunking, which
  is not identity, so honest re-ingests at another chunk size would be refused.
- **Recompute record ids instead of digesting bodies.** Rejected. An id that recomputes proves
  nothing about the fields outside the id, which is exactly the gap.
- **Keep both bodies as separate entries.** Rejected. An entry's identity is (package, record id),
  but the current view collapses equal ids, and two bodies under one id contradict the compiler's
  determinism. Refusal surfaces the tampering, or the compiler bug, as a finding.
- **`COLLATE "C"` on each query instead of the database.** Rejected. It disables index order for
  every ordered scan unless every index is rebuilt with `COLLATE "C"`, and `record.kind` is a
  partition key, so its type cannot be altered.
- **Secondary indexes without `tenant_id`.** Rejected. That doubles the key indexes when the
  access layer always knows the tenant.

## Consequences

- MVL-90 computes `body_digest` from the verified line bytes and reports conflicts before writing.
  The trigger is a backstop, not the reporting path.
- Deployments must create the catalog database with `C` collation. Migrating an existing
  non-`C` database means dump and restore into one, and none exists yet.
- Registration cost no longer grows with the catalog (measured, review case 12).
- Revisit the budget when a tenant passes 10⁶ packages, or when the derived thread index lands.

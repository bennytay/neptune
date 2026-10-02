# 0008 — A default record partition for declared kinds; registration decides which kinds a package holds

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-91
- Amends: ADR 0002 §5 (`record` partitions) and its rejected alternative "a default partition
  for future kinds"; ADR 0006 §1 (which tables a manifest must count); ADR 0007 §1 (the rows
  registration writes come from the package's own tables).

## Context

ADR 0002 §5 gave `record` one partition per kind of package schema 1 and no default partition, so
the database refused a kind it did not list. It rejected a default partition because "a newer kind
would be filed silently". Package schema 2 (root ADR 0037, compiler PR #37) adds two kinds,
`configuration_snapshot` and `configuration_value`, and changes no version 1 record. Under ADR 0002
every new kind needs a Ledger migration before the compiler can ship it. Without one, the Ledger's
registration, and its tests that list the compiler's kinds, break on the day the compiler adds a
kind. Registration also compared the manifest with the compiler's whole kind list and the newest
schema version. So it refused version 1 packages as soon as version 2 existed, though a version 1
package holds only version 1's tables (root ADR 0037 §1).

The risk ADR 0002 guarded against was an *undeclared* kind entering the catalog. Since ADR 0006 §1,
registration checks the whole package before it writes anything, so the check can sit there.

## Decision

1. **Migration 0003 adds `record_default`, the `DEFAULT` partition of `record`.** It holds the rows
   of every kind without a partition of its own, with the same columns, keys, indexes and
   triggers. The schema-1 partitions of 0001 stay. The database keeps no list of kinds. A `CHECK`
   holds `kind` to a table name (`^[a-z][a-z0-9_]*$`). The partition is static schema, so a
   rebuild from the registration log files every row where the original did.
2. **Registration decides which kinds a package may hold, before writing anything.** The manifest's
   `schema_version` must lie between the compiler's `OLDEST_READABLE_VERSION` and `SCHEMA_VERSION`.
   Any other version is `unsupported_schema_version`, so a package from a future version is still
   refused. Its `tables` must be exactly that version's kinds, or it is `manifest_invalid`.
   `check.kinds_of` returns the compiler's `kinds_at(version)`. A compiler with one schema
   version has no `kinds_at`, so every kind belongs to that version; the fallback goes once
   package schema 2 is on main. Every line must also pass the compiler's readers
   (`record_invalid`). A kind reaches `record` only if a schema version this Ledger reads declares
   it. The Ledger's declared version stays in `docs/contracts.md`. MVL-93 owns any registry of
   versions beyond it.
3. **Registration and indexing iterate the package's own tables, never the compiler's kind list.**
   `check_package` reads the tables its manifest counts. `package_rows` indexes the tables it is
   given. The walkthrough tests take each package's kinds from its manifest.
4. **A kind gets its own partition by a later migration, when a caller needs one.** For example,
   a kind-specific projection (MVL-91) or a measured scan. That migration must move the kind's
   rows out of `record_default` in the same transaction, because PostgreSQL refuses to attach a
   partition while the default holds rows that belong in it. The move leaves every column as it
   was.

## Alternatives considered

- **A migration per new kind (ADR 0002 as it stood).** Rejected. It couples every compiler release
  that adds a kind to a Ledger migration landing first. The refusal it bought now happens at
  registration (§2).
- **Create a partition on demand when a registered package declares a new kind.** Rejected. It
  runs DDL inside registration and takes an exclusive lock on `record` per new kind. The schema
  would then depend on registration history and not on migrations, so `schema_migration` would no
  longer describe it.
- **Add partitions for `configuration_snapshot` and `configuration_value` now.** Rejected. It names
  kinds the compiler on `main` does not have yet. The next kind would bring the same break.

## Consequences

- A compiler version that only adds kinds needs no Ledger migration to be registered. It needs
  only the Ledger's declared version to cover it (`docs/contracts.md`).
- Kinds without their own partition share `record_default`. Partition pruning on `kind` narrows to
  that partition, and the shared indexes serve it. Revisit when a kind there dominates a measured
  query.
- Splitting a kind out of the default later costs a row move in that migration (§4).

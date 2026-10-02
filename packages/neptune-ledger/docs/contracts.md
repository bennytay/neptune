# neptune-ledger contracts

What this package publishes to, and consumes from, other workspace members. Each entry names the schema or
interface under `contracts/`, its version, and the ADR that fixed it. Nothing else is a public surface.

## Publishes

| Contract | Version | Version constant | Registry | Reference | Fixed by |
|---|---|---|---|---|---|
| `catalog-api` (register, verify, resolve, thread, threads_of, lineage, query) | **1.5.0** (stable since the L1 gate, MVL-89; 1.2.0 adds package-schema 2's record kinds, MVL-23; 1.3.0 package-schema 3's, MVL-82; 1.4.0 package-schema 4's, MVL-83; 1.5.0 package-schema 5's, MVL-202) | `neptune_ledger.api.CATALOG_API_VERSION` | `contracts/catalog-api/v1.5.0/` | [catalog-api.md](catalog-api.md) | [ADR 0004](adr/0004-catalog-api-error-model-and-versioning.md), [0006](adr/0006-l1-gate-catalog-api-amendments.md), [0002](adr/0002-catalog-data-model.md), [0005](adr/0005-l1-gate-catalog-data-model-amendments.md), [0003](adr/0003-entity-threads-and-the-lineage-current-view.md) |

Lakehouse table contracts are listed here when they land.

## Consumes

| Contract | Owner | Version built against | Source of truth | Fixed by |
|---|---|---|---|---|
| Package schema (canonical records) | `neptune` (compiler) | **5** | `neptune.model.record.SCHEMA_VERSION`; schema id `urn:neptune:schema:canonical:5` (versions 2 to 4 only add kinds: root ADR 0037 §1, MVL-23 / PR #37; root ADR 0050, MVL-82; root ADR 0051, MVL-83; 5 adds a kind and the states a lifecycle list may hold, root ADR 0061, MVL-202); its JSON Schema exports generate the record projections (`catalog/projections.json` from `contracts/package-schema/v5.0.0/schema.json`; migration 0005 from v1.0.0; v2.0.0 needed none; migration 0006 from v2.0.0 to v4.0.0, a guard only: v3's `run_assembly` and `snapshot_binding` fill the existing `run_ids`, v4's lifecycle kinds fill the existing `site_namespace`/`site_value`; v5.0.0 needed none: `civil_time_zone` states no hot filter) | root ADR 0017; Ledger [ADR 0001](adr/0001-ledger-place-in-the-programme.md), [ADR 0009](adr/0009-record-index-bodies-pointers-and-generated-projections.md) |

Rules:

- The Ledger reads package records whose `schema_version` is 1 to 5 (2 adds the configuration kinds,
  3 the alignment kinds, 4 the deployment lifecycle kinds, 5 the civil time zone kind and lifecycle list
  states; none changes an earlier record). It records any other version as a
  structured finding and does not guess at its shape.
- Moving the declared version is a PR in this package that updates this table, cites the owning package's
  bump PR and its `contracts/` goldens, and adds or supersedes an ADR.

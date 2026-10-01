# neptune-ledger contracts

What this package publishes to, and consumes from, other workspace members. Each entry names the schema or
interface under `contracts/`, its version, and the ADR that fixed it. Nothing else is a public surface.

## Publishes

| Contract | Version | Version constant | Registry | Reference | Fixed by |
|---|---|---|---|---|---|
| `catalog-api` (register, verify, resolve, thread, threads_of, lineage, query) | **1.0.0** (draft until MVL-89) | `neptune_ledger.api.CATALOG_API_VERSION` | `contracts/catalog-api/v1.0.0/` | [catalog-api.md](catalog-api.md) | [ADR 0004](adr/0004-catalog-api-error-model-and-versioning.md), [0002](adr/0002-catalog-data-model.md), [0003](adr/0003-entity-threads-and-the-lineage-current-view.md) |

Lakehouse table contracts are listed here when they land.

## Consumes

| Contract | Owner | Version built against | Source of truth | Fixed by |
|---|---|---|---|---|
| Package schema (canonical records) | `neptune` (compiler) | **1** | `neptune.model.record.SCHEMA_VERSION`; schema id `urn:neptune:schema:canonical:1` | root ADR 0017; Ledger [ADR 0001](adr/0001-ledger-place-in-the-programme.md) |

Rules:

- The Ledger reads package records whose `schema_version` is 1. It records any other version as a
  structured finding and does not guess at its shape.
- Moving the declared version is a PR in this package that updates this table, cites the owning package's
  bump PR and its `contracts/` goldens, and adds or supersedes an ADR.

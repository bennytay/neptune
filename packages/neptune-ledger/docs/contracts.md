# neptune-ledger contracts

What this package publishes to, and consumes from, other workspace members. Each entry names the schema or
interface under `contracts/`, its version, and the ADR that fixed it. Nothing else is a public surface.

## Publishes

| Contract | Version | Version constant | Registry | Reference | Fixed by |
|---|---|---|---|---|---|
| `catalog-api` (register, verify, resolve, thread, threads_of, lineage, query) | **1.3.0** (stable since the L1 gate, MVL-89; 1.2.0 adds package-schema 2's record kinds, MVL-23; 1.3.0 names record kinds by the package-schema contract instead of listing them, MVL-93) | `neptune_ledger.api.CATALOG_API_VERSION` | `contracts/catalog-api/v1.3.0/` | [catalog-api.md](catalog-api.md) | [ADR 0004](adr/0004-catalog-api-error-model-and-versioning.md), [0006](adr/0006-l1-gate-catalog-api-amendments.md), [0002](adr/0002-catalog-data-model.md), [0005](adr/0005-l1-gate-catalog-data-model-amendments.md), [0003](adr/0003-entity-threads-and-the-lineage-current-view.md), [0011](adr/0011-schema-version-registry-and-record-kinds-by-package-schema-version.md) |

Lakehouse table contracts are listed here when they land.

## Consumes

| Contract | Owner | Version built against | Source of truth | Fixed by |
|---|---|---|---|---|
| Package schema (canonical records) | `neptune` (compiler) | **2** | `neptune.model.record.SCHEMA_VERSION`; schema id `urn:neptune:schema:canonical:2` (version 2 only adds kinds: root ADR 0037 §1, MVL-23 / PR #37); its JSON Schema exports generate the schema-version registry, one projection spec per version (`catalog/projections.json` from `contracts/package-schema/v1.0.0/` and `v2.0.0/`; migration 0005 from v1.0.0) | root ADR 0017; Ledger [ADR 0001](adr/0001-ledger-place-in-the-programme.md), [ADR 0009](adr/0009-record-index-bodies-pointers-and-generated-projections.md), [ADR 0011](adr/0011-schema-version-registry-and-record-kinds-by-package-schema-version.md) |

Rules:

- The Ledger reads packages of every package-schema version its schema-version registry holds, within the
  compiler's readable range: today 1 and 2 (2 adds the configuration kinds and changes no version 1 record).
  Packages of both versions are indexed side by side, each record with its own version's projections. Any
  other version is refused with `unsupported_schema_version`; the Ledger does not guess at its shape.
- catalog-api does not list record kinds (1.3.0, ADR 0011 §4). A compiler PR that raises `SCHEMA_VERSION`
  changes no catalog-api file. Instead it runs
  `uv run python -m neptune_ledger.catalog.projection contracts/package-schema/v<N>.0.0/schema.json`,
  which appends version N to the registry and writes the next migration only when N adds a hot-filter
  projection, and moves the declared version in this table. Until then the Ledger refuses version N
  packages, the compiler's worked examples included.
- Moving the declared version is a PR in this package that updates this table, cites the owning package's
  bump PR and its `contracts/` goldens, and adds or supersedes an ADR.

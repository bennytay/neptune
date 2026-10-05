# neptune-ledger contracts

What this package publishes to, and consumes from, other workspace members. Each entry names the schema or
interface under `contracts/`, its version, and the ADR that fixed it. Nothing else is a public surface.

## Publishes

| Contract | Version | Version constant | Registry | Reference | Fixed by |
|---|---|---|---|---|---|
| `catalog-api` (register, verify, resolve, thread, threads_of, lineage, query) | **1.7.0** (stable since the L1 gate, MVL-89; 1.2.0 adds package-schema 2's record kinds, MVL-23; 1.3.0 package-schema 3's, MVL-82; 1.4.0 package-schema 4's, MVL-83; 1.5.0 `ThreadLink.entity_kind`, MVL-92; 1.6.0 names record kinds by the package-schema contract instead of listing them, MVL-93; 1.7.0 adds query frame windows, lineage preferences, projections, series joins, budgets and explain, MVL-98) | `neptune_ledger.api.CATALOG_API_VERSION` | `contracts/catalog-api/v1.7.0/` | [catalog-api.md](catalog-api.md) | [ADR 0004](adr/0004-catalog-api-error-model-and-versioning.md), [0006](adr/0006-l1-gate-catalog-api-amendments.md), [0002](adr/0002-catalog-data-model.md), [0005](adr/0005-l1-gate-catalog-data-model-amendments.md), [0003](adr/0003-entity-threads-and-the-lineage-current-view.md), [0011](adr/0011-schema-version-registry-and-record-kinds-by-package-schema-version.md), [0016](adr/0016-query-engine-planner-budgets-and-sql-passthrough.md) |

Lakehouse table contracts are listed here when they land.

## Consumes

| Contract | Owner | Version built against | Source of truth | Fixed by |
|---|---|---|---|---|
| Package schema (canonical records) | `neptune` (compiler) | **6** | `neptune.model.record.SCHEMA_VERSION`; schema id `urn:neptune:schema:canonical:6` (versions 2 to 5 only add kinds: root ADR 0037 §1, MVL-23 / PR #37; root ADR 0050, MVL-82; root ADR 0051, MVL-83; root ADR 0062, MVL-183; 6 adds the `civil_time_zone` kind and the states a lifecycle list may hold, root ADR 0061, MVL-202); its JSON Schema exports generate the schema-version registry, one projection spec per version (`catalog/projections.json` from `contracts/package-schema/v1.0.0/` to `v6.0.0/`; migration 0005 from v1.0.0; v2.0.0 needed none; migration 0006 from v2.0.0 to v4.0.0, a guard only: v3's `run_assembly` and `snapshot_binding` fill the existing `run_ids`, v4's lifecycle kinds fill the existing `site_namespace`/`site_value`; v5's `assertion` has no hot filter, so no migration; v6.0.0 needed none: `civil_time_zone` states no hot filter) | root ADR 0017; Ledger [ADR 0001](adr/0001-ledger-place-in-the-programme.md), [ADR 0009](adr/0009-record-index-bodies-pointers-and-generated-projections.md), [ADR 0011](adr/0011-schema-version-registry-and-record-kinds-by-package-schema-version.md) |
| Package series files | `neptune` (compiler) | series contract of root ADRs 0018 and 0025; the manifest's `store.series` (root ADR 0022) | `neptune.store.series` (`check_settings`, `SERIES_SETTINGS`), `neptune.store.package.series_path` | Ledger [ADR 0013](adr/0013-lakehouse-layout-and-in-place-series-reads.md) |
| Evidence locators and source chunk ids | `neptune` (compiler) | locator steps of root ADR 0016; `source_artifact` chunk ids of root ADR 0009; the CSV grammar and delimiter sniffing of root ADR 0042, mirrored for `row` citations | `neptune.model.provenance` (`locator_from_json`), `neptune.store.package.blob_path` | Ledger [ADR 0014](adr/0014-lance-media-store-and-evidence-resolution-to-bytes.md) |

Rules:

- The Ledger reads packages of every package-schema version its schema-version registry holds, within the
  compiler's readable range: today 1 to 6 (2 adds the configuration kinds, 3 the alignment kinds, 4 the
  deployment lifecycle kinds, 5 the `assertion` kind, 6 the civil time zone kind and lifecycle list
  states; none changes an earlier record). Packages of every version are indexed side
  by side, each record with its own version's projections. Any other version is refused with
  `unsupported_schema_version`; the Ledger does not guess at its shape.
- catalog-api does not list record kinds (1.6.0, ADR 0011 §4). A compiler PR that raises `SCHEMA_VERSION`
  changes no catalog-api file. Instead it runs
  `uv run python -m neptune_ledger.catalog.projection contracts/package-schema/v<N>.0.0/schema.json`,
  which appends version N to the registry and writes the next migration when a kind gains a projection
  (columns, or a guard over existing ones), and moves the declared version in this table. Until then the
  Ledger refuses version N packages, the compiler's worked examples included.
- Moving the declared version is a PR in this package that updates this table, cites the owning package's
  bump PR and its `contracts/` goldens, and adds or supersedes an ADR.

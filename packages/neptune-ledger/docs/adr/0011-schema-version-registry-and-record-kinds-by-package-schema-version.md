# 0011 — Schema-version registry: per-version projections, refusal of unknown versions, and record kinds by package-schema version

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-93
- Amends: ADR 0008 §2 (which versions registration reads; the `kinds_at` fallback is gone), ADR 0009
  §3 (`projections.json` holds one spec per version; each record is projected with its own
  version's spec), ADR 0002 §5 (two registry tables), ADR 0004 §5 and ADR 0006 §9 (catalog-api
  1.3.0 names record kinds by the package-schema contract).

## Context

Packages are never rewritten, so packages of package-schema 1, 2 and later versions stay
registered side by side forever. Three gaps followed from ADRs 0008 and 0009:

- **The Ledger read whatever the installed compiler read.** ADR 0008 §2 admitted any version
  between the compiler's `OLDEST_READABLE_VERSION` and `SCHEMA_VERSION`. ADR 0009 §3 indexed
  every package with one spec, the newest. A compiler that moved to version 3 made the Ledger
  index version-3 packages with version-2 projections: a field version 3 adds would read as NULL,
  "not Known in the record", a blank turned into a statement about the record.
- **A NULL projection column could not be read.** A record of an older version lacks a field a
  newer version adds. ADR 0009 §3 gave it NULL, the same NULL as a field the record states as
  `Unknown`. A reader could not tell "this schema has no such field" from "the package holds a
  state for it".
- **catalog-api embedded the compiler's kind list.** `RecordKind` was an enum of
  `neptune.model.kinds.RECORD_KINDS`, so every additive compiler kind changed the catalog-api
  export and forced a catalog-api minor version: 1.2.0 for package schema 2, and three more
  queued (MVL-82, MVL-83, PR #38). Its description still said "package schema 1".

## Decision

1. **The registry.** `catalog/projections.json` holds one entry per package-schema version this
   Ledger reads, versions 1..n without gaps (`projection.Registry`). An entry is the version's
   projection spec (ADR 0009 §3: kinds, hot-filter projections, free-form fields) plus the
   package-schema registry version it was generated from (`contract_version`, for example
   `2.0.0`) and the sha256 of that version's `schema.json`. The schema is referenced by digest,
   not copied: `contracts/package-schema/v<contract_version>/` is immutable and canonical. Tests
   pin each entry to its published version, and each entry's kinds to the compiler's
   `kinds_at(version)`. The *mapping* of a version is its spec as canonical JSON; its digest is
   `mapping_digest`.
2. **Which versions are read.** A package is read only if its manifest's version is in the
   registry and in the compiler's readable range (`check.readable_versions`). Any other version is
   `unsupported_schema_version`, before anything is written: a package from a future version is
   refused until a Ledger version adds its projection. `check.kinds_of` is the compiler's
   `kinds_at`; the fallback for a compiler without it is gone (package schema 2 is on main).
   - An indexed version's mapping never changes. `projection.add_version` appends only the next
     version and refuses a different spec for a version already present, with `ProjectionError`.
     A later registry version of the same schema version (`2.1.0`) may re-point an entry only
     when its mapping is identical. A new projection rule for an indexed version is an ADR and a
     rebuild.
   - The generator command takes the version's directory:
     `python -m neptune_ledger.catalog.projection contracts/package-schema/v<N>.0.0/schema.json`
     appends version N and writes the next migration only when N adds a hot-filter projection,
     rendered against version N−1's spec as before (ADR 0009 §3).
3. **Each record is projected with its own version's spec.** A package of version V holds records
   of versions up to V (a record is written at the version that added its kind, root ADR 0037 §1).
   `index.package_rows` looks up each record's spec by the version the record states. The record
   columns are the union of every version's projections. A record stating a version the registry
   lacks, or one newer than its package, is `record_invalid`. Packages of every version share one
   `record` table and one set of columns, so lineage sets, threads and windows span versions.
4. **catalog-api 1.3.0 references the package-schema contract instead of listing kinds.**
   `RecordKind` is a table name, `^[a-z][a-z0-9_]*$`, the shape ADR 0008 §1's `CHECK` already
   holds. Its description points at `contracts/package-schema` at the version a package declares
   (`Registration.schema_version`). The kinds are checked where they enter the catalog:
   registration (§2, ADR 0008 §2) and `query`, where a kind no version the Ledger reads declares
   is `invalid_request` (MVL-98 implements it; contract test
   `test_query_rejects_a_kind_no_schema_version_declares`). `KindCount` gains a docstring, so its
   description no longer prints the kind tuple. The change only widens one definition, so every
   1.2.0 golden validates against 1.3.0 (a minor version, ADR 0004 §5). From 1.3.0 on, a
   package-schema version that adds kinds changes no catalog-api file; a test proves that a
   compiler with one more kind exports byte-identical catalog-api schema.
5. **Migration 0007: `schema_version` and `schema_version_projection`.** Registration writes a
   `schema_version` row for each version a package states (its manifest's and its records') the
   first time the tenant sees it: the version, schema id, `contract_version`, `schema_sha256`,
   kinds, mapping and `mapping_digest`, and `first_registration_key`, the `tx_seq` of that
   registration. `schema_version_projection` lists one row per `(version, kind, field, column)`.
   Both are append-only by trigger. If a stored row's `mapping_digest` differs from this Ledger's,
   registration refuses with `unsupported_schema_version` and says to rebuild: the catalog never
   holds one version indexed two ways. The rows are a function of the packages in registration
   order, so a rebuild from the log reproduces them (ADR 0002 §4). 0007 refuses to apply to a
   catalog that already holds packages, which would lack their rows; such a catalog is rebuilt, as
   for 0004.
6. **Missingness across versions.** `projection_covered(kind, schema_version, column)` says
   whether a record of that kind and version states the field behind a projection column. For a
   NULL in that column, true means the record does not state the value as `Known` and the package
   holds its state; false means the field is `NotCovered` by the record's schema version, or the
   kind never has it. A kind is covered by a package when it is in its version's `kinds`: a
   version-1 package says nothing about configuration snapshots (`NotCovered`), while an empty
   version-2 table states that there are none. Neither NULL is ever read as a fact.

## Alternatives considered

- **Keep reading whatever the compiler reads, with the newest spec.** Rejected: a field a newer
  version adds would be indexed as NULL for records that state it (§2's first gap), and the
  issue requires refusing what the Ledger has no projection for.
- **Fill the registry by migration, one row per version the Ledger knows.** Rejected: every
  compiler version would then need a Ledger migration, numbered against parallel Ledger work. Rows
  written at first sight need a migration only when projections are added, and still record
  exactly the versions the tenant holds.
- **Store the full JSON Schema per version in the catalog or in the Ledger package.** Rejected:
  about 150 KB per version per tenant, duplicating the immutable registry. The `contract_version`
  plus `schema_sha256` identify it exactly.
- **A per-row list of NotCovered pointers on `record`.** Rejected: `record` is append-only, so a
  later Ledger that adds a projection could not add the pointer to rows already filed. The
  per-version registry answers for old rows and new ones alike.
- **Pin catalog-api to one package-schema version (`$ref` into its schema).** Rejected: the
  package-schema major moves with every additive kind (an integer constant is the registry major),
  so the reference would change as often as the enum did.
- **Drop the kind check from `query`.** Rejected: before 1.3.0 the schema rejected an unknown
  kind. Keeping that as `invalid_request` keeps a typo from reading as "no records".

## Consequences

- A compiler PR that raises `SCHEMA_VERSION` changes no catalog-api file. It runs the generator for
  the new version (one command, plus review of any generated migration) and moves the Ledger's
  declared version in `docs/contracts.md`. Until then the Ledger refuses that version's packages,
  the compiler's worked examples included, so the Ledger's CI fails on that PR. That failure is
  the intended signal.
- Consumers read a `RecordKind` they do not know as a kind of a newer package-schema version.
- MVL-92 and MVL-98 use `projection_covered` and the registry's kinds when they read projection
  columns or report per-package kind coverage. The acceptance test over the two-version fixture
  (`at_schema_2`) runs at the catalog level here; a `thread` over it is MVL-92's.
- Revisit if the compiler ever changes an existing field rather than adding one: §3 assumes a
  record's version fully determines its shape.

# neptune-ledger contracts

What this package publishes to, and consumes from, other workspace members. Each entry names the schema or
interface under `contracts/`, its version, and the ADR that fixed it. Nothing else is a public surface.

## Publishes

_None yet._ The catalog API, entity-thread, lakehouse and lineage current-view contracts will be listed
here, each with its version constant and goldens under `contracts/`, as they land.

## Consumes

| Contract | Owner | Version built against | Source of truth | Fixed by |
|---|---|---|---|---|
| Package schema (canonical records) | `neptune` (compiler) | **2** | `neptune.model.record.SCHEMA_VERSION`; schema id `urn:neptune:schema:canonical:2` (version 2 only adds kinds: root ADR 0037 §1, MVL-23 / PR #37) | root ADR 0017; Ledger [ADR 0001](adr/0001-ledger-place-in-the-programme.md) |

Rules:

- The Ledger reads package records whose `schema_version` is 1 or 2 (2 adds the configuration kinds and
  changes no version 1 record). It records any other version as a
  structured finding and does not guess at its shape.
- Moving the declared version is a PR in this package that updates this table, cites the owning package's
  bump PR and its `contracts/` goldens, and adds or supersedes an ADR.

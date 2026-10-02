# neptune-deploy contracts

What this package publishes to, and consumes from, other workspace members. Each entry names the schema or
interface under `contracts/`, its version, and the ADR that fixed it. Nothing else is a public surface.

CI runs `make contracts-check PKG=neptune-deploy`. That target applies the owner rule to any schema this package
exports, then runs `scripts/contracts.py check --package neptune-deploy` against `contracts/lock.toml`. The policy is
neptune-platform ADR 0002.

## Publishes

Deploy's records are the compiler's lifecycle kinds, published by the compiler as part of the package
schema. The evidence packs (`packs/`) will be published here when they land.

| Interface | Consumer | Version | Source of truth | Fixed by |
|---|---|---|---|---|
| Object-store connectors: `neptune.sources` entry points `deploy_s3`, `deploy_gcs`, `deploy_azure_blob`; factory `(url, *, network, ledger, options, credentials, environ)` returning a read-only `Source` | `neptune` (compiler), once it ingests plugin Sources | connector **0.1.0** | `neptune_deploy.sources.object_store`; `CONNECTOR_VERSION`; external identity `ExternalObjectRef(connector id, [<store>:]<bucket>/<key>, version:/generation:/etag:)` | Deploy [ADR 0006](adr/0006-object-stores-are-read-only-sources-over-a-standard-library-client.md) |
| Foxglove Data Platform connector: `neptune.sources` entry point `deploy_foxglove`; the same factory signature, `url` `foxglove://<project id>` or `foxglove://-`; returns a read-only `Source` whose `declared(location)` gives a recording's stated identifiers, facts and topics as `Knowledge` | `neptune` (compiler), once it ingests plugin Sources; Memory (declared identifiers, once the compiler carries them) | connector **0.1.0** | `neptune_deploy.sources.foxglove`; `CONNECTOR_VERSION`; external identity `ExternalObjectRef("deploy_foxglove", [<store>:]recording/<recording id>, import:<importedAt>;created:<createdAt>;size:<n>)` | Deploy [ADR 0007](adr/0007-foxglove-recordings-are-read-only-sources-over-the-documented-streaming-api.md) |
| Roboto and Rerun Hub connectors: `neptune.sources` entry points `deploy_roboto` (`roboto://<org>/<dataset>/<prefix>`) and `deploy_rerun` (path of a catalog export file); the same factory signature, returning a read-only `Source` whose `catalog()` gives `stated` structured records (`StructuredTable`, `StructuredRecord`) and `TimestampDomain`s over catalog documents | `neptune` (compiler), once it ingests plugin Sources | connector **0.1.0** | `neptune_deploy.sources.roboto`, `neptune_deploy.sources.rerun`, `neptune_deploy.sources.stated_records`; external identity `ExternalObjectRef("deploy_roboto", <org>/<dataset>/<path>, version:<file id>:<version>)`, Rerun objects as the object-store connector of their store; catalog documents `ExternalObjectRef(connector id, <scope>:<part>, records:<sha256>)` | Deploy [ADR 0009](adr/0009-roboto-and-rerun-hub-connectors-with-catalog-metadata-as-stated-records.md) |

## Consumes

| Contract | Owner | Version built against | Source of truth | Fixed by |
|---|---|---|---|---|
| Package schema (canonical records) | `neptune` (compiler) | **4** | `neptune.model.record.SCHEMA_VERSION`; schema id `urn:neptune:schema:canonical:4`; the lifecycle kinds of root ADR 0051 (MVL-83), `lifecycle-records` riding on it | Deploy [ADR 0001](adr/0001-a-compiler-plugin-of-adapters-and-read-only-sources.md) |
| `catalog-api` | `neptune-ledger` | **1.4.0** | `neptune_ledger.api.CATALOG_API_VERSION`; declared because the registry lists Deploy as a consumer (`packs/` will read packages through it); nothing reads it yet | Ledger ADR 0004 |
| `graph-schema` | `neptune-memory` | **1.0.0** | `neptune_memory.schema.GRAPH_SCHEMA_VERSION`; declared for the same reason; nothing reads it yet | Memory ADR 0002 |
| Adapter ABI and plugin entry points | `neptune` (compiler) | ABI **1** | `neptune.adapters.contract.ABI_VERSION`; entry-point groups `neptune.adapters`, `neptune.sources` | root ADRs 0008, 0024; Deploy [ADR 0001](adr/0001-a-compiler-plugin-of-adapters-and-read-only-sources.md) |

Rules:

- `neptune_deploy.PACKAGE_SCHEMA_VERSION`, this table and `contracts/lock.toml` name one package-schema version; a test fails if
  they or the compiler's `SCHEMA_VERSION` disagree.
- Moving the declared version is a PR in this package that updates all three, cites the compiler's bump PR and
  its `contracts/` goldens, and adds or supersedes an ADR.

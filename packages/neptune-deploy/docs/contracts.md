# neptune-deploy contracts

What this package publishes to, and consumes from, other workspace members. Each entry names the schema or
interface under `contracts/`, its version, and the ADR that fixed it. Nothing else is a public surface.

CI runs `make contracts-check PKG=neptune-deploy`. That target applies the owner rule to any schema this package
exports, then runs `scripts/contracts.py check --package neptune-deploy` against `contracts/lock.toml`. The policy is
neptune-platform ADR 0002.

## Publishes

_None yet._ Deploy's records are the compiler's lifecycle kinds, published by the compiler as part of the
package schema. The evidence packs (`packs/`) will be published here when they land.

## Consumes

| Contract | Owner | Version built against | Source of truth | Fixed by |
|---|---|---|---|---|
| Package schema (canonical records) | `neptune` (compiler) | **3** | `neptune.model.record.SCHEMA_VERSION`; schema id `urn:neptune:schema:canonical:3`; the lifecycle kinds of root ADR 0051 (MVL-83), `lifecycle-records` riding on it | Deploy [ADR 0001](adr/0001-a-compiler-plugin-of-adapters-and-read-only-sources.md) |
| `catalog-api` | `neptune-ledger` | **1.3.0** | `neptune_ledger.api.CATALOG_API_VERSION`; declared because the registry lists Deploy as a consumer (`packs/` will read packages through it); nothing reads it yet | Ledger ADR 0004 |
| `graph-schema` | `neptune-memory` | **1.0.0** | `neptune_memory.schema.GRAPH_SCHEMA_VERSION`; declared for the same reason; nothing reads it yet | Memory ADR 0002 |
| Adapter ABI and plugin entry points | `neptune` (compiler) | ABI **1** | `neptune.adapters.contract.ABI_VERSION`; entry-point groups `neptune.adapters`, `neptune.sources` | root ADRs 0008, 0024; Deploy [ADR 0001](adr/0001-a-compiler-plugin-of-adapters-and-read-only-sources.md) |

Rules:

- `neptune_deploy.PACKAGE_SCHEMA_VERSION`, this table and `contracts/lock.toml` name one package-schema version; a test fails if
  they or the compiler's `SCHEMA_VERSION` disagree.
- Moving the declared version is a PR in this package that updates all three, cites the compiler's bump PR and
  its `contracts/` goldens, and adds or supersedes an ADR.

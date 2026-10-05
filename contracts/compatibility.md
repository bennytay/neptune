# Contract compatibility matrix

Which contract versions exist and which version each consuming package is built against. Generated
by `scripts/contracts.py matrix` from `contracts/<id>/contract.toml`,
`contracts/<id>/v*/version.json`, `contracts/lock.toml` and `contracts/packages.toml`; do not edit
it by hand. `make contracts-check` fails while it is stale. The policy is ADR 0002 in
`packages/neptune-platform/docs/adr/`. The file at a release tag is that release's compatibility
statement.

## Contracts

| Contract | Owner package | Status | Latest stable | Latest draft | Consumers |
|---|---|---|---|---|---|
| `package-schema` | `neptune` | active | 6.0.0 | — | `neptune-deploy`, `neptune-learn`, `neptune-ledger` |
| `alignment-records` | `neptune` (part of `package-schema`) | planned | — | — | `neptune-memory` |
| `assertion-records` | `neptune` (part of `package-schema`) | planned | — | — | `neptune-deploy`, `neptune-memory` |
| `lifecycle-records` | `neptune` (part of `package-schema`) | planned | — | — | `neptune-deploy`, `neptune-memory` |
| `catalog-api` | `neptune-ledger` | active | 1.6.0 | — | `neptune-context`, `neptune-deploy`, `neptune-learn`, `neptune-memory` |
| `graph-schema` | `neptune-memory` | active | 1.1.0 | — | `neptune-context`, `neptune-deploy`, `neptune-learn` |
| `query-packet` | `neptune-context` | planned | — | — | `neptune-deploy`, `neptune-learn` |
| `dataset-manifest` | `neptune-learn` | planned | — | — | none in this repository |

## Consumer locks

Cells: the version `lock.toml` declares, then `current` (the latest stable), `behind` (an older
stable) or `draft`; `no stable` when the contract has no stable version yet (nothing to declare);
`not declared` when a stable version exists but the lock has no entry, `(no package yet)` when the
package has no lock section; blank when the package does not consume the contract. A contract that
is part of another rides on its version and has no column.

| Consumer | `package-schema` | `catalog-api` | `graph-schema` | `query-packet` |
|---|---|---|---|---|
| `neptune-ledger` | 6.0.0 current |  |  |  |
| `neptune-memory` |  | 1.1.0 behind |  |  |
| `neptune-context` |  | not declared (no package yet) | not declared (no package yet) |  |
| `neptune-deploy` | 6.0.0 current | 1.4.0 behind | 1.0.0 behind | no stable |
| `neptune-learn` | not declared (no package yet) | not declared (no package yet) | not declared (no package yet) | no stable |

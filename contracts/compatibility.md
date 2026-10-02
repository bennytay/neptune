# Contract compatibility matrix

Which contract versions exist and which version each consuming package is built against. Sources:
`contracts/<id>/contract.toml`, `contracts/<id>/v*/version.json` and `contracts/lock.toml`
(ADR 0002 in `packages/neptune-platform/docs/adr/`). Hand-maintained until `scripts/contracts.py matrix`
generates it and `make check` verifies it (follow-up, ADR 0003 §10): any PR that edits `lock.toml` or
publishes a contract version updates this file in the same PR. The file at a release tag is that release's
compatibility statement.

## Contracts

| Contract | Owner package | Status | Latest stable | Latest draft | Consumers |
|---|---|---|---|---|---|
| `package-schema` | `neptune` | active | 3.0.0 | — | `neptune-deploy`, `neptune-learn`, `neptune-ledger` |
| `alignment-records` | `neptune` (part of `package-schema`) | planned | — | — | `neptune-memory` |
| `lifecycle-records` | `neptune` (part of `package-schema`; kinds ship in 3.0.0, ADR 0051) | planned | — | — | `neptune-deploy`, `neptune-memory` |
| `catalog-api` | `neptune-ledger` | active | — | 0.0.0 | `neptune-context`, `neptune-deploy`, `neptune-learn`, `neptune-memory` |
| `graph-schema` | `neptune-memory` | planned | — | — | `neptune-context`, `neptune-deploy`, `neptune-learn` |
| `query-packet` | `neptune-context` | planned | — | — | `neptune-deploy`, `neptune-learn` |
| `dataset-manifest` | `neptune-learn` | planned | — | — | external training pipelines |

## Consumer locks

Cells: the version in `lock.toml` and whether it equals the latest stable (`current`) or not (`behind`);
`no stable` when the contract has no stable version yet (nothing to declare); blank when the package does not
consume the contract. Planned parts of `package-schema` ride on its version.

| Consumer | `package-schema` | `catalog-api` | `graph-schema` | `query-packet` |
|---|---|---|---|---|
| `neptune-ledger` | 3.0.0 current | | | |
| `neptune-memory` | | no stable | | |
| `neptune-context` | | no stable | no stable | |
| `neptune-deploy` | not declared (no package yet) | no stable | no stable | no stable |
| `neptune-learn` | not declared (no package yet) | no stable | no stable | no stable |

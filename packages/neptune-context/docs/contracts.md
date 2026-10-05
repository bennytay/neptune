# neptune-context contracts

What this package publishes to, and consumes from, other workspace members. Each entry names the schema or
interface under `contracts/`, its version, and the ADR that fixed it. Nothing else is a public surface.

CI runs `make contracts-check PKG=neptune-context`. That target applies the owner rule to any schema this package
exports, then runs `scripts/contracts.py check --package neptune-context` against `contracts/lock.toml`. The policy is
neptune-platform ADR 0002. A new package needs a `[neptune-context]` section in `contracts/lock.toml`, left empty if it
consumes nothing, and an entry in `contracts/packages.toml`.

## Publishes

- `query-packet` (`contracts/query-packet/`, status `planned`, owner module `neptune_context.contract`):
  the query language and the context packet, consumed by Deploy and Learn. No version is published and
  no schema is exported yet, so the owner rule has nothing to check; the first version lands with the
  `packets/` issues and registers `schema_export` and `version_constant` in `contract.toml`.
- The packet half is defined ([ADR 0003](adr/0003-the-context-packet.md)): `PACKET_VERSION = 1`
  (`neptune_context.packets.model`), the JSON Schema export `neptune_context.packets.schema:packet_schema`,
  the consumer checks `neptune_context.packets.conformance.check` and ten golden packets under
  `tests/golden/`. It is published with the query half as `query-packet`'s first version by the C1 gate
  (MVL-111), not before (ADR 0003 §9).

## Consumes

Pins live in `src/neptune_context/pins.py`; `tests/test_pins_context.py` keeps them, `contracts/lock.toml`
and this file in step.

| Contract | Owner | Version built against | Source of truth | Fixed by |
|---|---|---|---|---|
| `catalog-api` | `neptune-ledger` | `CATALOG_API_VERSION = "1.6.0"` | `neptune_ledger.api.CATALOG_API_VERSION`; read through `neptune_ledger.api.CatalogApi` | Ledger ADR 0004; Context [ADR 0001](adr/0001-place-in-the-programme-and-contract-pins.md) |
| `graph-schema` | `neptune-memory` | `GRAPH_SCHEMA_VERSION = "1.0.0"` | `neptune_memory.schema.GRAPH_SCHEMA_VERSION` (registry major 1); read through `neptune_memory.schema.reader.MemoryReader` | Memory ADR 0006; Context [ADR 0001](adr/0001-place-in-the-programme-and-contract-pins.md) |

Context declares no `package-schema` pin: packages reach it only as Ledger catalog rows and Memory claims.
Both upstream contract suites run from this package against stubs: the Ledger's `CatalogContract` against
`StubCatalog` (strict expected failures) and Memory's `CHECKS` against its reference and stub readers.

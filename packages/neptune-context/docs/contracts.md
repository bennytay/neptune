# neptune-context contracts

What this package publishes to, and consumes from, other workspace members. Each entry names the schema or
interface under `contracts/`, its version, and the ADR that fixed it. Nothing else is a public surface.

CI runs `make contracts-check PKG=neptune-context`. That target applies the owner rule to any schema this package
exports, then runs `scripts/contracts.py check --package neptune-context` against `contracts/lock.toml`. The policy is
neptune-platform ADR 0002. A new package needs a `[neptune-context]` section in `contracts/lock.toml`, left empty if it
consumes nothing, and an entry in `contracts/packages.toml`.

## Publishes

`query-packet` **1.1.0, stable** (`contracts/query-packet/v1.1.0/`), consumed by Deploy and Learn. Fixed by
[ADR 0002](adr/0002-query-language.md) (query), [ADR 0003](adr/0003-the-context-packet.md) (packet) and
[ADR 0006](adr/0006-c1-gate-query-packet-1-0-0-and-answer-checks.md) (publication, answer checks, C1 gate
amendments). 1.1.0 is the additive pin bump of [ADR 0007](adr/0007-graph-channel-retrieval-interface-and-local-engine.md):
the subject-kind and predicate enums and the embedded Memory definitions follow graph-schema 1.6.0, and the
packet reader refuses values beyond that pin.

- **Owner module** `neptune_context.contract`.
  - `contract_schema()` is the registry export. It embeds both halves' schemas verbatim, each as its own
    resource: `#/$defs/Query` (`urn:neptune:schema:query:1`) and `#/$defs/ContextPacket`
    (`urn:neptune:schema:context-packet:1`).
  - `QUERY_PACKET_VERSION = 1` is the registry major. It rises with `QUERY_VERSION` or `PACKET_VERSION`.
  - The module also exports the readers (`loads`, `decode_packet`), canonical bytes and ids (`canonical_bytes`,
    `query_id`, `packet_canonical_bytes`), the consumer checks `check_packet`, and `answer_problems(query,
    packet)` for a caller that holds both. The SDK is not part of the contract (ADR 0006 §6).
- **Query half.**
  - `QUERY_VERSION = 1`, carried as `query_version`.
  - Canonical bytes are the compiler's canonical JSON of `to_json`. The id `query_id` is
    `query:sha256:<hex>` of those bytes; a packet names its query by this id.
  - The JSON Schema snapshot is [`schema/query.schema.json`](schema/query.schema.json). Regenerate it with
    `uv run python -m neptune_context.query.schema packages/neptune-context/docs/schema/query.schema.json`.
- **Packet half.**
  - `PACKET_VERSION = 1`, carried as `packet_version`.
  - The JSON Schema export is `neptune_context.packets.schema:packet_schema`, and its snapshot is
    `tests/golden/context-packet.schema.json`.
- **Goldens** (`contracts/query-packet/goldens.py`, read from `tests/golden/`): ten persona query/packet
  pairs (`packet.qNN-*.json`, `query.qNN-*.json`) and ADR 0002's ten worked queries
  (`query.worked-qNN.json`).
- **Owner contract tests:** `tests/test_contract_context.py`, `tests/test_packet_goldens_context.py`,
  `tests/test_answer_context.py`. `make contracts-check PKG=neptune-context` applies the owner rule:
  the export must equal the latest registry version.
- **Changing it.**
  1. Edit the code.
  2. For a change an older reader would misread, raise the half's version and `QUERY_PACKET_VERSION`.
  3. Run `scripts/contracts.py bump query-packet <next>`.
  4. Add or supersede an ADR.

  A new optional member is a minor version (ADR 0002 §9).

## Consumes

Pins live in `src/neptune_context/pins.py`; `tests/test_pins_context.py` keeps them, `contracts/lock.toml`
and this file in step.

| Contract | Owner | Version built against | Source of truth | Fixed by |
|---|---|---|---|---|
| `catalog-api` | `neptune-ledger` | `CATALOG_API_VERSION = "1.7.0"` | `neptune_ledger.api.CATALOG_API_VERSION`; read through `neptune_ledger.api.CatalogApi` (`query(spec)` with frame windows and budgets, Ledger ADR 0016) | Ledger ADR 0004, 0016; Context [ADR 0001](adr/0001-place-in-the-programme-and-contract-pins.md), [ADR 0007](adr/0007-graph-channel-retrieval-interface-and-local-engine.md) |
| `graph-schema` | `neptune-memory` | `GRAPH_SCHEMA_VERSION = "1.6.0"` | `neptune_memory.schema.GRAPH_SCHEMA_VERSION` (registry major 1); read through `neptune_memory.schema.reader.MemoryReader` | Memory ADR 0006; Context [ADR 0001](adr/0001-place-in-the-programme-and-contract-pins.md), [ADR 0007](adr/0007-graph-channel-retrieval-interface-and-local-engine.md) |

The vocabularies and definitions Context publishes from these contracts (subject kinds, predicate names, the
Memory definitions the packet schema embeds) come from `src/neptune_context/pinned.json`, a snapshot of the
registry at the pins, never from the owners' live code (ADR 0006 §9). A pin bump regenerates it with
`uv run python -m neptune_context.pinned contracts packages/neptune-context/src/neptune_context/pinned.json`,
then the query and packet schemas, the planner recordings and a `query-packet` minor version.

Context declares no `package-schema` pin: packages reach it only as Ledger catalog rows and Memory claims.
Both upstream contract suites run from this package against stubs: the Ledger's `CatalogContract` against
`StubCatalog` (strict expected failures) and Memory's `CHECKS` against its reference and stub readers.

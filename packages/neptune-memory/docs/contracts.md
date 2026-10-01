# neptune-memory contracts

What this package publishes to, and consumes from, other workspace members. Each entry names the schema or
interface under `contracts/`, its version, and the ADR that fixed it. Nothing else is a public surface.
Pins live in `src/neptune_memory/pins.py`; `tests/test_pins_memory.py` keeps them and this file in step.

## Publishes

- Graph schema and claim model: `GRAPH_SCHEMA_VERSION = 0` (undefined until MVL-105 defines it); consumed by
  Context, Deploy and Learn. [ADR 0001](adr/0001-place-in-the-programme-and-contract-pins.md).

## Consumes

- Compiler package schema: `SCHEMA_VERSION = 1` (`neptune.model.record`, as on main). Alignment records
  (MVL-82) are consumed through the Ledger once they land.
- Ledger catalog API: `CATALOG_API_VERSION = "pending: pinned when MVL-85 (Ledger catalog API) lands"`.
  Until then Memory codes against the `LedgerReader` Protocol in `neptune_memory/ledger.py` and tests
  against `StubLedger`.

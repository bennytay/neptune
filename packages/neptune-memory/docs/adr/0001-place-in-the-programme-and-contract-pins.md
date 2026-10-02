# 0001 — Memory's place in the programme, declared contract versions, and the stub Ledger

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-101

## Context

`neptune-memory` is the second layer above the compiler. The Ledger package (`neptune-ledger`, MVL-46 scaffold)
is not on `main` and its catalog API (MVL-85) is not built, so Memory cannot depend on it yet. Memory must still
fix, from its first commit, what it reads, what it publishes, and which versions it is written against.

## Decision

1. **Place.** Layer 2. Memory consumes the Ledger's catalog API and the compiler's alignment records (MVL-82,
   delivered through the Ledger) and produces the graph schema and claim model that Context, Deploy and Learn
   consume. It never re-parses raw sources and never reads package files; packages arrive only through the Ledger.
2. **Workspace member.** `packages/neptune-memory`, depending on `neptune` only for now (the member `[project]
   dependencies` list drives CI selection, so adding `neptune-ledger` later also makes Ledger changes run this
   job). CI runs the `neptune-memory` matrix job when `packages/neptune-memory/**` or `contracts/**` changes.
3. **Declared contract versions** (mirrored in `docs/contracts.md` and `neptune_memory/pins.py`):
   - `SCHEMA_VERSION = 1`: the compiler package schema, `neptune.model.record.SCHEMA_VERSION` on main.
   - `CATALOG_API_VERSION = "pending: pinned when MVL-85 (Ledger catalog API) lands"`.
   - `GRAPH_SCHEMA_VERSION = 0`: Memory's own published schema, undefined until MVL-105.
4. **Stub Ledger.** `neptune_memory/ledger.py` defines a minimal typed `LedgerReader` Protocol (catalog API
   version, ordered package listing, records by kind with `None` for an unknown package versus `()` for none of
   that kind) and an in-memory `StubLedger`. Contract tests are parametrised over readers and run in CI against the
   stub; the real client adds one entry to the reader list when MVL-85 lands, and the pin above is set then.
5. **Boundaries, enforced by test.** `consolidate/` imports neither `derived/` nor any model or LLM client; no
   module imports `neptune.store`; model clients appear only under `derived/`.

## Alternatives considered

- **Wait for the Ledger before scaffolding.** Blocks every Memory issue on an unbuilt API; the Protocol stub costs
  one small module and is replaced, not migrated.
- **Read packages through `neptune.store` meanwhile.** Couples Memory to on-disk layout, the exact thing the
  Ledger exists to hide, and would have to be ripped out.
- **Pin the catalog version to a guess.** A fabricated pin reads as a fact; the `pending` marker is explicit.

## Consequences

- Memory work can start against a stable seam; swapping the stub for the Ledger client is a one-line fixture change.
- The pins must be updated in the same PR that adopts MVL-85 and MVL-105; `test_pins_memory.py` fails if the docs and
  code disagree.
- Revisit the Protocol's shape when MVL-85 defines the real catalog API; any difference is resolved in the Protocol,
  not by widening what Memory reads.

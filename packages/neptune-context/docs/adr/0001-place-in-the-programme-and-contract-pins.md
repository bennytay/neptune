# 0001 — Context's place in the programme, declared contract versions, and the read-only seam

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-107

## Context

`neptune-context` (P-MVL-14) is the third layer above the compiler: it answers questions over what the Ledger
catalogues and Memory has consolidated. Both upstream contracts are now published (`catalog-api` 1.6.0,
`graph-schema` 1.0.0), so Context can fix, from its first commit, what it reads, what it publishes and which
versions it is written against. It must do so before any query code exists, because the boundary is what keeps
a retrieval engine from turning into a second memory or a RAG index over raw files.

## Decision

1. **Place.** Layer 3. Context consumes the Ledger's catalog API (packages, records, threads, lineage) and
   Memory's graph schema (claims, nodes, episodes, spatial views) and publishes the `query-packet` contract
   (query language and context packet) that Deploy and Learn consume. It never re-parses raw sources, never
   reads package files and never writes to the Ledger or Memory.
2. **Workspace member.** `packages/neptune-context` depends on `neptune`, `neptune-ledger` and `neptune-memory`.
   The dependency list drives CI selection, so the `neptune-context` matrix job runs when
   `packages/neptune-context/**` or `contracts/**` changes, and when an upstream member it depends on runs.
3. **Declared contract versions** (mirrored in `docs/contracts.md`, `neptune_context/pins.py` and
   `contracts/lock.toml`):
   - `CATALOG_API_VERSION = "1.6.0"`: the registry's latest stable `catalog-api`, owned by `neptune-ledger`.
   - `GRAPH_SCHEMA_VERSION = "1.0.0"`: the registry's latest stable `graph-schema`, owned by `neptune-memory`.
   - `query-packet`: owned here, status `planned`, nothing published or declared yet.
   - No `package-schema` pin: Context sees packages only through the two contracts above.
4. **The upstream contract suites run from here, against stubs.** `CatalogContract` (Ledger) runs against
   `StubCatalog` as strict expected failures; Memory's `CHECKS` run against `ReferenceReader` (all green) and
   `StubReader` (red wherever data is visible). A real or in-memory client adds one subclass or reader to the
   same list when it exists. `make contracts-check PKG=neptune-context` also runs each owner's own suites.
5. **Boundaries, enforced by test.** Context imports from Memory only `neptune_memory.schema` and from the
   Ledger only `neptune_ledger.api`; it imports none of the compiler's `store`, `runtime`, `adapters` or
   `discovery`. Context never writes memory, so no Memory store, consolidator, derived annotator or CLI is
   reachable from its code.
6. **Skeleton layout.** `query/`, `retrieve/`, `packets/`, `render/`, `sdk/`, `mcp/`, `explain/`, `eval/` exist
   as documented empty packages; no query or retrieval behaviour lands in the scaffold.

## Alternatives considered

- **Read Memory's Postgres store directly.** Faster for one backend, but it makes Context depend on Memory's
  persistence and lets it write; the `MemoryReader` Protocol is the published seam. Lost.
- **Define Context's own stub Ledger and Memory.** Duplicates the owners' stubs and drifts from their suites;
  the owners already ship stubs and suites for downstream use. Lost.
- **Pin to a draft or to "latest".** A moving pin reads as a fact the code never checked; the registry's
  declared, tested versions are explicit. Lost.
- **Wait for `query-packet` to design the package.** Blocks every Context issue; the contract is owned here
  and arrives with `packets/`. Lost.

## Consequences

- Context work starts against stable seams; replacing a stub with a real client is one test-list entry.
- Pins change in the PR that adopts an upstream version; `test_pins_context.py` fails if code, lock, registry
  and docs disagree, and `scripts/contracts.py check` warns while the lock lags the registry.
- Revisit the import allow-list if Memory publishes a second read surface (episodes or spatial readers outside
  `schema`), or when `query-packet` needs a typed dependency on Ledger records beyond `neptune_ledger.api`.

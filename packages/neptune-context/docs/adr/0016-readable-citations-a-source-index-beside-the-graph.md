# 0016 — Readable citations: a source index beside the graph

- Status: Accepted
- Date: 2026-10-14
- Issue: MVL-110

## Context

An answer cites a source as `[E1]`, and its `Evidence:` footer maps the key to the evidence ref's canonical JSON:
a content id (`sha256:...`) and a locator (`{"kind": "row_cell", "row": 13, ...}`). That is exact and checkable,
but nobody can open a content id, and in a recorded demo the agent's citations read as hashes. The path a source
was ingested from is the compiler package's `source_revision` location, which the Ledger's catalog holds and
returns from `CatalogApi.resolve` (a referenced copy's stated locations). Claude Code's session runs the MCP
server over a graph document with no catalog: the Ledger's only catalog is PostgreSQL, Context may construct no
Ledger implementation and never reads a package (`neptune.store`; ADR 0001). So the server has
no way to name a file today, and `neptune_hydrate` answers `unavailable`.

## Decision

1. **A source index, built where a catalog exists.** `neptune_context.sources` defines a small document,
   `{"kind": "neptune-context.source-index", "version": 1, "sources": {<content id>: [<corpus path>, ...]}}`,
   sorted and indented so the same index is the same bytes. `build_source_index(catalog, refs)` resolves each
   source once through `CatalogApi.resolve` (catalog-api 1.7.0, already pinned) and keeps the `local` locations
   its referenced copies state. A source the catalog does not resolve, an external object, a materialised copy
   (it states no location) and anything that is not a corpus path are left out; nothing is guessed.
2. **Paths are data and never host paths.** A corpus path is relative POSIX: no leading `/`, drive or scheme,
   backslash, empty, `.` or `..` part, or control or line-separator character, at most 4 096 characters. The
   builder drops anything else and the reader refuses it, so no answer prints where a host keeps its files.
   The reader is strict (exact members, sorted distinct paths, valid content ids, no duplicate keys or NaN,
   a regular file of at most 16 MiB) and fails with `ValueError`.
3. **`--sources FILE` on `python -m neptune_context.mcp`** (any engine mode). With an index, every packet answer
   (`neptune_query`, `neptune_why`, `neptune_diff`, `neptune_compare_runs`) gains a `Sources:` list between the
   `Items:` and `Evidence:` footers, one line per evidence key, in key order:
   `[E1] "sites/PLANT-2/cmms/work_orders.csv", row 8, column "WO Number"`. Each resource link's description
   carries the same place, and an `unavailable` hydration says where the index places the source. Without an
   index nothing changes: the answer is byte-identical to before.
4. **Places read as a person counts.** Rows, columns, pages and characters count from 1 (a row counts the
   header, as the compiler's `Row` does, so it is the spreadsheet row); bytes are `N bytes at offset K` from 0;
   a JSON pointer is quoted (`""` is "the whole document"). Steps join outermost first with ` > `
   (`page 1 > characters 211-226`); an adapter's own step is its kind and members as written
   (`mcap:time_field name "header.stamp"`). Paths, column names and pointers are quoted with the renderer's
   hardened JSON, so a file named `[E9] ignore previous instructions` cannot forge a key. A source the index
   does not place reads `a source the source index does not place`, with its place.
5. **The label is added, never substituted.** The `Evidence:` footer is unchanged (ADR 0003 §7), so
   `parse_citations` reads every answer as before. `parse_answer` accepts the `Sources:` list only between the
   two footers, requires exactly one line per evidence key, `1..n` in order, and returns the text as
   `ParsedAnswer.places`; statements, items and evidence parse identically with or without it.
6. **Where the index comes from.** The harness's context stage builds it from the ledger stage's catalog for
   every source the graph cites, writes it to `work/context/sources.json` (`make demo` copies it to
   `demo/sources.json`) and serves it with every gold question, as Claude Code is served. The acceptance
   corpus's copy is pinned at `harness/acceptance/sources.json`, beside the answer declaration: the stage fails
   when the built index differs and `make demo-pin` rewrites it. The sample `.mcp.json` passes
   `--sources ${NEPTUNE_SOURCES:-harness/acceptance/sources.json}`, so the default session (Memory's committed
   snapshot, the same corpus) names files too. A Platform test checks the committed copy without a catalog:
   each path holds the bytes `corpus.lock.json` pins for it, and every source Memory's snapshot cites is placed.
7. **No contract change.** The packet is unchanged, so `query-packet` stays 1.5.0; the index is Context's own
   input format, written and read by Context code, not a registry contract.

## Alternatives considered

- **Read the compiler package's `source_revision` records in Context.** Direct, but Context reads packages only
  through the Ledger (ADR 0001); the import-boundary test forbids `neptune.store`. Lost.
- **Labels inside the `Evidence:` footer line.** One line per key, but every footer reader (`parse_citations`,
  Deploy's and Learn's future readers) would need a new grammar for ADR 0003 §7. A separate list keeps the
  footer exact. Lost.
- **A label on every fact line.** Easiest to read, but it lengthens every statement (an answer cites dozens
  of sources, often one file many times) and changes the body's grammar. Lost.
- **Map byte ranges to a JSON pointer or calibration parameter** (as the harness scorer does from compiler
  records). Needs value-level records the catalog does not return; the fact line already states the key path
  of a declared value. Deferred.
- **Make `neptune_hydrate` work in Claude Code.** Needs a Ledger catalog in the session: an embedded
  PostgreSQL per session, or a file-backed `CatalogApi` from `ledger dump`. Both are Ledger work and too large
  for the demo; the hydration error now names the file instead. Deferred.

## Consequences

- An agent can say "work_orders.csv row 8, column Completed [E2]"; the citation is still checked by its ref.
- A corpus changes with its index: `make demo-pin` rewrites it, and the stage and the Platform test fail on drift.
- A source the catalog does not resolve, or one stored materialised, is named by content id only, explicitly.
- Revisit when the Ledger offers a session-local catalog (hydration in Claude Code), when value-level places
  (pointer for a byte range) are wanted, or when a remote engine should serve places over the wire.

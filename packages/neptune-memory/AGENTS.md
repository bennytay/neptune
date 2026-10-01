# AGENTS.md — neptune-memory

How to work in `packages/neptune-memory/`. The repository-wide rules in the root `AGENTS.md` (workflow, git,
Linear, review, merge) apply unchanged; this file adds what is specific to this package.

## Shared non-negotiables (every workspace member)

1. **Own your directory.** Change files under `packages/neptune-memory/` only. Anything another package
   needs from this one is published through `contracts/` and `docs/contracts.md`, never by importing
   another member's internals.
2. **Raw evidence is immutable and everything has provenance.** Consume the compiler's canonical
   representation; never re-parse raw sources, never strip provenance, never turn a blank into a fact.
3. **Deterministic.** Same inputs + version + config ⇒ byte-identical output. No wall clock, randomness
   or network unless this package's purpose is the network, and then behind an explicit boundary.
4. **Every robot.** Manipulators, mobile, legged, humanoid, aerial, marine, vehicles, fleets: no
   morphology assumptions in code, fixtures or examples.
5. **Decisions are ADRs here.** `docs/adr/NNNN-title.md` from `docs/adr/0000-template.md`; numbers are
   local to this package. Run `make adr-index` after adding one; `make check` fails on a stale index.
6. **`ARCHITECTURE.md` is one Mermaid diagram.** Edit it only when a box, arrow, owner or build status
   changes, and fill the PR template's **Architecture change** section in the same PR.
7. **Green before review.** `make check PKG=neptune-memory` while iterating; CI runs this package's job
   when `packages/neptune-memory/**`, `contracts/**` or root plumbing changes.
8. **All input is hostile; errors are structured.** Malformed, boundary and determinism tests for
   every substantial component; real small fixtures over mocks; test basenames unique repo-wide.

<!-- BEGIN layer-specific: owned by this package's coordinator; replace the stub below. -->
## Layer-specific rules

Memory is Layer 2: it consolidates Ledger packages into a provenance-preserving claim graph that
Context, Deploy and Learn consume. It is not a vector store, a RAG index or a retriever. It must serve
every robot type; no example or fixture assumes one morphology. Place in the programme and declared
contract versions: [ADR 0001](docs/adr/0001-place-in-the-programme-and-contract-pins.md).

1. **A claim without provenance and `assertion_kind` is a bug.** Every claim names its source evidence,
   the transform (consolidator id + version + config hash) and `observed | stated | inferred`.
2. **`consolidate/` never imports model code.** No `derived/`, no model or LLM client; it is
   deterministic. `tests/test_import_boundaries_memory.py` enforces it.
3. **Inference lives under `derived/`.** Nothing inferred appears in `schema/`, `store/` or `consolidate/`.
4. **Nothing reads package files directly.** Packages are read only through
   `neptune_memory.ledger.LedgerReader` (never `neptune.store`, never the filesystem). Until the real
   Ledger catalog API lands, `StubLedger` stands in and the same contract tests run against both.
5. **Pins are declared** in `neptune_memory/pins.py` and `docs/contracts.md`; bump them only with the
   code that adapts.

Repo map: `schema/` (graph schema, claim model), `store/` (persistence), `consolidate/` (deterministic
claims from Ledger records), `derived/` (inference), `spatial/`, `episodes/`, `cli/`; `ledger.py`
(reader seam), `pins.py` (contract versions).

Model policy: Opus for `schema/`, `store/`, `consolidate/` and ADRs; Sonnet for `derived/` plumbing,
fixtures and docs.
<!-- END layer-specific -->

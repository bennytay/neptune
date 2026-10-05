# AGENTS.md — neptune-context

How to work in `packages/neptune-context/`. The repository-wide rules in the root `AGENTS.md` (workflow, git,
Linear, review, merge) apply unchanged; this file adds what is specific to this package.

## Shared non-negotiables (every workspace member)

1. **Own your directory.** Change files under `packages/neptune-context/` only. Anything another package
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
7. **Green before review.** `make check PKG=neptune-context` while iterating; CI runs this package's job
   when `packages/neptune-context/**`, `contracts/**` or root plumbing changes.
8. **All input is hostile; errors are structured.** Malformed, boundary and determinism tests for
   every substantial component; real small fixtures over mocks; test basenames unique repo-wide.

<!-- BEGIN layer-specific: owned by this package's coordinator; replace the stub below. -->
## Layer-specific rules

Context is Layer 3: it turns a question into a query over the Ledger's catalog API and Memory's graph
schema and returns a context packet that agents, Deploy and Learn consume. It is not a memory, a vector
store, a chat product or a dashboard, and it never re-parses raw evidence. It must serve every robot
type; no example or fixture assumes one morphology. Place in the programme and declared contract
versions: [ADR 0001](docs/adr/0001-place-in-the-programme-and-contract-pins.md).

1. **Context never writes memory.** It reads Memory through `neptune_memory.schema` and the Ledger
   through `neptune_ledger.api` only; no store, consolidator, derived annotator or CLI of either, and
   never `neptune.store`. `tests/test_import_boundaries_context.py` enforces it.
2. **Every packet item carries provenance.** Source evidence, transform and `assertion_kind`
   (`observed | stated | inferred`) travel from claim to packet to render. A blank is explicit
   missingness (`Unknown`, `NotCovered`, ...), never a dropped field or a fact.
3. **No channel is privileged.** Graph, catalog, lexical, vector and spatial retrieval are peers behind
   one interface; fusion and the planner may not hard-wire one ahead of the others, and a test per
   channel pair proves it.
4. **Latency budgets are tests.** A budget is an assertion under `eval/` that fails CI, never a comment.
5. **Deterministic.** Same query + pinned versions + snapshot (`as_of`) give a byte-identical packet.
   Anything model-generated lives under a `derived` boundary and is labelled `inferred`.
6. **Pins are declared** in `neptune_context/pins.py`, `docs/contracts.md` and `contracts/lock.toml`;
   bump them only with the code that adapts. Upstream vocabularies Context publishes come from the
   pinned snapshot `pinned.json`, never from Memory's or the Ledger's live code (ADR 0006 §9). The upstream contract suites run from here against stubs
   (`tests/test_catalog_contract_context.py`, `tests/test_graph_contract_context.py`).

Repo map (`src/neptune_context/`; subpackages are skeletons until their issues land):
`query/` (language, parse, plan), `retrieve/` (channels, fusion), `packets/` (the `query-packet`
contract's types), `render/` (packet to JSON, Markdown, prompt block), `sdk/` (Python API),
`mcp/` (agent tool server), `explain/` (why an item is in a packet), `eval/` (quality and latency
budgets); `contract.py` (the `query-packet` owner module), `answer.py` (does a packet answer its query,
ADR 0006); `pins.py` (contract versions), `pinned.py` (upstream vocabularies at those versions).

Model policy: Opus for `query/`, `retrieve/` fusion, ADRs and gates; Sonnet for renderers, SDK
plumbing, the MCP server and docs.
<!-- END layer-specific -->

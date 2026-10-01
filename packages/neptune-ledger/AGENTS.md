# AGENTS.md — neptune-ledger

How to work in `packages/neptune-ledger/`. The repository-wide rules in the root `AGENTS.md` (workflow, git,
Linear, review, merge) apply unchanged; this file adds what is specific to this package.

## Shared non-negotiables (every workspace member)

1. **Own your directory.** Change files under `packages/neptune-ledger/` only. Anything another package
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
7. **Green before review.** `make check PKG=neptune-ledger` while iterating; CI runs this package's job
   when `packages/neptune-ledger/**`, `contracts/**` or root plumbing changes.
8. **All input is hostile; errors are structured.** Malformed, boundary and determinism tests for
   every substantial component; real small fixtures over mocks; test basenames unique repo-wide.

<!-- BEGIN layer-specific: owned by this package's coordinator; replace the stub below. -->
## Layer-specific rules

The Ledger is layer 1: it consumes compiler packages and produces the catalog API, entity threads, the
lakehouse and the lineage current-view for Memory, Context, Deploy and Learn
([ADR 0001](docs/adr/0001-ledger-place-in-the-programme.md)). It organises evidence; it never reads
another layer's storage and never becomes a memory learner, retriever or reasoner.

### Repo map (`src/neptune_ledger/`; subpackages appear as their issues land)

```
api/             the catalog API contract: protocol, request/response records, JSON Schema, Arrow
                 query schema, StubCatalog (published as contracts/catalog-api)
contract_tests/  the catalog contract as pytest tests any implementation subclasses
catalog/         package registry and immutable catalog records: migrations, the schema
threads/         entity threads: ordered, provenance-linked views of one entity across packages
lineage/         parser/adapter lineage and the current-view over superseded packages
lake/            lakehouse tables derived from package records, rebuildable from packages alone
query/           read-only query planning over catalog, threads, lineage and lake
access/          the surface other layers call: catalog API, auth boundary, exports
cli/             thin command-line front end over access/
```

### Things not to do

- Never merge identities. Two packages with the same robot name, URDF hash or serial are two entities
  until a stated, provenanced link says otherwise; threads group by declared links, not guesses.
- Never edit a package. Packages are immutable inputs; a fix is a new compiler lineage, not a Ledger write.
- Never store interpretation. Inferred or model-generated annotations belong to layers above; the Ledger
  stores evidence, declared links and its own derived indexes, each marked as derived and rebuildable.
- Never import another member's internals or read another layer's storage; use `contracts/`.
- Never add a subpackage, table or abstraction without a concrete, present caller.

### Programme contract rule

A contract change is a PR to the **owning** package that bumps its version constant and updates the
goldens in `contracts/`. Consumers declare the version they build against in `docs/contracts.md` and
move it in their own PR. The Ledger builds against package-schema version 1 (`neptune.model.record.SCHEMA_VERSION`).
It reads newer versions only after a PR here raises the declaration; it never guesses.

### Model policy

Opus for `catalog/`, `lineage/`, `query/` and all ADRs; Sonnet otherwise.
<!-- END layer-specific -->

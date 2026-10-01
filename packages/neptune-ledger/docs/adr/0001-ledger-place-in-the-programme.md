# 0001 — The Ledger is layer 1: it consumes compiler packages and serves the layers above

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-85

## Context

Neptune's programme is a stack of workspace members in one repository. The compiler (`neptune`) turns
raw robotics evidence into immutable, provenance-preserving packages. Memory, Context, Deploy and Learn
need to find, relate and read those packages across many robots and runs without re-parsing raw data and
without each reading the compiler's on-disk layout. Something must own cataloguing, entity threads, a
queryable lakehouse and the lineage view of superseded packages. If every layer built its own, identities
would be merged inconsistently and parser-upgrade lineage would be lost.

## Decision

1. **Place.** The Ledger (`packages/neptune-ledger`, import `neptune_ledger`) is layer 1. It consumes
   compiler packages through the package schema and nothing else of the compiler's.
2. **Produces** for Memory, Context, Deploy and Learn: a catalog API, entity threads, a lakehouse, and a
   lineage current-view. These are the only surfaces upper layers use.
3. **Boundaries.** The Ledger never reads another layer's storage, never edits a package, never merges
   identities without a stated, provenanced link, and never stores interpretation; derived indexes are
   marked derived and are rebuildable from packages alone. It does not learn, retrieve or reason.
4. **Contract version.** The Ledger builds against package-schema version **1**
   (`neptune.model.record.SCHEMA_VERSION`, schema id `urn:neptune:schema:canonical:1`), declared in
   `docs/contracts.md`. A contract change is a PR to the owning package that bumps its version constant
   and its `contracts/` goldens; the Ledger moves its declaration in its own PR.
5. **Layout.** `catalog/`, `threads/`, `lineage/`, `lake/`, `query/`, `access/`, `cli/` under
   `src/neptune_ledger/`, created only when an issue needs them. The package depends on the root
   `neptune` project as a workspace source.

## Alternatives considered

- **Fold cataloguing into the compiler.** Rejected: the compiler's contract is per-source determinism;
  cross-package identity and lineage views change on a different cadence and would couple its releases
  to every upper layer.
- **Let each upper layer read packages directly.** Rejected: duplicated indexing, inconsistent identity
  decisions, and every layer coupled to the on-disk layout.
- **Pin by importing the compiler's constant at runtime only.** Rejected: the declaration must be
  reviewable in a PR, so it is written in `docs/contracts.md`; code may check it against the constant.

## Consequences

Upper layers get one stable surface and can be built in parallel against the declared version. The Ledger
must track compiler schema bumps deliberately and cannot silently ingest a newer version. Revisit if a
second producer of packages appears, or if lineage needs to be owned by the compiler.

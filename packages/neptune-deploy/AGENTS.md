# AGENTS.md — neptune-deploy

How to work in `packages/neptune-deploy/`. The repository-wide rules in the root `AGENTS.md` (workflow, git,
Linear, review, merge) apply unchanged; this file adds what is specific to this package.

## Shared non-negotiables (every workspace member)

1. **Own your directory.** Change files under `packages/neptune-deploy/` only. Anything another package
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
7. **Green before review.** `make check PKG=neptune-deploy` while iterating; CI runs this package's job
   when `packages/neptune-deploy/**`, `contracts/**` or root plumbing changes.
8. **All input is hostile; errors are structured.** Malformed, boundary and determinism tests for
   every substantial component; real small fixtures over mocks; test basenames unique repo-wide.

<!-- BEGIN layer-specific: owned by this package's coordinator; replace the stub below. -->
## Layer-specific rules

_Stub._ State what this layer is, what it must never become, and its own non-negotiables.
<!-- END layer-specific -->

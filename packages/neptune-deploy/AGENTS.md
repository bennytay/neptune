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

Deploy turns deployment lifecycle evidence (commissioning, authorisation envelopes, interventions,
maintenance, requalification, incidents, changes, risk assessments) into the compiler's canonical lifecycle
records, for an arm cell, an AMR fleet or any other deployment. It is a **compiler plugin**: it adds adapters
and read-only `Source`s through the compiler's entry points and nothing else
([ADR 0001](docs/adr/0001-a-compiler-plugin-of-adapters-and-read-only-sources.md)). It never becomes a
compliance engine, a safety case generator, a CMMS or a dashboard.

### Non-negotiables

1. **Lifecycle records are `stated` evidence only.** Each cites the one form, ticket, work order or register
   row it comes from, values exactly as declared (root ADR 0051): no ranking a severity, no converting a
   speed limit, no reading "approved with conditions" as a boolean.
2. **Never infer lifecycle state.** Nothing orders stages, decides that a deployment is commissioned,
   checks an intervention against its envelope, or decides that a requalification passed.
3. **Every connector is read-only.** A `Source` lists and fetches; it never writes, acknowledges,
   transitions or comments on anything it reads, and it uses the network only after `require_network`.
4. **Adapters follow the four-method ABI and the sandbox.** Leaves over `neptune.model`,
   `neptune.identity` and `neptune.adapters.contract`; no network, filesystem or subprocess. Each is
   registered under the `neptune.adapters` entry point and passes `neptune.adapters.conformance`.
5. **Record kinds come from the compiler model.** A missing field is a compiler PR, never a Deploy type.
   Deploy pins package schema 4 (`PACKAGE_SCHEMA_VERSION`, `docs/contracts.md`, `contracts/lock.toml`).

### Repo map (`src/neptune_deploy/`; subpackages appear as their issues land)

```
adapters/lifecycle/  lifecycle source formats (forms, CMMS exports, tickets, registers) -> lifecycle records
sources/             read-only connectors (CMMS, ticketing, fleet managers) as compiler `Source`s
packs/               the evidence-pack compiler: a deployment's lifecycle evidence assembled from packages
console/             a thin read-only front end over packs/
```

### Model policy

Sonnet by default (adapters, connectors, console); Opus for the evidence-pack compiler (`packs/`), ADRs and
gates.
<!-- END layer-specific -->

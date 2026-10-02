# 0006 — Graph-schema v1: contract surface, version policy and the MemoryReader

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-105

## Context

Context, Deploy and Learn read Memory's graph. ADR 0002 fixed the claim model, ADR 0003 the identity policy and
the consolidator contract, and ADR 0005 the superseding resolver. None of it was published: `GRAPH_SCHEMA_VERSION`
was `0`, consumers had no read API, and there was no test a consumer could run to know that a reader answers
correctly. The reviews of MVL-102, MVL-103 and #57 left inputs for this issue:

- a model field on claim provenance;
- whether `same_as` is core;
- findings that a store can key;
- an `as_of` that showed supersessions recorded after the snapshot;
- closure ids that silently change with the resolver's configuration;
- whitespace in declared identifiers.

Getting the surface wrong means every consumer codes against a moving target, and a reader that leaks later
knowledge makes "what did we believe at tx" unanswerable.

## Decision

Code: `neptune_memory/schema/` (`__init__`, `claim`, `predicates`, `supersede`, `codec`, `reader`, `reference`,
`export`) and `neptune_memory/contract/` (`golden`, `suite`, `worked_examples`, `_fixture_model`).
The guarantees are listed in [`docs/graph-schema.md`](../graph-schema.md).

### 1. The contract surface

Graph-schema v1 consists of the following:

- the types in `neptune_memory.schema`: nodes, `Claim` and its objects and provenance, `Interval`, `LedgerTx`, the
  predicate registry, `ResolutionFinding`, `Resolution`, `resolve`, `as_of` and `resolver_config`;
- the JSON shapes their `to_json` writes and `codec` reads strictly, including the *graph document*: a resolved
  history plus the resolver configuration;
- the `MemoryReader` protocol and its result types;
- the contract suite `neptune_memory.contract.suite.CHECKS`.

It is published in the Platform registry as `contracts/graph-schema/v1.0.0/`. That directory holds the JSON
Schema export (`schema.export.graph_schema`), the golden graph and the vocabulary as goldens, and
`contracts/graph-schema/goldens.py` as the generator.

### 2. Version policy

`GRAPH_SCHEMA_VERSION = 1` lives in `neptune_memory.schema` and is the registry major (`1.0.0`). The rules follow
Platform ADR 0002:

- A change that still accepts every published golden of its major is a minor or patch release. Adding a predicate
  is one example; adding an optional field is another.
- Anything else raises `GRAPH_SCHEMA_VERSION`: removing or narrowing a predicate, renaming a field, changing an id
  scheme.

`VOCABULARY_VERSION` (now `2`) and `RESOLVER_VERSION` (still `"2"`) are inputs to the generation (§7). They are
not registry versions. `scripts/contracts.py check-owner` fails when the export drifts from the latest published
version.

### 3. The model is in claim provenance

`ClaimProvenance.model: ModelRef | None` holds the model id and version. `Claim` enforces that a claim is inferred
exactly when its provenance names a model, so a deterministic claim can never carry one. The model is hashed into
the claim id, and it is written to JSON only when present, so deterministic claim ids are unchanged. The runner
stamps the consolidator's model into every claim. A model-based consolidator's resolved config still holds its
model, which keeps a model swap a new lineage (ADR 0003 §3) without widening the lineage key. A closure version
carries its original assertion's model.

### 4. Identity predicates are core

`same_as` and `same_as_candidate` join `CORE_PREDICATES`: every node type, `many`. Consumers traverse them, so
they must be in the published vocabulary. `VOCABULARY_VERSION` becomes `2`.
`consolidate.identity.IDENTITY_PREDICATES` remains as a name for `CORE_PREDICATES`. ADR 0003 §1.2 still holds and
the runner still enforces it: only `memory.identity` grounds `same_as`, and never by inference.
`same_as_candidate` stays pairwise, one claim in each direction.

### 5. Findings are contract objects

`ResolutionFinding` gains `provenance = FindingProvenance(resolver_id, resolver_version, config_hash)` and a
deterministic `id = "finding:" + sha256(canonical JSON of {code, claim, others, provenance})`. Like a claim id, it
excludes bookkeeping (`recorded_at`, `superseded_at`). A store therefore keys a finding on arrival and closes it in
place, in the same transaction as the claims it names. `MemoryReader` returns the findings active at `as_of` next to
the claims. These are the findings that name a returned claim, plus those whose own `claim` matches the query.
The second group keeps an `overridden_on_arrival` finding visible after its winners are superseded.

### 6. A snapshot never leaks later knowledge: masking

`as_of(resolution, tx)` returns the claims and findings active at `tx`. Each one is presented as known at `tx`: its
`superseded_at` is masked to `OPEN`. Every item in a snapshot was current at `tx`, so `OPEN` is exactly what was
known then. The alternative of omitting the field would make snapshot items a different shape from history items.
With masking, P4 holds exactly, bookkeeping included: `as_of(resolve(all), tx)` equals the current part of
`resolve(recorded ≤ tx)`. A claim's `supersedes` is fixed when the claim is recorded, so it never leaks. The
unmasked history stays in `Resolution.claims`.

### 7. A resolver configuration is a store generation

The *generation* is `resolver_config_hash(registry, priorities)`. Every closure id and every finding id covers it,
so any of the following re-ids every closure and every finding:

- adding a consolidator;
- changing a priority;
- extending the vocabulary;
- bumping `VOCABULARY_VERSION` or `RESOLVER_VERSION`.

Asserted claims keep their ids. The guarantee of ADR 0003 §3(a), that `as_of` at an earlier transaction answers as
before, therefore holds only within one generation. An ADR 0004 append-only store must start a **new generation**
when the configuration changes: it re-resolves from `assertions(history)` and never patches the old history.
Readers expose `generation`, and graph documents carry it. Ids and answers from two generations are never compared.

### 8. `MemoryReader` is a read protocol, separate from `MemoryStore`

`MemoryReader` provides `graph_schema_version`, `generation`, `head`, `node`, `claims`, `neighbours`, `episodes`
and `spatial`. Every query takes an `as_of` no later than `head`; a later one raises `AsOfBeyondHeadError`, so one
`as_of` always gets one answer. `head` is the latest Ledger transaction, which can be later than every
`recorded_at`, because a transaction may produce no claim. The graph document therefore carries `head`
explicitly. `node`, `claims` and `neighbours` all take `include_inferred`. Neighbour findings are those naming any
edge between two nodes of the result, so they do not depend on which shortest path `via` shows.

Every result type has a `to_json` and a JSON Schema definition: `NodeResult`, `NodeView`, `ClaimsResult`,
`NeighboursResult`, `Neighbour`, `EpisodesResult`, `EpisodeView`, `SpatialResult` and `SpatialView`. A
`Knowledge`-wrapped result is `known` with a value, or `not_covered`. The definitions are published with goldens
(`result.*.json`), so `check-owner` catches a renamed or retyped result field. The schema also states the claim
rules the codec enforces: inferred exactly when `provenance.model` is present, and deterministic only with
confidence `not_applicable`.

`schema.reference.ReferenceReader` is the in-memory reference built on `resolve` and `as_of`. The contract suite compares any reader with it on the golden graph, and checks the guarantees directly
as well. `contract.suite.StubReader` is the stub that the suite rejects.

`MemoryStore` (ADR 0004 §5) is unchanged and remains provisional. Its write side is refactored in G2, with
`ClaimId` keys, atomic resolution writes and finding ids. The Postgres adapter is not touched. G2 follow-up:
`MemoryStore.as_of_thread` returns unmasked `ClaimRecord`s with integer ids, so a Postgres-backed `MemoryReader`
must map rows to `Claim`, mask `superseded_at`, and join findings before it can pass the suite.

### 9. Declared identifiers are trimmed

A declared identifier's value must not be blank and must carry no leading or trailing whitespace. The compiler's
`LogicalId` accepts such values. Memory refuses them in two places:

- the person rule (`undeclared_person`);
- the identity consolidator, as `identity.malformed_record`.

`badge: 4411` is a different string from `badge:4411` that reads the same. Memory must not key a node by it, or
name a person by it.

### 10. The golden graph

The golden graph is built from the compiler's four worked examples: drone, manipulator, mobile robot and
quadruped. They are loaded into a `StubLedger` over five transactions, with a Ledger overlay: two operator logs
whose bytes live in the code, the two Ledger threads the identity assertion needs, and a final package that no
consolidator reads. Four consolidators run, in priority order:

- `golden.runs` is deterministic: `evidenced_by` per run, plus `recorded_by` where the run declares a machine.
- `golden.operator` makes stated statements and corrections.
- `memory.identity` is the real identity policy.
- `golden.fixture_model` stands in for a model and emits inferred claims. It lives in
  `contract/_fixture_model.py`: golden only, private, and never registered. Real model-based consolidators live
  in `derived/`.

The resolver then runs over the result. The review of PR #60 showed that the suite can only catch what the graph
contains, so the graph contains all of the following:

- deterministic and inferred claims;
- an inferred guess superseded at tx 3;
- an `overridden_on_arrival` finding whose winner is superseded at tx 4;
- a `clock_mismatch` that closes at tx 4, plus newer ones that stay open;
- split closure versions;
- an inferred `same_as_candidate` pair;
- a stated `same_as`;
- two corroborating `recorded_by` claims, observed by the log and stated by the operator;
- neighbour results that carry findings;
- a head (tx 5) after the last claim (tx 4).

The package tests keep six subtly wrong readers that each must fail the suite on this graph. Rebuilding is
byte-identical, and a test compares the rebuild with the published file. The golden consolidators are fixtures,
not production policy: in particular, keying a run node by its record id (`record:<id>`) is a golden-graph
convention, not an identity rule.

### 11. Provisional queries answer `NotCovered`

`episodes(filter)` and `spatial(site, frame, as_of)` are G3. Their signatures and result types (`EpisodeFilter`,
`EpisodeView`, `SpatialView`) are fixed now. In v1 every reader answers `NotCovered`, using the compiler's
missingness vocabulary, and never `Known(())`, which would read as "there are none". The suite asserts this. When
G3 lands, the answer becomes a minor version and the check changes with it.

## Alternatives considered

- **Model only in the resolved config (ADR 0003 §2).** A consumer would have to resolve a config hash to learn which
  model inferred a claim. Provenance is where consumers look.
- **Model as a fourth part of the lineage key.** This needs a new lineage type everywhere. Keeping the model in the
  config hash as well gives the same separation with no new key.
- **Keep identity predicates in a separate registry.** Consumers would validate `same_as` against a vocabulary they
  do not have.
- **Omit `superseded_at` from snapshots.** Snapshot claims would be a different shape from history claims, and
  every consumer would need two parsers.
- **Answer an `as_of` beyond the head from the head.** The same query would then change its answer as the Ledger
  grows. Refusing is honest.
- **Make `MemoryStore` the read API.** Its rows, integer ids and write methods are provisional (ADR 0004 §5).
  Consumers need a stable read surface now.
- **Return `[]` from `episodes` and `spatial` until G3.** An empty list reads as "there are none".
- **Hand-write the golden graph.** It would drift from the code that produces it. The generator and the test make
  drift impossible.

## Consequences

- Consumers code against `MemoryReader` and run `CHECKS` against whatever reader they use. A Postgres-backed reader
  in G2 must pass the same suite.
- Changing a resolver config is a migration: a new generation, re-resolved from assertions.
- Every inferred claim now names its model. Inferred claims in existing fixtures gained one.
- Deterministic claim ids are unchanged. Closure and finding ids changed, because the vocabulary version changed
  (§7); no store holds them yet.
- Revisit this ADR in these cases:
  - G3 lands episodes and spatial structure;
  - MVL-85 defines the Ledger's transaction and thread shapes;
  - a consumer needs per-predicate superseding policy, or findings beyond the resolver's.

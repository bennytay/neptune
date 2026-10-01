# 0003 — Identity policy, the consolidator contract, and consolidator lineage

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-103

## Context

Memory turns Ledger records into claims. Two failure modes would make that graph useless as evidence:

- **Silent identity merges.** Two quadrupeds built from one URDF, two arms sharing a calibration file, two AMRs
  on one site register: equal content is not the same robot (root ADR 0003). A merge that cannot be undone
  poisons every claim that hangs off the merged node.
- **Unreproducible claims.** If a claim's id or content depends on run order, wall clock, a model call or the
  previous state of the graph, Memory cannot be rebuilt from the Ledger and an upgrade cannot be compared with
  the version it replaces.

The claim, node and predicate schema is ADR 0002's (MVL-102, `neptune_memory/schema/`). This ADR fixes what
identity means, what a consolidator is, and how claims get their lineage. The compiler's `IdentityLink`
(MVL-82) and the Ledger catalog API (MVL-85) are not on `main`; the shapes consumed here are pinned when they
land.

## Decision

### 1. Identity policy

1. **One node per Ledger thread.** A node is keyed by its thread's declared logical id
   (`neptune.model.ids.LogicalId`): `NodeRef(node_type, "<namespace>:<value>")`, where the thread declares its
   node type (a namespace is a token without `:`, so the id is unique). Thread records with one logical id in
   several packages are one thread, one node; if they declare different node types there is no node and a
   finding. A node is never keyed by content, so equal bytes never produce one node.
2. **`same_as` has exactly three grounds**, each one Ledger record cited in the claim's `provenance.records`:
   - `identity_link`: the compiler's `IdentityLink`, emitted because both sides *declare* the same identifier
     (a PX4 `sys_uuid`, a serial). `assertion_kind = observed`.
   - `configuration_lineage`: a Ledger record declaring one thread the continuation of another (a
     re-commissioned arm cell). `assertion_kind = observed`.
   - `operator_assertion` with `predicate = same_as`: an operator's recorded assertion.
     `assertion_kind = stated`.
   Both ends must be nodes of the same type.
3. **Everything else is a candidate, never a fact.** Two same-type nodes that only cite a common source (one
   URDF, one register CSV) get a `same_as_candidate` claim each way. Its evidence is every ref into the shared
   sources and its records are the threads that cite them, so each candidate carries its own evidence. ADR
   0002's `ClaimObject` cannot be `Ambiguous`, so the ambiguity is the predicate: a subject's candidate claims
   plus the subject itself (the "distinct" reading) are its `Ambiguous` readings (`same_as_candidates`), at
   least two by construction. Nodes already joined by `same_as` are not candidates for each other.
4. **No merge operation.** `same_as` is a symmetric edge emitted once per grounding record, subject = the lower
   logical id in canonical JSON order. Queries traverse it; nothing rewrites or collapses nodes. Undoing an
   identity is superseding a claim, never splitting a node.
5. **Vocabulary.** `same_as` and `same_as_candidate` (`many`, every node type) extend `CORE_PREDICATES` as
   `consolidate.identity.IDENTITY_PREDICATES`; MVL-105 may fold them into the published core.
6. **Hostile input is findings.** A malformed record, a link naming a logical id with no node, a self-link or a
   cross-type link yields a finding and no claim; the rest of the build is unaffected.

Consumed Ledger record kinds (read only through `LedgerReader`), pinned when MVL-82 / MVL-85 land. Every one
carries `id` (record id), `valid_from` (compiler `Timestamp`) and `evidence` (non-empty `EvidenceRef` list):
`ledger_thread {logical_id, node_type}`, `identity_link {left, right, identifier}`,
`configuration_lineage {predecessor, successor}`, `operator_assertion {predicate, subject, object, operator}`.

### 2. Consolidator contract

A consolidator is a pure function `(LedgerReader, previous claims, resolved config) -> ClaimDrafts + findings`
with a token `consolidator_id`, a `version` and a `model` that is `None` for deterministic consolidators.

- **Deterministic consolidators** live in `consolidate/`, may not call models or the network, and emit only
  `observed` or `stated` claims.
- **Model-based consolidators** live in `derived/`, emit only `inferred` claims, and put
  `"model": {model_id, model_version}` in their resolved config, so the model is in every claim's
  `config_hash` (and so its id). The runner refuses to run one whose config does not name its model. This
  moves into `ClaimProvenance` if ADR 0002's provenance gains a model field.
- **The runner stamps provenance.** `run_consolidator` hashes the resolved config, stamps consolidator id,
  version, config hash and the Ledger transaction onto each draft to build a `schema.Claim`, de-duplicates and
  sorts evidence and records, and returns claims and findings sorted by id. A draft `Claim` refuses, one that
  breaks the predicate vocabulary, or one with the wrong `assertion_kind` becomes a finding; a raised exception
  becomes `consolidate.failed` and output of the wrong type `consolidate.bad_output`. A consolidator never sets
  ids.
- **"Previous claims"** are the claims of consolidators *earlier in the same build's declared order*; never a
  consolidator's own output and never a previous build's graph. That is what makes a re-run idempotent. A claim
  resting on earlier claims cites their records and evidence (provenance holds Ledger record ids only).

### 3. Lineage

A claim's id is ADR 0002's `Claim.id`: a hash of what it asserts (subject, predicate, object, valid interval,
`assertion_kind`, confidence) and its provenance (evidence refs, Ledger record ids, consolidator id, version,
config hash). That covers every input the issue names. The runner canonicalises evidence and record order, so
input order is irrelevant; Ledger transaction time is bookkeeping and not hashed. Same inputs give the same
id; a version, config or model change gives a sibling id. An upgrade adds sibling claims; nothing edits or
deletes a claim from an earlier lineage.

### 4. Rebuild

Memory is a deterministic function of (Ledger snapshot, ordered consolidator set, versions, configs).
`rebuild(ledger, plan, recorded_at=<snapshot tx>, registry=...)` runs the set in order and must reproduce the
graph byte for byte in canonical JSON. This is Memory's first guarantee, tested on every change, and the
contract `memory rebuild` (CLI, later) exposes.

## Alternatives considered

- **Merge nodes on a strong match.** Irreversible, and "strong" becomes a threshold someone tunes. Traversing
  `same_as` costs one hop per query and keeps every identity decision revisable.
- **Treat shared content (same URDF hash) as `same_as`.** The exact failure the compiler's identity ADR forbids:
  identical descriptions are routinely shared across a fleet.
- **One `same_as_candidate` claim with an `Ambiguous` object listing every candidate.** ADR 0002's claim object
  is a single node, literal or record; widening it for one predicate costs every consumer a case. Pairwise
  claims carry per-candidate evidence natively.
- **Feed a consolidator its own previous output.** Makes the output a function of history, so a rebuild from the
  Ledger differs from the incremental graph.
- **A separate claim-id formula in `consolidate/`.** Two id schemes for one claim would diverge; ADR 0002's id
  already hashes the transform.

## Consequences

- Every identity decision is an inspectable claim with evidence; operators fix identity by asserting, not by
  editing.
- Queries must traverse `same_as` (Context's job); the graph never pretends two threads are one row.
- Upgrading a consolidator doubles its claims until the old lineage is retired by policy; storage pays for
  comparability.
- Revisit when MVL-82 defines `IdentityLink` (adopt its shape in the parser, keep the policy), when MVL-85
  defines the catalog API (record shapes, snapshot transaction), or when ADR 0002's provenance gains a model.

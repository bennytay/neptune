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

The claim and node schema is MVL-102's (`neptune_memory/schema/`); this ADR fixes only what identity means,
what a consolidator is, and how claim ids are derived. The compiler's `IdentityLink` (MVL-82) and the Ledger
catalog API (MVL-85) are not on `main`; the shapes consumed here are pinned when they land.

## Decision

### 1. Identity policy

1. **One node per Ledger thread.** A node's key is its thread's declared logical id (`neptune.model.ids.LogicalId`,
   `namespace` + `value`). Thread records with the same logical id in several packages are one thread, one node.
   A node is never keyed by content, so equal bytes never produce one node.
2. **`same_as` has exactly three grounds**, recorded as `ground` in the claim object:
   - `declared_identifier`: an `IdentityLink` the compiler emitted because both sides *declare* the same
     identifier (a PX4 `sys_uuid`, a serial). `assertion_kind = observed`.
   - `configuration_lineage`: a Ledger configuration-lineage record declaring one thread the continuation of
     another (a re-commissioned arm cell). `assertion_kind = observed`.
   - `operator_assertion`: an operator's recorded assertion. `assertion_kind = stated`, provenance cites the
     assertion record.
3. **Everything else is a candidate, never a fact.** Threads that only share evidence (one URDF, one register
   CSV) get one `same_as_candidate` claim per subject node per shared content id. Its knowledge state is
   `Ambiguous`; its object lists every candidate node (the subject included, which is the "distinct" reading, so
   there are always at least two) with the record ids that are the evidence for each. Nodes already joined to
   the subject by `same_as` are not candidates for it.
4. **No merge operation.** `same_as` is a symmetric edge emitted once, subject = the lower logical id in
   canonical JSON order. Queries traverse it; nothing rewrites or collapses nodes. Undoing an identity is
   superseding a claim, never splitting a node.
5. **Hostile links are findings.** A link or assertion naming a logical id with no thread, a self-link, or a
   malformed record yields a finding and no claim.

Consumed shapes (Ledger record kinds, read only through `LedgerReader`), pinned when MVL-82 / MVL-85 land:
`ledger_thread {id, logical_id, sources[content id]}`, `identity_link {id, left, right, identifier}`,
`configuration_lineage {id, predecessor, successor}`, `operator_assertion {id, predicate, subject, object,
operator}`. Only `predicate = same_as` assertions are identity's concern.

### 2. Consolidator contract

A consolidator is a pure function `(LedgerReader, previous claims, resolved config) -> claim drafts + findings`
with a token `consolidator_id`, a `version` and a `model` that is `None` for deterministic consolidators.

- **Deterministic consolidators** live in `consolidate/`, may not call models or the network, and may emit only
  `observed` or `stated` claims.
- **Model-based consolidators** live in `derived/`, set `model = (model_id, model_version)`, emit only
  `inferred` claims, and the model is hashed into every claim id and carried in every claim's transform.
- **The runner stamps provenance.** `run_consolidator` hashes the resolved config, derives every id, drops a
  draft without input records or with the wrong `assertion_kind` (a finding, not an exception), turns a raised
  exception into a `consolidate.failed` finding (and output of the wrong type into `consolidate.bad_output`), and
  returns claims sorted by id. A consolidator never sets ids.
- **"Previous claims"** are the claims of consolidators *earlier in the same build's declared order*; never a
  consolidator's own output and never a previous build's graph. That is what makes a re-run idempotent.
- **Drafts, not `Claim`s.** Until MVL-102's `Claim` is on `main`, the runner returns `ProposedClaim(id, draft,
  transform)`; `schema/` builds its `Claim` from that triple. Memory has one claim model, MVL-102's.

### 3. Lineage

`claim_id = record_id("memory.claim", {consolidator_id, consolidator_version, config_hash, inputs, predicate,
subject, object, assertion_kind, state[, model]})` using the compiler's canonical JSON and sha256 record-id scheme. `inputs` is the
sorted, de-duplicated set of Ledger record ids (and earlier claim ids) the claim rests on, so input order is
irrelevant. `assertion_kind` and knowledge `state` are hashed too, so a stated and an observed claim (or a
known and an ambiguous one) on the same inputs are never one id. Same inputs give the same id; a version or config change gives a sibling id. An upgrade adds sibling
claims; nothing edits or deletes a claim from an earlier lineage.

### 4. Rebuild

Memory is a deterministic function of (Ledger snapshot, ordered consolidator set, versions, configs).
`rebuild` runs the set in order and must reproduce the graph byte for byte in canonical JSON; this is Memory's
first guarantee, tested on every change, and the contract `memory rebuild` (CLI, later) exposes.

## Alternatives considered

- **Merge nodes on a strong match.** Irreversible, and "strong" becomes a threshold someone tunes. Traversing
  `same_as` costs one hop per query and keeps every identity decision revisable.
- **Treat shared content (same URDF hash) as `same_as`.** The exact failure the compiler's identity ADR forbids:
  identical descriptions are routinely shared across a fleet.
- **Feed a consolidator its own previous output.** Makes the output a function of history, so a rebuild from the
  Ledger differs from the incremental graph.
- **Random or sequence claim ids.** Breaks idempotence and sibling comparison across versions.
- **A Memory-local `Claim` dataclass now.** Two claim models would diverge; the draft/triple seam is discarded
  when MVL-102 lands.

## Consequences

- Every identity decision is an inspectable claim with evidence; operators fix identity by asserting, not by
  editing.
- Queries must traverse `same_as` (Context's job); the graph never pretends two threads are one row.
- Upgrading a consolidator doubles its claims until the old lineage is retired by policy; storage pays for
  comparability.
- Revisit when MVL-82 defines `IdentityLink` (adopt its shape in the parser, keep the policy) or when MVL-102's
  `Claim` lands (replace `ProposedClaim`).

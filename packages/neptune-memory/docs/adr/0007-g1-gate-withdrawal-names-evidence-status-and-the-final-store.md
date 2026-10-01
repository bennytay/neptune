# 0007 — G1 gate: withdrawal, names, evidence status, and the final store decision

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-106

## Context

The G1 gate ([review](../reviews/g1-stress-test.md)) ran eight hostile scenarios through the code of ADRs
0001–0006 and re-measured the store. Most held. Four findings need a decision before Context C1 codes against
graph-schema v1:

- **Nothing can withdraw a claim.** A `many` claim (`same_as`, `has_configuration`) never contradicts, so it
  ends only by its own `valid_to` or by lineage retirement. A consolidator that stops emitting a claim (the
  operator retracted it, the clock mapping it was timed by was revised, a v2 parser emits nothing) leaves it
  current. ADR 0003 §1.4 says "undoing an identity is superseding a claim", and the resolver cannot do that.
  ADR 0003 §3 already lists the empty-upgrade case as a known gap.
- **Names have no predicate.** A renamed site keeps its node, but the vocabulary has nowhere to put its name.
- **Retention has no signal.** Claims survive the expiry of the bytes they cite, but neither `LedgerReader`
  nor catalog-api v1 says that bytes expired, so a reader cannot mark an `EvidenceRef` unavailable.
- **The store decision was provisional on three numbers.** Graph-filtered recall@10 was 0.89 on 20 queries
  against a 0.9 budget. The 10^8 as-of bound was a warm-cache extrapolation of data that will not fit in
  memory. The walk's visited set was a linear scan per edge.

## Decision

This ADR supersedes three passages and leaves everything else in ADRs 0001–0006 standing:

- ADR 0003 §1.4, the sentence "Undoing an identity is superseding a claim, never splitting a node", is replaced
  by §5 below. The rest of §1.4 (no merge operation) stands.
- ADR 0003 §3, the "Known gap" bullet, is replaced by §5.
- ADR 0004 Decision 4 (budgets) and the recall paragraph of its Consequences are replaced by §7. Decision 1 is
  made final there.

### 1. What the gate settled without a change

Identity by declared logical id, never content (ADR 0003 §1); valid time stored as declared on its own clock
(ADR 0002 §3); lineage retirement on upgrade (ADR 0003 §3, ADR 0005 §6); bi-temporal snapshots with masking
(ADR 0006 §6). Each is pinned by a `tests/test_g1_stress_*.py` test.

### 2. A name is a claim, never an identity

`has_name`: every node type → `text`, cardinality `one`, version 1, "a declared display name, verbatim; never
an identifier". It is stated or observed from the record that declares it. A rename is a new claim valid from
the rename; the old name keeps its interval as a closure; the node and every claim naming it keep their ids.
A rename that changes the declared identifier is a new thread, so a new node, joined to the old one only by a
`same_as` ground (ADR 0003 §1.2), never by resemblance. MVL-126 adds `has_name` to `CORE_PREDICATES`; that is
a graph-schema minor release with `VOCABULARY_VERSION = 3`, and so a new generation (ADR 0006 §7).

### 3. Memory models no node lifetime

A claim is never clamped, refused or flagged because its valid time precedes the thread of its subject or of
a related node. Components are calibrated before they are mounted, and logs are back-filled. Plausibility
checks are interpretation: an inferred claim under `derived/`. A date from an unset real-time clock stays on
that clock's own domain unless its epoch is declared (ADR 0002 §3).

### 4. Clock mappings never re-time a claim

No consolidator applies a clock mapping to a claim's valid time. When MVL-130 consumes alignment records, a
mapping is a claim about clocks with its own validity. A claim re-timed through a mapping is a *derivative*: a
separate claim on the target clock, whose `provenance.records` cite the mapping's record. The source-clock
claim is never touched. When the mapping is revised, its derivatives are withdrawn (§5) and recomputed under
the new mapping, at the revision's transaction.

### 5. Withdrawal: a build is a complete statement of its lineage

Shape fixed here; built by MVL-132 (resolver, graph document, rebuild), with MVL-126 for retraction records.

1. **A build** is `(consolidator id, version, config hash, recorded_at)`: one lineage ran over one Ledger
   snapshot at one transaction, and emitted a set of claims, possibly empty. `rebuild` records one build per
   consolidator in the plan.
2. **Withdrawal rule.** At a build of lineage `L` at transaction `t`, every current version whose original
   assertion belongs to `L` and is not emitted by this build gets `superseded_at = t`. Valid time is not cut,
   nothing is deleted, and `as_of` before `t` answers as before. A build of a *new* lineage of the same
   consolidator withdraws all current versions of its other lineages, which is ADR 0003 §3(c) extended to the
   empty build: the known gap is closed.
3. **Scope.** A build is complete over the snapshot it read (ADR 0003 §4). An incremental build declares the
   packages it read, and withdraws only claims whose `provenance.records` all come from those packages.
4. **Retraction** is a Ledger record, `operator_retraction {id, retracts: record id, operator, evidence}`.
   It never edits the retracted record. A consolidator emits no claim that rests on a retracted record, and
   the build's withdrawal ends it. This works for `one` and `many` predicates, `same_as` included. A
   retraction that replaces a `one` fact is just the new stated claim, which supersedes on arrival
   (ADR 0005 §1); that already works.
5. **Contract.** `resolve` gains an optional `builds` argument, and the graph document gains an optional
   `builds` list. Without builds the behaviour is v1's, so this is a graph-schema minor release. P1–P8 hold
   with builds in the input; a new property P9 states that no current version belongs to a lineage whose
   latest build did not emit it.

### 6. Evidence availability sits beside the claim, never in it

A claim's content, id and `EvidenceRef`s never change when cited bytes expire. The Ledger keeps catalog
records after their bytes expire, so a rebuild is unchanged. Availability is a bi-temporal fact about a
source, joined at read time:

- `LedgerReader.evidence_status(source: ContentId, as_of: LedgerTx) -> EvidenceStatus`, one of `available`,
  `expired {at: LedgerTx, policy: str}` or `unknown`. An unknown status is never read as available.
- `MemoryReader` results gain an optional `evidence_status` map, keyed by every source cited by a returned
  claim, as of the query's `as_of`. This is a graph-schema minor release.

The catalog-api owner is asked for the signal; MVL-132 consumes it. Until then readers return refs without
status, which is the explicit absence of a signal, not "available".

### 7. The store decision is final

ADR 0004 Decision 1 is final: **PostgreSQL 16 + pgvector, queried with SQL**. Apache AGE stays optional.
`MemoryStore` stays provisional only in its row shape, which G2 maps to `schema.Claim` (ADR 0006 §8).

Two changes to the adapter come with it:

- **Graph-filtered search is exact over the scope.** `vector_top_k(within=...)` computes distances for every
  embedding of the walk's scope, through the subject index, in a materialized CTE that the HNSW index cannot
  serve. Unfiltered search keeps HNSW.
- **`ef_search` defaults to 400** for unfiltered search.

The C2 budgets stand as ADR 0004 set them. The G1 numbers are below; raw data is in
[`../benchmarks/g1-results.json`](../benchmarks/g1-results.json), and `tests/test_g1_stress_scale.py` holds
every row to its budget. m = measured; x = extrapolated upper bound at 10^8 claims.

| Budget | G1 value | Label |
|---|---|---|
| As-of thread p50 < 300 ms, p99 < 1 s, warm cache | 2.04 / 3.48 ms at 10^8 | x (ADR 0004) |
| The same, cold: OS page cache evicted before every query, `shared_buffers` 16 MB | 3.38 / 6.53 ms at 10^7 (30 device reads, 2.6 ms of I/O per query) | m |
| The same, cold | 6.75 / 13.1 ms at 10^8 | x: 2× 10^7 (10^6 → 10^7 grew ×1.71 at p50, ×1.32 at p99) |
| 3-hop traversal p50 < 300 ms | 0.89 ms at 10^7; 1.88 ms at 10^8 | m; x (ADR 0004) |
| Graph-filtered top-10 recall@10 ≥ 0.9 | 1.0 at 10^6 embeddings, 200 queries | m |
| Graph-filtered top-10 p50 < 300 ms | 6.5 ms at 10^7 (25,000 embeddings per scope); 65 ms at 10^8 | m; x (linear in scope) |
| Superseding writes ≥ 200/s, thread p99 < 1 s under them | 1,064/s, 1.68 ms at 10^7 | m (ADR 0004) |
| Full rebuild of 10^8 claims < 2 h | 404 s at 10^7; 5,384 s at 10^8 | m; x, **unproven** (GAP, MVL-132) |

- **Recall.** The filtered HNSW query was re-measured on all 200 seeded queries, unprepared, against a top-10
  computed in numpy from the scope's embeddings. Recall@10 is 0.855 / 0.867 / 0.881 / 0.887 at `ef_search`
  40 / 100 / 200 / 400. Between 47 and 73 queries fall below 0.9, and some return none of the true ten:
  a 2-hop scope holds about 2.5% of the embeddings, and the iterative scan gives up before it finds them.
  Tuning does not reach the budget. The exact scan does by construction, at about the same latency (6.5 ms
  against 6.2 ms p50). Its cost is linear in the scope's embeddings, so the 10^8 figure is 10× the 10^7 one.
  Unfiltered recall@10 is 0.66 / 0.82 / 0.89 / 0.93 at the same settings; it has no budget, and 400 is the
  first setting above 0.9, at 4.3 ms p50.
- **A measurement trap.** psycopg prepares a statement after five executions, and a prepared plan survives
  a later change to `enable_indexscan`. A first grid, run with the index off for ground truth, therefore
  read 1.0 for every setting. `bench/g1_bench.py` now never prepares a vector query.
- **Cold.** Each cold thread reads about 30 blocks from the device. Postgres's 16 MB of buffers keep only the
  upper index pages. A thread's visible rows do not grow with history depth, and the GiST index gains at most
  one level per 10× claims, so 2× the 10^7 figure bounds 10^8. Both bounds are about 75× under budget.
- **Walk.** The keyed visited set (§F1 of the review) leaves ordinary sites unchanged (0.89 ms against 0.895 ms)
  and makes hubs linear: 10^4 machines in 379 ms against 2.09 s before, and 10^5 in 0.79 s against 173 s.
- **Rebuild** is still the one unproven budget. MVL-132 owns measuring it at 10^8 on production-class
  hardware, with a partitioned or parallel index build if it misses. Neither changes the engine.

## Alternatives considered

- **Retraction as a negated claim** (`not same_as`): it doubles the vocabulary, and every consumer would have
  to cancel pairs. Withdrawal ends the claim where it is.
- **Withdrawal inferred from re-recordings, with no build records**: `resolve` keeps only the earliest
  recording of an id, so the evidence is gone after one round trip, and an empty build is invisible.
- **Delete the claim on retraction or expiry**: this breaks "nothing deleted" and every earlier `as_of`.
- **Put availability in `EvidenceRef` or the claim**: claim ids would change when bytes expire, which would
  re-id history for an operational fact.
- **Add `has_name` in this gate**: it is a vocabulary change and a new generation with republished goldens.
  G2 adds predicates for every consolidator anyway, so it rides with MVL-126.
- **Tune HNSW for the filtered query** (`ef_search`, strict ordering, over-fetching): it plateaus at 0.887,
  because the scan stops before it reaches a scope that small. A larger tuple budget makes it slower than
  the exact scan it approximates.
- **Supersede the recall budget**: the exact scan meets it at the same latency, so the budget stands.

## Consequences

- C1 can code against v1 now: every change above is a minor graph-schema release.
- G2 owns three GAPs: withdrawal and builds (MVL-132), retraction records and `has_name` (MVL-126), and
  mapping derivatives (MVL-130). Strict `xfail` tests pin each one, and flip when it lands.
- Revisit this ADR if production-scale rebuild misses its budget after partitioning, or if the Ledger cannot
  record builds.

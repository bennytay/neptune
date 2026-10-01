# G1 gate: stress test of the claim model and the store

- Date: 2026-10-02 · Issue: MVL-106 · Reviewed: Memory ADRs 0001–0006, `docs/graph-schema.md`,
  `neptune_memory.schema`, `consolidate`, `store`, `contract`
- Method: every scenario was run, not walked on paper. Each one is a `tests/test_g1_stress_*.py` module
  that drives the real code: Ledger records in a `StubLedger`, the consolidator runner and the identity
  policy, the resolver, and the reference `MemoryReader` over a graph document. Where a behaviour is
  missing, a strict `xfail` test states the expected behaviour, so it fails loudly once it is built. The
  scale scenario re-ran the MVL-104 benchmark at 10^7 claims (`bench/g1_bench.py`) and walks a
  10^4-entity hub on a live PostgreSQL.
- Embodiments: humanoids, arms, an AMR fleet, a marine harbour, an aerial camera, a quadruped, a marine
  ROV, an AUV, and the six-embodiment benchmark fleet.
- Outcome: the claim model holds on identity, time, clock refusal, upgrades and expiry. One mechanism is
  missing: nothing can **withdraw** a claim (retraction, a revised mapping, an empty upgrade). [ADR 0007](../adr/0007-g1-gate-withdrawal-names-evidence-status-and-the-final-store.md)
  fixes its shape and that of names and evidence status, and makes the store decision final with re-measured
  numbers. Every contract gap is a minor graph-schema release owned by a G2 issue, so Context C1 may start once this
  is merged and `main` is tagged `g1-gate`.

## Scenarios

| # | Scenario | Expected (ADRs) | Test | What happened | Verdict |
|---|---|---|---|---|---|
| 1 | Two robots, identical URDFs, no declared ids | One node per Ledger thread, keyed by its logical id, never by content; no `same_as` without a declared ground; shared bytes across namespaces give pairwise `same_as_candidate`, inside one namespace nothing (0003 §1) | `test_g1_stress_identical_urdf.py` | Three humanoids keyed by their run logs, one URDF: three nodes, no claim at all. A maintenance-controller thread citing the same URDF is ambiguous between all three: six candidate claims, four readings, reader views stay separate, traversal crosses only candidate edges and says so. Two UR5e cells in two vendor namespaces: one candidate pair. Output is independent of package order. | HOLDS |
| 2 | A site renamed | The node is the declared id, so it survives; the name is a `one` claim with an interval; an identifier that *is* the name gives a new node joined only by a declared ground (0002 §2–§4, 0003 §1, 0007 §2) | `test_g1_stress_site_rename.py` | `WH-07`'s two register rows are one node. "Warehouse 7" → "Northgate Fulfilment": the old name becomes a closure over [Jun 2025, Mar 2026), `as_of(1)` shows the old name open-ended, the AMR's `located_at` keeps its id. A harbour register keyed by name gets two nodes and no guess, then one `same_as` from a lineage record. `has_name` is not in the core vocabulary, so the test registers it. | GAP: `has_name` (MVL-126) |
| 3 | A calibration dated before the robot existed | Valid time as declared, never clamped; Memory models no node lifetime; plausibility is `derived/`; an undeclared-epoch date stays on its clock (0002 §3, 0007 §3) | `test_g1_stress_early_calibration.py` | A drone camera's factory calibration from January predates the drone's June thread: returned from January, cut only by the 2026 recalibration, found by a `during` before commissioning, no finding. A 1970 date from an unset RTC stays on the camera's own clock: in `other_clocks`, `clock_mismatch` with each civil calibration, never ordered. | HOLDS |
| 4 | A clock mapping revised after claims were made on it | A claim keeps the clock its record declares and is never compared across clocks; a claim re-timed through a mapping is a derivative that cites the mapping and is withdrawn when the mapping is revised (0002 §3, 0005 §2, 0007 §4) | `test_g1_stress_clock_mapping.py` | **Holds:** a quadruped's boot-clock diagnostic stays on its own clock, sits in `other_clocks`, and competes with the civil work order only as `clock_mismatch`. **Not tested by anything that holds:** no consolidator reads `clock_alignment` records today, so nothing can make a claim through a mapping. The test consolidator ignores the mapping too, which is why its rebuilds are byte-identical; that proves nothing about revision. A derivative re-timed through the old mapping stays current after the revision (strict `xfail`). | `clock_mismatch` refusal HOLDS; revised mapping GAP (MVL-130 + MVL-132) |
| 5 | A consolidator upgrade that changes a claim's object | v2 is a new lineage at a new transaction; its first claim retires v1 at that transaction; no contradiction between versions; earlier `as_of` unchanged; reruns idempotent; rollback refused (0003 §3, 0005 §6, 0006 §7) | `test_g1_stress_upgrade.py` | ROV "thruster fault" → "thruster 3 (port vertical) fault": sibling id, v1 superseded at tx 2 with its interval intact, no closure and no finding, `as_of(1)` unchanged. A fact v2 no longer derives is retired with v1. A rerun keeps ids and first `recorded_at`; v1 → v2 → v1 raises `lineage_reuse`. An upgrade emitting nothing retires nothing (strict `xfail`). | HOLDS; empty upgrade (MVL-132) |
| 6 | An operator assertion later retracted | Retraction is a new record, hence a new claim or a closure; nothing deleted; `as_of` before it shows what was said (0002 §4, 0005 §1, 0007 §5) | `test_g1_stress_retraction.py` | An AMR "parked in bay 4" corrected to bay 2: the correction supersedes, lists the mistake in `supersedes`, `as_of(1)` still says bay 4, bay 4 is `NotCovered` now. An operator `same_as` between two humanoid threads cannot be retracted: `many` never contradicts and a consolidator that stops emitting a claim does not end it (strict `xfail`). ADR 0003 §1.4 promised "undoing an identity is superseding a claim"; the resolver could not. | GAP: withdrawal (MVL-132), retraction record (MVL-126) |
| 7 | A claim whose evidence package expired under retention | Claims survive with their refs and ids; availability is a signal beside the claim (0002 §2, 0003 §4, 0007 §6) | `test_g1_stress_retention.py` | A drone flight log and an AUV dive log expire at tx 4: claims rebuilt at tx 5 are byte-identical, ids and first `recorded_at` kept, both snapshots return them with their `EvidenceRef`s. `LedgerReader` (and catalog-api v1) has no retention state. Under ADR 0007 §6 every cited source gets a `Knowledge[EvidenceStatus]`, which is `NotCovered` until the catalog emits a signal; that map is not built yet. | GAP: evidence status (MVL-132) |
| 8 | 10^8 claims under the store budgets | Budgets met, measured where possible, labelled extrapolations or estimates where not (0004, 0007 §7) | `test_g1_stress_scale.py`, `bench/g1_bench.py` | See Measurements. The filtered HNSW query reaches only 0.877 recall@10 on 200 queries, and no tuning reaches 0.9, so scopes up to 500,000 embeddings are now scanned exactly: 1.0, at 8.0 ms p50. Scopes above the cutoff keep the filtered HNSW scan, whose recall was not measured there and was below budget (0.899) wherever measured. Cold as-of measured at 10^7: 3.4 / 6.5 ms. A 10^4 hub walks in 379 ms, against 2.09 s before. Rebuild of 10^8 is still only an extrapolation. | HOLDS; GAPs: wide-scope recall and 10^8 rebuild (MVL-132) |

## Fixed in this gate

- **F1, the walk's visited set** (`store/postgres.py`). It was a `text[]` checked with `<> ALL` on every
  edge, so a walk around a hub was quadratic. It is now a `jsonb` object checked with `jsonb_exists`, which
  is a keyed lookup, and each level adds its entities with one `||`. Tested on a live 10^4-machine hub.
- **F2, `_require_idle`.** A connection without `info.transaction_status` used to pass the idle check by
  default, so the store could commit a caller's open transaction. The constructor now refuses it.
- **F3, the rollback after an error.** A rollback that failed in `_read`'s `finally` replaced the original
  error. The original now propagates, with the rollback failure attached as a note; `_write` does the same.
- **F4, the live-test teardown.** It dropped an AGE graph unconditionally, so it failed without AGE. It now
  drops the graph only when the extension exists, and the snapshot check skips without it.
- **F5, graph-filtered vector search.** It was an HNSW scan filtered by the walk's scope, with recall@10 of
  0.877 on 200 queries, and some queries got none of the true ten. It is now an exact scan of the scope's
  embeddings, for scopes of up to 500,000 embeddings: recall 1.0 at 8.0 ms p50. Wider scopes keep the
  filtered HNSW scan so that a 6-hop or hub query cannot become a full scan. That bounds cost, not recall:
  the branch's recall is below budget wherever measured (GAP, MVL-132). Vector reads force custom plans
  (`plan_cache_mode`), because every latency was measured unprepared. Unfiltered search keeps HNSW, now at
  `ef_search = 400` (recall@10 0.93). See ADR 0007 §7.
- **F6, the ADRs.** ADR 0007 supersedes ADR 0003 §1.4's undo sentence and §3's known gap (withdrawal), and
  ADR 0004 Decision 4 (budgets). Status lines are updated; all seven ADRs are Accepted.

## Gaps and owners

| Gap | Shape fixed in | Owner | Pinned by |
|---|---|---|---|
| Withdrawal: builds, the withdrawal rule, P9; also closes the empty-upgrade gap | ADR 0007 §5 | MVL-132 | `xfail` in `test_g1_stress_retraction.py`, `test_g1_stress_upgrade.py`, `test_g1_stress_clock_mapping.py` |
| `operator_retraction` records in the identity consolidator | ADR 0007 §5.4 | MVL-126 | `xfail` in `test_g1_stress_retraction.py` |
| `has_name` in the core vocabulary (`VOCABULARY_VERSION = 3`) | ADR 0007 §2 | MVL-126 | `test_has_name_is_not_core_yet` |
| Re-timed derivatives cite their mapping and are withdrawn on revision | ADR 0007 §4 | MVL-130 + MVL-132 | `xfail` in `test_g1_stress_clock_mapping.py` |
| Evidence status: `Knowledge[EvidenceStatus]` for every cited source, `NotCovered` until the catalog signals | ADR 0007 §6 | MVL-132 | `test_there_is_no_retention_signal_to_mark_a_ref_unavailable_yet` |
| Recall of filtered search above the 500,000-embedding exact cutoff (below budget where measured) | ADR 0007 §7 | MVL-132 | the `gap` row in `g1-results.json` |
| Full rebuild of 10^8 claims in < 2 h | ADR 0007 §7 | MVL-132 | the `unproven` row in `g1-results.json` |

The behavioural `xfail`s are strict with `raises=AssertionError`. Their harness passes builds to `resolve` as
soon as `resolve` accepts them (ADR 0007 §5.5), so they flip to failures when MVL-132 lands.

## Measurements

The MVL-104 host and engines (ADR 0004 Method): i5-13600K, 20 threads, NVMe, ~5 GB free RAM, Postgres
16.15 + pgvector 0.8.1, `shared_buffers` 2 GB unless stated. Data: the seeded 10^7-claim fleet (200 robots
across six embodiments), 10^6 embeddings. Raw results: [`../benchmarks/g1-results.json`](../benchmarks/g1-results.json).

| Workload | 10^6 | 10^7 | 10^8 | Budget |
|---|---|---|---|---|
| As-of thread p50 / p99, warm | 0.63 / 1.09 ms m | 0.76 / 1.36 ms m | 2.04 / 3.48 ms x | < 300 ms / < 1 s |
| As-of thread p50 / p99, cold (page cache evicted per query, 16 MB buffers) | 1.97 / 4.94 ms m | 3.38 / 6.53 ms m | 6.75 / 13.1 ms e | < 300 ms / < 1 s |
| Device reads / I/O time per cold thread | 21 blocks / 1.4 ms m | 30 blocks / 2.6 ms m | — | — |
| 3-hop traversal p50, ordinary site | 0.43 ms m | 0.89 ms m | 1.88 ms x | < 300 ms |
| 3-hop walk, hub of 10^4 machines (2·10^4 + 1 entities) | — | 379 ms m (text[]: 2.09 s) | — | none |
| 3-hop walk, hub of 10^5 machines | — | 0.79 s m (text[]: 173 s) | — | none |
| Graph-filtered top-10, exact: recall@10, p50 / p99 | — | 1.0, 8.0 / 10.8 ms m | 80 ms p50 x | ≥ 0.9, < 300 ms |
| Graph-filtered top-10, HNSW at `ef_search` 40 / 100 / 200 / 400 (replaced) | — | 0.867 / 0.877 / 0.890 / 0.899 m | — | ≥ 0.9 |
| Unfiltered top-10 recall@10 at `ef_search` 40 / 100 / 200 / 400 | — | 0.67 / 0.81 / 0.89 / 0.93 m | — | none |
| Rebuild from CSV | 28.5 s m | 404 s m | 5,384 s x, unproven | < 2 h |

- m = measured, x = extrapolated upper bound, e = estimate from a single 10× step (cold 10^8). ADR 0004's numbers: warm thread rows, the 10^6 traversal,
  the 10^6 rebuild and every 10^8 row except the cold and vector ones.
  Cold 10^8 is twice the 10^7 figure, an estimate from the one 10^6 → 10^7 step (×1.71 at p50, ×1.32 at p99).
- The vector rows use 10^6 embeddings, 200 seeded queries, all unprepared, scored against a top-10
  computed in numpy from every embedding of the walk's scope. A first grid read 1.0 for everything: psycopg
  had prepared the ground-truth statement with the index off, and the cached plan outlived the setting.
- Cold reads are real device reads: `track_io_timing` puts 85 µs on each of about 30 blocks.

## Questions

- **Can C1 code against graph-schema v1 now?** Yes. Every gap above lands as a minor release: an added
  predicate, an optional `builds` list, an optional `evidence_status` map. No published shape changes.
- **Is the claim model safe from silent identity merges?** Yes. Identity comes only from declared ids and
  grounds. Equal content is at most a candidate, and readers never collapse nodes.
- **Can the graph forget?** Only in transaction time. Until withdrawal lands it cannot even do that for a
  retracted `many` fact, which is the main G2 item this gate hands on.
- **Is the store decision final?** Yes (ADR 0007 §7). Two numbers are still open, and MVL-132 owns both:
  rebuild time at 10^8, and recall above the exact-scan cutoff. Neither mitigation (partitioned or parallel
  index builds; a larger or selectivity-relative cutoff) changes the engine.

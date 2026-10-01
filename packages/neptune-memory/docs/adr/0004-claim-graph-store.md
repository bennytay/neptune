# 0004 — Claim graph store: PostgreSQL 16 + Apache AGE + pgvector, measured against Neo4j 5

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-104

## Context

Memory persists a bi-temporal claim graph: every claim has a valid interval on a named clock and a
transaction interval `[recorded_at, superseded_at)`; entity-valued claims are graph edges; some claims
carry embeddings. The programme's working choice was PostgreSQL + Apache AGE + pgvector, so that claims,
graph and the Ledger catalog share one transactional database. G2 builds on the store, so the choice had
to survive a benchmark against the obvious alternative, Neo4j 5 community with its vector index, before
anything depends on it. Getting it wrong means a migration of every claim later, or a C2 query budget the
engine cannot meet.

## Method

- **Data.** `neptune_memory.store.bench.generator` (seeded, streaming, byte-identical per spec; a test pins
  its digest): 200 robots across six embodiments (60 AMR, 50 arm, 25 quadruped, 20 humanoid, 20 marine,
  25 aerial), 40 sites, 5 years. Per robot: high-rate observed state (pose zone, mode, health, energy),
  fault events, mission assignments, slow stated facts (site, firmware, one `mounts/<slot>` per slot);
  per mounted component: calibrations and wear. 5.7% of claims are superseded by a correction recorded
  1 h–60 days later with a new transform version. Every claim carries source id, transform id and
  `assertion_kind`. Smaller targets scale every rate, so the entity graph is fixed and history depth is
  the scaled axis (10^7 claims: 308k entities, 327k edges).
- **Ladder.** 10^5, 10^6, 10^7 claims measured; embeddings for every 10th claim (10^4, 10^5, 10^6; 128-d,
  clustered). 10^8 is extrapolated with a least-squares power-law fit `log y = a + b log n` over the three
  points (`bench/report.py`). The 10^8 / 10^7-embedding sizes do not fit this host in reasonable time.
- **Same semantics, same parameters.** Both engines load the same CSVs and run the same seeded
  parameters (half "known one week later", half "known now"). Each engine's as-of thread was checked
  against a brute-force scan with the reference predicate (`ClaimRecord.visible`): 0 mismatches in 100
  samples at every scale on both engines.
- **Workloads.** *Thread*: all claims about one robot visible at (valid_at, known_at). *Traverse*:
  entities within 3 undirected hops of a site over visible edges (~87 rows). *Directed*: the fixed
  pattern site ← robot → component → calibration (~38 rows), the shape AGE executes well. *Vector*:
  top-10 over all embeddings (HNSW), and top-10 restricted to subjects within 2 hops of a site as of a
  time. *Write*: 4 writers superseding claims (close old + append correction, one transaction) while 8
  readers run thread queries, 30 s. *Rebuild*: empty to queryable from CSV, all indexes included.
- **Query shapes.** Postgres runs the adapter's own SQL (`neptune_memory.store.postgres`). Neo4j runs the
  equivalent Cypher (`bench/neo4j_bench.py`) with a composite range index on `(subject, predicate,
  valid_from)` and is handed each robot's predicate list (Postgres derives it with a loose index scan),
  which favours Neo4j.
- **Host.** Intel i5-13600K (20 threads), Samsung 980 PRO NVMe, Linux 7.0, 30 GB RAM of which ~5 GB
  free, so engines ran one at a time with capped memory: Postgres 16.15 (conda-forge) + Apache AGE 1.6.0
  (PG16 release, built from source) + pgvector 0.8.1 (from source), `shared_buffers=2GB`; Neo4j 5.26.12
  community on OpenJDK 21, heap 1 GB (2 GB for import), page cache 2 GB. Client: Python 3.12, psycopg
  3.3 / neo4j driver 6.3, one connection per thread, latency measured client-side.

- **Reproduce.** Engines and data live outside the repo (`~/.cache/neptune-bench/`). Generate with
  `write_dataset`, embeddings with `bench/workload.py`, then `uv run --with "psycopg[binary]" --with numpy
  python bench/pg_bench.py <claims>` and `uv run --with neo4j --with numpy python bench/neo4j_bench.py
  <claims>` with one server running at a time; `bench/report.py` writes the CSV and the table below.

## Results

All numbers are latency in ms unless marked; **m** = measured, **x** = extrapolated to 10^8 claims from
the three measured points. Raw rows: [`../benchmarks/mvl-104-results.csv`](../benchmarks/mvl-104-results.csv).

| Workload | PG 1e5 m | PG 1e6 m | PG 1e7 m | PG 1e8 x | Neo4j 1e5 m | Neo4j 1e6 m | Neo4j 1e7 m | Neo4j 1e8 x |
|---|---|---|---|---|---|---|---|---|
| As-of thread p50 | 0.246 | 0.29 | 0.637 | 0.924 | 0.942 | 1.32 | 2.67 | 4.23 |
| As-of thread p99 | 0.424 | 0.651 | 2.69 | 5.73 | 1.5 | 2.57 | 4.21 | 7.12 |
| 3-hop undirected p50 (PG: SQL) | 0.263 | 0.334 | 1.07 | 1.86 | 2.49 | 1.9 | 6.76 | 8.63 |
| 3-hop undirected p99 (PG: SQL) | 0.446 | 0.769 | 4.06 | 10.2 | 4.27 | 4.26 | 25 | 45.1 |
| 3-hop directed p50 (PG: AGE Cypher) | 2 | 2.48 | 8.56 | 14.9 | 1.1 | 1.41 | 5.59 | 10.4 |
| Vector top-10 p50 | 0.303 | 0.793 | 1.89 | 4.79 | 1.69 | 1.84 | 4.79 | 6.96 |
| Vector top-10 recall@10 | 1 | 1 | 0.845 | — | 0.865 | 0.815 | 0.75 | — |
| Graph-filtered top-10 p50 | 2.1 | 2.37 | 7.83 | 12.7 | 2.21 | 8.12 | 70.7 | 347 |
| Graph-filtered recall@10 | 0.985 | 0.995 | 0.9 | — | 1 | 1 | 1 | — |
| Supersedes/s (4 writers) | 841 | 946 | 888 | — | 409 | 411 | 394 | — |
| Thread p99 under write load | 1.29 | 1.29 | 1.52 | — | 11.5 | 10.2 | 10.7 | — |
| Rebuild from CSV, s | 1.4 | 6.48 | 117 | 850 | 21.7 | 25.1 | 65.7 | 99.8 |
| On-disk size, GB | 0.0623 | 0.461 | 4.42 | 35.7 | 0.0931 | 0.618 | 5.76 | 42.8 |
| Largest server process RSS, GB | 0.494 | 0.393 | 0.49 | — | 2.93 | 3.45 | 5.02 | — |
| Backup, s (PG online pg_dump; Neo4j offline dump) | 0.16 | 0.889 | 8.62 | 57.8 | 1.55 | 1.85 | 8.6 | 16.2 |

Reading the table:

- C2's query, the as-of thread, is 3–4× faster on Postgres at every measured scale, and the fitted
  10^8 p50 (0.9 ms) is >300× under the budget. Undirected 3-hop traversal: 2–7× faster in SQL.
- Graph-filtered vector search is where the engines diverge: Neo4j 5.x can only post-filter its vector
  index, so the filtered query brute-forces the candidate set (71 ms at 10^6 embeddings, fitted 347 ms at
  10^7). pgvector 0.8's iterative HNSW scan stays at 8 ms with recall@10 0.9.
- Under 4 superseding writers, Postgres commits ~2.2× more supersessions/s and keeps thread p99 at
  1.3–1.5 ms; Neo4j's thread p99 rises to 10–12 ms.
- Neo4j wins two rows: the directed Cypher pattern (5.6 vs 8.6 ms via AGE at 10^7) and rebuild at 10^7
  (66 vs 117 s). 80 s of the Postgres rebuild is the HNSW build with pgvector compiled without
  `-march=native` (portable flags); the claim table and its indexes load in ~25 s. The AGE projection adds
  0.2 / 0.5 / 2.9 s at 10^5 / 10^6 / 10^7 and is not in the rebuild row.
- Postgres "on-disk" is `pg_database_size` (tables, indexes, AGE projection, vectors); Neo4j is the store
  directory, plus 0.27 GB of transaction logs. Postgres backends stay under 0.5 GB RSS each (shared buffers
  counted once); the Neo4j JVM is 2.9–5.0 GB.
- The 10^8 column is a three-point power-law fit and inherits its limits: the 10^7 point is the first
  where data exceeds the 2 GB cache, so the fit may understate cache-miss growth. The first-principles
  bound below covers the thread query regardless.

Not measured, and why:

- **10^8 claims and 10^7 embeddings** — ~5 GB free RAM and the session's time box; extrapolated instead.
  The thread query's cost is bounded independently of the fit: it reads one index path per predicate
  (~15 predicates × ~4 index pages + 1 heap page), so even fully uncached at 10^8 it is ~75 random
  NVMe reads (~10 ms), 30× under budget.
- **AGE undirected traversal** — not benchmarked as a series: AGE compiles undirected edges to `OR` join
  conditions over agtype that the planner cannot index; one 2-hop probe at 10^6 took 63 s. AGE is measured
  on its directed shape; the adapter traverses in SQL.
- **Neo4j online backup** — community edition has none; `neo4j-admin database dump` requires stopping
  the database (measured as an offline dump).
- **Concurrency above 4 writers / 8 readers, replication, failover** — out of scope for a single host.

## Decision

1. **The claim graph store is PostgreSQL 16 + Apache AGE + pgvector.** It is faster on the as-of thread,
   traversal and graph-filtered vector search at every measured scale, sustains ~2.2× Neo4j's superseding
   write rate with ~8× lower thread p99 under that load, is smaller on disk and in memory, and backs up
   online. Neo4j's wins (directed patterns, rebuild at 10^7) are on non-budgeted paths.
2. **Tables are the source of truth; everything else is derived and rebuildable.** `claim` and
   `claim_embedding` hold the data; the bi-temporal thread index, the edge indexes, the HNSW index and the
   AGE graph projection (`Entity` vertices, one `CLAIM` edge per entity-valued claim) are rebuilt by
   `PostgresStore.rebuild()`.
3. **Hot paths are SQL; AGE is for ad-hoc Cypher.** The as-of thread (loose index scan + newest-visible
   per predicate), traversal (recursive CTE over the claim table) and graph-filtered vector search (same
   walk feeding a pgvector query with `hnsw.iterative_scan = relaxed_order`) are SQL because they measured
   fastest. AGE stays in the stack for directed Cypher pattern queries, which it runs in milliseconds.
4. **Budgets for C2** (enforced by the next store or consolidation issue that adds a perf gate):
   as-of thread p50 < 300 ms and p99 < 1 s at 10^8 claims; 3-hop traversal p50 < 300 ms; graph-filtered
   vector top-10 p50 < 300 ms with recall@10 ≥ 0.9; superseding writes ≥ 200/s with thread p99 < 1 s
   under that load; full rebuild of 10^8 claims < 2 h.
5. **`MemoryStore` is the seam.** A typed Protocol (`write_claims`, `supersede`, `as_of_thread`,
   `neighbours`, `write_embeddings`, `vector_top_k`, `rebuild`); `PostgresStore` implements it over any
   DB-API connection (no driver dependency); `Neo4jStore` is an interface stub that raises
   `NotImplementedError` naming this ADR.

## Alternatives considered

- **Neo4j 5 community + native vector index.** Lost on the measurements above. It also needs a second
  database beside the Ledger catalog (no shared transactions), a 3+ GB JVM resident set at small scale,
  offline-only backup in community, and post-filter-only vector search in 5.x (the filtered query had to
  brute-force the candidate set).
- **Postgres with AGE as the primary query path.** AGE's directed patterns are fine (8.6 ms p50 at 10^7)
  but its undirected and variable-length matching is unusable with range filters, and agtype property
  access defeats the planner's statistics. Keeping the claim table as truth and AGE as a projection
  avoids betting the store on AGE's planner.
- **Postgres without AGE (plain relational + recursive CTEs).** Viable, and what the hot paths are.
  AGE is kept because ad-hoc Cypher over the same transactional data is cheap to offer (projection
  rebuild: ~3 s at 10^7) and the programme expects graph-pattern queries from Context and Deploy.

## Consequences

- One database for claims, graph projection, vectors and (later) the Ledger catalog; one backup
  (`pg_dump`/base backup, online), one transaction boundary for supersession.
- Postgres needs `shared_preload_libraries = 'age'` and the `vector` extension; AGE must be built for the
  server's major version (1.6.0 for PG16, 1.7.0 for PG17). Pin both in deployment docs when G2 deploys.
- The as-of thread's fast path relies on `one`-cardinality predicates (visible intervals of one subject
  and predicate never overlap, which ADR 0002's superseding guarantees). ADR 0002's `many` predicates can
  overlap and need a second path (a GiST index over the two intervals) before the adapter serves them.
- `ClaimRecord` is the store's row shape, benchmarked with integer claim ids and one `supersedes` id. ADR 0002
  (merged during this work) fixes the claim model: `claim:<sha256>` ids, a sorted `supersedes` tuple,
  `confidence`, typed literals and `LedgerTx` transaction times. Mapping `schema.Claim` onto these tables
  (text ids, a `supersedes` array) is the next store issue; text ids enlarge the primary key and thread
  index by roughly 2×, which the 10^8 latency margin absorbs.
- HNSW recall@10 was 0.845 unfiltered / 0.9 filtered at 10^6 embeddings with `ef_search = 100`, `m = 16`,
  `ef_construction = 64` (defaults); tuning against the recall budget, and building pgvector with native
  SIMD flags, are follow-ups once real embeddings exist.
- Revisit if: measured C2 latency exceeds a budget at production scale; AGE stops shipping releases for
  the Postgres major in use; or Memory needs multi-node graph sharding that Postgres cannot provide.

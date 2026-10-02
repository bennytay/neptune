# 0004 — Claim graph store: PostgreSQL 16 + pgvector over Neo4j 5 (Apache AGE optional, measured)

- Status: Accepted; final per 0007, which supersedes Decision 4 (budgets) and the recall consequence
- Date: 2026-10-02
- Issue: MVL-104

## Context

Memory persists a bi-temporal claim graph: every claim has a valid interval on a named clock and a
transaction interval `[recorded_at, superseded_at)`; entity-valued claims are graph edges; some claims
carry embeddings. The programme's working choice was PostgreSQL + Apache AGE + pgvector, so that claims,
graph and the Ledger catalog share one transactional database. The benchmark kept Postgres and pgvector
but demoted AGE: see Decision 3. G2 builds on the store, so the choice had
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
  the scaled axis (10^7 claims: 308k entities, 327k edges). Limits, kept because the generator's digest
  pins the measured data: corrections never narrow a valid interval and there are no ADR 0002 closure
  versions or corroborating claims (those are covered by the live tests, not the benchmark); and there are
  no dense hubs (about 5 robots per site, firmware is a value), so the traversal numbers are optimistic.
- **Ladder.** 10^5, 10^6, 10^7 claims measured; embeddings for every 10th claim (10^4, 10^5, 10^6; 128-d,
  clustered). 10^8 is extrapolated as an upper bound: the larger of a least-squares power-law fit
  `log y = a + b log n` over the three points and the 10^6 → 10^7 segment's slope alone (that segment is
  steeper wherever data outgrows the cache; `bench/report.py`). Throughput, resident memory and Neo4j's
  rebuild (fixed cost plus a linear term) are not extrapolated. 10^8 claims and 10^7 embeddings do not
  fit this host in reasonable time.
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
- **Query shapes.** Postgres runs the adapter's own SQL (`neptune_memory.store.postgres`): the thread is
  a containment query over one GiST index on `(subject, valid range, transaction range)` that returns
  every visible claim, and traversal is a breadth-first walk that expands each entity once. Postgres rows
  were re-measured after review with these shapes. Neo4j runs Cypher (`bench/neo4j_bench.py`) with a
  composite range index on `(subject, predicate, valid_from)`, is handed each robot's predicate list, and
  takes the newest visible claim per predicate. That shortcut is correct only on this generator's data
  (no corroboration), so Neo4j's thread rows are a lower bound for a correct Neo4j query.
- **Host.** Intel i5-13600K (20 threads), Samsung 980 PRO NVMe, Linux 7.0, 30 GB RAM of which ~5 GB
  free, so engines ran one at a time with capped memory: Postgres 16.15 (conda-forge) + Apache AGE 1.6.0
  (PG16 release, built from source) + pgvector 0.8.1 (from source), `shared_buffers=2GB`; Neo4j 5.26.12
  community on OpenJDK 21, heap 1 GB (2 GB for import), page cache 2 GB. A 1 GB heap is below Neo4j's
  sizing guidance for a 5.8 GB store with 12 sessions; garbage collection may explain part of its p99
  under load. Client: Python 3.12, psycopg
  3.3 / neo4j driver 6.3, one connection per thread, latency measured client-side.

- **Reproduce.** Engines and data live outside the repo (`~/.cache/neptune-bench/`). Generate with
  `write_dataset`, embeddings with `bench/workload.py`, then `uv run --with "psycopg[binary]" --with numpy
  python bench/pg_bench.py <claims>` and `uv run --with neo4j --with numpy python bench/neo4j_bench.py
  <claims>` with one server running at a time; `bench/report.py` writes the CSV and the table below.

## Results

All numbers are latency in ms unless marked; **m** = measured, **x (upper)** = extrapolated upper bound
at 10^8 claims (see Method). Raw rows: [`../benchmarks/mvl-104-results.csv`](../benchmarks/mvl-104-results.csv).

| Workload | PG 1e5 m | PG 1e6 m | PG 1e7 m | PG 1e8 x (upper) | Neo4j 1e5 m | Neo4j 1e6 m | Neo4j 1e7 m | Neo4j 1e8 x (upper) |
|---|---|---|---|---|---|---|---|---|
| As-of thread p50 | 0.157 | 0.625 | 0.761 | 2.04 | 0.942 | 1.32 | 2.67 | 5.43 |
| As-of thread p99 | 0.298 | 1.09 | 1.36 | 3.48 | 1.5 | 2.57 | 4.21 | 7.12 |
| 3-hop undirected p50 (PG: SQL) | 0.219 | 0.425 | 0.895 | 1.88 | 2.49 | 1.9 | 6.76 | 24 |
| 3-hop undirected p99 (PG: SQL) | 0.281 | 0.744 | 3.69 | 18.3 | 4.27 | 4.26 | 25 | 147 |
| 3-hop directed p50 (PG: AGE Cypher) | 2 | 2.48 | 8.56 | 29.5 | 1.1 | 1.41 | 5.59 | 22.2 |
| Vector top-10 p50 | 0.192 | 0.954 | 1.99 | 7.42 | 1.69 | 1.84 | 4.79 | 12.4 |
| Vector top-10 recall@10 | 1 | 1 | 0.845 | — | 0.865 | 0.815 | 0.75 | — |
| Graph-filtered top-10 p50 | 0.203 | 0.915 | 7.82 | 66.9 | 2.21 | 8.12 | 70.7 | 616 |
| Graph-filtered recall@10 | 1 | 1 | 0.89 | — | 1 | 1 | 1 | — |
| Supersedes/s (4 writers) | 832 | 989 | 1.06e+03 | — | 409 | 411 | 394 | — |
| Thread p99 under write load | 1.17 | 1.34 | 1.68 | — | 11.5 | 10.2 | 10.7 | — |
| Rebuild from CSV, s | 3.23 | 28.5 | 391 | 5.38e+03 | 21.7 | 25.1 | 65.7 | — |
| On-disk size, GB | 0.072 | 0.504 | 4.72 | 44.2 | 0.0931 | 0.618 | 5.76 | 53.6 |
| Largest server process RSS, GB | 0.839 | 0.839 | 0.812 | — | 2.93 | 3.45 | 5.02 | — |
| Backup, s (PG online pg_dump; Neo4j offline dump) | 0.154 | 0.848 | 9.06 | 96.8 | 1.55 | 1.85 | 8.6 | 40.1 |

Reading the table:

- C2's query, the as-of thread, is 2–6× faster on Postgres at every measured scale, and the 10^8 upper
  bound (p50 2.0 ms, p99 3.5 ms) is ~150× under the budget. Undirected 3-hop traversal: 2–11× faster in
  SQL.
- Graph-filtered vector search is where the engines diverge: Neo4j 5.x can only post-filter its vector
  index, so the filtered query brute-forces the candidate set (71 ms at 10^6 embeddings). pgvector 0.8's
  iterative HNSW scan takes 7.8 ms, with recall@10 0.89 at `ef_search = 100`, just under the 0.9 budget.
- Under 4 superseding writers, Postgres commits ~2.5× more supersessions/s and keeps thread p99 at
  1.2–1.7 ms; Neo4j's thread p99 rises to 10–12 ms.
- Neo4j wins two rows. The directed Cypher pattern takes 5.6 ms against 8.6 ms via AGE at 10^7. Rebuild
  at 10^7 takes 66 s against 391 s: the bi-temporal GiST index takes 296 s of the Postgres rebuild and the
  HNSW build 73 s. pgvector was compiled with portable flags (no `-march=native`). The optional AGE
  snapshot adds 0.2 / 0.5 / 2.9 s at 10^5 / 10^6 / 10^7 and is not in the rebuild row.
- Postgres "on-disk" is `pg_database_size` without the AGE snapshot. Neo4j's figure is the store
  directory, plus 0.27 GB of transaction logs. The RSS row is not like-for-like: it compares the largest
  single Postgres backend, which excludes most of the 2 GB of shared buffers, with the whole JVM. It shows
  only that Neo4j needs a large heap and page cache resident in one process.

Not measured, and why:

- **10^8 claims and 10^7 embeddings** — ~5 GB free RAM and the session's time box; extrapolated instead.
  The rebuild row is the weakest extrapolation. An HNSW build over 10^7 vectors no longer fits
  `maintenance_work_mem = 1GB` and slows down sharply, and the GiST build grows faster than linearly.
  The 1.5 h upper bound is therefore not evidence that the "< 2 h" budget holds.
- **AGE undirected traversal** — not benchmarked as a series: AGE compiles undirected edges to `OR` join
  conditions over agtype that the planner cannot index; one 2-hop probe at 10^6 took 63 s. AGE is measured
  on its directed shape (before review; AGE was not re-run); the adapter traverses in SQL.
- **Neo4j with a correct thread query or a larger heap** — not re-run after review; both caveats above
  favour Neo4j or are neutral, so they cannot change the decision.
- **Dense hubs** — the generator has none; the live tests check that the walk expands each entity once
  through a 160-entity hub with cycles, but hub latency is not measured.
- **Neo4j online backup** — community edition has none; `neo4j-admin database dump` requires stopping
  the database (measured as an offline dump).
- **Concurrency above 4 writers / 8 readers, replication, failover** — out of scope for a single host.

## Decision

1. **The claim graph store is PostgreSQL 16 + pgvector, queried with SQL; Neo4j is not adopted.**
   Postgres is faster on the as-of thread, traversal and graph-filtered vector search at every measured
   scale. It sustains ~2.5× Neo4j's superseding write rate with ~7× lower thread p99 under that load, uses
   less disk, and backs up online. Neo4j's two wins, directed patterns and rebuild time, are on paths
   with no latency budget.
2. **Tables are the store of record; everything else is derived and rebuildable.** `claim` and
   `claim_embedding` hold the data. `PostgresStore.rebuild()` recreates the bi-temporal GiST index, the
   edge indexes and the HNSW index.
3. **Apache AGE is optional and not part of the store's contract.** `PostgresStore(graph=...)` can build
   an AGE snapshot (`Entity` vertices, one `CLAIM` edge per entity-valued claim). It defaults to off.
   Only `rebuild()` refreshes the snapshot: it is not transactional with writes, it is stale from the
   first write after a rebuild, and `MemoryStore` exposes no Cypher. Turning it on costs
   `shared_preload_libraries = 'age'` and a source build for each Postgres major version. The AGE
   measurements are kept as evidence for revisiting this if Context or Deploy need graph-pattern queries.
4. **Budgets for C2** (enforced by the next store or consolidation issue that adds a perf gate):
   as-of thread p50 < 300 ms and p99 < 1 s at 10^8 claims; 3-hop traversal p50 < 300 ms; graph-filtered
   vector top-10 p50 < 300 ms with recall@10 ≥ 0.9 (0.89 measured: tune before the gate); superseding
   writes ≥ 200/s with thread p99 < 1 s under that load; full rebuild of 10^8 claims < 2 h (unproven: see
   Not measured).
5. **`MemoryStore` is the seam, and it is provisional.** It is a typed Protocol (`write_claims`,
   `supersede`, `as_of_thread`, `neighbours`, `write_embeddings`, `vector_top_k`, `rebuild`).
   `PostgresStore` implements it over any non-autocommit DB-API connection, with no driver dependency.
   Every call runs in its own transaction and refuses a connection the caller left mid-transaction.
   `Neo4jStore` is an interface stub that raises `NotImplementedError` naming this ADR. The interface
   changes in G2 (MVL-105): ids become ADR 0002 `ClaimId`s (`claim:<sha256>`), and `supersede(old, new)`
   is replaced by an atomic write of one ADR 0002/0005 resolution (several closures plus new claims at one
   transaction time).

## Alternatives considered

- **Neo4j 5 community + native vector index.** Lost on the measurements above. It also needs a second
  database beside the Ledger catalog (no shared transactions), offline-only backup in community, and
  post-filter-only vector search in 5.x (the filtered query had to brute-force the candidate set).
- **Postgres with AGE as the query path or the store of record.** AGE's directed patterns are fine
  (8.6 ms p50 at 10^7), but its undirected and variable-length matching is unusable with range filters,
  agtype property access defeats the planner's statistics, and keeping it transactional with every write
  would double the write path. It is kept only as the optional snapshot of Decision 3.

## Consequences

- One database for claims, vectors and (later) the Ledger catalog; one backup (`pg_dump`/base backup,
  online), one transaction boundary for supersession.
- Postgres needs the `vector` and `btree_gist` extensions. AGE is needed only for the optional snapshot,
  and must then be preloaded and built for the server's major version (1.6.0 for PG16, 1.7.0 for PG17).
- The as-of thread assumes nothing about overlap. Corroborating claims (ADR 0002 §4.1: same object,
  different evidence, current together) and `many`-cardinality predicates all come back, at the cost of
  a GiST index that dominates the rebuild.
- `ClaimRecord` is the store's row shape, benchmarked with integer claim ids and one `supersedes` id. ADR 0002
  (merged during this work) fixes the claim model: `claim:<sha256>` ids, a sorted `supersedes` tuple,
  `confidence`, typed literals and `LedgerTx` transaction times. Mapping `schema.Claim` onto these tables
  (text ids, a `supersedes` array) is the next store issue; text ids enlarge the primary key and thread
  index by roughly 2×, which the 10^8 latency margin absorbs.
- HNSW recall@10 was 0.845 unfiltered and 0.89 filtered at 10^6 embeddings. That was with
  `ef_search = 100`, which `PostgresStore` sets per query (`ef_search` constructor parameter;
  pgvector's own default is 40), and the default `m = 16`, `ef_construction = 64`. Tuning against the
  recall budget, and building pgvector with native SIMD flags, are follow-ups once real embeddings exist.
- Revisit if: measured C2 latency or rebuild time exceeds a budget at production scale; Context or Deploy
  need transactional graph-pattern queries (then AGE's role, or a graph engine, is reopened with the
  measurements here); or Memory needs multi-node graph sharding that Postgres cannot provide.

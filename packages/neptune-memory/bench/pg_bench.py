"""PostgreSQL 16 + Apache AGE + pgvector benchmark (MVL-104). Benchmark-only, not packaged.

    uv run --with "psycopg[binary]" --with numpy python bench/pg_bench.py <claims> [phase ...]

Phases: rebuild, thread, traverse, age, vector, write, footprint, backup. The SQL under test is the
adapter's own (``neptune_memory.store.postgres``), so what is measured is what ships.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import psycopg

sys.path.insert(0, str(Path(__file__).parent))
import workload as w

from neptune_memory.store import postgres as pg

DSN = "host=127.0.0.1 port=55432 user=bench dbname=bench"
S = "memory"
GRAPH = "claimgraph"
PGBIN = w.ROOT / "env" / "bin"


def conn() -> psycopg.Connection[Any]:
    return psycopg.connect(DSN, autocommit=False)


def rebuild(n: int) -> None:
    p = w.paths(n)
    steps: dict[str, float] = {}
    with conn() as c:
        c.autocommit = True
        c.execute(f"DROP SCHEMA IF EXISTS {S} CASCADE")
        c.execute("SET maintenance_work_mem = '1GB'")
        c.execute("SET max_parallel_maintenance_workers = 7")
        t0 = time.perf_counter()
        for stmt in pg.ddl(S, w.DIM):
            c.execute(stmt)
        cols = ", ".join(pg.CLAIM_COLUMNS)
        ms, _ = w.timed(
            lambda: c.execute(f"COPY {S}.claim ({cols}) FROM '{p['claims']}' CSV HEADER")
        )
        steps["copy_claims_ms"] = ms
        ms, _ = w.timed(lambda: c.execute(f"COPY {S}.claim_embedding FROM '{p['emb_pg']}' CSV"))
        steps["copy_embeddings_ms"] = ms
        for stmt in pg.index_ddl(S):
            ms, _ = w.timed(lambda stmt=stmt: c.execute(stmt))
            key = (
                stmt.split(" ON ")[0].split()[-1] if "INDEX" in stmt else stmt.replace(f"{S}.", "")
            )
            steps[f"{key}_ms"] = ms
        c.execute(f"VACUUM (ANALYZE) {S}.claim")
        steps["total_ms"] = (time.perf_counter() - t0) * 1000.0
    w.save("pg", n, "rebuild", {k: round(v, 1) for k, v in steps.items()})


def thread(n: int, reps: int = 300) -> None:
    robots = w.entities(n, "robot")
    rng = random.Random(1)
    samples = w.as_of_samples(reps + 30, seed=2)
    sql = pg.as_of_thread_sql(S)
    ms_all, rows = [], []
    with conn() as c:
        c.autocommit = True
        for i, at in enumerate(samples):
            params = {"subject": rng.choice(robots), **at}
            ms, out = w.timed(lambda params=params: c.execute(sql, params, prepare=True).fetchall())
            if i >= 30:
                ms_all.append(ms)
                rows.append(len(out))
    w.save("pg", n, "thread", {**w.summary(ms_all), "rows_mean": sum(rows) / len(rows)})


def traverse(n: int, reps: int = 200, hops: int = 3) -> None:
    sites = w.entities(n, "site")
    rng = random.Random(3)
    samples = w.as_of_samples(reps + 20, seed=4)
    sql = pg.neighbours_sql(S)
    ms_all, rows = [], []
    with conn() as c:
        c.autocommit = True
        for i, at in enumerate(samples):
            params = {"start": rng.choice(sites), "hops": hops, **at}
            ms, out = w.timed(lambda params=params: c.execute(sql, params, prepare=True).fetchall())
            if i >= 20:
                ms_all.append(ms)
                rows.append(len(out))
    w.save("pg", n, "traverse", {**w.summary(ms_all), "rows_mean": sum(rows) / len(rows)})


# --- Apache AGE projection: same edges, queried through Cypher ----------------------------------

AGE_SETUP = [
    "LOAD 'age'",
    "SET search_path = ag_catalog, public",
]


def age_load(n: int) -> None:
    """The adapter's own AGE projection, timed: load (vertices + edges) and its indexes."""
    stmts = pg.age_projection_sql(S, GRAPH)
    split = next(i for i, x in enumerate(stmts) if x.startswith("CREATE INDEX"))
    with conn() as c:
        c.autocommit = True
        load_ms, _ = w.timed(lambda: [c.execute(x) for x in stmts[:split]])
        index_ms, _ = w.timed(lambda: [c.execute(x) for x in stmts[split:]])
    w.save("age", n, "rebuild", {"load_ms": round(load_ms, 1), "index_ms": round(index_ms, 1)})


def _age_vis(r: str, at: dict[str, Any]) -> str:
    return (
        f"{r}.valid_clock = '{at['clock']}' AND {r}.valid_from <= {at['valid_at']} "
        f"AND ({r}.valid_to IS NULL OR {r}.valid_to > {at['valid_at']}) "
        f"AND {r}.recorded_at <= {at['known_at']} "
        f"AND ({r}.superseded_at IS NULL OR {r}.superseded_at > {at['known_at']})"
    )


def age_cypher(start: str, at: dict[str, Any]) -> str:
    """Directed 3-hop pattern site <- robot -> component -> calibration (and robot -> episode etc.).

    Undirected AGE patterns compile to OR join conditions the planner cannot index; a 2-hop
    undirected probe took 63 s at 10**6 claims (ADR 0004), so AGE is measured on its best shape.
    """
    return (
        f"MATCH (s:Entity {{eid: '{start}'}})<-[r0:CLAIM]-(a:Entity)-[r1:CLAIM]->(b:Entity)"
        f"-[r2:CLAIM]->(x:Entity) WHERE {_age_vis('r0', at)} AND {_age_vis('r1', at)} "
        f"AND {_age_vis('r2', at)} RETURN DISTINCT x.eid"
    )


def age(n: int, reps: int = 200) -> None:
    sites = w.entities(n, "site")
    rng = random.Random(3)
    samples = w.as_of_samples(reps + 10, seed=4)
    ms_all, rows = [], []
    with conn() as c:
        c.autocommit = True
        for s in AGE_SETUP:
            c.execute(s)
        c.execute("SET statement_timeout = '60s'")
        for i, at in enumerate(samples):
            cypher = age_cypher(rng.choice(sites), at)
            q = f"SELECT * FROM cypher('{GRAPH}', $$ {cypher} $$) AS (eid agtype)"
            try:
                ms, out = w.timed(lambda q=q: c.execute(q).fetchall())
            except psycopg.errors.QueryCanceled:
                ms, out = 60_000.0, []
            if i >= 10:
                ms_all.append(ms)
                rows.append(len(out))
    w.save("age", n, "traverse", {**w.summary(ms_all), "rows_mean": sum(rows) / len(rows)})


def vector(n: int, reps: int = 100, k: int = 10) -> None:
    queries = np.load(w.paths(n)["queries"])
    sites = w.entities(n, "site")
    rng = random.Random(5)
    samples = w.as_of_samples(reps + 10, seed=6)
    plain = pg.vector_top_k_sql(S, filtered=False)
    filt = pg.vector_top_k_sql(S, filtered=True)
    res: dict[str, Any] = {}
    with conn() as c:
        c.autocommit = True
        c.execute("SET hnsw.iterative_scan = relaxed_order")
        c.execute("SET hnsw.ef_search = 100")
        ms_plain, ms_filt, recall_plain, recall_filt, scope_rows = [], [], [], [], []
        for i in range(reps + 10):
            q = pg.vector_literal(queries[i % len(queries)].tolist())
            at = samples[i]
            fp = {"query": q, "k": k, "start": rng.choice(sites), "hops": 2, **at}
            ms1, got1 = w.timed(lambda q=q: c.execute(plain, {"query": q, "k": k}).fetchall())
            ms2, got2 = w.timed(lambda fp=fp: c.execute(filt, fp).fetchall())
            if i < 10:
                continue
            ms_plain.append(ms1)
            ms_filt.append(ms2)
            if i % 5 == 0:  # exact ground truth with the vector index disabled
                c.execute("SET enable_indexscan = off")
                c.execute("SET enable_indexonlyscan = off")
                exact1 = c.execute(plain, {"query": q, "k": k}).fetchall()
                exact2 = c.execute(filt, fp).fetchall()
                c.execute("RESET enable_indexscan")
                c.execute("RESET enable_indexonlyscan")
                recall_plain.append(
                    len({r[0] for r in got1} & {r[0] for r in exact1}) / max(1, len(exact1))
                )
                recall_filt.append(
                    len({r[0] for r in got2} & {r[0] for r in exact2}) / max(1, len(exact2))
                )
                scope_rows.append(len(exact2))
        res["unfiltered"] = {
            **w.summary(ms_plain),
            "recall_at_10": round(sum(recall_plain) / len(recall_plain), 4),
        }
        res["graph_filtered"] = {
            **w.summary(ms_filt),
            "recall_at_10": round(sum(recall_filt) / len(recall_filt), 4),
            "exact_rows_mean": sum(scope_rows) / len(scope_rows),
        }
        res["embeddings"] = c.execute(f"SELECT count(*) FROM {S}.claim_embedding").fetchone()[0]
    w.save("pg", n, "vector", res)


def write(n: int, seconds: float = 30.0, writers: int = 4, readers: int = 8) -> None:
    """Superseding writes (close old + append correction, one transaction) under thread reads."""
    robots = w.entities(n, "robot")
    with conn() as c:
        top = c.execute(f"SELECT max(claim_id), max(recorded_at) FROM {S}.claim").fetchone()
        candidates = [
            r[0]
            for r in c.execute(
                f"SELECT claim_id FROM {S}.claim WHERE superseded_at IS NULL "
                "AND object_value IS NOT NULL AND claim_id % 7 = 0 LIMIT 200000"
            ).fetchall()
        ]
    next_id = [int(top[0]) + 1_000_000]
    now = int(top[1]) + w.DAY
    lock = threading.Lock()
    stop = threading.Event()
    writes: list[float] = []
    reads: list[float] = []
    random.Random(7).shuffle(candidates)
    pool = iter(candidates)

    def writer() -> None:
        with conn() as c:
            while not stop.is_set():
                with lock:
                    old = next(pool)
                    new_id = next_id[0]
                    next_id[0] += 1
                t0 = time.perf_counter()
                with c.transaction():
                    cur = c.execute(
                        f"UPDATE {S}.claim SET superseded_at = %(at)s WHERE claim_id = %(old)s "
                        "AND superseded_at IS NULL RETURNING " + ", ".join(pg.CLAIM_COLUMNS),
                        {"old": old, "at": now},
                    )
                    row = dict(zip(pg.CLAIM_COLUMNS, cur.fetchone(), strict=True))
                    row |= {
                        "claim_id": new_id,
                        "recorded_at": now,
                        "superseded_at": None,
                        "object_value": f"{row['object_value']}~bench",
                        "transform_id": "memory.consolidate.bench@2.0.0",
                        "supersedes": old,
                    }
                    c.execute(pg.insert_claim_sql(S), row)
                with lock:
                    writes.append((time.perf_counter() - t0) * 1000.0)

    def reader(seed: int) -> None:
        rng = random.Random(seed)
        samples = w.as_of_samples(100_000, seed=seed)
        sql = pg.as_of_thread_sql(S)
        with conn() as c:
            c.autocommit = True
            i = 0
            while not stop.is_set():
                ms, _ = w.timed(
                    lambda i=i: c.execute(
                        sql, {"subject": rng.choice(robots), **samples[i]}, prepare=True
                    ).fetchall()
                )
                i += 1
                with lock:
                    reads.append(ms)

    threads = [threading.Thread(target=writer) for _ in range(writers)]
    threads += [threading.Thread(target=reader, args=(100 + i,)) for i in range(readers)]
    for t in threads:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in threads:
        t.join()
    w.save(
        "pg",
        n,
        "write",
        {
            "writers": writers,
            "readers": readers,
            "seconds": seconds,
            "supersedes_per_s": round(len(writes) / seconds, 1),
            "write_latency": w.summary(writes),
            "thread_under_load": w.summary(reads),
        },
    )


def footprint(n: int) -> None:
    with conn() as c:
        rows = c.execute(
            "SELECT n.nspname || '.' || c.relname, pg_total_relation_size(c.oid) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname IN (%s, %s) "
            "AND c.relkind IN ('r') ORDER BY 2 DESC",
            (S, GRAPH),
        ).fetchall()
        db = c.execute("SELECT pg_database_size('bench')").fetchone()[0]
        idx = c.execute(
            "SELECT c.relname, pg_relation_size(c.oid) FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind = 'i'",
            (S,),
        ).fetchall()
    rss = subprocess.run(
        ["ps", "-C", "postgres", "-o", "rss="], capture_output=True, text=True, check=False
    ).stdout.split()
    w.save(
        "pg",
        n,
        "footprint",
        {
            "database_bytes": db,
            "tables_total_bytes": dict(rows),
            "index_bytes": dict(idx),
            "server_rss_sum_kb_note": "sum over backends double-counts shared_buffers",
            "server_rss_max_kb": max(int(x) for x in rss) if rss else None,
        },
    )


def backup(n: int) -> None:
    out = w.ROOT / f"pg_{n}.dump"
    env = {**os.environ, "PATH": f"{PGBIN}:{os.environ['PATH']}"}
    ms, _ = w.timed(
        lambda: subprocess.run(
            [
                "pg_dump",
                "-h",
                "127.0.0.1",
                "-p",
                "55432",
                "-U",
                "bench",
                "-Fd",
                "-j",
                "4",
                "-Z",
                "1",
                "-n",
                S,
                "-f",
                str(out),
                "bench",
            ],
            check=True,
            env=env,
        )
    )
    size = sum(f.stat().st_size for f in out.iterdir())
    subprocess.run(["rm", "-rf", str(out)], check=True)
    w.save(
        "pg", n, "backup", {"pg_dump_dir_j4_ms": round(ms, 1), "dump_bytes": size, "online": True}
    )


def verify(n: int, reps: int = 100) -> None:
    """The indexed as-of thread must equal a brute-force scan with the reference predicate."""
    robots = w.entities(n, "robot")
    rng = random.Random(11)
    brute = f"SELECT claim_id FROM {S}.claim c WHERE c.subject = %(subject)s AND " + pg._visible(
        "c"
    )
    mismatches = 0
    with conn() as c:
        for at in w.as_of_samples(reps, seed=12):
            params = {"subject": rng.choice(robots), **at}
            fast = {r[0] for r in c.execute(pg.as_of_thread_sql(S), params).fetchall()}
            slow = {r[0] for r in c.execute(brute, params).fetchall()}
            mismatches += fast != slow
    w.save("pg", n, "verify", {"samples": reps, "mismatches": mismatches})


PHASES = {
    "verify": verify,
    "rebuild": rebuild,
    "thread": thread,
    "traverse": traverse,
    "age_load": age_load,
    "age": age,
    "vector": vector,
    "write": write,
    "footprint": footprint,
    "backup": backup,
}

if __name__ == "__main__":
    scale = int(sys.argv[1])
    for phase in sys.argv[2:] or list(PHASES):
        PHASES[phase](scale)

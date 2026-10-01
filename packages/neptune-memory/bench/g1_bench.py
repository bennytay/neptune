"""G1 gate re-measurements (MVL-106): recall, cold as-of, traversal and hubs. Benchmark-only.

    uv run --with "psycopg[binary]" --with numpy python bench/g1_bench.py <claims> <phase> ...

Runs against the MVL-104 Postgres (``bench/pg_bench.py``, same DSN and data) after
``pg_bench.py <claims> rebuild``. Phases:

- ``recall``: graph-filtered and unfiltered top-10 over every one of the 200 seeded queries, for a
  grid of HNSW settings, against exact search (indexes off). Settles ADR 0004's 0.89 (20 queries).
- ``cold``: the as-of thread with the OS page cache evicted (``posix_fadvise(DONTNEED)`` on every
  file of the cluster) before every query. Run it against a server started with a small
  ``shared_buffers`` (``-c shared_buffers=16MB``) so Postgres's own cache cannot hide the reads;
  ``track_io_timing`` proves they reached the device.
- ``walk``: the 3-hop walk with the shipped SQL (warm), and a hub of 10^4 and 10^5 entities walked
  with the shipped SQL and with the pre-gate ``text[]`` visited set.

Results land in ``~/.cache/neptune-bench/results/g1_<claims>_<phase>.json``; ``bench/report.py``
does not read them, the G1 review quotes them and ``docs/benchmarks/g1-results.json`` keeps them.
"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import psycopg

sys.path.insert(0, str(Path(__file__).parent))
import pg_bench as pb
import workload as w

from neptune_memory.store import postgres as pg

S = pb.S
PGDATA = w.ROOT / "pgdata"


# --- recall --------------------------------------------------------------------------------------

#: (ef_search, iterative scan mode, max_scan_tuples): the shipped default first.
GRID: list[tuple[int, str, int]] = [
    (100, "relaxed_order", 20_000),
    (40, "relaxed_order", 20_000),
    (200, "relaxed_order", 20_000),
    (400, "relaxed_order", 20_000),
    (100, "strict_order", 20_000),
    (100, "relaxed_order", 100_000),
    (200, "relaxed_order", 100_000),
]


def _ids(rows: list[tuple[Any, ...]]) -> set[int]:
    return {int(r[0]) for r in rows}


def recall(n: int, k: int = 10) -> None:
    queries = np.load(w.paths(n)["queries"])
    sites = w.entities(n, "site")
    rng = random.Random(5)
    samples = w.as_of_samples(len(queries), seed=6)
    params = [
        {
            "query": pg.vector_literal(q.tolist()),
            "k": k,
            "start": rng.choice(sites),
            "hops": 2,
            **samples[i],
        }
        for i, q in enumerate(queries)
    ]
    plain = pg.vector_top_k_sql(S, filtered=False)
    filt = pg.vector_top_k_sql(S, filtered=True)
    out: dict[str, Any] = {"queries": len(params), "k": k}
    with pb.conn() as c:
        c.autocommit = True
        c.execute("SET enable_indexscan = off")
        c.execute("SET enable_indexonlyscan = off")
        exact_plain = [_ids(c.execute(plain, p).fetchall()) for p in params]
        exact_filt = [_ids(c.execute(filt, p).fetchall()) for p in params]
        c.execute("RESET enable_indexscan")
        c.execute("RESET enable_indexonlyscan")
        out["exact_filtered_rows_mean"] = sum(len(e) for e in exact_filt) / len(exact_filt)
        for ef, mode, max_tuples in GRID:
            c.execute(f"SET hnsw.ef_search = {ef}")
            c.execute(f"SET hnsw.iterative_scan = {mode}")
            c.execute(f"SET hnsw.max_scan_tuples = {max_tuples}")
            row: dict[str, Any] = {}
            for name, sql, exact in (
                ("unfiltered", plain, exact_plain),
                ("graph_filtered", filt, exact_filt),
            ):
                ms_all, hits = [], []
                for p, want in zip(params, exact, strict=True):
                    ms, got = w.timed(lambda p=p, sql=sql: c.execute(sql, p).fetchall())
                    ms_all.append(ms)
                    hits.append(len(_ids(got) & want) / max(1, len(want)))
                row[name] = {
                    **w.summary(ms_all),
                    "recall_at_10": round(sum(hits) / len(hits), 4),
                    "recall_min": round(min(hits), 2),
                    "queries_below_0_9": sum(h < 0.9 for h in hits),
                }
            out[f"ef{ef}_{mode}_max{max_tuples}"] = row
    w.save("g1", n, "recall", out)


# --- cold as-of ----------------------------------------------------------------------------------


def evict() -> tuple[int, float]:
    """Drop every file of the cluster from the OS page cache; returns (files, ms)."""
    t0 = time.perf_counter()
    files = 0
    for path in PGDATA.rglob("*"):
        if path.is_file():
            try:
                fd = os.open(path, os.O_RDONLY)
            except OSError:
                continue
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                files += 1
            finally:
                os.close(fd)
    return files, (time.perf_counter() - t0) * 1000.0


def cold(n: int, reps: int = 200) -> None:
    robots = w.entities(n, "robot")
    rng = random.Random(21)
    samples = w.as_of_samples(reps, seed=22)
    sql = pg.as_of_thread_sql(S)
    ms_all, rows, io_ms, reads = [], [], [], []
    with pb.conn() as c:
        c.autocommit = True
        buffers = c.execute("SHOW shared_buffers").fetchone()[0]
        c.execute("SET track_io_timing = on")
        for at in samples:
            params = {"subject": rng.choice(robots), **at}
            evict()
            ms, out = w.timed(lambda params=params: c.execute(sql, params).fetchall())
            ms_all.append(ms)
            rows.append(len(out))
            evict()  # the same query again, cold, to count its reads and their device time
            plan = c.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params).fetchone()
            top = plan[0][0]["Plan"]
            reads.append(int(top.get("Shared Read Blocks", 0)))
            io_ms.append(float(top.get("I/O Read Time", 0.0)))
    w.save(
        "g1",
        n,
        "cold",
        {
            **w.summary(ms_all),
            "rows_mean": sum(rows) / len(rows),
            "shared_buffers": buffers,
            "eviction": "posix_fadvise(DONTNEED) on every cluster file before every query",
            "blocks_read_mean": sum(reads) / len(reads),
            "io_read_ms_mean": round(sum(io_ms) / len(io_ms), 3),
        },
    )


# --- walk ----------------------------------------------------------------------------------------

#: The pre-gate walk: a ``text[]`` visited set checked with ``<> ALL`` on every edge.
OLD_WALK = (
    pg.neighbours_sql(S)
    .replace(
        "jsonb_build_object(%(start)s::text, 0)",
        "ARRAY[%(start)s::text]",
    )
    .replace("b.seen || n.added", "b.seen || n.ents")
    .replace(",\n      jsonb_object_agg(x.other, b.depth + 1) AS added", "")
    .replace("NOT jsonb_exists(b.seen, e.other)", "e.other <> ALL (b.seen)")
)
HUB = "g1hub"
TIMEOUT_S = 300


def _hub(c: psycopg.Connection[Any], size: int) -> None:
    """A hub site with ``size`` machines, each mounting one component calibrated by one shared
    rig located at the site: cycles, and a 2-hop frontier of 2 * size entities."""
    for stmt in pg.ddl(HUB, w.DIM):
        c.execute(stmt)
    c.execute(f"TRUNCATE {HUB}.claim")
    c.execute(
        f"""INSERT INTO {HUB}.claim
SELECT row_number() OVER (), s, p, o, NULL, 'fleet_utc', 0, NULL, 0, NULL, 'stated', 'ev:hub', 't@1'
FROM (
  SELECT 'robot:hub-' || i AS s, 'located_at' AS p, 'site:hub' AS o FROM generate_series(1, %(n)s) i
  UNION ALL SELECT 'robot:hub-' || i, 'mounts/s0', 'component:hub-' || i
    FROM generate_series(1, %(n)s) i
  UNION ALL SELECT 'component:hub-' || i, 'calibrated_by', 'calibration:rig'
    FROM generate_series(1, %(n)s) i
  UNION ALL SELECT 'calibration:rig', 'located_at', 'site:hub'
) x""",
        {"n": size},
    )
    for stmt in pg.index_ddl(HUB):
        if "hnsw" not in stmt:
            c.execute(stmt)


def walk(n: int, reps: int = 200) -> None:
    sites = w.entities(n, "site")
    rng = random.Random(3)
    samples = w.as_of_samples(reps + 20, seed=4)
    out: dict[str, Any] = {}
    with pb.conn() as c:
        c.autocommit = True
        ms_all, rows = [], []
        for i, at in enumerate(samples):
            params = {"start": rng.choice(sites), "hops": 3, **at}
            ms, got = w.timed(
                lambda p=params: c.execute(pg.neighbours_sql(S), p, prepare=True).fetchall()
            )
            if i >= 20:
                ms_all.append(ms)
                rows.append(len(got))
        out["traverse_3hop"] = {**w.summary(ms_all), "rows_mean": sum(rows) / len(rows)}
        at = {"clock": "fleet_utc", "valid_at": 1, "known_at": 1}
        for size in (10_000, 100_000):
            _hub(c, size)
            for name, text in (("jsonb_set", pg.neighbours_sql(HUB)), ("text_array", OLD_WALK)):
                sql = text.replace(f"{S}.claim", f"{HUB}.claim")
                runs = 3 if name == "jsonb_set" or size <= 10_000 else 1
                c.execute(f"SET statement_timeout = '{TIMEOUT_S}s'")
                timings, found, timed_out = [], 0, False
                for _ in range(runs):
                    try:
                        ms, got = w.timed(
                            lambda sql=sql: c.execute(
                                sql, {"start": "site:hub", "hops": 3, **at}
                            ).fetchall()
                        )
                    except psycopg.errors.QueryCanceled:
                        timed_out = True
                        break
                    timings.append(ms)
                    found = len(got)
                c.execute("RESET statement_timeout")
                out[f"hub_{size}_{name}"] = (
                    {"ms_min": f"> {TIMEOUT_S * 1000}", "rows": None, "timed_out": True}
                    if timed_out
                    else {"ms_min": round(min(timings), 1), "rows": found}
                )
        c.execute(f"DROP SCHEMA {HUB} CASCADE")
    w.save("g1", n, "walk", out)


PHASES = {"recall": recall, "cold": cold, "walk": walk}

if __name__ == "__main__":
    scale = int(sys.argv[1])
    for phase in sys.argv[2:]:
        PHASES[phase](scale)

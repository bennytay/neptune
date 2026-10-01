"""Neo4j 5 community benchmark (MVL-104). Benchmark-only; not part of the package.

    uv run --with neo4j --with numpy python bench/neo4j_bench.py <claims> [phase ...]

Phases: rebuild, verify, thread, traverse, directed, vector, write, footprint, backup. Same data,
same seeded parameters and the same semantics as ``pg_bench.py``; every claim is a ``:Claim``
node and every entity-valued claim is also a ``:CLAIM`` relationship between ``:Entity`` nodes.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).parent))
import workload as w

from neptune_memory.store.bench.generator import EMBODIMENTS, ROBOT_PREDICATES

HOME = w.ROOT / "neo4j-community-5.26.12"
URI = "bolt://127.0.0.1:57687"
ENV = {**os.environ, "JAVA_HOME": str(w.ROOT / "env"), "HEAP_SIZE": "2g"}
IMPORT = w.ROOT / "neo4j-import"

VIS = (
    "{r}.valid_clock = $clock AND {r}.valid_from <= $valid_at "
    "AND ({r}.valid_to IS NULL OR {r}.valid_to > $valid_at) AND {r}.recorded_at <= $known_at "
    "AND ({r}.superseded_at IS NULL OR {r}.superseded_at > $known_at)"
)
THREAD = """
UNWIND $preds AS p
CALL {
  WITH p
  MATCH (c:Claim)
  WHERE c.subject = $subject AND c.predicate = p AND c.valid_from <= $valid_at
    AND c.valid_clock = $clock AND c.recorded_at <= $known_at
    AND (c.superseded_at IS NULL OR c.superseded_at > $known_at)
  RETURN c ORDER BY c.subject DESC, c.predicate DESC, c.valid_from DESC LIMIT 1
}
WITH c WHERE c.valid_to IS NULL OR c.valid_to > $valid_at
RETURN c.claim_id AS claim_id ORDER BY c.predicate
"""
TRAVERSE = (
    "MATCH (s:Entity {eid: $start})-[rs:CLAIM*1..3]-(x:Entity) "
    "WHERE all(r IN rs WHERE " + VIS.format(r="r") + ") AND x <> s "
    "RETURN x.eid AS eid, min(size(rs)) AS depth"
)
DIRECTED = (
    "MATCH (s:Entity {eid: $start})<-[r0:CLAIM]-(a:Entity)-[r1:CLAIM]->(b:Entity)"
    "-[r2:CLAIM]->(x:Entity) "
    "WHERE " + " AND ".join(VIS.format(r=r) for r in ("r0", "r1", "r2")) + " RETURN DISTINCT x.eid"
)
VEC_PLAIN = (
    "CALL db.index.vector.queryNodes('emb_vec', $k, $q) YIELD node, score "
    "RETURN node.claim_id AS claim_id, score"
)
VEC_EXACT = (
    "MATCH (e:Emb) WITH e, vector.similarity.euclidean(e.embedding, $q) AS score "
    "RETURN e.claim_id AS claim_id, score ORDER BY score DESC LIMIT $k"
)
VEC_FILTERED = (
    "MATCH (s:Entity {eid: $start})-[rs:CLAIM*1..2]-(x:Entity) "
    "WHERE all(r IN rs WHERE " + VIS.format(r="r") + ") "
    "WITH collect(DISTINCT x.eid) + [$start] AS scope "
    "MATCH (e:Emb) WHERE e.subject IN scope "
    "WITH e, vector.similarity.euclidean(e.embedding, $q) AS score "
    "RETURN e.claim_id AS claim_id, score ORDER BY score DESC LIMIT $k"
)
INDEXES = [
    "CREATE INDEX entity_eid IF NOT EXISTS FOR (e:Entity) ON (e.eid)",
    "CREATE INDEX claim_thread IF NOT EXISTS FOR (c:Claim) "
    "ON (c.subject, c.predicate, c.valid_from)",
    "CREATE INDEX claim_id IF NOT EXISTS FOR (c:Claim) ON (c.claim_id)",
    "CREATE INDEX emb_subject IF NOT EXISTS FOR (e:Emb) ON (e.subject)",
    "CREATE VECTOR INDEX emb_vec IF NOT EXISTS FOR (e:Emb) ON (e.embedding) OPTIONS {indexConfig: "
    "{`vector.dimensions`: 128, `vector.similarity_function`: 'euclidean'}}",
]


def neo(*args: str) -> None:
    subprocess.run(
        [str(HOME / "bin" / args[0]), *args[1:]], check=True, env=ENV, capture_output=True
    )


def start() -> None:
    neo("neo4j", "start")
    for _ in range(120):
        try:
            with GraphDatabase.driver(URI) as d:
                d.verify_connectivity()
                return
        except Exception:
            time.sleep(1)
    raise RuntimeError("neo4j did not start")


def stop() -> None:
    neo("neo4j", "stop")


def driver() -> Any:
    return GraphDatabase.driver(URI)


def preds_for(robot: str) -> list[str]:
    kind = robot.split(":", 1)[1].rsplit("-", 1)[0]
    slots = next(e[2] for e in EMBODIMENTS if e[0] == kind)
    return sorted([p.name for p in ROBOT_PREDICATES] + [f"mounts/{s}" for s in slots])


def rebuild(n: int) -> None:
    p = w.paths(n)
    steps: dict[str, float] = {}
    t0 = time.perf_counter()
    stop()
    shutil.rmtree(IMPORT, ignore_errors=True)
    IMPORT.mkdir(parents=True)
    (IMPORT / "ent_h.csv").write_text("eid:ID(Entity),kind\n")
    (IMPORT / "claim_h.csv").write_text(
        "claim_id:long,subject,predicate,object_entity,object_value,valid_clock,valid_from:long,"
        "valid_to:long,recorded_at:long,superseded_at:long,assertion_kind,source_id,transform_id,"
        "supersedes:long\n"
    )
    (IMPORT / "edge_h.csv").write_text(
        "claim_id:long,:START_ID(Entity),predicate,:END_ID(Entity),valid_clock,valid_from:long,"
        "valid_to:long,recorded_at:long,superseded_at:long\n"
    )
    (IMPORT / "emb_h.csv").write_text("claim_id:long,subject,embedding:float[]\n")
    subprocess.run(f"tail -n +2 {p['claims']} > {IMPORT}/claims.csv", shell=True, check=True)
    subprocess.run(f"tail -n +2 {p['entities']} > {IMPORT}/ent.csv", shell=True, check=True)
    subprocess.run(
        f"awk -F, -v OFS=, '$4 != \"\" {{print $1,$2,$3,$4,$6,$7,$8,$9,$10}}' {IMPORT}/claims.csv "
        f"> {IMPORT}/edges.csv",
        shell=True,
        check=True,
    )
    steps["prepare_csv_ms"] = (time.perf_counter() - t0) * 1000.0
    t1 = time.perf_counter()
    neo(
        "neo4j-admin",
        "database",
        "import",
        "full",
        "neo4j",
        "--overwrite-destination",
        f"--nodes=Entity={IMPORT}/ent_h.csv,{IMPORT}/ent.csv",
        f"--nodes=Claim={IMPORT}/claim_h.csv,{IMPORT}/claims.csv",
        f"--nodes=Emb={IMPORT}/emb_h.csv,{p['emb_neo']}",
        f"--relationships=CLAIM={IMPORT}/edge_h.csv,{IMPORT}/edges.csv",
        "--array-delimiter=;",
        "--threads=8",
    )
    steps["admin_import_ms"] = (time.perf_counter() - t1) * 1000.0
    t2 = time.perf_counter()
    start()
    steps["start_ms"] = (time.perf_counter() - t2) * 1000.0
    with driver() as d:
        for stmt in INDEXES:
            name = stmt.split()[2] if "VECTOR" not in stmt else "emb_vec"
            ms, _ = w.timed(
                lambda stmt=stmt: (
                    d.execute_query(stmt),
                    d.execute_query("CALL db.awaitIndexes(36000)"),
                )
            )
            steps[f"{name}_ms"] = ms
    steps["total_ms"] = (time.perf_counter() - t0) * 1000.0
    shutil.rmtree(IMPORT, ignore_errors=True)
    w.save("neo4j", n, "rebuild", {k: round(v, 1) for k, v in steps.items()})


def _bench(n: int, name: str, query: str, params_of: Any, reps: int, warm: int) -> None:
    ms_all, rows = [], []
    with driver() as d, d.session() as s:
        for i in range(reps + warm):
            params = params_of(i)
            ms, out = w.timed(lambda params=params: list(s.run(query, params)))
            if i >= warm:
                ms_all.append(ms)
                rows.append(len(out))
    w.save("neo4j", n, name, {**w.summary(ms_all), "rows_mean": sum(rows) / len(rows)})


def thread(n: int, reps: int = 300) -> None:
    robots = w.entities(n, "robot")
    rng = random.Random(1)
    samples = w.as_of_samples(reps + 30, seed=2)

    def params(i: int) -> dict[str, Any]:
        r = rng.choice(robots)
        return {"subject": r, "preds": preds_for(r), **samples[i]}

    _bench(n, "thread", THREAD, params, reps, 30)


def traverse(n: int, reps: int = 200) -> None:
    sites = w.entities(n, "site")
    rng = random.Random(3)
    samples = w.as_of_samples(reps + 20, seed=4)
    _bench(n, "traverse", TRAVERSE, lambda i: {"start": rng.choice(sites), **samples[i]}, reps, 20)


def directed(n: int, reps: int = 200) -> None:
    sites = w.entities(n, "site")
    rng = random.Random(3)
    samples = w.as_of_samples(reps + 10, seed=4)
    _bench(n, "directed", DIRECTED, lambda i: {"start": rng.choice(sites), **samples[i]}, reps, 10)


def verify(n: int, reps: int = 100) -> None:
    """Indexed thread must equal a brute-force scan of the subject's claims."""
    robots = w.entities(n, "robot")
    rng = random.Random(11)
    brute = (
        "MATCH (c:Claim) WHERE c.subject = $subject AND " + VIS.format(r="c") + " RETURN c.claim_id"
    )
    mismatches = 0
    with driver() as d, d.session() as s:
        for at in w.as_of_samples(reps, seed=12):
            r = rng.choice(robots)
            params = {"subject": r, "preds": preds_for(r), **at}
            fast = {x[0] for x in s.run(THREAD, params)}
            slow = {x[0] for x in s.run(brute, params)}
            mismatches += fast != slow
    w.save("neo4j", n, "verify", {"samples": reps, "mismatches": mismatches})


def vector(n: int, reps: int = 100, k: int = 10) -> None:
    queries = np.load(w.paths(n)["queries"])
    sites = w.entities(n, "site")
    rng = random.Random(5)
    samples = w.as_of_samples(reps + 10, seed=6)
    ms_plain, ms_filt, recall = [], [], []
    with driver() as d, d.session() as s:
        for i in range(reps + 10):
            q = queries[i % len(queries)].tolist()
            fp = {"q": q, "k": k, "start": rng.choice(sites), **samples[i]}
            ms1, got = w.timed(lambda q=q: list(s.run(VEC_PLAIN, {"q": q, "k": k})))
            ms2, _ = w.timed(lambda fp=fp: list(s.run(VEC_FILTERED, fp)))
            if i < 10:
                continue
            ms_plain.append(ms1)
            ms_filt.append(ms2)
            if i % 5 == 0:
                exact = {r[0] for r in s.run(VEC_EXACT, {"q": q, "k": k})}
                recall.append(len({r[0] for r in got} & exact) / k)
        count = s.run("MATCH (e:Emb) RETURN count(e)").single()[0]
    w.save(
        "neo4j",
        n,
        "vector",
        {
            "unfiltered": {
                **w.summary(ms_plain),
                "recall_at_10": round(sum(recall) / len(recall), 4),
            },
            "graph_filtered": {
                **w.summary(ms_filt),
                "recall_at_10": 1.0,
                "note": "exact by construction",
            },
            "embeddings": count,
        },
    )


def write(n: int, seconds: float = 30.0, writers: int = 4, readers: int = 8) -> None:
    robots = w.entities(n, "robot")
    with driver() as d:
        recs, _, _ = d.execute_query(
            "MATCH (c:Claim) WHERE c.claim_id % 7 = 0 AND c.superseded_at IS NULL "
            "AND c.object_value IS NOT NULL RETURN c.claim_id LIMIT 200000"
        )
        top, _, _ = d.execute_query("MATCH (c:Claim) RETURN max(c.claim_id), max(c.recorded_at)")
    candidates = [r[0] for r in recs]
    random.Random(7).shuffle(candidates)
    pool = iter(candidates)
    next_id = [int(top[0][0]) + 1_000_000]
    now = int(top[0][1]) + w.DAY
    lock = threading.Lock()
    stop_ev = threading.Event()
    writes: list[float] = []
    reads: list[float] = []
    supersede = (
        "MATCH (c:Claim {claim_id: $old}) WHERE c.superseded_at IS NULL "
        "SET c.superseded_at = $at WITH c CREATE (n:Claim) SET n = properties(c), "
        "n.claim_id = $new, n.recorded_at = $at, n.superseded_at = null, "
        "n.object_value = c.object_value + '~bench', "
        "n.transform_id = 'memory.consolidate.bench@2.0.0', "
        "n.supersedes = $old"
    )

    def writer() -> None:
        with driver() as d, d.session() as s:
            while not stop_ev.is_set():
                with lock:
                    old = next(pool)
                    new = next_id[0]
                    next_id[0] += 1
                t0 = time.perf_counter()
                s.execute_write(
                    lambda tx, old=old, new=new: tx.run(
                        supersede, old=old, new=new, at=now
                    ).consume()
                )
                with lock:
                    writes.append((time.perf_counter() - t0) * 1000.0)

    def reader(seed: int) -> None:
        rng = random.Random(seed)
        samples = w.as_of_samples(100_000, seed=seed)
        with driver() as d, d.session() as s:
            i = 0
            while not stop_ev.is_set():
                r = rng.choice(robots)
                params = {"subject": r, "preds": preds_for(r), **samples[i]}
                ms, _ = w.timed(lambda params=params: list(s.run(THREAD, params)))
                i += 1
                with lock:
                    reads.append(ms)

    threads = [threading.Thread(target=writer) for _ in range(writers)]
    threads += [threading.Thread(target=reader, args=(100 + i,)) for i in range(readers)]
    for t in threads:
        t.start()
    time.sleep(seconds)
    stop_ev.set()
    for t in threads:
        t.join()
    w.save(
        "neo4j",
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


def _du(path: Path) -> int:
    return int(
        subprocess.run(
            ["du", "-sb", str(path)], capture_output=True, text=True, check=True
        ).stdout.split()[0]
    )


def footprint(n: int) -> None:
    rss = subprocess.run(
        ["ps", "-C", "java", "-o", "rss="], capture_output=True, text=True, check=False
    ).stdout.split()
    w.save(
        "neo4j",
        n,
        "footprint",
        {
            "store_bytes": _du(HOME / "data" / "databases" / "neo4j"),
            "tx_log_bytes": _du(HOME / "data" / "transactions" / "neo4j"),
            "server_rss_max_kb": max(int(x) for x in rss) if rss else None,
        },
    )


def backup(n: int) -> None:
    out = w.ROOT / "neo4j-dump"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    stop()  # community edition: dump is offline only
    ms, _ = w.timed(lambda: neo("neo4j-admin", "database", "dump", "neo4j", f"--to-path={out}"))
    size = _du(out)
    shutil.rmtree(out)
    start()
    w.save("neo4j", n, "backup", {"dump_ms": round(ms, 1), "dump_bytes": size, "online": False})


PHASES = {
    "rebuild": rebuild,
    "verify": verify,
    "thread": thread,
    "traverse": traverse,
    "directed": directed,
    "vector": vector,
    "write": write,
    "footprint": footprint,
    "backup": backup,
    "stop": lambda n: stop(),
}

if __name__ == "__main__":
    scale = int(sys.argv[1])
    for phase in sys.argv[2:] or [p for p in PHASES if p != "stop"]:
        PHASES[phase](scale)

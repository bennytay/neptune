"""Shared, seeded workload for both engines: embeddings, query parameters, timing helpers.

Benchmark-only (MVL-104). Run through ``uv run --with numpy ...``; numpy is never a package
dependency.
"""

from __future__ import annotations

import csv
import json
import random
import statistics
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from neptune_memory.store.bench.generator import DAY, VALID_CLOCK, DeploymentSpec

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path.home() / ".cache" / "neptune-bench"
DATA = ROOT / "data"
RESULTS = ROOT / "results"
DIM = 128
EMBED_STRIDE = 10  # one embedding per 10 claims


def paths(n: int) -> dict[str, Path]:
    return {
        "claims": DATA / f"claims_{n}.csv",
        "entities": DATA / f"entities_{n}.csv",
        "emb_pg": DATA / f"emb_{n}.pg.csv",
        "emb_neo": DATA / f"emb_{n}.neo.csv",
        "queries": DATA / f"queries_{n}.npy",
    }


def make_embeddings(n: int) -> int:
    """Clustered 128-d vectors for every 10th claim; seeded; written once per scale."""
    p = paths(n)
    rng = np.random.default_rng(104)
    centers = rng.standard_normal((64, DIM)).astype(np.float32)
    count = 0
    with (
        p["claims"].open(encoding="utf-8") as src,
        p["emb_pg"].open("w", encoding="utf-8") as pg,
        p["emb_neo"].open("w", encoding="utf-8") as neo,
    ):
        reader = csv.reader(src)
        next(reader)
        batch: list[tuple[str, str]] = []

        def flush() -> None:
            vecs = centers[rng.integers(0, 64, len(batch))] + 0.35 * rng.standard_normal(
                (len(batch), DIM)
            ).astype(np.float32)
            for (cid, subj), v in zip(batch, vecs, strict=True):
                txt = [f"{x:.4f}" for x in v]
                pg.write(f'{cid},{subj},"[{",".join(txt)}]"\n')
                neo.write(f"{cid},{subj},{';'.join(txt)}\n")
            batch.clear()

        for row in reader:
            if int(row[0]) % EMBED_STRIDE == 0:
                batch.append((row[0], row[1]))
                count += 1
                if len(batch) == 50_000:
                    flush()
        if batch:
            flush()
    q = centers[rng.integers(0, 64, 200)] + 0.35 * rng.standard_normal((200, DIM)).astype(
        np.float32
    )
    np.save(p["queries"], q.round(4))
    return count


def entities(n: int, kind: str) -> list[str]:
    with paths(n)["entities"].open(encoding="utf-8") as fh:
        return [row[0] for row in csv.reader(fh) if row[1] == kind]


def as_of_samples(count: int, seed: int) -> list[dict[str, Any]]:
    """Valid times uniform over the 5 years; known one week later or at the end of the record."""
    span = DeploymentSpec(claims=1).span
    rng = random.Random(seed)
    out = []
    for i in range(count):
        t = rng.randrange(DAY, span - DAY)
        known = span + 90 * DAY if i % 2 else t + 7 * DAY
        out.append({"clock": VALID_CLOCK, "valid_at": t, "known_at": known})
    return out


def timed(fn: Callable[[], Any]) -> tuple[float, Any]:
    t0 = time.perf_counter()
    out = fn()
    return (time.perf_counter() - t0) * 1000.0, out


def summary(ms: list[float]) -> dict[str, float]:
    s = sorted(ms)
    q = statistics.quantiles(s, n=100, method="inclusive") if len(s) > 1 else [s[0]] * 99
    return {
        "n": len(s),
        "p50_ms": round(q[49], 3),
        "p99_ms": round(q[98], 3),
        "mean_ms": round(statistics.fmean(s), 3),
    }


def save(engine: str, n: int, name: str, payload: dict[str, Any]) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{engine}_{n}_{name}.json"
    path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    print(engine, n, name, json.dumps(payload, sort_keys=True))  # noqa: T201

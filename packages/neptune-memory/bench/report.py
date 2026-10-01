"""Collect the per-phase JSON results into one CSV, plus power-law extrapolations to 10**8 claims.

    uv run --with numpy python bench/report.py docs/benchmarks/mvl-104-results.csv

Every row says whether it was ``measured`` or ``extrapolated``. Extrapolations fit
``log(value) = a + b * log(claims)`` by least squares over the measured ladder points of the
metric.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import workload as w

FULL = 10**8
#: (engine, phase, dotted metric path) worth extrapolating; the rest are reported as measured only.
EXTRAPOLATE = [
    ("pg", "thread", "p50_ms"),
    ("pg", "thread", "p99_ms"),
    ("pg", "traverse", "p50_ms"),
    ("pg", "traverse", "p99_ms"),
    ("age", "traverse", "p50_ms"),
    ("age", "traverse", "p99_ms"),
    ("pg", "vector", "graph_filtered.p50_ms"),
    ("pg", "vector", "unfiltered.p50_ms"),
    ("pg", "write", "supersedes_per_s"),
    ("pg", "rebuild", "total_ms"),
    ("pg", "footprint", "database_bytes"),
    ("neo4j", "thread", "p50_ms"),
    ("neo4j", "thread", "p99_ms"),
    ("neo4j", "traverse", "p50_ms"),
    ("neo4j", "traverse", "p99_ms"),
    ("neo4j", "directed", "p50_ms"),
    ("neo4j", "vector", "graph_filtered.p50_ms"),
    ("neo4j", "vector", "unfiltered.p50_ms"),
    ("neo4j", "write", "supersedes_per_s"),
    ("neo4j", "rebuild", "total_ms"),
    ("neo4j", "footprint", "store_bytes"),
]


def _flatten(prefix: str, value: object, out: dict[str, float]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}.{k}" if prefix else k, v, out)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        out[prefix] = float(value)


#: Rows of the ADR table: label, (pg engine, phase, metric), (neo4j engine, phase, metric), scale.
TABLE = [
    ("As-of thread p50", ("pg", "thread", "p50_ms"), ("neo4j", "thread", "p50_ms"), 1),
    ("As-of thread p99", ("pg", "thread", "p99_ms"), ("neo4j", "thread", "p99_ms"), 1),
    (
        "3-hop undirected p50 (PG: SQL)",
        ("pg", "traverse", "p50_ms"),
        ("neo4j", "traverse", "p50_ms"),
        1,
    ),
    (
        "3-hop undirected p99 (PG: SQL)",
        ("pg", "traverse", "p99_ms"),
        ("neo4j", "traverse", "p99_ms"),
        1,
    ),
    (
        "3-hop directed p50 (PG: AGE Cypher)",
        ("age", "traverse", "p50_ms"),
        ("neo4j", "directed", "p50_ms"),
        1,
    ),
    (
        "Vector top-10 p50",
        ("pg", "vector", "unfiltered.p50_ms"),
        ("neo4j", "vector", "unfiltered.p50_ms"),
        1,
    ),
    (
        "Vector top-10 recall@10",
        ("pg", "vector", "unfiltered.recall_at_10"),
        ("neo4j", "vector", "unfiltered.recall_at_10"),
        1,
    ),
    (
        "Graph-filtered top-10 p50",
        ("pg", "vector", "graph_filtered.p50_ms"),
        ("neo4j", "vector", "graph_filtered.p50_ms"),
        1,
    ),
    (
        "Graph-filtered recall@10",
        ("pg", "vector", "graph_filtered.recall_at_10"),
        ("neo4j", "vector", "graph_filtered.recall_at_10"),
        1,
    ),
    (
        "Supersedes/s (4 writers)",
        ("pg", "write", "supersedes_per_s"),
        ("neo4j", "write", "supersedes_per_s"),
        1,
    ),
    (
        "Thread p99 under write load",
        ("pg", "write", "thread_under_load.p99_ms"),
        ("neo4j", "write", "thread_under_load.p99_ms"),
        1,
    ),
    ("Rebuild from CSV, s", ("pg", "rebuild", "total_ms"), ("neo4j", "rebuild", "total_ms"), 1e-3),
    (
        "On-disk size, GB",
        ("pg", "footprint", "database_bytes"),
        ("neo4j", "footprint", "store_bytes"),
        1e-9,
    ),
    (
        "Largest server process RSS, GB",
        ("pg", "footprint", "server_rss_max_kb"),
        ("neo4j", "footprint", "server_rss_max_kb"),
        1e-6,
    ),
    (
        "Backup, s (PG online pg_dump; Neo4j offline dump)",
        ("pg", "backup", "pg_dump_dir_j4_ms"),
        ("neo4j", "backup", "dump_ms"),
        1e-3,
    ),
]


#: Throughput and resident memory are not functions of history depth; a power law would mislead.
NO_FIT = {
    "Supersedes/s (4 writers)",
    "Thread p99 under write load",
    "Largest server process RSS, GB",
}


def _fit(points: dict[int, float]) -> float | None:
    if len(points) < 3 or min(points.values()) <= 0:
        return None
    x = np.log10(np.array(sorted(points), dtype=float))
    y = np.log10(np.array([points[k] for k in sorted(points)], dtype=float))
    slope, intercept = np.polyfit(x, y, 1)
    return float(10 ** (intercept + slope * np.log10(FULL)))


def markdown(series: dict[tuple[str, str, str], dict[int, float]]) -> str:
    scales = (10**5, 10**6, 10**7)
    head = "| Workload | " + " | ".join(
        [
            *(f"PG {s:.0e} m" for s in scales),
            "PG 1e8 x",
            *(f"Neo4j {s:.0e} m" for s in scales),
            "Neo4j 1e8 x",
        ]
    )
    lines = [head.replace("e+0", "e") + " |", "|---" * 9 + "|"]
    for label, pgk, neok, unit in TABLE:
        cells = []
        for key in (pgk, neok):
            pts = series.get(key, {})
            cells += [f"{pts[s] * unit:.3g}" if s in pts else "n/m" for s in scales]
            fit = None if label in NO_FIT or "recall" in key[2] else _fit(pts)
            cells.append("—" if fit is None else f"{fit * unit:.3g}")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(target: Path) -> None:
    rows: list[tuple[str, int, str, str, float, str]] = []
    series: dict[tuple[str, str, str], dict[int, float]] = defaultdict(dict)
    for path in sorted(w.RESULTS.glob("*.json")):
        engine, claims, phase = path.stem.split("_", 2)
        flat: dict[str, float] = {}
        _flatten("", json.loads(path.read_text(encoding="utf-8")), flat)
        for metric, value in sorted(flat.items()):
            rows.append((engine, int(claims), phase, metric, value, "measured"))
            series[(engine, phase, metric)][int(claims)] = value
    for key in EXTRAPOLATE:
        value = _fit(series.get(key, {}))
        if value is not None:
            rows.append((key[0], FULL, key[1], key[2], value, "extrapolated"))
    with target.open("w", newline="", encoding="utf-8") as fh:
        out = csv.writer(fh, lineterminator="\n")
        out.writerow(("engine", "claims", "workload", "metric", "value", "kind"))
        for engine, claims, phase, metric, value, kind in rows:
            out.writerow((engine, claims, phase, metric, f"{value:.6g}", kind))
    print(markdown(series))  # noqa: T201


if __name__ == "__main__":
    main(Path(sys.argv[1]))

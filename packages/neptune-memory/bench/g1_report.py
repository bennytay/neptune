"""Collect the G1 re-measurements into ``docs/benchmarks/g1-results.json`` (MVL-106).

    uv run python bench/g1_report.py

Reads ``~/.cache/neptune-bench/results/{g1,pg}_*.json`` (from ``bench/g1_bench.py`` and
``bench/pg_bench.py``) and ADR 0004's published 10^8 extrapolations from
``docs/benchmarks/mvl-104-results.csv``. Every budget row is labelled ``measured`` or
``extrapolated``; ``tests/test_g1_stress_scale.py`` holds the rows to their budgets.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

RESULTS = Path.home() / ".cache" / "neptune-bench" / "results"
DOCS = Path(__file__).parents[1] / "docs" / "benchmarks"
RAW = (
    "g1_10000000_recall",
    "g1_10000000_walk",
    "g1_10000000_cold",
    "g1_1000000_cold",
    "pg_10000000_rebuild",
    "pg_10000000_write",
)
EF_SEARCH = 400  # the shipped DEFAULT_EF_SEARCH, chosen from the unfiltered grid


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((RESULTS / f"{name}.json").read_text(encoding="utf-8"))
    return data


def _mvl104(workload: str, metric: str) -> float:
    """ADR 0004's PG 10^8 upper-bound extrapolation for one workload metric."""
    with (DOCS / "mvl-104-results.csv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if (row["engine"], row["claims"], row["workload"], row["metric"], row["kind"]) == (
                "pg",
                str(10**8),
                workload,
                metric,
                "extrapolated",
            ):
                return float(row["value"])
    raise LookupError(f"no ADR 0004 extrapolation for {workload}/{metric}")


def _row(
    name: str, value: float, budget: float, label: str, note: str, direction: str = "<"
) -> dict[str, Any]:
    return {
        "name": name,
        "value": round(value, 3),
        "budget": budget,
        "direction": direction,
        "label": label,
        "note": note,
    }


def main() -> None:
    raw = {name: _load(name) for name in RAW}
    cold7, cold6 = raw["g1_10000000_cold"], raw["g1_1000000_cold"]
    recall, walk = raw["g1_10000000_recall"], raw["g1_10000000_walk"]
    filtered = recall["graph_filtered_shipped"]
    write = raw["pg_10000000_write"]
    slope = {q: cold7[q] / cold6[q] for q in ("p50_ms", "p99_ms")}
    budgets = [
        _row(
            "thread_warm_1e8_p50_ms",
            _mvl104("thread", "p50_ms"),
            300,
            "extrapolated",
            "ADR 0004 upper bound, data in cache",
        ),
        _row(
            "thread_warm_1e8_p99_ms",
            _mvl104("thread", "p99_ms"),
            1000,
            "extrapolated",
            "ADR 0004 upper bound, data in cache",
        ),
        _row(
            "thread_cold_1e7_p50_ms",
            cold7["p50_ms"],
            300,
            "measured",
            "OS page cache evicted before every query, shared_buffers 16MB",
        ),
        _row(
            "thread_cold_1e7_p99_ms",
            cold7["p99_ms"],
            1000,
            "measured",
            "OS page cache evicted before every query, shared_buffers 16MB",
        ),
        _row(
            "thread_cold_1e8_p50_ms",
            2 * cold7["p50_ms"],
            300,
            "extrapolated",
            f"2x the 1e7 measurement; the 1e6->1e7 decade grew x{slope['p50_ms']:.2f}",
        ),
        _row(
            "thread_cold_1e8_p99_ms",
            2 * cold7["p99_ms"],
            1000,
            "extrapolated",
            f"2x the 1e7 measurement; the 1e6->1e7 decade grew x{slope['p99_ms']:.2f}",
        ),
        _row(
            "traverse_3hop_1e7_p50_ms",
            walk["traverse_3hop"]["p50_ms"],
            300,
            "measured",
            "jsonb visited set",
        ),
        _row(
            "traverse_3hop_1e8_p50_ms",
            _mvl104("traverse", "p50_ms"),
            300,
            "extrapolated",
            "ADR 0004 upper bound (text[] walk; equal at ordinary sites)",
        ),
        _row(
            "graph_filtered_recall_at_10_1e7",
            filtered["recall_at_10"],
            0.9,
            "measured",
            f"{recall['queries']} queries, exact scan of the scope",
            direction=">=",
        ),
        _row(
            "graph_filtered_1e7_p50_ms",
            filtered["p50_ms"],
            300,
            "measured",
            f"exact over {recall['scope_embeddings']['mean']:.0f} scope embeddings",
        ),
        _row(
            "graph_filtered_1e8_p50_ms",
            10 * filtered["p50_ms"],
            300,
            "extrapolated",
            "10x: linear in scope embeddings, which grow with history depth",
        ),
        _row(
            "supersedes_per_s_1e7",
            write["supersedes_per_s"],
            200,
            "measured",
            "ADR 0004, 4 writers",
            direction=">=",
        ),
        _row(
            "thread_p99_under_write_load_1e7_ms",
            write["thread_under_load"]["p99_ms"],
            1000,
            "measured",
            "ADR 0004, 8 readers",
        ),
        {
            **_row(
                "rebuild_1e8_s",
                _mvl104("rebuild", "total_ms") / 1000,
                7200,
                "extrapolated",
                "ADR 0004 upper bound; not evidence (GAP, MVL-132)",
            ),
            "status": "unproven",
        },
    ]
    out = {
        "issue": "MVL-106",
        "host": "i5-13600K, 20 threads, NVMe, ~5 GB free RAM; Postgres 16.15 + pgvector 0.8.1",
        "recall": {
            "queries": recall["queries"],
            "chosen": {"ef_search": EF_SEARCH, "graph_filtered": "exact"},
            "unfiltered_recall_at_10": recall[f"unfiltered_ef{EF_SEARCH}"]["recall_at_10"],
        },
        "budgets": budgets,
        "raw": raw,
    }
    target = DOCS / "g1-results.json"
    target.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(target, target.stat().st_size, "bytes")  # noqa: T201


if __name__ == "__main__":
    main()

"""MVL-98 acceptance: a thread + window query over 10⁴ packages, p50 under 300 ms (ADR 0016).

The L1 scale harness (``ledger_catalog_scale``) builds a synthetic catalog of 10 000 packages
(about 1.1 million records across seven embodiments) in a real PostgreSQL 16. The query is the
workhorse machine's thread, windowed on one of its packages' log clocks, through
``PostgresCatalog.query`` end to end: validation, the catalog point, the thread-driven candidate
statement, Arrow and the budget. Set ``NEPTUNE_LEDGER_QUERY_SCALE`` to a package count to
measure another scale; the measured numbers are printed (run with ``-s`` to see them).
"""

import os
import statistics
import time
from typing import Any

import psycopg
import pytest
from psycopg import sql

from conftest import new_database
from ledger_catalog_scale import (
    MACHINE_THREAD_KINDS,
    SCHEMA,
    TENANT,
    Scale,
    build,
    machine_thread_id,
)
from neptune_ledger.api import arrow
from neptune_ledger.api.types import QuerySpec, TimeWindow
from neptune_ledger.catalog.registry import PostgresCatalog

pytestmark = pytest.mark.slow

PACKAGES = int(os.environ.get("NEPTUNE_LEDGER_QUERY_SCALE", "10000"))
P50_MS = 300
RUNS = 41


def test_a_thread_and_window_query_over_ten_thousand_packages(pg_server: str) -> None:
    uri = new_database(pg_server)
    scale = Scale(packages=PACKAGES, samples=20)
    with psycopg.connect(uri, autocommit=True) as conn:
        build(conn, scale, {})
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(SCHEMA)))
        # The workhorse machine's packages: every heavy_every-th source, one run each.
        picked = conn.execute(
            "SELECT ns, clock0, base FROM gen_pkg WHERE machine = 0 ORDER BY seq"
        ).fetchall()
    assert len(picked) >= 20
    thread_id = machine_thread_id(str(picked[0][0]), "M-0")
    specs = [
        QuerySpec(
            kinds=MACHINE_THREAD_KINDS,
            thread_id=thread_id,
            window=TimeWindow(str(clock), int(base), int(base) + 600_000_000_000),
        )
        for _, clock, base in picked[:: max(1, len(picked) // RUNS)][:RUNS]
    ]
    times: list[float] = []
    with PostgresCatalog(uri, TENANT, package_roots=None) as catalog:
        catalog.query(specs[0])  # warm the connection and the plan cache
        for spec in specs:
            start = time.perf_counter()
            table = catalog.query(spec)
            times.append((time.perf_counter() - start) * 1000)
            meta = arrow.query_meta(table)
            assert meta.findings == ()
            rows: Any = arrow.query_rows(table)
            assert [r.kind for r in rows] == ["run"], "the window holds that package's run"
    p50 = statistics.median(times)
    p95 = sorted(times)[int(0.95 * (len(times) - 1))]
    print(f"\nthread + window over {PACKAGES} packages: p50 {p50:.1f} ms, p95 {p95:.1f} ms")  # noqa: T201 - the measurement
    assert p50 < P50_MS

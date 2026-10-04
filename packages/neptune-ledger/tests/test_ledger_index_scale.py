"""The time and spatial indexes scale: building them is linear, a lookup is not a scan (ADR 0015).

Build: the rows registration derives from a package grow in proportion to the package. Lookup: a
window or a box searches the R-tree inside its own clock or reference, so four times the index
costs about the same pages, not four times as many. Lookups are measured in buffers, which a busy
machine does not change; builds in time, with a wide margin.
"""

import os
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import psycopg

from ledger_index_packages import child_frame, drone, one_of, ticks_batch, urdf
from ledger_series_packages import read
from neptune.model.run import Stream
from neptune.store.package import package_contents, write_package
from neptune.store.series import SERIES_SETTINGS, write_series
from neptune_ledger.catalog.check import check_package, open_root
from neptune_ledger.catalog.migrate import apply_migrations
from neptune_ledger.lake.space_index import (
    _LIMIT,
    _PLACED,
    _UNPLACED,
    FrameReference,
    extent_rows,
    reference_text,
)
from neptune_ledger.lake.time_index import _WINDOW, series_intervals

Conn = psycopg.Connection[tuple[object, ...]]
CLOCKS = 40
FRAMES = 20


def _best(run: Callable[[], object], times: int = 3) -> float:
    best = float("inf")
    for _ in range(times):
        start = time.perf_counter()
        run()
        best = min(best, time.perf_counter() - start)
    return best


# --- build ---------------------------------------------------------------------------------------


def test_extent_rows_grow_linearly_with_the_records() -> None:
    camera = one_of(urdf(), "frame_transform", lambda r: child_frame(r) == "front_camera")
    record = read(camera)
    small, large = (record,) * 5_000, (record,) * 20_000
    assert len(extent_rows(large)) == 4 * len(extent_rows(small)) == 40_000
    ratio = _best(lambda: extent_rows(large)) / _best(lambda: extent_rows(small))
    assert ratio < 8, f"4x the records took {ratio:.1f}x the time"


def test_series_intervals_grow_linearly_with_the_rows(tmp_path: Path) -> None:
    flight = drone()
    record = one_of(flight, "stream", lambda r: r["topic"].get("value") == "sensor_accel")
    stream = read(record)
    assert isinstance(stream, Stream)
    others = [read(r) for r in flight if r["kind"] != "stream" or r["id"] == stream.id]
    took = {}
    for n in (100_000, 400_000):
        made = ticks_batch(stream, n, [lambda i: 4_000 * i, lambda i: 4_000 * i - 150])
        series = tmp_path / f"{n}.parquet"
        write_series(stream, [made], series)
        root = tmp_path / f"p{n}"
        write_package(
            root,
            package_contents(others, series={stream.id: series}, store={"series": SERIES_SETTINGS}),
        )
        fd = open_root(str(root))
        assert fd is not None
        try:
            package = check_package(fd, "register").package
            assert package is not None
            found = {r.clock: r for r in series_intervals(fd, package)}
            boot, sample = (found[c] for c in stream.clocks)
            assert (sample.rows_known, sample.last) == (n, 4_000 * (n - 1) - 150)
            assert (boot.first, boot.last) == (0, 4_000 * (n - 1))
            took[n] = _best(partial(series_intervals, fd, package))
        finally:
            os.close(fd)
    ratio = took[400_000] / took[100_000]
    assert ratio < 8, f"4x the rows took {ratio:.1f}x the time"


# --- lookup --------------------------------------------------------------------------------------


def _fill_time(conn: Conn, start: int, stop: int) -> None:
    """Series intervals ``start`` to ``stop`` of a catalog spreading them over ``CLOCKS`` clocks,
    each clock's back to back: a lookup at a clock's end is the B-tree's worst case."""
    conn.execute("SET session_replication_role = replica")  # synthetic rows: no parent records
    conn.execute(
        "INSERT INTO time_interval (tenant_id, subject, kind, record_id, package_id, clock,"
        " first_tick, last_tick, rows_known, rows_unknown, registration_key)"
        " SELECT 'acme', 'series', 'stream',"
        "   'rec:sha256:' || encode(sha256(convert_to('r' || i, 'UTF8')), 'hex'),"
        "   'sha256:' || encode(sha256(convert_to('p' || (i / 100), 'UTF8')), 'hex'),"
        "   'rec:sha256:' || encode(sha256(convert_to('c' || (i %% %(clocks)s), 'UTF8')), 'hex'),"
        "   (i / %(clocks)s) * 1000, (i / %(clocks)s) * 1000 + 999, 10, 0, 1"
        " FROM generate_series(%(start)s, %(stop)s - 1) AS i",
        {"clocks": CLOCKS, "start": start, "stop": stop},
    )
    conn.execute("SET session_replication_role = origin")
    conn.execute("ANALYZE time_interval")


def _fill_space(conn: Conn, start: int, stop: int, references: list[str]) -> None:
    """Point extents ``start`` to ``stop``, in metres on a 1 m grid, spread over ``references``:
    every robot's frames near its own origin, the case a plain R-tree mixes."""
    conn.execute("SET session_replication_role = replica")
    conn.execute(
        "INSERT INTO spatial_extent (tenant_id, kind, record_id, package_id, pointer,"
        " registration_key, reference_kind, reference, extent_pointer, dims, unit,"
        " min_x, min_y, min_z, max_x, max_y, max_z)"
        " SELECT 'acme', 'frame_transform',"
        "   'rec:sha256:' || encode(sha256(convert_to('r' || i, 'UTF8')), 'hex'),"
        "   'sha256:' || encode(sha256(convert_to('p' || (i / 100), 'UTF8')), 'hex'),"
        "   '/parent', 1, 'frame', (%(refs)s::text[])[1 + i %% %(n)s],"
        "   '/value/translation/values', 3, 'm',"
        "   (i / %(n)s) %% 100, (i / %(n)s) / 100, 0, (i / %(n)s) %% 100, (i / %(n)s) / 100, 0"
        " FROM generate_series(%(start)s, %(stop)s - 1) AS i",
        {"refs": references, "n": len(references), "start": start, "stop": stop},
    )
    conn.execute("SET session_replication_role = origin")
    conn.execute("ANALYZE spatial_extent")


def _explain(conn: Conn, statement: str, params: dict[str, Any]) -> tuple[int, str, int]:
    """Buffers the statement touched, its plan as text and the rows it returned."""
    found = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, params).fetchone()
    assert found is not None
    top: Any = found[0]
    plan = top[0]["Plan"]
    buffers = int(plan["Shared Hit Blocks"]) + int(plan["Shared Read Blocks"])
    return buffers, str(top), int(plan["Actual Rows"])


def test_a_window_lookup_searches_its_clock_not_the_index(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    pg.execute("SET search_path TO tenant_acme")
    row = pg.execute("SELECT 'rec:sha256:' || encode(sha256(convert_to('c0', 'UTF8')), 'hex')")
    clock = row.fetchone()
    assert clock is not None
    measured = []
    for start, stop in ((0, 20_000), (20_000, 80_000)):
        _fill_time(pg, start, stop)
        last = (stop // CLOCKS - 1) * 1000
        params = {"tenant": "acme", "clock": clock[0], "lo": last - 2_000, "hi": last}
        measured.append(_explain(pg, _WINDOW, {**params, "as_of": 1, "cap": 1_000}))
    (small, plan, rows), (large, _, rows_large) = measured
    assert "time_interval_by_span" in plan and "Seq Scan" not in plan
    assert rows == rows_large == 3, "the window meets three intervals at the clock's end"
    assert large <= 2 * small + 4, f"{small} buffers at 20k intervals, {large} at 80k"


def test_a_box_lookup_searches_its_reference_not_the_index(pg: Conn) -> None:
    graph = "rec:sha256:" + "a" * 64
    frames = [FrameReference(graph, f"robot_{k}/base") for k in range(FRAMES)]
    references = [reference_text(f)[1] for f in frames]
    apply_migrations(pg, "acme")
    pg.execute("SET search_path TO tenant_acme")
    kind, text = reference_text(frames[0])
    params = {
        "tenant": "acme",
        "kind": kind,
        "reference": text,
        "as_of": 1,
        "unit": "m",
        "scope": f"{kind} {text} m",
        "x0": 10.0,
        "y0": 0.0,
        "x1": 12.0,
        "y1": 0.0,
    }
    measured, others = [], []
    for start, stop in ((0, 20_000), (20_000, 80_000)):
        _fill_space(pg, start, stop, references)
        measured.append(_explain(pg, _PLACED, params))
        others.append(_explain(pg, _UNPLACED + _LIMIT, {**params, "three": False, "cap": 11}))
    (small, plan, rows), (large, _, rows_large) = measured
    assert "spatial_extent_by_scope" in plan and "Seq Scan" not in plan
    assert rows == rows_large == 3, "x 10, 11 and 12 on row y 0 of robot 0's grid"
    assert large <= 2 * small + 4, f"{small} buffers at 20k extents, {large} at 80k"
    # Every member is comparable (metres, three axes): the unplaced lookup reads none of them.
    (few, unplaced_plan, none), (few_large, _, none_large) = others
    assert none == none_large == 0
    assert "spatial_extent_by_reference" in unplaced_plan and "Seq Scan" not in unplaced_plan
    assert few_large <= few + 4 <= 40, f"{few} buffers at 20k extents, {few_large} at 80k"

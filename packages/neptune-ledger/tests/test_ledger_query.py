"""The query engine over registered packages (MVL-98, Ledger ADR 0016).

Packages from several embodiments are registered, then queried through ``PostgresCatalog.query``
and ``QueryEngine``. Each case checks one claim: a thread and a window join series rows across
packages on one clock; a frame window finds what the spatial index places in the box; a lineage
preference is resolved over whole lineage sets, never over what a window kept; a cut answer is a
flagged prefix; the same call gives the same bytes; a spec outside the contract is refused; and
SQL passthrough cannot leave its views.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pyarrow as pa
import pytest

from ledger_index_packages import (
    BOOT,
    CRS84,
    child_frame,
    drone,
    drone_series,
    frame_graph,
    one_of,
    urdf,
    vector_map,
    warehouse,
)
from ledger_series_packages import (
    ARM,
    LEGGED,
    MOBILE,
    batch,
    battery,
    declared_run,
    joint_values,
    odometry,
    one,
    second_part,
    stream_of,
    write,
)
from ledger_thread_packages import Record, files, reparsed, subset
from neptune.store.package import write_package
from neptune_ledger.api import arrow
from neptune_ledger.api.types import (
    CrsReference,
    DeclaredKey,
    EvidenceAnchor,
    FrameReference,
    FrameWindow,
    LatestTransform,
    QueryBudget,
    QueryMeta,
    QuerySpec,
    SeriesJoin,
    ThreadKey,
    TimeWindow,
)
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.lake.indexes import IndexCatalog
from neptune_ledger.lake.read import (
    DataFusionReader,
    DuckDBReader,
    ScanInterrupted,
    SeriesPlan,
    SeriesRead,
    plan_series,
)
from neptune_ledger.lake.series import SeriesCatalog
from neptune_ledger.lake.space_index import SPATIAL_KINDS, SpatialBox
from neptune_ledger.query import QueryEngine, QueryLimits
from test_ledger_registration import fresh

START = 1_790_762_401_000_000_000
JOINTS = 7


@pytest.fixture
def catalog(pg_uri: str) -> Iterator[PostgresCatalog]:
    with fresh(pg_uri) as made:
        yield made


@pytest.fixture
def engine(pg_uri: str, catalog: PostgresCatalog) -> Iterator[QueryEngine]:
    with QueryEngine(pg_uri, "acme") as made:
        yield made


def register(catalog: PostgresCatalog, root: Path) -> str:
    outcome = catalog.register(root)
    assert outcome.outcome == "registered", outcome.findings
    return str(outcome.package_id.value)  # type: ignore[union-attr]


def meta_of(table: Any) -> QueryMeta:
    return arrow.query_meta(table)


def codes(table: Any) -> list[tuple[str, str]]:
    return [(f.code, f.subject) for f in meta_of(table).findings]


def pairs(table: Any) -> list[tuple[str, str]]:
    return list(
        zip(
            table.column("package_id").to_pylist(),
            table.column("record_id").to_pylist(),
            strict=True,
        )
    )


def stream_key(record: Record) -> ThreadKey:
    evidence = record["provenance"]["evidence"]
    return ThreadKey("stream", EvidenceAnchor(evidence["source"], tuple(evidence["locator"])))


# --- a thread and a window, joined to series rows across packages --------------------------------


def _with_sites(rows: list[Record]) -> list[Record]:
    """The bag's records plus the site table's: a second package holding the same streams."""
    sites = subset("mobile_robot", "csv")
    have = {(r["kind"], r.get("id", r.get("content_id"))) for r in rows}
    return rows + [r for r in sites if (r["kind"], r.get("id", r.get("content_id"))) not in have]


@pytest.fixture
def mobile_twice(catalog: PostgresCatalog, tmp_path: Path) -> dict[str, Any]:
    """A mobile base's bag, registered alone and again inside a second package: one stream
    thread across two packages, on one log clock."""
    rows = subset(*MOBILE)
    odom, power = stream_of(rows, "/wheel_odom"), stream_of(rows, "/battery")
    made = {
        "/wheel_odom": batch(odom, 40, START, 100, odometry, unknown_last=5),
        "/battery": batch(power, 4, START + 50, 1_000, battery),
    }
    write(rows, tmp_path / "base", made)
    base = register(catalog, tmp_path / "base")
    write(_with_sites(rows), tmp_path / "again", made)
    again = register(catalog, tmp_path / "again")
    return {"rows": rows, "odom": odom, "power": power, "base": base, "again": again}


def test_a_thread_and_a_window_join_series_rows_across_packages(
    catalog: PostgresCatalog, pg_uri: str, mobile_twice: dict[str, Any]
) -> None:
    odom = mobile_twice["odom"]
    key = stream_key(one(mobile_twice["rows"], "stream", "/wheel_odom"))
    window = TimeWindow(odom.clocks[0], START + 1_000, START + 2_000)
    streams = catalog.query(QuerySpec(kinds=("stream",), thread_id=key.thread_id))
    assert sorted(pairs(streams)) == sorted(
        [(mobile_twice["base"], odom.id), (mobile_twice["again"], odom.id)]
    ), "the stream thread spans both packages"
    spec = QuerySpec(
        kinds=("stream",),
        thread_id=key.thread_id,
        window=window,
        series=SeriesJoin(("value/linear_x",)),
    )
    table = catalog.query(spec)
    assert codes(table) == []
    assert table.schema.names == [
        *arrow.QUERY_RESULT_SCHEMA.names,
        "clock",
        "ticks",
        "ticks_state",
        "seq",
        "value/linear_x",
    ]
    assert table.column("ticks").to_pylist() == [START + 100 * i for i in range(10, 21)]
    assert set(pairs(table)) == {(mobile_twice["base"], odom.id)}, "read once, first package"
    assert set(table.column("clock").to_pylist()) == {odom.clocks[0]}
    # The join is the lake's own read of the file in the window, with the stream's record row.
    with SeriesCatalog(pg_uri, "acme") as series:
        (file,) = series.files([(mobile_twice["base"], odom.id)]).files
    direct = DuckDBReader().read(plan_series([file], windows=[window], columns=["value/linear_x"]))
    for name in ("ticks", "seq", "value/linear_x"):
        assert table.column(name).to_pylist() == direct.table.column(name).to_pylist()
    (record,) = [r for r in arrow.query_rows(streams) if r.package_id == mobile_twice["base"]]
    assert set(table.column("line").to_pylist()) == {record.line}
    assert set(table.column("source_locator").to_pylist()) == {record.source_locator}


@pytest.fixture
def arm_run(catalog: PostgresCatalog, tmp_path: Path) -> dict[str, Any]:
    """An arm's shift recorded as two MCAP files: one run thread, two packages, two clocks."""
    part0 = declared_run(subset(*ARM), "cell-3/shift-a")
    part1 = second_part(part0, "shift-a-part1")
    out: dict[str, Any] = {}
    for name, rows, start, n in (("p0", part0, START, 300), ("p1", part1, START + 300_000, 200)):
        stream = stream_of(rows, "/joint_states")
        write(
            rows,
            tmp_path / name,
            {"/joint_states": batch(stream, n, start, 1_000, joint_values(JOINTS))},
        )
        out[name] = {"package": register(catalog, tmp_path / name), "stream": stream}
    return out


def test_a_window_reads_one_clock_of_a_thread_that_spans_two(
    catalog: PostgresCatalog, arm_run: dict[str, Any]
) -> None:
    run = ThreadKey("run", DeclaredKey("manifest", "cell-3/shift-a"))
    for name, n in (("p0", 300), ("p1", 200)):
        part = arm_run[name]
        clock = part["stream"].clocks[0]
        spec = QuerySpec(
            kinds=("stream",),
            thread_id=run.thread_id,
            window=TimeWindow(clock, START - 10**9, START + 10**9),
            series=SeriesJoin(("value/position",)),
        )
        table = catalog.query(spec)
        assert codes(table) == []
        assert table.num_rows == n
        assert set(pairs(table)) == {(part["package"], part["stream"].id)}
        assert set(table.column("clock").to_pylist()) == {clock}, "never another clock's rows"


def test_a_series_join_is_the_same_on_both_engines(
    pg_uri: str, catalog: PostgresCatalog, mobile_twice: dict[str, Any]
) -> None:
    odom = mobile_twice["odom"]
    spec = QuerySpec(
        kinds=("stream",),
        window=TimeWindow(odom.clocks[0], START, START + 10**6),
        series=SeriesJoin(),
        explain=True,
    )
    answers = []
    for reader in (DuckDBReader(), DataFusionReader()):
        with QueryEngine(pg_uri, "acme", reader=reader) as made:
            answers.append(made.query(spec))
    duck, fusion = answers
    assert duck.replace_schema_metadata().equals(fusion.replace_schema_metadata())
    steps = [(s.engine, s.operation) for s in meta_of(fusion).plan or ()]
    assert ("datafusion", "series.scan") in steps
    assert arrow.ipc_bytes(duck) == arrow.ipc_bytes(catalog.query(spec)), "DuckDB by default"


# --- frame windows ---------------------------------------------------------------------------


@pytest.fixture
def legged(catalog: PostgresCatalog, tmp_path: Path) -> dict[str, Any]:
    rows = urdf()
    write_package(tmp_path / "urdf", files(rows))
    return {
        "rows": rows,
        "package": register(catalog, tmp_path / "urdf"),
        "graph": frame_graph(rows),
    }


@pytest.mark.parametrize(
    ("unit", "low", "high"),
    [
        ("m", (0.2, -0.1), (0.3, 0.1)),
        ("m", (0.0, -1.0, -0.01), (1.0, 1.0, 0.01)),
        ("m", (-10.0, -10.0), (10.0, 10.0)),
        ("mm", (0.0, -1000.0), (1000.0, 1000.0)),
    ],
)
def test_a_frame_window_returns_what_the_index_places_in_the_box(
    catalog: PostgresCatalog,
    pg_uri: str,
    legged: dict[str, Any],
    unit: str,
    low: tuple[float, ...],
    high: tuple[float, ...],
) -> None:
    base = FrameReference(legged["graph"], "base")
    with IndexCatalog(pg_uri, "acme") as index:
        placed = index.within(base, unit, SpatialBox(low, high), unplaced=False).placed
    expected = sorted({(p.kind, p.record_id, p.package_id) for p in placed})
    table = catalog.query(QuerySpec(kinds=SPATIAL_KINDS, frame=FrameWindow(base, unit, low, high)))
    assert codes(table) == []
    got = [(r.kind, r.record_id, r.package_id) for r in arrow.query_rows(table)]
    assert got == expected
    if unit == "m" and low == (0.2, -0.1):
        camera = one_of(
            legged["rows"], "frame_transform", lambda r: child_frame(r) == "front_camera"
        )
        assert got == [("frame_transform", camera["id"], legged["package"])]


def test_a_frame_window_in_a_crs_compares_codes_verbatim(
    catalog: PostgresCatalog, tmp_path: Path
) -> None:
    for name, rows in (("docks", warehouse()), ("floor", vector_map())):
        write_package(tmp_path / name, files(rows))
        register(catalog, tmp_path / name)
    box = ((103.6000, 1.3500), (103.6006, 1.3506))
    crs84 = CrsReference(CRS84["authority"], CRS84["code"])
    table = catalog.query(QuerySpec(kinds=SPATIAL_KINDS, frame=FrameWindow(crs84, "deg", *box)))
    assert [r.kind for r in arrow.query_rows(table)] == ["asset"], "the map has no extent"
    wgs84 = FrameWindow(CrsReference("EPSG", "4326"), "deg", (-180.0, -90.0), (180.0, 90.0))
    assert catalog.query(QuerySpec(kinds=SPATIAL_KINDS, frame=wgs84)).num_rows == 0


def test_a_frame_window_filters_a_thread(catalog: PostgresCatalog, legged: dict[str, Any]) -> None:
    """A filter that does not drive is applied exactly: the same rows whichever index drives."""
    base = FrameWindow(FrameReference(legged["graph"], "base"), "m", (-9.0, -9.0), (9.0, 9.0))
    alone = catalog.query(QuerySpec(kinds=SPATIAL_KINDS, frame=base, explain=True))
    packages = (legged["package"],)
    both = catalog.query(
        QuerySpec(kinds=SPATIAL_KINDS, frame=base, packages=packages, explain=True)
    )
    assert alone.num_rows == 2
    assert alone.to_pylist() == both.to_pylist()
    assert next(s.operation for s in meta_of(alone).plan or ()) == "candidates.frame"


# --- the window rule, and lineage over whole sets --------------------------------------------


def test_a_window_driven_query_keeps_exactly_the_records_the_rule_keeps(
    catalog: PostgresCatalog, pg_uri: str, tmp_path: Path
) -> None:
    rows = drone()
    write(rows, tmp_path / "drone", drone_series(rows))
    register(catalog, tmp_path / "drone")
    with psycopg.connect(pg_uri) as conn:
        conn.execute("SET search_path TO tenant_acme")
        bounds = conn.execute(
            "SELECT min(world_first), max(coalesce(world_last, world_first)) FROM record"
            " WHERE world_clock = %s",
            (BOOT,),
        ).fetchone()
        assert bounds is not None and bounds[0] is not None
        for first, last in ((bounds[0], bounds[0]), (bounds[0] - 10, bounds[1] + 10), (0, 1)):
            expected = conn.execute(
                "SELECT kind, record_id, package_id FROM record WHERE world_clock = %s"
                " AND world_first <= %s AND coalesce(world_last, world_first) >= %s"
                " ORDER BY kind, record_id, package_id",
                (BOOT, last, first),
            ).fetchall()
            window = TimeWindow(BOOT, first, last)
            table = catalog.query(QuerySpec(kinds=("run", "stream"), window=window, explain=True))
            assert [(r.kind, r.record_id, r.package_id) for r in arrow.query_rows(table)] == [
                tuple(row) for row in expected
            ]
            assert (meta_of(table).plan or ())[0].operation == "candidates.window"


def test_a_window_never_picks_the_transform(catalog: PostgresCatalog, tmp_path: Path) -> None:
    v1 = subset(*LEGGED)
    v2 = reparsed(v1, "2.0.0")
    for name, rows in (("v1", v1), ("v2", v2)):
        stream = stream_of(rows, "/joint_states")
        write(
            rows, tmp_path / name, {"/joint_states": batch(stream, 3, START, 10, joint_values(12))}
        )
        register(catalog, tmp_path / name)
    history = catalog.query(QuerySpec(kinds=("run", "stream")))
    timed = [r for r in arrow.query_rows(history) if r.world_clock is not None]
    v1_ids = {r["id"] for r in v1 if "id" in r}
    mine = next(r for r in timed if r.record_id in v1_ids)
    assert mine.world_first is not None and mine.world_clock is not None
    window = TimeWindow(mine.world_clock, mine.world_first, mine.world_first)
    plain = catalog.query(QuerySpec(kinds=(mine.kind,), window=window))
    assert mine.record_id in {r.record_id for r in arrow.query_rows(plain)}
    # v2's sibling sits on v2's own clock, outside the window; it still wins the lineage set.
    latest = catalog.query(QuerySpec(kinds=(mine.kind,), window=window, lineage=LatestTransform()))
    assert latest.num_rows == 0
    assert codes(latest) == []


# --- budgets ---------------------------------------------------------------------------------


class Ticking:
    """A clock that moves ``step`` seconds every time it is read: a deadline that runs out
    after a fixed number of checks, so a time cut is reproducible inside a test."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_a_time_budget_returns_a_prefix_flagged_as_not_reproducible(
    pg_uri: str, catalog: PostgresCatalog, mobile_twice: dict[str, Any]
) -> None:
    full = catalog.query(QuerySpec(kinds=("stream", "run", "image", "site")))
    assert full.num_rows > 4
    limits = QueryLimits(batch_rows=2)
    with QueryEngine(pg_uri, "acme", limits=limits, clock=Ticking(0.25)) as made:
        spec = QuerySpec(
            kinds=("stream", "run", "image", "site"), budget=QueryBudget(max_millis=1_000)
        )
        cut = made.query(spec)
    meta = meta_of(cut)
    assert meta.budget is not None
    assert (meta.budget.exceeded, meta.budget.reproducible) == (("time",), False)
    assert [(f.code, f.subject) for f in meta.findings] == [("budget_exceeded", "time")]
    n = cut.num_rows
    assert 0 < n < full.num_rows
    assert cut.to_pylist() == full.slice(0, n).to_pylist(), "a prefix, whole batches"
    replay = catalog.query(
        QuerySpec(kinds=("stream", "run", "image", "site"), budget=QueryBudget(max_rows=n))
    )
    assert replay.to_pylist() == cut.to_pylist(), "max_rows = rows reproduces it"


class Interrupted:
    """A reader whose scan the deadline stops, as DuckDB's watchdog does."""

    name = "duckdb"

    def read(self, plan: SeriesPlan, *, timeout: float | None = None) -> SeriesRead:
        raise ScanInterrupted("the read's time ran out")

    def explain(self, plan: SeriesPlan, *, analyze: bool = False) -> str:
        return ""


def test_a_series_scan_stopped_at_the_deadline_returns_no_series_row(
    pg_uri: str, mobile_twice: dict[str, Any]
) -> None:
    odom = mobile_twice["odom"]
    spec = QuerySpec(
        kinds=("stream",),
        window=TimeWindow(odom.clocks[0], START, START + 10**6),
        series=SeriesJoin(),
        budget=QueryBudget(max_millis=60_000),
    )
    with QueryEngine(pg_uri, "acme", reader=Interrupted()) as made:
        table = made.query(spec)
    assert table.num_rows == 0
    meta = meta_of(table)
    assert meta.budget is not None and meta.budget.reproducible is False
    assert ("budget_exceeded", "time") in codes(table)


def test_a_series_join_over_the_row_budget_is_its_first_rows(
    catalog: PostgresCatalog, mobile_twice: dict[str, Any]
) -> None:
    odom = mobile_twice["odom"]
    window = TimeWindow(odom.clocks[0], START, START + 10**6)
    whole = catalog.query(QuerySpec(kinds=("stream",), window=window, series=SeriesJoin()))
    assert whole.num_rows == 40 + 4, "every odometry and battery row in the window"
    cut = catalog.query(
        QuerySpec(
            kinds=("stream",), window=window, series=SeriesJoin(), budget=QueryBudget(max_rows=6)
        )
    )
    assert cut.to_pylist() == whole.slice(0, 6).to_pylist()
    assert codes(cut) == [("budget_exceeded", "rows")]
    ticks = cut.column("ticks").to_pylist()
    assert ticks == sorted(ticks), "rows of two streams interleave by ticks on their one clock"


def test_the_same_query_gives_the_same_bytes(
    catalog: PostgresCatalog, mobile_twice: dict[str, Any]
) -> None:
    odom = mobile_twice["odom"]
    specs = [
        QuerySpec(kinds=("stream", "run"), explain=True, budget=QueryBudget(max_bytes=2_000)),
        QuerySpec(
            kinds=("stream",),
            window=TimeWindow(odom.clocks[0], START, START + 10**6),
            series=SeriesJoin(),
            explain=True,
        ),
    ]
    for spec in specs:
        assert arrow.ipc_bytes(catalog.query(spec)) == arrow.ipc_bytes(catalog.query(spec))


# --- specs outside the contract ----------------------------------------------------------------

STREAM = "rec:sha256:" + "1" * 64


REFUSED: list[tuple[Any, str]] = [
    (QuerySpec(kinds=("run",), budget=QueryBudget(max_rows=10**8)), "budget"),
    (QuerySpec(kinds=("run",), budget=QueryBudget(max_millis=10**7)), "budget"),
    (
        QuerySpec(kinds=("run", "stream"), window=TimeWindow(STREAM, 0, 1), series=SeriesJoin()),
        "series",
    ),
    (
        QuerySpec(kinds=("stream",), window=TimeWindow(STREAM, 0, 1), series=SeriesJoin(), limit=5),
        "series",
    ),
    (QuerySpec(kinds=("run",), as_of=2**63), "as_of"),
    (QuerySpec(kinds=("run",), thread_id="not-a-thread"), "spec"),
    (QuerySpec(kinds=()), "spec"),
    (
        QuerySpec(
            kinds=("run",), frame=FrameWindow(FrameReference(STREAM, ""), "m", (0, 0), (1, 1))
        ),
        "spec",
    ),
    (
        QuerySpec(
            kinds=("run",),
            frame=FrameWindow(FrameReference(STREAM, "x" * 300), "m", (0, 0), (1, 1)),
        ),
        "frame.reference",
    ),
    (
        QuerySpec(
            kinds=("run",),
            frame=FrameWindow(CrsReference("EPSG", "4326"), "m", (0, float("nan")), (1, 1)),
        ),
        "spec",
    ),
    (
        QuerySpec(
            kinds=("stream",), window=TimeWindow(STREAM, 0, 1), series=SeriesJoin(("time/0",))
        ),
        "spec",
    ),
    (None, "spec"),
]


def test_a_spec_outside_the_contract_is_refused_not_raised(catalog: PostgresCatalog) -> None:
    for spec, subject in REFUSED:
        table = catalog.query(spec)
        assert table.num_rows == 0, spec
        assert table.schema.remove_metadata().equals(arrow.QUERY_RESULT_SCHEMA)
        meta = meta_of(table)
        assert meta.budget is None, spec
        assert ("invalid_request", subject) in codes(table), (spec, codes(table))
        assert {c for c, _ in codes(table)} == {"invalid_request"}, spec


def test_a_series_join_names_columns_every_file_has(
    catalog: PostgresCatalog, mobile_twice: dict[str, Any]
) -> None:
    odom = mobile_twice["odom"]
    spec = QuerySpec(
        kinds=("stream",),
        window=TimeWindow(odom.clocks[0], START, START + 10**6),
        series=SeriesJoin(("value/linear_x",)),
    )
    table = catalog.query(spec)
    assert codes(table) == [("invalid_request", "series")], "the battery has no linear_x"


def test_a_series_join_reads_at_most_its_stream_cap(
    pg_uri: str, mobile_twice: dict[str, Any]
) -> None:
    odom = mobile_twice["odom"]
    spec = QuerySpec(
        kinds=("stream",),
        window=TimeWindow(odom.clocks[0], START, START + 10**6),
        series=SeriesJoin(),
    )
    with QueryEngine(pg_uri, "acme", limits=QueryLimits(max_streams=1)) as made:
        table = made.query(spec)
    assert table.num_rows == 0 and codes(table) == [("invalid_request", "series")]


# --- results as streams, and operators' plans ----------------------------------------------------


def test_a_stream_holds_the_tables_rows_and_metadata(
    engine: QueryEngine, mobile_twice: dict[str, Any]
) -> None:
    spec = QuerySpec(kinds=("stream", "run", "site"))
    table = engine.query(spec)
    reader = engine.stream(spec, batch_rows=2)
    batches = list(reader)
    assert len(batches) == (table.num_rows + 1) // 2
    assert pa.Table.from_batches(batches).to_pylist() == table.to_pylist()
    assert reader.schema.metadata == table.schema.metadata


def test_explain_shows_the_driving_index(engine: QueryEngine, legged: dict[str, Any]) -> None:
    thread = ThreadKey("machine", DeclaredKey("serial", "none")).thread_id
    plan = json.dumps(json.loads(engine.explain(QuerySpec(kinds=("frame",), thread_id=thread))))
    assert "thread_member" in plan
    window = TimeWindow(STREAM, 0, 10)
    assert "time_interval_by_span" in engine.explain(QuerySpec(kinds=("run",), window=window))
    analysed = json.loads(engine.explain(QuerySpec(kinds=("frame",)), analyze=True))
    assert analysed[0]["Plan"]["Actual Rows"] >= 0


# --- SQL passthrough -----------------------------------------------------------------------------


def test_sql_runs_over_the_scoped_answer(
    catalog: PostgresCatalog, mobile_twice: dict[str, Any]
) -> None:
    scope = QuerySpec(kinds=("stream", "site"), packages=(mobile_twice["base"],))
    table = catalog.sql(
        "SELECT kind, count(*) AS n, count(DISTINCT package_id) AS packages FROM records"
        " GROUP BY kind ORDER BY kind",
        scope,
    )
    assert codes(table) == []
    assert table.to_pylist() == [{"kind": "stream", "n": 2, "packages": 1}]
    meta = meta_of(table)
    assert meta.budget is not None and meta.budget.reproducible


def test_sql_over_a_series_scope_sees_the_joined_rows(
    catalog: PostgresCatalog, mobile_twice: dict[str, Any]
) -> None:
    odom = mobile_twice["odom"]
    scope = QuerySpec(
        kinds=("stream",),
        window=TimeWindow(odom.clocks[0], START, START + 10**6),
        series=SeriesJoin(),
    )
    table = catalog.sql(
        "SELECT record_id, count(*) AS n, min(ticks) AS first FROM series GROUP BY record_id"
        " ORDER BY record_id",
        scope,
    )
    assert codes(table) == []
    assert {row["n"] for row in table.to_pylist()} == {40, 4}


SECRET = pa.table({"x": [42]})


ESCAPES: list[str] = [
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM '/etc/passwd'",
    "SELECT * FROM glob('/*')",
    "SELECT * FROM read_text('/etc/hostname')",
    "SELECT * FROM read_parquet('series/x.parquet')",
    "COPY records TO '{tmp}/leak.csv'",
    "EXPORT DATABASE '{tmp}/export'",
    "ATTACH '{tmp}/other.db' AS other",
    "INSTALL httpfs",
    "LOAD httpfs",
    "SET enable_external_access = true",
    "PRAGMA enable_profiling",
    "CALL pragma_version()",
    "CREATE TABLE stash AS SELECT * FROM records",
    "DROP VIEW records",
    "SELECT 1; SELECT 2",
    "EXPLAIN SELECT 1",
    "SELECT * FROM SECRET",
    "SELECT * FROM secret",
    "SELECT * FROM tenant_acme.record",
    "SELECT * FROM query('CREATE TABLE q (a INT)')",
    "SELECT * FROM duckdb_extensions()",
    "not sql at all",
    "",
    "SELECT '\x00'",
    "SELECT " + "1 + " * 20_000 + "1",
]


def test_sql_cannot_leave_its_views(
    catalog: PostgresCatalog, mobile_twice: dict[str, Any], tmp_path: Path
) -> None:
    secret = SECRET  # a Python object a replacement scan would find in this frame
    assert secret.num_rows == 1
    before = {path.name for path in tmp_path.iterdir()}
    for statement in ESCAPES:
        text = statement.replace("{tmp}", str(tmp_path))
        table = catalog.sql(text, QuerySpec(kinds=("stream",)))
        assert table.num_rows == 0, statement
        assert [c for c, _ in codes(table)] == ["invalid_request"], (statement, codes(table))
    assert {path.name for path in tmp_path.iterdir()} == before, "nothing was written"


def test_sql_reads_its_own_settings_as_locked(
    catalog: PostgresCatalog, mobile_twice: dict[str, Any]
) -> None:
    table = catalog.sql(
        "SELECT current_setting('enable_external_access') AS external,"
        " current_setting('lock_configuration') AS locked,"
        " current_setting('python_enable_replacements') AS replacements",
        QuerySpec(kinds=("stream",)),
    )
    assert table.to_pylist() == [{"external": False, "locked": True, "replacements": False}]


def test_sql_is_bounded_by_the_same_budgets(
    catalog: PostgresCatalog, mobile_twice: dict[str, Any]
) -> None:
    rows = catalog.sql(
        "SELECT range AS n FROM range(100000) ORDER BY n",
        QuerySpec(kinds=("stream",), budget=QueryBudget(max_rows=10)),
    )
    assert rows.column("n").to_pylist() == list(range(10))
    assert codes(rows) == [("budget_exceeded", "rows")]
    slow = catalog.sql(
        "SELECT count(*) FROM range(1000000000000)",
        QuerySpec(kinds=("stream",), budget=QueryBudget(max_millis=300)),
    )
    meta = meta_of(slow)
    assert ("budget_exceeded", "time") in codes(slow)
    assert meta.budget is not None and meta.budget.reproducible is False
    assert slow.num_rows == 0

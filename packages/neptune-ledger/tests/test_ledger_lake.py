"""The lakehouse view: series Parquet read in place across packages (MVL-95, ADR 0013).

Packages from three embodiments (an arm's joint states, a mobile base's odometry and battery, a
legged robot's joint states) are registered, resolved to their series files by the
``SeriesCatalog``, and read with DuckDB and DataFusion. Both engines must return the same table,
push each window down to the scan, and leave every byte where it was.
"""

import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

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
from ledger_thread_packages import Record, reparsed, subset
from neptune.model.time import INT64_MAX, INT64_MIN
from neptune_ledger.api.types import (
    DeclaredKey,
    EvidenceAnchor,
    History,
    LatestTransform,
    ThreadKey,
    TimeWindow,
)
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.lake.read import (
    DataFusionReader,
    DuckDBReader,
    LakeRequestError,
    SeriesPlan,
    SeriesReader,
    plan_series,
    read_findings,
)
from neptune_ledger.lake.series import SeriesCatalog, SeriesFile
from neptune_ledger.lake.store import LocalObjectStore, StoreError
from test_ledger_registration import fresh

READERS: tuple[SeriesReader, ...] = (DuckDBReader(), DataFusionReader())
START = 1_790_762_401_000_000_000  # ns on each recording's log clock
JOINTS = 7


@pytest.fixture
def catalog(pg_uri: str) -> Iterator[PostgresCatalog]:
    with fresh(pg_uri) as made:
        yield made


@pytest.fixture
def series(pg_uri: str, catalog: PostgresCatalog) -> Iterator[SeriesCatalog]:
    with SeriesCatalog(pg_uri, "acme") as made:
        yield made


def register(catalog: PostgresCatalog, root: Path) -> None:
    outcome = catalog.register(root)
    assert outcome.outcome == "registered", outcome.findings


def read_all(plan: SeriesPlan) -> Any:
    """The table both engines return, after checking they agree row for row."""
    tables = [reader.read(plan) for reader in READERS]
    assert tables[0].schema == tables[1].schema == plan.schema
    assert tables[0].equals(tables[1]), "DuckDB and DataFusion disagree"
    return tables[0]


def rows(table: Any, *names: str) -> list[tuple[Any, ...]]:
    columns = [table.column(n).to_pylist() for n in names]
    return list(zip(*columns, strict=True))


# --- an arm's run recorded as two files, registered as two packages ---------------------------


@pytest.fixture
def arm_run(catalog: PostgresCatalog, tmp_path: Path) -> dict[str, Any]:
    """Shift A of an arm cell, recorded as two MCAP files and ingested apart. Each file is its
    own source, so each package's joint states carry their own clocks (root ADR 0005)."""
    part0 = declared_run(subset(*ARM), "cell-3/shift-a")
    part1 = second_part(part0, "shift-a-part1")
    roots, pairs = {}, []
    for name, rows_, start, n in (("p0", part0, START, 300), ("p1", part1, START + 300_000, 200)):
        stream = stream_of(rows_, "/joint_states")
        made = batch(stream, n, start, 1_000, joint_values(JOINTS))
        pairs.append((write(rows_, tmp_path / name, {"/joint_states": made}), stream.id))
        register(catalog, tmp_path / name)
        roots[name] = tmp_path / name
    return {"part0": part0, "part1": part1, "roots": roots, "pairs": pairs}


def _pairs(series: SeriesCatalog, run: dict[str, Any]) -> list[tuple[str, str]]:
    return list(run["pairs"])


def _package_id(root: Path) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()


def test_a_run_spanning_two_packages_reads_as_one_sorted_table(
    catalog: PostgresCatalog, series: SeriesCatalog, arm_run: dict[str, Any]
) -> None:
    thread = catalog.thread(
        ThreadKey("run", DeclaredKey("manifest", "cell-3/shift-a")), "world", History()
    )
    joint_ids = {one(arm_run[p], "stream", "/joint_states")["id"] for p in ("part0", "part1")}
    pairs = [
        (package, entry.record_id)
        for partition in thread.partitions
        for entry in partition.entries
        if entry.record_id in joint_ids
        for package in entry.packages
    ]
    assert len(pairs) == 2, "the run thread reaches the joint states in both packages"
    selection = series.files(pairs)
    assert selection.findings == ()
    plan = plan_series(selection.files)
    table = read_all(plan)
    assert table.num_rows == 500
    assert plan.columns == (
        "locator/0/length",
        "locator/0/offset",
        "value/position",
        "value/velocity",
    )
    clocks = [stream_of(arm_run[p], "/joint_states").clocks[0] for p in ("part0", "part1")]
    assert clocks[0] != clocks[1], "two files, two log clocks"
    got = rows(table, "clock", "ticks", "seq")
    expected = [(clocks[0], START + 1_000 * i, i) for i in range(300)]
    expected += [(clocks[1], START + 300_000 + 1_000 * i, i) for i in range(200)]
    assert got == expected, "one partition per clock, by registration, each in tick order"
    assert table.column("value/position")[0].as_py() == [float(j) for j in range(JOINTS)]
    assert set(table.column("package_id").to_pylist()) == {p for p, _ in pairs}


def test_a_window_on_each_files_clock_is_pushed_to_the_scan(
    series: SeriesCatalog, arm_run: dict[str, Any]
) -> None:
    files = series.files(_pairs(series, arm_run)).files
    c0, c1 = (f.clocks[0] for f in files)
    windows = [
        TimeWindow(c0, START + 100_000, START + 149_000),
        TimeWindow(c1, START + 300_000, START + 300_000),
    ]
    plan = plan_series(files, windows=windows)
    table = read_all(plan)
    assert rows(table, "clock", "seq") == [(c0, i) for i in range(100, 150)] + [(c1, 0)]
    duck = DuckDBReader().explain(plan)
    for low, high, column in (
        (START + 100_000, START + 149_000, "time/0"),
        (START + 300_000, START + 300_000, "time/0"),
    ):
        assert _duckdb_scan_filter(duck, column, low, high), duck
    fusion = DataFusionReader().explain(plan)
    for low, high in ((START + 100_000, START + 149_000), (START + 300_000, START + 300_000)):
        assert _datafusion_scan_filter(fusion, "time/0", low, high), fusion


def _duckdb_scans(plan: str) -> list[dict[str, Any]]:
    """Every scan operator of DuckDB's JSON plan, with its extra information."""
    found: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        if node["name"] in ("PARQUET_SCAN", "READ_PARQUET", "ARROW_SCAN"):
            found.append(node["extra_info"])
        for child in node["children"]:
            walk(child)

    for root in json.loads(plan):
        walk(root)
    return found


def _duckdb_scan_filter(plan: str, column: str, low: int, high: int) -> bool:
    """A Parquet scan of DuckDB's plan holds the window as its own filter, so no FILTER above
    it reads rows the window excludes."""
    want = f"{column}>={low} AND {column}<={high}"
    return any(scan.get("Filters") == want for scan in _duckdb_scans(plan))


def _datafusion_scan_filter(plan: str, column: str, low: int, high: int) -> bool:
    """A DataFusion Parquet scan carries the window as its predicate, and prunes row groups and
    pages by the column's statistics (its pruning predicate). A point window may be simplified
    to an equality."""
    for line in plan.splitlines():
        if "DataSourceExec" not in line or "file_type=parquet" not in line:
            continue
        predicate = line.partition(" predicate=")[2].partition(", pruning_predicate=")[0]
        pruning = line.partition("pruning_predicate=")[2]
        bounds = {str(low), str(high)}
        if (
            f"{column}@" in predicate
            and all(b in predicate for b in bounds)
            and f"{column}_min@" in pruning
            and f"{column}_max@" in pruning
        ):
            return True
    return False


def test_reads_copy_no_byte_and_scan_the_packages_own_files(
    series: SeriesCatalog, arm_run: dict[str, Any], tmp_path: Path
) -> None:
    before = _tree(tmp_path)
    files = series.files(_pairs(series, arm_run)).files
    plan = plan_series(files, windows=[TimeWindow(files[0].clocks[0], START, START + 5_000)])
    for reader in READERS:
        reader.read(plan)
        reader.explain(plan, analyze=True)
    # DataFusion's plan names each file it scans: the package's own, where it was registered.
    explained = DataFusionReader().explain(plan)
    assert [s.file for s in plan.scans] == [files[0]], "the other file is on another clock"
    assert files[0].location.path.lstrip("/") in explained
    assert _tree(tmp_path) == before, "no file was created, changed or removed"
    for file in files:
        assert file.location.url.startswith(str(tmp_path))
        assert file.location.url.endswith(
            f"series/{file.stream_id.removeprefix('rec:sha256:')}.parquet"
        )


def _tree(root: Path) -> dict[str, tuple[int, int, int]]:
    out = {}
    for path in sorted(root.rglob("*")):
        if "pgdata" in path.parts:
            continue
        info = path.lstat()
        out[str(path)] = (info.st_size, info.st_mtime_ns, info.st_ino)
    return out


# --- a mobile base: two streams of one bag share its log clock --------------------------------


@pytest.fixture
def mobile(catalog: PostgresCatalog, tmp_path: Path) -> dict[str, Any]:
    rows_ = subset(*MOBILE)
    odom, power = stream_of(rows_, "/wheel_odom"), stream_of(rows_, "/battery")
    write(
        rows_,
        tmp_path / "base",
        {
            "/wheel_odom": batch(odom, 40, START, 100, odometry, unknown_last=5),
            "/battery": batch(power, 4, START + 50, 1_000, battery),
        },
    )
    register(catalog, tmp_path / "base")
    package = _package_id(tmp_path / "base")
    return {"rows": rows_, "package": package, "odom": odom, "power": power}


def test_streams_sharing_a_clock_interleave_by_ticks(
    series: SeriesCatalog, mobile: dict[str, Any]
) -> None:
    pairs = [(mobile["package"], mobile["odom"].id), (mobile["package"], mobile["power"].id)]
    files = series.files(pairs).files
    plan = plan_series(files)
    assert plan.columns == ("locator/0/length", "locator/0/offset"), "only shared columns"
    assert {s.partition for s in plan.scans} == {0}, "one clock, one partition"
    table = read_all(plan)
    got = rows(table, "ticks", "stream_id")
    assert got == sorted(got, key=lambda r: (r[0], r[1].encode()))
    power_at = [t for t, s in got if s == mobile["power"].id]
    assert power_at == [START + 50 + 1_000 * i for i in range(4)]
    assert got.index((START + 50, mobile["power"].id)) == 1, "between odometry rows 0 and 1"


def test_a_window_on_a_second_clock_reads_only_streams_that_carry_it(
    series: SeriesCatalog, mobile: dict[str, Any]
) -> None:
    pairs = [(mobile["package"], mobile["odom"].id), (mobile["package"], mobile["power"].id)]
    files = series.files(pairs).files
    stamp = mobile["odom"].clocks[1]
    plan = plan_series(
        files, windows=[TimeWindow(stamp, INT64_MIN, INT64_MAX)], columns=["value/linear_x"]
    )
    assert [(f.code, f.subject) for f in plan.findings] == [("unknown_clock", mobile["power"].id)]
    table = read_all(plan)
    assert table.num_rows == 35, "the five rows whose header stamp is unknown never match"
    assert set(table.column("clock").to_pylist()) == {stamp}
    assert table.column("ticks").to_pylist() == [START + 100 * i + 1 for i in range(35)]
    assert set(table.column("ticks_state").to_pylist()) == {"known"}
    full = read_all(plan_series(files[:1]))
    assert full.num_rows == 40 and full.column("ticks").null_count == 0, "clock 0 is known"
    assert "state/time/1" not in full.schema.names, "another clock's state is never returned"


def test_window_boundaries_are_inclusive_and_an_empty_window_is_an_empty_table(
    series: SeriesCatalog, mobile: dict[str, Any]
) -> None:
    (file,) = series.files([(mobile["package"], mobile["odom"].id)]).files
    clock = file.clocks[0]
    point = read_all(plan_series([file], windows=[TimeWindow(clock, START + 300, START + 300)]))
    assert rows(point, "seq") == [(3,)]
    edge = read_all(plan_series([file], windows=[TimeWindow(clock, START + 301, START + 399)]))
    assert edge.num_rows == 0
    assert edge.schema.names[:6] == [
        "package_id",
        "stream_id",
        "clock",
        "ticks",
        "ticks_state",
        "seq",
    ]
    nothing = plan_series([], windows=[TimeWindow(clock, 0, 1)])
    assert all(r.read(nothing).num_rows == 0 for r in READERS)
    assert all(r.explain(nothing) == "" for r in READERS)


@pytest.mark.parametrize(
    ("windows", "columns", "message"),
    [
        ([TimeWindow("rec:sha256:" + "1" * 64, 5, 4)], None, "is after"),
        ([TimeWindow("rec:sha256:" + "1" * 64, 0, 1)] * 2, None, "two windows"),
        ([TimeWindow("clock", 0, 1)], None, "record id"),
        ([TimeWindow("rec:sha256:" + "1" * 64, 0, INT64_MAX + 1)], None, "int64"),
        ([TimeWindow("rec:sha256:" + "1" * 64, True, 1)], None, "int64"),
        (None, ["time/1"], "only value/"),
        (None, ["state/time/1"], "only value/"),
        ([], None, "at least one window"),
        (None, ["value/linear_x", "value/linear_x"], "distinct"),
        (None, "value/linear_x", "distinct"),
        (None, ["value/data"], "not in every file"),
    ],
)
def test_requests_outside_the_contract_are_refused(
    series: SeriesCatalog, mobile: dict[str, Any], windows: Any, columns: Any, message: str
) -> None:
    files = series.files([(mobile["package"], mobile["odom"].id)]).files
    with pytest.raises(LakeRequestError, match=message):
        plan_series(files, windows=windows, columns=columns)


def test_a_stream_carrying_two_window_clocks_is_refused(
    series: SeriesCatalog, mobile: dict[str, Any]
) -> None:
    (file,) = series.files([(mobile["package"], mobile["odom"].id)]).files
    windows = [TimeWindow(file.clocks[0], 0, 1), TimeWindow(file.clocks[1], 0, 1)]
    with pytest.raises(LakeRequestError, match="two window clocks"):
        plan_series([file], windows=windows)


def test_reads_are_deterministic_whatever_order_files_are_given_in(
    series: SeriesCatalog, mobile: dict[str, Any], arm_run: dict[str, Any]
) -> None:
    pairs = [
        (mobile["package"], mobile["odom"].id),
        (mobile["package"], mobile["power"].id),
        *_pairs(series, arm_run),
    ]
    first = read_all(plan_series(series.files(pairs).files, columns=["locator/0/offset"]))
    again = read_all(plan_series(series.files(pairs[::-1]).files, columns=["locator/0/offset"]))
    assert first.equals(again)
    # Partitions follow registration: the arm's packages were registered after the base's.
    assert first.column("package_id")[0].as_py() == mobile["package"]


# --- a legged robot's stream thread: lineage siblings and current views ----------------------


def test_a_stream_thread_reads_each_lineage_on_its_own_clocks(
    catalog: PostgresCatalog, series: SeriesCatalog, tmp_path: Path
) -> None:
    v1 = subset(*LEGGED)
    v2 = reparsed(v1, "2.0.0")
    for name, rows_ in (("v1", v1), ("v2", v2)):
        stream = stream_of(rows_, "/joint_states")
        write(
            rows_,
            tmp_path / name,
            {"/joint_states": batch(stream, 30, START, 2_500, joint_values(12))},
        )
        register(catalog, tmp_path / name)
    record = one(v1, "stream", "/joint_states")
    evidence = record["provenance"]["evidence"]
    key = ThreadKey("stream", EvidenceAnchor(evidence["source"], tuple(evidence["locator"])))
    history = series.of_thread(catalog.thread(key, "world", History()))
    assert history.findings == () and len(history.files) == 2
    plan = plan_series(history.files)
    assert sorted(s.partition for s in plan.scans) == [0, 1], "v1 and v2 never interleave"
    table = read_all(plan)
    assert table.num_rows == 60
    assert table.column("value/position").type == pa.list_(
        pa.field("item", pa.float64(), nullable=False)
    )
    latest = series.of_thread(catalog.thread(key, "world", LatestTransform()))
    (file,) = latest.files
    assert file.stream_id == one(v2, "stream", "/joint_states")["id"]


# --- one stream in two packages ---------------------------------------------------------------


def _with_sites(rows_: list[Record]) -> list[Record]:
    """The bag's records plus the site table's: a second package holding the same stream."""
    sites = subset("mobile_robot", "csv")
    have = {(r["kind"], r.get("id", r.get("content_id"))) for r in rows_}
    return rows_ + [r for r in sites if (r["kind"], r.get("id", r.get("content_id"))) not in have]


def test_one_stream_in_two_packages_is_read_once(
    catalog: PostgresCatalog, series: SeriesCatalog, mobile: dict[str, Any], tmp_path: Path
) -> None:
    rows_ = _with_sites(mobile["rows"])
    odom = mobile["odom"]
    write(
        rows_,
        tmp_path / "again",
        {
            "/wheel_odom": batch(odom, 40, START, 100, odometry, unknown_last=5),
            "/battery": batch(mobile["power"], 4, START + 50, 1_000, battery),
        },
    )
    register(catalog, tmp_path / "again")
    again = _package_id(tmp_path / "again")
    selection = series.files([(again, odom.id), (mobile["package"], odom.id)])
    (file,) = selection.files
    assert selection.findings == ()
    assert (file.package_id, file.also_in) == (mobile["package"], (again,)), "first registration"
    assert read_all(plan_series([file])).num_rows == 40


def test_one_stream_with_two_different_series_files_is_not_read(
    catalog: PostgresCatalog, series: SeriesCatalog, mobile: dict[str, Any], tmp_path: Path
) -> None:
    rows_ = _with_sites(mobile["rows"])
    odom = mobile["odom"]
    write(rows_, tmp_path / "other", {"/wheel_odom": batch(odom, 41, START, 100, odometry)})
    register(catalog, tmp_path / "other")
    other = _package_id(tmp_path / "other")
    selection = series.files([(mobile["package"], odom.id), (other, odom.id)])
    assert selection.files == ()
    assert [(f.code, f.subject) for f in selection.findings] == [("conflicting_id", odom.id)]


# --- hostile and missing packages -------------------------------------------------------------


def test_pairs_the_catalog_does_not_hold_are_findings(
    series: SeriesCatalog, mobile: dict[str, Any]
) -> None:
    other = "rec:sha256:" + "5" * 64
    selection = series.files(
        [
            (mobile["package"], other),
            ("sha256:" + "6" * 64, mobile["odom"].id),
            ("not-a-package", mobile["odom"].id),
            (mobile["package"], "rec:sha256:XYZ"),
        ]
    )
    assert selection.files == ()
    assert [f.code for f in selection.findings] == [
        "invalid_request",
        "invalid_request",
        "unknown_record",
        "unknown_record",
    ]
    assert series.files([]).files == ()


def test_a_records_only_stream_has_no_series_file(
    catalog: PostgresCatalog, series: SeriesCatalog, tmp_path: Path
) -> None:
    rows_ = subset(*MOBILE)
    odom = stream_of(rows_, "/wheel_odom")
    write(
        rows_,
        tmp_path / "bare",
        {"/wheel_odom": batch(odom, 3, START, 100, odometry)},
        every_stream=False,
    )
    register(catalog, tmp_path / "bare")
    package = _package_id(tmp_path / "bare")
    power = stream_of(rows_, "/battery")
    selection = series.files([(package, odom.id), (package, power.id)])
    assert [f.stream_id for f in selection.files] == [odom.id]
    assert [(f.code, f.subject) for f in selection.findings] == [("file_missing", power.id)]


def _damage(root: Path, stream_id: str, how: str) -> None:
    path = root / "series" / f"{stream_id.removeprefix('rec:sha256:')}.parquet"
    if how == "truncated":
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])
    elif how == "link":
        target = root.parent / "elsewhere.parquet"
        shutil.copyfile(path, target)
        path.unlink()
        path.symlink_to(target)
    elif how == "removed":
        path.unlink()
    elif how == "manifest":
        (root / "manifest.json").write_bytes((root / "manifest.json").read_bytes() + b" ")
    elif how == "moved":
        root.rename(root.parent / "moved")
    elif how == "root_link":
        root.rename(root.parent / "real")
        root.symlink_to(root.parent / "real")


@pytest.mark.parametrize(
    ("how", "code"),
    [
        ("truncated", "file_digest_mismatch"),
        ("link", "file_missing"),
        ("removed", "file_missing"),
        ("manifest", "manifest_digest_mismatch"),
        ("moved", "package_unreadable"),
        ("root_link", "package_unreadable"),
    ],
)
def test_a_package_changed_since_registration_is_a_finding(
    series: SeriesCatalog, mobile: dict[str, Any], tmp_path: Path, how: str, code: str
) -> None:
    _damage(tmp_path / "base", mobile["odom"].id, how)
    selection = series.files([(mobile["package"], mobile["odom"].id)])
    assert selection.files == ()
    assert [f.code for f in selection.findings] == [code]


def test_a_root_engines_would_read_as_a_glob_is_refused(
    catalog: PostgresCatalog, series: SeriesCatalog, tmp_path: Path
) -> None:
    rows_ = subset(*MOBILE)
    odom = stream_of(rows_, "/wheel_odom")
    root = tmp_path / "runs[1]"
    write(rows_, root, {"/wheel_odom": batch(odom, 3, START, 100, odometry)})
    register(catalog, root)
    selection = series.files([(_package_id(root), odom.id)])
    assert [f.code for f in selection.findings] == ["unsafe_entry"]


def test_a_root_with_a_space_reads(
    catalog: PostgresCatalog, series: SeriesCatalog, tmp_path: Path
) -> None:
    rows_ = subset(*MOBILE)
    odom = stream_of(rows_, "/wheel_odom")
    root = tmp_path / "base 2"
    write(rows_, root, {"/wheel_odom": batch(odom, 3, START, 100, odometry)})
    register(catalog, root)
    (file,) = series.files([(_package_id(root), odom.id)]).files
    assert read_all(plan_series([file])).num_rows == 3


def test_a_file_that_is_no_longer_parquet_is_a_finding_at_planning(
    series: SeriesCatalog, mobile: dict[str, Any], tmp_path: Path
) -> None:
    (file,) = series.files([(mobile["package"], mobile["odom"].id)]).files
    path = Path(file.location.path)
    path.write_bytes(b"\0" * file.size)  # same size, so only the footer read notices
    plan = plan_series([file])
    assert plan.scans == () and [f.code for f in plan.findings] == ["file_digest_mismatch"]


# --- the local store --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["", "/abs", "a//b", "../x", "a/../b", "./a", "a\\b", "a\x00b", "a/"]
)
def test_store_keys_stay_inside_the_package(tmp_path: Path, key: str) -> None:
    store = LocalObjectStore(tmp_path)
    with pytest.raises(StoreError):
        store.location(key)


def test_a_local_store_reads_bounded_and_never_through_a_link(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"12345")
    (tmp_path / "l").symlink_to(tmp_path / "a")
    store = LocalObjectStore(tmp_path)
    assert store.read("a", 5) == b"12345" and store.read("a", 4) is None
    assert store.size("a") == 5
    assert store.read("l", 10) is None and store.size("l") is None
    assert store.read("missing", 10) is None
    with pytest.raises(StoreError):
        LocalObjectStore("relative/root")


def test_series_files_say_which_column_holds_each_clock(
    series: SeriesCatalog, mobile: dict[str, Any]
) -> None:
    (file,) = series.files([(mobile["package"], mobile["odom"].id)]).files
    assert isinstance(file, SeriesFile)
    assert [file.time_column(c) for c in mobile["odom"].clocks] == ["time/0", "time/1"]
    assert file.time_column("rec:sha256:" + "7" * 64) is None
    assert file.settings["row_group_rows"] == 65_536


def test_unknown_ticks_sort_last_with_their_state(
    catalog: PostgresCatalog, series: SeriesCatalog, tmp_path: Path
) -> None:
    """A base whose log clock was not read for its last three odometry rows (a wrapped clock 0),
    beside its battery, whose clock 0 has no state column: every row is known there."""
    rows_ = subset(*MOBILE)
    odom, power = stream_of(rows_, "/wheel_odom"), stream_of(rows_, "/battery")
    package = write(
        rows_,
        tmp_path / "gaps",
        {
            "/wheel_odom": batch(odom, 6, START, 100, odometry, unknown_last=3, unknown_clock=0),
            "/battery": batch(power, 2, START + 50, 100, battery),
        },
    )
    register(catalog, tmp_path / "gaps")
    files = series.files([(package, odom.id), (package, power.id)]).files
    table = read_all(plan_series(files, columns=[]))
    got = rows(table, "ticks", "ticks_state", "stream_id")
    assert got == [
        (START, "known", odom.id),
        (START + 50, "known", power.id),
        (START + 100, "known", odom.id),
        (START + 150, "known", power.id),
        (START + 200, "known", odom.id),
        (None, "unknown", odom.id),
        (None, "unknown", odom.id),
        (None, "unknown", odom.id),
    ]
    windowed = read_all(plan_series(files, windows=[TimeWindow(odom.clocks[0], 0, INT64_MAX)]))
    assert windowed.num_rows == 5, "a window never matches an unknown tick"


def test_duckdb_refuses_columns_that_differ_only_in_case(
    catalog: PostgresCatalog, series: SeriesCatalog, tmp_path: Path
) -> None:
    """DuckDB matches identifiers without case; DataFusion reads such a file as written."""
    rows_ = subset(*MOBILE)
    odom = stream_of(rows_, "/wheel_odom")

    def twins(n: int) -> dict[str, Any]:
        from neptune.model.series import ColumnType, SeriesColumn

        return {
            "value/X": SeriesColumn("value/X", ColumnType.INT64, (10,) * n),
            "value/x": SeriesColumn("value/x", ColumnType.INT64, (30,) * n),
        }

    package = write(rows_, tmp_path / "twins", {"/wheel_odom": batch(odom, 2, START, 100, twins)})
    register(catalog, tmp_path / "twins")
    plan = plan_series(series.files([(package, odom.id)]).files, columns=["value/x"])
    with pytest.raises(LakeRequestError, match="only in case"):
        DuckDBReader().read(plan)
    assert DataFusionReader().read(plan).column("value/x").to_pylist() == [30, 30]


def test_a_link_swapped_in_after_resolution_is_caught_before_the_scan(
    series: SeriesCatalog, mobile: dict[str, Any], tmp_path: Path
) -> None:
    (file,) = series.files([(mobile["package"], mobile["odom"].id)]).files
    _damage(tmp_path / "base", mobile["odom"].id, "link")
    plan = plan_series([file])
    assert plan.scans == () and [f.code for f in plan.findings] == ["file_missing"]


def test_a_parquet_file_without_the_scanned_columns_is_a_finding(
    series: SeriesCatalog, mobile: dict[str, Any], tmp_path: Path
) -> None:
    """A plain Parquet file in a series file's place, as a ``SeriesFile`` naming it would see."""
    import dataclasses

    import pyarrow.parquet as pq

    (file,) = series.files([(mobile["package"], mobile["odom"].id)]).files
    other = tmp_path / "plain.parquet"
    pq.write_table(pa.table({"time/0": pa.array(["not ticks"])}), other)
    location = dataclasses.replace(file.location, url=str(other))
    swapped = dataclasses.replace(file, location=location, size=other.stat().st_size)
    plan = plan_series([swapped])
    assert plan.scans == () and [f.code for f in plan.findings] == ["file_digest_mismatch"]


@pytest.mark.parametrize("reader", READERS, ids=lambda r: r.name)
def test_a_file_whose_pages_do_not_decode_costs_only_its_own_rows(
    series: SeriesCatalog,
    mobile: dict[str, Any],
    arm_run: dict[str, Any],
    tmp_path: Path,
    reader: SeriesReader,
) -> None:
    """Pages damaged in place at the same size: the footer still parses, so planning passes,
    and only the engine notices. The base's odometry becomes a finding; the arm's rows from
    another package, and the base's battery, still come back."""
    odom, power = mobile["odom"], mobile["power"]
    arm = arm_run["pairs"][0]
    pairs = [(mobile["package"], odom.id), (mobile["package"], power.id), arm]
    files = series.files(pairs).files
    path = tmp_path / "base" / "series" / f"{odom.id.removeprefix('rec:sha256:')}.parquet"
    data = bytearray(path.read_bytes())
    data[8:200] = bytes(b ^ 0xFF for b in data[8:200])
    path.write_bytes(bytes(data))
    plan = plan_series(files, columns=["locator/0/offset"])
    assert plan.findings == () and len(plan.scans) == 3, "planning cannot see page damage"
    table = reader.read(plan)
    findings = read_findings(table)
    assert [(f.code, f.subject) for f in findings] == [("file_digest_mismatch", odom.id)]
    assert set(table.column("stream_id").to_pylist()) == {power.id, arm[1]}
    good = reader.read(
        plan_series([f for f in files if f.stream_id != odom.id], columns=["locator/0/offset"])
    )
    assert table.equals(good) and read_findings(good) == ()


def test_every_read_carries_its_plans_findings(
    series: SeriesCatalog, mobile: dict[str, Any]
) -> None:
    files = series.files(
        [(mobile["package"], mobile["odom"].id), (mobile["package"], mobile["power"].id)]
    ).files
    plan = plan_series(files, windows=[TimeWindow(mobile["odom"].clocks[1], 0, INT64_MAX)])
    for reader in READERS:
        assert read_findings(reader.read(plan)) == plan.findings != ()
    with pytest.raises(LakeRequestError, match="not a series read"):
        read_findings(pa.table({"x": [1]}))


# --- the budget: a 10^6-row window under 200 ms ----------------------------------------------

BUDGET_ROWS = 1_000_000
BUDGET_SECONDS = 0.2


@pytest.mark.slow
def test_a_million_row_window_reads_under_200_ms(
    catalog: PostgresCatalog, series: SeriesCatalog, tmp_path: Path
) -> None:
    """MVL-95's acceptance: a mobile base's wheel odometry at 100 Hz for 3 h 20 min (1.2 M rows,
    19 row groups), a window of exactly 10^6 of them. Timed end to end per read: resolving the
    file from the catalog and the manifest, planning (one footer read), and the engine's scan
    into one Arrow table. The median of five reads after a warm-up must hold the budget."""
    from time import perf_counter

    rows_ = subset(*MOBILE)
    odom = stream_of(rows_, "/wheel_odom")
    step = 10_000_000  # 100 Hz in ns
    package = write(
        rows_, tmp_path / "long", {"/wheel_odom": batch(odom, 1_200_000, START, step, odometry)}
    )
    register(catalog, tmp_path / "long")
    first = START + 100_000 * step
    window = TimeWindow(odom.clocks[0], first, first + (BUDGET_ROWS - 1) * step)
    pairs = [(package, odom.id)]
    timings = {}
    for reader in READERS:
        laps = []
        for _ in range(6):
            began = perf_counter()
            plan = plan_series(series.files(pairs).files, windows=[window])
            table = reader.read(plan)
            laps.append(perf_counter() - began)
            assert table.num_rows == BUDGET_ROWS
        timings[reader.name] = sorted(laps[1:])[2]
    assert table.column("seq")[0].as_py() == 100_000
    assert all(t < BUDGET_SECONDS for t in timings.values()), timings
    analyzed = DataFusionReader().explain(plan, analyze=True)
    pruned = re.search(r"row_groups_pruned_statistics=(\d+)", analyzed)
    assert pruned and int(pruned.group(1)) >= 1, "row groups outside the window are not read"

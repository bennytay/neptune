"""The derived time and spatial indexes (MVL-97, Ledger ADR 0015).

Time: the drone's flight log (boot, sample and GPS clocks) and the mobile base's bag (log and
header clocks) are registered with series rows; a window on one clock lists what exists there and
never an interval of another clock, unless the request names that clock and the ClockMapping that
relates them. Space: the quadruped's URDF frames and the warehouse site map's docks are indexed in
the frame or CRS they declare, and a box query names its reference and unit. Both indexes are
written at registration and reproduced by a rebuild.
"""

import os
import random
import shutil
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from conftest import new_database
from ledger_index_packages import (
    ACCEL_START,
    BOOT,
    CRS84,
    GPS,
    GPS_BOOT_START,
    GPS_UTC_START,
    SAMPLE,
    alignment_package,
    child_frame,
    drone,
    drone_series,
    frame_graph,
    gps_to_boot,
    one_of,
    urdf,
    vector_map,
    warehouse,
)
from ledger_series_packages import MOBILE, batch, battery, odometry, stream_of, write
from ledger_thread_packages import Record, files, resourced, subset
from neptune.model.time import INT64_MAX, INT64_MIN
from neptune.store.package import series_path, write_package
from neptune_ledger.api import CatalogUnavailable
from neptune_ledger.api.types import CrsReference, FrameReference, TimeWindow
from neptune_ledger.catalog.check import check_package, open_root
from neptune_ledger.catalog.manifest import Manifest
from neptune_ledger.catalog.migrate import apply_migrations
from neptune_ledger.catalog.rebuild import rebuild
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.lake.indexes import MAX_CLOCKS, MAX_ENTRIES_LIMIT, IndexCatalog
from neptune_ledger.lake.space_index import (
    SpatialBox,
    SpatialResult,
)
from neptune_ledger.lake.time_index import IntervalEntry, WindowResult, place, series_intervals
from neptune_ledger.threads.merge import ClockMapping, IntervalMapper
from test_ledger_registration import dump as dump_tables
from test_ledger_registration import fresh

Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
def catalog(pg_uri: str) -> Iterator[PostgresCatalog]:
    with fresh(pg_uri) as made:
        yield made


@pytest.fixture
def index(pg_uri: str, catalog: PostgresCatalog) -> Iterator[IndexCatalog]:
    with IndexCatalog(pg_uri, "acme") as made:
        yield made


def register(catalog: PostgresCatalog, root: Path) -> str:
    outcome = catalog.register(root)
    assert outcome.outcome == "registered", outcome.findings
    return str(outcome.package_id.value)  # type: ignore[union-attr]


def stream_id(rows: list[Record], topic: str) -> str:
    return str(
        one_of(rows, "stream", lambda r: r["topic"].get("value") == topic)["id"],
    )


def summary(result: WindowResult) -> list[tuple[str, str, str, int, int | None]]:
    return [(e.subject, e.record_id, e.clock, e.first, e.last) for e in result.entries]


def codes(result: WindowResult | SpatialResult) -> list[tuple[str, str]]:
    return [(f.code, f.subject) for f in result.findings]


# --- time: the drone across boot, sample and GPS clocks ------------------------------------------


@pytest.fixture
def flight(catalog: PostgresCatalog, tmp_path: Path) -> dict[str, Any]:
    rows = drone()
    package = write(rows, tmp_path / "drone", drone_series(rows))
    register(catalog, tmp_path / "drone")
    transform = one_of(rows, "transform_record", lambda r: True)
    validity = (GPS_UTC_START - 1_000_000, GPS_UTC_START + 10_000_000)
    sync = alignment_package(transform, b"gps,boot\n", gps_to_boot(validity))
    write_package(tmp_path / "sync", files(sync))
    register(catalog, tmp_path / "sync")
    return {
        "rows": rows,
        "package": package,
        "run": one_of(rows, "run", lambda r: True)["id"],
        "accel": stream_id(rows, "sensor_accel"),
        "gps": stream_id(rows, "vehicle_gps_position"),
        "mapping": one_of(sync, "clock_mapping", lambda r: True)["id"],
    }


def test_a_window_on_the_boot_clock_lists_what_exists_there(
    index: IndexCatalog, flight: dict[str, Any]
) -> None:
    result = index.window(TimeWindow(BOOT, ACCEL_START, ACCEL_START + 200_000))
    assert result.outcome == "answered" and result.findings == ()
    assert summary(result) == [
        ("record", flight["run"], BOOT, ACCEL_START, None),
        ("series", flight["accel"], BOOT, ACCEL_START, ACCEL_START + 499 * 4_000),
        ("series", flight["gps"], BOOT, GPS_BOOT_START, GPS_BOOT_START + 49 * 40_000),
    ]
    accel = result.entries[1]
    assert (accel.rows_known, accel.rows_unknown, accel.package_id) == (500, 0, flight["package"])
    assert all(e.mapped is None for e in result.entries), "nothing was carried across clocks"
    assert result.as_of.value.tx_seq == 2  # type: ignore[union-attr]


def test_each_clock_is_its_own_index_and_gps_time_is_never_compared_with_boot_time(
    index: IndexCatalog, flight: dict[str, Any]
) -> None:
    gps = index.window(TimeWindow(GPS, GPS_UTC_START, GPS_UTC_START))
    assert summary(gps) == [
        ("series", flight["gps"], GPS, GPS_UTC_START, GPS_UTC_START + 49 * 40_000)
    ]
    sample = index.window(TimeWindow(SAMPLE, INT64_MIN, INT64_MAX))
    assert [e.record_id for e in sample.entries] == [flight["accel"]]
    # Boot ticks numerically inside a window on another clock are not on that clock.
    assert index.window(TimeWindow(GPS, 0, GPS_UTC_START - 1)).entries == ()


def test_naming_the_gps_clock_without_a_mapping_is_refused(
    index: IndexCatalog, flight: dict[str, Any]
) -> None:
    result = index.window(TimeWindow(BOOT, ACCEL_START, ACCEL_START + 200_000), clocks=[GPS])
    assert result.outcome == "refused" and result.entries == ()
    assert codes(result) == [("invalid_request", GPS)]
    assert "never compared" in result.findings[0].detail


def test_a_named_mapping_carries_gps_intervals_onto_the_boot_clock(
    index: IndexCatalog, flight: dict[str, Any]
) -> None:
    window = TimeWindow(BOOT, GPS_BOOT_START + 1_000_000, GPS_BOOT_START + 1_000_000)
    result = index.window(window, clocks=[GPS], mappings=[flight["mapping"]])
    assert result.outcome == "answered" and result.findings == ()
    assert (result.clocks, result.mappings) == ((GPS,), (flight["mapping"],))
    by_clock = {(e.record_id, e.clock): e for e in result.entries}
    assert set(by_clock) == {(flight["accel"], BOOT), (flight["gps"], BOOT), (flight["gps"], GPS)}
    mapped = by_clock[flight["gps"], GPS].mapped
    assert mapped is not None
    assert (mapped.reference_clock, mapped.path) == (BOOT, (flight["mapping"],))
    assert (mapped.lo, mapped.hi) == (GPS_BOOT_START - 2, GPS_BOOT_START + 49 * 40_000 + 2)
    native = by_clock[flight["gps"], GPS]
    assert (native.first, native.last) == (GPS_UTC_START, GPS_UTC_START + 49 * 40_000), "as stated"
    # Merged order: by the interval on the window's clock, then clock id bytes.
    keys = [(e.mapped.lo if e.mapped else e.first) for e in result.entries]
    assert keys == sorted(keys)


def test_an_interval_outside_the_mappings_validity_is_reported_not_placed(
    catalog: PostgresCatalog, index: IndexCatalog, flight: dict[str, Any], tmp_path: Path
) -> None:
    transform = one_of(flight["rows"], "transform_record", lambda r: True)
    short = (GPS_UTC_START, GPS_UTC_START + 1_000)  # holds the first GPS row only
    sync = alignment_package(transform, b"short\n", gps_to_boot(short))
    write_package(tmp_path / "short", files(sync))
    register(catalog, tmp_path / "short")
    mapping = one_of(sync, "clock_mapping", lambda r: True)["id"]
    result = index.window(TimeWindow(BOOT, INT64_MIN, INT64_MAX), clocks=[GPS], mappings=[mapping])
    assert result.outcome == "answered"
    assert codes(result) == [("mapping_out_of_range", flight["gps"])]
    assert result.findings[0].paths_tried is not None
    assert all(e.clock == BOOT for e in result.entries), "never placed by a guess"


def test_a_mapping_that_does_not_reach_a_named_clock_is_refused(
    index: IndexCatalog, flight: dict[str, Any]
) -> None:
    result = index.window(
        TimeWindow(BOOT, 0, 1), clocks=[SAMPLE, GPS], mappings=[flight["mapping"]]
    )
    assert result.outcome == "refused"
    assert codes(result) == [("invalid_request", SAMPLE)]


def test_unknown_clocks_and_mappings_are_refused(
    index: IndexCatalog, flight: dict[str, Any]
) -> None:
    nowhere = "rec:sha256:" + "e" * 64
    result = index.window(TimeWindow(nowhere, 0, 1))
    assert (result.outcome, codes(result)) == ("refused", [("unknown_clock", nowhere)])
    result = index.window(TimeWindow(BOOT, 0, 1), clocks=[GPS], mappings=[flight["run"]])
    assert codes(result) == [("unknown_mapping", flight["run"])], "a run is not a mapping"
    before = index.window(
        TimeWindow(BOOT, 0, 1), clocks=[GPS], mappings=[flight["mapping"]], as_of=1
    )
    assert codes(before) == [("unknown_mapping", flight["mapping"])], "registered at tx 2"


def test_a_window_reads_the_index_at_a_catalog_point(
    catalog: PostgresCatalog, index: IndexCatalog, flight: dict[str, Any], tmp_path: Path
) -> None:
    """A second flight is a second source: its boot clock is another clock, registered later."""
    later = resourced(flight["rows"], b"second flight\n", {"kind": "local", "path": "f2.ulg"})
    write(later, tmp_path / "later", drone_series(later))
    before = index.window(TimeWindow(BOOT, INT64_MIN, INT64_MAX))
    register(catalog, tmp_path / "later")
    boot2 = one_of(later, "run", lambda r: True)["first"]["value"]["domain_id"]
    assert boot2 != BOOT
    after = index.window(TimeWindow(BOOT, INT64_MIN, INT64_MAX))
    assert after.entries == before.entries, "the second flight's ticks are not on BOOT"
    now = index.window(TimeWindow(boot2, INT64_MIN, INT64_MAX))
    assert len(now.entries) == 3 and now.as_of.value.tx_seq == 3  # type: ignore[union-attr]
    then = index.window(TimeWindow(boot2, INT64_MIN, INT64_MAX), as_of=2)
    assert codes(then) == [("unknown_clock", boot2)]
    assert then.as_of.value.tx_seq == 2  # type: ignore[union-attr]
    beyond = index.window(TimeWindow(BOOT, 0, 1), as_of=99)
    assert codes(beyond) == [("as_of_out_of_range", "99")]


# --- time: a mobile base, unknown header stamps ---------------------------------------------------


def test_series_rows_without_a_known_tick_are_counted_not_dropped(
    catalog: PostgresCatalog, index: IndexCatalog, tmp_path: Path
) -> None:
    rows = subset(*MOBILE)
    odom, power = stream_of(rows, "/wheel_odom"), stream_of(rows, "/battery")
    start = 1_790_762_401_000_000_000
    batches = {
        "/wheel_odom": batch(odom, 100, start, 10_000_000, odometry, unknown_last=7),
        "/battery": batch(power, 10, start + 5, 100_000_000, battery),
    }
    write(rows, tmp_path / "mobile", batches)
    register(catalog, tmp_path / "mobile")
    log, header = odom.clocks
    on_log = index.window(TimeWindow(log, start, start))
    assert [(e.subject, e.record_id) for e in on_log.entries if e.subject == "series"] == [
        ("series", odom.id)
    ], "the battery starts 5 ns later"
    both = index.window(TimeWindow(log, start, start + 5))
    assert {e.record_id for e in both.entries if e.subject == "series"} == {odom.id, power.id}
    every = index.window(TimeWindow(header, INT64_MIN, INT64_MAX)).entries
    (on_header,) = [e for e in every if e.subject == "series"]
    assert (on_header.rows_known, on_header.rows_unknown) == (93, 7)
    assert (on_header.first, on_header.last) == (start + 1, start + 92 * 10_000_000 + 1)


# --- time: boundaries --------------------------------------------------------------------------


def test_window_ends_are_inclusive(index: IndexCatalog, flight: dict[str, Any]) -> None:
    last = ACCEL_START + 499 * 4_000

    def ids(first: int, end: int) -> set[str]:
        found = index.window(TimeWindow(SAMPLE, first, end)).entries
        return {e.record_id for e in found}

    assert ids(last - 150, last - 150) == {flight["accel"]}
    assert ids(last - 149, INT64_MAX) == set()
    assert ids(INT64_MIN, ACCEL_START - 150) == {flight["accel"]}
    assert ids(INT64_MIN, ACCEL_START - 151) == set()
    # An open-ended run is its start: a window after it does not meet it.
    after = index.window(TimeWindow(BOOT, ACCEL_START + 1, ACCEL_START + 1)).entries
    assert flight["run"] not in {e.record_id for e in after}


def test_too_many_intervals_are_refused_never_cut_short(
    index: IndexCatalog, flight: dict[str, Any]
) -> None:
    window = TimeWindow(BOOT, INT64_MIN, INT64_MAX)
    assert len(index.window(window, max_entries=3).entries) == 3
    refused = index.window(window, max_entries=2)
    assert refused.outcome == "refused" and refused.entries == ()
    assert codes(refused) == [("invalid_request", BOOT)]
    # The bound is on the whole request: three on BOOT and one on GPS are four.
    named: dict[str, Any] = {"clocks": [GPS], "mappings": [flight["mapping"]]}
    assert len(index.window(window, max_entries=4, **named).entries) == 4
    assert codes(index.window(window, max_entries=3, **named)) == [("invalid_request", GPS)]


# --- time: requests outside the contract -------------------------------------------------------


@pytest.mark.parametrize(
    ("window", "kwargs", "subjects"),
    [
        (TimeWindow(BOOT, 2, 1), {}, ["window"]),
        (TimeWindow("boot", 0, 1), {}, ["window"]),
        (TimeWindow(BOOT, 0, 2**63), {}, ["window"]),
        ((BOOT, 0, 1), {}, ["window"]),
        (TimeWindow(BOOT, 0, 1), {"clocks": GPS}, ["clocks"]),
        (TimeWindow(BOOT, 0, 1), {"clocks": ["gps"]}, ["clocks"]),
        (TimeWindow(BOOT, 0, 1), {"clocks": [[GPS]]}, ["clocks"]),
        (TimeWindow(BOOT, 0, 1), {"clocks": [GPS] * (MAX_CLOCKS + 1)}, ["clocks"]),
        (TimeWindow(BOOT, 0, 1), {"mappings": [None]}, ["mappings"]),
        (TimeWindow(BOOT, 0, 1), {"as_of": 0}, ["0"]),
        (TimeWindow(BOOT, 0, 1), {"as_of": True}, ["True"]),
        (TimeWindow(BOOT, 0, 1), {"max_entries": 0}, ["max_entries"]),
        (TimeWindow(BOOT, 0, 1), {"max_entries": MAX_ENTRIES_LIMIT + 1}, ["max_entries"]),
    ],
)
def test_a_malformed_window_request_is_refused(
    index: IndexCatalog,
    flight: dict[str, Any],
    window: Any,
    kwargs: dict[str, Any],
    subjects: list[str],
) -> None:
    result = index.window(window, **kwargs)
    assert result.outcome == "refused" and result.entries == ()
    assert [f.subject for f in result.findings] == subjects
    assert {f.code for f in result.findings} == {"invalid_request"}


def test_the_window_clock_named_again_is_not_another_clock(
    index: IndexCatalog, flight: dict[str, Any]
) -> None:
    plain = index.window(TimeWindow(BOOT, 0, INT64_MAX))
    again = index.window(TimeWindow(BOOT, 0, INT64_MAX), clocks=[BOOT, BOOT])
    assert again.outcome == "answered" and again.clocks == ()
    assert again.entries == plain.entries


# --- time: determinism -------------------------------------------------------------------------


@pytest.mark.slow
def test_what_a_spent_step_budget_placed_does_not_depend_on_row_order() -> None:
    """256 parallel mappings and 10 000 intervals spend the request's step budget part-way; the
    intervals placed before it ran out are the same whatever order the store returned them in."""
    source, reference = "rec:sha256:" + "a" * 64, "rec:sha256:" + "b" * 64
    mappings = [
        ClockMapping(
            "rec:sha256:" + f"{k:064x}",
            source,
            reference,
            Fraction(1),
            Fraction(k),
            Fraction(k),
            (None, None),
        )
        for k in range(256)
    ]
    window = TimeWindow(reference, INT64_MIN, INT64_MAX)
    entries = [
        IntervalEntry(
            "series",
            "stream",
            f"rec:sha256:{i:064x}",
            "sha256:" + "c" * 64,
            source,
            1_000 * i,
            1_000 * i + 10,
            1,
        )
        for i in range(10_000)
    ]
    answers = []
    for seed in (1, 2):
        shuffled = list(entries)
        random.Random(seed).shuffle(shuffled)
        answers.append(place(window, source, shuffled, IntervalMapper(reference, mappings)))
    (placed, missed), again = answers
    assert (placed, missed) == again
    assert 0 < len(placed) < len(entries), "the budget ran out part-way"
    assert missed and all("step budget" in f.detail for f in missed)
    assert [e.record_id for _, e in placed] == [e.record_id for e in entries[: len(placed)]]


def test_a_window_answer_does_not_depend_on_request_order(
    index: IndexCatalog, flight: dict[str, Any], catalog: PostgresCatalog, tmp_path: Path
) -> None:
    transform = one_of(flight["rows"], "transform_record", lambda r: True)
    other = alignment_package(
        transform, b"other\n", gps_to_boot((GPS_UTC_START - 5, GPS_UTC_START + 10**8), bound=9)
    )
    write_package(tmp_path / "other", files(other))
    register(catalog, tmp_path / "other")
    second = one_of(other, "clock_mapping", lambda r: True)["id"]
    window = TimeWindow(BOOT, INT64_MIN, INT64_MAX)
    a = index.window(window, clocks=[GPS], mappings=[flight["mapping"], second])
    b = index.window(window, clocks=[GPS, GPS], mappings=[second, flight["mapping"]])
    assert a == b
    (gps,) = [e for e in a.entries if e.clock == GPS]
    assert gps.mapped is not None
    assert gps.mapped.path == (flight["mapping"],), "the smaller bound ranks first"


# --- space: the quadruped's frames --------------------------------------------------------------


@pytest.fixture
def legged(catalog: PostgresCatalog, tmp_path: Path) -> dict[str, Any]:
    rows = urdf()
    write_package(tmp_path / "urdf", files(rows))
    package = register(catalog, tmp_path / "urdf")
    return {"rows": rows, "package": package, "graph": frame_graph(rows)}


def test_a_box_in_a_named_frame_finds_the_poses_declared_there(
    index: IndexCatalog, legged: dict[str, Any]
) -> None:
    rows = legged["rows"]
    base = FrameReference(legged["graph"], "base")
    camera = one_of(rows, "frame_transform", lambda r: child_frame(r) == "front_camera")
    result = index.within(base, "m", SpatialBox((0.2, -0.1), (0.3, 0.1)))
    assert result.outcome == "answered" and result.findings == ()
    ((placed),) = result.placed
    assert (placed.record_id, placed.pointer, placed.package_id) == (
        camera["id"],
        "/parent",
        legged["package"],
    )
    assert (placed.low, placed.high, placed.unit) == ((0.28, 0.0, 0.05),) * 2 + ("m",)
    assert placed.extent_pointer == "/value/translation/values"
    # Every other member of base states no coordinates there; the thigh's pose misses the box.
    assert {u.reason for u in result.unplaced} == {"no_extent"}
    assert {u.entry.kind for u in result.unplaced} == {
        "frame",
        "frame_binding",
        "hardware_component",
    }
    thigh = one_of(rows, "frame_transform", lambda r: child_frame(r) == "fl_thigh")["id"]
    assert thigh not in {e.record_id for e in result.placed} | {
        u.entry.record_id for u in result.unplaced
    }


def test_a_third_axis_and_another_unit_are_compared_never_converted(
    index: IndexCatalog, legged: dict[str, Any]
) -> None:
    base = FrameReference(legged["graph"], "base")
    low = index.within(base, "m", SpatialBox((0.0, -1.0, -0.01), (1.0, 1.0, 0.01)))
    assert [p.low for p in low.placed] == [(0.19, 0.05, 0.0)], "the camera is 5 cm up"
    millimetres = index.within(base, "mm", SpatialBox((0.0, -1000.0), (1000.0, 1000.0)))
    assert millimetres.placed == ()
    assert {(u.entry.unit, u.reason) for u in millimetres.unplaced if u.entry.unit} == {
        ("m", "unit")
    }


def test_a_frame_of_another_graph_is_another_frame(
    catalog: PostgresCatalog, index: IndexCatalog, legged: dict[str, Any], tmp_path: Path
) -> None:
    other = resourced(legged["rows"], b"<robot name='b'/>\n", {"kind": "local", "path": "b.urdf"})
    write_package(tmp_path / "other", files(other))
    register(catalog, tmp_path / "other")
    assert frame_graph(other) != legged["graph"]
    box = SpatialBox((-10.0, -10.0), (10.0, 10.0))
    mine = index.within(FrameReference(legged["graph"], "base"), "m", box)
    theirs = index.within(FrameReference(frame_graph(other), "base"), "m", box)
    assert len(mine.placed) == len(theirs.placed) == 2
    assert {p.package_id for p in mine.placed} == {legged["package"]}
    assert not {p.record_id for p in mine.placed} & {p.record_id for p in theirs.placed}


def test_a_child_frame_holds_no_coordinates_of_its_parent(
    index: IndexCatalog, legged: dict[str, Any]
) -> None:
    thigh = index.within(
        FrameReference(legged["graph"], "fl_thigh"), "m", SpatialBox((-9.0, -9.0), (9.0, 9.0))
    )
    assert thigh.placed == ()
    assert {u.entry.pointer for u in thigh.unplaced} >= {"/child", "/ref"}


# --- space: the warehouse site map --------------------------------------------------------------


@pytest.fixture
def site_map(catalog: PostgresCatalog, tmp_path: Path) -> dict[str, Any]:
    docks, floor = warehouse(), vector_map()
    nowhere = {"kind": "local", "path": "sites-without-crs.csv"}
    unknown = resourced(warehouse(crs=None), b"the same docks, no CRS\n", nowhere)
    for name, rows in (("docks", docks), ("floor", floor), ("unknown", unknown)):
        write_package(tmp_path / name, files(rows))
        register(catalog, tmp_path / name)
    return {"docks": docks, "floor": floor, "unknown": unknown}


def test_a_box_in_a_named_crs_finds_the_docks_and_lists_the_map(
    index: IndexCatalog, site_map: dict[str, Any]
) -> None:
    crs = CrsReference(CRS84["authority"], CRS84["code"])
    result = index.within(crs, "deg", SpatialBox((103.6000, 1.3500), (103.6006, 1.3506)))
    assert result.outcome == "answered"
    ((dock),) = result.placed
    assert (dock.kind, dock.low, dock.unit) == ("asset", (103.6004, 1.3504), "deg")
    assert dock.extent_pointer == "/location/value"
    ((floor),) = result.unplaced
    assert (floor.entry.kind, floor.reason) == ("spatial_artifact", "no_extent")
    both = index.within(crs, "deg", SpatialBox((103.6, 1.35), (103.61, 1.36)))
    assert [p.low for p in both.placed] == [(103.6004, 1.3504), (103.6008, 1.3508)]
    unknown = {r["id"] for r in site_map["unknown"] if r["kind"] == "asset"}
    assert not unknown & {p.record_id for p in both.placed}, "an Unknown CRS is not CRS84"


def test_crs_codes_are_compared_verbatim(index: IndexCatalog, site_map: dict[str, Any]) -> None:
    wgs84 = index.within(
        CrsReference("EPSG", "4326"), "deg", SpatialBox((-180.0, -90.0), (180.0, 90.0))
    )
    assert (wgs84.outcome, wgs84.placed, wgs84.unplaced) == ("answered", (), ())


@pytest.mark.parametrize(
    ("reference", "unit", "box", "kwargs", "subjects"),
    [
        (None, "m", SpatialBox((0.0, 0.0), (1.0, 1.0)), {}, ["reference"]),
        (
            FrameReference("graph", "base"),
            "m",
            SpatialBox((0.0, 0.0), (1.0, 1.0)),
            {},
            ["reference"],
        ),
        (CrsReference("EPSG", ""), "deg", SpatialBox((0.0, 0.0), (1.0, 1.0)), {}, ["reference"]),
        (CrsReference("EPSG", "4326"), "metres", SpatialBox((0.0, 0.0), (1.0, 1.0)), {}, ["unit"]),
        (CrsReference("EPSG", "4326"), "m", SpatialBox((1.0, 0.0), (0.0, 1.0)), {}, ["box"]),
        (CrsReference("EPSG", "4326"), "m", SpatialBox((0.0,), (1.0,)), {}, ["box"]),
        (CrsReference("EPSG", "4326"), "m", SpatialBox((0.0, 0.0), (1.0, 1.0, 1.0)), {}, ["box"]),
        (
            CrsReference("EPSG", "4326"),
            "m",
            SpatialBox((0.0, float("nan")), (1.0, 1.0)),
            {},
            ["box"],
        ),
        (CrsReference("EPSG", "4326"), "m", SpatialBox([0.0, 0.0], [1.0, 1.0]), {}, ["box"]),  # type: ignore[arg-type]
        (CrsReference("EPSG", "4326"), "m", SpatialBox((10**400, 0), (10**401, 1)), {}, ["box"]),
        (
            CrsReference("EPSG", "4326"),
            "m",
            SpatialBox((0.0, 0.0), (1.0, 1.0)),
            {"as_of": -1},
            ["-1"],
        ),
        (
            CrsReference("EPSG", "4326"),
            "m",
            SpatialBox((0.0, 0.0), (1.0, 1.0)),
            {"max_entries": "10"},
            ["max_entries"],
        ),
    ],
)
def test_a_malformed_box_request_is_refused(
    index: IndexCatalog,
    reference: Any,
    unit: Any,
    box: Any,
    kwargs: dict[str, Any],
    subjects: list[str],
) -> None:
    result = index.within(reference, unit, box, **kwargs)
    assert (result.outcome, result.placed, result.unplaced) == ("refused", (), ())
    assert [f.subject for f in result.findings] == subjects


def test_too_many_members_are_refused(index: IndexCatalog, legged: dict[str, Any]) -> None:
    base = FrameReference(legged["graph"], "base")
    box = SpatialBox((-1.0, -1.0), (1.0, 1.0))
    assert index.within(base, "m", box, max_entries=50).outcome == "answered"
    refused = index.within(base, "m", box, max_entries=1)
    assert (refused.outcome, codes(refused)) == ("refused", [("invalid_request", "box")])


# --- registration and the rebuild ---------------------------------------------------------------


@pytest.mark.parametrize(
    "columns",
    [
        {"seq": pa.array([0, 1], pa.int64()), "time/0": pa.array([0.5, 1.5], pa.float64())},
        {"seq": pa.array([0, 1], pa.int64())},
    ],
    ids=["float-ticks", "no-clock-column"],
)
def test_a_series_file_that_holds_no_int64_ticks_is_an_error_not_a_guess(
    tmp_path: Path, columns: dict[str, Any]
) -> None:
    """The compiler's writer and checks refuse such a file, so a verified package never holds
    one; a file swapped after the check is read as hostile: an error registration turns into a
    ``record_invalid`` refusal, never an interval read from the wrong column."""
    rows = drone()
    write(rows, tmp_path / "drone", drone_series(rows))
    fd = open_root(str(tmp_path / "drone"))
    assert fd is not None
    try:
        package = check_package(fd, "register").package
        assert package is not None
        target = tmp_path / "drone" / series_path(stream_id(rows, "sensor_accel"))  # type: ignore[arg-type]
        pq.write_table(pa.table(columns), target)
        with pytest.raises((ValueError, KeyError, pa.ArrowException)):
            series_intervals(fd, package)
    finally:
        os.close(fd)


def test_index_rows_are_written_with_the_registration_and_append_only(
    pg_uri: str, flight: dict[str, Any]
) -> None:
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        conn.execute("SET search_path TO tenant_acme")
        rows = conn.execute(
            "SELECT subject, count(*) FROM time_interval GROUP BY subject ORDER BY subject"
        ).fetchall()
        assert rows == [("record", 1), ("series", 4)]
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("DELETE FROM time_interval")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE time_interval SET rows_known = 0")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("TRUNCATE spatial_extent")


def test_a_rebuild_reproduces_both_indexes(pg_server: str, pg_uri: str, tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        apply_migrations(conn, "acme")
    rows = drone()
    write(rows, tmp_path / "drone", drone_series(rows))
    transform = one_of(rows, "transform_record", lambda r: True)
    sync = alignment_package(transform, b"sync\n", gps_to_boot((0, INT64_MAX)))
    write_package(tmp_path / "sync", files(sync))
    write_package(tmp_path / "urdf", files(urdf()))
    write_package(tmp_path / "docks", files(warehouse()))
    with PostgresCatalog(pg_uri, "acme", package_roots=None, manifest=manifest) as catalog:
        for name in ("urdf", "drone", "docks", "sync"):
            register(catalog, tmp_path / name)
    with psycopg.connect(pg_uri) as conn:
        before = dump_tables(conn, "tenant_acme")
    assert before["time_interval"] and before["spatial_extent"]

    elsewhere = new_database(pg_server)
    report = rebuild(elsewhere, Manifest.from_bytes(manifest.read_bytes()), package_roots=None)
    assert report.outcome == "rebuilt", report
    with psycopg.connect(elsewhere) as conn:
        after = dump_tables(conn, "tenant_acme")
    assert after["time_interval"] == before["time_interval"]
    assert after["spatial_extent"] == before["spatial_extent"]
    window = TimeWindow(BOOT, INT64_MIN, INT64_MAX)
    mapping = one_of(sync, "clock_mapping", lambda r: True)["id"]
    with IndexCatalog(pg_uri, "acme") as a, IndexCatalog(elsewhere, "acme") as b:
        assert a.window(window, clocks=[GPS], mappings=[mapping]) == b.window(
            window, clocks=[GPS], mappings=[mapping]
        )
        crs = CrsReference("OGC", "CRS84")
        box = SpatialBox((100.0, 0.0), (110.0, 2.0))
        assert a.within(crs, "deg", box) == b.within(crs, "deg", box)


def test_an_unreachable_store_is_unavailable_not_an_answer(pg_uri: str) -> None:
    with fresh(pg_uri):
        pass
    broken = IndexCatalog(pg_uri.replace("/catalog_", "/missing_", 1), "acme")
    with pytest.raises(CatalogUnavailable):
        broken.window(TimeWindow(BOOT, 0, 1))


def test_a_registration_leaves_every_package_byte_unchanged(
    catalog: PostgresCatalog, tmp_path: Path
) -> None:
    rows = drone()
    write(rows, tmp_path / "drone", drone_series(rows))
    copy = tmp_path / "copy"
    shutil.copytree(tmp_path / "drone", copy)
    register(catalog, tmp_path / "drone")
    for path in sorted((tmp_path / "drone").rglob("*")):
        if path.is_file():
            assert path.read_bytes() == (copy / path.relative_to(tmp_path / "drone")).read_bytes()
    assert sorted(p.name for p in (tmp_path / "drone").iterdir()) == sorted(
        p.name for p in copy.iterdir()
    )

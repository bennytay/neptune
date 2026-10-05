"""Packages for the time and spatial index tests (MVL-97, ADR 0015), across embodiments.

Each is a subset of one of the compiler's worked examples, edited as JSON and re-identified with
the compiler's id rules (``ledger_thread_packages``):

- the drone's flight log with series rows on its boot clock, its sample clock and its GPS clock,
  and a ``ClockMapping`` from GPS time onto boot time stated in a package of its own;
- the warehouse AMR's site map (``tests/fixtures/geojson/warehouse_amr_site.geojson``): its docks
  as assets at the declared longitude and latitude in ``OGC:CRS84``, and the floor map as a vector
  map whose geometry stays in the file;
- the quadruped's URDF: frames, transforms with declared translations, and the parts mounted at
  them, all in one frame graph.
"""

import copy
from collections.abc import Callable
from typing import Any, Final

from ledger_series_packages import read
from ledger_thread_packages import (
    Record,
    artifact,
    known,
    logical,
    rebase,
    revision,
    stated,
    subset,
    timestamp,
)
from neptune.model.run import Stream
from neptune.model.series import ColumnType, SeriesBatch, SeriesColumn

# The drone's clocks (tests/fixtures/model/drone): the log's boot time, the accelerometer's sample
# time and the GPS receiver's UTC time, each a TimestampDomain of its own.
BOOT: Final = "rec:sha256:930f36cc553dce34af08e794001077c634527116bc12138b8ecd91d415b9ee31"
SAMPLE: Final = "rec:sha256:54affe4921915bef92a30a65aef80ead1d42b95ed34d41f722fb326a63e7e3b0"
GPS: Final = "rec:sha256:2cc49276ad09ca7f401000c838f09649480b62e881be9954925ffd76f3b06b9e"
# Where the drone's rows sit: boot microseconds after its run starts, and GPS microseconds.
ACCEL_START: Final = 12_000_000
GPS_BOOT_START: Final = 12_100_000
GPS_UTC_START: Final = 1_790_000_000_000_000
# The warehouse site map's docks (warehouse_amr_site.geojson): id, category, longitude, latitude.
DOCKS: Final = (("DOCK-01", "charging dock", 103.6004, 1.3504), ("17", "dock", 103.6008, 1.3508))
CRS84: Final = {"authority": "OGC", "code": "CRS84"}

Ticks = Callable[[int], int | None]


def drone() -> list[Record]:
    return subset("drone", "ulog")


def ticks_batch(stream: Stream, n: int, clocks: list[Ticks]) -> SeriesBatch:
    """``n`` rows of ``stream``, with ``clocks[k](i)`` the ticks of row ``i`` on clock ``k``; a
    None is an unknown tick, with its state column."""
    columns = [SeriesColumn("seq", ColumnType.INT64, tuple(range(n)))]
    for k, at in enumerate(clocks):
        ticks = tuple(at(i) for i in range(n))
        if any(t is None for t in ticks):
            states = tuple("unknown" if t is None else "known" for t in ticks)
            columns.append(SeriesColumn(f"state/time/{k}", ColumnType.STRING, states))
        columns.append(SeriesColumn(f"time/{k}", ColumnType.INT64, ticks))
    columns.append(SeriesColumn("locator/0/length", ColumnType.INT64, (64,) * n))
    columns.append(
        SeriesColumn("locator/0/offset", ColumnType.INT64, tuple(4096 + 64 * i for i in range(n)))
    )
    columns.append(
        SeriesColumn("value/x", ColumnType.FLOAT64, tuple(float(i % 13) for i in range(n)))
    )
    return SeriesBatch(stream.id, tuple(columns))


def drone_series(rows: list[Record]) -> dict[str, SeriesBatch]:
    """The accelerometer at 250 Hz for two seconds (boot and sample clocks) and the GPS at 25 Hz
    (boot and UTC clocks), on the topics the drone's streams name."""
    accel = _stream(rows, "sensor_accel")
    gps = _stream(rows, "vehicle_gps_position")
    assert len(accel.clocks) == len(gps.clocks) == 2 and accel.clocks[0] == gps.clocks[0]
    return {
        "sensor_accel": ticks_batch(
            accel, 500, [lambda i: ACCEL_START + 4_000 * i, lambda i: ACCEL_START + 4_000 * i - 150]
        ),
        "vehicle_gps_position": ticks_batch(
            gps,
            50,
            [lambda i: GPS_BOOT_START + 40_000 * i, lambda i: GPS_UTC_START + 40_000 * i],
        ),
    }


def _stream(rows: list[Record], topic: str) -> Stream:
    (record,) = [r for r in rows if r["kind"] == "stream" and r["topic"].get("value") == topic]
    made = read(record)
    assert isinstance(made, Stream)
    return made


def alignment_package(transform: Record, data: bytes, record: Record) -> list[Record]:
    """A package holding one alignment record over the bytes ``data`` (a sync log), stated by
    ``transform``, re-identified by the compiler's id rules."""
    source = artifact(data)
    content = source["content_id"]
    located = revision({"kind": "local", "path": f"sync/{content[7:19]}.txt"}, content)
    record = {
        **record,
        "id": "rec:sha256:" + "0" * 64,
        "provenance": {
            "assertion_kind": "stated",
            "evidence": {
                "locator": [{"kind": "byte_range", "length": len(data), "offset": 0}],
                "source": content,
            },
            "transform": transform["id"],
        },
        "schema_version": 3,
    }
    return [source, located, *rebase([transform, record])]


def gps_to_boot(window: tuple[int, int], bound: int = 2) -> Record:
    """GPS time onto boot time, ``t ↦ t - (GPS_UTC_START - GPS_BOOT_START)``, as the log's GPS
    messages pair them, valid for GPS ticks in ``window``."""
    return {
        "anchor": known(
            {
                "source": timestamp(GPS, GPS_UTC_START),
                "target": timestamp(BOOT, GPS_BOOT_START),
            }
        ),
        "kind": "clock_mapping",
        "method": "stated",
        "rate": known({"denominator": 1, "numerator": 1}),
        "residual_bound": known(timestamp(BOOT, bound)),
        "source": GPS,
        "target": BOOT,
        "validity": known(
            {
                "clock": GPS,
                "end": known(timestamp(GPS, window[1])),
                "start": known(timestamp(GPS, window[0])),
            }
        ),
    }


# --- space ---------------------------------------------------------------------------------------


def _position(record: Record, lon: float, lat: float, crs: Record | None) -> Record:
    """A declared WGS-84-style point as the GeoJSON adapter states one: longitude and latitude in
    degrees, in ``crs`` (Unknown when None)."""
    out: Record = copy.deepcopy(record["location"])
    value = out["value"]
    value["longitude"], value["latitude"] = lon, lat
    value["angle_unit"] = known("deg", stated(record))
    value["crs"] = known(crs, stated(record)) if crs is not None else {"knowledge": "unknown"}
    return out


def warehouse(crs: Record | None = CRS84) -> list[Record]:
    """The warehouse site map's docks: assets of site WH-7 at their declared points in ``crs``
    (Unknown when None). Built from the mobile robot's site register (the ``csv`` adapter's
    output), edited as the GeoJSON adapter states a Point feature with an ``asset_id``."""
    rows = subset("mobile_robot", "csv")
    sites = [r for r in rows if r["kind"] == "site"]
    rest = [r for r in rows if r["kind"] not in ("site", "structured_record", "structured_table")]
    made: list[Record] = []
    for site, (dock, category, lon, lat) in zip(sites, DOCKS, strict=True):
        ident = copy.deepcopy(site["identifiers"][0])
        ident["value"] = logical("geojson.asset_id", dock)
        made.append(
            {
                **site,
                "category": known(category, stated(site)),
                "identifiers": [ident],
                "kind": "asset",
                "location": _position(site, lon, lat, crs),
                "site": known(logical("geojson.site_id", "WH-7"), stated(site)),
            }
        )
    return rebase([*rest, *made])


def vector_map(crs: Record = CRS84) -> list[Record]:
    """The floor map as a spatial artifact: a vector map in ``crs`` whose zones stay in the file."""
    rows = subset("quadruped", "stl")

    def as_map(record: Record) -> Record:
        if record["kind"] != "spatial_artifact":
            return record
        return {
            **record,
            "category": "vector_map",
            "crs": known(crs, stated(record)),
            "unit": {"knowledge": "not_applicable"},
        }

    return rebase([as_map(r) for r in rows])


def urdf() -> list[Record]:
    """The quadruped's URDF: frames base, fl_thigh and front_camera in one frame graph."""
    return subset("quadruped", "urdf")


def frame_graph(rows: list[Record]) -> str:
    (graph,) = {r["ref"]["frame_graph_id"] for r in rows if r["kind"] == "frame"}
    return str(graph)


def one_of(rows: list[Record], kind: str, test: Callable[[Record], bool]) -> Record:
    (found,) = [r for r in rows if r["kind"] == kind and test(r)]
    return found


def child_frame(record: Record) -> Any:
    return record["child"]["frame_id"]

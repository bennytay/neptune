"""Packages with series files for the lakehouse tests (MVL-95), across embodiments.

Each package is a subset of one of the compiler's worked examples, re-identified with the
compiler's id rules (``ledger_thread_packages``), with a series file per stream written by the
compiler's own series writer and packaged by its package writer, so registration verifies every
row as it would an ingest's. Rows are synthetic but follow each stream's declared shape:

- an arm's ``/joint_states`` (the manipulator's MCAP: three clocks, joint positions and
  velocities as lists);
- a mobile base's ``/wheel_odom`` and ``/battery`` (the mobile robot's bag: two clocks and one);
- a legged robot's ``/joint_states`` (the quadruped's rosbag2).
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ledger_thread_packages import Record, edit, known, logical, resourced, stated
from neptune.model.kinds import RECORD_KINDS
from neptune.model.run import Stream
from neptune.model.series import ColumnType, SeriesBatch, SeriesColumn
from neptune.store.package import package_contents, write_package
from neptune.store.series import SERIES_SETTINGS, write_series

Values = Callable[[int], dict[str, SeriesColumn]]
# A worked example's adapter output: (example, adapter id).
ARM = ("manipulator", "mcap")
MOBILE = ("mobile_robot", "rosbag1")
LEGGED = ("quadruped", "rosbag2")


def read(record: Record) -> Any:
    _, parse = RECORD_KINDS[record["kind"]]
    return parse(record)


def one(rows: list[Record], kind: str, topic: str | None = None) -> Record:
    (found,) = [
        r for r in rows if r["kind"] == kind and (topic is None or r["topic"].get("value") == topic)
    ]
    return found


def stream_of(rows: list[Record], topic: str) -> Stream:
    made = read(one(rows, "stream", topic))
    assert isinstance(made, Stream)
    return made


def declared_run(rows: list[Record], name: str) -> list[Record]:
    """The run declared by a manifest as ``("manifest", name)``, so it threads across packages."""

    def declare(record: Record) -> Record:
        return {**record, "logical_id": known(logical("manifest", name), stated(record))}

    return edit(rows, "run", declare)


def second_part(rows: list[Record], label: str) -> list[Record]:
    """The same adapter over the next file of the recording: new bytes, so new ids and clocks."""
    return resourced(rows, f"{label}\n".encode(), {"kind": "local", "path": f"{label}.bin"})


def floats(name: str, n: int, at: Callable[[int], float]) -> SeriesColumn:
    return SeriesColumn(name, ColumnType.FLOAT64, tuple(at(i) for i in range(n)))


def joint_values(joints: int) -> Values:
    """An arm or a leg: positions and velocities per joint, as lists."""

    def make(n: int) -> dict[str, SeriesColumn]:
        def cells(scale: float) -> tuple[tuple[float, ...], ...]:
            return tuple(tuple(scale * (i % 997) + j for j in range(joints)) for i in range(n))

        return {
            "value/position": SeriesColumn("value/position", ColumnType.FLOAT64, cells(1e-3), True),
            "value/velocity": SeriesColumn("value/velocity", ColumnType.FLOAT64, cells(1e-2), True),
        }

    return make


def odometry(n: int) -> dict[str, SeriesColumn]:
    """A mobile base's wheel odometry: forward speed and yaw rate."""
    return {
        "value/linear_x": floats("value/linear_x", n, lambda i: 0.5 + (i % 7) * 0.01),
        "value/angular_z": floats("value/angular_z", n, lambda i: (i % 11) * 0.001),
    }


def battery(n: int) -> dict[str, SeriesColumn]:
    return {"value/data": SeriesColumn("value/data", ColumnType.FLOAT32, (24.0,) * n)}


def batch(
    stream: Stream,
    n: int,
    start: int,
    step: int,
    values: Values,
    *,
    unknown_last: int = 0,
    unknown_clock: int = 1,
) -> SeriesBatch:
    """``n`` rows of ``stream``: clock 0 at ``start + i * step``; clock ``k`` offset by ``k``
    ticks. With ``unknown_last``, that many trailing rows have no known tick on
    ``unknown_clock`` (a wrapped column with its state column), as a header stamp an adapter
    could not read. Rows whose clock 0 is unknown sort last, so they are the trailing ones."""
    columns = [SeriesColumn("seq", ColumnType.INT64, tuple(range(n)))]
    for k in range(len(stream.clocks)):
        ticks: tuple[int | None, ...] = tuple(start + i * step + k for i in range(n))
        if k == unknown_clock and unknown_last:
            ticks = ticks[: n - unknown_last] + (None,) * unknown_last
            states = ("known",) * (n - unknown_last) + ("unknown",) * unknown_last
            columns.append(SeriesColumn(f"state/time/{k}", ColumnType.STRING, states))
        columns.append(SeriesColumn(f"time/{k}", ColumnType.INT64, ticks))
    columns.append(SeriesColumn("locator/0/length", ColumnType.INT64, (64,) * n))
    columns.append(
        SeriesColumn("locator/0/offset", ColumnType.INT64, tuple(4096 + 64 * i for i in range(n)))
    )
    columns.extend(values(n).values())
    return SeriesBatch(stream.id, tuple(columns))


def empty_batch(stream: Stream) -> SeriesBatch:
    """A typed series with no rows, for a stream a test does not read."""
    names = stream.series_columns()
    return SeriesBatch(stream.id, tuple(SeriesColumn(name, ColumnType.INT64, ()) for name in names))


def write(
    rows: list[Record], root: Path, batches: dict[str, SeriesBatch], *, every_stream: bool = True
) -> str:
    """The package of ``rows`` at ``root``, with a series file per stream: ``batches`` by topic,
    an empty one for every other stream unless ``every_stream`` is False (a records-only stream)."""
    series = {}
    scratch = root.parent / f"{root.name}.series"
    scratch.mkdir(parents=True, exist_ok=True)
    for record in rows:
        if record["kind"] != "stream":
            continue
        stream = read(record)
        topic = record["topic"].get("value")
        if topic in batches:
            made = batches[topic]
        elif every_stream:
            made = empty_batch(stream)
        else:
            continue
        path = scratch / f"{stream.id[-16:]}.parquet"
        write_series(stream, [made], path)
        series[stream.id] = path
    contents = package_contents(
        [read(r) for r in rows], series=series, store={"series": SERIES_SETTINGS}
    )
    return write_package(root, contents)

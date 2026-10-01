"""MVL-18's acceptance: a ROS 1 run takes part in the same canonical model as an MCAP one.

``same_recording.mcap`` and the bags are one recording of a mobile manipulator (joint states,
odometry, transforms). Both are ingested, each by its own adapter, and their Run and Stream
objects are compared: topic, message type, message and schema encoding, the exact bytes of the
message definition, md5sum, callerid and latching, declared counts, the run's extent, the clock's
ticks, and every message's seq, time and payload bytes, each read back from its own source through
its own citations.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.mcap import McapAdapter
from neptune.adapters.rosbag1 import Rosbag1Adapter
from neptune.discovery.reader import BytesReader
from neptune.model.knowledge import Known, Unknown
from neptune.model.reference import TimestampDomain
from neptune.model.run import Run, Stream
from neptune.model.series import SEQ

FIXTURES: Final = Path(__file__).parents[2] / "fixtures"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BAGS: Final = _load("rosbag_reading", FIXTURES / "rosbag1" / "rosbag_reading.py")
MCAPS: Final = _load("mcap_reading", FIXTURES / "mcap" / "mcap_reading.py")
RECORDING: Final = _load("make_rosbag1", FIXTURES / "rosbag1" / "make_rosbag1.py")


def ingest(adapter: Any, name: str, folder: str) -> tuple[bytes, SourceOutput]:
    data = (FIXTURES / folder / name).read_bytes()
    return data, ingest_source(adapter, BytesReader(data))


def topic(stream: Stream) -> str:
    return stream.topic.known_or_raise()


def resolved(data: bytes, ref: Any, reading: Any) -> bytes:
    return bytes(reading.resolve(data, ref))


def describe(data: bytes, output: SourceOutput, reading: Any, payload: Any) -> dict[str, Any]:
    """Everything the two containers must agree on, read back from the container's own bytes."""
    (run,) = [r for r in output.records() if isinstance(r, Run)]
    clocks = {d.id: d for d in output.records() if isinstance(d, TimestampDomain)}
    by_id = {s.id: s for s in output.records() if isinstance(s, Stream)}
    extent = tuple(k.value.ticks if isinstance(k, Known) else None for k in (run.first, run.last))
    out: dict[str, Any] = {"run": (*extent, run.logical_id, run.machine), "streams": {}}
    for stream_id, stream in by_id.items():
        assert isinstance(stream.schema_definition, Known)
        count = stream.message_count.value if isinstance(stream.message_count, Known) else None
        found = sorted(
            (
                (row[SEQ], row["time/0"], payload(reading.resolve(data, stream.row_evidence(row))))
                for batches in [output.series()[stream_id]]
                for batch in batches
                for row in batch.rows()
            ),
            key=lambda item: item[0],  # type: ignore[arg-type, return-value]
        )
        first_clock = clocks[stream.clocks[0]]
        out["streams"][topic(stream)] = {
            "schema_name": stream.schema_name.known_or_raise(),
            "schema_encoding": stream.schema_encoding.known_or_raise(),
            "message_encoding": stream.message_encoding.known_or_raise(),
            "definition": resolved(data, stream.schema_definition.value, reading),
            "metadata": stream.metadata,
            "message_count": count,
            "extent": (stream.first, stream.last),
            "clock": (
                first_clock.scope,
                first_clock.resolution.known_or_raise(),
                first_clock.epoch,
                first_clock.timescale,
            ),
            "rows": found,
        }
    return out


def bag_payload(record: bytes) -> bytes:
    return bytes(BAGS.message(record).payload)


def mcap_payload(record: bytes) -> bytes:
    return bytes(MCAPS.message(record).payload)


@pytest.mark.parametrize(
    "bag", ["robot_none.bag", "robot_bz2.bag", "robot_lz4.bag", "unclosed.bag"]
)
def test_a_ros1_bag_and_the_same_recording_as_mcap_say_the_same_things(bag: str) -> None:
    bag_data, bag_out = ingest(Rosbag1Adapter(), bag, "rosbag1")
    mcap_data, mcap_out = ingest(McapAdapter(), "same_recording.mcap", "rosbag1")
    from_bag = describe(bag_data, bag_out, BAGS, bag_payload)
    from_mcap = describe(mcap_data, mcap_out, MCAPS, mcap_payload)
    scanned = bag == "unclosed.bag"  # nothing states a count or an extent in a bag never closed
    for side in (from_bag, from_mcap):
        assert set(side["streams"]) == {c.topic for c in RECORDING.CONNECTIONS}
        assert sum(len(s["rows"]) for s in side["streams"].values()) == len(RECORDING.MESSAGES)
    for name, expected in from_mcap["streams"].items():
        actual = from_bag["streams"][name]
        for key in (
            "schema_name",
            "schema_encoding",
            "message_encoding",
            "definition",
            "metadata",
            "extent",
            "clock",
            "rows",
        ):
            assert actual[key] == expected[key], (name, key)
        assert actual["message_count"] == (None if scanned else expected["message_count"]), name
    assert from_bag["run"][:2] == ((None, None) if scanned else from_mcap["run"][:2])
    assert from_bag["run"][2:] == from_mcap["run"][2:] == (Unknown(), Unknown())


def test_the_message_definitions_are_the_publishers_bytes_in_both() -> None:
    bag_data, bag_out = ingest(Rosbag1Adapter(), "robot_bz2.bag", "rosbag1")
    described = describe(bag_data, bag_out, BAGS, bag_payload)
    for connection in RECORDING.CONNECTIONS:
        side = described["streams"][connection.topic]
        assert side["definition"] == connection.definition.encode()
        assert side["schema_name"] == connection.type
        assert dict(side["metadata"])["md5sum"] == connection.md5sum


def test_a_bag_never_closed_has_unknown_counts_and_extent_where_mcap_states_them() -> None:
    _, bag_out = ingest(Rosbag1Adapter(), "unclosed.bag", "rosbag1")
    (run,) = [r for r in bag_out.records() if isinstance(r, Run)]
    assert (run.first, run.last) == (Unknown(), Unknown())
    assert all(s.message_count == Unknown() for s in bag_out.records() if isinstance(s, Stream))

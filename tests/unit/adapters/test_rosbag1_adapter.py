"""The ROS 1 bag adapter on real bags: what it reads, what it emits, and what it cites.

The fixtures (``tests/fixtures/rosbag1``) are one recording of a mobile manipulator written
several ways. The oracle for the messages is the official ``rosbags`` reader's reading of the same
files (``oracle.json``); the oracle for citations is ``rosbag_reading.resolve``, which decompresses
chunks itself.
"""

import hashlib
import importlib.util
import json
import struct
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.contract import (
    SIGNATURE,
    VERIFIED,
    ConfigError,
    ProbeHints,
    configure,
)
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.rosbag1 import DESCRIPTOR, Rosbag1Adapter
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.knowledge import AssertionKind, Known, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance
from neptune.model.reference import TimestampDomain
from neptune.model.run import Run, Stream
from neptune.model.series import SEQ, row_order
from neptune.model.time import NANOSECOND, ClockRole, Timestamp

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "rosbag1"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MAKE: Final = _load("make_rosbag1")
READING: Final = _load("rosbag_reading")
ORACLE: Final = json.loads((FIXTURES / "oracle.json").read_text())
VALID: Final = ("robot_none.bag", "robot_bz2.bag", "robot_lz4.bag", "unclosed.bag")


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(
    data: bytes, chunk_bytes: int = 64 << 20, max_rows: int = 100_000, **config: Any
) -> SourceOutput:
    return ingest_source(Rosbag1Adapter(chunk_bytes, max_rows), BytesReader(data), config)


def of(output: SourceOutput, kind: type) -> list[Any]:
    return [record for record in output.records() if isinstance(record, kind)]


def codes(output: SourceOutput) -> list[str]:
    return sorted(f.code for f in output.findings() if f.code != "rosbag1.payload_not_decoded")


def streams(output: SourceOutput) -> dict[str, Stream]:
    found = {}
    for stream in of(output, Stream):
        topic = stream.topic.known_or_raise() if isinstance(stream.topic, Known) else ""
        found[topic] = stream
    return found


def rows(output: SourceOutput) -> Iterator[tuple[Stream, dict[str, object]]]:
    by_id = {stream.id: stream for stream in of(output, Stream)}
    for stream_id, batches in output.series().items():
        for batch in batches:
            for row in batch.rows():
                yield by_id[stream_id], row


def as_bytes(output: SourceOutput) -> bytes:
    """Records and findings as canonical lines, then every stream's rows in ``seq`` order."""
    lines = [canonical_json.dumps(record.to_json()) for record in output.package_records()]
    series = []
    for stream, batches in output.series().items():
        found = sorted((row for batch in batches for row in batch.rows()), key=lambda r: r[SEQ])  # type: ignore[arg-type, return-value]
        series.append(f"{stream} {[sorted(row.items()) for row in found]!r}".encode())
        series.append(repr(sorted({batch.schema() for batch in batches})).encode())
    return b"\n".join(sorted(lines) + series)


def cites(data: bytes, evidence: EvidenceRef) -> bytes:
    return bytes(READING.resolve(data, evidence))


def grounds(state: Known[Any]) -> EvidenceRef:
    """The evidence a value's own provenance cites."""
    assert isinstance(state.provenance, Provenance)
    return state.provenance.evidence


# --- Probe ---------------------------------------------------------------------------------------


def probe(data: bytes, name: str = "x") -> tuple[float, list[str], str | None]:
    result = Rosbag1Adapter().probe(data[: 64 * 1024], ProbeHints(name, len(data)))
    return result.confidence, [r.code for r in result.reasons], result.version


def test_a_bag_is_verified_by_its_magic_and_bag_header_whatever_its_name() -> None:
    expected = (VERIFIED, ["rosbag1.magic", "rosbag1.header"], "2.0")
    assert probe(fixture("robot_bz2.bag"), "drive.bag") == expected
    assert probe(fixture("robot_bz2.bag"), "notes.txt") == expected
    assert probe(fixture("robot_bz2.bag"), "") == expected
    assert probe(fixture("unclosed.bag")) == expected


def test_magic_without_a_well_formed_bag_header_is_a_signature_match() -> None:
    assert probe(MAKE.MAGIC)[:2] == (SIGNATURE, ["rosbag1.magic"])
    assert probe(MAKE.MAGIC + bytes(20))[:2] == (SIGNATURE, ["rosbag1.magic"])
    junk = MAKE.MAGIC + MAKE._record([("op", b"\x05")], b"")  # a record, not a Bag Header
    assert probe(junk)[:2] == (SIGNATURE, ["rosbag1.magic"])


def test_anything_else_is_not_a_bag() -> None:
    for data in (b"", b"#ROSBAG", b"#ROSBAG V1.2\n" + bytes(20), b"plain text\n", b"\x89MCAP0\r\n"):
        assert probe(data)[:2] == (0.0, ["rosbag1.no_magic"])


# --- Inspect and plan: cheap, from the index -----------------------------------------------------


class Recording:
    """A reader that records every range read."""

    def __init__(self, data: bytes) -> None:
        self.inner = BytesReader(data)
        self.reads: list[tuple[int, int]] = []

    @property
    def content_id(self) -> Any:
        return self.inner.content_id

    @property
    def size(self) -> int:
        return self.inner.size

    def read(self, offset: int, length: int) -> bytes:
        self.reads.append((offset, length))
        return self.inner.read(offset, length)


def data_section_reads(data: bytes, reads: list[tuple[int, int]]) -> int:
    """Bytes read between the Bag Header and the index."""
    index_pos = struct.unpack_from("<Q", data, data.index(b"index_pos=") + 10)[0]
    start = len(MAKE.MAGIC) + 4096
    return sum(
        max(0, min(offset + length, index_pos) - max(offset, start)) for offset, length in reads
    )


def test_inspect_summarises_from_the_head_and_the_index_alone() -> None:
    data = fixture("robot_bz2.bag")
    source = Recording(data)
    result = Rosbag1Adapter().inspect(source, configure(DESCRIPTOR))
    assert data_section_reads(data, source.reads) == 0
    summary: Any = result.summary
    assert result.findings == ()
    assert (summary["planning"], summary["index"], summary["format_version"]) == (
        "indexed",
        "usable",
        "2.0",
    )
    assert summary["bag_header"]["chunk_count"] == 3 and summary["bag_header"]["conn_count"] == 4
    assert summary["chunks"] == 3 and summary["messages"] == 17
    assert [c["topic"] for c in summary["connections"]] == [c.topic for c in MAKE.CONNECTIONS]
    odom = summary["connections"][1]
    assert (odom["type"], odom["md5sum"], odom["callerid"], odom["message_count"]) == (
        "nav_msgs/Odometry",
        "cd5e73d190d741a2f92e81eda573aca7",
        "/base_driver",
        5,
    )
    canonical_json.dumps(summary)


def test_inspect_says_a_bag_without_an_index_is_scanned() -> None:
    for name, reason in (("unclosed.bag", "unclosed"), ("truncated.bag", "index_pos")):
        result = Rosbag1Adapter().inspect(BytesReader(fixture(name)), configure(DESCRIPTOR))
        assert (result.summary["index"], result.summary["planning"]) == (
            f"unusable: {reason}",
            "scan",
        )
        assert [f.code for f in result.findings] == ["rosbag1.index_invalid"]
    not_bag = Rosbag1Adapter().inspect(BytesReader(b"hello"), configure(DESCRIPTOR))
    assert [f.code for f in not_bag.findings] == ["rosbag1.bad_magic"]


def test_an_indexed_plan_never_reads_the_data_section() -> None:
    data = fixture("robot_bz2.bag")
    source = Recording(data)
    plan = Rosbag1Adapter(chunk_bytes=1, max_rows=1).plan(source, configure(DESCRIPTOR))
    assert data_section_reads(data, source.reads) == 0
    assert len(plan.chunks) > 3 and plan.findings == ()
    assert all(chunk.context["layout"] == "indexed" for chunk in plan.chunks)


def test_a_bag_without_a_usable_index_is_planned_by_scanning_it() -> None:
    plan = Rosbag1Adapter().plan(BytesReader(fixture("unclosed.bag")), configure(DESCRIPTOR))
    assert all(chunk.context["layout"] == "scanned" for chunk in plan.chunks)
    assert [f.code for f in plan.findings] == ["rosbag1.index_invalid"]
    assert plan.findings[0].details == {"reason": "unclosed"}


# --- What a bag declares -------------------------------------------------------------------------


BAG: Final = run(fixture("robot_bz2.bag"))


def test_the_run_cites_the_bag_header_and_its_extent_the_chunk_info_fields() -> None:
    data = fixture("robot_bz2.bag")
    (run_record,) = of(BAG, Run)
    assert len(cites(data, run_record.provenance.evidence)) == 4096  # the Bag Header record
    assert (run_record.logical_id, run_record.machine) == (Unknown(), Unknown())
    clock = streams(BAG)["/odom"].clocks[0]
    first, last = MAKE.MESSAGES[0].time, MAKE.MESSAGES[-1].time
    assert isinstance(run_record.first, Known) and isinstance(run_record.last, Known)
    assert run_record.first.value == Timestamp(first, clock)
    assert run_record.last.value == Timestamp(last, clock)
    for state, value in ((run_record.first, first), (run_record.last, last)):
        assert state.provenance.assertion_kind is AssertionKind.STATED
        sec, nsec = divmod(value, 10**9)
        assert cites(data, grounds(state)) == struct.pack("<II", sec, nsec)


def test_the_record_time_is_one_clock_on_the_recorder_never_converted() -> None:
    (domain,) = of(BAG, TimestampDomain)
    assert (domain.field, domain.scope) == ("time", ())
    assert isinstance(domain.role, Known) and domain.role.value is ClockRole.RECEIVE
    assert isinstance(domain.resolution, Known) and domain.resolution.value == NANOSECOND
    assert (domain.epoch, domain.timescale) == (Unknown(), Unknown())  # ROS time: never assumed
    assert {stream.clocks for stream in of(BAG, Stream)} == {(domain.id,)}


def test_a_stream_holds_its_connection_as_the_publisher_stated_it() -> None:
    data = fixture("robot_bz2.bag")
    found = streams(BAG)
    assert sorted(found) == sorted(c.topic for c in MAKE.CONNECTIONS)
    for connection in MAKE.CONNECTIONS:
        stream = found[connection.topic]
        declared = READING.fields(cites(data, stream.provenance.evidence))
        assert declared[0]["op"] == b"\x07"  # its Connection record
        assert declared[0]["topic"] == connection.topic.encode()
        assert stream.provenance.assertion_kind is AssertionKind.STATED
        assert isinstance(stream.schema_name, Known)
        assert stream.schema_name.value == connection.type
        assert stream.schema_name.provenance.assertion_kind is AssertionKind.STATED
        assert cites(data, grounds(stream.schema_name)) == connection.type.encode()
        assert isinstance(stream.schema_definition, Known)
        assert cites(data, stream.schema_definition.value) == connection.definition.encode()
        assert stream.schema_definition.provenance.assertion_kind is AssertionKind.STATED
        assert stream.metadata == tuple(
            sorted(
                [
                    ("callerid", connection.callerid),
                    ("latching", connection.latching),
                    ("md5sum", connection.md5sum),
                ]
            )
        )
        # the formats are the bag's own, named as MCAP's registry names them, cited to the magic
        assert (
            isinstance(stream.schema_encoding, Known) and stream.schema_encoding.value == "ros1msg"
        )
        assert isinstance(stream.message_encoding, Known)
        assert stream.message_encoding.value == "ros1"
        assert cites(data, grounds(stream.message_encoding)) == MAKE.MAGIC
    tf_static = found["/tf_static"]
    assert dict(tf_static.metadata)["latching"] == "1"


def test_message_counts_are_stated_and_cite_the_chunk_infos_that_state_them() -> None:
    data = fixture("robot_bz2.bag")
    counts = {t: s.message_count for t, s in streams(BAG).items()}
    assert {t: c.value for t, c in counts.items() if isinstance(c, Known)} == {
        "/joint_states": 6,
        "/odom": 5,
        "/tf": 5,
        "/tf_static": 1,
    }
    for knowledge in counts.values():
        assert isinstance(knowledge, Known)
        assert knowledge.provenance.assertion_kind is AssertionKind.STATED
        span = cites(data, grounds(knowledge))
        assert span.count(b"op=\x06") >= 1 and b"op=\x05" not in span  # chunk infos only


def test_every_stream_has_a_payload_finding_and_nothing_else_is_reported() -> None:
    assert codes(BAG) == []
    payloads = [f for f in BAG.findings() if f.code == "rosbag1.payload_not_decoded"]
    assert len(payloads) == 4 and all(len(f.records) == 1 for f in payloads)


# --- Every message a row, every row its bytes ----------------------------------------------------


def oracle_messages(name: str) -> list[list[Any]]:
    return sorted(ORACLE[name]["messages"], key=lambda m: (m[1], m[0]))


@pytest.mark.parametrize("name", VALID)
def test_rows_are_the_official_readers_messages(name: str) -> None:
    data = fixture(name)
    found = []
    for stream, row in rows(run(data)):
        message = READING.message(READING.resolve(data, stream.row_evidence(row)))
        found.append(
            [
                stream.topic.value if isinstance(stream.topic, Known) else "",
                row["time/0"],
                len(message.payload),
                hashlib.sha256(message.payload).hexdigest(),
            ]
        )
    assert sorted(found, key=lambda m: (m[1], m[0])) == oracle_messages("robot_bz2.bag")
    assert (
        ORACLE["robot_bz2.bag"]["messages"] == ORACLE["robot_none.bag"]["messages"]
    )  # the same recording, however it is compressed


@pytest.mark.parametrize("name", VALID)
def test_every_row_cites_exactly_its_message(name: str) -> None:
    data = fixture(name)
    output = run(data)
    for stream, row in rows(output):
        stream.check_row(row)
        op, record = READING.record_at(data, stream.row_evidence(row))
        message = READING.message(record)
        assert op == 2 and row["time/0"] == message.time
        declared = READING.fields(READING.resolve(data, stream.provenance.evidence))[0]
        assert struct.unpack("<I", declared["conn"])[0] == message.conn
        evidence = stream.row_evidence(row)
        assert [type(step) for step in evidence.locator] == [ByteRange, ByteRange]


@pytest.mark.parametrize("name", VALID)
def test_seq_is_each_connections_source_order(name: str) -> None:
    output = run(fixture(name))
    by_stream: dict[Any, list[dict[str, object]]] = {}
    for stream, row in rows(output):
        by_stream.setdefault(stream.id, []).append(row)
    for found in by_stream.values():
        ordered = sorted(found, key=lambda r: r["locator/0/offset"] * 10**9 + r["locator/1/offset"])  # type: ignore[operator]
        assert [row[SEQ] for row in ordered] == list(range(len(found)))
        assert [row[SEQ] for row in sorted(found, key=row_order)] == list(range(len(found)))


def test_every_layout_and_compression_reads_the_same_messages_with_the_same_seq() -> None:
    def keyed(output: SourceOutput) -> list[tuple[object, ...]]:
        return sorted(
            (stream.topic.known_or_raise(), row[SEQ], row["time/0"]) for stream, row in rows(output)
        )

    reference = keyed(BAG)
    assert len(reference) == 17
    for name in ("robot_none.bag", "robot_lz4.bag", "unclosed.bag"):
        assert keyed(run(fixture(name))) == reference, name


# --- Determinism, chunking and lineage -----------------------------------------------------------


GRANULARITIES: Final = ((64 << 20, 100_000), (1, 1), (300, 2), (5_000, 5))


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.bag")))
def test_output_is_byte_identical_twice_and_whatever_the_plan_cuts(name: str) -> None:
    data = fixture(name)
    reference = as_bytes(run(data))
    assert as_bytes(run(data)) == reference
    for chunk_bytes, max_rows in GRANULARITIES:
        assert as_bytes(run(data, chunk_bytes, max_rows)) == reference, (chunk_bytes, max_rows)


def test_a_chunk_with_more_messages_than_a_planned_chunk_holds_is_read_in_stretches() -> None:
    adapter = Rosbag1Adapter(max_rows=2)
    plan = adapter.plan(BytesReader(fixture("robot_bz2.bag")), configure(DESCRIPTOR))
    stretches = [c.context for c in plan.chunks if "first" in c.context and "part" in c.context]
    stretches = [c for c in stretches if c["part"] == "data"]
    assert stretches and stretches[0]["first"] == 0
    assert [("last" in c) for c in stretches].count(False) == 3  # one open-ended per chunk


def test_another_version_or_config_is_another_lineage() -> None:
    data = fixture("robot_bz2.bag")

    class Bumped(Rosbag1Adapter):
        descriptor = replace(DESCRIPTOR, version="0.1.1")

    bumped = ingest_source(Bumped(), BytesReader(data))
    configured = run(data, max_chunk_bytes=1 << 20)
    original = {record.id for record in BAG.records()}
    for other in (bumped, configured):
        assert other.config.transform.id != BAG.config.transform.id
        assert original.isdisjoint(record.id for record in other.records())


def test_the_decompressors_are_output_affecting_libraries_of_the_lineage() -> None:
    data = fixture("robot_bz2.bag")
    assert {name for name, _ in DESCRIPTOR.libraries} == {"bz2", "lz4"}
    assert all(version for _, version in DESCRIPTOR.libraries)
    assert dict(BAG.config.transform.libraries) == dict(DESCRIPTOR.libraries)

    class Upgraded(Rosbag1Adapter):
        descriptor = replace(
            DESCRIPTOR,
            libraries=tuple((name, version + ".1") for name, version in DESCRIPTOR.libraries),
        )

    upgraded = ingest_source(Upgraded(), BytesReader(data))
    assert upgraded.config.transform.id != BAG.config.transform.id
    (run_record,) = [r for r in upgraded.records() if isinstance(r, Run)]
    (original,) = [r for r in BAG.records() if isinstance(r, Run)]
    assert run_record.provenance.transform != original.provenance.transform


def test_limits_outside_what_the_adapter_declares_memory_for_are_config_errors() -> None:
    for name, ceiling in (("max_chunk_bytes", 256 << 20), ("max_header_bytes", 64 << 20)):
        for value in (0, ceiling + 1):
            with pytest.raises(ConfigError):
                run(fixture("robot_bz2.bag"), **{name: value})
        assert run(fixture("robot_bz2.bag"), **{name: ceiling}).config.transform.id


def test_inspect_lists_at_most_a_bounded_number_of_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("neptune.adapters.rosbag1.summary.MAX_LISTED", 2)
    result = Rosbag1Adapter().inspect(BytesReader(fixture("robot_bz2.bag")), configure(DESCRIPTOR))
    summary: Any = result.summary
    assert len(summary["connections"]) == 2 and summary["connections_omitted"] == 2

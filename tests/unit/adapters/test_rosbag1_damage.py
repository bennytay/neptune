"""The ROS 1 bag adapter on hostile and damaged bags (AGENTS.md non-negotiables 7 and 9).

Every case goes through ``ingest_source``, so every law of the adapter contract is checked on the
output; what each test adds is which findings come out, which rows survive, and that nothing
raises. Damaged bags are built here from the fixture generator's writer and patched byte by byte.
"""

import bz2
import importlib.util
import random
import struct
import sys
from collections import defaultdict
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.contract import configure
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.rosbag1 import DESCRIPTOR, Rosbag1Adapter
from neptune.adapters.rosbag1.records import parse_chunk_info
from neptune.discovery.reader import BytesReader
from neptune.model.finding import Severity
from neptune.model.knowledge import Known, Unknown
from neptune.model.run import Stream
from neptune.model.series import SEQ

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "rosbag1"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MAKE: Final = _load("make_rosbag1")
Options: Final = MAKE.Options


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(data: bytes, chunk_bytes: int = 64 << 20, max_rows: int = 100_000, **config: Any) -> Any:
    return ingest_source(Rosbag1Adapter(chunk_bytes, max_rows), BytesReader(data), config)


def codes(output: SourceOutput) -> list[str]:
    return sorted(f.code for f in output.findings() if f.code != "rosbag1.payload_not_decoded")


def found_rows(output: SourceOutput) -> dict[Any, list[dict[str, object]]]:
    by_stream: dict[Any, list[dict[str, object]]] = defaultdict(list)
    for stream_id, batches in output.series().items():
        for batch in batches:
            by_stream[stream_id].extend(batch.rows())
    return by_stream


def row_count(output: SourceOutput) -> int:
    return sum(len(rows) for rows in found_rows(output).values())


def keys(output: SourceOutput) -> set[tuple[str, int, object]]:
    names = {s.id: s.topic.known_or_raise() for s in output.records() if isinstance(s, Stream)}
    return {
        (names[stream], row[SEQ], row["time/0"])  # type: ignore[misc]
        for stream, rows in found_rows(output).items()
        for row in rows
    }


def assert_no_seq_twice(output: SourceOutput) -> None:
    for rows in found_rows(output).values():
        seqs = [row[SEQ] for row in rows]
        assert len(seqs) == len(set(seqs))


def patch(data: bytes, offset: int, value: bytes) -> bytes:
    return bytes(data[:offset] + value + data[offset + len(value) :])


def with_bag_header(data: bytes, **changes: int) -> bytes:
    """The bag with its Bag Header rewritten: ``index_pos``, ``conn_count``, ``chunk_count``."""
    start = len(MAKE.MAGIC)
    fields = {
        "index_pos": struct.unpack_from("<Q", data, data.index(b"index_pos=", start) + 10)[0],
        "conn_count": struct.unpack_from("<I", data, data.index(b"conn_count=", start) + 11)[0],
        "chunk_count": struct.unpack_from("<I", data, data.index(b"chunk_count=", start) + 12)[0],
    }
    fields.update(changes)
    header = MAKE._fields(
        [
            ("op", b"\x03"),
            ("index_pos", struct.pack("<Q", fields["index_pos"])),
            ("conn_count", struct.pack("<I", fields["conn_count"])),
            ("chunk_count", struct.pack("<I", fields["chunk_count"])),
        ]
    )
    padding = 4096 - 8 - len(header)
    record = struct.pack("<I", len(header)) + header + struct.pack("<I", padding) + b" " * padding
    return bytes(data[:start] + record + data[start + 4096 :])


ROBOT_NONE: Final = fixture("robot_none.bag")
ROBOT_BZ2: Final = fixture("robot_bz2.bag")
FULL: Final = run(ROBOT_NONE)
FULL_KEYS: Final = keys(FULL)


def times(output: SourceOutput) -> set[tuple[str, object]]:
    """Which messages have rows, whatever seq the index's counts gave them."""
    return {(topic, time) for topic, _, time in keys(output)}


FULL_TIMES: Final = times(FULL)


# --- Truncation and bit rot ----------------------------------------------------------------------


@pytest.mark.parametrize("name", ["robot_none.bag", "robot_bz2.bag", "robot_lz4.bag"])
def test_a_bag_cut_anywhere_gives_findings_and_a_strict_prefix_of_every_stream(name: str) -> None:
    data = fixture(name)
    cuts = sorted({*range(0, len(data), 97), 13, 14, 4108, 4109, 4110, len(data) - 1})
    for cut in cuts:
        output = run(data[:cut])
        assert_no_seq_twice(output)
        assert keys(output) <= FULL_KEYS, cut
        for rows in found_rows(output).values():  # no gaps: a prefix of the stream's messages
            assert sorted(int(str(row[SEQ])) for row in rows) == list(range(len(rows))), cut
        if cut < len(data):
            assert codes(output), cut  # a cut bag is never silent


def test_a_bag_cut_inside_a_chunk_keeps_the_whole_messages_its_prefix_decodes_to() -> None:
    data = fixture("truncated.bag")
    output = run(data)
    assert codes(output) == ["rosbag1.chunk_truncated", "rosbag1.index_invalid"]
    (cut,) = [f for f in output.findings() if f.code == "rosbag1.chunk_truncated"]
    assert cut.details["reason"] == "file_end" and cut.severity is Severity.WARNING
    assert 12 < row_count(output) < 17
    assert keys(output) <= FULL_KEYS


def test_a_cut_after_a_chunk_costs_only_the_index() -> None:
    data = fixture("robot_none.bag")
    index_pos = struct.unpack_from("<Q", data, data.index(b"index_pos=") + 10)[0]
    output = run(data[:index_pos])
    assert codes(output) == ["rosbag1.index_invalid"]
    assert keys(output) == FULL_KEYS
    (finding,) = [f for f in output.findings() if f.code == "rosbag1.index_invalid"]
    assert finding.details == {"reason": "counts"}  # the Bag Header counts what is no longer there


@pytest.mark.parametrize("name", ["robot_none.bag", "robot_bz2.bag", "robot_lz4.bag"])
def test_random_byte_flips_never_raise_and_never_invent_rows(name: str) -> None:
    data = fixture(name)
    rng = random.Random(18)
    for _ in range(60):
        damaged = bytearray(data)
        for _ in range(rng.choice((1, 1, 3, 8))):
            damaged[rng.randrange(len(damaged))] ^= 1 << rng.randrange(8)
        output = run(bytes(damaged))
        assert_no_seq_twice(output)
        assert row_count(output) <= 17 + 4  # a flipped length may add a message, never many


# --- An index that lies --------------------------------------------------------------------------


def test_a_chunk_info_that_counts_too_few_loses_the_messages_past_its_count() -> None:
    output = run(fixture("lying_chunk_info.bag"))
    assert codes(output) == ["rosbag1.message_count_mismatch"]
    (finding,) = [f for f in output.findings() if f.code == "rosbag1.message_count_mismatch"]
    assert finding.severity is Severity.ERROR
    assert finding.details["connections"] == [[0, 1, 2]]  # connection 0: indexed 1, found 2
    # the message past the count has no row; later rows are numbered from the index's counts
    assert row_count(output) == 16 and times(output) < FULL_TIMES
    assert_no_seq_twice(output)


def test_seq_never_overlaps_whatever_the_index_claims_or_the_plan_cuts() -> None:
    for change in (-1, 3):
        data, _ = MAKE.write(Options(compression="none", count_changes=((0, 0, change),)))
        reference = keys(run(data))
        for chunk_bytes, max_rows in ((1, 1), (400, 2), (64 << 20, 100_000)):
            output = run(data, chunk_bytes, max_rows)
            assert_no_seq_twice(output)
            assert keys(output) == reference, (change, chunk_bytes, max_rows)


def test_a_chunk_info_that_counts_too_many_is_a_warning_and_costs_no_row() -> None:
    data, _ = MAKE.write(Options(compression="none", count_changes=((0, 0, 3),)))
    output = run(data)
    assert codes(output) == ["rosbag1.message_count_mismatch"]
    (finding,) = [f for f in output.findings() if f.code == "rosbag1.message_count_mismatch"]
    assert finding.severity is Severity.WARNING
    assert row_count(output) == 17  # the later chunks' seq start after the claimed rows: a gap


def test_the_bag_header_miscounting_chunks_or_connections_means_scanning() -> None:
    for change in ({"chunk_count": 99}, {"conn_count": 0}, {"chunk_count": 2}):
        output = run(with_bag_header(ROBOT_NONE, **change))
        assert codes(output) == ["rosbag1.index_invalid"], change
        assert keys(output) == FULL_KEYS
    (finding,) = [f for f in output.findings() if f.code == "rosbag1.index_invalid"]
    assert finding.details == {"reason": "counts"}


def test_an_index_position_outside_the_data_section_means_scanning() -> None:
    for position in (1, 4000, len(ROBOT_NONE) + 5, 2**62):
        output = run(with_bag_header(ROBOT_NONE, index_pos=position))
        assert codes(output) == ["rosbag1.index_invalid"], position
        assert keys(output) == FULL_KEYS


def test_chunk_infos_out_of_order_are_not_believed() -> None:
    data, at = MAKE.write(Options(compression="none"))
    (a, la), (b, lb) = at["chunk_info:0"], at["chunk_info:1"]
    assert a + la == b  # neighbours
    swapped = data[:a] + data[b : b + lb] + data[a : a + la] + data[b + lb :]
    output = run(swapped)
    assert codes(output) == ["rosbag1.index_invalid"]
    (finding,) = [f for f in output.findings() if f.code == "rosbag1.index_invalid"]
    assert finding.details == {"reason": "order"}
    assert keys(output) == FULL_KEYS


def test_a_chunk_info_claiming_more_messages_than_a_chunk_can_hold_is_not_believed() -> None:
    data, at = MAKE.write(Options(compression="none"))
    offset, length = at["chunk_info:0"]
    # the last pair is (connection, count): count := 4 billion
    huge = patch(data, offset + length - 4, struct.pack("<I", 4_000_000_000))
    output = run(huge)
    assert codes(output) == ["rosbag1.index_invalid"]
    assert any(f.details == {"reason": "implausible"} for f in output.findings())
    assert keys(output) == FULL_KEYS
    # and a plan never explodes into stretches however many messages are claimed
    plan = Rosbag1Adapter(max_rows=1).plan(BytesReader(huge), configure(DESCRIPTOR))
    assert len(plan.chunks) < 100


def test_a_chunk_the_index_places_wrongly_loses_only_its_own_unit() -> None:
    data, at = MAKE.write(Options(compression="none"))
    offset, _ = at["chunk_info:1"]
    field = data.index(b"chunk_pos=", offset) + len(b"chunk_pos=")
    chunk_one = at["chunk:1"][0]
    moved = patch(data, field, struct.pack("<Q", chunk_one + 3))
    output = run(moved)
    mismatch = [f for f in output.findings() if f.code == "rosbag1.index_mismatch"]
    assert {f.details["reason"] for f in mismatch} == {"chunk_place"}
    assert mismatch[0].severity is Severity.ERROR
    assert 0 < row_count(output) < 17 and times(output) <= FULL_TIMES
    assert_no_seq_twice(output)


def test_a_chunk_the_index_does_not_list_has_no_rows_and_a_finding() -> None:
    data, at = MAKE.write(Options(compression="none"))
    offset, length = at["chunk_info:1"]
    without = data[:offset] + data[offset + length :]
    output = run(with_bag_header(without, chunk_count=2))
    mismatch = [f for f in output.findings() if f.code == "rosbag1.index_mismatch"]
    assert [f.details["reason"] for f in mismatch] == ["unindexed_chunk"]
    assert row_count(output) == 11 and times(output) <= FULL_TIMES


def test_index_data_records_that_disagree_with_their_chunk_are_one_finding_per_unit() -> None:
    data, at = MAKE.write(Options(compression="none"))
    offset, _ = at["index_data:0:0"]
    count_at = data.index(b"count=", offset) + len(b"count=")
    lied = patch(data, count_at, struct.pack("<I", 9))
    output = run(lied)
    (finding,) = [f for f in output.findings() if f.code == "rosbag1.index_mismatch"]
    assert finding.details["reason"] == "index_data" and finding.details["connections"] == [
        [0, 9, 2]
    ]
    assert keys(output) == FULL_KEYS  # the Chunk Info, not the Index Data, numbers the rows


def test_a_chunks_message_times_that_leave_its_chunk_info_span_are_a_warning() -> None:
    data, at = MAKE.write(Options(compression="none"))
    offset, _ = at["chunk_info:0"]
    field = data.index(b"end_time=", offset) + len(b"end_time=")
    lied = patch(data, field, struct.pack("<II", 1, 0))
    output = run(lied)
    (finding,) = [f for f in output.findings() if f.code == "rosbag1.index_mismatch"]
    assert finding.details["reason"] == "chunk_times" and finding.severity is Severity.WARNING
    assert keys(output) == FULL_KEYS


# --- Connections ---------------------------------------------------------------------------------


def test_a_connection_id_declared_twice_with_other_content_keeps_the_first() -> None:
    output = run(fixture("connection_collision.bag"))
    assert codes(output) == ["rosbag1.conflicting_declaration"]
    topics = sorted(s.topic.known_or_raise() for s in output.records() if isinstance(s, Stream))
    assert "/impostor" not in topics and len(topics) == 4
    assert keys(output) == FULL_KEYS


def test_connection_collisions_in_the_index_and_in_a_scanned_bag_are_found_too() -> None:
    other = MAKE.Connection(0, "/other", "std_msgs/String", "x" * 32, "string data\n", "/x", "0")
    for closed in (True, False):
        data, at = MAKE.write(Options(compression="none", closed=closed))
        extra = MAKE.connection_record(other)
        if closed:  # one more record in the index; the Bag Header counts it
            end = at["connection:3"][0] + at["connection:3"][1]
            data = with_bag_header(data[:end] + extra + data[end:], conn_count=5)
        else:
            data += extra
        output = run(data)
        assert "rosbag1.conflicting_declaration" in codes(output), closed
        topics = [s.topic.known_or_raise() for s in output.records() if isinstance(s, Stream)]
        assert "/other" not in topics


def test_messages_of_a_connection_nothing_declares_have_no_rows() -> None:
    data, _ = MAKE.write(Options(compression="none", closed=False))
    at = data.index(b"conn=\x03\x00\x00\x00", data.index(b"op=\x02"))  # the first tf_static message
    start = data.rfind(b"op=\x02", 0, at)
    assert start > 0
    damaged = patch(data, at + 5, struct.pack("<I", 7))
    output = run(damaged)
    (finding,) = [f for f in output.findings() if f.code == "rosbag1.unknown_connection"]
    assert finding.details["connections"] == [[7, 1]]
    assert row_count(output) == 16


def test_messages_without_a_conn_or_time_field_are_counted_not_read() -> None:
    data, _ = MAKE.write(Options(compression="none", closed=False))
    first = data.index(b"op=\x02")
    for name, expected in ((b"time", 1), (b"conn", 1)):
        at = data.index(name + b"=", first)
        damaged = patch(data, at, b"t1me" if name == b"time" else b"c0nn")
        output = run(damaged)
        (finding,) = [f for f in output.findings() if f.code == "rosbag1.corrupt_record"]
        assert finding.details["reason"] == "message" and finding.details["messages"] == expected
        assert row_count(output) <= 17


def test_a_connection_with_non_utf8_text_has_unknown_values_and_a_finding() -> None:
    data, _ = MAKE.write(Options(compression="none"))
    damaged = data.replace(b"sensor_msgs/JointState", b"sensor_msgs/Joint\xfftate")  # same length
    assert damaged != data
    output = run(damaged)
    assert "rosbag1.invalid_utf8" in codes(output)
    streams = {s.topic.known_or_raise(): s for s in output.records() if isinstance(s, Stream)}
    assert isinstance(streams["/joint_states"].schema_name, Unknown)
    assert isinstance(streams["/odom"].schema_name, Known)
    assert row_count(output) == 17


def test_missing_and_empty_connection_fields_are_unknown_and_reported() -> None:
    odd = MAKE.Connection(1, "/odom", "nav_msgs/Odometry", "m" * 32, "int32 x\n", "", "0")
    connections = (MAKE.CONNECTIONS[0], odd, *MAKE.CONNECTIONS[2:])
    data, _ = MAKE.write(Options(compression="none", connections=connections))
    damaged = data.replace(b"md5sum=", b"md5zum=")  # the same length: every md5sum is missing
    output = run(damaged)
    findings = [f for f in output.findings() if f.code == "rosbag1.missing_field"]
    assert len(findings) == 4
    assert all(f.details["fields"] == {"md5sum": "missing"} for f in findings)
    assert row_count(output) == 17
    odom = next(
        s for s in output.records() if isinstance(s, Stream) and s.topic.known_or_raise() == "/odom"
    )
    assert dict(odom.metadata)["callerid"] == ""  # blank metadata stays verbatim, never a fact
    stripped = data.replace(b"message_definition=", b"message_definitiox=")
    output = run(stripped)
    streams = [s for s in output.records() if isinstance(s, Stream)]
    assert all(isinstance(s.schema_definition, Unknown) for s in streams)
    reasons = {
        f.details["fields"]["message_definition"]
        for f in output.findings()
        if f.code == "rosbag1.missing_field"
    }
    assert reasons == {"missing"}


def test_a_repeated_connection_header_field_is_left_out_of_the_metadata() -> None:
    connection = MAKE.CONNECTIONS[1]
    fields = MAKE._fields(
        [
            ("topic", b"/odom"),
            ("type", connection.type.encode()),
            ("md5sum", connection.md5sum.encode()),
            ("message_definition", connection.definition.encode()),
            ("callerid", b"/a"),
            ("callerid", b"/b"),
            ("latching", b"0"),
        ]
    )
    head = [("op", b"\x07"), ("conn", struct.pack("<I", 7)), ("topic", b"/odom")]
    output = run(fixture("unclosed.bag") + MAKE._record(head, fields))
    stream = next(
        s for s in output.records() if isinstance(s, Stream) and s.id in found_rows(output)
    )
    del stream
    odd = [
        s for s in output.records() if isinstance(s, Stream) and "callerid" not in dict(s.metadata)
    ]
    assert len(odd) == 1 and dict(odd[0].metadata) == {"latching": "0", "md5sum": connection.md5sum}
    (finding,) = [f for f in output.findings() if f.code == "rosbag1.duplicate_key"]
    assert finding.details == {"id": 7, "keys": ["callerid"]}


def test_a_topic_filed_differently_than_the_publisher_named_it_is_reported() -> None:
    connection = MAKE.CONNECTIONS[0]
    head = [("op", b"\x07"), ("conn", struct.pack("<I", 0)), ("topic", b"/filed_as")]
    fields = MAKE._fields(
        [
            ("topic", connection.topic.encode()),
            ("type", connection.type.encode()),
            ("md5sum", connection.md5sum.encode()),
            ("message_definition", connection.definition.encode()),
        ]
    )
    output = run(MAKE.MAGIC + MAKE._record([("op", b"\x03")], b"") + MAKE._record(head, fields))
    (stream,) = [s for s in output.records() if isinstance(s, Stream)]
    assert stream.topic.known_or_raise() == "/filed_as"
    assert dict(stream.metadata) == {"md5sum": connection.md5sum, "topic": "/joint_states"}
    assert "rosbag1.topic_mismatch" in codes(output)


def test_more_connections_than_a_source_may_declare_are_a_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("neptune.adapters.rosbag1.layout.MAX_CONNECTIONS", 2)
    for name in ("robot_bz2.bag", "unclosed.bag"):
        output = run(fixture(name))
        assert "rosbag1.too_many_connections" in codes(output), name
        assert len([s for s in output.records() if isinstance(s, Stream)]) == 2


# --- Limits and bombs ----------------------------------------------------------------------------


def test_a_header_over_the_limit_is_skipped_by_its_length_and_never_read() -> None:
    big = MAKE._record([("op", b"\x09"), ("blob", b"x" * 1_500_000)], b"")
    data = fixture("unclosed.bag") + big
    sizes: list[int] = []

    class Spy:
        def __init__(self) -> None:
            self.inner = BytesReader(data)
            self.content_id = self.inner.content_id
            self.size = self.inner.size

        def read(self, offset: int, length: int) -> bytes:
            sizes.append(length)
            return self.inner.read(offset, length)

    output = ingest_source(Rosbag1Adapter(), Spy())
    (finding,) = [f for f in output.findings() if f.code == "rosbag1.header_too_large"]
    assert finding.severity is Severity.ERROR and finding.details["count"] == 1
    assert max(sizes) < 1_000_000  # the 1.5 MB header was never read
    assert keys(output) == FULL_KEYS
    raised = run(data, max_header_bytes=2_000_000)
    assert "rosbag1.header_too_large" not in codes(raised)


def test_a_header_length_that_lies_about_the_file_is_a_cut_not_a_read() -> None:
    liar = struct.pack("<I", 0xFFFFFFF0) + b"op=\x05"
    output = run(fixture("unclosed.bag") + liar)
    assert keys(output) == FULL_KEYS
    assert codes(output) in (
        ["rosbag1.corrupt_record", "rosbag1.index_invalid"],
        ["rosbag1.index_invalid", "rosbag1.truncated"],
    )


def test_a_chunk_that_decompresses_past_its_declared_size_is_refused_without_the_memory() -> None:
    import tracemalloc

    bomb = bz2.compress(bytes(40_000_000))  # 40 MB of zeros in a few dozen bytes
    assert len(bomb) < 100
    record = MAKE._record(
        [("op", b"\x05"), ("compression", b"bz2"), ("size", struct.pack("<I", 1000))], bomb
    )
    data = MAKE.MAGIC + MAKE._record([("op", b"\x03")], b"") + record
    tracemalloc.start()
    output = run(data)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert "rosbag1.decompression_failed" in codes(output)
    assert peak < 8_000_000, peak


def test_a_chunk_declaring_more_than_the_limit_is_not_read() -> None:
    output = run(ROBOT_BZ2, max_chunk_bytes=1000)
    assert set(codes(output)) == {"rosbag1.record_too_large"}
    assert row_count(output) == 0
    sized = [f for f in output.findings() if f.code == "rosbag1.record_too_large"]
    assert len(sized) == 3 and sized[0].severity is Severity.ERROR


def test_a_chunk_of_a_size_that_does_not_match_its_records_is_a_finding() -> None:
    for delta in (-1, 1, 1000):
        data = ROBOT_NONE
        at = data.index(b"size=")
        (size,) = struct.unpack_from("<I", data, at + 5)
        output = run(patch(data, at + 5, struct.pack("<I", size + delta)))
        assert "rosbag1.decompression_failed" in codes(output), delta
        assert_no_seq_twice(output)


def test_a_chunk_of_tiny_records_stops_at_the_most_a_chunk_within_the_limit_can_hold() -> None:
    tiny = struct.pack("<II", 0, 0) * 40  # 40 empty records, 8 bytes each
    record = MAKE._record(
        [("op", b"\x05"), ("compression", b"none"), ("size", struct.pack("<I", len(tiny)))], tiny
    )
    header = MAKE.MAGIC + MAKE._record([("op", b"\x03")], b"")
    output = run(header + record, max_chunk_bytes=460)  # at most 460 // 46 = 10 records
    assert "rosbag1.too_many_records" in codes(output)
    output = run(header + record)
    assert "rosbag1.too_many_records" not in codes(output)
    assert "rosbag1.corrupt_record" in codes(output)  # records with no op


def test_a_connection_header_of_thousands_of_fields_is_refused() -> None:
    fields = MAKE._fields([(f"k{i}", b"v") for i in range(3000)])
    head = [("op", b"\x07"), ("conn", struct.pack("<I", 9)), ("topic", b"/x")]
    output = run(fixture("unclosed.bag") + MAKE._record(head, fields))
    assert keys(output) == FULL_KEYS
    (finding,) = [f for f in output.findings() if f.code == "rosbag1.corrupt_record"]
    assert finding.details == {"reason": "connection"}
    assert len([s for s in output.records() if isinstance(s, Stream)]) == 4


def test_unknown_ops_and_top_level_messages_are_reported_once_per_unit() -> None:
    odd = MAKE._record([("op", b"\x63")], b"abc") * 3
    message = MAKE.message_record(MAKE.MESSAGES[0])
    output = run(fixture("unclosed.bag") + odd + message)
    unknown = [f for f in output.findings() if f.code == "rosbag1.unknown_record"]
    assert len(unknown) == 1 and unknown[0].details == {"ops": {"0x63": 3}}
    (outside,) = [f for f in output.findings() if f.code == "rosbag1.message_outside_layout"]
    assert outside.details == {"count": 1}
    assert keys(output) == FULL_KEYS


# --- Not a bag, or hardly one --------------------------------------------------------------------


def test_sources_that_are_not_bags_or_hardly_are_findings_never_raises() -> None:
    cases = {
        b"": ["rosbag1.bad_magic"],
        b"hello": ["rosbag1.bad_magic"],
        MAKE.MAGIC: ["rosbag1.truncated"],
        MAKE.MAGIC + b"\x01\x00": ["rosbag1.truncated"],
        MAKE.MAGIC + MAKE._record([("op", b"\x05")], b""): ["rosbag1.corrupt_record"],
        MAKE.MAGIC + MAKE._record([("op", b"\x03")], b""): ["rosbag1.corrupt_record"],
    }
    for data, expected in cases.items():
        output = run(data)
        assert codes(output)[: len(expected)] == expected, data
        assert row_count(output) == 0


def test_an_empty_closed_bag_has_a_run_and_a_clock_and_nothing_else() -> None:
    output = run(fixture("empty.bag"))
    assert codes(output) == []
    assert sorted(r.kind for r in output.records()) == ["run", "timestamp_domain"]


def test_ros_time_is_kept_as_stored_even_when_it_is_not_normalised() -> None:
    odd = MAKE.Message(0, 5 * 10**9 + 7, b"\x00")
    data, _ = MAKE.write(
        Options(
            compression="none", connections=MAKE.CONNECTIONS[:1], messages=(odd,), chunks=((0, 1),)
        )
    )
    at = data.index(b"time=", data.index(b"op=\x02")) + 5
    unnormalised = patch(data, at, struct.pack("<II", 2, 3_000_000_000))  # 2 s + 3e9 ns
    output = run(unnormalised)
    (rows,) = found_rows(output).values()
    assert rows[0]["time/0"] == 2 * 10**9 + 3_000_000_000  # as declared, not 5 s


def test_many_chunks_the_index_does_not_list_are_one_finding_with_a_count() -> None:
    data, at = MAKE.write(Options(compression="none"))
    start = at["chunk_info:0"][0]
    end = at["chunk_info:2"][0] + at["chunk_info:2"][1]
    unlisted = with_bag_header(data[:start] + data[end:], chunk_count=0)
    output = run(unlisted)
    (finding,) = [f for f in output.findings() if f.code == "rosbag1.index_mismatch"]
    assert finding.details == {"count": 3, "reason": "unindexed_chunk"}
    assert row_count(output) == 0


def test_a_scan_plans_at_most_so_many_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("neptune.adapters.rosbag1.layout.MAX_UNITS", 1)
    output = run(fixture("unclosed.bag"))
    (finding,) = [f for f in output.findings() if f.code == "rosbag1.too_many_records"]
    assert finding.details == {"limit": 1, "reason": "chunks"}
    unplanned = [f for f in output.findings() if f.code == "rosbag1.index_mismatch"]
    assert [f.details for f in unplanned] == [{"count": 2, "reason": "unplanned_chunk"}]
    assert row_count(output) == 6 and times(output) <= FULL_TIMES  # only the planned chunk


def test_an_index_listing_more_than_the_bag_header_counts_is_not_read_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int] = []
    real = parse_chunk_info

    def counting(raw: bytes) -> Any:
        seen.append(1)
        return real(raw)

    monkeypatch.setattr("neptune.adapters.rosbag1.layout.parse_chunk_info", counting)
    output = run(with_bag_header(ROBOT_NONE, chunk_count=1))
    assert codes(output) == ["rosbag1.index_invalid"] and len(seen) == 1  # stopped at the second
    assert keys(output) == FULL_KEYS


def test_the_index_lists_a_connection_by_a_bounded_number_of_bytes() -> None:
    long = MAKE.Connection(0, "/" + "t" * 5000, "p/T", "m" * 32, "int32 x\n", "/" + "n" * 5000, "0")
    data, _ = MAKE.write(Options(compression="none", connections=(long,), messages=(), chunks=()))
    summary: Any = Rosbag1Adapter().inspect(BytesReader(data), configure(DESCRIPTOR)).summary
    (entry,) = summary["connections"]
    assert len(entry["topic"]) == 256 and len(entry["callerid"]) == 256

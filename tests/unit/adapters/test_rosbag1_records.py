"""The ROS 1 bag container's parsers, on bytes: boundaries, malformed fields, bounded decoding."""

import bz2
import struct
from typing import Final

import lz4.frame
import pytest

from neptune.adapters.rosbag1.records import (
    MAX_FIELDS,
    ChunkError,
    ChunkFault,
    FieldError,
    InnerRecords,
    Op,
    decompress,
    op_name,
    parse_bag_header,
    parse_chunk_head,
    parse_chunk_info,
    parse_connection,
    parse_fields,
    parse_index_data,
    parse_record,
    record_limit,
    ticks,
)


def field(name: bytes, value: bytes) -> bytes:
    return struct.pack("<I", len(name) + 1 + len(value)) + name + b"=" + value


def record(fields: list[tuple[bytes, bytes]], data: bytes = b"") -> bytes:
    header = b"".join(field(n, v) for n, v in fields)
    return struct.pack("<I", len(header)) + header + struct.pack("<I", len(data)) + data


BODY: Final = b"".join(struct.pack("<II", n, n) for n in range(1, 9)) * 4000  # compressible


def test_fields_are_name_equals_value_with_binary_values() -> None:
    data = field(b"op", b"\x03") + field(b"blob", b"a=b\x00\xff")
    fields = parse_fields(data, 0, len(data))
    assert [f.name for f in fields] == [b"op", b"blob"]
    assert (
        data[fields[1].start : fields[1].start + fields[1].length] == b"a=b\x00\xff"
    )  # first = only


@pytest.mark.parametrize(
    "data",
    [
        b"\x03\x00",  # a length cut
        struct.pack("<I", 9) + b"op=1",  # a length past the header
        struct.pack("<I", 3) + b"op1",  # no equals sign
        struct.pack("<I", 3) + b"=ab",  # no name
    ],
)
def test_a_field_that_overruns_its_header_or_has_no_name_is_a_field_error(data: bytes) -> None:
    with pytest.raises(FieldError):
        parse_fields(data, 0, len(data))


def test_a_header_with_too_many_fields_is_refused_not_listed() -> None:
    data = field(b"k", b"v") * (MAX_FIELDS + 1)
    with pytest.raises(FieldError):
        parse_fields(data, 0, len(data))
    assert len(parse_fields(data, 0, len(field(b"k", b"v")) * MAX_FIELDS)) == MAX_FIELDS


def test_a_field_may_have_an_empty_value() -> None:
    (one,) = parse_fields(field(b"callerid", b""), 0, 13)
    assert one.length == 0


def test_a_record_must_hold_all_its_bytes() -> None:
    whole = record([(b"op", b"\x07")], b"abc")
    assert parse_record(whole).data_length == 3
    for cut in range(len(whole)):
        with pytest.raises(FieldError):
            parse_record(whole[:cut])


def test_the_bag_header_needs_its_three_fields_and_a_non_negative_position() -> None:
    good = record(
        [
            (b"op", b"\x03"),
            (b"index_pos", struct.pack("<Q", 4109)),
            (b"conn_count", struct.pack("<I", 1)),
            (b"chunk_count", struct.pack("<I", 2)),
        ]
    )
    header = parse_bag_header(parse_record(good).fields)
    assert (header.index_pos, header.conn_count, header.chunk_count) == (4109, 1, 2)
    for bad in (
        record([(b"op", b"\x03")]),
        record([(b"op", b"\x03"), (b"index_pos", b"\x00" * 4)]),
        record(
            [
                (b"op", b"\x03"),
                (b"index_pos", struct.pack("<Q", 2**63)),
                (b"conn_count", struct.pack("<I", 1)),
                (b"chunk_count", struct.pack("<I", 2)),
            ]
        ),
    ):
        with pytest.raises(FieldError):
            parse_bag_header(parse_record(bad).fields)


def test_times_are_seconds_and_nanoseconds_as_stored() -> None:
    assert ticks((1, 5)) == 10**9 + 5
    assert ticks((0, 3_000_000_000)) == 3_000_000_000  # never normalised
    assert ticks((2**32 - 1, 2**32 - 1)) < 2**63  # always a signed 64-bit tick count


def test_a_chunk_info_must_hold_the_pairs_it_counts() -> None:
    fields = [
        (b"op", b"\x06"),
        (b"ver", struct.pack("<I", 1)),
        (b"chunk_pos", struct.pack("<Q", 4109)),
        (b"start_time", struct.pack("<II", 1, 0)),
        (b"end_time", struct.pack("<II", 2, 0)),
        (b"count", struct.pack("<I", 2)),
    ]
    info = parse_chunk_info(record(fields, struct.pack("<IIII", 0, 5, 3, 7)))
    assert (info.chunk_pos, info.start_time, info.end_time, info.total) == (
        4109,
        10**9,
        2 * 10**9,
        12,
    )
    with pytest.raises(FieldError):
        parse_chunk_info(record(fields, struct.pack("<II", 0, 5)))  # one pair short
    other = [(n, struct.pack("<I", 2) if n == b"ver" else v) for n, v in fields]
    with pytest.raises(FieldError):
        parse_chunk_info(record(other, struct.pack("<IIII", 0, 5, 3, 7)))  # version 2


def test_an_index_data_record_says_whether_its_entries_are_whole() -> None:
    fields = [
        (b"op", b"\x04"),
        (b"ver", struct.pack("<I", 1)),
        (b"conn", struct.pack("<I", 3)),
        (b"count", struct.pack("<I", 2)),
    ]
    assert parse_index_data(record(fields, bytes(24))).consistent
    assert not parse_index_data(record(fields, bytes(23))).consistent
    assert parse_index_data(record(fields, bytes(24))).conn == 3


def test_a_connection_keeps_the_record_topic_apart_from_the_headers_fields() -> None:
    header = field(b"topic", b"/a") + field(b"type", b"t/T") + field(b"callerid", b"/n")
    raw = record([(b"op", b"\x07"), (b"conn", struct.pack("<I", 4)), (b"topic", b"/b")], header)
    connection = parse_connection(raw)
    assert connection.id == 4 and connection.topic is not None
    assert connection.topic.value == "/b"
    text = connection.text(b"topic")
    assert text is not None and text.value == "/a"
    assert [name for name, _ in connection.pairs()] == [b"topic", b"type", b"callerid"]
    assert connection.signature() == parse_connection(raw).signature()
    other = record([(b"op", b"\x07"), (b"conn", struct.pack("<I", 4)), (b"topic", b"/c")], header)
    assert connection.signature() != parse_connection(other).signature()  # the topic is part of it
    assert dict(connection.brief()) == {"topic": "/b", "type": "t/T", "callerid": "/n"}
    with pytest.raises(FieldError):
        parse_connection(record([(b"op", b"\x05")]))


def test_the_chunk_head_needs_compression_and_size() -> None:
    raw = record([(b"op", b"\x05"), (b"compression", b"bz2"), (b"size", struct.pack("<I", 9))])
    head = parse_chunk_head(parse_record(raw).fields)
    assert (head.compression.value, head.size) == ("bz2", 9)
    with pytest.raises(FieldError):
        parse_chunk_head(parse_record(record([(b"op", b"\x05")])).fields)


def test_op_names() -> None:
    assert [op_name(o) for o in (2, 3, 4, 5, 6, 7, 99, -1)] == [
        "message",
        "bag_header",
        "index_data",
        "chunk",
        "chunk_info",
        "connection",
        "0x63",
        "none",
    ]
    assert int(Op.CHUNK) == 5


# --- Walking a chunk's records -------------------------------------------------------------------


def test_a_chunks_records_are_walked_with_their_ops_and_where_the_walk_ended() -> None:
    a, b = (
        record([(b"op", b"\x02"), (b"conn", struct.pack("<I", 0))], b"xy"),
        record([(b"op", b"\x07")]),
    )
    walk = InnerRecords(a + b, 10)
    found = list(walk)
    assert [(i.offset, i.length, i.op) for i in found] == [(0, len(a), 2), (len(a), len(b), 7)]
    assert (walk.cut, walk.stop, walk.unparsed) == (None, None, 0)


def test_a_walk_reports_a_cut_record_a_stop_and_headers_that_do_not_parse() -> None:
    a = record([(b"op", b"\x02")])
    walk = InnerRecords(a + a[:-2], 10)
    assert len(list(walk)) == 1 and walk.cut == len(a)
    walk = InnerRecords(a * 5, 3)
    assert len(list(walk)) == 3 and walk.stop == 3 * len(a)
    broken = struct.pack("<I", 3) + b"xyz" + struct.pack("<I", 0)  # a header with no field
    walk = InnerRecords(broken + a, 10)
    found = list(walk)
    assert [i.op for i in found] == [-1, 2] and found[0].fields is None and walk.unparsed == 1
    assert list(InnerRecords(b"", 10)) == [] and list(InnerRecords(b"\x00\x00", 10)) == []


def test_the_record_limit_is_what_a_chunk_of_the_smallest_messages_holds() -> None:
    assert record_limit(46 * 100) == 100 and record_limit(1) == 1
    smallest = record(
        [(b"op", b"\x02"), (b"conn", struct.pack("<I", 0)), (b"time", struct.pack("<II", 0, 0))]
    )
    assert len(smallest) == 46


# --- Decompression -------------------------------------------------------------------------------


@pytest.mark.parametrize("compression", ["none", "bz2", "lz4"])
def test_a_chunk_decompresses_to_exactly_its_declared_size(compression: str) -> None:
    stored = {"none": BODY, "bz2": bz2.compress(BODY), "lz4": bytes(lz4.frame.compress(BODY))}
    assert decompress(compression, stored[compression], len(BODY), whole=True) == BODY
    for size in (len(BODY) - 1, len(BODY) + 1, 0):
        with pytest.raises(ChunkError) as error:
            decompress(compression, stored[compression], size, whole=True)
        assert error.value.fault in (ChunkFault.SIZE, ChunkFault.DECOMPRESSION)


def test_an_unknown_compression_is_refused() -> None:
    with pytest.raises(ChunkError) as error:
        decompress("zstd", b"", 0, whole=True)
    assert error.value.fault is ChunkFault.UNKNOWN_COMPRESSION


@pytest.mark.parametrize("compression", ["bz2", "lz4"])
def test_garbage_is_a_chunk_error_not_an_exception(compression: str) -> None:
    with pytest.raises(ChunkError) as error:
        decompress(compression, b"not compressed at all" * 10, 100, whole=True)
    assert error.value.fault in (ChunkFault.DECOMPRESSION, ChunkFault.SIZE)


@pytest.mark.parametrize("compression", ["bz2", "lz4"])
def test_a_cut_chunk_gives_the_prefix_its_stored_bytes_decode_to(compression: str) -> None:
    stored = bz2.compress(BODY) if compression == "bz2" else bytes(lz4.frame.compress(BODY))
    prefix = decompress(compression, stored[: len(stored) // 2], len(BODY), whole=False)
    assert BODY.startswith(prefix) and len(prefix) < len(BODY)
    assert decompress(compression, b"", len(BODY), whole=False) == b""


@pytest.mark.parametrize("compression", ["bz2", "lz4"])
def test_a_bomb_costs_its_declared_size_not_its_real_one(compression: str) -> None:
    zeros = bytes(30_000_000)
    stored = bz2.compress(zeros) if compression == "bz2" else bytes(lz4.frame.compress(zeros))
    with pytest.raises(ChunkError) as error:
        decompress(compression, stored, 1000, whole=True)
    assert error.value.fault is ChunkFault.SIZE
    partial = decompress(compression, stored, 1000, whole=False)
    assert partial == bytes(1000)  # never more than the declared size, however much it holds

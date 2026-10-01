"""MCAP records read from bytes: every field, every position, and every way bytes can lie."""

import contextlib
import struct
import zlib

import lz4.frame
import pytest
import zstandard
from hypothesis import given, settings
from hypothesis import strategies as st

from neptune.adapters.mcap.records import (
    MAGIC,
    RECORD_HEADER,
    ChunkError,
    ChunkFault,
    FieldError,
    Opcode,
    Text,
    check_crc,
    decompress,
    inner_records,
    opcode_name,
    parse_attachment_head,
    parse_channel,
    parse_chunk_head,
    parse_chunk_index,
    parse_header,
    parse_message_head,
    parse_message_index,
    parse_metadata,
    parse_schema,
    parse_statistics,
)
from neptune.adapters.mcap.scan import Place, place_from_json, scan
from neptune.discovery.reader import BytesReader


def string(text: bytes) -> bytes:
    return struct.pack("<I", len(text)) + text


def record(opcode: int, content: bytes) -> bytes:
    return struct.pack("<BQ", opcode, len(content)) + content


# --- Fields --------------------------------------------------------------------------------------


def test_strings_keep_their_raw_bytes_and_where_they_start() -> None:
    header = parse_header(string(b"ros2") + string(b"writer \xff"))
    assert header.profile == Text(b"ros2", 4)
    assert header.library.raw == b"writer \xff"
    assert header.library.value is None  # not UTF-8: never guessed
    assert header.library.shown == "writer \\xff"


def test_a_field_that_runs_past_its_record_is_a_field_error() -> None:
    with pytest.raises(FieldError):
        parse_header(struct.pack("<I", 10) + b"short")
    with pytest.raises(FieldError):
        parse_message_head(b"\x01\x00" + bytes(10))


def test_a_channel_reads_its_metadata_in_order_repeats_included() -> None:
    entries = (
        string(b"b") + string(b"2") + string(b"a") + string(b"1") + string(b"b") + string(b"3")
    )
    content = struct.pack("<HH", 7, 2) + string(b"/imu") + string(b"cdr") + string(entries)
    channel = parse_channel(content)
    assert (channel.id, channel.schema_id, channel.topic.value) == (7, 2, "/imu")
    assert [(k.value, v.value) for k, v in channel.metadata] == [("b", "2"), ("a", "1"), ("b", "3")]


def test_a_schema_says_where_its_definition_is() -> None:
    content = struct.pack("<H", 3) + string(b"pkg/Msg") + string(b"ros2msg") + string(b"int32 x")
    schema = parse_schema(content)
    start, length = schema.data
    assert content[start : start + length] == b"int32 x"


def test_trailing_bytes_a_later_version_adds_are_left_alone() -> None:
    assert parse_header(string(b"p") + string(b"l") + b"future").profile.value == "p"


def test_statistics_keep_where_each_channel_count_is() -> None:
    counts = struct.pack("<HQ", 1, 11) + struct.pack("<HQ", 2, 4)
    content = struct.pack("<QHIIIIQQ", 15, 1, 2, 0, 0, 3, 10, 90) + string(counts)
    stats = parse_statistics(content)
    assert (stats.message_count, stats.chunk_count) == (15, 3)
    assert [(c, n) for c, n, _ in stats.channel_message_counts] == [(1, 11), (2, 4)]
    for channel, count, at in stats.channel_message_counts:
        assert struct.unpack_from("<HQ", content, at) == (channel, count)


def test_index_and_metadata_records_parse() -> None:
    entries = struct.pack("<QQ", 5, 0) + struct.pack("<QQ", 6, 40)
    assert parse_message_index(struct.pack("<H", 1) + string(entries)).entries == ((5, 0), (6, 40))
    pairs = struct.pack("<HQ", 1, 100)
    content = (
        struct.pack("<QQQQ", 1, 2, 50, 60) + string(pairs) + struct.pack("<Q", 9) + string(b"zstd")
    )
    index = parse_chunk_index(content + struct.pack("<QQ", 30, 70))
    assert (index.chunk_start, index.message_index_offsets, index.compression.value) == (
        50,
        ((1, 100),),
        "zstd",
    )
    metadata = parse_metadata(string(b"run") + string(string(b"k") + string(b"")))
    assert [(k.value, v.value) for k, v in metadata.entries] == [("k", "")]


def test_an_attachment_says_where_its_data_and_crc_are() -> None:
    fields = struct.pack("<QQ", 1, 2) + string(b"a.yaml") + string(b"text/yaml")
    fields += struct.pack("<Q", 3) + b"abc"
    content = fields + struct.pack("<I", zlib.crc32(fields))
    head = parse_attachment_head(content, len(content))
    assert content[head.data[0] : head.data[0] + 3] == b"abc"
    assert (head.crc, head.crc_at) == (zlib.crc32(fields), len(fields))
    with pytest.raises(FieldError):
        parse_attachment_head(content, len(content) - 1)  # the CRC would run past the record


def test_opcode_names() -> None:
    assert opcode_name(Opcode.CHUNK) == "chunk"
    assert opcode_name(0x80) == "0x80"


# --- Records inside a chunk ----------------------------------------------------------------------


def test_inner_records_stop_at_a_record_cut_short() -> None:
    data = record(0x05, bytes(22)) + record(0x05, bytes(22))
    found, cut = inner_records(data)
    assert [(r.offset, r.length) for r in found] == [(0, 22), (31, 22)] and cut is None
    found, cut = inner_records(data[:-1])
    assert len(found) == 1 and cut == 31
    found, cut = inner_records(data[:35])
    assert len(found) == 1 and cut == 31  # not even the header


def test_a_chunk_head_reads_only_up_to_its_records() -> None:
    content = struct.pack("<QQQI", 1, 2, 100, 0) + string(b"lz4") + struct.pack("<Q", 64)
    head = parse_chunk_head(content)  # the records themselves are not needed
    assert (head.uncompressed_size, head.compression.value, head.records) == (100, "lz4", (43, 64))


# --- Decompression -------------------------------------------------------------------------------


def test_every_compression_the_specification_defines_decompresses_exactly() -> None:
    data = b"records " * 1000
    stored = {
        "": data,
        "zstd": zstandard.ZstdCompressor().compress(data),
        "lz4": lz4.frame.compress(data),
    }
    for compression, raw in stored.items():
        assert decompress(compression, raw, len(data), whole=True) == data


def test_a_decompression_bomb_costs_at_most_the_declared_size() -> None:
    bomb = zstandard.ZstdCompressor().compress(bytes(50_000_000))
    with pytest.raises(ChunkError) as raised:
        decompress("zstd", bomb, 1000, whole=True)
    assert raised.value.fault is ChunkFault.SIZE
    lz4_bomb = lz4.frame.compress(bytes(50_000_000))
    with pytest.raises(ChunkError):
        decompress("lz4", lz4_bomb, 1000, whole=True)


def test_garbage_unknown_names_and_short_output_are_chunk_errors() -> None:
    with pytest.raises(ChunkError) as raised:
        decompress("zstd", b"not zstd at all", 10, whole=True)
    assert raised.value.fault is ChunkFault.DECOMPRESSION
    with pytest.raises(ChunkError) as raised:
        decompress("lz4", b"not lz4 either", 10, whole=True)
    assert raised.value.fault is ChunkFault.DECOMPRESSION
    with pytest.raises(ChunkError) as raised:
        decompress("brotli", b"x", 1, whole=True)
    assert raised.value.fault is ChunkFault.UNKNOWN_COMPRESSION
    with pytest.raises(ChunkError) as raised:
        decompress("", b"abc", 4, whole=True)
    assert raised.value.fault is ChunkFault.SIZE


def test_a_chunk_cut_short_gives_the_prefix_its_bytes_decode_to() -> None:
    data = bytes(range(256)) * 2000
    for compression, raw in (
        ("", data),
        ("zstd", zstandard.ZstdCompressor().compress(data)),
        ("lz4", lz4.frame.compress(data)),
    ):
        prefix = decompress(compression, raw[: len(raw) // 2], len(data), whole=False)
        assert data.startswith(prefix)


def test_a_crc_of_zero_is_not_checked_and_any_other_must_match() -> None:
    check_crc(b"abc", 0)
    check_crc(b"abc", zlib.crc32(b"abc"))
    with pytest.raises(ChunkError) as raised:
        check_crc(b"abc", zlib.crc32(b"abd"))
    assert raised.value.fault is ChunkFault.CRC


# --- Scanning a source ---------------------------------------------------------------------------


def test_a_scan_yields_every_record_and_marks_the_one_cut_short() -> None:
    body = record(0x01, string(b"") + string(b"")) + record(0x0F, bytes(4))
    data = MAGIC + body
    found = list(scan(BytesReader(data), 8, len(data)))
    assert [(r.offset, r.opcode, r.cut) for r in found] == [(8, 1, False), (25, 15, False)]
    cut = list(scan(BytesReader(data), 8, len(data) - 1))
    assert cut[-1].cut and cut[-1].place == Place(((25, len(data) - 1 - 25),))
    header_cut = list(scan(BytesReader(data), 8, 30))
    assert header_cut[-1].opcode == -1 and header_cut[-1].place == Place(((25, 5),))


def test_a_scan_reads_large_records_lazily() -> None:
    big = record(0x09, bytes(3_000_000))
    data = MAGIC + big + record(0x0F, bytes(4))
    first, second = scan(BytesReader(data), 8, len(data))
    assert first.content is None and first.length == 3_000_000
    assert second.content == bytes(4)


def test_a_place_round_trips_through_its_json_and_names_its_bytes() -> None:
    nested = Place(((100, 50), (9, 31)))
    assert place_from_json(nested.to_json()) == nested
    assert nested.within(RECORD_HEADER + 2, 4) == Place(((100, 50), (20, 4)))
    with pytest.raises(ValueError):
        place_from_json([[1, -1]])
    with pytest.raises(ValueError):
        Place(())


@settings(max_examples=200, deadline=None)
@given(st.binary(max_size=400))
def test_no_bytes_make_the_parsers_raise_anything_but_field_errors(data: bytes) -> None:
    for parse in (
        parse_header,
        parse_schema,
        parse_channel,
        parse_chunk_head,
        parse_chunk_index,
        parse_statistics,
        parse_metadata,
        parse_message_index,
    ):
        with contextlib.suppress(FieldError):
            parse(data)
    inner_records(data)

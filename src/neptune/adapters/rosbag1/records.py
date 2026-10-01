"""The ROS 1 bag container, format 2.0, read from bytes (http://wiki.ros.org/Bags/Format/2.0).

A bag is a magic line, then records. A record is a ``u32`` header length, a header, a ``u32`` data
length and the data. A header is fields, each a ``u32`` length and ``name=value`` (the value is
binary); one field, ``op`` (a single byte), says what the record is. Integers are little-endian,
a time is ``u32`` seconds then ``u32`` nanoseconds. A Chunk's data is its records, stored
``none``, ``bz2`` or ``lz4``-compressed; a bag written to the end ends with Connection records and
one Chunk Info per chunk, which the Bag Header's ``index_pos`` points at.

Everything here is pure: bytes in, parsed fields out, with every position kept so a citation can
name the exact bytes a value came from. Anything that does not parse is a ``FieldError``; the
caller turns it into a finding. Nothing here reads a source or allocates more than its input.
"""

import bz2
import hashlib
import io
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any, Final, NamedTuple

MAGIC: Final = b"#ROSBAG V2.0\n"
FORMAT_VERSION: Final = "2.0"
BAG_HEADER_RECORD: Final = 4096  # the Bag Header record is padded to this size
LENGTH: Final = 4  # a u32 length
MAX_FIELDS: Final = 1024  # a header with more fields than this is not read
# A Message Data record needs op, conn and time fields: 4 + (4 + 3 + 1) + (4 + 5 + 4) + (4 + 5 + 8)
# header bytes, the header's and the data's length: no whole message is smaller than this.
MIN_MESSAGE_RECORD: Final = 46
MAX_CHUNK_BYTES_CEILING: Final = 256 * 1024 * 1024  # what the adapter's memory is declared for
MAX_HEADER_BYTES_CEILING: Final = 64 * 1024 * 1024
TIME_BYTES: Final = 8
NANOSECONDS: Final = 10**9
COMPRESSIONS: Final = ("none", "bz2", "lz4")
_BRIEF: Final = 256


class Op(IntEnum):
    MESSAGE = 0x02
    BAG_HEADER = 0x03
    INDEX_DATA = 0x04
    CHUNK = 0x05
    CHUNK_INFO = 0x06
    CONNECTION = 0x07


OPS: Final = frozenset(int(op) for op in Op)


def op_name(op: int) -> str:
    """``chunk`` for 0x05; ``0x80`` for an op the specification does not name, ``none`` for -1."""
    if op < 0:
        return "none"
    return Op(op).name.lower() if op in OPS else f"0x{op:02x}"


class FieldError(ValueError):
    """A record's fields run past its bytes or hold an impossible value."""


@dataclass(frozen=True)
class Text:
    """A string field: its raw bytes and where they start in the record."""

    raw: bytes
    at: int

    @property
    def value(self) -> str | None:
        """The text, or ``None`` if the bytes are not UTF-8."""
        try:
            return self.raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    @property
    def shown(self) -> str:
        """For summaries and messages only: invalid bytes as ``\\xNN`` escapes."""
        return self.raw.decode("utf-8", errors="backslashreplace")


class Field(NamedTuple):
    """One ``name=value`` field: the name, and where the value starts and how long it is."""

    name: bytes
    start: int
    length: int


def parse_fields(data: bytes, start: int, end: int) -> tuple[Field, ...]:
    """The fields in ``data[start:end]``; every length is checked against ``end`` first."""
    fields: list[Field] = []
    pos = start
    while pos < end:
        if len(fields) >= MAX_FIELDS:
            raise FieldError(f"more than {MAX_FIELDS} fields")
        if pos + LENGTH > end:
            raise FieldError("a field's length is cut")
        (size,) = struct.unpack_from("<I", data, pos)
        pos += LENGTH
        if size > end - pos:
            raise FieldError(f"a field of {size} bytes at {pos} runs past its header")
        equals = data.find(b"=", pos, pos + size)
        if equals <= pos:
            raise FieldError(f"the field at {pos} has no name")
        fields.append(Field(data[pos:equals], equals + 1, pos + size - equals - 1))
        pos += size
    return tuple(fields)


class Fields:
    """A header's fields over the bytes they were parsed from."""

    __slots__ = ("data", "fields")

    def __init__(self, data: bytes, fields: tuple[Field, ...]) -> None:
        self.data = data
        self.fields = fields

    def find(self, name: bytes) -> Field | None:
        """The first field called ``name``."""
        for field in self.fields:
            if field.name == name:
                return field
        return None

    def count(self, name: bytes) -> int:
        return sum(1 for field in self.fields if field.name == name)

    def value(self, name: bytes) -> bytes | None:
        field = self.find(name)
        return None if field is None else self.data[field.start : field.start + field.length]

    def unsigned(self, name: bytes, size: int) -> int | None:
        """An integer field of exactly ``size`` bytes (4 or 8), else ``None``."""
        raw = self.value(name)
        if raw is None or len(raw) != size:
            return None
        return int.from_bytes(raw, "little")

    def time(self, name: bytes) -> tuple[int, int] | None:
        """A time field: seconds and nanoseconds, else ``None``."""
        raw = self.value(name)
        if raw is None or len(raw) != TIME_BYTES:
            return None
        sec, nsec = struct.unpack("<II", raw)
        return int(sec), int(nsec)


def ticks(time: tuple[int, int]) -> int:
    """Nanoseconds from a time's seconds and nanoseconds, as stored (never normalised)."""
    return time[0] * NANOSECONDS + time[1]


def time_at(data: bytes, at: int = 0) -> int:
    """The nanosecond ticks of the 8-byte time that starts at ``data[at]``."""
    sec, nsec = struct.unpack_from("<II", data, at)
    return int(sec) * NANOSECONDS + int(nsec)


@dataclass(frozen=True)
class Parsed:
    """A whole record's bytes, parsed: its header's fields and where its data is."""

    fields: Fields
    data_start: int
    data_length: int

    @property
    def op(self) -> int:
        raw = self.fields.value(b"op")
        return raw[0] if raw is not None and len(raw) == 1 else -1


def parse_record(data: bytes, at: int = 0) -> Parsed:
    """The record that starts at ``data[at]``; ``data`` must hold all of it."""
    try:
        (header_length,) = struct.unpack_from("<I", data, at)
        header_end = at + LENGTH + header_length
        fields = parse_fields(data, at + LENGTH, header_end)
        (data_length,) = struct.unpack_from("<I", data, header_end)
    except struct.error:
        raise FieldError("a record is cut inside its lengths") from None
    start = header_end + LENGTH
    if start + data_length > len(data):
        raise FieldError("a record's data runs past its bytes")
    return Parsed(Fields(data, fields), start, data_length)


# --- Records ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BagHeader:
    index_pos: int
    conn_count: int
    chunk_count: int


def parse_bag_header(fields: Fields) -> BagHeader:
    index_pos = fields.unsigned(b"index_pos", 8)
    conn_count = fields.unsigned(b"conn_count", 4)
    chunk_count = fields.unsigned(b"chunk_count", 4)
    if index_pos is None or conn_count is None or chunk_count is None:
        raise FieldError("the Bag Header lacks index_pos, conn_count or chunk_count")
    if index_pos >= 2**63:
        raise FieldError("index_pos is negative")
    return BagHeader(index_pos, conn_count, chunk_count)


@dataclass(frozen=True)
class ChunkHead:
    compression: Text
    size: int  # the uncompressed size the Chunk declares


def parse_chunk_head(fields: Fields) -> ChunkHead:
    compression, size = fields.find(b"compression"), fields.unsigned(b"size", 4)
    if compression is None or size is None:
        raise FieldError("a Chunk lacks compression or size")
    raw = fields.data[compression.start : compression.start + compression.length]
    return ChunkHead(Text(raw, compression.start), size)


@dataclass(frozen=True)
class Connection:
    """A Connection record: its id, the topic it is recorded under and its connection header.

    ``header`` holds the connection header's fields (the data of the record), ``topic`` the record
    header's own ``topic`` field. Positions are relative to the start of the record's bytes.
    """

    id: int
    topic: Text | None
    header: Fields

    def text(self, name: bytes) -> Text | None:
        field = self.header.find(name)
        if field is None:
            return None
        return Text(self.header.data[field.start : field.start + field.length], field.start)

    def pairs(self) -> tuple[tuple[bytes, bytes], ...]:
        """Every connection header field, name and value, in stored order."""
        data = self.header.data
        return tuple((f.name, data[f.start : f.start + f.length]) for f in self.header.fields)

    def signature(self) -> bytes:
        """A digest of the topic and every header field, to tell a repeated declaration from a
        different one without keeping the record."""
        digest = hashlib.sha256(self.topic.raw if self.topic is not None else b"\xff")
        for name, value in self.pairs():
            digest.update(struct.pack("<II", len(name), len(value)) + name + value)
        return digest.digest()

    def brief(self) -> tuple[tuple[str, str], ...]:
        """What ``inspect`` lists of a connection: its topic, type, md5sum, callerid and
        latching, each cut to 256 bytes."""
        found = []
        if self.topic is not None:
            found.append(("topic", Text(self.topic.raw[:_BRIEF], 0).shown))
        for name in ("type", "md5sum", "callerid", "latching"):
            text = self.text(name.encode())
            if text is not None:
                found.append((name, Text(text.raw[:_BRIEF], 0).shown))
        return tuple(found)


def parse_connection(data: bytes) -> Connection:
    """A whole Connection record (``data`` starts at its header length)."""
    record = parse_record(data)
    if record.op != Op.CONNECTION:
        raise FieldError("not a Connection record")
    conn = record.fields.unsigned(b"conn", 4)
    topic = record.fields.find(b"topic")
    if conn is None:
        raise FieldError("a Connection lacks conn")
    header = Fields(
        data, parse_fields(data, record.data_start, record.data_start + record.data_length)
    )
    topic_text = (
        None if topic is None else Text(data[topic.start : topic.start + topic.length], topic.start)
    )
    return Connection(conn, topic_text, header)


@dataclass(frozen=True)
class ChunkInfo:
    """A Chunk Info record: where its chunk is, its time span and its messages per connection."""

    chunk_pos: int
    start_time: int  # ns
    end_time: int
    start_at: int  # where the start_time / end_time field values start in the record
    end_at: int
    counts: tuple[tuple[int, int], ...]  # (connection id, messages), as stored

    @property
    def total(self) -> int:
        return sum(count for _, count in self.counts)


def parse_chunk_info(data: bytes) -> ChunkInfo:
    """A whole Chunk Info record (``data`` starts at its header length)."""
    record = parse_record(data)
    fields = record.fields
    if record.op != Op.CHUNK_INFO:
        raise FieldError("not a Chunk Info record")
    version, pos = fields.unsigned(b"ver", 4), fields.unsigned(b"chunk_pos", 8)
    start, end = fields.time(b"start_time"), fields.time(b"end_time")
    count = fields.unsigned(b"count", 4)
    start_field, end_field = fields.find(b"start_time"), fields.find(b"end_time")
    if None in (version, pos, start, end, count, start_field, end_field):
        raise FieldError("a Chunk Info lacks ver, chunk_pos, start_time, end_time or count")
    assert start and end and start_field and end_field and count is not None and pos is not None
    if version != 1:
        raise FieldError(f"Chunk Info version {version}")
    if record.data_length != 8 * count:
        raise FieldError("a Chunk Info's data is not its count of (connection, count) pairs")
    pairs = struct.iter_unpack(
        "<II", memoryview(data)[record.data_start : record.data_start + 8 * count]
    )
    return ChunkInfo(
        pos, ticks(start), ticks(end), start_field.start, end_field.start, tuple(pairs)
    )


@dataclass(frozen=True)
class IndexData:
    """An Index Data record: the connection it indexes and how many entries it says it holds."""

    conn: int
    count: int
    consistent: bool  # the data holds exactly ``count`` (time, offset) entries


def parse_index_data(data: bytes) -> IndexData:
    record = parse_record(data)
    fields = record.fields
    if record.op != Op.INDEX_DATA:
        raise FieldError("not an Index Data record")
    version, conn, count = (
        fields.unsigned(b"ver", 4),
        fields.unsigned(b"conn", 4),
        fields.unsigned(b"count", 4),
    )
    if version is None or conn is None or count is None:
        raise FieldError("an Index Data record lacks ver, conn or count")
    if version != 1:
        raise FieldError(f"Index Data version {version}")
    return IndexData(conn, count, record.data_length == 12 * count)


# --- Records inside a chunk ---------------------------------------------------------------------


def record_limit(max_chunk_bytes: int) -> int:
    """How many records a chunk's walk visits: as many as ``max_chunk_bytes`` holds messages.

    No chunk within the size limit holds more records than this, so only a chunk of records
    smaller than any whole message reaches it (ADR 0046 §7).
    """
    return max(1, max_chunk_bytes // MIN_MESSAGE_RECORD)


class Inner(NamedTuple):
    """A record inside a chunk: where it is and how long, its op, and its parsed header.

    ``fields`` is ``None`` when the header does not parse (``op`` is then -1).
    """

    offset: int
    length: int
    op: int
    fields: Fields | None


def message_connection(inner: Inner) -> int | None:
    """The connection a record inside a chunk is a message of: the rule that decides which records
    are messages, and so how every connection's messages are numbered, for the plan's counts and
    for ingest alike. ``None`` for a record that is not a message or names no ``conn``."""
    if inner.op != Op.MESSAGE or inner.fields is None:
        return None
    return inner.fields.unsigned(b"conn", 4)


class InnerRecords:
    """The records in a chunk's uncompressed bytes, walked lazily: nothing is kept per record.

    Iterating yields an ``Inner`` for each whole record in order, at most ``most`` of them. After
    a whole walk ``cut`` is the offset of a record the bytes end inside, ``stop`` the offset where
    the walk stopped after ``most`` records, ``unparsed`` how many records had a header that does
    not parse; each is ``None`` or 0 when it does not apply. Framing needs only the two lengths,
    so a record whose header fields are malformed is yielded (with ``fields`` ``None``) and the
    walk goes on after it. A walk holds no more than the bytes it is given.
    """

    __slots__ = ("cut", "data", "most", "stop", "unparsed")

    def __init__(self, data: bytes, most: int) -> None:
        self.data = data
        self.most = most
        self.cut: int | None = None
        self.stop: int | None = None
        self.unparsed = 0

    def __iter__(self) -> Iterator[Inner]:
        data, size, most = self.data, len(self.data), self.most
        unpack = struct.Struct("<I").unpack_from
        self.cut = self.stop = None
        self.unparsed = 0
        offset = count = 0
        while offset < size:
            if offset + LENGTH > size:
                self.cut = offset
                break
            header_end = offset + LENGTH + unpack(data, offset)[0]
            if header_end + LENGTH > size:
                self.cut = offset
                break
            end = header_end + LENGTH + unpack(data, header_end)[0]
            if end > size:
                self.cut = offset
                break
            if count == most:
                self.stop = offset
                break
            count += 1
            try:
                fields = Fields(data, parse_fields(data, offset + LENGTH, header_end))
                raw = fields.value(b"op")
                op = raw[0] if raw is not None and len(raw) == 1 else -1
            except FieldError:
                fields, op = None, -1
            if op < 0:
                self.unparsed += 1
            yield Inner(offset, end - offset, op, fields)
            offset = end


# --- Decompression ------------------------------------------------------------------------------


class ChunkFault(StrEnum):
    """Why a chunk's records could not be read."""

    UNKNOWN_COMPRESSION = "unknown_compression"
    DECOMPRESSION = "decompression"
    SIZE = "size"


class ChunkError(Exception):
    def __init__(self, fault: ChunkFault) -> None:
        super().__init__(str(fault))
        self.fault = fault


# Decompressed output is written in pieces of at most this, into one growing buffer.
_PIECE: Final = 4 * 1024 * 1024


def _drain(decompressor: Any, data: bytes, limit: int, out: io.BytesIO) -> None:
    """One compressed stream, fed in pieces so the decompressor never holds a copy of the rest,
    and read in pieces so its output never passes ``limit`` + 1 bytes."""
    view = memoryview(data)
    position = 0
    while out.tell() <= limit and not decompressor.eof:
        room = min(_PIECE, limit + 1 - out.tell())
        if decompressor.needs_input:
            if position >= len(view):
                break
            stored = view[position : position + _PIECE]
            position += len(stored)
            piece = decompressor.decompress(stored, max_length=room)
        else:
            piece = decompressor.decompress(b"", max_length=room)
            if not piece:
                break
        out.write(piece)


def _bz2(data: bytes, limit: int, out: io.BytesIO) -> None:
    _drain(bz2.BZ2Decompressor(), data, limit, out)


def _lz4(data: bytes, limit: int, out: io.BytesIO) -> None:
    import lz4.frame

    _drain(lz4.frame.LZ4FrameDecompressor(), data, limit, out)


def decompress(compression: str, data: bytes, size: int, *, whole: bool) -> bytes:
    """A chunk's records from its stored bytes: exactly ``size`` bytes, or ``ChunkError``.

    Output is bounded by ``size`` whatever the stored bytes claim and is written into one buffer,
    so a decompression bomb costs at most ``size + 1`` bytes besides its stored bytes. A chunk cut
    short (``whole`` false) yields the prefix its stored bytes decode to before they end or stop
    decoding: how far a library gets into an incomplete block is its own, which is why the
    libraries' versions are part of the transform (ADR 0046 §1).
    """
    if compression not in COMPRESSIONS:
        raise ChunkError(ChunkFault.UNKNOWN_COMPRESSION)
    if compression == "none":
        out = data[: size + 1]
    else:
        buffer = io.BytesIO()
        try:
            (_bz2 if compression == "bz2" else _lz4)(data, size, buffer)
        except MemoryError:
            raise
        except Exception as exc:  # the libraries raise their own errors on hostile bytes
            if whole:
                raise ChunkError(ChunkFault.DECOMPRESSION) from exc
        out = buffer.getvalue()  # the buffer's own bytes: no copy
        del buffer
    if whole and len(out) != size:
        raise ChunkError(ChunkFault.SIZE)
    return out[:size]

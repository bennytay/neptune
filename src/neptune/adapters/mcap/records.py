"""The MCAP container, format version 0, read from bytes (https://mcap.dev/spec).

A file is the magic, a sequence of records, and the magic again. A record is an opcode (``u8``), a
content length (``u64``) and that many content bytes. Integers are little-endian; a string is a
``u32`` byte length and UTF-8 bytes; byte arrays, maps and arrays carry a ``u32`` byte length
(the Chunk's and the Attachment's data a ``u64`` one). Fields a later version adds go at the end
of a record, so a record holding more bytes than its fields is read and the rest left alone.

Everything here is pure: bytes in, parsed fields out, with every position kept so a citation can
name the exact bytes a value came from. Anything that does not parse is a ``FieldError``; the
caller turns it into a finding. Nothing here reads a source or allocates more than its input.
"""

import io
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Final

MAGIC: Final = b"\x89MCAP0\r\n"
FORMAT_VERSION: Final = "0"  # the magic's sixth byte, the format's major version
RECORD_HEADER: Final = 9  # opcode u8 + content length u64
FOOTER_CONTENT: Final = 20  # summary_start u64, summary_offset_start u64, summary_crc u32
FOOTER_RECORD: Final = RECORD_HEADER + FOOTER_CONTENT
TAIL: Final = FOOTER_RECORD + len(MAGIC)  # the footer record and the closing magic
FOOTER_CRC_SPAN: Final = RECORD_HEADER + 16  # the footer bytes the summary CRC covers
MESSAGE_FIELDS: Final = 22  # channel_id u16, sequence u32, log_time u64, publish_time u64
MESSAGE_RECORD: Final = RECORD_HEADER + MESSAGE_FIELDS  # the smallest whole Message record
MAX_CHUNK_BYTES_CEILING: Final = 256 * 1024 * 1024  # what the adapter's memory is declared for
MESSAGE_INDEX_ENTRY: Final = 16  # log_time u64, offset u64
CHANNEL_COUNT_ENTRY: Final = 10  # channel_id u16, count u64

# Statistics content offsets of the fields a citation names (spec: Statistics record).
STATISTICS_START_TIME: Final = 26
STATISTICS_END_TIME: Final = 34

# The registry of well-known encodings in the MCAP specification's appendix.
MESSAGE_ENCODINGS: Final = frozenset({"cbor", "cdr", "flatbuffer", "json", "protobuf", "ros1"})
SCHEMA_ENCODINGS: Final = frozenset(
    {"flatbuffer", "jsonschema", "omgidl", "protobuf", "ros1msg", "ros2idl", "ros2msg"}
)
COMPRESSIONS: Final = ("", "lz4", "zstd")


class Opcode(IntEnum):
    HEADER = 0x01
    FOOTER = 0x02
    SCHEMA = 0x03
    CHANNEL = 0x04
    MESSAGE = 0x05
    CHUNK = 0x06
    MESSAGE_INDEX = 0x07
    CHUNK_INDEX = 0x08
    ATTACHMENT = 0x09
    ATTACHMENT_INDEX = 0x0A
    STATISTICS = 0x0B
    METADATA = 0x0C
    METADATA_INDEX = 0x0D
    SUMMARY_OFFSET = 0x0E
    DATA_END = 0x0F


OPCODES: Final = frozenset(int(opcode) for opcode in Opcode)


def opcode_name(opcode: int) -> str:
    """``chunk`` for 0x06; ``0x80`` for an opcode the specification does not name."""
    return Opcode(opcode).name.lower() if opcode in OPCODES else f"0x{opcode:02x}"


class FieldError(ValueError):
    """A record's fields run past its content or hold an impossible value."""


@dataclass(frozen=True)
class Text:
    """A string field: its raw bytes and where they start in the record's content."""

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


class Fields:
    """Reads one record's content field by field, from ``start``, never past ``end``."""

    __slots__ = ("data", "end", "pos")

    def __init__(self, data: bytes, start: int = 0, end: int | None = None) -> None:
        self.data = data
        self.pos = start
        self.end = len(data) if end is None else end

    def take(self, size: int) -> int:
        """Skip ``size`` bytes, returning where they start."""
        start = self.pos
        if size < 0 or start + size > self.end:
            raise FieldError(f"a field of {size} bytes at {start} runs past the record")
        self.pos = start + size
        return start

    def _unpack(self, code: str, size: int) -> int:
        value: int = struct.unpack_from(code, self.data, self.take(size))[0]
        return value

    def u8(self) -> int:
        return self._unpack("<B", 1)

    def u16(self) -> int:
        return self._unpack("<H", 2)

    def u32(self) -> int:
        return self._unpack("<I", 4)

    def u64(self) -> int:
        return self._unpack("<Q", 8)

    def blob(self) -> tuple[int, int]:
        """A length-prefixed byte field: where its bytes start and how many there are."""
        length = self.u32()
        return self.take(length), length

    def text(self) -> Text:
        start, length = self.blob()
        return Text(self.data[start : start + length], start)

    def section(self) -> "Fields":
        """A ``u32``-prefixed map or array, as a reader over just its bytes."""
        start, length = self.blob()
        return Fields(self.data, start, start + length)

    @property
    def left(self) -> int:
        return self.end - self.pos


def record_header(data: bytes, at: int = 0) -> tuple[int, int]:
    """The opcode and content length of the record starting at ``data[at]``."""
    opcode, length = struct.unpack_from("<BQ", data, at)
    return int(opcode), int(length)


# --- Records ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Header:
    profile: Text
    library: Text


def parse_header(content: bytes) -> Header:
    fields = Fields(content)
    return Header(fields.text(), fields.text())


@dataclass(frozen=True)
class Footer:
    summary_start: int
    summary_offset_start: int
    summary_crc: int


def parse_footer(content: bytes) -> Footer:
    fields = Fields(content)
    return Footer(fields.u64(), fields.u64(), fields.u32())


@dataclass(frozen=True)
class Schema:
    id: int
    name: Text
    encoding: Text
    data: tuple[int, int]  # where the definition's bytes start in the content, and their length


def parse_schema(content: bytes) -> Schema:
    fields = Fields(content)
    schema_id = fields.u16()
    name, encoding = fields.text(), fields.text()
    return Schema(schema_id, name, encoding, fields.blob())


@dataclass(frozen=True)
class Channel:
    id: int
    schema_id: int
    topic: Text
    message_encoding: Text
    metadata: tuple[tuple[Text, Text], ...]


def parse_channel(content: bytes) -> Channel:
    fields = Fields(content)
    channel_id, schema_id = fields.u16(), fields.u16()
    topic, encoding = fields.text(), fields.text()
    entries = fields.section()
    metadata = []
    while entries.left:
        metadata.append((entries.text(), entries.text()))
    return Channel(channel_id, schema_id, topic, encoding, tuple(metadata))


@dataclass(frozen=True)
class MessageHead:
    channel_id: int
    sequence: int
    log_time: int
    publish_time: int


_MESSAGE: Final = struct.Struct("<HIQQ")


def parse_message_head(data: bytes, at: int = 0) -> MessageHead:
    """The fixed fields of a Message whose content starts at ``data[at]``; the payload follows."""
    if at + MESSAGE_FIELDS > len(data):
        raise FieldError("a Message record is shorter than its fixed fields")
    channel_id, sequence, log_time, publish_time = _MESSAGE.unpack_from(data, at)
    return MessageHead(channel_id, sequence, log_time, publish_time)


@dataclass(frozen=True)
class ChunkHead:
    start_time: int
    end_time: int
    uncompressed_size: int
    uncompressed_crc: int
    compression: Text
    records: tuple[int, int]  # where the records start in the content, and their declared length


def parse_chunk_head(content: bytes) -> ChunkHead:
    """A Chunk's fields up to its records; ``content`` may stop anywhere after them."""
    fields = Fields(content)
    start_time, end_time, size, crc = fields.u64(), fields.u64(), fields.u64(), fields.u32()
    compression = fields.text()
    length = fields.u64()
    return ChunkHead(start_time, end_time, size, crc, compression, (fields.pos, length))


@dataclass(frozen=True)
class MessageIndex:
    """A Message Index record: its channel, and where its entries are, read lazily."""

    channel_id: int
    entries: tuple[int, int]  # where the entries start in the content, and how many there are

    def read(self, content: bytes) -> Iterator[tuple[int, int]]:
        """Each entry, ``(log_time, offset in the chunk's records)``, in stored order."""
        start, count = self.entries
        return _INDEX_ENTRY.iter_unpack(memoryview(content)[start : start + count * 16])


_INDEX_ENTRY: Final = struct.Struct("<QQ")


def parse_message_index(content: bytes) -> MessageIndex:
    fields = Fields(content)
    channel_id = fields.u16()
    start, length = fields.blob()
    count, rest = divmod(length, MESSAGE_INDEX_ENTRY)
    if rest:
        raise FieldError("a Message Index's entries are not whole (log_time, offset) pairs")
    return MessageIndex(channel_id, (start, count))


@dataclass(frozen=True)
class ChunkIndex:
    start_time: int
    end_time: int
    chunk_start: int
    chunk_length: int
    message_index_offsets: tuple[tuple[int, int], ...]  # (channel id, file offset), as stored
    message_index_length: int
    compression: Text
    compressed_size: int
    uncompressed_size: int


def parse_chunk_index(content: bytes) -> ChunkIndex:
    fields = Fields(content)
    start_time, end_time, chunk_start, chunk_length = (fields.u64() for _ in range(4))
    offsets = fields.section()
    pairs = []
    while offsets.left:
        pairs.append((offsets.u16(), offsets.u64()))
    message_index_length = fields.u64()
    compression = fields.text()
    return ChunkIndex(
        start_time,
        end_time,
        chunk_start,
        chunk_length,
        tuple(pairs),
        message_index_length,
        compression,
        fields.u64(),
        fields.u64(),
    )


@dataclass(frozen=True)
class AttachmentHead:
    log_time: int
    create_time: int
    name: Text
    media_type: Text
    data: tuple[int, int]  # where the data starts in the content, and its length
    crc: int
    crc_at: int  # where the CRC field starts: the CRC covers every content byte before it


def parse_attachment_head(content: bytes, length: int) -> AttachmentHead:
    """An Attachment's fields from the bytes up to its data; ``length`` is the whole content's.

    ``content`` need not hold the data: the CRC that follows it is read separately by the
    caller when it is not there (``crc`` is then 0 and ``crc_at`` says where to read it).
    """
    fields = Fields(content)
    log_time, create_time = fields.u64(), fields.u64()
    name, media_type = fields.text(), fields.text()
    size = fields.u64()
    data = fields.pos
    if data + size + 4 > length:
        raise FieldError("an Attachment's data and CRC run past the record")
    crc_at = data + size
    crc = struct.unpack_from("<I", content, crc_at)[0] if crc_at + 4 <= len(content) else 0
    return AttachmentHead(log_time, create_time, name, media_type, (data, size), crc, crc_at)


@dataclass(frozen=True)
class AttachmentIndex:
    offset: int
    length: int
    log_time: int
    create_time: int
    data_size: int
    name: Text
    media_type: Text


def parse_attachment_index(content: bytes) -> AttachmentIndex:
    fields = Fields(content)
    offset, length, log_time, create_time, size = (fields.u64() for _ in range(5))
    return AttachmentIndex(
        offset, length, log_time, create_time, size, fields.text(), fields.text()
    )


@dataclass(frozen=True)
class Statistics:
    message_count: int
    schema_count: int
    channel_count: int
    attachment_count: int
    metadata_count: int
    chunk_count: int
    message_start_time: int
    message_end_time: int
    channel_message_counts: tuple[tuple[int, int, int], ...]  # (channel id, count, entry offset)


def parse_statistics(content: bytes) -> Statistics:
    fields = Fields(content)
    message_count, schema_count = fields.u64(), fields.u16()
    channel_count, attachment_count, metadata_count, chunk_count = (fields.u32() for _ in range(4))
    start_time, end_time = fields.u64(), fields.u64()
    entries = fields.section()
    counts = []
    while entries.left:
        at = entries.pos
        counts.append((entries.u16(), entries.u64(), at))
    return Statistics(
        message_count,
        schema_count,
        channel_count,
        attachment_count,
        metadata_count,
        chunk_count,
        start_time,
        end_time,
        tuple(counts),
    )


@dataclass(frozen=True)
class Metadata:
    name: Text
    entries: tuple[tuple[Text, Text], ...]


def parse_metadata(content: bytes) -> Metadata:
    fields = Fields(content)
    name = fields.text()
    entries = fields.section()
    pairs = []
    while entries.left:
        pairs.append((entries.text(), entries.text()))
    return Metadata(name, tuple(pairs))


@dataclass(frozen=True)
class MetadataIndex:
    offset: int
    length: int
    name: Text


def parse_metadata_index(content: bytes) -> MetadataIndex:
    fields = Fields(content)
    return MetadataIndex(fields.u64(), fields.u64(), fields.text())


# --- Records inside a chunk ---------------------------------------------------------------------


_RECORD_HEAD: Final = struct.Struct("<BQ")


def record_limit(max_chunk_bytes: int) -> int:
    """How many records a chunk's walk visits: as many as ``max_chunk_bytes`` holds Messages.

    No chunk within the size limit holds more Message records than this, so only a chunk of
    records smaller than any Message reaches it (ADR 0034 §7).
    """
    return max(1, max_chunk_bytes // MESSAGE_RECORD)


class InnerRecords:
    """The records in a chunk's uncompressed bytes, walked lazily: nothing is kept per record.

    Iterating yields ``(offset, opcode, length)`` for each whole record in order, at most ``most``
    of them. After a whole walk, ``framed`` is where the last whole record visited ends, ``cut``
    the offset of a record the bytes end inside, and ``stop`` the offset where the walk stopped
    after ``most`` records; each is ``None`` when it does not apply. A walk holds no more than
    the bytes it is given, however many records they hold.
    """

    __slots__ = ("cut", "data", "framed", "most", "stop")

    def __init__(self, data: bytes, most: int) -> None:
        self.data = data
        self.most = most
        self.framed = 0
        self.cut: int | None = None
        self.stop: int | None = None

    def __iter__(self) -> Iterator[tuple[int, int, int]]:
        data, size, most, unpack = self.data, len(self.data), self.most, _RECORD_HEAD.unpack_from
        self.cut = self.stop = None
        offset = count = 0
        while offset < size:
            if offset + RECORD_HEADER > size:
                self.cut = offset
                break
            opcode, length = unpack(data, offset)
            end = offset + RECORD_HEADER + length
            if end > size:
                self.cut = offset
                break
            if count == most:
                self.stop = offset
                break
            count += 1
            yield offset, opcode, length
            offset = end
        self.framed = offset


# --- Decompression ------------------------------------------------------------------------------


class ChunkFault(StrEnum):
    """Why a chunk's records could not be read."""

    UNKNOWN_COMPRESSION = "unknown_compression"
    DECOMPRESSION = "decompression"
    SIZE = "size"
    CRC = "crc"


class ChunkError(Exception):
    def __init__(self, fault: ChunkFault) -> None:
        super().__init__(str(fault))
        self.fault = fault


# Decompressed output is written in pieces of at most this, into one growing buffer.
_PIECE: Final = 4 * 1024 * 1024


def _zstd(data: bytes, limit: int, out: io.BytesIO) -> None:
    import zstandard

    reader = zstandard.ZstdDecompressor().stream_reader(
        io.BytesIO(data), read_across_frames=True, closefd=False
    )
    while out.tell() <= limit:
        piece = reader.read(min(_PIECE, limit + 1 - out.tell()))
        if not piece:
            break
        out.write(piece)


def _lz4(data: bytes, limit: int, out: io.BytesIO) -> None:
    """LZ4 frames, fed in pieces so the decompressor never holds a copy of the rest."""
    import lz4.frame

    view = memoryview(data)
    position, pending = 0, b""
    decompressor = lz4.frame.LZ4FrameDecompressor()
    while out.tell() <= limit:
        room = min(_PIECE, limit + 1 - out.tell())
        if decompressor.eof:  # a frame ended: the next one starts in what it did not use
            pending = decompressor.unused_data or b""
            decompressor = lz4.frame.LZ4FrameDecompressor()
            if not pending and position >= len(view):
                break
        if pending:
            piece, pending = decompressor.decompress(pending, max_length=room), b""
        elif decompressor.needs_input:
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


def decompress(compression: str, data: bytes, size: int, *, whole: bool) -> bytes:
    """A chunk's records from its stored bytes: exactly ``size`` bytes, or ``ChunkError``.

    Output is bounded by ``size`` whatever the stored bytes claim and is written into one buffer,
    so a decompression bomb costs at most ``size + 1`` bytes besides its stored bytes. A chunk cut
    short (``whole`` false) yields the prefix its stored bytes decode to before they end or stop
    decoding: how far a library gets into an incomplete block is its own, which is why the
    libraries' versions are part of the transform (ADR 0034 §1).
    """
    if compression not in COMPRESSIONS:
        raise ChunkError(ChunkFault.UNKNOWN_COMPRESSION)
    if compression == "":
        out = data[: size + 1]
    else:
        buffer = io.BytesIO()
        try:
            (_zstd if compression == "zstd" else _lz4)(data, size, buffer)
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


def check_crc(data: bytes, expected: int) -> None:
    """A CRC of 0 means the writer did not compute one (spec), so it is not checked."""
    if expected and zlib.crc32(data) != expected:
        raise ChunkError(ChunkFault.CRC)

"""Reading an MCAP source: places, the top-level records of a byte range, and chunks.

A ``Place`` is where a record is: one ``(offset, length)`` step in the file for a top-level
record, or the Chunk record's step plus an ``(offset, length)`` step in that chunk's uncompressed
records for a record inside a chunk. It is also the record's locator: two ``ByteRange`` steps,
the second inside what the first decodes to, exactly as MCAP's own Message Index counts offsets.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import SourceReader, read_pieces
from neptune.adapters.mcap.records import (
    RECORD_HEADER,
    ChunkError,
    ChunkFault,
    ChunkHead,
    FieldError,
    Inner,
    check_crc,
    decompress,
    inner_records,
    parse_chunk_head,
    record_header,
)
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import ByteRange

# A top-level record up to this size is read whole while scanning; a larger one is read in parts
# by whoever needs it (a chunk's records, an attachment's data), so a scan's memory is bounded.
WHOLE_RECORD: Final = 1024 * 1024
_SCAN_READ: Final = 64 * 1024
# A Chunk's fields before its records are a few dozen bytes; a compression name is a few more.
_CHUNK_HEAD_READ: Final = 4096


def read_exact(source: SourceReader, offset: int, length: int) -> bytes:
    """``length`` bytes from ``offset``; a reader that comes up short raises ``ShortReadError``."""
    if length == 0:
        return b""
    return b"".join(read_pieces(source, offset, offset + length))


@dataclass(frozen=True)
class Place:
    """Where a record or field is: a step in the file, then optionally one in a chunk's records."""

    steps: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        if len(self.steps) not in (1, 2):
            raise ValueError(f"a place is one or two (offset, length) steps: {self.steps}")

    @property
    def nested(self) -> bool:
        return len(self.steps) == 2

    def locator(self) -> tuple[ByteRange, ...]:
        return tuple(ByteRange(offset, length) for offset, length in self.steps)

    def within(self, offset: int, length: int) -> "Place":
        """The place of ``length`` bytes from ``offset`` of this record (header included)."""
        *outer, (start, _) = self.steps
        return Place((*outer, (start + offset, length)))

    def to_json(self) -> list[JsonValue]:
        return [[offset, length] for offset, length in self.steps]


def place_from_json(data: JsonValue) -> Place:
    if not isinstance(data, list):
        raise ValueError(f"a place is a list of steps: {data!r}")
    steps = []
    for step in data:
        if not isinstance(step, list) or len(step) != 2:
            raise ValueError(f"a place step is [offset, length]: {step!r}")
        offset, length = step
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in step):
            raise ValueError(f"a place step holds two non-negative integers: {step!r}")
        steps.append((int(offset), int(length)))
    return Place(tuple(steps))


@dataclass(frozen=True)
class TopRecord:
    """A record in the file. ``content`` is its content when it was read whole, else ``None``.

    ``cut`` means the record runs past the end of the range scanned: only ``present`` bytes of
    its declared content are there. When even its header is cut, ``opcode`` is -1 and
    ``present`` is negative, so ``place`` still covers exactly the bytes there are.
    """

    offset: int
    opcode: int
    length: int
    content: bytes | None
    cut: bool = False
    present: int = 0

    @property
    def end(self) -> int:
        return self.offset + RECORD_HEADER + self.length

    @property
    def place(self) -> Place:
        size = RECORD_HEADER + (self.present if self.cut else self.length)
        return Place(((self.offset, size),))


def scan(source: SourceReader, start: int, end: int) -> Iterator[TopRecord]:
    """The records of ``[start, end)`` in file order; the last is ``cut`` if it overruns ``end``.

    Reads in bounded pieces: small records come whole, larger ones only as their header.
    """
    buffer, at = b"", start
    pos = start
    while pos < end:
        if pos + RECORD_HEADER > at + len(buffer):
            buffer, at = read_exact(source, pos, min(_SCAN_READ, end - pos)), pos
        if pos + RECORD_HEADER > end:  # not even the header: ``place`` covers what is there
            yield TopRecord(pos, -1, 0, None, cut=True, present=end - pos - RECORD_HEADER)
            return
        opcode, length = record_header(buffer, pos - at)
        record_end = pos + RECORD_HEADER + length
        if record_end > end:
            yield TopRecord(pos, opcode, length, None, cut=True, present=end - pos - RECORD_HEADER)
            return
        content: bytes | None = None
        if record_end <= at + len(buffer):
            content = buffer[pos - at + RECORD_HEADER : record_end - at]
        elif RECORD_HEADER + length <= WHOLE_RECORD:
            buffer, at = read_exact(source, pos, RECORD_HEADER + length), pos
            content = buffer[RECORD_HEADER:]
        yield TopRecord(pos, opcode, length, content)
        pos = record_end


def content_of(source: SourceReader, record: TopRecord, start: int = 0, size: int = -1) -> bytes:
    """Bytes ``[start, start + size)`` of a record's content (to its end, or what is present)."""
    available = record.present if record.cut else record.length
    stop = available if size < 0 else min(available, start + size)
    if record.content is not None:
        return record.content[start:stop]
    return read_exact(source, record.offset + RECORD_HEADER + start, max(0, stop - start))


# --- Chunks -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenedChunk:
    """A chunk's records, uncompressed and checked, with the records found in them.

    ``partial`` when the Chunk record was cut short: ``data`` is then the prefix its stored bytes
    decode to, unchecked, and ``cut`` the offset of a record that prefix ends inside.
    """

    place: Place
    head: ChunkHead
    data: bytes
    records: Sequence[Inner]
    cut: int | None
    partial: bool


class ChunkProblem(Exception):
    """A chunk whose records cannot be read: why, and the declared sizes behind a limit."""

    def __init__(self, reason: str, head: ChunkHead | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.head = head


def open_chunk(source: SourceReader, record: TopRecord, limit: int) -> OpenedChunk:
    """Read, decompress and check one Chunk record, or raise ``ChunkProblem``.

    ``limit`` bounds the bytes held: a chunk declaring more stored or uncompressed bytes is not
    read. A cut record is read as far as it goes.
    """
    available = record.present if record.cut else record.length
    try:
        head = parse_chunk_head(content_of(source, record, 0, min(available, _CHUNK_HEAD_READ)))
    except FieldError:
        raise ChunkProblem("malformed") from None
    compression = head.compression.value
    start, length = head.records
    if not record.cut and start + length > record.length:
        raise ChunkProblem("malformed", head)
    if max(length, head.uncompressed_size) > limit:
        raise ChunkProblem("too_large", head)
    if compression is None:
        raise ChunkProblem(str(ChunkFault.UNKNOWN_COMPRESSION), head)
    stored = content_of(source, record, start, length)
    whole = len(stored) == length
    try:
        data = decompress(compression, stored, head.uncompressed_size, whole=whole)
        if whole:
            check_crc(data, head.uncompressed_crc)
    except ChunkError as exc:
        raise ChunkProblem(str(exc.fault), head) from None
    found, cut = inner_records(data)
    return OpenedChunk(record.place, head, data, found, cut, not whole)

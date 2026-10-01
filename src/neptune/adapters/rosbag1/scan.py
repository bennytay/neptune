"""Reading a bag source: places, the top-level records of a byte range, and chunks.

A ``Place`` is where a record is: one ``(offset, length)`` step in the file for a top-level
record, or the Chunk record's step plus an ``(offset, length)`` step in that chunk's uncompressed
records for a record inside a chunk. It is also the record's locator: two ``ByteRange`` steps,
the second inside what the first decodes to, offsets counted from the start of the chunk's
uncompressed data exactly as the bag's own Index Data records count them.
"""

import io
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import SourceReader, read_pieces
from neptune.adapters.rosbag1.records import (
    LENGTH,
    ChunkError,
    ChunkFault,
    ChunkHead,
    FieldError,
    Fields,
    InnerRecords,
    decompress,
    parse_chunk_head,
    parse_fields,
    record_limit,
)
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import ByteRange

# A record up to this size (or up to the header limit, if that is larger) is read whole while
# scanning; a larger one is read in parts by whoever needs it (a chunk's data), so a scan's memory
# is bounded.
WHOLE_RECORD: Final = 1024 * 1024
_SCAN_READ: Final = 64 * 1024


def read_exact(source: SourceReader, offset: int, length: int) -> bytes:
    """``length`` bytes from ``offset``; a reader that comes up short raises ``ShortReadError``.

    The pieces are written into one buffer as they come, so a read holds its bytes once.
    """
    if length == 0:
        return b""
    pieces = read_pieces(source, offset, offset + length)
    first = next(pieces)
    if len(first) == length:
        return first
    buffer = io.BytesIO()
    buffer.write(first)
    del first
    for piece in pieces:
        buffer.write(piece)
    return buffer.getvalue()


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

    def inner(self, offset: int, length: int) -> "Place":
        """The place of ``length`` bytes at ``offset`` in the uncompressed data of this Chunk
        record (a top-level place)."""
        (outer,) = self.steps
        return Place((outer, (offset, length)))

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
    """A record in the file.

    ``op`` is -1 when its header does not parse, is too large (``problem``) or is cut. ``fields``
    is its parsed header; its positions are relative to ``fields.data``, which is the record's own
    bytes only when ``raw`` is set. ``data_length`` is the declared length of its data, ``None``
    when the file ends before the length does. ``raw`` is the whole record when it was read whole,
    else ``None``. ``cut`` means the record runs past the end of the range scanned: ``end`` is then
    where the bytes there end, so ``place`` covers exactly what is present.
    """

    offset: int
    header_length: int
    data_length: int | None
    op: int
    fields: Fields | None
    raw: bytes | None
    end: int
    cut: bool = False
    problem: str | None = None  # "header_too_large" or "malformed"

    @property
    def data_offset(self) -> int:
        return self.offset + 2 * LENGTH + self.header_length

    @property
    def place(self) -> Place:
        return Place(((self.offset, self.end - self.offset),))

    @property
    def next(self) -> int:
        """Where the following record starts (meaningful when the record is not cut)."""
        return self.end


def scan(
    source: SourceReader, start: int, end: int, max_header: int, window: int = _SCAN_READ
) -> Iterator[TopRecord]:
    """The records of ``[start, end)`` in file order; the last is ``cut`` if it overruns ``end``.

    Reads in bounded pieces: small records come whole, larger ones only as their header. A header
    over ``max_header`` bytes is not read: its record is yielded with ``problem`` and the walk
    goes on from where its lengths say the data ends, so a lying length costs findings, not time.
    ``window`` is how much it reads ahead when it must read.
    """
    buffer, at = b"", start
    pos = start
    whole_limit = max(WHOLE_RECORD, max_header) + _SCAN_READ

    def have(offset: int, size: int) -> bool:
        return at <= offset and offset + size <= at + len(buffer)

    def load(offset: int, size: int) -> None:
        nonlocal buffer, at
        buffer, at = read_exact(source, offset, min(max(size, window), end - offset)), offset

    while pos < end:
        if pos + LENGTH > end:
            yield TopRecord(pos, 0, None, -1, None, None, end, cut=True)
            return
        if not have(pos, LENGTH):
            load(pos, LENGTH)
        (header_length,) = struct.unpack_from("<I", buffer, pos - at)
        header_end = pos + LENGTH + header_length
        if header_end + LENGTH > end:
            yield TopRecord(pos, header_length, None, -1, None, None, end, cut=True)
            return
        fields: Fields | None = None
        problem: str | None = None
        op = -1
        if header_length > max_header:
            problem = "header_too_large"
            if not have(header_end, LENGTH):
                load(header_end, LENGTH)
        else:
            if not have(pos, header_length + 2 * LENGTH):
                load(pos, header_length + 2 * LENGTH)
            try:
                parsed = parse_fields(buffer, pos - at + LENGTH, pos - at + LENGTH + header_length)
            except FieldError:
                problem = "malformed"
            else:
                fields = Fields(buffer, parsed)
                raw_op = fields.value(b"op")
                if raw_op is not None and len(raw_op) == 1:
                    op = raw_op[0]
                else:
                    problem = "malformed"
        (data_length,) = struct.unpack_from("<I", buffer, header_end - at)
        record_end = header_end + LENGTH + data_length
        if record_end > end:
            yield TopRecord(
                pos, header_length, data_length, op, fields, None, end, cut=True, problem=problem
            )
            return
        raw: bytes | None = None
        if problem is None and record_end - pos <= whole_limit:
            if not have(pos, record_end - pos):
                load(pos, record_end - pos)
            raw = buffer[pos - at : record_end - at]
            # the parsed fields point into the window; re-parse over the record's own bytes
            fields = Fields(raw, parse_fields(raw, LENGTH, LENGTH + header_length))
        yield TopRecord(
            pos, header_length, data_length, op, fields, raw, record_end, problem=problem
        )
        pos = record_end


def whole(source: SourceReader, record: TopRecord) -> bytes:
    """The record's bytes, read if the scan did not hold them. The caller bounds the size."""
    if record.raw is not None:
        return record.raw
    return read_exact(source, record.offset, record.end - record.offset)


# --- Chunks -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenedChunk:
    """A chunk's records, uncompressed and checked.

    ``partial`` when the Chunk record was cut short: ``data`` is then the prefix its stored bytes
    decode to, unchecked. ``records`` walks the records lazily, at most ``most`` of them.
    """

    place: Place
    head: ChunkHead
    stored: int  # the bytes the Chunk record declares for its data
    data: bytes
    partial: bool
    most: int

    def records(self) -> InnerRecords:
        return InnerRecords(self.data, self.most)


class ChunkProblem(Exception):
    """A chunk whose records cannot be read: why, and the declared sizes behind a limit."""

    def __init__(self, reason: str, head: ChunkHead | None = None, stored: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.head = head
        self.stored = stored


def chunk_head(record: TopRecord) -> ChunkHead:
    """The Chunk record's compression and declared size, or ``FieldError``."""
    if record.fields is None or record.op != 5:
        raise FieldError("not a Chunk record")
    return parse_chunk_head(record.fields)


def open_chunk(source: SourceReader, record: TopRecord, limit: int) -> OpenedChunk:
    """Read, decompress and check one Chunk record, or raise ``ChunkProblem``.

    ``limit`` bounds the bytes held: a chunk declaring more stored or uncompressed bytes is not
    read. A cut record is read as far as it goes. The caller has checked that the record's header
    parsed (``chunk_head`` raises ``FieldError`` otherwise, here ``malformed``).
    """
    try:
        head = chunk_head(record)
    except FieldError:
        raise ChunkProblem("malformed") from None
    declared = record.data_length
    if declared is None:
        # the file ends inside the data length: nothing of the data is there
        declared, available = 0, 0
    else:
        available = declared if not record.cut else record.end - record.data_offset
    if max(declared, head.size) > limit:
        raise ChunkProblem("too_large", head, declared)
    compression = head.compression.value
    if compression is None or compression not in ("none", "bz2", "lz4"):
        raise ChunkProblem(str(ChunkFault.UNKNOWN_COMPRESSION), head, declared)
    whole_chunk = not record.cut
    try:
        # The stored bytes are only the call's argument, so they are freed once decompressed.
        data = decompress(
            compression,
            read_exact(source, record.data_offset, max(0, available)),
            head.size,
            whole=whole_chunk,
        )
    except ChunkError as exc:
        raise ChunkProblem(str(exc.fault), head, declared) from None
    return OpenedChunk(record.place, head, declared, data, not whole_chunk, record_limit(limit))

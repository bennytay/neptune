"""An MCAP source's head and tail: the magic, the Header, the Footer and the summary.

What ``inspect`` and ``plan`` read first (ADR 0034): the magic and the Header record at the start;
the Footer and the closing magic at the end, and through them the summary section, checked against
its CRC and parsed whole. A usable summary declares the schemas, channels, statistics and the
chunk, attachment and metadata indexes. ``ranges`` decides from it how the data section is cut.
"""

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.contract import SourceReader
from neptune.adapters.mcap.records import (
    FOOTER_CONTENT,
    FOOTER_CRC_SPAN,
    MAGIC,
    RECORD_HEADER,
    TAIL,
    AttachmentIndex,
    Channel,
    ChunkError,
    ChunkIndex,
    FieldError,
    Footer,
    Header,
    MetadataIndex,
    Opcode,
    Schema,
    Statistics,
    check_crc,
    opcode_name,
    parse_attachment_index,
    parse_channel,
    parse_chunk_index,
    parse_footer,
    parse_header,
    parse_metadata_index,
    parse_schema,
    parse_statistics,
    record_header,
)
from neptune.adapters.mcap.scan import Place, read_exact

DATA_START: Final = len(MAGIC)
# A summary (or a Header) larger than this is not read: planning holds the summary in memory.
MAX_SUMMARY_BYTES: Final = 64 * 1024 * 1024


# --- Head and tail ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Head:
    """The magic, and the Header record when the file starts with a well-formed one."""

    magic: bool
    header: tuple[Place, Header] | None


def read_head(source: SourceReader) -> Head:
    if source.size < len(MAGIC) or read_exact(source, 0, len(MAGIC)) != MAGIC:
        return Head(False, None)
    if source.size < DATA_START + RECORD_HEADER:
        return Head(True, None)
    opcode, length = record_header(read_exact(source, DATA_START, RECORD_HEADER))
    end = DATA_START + RECORD_HEADER + length
    if opcode != Opcode.HEADER or end > source.size or length > MAX_SUMMARY_BYTES:
        return Head(True, None)
    try:
        header = parse_header(read_exact(source, DATA_START + RECORD_HEADER, length))
    except FieldError:
        return Head(True, None)
    return Head(True, (Place(((DATA_START, RECORD_HEADER + length),)), header))


@dataclass(frozen=True)
class Declared:
    """A Schema or Channel record: where it is and a digest of its content, to compare repeats."""

    place: Place
    digest: bytes


def _digest(content: bytes) -> bytes:
    return hashlib.sha256(content).digest()


@dataclass
class Summary:
    """A summary section that passed its CRC and parsed whole."""

    start: int
    end: int
    schemas: dict[int, tuple[Declared, Schema]] = field(default_factory=dict)
    channels: dict[int, tuple[Declared, Channel]] = field(default_factory=dict)
    chunk_indexes: list[tuple[Place, ChunkIndex]] = field(default_factory=list)
    attachment_indexes: list[tuple[Place, AttachmentIndex]] = field(default_factory=list)
    metadata_indexes: list[tuple[Place, MetadataIndex]] = field(default_factory=list)
    statistics: tuple[Place, Statistics] | None = None
    records: Counter[str] = field(default_factory=Counter)


def _parse_summary(data: bytes, start: int) -> Summary:
    """Every record of a summary section whose bytes ``data`` start at file offset ``start``."""
    summary = Summary(start, start + len(data))
    offset = 0
    while offset < len(data):
        if offset + RECORD_HEADER > len(data):
            raise FieldError("the summary ends inside a record's header")
        opcode, length = record_header(data, offset)
        end = offset + RECORD_HEADER + length
        if end > len(data):
            raise FieldError("a summary record runs past the summary")
        content = data[offset + RECORD_HEADER : end]
        place = Place(((start + offset, RECORD_HEADER + length),))
        summary.records[opcode_name(opcode)] += 1
        if opcode == Opcode.SCHEMA:
            schema = parse_schema(content)
            if schema.id == 0 or schema.id in summary.schemas:
                raise FieldError(f"schema id {schema.id} is 0 or repeats in the summary")
            summary.schemas[schema.id] = (Declared(place, _digest(content)), schema)
        elif opcode == Opcode.CHANNEL:
            channel = parse_channel(content)
            if channel.id in summary.channels:
                raise FieldError(f"channel id {channel.id} repeats in the summary")
            summary.channels[channel.id] = (Declared(place, _digest(content)), channel)
        elif opcode == Opcode.CHUNK_INDEX:
            summary.chunk_indexes.append((place, parse_chunk_index(content)))
        elif opcode == Opcode.ATTACHMENT_INDEX:
            summary.attachment_indexes.append((place, parse_attachment_index(content)))
        elif opcode == Opcode.METADATA_INDEX:
            summary.metadata_indexes.append((place, parse_metadata_index(content)))
        elif opcode == Opcode.STATISTICS:
            if summary.statistics is not None:
                raise FieldError("the summary holds two Statistics records")
            summary.statistics = (place, parse_statistics(content))
        offset = end
    return summary


@dataclass(frozen=True)
class Tail:
    """The Footer, when the file ends with one and the magic, and the summary it points at.

    ``problem`` says why a summary the footer points at is not used, and the place to cite.
    """

    footer: tuple[Place, Footer] | None
    summary: Summary | None
    problem: tuple[str, Place] | None = None

    @property
    def footer_offset(self) -> int | None:
        return None if self.footer is None else self.footer[0].steps[0][0]


def read_tail(source: SourceReader) -> Tail:
    """The footer and the summary, if the file ends with them and they hold together."""
    size = source.size
    if size < DATA_START + TAIL:
        return Tail(None, None)
    tail = read_exact(source, size - TAIL, TAIL)
    opcode, length = record_header(tail)
    if tail[-len(MAGIC) :] != MAGIC or opcode != Opcode.FOOTER or length != FOOTER_CONTENT:
        return Tail(None, None)
    footer_offset = size - TAIL
    footer_place = Place(((footer_offset, RECORD_HEADER + length),))
    footer = parse_footer(tail[RECORD_HEADER : RECORD_HEADER + length])
    found = (footer_place, footer)
    start = footer.summary_start
    if start == 0:
        return Tail(found, None)
    offsets = footer.summary_offset_start
    if not DATA_START <= start <= footer_offset or (
        offsets and not start <= offsets <= footer_offset
    ):
        return Tail(found, None, ("bounds", footer_place))
    if footer_offset - start > MAX_SUMMARY_BYTES:
        return Tail(found, None, ("too_large", footer_place))
    data = read_exact(source, start, footer_offset - start + FOOTER_CRC_SPAN)
    summary_place = Place(((start, footer_offset - start),))
    try:
        check_crc(data, footer.summary_crc)
    except ChunkError:
        return Tail(found, None, ("crc", summary_place))
    try:
        summary = _parse_summary(data[: footer_offset - start], start)
    except (FieldError, ValueError):
        return Tail(found, None, ("malformed", summary_place))
    return Tail(found, summary)


# --- The directory: the record each schema and channel is read from ----------------------------


@dataclass
class Directory:
    """The declaration each schema and channel id is read from, and the channels' fields."""

    schemas: dict[int, Declared] = field(default_factory=dict)
    channels: dict[int, tuple[Declared, Channel]] = field(default_factory=dict)

    @classmethod
    def of(cls, summary: Summary | None) -> "Directory":
        if summary is None:
            return cls()
        return cls(
            {i: declared for i, (declared, _) in summary.schemas.items()}, dict(summary.channels)
        )

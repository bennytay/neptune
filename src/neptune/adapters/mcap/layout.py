"""Planning an MCAP source: its head, its tail and summary, and how its data section is cut.

``plan`` reads as little as the file allows (ADR 0034):

1. **Head.** The magic and the Header record.
2. **Tail.** The Footer and the closing magic; through them the summary section, checked against
   its CRC and parsed whole. A usable summary declares the schemas, channels and statistics.
3. **Data section, indexed.** When the summary indexes every chunk, the chunk indexes alone give
   the chunks' places and, through the Message Index records' offsets, each chunk's message count
   per channel; the statistics confirm the counts. Of the data section, only the 9-byte head of
   each record a planned chunk starts at is read, to confirm the index.
4. **Data section, scanned.** Otherwise every top-level record is visited once and every chunk
   decompressed once, to find the declarations and count the messages. This is the price of a
   file without a usable index: cut short, its summary damaged, or written without one.

The data section is cut into contiguous byte ranges at record boundaries, each holding at most
``chunk_bytes`` bytes and ``max_rows`` messages; a chunk holding more messages than that is read
by several planned chunks, each emitting one stretch of its messages. Every planned chunk gets,
per channel, the ``seq`` its rows start from and how many messages it holds, so rows are numbered
in source order however the file is cut.
"""

import hashlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.contract import SourceReader
from neptune.adapters.mcap.records import (
    FOOTER_CONTENT,
    FOOTER_CRC_SPAN,
    MAGIC,
    MESSAGE_INDEX_ENTRY,
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
from neptune.adapters.mcap.report import Reporter, Selection
from neptune.adapters.mcap.scan import (
    ChunkProblem,
    OpenedChunk,
    Place,
    TopRecord,
    content_of,
    open_chunk,
    read_exact,
    scan,
)
from neptune.model.finding import FindingCategory, IngestFinding, Severity

DATA_START: Final = len(MAGIC)
# A summary (or a Header) larger than this is not read: planning holds the summary in memory.
MAX_SUMMARY_BYTES: Final = 256 * 1024 * 1024


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


# --- Ranges ---------------------------------------------------------------------------------------


@dataclass
class Range:
    """One planned chunk of the data section: ``[start, end)`` and its messages per channel.

    ``top`` counts messages outside chunks and ``chunked`` those inside; the layout decides which
    get rows. ``first``/``last`` select one stretch of a chunk read by several planned chunks.
    ``decode`` false: the range holds no selected message, and its chunks are not decompressed.
    """

    start: int
    end: int = -1
    top: Counter[int] = field(default_factory=Counter)
    chunked: Counter[int] = field(default_factory=Counter)
    size: int = 0
    decode: bool = True
    first: int | None = None
    last: int | None = None

    @property
    def rows(self) -> int:
        return sum(self.top.values()) + sum(self.chunked.values())

    def counts(self, chunked: bool) -> Counter[int]:
        return self.chunked if chunked else self.top


@dataclass(frozen=True)
class Unit:
    """A stretch of the data section that planning never cuts: a record, or a chunk and what
    follows it up to the next chunk. ``messages`` is the channel of each message in a chunk, in
    order, when planning read them; ``confirm`` is a Chunk record to confirm where a range starts.
    """

    start: int
    size: int
    top: Counter[int]
    chunked: Counter[int]
    messages: list[int] | None = None
    skip: bool = False
    confirm: tuple[int, int] | None = None

    @property
    def rows(self) -> int:
        return sum(self.top.values()) + sum(self.chunked.values())


class Grouper:
    """Cuts units into ranges of at most ``chunk_bytes`` bytes and ``max_rows`` messages."""

    def __init__(self, chunk_bytes: int, max_rows: int) -> None:
        self.chunk_bytes = chunk_bytes
        self.max_rows = max_rows
        self.ranges: list[Range] = []
        self.confirm: list[tuple[int, int]] = []
        self._open: Range | None = None

    def add(self, unit: Unit) -> None:
        current = self._open
        if not unit.skip and unit.rows > self.max_rows and unit.messages is not None:
            self._open = None
            for first in range(0, len(unit.messages), self.max_rows):
                part = unit.messages[first : first + self.max_rows]
                self.ranges.append(
                    Range(
                        unit.start,
                        chunked=Counter(part),
                        size=unit.size,
                        first=first,
                        last=first + len(part),
                    )
                )
            if unit.confirm is not None:
                self.confirm.append(unit.confirm)
            return
        if (
            current is None
            or current.decode == unit.skip
            or (
                current.size > 0
                and (
                    current.size + unit.size > self.chunk_bytes
                    or (not unit.skip and current.rows + unit.rows > self.max_rows)
                )
            )
        ):
            current = self._open = Range(unit.start, decode=not unit.skip)
            self.ranges.append(current)
            if unit.confirm is not None:
                self.confirm.append(unit.confirm)
        current.top.update(unit.top)
        current.chunked.update(unit.chunked)
        current.size += unit.size

    def finish(self, end: int) -> list[Range]:
        """The ranges, each ending where the next starts and the last at ``end``."""
        if not self.ranges:
            self.ranges.append(Range(DATA_START))
        self.ranges[0].start = DATA_START  # the first range also holds the Header and what follows
        following = [r.start for r in self.ranges[1:]] + [end]
        for current, stop in zip(self.ranges, following, strict=True):
            current.end = stop
        return self.ranges


@dataclass
class Layout:
    """How the data section is planned: chunked or not, its ranges, and what planning found."""

    chunked: bool
    ranges: list[Range]
    findings: list[IngestFinding] = field(default_factory=list)
    data_end: bool = False  # the data section ends with its Data End record


def _messages(chunk: OpenedChunk) -> list[int]:
    """The channel id of each message in a chunk, in order (as ingest counts them)."""
    return [
        int.from_bytes(chunk.data[inner.content : inner.content + 2], "little")
        for inner in chunk.records
        if inner.opcode == Opcode.MESSAGE and inner.length >= 2
    ]


# --- Indexed layout -----------------------------------------------------------------------------


def _index_counts(index: ChunkIndex) -> Counter[int] | None:
    """Messages per channel from the Message Index records' offsets alone, or ``None``.

    The records follow the chunk back to back, so each one's length is the gap to the next, and a
    record of ``n`` entries holds ``9 + 6 + 16 n`` bytes. Anything else means the offsets cannot be
    read this way, and the chunk is counted by decompressing it.
    """
    pairs = sorted((offset, channel) for channel, offset in index.message_index_offsets)
    if not pairs or len({channel for _, channel in pairs}) != len(pairs):
        return None
    section = index.chunk_start + index.chunk_length
    bounds = [offset for offset, _ in pairs] + [section + index.message_index_length]
    if bounds[0] != section:
        return None
    counts: Counter[int] = Counter()
    for (offset, channel), end in zip(pairs, bounds[1:], strict=False):
        entries, remainder = divmod(end - offset - RECORD_HEADER - 6, MESSAGE_INDEX_ENTRY)
        if end - offset < RECORD_HEADER + 6 or remainder:
            return None
        counts[channel] = entries
    return counts


def _chunk_at(source: SourceReader, offset: int, length: int) -> TopRecord | None:
    """The Chunk record at ``offset`` if one of ``length`` bytes starts there."""
    if length < RECORD_HEADER or offset + length > source.size:
        return None
    opcode, declared = record_header(read_exact(source, offset, RECORD_HEADER))
    if opcode != Opcode.CHUNK or RECORD_HEADER + declared != length:
        return None
    return TopRecord(offset, opcode, declared, None)


def _chunk_messages(source: SourceReader, index: ChunkIndex, limit: int) -> list[int] | None:
    record = _chunk_at(source, index.chunk_start, index.chunk_length)
    if record is None:
        return None
    try:
        return _messages(open_chunk(source, record, limit))
    except ChunkProblem:
        return None


def _indexed(
    source: SourceReader,
    summary: Summary,
    directory: Directory,
    selection: Selection,
    limit: int,
    grouper: Grouper,
) -> Layout | tuple[str, Place] | None:
    """Ranges from the chunk indexes; ``None`` when there are none, else why they are wrong."""
    indexes = sorted(summary.chunk_indexes, key=lambda item: item[1].chunk_start)
    statistics = summary.statistics
    if not indexes:
        return None
    if statistics is not None and statistics[1].chunk_count != len(indexes):
        return "chunk_count", statistics[0]
    previous = DATA_START
    for place, index in indexes:
        end = index.chunk_start + index.chunk_length + index.message_index_length
        if index.chunk_start < previous or index.chunk_length < RECORD_HEADER:
            return "overlap", place
        if end > summary.start:
            return "bounds", place
        previous = end

    counts: list[Counter[int]] = []
    read: dict[int, list[int]] = {}
    unreadable = False
    for _, index in indexes:
        found = _index_counts(index)
        if found is None:
            messages = _chunk_messages(source, index, limit)
            unreadable = unreadable or messages is None
            read[index.chunk_start] = messages or []
            found = Counter(messages or [])
        counts.append(found)
    if statistics is not None and not unreadable:
        place, stats = statistics
        totals: Counter[int] = Counter()
        for found in counts:
            totals.update(found)
        if sum(totals.values()) != stats.message_count:
            return "message_count", place
        declared = {channel: count for channel, count, _ in stats.channel_message_counts if count}
        if declared and declared != {c: n for c, n in totals.items() if n}:
            return "channel_message_counts", place

    if indexes[0][1].chunk_start > DATA_START:
        grouper.add(Unit(DATA_START, indexes[0][1].chunk_start - DATA_START, Counter(), Counter()))
    following = [index.chunk_start for _, index in indexes[1:]] + [summary.start]
    for (_, index), found, stop in zip(indexes, counts, following, strict=True):
        live = [c for c, n in found.items() if n]
        wanted = any(
            c not in directory.channels or selection.selects(directory.channels[c][1].topic)
            for c in live
        )
        skip = selection.active and (
            not wanted or not selection.overlaps(index.start_time, index.end_time)
        )
        messages = read.get(index.chunk_start)
        if not skip and sum(found.values()) > grouper.max_rows and messages is None:
            messages = _chunk_messages(source, index, limit)
        grouper.add(
            Unit(
                index.chunk_start,
                stop - index.chunk_start,
                Counter(),
                found,
                messages,
                skip,
                (index.chunk_start, index.chunk_length),
            )
        )
    for offset, length in grouper.confirm:
        if _chunk_at(source, offset, length) is None:
            place = next(p for p, i in indexes if i.chunk_start == offset)
            return "chunk_place", place
    return Layout(True, grouper.finish(summary.start), data_end=True)


# --- Scanned layout -----------------------------------------------------------------------------


@dataclass
class _Scan:
    """What a scan of the data section has found so far."""

    reporter: Reporter
    directory: Directory
    findings: list[IngestFinding] = field(default_factory=list)
    chunks: bool = False

    def declare(self, opcode: int, content: bytes, place: Place) -> None:
        """A Schema or Channel record: the first declaration of its id, or a repeat to compare."""
        try:
            parsed: Schema | Channel = (
                parse_schema(content) if opcode == Opcode.SCHEMA else parse_channel(content)
            )
        except FieldError:
            return  # ingest reports the malformed record where it finds it
        declared = Declared(place, _digest(content))
        if isinstance(parsed, Schema):
            if parsed.id == 0:
                return
            known = self.directory.schemas.setdefault(parsed.id, declared)
        else:
            known = self.directory.channels.setdefault(parsed.id, (declared, parsed))[0]
        if known.digest != declared.digest:
            what = "schema" if isinstance(parsed, Schema) else "channel"
            self.findings.append(
                self.reporter.finding(
                    "conflicting_declaration",
                    FindingCategory.INCONSISTENT,
                    Severity.WARNING,
                    place,
                    f"this {what} record declares {what} id {parsed.id} unlike its first"
                    " declaration, which is the one read",
                    {"id": parsed.id, "record": what},
                    related=(known.place,),
                )
            )


def _scanned(
    source: SourceReader,
    reporter: Reporter,
    directory: Directory,
    tail: Tail,
    limit: int,
    grouper: Grouper,
) -> Layout:
    """Ranges from one pass over the data section, decompressing every chunk to count it."""
    found = _Scan(reporter, directory)
    stop = source.size if tail.footer_offset is None else tail.footer_offset
    end, data_end = stop, False
    for record in scan(source, DATA_START, stop):
        if record.cut:
            break
        top: Counter[int] = Counter()
        chunked: Counter[int] = Counter()
        messages: list[int] | None = None
        opcode = record.opcode
        if opcode in (Opcode.SCHEMA, Opcode.CHANNEL) and record.length <= limit:
            found.declare(opcode, content_of(source, record), record.place)
        elif opcode == Opcode.MESSAGE and record.length >= 2:
            top[int.from_bytes(content_of(source, record, 0, 2), "little")] += 1
        elif opcode == Opcode.CHUNK:
            found.chunks = True
            try:
                chunk = open_chunk(source, record, limit)
            except ChunkProblem:
                pass  # ingest reports it; its messages have no rows, so it counts none
            else:
                for inner in chunk.records:
                    if inner.opcode in (Opcode.SCHEMA, Opcode.CHANNEL):
                        place = Place((chunk.place.steps[0], (inner.offset, inner.end - inner.offset)))
                        found.declare(inner.opcode, chunk.data[inner.content : inner.end], place)
                messages = _messages(chunk)
                chunked.update(messages)
        elif opcode == Opcode.DATA_END:
            end, data_end = record.end, True
            grouper.add(Unit(record.offset, record.end - record.offset, top, chunked))
            break
        elif opcode == Opcode.FOOTER:
            end = record.offset
            break
        grouper.add(Unit(record.offset, record.end - record.offset, top, chunked, messages))
    return Layout(found.chunks, grouper.finish(end), found.findings, data_end)


# --- The plan's layout ----------------------------------------------------------------------------


def plan_layout(
    source: SourceReader,
    reporter: Reporter,
    tail: Tail,
    directory: Directory,
    selection: Selection,
    limit: int,
    chunk_bytes: int,
    max_rows: int,
) -> Layout:
    """The indexed layout if the summary's index holds, else the scanned one."""
    findings: list[IngestFinding] = []
    if tail.summary is not None:
        indexed = _indexed(
            source, tail.summary, directory, selection, limit, Grouper(chunk_bytes, max_rows)
        )
        if isinstance(indexed, Layout):
            return indexed
        if indexed is not None:
            reason, place = indexed
            findings.append(
                reporter.finding(
                    "index_invalid",
                    FindingCategory.INCONSISTENT,
                    Severity.WARNING,
                    place,
                    f"the summary's chunk index does not hold ({reason}); the data section is"
                    " planned by scanning it",
                    {"reason": reason},
                )
            )
    layout = _scanned(source, reporter, directory, tail, limit, Grouper(chunk_bytes, max_rows))
    layout.findings[:0] = findings
    if selection.active:
        for current in layout.ranges:
            current.decode = any(
                c not in directory.channels or selection.selects(directory.channels[c][1].topic)
                for c, n in current.counts(layout.chunked).items()
                if n
            )
    return layout


def seq_starts(ranges: Iterable[Range], chunked: bool) -> list[dict[int, int]]:
    """For each range, the ``seq`` each channel's first row in it gets: messages before it."""
    seen: Counter[int] = Counter()
    starts = []
    for current in ranges:
        counts = current.counts(chunked)
        starts.append({channel: seen[channel] for channel in counts})
        seen.update(counts)
    return starts

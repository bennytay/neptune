"""How ``plan`` cuts an MCAP data section into chunks (ADR 0034).

- **Indexed.** When the summary indexes every chunk and its counts agree with the statistics,
  the chunk indexes alone give each chunk's place and, through the Message Index records' offsets,
  its message count per channel. Only the first and last chunk's heads are read to confirm the
  index; every other chunk is confirmed by the planned chunk that reads it. A chunk whose index
  gives no counts is decompressed once to count it.
- **Scanned.** Otherwise every top-level record is visited once and every chunk decompressed
  once, to find the declarations and count the messages: the price of a file without a usable
  index (cut short, its summary damaged, or written without one).

A **unit** is what planning never cuts: a chunk with what follows it up to the next chunk
(indexed) or with its Message Index records (scanned), or any other record. Ranges of whole units
hold at most ``chunk_bytes`` bytes and ``max_rows`` messages; a chunk with more messages than that
is read by several planned chunks, each emitting one stretch of its messages, the last stretch
open-ended. Every count a planned chunk relies on is one its ``ingest`` derives again from the
same bytes, and every finding cites one record or one chunk, so nothing in the output depends on
where the ranges fall.
"""

import hashlib
import struct
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from neptune.adapters.contract import SourceReader
from neptune.adapters.mcap.layout import DATA_START, Declared, Directory, Summary, Tail
from neptune.adapters.mcap.records import (
    INT64_MAX,
    MESSAGE_FIELDS,
    MESSAGE_INDEX_ENTRY,
    RECORD_HEADER,
    Channel,
    ChunkIndex,
    FieldError,
    Opcode,
    Schema,
    parse_channel,
    parse_message_head,
    parse_schema,
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

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue


@dataclass
class Range:
    """One planned data chunk: ``[start, end)``, its messages per channel, and the chunk index
    records of the chunks in it (indexed layout). ``first``/``last`` select one stretch of a
    chunk's messages when several planned chunks read it; ``last`` is ``None`` on the final one.
    """

    start: int
    end: int = -1
    top: Counter[int] = field(default_factory=Counter)
    chunked: Counter[int] = field(default_factory=Counter)
    size: int = 0
    rows: int = 0
    indexes: list[Place] = field(default_factory=list)
    first: int | None = None
    last: int | None = None

    def counts(self, chunked: bool) -> Counter[int]:
        return self.chunked if chunked else self.top


@dataclass(frozen=True)
class Unit:
    """What planning never cuts. ``rows`` counts the messages that may get rows here."""

    start: int
    size: int
    top: Counter[int]
    chunked: Counter[int]
    rows: int
    chunk: bool = False
    index: Place | None = None


class Grouper:
    """Cuts units into ranges of at most ``chunk_bytes`` bytes and ``max_rows`` messages."""

    def __init__(self, chunk_bytes: int, max_rows: int) -> None:
        self.chunk_bytes = chunk_bytes
        self.max_rows = max_rows
        self.ranges: list[Range] = []
        self._open: Range | None = None

    def add(self, unit: Unit) -> None:
        indexes = [unit.index] if unit.index is not None else []
        if unit.chunk and unit.rows > self.max_rows:
            self._open = None
            for first in range(0, unit.rows, self.max_rows):
                final = first + self.max_rows >= unit.rows
                self.ranges.append(
                    Range(
                        unit.start,
                        top=unit.top,
                        chunked=unit.chunked,
                        size=unit.size,
                        rows=min(self.max_rows, unit.rows - first),
                        indexes=indexes,
                        first=first,
                        last=None if final else first + self.max_rows,
                    )
                )
            return
        current = self._open
        if current is None or (
            current.size > 0
            and (
                current.size + unit.size > self.chunk_bytes
                or current.rows + unit.rows > self.max_rows
            )
        ):
            current = self._open = Range(unit.start)
            self.ranges.append(current)
        current.top.update(unit.top)
        current.chunked.update(unit.chunked)
        current.size += unit.size
        current.rows += unit.rows
        current.indexes += indexes

    def finish(self, end: int) -> list[Range]:
        """The ranges, each ending where the next starts and the last at ``end``."""
        if not self.ranges:
            self.ranges.append(Range(DATA_START))
        self.ranges[0].start = DATA_START  # the first range also holds the Header and what follows
        stop = end
        for current in reversed(self.ranges):  # the stretches of one chunk share its range
            current.end = stop
            if current.first in (None, 0):
                stop = current.start
        return self.ranges


@dataclass
class Layout:
    """How the data section is planned: chunked or not, its ranges, and what planning found."""

    chunked: bool
    indexed: bool
    ranges: list[Range]
    findings: list[IngestFinding] = field(default_factory=list)
    data_end: bool = False  # the data section ends with its Data End record


CHANNEL_ID: Final = struct.Struct("<H")  # a Message's first field


def count_messages(chunk: OpenedChunk) -> Counter[int]:
    """A chunk's messages per channel, as ingest counts them, holding nothing per message."""
    data, read_channel = chunk.data, CHANNEL_ID.unpack_from
    counts: Counter[int] = Counter()
    for offset, opcode, length in chunk.records():
        if opcode == Opcode.MESSAGE and length >= 2:
            counts[read_channel(data, offset + RECORD_HEADER)[0]] += 1
    return counts


# --- Indexed layout -----------------------------------------------------------------------------


def index_counts(index: ChunkIndex) -> Counter[int] | None:
    """A chunk's messages per channel from its Message Index records' offsets alone, or ``None``.

    The records follow the chunk back to back, so each one's length is the gap to the next, and a
    record of ``n`` entries holds ``9 + 6 + 16 n`` bytes. Anything else means the offsets cannot be
    read this way: the chunk is counted by decompressing it, at planning and again at ingest.
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


def skipped(
    selection: Selection, index: ChunkIndex, planned: Counter[int] | None, wanted: Iterable[int]
) -> str | None:
    """Why a chunk holds no message the config selects, by its index alone, so it is not read.

    ``time`` when the index puts the chunk outside the log_time window, ``topics`` when it lists
    no selected channel in it, else ``None``. Only a chunk whose counts the index gives may be
    skipped: another one's counts are known only by reading it, and the chunks after it are
    numbered from them.
    """
    if not selection.active or planned is None:
        return None
    if not selection.overlaps(index.start_time, index.end_time):
        return "time"
    return None if any(planned[channel] for channel in wanted) else "topics"


def chunk_at(source: SourceReader, offset: int, length: int) -> TopRecord | None:
    """The Chunk record at ``offset`` if one of ``length`` bytes starts there."""
    if length < RECORD_HEADER or offset + length > source.size:
        return None
    opcode, declared = record_header(read_exact(source, offset, RECORD_HEADER))
    if opcode != Opcode.CHUNK or RECORD_HEADER + declared != length:
        return None
    return TopRecord(offset, opcode, declared, None)


def chunk_messages(source: SourceReader, index: ChunkIndex, limit: int) -> Counter[int] | None:
    """The messages per channel of the chunk an index entry names, or ``None``."""
    record = chunk_at(source, index.chunk_start, index.chunk_length)
    if record is None:
        return None
    try:
        return count_messages(open_chunk(source, record, limit))
    except ChunkProblem:
        return None


def _wanted(directory: Directory, selection: Selection) -> set[int]:
    return {c for c, (_, channel) in directory.channels.items() if selection.selects(channel.topic)}


def _indexed(
    source: SourceReader,
    reporter: Reporter,
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
    for place, index in (indexes[0], indexes[-1]):
        if chunk_at(source, index.chunk_start, index.chunk_length) is None:
            return "chunk_place", place

    counts: list[Counter[int]] = []
    exact: list[bool] = []
    for _, index in indexes:
        found = index_counts(index)
        exact.append(found is not None)
        if found is None:
            found = chunk_messages(source, index, limit) or Counter()
        counts.append(found)
    totals: Counter[int] = Counter()
    for found in counts:
        totals.update(found)
    if statistics is not None:
        place, stats = statistics
        if sum(totals.values()) != stats.message_count:
            return "message_count", place
        declared = {channel: count for channel, count, _ in stats.channel_message_counts if count}
        if declared and declared != {c: n for c, n in totals.items() if n}:
            return "channel_message_counts", place
    # A summary need not repeat the Schema and Channel records (the specification makes them
    # optional there); then only the data section declares them, and only a scan finds them.
    if any(n and c not in directory.channels for c, n in totals.items()):
        return None
    schemas = {channel.schema_id for _, channel in directory.channels.values()}
    if any(schema and schema not in directory.schemas for schema in schemas):
        return None

    wanted = _wanted(directory, selection)
    if indexes[0][1].chunk_start > DATA_START:
        lead = indexes[0][1].chunk_start - DATA_START
        grouper.add(Unit(DATA_START, lead, Counter(), Counter(), 0))
    following = [index.chunk_start for _, index in indexes[1:]] + [summary.start]
    skips: Counter[str] = Counter()
    first_skip: Place | None = None
    for (place, index), found, known, stop in zip(indexes, counts, exact, following, strict=True):
        skip = skipped(selection, index, found if known else None, wanted)
        if skip is not None:
            skips[skip] += 1
            first_skip = first_skip or Place(((index.chunk_start, index.chunk_length),))
        rows = 0 if skip else sum(found.values())
        unit = Unit(
            index.chunk_start, stop - index.chunk_start, Counter(), found, rows, True, place
        )
        grouper.add(unit)
    layout = Layout(True, True, grouper.finish(summary.start), data_end=True)
    if first_skip is not None:
        layout.findings.append(
            reporter.finding(
                "skipped_by_index",
                FindingCategory.SKIPPED,
                Severity.INFO,
                first_skip,
                f"{skips.total()} chunk(s) are not read: the summary's chunk index puts them"
                " outside the log_time window (time) or lists no selected channel in them"
                " (topics). The skip relies on the index alone, so messages a lying index hides"
                " there have no rows; the first is cited",
                {"chunks": skips.total(), "reasons": dict(sorted(skips.items()))},
            )
        )
    first: dict[int, Place] = {}
    for (_, index), found in zip(indexes, counts, strict=True):
        for channel, count in found.items():
            if count and channel not in directory.channels:
                first.setdefault(channel, Place(((index.chunk_start, index.chunk_length),)))
    layout.findings += _unknown_channels(reporter, first, totals)
    return layout


def _unknown_channels(
    reporter: Reporter, first: dict[int, Place], counts: Counter[int]
) -> list[IngestFinding]:
    return [
        reporter.finding(
            "unknown_channel",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            place,
            f"{counts[channel]} message(s) name channel {channel}, which no Channel record"
            " declares; they have no rows. The first holder is cited",
            {"id": channel, "messages": counts[channel]},
        )
        for channel, place in sorted(first.items())
    ]


# --- Scanned layout -----------------------------------------------------------------------------


@dataclass
class _Scan:
    """What a scan of the data section has found so far."""

    reporter: Reporter
    directory: Directory
    findings: list[IngestFinding] = field(default_factory=list)
    chunks: bool = False
    digests: dict[tuple[int, int], tuple[bytes, Place]] = field(default_factory=dict)
    # Top-level messages: per channel the count and first place; malformed and late times.
    top: Counter[int] = field(default_factory=Counter)
    top_first: dict[int, Place] = field(default_factory=dict)
    chunked: Counter[int] = field(default_factory=Counter)
    chunked_first: dict[int, Place] = field(default_factory=dict)
    malformed: int = 0
    malformed_first: Place | None = None
    out_of_range: Counter[int] = field(default_factory=Counter)
    out_of_range_first: list[Place] = field(default_factory=list)
    # Per (record kind, id): the first record unlike the first declaration, it, and how many.
    conflicts: dict[tuple[str, int], tuple[Place, Place, int]] = field(default_factory=dict)

    def chunk(self, source: SourceReader, record: TopRecord, limit: int) -> Counter[int]:
        """A chunk's declarations and its messages per channel; none if it is unreadable.

        An unreadable chunk counts no messages: ingest reports it, and its messages have no rows.
        One walk over the chunk's records, keeping nothing per record.
        """
        self.chunks = True
        try:
            chunk = open_chunk(source, record, limit)
        except ChunkProblem:
            return Counter()
        data, outer, read_channel = chunk.data, chunk.place.steps[0], CHANNEL_ID.unpack_from
        messages: Counter[int] = Counter()
        for offset, opcode, length in chunk.records():
            if opcode == Opcode.MESSAGE:
                if length >= 2:
                    messages[read_channel(data, offset + RECORD_HEADER)[0]] += 1
            elif opcode in (Opcode.SCHEMA, Opcode.CHANNEL):
                end = offset + RECORD_HEADER + length
                place = Place((outer, (offset, end - offset)))
                self.declare(opcode, data[offset + RECORD_HEADER : end], place)
        self.chunked.update(messages)
        for channel in messages:
            self.chunked_first.setdefault(channel, record.place)
        return messages

    def _malformed(self, place: Place) -> None:
        self.malformed += 1
        self.malformed_first = self.malformed_first or place

    def message(self, source: SourceReader, record: TopRecord) -> Counter[int]:
        """A top-level message: its channel, or a problem planning reports for unchunked files."""
        if record.length < 2:
            self._malformed(record.place)
            return Counter()
        head = content_of(source, record, 0, MESSAGE_FIELDS)
        channel = int.from_bytes(head[:2], "little")
        self.top[channel] += 1
        self.top_first.setdefault(channel, record.place)
        if record.length < MESSAGE_FIELDS:
            self._malformed(record.place)
        else:
            parsed = parse_message_head(head)
            if max(parsed.log_time, parsed.publish_time) > INT64_MAX:
                self.out_of_range[channel] += 1
                if not self.out_of_range_first:
                    self.out_of_range_first.append(record.place)
        return Counter({channel: 1})

    def declare(self, opcode: int, content: bytes, place: Place) -> None:
        """A Schema or Channel record: the first declaration of its id, or a repeat to compare."""
        try:
            parsed: Schema | Channel = (
                parse_schema(content) if opcode == Opcode.SCHEMA else parse_channel(content)
            )
        except FieldError:
            return  # ingest reports the malformed record where it finds it
        digest = hashlib.sha256(content).digest()
        if isinstance(parsed, Schema):
            if parsed.id == 0:
                return
            if parsed.id not in self.directory.schemas:
                self.directory.schemas[parsed.id] = Declared(place, digest)
            known = self.directory.schemas[parsed.id]
        else:
            if parsed.id not in self.directory.channels:
                self.directory.channels[parsed.id] = (Declared(place, digest), parsed)
            known = self.directory.channels[parsed.id][0]
        if known.digest != digest:
            key = ("schema" if isinstance(parsed, Schema) else "channel", parsed.id)
            first, _, count = self.conflicts.get(key, (place, known.place, 0))
            self.conflicts[key] = (first, known.place, count + 1)

    def report(self, chunked: bool) -> None:
        """What planning reports once for the whole file: conflicting declarations, undeclared
        channels, and in a file whose messages are not chunked the messages that cannot get rows
        or their times.
        """
        for (what, declared_id), (place, known, count) in self.conflicts.items():
            self.findings.append(
                self.reporter.finding(
                    "conflicting_declaration",
                    FindingCategory.INCONSISTENT,
                    Severity.WARNING,
                    place,
                    f"{count} {what} record(s) declare {what} id {declared_id} unlike its first"
                    " declaration, which is the one read; the first of them is cited",
                    {"id": declared_id, "record": what, "records": count},
                    related=(known,),
                )
            )
        counts, first = (
            (self.chunked, self.chunked_first) if chunked else (self.top, self.top_first)
        )
        undeclared = {c: p for c, p in first.items() if c not in self.directory.channels}
        self.findings += _unknown_channels(self.reporter, undeclared, counts)
        if chunked:
            return  # top-level messages and chunk contents are ingest's, record by record
        if self.malformed_first is not None:
            self.findings.append(
                self.reporter.finding(
                    "corrupt_record",
                    FindingCategory.CORRUPT,
                    Severity.ERROR,
                    self.malformed_first,
                    f"{self.malformed} Message record(s) are shorter than their fixed fields;"
                    " they have no rows. The first is cited",
                    {"count": self.malformed, "reason": "malformed"},
                )
            )
        if self.out_of_range:
            channels: dict[str, JsonValue] = {
                str(c): n for c, n in sorted(self.out_of_range.items())
            }
            self.findings.append(
                self.reporter.finding(
                    "time_out_of_range",
                    FindingCategory.UNREPRESENTABLE,
                    Severity.WARNING,
                    self.out_of_range_first[0],
                    "message times that do not fit a signed 64-bit tick count are unknown in"
                    " their rows; the first such message is cited",
                    {"channels": channels},
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
    pending: Unit | None = None  # a chunk, waiting for the Message Index records after it

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            grouper.add(pending)
            pending = None

    for record in scan(source, DATA_START, stop):
        opcode = record.opcode
        if record.cut:
            # A file cut inside a chunk: what its stored bytes decode to is read, as ingest does.
            flush()
            if opcode == Opcode.CHUNK and stop == source.size:
                messages = found.chunk(source, record, limit)
                size = stop - record.offset
                grouper.add(Unit(record.offset, size, Counter(), messages, messages.total(), True))
            break
        size = record.end - record.offset
        if opcode == Opcode.MESSAGE_INDEX and pending is not None:
            pending = Unit(
                pending.start,
                pending.size + size,
                pending.top,
                pending.chunked,
                pending.rows,
                True,
            )
            continue
        flush()
        if opcode == Opcode.CHUNK:
            messages = found.chunk(source, record, limit)
            pending = Unit(record.offset, size, Counter(), messages, messages.total(), True)
            continue
        top: Counter[int] = Counter()
        if opcode in (Opcode.SCHEMA, Opcode.CHANNEL) and record.length <= limit:
            found.declare(opcode, content_of(source, record), record.place)
        elif opcode == Opcode.MESSAGE:
            top = found.message(source, record)
        elif opcode == Opcode.DATA_END:
            end, data_end = record.end, True
            grouper.add(Unit(record.offset, size, top, Counter(), 0))
            break
        elif opcode == Opcode.FOOTER:
            end = record.offset
            break
        grouper.add(Unit(record.offset, size, top, Counter(), sum(top.values())))
    flush()
    found.report(found.chunks)
    return Layout(found.chunks, False, grouper.finish(end), found.findings, data_end)


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
        grouper = Grouper(chunk_bytes, max_rows)
        indexed = _indexed(source, reporter, tail.summary, directory, selection, limit, grouper)
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
    return layout


def seq_starts(ranges: Iterable[Range], chunked: bool) -> list[dict[int, int]]:
    """For each range, the ``seq`` each channel's first row in it gets: messages before it."""
    seen: Counter[int] = Counter()
    starts = []
    for current in ranges:
        counts = current.counts(chunked)
        starts.append({channel: seen[channel] for channel in counts})
        if current.last is None:  # every stretch of one chunk starts where the chunk does
            seen.update(counts)
    return starts

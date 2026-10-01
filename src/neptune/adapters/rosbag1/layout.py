"""The shape of a bag: its head, its connections, and the chunks (units) of its data section.

``plan_layout`` plans from the index when there is one that holds together: the Bag Header's
``index_pos`` points at the Connection records and the Chunk Info records, and neither the chunks
nor the messages are read to plan. An index that is missing (a bag that was never closed) or fails
a check is a finding, and the bag is planned by scanning it instead: every record of the data
section visited once and every chunk decompressed once, to find the connections and count the
messages. A unit is a chunk and what follows it up to the next chunk.
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.contract import SourceReader
from neptune.adapters.rosbag1.records import (
    BAG_HEADER_RECORD,
    MAGIC,
    MIN_MESSAGE_RECORD,
    BagHeader,
    ChunkInfo,
    Connection,
    FieldError,
    Op,
    parse_bag_header,
    parse_chunk_info,
    parse_connection,
)
from neptune.adapters.rosbag1.report import Limits, Reporter
from neptune.adapters.rosbag1.scan import (
    ChunkProblem,
    OpenedChunk,
    Place,
    TopRecord,
    open_chunk,
    read_exact,
    scan,
    whole,
)
from neptune.model.finding import FindingCategory, IngestFinding, Severity

MAX_CONNECTIONS: Final = 10_000  # streams a bag declares; the rest are findings
MAX_INDEX_BYTES: Final = 64 * 1024 * 1024  # the most of an index planning reads
# A chunk claiming more messages than max_rows is read by several planned chunks; an index that
# asks for more than this many extra ones is not believed.
MAX_EXTRA_STRETCHES: Final = 10_000


@dataclass(frozen=True)
class Head:
    """The magic and the Bag Header record. ``problem`` says how the head failed, if it did:
    ``magic``, ``missing``, ``cut``, ``malformed`` or ``not_header``."""

    place: Place  # the Bag Header record, or the magic when there is none
    header: BagHeader | None
    start: int  # where the records after the Bag Header start
    problem: str | None = None


def read_head(source: SourceReader, limits: Limits) -> Head:
    magic = Place(((0, min(len(MAGIC), source.size)),))
    if source.size < len(MAGIC) or read_exact(source, 0, len(MAGIC)) != MAGIC:
        return Head(magic, None, 0, "magic")
    # the Bag Header is padded to 4096 bytes: read no further ahead than that
    first = next(
        scan(source, len(MAGIC), source.size, limits.header_bytes, BAG_HEADER_RECORD), None
    )
    if first is None:
        return Head(magic, None, source.size, "missing")
    if first.cut:
        return Head(first.place, None, source.size, "cut")
    if first.op != Op.BAG_HEADER:
        return Head(magic, None, len(MAGIC), "not_header")
    try:
        assert first.fields is not None
        return Head(first.place, parse_bag_header(first.fields), first.end)
    except FieldError:
        return Head(first.place, None, first.end, "malformed")


@dataclass(frozen=True)
class Declared:
    """A connection and where its Connection record is."""

    place: Place
    connection: Connection


@dataclass(frozen=True)
class Unit:
    """A chunk and what follows it up to the next chunk, with the messages planned for it.

    ``counts`` is each connection's message count as the plan believes it: a Chunk Info's, or what
    scanning found. ``info`` is the Chunk Info record (indexed layout).
    """

    pos: int
    end: int
    counts: tuple[tuple[int, int], ...]
    info: Place | None = None

    @property
    def total(self) -> int:
        return sum(count for _, count in self.counts)


@dataclass(frozen=True)
class Stated:
    """What an index states about the whole bag, with where it states it."""

    first: Place | None  # the least start_time of the chunks holding messages
    last: Place | None  # the greatest end_time
    counts: dict[int, tuple[int, Place]] = field(default_factory=dict)  # per connection


@dataclass
class Layout:
    indexed: bool
    start: int  # where the data section's records start
    end: int  # where they end: the index, or the end of the file
    units: list[Unit] = field(default_factory=list)
    declared: dict[int, Declared] = field(default_factory=dict)
    findings: list[IngestFinding] = field(default_factory=list)
    stated: Stated | None = None


@dataclass(frozen=True)
class IndexProblem:
    reason: str
    place: Place


INDEX_PROBLEMS: Final = {
    "unclosed": "has index_pos 0: the bag was never closed",
    "index_pos": "points outside the file",
    "too_large": "is larger than planning reads",
    "cut": "ends inside a record",
    "record": "holds a record that is not a Connection or a Chunk Info",
    "chunk_info": "has a Chunk Info that does not parse",
    "record_size": "has a record too large to be a Chunk Info",
    "counts": "does not hold the number of connections and chunks the Bag Header counts",
    "order": "has Chunk Infos out of order or outside the data section",
    "implausible": "claims more messages than its chunks can hold",
    "unknown_connection": "lists messages of a connection no Connection record declares",
}


class Declarations:
    """The connections found, first declaration wins; repeats that differ are findings."""

    def __init__(self, reporter: Reporter) -> None:
        self.reporter = reporter
        self.found: dict[int, Declared] = {}
        self.findings: list[IngestFinding] = []
        self._conflicts: dict[int, int] = {}
        self._too_many = 0

    def add(self, place: Place, connection: Connection) -> None:
        existing = self.found.get(connection.id)
        if existing is None:
            if len(self.found) >= MAX_CONNECTIONS:
                if not self._too_many:
                    self.findings.append(
                        self.reporter.finding(
                            "too_many_connections",
                            FindingCategory.LIMIT,
                            Severity.ERROR,
                            place,
                            f"the bag declares more than {MAX_CONNECTIONS} connections; the rest"
                            " are not declared and their messages have no rows",
                            {"limit": MAX_CONNECTIONS},
                        )
                    )
                self._too_many += 1
                return
            self.found[connection.id] = Declared(place, connection)
        elif not existing.connection.same_as(connection) and connection.id not in self._conflicts:
            self._conflicts[connection.id] = 1
            self.findings.append(
                self.reporter.finding(
                    "conflicting_declaration",
                    FindingCategory.INCONSISTENT,
                    Severity.WARNING,
                    place,
                    f"connection {connection.id} is declared again with other content; the"
                    " first declaration is read",
                    {"id": connection.id},
                    related=(existing.place,),
                )
            )


def _declare_top(
    source: SourceReader,
    reporter: Reporter,
    limits: Limits,
    record: TopRecord,
    declarations: Declarations,
) -> None:
    if record.data_length is not None and record.data_length > limits.header_bytes:
        declarations.findings.append(
            reporter.finding(
                "header_too_large",
                FindingCategory.LIMIT,
                Severity.ERROR,
                record.place,
                f"a Connection record declares {record.data_length} bytes of connection header,"
                f" more than max_header_bytes ({limits.header_bytes}); it is not read",
                {"declared": record.data_length, "limit": limits.header_bytes},
            )
        )
        return
    try:
        declarations.add(record.place, parse_connection(whole(source, record)))
    except FieldError:
        declarations.findings.append(
            reporter.finding(
                "corrupt_record",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                record.place,
                "a Connection record does not parse; it declares nothing",
                {"reason": "connection"},
            )
        )


def read_index(
    source: SourceReader,
    reporter: Reporter,
    head: Head,
    limits: Limits,
    max_rows: int,
) -> Layout | IndexProblem:
    """The index the Bag Header points at, if it holds together."""
    header = head.header
    assert header is not None
    pos, size = header.index_pos, source.size
    if pos == 0:
        return IndexProblem("unclosed", head.place)
    if not head.start <= pos <= size:
        return IndexProblem("index_pos", head.place)
    if size - pos > MAX_INDEX_BYTES:
        return IndexProblem("too_large", Place(((pos, MAX_INDEX_BYTES),)))
    declarations = Declarations(reporter)
    connection_records = 0
    parsed: list[tuple[Place, ChunkInfo]] = []
    for record in scan(source, pos, size, limits.header_bytes):
        if record.cut:
            return IndexProblem("cut", record.place)
        if record.problem is not None:
            return IndexProblem("record", record.place)
        if record.op == Op.CONNECTION:
            connection_records += 1
            _declare_top(source, reporter, limits, record, declarations)
        elif record.op == Op.CHUNK_INFO:
            if record.raw is None:
                return IndexProblem("record_size", record.place)
            try:
                parsed.append((record.place, parse_chunk_info(record.raw)))
            except FieldError:
                return IndexProblem("chunk_info", record.place)
        else:
            return IndexProblem("record", record.place)
    if connection_records != header.conn_count or len(parsed) != header.chunk_count:
        return IndexProblem("counts", head.place)
    units: list[Unit] = []
    extra = 0
    previous = head.start - 1
    for place, info in parsed:
        if not previous < info.chunk_pos < pos:
            return IndexProblem("order", place)
        previous = info.chunk_pos
        merged = Counter[int]()
        for conn, count in info.counts:
            merged[conn] += count
        total = sum(merged.values())
        if total > limits.chunk_bytes // MIN_MESSAGE_RECORD:
            return IndexProblem("implausible", place)
        extra += max(0, total - 1) // max_rows
        if any(conn not in declarations.found for conn in merged):
            return IndexProblem("unknown_connection", place)
        units.append(Unit(info.chunk_pos, 0, tuple(sorted(merged.items())), place))
    if extra > MAX_EXTRA_STRETCHES:
        return IndexProblem("implausible", parsed[0][0])
    ends = [unit.pos for unit in units[1:]] + [pos] if units else []
    units = [Unit(u.pos, end, u.counts, u.info) for u, end in zip(units, ends, strict=True)]
    layout = Layout(True, head.start, pos, units, declarations.found, declarations.findings)
    layout.stated = _stated(parsed)
    return layout


def _stated(parsed: list[tuple[Place, ChunkInfo]]) -> Stated:
    """The bag's extent and per-connection counts, as its Chunk Infos state them."""
    holding = [(place, info) for place, info in parsed if info.total]
    first = last = None
    if holding:
        place, info = min(holding, key=lambda item: (item[1].start_time, item[1].chunk_pos))
        first = place.within(info.start_at, 8)
        place, info = max(holding, key=lambda item: (item[1].end_time, -item[1].chunk_pos))
        last = place.within(info.end_at, 8)
    spans: dict[int, list[int]] = {}
    for place, info in parsed:
        offset, length = place.steps[0]
        for conn, count in info.counts:
            entry = spans.setdefault(conn, [0, offset, offset + length])
            entry[0] += count
            entry[1], entry[2] = min(entry[1], offset), max(entry[2], offset + length)
    counts = {
        conn: (total, Place(((low, high - low),))) for conn, (total, low, high) in spans.items()
    }
    return Stated(first, last, counts)


# --- Scanning -----------------------------------------------------------------------------------


def survey(chunk: OpenedChunk) -> tuple[Counter[int], list[tuple[int, int]]]:
    """A chunk's messages per connection, and the Connection records in it (offset, length).

    The rule that decides which records are messages is the one ``Data`` numbers rows by.
    """
    counts: Counter[int] = Counter()
    connections: list[tuple[int, int]] = []
    for inner in chunk.records():
        if inner.op == Op.MESSAGE and inner.fields is not None:
            conn = inner.fields.unsigned(b"conn", 4)
            if conn is not None:
                counts[conn] += 1
        elif inner.op == Op.CONNECTION:
            connections.append((inner.offset, inner.length))
    return counts, connections


_RECORD_SLACK: Final = 64 * 1024  # what a Connection record holds besides its connection header


def _unusable_findings(
    reporter: Reporter, limits: Limits, unusable: dict[str, tuple[int, Place]]
) -> list[IngestFinding]:
    found = []
    for reason, (count, first) in sorted(unusable.items()):
        if reason == "header_too_large":
            found.append(
                reporter.finding(
                    "header_too_large",
                    FindingCategory.LIMIT,
                    Severity.ERROR,
                    first,
                    f"{count} Connection record(s) inside chunks declare a connection header over"
                    f" max_header_bytes ({limits.header_bytes}); they declare nothing",
                    {"count": count, "limit": limits.header_bytes},
                )
            )
        else:
            found.append(
                reporter.finding(
                    "corrupt_record",
                    FindingCategory.CORRUPT,
                    Severity.ERROR,
                    first,
                    f"{count} Connection record(s) inside chunks do not parse; they declare"
                    " nothing",
                    {"count": count, "reason": "connection"},
                )
            )
    return found


def scan_layout(source: SourceReader, reporter: Reporter, head: Head, limits: Limits) -> Layout:
    """Every record of the data section visited once, every chunk decompressed once."""
    declarations = Declarations(reporter)
    layout = Layout(False, head.start, source.size)
    starts: list[tuple[int, Counter[int]]] = []
    # Connection records inside chunks that cannot be declared: how many, and the first.
    unusable: dict[str, tuple[int, Place]] = {}
    for record in scan(source, head.start, source.size, limits.header_bytes):
        if record.op == Op.CONNECTION and not record.cut:
            _declare_top(source, reporter, limits, record, declarations)
        elif record.op == Op.CHUNK and record.problem is None:
            counts: Counter[int] = Counter()
            try:
                opened = open_chunk(source, record, limits.chunk_bytes)
            except ChunkProblem:
                pass
            else:
                counts, inner = survey(opened)
                for offset, length in inner:
                    where = opened.place.inner(offset, length)
                    if length > limits.header_bytes + _RECORD_SLACK:
                        reason = "header_too_large"
                    else:
                        try:
                            found = parse_connection(opened.data[offset : offset + length])
                        except FieldError:
                            reason = "corrupt_record"
                        else:
                            declarations.add(where, found)
                            continue
                    count, first = unusable.get(reason, (0, where))
                    unusable[reason] = (count + 1, first)
                del opened
            starts.append((record.offset, counts))
        if record.cut:
            break
    declarations.findings += _unusable_findings(reporter, limits, unusable)
    ends = [pos for pos, _ in starts[1:]] + [source.size]
    layout.units = [
        Unit(pos, end, tuple(sorted(counts.items())))
        for (pos, counts), end in zip(starts, ends, strict=True)
    ]
    layout.declared = declarations.found
    layout.findings = declarations.findings
    return layout


def plan_layout(
    source: SourceReader, reporter: Reporter, head: Head, limits: Limits, max_rows: int
) -> Layout:
    """Indexed when the bag's index holds together, else scanned; a failed index is a finding."""
    if head.header is not None:
        found = read_index(source, reporter, head, limits, max_rows)
        if isinstance(found, Layout):
            return found
        severity = Severity.INFO if found.reason == "unclosed" else Severity.WARNING
        layout = scan_layout(source, reporter, head, limits)
        layout.findings.append(
            reporter.finding(
                "index_invalid",
                FindingCategory.INCONSISTENT,
                severity,
                found.place,
                f"the bag's index {INDEX_PROBLEMS[found.reason]}; the data section is planned"
                " by scanning it",
                {"reason": found.reason},
            )
        )
        return layout
    return scan_layout(source, reporter, head, limits)

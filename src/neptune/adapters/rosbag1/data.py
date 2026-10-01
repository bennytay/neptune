"""Ingesting one planned range of a bag's data section, or one stretch of a chunk's messages.

The range is walked unit by unit, a unit being a chunk and the records after it up to the next
chunk. A Message Data record becomes a series row citing its exact bytes (the Chunk record, then
the message inside its uncompressed data); Index Data records and the Chunk Info are checked
against the chunk; everything else is a finding. Every chunk is decompressed and checked on the
way.

Numbering never depends on where ranges fall: a chunk's messages of one connection are numbered
from the ``seq`` the plan gives the range plus the counts of the chunks before it in the range,
each taken from the chunk's Chunk Info (the same bytes the plan read) or, without one, from the
chunk itself. A message past its chunk's indexed count has no row. Every finding cites one record
or one chunk and is made by the one planned chunk that starts at it.
"""

import contextlib
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field

from neptune.adapters.contract import AdapterConfig, Chunk, ChunkOutput, SourceReader
from neptune.adapters.rosbag1.ingest import (
    TIME,
    Cite,
    as_int,
    as_list,
    as_place,
    columns,
)
from neptune.adapters.rosbag1.records import (
    Connection,
    FieldError,
    InnerRecords,
    Op,
    op_name,
    parse_chunk_info,
    parse_connection,
    parse_index_data,
    ticks,
)
from neptune.adapters.rosbag1.report import Reporter, limits
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
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.series import SEQ, SeriesBatch, SeriesColumn, locator_column

_CHUNK_PROBLEMS = {
    "too_large": ("record_too_large", FindingCategory.LIMIT),
    "unknown_compression": ("unknown_compression", FindingCategory.UNSUPPORTED),
    "decompression": ("decompression_failed", FindingCategory.CORRUPT),
    "size": ("decompression_failed", FindingCategory.CORRUPT),
    "malformed": ("corrupt_record", FindingCategory.CORRUPT),
}
_LISTED = 20  # entries a finding's details list before they are counted


@dataclass
class Slot:
    """A declared connection's rows here, and the ``seq`` the next chunk's messages start from."""

    stream: RecordId
    seq: int
    rows: dict[str, list[int]] = field(default_factory=dict)


@dataclass(frozen=True)
class Planned:
    """What a Chunk Info states about one chunk."""

    place: Place
    counts: dict[int, int]
    start_time: int
    end_time: int


@dataclass
class _Held:
    """What one chunk's messages hold."""

    found: Counter[int] = field(default_factory=Counter)
    over: Counter[int] = field(default_factory=Counter)  # past the indexed count: no row
    unknown: Counter[int] = field(default_factory=Counter)  # connections nothing declares
    malformed: int = 0
    other: Counter[str] = field(default_factory=Counter)  # ops that do not belong in a chunk
    first_other: Place | None = None
    conflicts: Counter[int] = field(default_factory=Counter)
    first_conflict: tuple[Place, Place] | None = None
    unparsed_connections: int = 0
    times: tuple[int, int] | None = None  # the least and greatest message time
    walk: InnerRecords | None = None  # the walk, for how it ended


@dataclass
class _Unit:
    """What the records around a chunk hold, reported once per unit."""

    other: Counter[str] = field(default_factory=Counter)
    first_other: Place | None = None
    headers: Counter[str] = field(default_factory=Counter)  # unreadable headers, by problem
    first_header: dict[str, Place] = field(default_factory=dict)
    outside: int = 0
    first_outside: Place | None = None
    index_mismatch: list[list[JsonValue]] = field(default_factory=list)
    index_mismatches: int = 0
    first_index: Place | None = None


class Data:
    """One planned range of the data section, or one stretch of a chunk's messages."""

    def __init__(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> None:
        context = chunk.context
        self.source = source
        self.cite = Cite(source, config)
        self.reporter = Reporter(source, config)
        self.limits = limits(config)
        self.start, self.end = as_int(context["start"]), as_int(context["end"])
        self.indexed = context["layout"] == "indexed"
        self.first = as_int(context["first"]) if "first" in context else 0
        self.last = as_int(context["last"]) if "last" in context else None
        self.lead = self.first == 0  # of the planned chunks reading one chunk, the one reporting
        self.columns = columns()
        self.slots: dict[int, Slot] = {}
        self.declared: dict[int, tuple[Place, Connection]] = {}
        for item in as_list(context["channels"]):
            conn, where, seq = as_list(item)
            place = as_place(where)
            self.slots[as_int(conn)] = Slot(
                self.cite.stream(place), as_int(seq), {name: [] for name, _ in self.columns}
            )
            if self.indexed and self.lead:
                self._declare(as_int(conn), place)
        self.units: list[tuple[int, Place | None]] = []
        for item in as_list(context["units"]):
            pos, *info = as_list(item)
            self.units.append((as_int(pos), as_place(info[0]) if info else None))
        self.findings: list[IngestFinding] = []
        self.read: dict[int, int] | None = None  # the last chunk's messages, for its Index Data

    def _declare(self, conn: int, place: Place) -> None:
        (offset, length) = place.steps[0]
        # The plan declared it from these bytes; only a source that changed fails to parse.
        with contextlib.suppress(FieldError):
            self.declared[conn] = (place, parse_connection(read_exact(self.source, offset, length)))

    def report(
        self,
        code: str,
        category: FindingCategory,
        severity: Severity,
        subject: Place,
        message: str,
        details: Mapping[str, JsonValue],
        *,
        related: Iterable[Place] = (),
        records: Iterable[RecordId] = (),
    ) -> None:
        if self.lead:
            self.findings.append(
                self.reporter.finding(
                    code,
                    category,
                    severity,
                    subject,
                    message,
                    details,
                    related=related,
                    records=records,
                )
            )

    # -- the walk --

    def run(self) -> ChunkOutput:
        positions = [pos for pos, _ in self.units]
        if not positions:
            self._walk(self.start, self.end, None)
        else:
            if self.start < positions[0]:
                self._walk(self.start, positions[0], None)
            for k, unit in enumerate(self.units):
                self._walk(unit[0], ([*positions[k + 1 :], self.end])[0], unit)
        return ChunkOutput(series=tuple(self._batches()), findings=tuple(self.findings))

    def _planned(self, info: Place | None) -> Planned | None:
        if info is None:
            return None
        offset, length = info.steps[0]
        parsed = parse_chunk_info(read_exact(self.source, offset, length))
        counts: Counter[int] = Counter()
        for conn, count in parsed.counts:
            counts[conn] += count
        return Planned(info, dict(counts), parsed.start_time, parsed.end_time)

    def _walk(self, start: int, end: int, unit: tuple[int, Place | None] | None) -> None:
        """The records of ``[start, end)``; with ``unit``, one starting at that chunk."""
        self.read = None
        around = _Unit()
        records = scan(self.source, start, end, self.limits.header_bytes)
        planned = self._planned(unit[1]) if unit is not None else None
        if unit is not None:
            first = next(records, None)
            if first is not None and first.cut and first.op != Op.CHUNK:
                self._advance(planned.counts if planned else {})
                self._cut(first, end)
                return
            if first is None or first.op != Op.CHUNK or first.problem is not None:
                self._advance(planned.counts if planned else {})
                self._not_a_chunk(unit[0], planned)
                return
            self._chunk(first, planned, end)
        for record in records:
            if record.cut:
                self._cut(record, end)
                break
            self._record(record, around, end)
        self._unit_findings(around)

    def _record(self, record: TopRecord, around: _Unit, end: int) -> None:
        op = record.op
        if record.problem is not None:
            around.headers[record.problem] += 1
            around.first_header.setdefault(record.problem, record.place)
            self.read = None
        elif op == Op.INDEX_DATA:
            self._index_data(record, around)
        elif op == Op.CHUNK:
            self.read = None
            if self.indexed:
                self.report(
                    "index_mismatch",
                    FindingCategory.INCONSISTENT,
                    Severity.ERROR,
                    record.place,
                    "a chunk the index does not list; its messages have no rows",
                    {"reason": "unindexed_chunk"},
                )
            else:
                self._chunk(record, None, end)
        elif op == Op.MESSAGE:
            self.read = None
            around.outside += 1
            if around.first_outside is None:
                around.first_outside = record.place
        elif op in (Op.CONNECTION, Op.CHUNK_INFO):
            self.read = None
        else:
            self.read = None
            around.other[op_name(op)] += 1
            if around.first_other is None:
                around.first_other = record.place

    def _not_a_chunk(self, pos: int, planned: Planned | None) -> None:
        length = max(1, min(8, self.source.size - pos))
        self.report(
            "index_mismatch",
            FindingCategory.INCONSISTENT,
            Severity.ERROR,
            Place(((pos, length),)),
            "the index lists a chunk that is not at its place; up to the next listed chunk,"
            " nothing is read",
            {"reason": "chunk_place"},
            related=(planned.place,) if planned is not None else (),
        )

    def _unit_findings(self, around: _Unit) -> None:
        for problem, count in sorted(around.headers.items()):
            where = around.first_header[problem]
            if problem == "header_too_large":
                self.report(
                    "header_too_large",
                    FindingCategory.LIMIT,
                    Severity.ERROR,
                    where,
                    f"{count} record(s) declare a header over max_header_bytes"
                    f" ({self.limits.header_bytes}); they are not read",
                    {"count": count, "limit": self.limits.header_bytes},
                )
            else:
                self.report(
                    "corrupt_record",
                    FindingCategory.CORRUPT,
                    Severity.ERROR,
                    where,
                    f"{count} record(s) have a header that does not parse (or no one-byte op);"
                    " they are not read",
                    {"count": count, "reason": "header"},
                )
        if around.outside and around.first_outside is not None:
            self.report(
                "message_outside_layout",
                FindingCategory.UNSUPPORTED,
                Severity.ERROR,
                around.first_outside,
                f"{around.outside} message(s) outside any chunk; they have no rows",
                {"count": around.outside},
            )
        if around.other and around.first_other is not None:
            self.report(
                "unknown_record",
                FindingCategory.UNSUPPORTED,
                Severity.INFO,
                around.first_other,
                "records of an op the place does not hold or the specification does not define;"
                " skipped",
                {"ops": dict(sorted(around.other.items()))},
            )
        if around.index_mismatches and around.first_index is not None:
            self.report(
                "index_mismatch",
                FindingCategory.INCONSISTENT,
                Severity.WARNING,
                around.first_index,
                f"{around.index_mismatches} Index Data record(s) disagree with the chunk they"
                " follow",
                {
                    "connections": around.index_mismatch[:_LISTED],
                    "count": around.index_mismatches,
                    "reason": "index_data",
                },
            )

    def _cut(self, record: TopRecord, end: int) -> None:
        at_end = end == self.source.size
        present = record.end - record.offset
        if at_end:
            self.report(
                "truncated",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                record.place,
                f"the file ends inside a {op_name(record.op)} record: {present} of its bytes"
                " are there",
                {"op": op_name(record.op), "present": present},
            )
        else:
            self.report(
                "corrupt_record",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                record.place,
                f"a {op_name(record.op)} record runs past the unit it is in; it is not read",
                {"op": op_name(record.op), "reason": "overrun"},
            )

    # -- chunks --

    def _advance(self, counts: Mapping[int, int]) -> None:
        for conn, slot in self.slots.items():
            slot.seq += counts.get(conn, 0)

    def _chunk(self, record: TopRecord, planned: Planned | None, end: int) -> None:
        """One chunk: its messages numbered from the slots' ``seq``, then the slots advanced."""
        self.read = None
        counts = planned.counts if planned is not None else {}
        try:
            opened = open_chunk(self.source, record, self.limits.chunk_bytes)
        except ChunkProblem as problem:
            self._advance(counts)
            self._chunk_problem(record, problem)
            return
        held = self._messages(opened, planned)
        self._advance(counts if planned is not None else held.found)
        if self.lead:
            self._chunk_findings(opened, record, held, planned, end)
            self.read = dict(held.found) if not opened.partial else None

    def _chunk_problem(self, record: TopRecord, problem: ChunkProblem) -> None:
        code, category = _CHUNK_PROBLEMS[problem.reason]
        head = problem.head
        details: dict[str, JsonValue] = {"reason": problem.reason}
        if head is not None:
            details["compression"] = head.compression.shown
            details["declared_size"] = head.size
            details["stored"] = problem.stored
        if code == "record_too_large":
            message = (
                "a chunk declares more stored or uncompressed bytes than max_chunk_bytes"
                f" ({self.limits.chunk_bytes}); it is not read"
            )
        elif code == "unknown_compression":
            message = "a chunk's compression is not one the specification defines; no rows"
        elif code == "decompression_failed":
            message = (
                "a chunk's records do not decompress to its declared size; no rows"
                f" ({problem.reason})"
            )
        else:
            message = "a Chunk record lacks its compression or size field; it is not read"
            details["reason"] = "chunk_header"
        self.report(code, category, Severity.ERROR, record.place, message, details)

    def _messages(self, chunk: OpenedChunk, planned: Planned | None) -> _Held:
        """One walk over a chunk's records, holding nothing per record.

        The planned chunk reporting on the chunk walks all of it; a later stretch of its messages
        only counts its way to its first message and stops after its last.
        """
        outer, lead = chunk.place.steps[0], self.lead
        first, last, slots = self.first, self.last, self.slots
        held = _Held()
        found = held.found
        base = {conn: slot.seq for conn, slot in slots.items()}
        counts = planned.counts if planned is not None else None
        ordinal = 0
        least, greatest = 1 << 64, -1
        records = chunk.records()
        for inner in records:
            op = inner.op
            fields = inner.fields
            if op != Op.MESSAGE or fields is None:
                if not lead:
                    continue
                if op == Op.CONNECTION:
                    self._connection(chunk, inner.offset, inner.length, held)
                else:
                    held.other[op_name(op)] += 1
                    if held.first_other is None:
                        held.first_other = chunk.place.inner(inner.offset, inner.length)
                continue
            conn = fields.unsigned(b"conn", 4)
            if conn is None:
                held.malformed += 1
                continue
            j = found[conn]
            found[conn] = j + 1
            here, ordinal = ordinal, ordinal + 1
            if not lead and here < first:
                continue  # a later stretch counts its way to its first message
            if not lead and last is not None and here >= last:
                break  # and what follows its last message is not its own
            stamp = fields.time(b"time")
            if stamp is None:
                held.malformed += 1
                continue
            tick = ticks(stamp)
            if lead:
                least, greatest = min(least, tick), max(greatest, tick)
            if counts is not None and j >= counts.get(conn, 0):
                held.over[conn] += 1
                continue
            slot = slots.get(conn)
            if slot is None:
                held.unknown[conn] += 1
                continue
            if here < first or (last is not None and here >= last):
                continue
            rows = slot.rows
            rows[SEQ].append(base[conn] + j)
            rows[TIME].append(tick)
            for step, (offset, size) in enumerate((outer, (inner.offset, inner.length))):
                rows[locator_column(step, "length")].append(size)
                rows[locator_column(step, "offset")].append(offset)
        held.walk = records
        if greatest >= 0:
            held.times = (least, greatest)
        return held

    def _connection(self, chunk: OpenedChunk, offset: int, length: int, held: _Held) -> None:
        """A Connection record inside a chunk, against the declaration the index gave."""
        if not self.indexed:
            return
        try:
            found = parse_connection(chunk.data[offset : offset + length])
        except FieldError:
            held.unparsed_connections += 1
            return
        declared = self.declared.get(found.id)
        if declared is None or declared[1].same_as(found):
            return
        held.conflicts[found.id] += 1
        if held.first_conflict is None:
            held.first_conflict = (chunk.place.inner(offset, length), declared[0])

    def _chunk_findings(
        self,
        chunk: OpenedChunk,
        record: TopRecord,
        held: _Held,
        planned: Planned | None,
        end: int,
    ) -> None:
        place = chunk.place
        walk = held.walk
        assert walk is not None
        if chunk.partial:
            at_end = end == self.source.size
            present = record.end - record.data_offset
            self.report(
                "chunk_truncated",
                FindingCategory.CORRUPT,
                Severity.WARNING,
                place,
                (
                    "the file ends inside a chunk"
                    if at_end
                    else "a chunk's record runs past the unit it is in"
                )
                + f": the {len(chunk.data)} bytes its stored prefix decodes to are read",
                {
                    "decoded": len(chunk.data),
                    "declared_size": chunk.head.size,
                    "declared_stored": chunk.stored,
                    "present_stored": present,
                    "reason": "file_end" if at_end else "overrun",
                },
            )
        if walk.stop is not None:
            self.report(
                "too_many_records",
                FindingCategory.LIMIT,
                Severity.ERROR,
                place.inner(walk.stop, len(chunk.data) - walk.stop),
                f"a chunk holds more than {walk.most} records, more than a chunk within"
                " max_chunk_bytes can; the rest is not read",
                {"limit": walk.most},
            )
        if walk.cut is not None and not chunk.partial:
            self.report(
                "corrupt_record",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                place.inner(walk.cut, len(chunk.data) - walk.cut),
                "a chunk's records end inside a record; what follows its last whole record is"
                " not read",
                {"reason": "chunk_records"},
            )
        if walk.unparsed:
            self.report(
                "corrupt_record",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                place,
                f"{walk.unparsed} record(s) in a chunk have a header that does not parse; they"
                " are not read",
                {"count": walk.unparsed, "reason": "record_header"},
            )
        if held.malformed or held.unparsed_connections:
            self.report(
                "corrupt_record",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                place,
                f"{held.malformed} message(s) lack a readable conn or time field and"
                f" {held.unparsed_connections} Connection record(s) do not parse; they have no"
                " rows",
                {
                    "connections": held.unparsed_connections,
                    "messages": held.malformed,
                    "reason": "message",
                },
            )
        if held.other and held.first_other is not None:
            self.report(
                "unknown_record",
                FindingCategory.UNSUPPORTED,
                Severity.INFO,
                held.first_other,
                "records of an op a chunk does not hold or the specification does not define;"
                " skipped",
                {"ops": dict(sorted(held.other.items()))},
            )
        if held.unknown:
            self.report(
                "unknown_connection",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                place,
                f"messages of {len(held.unknown)} connection(s) that nothing declares; no rows",
                _counts("connections", held.unknown),
            )
        if held.conflicts and held.first_conflict is not None:
            conflicting, declared = held.first_conflict
            self.report(
                "conflicting_declaration",
                FindingCategory.INCONSISTENT,
                Severity.WARNING,
                conflicting,
                f"a chunk declares {len(held.conflicts)} connection(s) again with other content"
                " than the index; the index's declaration is read",
                {"ids": sorted(held.conflicts)[:_LISTED]},
                related=(declared,),
            )
        if planned is not None:
            self._indexed_findings(chunk, held, planned)

    def _indexed_findings(self, chunk: OpenedChunk, held: _Held, planned: Planned) -> None:
        lost = {c: n for c, n in held.over.items() if n}
        short = {
            conn: [count, held.found[conn]]
            for conn, count in sorted(planned.counts.items())
            if held.found[conn] < count
        }
        if lost or (short and not chunk.partial):
            differ: dict[int, list[int]] = {
                conn: [planned.counts.get(conn, 0), held.found[conn]]
                for conn in sorted(set(lost) | set(short))
            }
            self.report(
                "message_count_mismatch",
                FindingCategory.INCONSISTENT,
                Severity.ERROR if lost else Severity.WARNING,
                chunk.place,
                f"a chunk holds other message counts than its Chunk Info states for"
                f" {len(differ)} connection(s); messages past the count have no rows",
                {
                    "connections": [[c, *pair] for c, pair in list(differ.items())[:_LISTED]],
                    "count": len(differ),
                },
                related=(planned.place,),
            )
        if held.times is not None and not chunk.partial:
            least, greatest = held.times
            if (least, greatest) != (planned.start_time, planned.end_time):
                self.report(
                    "index_mismatch",
                    FindingCategory.INCONSISTENT,
                    Severity.WARNING,
                    chunk.place,
                    "a chunk's message times are not the span its Chunk Info states",
                    {
                        "found": [least, greatest],
                        "reason": "chunk_times",
                        "stated": [planned.start_time, planned.end_time],
                    },
                    related=(planned.place,),
                )

    def _index_data(self, record: TopRecord, around: _Unit) -> None:
        """An Index Data record against the chunk it follows: it counts the same messages."""
        if self.read is None or not self.lead:
            return
        if record.data_length is None or record.end - record.offset > self.limits.header_bytes:
            return
        try:
            index = parse_index_data(whole(self.source, record))
        except FieldError:
            index = None
        found = self.read.get(index.conn, 0) if index is not None else 0
        if index is not None and index.consistent and index.count == found:
            return
        around.index_mismatches += 1
        if around.first_index is None:
            around.first_index = record.place
        around.index_mismatch.append(
            [index.conn, index.count, found] if index is not None else [-1, -1, -1]
        )

    # -- output --

    def _batches(self) -> Iterator[SeriesBatch]:
        for slot in self.slots.values():
            if slot.rows[SEQ]:
                yield SeriesBatch(
                    slot.stream,
                    tuple(
                        SeriesColumn(name, kind, tuple(slot.rows[name]))
                        for name, kind in self.columns
                    ),
                )


def _counts(name: str, counts: Counter[int]) -> dict[str, JsonValue]:
    listed = sorted(counts.items())[:_LISTED]
    return {name: [[conn, count] for conn, count in listed], "count": len(counts)}

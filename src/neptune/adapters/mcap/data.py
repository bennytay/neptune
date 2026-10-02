"""Ingesting one planned range of an MCAP data section, or one stretch of a chunk's messages.

The range's records are walked in file order: in the indexed layout unit by unit, each unit
starting at the chunk its index names. A Message becomes a series row citing its exact bytes; a
Metadata record a table; an Attachment a finding citing it; every chunk is decompressed and
checked, and the Message Index records after it are checked against it.

Numbering never depends on where ranges fall: a chunk's messages of one channel are numbered from
the ``seq`` the plan gives the range plus the counts of the chunks before it in the range, each
taken from the chunk's index (the same bytes the plan read) or, without one, from the chunk
itself. A message past its chunk's indexed count has no row. Every finding cites one record or one
chunk and is made by the one planned chunk that starts at it.
"""

import struct
import zlib
from array import array
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from neptune.adapters.contract import AdapterConfig, Chunk, ChunkOutput, SourceReader, read_pieces
from neptune.adapters.mcap.ingest import (
    HEADER_TIME,
    KNOWN,
    LOG_TIME,
    PUBLISH_TIME,
    SEQUENCE,
    UNKNOWN,
    Cite,
    Records,
    as_int,
    as_list,
    as_place,
    columns,
    decoding_of,
    ticks,
)
from neptune.adapters.mcap.ranges import CHANNEL_ID, index_counts, skipped
from neptune.adapters.mcap.records import (
    MAGIC,
    MESSAGE_FIELDS,
    RECORD_HEADER,
    ChunkIndex,
    FieldError,
    InnerRecords,
    Opcode,
    opcode_name,
    parse_attachment_head,
    parse_chunk_index,
    parse_message_index,
    parse_metadata,
)
from neptune.adapters.mcap.report import Reporter, Selection, selection
from neptune.adapters.mcap.scan import (
    WHOLE_RECORD,
    ChunkProblem,
    OpenedChunk,
    Place,
    TopRecord,
    content_of,
    open_chunk,
    read_exact,
    scan,
)
from neptune.adapters.rosmsg.streams import LIMIT_REASONS, Decoding, Undecoded, decode_row
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import Knowledge, Known, NotApplicable, Unknown
from neptune.model.provenance import Row
from neptune.model.series import (
    SEQ,
    ColumnType,
    SeriesBatch,
    SeriesColumn,
    locator_column,
    state_column,
)
from neptune.model.time import INT64_MAX
from neptune.model.world import CellValue, StructuredRecord, StructuredTable

if TYPE_CHECKING:
    from neptune.identity.provenance import EvidenceRecord

_CHUNK_PROBLEMS: Final = {
    "too_large": ("record_too_large", FindingCategory.LIMIT),
    "unknown_compression": ("unknown_compression", FindingCategory.UNSUPPORTED),
    "crc": ("crc_mismatch", FindingCategory.CORRUPT),
    "decompression": ("decompression_failed", FindingCategory.CORRUPT),
    "size": ("decompression_failed", FindingCategory.CORRUPT),
}


@dataclass
class Slot:
    """A selected channel's rows here, and the ``seq`` the next chunk's messages start from;
    how its payloads decode, its columns, and the payloads this chunk could not decode."""

    stream: RecordId
    seq: int
    rows: dict[str, list[object]] = field(default_factory=dict)
    decoding: Decoding | None = None
    columns: tuple[tuple[str, ColumnType, bool], ...] = ()
    undecoded: Undecoded = field(default_factory=Undecoded)


# A channel's messages in one chunk, in record order: their offsets and log times, 16 bytes each.
Entries = tuple["array[int]", "array[int]"]
_NONE: Final[Entries] = (array("Q"), array("Q"))


@dataclass
class _Read:
    """A chunk just read: its messages per channel, for the Message Index records after it."""

    chunk: OpenedChunk
    entries: dict[int, Entries]


@dataclass
class _Held:
    """What one chunk's messages hold. ``found`` is counted by every planned chunk reading it;
    the rest only by the one that reports on it, over the whole chunk.
    """

    found: Counter[int] = field(default_factory=Counter)
    malformed: int = 0
    out_of_range: Counter[int] = field(default_factory=Counter)
    unknown: Counter[int] = field(default_factory=Counter)
    first_unknown: Place | None = None
    entries: dict[int, Entries] = field(default_factory=dict)
    times: tuple[int, int] | None = None  # the least and greatest log time of the messages


_MESSAGE_HEAD: Final = struct.Struct("<HIQQ")


def _content(source: SourceReader, place: Place) -> bytes:
    (offset, length), *_ = place.steps
    return read_exact(source, offset + RECORD_HEADER, length - RECORD_HEADER)


class Data:
    """One planned range of the data section, or one stretch of a chunk's messages."""

    def __init__(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> None:
        context = chunk.context
        self.source = source
        self.cite = Cite(source, config)
        self.reporter = Reporter(source, config)
        self.selection: Selection = selection(config)
        self.limit = config.integer("max_chunk_bytes")
        self.start, self.end = as_int(context["start"]), as_int(context["end"])
        self.chunked = context["layout"] == "chunked"
        self.first = as_int(context["first"]) if "first" in context else 0
        self.last = as_int(context["last"]) if "last" in context else None
        self.lead = self.first == 0  # of the planned chunks reading one chunk, the one reporting
        self.slots: dict[int, Slot] = {}
        entries = [as_list(item) for item in as_list(context["channels"])]
        declared = Records(source, self.limit)
        declared.load(
            (as_place(entry[1]) for entry in entries),
            (as_place(entry[3]) for entry in entries if len(entry) > 3),
        )
        for entry in entries:
            channel, where, seq = entry[:3]
            place = as_place(where)
            schema = as_place(entry[3]) if len(entry) > 3 else None
            decoding = decoding_of(declared, declared.channel(place), schema, config)
            usable = decoding if isinstance(decoding, Decoding) else None
            kinds = columns(self.chunked, decoding)
            self.slots[as_int(channel)] = Slot(
                self.cite.channel(place).stream,
                as_int(seq),
                {name: [] for name, _, _ in kinds},
                usable,
                kinds,
            )
        self.indexes: list[tuple[Place, ChunkIndex]] | None = None
        if "index" in context:
            places = [as_place(place) for place in as_list(context["index"])]
            self.indexes = [(place, parse_chunk_index(_content(source, place))) for place in places]
        self.ordinal = 0
        self.records: list[EvidenceRecord] = []
        self.findings: list[IngestFinding] = []
        self.read: _Read | None = None
        self.data_end = False
        self.truncated = False

    def report(
        self,
        code: str,
        category: FindingCategory,
        severity: Severity,
        subject: Place,
        message: str,
        details: dict[str, JsonValue],
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
        if self.indexes is None:
            self._walk(self.start, self.end, None)
        else:
            starts = [index.chunk_start for _, index in self.indexes]
            if not starts or self.start < starts[0]:
                self._walk(self.start, starts[0] if starts else self.end, None)
            for k, entry in enumerate(self.indexes):
                self._walk(entry[1].chunk_start, ([*starts[k + 1 :], self.end])[0], entry)
        at_end = self.end == self.source.size and self.indexes is None
        if at_end and not self.data_end and not self.truncated:
            self.report(
                "truncated",
                FindingCategory.CORRUPT,
                Severity.WARNING,
                Place(((len(MAGIC), self.source.size - len(MAGIC)),)),
                "the file ends after a whole record, without a Data End record, summary or footer",
                {"size": self.source.size},
            )
        self._payload_findings()
        return ChunkOutput(
            records=tuple(self.records),
            series=tuple(self._batches()),
            findings=tuple(self.findings),
        )

    def _walk(self, start: int, end: int, indexed: tuple[Place, ChunkIndex] | None) -> None:
        """The records of ``[start, end)``; with ``indexed``, a unit starting at that chunk."""
        self.read = None
        records = scan(self.source, start, end)
        if indexed is not None:
            index = indexed[1]
            first = next(records, None)
            if (
                first is None
                or first.cut
                or first.opcode != Opcode.CHUNK
                or RECORD_HEADER + first.length != index.chunk_length
            ):
                self._unindexed(index)
                return
            self._chunk(first, indexed)
        for record in records:
            if record.cut:
                self._cut(record, end)
                return
            read, self.read = self.read, None  # a Message Index is checked right after its chunk
            if record.opcode == Opcode.DATA_END:
                self.data_end = True
                if record.end < end:
                    self.report(
                        "corrupt_record",
                        FindingCategory.CORRUPT,
                        Severity.WARNING,
                        Place(((record.end, end - record.end),)),
                        f"{end - record.end} bytes follow the Data End record inside the data"
                        " section; they are not read",
                        {"reason": "after_data_end"},
                    )
                return
            self._record(record, read)

    def _unindexed(self, index: ChunkIndex) -> None:
        """The chunk index names no chunk of its length at its place: the unit is not read."""
        self._advance(index_counts(index) or Counter())
        length = min(RECORD_HEADER, self.source.size - index.chunk_start)
        self.report(
            "index_mismatch",
            FindingCategory.INCONSISTENT,
            Severity.ERROR,
            Place(((index.chunk_start, length),)),
            "the summary's chunk index names a chunk that is not at its place; up to the next"
            " indexed chunk, nothing is read",
            {"chunk_length": index.chunk_length, "reason": "chunk_place"},
        )

    def _record(self, record: TopRecord, read: _Read | None) -> None:
        opcode = record.opcode
        if opcode == Opcode.CHUNK:
            if self.indexes is not None:
                self._unlisted(record)
            elif self.chunked:
                self._chunk(record, None)
            else:
                self._outside(record)
        elif opcode == Opcode.MESSAGE:
            if self.chunked:
                self._outside(record)
            elif record.length >= 2:
                self._top_message(content_of(self.source, record, 0, MESSAGE_FIELDS), record)
        elif opcode in (Opcode.SCHEMA, Opcode.CHANNEL):
            return
        elif opcode == Opcode.MESSAGE_INDEX:
            if read is not None:
                self._message_index(record, read)
        elif opcode == Opcode.ATTACHMENT:
            self._attachment(record)
        elif opcode == Opcode.METADATA:
            self._metadata(record)
        elif not (opcode == Opcode.HEADER and record.offset == len(MAGIC)):
            self.report(
                "unknown_record",
                FindingCategory.UNSUPPORTED,
                Severity.INFO,
                record.place,
                f"a {opcode_name(opcode)} record does not belong in the data section; it is"
                " skipped",
                {"opcodes": {opcode_name(opcode): 1}},
            )

    def _unlisted(self, record: TopRecord) -> None:
        """A chunk the summary's index does not list: planning numbered no message of it."""
        self.report(
            "index_mismatch",
            FindingCategory.INCONSISTENT,
            Severity.ERROR,
            record.place,
            "a chunk the summary's chunk index does not list; its messages have no rows",
            {"reason": "unindexed_chunk"},
        )

    def _outside(self, record: TopRecord) -> None:
        what = "chunk" if record.opcode == Opcode.CHUNK else "top-level message"
        layout = "chunked" if self.chunked else "not chunked"
        self.report(
            "message_outside_layout",
            FindingCategory.UNSUPPORTED,
            Severity.ERROR,
            record.place,
            f"a {what} in a file whose messages are {layout}; it has no rows",
            {"record": opcode_name(record.opcode)},
        )

    # -- messages --

    def _row(
        self,
        slot: Slot,
        seq: int,
        head: tuple[int, int, int, int],
        steps: tuple[tuple[int, int], ...],
        payload: "Callable[[], bytes | memoryview] | None" = None,
    ) -> None:
        _, sequence, log_time, publish_time = head
        if not self.selection.admits(log_time):
            return
        rows = slot.rows
        for column, value in ((LOG_TIME, log_time), (PUBLISH_TIME, publish_time)):
            tick = ticks(value)
            rows[column].append(tick)
            rows[state_column(column)].append(KNOWN if tick is not None else UNKNOWN)
        rows[SEQ].append(seq)
        rows[SEQUENCE].append(sequence)
        for step, (offset, size) in enumerate(steps):
            rows[locator_column(step, "length")].append(size)
            rows[locator_column(step, "offset")].append(offset)
        if slot.decoding is not None and payload is not None:
            decoded = decode_row(slot.decoding, payload())
            for name, cell in decoded.cells.items():
                rows[name].append(cell)
            if slot.decoding.has_header:
                rows[HEADER_TIME].append(decoded.stamp)
                rows[state_column(HEADER_TIME)].append(
                    KNOWN if decoded.stamp is not None else UNKNOWN
                )
            if decoded.problem is not None:
                slot.undecoded.add(decoded.problem.reason, Place(steps))

    def _top_message(self, head: bytes, record: TopRecord) -> None:
        """A message outside chunks, in a file whose messages are not chunked.

        Planning counted it, and reported once for the file the messages too short for their
        fields, naming undeclared channels, or holding times past 2^63 - 1; here a message just
        gets its row, or none.
        """
        slot = self.slots.get(int.from_bytes(head[:2], "little"))
        if slot is None:
            return
        seq, slot.seq = slot.seq, slot.seq + 1
        if record.length >= MESSAGE_FIELDS:

            def payload() -> bytes:
                return content_of(self.source, record, MESSAGE_FIELDS)

            self._row(slot, seq, _MESSAGE_HEAD.unpack_from(head), record.place.steps, payload)

    def _advance(self, counts: Counter[int]) -> None:
        for channel, slot in self.slots.items():
            slot.seq += counts[channel]

    def _chunk(self, record: TopRecord, indexed: tuple[Place, ChunkIndex] | None) -> int:
        """One chunk: its messages numbered from the slots' ``seq``, then the slots advanced.

        Returns how many messages it holds that could be read.
        """
        self.read = None  # Message Index records after this chunk are checked against it only
        index = indexed[1] if indexed is not None else None
        planned = index_counts(index) if index is not None else None
        if index is not None and skipped(self.selection, index, planned, self.slots):
            self._advance(planned or Counter())
            return 0
        try:
            chunk = open_chunk(self.source, record, self.limit)
        except ChunkProblem as problem:
            self._advance(planned or Counter())
            self._chunk_problem(record, problem)
            return 0
        if indexed is not None:
            self._index_fields(chunk, *indexed)
        records = chunk.records()
        held = self._messages(chunk, records, planned)
        self._advance(planned if planned is not None else held.found)
        if self.lead:
            self._chunk_findings(chunk, records, held, planned)
        return held.found.total()

    def _messages(
        self, chunk: OpenedChunk, records: InnerRecords, planned: Counter[int] | None
    ) -> _Held:
        """One walk over a chunk's records, holding nothing per record but 16 bytes a message
        for the Message Index check.

        The planned chunk reporting on the chunk walks all of it; a later stretch of its
        messages only counts its way to its first message and stops after its last.
        """
        data, outer, lead = chunk.data, chunk.place.steps[0], self.lead
        view = memoryview(data)
        first, last, slots = self.first, self.last, self.slots
        read_head, read_channel = _MESSAGE_HEAD.unpack_from, CHANNEL_ID.unpack_from
        held = _Held()
        found = held.found
        base = {channel: slot.seq for channel, slot in slots.items()}
        least, greatest = 1 << 64, -1
        for offset, opcode, length in records:
            if opcode != Opcode.MESSAGE:
                if lead and opcode not in (Opcode.SCHEMA, Opcode.CHANNEL):
                    held.unknown[opcode] += 1
                    if held.first_unknown is None:
                        held.first_unknown = Place((outer, (offset, RECORD_HEADER + length)))
                continue
            if length < 2:
                held.malformed += 1
                continue
            content = offset + RECORD_HEADER
            channel = read_channel(data, content)[0]
            j = found[channel]
            found[channel] = j + 1
            ordinal = self.ordinal
            self.ordinal += 1
            if length < MESSAGE_FIELDS:
                held.malformed += 1
                continue
            if not lead and ordinal < first:
                continue  # a later stretch counts its way to its first message
            if not lead and last is not None and ordinal >= last:
                break  # and what follows its last message is not its own
            head = read_head(data, content)
            if lead:
                _, _, log_time, publish_time = head
                offsets, times = held.entries.setdefault(channel, (array("Q"), array("Q")))
                offsets.append(offset)
                times.append(log_time)
                least, greatest = min(least, log_time), max(greatest, log_time)
                if log_time > INT64_MAX or publish_time > INT64_MAX:
                    held.out_of_range[channel] += 1
            slot = slots.get(channel)
            if slot is None or (planned is not None and j >= planned[channel]):
                continue
            if ordinal < first or (last is not None and ordinal >= last):
                continue
            start, stop = content + MESSAGE_FIELDS, content + length

            def payload(start: int = start, stop: int = stop) -> memoryview:
                return view[start:stop]

            self._row(
                slot, base[channel] + j, head, (outer, (offset, RECORD_HEADER + length)), payload
            )
        if greatest >= 0:
            held.times = (least, greatest)
        return held

    def _index_fields(self, chunk: OpenedChunk, place: Place, index: ChunkIndex) -> None:
        """The chunk index entry against the Chunk record it names: their fields must agree."""
        head = chunk.head
        pairs: dict[str, tuple[JsonValue, JsonValue]] = {
            "compressed_size": (index.compressed_size, head.records[1]),
            "end_time": (index.end_time, head.end_time),
            "start_time": (index.start_time, head.start_time),
            "uncompressed_size": (index.uncompressed_size, head.uncompressed_size),
        }
        differ: dict[str, JsonValue] = {
            name: [indexed, found] for name, (indexed, found) in pairs.items() if indexed != found
        }
        if index.compression.raw != head.compression.raw:
            differ["compression"] = [index.compression.shown, head.compression.shown]
        if differ:
            self.report(
                "index_mismatch",
                FindingCategory.INCONSISTENT,
                Severity.WARNING,
                chunk.place,
                "the summary's chunk index entry ([indexed, chunk]) disagrees with the Chunk"
                " record it names; the chunk's own fields are read. Selection by the index may"
                " have skipped what it holds elsewhere",
                {"fields": differ, "reason": "chunk_fields"},
                related=(place,),
            )

    def _chunk_findings(
        self,
        chunk: OpenedChunk,
        records: InnerRecords,
        held: _Held,
        planned: Counter[int] | None,
    ) -> None:
        place, outer = chunk.place, chunk.place.steps[0]
        if planned is not None and +planned != +held.found:
            channels: dict[str, JsonValue] = {
                str(c): [planned[c], held.found[c]]
                for c in sorted(set(planned) | set(held.found))
                if planned[c] != held.found[c]
            }
            lost = any(held.found[c] > planned[c] for c in held.found)
            self.report(
                "message_count_mismatch",
                FindingCategory.INCONSISTENT,
                Severity.ERROR if lost else Severity.WARNING,
                place,
                "the chunk holds other message counts ([indexed, found]) than its index states;"
                " messages past the indexed count have no rows",
                {"channels": channels},
                records=sorted(self.slots[int(c)].stream for c in channels if int(c) in self.slots),
            )
        if held.malformed:
            self.report(
                "corrupt_record",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                place,
                f"{held.malformed} Message record(s) in this chunk are shorter than their fixed"
                " fields; they have no rows",
                {"count": held.malformed, "reason": "malformed"},
            )
        if held.out_of_range:
            self.report(
                "time_out_of_range",
                FindingCategory.UNREPRESENTABLE,
                Severity.WARNING,
                place,
                "message times in this chunk that do not fit a signed 64-bit tick count are"
                " unknown in their rows",
                {"channels": {str(c): n for c, n in sorted(held.out_of_range.items())}},
            )
        if held.first_unknown is not None:
            unknown = {opcode_name(opcode): n for opcode, n in held.unknown.items()}
            self.report(
                "unknown_record",
                FindingCategory.UNSUPPORTED,
                Severity.INFO,
                held.first_unknown,
                f"{held.unknown.total()} record(s) a chunk does not hold are skipped; the"
                " first is cited",
                {"opcodes": dict(sorted(unknown.items()))},
            )
        if records.stop is not None:
            self.report(
                "too_many_records",
                FindingCategory.LIMIT,
                Severity.ERROR,
                Place((outer, (records.stop, len(chunk.data) - records.stop))),
                f"the chunk holds more than {records.most} records, more than a chunk of"
                " messages within max_chunk_bytes can; the records after them are not read",
                {"max_chunk_bytes": self.limit, "max_records": records.most},
            )
        if chunk.partial:
            self.report(
                "chunk_truncated",
                FindingCategory.CORRUPT,
                Severity.WARNING,
                place,
                f"the chunk is cut short: its stored bytes decode to {len(chunk.data)} of its"
                f" {chunk.head.uncompressed_size} bytes, whose whole records end at"
                f" {records.framed}; only those records are read, unchecked by its CRC",
                {
                    "decoded_bytes": len(chunk.data),
                    "framed_bytes": records.framed,
                    "uncompressed_bytes": chunk.head.uncompressed_size,
                },
            )
            return
        if records.cut is not None:
            self.report(
                "corrupt_record",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                Place((outer, (records.cut, len(chunk.data) - records.cut))),
                "the chunk's records end inside a record; the rest of the chunk is not read",
                {"reason": "records_overrun"},
            )
        self.read = _Read(chunk, held.entries)
        declared: list[JsonValue] = [chunk.head.start_time, chunk.head.end_time]
        if held.times is not None and list(held.times) != declared:
            self.report(
                "index_mismatch",
                FindingCategory.INCONSISTENT,
                Severity.WARNING,
                place,
                "the chunk's declared message times differ from the log times of the messages"
                " it holds",
                {"declared": declared, "found": list(held.times), "reason": "times"},
            )

    def _chunk_problem(self, record: TopRecord, problem: ChunkProblem) -> None:
        head = problem.head
        details: dict[str, JsonValue] = {"reason": problem.reason}
        if head is not None:
            details["compression"] = head.compression.shown
            details["stored_bytes"] = head.records[1]
            details["uncompressed_bytes"] = head.uncompressed_size
        code, category = _CHUNK_PROBLEMS.get(
            problem.reason, ("corrupt_record", FindingCategory.CORRUPT)
        )
        if code == "record_too_large":
            details["max_chunk_bytes"] = self.limit
        self.report(
            code,
            category,
            Severity.ERROR,
            record.place,
            f"a chunk is not read ({problem.reason}); its messages have no rows",
            details,
        )

    # -- other records --

    def _message_index(self, record: TopRecord, read: _Read) -> None:
        """A Message Index record after the chunk just read: the same messages, in any order.

        Its entries are read lazily and matched against the chunk's, which are in offset order:
        each must name a message of the chunk, with its log time, and none twice.
        """
        self.read = read  # the next Message Index record follows the same chunk
        if record.length > self.limit:
            self._too_large(record)
            return
        content = content_of(self.source, record)
        try:
            index = parse_message_index(content)
        except FieldError:
            self._malformed(record)
            return
        offsets, times = read.entries.get(index.channel_id, _NONE)
        seen = bytearray(len(offsets))
        declared = matched = 0
        for log_time, offset in index.read(content):
            declared += 1
            k = bisect_left(offsets, offset)
            if k < len(offsets) and offsets[k] == offset and times[k] == log_time and not seen[k]:
                seen[k] = 1
                matched += 1
        if declared != len(offsets) or matched != declared:
            self.report(
                "index_mismatch",
                FindingCategory.INCONSISTENT,
                Severity.WARNING,
                record.place,
                f"the Message Index for channel {index.channel_id} lists {declared}"
                f" message(s) unlike the {len(offsets)} the chunk before it holds",
                {
                    "declared": declared,
                    "found": len(offsets),
                    "id": index.channel_id,
                    "reason": "message_index",
                },
                related=(read.chunk.place,),
            )

    def _too_large(self, record: TopRecord) -> None:
        what = opcode_name(record.opcode)
        self.report(
            "record_too_large",
            FindingCategory.LIMIT,
            Severity.ERROR,
            record.place,
            f"a {what} record of {record.length} bytes is over max_chunk_bytes; it is not read",
            {"bytes": record.length, "max_chunk_bytes": self.limit},
        )

    def _malformed(self, record: TopRecord) -> None:
        self.report(
            "corrupt_record",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            record.place,
            f"a {opcode_name(record.opcode)} record's fields do not parse; it is not read",
            {"reason": "malformed"},
        )

    def _attachment(self, record: TopRecord) -> None:
        if not self.lead:
            return
        head_bytes = content_of(self.source, record, 0, WHOLE_RECORD)
        try:
            head = parse_attachment_head(head_bytes, record.length)
        except FieldError:
            self._malformed(record)
            return
        start, size = head.data
        base = record.offset + RECORD_HEADER
        crc = head.crc
        if head.crc_at + 4 > len(head_bytes):
            crc = int.from_bytes(content_of(self.source, record, head.crc_at, 4), "little")
        checked = bool(crc) and head.crc_at <= self.limit
        if checked:
            running = 0
            for piece in read_pieces(self.source, base, base + head.crc_at):
                running = zlib.crc32(piece, running)
            if running != crc:
                self.report(
                    "crc_mismatch",
                    FindingCategory.CORRUPT,
                    Severity.ERROR,
                    record.place,
                    "the attachment's CRC does not match its bytes",
                    {"reason": "crc", "record": "attachment"},
                )
        self.report(
            "attachment_not_extracted",
            FindingCategory.UNSUPPORTED,
            Severity.INFO,
            record.place,
            f"an attachment of {size} bytes is an embedded file no record kind holds yet; it is"
            " cited here, its name and media type in the details",
            {
                "create_time": head.create_time,
                "crc_checked": checked,
                "data": [base + start, size],
                "log_time": head.log_time,
                "media_type": head.media_type.shown,
                "name": head.name.shown,
            },
        )

    def _metadata(self, record: TopRecord) -> None:
        if not self.lead:
            return
        if record.length > self.limit:
            self._too_large(record)
            return
        try:
            metadata = parse_metadata(content_of(self.source, record))
        except FieldError:
            self._malformed(record)
            return
        cite, place = self.cite, record.place
        name = metadata.name.value
        table = StructuredTable(
            id=cite.record_id(StructuredTable.kind, place),
            provenance=cite.provenance(place),
            name=Known(name) if name else Unknown(),
            header=NotApplicable(),
        )
        self.records.append(table)
        bad = int(name is None)
        for row, pair in enumerate(metadata.entries):
            cells: list[Knowledge[CellValue]] = []
            for text in pair:
                value = text.value
                cells.append(Known(value) if value else Unknown())
                bad += value is None
            self.records.append(
                StructuredRecord(
                    id=cite.record_id(StructuredRecord.kind, place, Row(row)),
                    provenance=cite.provenance(place, Row(row)),
                    table=table.id,
                    row=row,
                    cells=tuple(cells),
                )
            )
        if bad:
            self.report(
                "invalid_utf8",
                FindingCategory.UNREPRESENTABLE,
                Severity.WARNING,
                place,
                f"the Metadata record has {bad} string(s) that are not UTF-8; they are unknown",
                {"strings": bad},
                records=(table.id,),
            )

    def _cut(self, record: TopRecord, end: int) -> None:
        """The record ``[.., end)`` ends inside: the file's end (truncation) or the next unit."""
        what = opcode_name(record.opcode) if record.opcode >= 0 else "record header"
        declared = RECORD_HEADER + record.length if record.opcode >= 0 else -1
        present = record.place.steps[0][1]
        if end != self.source.size:
            self.report(
                "corrupt_record",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                record.place,
                f"a {what} runs past where the next indexed chunk starts; it is not read",
                {"declared": declared, "reason": "overrun"},
            )
            return
        self.truncated = True
        recovered = 0
        if record.opcode == Opcode.CHUNK and self.chunked:
            recovered = self._chunk(record, None)
        self.report(
            "truncated",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            record.place,
            f"the file ends inside a {what}: {present} of its bytes are there, and"
            f" {recovered} message(s) are read from them",
            {"declared": declared, "present": present, "recovered_messages": recovered},
        )

    def _batches(self) -> Iterator[SeriesBatch]:
        for slot in self.slots.values():
            if slot.rows[SEQ]:
                yield SeriesBatch(
                    slot.stream,
                    tuple(
                        SeriesColumn(name, kind, tuple(slot.rows[name]), repeated)  # type: ignore[arg-type]
                        for name, kind, repeated in slot.columns
                    ),
                )

    def _payload_findings(self) -> None:
        """One finding per stream whose payloads this chunk could not all decode."""
        for channel, slot in sorted(self.slots.items()):
            undecoded = slot.undecoded
            if not undecoded.total or not isinstance(undecoded.first, Place):
                continue
            limit = set(undecoded.counts) <= LIMIT_REASONS
            self.findings.append(
                self.reporter.finding(
                    "payload_undecodable",
                    FindingCategory.LIMIT if limit else FindingCategory.CORRUPT,
                    Severity.WARNING,
                    undecoded.first,
                    f"{undecoded.total} payload(s) of channel {channel} here do not decode by its"
                    " definition; their values are unknown (not covered past a limit), each row"
                    " still cites its message",
                    {"counts": dict(sorted(undecoded.counts.items())), "id": channel},
                    records=(slot.stream,),
                )
            )

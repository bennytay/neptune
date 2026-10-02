"""Ingesting the declarations chunk of a bag: the clock, the run, and a stream per connection.

Each stream comes with the empty series batch that types its columns. What a connection header
states (topic, type, md5sum, message definition, callerid, latching) is ``stated``: the publisher's
claim, recorded in the bag. The format itself defines the message and schema encodings, so those
cite the magic.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import AdapterConfig, Chunk, ChunkOutput, SourceReader
from neptune.adapters.rosbag1.records import MAGIC, Connection, Text, parse_connection, time_at
from neptune.adapters.rosbag1.report import Limits, Reporter, limits
from neptune.adapters.rosbag1.scan import (
    ChunkProblem,
    Place,
    open_chunk,
    place_from_json,
    read_exact,
    scan,
)
from neptune.adapters.rosmsg.streams import HEADER_STAMP, Decoding, NotDecoded, plan_stream
from neptune.identity.provenance import EvidenceRecord, evidence_record_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import AssertionKind, Knowledge, Known, Unknown
from neptune.model.provenance import EvidenceRef, Locator, Provenance, adapter_locator
from neptune.model.reference import TimestampDomain
from neptune.model.run import Run, Stream
from neptune.model.series import (
    SEQ,
    ColumnType,
    SeriesBatch,
    SeriesColumn,
    SeriesProvenance,
    locator_column,
    state_column,
    step_template,
    time_column,
)
from neptune.model.time import NANOSECOND, ClockRole, Timestamp

TIME_FIELD: Final = "rosbag1:time_field"
MAGIC_PLACE: Final = Place(((0, len(MAGIC)),))
TIME: Final = time_column(0)
HEADER_TIME: Final = time_column(1)
SCHEMA_ENCODING: Final = "ros1msg"  # the MCAP registry's names for the bag's own formats
MESSAGE_ENCODING: Final = "ros1"
# Connection header fields the stream holds elsewhere; every other field is its metadata.
_ELSEWHERE: Final = (b"message_definition", b"topic", b"type")


def columns(
    decoding: "Decoding | NotDecoded | None" = None,
) -> tuple[tuple[str, ColumnType, bool], ...]:
    """Every column of a bag stream's series, in name order, with its type and whether it is
    repeated: the message's own, then what its payload decodes to (ADR 0068 §1).

    A message lives in a chunk, so a row cites two byte ranges: the Chunk record in the file,
    then the message record in the chunk's uncompressed data.
    """
    found = [(SEQ, ColumnType.INT64, False), (TIME, ColumnType.INT64, False)]
    for step in (0, 1):
        found += [
            (locator_column(step, "length"), ColumnType.INT64, False),
            (locator_column(step, "offset"), ColumnType.INT64, False),
        ]
    if isinstance(decoding, Decoding):
        found += decoding.series_columns()
        if decoding.has_header:
            found += [
                (HEADER_TIME, ColumnType.INT64, False),
                (state_column(HEADER_TIME), ColumnType.STRING, False),
            ]
    return tuple(sorted(found))


def decoding_of(connection: Connection, config: AdapterConfig) -> Decoding | NotDecoded:
    """How a connection's payloads decode, from the type and definition its header states."""
    kind = connection.text(b"type")
    field = connection.header.find(b"message_definition")
    definition = None
    if field is not None and field.length:
        definition = connection.header.data[field.start : field.start + field.length]
    return plan_stream(
        config=config,
        message_encoding=MESSAGE_ENCODING,
        schema_encoding=SCHEMA_ENCODING,
        schema_name=kind.value if kind is not None else None,
        definition=definition,
    )


class ConnectionReader:
    """Reads Connection records by place; the chunk last opened is kept for the next place."""

    def __init__(self, source: SourceReader, limits: Limits) -> None:
        self.source = source
        self.limits = limits
        self._chunk_cache: tuple[tuple[int, int], bytes] | None = None

    def record(self, place: Place) -> bytes:
        (offset, length), *inner = place.steps
        if not inner:
            return read_exact(self.source, offset, length)
        if self._chunk_cache is None or self._chunk_cache[0] != (offset, length):
            record = next(scan(self.source, offset, offset + length, self.limits.header_bytes))
            self._chunk_cache = None  # one chunk held at a time
            try:
                opened = open_chunk(self.source, record, self.limits.chunk_bytes)
            except ChunkProblem as problem:
                raise ValueError(f"the plan's chunk no longer opens: {problem.reason}") from None
            self._chunk_cache = ((offset, length), opened.data)
        start, size = inner[0]
        return self._chunk_cache[1][start : start + size]


def series_template(source: SourceReader) -> SeriesProvenance:
    """A row cites its Message Data record inside its Chunk record's uncompressed data."""
    step = step_template("byte_range", per_row=("length", "offset"))
    return SeriesProvenance(source.content_id, (step, step), AssertionKind.OBSERVED)


def time_field(name: str) -> Locator:
    return adapter_locator(TIME_FIELD, {"name": name})


@dataclass(frozen=True)
class Ids:
    """The id a connection's stream gets, from where it is declared."""

    stream: RecordId


class Cite:
    """Evidence, provenance and record ids for one source under one config."""

    def __init__(self, source: SourceReader, config: AdapterConfig) -> None:
        self.source = source
        self.config = config
        self.transform = config.transform

    def evidence(self, place: Place, *more: Locator) -> EvidenceRef:
        return EvidenceRef(self.source.content_id, (*place.locator(), *more))

    def provenance(
        self, place: Place, *more: Locator, kind: AssertionKind = AssertionKind.OBSERVED
    ) -> Provenance:
        return Provenance(self.evidence(place, *more), self.transform.id, kind)

    def record_id(self, kind: str, place: Place, *more: Locator) -> RecordId:
        return evidence_record_id(kind, self.evidence(place, *more), self.transform)

    @property
    def clock(self) -> RecordId:
        return self.record_id(TimestampDomain.kind, MAGIC_PLACE, time_field("time"))

    def stream(self, place: Place) -> RecordId:
        return self.record_id(Stream.kind, place)


def as_place(data: JsonValue) -> Place:
    return place_from_json(data)


def as_int(data: JsonValue) -> int:
    if isinstance(data, bool) or not isinstance(data, int):
        raise ValueError(f"expected an integer, got {data!r}")
    return data


def as_list(data: JsonValue) -> list[JsonValue]:
    if not isinstance(data, list):
        raise ValueError(f"expected a list, got {data!r}")
    return data


class Declarations:
    """The chunk holding the clock, the run, and a stream per connection."""

    def __init__(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> None:
        self.cite = Cite(source, config)
        self.reporter = Reporter(source, config)
        self.limits = limits(config)
        self.source = source
        self.context = chunk.context
        self.records: list[EvidenceRecord] = []
        self.findings: list[IngestFinding] = []
        self.series: list[SeriesBatch] = []
        self.connections = ConnectionReader(source, self.limits)

    def finding(
        self,
        code: str,
        category: FindingCategory,
        severity: Severity,
        subject: Place,
        message: str,
        details: dict[str, JsonValue],
        records: Iterable[RecordId] = (),
    ) -> None:
        self.findings.append(
            self.reporter.finding(
                code, category, severity, subject, message, details, records=records
            )
        )

    def run(self) -> ChunkOutput:
        cite = self.cite
        clock = TimestampDomain(
            id=cite.clock,
            provenance=cite.provenance(MAGIC_PLACE, time_field("time")),
            field="time",
            scope=(),
            role=Known(ClockRole.RECEIVE, cite.provenance(MAGIC_PLACE)),
            resolution=Known(NANOSECOND, cite.provenance(MAGIC_PLACE)),
            epoch=Unknown(),
            timescale=Unknown(),
            declared_monotonic=Unknown(),
        )
        self.records.append(clock)
        header = self.context.get("header")
        run_place = as_place(header) if header is not None else MAGIC_PLACE
        first, last = self._extent()
        run = Run(
            id=cite.record_id(Run.kind, run_place),
            provenance=cite.provenance(run_place),
            logical_id=Unknown(),
            machine=Unknown(),
            first=first,
            last=last,
        )
        self.records.append(run)
        counts: dict[int, tuple[int, Place]] = {}
        for item in as_list(self.context.get("counts", [])):
            conn, total, where = as_list(item)
            counts[as_int(conn)] = (as_int(total), as_place(where))
        # In file order, so each chunk that holds declarations is decompressed once.
        channels = [as_list(item) for item in as_list(self.context["channels"])]
        for conn, where in sorted(channels, key=lambda item: as_place(item[1]).steps):
            self._stream(as_int(conn), as_place(where), run.id, counts)
        return ChunkOutput(
            records=tuple(self.records),
            series=tuple(self.series),
            findings=tuple(self.findings),
        )

    def _extent(self) -> tuple[Knowledge[Timestamp], Knowledge[Timestamp]]:
        """The run's first and last instants, as the Chunk Infos state them on the bag's clock."""
        found: list[Knowledge[Timestamp]] = []
        for name in ("first", "last"):
            where = self.context.get(name)
            if where is None:
                found.append(Unknown())
                continue
            place = as_place(where)
            ticks = time_at(read_exact(self.source, place.steps[0][0], 8))
            stated = self.cite.provenance(place, kind=AssertionKind.STATED)
            found.append(Known(Timestamp(ticks, self.cite.clock), stated))
        return found[0], found[1]

    def _text(self, record: Place, text: Text | None) -> tuple[Knowledge[str], str | None]:
        """A connection header string as ``Known`` (stated, citing its bytes), or ``Unknown``.

        The second value is why it is unknown: ``missing``, ``empty`` or ``utf8``.
        """
        if text is None:
            return Unknown(self.cite.provenance(record, kind=AssertionKind.STATED)), "missing"
        if not text.raw:
            return Unknown(self.cite.provenance(record, kind=AssertionKind.STATED)), "empty"
        place = record.within(text.at, len(text.raw))
        stated = self.cite.provenance(place, kind=AssertionKind.STATED)
        value = text.value
        if value is None:
            return Unknown(stated), "utf8"
        return Known(value, stated), None

    def _stream(
        self,
        conn: int,
        place: Place,
        run: RecordId,
        counts: dict[int, tuple[int, Place]],
    ) -> None:
        cite = self.cite
        content = self._record(place)
        connection = parse_connection(content)
        if connection.id != conn:
            raise ValueError(f"the plan's connection {conn} is declared as {connection.id}")
        stream_id = cite.stream(place)
        header_topic = connection.text(b"topic")
        record_topic = connection.topic
        # The topic the bag files the connection under is the record header's; a bag without one
        # falls back to the connection header's, which is the publisher's.
        chosen = record_topic if record_topic is not None else header_topic
        topic, topic_problem = self._text(place, chosen)
        name, name_problem = self._text(place, connection.text(b"type"))
        problems: dict[str, str] = {
            what: problem
            for what, problem in (("topic", topic_problem), ("type", name_problem))
            if problem is not None
        }
        definition: Knowledge[EvidenceRef]
        field = connection.header.find(b"message_definition")
        if field is None or field.length == 0:
            problems["message_definition"] = "missing" if field is None else "empty"
            definition = Unknown(cite.provenance(place, kind=AssertionKind.STATED))
        else:
            value_place = place.within(field.start, field.length)
            definition = Known(
                cite.evidence(value_place), cite.provenance(value_place, kind=AssertionKind.STATED)
            )
        if connection.header.find(b"md5sum") is None:
            problems["md5sum"] = "missing"
        metadata = self._metadata(connection, place, stream_id, record_topic, header_topic)
        invalid = sorted(what for what, problem in problems.items() if problem == "utf8")
        if invalid:
            self.finding(
                "invalid_utf8",
                FindingCategory.UNREPRESENTABLE,
                Severity.WARNING,
                place,
                f"connection {conn} has text that is not UTF-8 ({', '.join(invalid)}); it is"
                " unknown",
                {"fields": invalid, "id": conn},
                records=(stream_id,),
            )
        absent = {what: problem for what, problem in problems.items() if problem != "utf8"}
        if absent:
            self.finding(
                "missing_field",
                FindingCategory.MISSING,
                Severity.WARNING,
                place,
                f"connection {conn} lacks {', '.join(sorted(absent))} (absent or empty); each is"
                " unknown",
                {"fields": dict(sorted(absent.items())), "id": conn},
                records=(stream_id,),
            )
        spec = cite.provenance(MAGIC_PLACE)
        count: Knowledge[int] = Unknown()
        if conn in counts:
            total, span = counts[conn]
            count = Known(total, cite.provenance(span, kind=AssertionKind.STATED))
        decoding = decoding_of(connection, cite.config)
        clocks = [cite.clock]
        if isinstance(decoding, Decoding) and decoding.has_header:
            assert isinstance(definition, Known)
            header = self._header_clock(place, topic, conn, definition.value)
            clocks.append(header.id)
            self.records.append(header)
        stream = Stream(
            id=stream_id,
            provenance=cite.provenance(place, kind=AssertionKind.STATED),
            run=run,
            topic=topic,
            schema_name=name,
            schema_encoding=Known(SCHEMA_ENCODING, spec),
            schema_definition=definition,
            message_encoding=Known(MESSAGE_ENCODING, spec),
            metadata=metadata,
            clocks=tuple(clocks),
            message_count=count,
            first=Unknown(),
            last=Unknown(),
            series=series_template(cite.source),
        )
        self.records.append(stream)
        self.series.append(
            SeriesBatch(
                stream.id,
                tuple(
                    SeriesColumn(column, kind, (), repeated)
                    for column, kind, repeated in columns(decoding)
                ),
            )
        )
        self._decoding_findings(conn, place, stream_id, decoding)

    def _header_clock(
        self, place: Place, topic: Knowledge[str], conn: int, definition: EvidenceRef
    ) -> TimestampDomain:
        """The clock a leading ``Header``'s stamp reads (ADR 0068 §2): nanosecond ticks, as the
        definition's ``time`` declares them; role, epoch and timescale are the publisher's and
        unstated."""
        cite = self.cite
        where = (time_field(HEADER_STAMP),)
        scope = (topic.value,) if isinstance(topic, Known) else ("connection", str(conn))
        stated = Provenance(definition, cite.transform.id, AssertionKind.STATED)
        return TimestampDomain(
            id=cite.record_id(TimestampDomain.kind, place, *where),
            provenance=cite.provenance(place, *where),
            field=HEADER_STAMP,
            scope=scope,
            role=Unknown(),
            resolution=Known(NANOSECOND, stated),
            epoch=Unknown(),
            timescale=Unknown(),
            declared_monotonic=Unknown(),
        )

    def _decoding_findings(
        self, conn: int, place: Place, stream: RecordId, decoding: Decoding | NotDecoded
    ) -> None:
        if isinstance(decoding, NotDecoded):
            self.finding(
                "payload_not_decoded",
                FindingCategory.UNSUPPORTED,
                Severity.INFO,
                place,
                f"connection {conn}'s message payloads are not decoded ({decoding.detail}); each"
                " row cites its message's bytes",
                {"id": conn, "message_encoding": MESSAGE_ENCODING, "reason": decoding.reason},
                records=(stream,),
            )
        elif decoding.mode != "full":
            what = (
                f"only its header is decoded ({decoding.detail})"
                if decoding.mode == "header_only"
                else f"{len(decoding.left_out)} field path(s) are walked without a column"
            )
            self.finding(
                "payload_partly_decoded",
                FindingCategory.UNSUPPORTED,
                Severity.INFO,
                place,
                f"connection {conn}'s payloads are decoded, but {what}; each row still cites its"
                " message's bytes",
                {"id": conn, **decoding.details()},  # type: ignore[dict-item]
                records=(stream,),
            )

    def _metadata(
        self,
        connection: Connection,
        place: Place,
        stream: RecordId,
        record_topic: Text | None,
        header_topic: Text | None,
    ) -> tuple[tuple[str, str], ...]:
        """Every connection header field not held elsewhere, verbatim; none that repeats or is
        not UTF-8. The connection header's own ``topic`` is kept only if it differs."""
        data = connection.header.data
        pairs: dict[str, list[str]] = {}
        bad: set[str] = set()
        for field in connection.header.fields:
            if field.name in _ELSEWHERE:
                continue
            key = field.name.decode("utf-8", errors="backslashreplace")
            try:
                if field.name.decode("utf-8") != key:
                    raise UnicodeDecodeError("utf-8", b"", 0, 0, "")
                pairs.setdefault(key, []).append(
                    data[field.start : field.start + field.length].decode("utf-8")
                )
            except UnicodeDecodeError:
                bad.add(key)
        if (
            record_topic is not None
            and header_topic is not None
            and header_topic.raw != record_topic.raw
        ):
            shown = header_topic.value
            if shown is None:
                bad.add("topic")
            else:
                pairs["topic"] = [shown]
            self.finding(
                "topic_mismatch",
                FindingCategory.INCONSISTENT,
                Severity.INFO,
                place.within(header_topic.at, len(header_topic.raw)),
                f"connection {connection.id}'s header names another topic than the record"
                " it is filed under; the record's is the stream's topic",
                {"id": connection.id},
                records=(stream,),
            )
        if bad:
            self.finding(
                "invalid_utf8",
                FindingCategory.UNREPRESENTABLE,
                Severity.WARNING,
                place,
                f"connection {connection.id}'s header has fields that are not UTF-8"
                f" ({len(bad)}); they are left out of the metadata",
                {"fields": sorted(bad), "id": connection.id},
                records=(stream,),
            )
        repeated = sorted(key for key, values in pairs.items() if len(values) > 1)
        if repeated:
            self.finding(
                "duplicate_key",
                FindingCategory.AMBIGUOUS,
                Severity.WARNING,
                place,
                f"connection {connection.id}'s header repeats {len(repeated)} field(s); they are"
                " left out of the stream's metadata",
                {"id": connection.id, "keys": repeated},
                records=(stream,),
            )
        return tuple(sorted((key, values[0]) for key, values in pairs.items() if len(values) == 1))

    def _record(self, place: Place) -> bytes:
        """A Connection record's bytes; the chunk last opened is kept for the next place in it."""
        return self.connections.record(place)

"""Ingesting one planned chunk of an MCAP source: its declarations, or one range of its data.

The declarations chunk emits the clocks, the run and one stream per channel, each with the empty
series batch that types its columns. A data chunk walks the records of its byte range: messages
become series rows citing their exact bytes, metadata records become tables, attachments and
anything else are findings, and every chunk is decompressed and checked on the way.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import AdapterConfig, Chunk, ChunkOutput, SourceReader
from neptune.adapters.mcap.records import (
    CHANNEL_COUNT_ENTRY,
    MAGIC,
    MESSAGE_ENCODINGS,
    RECORD_HEADER,
    SCHEMA_ENCODINGS,
    STATISTICS_END_TIME,
    STATISTICS_START_TIME,
    Channel,
    Opcode,
    Schema,
    Statistics,
    Text,
    parse_channel,
    parse_schema,
    parse_statistics,
    record_header,
)
from neptune.adapters.mcap.report import Reporter, selection
from neptune.adapters.mcap.scan import (
    OpenedChunk,
    Place,
    TopRecord,
    open_chunk,
    place_from_json,
    read_exact,
)
from neptune.adapters.rosmsg.streams import (
    HEADER_STAMP,
    Decoding,
    NotDecoded,
    plan_stream,
)
from neptune.identity.provenance import EvidenceRecord, evidence_record_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import (
    AssertionKind,
    Knowledge,
    Known,
    KnownAbsent,
    Unknown,
)
from neptune.model.provenance import (
    EvidenceRef,
    Locator,
    Provenance,
    adapter_locator,
)
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
    value_column,
)
from neptune.model.time import INT64_MAX, NANOSECOND, ClockRole, Timestamp

TIME_FIELD: Final = "mcap:time_field"
MAGIC_PLACE: Final = Place(((0, len(MAGIC)),))
LOG_TIME, PUBLISH_TIME, HEADER_TIME = time_column(0), time_column(1), time_column(2)
SEQUENCE: Final = value_column("sequence")
KNOWN, UNKNOWN = "known", "unknown"


def columns(
    chunked: bool, decoding: "Decoding | NotDecoded | None" = None
) -> tuple[tuple[str, ColumnType, bool], ...]:
    """Every column of an MCAP stream's series, in name order, with its type and whether it is
    repeated: the message's own, then what its payload decodes to (ADR 0068 §1)."""
    steps = (0, 1) if chunked else (0,)
    found = [
        (SEQ, ColumnType.INT64, False),
        (LOG_TIME, ColumnType.INT64, False),
        (PUBLISH_TIME, ColumnType.INT64, False),
        (state_column(LOG_TIME), ColumnType.STRING, False),
        (state_column(PUBLISH_TIME), ColumnType.STRING, False),
        (SEQUENCE, ColumnType.UINT32, False),
    ]
    for step in steps:
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


def decoding_of(
    read: "Records", channel: Channel, schema: Place | None, config: AdapterConfig
) -> Decoding | NotDecoded:
    """How a channel's payloads decode, from what the channel and its schema declare."""
    name = encoding = definition = None
    if schema is not None:
        parsed = read.schema(schema)
        name, encoding = parsed.name.value, parsed.encoding.value
        start, length = parsed.data
        definition = read.content(schema)[start : start + length] if length else None
    return plan_stream(
        config=config,
        message_encoding=channel.message_encoding.value,
        schema_encoding=encoding,
        schema_name=name,
        definition=definition,
    )


def series_template(source: SourceReader, chunked: bool) -> SeriesProvenance:
    """A row cites its Message record: in the file, or in its chunk's uncompressed records."""
    step = step_template("byte_range", per_row=("length", "offset"))
    return SeriesProvenance(
        source.content_id, (step, step) if chunked else (step,), AssertionKind.OBSERVED
    )


def time_field(name: str) -> Locator:
    return adapter_locator(TIME_FIELD, {"name": name})


@dataclass(frozen=True)
class Ids:
    """The ids a channel's records get, from where it is declared."""

    stream: RecordId
    publish: RecordId


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
    def log_time(self) -> RecordId:
        return self.record_id(TimestampDomain.kind, MAGIC_PLACE, time_field("log_time"))

    def channel(self, place: Place) -> Ids:
        return Ids(
            self.record_id(Stream.kind, place),
            self.record_id(TimestampDomain.kind, place, time_field("publish_time")),
        )


class Records:
    """Reads records by place, the chunk last opened kept for the next place inside it."""

    def __init__(self, source: SourceReader, limit: int) -> None:
        self.source = source
        self.limit = limit
        self._chunk: OpenedChunk | None = None
        self._declared: dict[Place, Channel | Schema] = {}

    def load(self, channels: Iterable[Place], schemas: Iterable[Place]) -> None:
        """Parse these Channel and Schema records in file order, so each chunk holding some is
        decompressed once, however the channels' order interleaves their chunks."""
        parsers: list[Callable[[bytes], Channel | Schema]] = [parse_channel, parse_schema]
        wanted = [(place, parsers[0]) for place in channels]
        wanted += [(place, parsers[1]) for place in schemas]
        for place, parse in sorted(wanted, key=lambda item: item[0].steps):
            self._declared[place] = parse(self.content(place))
        self._chunk = None

    def channel(self, place: Place) -> Channel:
        found = self._declared.get(place)
        return found if isinstance(found, Channel) else parse_channel(self.content(place))

    def schema(self, place: Place) -> Schema:
        found = self._declared.get(place)
        return found if isinstance(found, Schema) else parse_schema(self.content(place))

    def content(self, place: Place) -> bytes:
        (offset, length), *inner = place.steps
        if not inner:
            return read_exact(self.source, offset + RECORD_HEADER, length - RECORD_HEADER)
        if self._chunk is None or self._chunk.place.steps[0] != (offset, length):
            # A place's chunk step covers what is there: less than declared if the file is cut.
            _, declared = record_header(read_exact(self.source, offset, RECORD_HEADER))
            cut = RECORD_HEADER + declared > length
            record = TopRecord(
                offset, Opcode.CHUNK, declared, None, cut=cut, present=length - RECORD_HEADER
            )
            self._chunk = open_chunk(self.source, record, self.limit)
        start, size = inner[0]
        return self._chunk.data[start + RECORD_HEADER : start + size]


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


def text_knowledge(value: Text) -> Knowledge[str]:
    """A declared string: ``Known``, or ``Unknown`` when blank or not UTF-8."""
    text = value.value
    return Known(text) if text else Unknown()


def ticks(value: int) -> int | None:
    return value if value <= INT64_MAX else None


# --- Declarations -------------------------------------------------------------------------------


class Declarations:
    """The chunk holding the clocks, the run, and a stream per channel."""

    def __init__(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> None:
        self.cite = Cite(source, config)
        self.reporter = Reporter(source, config)
        self.selection = selection(config)
        self.context = chunk.context
        self.chunked = self.context["layout"] == "chunked"
        self.records: list[EvidenceRecord] = []
        self.findings: list[IngestFinding] = []
        self.series: list[SeriesBatch] = []
        self.read = Records(source, config.integer("max_chunk_bytes"))
        self.schemas = {
            as_int(pair[0]): as_place(pair[1])
            for pair in (as_list(item) for item in as_list(self.context["schemas"]))
        }
        self.reported_schemas: set[int] = set()

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
        log_place = (time_field("log_time"),)
        log_time = TimestampDomain(
            id=cite.log_time,
            provenance=cite.provenance(MAGIC_PLACE, *log_place),
            field="log_time",
            scope=(),
            role=Known(ClockRole.RECEIVE),
            resolution=Known(NANOSECOND),
            epoch=Unknown(),
            timescale=Unknown(),
            declared_monotonic=Unknown(),
        )
        self.records.append(log_time)
        header = self.context.get("header")
        run_place = as_place(header) if header is not None else MAGIC_PLACE
        statistics = self._statistics()
        first, last = self._extent(statistics)
        run = Run(
            id=cite.record_id(Run.kind, run_place),
            provenance=cite.provenance(run_place),
            logical_id=Unknown(),
            machine=Unknown(),
            first=first,
            last=last,
        )
        self.records.append(run)
        channels = [as_list(item) for item in as_list(self.context["channels"])]
        self.read.load((as_place(where) for _, where in channels), self.schemas.values())
        counts: dict[int, tuple[int, Place]] = {}
        if statistics is not None:
            place, stats = statistics
            for channel, count, at in stats.channel_message_counts:
                entry = place.within(RECORD_HEADER + at, CHANNEL_COUNT_ENTRY)
                counts.setdefault(channel, (count, entry))
        for channel_id, where in channels:
            self._stream(as_int(channel_id), as_place(where), run.id, counts)
        return ChunkOutput(
            records=tuple(self.records),
            series=tuple(self.series),
            findings=tuple(self.findings),
        )

    def _statistics(self) -> tuple[Place, Statistics] | None:
        where = self.context.get("statistics")
        if where is None:
            return None
        place = as_place(where)
        return place, parse_statistics(self.read.content(place))

    def _extent(
        self, statistics: tuple[Place, Statistics] | None
    ) -> tuple[Knowledge[Timestamp], Knowledge[Timestamp]]:
        """The run's first and last instants, as the statistics state them on ``log_time``."""
        if statistics is None or statistics[1].message_count == 0:
            return Unknown(), Unknown()
        place, stats = statistics
        found: list[Knowledge[Timestamp]] = []
        for name, value, at in (
            ("message_start_time", stats.message_start_time, STATISTICS_START_TIME),
            ("message_end_time", stats.message_end_time, STATISTICS_END_TIME),
        ):
            field_place = place.within(RECORD_HEADER + at, 8)
            tick = ticks(value)
            if tick is None:
                found.append(Unknown(self.cite.provenance(field_place, kind=AssertionKind.STATED)))
                self.finding(
                    "time_out_of_range",
                    FindingCategory.UNREPRESENTABLE,
                    Severity.WARNING,
                    field_place,
                    f"the statistics' {name} {value} does not fit a signed 64-bit tick count;"
                    " it is unknown",
                    {"field": name, "value": value},
                )
            else:
                stated = self.cite.provenance(field_place, kind=AssertionKind.STATED)
                found.append(Known(Timestamp(tick, self.cite.log_time), stated))
        return found[0], found[1]

    def _stream(
        self,
        channel_id: int,
        place: Place,
        run: RecordId,
        counts: dict[int, tuple[int, Place]],
    ) -> None:
        cite = self.cite
        channel = self.read.channel(place)
        if channel.id != channel_id:
            raise ValueError(f"the plan's channel {channel_id} is declared as {channel.id}")
        ids = cite.channel(place)
        topic = text_knowledge(channel.topic)
        bad = [
            name
            for name, text in (
                ("topic", channel.topic),
                ("message_encoding", channel.message_encoding),
            )
            if text.value is None
        ]
        metadata = self._metadata(channel, place, bad)
        if bad:
            self.finding(
                "invalid_utf8",
                FindingCategory.UNREPRESENTABLE,
                Severity.WARNING,
                place,
                f"channel {channel_id} has text that is not UTF-8 ({', '.join(bad)}); it is unknown"
                " or left out",
                {"fields": bad, "id": channel_id},
                records=(ids.stream,),
            )
        scope = (topic.value,) if isinstance(topic, Known) else ("channel", str(channel_id))
        spec = cite.provenance(MAGIC_PLACE)
        publish = TimestampDomain(
            id=ids.publish,
            provenance=cite.provenance(place, time_field("publish_time")),
            field="publish_time",
            scope=scope,
            role=Known(ClockRole.PUBLISH, spec),
            resolution=Known(NANOSECOND, spec),
            epoch=Unknown(),
            timescale=Unknown(),
            declared_monotonic=Unknown(),
        )
        name, encoding, definition = self._schema(channel, place, ids.stream)
        schema_place = self.schemas.get(channel.schema_id) if channel.schema_id else None
        decoding = decoding_of(self.read, channel, schema_place, cite.config)
        clocks = [cite.log_time, ids.publish]
        records: list[EvidenceRecord] = [publish]
        if isinstance(decoding, Decoding) and decoding.has_header:
            assert schema_place is not None and isinstance(definition, Known)
            header = self._header_clock(place, scope, definition.value)
            clocks.append(header.id)
            records.append(header)
        count: Knowledge[int] = Unknown()
        if channel_id in counts:
            value, entry = counts[channel_id]
            count = Known(value, cite.provenance(entry, kind=AssertionKind.STATED))
        stream = Stream(
            id=ids.stream,
            provenance=cite.provenance(place),
            run=run,
            topic=topic,
            schema_name=name,
            schema_encoding=encoding,
            schema_definition=definition,
            message_encoding=text_knowledge(channel.message_encoding),
            metadata=metadata,
            clocks=tuple(clocks),
            message_count=count,
            first=Unknown(),
            last=Unknown(),
            series=series_template(cite.source, self.chunked),
        )
        self.records += [*records, stream]
        self.series.append(
            SeriesBatch(
                stream.id,
                tuple(
                    SeriesColumn(column, kind, (), repeated)
                    for column, kind, repeated in columns(self.chunked, decoding)
                ),
            )
        )
        self._decoding_findings(channel, place, stream.id, decoding)
        encoding_text = channel.message_encoding.value
        if encoding_text and encoding_text not in MESSAGE_ENCODINGS:
            self.finding(
                "unknown_encoding",
                FindingCategory.UNSUPPORTED,
                Severity.INFO,
                place,
                f"channel {channel_id}'s message encoding is not one the MCAP specification"
                " registers; it is kept as declared",
                {"encoding": encoding_text, "field": "message_encoding", "id": channel_id},
                records=(stream.id,),
            )
        selected = self.selection.selects(channel.topic)
        if not selected or self.selection.windowed:
            share = "only those in its log_time window" if selected else "none"
            self.finding(
                "not_selected",
                FindingCategory.SKIPPED,
                Severity.INFO,
                place,
                f"the config selects {share} of channel {channel_id}'s messages; the others have"
                " no rows",
                {
                    "id": channel_id,
                    "log_time_end": self.selection.end,
                    "log_time_start": self.selection.start,
                    "topic_selected": selected,
                },
                records=(stream.id,),
            )

    def _header_clock(
        self, place: Place, scope: tuple[str, ...], definition: EvidenceRef
    ) -> TimestampDomain:
        """The clock a leading ``std_msgs/Header``'s stamp reads (ADR 0068 §2): nanosecond
        ticks, as the definition's ``sec`` and ``nanosec`` (ROS 1: ``time``) declare them; its
        role, epoch and timescale are the publisher's and unstated."""
        cite = self.cite
        where = (time_field(HEADER_STAMP),)
        return TimestampDomain(
            id=cite.record_id(TimestampDomain.kind, place, *where),
            provenance=cite.provenance(place, *where),
            field=HEADER_STAMP,
            scope=scope,
            role=Unknown(),
            resolution=Known(
                NANOSECOND, Provenance(definition, cite.transform.id, AssertionKind.STATED)
            ),
            epoch=Unknown(),
            timescale=Unknown(),
            declared_monotonic=Unknown(),
        )

    def _decoding_findings(
        self, channel: Channel, place: Place, stream: RecordId, decoding: Decoding | NotDecoded
    ) -> None:
        if isinstance(decoding, NotDecoded):
            self.finding(
                "payload_not_decoded",
                FindingCategory.UNSUPPORTED,
                Severity.INFO,
                place,
                f"channel {channel.id}'s message payloads are not decoded ({decoding.detail});"
                " each row cites its message's bytes",
                {
                    "id": channel.id,
                    "message_encoding": channel.message_encoding.shown,
                    "reason": decoding.reason,
                },
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
                f"channel {channel.id}'s payloads are decoded, but {what}; each row still cites"
                " its message's bytes",
                {"id": channel.id, **decoding.details()},  # type: ignore[dict-item]
                records=(stream,),
            )

    def _metadata(
        self, channel: Channel, place: Place, bad: list[str]
    ) -> tuple[tuple[str, str], ...]:
        pairs: dict[str, list[str]] = {}
        for key, value in channel.metadata:
            if key.value is None or value.value is None:
                if "metadata" not in bad:
                    bad.append("metadata")
                continue
            pairs.setdefault(key.value, []).append(value.value)
        repeated = sorted(key for key, values in pairs.items() if len(values) > 1)
        if repeated:
            self.finding(
                "duplicate_key",
                FindingCategory.AMBIGUOUS,
                Severity.WARNING,
                place,
                f"channel {channel.id}'s metadata repeats {len(repeated)} key(s); they are left"
                " out of the stream's metadata",
                {"id": channel.id, "keys": list(repeated)},
                records=(self.cite.channel(place).stream,),
            )
        return tuple(sorted((key, values[0]) for key, values in pairs.items() if len(values) == 1))

    def _schema(
        self, channel: Channel, place: Place, stream: RecordId
    ) -> tuple[Knowledge[str], Knowledge[str], Knowledge[EvidenceRef]]:
        cite = self.cite
        if channel.schema_id == 0:  # the specification: schema_id 0 means no schema
            absent = KnownAbsent(cite.provenance(MAGIC_PLACE))
            return absent, absent, absent
        where = self.schemas.get(channel.schema_id)
        if where is None:
            self.finding(
                "unknown_schema",
                FindingCategory.CORRUPT,
                Severity.WARNING,
                place,
                f"channel {channel.id} names schema {channel.schema_id}, which no Schema record"
                " declares; its schema is unknown",
                {"id": channel.id, "schema_id": channel.schema_id},
                records=(stream,),
            )
            return Unknown(), Unknown(), Unknown()
        schema = self.read.schema(where)
        provenance = cite.provenance(where)
        start, length = schema.data
        definition: Knowledge[EvidenceRef] = (
            Known(cite.evidence(where.within(RECORD_HEADER + start, length)), provenance)
            if length
            else Unknown(provenance)
        )
        name, encoding = schema.name.value, schema.encoding.value
        if schema.id not in self.reported_schemas:
            self.reported_schemas.add(schema.id)
            self._schema_findings(schema, where)
        return (
            Known(name, provenance) if name else Unknown(provenance),
            Known(encoding, provenance) if encoding else Unknown(provenance),
            definition,
        )

    def _schema_findings(self, schema: Schema, where: Place) -> None:
        bad = [
            n for n, t in (("name", schema.name), ("encoding", schema.encoding)) if t.value is None
        ]
        if bad:
            self.finding(
                "invalid_utf8",
                FindingCategory.UNREPRESENTABLE,
                Severity.WARNING,
                where,
                f"schema {schema.id} has text that is not UTF-8 ({', '.join(bad)}); it is unknown",
                {"fields": bad, "id": schema.id},
            )
        encoding = schema.encoding.value
        if encoding and encoding not in SCHEMA_ENCODINGS:
            self.finding(
                "unknown_encoding",
                FindingCategory.UNSUPPORTED,
                Severity.INFO,
                where,
                f"schema {schema.id}'s encoding is not one the MCAP specification registers; it"
                " is kept as declared",
                {"encoding": encoding, "field": "schema_encoding", "id": schema.id},
            )

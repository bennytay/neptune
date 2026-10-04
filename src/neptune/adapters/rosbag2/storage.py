"""rosbag2's ``sqlite3`` storage: one ``.db3`` file of a bag (ADR 0045).

The file is a SQLite database with a ``topics`` table (one row per topic: name, type, serialisation
format, offered QoS profiles) and a ``messages`` table (one row per message: topic, timestamp,
payload); newer bags add ``message_definitions`` (the message type's definition text). A database
becomes what an MCAP recording becomes (ADR 0034), so that both storage backends of one recording
give the same run and streams:

- one ``Run`` citing the database header, its ``first`` and ``last`` the smallest and largest
  message timestamp, each citing the bytes of that timestamp;
- one ``TimestampDomain`` for the ``timestamp`` column (the recorder's clock, scope ``()``);
- one ``Stream`` per ``topics`` row, citing the row, with the type, serialisation and QoS as the
  row states them, the definition's bytes when ``message_definitions`` holds them, and the message
  count the planning pass counted;
- one series row per message: ``seq`` (its place among its topic's messages in rowid order),
  ``time/0``, ``value/message_id`` and ``value/data_bytes``, and the locator of the message's cell.

Planning reads every leaf page of ``messages`` once (not the payloads that spill into overflow
pages) because ``seq`` is a rank within the topic and the extent is a minimum and maximum; the
chunks are rowid ranges, each with the ``seq`` its topics start from. A chunk walks its range only.
"""

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final

from neptune.adapters.contract import (
    AdapterConfig,
    Chunk,
    ChunkOutput,
    InspectResult,
    Plan,
    SourceReader,
    make_chunk,
)
from neptune.adapters.rosbag2._cite import Cite, clip
from neptune.adapters.rosbag2._sqlite import (
    HEADER_SIZE,
    Cell,
    Database,
    SchemaEntry,
    SqliteError,
    Walk,
    column_names,
    int_value,
    read_schema,
    record_fields,
    rowid_alias,
    text_value,
)
from neptune.adapters.rosmsg.streams import (
    HEADER_STAMP,
    Declared,
    Decoding,
    NotDecoded,
    Undecoded,
    add_cells,
    decode_row,
    decoded_columns,
    decoding_report,
    header_domain,
    over_budget,
    plan_stream,
    undecoded_report,
)
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import AssertionKind, Knowledge, Known, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef, adapter_locator
from neptune.model.reference import TimestampDomain
from neptune.model.run import Run, Stream
from neptune.model.series import (
    SEQ,
    ColumnType,
    SeriesBatch,
    SeriesColumn,
    SeriesProvenance,
    locator_column,
    step_template,
    time_column,
    value_column,
)
from neptune.model.time import NANOSECOND, ClockRole, Timestamp

if TYPE_CHECKING:
    from neptune.identity.provenance import EvidenceRecord

TIME_FIELD: Final = "rosbag2:time_field"
MAX_TOPICS: Final = 100_000
LISTED: Final = 16
TOPIC_REQUIRED: Final = ("name", "type", "serialization_format")
MESSAGE_REQUIRED: Final = ("topic_id", "timestamp", "data")
DEFINITION_REQUIRED: Final = ("topic_type", "encoding", "encoded_message_definition")
MESSAGE_ID, DATA_BYTES = value_column("message_id"), value_column("data_bytes")
HEADER_CLOCK: Final = 1  # a leading header's stamp, where the payload decodes
RESERVED: Final = frozenset({"message_id", "data_bytes"})  # the row's own value columns
COLUMNS: Final = (
    (SEQ, ColumnType.INT64, False),
    (time_column(0), ColumnType.INT64, False),
    (MESSAGE_ID, ColumnType.INT64, False),
    (DATA_BYTES, ColumnType.INT64, False),
    (locator_column(0, "length"), ColumnType.INT64, False),
    (locator_column(0, "offset"), ColumnType.INT64, False),
)


def columns(
    decoding: Decoding | NotDecoded | None = None,
) -> tuple[tuple[str, ColumnType, bool], ...]:
    """Every column of a topic's series, in name order, with its type and whether it is
    repeated: the message's own, then what its payload decodes to (ADR 0068 §1)."""
    found = [*COLUMNS, *decoded_columns(decoding, HEADER_CLOCK)]
    return tuple(sorted(found))


def declared_of(
    source: SourceReader, topic: "Topic", definitions: "dict[str, Definition]"
) -> Declared:
    """A topic's row and its type's ``message_definitions`` row."""
    kind = topic.values.get("type") or None
    found = definitions.get(kind or "")
    text = None
    if found is not None and found.text is not None:
        text = source.read(found.text.offset, found.text.length)
    return Declared(
        topic.values.get("serialization_format") or None,
        found.encoding if found is not None else None,
        kind,
        text,
    )


def decodings_of(
    source: SourceReader,
    topics: "list[Topic]",
    definitions: "dict[str, Definition]",
    config: AdapterConfig,
) -> dict[int, Decoding | NotDecoded]:
    """How each topic's payloads decode, by row id. Every call reads every topic, so each
    decides the source's decoding budget alike, over the topics in row id order (ADR 0068 §1)."""
    declared = {topic.row.rowid: declared_of(source, topic, definitions) for topic in topics}
    over = set(over_budget(((rowid, partial(_given, d)) for rowid, d in declared.items()), config))
    return {
        rowid: plan_stream(
            config=config,
            message_encoding=d.message_encoding,
            schema_encoding=d.schema_encoding,
            schema_name=d.schema_name,
            definition=d.definition,
            reserved=RESERVED,
            budget=rowid not in over,
        )
        for rowid, d in declared.items()
    }


def _given(declared: Declared) -> Declared:
    return declared


PROBLEMS: Final = {
    "unreadable": "a page the file does not hold whole",
    "not_table_page": "a page that is not a table b-tree page",
    "cell_count": "a page whose cell count does not fit it",
    "cell_pointer": "a cell pointer or varint outside its page",
    "cell_overrun": "a cell that runs past its page",
    "key_order": "an interior key out of order or outside its parent's range",
    "rowid_order": "a row whose rowid is not after the rows before it, or is outside its range",
    "too_deep": "a b-tree deeper than the reader follows",
    "page_budget": "more pages visited than the file has (a cycle)",
}


@dataclass(frozen=True)
class Layout:
    """The tables a rosbag2 database must have and the position of each column in its records."""

    topics: SchemaEntry
    messages: SchemaEntry
    definitions: SchemaEntry | None
    topic_columns: dict[str, int]
    message_columns: dict[str, int]
    definition_columns: dict[str, int]


def _columns(entry: SchemaEntry, required: tuple[str, ...]) -> dict[str, int] | None:
    names = column_names(entry.sql)
    if names is None or not set(required) <= set(names):
        return None
    return {name: index for index, name in enumerate(names)}


def find_layout(schema: list[SchemaEntry]) -> Layout | None:
    """The rosbag2 tables among ``schema``, or ``None`` if ``topics`` and ``messages`` do not
    have the columns rosbag2 writes."""
    by_name: dict[str, SchemaEntry] = {}
    for entry in schema:
        by_name.setdefault(entry.name, entry)
    topics, messages = by_name.get("topics"), by_name.get("messages")
    if topics is None or messages is None:
        return None
    topic_columns = _columns(topics, TOPIC_REQUIRED)
    message_columns = _columns(messages, MESSAGE_REQUIRED)
    if topic_columns is None or message_columns is None:
        return None
    if rowid_alias(topics.sql) != "id":
        return None  # messages name a topic by its rowid; `id` must be that rowid
    definitions = by_name.get("message_definitions")
    definition_columns = _columns(definitions, DEFINITION_REQUIRED) if definitions else None
    return Layout(
        topics,
        messages,
        definitions if definition_columns else None,
        topic_columns,
        message_columns,
        definition_columns or {},
    )


@dataclass(frozen=True)
class Message:
    rowid: int
    topic: int
    ticks: int
    stamp: ByteRange  # the bytes of the timestamp (the cell, when its value has none)
    cell: ByteRange
    data_bytes: int
    payload: bytes | None = None  # the data's bytes when all of them are in the cell's page


def decode_message(cell: Cell, columns: dict[str, int]) -> Message | str:
    """A ``messages`` row, or the reason it cannot be one (``bad_record``, ``bad_type``)."""
    wanted = max(columns[name] for name in MESSAGE_REQUIRED) + 1
    try:
        fields = record_fields(cell, wanted)
        if len(fields) < wanted:
            return "bad_record"
        topic, stamp, data = (fields[columns[name]] for name in MESSAGE_REQUIRED)
        if not (topic.is_int and stamp.is_int and (data.is_blob or data.is_text)):
            return "bad_type"
        topic_id, ticks = int_value(cell, topic), int_value(cell, stamp)
    except SqliteError:
        return "bad_record"
    where = ByteRange(cell.offset, cell.length)
    place = ByteRange(stamp.offset, stamp.size) if stamp.size else where
    end = data.at + data.size
    payload = cell.local[data.at : end] if end <= len(cell.local) else None
    return Message(cell.rowid, topic_id, ticks, place, where, data.size, payload)


@dataclass(frozen=True)
class Topic:
    row: Cell
    values: dict[str, str | None]  # None: not text, not UTF-8, or not all in the page
    place: ByteRange
    definition_of: str | None = None


def read_topics(db: Database, layout: Layout, walk: Walk) -> tuple[list[Topic], bool]:
    """The ``topics`` rows in rowid order, and whether there were more than ``MAX_TOPICS``."""
    wanted = max(layout.topic_columns.values()) + 1
    topics: list[Topic] = []
    for cell in db.table(layout.topics.root, walk=walk):
        if len(topics) >= MAX_TOPICS:
            return topics, True
        values: dict[str, str | None] = {}
        try:
            fields = record_fields(cell, wanted)
        except SqliteError:
            fields = []
        for name, index in layout.topic_columns.items():
            if index < len(fields) and fields[index].is_text:
                values[name] = text_value(cell, fields[index])
            elif index < len(fields) and fields[index].is_null and name != "id":
                values[name] = ""
            else:
                values[name] = None
        topics.append(Topic(cell, values, ByteRange(cell.offset, cell.length)))
    return topics, False


@dataclass(frozen=True)
class Definition:
    encoding: str | None
    text: ByteRange | None  # the bytes of the definition, when all of them are in the page


def read_definitions(db: Database, layout: Layout) -> dict[str, Definition]:
    """The first ``message_definitions`` row of each type, by rowid."""
    found: dict[str, Definition] = {}
    if layout.definitions is None:
        return found
    columns = layout.definition_columns
    wanted = max(columns[name] for name in DEFINITION_REQUIRED) + 1
    for cell in db.table(layout.definitions.root, walk=Walk()):
        try:
            fields = record_fields(cell, wanted)
            if len(fields) < wanted:
                continue
            kind, encoding, text = (fields[columns[name]] for name in DEFINITION_REQUIRED)
            name = text_value(cell, kind) if kind.is_text else None
        except SqliteError:
            continue
        if name is None or name in found:
            continue
        local = text.is_text and text.at + text.size <= len(cell.local) and text.size > 0
        found[name] = Definition(
            text_value(cell, encoding) if encoding.is_text else None,
            ByteRange(text.offset, text.size) if local else None,
        )
    return found


def open_database(
    source: SourceReader, walk: Walk
) -> tuple[Database | None, Layout | None, list[SchemaEntry], str]:
    """The database, its rosbag2 layout (``None`` if it has none), its tables, and an error."""
    try:
        db = Database(source)
        schema = read_schema(db, walk)
    except SqliteError as exc:
        return None, None, [], str(exc)
    return db, find_layout(schema), schema, ""


# --- Inspect -------------------------------------------------------------------------------------


def inspect_storage(source: SourceReader) -> InspectResult:
    walk = Walk()
    db, layout, schema, error = open_database(source, walk)
    if db is None:
        return InspectResult({"part": "sqlite3", "readable": False, "error": error})
    summary: dict[str, JsonValue] = {
        "part": "sqlite3",
        "readable": True,
        "page_size": db.header.page_size,
        "pages": db.header.pages,
        "tables": sorted(entry.name for entry in schema)[:1000],
        "rosbag2": layout is not None,
        "wal": db.header.journal == 2,
    }
    if layout is not None:
        topics, more = read_topics(db, layout, Walk())
        summary["topics"] = [
            {"name": t.values.get("name") or "", "type": t.values.get("type") or ""}
            for t in topics[:1000]
        ]
        summary["topics_omitted"] = max(0, len(topics) - 1000) + (1 if more else 0)
    return InspectResult(summary)


# --- Plan ----------------------------------------------------------------------------------------


def _page_range(source: SourceReader, db: Database, page: int) -> ByteRange:
    size = db.header.page_size
    start = (page - 1) * size
    if start >= source.size:
        return clip(source, 0, HEADER_SIZE)
    return clip(source, start, size)


class _Problems:
    """What a walk met besides rows: damaged pages and refused rows, by reason, first first."""

    def __init__(self) -> None:
        self.pages: dict[str, list[int]] = defaultdict(list)
        self.rows: dict[str, list[Cell]] = defaultdict(list)
        self.counts: dict[str, int] = defaultdict(int)

    def problems(self, walk: Walk) -> None:
        for reason, count in walk.counts.items():
            self.counts[reason] += count
        for problem in walk.problems:  # at most MAX_PROBLEMS, so this stays small
            pages = self.pages[problem.reason]
            if problem.page not in pages:
                pages.append(problem.page)


def plan_storage(source: SourceReader, config: AdapterConfig, max_rows: int) -> Plan:
    cite = Cite(source, config)
    findings: list[IngestFinding] = []
    walk = Walk()
    db, layout, schema, error = open_database(source, walk)

    def unreadable() -> Plan:
        return Plan((make_chunk(source, config, {"part": "unreadable"}, 0),), tuple(findings))

    if db is None:
        findings.append(
            cite.finding(
                "bad_database",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                (clip(source, 0, HEADER_SIZE),),
                f"the source is not a database this reader opens: {error}; nothing is read",
                {"size": source.size},
            )
        )
        return unreadable()
    header = db.header
    if header.truncated:
        findings.append(
            cite.finding(
                "truncated",
                FindingCategory.CORRUPT,
                Severity.WARNING,
                (clip(source, 28, 4),),
                f"the file holds {source.size} bytes, {header.pages} whole pages of"
                f" {header.page_size}; the header declares {header.declared_pages or 'none'}."
                " Pages that are not there are not read",
                {
                    "declared_pages": header.declared_pages,
                    "pages": header.pages,
                    "size": source.size,
                },
            )
        )
    if header.journal == 2:
        findings.append(
            cite.finding(
                "wal_not_read",
                FindingCategory.LIMIT,
                Severity.INFO,
                (clip(source, 18, 2),),
                "the database is in WAL mode; a `-wal` file beside it is another source and is"
                " not read, so messages only it holds are missing",
                {},
            )
        )
    problems = _Problems()
    problems.problems(walk)
    if layout is None:
        findings.append(
            cite.finding(
                "not_rosbag2_storage",
                FindingCategory.UNSUPPORTED,
                Severity.ERROR,
                (clip(source, 0, HEADER_SIZE),),
                "the database has no `topics` and `messages` tables with the columns rosbag2"
                " writes; it is not read",
                {"tables": sorted(entry.name for entry in schema)[:LISTED]},
            )
        )
        _damage(cite, source, db, problems, findings)
        return unreadable()
    walk = Walk()
    topics, too_many = read_topics(db, layout, walk)
    problems.problems(walk)
    if too_many:
        findings.append(
            cite.finding(
                "too_many_topics",
                FindingCategory.LIMIT,
                Severity.ERROR,
                (ByteRange(topics[-1].place.offset, topics[-1].place.length),),
                f"the database declares more than {MAX_TOPICS} topics; the rest are not read",
                {"limit": MAX_TOPICS},
            )
        )
    declared = {topic.row.rowid for topic in topics}
    counts: dict[int, int] = defaultdict(int)
    unknown: dict[int, int] = {}  # the first LISTED undeclared ids, each with its count
    unknown_total = 0
    first_unknown: Cell | None = None
    extent: dict[str, tuple[int, ByteRange]] = {}
    chunks: list[tuple[JsonObject, int]] = []
    current: dict[str, JsonValue] = {}
    starts: dict[int, int] = {}
    rows = size = 0
    walk = Walk()
    columns = layout.message_columns
    for cell in db.table(layout.messages.root, walk=walk):
        outcome = decode_message(cell, columns)
        if isinstance(outcome, str):
            problems.counts[outcome] += 1
            if not problems.rows[outcome]:
                problems.rows[outcome].append(cell)
            continue
        if outcome.topic not in declared:
            unknown_total += 1
            if outcome.topic in unknown or len(unknown) < LISTED:
                unknown[outcome.topic] = unknown.get(outcome.topic, 0) + 1
            first_unknown = first_unknown or cell
            continue
        if "first" not in extent or outcome.ticks < extent["first"][0]:
            extent["first"] = (outcome.ticks, outcome.stamp)
        if "last" not in extent or outcome.ticks > extent["last"][0]:
            extent["last"] = (outcome.ticks, outcome.stamp)
        if not rows:
            current = {"lo": outcome.rowid}
            starts = {}
        starts.setdefault(outcome.topic, counts[outcome.topic])
        counts[outcome.topic] += 1
        rows += 1
        size += outcome.cell.length
        current["hi"] = outcome.rowid
        if rows >= max_rows:
            chunks.append((_data(current, starts), size))
            rows = size = 0
    if rows:
        chunks.append((_data(current, starts), size))
    problems.problems(walk)
    _damage(cite, source, db, problems, findings)
    if unknown and first_unknown is not None:
        findings.append(
            cite.finding(
                "unknown_topic",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                (ByteRange(first_unknown.offset, first_unknown.length),),
                f"{unknown_total} message(s) name topic id(s) that no"
                " `topics` row declares; they get no rows",
                {
                    "messages": unknown_total,
                    "topic_ids": {str(i): n for i, n in sorted(unknown.items())},
                },
            )
        )
    declarations: dict[str, JsonValue] = {
        "counts": [[topic, n] for topic, n in sorted(counts.items())],
        "part": "declarations",
    }
    for name in ("first", "last"):
        if name in extent:
            ticks, where = extent[name]
            declarations[name] = [ticks, where.offset, where.length]
    plan = [make_chunk(source, config, declarations, source.size // 64)]
    plan += [make_chunk(source, config, context, cost) for context, cost in chunks]
    return Plan(tuple(plan), tuple(findings))


def _data(current: dict[str, JsonValue], starts: dict[int, int]) -> JsonObject:
    return {
        "hi": current["hi"],
        "lo": current["lo"],
        "part": "data",
        "seq": [[topic, start] for topic, start in sorted(starts.items())],
    }


def _damage(
    cite: Cite,
    source: SourceReader,
    db: Database,
    problems: _Problems,
    findings: list[IngestFinding],
) -> None:
    for reason in sorted(problems.pages):
        pages = sorted(problems.pages[reason])
        findings.append(
            cite.finding(
                "damaged_page",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                (_page_range(source, db, pages[0]),),
                f"{PROBLEMS.get(reason, reason)}: {problems.counts[reason]} time(s) on"
                f" {len(pages)} page(s); the rows under them are not read",
                {
                    "pages": pages[:LISTED],
                    "pages_omitted": max(0, len(pages) - LISTED),
                    "problems": problems.counts[reason],
                    "reason": reason,
                },
            )
        )
    for reason in sorted(problems.rows):
        cell = problems.rows[reason][0]
        findings.append(
            cite.finding(
                "bad_row",
                FindingCategory.CORRUPT,
                Severity.WARNING,
                (ByteRange(cell.offset, cell.length),),
                f"{problems.counts[reason]} message row(s) are unusable ({reason}: the record does"
                " not parse or its topic or timestamp is not an integer); they get no rows",
                {"first_rowid": cell.rowid, "reason": reason, "rows": problems.counts[reason]},
            )
        )


# --- Ingest --------------------------------------------------------------------------------------


def ingest_storage(source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
    part = chunk.context["part"]
    if part == "unreadable":
        return ChunkOutput()  # the plan's finding says why
    db, layout, _, error = open_database(source, Walk())
    if db is None or layout is None:
        raise SqliteError(error or "the planned database is not a rosbag2 database")
    if part == "declarations":
        return _Declarations(source, chunk, config, db, layout).run()
    return _Rows(source, chunk, config, db, layout).run()


class _Streams:
    """The ids and series template of every stream, from the topics rows."""

    def __init__(self, source: SourceReader, config: AdapterConfig) -> None:
        self.cite = Cite(source, config)
        self.source = source

    def stream_id(self, topic: Topic) -> RecordId:
        return self.cite.record_id(Stream.kind, topic.place)

    @property
    def clock(self) -> RecordId:
        return self.cite.record_id(
            TimestampDomain.kind,
            ByteRange(0, 16),
            adapter_locator(TIME_FIELD, {"name": "timestamp"}),
        )


def _ints(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"expected an integer, got {value!r}")
    return value


def _pairs(value: JsonValue) -> list[list[JsonValue]]:
    if not isinstance(value, list) or not all(isinstance(item, list) for item in value):
        raise ValueError(f"expected a list of lists, got {value!r}")
    return [item for item in value if isinstance(item, list)]


class _Declarations:
    def __init__(
        self,
        source: SourceReader,
        chunk: Chunk,
        config: AdapterConfig,
        db: Database,
        layout: Layout,
    ) -> None:
        self.ids = _Streams(source, config)
        self.cite = self.ids.cite
        self.source = source
        self.context = chunk.context
        self.db = db
        self.layout = layout
        self.records: list[EvidenceRecord] = []
        self.series: list[SeriesBatch] = []
        self.findings: list[IngestFinding] = []

    def run(self) -> ChunkOutput:
        cite, source = self.cite, self.source
        spec = cite.provenance(
            ByteRange(self.layout.messages.cell.offset, self.layout.messages.cell.length)
        )
        clock = TimestampDomain(
            id=self.ids.clock,
            provenance=cite.provenance(
                ByteRange(0, 16), adapter_locator(TIME_FIELD, {"name": "timestamp"})
            ),
            field="timestamp",
            scope=(),
            role=Known(ClockRole.RECEIVE, spec),
            resolution=Known(NANOSECOND, spec),
            epoch=Unknown(),
            timescale=Unknown(),
            declared_monotonic=Unknown(),
        )
        header = clip(source, 0, HEADER_SIZE)
        run = Run(
            id=cite.record_id(Run.kind, header),
            provenance=cite.provenance(header),
            logical_id=Unknown(),
            machine=Unknown(),
            first=self._extent("first", clock.id),
            last=self._extent("last", clock.id),
        )
        self.records += [clock, run]
        counts = {_ints(t): _ints(n) for t, n in (tuple(p) for p in _pairs(self.context["counts"]))}
        topics, _ = read_topics(self.db, self.layout, Walk())
        definitions = read_definitions(self.db, self.layout)
        decodings = decodings_of(self.source, topics, definitions, cite.config)
        for topic in topics:
            self._stream(
                topic,
                run.id,
                clock.id,
                counts.get(topic.row.rowid, 0),
                definitions,
                decodings[topic.row.rowid],
            )
        return ChunkOutput(tuple(self.records), tuple(self.series), tuple(self.findings))

    def _extent(self, name: str, clock: RecordId) -> Knowledge[Timestamp]:
        found = self.context.get(name)
        if found is None:
            return Unknown()
        ticks, offset, length = (_ints(v) for v in (found if isinstance(found, list) else []))
        return Known(Timestamp(ticks, clock), self.cite.provenance(ByteRange(offset, length)))

    def _text(self, topic: Topic, name: str, stream: RecordId) -> Knowledge[str]:
        value = topic.values.get(name)
        provenance = self.cite.provenance(topic.place)
        if value is None:
            self.findings.append(
                self.cite.finding(
                    "invalid_utf8",
                    FindingCategory.UNREPRESENTABLE,
                    Severity.WARNING,
                    (topic.place,),
                    f"the topics row's `{name}` is not UTF-8 text held in its page; it is unknown",
                    {"column": name, "rowid": topic.row.rowid},
                    records=(stream,),
                )
            )
            return Unknown(provenance)
        return Known(value, provenance) if value else Unknown(provenance)

    def _stream(
        self,
        topic: Topic,
        run: RecordId,
        clock: RecordId,
        count: int,
        definitions: dict[str, Definition],
        decoding: Decoding | NotDecoded,
    ) -> None:
        cite = self.cite
        stream_id = self.ids.stream_id(topic)
        provenance = cite.provenance(topic.place)
        name = self._text(topic, "name", stream_id)
        kind = self._text(topic, "type", stream_id)
        encoding = self._text(topic, "serialization_format", stream_id)
        schema_encoding: Knowledge[str] = Unknown()
        definition: Knowledge[EvidenceRef] = Unknown()
        found = definitions.get(topic.values.get("type") or "")
        if found is not None:
            schema_encoding = Known(found.encoding, provenance) if found.encoding else Unknown()
            if found.text is not None:
                definition = Known(cite.evidence(found.text), cite.provenance(found.text))
            else:
                self.findings.append(
                    cite.finding(
                        "definition_not_local",
                        FindingCategory.LIMIT,
                        Severity.INFO,
                        (topic.place,),
                        "the type's definition spills into overflow pages; its bytes are not one"
                        " range, so the stream's definition is unknown",
                        {"type": topic.values.get("type") or ""},
                        records=(stream_id,),
                    )
                )
        metadata = tuple(
            (key, value)
            for key in ("offered_qos_profiles", "type_description_hash")
            if (value := topic.values.get(key))
        )
        clocks = [clock]
        if isinstance(decoding, Decoding) and decoding.has_header:
            assert found is not None and found.text is not None
            header = self._header_clock(topic, name, found.text)
            clocks.append(header.id)
            self.records.append(header)
        stream = Stream(
            id=stream_id,
            provenance=provenance,
            run=run,
            topic=name,
            schema_name=kind,
            schema_encoding=schema_encoding,
            schema_definition=definition,
            message_encoding=encoding,
            metadata=metadata,
            clocks=tuple(clocks),
            message_count=Known(
                count,
                cite.provenance(
                    ByteRange(self.layout.messages.cell.offset, self.layout.messages.cell.length)
                ),
            ),
            first=Unknown(),
            last=Unknown(),
            series=series_template(self.source),
        )
        self.records.append(stream)
        self.series.append(
            SeriesBatch(
                stream_id, tuple(SeriesColumn(n, t, (), r) for n, t, r in columns(decoding))
            )
        )
        self._decoding_findings(topic, stream_id, decoding)

    def _header_clock(
        self, topic: Topic, name: Knowledge[str], definition: ByteRange
    ) -> TimestampDomain:
        """The clock a leading ``std_msgs/Header``'s stamp reads (ADR 0068 §2)."""
        cite = self.cite
        where = adapter_locator(TIME_FIELD, {"name": HEADER_STAMP})
        return header_domain(
            record_id=cite.record_id(TimestampDomain.kind, topic.place, where),
            provenance=cite.provenance(topic.place, where),
            scope=(name.value,) if isinstance(name, Known) else ("topic", str(topic.row.rowid)),
            definition=cite.provenance(definition, kind=AssertionKind.STATED),
        )

    def _decoding_findings(
        self, topic: Topic, stream: RecordId, decoding: Decoding | NotDecoded
    ) -> None:
        details: dict[str, JsonValue] = {
            "rowid": topic.row.rowid,
            "serialization_format": topic.values.get("serialization_format") or "",
        }
        report = decoding_report(decoding, f"topic {topic.row.rowid}", details)
        if report is not None:
            self.findings.append(
                self.cite.finding(
                    report.code,
                    report.category,
                    report.severity,
                    (topic.place,),
                    report.message,
                    report.details,
                    records=(stream,),
                )
            )


def series_template(source: SourceReader) -> SeriesProvenance:
    """A row cites its message's cell: its length and offset in the file."""
    step = step_template("byte_range", per_row=("length", "offset"))
    return SeriesProvenance(source.content_id, (step,), AssertionKind.OBSERVED)


class _Rows:
    def __init__(
        self,
        source: SourceReader,
        chunk: Chunk,
        config: AdapterConfig,
        db: Database,
        layout: Layout,
    ) -> None:
        self.ids = _Streams(source, config)
        self.source = source
        self.context = chunk.context
        self.db = db
        self.layout = layout

    def run(self) -> ChunkOutput:
        low, high = _ints(self.context["lo"]), _ints(self.context["hi"])
        starts = {_ints(t): _ints(s) for t, s in (tuple(p) for p in _pairs(self.context["seq"]))}
        topics, _ = read_topics(self.db, self.layout, Walk())
        streams = {topic.row.rowid: self.ids.stream_id(topic) for topic in topics}
        definitions = read_definitions(self.db, self.layout)
        decodings = decodings_of(self.source, topics, definitions, self.ids.cite.config)
        kinds = {rowid: columns(decoding) for rowid, decoding in decodings.items()}
        undecoded: dict[int, Undecoded] = {}
        rows: dict[int, dict[str, list[object]]] = {}
        for cell in self.db.table(self.layout.messages.root, low, high):
            message = decode_message(cell, self.layout.message_columns)
            if isinstance(message, str) or message.topic not in streams:
                continue
            seq = starts.get(message.topic, 0)
            starts[message.topic] = seq + 1
            decoding = decodings[message.topic]
            found = rows.get(message.topic)
            if found is None:
                found = rows[message.topic] = {name: [] for name, _, _ in kinds[message.topic]}
            found[SEQ].append(seq)
            found[time_column(0)].append(message.ticks)
            found[MESSAGE_ID].append(message.rowid)
            found[DATA_BYTES].append(message.data_bytes)
            found[locator_column(0, "length")].append(message.cell.length)
            found[locator_column(0, "offset")].append(message.cell.offset)
            if isinstance(decoding, Decoding):
                decoded = decode_row(decoding, message.payload)
                add_cells(found, decoding, decoded, HEADER_CLOCK)
                reason = decoded.problem.reason if decoded.problem else None
                if message.payload is None:
                    reason = "not_local"
                if reason is not None:
                    undecoded.setdefault(message.topic, Undecoded()).add(reason, message.cell)
        batches = tuple(
            SeriesBatch(
                streams[topic],
                tuple(
                    SeriesColumn(name, kind, tuple(found[name]), repeated)  # type: ignore[arg-type]
                    for name, kind, repeated in kinds[topic]
                ),
            )
            for topic, found in sorted(rows.items())
        )
        return ChunkOutput(series=batches, findings=tuple(self._findings(undecoded, streams)))

    def _findings(
        self, undecoded: dict[int, Undecoded], streams: dict[int, RecordId]
    ) -> Iterator[IngestFinding]:
        """One finding per topic whose payloads this chunk could not all decode (or that spill
        out of their cell's page: ``not_local``)."""
        for rowid, missed in sorted(undecoded.items()):
            report = undecoded_report(missed, f"topic {rowid}", {"rowid": rowid})
            if report is None or not isinstance(missed.first, ByteRange):
                continue
            yield self.ids.cite.finding(
                report.code,
                report.category,
                report.severity,
                (missed.first,),
                report.message,
                report.details,
                records=(streams[rowid],),
            )


__all__ = ["ingest_storage", "inspect_storage", "plan_storage"]

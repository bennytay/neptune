"""``plan`` for MCAP: the findings of the head and tail, and a chunk per planned range.

Chunk 0 is the declarations; its context names the Header, the Statistics, and the record each
referenced schema and each channel is read from. Every other chunk is a data range: its bytes,
the file's layout, the chunk index records of its chunks (indexed layout), the stretch of a large
chunk's messages it emits (``first``, ``last``), and per channel with messages in it the ``seq``
its rows start from and the record its schema is read from (``channels``), or that the config
does not select it (``ignore``), or that nothing declares it (``undeclared``).

The plan also decides which channels' definitions fall past the source's decoding budget
(``over_budget``, ADR 0068 §1): it reads every channel's schema once, in channel id order, so
every chunk decides alike; a context names those of its channels (only when there are any).
"""

from collections.abc import Callable

from neptune.adapters.contract import AdapterConfig, Plan, SourceReader, make_chunk
from neptune.adapters.mcap.ingest import Records, as_int, as_list, declared_of
from neptune.adapters.mcap.layout import DATA_START, Directory, read_head, read_tail
from neptune.adapters.mcap.ranges import Layout, plan_layout, seq_starts
from neptune.adapters.mcap.records import MAGIC, RECORD_HEADER, Channel
from neptune.adapters.mcap.report import Reporter, Selection, selection
from neptune.adapters.mcap.scan import Place
from neptune.adapters.rosmsg.streams import Declared, over_budget
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.jsonvalue import JsonObject, JsonValue

_SUMMARY_PROBLEMS = {
    "bounds": "points outside the file",
    "too_large": "is larger than planning reads",
    "crc": "fails its CRC",
    "malformed": "does not parse",
}


def _declarations(
    layout: Layout,
    header: Place | None,
    statistics: Place | None,
    directory: Directory,
    over: list[int],
) -> JsonObject:
    schemas = sorted(
        {
            channel.schema_id
            for _, channel in directory.channels.values()
            if channel.schema_id in directory.schemas
        }
    )
    context: dict[str, JsonValue] = {
        "channels": [
            [channel_id, declared.place.to_json()]
            for channel_id, (declared, _) in sorted(directory.channels.items())
        ],
        "layout": "chunked" if layout.chunked else "unchunked",
        "part": "declarations",
        "schemas": [[i, directory.schemas[i].place.to_json()] for i in schemas],
    }
    if header is not None:
        context["header"] = header.to_json()
    if over:
        context["over_budget"] = list(over)
    if statistics is not None:
        context["statistics"] = statistics.to_json()
    return context


def _over_budget(
    source: SourceReader, config: AdapterConfig, directory: Directory, limit: int
) -> list[int]:
    """The channels whose definitions fall past the source's decoding budget (ADR 0068 §1)."""
    if not config.flag("decode_payloads"):
        return []
    read = Records(source, limit)
    schemas = {i: declared.place for i, declared in directory.schemas.items()}
    read.load((), sorted(set(schemas.values()), key=lambda place: place.steps))

    def declared(channel: Channel) -> Callable[[], Declared]:
        return lambda: declared_of(read, channel, schemas.get(channel.schema_id))

    return over_budget(
        (
            (channel_id, declared(channel))
            for channel_id, (_, channel) in directory.channels.items()
        ),
        config,
    )


def _data(
    layout: Layout, directory: Directory, chosen: Selection, over: list[int]
) -> list[tuple[JsonObject, int]]:
    contexts: list[tuple[JsonObject, int]] = []
    starts = seq_starts(layout.ranges, layout.chunked)
    for current, start in zip(layout.ranges, starts, strict=True):
        channels: list[JsonValue] = []
        ignore: list[JsonValue] = []
        undeclared: list[JsonValue] = []
        for channel, count in sorted(current.counts(layout.chunked).items()):
            if not count:
                continue
            if channel not in directory.channels:
                undeclared.append(channel)
                continue
            declared, parsed = directory.channels[channel]
            if chosen.selects(parsed.topic):
                entry: list[JsonValue] = [channel, declared.place.to_json(), start[channel]]
                if parsed.schema_id and parsed.schema_id in directory.schemas:
                    entry.append(directory.schemas[parsed.schema_id].place.to_json())
                channels.append(entry)
            else:
                ignore.append(channel)
        context: dict[str, JsonValue] = {
            "channels": channels,
            "end": current.end,
            "ignore": ignore,
            "layout": "chunked" if layout.chunked else "unchunked",
            "part": "data",
            "start": current.start,
            "undeclared": undeclared,
        }
        if layout.indexed:
            context["index"] = [place.to_json() for place in current.indexes]
        if current.first is not None:
            context["first"] = current.first
            if current.last is not None:
                context["last"] = current.last
        listed = {as_int(as_list(entry)[0]) for entry in channels}
        if mine := [channel for channel in over if channel in listed]:
            context["over_budget"] = list(mine)
        contexts.append((context, current.end - current.start))
    return contexts


def make_plan(source: SourceReader, config: AdapterConfig, chunk_bytes: int, max_rows: int) -> Plan:
    reporter = Reporter(source, config)
    chosen = selection(config)
    head = read_head(source)
    if not head.magic:
        finding = reporter.finding(
            "bad_magic",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            Place(((0, min(source.size, len(MAGIC))),)),
            "the source does not start with MCAP's magic; nothing of it is read",
            {"size": source.size},
        )
        return Plan((make_chunk(source, config, {"part": "unreadable"}, 0),), (finding,))
    findings: list[IngestFinding] = []
    if head.header is None:
        findings.append(
            reporter.finding(
                "corrupt_record",
                FindingCategory.CORRUPT,
                Severity.WARNING,
                Place(((DATA_START, min(RECORD_HEADER, source.size - DATA_START)),)),
                "no well-formed Header record follows the magic; the run cites the magic",
                {"reason": "header"},
            )
        )
    tail = read_tail(source)
    if tail.problem is not None:
        reason, place = tail.problem
        findings.append(
            reporter.finding(
                "summary_unusable",
                FindingCategory.CORRUPT,
                Severity.WARNING,
                place,
                f"the summary the footer points at {_SUMMARY_PROBLEMS[reason]}; the data section"
                " is planned by scanning it",
                {"reason": reason},
            )
        )
    directory = Directory.of(tail.summary)
    limit = config.integer("max_chunk_bytes")
    layout = plan_layout(source, reporter, tail, directory, chosen, limit, chunk_bytes, max_rows)
    findings += layout.findings
    if tail.footer is None and layout.data_end:
        end = layout.ranges[-1].end
        findings.append(
            reporter.finding(
                "truncated",
                FindingCategory.CORRUPT,
                Severity.WARNING,
                Place(((end, source.size - end),)),
                "the file ends after its Data End record without a footer and the closing magic;"
                " every message is read, the summary is lost",
                {"size": source.size},
            )
        )
    statistics = tail.summary.statistics[0] if tail.summary and tail.summary.statistics else None
    header = head.header[0] if head.header else None
    over = _over_budget(source, config, directory, limit)
    declarations = _declarations(layout, header, statistics, directory, over)
    cost = sum(declared.place.steps[0][1] for declared, _ in directory.channels.values())
    chunks = [make_chunk(source, config, declarations, cost)]
    for context, size in _data(layout, directory, chosen, over):
        chunks.append(make_chunk(source, config, context, size))
    return Plan(tuple(chunks), tuple(findings))

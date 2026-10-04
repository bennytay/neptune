"""``plan`` for ROS 1 bags: the findings of the head and the index, and a chunk per planned range.

Chunk 0 is the declarations; its context names the Bag Header, the extent the index states, each
connection's stated count and the record each connection is read from. Every other chunk is a data
range: the file's layout, its bytes, its units (a chunk's position and, in the indexed layout, the
Chunk Info that lists it), the stretch of a large chunk's messages it emits (``first``, ``last``),
and per connection with messages in it the ``seq`` its rows start from.

The plan also decides which connections' definitions fall past the source's decoding budget
(``over_budget``, ADR 0068 §1), reading every connection's header once in connection id order,
so every chunk decides alike; a context names those of its connections (only when there are any).
"""

from collections import Counter
from dataclasses import dataclass
from functools import partial

from neptune.adapters.contract import AdapterConfig, Plan, SourceReader, make_chunk
from neptune.adapters.rosbag1.ingest import ConnectionReader, declared_of
from neptune.adapters.rosbag1.layout import Head, Layout, Unit, plan_layout, read_head
from neptune.adapters.rosbag1.records import MAGIC, FieldError, parse_connection
from neptune.adapters.rosbag1.report import Limits, Reporter, limits
from neptune.adapters.rosbag1.scan import Place
from neptune.adapters.rosmsg.streams import Declared, over_budget
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.jsonvalue import JsonObject, JsonValue


@dataclass(frozen=True)
class RangePlan:
    """One planned data range: whole units, or one stretch of a single large unit."""

    start: int
    end: int
    units: tuple[Unit, ...]
    seq: dict[int, int]  # each declared connection's first seq here
    first: int | None = None
    last: int | None = None


def plan_ranges(layout: Layout, chunk_bytes: int, max_rows: int) -> list[RangePlan]:
    """Cut the data section into ranges of whole units of at most ``chunk_bytes`` and
    ``max_rows`` messages; a unit with more messages is read by several ranges."""
    ranges: list[RangePlan] = []
    base: Counter[int] = Counter()
    declared = layout.declared

    def snapshot() -> dict[int, int]:
        return {conn: base[conn] for conn in declared}

    pending: list[Unit] = []
    pending_seq: dict[int, int] = {}
    rows = size = 0

    def flush() -> None:
        nonlocal pending, rows, size
        if pending:
            start = layout.start if not ranges else pending[0].pos
            ranges.append(RangePlan(start, pending[-1].end, tuple(pending), pending_seq))
        pending, rows, size = [], 0, 0

    for unit in layout.units:
        length = unit.end - unit.pos
        if unit.total > max_rows:
            flush()
            stretches = -(-unit.total // max_rows)
            for number in range(stretches):
                start = layout.start if not ranges and number == 0 else unit.pos
                last = (number + 1) * max_rows if number < stretches - 1 else None
                ranges.append(
                    RangePlan(start, unit.end, (unit,), snapshot(), number * max_rows, last)
                )
        else:
            if pending and (rows + unit.total > max_rows or size + length > chunk_bytes):
                flush()
            if not pending:
                pending_seq = snapshot()
            pending.append(unit)
            rows += unit.total
            size += length
        for conn, count in unit.counts:
            base[conn] += count
    flush()
    if not ranges and layout.end > layout.start:
        ranges.append(RangePlan(layout.start, layout.end, (), {}))
    return ranges


def _over_budget(
    source: SourceReader, config: AdapterConfig, layout: Layout, bounds: Limits
) -> list[int]:
    """The connections whose definitions fall past the source's decoding budget (ADR 0068
    §1): every connection's header read once, in connection id order, so every chunk decides
    alike. A header that no longer reads costs nothing here; its chunk says why."""
    if not config.flag("decode_payloads"):
        return []
    reader = ConnectionReader(source, bounds)
    found: dict[int, Declared] = {}
    # In file order, so each chunk holding declarations is decompressed once.
    for conn, declared in sorted(layout.declared.items(), key=lambda item: item[1].place.steps):
        try:
            found[conn] = declared_of(parse_connection(reader.record(declared.place)))
        except (FieldError, ValueError):
            found[conn] = Declared(None, None, None, None)
    return over_budget(((conn, partial(_given, found[conn])) for conn in found), config)


def _given(declared: Declared) -> Declared:
    return declared


def _declarations(layout: Layout, head: Head, over: list[int]) -> JsonObject:
    context: dict[str, JsonValue] = {
        "channels": [
            [conn, declared.place.to_json()] for conn, declared in sorted(layout.declared.items())
        ],
        "layout": "indexed" if layout.indexed else "scanned",
        "part": "declarations",
    }
    if over:
        context["over_budget"] = list(over)
    if head.header is not None:
        context["header"] = head.place.to_json()
    stated = layout.stated
    if stated is not None:
        if stated.first is not None:
            context["first"] = stated.first.to_json()
        if stated.last is not None:
            context["last"] = stated.last.to_json()
        context["counts"] = [
            [conn, total, span.to_json()]
            for conn, (total, span) in sorted(stated.counts.items())
            if conn in layout.declared
        ]
    return context


def _data(layout: Layout, ranges: list[RangePlan], over: list[int]) -> list[tuple[JsonObject, int]]:
    contexts: list[tuple[JsonObject, int]] = []
    for current in ranges:
        counts: Counter[int] = Counter()
        for unit in current.units:
            counts.update(dict(unit.counts))
        channels: list[JsonValue] = [
            [conn, layout.declared[conn].place.to_json(), current.seq[conn]]
            for conn in sorted(counts)
            if conn in layout.declared
        ]
        units: list[JsonValue] = []
        for unit in current.units:
            # [pos] in a scanned layout, [pos, the Chunk Info's place] in an indexed one
            units.append([unit.pos] if unit.info is None else [unit.pos, unit.info.to_json()])
        context: dict[str, JsonValue] = {
            "channels": channels,
            "end": current.end,
            "layout": "indexed" if layout.indexed else "scanned",
            "part": "data",
            "start": current.start,
            "units": units,
        }
        if current.first is not None:
            context["first"] = current.first
            if current.last is not None:
                context["last"] = current.last
        if mine := [conn for conn in over if conn in counts and conn in layout.declared]:
            context["over_budget"] = list(mine)
        contexts.append((context, current.end - current.start))
    return contexts


_HEAD_PROBLEMS = {
    "missing": ("truncated", "the file ends after the magic: there is no Bag Header record"),
    "cut": ("truncated", "the file ends inside the Bag Header record"),
    "malformed": ("corrupt_record", "the Bag Header record does not parse; its index is not used"),
    "not_header": (
        "corrupt_record",
        "no Bag Header record follows the magic; the run cites the magic and the records are"
        " read from there",
    ),
}


def _head_findings(reporter: Reporter, head: Head, size: int) -> list[IngestFinding]:
    if head.problem is None:
        return []
    code, message = _HEAD_PROBLEMS[head.problem]
    category = FindingCategory.CORRUPT
    severity = Severity.WARNING if head.problem in ("malformed", "not_header") else Severity.ERROR
    return [
        reporter.finding(
            code, category, severity, head.place, message, {"reason": "header", "size": size}
        )
    ]


def make_plan(source: SourceReader, config: AdapterConfig, chunk_bytes: int, max_rows: int) -> Plan:
    reporter = Reporter(source, config)
    bounds: Limits = limits(config)
    head = read_head(source, bounds)
    if head.problem == "magic":
        finding = reporter.finding(
            "bad_magic",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            Place(((0, min(source.size, len(MAGIC))),)),
            "the source does not start with the ROS 1 bag 2.0 magic; nothing of it is read",
            {"size": source.size},
        )
        return Plan((make_chunk(source, config, {"part": "unreadable"}, 0),), (finding,))
    findings = _head_findings(reporter, head, source.size)
    layout = plan_layout(source, reporter, head, bounds, max_rows)
    findings += layout.findings
    ranges = plan_ranges(layout, chunk_bytes, max_rows)
    # what reading the declarations costs: each record or chunk holding them, once
    cost = sum(length for _, length in {d.place.steps[0] for d in layout.declared.values()})
    over = _over_budget(source, config, layout, bounds)
    chunks = [make_chunk(source, config, _declarations(layout, head, over), cost)]
    for context, size in _data(layout, ranges, over):
        chunks.append(make_chunk(source, config, context, size))
    return Plan(tuple(chunks), tuple(findings))

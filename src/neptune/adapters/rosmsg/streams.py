"""What the stream adapters share: whether a stream's payloads are decoded, into which columns,
and one row's cells (ADR 0068 §1).

``plan_stream`` decides once per stream, from what the stream declares (its message encoding, its
schema's encoding and name, its definition's bytes), with no default for any of them:

- ``Decoding``: the payloads are decoded. ``mode`` is ``full`` (every field has a column),
  ``partial`` (some paths are walked without a column: ``left_out`` says which and why) or
  ``header_only`` (only a leading ``std_msgs/Header`` is read, because the rest of the layout
  cannot be: past the column limit, or a construct the decoder does not read).
- ``NotDecoded``: why not (``reason``): ``disabled``, ``message_encoding``, ``schema_encoding``,
  ``definition_absent``, a definition that does not parse (its own reason), or a layout that
  cannot be built and has no header to fall back on.

A decoded stream's columns are ``value/<path>`` with a ``state/value/<path>`` beside each: a row
whose payload does not hold its layout has every value ``unknown``; one past a decoding limit, or
whose bytes are not in one cited range, ``not_covered``; text that is not UTF-8 makes only its own
cell ``unknown``.
"""

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.contract import AdapterConfig, ConfigOption
from neptune.adapters.rosmsg.codec import (
    BAD_TEXT,
    MAX_LAYOUT_NODES,
    Column,
    DecodeLimits,
    Decoder,
    LeftOut,
    Malformed,
    compile_layout,
    layout_nodes,
)
from neptune.adapters.rosmsg.definitions import (
    Definition,
    DefinitionError,
    Limits,
    parse_definition,
)
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import Known, Unknown
from neptune.model.provenance import Provenance
from neptune.model.reference import TimestampDomain
from neptune.model.series import ColumnType, state_column, time_column, value_column
from neptune.model.time import NANOSECOND

KNOWN, UNKNOWN, NOT_COVERED = "known", "unknown", "not_covered"
SCHEMAS: Final = {"cdr": frozenset({"ros2msg", "ros2idl"}), "ros1": frozenset({"ros1msg"})}
HEADER_STAMP: Final = "header.stamp"
HEADER_FRAME: Final = value_column("header.frame_id")
# The reasons a payload is left undecoded that are a decoding limit, not a fault of its bytes.
LIMIT_REASONS: Final = frozenset({"array_limit", "message_limit", "not_local", "walk_limit"})
# Left-out paths a finding lists before it only counts them.
LISTED_LEFT_OUT: Final = 64
# What planning one source's definitions may cost in all, its distinct definitions taken in
# stream order (``over_budget``): fixed bounds, not config (ADR 0068 §1).
MAX_SOURCE_DEFINITION_BYTES: Final = 16 << 20
MAX_SOURCE_LAYOUT_NODES: Final = 1 << 20
# Distinct definitions one worker keeps planned, by digest (channels share schemas).
_PLANNED_MAX: Final = 64

DECODE_OPTIONS: Final = (
    ConfigOption(
        "decode_payloads",
        True,
        "decode ROS 1 and ROS 2 (CDR) payloads into value columns by the stream's declared"
        " definition; off keeps every payload undecoded, cited by its row",
    ),
    ConfigOption(
        "max_array_items",
        DecodeLimits.max_array_items,
        "a message with an array of more items is not decoded: its values are not covered",
    ),
    ConfigOption(
        "max_decoded_columns",
        DecodeLimits.max_columns,
        "a stream whose layout needs more value columns has only its header decoded, if it has"
        " one, else none",
    ),
    ConfigOption(
        "max_message_bytes",
        DecodeLimits.max_message_bytes,
        "a larger message is not decoded: its values are not covered",
    ),
)


def with_decode_options(*options: ConfigOption) -> tuple[ConfigOption, ...]:
    """An adapter's own options and the decoding ones, sorted by name as a descriptor lists them."""
    return tuple(sorted((*options, *DECODE_OPTIONS), key=lambda option: option.name))


def limits_of(config: AdapterConfig) -> DecodeLimits:
    return DecodeLimits(
        max_columns=config.integer("max_decoded_columns"),
        max_array_items=config.integer("max_array_items"),
        max_message_bytes=config.integer("max_message_bytes"),
    )


@dataclass(frozen=True)
class NotDecoded:
    reason: str
    detail: str


@dataclass(frozen=True)
class Decoding:
    decoder: Decoder
    mode: str  # full, partial, header_only
    detail: str  # why header_only, else ""

    @property
    def columns(self) -> tuple[Column, ...]:
        return self.decoder.layout.columns

    @property
    def left_out(self) -> tuple[LeftOut, ...]:
        return self.decoder.layout.left_out

    @property
    def has_header(self) -> bool:
        return self.decoder.layout.header is not None

    @property
    def root(self) -> str:
        return self.decoder.layout.root

    def series_columns(self) -> tuple[tuple[str, ColumnType, bool], ...]:
        """Every value column and its state column: (name, type, repeated)."""
        found: list[tuple[str, ColumnType, bool]] = []
        for column in self.columns:
            name = value_column(column.path)
            found += [
                (name, column.type, column.repeated),
                (state_column(name), ColumnType.STRING, False),
            ]
        return tuple(sorted(found))

    def details(self) -> dict[str, JsonValue]:
        return {
            "columns": len(self.columns),
            "left_out": [
                {"path": item.path, "reason": item.reason}
                for item in self.left_out[:LISTED_LEFT_OUT]
            ],
            "left_out_count": len(self.left_out),
            "mode": self.mode,
            "type": self.root,
        }


@dataclass(frozen=True)
class Declared:
    """What a stream declares that its decoding is planned from."""

    message_encoding: str | None
    schema_encoding: str | None
    schema_name: str | None
    definition: bytes | None


def over_budget(
    streams: Iterable[tuple[int, Callable[[], Declared]]], config: AdapterConfig
) -> list[int]:
    """The streams of one source whose definitions fall past its decoding budget, sorted.

    A source's distinct definitions are taken in stream key order (a channel's id, a
    connection's, a topic's row id), never in the order they are met. Reading one costs its bytes
    (``MAX_SOURCE_DEFINITION_BYTES`` in all, read or not taken); taking one costs the fields its
    layout visits, counted over its type graph (``MAX_SOURCE_LAYOUT_NODES`` in all). A definition
    is taken when it fits what is left of both, else its streams are over budget and
    ``plan_stream`` leaves them undecoded (``layout_budget``); a later, smaller one may still fit.
    Every call of an adapter must decide alike, so a planner computes this once and hands it to
    its chunks, or every call computes it over all of the source's streams. Streams that would
    not be decoded anyway cost nothing.
    """
    if not config.flag("decode_payloads"):
        return []
    limits = Limits()
    decided: dict[bytes, bool] = {}
    spent_bytes = spent_nodes = 0
    found: list[int] = []
    for key, declared_of in sorted(streams, key=lambda item: item[0]):
        declared = declared_of()
        schemas = SCHEMAS.get(declared.message_encoding or "")
        definition, name = declared.definition, declared.schema_name
        if schemas is None or declared.schema_encoding not in schemas or not definition or not name:
            continue
        definition = bytes(definition)
        if len(definition) > limits.max_definition_bytes:
            continue  # refused unread
        assert declared.schema_encoding is not None
        digest = hashlib.sha256(
            b"\0".join((declared.schema_encoding.encode(), name.encode(), definition))
        ).digest()
        if digest not in decided:
            taken = False
            if spent_bytes + len(definition) <= MAX_SOURCE_DEFINITION_BYTES:
                spent_bytes += len(definition)
                nodes = _definition_cost(definition, declared.schema_encoding, name, limits)
                if spent_nodes + nodes <= MAX_SOURCE_LAYOUT_NODES:
                    spent_nodes += nodes
                    taken = True
            decided[digest] = taken
        if not decided[digest]:
            found.append(key)
    return found


def _definition_cost(definition: bytes, encoding: str, name: str, limits: Limits) -> int:
    """The fields planning a definition walks: its graph once, and the layout it compiles."""
    try:
        parsed = parse_definition(definition, encoding, name, limits)
    except DefinitionError:
        return 0
    fields = sum(len(message.fields) for message in parsed.types.values())
    nodes = layout_nodes(parsed, parsed.root_type.fields, MAX_LAYOUT_NODES)
    return fields + (nodes if nodes <= MAX_LAYOUT_NODES else 0)


def plan_stream(
    *,
    config: AdapterConfig,
    message_encoding: str | None,
    schema_encoding: str | None,
    schema_name: str | None,
    definition: bytes | None,
    reserved: frozenset[str] = frozenset(),
    budget: bool = True,
) -> Decoding | NotDecoded:
    """How a stream's payloads are decoded, or why they are not (module docstring).
    ``reserved``: paths the adapter's own value columns use, never a decoded column's.
    ``budget``: ``False`` for a stream ``over_budget`` named, which is not decoded."""
    if not config.flag("decode_payloads"):
        return NotDecoded("disabled", "the config turns payload decoding off")
    if not budget:
        return NotDecoded(
            "layout_budget",
            "the source's definitions taken before this stream's, in stream order, leave too"
            f" little of its decoding budget ({MAX_SOURCE_DEFINITION_BYTES} bytes,"
            f" {MAX_SOURCE_LAYOUT_NODES} fields) for its own",
        )
    schemas = SCHEMAS.get(message_encoding or "")
    if schemas is None:
        return NotDecoded(
            "message_encoding", f"message encoding {message_encoding!r} is not ROS 1 or CDR"
        )
    if schema_encoding not in schemas:
        return NotDecoded(
            "schema_encoding",
            f"schema encoding {schema_encoding!r} does not define {message_encoding} messages",
        )
    if definition is None or not schema_name:
        return NotDecoded("definition_absent", "the stream declares no definition to decode by")
    assert schema_encoding is not None
    definition = bytes(definition)
    limits = Limits()
    if len(definition) > limits.max_definition_bytes:  # refused before the cache sees it
        return NotDecoded(
            "definition_too_large",
            f"the definition is {len(definition)} bytes, more than {limits.max_definition_bytes}",
        )
    key = (
        hashlib.sha256(definition).digest(),
        schema_encoding,
        schema_name,
        limits_of(config),
        message_encoding == "cdr",
        reserved,
    )
    planned = _PLANNED.get(key)
    if planned is None:
        try:
            parsed = parse_definition(definition, schema_encoding, schema_name, limits)
        except DefinitionError as exc:
            planned = NotDecoded(f"definition_{exc.reason}", str(exc))
        else:
            planned = _layout(parsed, key[3], key[4], reserved)
        if len(_PLANNED) >= _PLANNED_MAX:
            _PLANNED.pop(next(iter(_PLANNED)))  # the oldest
        _PLANNED[key] = planned
    return planned


# One parse and layout per distinct definition in a worker: channels sharing a schema (often
# hundreds) share it. Keyed by the definition's digest, never its bytes; both are pure functions
# of the key, so the cache changes no output.
_PLANNED: dict[
    tuple[bytes, str, str, DecodeLimits, bool, frozenset[str]], "Decoding | NotDecoded"
] = {}


def _layout(
    parsed: Definition, limits: DecodeLimits, cdr: bool, reserved: frozenset[str]
) -> Decoding | NotDecoded:
    try:
        layout = compile_layout(parsed, limits, reserved=reserved)
    except DefinitionError as exc:
        try:
            layout = compile_layout(parsed, limits, header_only=True, reserved=reserved)
        except DefinitionError:
            return NotDecoded(f"layout_{exc.reason}", str(exc))
        return Decoding(Decoder(layout, cdr, limits), "header_only", str(exc))
    mode = "partial" if layout.left_out else "full"
    return Decoding(Decoder(layout, cdr, limits), mode, "")


@dataclass(frozen=True)
class Row:
    """One message's cells by column name, its header stamp in ns ticks (``None`` where it has
    none or it is not known), and the problem that left it undecoded, if any."""

    cells: Mapping[str, object]
    stamp: int | None
    problem: Malformed | None


def decode_row(
    decoding: Decoding,
    payload: "bytes | memoryview | Callable[[], bytes | memoryview] | None",
    size: int | None = None,
) -> Row:
    """``payload``'s cells; ``None`` (bytes not in one cited range) is ``not_covered``.

    ``payload`` may be a function that reads the bytes: given their ``size``, a payload past
    ``max_message_bytes`` is never read (``message_limit``)."""
    if payload is None:
        return Row(_absent(decoding, NOT_COVERED), None, None)
    if size is not None and size > decoding.decoder.limits.max_message_bytes:
        return Row(_absent(decoding, NOT_COVERED), None, Malformed("message_limit", limit=True))
    if callable(payload):
        payload = payload()
    try:
        values = decoding.decoder.decode(payload)
    except Malformed as problem:
        state = NOT_COVERED if problem.limit else UNKNOWN
        return Row(_absent(decoding, state), None, problem)
    cells: dict[str, object] = {}
    for column, value in zip(decoding.columns, values, strict=True):
        name = value_column(column.path)
        known = value is not BAD_TEXT and value is not None
        cells[name] = value if known else None
        cells[state_column(name)] = KNOWN if known else UNKNOWN
    return Row(cells, decoding.decoder.stamp(values), None)


def _absent(decoding: Decoding, state: str) -> dict[str, object]:
    cells: dict[str, object] = {}
    for column in decoding.columns:
        name = value_column(column.path)
        cells[name] = None
        cells[state_column(name)] = state
    return cells


@dataclass
class Undecoded:
    """Per stream, the payloads one chunk could not decode, by reason, and the first one's
    place: one finding per stream and chunk, not one per message."""

    counts: dict[str, int] = field(default_factory=dict)
    first: object | None = None

    def add(self, reason: str, place: object) -> None:
        self.counts[reason] = self.counts.get(reason, 0) + 1
        if self.first is None:
            self.first = place

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def decoded_columns(
    decoding: "Decoding | NotDecoded | None", clock: int
) -> list[tuple[str, ColumnType, bool]]:
    """The columns decoding adds to a stream's series: each value column and its state, and the
    header stamp's ``time/<clock>`` with its state where a header leads."""
    if not isinstance(decoding, Decoding):
        return []
    found = list(decoding.series_columns())
    if decoding.has_header:
        column = time_column(clock)
        found += [
            (column, ColumnType.INT64, False),
            (state_column(column), ColumnType.STRING, False),
        ]
    return found


def add_cells(rows: dict[str, list[object]], decoding: Decoding, row: Row, clock: int) -> None:
    """Append one decoded row's cells, and its header stamp on ``time/<clock>``."""
    for name, cell in row.cells.items():
        rows[name].append(cell)
    if decoding.has_header:
        column = time_column(clock)
        rows[column].append(row.stamp)
        rows[state_column(column)].append(KNOWN if row.stamp is not None else UNKNOWN)


def header_domain(
    *,
    record_id: RecordId,
    provenance: Provenance,
    scope: tuple[str, ...],
    definition: Provenance,
) -> TimestampDomain:
    """The clock a leading ``std_msgs/Header``'s stamp reads (ADR 0068 §2): nanosecond ticks,
    as the definition's ``sec`` and ``nanosec`` (ROS 1: ``time``) declare them, cited by
    ``definition``; its role, epoch and timescale are the publisher's and unstated."""
    return TimestampDomain(
        id=record_id,
        provenance=provenance,
        field=HEADER_STAMP,
        scope=scope,
        role=Unknown(),
        resolution=Known(NANOSECOND, definition),
        epoch=Unknown(),
        timescale=Unknown(),
        declared_monotonic=Unknown(),
    )


@dataclass(frozen=True)
class Report:
    """A finding's code (without the adapter's prefix), category, severity, message and details;
    each adapter files it under its own id and citation."""

    code: str
    category: FindingCategory
    severity: Severity
    message: str
    details: dict[str, JsonValue]


def decoding_report(
    decoding: "Decoding | NotDecoded", what: str, details: Mapping[str, JsonValue]
) -> Report | None:
    """The finding a stream's decoding needs: why its payloads are not decoded, or which of
    their paths have no column; ``None`` when every field has one. ``what`` names the stream as
    its format does (``channel 3``)."""
    if isinstance(decoding, NotDecoded):
        budget = decoding.reason == "layout_budget"
        return Report(
            "payload_not_decoded",
            FindingCategory.LIMIT if budget else FindingCategory.UNSUPPORTED,
            Severity.WARNING if budget else Severity.INFO,
            f"{what}'s message payloads are not decoded ({decoding.detail}); each row cites its"
            " message",
            {**details, "reason": decoding.reason},
        )
    if decoding.mode == "full":
        return None
    part = (
        f"only its header is decoded ({decoding.detail})"
        if decoding.mode == "header_only"
        else f"{len(decoding.left_out)} field path(s) are walked without a column"
    )
    return Report(
        "payload_partly_decoded",
        FindingCategory.UNSUPPORTED,
        Severity.INFO,
        f"{what}'s payloads are decoded, but {part}; each row still cites its message",
        {**details, **decoding.details()},
    )


def undecoded_report(
    undecoded: Undecoded, what: str, details: Mapping[str, JsonValue]
) -> Report | None:
    """The finding for the payloads of one stream one chunk could not decode."""
    if not undecoded.total:
        return None
    limit = set(undecoded.counts) <= LIMIT_REASONS
    return Report(
        "payload_undecodable",
        FindingCategory.LIMIT if limit else FindingCategory.CORRUPT,
        Severity.WARNING,
        f"{undecoded.total} payload(s) of {what} here do not decode by its definition (or pass a"
        " decoding limit); their values are unknown, or not covered past a limit, and each row"
        " still cites its message",
        {**details, "counts": dict(sorted(undecoded.counts.items()))},
    )

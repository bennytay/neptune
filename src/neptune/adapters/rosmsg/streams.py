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

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Final

from neptune.adapters.contract import AdapterConfig, ConfigOption
from neptune.adapters.rosmsg.codec import (
    BAD_TEXT,
    Column,
    DecodeLimits,
    Decoder,
    LeftOut,
    Malformed,
    compile_layout,
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
LIMIT_REASONS: Final = frozenset({"array_limit", "message_limit", "not_local"})

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
            "left_out": [{"path": item.path, "reason": item.reason} for item in self.left_out],
            "mode": self.mode,
            "type": self.root,
        }


def plan_stream(
    *,
    config: AdapterConfig,
    message_encoding: str | None,
    schema_encoding: str | None,
    schema_name: str | None,
    definition: bytes | None,
    reserved: frozenset[str] = frozenset(),
) -> Decoding | NotDecoded:
    """How a stream's payloads are decoded, or why they are not (module docstring).
    ``reserved``: paths the adapter's own value columns use, never a decoded column's."""
    if not config.flag("decode_payloads"):
        return NotDecoded("disabled", "the config turns payload decoding off")
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
    return _planned(
        bytes(definition),
        schema_encoding,
        schema_name,
        limits_of(config),
        message_encoding == "cdr",
        reserved,
    )


@lru_cache(maxsize=64)
def _planned(
    definition: bytes,
    schema_encoding: str,
    schema_name: str,
    limits: DecodeLimits,
    cdr: bool,
    reserved: frozenset[str],
) -> Decoding | NotDecoded:
    """One parse and layout per distinct definition in a call: channels sharing a schema (often
    hundreds) share it. Both are pure functions of these arguments."""
    try:
        parsed = parse_definition(definition, schema_encoding, schema_name, Limits())
    except DefinitionError as exc:
        return NotDecoded(f"definition_{exc.reason}", str(exc))
    return _layout(parsed, limits, cdr, reserved)


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
        return Report(
            "payload_not_decoded",
            FindingCategory.UNSUPPORTED,
            Severity.INFO,
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

"""Flight logs: PX4 ULog and ArduPilot DataFlash (``.bin``), read into runs, streams and tables.

What it emits for a log (ADR 0048), the same shape for both formats:

- one ``Run`` citing the header (ULog) or the first record header (DataFlash); a ULog's header
  timestamp is its ``first``, and an info ``sys_uuid`` its machine id (``px4.sys_uuid``), both
  ``stated``;
- one ``TimestampDomain`` for the log's boot clock: the ULog ``timestamp`` field and the DataFlash
  ``TimeUS`` (or older ``TimeMS``) column, in microseconds (milliseconds) since boot as the format
  defines them. GPS time is never merged into it: GPS records are streams like any other, their
  week and millisecond fields plain value columns;
- one ``Stream`` per declared message type (ULog subscription, DataFlash FMT record), citing its
  declaration, with the message definition's exact bytes as the schema definition and one series
  row per message: ``seq``, the clock, the decoded fields as ``value/<name>`` columns exactly as
  stored (no scaling, no unit conversion), and the exact record in the file. ULog logged messages
  and dropouts are streams too;
- structured tables for what the log states about itself: ULog flag bits, info, multi info and
  parameters (initial, default and changed); DataFlash ``PARM`` records and the declared units and
  multipliers of every column (``FMTU``, ``UNIT``, ``MULT``), each cell cited to its bytes;
- findings for everything damaged or lost: truncation, bytes skipped, unknown ids and types,
  lying lengths, dropouts, timestamps that do not fit.

Planning reads the whole log once, without building rows, and is where every finding is made, so
findings do not depend on where chunks are cut. Chunks are byte ranges cut at message boundaries.
"""

from typing import TYPE_CHECKING, Final

from neptune.adapters.contract import (
    ABI_VERSION,
    ChunkExtent,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    Documented,
    FormatSpec,
    InspectResult,
    Magic,
    Plan,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    Resources,
    SourceReader,
    make_chunk,
    read_pieces,
)
from neptune.adapters.flightlog import dataflash, ulog
from neptune.adapters.flightlog.common import Findings
from neptune.adapters.flightlog.dataflash_format import FMT_TYPE
from neptune.adapters.flightlog.dataflash_format import HEAD as DATAFLASH_HEAD
from neptune.adapters.flightlog.ulog_format import MAGIC as ULOG_MAGIC
from neptune.model.finding import FindingCategory, Severity

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject

FMT_START: Final = bytes([FMT_TYPE])
DEFAULT_CHUNK_BYTES: Final = 8 * 1024 * 1024
DEFAULT_MAX_ROWS: Final = 100_000


def _code(name: str, description: str) -> Documented:
    return Documented(f"flightlog.{name}", description)


DESCRIPTOR: Final = AdapterDescriptor(
    id="flightlog",
    version="0.1.0",
    abi=ABI_VERSION,
    summary="PX4 ULog and ArduPilot DataFlash flight logs: a run, a stream per message type with"
    " decoded rows, tables for parameters, info and declared units.",
    formats=(
        FormatSpec(
            "ArduPilot DataFlash log", extensions=(".bin",), magic=(Magic(0, b"\xa3\x95\x80"),)
        ),
        FormatSpec("ULog", extensions=(".ulg", ".ulog"), magic=(Magic(0, ULOG_MAGIC),)),
    ),
    record_kinds=("run", "stream", "structured_record", "structured_table", "timestamp_domain"),
    config=(),
    libraries=(),
    finding_codes=(
        _code(
            "appended_misaligned",
            "an appended-data offset is out of order or outside the file, or the bytes before one"
            " are not a whole message; the offset is ignored or the bytes skipped"
            " (inconsistent, warning)",
        ),
        _code(
            "bad_format",
            "a ULog format message or DataFlash FMT record does not parse or lay out (unknown"
            " type or format character, repeated name, declared length not the format's, too many"
            " columns); its subscriptions or records get no rows (corrupt, error)",
        ),
        _code("bad_magic", "the source is neither a ULog nor a DataFlash log; nothing is read"),
        _code("bad_sync", "a sync message does not hold the sync magic (corrupt, warning)"),
        _code(
            "conflicting_format",
            "a format name or message type is declared again with other content; the first is"
            " used (inconsistent, warning)",
        ),
        _code(
            "corrupt_bytes",
            "bytes that are not a message or record were skipped to the next sync message or"
            " declared record, or to the end of the range, counted with the bytes lost"
            " (corrupt, error)",
        ),
        _code(
            "dropout",
            "the logger recorded that it dropped data; each dropout is a row of the dropout stream"
            " and `amount` is their total in milliseconds (missing, warning)",
        ),
        _code(
            "duplicate_subscription",
            "a message id is subscribed again; the first subscription is used"
            " (inconsistent, warning)",
        ),
        _code(
            "invalid_utf8",
            "text that is not UTF-8 is unknown in its row or cell, never replaced"
            " (unrepresentable, warning)",
        ),
        _code(
            "limit_exceeded",
            "more formats, subscriptions or unit records than the adapter reads; the rest are"
            " not read (limit, error)",
        ),
        _code(
            "malformed_message",
            "a message is too short or not laid out as its type requires; it is skipped"
            " (corrupt, error)",
        ),
        _code(
            "misplaced_message",
            "a message in a place the format does not allow (flag bits after the first message,"
            " a format in the data section); it is skipped (inconsistent, warning)",
        ),
        _code(
            "no_time_field",
            "a format or message type has no uint64 `timestamp` / TimeUS / TimeMS column; its rows"
            " have no time (missing, warning)",
        ),
        _code(
            "size_mismatch",
            "ULog data messages shorter than their format get no rows (error); longer ones are"
            " read from their format's fields (inconsistent, warning)",
        ),
        _code(
            "time_out_of_range",
            "a time past 2^63-1 does not fit a signed tick count; the row's time is unknown"
            " (unrepresentable, warning)",
        ),
        _code(
            "truncated",
            "the log ends inside a message or record; every one before it is read (corrupt, error)",
        ),
        _code(
            "unit_undeclared",
            "a DataFlash unit or multiplier id has no UNIT or MULT record; the unit or multiplier"
            " is unknown (missing, warning)",
        ),
        _code(
            "units_not_declared",
            "a DataFlash log declares no units (no FMTU record): every unit is unknown"
            " (missing, info)",
        ),
        _code(
            "unknown_flags",
            "ULog incompatible flag bits this adapter does not know are set; the format requires"
            " refusing the messages, so only the header is read (unsupported, error)",
        ),
        _code(
            "unknown_format",
            "a ULog subscription names a format no format message defines; it gets no stream"
            " (corrupt, error)",
        ),
        _code(
            "unknown_message_id",
            "ULog data messages name an id with no usable subscription before them; no rows"
            " (corrupt, warning)",
        ),
        _code(
            "unknown_message_type",
            "ULog messages of a type the format does not define, skipped by size"
            " (unsupported, info)",
        ),
        _code(
            "unknown_version",
            "the ULog file version is newer than 1; it is read as version 1 (unsupported, warning)",
        ),
        _code(
            "unreadable_records",
            "DataFlash records of a type whose FMT cannot be laid out are skipped by their"
            " declared length (corrupt, error)",
        ),
        _code(
            "unreadable_value",
            "an info or parameter value does not match its declared type, has too many elements or"
            " is an integer past 2^63-1; it is unknown (unrepresentable, warning)",
        ),
    ),
    locator_steps=(
        Documented(
            "flightlog:time_field",
            "the time field `name` (timestamp, TimeUS, TimeMS) of the log's boot clock, cited after"
            " the header or FMT record that declares it",
        ),
    ),
    conventions=(
        Documented(
            "chunks",
            "chunk 0 holds the declarations (run, clock, streams) and the first byte range; every"
            " chunk is a byte range cut at message boundaries with each stream's seq start and"
            " each table's row start",
        ),
        Documented(
            "clocks",
            "clock 0 is the boot clock: ULog `timestamp` and DataFlash TimeUS (TimeMS in old logs)"
            " in ticks as stored, never converted; a row with no time (ULog dropouts, DataFlash"
            " types without TimeUS) has state not_covered or unknown",
        ),
        Documented(
            "dataflash_columns",
            "value/<label> for every FMT label but the time one; c C e E columns hold the raw"
            " integer (the format character says: value times 100) and L the raw degrees times"
            " 10^7; n N Z are strings up to the first NUL, a is a 32-element int16 array",
        ),
        Documented(
            "dataflash_tables",
            "parameters: one row per PARM record, cells in FMT label order, header the labels;"
            " field_units: one row per declared column with cells message, field, format"
            " character, unit id, unit, multiplier id, multiplier, each stated and cited to the"
            " FMTU, UNIT and MULT bytes; no header",
        ),
        Documented(
            "locators",
            "a row cites its whole message or record as one byte_range in the file; a table row"
            " cites its message and each cell its bytes",
        ),
        Documented(
            "ulog_columns",
            "value/<path> for every field of the format, nested types flattened as `a.b` and"
            " arrays of nested types as `a[0].b`; primitive arrays are repeated columns, char"
            " arrays strings up to the first NUL (with a state column), `_padding*` fields are"
            " dropped and the top-level uint64 `timestamp` is the time column",
        ),
        Documented(
            "ulog_streams",
            "one stream per subscription (topic the message name; metadata msg_id, multi_id and"
            " the text of every nested format used), plus `logged_message` (level as stored,"
            " message, tag) and `dropout` (duration in milliseconds) streams",
        ),
        Documented(
            "ulog_tables",
            "flag_bits (compat 8, incompat 8, appended offsets 3), info, info_multi,"
            " parameters, parameters_default (definitions section) and info_data, info_multi_data,"
            " parameter_changes, parameters_default_data (data section); rows are name, declared"
            " type, [is_continued or default types], then the value cells; no header",
        ),
    ),
    resources=Resources(max_memory=1024 * 1024 * 1024, streaming=True),
    security=(
        "A message or record is at most 65,538 (ULog) or 255 (DataFlash) bytes; the source is read"
        " through one 1 MiB window, so memory is bounded by the chunk's rows, which are capped by"
        " bytes and by count.",
        "Formats are flattened to at most 2,048 columns, 65,535 bytes, 8 levels of nesting, 4,096"
        " formats and 4,096 subscriptions; nothing is allocated from a count the file states.",
        "Damage is skipped to the next sync message (ULog) or declared record (DataFlash) in one"
        " forward pass; a problem met many times is one finding, never one per message.",
        "No decompression, no native code, no network; the standard library's struct only.",
    ),
    extent=ChunkExtent(),  # data chunks name their [start, end) bytes (ADR 0069)
)


class FlightLogAdapter:
    """The flight-log adapter. ``chunk_bytes`` and ``max_rows`` set planning granularity only."""

    descriptor = DESCRIPTOR

    def __init__(
        self, chunk_bytes: int = DEFAULT_CHUNK_BYTES, max_rows: int = DEFAULT_MAX_ROWS
    ) -> None:
        if chunk_bytes <= 0 or max_rows <= 0:
            raise ValueError(
                f"chunk_bytes and max_rows must be positive: {chunk_bytes}, {max_rows}"
            )
        self._chunk_bytes = chunk_bytes
        self._max_rows = max_rows

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        if head.startswith(ULOG_MAGIC):
            return ulog.probe(head)
        if head.startswith(DATAFLASH_HEAD + FMT_START):
            return dataflash.probe(head)
        reason = ProbeReason("flightlog.no_magic", "neither a ULog header nor a DataFlash record")
        return ProbeResult(0.0, (reason,))

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        head = _head(source)
        if head.startswith(DATAFLASH_HEAD + FMT_START):
            return dataflash.inspect(source, config)
        return ulog.inspect(source, config)

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        head = _head(source)
        if head.startswith(ULOG_MAGIC):
            return ulog.make_plan(source, config, self._chunk_bytes, self._max_rows)
        if head.startswith(DATAFLASH_HEAD + FMT_START):
            return dataflash.make_plan(source, config, self._chunk_bytes, self._max_rows)
        findings = Findings(source, config, "flightlog.")
        findings.add(
            "bad_magic",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            (0, len(head)),
            "the source is neither a ULog nor a DataFlash log; nothing of it is read",
            {"size": source.size},
        )
        context: JsonObject = {"format": "none", "part": "unreadable"}
        return Plan((make_chunk(source, config, context, 0),), findings.flush())

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        match chunk.context["format"]:
            case ulog.FORMAT:
                return ulog.ingest(source, chunk, config)
            case dataflash.FORMAT:
                return dataflash.ingest(source, chunk, config)
            case _:
                return ChunkOutput()  # "unreadable": the plan's finding says why


def _head(source: SourceReader) -> bytes:
    return b"".join(read_pieces(source, 0, min(source.size, 16))) if source.size else b""

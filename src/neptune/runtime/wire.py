"""How a call's value crosses the sandbox boundary: plain JSON, decoded strictly (ADR 0030).

The child that ran the adapter read hostile bytes and may be compromised, so its reply is data,
never code: JSON parsed by the standard library, then rebuilt only through the model's own
parsers and constructors, which validate as they build. Pickle would hand the child code
execution in the job's process.

- Records and findings travel as their ``to_json``; ADR 0024 §6 makes every record read back as
  itself, and the job checks the rebuilt output against the contract as it would any adapter's.
- Series cells travel by column type, exactly: integers and booleans as JSON, strings as
  strings, binary as hex, and every float as the hex of its eight IEEE-754 bytes, so NaN
  payloads, infinities and ``-0.0`` cross unchanged. A null cell is ``null``.
- Plans travel as their chunks' ``to_json`` and findings; probe results as their ``to_json``;
  ``inspect``'s result as its summary and findings (for dry runs, MVL-15); the probe engine's
  view of a source as ``SourceProbe.to_json``, read back by the engine.

A decoder raises on anything it does not recognise; the sandbox reports that as a crash. The byte
size of a reply is already capped (``Limits.reply_bytes``); the decode is bounded in count and in
depth too, so a reply that stays under the byte cap yet packs it with millions of empty containers
or short strings, or nests past the parser's recursion guard, is refused (``ReplyTooLarge``) and
reported as ``Limit.REPLY``, never decoded and never a crash that is retried.
"""

import json
import re
import struct
from typing import Any, Final, cast

from neptune.adapters.contract import (
    EVIDENCE_KINDS,
    ChunkOutput,
    InspectResult,
    Plan,
    ProbeResult,
    chunk_from_json,
    probe_result_from_json,
)
from neptune.discovery.probe import ProbeEngine, SourceProbe
from neptune.identity.provenance import EvidenceRecord
from neptune.model.finding import IngestFinding, ingest_finding_from_json
from neptune.model.ids import ContentId, RecordId, parse_record_id
from neptune.model.jsonvalue import JsonValue
from neptune.model.kinds import RECORD_KINDS
from neptune.model.series import Cell, ColumnType, ScalarCell, SeriesBatch, SeriesColumn
from neptune.runtime.sandbox import Codec, ReplyTooLarge

_FLOATS: Final = frozenset({ColumnType.FLOAT32, ColumnType.FLOAT64})
_INTEGERS: Final = frozenset(
    {
        ColumnType.INT8,
        ColumnType.INT16,
        ColumnType.INT32,
        ColumnType.INT64,
        ColumnType.UINT8,
        ColumnType.UINT16,
        ColumnType.UINT32,
        ColumnType.UINT64,
    }
)
_HEX: Final = re.compile(r"(?:[0-9a-f]{2})*")
_DOUBLE: Final = re.compile(r"[0-9a-f]{16}")

# The reply's byte size is already capped (``Limits.reply_bytes``); this caps how many containers
# and elements a decode may build from it, so a reply that stays under the byte cap but packs it
# with empty lists or short strings — millions of small Python objects — cannot exhaust the
# parent. This cap, not the byte cap, bounds what a hostile reply decodes to: at the defaults the
# job peaks at about 1 GiB for one object of distinct keys filling the byte cap (the parser
# memoises keys as it goes) and 0.3 to 0.6 GiB for lists (``docs/security.md``). It is no lower
# because integer and boolean series cells cost 2 to 6 bytes each on the wire, so a legitimate
# reply of 8 Mi of them is 16 to 48 MiB, already short of the byte cap.
# A value needs at least one byte, so a reply no larger than the cap can never exceed it: the
# scan runs only for the rare reply above it, and stops the moment the count is passed.
_MAX_REPLY_NODES: Final = 8 * 1024 * 1024
_OPENERS: Final = frozenset(b",[{")
_QUOTE: Final = 0x22
_BACKSLASH: Final = 0x5C


def _refuse_overlong(data: bytes) -> None:
    """Raise ``ReplyTooLarge`` if ``data`` encodes more than ``_MAX_REPLY_NODES`` values, counting
    outside strings so commas and brackets in text never inflate the count. ASCII only (the caller
    has checked), so one byte is one character and ``0x22`` is always a real quote."""
    count = 1
    in_string = escaped = False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == _BACKSLASH:
                escaped = True
            elif byte == _QUOTE:
                in_string = False
        elif byte == _QUOTE:
            in_string = True
        elif byte in _OPENERS:
            count += 1
            if count > _MAX_REPLY_NODES:
                raise ReplyTooLarge(f"a reply of over {_MAX_REPLY_NODES} values is refused")


def _loads(data: bytes) -> object:
    """``json.loads``, bounded. A sound child encodes every reply as ASCII (``ensure_ascii``), so a
    non-ASCII reply is malformed and refused before ``json.loads`` can auto-detect UTF-16 or UTF-32
    and slip past the ASCII value-count scan. Over the byte cap, a fast C-level upper bound
    (commas and brackets anywhere, strings included) decides whether the exact string-aware scan
    even has to run, so a large but legitimate reply pays only the count, not a per-byte loop."""
    if not data.isascii():
        raise ValueError("a sandbox reply is ASCII; non-ASCII bytes are a malformed reply")
    if len(data) > _MAX_REPLY_NODES:  # a smaller reply can never hold too many values
        upper_bound = data.count(b",") + data.count(b"[") + data.count(b"{")
        if upper_bound > _MAX_REPLY_NODES:  # might be over: the string-aware scan is exact
            _refuse_overlong(data)
    try:
        return json.loads(data)
    except RecursionError:  # nested past the parser's recursion guard, well under the node cap
        raise ReplyTooLarge("a reply nested too deeply to decode is refused") from None


def _dumps(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=True).encode(
        "ascii"
    )


def _object(data: object, keys: set[str], what: str) -> dict[str, JsonValue]:
    if not isinstance(data, dict) or data.keys() != keys:
        raise ValueError(f"{what} is an object with exactly {sorted(keys)}")
    return data


def _list(data: object, what: str) -> list[JsonValue]:
    if not isinstance(data, list):
        raise ValueError(f"{what} is a list")
    return data


# --- Series cells --------------------------------------------------------------------------------


def _scalar_to_json(kind: ColumnType, value: ScalarCell) -> JsonValue:
    if kind in _FLOATS:
        assert isinstance(value, float)
        return struct.pack("<d", value).hex()
    if kind is ColumnType.BINARY:
        assert isinstance(value, bytes)
        return value.hex()
    return value


def _scalar_from_json(kind: ColumnType, data: object) -> ScalarCell:
    if kind in _FLOATS:
        if not isinstance(data, str) or not _DOUBLE.fullmatch(data):
            raise ValueError(f"a {kind} cell is 16 hex digits, got {data!r}")
        value: float = struct.unpack("<d", bytes.fromhex(data))[0]
        return value
    if kind is ColumnType.BINARY:
        if not isinstance(data, str) or not _HEX.fullmatch(data):
            raise ValueError(f"a binary cell is lowercase hex, got {data!r}")
        return bytes.fromhex(data)
    if kind is ColumnType.STRING:
        if not isinstance(data, str):
            raise ValueError(f"a string cell is a string, got {data!r}")
        return data
    if kind is ColumnType.BOOL:
        if not isinstance(data, bool):
            raise ValueError(f"a bool cell is a boolean, got {data!r}")
        return data
    if kind in _INTEGERS and isinstance(data, int) and not isinstance(data, bool):
        return data
    raise ValueError(f"a {kind} cell is an integer, got {data!r}")


def _cell_to_json(column: SeriesColumn, cell: Cell) -> object:
    if cell is None:
        return None
    if isinstance(cell, tuple):
        return [_scalar_to_json(column.type, item) for item in cell]
    return _scalar_to_json(column.type, cell)


def _cell_from_json(kind: ColumnType, repeated: bool, data: object) -> Cell:
    if data is None:
        return None
    if repeated:
        return tuple(_scalar_from_json(kind, item) for item in _list(data, "a repeated cell"))
    return _scalar_from_json(kind, data)


def _batch_to_json(batch: SeriesBatch) -> object:
    return {
        "columns": [
            {
                "name": column.name,
                "repeated": column.repeated,
                "type": str(column.type),
                "values": [_cell_to_json(column, cell) for cell in column.values],
            }
            for column in batch.columns
        ],
        "stream": batch.stream,
    }


def _batch_from_json(data: object) -> SeriesBatch:
    batch = _object(data, {"columns", "stream"}, "a series batch")
    columns = []
    for item in _list(batch["columns"], "a batch's columns"):
        column = _object(item, {"name", "repeated", "type", "values"}, "a series column")
        name, repeated, kind = column["name"], column["repeated"], column["type"]
        if not isinstance(name, str) or not isinstance(repeated, bool):
            raise ValueError("a column's name is a string and its repeated flag a boolean")
        if not isinstance(kind, str):
            raise ValueError("a column's type is a string")
        column_type = ColumnType(kind)
        values = tuple(
            _cell_from_json(column_type, repeated, cell)
            for cell in _list(column["values"], "a column's values")
        )
        columns.append(SeriesColumn(name, column_type, values, repeated))
    stream = batch["stream"]
    if not isinstance(stream, str):
        raise ValueError("a batch's stream is a record id")
    return SeriesBatch(RecordId(parse_record_id(stream)), tuple(columns))


# --- Records and findings ------------------------------------------------------------------------


def _record_to_json(record: EvidenceRecord) -> object:
    data: object = cast("Any", record).to_json()  # every record kind has one (ADR 0017)
    return data


def _record_from_json(data: object) -> EvidenceRecord:
    if not isinstance(data, dict):
        raise ValueError("a record is an object")
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in EVIDENCE_KINDS:
        raise ValueError(f"not an evidence record: kind {kind!r}")
    record: EvidenceRecord = RECORD_KINDS[kind][1](data)
    return record


def _findings_from_json(data: object) -> tuple[IngestFinding, ...]:
    return tuple(ingest_finding_from_json(item) for item in _list(data, "findings"))


# --- Codecs ------------------------------------------------------------------------------------


def encode_output(output: ChunkOutput) -> bytes:
    return _dumps(
        {
            "findings": [finding.to_json() for finding in output.findings],
            "records": [_record_to_json(record) for record in output.records],
            "series": [_batch_to_json(batch) for batch in output.series],
        }
    )


def decode_output(data: bytes) -> ChunkOutput:
    value = _object(_loads(data), {"findings", "records", "series"}, "a chunk output")
    return ChunkOutput(
        records=tuple(_record_from_json(item) for item in _list(value["records"], "records")),
        series=tuple(_batch_from_json(item) for item in _list(value["series"], "series")),
        findings=_findings_from_json(value["findings"]),
    )


def encode_plan(plan: Plan) -> bytes:
    return _dumps(
        {
            "chunks": [chunk.to_json() for chunk in plan.chunks],
            "findings": [finding.to_json() for finding in plan.findings],
        }
    )


def decode_plan(data: bytes) -> Plan:
    value = _object(_loads(data), {"chunks", "findings"}, "a plan")
    return Plan(
        chunks=tuple(chunk_from_json(item) for item in _list(value["chunks"], "chunks")),
        findings=_findings_from_json(value["findings"]),
    )


def encode_probe(result: ProbeResult) -> bytes:
    return _dumps(result.to_json())


def decode_probe(data: bytes) -> ProbeResult:
    return probe_result_from_json(cast("JsonValue", _loads(data)))


def encode_inspect(result: InspectResult) -> bytes:
    return _dumps(
        {
            "findings": [finding.to_json() for finding in result.findings],
            "summary": result.summary,
        }
    )


def decode_inspect(data: bytes) -> InspectResult:
    value = _object(_loads(data), {"findings", "summary"}, "an inspect result")
    summary = value["summary"]
    if not isinstance(summary, dict):
        raise ValueError("an inspect result's summary is an object")
    return InspectResult(summary, _findings_from_json(value["findings"]))


def source_probe(
    engine: ProbeEngine, source: ContentId, size: int, name: str, head: bytes
) -> Codec[SourceProbe]:
    """The codec of the probe engine's ``probe`` of one source: its ``to_json``, read back by
    ``ProbeEngine.source_probe_from_json``, which derives again what the job can and refuses a
    reply that is not what the engine writes (ADR 0033 §1)."""

    def encode(probed: SourceProbe) -> bytes:
        return _dumps(probed.to_json())

    def decode(data: bytes) -> SourceProbe:
        value = cast("JsonValue", _loads(data))
        return engine.source_probe_from_json(value, source=source, size=size, name=name, head=head)

    return Codec(SourceProbe, encode, decode)


OUTPUT: Final = Codec(ChunkOutput, encode_output, decode_output)
PLAN: Final = Codec(Plan, encode_plan, decode_plan)
PROBE: Final = Codec(ProbeResult, encode_probe, decode_probe)
INSPECT: Final = Codec(InspectResult, encode_inspect, decode_inspect)

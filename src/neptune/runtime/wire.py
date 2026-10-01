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
- Plans travel as their chunks' ``to_json`` and findings; probe results as their ``to_json``.

A decoder raises on anything it does not recognise; the sandbox reports that as a crash.
"""

import json
import re
import struct
from collections.abc import Mapping
from typing import Any, Final, cast

from neptune.adapters.contract import (
    EVIDENCE_KINDS,
    ChunkOutput,
    Plan,
    ProbeReason,
    ProbeResult,
    chunk_from_json,
)
from neptune.identity.provenance import EvidenceRecord
from neptune.model.finding import IngestFinding, ingest_finding_from_json
from neptune.model.ids import RecordId, parse_record_id
from neptune.model.jsonvalue import JsonValue
from neptune.model.kinds import RECORD_KINDS
from neptune.model.series import Cell, ColumnType, ScalarCell, SeriesBatch, SeriesColumn
from neptune.runtime.sandbox import Codec

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
    value = _object(json.loads(data), {"findings", "records", "series"}, "a chunk output")
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
    value = _object(json.loads(data), {"chunks", "findings"}, "a plan")
    return Plan(
        chunks=tuple(chunk_from_json(item) for item in _list(value["chunks"], "chunks")),
        findings=_findings_from_json(value["findings"]),
    )


def encode_probe(result: ProbeResult) -> bytes:
    return _dumps(result.to_json())


def decode_probe(data: bytes) -> ProbeResult:
    value = json.loads(data)
    if not isinstance(value, Mapping) or not {"confidence", "reasons"} <= value.keys():
        raise ValueError("a probe result has a confidence and reasons")
    if not value.keys() <= {"confidence", "reasons", "version"}:
        raise ValueError("a probe result has a confidence, reasons and a version")
    reasons = []
    for item in _list(value["reasons"], "reasons"):
        reason = _object(item, {"code", "message"}, "a probe reason")
        code, message = reason["code"], reason["message"]
        if not isinstance(code, str) or not isinstance(message, str):
            raise ValueError("a probe reason's code and message are strings")
        reasons.append(ProbeReason(code, message))
    confidence, version = value["confidence"], value.get("version")
    if not isinstance(confidence, float) or not (version is None or isinstance(version, str)):
        raise ValueError("a probe's confidence is a float and its version a string")
    return ProbeResult(confidence, tuple(reasons), version)


OUTPUT: Final = Codec(ChunkOutput, encode_output, decode_output)
PLAN: Final = Codec(Plan, encode_plan, decode_plan)
PROBE: Final = Codec(ProbeResult, encode_probe, decode_probe)

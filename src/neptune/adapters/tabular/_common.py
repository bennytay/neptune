"""What the tabular adapter's three readers share: finding codes, limits, citations, tables.

Every finding a reader makes about rows is collected per block (``Issues``): one finding per code
per block, citing the bytes from the first affected row to the last, with a count and the first
rows it names. A block is a chunk, and its bounds depend only on the bytes and on constants of the
adapter's version, never on a setting or on the host, so the findings are as deterministic as the
records (ADR 0042).
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from neptune.adapters.contract import AdapterConfig, ContractError, SourceReader
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import AssertionKind, Knowledge
from neptune.model.provenance import ByteRange, EvidenceRef, Locator, Provenance
from neptune.model.world import CellValue, StructuredRecord, StructuredTable

ADAPTER_ID: Final = "tabular"
BOM: Final = b"\xef\xbb\xbf"
# How many rows, and record ids, a finding about a block names; it counts them all.
EXAMPLES: Final = 10

_C, _S = FindingCategory, Severity
# name: (category, severity, what it means). The descriptor lists them as ``tabular.<name>``.
CODES: Final[dict[str, tuple[FindingCategory, Severity, str]]] = {
    "csv_dialect": (
        _C.MISSING,
        _S.INFO,
        "a CSV declares no dialect, encoding or header: the details say how it was read",
    ),
    "csv_malformed_quote": (
        _C.CORRUPT,
        _S.WARNING,
        "text follows a field's closing quote; the cell keeps it after the quoted text",
    ),
    "csv_ragged_rows": (
        _C.INCONSISTENT,
        _S.WARNING,
        "rows of a block hold another number of fields than the first row; each keeps its own",
    ),
    "csv_unterminated_quote": (
        _C.CORRUPT,
        _S.WARNING,
        "the last row ends inside a quoted field: its last cell runs to the end of the file",
    ),
    "invalid_utf8": (
        _C.UNREPRESENTABLE,
        _S.WARNING,
        "cells (CSV) or rows (JSON) of a block are not UTF-8: Unknown cells, or no record",
    ),
    "json_bom": (
        _C.INCONSISTENT,
        _S.INFO,
        "a JSON text starts with a byte-order mark, which RFC 8259 forbids; it is skipped",
    ),
    "json_duplicate_key": (
        _C.AMBIGUOUS,
        _S.WARNING,
        "an object of a row repeats a key: that member's cell is Unknown, citing its pointer",
    ),
    "json_number_text": (
        _C.UNREPRESENTABLE,
        _S.INFO,
        "a JSON number no int64, uint64 or double holds exactly is kept as its literal text",
    ),
    "json_structure": (
        _C.CORRUPT,
        _S.ERROR,
        "the JSON text breaks off or breaks its grammar between rows: the rest is not read",
    ),
    "json_syntax": (
        _C.CORRUPT,
        _S.ERROR,
        "rows of a block are not valid JSON: they have no record",
    ),
    "json_too_deep": (
        _C.LIMIT,
        _S.ERROR,
        "rows of a block nest deeper than max_json_depth: they have no record",
    ),
    "parquet_column_not_decoded": (
        _C.UNREPRESENTABLE,
        _S.WARNING,
        "a column's values have no cell type (bytes, INT96, intervals, list or map items):"
        " its cells are Unknown; the values stay in the bytes",
    ),
    "parquet_encrypted": (
        _C.UNSUPPORTED,
        _S.ERROR,
        "the file's footer is encrypted (PARE); nothing is decrypted",
    ),
    "parquet_footer": (
        _C.CORRUPT,
        _S.ERROR,
        "the file has no readable footer (magic, length or metadata): nothing is decoded",
    ),
    "parquet_footer_too_large": (
        _C.LIMIT,
        _S.ERROR,
        "the footer is longer than max_footer_bytes: nothing is decoded",
    ),
    "parquet_row_group_invalid": (
        _C.CORRUPT,
        _S.ERROR,
        "a row group's declared column chunks lie outside the data the file holds: no rows",
    ),
    "parquet_row_group_too_large": (
        _C.LIMIT,
        _S.ERROR,
        "a column chunk of a row group decodes to more than max_column_chunk_bytes: no rows",
    ),
    "parquet_rows_unreadable": (
        _C.CORRUPT,
        _S.ERROR,
        "the rows of a block could not be decoded from their pages: they have no record",
    ),
    "row_limit": (
        _C.LIMIT,
        _S.ERROR,
        "the table holds more than max_rows rows: the rest are not read",
    ),
    "row_too_large": (
        _C.LIMIT,
        _S.ERROR,
        "a row is longer than max_row_bytes: it has no record",
    ),
    "too_many_columns": (
        _C.LIMIT,
        _S.ERROR,
        "rows of a block hold more cells than max_columns: they have no record",
    ),
}


class Layout(StrEnum):
    """How a source holds its rows, as its bytes show it."""

    CSV = "csv"
    JSON_ARRAY = "json_array"  # a JSON text whose root is an array: each element is a row
    JSON_LINES = "json_lines"  # one JSON text per line: each non-blank line is a row
    JSON_DOCUMENT = "json_document"  # one JSON text of another root: one row
    PARQUET = "parquet"


@dataclass(frozen=True)
class Limits:
    """The settings that bound what one row, one footer or one table may cost."""

    max_columns: int
    max_column_chunk_bytes: int
    max_footer_bytes: int
    max_json_depth: int
    max_row_bytes: int
    max_rows: int

    @staticmethod
    def of(config: AdapterConfig) -> "Limits":
        return Limits(
            max_columns=config.integer("max_columns"),
            max_column_chunk_bytes=config.integer("max_column_chunk_bytes"),
            max_footer_bytes=config.integer("max_footer_bytes"),
            max_json_depth=config.integer("max_json_depth"),
            max_row_bytes=config.integer("max_row_bytes"),
            max_rows=config.integer("max_rows"),
        )


def whole(source: SourceReader) -> EvidenceRef:
    return EvidenceRef(source.content_id, (ByteRange(0, source.size),))


def cite(source: SourceReader, *steps: Locator) -> EvidenceRef:
    return EvidenceRef(source.content_id, steps)


def bytes_at(source: SourceReader, start: int, end: int) -> EvidenceRef:
    return EvidenceRef(source.content_id, (ByteRange(start, end - start),))


def observed(evidence: EvidenceRef, config: AdapterConfig) -> Provenance:
    return Provenance(evidence, config.transform.id, AssertionKind.OBSERVED)


def record_id(kind: str, evidence: EvidenceRef, config: AdapterConfig) -> RecordId:
    return evidence_record_id(kind, evidence, config.transform)


def table(
    evidence: EvidenceRef,
    config: AdapterConfig,
    name: Knowledge[str],
    header: Knowledge[tuple[str, ...]],
) -> StructuredTable:
    return StructuredTable(
        id=record_id(StructuredTable.kind, evidence, config),
        provenance=observed(evidence, config),
        name=name,
        header=header,
    )


def row_record(
    evidence: EvidenceRef,
    config: AdapterConfig,
    table_id: RecordId,
    row: int,
    cells: Iterable[Knowledge[CellValue]],
) -> StructuredRecord:
    return StructuredRecord(
        id=record_id(StructuredRecord.kind, evidence, config),
        provenance=observed(evidence, config),
        table=table_id,
        row=row,
        cells=tuple(cells),
    )


def finding(
    config: AdapterConfig,
    name: str,
    subject: EvidenceRef,
    message: str,
    details: JsonObject,
    records: Iterable[RecordId] = (),
) -> IngestFinding:
    category, severity, _ = CODES[name]
    return ingest_finding(
        code=f"{ADAPTER_ID}.{name}",
        category=category,
        severity=severity,
        subject=subject,
        transform=config.transform,
        message=message,
        details=details,
        records=records,
    )


@dataclass
class _Issue:
    start: int
    end: int
    message: str
    details: JsonObject
    count: int = 0
    rows: list[int] = field(default_factory=list)
    records: list[RecordId] = field(default_factory=list)


class Issues:
    """One block's findings about its rows: one per code and label, however many rows."""

    def __init__(self, source: SourceReader, config: AdapterConfig) -> None:
        self._source = source
        self._config = config
        self._found: dict[tuple[str, str], _Issue] = {}

    def add(
        self,
        name: str,
        start: int,
        end: int,
        row: int,
        message: str,
        *,
        label: str = "",
        details: JsonObject | None = None,
        record: RecordId | None = None,
    ) -> None:
        """Row ``row``, bytes ``[start, end)``, has problem ``name``. ``message`` and ``details``
        are the first such row's; ``label`` separates problems of one code (a column)."""
        issue = self._found.setdefault(
            (name, label), _Issue(start, end, message, dict(details or {}))
        )
        issue.start, issue.end = min(issue.start, start), max(issue.end, end)
        issue.count += 1
        if len(issue.rows) < EXAMPLES:
            issue.rows.append(row)
        if record is not None and len(issue.records) < EXAMPLES:
            issue.records.append(record)

    def findings(self) -> list[IngestFinding]:
        out = []
        for (name, _), issue in sorted(self._found.items()):
            more = f"; {issue.count} rows in all" if issue.count > 1 else ""
            details: dict[str, JsonValue] = {
                **issue.details,
                "count": issue.count,
                "rows": list(issue.rows),
            }
            out.append(
                finding(
                    self._config,
                    name,
                    bytes_at(self._source, issue.start, issue.end),
                    f"{issue.message}{more}",
                    details,
                    issue.records,
                )
            )
        return out


def context_int(context: JsonObject, key: str) -> int:
    value = context[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"chunk context {key} must be an integer, got {value!r}")
    return value


def context_text(context: JsonObject, key: str) -> str:
    value = context[key]
    if not isinstance(value, str):
        raise ContractError(f"chunk context {key} must be text, got {value!r}")
    return value


def context_flag(context: JsonObject, key: str) -> bool:
    value = context[key]
    if not isinstance(value, bool):
        raise ContractError(f"chunk context {key} must be a bool, got {value!r}")
    return value


def shown(text: str, limit: int = 40) -> str:
    """Hostile text made safe and short for a message: escaped, at most ``limit`` characters."""
    escaped = json.dumps(text, ensure_ascii=True)
    return escaped if len(escaped) <= limit else escaped[: limit - 4] + '..."'

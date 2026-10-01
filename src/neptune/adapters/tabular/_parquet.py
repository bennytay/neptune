"""Parquet: what the footer declares as tables, and the rows decoded with the pinned pyarrow.

A file gives up to four tables, each a ``StructuredTable`` with its rows:

- the data table, citing the whole file: ``header`` is the leaf columns' paths (names joined
  with ``.``, as Parquet's column paths write them), citing the footer that declares them; row
  ``r`` (0-based across row groups) cites ``[Row(r)]`` and holds one cell per leaf column;
- ``tabular:schema``, one row per leaf column: the types the footer declares (``SCHEMA``);
- ``tabular:row_groups``, one row per column chunk of each row group: its declared counts,
  statistics, codec, encodings, sizes and offsets (``ROW_GROUPS``);
- ``tabular:key_value``, one row per key-value pair of the footer's metadata, if it has any.

Cells keep the declared types: booleans, integers of every width, floats (a FLOAT widened exactly),
strings, a DECIMAL as its exact decimal text at its declared scale, and a DATE, TIME, TIMESTAMP or
INTERVAL-free duration as the integer the file stores, whose unit and zone the schema table
gives: nothing is converted to UTC or to another unit. A null is ``KnownAbsent`` citing the
footer, which declares the column optional; an empty or whitespace-only string is ``Unknown``.
Columns whose values have no cell type (bytes, INT96, intervals, and the items of lists and maps)
are not decoded: their cells are ``Unknown`` and one finding per column says so.

Row groups are the chunk boundaries. A row group with more cells than a block holds is read in
slices of ``rows_per_block`` rows, each slice decoding its row group up to its own rows.
"""

import io
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, cast

from neptune.adapters.contract import (
    AdapterConfig,
    Chunk,
    ChunkOutput,
    Plan,
    ShortReadError,
    SourceReader,
    make_chunk,
)
from neptune.adapters.tabular._common import (
    Issues,
    Layout,
    Limits,
    bytes_at,
    cite,
    context_flag,
    context_int,
    finding,
    observed,
    record_id,
    row_record,
    table,
    whole,
)
from neptune.model.finding import IngestFinding
from neptune.model.jsonvalue import JsonObject
from neptune.model.knowledge import (
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, Row, adapter_locator
from neptune.model.scalars import real
from neptune.model.world import CellValue, StructuredRecord, StructuredTable

MAGIC: Final = b"PAR1"
ENCRYPTED: Final = b"PARE"
TAIL: Final = 8  # the footer's length (4 bytes, little-endian) and the magic
BLOCK_ROWS: Final = 8192
BLOCK_CELLS: Final = 65536
# The most blocks a table is read in; a declared row count past it is a ``row_limit``.
MAX_BLOCKS: Final = 100_000
READ_BUFFER: Final = 1024 * 1024

SCHEMA: Final = (
    "path",
    "physical_type",
    "logical_type",
    "time_unit",
    "adjusted_to_utc",
    "bit_width",
    "signed",
    "precision",
    "scale",
    "type_length",
    "converted_type",
    "max_definition_level",
    "max_repetition_level",
)
ROW_GROUPS: Final = (
    "row_group",
    "column",
    "num_rows",
    "num_values",
    "null_count",
    "distinct_count",
    "min",
    "max",
    "compression",
    "encodings",
    "total_compressed_size",
    "total_uncompressed_size",
    "data_page_offset",
    "dictionary_page_offset",
)
KEY_VALUE: Final = ("key", "value")
STEPS: Final = {
    "schema": "tabular:schema",
    "row_groups": "tabular:row_groups",
    "key_value": "tabular:key_value",
}
_TEXT_TYPES: Final = frozenset({"String", "Enum", "Json"})
_TEXT_CONVERTED: Final = frozenset({"UTF8", "ENUM", "JSON"})


class _Reader(io.RawIOBase):
    """A ``SourceReader`` as the seekable file pyarrow reads; a short read is the source's."""

    def __init__(self, source: SourceReader) -> None:
        self._source = source
        self._position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._position, io.SEEK_END: self._source.size}
        self._position = max(0, base[whence] + offset)
        return self._position

    def readinto(self, buffer: Any) -> int:
        size = self._source.size
        if self._position >= size:
            return 0
        data = self._source.read(self._position, min(len(buffer), size - self._position))
        if not data:
            raise ShortReadError(self._source.content_id, self._position, size - self._position)
        buffer[: len(data)] = data
        self._position += len(data)
        return len(data)


def _arrow() -> tuple[Any, Any]:
    """pyarrow, imported only when a Parquet source is read: CSV and JSON never pay for it."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    return pa, pq


def _decode_errors() -> tuple[type[BaseException], ...]:
    pa, _ = _arrow()
    return (pa.ArrowException, OSError, ValueError, NotImplementedError, KeyError, IndexError)


@dataclass(frozen=True)
class Footer:
    start: int
    length: int

    def ref(self, source: SourceReader, *steps: Any) -> EvidenceRef:
        return EvidenceRef(source.content_id, (ByteRange(self.start, self.length), *steps))


def _footer(source: SourceReader, config: AdapterConfig, limits: Limits) -> Footer | IngestFinding:
    size = source.size
    if size < len(MAGIC) * 2 + 4:
        return finding(
            config,
            "parquet_footer",
            whole(source),
            f"the file holds {size} bytes, too few for a Parquet file's magic and footer",
            {"size": size},
        )
    if source.read(0, len(MAGIC)) != MAGIC:
        return finding(
            config,
            "parquet_footer",
            bytes_at(source, 0, len(MAGIC)),
            "the file does not start with Parquet's magic",
            {},
        )
    tail = source.read(size - TAIL, TAIL)
    length, magic = int.from_bytes(tail[:4], "little"), tail[4:]
    end = bytes_at(source, size - TAIL, size)
    if magic == ENCRYPTED:
        return finding(
            config,
            "parquet_encrypted",
            end,
            "the footer is encrypted (PARE): nothing is decrypted or decoded",
            {},
        )
    if magic != MAGIC:
        return finding(
            config,
            "parquet_footer",
            end,
            "the file does not end with Parquet's magic: it is cut short or not Parquet",
            {},
        )
    if length > size - TAIL - len(MAGIC):
        return finding(
            config,
            "parquet_footer",
            end,
            f"the footer declares {length} bytes, more than the file holds before it",
            {"declared": length, "size": size},
        )
    if length > limits.max_footer_bytes:
        return finding(
            config,
            "parquet_footer_too_large",
            end,
            f"the footer declares {length} bytes, over max_footer_bytes"
            f" ({limits.max_footer_bytes}); nothing is decoded",
            {"declared": length, "max_footer_bytes": limits.max_footer_bytes},
        )
    return Footer(size - TAIL - length, length)


def _open(source: SourceReader, limits: Limits) -> Any:
    pa, pq = _arrow()
    return pq.ParquetFile(
        pa.PythonFile(_Reader(source), mode="r"),
        pre_buffer=False,
        buffer_size=READ_BUFFER,
        thrift_string_size_limit=limits.max_footer_bytes,
        arrow_extensions_enabled=False,
    )


@dataclass(frozen=True)
class Leaf:
    """One leaf column: its Parquet path and physical type, the Arrow columns to walk to it (a
    top-level index, then struct fields), and whether its values are decoded."""

    path: str
    physical: str
    walk: tuple[int, ...]
    decoded: bool
    why: str  # why it is not decoded


def _value_kind(pa: Any, data_type: Any) -> str:
    types = pa.types
    if types.is_dictionary(data_type):
        return _value_kind(pa, data_type.value_type)
    if types.is_struct(data_type):
        return "struct"
    if (
        types.is_list(data_type)
        or types.is_large_list(data_type)
        or types.is_fixed_size_list(data_type)
        or types.is_map(data_type)
        or getattr(types, "is_list_view", lambda _: False)(data_type)
        or getattr(types, "is_large_list_view", lambda _: False)(data_type)
    ):
        return "repeated"
    if (
        types.is_boolean(data_type)
        or types.is_integer(data_type)
        or types.is_floating(data_type)
        or types.is_string(data_type)
        or types.is_large_string(data_type)
        or getattr(types, "is_string_view", lambda _: False)(data_type)
        or types.is_decimal(data_type)
        or types.is_temporal(data_type)
        or types.is_null(data_type)
    ) and not types.is_interval(data_type):
        return "value"
    return "other"


def _count(pa: Any, data_type: Any) -> int:
    """How many Parquet leaf columns an Arrow type is stored in."""
    types = pa.types
    if types.is_dictionary(data_type):
        return _count(pa, data_type.value_type)
    if types.is_struct(data_type):
        return sum(_count(pa, data_type.field(i).type) for i in range(data_type.num_fields))
    if types.is_map(data_type):
        return _count(pa, data_type.key_type) + _count(pa, data_type.item_type)
    if _value_kind(pa, data_type) == "repeated":
        return _count(pa, data_type.value_type)
    return 1


def leaves(pf: Any) -> list[Leaf] | None:
    """Every leaf column in schema order, or ``None`` if pyarrow's reading of the schema does
    not account for the footer's leaves one to one."""
    pa, _ = _arrow()
    schema, arrow = pf.schema, pf.schema_arrow
    walks: list[tuple[tuple[int, ...], str]] = []

    def visit(data_type: Any, walk: tuple[int, ...]) -> None:
        kind = _value_kind(pa, data_type)
        if kind == "struct":
            for index in range(data_type.num_fields):
                visit(data_type.field(index).type, (*walk, index))
        elif kind == "repeated":
            walks.extend(
                [(walk, "the items of a list or map are not cells")] * _count(pa, data_type)
            )
        else:
            walks.append((walk, "" if kind == "value" else f"{data_type} has no cell type"))

    for index in range(len(arrow)):
        visit(arrow.field(index).type, (index,))
    if len(walks) != len(schema):
        return None
    found = []
    for index, (walk, why) in enumerate(walks):
        column = schema.column(index)
        if column.physical_type == "INT96":
            why = "INT96 timestamps are a deprecated encoding with no cell type"
        found.append(Leaf(column.path, column.physical_type, walk, not why, why))
    return found


def _logical(column: Any) -> dict[str, Any]:
    declared = json.loads(column.logical_type.to_json())
    return declared if isinstance(declared, dict) else {}


def _declared(value: Any, kind: type) -> Knowledge[CellValue]:
    return Known(cast("CellValue", value)) if isinstance(value, kind) else NotApplicable()


def _schema_cells(column: Any) -> list[Knowledge[CellValue]]:
    logical = _logical(column)
    name = logical.get("Type")
    decimal = name == "Decimal"
    converted = column.converted_type
    return [
        Known(column.path) if column.path else Unknown(),
        Known(column.physical_type),
        Known(name) if isinstance(name, str) and name != "None" else Unknown(),
        _declared(logical.get("timeUnit"), str),
        _declared(logical.get("isAdjustedToUTC"), bool),
        _declared(logical.get("bitWidth"), int),
        _declared(logical.get("isSigned"), bool),
        Known(column.precision) if decimal and column.precision >= 0 else NotApplicable(),
        Known(column.scale) if decimal and column.scale >= 0 else NotApplicable(),
        Known(column.length) if column.physical_type == "FIXED_LEN_BYTE_ARRAY" else NotApplicable(),
        Known(converted) if converted and converted != "NONE" else Unknown(),
        Known(column.max_definition_level),
        Known(column.max_repetition_level),
    ]


def _exact_decimal(unscaled: int, scale: int) -> str:
    return format(Decimal(unscaled).scaleb(-scale), "f")


def _statistic(raw: Any, column: Any) -> Knowledge[CellValue]:
    """A statistic as the cell its column's values would be, or ``Unknown`` if none fits."""
    logical = _logical(column).get("Type")
    if isinstance(raw, bool):
        return Known(raw)
    if isinstance(raw, int):
        if logical == "Decimal" and column.scale >= 0:
            return Known(_exact_decimal(raw, column.scale))
        return Known(raw)
    if isinstance(raw, float):
        return Known(real(raw))
    if isinstance(raw, bytes):
        if logical in _TEXT_TYPES or column.converted_type in _TEXT_CONVERTED:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return Unknown()
            return Known(text) if text.strip() else Unknown()
        if logical == "Decimal" and column.scale >= 0 and raw:
            return Known(_exact_decimal(int.from_bytes(raw, "big", signed=True), column.scale))
    return Unknown()


def _chunk_range(chunk: Any) -> tuple[int, int]:
    """The bytes a column chunk declares: from its dictionary page (if any) through its data."""
    dictionary = chunk.dictionary_page_offset if chunk.has_dictionary_page else None
    start = dictionary if isinstance(dictionary, int) and dictionary > 0 else chunk.data_page_offset
    return start, start + chunk.total_compressed_size


def _group_range(group: Any, footer: Footer) -> tuple[int, int]:
    ranges = [_chunk_range(group.column(c)) for c in range(group.num_columns)]
    start = max(0, min((r[0] for r in ranges), default=0))
    end = min(footer.start, max((r[1] for r in ranges), default=0))
    return (start, end) if start < end else (max(0, footer.start - 1), footer.start)


def _check_group(
    group: Any, index: int, footer: Footer, found: list[Leaf], limits: Limits
) -> tuple[str, str, JsonObject] | None:
    """Why row group ``index`` cannot be read, as a finding's name, message and details."""
    if group.num_rows < 0:
        return (
            "parquet_row_group_invalid",
            f"row group {index} declares {group.num_rows} rows",
            {"num_rows": group.num_rows, "row_group": index},
        )
    if group.num_columns != len(found):
        return (
            "parquet_row_group_invalid",
            f"row group {index} declares {group.num_columns} column chunks for"
            f" {len(found)} columns",
            {"columns": group.num_columns, "row_group": index},
        )
    for column in range(group.num_columns):
        chunk = group.column(column)
        start, end = _chunk_range(chunk)
        if start < len(MAGIC) or chunk.total_compressed_size < 0 or end > footer.start:
            return (
                "parquet_row_group_invalid",
                f"row group {index}'s column {found[column].path!r} declares bytes"
                f" [{start}, {end}), outside the data before the footer at {footer.start}",
                {"column": found[column].path, "end": end, "row_group": index, "start": start},
            )
        size = chunk.total_uncompressed_size
        if found[column].decoded and size > limits.max_column_chunk_bytes:
            return (
                "parquet_row_group_too_large",
                f"row group {index}'s column {found[column].path!r} decodes to {size} bytes, over"
                f" max_column_chunk_bytes ({limits.max_column_chunk_bytes}); it is not read",
                {
                    "bytes": size,
                    "column": found[column].path,
                    "max_column_chunk_bytes": limits.max_column_chunk_bytes,
                    "row_group": index,
                },
            )
    return None


def _none(source: SourceReader, config: AdapterConfig, problem: IngestFinding) -> Plan:
    return Plan((make_chunk(source, config, {"layout": "parquet", "part": "none"}, 0),), (problem,))


def plan(source: SourceReader, config: AdapterConfig, limits: Limits) -> Plan:
    footer = _footer(source, config, limits)
    if isinstance(footer, IngestFinding):
        return _none(source, config, footer)
    try:
        pf = _open(source, limits)
        metadata = pf.metadata
        found = leaves(pf)
    except _decode_errors() as exc:
        return _none(source, config, _unreadable_footer(source, config, footer, exc))
    if found is None:
        return _none(source, config, _unmapped(source, config, footer))
    if len(found) > limits.max_columns:
        return _none(
            source,
            config,
            finding(
                config,
                "too_many_columns",
                footer.ref(source),
                f"the schema declares {len(found)} leaf columns, over max_columns"
                f" ({limits.max_columns}); nothing is decoded",
                {"columns": len(found), "max_columns": limits.max_columns},
            ),
        )
    base: JsonObject = {
        "footer_length": footer.length,
        "footer_start": footer.start,
        "layout": Layout.PARQUET.value,
    }
    chunks = [make_chunk(source, config, {**base, "part": "table"}, footer.length)]
    findings: list[IngestFinding] = []
    per_block = max(1, min(BLOCK_ROWS, BLOCK_CELLS // max(1, len(found))))
    row, blocks, limited = 0, 0, False
    for index in range(metadata.num_row_groups):
        group = metadata.row_group(index)
        problem = _check_group(group, index, footer, found, limits)
        declared = max(0, group.num_rows)
        count = 0 if problem is not None or limited else declared
        if problem is not None:
            name, message, details = problem
            findings.append(
                finding(
                    config, name, bytes_at(source, *_group_range(group, footer)), message, details
                )
            )
        if count and (
            row + count > limits.max_rows or blocks + -(-count // per_block) > MAX_BLOCKS
        ):
            limited = True
            count = 0
            findings.append(
                finding(
                    config,
                    "row_limit",
                    bytes_at(source, *_group_range(group, footer)),
                    f"row group {index} would take the table past max_rows ({limits.max_rows})"
                    f" rows or {MAX_BLOCKS} blocks; rows from {row} on are not read",
                    {"max_blocks": MAX_BLOCKS, "max_rows": limits.max_rows, "row": row},
                )
            )
        first = 0
        while True:
            take = min(per_block, count - first)
            context: JsonObject = {
                **base,
                "count": take,
                "first": first,
                "part": "row_group",
                "row": row + first,
                "row_group": index,
                "rows_per_block": per_block,
                "statistics": first == 0 and group.num_columns == len(found),
            }
            low, high = _group_range(group, footer)
            chunks.append(make_chunk(source, config, context, high - low if take else 0))
            blocks += 1
            first += take
            if first >= count:
                break
        row += declared
    return Plan(tuple(chunks), tuple(findings))


def _unreadable_footer(
    source: SourceReader, config: AdapterConfig, footer: Footer, exc: BaseException
) -> IngestFinding:
    return finding(
        config,
        "parquet_footer",
        footer.ref(source),
        f"the footer's metadata does not decode ({type(exc).__name__}): nothing is decoded",
        {"error": type(exc).__name__},
    )


def _unmapped(source: SourceReader, config: AdapterConfig, footer: Footer) -> IngestFinding:
    return finding(
        config,
        "parquet_footer",
        footer.ref(source),
        "the schema's leaf columns do not map one to one onto the columns pyarrow reads",
        {},
    )


def _footer_of(context: JsonObject) -> Footer:
    return Footer(context_int(context, "footer_start"), context_int(context, "footer_length"))


def _aux(source: SourceReader, config: AdapterConfig, footer: Footer, name: str) -> StructuredTable:
    evidence = footer.ref(source, adapter_locator(STEPS[name], {}))
    return table(evidence, config, NotCovered(), NotApplicable())


def _aux_row(
    source: SourceReader,
    config: AdapterConfig,
    owner: StructuredTable,
    row: int,
    cells: list[Knowledge[CellValue]],
) -> StructuredRecord:
    evidence = EvidenceRef(source.content_id, (*owner.provenance.evidence.locator, Row(row)))
    return row_record(evidence, config, owner.id, row, cells)


def _table(
    source: SourceReader, config: AdapterConfig, limits: Limits, context: JsonObject
) -> ChunkOutput:
    footer = _footer_of(context)
    pf = _open(source, limits)
    metadata = pf.metadata
    found = leaves(pf)
    if found is None:  # the plan checked; the bytes are the same
        return ChunkOutput()
    header = Known(tuple(leaf.path for leaf in found), observed(footer.ref(source), config))
    data = table(whole(source), config, NotCovered(), header)
    records: list[Any] = [data]
    schema = _aux(source, config, footer, "schema")
    records.append(schema)
    for index in range(len(found)):
        records.append(
            _aux_row(source, config, schema, index, _schema_cells(pf.schema.column(index)))
        )
    if metadata.num_row_groups:
        records.append(_aux(source, config, footer, "row_groups"))
    issues = Issues(source, config)
    pairs = metadata.metadata or {}
    if pairs:
        key_value = _aux(source, config, footer, "key_value")
        records.append(key_value)
        for index, (key, value) in enumerate(pairs.items()):
            cells = [_text_cell(key), _text_cell(value)]
            if any(cell is None for cell in cells):
                issues.add(
                    "invalid_utf8",
                    footer.start,
                    footer.start + footer.length,
                    index,
                    f"key-value pair {index} of the footer is not UTF-8; it is Unknown",
                    label="key_value",
                )
            records.append(
                _aux_row(
                    source,
                    config,
                    key_value,
                    index,
                    [cell if cell is not None else Unknown() for cell in cells],
                )
            )
    findings = list(issues.findings())
    for leaf in found:
        if not leaf.decoded:
            findings.append(
                finding(
                    config,
                    "parquet_column_not_decoded",
                    footer.ref(source),
                    f"column {leaf.path!r} is not decoded: {leaf.why}; its cells are Unknown",
                    {"column": leaf.path, "physical_type": leaf.physical},
                    (data.id,),
                )
            )
    return ChunkOutput(records=tuple(records), findings=tuple(findings))


def _text_cell(raw: bytes) -> Knowledge[CellValue] | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return Known(text) if text.strip() else Unknown()


def _statistics(
    source: SourceReader,
    config: AdapterConfig,
    pf: Any,
    footer: Footer,
    index: int,
    found: list[Leaf],
) -> list[StructuredRecord]:
    owner = _aux(source, config, footer, "row_groups")
    group = pf.metadata.row_group(index)
    out = []
    for column in range(group.num_columns):
        chunk = group.column(column)
        stats = chunk.statistics if chunk.is_stats_set else None
        declared = pf.schema.column(column)
        has_minmax = stats is not None and stats.has_min_max
        minimum = _statistic(stats.min_raw, declared) if stats and has_minmax else Unknown()
        maximum = _statistic(stats.max_raw, declared) if stats and has_minmax else Unknown()
        dictionary = chunk.dictionary_page_offset if chunk.has_dictionary_page else None
        cells: list[Knowledge[CellValue]] = [
            Known(index),
            Known(found[column].path) if found[column].path else Unknown(),
            Known(group.num_rows),
            Known(chunk.num_values),
            Known(stats.null_count) if stats is not None and stats.has_null_count else Unknown(),
            Known(stats.distinct_count)
            if stats is not None and stats.has_distinct_count
            else Unknown(),
            minimum,
            maximum,
            Known(chunk.compression),
            Known(",".join(chunk.encodings)) if chunk.encodings else Unknown(),
            Known(chunk.total_compressed_size),
            Known(chunk.total_uncompressed_size),
            Known(chunk.data_page_offset),
            Known(dictionary) if isinstance(dictionary, int) else NotApplicable(),
        ]
        out.append(_aux_row(source, config, owner, index * group.num_columns + column, cells))
    return out


def _arrays(pa: Any, batch: Any, found: list[Leaf]) -> list[list[Any] | None]:
    """Each leaf's values in the batch as Python values, ``None`` for a leaf not decoded."""
    out: list[list[Any] | None] = []
    for leaf in found:
        if not leaf.decoded:
            out.append(None)
            continue
        array = batch.column(leaf.walk[0])
        for field in leaf.walk[1:]:
            array = array.flatten()[field]  # the parent's nulls carried into each field
        types = pa.types
        if types.is_dictionary(array.type):
            array = array.dictionary_decode()
        kind = array.type
        if types.is_date32(kind) or types.is_time32(kind):
            array = array.view(pa.int32())
        elif (
            types.is_date64(kind)
            or types.is_time64(kind)
            or types.is_timestamp(kind)
            or types.is_duration(kind)
        ):
            array = array.view(pa.int64())
        elif types.is_float16(kind):
            array = array.cast(pa.float32())
        values = array.to_pylist()
        if types.is_decimal(kind):
            values = [None if v is None else format(v, "f") for v in values]
        out.append(values)
    return out


def _value_cell(value: Any, absent: Provenance) -> Knowledge[CellValue]:
    if value is None:
        return KnownAbsent(absent)
    if isinstance(value, float):
        return Known(real(value))
    if isinstance(value, str):
        return Known(value) if value.strip() else Unknown()
    return Known(value)


def _rows(
    source: SourceReader, config: AdapterConfig, limits: Limits, context: JsonObject
) -> ChunkOutput:
    footer = _footer_of(context)
    index, first = context_int(context, "row_group"), context_int(context, "first")
    count, row = context_int(context, "count"), context_int(context, "row")
    per_block = context_int(context, "rows_per_block")
    pa, _ = _arrow()
    pf = _open(source, limits)
    found = leaves(pf)
    if found is None:
        return ChunkOutput()
    records: list[StructuredRecord] = []
    if context_flag(context, "statistics") and pf.metadata.row_group(index).num_columns == len(
        found
    ):
        records.extend(_statistics(source, config, pf, footer, index, found))
    if not count:
        return ChunkOutput(records=tuple(records))
    absent = observed(footer.ref(source), config)
    table_id = record_id(StructuredTable.kind, whole(source), config)
    columns = sorted({leaf.walk[0] for leaf in found if leaf.decoded})
    if not columns and found:  # nothing to decode: read one column anyway, to count the rows
        columns = [found[0].walk[0]]
    position = {column: at for at, column in enumerate(columns)}
    # The batches hold only the columns read, so each leaf walks from its place among them.
    walked = [
        Leaf(leaf.path, leaf.physical, (position[leaf.walk[0]], *leaf.walk[1:]), True, "")
        if leaf.decoded
        else leaf
        for leaf in found
    ]
    pieces: list[tuple[int, list[list[Any] | None]]] = []
    if not columns:
        pieces.append((count, [None] * len(found)))
    else:
        names = [pf.schema_arrow.field(column).name for column in columns]
        try:
            seen = 0
            for batch in pf.iter_batches(
                batch_size=per_block, row_groups=[index], columns=names, use_threads=False
            ):
                if seen + batch.num_rows > first:
                    low = max(0, first - seen)
                    high = min(batch.num_rows, first + count - seen)
                    pieces.append((high - low, _arrays(pa, batch.slice(low, high - low), walked)))
                seen += batch.num_rows
                if seen >= first + count:
                    break
            if seen < first + count:
                raise ValueError("the row group holds fewer rows than it declares")
        except _decode_errors() as exc:
            start, end = _group_range(pf.metadata.row_group(index), footer)
            problem = finding(
                config,
                "parquet_rows_unreadable",
                bytes_at(source, start, end),
                f"rows {row} to {row + count - 1} of row group {index} do not decode"
                f" ({type(exc).__name__}); they have no record",
                {"error": type(exc).__name__, "first": row, "rows": count, "row_group": index},
            )
            return ChunkOutput(records=tuple(records), findings=(problem,))
    for height, piece in pieces:
        for offset in range(height):
            cells = [
                Unknown() if values is None else _value_cell(values[offset], absent)
                for values in piece
            ]
            records.append(row_record(cite(source, Row(row)), config, table_id, row, cells))
            row += 1
    return ChunkOutput(records=tuple(records))


def ingest(
    source: SourceReader, chunk: Chunk, config: AdapterConfig, limits: Limits
) -> ChunkOutput:
    context = chunk.context
    part = context["part"]
    if part == "none":
        return ChunkOutput()
    if part == "table":
        return _table(source, config, limits, context)
    return _rows(source, config, limits, context)


def inspect(source: SourceReader, limits: Limits) -> JsonObject:
    """What the footer declares, if it decodes: the file's shape without reading a page."""
    if source.size < TAIL or source.read(0, len(MAGIC)) != MAGIC:
        return {}
    tail = source.read(source.size - TAIL, TAIL)
    summary: dict[str, Any] = {"footer_bytes": int.from_bytes(tail[:4], "little")}
    if tail[4:] != MAGIC or summary["footer_bytes"] > min(
        limits.max_footer_bytes, source.size - TAIL - len(MAGIC)
    ):
        return summary
    try:
        metadata = _open(source, limits).metadata
    except _decode_errors():
        return summary
    summary.update(
        columns=metadata.num_columns,
        created_by=metadata.created_by or "",
        row_groups=metadata.num_row_groups,
        rows=metadata.num_rows,
    )
    return summary

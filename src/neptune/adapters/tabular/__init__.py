"""Tables as declared: CSV and TSV, JSON and JSON Lines, and Parquet (MVL-30, ADR 0042).

A source becomes one ``StructuredTable`` and one ``StructuredRecord`` per row, every cell a
``Knowledge`` state citing its exact place, so a consumer queries values and traces each to its
row and cell without parsing the file again. Nothing is inferred:

- **CSV** cells are text, exactly as written (blank is ``Unknown``). The delimiter is declared by
  ``csv_delimiter`` or sniffed by a fixed rule, the header by ``csv_header`` (a CSV cannot say
  whether its first row is one); the reading is recorded in a ``tabular.csv_dialect`` finding.
  Rows are cited ``[Row(r)]`` and their cells ``RowCell(r, c, name)``. See ``_csv``.
- **JSON** rows are an array's elements or JSON Lines' lines; their cells are the values' leaves
  in their JSON types, each citing ``[ByteRange(row), JsonPointer(path)]``. ``null`` is
  ``KnownAbsent``; a number nothing exact holds keeps its literal text. See ``_json``.
- **Parquet** cells keep the declared types; the schema, the row groups' statistics and the
  key-value metadata are tables of their own, citing the footer. See ``_parquet``.

Probing reads content, never names: Parquet's magic; a JSON array whose elements are records, or
lines that are each a JSON record (``VERIFIED``, so tabular JSON is never left to a configuration
reader); text whose records agree on a delimiter (``STRUCTURE``). A JSON object or an array of
scalars is not a table, and is declined.

Large sources stream: ``plan`` scans once for row boundaries (quote-aware for CSV, string- and
nesting-aware for JSON, the footer for Parquet) and cuts blocks between rows; each ``ingest``
reads one block. Findings about rows are aggregated per block, whose bounds depend only on the
bytes and this version's constants.
"""

from typing import TYPE_CHECKING, Final

from neptune.adapters.contract import (
    ABI_VERSION,
    NAME_ONLY,
    PROBE_HEAD_SIZE,
    SIGNATURE,
    STRUCTURE,
    VERIFIED,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    ConfigOption,
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
)
from neptune.adapters.tabular import _csv, _json, _parquet
from neptune.adapters.tabular._common import ADAPTER_ID, BOM, CODES, Layout, Limits

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject

_TABULAR_EXTENSIONS: Final = frozenset({".csv", ".tsv"})
# Names that declare YAML or TOML: delimiter agreement there is a coincidence of flow sequences
# or inline arrays, not a table, so a sniffed CSV under such a name is no claim.
_NOT_DELIMITED_EXTENSIONS: Final = (".toml", ".yaml", ".yml")
# C0 controls that text uses; any other control byte (or a NUL) means the head is not text.
_TEXT_CONTROLS: Final = frozenset(b"\t\n\x0b\x0c\r\x1b")
_BINARY: Final = bytes(b for b in range(0x20) if b not in _TEXT_CONTROLS)


def _pyarrow_version() -> str:
    from importlib.metadata import version

    return version("pyarrow")


DESCRIPTOR: Final = AdapterDescriptor(
    id=ADAPTER_ID,
    version="0.1.0",
    abi=ABI_VERSION,
    summary="CSV, TSV, JSON, JSON Lines and Parquet as tables of cells with exact citations.",
    formats=(
        FormatSpec("CSV", media_types=("text/csv",), extensions=(".csv",)),
        FormatSpec("TSV", media_types=("text/tab-separated-values",), extensions=(".tsv",)),
        FormatSpec("JSON table", media_types=("application/json",), extensions=(".json",)),
        FormatSpec(
            "JSON Lines",
            media_types=("application/jsonl", "application/x-ndjson"),
            extensions=(".jsonl", ".ndjson"),
        ),
        FormatSpec(
            "Parquet",
            media_types=("application/vnd.apache.parquet",),
            extensions=(".parquet",),
            magic=(Magic(0, _parquet.MAGIC),),
        ),
    ),
    record_kinds=("structured_record", "structured_table"),
    config=(
        ConfigOption(
            "csv_delimiter",
            "auto",
            "the CSV's delimiter as declared, or auto to sniff it from the head by a fixed rule",
            choices=("\t", ",", ";", "auto", "|"),
        ),
        ConfigOption(
            "csv_header",
            "undeclared",
            "first_row: the CSV's first record is its header; none: it has no header;"
            " undeclared: nobody says, so the header is Unknown and row 0 is a record",
            choices=("first_row", "none", "undeclared"),
        ),
        ConfigOption(
            "max_column_chunk_bytes",
            256 * 1024 * 1024,
            "a Parquet row group with a column chunk the footer declares as decoding to more"
            " bytes is not read",
        ),
        ConfigOption(
            "max_columns",
            16384,
            "a row with more cells, or a Parquet schema with more leaf columns, is not decoded",
        ),
        ConfigOption(
            "max_footer_bytes",
            16 * 1024 * 1024,
            "a Parquet footer declaring more bytes is not decoded",
        ),
        ConfigOption(
            "max_json_depth",
            64,
            "a JSON row nesting arrays and objects deeper is not decoded",
        ),
        ConfigOption(
            "max_row_bytes",
            1024 * 1024,
            "a CSV record or JSON row holding more bytes is not decoded",
        ),
        ConfigOption(
            "max_rows",
            100_000_000,
            "rows past this many are not read",
        ),
    ),
    libraries=(("pyarrow", _pyarrow_version()),),
    finding_codes=tuple(
        Documented(f"{ADAPTER_ID}.{name}", f"{text} ({category}, {severity})")
        for name, (category, severity, text) in sorted(CODES.items())
    ),
    locator_steps=(
        Documented(
            "tabular:key_value",
            "after the footer's byte range: the table of the footer's key-value metadata,"
            " columns " + ", ".join(_parquet.KEY_VALUE),
        ),
        Documented(
            "tabular:row_groups",
            "after the footer's byte range: the table of every row group's column chunks,"
            " row g * columns + c, columns " + ", ".join(_parquet.ROW_GROUPS),
        ),
        Documented(
            "tabular:schema",
            "after the footer's byte range: the table of the leaf columns the footer declares,"
            " columns " + ", ".join(_parquet.SCHEMA),
        ),
    ),
    conventions=(
        Documented(
            "blocks",
            "rows are read in blocks between rows: CSV 8,192 rows or 1 MiB, JSON 4,096 rows or"
            " 256 KiB, Parquet a slice of a row group of 65,536 cells (8,192 rows at most) or the"
            " statistics of up to 8,192 column chunks; at most 100,000 chunks, and chunks times"
            " footer bytes under 8 GiB; at most 512 slices of one row group; findings about rows"
            " count per block",
        ),
        Documented(
            "csv",
            "UTF-8 after a BOM; records end at LF outside quotes (a CR before it is the"
            " ending's), empty lines are not records; a field starting with '\"' is quoted,"
            " '\"\"' in it is '\"'; cells are text, blank or whitespace-only is Unknown;"
            " row r counts records from 0, a header included; sniffing tries comma, tab and"
            " semicolon and keeps the one whose first 64 records (at least 2) agree on the most"
            " fields",
        ),
        Documented(
            "json",
            "rows are a root array's elements or the non-blank lines of JSON Lines, cited by"
            " their bytes; cells are leaves in document order cited by JSON pointer; an empty"
            " object or array is an Unknown leaf; null is KnownAbsent citing itself; the"
            " empty string is Unknown and any other string is text; an integer"
            " is an int within int64 or uint64, another number a double when its shortest digits"
            " equal the literal, else its text; NaN and Infinity are non-finite reals; header is"
            " NotApplicable when every row is an object, Unknown otherwise",
        ),
        Documented(
            "parquet",
            "cells keep declared types; DECIMAL is exact decimal text; DATE, TIME, TIMESTAMP and"
            " durations are the stored integers (unit and zone in the schema table, never"
            " converted); null is KnownAbsent citing the footer; an empty string is Unknown, any"
            " other string text; bytes, INT96, intervals and"
            " list or map items are not decoded (Unknown); the data table's header is the leaf"
            " paths joined with '.'; statistics are the raw min and max read as the column's"
            " cells, Unknown where none fits",
        ),
        Documented(
            "probe",
            "Parquet magic: SIGNATURE (VERIFIED when the whole file is in the head and its tail"
            " checks); a JSON array of records or JSON Lines whose head rows all parse as"
            " records: VERIFIED; damaged JSON tables: STRUCTURE; CSV with a consistent delimiter"
            " over 3+ fields, or 2 fields named .csv/.tsv: STRUCTURE, else NAME_ONLY; JSON"
            " objects, arrays of scalars, empty files and binary: declined",
        ),
    ),
    resources=Resources(max_memory=512 * 1024 * 1024, streaming=True),
    security=(
        "Decodes UTF-8 only and never guesses another encoding.",
        "CSV and JSON rows are bounded by max_row_bytes, cells by max_columns and nesting by"
        " max_json_depth, checked before any row is parsed; nesting is measured without"
        " recursion.",
        "Parquet footers are bounded and their declared offsets checked against the file before"
        " pyarrow reads a page; column chunks the footer declares as decoding past"
        " max_column_chunk_bytes are not read (a footer that understates is bounded by the"
        " sandbox's memory limit).",
        "pyarrow reads single-threaded through the source reader: no file is opened, nothing is"
        " written, no thread pool is used.",
    ),
)


def _layout(source: SourceReader, limits: Limits) -> Layout:
    """How ``plan`` reads a source the registry gave it: by its bytes, as ``probe`` does.

    A head too short to tell JSON Lines from one JSON text (rows larger than half of it) is
    extended to what two rows of ``max_row_bytes`` need, so the layout never depends on how big
    the rows happen to be.
    """
    size = source.size
    window = min(size, PROBE_HEAD_SIZE)
    head = source.read(0, window)
    if head.startswith(_parquet.MAGIC):
        return Layout.PARQUET
    shape = _json.classify(head, window == size)
    if window < size and shape.undecided:
        window = min(size, 2 * limits.max_row_bytes + PROBE_HEAD_SIZE)
        shape = _json.classify(source.read(0, window), window == size)
    return shape.layout if shape.layout is not None else Layout.CSV


def _probe_parquet(head: bytes, hints: ProbeHints) -> ProbeResult:
    reasons = [ProbeReason("tabular.parquet_magic", "the head starts with Parquet's magic PAR1")]
    if len(head) == hints.size and len(head) >= 12:
        length = int.from_bytes(head[-8:-4], "little")
        if head.endswith(_parquet.MAGIC) and length <= len(head) - 12:
            reasons.append(
                ProbeReason("tabular.parquet_tail", "the tail ends with PAR1 after a footer")
            )
            return ProbeResult(VERIFIED, tuple(reasons))
    return ProbeResult(SIGNATURE, tuple(reasons))


def _probe_json(shape: _json.Shape) -> ProbeResult:
    code = f"tabular.{shape.layout}" if shape.layout else "tabular.not_json"
    reason = ProbeReason(code, shape.reason)
    if shape.layout in (Layout.JSON_ARRAY, Layout.JSON_LINES):
        if shape.rows == "records":
            return ProbeResult(VERIFIED, (reason,))
        if shape.rows in ("damaged", "unknown"):
            return ProbeResult(STRUCTURE, (reason,))
    declined = ProbeReason(
        "tabular.not_a_table", "a JSON object, an array of scalars or an empty array is not a table"
    )
    return ProbeResult(0.0, (reason, declined))


def _probe_csv(text: bytes, hints: ProbeHints, complete: bool) -> ProbeResult:
    if hints.name.lower().endswith(_NOT_DELIMITED_EXTENSIONS):
        reason = ProbeReason(
            "tabular.not_delimited_name", f"the name {hints.name!r} declares YAML or TOML"
        )
        return ProbeResult(0.0, (reason,))
    dialect = _csv.sniff(text, complete)
    if dialect is None:
        reason = ProbeReason(
            "tabular.csv_inconsistent", "no delimiter gives the records agreeing fields"
        )
        return ProbeResult(0.0, (reason,))
    shown = {"\t": "tab", ",": "comma", ";": "semicolon"}[dialect.delimiter]
    reason = ProbeReason(
        "tabular.csv",
        f"{dialect.records} records agree on {dialect.fields} {shown}-delimited fields",
    )
    named = any(hints.name.lower().endswith(ext) for ext in _TABULAR_EXTENSIONS)
    if dialect.fields >= 3 or named:
        return ProbeResult(STRUCTURE, (reason,))
    weak = ProbeReason(
        "tabular.csv_two_fields",
        "two fields a line could be prose or a log; not named .csv or .tsv",
    )
    return ProbeResult(NAME_ONLY, (reason, weak))


class TabularAdapter:
    """CSV, TSV, JSON, JSON Lines and Parquet tables. Blocks are fixed by the version."""

    descriptor = DESCRIPTOR

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        if not head:
            return ProbeResult(
                0.0, (ProbeReason("tabular.empty", "an empty source holds no table"),)
            )
        if head.startswith(_parquet.MAGIC):
            return _probe_parquet(head, hints)
        text = head[len(BOM) :] if head.startswith(BOM) else head
        if b"\x00" in text or len(text.translate(None, _BINARY)) != len(text):
            return ProbeResult(0.0, (ProbeReason("tabular.binary", "the head is not text"),))
        complete = len(head) == hints.size
        shape = _json.classify(head, complete)
        if shape.layout is not None:
            return _probe_json(shape)
        return _probe_csv(text, hints, complete)

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        head = source.read(0, min(source.size, PROBE_HEAD_SIZE))
        layout = _layout(source, Limits.of(config))
        summary: JsonObject = {"layout": layout.value, "size": source.size}
        if layout is Layout.PARQUET:
            summary = {**summary, **_parquet.inspect(source, Limits.of(config))}
        elif layout is Layout.CSV:
            text = head[len(BOM) :] if head.startswith(BOM) else head
            dialect = _csv.sniff(text, len(head) == source.size)
            summary = {
                **summary,
                "bom": head.startswith(BOM),
                "delimiter": dialect.delimiter if dialect else "",
                "fields": dialect.fields if dialect else 0,
            }
        else:
            summary = {**summary, "bom": head.startswith(BOM)}
        return InspectResult(summary)

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        limits = Limits.of(config)
        layout = _layout(source, limits)
        if layout is Layout.PARQUET:
            return _parquet.plan(source, config, limits)
        if layout is Layout.CSV:
            return _csv.plan(source, config, limits)
        return _json.plan(source, config, limits, layout)

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        layout = Layout(str(chunk.context["layout"]))
        limits = Limits.of(config)
        if layout is Layout.PARQUET:
            return _parquet.ingest(source, chunk, config, limits)
        if layout is Layout.CSV:
            return _csv.ingest(source, chunk, config, limits)
        return _json.ingest(source, chunk, config, limits)


__all__ = ["DESCRIPTOR", "TabularAdapter"]

"""CSV and TSV: records found by one quote-aware scan, cells kept as the text they are.

The grammar (RFC 4180, read leniently and the same way in every pass):

- Records end at an LF outside quotes; a CR directly before it belongs to the ending. A line that
  holds nothing (or only that CR) is not a record and is not counted. The last record may end
  without an LF.
- Fields are separated by the delimiter. A field that starts with ``"`` is quoted: it runs to the
  next ``"`` not doubled, ``""`` inside it is one ``"``, and delimiters and line breaks inside it
  are text. Text after the closing quote is kept after the quoted text and reported
  (``csv_malformed_quote``). A ``"`` anywhere else is text.
- Bytes are decoded as UTF-8 after a leading byte-order mark, cell by cell; another encoding is
  never guessed.

The dialect is the delimiter only: ``csv_delimiter`` declares it, or it is sniffed from the head
by a fixed rule (``sniff``). The quote is always ``"``. ``csv_header`` says whether the first
record is a header; a CSV cannot say so itself.
"""

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import (
    PROBE_HEAD_SIZE,
    AdapterConfig,
    Chunk,
    ChunkOutput,
    Plan,
    SourceReader,
    make_chunk,
    read_pieces,
)
from neptune.adapters.tabular._common import (
    BOM,
    Issues,
    Layout,
    Limits,
    bytes_at,
    cite,
    context_flag,
    context_int,
    context_text,
    finding,
    observed,
    record_id,
    row_record,
    table,
    whole,
)
from neptune.model.finding import IngestFinding
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import Knowledge, Known, NotApplicable, NotCovered, Unknown
from neptune.model.provenance import Row
from neptune.model.world import CellValue, StructuredRecord, StructuredTable

QUOTE: Final = 0x22
LF: Final = 0x0A
CR: Final = 0x0D
# The delimiters sniffing tries, in this order: a tie on fields goes to the earlier one. A pipe is
# never sniffed (Markdown tables use it); declare it with ``csv_delimiter``.
SNIFFED: Final = (",", "\t", ";")
SNIFF_RECORDS: Final = 64
# A block: at most this many rows and about this many bytes (a longer row is a block of its own).
BLOCK_ROWS: Final = 8192
BLOCK_BYTES: Final = 1024 * 1024

# Scanner states: at a field's start, in an unquoted field, in a quoted one, just after a quote
# inside a quoted field (a doubled quote, or the closing one).
_START, _UNQUOTED, _QUOTED, _AFTER_QUOTE = range(4)


@dataclass(frozen=True)
class Record:
    """One record: bytes ``[start, end)`` with its ending, ``[start, stop)`` without it."""

    start: int
    stop: int
    end: int
    fields: int
    crlf: bool
    unterminated: bool  # the source ends inside a quoted field

    @property
    def size(self) -> int:
        return self.stop - self.start


class Scanner:
    """Finds records in consecutive pieces of bytes, carrying its state between pieces.

    Only quotes, delimiters and line feeds move it, so a piece without quotes is cut at its line
    feeds directly. Memory is bounded by the piece, whatever a record's length.
    """

    def __init__(self, delimiter: bytes, offset: int) -> None:
        self._delimiter = delimiter[0]
        self._stop_unquoted = re.compile(b"[" + re.escape(delimiter) + b"\n]")
        self._state = _START
        self._start = offset  # where the current record starts
        self._offset = offset  # where the next piece starts
        self._fields = 1
        self._previous = -1  # the byte before the next piece's first one

    def feed(self, piece: bytes) -> Iterator[Record]:
        base, n = self._offset, len(piece)
        if self._state != _QUOTED and b'"' not in piece:
            yield from self._lines(piece)
        else:
            i = 0
            while i < n:
                state = self._state
                if state == _QUOTED:
                    j = piece.find(b'"', i)
                    if j < 0:
                        break
                    self._state, i = _AFTER_QUOTE, j + 1
                    continue
                if state != _UNQUOTED:
                    byte = piece[i]
                    if byte == QUOTE:
                        self._state = _QUOTED
                    elif byte == self._delimiter:
                        self._fields += 1
                        self._state = _START
                    elif byte == LF:
                        yield from self._end(base + i, piece[i - 1] if i else self._previous)
                    else:
                        self._state = _UNQUOTED
                    i += 1
                    continue
                found = self._stop_unquoted.search(piece, i)
                if found is None:
                    break
                j = found.start()
                if piece[j] == LF:
                    yield from self._end(base + j, piece[j - 1] if j else self._previous)
                else:
                    self._fields += 1
                    self._state = _START
                i = j + 1
        if n:
            self._previous = piece[-1]
        self._offset = base + n

    def _lines(self, piece: bytes) -> Iterator[Record]:
        base, i = self._offset, 0
        while True:
            j = piece.find(b"\n", i)
            if j < 0:
                rest = piece[i:]
                if rest:
                    self._fields += rest.count(self._delimiter)
                    # A quote right after a delimiter would open a quoted field; anywhere else
                    # it is text.
                    self._state = _START if rest[-1] == self._delimiter else _UNQUOTED
                return
            self._fields += piece.count(self._delimiter, i, j)
            yield from self._end(base + j, piece[j - 1] if j else self._previous)
            i = j + 1

    def _end(self, at: int, before: int) -> Iterator[Record]:
        """The LF at offset ``at`` ends the current record."""
        crlf = before == CR and at > self._start
        stop = at - 1 if crlf else at
        if stop > self._start:
            yield Record(self._start, stop, at + 1, self._fields, crlf, unterminated=False)
        self._start, self._fields, self._state = at + 1, 1, _START

    def finish(self) -> Iterator[Record]:
        """The record the source ends with, if it has no LF."""
        if self._offset > self._start:
            yield Record(
                self._start,
                self._offset,
                self._offset,
                self._fields,
                crlf=False,
                unterminated=self._state == _QUOTED,
            )


def records(pieces: Iterable[bytes], delimiter: bytes, offset: int) -> Iterator[Record]:
    scanner = Scanner(delimiter, offset)
    for piece in pieces:
        yield from scanner.feed(piece)
    yield from scanner.finish()


def split(content: bytes, delimiter: bytes) -> tuple[list[bytes], bool]:
    """A record's fields (its ending removed), and whether text follows a closing quote."""
    if b'"' not in content:
        return content.split(delimiter), False
    fields: list[bytes] = []
    i, n, malformed = 0, len(content), False
    while True:
        if i < n and content[i] == QUOTE:
            held = bytearray()
            i += 1
            while True:
                j = content.find(b'"', i)
                if j < 0:  # unterminated: the field runs to the end
                    held += content[i:]
                    fields.append(bytes(held))
                    return fields, malformed
                held += content[i:j]
                if j + 1 < n and content[j + 1] == QUOTE:
                    held += b'"'
                    i = j + 2
                    continue
                i = j + 1
                break
            k = content.find(delimiter, i)
            stop = n if k < 0 else k
            if stop > i:
                malformed = True
                held += content[i:stop]
            fields.append(bytes(held))
        else:
            k = content.find(delimiter, i)
            fields.append(content[i:] if k < 0 else content[i:k])
        if k < 0:
            return fields, malformed
        i = k + 1


@dataclass(frozen=True)
class Dialect:
    delimiter: str
    fields: int
    records: int


def sniff(sample: bytes, complete: bool) -> Dialect | None:
    """The delimiter the sample's records agree on, by a fixed rule, or ``None``.

    A delimiter qualifies when the sample's first ``SNIFF_RECORDS`` records (at least two; a
    record cut off by the end of an incomplete sample is left out) all hold the same number of
    fields, at least two. Of those that qualify, the one giving the most fields wins, then the
    earlier in ``SNIFFED``.
    """
    best: Dialect | None = None
    for delimiter in SNIFFED:
        found = []
        for record in records((sample,), delimiter.encode(), 0):
            if record.end == len(sample) and not complete and not sample.endswith(b"\n"):
                break  # the sample stops inside this record
            if record.unterminated:
                break
            found.append(record.fields)
            if len(found) == SNIFF_RECORDS:
                break
        if len(found) >= 2 and found[0] >= 2 and len(set(found)) == 1:
            if best is None or found[0] > best.fields:
                best = Dialect(delimiter, found[0], len(found))
    return best


def _text(field: bytes) -> str | None:
    try:
        return field.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _cell(field: bytes) -> tuple[Knowledge[CellValue], bool]:
    """A cell and whether its bytes are UTF-8. Blank and whitespace-only cells are Unknown."""
    text = _text(field)
    if text is None:
        return Unknown(), False
    if not text.strip():
        return Unknown(), True
    return Known(text), True


def plan(source: SourceReader, config: AdapterConfig, limits: Limits) -> Plan:
    head = source.read(0, min(source.size, PROBE_HEAD_SIZE))
    bom = head.startswith(BOM)
    begin = len(BOM) if bom else 0
    declared = config.text("csv_delimiter")
    if declared != "auto":
        delimiter, rule = declared, "configured"
    else:
        dialect = sniff(head[begin:], len(head) == source.size)
        delimiter, rule = (dialect.delimiter, "sniffed") if dialect else (",", "default")
    with_header = config.text("csv_header") == "first_row"
    findings: list[IngestFinding] = []
    chunks: list[Chunk] = []
    endings = {"crlf": 0, "lf": 0}
    header = (-1, -1)
    expected = -1
    held: list[Record] = []
    first_row = 0

    def close() -> None:
        if held:
            context: JsonObject = {
                "delimiter": delimiter,
                "end": held[-1].end,
                "expected": expected,
                "layout": Layout.CSV.value,
                "part": "rows",
                "row": first_row,
                "start": held[0].start,
            }
            chunks.append(make_chunk(source, config, context, held[-1].end - held[0].start))
            held.clear()

    row = 0
    pieces = read_pieces(source, begin, source.size)
    for record in records(pieces, delimiter.encode(), begin):
        if record.end > record.stop:
            endings["crlf" if record.crlf else "lf"] += 1
        if row >= limits.max_rows:
            close()
            findings.append(
                finding(
                    config,
                    "row_limit",
                    bytes_at(source, record.start, source.size),
                    f"the table holds more than max_rows ({limits.max_rows}) rows; rows from"
                    f" {row} on are not read",
                    {"max_rows": limits.max_rows, "row": row},
                )
            )
            break
        if row == 0:
            expected = record.fields if record.size <= limits.max_row_bytes else -1
        if record.unterminated:
            at = cite(source, Row(row))
            emitted = record.size <= limits.max_row_bytes and not (with_header and row == 0)
            findings.append(
                finding(
                    config,
                    "csv_unterminated_quote",
                    bytes_at(source, record.start, record.end),
                    f"row {row} ends inside a quoted field: its last cell runs to the end of"
                    " the file",
                    {"row": row},
                    (record_id(StructuredRecord.kind, at, config),) if emitted else (),
                )
            )
        if record.size > limits.max_row_bytes:
            close()
            findings.append(
                finding(
                    config,
                    "row_too_large",
                    bytes_at(source, record.start, record.end),
                    f"row {row} holds {record.size} bytes, over max_row_bytes"
                    f" ({limits.max_row_bytes}); it is not decoded",
                    {"bytes": record.size, "max_row_bytes": limits.max_row_bytes, "row": row},
                )
            )
        elif with_header and row == 0:
            header = (record.start, record.stop)
        else:
            if held and (len(held) >= BLOCK_ROWS or record.end - held[0].start > BLOCK_BYTES):
                close()
            if not held:
                first_row = row
            held.append(record)
        row += 1
    close()
    endings_json: dict[str, JsonValue] = dict(endings)
    context: JsonObject = {
        "bom": bom,
        "delimiter": delimiter,
        "delimiter_rule": rule,
        "header_start": header[0],
        "header_stop": header[1],
        "layout": Layout.CSV.value,
        "line_endings": endings_json,
        "part": "table",
    }
    table_chunk = make_chunk(source, config, context, max(0, header[1] - header[0]))
    return Plan((table_chunk, *chunks), tuple(findings))


def _table(
    source: SourceReader, chunk: Chunk, config: AdapterConfig, limits: Limits
) -> ChunkOutput:
    context = chunk.context
    delimiter = context_text(context, "delimiter")
    mode = config.text("csv_header")
    issues = Issues(source, config)
    header: Knowledge[tuple[str, ...]] = NotApplicable() if mode == "none" else Unknown()
    start, stop = context_int(context, "header_start"), context_int(context, "header_stop")
    if mode == "first_row" and start >= 0:
        fields, _ = split(source.read(start, stop - start), delimiter.encode())
        names = [_text(field) for field in fields]
        if len(fields) > limits.max_columns:
            issues.add(
                "too_many_columns",
                start,
                stop,
                0,
                f"the header row holds {len(fields)} fields, over max_columns"
                f" ({limits.max_columns}); the header is Unknown",
                details={"max_columns": limits.max_columns},
            )
        elif any(name is None for name in names):
            issues.add("invalid_utf8", start, stop, 0, "the header row is not UTF-8; it is Unknown")
        else:
            texts = tuple(name for name in names if name is not None)
            header = Known(texts, observed(cite(source, Row(0)), config))
    endings = context["line_endings"]
    record = table(whole(source), config, NotCovered(), header)
    bom = context_flag(context, "bom")
    rule = context_text(context, "delimiter_rule")
    shown_delimiter = {"\t": "tab", ",": "comma", ";": "semicolon", "|": "pipe"}[delimiter]
    message = (
        f"a CSV declares no dialect: read as UTF-8{' after a BOM' if bom else ''}, delimited by"
        f" {shown_delimiter} ({rule}), quoted with '\"', header {mode}"
    )
    dialect = finding(
        config,
        "csv_dialect",
        whole(source),
        message,
        {
            "bom": bom,
            "delimiter": delimiter,
            "delimiter_rule": rule,
            "encoding": "utf-8",
            "header": mode,
            "line_endings": endings,
            "quote": '"',
        },
        (record.id,),
    )
    return ChunkOutput(records=(record,), findings=(dialect, *issues.findings()))


def _rows(source: SourceReader, chunk: Chunk, config: AdapterConfig, limits: Limits) -> ChunkOutput:
    context = chunk.context
    delimiter = context_text(context, "delimiter").encode()
    start, end = context_int(context, "start"), context_int(context, "end")
    row, expected = context_int(context, "row"), context_int(context, "expected")
    table_id = record_id(StructuredTable.kind, whole(source), config)
    data = source.read(start, end - start)
    issues = Issues(source, config)
    out: list[StructuredRecord] = []
    for record in records((data,), delimiter, start):
        content = data[record.start - start : record.stop - start]
        fields, malformed = split(content, delimiter)
        at = cite(source, Row(row))
        rid = record_id(StructuredRecord.kind, at, config)
        if len(fields) > limits.max_columns:
            issues.add(
                "too_many_columns",
                record.start,
                record.end,
                row,
                f"row {row} holds {len(fields)} fields, over max_columns ({limits.max_columns});"
                " it is not decoded",
                details={"max_columns": limits.max_columns},
            )
            row += 1
            continue
        cells: list[Knowledge[CellValue]] = []
        bad: list[int] = []
        for column, field in enumerate(fields):
            cell, utf8 = _cell(field)
            cells.append(cell)
            if not utf8:
                bad.append(column)
        if bad:
            issues.add(
                "invalid_utf8",
                record.start,
                record.end,
                row,
                f"row {row}'s cells {bad[:10]} are not UTF-8; they are Unknown",
                record=rid,
            )
        if malformed:
            issues.add(
                "csv_malformed_quote",
                record.start,
                record.end,
                row,
                f"row {row} has text after a closing quote; the cell keeps it",
                record=rid,
            )
        if expected >= 0 and len(fields) != expected:
            issues.add(
                "csv_ragged_rows",
                record.start,
                record.end,
                row,
                f"row {row} holds {len(fields)} fields, the first row {expected}",
                details={"expected": expected},
                record=rid,
            )
        out.append(row_record(at, config, table_id, row, cells))
        row += 1
    return ChunkOutput(records=tuple(out), findings=tuple(issues.findings()))


def ingest(
    source: SourceReader, chunk: Chunk, config: AdapterConfig, limits: Limits
) -> ChunkOutput:
    if chunk.context["part"] == "table":
        return _table(source, chunk, config, limits)
    return _rows(source, chunk, config, limits)

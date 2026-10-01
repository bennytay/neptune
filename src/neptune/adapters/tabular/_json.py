"""JSON and JSON Lines: rows as their JSON values, cells as their leaves, each cited by pointer.

- A JSON text whose root is an array is a table whose rows are the array's elements; JSON Lines
  is a table whose rows are its non-blank lines (RFC 8259 values, one per line). A JSON text with
  another root, read when a manifest asks for it, is a table of one row.
- A row's record cites its value's bytes, ``[ByteRange(value)]``. Its cells are the value's
  leaves in document order, each citing ``[ByteRange(value), JsonPointer(path)]``: an object's
  members by key, an array's items by index, a scalar row at ``""``. An empty object or array
  is a blank leaf: ``Unknown`` at its pointer.
- Values keep JSON's types: strings are text (blank or whitespace-only text is ``Unknown``),
  ``true`` and ``false`` booleans, ``null`` is ``KnownAbsent`` citing the ``null`` itself, which
  the grammar defines. An integer literal is an int when an int64 or a uint64 holds it; another
  number is a double when the double's shortest digits equal the literal's value. Any other
  number keeps its literal text (``json_number_text``). ``NaN``, ``Infinity`` and ``-Infinity``,
  which RFC 8259 lacks and Python writes, are the non-finite reals they spell.
- A key an object repeats has no reading: one ``Unknown`` cell at its pointer
  (``json_duplicate_key``).

Large texts stream: planning scans the bytes once for row boundaries (strings, escapes and
nesting tracked, nothing decoded) and cuts blocks between rows.
"""

import json
import math
import re
from collections.abc import Generator, Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
from neptune.model.jsonvalue import JsonObject
from neptune.model.knowledge import (
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import ByteRange, EvidenceRef, JsonPointer, Provenance
from neptune.model.scalars import NonFinite
from neptune.model.world import CellValue, StructuredRecord, StructuredTable

WHITESPACE: Final = b" \t\n\r"
BLOCK_ROWS: Final = 4096
BLOCK_BYTES: Final = 256 * 1024
# What probing parses of a head's rows at most, and the nesting it allows them.
PROBE_ROWS: Final = 64
PROBE_DEPTH: Final = 64
INT_MIN: Final = -(2**63)
INT_LIMIT: Final = 2**64  # uint64 values are ints too

QUOTE: Final = 0x22
CR: Final = 0x0D
BACKSLASH: Final = 0x5C
FOLLOWS: Final = "text follows the root array"

_IN_STRING: Final = re.compile(rb'["\\]')
_IN_CONTAINER: Final = re.compile(rb'["\[\]{}]')
_SCALAR_END: Final = re.compile(rb"[ \t\n\r,\]]")


@dataclass(frozen=True)
class Element:
    """One row's value: bytes ``[start, end)``, beginning with ``first``."""

    start: int
    end: int
    first: int


@dataclass(frozen=True)
class Broken:
    """The text stops being an array of values at ``offset``: ``reason`` says how."""

    offset: int
    reason: str


# Array scanner states.
_BEFORE, _FIRST, _VALUE, _AFTER, _COMMA, _DONE = range(6)


class ArrayScanner:
    """Finds the elements of a root array in consecutive pieces, without decoding them.

    It tracks strings, escapes and nesting depth only; an element's own grammar is checked when
    its row is decoded. Memory is bounded by the piece, however deep or long an element is.
    """

    def __init__(self, offset: int, *, inside: bool = False) -> None:
        self._state = _COMMA if inside else _BEFORE
        self._offset = offset
        self._start = 0
        self._first = 0
        self._depth = 0
        self._string = False
        self._escape = False
        self._scalar = False
        self.broken: Broken | None = None

    def feed(self, piece: bytes) -> Iterator[Element]:
        base, n, i = self._offset, len(piece), 0
        while i < n and self.broken is None:
            state = self._state
            if state == _VALUE:
                i = yield from self._value(piece, i, base)
                continue
            byte = piece[i]
            if byte in WHITESPACE:
                i += 1
                continue
            if state == _BEFORE:
                if byte == ord("["):
                    self._state = _FIRST
                else:
                    self.broken = Broken(base + i, "the text is not an array")
            elif state in (_FIRST, _COMMA):
                if byte == ord("]") and state == _FIRST:
                    self._state = _DONE
                elif byte in b",]":
                    self.broken = Broken(base + i, f"a value is missing before {chr(byte)!r}")
                else:
                    self._begin(byte, base + i)
                    continue  # the value's first byte is read as part of it
            elif state == _AFTER:
                if byte == ord(","):
                    self._state = _COMMA
                elif byte == ord("]"):
                    self._state = _DONE
                else:
                    self.broken = Broken(base + i, "a ',' or ']' is missing after a value")
            else:  # _DONE
                self.broken = Broken(base + i, FOLLOWS)
            i += 1
        self._offset = base + n

    def _begin(self, byte: int, at: int) -> None:
        self._state, self._start, self._first = _VALUE, at, byte
        self._depth, self._string, self._escape = 0, False, False
        self._scalar = byte not in b'[{"'

    def _value(self, piece: bytes, i: int, base: int) -> Generator[Element, None, int]:
        """Advance inside the current value; yield it once it ends. Returns the next index."""
        n = len(piece)
        if self._scalar:
            found = _SCALAR_END.search(piece, i)
            if found is None:
                return n
            yield self._close(base + found.start())
            return found.start()
        while i < n:
            if self._escape:
                self._escape, i = False, i + 1
                continue
            if self._string:
                found = _IN_STRING.search(piece, i)
                if found is None:
                    return n
                i = found.start() + 1
                if piece[found.start()] == ord("\\"):
                    self._escape = True
                else:
                    self._string = False
                    if self._depth == 0:
                        yield self._close(base + i)
                        return i
                continue
            found = _IN_CONTAINER.search(piece, i)
            if found is None:
                return n
            byte, i = piece[found.start()], found.start() + 1
            if byte == ord('"'):
                self._string = True
            elif byte in b"[{":
                self._depth += 1
            else:
                self._depth -= 1
                if self._depth <= 0:
                    yield self._close(base + i)
                    return i
        return n

    def _close(self, end: int) -> Element:
        self._state = _AFTER
        return Element(self._start, end, self._first)

    def flush(self) -> Iterator[Element]:
        """The scalar a block of elements ends with (a block ends where its last value does)."""
        if self._state == _VALUE and self._scalar and self.broken is None:
            yield self._close(self._offset)

    def finish(self) -> Iterator[Element]:
        """The scalar the text ends with, if any; a text that stops early is ``broken``."""
        if self.broken is not None:
            return
        if self._state == _VALUE and self._scalar:
            yield self._close(self._offset)
        if self._state != _DONE:
            what = "begins" if self._state == _BEFORE else "closes"
            self.broken = Broken(self._offset, f"the text ends before its root array {what}")


def array_elements(scanner: ArrayScanner, pieces: Iterable[bytes]) -> Iterator[Element]:
    for piece in pieces:
        yield from scanner.feed(piece)
        if scanner.broken is not None:
            return
    yield from scanner.finish()


def lines(pieces: Iterable[bytes], offset: int) -> Iterator[Element]:
    """The non-blank lines of consecutive bytes from ``offset``; each ends before its LF and a
    CR directly before it. Memory is bounded by the piece, whatever a line's length."""
    start, first, last, position = offset, -1, -1, offset
    for piece in pieces:
        i, n = 0, len(piece)
        while True:
            j = piece.find(b"\n", i)
            segment = piece[i : n if j < 0 else j]
            if segment:
                if first < 0:
                    content = segment.lstrip(WHITESPACE)
                    if content:
                        first = content[0]
                last = segment[-1]
            if j < 0:
                break
            end = position + j
            if first >= 0:
                yield Element(start, end - 1 if last == CR else end, first)
            start, first, last, i = end + 1, -1, -1, j + 1
        position += n
    if first >= 0:
        yield Element(start, position - 1 if last == CR else position, first)


def deeper_than(data: bytes, limit: int) -> bool:
    """Whether a JSON text nests arrays and objects more than ``limit`` deep. Linear, decodes
    nothing and never recurses, so a hostile depth costs no stack."""
    depth, i, n, string = 0, 0, len(data), False
    while i < n:
        if string:
            found = _IN_STRING.search(data, i)
            if found is None:
                return False
            j = found.start()
            if data[j] == BACKSLASH:
                i = j + 2
                continue
            string, i = False, j + 1
            continue
        found = _IN_CONTAINER.search(data, i)
        if found is None:
            return False
        j = found.start()
        byte, i = data[j], j + 1
        if byte == QUOTE:
            string = True
        elif byte in b"[{":
            depth += 1
            if depth > limit:
                return True
        else:
            depth -= 1
    return False


@dataclass(frozen=True, slots=True)
class _Number:
    """A number as its literal: the parser keeps the text, so nothing is rounded unseen."""

    text: str
    integral: bool


class _Members(list[tuple[str, object]]):
    """An object's members in document order, a repeated key kept."""


_BLANK: Final = object()  # an empty object or array: a leaf holding nothing
_CONSTANTS: Final = {
    "NaN": NonFinite.NAN,
    "Infinity": NonFinite.POSITIVE_INFINITY,
    "-Infinity": NonFinite.NEGATIVE_INFINITY,
}


def _integer(text: str) -> _Number:
    return _Number(text, True)


def _real(text: str) -> _Number:
    return _Number(text, False)


def loads(text: str) -> object:
    """A JSON text as nested ``_Members``, lists and leaves; ``ValueError`` if it is invalid."""
    return json.loads(
        text,
        object_pairs_hook=_Members,
        parse_int=_integer,
        parse_float=_real,
        parse_constant=_CONSTANTS.__getitem__,
    )


def number(value: _Number) -> tuple[CellValue, bool]:
    """A number's cell value, and whether it kept its type: an int an int64 or a uint64 holds,
    a double whose shortest digits are the literal's value, or else the literal's text."""
    text = value.text
    if value.integral:
        if len(text) <= 21:
            as_int = int(text)
            if INT_MIN <= as_int < INT_LIMIT:
                return as_int, True
        return text, False
    as_float = float(text)
    if math.isfinite(as_float):
        try:
            if Decimal(repr(as_float)) == Decimal(text):
                return as_float, True
        except InvalidOperation:
            pass
    return text, False


class TooManyLeaves(Exception):
    """A row has more leaves than ``max_columns``."""


def leaves(value: object, limit: int) -> tuple[list[tuple[str, object]], list[str]]:
    """A row's leaves in document order as ``(pointer, value)``, and the pointers of the keys an
    object repeats (each one blank leaf). Raises ``TooManyLeaves`` past ``limit`` leaves."""
    out: list[tuple[str, object]] = []
    repeated: list[str] = []

    def walk(item: object, pointer: str) -> None:
        if isinstance(item, _Members):
            counts: dict[str, int] = {}
            for key, _ in item:
                counts[key] = counts.get(key, 0) + 1
            for key, child in item:
                if counts[key] == 0:
                    continue  # a repeated key, already given its blank leaf
                at = pointer + "/" + key.replace("~", "~0").replace("/", "~1")
                if counts[key] > 1:
                    repeated.append(at)
                    out.append((at, _BLANK))
                    counts[key] = 0
                else:
                    walk(child, at)
            if not item:
                out.append((pointer, _BLANK))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                walk(child, f"{pointer}/{index}")
            if not item:
                out.append((pointer, _BLANK))
        else:
            out.append((pointer, item))
        if len(out) > limit:
            raise TooManyLeaves

    walk(value, "")
    return out, repeated


# --- Telling a JSON table from other JSON ------------------------------------------------------


@dataclass(frozen=True)
class Shape:
    """What a head shows of a JSON text: its layout (``None``: not JSON), and its rows.

    ``rows`` is ``records`` when every row the head holds whole parses and every one is an
    object (or every one an array); ``scalars`` or ``empty`` when they are not records;
    ``unknown`` when the head holds no whole row; ``damaged`` when a row or the structure between
    rows is broken.
    """

    layout: Layout | None
    rows: str
    reason: str


def _parsed(data: bytes) -> object | None:
    if deeper_than(data, PROBE_DEPTH):
        return None
    try:
        return loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return None


def _rows_shape(values: list[object | None]) -> str:
    if any(value is None for value in values):
        return "damaged"
    if all(isinstance(v, _Members) for v in values) or all(type(v) is list for v in values):
        return "records"
    return "scalars"


def _line_shape(text: bytes, complete: bool) -> Shape | None:
    found = list(lines((text,), 0))
    if found and not complete and not text.endswith(b"\n"):
        found.pop()  # the head stops inside this line
    if len(found) < 2:
        return None
    values = [_parsed(text[e.start : e.end]) for e in found[:PROBE_ROWS]]
    if not isinstance(values[0], list):  # _Members is a list too
        return None
    shape = _rows_shape(values)
    reason = f"{len(values)} lines of the head are JSON texts, one per line"
    return Shape(Layout.JSON_LINES, "damaged" if shape == "scalars" else shape, reason)


def classify(head: bytes, complete: bool) -> Shape:
    """The JSON layout ``head`` shows: the first bytes of a source, all of it if ``complete``."""
    text = head[len(BOM) :] if head.startswith(BOM) else head
    body = text.lstrip(WHITESPACE)
    if not body or body[0] not in b"[{":
        return Shape(None, "unknown", "the text does not start with '[' or '{'")
    if body[0] == ord("{"):
        found = _line_shape(text, complete)
        if found is not None:
            return found
        return Shape(Layout.JSON_DOCUMENT, "unknown", "the root is an object")
    scanner = ArrayScanner(0)
    elements = list(scanner.feed(text))
    if complete:
        elements += list(scanner.finish())
    broken = scanner.broken
    if broken is not None and broken.reason == FOLLOWS:
        found = _line_shape(text, complete)
        if found is not None:
            return found
    if broken is not None:
        return Shape(Layout.JSON_ARRAY, "damaged", f"byte {broken.offset}: {broken.reason}")
    if not elements:
        if complete:
            return Shape(Layout.JSON_ARRAY, "empty", "the root is an empty array")
        return Shape(Layout.JSON_ARRAY, "unknown", "the head holds no whole element")
    values = [_parsed(text[e.start : e.end]) for e in elements[:PROBE_ROWS]]
    reason = f"the root is an array; {len(values)} elements of the head parse"
    return Shape(Layout.JSON_ARRAY, _rows_shape(values), reason)


# --- Planning and reading ------------------------------------------------------------------------


def _document(source: SourceReader, begin: int) -> Element | None:
    """The one value of a text read as a document: its bytes without surrounding whitespace."""
    start = begin
    for piece in read_pieces(source, begin, source.size):
        content = piece.lstrip(WHITESPACE)
        if content:
            start += len(piece) - len(content)
            break
        start += len(piece)
    else:
        return None
    end = source.size
    while end > start:
        window = source.read(max(start, end - PROBE_HEAD_SIZE), min(PROBE_HEAD_SIZE, end - start))
        content = window.rstrip(WHITESPACE)
        end -= len(window) - len(content)
        if content:
            break
    first = source.read(start, 1)
    return Element(start, end, first[0])


def plan(source: SourceReader, config: AdapterConfig, limits: Limits, layout: Layout) -> Plan:
    head = source.read(0, min(source.size, len(BOM)))
    bom = head == BOM
    begin = len(BOM) if bom else 0
    findings: list[IngestFinding] = []
    chunks: list[Chunk] = []
    held: list[Element] = []
    records_only = True
    first_row = 0

    def close() -> None:
        if held:
            context: JsonObject = {
                "end": held[-1].end,
                "layout": layout.value,
                "part": "rows",
                "row": first_row,
                "start": held[0].start,
            }
            chunks.append(make_chunk(source, config, context, held[-1].end - held[0].start))
            held.clear()

    scanner: ArrayScanner | None = None
    elements: Iterable[Element]
    if layout is Layout.JSON_ARRAY:
        scanner = ArrayScanner(begin)
        elements = array_elements(scanner, read_pieces(source, begin, source.size))
    elif layout is Layout.JSON_LINES:
        elements = lines(read_pieces(source, begin, source.size), begin)
    else:
        found = _document(source, begin)
        elements = () if found is None else (found,)
    row, last_end = 0, begin
    for element in elements:
        if row >= limits.max_rows:
            findings.append(
                finding(
                    config,
                    "row_limit",
                    bytes_at(source, element.start, source.size),
                    f"the table holds more than max_rows ({limits.max_rows}) rows; rows from"
                    f" {row} on are not read",
                    {"max_rows": limits.max_rows, "row": row},
                )
            )
            scanner = None  # the rest is not read, so its structure is not judged
            break
        records_only = records_only and element.first == ord("{")
        size = element.end - element.start
        last_end = element.end
        if size > limits.max_row_bytes:
            close()
            findings.append(
                finding(
                    config,
                    "row_too_large",
                    bytes_at(source, element.start, element.end),
                    f"row {row} holds {size} bytes, over max_row_bytes ({limits.max_row_bytes});"
                    " it is not decoded",
                    {"bytes": size, "max_row_bytes": limits.max_row_bytes, "row": row},
                )
            )
        else:
            if held and (len(held) >= BLOCK_ROWS or element.end - held[0].start > BLOCK_BYTES):
                close()
            if not held:
                first_row = row
            held.append(element)
        row += 1
    close()
    if scanner is not None and scanner.broken is not None:
        broken = scanner.broken
        start = min(last_end, broken.offset)
        findings.append(
            finding(
                config,
                "json_structure",
                bytes_at(source, start, source.size),
                f"at byte {broken.offset} {broken.reason}; nothing after"
                + (f" row {row - 1}" if row else " it")
                + " is read",
                {"offset": broken.offset, "rows": row},
            )
        )
    context: JsonObject = {
        "bom": bom,
        "header": "not_applicable" if records_only else "unknown",
        "layout": layout.value,
        "part": "table",
    }
    return Plan((make_chunk(source, config, context, 0), *chunks), tuple(findings))


def _table(source: SourceReader, config: AdapterConfig, context: JsonObject) -> ChunkOutput:
    header: Knowledge[tuple[str, ...]] = (
        NotApplicable() if context_text(context, "header") == "not_applicable" else Unknown()
    )
    record = table(whole(source), config, NotCovered(), header)
    found: list[IngestFinding] = []
    if context_flag(context, "bom"):
        found.append(
            finding(
                config,
                "json_bom",
                bytes_at(source, 0, len(BOM)),
                "the JSON text starts with a UTF-8 byte-order mark, which RFC 8259 forbids;"
                " it is skipped",
                {},
                (record.id,),
            )
        )
    return ChunkOutput(records=(record,), findings=tuple(found))


def _cell(value: object, provenance: Provenance, issues: "_RowIssues") -> Knowledge[CellValue]:
    if value is _BLANK:
        return Unknown(provenance)
    if value is None:
        return KnownAbsent(provenance)
    if isinstance(value, bool | NonFinite):
        return Known(value, provenance)
    if isinstance(value, _Number):
        cell, kept = number(value)
        if not kept:
            issues.text_numbers += 1
        return Known(cell, provenance)
    if isinstance(value, str):
        if not value.strip():
            return Unknown(provenance)
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            issues.surrogates += 1
            return Unknown(provenance)
        return Known(value, provenance)
    raise TypeError(f"not a JSON leaf: {type(value).__name__}")


@dataclass
class _RowIssues:
    text_numbers: int = 0
    surrogates: int = 0


def _rows(
    source: SourceReader, config: AdapterConfig, limits: Limits, context: JsonObject
) -> ChunkOutput:
    layout = Layout(context_text(context, "layout"))
    start, end = context_int(context, "start"), context_int(context, "end")
    row = context_int(context, "row")
    data = source.read(start, end - start)
    if layout is Layout.JSON_ARRAY:
        scanner = ArrayScanner(start, inside=True)
        elements = [*scanner.feed(data), *scanner.flush()]
    elif layout is Layout.JSON_LINES:
        elements = list(lines((data,), start))
    else:
        elements = [Element(start, end, data[0])]
    table_id = record_id(StructuredTable.kind, whole(source), config)
    issues = Issues(source, config)
    out: list[StructuredRecord] = []
    for element in elements:
        here = (element.start, element.end, row)
        raw = data[element.start - start : element.end - start]
        at = EvidenceRef(source.content_id, (ByteRange(element.start, len(raw)),))
        rid = record_id(StructuredRecord.kind, at, config)
        row += 1
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            issues.add(
                "invalid_utf8",
                *here,
                f"row {here[2]} is not UTF-8 from byte {element.start + exc.start}; it is not"
                " decoded",
            )
            continue
        if deeper_than(raw, limits.max_json_depth):
            issues.add(
                "json_too_deep",
                *here,
                f"row {here[2]} nests deeper than max_json_depth ({limits.max_json_depth});"
                " it is not decoded",
                details={"max_json_depth": limits.max_json_depth},
            )
            continue
        try:
            value = loads(text)
        except (ValueError, RecursionError) as exc:
            at_char = getattr(exc, "pos", None)
            where = f" at character {at_char}" if isinstance(at_char, int) else ""
            issues.add("json_syntax", *here, f"row {here[2]} is not valid JSON{where}")
            continue
        try:
            found, repeated = leaves(value, limits.max_columns)
            provenances = [
                observed(EvidenceRef(at.source, (*at.locator, JsonPointer(pointer))), config)
                for pointer, _ in found
            ]
        except TooManyLeaves:
            issues.add(
                "too_many_columns",
                *here,
                f"row {here[2]} holds more than max_columns ({limits.max_columns}) values;"
                " it is not decoded",
                details={"max_columns": limits.max_columns},
            )
            continue
        except ValueError:  # a key that is not valid Unicode cannot be cited by a pointer
            issues.add(
                "invalid_utf8",
                *here,
                f"row {here[2]} has a key that is not valid Unicode; it is not decoded",
            )
            continue
        row_issues = _RowIssues()
        cells = [
            _cell(leaf, provenance, row_issues)
            for (_, leaf), provenance in zip(found, provenances, strict=True)
        ]
        if repeated:
            issues.add(
                "json_duplicate_key",
                *here,
                f"row {here[2]} repeats key {repeated[0]!r}; that member is Unknown",
                record=rid,
            )
        if row_issues.text_numbers:
            issues.add(
                "json_number_text",
                *here,
                f"row {here[2]} holds a number no int64, uint64 or double holds exactly;"
                " it is kept as its literal text",
                record=rid,
            )
        if row_issues.surrogates:
            issues.add(
                "invalid_utf8",
                *here,
                f"row {here[2]} holds a string with a lone surrogate; that cell is Unknown",
                record=rid,
            )
        out.append(row_record(at, config, table_id, here[2], cells))
    return ChunkOutput(records=tuple(out), findings=tuple(issues.findings()))


def ingest(
    source: SourceReader, chunk: Chunk, config: AdapterConfig, limits: Limits
) -> ChunkOutput:
    if chunk.context["part"] == "table":
        return _table(source, config, chunk.context)
    return _rows(source, config, limits, chunk.context)

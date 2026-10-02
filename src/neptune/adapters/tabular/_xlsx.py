"""XLSX workbooks: each sheet a table, each cell a value citing the XML that declares it (ADR 0059).

A workbook is a zip of XML parts (``_xlsx_package``). This module reads the parts a table needs
and nothing else: the workbook part (sheet names, the date system), its relationships, the shared
strings, the cell styles' number formats and each worksheet. Pictures, charts, comments, pivot
tables, defined names and everything else stay in the zip.

What a reader may not assume, and so does not:

- **Cells are as declared.** A number is the number the cell stores: a date is its serial, with
  the workbook's 1900 or 1904 date system recorded in the ``workbook`` table, never converted. A
  cell's number format id (and the format code a workbook defines itself) is part of its citation.
  A formula cell is its *cached* value, marked ``formula``, and the formula text is a row of the
  sheet's formulas table; nothing is ever calculated.
- **Blank is not "".** A cell with no value, and a place with no cell, are ``Unknown``, and so is
  a cell holding the empty string, because the model holds no empty text (ADR 0020 §5, ADR 0042
  §1): the cell's citation says which of the three it was.
- **Nothing is followed.** External links are named in a finding and never opened; a VBA project
  is never read.

Blocks. A worksheet is scanned once in ``plan`` (a streaming parse that only counts and finds the
bounds of rows) and cut between rows; each block is read again by ``ingest``, which inflates the
part from its start (deflate cannot seek) and parses the part's prologue plus the block's rows.
"""

import io
import math
import re
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Final
from xml.parsers import expat

from neptune.adapters.contract import (
    SIGNATURE,
    STRUCTURE,
    VERIFIED,
    AdapterConfig,
    Chunk,
    ChunkOutput,
    Plan,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    SourceReader,
    make_chunk,
)
from neptune.adapters.tabular._common import (
    Issues,
    Limits,
    cite,
    context_text,
    finding,
    observed,
    record_id,
    whole,
)
from neptune.adapters.tabular._json import INT_LIMIT, INT_MIN
from neptune.adapters.tabular._xlsx_package import (
    MAX_DEPTH,
    MAX_PROLOGUE,
    Package,
    Problem,
    Refused,
    Relationship,
    SharedStrings,
    Stop,
    Styles,
    Window,
    XlsxLimits,
    drive,
    local,
    new_parser,
    read_relationships,
    read_shared_strings,
    read_styles,
    read_workbook,
    resolve,
    small_int,
)
from neptune.model.finding import IngestFinding
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    AssertionKind,
    Knowledge,
    Known,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import (
    AdapterLocator,
    ByteRange,
    EvidenceRef,
    Locator,
    Provenance,
    adapter_locator,
)
from neptune.model.scalars import NonFinite
from neptune.model.world import CellValue, StructuredRecord, StructuredTable

if TYPE_CHECKING:
    from neptune.model.ids import RecordId

# The steps a citation into a workbook uses (declared in the adapter's descriptor): the stored
# bytes of the part come first, then the byte range inside the part's inflated bytes, then these.
STEP_CELL: Final = "tabular:xlsx_cell"
STEP_FORMULAS: Final = "tabular:xlsx_formulas"
STEP_SHEET: Final = "tabular:xlsx_sheet"
STEP_WORKBOOK: Final = "tabular:xlsx_workbook"

# A block: at most this many rows, cells, or about this many part bytes (a longer row is a block
# of its own). Constants of the adapter's version, never settings.
BLOCK_ROWS: Final = 4096
BLOCK_CELLS: Final = 32768
BLOCK_BYTES: Final = 1024 * 1024
EXTERNAL_LINKS: Final = "xl/externalLinks/"
FORMULA_HEADER: Final = ("ref", "formula", "kind", "si", "range")
_CELL_REF: Final = re.compile(r"([A-Z]{1,3})([0-9]{1,10})")
_NUMBER: Final = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_INTEGER: Final = re.compile(r"[+-]?[0-9]+")
_NON_FINITE: Final = {
    "INF": NonFinite.POSITIVE_INFINITY,
    "+INF": NonFinite.POSITIVE_INFINITY,
    "-INF": NonFinite.NEGATIVE_INFINITY,
    "NaN": NonFinite.NAN,
}
_MAX_ROW_NUMBER: Final = 2**31 - 1
_BOOLEANS: Final = {"0": False, "false": False, "1": True, "true": True}


# --- Probing -----------------------------------------------------------------------------------


def _local_names(head: bytes) -> list[str]:
    """The part names of the zip's leading local headers that the head holds, in order."""
    names: list[str] = []
    at = 0
    while len(names) < 64 and head[at : at + 4] == b"PK\x03\x04" and at + 30 <= len(head):
        flags = int.from_bytes(head[at + 6 : at + 8], "little")
        packed = int.from_bytes(head[at + 18 : at + 22], "little")
        name_length = int.from_bytes(head[at + 26 : at + 28], "little")
        extra_length = int.from_bytes(head[at + 28 : at + 30], "little")
        if at + 30 + name_length > len(head):
            break
        names.append(head[at + 30 : at + 30 + name_length].decode("utf-8", "replace"))
        if flags & 0x8 and packed == 0:
            break  # sizes follow the data: the next header cannot be found without inflating
        at += 30 + name_length + extra_length + packed
    return names


def probe(head: bytes, hints: ProbeHints) -> ProbeResult:
    """A zip whose parts say it is a workbook: ``SIGNATURE`` for parts under ``xl/``, ``VERIFIED``
    when the whole file is in the head and holds the content types and the workbook part. A zip
    that only looks OOXML (``[Content_Types].xml``) is claimed by name alone."""
    names = _local_names(head)
    if len(head) == hints.size:
        try:
            with zipfile.ZipFile(io.BytesIO(head)) as archive:
                names = archive.namelist()[:10_000]
        except (zipfile.BadZipFile, ValueError, OSError, EOFError, NotImplementedError):
            pass
        else:
            if "[Content_Types].xml" in names and "xl/workbook.xml" in names:
                reason = ProbeReason(
                    "tabular.xlsx_workbook",
                    "the zip holds [Content_Types].xml and a workbook part under xl/",
                )
                return ProbeResult(VERIFIED, (reason,))
    if "xl/workbook.bin" in names:
        binary = ProbeReason("tabular.xlsx_binary", "a binary workbook (XLSB) is not read")
        return ProbeResult(0.0, (binary,))
    if any(name.startswith("xl/") for name in names):
        reason = ProbeReason("tabular.xlsx_parts", "the zip's leading parts are under xl/")
        return ProbeResult(SIGNATURE, (reason,))
    named = hints.name.lower().endswith((".xlsx", ".xlsm", ".xltx", ".xltm"))
    if named and names[:1] == ["[Content_Types].xml"]:
        reason = ProbeReason(
            "tabular.xlsx_named", "an OOXML package named as a workbook, its parts not yet seen"
        )
        return ProbeResult(STRUCTURE, (reason,))
    return ProbeResult(
        0.0, (ProbeReason("tabular.not_xlsx", "the zip's parts do not say it is a workbook"),)
    )


# --- Citations ---------------------------------------------------------------------------------


def _span(steps: object) -> ByteRange:
    """A ``[offset, length]`` of a chunk's context as a byte range."""
    assert isinstance(steps, list)
    offset, length = steps
    assert isinstance(offset, int)
    assert isinstance(length, int)
    return ByteRange(offset, length)


def _ints(value: object) -> list[int]:
    assert isinstance(value, list)
    out: list[int] = []
    for item in value:
        assert isinstance(item, int)
        out.append(item)
    return out


def _object(value: object) -> JsonObject:
    assert isinstance(value, dict)
    return value


def _text(value: object) -> str:
    assert isinstance(value, str)
    return value


def _int(value: object) -> int:
    assert isinstance(value, int)
    return value


def _letters(column: int) -> str:
    """The A1 letters of 0-based ``column``."""
    out = ""
    column += 1
    while column:
        column, rest = divmod(column - 1, 26)
        out = chr(65 + rest) + out
    return out


def _column(letters: str) -> int:
    value = 0
    for letter in letters:
        value = value * 26 + ord(letter) - 64
    return value - 1


@dataclass(frozen=True)
class Sheet:
    """What a sheet's chunks share: the workbook's tag that declares it, and its part."""

    index: int
    name: str
    part: str  # the part's name, "" if the sheet has none
    member: ByteRange | None  # the part's stored bytes in the source
    workbook: ByteRange  # the workbook part's stored bytes
    tag: ByteRange  # the <sheet> tag in the workbook part
    shared: str  # the shared strings part, "" if none
    styles: str

    @staticmethod
    def of(context: JsonObject) -> "Sheet":
        raw = _object(context["sheet"])
        member = raw["member"]
        return Sheet(
            _int(raw["index"]),
            _text(raw["name"]),
            _text(raw["part"]),
            _span(member) if member else None,
            _span(raw["workbook"]),
            _span(raw["tag"]),
            _text(raw["shared"]),
            _text(raw["styles"]),
        )

    def to_json(self) -> JsonObject:
        member: JsonValue = (
            [self.member.offset, self.member.length] if self.member is not None else []
        )
        return {
            "index": self.index,
            "member": member,
            "name": self.name,
            "part": self.part,
            "shared": self.shared,
            "styles": self.styles,
            "tag": [self.tag.offset, self.tag.length],
            "workbook": [self.workbook.offset, self.workbook.length],
        }

    def declaration(self, source: SourceReader) -> EvidenceRef:
        """Where the workbook declares the sheet: the workbook part's ``<sheet>`` tag."""
        return cite(source, self.workbook, self.tag)

    def table_evidence(self, source: SourceReader) -> EvidenceRef:
        step = adapter_locator(STEP_SHEET, {"part": self.part, "sheet": self.name})
        return cite(source, self.workbook, self.tag, step)

    def formulas_evidence(self, source: SourceReader) -> EvidenceRef:
        step = adapter_locator(STEP_FORMULAS, {"part": self.part, "sheet": self.name})
        return cite(source, self.workbook, self.tag, step)


class _Cites:
    """Builds the evidence of one sheet's rows and cells."""

    def __init__(self, source: SourceReader, config: AdapterConfig, sheet: Sheet) -> None:
        assert sheet.member is not None
        self.source = source
        self.config = config
        self.sheet = sheet
        self.member: ByteRange = sheet.member

    def at(self, start: int, end: int, *steps: Locator) -> EvidenceRef:
        return cite(self.source, self.member, ByteRange(start, end - start), *steps)

    def cell_step(
        self, ref: str, content: str, numfmt: int = 0, code: str | None = None
    ) -> AdapterLocator:
        fields: dict[str, str | int] = {
            "content": content,
            "part": self.sheet.part,
            "ref": ref,
            "sheet": self.sheet.name,
        }
        if numfmt:
            fields["numfmt"] = numfmt
        if code:
            fields["format"] = code
        return adapter_locator(STEP_CELL, fields)

    def provenance(self, evidence: EvidenceRef) -> Provenance:
        return observed(evidence, self.config)


# --- Findings ----------------------------------------------------------------------------------


def _problem(source: SourceReader, config: AdapterConfig, problem: Problem) -> IngestFinding:
    subject = cite(source, *problem.scope) if problem.scope else whole(source)
    return finding(config, problem.code, subject, problem.message, problem.details)


# --- Reading a worksheet -----------------------------------------------------------------------


class RowNumbers:
    """A sheet's row numbers: a row's ``r``, or the number after the row before it. A row is
    accepted when it comes after every accepted row; the others are dropped (``xlsx_row_order``).
    """

    def __init__(self, seen: int = 0, accepted: int = 0) -> None:
        self.seen = seen
        self.accepted = accepted

    def step(self, attribute: str | None) -> tuple[int, bool]:
        number = self.seen + 1
        if attribute is not None and attribute.isdecimal() and len(attribute) <= 10:
            declared = int(attribute)
            if 1 <= declared <= _MAX_ROW_NUMBER:
                number = declared
        self.seen = number
        accepted = number > self.accepted
        if accepted:
            self.accepted = number
        return number, accepted


@dataclass
class _Formula:
    kind: str
    si: str | None
    ref: str | None
    text: str
    start: int = 0
    end: int = 0


@dataclass
class _Cell:
    ref: str | None
    type: str | None
    style: str | None
    start: int
    end: int = 0
    value: str | None = None
    inline: str | None = None
    formula: _Formula | None = None


@dataclass
class _Row:
    r: str | None
    start: int
    end: int = 0
    cells: list[_Cell] = field(default_factory=list)


def _slices(package: Package, part: str, ranges: list[tuple[int, int]]) -> list[bytes]:
    """The bytes of ``part`` in each of ``ranges`` (ascending, disjoint), inflating once and
    stopping when the last range is done."""
    found = [bytearray() for _ in ranges]
    last = max((end for _, end in ranges), default=0)
    offset = 0
    for piece in package.pieces(part):
        for held, (start, end) in zip(found, ranges, strict=True):
            low, high = max(start, offset), min(end, offset + len(piece))
            if low < high:
                held += piece[low - offset : high - offset]
        offset += len(piece)
        if offset >= last:
            break
    return [bytes(held) for held in found]


def read_rows(
    package: Package, part: str, prologue: int, start: int, end: int
) -> tuple[list[_Row], Problem | None]:
    """The rows in bytes ``[start, end)`` of a worksheet part, read as they would be in place:
    the part's prologue (up to and including ``<sheetData>``) then the rows. Offsets are the
    part's. A ``Problem`` ends the rows early; the rows before it are returned."""
    try:
        before, block = _slices(package, part, [(0, prologue), (start, end)])
    except Problem as problem:
        return [], problem
    data = before + block
    shift = start - len(before)
    window = Window()
    window.push(data)
    parser = new_parser()
    rows: list[_Row] = []
    stack: list[tuple[str | None, int, int]] = []  # kind, where it starts, where its tag ends
    state: dict[str, int] = {"phonetic": 0, "inline": 0}
    buffer: list[str] = []
    row: list[_Row] = []
    cell: list[_Cell] = []
    collecting: list[str] = []

    def start_element(tag: str, attrs: dict[str, str]) -> None:
        kind = local(tag)
        at = parser.CurrentByteIndex
        parent = stack[-1][0] if stack else None
        tag_end = -1
        if len(stack) >= MAX_DEPTH:
            raise Problem("xlsx_limit", "XML nests too deep", {"limit": "depth", "part": part})
        if kind in ("row", "c", "f") and at >= len(before):
            found = window.tag_end(at)
            tag_end = -1 if found is None else found
        if kind == "row" and parent == "sheetData" and at >= len(before):
            row[:] = [_Row(attrs.get("r"), at + shift)]
        elif kind == "c" and parent == "row" and row:
            cell[:] = [_Cell(attrs.get("r"), attrs.get("t"), attrs.get("s"), at + shift)]
        elif kind == "f" and parent == "c" and cell:
            formula = _Formula(attrs.get("t", "normal"), attrs.get("si"), attrs.get("ref"), "")
            formula.start = at + shift
            cell[0].formula = formula
            collecting[:] = ["f"]
            buffer.clear()
        elif kind == "v" and parent == "c" and cell:
            collecting[:] = ["v"]
            buffer.clear()
        elif kind == "is" and parent == "c" and cell:
            state["inline"] = 1
            cell[0].inline = ""
        elif kind == "rPh" and state["inline"]:
            state["phonetic"] += 1
        elif kind == "t" and state["inline"] and not state["phonetic"]:
            collecting[:] = ["t"]
            buffer.clear()
        stack.append((kind, at, tag_end))

    def end_element(tag: str) -> None:
        kind, at, tag_end = stack.pop()
        here = parser.CurrentByteIndex
        if kind in ("row", "c", "f") and at >= len(before):
            empty = tag_end >= 0 and window.is_empty_tag(at, tag_end)
            stop = tag_end if empty else window.tag_end(here)
            if stop is None:
                raise Problem(
                    "xlsx_corrupt",
                    f"part {part!r} holds a tag longer than a read piece",
                    {"part": part, "error": "tag_too_long"},
                )
            stop += shift
        else:
            stop = 0
        if kind == "row" and row and stack and stack[-1][0] == "sheetData":
            row[0].end = stop
            rows.append(row[0])
            row.clear()
        elif kind == "c" and cell and row:
            cell[0].end = stop
            row[0].cells.append(cell[0])
            cell.clear()
        elif kind == "f" and cell and cell[0].formula is not None:
            cell[0].formula.end = stop
            cell[0].formula.text = "".join(buffer)
            collecting.clear()
        elif kind == "v" and cell and collecting == ["v"]:
            cell[0].value = "".join(buffer)
            collecting.clear()
        elif kind == "t" and cell and collecting == ["t"]:
            cell[0].inline = (cell[0].inline or "") + "".join(buffer)
            collecting.clear()
        elif kind == "rPh" and state["phonetic"]:
            state["phonetic"] -= 1
        elif kind == "is":
            state["inline"] = 0

    def characters(text: str) -> None:
        if collecting:
            buffer.append(text)

    parser.StartElementHandler = start_element
    parser.EndElementHandler = end_element
    parser.CharacterDataHandler = characters
    try:
        parser.Parse(data, False)
        parser.Parse(b"", False)
    except Problem as problem:
        return rows, Problem(problem.code, problem.message, problem.details, (package.span(part),))
    except Refused:
        return rows, Problem(
            "xlsx_part_refused",
            f"part {part!r} declares a document type; it is not read",
            {"part": part, "reason": "doctype"},
            (package.span(part),),
        )
    except expat.ExpatError:
        return rows, Problem(
            "xlsx_corrupt",
            f"part {part!r} is not well formed XML in the rows read: what precedes is read",
            {"part": part, "error": "bad_xml", "offset": parser.ErrorByteIndex + shift},
            (package.span(part),),
        )
    return rows, None


# --- Plan --------------------------------------------------------------------------------------


@dataclass
class _Block:
    start: int
    end: int
    seen: int
    accepted: int
    formulas: int
    rows: int = 0
    cells: int = 0


@dataclass
class _Scan:
    """What one pass over a worksheet found: its blocks, its header row, its formulas."""

    prologue: int = -1  # the bytes up to and including <sheetData>'s tag; -1: no sheetData
    blocks: list[_Block] = field(default_factory=list)
    header: tuple[int, int] | None = None
    header_formulas: int = 0  # the formulas before the header row
    formulas: int = 0
    findings: list[IngestFinding] = field(default_factory=list)


def _scan(
    package: Package,
    source: SourceReader,
    config: AdapterConfig,
    part: str,
    limits: Limits,
    with_header: bool,
) -> _Scan:
    """One streaming pass over a worksheet that counts and bounds rows, parsing no cell."""
    xl = package.limits
    found = _Scan()
    window = Window()
    parser = new_parser()
    stack: list[str | None] = []
    numbers = RowNumbers()
    member = package.span(part)
    # one row at a time: [start, cells, formulas, empty, width, last column, cell has a formula]
    row: list[list[int]] = []
    row_numbers: list[tuple[int, bool, int, int]] = []
    totals = {"rows": 0, "cells": 0, "header": 0}
    block: list[_Block] = []

    def close() -> None:
        if block:
            found.blocks.append(block[0])
            block.clear()

    def finish(
        start: int,
        end: int,
        declared: int,
        cells: int,
        formulas: int,
        numbered: tuple[int, bool, int, int],
    ) -> None:
        """A row of ``declared`` cells that read as ``cells`` (a gap is a cell too)."""
        number, accepted, seen_before, accepted_before = numbered
        totals["rows"] += 1
        if totals["rows"] > limits.max_rows:
            close()
            found.findings.append(
                finding(
                    config,
                    "row_limit",
                    cite(source, member, ByteRange(start, end - start)),
                    f"the sheet holds more than max_rows ({limits.max_rows}) rows; rows from"
                    f" number {number} on are not read",
                    {"max_rows": limits.max_rows, "row": number, "part": part},
                )
            )
            raise Stop
        totals["cells"] += cells
        if totals["cells"] > xl.max_cells:
            close()
            found.findings.append(
                finding(
                    config,
                    "xlsx_limit",
                    cite(source, member, ByteRange(start, end - start)),
                    f"the sheet holds more than xlsx_max_cells ({xl.max_cells}) cells; rows from"
                    f" number {number} on are not read",
                    {"limit": "xlsx_max_cells", "max": xl.max_cells, "part": part, "row": number},
                )
            )
            raise Stop
        before = found.formulas
        found.formulas += formulas
        if declared == 0:
            return
        too_large = end - start > limits.max_row_bytes
        if too_large:
            close()
            found.findings.append(
                finding(
                    config,
                    "row_too_large",
                    cite(source, member, ByteRange(start, end - start)),
                    f"row {number} holds {end - start} bytes, over max_row_bytes"
                    f" ({limits.max_row_bytes}); it is not decoded",
                    {"bytes": end - start, "max_row_bytes": limits.max_row_bytes, "row": number},
                )
            )
        if with_header and not totals["header"] and accepted:
            # the first row is the header, whether or not it could be read: a data row is never
            # promoted into its place
            totals["header"] = 1
            close()
            if not too_large:
                found.header = (start, end)
                found.header_formulas = before
            return
        if too_large:
            return
        if block and (
            block[0].rows >= BLOCK_ROWS
            or block[0].cells + cells > BLOCK_CELLS
            or end - block[0].start > BLOCK_BYTES
        ):
            close()
        if not block:
            block.append(_Block(start, end, seen_before, accepted_before, before))
        block[0].end = end
        block[0].rows += 1
        block[0].cells += cells

    def start_element(tag: str, attrs: dict[str, str]) -> None:
        kind = local(tag)
        at = parser.CurrentByteIndex
        parent = stack[-1] if stack else None
        if len(stack) >= MAX_DEPTH:
            raise Problem("xlsx_limit", "XML nests too deep", {"limit": "depth", "part": part})
        if kind == "sheetData" and parent == "worksheet" and found.prologue < 0:
            end = window.tag_end(at)
            if end is None or end > MAX_PROLOGUE:
                raise Problem(
                    "xlsx_limit",
                    f"the part {part!r} holds more than {MAX_PROLOGUE} bytes before its rows",
                    {"limit": "prologue", "part": part, "max": MAX_PROLOGUE},
                    (member,),
                )
            found.prologue = end
            if window.is_empty_tag(at, end):
                raise Stop
        elif kind == "row" and parent == "sheetData" and found.prologue >= 0:
            end = window.tag_end(at)
            empty = end is not None and window.is_empty_tag(at, end)
            seen_before, accepted_before = numbers.seen, numbers.accepted
            number, accepted = numbers.step(attrs.get("r"))
            row[:] = [[at, 0, 0, 1 if empty else 0, 0, -1, 0]]
            row_numbers[:] = [(number, accepted, seen_before, accepted_before)]
        elif kind == "c" and parent == "row" and row:
            counts = row[0]
            counts[1] += 1
            reference = _CELL_REF.fullmatch(attrs.get("r", ""))
            column = _column(reference.group(1)) if reference else counts[5] + 1
            counts[4] = max(counts[4], min(column, limits.max_columns) + 1)
            counts[5], counts[6] = column, 0
        elif kind == "f" and parent == "c" and row and not row[0][6]:
            row[0][2] += 1  # a cell holds one formula: a second <f> is not another
            row[0][6] = 1
        stack.append(kind)

    def end_element(tag: str) -> None:
        kind = stack.pop()
        here = parser.CurrentByteIndex
        parent = stack[-1] if stack else None
        if kind == "row" and parent == "sheetData" and row:
            start, declared, formulas, empty, width, _, _ = row[0]
            numbered = row_numbers[0]
            row.clear()
            if empty:
                finish(start, here, 0, 0, 0, numbered)
                return
            end = window.tag_end(here)
            if end is None:
                raise Problem(
                    "xlsx_corrupt",
                    f"part {part!r} holds a tag longer than a read piece",
                    {"part": part, "error": "tag_too_long"},
                    (member,),
                )
            finish(start, end, declared, width, formulas, numbered)
        elif kind == "sheetData" and parent == "worksheet":
            raise Stop

    parser.StartElementHandler = start_element
    parser.EndElementHandler = end_element
    try:
        drive(package, part, parser, window)
    except Problem as problem:  # a damaged part keeps the rows before the damage
        found.findings.append(_problem(source, config, problem))
    close()
    return found


@dataclass
class _Located:
    workbook: str
    relationships: dict[str, Relationship]
    rels_part: str | None


def _locate(package: Package) -> _Located:
    """The workbook part and its relationships. A ``Problem`` if there is no workbook."""
    workbook: str | None = None
    if package.has("_rels/.rels"):
        for rel in read_relationships(package, "_rels/.rels"):
            target = resolve("x", rel.target)
            if rel.type == "officeDocument" and not rel.external and target is not None:
                workbook = target
                break
    if workbook is None and package.has("xl/workbook.xml"):
        workbook = "xl/workbook.xml"
    if workbook is None or not package.has(workbook):
        raise Problem(
            "xlsx_corrupt",
            "the zip names no workbook part: it is not a workbook",
            {"error": "no_workbook"},
        )
    folder, _, base = workbook.rpartition("/")
    rels = f"{folder}/_rels/{base}.rels" if folder else f"_rels/{base}.rels"
    found: dict[str, Relationship] = {}
    if package.has(rels):
        for rel in read_relationships(package, rels):
            found.setdefault(rel.id, rel)
    return _Located(workbook, found, rels if package.has(rels) else None)


def _unreadable(source: SourceReader, config: AdapterConfig, problem: Problem) -> Plan:
    context: JsonObject = {"layout": "xlsx", "part": "unreadable"}
    chunk = make_chunk(source, config, context, source.size)
    return Plan((chunk,), (_problem(source, config, problem),))


def plan(source: SourceReader, config: AdapterConfig, limits: Limits) -> Plan:
    xl = XlsxLimits.of(config)
    try:
        package = Package.open(source, xl)
        located = _locate(package)
        book = read_workbook(package, located.workbook)
    except Problem as problem:
        return _unreadable(source, config, problem)
    findings: list[IngestFinding] = []
    workbook = package.span(located.workbook)
    by_type = {rel.type: rel for rel in located.relationships.values() if not rel.external}

    def target_of(rel: Relationship | None) -> str:
        found = resolve(located.workbook, rel.target) if rel is not None else None
        return found if found is not None and package.has(found) else ""

    shared_part, styles_part = (
        target_of(by_type.get("sharedStrings")),
        target_of(by_type.get("styles")),
    )
    for damage in (
        read_shared_strings(package, shared_part or None)[1],
        read_styles(package, styles_part or None)[1],
    ):
        if damage is not None:
            findings.append(_problem(source, config, damage))
    if book.sheet_total > len(book.sheets):
        findings.append(
            finding(
                config,
                "xlsx_limit",
                cite(source, workbook),
                f"the workbook declares {book.sheet_total} sheets, over xlsx_max_sheets"
                f" ({xl.max_sheets}); sheets from {len(book.sheets)} on are not read",
                {"limit": "xlsx_max_sheets", "max": xl.max_sheets, "sheets": book.sheet_total},
            )
        )
    externals = [rel for rel in located.relationships.values() if rel.external]
    linked = [rel for rel in located.relationships.values() if rel.type == "externalLink"]
    parts = [name for name in package.names if name.startswith(EXTERNAL_LINKS)]
    if externals or linked or parts:
        subject = (
            cite(source, package.span(located.rels_part))
            if located.rels_part is not None
            else cite(source, workbook)
        )
        targets = [rel.target[:200] for rel in externals[:10]]
        findings.append(
            finding(
                config,
                "xlsx_external_link",
                subject,
                f"the workbook links outside itself ({len(externals)} external targets,"
                f" {len(parts)} external link parts); nothing is followed or opened",
                {"external": len(externals), "link_parts": len(parts), "targets": targets},
            )
        )
    for name in package.names:
        if name.lower().endswith("vbaproject.bin"):
            findings.append(
                finding(
                    config,
                    "xlsx_macros",
                    cite(source, package.span(name)),
                    f"part {name!r} is a VBA project: it is never read, parsed or run",
                    {"part": name},
                )
            )
    with_header = config.text("csv_header") == "first_row"
    chunks = [
        make_chunk(
            source,
            config,
            {
                "date1904": -1 if book.date1904 is None else int(book.date1904),
                "epoch": list(book.epoch) if book.epoch else [],
                "layout": "xlsx",
                "member": [workbook.offset, workbook.length],
                "part": "workbook",
                "sheet_total": book.sheet_total,
                "sheets": [
                    [sheet.name, sheet.state or "", sheet.start, sheet.length]
                    for sheet in book.sheets
                ],
                "workbook": located.workbook,
            },
            workbook.length,
        )
    ]
    taken: set[str] = set()
    for index, decl in enumerate(book.sheets):
        rel = located.relationships.get(decl.rid or "")
        part = target_of(rel) if rel is not None and rel.type == "worksheet" else ""
        reason = (
            "no_relationship"
            if rel is None
            else f"type_{rel.type}"
            if rel.type != "worksheet"
            else "part_missing"
        )
        if part in taken:  # two sheets over one part would have the same rows' ids
            part, reason = "", "part_shared"
        if part:
            taken.add(part)
        sheet = Sheet(
            index,
            decl.name,
            part,
            package.span(part) if part else None,
            workbook,
            ByteRange(decl.start, decl.length),
            shared_part,
            styles_part,
        )
        scan = _Scan()
        if part:
            scan = _scan(package, source, config, part, limits, with_header)
        findings.extend(scan.findings)
        covered = scan.prologue >= 0
        if not covered and not scan.findings:
            why = reason if not part else "no_sheet_data"
            findings.append(
                finding(
                    config,
                    "xlsx_sheet_unsupported",
                    sheet.declaration(source),
                    f"sheet {decl.name!r} is not a worksheet this reader can read ({why}); its"
                    " table has no header and no rows",
                    {
                        "sheet": decl.name,
                        "reason": why,
                        "part": part,
                    },
                )
            )
        table: JsonObject = {
            "covered": covered,
            "formulas": scan.formulas > 0,
            "header": list(scan.header) if scan.header else [],
            "header_formulas": scan.header_formulas,
            "layout": "xlsx",
            "part": "sheet_table",
            "prologue": scan.prologue,
            "sheet": sheet.to_json(),
        }
        chunks.append(make_chunk(source, config, table, 0 if scan.header is None else 1))
        for block in scan.blocks:
            context: JsonObject = {
                "accepted": block.accepted,
                "end": block.end,
                "formulas": block.formulas,
                "layout": "xlsx",
                "part": "sheet_rows",
                "prologue": scan.prologue,
                "seen": block.seen,
                "sheet": sheet.to_json(),
                "start": block.start,
            }
            chunks.append(make_chunk(source, config, context, block.end - block.start))
    return Plan(tuple(chunks), tuple(findings))


# --- Ingest ------------------------------------------------------------------------------------


def _number(text: str) -> tuple[CellValue, bool] | None:
    """A numeric cell's value, and whether it kept its type; ``None`` if it is no number."""
    text = text.strip()
    if text in _NON_FINITE:
        return _NON_FINITE[text], True
    if not _NUMBER.fullmatch(text):
        return None
    if _INTEGER.fullmatch(text):
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


@dataclass(frozen=True)
class _Decoded:
    """A cell's state as its XML declares it. ``kind`` is a Knowledge state or an ``issue``."""

    kind: str  # known, unknown, not_covered
    content: str  # blank, empty_string, value, error
    value: CellValue | None = None
    issue: str = ""  # a finding code about this cell


def _known(value: CellValue, content: str = "value") -> _Decoded:
    return _Decoded("known", content, value)


def _decode(raw: _Cell, shared: SharedStrings) -> _Decoded:
    kind = raw.type or "n"
    blank = _Decoded("unknown", "blank")
    empty = _Decoded("unknown", "empty_string")
    text = raw.value
    if kind == "inlineStr":
        if raw.inline is None:
            return blank
        return _known(raw.inline) if raw.inline else empty
    if text is None:
        return blank
    if kind == "s":
        index = text.strip()
        if not index.isdecimal() or len(index) > 12:
            return _Decoded("unknown", "value", issue="xlsx_cell_unreadable")
        if int(index) < len(shared.strings):
            string = shared.strings[int(index)]
            return _known(string) if string else empty
        if shared.complete:
            return _Decoded("unknown", "value", issue="xlsx_shared_string_ref")
        return _Decoded("not_covered", "value")
    if kind in ("str", "d"):
        return _known(text) if text else empty
    if kind == "e":
        return _known(text, "error") if text else empty
    if kind == "b":
        flag = _BOOLEANS.get(text.strip())
        if flag is None:
            return _Decoded("unknown", "value", issue="xlsx_cell_unreadable")
        return _Decoded("known", "value", flag)
    if kind == "n":
        if not text.strip():
            return blank
        number = _number(text)
        if number is None:
            return _Decoded("unknown", "value", issue="xlsx_cell_unreadable")
        value, exact = number
        return _Decoded("known", "value", value, "" if exact else "xlsx_number_text")
    return _Decoded("unknown", "value", issue="xlsx_cell_unreadable")


def _state(decoded: _Decoded, provenance: Provenance) -> Knowledge[CellValue]:
    if decoded.kind == "known":
        assert decoded.value is not None
        return Known(decoded.value, provenance)
    if decoded.kind == "not_covered":
        return NotCovered(provenance)
    return Unknown(provenance)


def _raw_text(raw: _Cell, shared: SharedStrings) -> str:
    """A header cell's text as the sheet writes it: a string's text, or a stored value verbatim."""
    decoded = _decode(raw, shared)
    if decoded.kind == "known" and isinstance(decoded.value, str):
        return decoded.value
    if raw.type in (None, "n", "b", "e", "str", "d") and raw.value is not None:
        return raw.value
    return ""


@dataclass
class _Context:
    """What decoding a sheet's rows needs, once per chunk."""

    source: SourceReader
    config: AdapterConfig
    sheet: Sheet
    cites: _Cites
    shared: SharedStrings
    styles: Styles
    limits: Limits


def _open(package: Package, sheet: Sheet, rows: list[_Row]) -> tuple[SharedStrings, Styles]:
    """The shared strings and the styles, read only when a cell of ``rows`` needs them."""
    cells = [cell for row in rows for cell in row.cells]
    shared, styles = SharedStrings(), Styles()
    if any(cell.type == "s" for cell in cells):
        shared, _ = read_shared_strings(package, sheet.shared or None)
    if any(cell.style is not None for cell in cells):
        styles, _ = read_styles(package, sheet.styles or None)
    return shared, styles


def _format(styles: Styles, style: str | None) -> tuple[int, str | None]:
    """A cell's number format id and, when the workbook defines the code, the code."""
    index = small_int(style)
    if index is None or index >= len(styles.formats):
        return 0, None
    ident = styles.formats[index]
    return ident, styles.codes.get(ident)


def _row_cells(
    ctx: _Context, raw: _Row, number: int, issues: Issues, record: "RecordId | None"
) -> tuple[list[Knowledge[CellValue]], list[tuple[int, _Cell, str]]] | None:
    """A row's cells by column, a gap a blank cell, and its formula cells; ``None`` if the row
    has more cells than ``max_columns`` (it is not decoded)."""
    cites = ctx.cites
    placed: dict[int, _Cell] = {}
    last = -1
    for cell in raw.cells:
        if cell.ref is None:
            column = last + 1
        else:
            parsed = _CELL_REF.fullmatch(cell.ref)
            if parsed is None or int(parsed.group(2)) != number or _column(parsed.group(1)) <= last:
                issues.add(
                    "xlsx_cell_ref",
                    cell.start,
                    cell.end,
                    number,
                    f"cell {cell.ref!r} of row {number} is not an A1 reference of the row, or"
                    " does not follow the cell before it; it is dropped",
                    record=record,
                )
                continue
            column = _column(parsed.group(1))
        if column >= ctx.limits.max_columns:
            issues.add(
                "too_many_columns",
                raw.start,
                raw.end,
                number,
                f"row {number} holds a cell in column {column + 1}, over max_columns"
                f" ({ctx.limits.max_columns}); it is not decoded",
                details={"max_columns": ctx.limits.max_columns},
                record=record,
            )
            return None
        placed[column] = cell
        last = column
    if not placed:
        return [], []
    cells: list[Knowledge[CellValue]] = []
    formulas: list[tuple[int, _Cell, str]] = []
    for column in range(last + 1):
        ref = f"{_letters(column)}{number}"
        held = placed.get(column)
        if held is None:
            evidence = cites.at(raw.start, raw.end, cites.cell_step(ref, "missing"))
            cells.append(Unknown(cites.provenance(evidence)))
            continue
        cell = held
        decoded = _decode(cell, ctx.shared)
        content = decoded.content
        if cell.formula is not None:
            content = "formula"
            formulas.append((column, cell, ref))
            if decoded.kind == "unknown" and decoded.content == "blank":
                issues.add(
                    "xlsx_formula_no_value",
                    cell.start,
                    cell.end,
                    number,
                    f"formula cell {ref} of row {number} has no cached value",
                    record=record,
                )
        numfmt, code = _format(ctx.styles, cell.style)
        evidence = cites.at(cell.start, cell.end, cites.cell_step(ref, content, numfmt, code))
        cells.append(_state(decoded, cites.provenance(evidence)))
        if decoded.issue:
            _cell_issue(issues, decoded.issue, cell, ref, number, record)
    return cells, formulas


def _cell_issue(
    issues: Issues, name: str, cell: _Cell, ref: str, number: int, record: "RecordId | None"
) -> None:
    messages = {
        "xlsx_cell_unreadable": f"cell {ref} declares type {cell.type or 'n'!r} and its value"
        " does not read as that type; it is Unknown",
        "xlsx_number_text": f"cell {ref} holds a number no int64, uint64 or double holds exactly;"
        " it keeps its literal text",
        "xlsx_shared_string_ref": f"cell {ref} names a shared string the workbook does not have;"
        " it is Unknown",
    }
    issues.add(name, cell.start, cell.end, number, messages[name], record=record)


def _formula_row(
    ctx: _Context,
    table_id: "RecordId",
    ordinal: int,
    cell: _Cell,
    ref: str,
) -> StructuredRecord:
    """One row of the sheet's formulas table: the formula text of a formula cell."""
    cites, formula = ctx.cites, cell.formula
    assert formula is not None
    cell_evidence = cites.at(cell.start, cell.end, cites.cell_step(ref, "formula"))
    declared = cites.provenance(
        cites.at(formula.start, formula.end, cites.cell_step(ref, "formula_text"))
    )
    cells: list[Knowledge[CellValue]] = [
        Known(ref, cites.provenance(cell_evidence)),
        Known(formula.text, declared) if formula.text else Unknown(declared),
        Known(formula.kind, declared),
        Known(shared, declared)
        if (shared := small_int(formula.si)) is not None
        else Unknown(declared),
        Known(formula.ref, declared) if formula.ref else Unknown(declared),
    ]
    return StructuredRecord(
        id=record_id(StructuredRecord.kind, cell_evidence, ctx.config),
        provenance=cites.provenance(cell_evidence),
        table=table_id,
        row=ordinal,
        cells=tuple(cells),
    )


def _rows(
    ctx: _Context,
    rows: list[_Row],
    numbers: RowNumbers,
    table_id: "RecordId",
    formulas_id: "RecordId | None",
    formula_base: int,
    scope: tuple[ByteRange, ...],
) -> tuple[list[StructuredRecord], list[IngestFinding]]:
    cites = ctx.cites
    issues = Issues(ctx.source, ctx.config, scope)
    out: list[StructuredRecord] = []
    ordinal = formula_base
    for raw in rows:
        # a formula's ordinal is its place among the formulas of the sheet's rows as they stand,
        # whether or not its row or cell is dropped, so the blocks the sheet is cut into change
        # no ordinal
        position = {id(c): k for k, c in enumerate(c for c in raw.cells if c.formula is not None)}
        first, ordinal = ordinal, ordinal + len(position)
        number, accepted = numbers.step(raw.r)
        evidence = cites.at(raw.start, raw.end)
        rid = record_id(StructuredRecord.kind, evidence, ctx.config)
        if not accepted:
            issues.add(
                "xlsx_row_order",
                raw.start,
                raw.end,
                number,
                f"row {number} does not come after the rows before it; it is dropped",
                record=None,
            )
            continue
        made = _row_cells(ctx, raw, number, issues, rid)
        if made is None:
            continue
        cells, formulas = made
        if not cells:
            continue
        out.append(
            StructuredRecord(
                id=rid,
                provenance=cites.provenance(evidence),
                table=table_id,
                row=number - 1,
                cells=tuple(cells),
            )
        )
        if formulas_id is not None:
            for _, cell, ref in formulas:
                out.append(_formula_row(ctx, formulas_id, first + position[id(cell)], cell, ref))
    return out, issues.findings()


def _context(
    source: SourceReader,
    config: AdapterConfig,
    limits: Limits,
    sheet: Sheet,
    package: Package,
    rows: list[_Row],
) -> _Context:
    shared, styles = _open(package, sheet, rows)
    return _Context(source, config, sheet, _Cites(source, config, sheet), shared, styles, limits)


def _workbook(source: SourceReader, config: AdapterConfig, context: JsonObject) -> ChunkOutput:
    part, member = _text(context["workbook"]), _span(context["member"])
    step = adapter_locator(STEP_WORKBOOK, {"part": part})
    evidence = cite(source, member, step)
    header: Knowledge[tuple[str, ...]] = Known(("property", "value"), observed(evidence, config))
    table = StructuredTable(
        id=record_id(StructuredTable.kind, evidence, config),
        provenance=observed(evidence, config),
        name=NotCovered(),
        header=header,
    )
    out: list[StructuredRecord] = []

    def add(
        prop: str,
        value: CellValue | None,
        tag: ByteRange | None,
        kind: AssertionKind,
        extra: str = "",
    ) -> None:
        row_step = adapter_locator(STEP_WORKBOOK, {"part": part, "property": prop + extra})
        place = (
            cite(source, member, tag, row_step)
            if tag is not None
            else cite(source, member, row_step)
        )
        key = Provenance(place, config.transform.id, AssertionKind.OBSERVED)
        state = Provenance(place, config.transform.id, kind)
        out.append(
            StructuredRecord(
                id=record_id(StructuredRecord.kind, place, config),
                provenance=key,
                table=table.id,
                row=len(out),
                cells=(Known(prop, key), Unknown(state) if value is None else Known(value, state)),
            )
        )

    flag = _int(context["date1904"])
    epoch = _ints(context["epoch"])
    tag = ByteRange(epoch[0], epoch[1]) if epoch else None
    # What the workbook states and nothing else: with no statement the epoch is Unknown. The
    # format's default (ECMA-376 18.2.28: the 1900 system) is its specification's, not the
    # workbook's, and applying it is the reader of the serials' decision.
    add("date_epoch", None if flag < 0 else 1904 if flag == 1 else 1900, tag, AssertionKind.STATED)
    add("sheet_count", _int(context["sheet_total"]), None, AssertionKind.OBSERVED)
    sheets = context["sheets"]
    assert isinstance(sheets, list)
    for index, entry in enumerate(sheets):
        assert isinstance(entry, list)
        name, state, start, length = entry
        assert isinstance(name, str)
        assert isinstance(state, str)
        assert isinstance(start, int)
        assert isinstance(length, int)
        place = ByteRange(start, length)
        suffix = f"[{index}]"
        if name:
            add("sheet", name, place, AssertionKind.OBSERVED, suffix)
        if state:
            add("sheet_state", state, place, AssertionKind.OBSERVED, suffix)
    return ChunkOutput(records=(table, *out))


def _sheet_table(
    source: SourceReader,
    config: AdapterConfig,
    limits: Limits,
    context: JsonObject,
    package: Package | None,
) -> ChunkOutput:
    sheet = Sheet.of(context)
    declared = sheet.declaration(source)
    evidence = sheet.table_evidence(source)
    name: Knowledge[str] = (
        Known(sheet.name, observed(declared, config))
        if sheet.name
        else Unknown(observed(declared, config))
    )
    header: Knowledge[tuple[str, ...]] = Unknown()
    findings: list[IngestFinding] = []
    ctx: _Context | None = None
    formula_rows: list[tuple[int, _Cell, str]] = []
    mode = config.text("csv_header")
    bounds = _ints(context["header"])
    covered = context["covered"] is True
    if not covered:
        header = NotCovered()
    elif mode == "none":
        header = NotApplicable()
    elif mode == "first_row" and bounds and package is not None:
        rows, problem = read_rows(
            package, sheet.part, _int(context["prologue"]), bounds[0], bounds[1]
        )
        ctx = _context(source, config, limits, sheet, package, rows)
        if problem is not None:
            findings.append(_problem(source, config, problem))
        if rows and rows[0].cells:
            number, _ = RowNumbers().step(rows[0].r)
            issues = Issues(source, config, (ctx.cites.member,))
            names = _header_names(ctx, rows[0], number, issues)
            at = ctx.cites.at(rows[0].start, rows[0].end)
            if names is not None:
                header = Known(names, observed(at, config))
            header_cells = _row_cells(ctx, rows[0], number, issues, None)
            findings.extend(issues.findings())
            header_formulas = (header_cells or ([], []))[1]
            held = {id(c): k for k, c in enumerate(c for c in rows[0].cells if c.formula)}
            formula_rows = [
                (_int(context["header_formulas"]) + held[id(cell)], cell, ref)
                for _, cell, ref in header_formulas
            ]
    records: list[StructuredTable | StructuredRecord] = [
        StructuredTable(
            id=record_id(StructuredTable.kind, evidence, config),
            provenance=observed(evidence, config),
            name=name,
            header=header,
        )
    ]
    if context["formulas"] is True and covered:
        formulas_evidence = sheet.formulas_evidence(source)
        formulas_table = StructuredTable(
            id=record_id(StructuredTable.kind, formulas_evidence, config),
            provenance=observed(formulas_evidence, config),
            name=NotCovered(),
            header=Known(FORMULA_HEADER, observed(formulas_evidence, config)),
        )
        records.append(formulas_table)
        if formula_rows:  # a header row's formulas are rows of the formulas table too
            assert ctx is not None
            records.extend(
                _formula_row(ctx, formulas_table.id, ordinal, cell, ref)
                for ordinal, cell, ref in formula_rows
            )
    return ChunkOutput(records=tuple(records), findings=tuple(findings))


def _header_names(ctx: _Context, raw: _Row, number: int, issues: Issues) -> tuple[str, ...] | None:
    """The header row's cells as text by column, a gap ``""``; ``None`` if it cannot be read."""
    columns: dict[int, str] = {}
    for cell in raw.cells:
        parsed = _CELL_REF.fullmatch(cell.ref) if cell.ref else None
        column = _column(parsed.group(1)) if parsed else max(columns, default=-1) + 1
        if column >= ctx.limits.max_columns:
            issues.add(
                "too_many_columns",
                raw.start,
                raw.end,
                number,
                f"the header row holds a cell in column {column + 1}, over max_columns"
                f" ({ctx.limits.max_columns}); the header is Unknown",
                details={"max_columns": ctx.limits.max_columns},
            )
            return None
        if _decode(cell, ctx.shared).kind == "not_covered":
            return None  # a header cell names a shared string that was not read
        columns[column] = _raw_text(cell, ctx.shared)
    if not columns:
        return None
    return tuple(columns.get(column, "") for column in range(max(columns) + 1))


def _sheet_rows(
    source: SourceReader,
    config: AdapterConfig,
    limits: Limits,
    context: JsonObject,
    package: Package,
) -> ChunkOutput:
    sheet = Sheet.of(context)
    start, end = _int(context["start"]), _int(context["end"])
    rows, problem = read_rows(package, sheet.part, _int(context["prologue"]), start, end)
    ctx = _context(source, config, limits, sheet, package, rows)
    table_id = record_id(StructuredTable.kind, sheet.table_evidence(source), config)
    formulas_id = record_id(StructuredTable.kind, sheet.formulas_evidence(source), config)
    numbers = RowNumbers(_int(context["seen"]), _int(context["accepted"]))
    records, findings = _rows(
        ctx, rows, numbers, table_id, formulas_id, _int(context["formulas"]), (ctx.cites.member,)
    )
    if problem is not None:
        findings.append(_problem(source, config, problem))
    return ChunkOutput(records=tuple(records), findings=tuple(findings))


def ingest(
    source: SourceReader, chunk: Chunk, config: AdapterConfig, limits: Limits
) -> ChunkOutput:
    context = chunk.context
    kind = context_text(context, "part")
    if kind == "unreadable":
        evidence = whole(source)
        table = StructuredTable(
            id=record_id(StructuredTable.kind, evidence, config),
            provenance=observed(evidence, config),
            name=NotCovered(),
            header=NotCovered(),
        )
        return ChunkOutput(records=(table,))
    if kind == "workbook":
        return _workbook(source, config, context)
    try:
        package = Package.open(source, XlsxLimits.of(config))
    except Problem as problem:
        return ChunkOutput(findings=(_problem(source, config, problem),))
    if kind == "sheet_table":
        return _sheet_table(source, config, limits, context, package)
    return _sheet_rows(source, config, limits, context, package)

"""Markdown documents: CommonMark's blocks, as the syntax declares them, each citing its exact span.

What it emits for a file (ADR 0038):

- one ``DocumentRecord`` citing all of the file's bytes: format ``markdown``, the ``title`` its
  YAML front matter declares (``Unknown`` without one), no pages;
- one ``DocumentBlock`` per CommonMark leaf block with text, in document order, citing
  ``[Span(start, end)]`` in the file's text (``_blocks`` gives the span rule per block);
- one ``StructuredTable`` per GFM table and one ``StructuredRecord`` per body row, each cell
  citing its span.

Roles are the syntax's: an ATX or setext heading is a ``heading`` with its level; a paragraph in a
list item is a ``list_item`` with its list depth, in a blockquote a ``quote``, otherwise a
``paragraph``; fenced and indented code is ``code``; a GFM table is a ``table``. An HTML block's
role is ``Unknown``: CommonMark declares raw HTML, not what it is. So is a link reference
definition's (``[label]: url "title"``): the model has no role for it, but the text is evidence a
learner needs to resolve ``[text][label]``, so it is a block, and ``markdown.link_definitions``
says how many. Thematic breaks and blank lines are not blocks; they stay in the bytes.

The text is the file's bytes after a leading UTF-8 BOM, decoded as UTF-8; spans count code
points, an invalid sequence counting as the U+FFFD that Python's ``errors="replace"`` gives it,
as the ``text`` adapter does. Front matter is a first line ``---`` and a closing ``---`` or
``...`` line within the first 64 KiB; it is not CommonMark and is never parsed as blocks.

Chunks: chunk 0 is the document; chunk 1 holds every block, since CommonMark's blocks depend on
the whole file (lazy continuation, link definitions). ``max_document_bytes`` bounds that chunk.
"""

import codecs
import re
from bisect import bisect_left
from collections.abc import Callable
from contextvars import ContextVar
from importlib.metadata import version
from typing import Final

from neptune.adapters.contract import (
    ABI_VERSION,
    GENERIC,
    NAME_ONLY,
    PROBE_HEAD_SIZE,
    STRUCTURE,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    ConfigOption,
    Documented,
    FormatSpec,
    InspectResult,
    Plan,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    Resources,
    SourceReader,
    make_chunk,
    read_pieces,
)
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import EvidenceRecord, evidence_record_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import (
    AssertionKind,
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, Span
from neptune.model.world import (
    BlockRole,
    CellValue,
    DocumentBlock,
    DocumentRecord,
    StructuredRecord,
    StructuredTable,
)

from ._blocks import NEWLINES as _NEWLINES
from ._blocks import Block, Lines, normalize, parse

BOM: Final = b"\xef\xbb\xbf"
FRONT_MATTER_WINDOW: Final = 64 * 1024
MAX_FINDING_RECORDS: Final = 1000  # ids a finding lists; its details hold the full count
MiB: Final = 1024 * 1024
EXTENSIONS: Final = (".markdown", ".md", ".mdown", ".mkd", ".mkdn")
# A Markdown name over text the bytes cannot tell from plain text: just above the generic band.
NAMED_TEXT: Final = GENERIC + 0.1
NAMED_DAMAGED: Final = NAME_ONLY + 0.1
_TEXT_CONTROLS: Final = frozenset(b"\t\n\x0b\x0c\r\x1b")
_BINARY_CONTROLS: Final = bytes(b for b in range(0x20) if b not in _TEXT_CONTROLS)
_CONTINUATION: Final = bytes(range(0x80, 0xC0))
_YAML_NULLS: Final = frozenset({"~", "null", "Null", "NULL"})
OBSERVED: Final = AssertionKind.OBSERVED

# Constructs plain text rarely has. A fence or a GFM delimiter row is enough alone; the others
# count when two kinds appear. List markers are never counted: YAML and plain notes use them.
# The probe runs on hostile bytes, so it reads each line once, left to right: no pattern here can
# backtrack over a line more than once, and a line is read only up to PROBE_LINE characters.
PROBE_LINE: Final = 4096
_ATX: Final = re.compile(r" {0,3}#{1,6}[ \t]+\S")
_SETEXT_UNDERLINE: Final = re.compile(r" {0,3}(?:=+|-{2,}) *")
_DELIMITER_CELL: Final = re.compile(r":?-{3,}:?")
_BLOCKQUOTE: Final = re.compile(r" {0,3}> ?\S")
_LINK_TARGET: Final = re.compile(r"[)\s]")
_STRONG_NAMES: Final = ("fenced code", "table delimiter row")
_WEAK_NAMES: Final = (
    "ATX heading",
    "setext heading",
    "link",
    "strong emphasis",
    "blockquote",
    "code span",
)
_CLOSING: Final = re.compile(r"(?:---|\.\.\.)[ \t]*")
_TITLE: Final = re.compile(r"title:(?=[ \t]|$)")

DESCRIPTOR: Final = AdapterDescriptor(
    id="markdown",
    version="0.1.0",
    abi=ABI_VERSION,
    summary="Markdown: CommonMark's blocks and GFM tables, each citing its exact span.",
    formats=(FormatSpec("Markdown", media_types=("text/markdown",), extensions=EXTENSIONS),),
    record_kinds=("document_block", "document_record", "structured_record", "structured_table"),
    config=(
        ConfigOption(
            "front_matter",
            True,
            "read a leading YAML front matter block for the title and keep it out of the blocks",
        ),
        ConfigOption(
            "max_document_bytes",
            8 * MiB,
            "a file holding more bytes than this is reported and its blocks are not parsed",
        ),
    ),
    libraries=(("markdown-it-py", version("markdown-it-py")),),
    finding_codes=(
        Documented(
            "markdown.invalid_utf8",
            "blocks whose bytes are not valid UTF-8 have Unknown text (unrepresentable, warning)",
        ),
        Documented(
            "markdown.link_definitions",
            "link reference definitions are blocks with an Unknown role: the model has no role for"
            " them (unrepresentable, info)",
        ),
        Documented(
            "markdown.nesting_limit",
            "containers nested past the parser's limit: their content is not parsed (limit, error)",
        ),
        Documented(
            "markdown.table_row_width",
            "a GFM table row writes more or fewer cells than its header: extra cells are not"
            " records, missing ones are Unknown (inconsistent, warning)",
        ),
        Documented(
            "markdown.title_unreadable",
            "the front matter's title is YAML this adapter does not read: it is Unknown"
            " (unrepresentable, warning)",
        ),
        Documented(
            "markdown.too_large",
            "the file holds more than max_document_bytes bytes; its blocks are not parsed"
            " (limit, error)",
        ),
    ),
    locator_steps=(),
    conventions=(
        Documented(
            "blocks",
            "CommonMark leaf blocks with text (paragraphs, headings, code, HTML, link reference"
            " definitions, GFM tables);"
            " spans per ADR 0038: content without container markers or heading markers",
        ),
        Documented("chunks", "chunk 0 holds the document; chunk 1 holds every block"),
        Documented(
            "front_matter",
            "a first line --- and a closing --- or ... line in the first 64 KiB; the title is a"
            " top-level title: key with a plain or quoted one-line value",
        ),
        Documented(
            "tables",
            "header row is row 0 and the table's header; body rows are records 1..n; cells split"
            " at unescaped pipes and trimmed; a cell's value unescapes \\| ; blank is Unknown;"
            " every row has the header's width (GFM: short rows padded, extra cells ignored)",
        ),
        Documented(
            "text",
            "the bytes after a leading UTF-8 BOM, decoded as UTF-8; spans count its code points,"
            " an invalid sequence counting as Python's errors='replace' U+FFFD",
        ),
    ),
    resources=Resources(max_memory=512 * MiB, streaming=False),
    security=(
        "Inline Markdown is never parsed, HTML never rendered, links never followed.",
        "Containers nest at most 64 deep; the whole file is bounded by max_document_bytes.",
    ),
)

# --- Decoding --------------------------------------------------------------------------------

_INVALID: Final[ContextVar[list[tuple[int, int]] | None]] = ContextVar(
    "neptune_markdown_invalid", default=None
)


def _mark_invalid(error: UnicodeError) -> tuple[str, int]:
    """``errors="replace"``, recording each invalid byte range it replaces."""
    if not isinstance(error, UnicodeDecodeError):
        raise error
    marks = _INVALID.get()
    if marks is not None:
        marks.append((error.start, error.end))
    return ("\ufffd", error.end)


codecs.register_error("neptune.markdown.mark_invalid", _mark_invalid)


def decode(data: bytes) -> tuple[str, list[int], list[int]]:
    """The text after a BOM, the code points that replace invalid bytes, and their byte offsets."""
    start = len(BOM) if data.startswith(BOM) else 0
    marks: list[tuple[int, int]] = []
    token = _INVALID.set(marks)
    try:
        text = data[start:].decode("utf-8", errors="neptune.markdown.mark_invalid")
    finally:
        _INVALID.reset(token)
    positions, cursor, code_points = [], start, 0
    for low, high in marks:
        code_points += len(data[cursor : start + low].translate(None, _CONTINUATION))
        positions.append(code_points)
        code_points += 1
        cursor = start + high
    return text, positions, [start + low for low, _ in marks]


def front_matter(head: bytes, whole: bool) -> tuple[int, list[tuple[int, str]]] | None:
    """The line index closing a leading front matter block, and its inner lines with offsets.

    ``head`` is the file's first ``FRONT_MATTER_WINDOW`` bytes, ``whole`` whether that is all of
    it; both chunks call this with the same bytes, so they agree. Lines end as CommonMark's do
    (LF, CRLF or CR); a line the window cuts is never a closing line. Offsets are code points of
    the text after a BOM, the same in the head as in the whole file before the cut.
    """
    text = head.removeprefix(BOM).decode("utf-8", errors="replace")
    starts = [0, *(match.end() for match in _NEWLINES.finditer(text))]
    lines = [
        (start, text[start:end].rstrip("\r\n"))
        for start, end in zip(starts, [*starts[1:], len(text)], strict=True)
    ]
    complete = len(lines) if whole else len(starts) - 1
    if not lines or lines[0][1].rstrip(" \t") != "---":
        return None
    inner: list[tuple[int, str]] = []
    for index, (offset, content) in enumerate(lines[1:complete], start=1):
        if _CLOSING.fullmatch(content):
            return index, inner
        inner.append((offset, content))
    return None


def _front_matter(
    source: SourceReader, data: bytes | None = None
) -> tuple[int, list[tuple[int, str]]] | None:
    head = (
        data[:FRONT_MATTER_WINDOW]
        if data is not None
        else source.read(0, min(source.size, FRONT_MATTER_WINDOW))
    )
    return front_matter(head, source.size <= FRONT_MATTER_WINDOW)


# --- The adapter ------------------------------------------------------------------------------


def _opens_fence(line: str) -> str | None:
    """The fence a line opens (its run of three or more backticks or tildes), else ``None``."""
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3 or stripped[:3] not in ("```", "~~~"):
        return None
    mark = stripped[0]
    return stripped[: len(stripped) - len(stripped.lstrip(mark))]


def _is_delimiter_row(line: str) -> bool:
    """A GFM delimiter row: two or more cells of ``---`` with optional colons, pipes between."""
    stripped = line.strip(" ")
    if len(line) - len(line.lstrip(" ")) > 3 or "|" not in stripped:
        return False
    cells = stripped.removeprefix("|").removesuffix("|").split("|")
    return len(cells) >= 2 and all(_DELIMITER_CELL.fullmatch(cell.strip(" ")) for cell in cells)


def _paragraph_line(line: str) -> bool:
    """Text a setext underline can follow: indented at most three spaces, then non-space."""
    stripped = line.lstrip(" ")
    return len(line) - len(stripped) <= 3 and stripped[:1].strip() != ""


def _has_link(line: str) -> bool:
    """``[label](target)`` within the line, the label non-empty, the target without spaces."""
    start, stop = 0, -1  # the nearest ``)`` or space at or after a target's first character
    while (close := line.find("](", start)) >= 0:
        start = close + 2
        opening = line.find("[", line.rfind("]", 0, close) + 1, close)
        if opening < 0 or close - opening < 2:
            continue
        if stop < start:
            found = _LINK_TARGET.search(line, start)
            stop = found.start() if found else len(line)
            if stop == len(line):
                return False  # nothing later closes it either
        if stop > start and line[stop] == ")":
            return True
    return False


def _has_strong(line: str) -> bool:
    """``**text**`` or ``__text__`` within the line, the text starting and ending in non-space."""
    for mark in ("**", "__"):
        start = 0
        while (open_ := line.find(mark, start)) >= 0:
            inner = open_ + 2
            if inner >= len(line) or line[inner].isspace():
                start = open_ + 1
                continue
            close = line.find(mark, inner + 2)  # two or more characters between the marks
            while close >= 0 and line[close - 1].isspace():
                close = line.find(mark, close + 1)
            return close >= 0  # no closing mark later serves any later opening either
    return False


def _has_code_span(line: str) -> bool:
    tick = line.find("`")
    while tick >= 0:
        close = line.find("`", tick + 1)
        if close < 0:
            return False
        if close > tick + 1:
            return True
        tick = close
    return False


def _probe_kinds(text: str) -> tuple[list[str], list[str]]:
    """The Markdown constructs in ``text`` (newlines as LF), strong kinds then weak, in order."""
    found: set[str] = set()
    fence: str | None = None
    previous = ""
    for raw in text.split("\n"):
        line = raw[:PROBE_LINE]
        if fence is not None:
            if line.lstrip(" ").startswith(fence) and len(line) - len(line.lstrip(" ")) <= 3:
                found.add("fenced code")
                fence = None
        elif "fenced code" not in found:
            fence = _opens_fence(line)
        if "table delimiter row" not in found and _is_delimiter_row(line):
            found.add("table delimiter row")
        if _ATX.match(line):
            found.add("ATX heading")
        if _SETEXT_UNDERLINE.fullmatch(line) and _paragraph_line(previous):
            found.add("setext heading")
        if _BLOCKQUOTE.match(line):
            found.add("blockquote")
        for name, test in (
            ("link", _has_link),
            ("strong emphasis", _has_strong),
            ("code span", _has_code_span),
        ):
            if name not in found and test(line):
                found.add(name)
        previous = line
    return ([n for n in _STRONG_NAMES if n in found], [n for n in _WEAK_NAMES if n in found])


class MarkdownAdapter:
    """The Markdown adapter. It plans two chunks whatever the file's size."""

    descriptor = DESCRIPTOR

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        named = hints.name.lower().endswith(EXTENSIONS)
        reasons: list[ProbeReason] = []
        if named:
            reasons.append(ProbeReason("markdown.name", f"the name {hints.name!r} says Markdown"))
        if not head:
            return ProbeResult(NAMED_DAMAGED if named else 0.0, (*reasons,))
        if b"\x00" in head or len(head.translate(None, _BINARY_CONTROLS)) != len(head):
            return ProbeResult(0.0, (ProbeReason("markdown.binary", "the head is not text"),))
        try:
            decoder = codecs.getincrementaldecoder("utf-8")()
            text = decoder.decode(head.removeprefix(BOM), final=len(head) == hints.size)
        except UnicodeDecodeError:
            reasons.append(ProbeReason("markdown.not_utf8", "the head is damaged UTF-8 text"))
            return ProbeResult(NAMED_DAMAGED if named else 0.0, tuple(reasons))
        strong, weak = _probe_kinds(_NEWLINES.sub("\n", text))
        if strong or len(weak) >= 2:
            found = ", ".join([*strong, *weak])
            reasons.append(ProbeReason("markdown.syntax", f"the head has Markdown syntax: {found}"))
            return ProbeResult(STRUCTURE, tuple(reasons))
        if named:
            reasons.append(ProbeReason("markdown.text", "UTF-8 text with no distinctive syntax"))
            return ProbeResult(NAMED_TEXT, tuple(reasons))
        return ProbeResult(0.0, (ProbeReason("markdown.no_syntax", "no Markdown syntax in head"),))

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        head = source.read(0, min(source.size, PROBE_HEAD_SIZE))
        return InspectResult(
            {
                "bom": head.startswith(BOM),
                "front_matter": _front_matter(source) is not None,
                "head_lines": head.count(b"\n"),
                "size": source.size,
            }
        )

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        document = make_chunk(
            source, config, {"part": "document"}, min(source.size, FRONT_MATTER_WINDOW)
        )
        blocks = make_chunk(source, config, {"part": "blocks"}, source.size)
        return Plan((document, blocks))

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        out = _Output(source, config)
        if chunk.context["part"] == "document":
            _document(out)
        else:
            _blocks(out)
        return ChunkOutput(records=tuple(out.records), findings=tuple(out.findings))


class _Output:
    def __init__(self, source: SourceReader, config: AdapterConfig) -> None:
        self.source = source
        self.config = config
        self.records: list[EvidenceRecord] = []
        self.findings: list[IngestFinding] = []
        self.whole = EvidenceRef(source.content_id, (ByteRange(0, source.size),))
        self.document = evidence_record_id(DocumentRecord.kind, self.whole, config.transform)

    def cite(self, start: int, end: int) -> Provenance:
        evidence = EvidenceRef(self.source.content_id, (Span(start, end),))
        return Provenance(evidence, self.config.transform.id, OBSERVED)

    def record_id(self, kind: str, provenance: Provenance) -> RecordId:
        return evidence_record_id(kind, provenance.evidence, self.config.transform)

    def finding(
        self,
        code: str,
        category: FindingCategory,
        severity: Severity,
        message: str,
        details: dict[str, JsonValue],
        records: list[RecordId] | None = None,
    ) -> None:
        self.findings.append(
            ingest_finding(
                code=code,
                category=category,
                severity=severity,
                subject=self.whole,
                transform=self.config.transform,
                message=message,
                details=details,
                records=(records or [])[:MAX_FINDING_RECORDS],
            )
        )


def _document(out: _Output) -> None:
    title: Knowledge[str] = Unknown()
    found = _front_matter(out.source) if out.config.flag("front_matter") else None
    if found is not None:
        title = _title(out, found[1])
    out.records.append(
        DocumentRecord(
            id=out.document,
            provenance=Provenance(out.whole, out.config.transform.id, OBSERVED),
            format="markdown",
            title=title,
            pages=(),
        )
    )


def _title(out: _Output, lines: list[tuple[int, str]]) -> Knowledge[str]:
    """The front matter's top-level ``title``: a plain or quoted one-line scalar."""
    for position, (offset, line) in enumerate(lines):
        match = _TITLE.match(line)
        if match is None:
            continue
        raw = line[match.end() :].strip(" \t")
        start = offset + line.index(raw, match.end()) if raw else offset + match.end()
        cited = out.cite(start, start + len(raw))
        following = lines[position + 1][1] if position + 1 < len(lines) else ""
        if not raw or raw.startswith("#"):
            if following[:1] in (" ", "\t"):
                return _unreadable(out, cited)
            return Unknown(cited)
        if raw in _YAML_NULLS:
            return KnownAbsent(cited)
        if raw[0] == '"':
            if len(raw) < 2 or raw[-1] != '"' or "\\" in raw[1:-1] or '"' in raw[1:-1]:
                return _unreadable(out, cited)
            value = raw[1:-1]
        elif raw[0] == "'":
            inner = raw[1:-1]
            if len(raw) < 2 or raw[-1] != "'" or "'" in inner.replace("''", ""):
                return _unreadable(out, cited)
            value = inner.replace("''", "'")
        elif raw[0] in "|>[{&*!%@`":
            return _unreadable(out, cited)
        else:
            comment = raw.find(" #")
            value = (raw if comment < 0 else raw[:comment]).rstrip(" \t")
        return Known(value, cited) if value.strip() else Unknown(cited)
    return Unknown()


def _unreadable(out: _Output, cited: Provenance) -> Knowledge[str]:
    out.findings.append(
        ingest_finding(
            code="markdown.title_unreadable",
            category=FindingCategory.UNREPRESENTABLE,
            severity=Severity.WARNING,
            subject=cited.evidence,
            transform=out.config.transform,
            message="the front matter's title is YAML this adapter does not read; it is unknown",
            details={},
        )
    )
    return Unknown(cited)


def _blocks(out: _Output) -> None:
    source, config = out.source, out.config
    limit = config.integer("max_document_bytes")
    if source.size > limit:
        out.finding(
            "markdown.too_large",
            FindingCategory.LIMIT,
            Severity.ERROR,
            f"the file holds {source.size} bytes, over max_document_bytes ({limit});"
            " its blocks are not parsed",
            {"bytes": source.size, "max_document_bytes": limit},
        )
        return
    data = b"".join(read_pieces(source, 0, source.size))
    text, invalid, invalid_bytes = decode(data)
    normalized = normalize(text)
    found = _front_matter(source, data) if config.flag("front_matter") else None
    if found is not None:
        closing = found[0]
        lines = normalized.split("\n")
        normalized = "\n".join(["" for _ in lines[: closing + 1]] + lines[closing + 1 :])
    lines_map = Lines(text, normalized)
    parsed = parse(normalized)
    damaged: list[RecordId] = []
    definitions: list[RecordId] = []
    resized: list[tuple[RecordId, int, int]] = []  # (row, cells it wrote, header's cells)

    def bad(start: int, end: int) -> bool:
        index = bisect_left(invalid, start)
        return index < len(invalid) and invalid[index] < end

    for order, block in enumerate(parsed.blocks):
        start, end = lines_map.to_source(block.start), lines_map.to_source(block.end)
        cited = out.cite(start, end)
        block_id = out.record_id(DocumentBlock.kind, cited)
        text_value: Knowledge[str] = Known(text[start:end])
        if bad(start, end):
            text_value = Unknown()
            damaged.append(block_id)
        role, level = _role(block)
        out.records.append(
            DocumentBlock(
                id=block_id,
                provenance=cited,
                document=out.document,
                order=order,
                role=role,
                level=level,
                text=text_value,
                region=NotApplicable(),
            )
        )
        if block.definition:
            definitions.append(block_id)
        if block.role is BlockRole.TABLE and block.rows:
            _table(out, block, cited, lines_map, text, bad, resized)
    if resized:
        out.finding(
            "markdown.table_row_width",
            FindingCategory.INCONSISTENT,
            Severity.WARNING,
            f"{len(resized)} table row(s) hold a different number of cells than their header;"
            " GFM pads short rows with blank cells (unknown) and ignores the extra ones",
            {
                "rows": len(resized),
                "padded": sum(wrote < width for _, wrote, width in resized),
                "cut": sum(wrote > width for _, wrote, width in resized),
            },
            [row for row, _, _ in resized],
        )
    if definitions:
        out.finding(
            "markdown.link_definitions",
            FindingCategory.UNREPRESENTABLE,
            Severity.INFO,
            f"{len(definitions)} link reference definition(s) are blocks with an unknown role;"
            " the canonical model has no role for them",
            {"blocks": len(definitions)},
            definitions,
        )
    if damaged:
        out.finding(
            "markdown.invalid_utf8",
            FindingCategory.UNREPRESENTABLE,
            Severity.WARNING,
            f"{len(damaged)} block(s) hold bytes that are not valid UTF-8, the first at byte"
            f" {invalid_bytes[0]}; their text is unknown",
            {"blocks": len(damaged), "first_invalid_byte": invalid_bytes[0]},
            damaged,
        )
    for first, last in parsed.nesting_cut:
        out.finding(
            "markdown.nesting_limit",
            FindingCategory.LIMIT,
            Severity.ERROR,
            f"lines {first} to {last - 1} nest containers past the parser's limit; their content"
            " is not parsed",
            {"first_line": first, "last_line": last - 1},
        )


def _role(block: Block) -> tuple[Knowledge[BlockRole], Knowledge[int]]:
    if block.role is None:
        return Unknown(), Unknown()
    if block.leveled:
        return Known(block.role), Known(block.level) if block.level else Unknown()
    return Known(block.role), NotApplicable()


def _table(
    out: _Output,
    block: Block,
    cited: Provenance,
    lines_map: Lines,
    text: str,
    bad: Callable[[int, int], bool],
    resized: list[tuple[RecordId, int, int]],
) -> None:
    table_id = out.record_id(StructuredTable.kind, cited)
    header_row = block.rows[0]
    header_start = lines_map.to_source(header_row.start)
    header_end = lines_map.to_source(header_row.end)
    header: Knowledge[tuple[str, ...]] = Known(
        tuple(cell.value for cell in header_row.cells), out.cite(header_start, header_end)
    )
    if bad(header_start, header_end):
        header = Unknown(out.cite(header_start, header_end))
    out.records.append(
        StructuredTable(id=table_id, provenance=cited, name=NotCovered(), header=header)
    )
    for row_number, row in enumerate(block.rows[1:], start=1):
        row_cited = out.cite(lines_map.to_source(row.start), lines_map.to_source(row.end))
        cells: list[Knowledge[CellValue]] = []
        width = len(header_row.cells)
        for cell in row.cells[:width]:
            start, end = lines_map.to_source(cell.start), lines_map.to_source(cell.end)
            cell_cited = out.cite(start, end)
            if not cell.value.strip() or bad(start, end):
                cells.append(Unknown(cell_cited))
            else:
                cells.append(Known(cell.value, cell_cited))
        row_end = lines_map.to_source(row.end)
        # GFM: a short row is padded with blank cells (here Unknown, at the row's end)
        cells.extend(Unknown(out.cite(row_end, row_end)) for _ in range(width - len(cells)))
        row_id = out.record_id(StructuredRecord.kind, row_cited)
        if len(row.cells) != width:
            resized.append((row_id, len(row.cells), width))
        out.records.append(
            StructuredRecord(
                id=row_id,
                provenance=row_cited,
                table=table_id,
                row=row_number,
                cells=tuple(cells),
            )
        )

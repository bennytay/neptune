"""Plain UTF-8 text: Neptune's reference adapter (ADR 0024). Copy this package to start one.

What it emits for a file:

- one ``DocumentRecord`` citing all of the file's bytes: format ``text``, title ``NotCovered``
  (plain text has no place for one), no pages;
- one ``DocumentBlock`` per block, in file order, citing its exact ``Span`` of the text.

The rules, also in the descriptor's conventions:

- **Text.** The file's bytes after a leading UTF-8 byte-order mark, decoded as UTF-8. ``Span``
  offsets count its code points; an invalid byte sequence counts as the U+FFFD characters
  Python's ``errors="replace"`` decoding gives it (one per maximal invalid subpart).
- **Lines** end at LF. A CR directly before the LF belongs to the line ending; any other CR is
  text. A line holding only spaces, tabs and CRs is blank.
- **Blocks** (option ``block_rule``): ``paragraph`` makes each maximal run of non-blank lines a
  block, ``line`` each non-blank line. A block's span runs from its first line's start to its last
  line's end, line ending excluded, so its text keeps the line endings inside it.
- A block's ``text`` is its span's text exactly. If its bytes are not valid UTF-8 it is
  ``Unknown`` with a ``text.invalid_utf8`` finding: replacement characters are never stored.
  ``role`` and ``level`` are ``NotCovered`` (plain text declares neither) and ``region`` is
  ``NotApplicable`` (the document has no pages).
- A block longer than ``max_block_bytes`` is not decoded: ``text.block_too_large``, and its place
  in the order is left empty. This bounds the memory one chunk needs.

Probing never decides from the name. A head that decodes as UTF-8 is ``GENERIC`` text; one with
an invalid sequence but no NUL or binary control byte is damaged text, ``NAME_ONLY``, so it is still
read when nothing else claims it; a NUL or a binary control byte means it is not text.

Planning reads the file once, streaming, to find the blocks and the code-point offset of each.
Chunk 0 emits the document; every later chunk holds whole blocks, about ``chunk_bytes`` of them,
and carries the offsets it starts from. The records never depend on where chunks are cut.
"""

import codecs
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from typing import Final

from neptune.adapters.contract import (
    ABI_VERSION,
    GENERIC,
    NAME_ONLY,
    PROBE_HEAD_SIZE,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkExtent,
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
from neptune.identity.provenance import evidence_record_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonObject
from neptune.model.knowledge import (
    AssertionKind,
    Knowledge,
    Known,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, Span
from neptune.model.world import DocumentBlock, DocumentRecord

BOM: Final = b"\xef\xbb\xbf"
DEFAULT_CHUNK_BYTES: Final = 1024 * 1024
# C0 controls that plain text uses: tab, LF, VT, FF, CR, and ESC for terminal colours in logs.
_TEXT_CONTROLS: Final = frozenset(b"\t\n\x0b\x0c\r\x1b")
_BINARY_CONTROLS: Final = bytes(b for b in range(0x20) if b not in _TEXT_CONTROLS)
_BLANK: Final = b" \t\r"

DESCRIPTOR: Final = AdapterDescriptor(
    id="text",
    version="0.1.0",
    abi=ABI_VERSION,
    summary="Plain UTF-8 text: a document, and a block per paragraph or line citing its span.",
    formats=(FormatSpec("Plain text", media_types=("text/plain",), extensions=(".text", ".txt")),),
    record_kinds=("document_block", "document_record"),
    config=(
        ConfigOption(
            "block_rule",
            "paragraph",
            "paragraph: each run of non-blank lines is a block; line: each non-blank line is",
            choices=("line", "paragraph"),
        ),
        ConfigOption(
            "max_block_bytes",
            1024 * 1024,
            "a block holding more bytes than this is reported and not decoded",
        ),
    ),
    libraries=(),
    finding_codes=(
        Documented(
            "text.block_too_large",
            "a block holds more than max_block_bytes bytes; it has no record (limit, error)",
        ),
        Documented(
            "text.invalid_utf8",
            "a block's bytes are not valid UTF-8; its text is Unknown (unrepresentable, warning)",
        ),
    ),
    locator_steps=(),
    conventions=(
        Documented(
            "blocks",
            "paragraph: maximal runs of non-blank lines; line: non-blank lines. A span runs from"
            " the first line's start to the last line's end, line ending excluded",
        ),
        Documented(
            "chunks",
            "chunk 0 holds the document; each other chunk holds whole blocks and its context"
            " gives start and end bytes, the code point and block order it starts at",
        ),
        Documented(
            "lines",
            "lines end at LF; a CR directly before the LF is part of the ending; a line of only"
            " spaces, tabs and CRs is blank",
        ),
        Documented(
            "text",
            "the bytes after a leading UTF-8 BOM, decoded as UTF-8; spans count its code points,"
            " an invalid sequence counting as Python's errors='replace' U+FFFDs",
        ),
    ),
    resources=Resources(max_memory=64 * 1024 * 1024, streaming=True),
    security=(
        "Decodes UTF-8 only and never guesses another encoding.",
        "Holds one chunk in memory: at most chunk_bytes or max_block_bytes of source.",
    ),
    extent=ChunkExtent(),  # data chunks name their [start, end) bytes (ADR 0069)
)


@dataclass(frozen=True)
class _Line:
    """A line's bytes ``[start, end)`` and code points ``[cp_start, cp_end)``, ending excluded."""

    start: int
    end: int
    cp_start: int
    cp_end: int
    blank: bool


@dataclass(frozen=True)
class _Block:
    order: int
    start: int
    end: int
    cp_start: int
    cp_end: int


def _lines(pieces: Iterable[bytes], start: int, code_point: int) -> Iterator[_Line]:
    """The lines of consecutive bytes beginning at byte ``start`` and code point ``code_point``.

    Streams: memory is bounded by the largest piece, whatever a line's length. An LF always ends
    any pending invalid sequence, so counting lines piece by piece gives the same code points as
    decoding the whole text at once.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    offset, cp = start, code_point
    line_start, line_cp, blank, last = start, code_point, True, b""
    for piece in pieces:
        position = 0
        while position < len(piece):
            newline = piece.find(b"\n", position)
            stop = len(piece) if newline < 0 else newline + 1
            segment = piece[position:stop]
            cp += len(decoder.decode(segment))
            content = segment[:-1] if newline >= 0 else segment
            blank = blank and not content.strip(_BLANK)
            if newline >= 0:
                end = offset + position + len(content)
                # The CR before this LF may have ended the previous piece.
                crlf = (content[-1:] or last) == b"\r"
                ending = 2 if crlf else 1
                yield _Line(line_start, end - (ending - 1), line_cp, cp - ending, blank)
                line_start, line_cp, blank, last = end + 1, cp, True, b""
            elif segment:
                last = segment[-1:]
            position = stop
        offset += len(piece)
    cp += len(decoder.decode(b"", final=True))
    if offset > line_start:
        yield _Line(line_start, offset, line_cp, cp, blank)


def _blocks(lines: Iterable[_Line], rule: str, order: int) -> Iterator[_Block]:
    """Group lines into blocks by ``rule``, numbering them from ``order``."""
    current: _Block | None = None
    for line in lines:
        if line.blank:
            if current is not None:
                yield current
                order, current = order + 1, None
        elif rule == "line":
            yield _Block(order, line.start, line.end, line.cp_start, line.cp_end)
            order += 1
        elif current is None:
            current = _Block(order, line.start, line.end, line.cp_start, line.cp_end)
        else:
            current = replace(current, end=line.end, cp_end=line.cp_end)
    if current is not None:
        yield current


def _int(context: JsonObject, key: str) -> int:
    value = context[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"chunk context {key} must be an integer, got {value!r}")
    return value


def _text_start(source: SourceReader) -> int:
    return len(BOM) if source.read(0, len(BOM)) == BOM else 0


def _document(source: SourceReader, config: AdapterConfig) -> tuple[RecordId, Provenance]:
    evidence = EvidenceRef(source.content_id, (ByteRange(0, source.size),))
    transform = config.transform
    return (
        evidence_record_id(DocumentRecord.kind, evidence, transform),
        Provenance(evidence, transform.id, AssertionKind.OBSERVED),
    )


def _finding(
    source: SourceReader,
    config: AdapterConfig,
    code: str,
    category: FindingCategory,
    severity: Severity,
    block: _Block,
    message: str,
    details: dict[str, int],
    records: tuple[RecordId, ...] = (),
) -> IngestFinding:
    return ingest_finding(
        code=code,
        category=category,
        severity=severity,
        subject=EvidenceRef(source.content_id, (ByteRange(block.start, block.end - block.start),)),
        transform=config.transform,
        message=message,
        details={"block": block.order, **details},
        records=records,
    )


class TextAdapter:
    """The plain-text adapter. ``chunk_bytes`` sets planning granularity and never the output."""

    descriptor = DESCRIPTOR

    def __init__(self, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> None:
        if chunk_bytes <= 0:
            raise ValueError(f"chunk_bytes must be positive: {chunk_bytes}")
        self._chunk_bytes = chunk_bytes

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        if not head:
            reason = ProbeReason("text.empty", "the source is empty, which any text may be")
            return ProbeResult(NAME_ONLY, (reason,))
        if b"\x00" in head:
            return ProbeResult(0.0, (ProbeReason("text.nul", "the head holds a NUL byte"),))
        if len(head.translate(None, _BINARY_CONTROLS)) != len(head):
            reason = ProbeReason("text.control", "the head holds control bytes text does not use")
            return ProbeResult(0.0, (reason,))
        reasons: list[ProbeReason] = []
        if head.startswith(BOM):
            reasons.append(ProbeReason("text.bom", "the source starts with a UTF-8 BOM"))
        try:
            # A head cut short of the source may end inside a character: decode it as unfinished.
            decoder = codecs.getincrementaldecoder("utf-8")()
            decoder.decode(head, final=len(head) == hints.size)
        except UnicodeDecodeError as exc:
            # Damaged text, not binary: read it, and the damaged blocks become findings.
            message = f"byte {exc.start} of the head is not UTF-8; read as damaged text"
            return ProbeResult(NAME_ONLY, (*reasons, ProbeReason("text.not_utf8", message)))
        reasons.insert(0, ProbeReason("text.utf8", f"the first {len(head)} bytes are UTF-8 text"))
        return ProbeResult(GENERIC, tuple(reasons))

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        head = source.read(0, min(source.size, PROBE_HEAD_SIZE))
        return InspectResult(
            {"bom": head.startswith(BOM), "head_lines": head.count(b"\n"), "size": source.size}
        )

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        rule, limit = config.text("block_rule"), config.integer("max_block_bytes")
        start = _text_start(source)
        lines = _lines(read_pieces(source, start, source.size), start, 0)
        chunks: list[Chunk] = [make_chunk(source, config, {"part": "document"}, 0)]
        findings: list[IngestFinding] = []
        held: list[_Block] = []

        def close() -> None:
            if held:
                first, last = held[0], held[-1]
                context: JsonObject = {
                    "code_point": first.cp_start,
                    "end": last.end,
                    "order": first.order,
                    "part": "blocks",
                    "start": first.start,
                }
                chunks.append(make_chunk(source, config, context, last.end - first.start))
                held.clear()

        for block in _blocks(lines, rule, 0):
            size = block.end - block.start
            if size > limit:
                close()
                findings.append(
                    _finding(
                        source,
                        config,
                        "text.block_too_large",
                        FindingCategory.LIMIT,
                        Severity.ERROR,
                        block,
                        f"block {block.order} holds {size} bytes, over max_block_bytes ({limit});"
                        " it is not decoded",
                        {"bytes": size, "max_block_bytes": limit},
                    )
                )
                continue
            if held and block.end - held[0].start > self._chunk_bytes:
                close()
            held.append(block)
        close()
        return Plan(tuple(chunks), tuple(findings))

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        document, provenance = _document(source, config)
        context = chunk.context
        if context["part"] == "document":
            title: Knowledge[str] = NotCovered()
            record = DocumentRecord(document, provenance, "text", title, ())
            return ChunkOutput(records=(record,))
        start, end = _int(context, "start"), _int(context, "end")
        code_point, order = _int(context, "code_point"), _int(context, "order")
        data = source.read(start, end - start)
        records: list[DocumentBlock] = []
        findings: list[IngestFinding] = []
        rule = config.text("block_rule")
        for block in _blocks(_lines((data,), start, code_point), rule, order):
            evidence = EvidenceRef(source.content_id, (Span(block.cp_start, block.cp_end),))
            block_id = evidence_record_id(DocumentBlock.kind, evidence, config.transform)
            raw = data[block.start - start : block.end - start]
            text: Knowledge[str]
            try:
                text = Known(raw.decode("utf-8"))
            except UnicodeDecodeError as exc:
                text = Unknown()
                findings.append(
                    _finding(
                        source,
                        config,
                        "text.invalid_utf8",
                        FindingCategory.UNREPRESENTABLE,
                        Severity.WARNING,
                        block,
                        f"block {block.order} is not valid UTF-8 from byte"
                        f" {block.start + exc.start}; its text is unknown",
                        {"first_invalid_byte": block.start + exc.start},
                        (block_id,),
                    )
                )
            records.append(
                DocumentBlock(
                    id=block_id,
                    provenance=Provenance(evidence, config.transform.id, AssertionKind.OBSERVED),
                    document=document,
                    order=block.order,
                    role=NotCovered(),
                    level=NotCovered(),
                    text=text,
                    region=NotApplicable(),
                )
            )
        return ChunkOutput(records=tuple(records), findings=tuple(findings))

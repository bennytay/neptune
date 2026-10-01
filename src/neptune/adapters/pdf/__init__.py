"""PDF documents, layout preserved: pages, blocks in declared reading order, spans and regions.

What it emits for a file (ADR 0038):

- one ``DocumentRecord`` citing all of the file's bytes: format ``pdf``, the ``/Info`` ``/Title``,
  and every page with its declared label (``/PageLabels``), its size (``/MediaBox``, default user
  space) and its ``/Rotate``, never applied;
- one ``DocumentBlock`` per block of a page (``_page``), in reading order, citing
  ``[Page(p), Span(start, end)]`` in that page's extracted text, with the box it is drawn in as
  ``[Page(p), PageRegion(...)]``;
- for each table a tagged PDF declares, one ``StructuredTable`` citing its structure element and
  one ``StructuredRecord`` per row that is not the header, each cell citing its span.

Reading order is the file's own: the content stream's order on an untagged page, the structure
tree's on a tagged one. Roles and levels are what tags declare; an untagged page's text has
``Unknown`` roles (a heading guessed from a font size is derived, never here). Nothing is OCRed
and nothing runs: JavaScript and embedded files are reported, never executed or opened.

Chunks: chunk 0 is the document; every later chunk holds ``pages_per_chunk`` whole pages, so a
hostile page costs at most its chunk. ``order`` is ``page * 2^24 + position on the page``: it
sorts blocks in reading order whatever the chunking, with gaps between pages.
"""

import math
from collections.abc import Sequence
from importlib.metadata import version
from typing import Final

from pypdf.errors import LimitReachedError
from pypdf.generic import DictionaryObject, TextStringObject

from neptune.adapters.contract import (
    ABI_VERSION,
    PROBE_HEAD_SIZE,
    SIGNATURE,
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
    ShortReadError,
    SourceReader,
    make_chunk,
)
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import EvidenceRecord, evidence_record_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    AssertionKind,
    Knowledge,
    Known,
    NotApplicable,
    Unknown,
)
from neptune.model.provenance import (
    AdapterLocator,
    ByteRange,
    EvidenceRef,
    Locator,
    Page,
    PageRegion,
    Provenance,
    Span,
    adapter_locator,
)
from neptune.model.world import (
    BlockRole,
    CellValue,
    DocumentBlock,
    DocumentPage,
    DocumentRecord,
    StructuredRecord,
    StructuredTable,
)

from ._content import Interpreter, PageContent
from ._objects import (
    array,
    dictionary,
    entry,
    integer,
    name,
    number,
    reference,
    resolve,
)
from ._page import Block, blocks
from ._reader import (
    Opened,
    Unreadable,
    Warnings,
    open_document,
    page_list,
    pypdf_session,
)
from ._structure import Key, PageStructure, Structure

HEADER: Final = b"%PDF-"
HEADER_WINDOW: Final = 1024
STRIDE: Final = 1 << 24
DEFAULT_PAGES_PER_CHUNK: Final = 8
MiB: Final = 1024 * 1024
MAX_ANNOTATIONS: Final = 100_000
MAX_NAME_TREE_VISITS: Final = 100_000
OBSERVED: Final = AssertionKind.OBSERVED

DESCRIPTOR: Final = AdapterDescriptor(
    id="pdf",
    version="0.1.0",
    abi=ABI_VERSION,
    summary="PDF: pages, blocks in declared reading order with exact spans and regions, tables.",
    formats=(
        FormatSpec(
            "PDF",
            media_types=("application/pdf",),
            extensions=(".pdf",),
            magic=(Magic(0, HEADER),),
        ),
    ),
    record_kinds=("document_block", "document_record", "structured_record", "structured_table"),
    config=(
        ConfigOption(
            "max_page_content_bytes",
            16 * MiB,
            "a page whose content streams decode to more bytes than this is read no further",
        ),
        ConfigOption(
            "max_page_operations",
            1_000_000,
            "a page is read for at most this many content operators, its forms' included",
        ),
        ConfigOption(
            "max_stream_bytes",
            64 * MiB,
            "no stream of the file is inflated past this many bytes",
        ),
        ConfigOption(
            "space_threshold",
            200,
            "a gap of at least this many thousandths of an em between two strings is a space",
        ),
    ),
    libraries=(("pypdf", version("pypdf")),),
    finding_codes=(
        Documented(
            "pdf.content_limit",
            "a page's content passed max_page_content_bytes, max_page_operations or"
            " max_stream_bytes; it was read up to there (limit, error)",
        ),
        Documented(
            "pdf.content_skipped",
            "operators of a page were skipped: wrong operands, a missing font or XObject"
            " (corrupt, warning)",
        ),
        Documented(
            "pdf.content_unreadable",
            "a page's content streams could not be decoded or parsed (corrupt, error)",
        ),
        Documented(
            "pdf.embedded_files",
            "the document embeds files; they are not opened (skipped, warning)",
        ),
        Documented(
            "pdf.encrypted",
            "the document is encrypted and needs a password or AES: its pages are listed, its"
            " text is not read (unsupported, error)",
        ),
        Documented(
            "pdf.geometry_unknown",
            "blocks whose fonts declare no widths or ascent have an Unknown region"
            " (unrepresentable, warning)",
        ),
        Documented(
            "pdf.javascript",
            "the document declares JavaScript; Neptune never runs it (skipped, info)",
        ),
        Documented(
            "pdf.page_repaired",
            "pypdf repaired objects while reading a page (corrupt, warning)",
        ),
        Documented(
            "pdf.repaired",
            "the file's cross-reference structure or trailer was rebuilt or repaired to read it"
            " (corrupt, warning)",
        ),
        Documented(
            "pdf.structure_unusable",
            "a page's tags could not be read; the page is read as untagged (corrupt, warning)",
        ),
        Documented(
            "pdf.table_incomplete",
            "a tagged table's cells or rows could not be placed on one page: those cells are"
            " Unknown, or the table has no records (unrepresentable, warning)",
        ),
        Documented(
            "pdf.unmapped_glyphs",
            "codes that no ToUnicode map or encoding gives text: those blocks' text is Unknown"
            " (unrepresentable, warning)",
        ),
        Documented(
            "pdf.unreadable",
            "the file cannot be opened as a PDF, even repaired, or its page tree cannot be"
            " walked (corrupt, error)",
        ),
        Documented(
            "pdf.value_unreadable",
            "a declared value (title, page labels, media box, rotation) has the wrong type; it is"
            " Unknown (corrupt, warning)",
        ),
    ),
    locator_steps=(
        Documented(
            "pdf:object",
            "the indirect object number/generation, as the file's last cross-reference section"
            " resolves it: where a title or page labels are declared",
        ),
        Documented(
            "pdf:structure",
            "a structure element by path: child indices from the StructTreeRoot, '/'-separated",
        ),
    ),
    conventions=(
        Documented(
            "blocks",
            "untagged: one block per text-showing operator, one figure block per image painted;"
            " tagged: one block per owning structure element per page (a table is one block),"
            " one per Artifact sequence, one per untagged run",
        ),
        Documented(
            "chunks",
            "chunk 0 holds the document; each other chunk holds whole pages: its context gives"
            " the first page and the page count",
        ),
        Documented(
            "coordinates",
            "PageRegion and page sizes are in default user space as stored: points, /Rotate"
            " not applied, no shift to the crop box, UserUnit not applied, rounded to 0.001",
        ),
        Documented(
            "order",
            "page index x 2^24 + the block's position on its page; tagged pages in structure"
            " order, then artifacts and untagged content in content order",
        ),
        Documented(
            "text",
            "a page's text is its blocks' texts each followed by LF; runs join with LF on a new"
            " line, one space for a gap of space_threshold or more; table cells by TAB, rows by"
            " LF; a block with no text is U+FFFC; unmapped codes are U+FFFD",
        ),
    ),
    resources=Resources(max_memory=1024 * MiB, streaming=False),
    security=(
        "Parsed with pypdf in pure Python under its stream, page-tree and recursion bounds.",
        "JavaScript, actions and embedded files are reported and never run or opened.",
        "AES is never decrypted; RC4 only with the empty user password, in pure Python.",
        "No external decoder (jbig2dec) is ever started; images are never decoded.",
    ),
)


def _object(ref: tuple[int, int]) -> AdapterLocator:
    return adapter_locator("pdf:object", {"generation": ref[1], "number": ref[0]})


def _structure(path: Sequence[int]) -> AdapterLocator:
    return adapter_locator("pdf:structure", {"path": "/".join(str(step) for step in path)})


def _coordinate(value: float) -> float:
    return round(value, 3) + 0.0


class _Output:
    """Records and findings one chunk emits, with the helpers that build them."""

    def __init__(self, source: SourceReader, config: AdapterConfig) -> None:
        self.source = source
        self.config = config
        self.records: list[EvidenceRecord] = []
        self.findings: list[IngestFinding] = []
        whole = EvidenceRef(source.content_id, (ByteRange(0, source.size),))
        self.whole = whole
        self.document = evidence_record_id(DocumentRecord.kind, whole, config.transform)

    def evidence(self, *locator: Locator) -> EvidenceRef:
        return EvidenceRef(self.source.content_id, tuple(locator))

    def provenance(self, *locator: Locator) -> Provenance:
        return Provenance(self.evidence(*locator), self.config.transform.id, OBSERVED)

    def record_id(self, kind: str, evidence: EvidenceRef) -> RecordId:
        return evidence_record_id(kind, evidence, self.config.transform)

    def finding(
        self,
        code: str,
        category: FindingCategory,
        severity: Severity,
        subject: EvidenceRef,
        message: str,
        details: dict[str, JsonValue],
        records: Sequence[RecordId] = (),
    ) -> None:
        self.findings.append(
            ingest_finding(
                code=code,
                category=category,
                severity=severity,
                subject=subject,
                transform=self.config.transform,
                message=message,
                details=details,
                records=records,
            )
        )

    def output(self) -> ChunkOutput:
        return ChunkOutput(records=tuple(self.records), findings=tuple(self.findings))


def _header_version(head: bytes) -> tuple[int, str | None] | None:
    at = head[:HEADER_WINDOW].find(HEADER)
    if at < 0:
        return None
    tail = head[at + len(HEADER) : at + len(HEADER) + 8]
    digits = tail.split(maxsplit=1)[0] if tail.split() else b""
    text = digits.decode("ascii", errors="replace")
    valid = len(text) == 3 and text[0].isdigit() and text[1] == "." and text[2].isdigit()
    return at, text if valid else None


class PdfAdapter:
    """The PDF adapter. ``pages_per_chunk`` sets planning granularity and never the output."""

    descriptor = DESCRIPTOR

    def __init__(self, pages_per_chunk: int = DEFAULT_PAGES_PER_CHUNK) -> None:
        if pages_per_chunk <= 0:
            raise ValueError(f"pages_per_chunk must be positive: {pages_per_chunk}")
        self._pages_per_chunk = pages_per_chunk

    # --- probe and inspect ------------------------------------------------------------------

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        found = _header_version(head)
        if found is None:
            return ProbeResult(0.0, (ProbeReason("pdf.no_header", "no %PDF- header in the head"),))
        at, declared = found
        if at == 0:
            reason = ProbeReason("pdf.header", "the source starts with a %PDF- header")
        else:
            reason = ProbeReason("pdf.header_offset", f"a %PDF- header after {at} other bytes")
        return ProbeResult(SIGNATURE, (reason,), declared)

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        head = source.read(0, min(source.size, PROBE_HEAD_SIZE))
        found = _header_version(head)
        summary: dict[str, JsonValue] = {
            "encrypted": False,
            "readable": False,
            "size": source.size,
            "tagged": False,
        }
        if found is not None and found[1] is not None:
            summary["version"] = found[1]
        with pypdf_session(config.integer("max_stream_bytes")) as captured:
            try:
                opened = open_document(source, captured)
                summary["pages"] = len(page_list(opened))
                summary["encrypted"] = opened.encryption != "none"
                summary["readable"] = opened.encryption != "unreadable"
                summary["tagged"] = entry(opened.catalog, "/StructTreeRoot") is not None
            except Unreadable:
                pass
        return InspectResult(summary)

    # --- plan --------------------------------------------------------------------------------

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        chunks = [make_chunk(source, config, {"part": "document"}, source.size)]
        count = 0
        with pypdf_session(config.integer("max_stream_bytes")) as captured:
            try:
                opened = open_document(source, captured)
                if opened.encryption != "unreadable":
                    count = len(page_list(opened))
            except Unreadable:
                count = 0
        for first in range(0, count, self._pages_per_chunk):
            pages = min(self._pages_per_chunk, count - first)
            context: JsonObject = {"count": pages, "first": first, "part": "pages"}
            cost = source.size * pages // count
            chunks.append(make_chunk(source, config, context, cost))
        return Plan(tuple(chunks))

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        out = _Output(source, config)
        context = chunk.context
        with pypdf_session(config.integer("max_stream_bytes")) as captured:
            if context["part"] == "document":
                _document(out, captured)
            else:
                first, count = context["first"], context["count"]
                if not isinstance(first, int) or not isinstance(count, int):
                    raise ValueError(f"chunk context {context} names no page range")
                _pages(out, captured, first, count)
        return out.output()


# --- The document ---------------------------------------------------------------------------


def _document(out: _Output, captured: Warnings) -> None:
    try:
        opened = open_document(out.source, captured)
        pages = page_list(opened)
    except Unreadable as exc:
        out.finding(
            "pdf.unreadable",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            out.whole,
            "the file could not be opened as a PDF, even with its cross-reference rebuilt",
            {"error": exc.cause},
        )
        return
    readable = opened.encryption != "unreadable"
    if not readable:
        _encrypted(out, opened)
    title = _title(out, opened) if readable else Unknown()
    labels = _labels(out, opened, len(pages)) if readable else [Unknown()] * len(pages)
    header = _header_evidence(out)
    described = [
        _page_record(out, page, index, label, header)
        for index, (page, label) in enumerate(zip(pages, labels, strict=True))
    ]
    out.records.append(
        DocumentRecord(
            id=out.document,
            provenance=Provenance(out.whole, out.config.transform.id, OBSERVED),
            format="pdf",
            title=title,
            pages=tuple(described),
        )
    )
    if readable:
        try:
            _active_content(out, opened, pages)
        except (MemoryError, ShortReadError):
            raise
        except Exception:
            captured.other += 1  # the scan stopped on a broken object: a repair to report
    opened.stream.check()
    streams, other = captured.take()
    warnings = opened.open_warnings[0] + opened.open_warnings[1] + streams + other
    if opened.repaired or warnings:
        rebuilt = (
            "its cross-reference was rebuilt by scanning its objects; " if opened.repaired else ""
        )
        out.finding(
            "pdf.repaired",
            FindingCategory.CORRUPT,
            Severity.WARNING,
            out.whole,
            f"the file was read with repairs: {rebuilt}pypdf reported {warnings} problem(s)",
            {"rebuilt": opened.repaired, "warnings": warnings},
        )


def _encrypted(out: _Output, opened: Opened) -> None:
    encrypt = dictionary(entry(opened.reader.trailer, "/Encrypt"))
    method = name(entry(encrypt, "/Filter")) or ""
    revision = integer(entry(encrypt, "/V"))
    out.finding(
        "pdf.encrypted",
        FindingCategory.UNSUPPORTED,
        Severity.ERROR,
        out.whole,
        "the file is encrypted with a password or a cipher Neptune does not decrypt; its pages"
        " are listed and its text is not read",
        {"filter": method, "version": revision if revision is not None else -1},
    )


def _header_evidence(out: _Output) -> EvidenceRef:
    head = out.source.read(0, min(out.source.size, HEADER_WINDOW))
    at = head.find(HEADER)
    if at < 0:
        return out.whole
    return out.evidence(ByteRange(at, min(len(HEADER) + 3, out.source.size - at)))


def _text_value(
    out: _Output, value: object, field: str, cited: Provenance | None
) -> Knowledge[str]:
    """Declared text: ``Known`` as written, ``Unknown`` when blank or of the wrong type."""
    if value is None:
        return Unknown() if cited is None else Unknown(cited)
    if not isinstance(value, TextStringObject):
        out.finding(
            "pdf.value_unreadable",
            FindingCategory.CORRUPT,
            Severity.WARNING,
            cited.evidence if cited is not None else out.whole,
            f"the declared {field} is not a text string; it is unknown",
            {"field": field},
        )
        return Unknown() if cited is None else Unknown(cited)
    text = str(value)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        out.finding(
            "pdf.value_unreadable",
            FindingCategory.CORRUPT,
            Severity.WARNING,
            cited.evidence if cited is not None else out.whole,
            f"the declared {field} is not valid text; it is unknown",
            {"field": field},
        )
        return Unknown() if cited is None else Unknown(cited)
    if not text.strip():
        return Unknown() if cited is None else Unknown(cited)
    return Known(text) if cited is None else Known(text, cited)


def _title(out: _Output, opened: Opened) -> Knowledge[str]:
    raw = dict.get(opened.reader.trailer, "/Info")
    if raw is None:
        return Unknown()
    ref = reference(raw)
    cited = out.provenance(_object(ref)) if ref is not None else None
    value: object
    try:
        info = dictionary(raw)
        value = entry(info, "/Title") if info is not None else None
    except (MemoryError, ShortReadError):
        raise
    except Exception:
        value = b""  # the information dictionary does not parse: reported as the wrong type
    return _text_value(out, value, "title", cited)


def _labels(out: _Output, opened: Opened, count: int) -> list[Knowledge[str]]:
    raw = dict.get(opened.catalog, "/PageLabels")
    if raw is None:
        return [Unknown()] * count
    ref = reference(raw) or reference(opened.catalog)
    cited = out.provenance(_object(ref)) if ref is not None else None
    try:
        labels = list(opened.reader.page_labels)
    except (MemoryError, ShortReadError):
        raise
    except Exception:
        out.finding(
            "pdf.value_unreadable",
            FindingCategory.CORRUPT,
            Severity.WARNING,
            cited.evidence if cited is not None else out.whole,
            "the declared page labels cannot be read; every label is unknown",
            {"field": "page_labels"},
        )
        return [Unknown()] * count
    found: list[Knowledge[str]] = []
    for index in range(count):
        label = labels[index] if index < len(labels) else ""
        if label:
            found.append(Known(label) if cited is None else Known(label, cited))
        else:
            found.append(Unknown() if cited is None else Unknown(cited))
    return found


def _page_record(
    out: _Output, page: DictionaryObject, index: int, label: Knowledge[str], header: EvidenceRef
) -> DocumentPage:
    at = out.provenance(Page(index))
    try:
        box = [number(value) for value in array(entry(page, "/MediaBox")) or []]
        rotate = resolve(dict.get(page, "/Rotate"))
    except (MemoryError, ShortReadError):
        raise
    except Exception:
        box, rotate = [], b""
    width: Knowledge[float]
    height: Knowledge[float]
    if len(box) == 4 and None not in box:
        x0, y0, x1, y1 = (value for value in box if value is not None)
        width, height = Known(abs(x1 - x0), at), Known(abs(y1 - y0), at)
    else:
        width, height = Unknown(at), Unknown(at)
        out.finding(
            "pdf.value_unreadable",
            FindingCategory.CORRUPT,
            Severity.WARNING,
            at.evidence,
            f"page {index} declares no usable media box; its size is unknown",
            {"field": "media_box", "page": index},
        )
    rotation: Knowledge[int]
    if rotate is None:
        rotation = Known(0, Provenance(header, out.config.transform.id, OBSERVED))
    elif integer(rotate) is not None:
        rotation = Known(integer(rotate) or 0, at)
    else:
        rotation = Unknown(at)
        out.finding(
            "pdf.value_unreadable",
            FindingCategory.CORRUPT,
            Severity.WARNING,
            at.evidence,
            f"page {index} declares a rotation that is not an integer; it is unknown",
            {"field": "rotation", "page": index},
        )
    return DocumentPage(label=label, width=width, height=height, rotation=rotation)


def _count_names(tree: object) -> int:
    """How many entries a name tree holds, bounded."""
    total, visits = 0, 0
    stack = [tree]
    while stack and visits < MAX_NAME_TREE_VISITS:
        visits += 1
        node = dictionary(stack.pop())
        if node is None:
            continue
        names = array(entry(node, "/Names"))
        if names is not None:
            total += len(names) // 2
        stack.extend(array(entry(node, "/Kids")) or [])
    return total


def _is_script(action: object) -> bool:
    return name(entry(action, "/S")) == "JavaScript"


def _active_content(out: _Output, opened: Opened, pages: list[DictionaryObject]) -> None:
    """Report JavaScript and embedded files the document declares; never touch them."""
    catalog = opened.catalog
    names = dictionary(entry(catalog, "/Names"))
    scripts = _count_names(entry(names, "/JavaScript")) if names is not None else 0
    files = _count_names(entry(names, "/EmbeddedFiles")) if names is not None else 0
    scripts += 1 if _is_script(entry(catalog, "/OpenAction")) else 0
    holders: list[object] = [entry(catalog, "/AA")]
    annotations = 0
    for page in pages:
        holders.append(entry(page, "/AA"))
        for raw in array(entry(page, "/Annots")) or []:
            annotations += 1
            if annotations > MAX_ANNOTATIONS:
                break
            annotation = dictionary(raw)
            if annotation is None:
                continue
            if name(entry(annotation, "/Subtype")) == "FileAttachment":
                files += 1
            scripts += 1 if _is_script(entry(annotation, "/A")) else 0
            holders.append(entry(annotation, "/AA"))
    for holder in holders:
        actions = dictionary(holder)
        if actions is not None:
            scripts += sum(1 for action in actions.values() if _is_script(action))
    if scripts:
        out.finding(
            "pdf.javascript",
            FindingCategory.SKIPPED,
            Severity.INFO,
            out.whole,
            f"the document declares {scripts} JavaScript action(s); they are never run",
            {"scripts": scripts},
        )
    if files:
        out.finding(
            "pdf.embedded_files",
            FindingCategory.SKIPPED,
            Severity.WARNING,
            out.whole,
            f"the document embeds {files} file(s); they are not opened or ingested",
            {"files": files},
        )


# --- Pages ----------------------------------------------------------------------------------


def _pages(out: _Output, captured: Warnings, first: int, count: int) -> None:
    try:
        opened = open_document(out.source, captured)
        pages = page_list(opened)
    except Unreadable:
        return  # the document chunk reports it
    captured.take()  # opening's warnings are the document chunk's to report
    if opened.encryption == "unreadable":
        return
    numbers = {ref: index for index, page in enumerate(pages) if (ref := reference(page))}
    try:
        structure: Structure | None = Structure(opened.catalog, numbers)
    except (MemoryError, ShortReadError):
        raise
    except Exception:
        structure = None
    for index in range(first, min(first + count, len(pages))):
        _page(out, opened, captured, structure, pages[index], index)
        opened.stream.check()


def _page(
    out: _Output,
    opened: Opened,
    captured: Warnings,
    structure: Structure | None,
    page: DictionaryObject,
    index: int,
) -> None:
    config = out.config
    subject = out.evidence(Page(index))
    interpreter = Interpreter(
        opened.reader,
        max_operations=config.integer("max_page_operations"),
        max_content_bytes=config.integer("max_page_content_bytes"),
        space_threshold=config.integer("space_threshold"),
    )
    error: str | None = None
    content: PageContent | None = None
    try:
        content = interpreter.run(page)
    except (MemoryError, ShortReadError):
        raise
    except LimitReachedError:
        out.finding(
            "pdf.content_limit",
            FindingCategory.LIMIT,
            Severity.ERROR,
            subject,
            f"page {index} has a stream that inflates past max_stream_bytes; it is not read",
            {"limit": "max_stream_bytes", "page": index},
        )
    except Exception as exc:
        error = type(exc).__name__
    placed: PageStructure | None = None
    if structure is not None and structure.tagged:
        try:
            placed = structure.page(page)
        except (MemoryError, ShortReadError):
            raise
        except Exception as exc:
            out.finding(
                "pdf.structure_unusable",
                FindingCategory.CORRUPT,
                Severity.WARNING,
                subject,
                f"the tags of page {index} cannot be read; it is read as untagged",
                {"error": type(exc).__name__, "page": index},
            )
    streams, other = captured.take()
    if error is not None or streams:
        details: dict[str, JsonValue] = {"page": index}
        details.update({"error": error} if error is not None else {"streams": streams})
        out.finding(
            "pdf.content_unreadable",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            subject,
            f"the content of page {index} could not be fully decoded; what was read is kept",
            details,
        )
    if other:
        out.finding(
            "pdf.page_repaired",
            FindingCategory.CORRUPT,
            Severity.WARNING,
            subject,
            f"pypdf repaired {other} object(s) while reading page {index}",
            {"page": index, "warnings": other},
        )
    if content is not None:
        _content_findings(out, content.skipped, content.missing_fonts, content.limited, index)
    items = content.items if content is not None else []
    found = blocks(items, placed, config.integer("space_threshold") / 1000.0)
    _emit_blocks(out, found, index)


def _content_findings(
    out: _Output, skipped: int, missing: int, limited: str | None, index: int
) -> None:
    subject = out.evidence(Page(index))
    if limited is not None:
        limit = (
            "max_page_content_bytes" if limited == "max_content_bytes" else "max_page_operations"
        )
        out.finding(
            "pdf.content_limit",
            FindingCategory.LIMIT,
            Severity.ERROR,
            subject,
            f"page {index} passed {limit} ({out.config.integer(limit)}); it was read up to there",
            {"limit": limit, "page": index, "value": out.config.integer(limit)},
        )
    if skipped or missing:
        out.finding(
            "pdf.content_skipped",
            FindingCategory.CORRUPT,
            Severity.WARNING,
            subject,
            f"page {index}: {skipped} operator(s) skipped, {missing} run(s) shown with no font",
            {"missing_fonts": missing, "page": index, "skipped": skipped},
        )


def _role(block: Block) -> tuple[Knowledge[BlockRole], Knowledge[int]]:
    if block.role is None:
        return Unknown(), Unknown()
    if block.leveled:
        return Known(block.role), Known(block.level) if block.level else Unknown()
    return Known(block.role), NotApplicable()


def _emit_blocks(out: _Output, found: list[Block], index: int) -> None:
    unmapped: list[RecordId] = []
    unplaced: list[RecordId] = []
    by_owner = {block.owner.key: block for block in found if block.owner is not None}
    for position, block in enumerate(found[:STRIDE]):
        evidence = out.evidence(Page(index), Span(block.start, block.end))
        block_id = out.record_id(DocumentBlock.kind, evidence)
        text: Knowledge[str]
        if not block.textual:
            text = NotApplicable()
        elif block.unmapped:
            text = Unknown()
            unmapped.append(block_id)
        else:
            text = Known(block.text)
        region: Knowledge[EvidenceRef] = Unknown()
        if block.box is not None:
            corners = [
                _coordinate(v) for v in (block.box.x0, block.box.y0, block.box.x1, block.box.y1)
            ]
            if all(math.isfinite(v) for v in corners):
                x0, y0, x1, y1 = corners
                region = Known(out.evidence(Page(index), PageRegion(index, x0, y0, x1, y1)))
        if isinstance(region, Unknown):
            unplaced.append(block_id)
        role, level = _role(block)
        out.records.append(
            DocumentBlock(
                id=block_id,
                provenance=Provenance(evidence, out.config.transform.id, OBSERVED),
                document=out.document,
                order=index * STRIDE + position,
                role=role,
                level=level,
                text=text,
                region=region,
            )
        )
        if block.table is not None and block.owner is not None:
            _emit_table(out, block, index, by_owner)
    subject = out.evidence(Page(index))
    if unmapped:
        out.finding(
            "pdf.unmapped_glyphs",
            FindingCategory.UNREPRESENTABLE,
            Severity.WARNING,
            subject,
            f"{len(unmapped)} block(s) on page {index} show codes no font maps to text;"
            " their text is unknown",
            {"blocks": len(unmapped), "page": index},
            unmapped,
        )
    if unplaced:
        out.finding(
            "pdf.geometry_unknown",
            FindingCategory.UNREPRESENTABLE,
            Severity.WARNING,
            subject,
            f"{len(unplaced)} block(s) on page {index} use fonts without widths or ascent;"
            " their region is unknown",
            {"blocks": len(unplaced), "page": index},
            unplaced,
        )


def _emit_table(out: _Output, block: Block, index: int, by_owner: dict[Key, Block]) -> None:
    table, owner = block.table, block.owner
    assert table is not None and owner is not None
    table_evidence = out.evidence(_structure(owner.path))
    table_id = out.record_id(StructuredTable.kind, table_evidence)
    header_row = bool(table.rows) and bool(table.rows[0]) and all(k == "TH" for k in table.rows[0])
    cells = {(cell.row, cell.column): cell for cell in block.cells}
    if table.first_page is None:
        out.finding(
            "pdf.table_incomplete",
            FindingCategory.UNREPRESENTABLE,
            Severity.WARNING,
            out.evidence(Page(index), Span(block.start, block.end)),
            f"a table on page {index} declares no page for its content; it has no records",
            {"page": index},
        )
        return
    if table.first_page == index:
        header: Knowledge[tuple[str, ...]] = NotApplicable()
        if header_row:
            header = Unknown()
            if 0 in block.rows:
                row_cells = [cells.get((0, c)) for c in range(len(table.rows[0]))]
                if all(cell is not None and not cell.unmapped for cell in row_cells):
                    start, end = block.rows[0]
                    texts = tuple(cell.text for cell in row_cells if cell is not None)
                    header = Known(texts, out.provenance(Page(index), Span(start, end)))
        caption = by_owner.get(table.caption) if table.caption is not None else None
        title: Knowledge[str] = Unknown()
        if caption is not None and caption.textual and not caption.unmapped:
            title = Known(
                caption.text, out.provenance(Page(index), Span(caption.start, caption.end))
            )
        out.records.append(
            StructuredTable(
                id=table_id,
                provenance=Provenance(table_evidence, out.config.transform.id, OBSERVED),
                name=title,
                header=header,
            )
        )
    elsewhere = 0
    for row, (start, end) in sorted(block.rows.items()):
        if (row == 0 and header_row) or table.row_pages[row] != index:
            continue
        values: list[Knowledge[CellValue]] = []
        for column in range(len(table.rows[row])):
            cell = cells.get((row, column))
            declared = table.cell_pages[row][column]
            if cell is None or (declared is not None and declared != index):
                elsewhere += 1
                values.append(Unknown(out.provenance(Page(index), Span(end, end))))
                continue
            cited = out.provenance(Page(index), Span(cell.start, cell.end))
            if cell.empty or cell.unmapped:
                values.append(Unknown(cited))
            else:
                values.append(Known(cell.text, cited))
        evidence = out.evidence(Page(index), Span(start, end))
        out.records.append(
            StructuredRecord(
                id=out.record_id(StructuredRecord.kind, evidence),
                provenance=Provenance(evidence, out.config.transform.id, OBSERVED),
                table=table_id,
                row=row,
                cells=tuple(values),
            )
        )
    if elsewhere:
        out.finding(
            "pdf.table_incomplete",
            FindingCategory.UNREPRESENTABLE,
            Severity.WARNING,
            out.evidence(Page(index), Span(block.start, block.end)),
            f"{elsewhere} cell(s) of a table on page {index} are drawn on another page;"
            " they are unknown in their row",
            {"cells": elsewhere, "page": index},
        )

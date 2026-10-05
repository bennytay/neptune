"""Lifecycle records from documents: a compiler package's ``DocumentRecord``, blocks and tables,
through declared document templates, into lifecycle records in a new package (ADR 0003).

It reads ``DocumentRecord``, ``DocumentBlock``, ``StructuredTable`` and ``StructuredRecord`` only,
never a document's bytes. A template (``templates.py``) names the structure a document must show;
matching it is an observation, recorded as a finding that cites what was seen. Each value a field
reads is stated as the document states it and cites the exact span or cell. What no template
covers, and what a document shows that no field read, is a finding (ADR 0003 §7).
"""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from itertools import pairwise
from typing import Any, Final

from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ContentId, LogicalId, RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import AssertionKind, Knowledge, Known, NotCovered, Unknown
from neptune.model.lifecycle import LIFECYCLE_KINDS
from neptune.model.provenance import EvidenceRef, Page, Provenance, Span, TransformRecord
from neptune.model.world import (
    BlockRole,
    DocumentBlock,
    DocumentRecord,
    StructuredRecord,
    StructuredTable,
)
from neptune.store.package import IngestPackage
from neptune_deploy.lifecycle.mapper import (
    NAMED,
    _Cell,
    _Clocks,
    _Findings,
    _Read,
    _Row,
    _Table,
    _table,
    _Values,
    named_columns,
    unique_domains,
)
from neptune_deploy.lifecycle.mapping import (
    ListCell,
    MappingError,
    Part,
    Rows,
    spec_refs,
    uncovered,
)
from neptune_deploy.lifecycle.templates import DocumentTemplate, config_of, rows_read

DOCUMENT_MAPPER_ID: Final = "deploy_document_map"
DOCUMENT_MAPPER_VERSION: Final = "0.2.0"
STATED: Final = AssertionKind.STATED
OBSERVED: Final = AssertionKind.OBSERVED
# Page furniture: a running header or footer is neither a label nor part of a section.
FURNITURE: Final = frozenset({BlockRole.HEADER, BlockRole.FOOTER})
_NOT_TEXT: Final = frozenset({BlockRole.HEADING, BlockRole.TABLE, BlockRole.FIGURE}) | FURNITURE


def _catalog(*entries: tuple[str, Severity, FindingCategory, str]) -> dict[str, Any]:
    return {name: (severity, category, message) for name, severity, category, message in entries}


# Every finding code: severity, category and what it means (ADR 0003 §7).
FINDINGS: Final[dict[str, tuple[Severity, FindingCategory, str]]] = _catalog(
    (
        "template_matched",
        Severity.INFO,
        FindingCategory.MISSING,
        "a document matched a template: the structure it showed is cited, and the fields of the"
        " kind the template does not cover are listed as not covered",
    ),
    (
        "template_version_mismatch",
        Severity.WARNING,
        FindingCategory.INCONSISTENT,
        "a document declares the form of a registered template, but not a registered version of"
        " it; no version was assumed, so it has no lifecycle record",
    ),
    (
        "template_structure_missing",
        Severity.WARNING,
        FindingCategory.MISSING,
        "a document declares the form and version of a template but lacks structure the template"
        " requires; it has no lifecycle record",
    ),
    (
        "template_ambiguous",
        Severity.ERROR,
        FindingCategory.AMBIGUOUS,
        "a document matches more than one template; none was picked, so it has no lifecycle record",
    ),
    (
        "document_unmatched",
        Severity.INFO,
        FindingCategory.UNSUPPORTED,
        "a document no template matches; it has no lifecycle record",
    ),
    (
        "no_text_layer",
        Severity.WARNING,
        FindingCategory.MISSING,
        "a document with no text and no table (a scan, or unreadable); OCR is derived, never done"
        " here, so it has no lifecycle record",
    ),
    (
        "page_rotated",
        Severity.INFO,
        FindingCategory.UNSUPPORTED,
        "pages that declare a rotation; spans and values are unaffected, regions stay in the"
        " page's own coordinates",
    ),
    (
        "text_unread",
        Severity.INFO,
        FindingCategory.UNSUPPORTED,
        "text and tables of a matched document that no field of the template reads; they stay in"
        " the base package only",
    ),
    (
        "column_unmapped",
        Severity.WARNING,
        FindingCategory.UNSUPPORTED,
        "columns of a table the template reads that no field reads and the template does not"
        " ignore; their cells stay in the base package only",
    ),
    (
        "label_absent",
        Severity.INFO,
        FindingCategory.MISSING,
        "labels the template reads that the document does not show; their fields are not covered",
    ),
    (
        "label_value_wrapped",
        Severity.INFO,
        FindingCategory.UNREPRESENTABLE,
        "a label's value that takes several lines of its block: the value joins them with one"
        " space, and one citation cannot hold that text, so each line's span is listed",
    ),
    (
        "label_repeated",
        Severity.WARNING,
        FindingCategory.AMBIGUOUS,
        "labels the document shows more than once; the fields read from them are unknown",
    ),
    (
        "section_absent",
        Severity.INFO,
        FindingCategory.MISSING,
        "sections the template reads that the document does not show; their fields are not covered",
    ),
    (
        "section_repeated",
        Severity.WARNING,
        FindingCategory.AMBIGUOUS,
        "section headings the document shows more than once; the fields read from them are unknown",
    ),
    (
        "section_not_contiguous",
        Severity.WARNING,
        FindingCategory.UNREPRESENTABLE,
        "section text that is not one span of one page (it crosses a page, or holds a figure or a"
        " gap); one citation cannot hold it, so the field is unknown",
    ),
    (
        "value_unreadable",
        Severity.WARNING,
        FindingCategory.INCONSISTENT,
        "values that do not read as their field's declared shape or format; the fields are unknown",
    ),
    (
        "value_blank",
        Severity.WARNING,
        FindingCategory.MISSING,
        "blank values of fields the template declares required; the fields are unknown",
    ),
    (
        "list_cell_blank",
        Severity.WARNING,
        FindingCategory.MISSING,
        "a blank value among several read into one list field, where the others state items: the"
        " list holds those items and lacks what the blank value would have stated (a list whose"
        " every value is blank is Unknown, and needs no finding)",
    ),
    (
        "list_part_empty",
        Severity.INFO,
        FindingCategory.MISSING,
        "an empty part between declared delimiters in a list value; it states nothing and is not"
        " listed",
    ),
    (
        "list_id_repeated",
        Severity.INFO,
        FindingCategory.INCONSISTENT,
        "identifiers a list value states again; a declared-id list holds each once, so the first"
        " statement is kept and the repeats are cited",
    ),
    (
        "list_truncated",
        Severity.WARNING,
        FindingCategory.LIMIT,
        "a list value stating more parts than the mapper reads; the record's list holds the first"
        " of them only, and the value's text after them is cited as not read",
    ),
    (
        "item_blank",
        Severity.INFO,
        FindingCategory.MISSING,
        "parts whose every value is blank (no part swapped, no test); none is listed",
    ),
    (
        "identifier_repeated",
        Severity.WARNING,
        FindingCategory.INCONSISTENT,
        "records of one template stating the same identifier; they are kept apart, never merged",
    ),
    (
        "record_unrepresentable",
        Severity.ERROR,
        FindingCategory.UNREPRESENTABLE,
        "documents whose mapped values the lifecycle kind refuses; they have no lifecycle record",
    ),
)

# --- A document's evidence ----------------------------------------------------------------------


def _text(state: Knowledge[Any]) -> str | None:
    return state.value if isinstance(state, Known) and isinstance(state.value, str) else None


def _role(block: DocumentBlock) -> BlockRole | None:
    return block.role.value if isinstance(block.role, Known) else None


def _level(block: DocumentBlock) -> int | None:
    return block.level.value if isinstance(block.level, Known) else None


def _span(evidence: EvidenceRef) -> Span | None:
    last = evidence.locator[-1] if evidence.locator else None
    return last if isinstance(last, Span) else None


def _page(evidence: EvidenceRef) -> int | None:
    first = evidence.locator[0] if evidence.locator else None
    return first.index if isinstance(first, Page) else None


def _within(inner: EvidenceRef, outer: EvidenceRef) -> bool:
    """``inner`` is a span inside ``outer``'s span on the same page of the same bytes."""
    a, b = _span(inner), _span(outer)
    return (
        a is not None
        and b is not None
        and inner.source == outer.source
        and _page(inner) == _page(outer)
        and b.start <= a.start
        and a.end <= b.end
    )


@dataclass(frozen=True)
class _Scope:
    """What a document's findings and clocks are scoped to: the document."""

    record: DocumentRecord

    @property
    def evidence(self) -> EvidenceRef:
        return self.record.provenance.evidence


@dataclass(frozen=True)
class _Kv:
    """A table with no header row of its own: a form's two-column ``Label | Value`` rows."""

    record: StructuredTable
    rows: list[StructuredRecord]


@dataclass(frozen=True)
class _Hit:
    """A label found: the state it holds, where, and what was read (to account for what was not)."""

    state: Knowledge[Any]
    place: EvidenceRef
    block: RecordId | None = None
    row: tuple[RecordId, int] | None = None
    lines: tuple[int, ...] = ()  # the lines of the block this label and its value took
    cited: tuple[EvidenceRef, ...] = ()  # the spans of the first NAMED lines of a wrapped value
    wrapped: int = 0  # how many lines a value of several lines takes


def _lines(text: str) -> list[tuple[int, str]]:
    """A block's lines with their offsets in its text (a page's text is its blocks, LF between)."""
    out, offset = [], 0
    for line in text.split("\n"):
        out.append((offset, line))
        offset += len(line) + 1
    return out


def _stray_lines(block: DocumentBlock, taken: set[int]) -> list[EvidenceRef]:
    """The lines of a block, some of which a label took: each other line, cited by its own span."""
    text = _text(block.text) or ""
    evidence = block.provenance.evidence
    span = _span(evidence)
    exact = span is not None and span.end - span.start == len(text)
    out = []
    for index, (begin, line) in enumerate(_lines(text)):
        if index in taken or not line.strip():
            continue
        if span is None or not exact:
            out.append(evidence)
            continue
        first = begin + len(line) - len(line.lstrip())
        out.append(
            EvidenceRef(
                evidence.source,
                (
                    *evidence.locator[:-1],
                    Span(span.start + first, span.start + first + len(line.strip())),
                ),
            )
        )
    return list(dict.fromkeys(out))


def _header_evidence(table: _Table) -> EvidenceRef:
    """Where a table's header row is: its cells' own place in the document."""
    header = table.record.header
    provenance = header.provenance if isinstance(header, Known) else None
    return provenance.evidence if isinstance(provenance, Provenance) else table.evidence


class _View:
    """One document and what the compiler read of it: its blocks in reading order and its tables."""

    def __init__(
        self,
        record: DocumentRecord,
        blocks: list[DocumentBlock],
        tables: list[_Table],
        kv: list[_Kv],
    ) -> None:
        self.record = record
        self.blocks = sorted(blocks, key=lambda b: b.order)
        self.tables = tables
        self.kv = kv
        self.scope = _Scope(record)
        self.evidence = record.provenance.evidence

    @property
    def has_text(self) -> bool:
        texts = any(_text(b.text) and _role(b) not in FURNITURE for b in self.blocks)
        return texts or any(t.rows for t in self.tables) or any(k.rows for k in self.kv)

    def transforms(self) -> set[RecordId]:
        out = {self.record.provenance.transform}
        out.update(b.provenance.transform for b in self.blocks)
        for table in self.tables:
            out.add(table.record.provenance.transform)
            out.update(r.provenance.transform for r in table.rows)
        for kv in self.kv:
            out.add(kv.record.provenance.transform)
            out.update(r.provenance.transform for r in kv.rows)
        return out

    # Lookups (pure: nothing is marked read) ------------------------------------------------

    def labels(
        self, name: str, template: DocumentTemplate, *, own_line: bool = False
    ) -> list[_Hit]:
        """Every place the document shows ``name`` with a value: an inline ``name: value`` line of a
        paragraph, or a row of a headerless table whose first cell is ``name``.

        An extractor wraps a paragraph into one block of several lines, and a form may put a value
        under its label. A label's value is the rest of its line and every following line of the
        block up to the next line of a label the template knows, joined by one space (ADR 0003 §4);
        a value that goes on in another block or on another page is not followed. An identifier
        (``own_line``: the form's id and version) is only the rest of its own line."""
        hits = []
        lead = name + template.separator
        for block in self.blocks:
            text = _text(block.text)
            if text is None or _role(block) in _NOT_TEXT or lead not in text:
                continue
            evidence = block.provenance.evidence
            span = _span(evidence)
            exact = span is not None and span.end - span.start == len(text)
            provenance = Provenance(evidence, block.provenance.transform, OBSERVED)
            lines = _lines(text)
            for index, (_, line) in enumerate(lines):
                if not line.startswith(lead):
                    continue
                taken = [index]
                pieces: list[tuple[int, int, str]] = []  # (start, end, text) in the block's text
                for at in range(index, index + 1 if own_line else len(lines)):
                    start, rest = lines[at]
                    if at > index and any(rest.startswith(k) for k in template.leads):
                        break
                    if at > index:
                        taken.append(at)
                    if at == index:
                        start, rest = start + len(lead), rest[len(lead) :]
                    value = rest.strip()
                    if value:
                        first = start + len(rest) - len(rest.lstrip())
                        pieces.append((first, first + len(value), value))
                if not pieces:
                    hits.append(
                        _Hit(Unknown(provenance), evidence, block=block.id, lines=tuple(taken))
                    )
                    continue
                place = evidence
                cited: tuple[EvidenceRef, ...] = ()
                if span is not None and exact:
                    inner = (*evidence.locator[:-1],)
                    place = EvidenceRef(
                        evidence.source,
                        (*inner, Span(span.start + pieces[0][0], span.start + pieces[-1][1])),
                    )
                    cited = tuple(
                        EvidenceRef(evidence.source, (*inner, Span(span.start + a, span.start + b)))
                        for a, b, _ in pieces[:NAMED]
                    )
                value = " ".join(piece[2] for piece in pieces)
                hits.append(
                    _Hit(
                        Known(value, provenance),
                        place,
                        block=block.id,
                        lines=tuple(taken),
                        cited=cited if len(pieces) > 1 else (),
                        wrapped=len(pieces) if len(pieces) > 1 else 0,
                    )
                )
        for kv in self.kv:
            for row in kv.rows:
                if len(row.cells) >= 2 and _text(row.cells[0]) == name:
                    place = row.cell_evidence(kv.record, 1)
                    hits.append(_Hit(row.cells[1], place, row=(kv.record.id, row.row)))
        return hits

    def headings(self, name: str) -> list[DocumentBlock]:
        return [b for b in self.blocks if _role(b) is BlockRole.HEADING and _text(b.text) == name]

    def section(self, heading: DocumentBlock) -> list[DocumentBlock]:
        """The blocks under ``heading`` up to the next heading at its level or above."""
        level = _level(heading)
        out: list[DocumentBlock] = []
        after = False
        for block in self.blocks:
            if block is heading:
                after = True
                continue
            if not after or _role(block) in FURNITURE:
                continue
            if _role(block) is BlockRole.HEADING:
                inner = _level(block)
                if level is None or inner is None or inner <= level:
                    break
            out.append(block)
        return out

    def tables_named(self, header: Sequence[str]) -> list[_Table]:
        """Tables whose header cells are exactly ``header``, in document order (a long table that
        repeats its header on each page is several tables)."""
        found = [t for t in self.tables if t.header == tuple(header)]
        return sorted(found, key=lambda t: self._position(_header_evidence(t)))

    def _position(self, evidence: EvidenceRef) -> tuple[int, int]:
        span = _span(evidence)
        return (_page(evidence) or 0, span.start if span else 0)

    def block_at(self, evidence: EvidenceRef) -> DocumentBlock | None:
        """The block whose span holds ``evidence``'s. A page's blocks are disjoint spans (its text
        is its blocks, LF between: root ADR 0038), so the one starting last at or before it is the
        only candidate; an index by page makes each lookup logarithmic (ADR 0005 §4)."""
        span = _span(evidence)
        if span is None:
            return None
        starts, blocks = self._index.get((evidence.source, _page(evidence)), ([], []))
        at = bisect_right(starts, span.start) - 1
        if at < 0:
            return None
        # Blocks that start together (a zero-width block, say) are tried in reading order.
        for candidate in blocks[bisect_left(starts, starts[at]) : at + 1]:
            if _within(evidence, candidate.provenance.evidence):
                return candidate
        return None

    @cached_property
    def _index(
        self,
    ) -> dict[tuple[Any, int | None], tuple[list[int], list[DocumentBlock]]]:
        """Each page's blocks with a span, by span start (reading order breaks a tie)."""
        pages: dict[tuple[Any, int | None], list[tuple[int, int, DocumentBlock]]]
        pages = defaultdict(list)
        for order, block in enumerate(self.blocks):
            evidence = block.provenance.evidence
            span = _span(evidence)
            if span is not None:
                pages[(evidence.source, _page(evidence))].append((span.start, order, block))
        out = {}
        for key, entries in pages.items():
            entries.sort(key=lambda entry: (entry[0], entry[1]))
            out[key] = ([start for start, _, _ in entries], [block for _, _, block in entries])
        return out


# --- Reading a document into fields ---------------------------------------------------------


class _DocRow(_Values):
    """One document read into one template's fields, remembering what it read."""

    def __init__(self, mapper: "_TemplateMapper", view: _View) -> None:
        self.mapper, self.view, self.template = mapper, view, mapper.template
        self.table = view.scope
        self.evidence = view.evidence
        # The id the lifecycle record will have: findings about its values name it.
        self.record_id = evidence_record_id(
            self.template.kind.kind, self.evidence, mapper.transform
        )
        self.read_blocks: set[RecordId] = set()
        self.read_rows: set[tuple[RecordId, int]] = set()
        self.read_lines: set[tuple[RecordId, int]] = set()  # a label's lines, by block and line
        self._cells: dict[tuple[str, str], _Cell] = {}

    def finding(self, name: str, column: str, subject: EvidenceRef) -> None:
        self.mapper.findings.add(
            name, self.table, subject, key=column, details={"reference": column}
        )

    def cell_finding(
        self,
        name: str,
        column: str,
        path: str,
        subject: EvidenceRef,
        related: Sequence[EvidenceRef] = (),
        details: dict[str, JsonValue] | None = None,
        times: int = 1,
    ) -> None:
        """One finding per value, naming the record and field: never capped, never grouped."""
        self.mapper.findings.add(
            name,
            self.table,
            subject,
            key=f"{path}|{column}|{subject.locator_json()}",
            details={"reference": column, "field": path, **(details or {})},
            record=self.record_id,
            related=related,
            times=times,
        )

    def read(self, hit: _Hit) -> None:
        if hit.block is not None:
            self.read_lines.update((hit.block, line) for line in hit.lines)
        if hit.row is not None:
            self.read_rows.add(hit.row)

    def cell(self, column: str, via: str = "column") -> _Cell:
        """A reference read once per document: asking again (a part is checked for blankness, then
        read) gives the same cell and makes no finding twice."""
        key = (via, column)
        if key not in self._cells:
            self._cells[key] = self._read(column, via)
        return self._cells[key]

    def _read(self, column: str, via: str) -> _Cell:
        if via == "label":
            return self._label(column)
        status, heading, blocks = self._locate(column)
        if status == "absent":
            return _Cell(None, self.evidence, absent_from_table=True)
        assert heading is not None
        place = heading.provenance.evidence
        if status == "repeated":
            return _Cell(Unknown(self.provenance(self.evidence)), self.evidence)
        self.read_blocks.update(b.id for b in (heading, *blocks))
        if not blocks:
            return _Cell(Unknown(self.provenance(place)), place)
        joined = self._joined(blocks)
        if joined is None:
            self.finding("section_not_contiguous", column, place)
            return _Cell(Unknown(self.provenance(place)), place)
        text, cited = joined
        return _Cell(
            Known(text, Provenance(cited, blocks[0].provenance.transform, OBSERVED)), cited
        )

    def _label(self, column: str) -> _Cell:
        hits = self.view.labels(column, self.template)
        if not hits:
            self.mapper.findings.once("label_absent", self.table, self.evidence, column)
            return _Cell(None, self.evidence, absent_from_table=True)
        for hit in hits:
            self.read(hit)
        values = {st.value if isinstance(st, Known) else None for st in (h.state for h in hits)}
        if len(hits) > 1 and len(values) == 1 and None not in values:
            hits = hits[:1]  # said again, the same: it is that value, as for a form
        if len(hits) > 1:
            self.mapper.findings.add(
                "label_repeated",
                self.table,
                hits[0].place,
                key=column,
                details={"reference": column},
                related=[hit.place for hit in hits[1:]],
            )
            return _Cell(Unknown(self.provenance(self.evidence)), self.evidence)
        hit = hits[0]
        if hit.wrapped:
            self.mapper.direct.append(
                _finding(
                    "label_value_wrapped",
                    hit.place,
                    self.mapper.transform,
                    {"reference": column, "lines": hit.wrapped},
                    related=hit.cited,
                    records=[self.record_id],
                )
            )
        return _Cell(hit.state, hit.place)

    def _locate(self, name: str) -> tuple[str, DocumentBlock | None, list[DocumentBlock]]:
        headings = self.view.headings(name)
        if not headings:
            self.mapper.findings.once("section_absent", self.table, self.evidence, name)
            return "absent", None, []
        if len(headings) > 1:
            place = headings[0].provenance.evidence
            self.mapper.findings.add(
                "section_repeated",
                self.table,
                place,
                key=name,
                details={"reference": name},
                related=[h.provenance.evidence for h in headings[1:]],
            )
            self.read_blocks.update(h.id for h in headings)
            return "repeated", headings[0], []
        return "ok", headings[0], self.view.section(headings[0])

    @staticmethod
    def _joined(blocks: list[DocumentBlock]) -> tuple[str, EvidenceRef] | None:
        """The blocks' texts as one span: only if they are text and each starts one LF after the
        last ends, on one page of one document (a page's text is its blocks, each then LF)."""
        texts = [_text(b.text) for b in blocks]
        spans = [_span(b.provenance.evidence) for b in blocks]
        first = blocks[0].provenance.evidence
        prefix = first.locator[:-1]
        for block, text, span in zip(blocks, texts, spans, strict=True):
            evidence = block.provenance.evidence
            if text is None or span is None or evidence.locator[:-1] != prefix:
                return None
            if evidence.source != first.source:
                return None
        for before, after in pairwise(spans):
            assert before is not None and after is not None
            if after.start != before.end + 1:
                return None
        head, tail = spans[0], spans[-1]
        assert head is not None and tail is not None
        text = "\n".join(t for t in texts if t is not None)
        return text, EvidenceRef(first.source, (*prefix, Span(head.start, tail.end)))

    def pieces(self, spec: ListCell, path: str) -> _Read:
        if spec.via != "section":
            return super().pieces(spec, path)
        status, heading, blocks = self._locate(spec.column)
        if status == "absent":
            return _Read([], NotCovered())
        assert heading is not None
        place = heading.provenance.evidence
        if status == "repeated":
            return _Read([], Unknown(self.provenance(self.evidence)), self.evidence)
        self.read_blocks.add(heading.id)
        if not blocks:
            return _Read([], Unknown(self.provenance(place)), place)
        out = []
        for block in blocks:
            if _role(block) is BlockRole.LIST_ITEM and (text := _text(block.text)):
                self.read_blocks.add(block.id)
                out.append((text, block.provenance.evidence, block.provenance.evidence))
        return _Read(out)

    def blank(self, spec: Part) -> bool:
        for via, name in sorted(spec_refs(spec)):
            if via == "column":  # a column belongs to a table's row, not to the document
                continue
            state = self.cell(name, via).state
            if state is not None and not isinstance(state, Unknown):
                return False
        return True

    def absent(self, spec: Part) -> bool:
        refs = [(via, name) for via, name in sorted(spec_refs(spec)) if via != "column"]
        return bool(refs) and all(self.cell(name, via).absent_from_table for via, name in refs)

    def items(self, specs: Any, path: str) -> Knowledge[tuple[Any, ...]]:
        if not isinstance(specs, Rows):
            return super().items(specs, path)
        out: list[Any] = []
        kind = self.template.kind.kind
        tables = self.view.tables_named(self.template.tables[specs.table])
        for table in tables:
            for index in range(len(table.rows)):
                row = _TableRow(self.mapper, table, index, kind, self.record_id, self.table)
                if row.blank(specs.part):
                    column = ", ".join(sorted(name for _, name in spec_refs(specs.part)))
                    row.finding("item_blank", column, row.evidence)
                    continue
                out.append(row.part(specs.part, f"{path}/{len(out)}"))
        if out:
            return Known(tuple(out))
        return Unknown(self.provenance(self.evidence)) if tables else NotCovered()


class _TableRow(_Row):
    """A row of a table in a document, read as a part of the document's record: its findings are
    the document's (ADR 0003 §7), scoped to the document and naming a reference."""

    def __init__(
        self,
        mapper: "_TemplateMapper",
        table: _Table,
        index: int,
        kind: str,
        record_id: RecordId,
        scope: _Scope,
    ) -> None:
        super().__init__(mapper, table, index, kind, record_id)
        self.scope = scope

    def finding(self, name: str, column: str, subject: EvidenceRef) -> None:
        self.mapper.findings.add(
            name, self.scope, subject, key=column, details={"reference": column}
        )

    def cell_finding(
        self,
        name: str,
        column: str,
        path: str,
        subject: EvidenceRef,
        related: Sequence[EvidenceRef] = (),
        details: dict[str, JsonValue] | None = None,
        times: int = 1,
    ) -> None:
        self.mapper.findings.add(
            name,
            self.scope,
            subject,
            key=f"{self.record.row}|{path}|{column}|{subject.locator_json()}",
            details={"reference": column, "field": path, **(details or {})},
            record=self.record_id,
            related=related,
            times=times,
        )


# --- One template over its documents ---------------------------------------------------------


def _distinct(refs: Iterable[EvidenceRef], subject: EvidenceRef) -> tuple[EvidenceRef, ...]:
    return tuple(dict.fromkeys(ref for ref in refs if ref != subject))


def _finding(
    name: str,
    subject: EvidenceRef,
    transform: TransformRecord,
    details: dict[str, Any],
    related: Iterable[EvidenceRef] = (),
    records: Iterable[RecordId] = (),
) -> IngestFinding:
    severity, category, message = FINDINGS[name]
    return ingest_finding(
        code=f"{DOCUMENT_MAPPER_ID}.{name}",
        category=category,
        severity=severity,
        subject=subject,
        transform=transform,
        message=message,
        details=details,
        related=_distinct(related, subject),
        records=records,
    )


@dataclass
class _Verdict:
    """What one template makes of one document: it matches (and what it saw), it names the form
    but not this version, it names the form and lacks structure, or it is not this document's."""

    template: DocumentTemplate
    state: str  # match | mismatch | missing | other
    seen: list[EvidenceRef] = field(default_factory=list)
    found: str | None = None  # the version the document declares, for a mismatch
    missing: list[str] = field(default_factory=list)


def _judge(view: _View, template: DocumentTemplate) -> _Verdict:
    if view.record.format not in template.formats:
        return _Verdict(template, "other")
    seen: list[EvidenceRef] = []
    if template.form is not None:
        form = template.form
        named = view.labels(form.label, template, own_line=True)
        # A form shown more than once (say, on every page) is that form if every statement agrees.
        if not named or any(_text(hit.state) != form.value for hit in named):
            return _Verdict(template, "other")
        seen.append(named[0].place)
        versions = view.labels(form.version_label, template, own_line=True)
        states = sorted({text for hit in versions if (text := _text(hit.state)) is not None})
        found = states[0] if len(states) == 1 and len(versions) >= 1 else None
        if found != form.version or any(_text(hit.state) is None for hit in versions):
            return _Verdict(template, "mismatch", seen, found=found)
        seen.append(versions[0].place)
    missing: list[str] = []
    for label in template.require_labels:
        hits = view.labels(label, template)
        seen.extend(hit.place for hit in hits[:1])
        if not hits:
            missing.append(f"label {label}")
    for heading in template.require_headings:
        blocks = view.headings(heading)
        seen.extend(b.provenance.evidence for b in blocks[:1])
        if not blocks:
            missing.append(f"heading {heading}")
    for name in template.require_tables:
        tables = view.tables_named(template.tables[name])
        seen.extend(_header_evidence(t) for t in tables[:1])
        if not tables:
            missing.append(f"table {name}")
    if missing:
        return _Verdict(template, "missing" if template.form else "other", seen, missing=missing)
    return _Verdict(template, "match", seen)


class _TemplateMapper(_Clocks):
    """One template applied to the documents that match it."""

    def __init__(self, template: DocumentTemplate, base: ContentId, views: list[_View]) -> None:
        self.template, self.views = template, views
        upstream = sorted(set().union(*(view.transforms() for view in views)))
        self.transform = transform_record(
            adapter_id=DOCUMENT_MAPPER_ID,
            adapter_version=DOCUMENT_MAPPER_VERSION,
            config=config_of(template, base),
            upstream=upstream,
        )
        self.findings = _Findings(FINDINGS, DOCUMENT_MAPPER_ID, "document", "reference")
        self.domains = {}
        self.zones = {}
        self.direct: list[IngestFinding] = []

    def run(self, verdicts: dict[RecordId, _Verdict]) -> list[Any]:
        records: list[Any] = []
        for view in self.views:
            row = _DocRow(self, view)
            record = self._record(view, row)
            if record is not None:
                records.append(record)
            self._notes(view, row, verdicts[view.record.id], record)
        self._repeated(records)
        domains = unique_domains(self.domains.values())
        return [
            self.transform,
            *records,
            *domains.values(),
            *self.zones.values(),
            *self.findings.build(self.transform),
            *self.direct,
        ]

    def _record(self, view: _View, row: _DocRow) -> Any:
        kind, evidence = self.template.kind, view.evidence
        try:
            values = row.values(kind, self.template.fields)
            return kind(
                id=row.record_id,
                provenance=Provenance(evidence, self.transform.id, STATED),
                **values,
            )
        except (ValueError, TypeError) as exc:
            self.findings.add(
                "record_unrepresentable",
                view.scope,
                evidence,
                key=self.template.id,
                details={"template": self.template.id, "reason": str(exc)[:200]},
            )
            return None

    def _notes(self, view: _View, row: _DocRow, verdict: _Verdict, record: Any) -> None:
        """What the receipt says about a matched document: the observation, what the template
        leaves out, what no field read, and any declared page rotation."""
        template, evidence = self.template, view.evidence
        records = [record.id] if record is not None else []
        not_covered = uncovered(template.kind, template.fields)
        self.direct.append(
            _finding(
                "template_matched",
                evidence,
                self.transform,
                {
                    "template": template.id,
                    "version": template.version,
                    "kind": template.kind.kind,
                    "not_covered": not_covered,
                },
                related=verdict.seen,
                records=records,
            )
        )
        self._unmapped_columns(view, records)
        # A record the kind refused read only part of the document: its unread text would mislead.
        unread = self._unread(view, row) if record is not None else []
        if unread:
            self.direct.append(
                _finding(
                    "text_unread",
                    evidence,
                    self.transform,
                    {"template": template.id, "count": len(unread)},
                    related=unread[:NAMED],
                    records=records,
                )
            )
        rotated = [
            (index, page.rotation.value)
            for index, page in enumerate(view.record.pages)
            if isinstance(page.rotation, Known) and page.rotation.value % 360 != 0
        ]
        if rotated:
            self.direct.append(
                _finding(
                    "page_rotated",
                    evidence,
                    self.transform,
                    {"pages": [i for i, _ in rotated], "degrees": [d for _, d in rotated]},
                    records=records,
                )
            )

    def _unmapped_columns(self, view: _View, records: list[RecordId]) -> None:
        """Columns of a table whose rows are read that no field reads and the template does not
        ignore: their cells stay in the base package only."""
        template = self.template
        for name, read in sorted(rows_read(template).items()):
            ignored = set(template.ignore_columns.get(name, ()))
            tables = view.tables_named(template.tables[name])
            left = [c for c in template.tables[name] if c not in read and c not in ignored]
            if left and tables:
                self.direct.append(
                    _finding(
                        "column_unmapped",
                        _header_evidence(tables[0]),
                        self.transform,
                        {"template": template.id, "table": name, **named_columns(left)},
                        records=records,
                    )
                )

    def _unread(self, view: _View, row: _DocRow) -> list[EvidenceRef]:
        """Evidence of blocks and headerless-table rows that neither a field, a required or
        ignored structure, nor a declared table accounts for, in reading order."""
        template = self.template
        read = set(row.read_blocks)
        rows = set(row.read_rows)
        lines = set(row.read_lines)

        def mark(hits: list[_Hit]) -> None:
            for hit in hits:
                if hit.block is not None:
                    lines.update((hit.block, line) for line in hit.lines)
                if hit.row is not None:
                    rows.add(hit.row)

        for label in (*template.require_labels, *template.ignore_labels):
            mark(view.labels(label, template))
        if template.form is not None:
            for label in (template.form.label, template.form.version_label):
                mark(view.labels(label, template, own_line=True))
        for name in (*template.require_headings, *template.ignore_headings):
            read.update(heading.id for heading in view.headings(name))
        for header in template.tables.values():
            for table in view.tables_named(header):
                block = view.block_at(_header_evidence(table))
                if block is not None:
                    read.add(block.id)
        unread_rows: list[EvidenceRef] = []
        for kv in view.kv:
            taken = [r for r in kv.rows if (kv.record.id, r.row) in rows]
            if not taken:
                continue
            block = view.block_at(taken[0].provenance.evidence)
            if block is not None:
                read.add(block.id)
            unread_rows += [
                r.provenance.evidence for r in kv.rows if (kv.record.id, r.row) not in rows
            ]
        done_lines: dict[RecordId, set[int]] = defaultdict(set)
        for owner, line in lines:
            done_lines[owner].add(line)
        blocks: list[EvidenceRef] = []
        for block in view.blocks:
            if block.id in read or _role(block) in FURNITURE:
                continue
            done = done_lines.get(block.id, set())
            if not done:
                blocks.append(block.provenance.evidence)
            else:
                blocks.extend(_stray_lines(block, done))
        return [*blocks, *unread_rows]

    def _repeated(self, records: list[Any]) -> None:
        """Two records of this template stating one identifier: both kept, one finding."""
        holders: dict[LogicalId, list[Any]] = defaultdict(list)
        for record in records:
            ids = record.identifiers  # a Known list from this mapper (root ADR 0061 §4)
            for identifier in ids.value if isinstance(ids, Known) else ():
                if isinstance(identifier, Known):
                    holders[identifier.value].append(record)
        scopes = {view.evidence: view.scope for view in self.views}
        for identifier, members in sorted(
            holders.items(), key=lambda i: (i[0].namespace, i[0].value)
        ):
            for member in members[1:]:
                evidence = member.provenance.evidence
                self.findings.add(
                    "identifier_repeated",
                    scopes[evidence],
                    evidence,
                    key=f"{identifier.namespace}:{identifier.value}",
                    details={"identifier": identifier.to_json()},
                    record=member.id,
                    related=(members[0].provenance.evidence,),
                )


# --- A package's documents --------------------------------------------------------------------


def _views(records: Iterable[Any]) -> list[_View]:
    documents: list[DocumentRecord] = []
    blocks: dict[RecordId, list[DocumentBlock]] = defaultdict(list)
    tables: dict[Any, list[StructuredTable]] = defaultdict(list)
    rows: dict[RecordId, list[StructuredRecord]] = defaultdict(list)
    for record in records:
        if isinstance(record, DocumentRecord):
            documents.append(record)
        elif isinstance(record, DocumentBlock):
            blocks[record.document].append(record)
        elif isinstance(record, StructuredTable):
            tables[record.provenance.evidence.source].append(record)
        elif isinstance(record, StructuredRecord):
            rows[record.table].append(record)
    views = []
    for document in sorted(documents, key=lambda d: d.id):
        headed: list[_Table] = []
        headerless: list[_Kv] = []
        for table in sorted(tables[document.provenance.evidence.source], key=lambda t: t.id):
            if isinstance(table.header, Known):
                built = _table(table, rows[table.id])
                if built is not None:
                    headed.append(built)
            else:
                headerless.append(_Kv(table, sorted(rows[table.id], key=lambda r: r.row)))
        views.append(_View(document, blocks[document.id], headed, headerless))
    return views


def map_documents(
    base: IngestPackage, templates: Sequence[DocumentTemplate]
) -> tuple[list[Any], set[RecordId]]:
    """Every record the templates make of the package's documents (each template's transform,
    records, clocks and findings, then findings about documents none matched, under one more
    transform), and the ids of the tables of the documents that matched."""
    if not templates:
        return [], set()
    if any(t.kind not in LIFECYCLE_KINDS for t in templates):
        raise MappingError("a template names a kind outside the compiler's lifecycle kinds")
    views = _views(base.records)
    matched: dict[ContentId, list[_View]] = defaultdict(list)
    verdicts: dict[RecordId, _Verdict] = {}
    pending: list[tuple[str, _View, dict[str, Any], list[EvidenceRef]]] = []
    for view in views:
        if not view.has_text:
            pending.append(("no_text_layer", view, {}, []))
            continue
        judged = [_judge(view, template) for template in templates]
        wins = [v for v in judged if v.state == "match"]
        if len(wins) > 1:
            details = {"templates": sorted(f"{v.template.id}@{v.template.version}" for v in wins)}
            pending.append(("template_ambiguous", view, details, [s for v in wins for s in v.seen]))
        elif wins:
            verdicts[view.record.id] = wins[0]
            matched[wins[0].template.sha256].append(view)
        else:
            _explain(view, judged, pending)
    out: list[Any] = []
    claimed: set[RecordId] = set()
    by_sha = {t.sha256: t for t in templates}
    for key in sorted(matched):
        mapper = _TemplateMapper(by_sha[key], base.id, matched[key])
        out.extend(mapper.run(verdicts))
        for view in matched[key]:
            claimed.update(t.record.id for t in view.tables)
            claimed.update(k.record.id for k in view.kv)
    if pending:
        out.extend(_run_findings(base.id, templates, pending))
    return out, claimed


def _explain(
    view: _View,
    judged: list[_Verdict],
    pending: list[tuple[str, _View, dict[str, Any], list[EvidenceRef]]],
) -> None:
    """Why no template matched a document that has text."""
    mismatches = [v for v in judged if v.state == "mismatch"]
    missing = [v for v in judged if v.state == "missing"]
    if mismatches:
        declared = {
            "templates": [
                {
                    "template": v.template.id,
                    "version": v.template.version,
                    # No single version stated (none, a blank one, or several): nothing to name.
                    **({"found": v.found} if v.found is not None else {}),
                }
                for v in sorted(mismatches, key=lambda v: (v.template.id, v.template.version))
            ]
        }
        pending.append(("template_version_mismatch", view, declared, mismatches[0].seen))
    elif missing:
        absent = {
            "templates": [
                {"template": v.template.id, "version": v.template.version, "missing": v.missing}
                for v in sorted(missing, key=lambda v: (v.template.id, v.template.version))
            ]
        }
        pending.append(("template_structure_missing", view, absent, missing[0].seen))
    else:
        pending.append(("document_unmatched", view, {"format": view.record.format}, []))


def _run_findings(
    base: ContentId,
    templates: Sequence[DocumentTemplate],
    pending: list[tuple[str, _View, dict[str, Any], list[EvidenceRef]]],
) -> list[Any]:
    """Findings about documents no template matched, under a transform of the whole run."""
    upstream = sorted(set().union(*(view.transforms() for _, view, _, _ in pending)))
    transform = transform_record(
        adapter_id=DOCUMENT_MAPPER_ID,
        adapter_version=DOCUMENT_MAPPER_VERSION,
        config={"base_package": base, "templates": sorted(t.sha256 for t in templates)},
        upstream=upstream,
    )
    findings = [
        _finding(name, view.evidence, transform, details, related)
        for name, view, details, related in pending
    ]
    return [transform, *findings]

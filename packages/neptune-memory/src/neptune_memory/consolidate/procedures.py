"""Procedure revisions from declared document labels (ADR 0027).

A deterministic consolidator that reads the compiler's document records (``document_record``,
``document_block``, ``structured_table``, ``structured_record``) under a declaration in its config,
and states what a procedure document says about itself, never what it means:

- A document that shows the declared procedure and revision labels is a **revision**: an ``event``
  node ``record:<document_record id>`` at the date its effective label states, with ``event_kind``
  ``procedure_revision``, ``has_name`` the procedure label's value, ``evidenced_by`` the document,
  ``involves`` each machine it applies to (by an id a ``machine`` record states, never guessed),
  ``declared_value`` per label read and per step under the steps heading, and ``stated_blank`` per
  label shown with nothing after it.
- Each row of the declared revision-history table is an event, ``record:<id>/revisions/<i>``, at
  its date cell: ``has_description`` its description cell, ``declared_value`` per other cell,
  ``stated_blank`` per blank cell.

A label is ``<label>:`` at the start of a line of a paragraph block; its value is the rest of
that line, cited at its own span. A document's dates have no stated zone, so they are whole days on
the document's own calendar (``calendar``), never ordered against another source. Anything that
cannot be read as declared (a label shown twice, a date in another form, an unresolved machine) is
a finding, never a fact.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import TYPE_CHECKING, Final, TypeVar

from neptune.identity import canonical_json
from neptune.identity.ids import record_id
from neptune.model.finding import Severity
from neptune.model.ids import LogicalId
from neptune.model.knowledge import AssertionKind, Known, KnownAbsent, Unknown
from neptune.model.machine import Machine, machine_from_json
from neptune.model.provenance import EvidenceRef, Provenance, Span
from neptune.model.time import Timestamp
from neptune.model.world import (
    BlockRole,
    DocumentBlock,
    DocumentRecord,
    StructuredRecord,
    StructuredTable,
    document_block_from_json,
    document_record_from_json,
    structured_record_from_json,
    structured_table_from_json,
)
from neptune_memory.consolidate.base import ClaimDraft, ConsolidationFinding, ConsolidatorOutput
from neptune_memory.consolidate.identity import node_ref
from neptune_memory.consolidate.identity_records import Malformed
from neptune_memory.consolidate.run_records import Inferred, _strict
from neptune_memory.consolidate.runs import EVIDENCED_BY
from neptune_memory.schema.claim import (
    DeclaredType,
    DeclaredValue,
    LedgerRecordRef,
    TypedLiteral,
    ValueType,
)
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import (
    DECLARED_VALUE,
    EVENT_KIND,
    HAS_DESCRIPTION,
    INVOLVES,
    PROCEDURE_REVISION,
    STATED_BLANK,
    is_declared_value,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from neptune.model.ids import RecordId
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge
    from neptune.model.world import CellValue
    from neptune_memory.consolidate.base import ModelRef
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim, ClaimObject

PROCEDURES_CONSOLIDATOR_ID: Final = "memory.procedures"
HAS_NAME: Final = "has_name"
RECORD_NAMESPACE: Final = "record"
REVISIONS: Final = "revisions"
STATED: Final = AssertionKind.STATED
DAY: Final = Fraction(86400)
CALENDAR_KIND: Final = "memory.document_calendar"
MAX_LISTED: Final = 16  # record ids a finding lists; its details give the count
_DATE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_EPOCH: Final = _dt.date(1970, 1, 1)

_T = TypeVar("_T")


def _finding(
    code: str,
    message: str,
    records: Iterable[RecordId] = (),
    severity: Severity = Severity.INFO,
    **details: JsonValue,
) -> ConsolidationFinding:
    listed = sorted(set(records))
    return ConsolidationFinding(
        code=f"procedures.{code}",
        severity=severity,
        message=message[:1000],
        records=tuple(listed[:MAX_LISTED]),
        details={**details, "count": len(listed)} if len(listed) > MAX_LISTED else details,
    )


# --- The declaration ------------------------------------------------------------------------------


@dataclass(frozen=True)
class History:
    header: tuple[str, ...]
    date: str
    description: str


@dataclass(frozen=True)
class DocumentSpec:
    """One declared kind of procedure document (ADR 0027 §1)."""

    name: str
    procedure: str
    revision: str
    effective: str
    machines_label: str | None
    machines_separator: str
    machines_namespace: str | None
    labels: tuple[str, ...]
    steps: str | None
    history: History | None

    @property
    def read_labels(self) -> tuple[str, ...]:
        """Every label read, in declared order, once."""
        out: list[str] = []
        for label in (
            self.procedure,
            self.revision,
            self.effective,
            *((self.machines_label,) if self.machines_label else ()),
            *self.labels,
        ):
            if label not in out:
                out.append(label)
        return tuple(out)


class _Bad(ValueError):
    pass


_TOP: Final = frozenset({"documents"})
_DOC: Final = frozenset(
    {"name", "procedure", "revision", "effective", "machines", "labels", "steps", "history"}
)
_MACHINES: Final = frozenset({"label", "separator", "namespace"})
_HISTORY: Final = frozenset({"header", "date", "description"})


def procedure_config(config: Mapping[str, JsonValue] | None = None) -> dict[str, JsonValue]:
    """``config`` with its default (no documents) filled in; unknown keys are kept for
    ``parse_config`` to refuse."""
    out: dict[str, JsonValue] = {"documents": [], **dict(config or {})}
    return canonical_json.loads(canonical_json.dumps(out))  # type: ignore[return-value]


def _label(value: object, what: str) -> str:
    if not isinstance(value, str) or not is_declared_value(value) or "\n" in value:
        raise _Bad(f"{what} is one line of text, not blank or padded")
    return value


def _keys(value: object, allowed: frozenset[str], required: set[str], what: str) -> dict:  # type: ignore[type-arg]
    if not isinstance(value, dict):
        raise _Bad(f"{what} must be an object")
    extra = sorted(set(value) - allowed)
    if extra:
        raise _Bad(f"{what} has unexpected keys {extra}")
    missing = sorted(required - set(value))
    if missing:
        raise _Bad(f"{what} lacks {missing}")
    return value


def _document(value: object, index: int) -> DocumentSpec:
    what = f"documents[{index}]"
    doc = _keys(value, _DOC, {"name", "procedure", "revision", "effective"}, what)
    machines = doc.get("machines")
    label = separator = namespace = None
    if machines is not None:
        m = _keys(machines, _MACHINES, {"label", "namespace"}, f"{what}.machines")
        label = _label(m["label"], f"{what}.machines.label")
        namespace = _label(m["namespace"], f"{what}.machines.namespace")
        LogicalId(namespace, "x")  # a namespace the compiler accepts, or ValueError
        separator = m.get("separator", ", ")
        if not isinstance(separator, str) or not separator or "\n" in separator:
            raise _Bad(f"{what}.machines.separator is non-empty text on one line")
    labels = doc.get("labels", [])
    if not isinstance(labels, list):
        raise _Bad(f"{what}.labels must be a list")
    history = None
    if doc.get("history") is not None:
        h = _keys(doc["history"], _HISTORY, {"header", "date", "description"}, f"{what}.history")
        header = h["header"]
        if not isinstance(header, list) or not header:
            raise _Bad(f"{what}.history.header is a non-empty list of column names")
        names = tuple(_label(c, f"{what}.history.header[]") for c in header)
        if len(set(names)) != len(names):
            raise _Bad(f"{what}.history.header names a column twice")
        date, description = (_label(h[k], f"{what}.history.{k}") for k in ("date", "description"))
        if date not in names or description not in names or date == description:
            raise _Bad(f"{what}.history: date and description are two columns of its header")
        history = History(names, date, description)
    steps = doc.get("steps")
    return DocumentSpec(
        name=_label(doc["name"], f"{what}.name"),
        procedure=_label(doc["procedure"], f"{what}.procedure"),
        revision=_label(doc["revision"], f"{what}.revision"),
        effective=_label(doc["effective"], f"{what}.effective"),
        machines_label=label,
        machines_separator=separator or ", ",
        machines_namespace=namespace,
        labels=tuple(_label(v, f"{what}.labels[]") for v in labels),
        steps=None if steps is None else _label(steps, f"{what}.steps"),
        history=history,
    )


def parse_config(config: Mapping[str, JsonValue]) -> tuple[list[DocumentSpec], list[str]]:
    """The usable declarations of a resolved config, and a problem for each part refused."""
    problems: list[str] = []
    extra = sorted(set(config) - _TOP)
    if extra:
        problems.append(f"unexpected keys {extra}; ignored")
    raw = config.get("documents", [])
    if not isinstance(raw, list):
        return [], [*problems, "documents must be a list; no document is read"]
    specs: list[DocumentSpec] = []
    for index, value in enumerate(raw):
        try:
            spec = _document(value, index)
        except (_Bad, ValueError, TypeError) as exc:
            problems.append(f"{exc}; declaration ignored")
            continue
        if any(s.name == spec.name for s in specs):
            problems.append(
                f"documents[{index}]: the name {spec.name!r} is declared twice; ignored"
            )
            continue
        specs.append(spec)
    return specs, problems


# --- Reading the Ledger ---------------------------------------------------------------------------

_READERS: Final[Mapping[str, Callable[[JsonValue], object]]] = {
    "document_block": document_block_from_json,
    "document_record": document_record_from_json,
    "machine": machine_from_json,
    "structured_record": structured_record_from_json,
    "structured_table": structured_table_from_json,
}


@dataclass
class _View:
    records: dict[str, dict[RecordId, object]]
    findings: list[ConsolidationFinding]

    def of(self, kind: str, cls: type[_T]) -> list[_T]:
        found = self.records.get(kind, {})
        return [r for _, r in sorted(found.items()) if isinstance(r, cls)]


def _read(ledger: LedgerReader) -> _View:
    """Every record of the kinds read, by id; one id with two contents is dropped (a finding)."""
    view = _View({}, [])
    conflicted: set[tuple[str, RecordId]] = set()
    for ref in ledger.list_packages():
        for kind, reader in _READERS.items():
            for index, record in enumerate(ledger.read_records(ref.package_id, kind) or ()):
                try:
                    parsed = _strict(reader, record)
                except Inferred:
                    continue
                except Malformed as exc:
                    view.findings.append(
                        _finding(
                            "malformed_record",
                            f"{kind} record {index} in package {ref.package_id!r} is malformed:"
                            f" {str(exc).encode('utf-8', 'replace').decode('utf-8')}",
                            severity=Severity.ERROR,
                            index=index,
                            kind=kind,
                            package_id=ref.package_id,
                        )
                    )
                    continue
                rid: RecordId = parsed.id  # type: ignore[attr-defined]
                if (kind, rid) in conflicted:
                    continue
                seen = view.records.setdefault(kind, {})
                if seen.setdefault(rid, parsed) != parsed:
                    conflicted.add((kind, rid))
                    del seen[rid]
                    view.findings.append(
                        _finding(
                            "record_conflict",
                            "one record id carries different content in two places; not used",
                            (rid,),
                            Severity.ERROR,
                            kind=kind,
                        )
                    )
    return view


# --- Reading one document -------------------------------------------------------------------------


def calendar(source: str) -> RecordId:
    """The clock a document's dates are on: whole days as written, keyed by its bytes (§6)."""
    return record_id(
        CALENDAR_KIND,
        {
            "resolution": {"denominator": DAY.denominator, "numerator": DAY.numerator},
            "source": source,
        },
    )


def _day(text: str) -> int | None:
    """Days since 1970-01-01 of an ISO calendar date written ``YYYY-MM-DD``; ``None`` otherwise."""
    if not _DATE.fullmatch(text):
        return None
    try:
        return (_dt.date.fromisoformat(text) - _EPOCH).days
    except ValueError:
        return None


def _sub(evidence: EvidenceRef, start: int, end: int) -> EvidenceRef:
    """``evidence`` narrowed to ``[start, end)`` of its last span; as given when it has none."""
    last = evidence.locator[-1]
    if not isinstance(last, Span) or not 0 <= start <= end <= last.end - last.start:
        return evidence
    return EvidenceRef(
        evidence.source, (*evidence.locator[:-1], Span(last.start + start, last.start + end))
    )


@dataclass(frozen=True)
class _Label:
    """One ``<label>:`` line: its value (``None`` when blank), the value's citation (the line's,
    when blank) and the block it is in."""

    value: str | None
    evidence: EvidenceRef
    block: RecordId


def _labels(blocks: Sequence[DocumentBlock], wanted: Sequence[str]) -> dict[str, list[_Label]]:
    out: dict[str, list[_Label]] = defaultdict(list)
    for block in blocks:
        if not (
            isinstance(block.role, Known)
            and block.role.value is BlockRole.PARAGRAPH
            and isinstance(block.text, Known)
        ):
            continue
        offset = 0
        for line in block.text.value.split("\n"):
            for label in wanted:
                prefix = f"{label}:"
                if not line.startswith(prefix):
                    continue
                rest = line[len(prefix) :]
                value = rest.strip()
                if value:
                    start = offset + len(prefix) + (len(rest) - len(rest.lstrip()))
                    cited = _sub(block.provenance.evidence, start, start + len(value))
                    out[label].append(_Label(value, cited, block.id))
                else:
                    cited = _sub(block.provenance.evidence, offset, offset + len(line))
                    out[label].append(_Label(None, cited, block.id))
            offset += len(line) + 1
    return out


def _steps(blocks: Sequence[DocumentBlock], heading: str) -> list[DocumentBlock] | None:
    """The list items under the one heading whose text is ``heading``, up to the next heading at
    its level or above; ``None`` when the document shows that heading twice."""
    found = [
        i
        for i, b in enumerate(blocks)
        if isinstance(b.role, Known)
        and b.role.value is BlockRole.HEADING
        and isinstance(b.text, Known)
        and b.text.value == heading
    ]
    if len(found) > 1:
        return None
    if not found:
        return []
    top = blocks[found[0]]
    level = top.level.value if isinstance(top.level, Known) else None
    items: list[DocumentBlock] = []
    for block in blocks[found[0] + 1 :]:
        role = block.role.value if isinstance(block.role, Known) else None
        if role is BlockRole.HEADING:
            other = block.level.value if isinstance(block.level, Known) else None
            if level is None or other is None or other <= level:
                break
        if role is BlockRole.LIST_ITEM and isinstance(block.text, Known):
            items.append(block)
    return items


def _text(value: str) -> TypedLiteral:
    return TypedLiteral(ValueType.TEXT, value)


def _declared(path: tuple[str | int, ...], value: CellValue) -> TypedLiteral:
    """A cell or label value as a ``declared_value`` literal, typed as its record types it."""
    if isinstance(value, str):
        return TypedLiteral(ValueType.DECLARED_VALUE, DeclaredValue(path, DeclaredType.TEXT, value))
    if isinstance(value, bool):
        literal = DeclaredValue(path, DeclaredType.BOOLEAN, value)
        return TypedLiteral(ValueType.DECLARED_VALUE, literal)
    kind = DeclaredType.INTEGER if isinstance(value, int) else DeclaredType.REAL
    literal = DeclaredValue(path, kind, value)
    return TypedLiteral(ValueType.DECLARED_VALUE, literal, Unknown())  # a number states no unit


@dataclass
class _Fact:
    predicate: str
    obj: ClaimObject
    evidence: tuple[EvidenceRef, ...] = ()
    records: tuple[RecordId, ...] = ()


@dataclass
class _Event:
    node: NodeRef
    day: int
    clock: RecordId
    evidence: tuple[EvidenceRef, ...]  # where its date is stated: every claim cites it
    records: tuple[RecordId, ...]
    facts: list[_Fact] = field(default_factory=list)

    def drafts(self) -> list[ClaimDraft]:
        start, end = Timestamp(self.day, self.clock), Timestamp(self.day + 1, self.clock)
        return [
            ClaimDraft(
                subject=self.node,
                predicate=fact.predicate,
                object=fact.obj,
                valid_from=start,
                valid_to=end,
                assertion_kind=STATED,
                evidence=(*self.evidence, *fact.evidence),
                records=(*self.records, *fact.records),
            )
            for fact in self.facts
        ]


def event_node(record: RecordId, *path: str | int) -> NodeRef:
    value = "/".join((record, *(str(step) for step in path)))
    return node_ref(NodeType.EVENT, LogicalId(RECORD_NAMESPACE, value))


def _cell_evidence(slot: Knowledge[CellValue], row: StructuredRecord) -> EvidenceRef:
    provenance = getattr(slot, "provenance", None)
    return provenance.evidence if isinstance(provenance, Provenance) else row.provenance.evidence


class _Builder:
    def __init__(self, view: _View) -> None:
        self.view = view
        self.findings: list[ConsolidationFinding] = []
        self.counts: dict[str, set[RecordId]] = defaultdict(set)
        self.machines: dict[tuple[str, str], NodeRef] = {}
        for machine in view.of("machine", Machine):
            for slot in machine.identifiers:
                if isinstance(slot, Known) and is_declared_value(slot.value.value):
                    key = (slot.value.namespace, slot.value.value)
                    self.machines[key] = node_ref(NodeType.MACHINE, slot.value)
        self.blocks: dict[RecordId, list[DocumentBlock]] = defaultdict(list)
        for block in view.of("document_block", DocumentBlock):
            self.blocks[block.document].append(block)
        for blocks in self.blocks.values():
            blocks.sort(key=lambda b: (b.order, b.id))
        self.tables: dict[str, list[StructuredTable]] = defaultdict(list)
        for table in view.of("structured_table", StructuredTable):
            source = table.provenance.evidence.source
            if isinstance(source, str):
                self.tables[source].append(table)
        self.rows: dict[RecordId, list[StructuredRecord]] = defaultdict(list)
        for row in view.of("structured_record", StructuredRecord):
            self.rows[row.table].append(row)
        for rows in self.rows.values():
            rows.sort(key=lambda r: (r.row, r.id))

    def note(self, code: str, record: RecordId) -> None:
        self.counts[code].add(record)

    def document(self, doc: DocumentRecord, spec: DocumentSpec) -> list[_Event] | None:
        """The events one document states under ``spec``; ``None`` when it is not one of them."""
        blocks = self.blocks.get(doc.id, [])
        read = _labels(blocks, spec.read_labels)
        if not read.get(spec.procedure) or not read.get(spec.revision):
            return None
        single: dict[str, _Label] = {}
        for label, found in read.items():
            if len({f.value for f in found}) > 1:
                self.note("label_repeated", doc.id)
                return []
            single[label] = found[0]
        name, revision = single[spec.procedure], single[spec.revision]
        if name.value is None or revision.value is None:
            self.note("label_blank", doc.id)
            return []
        source = doc.provenance.evidence.source
        if not isinstance(source, str):
            return []
        effective = single.get(spec.effective)
        if effective is None or effective.value is None:
            self.note("untimed", doc.id)
            return []
        day = _day(effective.value)
        if day is None:
            self.note("date_unreadable", doc.id)
            return []
        clock = calendar(source)
        node = event_node(doc.id)
        event = _Event(node, day, clock, (effective.evidence,), (doc.id, effective.block))
        event.facts += [
            _Fact(EVENT_KIND, _text(PROCEDURE_REVISION)),
            _Fact(EVIDENCED_BY, LedgerRecordRef(doc.id)),
            _Fact(HAS_NAME, _text(name.value), (name.evidence,), (name.block,)),
        ]
        machines = self.involved(doc, spec, single)
        event.facts += machines
        for label in spec.read_labels:
            got = single.get(label)
            if got is None:
                continue
            if got.value is None:
                event.facts.append(_Fact(STATED_BLANK, _text(label), (got.evidence,), (got.block,)))
                continue
            literal = _declared((label,), got.value)
            event.facts.append(_Fact(DECLARED_VALUE, literal, (got.evidence,), (got.block,)))
        if spec.steps is not None:
            steps = _steps(blocks, spec.steps)
            if steps is None:
                self.note("section_repeated", doc.id)
            for index, step in enumerate(steps or ()):
                assert isinstance(step.text, Known)
                literal = _declared((spec.steps, index), step.text.value)
                event.facts.append(
                    _Fact(DECLARED_VALUE, literal, (step.provenance.evidence,), (step.id,))
                )
        events = [event]
        if spec.history is not None:
            events += self.history(doc, spec.history, source, clock, machines)
        return events

    def involved(
        self, doc: DocumentRecord, spec: DocumentSpec, single: Mapping[str, _Label]
    ) -> list[_Fact]:
        if spec.machines_label is None or spec.machines_namespace is None:
            return []
        got = single.get(spec.machines_label)
        if got is None or got.value is None:
            return []
        facts: list[_Fact] = []
        for item in got.value.split(spec.machines_separator):
            node = self.machines.get((spec.machines_namespace, item.strip()))
            if node is None:
                self.note("reference_unresolved", doc.id)
                continue
            facts.append(_Fact(INVOLVES, node, (got.evidence,), (got.block,)))
        return facts

    def history(
        self,
        doc: DocumentRecord,
        spec: History,
        source: str,
        clock: RecordId,
        machines: Sequence[_Fact],
    ) -> list[_Event]:
        tables = [
            t
            for t in self.tables.get(source, [])
            if isinstance(t.header, Known) and t.header.value == spec.header
        ]
        if len(tables) > 1:
            self.note("table_repeated", doc.id)
            return []
        if not tables:
            return []
        header = spec.header
        events: list[_Event] = []
        for index, row in enumerate(self.rows.get(tables[0].id, [])):
            cells = dict(zip(header, row.cells, strict=False))
            date = cells.get(spec.date)
            day = (
                _day(date.value)
                if isinstance(date, Known) and isinstance(date.value, str)
                else None
            )
            if date is None or day is None:
                self.note("row_untimed", row.id)
                continue
            node = event_node(doc.id, REVISIONS, index)
            event = _Event(node, day, clock, (_cell_evidence(date, row),), (doc.id, row.id))
            event.facts.append(_Fact(EVIDENCED_BY, LedgerRecordRef(row.id)))
            event.facts += machines
            for column, slot in cells.items():
                cited = (_cell_evidence(slot, row),)
                if isinstance(slot, Unknown | KnownAbsent):
                    event.facts.append(_Fact(STATED_BLANK, _text(column), cited))
                elif not isinstance(slot, Known):
                    self.note("value_ambiguous", row.id)
                elif column == spec.description and isinstance(slot.value, str):
                    event.facts.append(_Fact(HAS_DESCRIPTION, _text(slot.value), cited))
                else:
                    event.facts.append(
                        _Fact(DECLARED_VALUE, _declared((column,), slot.value), cited)
                    )
            events.append(event)
        return events

    def summarise(self) -> list[ConsolidationFinding]:
        messages = {
            "label_repeated": "a label is shown twice with different values: document not read",
            "label_blank": "the procedure or revision label is blank: document not read",
            "untimed": "a procedure document states no effective date: no claim (Memory gives no"
            " time to what states none)",
            "date_unreadable": "an effective date is not written YYYY-MM-DD: no claim",
            "reference_unresolved": "an applies-to item is no machine id a machine record states"
            " in the declared namespace: it joins nothing",
            "section_repeated": "the steps heading is shown twice: no step is read",
            "table_repeated": "two tables have the revision-history header: no row is read",
            "row_untimed": "a revision-history row's date is blank or not YYYY-MM-DD: no claim",
            "value_ambiguous": "a cell is ambiguous: not a value",
        }
        out = list(self.findings)
        for code, records in sorted(self.counts.items()):
            out.append(_finding(code, messages[code], records, count=len(records)))
        return out


class ProcedureConsolidator:
    """Deterministic procedure-revision claims (ADR 0027); config: ``procedure_config``'s."""

    consolidator_id: Final = PROCEDURES_CONSOLIDATOR_ID
    version: Final = "1"
    model: Final[ModelRef | None] = None

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        specs, problems = parse_config(config)
        findings = [
            _finding("invalid_config", problem, severity=Severity.ERROR) for problem in problems
        ]
        if not specs:
            return ConsolidatorOutput((), tuple(findings))
        view = _read(ledger)
        builder = _Builder(view)
        drafts: list[ClaimDraft] = []
        for doc in view.of("document_record", DocumentRecord):
            matched = [(s, e) for s in specs if (e := builder.document(doc, s)) is not None]
            if len(matched) > 1:
                builder.note("declaration_ambiguous", doc.id)
                continue
            for _, events in matched:
                for event in events:
                    drafts += event.drafts()
        if builder.counts.get("declaration_ambiguous"):
            builder.findings.append(
                _finding(
                    "declaration_ambiguous",
                    "two declarations read one document: neither is used",
                    builder.counts.pop("declaration_ambiguous"),
                    Severity.WARNING,
                )
            )
        return ConsolidatorOutput(tuple(drafts), (*view.findings, *findings, *builder.summarise()))

"""The lifecycle mapper: a compiler package's tables, through declared mappings, into lifecycle
records in a new package (ADR 0002).

It reads ``StructuredTable`` and ``StructuredRecord`` records only, never a source's bytes. Each
mapping file is one transform; each row one rule matches is one record, whose provenance is the
row's and whose every value cites its cell. What does not map is a finding (ADR 0002 §6).
"""

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from neptune.identity import canonical_json
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ContentId, LogicalId, RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import (
    EvidenceRef,
    JsonPointer,
    Provenance,
    Row,
    Span,
    TransformRecord,
)
from neptune.model.reference import TimestampDomain
from neptune.model.scalars import NonFinite
from neptune.model.time import ClockRole, Epoch, Timescale, Timestamp
from neptune.model.units import UncataloguedUnitError, unit_from_text
from neptune.model.versions import BuildId, DeclaredVersion, FirmwareVersion
from neptune.model.world import StructuredRecord, StructuredTable
from neptune.store.package import IngestPackage
from neptune_deploy.lifecycle.mapping import (
    LifecycleMapping,
    ListCell,
    MappingError,
    Part,
    Rule,
    Scalar,
    config_of,
    match_pattern,
    spec_columns,
)
from neptune_deploy.lifecycle.shapes import Shape, fields_of
from neptune_deploy.lifecycle.times import read_time

MAPPER_ID: Final = "deploy_lifecycle_map"
MAPPER_VERSION: Final = "0.1.0"
STATED: Final = AssertionKind.STATED
LEDGER_KINDS: Final = frozenset({"source_artifact", "source_revision", "source_absence"})
# How many rows and records one grouped finding names (as root ADR 0042 §10 does).
NAMED: Final = 10
_DECIMAL: Final = re.compile(r"[+-]?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_VERSIONS: Final = {"build": BuildId, "declared": DeclaredVersion, "firmware": FirmwareVersion}


def code(name: str) -> str:
    return f"{MAPPER_ID}.{name}"


# Every finding code: severity, category and what it means (ADR 0002 §6).
FINDINGS: Final[dict[str, tuple[Severity, FindingCategory, str]]] = {
    "row_unmatched": (
        Severity.WARNING,
        FindingCategory.UNSUPPORTED,
        "rows of a mapped table that no rule of the mapping applies to; they have no lifecycle"
        " record",
    ),
    "rule_ambiguous": (
        Severity.ERROR,
        FindingCategory.AMBIGUOUS,
        "rows that more than one rule applies to; none was picked, so they have no lifecycle"
        " record",
    ),
    "column_unmapped": (
        Severity.WARNING,
        FindingCategory.UNSUPPORTED,
        "columns the mapping neither maps nor ignores; they stay in the base package's rows only",
    ),
    "column_absent": (
        Severity.INFO,
        FindingCategory.MISSING,
        "columns the mapping reads that the table does not have; their fields are not covered",
    ),
    "column_repeated": (
        Severity.WARNING,
        FindingCategory.AMBIGUOUS,
        "columns whose header text repeats; the fields read from them are unknown",
    ),
    "value_unreadable": (
        Severity.WARNING,
        FindingCategory.INCONSISTENT,
        "cells that do not read as their field's declared shape or format; the fields are unknown",
    ),
    "value_blank": (
        Severity.WARNING,
        FindingCategory.MISSING,
        "blank cells in columns the mapping declares required; the fields are unknown",
    ),
    "list_cell_blank": (
        Severity.WARNING,
        FindingCategory.MISSING,
        "a blank cell read into a list field: a list holds no unknown, so this record's list lacks"
        " what the cell would have stated; the list is not a statement of none",
    ),
    "list_part_empty": (
        Severity.INFO,
        FindingCategory.MISSING,
        "an empty part between declared delimiters in a list cell; it states nothing and is not"
        " listed",
    ),
    "list_id_repeated": (
        Severity.INFO,
        FindingCategory.INCONSISTENT,
        "an identifier stated again in a record's list; a declared-id list holds each once, so the"
        " first statement is kept",
    ),
    "item_blank": (
        Severity.INFO,
        FindingCategory.MISSING,
        "parts whose every cell is blank in the row (no part swapped, no test); none is listed",
    ),
    "identifier_repeated": (
        Severity.WARNING,
        FindingCategory.INCONSISTENT,
        "records of one mapping stating the same identifier; they are kept apart, never merged",
    ),
    "record_unrepresentable": (
        Severity.ERROR,
        FindingCategory.UNREPRESENTABLE,
        "rows whose mapped values the lifecycle kind refuses; they have no lifecycle record",
    ),
    "table_unmapped": (
        Severity.INFO,
        FindingCategory.UNSUPPORTED,
        "tables no mapping applies to; they have no lifecycle records",
    ),
    "header_undeclared": (
        Severity.WARNING,
        FindingCategory.MISSING,
        "tables whose header row the compiler was not told of (csv_header); no mapping can name"
        " their columns",
    ),
}


# --- Tables --------------------------------------------------------------------------------------


def _grounds(state: Knowledge[Any]) -> Provenance | None:
    """The explicit provenance a cell's state carries, if any."""
    slot: object = None
    if isinstance(state, Ambiguous):
        slot = state.candidates[0].provenance
    elif not isinstance(state, NotApplicable):
        slot = state.provenance
    return slot if isinstance(slot, Provenance) else None


@dataclass
class _Table:
    """A table and its rows, with each row's columns by name (header text or JSON pointer)."""

    record: StructuredTable
    rows: list[StructuredRecord]
    header: tuple[str, ...] | None  # a headed table's header; None for a table of JSON objects
    columns: dict[str, int | None] = field(default_factory=dict)  # name -> index, None if repeated
    pointers: list[dict[str, int]] = field(default_factory=list)  # JSON: per row
    # Every column name in first-seen order, computed once: lookups never scan the rows.
    ordered: tuple[str, ...] = ()
    present: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.header is not None:
            seen = dict.fromkeys(name for name in self.header if name)
        else:
            seen = {}
            for row in self.pointers:
                seen.update(dict.fromkeys(row))
        self.ordered = tuple(seen)
        self.present = frozenset(seen)

    @property
    def evidence(self) -> EvidenceRef:
        return self.record.provenance.evidence

    def has(self, column: str) -> bool:
        return column in self.present

    def names(self) -> list[str]:
        return list(self.ordered)


def _json_pointer(cell: Knowledge[Any]) -> str | None:
    grounds = _grounds(cell)
    if grounds is not None and isinstance(grounds.evidence.locator[-1], JsonPointer):
        return grounds.evidence.locator[-1].pointer
    return None


def _table(record: StructuredTable, rows: list[StructuredRecord]) -> _Table | None:
    """The table, or ``None`` when its columns have no names a mapping could use."""
    rows.sort(key=lambda row: row.row)
    if isinstance(record.header, Known):
        table = _Table(record, rows, record.header.value)
        for index, name in enumerate(record.header.value):
            if name:
                table.columns[name] = None if name in table.columns else index
        return table
    pointers: list[dict[str, int]] = []
    for row in rows:
        named: dict[str, int] = {}
        for index, cell in enumerate(row.cells):
            pointer = _json_pointer(cell)
            if pointer is None or row.provenance.evidence.locator[-1] == Row(row.row):
                return None
            named.setdefault(pointer, index)
        pointers.append(named)
    if not isinstance(record.header, NotApplicable) or not rows:
        return None
    return _Table(record, rows, None, pointers=pointers)


# --- Findings ------------------------------------------------------------------------------------


@dataclass
class _Group:
    subject: EvidenceRef
    details: dict[str, JsonValue]
    rows: list[int] = field(default_factory=list)
    records: list[RecordId] = field(default_factory=list)
    related: list[EvidenceRef] = field(default_factory=list)
    count: int = 0


class _Findings:
    """Findings grouped by code, table and detail: one finding names its first rows and records."""

    def __init__(
        self,
        catalog: dict[str, tuple[Severity, FindingCategory, str]] | None = None,
        producer: str = MAPPER_ID,
        scope: str = "table",
        field_key: str = "column",
    ) -> None:
        self.catalog = FINDINGS if catalog is None else catalog
        self.producer, self.scope, self.field_key = producer, scope, field_key
        self.groups: dict[tuple[str, str, str], _Group] = {}

    def add(
        self,
        name: str,
        table: Any,
        subject: EvidenceRef,
        *,
        key: str = "",
        details: dict[str, JsonValue] | None = None,
        row: int | None = None,
        record: RecordId | None = None,
        related: Sequence[EvidenceRef] = (),
    ) -> None:
        group = self.groups.setdefault(
            (name, table.record.id, key),
            _Group(subject, {self.scope: table.record.id, **(details or {})}),
        )
        group.count += 1
        if row is not None and len(group.rows) < NAMED:
            group.rows.append(row)
        if record is not None and len(group.records) < NAMED:
            group.records.append(record)
        for ref in related:
            if len(group.related) < NAMED and ref not in group.related and ref != group.subject:
                group.related.append(ref)

    def once(self, name: str, table: Any, subject: EvidenceRef, column: str) -> None:
        """A finding about a table's column, made once however many rows read it."""
        if (name, table.record.id, column) not in self.groups:
            self.add(name, table, subject, key=column, details={self.field_key: column})

    def build(self, transform: TransformRecord) -> list[IngestFinding]:
        out = []
        for (name, _, _), group in sorted(self.groups.items()):
            severity, category, message = self.catalog[name]
            details = dict(group.details)
            details["count"] = group.count
            if group.rows:
                details["rows"] = list(group.rows)
            out.append(
                ingest_finding(
                    code=f"{self.producer}.{name}",
                    category=category,
                    severity=severity,
                    subject=group.subject,
                    transform=transform,
                    message=message,
                    details=details,
                    related=tuple(group.related),
                    records=group.records,
                )
            )
        return out


# --- Values --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Cell:
    """What one column holds in one row: a state and where it is; ``None`` state = absent."""

    state: Knowledge[Any] | None
    place: EvidenceRef
    absent_from_table: bool = False


class _Values:
    """Values read into fields, one record at a time, under one transform: the shapes of ADR 0002 §3
    and §4 over ``cell``, which a subclass says how to find. A table row (``_Row``) and a document
    (``documents._DocRow``) differ only in what a reference names."""

    mapper: Any  # has ``transform``, ``findings`` and ``domain``
    table: Any  # has ``record`` (its ``id`` scopes findings and clocks) and ``evidence``
    evidence: EvidenceRef  # the record's own evidence
    record_id: RecordId  # the id the lifecycle record will have: findings about its values name it
    pointers: dict[str, int] | None = None

    def cell(self, column: str, via: str = "column") -> _Cell:
        raise NotImplementedError

    def finding(self, name: str, column: str, subject: EvidenceRef) -> None:
        raise NotImplementedError

    def cell_finding(self, name: str, column: str, path: str, subject: EvidenceRef) -> None:
        raise NotImplementedError

    def blank(self, spec: Part) -> bool:
        raise NotImplementedError

    def provenance(self, evidence: EvidenceRef) -> Provenance:
        return Provenance(evidence, self.mapper.transform.id, STATED)

    def label(self, column: str) -> str:
        return column

    # One cell into one field ---------------------------------------------------------------

    def scalar(self, shape: Shape, spec: Scalar, path: str) -> Knowledge[Any]:
        cell = self.cell(spec.column, spec.via)
        if cell.absent_from_table:
            return NotCovered()
        state, place = cell.state, cell.place
        if state is None or isinstance(state, Unknown):
            if spec.required:
                self.finding("value_blank", spec.column, place)
            return Unknown(self.provenance(place))
        if isinstance(state, KnownAbsent):
            grounds = _grounds(state)
            return KnownAbsent(self.provenance(grounds.evidence if grounds else place))
        if isinstance(state, NotCovered):
            return NotCovered(self.provenance(place))
        if not isinstance(state, Known):
            self.cell_finding("value_unreadable", spec.column, path, place)
            return Unknown(self.provenance(place))
        provenance = self.provenance(place)
        value = self._convert(shape, spec, state.value, place, provenance)
        if value is None:
            self.cell_finding("value_unreadable", spec.column, path, place)
            return Unknown(provenance)
        return value

    def _convert(
        self,
        shape: Shape,
        spec: Scalar,
        value: Any,
        place: EvidenceRef,
        provenance: Provenance,
    ) -> Knowledge[Any] | None:
        if shape is Shape.NUMBER:
            number = _number(value)
            return None if number is None else Known(number, provenance)
        text = _text(value)
        if text is None:
            return None
        try:
            match shape:
                case Shape.TEXT:
                    return Known(text, provenance)
                case Shape.ID:
                    assert spec.namespace is not None
                    return Known(LogicalId(spec.namespace, text), provenance)
                case Shape.VERSION:
                    assert spec.scheme is not None
                    return Known(_VERSIONS[spec.scheme](text), provenance)
                case Shape.UNIT:
                    return unit_from_text(text, provenance=provenance)
                case Shape.TIME:
                    return self._time(spec, text, place, provenance)
        except (ValueError, TypeError, UncataloguedUnitError):
            return None
        raise AssertionError(shape)

    def _time(
        self, spec: Scalar, text: str, place: EvidenceRef, provenance: Provenance
    ) -> Knowledge[Timestamp] | None:
        reading = read_time(text, spec.formats)
        if reading is None:
            return None
        assert spec.zone is not None
        domain = self.mapper.domain(
            self.table.record.id,
            spec.column,
            reading.instant,
            reading.resolution,
            spec.zone,
            place,
        )
        return Known(Timestamp(reading.ticks, domain), provenance)

    # Cells into lists ----------------------------------------------------------------------

    def pieces(self, spec: ListCell, path: str) -> list[tuple[str, EvidenceRef]]:
        """The texts a list cell states, each with its citation (a span inside a split cell)."""
        cell = self.cell(spec.column, spec.via)
        if cell.absent_from_table or isinstance(cell.state, KnownAbsent):
            return []
        if cell.state is None or isinstance(cell.state, Unknown | NotCovered):
            self.cell_finding("list_cell_blank", spec.column, path, cell.place)
            return []
        text = _text(cell.state.value) if isinstance(cell.state, Known) else None
        if text is None:
            self.cell_finding("value_unreadable", spec.column, path, cell.place)
            return []
        if spec.split is None:
            return [(text, cell.place)]
        out = []
        start = 0
        for part in text.split(spec.split):
            stripped = part.strip()
            if not stripped:
                self.cell_finding("list_part_empty", spec.column, path, cell.place)
            else:
                begin = start + (len(part) - len(part.lstrip()))
                end = begin + len(stripped)
                place = cell.place
                if (begin, end) != (0, len(text)):
                    place = EvidenceRef(place.source, (*place.locator, Span(begin, end)))
                out.append((stripped, place))
            start += len(part) + len(spec.split)
        return out

    def ids(self, specs: tuple[ListCell, ...], path: str) -> tuple[Knowledge[LogicalId], ...]:
        """Declared ids, sorted, each once: a repeat is kept once and is a finding."""
        found: dict[LogicalId, Knowledge[LogicalId]] = {}
        for spec in specs:
            assert spec.namespace is not None
            for text, place in self.pieces(spec, path):
                identifier = LogicalId(spec.namespace, text)
                if identifier in found:
                    self.cell_finding("list_id_repeated", spec.column, path, place)
                    continue
                found[identifier] = Known(identifier, self.provenance(place))
        return tuple(found[key] for key in sorted(found, key=lambda i: (i.namespace, i.value)))

    def statements(self, specs: tuple[ListCell, ...], path: str) -> tuple[Knowledge[str], ...]:
        return tuple(
            Known(text, self.provenance(place))
            for spec in specs
            for text, place in self.pieces(spec, path)
        )

    # Parts and records ---------------------------------------------------------------------

    def part(self, spec: Part, path: str) -> Any:
        if spec.score is not None:
            return spec.cls(
                name=self.label(spec.score.column),
                value=self.scalar(Shape.TEXT, spec.score, f"{path}/value"),
            )
        return spec.cls(**self.values(spec.cls, spec.fields, path))

    def items(self, specs: Any, path: str) -> tuple[Any, ...]:
        """Parts spelled out field by field; one whose every cell is blank is not listed."""
        items: list[Any] = []
        for item in specs:
            assert isinstance(item, Part)
            if self.blank(item):
                column = ", ".join(sorted(spec_columns(item)))
                self.finding("item_blank", column, self.evidence)
                continue
            items.append(self.part(item, f"{path}/{len(items)}"))
        return tuple(items)

    def values(self, cls: type[Any], specs: Any, path: str = "") -> dict[str, Any]:
        """Every field of ``cls``; ``path`` is where ``cls`` sits in the record (JSON pointer)."""
        out: dict[str, Any] = {}
        for shape in fields_of(cls):
            spec: Any = specs.get(shape.name)
            at = f"{path}/{shape.name}"
            match shape.shape:
                case Shape.IDS:
                    out[shape.name] = self.ids(spec, at) if spec else ()
                case Shape.STATEMENTS:
                    out[shape.name] = self.statements(spec, at) if spec else ()
                case Shape.ITEMS:
                    out[shape.name] = self.items(spec or (), at)
                case Shape.PART:
                    assert shape.part is not None
                    out[shape.name] = self.part(
                        spec if isinstance(spec, Part) else Part(shape.part, {}), at
                    )
                case _:
                    out[shape.name] = (
                        self.scalar(shape.shape, spec, at)
                        if isinstance(spec, Scalar)
                        else NotCovered()
                    )
        return out


class _Row(_Values):
    """One row being mapped by one rule under one transform."""

    def __init__(
        self,
        mapper: Any,
        table: _Table,
        index: int,
        kind: str,
        record_id: RecordId | None = None,
    ) -> None:
        self.mapper, self.table = mapper, table
        self.record = table.rows[index]
        self.pointers = table.pointers[index] if table.header is None else None
        self.evidence = self.record.provenance.evidence
        # The id the lifecycle record will have: findings about its cells name it.
        self.record_id = record_id or evidence_record_id(kind, self.evidence, mapper.transform)

    def finding(self, name: str, column: str, subject: EvidenceRef) -> None:
        self.mapper.findings.add(
            name, self.table, subject, key=column, details={"column": column}, row=self.record.row
        )

    def cell_finding(self, name: str, column: str, path: str, subject: EvidenceRef) -> None:
        """One finding per cell, naming the record and field: never capped, never grouped."""
        self.mapper.findings.add(
            name,
            self.table,
            subject,
            key=f"{self.record.row}|{path}|{column}|{subject.locator_json()}",
            details={"column": column, "field": path},
            row=self.record.row,
            record=self.record_id,
        )

    def cell(self, column: str, via: str = "column") -> _Cell:
        table, record = self.table, self.record
        if not table.has(column):
            self.mapper.findings.once("column_absent", table, table.evidence, column)
            return _Cell(None, self.evidence, absent_from_table=True)
        if self.pointers is not None:
            index: int | None = self.pointers.get(column)
        else:
            index = table.columns[column]
            if index is None:
                self.mapper.findings.once("column_repeated", table, table.evidence, column)
                return _Cell(Unknown(self.provenance(self.evidence)), self.evidence)
        if index is None or index >= len(record.cells):
            return _Cell(None, self.evidence)  # a missing key, or a short row
        return _Cell(record.cells[index], record.cell_evidence(table.record, index))

    def blank(self, spec: Part) -> bool:
        """Every cell the part reads is blank or absent in this row."""
        for column in sorted(spec_columns(spec)):
            if not self.table.has(column):
                continue
            if self.pointers is None and self.table.columns[column] is None:
                return False  # a repeated header: its field is unknown, not blank
            cell = self.cell(column)
            if cell.state is not None and not isinstance(cell.state, Unknown):
                return False
        return True


def _number(value: Any) -> float | NonFinite | None:
    """A cell's value as a double, only when the double holds it exactly (root ADR 0042 §3).

    A typed double (JSON, Parquet) already passed that rule in the compiler. An integer, or a
    decimal literal in text, is read only when the double's shortest digits equal its value:
    ``2^53 + 1`` or ``1e-400`` would become another number, so they are not read.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, float | NonFinite):
        return value
    if isinstance(value, int):
        literal = str(value)
    elif isinstance(value, str) and _DECIMAL.fullmatch(value):
        literal = value
    else:
        return None
    try:
        number = float(literal)
        exact = Decimal(repr(number)) == Decimal(literal)
    except (OverflowError, ValueError, InvalidOperation):
        return None
    return number if exact and math.isfinite(number) else None


def _text(value: Any) -> str | None:
    """A cell's value as text: text as is; a typed integer, boolean or double as its canonical
    JSON text, a lossless rendering (ADR 0002 §4). A non-finite double has no JSON text."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, bool | int) or (isinstance(value, float) and math.isfinite(value)):
        return canonical_json.dumps(value).decode("utf-8")
    return None


# --- The mapper ---------------------------------------------------------------------------------


class _Clocks:
    """The ``TimestampDomain`` of each time field read, one per scope and field (ADR 0002 §5)."""

    transform: TransformRecord
    domains: dict[tuple[Any, ...], TimestampDomain]

    def domain(
        self,
        scope: RecordId,
        column: str,
        instant: bool,
        resolution: Any,
        zone: str,
        place: EvidenceRef,
    ) -> RecordId:
        key = (scope, column, instant, resolution, "" if instant else zone)
        if key not in self.domains:
            provenance = Provenance(place, self.transform.id, STATED)
            self.domains[key] = TimestampDomain(
                id=evidence_record_id(TimestampDomain.kind, place, self.transform),
                provenance=provenance,
                field=column,
                scope=(),  # the declared zone is the mapping's, in the transform config
                role=Known(ClockRole.DOCUMENT),
                resolution=Known(resolution),
                epoch=Known(Epoch.UNIX),
                timescale=Known(Timescale.POSIX) if instant else Unknown(),
                declared_monotonic=NotCovered(),
            )
        return self.domains[key].id


class _Mapper(_Clocks):
    """One mapping applied to one package's tables."""

    def __init__(self, mapping: LifecycleMapping, base: ContentId, tables: list[_Table]) -> None:
        self.mapping = mapping
        self.applicable = {
            table.record.id: [r for r in mapping.rules if all(table.has(c) for c in r.requires)]
            for table in tables
        }
        self.tables = [table for table in tables if self.applicable[table.record.id]]
        upstream = sorted({table.record.provenance.transform for table in self.tables})
        self.transform = transform_record(
            adapter_id=MAPPER_ID,
            adapter_version=MAPPER_VERSION,
            config=config_of(mapping, base),
            upstream=upstream,
        )
        self.findings = _Findings()
        self.domains: dict[tuple[Any, ...], TimestampDomain] = {}

    def run(self) -> list[Any]:
        records: list[Any] = []
        for table in self.tables:
            rules = self.applicable[table.record.id]
            self._unmapped(table, rules)
            for index, row in enumerate(table.rows):
                matched = [rule for rule in rules if self._selects(rule, table, index)]
                if not matched:
                    self.findings.add("row_unmatched", table, row.provenance.evidence, row=row.row)
                elif len(matched) > 1:
                    self.findings.add(
                        "rule_ambiguous",
                        table,
                        row.provenance.evidence,
                        key=",".join(rule.id for rule in matched),
                        details={"rules": [rule.id for rule in matched]},
                        row=row.row,
                    )
                else:
                    record = self._record(matched[0], table, index)
                    if record is not None:
                        records.append(record)
        self._repeated(records)
        domains = {domain.id: domain for domain in self.domains.values()}
        return [
            self.transform,
            *records,
            *domains.values(),
            *self.findings.build(self.transform),
        ]

    def _selects(self, rule: Rule, table: _Table, index: int) -> bool:
        if rule.where is None:
            return True
        row = table.rows[index]
        if table.header is None:
            position = table.pointers[index].get(rule.where.column)
        else:
            position = table.columns.get(rule.where.column)
        if position is None or position >= len(row.cells):
            return False
        cell = row.cells[position]
        return isinstance(cell, Known) and _text(cell.value) in rule.where.values

    def _unmapped(self, table: _Table, rules: list[Rule]) -> None:
        read = set().union(*(rule.columns() for rule in rules))
        ignore = [pattern for rule in rules for pattern in rule.ignore]
        unmapped = [
            name
            for name in table.names()
            if name not in read and not any(match_pattern(p, name) for p in ignore)
        ]
        if unmapped:
            subject = table.evidence
            if isinstance(table.record.header, Known):
                grounds = _grounds(table.record.header)
                subject = grounds.evidence if grounds else subject
            self.findings.add(
                "column_unmapped", table, subject, details={"columns": list(unmapped)}
            )

    def _record(self, rule: Rule, table: _Table, index: int) -> Any:
        row = _Row(self, table, index, rule.kind.kind)
        evidence = row.evidence
        try:
            values = row.values(rule.kind, rule.fields)
            return rule.kind(
                id=row.record_id,
                provenance=Provenance(evidence, self.transform.id, STATED),
                **values,
            )
        except (ValueError, TypeError) as exc:
            self.findings.add(
                "record_unrepresentable",
                table,
                evidence,
                key=rule.id,
                details={"rule": rule.id, "reason": str(exc)[:200]},
                row=table.rows[index].row,
            )
            return None

    def _repeated(self, records: list[Any]) -> None:
        """Two records of this mapping stating one identifier: both kept, one finding."""
        holders: dict[LogicalId, list[Any]] = defaultdict(list)
        for record in records:
            for identifier in record.identifiers:
                if isinstance(identifier, Known):
                    holders[identifier.value].append(record)
        rows = {
            row.provenance.evidence: (table, row) for table in self.tables for row in table.rows
        }
        for identifier, members in sorted(
            holders.items(), key=lambda i: (i[0].namespace, i[0].value)
        ):
            if len(members) < 2:
                continue
            first = members[0].provenance.evidence
            for member in members[1:]:
                table, row = rows[member.provenance.evidence]
                self.findings.add(
                    "identifier_repeated",
                    table,
                    member.provenance.evidence,
                    key=f"{identifier.namespace}:{identifier.value}",
                    details={"identifier": identifier.to_json()},
                    row=row.row,
                    record=member.id,
                    related=(first,),
                )


def tables_of(records: Iterable[Any]) -> tuple[list[_Table], list[StructuredTable]]:
    """The package's tables a mapping can name, and those whose columns have no names."""
    rows: dict[RecordId, list[StructuredRecord]] = defaultdict(list)
    tables: list[StructuredTable] = []
    for record in records:
        if isinstance(record, StructuredRecord):
            rows[record.table].append(record)
        elif isinstance(record, StructuredTable):
            tables.append(record)
    usable, unnamed = [], []
    for table in sorted(tables, key=lambda t: t.id):
        built = _table(table, rows[table.id])
        if built is None:
            unnamed.append(table)
        else:
            usable.append(built)
    return usable, unnamed


def map_tables(
    base: IngestPackage, mappings: Sequence[LifecycleMapping], claimed: Iterable[RecordId] = ()
) -> list[Any]:
    """Each mapping's transform, lifecycle records, clocks and findings, then findings about the
    tables no mapping applies to. ``claimed`` are tables something else (a document template)
    already accounts for."""
    hashes = [mapping.sha256 for mapping in mappings]
    if len(set(hashes)) != len(hashes):
        raise MappingError("the same mapping file is given twice")
    ids = [mapping.id for mapping in mappings]
    if len(set(ids)) != len(ids):
        raise MappingError(f"two mapping files share an id: {ids}")
    taken = set(claimed)
    # A table of a document a template matched is that template's: no mapping reads it again.
    named, nameless = tables_of(base.records)
    usable = [t for t in named if t.record.id not in taken]
    unnamed = [t for t in nameless if t.id not in taken]
    out: list[Any] = []
    for mapping in sorted(mappings, key=lambda m: m.sha256):
        mapper = _Mapper(mapping, base.id, usable)
        taken.update(table.record.id for table in mapper.tables)
        out.extend(mapper.run())
    out.extend(
        _run_findings(
            base.id,
            hashes,
            [t for t in usable if t.record.id not in taken],
            [t for t in unnamed if t.id not in taken],
        )
    )
    return out


def _run_findings(
    base: ContentId,
    hashes: list[ContentId],
    unclaimed: list[_Table],
    unnamed: list[StructuredTable],
) -> list[Any]:
    """Findings about tables no mapping applies to, under a transform of the whole run."""
    if not unclaimed and not unnamed:
        return []
    upstream = sorted(
        {t.record.provenance.transform for t in unclaimed}
        | {t.provenance.transform for t in unnamed}
    )
    transform = transform_record(
        adapter_id=MAPPER_ID,
        adapter_version=MAPPER_VERSION,
        config={"base_package": base, "mappings": sorted(hashes)},
        upstream=upstream,
    )
    findings = _Findings()
    for table in unclaimed:
        findings.add("table_unmapped", table, table.evidence)
    for record in unnamed:
        # Only a header the compiler was not told about is undeclared; a table declared to have
        # none (csv_header none, a Parquet footer's tables) simply has no names to map.
        name = "header_undeclared" if isinstance(record.header, Unknown) else "table_unmapped"
        findings.add(name, _Table(record, [], ()), record.provenance.evidence)
    return [transform, *findings.build(transform)]


def carried(base: IngestPackage, records: list[Any]) -> list[Any]:
    """The base package's source ledger, and every base transform the new records' transforms
    name upstream, with theirs in turn: the new package's lineage is whole."""
    transforms = {r.id: r for r in base.records if r.kind == "transform_record"}
    wanted = [u for r in records if r.kind == "transform_record" for u in r.upstream]
    kept: dict[RecordId, Any] = {}
    while wanted:
        current = wanted.pop()
        if current in kept or current not in transforms:
            continue
        kept[current] = transforms[current]
        wanted.extend(transforms[current].upstream)
    ledger = [r for r in base.records if r.kind in LEDGER_KINDS]
    return [*ledger, *kept.values()]

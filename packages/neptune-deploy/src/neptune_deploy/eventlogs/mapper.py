"""A log export's rows as a typed event table (ADR 0017 §1).

It reads a compiler package's headed tables (a CSV the tabular adapter read) and nothing else.
For each table that has every column a mapping ``requires``, one typed table is written, one row per
source row, in order:

- each listed column, verbatim, citing its cell (``NotCovered`` when the table lacks it);
- the time text verbatim, then ``<time>.sec`` and ``<time>.nanosec``, integers read by the declared
  format on the log's own clock (``lifecycle.times``: the precision the text states, ADR 0016 §9;
  never moved to UTC), and ``@clock:<time>``, the ``TimestampDomain`` id they count on;
- ``@id:<namespace>``, the row's own identifier text, when the mapping names one.

A blank time is ``Unknown`` with ``value_blank``, one that does not read is ``Unknown`` with
``value_unreadable``; the row is kept. A time of day without a date never reads (ADR 0016 §1). Two
rows stating one identifier are both kept, with ``identifier_repeated``; a row stating none is kept,
its identifier ``Unknown``, with ``identifier_blank``. A civil clock has a
``civil_time_zone`` companion: the mapping's zone, or the caller's for the source (ADR 0017 §2).
"""

from collections.abc import Iterator, Sequence
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Final

from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import RecordId
from neptune.model.knowledge import (
    AssertionKind,
    Knowledge,
    Known,
    KnownAbsent,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import EvidenceRef, Provenance, adapter_locator
from neptune.model.world import CellValue, StructuredRecord, StructuredTable
from neptune.store.package import IngestPackage
from neptune_deploy.eventlogs.mapping import EventLogMapping
from neptune_deploy.lifecycle.mapper import (
    _Clocks,
    _Findings,
    _Table,
    source_paths,
    source_zones,
    unique_domains,
    zone_sources,
)
from neptune_deploy.lifecycle.mapping import MappingError
from neptune_deploy.lifecycle.times import read_time

if TYPE_CHECKING:
    from neptune.model.reference import CivilTimeZone, TimestampDomain

MAPPER_ID: Final = "deploy_event_log_map"
MAPPER_VERSION: Final = "0.1.0"
STATED: Final = AssertionKind.STATED
NANO: Final = 10**9

FINDINGS: Final[dict[str, tuple[Severity, FindingCategory, str]]] = {
    "value_blank": (
        Severity.WARNING,
        FindingCategory.MISSING,
        "a log row states no time; its typed time is Unknown and the row is kept",
    ),
    "value_unreadable": (
        Severity.WARNING,
        FindingCategory.INCONSISTENT,
        "a log row's time does not read under the declared format; its typed time is Unknown and"
        " the row is kept",
    ),
    "identifier_blank": (
        Severity.WARNING,
        FindingCategory.MISSING,
        "a log row states no identifier; its identifier is Unknown, so no assertion can name it,"
        " and the row is kept",
    ),
    "identifier_repeated": (
        Severity.WARNING,
        FindingCategory.INCONSISTENT,
        "two rows of a log state the same identifier; both are kept",
    ),
    "column_absent": (
        Severity.INFO,
        FindingCategory.MISSING,
        "a column the mapping copies is not in the log; its cells are NotCovered",
    ),
    "column_unmapped": (
        Severity.WARNING,
        FindingCategory.UNSUPPORTED,
        "a column of the log that the mapping neither copies nor ignores",
    ),
}


class _Text:
    """One cell's text, its state and its citation. A short row's missing cell is blank: the
    compiler already reports the row (``tabular.csv_ragged_rows``, in the base's receipt)."""

    def __init__(self, table: _Table, row: StructuredRecord, column: str) -> None:
        index = table.columns.get(column)
        self.absent = index is None
        if index is None or index >= len(row.cells):
            self.state: Knowledge[CellValue] | None = None
            self.place = row.provenance.evidence
        else:
            self.state = row.cells[index]
            self.place = row.cell_evidence(table.record, index)

    @property
    def text(self) -> str | None:
        if isinstance(self.state, Known) and isinstance(self.state.value, str):
            stripped = self.state.value.strip()
            return stripped or None
        return None


class EventLogRun(_Clocks):
    """One mapping applied to one package's tables."""

    def __init__(
        self, mapping: EventLogMapping, base: IngestPackage, tables: Sequence[_Table]
    ) -> None:
        self.mapping = mapping
        self.tables = [
            t for t in tables if t.header is not None and all(map(t.has, mapping.requires))
        ]
        self.tables = [t for t in self.tables if t.has(mapping.time_column)]
        paths = source_paths(base)
        self.zone_sources = zone_sources(mapping.source_zones, paths)
        self.source_zones = source_zones(mapping.source_zones, paths)
        self.transform = transform_record(
            adapter_id=MAPPER_ID,
            adapter_version=MAPPER_VERSION,
            config=mapping.config(base.id),
            upstream=sorted({t.record.provenance.transform for t in self.tables}),
        )
        self.findings = _Findings(FINDINGS, MAPPER_ID, "table", "column")
        self.domains: dict[tuple[Any, ...], TimestampDomain] = {}
        self.zones: dict[RecordId, CivilTimeZone] = {}

    @property
    def claimed(self) -> set[RecordId]:
        return {t.record.id for t in self.tables}

    def prov(self, evidence: EvidenceRef) -> Provenance:
        return Provenance(evidence, self.transform.id, STATED)

    def records(self) -> Iterator[Any]:
        """The transform, each typed table and its rows, then the clocks and the findings."""
        if not self.tables:
            return
        yield self.transform
        for table in self.tables:
            yield from self._one(table)
        yield from unique_domains(self.domains.values()).values()
        yield from self.zones.values()
        yield from self.findings.build(self.transform)

    def _one(self, table: _Table) -> Iterator[Any]:
        mapping = self.mapping
        copied = [*mapping.columns]
        if mapping.identifier is not None:
            copied.append(mapping.identifier[0])
        for column in dict.fromkeys(copied):
            if not table.has(column):
                self.findings.once("column_absent", table, table.evidence, column)
        unread = [c for c in table.names() if c not in mapping.read]
        if unread:
            self.findings.add("column_unmapped", table, table.evidence, details={"columns": unread})
        where = EvidenceRef(
            table.evidence.source,
            (*table.evidence.locator, adapter_locator(f"{MAPPER_ID}:table", {})),
        )
        typed = StructuredTable(
            id=evidence_record_id(StructuredTable.kind, where, self.transform),
            provenance=self.prov(where),
            name=Known(mapping.table, self.prov(where)),
            header=Known(mapping.header(), self.prov(where)),
        )
        yield typed
        holders: dict[str, EvidenceRef] = {}
        for number, row in enumerate(table.rows):
            yield self._row(table, typed, row, number, holders)

    def _copy(self, cell: _Text) -> Knowledge[CellValue]:
        if cell.absent:
            return NotCovered(self.prov(cell.place))
        provenance = self.prov(cell.place)
        if isinstance(cell.state, Known):
            return Known(cell.state.value, provenance)
        if isinstance(cell.state, KnownAbsent):
            return KnownAbsent(provenance)
        return Unknown(provenance)

    def _row(
        self,
        table: _Table,
        typed: StructuredTable,
        row: StructuredRecord,
        number: int,
        holders: dict[str, EvidenceRef],
    ) -> StructuredRecord:
        mapping = self.mapping
        record_id = evidence_record_id(
            StructuredRecord.kind, row.provenance.evidence, self.transform
        )
        cells = [self._copy(_Text(table, row, column)) for column in mapping.columns]
        time = _Text(table, row, mapping.time_column)
        cells.append(self._copy(time))
        cells.extend(self._time(table, row, time, record_id))
        if mapping.identifier is not None:
            ident = _Text(table, row, mapping.identifier[0])
            text = ident.text
            if text is None and not ident.absent:
                # The row's identity is what an assertion names (``{syslog, "4182"}``): a row
                # that states none is kept, and says so.
                cells.append(Unknown(self.prov(ident.place)))
                self.findings.add(
                    "identifier_blank",
                    table,
                    ident.place,
                    key=mapping.identifier[0],
                    details={"column": mapping.identifier[0]},
                    row=row.row,
                    record=record_id,
                )
            else:
                cells.append(self._copy(ident))
            if text is not None:
                first = holders.setdefault(text, ident.place)
                if first != ident.place:
                    self.findings.add(
                        "identifier_repeated",
                        table,
                        ident.place,
                        key=text,
                        details={"identifier": text},
                        row=row.row,
                        record=record_id,
                        related=[first],
                    )
        return StructuredRecord(
            id=record_id,
            provenance=self.prov(row.provenance.evidence),
            table=typed.id,
            row=number,
            cells=tuple(cells),
        )

    def _time(
        self, table: _Table, row: StructuredRecord, time: _Text, record_id: RecordId
    ) -> list[Knowledge[CellValue]]:
        """``sec``, ``nanosec`` and the clock, each citing the time cell; ``Unknown`` if none."""
        provenance = self.prov(time.place)
        unknown: list[Knowledge[CellValue]] = [Unknown(provenance)] * 3
        column = self.mapping.time_column
        text = time.text
        if text is None:
            if not isinstance(time.state, KnownAbsent):
                self.findings.add(
                    "value_blank",
                    table,
                    time.place,
                    key=column,
                    details={"column": column},
                    row=row.row,
                    record=record_id,
                )
            return unknown
        reading = read_time(text, self.mapping.formats)
        if reading is None:
            self.findings.add(
                "value_unreadable",
                table,
                time.place,
                key=column,
                details={"column": column},
                row=row.row,
                record=record_id,
            )
            return unknown
        zone, stated_by = self.zone_of(table, self.mapping.zone)
        clock = self.domain(
            table.record.id,
            column,
            reading.instant,
            reading.resolution,
            zone,
            table.first_cell(column) or time.place,
            stated_by,
        )
        total = reading.ticks * reading.resolution
        seconds = total.numerator // total.denominator
        nanos = (total - seconds) * NANO
        assert isinstance(nanos, Fraction) and nanos.denominator == 1
        return [
            Known(seconds, provenance),
            Known(int(nanos), provenance),
            Known(clock, provenance),
        ]


def plan_event_logs(
    base: IngestPackage, mappings: Sequence[EventLogMapping], tables: Sequence[_Table]
) -> list[EventLogRun]:
    """Each mapping's run, in mapping-hash order. A table is the first matching mapping's."""
    hashes = [m.sha256 for m in mappings]
    if len(set(hashes)) != len(hashes):
        raise MappingError("the same event-log mapping file is given twice")
    runs: list[EventLogRun] = []
    taken: set[RecordId] = set()
    for mapping in sorted(mappings, key=lambda m: m.sha256):
        run = EventLogRun(mapping, base, [t for t in tables if t.record.id not in taken])
        taken |= run.claimed
        runs.append(run)
    return runs


__all__ = ["FINDINGS", "MAPPER_ID", "MAPPER_VERSION", "EventLogRun", "plan_event_logs"]

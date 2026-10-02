"""ROS 2 diagnostics as stated event records, through a declared vendor mapping (ADR 0010 §7).

It reads a compiler package's records and nothing else: never a bag, never a source's bytes.

- **Exported JSON.** The compiler's tabular adapter reads a JSON array of ``DiagnosticArray``
  objects into a ``StructuredTable`` with one row per array and one cited cell per leaf. For each
  status under the mapping's ``array`` pointer, one row of a ``diagnostic events`` table is made:
  ``event_kind``, then ``level``, ``name``, ``message`` and ``hardware_id`` exactly as the export
  states them (a level stays the integer it was), the stamp's ``sec`` and ``nanosec`` as stated, and
  a companion clock cell. A status's key and value pairs are rows of a ``diagnostic values`` table
  that name their event row. Every cell cites its source cell, ``stated``, under the mapping's
  transform, whose config holds the whole mapping and its hash.
- **The kind.** ``names[name]`` if the mapping has the status name, else ``levels[code]``, where
  the code is the level as declared text. A level outside ``levels`` is a ``status_unmapped``
  finding (the code and how many), and the row's ``event_kind`` is ``Unknown`` unless the name maps:
  the value stays as declared and nothing is guessed. A status with no readable level is
  ``level_missing``.
- **Bags.** A bag's ``/diagnostics`` is a ``Stream`` of its package. The compiler does not decode
  message payloads into the package yet (compiler gap, ADR 0010), so there is nothing here to map:
  each such stream is a ``bag_payload_not_decoded`` finding and the bag is not opened.
- **The clock.** ``header.stamp`` is a ``TimestampDomain`` whose role, resolution, epoch and
  timescale are ``Unknown`` unless the mapping's ``clock`` declares them.

The base package is never changed; the same package and mapping give byte-identical records.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, Final

from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import AssertionKind, Knowledge, Known, KnownAbsent, Unknown
from neptune.model.provenance import (
    EvidenceRef,
    JsonPointer,
    Provenance,
    TransformRecord,
    adapter_locator,
)
from neptune.model.reference import TimestampDomain
from neptune.model.run import Stream
from neptune.model.world import CellValue, StructuredRecord, StructuredTable
from neptune.store.package import IngestPackage, package_files
from neptune_deploy.diagnostics.mapping import DiagnosticsMapping
from neptune_deploy.lifecycle.mapper import _Findings, _Table, carried, tables_of, unique_domains
from neptune_deploy.lifecycle.mapping import MappingError

MAPPER_ID: Final = "deploy_diagnostics_map"
MAPPER_VERSION: Final = "0.1.0"
STATED: Final = AssertionKind.STATED
SCHEMA_NAME: Final = "diagnostic_msgs/msg/DiagnosticArray"
EVENT_TABLE: Final = "diagnostic events"
VALUES_TABLE: Final = "diagnostic values"
EVENT_HEADER: Final = (
    "event_kind",
    "level",
    "name",
    "message",
    "hardware_id",
    "stamp.sec",
    "stamp.nanosec",
    "@clock:stamp",
)
VALUES_HEADER: Final = ("event", "key", "value")

FINDINGS: Final[dict[str, tuple[Severity, FindingCategory, str]]] = {
    "status_unmapped": (
        Severity.WARNING,
        FindingCategory.UNSUPPORTED,
        "a diagnostic status code is outside the vendor mapping; it stays as declared and its"
        " event kind is Unknown (unless its name is mapped)",
    ),
    "level_missing": (
        Severity.WARNING,
        FindingCategory.MISSING,
        "a diagnostic status states no level, so no code can be looked up",
    ),
    "bag_payload_not_decoded": (
        Severity.WARNING,
        FindingCategory.UNSUPPORTED,
        "the package holds a diagnostics stream but not its decoded messages (the compiler does"
        " not decode them yet); the bag is not read here, so its statuses are not mapped",
    ),
    "nothing_to_map": (
        Severity.WARNING,
        FindingCategory.MISSING,
        "no table of the package holds statuses under the mapping's array pointer, and no stream"
        " carries diagnostics",
    ),
}


def code(name: str) -> str:
    return f"{MAPPER_ID}.{name}"


def _escaped(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _known(value: Any) -> Knowledge[Any]:
    return Unknown() if value is None else Known(value)


def _text_of(value: object) -> str | None:
    """A level as the declared text a code is looked up by."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value:
        return value
    return None


def _inside(evidence: EvidenceRef, pointer: str) -> EvidenceRef:
    """``evidence`` (a cell of a JSON row) with its last step replaced by ``pointer``."""
    return EvidenceRef(evidence.source, (*evidence.locator[:-1], JsonPointer(pointer)))


class _Run:
    """One mapping applied to one package."""

    def __init__(self, base: IngestPackage, mapping: DiagnosticsMapping) -> None:
        self.base, self.mapping = base, mapping
        self.tables, _ = tables_of(base.records)
        self.pattern = re.compile(re.escape(mapping.array) + r"/([0-9]+)(/.*)?")
        self.cache = {t.record.id: self.statuses(t) for t in self.tables}
        self.found = [t for t in self.tables if self.cache[t.record.id]]
        self.streams = sorted(
            (s for s in base.records if isinstance(s, Stream) and self._diagnostic(s)),
            key=lambda s: s.id,
        )
        upstream = sorted(
            {t.record.provenance.transform for t in self.found}
            | {s.provenance.transform for s in self.streams}
        )
        self.transform: TransformRecord = transform_record(
            adapter_id=MAPPER_ID,
            adapter_version=MAPPER_VERSION,
            config={**mapping.config(), "base_package": base.id},
            upstream=upstream,
        )
        self.findings = _Findings(FINDINGS, MAPPER_ID, "table", "field")
        self.extra: list[Any] = []
        self.domains: dict[RecordId, TimestampDomain] = {}
        self.out: list[Any] = []

    def _diagnostic(self, stream: Stream) -> bool:
        topic = stream.topic.value if isinstance(stream.topic, Known) else None
        schema = stream.schema_name.value if isinstance(stream.schema_name, Known) else None
        return topic in self.mapping.topics or schema == SCHEMA_NAME

    def prov(self, evidence: EvidenceRef) -> Provenance:
        return Provenance(evidence, self.transform.id, STATED)

    def statuses(self, table: _Table) -> list[tuple[int, dict[int, dict[str, int]]]]:
        """For each row with statuses: its position and ``status index -> {key path: cell}``.

        Only a table of JSON objects has cells named by pointers; a headed table (CSV) has none."""
        if table.header is not None:
            return []
        out = []
        for position in range(len(table.rows)):
            found: dict[int, dict[str, int]] = defaultdict(dict)
            for pointer, cell in table.pointers[position].items():
                match = self.pattern.fullmatch(pointer)
                if match is not None:
                    found[int(match.group(1))][match.group(2) or ""] = cell
            if found:
                out.append((position, dict(sorted(found.items()))))
        return out

    def run(self) -> list[Any]:
        for table in self.found:
            self.one(table)
        for stream in self.streams:
            self._bag(stream)
        if not self.found and not self.streams:
            self._nothing()
        built = self.findings.build(self.transform)
        domains = unique_domains(self.domains.values()).values()
        return [self.transform, *domains, *self.out, *self.extra, *built]

    def _finding(
        self,
        name: str,
        subject: EvidenceRef,
        details: dict[str, JsonValue],
        records: Sequence[RecordId] = (),
    ) -> None:
        severity, category, message = FINDINGS[name]
        self.extra.append(
            ingest_finding(
                code=code(name),
                category=category,
                severity=severity,
                subject=subject,
                transform=self.transform,
                message=message,
                details=details,
                records=records,
            )
        )

    def _nothing(self) -> None:
        for record in self.base.records:
            provenance = getattr(record, "provenance", None)
            if isinstance(provenance, Provenance):
                self._finding("nothing_to_map", provenance.evidence, {"package": self.base.id})
                return
        raise MappingError("the package holds no records to map")

    def _bag(self, stream: Stream) -> None:
        details: dict[str, JsonValue] = {"stream": stream.id}
        if isinstance(stream.topic, Known):
            details["topic"] = stream.topic.value
        if isinstance(stream.message_count, Known):
            details["message_count"] = stream.message_count.value
        self._finding("bag_payload_not_decoded", stream.provenance.evidence, details, [stream.id])

    # --- One table ------------------------------------------------------------------------

    def one(self, table: _Table) -> None:
        statuses = self.cache[table.record.id]
        clock = self._clock(table, statuses)
        source = table.evidence
        events = self._table(source, "events", EVENT_TABLE, EVENT_HEADER)
        values = self._table(source, "values", VALUES_TABLE, VALUES_HEADER)
        event_rows: list[StructuredRecord] = []
        value_rows: list[StructuredRecord] = []
        for position, found in statuses:
            row = table.rows[position]
            for index, keys in found.items():
                event = self._event(
                    table, events, position, row, index, keys, clock, len(event_rows)
                )
                event_rows.append(event)
                value_rows.extend(
                    self._values(table, values, row, index, keys, event, len(value_rows))
                )
        self.out.extend([events, *event_rows])
        if value_rows:
            self.out.extend([values, *value_rows])

    def _table(
        self, source: EvidenceRef, part: str, name: str, header: tuple[str, ...]
    ) -> StructuredTable:
        where = EvidenceRef(
            source.source, (*source.locator, adapter_locator(f"{MAPPER_ID}:{part}", {}))
        )
        provenance = self.prov(where)
        return StructuredTable(
            id=evidence_record_id(StructuredTable.kind, where, self.transform),
            provenance=provenance,
            name=Known(name, provenance),
            header=Known(header, provenance),
        )

    def _clock(
        self, table: _Table, statuses: Sequence[tuple[int, dict[int, dict[str, int]]]]
    ) -> TimestampDomain | None:
        sec = self.mapping.sec
        if sec is None:
            return None
        for position, _ in statuses:
            cell = table.pointers[position].get(sec)
            if cell is None:
                continue
            where = table.rows[position].cell_evidence(table.record, cell)
            clock = self.mapping.clock
            domain = TimestampDomain(
                id=evidence_record_id(TimestampDomain.kind, where, self.transform),
                provenance=self.prov(where),
                field="header.stamp",
                scope=(),
                role=_known(clock.role),
                resolution=_known(clock.resolution),
                epoch=_known(clock.epoch),
                timescale=_known(clock.timescale),
                declared_monotonic=Unknown(),
            )
            self.domains[domain.id] = domain
            return domain
        return None

    def _copy(
        self, table: _Table, row: StructuredRecord, cell: int | None, fallback: EvidenceRef
    ) -> Knowledge[CellValue]:
        """One source cell as a stated cell: the value as declared, citing the cell."""
        if cell is None:
            return Unknown(self.prov(fallback))
        evidence = row.cell_evidence(table.record, cell)
        state = row.cells[cell]
        provenance = self.prov(evidence)
        if isinstance(state, KnownAbsent):
            return KnownAbsent(provenance)
        if isinstance(state, Known):
            return Known(state.value, provenance)
        return Unknown(provenance)

    def _event(
        self,
        table: _Table,
        events: StructuredTable,
        position: int,
        row: StructuredRecord,
        index: int,
        keys: dict[str, int],
        clock: TimestampDomain | None,
        number: int,
    ) -> StructuredRecord:
        mapping = self.mapping
        base = f"{mapping.array}/{index}"
        any_cell = next(iter(keys.values()))
        status = _inside(row.cell_evidence(table.record, any_cell), base)

        def cell(field: str) -> int | None:
            return keys.get("/" + _escaped(mapping.fields[field]))

        cells = {
            name: self._copy(table, row, cell(name), status)
            for name in ("level", "name", "message", "hardware_id")
        }
        level_cell, name_cell = cell("level"), cell("name")
        level_state = cells["level"]
        level = _text_of(level_state.value) if isinstance(level_state, Known) else None
        name = cells["name"].value if isinstance(cells["name"], Known) else None
        kind: str | None = None
        cited = status
        if isinstance(name, str) and name in mapping.names:
            kind, cited = mapping.names[name], row.cell_evidence(table.record, name_cell or 0)
        elif level is not None and level in mapping.levels:
            kind, cited = mapping.levels[level], row.cell_evidence(table.record, level_cell or 0)
        if level_cell is not None:
            cited = cited if kind is not None else row.cell_evidence(table.record, level_cell)
        event_kind: Knowledge[CellValue] = (
            Known(kind, self.prov(cited)) if kind is not None else Unknown(self.prov(cited))
        )
        record_id = evidence_record_id(StructuredRecord.kind, status, self.transform)
        if level is None:
            self.findings.add("level_missing", table, status, row=position, record=record_id)
        elif level not in mapping.levels:
            self.findings.add(
                "status_unmapped",
                table,
                row.cell_evidence(table.record, level_cell or 0),
                key=level,
                details={"level": level},
                row=position,
                record=record_id,
            )
        stamp = [
            self._copy(table, row, table.pointers[position].get(pointer), status)
            for pointer in (mapping.sec, mapping.nanosec)
            if pointer is not None
        ]
        if mapping.sec is None or mapping.nanosec is None:
            stamp = [Unknown(self.prov(status)), Unknown(self.prov(status))]
        sec_cell = table.pointers[position].get(mapping.sec) if mapping.sec else None
        companion: Knowledge[CellValue] = (
            Known(clock.id, self.prov(row.cell_evidence(table.record, sec_cell)))
            if clock is not None and sec_cell is not None
            else Unknown(self.prov(status))
        )
        return StructuredRecord(
            id=record_id,
            provenance=self.prov(status),
            table=events.id,
            row=number,
            cells=(
                event_kind,
                cells["level"],
                cells["name"],
                cells["message"],
                cells["hardware_id"],
                stamp[0],
                stamp[1],
                companion,
            ),
        )

    def _values(
        self,
        table: _Table,
        values: StructuredTable,
        row: StructuredRecord,
        index: int,
        keys: dict[str, int],
        event: StructuredRecord,
        first: int,
    ) -> list[StructuredRecord]:
        """The ``values`` of one status: a row per key and value pair, in the export's order."""
        mapping = self.mapping
        prefix = f"/{_escaped(mapping.fields['values'])}/"
        pairs: dict[int, dict[str, int]] = defaultdict(dict)
        for path, cell in keys.items():
            if path.startswith(prefix):
                head, _, field = path[len(prefix) :].partition("/")
                if head.isascii() and head.isdigit():
                    pairs[int(head)][field] = cell
        out: list[StructuredRecord] = []
        for number, (item, fields) in enumerate(sorted(pairs.items())):
            anchor = row.cell_evidence(table.record, next(iter(fields.values())))
            where = _inside(anchor, f"{mapping.array}/{index}{prefix[:-1]}/{item}")
            out.append(
                StructuredRecord(
                    id=evidence_record_id(StructuredRecord.kind, where, self.transform),
                    provenance=self.prov(where),
                    table=values.id,
                    row=first + number,
                    cells=(
                        Known(event.id, self.prov(event.provenance.evidence)),
                        self._copy(table, row, fields.get("key"), where),
                        self._copy(table, row, fields.get("value"), where),
                    ),
                )
            )
        return out


def map_diagnostics(base: IngestPackage, mappings: Sequence[DiagnosticsMapping]) -> list[Any]:
    """Every record of the mapped package: the base's source ledger and the transforms its records
    name, then each mapping's transform, clocks, tables and findings."""
    if not mappings:
        raise MappingError("name at least one diagnostics mapping file")
    hashes = [mapping.sha256 for mapping in mappings]
    if len(set(hashes)) != len(hashes):
        raise MappingError("the same mapping file is given twice")
    ids = [mapping.id for mapping in mappings]
    if len(set(ids)) != len(ids):
        raise MappingError(f"two mapping files share an id: {ids}")
    out: list[Any] = []
    for mapping in sorted(mappings, key=lambda m: m.sha256):
        out.extend(_Run(base, mapping).run())
    return [*carried(base, out), *out]


def map_diagnostics_files(
    base: IngestPackage, mappings: Sequence[DiagnosticsMapping]
) -> dict[str, bytes]:
    """Every file of the mapped package (``neptune.store.package.package_files``)."""
    return package_files(map_diagnostics(base, mappings))

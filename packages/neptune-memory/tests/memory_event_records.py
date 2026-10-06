"""Compiler-shaped event records for tests, built with the compiler's own types.

Every ``incident_record``, ``intervention``, ``structured_table`` and ``structured_record`` here is
constructed as the compiler model class and serialised with its ``to_json``, so a test can never
feed the event consolidator a shape the compiler would not write (root ADRs 0020 §5 and 0051).
Lifecycle records are ``stated`` by a CMMS row, a form or a ticket; a table's rows cite their row
(``Row(n)``), so each cell inherits it and resolves to its ``RowCell``. Clocks and clock mappings
come from ``memory_run_records``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from memory_identity_records import STATED, Record, cite, provenance, rid, source
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import Known, Unknown
from neptune.model.lifecycle import IncidentRecord, Intervention, TimelineEntry
from neptune.model.provenance import EvidenceRef, Provenance, Row
from neptune.model.world import StructuredRecord, StructuredTable

if TYPE_CHECKING:
    from neptune.model.knowledge import Knowledge
    from neptune.model.time import Timestamp

SECOND: Final = 10**9  # nanoseconds


def _ids(values: Sequence[LogicalId] | None) -> Knowledge[tuple[Knowledge[LogicalId], ...]]:
    if values is None:
        return Unknown()
    return Known(tuple(Known(v) for v in sorted(values, key=lambda v: (v.namespace, v.value))))


def _opt(value: object) -> Knowledge[object]:
    return Unknown() if value is None else Known(value)


def incident(
    name: str,
    *,
    occurred: Timestamp | None,
    severity: str | None = None,
    description: str | None = None,
    machines: Sequence[LogicalId] | None = None,
    assets: Sequence[LogicalId] | None = None,
    site: LogicalId | Knowledge[LogicalId] | None = None,
    zone: LogicalId | Knowledge[LogicalId] | None = None,
    timeline: Sequence[tuple[Timestamp | None, str]] | None = None,
    identifier: LogicalId | None = None,
) -> tuple[Record, RecordId]:
    """An ``incident_record`` stated by ``name`` (a CMMS row, an incident form)."""
    declared = provenance(cite(name), STATED)

    def place(value: LogicalId | Knowledge[LogicalId] | None) -> Knowledge[LogicalId]:
        return value if not isinstance(value, LogicalId | type(None)) else _opt(value)  # type: ignore[return-value]

    record = IncidentRecord(
        id=rid("incident_record", declared.evidence),
        provenance=declared,
        identifiers=_ids([identifier] if identifier else []),
        site=place(site),
        machines=_ids(machines),
        configuration=Unknown(),
        related=Known(()),
        occurred=_opt(occurred),  # type: ignore[arg-type]
        severity=_opt(severity),  # type: ignore[arg-type]
        zone=place(zone),
        location=Unknown(),
        assets=_ids(assets) if assets is not None else Known(()),
        timeline=(
            Known(
                tuple(
                    TimelineEntry(time=_opt(t), text=Known(text))  # type: ignore[arg-type]
                    for t, text in timeline
                )
            )
            if timeline is not None
            else Known(())
        ),
        description=_opt(description),  # type: ignore[arg-type]
        root_cause=Unknown(),
    )
    return record.to_json(), record.id  # type: ignore[return-value]


def intervention(
    name: str,
    *,
    start: Timestamp | None,
    end: Timestamp | None = None,
    mode: str | None = None,
    reason: str | None = None,
    machines: Sequence[LogicalId] | None = None,
    site: LogicalId | None = None,
    identifiers: Sequence[LogicalId] | Knowledge[tuple[Knowledge[LogicalId], ...]] = (),
) -> tuple[Record, RecordId]:
    """An ``intervention`` stated by ``name`` (a Formant intervention request, a CMMS downtime
    row, a form); ``identifiers`` the ids it declares itself by, or the field as any state."""
    declared = provenance(cite(name), STATED)
    record = Intervention(
        id=rid("intervention", declared.evidence),
        provenance=declared,
        identifiers=identifiers if not isinstance(identifiers, Sequence) else _ids(identifiers),
        site=_opt(site),  # type: ignore[arg-type]
        machines=_ids(machines),
        configuration=Unknown(),
        related=Known(()),
        mode=_opt(mode),  # type: ignore[arg-type]
        authority=Unknown(),
        reason=_opt(reason),  # type: ignore[arg-type]
        commands=Known(()),
        start=_opt(start),  # type: ignore[arg-type]
        end=_opt(end),  # type: ignore[arg-type]
        outcome=Unknown(),
    )
    return record.to_json(), record.id  # type: ignore[return-value]


Cell = str | int | float | bool | None


def table(
    name: str,
    header: Sequence[str],
    rows: Sequence[Sequence[Cell]],
    *,
    file: str | None = None,
) -> tuple[list[Record], RecordId, list[RecordId]]:
    """A ``structured_table`` named ``name`` and one ``structured_record`` per row, in the file
    ``file`` (default: the table's name); ``None`` is a blank (``Unknown``) cell."""
    document = source(file or name)
    table_evidence = EvidenceRef(document, (Row(0),))
    declared = provenance(table_evidence, STATED)
    built = StructuredTable(
        id=rid("structured_table", table_evidence),
        provenance=declared,
        name=Known(name),
        header=Known(tuple(header)),
    )
    records: list[Record] = [built.to_json()]  # type: ignore[list-item]
    ids: list[RecordId] = []
    for index, cells in enumerate(rows, start=1):
        evidence = EvidenceRef(document, (Row(index),))
        row = StructuredRecord(
            id=rid("structured_record", evidence),
            provenance=Provenance(evidence, declared.transform, STATED),
            table=built.id,
            row=index,
            cells=tuple(Unknown() if c is None else Known(c) for c in cells),
        )
        records.append(row.to_json())  # type: ignore[arg-type]
        ids.append(row.id)
    return records, built.id, ids

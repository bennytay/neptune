"""Compiler-shaped event records for episode tests, built with the compiler's own types.

Every ``intervention`` and ``incident_record`` here is constructed as the compiler model class
(root ADR 0051) and serialised with its ``to_json``, so a test can never feed the episode
consolidator a shape the compiler would not write. Both are ``stated`` by their declaration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from memory_identity_records import STATED, Record, ambiguous, cite, provenance, rid
from neptune.model.ids import LogicalId
from neptune.model.knowledge import Known, Unknown
from neptune.model.lifecycle import IncidentRecord, Intervention

if TYPE_CHECKING:
    from collections.abc import Sequence

    from neptune.model.knowledge import Knowledge
    from neptune.model.time import Timestamp


def _ids(values: Sequence[LogicalId | Sequence[LogicalId]] | None, name: str) -> Knowledge:  # type: ignore[type-arg]
    """A ``Listed`` of declared ids: ``None`` is a blank list (``Unknown``); a nested sequence is
    one ``Ambiguous`` item."""
    if values is None:
        return Unknown()
    items = [
        Known(v) if isinstance(v, LogicalId) else ambiguous(f"{name} item {i}", *v)
        for i, v in enumerate(values)
    ]
    return Known(tuple(items))


def _time(value: Timestamp | None) -> Knowledge:  # type: ignore[type-arg]
    return Known(value) if value is not None else Unknown()


def intervention(
    name: str,
    *,
    machines: Sequence[LogicalId | Sequence[LogicalId]] | None = None,
    related: Sequence[LogicalId | Sequence[LogicalId]] | None = (),
    start: Timestamp | None = None,
    end: Timestamp | None = None,
    outcome: str | None = None,
) -> tuple[Record, str]:
    """An ``Intervention`` declared by ticket ``name``."""
    declared = provenance(cite(f"ticket {name}"), STATED)
    record = Intervention(
        id=rid("intervention", declared.evidence),
        provenance=declared,
        identifiers=Known((Known(LogicalId("ticket", name)),)),
        site=Unknown(),
        machines=_ids(machines, name),
        configuration=Unknown(),
        related=_ids(related, name),
        mode=Known("remote assist"),
        authority=Unknown(),
        reason=Known("blocked path"),
        commands=Known((Known("pause"), Known("resume"))),
        start=_time(start),
        end=_time(end),
        outcome=Known(outcome) if outcome is not None else Unknown(),
    )
    return record.to_json(), record.id  # type: ignore[return-value]


def incident(
    name: str,
    *,
    machines: Sequence[LogicalId | Sequence[LogicalId]] | None = None,
    related: Sequence[LogicalId | Sequence[LogicalId]] | None = (),
    occurred: Timestamp | None = None,
    description: str = "emergency stop",
) -> tuple[Record, str]:
    """An ``IncidentRecord`` (a stop or a fault) declared by report ``name``."""
    declared = provenance(cite(f"incident {name}"), STATED)
    record = IncidentRecord(
        id=rid("incident_record", declared.evidence),
        provenance=declared,
        identifiers=Known((Known(LogicalId("incident", name)),)),
        site=Unknown(),
        machines=_ids(machines, name),
        configuration=Unknown(),
        related=_ids(related, name),
        occurred=_time(occurred),
        severity=Known("S3"),
        zone=Unknown(),
        location=Unknown(),
        assets=Known(()),
        timeline=Known(()),
        description=Known(description),
        root_cause=Unknown(),
    )
    return record.to_json(), record.id  # type: ignore[return-value]

"""Parsing the stated events the episode consolidator reads (ADR 0012 §1).

Parsing is kept apart from the episode policy: each parser turns one Ledger record into a typed
``Event`` or raises ``Malformed``, and decides nothing about episodes. ``consolidate.episodes``
applies the policy. The readers are small on purpose so the event index (MVL-134) can share them.

Both kinds are compiler lifecycle records (root ADR 0051), read with the compiler's strict
readers, so Memory reads exactly the package-schema shape. Each is ``stated`` by its declaration:

- ``intervention``: a human intervention (a remote assist, an on-site action) with the
  ``machines`` it involved, the records it names (``related``), and its ``start`` / ``end`` on the
  clock its timestamps name.
- ``incident_record``: an incident (a contact, a bumper or emergency stop, a fault) with the
  ``machines`` involved, ``related`` records and the instant it ``occurred``.

No compiler kind states a task attempt, its boundaries or its outcome yet (root ADR 0047 §9: there
is no task record kind), and none tells an emergency stop or a fault from any other incident: an
``incident_record`` is the only stated stop event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeVar

from neptune.model.knowledge import Ambiguous, Known
from neptune.model.lifecycle import (
    IncidentRecord,
    Intervention,
    incident_record_from_json,
    intervention_from_json,
)
from neptune.model.provenance import Provenance
from neptune_memory.consolidate.identity_records import Malformed, declared

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from neptune.model.ids import LogicalId, RecordId
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge
    from neptune.model.provenance import EvidenceRef
    from neptune.model.time import Timestamp

_T = TypeVar("_T")

# Ledger record kinds the episode consolidator reads, beside ``run`` and ``timestamp_domain``.
INTERVENTION: Final = "intervention"
INCIDENT: Final = "incident_record"

__all__ = ["INCIDENT", "INTERVENTION", "Event", "Malformed", "Named", "incident", "intervention"]


@dataclass(frozen=True)
class Named:
    """One id a list states: ``ids`` holds one id when it is ``Known``, every candidate when it is
    ``Ambiguous``; ``evidence`` is what the item itself cites (nothing when it inherits)."""

    ids: tuple[LogicalId, ...]
    decided: bool
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class Event:
    """A stated event as the episode policy needs it.

    ``start`` and ``end`` are the instants the record states, each on its own clock; ``end`` is
    inclusive and ``None`` when not stated (an incident states one instant: ``start`` only).
    ``evidence`` cites the record and every value read from it that cites its own place.
    """

    record: RecordId
    kind: str
    machines: tuple[Named, ...]
    related: tuple[Named, ...]
    start: Timestamp | None
    end: Timestamp | None
    evidence: tuple[EvidenceRef, ...]


def _strict(parse: Callable[[JsonValue], _T], record: Mapping[str, object]) -> _T:
    """A compiler reader over one record; whatever it refuses is malformed here."""
    try:
        return parse(dict(record))  # type: ignore[arg-type]
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise Malformed(str(exc) or type(exc).__name__) from exc


def _cited(knowledge: object) -> tuple[EvidenceRef, ...]:
    """The evidence a value (or a candidate) cites itself; nothing when it inherits."""
    slot = getattr(knowledge, "provenance", None)
    return (slot.evidence,) if isinstance(slot, Provenance) else ()


def _named(listed: Knowledge[tuple[Knowledge[LogicalId], ...]]) -> tuple[Named, ...]:
    """The ids a ``Listed`` field states; a list not stated (``Unknown``, ``NotCovered``) names
    none. Every id is a declared value (ADR 0006 §9): never blank or padded."""
    if not isinstance(listed, Known):
        return ()
    out: list[Named] = []
    for item in listed.value:
        if isinstance(item, Known):
            out.append(Named((declared(item.value),), True, _cited(item)))
        elif isinstance(item, Ambiguous):
            ids = tuple(declared(c.value) for c in item.candidates)
            cited = tuple(ref for c in item.candidates for ref in _cited(c))
            out.append(Named(ids, False, (*_cited(item), *cited)))
    return tuple(out)


def _instant(knowledge: Knowledge[Timestamp]) -> Timestamp | None:
    """A stated instant; an ``Ambiguous`` or unstated one is not read as any of its readings."""
    return knowledge.value if isinstance(knowledge, Known) else None


def _event(
    record: Intervention | IncidentRecord,
    start: Knowledge[Timestamp],
    end: Knowledge[Timestamp] | None,
) -> Event:
    stated = [start, *([end] if end is not None else [])]
    return Event(
        record=record.id,
        kind=record.kind,
        machines=_named(record.machines),
        related=_named(record.related),
        start=_instant(start),
        end=_instant(end) if end is not None else None,
        evidence=(
            record.provenance.evidence,
            *(ref for k in stated if isinstance(k, Known) for ref in _cited(k)),
        ),
    )


def intervention(record: Mapping[str, object]) -> Event:
    parsed = _strict(intervention_from_json, record)
    return _event(parsed, parsed.start, parsed.end)


def incident(record: Mapping[str, object]) -> Event:
    parsed = _strict(incident_record_from_json, record)
    return _event(parsed, parsed.occurred, None)

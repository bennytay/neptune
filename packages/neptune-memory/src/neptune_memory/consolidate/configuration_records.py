"""Parsing the Ledger records the configuration lineage consolidator reads (ADR 0010 §1).

Parsing is kept apart from the policy in ``consolidate.configuration``: each parser turns one
Ledger record into a typed value or raises ``Malformed``, and decides nothing about chains,
bindings or coverage. Every kind is read with the compiler's own strict reader, so Memory reads
exactly the package-schema shape:

- the lifecycle kinds that place a configuration on a machine at an instant (root ADR 0051):
  ``commissioning_baseline`` (``commissioned``), ``maintenance_event`` (``performed``; its
  ``configuration`` is the as-maintained one), ``change_record`` (``effective``) and
  ``requalification_record`` (``performed``);
- ``authorisation_envelope``: a configuration approved at a site over ``[valid_from,
  valid_until)``;
- ``run`` and ``snapshot_binding`` (root ADR 0050 §8): which snapshot a run ran with, and when;
- the snapshot kinds a binding names (``hardware_configuration``, ``software_configuration``,
  ``calibration``, ``configuration_snapshot``), read for their record-level evidence: the anchor of
  their Ledger configuration thread (Ledger ADR 0003 §2);
- ``timestamp_domain``, through ``identity_records.clock``.

A value that is not ``Known`` is kept as its state (``Outcome``), never as a blank.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, TypeAlias, TypeVar

from neptune.model.alignment import SnapshotKind, snapshot_binding_from_json
from neptune.model.configuration import configuration_snapshot_from_json
from neptune.model.knowledge import Ambiguous, AssertionKind, Known, KnownAbsent, NotApplicable
from neptune.model.lifecycle import (
    authorisation_envelope_from_json,
    change_record_from_json,
    commissioning_baseline_from_json,
    maintenance_event_from_json,
    requalification_record_from_json,
)
from neptune.model.machine import (
    calibration_from_json,
    hardware_configuration_from_json,
    software_configuration_from_json,
)
from neptune.model.provenance import Provenance
from neptune.model.run import run_from_json
from neptune.model.time import Timestamp
from neptune_memory.consolidate.identity_records import Inferred, Malformed, declared

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from neptune.model.alignment import ValidityWindow
    from neptune.model.ids import LogicalId, RecordId
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge
    from neptune.model.provenance import EvidenceRef

_T = TypeVar("_T")

# Lifecycle kinds that place a configuration on a machine, and the field stating when.
CHAIN_KINDS: Final[Mapping[str, tuple[Callable[[JsonValue], object], str]]] = {
    "commissioning_baseline": (commissioning_baseline_from_json, "commissioned"),
    "maintenance_event": (maintenance_event_from_json, "performed"),
    "change_record": (change_record_from_json, "effective"),
    "requalification_record": (requalification_record_from_json, "performed"),
}
AUTHORISATION_ENVELOPE: Final = "authorisation_envelope"
RUN: Final = "run"
SNAPSHOT_BINDING: Final = "snapshot_binding"
# The one snapshot kind the Ledger's thread table has no row for (Ledger ADR 0003 §2; ADR 0022).
CONFIGURATION_SNAPSHOT: Final = str(SnapshotKind.CONFIGURATION_SNAPSHOT)
SNAPSHOT_KINDS: Final[Mapping[str, Callable[[JsonValue], object]]] = {
    str(SnapshotKind.HARDWARE_CONFIGURATION): hardware_configuration_from_json,
    str(SnapshotKind.SOFTWARE_CONFIGURATION): software_configuration_from_json,
    str(SnapshotKind.CALIBRATION): calibration_from_json,
    str(SnapshotKind.CONFIGURATION_SNAPSHOT): configuration_snapshot_from_json,
}

# What a declared id field states:
# - ``known``: one id; ``ambiguous``: every candidate the evidence could mean;
# - ``unknown``: the record could state it and does not (``Unknown``, ``NotCovered``);
# - ``absent``: the record states there is none (``KnownAbsent``, ``NotApplicable``).
Outcome = Literal["known", "ambiguous", "unknown", "absent"]

# One bound of a window as its record states it (ADR 0010 §3, §5): an instant; ``open``, stated
# open on that side (``KnownAbsent``); or ``unstated`` (``Unknown``, ``NotCovered``, ``Ambiguous``,
# ``NotApplicable``). Only a stated bound decides anything; ``unstated`` is never ``open``.
Bound: TypeAlias = Timestamp | Literal["open", "unstated"]


def _strict(parse: Callable[[JsonValue], _T], record: Mapping[str, object]) -> _T:
    provenance = record.get("provenance")
    if isinstance(provenance, dict) and provenance.get("assertion_kind") == "inferred":
        raise Inferred("an inferred record belongs in derived/")
    try:
        return parse(dict(record))  # type: ignore[arg-type]
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise Malformed(str(exc) or type(exc).__name__) from exc


def _cited(knowledge: object) -> tuple[EvidenceRef, ...]:
    """The evidence a value cites of its own; nothing when it inherits its record's."""
    provenance = getattr(knowledge, "provenance", None)
    return (provenance.evidence,) if isinstance(provenance, Provenance) else ()


def _known(knowledge: Knowledge[_T]) -> _T | None:
    return knowledge.value if isinstance(knowledge, Known) else None


def _bound(knowledge: Knowledge[Timestamp]) -> Bound:
    match knowledge:
        case Known(value=value):
            return value
        case KnownAbsent():
            return "open"
        case _:
            return "unstated"


def _declared_id(knowledge: Knowledge[LogicalId]) -> tuple[Outcome, tuple[LogicalId, ...]]:
    match knowledge:
        case Known(value=value):
            return "known", (declared(value),)
        case Ambiguous(candidates=candidates):
            return "ambiguous", tuple(declared(c.value) for c in candidates)
        case KnownAbsent() | NotApplicable():
            return "absent", ()
        case _:
            return "unknown", ()


def _evidence(provenance: Provenance, *values: object) -> tuple[EvidenceRef, ...]:
    return (provenance.evidence, *(ref for value in values for ref in _cited(value)))


# --- Machine chains -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """One lifecycle record, as far as a machine chain reads it.

    ``machines``: the ``Known`` machine ids of a ``Known`` list, each once in declared order, or
    ``None`` when the list is not ``Known``; ``unread_machines`` counts entries of a ``Known`` list
    that are not ``Known`` (an ``Ambiguous`` machine). ``configuration`` holds the id (``known``)
    or the candidates (``ambiguous``). ``at`` is the record's own instant, or ``None``.
    """

    record: RecordId
    kind: str
    machines: tuple[LogicalId, ...] | None
    unread_machines: int
    outcome: Outcome
    configuration: tuple[LogicalId, ...]
    at: Timestamp | None
    evidence: tuple[EvidenceRef, ...]


def _machines(
    listed: Knowledge[tuple[Knowledge[LogicalId], ...]],
) -> tuple[tuple[LogicalId, ...] | None, int]:
    items = _known(listed)
    if items is None:
        return None, 0
    found: list[LogicalId] = []
    unread = 0
    for item in items:
        value = _known(item)
        if value is None:
            unread += 1
        elif declared(value) not in found:
            found.append(value)
    return tuple(found), unread


def event(kind: str, record: Mapping[str, object]) -> Event:
    parse, time_field = CHAIN_KINDS[kind]
    parsed = _strict(parse, record)
    when: Knowledge[Timestamp] = getattr(parsed, time_field)
    configuration: Knowledge[LogicalId] = parsed.configuration  # type: ignore[attr-defined]
    machines, unread = _machines(parsed.machines)  # type: ignore[attr-defined]
    outcome, ids = _declared_id(configuration)
    return Event(
        record=parsed.id,  # type: ignore[attr-defined]
        kind=kind,
        machines=machines,
        unread_machines=unread,
        outcome=outcome,
        configuration=ids,
        at=_known(when),
        evidence=_evidence(parsed.provenance, when, configuration),  # type: ignore[attr-defined]
    )


# --- Authorisation ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Envelope:
    """An ``AuthorisationEnvelope``: the configuration it approves at a site, and when.

    ``valid_until`` is the window's exclusive end, as a validity window's is: ``open`` only when
    the envelope states it has none (ADR 0010 §5).
    """

    record: RecordId
    site: LogicalId | None
    outcome: Outcome
    configuration: tuple[LogicalId, ...]
    valid_from: Timestamp | None
    valid_until: Bound
    evidence: tuple[EvidenceRef, ...]


def envelope(record: Mapping[str, object]) -> Envelope:
    parsed = _strict(authorisation_envelope_from_json, record)
    site = _known(parsed.site)
    outcome, ids = _declared_id(parsed.configuration)
    return Envelope(
        record=parsed.id,
        site=None if site is None else declared(site),
        outcome=outcome,
        configuration=ids,
        valid_from=_known(parsed.valid_from),
        valid_until=_bound(parsed.valid_until),
        evidence=_evidence(
            parsed.provenance,
            parsed.site,
            parsed.configuration,
            parsed.valid_from,
            parsed.valid_until,
        ),
    )


# --- Runs, snapshots and bindings ---------------------------------------------------------------


@dataclass(frozen=True)
class RunRecord:
    """A compiler ``Run``: its declared logical id, if ``Known``, and its stated instants.

    ``anchor`` is its record-level evidence: the key of its Ledger thread when it declares no
    logical id (Ledger ADR 0003 §2).
    """

    record: RecordId
    logical_id: LogicalId | None
    first: Timestamp | None
    last: Timestamp | None
    anchor: EvidenceRef
    evidence: tuple[EvidenceRef, ...]


def run(record: Mapping[str, object]) -> RunRecord:
    parsed = _strict(run_from_json, record)
    logical_id = _known(parsed.logical_id)
    return RunRecord(
        record=parsed.id,
        logical_id=None if logical_id is None else declared(logical_id),
        first=_known(parsed.first),
        last=_known(parsed.last),
        anchor=parsed.provenance.evidence,
        evidence=_evidence(parsed.provenance, parsed.logical_id),
    )


@dataclass(frozen=True)
class Snapshot:
    """A record a binding can name. ``anchor`` keys its Ledger configuration thread."""

    record: RecordId
    kind: str
    anchor: EvidenceRef


def snapshot(kind: str, record: Mapping[str, object]) -> Snapshot:
    parsed = _strict(SNAPSHOT_KINDS[kind], record)
    provenance: Provenance = parsed.provenance  # type: ignore[attr-defined]
    return Snapshot(parsed.id, kind, provenance.evidence)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Binding:
    """A ``SnapshotBinding``: run ``run`` ran with ``snapshot`` over a window of the run.

    ``windows`` holds each window the validity states, as ``(start, end)`` bounds: one for a
    ``Known`` validity, one per candidate for an ``Ambiguous`` one (``ambiguous``), and
    ``("unstated", "unstated")`` when it states none (ADR 0010 §3).
    """

    record: RecordId
    run: RecordId
    snapshot: RecordId
    snapshot_kind: str
    windows: tuple[tuple[Bound, Bound], ...]
    ambiguous: bool
    assertion_kind: AssertionKind
    evidence: tuple[EvidenceRef, ...]


def binding(record: Mapping[str, object]) -> Binding:
    parsed = _strict(snapshot_binding_from_json, record)
    validity = parsed.validity
    stated: tuple[ValidityWindow, ...] = ()
    if isinstance(validity, Known):
        stated = (validity.value,)
    elif isinstance(validity, Ambiguous):
        stated = tuple(c.value for c in validity.candidates)
    unstated: tuple[Bound, Bound] = ("unstated", "unstated")
    return Binding(
        record=parsed.id,
        run=parsed.run,
        snapshot=parsed.snapshot,
        snapshot_kind=str(parsed.snapshot_kind),
        windows=tuple((_bound(w.start), _bound(w.end)) for w in stated) or (unstated,),
        ambiguous=isinstance(validity, Ambiguous),
        assertion_kind=parsed.provenance.assertion_kind,
        evidence=_evidence(
            parsed.provenance,
            validity,
            *(bound for w in stated for bound in (w.start, w.end)),
        ),
    )


def ids_json(ids: Iterable[LogicalId]) -> list[JsonValue]:
    return [node.to_json() for node in ids]

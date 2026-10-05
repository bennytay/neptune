"""Parsing the Ledger records the calibration history consolidator reads (ADR 0014 §1).

Parsing is kept apart from the policy in ``consolidate.calibration``: each parser turns one Ledger
record into a typed value or raises ``Malformed``, and decides nothing about sensors, series or
deltas. Every kind is read with the compiler's own strict reader, so Memory reads exactly the
package-schema shape:

- ``calibration`` (root ADR 0019 §6): what one declaration states about calibrating one subject;
- ``hardware_configuration`` and ``hardware_component`` (root ADR 0019 §3-§4): the sensors a
  configuration declares, by name, with their declared identifiers and frames;
- ``frame_transform`` (root ADR 0015 §5) and ``frame_binding`` (root ADR 0050 §6): the edges of a
  configuration's frame graph, and which description edge a calibration's extrinsic measures;
- ``maintenance_event`` and ``requalification_record`` (root ADR 0051): the records a calibration
  can be stated to have resulted from;
- ``timestamp_domain``, through ``identity_records.clock``.

A value that is not ``Known`` keeps its state, never a blank: ``Knowledge`` is carried as is
where the policy needs to tell ``Unknown`` from ``KnownAbsent`` from ``Ambiguous``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from neptune.model.alignment import FrameBindingBasis, frame_binding_from_json
from neptune.model.knowledge import Ambiguous, Known
from neptune.model.lifecycle import maintenance_event_from_json, requalification_record_from_json
from neptune.model.machine import (
    ComponentCategory,
    calibration_from_json,
    hardware_component_from_json,
    hardware_configuration_from_json,
)
from neptune.model.provenance import Provenance
from neptune.model.reference import frame_transform_from_json
from neptune_memory.consolidate.configuration_records import Outcome, _declared_id, _strict
from neptune_memory.consolidate.identity_records import declared

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from neptune.model.frames import FrameRef, TransformDirection, TransformValue
    from neptune.model.ids import LogicalId, RecordId
    from neptune.model.knowledge import AssertionKind, Knowledge
    from neptune.model.machine import CalibrationParameter
    from neptune.model.provenance import EvidenceRef
    from neptune.model.time import Timestamp

CALIBRATION: Final = "calibration"
HARDWARE_CONFIGURATION: Final = "hardware_configuration"
HARDWARE_COMPONENT: Final = "hardware_component"
FRAME_TRANSFORM: Final = "frame_transform"
FRAME_BINDING: Final = "frame_binding"
# The lifecycle kinds whose ``configuration`` states what resulted (maintenance) or what was
# requalified (requalification): the records a calibration can be ``calibrated_by``.
PRODUCER_KINDS: Final = ("maintenance_event", "requalification_record")


def cited(knowledge: object) -> tuple[EvidenceRef, ...]:
    """The evidence a value cites of its own; nothing when it inherits its record's."""
    provenance = getattr(knowledge, "provenance", None)
    return (provenance.evidence,) if isinstance(provenance, Provenance) else ()


def _evidence(provenance: Provenance, *values: object) -> tuple[EvidenceRef, ...]:
    return (provenance.evidence, *(ref for value in values for ref in cited(value)))


def _known(knowledge: Knowledge[Any]) -> Any:
    return knowledge.value if isinstance(knowledge, Known) else None


def readings(knowledge: Knowledge[Any], text: Callable[[Any], str] = str) -> Readings:
    """What a declared text may be: one ``Known`` reading, every candidate of an ``Ambiguous``
    one (``ambiguous``), or none when it is not stated."""
    if isinstance(knowledge, Known):
        return Readings((text(knowledge.value),), ambiguous=False)
    if isinstance(knowledge, Ambiguous):
        return Readings(
            tuple(sorted({text(c.value) for c in knowledge.candidates})), ambiguous=True
        )
    return Readings((), ambiguous=False)


@dataclass(frozen=True)
class Readings:
    """The readings of one declared text; ``()`` when it is not stated."""

    values: tuple[str, ...]
    ambiguous: bool


def _version_text(value: object) -> str:
    return value.value  # type: ignore[attr-defined, no-any-return]


@dataclass(frozen=True)
class CalibrationRecord:
    """A compiler ``Calibration``, as the history reads it.

    ``machine`` / ``machines``: what its ``machine`` states (``known``: one id; ``ambiguous``:
    every candidate). ``subject`` and ``revision``: the readings of the declared subject name and
    hardware revision text. The three instants keep their ``Knowledge``.
    ``anchor`` is its record-level evidence, the key of its anchored configuration thread
    (Ledger ADR 0003 §2).
    """

    record: RecordId
    assertion_kind: AssertionKind
    machine: Outcome
    machines: tuple[LogicalId, ...]
    subject: Readings
    revision: Readings
    performed: Knowledge[Timestamp]
    valid_from: Knowledge[Timestamp]
    valid_until: Knowledge[Timestamp]
    parameters: tuple[CalibrationParameter, ...]
    anchor: EvidenceRef
    evidence: tuple[EvidenceRef, ...]


def calibration(record: Mapping[str, object]) -> CalibrationRecord:
    parsed = _strict(calibration_from_json, record)
    machine, machines = _declared_id(parsed.machine)
    return CalibrationRecord(
        record=parsed.id,
        assertion_kind=parsed.provenance.assertion_kind,
        machine=machine,
        machines=machines,
        subject=readings(parsed.subject),
        revision=readings(parsed.hardware_revision, _version_text),
        performed=parsed.performed,
        valid_from=parsed.valid_from,
        valid_until=parsed.valid_until,
        parameters=parsed.parameters,
        anchor=parsed.provenance.evidence,
        evidence=_evidence(
            parsed.provenance,
            parsed.machine,
            parsed.subject,
            parsed.performed,
            parsed.valid_from,
            parsed.valid_until,
        ),
    )


@dataclass(frozen=True)
class Configuration:
    """A ``HardwareConfiguration``: the machine it states it describes, and its revision."""

    record: RecordId
    machine: Outcome
    machines: tuple[LogicalId, ...]
    revision: Readings
    anchor: EvidenceRef
    evidence: tuple[EvidenceRef, ...]


def hardware_configuration(record: Mapping[str, object]) -> Configuration:
    parsed = _strict(hardware_configuration_from_json, record)
    machine, machines = _declared_id(parsed.machine)
    return Configuration(
        record=parsed.id,
        machine=machine,
        machines=machines,
        revision=readings(parsed.revision, _version_text),
        anchor=parsed.provenance.evidence,
        evidence=_evidence(parsed.provenance, parsed.machine, parsed.revision),
    )


@dataclass(frozen=True)
class Component:
    """A ``HardwareComponent``. Only a ``sensor`` is a calibration's subject; every part's frame
    is a frame of its configuration's graph.

    ``identifiers``: its ``Known`` declared ids, each a sensor thread's key (Ledger ADR 0003 §2);
    an ``Ambiguous`` one keys no thread. ``name``: the readings of its declared name; ``frame``: its
    declared frame when ``Known``.
    """

    record: RecordId
    configuration: RecordId
    sensor: bool
    name: Readings
    identifiers: tuple[LogicalId, ...]
    frame: FrameRef | None
    evidence: tuple[EvidenceRef, ...]


def hardware_component(record: Mapping[str, object]) -> Component:
    parsed = _strict(hardware_component_from_json, record)
    frame = _known(parsed.frame)
    known = [declared(i.value) for i in parsed.identifiers if isinstance(i, Known)]
    return Component(
        record=parsed.id,
        configuration=parsed.configuration,
        sensor=parsed.category is ComponentCategory.SENSOR,
        name=readings(parsed.name),
        identifiers=tuple(known),
        frame=frame,
        evidence=_evidence(parsed.provenance, parsed.name, parsed.frame),
    )


@dataclass(frozen=True)
class Transform:
    """A ``FrameTransform``: one edge of one graph and the value it declares, as declared."""

    record: RecordId
    parent: FrameRef
    child: FrameRef
    direction: Knowledge[TransformDirection]
    value: TransformValue
    evidence: tuple[EvidenceRef, ...]


def frame_transform(record: Mapping[str, object]) -> Transform:
    parsed = _strict(frame_transform_from_json, record)
    return Transform(
        record=parsed.id,
        parent=parsed.parent,
        child=parsed.child,
        direction=parsed.direction,
        value=parsed.value,
        evidence=_evidence(parsed.provenance, parsed.direction),
    )


@dataclass(frozen=True)
class Binding:
    """A ``FrameBinding`` with basis ``calibration``: the transform a calibration declares gives
    the edge ``parent -> child`` its value. ``calibrations`` holds the one record a ``Known``
    ``calibration`` names, or every candidate of an ``Ambiguous`` one (``ambiguous``)."""

    record: RecordId
    parent: FrameRef
    child: FrameRef
    transform: RecordId
    calibrations: tuple[RecordId, ...]
    ambiguous: bool
    evidence: tuple[EvidenceRef, ...]


def frame_binding(record: Mapping[str, object]) -> Binding | None:
    parsed = _strict(frame_binding_from_json, record)
    if parsed.basis is not FrameBindingBasis.CALIBRATION:
        return None
    named = parsed.calibration
    calibrations: tuple[RecordId, ...] = ()
    if isinstance(named, Known):
        calibrations = (named.value,)
    elif isinstance(named, Ambiguous):
        calibrations = tuple(c.value for c in named.candidates)
    return Binding(
        record=parsed.id,
        parent=parsed.parent,
        child=parsed.child,
        transform=parsed.transform,
        calibrations=calibrations,
        ambiguous=isinstance(named, Ambiguous),
        evidence=_evidence(parsed.provenance, parsed.calibration),
    )


@dataclass(frozen=True)
class Producer:
    """A maintenance or requalification record, as far as ``calibrated_by`` reads it."""

    record: RecordId
    kind: str
    outcome: Outcome
    configuration: tuple[LogicalId, ...]
    performed: Timestamp | None
    evidence: tuple[EvidenceRef, ...]


_PRODUCER_READERS: Final[Mapping[str, Callable[[object], object]]] = {
    "maintenance_event": maintenance_event_from_json,  # type: ignore[dict-item]
    "requalification_record": requalification_record_from_json,  # type: ignore[dict-item]
}


def producer(kind: str, record: Mapping[str, object]) -> Producer:
    parsed = _strict(_PRODUCER_READERS[kind], record)
    outcome, ids = _declared_id(parsed.configuration)  # type: ignore[attr-defined]
    performed = _known(parsed.performed)  # type: ignore[attr-defined]
    return Producer(
        record=parsed.id,  # type: ignore[attr-defined]
        kind=kind,
        outcome=outcome,
        configuration=ids,
        performed=performed,
        evidence=_evidence(
            parsed.provenance,  # type: ignore[attr-defined]
            parsed.configuration,  # type: ignore[attr-defined]
            parsed.performed,  # type: ignore[attr-defined]
        ),
    )

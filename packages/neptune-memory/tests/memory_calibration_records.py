"""Compiler-shaped calibration records for tests, built with the compiler's own types.

Every ``calibration``, ``hardware_configuration``, ``hardware_component``, ``frame_transform`` and
``frame_binding`` here is constructed as the compiler model class and serialised with its
``to_json``, so a test can never feed the calibration history consolidator a shape the compiler
would not write (root ADRs 0015, 0019 and 0050). Threads are ``ledger_thread`` stand-ins (ADR 0003
§1): a sensor thread is keyed by the component's declared identifier, and a calibration's
configuration thread cites the calibration's own evidence, as the Ledger anchors it (Ledger ADR
0003 §2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, TypeAlias

from memory_identity_records import TRANSFORM, cite, thread
from neptune.identity.ids import record_id
from neptune.identity.provenance import evidence_record_id
from neptune.model.alignment import FrameBinding, FrameBindingBasis
from neptune.model.frames import (
    STATIC,
    FrameRef,
    HomogeneousMatrix,
    MatrixLayout,
    Pose,
    Quaternion,
    QuaternionConvention,
    QuaternionOrder,
    TransformDirection,
    Translation,
)
from neptune.model.ids import LogicalId
from neptune.model.knowledge import AssertionKind, Known, NotApplicable, NotCovered, Unknown
from neptune.model.machine import (
    Calibration,
    CalibrationParameter,
    ComponentCategory,
    HardwareComponent,
    HardwareConfiguration,
)
from neptune.model.provenance import Provenance
from neptune.model.reference import FrameTransform
from neptune.model.time import Timestamp
from neptune.model.units import unit_from_text
from neptune.model.versions import DeclaredVersion
from neptune_memory.schema.nodes import NodeType

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.frames import TransformValue
    from neptune.model.ids import RecordId
    from neptune.model.knowledge import Knowledge

Record = dict[str, object]
OBSERVED, STATED = AssertionKind.OBSERVED, AssertionKind.STATED
if TYPE_CHECKING:
    # A parameter as a test states it: numbers and a unit text (``None``: unit ``Unknown``), or a
    # setting's text, or any ``Knowledge`` value with a unit ``Knowledge``.
    Param: TypeAlias = (
        tuple[tuple[float, ...], str | None] | str | tuple["Knowledge[object]", "Knowledge[object]"]
    )
    When: TypeAlias = "Timestamp | Knowledge[Timestamp] | None"


def graph(name: str) -> RecordId:
    """A frame graph's id: a URDF's, or a calibration file's own."""
    return record_id("frame_graph", {"name": name})


def frame(name: str, graph_name: str) -> FrameRef:
    return FrameRef(name, graph(graph_name))


def _observed(name: str, kind: AssertionKind = OBSERVED) -> Provenance:
    return Provenance(cite(name), TRANSFORM.id, kind)


def _id(kind: str, provenance: Provenance) -> RecordId:
    return evidence_record_id(kind, provenance.evidence, TRANSFORM)


def _when(value: When) -> Knowledge[Timestamp]:
    if value is None:
        return Unknown()
    if isinstance(value, Timestamp):
        return Known(value)
    return value


def _parameter(name: str, param: Param) -> CalibrationParameter:
    if isinstance(param, str):
        return CalibrationParameter(name, Known(param), NotApplicable())
    values, unit = param
    value = Known(values) if isinstance(values, tuple) else values
    declared = unit_from_text(unit) if isinstance(unit, str) or unit is None else unit
    return CalibrationParameter(name, value, declared)  # type: ignore[arg-type]


def calibration(
    name: str,
    *,
    machine: LogicalId | Knowledge[LogicalId] | None,
    subject: str | Knowledge[str] | None,
    valid_from: When = None,
    valid_until: When = None,
    performed: When = None,
    parameters: Mapping[str, Param] | None = None,
    extrinsics: Sequence[RecordId] = (),
    revision: str | None = None,
    kind: AssertionKind = OBSERVED,
) -> Record:
    """A calibration declared by source ``cal/<name>``; its configuration thread cites that."""
    provenance = _observed(f"cal/{name}", kind)
    params = parameters if parameters is not None else {"camera_matrix/data": ((500.0,), "m")}
    return Calibration(
        id=_id("calibration", provenance),
        provenance=provenance,
        machine=Unknown()
        if machine is None
        else Known(machine)
        if isinstance(machine, LogicalId)
        else machine,
        hardware_revision=Known(DeclaredVersion(revision)) if revision else Unknown(),
        subject=Unknown()
        if subject is None
        else Known(subject)
        if isinstance(subject, str)
        else subject,
        performed=_when(performed),
        valid_from=_when(valid_from),
        valid_until=_when(valid_until),
        parameters=tuple(_parameter(n, p) for n, p in sorted(params.items())),
        extrinsics=tuple(sorted(extrinsics)),
    ).to_json()  # type: ignore[return-value]


def hardware(
    name: str,
    machine: LogicalId | Knowledge[LogicalId] | None = None,
    revision: str | None = None,
) -> Record:
    """A hardware configuration declared by source ``hw/<name>`` (a URDF: ``machine`` None)."""
    provenance = _observed(f"hw/{name}")
    return HardwareConfiguration(
        id=_id("hardware_configuration", provenance),
        provenance=provenance,
        machine=NotCovered()
        if machine is None
        else Known(machine)
        if isinstance(machine, LogicalId)
        else machine,
        name=Known(name),
        revision=Known(DeclaredVersion(revision)) if revision else Unknown(),
    ).to_json()  # type: ignore[return-value]


def component(
    configuration: Record,
    name: str | Knowledge[str],
    *,
    serial: str | None = None,
    at: FrameRef | None = None,
    category: ComponentCategory = ComponentCategory.SENSOR,
) -> Record:
    """A component of ``configuration``; ``serial`` is its declared ``("serial", …)`` id."""
    label = name if isinstance(name, str) else "ambiguous"
    provenance = _observed(f"{configuration['id']}/{label}/{category}")
    return HardwareComponent(
        id=_id("hardware_component", provenance),
        provenance=provenance,
        configuration=configuration["id"],  # type: ignore[arg-type]
        category=category,
        name=Known(name) if isinstance(name, str) else name,
        model=Unknown(),
        identifiers=(Known(LogicalId("serial", serial)),) if serial else (),
        frame=Known(at) if at is not None else Unknown(),
    ).to_json()  # type: ignore[return-value]


M: Final = unit_from_text("m")


def pose(
    xyz: tuple[float, float, float],
    quaternion: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0),
    unit: str | None = "m",
    order: QuaternionOrder | None = QuaternionOrder.XYZW,
) -> Pose:
    return Pose(
        Translation(xyz, unit_from_text(unit)),
        Quaternion(
            quaternion,
            order=Known(order) if order else Unknown(),
            convention=Known(QuaternionConvention.HAMILTON),
        ),
    )


def matrix(values: Sequence[float], unit: str | None = "m") -> HomogeneousMatrix:
    return HomogeneousMatrix(
        tuple(values), layout=Known(MatrixLayout.ROW_MAJOR), translation_unit=unit_from_text(unit)
    )


def transform(
    name: str,
    parent: FrameRef,
    child: FrameRef,
    value: TransformValue,
    direction: Knowledge[TransformDirection] | None = None,
) -> Record:
    provenance = _observed(f"tf/{name}")
    return FrameTransform(
        id=_id("frame_transform", provenance),
        provenance=provenance,
        parent=parent,
        child=child,
        direction=direction or Known(TransformDirection.CHILD_TO_PARENT),
        value=value,
        validity=STATIC,
    ).to_json()  # type: ignore[return-value]


def binding(
    name: str,
    parent: FrameRef,
    child: FrameRef,
    tf: Record,
    calibration_record: Record | Knowledge[RecordId] | None,
) -> Record:
    """A frame binding: ``calibration`` basis naming the record, else ``robot_description``."""
    provenance = _observed(f"bind/{name}")
    named: Knowledge[RecordId]
    if calibration_record is None:
        basis, named = FrameBindingBasis.ROBOT_DESCRIPTION, NotApplicable()
    elif isinstance(calibration_record, dict):
        basis, named = FrameBindingBasis.CALIBRATION, Known(calibration_record["id"])  # type: ignore[arg-type]
    else:
        basis, named = FrameBindingBasis.CALIBRATION, calibration_record
    return FrameBinding(
        id=_id("frame_binding", provenance),
        provenance=provenance,
        parent=parent,
        child=child,
        transform=tf["id"],  # type: ignore[arg-type]
        basis=basis,
        calibration=named,
        validity=Unknown(),
    ).to_json()  # type: ignore[return-value]


# --- Threads ------------------------------------------------------------------------------------


def sensor_thread(serial: str) -> Record:
    return thread(
        LogicalId("serial", serial), f"threads/sensor/{serial}", node_type=NodeType.SENSOR
    )


def calibration_thread(name: str) -> Record:
    """The anchored configuration thread of calibration ``name`` (it cites ``cal/<name>``)."""
    return thread(LogicalId("cal", name), f"cal/{name}", node_type=NodeType.CONFIGURATION)


def configuration_thread(node: LogicalId, *hardware_names: str) -> Record:
    """A configuration thread anchored on hardware configurations ``hw/<name>``."""
    return thread(node, *(f"hw/{n}" for n in hardware_names), node_type=NodeType.CONFIGURATION)

"""Clocks and frames as records: what other records' times and poses are expressed in.

``TimestampDomain`` (ADR 0005, ADR 0012) and ``FrameGraph``, ``Frame`` and ``FrameTransform``
(ADR 0007, ADR 0015) are evidence records (ADR 0017): each carries the envelope, a tier-2 id and
the record-level provenance of the evidence that declares it. Their values are the primitives in
``neptune.model.time`` and ``neptune.model.frames``. The records live here, not beside those
primitives, because a record needs ``Provenance`` and provenance's locators need the primitives.
"""

from dataclasses import dataclass
from fractions import Fraction
from typing import ClassVar

from neptune.model._fields import (
    check_type,
    enum_decoder,
    json_array,
    json_bool,
    json_str,
    values_of,
)
from neptune.model.frames import (
    AxisConvention,
    FrameRef,
    Handedness,
    HomogeneousMatrix,
    Pose,
    Static,
    TransformDirection,
    TransformValue,
    Validity,
    frame_ref_from_json,
    transform_value_from_json,
    validity_from_json,
    validity_to_json,
)
from neptune.model.ids import RecordId, check_text
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import Knowledge, Known, from_json, to_json
from neptune.model.provenance import (
    Provenance,
    check_evidence_record,
    evidence_record_json,
    evidence_record_object,
    provenance_from_json,
)
from neptune.model.record import Family
from neptune.model.time import (
    ClockRole,
    Epoch,
    Timescale,
    Timestamp,
    resolution_from_json,
    resolution_to_json,
)


def _check_scope(scope: tuple[str, ...]) -> None:
    if not isinstance(scope, tuple):
        raise TypeError(f"scope must be a tuple of names, got {type(scope).__name__}")
    for part in scope:
        check_text("scope part", part)


def _scope(data: JsonValue) -> tuple[str, ...]:
    return tuple(json_str(part, "scope part") for part in json_array(data, "scope"))


# --- Clocks ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TimestampDomain:
    """One clock of one source (ADR 0005 §2, §3): where its ticks come from and what they mean.

    ``field`` and ``scope`` say which time field of which part of the source the ticks are read
    from, verbatim: ``field="log_time"``, ``scope=("/imu",)``; ``scope=()`` for the whole source.
    The adapter always knows these, so they are structural. Everything that interprets the ticks
    is ``Knowledge``-wrapped and filled only from evidence. ``provenance`` cites what declares the
    field; two clocks declared by one structure (an MCAP channel's log and publish times) cite it
    through finer locators.
    """

    kind: ClassVar[str] = "timestamp_domain"
    family: ClassVar[Family] = Family.REFERENCE
    id: RecordId
    provenance: Provenance
    field: str
    scope: tuple[str, ...]
    role: Knowledge[ClockRole]
    resolution: Knowledge[Fraction]  # seconds per tick, exact and positive
    epoch: Knowledge[Epoch]
    timescale: Knowledge[Timescale]
    declared_monotonic: Knowledge[bool]  # as declared; observed violations are findings

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        check_text("field", self.field)
        _check_scope(self.scope)
        check_type("role", self.role, ClockRole)
        check_type("resolution", self.resolution, Fraction)
        if any(value <= 0 for value in values_of(self.resolution)):
            raise ValueError(f"resolution must be positive: {self.resolution}")
        check_type("epoch", self.epoch, Epoch)
        check_type("timescale", self.timescale, Timescale)
        check_type("declared_monotonic", self.declared_monotonic, bool)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "declared_monotonic": to_json(self.declared_monotonic),
                "epoch": to_json(self.epoch, str),
                "field": self.field,
                "resolution": to_json(self.resolution, resolution_to_json),
                "role": to_json(self.role, str),
                "scope": list(self.scope),
                "timescale": to_json(self.timescale, str),
            },
        )


def timestamp_domain_from_json(data: JsonValue) -> TimestampDomain:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        TimestampDomain.kind,
        {"declared_monotonic", "epoch", "field", "resolution", "role", "scope", "timescale"},
    )
    return TimestampDomain(
        id=record_id,
        provenance=provenance,
        field=json_str(obj["field"], "field"),
        scope=_scope(obj["scope"]),
        role=from_json(obj["role"], enum_decoder(ClockRole), provenance_from_json),
        resolution=from_json(obj["resolution"], resolution_from_json, provenance_from_json),
        epoch=from_json(obj["epoch"], enum_decoder(Epoch), provenance_from_json),
        timescale=from_json(obj["timescale"], enum_decoder(Timescale), provenance_from_json),
        declared_monotonic=from_json(obj["declared_monotonic"], json_bool, provenance_from_json),
    )


# --- Frames ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameGraph:
    """The frames and transforms one part of one source declares (ADR 0007 §2).

    A URDF, one log's tf, one calibration file. ``scope`` names the part the way the source does,
    outermost first: ``()`` for the whole source, which is the usual case and covers both ``/tf``
    and ``/tf_static`` of one log; ``("robot_1",)`` for one model of a multi-robot world file.
    Every ``FrameRef.frame_graph_id`` names one of these. Equal frame names in two graphs are two
    frames until an alignment record (MVL-37) relates them.
    """

    kind: ClassVar[str] = "frame_graph"
    family: ClassVar[Family] = Family.REFERENCE
    id: RecordId
    provenance: Provenance
    scope: tuple[str, ...]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        _check_scope(self.scope)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind, self.id, self.provenance, {"scope": list(self.scope)}
        )


def frame_graph_from_json(data: JsonValue) -> FrameGraph:
    obj, record_id, provenance = evidence_record_object(data, FrameGraph.kind, {"scope"})
    return FrameGraph(id=record_id, provenance=provenance, scope=_scope(obj["scope"]))


@dataclass(frozen=True)
class Frame:
    """What the evidence says about one frame's axes (ADR 0007 §4). There is no default.

    ``handedness`` may be declared without a named convention. When both are ``Known`` they must
    agree: a source that contradicts itself is a finding, and the adapter records ``Ambiguous``.
    """

    kind: ClassVar[str] = "frame"
    family: ClassVar[Family] = Family.REFERENCE
    id: RecordId
    provenance: Provenance
    ref: FrameRef
    axes: Knowledge[AxisConvention]
    handedness: Knowledge[Handedness]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        if not isinstance(self.ref, FrameRef):
            raise TypeError(f"ref must be a FrameRef, got {type(self.ref).__name__}")
        check_type("axes", self.axes, AxisConvention)
        check_type("handedness", self.handedness, Handedness)
        match self.axes, self.handedness:
            case Known(value=axes), Known(value=handedness) if axes.handedness != handedness:
                raise ValueError(f"{axes} is {axes.handedness}-handed, not {handedness}-handed")

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "axes": to_json(self.axes, str),
                "handedness": to_json(self.handedness, str),
                "ref": self.ref.to_json(),
            },
        )


def frame_from_json(data: JsonValue) -> Frame:
    obj, record_id, provenance = evidence_record_object(
        data, Frame.kind, {"axes", "handedness", "ref"}
    )
    return Frame(
        id=record_id,
        provenance=provenance,
        ref=frame_ref_from_json(obj["ref"]),
        axes=from_json(obj["axes"], enum_decoder(AxisConvention), provenance_from_json),
        handedness=from_json(obj["handedness"], enum_decoder(Handedness), provenance_from_json),
    )


@dataclass(frozen=True)
class FrameTransform:
    """One transform between two frames of one graph, exactly as declared (ADR 0007 §3).

    ``parent`` and ``child`` are the roles the source gives the frames (tf's ``header.frame_id``
    and ``child_frame_id``, a URDF joint's links). Where the source has no hierarchy, the adapter's
    descriptor documents which named frame fills which slot, and ``direction`` still says which way
    the values map; if the source does not say, it is ``Ambiguous`` or ``Unknown``, never guessed.
    Relating frames of two graphs is an alignment record (MVL-37), not a transform. Not to be
    confused with ``TransformRecord``, the provenance record of what produced a record.
    """

    kind: ClassVar[str] = "frame_transform"
    family: ClassVar[Family] = Family.REFERENCE
    id: RecordId
    provenance: Provenance
    parent: FrameRef
    child: FrameRef
    direction: Knowledge[TransformDirection]
    value: TransformValue
    validity: Validity

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        for name, ref in (("parent", self.parent), ("child", self.child)):
            if not isinstance(ref, FrameRef):
                raise TypeError(f"{name} must be a FrameRef, got {type(ref).__name__}")
        if self.parent.frame_graph_id != self.child.frame_graph_id:
            raise ValueError("parent and child are in different frame graphs; that is alignment")
        if self.parent == self.child:
            raise ValueError(f"a frame cannot be its own parent: {self.parent.frame_id!r}")
        check_type("direction", self.direction, TransformDirection)
        if not isinstance(self.value, Pose | HomogeneousMatrix):
            raise TypeError(f"value must be a Pose or HomogeneousMatrix, got {self.value!r}")
        if not isinstance(self.validity, Static | Timestamp):
            raise TypeError(f"validity must be STATIC or a Timestamp, got {self.validity!r}")

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "child": self.child.to_json(),
                "direction": to_json(self.direction, str),
                "parent": self.parent.to_json(),
                "validity": validity_to_json(self.validity),
                "value": self.value.to_json(),
            },
        )


def frame_transform_from_json(data: JsonValue) -> FrameTransform:
    """Parse strictly: unexpected or missing keys, unknown kinds and ints for floats are errors."""
    obj, record_id, provenance = evidence_record_object(
        data, FrameTransform.kind, {"child", "direction", "parent", "validity", "value"}
    )
    return FrameTransform(
        id=record_id,
        provenance=provenance,
        parent=frame_ref_from_json(obj["parent"]),
        child=frame_ref_from_json(obj["child"]),
        direction=from_json(
            obj["direction"], enum_decoder(TransformDirection), provenance_from_json
        ),
        value=transform_value_from_json(obj["value"], provenance_from_json),
        validity=validity_from_json(obj["validity"]),
    )

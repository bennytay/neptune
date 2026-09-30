"""Coordinate frames, rotations and frame transforms as the evidence declares them (ADR 0007, 0015).

A ``FrameRef`` is ``(frame_id, frame_graph_id)``: the name exactly as the source writes it, in the
source-scoped graph it belongs to. Equal names in two graphs are two frames until an alignment
record (MVL-37) says otherwise.

Rotation and pose values keep their numbers as declared, in the source's order, and wrap every
interpretation (component order, matrix layout, Euler sequence, units, transform direction) in
``Knowledge``. An undeclared order is ``Unknown`` or ``Ambiguous`` and the numbers are still kept.
Nothing here reorders, normalises, inverts, converts or composes: those are derived transforms.

The records built from these values (``FrameGraph``, ``Frame``, ``FrameTransform``) live in
``neptune.model.reference``: a record carries provenance, whose locators use ``FrameRef``.
"""

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final, TypeAlias

from neptune.model._fields import (
    check_type,
    check_unit,
    enum_decoder,
    exact_object,
    json_str,
    unit_json,
)
from neptune.model.ids import RecordId, check_text, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import Grounding, Knowledge, from_json, to_json
from neptune.model.time import Timestamp, timestamp_from_json
from neptune.model.units import Dimension, Unit, unit_from_json

# Bound on hostile or absurd frame names. Real ones are far shorter.
MAX_TEXT_LENGTH: Final = 256

LENGTH: Final = Dimension(length=1)
ANGLE: Final = Dimension(angle=1)


# --- Frames ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameRef:
    """A frame by its declared name in one frame graph (ADR 0007 §1, §2).

    ``frame_id`` is verbatim: ``/base_link`` and ``base_link`` are different ids. Reconciling them
    is alignment (MVL-37). ``frame_graph_id`` is the graph's tier-2 record id, which the adapter
    derives from the source revision and the part of it the graph covers (a URDF, one log's tf
    stream, one calibration file).
    """

    frame_id: str
    frame_graph_id: RecordId

    def __post_init__(self) -> None:
        if not isinstance(self.frame_id, str):
            raise TypeError(f"frame_id must be a str, got {type(self.frame_id).__name__}")
        check_text("frame_id", self.frame_id)
        if len(self.frame_id) > MAX_TEXT_LENGTH:
            raise ValueError(f"frame_id is longer than {MAX_TEXT_LENGTH} characters")
        parse_record_id(self.frame_graph_id)

    def to_json(self) -> JsonObject:
        return {"frame_graph_id": self.frame_graph_id, "frame_id": self.frame_id}


def frame_ref_from_json(data: JsonValue) -> FrameRef:
    obj = exact_object(data, "frame ref", {"frame_graph_id", "frame_id"})
    return FrameRef(
        json_str(obj["frame_id"], "frame_id"),
        parse_record_id(json_str(obj["frame_graph_id"], "frame_graph_id")),
    )


class Handedness(StrEnum):
    RIGHT = "right"
    LEFT = "left"


class AxisConvention(StrEnum):
    """Named axis conventions: the directions of +x, +y, +z, in that order (ADR 0015 §2).

    E/N/W/S/U/D are earth directions (east, north, west, south, up, down); F/B/L/R/U/D are the
    body's (forward, back, left, right, up, down). ``Known`` only when the source or a normative
    specification of its format declares it; REP-103 as community convention is not grounds.
    """

    ENU = "enu"  # local tangent plane: ROS map/odom by REP-103, GIS
    NED = "ned"  # local tangent plane: PX4, aerospace
    NWU = "nwu"  # local tangent plane: some ROS drivers, marine
    FLU = "flu"  # body: ROS base_link by REP-103
    FRD = "frd"  # body: PX4, aerospace
    RDF = "rdf"  # camera optical: ROS *_optical_frame, OpenCV
    RUB = "rub"  # camera: OpenGL, Blender, ARKit, NeRF
    RUF = "ruf"  # left-handed: Unity
    FRU = "fru"  # left-handed: Unreal Engine

    @property
    def handedness(self) -> Handedness:
        return _HANDEDNESS[self]


_HANDEDNESS: Final[Mapping[AxisConvention, Handedness]] = {
    AxisConvention.ENU: Handedness.RIGHT,
    AxisConvention.NED: Handedness.RIGHT,
    AxisConvention.NWU: Handedness.RIGHT,
    AxisConvention.FLU: Handedness.RIGHT,
    AxisConvention.FRD: Handedness.RIGHT,
    AxisConvention.RDF: Handedness.RIGHT,
    AxisConvention.RUB: Handedness.RIGHT,
    AxisConvention.RUF: Handedness.LEFT,
    AxisConvention.FRU: Handedness.LEFT,
}


# --- Rotations ---------------------------------------------------------------------------------


class QuaternionOrder(StrEnum):
    """The order the source lists the four components in."""

    XYZW = "xyzw"  # ROS geometry_msgs, scipy, Unity
    WXYZ = "wxyz"  # Eigen's constructor, PX4 uORB, many calibration tools


class QuaternionConvention(StrEnum):
    """The quaternion algebra: whether ij = k (Hamilton) or ij = -k (JPL)."""

    HAMILTON = "hamilton"  # ROS, Eigen, most robotics software
    JPL = "jpl"  # some VIO and aerospace code, e.g. OpenVINS


class MatrixLayout(StrEnum):
    """How a flat list of matrix entries is arranged."""

    ROW_MAJOR = "row_major"
    COLUMN_MAJOR = "column_major"


class EulerSequence(StrEnum):
    """The axes, in the order of the declared angles: ``values[i]`` is about ``sequence[i]``."""

    XYZ = "XYZ"
    XZY = "XZY"
    YXZ = "YXZ"
    YZX = "YZX"
    ZXY = "ZXY"
    ZYX = "ZYX"
    XYX = "XYX"
    XZX = "XZX"
    YXY = "YXY"
    YZY = "YZY"
    ZXZ = "ZXZ"
    ZYZ = "ZYZ"


class EulerMode(StrEnum):
    INTRINSIC = "intrinsic"  # about the rotating axes
    EXTRINSIC = "extrinsic"  # about the fixed axes, e.g. URDF rpy


def _check_components(what: str, values: tuple[float, ...], count: int) -> None:
    """Exactly ``count`` finite floats. Norms, orthonormality and bottom rows are not checked."""
    if not isinstance(values, tuple):
        raise TypeError(f"{what} must be a tuple of floats, got {type(values).__name__}")
    if len(values) != count:
        raise ValueError(f"{what} needs {count} components, got {len(values)}")
    for value in values:
        if not isinstance(value, float):
            # 1 and 1.0 are different canonical JSON; the adapter decides once, with float().
            raise TypeError(f"{what} components must be floats, got {value!r}")
        if not math.isfinite(value):
            raise ValueError(f"{what} component {value!r} is not finite")


@dataclass(frozen=True)
class Quaternion:
    kind: ClassVar[str] = "quaternion"
    values: tuple[float, ...]
    order: Knowledge[QuaternionOrder]
    convention: Knowledge[QuaternionConvention]

    def __post_init__(self) -> None:
        _check_components("quaternion", self.values, 4)
        check_type("order", self.order, QuaternionOrder)
        check_type("convention", self.convention, QuaternionConvention)

    def to_json(self) -> JsonObject:
        return {
            "convention": to_json(self.convention, str),
            "kind": self.kind,
            "order": to_json(self.order, str),
            "values": list(self.values),
        }


@dataclass(frozen=True)
class RotationMatrix:
    kind: ClassVar[str] = "rotation_matrix"
    values: tuple[float, ...]
    layout: Knowledge[MatrixLayout]

    def __post_init__(self) -> None:
        _check_components("rotation matrix", self.values, 9)
        check_type("layout", self.layout, MatrixLayout)

    def to_json(self) -> JsonObject:
        return {
            "kind": self.kind,
            "layout": to_json(self.layout, str),
            "values": list(self.values),
        }


@dataclass(frozen=True)
class EulerAngles:
    kind: ClassVar[str] = "euler_angles"
    values: tuple[float, ...]
    sequence: Knowledge[EulerSequence]
    mode: Knowledge[EulerMode]
    unit: Knowledge[Unit]

    def __post_init__(self) -> None:
        _check_components("euler angles", self.values, 3)
        check_type("sequence", self.sequence, EulerSequence)
        check_type("mode", self.mode, EulerMode)
        check_unit("angle unit", self.unit, ANGLE)

    def to_json(self) -> JsonObject:
        return {
            "kind": self.kind,
            "mode": to_json(self.mode, str),
            "sequence": to_json(self.sequence, str),
            "unit": to_json(self.unit, unit_json),
            "values": list(self.values),
        }


@dataclass(frozen=True)
class RotationVector:
    """Axis times angle (Rodrigues vector), e.g. OpenCV's ``rvec``."""

    kind: ClassVar[str] = "rotation_vector"
    values: tuple[float, ...]
    unit: Knowledge[Unit]

    def __post_init__(self) -> None:
        _check_components("rotation vector", self.values, 3)
        check_unit("angle unit", self.unit, ANGLE)

    def to_json(self) -> JsonObject:
        return {
            "kind": self.kind,
            "unit": to_json(self.unit, unit_json),
            "values": list(self.values),
        }


Rotation: TypeAlias = Quaternion | RotationMatrix | EulerAngles | RotationVector
_ROTATIONS: Final = (Quaternion, RotationMatrix, EulerAngles, RotationVector)


# --- Poses -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Translation:
    values: tuple[float, ...]
    unit: Knowledge[Unit]

    def __post_init__(self) -> None:
        _check_components("translation", self.values, 3)
        check_unit("translation unit", self.unit, LENGTH)

    def to_json(self) -> JsonObject:
        return {"unit": to_json(self.unit, unit_json), "values": list(self.values)}


@dataclass(frozen=True)
class Pose:
    """A translation and a rotation, declared separately (tf, URDF ``xyz``/``rpy``)."""

    kind: ClassVar[str] = "pose"
    translation: Translation
    rotation: Rotation

    def __post_init__(self) -> None:
        if not isinstance(self.translation, Translation):
            raise TypeError(f"translation must be a Translation, got {self.translation!r}")
        if not isinstance(self.rotation, _ROTATIONS):
            raise TypeError(f"rotation must be a Rotation, got {self.rotation!r}")

    def to_json(self) -> JsonObject:
        return {
            "kind": self.kind,
            "rotation": self.rotation.to_json(),
            "translation": self.translation.to_json(),
        }


@dataclass(frozen=True)
class HomogeneousMatrix:
    """A 4x4 transform as sixteen declared entries, e.g. Kalibr's ``T_cam_imu``."""

    kind: ClassVar[str] = "homogeneous_matrix"
    values: tuple[float, ...]
    layout: Knowledge[MatrixLayout]
    translation_unit: Knowledge[Unit]

    def __post_init__(self) -> None:
        _check_components("homogeneous matrix", self.values, 16)
        check_type("layout", self.layout, MatrixLayout)
        check_unit("translation unit", self.translation_unit, LENGTH)

    def to_json(self) -> JsonObject:
        return {
            "kind": self.kind,
            "layout": to_json(self.layout, str),
            "translation_unit": to_json(self.translation_unit, unit_json),
            "values": list(self.values),
        }


TransformValue: TypeAlias = Pose | HomogeneousMatrix


# --- Frame transforms --------------------------------------------------------------------------


class TransformDirection(StrEnum):
    """Which way the declared values map coordinates between ``parent`` and ``child``."""

    # Maps coordinates in child into parent: the child's pose in the parent (ROS tf, URDF origin).
    CHILD_TO_PARENT = "child_to_parent"
    PARENT_TO_CHILD = "parent_to_child"


@dataclass(frozen=True)
class Static:
    """The source gives the transform without a time (URDF, ``/tf_static``, a calibration file).

    How long it stays valid is not stated here; that is alignment's and validation's question.
    """

    def to_json(self) -> JsonObject:
        return {"kind": "static"}


STATIC = Static()
Validity: TypeAlias = Static | Timestamp


def validity_to_json(validity: Validity) -> JsonObject:
    """``{"kind":"static"}`` or ``{"kind":"stamped","stamp":{…}}``."""
    if isinstance(validity, Static):
        return validity.to_json()
    return {"kind": "stamped", "stamp": validity.to_json()}


# --- JSON --------------------------------------------------------------------------------------

_Decode: TypeAlias = Callable[[JsonObject], Grounding]


def _components(data: JsonValue, what: str) -> tuple[float, ...]:
    if not isinstance(data, list | tuple) or not all(isinstance(v, float) for v in data):
        raise ValueError(f"{what} values must be an array of floats")
    return tuple(v for v in data if isinstance(v, float))


def _kind(data: JsonValue, what: str, kinds: Mapping[str, set[str]]) -> Mapping[str, JsonValue]:
    """The object, checked to have a known ``kind`` and exactly that kind's keys."""
    if not isinstance(data, Mapping):
        raise ValueError(f"{what} must be a JSON object, got {type(data).__name__}")
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in kinds:
        raise ValueError(f"unknown {what} kind: {kind!r}")
    return exact_object(data, kind, kinds[kind])


_ROTATION_KEYS: Final = {
    Quaternion.kind: {"convention", "kind", "order", "values"},
    RotationMatrix.kind: {"kind", "layout", "values"},
    EulerAngles.kind: {"kind", "mode", "sequence", "unit", "values"},
    RotationVector.kind: {"kind", "unit", "values"},
}


def rotation_from_json(data: JsonValue, decode_provenance: _Decode) -> Rotation:
    obj = _kind(data, "rotation", _ROTATION_KEYS)
    values = _components(obj["values"], "rotation")
    match obj["kind"]:
        case Quaternion.kind:
            return Quaternion(
                values,
                order=from_json(obj["order"], enum_decoder(QuaternionOrder), decode_provenance),
                convention=from_json(
                    obj["convention"], enum_decoder(QuaternionConvention), decode_provenance
                ),
            )
        case RotationMatrix.kind:
            return RotationMatrix(
                values, from_json(obj["layout"], enum_decoder(MatrixLayout), decode_provenance)
            )
        case EulerAngles.kind:
            return EulerAngles(
                values,
                sequence=from_json(obj["sequence"], enum_decoder(EulerSequence), decode_provenance),
                mode=from_json(obj["mode"], enum_decoder(EulerMode), decode_provenance),
                unit=from_json(obj["unit"], unit_from_json, decode_provenance),
            )
        case _:
            return RotationVector(values, from_json(obj["unit"], unit_from_json, decode_provenance))


def translation_from_json(data: JsonValue, decode_provenance: _Decode) -> Translation:
    obj = exact_object(data, "translation", {"unit", "values"})
    return Translation(
        _components(obj["values"], "translation"),
        from_json(obj["unit"], unit_from_json, decode_provenance),
    )


_VALUE_KEYS: Final = {
    Pose.kind: {"kind", "rotation", "translation"},
    HomogeneousMatrix.kind: {"kind", "layout", "translation_unit", "values"},
}


def transform_value_from_json(data: JsonValue, decode_provenance: _Decode) -> TransformValue:
    obj = _kind(data, "transform value", _VALUE_KEYS)
    if obj["kind"] == Pose.kind:
        return Pose(
            translation_from_json(obj["translation"], decode_provenance),
            rotation_from_json(obj["rotation"], decode_provenance),
        )
    return HomogeneousMatrix(
        _components(obj["values"], "homogeneous matrix"),
        layout=from_json(obj["layout"], enum_decoder(MatrixLayout), decode_provenance),
        translation_unit=from_json(obj["translation_unit"], unit_from_json, decode_provenance),
    )


def validity_from_json(data: JsonValue) -> Validity:
    obj = _kind(data, "validity", {"static": {"kind"}, "stamped": {"kind", "stamp"}})
    return STATIC if obj["kind"] == "static" else timestamp_from_json(obj["stamp"])

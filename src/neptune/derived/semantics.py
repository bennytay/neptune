"""What a stream carries, inferred: the ``stream_semantic`` kind and its rules (ADR 0049 §3).

"This stream is an IMU" is an inference, never evidence, so it lives here and in a package's
``derived/`` tables, ``inferred``, and a ``Stream`` never says it (non-negotiable 8). Every line
lists its candidates, each with its confidence (a named band, a ranking, not a probability), every
rule that fired for it, and the evidence those rules read: the stream's declared type name and,
where the registry parsed one, its definition.

Rules read declared type names and field shapes, never topic names: a topic is a name a person
chose (``/battery_voltage`` may well be a ``std_msgs/Float32``), and a type is what the schema
says the bytes are.

- ``known_type`` (0.9): the declared type is a well-known one (ROS 1 and 2, PX4, Foxglove) and its
  parsed layout has the fields that type has. A layout lacking them contradicts the name: the rule
  does not fire, and the driver reports the contradiction.
- ``known_type_unchecked`` (0.8): the same, where no layout could be parsed (``protobuf``, say).
- ``shape_<semantic>`` (0.6; ``shape_odometry`` 0.65, since it is a pose and a twist together):
  the root type's fields have the shape the semantic has, whatever the type is called.

A candidate's confidence is its best rule's. The most confident candidate is the stream's semantic
(``known``); two or more at the top are ``ambiguous`` and none is chosen; none is ``unknown``.
Units by convention (REP 103: metres, radians, seconds) come only with ``known_type`` rules, for
the fields that convention covers, as field paths (a path and everything below it); a joint's unit
depends on whether it is revolute or prismatic, so joint states, trajectories and grippers get none.
"""

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final

from neptune.derived.provenance import (
    DERIVED_SCHEMA_VERSION,
    INFERRED,
    InferredProvenance,
    derived_object,
)
from neptune.derived.schemas import Layout, LayoutState, MessageType, canonical_type_name
from neptune.identity.ids import record_id
from neptune.model._fields import exact_object, json_array, json_str
from neptune.model.ids import RecordId, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json

SEMANTIC_KIND: Final = "stream_semantic"


class Semantic(StrEnum):
    POSE = "pose"
    TWIST = "twist"
    ODOMETRY = "odometry"
    IMU = "imu"
    WRENCH = "wrench"
    TRANSFORM = "transform"
    BATTERY = "battery"
    GNSS = "gnss"
    IMAGE = "image"
    COMPRESSED_IMAGE = "compressed_image"
    POINT_CLOUD = "point_cloud"
    LASER_SCAN = "laser_scan"
    JOINT_STATE = "joint_state"
    JOINT_TRAJECTORY_COMMAND = "joint_trajectory_command"
    DRIVE_COMMAND = "drive_command"
    GRIPPER_COMMAND = "gripper_command"
    VEHICLE_COMMAND = "vehicle_command"


class SemanticState(StrEnum):
    KNOWN = "known"  # one candidate is more confident than every other
    AMBIGUOUS = "ambiguous"  # two or more candidates tie at the top; none was chosen
    UNKNOWN = "unknown"  # no rule fired


KNOWN_TYPE: Final = "known_type"
KNOWN_TYPE_UNCHECKED: Final = "known_type_unchecked"
BANDS: Final[Mapping[str, float]] = {KNOWN_TYPE: 0.9, KNOWN_TYPE_UNCHECKED: 0.8}
SHAPE_BAND: Final = 0.6
COMPOSITE_SHAPE_BAND: Final = 0.65


@dataclass(frozen=True)
class KnownType:
    """A well-known type: what it carries, the root fields its definition has, and the units its
    convention gives fields (a field path and everything below it)."""

    semantic: Semantic
    requires: tuple[str, ...]
    units: tuple[tuple[str, str], ...] = ()


def _k(semantic: Semantic, requires: str, *units: tuple[str, str]) -> KnownType:
    return KnownType(semantic, tuple(requires.split()), units)


_S = Semantic
# Canonical names (``pkg/Name``; ``pkg/msg/Name`` is the same type). ROS 1 and 2 share them.
KNOWN_TYPES: Final[Mapping[str, KnownType]] = {
    # poses, velocities, forces, frames: arms, mobile bases, legged and aerial platforms alike
    "geometry_msgs/Pose": _k(_S.POSE, "position orientation", ("position", "m")),
    "geometry_msgs/PoseStamped": _k(_S.POSE, "header pose", ("pose.position", "m")),
    "geometry_msgs/PoseWithCovariance": _k(_S.POSE, "pose covariance", ("pose.position", "m")),
    "geometry_msgs/PoseWithCovarianceStamped": _k(
        _S.POSE, "header pose", ("pose.pose.position", "m")
    ),
    "geometry_msgs/PoseArray": _k(_S.POSE, "header poses", ("poses[].position", "m")),
    "geometry_msgs/Twist": _k(_S.TWIST, "linear angular", ("linear", "m/s"), ("angular", "rad/s")),
    "geometry_msgs/TwistStamped": _k(
        _S.TWIST, "header twist", ("twist.linear", "m/s"), ("twist.angular", "rad/s")
    ),
    "geometry_msgs/TwistWithCovariance": _k(
        _S.TWIST, "twist covariance", ("twist.linear", "m/s"), ("twist.angular", "rad/s")
    ),
    "geometry_msgs/TwistWithCovarianceStamped": _k(
        _S.TWIST, "header twist", ("twist.twist.linear", "m/s"), ("twist.twist.angular", "rad/s")
    ),
    "geometry_msgs/Wrench": _k(_S.WRENCH, "force torque", ("force", "N"), ("torque", "N.m")),
    "geometry_msgs/WrenchStamped": _k(
        _S.WRENCH, "header wrench", ("wrench.force", "N"), ("wrench.torque", "N.m")
    ),
    "geometry_msgs/Transform": _k(_S.TRANSFORM, "translation rotation", ("translation", "m")),
    "geometry_msgs/TransformStamped": _k(
        _S.TRANSFORM, "header child_frame_id transform", ("transform.translation", "m")
    ),
    "tf2_msgs/TFMessage": _k(
        _S.TRANSFORM, "transforms", ("transforms[].transform.translation", "m")
    ),
    "tf/tfMessage": _k(_S.TRANSFORM, "transforms", ("transforms[].transform.translation", "m")),
    "nav_msgs/Odometry": _k(
        _S.ODOMETRY,
        "header child_frame_id pose twist",
        ("pose.pose.position", "m"),
        ("twist.twist.linear", "m/s"),
        ("twist.twist.angular", "rad/s"),
    ),
    # sensors
    "sensor_msgs/Imu": _k(
        _S.IMU,
        "orientation angular_velocity linear_acceleration",
        ("angular_velocity", "rad/s"),
        ("linear_acceleration", "m/s2"),
    ),
    "sensor_msgs/BatteryState": _k(
        _S.BATTERY,
        "voltage percentage",
        ("voltage", "V"),
        ("current", "A"),
        ("charge", "A.h"),
        ("capacity", "A.h"),
        ("design_capacity", "A.h"),
        ("percentage", "1"),
        ("temperature", "Cel"),
    ),
    "sensor_msgs/NavSatFix": _k(
        _S.GNSS,
        "latitude longitude altitude",
        ("latitude", "deg"),
        ("longitude", "deg"),
        ("altitude", "m"),
    ),
    "sensor_msgs/Image": _k(_S.IMAGE, "height width encoding step data"),
    "sensor_msgs/CompressedImage": _k(_S.COMPRESSED_IMAGE, "format data"),
    "sensor_msgs/PointCloud2": _k(_S.POINT_CLOUD, "height width fields point_step data"),
    "sensor_msgs/PointCloud": _k(_S.POINT_CLOUD, "points channels", ("points", "m")),
    "sensor_msgs/LaserScan": _k(
        _S.LASER_SCAN,
        "angle_min angle_max ranges",
        ("angle_min", "rad"),
        ("angle_max", "rad"),
        ("angle_increment", "rad"),
        ("range_min", "m"),
        ("range_max", "m"),
        ("ranges", "m"),
    ),
    "sensor_msgs/JointState": _k(_S.JOINT_STATE, "name position velocity effort"),
    # commands: manipulators, grippers, car-like bases, flight controllers
    "trajectory_msgs/JointTrajectory": _k(_S.JOINT_TRAJECTORY_COMMAND, "joint_names points"),
    "trajectory_msgs/MultiDOFJointTrajectory": _k(
        _S.JOINT_TRAJECTORY_COMMAND, "joint_names points"
    ),
    # a gripper's position is metres or radians, as its fingers slide or turn: no unit
    "control_msgs/GripperCommand": _k(_S.GRIPPER_COMMAND, "position max_effort"),
    "ackermann_msgs/AckermannDrive": _k(
        _S.DRIVE_COMMAND, "steering_angle speed", ("steering_angle", "rad"), ("speed", "m/s")
    ),
    "ackermann_msgs/AckermannDriveStamped": _k(
        _S.DRIVE_COMMAND,
        "header drive",
        ("drive.steering_angle", "rad"),
        ("drive.speed", "m/s"),
    ),
    "px4_msgs/VehicleCommand": _k(_S.VEHICLE_COMMAND, "command"),
    "px4_msgs/SensorCombined": _k(
        _S.IMU, "gyro_rad accelerometer_m_s2", ("gyro_rad", "rad/s"), ("accelerometer_m_s2", "m/s2")
    ),
    "px4_msgs/BatteryStatus": _k(_S.BATTERY, "voltage_v", ("voltage_v", "V")),
    "px4_msgs/SensorGps": _k(
        _S.GNSS, "latitude_deg longitude_deg", ("latitude_deg", "deg"), ("longitude_deg", "deg")
    ),
    "px4_msgs/VehicleOdometry": _k(_S.ODOMETRY, "position q"),
    # Foxglove's schemas, in protobuf, JSON Schema, flatbuffer or ros2msg
    "foxglove.PoseInFrame": _k(_S.POSE, "timestamp frame_id pose", ("pose.position", "m")),
    "foxglove.PosesInFrame": _k(_S.POSE, "timestamp frame_id poses", ("poses[].position", "m")),
    "foxglove.FrameTransform": _k(
        _S.TRANSFORM, "parent_frame_id child_frame_id translation rotation", ("translation", "m")
    ),
    "foxglove.FrameTransforms": _k(_S.TRANSFORM, "transforms"),
    "foxglove.RawImage": _k(_S.IMAGE, "width height encoding data"),
    "foxglove.CompressedImage": _k(_S.COMPRESSED_IMAGE, "format data"),
    "foxglove.PointCloud": _k(_S.POINT_CLOUD, "fields data point_stride"),
    "foxglove.LaserScan": _k(_S.LASER_SCAN, "ranges", ("ranges", "m")),
    "foxglove.LocationFix": _k(
        _S.GNSS, "latitude longitude", ("latitude", "deg"), ("longitude", "deg"), ("altitude", "m")
    ),
}


def known_type(schema_name: str) -> KnownType | None:
    """The well-known type ``schema_name`` names, by its canonical name, if it is one."""
    return KNOWN_TYPES.get(canonical_type_name(schema_name) or schema_name)


# --- field shapes -------------------------------------------------------------------------------

_NUMERIC: Final = frozenset(
    {
        "float32",
        "float64",
        "int8",
        "uint8",
        "int16",
        "uint16",
        "int32",
        "uint32",
        "int64",
        "uint64",
        "number",
        "integer",
    }
)
_BYTES: Final = frozenset({"uint8", "byte", "char", "integer"})
_TEXT: Final = frozenset({"string", "wstring"})


class _Shapes:
    """Shape predicates over one parsed layout. Nesting is followed at most ``_HOPS`` times."""

    _HOPS: Final = 3

    def __init__(self, layout: Layout) -> None:
        self.by_name = {t.name: t for t in layout.types}
        self.root = self.by_name[layout.root]

    def message(self, owner: MessageType, name: str, array: bool = False) -> MessageType | None:
        field = owner.field(name)
        if field is None or field.primitive or (field.array is not None) != array:
            return None
        return self.by_name.get(field.type)

    def scalar(self, owner: MessageType, name: str, kinds: frozenset[str] = _NUMERIC) -> bool:
        field = owner.field(name)
        return field is not None and field.primitive and field.array is None and field.type in kinds

    def array_of(self, owner: MessageType, name: str, kinds: frozenset[str]) -> bool:
        field = owner.field(name)
        return (
            field is not None
            and field.primitive
            and field.array is not None
            and field.type in kinds
        )

    def vec3(self, owner: MessageType, name: str) -> bool:
        inner = self.message(owner, name)
        return inner is not None and all(self.scalar(inner, axis) for axis in "xyz")

    def quaternion(self, owner: MessageType, name: str) -> bool:
        inner = self.message(owner, name)
        return inner is not None and all(self.scalar(inner, axis) for axis in "xyzw")

    def nested(
        self, owner: MessageType, via: str, test: Callable[[MessageType], bool], hops: int = _HOPS
    ) -> bool:
        """``test`` holds for ``owner``, or for its field ``via``'s type, and so on down."""
        current: MessageType | None = owner
        for _ in range(hops + 1):
            if current is None:
                return False
            if test(current):
                return True
            current = self.message(current, via)
        return False

    def pose(self, t: MessageType) -> bool:
        return self.nested(
            t, "pose", lambda m: self.vec3(m, "position") and self.quaternion(m, "orientation")
        )

    def twist(self, t: MessageType) -> bool:
        return self.nested(t, "twist", lambda m: self.vec3(m, "linear") and self.vec3(m, "angular"))

    def wrench(self, t: MessageType) -> bool:
        return self.nested(t, "wrench", lambda m: self.vec3(m, "force") and self.vec3(m, "torque"))

    def transform(self, t: MessageType) -> bool:
        def one(m: MessageType) -> bool:
            return self.vec3(m, "translation") and self.quaternion(m, "rotation")

        if self.nested(t, "transform", one):
            return True
        inner = self.message(t, "transforms", array=True)
        return inner is not None and self.nested(inner, "transform", one)


def _shape_rules(shapes: _Shapes) -> list[tuple[Semantic, str, float, tuple[str, ...]]]:
    """Every shape rule that fires on the root type: (semantic, rule, band, fields it read)."""
    s, root = shapes, shapes.root
    fired: list[tuple[Semantic, str, float, tuple[str, ...]]] = []

    def fire(semantic: Semantic, fields: str, band: float = SHAPE_BAND) -> None:
        fired.append((semantic, f"shape_{semantic}", band, tuple(fields.split())))

    if s.pose(root):
        fire(_S.POSE, "position orientation" if root.field("position") else "pose")
    if s.twist(root):
        fire(_S.TWIST, "linear angular" if root.field("linear") else "twist")
    pose = s.message(root, "pose")
    twist = s.message(root, "twist")
    if pose is not None and twist is not None and s.pose(pose) and s.twist(twist):
        fire(_S.ODOMETRY, "pose twist", COMPOSITE_SHAPE_BAND)
    if (
        s.quaternion(root, "orientation")
        and s.vec3(root, "angular_velocity")
        and s.vec3(root, "linear_acceleration")
    ):
        fire(_S.IMU, "orientation angular_velocity linear_acceleration")
    if s.wrench(root):
        fire(_S.WRENCH, "force torque" if root.field("force") else "wrench")
    if s.transform(root):
        fire(_S.TRANSFORM, "translation rotation" if root.field("translation") else "transform")
    others = [
        n for n in ("percentage", "charge", "current", "capacity", "remaining") if s.scalar(root, n)
    ]
    if s.scalar(root, "voltage") and others:
        fire(_S.BATTERY, " ".join(("voltage", *others)))
    if s.scalar(root, "latitude") and s.scalar(root, "longitude"):
        fire(_S.GNSS, "latitude longitude")
    if (
        s.scalar(root, "height")
        and s.scalar(root, "width")
        and s.scalar(root, "encoding", _TEXT)
        and s.array_of(root, "data", _BYTES)
    ):
        fire(_S.IMAGE, "height width encoding data")
    elif s.scalar(root, "format", _TEXT) and s.array_of(root, "data", _BYTES):
        fire(_S.COMPRESSED_IMAGE, "format data")
    point = s.message(root, "fields", array=True)
    if (
        point is not None
        and all(point.field(n) is not None for n in ("name", "offset", "datatype"))
        and s.array_of(root, "data", _BYTES)
    ):
        fire(_S.POINT_CLOUD, "fields data")
    if (
        s.scalar(root, "angle_min")
        and s.scalar(root, "angle_max")
        and s.array_of(root, "ranges", _NUMERIC)
    ):
        fire(_S.LASER_SCAN, "angle_min angle_max ranges")
    if s.array_of(root, "name", _TEXT) and s.array_of(root, "position", _NUMERIC):
        fire(_S.JOINT_STATE, "name position")
    points = s.message(root, "points", array=True)
    if (
        s.array_of(root, "joint_names", _TEXT)
        and points is not None
        and s.array_of(points, "positions", _NUMERIC)
    ):
        fire(_S.JOINT_TRAJECTORY_COMMAND, "joint_names points")
    return fired


# --- the record ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Firing:
    """One rule that fired for a candidate, at its band, and what it read."""

    rule: str
    confidence: float
    facts: JsonObject

    def to_json(self) -> JsonObject:
        return {"confidence": self.confidence, "facts": dict(self.facts), "rule": self.rule}


@dataclass(frozen=True)
class UnitHint:
    """A unit a convention gives a field path and every path below it, inferred."""

    field: str
    unit: str

    def to_json(self) -> JsonObject:
        return {"field": self.field, "unit": self.unit}


@dataclass(frozen=True)
class Candidate:
    semantic: Semantic
    confidence: float
    rules: tuple[Firing, ...]
    units: tuple[UnitHint, ...] = ()

    def __post_init__(self) -> None:
        _check_confidence(self.confidence)
        if not self.rules:
            raise ValueError("a candidate has at least one rule that fired")
        if self.confidence != max(rule.confidence for rule in self.rules):
            raise ValueError("a candidate's confidence is its best rule's")

    def to_json(self) -> JsonObject:
        return {
            "confidence": self.confidence,
            "rules": [rule.to_json() for rule in self.rules],
            "semantic": str(self.semantic),
            "units": [unit.to_json() for unit in self.units],
        }


def _check_confidence(value: object) -> float:
    if not isinstance(value, float) or not math.isfinite(value) or not 0.0 < value <= 1.0:
        raise ValueError(f"a confidence is a float in (0, 1], got {value!r}")
    return value


def semantic_id(transform: RecordId, stream: RecordId) -> RecordId:
    return record_id(SEMANTIC_KIND, {"stream": stream, "transform": transform})


def state_of(candidates: tuple[Candidate, ...]) -> SemanticState:
    if not candidates:
        return SemanticState.UNKNOWN
    if len(candidates) > 1 and candidates[1].confidence == candidates[0].confidence:
        return SemanticState.AMBIGUOUS
    return SemanticState.KNOWN


@dataclass(frozen=True)
class StreamSemantic:
    """What a stream carries, inferred (module docstring), as a derived table line.

    ``candidates`` are most confident first (then by name); ``state`` follows from them.
    ``evidence`` is what the rules read: the definition's bytes, then the declared type name's.
    """

    kind: ClassVar[str] = SEMANTIC_KIND
    id: RecordId
    transform: RecordId
    stream: RecordId
    candidates: tuple[Candidate, ...]
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        if self.id != semantic_id(self.transform, self.stream):
            raise ValueError(f"{self.id} is not the id of this stream's semantic")
        order = sorted(self.candidates, key=lambda c: (-c.confidence, str(c.semantic)))
        if list(self.candidates) != order:
            raise ValueError("candidates are most confident first, then by name")
        if len({c.semantic for c in self.candidates}) != len(self.candidates):
            raise ValueError("a semantic is one candidate")
        InferredProvenance(self.evidence, self.transform)  # checks the evidence

    @property
    def assertion_kind(self) -> str:
        return INFERRED

    @property
    def state(self) -> SemanticState:
        return state_of(self.candidates)

    @property
    def semantic(self) -> Semantic | None:
        """The stream's semantic when it is ``known``; ``None`` when ambiguous or unknown."""
        return self.candidates[0].semantic if self.state is SemanticState.KNOWN else None

    @property
    def provenance(self) -> InferredProvenance:
        return InferredProvenance(self.evidence, self.transform)

    def to_json(self) -> JsonObject:
        return {
            "assertion_kind": INFERRED,
            "candidates": [candidate.to_json() for candidate in self.candidates],
            "evidence": [ref.to_json() for ref in self.evidence],
            "id": self.id,
            "kind": SEMANTIC_KIND,
            "schema_version": DERIVED_SCHEMA_VERSION,
            "state": str(self.state),
            "stream": self.stream,
            "transform": self.transform,
        }


def _firing_from_json(data: JsonValue) -> Firing:
    obj = exact_object(data, "rule", {"confidence", "facts", "rule"})
    facts = obj["facts"]
    if not isinstance(facts, Mapping):
        raise ValueError("a rule's facts are a JSON object")
    return Firing(json_str(obj["rule"], "rule"), _check_confidence(obj["confidence"]), facts)


def _candidate_from_json(data: JsonValue) -> Candidate:
    obj = exact_object(data, "candidate", {"confidence", "rules", "semantic", "units"})
    units = []
    for entry in json_array(obj["units"], "units"):
        unit = exact_object(entry, "unit", {"field", "unit"})
        units.append(UnitHint(json_str(unit["field"], "field"), json_str(unit["unit"], "unit")))
    return Candidate(
        Semantic(json_str(obj["semantic"], "semantic")),
        _check_confidence(obj["confidence"]),
        tuple(_firing_from_json(rule) for rule in json_array(obj["rules"], "rules")),
        tuple(units),
    )


def stream_semantic_from_json(data: JsonValue) -> StreamSemantic:
    """Parse strictly; the id and the state must recompute."""
    obj = derived_object(
        data, SEMANTIC_KIND, {"candidates", "evidence", "id", "state", "stream", "transform"}
    )
    semantic = StreamSemantic(
        id=parse_record_id(json_str(obj["id"], "id")),
        transform=parse_record_id(json_str(obj["transform"], "transform")),
        stream=parse_record_id(json_str(obj["stream"], "stream")),
        candidates=tuple(
            _candidate_from_json(c) for c in json_array(obj["candidates"], "candidates")
        ),
        evidence=tuple(evidence_ref_from_json(r) for r in json_array(obj["evidence"], "evidence")),
    )
    if str(semantic.state) != json_str(obj["state"], "state"):
        raise ValueError(f"state {obj['state']!r} is not what the candidates say")
    return semantic


# --- classification -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Classified:
    """A stream's candidates, and the well-known type its layout contradicts, if any."""

    candidates: tuple[Candidate, ...]
    contradicted: str | None = None


def classify(schema_name: str | None, state: LayoutState, layout: Layout | None) -> Classified:
    """The candidates the rules give a declared type name and its layout (``state``)."""
    firings: dict[Semantic, list[Firing]] = {}
    units: dict[Semantic, tuple[UnitHint, ...]] = {}
    contradicted = None
    known = known_type(schema_name) if schema_name is not None else None
    if known is not None and schema_name is not None:
        facts: JsonObject = {"type": canonical_type_name(schema_name) or schema_name}
        root = layout.type(layout.root) if layout is not None else None
        if root is None:
            rule = KNOWN_TYPE_UNCHECKED
        elif all(root.field(name) is not None for name in known.requires):
            rule = KNOWN_TYPE
        else:
            rule, contradicted = None, str(facts["type"])
        if rule is not None:
            firings[known.semantic] = [Firing(rule, BANDS[rule], facts)]
            units[known.semantic] = tuple(UnitHint(f, u) for f, u in known.units)
    if state is LayoutState.KNOWN and layout is not None:
        for semantic, rule, band, fields in _shape_rules(_Shapes(layout)):
            firings.setdefault(semantic, []).append(Firing(rule, band, {"fields": list(fields)}))
    candidates = []
    for semantic, fired in firings.items():
        fired.sort(key=lambda f: (-f.confidence, f.rule))
        candidates.append(
            Candidate(semantic, fired[0].confidence, tuple(fired), units.get(semantic, ()))
        )
    candidates.sort(key=lambda c: (-c.confidence, str(c.semantic)))
    return Classified(tuple(candidates), contradicted)


def stream_semantic(
    transform: RecordId,
    stream: RecordId,
    classified: Classified,
    evidence: Iterable[EvidenceRef],
) -> StreamSemantic:
    refs = tuple(dict.fromkeys(evidence))  # each once, in the order read
    return StreamSemantic(
        semantic_id(transform, stream), transform, stream, classified.candidates, refs
    )

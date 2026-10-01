"""Semantic rules: inferred from declared type names and field shapes, never topics; ties are
ambiguous. Embodiments: arms, grippers, mobile bases, legged, aerial and car-like platforms."""

import pytest

from neptune.derived.schemas import LayoutState, SchemaLimits, parse_definition
from neptune.derived.semantics import (
    KNOWN_TYPE,
    KNOWN_TYPE_UNCHECKED,
    Candidate,
    Classified,
    Firing,
    Semantic,
    SemanticState,
    StreamSemantic,
    classify,
    known_type,
    semantic_id,
    stream_semantic,
    stream_semantic_from_json,
)
from neptune.identity import canonical_json
from neptune.model.ids import ContentId, RecordId
from neptune.model.provenance import ByteRange, EvidenceRef

SEP = "=" * 80
VEC = f"{SEP}\nMSG: geometry_msgs/Vector3\nfloat64 x\nfloat64 y\nfloat64 z\n"
QUAT = f"{SEP}\nMSG: geometry_msgs/Quaternion\nfloat64 x\nfloat64 y\nfloat64 z\nfloat64 w\n"
POINT = f"{SEP}\nMSG: geometry_msgs/Point\nfloat64 x\nfloat64 y\nfloat64 z\n"
POSE = f"{SEP}\nMSG: geometry_msgs/Pose\nPoint position\nQuaternion orientation\n{POINT}{QUAT}"
TWIST = f"{SEP}\nMSG: geometry_msgs/Twist\nVector3 linear\nVector3 angular\n{VEC}"
IMU = (
    "geometry_msgs/Quaternion orientation\ngeometry_msgs/Vector3 angular_velocity\n"
    f"geometry_msgs/Vector3 linear_acceleration\n{QUAT}{VEC}"
)
ODOMETRY = (
    "std_msgs/Header header\nstring child_frame_id\ngeometry_msgs/PoseWithCovariance pose\n"
    "geometry_msgs/TwistWithCovariance twist\n"
    f"{SEP}\nMSG: geometry_msgs/PoseWithCovariance\nPose pose\nfloat64[36] covariance\n{POSE}"
    f"{SEP}\nMSG: geometry_msgs/TwistWithCovariance\nTwist twist\nfloat64[36] covariance\n{TWIST}"
)
JOINT_STATE = "string[] name\nfloat64[] position\nfloat64[] velocity\nfloat64[] effort\n"
TRAJECTORY = (
    "string[] joint_names\ntrajectory_msgs/JointTrajectoryPoint[] points\n"
    f"{SEP}\nMSG: trajectory_msgs/JointTrajectoryPoint\nfloat64[] positions\n"
)
WRENCH = f"geometry_msgs/Vector3 force\ngeometry_msgs/Vector3 torque\n{VEC}"
CLOUD = (
    "uint32 height\nuint32 width\nsensor_msgs/PointField[] fields\nuint32 point_step\n"
    "uint8[] data\n"
    f"{SEP}\nMSG: sensor_msgs/PointField\nstring name\nuint32 offset\nuint8 datatype\n"
    "uint32 count\n"
)
IMAGE = "uint32 height\nuint32 width\nstring encoding\nuint32 step\nuint8[] data\n"
SCAN = "float32 angle_min\nfloat32 angle_max\nfloat32[] ranges\n"
FIX = "float64 latitude\nfloat64 longitude\nfloat64 altitude\n"
TF = (
    "geometry_msgs/TransformStamped[] transforms\n"
    f"{SEP}\nMSG: geometry_msgs/TransformStamped\nstring child_frame_id\nTransform transform\n"
    f"{SEP}\nMSG: geometry_msgs/Transform\nVector3 translation\nQuaternion rotation\n{VEC}{QUAT}"
)


def classified(name: str | None, text: str, encoding: str = "ros2msg") -> Classified:
    parsed = parse_definition(encoding, name, text.encode(), SchemaLimits())
    return classify(name, parsed.state, parsed.layout)


def top(name: str | None, text: str, encoding: str = "ros2msg") -> tuple[str, float, list[str]]:
    result = classified(name, text, encoding)
    best = result.candidates[0]
    return str(best.semantic), best.confidence, [r.rule for r in best.rules]


@pytest.mark.parametrize(
    ("name", "text", "semantic"),
    [
        ("sensor_msgs/msg/Imu", IMU, "imu"),  # legged trunks, aerial, mobile bases
        ("nav_msgs/msg/Odometry", ODOMETRY, "odometry"),  # mobile bases, legged odometry
        ("sensor_msgs/msg/JointState", JOINT_STATE, "joint_state"),  # arms, legs, humanoids
        ("trajectory_msgs/msg/JointTrajectory", TRAJECTORY, "joint_trajectory_command"),
        ("geometry_msgs/msg/Wrench", WRENCH, "wrench"),  # wrist force-torque sensors
        ("sensor_msgs/msg/PointCloud2", CLOUD, "point_cloud"),
        ("sensor_msgs/msg/Image", IMAGE, "image"),
        ("sensor_msgs/msg/LaserScan", SCAN, "laser_scan"),
        ("sensor_msgs/msg/NavSatFix", FIX, "gnss"),
        ("tf2_msgs/msg/TFMessage", TF, "transform"),
        ("geometry_msgs/msg/Twist", TWIST.split("MSG: geometry_msgs/Twist\n", 1)[1], "twist"),
    ],
)
def test_a_known_type_whose_definition_agrees_is_known_at_the_top_band(
    name: str, text: str, semantic: str
) -> None:
    result = classified(name, text)
    assert result.contradicted is None
    best = result.candidates[0]
    assert (str(best.semantic), best.confidence, best.rules[0].rule) == (semantic, 0.9, KNOWN_TYPE)


def test_the_same_shapes_under_custom_names_are_known_by_shape() -> None:
    assert top("my_arm/msg/ArmJoints", JOINT_STATE)[:2] == ("joint_state", 0.6)
    assert top("quadruped/TrunkImu", IMU)[:2] == ("imu", 0.6)
    assert top("base/WheelOdom", ODOMETRY) == ("odometry", 0.65, ["shape_odometry"])
    assert top("ft/Sensor", WRENCH)[:2] == ("wrench", 0.6)
    assert top("cam/Frame", IMAGE)[:2] == ("image", 0.6)
    assert top("lidar/Cloud", CLOUD)[:2] == ("point_cloud", 0.6)
    assert top("planner/Plan", TRAJECTORY)[:2] == ("joint_trajectory_command", 0.6)
    assert top("tf/Tree", TF)[:2] == ("transform", 0.6)


def test_odometry_outranks_the_pose_and_twist_it_holds() -> None:
    result = classified("base/WheelOdom", ODOMETRY)
    assert [(str(c.semantic), c.confidence) for c in result.candidates] == [
        ("odometry", 0.65),
        ("pose", 0.6),
        ("twist", 0.6),
    ]


def test_known_types_carry_their_conventional_units_and_shape_rules_none() -> None:
    imu = classified("sensor_msgs/msg/Imu", IMU).candidates[0]
    assert {(u.field, u.unit) for u in imu.units} == {
        ("angular_velocity", "rad/s"),
        ("linear_acceleration", "m/s2"),
    }
    assert classified("quadruped/TrunkImu", IMU).candidates[0].units == ()
    joints = classified("sensor_msgs/msg/JointState", JOINT_STATE).candidates[0]
    assert joints.units == ()  # revolute or prismatic: no conventional unit


def test_a_known_name_without_a_parsed_layout_is_unchecked() -> None:
    result = classified("foxglove.PoseInFrame", "", "protobuf")
    assert top("foxglove.PoseInFrame", "", "protobuf") == ("pose", 0.8, [KNOWN_TYPE_UNCHECKED])
    assert result.candidates[0].rules[0].facts == {"type": "foxglove.PoseInFrame"}
    assert top("px4_msgs/msg/BatteryStatus", "", "ros2idl")[:2] == ("battery", 0.8)


def test_a_definition_contradicting_a_known_name_is_not_classified_by_it() -> None:
    result = classified("sensor_msgs/msg/Imu", "float32 temperature\n")
    assert result.candidates == ()
    assert result.contradicted == "sensor_msgs/Imu"


def test_topic_names_are_never_read() -> None:
    # A battery voltage published as a bare float carries no battery shape, whatever its topic.
    assert classified("std_msgs/msg/Float32", "float32 data\n").candidates == ()
    assert "topic" not in classify.__code__.co_varnames


def test_a_tie_at_the_top_is_ambiguous_and_names_no_semantic() -> None:
    text = "float64 latitude\nfloat64 longitude\nfloat32 voltage\nfloat32 percentage\n"
    result = classified("telemetry/Combined", text)
    assert [str(c.semantic) for c in result.candidates] == ["battery", "gnss"]
    line = stream_semantic(TRANSFORM, STREAM, result, [REF])
    assert line.state is SemanticState.AMBIGUOUS and line.semantic is None


def test_nothing_fired_is_unknown() -> None:
    line = stream_semantic(TRANSFORM, STREAM, classified("std_msgs/String", "string data\n"), [REF])
    assert line.state is SemanticState.UNKNOWN and line.semantic is None


def test_a_json_schema_battery_is_known_by_shape() -> None:
    data = (
        '{"type":"object",'
        '"properties":{"voltage":{"type":"number"},"percentage":{"type":"number"}}}'
    )
    assert top("fixture.Battery", data, "jsonschema") == ("battery", 0.6, ["shape_battery"])


def test_known_types_cover_every_embodiment_family() -> None:
    for name in (
        "sensor_msgs/msg/JointState",  # arms, legs, humanoids
        "control_msgs/GripperCommand",  # grippers
        "nav_msgs/Odometry",  # mobile bases
        "ackermann_msgs/msg/AckermannDriveStamped",  # car-like platforms
        "px4_msgs/msg/VehicleCommand",  # aerial
        "sensor_msgs/BatteryState",
    ):
        assert known_type(name) is not None, name
    assert known_type("unitree/Unknown") is None


# --- the record ----------------------------------------------------------------------------------

STREAM = RecordId("rec:sha256:" + "1" * 64)
TRANSFORM = RecordId("rec:sha256:" + "2" * 64)
REF = EvidenceRef(ContentId("sha256:" + "3" * 64), (ByteRange(10, 20),))


def test_a_semantic_line_round_trips_and_is_inferred() -> None:
    line = stream_semantic(TRANSFORM, STREAM, classified("nav_msgs/Odometry", ODOMETRY), [REF, REF])
    data = canonical_json.loads(canonical_json.dumps(line.to_json()))
    assert isinstance(data, dict)
    assert stream_semantic_from_json(data) == line
    assert data["assertion_kind"] == "inferred" and data["state"] == "known"
    assert line.evidence == (REF,)
    assert line.provenance.transform == TRANSFORM


def test_a_semantic_line_is_read_strictly() -> None:
    good = dict(stream_semantic(TRANSFORM, STREAM, classified("x/Y", JOINT_STATE), [REF]).to_json())
    for broken in (
        {**good, "state": "ambiguous"},
        {**good, "assertion_kind": "observed"},
        {**good, "evidence": []},
        {**good, "id": "rec:sha256:" + "8" * 64},
    ):
        with pytest.raises((ValueError, TypeError)):
            stream_semantic_from_json(broken)


def test_candidates_must_be_ordered_and_consistent() -> None:
    low = Candidate(Semantic.POSE, 0.6, (Firing("shape_pose", 0.6, {}),))
    high = Candidate(Semantic.ODOMETRY, 0.65, (Firing("shape_odometry", 0.65, {}),))
    with pytest.raises(ValueError, match="most confident"):
        StreamSemantic(semantic_id(TRANSFORM, STREAM), TRANSFORM, STREAM, (low, high), (REF,))
    with pytest.raises(ValueError, match="best rule"):
        Candidate(Semantic.POSE, 0.9, (Firing("shape_pose", 0.6, {}),))
    with pytest.raises(ValueError):
        Candidate(Semantic.POSE, float("nan"), (Firing("shape_pose", 0.6, {}),))


def test_classification_is_deterministic() -> None:
    first = classified("telemetry/Combined", FIX + "float32 voltage\nfloat32 charge\n")
    assert first == classified("telemetry/Combined", FIX + "float32 voltage\nfloat32 charge\n")
    assert classify(None, LayoutState.UNKNOWN, None).candidates == ()

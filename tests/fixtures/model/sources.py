"""The worked examples' source files, as bytes plus the byte range of every part they cite.

Four robots, each with the sources such a team typically has (MVL-70):

- drone: a PX4 flight log (``flight.ulg``);
- quadruped: a ROS 2 bag (``bag/metadata.yaml`` and its MCAP file), a URDF and the mesh it uses;
- manipulator: an MCAP recording, a hand-eye calibration result and the cell's deployment records;
- mobile_robot: a ROS 1 bag, a site register (CSV), a photo taken at a dock (PNG with EXIF) and
  the warehouse deployment's records.

Deployment records (ADR 0051) are JSON exports of the forms, tickets and work orders a deployment
keeps: date-times carry their offset, and every value is written as the people who filled the
forms wrote it.

YAML sources are written in flow style, which is valid YAML and parses as JSON, so tests can
resolve JSON pointers into them with the standard library.
"""

import json
import struct
from dataclasses import dataclass
from typing import Final

from formats import (
    BagConnection,
    Cdr,
    Exif,
    Layout,
    McapChannel,
    McapMessage,
    McapSchema,
    Ros1,
    UlogTopic,
    mcap,
    png,
    ros1_bag,
    ros1_md5,
    ulog,
)

MS: Final = 10**6  # nanoseconds
T0: Final = 1_790_762_400 * 10**9  # 2026-09-30T10:00:00Z, the ROS recordings' zero, ns


@dataclass(frozen=True)
class Source:
    """One file of an example: its path under ``sources/``, its bytes and its layout."""

    path: str
    data: bytes
    layout: Layout


def _json(value: object) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode()


# --- Drone: PX4 ULog ---------------------------------------------------------------------------

DRONE_START_US: Final = 12_000_000  # microseconds since boot when logging began
DRONE_SYS_UUID: Final = "000200000000343233345117003a0027"
DRONE_VER_SW: Final = "2a7d3f1ce8b5f0a9d61c2e7b4f8a3d5c6e9b1f07"
ACCEL: Final = UlogTopic(
    "sensor_accel",
    (
        ("uint64_t", "timestamp"),
        ("uint64_t", "timestamp_sample"),
        ("uint32_t", "device_id"),
        ("float", "x"),
        ("float", "y"),
        ("float", "z"),
    ),
    multi_id=0,
    msg_id=0,
)
GPS: Final = UlogTopic(
    "vehicle_gps_position",
    (
        ("uint64_t", "timestamp"),
        ("uint64_t", "time_utc_usec"),
        ("int32_t", "lat"),
        ("int32_t", "lon"),
    ),
    multi_id=0,
    msg_id=1,
)
DRONE_DATA: Final = (
    (0, (12_100_000, 12_099_800, 1310988, 0.25, -0.5, -9.75)),
    (1, (12_150_000, 1_790_762_400_500_000, -338651000, 1512099000)),
    (0, (12_200_000, 12_199_750, 1310988, 0.5, -0.25, -9.875)),
    (0, (12_400_000, 12_399_800, 1310988, 0.125, 0.0, -9.75)),  # after a 120 ms dropout
    (1, (12_450_000, 1_790_762_400_800_000, -338651100, 1512099100)),
)


def drone() -> list[Source]:
    data, layout = ulog(
        DRONE_START_US,
        infos=(
            ("char[3] sys_name", b"PX4"),
            ("char[11] ver_hw", b"PX4_FMU_V5X"),
            (f"char[{len(DRONE_VER_SW)}] ver_sw", DRONE_VER_SW.encode()),
            (f"char[{len(DRONE_SYS_UUID)}] sys_uuid", DRONE_SYS_UUID.encode()),
        ),
        parameters=(
            ("int32_t CAL_ACC0_ID", struct.pack("<i", 1310988)),
            ("float CAL_ACC0_XOFF", struct.pack("<f", 0.0625)),
            ("float CAL_ACC0_YOFF", struct.pack("<f", -0.03125)),
            ("float CAL_ACC0_ZOFF", struct.pack("<f", 0.1875)),
        ),
        topics=(ACCEL, GPS),
        data=DRONE_DATA,
        dropouts=((3, 120),),
    )
    return [Source("flight.ulg", data, layout)]


# --- ROS 2 message definitions (ros2msg, as rosbag2 stores them) -------------------------------

_HEADER_DEFINITION: Final = (
    "================================================================================\n"
    "MSG: std_msgs/Header\n"
    "builtin_interfaces/Time stamp\n"
    "string frame_id\n"
    "================================================================================\n"
    "MSG: builtin_interfaces/Time\n"
    "int32 sec\n"
    "uint32 nanosec\n"
)
JOINT_STATE: Final = (
    "std_msgs/Header header\n"
    "string[] name\n"
    "float64[] position\n"
    "float64[] velocity\n"
    "float64[] effort\n" + _HEADER_DEFINITION
)
POSE_STAMPED: Final = (
    "std_msgs/Header header\n"
    "geometry_msgs/Pose pose\n"
    + _HEADER_DEFINITION
    + "================================================================================\n"
    "MSG: geometry_msgs/Pose\n"
    "Point position\n"
    "Quaternion orientation\n"
    "================================================================================\n"
    "MSG: geometry_msgs/Point\n"
    "float64 x\n"
    "float64 y\n"
    "float64 z\n"
    "================================================================================\n"
    "MSG: geometry_msgs/Quaternion\n"
    "float64 x 0\n"
    "float64 y 0\n"
    "float64 z 0\n"
    "float64 w 1\n"
)
COMPRESSED_IMAGE: Final = (
    "std_msgs/Header header\nstring format\nuint8[] data\n" + _HEADER_DEFINITION
)
# The offered QoS profile, as rosbag2 (Humble) writes it into metadata and MCAP channels.
QOS: Final = (
    "- history: 3\n"
    "  depth: 0\n"
    "  reliability: 1\n"
    "  durability: 2\n"
    "  deadline:\n"
    "    sec: 2147483647\n"
    "    nsec: 4294967295\n"
    "  lifespan:\n"
    "    sec: 2147483647\n"
    "    nsec: 4294967295\n"
    "  liveliness: 1\n"
    "  liveliness_lease_duration:\n"
    "    sec: 2147483647\n"
    "    nsec: 4294967295\n"
    "  avoid_ros_namespace_conventions: false\n"
)


def _joint_state(stamp: int, positions: tuple[float, ...], names: tuple[str, ...]) -> bytes:
    cdr = Cdr().header(stamp, "").strings(names).float64s(positions)
    return cdr.float64s(()).float64s(()).bytes()


def _pose(stamp: int, x: float, y: float) -> bytes:
    cdr = Cdr().header(stamp, "odom")
    for value in (x, y, 0.3, 0.0, 0.0, 0.0, 1.0):
        cdr.float64(value)
    return cdr.bytes()


# --- Quadruped: ROS 2 bag + URDF + mesh --------------------------------------------------------

LEGS: Final = ("fl_hip", "fr_hip")
QUADRUPED_MESSAGES: Final = (
    McapMessage(1, 0, T0 + 0 * MS, T0 + 0 * MS, _joint_state(T0 - 2 * MS, (0.1, -0.1), LEGS)),
    McapMessage(2, 0, T0 + 5 * MS, T0 + 5 * MS, _pose(T0 + 3 * MS, 0.0, 0.0)),
    McapMessage(1, 1, T0 + 20 * MS, T0 + 20 * MS, _joint_state(T0 + 18 * MS, (0.2, -0.2), LEGS)),
    McapMessage(2, 1, T0 + 25 * MS, T0 + 25 * MS, _pose(T0 + 23 * MS, 0.05, 0.0)),
    McapMessage(1, 2, T0 + 40 * MS, T0 + 40 * MS, _joint_state(T0 + 38 * MS, (0.3, -0.3), LEGS)),
    McapMessage(2, 2, T0 + 45 * MS, T0 + 45 * MS, _pose(T0 + 43 * MS, 0.1, 0.01)),
)
URDF: Final = """<?xml version="1.0"?>
<robot name="quadruped">
  <link name="base">
    <visual>
      <geometry>
        <mesh filename="package://quadruped/meshes/body.stl"/>
      </geometry>
    </visual>
  </link>
  <link name="fl_thigh"/>
  <joint name="fl_hip" type="revolute">
    <parent link="base"/>
    <child link="fl_thigh"/>
    <origin xyz="0.19 0.05 0" rpy="0 0 0"/>
    <axis xyz="1 0 0"/>
    <limit lower="-0.8" upper="0.8" effort="23.7" velocity="30.1"/>
  </joint>
  <link name="front_camera"/>
  <joint name="front_camera_joint" type="fixed">
    <parent link="base"/>
    <child link="front_camera"/>
    <origin xyz="0.28 0 0.05" rpy="0 0.25 0"/>
  </joint>
  <gazebo reference="front_camera">
    <sensor name="front_camera" type="camera"/>
  </gazebo>
</robot>
"""
STL: Final = """solid body
  facet normal 0 0 1
    outer loop
      vertex 0 0 0
      vertex 0.4 0 0
      vertex 0 0.2 0
    endloop
  endfacet
endsolid body
"""


def _text(path: str, text: str, parts: dict[str, str]) -> Source:
    """A text source, with the byte range of each named snippet (its first occurrence)."""
    data = text.encode()
    layout: Layout = {}
    for name, snippet in parts.items():
        start = data.index(snippet.encode())
        layout[name] = (start, len(snippet.encode()))
    return Source(path, data, layout)


def quadruped() -> list[Source]:
    schemas = (
        McapSchema(1, "sensor_msgs/msg/JointState", "ros2msg", JOINT_STATE.encode()),
        McapSchema(2, "geometry_msgs/msg/PoseStamped", "ros2msg", POSE_STAMPED.encode()),
    )
    channels = (
        McapChannel(1, 1, "/joint_states", "cdr", (("offered_qos_profiles", QOS),)),
        McapChannel(2, 2, "/body_pose", "cdr", (("offered_qos_profiles", QOS),)),
    )
    bag, bag_layout = mcap("ros2", schemas, channels, QUADRUPED_MESSAGES)
    counts = {"/joint_states": 3, "/body_pose": 3}
    types = {
        "/joint_states": "sensor_msgs/msg/JointState",
        "/body_pose": "geometry_msgs/msg/PoseStamped",
    }
    metadata = {
        "rosbag2_bagfile_information": {
            "compression_format": "",
            "compression_mode": "",
            "custom_data": {},
            "duration": {"nanoseconds": 45 * MS},
            "files": [
                {
                    "duration": {"nanoseconds": 45 * MS},
                    "message_count": 6,
                    "path": "walk_0.mcap",
                    "starting_time": {"nanoseconds_since_epoch": T0},
                }
            ],
            "message_count": 6,
            "relative_file_paths": ["walk_0.mcap"],
            "ros_distro": "humble",
            "starting_time": {"nanoseconds_since_epoch": T0},
            "storage_identifier": "mcap",
            "topics_with_message_count": [
                {
                    "message_count": counts[topic],
                    "topic_metadata": {
                        "name": topic,
                        "offered_qos_profiles": QOS,
                        "serialization_format": "cdr",
                        "type": types[topic],
                    },
                }
                for topic in ("/joint_states", "/body_pose")
            ],
            "version": 8,
        }
    }
    return [
        Source("bag/metadata.yaml", _json(metadata), {}),
        Source("bag/walk_0.mcap", bag, bag_layout),
        _text(
            "robot.urdf",
            URDF,
            {
                "robot": URDF[URDF.index("<robot") :],
                "link:base": URDF[URDF.index('<link name="base">') : URDF.index("</link>") + 7],
                "link:fl_thigh": '<link name="fl_thigh"/>',
                "link:front_camera": '<link name="front_camera"/>',
                "joint:fl_hip": URDF[
                    URDF.index('<joint name="fl_hip"') : URDF.index("</joint>") + 8
                ],
                "joint:front_camera_joint": URDF[
                    URDF.index('<joint name="front_camera_joint"') : URDF.rindex("</joint>") + 8
                ],
                "origin:fl_hip": '<origin xyz="0.19 0.05 0" rpy="0 0 0"/>',
                "origin:front_camera_joint": '<origin xyz="0.28 0 0.05" rpy="0 0.25 0"/>',
                "mesh": '<mesh filename="package://quadruped/meshes/body.stl"/>',
                "sensor:front_camera": '<sensor name="front_camera" type="camera"/>',
            },
        ),
        Source("meshes/body.stl", STL.encode(), {}),
    ]


# --- Manipulator: MCAP + hand-eye calibration --------------------------------------------------

ARM_JOINTS: Final = ("shoulder_pan", "shoulder_lift", "elbow", "wrist_1", "wrist_2", "wrist_3")
JPEG_STUB: Final = (
    b"\xff\xd8\xff\xdb" + bytes(60) + b"\xff\xd9"
)  # a frame's bytes, not decoded here
MANIPULATOR_MESSAGES: Final = (
    McapMessage(
        1,
        0,
        T0 + 1_000 * MS,
        T0 + 999 * MS,
        _joint_state(T0 + 998 * MS, (0.0, -1.57, 1.57, 0.0, 1.57, 0.0), ARM_JOINTS),
    ),
    McapMessage(
        2,
        0,
        T0 + 1_010 * MS,
        T0 + 1_008 * MS,
        Cdr().header(T0 + 1_005 * MS, "wrist_camera").string("jpeg").octets(JPEG_STUB).bytes(),
    ),
    McapMessage(
        1,
        1,
        T0 + 1_020 * MS,
        T0 + 1_019 * MS,
        _joint_state(T0 + 1_018 * MS, (0.1, -1.5, 1.5, 0.0, 1.57, 0.0), ARM_JOINTS),
    ),
)
HANDEYE: Final = {
    "eye_on_hand": True,
    "robot_base_frame": "base_link",
    "robot_effector_frame": "tool0",
    "tracking_base_frame": "wrist_camera",
    "tracking_marker_frame": "aruco_marker_42",
    "transformation": {
        "qw": 0.7071067811865476,
        "qx": 0.0,
        "qy": 0.0,
        "qz": 0.7071067811865476,
        "x": 0.032,
        "y": -0.011,
        "z": 0.071,
    },
}


# The manipulator cell's records: commissioning to ANSI/RIA R15.08 and ISO 10218-2, its risk
# assessment, a joint-drive replacement and the requalification that returned it to service.
CELL_RECORDS: Final = {
    "cell": "CELL-3",
    "commissioning": {
        "record": "CC-3-001",
        "robot": "UR10e-20415",
        "configuration": "CELL3-CFG-A",
        "date": "2026-09-14T08:30:00+10:00",
        "hardware": [
            {"item": "arm", "model": "UR10e", "serial": "20415"},
            {"item": "gripper", "model": "2F-140", "serial": "G140-7781"},
            {"item": "light curtain", "model": "C4000", "serial": "LC-55102"},
        ],
        "software": [
            {"item": "PolyScope", "version": "5.15.2"},
            {"item": "cell PLC program", "version": "rev 7"},
        ],
        "calibrations": ["HE-0930"],
        "tests": [
            {
                "test": "protective stop response",
                "result": "PASS",
                "date": "2026-09-14T10:05:00+10:00",
            },
            {"test": "light curtain muting", "result": "PASS", "date": "2026-09-14T10:40:00+10:00"},
        ],
        "constraints": ["collaborative operation disabled", "tool speed <= 250 mm/s in zone Z2"],
        "signed_off_by": "Integrator commissioning engineer",
        "signed_off": "2026-09-14T15:00:00+10:00",
        "acceptance": "accepted",
    },
    "risk_assessment": {
        "record": "RA-CELL3",
        "robot": "UR10e-20415",
        "configuration": "CELL3-CFG-A",
        "method": "ISO 12100 risk graph",
        "date": "2026-09-10T13:00:00+10:00",
        "hazards": [
            {
                "hazard": "crush between gripper and fixture",
                "severity": "S2",
                "exposure": "F1",
                "avoidance": "P2",
                "PLr": "d",
                "mitigations": ["light curtain C4000 at cell entry", "reduced speed in zone Z2"],
            },
            {
                "hazard": "ejected part on gripper loss",
                "severity": "S1",
                "exposure": "F2",
                "avoidance": "P1",
                "PLr": "b",
                "mitigations": ["polycarbonate guarding"],
            },
        ],
        "approved_by": "Plant safety officer",
        "approved": "2026-09-11T09:00:00+10:00",
        "decision": "accepted",
    },
    "maintenance": {
        "work_order": "WO-55190",
        "robot": "UR10e-20415",
        "date": "2026-09-29T07:10:00+10:00",
        "diagnosis": "joint 3 encoder fault: 14 protective stops over two shifts",
        "actions": ["replace joint 3 drive", "re-run joint calibration"],
        "parts": [{"part": "joint 3 drive", "removed": "J3-118842", "installed": "J3-120077"}],
        "as_maintained_configuration": "CELL3-CFG-A.1",
    },
    "requalification": {
        "record": "RQ-0019",
        "robot": "UR10e-20415",
        "configuration": "CELL3-CFG-A.1",
        "work_order": "WO-55190",
        "date": "2026-09-29T13:30:00+10:00",
        "cause": "joint 3 drive replaced under WO-55190",
        "corrective_actions": ["joint calibration", "payload and TCP re-entered"],
        "tests": [
            {
                "test": "protective stop response",
                "result": "PASS",
                "date": "2026-09-29T14:00:00+10:00",
            },
            {
                "test": "joint 3 stopping distance",
                "result": "118 mm",
                "date": "2026-09-29T14:20:00+10:00",
            },
        ],
        "result": "pass",
        "return_to_service": "returned to service",
        "returned_by": "Maintenance supervisor",
        "returned": "2026-09-29T15:00:00+10:00",
    },
}


def manipulator() -> list[Source]:
    schemas = (
        McapSchema(1, "sensor_msgs/msg/JointState", "ros2msg", JOINT_STATE.encode()),
        McapSchema(2, "sensor_msgs/msg/CompressedImage", "ros2msg", COMPRESSED_IMAGE.encode()),
    )
    channels = (
        McapChannel(1, 1, "/joint_states", "cdr"),
        McapChannel(2, 2, "/wrist_camera/image/compressed", "cdr"),
    )
    data, layout = mcap("ros2", schemas, channels, MANIPULATOR_MESSAGES)
    return [
        Source("session.mcap", data, layout),
        Source("handeye.yaml", _json(HANDEYE), {}),
        Source("cell/records.json", _json(CELL_RECORDS), {}),
    ]


# --- Mobile robot: ROS 1 bag + site register + photo -------------------------------------------

ROS1_HEADER_MD5: Final = "2176decaecbce78abc3b96ef049fabed"
WHEEL_ODOM_TEXT: Final = "Header header\nfloat64 x\nfloat64 y\nfloat64 yaw\n"
WHEEL_ODOM: Final = BagConnection(
    0,
    "/wheel_odom",
    "husky_examples/WheelOdom",
    ros1_md5(f"{ROS1_HEADER_MD5} header\nfloat64 x\nfloat64 y\nfloat64 yaw"),
    WHEEL_ODOM_TEXT
    + "================================================================================\n"
    "MSG: std_msgs/Header\nuint32 seq\ntime stamp\nstring frame_id\n",
)
BATTERY: Final = BagConnection(
    1, "/battery", "std_msgs/Float32", ros1_md5("float32 data"), "float32 data\n"
)


def _wheel_odom(seq: int, stamp: int, x: float, y: float, yaw: float) -> bytes:
    return Ros1().header(seq, stamp, "odom").float64(x).float64(y).float64(yaw).bytes()


MOBILE_MESSAGES: Final = (
    (0, T0 + 3_600_000 * MS, _wheel_odom(0, T0 + 3_599_990 * MS, 0.0, 0.0, 0.0)),
    (1, T0 + 3_600_020 * MS, struct.pack("<f", 0.875)),
    (0, T0 + 3_600_100 * MS, _wheel_odom(1, T0 + 3_600_090 * MS, 0.12, 0.0, 0.01)),
    (0, T0 + 3_600_200 * MS, _wheel_odom(2, T0 + 3_600_190 * MS, 0.24, 0.01, 0.02)),
)
REGISTER: Final = (
    "site_id,name,aka,latitude,longitude,dock\r\n"
    "S-007,North Plant,NP;Plant 3,-33.8651,151.2099,D1\r\n"
    "S-008,Berth 4,,-33.8612,151.2111,\r\n"
)
DOCK_EXIF: Final = Exif(
    make="FLIR",
    model="Blackfly S BFS-U3-51S5C",
    body_serial="22061345",
    taken="2026:09:30 21:00:12",
    latitude=("S", ((33, 1), (51, 1), (4032, 100))),
    longitude=("E", ((151, 1), (12, 1), (3996, 100))),
)


# The warehouse deployment at site S-007: AMR-07's commissioning, its authorisation, a remote
# assist, a collision with a rack, the map and zone change that followed, and the risk assessment.
WAREHOUSE_RECORDS: Final = {
    "site": "S-007",
    "commissioning": [
        {
            "form": "COM-0042",
            "machine": "AMR-07",
            "configuration": "CFG-AMR07-r3",
            "commissioned": "2026-09-21T09:00:00+10:00",
            "hardware": [
                {"item": "drive unit", "model": "DU-200", "serial": "DU2-55120", "revision": "C"},
                {
                    "item": "safety lidar",
                    "model": "microScan3",
                    "serial": "23110457",
                    "revision": "1.4",
                },
            ],
            "software": [
                {"item": "navigation stack", "version": "2.4.1"},
                {"item": "safety controller firmware", "version": "V01.03.02"},
            ],
            "calibrations": ["CAL-LIDAR-0912"],
            "tests": [
                {"test": "emergency stop", "result": "PASS", "date": "2026-09-21T10:15:00+10:00"},
                {
                    "test": "braking distance at 1.5 m/s",
                    "result": "0.92 m",
                    "date": "2026-09-21T10:40:00+10:00",
                },
            ],
            "constraints": [
                "no operation in aisle 14 during forklift shift change",
                "ambient temperature at most 35 C",
            ],
            "signed_off_by": "Site safety lead",
            "signed_off": "2026-09-21T16:00:00+10:00",
            "acceptance": "accepted",
        }
    ],
    "authorisations": [
        {
            "authorisation": "AUTH-0042-1",
            "commissioning": "COM-0042",
            "machine": "AMR-07",
            "configuration": "CFG-AMR07-r3",
            "missions": ["tote transport", "empty pallet return"],
            "payload": {"min": 0, "max": 150, "unit": "kg"},
            "zones": [
                {"zone": "PICK-A", "speed_limit": 1.5, "unit": "m/s"},
                {"zone": "DOCK-1", "speed_limit": 0.8, "unit": "m/s"},
            ],
            "supervision": "remote supervision, 1 operator : 6 robots",
            "dependencies": ["Wi-Fi coverage AP-3 to AP-9", "fire door interlock FD-2"],
            "valid_from": "2026-09-22T00:00:00+10:00",
            "valid_until": "2027-03-22T00:00:00+10:00",
            "decision": "approved with conditions",
            "approved_by": "Operations manager",
            "approved": "2026-09-21T17:30:00+10:00",
        }
    ],
    "interventions": [
        {
            "ticket": "INT-1187",
            "machine": "AMR-07",
            "mode": "remote assist",
            "authority": "level 2 remote operator",
            "reason": "blocked by a pallet in aisle 12",
            "commands": ["pause", "set waypoint W-12-3", "resume"],
            "start": "2026-09-24T14:02:10+10:00",
            "end": "2026-09-24T14:05:41+10:00",
            "outcome": "mission completed",
        }
    ],
    "incidents": [
        {
            "incident": "INC-0007",
            "severity": "S3",
            "occurred": "2026-09-25T04:12:00+10:00",
            "zone": "DOCK-1",
            "location": "dock door 4",
            "machines": ["AMR-07"],
            "assets": ["RACK-R12"],
            "timeline": [
                {"time": "2026-09-25T04:12:00+10:00", "entry": "AMR-07 touched rack upright R12"},
                {"time": "2026-09-25T04:12:01+10:00", "entry": "bumper stop; robot halted"},
            ],
            "description": "low-speed contact with a rack upright while docking",
            "root_cause": "map offset after rack R12 was moved",
            "evidence": ["VID-20260925-0412", "drive.bag"],
        }
    ],
    "changes": [
        {
            "change": "CHG-0031",
            "incident": "INC-0007",
            "machines": ["AMR-07"],
            "configuration": "CFG-AMR07-r4",
            "items": [
                {"type": "map", "target": "warehouse map", "from": "r12", "to": "r13"},
                {
                    "type": "zone",
                    "target": "DOCK-1 speed limit",
                    "from": "0.8 m/s",
                    "to": "0.5 m/s",
                },
            ],
            "decision": "approved",
            "approved_by": "Site safety lead",
            "approved": "2026-09-26T11:00:00+10:00",
            "effective": "2026-09-27T06:00:00+10:00",
            "rollback": "map r12",
        }
    ],
    "risk_assessments": [
        {
            "assessment": "RA-0042",
            "machines": ["AMR-07"],
            "configuration": "CFG-AMR07-r3",
            "method": "ISO 3691-4 Annex A risk matrix",
            "assessed": "2026-09-18T10:00:00+10:00",
            "hazards": [
                {
                    "hazard": "collision with a pedestrian in PICK-A",
                    "severity": "high",
                    "likelihood": "unlikely",
                    "risk": "12",
                    "mitigations": ["safety lidar protective field", "speed limit 1.5 m/s"],
                }
            ],
            "decision": "accepted",
            "approved_by": "Site safety lead",
            "approved": "2026-09-19T09:00:00+10:00",
        }
    ],
}


def mobile_robot() -> list[Source]:
    bag, bag_layout = ros1_bag((WHEEL_ODOM, BATTERY), MOBILE_MESSAGES)
    photo, photo_layout = png(8, 6, (96, 110, 120), DOCK_EXIF)
    return [
        Source("drive.bag", bag, bag_layout),
        Source("sites.csv", REGISTER.encode(), {}),
        Source("photos/dock.png", photo, photo_layout),
        Source("deployment/records.json", _json(WAREHOUSE_RECORDS), {}),
    ]


EXAMPLES: Final = {
    "drone": drone,
    "quadruped": quadruped,
    "manipulator": manipulator,
    "mobile_robot": mobile_robot,
}

"""Generate the frame-alignment fixtures: three ROS 2 recordings as MCAP, one per embodiment.

    uv run --no-project --with rosbags==0.11.5 python tests/fixtures/frames/make_frames.py
    uv run --no-project --with rosbags==0.11.5 --with mcap==1.2.2 \
        python tests/fixtures/frames/make_frames.py --oracle

The first form writes the recordings next to this file; payloads are serialised by ``rosbags``
(the official-reader stand-in, never a project dependency), so the decoder under test never wrote
the bytes it reads. The second decodes every payload of these recordings, of
``tests/fixtures/rosbag1/robot_none.bag``, of ``tests/fixtures/rosbag2/mobile_base_sqlite3`` and
of ``tests/fixtures/mcap/robot.mcap`` with ``rosbags`` and writes what it reads, flattened to the
decoder's column paths, as ``oracle.json``. The MCAP files are written by the small writer below
(unchunked, no summary: the adapter plans them by scanning), so their bytes depend only on this
script and the pinned ``rosbags``.

The recordings (times in ns from ``T0``):

- ``arm_cell.mcap``: a six-axis arm on a fixed stand (``ros2msg``). ``/tf_static`` places
  ``base_link`` on ``world`` and the gripper on ``tool0``; ``/tf`` moves the chain
  ``base_link → shoulder_link → upper_arm_link → forearm_link → wrist_link → tool0``;
  ``/joint_states`` leaves ``header.frame_id`` empty (as arm drivers do); ``/ft_sensor``
  (``WrenchStamped``) is in ``ft_sensor_link``, a frame no transform names.
- ``quadruped_walk.mcap``: a legged robot (``ros2idl`` schemas, MCAP's layout of the IDL
  rosidl generates). ``/tf_static`` holds ``body → imu``, ``body → cam0``, ``body → cam1`` and
  four hips; ``/tf`` moves ``odom → body``; ``/imu`` is in ``imu``. One ``/imu`` message is
  big-endian CDR. Kalibr's ``quadruped_camchain_imucam.yaml`` names the same ``cam0``, ``cam1``
  and ``imu``.
- ``usv_survey.mcap``: an uncrewed surface vessel (``ros2msg``). ``/fix`` (``NavSatFix``) is in
  ``gps``; ``/tf_static`` holds ``base_link → gps`` and ``base_link → sonar``; ``/tf`` moves
  ``odom → base_link``; ``/sonar/range`` (``Range``) is in ``sonar``; ``/dvl``
  (``TwistWithCovarianceStamped``) is in ``dvl_link``, which no transform names; ``/imu`` writes
  its frame ``/base_link`` (a ROS 1 habit) beside ``base_link``; one ``/sonar/range`` payload is
  cut short.
"""

import json
import struct
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

HERE: Final = Path(__file__).parent
ROOT: Final = HERE.parents[2]
MAGIC: Final = b"\x89MCAP0\r\n"
T0: Final = 1_800_000_000 * 10**9
MS: Final = 10**6


# --- A minimal MCAP writer: Header, Schemas, Channels, Messages, Data End, Footer ---------------


def _string(text: str) -> bytes:
    data = text.encode()
    return struct.pack("<I", len(data)) + data


def _record(opcode: int, content: bytes) -> bytes:
    return struct.pack("<BQ", opcode, len(content)) + content


@dataclass(frozen=True)
class Channel:
    id: int
    topic: str
    type: str  # pkg/msg/Name


@dataclass(frozen=True)
class Message:
    channel: int
    time: int
    data: bytes


def write_mcap(
    encoding: str, schemas: dict[str, str], channels: list[Channel], messages: list[Message]
) -> bytes:
    """``schemas``: type name to definition text, in ``encoding`` (``ros2msg`` or ``ros2idl``)."""
    out = bytearray(MAGIC)
    out += _record(0x01, _string("ros2") + _string("neptune frames fixture"))
    ids = {name: i + 1 for i, name in enumerate(sorted(schemas))}
    for name, schema_id in ids.items():
        data = schemas[name].encode()
        content = struct.pack("<H", schema_id) + _string(name) + _string(encoding)
        out += _record(0x03, content + struct.pack("<I", len(data)) + data)
    for channel in channels:
        content = struct.pack("<HH", channel.id, ids[channel.type])
        content += _string(channel.topic) + _string("cdr") + struct.pack("<I", 0)
        out += _record(0x04, content)
    sequence: dict[int, int] = {}
    for message in sorted(messages, key=lambda m: (m.time, m.channel)):
        sequence[message.channel] = sequence.get(message.channel, 0) + 1
        head = struct.pack(
            "<HIQQ", message.channel, sequence[message.channel], message.time, message.time
        )
        out += _record(0x05, head + message.data)
    out += _record(0x0F, struct.pack("<I", 0))
    out += _record(0x02, struct.pack("<QQI", 0, 0, 0))
    return bytes(out + MAGIC)


# --- Messages, serialised by rosbags ------------------------------------------------------------


def _store() -> Any:
    from rosbags.typesys import Stores, get_typestore  # type: ignore[import-not-found]

    return get_typestore(Stores.ROS2_HUMBLE)


class Build:
    def __init__(self) -> None:
        import numpy  # type: ignore[import-not-found]

        self.np = numpy
        self.ts = _store()
        self.t = self.ts.types

    def stamp(self, ns: int) -> Any:
        sec, nanosec = divmod(ns, 10**9)
        return self.t["builtin_interfaces/msg/Time"](sec=sec, nanosec=nanosec)

    def header(self, ns: int, frame: str) -> Any:
        return self.t["std_msgs/msg/Header"](stamp=self.stamp(ns), frame_id=frame)

    def vector(self, x: float, y: float, z: float) -> Any:
        return self.t["geometry_msgs/msg/Vector3"](x=x, y=y, z=z)

    def quaternion(self, x: float, y: float, z: float, w: float) -> Any:
        return self.t["geometry_msgs/msg/Quaternion"](x=x, y=y, z=z, w=w)

    def transform(
        self,
        ns: int,
        parent: str,
        child: str,
        xyz: tuple[float, float, float],
        q: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0),
    ) -> Any:
        return self.t["geometry_msgs/msg/TransformStamped"](
            header=self.header(ns, parent),
            child_frame_id=child,
            transform=self.t["geometry_msgs/msg/Transform"](
                translation=self.vector(*xyz), rotation=self.quaternion(*q)
            ),
        )

    def tf(self, transforms: list[Any]) -> Any:
        return self.t["tf2_msgs/msg/TFMessage"](transforms=transforms)

    def cdr(self, message: Any, typename: str, little: bool = True) -> bytes:
        return bytes(self.ts.serialize_cdr(message, typename, little_endian=little))

    def covariance(self, n: int, first: float) -> Any:
        values = [0.0] * n
        values[0] = first
        return self.np.array(values, dtype=self.np.float64)

    def imu(self, ns: int, frame: str, az: float) -> Any:
        return self.t["sensor_msgs/msg/Imu"](
            header=self.header(ns, frame),
            orientation=self.quaternion(0.0, 0.0, 0.0, 1.0),
            orientation_covariance=self.covariance(9, 0.01),
            angular_velocity=self.vector(0.0, 0.0, 0.02),
            angular_velocity_covariance=self.covariance(9, 0.001),
            linear_acceleration=self.vector(0.0, 0.0, az),
            linear_acceleration_covariance=self.covariance(9, 0.1),
        )


def msgdef(build: Build, typename: str) -> str:
    text, _ = build.ts.generate_msgdef(typename, ros_version=2)
    return str(text)


# --- IDL as rosidl writes it, for the quadruped --------------------------------------------------

_IDL_TYPES: Final = {
    "float64": "double",
    "float32": "float",
    "int32": "int32",
    "uint32": "uint32",
    "uint8": "uint8",
    "string": "string",
}


def idl(build: Build, typename: str) -> str:
    """The definition of ``typename`` and every type it uses, each as rosidl's IDL, in MCAP's
    ``ros2idl`` layout: the root first, then each dependency after a separator line and an
    ``IDL: <name>`` line."""
    order: list[str] = []

    def visit(name: str) -> None:
        if name in order:
            return
        order.append(name)
        _, fields = build.ts.fielddefs[name]
        for _, (kind, detail) in fields:
            if kind == 2:  # a message type, by name
                visit(detail)
            elif kind in (3, 4) and detail[0][0] == 2:  # an array or sequence of messages
                visit(detail[0][1])

    visit(typename)
    sections = [_idl_one(build, name) for name in order]
    out = sections[0]
    for name, text in zip(order[1:], sections[1:], strict=True):
        out += "=" * 80 + "\n" + f"IDL: {name}\n" + text
    return out


def _idl_one(build: Build, name: str) -> str:
    package, _, short = name.split("/")
    _, fields = build.ts.fielddefs[name]
    typedefs: list[str] = []
    members: list[str] = []
    for field, (kind, detail) in fields:
        if kind == 1:  # a primitive: (name, bound)
            members.append(f"      {_idl_base(detail)} {field};")
        elif kind == 2:  # a message
            members.append(f"      {detail.replace('/', '::')} {field};")
        elif kind == 3:  # a fixed array: ((kind, detail), length)
            element, length = detail
            base = _idl_element(element)
            alias = f"{base.replace('::', '__')}__{length}"
            if f"    typedef {base} {alias}[{length}];" not in typedefs:
                typedefs.append(f"    typedef {base} {alias}[{length}];")
            members.append(f"      {alias} {field};")
        elif kind == 4:  # a sequence: ((kind, detail), bound)
            element, bound = detail
            base = _idl_element(element)
            members.append(f"      sequence<{base}{', ' + str(bound) if bound else ''}> {field};")
    lines = [
        "// generated from rosidl_adapter/resource/msg.idl.em",
        f"// with input from {name}.msg",
        "// generated code does not contain a copyright notice",
        "",
        f"module {package} {{",
        "  module msg {",
        *typedefs,
        '    @verbatim (language="comment", text=',
        f'      "{short}, as the fixture writes it.")',
        f"    struct {short} {{",
        *members,
        "    };",
        "  };",
        "};",
        "",
    ]
    return "\n".join(lines)


def _idl_base(detail: Any) -> str:
    primitive, bound = detail if isinstance(detail, tuple) else (detail, 0)
    if primitive == "string" and bound:
        return f"string<{bound}>"
    return _IDL_TYPES[primitive]


def _idl_element(element: Any) -> str:
    kind, detail = element
    return _idl_base(detail) if kind == 1 else detail.replace("/", "::")


# --- The recordings -----------------------------------------------------------------------------

ARM_CHAIN: Final = (
    ("base_link", "shoulder_link", (0.0, 0.0, 0.1625)),
    ("shoulder_link", "upper_arm_link", (0.0, 0.138, 0.0)),
    ("upper_arm_link", "forearm_link", (0.0, -0.131, 0.425)),
    ("forearm_link", "wrist_link", (0.0, 0.0, 0.3922)),
    ("wrist_link", "tool0", (0.0, 0.0996, 0.0)),
)
ARM_JOINTS: Final = ("shoulder_pan", "shoulder_lift", "elbow", "wrist_1", "wrist_2", "wrist_3")


def arm_cell(build: Build) -> bytes:
    t = build.t
    channels = [
        Channel(1, "/tf_static", "tf2_msgs/msg/TFMessage"),
        Channel(2, "/tf", "tf2_msgs/msg/TFMessage"),
        Channel(3, "/joint_states", "sensor_msgs/msg/JointState"),
        Channel(4, "/ft_sensor", "geometry_msgs/msg/WrenchStamped"),
    ]
    messages = []
    static = build.tf(
        [
            build.transform(T0, "world", "base_link", (0.0, 0.0, 0.75)),
            build.transform(T0, "tool0", "gripper", (0.0, 0.0, 0.12)),
        ]
    )
    messages.append(Message(1, T0, build.cdr(static, "tf2_msgs/msg/TFMessage")))
    for step in range(4):
        ns = T0 + (10 + 50 * step) * MS
        angle = 0.01 * step
        moving = [
            build.transform(ns, parent, child, xyz, (0.0, 0.0, angle, (1 - angle * angle) ** 0.5))
            for parent, child, xyz in ARM_CHAIN
        ]
        messages.append(Message(2, ns, build.cdr(build.tf(moving), "tf2_msgs/msg/TFMessage")))
        joints = t["sensor_msgs/msg/JointState"](
            header=build.header(ns + 2 * MS, ""),
            name=list(ARM_JOINTS),
            position=build.np.array([0.1 * i + angle for i in range(6)], dtype=build.np.float64),
            velocity=build.np.array([0.01] * 6, dtype=build.np.float64),
            effort=build.np.array([], dtype=build.np.float64),
        )
        messages.append(Message(3, ns + 2 * MS, build.cdr(joints, "sensor_msgs/msg/JointState")))
        wrench = t["geometry_msgs/msg/WrenchStamped"](
            header=build.header(ns + 4 * MS, "ft_sensor_link"),
            wrench=t["geometry_msgs/msg/Wrench"](
                force=build.vector(0.0, 0.0, -9.8 - step), torque=build.vector(0.0, 0.01, 0.0)
            ),
        )
        messages.append(
            Message(4, ns + 4 * MS, build.cdr(wrench, "geometry_msgs/msg/WrenchStamped"))
        )
    schemas = {c.type: msgdef(build, c.type) for c in channels}
    return write_mcap("ros2msg", schemas, channels, messages)


HIPS: Final = ("front_left_hip", "front_right_hip", "rear_left_hip", "rear_right_hip")


def quadruped_walk(build: Build) -> bytes:
    channels = [
        Channel(1, "/tf_static", "tf2_msgs/msg/TFMessage"),
        Channel(2, "/tf", "tf2_msgs/msg/TFMessage"),
        Channel(3, "/imu", "sensor_msgs/msg/Imu"),
    ]
    static = [
        build.transform(T0, "body", "imu", (0.0, 0.0, 0.05)),
        build.transform(T0, "body", "cam0", (0.3, 0.05, 0.1)),
        build.transform(T0, "body", "cam1", (0.3, -0.05, 0.1)),
    ]
    for k, hip in enumerate(HIPS):
        static.append(
            build.transform(
                T0, "body", hip, (0.2 if k < 2 else -0.2, 0.1 if k % 2 == 0 else -0.1, 0.0)
            )
        )
    messages = [Message(1, T0, build.cdr(build.tf(static), "tf2_msgs/msg/TFMessage"))]
    for step in range(5):
        ns = T0 + (20 + 40 * step) * MS
        odom = build.tf([build.transform(ns, "odom", "body", (0.1 * step, 0.0, 0.32))])
        messages.append(Message(2, ns, build.cdr(odom, "tf2_msgs/msg/TFMessage")))
        imu = build.imu(ns + 5 * MS, "imu", 9.81 + 0.01 * step)
        little = step != 3  # one publisher wrote big-endian CDR
        messages.append(Message(3, ns + 5 * MS, build.cdr(imu, "sensor_msgs/msg/Imu", little)))
    schemas = {c.type: idl(build, c.type) for c in channels}
    return write_mcap("ros2idl", schemas, channels, messages)


def usv_survey(build: Build) -> bytes:
    t = build.t
    channels = [
        Channel(1, "/tf_static", "tf2_msgs/msg/TFMessage"),
        Channel(2, "/tf", "tf2_msgs/msg/TFMessage"),
        Channel(3, "/fix", "sensor_msgs/msg/NavSatFix"),
        Channel(4, "/sonar/range", "sensor_msgs/msg/Range"),
        Channel(5, "/dvl", "geometry_msgs/msg/TwistWithCovarianceStamped"),
        Channel(6, "/imu", "sensor_msgs/msg/Imu"),
    ]
    static = build.tf(
        [
            build.transform(T0, "base_link", "gps", (0.0, 0.0, 1.2)),
            build.transform(T0, "base_link", "sonar", (0.4, 0.0, -0.5)),
        ]
    )
    messages = [Message(1, T0, build.cdr(static, "tf2_msgs/msg/TFMessage"))]
    for step in range(4):
        ns = T0 + (30 + 100 * step) * MS
        odom = build.tf([build.transform(ns, "odom", "base_link", (1.5 * step, 0.2 * step, 0.0))])
        messages.append(Message(2, ns, build.cdr(odom, "tf2_msgs/msg/TFMessage")))
        fix = t["sensor_msgs/msg/NavSatFix"](
            header=build.header(ns + MS, "gps"),
            status=t["sensor_msgs/msg/NavSatStatus"](status=0, service=1),
            latitude=-33.8568 + 1e-5 * step,
            longitude=151.2153 + 2e-5 * step,
            altitude=1.2,
            position_covariance=build.covariance(9, 4.0),
            position_covariance_type=2,
        )
        messages.append(Message(3, ns + MS, build.cdr(fix, "sensor_msgs/msg/NavSatFix")))
        sonar = t["sensor_msgs/msg/Range"](
            header=build.header(ns + 2 * MS, "sonar"),
            radiation_type=0,
            field_of_view=0.1,
            min_range=0.5,
            max_range=100.0,
            range=12.5 - step,
        )
        data = build.cdr(sonar, "sensor_msgs/msg/Range")
        if step == 2:
            data = data[:20]  # the payload is cut short
        messages.append(Message(4, ns + 2 * MS, data))
        dvl = t["geometry_msgs/msg/TwistWithCovarianceStamped"](
            header=build.header(ns + 3 * MS, "dvl_link"),
            twist=t["geometry_msgs/msg/TwistWithCovariance"](
                twist=t["geometry_msgs/msg/Twist"](
                    linear=build.vector(1.5, 0.2, 0.0), angular=build.vector(0.0, 0.0, 0.01)
                ),
                covariance=build.covariance(36, 0.01),
            ),
        )
        messages.append(
            Message(5, ns + 3 * MS, build.cdr(dvl, "geometry_msgs/msg/TwistWithCovarianceStamped"))
        )
        frame = "/base_link" if step % 2 else "base_link"
        imu = build.imu(ns + 4 * MS, frame, 9.80)
        messages.append(Message(6, ns + 4 * MS, build.cdr(imu, "sensor_msgs/msg/Imu")))
    schemas = {c.type: msgdef(build, c.type) for c in channels}
    return write_mcap("ros2msg", schemas, channels, messages)


RECORDINGS: Final[dict[str, Callable[[Build], bytes]]] = {
    "arm_cell.mcap": arm_cell,
    "quadruped_walk.mcap": quadruped_walk,
    "usv_survey.mcap": usv_survey,
}


# --- The oracle ---------------------------------------------------------------------------------

_BYTE_ARRAYS: Final = {"uint8", "byte", "char", "octet"}


def flatten(ts: Any, value: Any, typename: str, ros1: bool) -> dict[str, Any]:
    """A message as the decoder's columns: path to value, a list under one array level, paths
    under two array levels and byte arrays left out, ROS 1 time as ``.secs`` and ``.nsecs``."""
    out: dict[str, Any] = {}

    def put(path: str, item: Any, depth: int) -> None:
        if depth > 1:
            return
        if depth == 1:
            out.setdefault(path, []).append(item)
        else:
            out[path] = item

    def walk(obj: Any, name: str, prefix: str, depth: int) -> None:
        if ros1 and name in ("builtin_interfaces/msg/Time", "builtin_interfaces/msg/Duration"):
            put(prefix + "secs", int(obj.sec), depth)
            put(prefix + "nsecs", int(obj.nanosec), depth)
            return
        _, fields = ts.fielddefs[name]
        for field, (kind, detail) in fields:
            path = prefix + field
            item = getattr(obj, field)
            if kind == 1:
                put(path, _scalar(item), depth)
            elif kind == 2:
                walk(item, detail, path + ".", depth)
            else:
                element, _ = detail
                if element[0] == 1 and _primitive(element[1]) in _BYTE_ARRAYS:
                    continue
                for entry in list(item):
                    if element[0] == 1:
                        put(path + "[]", _scalar(entry), depth + 1)
                    else:
                        walk(entry, element[1], path + "[].", depth + 1)
                if depth == 0 and path + "[]" not in out and element[0] == 1:
                    out[path + "[]"] = []
                if depth == 0 and element[0] == 2:
                    _empty(ts, element[1], path + "[].", out, ros1)

    walk(value, typename, "", 0)
    return out


def _empty(ts: Any, name: str, prefix: str, out: dict[str, Any], ros1: bool) -> None:
    """Every column under an array of messages, empty where the array holds nothing."""
    if ros1 and name in ("builtin_interfaces/msg/Time", "builtin_interfaces/msg/Duration"):
        out.setdefault(prefix[:-1] + ".secs", [])
        out.setdefault(prefix[:-1] + ".nsecs", [])
        return
    _, fields = ts.fielddefs[name]
    for field, (kind, detail) in fields:
        if kind == 1:
            out.setdefault(prefix + field, [])
        elif kind == 2:
            _empty(ts, detail, prefix + field + ".", out, ros1)


def _primitive(detail: Any) -> str:
    return str(detail[0] if isinstance(detail, tuple) else detail)


def _scalar(item: Any) -> Any:
    if hasattr(item, "item"):
        item = item.item()
    return item


def _recording_messages(path: Path, ts: Any) -> Iterator[tuple[str, str, bytes]]:
    """(topic, type, payload) of an MCAP file this script wrote, in file order."""
    data = path.read_bytes()
    pos = len(MAGIC)
    schemas: dict[int, tuple[str, str, str]] = {}
    channels: dict[int, tuple[str, str]] = {}
    while pos < len(data) - len(MAGIC):
        opcode, length = struct.unpack_from("<BQ", data, pos)
        content = data[pos + 9 : pos + 9 + length]
        pos += 9 + length
        if opcode == 0x03:
            (schema_id,) = struct.unpack_from("<H", content)
            at = 2
            fields = []
            for _ in range(3):
                (size,) = struct.unpack_from("<I", content, at)
                fields.append(content[at + 4 : at + 4 + size].decode())
                at += 4 + size
            schemas[schema_id] = (fields[0], fields[1], fields[2])
        elif opcode == 0x04:
            channel_id, schema_id = struct.unpack_from("<HH", content)
            (size,) = struct.unpack_from("<I", content, 4)
            topic = content[8 : 8 + size].decode()
            channels[channel_id] = (topic, schemas[schema_id][0])
        elif opcode == 0x05:
            (channel_id,) = struct.unpack_from("<H", content)
            topic, kind = channels[channel_id]
            yield topic, kind, content[22:]


def oracle() -> dict[str, Any]:
    from rosbags.highlevel import AnyReader  # type: ignore[import-not-found]
    from rosbags.typesys import Stores, get_typestore

    found: dict[str, Any] = {}
    store = get_typestore(Stores.ROS2_HUMBLE)
    _check_idl(HERE / "quadruped_walk.mcap")
    for name in RECORDINGS:
        topics: dict[str, list[Any]] = {}
        for topic, kind, payload in _recording_messages(HERE / name, store):
            try:
                message = store.deserialize_cdr(payload, kind)
            except Exception:
                topics.setdefault(topic, []).append(None)
                continue
            topics.setdefault(topic, []).append(flatten(store, message, kind, ros1=False))
        found[name] = topics
    for label, path, ros1 in (
        ("rosbag1/robot_none.bag", ROOT / "tests/fixtures/rosbag1/robot_none.bag", True),
        ("rosbag2/mobile_base_sqlite3", ROOT / "tests/fixtures/rosbag2/mobile_base_sqlite3", False),
    ):
        topics = {}
        with AnyReader([path], default_typestore=store) as reader:
            for connection, _, raw in reader.messages():
                message = reader.deserialize(raw, connection.msgtype)
                topics.setdefault(connection.topic, []).append(
                    flatten(reader.typestore, message, connection.msgtype, ros1=ros1)
                )
        found[label] = topics
    imu = []
    for topic, kind, payload in _mcap_messages(ROOT / "tests/fixtures/mcap/robot_plain.mcap"):
        if topic == "/imu":
            imu.append(flatten(store, store.deserialize_cdr(payload, kind), kind, ros1=False))
    found["mcap/robot.mcap"] = {"/imu": imu}
    return found


def _check_idl(path: Path) -> None:
    """The quadruped's IDL, read by rosbags' own IDL parser, describes its payloads: a store of
    only the types it declares decodes every message as the full store does."""
    from rosbags.typesys import Stores, get_types_from_idl, get_typestore

    full = get_typestore(Stores.ROS2_HUMBLE)
    data = path.read_bytes()
    for topic, kind, payload in _recording_messages(path, full):
        text = _schema_text(data, kind)
        own = get_typestore(Stores.EMPTY)
        for section in text.split("=" * 80 + "\n"):
            body = section.split("\n", 1)[1] if section.startswith("IDL:") else section
            own.register(get_types_from_idl(body))
        assert flatten(own, own.deserialize_cdr(payload, kind), kind, False) == flatten(
            full, full.deserialize_cdr(payload, kind), kind, False
        ), topic


def _schema_text(data: bytes, kind: str) -> str:
    pos = len(MAGIC)
    while pos < len(data) - len(MAGIC):
        opcode, length = struct.unpack_from("<BQ", data, pos)
        content = data[pos + 9 : pos + 9 + length]
        pos += 9 + length
        if opcode == 0x03:
            at, fields = 2, []
            for _ in range(3):
                (size,) = struct.unpack_from("<I", content, at)
                fields.append(content[at + 4 : at + 4 + size].decode())
                at += 4 + size
            if fields[0] == kind:
                return fields[2]
    raise KeyError(kind)


def _mcap_messages(path: Path) -> Iterator[tuple[str, str, bytes]]:
    """(topic, type, payload) of the MCAP fixture's ``ros2msg`` channels, by log time then
    sequence (the order of their series rows), read with the ``mcap`` package's reader."""
    from mcap.reader import make_reader  # type: ignore[import-not-found]

    with path.open("rb") as stream:
        reader = make_reader(stream)
        messages = [
            (message.log_time, message.sequence, channel.topic, schema.name, bytes(message.data))
            for schema, channel, message in reader.iter_messages()
            if schema is not None and schema.encoding == "ros2msg"
        ]
    for _, _, topic, kind, data in sorted(messages, key=lambda m: (m[2], m[0], m[1])):
        yield topic, kind, data


def main() -> None:
    if "--oracle" in sys.argv:
        text = json.dumps(oracle(), indent=1, sort_keys=True)
        (HERE / "oracle.json").write_text(text + "\n")
        return
    build = Build()
    for name, make in RECORDINGS.items():
        (HERE / name).write_bytes(make(build))


if __name__ == "__main__":
    main()

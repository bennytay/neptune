"""Generate the ROS 1 bag adapter's fixtures: one recording, written several ways, damaged too.

Run ``uv run python tests/fixtures/rosbag1/make_rosbag1.py`` to rewrite every fixture, then
``uv run python tests/fixtures/rosbag1/make_rosbag1.py --oracle`` to read each one with the
official ``rosbags`` reader (fetched by ``uv`` for that run only, never a dependency) and record
what it reads in ``oracle.json``. Output is deterministic for the pinned ``lz4`` and the
interpreter's ``bz2``; ``tests/unit/adapters/test_rosbag1_fixtures.py`` checks that every committed
file is what ``build()`` gives and that ``oracle.json`` agrees with the adapter.

The recording is a mobile manipulator (not a flight log): an arm's joint states, the base's
odometry and the transform tree, 17 messages with real ROS 1 serialisation:

- connections 0 ``/joint_states`` (sensor_msgs/JointState, six arm joints), 1 ``/odom``
  (nav_msgs/Odometry), 2 ``/tf`` (tf2_msgs/TFMessage) and 3 ``/tf_static`` (latched, one message);
  the message definitions are the real ones, their md5sums the ones ROS computes;
- three chunks of 6, 6 and 5 messages, each followed by its Index Data records; Connection records
  inside the chunks before a connection's first message there, repeated after the chunks with one
  Chunk Info per chunk, and the Bag Header pointing at them.

``same_recording.mcap`` is the same recording as an MCAP file (the MCAP fixtures' writer,
``tests/fixtures/mcap/make_mcap.py``, profile ``ros1``, schemas ``ros1msg``, messages ``ros1``):
the acceptance test ingests both and compares what they say.

Files (see ``README.md``): ``robot_none.bag``, ``robot_bz2.bag``, ``robot_lz4.bag``,
``unclosed.bag``, ``empty.bag``, ``same_recording.mcap``, and the damaged ``truncated.bag``,
``lying_chunk_info.bag``, ``unknown_compression.bag`` and ``connection_collision.bag``.
"""

import bz2
import importlib.util
import struct
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Final

import lz4.frame

HERE: Final = Path(__file__).parent
MAGIC: Final = b"#ROSBAG V2.0\n"
MS: Final = 10**6
T0: Final = 1_790_000_000 * 10**9  # the recording's start, ns on the bag's clock

HEADER_DEFINITION: Final = """uint32 seq
time stamp
string frame_id
"""
VECTOR3: Final = """float64 x
float64 y
float64 z
"""
QUATERNION: Final = """float64 x
float64 y
float64 z
float64 w
"""
SEPARATOR: Final = "=" * 80


def _joined(root: str, *dependencies: tuple[str, str]) -> str:
    out = root
    for name, text in dependencies:
        out += f"{SEPARATOR}\nMSG: {name}\n{text}"
    return out


JOINT_STATE: Final = _joined(
    "Header header\nstring[] name\nfloat64[] position\nfloat64[] velocity\nfloat64[] effort\n",
    ("std_msgs/Header", HEADER_DEFINITION),
)
ODOMETRY: Final = _joined(
    "Header header\nstring child_frame_id\ngeometry_msgs/PoseWithCovariance pose\n"
    "geometry_msgs/TwistWithCovariance twist\n",
    ("std_msgs/Header", HEADER_DEFINITION),
    ("geometry_msgs/PoseWithCovariance", "Pose pose\nfloat64[36] covariance\n"),
    ("geometry_msgs/Pose", "Point position\nQuaternion orientation\n"),
    ("geometry_msgs/Point", VECTOR3),
    ("geometry_msgs/Quaternion", QUATERNION),
    ("geometry_msgs/TwistWithCovariance", "Twist twist\nfloat64[36] covariance\n"),
    ("geometry_msgs/Twist", "Vector3 linear\nVector3 angular\n"),
    ("geometry_msgs/Vector3", VECTOR3),
)
TF_MESSAGE: Final = _joined(
    "geometry_msgs/TransformStamped[] transforms\n",
    (
        "geometry_msgs/TransformStamped",
        "Header header\nstring child_frame_id\nTransform transform\n",
    ),
    ("std_msgs/Header", HEADER_DEFINITION),
    ("geometry_msgs/Transform", "Vector3 translation\nQuaternion rotation\n"),
    ("geometry_msgs/Vector3", VECTOR3),
    ("geometry_msgs/Quaternion", QUATERNION),
)
JOINTS: Final = (
    "shoulder_pan_joint",
    "shoulder_lift_joint",
    "elbow_joint",
    "wrist_1_joint",
    "wrist_2_joint",
    "wrist_3_joint",
)


# --- Serialisation (ROS 1: little-endian, packed, uint32 lengths) -------------------------------


def _string(text: str) -> bytes:
    data = text.encode()
    return struct.pack("<I", len(data)) + data


def _stamp(ns: int) -> bytes:
    sec, nsec = divmod(ns, 10**9)
    return struct.pack("<II", sec, nsec)


def _header(seq: int, stamp: int, frame: str) -> bytes:
    return struct.pack("<I", seq) + _stamp(stamp) + _string(frame)


def _floats(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}d", *values)


def _joint_state(seq: int, stamp: int, step: int) -> bytes:
    position = [0.1 * (i + 1) + 0.01 * step for i in range(6)]
    velocity = [0.01 * (i + 1) for i in range(6)]
    effort = [0.5 * (i + 1) for i in range(6)]
    out = _header(seq, stamp, "base_link") + struct.pack("<I", 6)
    out += b"".join(_string(name) for name in JOINTS)
    for series in (position, velocity, effort):
        out += struct.pack("<I", 6) + _floats(series)
    return out


def _odom(seq: int, stamp: int, step: int) -> bytes:
    x, yaw = 0.05 * step, 0.01 * step
    out = _header(seq, stamp, "odom") + _string("base_link")
    out += _floats([x, 0.0, 0.0, 0.0, 0.0, yaw, 1.0])  # position then orientation
    out += _floats([0.01] + [0.0] * 35)  # pose covariance
    out += _floats([0.2, 0.0, 0.0, 0.0, 0.0, 0.05])  # linear then angular
    out += _floats([0.02] + [0.0] * 35)  # twist covariance
    return out


def _transform(seq: int, stamp: int, parent: str, child: str, x: float) -> bytes:
    return _header(seq, stamp, parent) + _string(child) + _floats([x, 0.0, 0.1, 0, 0, 0, 1.0])


def _tf(seq: int, stamp: int, step: int) -> bytes:
    transforms = [
        _transform(seq, stamp, "odom", "base_link", 0.05 * step),
        _transform(seq, stamp, "base_link", "arm_base", 0.0),
    ]
    return struct.pack("<I", len(transforms)) + b"".join(transforms)


def _tf_static(seq: int, stamp: int) -> bytes:
    return struct.pack("<I", 1) + _transform(seq, stamp, "base_link", "lidar", 0.3)


# --- The recording ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Connection:
    id: int
    topic: str
    type: str
    md5sum: str
    definition: str
    callerid: str
    latching: str


CONNECTIONS: Final = (
    Connection(
        0,
        "/joint_states",
        "sensor_msgs/JointState",
        "3066dcd76a6cfaef579bd0f34173e9fd",
        JOINT_STATE,
        "/arm_driver",
        "0",
    ),
    Connection(
        1,
        "/odom",
        "nav_msgs/Odometry",
        "cd5e73d190d741a2f92e81eda573aca7",
        ODOMETRY,
        "/base_driver",
        "0",
    ),
    Connection(
        2,
        "/tf",
        "tf2_msgs/TFMessage",
        "94810edda583a504dfda3829e70d7eec",
        TF_MESSAGE,
        "/robot_state_publisher",
        "0",
    ),
    Connection(
        3,
        "/tf_static",
        "tf2_msgs/TFMessage",
        "94810edda583a504dfda3829e70d7eec",
        TF_MESSAGE,
        "/robot_state_publisher",
        "1",
    ),
)


@dataclass(frozen=True)
class Message:
    conn: int
    time: int  # ns on the bag's clock
    data: bytes


def _messages() -> tuple[Message, ...]:
    out: list[Message] = []
    seqs = [0, 0, 0, 0]

    def add(conn: int, ms: int, step: int) -> None:
        time = T0 + ms * MS
        seq = seqs[conn]
        seqs[conn] += 1
        makers = (
            lambda: _joint_state(seq, time, step),
            lambda: _odom(seq, time, step),
            lambda: _tf(seq, time, step),
            lambda: _tf_static(seq, time),
        )
        out.append(Message(conn, time, makers[conn]()))

    add(3, 5, 0)
    for step in range(5):
        base = 10 + 100 * step
        add(0, base, step)
        add(1, base + 20, step)
        add(2, base + 30, step)
    add(0, 520, 5)
    return tuple(out)


MESSAGES: Final = _messages()
CHUNKS: Final = ((0, 6), (6, 12), (12, 17))


# --- Encoding -----------------------------------------------------------------------------------


def _fields(fields: list[tuple[str, bytes]]) -> bytes:
    return b"".join(
        struct.pack("<I", len(name) + 1 + len(value)) + name.encode() + b"=" + value
        for name, value in fields
    )


def _record(fields: list[tuple[str, bytes]], data: bytes) -> bytes:
    header = _fields(fields)
    return struct.pack("<I", len(header)) + header + struct.pack("<I", len(data)) + data


def _time(ns: int) -> bytes:
    return _stamp(ns)


def connection_record(c: Connection, record_topic: str | None = None) -> bytes:
    header = _fields(
        [
            ("topic", c.topic.encode()),
            ("type", c.type.encode()),
            ("md5sum", c.md5sum.encode()),
            ("message_definition", c.definition.encode()),
            ("callerid", c.callerid.encode()),
            ("latching", c.latching.encode()),
        ]
    )
    topic = (record_topic or c.topic).encode()
    return _record([("op", b"\x07"), ("conn", struct.pack("<I", c.id)), ("topic", topic)], header)


def message_record(m: Message) -> bytes:
    fields = [("op", b"\x02"), ("conn", struct.pack("<I", m.conn)), ("time", _time(m.time))]
    return _record(fields, m.data)


def _compress(compression: str, data: bytes) -> bytes:
    if compression == "bz2":
        return bz2.compress(data, 9)
    if compression == "lz4":
        return bytes(lz4.frame.compress(data, compression_level=0, content_checksum=False))
    return data


@dataclass(frozen=True)
class Options:
    """How the recording is written; the damage is applied afterwards."""

    compression: str = "bz2"
    closed: bool = True  # the Bag Header points at the Connection and Chunk Info records
    chunk_infos: bool = True
    connections: tuple[Connection, ...] = CONNECTIONS
    messages: tuple[Message, ...] = MESSAGES
    chunks: tuple[tuple[int, int], ...] = CHUNKS
    renamed: tuple[tuple[int, str], ...] = ()  # chunks that declare a compression they don't use
    count_changes: tuple[tuple[int, int, int], ...] = ()  # (chunk, connection, change) in infos
    impostor: tuple[int, int, str] | None = None  # (chunk, connection, topic) inside a chunk


def write(options: Options) -> tuple[bytes, dict[str, tuple[int, int]]]:
    """The bag's bytes, and where each labelled record is."""
    out = bytearray(MAGIC)
    at: dict[str, tuple[int, int]] = {}

    def add(label: str, record: bytes) -> int:
        offset = len(out)
        out.extend(record)
        at[label] = (offset, len(record))
        return offset

    out.extend(bytes(4096))  # the Bag Header, written last
    by_id = {c.id: c for c in options.connections}
    infos: list[tuple[int, int, int, dict[int, int]]] = []
    for number, (first, last) in enumerate(options.chunks):
        records = bytearray()
        entries: dict[int, list[tuple[int, int]]] = {}
        seen: set[int] = set()
        span = [m.time for m in options.messages[first:last]]
        for message in options.messages[first:last]:
            if message.conn not in seen:
                seen.add(message.conn)
                topic = None
                if options.impostor and options.impostor[:2] == (number, message.conn):
                    topic = options.impostor[2]
                declaration = connection_record(by_id[message.conn], topic)
                at[f"chunk_connection:{number}:{message.conn}"] = (len(records), len(declaration))
                records += declaration
            entries.setdefault(message.conn, []).append((message.time, len(records)))
            records += message_record(message)
        stored = _compress(options.compression, bytes(records))
        name = dict(options.renamed).get(number, options.compression)
        chunk_fields = [
            ("op", b"\x05"),
            ("compression", name.encode()),
            ("size", struct.pack("<I", len(records))),
        ]
        chunk_pos = add(f"chunk:{number}", _record(chunk_fields, stored))
        for conn in sorted(entries):
            index_fields = [
                ("op", b"\x04"),
                ("ver", struct.pack("<I", 1)),
                ("conn", struct.pack("<I", conn)),
                ("count", struct.pack("<I", len(entries[conn]))),
            ]
            body = b"".join(_time(t) + struct.pack("<I", o) for t, o in entries[conn])
            add(f"index_data:{number}:{conn}", _record(index_fields, body))
        counts = {conn: len(found) for conn, found in sorted(entries.items())}
        infos.append((chunk_pos, min(span), max(span), counts))
    index_pos = len(out)
    if options.closed:
        for connection in options.connections:
            add(f"connection:{connection.id}", connection_record(connection))
        if options.chunk_infos:
            for number, (chunk_pos, low, high, counts) in enumerate(infos):
                counts = dict(counts)
                for chunk, conn, change in options.count_changes:
                    if chunk == number and conn in counts:
                        counts[conn] += change
                info_fields = [
                    ("op", b"\x06"),
                    ("ver", struct.pack("<I", 1)),
                    ("chunk_pos", struct.pack("<Q", chunk_pos)),
                    ("start_time", _time(low)),
                    ("end_time", _time(high)),
                    ("count", struct.pack("<I", len(counts))),
                ]
                body = b"".join(struct.pack("<II", c, n) for c, n in sorted(counts.items()))
                add(f"chunk_info:{number}", _record(info_fields, body))
    header = _fields(
        [
            ("op", b"\x03"),
            ("index_pos", struct.pack("<Q", index_pos if options.closed else 0)),
            ("conn_count", struct.pack("<I", len(options.connections))),
            ("chunk_count", struct.pack("<I", len(infos))),
        ]
    )
    padding = 4096 - 8 - len(header)  # the whole Bag Header record is 4096 bytes
    record = struct.pack("<I", len(header)) + header + struct.pack("<I", padding) + b" " * padding
    out[len(MAGIC) : len(MAGIC) + 4096] = record
    at["bag_header"] = (len(MAGIC), 4096)
    return bytes(out), at


# --- Damage -------------------------------------------------------------------------------------


def truncated() -> bytes:
    data, at = write(Options(compression="none"))
    offset, length = at["chunk:2"]
    return data[: offset + length // 2]


def empty() -> bytes:
    data, _ = write(Options(connections=(), messages=(), chunks=()))
    return data


def _mcap() -> ModuleType:
    path = HERE.parent / "mcap" / "make_mcap.py"
    spec = importlib.util.spec_from_file_location("make_mcap", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_mcap"] = module
    spec.loader.exec_module(module)
    return module


def same_recording() -> bytes:
    """The recording as MCAP: ``ros1msg`` schemas, ``ros1`` messages, the bag's time as both
    clocks, the bag's connection header fields as channel metadata (what ``mcap convert`` makes).
    """
    mcap = _mcap()
    schemas, channels = [], []
    schema_of: dict[str, int] = {}
    for c in CONNECTIONS:
        if c.type not in schema_of:
            schema_of[c.type] = len(schema_of) + 1
            schemas.append(mcap.Schema(schema_of[c.type], c.type, "ros1msg", c.definition.encode()))
        metadata = {"callerid": c.callerid, "latching": c.latching, "md5sum": c.md5sum}
        channels.append(mcap.Channel(c.id + 1, schema_of[c.type], c.topic, "ros1", metadata))
    sequence = [0] * len(CONNECTIONS)
    messages = []
    for m in MESSAGES:
        messages.append(mcap.Message(m.conn + 1, sequence[m.conn], m.time, m.time, m.data))
        sequence[m.conn] += 1
    options = mcap.Options(
        compression="",
        schemas=tuple(schemas),
        channels=tuple(channels),
        messages=tuple(messages),
        chunks=CHUNKS,
        attachment=False,
        metadata=False,
        profile="ros1",
    )
    return bytes(mcap.write(options)[0])


FILES: Final[dict[str, Callable[[], bytes]]] = {
    "robot_none.bag": lambda: write(Options(compression="none"))[0],
    "robot_bz2.bag": lambda: write(Options(compression="bz2"))[0],
    "robot_lz4.bag": lambda: write(Options(compression="lz4"))[0],
    "unclosed.bag": lambda: write(Options(closed=False))[0],
    "empty.bag": empty,
    "same_recording.mcap": same_recording,
    "truncated.bag": truncated,
    "lying_chunk_info.bag": lambda: write(Options(count_changes=((0, 0, -1),)))[0],
    "unknown_compression.bag": lambda: write(Options(compression="none", renamed=((1, "zstd"),)))[
        0
    ],
    "connection_collision.bag": lambda: write(Options(impostor=(1, 0, "/impostor")))[0],
}


def build() -> dict[str, bytes]:
    """Every fixture's bytes, by file name."""
    return {name: make() for name, make in FILES.items()}


# --- The official reader as an oracle -----------------------------------------------------------

_ORACLE: Final = r"""
import hashlib, json, sys
from pathlib import Path
from rosbags.rosbag1 import Reader
from rosbags.typesys import Stores, get_typestore, get_types_from_msg

store = get_typestore(Stores.ROS1_NOETIC)

def read(path):
    out = {"error": None, "connections": [], "messages": [], "start": None, "end": None}
    try:
        with Reader(path) as reader:
            out["start"], out["end"] = reader.start_time, reader.end_time
            for c in reader.connections:
                if c.msgtype not in store.types:
                    store.register(get_types_from_msg(c.msgdef.data, c.msgtype))
                ext = c.ext
                out["connections"].append({
                    "id": c.id, "topic": c.topic, "type": c.msgtype, "md5sum": c.digest,
                    "count": c.msgcount, "callerid": ext.callerid, "latching": ext.latching,
                })
            for connection, timestamp, raw in reader.messages():
                store.deserialize_ros1(raw, connection.msgtype)  # raises unless it is whole
                out["messages"].append([connection.topic, timestamp, len(raw),
                                        hashlib.sha256(raw).hexdigest()])
    except Exception as exc:
        out["error"] = type(exc).__name__
    return out

out = {}
for path in sorted(Path(sys.argv[1]).glob("*.bag")):
    out[path.name] = read(path)
print(json.dumps(out, indent=1, sort_keys=True))
"""


def oracle() -> str:
    """What the official ``rosbags`` reader reads from every committed bag."""
    command = ["uv", "run", "--no-project", "--with", "rosbags", "python", "-c", _ORACLE]
    result = subprocess.run(
        [*command, str(HERE)], check=True, capture_output=True, text=True, cwd="/tmp"
    )
    return result.stdout


if __name__ == "__main__":
    if sys.argv[1:] == ["--oracle"]:
        (HERE / "oracle.json").write_text(oracle())
    else:
        for name, data in build().items():
            (HERE / name).write_bytes(data)

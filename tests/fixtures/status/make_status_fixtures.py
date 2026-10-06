"""Generate the status and safety-state fixtures (ADR 0071): five small logs, one per embodiment,
each holding messages whose declared types are statuses or safety states.

Run ``uv run python tests/fixtures/status/make_status_fixtures.py`` to rewrite every fixture, then
``uv run python tests/fixtures/status/make_status_fixtures.py --oracle`` to read each one with the
official readers (``mcap`` and ``rosbags`` for the ROS logs, ``pyulog``, ``pymavlink``; fetched by
``uv`` for that run only, never dependencies) and record what they read in ``oracle.json``.
``tests/unit/adapters/test_status_fixtures.py`` checks that every committed file is what
``build()`` gives and that ``oracle.json`` agrees with the records the adapters write.

- ``arm_cell.mcap`` (an industrial arm): ``/diagnostics`` (``diagnostic_msgs/msg/DiagnosticArray``,
  ros2msg), ``/robot_status`` (``industrial_msgs/msg/RobotStatus``: the controller's e-stop and
  error flags), ``/ur/safety_mode`` (``ur_dashboard_msgs/msg/SafetyMode``) and ``/gripper/status``
  (a lone ``diagnostic_msgs/msg/DiagnosticStatus`` declared in ros2idl). The arm collides, its
  controller faults, the safety controller goes to a protective stop and the operator presses
  the e-stop.
- ``mobile_base.bag`` (a ROS 1 mobile base): ``/diagnostics_agg``
  (``diagnostic_msgs/DiagnosticArray``, ROS 1's Header) and ``/status``
  (``husky_msgs/HuskyStatus``, its ``e_stop`` flag).
- ``av_shuttle.db3`` (a ROS 2 autonomous shuttle, rosbag2 sqlite3 storage): ``/diagnostics`` and
  ``/system/emergency/emergency_state`` (``autoware_auto_system_msgs/msg/EmergencyState``).
- ``quad_killswitch.ulg`` (a PX4 multicopter): logged messages, untagged and tagged, and
  ``actuator_armed`` with the kill switch (``manual_lockdown``) engaged for a while.
- ``boat_failsafe.bin`` (an ArduRover boat): ``MSG`` texts and ``ERR`` subsystem/error codes.

Every name, number and serial is invented; times are each format's own (ns on the recorder's
clock for ROS, µs since boot for PX4 and ArduPilot).
"""

import importlib.util
import json
import sqlite3
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Final

HERE: Final = Path(__file__).parent
FIXTURES: Final = HERE.parent
SECOND: Final = 10**9
MS: Final = 10**6
T0: Final = 1_790_000_000 * SECOND  # the ROS recordings' start, ns on the recorder's clock
SEPARATOR: Final = "=" * 80


def _load(name: str, path: Path) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


# The other fixtures' writers, imported, never copied.
MCAP: Final = _load("status_mcap_writer", FIXTURES / "mcap" / "make_mcap.py")
BAG1: Final = _load("status_rosbag1_writer", FIXTURES / "rosbag1" / "make_rosbag1.py")
ULOG: Final = _load("status_ulog_writer", FIXTURES / "ulog" / "make_ulog_fixtures.py")
DF: Final = _load("status_dataflash_writer", FIXTURES / "ardupilot" / "make_dataflash_fixtures.py")


def joined(root: str, *dependencies: tuple[str, str]) -> str:
    out = root
    for name, text in dependencies:
        out += f"{SEPARATOR}\nMSG: {name}\n{text}"
    return out


# --- Definitions, as the publishers declare them -------------------------------------------------

HEADER2: Final = (
    ("std_msgs/Header", "builtin_interfaces/Time stamp\nstring frame_id\n"),
    ("builtin_interfaces/Time", "int32 sec\nuint32 nanosec\n"),
)
STATUS_TEXT: Final = (
    "byte OK=0\nbyte WARN=1\nbyte ERROR=2\nbyte STALE=3\n\nbyte level\nstring name\n"
    "string message\nstring hardware_id\nKeyValue[] values\n"
)
KEY_VALUE: Final = ("diagnostic_msgs/KeyValue", "string key\nstring value\n")
DIAGNOSTIC_ARRAY2: Final = joined(
    "std_msgs/Header header\nDiagnosticStatus[] status\n",
    ("diagnostic_msgs/DiagnosticStatus", STATUS_TEXT),
    KEY_VALUE,
    *HEADER2,
)
DIAGNOSTIC_ARRAY1: Final = joined(
    "Header header\nDiagnosticStatus[] status\n",
    ("diagnostic_msgs/DiagnosticStatus", STATUS_TEXT),
    KEY_VALUE,
    ("std_msgs/Header", "uint32 seq\ntime stamp\nstring frame_id\n"),
)
TRI_STATE: Final = (
    "int8 val\n\nint8 UNKNOWN=-1\nint8 TRUE=1\nint8 ON=1\nint8 ENABLED=1\nint8 HIGH=1\n"
    "int8 CLOSED=1\nint8 FALSE=0\nint8 OFF=0\nint8 DISABLED=0\nint8 LOW=0\nint8 OPEN=0\n"
)
ROBOT_STATUS: Final = joined(
    "std_msgs/Header header\nindustrial_msgs/RobotMode mode\nindustrial_msgs/TriState e_stopped\n"
    "industrial_msgs/TriState drives_powered\nindustrial_msgs/TriState motion_possible\n"
    "industrial_msgs/TriState in_motion\nindustrial_msgs/TriState in_error\nint32 error_code\n",
    ("industrial_msgs/RobotMode", "int8 val\n\nint8 UNKNOWN=-1\nint8 MANUAL=1\nint8 AUTO=2\n"),
    ("industrial_msgs/TriState", TRI_STATE),
    *HEADER2,
)
SAFETY_MODE: Final = (
    "uint8 NORMAL=1\nuint8 REDUCED=2\nuint8 PROTECTIVE_STOP=3\nuint8 RECOVERY=4\n"
    "uint8 SAFEGUARD_STOP=5\nuint8 SYSTEM_EMERGENCY_STOP=6\nuint8 ROBOT_EMERGENCY_STOP=7\n"
    "uint8 VIOLATION=8\nuint8 FAULT=9\nuint8 VALIDATE_JOINT_ID=10\nuint8 UNDEFINED_SAFETY_MODE=11\n"
    "uint8 mode\n"
)
# rosidl's IDL for a lone DiagnosticStatus, as MCAP's ros2idl schemas carry it: the constants in
# the type's _Constants module.
GRIPPER_STATUS_IDL: Final = f"""{SEPARATOR}
IDL: diagnostic_msgs/msg/DiagnosticStatus
module diagnostic_msgs {{
  module msg {{
    module DiagnosticStatus_Constants {{
      const octet OK = 0;
      const octet WARN = 1;
      const octet ERROR = 2;
      const octet STALE = 3;
    }};
    struct DiagnosticStatus {{
      octet level;
      string name;
      string message;
      string hardware_id;
      sequence<diagnostic_msgs::msg::KeyValue> values;
    }};
  }};
}};
{SEPARATOR}
IDL: diagnostic_msgs/msg/KeyValue
module diagnostic_msgs {{
  module msg {{
    struct KeyValue {{
      string key;
      string value;
    }};
  }};
}};
"""
HUSKY_STATUS: Final = joined(
    "Header header\nuint64 uptime\nfloat64 ros_control_loop_freq\n"
    "float64 mcu_and_user_port_current\nfloat64 left_driver_current\n"
    "float64 right_driver_current\nfloat64 battery_voltage\nfloat64 left_driver_voltage\n"
    "float64 right_driver_voltage\nfloat64 left_driver_temp\nfloat64 right_driver_temp\n"
    "float64 left_motor_temp\nfloat64 right_motor_temp\nuint16 capacity_estimate\n"
    "float64 charge_estimate\nbool timeout\nbool lockout\nbool e_stop\nbool ros_pause\n"
    "bool no_battery\nbool current_limit\n",
    ("std_msgs/Header", "uint32 seq\ntime stamp\nstring frame_id\n"),
)
EMERGENCY_STATE: Final = joined(
    "builtin_interfaces/Time stamp\n\nuint8 NORMAL = 1\nuint8 OVERRIDE_REQUESTING = 2\n"
    "uint8 MRM_OPERATING = 3\nuint8 MRM_SUCCEEDED = 4\nuint8 MRM_FAILED = 5\n\nuint8 state\n",
    ("builtin_interfaces/Time", "int32 sec\nuint32 nanosec\n"),
)
OK, WARN, ERROR, STALE = 0, 1, 2, 3

Status = tuple[int, str, str, str, tuple[tuple[str, str], ...]]


# --- Serialisation -------------------------------------------------------------------------------


class Cdr:
    """ROS 2's CDR, little-endian, alignment counted after the 4-byte encapsulation header."""

    def __init__(self) -> None:
        self.out = bytearray()

    def align(self, size: int) -> None:
        self.out += bytes(-len(self.out) % size)

    def put(self, code: str, value: object) -> None:
        size = struct.calcsize("<" + code)
        self.align(size)
        self.out += struct.pack("<" + code, value)

    def string(self, text: str | bytes) -> None:
        raw = (text.encode() if isinstance(text, str) else text) + b"\0"
        self.put("I", len(raw))
        self.out += raw

    def stamp(self, ns: int) -> None:
        seconds, nanoseconds = divmod(ns, SECOND)
        self.put("i", seconds)
        self.put("I", nanoseconds)

    def bytes(self) -> bytes:
        return b"\x00\x01\x00\x00" + bytes(self.out)


def cdr_status(cdr: Cdr, status: Status) -> None:
    level, name, message, hardware, values = status
    cdr.put("B", level)
    cdr.string(name)
    cdr.string(message)
    cdr.string(hardware)
    cdr.put("I", len(values))
    for key, value in values:
        cdr.string(key)
        cdr.string(value)


def cdr_array(stamp: int, frame: str, statuses: tuple[Status, ...]) -> bytes:
    cdr = Cdr()
    cdr.stamp(stamp)
    cdr.string(frame)
    cdr.put("I", len(statuses))
    for status in statuses:
        cdr_status(cdr, status)
    return cdr.bytes()


def cdr_lone_status(status: Status) -> bytes:
    cdr = Cdr()
    cdr_status(cdr, status)
    return cdr.bytes()


def cdr_robot_status(stamp: int, e_stopped: int, in_error: int, error_code: int) -> bytes:
    cdr = Cdr()
    cdr.stamp(stamp)
    cdr.string("base_link")
    for value in (2, e_stopped, 1, 1 - max(e_stopped, in_error), 0, in_error):
        cdr.put("b", value)  # mode AUTO, e_stopped, drives_powered, motion_possible, ...
    cdr.put("i", error_code)
    return cdr.bytes()


def cdr_byte(code: str, value: int) -> bytes:
    cdr = Cdr()
    cdr.put(code, value)
    return cdr.bytes()


def cdr_emergency(stamp: int, state: int) -> bytes:
    cdr = Cdr()
    cdr.stamp(stamp)
    cdr.put("B", state)
    return cdr.bytes()


def ros1_string(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<I", len(raw)) + raw


def ros1_header(seq: int, stamp: int, frame: str) -> bytes:
    seconds, nanoseconds = divmod(stamp, SECOND)
    return struct.pack("<III", seq, seconds, nanoseconds) + ros1_string(frame)


def ros1_array(seq: int, stamp: int, statuses: tuple[Status, ...]) -> bytes:
    out = ros1_header(seq, stamp, "") + struct.pack("<I", len(statuses))
    for level, name, message, hardware, values in statuses:
        out += struct.pack("<b", level) + ros1_string(name) + ros1_string(message)
        out += ros1_string(hardware) + struct.pack("<I", len(values))
        for key, value in values:
            out += ros1_string(key) + ros1_string(value)
    return out


def ros1_husky(seq: int, stamp: int, e_stop: bool, voltage: float) -> bytes:
    floats = (50.0, 0.4, 1.2, 1.1, voltage, voltage, voltage, 31.0, 31.5, 28.0, 28.5)
    return (
        ros1_header(seq, stamp, "")
        + struct.pack("<Q", 3600 + seq)
        + struct.pack("<11d", *floats)
        + struct.pack("<Hd", 80, 0.8)
        + struct.pack("<6?", False, e_stop, e_stop, False, False, False)
    )


# --- The arm cell (MCAP) -------------------------------------------------------------------------

ARM_TOPICS: Final = (
    ("/diagnostics", "diagnostic_msgs/msg/DiagnosticArray", "ros2msg", DIAGNOSTIC_ARRAY2),
    ("/robot_status", "industrial_msgs/msg/RobotStatus", "ros2msg", ROBOT_STATUS),
    ("/ur/safety_mode", "ur_dashboard_msgs/msg/SafetyMode", "ros2msg", SAFETY_MODE),
    ("/gripper/status", "diagnostic_msgs/msg/DiagnosticStatus", "ros2idl", GRIPPER_STATUS_IDL),
)


def arm_messages() -> list[tuple[int, str, bytes]]:
    """(log time, topic, payload); the controller's stamps run 40 ms behind the recorder."""
    out: list[tuple[int, str, bytes]] = []
    for second in range(6):
        at = T0 + second * SECOND
        stamp = at - 40 * MS
        joint: Status = (OK, "arm/joint_3", "temperature 64 C", "J3-DRIVE", (("temp_c", "64"),))
        if second == 3:
            joint = (
                WARN,
                "arm/joint_3",
                "temperature 71 C (limit 70 C)",
                "J3-DRIVE",
                (("temp_c", "71"), ("limit_c", "70")),
            )
        if second >= 4:
            joint = (
                ERROR,
                "arm/joint_3",
                "following error 4.2 deg",
                "J3-DRIVE",
                (("error_deg", "4.2"),),
            )
        controller: Status = (OK, "arm/controller", "running", "ARM-7", ())
        out.append((at, "/diagnostics", cdr_array(stamp, "", (controller, joint))))
        for half in (0, 1):
            tick = at + half * SECOND // 2
            e_stopped = 1 if tick >= T0 + 4_500 * MS else 0
            in_error = 1 if tick >= T0 + 4 * SECOND else 0
            code = 1042 if in_error else 0
            out.append(
                (
                    tick + MS,
                    "/robot_status",
                    cdr_robot_status(tick - 40 * MS, e_stopped, in_error, code),
                )
            )
    out.append((T0 + 200 * MS, "/ur/safety_mode", cdr_byte("B", 1)))  # NORMAL
    out.append((T0 + 4_200 * MS, "/ur/safety_mode", cdr_byte("B", 3)))  # PROTECTIVE_STOP
    out.append((T0 + 4_600 * MS, "/ur/safety_mode", cdr_byte("B", 7)))  # ROBOT_EMERGENCY_STOP
    gripper: Status = (STALE, "gripper", "no update for 2.0 s", "GRIP-2F", (("age_s", "2.0"),))
    out.append(
        (T0 + 2_500 * MS, "/gripper/status", cdr_lone_status((OK, "gripper", "ok", "GRIP-2F", ())))
    )
    out.append((T0 + 5_500 * MS, "/gripper/status", cdr_lone_status(gripper)))
    return sorted(out, key=lambda m: (m[0], m[1]))


def arm_cell() -> bytes:
    ids = {name: n for n, (name, _, _, _) in enumerate(ARM_TOPICS, 1)}
    schemas = tuple(
        MCAP.Schema(ids[name], kind, encoding, text.encode())
        for name, kind, encoding, text in ARM_TOPICS
    )
    channels = tuple(MCAP.Channel(ids[name], ids[name], name, "cdr") for name, *_ in ARM_TOPICS)
    sequence: dict[str, int] = {}
    built = []
    for at, topic, data in arm_messages():
        count = sequence.get(topic, 0)
        sequence[topic] = count + 1
        built.append(MCAP.Message(ids[topic], count, at, at, data))
    half = len(built) // 2
    data, _ = MCAP.write(
        MCAP.Options(
            compression="zstd",
            schemas=schemas,
            channels=channels,
            messages=tuple(built),
            chunks=((0, half), (half, len(built))),
            attachment=False,
            metadata=False,
        )
    )
    return bytes(data)


# --- The mobile base (ROS 1 bag) -----------------------------------------------------------------

MOBILE_CONNECTIONS: Final = (
    BAG1.Connection(
        0,
        "/diagnostics_agg",
        "diagnostic_msgs/DiagnosticArray",
        "60810da900de1dd6ddd437c3503511da",
        DIAGNOSTIC_ARRAY1,
        "/diagnostic_aggregator",
        "0",
    ),
    BAG1.Connection(
        1,
        "/status",
        "husky_msgs/HuskyStatus",
        "fd724379c53d89ec4629be3b235dc10d",
        HUSKY_STATUS,
        "/husky_node",
        "0",
    ),
)


def mobile_messages() -> tuple[object, ...]:
    out = []
    for second in range(5):
        at = T0 + second * SECOND
        battery: Status = (
            OK,
            "/Power System/Battery",
            "OK",
            "husky_battery",
            (("voltage_v", "25.6"),),
        )
        if second >= 2:
            battery = (
                WARN,
                "/Power System/Battery",
                "Low",
                "husky_battery",
                (("voltage_v", "23.1"),),
            )
        lidar: Status = (OK, "/Sensors/Lidar", "OK", "LMS1xx-0451", ())
        if second == 4:
            lidar = (STALE, "/Sensors/Lidar", "No message received", "LMS1xx-0451", ())
        out.append(BAG1.Message(0, at, ros1_array(second, at, (battery, lidar))))
        e_stop = 2 <= second <= 3
        out.append(BAG1.Message(1, at + 10 * MS, ros1_husky(second, at, e_stop, 25.0 - second)))
    return tuple(out)


def mobile_base() -> bytes:
    messages = mobile_messages()
    data, _ = BAG1.write(
        BAG1.Options(
            compression="none",
            connections=MOBILE_CONNECTIONS,
            messages=messages,
            chunks=((0, 6), (6, len(messages))),
        )
    )
    return bytes(data)


# --- The autonomous shuttle (rosbag2, sqlite3) ----------------------------------------------------

AV_TOPICS: Final = (
    ("/diagnostics", "diagnostic_msgs/msg/DiagnosticArray", DIAGNOSTIC_ARRAY2),
    (
        "/system/emergency/emergency_state",
        "autoware_auto_system_msgs/msg/EmergencyState",
        EMERGENCY_STATE,
    ),
)


def av_messages() -> list[tuple[int, str, bytes]]:
    out: list[tuple[int, str, bytes]] = []
    for step in range(8):
        at = T0 + step * 500 * MS
        localization: Status = (OK, "localization: ndt_scan_matcher", "OK", "", ())
        if step >= 5:
            localization = (
                ERROR,
                "localization: ndt_scan_matcher",
                "NDT score below threshold",
                "",
                (("nearest_voxel_score", "1.9"), ("threshold", "2.3")),
            )
        out.append((at, "/diagnostics", cdr_array(at, "", (localization,))))
        state = 1 if step < 5 else (3 if step < 7 else 4)  # NORMAL, MRM_OPERATING, MRM_SUCCEEDED
        out.append((at + 5 * MS, "/system/emergency/emergency_state", cdr_emergency(at, state)))
    return out


def av_shuttle() -> bytes:
    """A rosbag2 sqlite3 storage file (schema version 4, rosbag2 Jazzy's)."""
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / "av_shuttle.db3"
        db = sqlite3.connect(path)
        db.executescript(
            """
            PRAGMA page_size = 4096;
            CREATE TABLE schema(schema_version INTEGER PRIMARY KEY, ros_distro TEXT NOT NULL);
            CREATE TABLE topics(id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
                serialization_format TEXT NOT NULL, offered_qos_profiles TEXT NOT NULL,
                type_description_hash TEXT NOT NULL);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, topic_id INTEGER NOT NULL,
                timestamp INTEGER NOT NULL, data BLOB NOT NULL);
            CREATE INDEX timestamp_idx ON messages (timestamp ASC);
            CREATE TABLE message_definitions(id INTEGER PRIMARY KEY, topic_type TEXT NOT NULL,
                encoding TEXT NOT NULL, encoded_message_definition TEXT NOT NULL,
                type_description_hash TEXT NOT NULL);
            """
        )
        db.execute("INSERT INTO schema VALUES (4, 'jazzy')")
        ids = {}
        for number, (name, kind, definition) in enumerate(AV_TOPICS, 1):
            ids[name] = number
            db.execute("INSERT INTO topics VALUES (?, ?, ?, 'cdr', '', '')", (number, name, kind))
            db.execute(
                "INSERT INTO message_definitions(topic_type, encoding, encoded_message_definition,"
                " type_description_hash) VALUES (?, 'ros2msg', ?, '')",
                (kind, definition),
            )
        db.executemany(
            "INSERT INTO messages(topic_id, timestamp, data) VALUES (?, ?, ?)",
            [(ids[topic], at, data) for at, topic, data in av_messages()],
        )
        db.commit()
        db.execute("VACUUM")
        db.close()
        data = bytearray(path.read_bytes())
    # The header's last 8 bytes name the SQLite library that wrote it; pinned, so the bytes are
    # the same whichever SQLite runs this.
    data[92:100] = struct.pack(">II", 1, 3_045_001)
    return bytes(data)


# --- The multicopter (PX4 ULog) ------------------------------------------------------------------

ARMED: Final = (
    "actuator_armed:uint64_t timestamp;bool armed;bool prearmed;bool ready_to_arm;bool lockdown;"
    "bool manual_lockdown;bool force_failsafe;bool in_esc_calibration_mode;uint8_t[1] _padding0;"
)


def quad_killswitch() -> bytes:
    u = ULOG
    boot = u.BOOT
    out = [u.header(), u.flag_bits(), u.fmt(ARMED)]
    out += [
        u.info("char[3] sys_name", b"PX4"),
        u.info("char[16] sys_uuid", b"quad-0042-killsw"),
        u.info("char[10] vehicle", b"multicopte"),
    ]
    out.append(u.subscribe(0, 0, "actuator_armed"))
    out.append(u.logged(6, boot + 100_000, "Armed by RC"))
    for k in range(8):
        ts = boot + 200_000 * (k + 1)
        killed = 3 <= k <= 5
        out.append(
            u.data(0, struct.pack("<Q7?", ts, True, False, True, False, killed, False, False))
        )
        if k == 3:
            out.append(u.logged(2, ts + 10, "Manual kill switch engaged"))
            out.append(u.tagged(4, 17, ts + 20, "[commander] Motors stopped"))
        if k == 6:
            out.append(u.logged(4, ts + 10, "Manual kill switch disengaged"))
    out.append(u.sync())
    return b"".join(out)


# --- The boat (ArduPilot DataFlash) --------------------------------------------------------------


def boat_failsafe() -> bytes:
    log = DF.Log()
    DF.header(
        log,
        {
            "MSG": ("QZ", "TimeUS,Message"),
            "ERR": ("QBB", "TimeUS,Subsys,ECode"),
            "MODE": ("QMBB", "TimeUS,Mode,ModeNum,Rsn"),
        },
    )
    DF.fmtu(log, "ERR", "s--", "F--")
    DF.fmtu(log, "MODE", "s---", "F---")
    log.add("MSG", 100, DF.text("ArduRover V4.5.1 (boat0042)", 64))
    log.add("MSG", 101, DF.text("Frame: BOAT", 64))
    log.add("MODE", 5_000_000, 10, 10, 1)  # AUTO
    log.add("ERR", 9_000_000, 5, 1)  # radio failsafe
    log.add("MSG", 9_000_050, DF.text("Failsafe: radio, action RTL", 64))
    log.add("MODE", 9_000_100, 11, 11, 3)  # RTL by failsafe
    log.add("ERR", 12_000_000, 5, 0)  # radio failsafe resolved
    log.add("ERR", 14_000_000, 11, 2)  # GPS glitch
    return bytes(log.bytes())


def build() -> dict[str, bytes]:
    return {
        "arm_cell.mcap": arm_cell(),
        "mobile_base.bag": mobile_base(),
        "av_shuttle.db3": av_shuttle(),
        "quad_killswitch.ulg": quad_killswitch(),
        "boat_failsafe.bin": boat_failsafe(),
    }


# --- The official readers as an oracle -----------------------------------------------------------

_ORACLE: Final = r"""
import json, sys
from pathlib import Path
import contextlib, io

HERE = Path(sys.argv[1])
TYPES = json.loads(sys.argv[2])

from rosbags.typesys import Stores, get_typestore, get_types_from_idl, get_types_from_msg


def store_for(definitions):
    store = get_typestore(Stores.EMPTY)
    for name, (encoding, text) in definitions.items():
        if encoding == "ros2idl":
            # rosbags' CDR serialiser has no IDL octet; uint8 is the same byte on the wire
            store.register(get_types_from_idl(text.replace("octet", "uint8")))
        else:
            store.register(get_types_from_msg(text, name))
    return store


def status(item):
    return [int(item.level), item.name, item.message, item.hardware_id,
            [[pair.key, pair.value] for pair in item.values]]


def entry(kind, message):
    if kind.endswith("DiagnosticArray"):
        return {"statuses": [status(item) for item in message.status]}
    if kind.endswith("DiagnosticStatus"):
        return {"statuses": [status(message)]}
    if kind.endswith("RobotStatus"):
        return {
            "e_stopped.val": int(message.e_stopped.val),
            "in_error.val": int(message.in_error.val),
        }
    if kind.endswith("SafetyMode"):
        return {"mode": int(message.mode)}
    if kind.endswith("EmergencyState"):
        return {"state": int(message.state)}
    if kind.endswith("HuskyStatus"):
        return {"e_stop": bool(message.e_stop)}
    raise ValueError(kind)


out = {}

# MCAP, by the mcap reader; payloads by rosbags' CDR deserialiser
from mcap.reader import make_reader
with (HERE / "arm_cell.mcap").open("rb") as stream:
    reader = make_reader(stream, validate_crcs=True)
    summary = reader.get_summary()
    # one store per schema: each declares its own copy of the types it uses
    stores = {
        s.name: store_for({s.name: (s.encoding, s.data.decode())})
        for s in summary.schemas.values()
    }
    rows = []
    for schema, channel, message in reader.iter_messages(log_time_order=False):
        decoded = stores[schema.name].deserialize_cdr(message.data, schema.name)
        rows.append([channel.topic, message.log_time, entry(schema.name, decoded)])
out["arm_cell.mcap"] = rows

# ROS 1, by rosbags' rosbag1 reader
from rosbags.rosbag1 import Reader as Reader1
with Reader1(HERE / "mobile_base.bag") as reader:
    store = store_for({c.msgtype: ("ros1msg", c.msgdef.data) for c in reader.connections})
    rows = []
    for connection, timestamp, data in reader.messages():
        decoded = store.deserialize_ros1(data, connection.msgtype)
        rows.append([connection.topic, timestamp, entry(connection.msgtype, decoded)])
out["mobile_base.bag"] = rows

# rosbag2 sqlite3, read with sqlite3; payloads by rosbags' CDR deserialiser
import sqlite3
db = sqlite3.connect(HERE / "av_shuttle.db3")
definitions = {t: ("ros2msg", d) for t, d in db.execute(
    "SELECT topic_type, encoded_message_definition FROM message_definitions")}
store = store_for(definitions)
topics = {i: (n, t) for i, n, t in db.execute("SELECT id, name, type FROM topics")}
rows = []
for topic, timestamp, data in db.execute(
        "SELECT topic_id, timestamp, data FROM messages ORDER BY id"):
    name, kind = topics[topic]
    rows.append([name, timestamp, entry(kind, store.deserialize_cdr(data, kind))])
out["av_shuttle.db3"] = rows

# PX4 ULog, by pyulog
from pyulog import ULog
with contextlib.redirect_stdout(io.StringIO()):
    log = ULog(str(HERE / "quad_killswitch.ulg"))
out["quad_killswitch.ulg"] = {
    "logged": [[m.timestamp, m.log_level, m.message] for m in log.logged_messages],
    "tagged": sorted(
        [m.timestamp, m.log_level, tag, m.message]
        for tag, messages in log.logged_messages_tagged.items() for m in messages
    ),
    "actuator_armed": [
        [int(t), bool(v)] for t, v in zip(
            log.get_dataset("actuator_armed").data["timestamp"].tolist(),
            log.get_dataset("actuator_armed").data["manual_lockdown"].tolist())
    ],
}

# ArduPilot DataFlash, by pymavlink
from pymavlink import DFReader
log = DFReader.DFReader_binary(str(HERE / "boat_failsafe.bin"), zero_time_base=True)
rows = []
while True:
    m = log.recv_match(type=["MSG", "ERR"])
    if m is None:
        break
    if m.get_type() == "MSG":
        rows.append(["MSG", m.TimeUS, m.Message])
    else:
        rows.append(["ERR", m.TimeUS, m.Subsys, m.ECode])
out["boat_failsafe.bin"] = rows

print(json.dumps(out, indent=1, sort_keys=True))
"""


def oracle() -> str:
    """What the official readers read from each fixture, as JSON."""
    packages = ("mcap", "rosbags", "pyulog", "pymavlink")
    command = ["uv", "run", "--no-project", *(f"--with={p}" for p in packages), "python", "-c"]
    result = subprocess.run(
        [*command, _ORACLE, str(HERE), json.dumps({})],
        check=True,
        capture_output=True,
        text=True,
    )
    return str(result.stdout)


def main() -> None:
    if sys.argv[1:] == ["--oracle"]:
        (HERE / "oracle.json").write_text(oracle())
        return
    for name, content in build().items():
        (HERE / name).write_bytes(content)


if __name__ == "__main__":
    main()

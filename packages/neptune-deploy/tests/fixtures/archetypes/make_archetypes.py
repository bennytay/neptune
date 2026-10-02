"""Generate the two deployment archetypes and run them through the pipeline (Deploy ADR 0004).

``sources/warehouse_amr_fleet`` is a small AMR fleet at two sites and ``sources/manipulator_cell``
is one arm in a cell. Each is a deployment folder as a team would hand it over: logs, bags, URDFs,
configs, maps, exports and PDFs. The fleet has a corrupt bag and a stale config; both deployments
have export rows that no mapping reads (an inspection, a task) and blank cells. Run from the
repository root::

    uv run python packages/neptune-deploy/tests/fixtures/archetypes/make_archetypes.py

It rewrites ``sources/`` and ``golden/``. Output is deterministic: no clock, randomness or network,
and no third-party writer. MCAP files and PDFs are written with the compiler's own fixture writers
(``tests/fixtures/mcap/make_mcap.py``, ``tests/fixtures/pdf/make_pdfs.py`` and this package's
``documents/make_document_fixtures.py``), loaded by path. Chunks are uncompressed, so the bytes do
not follow a compression library, and ROS 2 bags use MCAP storage, because a SQLite file's header
carries the library version.

``pipeline`` is the one ADR 0002 draws: ``neptune ingest`` (the SDK the command line wraps) builds
the base package from a folder, then the Deploy mapper builds a new lineage from declared mapping
files and document templates (``declared/``), each value citing the base package's cells and spans.
``golden/<name>/base`` keeps the base package's manifest and receipt. ``golden/<name>/lifecycle``
keeps the mapped package whole, because it is the lifecycle evidence later layers read.

Every number and name is invented. The fleet reuses the compiler's worked example identifiers
(site S-007, AMR-07, INC-0007, zones DOCK-1 and PICK-A); the cell reuses CELL-3 and ARM-3A.
"""

# ruff: noqa: E501  (the exports below are CSV as a person would open it, one row per line)

import csv
import importlib.util
import io
import json
import shutil
import struct
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Final

HERE: Final = Path(__file__).resolve().parent
ROOT: Final = HERE.parents[4]
SOURCES: Final = HERE / "sources"
GOLDEN: Final = HERE / "golden"
DECLARED: Final = HERE / "declared"
DOCUMENTS: Final = HERE.parent / "documents"
FLEET: Final = "warehouse_amr_fleet"
CELL: Final = "manipulator_cell"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


M: Final = _load("archetype_mcap_writer", ROOT / "tests" / "fixtures" / "mcap" / "make_mcap.py")
R: Final = _load(
    "archetype_rosbag2_writer", ROOT / "tests" / "fixtures" / "rosbag2" / "make_rosbag2.py"
)
D: Final = _load(
    "archetype_document_writer", HERE.parent / "documents" / "make_document_fixtures.py"
)

MS: Final = 10**6
NEPTUNE_YAML: Final = (
    "# Read every CSV here with its first row as the header (root ADR 0042 section 2).\n"
    "neptune: 1\nadapters:\n  tabular: {options: {csv_header: first_row}}\n"
)


def epoch(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    """Seconds since 1970-01-01T00:00:00Z of a UTC date-time."""
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


def text(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> bytes:
    """A CSV the way an export writes it: LF line ends, quotes only where needed."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode()


def as_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


# --- Logs: MCAP runs and ROS 2 bags -------------------------------------------------------------


def amr_run(robot: str, start: int, bias: float, volts: float, events: dict[int, str]) -> bytes:
    """One AMR run as MCAP (profile ``ros2``): an IMU (real CDR ``Imu``), a battery and diagnostics.

    36 messages, one IMU sample per 5 s, in two chunks. ``events`` names diagnostics by sample.
    """
    t0 = start * 10**9
    messages = []
    sequence = {1: 0, 2: 0, 3: 0}

    def add(channel: int, at_ms: int, data: bytes) -> None:
        messages.append(
            M.Message(channel, sequence[channel], t0 + at_ms * MS, t0 + at_ms * MS - 250_000, data)
        )
        sequence[channel] += 1

    for i in range(36):
        at = i * 5000
        imu_stamp = t0 + at * MS - 2 * MS
        add(1, at, M._cdr_imu(imu_stamp, "base_imu", bias + 0.01 * (i % 5), -0.1, 9.81))
        if i % 6 == 0:
            volts_now = round(volts - 0.05 * (i // 6), 2)
            pct = round(0.9 - 0.02 * (i // 6), 2)
            add(2, at + 2, _json_bytes({"percentage": pct, "voltage": volts_now}))
        if i in events:
            add(3, at + 3, _json_bytes({"level": events[i], "robot": robot}))
    channels = (
        M.Channel(1, 1, "/imu", "cdr", {"offered_qos_profiles": QOS}),
        M.Channel(2, 2, "/battery", "json"),
        M.Channel(3, 0, "/diagnostics", "json", {"source": robot}),
    )
    half = len(messages) // 2
    options = M.Options(
        compression="",
        schemas=M.SCHEMAS,
        channels=channels,
        messages=tuple(messages),
        chunks=((0, half), (half, len(messages))),
        attachment=False,
        metadata=False,
    )
    return bytes(M.write(options)[0])


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class Topic:
    name: str
    type: str
    definition: str


# The definitions a ROS 2 message that carries a header appends, one section per nested type.
HEADER_MSGS: Final = (
    "=" * 80
    + "\nMSG: std_msgs/Header\nbuiltin_interfaces/Time stamp\nstring frame_id\n"
    + "=" * 80
    + "\nMSG: builtin_interfaces/Time\nint32 sec\nuint32 nanosec\n"
)
JOINT_STATE: Final = Topic(
    "/joint_states",
    "sensor_msgs/msg/JointState",
    "std_msgs/Header header\nstring[] name\nfloat64[] position\nfloat64[] velocity\n"
    "float64[] effort\n" + HEADER_MSGS,
)
STRING: Final = Topic("/status", "std_msgs/msg/String", "string data\n")
FLOAT32: Final = Topic("/battery_voltage", "std_msgs/msg/Float32", "float32 data\n")
IMU: Final = Topic("/imu", "sensor_msgs/msg/Imu", M.IMU_DEFINITION)
CDR_HEADER: Final = R.CDR
# The QoS profile text rosbag2 writes; the official reader needs every key of it.
QOS: Final = R.QOS


class Cdr:
    """Little-endian CDR as ROS 2 writes it: alignment counts from after the 4-byte header."""

    def __init__(self) -> None:
        self.out = bytearray(CDR_HEADER)

    def align(self, size: int) -> None:
        while (len(self.out) - 4) % size:
            self.out.append(0)

    def u32(self, value: int) -> None:
        self.align(4)
        self.out += struct.pack("<I", value)

    def i32(self, value: int) -> None:
        self.align(4)
        self.out += struct.pack("<i", value)

    def f32(self, value: float) -> None:
        self.align(4)
        self.out += struct.pack("<f", value)

    def string(self, value: str) -> None:
        raw = value.encode() + b"\x00"
        self.u32(len(raw))
        self.out += raw

    def f64s(self, values: Sequence[float]) -> None:
        self.u32(len(values))
        self.align(8)
        for value in values:
            self.out += struct.pack("<d", value)


def string_message(value: str) -> bytes:
    cdr = Cdr()
    cdr.string(value)
    return bytes(cdr.out)


def float32_message(value: float) -> bytes:
    cdr = Cdr()
    cdr.f32(value)
    return bytes(cdr.out)


def joint_state(
    stamp_ns: int, names: Sequence[str], position: Sequence[float], effort: Sequence[float]
) -> bytes:
    cdr = Cdr()
    seconds, nanoseconds = divmod(stamp_ns, 10**9)
    cdr.i32(seconds)
    cdr.u32(nanoseconds)
    cdr.string("base_link")
    cdr.u32(len(names))
    for name in names:
        cdr.string(name)
    cdr.f64s(position)
    cdr.f64s([0.0] * len(names))
    cdr.f64s(effort)
    return bytes(cdr.out)


def imu_message(stamp_ns: int, ax: float) -> bytes:
    return bytes(M._cdr_imu(stamp_ns, "base_imu", ax, -0.1, 9.81))


def ros2_bag(
    name: str,
    topics: Sequence[Topic],
    messages: Sequence[tuple[int, str, bytes]],
    cut: bool = False,
) -> dict[str, bytes]:
    """A rosbag2 bag with MCAP storage: ``metadata.yaml`` and one ``.mcap`` part.

    ``cut`` truncates the storage file in the middle of its last chunk, as a copy that was
    interrupted would be: the metadata still claims every message.
    """
    ids = {topic.name: n for n, topic in enumerate(topics, 1)}
    schemas = tuple(M.Schema(ids[t.name], t.type, "ros2msg", t.definition.encode()) for t in topics)
    channels = tuple(
        M.Channel(ids[t.name], ids[t.name], t.name, "cdr", {"offered_qos_profiles": QOS})
        for t in topics
    )
    ordered = sorted(messages, key=lambda m: (m[0], m[1]))
    sequence: dict[str, int] = {}
    built = []
    for stamp, topic, data in ordered:
        count = sequence.get(topic, 0)
        sequence[topic] = count + 1
        built.append(M.Message(ids[topic], count, stamp, stamp, data))
    third = max(1, len(built) // 3)
    chunks = ((0, third), (third, 2 * third), (2 * third, len(built)))
    data, at = M.write(
        M.Options(
            compression="",
            schemas=schemas,
            channels=channels,
            messages=tuple(built),
            chunks=chunks,
            attachment=False,
            metadata=False,
        )
    )
    if cut:
        offset, length = at["chunk:2"]
        data = data[: offset + length // 2]
    part = f"{name}_0.mcap"
    first, last = ordered[0][0], ordered[-1][0]
    qos = json.dumps(QOS)
    lines = [
        "rosbag2_bagfile_information:",
        "  version: 5",
        "  storage_identifier: mcap",
        "  relative_file_paths:",
        f"    - {part}",
        "  duration:",
        f"    nanoseconds: {last - first}",
        "  starting_time:",
        f"    nanoseconds_since_epoch: {first}",
        f"  message_count: {len(ordered)}",
        "  topics_with_message_count:",
    ]
    for entry in topics:
        lines += [
            "    - topic_metadata:",
            f"        name: {entry.name}",
            f"        type: {entry.type}",
            "        serialization_format: cdr",
            f"        offered_qos_profiles: {qos}",
            f"      message_count: {sequence.get(entry.name, 0)}",
        ]
    lines += [
        '  compression_format: ""',
        '  compression_mode: ""',
        "  files:",
        f"    - path: {part}",
        "      starting_time:",
        f"        nanoseconds_since_epoch: {first}",
        "      duration:",
        f"        nanoseconds: {last - first}",
        f"      message_count: {len(ordered)}",
    ]
    return {"metadata.yaml": text(*lines), part: bytes(data)}


# --- The warehouse AMR fleet --------------------------------------------------------------------

# robot, site, model. AMR-07 is the compiler's worked example robot.
FLEET_ROBOTS: Final = (
    ("AMR-05", "S-007", "tug-200"),
    ("AMR-06", "S-007", "tug-200"),
    ("AMR-07", "S-007", "lift-150"),
    ("AMR-08", "S-012", "tug-200"),
    ("AMR-09", "S-012", "lift-150"),
    ("AMR-10", "S-012", "lift-150"),
)


def urdf_amr(model: str, lift: bool) -> bytes:
    """A small differential-drive AMR; the lift model adds a prismatic fork carriage."""
    lines = [
        '<?xml version="1.0"?>',
        f'<robot name="{model}">',
        '  <link name="base_footprint"/>',
        '  <link name="base_link">',
        '    <inertial><mass value="210.0"/><origin xyz="0 0 0.18"/>',
        '      <inertia ixx="9.1" ixy="0" ixz="0" iyy="14.6" iyz="0" izz="19.8"/></inertial>',
        '    <visual><geometry><box size="1.10 0.70 0.30"/></geometry></visual>',
        "  </link>",
        '  <joint name="base_joint" type="fixed">',
        '    <parent link="base_footprint"/><child link="base_link"/>',
        '    <origin xyz="0 0 0.12" rpy="0 0 0"/>',
        "  </joint>",
    ]
    for side, y in (("left", 0.31), ("right", -0.31)):
        lines += [
            f'  <link name="{side}_wheel"><visual><geometry>',
            '    <cylinder radius="0.12" length="0.06"/></geometry></visual></link>',
            f'  <joint name="{side}_wheel_joint" type="continuous">',
            f'    <parent link="base_link"/><child link="{side}_wheel"/>',
            f'    <origin xyz="0 {y} -0.0" rpy="-1.5708 0 0"/><axis xyz="0 0 1"/>',
            "  </joint>",
        ]
    lines += [
        '  <link name="lidar_front"/>',
        '  <joint name="lidar_front_joint" type="fixed">',
        '    <parent link="base_link"/><child link="lidar_front"/>',
        '    <origin xyz="0.52 0 0.10" rpy="0 0 0"/>',
        "  </joint>",
        '  <link name="base_imu"/>',
        '  <joint name="base_imu_joint" type="fixed">',
        '    <parent link="base_link"/><child link="base_imu"/>',
        '    <origin xyz="0.0 0 0.05" rpy="0 0 0"/>',
        "  </joint>",
    ]
    if lift:
        lines += [
            '  <link name="fork_carriage"/>',
            '  <joint name="fork_lift_joint" type="prismatic">',
            '    <parent link="base_link"/><child link="fork_carriage"/>',
            '    <origin xyz="0.62 0 0.10" rpy="0 0 0"/><axis xyz="0 0 1"/>',
            '    <limit lower="0.0" upper="1.6" effort="4500" velocity="0.2"/>',
            "  </joint>",
        ]
    lines.append("</robot>")
    return text(*lines)


def nav_params(
    robot: str, site: str, revision: int, firmware: str, max_vel: float, stale: bool = False
) -> bytes:
    """An AMR's navigation parameters. A stale copy was hand-merged and keeps an old line."""
    lines = [
        f"# {robot} navigation parameters, exported by the fleet manager",
        f"config_revision: {revision}",
        f"robot_id: {robot}",
        f"site: {site}",
        f"firmware_compat: {firmware}",
        "controller_server:",
        "  ros__parameters:",
        "    controller_frequency: 20.0",
        f"    max_vel_x: {max_vel}",
        "    min_vel_x: 0.0",
        "    max_vel_theta: 0.8",
    ]
    if stale:
        lines.append("    max_vel_x: 1.2")  # the value before the rollout, left in by a hand merge
    lines += [
        "local_costmap:",
        "  ros__parameters:",
        "    inflation_radius: 0.55",
        "    robot_radius: 0.62",
        "safety:",
        "  protective_field_m: 1.4",
        "  warning_field_m: 2.8",
    ]
    return text(*lines)


def geojson_zones(site: str, zones: Sequence[tuple[str, str, float, tuple[float, float]]]) -> bytes:
    """A zone map: one rectangle per zone, in the site's own metric grid."""
    features = []
    for zone, name, limit, (x, y) in zones:
        ring = [[x, y], [x + 18.0, y], [x + 18.0, y + 6.0], [x, y + 6.0], [x, y]]
        features.append(
            {
                "type": "Feature",
                "properties": {"zone_id": zone, "name": name, "speed_limit_mps": limit},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )
    return as_json(
        {
            "type": "FeatureCollection",
            "name": f"{site}_zones",
            "crs": {"type": "name", "properties": {"name": "local:site-grid-m"}},
            "features": features,
        }
    )


def pdf_incident(
    number: str,
    site: str,
    zone: str,
    location: str,
    machine: str,
    assets: str,
    occurred: str,
    severity: str,
    related: str,
    timeline: Sequence[tuple[str, str]],
    description: Sequence[str],
    cause: Sequence[str],
) -> bytes:
    first = D.PageSpec(
        [
            D.Heading("Incident report", 1),
            D.Para([f"Incident no: {number}"]),
            D.Para([f"Site: {site}"]),
            D.Para([f"Zone: {zone}"]),
            D.Para([f"Location: {location}"]),
            D.Para([f"Machine: {machine}"]),
            D.Para([f"Assets: {assets}"]),
            D.Para([f"Occurred at: {occurred}"]),
            D.Para([f"Severity: {severity}"]),
            D.Para([f"Related records: {related}"]),
            D.Heading("Timeline"),
            D.Table([("Time", "Event"), *timeline], (150, 540)),
        ]
    )
    second = D.PageSpec(
        [
            D.Heading("Description"),
            D.Para(list(description)),
            D.Heading("Root cause"),
            D.Para(list(cause)),
        ]
    )
    return bytes(D.tagged_pdf(f"Incident report {number}", [first, second]))


FLEET_CMMS: Final = """\
WO Number,WO Type,Asset ID,Site,Completed,Problem,Work Performed,Component,Removed Serial,Installed Serial,Firmware After,Related,Technician,Downtime h
WO-26-0301,PM,AMR-05,S-007,2026-03-02 09:10,Scheduled 500 h service,Replace drive wheel; Clean lidar window,Drive wheel assembly,DW-11801,DW-12911,4.2.0,,J. Ortiz,1.5
WO-26-0302,PM,AMR-06,S-007,2026-03-02 13:40,Scheduled 500 h service,Clean lidar window; Check bumper,,,,4.2.0,,J. Ortiz,1
WO-26-0303,PM,AMR-07,S-007,2026-03-03 08:30,Scheduled 500 h service,Replace drive wheel; Clean lidar window,Drive wheel assembly,DW-11873,DW-12990,4.2.0,,J. Ortiz,1.5
WO-26-0304,PM,AMR-08,S-012,2026-03-04 10:05,Scheduled 500 h service,Clean lidar window; Check bumper,,,,4.2.0,,A. Weber,1
WO-26-0305,PM,AMR-09,S-012,2026-03-05 11:20,Scheduled 500 h service,Replace drive wheel,Drive wheel assembly,DW-11902,DW-13004,4.2.0,,A. Weber,1.25
WO-26-0306,PM,AMR-10,S-012,07/03/2026,Scheduled 500 h service,Clean lidar window,,,,4.2.0,,A. Weber,0.75
WO-26-0319,CM,AMR-07,S-007,2026-03-19 15:10,Fork carriage chain stretch,Replace lift chain; Re-tension,Lift chain,LC-0442,LC-0518,4.2.0,,J. Ortiz,2.5
WO-26-0402,CM,AMR-07,S-007,2026-04-02 16:00,Contact with rack upright,Straighten left fork; Inspect mast,,,,4.2.0,INC-0007,J. Ortiz,4
WO-26-0414,CM,AMR-07,S-007,2026-04-14 19:30,Localisation drift after map update,Flash controller firmware; Re-run lidar calibration,,,,4.3.1,CHG0050023; INC-0007,J. Ortiz,1.5
WO-26-0420,INSP,AMR-09,S-012,2026-04-20 08:00,Monthly inspection,Visual check,,,,4.3.1,,A. Weber,0.5
WO-26-0520,CM,AMR-08,S-012,2026-05-19 15:30,Bumper switch intermittent after contact,Replace bumper switch,Bumper switch,BS-2201,BS-2307,4.3.1,INC-0013,A. Weber,2
WO-26-0611,CM,AMR-10,S-012,,Charging contacts worn,Replace charge contact plate,Charge contact plate,CC-0091,CC-0144,4.3.1,,A. Weber,3
"""


def fleet_cmms() -> bytes:
    return FLEET_CMMS.encode()


def fleet_changes() -> bytes:
    header = (
        "number",
        "short_description",
        "category",
        "type",
        "cmdb_ci",
        "u_site",
        "approval",
        "approval_set",
        "start_date",
        "end_date",
        "u_before",
        "u_after",
        "assignment_group",
        "description",
    )
    rows = []
    for n, (robot, site, _) in enumerate(FLEET_ROBOTS, 21):
        rows.append(
            (
                f"CHG00500{n}",
                "Fleet firmware rollout 4.3.1",
                "Software",
                "Normal",
                robot,
                site,
                "approved",
                "2026-04-10 15:00:00",
                "2026-04-14 18:00:00",
                "2026-04-14 20:00:00",
                "4.2.0",
                "4.3.1",
                "Fleet Engineering",
                "Rollout of drive controller firmware 4.3.1: fixes pose jump after map reload.",
            )
        )
    return table(header, rows)


FLEET_REQUALIFICATION: Final = """\
Requal ID,Site,Robot,Performed,Cause,Corrective Actions,Test 1,Result 1,Test 2,Result 2,Test 3,Result 3,Result,Decision,Decided By,Decided On,Work Order,Inspector
RQ-S007-0007,S-007,AMR-07,2026-04-15 09:30,Firmware 4.3.1 (CHG0050023) and map revision 14,Re-teach PICK-A pick positions; Verify rack face offsets,Protective stop distance at 1.5 m/s,0.94 m,Lidar field switch at DOCK-1,PASS,Fork height interlock,PASS,PASS,Returned to service,Site safety lead,2026-04-15 11:00,WO-26-0414,R. Okafor
RQ-S012-0003,S-012,AMR-09,2026-04-16 13:10,Firmware 4.3.1 (CHG0050025),Verify localisation repeatability,Localisation repeatability at PICK-C,22 mm,Protective stop distance at 1.2 m/s,0.81 m,,,PASS with note,Returned to service with speed restriction,Site safety lead,,WO-26-0420,S. Brandt
"""


def fleet_requalification() -> bytes:
    return FLEET_REQUALIFICATION.encode()


ZONE_REGISTER: Final = """\
Envelope ID,Site,Robots,Zone,Speed Limit,Speed Unit,Payload Max,Payload Unit,Missions,Supervision,Depends On,Valid From,Valid Until,Approved By,Approved On
ENV-S007-03,S-007,AMR-05; AMR-06; AMR-07,DOCK-1,0.8,m/s,600,kg,Pallet transfer; Charging,"remote, 1 operator : 6 robots",Wi-Fi AP-3; Door interlock D2,2026-03-09,2026-09-09,Site safety lead,2026-03-06
ENV-S007-04,S-007,AMR-07,PICK-A,1.5,m/s,450,kg,Tote picking,on-site spotter,Wi-Fi AP-4,2026-03-09,2026-09-09,Site safety lead,2026-03-06
ENV-S012-01,S-012,AMR-08; AMR-09; AMR-10,CROSS-2,0.6,m/s,500,kg,Pallet transfer,on-site spotter,Door interlock D5,2026-03-16,2026-09-16,Site safety lead,2026-03-12
ENV-S012-02,S-012,AMR-09; AMR-10,PICK-C,1.2,m/s,450,kg,Tote picking; Charging,,Wi-Fi AP-2,2026-03-16,,Site safety lead,2026-03-12
"""


def zone_register() -> bytes:
    return ZONE_REGISTER.encode()


def fleet_run_files() -> dict[str, bytes]:
    runs: tuple[tuple[str, str, str, int, float, float, dict[int, str]], ...] = (
        ("S-007", "amr-05_2026-03-03", "AMR-05", epoch(2026, 3, 3, 21, 30), 0.10, 24.4, {}),
        (
            "S-007",
            "amr-06_2026-03-03",
            "AMR-06",
            epoch(2026, 3, 3, 1, 10),
            0.12,
            24.1,
            {18: "warn"},
        ),
        (
            "S-007",
            "amr-07_2026-04-02",
            "AMR-07",
            epoch(2026, 4, 2, 4, 5),
            0.14,
            24.2,
            {24: "error"},
        ),
        ("S-007", "amr-07_2026-04-15", "AMR-07", epoch(2026, 4, 15, 22, 0), 0.11, 24.6, {}),
        ("S-012", "amr-09_2026-03-05", "AMR-09", epoch(2026, 3, 5, 9, 0), 0.13, 23.9, {}),
        (
            "S-012",
            "amr-10_2026-03-06",
            "AMR-10",
            epoch(2026, 3, 6, 9, 40),
            0.09,
            24.0,
            {30: "warn"},
        ),
    )
    files = {}
    for site, name, robot, start, bias, volts, events in runs:
        files[f"runs/{site}/{name}.mcap"] = amr_run(robot, start, bias, volts, events)
    # AMR-08's bag from the day of INC-0013, copied off the robot and cut short.
    start = epoch(2026, 5, 18, 22, 40) * 10**9
    messages: list[tuple[int, str, bytes]] = []
    for i in range(24):
        stamp = start + i * 5 * 10**9
        messages.append((stamp, "/imu", imu_message(stamp, 0.1 + 0.01 * (i % 4))))
        if i % 4 == 0:
            messages.append((stamp + 2 * MS, "/battery_voltage", float32_message(24.0 - 0.1 * i)))
        if i % 8 == 0:
            messages.append((stamp + 3 * MS, "/status", string_message("driving")))
    for path, data in ros2_bag(
        "amr-08_2026-05-19", (IMU, FLOAT32, STRING), messages, cut=True
    ).items():
        files[f"runs/S-012/amr-08_2026-05-19/{path}"] = data
    return files


def fleet() -> dict[str, bytes]:
    files: dict[str, bytes] = {
        "neptune.yaml": NEPTUNE_YAML.encode(),
        "urdf/tug_200.urdf": urdf_amr("tug-200", lift=False),
        "urdf/lift_150.urdf": urdf_amr("lift-150", lift=True),
        "maps/S-007_zones.geojson": geojson_zones(
            "S-007",
            (
                ("DOCK-1", "Outbound dock", 0.8, (0.0, 0.0)),
                ("PICK-A", "Pick aisle A", 1.5, (0.0, 12.0)),
            ),
        ),
        "maps/S-012_zones.geojson": geojson_zones(
            "S-012",
            (
                ("CROSS-2", "Cross aisle 2", 0.6, (0.0, 0.0)),
                ("PICK-C", "Pick aisle C", 1.2, (0.0, 12.0)),
            ),
        ),
        "authorisation/zone_register.csv": zone_register(),
        "cmms/work_orders.csv": fleet_cmms(),
        "changes/servicenow_changes.csv": fleet_changes(),
        "requalification/requalification_tests.csv": fleet_requalification(),
        "incidents/INC-0007.pdf": pdf_incident(
            "INC-0007",
            "S-007",
            "PICK-A",
            "Aisle A, rack face 14B",
            "AMR-07",
            "PAL-5521; RACK-14B",
            "2026-04-02 14:07",
            "Minor, no injury",
            "WO-26-0402",
            (
                ("2026-04-02 14:05", "AMR-07 starts pallet pick in aisle A"),
                ("2026-04-02 14:07", "Fork contacts rack upright; protective stop"),
                ("2026-04-02 14:09", "Fleet manager flags AMR-07 as blocked"),
                ("2026-04-02 14:31", "Technician clears the aisle and resets"),
            ),
            (
                "While lifting pallet PAL-5521 the left fork touched the rack upright and the",
                "mast stopped. The pallet stayed on the forks. No person was in the aisle.",
            ),
            (
                "The rack face was 40 mm further out than the site map. The pick position",
                "had not been refreshed after the rack was re-bolted on 2026-03-28.",
            ),
        ),
        "incidents/INC-0013.pdf": pdf_incident(
            "INC-0013",
            "S-012",
            "CROSS-2",
            "Cross aisle 2, door D5",
            "AMR-08",
            "PJ-0031",
            "2026-05-19 08:42",
            "Near miss",
            "WO-26-0520",
            (
                ("2026-05-19 08:41", "AMR-08 crosses aisle 2 at the declared 0.6 m/s"),
                ("2026-05-19 08:42", "A pallet jack enters the lane; bumper contact and stop"),
                ("2026-05-19 08:44", "Operator confirms nobody hurt and clears the lane"),
            ),
            (
                "A pallet jack driven by a picker entered the cross aisle from behind door D5.",
                "The robot stopped on its bumper. The picker was not touched.",
            ),
            (
                "Door D5 was propped open for loading, so the interlock did not hold the lane.",
                "The envelope lists the door interlock as a dependency.",
            ),
        ),
    }
    for robot, site, model in FLEET_ROBOTS:
        stale = robot == "AMR-09"
        files[f"config/{robot}/nav2_params.yaml"] = nav_params(
            robot,
            site,
            11 if stale else 12,
            "4.2.0" if stale else "4.3.1",
            1.0 if model == "tug-200" else 0.9,
            stale=stale,
        )
    files.update(fleet_run_files())
    return files


# --- The manipulator cell -----------------------------------------------------------------------

ARM_JOINTS: Final = ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6")


def urdf_arm() -> bytes:
    lines = ['<?xml version="1.0"?>', '<robot name="arm6">', '  <link name="base_link"/>']
    parent = "base_link"
    heights = (0.35, 0.0, 0.42, 0.0, 0.40, 0.0)
    for n, (joint, height) in enumerate(zip(ARM_JOINTS, heights, strict=True), 1):
        axis = "0 0 1" if n in (1, 4, 6) else "0 1 0"
        lines += [
            f'  <link name="link_{n}"/>',
            f'  <joint name="{joint}" type="revolute">',
            f'    <parent link="{parent}"/><child link="link_{n}"/>',
            f'    <origin xyz="0 0 {height}" rpy="0 0 0"/><axis xyz="{axis}"/>',
            '    <limit lower="-3.14" upper="3.14" effort="150" velocity="3.1"/>',
            "  </joint>",
        ]
        parent = f"link_{n}"
    lines += [
        '  <link name="tool0"/>',
        '  <joint name="tool0_joint" type="fixed">',
        f'    <parent link="{parent}"/><child link="tool0"/>',
        '    <origin xyz="0 0 0.09" rpy="0 0 0"/>',
        "  </joint>",
        "</robot>",
    ]
    return text(*lines)


def calibration(
    ident: str, performed: str, reason: str, xyz: tuple[float, float, float], error: float
) -> bytes:
    return text(
        f"calibration_id: {ident}",
        "robot: ARM-3A",
        f"performed: '{performed}'",
        f"reason: {reason}",
        "method: hand-eye, 24 poses, ChArUco board",
        "eye_on_hand: true",
        "robot_effector_frame: tool0",
        "tracking_base_frame: wrist_camera",
        "translation:",
        f"  x: {xyz[0]}",
        f"  y: {xyz[1]}",
        f"  z: {xyz[2]}",
        "  unit: m",
        f"reprojection_error_px: {error}",
    )


def arm_bag() -> dict[str, bytes]:
    start = epoch(2026, 8, 20, 6, 0) * 10**9
    messages: list[tuple[int, str, bytes]] = []
    for i in range(60):
        stamp = start + i * 100 * MS
        phase = i / 60.0 * 3.0
        position = [round(0.4 * (1 - (j % 2) * 0.5) * (phase - 1.5) / 1.5, 4) for j in range(6)]
        effort = [round(12.0 + 3.0 * j + 0.5 * (i % 7), 2) for j in range(6)]
        messages.append((stamp, "/joint_states", joint_state(stamp, ARM_JOINTS, position, effort)))
        if i % 20 == 0:
            label = ("home", "picking", "placing")[i // 20]
            messages.append((stamp + 5 * MS, "/status", string_message(label)))
    return {
        f"bags/pick_place_2026-08-20/{path}": data
        for path, data in ros2_bag("pick_place_2026-08-20", (JOINT_STATE, STRING), messages).items()
    }


def pdf_risk() -> bytes:
    fields = [
        ("Document no", "CELL3-RA-009"),
        ("Revision", "B"),
        ("Cell", "CELL-3"),
        ("Robots", "ARM-3A"),
        ("Assessment date", "20/02/2026"),
        ("Method", "ISO 10218-2 risk estimation, severity x exposure x avoidance"),
    ]
    hazards = [
        (
            "Task",
            "Hazard",
            "Severity of injury",
            "Exposure",
            "Avoidance",
            "Risk level",
            "Risk reduction measures",
        ),
        (
            "Pallet change",
            "Crushing between the arm and the pallet stack",
            "Serious",
            "Frequent",
            "Possible",
            "High",
            "Light curtain on the pallet gate; safety-rated monitored stop",
        ),
        (
            "Tool change",
            "Gripper release with a part held",
            "Serious",
            "Seldom",
            "Possible",
            "Medium",
            "Part-present check; two-hand enabling for manual tool change",
        ),
        (
            "Teaching",
            "Unexpected motion in manual mode",
            "Serious",
            "Seldom",
            "Likely",
            "Medium",
            "Reduced speed 250 mm/s; enabling switch",
        ),
    ]
    approval = [
        ("Approved by", "Plant safety manager"),
        ("Decision", "Approved"),
        ("Approval date", "24/02/2026"),
    ]
    first = D.PageSpec(
        [
            D.Heading("Robot cell risk assessment", 1),
            D.Table(fields, (150, 540), header=False),
            D.Heading("Hazards"),
            D.Table(hazards, (70, 150, 80, 60, 60, 60, 262)),
            D.Heading("Approval"),
            D.Table(approval, (150, 540), header=False),
        ]
    )
    return bytes(D.tagged_pdf("Cell 3 risk assessment", [first]))


def pdf_commissioning() -> bytes:
    first = D.PageSpec(
        [
            D.Heading("Commissioning report: Cell 3 palletising", 1),
            D.Para(["Report no: CR-C3-2026-02"]),
            D.Para(["Cell: CELL-3"]),
            D.Para(["Robot: ARM-3A"]),
            D.Para(["Configuration baseline: cfg-c3-1.4"]),
            D.Para(["Commissioned on: 2026-02-26 16:30"]),
            D.Para(["Calibration records: CAL-ARM3A-0226"]),
            D.Heading("Hardware"),
            D.Table(
                [
                    ("Item", "Model", "Serial", "Firmware"),
                    ("Manipulator ARM-3A", "IRB-6700", "SN-6700-118", "7.8.1"),
                    ("Controller", "OmniCore C30", "SN-C30-552", "7.8.1"),
                    ("Gripper", "PG-80", "PG80-0931", "2.3"),
                ],
                (160, 150, 150, 230),
            ),
            D.Heading("Software"),
            D.Table(
                [
                    ("Component", "Version"),
                    ("Palletising application", "1.4.0"),
                    ("Cell controller software", "5.4.2"),
                    ("Safety configuration", "2026.02-a"),
                ],
                (300, 390),
            ),
        ]
    )
    second = D.PageSpec(
        [
            D.Heading("Acceptance tests"),
            D.Table(
                [
                    ("Test", "Result", "Performed"),
                    ("Safety-rated monitored stop", "PASS", "2026-02-26 11:05"),
                    ("Light curtain response", "PASS 142 ms", "2026-02-26 11:40"),
                    ("Palletising cycle, 50 pallets", "PASS", "2026-02-26 14:15"),
                ],
                (300, 190, 200),
            ),
            D.Heading("Constraints"),
            D.Item("Payload not above 35 kg"),
            D.Item("Pallet gate closed during automatic mode"),
            D.Heading("Sign-off"),
            D.Para(["Sign-off: Accepted"]),
            D.Para(["Signed by: Commissioning engineer"]),
            D.Para(["Signed on: 2026-02-27 09:10"]),
        ]
    )
    return bytes(D.tagged_pdf("Commissioning report CR-C3-2026-02", [first, second]))


def pdf_sop() -> bytes:
    first = D.PageSpec(
        [
            D.Heading("SOP-CELL-014 Gripper finger set change", 1),
            D.Para(["Procedure: SOP-CELL-014"]),
            D.Para(["Revision: A"]),
            D.Heading("Work record"),
            D.Para(["Work order: WO-26-0391"]),
            D.Para(["Machine: ARM-3A"]),
            D.Para(["Performed on: 2026-08-18"]),
            D.Para(["Diagnosis: Finger pads worn beyond limit; set replaced with the long set"]),
            D.Heading("Procedure"),
            D.Item("Stop the cell and lock out the controller"),
            D.Item("Release the gripper and remove the two finger retaining screws"),
            D.Item("Fit the new finger set and torque the screws to 2.5 N.m"),
            D.Item("Update the tool centre point and run the part-present check"),
            D.Heading("Parts replaced"),
            D.Table(
                [
                    ("Part", "Removed", "Installed"),
                    ("Finger set PG-80", "FS-0183", "FS-0291"),
                    ("Retaining screw set", "SCR-KIT-77", "SCR-KIT-81"),
                ],
                (260, 215, 215),
            ),
            D.Para(["Performed by: K. Patel"]),
        ]
    )
    return bytes(D.tagged_pdf("SOP-CELL-014", [first]))


CELL_CMMS: Final = """\
WO Number,WO Type,Asset ID,Site,Completed,Problem,Work Performed,Component,Removed Serial,Installed Serial,Firmware After,Related,Technician,Downtime h
WO-26-0310,CM,ARM-3A,CELL-3,2026-03-10 19:45,Joint 4 brake test timeout,Apply controller software update,,,,5.6.0,CHG0030012,K. Patel,1.75
WO-26-0415,PM,ARM-3A,CELL-3,2026-04-15 10:20,Scheduled calibration check,Re-run hand-eye calibration,,,,5.6.0,CAL-ARM3A-0415,K. Patel,2
WO-26-0623,CM,ARM-3A,CELL-3,2026-06-23 17:45,Joint 4 drive noise,Replace joint 4 drive; Re-run hand-eye calibration,Joint 4 drive unit,JD4-0771,JD4-0912,5.6.0,CAL-ARM3A-0623,K. Patel,6
WO-26-0709,INSP,ARM-3A,CELL-3,2026-07-09 15:00,Review after near miss,Review of light curtain muting; no fault found,,,,5.6.0,INC-C3-0004,K. Patel,0.5
WO-26-0391,CM,ARM-3A,CELL-3,2026-08-18 13:00,Finger pads worn,Replace finger set; Re-run hand-eye calibration,Finger set PG-80,FS-0183,FS-0291,5.6.0,CAL-ARM3A-0818; CHG0030013,K. Patel,3
"""


def cell_cmms() -> bytes:
    return CELL_CMMS.encode()


CELL_CHANGES: Final = """\
number,short_description,category,type,cmdb_ci,u_site,approval,approval_set,start_date,end_date,u_before,u_after,assignment_group,description
CHG0030012,Update cell controller software,Software,Normal,ARM-3A,PLANT-2,approved,2026-03-09 16:30:00,2026-03-10 18:00:00,2026-03-10 19:30:00,5.4.2,5.6.0,Robotics Engineering,Controller software update to fix the joint 4 brake test timeout.
CHG0030013,Gripper TCP offset after finger replacement,Parameter,Standard,ARM-3A,PLANT-2,approved,2026-08-18 08:10:00,2026-08-18 12:50:00,2026-08-18 12:55:00,TCP z=142.0 mm,TCP z=145.5 mm,Robotics Engineering,New finger set is 3.5 mm longer.
"""


def cell_changes() -> bytes:
    return CELL_CHANGES.encode()


CELL_REQUALIFICATION: Final = """\
Requal ID,Cell,Robot,Performed,Cause,Corrective Actions,Test 1,Result 1,Test 2,Result 2,Test 3,Result 3,Result,Decision,Decided By,Decided On,Work Order,Inspector
RQ-2026-004,CELL-3,ARM-3A,2026-03-10 20:15,Controller software update CHG0030012,Re-teach safe zones; Verify brake test,Joint brake test,PASS,Safety-rated speed monitoring 250 mm/s,PASS,Light curtain stop distance,212 mm,PASS,Returned to service,Cell owner,2026-03-10 21:00,WO-26-0310,K. Patel
RQ-2026-005,CELL-3,ARM-3A,2026-06-23 20:15,Joint 4 drive replacement WO-26-0623,Re-teach safe zones; Verify brake test,Joint brake test,PASS,Safety-rated speed monitoring 250 mm/s,PASS,Light curtain stop distance,212 mm,PASS,Returned to service,Cell owner,2026-06-23 21:00,WO-26-0623,K. Patel
RQ-2026-006,CELL-3,ARM-3A,2026-08-18 14:05,Gripper finger replacement CHG0030013,Re-calibrate TCP,TCP accuracy check,0.21 mm,Pick-and-place cycle 50x,PASS,,,PASS,Returned to service with speed restriction,Cell owner,,WO-26-0391,J. Meyer
"""


def cell_requalification() -> bytes:
    return CELL_REQUALIFICATION.encode()


def near_miss_export() -> bytes:
    return as_json(
        [
            {
                "key": "INC-C3-0004",
                "fields": {
                    "issuetype": {"name": "Incident"},
                    "summary": "Hand inside pallet gate while arm moving",
                    "priority": {"name": "near miss"},
                    "created": "2026-07-09T14:22:00.000-0400",
                    "environment": "Pallet gate, CELL-3",
                    "description": (
                        "Operator reached into the pallet gate while the light curtain was"
                        " muted for a pallet change. The arm was at reduced speed and stopped."
                    ),
                    "status": {"name": "Resolved"},
                    "labels": ["safety", "arm"],
                },
            },
            {
                "key": "OPS-0711",
                "fields": {
                    "issuetype": {"name": "Task"},
                    "summary": "Add floor marking at the pallet gate",
                    "priority": {"name": "Low"},
                    "created": "2026-07-10T09:00:00.000-0400",
                    "environment": None,
                    "description": "Mark the muting zone on the floor.",
                    "status": {"name": "Done"},
                    "labels": ["facilities"],
                },
            },
        ]
    )


def cell() -> dict[str, bytes]:
    files: dict[str, bytes] = {
        "neptune.yaml": NEPTUNE_YAML.encode(),
        "urdf/arm6.urdf": urdf_arm(),
        "calibration/CAL-ARM3A-0226.yaml": calibration(
            "CAL-ARM3A-0226",
            "2026-02-26T15:10:00-05:00",
            "commissioning",
            (0.032, -0.011, 0.071),
            0.42,
        ),
        "calibration/CAL-ARM3A-0415.yaml": calibration(
            "CAL-ARM3A-0415",
            "2026-04-15T10:05:00-04:00",
            "scheduled recalibration",
            (0.0321, -0.0108, 0.0712),
            0.39,
        ),
        "calibration/CAL-ARM3A-0623.yaml": calibration(
            "CAL-ARM3A-0623",
            "2026-06-23T17:30:00-04:00",
            "after joint 4 drive replacement",
            (0.0334, -0.0102, 0.0709),
            0.47,
        ),
        "calibration/CAL-ARM3A-0818.yaml": calibration(
            "CAL-ARM3A-0818",
            "2026-08-18T12:40:00-04:00",
            "after gripper finger set change",
            (0.0334, -0.0103, 0.0745),
            0.44,
        ),
        "cmms/work_orders.csv": cell_cmms(),
        "changes/servicenow_changes.csv": cell_changes(),
        "requalification/requalification_tests.csv": cell_requalification(),
        "tickets/near_miss_export.json": near_miss_export(),
        "documents/risk_assessment_CELL3-RA-009.pdf": pdf_risk(),
        "documents/commissioning_CR-C3-2026-02.pdf": pdf_commissioning(),
        "documents/sop_CELL-014_finger_set.pdf": pdf_sop(),
    }
    files.update(arm_bag())
    return files


def build() -> dict[str, bytes]:
    """Every source file, by path relative to ``sources/``."""
    files = {f"{FLEET}/{path}": data for path, data in fleet().items()}
    files.update({f"{CELL}/{path}": data for path, data in cell().items()})
    return files


# --- The pipeline: compiler ingest, then the Deploy mapper (ADR 0002) ----------------------------


@dataclass(frozen=True)
class Declared:
    """What an operator declares for one deployment: shipped presets, mapping files, templates."""

    presets: tuple[str, ...]
    mappings: tuple[Path, ...]
    templates: tuple[Path, ...]


PIPELINES: Final = {
    FLEET: Declared(
        presets=("cmms_generic", "servicenow_csv", "register_zone"),
        mappings=(DECLARED / FLEET / "requalification.json",),
        templates=(DECLARED / FLEET / "incident_report.json",),
    ),
    CELL: Declared(
        presets=("cmms_generic", "servicenow_csv", "jira_json"),
        mappings=(DECLARED / CELL / "requalification.json",),
        templates=(
            DOCUMENTS / "templates" / "risk_cell_arm.json",
            DOCUMENTS / "templates" / "commissioning_cell3.json",
            DECLARED / CELL / "sop_tool_change.json",
        ),
    ),
}


def ingest(root: Path, out: Path, scratch: Path) -> None:
    """``neptune ingest`` over ``root``, in process (no sandbox: the bytes are ours).

    It goes through the SDK the command line wraps, so the folder's ``neptune.yaml`` is applied.
    """
    from neptune.runtime import Isolation, JobOptions
    from neptune.sdk import Neptune

    options = JobOptions(isolation=Isolation.IN_PROCESS, job="archetype")
    Neptune(scratch / "home", options=options).ingest(root, out)


def lifecycle(base: Path, declared: Declared, out: Path) -> None:
    """The Deploy mapper over the base package, with the deployment's declared files."""
    from neptune_deploy.lifecycle import (
        TemplateRegistry,
        load_mapping,
        map_package,
        preset,
    )

    mappings = [preset(name) for name in declared.presets]
    mappings += [load_mapping(path) for path in declared.mappings]
    templates = list(TemplateRegistry.from_paths(declared.templates).templates())
    map_package(base, mappings, out, templates)


def package_documents(root: Path, *, whole: bool) -> dict[str, bytes]:
    """The package's files, without ``volatile/``. A base package is kept as its manifest and
    receipt; a mapped one whole, but for empty record tables its manifest lists with hashes."""
    keep = {"manifest.json", "receipt.json", "receipt.md"}
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if not path.is_file() or relative.startswith("volatile/"):
            continue
        if whole and path.stat().st_size == 0:
            continue
        if whole or relative in keep:
            files[relative] = path.read_bytes()
    return files


def pipeline(name: str, sources: Path, scratch: Path) -> tuple[Path, Path]:
    """Ingest ``sources/<name>`` and map it; the base package's and the mapped package's roots."""
    base, mapped = scratch / f"{name}.base", scratch / f"{name}.lifecycle"
    ingest(sources / name, base, scratch / name)
    lifecycle(base, PIPELINES[name], mapped)
    return base, mapped


def golden_files(name: str, base: Path, mapped: Path) -> dict[str, bytes]:
    """One deployment's golden files, by path relative to ``golden/``."""
    files = {
        f"{name}/base/{relative}": data
        for relative, data in package_documents(base, whole=False).items()
    }
    files.update(
        {
            f"{name}/lifecycle/{relative}": data
            for relative, data in package_documents(mapped, whole=True).items()
        }
    )
    return files


def golden() -> dict[str, bytes]:
    """The golden files, by path relative to ``golden/``."""
    files: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory() as scratch:
        for name in PIPELINES:
            files.update(golden_files(name, *pipeline(name, SOURCES, Path(scratch))))
    return files


def write_tree(root: Path, files: dict[str, bytes], clear: bool = True) -> None:
    if clear:
        shutil.rmtree(root, ignore_errors=True)
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def main(argv: Sequence[str] | None = None) -> int:
    write_tree(SOURCES, build())
    write_tree(GOLDEN, golden())
    sys.stdout.write(f"wrote {SOURCES.relative_to(ROOT)} and {GOLDEN.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

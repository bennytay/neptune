"""Build the four worked examples: their source files and the canonical records they declare.

Run ``uv run python tests/fixtures/model/make_examples.py`` to rewrite everything under
``tests/fixtures/model/<example>/``. The output is byte-for-byte deterministic, and
``tests/integration/test_worked_examples.py`` checks that the committed files are exactly what
``build()`` returns.

Each example plays the adapters that do not exist yet (M4 to M6) over its sources, by hand and by
the rules of ADRs 0017 to 0020: every record cites the bytes that declare it, clocks and frames
are records, and what a source does not say stays ``Unknown`` or ``NotCovered``. Series rows are
not written: a stream's samples are Parquet, which the store writes (MVL-5, MVL-16).
"""

import calendar
import io
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TypeVar

sys.path.insert(0, str(Path(__file__).parent))

from sources import (
    DOCK_EXIF,
    DRONE_START_US,
    DRONE_SYS_UUID,
    DRONE_VER_SW,
    EXAMPLES,
    HANDEYE,
    MS,
    QOS,
    T0,
    Source,
)

from neptune.identity import canonical_json
from neptune.identity.findings import ingest_finding
from neptune.identity.hashing import digest_stream
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.frames import (
    STATIC,
    EulerAngles,
    EulerMode,
    EulerSequence,
    FrameRef,
    Pose,
    Quaternion,
    QuaternionOrder,
    TransformDirection,
    Translation,
)
from neptune.model.ids import ContentId, LogicalId, RecordId
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Knowledge,
    Known,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.lifecycle import (
    AuthorisationEnvelope,
    ChangeItem,
    ChangeRecord,
    CommissioningBaseline,
    Decision,
    Hazard,
    IncidentRecord,
    Intervention,
    InventoryItem,
    MaintenanceEvent,
    PartReplacement,
    Quantity,
    RequalificationRecord,
    RiskAssessment,
    Score,
    TestResult,
    TimelineEntry,
    ZoneLimit,
)
from neptune.model.machine import (
    Calibration,
    CalibrationParameter,
    ComponentCategory,
    HardwareComponent,
    HardwareConfiguration,
    Machine,
    SoftwareConfiguration,
    SoftwareItem,
)
from neptune.model.provenance import (
    ByteRange,
    EvidenceRef,
    JsonPointer,
    Locator,
    Provenance,
    Row,
    RowCell,
    Span,
    TransformRecord,
    adapter_locator,
)
from neptune.model.reference import Frame, FrameGraph, FrameTransform, TimestampDomain
from neptune.model.run import Run, Stream
from neptune.model.series import SeriesProvenance, step_template
from neptune.model.source import LocalPath
from neptune.model.spatial import GeodeticPosition
from neptune.model.time import (
    MICROSECOND,
    NANOSECOND,
    SECOND,
    ClockRole,
    Epoch,
    Timescale,
    Timestamp,
)
from neptune.model.units import unit_from_json, unit_from_text
from neptune.model.versions import DeclaredVersion, GitCommit, VersionPrimitive
from neptune.model.world import (
    Capture,
    Image,
    Site,
    SpatialArtifact,
    SpatialCategory,
    StructuredRecord,
    StructuredTable,
)

if TYPE_CHECKING:
    from neptune.model.scalars import Real

HERE: Final = Path(__file__).parent
R = TypeVar("R")
OBSERVED, STATED = AssertionKind.OBSERVED, AssertionKind.STATED
ADAPTER_VERSION: Final = "1.0.0"


@dataclass
class Example:
    """One example's ledger and records, and the helpers that cite its sources exactly."""

    name: str
    sources: dict[str, Source]
    ledger: SourceLedger = field(default_factory=SourceLedger)
    transforms: dict[str, TransformRecord] = field(default_factory=dict)
    records: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        for source in self.sources.values():
            self.ledger.observe(LocalPath(source.path), digest_stream(io.BytesIO(source.data)))

    def transform(self, adapter: str) -> TransformRecord:
        if adapter not in self.transforms:
            self.transforms[adapter] = transform_record(
                adapter_id=adapter, adapter_version=ADAPTER_VERSION, config={}
            )
        return self.transforms[adapter]

    def content(self, path: str) -> ContentId:
        artifact = digest_stream(io.BytesIO(self.sources[path].data))
        return artifact.content_id

    def cite(
        self, adapter: str, path: str, *steps: Locator, kind: AssertionKind = OBSERVED
    ) -> Provenance:
        return Provenance(EvidenceRef(self.content(path), steps), self.transform(adapter).id, kind)

    def part(self, path: str, name: str) -> ByteRange:
        """The byte range of one named part of a source (``Source.layout``)."""
        return ByteRange(*self.sources[path].layout[name])

    def span(self, path: str, first: str, last: str) -> ByteRange:
        """The bytes from the start of part ``first`` to the end of part ``last``."""
        start, _ = self.sources[path].layout[first]
        end_start, end_length = self.sources[path].layout[last]
        return ByteRange(start, end_start + end_length - start)

    def whole(self, path: str) -> ByteRange:
        return ByteRange(0, len(self.sources[path].data))

    def at(
        self, adapter: str, path: str, name: str, *steps: Locator, kind: AssertionKind = OBSERVED
    ) -> Provenance:
        return self.cite(adapter, path, self.part(path, name), *steps, kind=kind)

    def pointer(
        self, adapter: str, path: str, pointer: str, kind: AssertionKind = OBSERVED
    ) -> Provenance:
        return self.cite(adapter, path, self.whole(path), JsonPointer(pointer), kind=kind)

    def id_of(self, kind: str, provenance: Provenance) -> RecordId:
        return evidence_record_id(
            kind, provenance.evidence, self.transforms[_adapter_of(self, provenance)]
        )

    def add(self, record: R) -> R:
        self.records.append(record)
        return record

    def finding(self, adapter: str, **fields: Any) -> IngestFinding:
        found: IngestFinding = ingest_finding(transform=self.transform(adapter), **fields)
        return self.add(found)

    def tables(self) -> dict[str, list[bytes]]:
        """Every table's lines, each line canonical JSON, sorted by id (ADR 0002 §1)."""
        rows: dict[str, list[tuple[str, bytes]]] = {}

        def put(kind: str, key: str, record: Any) -> None:
            rows.setdefault(kind, []).append((key, canonical_json.dumps(record.to_json())))

        for artifact in self.ledger.artifacts():
            put(artifact.kind, artifact.content_id, artifact)
        for revision in self.ledger.revisions():
            put(revision.kind, revision.id, revision)
        for transform in self.transforms.values():
            put(transform.kind, transform.id, transform)
        for record in self.records:
            put(record.kind, record.id, record)
        tables: dict[str, list[bytes]] = {}
        for kind, entries in sorted(rows.items()):
            keys = [key for key, _ in entries]
            if len(set(keys)) != len(keys):
                raise ValueError(f"{self.name}: two {kind} records share an id")
            tables[kind] = [line for _, line in sorted(entries)]
        return tables


def _adapter_of(example: Example, provenance: Provenance) -> str:
    for adapter, transform in example.transforms.items():
        if transform.id == provenance.transform:
            return adapter
    raise KeyError(provenance.transform)


def clock(
    example: Example,
    provenance: Provenance,
    field_name: str,
    scope: tuple[str, ...] = (),
    **known: Any,
) -> TimestampDomain:
    """A clock: ``known`` holds the properties the evidence declares; the rest are Unknown."""
    values: dict[str, Any] = {
        "role": Unknown(),
        "resolution": Unknown(),
        "epoch": Unknown(),
        "timescale": Unknown(),
        "declared_monotonic": Unknown(),
    }
    values.update(known)
    return example.add(
        TimestampDomain(
            id=example.id_of("timestamp_domain", provenance),
            provenance=provenance,
            field=field_name,
            scope=scope,
            **values,
        )
    )


def byte_rows(example: Example, path: str) -> SeriesProvenance:
    """Series rows that each cite their own record's bytes: offset and length per row."""
    template = step_template("byte_range", per_row=("length", "offset"))
    return SeriesProvenance(example.content(path), (template,), OBSERVED)


# --- Drone: a PX4 flight log -------------------------------------------------------------------


def drone() -> Example:
    ex = Example("drone", {source.path: source for source in EXAMPLES["drone"]()})
    log = "flight.ulg"

    def info(key: str) -> Provenance:
        return ex.at("ulog", log, f"info:{key}")

    def parameter(name: str) -> Provenance:
        return ex.at("ulog", log, f"parameter:{name}")

    header = ex.at("ulog", log, "header")
    # The ULog specification makes every `timestamp` field microseconds; the header states the
    # start on that clock. What tick zero is, PX4's documentation says, not the file: Unknown.
    boot = clock(ex, header, "timestamp", resolution=Known(MICROSECOND))
    sample = clock(
        ex,
        ex.at(
            "ulog",
            log,
            "format:sensor_accel",
            adapter_locator("ulog:field", {"name": "timestamp_sample"}),
        ),
        "timestamp_sample",
        ("sensor_accel",),
    )
    gps = clock(
        ex,
        ex.at(
            "ulog",
            log,
            "format:vehicle_gps_position",
            adapter_locator("ulog:field", {"name": "time_utc_usec"}),
        ),
        "time_utc_usec",
        ("vehicle_gps_position",),
    )
    uuid = LogicalId("px4.sys_uuid", DRONE_SYS_UUID)
    run = ex.add(
        Run(
            id=ex.id_of("run", header),
            provenance=header,
            logical_id=NotCovered(),  # a ULog has no field for a session id
            machine=Known(uuid, info("sys_uuid")),
            first=Known(Timestamp(DRONE_START_US, boot.id)),
            last=NotCovered(),  # the header states the start only
        )
    )
    streams = []
    for msg_id, topic, second_clock in (
        (0, "sensor_accel", sample),
        (1, "vehicle_gps_position", gps),
    ):
        declared = ex.at("ulog", log, f"subscription:{msg_id}")
        definition = ex.at("ulog", log, f"format:{topic}")
        streams.append(
            ex.add(
                Stream(
                    id=ex.id_of("stream", declared),
                    provenance=declared,
                    run=run.id,
                    topic=Known(topic),
                    schema_name=Known(topic, definition),
                    schema_encoding=Known("ulog", header),
                    schema_definition=Known(definition.evidence),
                    message_encoding=Known("ulog", header),
                    metadata=(("multi_id", "0"),),
                    clocks=(boot.id, second_clock.id),
                    message_count=NotCovered(),  # ULog has no index or summary
                    first=NotCovered(),
                    last=NotCovered(),
                    series=byte_rows(ex, log),
                )
            )
        )
    ex.add(
        Machine(
            id=ex.id_of("machine", info("sys_uuid")),
            provenance=info("sys_uuid"),
            identifiers=(Known(uuid),),
            manufacturer=NotCovered(),
            model=NotCovered(),
        )
    )
    hardware = ex.add(
        HardwareConfiguration(
            id=ex.id_of("hardware_configuration", info("ver_hw")),
            provenance=info("ver_hw"),
            machine=Known(uuid, info("sys_uuid")),
            name=NotCovered(),
            revision=Unknown(),  # ver_hw_subtype could say; this log does not
        )
    )
    ex.add(
        HardwareComponent(
            id=ex.id_of("hardware_component", info("ver_hw")),
            provenance=info("ver_hw"),
            configuration=hardware.id,
            category=ComponentCategory.COMPUTER,
            name=NotCovered(),
            model=Known("PX4_FMU_V5X"),
            identifiers=(),
            frame=NotCovered(),
        )
    )
    ex.add(
        HardwareComponent(
            id=ex.id_of("hardware_component", parameter("CAL_ACC0_ID")),
            provenance=parameter("CAL_ACC0_ID"),
            configuration=hardware.id,
            category=ComponentCategory.SENSOR,
            name=NotCovered(),
            model=NotCovered(),
            identifiers=(Known(LogicalId("px4.device_id", "1310988")),),
            frame=NotCovered(),
        )
    )
    infos = ex.cite("ulog", log, ex.span(log, "info:sys_name", "info:sys_uuid"))
    software = ex.add(
        SoftwareConfiguration(
            id=ex.id_of("software_configuration", info("ver_sw")),
            provenance=info("ver_sw"),
            machine=Known(uuid, info("sys_uuid")),
            software=(
                SoftwareItem(
                    name=Known("PX4", info("sys_name")),
                    device=Known("PX4_FMU_V5X", info("ver_hw")),
                    commit=Known(GitCommit(DRONE_VER_SW)),
                    release=Unknown(infos),  # the log could state ver_sw_release and does not
                    build=NotCovered(),
                    digest=NotCovered(),
                ),
            ),
        )
    )
    ex.finding(
        "ulog",
        code="ulog.software_identity_missing",
        category=FindingCategory.MISSING,
        severity=Severity.WARNING,
        subject=infos.evidence,
        message="the log states no ver_sw_release, so the PX4 release that ran is unknown",
        details={"key": "ver_sw_release"},
        records=[software.id],
    )
    ex.add(
        Calibration(
            id=ex.id_of("calibration", parameter("CAL_ACC0_ID")),
            provenance=parameter("CAL_ACC0_ID"),
            machine=Known(uuid, info("sys_uuid")),
            hardware_revision=NotCovered(),
            subject=Known("CAL_ACC0"),
            performed=NotCovered(),
            valid_from=NotCovered(),
            valid_until=NotCovered(),
            parameters=tuple(
                CalibrationParameter(
                    f"CAL_ACC0_{axis}OFF",
                    Known((offset,), parameter(f"CAL_ACC0_{axis}OFF")),
                    Unknown(parameter(f"CAL_ACC0_{axis}OFF")),  # a ULog carries no units
                )
                for axis, offset in (("X", 0.0625), ("Y", -0.03125), ("Z", 0.1875))
            ),
            extrinsics=(),
        )
    )
    dropout = ex.at("ulog", log, "dropout:3")
    ex.finding(
        "ulog",
        code="ulog.dropout",
        category=FindingCategory.MISSING,
        severity=Severity.WARNING,
        subject=dropout.evidence,
        message="the logger dropped 120 ms of data, so every series has a gap",
        details={"duration_ms": 120},
        records=[stream.id for stream in streams],
    )
    return ex


# --- ROS recordings: the clocks MCAP and ROS messages carry ------------------------------------


def mcap_clocks(ex: Example, adapter: str, path: str) -> tuple[TimestampDomain, TimestampDomain]:
    """MCAP's log and publish times: nanoseconds by its specification, epoch user-defined."""
    magic = ex.part(path, "header")

    def time_field(name: str) -> Provenance:
        return ex.cite(adapter, path, magic, adapter_locator("mcap:time_field", {"name": name}))

    log_time = clock(
        ex,
        time_field("log_time"),
        "log_time",
        role=Known(ClockRole.RECEIVE),
        resolution=Known(NANOSECOND),
    )
    publish_time = clock(
        ex,
        time_field("publish_time"),
        "publish_time",
        role=Known(ClockRole.PUBLISH),
        resolution=Known(NANOSECOND),
    )
    return log_time, publish_time


def header_stamp(ex: Example, adapter: str, path: str, schema: str, topic: str) -> TimestampDomain:
    """A ROS 2 message's header.stamp: sec + nanosec by its definition; its meaning undeclared."""
    field = adapter_locator("ros2msg:field", {"path": "header.stamp"})
    at = ex.cite(adapter, path, ex.part(path, schema), field)
    return clock(ex, at, "header.stamp", (topic,), resolution=Known(NANOSECOND))


def mcap_stream(
    ex: Example,
    adapter: str,
    path: str,
    run: Run,
    channel: int,
    schema: int,
    topic: str,
    type_name: str,
    clocks: tuple[RecordId, ...],
    count: Known[int],
    metadata: tuple[tuple[str, str], ...] = (),
) -> Stream:
    declared = ex.at(adapter, path, f"channel:{channel}")
    definition = ex.at(adapter, path, f"schema:{schema}")
    return ex.add(
        Stream(
            id=ex.id_of("stream", declared),
            provenance=declared,
            run=run.id,
            topic=Known(topic),
            schema_name=Known(type_name, definition),
            schema_encoding=Known("ros2msg", definition),
            schema_definition=Known(definition.evidence),
            message_encoding=Known("cdr"),
            metadata=metadata,
            clocks=clocks,
            message_count=count,
            first=Unknown(),  # per-channel extents need message indexes, which these files lack
            last=Unknown(),
            series=byte_rows(ex, path),
        )
    )


# --- Quadruped: a ROS 2 bag, its URDF and the mesh the URDF uses -------------------------------


def quadruped() -> Example:
    ex = Example("quadruped", {source.path: source for source in EXAMPLES["quadruped"]()})
    meta, bag, urdf = "bag/metadata.yaml", "bag/walk_0.mcap", "robot.urdf"
    info = "/rosbag2_bagfile_information"

    def yaml(pointer: str) -> Provenance:
        return ex.pointer("rosbag2", meta, info + pointer)

    started = clock(
        ex,
        yaml("/starting_time"),
        "starting_time",
        # The key says nanoseconds since an epoch; which epoch, the file does not say.
        resolution=Known(NANOSECOND, yaml("/starting_time/nanoseconds_since_epoch")),
    )
    log_time, publish_time = mcap_clocks(ex, "rosbag2", bag)
    joints_stamp = header_stamp(ex, "rosbag2", bag, "schema:1", "/joint_states")
    pose_stamp = header_stamp(ex, "rosbag2", bag, "schema:2", "/body_pose")
    run = ex.add(
        Run(
            id=ex.id_of("run", yaml("")),
            provenance=yaml(""),
            logical_id=NotCovered(),
            machine=NotCovered(),  # rosbag2 metadata has no field for the robot
            first=Known(Timestamp(T0, started.id), yaml("/starting_time")),
            # rosbag2 defines duration as last minus first message time: last = start + duration.
            # A value read from two fields cites the smallest part holding both (ADR 0023 §3).
            last=Known(Timestamp(T0 + 45 * MS, started.id), yaml("")),
        )
    )
    statistics = ex.at("rosbag2", bag, "statistics")
    for channel, schema, topic, type_name, stamp in (
        (1, 1, "/joint_states", "sensor_msgs/msg/JointState", joints_stamp),
        (2, 2, "/body_pose", "geometry_msgs/msg/PoseStamped", pose_stamp),
    ):
        mcap_stream(
            ex,
            "rosbag2",
            bag,
            run,
            channel,
            schema,
            topic,
            type_name,
            (log_time.id, publish_time.id, stamp.id),
            Known(3, statistics),
            (("offered_qos_profiles", QOS),),
        )
    ex.add(
        SoftwareConfiguration(
            id=ex.id_of("software_configuration", yaml("/ros_distro")),
            provenance=yaml("/ros_distro"),
            machine=NotCovered(),
            software=(
                SoftwareItem(
                    name=Known("ros"),
                    device=NotCovered(),
                    commit=NotApplicable(),  # a distribution is a release of many repositories
                    release=Known(DeclaredVersion("humble")),
                    build=NotCovered(),
                    digest=NotCovered(),
                ),
            ),
        )
    )

    # The URDF: one frame graph, a frame per link, a transform per joint (ADR 0015).
    robot = ex.at("urdf", urdf, "robot")
    graph = ex.add(FrameGraph(id=ex.id_of("frame_graph", robot), provenance=robot, scope=()))
    for link in ("base", "fl_thigh", "front_camera"):
        at = ex.at("urdf", urdf, f"link:{link}")
        ex.add(
            Frame(
                id=ex.id_of("frame", at),
                provenance=at,
                ref=FrameRef(link, graph.id),
                axes=Unknown(),  # REP-103 is convention, not a declaration (ADR 0007 §4)
                handedness=Unknown(),
            )
        )
    metres, radians = Known(unit_from_json("m"), robot), Known(unit_from_json("rad"), robot)
    for joint, child, xyz, rpy in (
        ("fl_hip", "fl_thigh", (0.19, 0.05, 0.0), (0.0, 0.0, 0.0)),
        ("front_camera_joint", "front_camera", (0.28, 0.0, 0.05), (0.0, 0.25, 0.0)),
    ):
        at = ex.at("urdf", urdf, f"joint:{joint}")
        ex.add(
            FrameTransform(
                id=ex.id_of("frame_transform", at),
                provenance=at,
                parent=FrameRef("base", graph.id),
                child=FrameRef(child, graph.id),
                # The URDF specification: an origin is the child frame's pose in the parent,
                # xyz in metres, rpy fixed-axis roll, pitch, yaw in radians.
                direction=Known(TransformDirection.CHILD_TO_PARENT, robot),
                value=Pose(
                    Translation(xyz, metres),
                    EulerAngles(
                        rpy,
                        sequence=Known(EulerSequence.XYZ, robot),
                        mode=Known(EulerMode.EXTRINSIC, robot),
                        unit=radians,
                    ),
                ),
                validity=STATIC,
            )
        )
    hardware = ex.add(
        HardwareConfiguration(
            id=ex.id_of("hardware_configuration", robot),
            provenance=robot,
            machine=NotCovered(),  # a URDF describes a model, never a machine (ADR 0019 §3)
            name=Known("quadruped"),
            revision=NotCovered(),
        )
    )
    for category, name, part, frame in (
        (ComponentCategory.LINK, "base", "link:base", "base"),
        (ComponentCategory.LINK, "fl_thigh", "link:fl_thigh", "fl_thigh"),
        (ComponentCategory.LINK, "front_camera", "link:front_camera", "front_camera"),
        (ComponentCategory.JOINT, "fl_hip", "joint:fl_hip", "fl_thigh"),
        (ComponentCategory.JOINT, "front_camera_joint", "joint:front_camera_joint", "front_camera"),
        (ComponentCategory.SENSOR, "front_camera", "sensor:front_camera", "front_camera"),
    ):
        at = ex.at("urdf", urdf, part)
        ex.add(
            HardwareComponent(
                id=ex.id_of("hardware_component", at),
                provenance=at,
                configuration=hardware.id,
                category=category,
                name=Known(name),
                model=NotCovered(),
                identifiers=(),
                frame=Known(FrameRef(frame, graph.id)),
            )
        )
    mesh = ex.cite("stl", "meshes/body.stl", ex.whole("meshes/body.stl"))
    ex.add(
        SpatialArtifact(
            id=ex.id_of("spatial_artifact", mesh),
            provenance=mesh,
            category=SpatialCategory.MESH,
            name=Known("body"),
            unit=NotCovered(),  # STL has no place for a unit
            crs=NotCovered(),
            frame=NotCovered(),
        )
    )
    return ex


# --- Manipulator: an MCAP recording and a hand-eye calibration ---------------------------------


def manipulator() -> Example:
    ex = Example("manipulator", {source.path: source for source in EXAMPLES["manipulator"]()})
    log, handeye = "session.mcap", "handeye.yaml"
    log_time, publish_time = mcap_clocks(ex, "mcap", log)
    joints_stamp = header_stamp(ex, "mcap", log, "schema:1", "/joint_states")
    camera_stamp = header_stamp(ex, "mcap", log, "schema:2", "/wrist_camera/image/compressed")
    header, statistics = ex.at("mcap", log, "header"), ex.at("mcap", log, "statistics")
    run = ex.add(
        Run(
            id=ex.id_of("run", header),
            provenance=header,
            logical_id=Unknown(),  # an MCAP metadata record could name the session; none does
            machine=Unknown(),
            first=Known(Timestamp(T0 + 1_000 * MS, log_time.id), statistics),
            last=Known(Timestamp(T0 + 1_020 * MS, log_time.id), statistics),
        )
    )
    mcap_stream(
        ex,
        "mcap",
        log,
        run,
        1,
        1,
        "/joint_states",
        "sensor_msgs/msg/JointState",
        (log_time.id, publish_time.id, joints_stamp.id),
        Known(2, statistics),
    )
    # Images inside a log are a stream of samples (ADR 0018), not Image records.
    mcap_stream(
        ex,
        "mcap",
        log,
        run,
        2,
        2,
        "/wrist_camera/image/compressed",
        "sensor_msgs/msg/CompressedImage",
        (log_time.id, publish_time.id, camera_stamp.id),
        Known(1, statistics),
    )

    def yaml(pointer: str) -> Provenance:
        return ex.pointer("handeye", handeye, pointer)

    whole = ex.cite("handeye", handeye, ex.whole(handeye))
    graph = ex.add(FrameGraph(id=ex.id_of("frame_graph", whole), provenance=whole, scope=()))
    for key in (
        "robot_base_frame",
        "robot_effector_frame",
        "tracking_base_frame",
        "tracking_marker_frame",
    ):
        at = yaml(f"/{key}")
        ex.add(
            Frame(
                id=ex.id_of("frame", at),
                provenance=at,
                ref=FrameRef(str(HANDEYE[key]), graph.id),
                axes=Unknown(),
                handedness=Unknown(),
            )
        )
    moved = yaml("/transformation")
    transformation = HANDEYE["transformation"]
    assert isinstance(transformation, dict)
    # The quaternion keeps the file's key order, qw first; the key names declare the order.
    keys = ("qw", "qx", "qy", "qz")
    transform = ex.add(
        FrameTransform(
            id=ex.id_of("frame_transform", moved),
            provenance=moved,
            parent=FrameRef("tool0", graph.id),
            child=FrameRef("wrist_camera", graph.id),
            # The file names both frames and not which way its numbers map.
            direction=Ambiguous(
                (
                    Candidate(TransformDirection.CHILD_TO_PARENT),
                    Candidate(TransformDirection.PARENT_TO_CHILD),
                )
            ),
            value=Pose(
                Translation(
                    tuple(float(transformation[axis]) for axis in ("x", "y", "z")),
                    Unknown(),  # no unit is stated; see the finding
                ),
                Quaternion(
                    tuple(float(transformation[key]) for key in keys),
                    order=Known(QuaternionOrder.WXYZ),
                    convention=Unknown(),  # Hamilton or JPL: the file does not say
                ),
            ),
            validity=STATIC,
        )
    )
    ex.finding(
        "handeye",
        code="handeye.unit_undeclared",
        category=FindingCategory.MISSING,
        severity=Severity.WARNING,
        subject=moved.evidence,
        message="the transformation states no unit for x, y and z",
        records=[transform.id],
    )
    ex.add(
        Calibration(
            id=ex.id_of("calibration", whole),
            provenance=whole,
            machine=Unknown(),
            hardware_revision=Unknown(),
            subject=Unknown(),  # a hand-eye result calibrates a pair of frames, not one thing
            performed=Unknown(),
            valid_from=Unknown(),
            valid_until=Unknown(),
            parameters=(
                CalibrationParameter(
                    "eye_on_hand", Known("true", yaml("/eye_on_hand")), NotApplicable()
                ),
            ),
            extrinsics=(transform.id,),
        )
    )
    cell_records(ex)
    return ex


# --- Deployment records (ADR 0051) -------------------------------------------------------------


class Records:
    """Cites one deployment-records export: each value its own JSON pointer, all ``stated``.

    The adapter it stands in for is Neptune Deploy's (MVL-112 on); it reads every value as
    written, so a severity stays its text and a number keeps its unit.
    """

    ADAPTER: Final = "deployment_json"

    def __init__(self, ex: Example, path: str) -> None:
        self.ex, self.path = ex, path
        self.document = json.loads(ex.sources[path].data)
        # Every date-time in the export states its offset, so its ticks are the POSIX seconds of
        # the instant it names (ADR 0023 §2); the export's own root is what declares that.
        self.clock = clock(
            ex,
            self.cite(""),
            "date-time",
            role=Known(ClockRole.DOCUMENT),
            resolution=Known(SECOND),
            epoch=Known(Epoch.UNIX),
            timescale=Known(Timescale.POSIX),
        )

    def cite(self, pointer: str) -> Provenance:
        return self.ex.pointer(self.ADAPTER, self.path, pointer, kind=STATED)

    def get(self, pointer: str) -> Any:
        value = self.document
        for token in pointer.split("/")[1:]:
            value = value[int(token)] if isinstance(value, list) else value[token]
        return value

    def text(self, pointer: str) -> Known[str]:
        return Known(str(self.get(pointer)), self.cite(pointer))

    def texts(self, pointer: str) -> tuple[Known[str], ...]:
        return tuple(self.text(f"{pointer}/{i}") for i in range(len(self.get(pointer))))

    def ref(self, namespace: str, pointer: str) -> Known[LogicalId]:
        return Known(LogicalId(namespace, self.get(pointer)), self.cite(pointer))

    def refs(self, *named: tuple[str, str]) -> tuple[Known[LogicalId], ...]:
        """Declared ids: ``(namespace, pointer)`` to a value, or to a list of them."""
        found = []
        for namespace, pointer in named:
            value = self.get(pointer)
            if isinstance(value, list):
                found += [self.ref(namespace, f"{pointer}/{i}") for i in range(len(value))]
            else:
                found.append(self.ref(namespace, pointer))
        return tuple(sorted(found, key=lambda known: (known.value.namespace, known.value.value)))

    def time(self, pointer: str) -> Known[Timestamp]:
        instant = datetime.fromisoformat(self.get(pointer))
        ticks = calendar.timegm(instant.utctimetuple())
        return Known(Timestamp(ticks, self.clock.id), self.cite(pointer))

    def quantity(self, value: str, unit: str) -> Quantity:
        # A declared integer is read with float(), as calibration parameters are (ADR 0019 §6).
        number: Knowledge[Real] = Known(float(self.get(value)), self.cite(value))
        return Quantity(number, unit_from_text(self.get(unit), provenance=self.cite(unit)))

    def decision(self, decision: str, authority: str, time: str) -> Decision:
        return Decision(self.text(decision), self.text(authority), self.time(time))

    def tests(self, pointer: str) -> tuple[TestResult, ...]:
        return tuple(
            TestResult(
                self.text(f"{pointer}/{i}/test"),
                self.text(f"{pointer}/{i}/result"),
                self.time(f"{pointer}/{i}/date"),
            )
            for i in range(len(self.get(pointer)))
        )

    def inventory(self, pointer: str) -> tuple[InventoryItem, ...]:
        items = []
        for i, item in enumerate(self.get(pointer)):
            at = f"{pointer}/{i}"
            version: Knowledge[VersionPrimitive] = (
                # A version is the kind the source names; a form names none (ADR 0014).
                Known(DeclaredVersion(item[key]), self.cite(f"{at}/{key}"))
                if (key := "version" if "version" in item else "revision") in item
                else NotCovered()
            )
            items.append(
                InventoryItem(
                    name=self.text(f"{at}/item"),
                    model=self.text(f"{at}/model") if "model" in item else NotCovered(),
                    identifiers=self.refs(("serial", f"{at}/serial")) if "serial" in item else (),
                    version=version,
                )
            )
        return tuple(items)

    def hazards(self, pointer: str) -> tuple[Hazard, ...]:
        hazards = []
        for i, hazard in enumerate(self.get(pointer)):
            at = f"{pointer}/{i}"
            # Every other column is a score, named by its column, in the form's order.
            scores = tuple(
                Score(name, self.text(f"{at}/{name}"))
                for name in hazard
                if name not in {"hazard", "mitigations"}
            )
            hazards.append(
                Hazard(self.text(f"{at}/hazard"), scores, self.texts(f"{at}/mitigations"))
            )
        return tuple(hazards)

    def add(self, cls: Any, pointer: str, **values: Any) -> Any:
        provenance = self.cite(pointer)
        return self.ex.add(
            cls(id=self.ex.id_of(cls.kind, provenance), provenance=provenance, **values)
        )


def warehouse_records(ex: Example) -> None:
    """The warehouse deployment of AMR-07 at site S-007, from its records export."""
    r = Records(ex, "deployment/records.json")
    site = r.ref("register", "/site")  # the site register's id (sites.csv)
    c = "/commissioning/0"
    r.add(
        CommissioningBaseline,
        c,
        identifiers=r.refs(("siteops.form", f"{c}/form")),
        site=site,
        machines=r.refs(("fleet", f"{c}/machine")),
        configuration=r.ref("siteops.configuration", f"{c}/configuration"),
        related=(),
        commissioned=r.time(f"{c}/commissioned"),
        hardware=r.inventory(f"{c}/hardware"),
        software=r.inventory(f"{c}/software"),
        calibrations=r.refs(("siteops.calibration", f"{c}/calibrations")),
        tests=r.tests(f"{c}/tests"),
        constraints=r.texts(f"{c}/constraints"),
        sign_off=r.decision(f"{c}/acceptance", f"{c}/signed_off_by", f"{c}/signed_off"),
    )
    a = "/authorisations/0"
    zones = tuple(
        ZoneLimit(
            r.ref("siteops.zone", f"{a}/zones/{i}/zone"),
            r.quantity(f"{a}/zones/{i}/speed_limit", f"{a}/zones/{i}/unit"),
        )
        for i in range(len(r.get(f"{a}/zones")))
    )
    r.add(
        AuthorisationEnvelope,
        a,
        identifiers=r.refs(("siteops.form", f"{a}/authorisation")),
        site=site,
        machines=r.refs(("fleet", f"{a}/machine")),
        configuration=r.ref("siteops.configuration", f"{a}/configuration"),
        related=r.refs(("siteops.form", f"{a}/commissioning")),
        missions=r.texts(f"{a}/missions"),
        payload_min=r.quantity(f"{a}/payload/min", f"{a}/payload/unit"),
        payload_max=r.quantity(f"{a}/payload/max", f"{a}/payload/unit"),
        zones=zones,
        supervision=r.text(f"{a}/supervision"),
        dependencies=r.texts(f"{a}/dependencies"),
        valid_from=r.time(f"{a}/valid_from"),
        valid_until=r.time(f"{a}/valid_until"),
        approval=r.decision(f"{a}/decision", f"{a}/approved_by", f"{a}/approved"),
    )
    n = "/interventions/0"
    r.add(
        Intervention,
        n,
        identifiers=r.refs(("siteops.ticket", f"{n}/ticket")),
        site=site,
        machines=r.refs(("fleet", f"{n}/machine")),
        configuration=NotCovered(),  # a ticket has no place for one
        related=(),
        mode=r.text(f"{n}/mode"),
        authority=r.text(f"{n}/authority"),
        reason=r.text(f"{n}/reason"),
        commands=r.texts(f"{n}/commands"),
        start=r.time(f"{n}/start"),
        end=r.time(f"{n}/end"),
        outcome=r.text(f"{n}/outcome"),
    )
    i = "/incidents/0"
    r.add(
        IncidentRecord,
        i,
        identifiers=r.refs(("siteops.incident", f"{i}/incident")),
        site=site,
        machines=r.refs(("fleet", f"{i}/machines")),
        configuration=NotCovered(),
        # The evidence it links, by the names it gives them: a video id and a log's file name.
        related=r.refs(("siteops.evidence", f"{i}/evidence")),
        occurred=r.time(f"{i}/occurred"),
        severity=r.text(f"{i}/severity"),  # "S3", as the site's scale writes it; never ranked
        zone=r.ref("siteops.zone", f"{i}/zone"),
        location=r.text(f"{i}/location"),
        assets=r.refs(("siteops.asset", f"{i}/assets")),
        timeline=tuple(
            TimelineEntry(r.time(f"{i}/timeline/{k}/time"), r.text(f"{i}/timeline/{k}/entry"))
            for k in range(len(r.get(f"{i}/timeline")))
        ),
        description=r.text(f"{i}/description"),
        root_cause=r.text(f"{i}/root_cause"),
    )
    g = "/changes/0"
    r.add(
        ChangeRecord,
        g,
        identifiers=r.refs(("siteops.change", f"{g}/change")),
        site=site,
        machines=r.refs(("fleet", f"{g}/machines")),
        configuration=r.ref("siteops.configuration", f"{g}/configuration"),
        related=r.refs(("siteops.incident", f"{g}/incident")),
        changes=tuple(
            ChangeItem(
                r.text(f"{g}/items/{k}/type"),
                r.text(f"{g}/items/{k}/target"),
                r.text(f"{g}/items/{k}/from"),
                r.text(f"{g}/items/{k}/to"),
            )
            for k in range(len(r.get(f"{g}/items")))
        ),
        approval=r.decision(f"{g}/decision", f"{g}/approved_by", f"{g}/approved"),
        effective=r.time(f"{g}/effective"),
        rollback=r.ref("siteops.release", f"{g}/rollback"),
    )
    k = "/risk_assessments/0"
    r.add(
        RiskAssessment,
        k,
        identifiers=r.refs(("siteops.form", f"{k}/assessment")),
        site=site,
        machines=r.refs(("fleet", f"{k}/machines")),
        configuration=r.ref("siteops.configuration", f"{k}/configuration"),
        related=(),
        assessed=r.time(f"{k}/assessed"),
        method=r.text(f"{k}/method"),
        hazards=r.hazards(f"{k}/hazards"),
        approval=r.decision(f"{k}/decision", f"{k}/approved_by", f"{k}/approved"),
    )


def cell_records(ex: Example) -> None:
    """The manipulator cell CELL-3: commissioning, risk, a repair and its requalification."""
    r = Records(ex, "cell/records.json")
    site = r.ref("plant.cell", "/cell")
    c = "/commissioning"
    r.add(
        CommissioningBaseline,
        c,
        identifiers=r.refs(("plant.record", f"{c}/record")),
        site=site,
        machines=r.refs(("robot.serial", f"{c}/robot")),
        configuration=r.ref("plant.configuration", f"{c}/configuration"),
        related=(),
        commissioned=r.time(f"{c}/date"),
        hardware=r.inventory(f"{c}/hardware"),
        software=r.inventory(f"{c}/software"),
        calibrations=r.refs(("plant.calibration", f"{c}/calibrations")),
        tests=r.tests(f"{c}/tests"),
        constraints=r.texts(f"{c}/constraints"),
        sign_off=r.decision(f"{c}/acceptance", f"{c}/signed_off_by", f"{c}/signed_off"),
    )
    k = "/risk_assessment"
    r.add(
        RiskAssessment,
        k,
        identifiers=r.refs(("plant.record", f"{k}/record")),
        site=site,
        machines=r.refs(("robot.serial", f"{k}/robot")),
        configuration=r.ref("plant.configuration", f"{k}/configuration"),
        related=(),
        assessed=r.time(f"{k}/date"),
        method=r.text(f"{k}/method"),
        hazards=r.hazards(f"{k}/hazards"),
        approval=r.decision(f"{k}/decision", f"{k}/approved_by", f"{k}/approved"),
    )
    m = "/maintenance"
    r.add(
        MaintenanceEvent,
        m,
        identifiers=r.refs(("cmms.work_order", f"{m}/work_order")),
        site=site,
        machines=r.refs(("robot.serial", f"{m}/robot")),
        # The as-maintained configuration the work order states resulted.
        configuration=r.ref("plant.configuration", f"{m}/as_maintained_configuration"),
        related=(),
        performed=r.time(f"{m}/date"),
        diagnosis=r.text(f"{m}/diagnosis"),
        actions=r.texts(f"{m}/actions"),
        parts=tuple(
            PartReplacement(
                r.text(f"{m}/parts/{p}/part"),
                r.refs(("serial", f"{m}/parts/{p}/removed")),
                r.refs(("serial", f"{m}/parts/{p}/installed")),
            )
            for p in range(len(r.get(f"{m}/parts")))
        ),
    )
    q = "/requalification"
    r.add(
        RequalificationRecord,
        q,
        identifiers=r.refs(("plant.record", f"{q}/record")),
        site=site,
        machines=r.refs(("robot.serial", f"{q}/robot")),
        configuration=r.ref("plant.configuration", f"{q}/configuration"),
        related=r.refs(("cmms.work_order", f"{q}/work_order")),
        performed=r.time(f"{q}/date"),
        cause=r.text(f"{q}/cause"),
        corrective_actions=r.texts(f"{q}/corrective_actions"),
        tests=r.tests(f"{q}/tests"),
        result=r.text(f"{q}/result"),
        return_to_service=r.decision(f"{q}/return_to_service", f"{q}/returned_by", f"{q}/returned"),
    )


# --- Mobile robot: a ROS 1 bag, a site register and a photo ------------------------------------


def mobile_robot() -> Example:
    ex = Example("mobile_robot", {source.path: source for source in EXAMPLES["mobile_robot"]()})
    bag, register, photo = "drive.bag", "sites.csv", "photos/dock.png"
    magic = ex.part(bag, "magic")
    # The bag format defines a record's time as the time it was received, as sec + nsec.
    record_time = clock(
        ex,
        ex.cite("rosbag1", bag, magic, adapter_locator("rosbag1:time_field", {"name": "time"})),
        "time",
        role=Known(ClockRole.RECEIVE),
        resolution=Known(NANOSECOND),
    )
    odom_stamp = clock(
        ex,
        ex.at(
            "rosbag1",
            bag,
            "connection:0",
            adapter_locator("ros1msg:field", {"path": "header.stamp"}),
        ),
        "header.stamp",
        ("/wheel_odom",),
        resolution=Known(NANOSECOND),
    )
    header, chunk_info = ex.at("rosbag1", bag, "bag_header"), ex.at("rosbag1", bag, "chunk_info")
    run = ex.add(
        Run(
            id=ex.id_of("run", header),
            provenance=header,
            logical_id=NotCovered(),
            machine=NotCovered(),
            first=Known(Timestamp(T0 + 3_600_000 * MS, record_time.id), chunk_info),
            last=Known(Timestamp(T0 + 3_600_200 * MS, record_time.id), chunk_info),
        )
    )
    for connection, topic, type_name, clocks, count in (
        (0, "/wheel_odom", "husky_examples/WheelOdom", (record_time.id, odom_stamp.id), 3),
        (1, "/battery", "std_msgs/Float32", (record_time.id,), 1),
    ):
        declared = ex.at("rosbag1", bag, f"connection:{connection}")
        start, length = ex.sources[bag].layout[f"connection:{connection}"]
        record = ex.sources[bag].data[start : start + length]
        at = record.index(b"md5sum=") + len(b"md5sum=")
        md5 = record[at : at + 32].decode()  # the connection header's other field, verbatim
        ex.add(
            Stream(
                id=ex.id_of("stream", declared),
                provenance=declared,
                run=run.id,
                topic=Known(topic),
                schema_name=Known(type_name),
                schema_encoding=Known("ros1msg"),
                schema_definition=Known(declared.evidence),
                message_encoding=Known("ros1"),
                metadata=(("md5sum", md5),),
                clocks=clocks,
                message_count=Known(count, chunk_info),
                first=Unknown(),
                last=Unknown(),
                series=byte_rows(ex, bag),
            )
        )

    # The register: a table, its rows cell by cell, and the sites the rows name (ADR 0020).
    table_at = ex.cite("csv", register, ex.whole(register), kind=STATED)
    rows = ex.sources[register].data.decode().splitlines()
    cells = [row.split(",") for row in rows]
    header_names = tuple(cells[0])
    table = ex.add(
        StructuredTable(
            id=ex.id_of("structured_table", table_at),
            provenance=table_at,
            name=NotCovered(),
            header=Known(header_names, ex.cite("csv", register, Row(0), kind=STATED)),
        )
    )
    for index in range(1, len(cells)):
        row_at = ex.cite("csv", register, Row(index), kind=STATED)
        ex.add(
            StructuredRecord(
                id=ex.id_of("structured_record", row_at),
                provenance=row_at,
                table=table.id,
                row=index,
                cells=tuple(Known(text) if text else Unknown() for text in cells[index]),
            )
        )

        def cell(column: int, *steps: Locator, row: int = index) -> Provenance:
            return ex.cite(
                "csv", register, RowCell(row, column, header_names[column]), *steps, kind=STATED
            )

        site_id, name, aka, latitude, longitude, _ = cells[index]
        aliases: list[Known[str]] = []
        start = 0
        for alias in aka.split(";") if aka else ():
            aliases.append(Known(alias, cell(2, Span(start, start + len(alias)))))
            start += len(alias) + 1
        aliases.sort(key=lambda known: known.value)
        ex.add(
            Site(
                id=ex.id_of("site", row_at),
                provenance=row_at,
                identifiers=(Known(LogicalId("register", site_id), cell(0)),),
                name=Known(name, cell(1)),
                aliases=tuple(aliases),
                parent=NotCovered(),
                # Two cells make one position, so it cites their row.
                location=Known(
                    GeodeticPosition(
                        latitude=float(latitude),
                        longitude=float(longitude),
                        height=NotCovered(),
                        crs=Unknown(),  # the register names no datum
                        angle_unit=Unknown(),
                        height_unit=NotApplicable(),
                        height_reference=NotApplicable(),
                    )
                ),
            )
        )

    # The photo: pixels, and what its EXIF block declares about its capture.
    exif_at = ex.at("png", photo, "eXIf")

    def tag(number: int) -> Provenance:
        return ex.cite(
            "png", photo, ex.part(photo, "eXIf"), adapter_locator("exif:tag", {"tag": number})
        )

    taken = clock(
        ex,
        tag(0x9003),
        "DateTimeOriginal",
        # EXIF defines it as when the image was captured, to the second, as civil date and time
        # with no zone. Ticks count its civil seconds from 1970-01-01T00:00:00 (Epoch.UNIX on the
        # camera's own timescale), which the zone-less tag leaves Unknown.
        role=Known(ClockRole.SAMPLE),
        resolution=Known(SECOND),
        epoch=Known(Epoch.UNIX),
    )

    def civil_seconds(text: str) -> int:
        """EXIF's "YYYY:MM:DD HH:MM:SS", counted as POSIX counts (ADR 0023 §2)."""
        date, time = text.split(" ")
        fields = [int(part) for part in (*date.split(":"), *time.split(":"))]
        return calendar.timegm((*fields, 0, 0, 0))

    def degrees(dms: tuple[tuple[int, int], ...], ref: str, negative: str) -> float:
        value = sum(Fraction(n, d) / 60**i for i, (n, d) in enumerate(dms))
        return float(-value if ref == negative else value)

    whole_photo = ex.cite("png", photo, ex.whole(photo))
    ex.add(
        Image(
            id=ex.id_of("image", whole_photo),
            provenance=whole_photo,
            width=8,
            height=6,
            encoding="png",
            orientation=Unknown(exif_at),  # EXIF could state it; this file does not
            capture=Capture(
                time=Known(Timestamp(civil_seconds(DOCK_EXIF.taken), taken.id), tag(0x9003)),
                position=Known(
                    GeodeticPosition(
                        # EXIF states degrees, minutes and seconds and a hemisphere; the adapter
                        # reads them per the EXIF specification into signed degrees.
                        latitude=degrees(DOCK_EXIF.latitude[1], DOCK_EXIF.latitude[0], "S"),
                        longitude=degrees(DOCK_EXIF.longitude[1], DOCK_EXIF.longitude[0], "W"),
                        height=Unknown(),
                        crs=Unknown(),  # no GPSMapDatum
                        angle_unit=Known(unit_from_json("deg"), tag(0x0002)),
                        height_unit=Unknown(),
                        height_reference=Unknown(),
                    ),
                    tag(0x0002),
                ),
                device_manufacturer=Known(DOCK_EXIF.make, tag(0x010F)),
                device_model=Known(DOCK_EXIF.model, tag(0x0110)),
                device_identifiers=(
                    Known(LogicalId("exif.body_serial", DOCK_EXIF.body_serial), tag(0xA431)),
                ),
            ),
        )
    )
    warehouse_records(ex)
    return ex


EXAMPLE_BUILDERS: Final[dict[str, Callable[[], Example]]] = {
    "drone": drone,
    "quadruped": quadruped,
    "manipulator": manipulator,
    "mobile_robot": mobile_robot,
}


def build() -> dict[str, bytes]:
    """Every file under tests/fixtures/model/, by path relative to it."""
    files: dict[str, bytes] = {}
    for name, builder in EXAMPLE_BUILDERS.items():
        example = builder()
        for source in example.sources.values():
            files[f"{name}/sources/{source.path}"] = source.data
        for kind, lines in example.tables().items():
            files[f"{name}/records/{kind}.jsonl"] = b"".join(line + b"\n" for line in lines)
    return files


def write() -> None:
    for relative, data in build().items():
        path = HERE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


if __name__ == "__main__":
    write()

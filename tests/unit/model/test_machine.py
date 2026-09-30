"""Machine-context records: what each field may hold, and the acceptance of MVL-68 (ADR 0019).

The four platforms at the end build the machine context a drone, a quadruped, a manipulator and a
mobile robot declare, from the sources each typically has, and check every record round-trips.
"""

from dataclasses import replace
from typing import Any

import pytest

from neptune.derived.provenance import InferredProvenance
from neptune.identity import canonical_json
from neptune.identity.findings import ingest_finding
from neptune.identity.hashing import content_id
from neptune.identity.provenance import (
    check_evidence_record_id,
    evidence_record_id,
    transform_record,
)
from neptune.model.finding import FindingCategory, Severity
from neptune.model.frames import (
    STATIC,
    FrameRef,
    HomogeneousMatrix,
    MatrixLayout,
    TransformDirection,
)
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
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
    calibration_from_json,
    hardware_component_from_json,
    hardware_configuration_from_json,
    machine_from_json,
    software_configuration_from_json,
)
from neptune.model.provenance import (
    ByteRange,
    EvidenceRef,
    JsonPointer,
    Locator,
    Provenance,
    adapter_locator,
)
from neptune.model.record import SCHEMA_VERSION, Family, SchemaVersionError
from neptune.model.reference import FrameGraph, FrameTransform, frame_transform_from_json
from neptune.model.scalars import NonFinite, Real
from neptune.model.time import Timestamp
from neptune.model.units import unit_from_json
from neptune.model.versions import (
    BuildId,
    ContainerImageDigest,
    DeclaredVersion,
    FirmwareVersion,
    GitCommit,
    HashAlgorithm,
    ModelCheckpointHash,
    SemanticVersion,
)

MANIFEST_BYTES = b'{"robots":[{"id":"spot-07","serial":"BD-10470012","model":"Spot"}]}'
URDF_BYTES = b'<robot name="spot"><link name="body"/><joint name="front_left_hip_x"/></robot>'
ULOG_BYTES = b"ULog\x01\x12\x35\x01" + bytes(512)
CAMCHAIN_BYTES = (
    b"cam0:\n  camera_model: pinhole\n  intrinsics: [458.654, 457.296, 367.215, 248.375]\n"
)
HANDEYE_BYTES = b"eye_on_hand: true\ntransformation: {x: 0.03, y: -0.01, z: 0.07}\n"
SOURCES = {
    name: content_id(data)
    for name, data in (
        ("manifest", MANIFEST_BYTES),
        ("urdf", URDF_BYTES),
        ("ulog", ULOG_BYTES),
        ("kalibr", CAMCHAIN_BYTES),
        ("handeye", HANDEYE_BYTES),
    )
}
ADAPTERS = {
    name: transform_record(adapter_id=name, adapter_version="1.0.0", config={}) for name in SOURCES
}
TRANSFORMS = {transform.id: transform for transform in ADAPTERS.values()}
SOURCE_OF = {ADAPTERS[name].id: source for name, source in SOURCES.items()}
OBSERVED, STATED = AssertionKind.OBSERVED, AssertionKind.STATED
M = Known(unit_from_json("m"))


def cite(adapter: str, *steps: Locator, kind: AssertionKind = OBSERVED) -> Provenance:
    transform = ADAPTERS[adapter]
    return Provenance(EvidenceRef(SOURCE_OF[transform.id], steps), transform.id, kind)


def pointer(adapter: str, path: str, kind: AssertionKind = OBSERVED) -> Provenance:
    """A value inside a JSON or YAML source, located by an RFC 6901 pointer."""
    return cite(adapter, ByteRange(0, 4096), JsonPointer(path), kind=kind)


def record_id_of(kind: str, provenance: Provenance) -> RecordId:
    return evidence_record_id(kind, provenance.evidence, TRANSFORMS[provenance.transform])


def ulog_info(key: str) -> Provenance:
    """One ULog information message, e.g. ``ver_sw``, by its key."""
    return cite("ulog", ByteRange(16, 400), adapter_locator("ulog:info", {"key": key}))


def ulog_parameter(name: str) -> Provenance:
    return cite("ulog", ByteRange(16, 400), adapter_locator("ulog:parameter", {"name": name}))


def ident(namespace: str, value: str, at: Provenance | None = None) -> Known[LogicalId]:
    """A declared tier-3 id, citing where it is stated (else the record's own provenance)."""
    if at is None:
        return Known(LogicalId(namespace, value))
    return Known(LogicalId(namespace, value), at)


SPOT_07 = LogicalId("manifest", "spot-07")
SYS_UUID = LogicalId("px4.sys_uuid", "000200000000343233345117003a0027")
URDF_AT = cite("urdf", ByteRange(0, len(URDF_BYTES)))
URDF_GRAPH = record_id_of("frame_graph", URDF_AT)
HANDEYE_AT = cite("handeye", ByteRange(0, len(HANDEYE_BYTES)))
HANDEYE_GRAPH = record_id_of("frame_graph", HANDEYE_AT)
# A date written in a document: its own clock, whatever the ticks mean (ADR 0012).
CALIBRATED_ON = record_id_of("timestamp_domain", pointer("kalibr", "/cam0/date"))


# --- One record of each kind -------------------------------------------------------------------


def manifest_machine(**changes: Any) -> Machine:
    """A manifest states the robot of a fleet by its id and its serial: one machine, two ids."""
    at = pointer("manifest", "/robots/0", STATED)
    machine = Machine(
        id=record_id_of("machine", at),
        provenance=at,
        identifiers=(
            ident("manifest", "spot-07", pointer("manifest", "/robots/0/id", STATED)),
            ident("serial", "BD-10470012", pointer("manifest", "/robots/0/serial", STATED)),
        ),
        manufacturer=Unknown(),
        model=Known("Spot", pointer("manifest", "/robots/0/model", STATED)),
    )
    return replace(machine, **changes)


def ulog_machine() -> Machine:
    """A flight log observes the vehicle that recorded it by its autopilot's hardware UUID."""
    at = ulog_info("sys_uuid")
    return Machine(
        id=record_id_of("machine", at),
        provenance=at,
        identifiers=(Known(SYS_UUID),),
        manufacturer=NotCovered(),  # a ULog has no field for it
        model=NotCovered(),
    )


def urdf_configuration(**changes: Any) -> HardwareConfiguration:
    configuration = HardwareConfiguration(
        id=record_id_of("hardware_configuration", URDF_AT),
        provenance=URDF_AT,
        machine=NotCovered(),  # a URDF describes a model many robots share; it names no machine
        name=Known("spot"),
        revision=NotCovered(),
    )
    return replace(configuration, **changes)


def urdf_component(
    category: ComponentCategory, part: str, at: tuple[int, int], /, **changes: Any
) -> HardwareComponent:
    declared = cite("urdf", ByteRange(*at))
    component = HardwareComponent(
        id=record_id_of("hardware_component", declared),
        provenance=declared,
        configuration=urdf_configuration().id,
        category=category,
        name=Known(part),
        model=NotCovered(),
        identifiers=(),
        frame=Known(FrameRef(part, URDF_GRAPH)),
    )
    return replace(component, **changes)


def px4_item(**changes: Any) -> SoftwareItem:
    item = SoftwareItem(
        name=Known("PX4", ulog_info("sys_name")),
        device=Known("PX4_FMU_V5X", ulog_info("ver_hw")),
        commit=Known(GitCommit("2a7d3f1ce8b5f0a9d61c2e7b4f8a3d5c6e9b1f07"), ulog_info("ver_sw")),
        release=Known(FirmwareVersion("v1.14.0"), ulog_info("ver_sw_release")),
        build=NotCovered(),  # a ULog has no field for a CI build id
        digest=NotCovered(),
    )
    return replace(item, **changes)


def ulog_software(*items: SoftwareItem) -> SoftwareConfiguration:
    at = cite("ulog", ByteRange(16, 400))  # the log's information section
    return SoftwareConfiguration(
        id=record_id_of("software_configuration", at),
        provenance=at,
        machine=Known(SYS_UUID, ulog_info("sys_uuid")),
        software=items or (px4_item(),),
    )


def parameter(name: str, *values: Real, unit: Any = None) -> CalibrationParameter:
    return CalibrationParameter(name, Known(values), Unknown() if unit is None else unit)


def setting(name: str, text: str) -> CalibrationParameter:
    return CalibrationParameter(name, Known(text), NotApplicable())


def kalibr_camera(**changes: Any) -> Calibration:
    """One camera of a Kalibr camchain: intrinsics as declared, for one hardware revision."""
    at = pointer("kalibr", "/cam0")
    calibration = Calibration(
        id=record_id_of("calibration", at),
        provenance=at,
        machine=Known(SPOT_07, pointer("kalibr", "/cam0/robot")),
        hardware_revision=Known(DeclaredVersion("rev-C"), pointer("kalibr", "/cam0/hardware")),
        subject=Known("cam0"),
        performed=Known(Timestamp(20_359, CALIBRATED_ON)),  # days since 1970-01-01, as declared
        valid_from=Unknown(),
        valid_until=Unknown(),
        parameters=(
            setting("camera_model", "pinhole"),
            parameter("distortion_coeffs", -0.28340811, 0.07395907, 0.00019359, 1.76187114e-05),
            setting("distortion_model", "radtan"),
            parameter("intrinsics", 458.654, 457.296, 367.215, 248.375),
            parameter("resolution", 752.0, 480.0),
            setting("rostopic", "/cam0/image_raw"),
        ),
        extrinsics=(),
    )
    return replace(calibration, **changes)


def hand_eye_transform() -> FrameTransform:
    """The file names both frames and not which way its numbers map: direction is Ambiguous."""
    at = pointer("handeye", "/transformation")
    return FrameTransform(
        id=record_id_of("frame_transform", at),
        provenance=at,
        parent=FrameRef("tool0", HANDEYE_GRAPH),
        child=FrameRef("camera_link", HANDEYE_GRAPH),
        direction=Ambiguous(
            (
                Candidate(TransformDirection.CHILD_TO_PARENT),
                Candidate(TransformDirection.PARENT_TO_CHILD),
            )
        ),
        value=HomogeneousMatrix(
            (1.0, 0.0, 0.0, 0.03, 0.0, 1.0, 0.0, -0.01, 0.0, 0.0, 1.0, 0.07, 0.0, 0.0, 0.0, 1.0),
            layout=Known(MatrixLayout.ROW_MAJOR),
            translation_unit=M,
        ),
        validity=STATIC,
    )


def hand_eye(**changes: Any) -> Calibration:
    calibration = Calibration(
        id=record_id_of("calibration", HANDEYE_AT),
        provenance=HANDEYE_AT,
        machine=Unknown(),
        hardware_revision=Unknown(),
        subject=Unknown(),  # the file calibrates a pair of frames, not one named thing
        performed=Unknown(),
        valid_from=Unknown(),
        valid_until=Unknown(),
        parameters=(setting("eye_on_hand", "true"),),
        extrinsics=(hand_eye_transform().id,),
    )
    return replace(calibration, **changes)


RECORDS: list[tuple[Any, Any]] = [
    (manifest_machine(), machine_from_json),
    (ulog_machine(), machine_from_json),
    (urdf_configuration(), hardware_configuration_from_json),
    (
        urdf_component(ComponentCategory.JOINT, "front_left_hip_x", (40, 32)),
        hardware_component_from_json,
    ),
    (ulog_software(), software_configuration_from_json),
    (kalibr_camera(), calibration_from_json),
    (hand_eye(), calibration_from_json),
]
IDS = [
    "manifest machine",
    "ulog machine",
    "urdf configuration",
    "joint",
    "px4 software",
    "kalibr camera",
    "hand-eye",
]


# --- What makes them records -------------------------------------------------------------------


@pytest.mark.parametrize(("record", "read"), RECORDS, ids=IDS)
def test_machine_records_round_trip_byte_identically(record: Any, read: Any) -> None:
    line = canonical_json.dumps(record.to_json())
    assert read(canonical_json.loads(line)) == record
    assert canonical_json.dumps(read(canonical_json.loads(line)).to_json()) == line
    data = canonical_json.loads(line)
    assert isinstance(data, dict)
    assert (data["kind"], data["schema_version"]) == (record.kind, SCHEMA_VERSION)
    assert record.family is Family.MACHINE
    check_evidence_record_id(record, TRANSFORMS[record.provenance.transform])


@pytest.mark.parametrize(("record", "read"), RECORDS, ids=IDS)
def test_their_json_is_read_strictly(record: Any, read: Any) -> None:
    data = record.to_json()
    with pytest.raises(SchemaVersionError, match="newer"):
        read({**data, "schema_version": SCHEMA_VERSION + 1, "added_later": 1})
    first = sorted(key for key in data if key not in {"id", "kind", "provenance", "schema_version"})
    for broken in (
        {**data, "confidence": 0.9},
        {key: value for key, value in data.items() if key != first[0]},
        {**data, "kind": "stream"},
        {**data, first[0]: None},
    ):
        with pytest.raises(ValueError):
            read(broken)


# --- Machine: declared identifiers, never inferred ---------------------------------------------


def test_a_machine_is_what_its_declaration_names_it() -> None:
    machine = manifest_machine()
    assert machine.provenance.assertion_kind is STATED
    assert [known.known_or_raise() for known in machine.identifiers] == [
        SPOT_07,
        LogicalId("serial", "BD-10470012"),
    ]
    # Each id cites where it is stated, so identity resolution (MVL-35) can weigh it.
    assert machine.identifiers[1] == Known(
        LogicalId("serial", "BD-10470012"), pointer("manifest", "/robots/0/serial", STATED)
    )
    assert ulog_machine().provenance.assertion_kind is OBSERVED


def test_the_same_serial_in_two_declarations_is_two_machine_records() -> None:
    # Equal ids are evidence for MVL-35 to weigh; the model merges nothing.
    at = pointer("manifest", "/robots/1", STATED)
    other = manifest_machine(id=record_id_of("machine", at), provenance=at)
    assert other.identifiers == manifest_machine().identifiers
    assert other.id != manifest_machine().id


def test_a_urdf_names_a_model_not_a_machine() -> None:
    configuration = urdf_configuration()
    assert configuration.name == Known("spot")
    assert configuration.machine == NotCovered()


def test_conflicting_readings_of_one_id_are_ambiguous() -> None:
    serials = Ambiguous(
        (
            Candidate(LogicalId("serial", "BD-10470012"), pointer("manifest", "/robots/0/serial")),
            Candidate(LogicalId("serial", "BD-10470021"), pointer("manifest", "/robots/0/label")),
        )
    )
    machine = manifest_machine(identifiers=(manifest_machine().identifiers[0], serials))
    assert machine_from_json(machine.to_json()) == machine


@pytest.mark.parametrize(
    ("identifiers", "error"),
    [
        ((), ValueError),  # a declaration with no id declares no Machine
        ((Unknown(),), ValueError),  # the list holds what is stated, never a gap
        ((KnownAbsent(pointer("manifest", "/robots/0/id")),), ValueError),
        ((Known("spot-07"),), ValueError),  # an id is (namespace, value), not a name
        ((ident("serial", "BD-1"), ident("manifest", "spot-07")), ValueError),  # unsorted
        ((ident("serial", "BD-1"), ident("serial", "BD-1", pointer("manifest", "/x"))), ValueError),
        ([ident("serial", "BD-1")], TypeError),
    ],
)
def test_machine_identifiers_are_checked(identifiers: Any, error: type) -> None:
    with pytest.raises(error):
        manifest_machine(identifiers=identifiers)


@pytest.mark.parametrize(
    "change",
    [
        {"manufacturer": Known("")},  # a blank is Unknown, never a value
        {"model": Known(7)},
        {"provenance": InferredProvenance((URDF_AT.evidence,), ADAPTERS["urdf"].id)},
        {"id": SOURCES["manifest"]},
    ],
)
def test_machine_fields_are_typed(change: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        manifest_machine(**change)


# --- Hardware: snapshots and their parts -------------------------------------------------------


def manipulator_with(tool: str, model: str, entry: int) -> tuple[HardwareConfiguration, Any]:
    """One manifest entry per tool set: the configuration and its tool."""
    at = pointer("manifest", f"/hardware/{entry}", STATED)
    configuration = HardwareConfiguration(
        id=record_id_of("hardware_configuration", at),
        provenance=at,
        machine=Known(LogicalId("manifest", "ur5e-cell-2")),
        name=Known(f"ur5e with {tool}"),
        revision=Known(DeclaredVersion(tool)),
    )
    tool_at = pointer("manifest", f"/hardware/{entry}/tool", STATED)
    component = HardwareComponent(
        id=record_id_of("hardware_component", tool_at),
        provenance=tool_at,
        configuration=configuration.id,
        category=ComponentCategory.TOOL,
        name=Known(tool),
        model=Known(model),
        identifiers=(),
        frame=Unknown(),  # the manifest names no frame for it
    )
    return configuration, component


def test_a_tool_change_is_a_new_hardware_configuration() -> None:
    gripper, gripper_tool = manipulator_with("gripper", "Robotiq 2F-85", 0)
    vacuum, vacuum_tool = manipulator_with("vacuum", "Schmalz FXCB", 1)
    assert gripper.id != vacuum.id
    assert gripper.machine == vacuum.machine  # one machine, two declared snapshots of it
    assert (gripper_tool.configuration, vacuum_tool.configuration) == (gripper.id, vacuum.id)
    for record, read in (
        (gripper, hardware_configuration_from_json),
        (vacuum, hardware_configuration_from_json),
        (gripper_tool, hardware_component_from_json),
        (vacuum_tool, hardware_component_from_json),
    ):
        assert read(record.to_json()) == record
        check_evidence_record_id(record, ADAPTERS["manifest"])


def joint(**changes: Any) -> HardwareComponent:
    return urdf_component(ComponentCategory.JOINT, "front_left_hip_x", (40, 32), **changes)


def test_components_carry_their_frames() -> None:
    joint = urdf_component(ComponentCategory.JOINT, "front_left_hip_x", (40, 32))
    link = urdf_component(ComponentCategory.LINK, "body", (19, 20))
    assert link.frame == Known(FrameRef("body", URDF_GRAPH))
    assert joint.configuration == link.configuration == urdf_configuration().id
    assert joint.id != link.id  # each part cites its own element


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"category": "joint"}, TypeError),  # the enum, not its text
        ({"configuration": "base"}, ValueError),
        ({"name": Known("")}, ValueError),
        ({"frame": Known("front_left_hip_x")}, ValueError),  # a frame lives in a graph
        ({"identifiers": (Unknown(),)}, ValueError),
        ({"identifiers": (ident("px4.device_id", "1310988"),)}, None),
        ({"model": Known("Unitree A1 motor")}, None),
    ],
)
def test_component_fields_are_typed(change: dict[str, Any], error: type | None) -> None:
    if error is None:
        component = joint(**change)
        assert hardware_component_from_json(component.to_json()) == component
        return
    with pytest.raises(error):
        joint(**change)


def test_component_categories_are_read_strictly() -> None:
    data = urdf_component(ComponentCategory.LINK, "body", (19, 20)).to_json()
    with pytest.raises(ValueError):
        hardware_component_from_json({**data, "category": "wheel"})


@pytest.mark.parametrize(
    "revision",
    [Known(SemanticVersion("3.0.0")), Known("rev-C"), Known(FirmwareVersion("rev-C"))],
)
def test_a_hardware_revision_is_declared_text(revision: Any) -> None:
    # Declared text on both sides, so a calibration's revision can match a configuration's.
    with pytest.raises(ValueError):
        urdf_configuration(revision=revision)


# --- Software: every identity in its own field, never blank ------------------------------------


def test_software_items_keep_each_kind_of_identity_apart() -> None:
    policy = SoftwareItem(
        name=Known("grasp-policy"),
        device=Unknown(),
        commit=Known(GitCommit("9fceb02d0ae598e95dc970b74767f19372d61af8")),
        release=Known(DeclaredVersion("v3")),
        build=Known(BuildId("build-4812")),
        digest=Known(ModelCheckpointHash(HashAlgorithm.SHA256, "ab" * 32)),
    )
    image = replace(
        policy,
        name=Known("perception"),
        release=Known(SemanticVersion("2.4.1")),
        digest=Known(ContainerImageDigest("sha256:" + "cd" * 32)),
    )
    configuration = ulog_software(px4_item(), policy, image)
    assert software_configuration_from_json(configuration.to_json()) == configuration


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"release": Known(GitCommit("2a7d3f1"))}, ValueError),  # a commit is not a release
        ({"release": Known("v1.14.0")}, ValueError),  # declared text is a DeclaredVersion
        ({"digest": Known(BuildId("build-1"))}, ValueError),
        ({"commit": Known(DeclaredVersion("2a7d3f1"))}, ValueError),
        ({"build": Known(GitCommit("2a7d3f1"))}, ValueError),
        ({"name": Known("")}, ValueError),  # never blank
        ({"device": Known(1)}, ValueError),
    ],
)
def test_software_item_fields_are_typed(change: dict[str, Any], error: type) -> None:
    with pytest.raises(error):
        px4_item(**change)


def test_a_release_of_the_wrong_kind_is_refused_when_read() -> None:
    data = canonical_json.loads(canonical_json.dumps(ulog_software().to_json()))
    assert isinstance(data, dict)
    item = dict(data["software"][0])
    item["release"] = {"knowledge": "known", "value": {"kind": "git_commit", "sha": "2a7d3f1"}}
    with pytest.raises(ValueError, match="release cannot be a git_commit"):
        software_configuration_from_json({**data, "software": [item]})


def test_missing_software_identity_is_unknown_plus_a_finding() -> None:
    # A PX4 log whose information section has no ver_sw: the adapter looked there and found none.
    looked = cite("ulog", ByteRange(16, 400))
    item = px4_item(commit=Unknown(looked), release=Unknown(looked))
    configuration = ulog_software(item)
    finding = ingest_finding(
        code="ulog.software_identity_missing",
        category=FindingCategory.MISSING,
        severity=Severity.WARNING,
        subject=looked.evidence,
        transform=ADAPTERS["ulog"],
        message="the log states no ver_sw or ver_sw_release, so what ran is unidentified",
        records=[configuration.id],
    )
    data = configuration.to_json()
    software = data["software"]
    assert isinstance(software, list)
    assert [software[0][key]["knowledge"] for key in ("commit", "release")] == [
        "unknown",
        "unknown",
    ]
    assert finding.records == (configuration.id,)
    assert software_configuration_from_json(data) == configuration


@pytest.mark.parametrize(
    ("software", "error"),
    [
        ((), ValueError),  # a declaration of software names something
        ((px4_item(), px4_item()), ValueError),
        (["PX4"], TypeError),
        ([px4_item()], TypeError),
    ],
)
def test_a_software_configuration_lists_distinct_items(software: Any, error: type) -> None:
    with pytest.raises(error):
        replace(ulog_software(), software=software)


# --- Calibration: what, for which hardware, when -----------------------------------------------


def test_a_calibration_can_apply_to_one_hardware_revision_only() -> None:
    camera = kalibr_camera()
    assert camera.hardware_revision == Known(
        DeclaredVersion("rev-C"), pointer("kalibr", "/cam0/hardware")
    )
    assert camera.machine.known_or_raise() == SPOT_07
    # The same camera calibrated for another revision is another record, beside this one.
    at = pointer("kalibr", "/cam1")
    other = kalibr_camera(
        id=record_id_of("calibration", at),
        provenance=at,
        hardware_revision=Known(DeclaredVersion("rev-D")),
    )
    assert other.id != camera.id


def test_calibration_parameters_are_kept_as_declared() -> None:
    camera = kalibr_camera()
    data = camera.to_json()
    parameters = data["parameters"]
    assert isinstance(parameters, list)
    by_name = {p["name"]: p for p in parameters}
    assert by_name["intrinsics"]["value"] == {
        "knowledge": "known",
        "value": [458.654, 457.296, 367.215, 248.375],
    }
    assert by_name["camera_model"]["unit"] == {"knowledge": "not_applicable"}


def test_non_finite_parameters_survive_canonical_json() -> None:
    unset = parameter("CAL_ACC0_XSCALE", 1.0, NonFinite.NAN)
    limit = parameter(
        "MPC_XY_VEL_MAX", NonFinite.POSITIVE_INFINITY, unit=Known(unit_from_json("m.s^-1"))
    )
    camera = kalibr_camera(parameters=(unset, limit))
    line = canonical_json.dumps(camera.to_json())
    assert b'{"non_finite":"nan"}' in line
    assert calibration_from_json(canonical_json.loads(line)) == camera


@pytest.mark.parametrize(
    ("build", "error"),
    [
        (lambda: parameter("resolution", 752, 480), TypeError),  # ints: read them with float()
        (lambda: parameter("fx", float("nan")), ValueError),  # write NonFinite.NAN
        (lambda: CalibrationParameter("rostopic", Known("/cam0"), M), ValueError),  # text: no unit
        (lambda: CalibrationParameter("", Known((1.0,)), Unknown()), ValueError),
        (lambda: CalibrationParameter("rostopic", Known(""), NotApplicable()), ValueError),
        (lambda: CalibrationParameter("fx", Known([1.0]), Unknown()), ValueError),  # type: ignore[arg-type]
        (lambda: CalibrationParameter("fx", Known((1.0,)), Known("px")), ValueError),  # type: ignore[arg-type]
        (lambda: CalibrationParameter("fx", Unknown(), Unknown()), None),  # a blank value
        (lambda: CalibrationParameter("fx", Known(()), Unknown()), None),  # an empty list
    ],
)
def test_calibration_parameters_are_typed(build: Any, error: type | None) -> None:
    if error is None:
        value = build()
        assert calibration_from_json(kalibr_camera(parameters=(value,)).to_json()).parameters == (
            value,
        )
        return
    with pytest.raises(error):
        build()


def test_a_hand_eye_calibration_lists_its_extrinsics() -> None:
    transform = hand_eye_transform()
    assert frame_transform_from_json(transform.to_json()) == transform
    assert hand_eye().extrinsics == (transform.id,)
    assert transform.parent.frame_graph_id == HANDEYE_GRAPH
    FrameGraph(id=HANDEYE_GRAPH, provenance=HANDEYE_AT, scope=())
    check_evidence_record_id(transform, ADAPTERS["handeye"])


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"parameters": (), "extrinsics": ()}, ValueError),  # it must state something
        ({"parameters": tuple(reversed(kalibr_camera().parameters))}, ValueError),
        ({"parameters": (setting("a", "x"), setting("a", "y"))}, ValueError),
        ({"parameters": [setting("a", "x")]}, TypeError),
        ({"extrinsics": ("T_cam_imu",)}, ValueError),
        ({"extrinsics": (hand_eye_transform().id,) * 2}, ValueError),
        ({"hardware_revision": Known(SemanticVersion("3.0.0"))}, ValueError),
        ({"performed": Known(20_359)}, ValueError),  # ticks mean nothing without their clock
        ({"subject": Known("")}, ValueError),
        ({"machine": Known("spot-07")}, ValueError),
    ],
)
def test_calibration_fields_are_typed(change: dict[str, Any], error: type) -> None:
    with pytest.raises(error):
        kalibr_camera(**change)


# --- Four platforms (acceptance) ---------------------------------------------------------------


def component(
    configuration: HardwareConfiguration,
    category: ComponentCategory,
    name: str,
    at: Provenance,
    *,
    model: Any = None,
    identifiers: Any = (),
    frame: Any = None,
) -> HardwareComponent:
    """A part as a log or a manifest declares it; ``model`` and ``frame`` Unknown unless given."""
    return HardwareComponent(
        id=record_id_of("hardware_component", at),
        provenance=at,
        configuration=configuration.id,
        category=category,
        name=Known(name),
        model=Unknown() if model is None else Known(model),
        identifiers=identifiers,
        frame=Unknown() if frame is None else Known(frame),
    )


def drone() -> list[Any]:
    """PX4: the log observes the vehicle, its board, sensors, firmware and sensor calibration."""
    at = cite("ulog", ByteRange(16, 400))
    hardware = HardwareConfiguration(
        id=record_id_of("hardware_configuration", at),
        provenance=at,
        machine=Known(SYS_UUID, ulog_info("sys_uuid")),
        name=NotCovered(),
        revision=Known(DeclaredVersion("V5X00"), ulog_info("ver_hw_subtype")),
    )
    parts = [
        component(
            hardware,
            ComponentCategory.COMPUTER,
            "fmu",
            ulog_info("ver_hw"),
            model="PX4_FMU_V5X",
        ),
        component(
            hardware,
            ComponentCategory.SENSOR,
            "accel0",
            ulog_parameter("CAL_ACC0_ID"),
            identifiers=(Known(LogicalId("px4.device_id", "1310988")),),
        ),
        component(
            hardware,
            ComponentCategory.ACTUATOR,
            "motor1",
            ulog_parameter("PWM_MAIN_FUNC1"),
        ),
    ]
    calibration = Calibration(
        id=record_id_of("calibration", ulog_parameter("CAL_ACC0_ID")),
        provenance=ulog_parameter("CAL_ACC0_ID"),
        machine=Known(SYS_UUID, ulog_info("sys_uuid")),
        hardware_revision=NotCovered(),
        subject=Known("CAL_ACC0"),
        performed=NotCovered(),
        valid_from=NotCovered(),
        valid_until=NotCovered(),
        parameters=tuple(
            CalibrationParameter(
                f"CAL_ACC0_{axis}OFF",
                Known((offset,), ulog_parameter(f"CAL_ACC0_{axis}OFF")),
                Known(unit_from_json("m.s^-2"), ulog_parameter(f"CAL_ACC0_{axis}OFF")),
            )
            for axis, offset in (("X", 0.0412), ("Y", -0.0133), ("Z", 0.2071))
        ),
        extrinsics=(),
    )
    nuttx = SoftwareItem(
        name=Known("NuttX", ulog_info("sys_os_name")),
        device=Known("PX4_FMU_V5X", ulog_info("ver_hw")),
        commit=Known(
            GitCommit("d3b9c5e0f1a2b3c4d5e6f708192a3b4c5d6e7f80"), ulog_info("sys_os_ver")
        ),
        release=NotCovered(),
        build=NotCovered(),
        digest=NotCovered(),
    )
    return [ulog_machine(), hardware, *parts, ulog_software(px4_item(), nuttx), calibration]


def quadruped() -> list[Any]:
    """A manifest names the robot; its URDF describes the model; a camera_info file calibrates."""
    parts = [
        urdf_component(ComponentCategory.LINK, "body", (19, 20)),
        urdf_component(ComponentCategory.JOINT, "front_left_hip_x", (40, 32)),
        urdf_component(ComponentCategory.SENSOR, "frontleft_fisheye", (72, 1)),
    ]
    software_at = pointer("manifest", "/robots/0/software", STATED)
    software = SoftwareConfiguration(
        id=record_id_of("software_configuration", software_at),
        provenance=software_at,
        machine=Known(SPOT_07),
        software=(
            SoftwareItem(
                name=Known("ros"),
                device=Unknown(),
                commit=NotApplicable(),
                release=Known(DeclaredVersion("humble")),
                build=Unknown(),
                digest=Unknown(),
            ),
            SoftwareItem(
                name=Known("spot_ros2"),
                device=Unknown(),
                commit=Known(GitCommit("5e1f0c3")),  # an abbreviation is still a commit
                release=Unknown(),
                build=Unknown(),
                digest=Unknown(),
            ),
        ),
    )
    camera = kalibr_camera(
        subject=Known("frontleft_fisheye"),
        parameters=(
            parameter("camera_matrix/data", 331.3, 0.0, 320.0, 0.0, 331.3, 240.0, 0.0, 0.0, 1.0),
            parameter("distortion_coefficients/data", -0.013, 0.021, -0.012, 0.003),
            setting("distortion_model", "equidistant"),
        ),
    )
    return [manifest_machine(), urdf_configuration(), *parts, software, camera]


def manipulator() -> list[Any]:
    """Two tool sets of one arm, the software that drove it, and a hand-eye calibration."""
    gripper, gripper_tool = manipulator_with("gripper", "Robotiq 2F-85", 0)
    vacuum, vacuum_tool = manipulator_with("vacuum", "Schmalz FXCB", 1)
    machine_at = pointer("manifest", "/robots/1", STATED)
    machine = Machine(
        id=record_id_of("machine", machine_at),
        provenance=machine_at,
        identifiers=(ident("manifest", "ur5e-cell-2"), ident("serial", "20185500123")),
        manufacturer=Known("Universal Robots"),
        model=Known("UR5e"),
    )
    software_at = pointer("manifest", "/robots/1/software", STATED)
    software = SoftwareConfiguration(
        id=record_id_of("software_configuration", software_at),
        provenance=software_at,
        machine=Known(LogicalId("manifest", "ur5e-cell-2")),
        software=(
            SoftwareItem(
                name=Known("PolyScope"),
                device=Known("control box"),
                commit=NotCovered(),
                release=Known(FirmwareVersion("5.12.2")),
                build=Unknown(),
                digest=NotApplicable(),
            ),
            SoftwareItem(
                name=Known("grasp-policy"),
                device=Known("workcell pc"),
                commit=Unknown(),
                release=Known(DeclaredVersion("v3")),
                build=Unknown(),
                digest=Known(ModelCheckpointHash(HashAlgorithm.SHA256, "ab" * 32)),
            ),
            SoftwareItem(
                name=Known("perception"),
                device=Known("workcell pc"),
                commit=Unknown(),
                release=Unknown(),
                build=Unknown(),
                digest=Known(ContainerImageDigest("sha256:" + "cd" * 32)),
            ),
        ),
    )
    # The hand-eye calibration was done with the gripper mounted: it applies to that set only.
    calibration = hand_eye(
        machine=Known(LogicalId("manifest", "ur5e-cell-2")),
        hardware_revision=Known(DeclaredVersion("gripper")),
    )
    return [
        machine,
        gripper,
        vacuum,
        gripper_tool,
        vacuum_tool,
        software,
        calibration,
        hand_eye_transform(),
    ]


def mobile_robot() -> list[Any]:
    """A fleet manifest's robot with its sensors, compute, battery and per-device firmware."""
    machine_at = pointer("manifest", "/robots/2", STATED)
    machine = Machine(
        id=record_id_of("machine", machine_at),
        provenance=machine_at,
        identifiers=(ident("serial", "A200-0417"),),
        manufacturer=Known("Clearpath Robotics"),
        model=Known("Husky A200"),
    )
    hardware_at = pointer("manifest", "/robots/2/hardware", STATED)
    hardware = HardwareConfiguration(
        id=record_id_of("hardware_configuration", hardware_at),
        provenance=hardware_at,
        machine=Known(LogicalId("serial", "A200-0417")),
        name=Unknown(),
        revision=Unknown(),
    )
    parts = [
        component(
            hardware,
            category,
            name,
            pointer("manifest", f"/robots/2/hardware/{i}", STATED),
            model=model,
        )
        for i, (category, name, model) in enumerate(
            [
                (ComponentCategory.SENSOR, "front_lidar", "Velodyne VLP-16"),
                (ComponentCategory.SENSOR, "imu", "Microstrain 3DM-GX5-25"),
                (ComponentCategory.COMPUTER, "mini-itx", None),
                (ComponentCategory.POWER, "battery", "24V 20Ah AGM"),
                (ComponentCategory.PAYLOAD, "sensor mast", None),
            ]
        )
    ]
    firmware = SoftwareConfiguration(
        id=record_id_of(
            "software_configuration", pointer("manifest", "/robots/2/software", STATED)
        ),
        provenance=pointer("manifest", "/robots/2/software", STATED),
        machine=Known(LogicalId("serial", "A200-0417")),
        software=(
            SoftwareItem(
                name=Known("ros"),
                device=Known("mini-itx"),
                commit=NotApplicable(),
                release=Known(DeclaredVersion("noetic")),
                build=Unknown(),
                digest=Unknown(),
            ),
            SoftwareItem(
                name=Known("mcu firmware"),
                device=Known("mcu"),
                commit=Unknown(),
                release=Known(FirmwareVersion("0.4.3")),
                build=Unknown(),
                digest=NotApplicable(),
            ),
        ),
    )
    return [machine, hardware, *parts, firmware, kalibr_camera(machine=Unknown())]


READERS = {
    "machine": machine_from_json,
    "hardware_configuration": hardware_configuration_from_json,
    "hardware_component": hardware_component_from_json,
    "software_configuration": software_configuration_from_json,
    "calibration": calibration_from_json,
    "frame_transform": frame_transform_from_json,
}


@pytest.mark.parametrize("platform", [drone, quadruped, manipulator, mobile_robot])
def test_the_machine_context_of_each_platform_is_representable(platform: Any) -> None:
    records = platform()
    kinds = {record.kind for record in records}
    assert {
        "machine",
        "hardware_configuration",
        "hardware_component",
        "software_configuration",
        "calibration",
    } <= kinds
    ids = [record.id for record in records]
    assert len(set(ids)) == len(ids)  # every record cites its own evidence
    for record in records:
        line = canonical_json.dumps(record.to_json())
        assert READERS[record.kind](canonical_json.loads(line)) == record
        check_evidence_record_id(record, TRANSFORMS[record.provenance.transform])
    configurations = {r.id for r in records if r.kind == "hardware_configuration"}
    assert all(r.configuration in configurations for r in records if r.kind == "hardware_component")

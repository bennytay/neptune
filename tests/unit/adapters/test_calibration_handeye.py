"""Hand-eye results and an OpenCV calibration's subject and time, as tools write them (ADR 0073).

The oracles are independent readings: PyYAML's ``safe_load`` and ``xml.etree`` read each fixture,
``datetime`` counts each stated time, and the test compares them with what the adapter says.
Malformed, partial and boundary cases are made from the real fixtures by replacing one value.
"""

import calendar
import math
import xml.etree.ElementTree as ET
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from neptune.adapters.builtin import default_registry
from neptune.adapters.calibration import CalibrationAdapter
from neptune.adapters.config import ConfigAdapter
from neptune.adapters.contract import PROBE_HEAD_SIZE, STRUCTURE, VERIFIED, ProbeHints
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.discovery.probe import ProbeEngine
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.frames import Pose, Quaternion, QuaternionOrder, TransformDirection
from neptune.model.knowledge import AssertionKind, Known, Unknown
from neptune.model.machine import Calibration
from neptune.model.provenance import ByteRange, Provenance, Span
from neptune.model.reference import FrameGraph, FrameTransform, TimestampDomain
from neptune.model.time import ClockRole, Epoch, Timescale, Timestamp
from neptune.model.units import unit_from_json

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "calibration"
EASY: Final = "arm_wrist_easy_handeye.yaml"
LEGACY: Final = "humanoid_head_easy_handeye_legacy.yaml"
EASY2: Final = "mobile_manipulator_easy_handeye2.calib"
MOVEIT: Final = "arm_wrist_moveit_camera_pose.launch"
OPENCV: Final = "arm_wrist_opencv_handeye.yml"
SAMPLE: Final = "legged_head_opencv_sample.yml"
INVENTED: Final = "invented_handeye_shape.yaml"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def text(name: str) -> str:
    return fixture(name).decode()


def run(data: bytes | str, **config: Any) -> SourceOutput:
    raw = data.encode() if isinstance(data, str) else data
    return ingest_source(CalibrationAdapter(), BytesReader(raw), config)


def codes(output: SourceOutput) -> list[str]:
    return sorted(f.code for f in output.findings())


def only_calibration(output: SourceOutput) -> Calibration:
    (calibration,) = [r for r in output.records() if isinstance(r, Calibration)]
    return calibration


def transforms(output: SourceOutput) -> list[FrameTransform]:
    return [r for r in output.records() if isinstance(r, FrameTransform)]


def only_transform(output: SourceOutput) -> FrameTransform:
    (transform,) = transforms(output)
    return transform


def pose(transform: FrameTransform) -> Pose:
    assert isinstance(transform.value, Pose)
    return transform.value


def quaternion(transform: FrameTransform) -> Quaternion:
    rotation = pose(transform).rotation
    assert isinstance(rotation, Quaternion)
    return rotation


def parameter_names(calibration: Calibration) -> list[str]:
    return [p.name for p in calibration.parameters]


def cited_text(data: bytes, where: object) -> str:
    match where:
        case Span(start=start, end=end):
            return data.decode()[start:end]
        case ByteRange(offset=offset, length=length):
            return data[offset : offset + length].decode()
    raise AssertionError(f"unexpected locator {where!r}")


def probe(data: bytes, name: str = "f") -> tuple[float, list[str]]:
    result = CalibrationAdapter().probe(data[:PROBE_HEAD_SIZE], ProbeHints(name, len(data)))
    return result.confidence, [reason.code for reason in result.reasons]


def opencv(body: str) -> str:
    """An OpenCV FileStorage YAML calibration with ``body`` before its camera matrix."""
    matrix = "camera_matrix: !!opencv-matrix\n  rows: 1\n  cols: 1\n  dt: d\n  data: [ 1. ]\n"
    return "%YAML:1.0\n---\n" + body + matrix


# --- easy_handeye, easy_handeye2 and MoveIt as their tools write them --------------------------


@pytest.mark.parametrize(
    ("name", "parent", "child", "order", "keys"),
    [
        (EASY, "tool0", "wrist_camera_color_optical_frame", "wxyz", ("qw", "qx", "qy", "qz")),
        (LEGACY, "torso_link", "head_camera_link", "wxyz", ("qw", "qx", "qy", "qz")),
    ],
)
def test_an_easy_handeye_result_is_its_tf_transform_as_written(
    name: str, parent: str, child: str, order: str, keys: tuple[str, ...]
) -> None:
    data = fixture(name)
    output = run(data)
    loaded = yaml.safe_load(data)
    transform = only_transform(output)
    assert (transform.parent.frame_id, transform.child.frame_id) == (parent, child)
    (graph,) = [r for r in output.records() if isinstance(r, FrameGraph)]
    assert transform.parent.frame_graph_id == graph.id == transform.child.frame_graph_id
    # The tool publishes it as tf parent -> child: the child's pose in the parent.
    assert isinstance(transform.direction, Known)
    assert transform.direction.value is TransformDirection.CHILD_TO_PARENT
    value = pose(transform)
    moved = loaded["transformation"]
    assert value.translation.values == tuple(float(moved[k]) for k in "xyz")
    assert isinstance(value.translation.unit, Unknown)  # geometry_msgs states no unit
    assert quaternion(transform).values == tuple(float(moved[k]) for k in keys)
    assert quaternion(transform).order == Known(
        QuaternionOrder(order),
        transform.provenance,
    )
    assert isinstance(quaternion(transform).convention, Unknown)
    # The transform cites the transformation mapping, which reads again as the file's.
    (where,) = transform.provenance.evidence.locator
    assert isinstance(where, Span)
    line = data.decode().rfind("\n", 0, where.start) + 1  # the block, from its own line's indent
    assert yaml.safe_load("k:\n" + data.decode()[line : where.end])["k"] == moved
    calibration = only_calibration(output)
    assert calibration.extrinsics == (transform.id,)
    assert calibration.subject == Known(child, calibration.subject.provenance)  # type: ignore[union-attr]
    assert not any(n.startswith("transformation") for n in parameter_names(calibration))
    assert output.findings() == ()


def test_an_easy_handeye_result_keeps_every_other_key_as_a_parameter() -> None:
    calibration = only_calibration(run(fixture(EASY)))
    loaded = yaml.safe_load(fixture(EASY))["parameters"]
    assert parameter_names(calibration) == sorted(f"parameters/{k}" for k in loaded)
    found = {p.name: p.value for p in calibration.parameters}
    assert found["parameters/eye_on_hand"].value == "true"  # type: ignore[union-attr]
    assert found["parameters/robot_effector_frame"].value == "tool0"  # type: ignore[union-attr]
    # Machine, revision and times: nothing in the file states them.
    for field in (calibration.machine, calibration.performed, calibration.valid_from):
        assert isinstance(field, Unknown)


def test_an_easy_handeye2_result_is_read_from_its_message_fields() -> None:
    data = fixture(EASY2)
    output = run(data)
    loaded = yaml.safe_load(data)
    transform = only_transform(output)
    assert (transform.parent.frame_id, transform.child.frame_id) == (
        "arm_base_link",  # eye_on_base: the robot base is the parent
        "base_camera_link",
    )
    value = pose(transform)
    moved, turned = loaded["transform"]["translation"], loaded["transform"]["rotation"]
    assert value.translation.values == tuple(float(moved[k]) for k in "xyz")
    assert quaternion(transform).values == tuple(float(turned[k]) for k in "xyzw")
    assert quaternion(transform).order == Known(QuaternionOrder.XYZW, transform.provenance)
    calibration = only_calibration(output)
    assert calibration.subject.value == "base_camera_link"  # type: ignore[union-attr]
    assert "parameters/calibration_type" in parameter_names(calibration)
    assert output.findings() == ()


def test_a_moveit_camera_pose_is_its_static_transform_publisher_args() -> None:
    data = fixture(MOVEIT)
    output = run(data)
    node = ET.fromstring(data).find("node")
    assert node is not None
    args = node.attrib["args"].split()
    transform = only_transform(output)
    assert (transform.parent.frame_id, transform.child.frame_id) == (args[7], args[8])
    value = pose(transform)
    assert value.translation.values == tuple(float(a) for a in args[:3])
    assert quaternion(transform).values == tuple(float(a) for a in args[3:7])
    assert quaternion(transform).order == Known(QuaternionOrder.XYZW, transform.provenance)
    # static_transform_publisher documents its offset in metres.
    assert value.translation.unit == Known(unit_from_json("m"), transform.provenance)
    (where,) = transform.provenance.evidence.locator
    assert ET.fromstring(cited_text(data, where)).attrib == node.attrib
    calibration = only_calibration(output)
    assert calibration.subject == Known(args[8], transform.provenance)
    assert calibration.parameters == () and calibration.extrinsics == (transform.id,)
    assert output.findings() == ()


@pytest.mark.parametrize(
    ("name", "code"),
    [
        (EASY, "calibration.easy_handeye"),
        (LEGACY, "calibration.easy_handeye"),
        (EASY2, "calibration.easy_handeye2"),
        (MOVEIT, "calibration.moveit_handeye"),
    ],
)
def test_a_hand_eye_result_is_claimed_by_its_tools_keys_above_the_generic_claim(
    name: str, code: str
) -> None:
    data = fixture(name)
    assert probe(data) == (VERIFIED, [code])
    assert probe(data, "renamed.txt") == (VERIFIED, [code])  # the bytes decide, never the name
    config = ConfigAdapter().probe(data[:PROBE_HEAD_SIZE], ProbeHints("f", len(data)))
    assert config.confidence in (0.0, STRUCTURE) and config.confidence < VERIFIED
    assert ProbeEngine(default_registry()).probe(BytesReader(data), name).adapter == "calibration"


def test_the_corpus_invented_shape_is_not_a_hand_eye_format_and_stays_configuration() -> None:
    data = fixture(INVENTED)
    assert probe(data) == (0.0, ["calibration.not_calibration"])
    assert ProbeEngine(default_registry()).probe(BytesReader(data), INVENTED).adapter == "config"
    output = run(data)
    assert output.records() == () and codes(output) == ["calibration.not_calibration"]


@pytest.mark.parametrize(
    "data",
    [
        # a launch file with another static transform: a mounting, not MoveIt's result
        b'<launch><node pkg="tf2_ros" type="static_transform_publisher" name="lidar_tf"'
        b' args="0 0 0.3 0 0 0 1 base_link lidar" /></launch>',
        # MoveIt's node beside others
        b'<launch><node pkg="tf2_ros" type="static_transform_publisher"'
        b' name="camera_link_broadcaster" args="0 0 0 0 0 0 1 a b" /><node pkg="x" type="y"/>'
        b"</launch>",
        # easy_handeye's parameters without a transformation
        b"parameters:\n  eye_on_hand: true\n  robot_effector_frame: tool0\n"
        b"  tracking_base_frame: cam\n",
        # a transformation with no mode or camera frame
        b"transformation: {x: 0, y: 0, z: 0, qx: 0, qy: 0, qz: 0, qw: 1}\nrobot: arm\n",
        # easy_handeye2's parameters with no transform
        b"parameters:\n  calibration_type: eye_in_hand\n  robot_effector_frame: tool0\n"
        b"  tracking_base_frame: cam\n",
    ],
)
def test_hand_eye_lookalikes_are_not_claimed(data: bytes) -> None:
    assert probe(data)[0] == 0.0


# --- Malformed, partial and boundary hand-eye results --------------------------------------------


@pytest.mark.parametrize(
    ("name", "old", "new", "code", "also"),
    [
        (EASY, "  qw: 0.7095707365365209\n", "", "extrinsic_not_read", ()),  # partial
        (
            EASY,
            "qx: -0.014083532876116462",
            "qx: .nan",
            "extrinsic_not_read",
            ("non_finite_value",),
        ),
        (
            EASY,
            "qx: -0.014083532876116462",
            "qx: .inf",
            "extrinsic_not_read",
            ("non_finite_value",),
        ),
        (EASY, "z: 0.07019345517", "z: seven", "extrinsic_not_read", ()),
        (EASY, "z: 0.07019345517", "z: [0.07]", "extrinsic_not_read", ()),
        (
            EASY,
            "  qw: 0.7095707365365209\n",
            "  qw: 0.7095707365365209\n  qw: 0.7\n",
            "extrinsic_not_read",
            ("duplicate_key",),
        ),
        (EASY, "  qw: 0.7095707365365209\n", "", "extrinsic_not_read", ()),
        (EASY, "eye_on_hand: true", "eye_on_hand: maybe", "frame_unresolved", ()),
        (EASY, "eye_on_hand: true", "eye_on_hand: yes", "frame_unresolved", ()),
        (EASY, "eye_on_hand: true", "eye_on_hand: null", "frame_unresolved", ()),
        (EASY, "robot_effector_frame: tool0", "robot_effector_frame: ''", "frame_unresolved", ()),
        (EASY, "robot_effector_frame: tool0", "robot_effector_frame: 7", "frame_unresolved", ()),
        (
            EASY,
            "tracking_base_frame: wrist_camera_color_optical_frame",
            "tracking_base_frame: tool0",
            "frame_unresolved",
            (),
        ),
        (
            EASY,
            "tracking_base_frame: wrist_camera_color_optical_frame",
            "tracking_base_frame: " + "c" * 257,
            "frame_unresolved",
            (),
        ),
        (LEGACY, "eye_on_hand: false", "eye_on_hand: true", "frame_unresolved", ()),
        (
            EASY2,
            "calibration_type: eye_on_base",
            "calibration_type: eye_to_hand",
            "frame_unresolved",
            (),
        ),
        (EASY2, "  rotation:\n", "  turned:\n", "extrinsic_not_read", ()),
        (EASY2, "    w: 0.5135412520581701\n", "", "extrinsic_not_read", ()),
    ],
)
def test_a_hand_eye_transform_the_file_does_not_fully_state_is_a_finding_and_parameters(
    name: str, old: str, new: str, code: str, also: tuple[str, ...]
) -> None:
    source = text(name)
    assert old in source
    output = run(source.replace(old, new, 1))
    assert transforms(output) == []
    assert codes(output) == sorted(f"calibration.{c}" for c in (code, *also))
    calibration = only_calibration(output)
    (found,) = [f for f in output.findings() if f.code == f"calibration.{code}"]
    assert found.records == (calibration.id,)
    key = "transform" if name == EASY2 else "transformation"
    assert any(n.startswith(f"{key}/") for n in parameter_names(calibration))  # values stay
    assert calibration.extrinsics == ()


def test_quaternion_components_in_an_order_the_model_names_neither_are_never_reordered() -> None:
    source = text(EASY).replace("  qw: 0.7095707365365209\n", "")
    source = source.replace("  qz: -0.7041766438058231\n", "  qz: -0.7041766438058231\n  qw: 0.7\n")
    reordered = source.replace("  qx:", "  qy_:").replace("  qy:", "  qx:").replace("qy_", "qy")
    output = run(reordered)
    assert transforms(output) == []
    assert "neither x, y, z, w" in output.findings()[0].message


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ("0.0254 -0.0123 0.0701 -0.714048 -0.0357024 -0.014281 0.69904 panda_hand", "extrinsic"),
        ("0.0254 -0.0123 nan -0.714048 -0.0357024 -0.014281 0.69904 a b", "extrinsic"),
        ("0.0254 -0.0123 0.0701 x -0.0357024 -0.014281 0.69904 a b", "extrinsic"),
        ("0.0254 -0.0123 1e999 -0.714048 -0.0357024 -0.014281 0.69904 a b", "extrinsic"),
        ("0 0 0 0 0 0 1 panda_hand panda_hand", "frame"),
        ("0 0 0 0 0 0 1 panda_hand " + "c" * 257, "frame"),
    ],
)
def test_moveit_args_that_are_no_transform_stay_a_parameter(args: str, code: str) -> None:
    data = text(MOVEIT).replace(
        "0.0254 -0.0123 0.0701   -0.714048 -0.0357024 -0.014281 0.69904 panda_hand"
        " camera_color_optical_frame",
        args,
    )
    output = run(data)
    assert transforms(output) == []
    expected = (
        "calibration.extrinsic_not_read" if code == "extrinsic" else "calibration.frame_unresolved"
    )
    assert codes(output) == [expected]
    calibration = only_calibration(output)
    assert [(p.name, p.value.value) for p in calibration.parameters] == [  # type: ignore[union-attr]
        ("node/args", args)
    ]
    # The camera is named where args names it, a frame name or not: a child_frame_id
    # of 1 to 256 characters.
    tokens = args.split()
    if len(tokens) == 9 and len(tokens[8]) <= 256:
        assert calibration.subject.value == tokens[8]  # type: ignore[union-attr]
    else:
        assert isinstance(calibration.subject, Unknown)


def test_moveit_args_past_max_scalar_length_are_not_kept() -> None:
    data = text(MOVEIT).replace("panda_hand camera_color_optical_frame", "a")
    output = run(data, max_scalar_length=40)
    assert codes(output) == ["calibration.extrinsic_not_read", "calibration.value_not_read"]
    (parameter,) = only_calibration(output).parameters
    assert parameter.name == "node/args" and isinstance(parameter.value, Unknown)


def test_a_camera_frame_that_is_no_frame_name_is_no_subject_either() -> None:
    old = "tracking_base_frame: wrist_camera_color_optical_frame"
    output = run(text(EASY).replace(old, "tracking_base_frame: 42"))
    assert codes(output) == ["calibration.frame_unresolved"]
    assert isinstance(only_calibration(output).subject, Unknown)


@pytest.mark.parametrize(
    ("scale", "reported"),
    [
        (1.0, False),
        (1.0 + 0.5e-4, False),  # within MoveIt's printed precision
        (1.0 + 2e-4, True),
        (0.5, True),
        (0.0, True),  # a zero quaternion is no rotation: kept and reported
    ],
)
def test_a_quaternion_is_never_normalised_and_one_far_from_unit_is_reported(
    scale: float, reported: bool
) -> None:
    loaded = yaml.safe_load(fixture(EASY))
    moved = loaded["transformation"]
    for key in ("qx", "qy", "qz", "qw"):
        moved[key] = moved[key] * scale
    output = run(yaml.safe_dump(loaded))
    transform = only_transform(output)
    rotation = pose(transform).rotation
    assert rotation.values == tuple(moved[k] for k in ("qw", "qx", "qy", "qz"))  # as written
    found = [f for f in output.findings() if f.code == "calibration.quaternion_not_unit"]
    assert bool(found) is reported
    if reported:
        norm = math.sqrt(sum(v * v for v in rotation.values))
        assert float(str(found[0].details["norm"])) == pytest.approx(norm)
        assert set(found[0].records) == {transform.id, only_calibration(output).id}


def test_a_truncated_hand_eye_result_is_a_finding_and_no_records() -> None:
    data = fixture(EASY)
    output = run(data[: data.index(b"  qy:") + 6] + b"[")
    assert output.records() == () and codes(output) == ["calibration.syntax_error"]


def test_a_hand_eye_xml_with_a_dtd_is_refused() -> None:
    data = b'<!DOCTYPE launch [<!ENTITY a "b">]>' + fixture(MOVEIT)
    output = run(data)
    assert output.records() == () and codes(output) == ["calibration.dtd_refused"]


# --- OpenCV: the camera and the time the file states --------------------------------------------


def test_an_opencv_file_states_its_camera_and_an_instant_with_its_offset() -> None:
    data = fixture(OPENCV)
    output = run(data)
    calibration = only_calibration(output)
    assert isinstance(calibration.subject, Known) and calibration.subject.value == "WCAM-2B"
    assert isinstance(calibration.subject.provenance, Provenance)
    assert calibration.subject.provenance.assertion_kind is AssertionKind.STATED
    (domain,) = [r for r in output.records() if isinstance(r, TimestampDomain)]
    assert isinstance(calibration.performed, Known)
    stamp = calibration.performed.value
    stated = datetime.strptime("2026-04-12 09:15:41 -0400", "%Y-%m-%d %H:%M:%S %z")
    assert stamp == Timestamp(int(stated.timestamp()), domain.id)
    assert (domain.field, domain.scope) == ("calibration_time", ())
    assert domain.role == Known(ClockRole.DOCUMENT) and domain.epoch == Known(Epoch.UNIX)
    assert domain.timescale == Known(Timescale.POSIX)  # an offset makes it an instant
    assert domain.resolution == Known(Fraction(1))
    assert domain.provenance.assertion_kind is AssertionKind.STATED
    # The time cites its scalar, offset included; the text stays a parameter.
    (where,) = calibration.performed.provenance.evidence.locator  # type: ignore[union-attr]
    assert cited_text(data, where) == '"2026-04-12 09:15:41 -0400"'
    assert "calibration_time" in parameter_names(calibration)
    assert "camera_name" in parameter_names(calibration)
    assert output.findings() == ()


def test_opencvs_own_c_locale_time_counts_on_its_own_clock() -> None:
    output = run(fixture(SAMPLE))
    calibration = only_calibration(output)
    (domain,) = [r for r in output.records() if isinstance(r, TimestampDomain)]
    assert isinstance(calibration.subject, Unknown)  # calibration.cpp names no camera
    assert isinstance(domain.timescale, Unknown)  # no zone stated, none assumed
    assert calibration.performed.value == Timestamp(  # type: ignore[union-attr]
        calendar.timegm((2026, 10, 1, 14, 2, 37, 0, 0, 0)), domain.id
    )
    assert output.findings() == ()


@pytest.mark.parametrize(
    ("stated", "seconds", "resolution", "instant"),
    [
        ("2026-04-12T09:15:41Z", (2026, 4, 12, 9, 15, 41), Fraction(1), True),
        ("2026-04-12 09:15:41.250", (2026, 4, 12, 9, 15, 41), Fraction(1, 1000), False),
        ("2026-04-12T09:15:41.123456789+05:30", (2026, 4, 12, 3, 45, 41), Fraction(1, 10**9), True),
        ("2026-04-12 09:15:41+0000", (2026, 4, 12, 9, 15, 41), Fraction(1), True),
        ("Sat Feb 28 23:59:59 2026", (2026, 2, 28, 23, 59, 59), Fraction(1), False),
        ("1969-12-31 23:59:59", (1969, 12, 31, 23, 59, 59), Fraction(1), False),
    ],
)
def test_each_time_form_read_counts_as_adr_0023_counts_civil_time(
    stated: str, seconds: tuple[int, ...], resolution: Fraction, instant: bool
) -> None:
    output = run(opencv(f'calibration_time: "{stated}"\n'))
    (domain,) = [r for r in output.records() if isinstance(r, TimestampDomain)]
    assert domain.resolution == Known(resolution)
    assert (domain.timescale == Known(Timescale.POSIX)) is instant
    fraction = stated.split(".")[1][:9].split("+")[0] if "." in stated else ""
    expected = calendar.timegm((*seconds, 0, 0, 0)) * resolution.denominator + int(fraction or 0)
    assert only_calibration(output).performed.value.ticks == expected  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "stated",
    [
        '"08/19/11 20:44:38"',  # MSVC's %c: a two-digit year
        '"Thu 01 Oct 2026 02:02:37 PM EDT"',  # a localised %c with a zone abbreviation
        '"Fri Oct  1 14:02:37 2026"',  # the weekday is not the date's
        '"2026-06-30 23:59:60"',  # a leap second
        '"2026-02-30 10:00:00"',  # no such date
        '"2026-04-12 09:15:41 +25:00"',
        '"2026-04-12 09:15:41.1234567890"',  # finer than a nanosecond
        '"9999-12-31 23:59:59.999999999"',  # past 64-bit ticks
        '"2026-04-12"',
        "12345",
        '""',
    ],
)
def test_a_time_in_another_form_is_unknown_with_a_finding_and_stays_a_parameter(
    stated: str,
) -> None:
    output = run(opencv(f"calibration_time: {stated}\n"))
    calibration = only_calibration(output)
    assert isinstance(calibration.performed, Unknown)
    assert [r for r in output.records() if isinstance(r, TimestampDomain)] == []
    assert codes(output) == ["calibration.time_not_read"]
    assert output.findings()[0].records == (calibration.id,)


def test_the_tutorials_older_key_and_xml_state_a_time_too() -> None:
    old = only_calibration(run(opencv('calibration_Time: "2026-01-02 03:04:05"\n')))
    assert isinstance(old.performed, Known)
    xml = (
        "<?xml version='1.0'?><opencv_storage>"
        '<calibration_time>"Thu Oct  1 14:02:37 2026"</calibration_time>'
        "<camera_name>head_cam</camera_name>"
        "<camera_matrix type_id='opencv-matrix'><rows>1</rows><cols>1</cols><dt>d</dt>"
        "<data>1.</data></camera_matrix></opencv_storage>"
    )
    output = run(xml)
    calibration = only_calibration(output)
    assert calibration.subject.value == "head_cam"  # type: ignore[union-attr]
    # OpenCV writes a string with spaces in double quotes and reads it without them.
    assert calibration.performed.value.ticks == calendar.timegm(  # type: ignore[union-attr]
        (2026, 10, 1, 14, 2, 37, 0, 0, 0)
    )
    assert output.findings() == ()


def test_an_opencv_xml_string_is_read_as_opencv_reads_it_without_its_quotes() -> None:
    calibration = only_calibration(run(fixture("rov_camera.xml")))
    assert calibration.subject.value == "rov_down_cam"  # type: ignore[union-attr]
    found = {p.name: p.value for p in calibration.parameters}
    assert found["camera_name"].value == "rov_down_cam"  # type: ignore[union-attr]


@pytest.mark.parametrize("value", ["", "null", "~"])
def test_a_blank_time_is_unknown_without_a_finding(value: str) -> None:
    output = run(opencv(f"calibration_time: {value}\n"))
    assert isinstance(only_calibration(output).performed, Unknown)
    assert codes(output) == []


def test_a_time_that_is_no_scalar_is_reported() -> None:
    output = run(opencv("calibration_time: [2026, 4, 12]\n"))
    assert isinstance(only_calibration(output).performed, Unknown)
    assert codes(output) == ["calibration.time_not_read"]
    assert "not a scalar" in output.findings()[0].message


def test_a_blank_camera_name_is_no_subject() -> None:
    calibration = only_calibration(run(opencv('camera_name: ""\n')))
    assert isinstance(calibration.subject, Unknown)


# --- Determinism and lineage ---------------------------------------------------------------------


def dump(output: SourceOutput) -> bytes:
    return canonical_json.dumps([r.to_json() for r in (*output.records(), *output.findings())])


@pytest.mark.parametrize("name", [EASY, LEGACY, EASY2, MOVEIT, OPENCV, SAMPLE])
def test_output_is_byte_identical_and_a_setting_gives_new_lineage(name: str) -> None:
    data = fixture(name)
    first, again = run(data), run(data)
    assert dump(first) == dump(again)
    other = run(data, max_array_values=99_999)
    assert {r.id for r in other.records()}.isdisjoint({r.id for r in first.records()})

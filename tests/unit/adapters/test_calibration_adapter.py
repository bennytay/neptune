"""The calibration adapter on files shaped as ROS, Kalibr and OpenCV write them (ADR 0055).

The oracles are independent readings: PyYAML's ``safe_load`` and ``xml.etree`` read each file and
each cited span again on its own, and the test compares them with what the adapter says. Frame
transforms are checked against the matrices the file holds, not against the adapter's code.
"""

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from neptune.adapters.builtin import default_registry
from neptune.adapters.calibration import DESCRIPTOR, CalibrationAdapter
from neptune.adapters.config import ConfigAdapter
from neptune.adapters.contract import (
    PROBE_HEAD_SIZE,
    SIGNATURE,
    STRUCTURE,
    VERIFIED,
    ProbeHints,
)
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.discovery.probe import ProbeEngine
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.finding import IngestFinding
from neptune.model.frames import (
    STATIC,
    HomogeneousMatrix,
    MatrixLayout,
    TransformDirection,
)
from neptune.model.kinds import RECORD_KINDS
from neptune.model.knowledge import Ambiguous, Known, KnownAbsent, NotApplicable, Unknown
from neptune.model.machine import Calibration
from neptune.model.provenance import ByteRange, Locator, Span
from neptune.model.reference import FrameGraph, FrameTransform
from neptune.model.scalars import NonFinite

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "calibration"
CONFIG_FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "config"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(data: bytes, **config: Any) -> SourceOutput:
    return ingest_source(CalibrationAdapter(), BytesReader(data), config)


def calibrations(output: SourceOutput) -> list[Calibration]:
    return [r for r in output.records() if isinstance(r, Calibration)]


def transforms(output: SourceOutput) -> list[FrameTransform]:
    return [r for r in output.records() if isinstance(r, FrameTransform)]


def codes(output: SourceOutput) -> list[str]:
    return sorted(f.code for f in output.findings())


def finding(output: SourceOutput, code: str) -> IngestFinding:
    (found,) = [f for f in output.findings() if f.code == code]
    return found


def by_subject(output: SourceOutput) -> dict[str, Calibration]:
    found: dict[str, Calibration] = {}
    for calibration in calibrations(output):
        assert isinstance(calibration.subject, Known)
        found[calibration.subject.value] = calibration
    return found


def values(calibration: Calibration) -> dict[str, object]:
    """Each parameter's declared value: text, or its numbers as a tuple."""
    found: dict[str, object] = {}
    for parameter in calibration.parameters:
        assert isinstance(parameter.value, Known)
        found[parameter.name] = parameter.value.value
    return found


def cited(data: bytes, where: Locator) -> str | bytes:
    match where:
        case Span(start=start, end=end):
            return data.decode("utf-8")[start:end]
        case ByteRange(offset=offset, length=length):
            return data[offset : offset + length]
        case _:
            raise AssertionError(f"unexpected locator {where!r}")


def reparse(data: bytes, where: Locator) -> Any:
    """A cited block value read again on its own: under a key, with its own line's indent."""
    text = data.decode("utf-8")
    assert isinstance(where, Span)
    line = text.rfind("\n", 0, where.start) + 1
    return yaml.safe_load("k:\n" + text[line : where.end])["k"]


def probe(data: bytes, name: str = "f") -> tuple[float, list[str]]:
    result = CalibrationAdapter().probe(data[:PROBE_HEAD_SIZE], ProbeHints(name, len(data)))
    return result.confidence, [reason.code for reason in result.reasons]


def oracle(node: Any, prefix: str = "") -> dict[str, object]:
    """The parameters of a PyYAML-loaded mapping, flattened by the documented rule."""
    found: dict[str, object] = {}
    items = node.items() if isinstance(node, dict) else enumerate(node)
    for key, child in items:
        name = f"{prefix}/{key}" if prefix else str(key)
        if isinstance(child, dict):
            found.update(oracle(child, name))
        elif isinstance(child, list) and all(
            isinstance(n, int | float) and not isinstance(n, bool) for n in child
        ):
            found[name] = tuple(float(n) for n in child)
        elif isinstance(child, list):
            found.update(oracle(child, name))
        elif isinstance(child, int | float) and not isinstance(child, bool):
            found[name] = (float(child),)
        else:
            found[name] = str(child)
    return found


def _as_mapping(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
    assert isinstance(node, yaml.MappingNode)
    return loader.construct_mapping(node)


# OpenCV's `!!opencv-matrix` tag is read as the plain mapping it tags.
yaml.add_multi_constructor("tag:yaml.org,2002:opencv", _as_mapping, Loader=yaml.SafeLoader)


def load_yaml(data: bytes) -> Any:
    text = data.decode("utf-8")
    if text.startswith("%YAML:"):
        text = "#" + text[1:]
    return yaml.safe_load(text)


# --- Formats: each parameter equals an independent reading -------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "wrist_camera_info.yaml",
        "lidar_camera_autoware.yml",
        "stereo_unnamed_extrinsics.yml",
        "aerial_camera_info.json",
        "quadruped_imu.yaml",
    ],
)
def test_a_single_subject_file_states_every_value_as_pyyaml_reads_it(name: str) -> None:
    data = fixture(name)
    (calibration,) = calibrations(run(data))
    # PyYAML reads JSON too.
    assert values(calibration) == oracle(load_yaml(data))


def test_a_ros_camera_info_file_is_one_calibration_named_by_its_camera() -> None:
    output = run(fixture("wrist_camera_info.yaml"))
    (calibration,) = calibrations(output)
    assert calibration.subject == Known(
        "wrist_cam",
        calibration.subject.provenance,  # type: ignore[union-attr]
    )
    assert values(calibration)["distortion_model"] == "plumb_bob"
    assert values(calibration)["camera_matrix/data"] == (
        615.2631,
        0.0,
        322.1054,
        0.0,
        614.9087,
        241.7762,
        0.0,
        0.0,
        1.0,
    )
    assert calibration.extrinsics == ()
    assert transforms(output) == []
    assert output.findings() == ()
    # What the file does not say stays unknown: nothing binds it to a machine or a time.
    for field in (calibration.machine, calibration.hardware_revision, calibration.performed):
        assert isinstance(field, Unknown)
    # Text has no unit; a number's unit is declared by no ROS camera_info file.
    units = {p.name: p.unit for p in calibration.parameters}
    assert isinstance(units["distortion_model"], NotApplicable)
    assert isinstance(units["camera_matrix/data"], Unknown)


def test_a_ros_camera_info_message_is_named_by_its_frame_id() -> None:
    (calibration,) = calibrations(run(fixture("aerial_camera_info.json")))
    assert isinstance(calibration.subject, Known)
    assert calibration.subject.value == "gimbal_camera_optical_frame"
    found = values(calibration)
    assert found["K"] == (1471.5, 0.0, 960.4, 0.0, 1470.8, 538.2, 0.0, 0.0, 1.0)
    assert found["D"] == (0.0113, -0.0382, 0.0409, -0.0151)
    assert found["header/stamp/secs"] == (0.0,)


def test_a_kalibr_rig_is_one_calibration_per_camera_and_a_graph_of_its_extrinsics() -> None:
    data = fixture("quadruped_camchain_imucam.yaml")
    output = run(data)
    cams = by_subject(output)
    assert sorted(cams) == ["cam0", "cam1"]
    oracle_ = load_yaml(data)
    for name, calibration in cams.items():
        expected = oracle(
            {k: v for k, v in oracle_[name].items() if k not in ("T_cam_imu", "T_cn_cnm1")}
        )
        assert values(calibration) == expected
    (graph,) = [r for r in output.records() if isinstance(r, FrameGraph)]
    found = {(t.parent.frame_id, t.child.frame_id): t for t in transforms(output)}
    assert sorted(found) == [("cam0", "imu"), ("cam1", "cam0"), ("cam1", "imu")]
    expected_matrices = {
        ("cam0", "imu"): oracle_["cam0"]["T_cam_imu"],
        ("cam1", "imu"): oracle_["cam1"]["T_cam_imu"],
        ("cam1", "cam0"): oracle_["cam1"]["T_cn_cnm1"],
    }
    for pair, transform in found.items():
        assert transform.parent.frame_graph_id == graph.id == transform.child.frame_graph_id
        assert transform.validity == STATIC
        # Kalibr's T_a_b maps b's coordinates into a's: child to parent, as its format says.
        assert isinstance(transform.direction, Known)
        assert transform.direction.value is TransformDirection.CHILD_TO_PARENT
        assert isinstance(transform.value, HomogeneousMatrix)
        assert isinstance(transform.value.layout, Known)
        assert transform.value.layout.value is MatrixLayout.ROW_MAJOR
        assert isinstance(transform.value.translation_unit, Unknown)  # no file states a unit
        flat = [n for row in expected_matrices[pair] for n in row]
        assert list(transform.value.values) == [float(n) for n in flat]
        # The cited span is the matrix, read again on its own.
        (where,) = transform.provenance.evidence.locator
        assert reparse(data, where) == expected_matrices[pair]
    for name, calibration in cams.items():
        wanted = sorted(t.id for (parent, _), t in found.items() if parent == name)
        assert list(calibration.extrinsics) == wanted
    assert codes(output) == ["calibration.frame_loop"]  # cam0-imu-cam1 are joined two ways


def test_an_imu_file_is_a_calibration_in_either_of_kalibrs_shapes() -> None:
    flat = calibrations(run(fixture("quadruped_imu.yaml")))
    assert isinstance(flat[0].subject, Unknown)  # a flat imu.yaml names nothing
    assert values(flat[0])["update_rate"] == (200.0,)
    wrapped = by_subject(run(fixture("quadruped_imu_calibrated.yaml")))["imu0"]
    assert values(wrapped)["gyroscope_noise_density"] == (0.00016,)
    assert values(wrapped)["model"] == "calibrated"
    assert values(wrapped)["T_i_b/0"] == (
        1.0,
        0.0,
        0.0,
        0.0,
    )  # an extrinsic whose frames are unnamed


def test_opencv_xml_is_read_as_opencv_reads_it() -> None:
    data = fixture("rov_camera.xml")
    output = run(data)
    (calibration,) = calibrations(output)
    found = {p.name: p.value for p in calibration.parameters}
    root = ET.fromstring(data)
    matrix = root.find("camera_matrix/data")
    assert matrix is not None and matrix.text is not None
    assert found["camera_matrix/data"] == Known(
        tuple(float(t) for t in matrix.text.split()),
        found["camera_matrix/data"].provenance,  # type: ignore[union-attr]
    )
    assert found["camera_matrix/dt"].value == "d"  # type: ignore[union-attr]
    assert found["image_width"].value == (1600.0,)  # type: ignore[union-attr]
    # `.Nan` is OpenCV's NaN: kept as declared, with a finding.
    assert found["avg_reprojection_error"].value == (NonFinite.NAN,)  # type: ignore[union-attr]
    assert codes(output) == ["calibration.non_finite_value"]
    # Every number of an array is cited by its element, which parses on its own.
    where = found["camera_matrix/data"].provenance.evidence.locator[0]  # type: ignore[union-attr]
    element = ET.fromstring(cited(data, where))
    assert element.tag == "data"


def test_the_autoware_extrinsic_has_no_named_frames_so_it_is_no_transform() -> None:
    output = run(fixture("lidar_camera_autoware.yml"))
    (calibration,) = calibrations(output)
    assert transforms(output) == [] and calibration.extrinsics == ()
    assert len(values(calibration)["CameraExtrinsicMat/data"]) == 16  # type: ignore[arg-type]
    assert codes(output) == ["calibration.frame_unresolved"]


def test_a_stereo_pair_keeps_r_and_t_as_parameters_and_says_their_frames_are_unnamed() -> None:
    output = run(fixture("stereo_unnamed_extrinsics.yml"))
    assert transforms(output) == []
    assert finding(output, "calibration.frame_unresolved").message.startswith("'R', 'T'")


# --- The graph and its checks -------------------------------------------------------------------


def test_a_chain_with_gaps_names_each_missing_extrinsic_and_the_split_graph() -> None:
    output = run(fixture("chain_missing_extrinsics.yaml"))
    assert sorted(by_subject(output)) == ["cam0", "cam1", "cam2"]
    found = {(t.parent.frame_id, t.child.frame_id) for t in transforms(output)}
    assert found == {("cam0", "imu"), ("cam2", "cam1")}
    missing = sorted(
        (f.details["entry"], f.details["key"])
        for f in output.findings()
        if f.code == "calibration.extrinsic_missing"
    )
    assert missing == [("cam1", "T_cam_imu"), ("cam1", "T_cn_cnm1"), ("cam2", "T_cam_imu")]
    assert "calibration.frame_graph_disconnected" in codes(output)


def test_a_transform_to_a_camera_the_file_lacks_is_not_emitted_and_stays_parameters() -> None:
    text = fixture("chain_missing_extrinsics.yaml").decode().replace("cam1:", "camx:")
    output = run(text.encode())
    cams = by_subject(output)
    assert "T_cn_cnm1/0" in values(cams["cam2"])  # the rows stay
    assert ("cam2", "camx") not in {
        (t.parent.frame_id, t.child.frame_id) for t in transforms(output)
    }
    assert "calibration.frame_unresolved" in codes(output)


def test_a_transform_declared_twice_is_kept_twice_and_reported() -> None:
    one = fixture("chain_missing_extrinsics.yaml").decode().split("cam1:")[0]
    output = run((one + one).encode())
    assert len(transforms(output)) == 2
    assert finding(output, "calibration.frame_transform_repeated").records == tuple(
        sorted(t.id for t in transforms(output))
    )
    assert "calibration.duplicate_subject" in codes(output)


def test_two_calibrations_of_one_subject_are_never_merged() -> None:
    one = fixture("wrist_camera_info.yaml").decode()
    two = one.replace("615.2631", "700.0")
    output = run(f"{one}---\n{two}".encode())
    found = calibrations(output)
    assert len(found) == 2 and found[0].id != found[1].id
    assert {values(c)["camera_matrix/data"][0] for c in found} == {615.2631, 700.0}  # type: ignore[index]
    assert finding(output, "calibration.duplicate_subject").records == tuple(
        sorted(c.id for c in found)
    )


def test_a_matrix_that_does_not_hold_its_declared_size_is_reported_and_kept_as_declared() -> None:
    output = run(fixture("short_matrix_camera_info.yaml"))
    (calibration,) = calibrations(output)
    assert len(values(calibration)["camera_matrix/data"]) == 8  # type: ignore[arg-type]
    assert "declares 9 numbers, holds 8" in finding(output, "calibration.shape_mismatch").message


def test_an_extrinsic_that_is_not_a_4x4_of_numbers_stays_parameters() -> None:
    rig = (
        "cam0:\n  camera_model: pinhole\n  intrinsics: [1.0, 1.0, 0.0, 0.0]\n"
        "  T_cam_imu: [[1,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]\n"
    )
    output = run(rig.encode())
    assert transforms(output) == []
    assert "calibration.extrinsic_not_read" in codes(output)
    assert "T_cam_imu/0" in values(calibrations(output)[0])


# --- Missingness and ambiguity -----------------------------------------------------------------


MINIMAL: Final = (
    "camera_name: c\ndistortion_model: plumb_bob\ncamera_matrix: {rows: 1, cols: 1, data: [1]}\n"
)


def test_a_value_yaml_versions_read_differently_is_ambiguous_until_one_is_declared() -> None:
    body = MINIMAL + "k: 1e-3\n"
    (undeclared,) = calibrations(run(body.encode()))
    k = next(p for p in undeclared.parameters if p.name == "k")
    assert isinstance(k.value, Ambiguous)
    assert [c.value for c in k.value.candidates] == ["1e-3", (0.001,)]
    assert isinstance(k.unit, NotApplicable)
    assert "calibration.ambiguous_value" in codes(run(body.encode()))
    declared = run(("%YAML 1.2\n---\n" + body).encode())
    assert values(calibrations(declared)[0])["k"] == (0.001,)
    assert codes(declared) == []


def test_a_null_is_known_absent_citing_the_file_and_text_is_kept_as_written() -> None:
    body = MINIMAL + "note: null\nid: 007\n"
    (calibration,) = calibrations(run(body.encode()))
    found = {p.name: p.value for p in calibration.parameters}
    assert isinstance(found["note"], KnownAbsent)
    assert found["id"] == Known((7.0,), found["id"].provenance)  # type: ignore[union-attr]


# --- Claims ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("wrist_camera_info.yaml", "calibration.ros_camera_info"),
        ("aerial_camera_info.json", "calibration.ros_camera_info_message"),
        ("quadruped_camchain_imucam.yaml", "calibration.kalibr"),
        ("quadruped_imu.yaml", "calibration.kalibr"),
        ("lidar_camera_autoware.yml", "calibration.opencv_yaml"),
        ("rov_camera.xml", "calibration.opencv_xml"),
        ("stereo_unnamed_extrinsics.yml", "calibration.opencv_yaml"),
    ],
)
def test_a_calibration_is_claimed_by_its_required_keys_above_the_generic_yaml_claim(
    name: str, code: str
) -> None:
    data = fixture(name)
    assert probe(data) == (VERIFIED, [code])
    assert probe(data, "renamed.txt") == (VERIFIED, [code])  # the bytes decide, never the name
    config = ConfigAdapter().probe(data[:PROBE_HEAD_SIZE], ProbeHints("f", len(data)))
    assert config.confidence in (0.0, STRUCTURE) and config.confidence < VERIFIED
    chosen = ProbeEngine(default_registry()).probe(BytesReader(data), name)
    assert chosen.adapter == "calibration"


@pytest.mark.parametrize(
    "data",
    [
        # a camera_matrix with nothing else of camera_info's
        b"camera_matrix:\n  rows: 3\n  cols: 3\n  data: [1, 0, 0, 0, 1, 0, 0, 0, 1]\n",
        # camera_matrix as a plain list in an application's settings
        b"camera_matrix: [1, 0, 0, 0, 1, 0, 0, 0, 1]\ndistortion_model: plumb_bob\nfps: 30\n",
        # a cam0 that is not Kalibr's
        b"cam0:\n  name: front\n  fps: 30\nimu0:\n  rate: 200\n",
        # an OpenCV-marked file with no calibration name
        b"%YAML:1.0\n---\nfeatures: !!opencv-matrix\n  rows: 1\n  cols: 1\n  dt: f\n  data: [1.]\n",
        # K, P without distortion_model
        b"K: [1, 0, 0, 0, 1, 0, 0, 0, 1]\nP: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]\n",
        b"<?xml version='1.0'?><opencv_storage><fps>30</fps></opencv_storage>",
        b"<robot name='arm'><link name='base'/></robot>",
    ],
)
def test_generic_yaml_and_lookalikes_are_not_claimed(data: bytes) -> None:
    assert probe(data)[0] == 0.0


@pytest.mark.parametrize("name", ["nav2_params.yaml", "px4_params.json", "gripper_tool.toml"])
def test_generic_configuration_stays_with_the_config_adapter(name: str) -> None:
    data = (CONFIG_FIXTURES / name).read_bytes()
    assert probe(data)[0] == 0.0
    assert ProbeEngine(default_registry()).probe(BytesReader(data), name).adapter == "config"


def test_a_calibration_whose_head_is_cut_is_claimed_below_a_parse_of_the_whole_file() -> None:
    padded = fixture("wrist_camera_info.yaml") + b"# " + b"x" * 70_000 + b"\n"
    assert len(padded) > PROBE_HEAD_SIZE
    assert probe(padded) == (SIGNATURE, ["calibration.ros_camera_info"])


# --- Determinism, lineage, contract ------------------------------------------------------------


def dump(output: SourceOutput) -> bytes:
    return canonical_json.dumps([r.to_json() for r in (*output.records(), *output.findings())])


@pytest.mark.parametrize(
    "name", ["quadruped_camchain_imucam.yaml", "rov_camera.xml", "lidar_camera_autoware.yml"]
)
def test_output_is_byte_identical_and_a_setting_gives_new_lineage(name: str) -> None:
    data = fixture(name)
    first, again = run(data), run(data)
    assert dump(first) == dump(again)
    other = run(data, max_array_values=99_999)
    assert {r.id for r in other.records()}.isdisjoint({r.id for r in first.records()})


def test_the_adapter_emits_only_existing_kinds() -> None:
    assert set(DESCRIPTOR.record_kinds) <= set(RECORD_KINDS)
    for name in ("quadruped_camchain_imucam.yaml", "wrist_camera_info.yaml", "rov_camera.xml"):
        kinds = {r.kind for r in run(fixture(name)).records()}
        assert kinds <= set(DESCRIPTOR.record_kinds)


def test_inspect_summarises_without_reading_values() -> None:
    from neptune.adapters.contract import configure

    config = configure(DESCRIPTOR, {})
    xml = CalibrationAdapter().inspect(BytesReader(fixture("rov_camera.xml")), config)
    opencv = CalibrationAdapter().inspect(BytesReader(fixture("lidar_camera_autoware.yml")), config)
    assert json.loads(canonical_json.dumps(xml.summary))["xml"] is True
    assert json.loads(canonical_json.dumps(opencv.summary))["opencv_header"] is True


@pytest.mark.parametrize(
    "data",
    [
        # Kalibr's flat imu.yaml with no imu<N> anywhere in it
        b"accelerometer_noise_density: 0.01\ngyroscope_noise_density: 0.005\nrostopic: /imu\n",
        # OpenCV files whose only calibration key is a distortion name, or a second matrix
        b"%YAML:1.0\n---\ndistCoeffs: !!opencv-matrix\n  rows: 1\n  cols: 1\n  dt: d\n"
        b"  data: [0.]\n",
        b"%YAML:1.0\n---\nM2: !!opencv-matrix\n  rows: 1\n  cols: 1\n  dt: d\n  data: [1.]\n",
        b"<opencv_storage><dist_coeffs type_id='opencv-matrix'><rows>1</rows><cols>1</cols>"
        b"<dt>d</dt><data>0.</data></dist_coeffs></opencv_storage>",
    ],
)
def test_every_key_a_format_requires_can_make_the_probe_look(data: bytes) -> None:
    assert probe(data)[0] == VERIFIED

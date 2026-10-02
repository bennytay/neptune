"""Corrupt, truncated and hostile calibration input costs findings, never exceptions (ADR 0055).

Every case runs through ``ingest_source``, which checks the adapter's contract laws on the way:
declared codes only, exact citations, one chunk per output.
"""

from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.calibration import CalibrationAdapter
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.discovery.reader import BytesReader
from neptune.model.knowledge import Known, Unknown
from neptune.model.machine import Calibration
from neptune.model.reference import FrameTransform
from neptune.model.scalars import NonFinite

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "calibration"
CONFIG_FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "config"
HEAD: Final = "camera_name: c\ndistortion_model: plumb_bob\n"


def run(data: bytes, **config: Any) -> SourceOutput:
    return ingest_source(CalibrationAdapter(), BytesReader(data), config)


def codes(output: SourceOutput) -> list[str]:
    return sorted(f.code for f in output.findings())


def calibrations(output: SourceOutput) -> list[Calibration]:
    return [r for r in output.records() if isinstance(r, Calibration)]


def camera(extra: str) -> bytes:
    matrix = "camera_matrix: {rows: 1, cols: 1, data: [1.0]}\n"
    return (HEAD + matrix + extra).encode()


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("truncated_camchain.yaml", "calibration.syntax_error"),
        ("empty.yaml", "calibration.no_document"),
        ("rov_dtd.xml", "calibration.dtd_refused"),
        ("rov_truncated.xml", "calibration.syntax_error"),
    ],
)
def test_a_broken_file_is_a_finding_and_no_records(name: str, code: str) -> None:
    output = run((FIXTURES / name).read_bytes())
    assert codes(output) == [code] and output.records() == ()
    assert len(output.plan.chunks) == 1


def test_bytes_that_are_not_text_are_not_read() -> None:
    assert codes(run(b"camera_matrix: \xc3\x28")) == ["calibration.invalid_encoding"]
    assert codes(run(b"\x89MCAP0\r\n\xff\xfe\xfd")) == ["calibration.invalid_encoding"]


def test_a_foreign_document_is_one_info_finding() -> None:
    output = run((CONFIG_FIXTURES / "nav2_params.yaml").read_bytes())
    assert codes(output) == ["calibration.not_calibration"] and output.records() == ()


def test_the_billion_laughs_is_not_expanded_and_not_a_calibration() -> None:
    output = run((CONFIG_FIXTURES / "billion_laughs.yaml").read_bytes())
    assert output.records() == () and codes(output) == ["calibration.not_calibration"]


def test_aliases_are_never_expanded_each_costs_one_unknown() -> None:
    output = run((FIXTURES / "alias_bomb_camera_info.yaml").read_bytes())
    (calibration,) = calibrations(output)
    assert len(calibration.parameters) < 40  # 9 + 9 nested aliases, not 9**9
    unread = [p for p in calibration.parameters if isinstance(p.value, Unknown)]
    assert len(unread) == 22
    assert "calibration.value_not_read" in codes(output)


def test_one_corrupt_document_does_not_cost_the_ones_before_it() -> None:
    good = (FIXTURES / "wrist_camera_info.yaml").read_bytes()
    output = run(good + b"---\ncamera_matrix: [1, 2\n")
    assert len(calibrations(output)) == 1
    assert codes(output) == ["calibration.syntax_error"]


def test_nan_and_infinity_are_kept_as_declared_with_a_finding() -> None:
    output = run((FIXTURES / "nan_camera_info.yaml").read_bytes())
    (calibration,) = calibrations(output)
    found = {p.name: p.value for p in calibration.parameters}
    data = found["camera_matrix/data"]
    assert isinstance(data, Known)
    assert data.value[0] is NonFinite.NAN and data.value[4] is NonFinite.POSITIVE_INFINITY
    assert codes(output) == ["calibration.non_finite_value"]


def test_an_extrinsic_with_nan_is_no_transform() -> None:
    rig = (
        "cam0:\n  camera_model: pinhole\n  intrinsics: [1.0, 1.0, 0.0, 0.0]\n"
        "  T_cam_imu: [[1,0,0,.nan],[0,1,0,0],[0,0,1,0],[0,0,0,1]]\n"
    )
    output = run(rig.encode())
    assert not [r for r in output.records() if isinstance(r, FrameTransform)]
    assert "calibration.extrinsic_not_read" in codes(output)


def test_a_huge_matrix_is_an_unknown_parameter_and_a_finding() -> None:
    numbers = ", ".join("1.5" for _ in range(5_000))
    output = run(camera(f"big: [{numbers}]\n"), max_array_values=1_000)
    (calibration,) = calibrations(output)
    big = next(p for p in calibration.parameters if p.name == "big")
    assert isinstance(big.value, Unknown)
    assert codes(output) == ["calibration.array_too_large"]
    # Under the limit the same file is read.
    fine = run(camera(f"big: [{numbers}]\n"), max_array_values=5_000)
    assert codes(fine) == []


def test_a_document_of_too_many_values_is_not_read() -> None:
    numbers = ", ".join("1.5" for _ in range(2_000))
    output = run(camera(f"big: [{numbers}]\n"), max_items=1_000)
    assert output.records() == () and codes(output) == ["calibration.too_many_values"]


def test_a_file_over_max_bytes_is_not_read() -> None:
    output = run((FIXTURES / "wrist_camera_info.yaml").read_bytes(), max_bytes=100)
    assert output.records() == () and codes(output) == ["calibration.too_large"]


def test_deep_yaml_is_refused_by_depth_not_by_the_stack() -> None:
    text = HEAD + "".join("  " * i + f"k{i}:\n" for i in range(400)) + "  " * 400 + "x: 1\n"
    output = run(camera("") + text.encode()[len(HEAD) :])
    assert output.records() == () and "calibration.too_deep" in codes(output)
    flow = run(("[" * 5_000 + "]" * 5_000).encode())
    assert flow.records() == () and "calibration.too_deep" in codes(flow)


def test_deep_xml_and_xml_with_too_many_elements_are_refused() -> None:
    deep = b"<opencv_storage>" + b"<a>" * 1_000 + b"</a>" * 1_000 + b"</opencv_storage>"
    assert codes(run(deep)) == ["calibration.too_deep"]
    wide = b"<opencv_storage>" + b"<a>1</a>" * 500 + b"</opencv_storage>"
    assert codes(run(wide, max_items=100)) == ["calibration.too_many_values"]


def test_an_xml_matrix_over_the_array_limit_is_an_unknown_parameter() -> None:
    numbers = " ".join("1.5" for _ in range(300))
    xml = (
        "<opencv_storage><camera_matrix type_id='opencv-matrix'><rows>1</rows><cols>300</cols>"
        f"<dt>d</dt><data>{numbers}</data></camera_matrix></opencv_storage>"
    ).encode()
    output = run(xml, max_array_values=100)
    (calibration,) = calibrations(output)
    data = next(p for p in calibration.parameters if p.name == "camera_matrix/data")
    assert isinstance(data.value, Unknown)
    assert codes(output) == ["calibration.array_too_large"]


def test_numbers_and_text_no_record_can_hold_are_unknown_not_crashes() -> None:
    output = run(camera("huge: 1" + "0" * 400 + '\nlone: "\\ud800"\n'))
    (calibration,) = calibrations(output)
    found = {p.name: p.value for p in calibration.parameters}
    assert isinstance(found["huge"], Unknown) and isinstance(found["lone"], Unknown)
    assert "calibration.value_not_read" in codes(output)


def test_repeated_keys_are_each_kept_and_named_by_position() -> None:
    output = run(camera("k: 1.0\nk: 2.0\n"))
    (calibration,) = calibrations(output)
    names = [p.name for p in calibration.parameters if p.name.startswith("k")]
    assert len(names) == 2 and len(set(names)) == 2
    assert codes(output) == ["calibration.duplicate_key"]


def test_a_utf8_byte_order_mark_and_crlf_are_read_past() -> None:
    data = b"\xef\xbb\xbf" + (FIXTURES / "wrist_camera_info.yaml").read_bytes().replace(
        b"\n", b"\r\n"
    )
    output = run(data)
    assert len(calibrations(output)) == 1 and codes(output) == []

"""Corrupt, truncated and hostile calibration input costs findings, never exceptions (ADR 0055).

Every case runs through ``ingest_source``, which checks the adapter's contract laws on the way:
declared codes only, exact citations, one chunk per output.
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.calibration import CalibrationAdapter
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.discovery.reader import BytesReader
from neptune.model.knowledge import Known, Unknown
from neptune.model.machine import Calibration
from neptune.model.provenance import ByteRange, Provenance
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


def xml(body: str) -> bytes:
    return f"<opencv_storage>{body}</opencv_storage>".encode()


def matrix(name: str, text: str, dt: str = "d") -> str:
    return (
        f"<{name} type_id='opencv-matrix'><rows>1</rows><cols>1</cols><dt>{dt}</dt>"
        f"<data>{text}</data></{name}>"
    )


def known(calibration: Calibration, name: str) -> Known[Any]:
    value = next(p.value for p in calibration.parameters if p.name == name)
    assert isinstance(value, Known)
    return value


def test_a_number_no_record_can_hold_in_xml_is_text_not_a_crash_nor_a_guess() -> None:
    body = (
        matrix("camera_matrix", "1") + f"<fx>{'9' * 5_000}</fx><fy>1e999</fy><fz>{'9' * 300}</fz>"
    )
    output = run(xml(body))
    (calibration,) = calibrations(output)
    assert known(calibration, "fx").value == "9" * 5_000  # too long to be a number: its text
    assert known(calibration, "fy").value == "1e999"  # not an infinity the file never wrote
    assert known(calibration, "fz").value == (float("9" * 300),)
    assert output.findings() == ()


@pytest.mark.parametrize("dt", ["²f", "9" * 5_000 + "f", "d"])
def test_an_odd_element_type_is_no_crash(dt: str) -> None:
    output = run(xml(matrix("camera_matrix", "1.0", dt)))
    assert len(calibrations(output)) == 1


def test_numbers_of_an_xml_matrix_count_against_max_items() -> None:
    body = "".join(matrix(f"M{i}", " ".join("1" for _ in range(5_000))) for i in range(40))
    output = run(xml(matrix("camera_matrix", "1") + body), max_items=50_000)
    assert output.records() == () and codes(output) == ["calibration.too_many_values"]


def test_a_long_scalar_and_long_element_paths_in_xml_are_limited() -> None:
    long = run(
        xml(matrix("camera_matrix", "1") + f"<note>{'x' * 600}</note>"), max_scalar_length=500
    )
    (calibration,) = calibrations(long)
    note = next(p for p in calibration.parameters if p.name == "note")
    assert isinstance(note.value, Unknown) and "calibration.value_not_read" in codes(long)
    name = "n" * 3_000
    nested = (
        "".join(f"<{name}>" for _ in range(10)) + "1" + "".join(f"</{name}>" for _ in range(10))
    )
    assert "calibration.paths_too_long" in codes(
        run(xml(matrix("camera_matrix", "1") + nested), max_path_ratio=1)
    )


def test_xml_lists_of_mappings_are_named_by_position() -> None:
    body = matrix("camera_matrix", "1") + (
        "<rigs><_><name>a</name><v>1</v></_><_><name>b</name><v>2</v></_></rigs>"
    )
    (calibration,) = calibrations(run(xml(body)))
    names = [p.name for p in calibration.parameters if p.name.startswith("rigs")]
    assert names == ["rigs/0/name", "rigs/0/v", "rigs/1/name", "rigs/1/v"]
    assert codes(run(xml(body))) == []


def test_an_xml_element_span_ends_at_its_own_closing_tag() -> None:
    body = (
        "<camera_matrix type_id='opencv-matrix' note=\"x>y\"><rows>1</rows><cols>1</cols>"
        "<dt>d</dt><data>1</data></camera_matrix><flag a='>'/><image_width>4</image_width>"
    )
    data = xml(body)
    (calibration,) = calibrations(run(data))
    for name, tag in (
        ("image_width", "image_width"),
        ("camera_matrix/rows", "rows"),
        ("flag", "flag"),
    ):
        value = next(p.value for p in calibration.parameters if p.name == name)
        assert isinstance(value, Known | Unknown)
        assert isinstance(value.provenance, Provenance)
        where = value.provenance.evidence.locator[0]
        assert isinstance(where, ByteRange)
        element = ET.fromstring(data[where.offset : where.offset + where.length])
        assert element.tag == tag  # the whole element, not cut at a '>' inside an attribute


def test_blank_values_and_empty_keys_are_unknown_never_a_value() -> None:
    output = run(camera('empty: ""\n"": 1\nblank: " "\n'))
    (calibration,) = calibrations(output)
    found = {p.name: p.value for p in calibration.parameters}
    assert isinstance(found["empty"], Unknown)  # the model holds no empty text
    assert known(calibration, "blank").value == " "
    assert "" not in found and "calibration.value_not_read" in codes(output)
    blank = run(xml(matrix("camera_matrix", "1") + "<flag/>"))
    assert isinstance(
        next(p for p in calibrations(blank)[0].parameters if p.name == "flag").value, Unknown
    )

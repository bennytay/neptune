"""Hostile and damaged geometry: truncation, lying headers, bombs, bad references and limits.

Every case is a finding, never an exception, and the work done is bounded by the config's limits.
``ingest_source`` runs the contract's checks on every output, so each run here also proves that no
record or finding is repeated and that every citation is inside the source.
"""

import json
import struct
import time
import tracemalloc
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.geometry import GeometryAdapter
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.discovery.reader import BytesReader
from neptune.model.finding import FindingCategory, Severity
from neptune.model.knowledge import Known, NotApplicable, NotCovered, Unknown
from neptune.model.world import SpatialArtifact, StructuredRecord, StructuredTable

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "geometry"
VALID: Final = (
    "arm_link.obj", "quadruped_foot.stl", "quadruped_hip.stl", "mobile_base_chassis.ply",
    "agv_fork.ply", "av_lidar_scan.ply", "marine_hull.gltf", "humanoid_torso.glb",
    "amr_chassis.usda", "rov_scene.usdc",
)  # fmt: skip


def data_of(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(data: bytes | str, **config: Any) -> SourceOutput:
    raw = data_of(data) if isinstance(data, str) else data
    return ingest_source(GeometryAdapter(), BytesReader(raw), config)


def codes(output: SourceOutput) -> set[str]:
    return {finding.code for finding in output.findings()}


def artifacts(output: SourceOutput) -> list[SpatialArtifact]:
    return [r for r in output.records() if isinstance(r, SpatialArtifact)]


def props(output: SourceOutput) -> dict[str, StructuredRecord]:
    (properties,) = (
        r
        for r in output.records()
        if isinstance(r, StructuredTable) and r.name == Known("geometry properties")
    )
    return {
        str(r.cells[0].value): r  # type: ignore[union-attr]
        for r in output.records()
        if isinstance(r, StructuredRecord) and r.table == properties.id
    }


def cells(output: SourceOutput, name: str) -> tuple[Any, ...]:
    return tuple(
        c.value if isinstance(c, Known) else type(c).__name__ for c in props(output)[name].cells[1:]
    )


def reference_scopes(output: SourceOutput) -> dict[str, str]:
    found = [
        r
        for r in output.records()
        if isinstance(r, StructuredTable) and r.name == Known("geometry dependencies")
    ]
    if not found:
        return {}
    return {
        str(r.cells[1].value): str(r.cells[2].value)  # type: ignore[union-attr]
        for r in output.records()
        if isinstance(r, StructuredRecord) and r.table == found[0].id
    }


def by_code(output: SourceOutput, code: str) -> list[Any]:
    return [f for f in output.findings() if f.code == code]


# --- Nothing readable ---------------------------------------------------------------------------


@pytest.mark.parametrize("data", [b"", b"\x00" * 7, b"hello, robot\n", b"\x7fELF" + bytes(200)])
def test_bytes_that_are_no_geometry_are_one_finding_and_no_record(data: bytes) -> None:
    output = run(data)
    assert artifacts(output) == []
    assert codes(output) == {"geometry.unreadable"}
    assert all(f.severity is Severity.ERROR for f in output.findings())


@pytest.mark.parametrize("name", VALID)
def test_every_truncation_of_every_fixture_is_findings_never_an_exception(name: str) -> None:
    data = data_of(name)
    for cut in sorted({0, 1, 3, 11, 19, 20, 50, 83, 84, 100, len(data) // 2, len(data) - 1}):
        if cut < len(data):
            run(data[:cut])  # must not raise; ingest_source also checks every law


@pytest.mark.parametrize("name", VALID)
def test_flipping_any_byte_never_raises(name: str) -> None:
    data = bytearray(data_of(name))
    for index in range(0, len(data), max(1, len(data) // 97)):
        flipped = bytearray(data)
        flipped[index] ^= 0xFF
        run(bytes(flipped))


# --- STL ----------------------------------------------------------------------------------------


def test_a_truncated_binary_stl_reads_the_facets_it_holds_and_says_it_is_cut() -> None:
    data = data_of("quadruped_foot.stl")[: 84 + 50 * 5 + 17]  # five whole facets and a bit
    output = run(data)
    assert cells(output, "declared_facet_count") == (12,)
    assert cells(output, "facet_count") == (5,)  # the declared and the measured are both kept
    assert {"geometry.truncated"} <= codes(output)
    (finding,) = by_code(output, "geometry.truncated")
    assert finding.category is FindingCategory.CORRUPT and finding.details["present"] == 5


def test_a_binary_stl_whose_count_lies_hugely_costs_nothing() -> None:
    data = bytearray(data_of("quadruped_foot.stl"))
    data[80:84] = struct.pack("<I", 0xFFFFFFFF)
    tracemalloc.start()
    output = run(bytes(data))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert cells(output, "declared_facet_count") == (0xFFFFFFFF,)
    assert cells(output, "facet_count") == (12,)
    assert "geometry.truncated" in codes(output)
    assert peak < 8 * 1024 * 1024


def test_a_binary_stl_with_bytes_after_its_facets_says_so() -> None:
    output = run(data_of("quadruped_foot.stl") + b"\x00" * 33)
    assert cells(output, "facet_count") == (12,)
    assert "geometry.count_mismatch" in codes(output)


def test_a_binary_stl_with_nan_vertices_leaves_them_out_of_the_bounds() -> None:
    data = bytearray(data_of("quadruped_foot.stl"))
    data[84 + 12 : 84 + 16] = struct.pack("<f", float("nan"))  # the first facet's first x
    output = run(bytes(data))
    assert "geometry.non_finite" in codes(output)
    assert cells(output, "bounds_min") == (-0.0625, -0.0625, 0.0)  # the other vertices decide


def test_a_binary_stl_over_max_vertices_is_not_scanned_and_its_bounds_are_not_covered() -> None:
    output = run("quadruped_foot.stl", max_vertices=10)
    assert cells(output, "facet_count") == (12,)  # the size says so, no scan needed
    assert cells(output, "bounds_min") == ("NotCovered",)
    (finding,) = by_code(output, "geometry.limit_exceeded")
    assert finding.details["option"] == "max_vertices"


def test_a_scan_budget_too_small_for_the_file_is_a_limit_and_not_covered() -> None:
    output = run("quadruped_foot.stl", max_scan_bytes=200)
    assert cells(output, "bounds_max") == ("NotCovered",)
    assert by_code(output, "geometry.limit_exceeded")[0].details["option"] == "max_scan_bytes"


def test_an_ascii_stl_with_no_endsolid_and_odd_vertices_is_findings() -> None:
    text = data_of("quadruped_hip.stl").decode().rsplit("endsolid", 1)[0]
    output = run(text.rsplit("vertex", 1)[0].encode())
    assert {"geometry.count_mismatch", "geometry.truncated"} <= codes(output)
    assert cells(output, "facet_count")[0] == 12


def test_an_ascii_stl_vertex_with_a_word_where_a_number_belongs_is_skipped_and_counted() -> None:
    text = data_of("quadruped_hip.stl").replace(b"vertex -1.25", b"vertex fish", 1)
    output = run(text)
    assert "geometry.malformed" in codes(output)


# --- PLY header lies ----------------------------------------------------------------------------


def ply(
    vertices: int, rows: bytes, *, form: str = "binary_little_endian", extra: str = ""
) -> bytes:
    header = (
        f"ply\nformat {form} 1.0\nelement vertex {vertices}\nproperty float x\nproperty float y\n"
        f"property float z\n{extra}end_header\n"
    )
    return header.encode() + rows


def test_a_ply_declaring_more_vertices_than_it_holds_keeps_both_numbers_and_says_truncated() -> (
    None
):
    rows = b"".join(struct.pack("<3f", i, i, i) for i in range(4))
    output = run(ply(1000, rows))
    assert cells(output, "declared_vertex_count") == (1000,)
    assert cells(output, "vertex_count") == (4,)
    assert cells(output, "bounds_max") == (3.0, 3.0, 3.0)
    assert {"geometry.truncated", "geometry.count_mismatch"} <= codes(output)


def test_a_ply_declaring_fewer_vertices_than_it_holds_still_measures_what_it_declares() -> None:
    rows = b"".join(struct.pack("<3f", i, i, i) for i in range(4))
    output = run(ply(2, rows))
    assert cells(output, "vertex_count") == (2,)
    assert cells(output, "bounds_max") == (1.0, 1.0, 1.0)


def test_a_ply_declaring_an_astronomical_count_costs_nothing() -> None:
    output = run(ply(10**18, b"\x00" * 24))
    assert cells(output, "declared_vertex_count") == (10**18,)
    assert cells(output, "vertex_count") == (2,)
    assert "geometry.truncated" in codes(output)


def test_an_ascii_ply_with_short_rows_and_garbage_is_findings_not_bounds_from_garbage() -> None:
    body = b"1 2 3\n4 5\nx y z\n7 8 9\n"
    output = run(ply(5, body, form="ascii"))
    assert cells(output, "bounds_min") == (1.0, 2.0, 3.0)
    assert cells(output, "bounds_max") == (7.0, 8.0, 9.0)
    assert {"geometry.malformed", "geometry.count_mismatch"} <= codes(output)


def test_a_ply_with_rows_after_the_declared_elements_says_so() -> None:
    output = run(ply(1, b"1 2 3\n4 5 6\n", form="ascii"))
    assert cells(output, "vertex_count") == (1,)
    assert "geometry.malformed" in codes(output)


def test_a_ply_header_with_no_end_header_is_unreadable_and_a_huge_one_is_a_limit() -> None:
    output = run(b"ply\nformat ascii 1.0\nelement vertex 1\nproperty float x\n")
    assert codes(output) == {"geometry.unreadable"}
    huge = b"ply\nformat ascii 1.0\n" + b"comment " + b"x" * 200 + b"\n"
    output = run(huge * 100, max_header_bytes=1024)
    assert codes(output) == {"geometry.limit_exceeded"}
    assert artifacts(output) == []


def test_a_ply_with_no_format_line_or_an_unknown_type_or_a_second_ply_is_findings() -> None:
    assert codes(run(b"ply\nelement vertex 1\nend_header\n1 2 3\n")) == {"geometry.unreadable"}
    odd = run(ply(1, b"\x00" * 12, extra="property quaternion q\n"))
    assert "geometry.malformed" in codes(odd)
    again = run(b"ply\nformat ascii 1.0\nformat binary_big_endian 1.0\nend_header\n")
    assert "geometry.malformed" in codes(again)


def test_a_binary_ply_with_a_list_before_the_vertices_does_not_guess_their_offset() -> None:
    header = (
        b"ply\nformat binary_little_endian 1.0\nelement face 1\n"
        b"property list uchar int vertex_indices\nelement vertex 1\nproperty float x\n"
        b"property float y\nproperty float z\nend_header\n"
    )
    output = run(header + b"\x03" + struct.pack("<3i", 0, 0, 0) + struct.pack("<3f", 1, 2, 3))
    assert cells(output, "vertex_count") == ("NotCovered",)
    assert cells(output, "bounds_min") == ("NotCovered",)
    assert "geometry.not_covered" in codes(output)


def test_a_ply_over_max_vertices_is_not_scanned() -> None:
    rows = b"".join(struct.pack("<3f", i, i, i) for i in range(20))
    output = run(ply(20, rows), max_vertices=5)
    assert cells(output, "bounds_min") == ("NotCovered",)
    assert by_code(output, "geometry.limit_exceeded")


def test_a_ply_with_a_nul_in_a_texture_name_is_not_a_dependency() -> None:
    output = run(ply(0, b"", extra="comment TextureFile a\x00b.png\n"))
    assert reference_scopes(output) == {}
    assert "geometry.reference_unsafe" in codes(output)


# --- glTF ---------------------------------------------------------------------------------------


def gltf(document: dict[str, Any]) -> bytes:
    return json.dumps(document).encode()


MINIMAL: Final = {"asset": {"version": "2.0"}}


def test_a_gltf_depth_bomb_is_a_limit_before_any_parse() -> None:
    bomb = b'{"asset":{"version":"2.0"},"extras":' + b"[" * 200_000 + b"]" * 200_000 + b"}"
    start = time.perf_counter()
    output = run(bomb)
    assert time.perf_counter() - start < 5
    assert codes(output) == {"geometry.limit_exceeded"}
    assert by_code(output, "geometry.limit_exceeded")[0].details["option"] == "max_json_depth"
    assert artifacts(output) == []


def test_brackets_inside_strings_do_not_count_as_depth() -> None:
    output = run(gltf({"asset": {"version": "2.0"}, "extras": {"note": "[" * 5000}}))
    assert artifacts(output) and codes(output) == set()


def test_a_gltf_over_max_json_bytes_is_a_limit() -> None:
    output = run(gltf({"asset": {"version": "2.0"}, "extras": "x" * 5000}), max_json_bytes=1000)
    assert codes(output) == {"geometry.limit_exceeded"}


BAD_JSON: Final = [
    b'{"asset":{"version":"2.0"},"x":NaN}',
    b'{"asset":{"version":"2.0"},"x":Infinity}',
    b'{"asset":{"version":"2.0"},"x":',
    b'\xef\xbb\xbf{"asset":{"version":"2.0"}}',
    b'{"asset":{"version":"2.0"}} trailing',
    b'[{"asset":{"version":"2.0"}}]',
    b'{"asset":{"version":"2.0"},"x":1' + b"0" * 5000 + b"}",
]


@pytest.mark.parametrize("text", BAD_JSON)
def test_json_that_is_not_a_gltf_object_is_unreadable_never_a_raise(text: bytes) -> None:
    output = run(text)
    assert artifacts(output) == [] and codes(output) == {"geometry.unreadable"}


def test_a_gltf_with_no_position_min_and_max_has_unknown_bounds_not_zero() -> None:
    doc = {
        **MINIMAL,
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
        "accessors": [{"count": 3, "componentType": 5126, "type": "VEC3"}],
    }
    output = run(gltf(doc))
    assert cells(output, "bounds_min") == ("Unknown",)
    assert cells(output, "vertex_count") == ("Unknown",) or cells(output, "vertex_count") == (3,)
    assert "geometry.malformed" in codes(output)


def test_a_gltf_pointing_a_primitive_at_a_missing_accessor_is_a_finding() -> None:
    doc = {
        **MINIMAL,
        "meshes": [{"primitives": [{"attributes": {"POSITION": 9}}]}],
        "accessors": [],
    }
    assert "geometry.malformed" in codes(run(gltf(doc)))


def test_a_gltf_whose_accessor_bounds_are_not_numbers_or_not_finite_is_unknown() -> None:
    text = (
        b'{"asset":{"version":"2.0"},"meshes":[{"primitives":[{"attributes":{"POSITION":0}}]}],'
        b'"accessors":[{"count":3,"min":["a",0,0],"max":[1,1e999,1]}]}'
    )
    output = run(text)
    assert cells(output, "bounds_max") == ("Unknown",)
    assert "geometry.malformed" in codes(output)


def test_two_position_accessors_union_their_bounds() -> None:
    doc = {
        **MINIMAL,
        "meshes": [
            {"primitives": [{"attributes": {"POSITION": 0}}, {"attributes": {"POSITION": 1}}]}
        ],
        "accessors": [
            {"count": 3, "min": [0, 0, 0], "max": [1, 1, 1]},
            {"count": 4, "min": [-2, 0.5, 0], "max": [0.5, 3, 1]},
        ],
    }
    output = run(gltf(doc))
    assert cells(output, "bounds_min") == (-2.0, 0.0, 0.0)
    assert cells(output, "bounds_max") == (1.0, 3.0, 1.0)
    assert cells(output, "vertex_count") == (7,)


def test_a_glb_whose_chunk_length_lies_or_whose_version_is_1_is_findings() -> None:
    glb = bytearray(data_of("humanoid_torso.glb"))
    lying = bytearray(glb)
    lying[12:16] = struct.pack("<I", 0x7FFFFFFF)
    out = run(bytes(lying))
    assert artifacts(out) == [] and {"geometry.truncated", "geometry.limit_exceeded"} & codes(out)
    old = bytearray(glb)
    old[4:8] = struct.pack("<I", 1)
    assert codes(run(bytes(old))) == {"geometry.unreadable"}
    short = bytearray(glb)
    short[8:12] = struct.pack("<I", 12)  # the total length lies
    out = run(bytes(short))
    assert artifacts(out) and "geometry.count_mismatch" in codes(out)


def test_a_glb_whose_first_chunk_is_not_json_is_unreadable() -> None:
    glb = bytearray(data_of("humanoid_torso.glb"))
    glb[16:20] = b"BIN\x00"
    assert codes(run(bytes(glb))) == {"geometry.unreadable"}


# --- References: classified, never opened -------------------------------------------------------


def obj_with(*libraries: bytes) -> bytes:
    return b"v 0 0 0\n" + b"".join(b"mtllib " + name + b"\n" for name in libraries)


def test_references_that_cannot_be_inside_the_source_root_are_findings_with_their_scope() -> None:
    names = [b"/etc/passwd", b"C:\\Users\\robot\\arm.mtl", b"\\\\server\\share\\a.mtl",
             b"http://example.com/a.mtl", b"file:///etc/shadow", b"s3://bucket/a.mtl"]  # fmt: skip
    output = run(obj_with(*names))
    scopes = reference_scopes(output)
    assert set(scopes.values()) == {"absolute", "uri"}
    assert len(scopes) == len(names)
    found = by_code(output, "geometry.reference_outside_root")
    assert len(found) == len(names)
    assert all(
        f.severity is Severity.WARNING and f.category is FindingCategory.SKIPPED for f in found
    )


def test_a_reference_that_climbs_out_of_its_directory_is_its_own_finding() -> None:
    output = run(obj_with(b"../../escape.mtl", b"a/../b.mtl", b"./ok.mtl", b"sub\\dir\\win.mtl"))
    scopes = reference_scopes(output)
    assert scopes == {
        "../../escape.mtl": "parent",
        "a/../b.mtl": "relative",
        "./ok.mtl": "relative",
        "sub\\dir\\win.mtl": "relative",
    }
    assert [f.code for f in output.findings()] == ["geometry.reference_leaves_directory"]
    assert not by_code(output, "geometry.reference_outside_root")


def test_a_reference_with_a_control_character_or_a_non_utf8_name_has_no_row() -> None:
    output = run(obj_with(b"a\x01b.mtl", b"bad\xff\xfe.mtl", b"   "))
    assert reference_scopes(output) == {}
    assert "geometry.reference_unsafe" in codes(output)


def test_a_reference_over_max_value_bytes_is_not_copied() -> None:
    output = run(obj_with(b"x" * 300 + b".mtl"), max_value_bytes=64)
    (row,) = (r for r in output.records() if isinstance(r, StructuredRecord) and len(r.cells) == 3)
    assert isinstance(row.cells[1], NotCovered)
    assert "geometry.limit_exceeded" in codes(output)


def test_many_references_stop_at_max_entries_and_the_rest_is_not_covered() -> None:
    output = run(obj_with(*[f"m{i}.mtl".encode() for i in range(50)]), max_entries=10)
    assert len(reference_scopes(output)) == 10
    assert by_code(output, "geometry.limit_exceeded")[0].details["option"] == "max_entries"
    assert cells(output, "vertex_count") == ("NotCovered",)


def test_a_gltf_reference_is_classified_by_its_percent_decoded_form() -> None:
    doc = {
        **MINIMAL,
        "buffers": [{"byteLength": 1, "uri": "%2E%2E/%2E%2E/secret.bin"}],
        "images": [{"uri": "https://cdn.example.com/t.png"}, {"uri": "%2Fetc%2Fpasswd"}],
    }
    output = run(gltf(doc))
    assert reference_scopes(output) == {
        "%2E%2E/%2E%2E/secret.bin": "parent",
        "https://cdn.example.com/t.png": "uri",
        "%2Fetc%2Fpasswd": "absolute",
    }


def test_a_reference_is_never_opened_even_when_it_names_a_real_file(tmp_path: Path) -> None:
    secret = tmp_path / "secret.mtl"
    secret.write_text("newmtl leak\n")
    output = run(obj_with(str(secret).encode(), b"link.mtl"))
    assert "leak" not in repr([r.to_json() for r in output.records()])
    assert reference_scopes(output)[str(secret)] == "absolute"


# --- OBJ ----------------------------------------------------------------------------------------


def test_obj_faces_naming_no_vertex_and_unknown_statements_are_counted_once_per_reason() -> None:
    data = b"v 0 0 0\nv 1 0 0\nf 1 2 9\nf 0 1 2\nf a b c\nzap 1 2\nzap 3\nv 1 2\n"
    output = run(data)
    found = by_code(output, "geometry.malformed")
    reasons = {f.details["reason"]: f.details["count"] for f in found}
    assert reasons == {
        "face corners naming no vertex read so far": 2,
        "face corners that are not an index": 1,
        "statements that are not OBJ keywords": 2,
        "vertices without three numbers were skipped": 1,
    }
    assert cells(output, "vertex_count") == (
        3,
    )  # the short vertex is counted as read, skipped in bounds
    assert cells(output, "face_count") == (3,)


def test_obj_free_form_statements_are_identified_and_not_decoded() -> None:
    output = run(b"v 0 0 0\ncstype bspline\ndeg 3\nsurf 0 1 0 1 1\nend\n")
    (found,) = by_code(output, "geometry.not_covered")
    assert found.details["statements"] == {"cstype": 1, "deg": 1, "end": 1, "surf": 1}


def test_an_obj_line_longer_than_the_cap_is_skipped_not_buffered() -> None:
    output = run(b"v 1 2 3\n" + b"v " + b"9" * 200_000 + b"\nv 4 5 6\n")
    assert cells(output, "vertex_count") == (2,)
    assert cells(output, "bounds_max") == (4.0, 5.0, 6.0)
    assert "geometry.malformed" in codes(output)


def test_an_obj_with_one_endless_line_is_bounded_in_memory_and_time() -> None:
    data = b"v 1 2 3\n" + b"#" * (20 * 1024 * 1024)
    tracemalloc.start()
    start = time.perf_counter()
    output = run(data)
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert cells(output, "vertex_count") == (1,)
    assert peak < 16 * 1024 * 1024 and elapsed < 10


def test_an_obj_over_max_vertices_stops_and_its_counts_and_bounds_are_not_covered() -> None:
    output = run(b"v 1 2 3\n" * 100, max_vertices=10)
    assert cells(output, "vertex_count") == ("NotCovered",)
    assert cells(output, "bounds_min") == ("NotCovered",)
    assert [a.category.value for a in artifacts(output)] == ["mesh"]


def test_an_obj_with_every_vertex_non_finite_has_no_bounds() -> None:
    output = run(b"v nan 0 0\nv inf 1 1\nv -inf 0 0\n")
    assert isinstance(props(output)["bounds_min"].cells[1], NotApplicable)
    (finding,) = by_code(output, "geometry.non_finite")
    assert finding.details["count"] == 3


def test_an_obj_with_crlf_line_ends_and_comments_after_statements_reads_the_same() -> None:
    plain = run(b"v 1 2 3\nv 4 5 6\nf 1 2 1\n")
    windows = run(b"v 1 2 3 # corner\r\nv 4 5 6\r\nf 1 2 1\r\n")
    assert cells(plain, "bounds_max") == cells(windows, "bounds_max") == (4.0, 5.0, 6.0)


# --- USD ----------------------------------------------------------------------------------------


def usda(block: str) -> bytes:
    return f'#usda 1.0\n(\n{block}\n)\ndef Xform "a" {{}}\n'.encode()


def test_usd_metadata_in_every_standard_scale_maps_to_its_unit_and_others_stay_unknown() -> None:
    expected = {
        "1": "m",
        "0.01": "cm",
        "0.001": "mm",
        "0.0254": "in",
        "0.3048": "ft",
        "1e-06": "um",
    }
    for scale, symbol in expected.items():
        unit = artifacts(run(usda(f"metersPerUnit = {scale}")))[0].unit
        assert isinstance(unit, Known) and dict(unit.value.factors) == {symbol: 1}, scale
    odd = run(usda("metersPerUnit = 0.5"))
    assert isinstance(artifacts(odd)[0].unit, Unknown)
    assert cells(odd, "meters_per_unit") == (0.5,)  # the declared number stays
    assert "geometry.unit_unmapped" in codes(odd)


def test_a_usd_layer_with_no_metadata_has_unknown_unit_and_axis_never_the_usd_fallback() -> None:
    output = run(b'#usda 1.0\ndef Xform "a" {}\n')
    assert isinstance(artifacts(output)[0].unit, Unknown)
    assert cells(output, "up_axis") == ("Unknown",)
    assert cells(output, "meters_per_unit") == ("Unknown",)


def test_usd_metadata_that_never_closes_is_a_finding_and_what_precedes_it_is_kept() -> None:
    output = run(b'#usda 1.0\n(\n    upAxis = "Z"\n    metersPerUnit = 1\n')
    assert cells(output, "up_axis") == ("Z",)
    assert "geometry.truncated" in codes(output)


def test_usd_metadata_nested_past_the_depth_limit_is_a_limit() -> None:
    deep = usda("customLayerData = " + "{" * 500 + "}" * 500)
    output = run(deep, max_json_depth=32)
    assert codes(output) == {"geometry.limit_exceeded"}


def test_a_usd_header_over_max_header_bytes_is_a_limit_not_a_scan() -> None:
    long = usda('doc = """' + "x" * 5000 + '"""')
    output = run(long, max_header_bytes=256)
    assert artifacts(output) == [] and codes(output) == {"geometry.limit_exceeded"}


def test_usd_asset_paths_hold_whatever_is_inside_the_at_signs() -> None:
    block = "subLayers = [@/abs/layer.usda@, @http://h/x.usda@, @@@odd@name.usda@@@, @a\x01b.usda@]"
    output = run(usda(block))
    scopes = reference_scopes(output)
    assert scopes["/abs/layer.usda"] == "absolute" and scopes["http://h/x.usda"] == "uri"
    assert "geometry.reference_unsafe" in codes(output)


def test_a_truncated_usdc_bootstrap_is_unreadable_and_a_full_one_is_identified() -> None:
    assert codes(run(b"PXR-USDC\x00\x08")) == {"geometry.unreadable"}
    assert codes(run(data_of("rov_scene.usdc"))) == {"geometry.not_covered"}


# --- The model's own rules and lineage ----------------------------------------------------------


def test_the_properties_of_every_valid_fixture_survive_a_strict_round_trip() -> None:
    from neptune.model.kinds import RECORD_KINDS

    for name in VALID:
        for record in run(name).records():
            _, read = RECORD_KINDS[record.kind]
            assert read(record.to_json()) == record


def test_one_corrupt_source_among_good_ones_does_not_stop_the_others() -> None:
    results = [
        run(data) for data in (data_of("arm_link.obj"), b"\x00" * 90, data_of("agv_fork.ply"))
    ]
    assert [bool(artifacts(r)) for r in results] == [True, False, True]


# --- Review findings: quadratic regexes, huge numbers, surrogates, whitespace, byte order marks -


def test_an_unterminated_json_string_full_of_escaped_quotes_costs_a_linear_scan() -> None:
    bomb = b'{"asset":{"version":"2.0"},"x":"' + b'\\"' * 2_000_000 + b"}"
    start = time.perf_counter()
    output = run(bomb)
    assert time.perf_counter() - start < 10
    assert codes(output) == {"geometry.unreadable"}


def test_an_unterminated_usd_string_full_of_escaped_quotes_costs_a_linear_scan() -> None:
    bomb = b'#usda 1.0\n(\n    doc = "' + b'\\"' * 400_000
    start = time.perf_counter()
    output = run(bomb)
    assert time.perf_counter() - start < 10
    assert "geometry.truncated" in codes(output) or "geometry.limit_exceeded" in codes(output)


def test_many_unterminated_usd_asset_paths_on_one_line_are_linear_too() -> None:
    bomb = b"#usda 1.0\n(\n    subLayers = [" + b"@a " * 300_000 + b"]\n)\n"
    start = time.perf_counter()
    run(bomb)
    assert time.perf_counter() - start < 10


def test_a_ply_element_count_of_thousands_of_digits_or_past_int64_is_unreadable_not_a_raise() -> (
    None
):
    for digits in ("9" * 5000, str(2**63), "9" * 19):
        output = run(f"ply\nformat ascii 1.0\nelement vertex {digits}\nend_header\n".encode())
        assert codes(output) == {"geometry.unreadable"}, digits[:20]


def test_a_ply_element_name_that_is_not_an_identifier_is_unreadable() -> None:
    output = run(b"ply\nformat ascii 1.0\nelement ve\xffrtex 1\nend_header\n1 2 3\n")
    assert codes(output) == {"geometry.unreadable"}


def test_a_gltf_integer_too_large_for_a_float_is_unknown_bounds_not_a_raise() -> None:
    big = b"9" * 400
    text = (
        b'{"asset":{"version":"2.0"},"meshes":[{"primitives":[{"attributes":{"POSITION":0}}]}],'
        b'"accessors":[{"count":3,"min":[0,0,0],"max":[1,1,' + big + b"]}]}"
    )
    output = run(text)
    assert cells(output, "bounds_max") == ("Unknown",)
    huge = b"1" + b"0" * 30
    text = (
        b'{"asset":{"version":"2.0"},"meshes":[{"primitives":[{"attributes":{"POSITION":0}}]}],'
        b'"accessors":[{"count":' + huge + b',"min":[0,0,0],"max":[1,1,1]}]}'
    )
    assert cells(run(text), "vertex_count") == ("Unknown",)


def test_a_json_string_with_a_lone_surrogate_or_a_control_character_is_not_a_name_or_a_row() -> (
    None
):
    document = (
        b'{"asset":{"version":"2.0"},"scenes":[{"name":"a\\ud800"}],'
        b'"buffers":[{"byteLength":1,"uri":"a\\ud800.bin"}],"images":[{"uri":"b\\u0000.png"}]}'
    )
    output = run(document)
    assert isinstance(artifacts(output)[0].name, Unknown)
    assert reference_scopes(output) == {}
    assert {"geometry.reference_unsafe", "geometry.malformed"} <= codes(output)
    long = json.dumps({"asset": {"version": "2.0"}, "scenes": [{"name": "n" * 100_000}]}).encode()
    assert isinstance(artifacts(run(long))[0].name, Unknown)
    nul = json.dumps({"asset": {"version": "2.0"}, "scenes": [{"name": "a\u0000b"}]}).encode()
    assert isinstance(artifacts(run(nul))[0].name, Unknown)


def test_a_glTF_finding_cites_the_json_it_is_about_not_byte_zero() -> None:
    glb = bytearray(data_of("humanoid_torso.glb"))
    document = json.loads(bytes(glb[20 : 20 + struct.unpack("<I", glb[12:16])[0]]))
    document["meshes"][0]["primitives"][0]["attributes"]["POSITION"] = 7
    text = json.dumps(document).encode()
    (found,) = by_code(run(text), "geometry.malformed")
    assert found.subject.locator[0].pointer == "/accessors"


def test_obj_statements_may_start_with_whitespace() -> None:
    output = run(b"  o  Arm link  \n\tv 1 2 3\n   mtllib   my arm.mtl  \n")
    assert artifacts(output)[0].name.known_or_raise() == "Arm link"
    assert reference_scopes(output) == {"my arm.mtl": "relative"}
    data = b"  o  Arm link  \n\tv 1 2 3\n   mtllib   my arm.mtl  \n"
    (row,) = (
        r for r in run(data).records() if isinstance(r, StructuredRecord) and len(r.cells) == 3
    )
    span = row.provenance.evidence.locator[0]
    assert data[span.offset : span.offset + span.length] == b"my arm.mtl"  # type: ignore[union-attr]


def test_a_byte_order_mark_is_not_text_in_obj_and_ascii_stl_and_not_json_in_gltf() -> None:
    bom = b"\xef\xbb\xbf"
    stl = run(bom + data_of("quadruped_hip.stl"))
    assert artifacts(stl)[0].name.known_or_raise() == "hip_abduction"
    assert codes(stl) == set()
    obj = run(bom + data_of("arm_link.obj"))
    assert artifacts(obj)[0].name.known_or_raise() == "shoulder_link"
    gltf = run(bom + data_of("marine_hull.gltf"))
    assert artifacts(gltf) == [] and codes(gltf) == {"geometry.unreadable"}


def test_a_usd_name_scale_or_axis_that_is_not_usable_is_unknown_not_a_raise() -> None:
    output = run(usda('defaultPrim = "a\x00b"\nupAxis = "Z\x01"\nmetersPerUnit = 1e999'))
    assert isinstance(artifacts(output)[0].name, Unknown)
    assert cells(output, "up_axis") == ("Unknown",)
    assert cells(output, "meters_per_unit") == ("Unknown",)
    assert isinstance(artifacts(output)[0].unit, Unknown)

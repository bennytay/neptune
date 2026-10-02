"""The geometry adapter on real files: what it records, how it cites it, and that it is repeatable.

The fixtures are written by ``tests/fixtures/geometry/make_geometry.py`` for robots of every kind (a
manipulator link, a quadruped's foot and hip, a mobile base, an AGV fork, an autonomous vehicle's
lidar scan, a marine hull, a humanoid torso, an AMR and an ROV scene). Agreement with independent
readers is in ``test_geometry_fixtures.py``; hostile input is in ``test_geometry_hostile.py``.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.contract import PROBE_HEAD_SIZE, ProbeHints
from neptune.adapters.geometry import DESCRIPTOR, GeometryAdapter
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.discovery.reader import BytesReader
from neptune.identity.canonical_json import dumps
from neptune.identity.hashing import content_id
from neptune.model.knowledge import (
    AssertionKind,
    Known,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import ByteRange, JsonPointer
from neptune.model.units import unit_from_text
from neptune.model.world import SpatialArtifact, SpatialCategory, StructuredRecord, StructuredTable

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "geometry"
ALL: Final = (
    "arm_link.obj", "quadruped_foot.stl", "quadruped_hip.stl", "mobile_base_chassis.ply",
    "agv_fork.ply", "av_lidar_scan.ply", "marine_hull.gltf", "humanoid_torso.glb",
    "amr_chassis.usda", "rov_scene.usdc",
)  # fmt: skip


def data_of(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(data: bytes | str, **config: Any) -> SourceOutput:
    raw = data_of(data) if isinstance(data, str) else data
    return ingest_source(GeometryAdapter(), BytesReader(raw), config)


def artifact(output: SourceOutput) -> SpatialArtifact:
    (found,) = (r for r in output.records() if isinstance(r, SpatialArtifact))
    return found


def table(output: SourceOutput, name: str) -> StructuredTable | None:
    found = [
        r for r in output.records() if isinstance(r, StructuredTable) and r.name == Known(name)
    ]
    return found[0] if found else None


def rows(output: SourceOutput, name: str) -> list[StructuredRecord]:
    found = table(output, name)
    assert found is not None, name
    return sorted(
        (r for r in output.records() if isinstance(r, StructuredRecord) and r.table == found.id),
        key=lambda r: r.row,
    )


def props(output: SourceOutput) -> dict[str, StructuredRecord]:
    return {str(r.cells[0].value): r for r in rows(output, "geometry properties")}  # type: ignore[union-attr]


def values(row: StructuredRecord) -> tuple[Any, ...]:
    """The known values after the property name, or the state's class name."""
    return tuple(c.value if isinstance(c, Known) else type(c).__name__ for c in row.cells[1:])


def deps(output: SourceOutput) -> list[tuple[Any, ...]]:
    if table(output, "geometry dependencies") is None:
        return []
    return [tuple(c.value for c in r.cells) for r in rows(output, "geometry dependencies")]  # type: ignore[union-attr]


def codes(output: SourceOutput) -> list[str]:
    return sorted(f.code for f in output.findings())


# --- What each file yields ----------------------------------------------------------------------

M = unit_from_text("m").known_or_raise()
CM = unit_from_text("cm").known_or_raise()


def test_a_manipulator_link_obj_is_counted_bounded_and_names_its_material_library() -> None:
    output = run("arm_link.obj")
    found = artifact(output)
    assert found.category is SpatialCategory.MESH
    assert found.name.known_or_raise() == "shoulder_link"
    assert isinstance(found.unit, Unknown)  # OBJ has no unit and none is guessed
    assert isinstance(found.crs, NotCovered) and isinstance(found.frame, NotCovered)
    p = props(output)
    assert values(p["vertex_count"]) == (8,)
    assert values(p["face_count"]) == (6,)
    assert values(p["object_count"]) == (1,)
    assert values(p["bounds_min"]) == (0.0, -0.0625, -0.0625)
    assert values(p["bounds_max"]) == (0.5, 0.0625, 0.0625)
    assert values(p["up_axis"]) == ("NotCovered",)
    assert deps(output) == [("material_library", "arm_link.mtl", "relative")]
    assert codes(output) == []


def test_a_quadruped_foot_binary_stl_has_a_measured_count_beside_the_declared_one() -> None:
    output = run("quadruped_foot.stl")
    found = artifact(output)
    assert isinstance(found.name, Unknown)  # the 80 header bytes are free text, not a name
    assert isinstance(found.unit, Unknown)  # STL has no unit
    p = props(output)
    assert values(p["encoding"]) == ("binary",)
    assert values(p["declared_facet_count"]) == (12,)
    assert values(p["facet_count"]) == (12,)
    assert values(p["bounds_min"]) == (-0.0625, -0.0625, 0.0)
    assert values(p["bounds_max"]) == (0.0625, 0.0625, 0.125)
    assert p["declared_facet_count"].provenance.assertion_kind is AssertionKind.STATED
    assert p["facet_count"].provenance.assertion_kind is AssertionKind.OBSERVED
    assert codes(output) == []


def test_a_quadruped_hip_ascii_stl_names_its_solid() -> None:
    output = run("quadruped_hip.stl")
    found = artifact(output)
    assert found.name.known_or_raise() == "hip_abduction"
    assert found.name.provenance.assertion_kind is AssertionKind.STATED  # type: ignore[union-attr]
    p = props(output)
    assert values(p["encoding"]) == ("ascii",)
    assert values(p["facet_count"]) == (12,)
    assert values(p["bounds_min"]) == (-0.125, -0.0625, -0.0625)
    assert isinstance(found.unit, Unknown)
    assert codes(output) == []


def test_a_mobile_base_ply_declares_counts_and_a_texture() -> None:
    output = run("mobile_base_chassis.ply")
    p = props(output)
    assert values(p["encoding"]) == ("ascii",)
    assert values(p["declared_vertex_count"]) == (8,) == values(p["vertex_count"])
    assert values(p["declared_face_count"]) == (6,) == values(p["face_count"])
    assert values(p["bounds_min"]) == (-0.25, -0.1875, 0.0)
    assert values(p["bounds_max"]) == (0.25, 0.1875, 0.125)
    assert artifact(output).category is SpatialCategory.MESH
    assert deps(output) == [("texture", "chassis_paint.png", "relative")]
    assert codes(output) == []


def test_an_agv_fork_binary_little_endian_ply_reads_past_its_extra_property() -> None:
    output = run("agv_fork.ply")
    p = props(output)
    assert values(p["encoding"]) == ("binary_little_endian",)
    assert values(p["vertex_count"]) == (8,)
    assert values(p["declared_face_count"]) == (6,)
    assert values(p["face_count"]) == ("NotCovered",)  # binary face lists are not read
    assert values(p["bounds_min"]) == (0.0, -0.0625, 0.0)
    assert values(p["bounds_max"]) == (1.0, 0.0625, 0.0625)


def test_a_lidar_scan_big_endian_ply_without_faces_is_a_point_cloud() -> None:
    output = run("av_lidar_scan.ply")
    assert artifact(output).category is SpatialCategory.POINT_CLOUD
    p = props(output)
    assert values(p["encoding"]) == ("binary_big_endian",)
    assert values(p["vertex_count"]) == (5,)
    assert values(p["bounds_min"]) == (-3.0, -4.0, 0.5)
    assert values(p["bounds_max"]) == (10.0, 2.0, 2.0)
    assert "declared_face_count" not in p


def test_a_marine_hull_gltf_states_metres_and_up_from_the_spec_and_bounds_from_its_accessors() -> (
    None
):
    output = run("marine_hull.gltf")
    found = artifact(output)
    assert found.name.known_or_raise() == "hull"
    unit = found.unit
    assert isinstance(unit, Known) and unit.value == M
    assert unit.provenance.assertion_kind is AssertionKind.STATED  # type: ignore[union-attr]
    p = props(output)
    assert values(p["format_version"]) == ("2.0",)
    assert values(p["up_axis"]) == ("Y",)
    assert values(p["vertex_count"]) == (8,)
    assert values(p["bounds_min"]) == (-1.5, -0.5, 0.0)
    assert values(p["bounds_max"]) == (1.5, 0.5, 0.75)
    assert p["bounds_min"].provenance.assertion_kind is AssertionKind.STATED
    assert values(p["mesh_count"]) == (1,) and values(p["image_count"]) == (3,)
    assert p["mesh_count"].provenance.assertion_kind is AssertionKind.OBSERVED
    assert values(p["embedded_resource_count"]) == (1,)  # the data: URI is not a dependency
    assert sorted(deps(output)) == [
        ("buffer", "hull.bin", "relative"),
        ("texture", "../shared/barnacle_mask.png", "parent"),
        ("texture", "textures/hull%20diffuse.png", "relative"),
    ]
    assert codes(output) == ["geometry.reference_leaves_directory"]


def test_a_humanoid_torso_glb_is_read_from_its_json_chunk_alone() -> None:
    output = run("humanoid_torso.glb")
    found = artifact(output)
    assert found.name.known_or_raise() == "torso"
    p = props(output)
    assert values(p["encoding"]) == ("glb",)
    assert values(p["bounds_max"]) == (1.5, 0.5, 0.75)
    assert deps(output) == []  # the buffer is the GLB's own chunk: it has no uri
    locator = found.name.provenance.evidence.locator  # type: ignore[union-attr]
    assert isinstance(locator[0], ByteRange) and locator[0].offset == 20
    assert locator[1] == JsonPointer("/scenes/0/name")
    assert codes(output) == []


def test_an_amr_usda_states_its_scale_up_axis_name_and_sublayers_from_the_header_block_only() -> (
    None
):
    output = run("amr_chassis.usda")
    found = artifact(output)
    assert found.category is SpatialCategory.SCENE
    assert found.name.known_or_raise() == "chassis"
    unit = found.unit
    assert isinstance(unit, Known) and unit.value == CM
    p = props(output)
    assert values(p["up_axis"]) == ("Z",)  # not the "Y" in the doc string
    assert values(p["meters_per_unit"]) == (0.01,)  # not the 1 in the doc string
    assert values(p["vertex_count"]) == ("NotCovered",)  # prims are not read
    assert sorted(deps(output)) == [
        ("sublayer", "../shared/wheels.usda", "parent"),
        ("sublayer", "./materials/steel.usda", "relative"),
    ]
    assert codes(output) == ["geometry.not_covered", "geometry.reference_leaves_directory"]


def test_an_rov_usdc_is_identified_and_everything_inside_it_is_not_covered() -> None:
    output = run("rov_scene.usdc")
    found = artifact(output)
    assert found.category is SpatialCategory.SCENE
    assert isinstance(found.name, NotCovered) and isinstance(found.unit, NotCovered)
    p = props(output)
    assert values(p["encoding"]) == ("usdc",)
    assert values(p["format_version"]) == ("0.8.0",)
    for name in ("up_axis", "vertex_count", "bounds_min", "bounds_max"):
        assert values(p[name]) == ("NotCovered",)
    assert codes(output) == ["geometry.not_covered"]


def test_no_vertices_have_no_bounds_which_is_not_applicable_not_zero() -> None:
    output = run(b"# nothing yet\nmtllib a.mtl\nv 1 2\nv 1 2 3\n")  # one vertex has two numbers
    p = props(output)
    assert values(p["vertex_count"]) == (2,)
    assert values(p["bounds_min"]) == (1.0, 2.0, 3.0)
    empty = run(b"o thing\nf 1 2 3\n" + b"v nan nan nan\n")
    q = props(empty)
    assert isinstance(q["bounds_min"].cells[1], NotApplicable)
    assert "geometry.non_finite" in codes(empty)


# --- Citations: the geometry stays in the source ------------------------------------------------


@pytest.mark.parametrize("name", ALL)
def test_the_artifact_cites_the_whole_file_which_is_the_lazy_handle_to_the_raw_geometry(
    name: str,
) -> None:
    data = data_of(name)
    output = run(name)
    found = artifact(output)
    evidence = found.provenance.evidence
    assert evidence.source == content_id(data)  # the resource identity: sha256 of the bytes
    assert evidence.locator == (ByteRange(0, len(data)),)
    assert found.provenance.assertion_kind is AssertionKind.OBSERVED
    for record in output.records():  # nothing is copied: every row cites bytes that exist
        assert record.provenance.evidence.source == evidence.source


def test_a_measured_property_cites_the_vertex_bytes_it_was_measured_from() -> None:
    data = data_of("quadruped_foot.stl")
    p = props(run("quadruped_foot.stl"))
    body = p["bounds_min"].provenance.evidence.locator[0]
    assert body == ByteRange(84, 12 * 50)  # the facet array, not the 80-byte header
    declared = p["declared_facet_count"].provenance.evidence.locator[0]
    assert declared == ByteRange(80, 4)
    assert len(data) == 84 + 12 * 50
    ply = props(run("agv_fork.ply"))
    assert ply["bounds_max"].provenance.evidence.locator[0].length == 8 * 13  # type: ignore[union-attr]


def test_a_dependency_cites_the_exact_bytes_that_name_it() -> None:
    data = data_of("arm_link.obj")
    (row,) = rows(run("arm_link.obj"), "geometry dependencies")
    span = row.provenance.evidence.locator[0]
    assert isinstance(span, ByteRange)
    assert data[span.offset : span.offset + span.length] == b"arm_link.mtl"
    gltf = data_of("marine_hull.gltf")
    pointers = {
        r.provenance.evidence.locator[0].pointer  # type: ignore[union-attr]
        for r in rows(run(gltf), "geometry dependencies")
    }
    assert pointers == {"/buffers/0/uri", "/images/0/uri", "/images/1/uri"}


# --- Probing ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL)
def test_probe_recognises_the_bytes_whatever_the_name(name: str) -> None:
    data = data_of(name)
    result = GeometryAdapter().probe(data[:PROBE_HEAD_SIZE], ProbeHints("", len(data)))
    assert result.confidence >= 0.7, name


@pytest.mark.parametrize("name", ALL)
def test_no_other_adapter_ties_with_geometry_on_its_own_files(name: str) -> None:
    data = data_of(name)
    scores = sorted(
        (a.probe(data[:PROBE_HEAD_SIZE], ProbeHints(name, len(data))).confidence, a.descriptor.id)
        for a in builtin_adapters()
    )
    assert scores[-1][1] == "geometry" and scores[-2][0] < scores[-1][0], scores


def test_probe_declines_other_text_and_binary_and_never_decides_from_the_name_alone() -> None:
    adapter = GeometryAdapter()
    for head in (b"hello, robot\n", b"\x7fELF" + bytes(120), b"a,b,c\n1,2,3\n", b"v 1 2\n"):
        assert adapter.probe(head, ProbeHints("x", len(head))).confidence == 0.0, head
    assert adapter.probe(b"", ProbeHints("empty.obj", 0)).confidence == 0.0
    json_not_gltf = b'{"asset": {"copyright": "me"}, "meshes": []}'
    assert adapter.probe(json_not_gltf, ProbeHints("x.json", len(json_not_gltf))).confidence == 0.0
    # a name is the last resort, for a non-empty file nothing else explains
    junk = bytes(range(256))
    assert adapter.probe(junk, ProbeHints("foot.stl", len(junk))).confidence == 0.1


def test_a_binary_stl_that_starts_with_solid_is_still_binary() -> None:
    data = bytearray(data_of("quadruped_foot.stl"))
    data[:5] = b"solid"
    output = run(bytes(data))
    assert values(props(output)["encoding"]) == ("binary",)
    assert values(props(output)["facet_count"]) == (12,)


def test_a_renamed_or_one_line_gltf_is_still_gltf() -> None:
    document = data_of("marine_hull.gltf").replace(b"\n", b"").replace(b"  ", b"")
    output = run(document)
    assert values(props(output)["encoding"]) == ("gltf",)


def test_inspect_reads_the_head_alone() -> None:
    adapter = GeometryAdapter()
    from neptune.adapters.contract import configure

    result = adapter.inspect(BytesReader(data_of("humanoid_torso.glb")), configure(DESCRIPTOR))
    assert result.summary == {
        "format": "glb",
        "size": len(data_of("humanoid_torso.glb")),
        "version": "2",
    }
    assert result.findings == ()


# --- Determinism and lineage --------------------------------------------------------------------


def dump(output: SourceOutput) -> bytes:
    return dumps([r.to_json() for r in output.package_records()])


@pytest.mark.parametrize("name", ALL)
def test_the_same_bytes_give_byte_identical_records_and_findings(name: str) -> None:
    assert dump(run(name)) == dump(run(name))
    assert dump(run(name)) == dump(run(data_of(name)))  # the name plays no part


def test_a_changed_config_value_is_a_new_transform_and_new_ids_for_the_same_bytes() -> None:
    first, second = run("arm_link.obj"), run("arm_link.obj", max_vertices=64)
    assert first.config.transform.id != second.config.transform.id
    assert {r.id for r in first.records()}.isdisjoint({r.id for r in second.records()})


def test_a_new_adapter_version_is_a_new_lineage_and_leaves_the_old_untouched() -> None:
    class Upgraded(GeometryAdapter):
        descriptor = replace(DESCRIPTOR, version="0.2.0")

    data = data_of("quadruped_foot.stl")
    old = run(data)
    before = dump(old)
    new = ingest_source(Upgraded(), BytesReader(data))
    assert new.config.transform.adapter_version == "0.2.0"
    assert not {r.id for r in old.records()} & {r.id for r in new.records()}
    assert dump(old) == before


def test_one_changed_vertex_changes_the_source_and_every_citing_record() -> None:
    data = bytearray(data_of("quadruped_foot.stl"))
    data[100] ^= 0x01
    first, second = run("quadruped_foot.stl"), run(bytes(data))
    assert {r.id for r in first.records()}.isdisjoint({r.id for r in second.records()})


def test_every_finding_code_the_adapter_makes_is_documented() -> None:
    documented = {d.name for d in DESCRIPTOR.finding_codes}
    for name in ALL:
        assert set(codes(run(name))) <= documented

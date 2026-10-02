"""The geometry fixtures against independent readers (``tests/fixtures/geometry/oracle.json``).

trimesh read the OBJ, STL, PLY and GLB files, usd-core the USD layer and the standard library the
glTF JSON, when ``oracle.json`` was written. The adapter's numbers must be theirs, and the
committed files must be exactly what ``make_geometry.py`` writes.
"""

import importlib.util
import json
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.geometry import GeometryAdapter
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.discovery.reader import BytesReader
from neptune.model.knowledge import Known
from neptune.model.world import StructuredRecord, StructuredTable

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "geometry"
ORACLE: Final = json.loads((FIXTURES / "oracle.json").read_text())


def run(name: str) -> SourceOutput:
    return ingest_source(GeometryAdapter(), BytesReader((FIXTURES / name).read_bytes()))


def values(output: SourceOutput, name: str) -> tuple[Any, ...]:
    (table,) = (
        r
        for r in output.records()
        if isinstance(r, StructuredTable) and r.name == Known("geometry properties")
    )
    for r in output.records():
        if isinstance(r, StructuredRecord) and r.table == table.id and r.cells[0].value == name:  # type: ignore[union-attr]
            return tuple(c.value for c in r.cells[1:])  # type: ignore[union-attr]
    raise KeyError(name)


@pytest.mark.parametrize("name", sorted(ORACLE["trimesh"]))
def test_bounds_and_counts_are_what_trimesh_reads(name: str) -> None:
    expected = ORACLE["trimesh"][name]
    output = run(name)
    low, high = expected["bounds"]
    assert list(values(output, "bounds_min")) == low
    assert list(values(output, "bounds_max")) == high
    if name.endswith(".stl"):  # trimesh counts a triangle soup's corners; the facet is the unit
        assert values(output, "facet_count") == (expected["faces"],)
        assert expected["vertices"] == 3 * expected["faces"]
    else:
        assert values(output, "vertex_count") == (expected["vertices"],)


def test_the_quad_faces_of_the_obj_and_ply_are_the_triangles_trimesh_splits_them_into() -> None:
    assert values(run("arm_link.obj"), "face_count") == (
        ORACLE["trimesh"]["arm_link.obj"]["faces"] // 2,
    )
    ply = run("mobile_base_chassis.ply")
    assert values(ply, "face_count") == (
        ORACLE["trimesh"]["mobile_base_chassis.ply"]["faces"] // 2,
    )


def test_the_usd_header_is_what_the_official_reader_reports() -> None:
    expected = ORACLE["usd"]["amr_chassis.usda"]
    output = run("amr_chassis.usda")
    assert values(output, "up_axis") == (expected["up_axis"],)
    assert values(output, "meters_per_unit") == (expected["meters_per_unit"],)
    from neptune.model.world import SpatialArtifact

    (artifact,) = (r for r in output.records() if isinstance(r, SpatialArtifact))
    assert artifact.name.known_or_raise() == expected["default_prim"]
    targets = {
        r.cells[1].value  # type: ignore[union-attr]
        for r in output.records()
        if isinstance(r, StructuredRecord) and len(r.cells) == 3 and r.cells[0].value == "sublayer"  # type: ignore[union-attr]
    }
    assert targets == set(expected["sublayers"])


def test_the_gltf_accessor_is_what_the_json_says() -> None:
    expected = ORACLE["gltf_json"]["marine_hull.gltf"]
    output = run("marine_hull.gltf")
    assert values(output, "vertex_count") == (expected["count"],)
    assert list(values(output, "bounds_min")) == expected["min"]
    assert list(values(output, "bounds_max")) == expected["max"]


def test_the_committed_fixtures_are_what_the_generator_writes() -> None:
    spec = importlib.util.spec_from_file_location("make_geometry", FIXTURES / "make_geometry.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, build in module.FILES.items():
        assert (FIXTURES / name).read_bytes() == build(), name
    assert sum(path.stat().st_size for path in FIXTURES.iterdir()) < 64 * 1024

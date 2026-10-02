"""Probing by structure, decimal numbers only, and cheaper lines (review of MVL-32).

A probe must not claim what other adapters should read (config JSON, prose), the formats'
numbers are decimal text and nothing Python's ``float`` happens to accept, and a file of short
lines costs about what its size says.
"""

import time
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.contract import PROBE_HEAD_SIZE, ProbeHints
from neptune.adapters.geometry import GeometryAdapter
from neptune.adapters.geometry._scan import BLOCK, Scanner
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.discovery.reader import BytesReader
from neptune.model.knowledge import Known
from neptune.model.world import SpatialArtifact, StructuredRecord, StructuredTable

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "geometry"


def run(data: bytes, **config: Any) -> SourceOutput:
    return ingest_source(GeometryAdapter(), BytesReader(data), config)


def probe(data: bytes, name: str = "") -> float:
    return GeometryAdapter().probe(data[:PROBE_HEAD_SIZE], ProbeHints(name, len(data))).confidence


def cells(output: SourceOutput, name: str) -> tuple[Any, ...]:
    (table,) = (
        r
        for r in output.records()
        if isinstance(r, StructuredTable) and r.name == Known("geometry properties")
    )
    for r in output.records():
        if isinstance(r, StructuredRecord) and r.table == table.id and r.cells[0].value == name:  # type: ignore[union-attr]
            return tuple(c.value if isinstance(c, Known) else type(c).__name__ for c in r.cells[1:])
    raise KeyError(name)


def codes(output: SourceOutput) -> set[str]:
    return {f.code for f in output.findings()}


# --- glTF is a top-level asset with a 2.x version ---------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        b'{"project":{"asset":{"version":"2.1"}}}',
        b'{"fleet":[{"asset":{"version":"2.0"}}]}',
        b'{"note":"\\"asset\\":{\\"version\\":\\"2.0\\"}","x":1}',
        b'{"asset":"2.0","version":"2.0"}',
        b'{"asset":{"version":"1.0"}}',
        b'{"asset":{"version":2}}',
        b'{"asset":{"id":1},"meta":{"version":"2.0"}}',
        b'{"asset":{"version":"2.0"',  # the head ends inside the asset object
    ],
)
def test_json_is_not_gltf_without_a_top_level_asset_whose_version_is_2_x(document: bytes) -> None:
    assert probe(document, "config.json") == 0.0


def test_gltf_is_claimed_when_its_asset_follows_other_keys_or_the_document_outgrows_the_head() -> (
    None
):
    assert probe(b'{"name":"fleet","asset":{"generator":"x","version":"2.1"},"scenes":[]}') == 0.9
    whole = data_of("marine_hull.gltf")
    cut = whole.index(b'"scene"')
    padded = whole[:cut] + b'"extras":"' + b"x" * 70_000 + b'",' + whole[cut:]
    assert len(padded) > PROBE_HEAD_SIZE
    assert probe(padded) == 0.9


def test_config_json_that_resembles_gltf_goes_to_the_config_adapter_and_is_not_lost() -> None:
    document = b'{"project":{"asset":{"version":"2.1"}},"name":"fleet"}'
    scores = {
        a.descriptor.id: a.probe(document, ProbeHints("config.json", len(document))).confidence
        for a in builtin_adapters()
    }
    assert scores["geometry"] == 0.0 and scores["config"] > 0.0


def test_glb_is_still_claimed_by_its_magic() -> None:
    assert probe(data_of("humanoid_torso.glb")) == 1.0


def data_of(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# --- ASCII STL is solid, then a facet or endsolid ---------------------------------------------


@pytest.mark.parametrize(
    "prose",
    [
        b"Solidarity matters: every facet of the fleet plan was reviewed.\n",
        b"Solid-state drives\nfacet normal is a term of art\n",
        b"solidify\nendsolid\n",
        b"solid\nthe facet of it\n",
        b"solid state\n\n\nplain words, then facet normal later\n",
        b"solid x\n",
    ],
)
def test_prose_is_not_an_ascii_stl(prose: bytes) -> None:
    assert probe(prose, "plan.md") == 0.0
    assert [
        a.descriptor.id
        for a in builtin_adapters()
        if a.probe(prose, ProbeHints("p.md", len(prose))).confidence > 0
    ] != ["geometry"]


@pytest.mark.parametrize(
    "text",
    [
        b"solid\nfacet normal 0 0 1\n",
        b"solid x\r\n  facet normal 0 0 1\n",
        b"SOLID part\n\nendsolid part\n",
        b"solid\tpart\nendsolid\n",
    ],
)
def test_an_ascii_stl_is_solid_then_whitespace_or_end_of_line_then_a_facet_or_endsolid(
    text: bytes,
) -> None:
    assert probe(text) == 0.7


def test_the_lenient_reader_of_a_damaged_stl_still_needs_solid_as_a_word() -> None:
    assert run(b"solidify the plan\n") and codes(run(b"solidify the plan\n")) == {
        "geometry.unreadable"
    }


# --- Numbers are decimal text -----------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        b"1_0",
        b"nan",
        b"inf",
        b"infinity",
        b"0x10",
        b"1,5",
        b"--1",
        b"1e",
        b"+",
        b".",
        b"\xef\xbc\x91",
    ],
)
def test_a_token_that_is_not_decimal_text_is_not_a_number_in_obj(token: bytes) -> None:
    output = run(b"v 1 2 3\nv " + token + b" 2 3\nv 4 5 6\n")
    assert cells(output, "vertex_count") == (3,)
    assert cells(output, "bounds_min") == (1.0, 2.0, 3.0)  # nothing entered the bounds from it
    assert cells(output, "bounds_max") == (4.0, 5.0, 6.0)
    (finding,) = (f for f in output.findings() if f.code == "geometry.malformed")
    assert finding.details["count"] == 1


@pytest.mark.parametrize(
    "token", [b"1", b"-1", b"+1", b"1.", b".5", b"-.5", b"1.5e3", b"1E-3", b"007"]
)
def test_every_decimal_form_is_still_a_number(token: bytes) -> None:
    assert codes(run(b"v 1 2 3\nv " + token + b" 2 3\n")) == set()


def test_underscores_are_not_numbers_in_stl_ply_faces_or_usd() -> None:
    hip = data_of("quadruped_hip.stl")
    stl = run(hip.replace(b"vertex -1.250000e-01", b"vertex -1_250000e-01", 1))
    assert "geometry.malformed" in codes(stl)
    header = b"ply\nformat ascii 1.0\nelement vertex 2\nproperty float x\nproperty float y\n"
    ply = run(header + b"property float z\nend_header\n1_0 2 3\n4 5 6\n")
    assert cells(ply, "bounds_min") == (4.0, 5.0, 6.0) and "geometry.malformed" in codes(ply)
    assert "geometry.malformed" in codes(run(b"v 0 0 0\nv 1 0 0\nf 1_0 1 1\n"))
    usd = run(b"#usda 1.0\n(\n    metersPerUnit = 1_0\n)\n")
    assert cells(usd, "meters_per_unit") == ("Unknown",)


# --- USD sublayers are never rewritten --------------------------------------------------------


def test_a_usd_sublayer_that_is_not_utf8_is_unsafe_not_rewritten_with_replacement_characters() -> (
    None
):
    output = run(b"#usda 1.0\n(\n    subLayers = [@ok.usda@, @bad\xff\xfe.usda@]\n)\n")
    targets = [
        r.cells[1].value  # type: ignore[union-attr]
        for r in output.records()
        if isinstance(r, StructuredRecord) and len(r.cells) == 3
    ]
    assert targets == ["ok.usda"]
    assert "geometry.reference_unsafe" in codes(output)
    assert all("�" not in repr(r.to_json()) for r in output.records())


# --- A huge integer spoils its value, not the file --------------------------------------------


def test_a_4301_digit_integer_anywhere_in_a_gltf_is_one_unknown_value_not_an_unreadable_file() -> (
    None
):
    doc = (
        b'{"asset":{"version":"2.0"},"extras":{"n":1' + b"0" * 5000 + b"},"
        b'"meshes":[{"primitives":[{"attributes":{"POSITION":0}}]}],'
        b'"accessors":[{"count":3,"min":[0,0,0],"max":[1,1,1' + b"0" * 5000 + b"]}]}"
    )
    output = run(doc)
    assert [a for a in output.records() if isinstance(a, SpatialArtifact)]
    assert cells(output, "bounds_max") == ("Unknown",)
    assert "geometry.unreadable" not in codes(output)


# --- Cheaper lines, and a bound on what a file of them can cost -------------------------------


@pytest.mark.parametrize("ends", [b"\n", b"\r\n", b"\r"])
def test_a_file_of_bare_line_ends_is_skipped_not_walked_line_by_line(ends: bytes) -> None:
    data = b"v 1 2 3\n" + ends * (8 << 20) + b"v 4 5 6\n"
    start = time.process_time()
    output = run(data)
    assert time.process_time() - start < 5
    assert cells(output, "vertex_count") == (2,)
    assert cells(output, "bounds_max") == (4.0, 5.0, 6.0)


def test_tiny_lines_are_charged_a_minimum_each_so_the_scan_budget_bounds_the_work() -> None:
    data = b"v 1 2 3\n" + b"o\n" * (6 << 20)  # 12 MiB of text, 6M lines: charged 32 bytes each
    start = time.process_time()
    output = run(data, max_scan_bytes=64 << 20)
    assert time.process_time() - start < 30
    (finding,) = (f for f in output.findings() if f.code == "geometry.limit_exceeded")
    assert finding.details["option"] == "max_scan_bytes"
    assert cells(output, "vertex_count") == ("NotCovered",)


@pytest.mark.parametrize("shift", range(0, 12))
def test_lines_are_cited_exactly_whatever_the_line_ends_and_block_boundaries(shift: int) -> None:
    body = b"alpha\r\n\r\nbeta\n\n\ngamma\rdelta\nlast"
    filler = b"abcdefg\n" * (BLOCK // 8 - 2) + b"q" * shift + b"\n"  # the body straddles a block
    data = filler + body
    found = list(Scanner(BytesReader(data), 1 << 30).lines(0, len(data)))
    for line in found:
        assert line.data and data[line.offset : line.offset + len(line.data)] == line.data
    assert [line.data for line in found][-5:] == [b"alpha", b"beta", b"gamma", b"delta", b"last"]


def test_an_overlong_line_spanning_blocks_is_reported_once_and_the_next_line_is_read() -> None:
    data = b"v 1 2 3\n" + b"9" * (3 * BLOCK) + b"\nv 4 5 6\n"
    found = list(Scanner(BytesReader(data), 1 << 30).lines(0, len(data)))
    assert [(line.overlong, line.data) for line in found] == [
        (False, b"v 1 2 3"),
        (True, b""),
        (False, b"v 4 5 6"),
    ]

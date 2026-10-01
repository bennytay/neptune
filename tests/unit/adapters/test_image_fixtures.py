"""The image fixtures are what their generator writes, and an independent reader agrees with them.

``oracle.json`` is Pillow's reading of every valid fixture (``make_images.py --oracle``, run with
``uv run --no-project --with pillow``; Pillow is not a Neptune dependency). The adapter's tables and
images must agree with it on size, EXIF, GPS, ICC and XMP. Pillow has no PAM reader, so
``gripper.pam`` is checked against the Netpbm specification in the adapter tests instead.
"""

import importlib.util
import json
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.image import ImageAdapter
from neptune.discovery.reader import BytesReader
from neptune.model.knowledge import Known
from neptune.model.provenance import ByteRange
from neptune.model.world import Image, StructuredRecord, StructuredTable

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "image"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_images", FIXTURES / "make_images.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR: Final = _generator()
ORACLE: Final[dict[str, Any]] = json.loads((FIXTURES / "oracle.json").read_text())
RATIONAL: Final = (5, 10)  # EXIF types RATIONAL and SRATIONAL


def run(name: str) -> SourceOutput:
    return ingest_source(ImageAdapter(), BytesReader((FIXTURES / name).read_bytes()), {})


def tables(output: SourceOutput) -> dict[str, list[list[Any]]]:
    """Each table's rows' cell values by table name (the first table of a name)."""
    records = output.records()
    found: dict[str, list[list[Any]]] = {}
    for table in (r for r in records if isinstance(r, StructuredTable)):
        assert isinstance(table.name, Known)
        rows = sorted(
            (r for r in records if isinstance(r, StructuredRecord) and r.table == table.id),
            key=lambda row: row.row,
        )
        cells = [[c.value if isinstance(c, Known) else None for c in row.cells] for row in rows]
        found.setdefault(table.name.value, cells)
    return found


def called(table: StructuredTable, name: str) -> bool:
    return isinstance(table.name, Known) and table.name.value == name


def stored(row: list[Any]) -> list[Any]:
    """An IFD row's values as the oracle lists them: rationals as Fractions."""
    _, kind, _, *values = row
    if kind in RATIONAL:
        return [Fraction(n, d) for n, d in zip(values[::2], values[1::2], strict=True)]
    return values


def oracle_values(kind: int, value: Any) -> list[Any]:
    if kind in RATIONAL:
        pairs = value if isinstance(value[0], list) else [value]
        return [Fraction(n, d) for n, d in pairs]
    return list(value) if isinstance(value, list) else [value]


# --- The committed files are the generator's ------------------------------------------


def test_every_committed_fixture_is_what_the_generator_writes() -> None:
    built = GENERATOR.build()
    for name, data in built.items():
        assert (FIXTURES / name).read_bytes() == data, name
    committed = {
        p.name
        for p in FIXTURES.iterdir()
        if p.is_file() and p.name not in ("make_images.py", "oracle.json")
    }
    assert committed == set(built)


def test_the_generator_is_deterministic() -> None:
    assert GENERATOR.build() == GENERATOR.build()


def test_every_fixture_is_small_and_the_robots_vary() -> None:
    assert all(p.stat().st_size < 64 * 1024 for p in FIXTURES.iterdir() if p.is_file())
    makers = {
        tables(run(name))["IFD0"][0][3]
        for name in ("crawler_inspection.jpg", "rov_survey.tif", "rover_raw.dng")
    }
    assert len(makers) == 3  # a pipe crawler, an ROV, a field rover: three makers


# --- Agreement with Pillow ------------------------------------------


@pytest.mark.parametrize("name", sorted(ORACLE))
def test_the_dimensions_and_encoding_match_the_reference_reader(name: str) -> None:
    images = [r for r in run(name).records() if isinstance(r, Image)]
    expected = ORACLE[name]
    sizes = [(i.width, i.height) for i in images]
    assert tuple(expected["decoded_size"]) in sizes
    assert all(tuple(frame) in sizes for frame in expected["frames"])
    assert {i.encoding for i in images} <= {
        "png", "jpeg", "tiff", "dng", "webp", "bmp", "pbm", "pgm", "ppm", "pam"
    }  # fmt: skip


@pytest.mark.parametrize("name", sorted(ORACLE))
def test_ifd_values_match_the_reference_reader(name: str) -> None:
    found = tables(run(name))
    expected = ORACLE[name]
    for table, tags in (("IFD0", expected["ifd0"]), ("Exif", expected["exif"]),
                        ("GPS", expected["gps"])):  # fmt: skip
        rows = {row[0]: row for row in found.get(table, [])}
        for tag, value in tags.items():
            assert int(tag) in rows, f"{name}: {table} tag {tag}"
            row = rows[int(tag)]
            if len(row) == 3:
                continue  # a MakerNote or an embedded profile: cited in the bytes, not copied
            if row[1] == 2:  # ASCII: one cell per string
                assert value.rstrip("\x00") in [str(c) for c in row[3:]], f"{name}: {table} {tag}"
            elif row[1] in (1, 3, 4, 5, 7, 8, 9, 10):
                assert stored(row) == oracle_values(row[1], value), f"{name}: {table} {tag}"


@pytest.mark.parametrize("name", sorted(ORACLE))
def test_icc_and_xmp_sizes_match_the_reference_reader(name: str) -> None:
    output = run(name)
    found = tables(output)
    expected = ORACLE[name]
    silent = expected["format"] == "BMP"  # Pillow reads no profile or XMP from a BMP
    if expected["icc_bytes"]:
        assert found["ICC header"][0][0] == expected["icc_bytes"]
    elif not silent:
        assert "ICC header" not in found
    xmp = [r for r in output.records() if isinstance(r, StructuredTable) and called(r, "XMP")]
    if expected["xmp_bytes"]:
        (packet,) = xmp
        ranges = [s for s in packet.provenance.evidence.locator if isinstance(s, ByteRange)]
        assert ranges[-1].length == expected["xmp_bytes"]
    elif not silent:
        assert not xmp

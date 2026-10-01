"""MVL-29 end to end: a site's photos from every kind of robot, ingested by a real sandboxed job.

Still images land as ``Image`` records that cite exact bytes, damaged ones become findings beside
the good ones, and the package is the same however many times and wherever it is built. The job
uses the shipped registry and the real parser sandbox (ADR 0030); nothing is mocked.
"""

import shutil
from pathlib import Path
from typing import Final

import pytest

from neptune.adapters.builtin import default_registry
from neptune.identity.canonical_json import dumps
from neptune.model.finding import IngestFinding
from neptune.model.provenance import EvidenceRef, ImageRegion
from neptune.model.source import SourceRevision
from neptune.model.world import Image
from neptune.runtime import IngestJob, JobOptions, JobState, Limits
from neptune.store.package import IngestPackage, read_package
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"
GOOD: Final = {
    "crawler/pipe7_joint12.jpg": ("crawler_inspection.jpg", [("jpeg", 64, 48)]),
    "amr/dock3.png": ("amr_dock.png", [("png", 32, 24)]),
    "rov/survey.tif": ("rov_survey.tif", [("tiff", 16, 12), ("tiff", 8, 6)]),
    "rover/mast.dng": ("rover_raw.dng", [("dng", 16, 12), ("dng", 32, 24)]),
    "humanoid/head.webp": ("humanoid_headcam.webp", [("webp", 1, 1)]),
    "arm/wrist_depth.pgm": ("wrist_depth.pgm", [("pgm", 16, 12)]),
    "maps/floor_map.bmp": ("floor_map.bmp", [("bmp", 8, 6)]),
}
DAMAGED: Final = {
    "crawler/cut.jpg": ("truncated.jpg", "image.truncated"),
    "amr/crc.png": ("bad_crc.png", "image.crc_mismatch"),
    "rover/loop.tif": ("subifd_cycle.tif", "image.ifd_loop"),
    "arm/bomb.png": ("bomb.png", "image.pixel_limit"),
    "arm/empty.png": ("empty.png", "neptune.probe.name_mismatch"),  # empty: the probe, not a parser
}


@pytest.fixture
def site(tmp_path: Path) -> Path:
    root = tmp_path / "site"
    for target, (name, _) in {**GOOD, **DAMAGED}.items():
        (root / target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / "image" / name, root / target)
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    return root


def ingest(root: Path, tmp_path: Path, name: str = "package") -> IngestPackage:
    job = IngestJob(
        root,
        tmp_path / name,
        Workspace(tmp_path / f"home-{name}"),
        default_registry(),
        JobOptions(limits=Limits(cpu_seconds=60, wall_seconds=60, memory_bytes=512 * 1024 * 1024)),
    )
    assert job.run().state is JobState.COMMITTED
    return read_package(tmp_path / name)


def paths_of(package: IngestPackage) -> dict[str, str]:
    """Each source's content id to the path it was found at."""
    return {
        str(r.content_id): str(r.location.path)  # type: ignore[union-attr]
        for r in package.records
        if isinstance(r, SourceRevision)
    }


def test_every_photo_lands_as_images_and_every_damaged_one_as_findings(
    site: Path, tmp_path: Path
) -> None:
    package = ingest(site, tmp_path)
    where = paths_of(package)
    found: dict[str, list[tuple[str, int, int]]] = {}
    for image in (r for r in package.records if isinstance(r, Image)):
        path = where[str(image.provenance.evidence.source)]
        found.setdefault(path, []).append((image.encoding, image.width, image.height))
    assert {p: sorted(v, key=lambda t: t[1]) for p, v in found.items() if p in GOOD} == {
        p: sorted(sizes, key=lambda t: t[1]) for p, (_, sizes) in GOOD.items()
    }
    findings = [r for r in package.records if isinstance(r, IngestFinding)]
    for path, (_, code) in DAMAGED.items():
        sources = {c for c, p in where.items() if p == path}
        assert any(
            f.code == code and getattr(f.subject, "source", None) in sources for f in findings
        ), path
    # the damaged images that still hold a raster are images too; the empty file is none
    assert "arm/empty.png" not in found and "arm/bomb.png" in found
    # no job-level failure: damage is the adapter's findings, not a quarantined source
    assert not [f for f in findings if f.code.startswith("neptune.runtime.")]


def test_an_image_region_extends_the_images_own_citation_and_the_source_names_its_place(
    site: Path, tmp_path: Path
) -> None:
    package = ingest(site, tmp_path)
    where = paths_of(package)
    images = [r for r in package.records if isinstance(r, Image)]
    assert len(images) >= len(GOOD)
    for image in images:
        evidence = image.provenance.evidence
        region = EvidenceRef(
            evidence.source, (*evidence.locator, ImageRegion(0, 0, image.width, image.height))
        )
        assert (
            str(region.source) in where
        )  # the revision says where the image lives: a run's folder
        assert region.locator[:-1] == evidence.locator


def test_the_same_tree_builds_the_same_images_and_findings_anywhere(
    site: Path, tmp_path: Path
) -> None:
    def digest(package: IngestPackage) -> bytes:
        kept = [r for r in package.records if isinstance(r, (Image, IngestFinding))]
        return dumps([r.to_json() for r in sorted(kept, key=lambda r: r.id)])

    first = ingest(site, tmp_path, "first")
    moved = tmp_path / "elsewhere" / "site"
    shutil.copytree(site, moved)
    second = ingest(moved, tmp_path, "second")
    assert digest(first) == digest(second)

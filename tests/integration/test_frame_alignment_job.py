"""MVL-37 end to end: four robots' recordings, a calibration and two site maps through the real
job (ADR 0068).

A fixed arm, a quadruped (``ros2idl`` MCAP) with its Kalibr calibration, an uncrewed surface
vessel and a mobile manipulator's ROS 1 bag, beside a warehouse map (GeoJSON, CRS84 by default) and
a marine survey map (UTM, stated). Their payloads are decoded in the sandbox; the package holds a
frame tree per run, its edges, the calibration's links to the quadruped's frames, the groups and
what each stream's and map's values are in, and says which pairs are comparable and why not.
"""

import shutil
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.derived.frames import (
    Basis,
    Comparable,
    EarthAt,
    FrameAt,
    FrameEdge,
    FrameLink,
    FrameTree,
    LinkRule,
    NotComparable,
    Reason,
    SpatialReference,
    frame_index,
)
from neptune.derived.sessions import read_derived
from neptune.derived.temporal import clock_graph
from neptune.identity.hashing import content_id
from neptune.model.finding import IngestFinding
from neptune.model.knowledge import Known
from neptune.model.run import Stream
from neptune.model.spatial import CrsCode
from neptune.model.time import Timestamp
from neptune.model.world import SpatialArtifact
from neptune.sdk import Neptune
from neptune.store.series import read_rows

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"
SOURCES: Final = (
    ("frames", "arm_cell.mcap"),
    ("frames", "quadruped_walk.mcap"),
    ("calibration", "quadruped_camchain_imucam.yaml"),
    ("frames", "usv_survey.mcap"),
    ("rosbag1", "robot_none.bag"),
    ("geojson", "warehouse_amr_site.geojson"),
    ("geojson", "marine_survey_projected.geojson"),
)


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Any:
    root = tmp_path_factory.mktemp("frames") / "robots"
    root.mkdir()
    for folder, name in SOURCES:
        shutil.copyfile(FIXTURES / folder / name, root / name)
    work = tmp_path_factory.mktemp("frames_job")
    result = Neptune(work / "home").ingest(root, work / "package")
    assert result.committed
    return result.read_package()


def stream(package: Any, topic: str, name: str) -> Stream:
    """The stream of ``topic`` in the fixture file ``name``."""
    folder = next(folder for folder, file in SOURCES if file == name)
    content = content_id((FIXTURES / folder / name).read_bytes())
    (found,) = [
        r
        for r in package.records
        if isinstance(r, Stream)
        and r.provenance.evidence.source == content
        and isinstance(r.topic, Known)
        and r.topic.value == topic
    ]
    return found


def findings(package: Any) -> dict[str, list[IngestFinding]]:
    found: dict[str, list[IngestFinding]] = {}
    for record in package.records:
        if isinstance(record, IngestFinding) and record.code.startswith("neptune.frames."):
            found.setdefault(record.code.removeprefix("neptune.frames."), []).append(record)
    return found


def test_the_package_holds_the_five_frame_tables(package: Any) -> None:
    for kind in ("frame_tree", "frame_edge", "frame_link", "frame_group", "spatial_reference"):
        assert package.derived[kind], kind
    derived = read_derived(package.derived)
    trees = [d for d in derived if isinstance(d, FrameTree)]
    assert len(trees) == 4  # one per recording; the maps and the calibration name no run


def test_payloads_are_decoded_in_the_sandbox(package: Any) -> None:
    codes = {
        r.code
        for r in package.records
        if isinstance(r, IngestFinding) and r.code.endswith("payload_not_decoded")
    }
    assert codes == set()  # every ROS stream here declares its definition
    imu = stream(package, "/imu", "quadruped_walk.mcap")
    rows = list(read_rows(package.series[imu.id]))
    assert {row["value/header.frame_id"] for row in rows} == {"imu"}
    assert all(row["time/2"] is not None for row in rows)


def test_disconnected_ambiguous_and_missing_frames_are_findings(package: Any) -> None:
    found = findings(package)
    assert {"disconnected", "name_variants", "frame_unset", "origin_unknown"} <= set(found)
    groups = [f.details["groups"] for f in found["disconnected"]]
    assert [["/base_link"], ["base_link", "gps", "odom", "sonar"], ["dvl_link"]] in groups
    assert any(["ft_sensor_link"] in g for g in groups)  # type: ignore[operator]


def test_the_calibration_links_to_the_quadrupeds_frames_by_name(package: Any) -> None:
    derived = read_derived(package.derived)
    links = [d for d in derived if isinstance(d, FrameLink) and d.rule is LinkRule.SAME_NAME]
    assert sorted(x.left.frame_id for x in links) == ["cam0", "cam1", "imu"]


def test_comparable_and_why_not(package: Any) -> None:
    derived = read_derived(package.derived)
    index = frame_index(package.records, derived)
    references = {r.subject: r for r in derived if isinstance(r, SpatialReference)}
    imu = references[stream(package, "/imu", "quadruped_walk.mcap").id]
    tree = imu.frames[0].frame.frame_graph_id
    (link,) = [d for d in derived if isinstance(d, FrameLink) and d.left.frame_id == "cam0"]
    camera = link.left if link.left.frame_graph_id != tree else link.right  # the calibration's
    strict = index.compare(FrameAt(imu.frames[0].frame), FrameAt(camera))
    assert isinstance(strict, NotComparable) and strict.reason is Reason.DISCONNECTED
    loose = index.compare(FrameAt(imu.frames[0].frame), FrameAt(camera), links=True)
    assert isinstance(loose, Comparable) and loose.inferred
    # The warehouse map is CRS84 by RFC 7946; the survey map states UTM: never reprojected.
    maps = {
        r.id: references[r.id]
        for r in package.records
        if isinstance(r, SpatialArtifact) and r.id in references
    }
    crs = sorted(str(m.crs.value.code) for m in maps.values() if isinstance(m.crs, Known))
    assert crs == ["32648", "CRS84"]
    warehouse = next(
        m for m in maps.values() if isinstance(m.crs, Known) and m.crs.value.code == "CRS84"
    )
    survey = next(m for m in maps.values() if m is not warehouse)
    differs = index.compare(EarthAt(warehouse.crs), EarthAt(survey.crs))
    assert isinstance(differs, NotComparable) and differs.reason is Reason.CRS_DIFFERS
    fix = references[stream(package, "/fix", "usv_survey.mcap").id]
    geodetic = index.compare(EarthAt(fix.crs, fix.geodetic), EarthAt(warehouse.crs))
    assert isinstance(geodetic, NotComparable) and geodetic.reason is Reason.CRS_UNKNOWN
    floating = index.compare(FrameAt(imu.frames[0].frame), EarthAt(Known(CrsCode("OGC", "CRS84"))))
    assert isinstance(floating, NotComparable) and floating.reason is Reason.NO_GEOREFERENCE
    assert tree in {t.id for t in derived if isinstance(t, FrameTree)}


def test_an_instant_on_a_header_clock_reaches_the_tf_samples_through_the_clock_graph(
    package: Any,
) -> None:
    derived = read_derived(package.derived)
    index = frame_index(package.records, derived)
    clocks = clock_graph(package.records, derived)
    imu = stream(package, "/imu", "quadruped_walk.mcap")
    edges = [d for d in derived if isinstance(d, FrameEdge)]
    moving = next(e for e in edges if (e.parent.frame_id, e.child.frame_id) == ("odom", "body"))
    imu_frame = next(
        d.frames[0].frame
        for d in derived
        if isinstance(d, SpatialReference) and d.subject == imu.id
    )
    rows = list(read_rows(package.series[imu.id]))
    stamp = Timestamp(rows[1]["time/2"], imu.clocks[2])  # type: ignore[arg-type]
    answer = index.compare(FrameAt(moving.parent, stamp), FrameAt(imu_frame), clocks=clocks)
    assert isinstance(answer, Comparable) and answer.basis is Basis.CONNECTED
    without = index.compare(FrameAt(moving.parent, stamp), FrameAt(imu_frame))
    assert isinstance(without, NotComparable) and without.reason is Reason.UNSYNCHRONISED

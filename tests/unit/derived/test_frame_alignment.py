"""The frame-alignment pass and the comparability query on real recordings (ADR 0068).

Four embodiments, each read by its adapter through the harness: a fixed arm (MCAP, ``ros2msg``),
a quadruped (MCAP, ``ros2idl``) with its Kalibr calibration, an uncrewed surface vessel (MCAP)
and a mobile manipulator (ROS 1 bag). The pass reads the adapters' records and decoded series
rows, as the job hands them over; nothing here decodes a payload.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.calibration import CalibrationAdapter
from neptune.adapters.harness import ingest_source
from neptune.adapters.mcap import McapAdapter
from neptune.adapters.rosbag1 import Rosbag1Adapter
from neptune.derived.clocks import ClockGraph
from neptune.derived.frames import (
    Basis,
    Caveat,
    Comparable,
    EarthAt,
    FrameAt,
    FrameEdge,
    FrameGroup,
    FrameIndex,
    FrameLink,
    FrameTree,
    LinkRule,
    NotComparable,
    Persistence,
    Reason,
    SpatialReference,
    StepKind,
    compare_subjects,
    frame_edge_from_json,
    frame_group_from_json,
    frame_index,
    frame_link_from_json,
    frame_tree_from_json,
    spatial_reference_from_json,
)
from neptune.derived.sessions import read_derived
from neptune.derived.spatial import FrameAlignment, align_frames, is_static_topic
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.frames import FrameRef, TransformDirection
from neptune.model.knowledge import Ambiguous, Known, Unknown
from neptune.model.reference import FrameTransform
from neptune.model.run import Stream
from neptune.model.spatial import CrsCode
from neptune.model.time import Timestamp

FIXTURES: Final = Path(__file__).parents[2] / "fixtures"
SOURCES: Final = {
    "arm": (McapAdapter(), FIXTURES / "frames" / "arm_cell.mcap"),
    "quadruped": (McapAdapter(), FIXTURES / "frames" / "quadruped_walk.mcap"),
    "kalibr": (CalibrationAdapter(), FIXTURES / "calibration" / "quadruped_camchain_imucam.yaml"),
    "usv": (McapAdapter(), FIXTURES / "frames" / "usv_survey.mcap"),
    "mobile": (Rosbag1Adapter(), FIXTURES / "rosbag1" / "robot_none.bag"),
}


class Corpus:
    def __init__(self, *names: str) -> None:
        self.records: list[Any] = []
        self.series: dict[str, list[Any]] = {}
        for name in names:
            adapter, path = SOURCES[name]
            output = ingest_source(adapter, BytesReader(path.read_bytes()), {})  # type: ignore[arg-type]
            self.records += list(output.records())
            for stream, batches in output.series().items():
                self.series.setdefault(stream, []).extend(batches)

    def rows(self, stream: Stream, columns: Sequence[str]) -> Iterator[Mapping[str, object]]:
        for batch in self.series.get(stream.id, ()):
            for row in batch.rows():
                yield {k: v for k, v in row.items() if k in columns}

    def align(self) -> FrameAlignment:
        found = align_frames(self.records, self.rows)
        assert found is not None
        return found

    def stream(self, topic: str) -> Stream:
        (found,) = [
            r
            for r in self.records
            if isinstance(r, Stream) and isinstance(r.topic, Known) and r.topic.value == topic
        ]
        return found


@pytest.fixture(scope="module")
def quadruped() -> tuple[Corpus, FrameAlignment]:
    corpus = Corpus("quadruped", "kalibr")
    return corpus, corpus.align()


@pytest.fixture(scope="module")
def usv() -> tuple[Corpus, FrameAlignment]:
    corpus = Corpus("usv")
    return corpus, corpus.align()


@pytest.fixture(scope="module")
def arm() -> tuple[Corpus, FrameAlignment]:
    corpus = Corpus("arm")
    return corpus, corpus.align()


def codes(found: FrameAlignment) -> list[str]:
    return sorted(f.code.removeprefix("neptune.frames.") for f in found.findings)


def edge(found: FrameAlignment, parent: str, child: str) -> FrameEdge:
    (line,) = [e for e in found.edges if (e.parent.frame_id, e.child.frame_id) == (parent, child)]
    return line


def reference(found: FrameAlignment, subject: str) -> SpatialReference:
    (line,) = [r for r in found.references if r.subject == subject]
    return line


# --- Trees and edges ----------------------------------------------------------------------------


def test_a_runs_tf_streams_and_headers_are_one_tree(
    quadruped: tuple[Corpus, FrameAlignment],
) -> None:
    corpus, found = quadruped
    (tree,) = found.trees
    topics = {"/tf", "/tf_static", "/imu"}
    assert set(tree.streams) == {corpus.stream(t).id for t in topics}
    assert {e.tree for e in found.edges} == {tree.id}
    assert {e.parent.frame_graph_id for e in found.edges} == {tree.id}


def test_edges_carry_what_tf2_states_and_leave_conventions_unknown(
    quadruped: tuple[Corpus, FrameAlignment],
) -> None:
    corpus, found = quadruped
    moving = edge(found, "odom", "body")
    assert moving.persistence is Persistence.DYNAMIC
    assert moving.samples == 5
    assert moving.direction == Known(TransformDirection.CHILD_TO_PARENT)
    assert (moving.translation_unit, moving.quaternion_convention) == (Unknown(), Unknown())
    tf = corpus.stream("/tf")
    assert moving.first == Timestamp(1_800_000_000_020_000_000, tf.clocks[0])
    assert moving.last == Timestamp(1_800_000_000_180_000_000, tf.clocks[0])
    fixed = edge(found, "body", "cam0")
    assert fixed.persistence is Persistence.STATIC and fixed.samples == 1
    assert codes(found) == []


def test_a_declared_graph_is_linked_by_name_never_joined(
    quadruped: tuple[Corpus, FrameAlignment],
) -> None:
    _, found = quadruped
    assert {(x.left.frame_id, x.rule) for x in found.links} == {
        ("cam0", LinkRule.SAME_NAME),
        ("cam1", LinkRule.SAME_NAME),
        ("imu", LinkRule.SAME_NAME),
    }
    (tree,) = found.trees
    calibration = {x.left.frame_graph_id for x in found.links} - {tree.id}
    assert len(calibration) == 1
    # The calibration's frames and the run's frames stay two groups: links join no group.
    assert len(found.groups) == 2
    run_group = next(g for g in found.groups if g.members[0].frame_graph_id == tree.id)
    assert run_group.origin == Known(FrameRef("odom", tree.id))
    assert run_group.earth == Unknown() and run_group.dynamic


def test_the_imu_is_comparable_with_a_camera_through_the_tree_and_the_calibration(
    quadruped: tuple[Corpus, FrameAlignment],
) -> None:
    corpus, found = quadruped
    index = frame_index(corpus.records, (*found.edges, *found.links, *found.groups))
    (tree,) = found.trees
    imu = reference(found, corpus.stream("/imu").id)
    assert [c.frame for c in imu.frames] == [FrameRef("imu", tree.id)]
    answer = index.compare(FrameAt(FrameRef("imu", tree.id)), FrameAt(FrameRef("cam0", tree.id)))
    assert isinstance(answer, Comparable) and answer.basis is Basis.CONNECTED
    assert [s.kind for s in answer.path] == [StepKind.EDGE, StepKind.EDGE]
    assert not answer.inferred
    assert Caveat.TRANSLATION_UNIT_UNKNOWN in answer.caveats
    calibration = next(x.left.frame_graph_id for x in found.links if x.left.frame_id == "cam0")
    kalibr_cam0 = FrameRef("cam0", calibration)
    strict = index.compare(FrameAt(FrameRef("imu", tree.id)), FrameAt(kalibr_cam0))
    assert isinstance(strict, NotComparable) and strict.reason is Reason.DISCONNECTED
    loose = index.compare(FrameAt(FrameRef("imu", tree.id)), FrameAt(kalibr_cam0), links=True)
    assert isinstance(loose, Comparable) and loose.inferred
    assert loose.path[-1].kind is StepKind.LINK or loose.path[0].kind is StepKind.LINK


def test_a_dynamic_step_is_bounded_by_its_samples_and_by_the_clocks(
    quadruped: tuple[Corpus, FrameAlignment],
) -> None:
    corpus, found = quadruped
    index = frame_index(corpus.records, (*found.edges, *found.links))
    (tree,) = found.trees
    odom, imu = FrameRef("odom", tree.id), FrameRef("imu", tree.id)
    tf_clock = corpus.stream("/tf").clocks[0]
    unbounded = index.compare(FrameAt(odom), FrameAt(imu))
    assert isinstance(unbounded, Comparable) and Caveat.TIME_DEPENDENT in unbounded.caveats
    inside = index.compare(
        FrameAt(odom, Timestamp(1_800_000_000_100_000_000, tf_clock)), FrameAt(imu)
    )
    assert isinstance(inside, Comparable) and Caveat.TIME_DEPENDENT not in inside.caveats
    after = index.compare(
        FrameAt(odom, Timestamp(1_800_000_001_000_000_000, tf_clock)), FrameAt(imu)
    )
    assert isinstance(after, NotComparable) and after.reason is Reason.OUTSIDE_COVERAGE
    header = corpus.stream("/imu").clocks[2]
    other = index.compare(FrameAt(odom, Timestamp(1_800_000_000_100_000_000, header)), FrameAt(imu))
    assert isinstance(other, NotComparable) and other.reason is Reason.UNSYNCHRONISED
    no_mapping = index.compare(
        FrameAt(odom, Timestamp(1_800_000_000_100_000_000, header)),
        FrameAt(imu),
        clocks=ClockGraph([]),
    )
    assert isinstance(no_mapping, NotComparable) and no_mapping.reason is Reason.UNSYNCHRONISED


# --- Disconnected, ambiguous and missing --------------------------------------------------------


def test_disconnected_frames_and_name_variants_are_findings(
    usv: tuple[Corpus, FrameAlignment],
) -> None:
    corpus, found = usv
    assert codes(found) == ["disconnected", "name_variants"]
    disconnected = next(f for f in found.findings if f.code.endswith("disconnected"))
    assert disconnected.details["groups"] == [
        ["/base_link"],
        ["base_link", "gps", "odom", "sonar"],
        ["dvl_link"],
    ]
    (link,) = found.links
    assert link.rule is LinkRule.LEADING_SLASH
    assert (link.left.frame_id, link.right.frame_id) == ("/base_link", "base_link")
    imu = reference(found, corpus.stream("/imu").id)
    assert [(c.frame.frame_id, c.rows) for c in imu.frames] == [("/base_link", 2), ("base_link", 2)]


def test_a_sensor_in_a_frame_no_transform_names_is_not_comparable(
    usv: tuple[Corpus, FrameAlignment],
) -> None:
    corpus, found = usv
    index = frame_index(corpus.records, found.edges)
    dvl = reference(found, corpus.stream("/dvl").id)
    sonar = reference(found, corpus.stream("/sonar/range").id)
    ((_, _, answer),) = compare_subjects(index, dvl, sonar)
    assert isinstance(answer, NotComparable) and answer.reason is Reason.DISCONNECTED
    assert sonar.frames[0].rows == 3  # the fourth payload does not decode: it names no frame


def test_a_fix_is_geodetic_by_its_type_and_never_comparable_with_a_frame_or_a_crs(
    usv: tuple[Corpus, FrameAlignment],
) -> None:
    corpus, found = usv
    index = frame_index(corpus.records, found.edges)
    fix = reference(found, corpus.stream("/fix").id)
    assert fix.geodetic == "sensor_msgs/NavSatFix"
    earth = EarthAt(fix.crs, fix.geodetic)
    assert index.compare(earth, EarthAt(fix.crs, fix.geodetic)) == Comparable(Basis.SAME_DEFINITION)
    crs84 = EarthAt(Known(CrsCode("OGC", "CRS84")))
    answer = index.compare(earth, crs84)
    assert isinstance(answer, NotComparable) and answer.reason is Reason.CRS_UNKNOWN
    sonar = FrameAt(reference(found, corpus.stream("/sonar/range").id).frames[0].frame)
    answer = index.compare(sonar, crs84)
    assert isinstance(answer, NotComparable) and answer.reason is Reason.NO_GEOREFERENCE


def test_crs_comparisons_never_reproject() -> None:
    index = FrameIndex()
    a = EarthAt(Known(CrsCode("EPSG", "4326")))
    assert index.compare(a, EarthAt(Known(CrsCode("EPSG", "4326")))).basis is Basis.SAME_CRS  # type: ignore[union-attr]
    differs = index.compare(a, EarthAt(Known(CrsCode("OGC", "CRS84"))))
    assert isinstance(differs, NotComparable) and differs.reason is Reason.CRS_DIFFERS
    unknown = index.compare(a, EarthAt(Unknown()))
    assert isinstance(unknown, NotComparable) and unknown.reason is Reason.CRS_UNKNOWN


def test_rows_that_name_no_frame_have_no_origin(arm: tuple[Corpus, FrameAlignment]) -> None:
    corpus, found = arm
    assert codes(found) == ["disconnected", "frame_unset", "origin_unknown"]
    joints = reference(found, corpus.stream("/joint_states").id)
    assert (joints.frames, joints.unset, joints.has_origin) == ((), 4, False)
    index = frame_index(corpus.records, found.edges)
    ((_, _, answer),) = compare_subjects(
        index, joints, reference(found, corpus.stream("/ft_sensor").id)
    )
    assert isinstance(answer, NotComparable) and answer.reason is Reason.FRAME_UNKNOWN
    (tree,) = found.trees
    chain = index.compare(
        FrameAt(FrameRef("world", tree.id)), FrameAt(FrameRef("gripper", tree.id))
    )
    assert isinstance(chain, Comparable) and len(chain.path) == 7


def test_a_static_transform_restated_with_other_values_is_inconsistent() -> None:
    corpus = Corpus("mobile")
    found = corpus.align()
    assert codes(found) == []  # the bag's one static message is stated once
    (tree,) = found.trees
    groups = [g for g in found.groups if g.members[0].frame_graph_id == tree.id]
    assert [g.origin for g in groups] == [Known(FrameRef("odom", tree.id))]
    static = edge(found, "base_link", "lidar")
    assert static.persistence is Persistence.STATIC
    odometry = reference(found, corpus.stream("/odom").id)
    assert [(c.frame.frame_id, c.rows) for c in odometry.frames] == [("base_link", 5), ("odom", 5)]


def test_two_parents_and_a_loop_are_findings() -> None:
    corpus = Corpus("arm")
    tf = corpus.stream("/tf")
    found = align_frames(
        corpus.records, _with_extra(corpus, tf, [("world", "tool0"), ("gripper", "world")])
    )
    assert found is not None
    assert {"loop", "multiple_parents"} <= set(codes(found))
    (tree,) = found.trees
    group = next(g for g in found.groups if FrameRef("world", tree.id) in g.members)
    assert isinstance(group.origin, Ambiguous | Unknown)


def _with_extra(corpus: Corpus, stream: Stream, pairs: list[tuple[str, str]]) -> Any:
    """``corpus.rows``, with one more transform per pair on ``stream``'s first row."""

    def rows(of: Stream, columns: Sequence[str]) -> Iterator[Mapping[str, object]]:
        for k, row in enumerate(corpus.rows(of, columns)):
            if of.id != stream.id or k:
                yield row
                continue
            changed = dict(row)
            for column, extra in (
                ("value/transforms[].header.frame_id", [p for p, _ in pairs]),
                ("value/transforms[].child_frame_id", [c for _, c in pairs]),
            ):
                if column in changed:
                    changed[column] = [*changed[column], *extra]  # type: ignore[misc]
            yield changed

    return rows


def test_a_static_restatement_is_reported() -> None:
    corpus = Corpus("arm")
    static = corpus.stream("/tf_static")

    def rows(of: Stream, columns: Sequence[str]) -> Iterator[Mapping[str, object]]:
        found = list(corpus.rows(of, columns))
        if of.id == static.id and found:
            again = dict(found[0])
            again["time/0"] = int(again["time/0"]) + 1  # type: ignore[call-overload]
            column = "value/transforms[].transform.translation.z"
            again[column] = [9.0 for _ in again[column]]  # type: ignore[attr-defined]
            found.append(again)
        yield from found

    result = align_frames(corpus.records, rows)
    assert result is not None and "static_changed" in codes(result)


# --- Lines, determinism, nothing spatial --------------------------------------------------------


def test_every_line_round_trips_and_reads_back(quadruped: tuple[Corpus, FrameAlignment]) -> None:
    _, found = quadruped
    readers: dict[str, Callable[[Any], Any]] = {
        "frame_tree": frame_tree_from_json,
        "frame_edge": frame_edge_from_json,
        "frame_link": frame_link_from_json,
        "frame_group": frame_group_from_json,
        "spatial_reference": spatial_reference_from_json,
    }
    tables = {kind: list(lines) for kind, lines in found.tables().items()}
    for kind, lines in tables.items():
        for line in lines:
            parsed = readers[kind](canonical_json.loads(canonical_json.dumps(line)))
            assert parsed.to_json() == line
    parsed_all = read_derived(tables)
    assert {type(r) for r in parsed_all} == {
        FrameTree,
        FrameEdge,
        FrameLink,
        FrameGroup,
        SpatialReference,
    }


def test_the_pass_is_deterministic_whatever_the_order_of_its_inputs() -> None:
    corpus = Corpus("usv", "quadruped", "kalibr")
    first = align_frames(corpus.records, corpus.rows)
    second = align_frames(list(reversed(corpus.records)), corpus.rows)
    assert first is not None and second is not None

    def dump(found: FrameAlignment) -> list[bytes]:
        lines = [canonical_json.dumps(x) for t in found.tables().values() for x in t]
        return lines + [canonical_json.dumps(f.to_json()) for f in found.findings]

    assert dump(first) == dump(second)


def test_a_package_with_nothing_spatial_gets_no_alignment() -> None:
    assert align_frames([], lambda stream, columns: iter(())) is None


def test_static_topics_follow_tf2_under_any_namespace() -> None:
    assert [is_static_topic(t) for t in ("/tf_static", "/robot1/tf_static", "tf_static")] == [
        True,
        True,
        True,
    ]
    assert not any(is_static_topic(t) for t in ("/tf", "/tf_static_old", "/static"))


def test_declared_transforms_carry_their_own_caveats() -> None:
    corpus = Corpus("kalibr")
    transforms = [r for r in corpus.records if isinstance(r, FrameTransform)]
    index = FrameIndex(transforms)
    first = transforms[0]
    answer = index.compare(FrameAt(first.parent), FrameAt(first.child))
    assert isinstance(answer, Comparable)
    assert Caveat.TRANSLATION_UNIT_UNKNOWN in answer.caveats  # Kalibr states no unit
    assert answer.path[0].kind is StepKind.TRANSFORM

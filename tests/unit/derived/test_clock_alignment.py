"""MVL-36: clock mappings fitted from sync anchors, clocks found in values, and aligning an instant
across clocks with an explicit bound (ADR 0060).

The worked examples give the embodiments: a manipulator's camera and joint-state header stamps
against its MCAP log clock, a drone's boot clock against GPS time, a quadruped's stated
rosbag2 mapping. Rows are written here, so the arithmetic can be checked exactly.
"""

import random
from collections.abc import Iterable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from neptune.derived.clocks import (
    MAX_RATE_DENOMINATOR,
    Aligned,
    ClockGraph,
    FitProblem,
    InferredClockMapping,
    InferredTimestampDomain,
    Line,
    Reason,
    Unaligned,
    fit_line,
    fitted_mapping,
    inferred_clock_mapping_from_json,
    inferred_timestamp_domain_from_json,
)
from neptune.derived.provenance import INFERRED
from neptune.derived.sessions import read_derived
from neptune.derived.temporal import CO_RECORDED, ClockConfig, align_clocks, clock_graph
from neptune.identity import canonical_json
from neptune.model.alignment import ClockAnchor, ClockMapping, MappingMethod, ValidityWindow
from neptune.model.ids import ContentId, RecordId
from neptune.model.kinds import RECORD_KINDS
from neptune.model.knowledge import Known, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.reference import TimestampDomain
from neptune.model.run import Stream
from neptune.model.time import INT64_MAX, Duration, Timestamp

EXAMPLES = Path(__file__).parents[2] / "fixtures" / "model"


def example_records(name: str) -> list[Any]:
    """Every record of a worked example, read from its committed tables."""
    records: list[Any] = []
    for path in sorted((EXAMPLES / name / "records").glob("*.jsonl")):
        _, read = RECORD_KINDS[path.stem]
        records += [read(canonical_json.loads(line)) for line in path.read_bytes().splitlines()]
    return records


A = RecordId("rec:sha256:" + "a" * 64)
B = RecordId("rec:sha256:" + "b" * 64)
C = RecordId("rec:sha256:" + "c" * 64)
TRANSFORM = RecordId("rec:sha256:" + "f" * 64)
REF = EvidenceRef(ContentId("sha256:" + "1" * 64), (ByteRange(0, 8),))
MS = 1_000_000  # nanoseconds


def line_of(pairs: Sequence[tuple[int, int]], **kwargs: Any) -> Line | FitProblem:
    return fit_line(lambda: iter(pairs), **kwargs)


def mapping(
    source: RecordId,
    target: RecordId,
    pairs: Sequence[tuple[int, int]],
    slack: int | None = 0,
    name: str = "1",
) -> InferredClockMapping:
    line = line_of(pairs)
    assert isinstance(line, Line)
    return fitted_mapping(
        record_id=RecordId("rec:sha256:" + name * 64),
        transform=TRANSFORM,
        evidence=(REF,),
        source=source,
        target=target,
        line=line,
        slack=slack,
    )


# --- Fitting ------------------------------------------------------------------------------------


def test_an_exact_drifting_clock_fits_exactly_without_a_float() -> None:
    # a microsecond boot clock against a nanosecond clock running 20 ppm fast, offset 7 ns
    rate = Fraction(1000 * 1_000_020, 1_000_000)
    pairs = [(x, 7 + int(rate * x)) for x in range(0, 10_000_000, 1_000_000)]
    line = line_of(pairs)
    assert isinstance(line, Line)
    assert line.rate == rate and line.residual == 0
    assert (line.first, line.last, line.count) == (0, 9_000_000, 10)
    assert line.anchor_target == 7 + rate * line.anchor_source


def test_the_residual_bound_is_the_largest_residual_over_every_anchor_rounded_up() -> None:
    pairs = [(0, 0), (10, 13), (20, 20), (30, 30)]
    line = line_of(pairs)
    assert isinstance(line, Line) and line.rate is not None
    worst = max(
        abs(y - (line.anchor_target + line.rate * (x - line.anchor_source))) for x, y in pairs
    )
    assert line.residual == -(-worst.numerator // worst.denominator) and line.residual >= 1


def test_the_fit_does_not_depend_on_the_order_of_the_anchors() -> None:
    generator = random.Random(36)
    pairs = [(x * 997, x * 1003 + generator.randrange(-50, 50)) for x in range(500)]
    shuffled = list(pairs)
    generator.shuffle(shuffled)
    assert line_of(pairs) == line_of(shuffled)


def test_the_rate_is_rounded_to_a_bounded_denominator_and_the_residual_measured_after() -> None:
    pairs = [(x, x * 3 + (x * x) % 7) for x in range(1, 2000, 17)]
    line = line_of(pairs, max_denominator=100)
    assert isinstance(line, Line) and line.rate is not None
    assert line.rate.denominator <= 100
    worst = max(
        abs(y - (line.anchor_target + line.rate * (x - line.anchor_source))) for x, y in pairs
    )
    assert line.residual >= worst


def test_no_anchor_and_a_clock_running_backward_are_problems_not_mappings() -> None:
    assert line_of([]) is FitProblem.NO_ANCHORS
    assert line_of([(0, 100), (10, 90), (20, 80)]) is FitProblem.NOT_INCREASING
    assert line_of([(0, 5), (10, 5)]) is FitProblem.NOT_INCREASING  # a stopped clock


def test_one_source_instant_gives_an_offset_there_and_no_rate() -> None:
    line = line_of([(40, 100), (40, 104)])
    assert isinstance(line, Line)
    assert line.rate is None and (line.anchor_source, line.anchor_target) == (40, 102)
    assert line.residual == 2
    single = mapping(A, B, [(40, 100), (40, 104)])
    assert isinstance(single.rate, Unknown)
    window = single.validity
    assert isinstance(window, Known)
    assert window.value.start == Known(Timestamp(40, A))
    assert window.value.end == Known(Timestamp(41, A))  # that instant and no other


def test_ticks_at_the_signed_64_bit_edge_fit_and_the_window_stays_representable() -> None:
    top = INT64_MAX
    edge = mapping(A, B, [(top - 2, top - 2), (top, top)])
    assert edge.rate == Known(Fraction(1))
    window = edge.validity
    assert isinstance(window, Known) and isinstance(window.value.end, Unknown)


# --- Records ------------------------------------------------------------------------------------


def test_an_inferred_mapping_round_trips_and_carries_inferred_provenance_only() -> None:
    fitted = mapping(A, B, [(0, 10), (100, 210), (200, 410)], slack=3)
    data = fitted.to_json()
    assert data["assertion_kind"] == INFERRED and data["kind"] == "clock_mapping"
    assert data["method"] == "co_sampled"
    assert data["residual_bound"] == {
        "knowledge": "known",
        "value": {"domain_id": B, "ticks": 3},
    }
    text = canonical_json.dumps(data)
    assert inferred_clock_mapping_from_json(canonical_json.loads(text)) == fitted
    assert read_derived({"clock_mapping": [data]}) == (fitted,)


def test_the_strict_reader_refuses_a_canonical_grounding_or_an_extra_key() -> None:
    data = dict(mapping(A, B, [(0, 0), (10, 10)]).to_json())
    grounded = dict(data)
    grounded["rate"] = {
        **data["rate"],  # type: ignore[dict-item]
        "provenance": {
            "assertion_kind": "stated",
            "evidence": REF.to_json(),
            "transform": TRANSFORM,
        },
    }
    with pytest.raises(ValueError, match="inherits"):
        inferred_clock_mapping_from_json(grounded)
    with pytest.raises(ValueError):
        inferred_clock_mapping_from_json({**data, "note": "x"})
    with pytest.raises(ValueError):
        inferred_clock_mapping_from_json({**data, "assertion_kind": "stated"})


def test_an_inferred_mapping_refuses_what_a_clock_mapping_refuses() -> None:
    good = mapping(A, B, [(0, 0), (10, 10)])
    with pytest.raises(ValueError, match="two clocks"):
        InferredClockMapping(**{**good.__dict__, "target": A})
    with pytest.raises(ValueError, match="positive"):
        InferredClockMapping(**{**good.__dict__, "rate": Known(Fraction(-1))})
    with pytest.raises(ValueError, match="target clock"):
        InferredClockMapping(**{**good.__dict__, "residual_bound": Known(Duration(1, A))})
    with pytest.raises(ValueError, match="at least one"):
        InferredClockMapping(**{**good.__dict__, "evidence": ()})


def test_an_inferred_domain_round_trips() -> None:
    from neptune.model.time import ClockRole, Epoch, Timescale

    domain = InferredTimestampDomain(
        id=C,
        transform=TRANSFORM,
        evidence=(REF,),
        field="GWk,GMS",
        scope=("GPS",),
        role=Known(ClockRole.SAMPLE),
        resolution=Known(Fraction(1, 1000)),
        epoch=Known(Epoch.GPS),
        timescale=Known(Timescale.GPS),
        declared_monotonic=Unknown(),
    )
    data = canonical_json.loads(canonical_json.dumps(domain.to_json()))
    assert inferred_timestamp_domain_from_json(data) == domain


# --- Aligning -----------------------------------------------------------------------------------


def test_an_instant_maps_forward_and_back_with_its_bound_growing_by_the_rate() -> None:
    # A: microseconds; B: nanoseconds, offset 5 us, bound 40 ns
    to_b = mapping(A, B, [(0, 5_000), (1_000, 1_005_000)], slack=40)
    graph = ClockGraph([to_b])
    there = graph.align(Timestamp(500, A), B)
    assert there == Aligned(Timestamp(505_000, B), Duration(40, B), (to_b.id,), True)
    assert there.window() == (Timestamp(504_960, B), Timestamp(505_040, B))
    back = graph.align(Timestamp(505_000, B), A)
    assert isinstance(back, Aligned) and back.instant == Timestamp(500, A)
    assert back.bound == Duration(1, A)  # 40 ns is 0.04 us, rounded up to a whole tick


def test_two_hops_add_their_bounds_and_name_their_path() -> None:
    first = mapping(A, B, [(0, 100), (1_000, 1_100)], slack=5, name="1")
    second = mapping(B, C, [(0, 0), (2_000, 4_000)], slack=7, name="2")
    found = ClockGraph([second, first]).align(Timestamp(400, A), C)
    assert isinstance(found, Aligned)
    assert found.instant == Timestamp(1_000, C)
    assert found.bound == Duration(2 * 5 + 7, C)
    assert found.path == (first.id, second.id)


def test_clocks_never_related_stay_unaligned() -> None:
    graph = ClockGraph([mapping(A, B, [(0, 0), (10, 10)])])
    assert graph.align(Timestamp(5, A), C) == Unaligned(Reason.UNSYNCHRONISED)
    assert graph.align(Timestamp(5, C), C) == Aligned(Timestamp(5, C), Duration(0, C), (), False)
    assert graph.groups([A, B, C]) == [[A, B], [C]]


def test_a_mapping_does_not_extrapolate_beyond_its_anchors() -> None:
    graph = ClockGraph([mapping(A, B, [(100, 0), (200, 100)])])
    assert graph.align(Timestamp(99, A), B) == Unaligned(Reason.OUTSIDE_VALIDITY)
    assert graph.align(Timestamp(201, A), B) == Unaligned(Reason.OUTSIDE_VALIDITY)
    assert isinstance(graph.align(Timestamp(200, A), B), Aligned)


def test_an_unbounded_mapping_gives_an_instant_without_a_bound() -> None:
    found = ClockGraph([mapping(A, B, [(0, 0), (10, 20)], slack=None)]).align(Timestamp(5, A), B)
    assert isinstance(found, Aligned)
    assert found.instant == Timestamp(10, B) and found.bound is None and found.window() is None


def test_a_rate_unknown_mapping_applies_at_its_one_instant_only() -> None:
    graph = ClockGraph([mapping(A, B, [(40, 100), (40, 104)])])
    assert graph.align(Timestamp(40, A), B) == Aligned(
        Timestamp(102, B), Duration(2, B), (graph_ids(graph)[0],), True
    )
    assert graph.align(Timestamp(41, A), B) == Unaligned(Reason.OUTSIDE_VALIDITY)


def graph_ids(graph: ClockGraph) -> list[RecordId]:
    return sorted({edge.mapping.id for edges in graph._edges.values() for edge in edges})


def test_a_stated_mapping_is_preferred_and_reported_as_not_inferred() -> None:
    quadruped = example_records("quadruped")
    stated = next(r for r in quadruped if isinstance(r, ClockMapping))
    assert stated.method is MappingMethod.STATED
    window = stated.validity
    assert isinstance(window, Known) and isinstance(window.value.start, Known)
    start = window.value.start.value
    found = clock_graph(quadruped).align(start, stated.target)
    assert isinstance(found, Aligned)
    assert found.instant.ticks == start.ticks and found.bound == Duration(0, stated.target)
    assert found.path == (stated.id,) and not found.inferred


# --- The pass over records and rows ------------------------------------------------------------

Rows = dict[RecordId, list[dict[str, object]]]


def reader(rows: Rows) -> Any:
    def read(stream: Stream, columns: Sequence[str]) -> Iterable[Mapping[str, object]]:
        return [{c: row[c] for c in columns if c in row} for row in rows.get(stream.id, [])]

    return read


def by_topic(records: Iterable[object]) -> dict[str, Stream]:
    return {
        s.topic.value: s for s in records if isinstance(s, Stream) and isinstance(s.topic, Known)
    }


def manipulator_rows(records: list[object]) -> Rows:
    """Joint states at 100 Hz and wrist-camera frames at 30 Hz: each published 300 us before it
    is logged, its header stamp 2 to 4 ms before that (exposure, driver), all on ROS time."""
    streams = by_topic(records)
    t0 = 1_790_762_401_000_000_000
    rows: Rows = {}
    for topic, period, lag in (
        ("/joint_states", 10 * MS, 2 * MS),
        ("/wrist_camera/image/compressed", 33 * MS, 4 * MS),
    ):
        out: list[dict[str, object]] = []
        for k in range(30):
            logged = t0 + k * period + (k % 3) * 50_000  # jitter
            out.append(
                {"time/0": logged, "time/1": logged - 300_000, "time/2": logged - lag - k % 2}
            )
        rows[streams[topic].id] = out
    return rows


def test_the_manipulator_camera_and_joint_stamps_align_through_the_log_clock() -> None:
    records: list[object] = list(example_records("manipulator"))
    streams = by_topic(records)
    rows = manipulator_rows(records)
    found = align_clocks(records, reader(rows))
    assert found is not None
    # every extra clock of each stream against its log time: publish and header stamp, twice
    assert len(found.mappings) == 4
    assert all(isinstance(m.residual_bound, Unknown) for m in found.mappings)
    codes = sorted(f.code for f in found.findings)
    assert "neptune.clocks.latency_unbounded" in codes
    camera = streams["/wrist_camera/image/compressed"]
    joints = streams["/joint_states"]
    stamp = rows[camera.id][5]["time/2"]  # 165 ms in: inside the joints' anchors too
    assert isinstance(stamp, int)
    frame = Timestamp(stamp, camera.clocks[2])
    unbounded = found.graph.align(frame, joints.clocks[2])
    assert isinstance(unbounded, Aligned) and unbounded.bound is None
    assert len(unbounded.path) == 2 and unbounded.inferred

    # the user states each reading pair is at most 5 ms apart: the same instant, now bounded
    config = ClockConfig(slack=((CO_RECORDED, Fraction(5, 1000)),))
    bounded = align_clocks(records, reader(rows), config)
    assert bounded is not None
    assert all(isinstance(m.residual_bound, Known) for m in bounded.mappings)
    aligned = bounded.graph.align(frame, joints.clocks[2])
    assert isinstance(aligned, Aligned) and aligned.bound is not None
    assert aligned.instant == unbounded.instant
    # the camera frame's true stamp on the joint clock (both are ROS time here) is inside
    window = aligned.window()
    assert window is not None
    assert window[0].ticks <= frame.ticks <= window[1].ticks
    assert aligned.bound.ticks <= 2 * (5 * MS + 400_000)
    # a frame logged after the last joint state has no instant on the joint clock: no extrapolation
    late = rows[camera.id][20]["time/2"]
    assert isinstance(late, int)
    beyond = bounded.graph.align(Timestamp(late, camera.clocks[2]), joints.clocks[2])
    assert beyond == Unaligned(Reason.OUTSIDE_VALIDITY)


def test_the_drone_boot_clock_maps_onto_gps_time_and_nothing_pretends_beyond() -> None:
    records: list[object] = list(example_records("drone"))
    streams = by_topic(records)
    gps = streams["vehicle_gps_position"]
    offset = 1_790_762_400_000_000 - 5_000_000
    fixes: list[dict[str, object]] = [
        {"time/0": boot, "time/1": boot + offset} for boot in range(5_000_000, 6_000_000, 200_000)
    ]
    rows: Rows = {
        gps.id: [*fixes, {"time/0": 6_000_000, "state/time/1": "unknown", "time/1": None}],
    }
    found = align_clocks(records, reader(rows))
    assert found is not None
    fitted = [m for m in found.mappings if m.source == gps.clocks[1]]
    assert len(fitted) == 1 and fitted[0].rate == Known(Fraction(1))
    attitude = Timestamp(5_500_000, gps.clocks[0])
    aligned = found.graph.align(attitude, gps.clocks[1])
    assert isinstance(aligned, Aligned) and aligned.instant.ticks == 5_500_000 + offset
    assert aligned.bound is None  # GPS fix to publication latency: nothing states it
    # the drone's other streams' clocks have no anchor: an explicit finding, never a guess
    codes = {f.code for f in found.findings}
    assert "neptune.clocks.anchors_absent" in codes
    assert found.graph.align(Timestamp(7_000_000, gps.clocks[0]), gps.clocks[1]) == Unaligned(
        Reason.OUTSIDE_VALIDITY
    )


def test_two_robots_with_no_shared_evidence_are_reported_unsynchronised() -> None:
    drone = list(example_records("drone"))
    arm = list(example_records("manipulator"))
    records: list[object] = [*drone, *arm]
    found = align_clocks(records, reader(manipulator_rows(arm)))
    assert found is not None
    [unsynced] = [f for f in found.findings if f.code == "neptune.clocks.unsynchronised"]
    groups = unsynced.details["groups"]
    assert isinstance(groups, list) and len(groups) >= 2
    drone_boot = next(r for r in drone if isinstance(r, TimestampDomain) and r.field == "timestamp")
    arm_log = next(r for r in arm if isinstance(r, TimestampDomain) and r.field == "log_time")
    assert found.graph.align(Timestamp(1, drone_boot.id), arm_log.id) == Unaligned(
        Reason.UNSYNCHRONISED
    )


def test_hostile_cells_are_never_anchors() -> None:
    records: list[object] = list(example_records("manipulator"))
    joints = by_topic(records)["/joint_states"]
    rows: Rows = {
        joints.id: [
            {"time/0": True, "time/1": 1, "time/2": 1},
            {"time/0": 1.5, "time/1": 1, "time/2": 1},
            {"time/0": 2**64, "time/1": 1, "time/2": 1},
            {"time/0": "10", "time/1": 1, "time/2": 1},
            {"time/0": None, "time/1": 1, "time/2": 1},
        ]
    }
    found = align_clocks(records, reader(rows))
    assert found is not None and not found.mappings
    absent = [f for f in found.findings if f.code == "neptune.clocks.anchors_absent"]
    assert len(absent) == 4  # each extra clock of both streams; none got a reading pair


def test_the_pass_is_deterministic_and_writes_no_record_of_the_evidence() -> None:
    records: list[object] = list(example_records("manipulator"))
    rows = manipulator_rows(records)
    first = align_clocks(records, reader(rows))
    again = align_clocks(list(reversed(records)), reader(rows))
    assert first is not None and again is not None
    assert first.transform == again.transform and first.findings == again.findings
    tables = {k: [canonical_json.dumps(x) for x in v] for k, v in first.tables().items()}
    assert tables == {k: [canonical_json.dumps(x) for x in v] for k, v in again.tables().items()}
    assert set(tables) == {"clock_mapping", "timestamp_domain"}


def test_a_package_with_one_clock_gets_no_alignment() -> None:
    records = [r for r in example_records("manipulator") if not isinstance(r, Stream)]
    domains = [r for r in records if isinstance(r, TimestampDomain)][:1]
    assert align_clocks(domains, reader({})) is None
    assert align_clocks([], reader({})) is None


def test_the_config_refuses_unknown_rules_and_negative_slack() -> None:
    with pytest.raises(ValueError, match="known rules"):
        ClockConfig(slack=(("made.up", Fraction(1)),))
    with pytest.raises(ValueError, match="non-negative"):
        ClockConfig(slack=((CO_RECORDED, Fraction(-1)),))
    with pytest.raises(ValueError, match="positive"):
        ClockConfig(max_rate_denominator=0)
    assert ClockConfig().max_rate_denominator == MAX_RATE_DENOMINATOR


def test_a_window_is_checked_on_the_source_clock() -> None:
    with pytest.raises(ValueError, match="validity"):
        good = mapping(A, B, [(0, 0), (10, 10)])
        InferredClockMapping(
            **{
                **good.__dict__,
                "validity": Known(ValidityWindow(B, Known(Timestamp(0, B)), Unknown())),
            }
        )
    anchor = ClockAnchor(Timestamp(0, B), Timestamp(0, A))
    with pytest.raises(ValueError, match="anchor"):
        InferredClockMapping(**{**good.__dict__, "anchor": Known(anchor)})

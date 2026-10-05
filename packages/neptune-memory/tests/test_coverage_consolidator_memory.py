"""Coverage and health (ADR 0015) on the issue's archetypes, in the compiler's record shapes.

- A mobile robot's bag: a lidar declared at 100 Hz that the series holds at 50 Hz, an odometry
  stream that starts a second late, and a camera configured with no stream naming it.
- A truncated bag: the series stops before the extent its index declares; the compiler's
  truncation findings become integrity claims with the severity it stated.
- The drone worked example as the compiler wrote it: a logger dropout naming two streams.
- A manipulator cell recorded as a wrist-camera video: the camera recorded, the force-torque
  sensor configured beside it known absent; then each thing that withholds that.
- A quadruped's streams on two clocks, one of them with untimed samples.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from memory_coverage_records import (
    HZ_100,
    SECOND,
    binding,
    component,
    configuration,
    finding,
    image,
    series,
    stream,
    video,
)
from memory_identity_records import Record, ambiguous, at, ledger
from memory_run_records import NS, assembly, domain, revision, run
from neptune.model.alignment import MemberRole
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import Known
from neptune.model.time import Epoch, Timescale, Timestamp
from neptune.model.units import unit_from_json
from neptune_memory.consolidate.base import Consolidation, run_consolidator
from neptune_memory.consolidate.coverage import CoverageConsolidator
from neptune_memory.schema.claim import Claim, TypedLiteral, ValueType
from neptune_memory.schema.interval import OPEN, CivilClock, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

TX = ledger_tx(9)
T0: Final = 1_790_000_000 * SECOND
REC, DESC = MemberRole.RECORDING, MemberRole.DESCRIPTION
EXAMPLES: Final = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "model"
HERTZ: Final = unit_from_json("Hz")


def consolidate(packages: Mapping[str, Sequence[Record]], config: object = None) -> Consolidation:
    return run_consolidator(
        CoverageConsolidator(),
        ledger(packages),
        (),
        dict(config or {}),  # type: ignore[call-overload]
        recorded_at=TX,
        registry=CORE_PREDICATES,
    )


def node(node_type: NodeType, value: LogicalId | str) -> NodeRef:
    if isinstance(value, str):
        return NodeRef(node_type, f"record:{value}")
    return NodeRef(node_type, f"{value.namespace}:{value.value}")


def of(result: Consolidation, predicate: str, subject: NodeRef | None = None) -> list[Claim]:
    return [
        c
        for c in result.claims
        if c.predicate == predicate and (subject is None or c.subject == subject)
    ]


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


def hertz(value: float) -> TypedLiteral:
    return TypedLiteral(ValueType.QUANTITY, value, Known(HERTZ))


def span(claim: Claim) -> tuple[int, int | None]:
    end = claim.valid_to
    return claim.valid_from.ticks, None if end is OPEN else end.ticks  # type: ignore[union-attr]


# --- A mobile robot's bag ------------------------------------------------------------------------

LIDAR, CAMERA = LogicalId("serial", "VLP-16-0042"), LogicalId("serial", "CAM-7731")


def amr_bag() -> tuple[dict[str, list[Record]], dict[str, RecordId]]:
    clock_record, clock = domain("amr-07 boot", civil=False)
    run_record, run_id = run(
        "amr-07.bag", first=at(T0, clock), last=at(T0 + 10 * SECOND, clock),
        machine=LogicalId("asset-tag", "AMR-07"),
    )  # fmt: skip
    scan, scan_id = stream(
        "/scan", run_id, (clock,), count=1001, first=at(T0, clock),
        last=at(T0 + 10 * SECOND, clock), recording="amr-07.bag",
    )  # fmt: skip
    odom, odom_id = stream(
        "/odom", run_id, (clock,), count=501, first=at(T0, clock),
        last=at(T0 + 5 * SECOND, clock), recording="amr-07.bag",
    )  # fmt: skip
    config, config_id = configuration("amr-07.urdf", LogicalId("asset-tag", "AMR-07"))
    lidar, _ = component("amr-07.urdf", config_id, "front_lidar", LIDAR)
    camera, camera_id = component("amr-07.urdf", config_id, "nav_camera", CAMERA)
    records = [
        clock_record,
        run_record,
        scan,
        odom,
        # 501 samples over the declared ten seconds: half the declared rate.
        series(scan_id, clock, T0, T0 + 10 * SECOND, 501),
        # 401 samples from one second in: the declared first second is missing.
        series(odom_id, clock, T0 + SECOND, T0 + 5 * SECOND, 401),
        config,
        lidar,
        camera,
        binding("amr-07 launch", run_id, config_id),
    ]
    ids = {"run": run_id, "scan": scan_id, "odom": odom_id, "camera": camera_id, "clock": clock}
    return {"pkg-amr": records}, ids


def test_declared_100_hz_observed_50_hz_are_both_claimed_and_never_judged() -> None:
    packages, ids = amr_bag()
    result = consolidate(packages)
    scan = node(NodeType.STREAM, ids["scan"])
    (declared,) = of(result, "rate_declared", scan)
    (observed,) = of(result, "rate_observed", scan)
    assert declared.object == hertz(100.0)
    assert observed.object == hertz(50.0)
    assert span(declared) == span(observed) == (T0, T0 + 10 * SECOND + 1)
    # No tolerance is stated anywhere, so nothing says the two disagree.
    assert not [f for f in result.findings if "rate" in f.code and f.records == (ids["scan"],)]
    (recorded,) = of(result, "recorded", scan)
    assert recorded.object == node(NodeType.RUN, ids["run"])
    assert not of(result, "gap", scan)
    assert recorded.assertion_kind == declared.assertion_kind == observed.assertion_kind


def test_a_late_start_is_a_gap_where_the_declared_extent_predicts_samples() -> None:
    packages, ids = amr_bag()
    result = consolidate(packages)
    odom = node(NodeType.STREAM, ids["odom"])
    (gap,) = of(result, "gap", odom)
    assert span(gap) == (T0, T0 + SECOND)
    assert gap.valid_from.domain_id == ids["clock"]
    (recorded,) = of(result, "recorded", odom)
    assert span(recorded) == (T0 + SECOND, T0 + 5 * SECOND + 1)
    (observed,) = of(result, "rate_observed", odom)
    assert observed.object == hertz(100.0)  # 400 intervals over 4 s


def test_a_camera_configured_with_no_stream_naming_it_is_unknown_not_absent() -> None:
    """The compiler states no stream's sensor, so a bag of lidar and odometry streams could hold
    the camera's images as far as the evidence says: its absence is never claimed."""
    packages, ids = amr_bag()
    result = consolidate(packages)
    amr = node(NodeType.RUN, ids["run"])
    assert not of(result, "sensor_not_recorded") and not of(result, "sensor_recorded")
    unknown = {c.object for c in of(result, "sensor_presence_unknown", amr)}
    assert unknown == {node(NodeType.SENSOR, CAMERA), node(NodeType.SENSOR, LIDAR)}
    undecided = [
        f for f in result.findings if f.code == "coverage.presence_undecided"
        and ids["camera"] in f.records
    ]  # fmt: skip
    assert undecided and undecided[0].details["reasons"] == ["streams_declare_no_sensor"]


# --- A truncated bag -----------------------------------------------------------------------------


def test_a_truncated_bag_has_a_trailing_gap_and_the_compilers_findings() -> None:
    clock_record, clock = domain("agv boot", civil=False)
    run_record, run_id = run("agv.bag", first=at(T0, clock), last=at(T0 + 10 * SECOND, clock))
    imu, imu_id = stream(
        "/imu", run_id, (clock,), count=1001, first=at(T0, clock),
        last=at(T0 + 10 * SECOND, clock), recording="agv.bag",
    )  # fmt: skip
    truncated = finding("rosbag1.truncated", "agv.bag")
    chunk = finding(
        "rosbag1.chunk_truncated", "agv.bag", severity=Severity.WARNING, records=(imu_id,)
    )
    unrelated = finding(
        "rosbag1.topic_mismatch", "other.bag",
        category=FindingCategory.INCONSISTENT, severity=Severity.INFO,
    )  # fmt: skip
    records = [
        clock_record, run_record, imu, truncated, chunk, unrelated,
        series(imu_id, clock, T0, T0 + 6 * SECOND, 601),
    ]  # fmt: skip
    result = consolidate({"pkg-agv": records})
    stream_node = node(NodeType.STREAM, imu_id)
    (gap,) = of(result, "gap", stream_node)
    assert span(gap) == (T0 + 6 * SECOND + 1, T0 + 10 * SECOND + 1)
    run_node = node(NodeType.RUN, run_id)
    # Both are about the bag's bytes, so both qualify the run, each with its own severity.
    on_run, on_chunk = sorted(
        of(result, "integrity_finding", run_node),
        key=lambda c: c.object.value != "error",  # type: ignore[union-attr]
    )
    assert on_run.object == TypedLiteral(ValueType.TEXT, "error")
    assert truncated["id"] in on_run.provenance.records
    assert on_chunk.object == TypedLiteral(ValueType.TEXT, "warning")
    (on_stream,) = of(result, "integrity_finding", stream_node)
    assert on_stream.object == TypedLiteral(ValueType.TEXT, "warning")
    assert chunk["id"] in on_stream.provenance.records
    assert span(on_run) == span(on_stream) == (T0, T0 + 10 * SECOND + 1)
    assert all(unrelated["id"] not in c.provenance.records for c in result.claims)


# --- The drone worked example --------------------------------------------------------------------


def worked(name: str) -> list[Any]:
    root = EXAMPLES / name / "records"
    return [
        json.loads(line)
        for path in sorted(root.glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_drone_dropout_is_an_integrity_finding_on_each_stream_it_names() -> None:
    records = worked("drone")
    (dropout,) = [r for r in records if r.get("code") == "ulog.dropout"]
    (accel,) = [
        r for r in records if r["kind"] == "stream" and r["topic"]["value"] == "sensor_accel"
    ]
    clock = accel["clocks"][0]  # the ULog's own microsecond ``timestamp``
    sample = series(accel["id"], clock, 12_000_000, 12_990_000, 100)
    result = consolidate({"pkg-drone": [*records, sample]})
    flagged = of(result, "integrity_finding")
    assert {c.subject for c in flagged} == {node(NodeType.STREAM, r) for r in dropout["records"]}
    for claim in flagged:
        assert claim.object == TypedLiteral(ValueType.TEXT, "warning")
        assert span(claim) == (12_000_000, None)  # the log states no last instant: open
    # The missing software identity is about a software configuration: not the run's health.
    missing = [r["id"] for r in records if r.get("code") == "ulog.software_identity_missing"]
    assert all(missing[0] not in c.provenance.records for c in result.claims)
    (recorded,) = of(result, "recorded", node(NodeType.STREAM, accel["id"]))
    (rate,) = of(result, "rate_observed", node(NodeType.STREAM, accel["id"]))
    assert rate.object == hertz(100.0)  # 99 intervals over 0.99 s on a microsecond clock
    assert span(recorded) == (12_000_000, 12_990_001)
    # The configured accelerometer: streams name no sensor and the log is open-ended.
    (presence,) = of(result, "sensor_presence_unknown")
    assert presence.object == node(NodeType.SENSOR, LogicalId("px4.device_id", "1310988"))
    (undecided,) = [f for f in result.findings if f.code == "coverage.presence_undecided"]
    assert undecided.details["reasons"] == [
        "streams_declare_no_sensor",
        "recording_not_closed",
        "integrity_findings",
    ]


def test_mobile_robot_worked_example_states_nothing_it_cannot_ground() -> None:
    """Its streams declare counts but no extents, and the Ledger rows are absent: no rates, no
    gaps, no recorded spans, and no finding blames the evidence for it."""
    result = consolidate({"pkg-mobile": worked("mobile_robot")})
    assert result.claims == ()
    assert codes(result) == []


# --- A manipulator cell's wrist-camera video -----------------------------------------------------

WRIST, FORCE = LogicalId("serial", "WRIST-CAM-1"), LogicalId("serial", "FT-300-9")


def cell(
    *,
    last: Timestamp | None = None,
    device: Sequence[object] | None = None,
    extra: Sequence[Record] = (),
    stated_last: bool = True,
) -> tuple[Consolidation, dict[str, RecordId]]:
    clock_record, clock = domain("cell civil", civil=True)
    run_record, run_id = run(
        "cell-run.yaml", first=at(T0, clock),
        last=(last or at(T0 + 30 * SECOND, clock)) if stated_last else None,
        machine=LogicalId("asset-tag", "ARM-06"),
    )  # fmt: skip
    files = assembly("cell-run.yaml", run_id, [("wrist.mp4", REC), ("cell-run.yaml", DESC)])[0]
    clip, clip_id = video(
        "wrist.mp4",
        clock,
        *(device if device is not None else (Known(WRIST),)),  # type: ignore[arg-type]
    )
    config, config_id = configuration("arm-06.urdf", LogicalId("asset-tag", "ARM-06"))
    wrist, wrist_id = component("arm-06.urdf", config_id, "wrist_camera", WRIST)
    force, force_id = component("arm-06.urdf", config_id, "wrist_ft", FORCE)
    records = [
        clock_record, run_record, files, revision("wrist.mp4")[0], revision("cell-run.yaml")[0],
        clip, config, wrist, force, binding("cell launch", run_id, config_id), *extra,
    ]  # fmt: skip
    ids = {"run": run_id, "clip": clip_id, "wrist": wrist_id, "force": force_id, "clock": clock}
    return consolidate({"pkg-cell": records}), ids


def test_a_sensor_no_file_could_hold_is_known_absent_beside_one_that_recorded() -> None:
    result, ids = cell()
    arm = node(NodeType.RUN, ids["run"])
    (recorded,) = of(result, "sensor_recorded", arm)
    (absent,) = of(result, "sensor_not_recorded", arm)
    assert recorded.object == node(NodeType.SENSOR, WRIST)
    assert absent.object == node(NodeType.SENSOR, FORCE)
    assert ids["clip"] in recorded.provenance.records
    assert ids["clip"] in absent.provenance.records  # what shows the force sensor recorded nothing
    assert not of(result, "sensor_presence_unknown")
    # The run's clock declares itself civil: the claims are on civil time.
    civil = CivilClock(Timescale.POSIX, Epoch.UNIX, NS)
    assert absent.valid_from == civil.at(T0)
    assert absent.valid_to == civil.at(T0 + 30 * SECOND + 1)


def test_an_open_ended_recording_never_makes_a_sensor_known_absent() -> None:
    result, _ = cell(stated_last=False)
    assert not of(result, "sensor_not_recorded")
    (unknown,) = of(result, "sensor_presence_unknown")
    assert unknown.object == node(NodeType.SENSOR, FORCE)
    assert unknown.valid_to is OPEN


def test_lost_bytes_in_a_member_file_withhold_known_absent() -> None:
    corrupt = finding("mp4.truncated", "wrist.mp4")
    result, ids = cell(extra=[corrupt])
    arm = node(NodeType.RUN, ids["run"])
    (flag,) = of(result, "integrity_finding", arm)
    assert flag.object == TypedLiteral(ValueType.TEXT, "error")
    assert not of(result, "sensor_not_recorded")
    assert [c.object for c in of(result, "sensor_presence_unknown")] == [
        node(NodeType.SENSOR, FORCE)
    ]
    assert of(result, "sensor_recorded")  # the camera's frames that were read still cite it


def test_a_file_that_may_be_the_sensors_withholds_known_absent() -> None:
    """A video citing an ambiguous device that may be the force sensor, or a device no configured
    sensor declares, could be the force sensor's: unknown, never absent."""
    for device in (
        [ambiguous("exif", WRIST, FORCE)],
        [Known(LogicalId("serial", "SOMEONE-ELSE"))],
        [],
    ):
        result, ids = cell(device=device)
        assert not of(result, "sensor_not_recorded"), device
        assert not of(result, "sensor_recorded"), device  # an ambiguous citation is not a record
        presences = {c.object for c in of(result, "sensor_presence_unknown")}
        assert node(NodeType.SENSOR, FORCE) in presences
        (undecided, *_) = [
            f for f in result.findings if f.code == "coverage.presence_undecided"
            and ids["force"] in f.records
        ]  # fmt: skip
        assert undecided.details["reasons"] == ["files_not_attributed"]


def test_an_image_member_attributes_by_its_exif_serial() -> None:
    photo, photo_id = image("inspection.jpg", Known(FORCE))
    del photo_id
    clock_record, clock = domain("cell boot", civil=False)
    run_record, run_id = run("survey.yaml", first=at(0, clock), last=at(SECOND, clock))
    files = assembly("survey.yaml", run_id, [("inspection.jpg", REC)])[0]
    config, config_id = configuration("probe.yaml", LogicalId("asset-tag", "PROBE-1"))
    probe = component("probe.yaml", config_id, "probe", FORCE)[0]
    result = consolidate(
        {
            "pkg": [
                clock_record,
                run_record,
                files,
                revision("inspection.jpg")[0],
                photo,
                config,
                probe,
                binding("survey", run_id, config_id),
            ]
        }
    )
    (recorded,) = of(result, "sensor_recorded")
    assert recorded.object == node(NodeType.SENSOR, FORCE)


# --- A quadruped's streams on two clocks ----------------------------------------------------------


def test_untimed_samples_withhold_gaps_and_observed_rate_on_their_clock_only() -> None:
    log_record, log_time = domain("quadruped log_time", civil=False)
    stamp_record, header = domain("quadruped header.stamp", civil=False)
    run_record, run_id = run(
        "spot-12.mcap", first=at(T0, log_time), last=at(T0 + 2 * SECOND, log_time),
        machine=LogicalId("asset-tag", "Q-12"),
    )  # fmt: skip
    joints, joints_id = stream(
        "/joint_states", run_id, (log_time, header), count=201, first=at(T0, log_time),
        last=at(T0 + 2 * SECOND, log_time), recording="spot-12.mcap",
    )  # fmt: skip
    records = [
        log_record, stamp_record, run_record, joints,
        series(joints_id, log_time, T0 + HZ_100, T0 + 2 * SECOND, 200),
        # Header stamps: ten samples carry none, so the header clock's gaps are not decided.
        series(joints_id, header, 5_000, 5_000 + 189 * HZ_100, 190, rows_unknown=10),
    ]  # fmt: skip
    result = consolidate({"pkg-quadruped": records})
    subject = node(NodeType.STREAM, joints_id)
    recorded = of(result, "recorded", subject)
    assert {c.valid_from.domain_id for c in recorded} == {log_time, header}
    (gap,) = of(result, "gap", subject)
    assert gap.valid_from.domain_id == log_time and span(gap) == (T0, T0 + HZ_100)
    (observed,) = of(result, "rate_observed", subject)
    assert observed.valid_from.domain_id == log_time
    assert "coverage.untimed_samples" in codes(result)


def test_a_run_with_a_declared_id_is_its_thread_node() -> None:
    clock_record, clock = domain("boat", civil=False)
    run_record, run_id = run(
        "survey-3.mcap", first=at(0, clock), last=at(SECOND, clock),
        logical_id=LogicalId("manifest", "survey-3"),
    )  # fmt: skip
    sonar, sonar_id = stream("/sonar", run_id, (clock,), recording="survey-3.mcap")
    result = consolidate(
        {"pkg": [clock_record, run_record, sonar, series(sonar_id, clock, 0, SECOND, 11)]}
    )
    (recorded,) = of(result, "recorded")
    assert recorded.object == node(NodeType.RUN, LogicalId("manifest", "survey-3"))
    assert recorded.subject == node(NodeType.STREAM, sonar_id)
    assert Timestamp(0, clock) == recorded.valid_from

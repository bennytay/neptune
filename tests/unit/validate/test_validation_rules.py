"""The integrity and data-quality engine (ADR 0054): each rule, over packages from four kinds of
robot (an arm, a mobile base, a legged robot, a multirotor), on clean and damaged evidence.

Packages are built from records and real Parquet series with the store's own writers and read
back with ``read_package``, as the job does; no rule sees anything a package would not hold.
"""

import io
import time
import tracemalloc
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import pytest

from neptune.identity.findings import ingest_finding
from neptune.identity.hashing import content_id, digest_stream
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.frames import FrameRef
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import AssertionKind, Known, NotApplicable, NotCovered, Unknown
from neptune.model.machine import (
    Calibration,
    CalibrationParameter,
    ComponentCategory,
    HardwareComponent,
    HardwareConfiguration,
    Machine,
    SoftwareConfiguration,
    SoftwareItem,
)
from neptune.model.package import SEVERITY_ORDER
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, Row, adapter_locator
from neptune.model.reference import Frame, FrameGraph, TimestampDomain
from neptune.model.run import Run, Stream
from neptune.model.series import (
    ColumnType,
    SeriesBatch,
    SeriesColumn,
    SeriesProvenance,
    step_template,
)
from neptune.model.source import LocalPath
from neptune.model.time import NANOSECOND, ClockRole, Timestamp
from neptune.model.units import Unit, unit_from_json
from neptune.model.versions import DeclaredVersion, GitCommit, SemanticVersion
from neptune.model.world import StructuredRecord, StructuredTable
from neptune.store.assemble import StagedPackage, amend
from neptune.store.package import IngestPackage, package_contents, read_package, write_package
from neptune.store.series import SERIES_SETTINGS, write_series
from neptune.validate import (
    ALL_RULES,
    DEFAULT_RULES,
    FINDINGS_CAPPED,
    VALIDATOR_ID,
    Bounds,
    Inputs,
    validate_package,
)

T = TypeVar("T")
ROBOTS = ("arm", "mobile_base", "legged", "aerial")
MACHINES = {
    "arm": LogicalId("serial", "ur5e-0042"),
    "mobile_base": LogicalId("serial", "tb4-0007"),
    "legged": LogicalId("serial", "spot-1234"),
    "aerial": LogicalId("px4.sys_uuid", "000100000000363533365113"),
}
TOPICS = {
    "arm": "/joint_states",
    "mobile_base": "/odom",
    "legged": "/imu",
    "aerial": "vehicle_attitude",
}


class Kit:
    """One source's records, as one adapter would have made them, citing distinct places in it."""

    def __init__(self, name: str, adapter: str = "fixture") -> None:
        self.name = name
        self.data = f"{name} evidence ".encode() * 64
        self.source = content_id(self.data)
        self.transform = transform_record(adapter_id=adapter, adapter_version="1.0.0", config={})
        self.records: list[Any] = [self.transform]
        self.batches: dict[RecordId, SeriesBatch] = {}
        self.streams: dict[RecordId, Stream] = {}
        self._places = 0

    def cite(self, label: str = "") -> Provenance:
        self._places += 1
        locator = adapter_locator("fixture:item", {"label": label, "n": self._places})
        evidence = EvidenceRef(self.source, (ByteRange(0, len(self.data)), locator))
        return Provenance(evidence, self.transform.id, AssertionKind.OBSERVED)

    def row(self, index: int) -> Provenance:
        """A table row as its own citation, as the tabular adapter cites one."""
        evidence = EvidenceRef(self.source, (Row(index),))
        return Provenance(evidence, self.transform.id, AssertionKind.STATED)

    def id_of(self, kind: str, provenance: Provenance) -> RecordId:
        return evidence_record_id(kind, provenance.evidence, self.transform)

    def add(self, record: T) -> T:
        self.records.append(record)
        return record

    def clock(self, monotonic: Any = None, resolution: Any = None) -> TimestampDomain:
        at = self.cite("clock")
        return self.add(
            TimestampDomain(
                id=self.id_of("timestamp_domain", at),
                provenance=at,
                field="log_time",
                scope=(),
                role=Known(ClockRole.RECEIVE),
                resolution=Known(NANOSECOND) if resolution is None else resolution,
                epoch=Unknown(),
                timescale=Unknown(),
                declared_monotonic=Unknown() if monotonic is None else monotonic,
            )
        )

    def run(
        self,
        clock: TimestampDomain,
        first: int | None = 0,
        last: int | None = 10_000,
        machine: LogicalId | None = None,
        logical: LogicalId | None = None,
    ) -> Run:
        at = self.cite("run")
        return self.add(
            Run(
                id=self.id_of("run", at),
                provenance=at,
                logical_id=Unknown() if logical is None else Known(logical),
                machine=Known(machine or MACHINES[self.name]),
                first=Unknown() if first is None else Known(Timestamp(first, clock.id)),
                last=Unknown() if last is None else Known(Timestamp(last, clock.id)),
            )
        )

    def stream(
        self,
        run: Run,
        clocks: Sequence[TimestampDomain],
        times: Sequence[Sequence[int | None]],
        topic: str | None = None,
        count: int | None = None,
        schema: str = "sensor_msgs/msg/Imu",
        encoding: str = "cdr",
        schema_name: Any = None,
    ) -> Stream:
        """A stream whose rows, in source order, carry ``times[i]`` on clock ``i``."""
        at = self.cite(f"channel:{topic}")
        rows = len(times[0])
        stream = self.add(
            Stream(
                id=self.id_of("stream", at),
                provenance=at,
                run=run.id,
                topic=Known(topic or TOPICS[self.name]),
                schema_name=Known(schema) if schema_name is None else schema_name,
                schema_encoding=Known("ros2msg"),
                schema_definition=NotCovered(),
                message_encoding=Known(encoding),
                metadata=(),
                clocks=tuple(clock.id for clock in clocks),
                message_count=Known(rows if count is None else count),
                first=NotCovered(),
                last=NotCovered(),
                series=SeriesProvenance(
                    self.source,
                    (step_template("byte_range", per_row=("length", "offset")),),
                    AssertionKind.OBSERVED,
                ),
            )
        )
        columns: list[SeriesColumn] = [
            SeriesColumn("seq", ColumnType.INT64, tuple(range(rows))),
            SeriesColumn("locator/0/offset", ColumnType.INT64, tuple(8 * i for i in range(rows))),
            SeriesColumn("locator/0/length", ColumnType.INT64, (8,) * rows),
            SeriesColumn("value/x", ColumnType.FLOAT64, tuple(float(i) for i in range(rows))),
        ]
        for index, ticks in enumerate(times):
            columns.append(SeriesColumn(f"time/{index}", ColumnType.INT64, tuple(ticks)))
            if None in ticks:  # a wrapped column: its state says why a cell is empty
                states = tuple("unknown" if t is None else "known" for t in ticks)
                columns.append(SeriesColumn(f"state/time/{index}", ColumnType.STRING, states))
        self.batches[stream.id] = SeriesBatch(stream.id, tuple(columns))
        self.streams[stream.id] = stream
        return stream

    def finding(
        self, code: str, category: FindingCategory, records: Iterable[RecordId] = ()
    ) -> IngestFinding:
        return self.add(
            ingest_finding(
                code=code,
                category=category,
                severity=Severity.ERROR,
                subject=self.cite(code).evidence,
                transform=self.transform,
                message=f"{code} in {self.name}",
                records=records,
            )
        )


def build(directory: Path, *kits: Kit) -> IngestPackage:
    """Write ``kits`` as one package under ``directory`` and read it back verified."""
    ledger = SourceLedger()
    for kit in kits:
        ledger.observe(LocalPath(f"{kit.name}.bin"), digest_stream(io.BytesIO(kit.data)))
    directory.mkdir(parents=True, exist_ok=True)
    series: dict[RecordId, Path] = {}
    for kit in kits:
        for stream_id, batch in kit.batches.items():
            path = directory / f"{stream_id[-12:]}.parquet"
            write_series(kit.streams[stream_id], [batch], path)
            series[stream_id] = path
    records = [*ledger.artifacts(), *ledger.revisions()]
    seen: set[str] = set()
    for kit in kits:
        for record in kit.records:
            if record.id not in seen:
                seen.add(record.id)
                records.append(record)
    contents = package_contents(
        records, series=series, store={"series": SERIES_SETTINGS} if series else {}
    )
    write_package(directory / "package", contents)
    return read_package(directory / "package")


def codes(package: IngestPackage, **kwargs: Any) -> list[str]:
    return [
        finding.code.removeprefix(f"{VALIDATOR_ID}.")
        for finding in validate_package(package, **kwargs).findings
    ]


def clean(name: str) -> Kit:
    """A healthy recording of robot ``name``: a run, a machine, one stream, two clocks in step."""
    kit = Kit(name)
    receive, header = kit.clock(), kit.clock(monotonic=Known(True))
    run = kit.run(receive)
    kit.stream(run, [receive, header], [[0, 10, 20, 30], [1, 11, 21, 31]])
    at = kit.cite("machine")
    kit.add(
        Machine(
            id=kit.id_of("machine", at),
            provenance=at,
            identifiers=(Known(MACHINES[name]),),
            manufacturer=Known(f"{name}-maker"),
            model=Known(f"{name}-model"),
        )
    )
    return kit


# --- Clean evidence: nothing to say, and the package is unchanged ------------------------------


@pytest.mark.parametrize("robot", ROBOTS)
def test_clean_evidence_from_every_robot_has_no_findings(tmp_path: Path, robot: str) -> None:
    package = build(tmp_path, clean(robot))
    report = validate_package(package)
    assert report.findings == () and report.records() == ()
    assert {o.code for o in report.rules if o.covered} == {r.code for r in DEFAULT_RULES}


def test_a_fleet_of_four_robots_is_clean_together(tmp_path: Path) -> None:
    assert codes(build(tmp_path, *(clean(robot) for robot in ROBOTS))) == []


# --- Truncation and corruption -------------------------------------------------------------------


def test_a_truncated_recording_is_rolled_up_and_its_count_disagrees(tmp_path: Path) -> None:
    kit = Kit("aerial")
    clock = kit.clock()
    run = kit.run(clock)
    stream = kit.stream(run, [clock], [[0, 10, 20]], count=5)  # the summary says 5; 3 were read
    kit.finding("ulog.truncated", FindingCategory.CORRUPT, [stream.id])
    kit.finding("ulog.bad_message", FindingCategory.CORRUPT)
    package = build(tmp_path, kit, clean("arm"))
    report = validate_package(package)
    by_code = {f.code.removeprefix(f"{VALIDATOR_ID}."): f for f in report.findings}
    assert set(by_code) == {"count_mismatch", "source_incomplete"}
    rollup = by_code["source_incomplete"]
    assert rollup.subject == EvidenceRef(kit.source, (ByteRange(0, len(kit.data)),))
    assert rollup.details["codes"] == {"ulog.bad_message": 1, "ulog.truncated": 1}
    assert len(rollup.related) == 2 and set(rollup.records) == {run.id, stream.id, clock.id}
    count = by_code["count_mismatch"]
    assert (count.details["declared"], count.details["stored"], count.details["missing"]) == (
        5,
        3,
        2,
    )
    assert count.severity is Severity.WARNING and count.records == (stream.id,)


# --- Timestamp monotonicity ----------------------------------------------------------------------


def test_a_clock_that_steps_back_in_source_order_is_cited_at_the_sample(tmp_path: Path) -> None:
    kit = Kit("legged")
    clock = kit.clock(monotonic=Known(True))
    run = kit.run(clock)
    # Source order: 0, 10, 30, 20 (seq 3 is stamped before seq 2), then 40.
    stream = kit.stream(run, [clock], [[0, 10, 30, 20, 40]])
    (finding,) = validate_package(build(tmp_path, kit)).findings
    assert finding.code == f"{VALIDATOR_ID}.time_regression"
    assert finding.details["seq"] == 3 and finding.details["ticks"] == 20
    assert finding.details["previous_seq"] == 2 and finding.details["previous_ticks"] == 30
    assert finding.details["declared_monotonic"] == "true" and finding.details["descents"] == 1
    assert finding.subject == stream.row_evidence({"locator/0/offset": 24, "locator/0/length": 8})


def test_a_second_clock_is_judged_in_source_order(tmp_path: Path) -> None:
    kit = Kit("mobile_base")
    receive, header = kit.clock(), kit.clock()
    run = kit.run(receive)
    kit.stream(run, [receive, header], [[0, 10, 20, 30], [5, 4, 6, 3]])  # header falls twice
    (finding,) = validate_package(build(tmp_path, kit)).findings
    assert finding.details["clock"] == 1 and finding.details["descents"] == 2
    assert (finding.details["seq"], finding.details["previous_seq"]) == (1, 0)


def test_unknown_times_and_equal_ticks_are_not_regressions(tmp_path: Path) -> None:
    kit = Kit("arm")
    clock = kit.clock()
    run = kit.run(clock)
    kit.stream(run, [clock], [[0, None, 10, 10, None, 20]])
    assert codes(build(tmp_path, kit)) == []


def test_unknown_clock_zero_does_not_hide_a_fall(tmp_path: Path) -> None:
    kit = Kit("arm")
    clock = kit.clock()
    run = kit.run(clock)
    kit.stream(run, [clock], [[0, None, 30, 20]])
    package = build(tmp_path, kit)
    (finding,) = validate_package(package).findings
    assert (finding.details["seq"], finding.details["descents"]) == (3, 1)


# --- Impossible ranges ----------------------------------------------------------------------------


def test_a_run_that_ends_before_it_starts_on_one_clock(tmp_path: Path) -> None:
    kit = Kit("aerial")
    clock, other = kit.clock(), kit.clock()
    run = kit.run(clock, first=500, last=100)
    at = kit.cite("run2")
    kit.add(  # different clocks: never compared, whatever the ticks
        Run(
            id=kit.id_of("run", at),
            provenance=at,
            logical_id=Unknown(),
            machine=Unknown(),
            first=Known(Timestamp(500, clock.id)),
            last=Known(Timestamp(100, other.id)),
        )
    )
    (finding,) = validate_package(build(tmp_path, kit)).findings
    assert finding.code.endswith("interval_reversed") and finding.records == (run.id,)
    assert (finding.details["start"], finding.details["end"]) == (500, 100)


class Limit:
    def __init__(
        self, stream: RecordId, evidence: EvidenceRef, unit: Unit, column_unit: Any
    ) -> None:
        self.stream, self.evidence = stream, evidence
        self.column, self.lower, self.upper = "value/x", 0.0, 2.0
        self.unit, self.column_unit = Known(unit), column_unit


def test_a_declared_limit_is_off_by_default_and_checked_only_in_its_own_unit(
    tmp_path: Path,
) -> None:
    kit = Kit("arm")
    clock = kit.clock()
    stream = kit.stream(kit.run(clock), [clock], [[0, 1, 2, 3, 4]])  # value/x is 0..4
    package = build(tmp_path, kit)
    report = validate_package(package)
    off = {o.code: o for o in report.rules}[f"{VALIDATOR_ID}.declared_limit_exceeded"]
    assert not off.covered and off.reason and "no record kind" in off.reason
    rad = unit_from_json("rad")
    urdf = kit.cite("urdf:limit").evidence
    same = Limit(stream.id, urdf, rad, Known(rad))
    (finding,) = validate_package(package, inputs=Inputs(limits=(same,))).findings
    assert (finding.details["above"], finding.details["below"]) == (2, 0)
    assert finding.related == (urdf,)
    for column_unit in (Unknown(), Known(unit_from_json("deg"))):  # never assumed or converted
        unsure = Limit(stream.id, urdf, rad, column_unit)
        assert validate_package(package, inputs=Inputs(limits=(unsure,))).findings == ()


# --- Missing metadata and schema mismatches ------------------------------------------------------


def test_missing_metadata_is_one_finding_per_source_and_field(tmp_path: Path) -> None:
    kit = Kit("legged")
    clock = kit.clock(resolution=Unknown())
    run = kit.run(clock)
    for topic in ("/a", "/b", "/c"):
        kit.stream(run, [clock], [[0, 1]], topic=topic, schema_name=Unknown())
    found = validate_package(build(tmp_path, kit)).findings
    fields = sorted((f.details["kind"], f.details["field"], f.details["count"]) for f in found)
    assert fields == [("stream", "schema_name", 3), ("timestamp_domain", "resolution", 1)]
    assert all(f.category is FindingCategory.MISSING for f in found)


def test_one_topic_two_schemas_in_one_run(tmp_path: Path) -> None:
    kit = Kit("mobile_base")
    clock = kit.clock()
    run = kit.run(clock)
    kit.stream(run, [clock], [[0]], topic="/odom", schema="nav_msgs/msg/Odometry")
    kit.stream(run, [clock], [[1]], topic="/odom", schema="geometry_msgs/msg/Pose", encoding="json")
    other = kit.run(clock)
    kit.stream(other, [clock], [[2]], topic="/odom", schema="other/Thing")  # another run: fine
    (finding,) = validate_package(build(tmp_path, kit)).findings
    assert finding.code.endswith("schema_conflict") and finding.details["run"] == run.id
    assert finding.details["differ"] == {
        "message_encoding": ["cdr", "json"],
        "schema_name": ["geometry_msgs/msg/Pose", "nav_msgs/msg/Odometry"],
    }


def test_rows_that_do_not_fit_their_header(tmp_path: Path) -> None:
    kit = Kit("arm")
    at = kit.cite("table")
    table = kit.add(
        StructuredTable(
            id=kit.id_of("structured_table", at),
            provenance=at,
            name=NotCovered(),
            header=Known(("joint", "lower", "upper")),
        )
    )
    for row, cells in enumerate((("j1", "-3.1", "3.1"), ("j2", "-2"), ("j3", "0", "1", "x")), 1):
        at = kit.row(row)
        kit.add(
            StructuredRecord(
                id=kit.id_of("structured_record", at),
                provenance=at,
                table=table.id,
                row=row,
                cells=tuple(Known(cell) for cell in cells),
            )
        )
    (finding,) = validate_package(build(tmp_path, kit)).findings
    assert finding.details["rows"] == 2 and finding.details["cells"] == [2, 4]
    assert finding.details["first_row"] == 2


# --- Duplicate and conflicting ids ----------------------------------------------------------------


def machine(kit: Kit, identifier: LogicalId, model: str) -> Machine:
    at = kit.cite(f"machine:{model}")
    return kit.add(
        Machine(
            id=kit.id_of("machine", at),
            provenance=at,
            identifiers=(Known(identifier),),
            manufacturer=NotCovered(),
            model=Known(model),
        )
    )


def test_one_id_two_models_is_a_conflict_and_never_a_merge(tmp_path: Path) -> None:
    fleet, manifest = Kit("legged", "register"), Kit("aerial", "manifest")
    spot = LogicalId("serial", "R-1")
    first = machine(fleet, spot, "Spot")
    second = machine(manifest, spot, "X500")
    machine(fleet, LogicalId("serial", "R-2"), "TurtleBot 4")
    package = build(tmp_path, fleet, manifest)
    (finding,) = validate_package(package).findings
    assert finding.code.endswith("id_conflict") and finding.details["values"] == ["Spot", "X500"]
    assert set(finding.records) == {first.id, second.id}
    assert len([r for r in package.records if r.kind == "machine"]) == 3  # nothing merged


def test_one_source_stating_one_id_twice(tmp_path: Path) -> None:
    register = Kit("mobile_base", "register")
    machine(register, LogicalId("asset", "AMR-9"), "MiR250")
    machine(register, LogicalId("asset", "AMR-9"), "MiR250")
    (finding,) = validate_package(build(tmp_path, register)).findings
    assert finding.code.endswith("duplicate_id") and finding.category is FindingCategory.AMBIGUOUS


def test_a_stream_naming_a_clock_the_package_lacks(tmp_path: Path) -> None:
    kit = Kit("aerial")
    clock = kit.clock()
    ghost = Kit("aerial", "other").clock()  # declared by a source this package does not hold
    run = kit.run(clock)
    kit.stream(run, [clock, ghost], [[0, 1], [0, 1]])
    (finding,) = validate_package(build(tmp_path, kit)).findings
    assert finding.code.endswith("dangling_reference") and finding.details["target"] == ghost.id


# --- Unresolved frames ----------------------------------------------------------------------------


def test_frames_a_graph_never_declares_or_a_graph_that_is_missing(tmp_path: Path) -> None:
    kit = Kit("arm")
    at = kit.cite("robot")
    graph = kit.add(FrameGraph(id=kit.id_of("frame_graph", at), provenance=at, scope=()))
    for link in ("base_link", "tool0"):
        at = kit.cite(link)
        kit.add(
            Frame(
                id=kit.id_of("frame", at),
                provenance=at,
                ref=FrameRef(link, graph.id),
                axes=Unknown(),
                handedness=Unknown(),
            )
        )
    at = kit.cite("hardware")
    config = kit.add(
        HardwareConfiguration(
            id=kit.id_of("hardware_configuration", at),
            provenance=at,
            machine=NotCovered(),
            name=Known("ur5e"),
            revision=NotCovered(),
        )
    )
    missing_graph = RecordId("rec:sha256:" + "ab" * 32)
    for name, ref in (
        ("tool0", FrameRef("tool0", graph.id)),
        ("camera", FrameRef("wrist_camera", graph.id)),
        ("gripper", FrameRef("tcp", missing_graph)),
    ):
        at = kit.cite(name)
        kit.add(
            HardwareComponent(
                id=kit.id_of("hardware_component", at),
                provenance=at,
                configuration=config.id,
                category=ComponentCategory.LINK,
                name=Known(name),
                model=NotCovered(),
                identifiers=(),
                frame=Known(ref),
            )
        )
    found = validate_package(build(tmp_path, kit)).findings
    assert sorted((f.details["frame"], f.details["reason"]) for f in found) == [
        ("tcp", "graph_missing"),
        ("wrist_camera", "frame_undeclared"),
    ]


# --- Stale configuration -------------------------------------------------------------------------


def calibration(kit: Kit, clock: TimestampDomain, revision: str, since: int, until: int) -> Any:
    at = kit.cite(f"calibration:{revision}")
    return kit.add(
        Calibration(
            id=kit.id_of("calibration", at),
            provenance=at,
            machine=Known(MACHINES[kit.name]),
            hardware_revision=Known(DeclaredVersion(revision)),
            subject=Known("cam0"),
            performed=NotCovered(),
            valid_from=Known(Timestamp(since, clock.id)),
            valid_until=Known(Timestamp(until, clock.id)),
            parameters=(CalibrationParameter("fx", Known("612.4"), NotApplicable()),),
            extrinsics=(),
        )
    )


def test_a_calibration_for_other_hardware_and_outside_its_window(tmp_path: Path) -> None:
    kit = Kit("legged")
    clock = kit.clock()
    at = kit.cite("hardware")
    kit.add(
        HardwareConfiguration(
            id=kit.id_of("hardware_configuration", at),
            provenance=at,
            machine=Known(MACHINES["legged"]),
            name=Known("spot"),
            revision=Known(DeclaredVersion("rev-C")),
        )
    )
    stale = calibration(kit, clock, "rev-B", 0, 1_000)
    calibration(kit, clock, "rev-C", 0, 1_000_000)
    early = kit.run(clock, first=2_000, last=3_000)
    later = kit.run(clock, first=5_000, last=6_000)
    found = {
        f.code.removeprefix(f"{VALIDATOR_ID}."): f
        for f in validate_package(build(tmp_path, kit)).findings
    }
    assert set(found) == {"calibration_out_of_window", "calibration_revision_mismatch"}
    assert found["calibration_revision_mismatch"].details["declared"] == ["rev-C"]
    window = found["calibration_out_of_window"]
    assert window.details["after_valid_until"] == 2
    assert set(window.records) == {stale.id, early.id, later.id}


def test_a_document_revision_superseded_in_the_package(tmp_path: Path) -> None:
    kit = Kit("mobile_base")
    package = build(tmp_path, kit)

    @dataclass(frozen=True)
    class Revision:
        record: RecordId
        document: LogicalId
        revision: str
        supersedes: tuple[str, ...]
        evidence: EvidenceRef

    sop = LogicalId("sop", "dock-charging")
    old = Revision(RecordId("rec:sha256:" + "01" * 32), sop, "B", (), kit.cite("B").evidence)
    new = Revision(RecordId("rec:sha256:" + "02" * 32), sop, "C", ("B",), kit.cite("C").evidence)
    (finding,) = validate_package(package, inputs=Inputs(document_revisions=(old, new))).findings
    assert finding.subject == old.evidence and finding.details["superseded_by"] == ["C"]


# --- Software bindings ---------------------------------------------------------------------------


def software(kit: Kit, name: str, release: str, commit: str) -> SoftwareConfiguration:
    at = kit.cite(f"software:{release}:{commit}")
    return kit.add(
        SoftwareConfiguration(
            id=kit.id_of("software_configuration", at),
            provenance=at,
            machine=Known(MACHINES[kit.name]),
            software=(
                SoftwareItem(
                    name=Known(name),
                    device=NotCovered(),
                    commit=Known(GitCommit(commit)),
                    release=Known(SemanticVersion(release)),
                    build=NotCovered(),
                    digest=NotCovered(),
                ),
            ),
        )
    )


def test_one_release_declared_with_two_commits(tmp_path: Path) -> None:
    flight, ground = Kit("aerial", "ulog"), Kit("aerial", "manifest")
    a = software(flight, "PX4", "1.14.0", "a" * 40)
    b = software(ground, "PX4", "1.14.0", "b" * 40)
    software(ground, "PX4", "1.15.0", "c" * 40)  # another release, another commit: fine
    package = build(tmp_path, flight, ground)
    (finding,) = validate_package(package).findings
    assert finding.code.endswith("software_conflict") and set(finding.records) == {a.id, b.id}
    assert finding.details["values"] == ["a" * 40, "b" * 40]

    @dataclass(frozen=True)
    class Binding:
        run: RecordId
        configuration: RecordId
        evidence: EvidenceRef

    run = RecordId("rec:sha256:" + "03" * 32)
    bindings = (
        Binding(run, a.id, a.provenance.evidence),
        Binding(run, b.id, b.provenance.evidence),
    )
    report = validate_package(package, inputs=Inputs(run_software=bindings))
    assert sorted(codes(package, inputs=Inputs(run_software=bindings))) == [
        "run_software_conflict",
        "software_conflict",
    ]
    assert {o.code for o in report.rules if not o.covered} == {
        f"{VALIDATOR_ID}.declared_limit_exceeded",
        f"{VALIDATOR_ID}.stale_document_revision",
    }


# --- Determinism, idempotence, severity, bounds --------------------------------------------------


def damaged(directory: Path) -> IngestPackage:
    kits = []
    for robot in ROBOTS:
        kit = Kit(robot)
        clock = kit.clock()
        run = kit.run(clock, first=9, last=1)
        kit.stream(run, [clock], [[3, 2, 1]], count=4, schema_name=Unknown())
        kit.finding(f"{robot.replace('_', '')}.truncated", FindingCategory.CORRUPT)
        kits.append(kit)
    return build(directory, *kits)


def test_validation_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    package = damaged(tmp_path / "a")
    report = validate_package(package)
    again = validate_package(damaged(tmp_path / "b"))
    assert [f.to_json() for f in report.findings] == [f.to_json() for f in again.findings]
    assert report.transform == again.transform
    # The package with its findings added validates to the same findings: rules ignore their own.
    staged = StagedPackage(tmp_path / "a" / "package", tmp_path / "out", package.id)
    amended = read_package(amend(staged, package, report.records()).path)
    assert validate_package(amended).findings == report.findings
    assert len(amended.receipt.findings) == len(package.receipt.findings) + len(report.findings)


def test_findings_are_ranked_by_a_fixed_severity_order(tmp_path: Path) -> None:
    assert SEVERITY_ORDER == (Severity.ERROR, Severity.WARNING, Severity.INFO)
    assert {rule.severity for rule in ALL_RULES} <= set(SEVERITY_ORDER)
    package = damaged(tmp_path)
    staged = StagedPackage(tmp_path / "package", tmp_path / "out", package.id)
    report = validate_package(package)
    receipt = read_package(amend(staged, package, report.records()).path).receipt
    ranks = [SEVERITY_ORDER.index(f.severity) for f in receipt.findings]
    assert ranks == sorted(ranks) and Severity.ERROR in {f.severity for f in receipt.findings}
    assert all(str(f.details["rule"]).endswith("/1") for f in report.findings)


def test_every_finding_cites_evidence_in_the_package(tmp_path: Path) -> None:
    package = damaged(tmp_path)
    sources = {a.content_id for a in package.records if a.kind == "source_artifact"}
    ids = {getattr(r, "id", None) for r in package.records}
    for finding in validate_package(package).findings:
        assert isinstance(finding.subject, EvidenceRef) and finding.subject.source in sources
        assert all(ref.source in sources for ref in finding.related)
        assert finding.records and set(finding.records) <= ids


def test_a_rule_that_finds_too_much_is_capped_and_says_so(tmp_path: Path) -> None:
    package = damaged(tmp_path)
    tight = Bounds(findings_per_rule=1, records_per_finding=1, related_per_finding=1)
    report = validate_package(package, bounds=tight)
    capped = [f for f in report.findings if f.code == FINDINGS_CAPPED]
    assert capped and all(f.severity is Severity.INFO for f in capped)
    per_rule: dict[str, int] = {}
    for finding in report.findings:
        if finding.code != FINDINGS_CAPPED:
            per_rule[finding.code] = per_rule.get(finding.code, 0) + 1
            assert len(finding.records) <= 1 and len(finding.related) <= 1
    assert set(per_rule.values()) == {1}
    assert report.transform != validate_package(package).transform  # bounds are in the lineage


def test_bounds_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        Bounds(findings_per_rule=0)


# --- Hostile scale -------------------------------------------------------------------------------


@pytest.mark.slow
def test_a_large_package_is_validated_in_bounded_time_memory_and_output(tmp_path: Path) -> None:
    """400k rows that fall every other sample and 20k rows of a ragged table: every rule is
    linear, the series is read a batch at a time, and the output stays within its bounds."""
    rows, records = 400_000, 20_000
    kit = Kit("legged")
    clock = kit.clock()
    run = kit.run(clock)
    ticks = [i + (2 if i % 2 == 0 else -2) for i in range(rows)]  # falls every other row
    kit.stream(run, [clock], [ticks], count=rows + 1)
    at = kit.cite("table")
    table = kit.add(
        StructuredTable(
            id=kit.id_of("structured_table", at),
            provenance=at,
            name=NotCovered(),
            header=Known(("a", "b")),
        )
    )
    for row in range(records):
        at = kit.row(row + 1)
        kit.add(
            StructuredRecord(
                id=kit.id_of("structured_record", at),
                provenance=at,
                table=table.id,
                row=row + 1,
                cells=(Known("x"),),
            )
        )
        if row < 10_000:  # 5,000 ids, each stated twice by one source with two models
            machine(kit, LogicalId("asset", f"A-{row % 5000}"), f"m{row % 3}")
    package = build(tmp_path, kit)
    tracemalloc.start()
    started = time.process_time()
    report = validate_package(package)
    elapsed = time.process_time() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    bounds = Bounds()
    assert len(report.findings) <= len(ALL_RULES) * (bounds.findings_per_rule + 1)
    assert all(len(f.records) <= bounds.records_per_finding for f in report.findings)
    regression = next(f for f in report.findings if f.code.endswith("time_regression"))
    assert regression.details["descents"] == rows // 2 - 1
    assert peak < 256 * 1024 * 1024 and elapsed < 30, (peak, elapsed)

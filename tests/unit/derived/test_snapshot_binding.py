"""The snapshot binding pass over records and a grouping, without a job (ADR 0064).

Hardware and calibration snapshots have no adapter in every tree yet, so their binding is pinned
here with records built by hand; the stated rule's limits (one run per source, one value naming
several snapshots), the per-run scope (a run's own snapshots, never another recording's sidecar),
cost linear in runs, independence from the ingest root and from input order, and the derived
record's strict JSON are pinned too.
"""

import hashlib
import json
import random
import time
from typing import Any

import pytest

from neptune.derived.bindings import (
    BINDING_KIND,
    CONFLICTING_SNAPSHOTS,
    NO_SOFTWARE_IDENTITY,
    SHARED_SNAPSHOT,
    SNAPSHOT_UNRESOLVED,
    Bindings,
    InferredSnapshotBinding,
    _resolve,
    bind_snapshots,
    inferred_snapshot_binding_from_json,
)
from neptune.derived.grouping import LayoutGrouper
from neptune.derived.sessions import read_derived
from neptune.discovery.layout import LayoutFile, layout_of
from neptune.identity.ids import record_id
from neptune.model.alignment import (
    MemberRole,
    RunAssembly,
    RunMember,
    SnapshotBinding,
    SnapshotKind,
)
from neptune.model.ids import ContentId, RecordId
from neptune.model.knowledge import AssertionKind, Known, NotApplicable, NotCovered, Unknown
from neptune.model.machine import (
    Calibration,
    HardwareConfiguration,
    SoftwareConfiguration,
    SoftwareItem,
)
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, Row
from neptune.model.run import Run
from neptune.model.source import local_location
from neptune.model.versions import FirmwareVersion, GitCommit
from neptune.model.world import StructuredRecord

TRANSFORM = record_id("transform_record", {"adapter": "test"})


def content(path: str) -> ContentId:
    return ContentId("sha256:" + hashlib.sha256(path.encode()).hexdigest())


def cite(path: str, *steps: Any) -> Provenance:
    ref = EvidenceRef(content(path), (ByteRange(0, 16), *steps))
    return Provenance(ref, TRANSFORM, AssertionKind.OBSERVED)


def rid(*parts: str) -> RecordId:
    return record_id("test", {"parts": list(parts)})


def run(path: str, n: int = 0) -> Run:
    return Run(
        id=rid("run", path, str(n)),
        provenance=cite(path) if n == 0 else cite(path, Row(n)),
        logical_id=Unknown(),
        machine=Unknown(),
        first=Unknown(),
        last=Unknown(),
    )


def hardware(path: str) -> HardwareConfiguration:
    return HardwareConfiguration(
        id=rid("hw", path),
        provenance=cite(path),
        machine=NotCovered(),
        name=Known("ur5e"),
        revision=Unknown(),
    )


def calibration(path: str) -> Calibration:
    return Calibration(
        id=rid("cal", path),
        provenance=cite(path),
        machine=Unknown(),
        hardware_revision=Unknown(),
        subject=Known("cam0"),
        performed=Unknown(),
        valid_from=Unknown(),
        valid_until=Unknown(),
        parameters=(),
        extrinsics=(rid("transform", path),),
    )


def firmware(path: str, version: str) -> SoftwareConfiguration:
    item = SoftwareItem(
        name=Known("gripper_fw"),
        device=NotCovered(),
        commit=NotCovered(),
        release=Known(FirmwareVersion(version)),
        build=NotCovered(),
        digest=NotCovered(),
    )
    return SoftwareConfiguration(
        id=rid("sw", path), provenance=cite(path), machine=NotCovered(), software=(item,)
    )


def build(path: str, commit: str) -> SoftwareConfiguration:
    item = SoftwareItem(
        name=Known("nav_stack"),
        device=NotCovered(),
        commit=Known(GitCommit(commit)),
        release=NotCovered(),
        build=NotCovered(),
        digest=NotCovered(),
    )
    return SoftwareConfiguration(
        id=rid("sw", path), provenance=cite(path), machine=NotCovered(), software=(item,)
    )


def row(path: str, n: int, *cells: str) -> StructuredRecord:
    return StructuredRecord(
        id=rid("row", path, str(n)),
        provenance=cite(path, Row(n)),
        table=rid("table", path),
        row=n,
        cells=tuple(Known(c) for c in cells),
    )


def bind(
    paths: list[str], records: list[Any], rows: list[StructuredRecord] | None = None
) -> Bindings:
    files = [
        LayoutFile(rid("rev", p), local_location(p.encode()), content(p)) for p in sorted(paths)
    ]
    layout = layout_of(files)
    found = bind_snapshots(records, rows or [], layout, LayoutGrouper().propose(layout))
    assert found is not None
    return found


def kinds(found: Bindings, run_id: RecordId) -> dict[SnapshotKind, set[RecordId]]:
    out: dict[SnapshotKind, set[RecordId]] = {}
    bindings: list[SnapshotBinding | InferredSnapshotBinding] = [*found.stated, *found.inferred]
    for binding in bindings:
        if binding.run == run_id:
            out.setdefault(binding.snapshot_kind, set()).add(binding.snapshot)
    return out


def codes(found: Bindings) -> list[str]:
    return sorted(f.code for f in found.findings)


PATHS = ["run_001/arm.mcap", "run_001/robot/ur5e.urdf", "run_001/cam/camchain.yaml"]


def test_hardware_and_calibration_in_the_session_bind_as_inferred() -> None:
    arm = run("run_001/arm.mcap")
    found = bind(PATHS, [arm, hardware("run_001/robot/ur5e.urdf"), calibration(PATHS[2])])
    assert kinds(found, arm.id) == {
        SnapshotKind.HARDWARE_CONFIGURATION: {rid("hw", "run_001/robot/ur5e.urdf")},
        SnapshotKind.CALIBRATION: {rid("cal", PATHS[2])},
    }
    assert not found.stated
    assert codes(found) == [NO_SOFTWARE_IDENTITY, SNAPSHOT_UNRESOLVED]
    binding = found.inferred[0]
    assert binding.evidence[0] == arm.provenance.evidence  # the run first, then the snapshot
    assert isinstance(binding.validity, Unknown)


def test_no_run_means_no_pass() -> None:
    layout = layout_of([LayoutFile(rid("rev"), local_location(b"a.urdf"), content("a.urdf"))])
    grouping = LayoutGrouper().propose(layout)
    assert bind_snapshots([hardware("a.urdf")], [], layout, grouping) is None


def test_a_firmware_version_naming_two_different_images_is_a_conflict() -> None:
    paths = ["run_002/arm.mcap", "run_002/a/fw.bin", "run_002/b/fw.bin"]
    arm = run(paths[0])
    statement = row(paths[0], 0, "gripper_firmware", "v2.3.1")
    records = [arm, firmware(paths[1], "v2.3.1"), firmware(paths[2], "v2.3.1")]
    found = bind(paths, records, [statement])
    assert not found.stated and not found.inferred
    conflicts = [f for f in found.findings if f.code == CONFLICTING_SNAPSHOTS]
    assert len(conflicts) == 1  # the declared value; its slot is then not decided by nearness
    assert conflicts[0].details["rule"] == "declared_by_run"
    assert NO_SOFTWARE_IDENTITY in codes(found)


def test_a_declared_firmware_version_is_inferred_and_settles_its_slot() -> None:
    paths = ["run_003/arm.mcap", "run_003/fw.bin", "run_003/old/fw.bin"]
    arm = run(paths[0])
    statement = row(paths[0], 0, "gripper_firmware", "v2.0.0")
    records = [arm, firmware(paths[1], "v3.0.0"), firmware(paths[2], "v2.0.0")]
    found = bind(paths, records, [statement])
    assert not found.stated  # a version names a release, which many images may carry
    (declared,) = found.inferred
    assert declared.snapshot == rid("sw", paths[2])
    assert declared.evidence[0] != arm.provenance.evidence  # it cites the naming row first
    assert "neptune.bindings.stated_differs_from_nearest" in codes(found)


def test_a_source_declaring_two_runs_states_nothing_for_either() -> None:
    paths = ["run_004/two.bag", "run_004/fw.bin"]
    first, second = run(paths[0], 1), run(paths[0], 2)
    statement = row(paths[0], 0, "fw", "v1.0.0")
    found = bind(paths, [first, second, firmware(paths[1], "v1.0.0")], [statement])
    assert not found.stated  # which run the row is about is not stated
    assert {b.run for b in found.inferred} == {first.id, second.id}


def test_paths_resolve_lexically_and_never_leave_the_root() -> None:
    assert _resolve(b"run/cfg", b"../params.yaml") == b"run/params.yaml"
    assert _resolve(b"run", b"../../etc/passwd") is None
    assert _resolve(b"run", b"/etc/passwd") is None
    assert _resolve(b"", b"params.yaml") == b"params.yaml"
    assert _resolve(b"run", b"..") is None  # the root itself is no file


def test_the_result_does_not_depend_on_input_order() -> None:
    paths = [*PATHS, "run_001/fw.bin", "run_001/old/fw.bin"]
    arm = run(PATHS[0])
    records = [
        arm,
        hardware(PATHS[1]),
        calibration(PATHS[2]),
        firmware("run_001/fw.bin", "v1"),
        firmware("run_001/old/fw.bin", "v2"),
    ]
    rows = [row(PATHS[0], 0, "fw", "v2"), row(PATHS[0], 1, "hw", "robot/ur5e.urdf")]
    first = bind(paths, records, rows)
    shuffled = random.Random(7)
    for _ in range(5):
        shuffled.shuffle(records)
        shuffled.shuffle(rows)
        again = bind(list(reversed(paths)), records, rows)
        assert again == first
    assert [b.id for b in first.inferred] == sorted(b.id for b in first.inferred)
    assert not first.stated  # a version and a relative path are inferred joins
    assert {b.snapshot for b in first.inferred if b.evidence[0] != arm.provenance.evidence} == {
        rid("sw", "run_001/old/fw.bin"),
        rid("hw", PATHS[1]),
    }


def test_inferred_bindings_round_trip_and_parse_strictly() -> None:
    arm = run(PATHS[0])
    found = bind(PATHS, [arm, hardware(PATHS[1])])
    lines = [json.loads(json.dumps(line)) for line in found.tables()[BINDING_KIND]]
    assert read_derived({BINDING_KIND: lines}) == found.inferred
    line = lines[0]
    with pytest.raises(ValueError):
        inferred_snapshot_binding_from_json({**line, "rule": "x"})
    with pytest.raises(ValueError):
        inferred_snapshot_binding_from_json({**line, "assertion_kind": "stated"})
    with pytest.raises(ValueError):
        inferred_snapshot_binding_from_json({**line, "snapshot_kind": "machine"})
    stated_state = {**line["validity"], "provenance": cite(PATHS[0]).to_json()}
    with pytest.raises(ValueError):
        inferred_snapshot_binding_from_json({**line, "validity": stated_state})
    with pytest.raises(ValueError):
        InferredSnapshotBinding(
            id=found.inferred[0].id,
            transform=found.transform.id,
            evidence=(arm.provenance.evidence, arm.provenance.evidence),
            run=arm.id,
            snapshot=rid("hw"),
            snapshot_kind=SnapshotKind.HARDWARE_CONFIGURATION,
            validity=Unknown(),
        )


def test_stated_bindings_are_canonical_records_with_evidence_ids() -> None:
    paths = ["run_005/arm.mcap", "run_005/build.yaml"]
    arm = run(paths[0])
    commit = "8f3c2a1d9e7b6c5a4f3e2d1c0b9a8f7e6d5c4b3a"
    found = bind(paths, [arm, build(paths[1], commit)], [row(paths[0], 0, "commit", commit)])
    (stated,) = found.stated
    assert stated.provenance.assertion_kind is AssertionKind.STATED
    assert isinstance(stated, SnapshotBinding)
    assert SnapshotBinding.kind == BINDING_KIND
    assert found.transform.adapter_id == "neptune.bindings"
    assert TRANSFORM in found.transform.upstream
    assert found.summary()["stated"] == 1


def test_one_row_naming_two_snapshots_binds_both() -> None:
    paths = ["run_006/arm.mcap", "run_006/params.yaml", "run_006/fw.bin"]
    arm = run(paths[0])
    statement = row(paths[0], 0, "fw.bin", "v4")  # one row, two cells, two snapshots
    found = bind(paths, [arm, hardware(paths[1]), firmware(paths[2], "v4")], [statement])
    declared = [b for b in found.inferred if b.evidence[0] != arm.provenance.evidence]
    assert [b.snapshot for b in declared] == [rid("sw", paths[2])]  # one image: one binding
    assert kinds(found, arm.id)[SnapshotKind.SOFTWARE_CONFIGURATION] == {rid("sw", paths[2])}
    assert not found.stated  # a relative path and a version are both inferred joins


def test_a_declared_path_outside_the_session_settles_its_slot() -> None:
    paths = ["shared/fw.bin", "run_007/arm.mcap", "run_007/fw.bin"]
    arm = run(paths[1])
    statement = row(paths[1], 0, "firmware", "../shared/fw.bin")
    found = bind(paths, [arm, firmware(paths[0], "v1"), firmware(paths[2], "v2")], [statement])
    assert kinds(found, arm.id)[SnapshotKind.SOFTWARE_CONFIGURATION] == {rid("sw", paths[0])}
    assert not found.stated  # relative to the recording's directory: an inferred join
    (declared,) = found.inferred
    assert declared.evidence[0] != arm.provenance.evidence  # it cites the naming row first
    assert "neptune.bindings.stated_differs_from_nearest" in codes(found)


def test_a_value_naming_several_snapshots_binds_none_of_them_by_nearness() -> None:
    paths = ["run_008/arm.mcap", "run_008/fw.bin", "run_008/sub/fw.bin"]
    arm = run(paths[0])
    statement = row(paths[0], 0, "firmware", "v5")
    records = [arm, firmware(paths[1], "v5"), firmware(paths[2], "v5")]
    found = bind(paths, records, [statement])
    assert not found.stated and not found.inferred  # the nearer one is not chosen silently
    assert codes(found).count(CONFLICTING_SNAPSHOTS) == 1
    assert NO_SOFTWARE_IDENTITY in codes(found)


def test_a_root_relative_path_names_nothing() -> None:
    """The review's repro: ``hw.yaml`` three directories up, outside the recording's session."""
    rec = "fleet/robot_a/2026-09-01/rec.mcap"
    found = bind([rec, "hw.yaml"], [run(rec), hardware("hw.yaml")], [row(rec, 1, "hw.yaml")])
    assert not found.stated and not found.inferred


def _rooted(prefix: str) -> Bindings:
    """One robot's tree under ``prefix``, with a decoy ``hw.yaml`` at the wider root. Ids and
    contents derive from the path below the prefix, so both roots hold the same records."""
    tree = [
        "session_2026-09-01T10-00-00/rec.mcap",
        "session_2026-09-01T10-00-00/params.yaml",
        "session_2026-09-01T10-00-00/config/controller.yaml",
    ]
    files = [
        LayoutFile(rid("rev", p), local_location((prefix + p).encode()), content(p)) for p in tree
    ]
    if prefix:
        files.append(
            LayoutFile(rid("rev", "hw.yaml"), local_location(b"hw.yaml"), content("hw.yaml"))
        )
    layout = layout_of(files)
    records = [run(tree[0]), hardware(tree[1]), hardware(tree[2]), hardware("hw.yaml")]
    rows = [row(tree[0], 1, "hw.yaml", "config/controller.yaml")]
    found = bind_snapshots(records, rows, layout, LayoutGrouper().propose(layout))
    assert found is not None
    return found


def _bindings(found: Bindings) -> set[tuple[str, str, str, str]]:
    stated = {(b.run, b.snapshot, str(b.snapshot_kind), "stated") for b in found.stated}
    inferred = {(b.run, b.snapshot, str(b.snapshot_kind), "inferred") for b in found.inferred}
    return stated | inferred


def test_the_result_does_not_depend_on_the_ingest_root() -> None:
    narrow, wide = _rooted(""), _rooted("fleet/robot_a/")
    assert _bindings(narrow) == _bindings(wide)
    assert codes(narrow) == codes(wide)
    assert not wide.stated  # the decoy at the wide root is never named
    assert {b.snapshot for b in wide.inferred} == {
        rid("hw", "session_2026-09-01T10-00-00/config/controller.yaml"),  # by relative path
        rid("hw", "session_2026-09-01T10-00-00/params.yaml"),  # nearest of its own slot
    }


def _one_folder(r: int) -> tuple[list[str], list[Any]]:
    """The review's repro: one session folder holding ``r`` recordings and their sidecars."""
    d = "session_2026-09-01T10-00-00"
    paths = [p for i in range(r) for p in (f"{d}/ep_{i:05d}.mcap", f"{d}/ep_{i:05d}_hw.yaml")]
    records = [
        x
        for i in range(r)
        for x in (run(f"{d}/ep_{i:05d}.mcap"), hardware(f"{d}/ep_{i:05d}_hw.yaml"))
    ]
    return paths, records


def test_each_run_binds_its_own_sidecar_never_another_recordings() -> None:
    r = 800
    found = bind(*_one_folder(r))
    assert len(found.inferred) == r  # R bindings, not R squared
    d = "session_2026-09-01T10-00-00"
    own = {run(f"{d}/ep_{i:05d}.mcap").id: rid("hw", f"{d}/ep_{i:05d}_hw.yaml") for i in range(r)}
    assert {b.run: b.snapshot for b in found.inferred} == own
    assert CONFLICTING_SNAPSHOTS not in codes(found)
    assert SHARED_SNAPSHOT not in codes(found)


def test_cost_and_output_are_linear_in_runs() -> None:
    """One robot directory per run (the review's second repro): bindings and findings grow with
    R, and R=2000 binds in seconds (it took 42 s when each run rescanned its session)."""
    d = "session_2026-09-01T10-00-00"
    sizes = (500, 2000)
    counts = []
    for r in sizes:
        paths = [
            p
            for i in range(r)
            for p in (f"{d}/robot_{i:04d}/rec.mcap", f"{d}/robot_{i:04d}/hw.yaml")
        ]
        records = [
            x
            for i in range(r)
            for x in (run(f"{d}/robot_{i:04d}/rec.mcap"), hardware(f"{d}/robot_{i:04d}/hw.yaml"))
        ]
        files = [
            LayoutFile(rid("rev", p), local_location(p.encode()), content(p)) for p in sorted(paths)
        ]
        layout = layout_of(files)
        grouping = LayoutGrouper().propose(layout)
        start = time.perf_counter()
        found = bind_snapshots(records, [], layout, grouping)
        elapsed = time.perf_counter() - start
        assert found is not None
        assert len(found.inferred) == r
        counts.append(len(found.inferred) + len(found.findings))
        if r == sizes[-1]:
            assert elapsed < 20, elapsed  # generous for a loaded host; quadratic took 42 s
    assert counts[1] == counts[0] * sizes[1] // sizes[0]


def test_a_file_as_near_to_several_recordings_is_bound_to_none() -> None:
    paths = ["run_010/left.mcap", "run_010/right.mcap", "run_010/params.yaml"]
    left, right = run(paths[0]), run(paths[1])
    found = bind(paths, [left, right, hardware(paths[2])])
    assert not found.stated and not found.inferred  # never bound to every run beside it
    (shared,) = [f for f in found.findings if f.code == SHARED_SNAPSHOT]
    assert shared.details["recordings"] == 2
    assert set(shared.records) == {rid("hw", paths[2]), left.id, right.id}
    assert codes(found).count(SNAPSHOT_UNRESOLVED) == 6  # each run: hardware, too, unresolved


def test_a_run_assembly_makes_one_unit_of_its_files() -> None:
    """A rosbag2 bag: the metadata's run and its storage's run are one unit by the assembly, so
    what one member declares is its unit-mate's too; a context member is the unit's own even
    outside the session; the session's parameters are still above another recording."""
    paths = [
        "run_011/bag/metadata.yaml",
        "run_011/bag/bag_0.mcap",
        "run_011/params.yaml",
        "elsewhere/cam.yaml",
        "run_011/other.mcap",
    ]
    described, stored, other = run(paths[0]), run(paths[1]), run(paths[4])
    records: list[Any] = [
        described,
        stored,
        other,
        hardware(paths[2]),
        calibration(paths[3]),
        firmware(paths[1], "v1"),  # the storage file declares its software itself
    ]
    alone = bind(paths, records)
    assert SHARED_SNAPSHOT in codes(alone)  # three recordings below run_011
    assert kinds(alone, described.id) == {}
    members = [
        RunMember(rid("rev", paths[0]), MemberRole.DESCRIPTION, cite(paths[0]).evidence),
        RunMember(rid("rev", paths[1]), MemberRole.RECORDING, cite(paths[0], Row(0)).evidence),
        RunMember(rid("rev", paths[3]), MemberRole.CONTEXT, cite(paths[0], Row(1)).evidence),
    ]
    assembly = RunAssembly(
        id=rid("assembly"),
        provenance=cite(paths[0]),
        run=described.id,
        rule="rosbag2.metadata",
        members=tuple(sorted(members, key=lambda m: m.revision)),
        validity=NotApplicable(),
    )
    found = bind(paths, [*records, assembly])
    for member in (described, stored):
        assert kinds(found, member.id) == {
            SnapshotKind.CALIBRATION: {rid("cal", paths[3])},  # the assembly places it
            SnapshotKind.SOFTWARE_CONFIGURATION: {rid("sw", paths[1])},  # the unit declares it
        }
    assert [b.run for b in found.stated] == [stored.id]  # its own source: same_source, stated
    assert kinds(found, other.id) == {}
    assert SHARED_SNAPSHOT in codes(found)  # params.yaml: above the bag and other.mcap


def test_a_sidecar_whose_recording_is_absent_is_never_a_shorter_stems() -> None:
    paths = ["run_012/run.mcap", "run_012/run_2.mcap", "run_012/run_3_params.yaml"]
    first, second = run(paths[0]), run(paths[1])
    found = bind(paths, [first, second, hardware(paths[2])])
    assert not found.inferred  # ``run`` is a stem ``run_2`` extends too: no one's sidecar alone
    assert SHARED_SNAPSHOT in codes(found)


def test_a_full_commit_names_a_build_outside_the_run_and_is_stated() -> None:
    """A fleet-wide build file above two robots is neither's own, but a run that states its
    exact commit names it: that is stated, wherever the file is."""
    commit = "0123456789abcdef0123456789abcdef01234567"
    paths = ["run_013/a/rec.mcap", "run_013/b/rec.mcap", "run_013/build.yaml"]
    a, b = run(paths[0]), run(paths[1])
    statement = row(paths[0], 0, "commit", commit)
    found = bind(paths, [a, b, build(paths[2], commit)], [statement])
    (stated,) = found.stated
    assert (stated.run, stated.snapshot) == (a.id, rid("sw", paths[2]))
    assert kinds(found, b.id) == {}
    (shared,) = [f for f in found.findings if f.code == SHARED_SNAPSHOT]
    assert "no run binds it by nearness" in shared.message

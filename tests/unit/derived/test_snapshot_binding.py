"""The snapshot binding pass over records and a grouping, without a job (ADR 0064).

Hardware and calibration snapshots have no adapter in every tree yet, so their binding is pinned
here with records built by hand; the stated rule's limits (one run per source, one value naming
several snapshots), order independence and the derived record's strict JSON are pinned too.
"""

import hashlib
import json
import random
from typing import Any

import pytest

from neptune.derived.bindings import (
    BINDING_KIND,
    CONFLICTING_SNAPSHOTS,
    NO_SOFTWARE_IDENTITY,
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
from neptune.model.alignment import SnapshotBinding, SnapshotKind
from neptune.model.ids import ContentId, RecordId
from neptune.model.knowledge import AssertionKind, Known, NotCovered, Unknown
from neptune.model.machine import (
    Calibration,
    HardwareConfiguration,
    SoftwareConfiguration,
    SoftwareItem,
)
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, Row
from neptune.model.run import Run
from neptune.model.source import local_location
from neptune.model.versions import FirmwareVersion
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


def test_a_stated_firmware_settles_its_slot_and_a_different_nearest_is_reported() -> None:
    paths = ["run_003/arm.mcap", "run_003/fw.bin", "run_003/old/fw.bin"]
    arm = run(paths[0])
    statement = row(paths[0], 0, "gripper_firmware", "v2.0.0")
    records = [arm, firmware(paths[1], "v3.0.0"), firmware(paths[2], "v2.0.0")]
    found = bind(paths, records, [statement])
    (stated,) = found.stated
    assert stated.snapshot == rid("sw", paths[2])
    assert stated.provenance.evidence == statement.provenance.evidence
    assert "neptune.bindings.stated_differs_from_nearest" in codes(found)
    assert not [b for b in found.inferred if b.snapshot_kind is SnapshotKind.SOFTWARE_CONFIGURATION]


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
    assert {b.snapshot for b in first.stated} == {
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
    paths = ["run_005/arm.mcap", "run_005/fw.bin"]
    arm = run(paths[0])
    found = bind(paths, [arm, firmware(paths[1], "v9")], [row(paths[0], 0, "fw", "v9")])
    (stated,) = found.stated
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
    assert {b.snapshot for b in found.stated} | {
        b.snapshot for b in found.inferred if b.evidence[0] != arm.provenance.evidence
    } == {rid("sw", paths[2])}
    assert kinds(found, arm.id)[SnapshotKind.SOFTWARE_CONFIGURATION] == {rid("sw", paths[2])}
    assert len(found.stated) == 1  # the path and the version name one image: one binding


def test_a_stated_snapshot_outside_the_session_settles_its_slot() -> None:
    paths = ["shared/fw.bin", "run_007/arm.mcap", "run_007/fw.bin"]
    arm = run(paths[1])
    statement = row(paths[1], 0, "firmware", "shared/fw.bin")
    found = bind(paths, [arm, firmware(paths[0], "v1"), firmware(paths[2], "v2")], [statement])
    assert kinds(found, arm.id)[SnapshotKind.SOFTWARE_CONFIGURATION] == {rid("sw", paths[0])}
    assert not found.inferred
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

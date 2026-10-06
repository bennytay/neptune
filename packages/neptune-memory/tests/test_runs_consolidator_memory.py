"""Run threads (ADR 0009) on the issue's archetypes, with the record shapes the compiler writes.

- Warehouse: six AMRs at two sites over two daily uploads, each log on its own boot clock, one
  mapped to civil time by a stated clock mapping; a manifest declares each run's site and task.
- A rosbag2 recording ingested in two uploads: each upload's assembly names the bag's run, so the
  second part ``continues`` the first; on clocks that cannot be compared, only candidates.
- A run folder holding two robots' logs: the folder's machine is ambiguous, nothing continues.
- A run whose manifest names a site the site register does not declare.
- A run whose records disagree, or leave its machine ``Ambiguous`` or unstated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest

from memory_identity_records import STATED, Record, at, ledger
from memory_run_records import (
    NS,
    assembly,
    declaration,
    domain,
    mapping,
    revision,
    run,
    site,
)
from neptune.model.alignment import MemberRole
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import Ambiguous, Known, NotCovered, Unknown
from neptune.model.time import Epoch, Timescale
from neptune_memory.consolidate.base import Consolidation, run_consolidator
from neptune_memory.consolidate.runs import RunConsolidator, involvement
from neptune_memory.schema.claim import Claim, LedgerRecordRef
from neptune_memory.schema.interval import CivilClock, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

TX = ledger_tx(7)
RUN, MACHINE, SITE, TASK = NodeType.RUN, NodeType.MACHINE, NodeType.SITE, NodeType.TASK
REC, DESC = MemberRole.RECORDING, MemberRole.DESCRIPTION
NORTH, SOUTH = LogicalId("site", "WH-NORTH"), LogicalId("site", "WH-SOUTH")
EPOCH_NS: Final = 1_790_000_000 * 10**9  # an instant in 2026, POSIX nanoseconds
CIVIL: Final = CivilClock(Timescale.POSIX, Epoch.UNIX, NS)  # what a civil log_time is placed on


def consolidate(packages: Mapping[str, Sequence[Record]], config: object = None) -> Consolidation:
    return run_consolidator(
        RunConsolidator(),
        ledger(packages),
        (),
        dict(config or {}),  # type: ignore[call-overload]
        recorded_at=TX,
        registry=CORE_PREDICATES,
    )


def amr(k: int) -> LogicalId:
    return LogicalId("asset-tag", f"AMR-0{k}")


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


def known(*nodes: NodeRef) -> Known[tuple[NodeRef, ...]]:
    return Known(tuple(nodes))


def readings(value: object) -> set[tuple[NodeRef, ...]]:
    assert isinstance(value, Ambiguous)
    return {c.value for c in value.candidates}


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


# --- warehouse: six AMRs, two sites, two uploads --------------------------------------------------


def _warehouse() -> tuple[dict[str, list[Record]], dict[int, RecordId], RecordId]:
    civil_record, civil_id = domain("site ntp", civil=True)
    packages: dict[str, list[Record]] = {
        "registers": [site("site register north", NORTH), site("site register south", SOUTH)],
        "ntp": [civil_record],
    }
    runs: dict[int, RecordId] = {}
    boots: dict[int, RecordId] = {}
    for k in range(1, 7):
        day = "day-1-north" if k <= 3 else "day-1-south"
        boot_record, boot = domain(f"amr-0{k} boot", civil=False)
        log = f"amr-0{k}/log.mcap"
        record, rid = run(log, first=at(1_000, boot), last=at(60_000, boot), machine=amr(k))
        held, _ = assembly(log, rid, [(log, REC)], rule="recording")
        packages.setdefault(day, []).extend([boot_record, revision(log)[0], record, held])
        packages.setdefault(f"{day}-manifest", []).append(
            declaration(
                f"amr-0{k}",
                rid,
                site=NORTH if k <= 3 else SOUTH,
                task=LogicalId("task", "pick-wave-1"),
            )
        )
        runs[k], boots[k] = rid, boot
    # AMR-01's boot clock is mapped to civil time by the site's sync log: boot 0 is EPOCH_NS.
    packages["day-1-north"].append(mapping("amr-01 sync", boots[1], civil_id, anchor=(0, EPOCH_NS)))
    return packages, runs, boots[1]


def test_warehouse_runs_are_known_per_machine_site_and_task() -> None:
    packages, runs, _ = _warehouse()
    result = consolidate(packages)
    assert codes(result) == []
    for k, rid in runs.items():
        subject = node(RUN, rid)
        current = [c for c in result.claims if c.subject == subject]
        assert involvement(current, subject, "recorded_by") == known(node(MACHINE, amr(k)))
        assert involvement(current, subject, "at_site") == known(
            node(SITE, NORTH if k <= 3 else SOUTH)
        )
        assert involvement(current, subject, "executes_task") == known(
            node(TASK, LogicalId("task", "pick-wave-1"))
        )
        assert involvement(current, subject, "continues") == Unknown()
        (machine,) = {c.assertion_kind for c in of(result, "recorded_by", subject)}
        assert machine == "observed"  # the log's own header
        (stated,) = {c.assertion_kind for c in of(result, "at_site", subject)}
        assert stated == "stated"  # the manifest
        (member,) = {c.object for c in of(result, "has_member", subject)}
        assert isinstance(member, LedgerRecordRef)
        assert member.record_id == revision(f"amr-0{k}/log.mcap")[1]
    assert {c.subject for c in of(result, "recorded_by")} == {node(RUN, r) for r in runs.values()}


def test_run_intervals_are_on_the_primary_clock_and_civil_only_where_mapped() -> None:
    packages, runs, boot = _warehouse()
    result = consolidate(packages)
    amr_01, amr_02 = node(RUN, runs[1]), node(RUN, runs[2])
    clocks = {c.valid_from.domain_id for c in of(result, "recorded_by", amr_01)}
    assert clocks == {boot, CIVIL.domain_id}
    (civil,) = [c for c in of(result, "recorded_by", amr_01) if c.valid_from.domain_id != boot]
    # [first, last] inclusive on the boot clock, projected by the stated map: last + 1 tick.
    assert (civil.valid_from, civil.valid_to) == (
        CIVIL.at(EPOCH_NS + 1_000),
        CIVIL.at(EPOCH_NS + 60_001),
    )
    assert len(civil.provenance.records) > len(
        next(c for c in of(result, "recorded_by", amr_01) if c is not civil).provenance.records
    )  # the projection cites the mapping and the civil clock's record
    (only,) = {c.valid_from.domain_id for c in of(result, "recorded_by", amr_02)}
    assert only != CIVIL.domain_id  # no mapping: its boot clock only, never guessed


# --- a recording ingested in two uploads ---------------------------------------------------------


def _split(*, comparable: bool) -> tuple[dict[str, list[Record]], RecordId, RecordId, RecordId]:
    clock_0, c0 = domain("bag_0 log_time", civil=comparable)
    clock_1, c1 = domain("bag_1 log_time", civil=comparable)
    meta, meta_id = run("amr-03/bag/metadata.yaml")  # rosbag2 metadata: no first, no last here
    bag_0, r0 = run(
        "amr-03/bag/bag_0.mcap", first=at(EPOCH_NS, c0), last=at(EPOCH_NS + 99, c0), machine=amr(3)
    )
    bag_1, r1 = run(
        "amr-03/bag/bag_1.mcap",
        first=at(EPOCH_NS + 100, c1),
        last=at(EPOCH_NS + 199, c1),
        machine=amr(3),
    )

    def upload(part: str, record: Record, clock: Record) -> list[Record]:
        # Both uploads carry the bag's metadata, so its Run and its assembly's id are the same;
        # each assembly holds the parts its upload saw (root ADR 0066 §1-2).
        held, _ = assembly(
            "amr-03/bag/metadata.yaml#relative_file_paths",
            meta_id,
            [("amr-03/bag/metadata.yaml", DESC), (part, REC)],
        )
        files = [revision("amr-03/bag/metadata.yaml")[0], revision(part)[0]]
        return [clock, meta, record, held, *files]

    packages = {
        "upload-1": upload("amr-03/bag/bag_0.mcap", bag_0, clock_0),
        "upload-2": upload("amr-03/bag/bag_1.mcap", bag_1, clock_1),
    }
    return packages, meta_id, r0, r1


def test_a_recording_in_two_uploads_continues_across_packages() -> None:
    packages, meta, r0, r1 = _split(comparable=True)
    result = consolidate(packages)
    assert codes(result) == []
    (link,) = of(result, "continues")
    assert (link.subject, link.object) == (node(RUN, r1), node(RUN, r0))
    assert link.assertion_kind == "observed"
    assert {r0, r1, meta} <= set(link.provenance.records) | {meta}
    assert of(result, "continues_candidate") == []
    whole = node(RUN, meta)
    members = {c.object for c in of(result, "has_member", whole)}
    assert members == {
        LedgerRecordRef(revision(f)[1])
        for f in ("amr-03/bag/metadata.yaml", "amr-03/bag/bag_0.mcap", "amr-03/bag/bag_1.mcap")
    }
    assert {c.assertion_kind for c in of(result, "has_member", whole)} == {"stated"}
    # The bag's run states no instants: it holds over its parts' span on their shared clock.
    (span,) = {(c.valid_from, c.valid_to) for c in of(result, "has_member", whole)}
    assert span == (CIVIL.at(EPOCH_NS), CIVIL.at(EPOCH_NS + 200))
    # Its machine is its parts': both declare AMR-03.
    assert involvement(result.claims, whole, "recorded_by") == known(node(MACHINE, amr(3)))


def test_parts_whose_clocks_cannot_be_compared_are_only_candidates() -> None:
    packages, meta, r0, r1 = _split(comparable=False)
    result = consolidate(packages)
    assert of(result, "continues") == []
    pairs = {(c.subject, c.object) for c in of(result, "continues_candidate")}
    assert pairs == {(node(RUN, r0), node(RUN, r1)), (node(RUN, r1), node(RUN, r0))}
    # It may continue the other part, or nothing: the two readings.
    assert readings(involvement(result.claims, node(RUN, r1), "continues")) == {
        (),
        (node(RUN, r0),),
    }
    # The bag's own run has no instant and its parts span no one clock: no claim, a finding.
    assert "runs.untimed_run" in codes(result)
    assert involvement(result.claims, node(RUN, meta), "recorded_by") == NotCovered()


# --- a run folder with two robots' logs -----------------------------------------------------------


def test_a_folder_with_two_robots_logs_is_ambiguous_and_nothing_continues() -> None:
    clock_a, ca = domain("amr-01 log_time", civil=True)
    clock_b, cb = domain("amr-02 log_time", civil=True)
    folder, folder_id = run(
        "manifest.yaml#/runs/0", logical_id=LogicalId("manifest", "shift-3"), kind=STATED
    )
    log_a, ra = run(
        "shift-3/amr-01.mcap", first=at(EPOCH_NS, ca), last=at(EPOCH_NS + 500, ca), machine=amr(1)
    )
    log_b, rb = run(
        "shift-3/amr-02.mcap",
        first=at(EPOCH_NS + 10, cb),
        last=at(EPOCH_NS + 400, cb),
        machine=amr(2),
    )
    held, _ = assembly(
        "manifest.yaml#/runs/0/paths",
        folder_id,
        [("shift-3/amr-01.mcap", REC), ("shift-3/amr-02.mcap", REC)],
        rule="manifest.run",
    )
    files = [revision("shift-3/amr-01.mcap")[0], revision("shift-3/amr-02.mcap")[0]]
    result = consolidate({"shift-3": [clock_a, clock_b, folder, log_a, log_b, held, *files]})
    shift = node(RUN, LogicalId("manifest", "shift-3"))
    reading = involvement(result.claims, shift, "recorded_by")
    assert readings(reading) == {(node(MACHINE, amr(1)),), (node(MACHINE, amr(2)),)}
    assert of(result, "recorded_by", shift) == []
    assert codes(result) == ["runs.parts_differ"]
    assert of(result, "continues") == [] and of(result, "continues_candidate") == []
    for rid, k in ((ra, 1), (rb, 2)):
        assert involvement(result.claims, node(RUN, rid), "recorded_by") == known(
            node(MACHINE, amr(k))
        )
    (span,) = {(c.valid_from, c.valid_to) for c in of(result, "has_member", shift)}
    assert span == (CIVIL.at(EPOCH_NS), CIVIL.at(EPOCH_NS + 501))


# --- declared roles -----------------------------------------------------------------------------


def _one_run(
    *declarations: Record, machine: object = None, registers: Sequence[Record] = ()
) -> tuple[Consolidation, NodeRef]:
    record, rid = run("arm-cell/ur10e.bag", first=at(0), last=at(10), machine=machine)  # type: ignore[arg-type]
    packages: dict[str, list[Record]] = {"log": [record], "manifest": list(declarations)}
    if registers:
        packages["registers"] = list(registers)
    return consolidate(packages), node(RUN, rid)


def _rid() -> RecordId:
    return run("arm-cell/ur10e.bag", first=at(0), last=at(10))[1]


def test_a_manifest_naming_a_site_absent_from_the_register_is_stated_and_flagged() -> None:
    east = LogicalId("site", "WH-EAST")
    result, subject = _one_run(
        declaration("cell", _rid(), site=east),
        registers=[site("site register", NORTH, SOUTH)],
    )
    assert involvement(result.claims, subject, "at_site") == known(node(SITE, east))
    (finding,) = result.findings
    assert finding.code == "runs.site_unregistered"
    assert finding.details["site"] == "site:WH-EAST"


def test_without_a_site_register_an_undeclared_site_is_not_flagged() -> None:
    result, subject = _one_run(declaration("cell", _rid(), site=LogicalId("site", "WH-EAST")))
    assert codes(result) == []
    assert isinstance(involvement(result.claims, subject, "at_site"), Known)


def test_a_record_and_a_manifest_that_disagree_give_candidates_not_a_winner() -> None:
    arm_5, arm_6 = LogicalId("asset-tag", "ARM-05"), LogicalId("asset-tag", "ARM-06")
    result, subject = _one_run(declaration("cell", _rid(), machine=arm_6), machine=arm_5)
    assert of(result, "recorded_by") == []
    reading = involvement(result.claims, subject, "recorded_by")
    assert readings(reading) == {(node(MACHINE, arm_5),), (node(MACHINE, arm_6),)}
    assert codes(result) == ["runs.declarations_disagree"]
    kinds = {c.object: c.assertion_kind for c in of(result, "recorded_by_candidate")}
    assert kinds == {node(MACHINE, arm_5): "observed", node(MACHINE, arm_6): "stated"}


def test_an_ambiguous_field_gives_one_candidate_per_reading_each_citing_its_own_place() -> None:
    a, b = LogicalId("serial", "QX-11"), LogicalId("serial", "QX-12")
    result, subject = _one_run(machine=[a, b])
    candidates = of(result, "recorded_by_candidate", subject)
    assert {c.object for c in candidates} == {node(MACHINE, a), node(MACHINE, b)}
    assert len({c.provenance.evidence for c in candidates}) == 2
    assert codes(result) == []


def test_a_run_attempting_two_tasks_holds_both_and_nothing_disagrees() -> None:
    pick, place = LogicalId("task", "pick"), LogicalId("task", "place")
    result, subject = _one_run(
        declaration("cell pick", _rid(), task=pick),
        declaration("cell place", _rid(), task=place),
    )
    assert codes(result) == [] and of(result, "executes_task_candidate") == []
    assert involvement(result.claims, subject, "executes_task") == known(
        node(TASK, pick), node(TASK, place)
    )


def test_agreeing_grounds_are_known_once_per_ground() -> None:
    arm = LogicalId("asset-tag", "ARM-05")
    result, subject = _one_run(declaration("cell", _rid(), machine=arm), machine=arm)
    claims = of(result, "recorded_by", subject)
    assert {c.object for c in claims} == {node(MACHINE, arm)}
    assert {c.assertion_kind for c in claims} == {"observed", "stated"}


def test_an_ambiguous_declaration_consistent_with_a_known_one_stays_known() -> None:
    arm, other = LogicalId("asset-tag", "ARM-05"), LogicalId("asset-tag", "ARM-07")
    result, subject = _one_run(declaration("cell", _rid(), machine=[arm, other]), machine=arm)
    assert involvement(result.claims, subject, "recorded_by") == known(node(MACHINE, arm))


def test_nothing_stated_is_unknown_and_a_run_never_named_is_not_covered() -> None:
    result, subject = _one_run()
    assert involvement(result.claims, subject, "recorded_by") == Unknown()
    assert involvement(result.claims, subject, "at_site") == Unknown()
    assert involvement(result.claims, NodeRef(RUN, "record:absent"), "recorded_by") == NotCovered()
    with pytest.raises(ValueError, match="no candidate form"):
        involvement(result.claims, subject, "has_member")


def test_runs_with_one_declared_id_in_two_packages_are_one_node_with_both_intervals() -> None:
    night = LogicalId("manifest", "night-42")
    part_1, r1 = run("quadruped/night-42-a.bag", first=at(0), last=at(9), logical_id=night)
    part_2, r2 = run("quadruped/night-42-b.bag", first=at(10), last=at(19), logical_id=night)
    result = consolidate({"upload-1": [part_1], "upload-2": [part_2]})
    evidenced = of(result, "evidenced_by", node(RUN, night))
    assert {c.object for c in evidenced} == {LedgerRecordRef(r1), LedgerRecordRef(r2)}
    assert {(c.valid_from, c.valid_to) for c in evidenced} == {(at(0), at(10)), (at(10), at(20))}
    assert of(result, "continues") == []  # one thread, one node: nothing to link (ADR 0003 §1.1)

"""Episodes (ADR 0012) on the issue's archetypes, with the record shapes the compiler writes.

- Warehouse missions: AMR-07's missions are runs with declared job ids (``wms.job:J-…``) and a
  manifest task each; an intervention mid-mission says "mission completed", a bumper stop cuts
  another short, a third names its mission but states no time, and a fourth mission has no task.
- A manipulator cell: one pick cycle on the arm's boot clock, mapped to civil time, with a human
  intervention mid-task stated on the console's civil clock.
- A run with no task evidence at all: no episode, read back as ``Unknown``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from memory_episode_records import incident, intervention
from memory_identity_records import Record, at, ledger
from memory_run_records import NS, declaration, domain, mapping, run
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import Ambiguous, Known, NotCovered, Unknown
from neptune.model.time import Epoch, Timescale, Timestamp
from neptune_memory.consolidate.base import Consolidation, rebuild
from neptune_memory.consolidate.episodes import (
    EpisodeConsolidator,
    boundary_of,
    episodes_of,
    outcome_of,
)
from neptune_memory.consolidate.runs import RunConsolidator
from neptune_memory.schema.claim import Claim, LedgerRecordRef, TypedLiteral, ValueType
from neptune_memory.schema.interval import CivilClock, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

TX = ledger_tx(9)
AMR07, AMR08 = LogicalId("asset-tag", "AMR-07"), LogicalId("asset-tag", "AMR-08")
TOTE, PALLET = LogicalId("task", "tote-transport"), LogicalId("task", "empty-pallet-return")
CIVIL: Final = CivilClock(Timescale.POSIX, Epoch.UNIX, NS)
EPOCH_NS: Final = 1_790_000_000 * 10**9


def build(
    packages: Mapping[str, Sequence[Record]], config: object = None
) -> tuple[Consolidation, Consolidation]:
    runs, episodes = rebuild(
        ledger(packages),
        [(RunConsolidator(), {}), (EpisodeConsolidator(), dict(config or {}))],  # type: ignore[call-overload]
        recorded_at=TX,
    )
    return runs, episodes


def every(*results: Consolidation) -> list[Claim]:
    return [c for r in results for c in r.claims]


def job(value: str) -> LogicalId:
    return LogicalId("wms.job", value)


def run_node(value: LogicalId) -> NodeRef:
    return NodeRef(NodeType.RUN, f"{value.namespace}:{value.value}")


def only_episode(claims: Sequence[Claim], run: NodeRef) -> NodeRef:
    found = episodes_of(claims, run)
    assert isinstance(found, Known) and len(found.value) == 1, found
    return found.value[0]


def about(result: Consolidation, episode: NodeRef, predicate: str) -> list[Claim]:
    return [c for c in result.claims if c.subject == episode and c.predicate == predicate]


def _instant(ticks: int, clock: RecordId) -> TypedLiteral:
    return TypedLiteral(ValueType.INSTANT, Timestamp(ticks, clock))


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


# --- warehouse missions with declared job ids -----------------------------------------------------


def _warehouse() -> tuple[dict[str, list[Record]], RecordId, dict[str, RecordId]]:
    boot_record, boot = domain("amr-07 boot", civil=False)
    boot8_record, boot8 = domain("amr-08 boot", civil=False)
    records: dict[str, RecordId] = {}
    missions: list[Record] = [boot_record, boot8_record]
    manifest: list[Record] = []
    for value, first, last, task in (
        ("J-1042", 1_000, 9_000, TOTE),
        ("J-1043", 10_000, 19_000, PALLET),
        ("J-1044", 20_000, 29_000, TOTE),
        ("J-1045", 30_000, 39_000, None),  # the mission log names no task for it
    ):
        record, rid = run(
            f"missions/{value}.mcap",
            first=at(first, boot),
            last=at(last, boot),
            machine=AMR07,
            logical_id=job(value),
        )
        missions.append(record)
        records[value] = rid
        if task is not None:
            manifest.append(declaration(value, job(value), task=task))
    # Another robot on the same shift: AMR-07's tickets never reach its mission.
    other, records["J-2001"] = run(
        "missions/J-2001.mcap",
        first=at(1_000, boot8),
        last=at(9_000, boot8),
        machine=AMR08,
        logical_id=job("J-2001"),
    )
    missions.append(other)
    manifest.append(declaration("J-2001", job("J-2001"), task=TOTE))
    assist, records["INT-1187"] = intervention(
        "INT-1187",
        machines=[AMR07],
        start=at(3_000, boot),
        end=at(3_500, boot),
        outcome="mission completed",
    )
    bumper, records["INC-0007"] = incident(
        "INC-0007", machines=[AMR07], occurred=at(14_000, boot), description="bumper stop"
    )
    named, records["INT-1190"] = intervention("INT-1190", related=[job("J-1044")])
    tickets = [assist, bumper, named]
    return {"missions": missions, "manifest": manifest, "tickets": tickets}, boot, records


def test_each_mission_with_a_task_is_one_episode_of_its_job() -> None:
    packages, boot, _ = _warehouse()
    runs, episodes = build(packages)
    claims = every(runs, episodes)
    for value, task in (("J-1042", TOTE), ("J-1043", PALLET), ("J-1044", TOTE)):
        mission = run_node(job(value))
        episode = only_episode(claims, mission)
        (part_of,) = about(episodes, episode, "episode_of")
        assert part_of.object == mission and part_of.assertion_kind == "stated"
        (performs,) = about(episodes, episode, "executes_task")
        assert performs.object == NodeRef(NodeType.TASK, f"task:{task.value}")
        # Boundaries carry the clock and the run record that states them.
        assert boundary_of(claims, episode, "start", boot) == Known(part_of.valid_from)
        assert part_of.valid_from.domain_id == boot
    # The other robot's mission has its own episode; nothing of AMR-07's reaches it.
    other = only_episode(claims, run_node(job("J-2001")))
    assert [c for c in episodes.claims if c.subject == other and "intervened" in c.predicate] == []


def test_a_mission_with_no_task_has_no_episode() -> None:
    packages, _, _ = _warehouse()
    runs, episodes = build(packages)
    claims = every(runs, episodes)
    assert episodes_of(claims, run_node(job("J-1045"))) == Unknown()
    assert episodes_of(claims, run_node(job("J-9999"))) == NotCovered()


def test_no_outcome_is_inferred_from_an_intervention_that_says_completed() -> None:
    packages, _, records = _warehouse()
    runs, episodes = build(packages)
    claims = every(runs, episodes)
    episode = only_episode(claims, run_node(job("J-1042")))
    (assisted,) = about(episodes, episode, "intervened")
    assert assisted.object == LedgerRecordRef(records["INT-1187"])
    assert assisted.assertion_kind == "observed"  # its machine and times place it in the mission
    assert records["INT-1187"] in assisted.provenance.records
    assert outcome_of(claims, episode) == Unknown()
    assert [c for c in episodes.claims if c.predicate == "outcome"] == []


def test_a_stop_inside_a_mission_leaves_its_end_ambiguous() -> None:
    packages, boot, records = _warehouse()
    runs, episodes = build(packages)
    claims = every(runs, episodes)
    episode = only_episode(claims, run_node(job("J-1043")))
    assert about(episodes, episode, "ends_at") == []
    ends = about(episodes, episode, "ends_at_candidate")
    assert {c.object for c in ends} == {_instant(14_000, boot), _instant(19_001, boot)}
    stop = next(c for c in ends if c.object == _instant(14_000, boot))
    assert records["INC-0007"] in stop.provenance.records
    end = boundary_of(claims, episode, "end", boot)
    assert isinstance(end, Ambiguous)
    assert {c.value.ticks for c in end.candidates} == {14_000, 19_001}
    # The stop is evidence about the episode, never part of what identifies it.
    without = {k: [r for r in v if r is not packages["tickets"][1]] for k, v in packages.items()}
    runs_b, episodes_b = build(without)
    assert episode == only_episode(every(runs_b, episodes_b), run_node(job("J-1043")))


def test_an_intervention_naming_its_mission_is_stated_even_untimed() -> None:
    packages, _, records = _warehouse()
    runs, episodes = build(packages)
    claims = every(runs, episodes)
    episode = only_episode(claims, run_node(job("J-1044")))
    (named,) = about(episodes, episode, "intervened")
    assert named.object == LedgerRecordRef(records["INT-1190"])
    assert named.assertion_kind == "stated"
    assert codes(episodes) == ["episodes.event_unplaced"]


# --- manipulator cell: a human intervention mid-task ---------------------------------------------


ARM: Final = LogicalId("asset-tag", "UR10-CELL3")
BIN_PICK: Final = LogicalId("task", "bin-pick-7")


def _cell(*, with_intervention: bool = True) -> tuple[dict[str, list[Record]], RecordId, RecordId]:
    boot_record, boot = domain("cell-3 arm boot", civil=False)
    console_record, console = domain("cell console", civil=True)
    cycle, _ = run(
        "cell-3/cycle-0412.bag",
        first=at(5_000_000_000, boot),
        last=at(65_000_000_000, boot),
        machine=ARM,
        logical_id=LogicalId("cell-3.cycle", "0412"),
    )
    records = [
        boot_record,
        console_record,
        cycle,
        mapping("cell-3 ptp", boot, console, anchor=(0, EPOCH_NS)),
        declaration("cycle 0412", LogicalId("cell-3.cycle", "0412"), task=BIN_PICK),
    ]
    held = None
    if with_intervention:
        # The operator reached in to free a jammed part 20 s into the cycle (console clock).
        stop, held = intervention(
            "OP-77",
            machines=[ARM],
            start=Timestamp(EPOCH_NS + 25_000_000_000, console),
            end=Timestamp(EPOCH_NS + 31_000_000_000, console),
        )
        records.append(stop)
    return {"cell-3": records}, boot, held or boot


def test_a_human_intervention_mid_task_stays_inside_one_episode() -> None:
    packages, boot, held = _cell()
    runs, episodes = build(packages)
    claims = every(runs, episodes)
    episode = only_episode(claims, run_node(LogicalId("cell-3.cycle", "0412")))
    intervened = about(episodes, episode, "intervened")
    # One claim per clock the episode is on: the arm's boot clock and civil time.
    assert {c.valid_from.domain_id for c in intervened} == {boot, CIVIL.domain_id}
    assert {c.object for c in intervened} == {LedgerRecordRef(held)}
    assert all(c.assertion_kind == "observed" for c in intervened)
    # Not cut: one start and one end on each clock.
    assert boundary_of(claims, episode, "start", boot) == Known(at(5_000_000_000, boot))
    assert boundary_of(claims, episode, "end", boot) == Known(at(65_000_000_001, boot))
    assert boundary_of(claims, episode, "start", CIVIL.domain_id) == Known(
        CIVIL.at(EPOCH_NS + 5_000_000_000)
    )
    assert outcome_of(claims, episode) == Unknown()
    assert codes(episodes) == []


def test_the_intervention_never_changes_the_episode_id() -> None:
    packages, _, _ = _cell()
    bare, _, _ = _cell(with_intervention=False)
    cycle = run_node(LogicalId("cell-3.cycle", "0412"))
    assert only_episode(every(*build(packages)), cycle) == only_episode(every(*build(bare)), cycle)


# --- a run with no task evidence ------------------------------------------------------------------


def test_a_run_with_no_task_evidence_has_no_episode() -> None:
    boot_record, boot = domain("rover boot", civil=False)
    rover = LogicalId("asset-tag", "ROVER-2")
    record, rid = run("rover/drive.ulg", first=at(0, boot), last=at(100, boot), machine=rover)
    assist, _ = intervention(
        "INT-5", machines=[rover], related=[LogicalId("record", rid)], start=at(50, boot)
    )
    runs, episodes = build({"field": [boot_record, record, assist]})
    claims = every(runs, episodes)
    assert episodes.claims == ()
    assert episodes.findings == ()
    assert episodes_of(claims, NodeRef(NodeType.RUN, f"record:{rid}")) == Unknown()

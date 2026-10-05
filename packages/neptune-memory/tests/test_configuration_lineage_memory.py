"""Machine configuration chains (ADR 0010 §2) on two deployments of different embodiments.

- warehouse AMR: the compiler's warehouse worked example (commissioning ``CFG-AMR07-r3``, the
  post-incident change to ``r4``, its authorisation envelope) plus a firmware change to ``r5`` and
  that change's requalification;
- manipulator cell: the compiler's arm-cell worked example (commissioning ``CELL3-CFG-A``, a joint
  repair to ``A.1`` and its requalification) plus a tool change and three recalibrations;
- a maintenance event that states no resulting configuration, and two changes on one day.
"""

from collections.abc import Sequence
from datetime import datetime
from fractions import Fraction

from memory_configuration_records import (
    change,
    commissioning,
    configuration_thread,
    maintenance,
    requalification,
    threads,
    worked_example,
)
from memory_identity_records import STATED, Record, cite, civil_domain, ledger, provenance
from neptune.identity import canonical_json
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import Knowledge, KnownAbsent, NotApplicable, NotCovered
from neptune.model.time import Epoch, Timescale, Timestamp
from neptune_memory.consolidate.base import Consolidation, run_consolidator
from neptune_memory.consolidate.configuration import ConfigurationLineageConsolidator
from neptune_memory.schema.claim import Claim, LedgerRecordRef
from neptune_memory.schema.interval import OPEN, CivilClock, Open, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

TX = ledger_tx(7)
SECONDS = CivilClock(Timescale.POSIX, Epoch.UNIX, Fraction(1))
DAYS = CivilClock(Timescale.POSIX, Epoch.UNIX, Fraction(86400))
SITE_CLOCK, SITE_CLOCK_ID = civil_domain("site forms")
DAY_CLOCK, DAY_CLOCK_ID = civil_domain("day-resolution register", Fraction(86400))


def posix(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp())


def stated(text: str) -> Timestamp:
    """An instant as a site form states it, on the forms' own (civil) clock."""
    return Timestamp(posix(text), SITE_CLOCK_ID)


def civil(text: str) -> Timestamp:
    """The same instant placed on the shared civil clock, as claims carry it."""
    return SECONDS.at(posix(text))


def consolidate(*packages: Sequence[Record]) -> Consolidation:
    result = run_consolidator(
        ConfigurationLineageConsolidator(),
        ledger({f"package-{i}": list(records) for i, records in enumerate(packages)}),
        (),
        {},
        recorded_at=TX,
    )
    assert not [f for f in result.findings if f.code.startswith("consolidate.")]
    return result


def of(result: Consolidation, predicate: str) -> list[Claim]:
    return [c for c in result.claims if c.predicate == predicate]


def chain(result: Consolidation, machine: NodeRef) -> list[tuple[str, Timestamp, Timestamp | Open]]:
    """A machine's ``has_configuration`` spans in valid-time order: (configuration, start, end)."""
    spans = [c for c in of(result, "has_configuration") if c.subject == machine]
    return [
        (c.object.node_id, c.valid_from, c.valid_to)  # type: ignore[union-attr]
        for c in sorted(spans, key=lambda c: c.valid_from.ticks)
    ]


def successions(result: Consolidation) -> set[tuple[str, str]]:
    return {(c.subject.node_id, c.object.node_id) for c in of(result, "succeeds")}  # type: ignore[union-attr]


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


def record_id_of(record: Record) -> RecordId:
    return record["id"]  # type: ignore[return-value]


# --- Warehouse AMR ------------------------------------------------------------------------------

AMR = LogicalId("fleet", "AMR-07")
S007 = LogicalId("siteops.site", "S-007")
R3, R4, R5 = (LogicalId("siteops.configuration", f"CFG-AMR07-r{n}") for n in (3, 4, 5))
AMR_NODE = NodeRef(NodeType.MACHINE, "fleet:AMR-07")


def warehouse() -> list[Record]:
    """The worked example, its Ledger threads, and a firmware change with its requalification."""
    return [
        *worked_example("warehouse_amr"),
        *threads(NodeType.MACHINE, AMR),
        *threads(NodeType.SITE, S007),
        *(configuration_thread(c) for c in (R3, R4, R5)),
        SITE_CLOCK,
        change("CHG-0040 firmware V01.04.00", [AMR], R5, stated("2026-10-01T06:00:00+10:00")),
        requalification("RQ-0040", [AMR], R5, stated("2026-10-01T15:00:00+10:00")),
    ]


def test_warehouse_chain_runs_from_commissioning_through_two_changes() -> None:
    result = consolidate(warehouse())
    assert result.findings == ()
    commissioned = civil("2026-09-21T09:00:00+10:00")
    map_change = civil("2026-09-27T06:00:00+10:00")
    firmware = civil("2026-10-01T06:00:00+10:00")
    assert chain(result, AMR_NODE) == [
        ("siteops.configuration:CFG-AMR07-r3", commissioned, map_change),
        ("siteops.configuration:CFG-AMR07-r4", map_change, firmware),
        ("siteops.configuration:CFG-AMR07-r5", firmware, OPEN),
    ]
    assert successions(result) == {
        ("siteops.configuration:CFG-AMR07-r4", "siteops.configuration:CFG-AMR07-r3"),
        ("siteops.configuration:CFG-AMR07-r5", "siteops.configuration:CFG-AMR07-r4"),
    }
    (latest,) = [c for c in of(result, "succeeds") if c.valid_from == firmware]
    assert latest.valid_to == OPEN and latest.assertion_kind == "stated"
    assert all(c.assertion_kind == "stated" for c in result.claims)


def test_a_requalification_extends_the_span_it_requalifies_and_adds_its_evidence() -> None:
    records = warehouse()
    result = consolidate(records)
    (r5,) = [
        c
        for c in of(result, "has_configuration")
        if c.object.node_id.endswith("r5")  # type: ignore[union-attr]
    ]
    requalified = {record_id_of(r) for r in records[-2:]}
    assert requalified <= set(r5.provenance.records)
    # Two records, each citing its form and its own date and configuration cells.
    assert len(r5.provenance.records) == 2 and len(r5.provenance.evidence) >= 2
    # The succession cites both sides of the change only, never the requalification after it.
    (succession,) = [c for c in of(result, "succeeds") if c.subject == r5.object]
    assert record_id_of(records[-1]) not in succession.provenance.records


def test_warehouse_authorisation_is_a_site_claim_on_the_envelope_window() -> None:
    (authorised,) = of(consolidate(warehouse()), "authorised_configuration")
    assert authorised.subject == NodeRef(NodeType.SITE, "siteops.site:S-007")
    assert authorised.object == NodeRef(
        NodeType.CONFIGURATION, "siteops.configuration:CFG-AMR07-r3"
    )
    assert (authorised.valid_from, authorised.valid_to) == (
        civil("2026-09-22T00:00:00+10:00"),
        civil("2027-03-22T00:00:00+10:00"),
    )


# --- Manipulator cell ---------------------------------------------------------------------------

ARM = LogicalId("robot.serial", "20415")
CELL = LogicalId("plant.cell", "CELL-3")
ARM_NODE = NodeRef(NodeType.MACHINE, "robot.serial:20415")
CELL_CONFIGS = ("CELL3-CFG-A", "CELL3-CFG-A.1", "CELL3-CFG-B", "CELL3-CFG-B.1", "CELL3-CFG-B.2")
CELL_IDS = (*CELL_CONFIGS, "CELL3-CFG-B.3")


def cell() -> list[Record]:
    """The worked example, then a gripper change and three recalibrations, each a new revision."""
    config = {name: LogicalId("plant.configuration", name) for name in CELL_IDS}
    return [
        *worked_example("manipulator_cell"),
        *threads(NodeType.MACHINE, ARM),
        *threads(NodeType.SITE, CELL),
        *(configuration_thread(c) for c in config.values()),
        SITE_CLOCK,
        change(
            "CHG-T1 gripper 2F-140 to EPick",
            [ARM],
            config["CELL3-CFG-B"],
            stated("2026-10-02T08:00:00+10:00"),
        ),
        maintenance(
            "WO-R1 TCP recalibration",
            [ARM],
            config["CELL3-CFG-B.1"],
            stated("2026-10-03T08:00:00+10:00"),
        ),
        maintenance(
            "WO-R2 TCP recalibration",
            [ARM],
            config["CELL3-CFG-B.2"],
            stated("2026-10-04T08:00:00+10:00"),
        ),
        maintenance(
            "WO-R3 TCP recalibration",
            [ARM],
            config["CELL3-CFG-B.3"],
            stated("2026-10-05T08:00:00+10:00"),
        ),
    ]


def test_manipulator_cell_chain_through_repair_tool_change_and_three_recalibrations() -> None:
    result = consolidate(cell())
    assert result.findings == ()
    spans = chain(result, ARM_NODE)
    assert [node for node, _, _ in spans] == [f"plant.configuration:{n}" for n in CELL_IDS]
    assert [start for _, start, _ in spans] == [
        civil(t)
        for t in (
            "2026-09-14T08:30:00+10:00",
            "2026-09-29T07:10:00+10:00",
            "2026-10-02T08:00:00+10:00",
            "2026-10-03T08:00:00+10:00",
            "2026-10-04T08:00:00+10:00",
            "2026-10-05T08:00:00+10:00",
        )
    ]
    # Each span ends where the next starts; the last is open.
    assert [end for _, _, end in spans] == [start for _, start, _ in spans[1:]] + [OPEN]
    ids = [f"plant.configuration:{n}" for n in CELL_IDS]
    assert successions(result) == set(zip(ids[1:], ids[:-1], strict=True))


def test_the_repair_span_cites_the_work_order_and_its_requalification() -> None:
    lines = worked_example("manipulator_cell")
    repair = {
        r["id"] for r in lines if r["kind"] in ("maintenance_event", "requalification_record")
    }
    result = consolidate(cell())
    (span,) = [
        c
        for c in of(result, "has_configuration")
        if c.object.node_id == "plant.configuration:CELL3-CFG-A.1"  # type: ignore[union-attr]
    ]
    assert set(span.provenance.records) == repair


# --- A maintenance event that states no resulting configuration -------------------------------

M = LogicalId("fleet", "QUAD-03")
QUAD = NodeRef(NodeType.MACHINE, "fleet:QUAD-03")
A, B = LogicalId("cfg", "quad-A"), LogicalId("cfg", "quad-B")


def maintained(
    configuration: LogicalId | Knowledge[LogicalId] | None,
) -> tuple[list[Record], Record]:
    work_order = maintenance(
        "WO-9 leg actuator swap", [M], configuration, stated("2026-09-10T08:00:00+00:00")
    )
    return [
        *threads(NodeType.MACHINE, M),
        configuration_thread(A),
        configuration_thread(B),
        SITE_CLOCK,
        commissioning("COM-Q", [M], A, stated("2026-09-01T08:00:00+00:00")),
        work_order,
        change("CHG-Q", [M], B, stated("2026-09-20T08:00:00+00:00")),
    ], work_order


def test_a_maintenance_event_with_no_resulting_configuration_is_a_gap_never_bridged() -> None:
    for unstated in (None, NotCovered()):  # Unknown: could state it and does not; no place for it
        records, work_order = maintained(unstated)
        result = consolidate(records)
        start, gap, end = (civil(f"2026-09-{d}T08:00:00+00:00") for d in ("01", "10", "20"))
        assert chain(result, QUAD) == [("cfg:quad-A", start, gap), ("cfg:quad-B", end, OPEN)]
        (unknown,) = of(result, "configuration_unknown")
        assert (unknown.subject, unknown.object) == (
            QUAD,
            LedgerRecordRef(record_id_of(work_order)),
        )
        assert (unknown.valid_from, unknown.valid_to) == (gap, end)
        assert successions(result) == set()  # B never succeeds A across the gap
        (finding,) = result.findings
        assert finding.code == "configuration.chain_gap"
        assert finding.records == (record_id_of(work_order),)


def test_a_maintenance_event_stating_no_configuration_places_nothing() -> None:
    # The work order's own cell, stated: the declaration says no configuration resulted.
    for absent in (KnownAbsent(provenance(cite("forms/WO-9", 64), STATED)), NotApplicable()):
        records, _ = maintained(absent)
        result = consolidate(records)
        assert [n for n, _, _ in chain(result, QUAD)] == ["cfg:quad-A", "cfg:quad-B"]
        assert successions(result) == {("cfg:quad-B", "cfg:quad-A")}
        assert result.findings == ()


# --- Two changes on one day ---------------------------------------------------------------------

C1, C2, C3, C4 = (LogicalId("cfg", f"amr-{n}") for n in range(1, 5))
DAY = 20_000  # days since the Unix epoch


def day(n: int) -> Timestamp:
    return Timestamp(DAY + n, DAY_CLOCK_ID)


def same_day(second: LogicalId) -> tuple[list[Record], Record, Record]:
    first = change("CHG-1 map r13", [AMR], C2, day(10))
    other = change("CHG-2 speed limit", [AMR], second, day(10))
    return (
        [
            *threads(NodeType.MACHINE, AMR),
            *(configuration_thread(c) for c in (C1, C2, C3, C4)),
            DAY_CLOCK,
            commissioning("COM-1", [AMR], C1, day(0)),
            first,
            other,
            change("CHG-3", [AMR], C4, day(12)),
        ],
        first,
        other,
    )


def test_two_changes_on_one_day_are_candidates_never_ordered() -> None:
    records, first, other = same_day(C3)
    result = consolidate(records)
    days = [DAYS.at(DAY + n) for n in (0, 10, 12)]
    assert chain(result, AMR_NODE) == [
        ("cfg:amr-1", days[0], days[1]),
        ("cfg:amr-4", days[2], OPEN),
    ]
    candidates = sorted(of(result, "configuration_candidate"), key=lambda c: c.object.node_id)  # type: ignore[union-attr]
    assert [(c.object.node_id, c.valid_from, c.valid_to) for c in candidates] == [  # type: ignore[union-attr]
        ("cfg:amr-2", days[1], days[2]),
        ("cfg:amr-3", days[1], days[2]),
    ]
    # Each reading cites only the record that states it.
    assert [c.provenance.records for c in candidates] == [
        (record_id_of(first),),
        (record_id_of(other),),
    ]
    assert successions(result) == set()  # nothing is claimed across an ambiguous span
    (overlap,) = result.findings
    assert overlap.code == "configuration.chain_overlap"
    assert set(overlap.records) == {record_id_of(first), record_id_of(other)}


def test_two_changes_on_one_day_stating_one_configuration_agree() -> None:
    records, first, other = same_day(C2)
    result = consolidate(records)
    assert result.findings == ()
    assert [n for n, _, _ in chain(result, AMR_NODE)] == ["cfg:amr-1", "cfg:amr-2", "cfg:amr-4"]
    (span,) = [c for c in of(result, "has_configuration") if c.object.node_id == "cfg:amr-2"]  # type: ignore[union-attr]
    assert set(span.provenance.records) == {record_id_of(first), record_id_of(other)}


def test_two_changes_on_one_day_at_different_times_are_ordered() -> None:
    records = [
        *threads(NodeType.MACHINE, AMR),
        *(configuration_thread(c) for c in (C1, C2, C3)),
        SITE_CLOCK,
        commissioning("COM-1", [AMR], C1, stated("2026-09-01T09:00:00+10:00")),
        change("CHG-1", [AMR], C2, stated("2026-09-27T06:00:00+10:00")),
        change("CHG-2", [AMR], C3, stated("2026-09-27T14:30:00+10:00")),
    ]
    result = consolidate(records)
    assert result.findings == ()
    assert successions(result) == {("cfg:amr-2", "cfg:amr-1"), ("cfg:amr-3", "cfg:amr-2")}


def test_a_known_and_an_unknown_outcome_on_one_instant_are_a_candidate_not_a_pick() -> None:
    records, _, _ = same_day(C3)
    records[-3] = maintenance("WO-1", [AMR], None, day(10))  # replaces CHG-1 at day 10
    result = consolidate(records)
    (candidate,) = of(result, "configuration_candidate")
    assert candidate.object.node_id == "cfg:amr-3"  # type: ignore[union-attr]
    assert codes(result) == ["configuration.chain_overlap"]


def test_an_ambiguous_configuration_field_gives_one_candidate_per_reading() -> None:
    from neptune.model.knowledge import Ambiguous, Candidate

    records = [
        *threads(NodeType.MACHINE, AMR),
        *(configuration_thread(c) for c in (C1, C2, C3)),
        DAY_CLOCK,
        commissioning("COM-1", [AMR], C1, day(0)),
        change("CHG-1", [AMR], Ambiguous((Candidate(C2), Candidate(C3))), day(4)),
    ]
    result = consolidate(records)
    assert {c.object.node_id for c in of(result, "configuration_candidate")} == {  # type: ignore[union-attr]
        "cfg:amr-2",
        "cfg:amr-3",
    }
    assert result.findings == ()  # one record's own ambiguity: the claims say it
    assert successions(result) == set()


def test_one_record_naming_several_machines_places_each() -> None:
    other = LogicalId("fleet", "AMR-08")
    records = [
        *threads(NodeType.MACHINE, AMR, other),
        configuration_thread(C1),
        DAY_CLOCK,
        commissioning("COM-fleet", [AMR, other], C1, day(0)),
    ]
    result = consolidate(records)
    assert {c.subject.node_id for c in of(result, "has_configuration")} == {
        "fleet:AMR-07",
        "fleet:AMR-08",
    }


def test_the_chain_is_deterministic_and_independent_of_package_and_record_order() -> None:
    records = cell()
    once = consolidate(records)
    again = consolidate(list(reversed(records[:20])), list(reversed(records[20:])))
    assert canonical_json.dumps(once.to_json()) == canonical_json.dumps(again.to_json())

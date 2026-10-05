"""Events (ADR 0013) on the issue's scenarios, with the record shapes the compiler and Deploy write.

- Warehouse: an AMR incident reconstructed from a CMMS incident record, an exported e-stop topic on
  the robot's boot clock (mapped to civil time by a stated clock mapping) and a syslog export.
- Arm cell (the acceptance corpus's INC-C3-0011): an incident report whose timeline is on the HMI
  clock, and Deploy's ``diagnostic events`` table from the cell PC's bag, 96.7 s ahead; a stated
  mapping relates them, and without it nothing is compared.
- A near miss known only from an operator's note.
- Two clocks with and without a mapping, and a mapping too coarse to decide.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from memory_event_records import SECOND, incident, intervention, table
from memory_identity_records import Record, ledger
from memory_run_records import NS, domain, mapping
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import AssertionKind, Known, Unknown
from neptune.model.time import Epoch, Timescale, Timestamp
from neptune_memory.consolidate.base import Consolidation, run_consolidator
from neptune_memory.consolidate.event_records import resolve_config
from neptune_memory.consolidate.events import EventConsolidator, event_node
from neptune_memory.consolidate.runs import involvement
from neptune_memory.schema.claim import Claim, LedgerRecordRef, TypedLiteral, ValueType
from neptune_memory.schema.interval import CivilClock, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES, EVENT_KINDS

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.jsonvalue import JsonValue

TX = ledger_tx(9)
CIVIL: Final = CivilClock(Timescale.POSIX, Epoch.UNIX, NS)
T0: Final = 1_790_000_000 * SECOND  # an instant in 2026, POSIX nanoseconds
WINDOW: Final = 5 * SECOND  # the default window on a nanosecond clock
STATED: Final = AssertionKind.STATED


def consolidate(
    packages: Mapping[str, Sequence[Record]], config: Mapping[str, JsonValue] | None = None
) -> Consolidation:
    return run_consolidator(
        EventConsolidator(),
        ledger(packages),
        (),
        resolve_config(config),
        recorded_at=TX,
        registry=CORE_PREDICATES,
    )


def of(result: Consolidation, predicate: str, subject: NodeRef | None = None) -> list[Claim]:
    return [
        c
        for c in result.claims
        if c.predicate == predicate and (subject is None or c.subject == subject)
    ]


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


def text(value: str) -> TypedLiteral:
    return TypedLiteral(ValueType.TEXT, value)


def pairs(result: Consolidation) -> set[tuple[NodeRef, NodeRef]]:
    return {(c.subject, c.object) for c in of(result, "co_occurs_within")}  # type: ignore[misc]


def contract_holds(result: Consolidation) -> None:
    """Nothing the runner refused, every claim cites evidence, and nothing names a cause."""
    assert not [f for f in result.findings if f.code.startswith("consolidate.")]
    for claim in result.claims:
        assert claim.provenance.evidence
        assert claim.provenance.records
        assert "cause" not in claim.predicate
        if claim.predicate == "event_kind":
            assert isinstance(claim.object, TypedLiteral)
            assert claim.object.value in EVENT_KINDS


# --- Warehouse: CMMS incident + e-stop topic + syslog -------------------------------------------

AMR07: Final = LogicalId("cmms.asset", "AMR-07")
WH_NORTH, AISLE_14 = LogicalId("site", "WH-NORTH"), LogicalId("zone", "WH-NORTH/AISLE-14")
B0: Final = 1_000 * SECOND  # the AMR's boot clock at T0


def warehouse(*, mapped: bool = True) -> tuple[dict[str, list[Record]], dict[str, Any]]:
    cmms_clock, cmms = domain("cmms export", civil=True)
    sys_clock, syslog_domain = domain("syslog host clock", civil=True)
    boot_clock, boot = domain("amr-07 boot", civil=False)
    report, report_id = incident(
        "cmms INC-0007",
        occurred=Timestamp(T0 + 2 * SECOND, cmms),
        severity="S2",
        description="AMR-07 struck a pallet in aisle 14; e-stop pressed by the picker",
        machines=[AMR07],
        site=WH_NORTH,
        zone=AISLE_14,
        identifier=LogicalId("cmms.incident", "INC-0007"),
    )
    estop, _, estop_rows = table(
        "/safety/estop",
        ("stamp", "@clock:stamp", "state", "robot"),
        [
            (B0 + SECOND // 2, boot, "ESTOP_PRESSED", "AMR-07"),
            (B0 + 40 * SECOND, boot, "ESTOP_RELEASED", "AMR-07"),
        ],
        file="amr-07 2026-09-14 estop.json",
    )
    syslog, _, syslog_rows = table(
        "syslog",
        ("sec", "nsec", "@clock:ts", "host", "severity", "msg"),
        [
            (T0 // SECOND + 1, 0, syslog_domain, "amr-07", "err", "safety_plc: SF2 field breach"),
            (T0 // SECOND + 1, 200_000_000, syslog_domain, "amr-07", "info", "nav: replanning"),
        ],
    )
    records: list[Record] = [cmms_clock, sys_clock, boot_clock, report, *estop, *syslog]
    if mapped:
        records.append(mapping("amr-07 ntp log", boot, cmms, anchor=(B0, T0)))
    config: dict[str, Any] = {
        "vendors": {
            "ros.estop": {"ESTOP_PRESSED": "emergency_stop", "ESTOP_RELEASED": "reset"},
            "syslog": {"err": "fault", "warning": "warning", "info": "not_an_event"},
        },
        "tables": [
            {
                "name": "/safety/estop",
                "vendor": "ros.estop",
                "kind": "state",
                "at": {"ticks": "stamp"},
                "clock": {"column": "@clock:stamp"},
                "machine": {"column": "robot", "namespace": "fleet.robot"},
            },
            {
                "name": "syslog",
                "vendor": "syslog",
                "kind": "severity",
                "at": {"seconds": "sec", "nanoseconds": "nsec"},
                "clock": {"column": "@clock:ts"},
                "machine": {"column": "host", "namespace": "syslog.host"},
                "severity": "severity",
                "description": "msg",
            },
        ],
    }
    ids = {"report": report_id, "estop": estop_rows, "syslog": syslog_rows, "boot": boot}
    return {"warehouse-2026-09-14": records}, {"config": config, **ids}


def test_warehouse_incident_is_reconstructed_from_three_sources() -> None:
    packages, built = warehouse()
    result = consolidate(packages, built["config"])
    contract_holds(result)
    report = event_node(built["report"])
    pressed, released = (event_node(r) for r in built["estop"])
    fault, info = (event_node(r) for r in built["syslog"])

    # The CMMS record: its kind, its severity verbatim, what and where, on civil time.
    (kind,) = of(result, "event_kind", report)
    assert kind.object == text("incident")
    assert kind.valid_from == CIVIL.at(T0 + 2 * SECOND)
    assert kind.valid_to == CIVIL.at(T0 + 2 * SECOND + 1)
    assert [c.object for c in of(result, "stated_severity", report)] == [text("S2")]
    assert [c.object for c in of(result, "involves", report)] == [
        NodeRef(NodeType.MACHINE, "cmms.asset:AMR-07")
    ]
    assert [c.object for c in of(result, "at_site", report)] == [
        NodeRef(NodeType.SITE, "site:WH-NORTH")
    ]
    assert [c.object for c in of(result, "in_zone", report)] == [
        NodeRef(NodeType.ZONE, "zone:WH-NORTH/AISLE-14")
    ]
    stated = [c for c in result.claims if c.subject == report and c.predicate != "co_occurs_within"]
    assert {c.assertion_kind for c in stated} == {STATED}  # what the CMMS row states

    # The e-stop: on the boot clock as declared, and on civil time through the stated mapping.
    kinds = of(result, "event_kind", pressed)
    assert {c.object for c in kinds} == {text("emergency_stop")}
    assert {c.valid_from.domain_id for c in kinds} == {built["boot"], CIVIL.domain_id}
    (on_civil,) = [c for c in kinds if c.valid_from.domain_id == CIVIL.domain_id]
    assert on_civil.valid_from == CIVIL.at(T0 + SECOND // 2)
    assert any(r for r in on_civil.provenance.records if r not in built["estop"])  # the mapping
    assert [c.object for c in of(result, "declared_kind", pressed)] == [text("ESTOP_PRESSED")] * 2
    assert {c.object for c in of(result, "event_kind", released)} == {text("reset")}

    # The syslog error is a fault with its message; the info line is declared not an event.
    assert {c.object for c in of(result, "event_kind", fault)} == {text("fault")}
    assert {c.object for c in of(result, "has_description", fault)} == {
        text("safety_plc: SF2 field breach")
    }
    assert not [c for c in result.claims if c.subject == info]

    # Co-occurrence: each pair of sources within 5 s, both ways, the window as valid time.
    assert pairs(result) == {
        (report, pressed),
        (pressed, report),
        (report, fault),
        (fault, report),
        (pressed, fault),
        (fault, pressed),
    }
    for claim in of(result, "co_occurs_within"):
        assert claim.valid_from.domain_id == CIVIL.domain_id
        assert isinstance(claim.valid_to, Timestamp)
        assert claim.valid_to.ticks - claim.valid_from.ticks == WINDOW
        assert claim.assertion_kind == "observed"
    (estop_report,) = of(result, "co_occurs_within", pressed)[:1]
    assert estop_report.valid_from == CIVIL.at(T0 + SECOND // 2)  # the earlier onset opens it


def test_warehouse_without_the_mapping_compares_only_civil_sources() -> None:
    packages, built = warehouse(mapped=False)
    result = consolidate(packages, built["config"])
    contract_holds(result)
    report = event_node(built["report"])
    pressed = event_node(built["estop"][0])
    fault = event_node(built["syslog"][0])
    assert pairs(result) == {(report, fault), (fault, report)}
    assert "events.clocks_unrelated" in codes(result)
    assert {c.valid_from.domain_id for c in of(result, "event_kind", pressed)} == {built["boot"]}


# --- Arm cell: INC-C3-0011 ------------------------------------------------------------------------

H0: Final = 52_358 * SECOND  # 14:32:38 on the HMI's time-of-day clock
AHEAD: Final = 96_700_000_000  # the cell PC's clock runs 96.7 s ahead of the HMI
ARM3A: Final = LogicalId("cmms.asset", "ARM-3A")


def arm_cell(*, mapped: bool) -> tuple[dict[str, list[Record]], dict[str, Any]]:
    hmi_clock, hmi = domain("cell3 hmi", civil=False)
    pc_clock, pc = domain("cell3 ipc header.stamp", civil=False)
    report, report_id = incident(
        "INC-C3-0011.pdf",
        occurred=Timestamp(H0, hmi),
        severity="S2",
        description="Part dropped onto the floor guard after a protective stop; E-stop at OP-2",
        machines=[ARM3A],
        site=LogicalId("site", "PLANT-2"),
        zone=LogicalId("zone", "PLANT-2/CELL-3"),
        timeline=[
            (Timestamp(H0, hmi), "Protective stop: joint 5 torque over its limit"),
            (Timestamp(H0 + 3 * SECOND, hmi), "Operator presses the E-stop at OP-2"),
        ],
    )

    def stamp(hmi_ticks: int) -> tuple[int, int]:
        return divmod(hmi_ticks + AHEAD, SECOND)

    header = (
        "event_kind",
        "level",
        "name",
        "message",
        "hardware_id",
        "stamp.sec",
        "stamp.nanosec",
        "@clock:stamp",
    )
    statuses = [
        ("diagnostic.warn", 1, "vision/hand_eye", "residual 4.1 mm", H0 - 200 * SECOND),
        ("diagnostic.ok", 0, "vision/hand_eye", "ok", H0 - 100 * SECOND),
        ("safety.protective_stop", 2, "safety/joint5", "joint 5 41.7 Nm > 35.0 Nm", H0),
        ("safety.estop", 2, "safety/estop", "E-STOP: OP-2", H0 + 3_100_000_000),
    ]
    rows = [(k, lvl, name, msg, "ARM-3A", *stamp(t), pc) for k, lvl, name, msg, t in statuses]
    diagnostics, _, row_ids = table(
        "diagnostic events", header, rows, file="pallet_2026-09-14 diagnostics export"
    )
    records: list[Record] = [hmi_clock, pc_clock, report, *diagnostics]
    if mapped:
        # The site survey states the offset at the incident, within 50 ms.
        records.append(
            mapping("time-sync survey", pc, hmi, anchor=(H0 + AHEAD, H0), bound=50_000_000)
        )
    config: dict[str, Any] = {
        "vendors": {
            "deploy.ros2_diagnostics": {
                "diagnostic.ok": "not_an_event",
                "diagnostic.warn": "warning",
                "diagnostic.error": "fault",
                "diagnostic.stale": "stale",
                "safety.estop": "emergency_stop",
                "safety.protective_stop": "protective_stop",
            },
        },
        "tables": [
            {
                "name": "diagnostic events",
                "vendor": "deploy.ros2_diagnostics",
                "kind": "event_kind",
                "at": {"seconds": "stamp.sec", "nanoseconds": "stamp.nanosec"},
                "clock": {"column": "@clock:stamp"},
                "machine": {"column": "hardware_id", "namespace": "ros.hardware_id"},
                "severity": "level",
                "description": "message",
            }
        ],
    }
    return {"plant-2 handover": records}, {
        "config": config,
        "report": report_id,
        "rows": row_ids,
        "hmi": hmi,
        "pc": pc,
    }


def test_arm_cell_incident_links_the_bag_to_the_hmi_timeline_through_the_survey() -> None:
    packages, built = arm_cell(mapped=True)
    result = consolidate(packages, built["config"])
    contract_holds(result)
    report_id: RecordId = built["report"]
    warn, ok, protective, estop = (event_node(r) for r in built["rows"])
    entry_stop, entry_estop = (event_node(report_id, "timeline", i) for i in (0, 1))

    # The bag's e-stop is an emergency stop on the PC clock, and again on the HMI clock.
    kinds = of(result, "event_kind", estop)
    assert {c.object for c in kinds} == {text("emergency_stop")}
    assert {c.valid_from.domain_id for c in kinds} == {built["pc"], built["hmi"]}
    assert {c.object for c in of(result, "stated_severity", estop)} == {
        TypedLiteral(ValueType.INTEGER, 2)
    }
    assert {c.object for c in of(result, "involves", estop)} == {
        NodeRef(NodeType.MACHINE, "ros.hardware_id:ARM-3A")
    }
    assert not [c for c in result.claims if c.subject == ok]  # declared not an event

    # Timeline entries are events of the report, evidenced by it, with their text verbatim.
    assert {c.object for c in of(result, "has_description", entry_estop)} == {
        text("Operator presses the E-stop at OP-2")
    }
    assert {c.object for c in of(result, "evidenced_by", entry_estop)} == {
        LedgerRecordRef(report_id)
    }
    assert not of(result, "event_kind", entry_estop)  # a timeline entry declares no kind

    # The e-stop in the bag and the operator's e-stop co-occur on the HMI clock, via the survey.
    found = pairs(result)
    assert (estop, entry_estop) in found and (entry_estop, estop) in found
    assert (protective, entry_stop) in found
    assert not [p for p in found if warn in p]  # 200 s earlier
    from_report = {n for n in (event_node(report_id), entry_stop, entry_estop)}
    assert not [p for p in found if set(p) <= from_report]  # one source: never compared
    (claim,) = [c for c in of(result, "co_occurs_within", estop) if c.object == entry_estop]
    assert claim.valid_from.domain_id == built["hmi"]
    assert isinstance(claim.valid_to, Timestamp)
    assert claim.valid_to.ticks - claim.valid_from.ticks == WINDOW
    survey = [r for r in claim.provenance.records if r not in (report_id, *built["rows"])]
    assert survey  # the mapping record and its target clock are cited


def test_arm_cell_without_a_mapping_compares_nothing_across_the_two_clocks() -> None:
    packages, built = arm_cell(mapped=False)
    result = consolidate(packages, built["config"])
    contract_holds(result)
    assert not of(result, "co_occurs_within")
    (unrelated,) = [f for f in result.findings if f.code == "events.clocks_unrelated"]
    clocks: list[str] = unrelated.details["clocks"]  # type: ignore[assignment]
    assert sorted(clocks) == sorted([built["hmi"], built["pc"]])


# --- A near miss known only from an operator's note --------------------------------------------


def test_near_miss_from_an_operator_note_states_only_what_the_note_states() -> None:
    clock, utc = domain("site log", civil=True)
    note, note_id = incident(
        "operator note 2026-09-02",
        occurred=Timestamp(T0, utc),
        severity="near miss",
        description="Forklift crossed in front of an AMR in aisle 3; the robot stopped short",
        machines=None,  # left blank: which robot is not stated
        site=LogicalId("site", "WH-SOUTH"),
    )
    config = {"vendors": {"incident_record": {"near miss": "near_miss"}}}
    result = consolidate({"notes": [clock, note]}, config)
    contract_holds(result)
    event = event_node(note_id)
    assert [c.object for c in of(result, "event_kind", event)] == [text("near_miss")]
    assert [c.object for c in of(result, "stated_severity", event)] == [text("near miss")]
    assert of(result, "has_description", event)
    assert not of(result, "in_zone", event) and not of(result, "co_occurs_within")
    # The machine is not stated: Unknown, never a guess.
    assert involvement(result.claims, event, "involves") == Unknown()
    assert involvement(result.claims, event, "at_site") == Known(
        (NodeRef(NodeType.SITE, "site:WH-SOUTH"),)
    )


def test_an_unmapped_severity_keeps_the_record_kind() -> None:
    clock, utc = domain("site log", civil=True)
    note, note_id = incident("form 7", occurred=Timestamp(T0, utc), severity="near-miss (B)")
    result = consolidate(
        {"forms": [clock, note]}, {"vendors": {"incident_record": {"near miss": "near_miss"}}}
    )
    assert [c.object for c in of(result, "event_kind", event_node(note_id))] == [text("incident")]


# --- Two clocks ---------------------------------------------------------------------------------


def two_clocks(
    *, anchor: bool, bound: int | None = None, apart: int = SECOND
) -> tuple[Consolidation, NodeRef, NodeRef, RecordId, RecordId]:
    """An intervention on a robot's boot clock and an incident on a controller's clock."""
    a_clock, a = domain("robot boot", civil=False)
    b_clock, b = domain("controller", civil=False)
    assist, assist_id = intervention(
        "formant intervention 41",
        start=Timestamp(10 * SECOND, a),
        end=Timestamp(70 * SECOND, a),
        mode="remote assist",
        reason="robot stuck at a door",
        machines=[LogicalId("formant.device", "dev-9")],
    )
    report, report_id = incident("controller fault log", occurred=Timestamp(5 * SECOND + apart, b))
    records = [a_clock, b_clock, assist, report]
    if anchor:
        records.append(mapping("sync", a, b, anchor=(10 * SECOND, 5 * SECOND), bound=bound))
    result = consolidate({"p": records})
    return result, event_node(assist_id), event_node(report_id), a, b


def test_events_on_two_clocks_without_a_mapping_are_unknown_together() -> None:
    result, _, _, a, b = two_clocks(anchor=False)
    assert not of(result, "co_occurs_within")
    (finding,) = [f for f in result.findings if f.code == "events.clocks_unrelated"]
    assert sorted(finding.details["clocks"]) == sorted([a, b])  # type: ignore[arg-type,type-var]


def test_events_on_two_clocks_with_a_mapping_co_occur_on_the_target_clock() -> None:
    result, assist, report, a, b = two_clocks(anchor=True)
    contract_holds(result)
    assert pairs(result) == {(assist, report), (report, assist)}
    (claim,) = of(result, "co_occurs_within", assist)
    assert claim.valid_from == Timestamp(5 * SECOND, b)  # the intervention's onset, mapped
    assert claim.valid_to == Timestamp(10 * SECOND, b)
    # The intervention holds over its stated interval on both clocks; its mode is its kind.
    spans = {(c.valid_from, c.valid_to) for c in of(result, "evidenced_by", assist)}
    assert spans == {
        (Timestamp(10 * SECOND, a), Timestamp(70 * SECOND, a)),
        (Timestamp(5 * SECOND, b), Timestamp(65 * SECOND, b)),
    }
    assert {c.object for c in of(result, "declared_kind", assist)} == {text("remote assist")}
    assert {c.object for c in of(result, "event_kind", assist)} == {text("intervention")}


def test_a_mapping_too_coarse_to_decide_gives_a_finding_not_a_claim() -> None:
    result, *_ = two_clocks(anchor=True, bound=3 * SECOND, apart=SECOND)
    assert not of(result, "co_occurs_within")
    assert "events.co_occurrence_undecided" in codes(result)


def test_events_further_apart_than_the_window_do_not_co_occur() -> None:
    result, *_ = two_clocks(anchor=True, apart=WINDOW)
    assert not of(result, "co_occurs_within")
    assert "events.co_occurrence_undecided" not in codes(result)
    result, *_ = two_clocks(anchor=True, apart=WINDOW - 1)
    assert len(of(result, "co_occurs_within")) == 2


def test_the_window_is_configured_in_seconds_and_scaled_by_each_clock() -> None:
    a_clock, a = domain("ms clock", civil=False, resolution=NS * 10**6)
    b_clock, b = domain("other ms clock", civil=False, resolution=NS * 10**6)
    one, one_id = incident("one", occurred=Timestamp(1000, a))
    two, two_id = incident("two", occurred=Timestamp(1400, b))
    sync = mapping("sync", a, b, anchor=(0, 0))
    config = {"co_occurrence": {"window_seconds": "0.5"}}
    result = consolidate({"p": [a_clock, b_clock, one, two, sync]}, config)
    (claim,) = of(result, "co_occurs_within", event_node(one_id))
    assert claim.object == event_node(two_id)
    assert (claim.valid_from, claim.valid_to) == (Timestamp(1000, b), Timestamp(1500, b))

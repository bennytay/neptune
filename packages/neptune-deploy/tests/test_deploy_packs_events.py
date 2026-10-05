"""Event-timeline packs over a graph-schema 1.6.0-shaped snapshot (Memory ADR 0013, PR #125).

The fixture is generated in the shape #125 publishes and validated against that schema in
``test_deploy_packs_contracts``; nothing here depends on #125 being merged.
"""

from deploy_pack_graphs import CIVIL, DAY, T0, TEACH, rec
from deploy_pack_support import AMR, SITE, events_pack
from neptune_deploy.packs.compile import EvidencePack, Section
from neptune_deploy.packs.snapshot import Interval, Stamp

ESTOP, FAULT, INTERVENTION, PENDANT, MAYBE = (
    f"record:{rec('event ' + n)}" for n in ("estop", "fault", "intervention", "pendant", "maybe")
)


def _section(pack: EvidencePack, section_id: str) -> Section:
    (section,) = [s for s in pack.sections if s.template.id == section_id]
    return section


def test_the_timeline_is_in_onset_order_on_the_pack_clock() -> None:
    timeline = _section(events_pack(), "events")
    assert [(e.node.node_id, e.knowledge) for e in timeline.entries] == [
        (ESTOP, "known"),
        (FAULT, "known"),
        (MAYBE, "ambiguous"),
        (INTERVENTION, "conflict"),
        (INTERVENTION, "conflict"),
    ]
    assert all(e.valid.start.domain == CIVIL for e in timeline.entries)
    starts = [e.valid.start.ticks for e in timeline.entries]
    assert starts == sorted(starts)


def test_a_mapped_placement_names_the_mapping_it_was_placed_through() -> None:
    timeline = _section(events_pack(), "events")
    estop, fault = timeline.entries[0], timeline.entries[1]
    assert estop.placement_records == tuple(
        sorted((rec("clock mapping hmi->civil"), rec("civil clock")))
    )
    assert fault.placement_records == ()  # placed natively on the civil clock


def test_an_event_placed_at_two_times_is_a_conflict_showing_both() -> None:
    timeline = _section(events_pack(), "events")
    conflicts = [e for e in timeline.entries if e.knowledge == "conflict"]
    assert len(conflicts) == 2
    assert conflicts[0].valid != conflicts[1].valid
    assert {e.placement_records[0] for e in conflicts if e.placement_records} == {
        rec("clock mapping plc->civil A"),
        rec("clock mapping plc->civil B"),
    }
    for entry in conflicts:
        assert {s.claim.predicate for s in entry.statements} == {
            "event_kind",
            "involves",
            "evidenced_by",
        }


def test_an_event_on_an_unmapped_clock_is_listed_never_compared() -> None:
    timeline = _section(events_pack(), "events")
    (pendant,) = timeline.other_clocks
    assert pendant.node.node_id == PENDANT
    assert pendant.valid.start.domain == TEACH
    # Events already placed on the pack clock are not listed again on their own clocks.
    assert {e.node.node_id for e in timeline.other_clocks}.isdisjoint(
        {e.node.node_id for e in timeline.entries}
    )


def test_a_possible_involvement_is_ambiguous_with_every_reading() -> None:
    timeline = _section(events_pack(subject=AMR), "events")
    (maybe,) = timeline.entries
    assert maybe.node.node_id == MAYBE
    assert maybe.knowledge == "ambiguous"
    readings = sorted(
        str(s.claim.object["node_id"])
        for s in maybe.statements
        if s.claim.predicate == "involves_candidate"
    )
    assert readings == ["asset-tag:AMR-12", "asset-tag:ARM-06"]


def test_co_occurrence_is_its_own_section_and_never_cause() -> None:
    pack = events_pack()
    section = _section(pack, "co-occurrence")
    pairs = [(e.node.node_id, e.statements[0].claim.object["node_id"]) for e in section.entries]
    assert pairs == sorted([(ESTOP, FAULT), (FAULT, ESTOP)])
    assert all(e.statements[0].claim.assertion_kind == "observed" for e in section.entries)
    window = section.entries[0].valid
    assert isinstance(window.end, Stamp)
    assert window.end.ticks - window.start.ticks == 5 * 10**9
    assert "cause" in section.template.title.lower()  # "never cause"


def test_a_site_timeline_reaches_events_at_the_site_and_of_its_machines() -> None:
    timeline = _section(events_pack(subject=SITE), "events")
    assert {e.node.node_id for e in timeline.entries} == {ESTOP, FAULT, MAYBE, INTERVENTION}


def test_an_interval_with_no_events_is_not_covered_and_counts_what_lies_outside() -> None:
    later = Interval(Stamp(CIVIL, T0 + 2 * DAY), Stamp(CIVIL, T0 + 3 * DAY))
    timeline = _section(events_pack(interval=later), "events")
    assert timeline.entries == ()
    assert timeline.knowledge == "known"  # the pendant event is still listed on its own clock
    assert timeline.outside_interval > 0
    on_teach = events_pack(interval=Interval(Stamp(TEACH, 0), Stamp(TEACH, 100)))
    (pendant,) = _section(on_teach, "events").entries
    assert pendant.node.node_id == PENDANT

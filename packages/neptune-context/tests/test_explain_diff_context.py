"""``diff(subject, before, after)`` (ADR 0010 §4): claims opened, closed and superseded."""

from __future__ import annotations

from fractions import Fraction
from functools import cache
from typing import TYPE_CHECKING

import explain_fixtures_context as X
from neptune_context.answer import answer_problems
from neptune_context.engine import LocalEngine
from neptune_context.explain import Caps, IndexedReader, render_markdown
from neptune_context.packets.codec import canonical_bytes, decode
from neptune_context.packets.model import GapCode
from neptune_context.packets.trails import Change, DiffTrail, TxPoint, WorldPoint
from neptune_context.query import (
    Budget,
    CivilTime,
    ClockBridge,
    DomainClock,
    During,
    Instant,
    Query,
    Subject,
)
from neptune_context.query.model import Diff
from neptune_context.sdk import Client

if TYPE_CHECKING:
    import pytest

    from neptune_context.packets.model import ContextPacket

UTC_NS = CivilTime("utc", "unix", Fraction(1, 10**9))
LEG = Subject("machine", "asset-tag:LEG-9")
AMR = Subject("machine", "asset-tag:AMR-9")


@cache
def reader() -> IndexedReader:
    return IndexedReader(X.document())


def ask(query: Query, **kwargs: object) -> ContextPacket:
    packet = Client(LocalEngine(reader(), X.Catalog(), **kwargs)).query(query)  # type: ignore[arg-type]
    assert answer_problems(query, packet) == ()
    assert decode(canonical_bytes(packet)) == packet
    return packet


def diff(subject: Subject, before: int | Instant, after: int | Instant, **kwargs: object) -> Query:
    return Query(
        include_inferred=bool(kwargs.pop("include_inferred", True)),
        budget=Budget(items=int(kwargs.pop("items", 60))),  # type: ignore[call-overload]
        explain=(Diff(subject, before, after),),
        **kwargs,  # type: ignore[arg-type]
    )


def trail(packet: ContextPacket) -> DiffTrail:
    (only,) = packet.trails
    assert isinstance(only, DiffTrail)
    return only


def changes(packet: ContextPacket) -> set[tuple[str, Change, tuple[str, ...], tuple[str, ...]]]:
    return {(c.predicate, c.change, c.before, c.after) for c in trail(packet).changes}


def test_a_firmware_change_in_world_time_is_one_superseded_configuration() -> None:
    old = X.find(X.LEG, "has_configuration", X.FW_OLD)  # the restated [1 Jan, 1 May)
    new = X.find(X.LEG, "has_configuration", X.FW_NEW)
    packet = ask(diff(LEG, Instant(UTC_NS, X.APR_1), Instant(UTC_NS, X.JUN_1)))
    found = trail(packet)
    assert found.before == WorldPoint(X.UTC, X.APR_1)  # type: ignore[arg-type]
    assert found.after == WorldPoint(X.UTC, X.JUN_1)  # type: ignore[arg-type]
    assert changes(packet) == {
        ("has_configuration", Change.SUPERSEDED, (old.id,), (new.id,)),
    }
    assert {old.id, new.id} <= packet.claim_ids
    text = render_markdown(packet)
    assert "### has\\_configuration" in text and "replaced by" in text
    assert '"cfg:leg-9-fw-3.1.4"' in text and '"cfg:leg-9-fw-3.2.0"' in text


def test_a_firmware_change_in_transaction_time_restates_the_old_and_opens_the_new() -> None:
    believed = X.find(X.LEG, "has_configuration", X.FW_OLD, current=False)  # open-ended, tx 1
    restated = X.find(X.LEG, "has_configuration", X.FW_OLD)
    new = X.find(X.LEG, "has_configuration", X.FW_NEW)
    packet = ask(diff(LEG, 1, 3))
    assert trail(packet).before == TxPoint(1) and trail(packet).after == TxPoint(3)  # type: ignore[arg-type]
    assert changes(packet) == {
        ("has_configuration", Change.CLOSED, (believed.id,), (restated.id,)),
        ("has_configuration", Change.OPENED, (), (new.id,)),
    }
    # The old belief is no longer current: named, linked at transaction 1, never carried.
    assert believed.id not in packet.claim_ids
    assert any(believed.id in g.refs for g in packet.gaps if g.at == "/explain/0")
    text = render_markdown(packet)
    assert f"neptune://claim/{believed.id}?as_of=1" in text and "narrowed to" in text


def test_a_move_is_superseded_with_memorys_split_closure() -> None:
    dock = X.find(X.AMR, "located_at", X.DOCK, current=False)  # [1 Feb, open), tx 1
    closure = X.find(X.AMR, "located_at", X.DOCK)  # [1 Feb, 1 Mar), Memory's closure
    aisle = X.find(X.AMR, "located_at", X.AISLE)
    run = X.find(X.AMR_RUN, "recorded_by")
    packet = ask(diff(AMR, 1, 2))
    assert changes(packet) == {
        ("located_at", Change.SUPERSEDED, (dock.id,), tuple(sorted((closure.id, aisle.id)))),
        ("recorded_by", Change.OPENED, (), (run.id,)),
    }
    text = render_markdown(packet)
    assert "narrowed to" in text and "replaced by" in text


def test_no_change_is_an_empty_list_not_a_gap() -> None:
    packet = ask(diff(AMR, 2, 4))
    assert trail(packet).changes == ()
    assert "No claim about it changed." in render_markdown(packet)


def test_declared_identities_are_compared_too() -> None:
    uav = Subject("machine", "asset-tag:UAV-8", same_as_depth=1)
    packet = ask(diff(uav, Instant(UTC_NS, X.FEB_1 - 1), Instant(UTC_NS, X.FEB_1)))
    found = trail(packet)
    assert X.UAV_SERIAL in found.nodes and X.UAV in found.nodes
    assert found.changes == ()  # the identity and name hold from 1 Jan
    later = ask(diff(uav, Instant(UTC_NS, X.FEB_1), Instant(UTC_NS, X.MAR_1)))
    predicates = {c.predicate for c in trail(later).changes}
    assert predicates == {"located_at"}  # the field from 1 March (the inference was overridden)


def test_instants_on_two_clocks_are_never_compared() -> None:
    truck = Subject("machine", "vin:av-2")
    vehicle = DomainClock(X.TRUCK_CLOCK)
    bridge = ClockBridge(str(X.rec("mapping:av-2 to utc")), vehicle, UTC_NS)
    query = diff(
        truck, Instant(UTC_NS, X.MAR_1), Instant(vehicle, 2000), clock_bridges=frozenset({bridge})
    )
    packet = ask(query)
    assert packet.trails == ()
    (gap,) = [g for g in packet.gaps if g.at == "/explain/0"]
    assert gap.code is GapCode.NOT_COVERED and "two clocks" in gap.detail


def test_claims_on_another_clock_are_named_not_compared() -> None:
    truck = Subject("machine", "vin:av-2")
    depot = X.find(X.TRUCK, "located_at", X.DEPOT)
    packet = ask(diff(truck, Instant(UTC_NS, X.FEB_1), Instant(UTC_NS, X.APR_1)))
    yard = X.find(X.TRUCK, "located_at", X.YARD)
    assert changes(packet) == {("located_at", Change.OPENED, (), (yard.id,))}
    (other,) = [g for g in packet.gaps if g.code is GapCode.OTHER_CLOCK]
    assert other.refs == (depot.id,)


def test_a_diff_past_memorys_snapshot_is_refused_at_its_after() -> None:
    # At "head" the query cannot know the head; the engine resolves it (4) and refuses 5.
    packet = ask(diff(AMR, 1, 5))
    assert packet.trails == ()
    (gap,) = [g for g in packet.gaps if g.at == "/explain/0/after"]
    assert gap.code is GapCode.NOT_COVERED and "transaction 4" in gap.detail


def test_a_subject_memory_never_heard_of_is_a_gap_with_an_empty_trail() -> None:
    packet = ask(diff(Subject("machine", "asset-tag:NOPE-0"), 1, 2))
    assert trail(packet).changes == ()
    (gap,) = [g for g in packet.gaps if g.at == "/explain/0/subject"]
    assert gap.code is GapCode.NOT_COVERED


def test_inferred_changes_are_withheld_when_inference_is_excluded() -> None:
    uav = Subject("machine", "asset-tag:UAV-8")
    candidate = X.find(X.UAV, "same_as_candidate")
    packet = ask(diff(uav, 1, 2, include_inferred=False))
    assert all(candidate.id not in (*c.after, *c.before) for c in trail(packet).changes)
    (gap,) = [g for g in packet.gaps if g.code is GapCode.INFERRED_WITHHELD]
    assert candidate.id in gap.refs and gap.at == "/explain/0"


def test_a_diff_with_a_window_keeps_other_clocks_out_of_the_items() -> None:
    truck = Subject("machine", "vin:av-2")
    query = diff(truck, 1, 2, during=During(UTC_NS, X.JAN_1, None))
    packet = ask(query)  # answer_problems: no item on the vehicle clock
    depot = X.find(X.TRUCK, "located_at", X.DEPOT)
    assert depot.id not in packet.claim_ids
    assert ("located_at", Change.OPENED, (), (depot.id,)) in changes(packet)


def test_the_diff_cap_lists_what_it_cut() -> None:
    packet = ask(diff(AMR, 1, 2), explain_caps=Caps(diff_claims=1))
    assert len(trail(packet).claims) <= 1
    assert any("at most 1 claims" in g.detail for g in packet.gaps)


def test_the_sdk_and_its_default_budget_answer_a_diff() -> None:
    packet = Client(LocalEngine(reader(), X.Catalog())).diff(AMR, 1, 2, include_inferred=False)
    assert trail(packet).subject == X.AMR


def test_a_diff_is_byte_identical_across_fresh_engines() -> None:
    query = diff(LEG, 1, 3)
    first = ask(query)
    again = Client(LocalEngine(IndexedReader(X.document()), X.Catalog())).query(query)
    assert canonical_bytes(again) == canonical_bytes(first)
    assert render_markdown(again) == render_markdown(first)


def test_a_fact_that_holds_at_both_instants_is_no_change_whichever_claims_carry_it() -> None:
    usv = Subject("machine", "asset-tag:USV-3")
    # One of the three berth claims ends on 1 June; the other two keep the fact.
    packet = ask(diff(usv, Instant(UTC_NS, X.MAR_1), Instant(UTC_NS, X.JUN_1 + 1)))
    assert trail(packet).changes == ()


def test_a_claim_valid_only_between_the_instants_is_listed_not_hidden() -> None:
    closure = X.find(X.AMR, "located_at", X.DOCK)  # the dock, [1 Feb, 1 Mar)
    aisle = X.find(X.AMR, "located_at", X.AISLE)
    packet = ask(diff(AMR, Instant(UTC_NS, X.JAN_1), Instant(UTC_NS, X.APR_1)))
    assert ("located_at", Change.BETWEEN, (), (closure.id,)) in changes(packet)
    assert ("located_at", Change.OPENED, (), (aisle.id,)) in changes(packet)
    text = render_markdown(packet)
    assert "**between**" in text and "held at neither point" in text


def test_a_version_recorded_and_replaced_between_two_transactions_is_listed() -> None:
    hum = Subject("machine", "asset-tag:HUM-1")
    dock = X.find(X.HUM, "located_at", X.DOCK, current=False)  # the open-ended original
    passing = X.find(X.HUM, "located_at", X.AISLE, current=False)  # recorded 2, replaced 3
    packet = ask(diff(hum, 1, 3))
    found = changes(packet)
    (superseded,) = [c for c in found if c[1] is Change.SUPERSEDED]
    assert superseded[2] == (dock.id,) and passing.id not in superseded[3]
    assert ("located_at", Change.BETWEEN, (), (passing.id,)) in found


def test_findings_survive_a_clause_that_failed_after_reading_the_same_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from neptune_context.explain import diff as diff_module

    calls = {"n": 0}
    finish = diff_module._Diff.finish

    def flaky(self: diff_module._Diff) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first clause fails late")
        finish(self)

    monkeypatch.setattr(diff_module._Diff, "finish", flaky)
    truck = Subject("machine", "vin:av-2")
    query = Query(
        include_inferred=True,
        budget=Budget(items=40),
        explain=(Diff(truck, 1, 2), Diff(truck, 1, 3)),
    )
    packet = ask(query)
    assert [t.at for t in packet.trails] == ["/explain/1"]
    assert [str(f.code) for f in packet.findings] == ["clock_mismatch"]

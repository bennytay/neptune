"""``why(claim_id)`` (ADR 0010 §3): the provenance tree down to bytes, across embodiments."""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

import pytest
from neptune_memory.schema.reference import ReferenceReader

import explain_fixtures_context as X
from neptune_context.answer import answer_problems
from neptune_context.engine import LocalEngine
from neptune_context.explain import Caps, IndexedReader, render_markdown
from neptune_context.packets.codec import canonical_bytes, decode
from neptune_context.packets.model import ClaimItem, EvidenceItem, EvidenceStatus, GapCode
from neptune_context.packets.trails import Relation, WhyTrail
from neptune_context.query import Budget, Query, Why
from neptune_context.sdk import Client
from neptune_context.sdk.client import why_query

if TYPE_CHECKING:
    from neptune_context.packets.model import ContextPacket, Gap
    from neptune_context.query import AsOf


@cache
def reader() -> IndexedReader:
    return IndexedReader(X.document())


def engine(**kwargs: object) -> LocalEngine:
    return LocalEngine(reader(), X.Catalog(), **kwargs)  # type: ignore[arg-type]


def why(
    claim_id: str,
    *,
    include_inferred: bool = True,
    as_of: AsOf = "head",
    items: int = 50,
    **kwargs: object,
) -> ContextPacket:
    query = why_query(
        claim_id,
        include_inferred=include_inferred,
        as_of=as_of,
        budget=Budget(items=items),
    )
    packet = Client(engine(**kwargs)).query(query)
    assert answer_problems(query, packet) == ()
    assert decode(canonical_bytes(packet)) == packet
    return packet


def trail(packet: ContextPacket) -> WhyTrail:
    (only,) = packet.trails
    assert isinstance(only, WhyTrail)
    return only


def gaps_at(packet: ContextPacket, at: str = "/explain/0") -> list[Gap]:
    return [g for g in packet.gaps if g.at == at]


def test_why_a_drift_claim_reaches_both_calibration_files() -> None:
    drift = X.find(X.WCAM, "drift")
    packet = why(drift.id)
    root = trail(packet).steps[0]
    assert root.relation is Relation.ROOT and root.claim == drift.id
    assert {str(r.source) for r in root.evidence} == {X.CAL_MARCH, X.CAL_APRIL}
    resolved = {str(i.evidence.source): i for i in packet.items if isinstance(i, EvidenceItem)}
    assert set(resolved) == {X.CAL_MARCH, X.CAL_APRIL}
    assert all(i.status is EvidenceStatus.RESOLVED for i in resolved.values())
    # Each evidence item is grounded on the drift's calibration records and consolidator.
    for item in resolved.values():
        assert item.provenance.records == tuple(sorted((X.CAL_MARCH_REC, X.CAL_APRIL_REC)))
        assert item.provenance.transform.producer_id == "fixture.calibration"
    # drift is newer than graph-schema 1.6.0: named and cited, never carried as a claim item.
    assert drift.id not in packet.claim_ids
    (named,) = [g for g in gaps_at(packet) if drift.id in g.refs]
    assert named.code is GapCode.NOT_COVERED and "'drift'" in named.detail
    text = render_markdown(packet)
    assert text.count("neptune://evidence/") >= 2 and "## Why do we believe" in text


def test_why_a_current_calibration_carries_the_claim_its_evidence_and_records() -> None:
    april = X.find(X.WCAM, "has_calibration", X.CAL_B)
    packet = why(april.id)
    assert [s.claim for s in trail(packet).steps] == [april.id]
    (item,) = [i for i in packet.items if isinstance(i, ClaimItem)]
    assert item.claim == april and item.provenance.records == (X.CAL_APRIL_REC,)
    (evidence,) = [i for i in packet.items if isinstance(i, EvidenceItem)]
    assert str(evidence.evidence.source) == X.CAL_APRIL
    assert packet.items[0] is item  # the root ranks first


def test_a_superseded_claim_is_refused_with_the_transaction_that_shows_it() -> None:
    march = X.find(X.WCAM, "has_calibration", X.CAL_A, current=False)
    assert not march.is_current
    packet = why(march.id)
    assert packet.trails == ()
    (gap,) = gaps_at(packet)
    assert gap.code is GapCode.NOT_COVERED and "as_of 1" in gap.detail
    assert march.id in gap.refs and len(gap.refs) >= 2  # the versions that superseded it
    then = why(march.id, as_of=1)
    assert trail(then).steps[0].claim == march.id and march.id in then.claim_ids


def test_a_claim_recorded_after_the_snapshot_is_refused() -> None:
    april = X.find(X.WCAM, "has_calibration", X.CAL_B)
    packet = why(april.id, as_of=1)
    (gap,) = gaps_at(packet)
    assert gap.code is GapCode.NOT_COVERED and "transaction 2" in gap.detail


def test_an_unknown_claim_is_a_structured_refusal_not_an_empty_answer() -> None:
    unknown = "claim:sha256:" + "0" * 64
    packet = why(unknown)
    assert packet.trails == () and packet.items == ()
    (gap,) = gaps_at(packet)
    assert gap.code is GapCode.NOT_COVERED and gap.refs == (unknown,)
    assert "holds no claim" in gap.detail


def test_a_reader_without_claim_history_says_it_cannot_look_up_a_claim() -> None:
    plain = LocalEngine(ReferenceReader(X.document()))
    april = X.find(X.WCAM, "has_calibration", X.CAL_B)
    packet = Client(plain).why(april.id, include_inferred=False)
    (gap,) = gaps_at(packet)
    assert "ClaimHistory" in gap.detail and packet.trails == ()


def test_corroborating_sources_form_a_cycle_shown_once_with_repeats() -> None:
    berth = sorted(
        (c for c in X.document().resolution.claims if c.subject == X.USV),
        key=lambda c: c.id,
    )
    assert len(berth) == 4
    packet = why(berth[0].id)
    steps = trail(packet).steps
    full = [s for s in steps if not s.repeat]
    assert {s.claim for s in full} == {c.id for c in berth}
    assert all(s.relation is Relation.CORROBORATES for s in steps[1:])
    assert any(s.repeat for s in steps)  # the third source closes the cycle
    assert max(s.depth for s in full) == 3  # A, B, C, D in a chain; D names A as a repeat
    assert {c.id for c in berth} <= packet.claim_ids


def test_a_clock_mismatch_is_a_conflict_naming_its_finding() -> None:
    yard = X.find(X.TRUCK, "located_at", X.YARD)
    depot = X.find(X.TRUCK, "located_at", X.DEPOT)
    packet = why(yard.id)
    (conflict,) = trail(packet).steps[1:]
    assert conflict.relation is Relation.CONFLICTS and conflict.claim == depot.id
    (finding,) = packet.findings
    assert conflict.finding == finding.id and str(finding.code) == "clock_mismatch"
    assert depot.id in packet.claim_ids  # current on its own clock; nothing asked for a window


def test_an_overridden_inference_is_named_and_withheld_when_inference_is_excluded() -> None:
    field = X.find(X.UAV, "located_at", X.FIELD)
    overridden = X.find(X.UAV, "located_at", X.DEPOT, current=False)
    packet = why(field.id)
    (conflict,) = trail(packet).steps[1:]
    assert conflict.claim == overridden.id and conflict.relation is Relation.CONFLICTS
    assert conflict.is_inferred and overridden.id not in packet.claim_ids  # never current
    assert any(overridden.id in g.refs and "not current" in g.detail for g in gaps_at(packet))
    assert "**INFERRED**" in render_markdown(packet)
    strict = why(field.id, include_inferred=False)
    assert [s.claim for s in trail(strict).steps] == [field.id]
    (withheld,) = [g for g in strict.gaps if g.code is GapCode.INFERRED_WITHHELD]
    assert withheld.refs == (overridden.id,) and withheld.at == "/explain/0"


def test_an_inferred_root_is_withheld_or_marked_and_has_a_declared_alternative() -> None:
    candidate = X.find(X.UAV, "same_as_candidate")
    declared = X.find(X.UAV, "same_as")
    strict = why(candidate.id, include_inferred=False)
    assert strict.trails == () and strict.items == ()
    (gap,) = gaps_at(strict)
    assert gap.code is GapCode.INFERRED_WITHHELD and gap.refs == (candidate.id,)
    packet = why(candidate.id)
    steps = trail(packet).steps
    assert steps[0].is_inferred and candidate.id in packet.claim_ids
    assert [(s.relation, s.claim) for s in steps[1:]] == [(Relation.ALTERNATIVE, declared.id)]
    text = render_markdown(packet)
    assert '**INFERRED** by `"fixture-matcher"` `"0.3"`, confidence 0.4' in text


def test_undecided_candidates_are_alternatives_of_each_other() -> None:
    first = X.find(X.HUM_RUN, "configuration_candidate", X.HUM_C1)
    second = X.find(X.HUM_RUN, "configuration_candidate", X.HUM_C2)
    steps = trail(why(first.id)).steps
    assert [(s.relation, s.claim) for s in steps[1:]] == [(Relation.ALTERNATIVE, second.id)]


def test_caps_cut_the_tree_and_say_what_was_not_followed() -> None:
    berth = sorted(c.id for c in X.document().resolution.claims if c.subject == X.USV)
    shallow = why(berth[0], explain_caps=Caps(depth=0))
    assert [s.claim for s in trail(shallow).steps] == [berth[0]]
    (cut,) = [g for g in gaps_at(shallow) if "stops at depth" in g.detail]
    assert set(cut.refs) == set(berth[1:])
    narrow = why(berth[0], explain_caps=Caps(fan_out=1, depth=1))
    assert len(trail(narrow).steps) == 2
    small = why(berth[0], explain_caps=Caps(steps=1))
    assert len(trail(small).steps) == 1


def test_without_a_ledger_evidence_is_cited_not_resolved() -> None:
    april = X.find(X.WCAM, "has_calibration", X.CAL_B)
    packet = Client(LocalEngine(reader())).why(april.id, include_inferred=False)
    assert not any(isinstance(i, EvidenceItem) for i in packet.items)
    assert trail(packet).steps[0].evidence == april.provenance.evidence
    (gap,) = [g for g in packet.gaps if g.at == "/explain"]
    assert "no Ledger catalog" in gap.detail


def test_an_unresolvable_source_is_an_item_and_a_gap() -> None:
    run = X.find(X.AMR_RUN, "recorded_by")
    packet = why(run.id)
    lost = [i for i in packet.items if isinstance(i, EvidenceItem) and i.evidence.source == X.LOST]
    assert [i.status for i in lost] == [EvidenceStatus.UNRESOLVABLE]
    assert any(g.code is GapCode.UNRESOLVABLE and X.LOST in g.refs for g in packet.gaps)


def test_two_whys_in_one_query_get_two_trails_in_clause_order() -> None:
    april = X.find(X.WCAM, "has_calibration", X.CAL_B)
    yard = X.find(X.TRUCK, "located_at", X.YARD)
    query = Query(
        include_inferred=True, budget=Budget(items=40), explain=(Why(yard.id), Why(april.id))
    )
    packet = Client(engine()).query(query)
    assert [(t.at, t.claim) for t in packet.trails] == [  # type: ignore[union-attr]
        ("/explain/0", yard.id),
        ("/explain/1", april.id),
    ]
    assert answer_problems(query, packet) == ()


def test_one_failing_clause_is_a_gap_and_the_other_still_answers() -> None:
    class Flaky(IndexedReader):
        def version(self, claim_id: str):  # type: ignore[no-untyped-def]
            if claim_id.endswith("f" * 8):
                raise RuntimeError("index offline")
            return super().version(claim_id)

    april = X.find(X.WCAM, "has_calibration", X.CAL_B)
    broken = "claim:sha256:" + "f" * 64
    query = Query(
        include_inferred=False, budget=Budget(items=10), explain=(Why(broken), Why(april.id))
    )
    packet = Client(LocalEngine(Flaky(X.document()))).query(query)
    assert [t.at for t in packet.trails] == ["/explain/1"]
    (gap,) = gaps_at(packet)
    assert "index offline" in gap.detail


@pytest.mark.parametrize("subject", ["WCAM", "USV", "TRUCK", "UAV", "HUM_RUN"])
def test_why_is_byte_identical_across_fresh_engines(subject: str) -> None:
    claim = sorted(
        (c for c in X.document().resolution.claims if c.subject == getattr(X, subject)),
        key=lambda c: c.id,
    )[-1]
    first = why(claim.id)
    again = Client(LocalEngine(IndexedReader(X.document()), X.Catalog())).query(
        why_query(claim.id, include_inferred=True, budget=Budget(items=50))
    )
    assert canonical_bytes(again) == canonical_bytes(first)
    assert render_markdown(again) == render_markdown(first)


def test_a_clause_that_fails_partway_leaves_nothing_behind() -> None:
    class Late(IndexedReader):
        def superseded_by(self, claim_id: str):  # type: ignore[no-untyped-def]
            raise RuntimeError("history offline")

    march = X.find(X.WCAM, "has_calibration", X.CAL_A, current=False)
    packet = Client(LocalEngine(Late(X.document()))).why(march.id, include_inferred=False)
    assert packet.items == () and packet.trails == ()
    (gap,) = gaps_at(packet)
    assert "history offline" in gap.detail


def test_the_whole_explainer_failing_is_a_gap_per_clause() -> None:
    broken = engine()

    def explode(request: object) -> object:
        raise RuntimeError("explainer down")

    broken._explainer.explain = explode  # type: ignore[method-assign,assignment]
    april = X.find(X.WCAM, "has_calibration", X.CAL_B)
    packet = Client(broken).why(april.id, include_inferred=False)
    (gap,) = gaps_at(packet)
    assert "explainer down" in gap.detail and packet.trails == ()


def test_claims_the_budget_cuts_from_a_trail_are_named_in_a_gap() -> None:
    berth = sorted(c.id for c in X.document().resolution.claims if c.subject == X.USV)
    packet = why(berth[0], items=2)
    missing = set(berth) - packet.claim_ids
    assert missing and packet.budget.dropped
    (cut,) = [g for g in gaps_at(packet) if "budget cut" in g.detail]
    assert set(cut.refs) == missing


def test_caps_stay_inside_the_trail_contract() -> None:
    for bad in ({"depth": 9}, {"fan_out": 0}, {"steps": 257}, {"diff_claims": 0}, {"depth": True}):
        with pytest.raises(ValueError, match="Caps"):
            Caps(**bad)

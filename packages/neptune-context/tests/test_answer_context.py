"""Does a packet answer its query? Checks only a holder of both halves can run (ADR 0006 §2)."""

from __future__ import annotations

import dataclasses
from fractions import Fraction

import pytest

from context_packet_helpers import with_items
from neptune.model.ids import RecordId
from neptune_context.answer import answer_problems, domain_id
from neptune_context.packets.model import ClaimItem, During, Gap, GapCode, Limits
from neptune_context.query import CivilTime, ClockBridge, DomainClock, Query, query_id
from neptune_context.query import During as QueryDuring
from sdk_testing_context import STEMS, golden_packet, golden_query

OTHER_CLOCK = "rec:sha256:" + "77" * 32


@pytest.mark.parametrize("stem", STEMS)
def test_every_golden_packet_answers_its_golden_query(stem: str) -> None:
    assert answer_problems(golden_query(stem), golden_packet(stem)) == ()


def test_another_querys_packet_is_one_problem_and_nothing_else() -> None:
    assert answer_problems(golden_query("q01"), golden_packet("q02")) == (
        "the packet answers a different query",
    )


def test_a_widened_budget_is_not_an_answer_within_the_callers_budget() -> None:
    query, packet = golden_query("q06"), golden_packet("q06")  # items 2, latency 100 ms
    wide = with_items(
        packet, list(packet.items), limits=Limits(items=1000, latency_ms=100), dropped=0
    )
    assert answer_problems(query, wide) == ("the packet's budget limits are not the query's",)
    no_latency = with_items(
        packet,
        list(packet.items),
        limits=Limits(items=2),
        dropped=packet.budget.dropped,
        exhausted=packet.budget.exhausted,
    )
    assert answer_problems(query, no_latency) == ("the packet's budget limits are not the query's",)


def test_the_window_must_be_the_querys_on_the_querys_clock() -> None:
    query, packet = golden_query("q05"), golden_packet("q05")
    assert packet.during is not None
    for during in (
        None,
        dataclasses.replace(packet.during, start=1),
        dataclasses.replace(packet.during, end=10**9),
        During(RecordId(OTHER_CLOCK), 0, None),
    ):
        changed = dataclasses.replace(packet, during=during)
        assert "the packet's world-time window is not the query's during" in answer_problems(
            query, changed
        )
    unasked = dataclasses.replace(golden_packet("q01"), during=packet.during)
    assert answer_problems(golden_query("q01"), unasked) == (
        "the packet has a world-time window the query did not ask for",
    )


def test_a_claim_on_an_unbridged_clock_is_a_gap_never_an_item() -> None:
    # The mixed-clock attack: the engine moves the window to another clock but keeps the claims.
    query, packet = golden_query("q05"), golden_packet("q05")
    claim_clock = str(
        next(i for i in packet.items if isinstance(i, ClaimItem)).claim.valid.domain_id
    )
    moved = Query(
        include_inferred=query.include_inferred,
        budget=query.budget,
        subjects=query.subjects,
        graph=query.graph,
        during=QueryDuring(DomainClock(OTHER_CLOCK), 0, None),
    )
    answer = dataclasses.replace(
        packet, query_id=query_id(moved), during=During(RecordId(OTHER_CLOCK), 0, None)
    )
    problems = answer_problems(moved, answer)
    assert problems and all(f"is on clock {claim_clock}" in p for p in problems)
    # Bridged, the same claims are allowed: the bridge names the mapping that places them.
    bridged = dataclasses.replace(
        moved,
        clock_bridges=frozenset(
            {
                ClockBridge(
                    "rec:sha256:" + "88" * 32, DomainClock(OTHER_CLOCK), DomainClock(claim_clock)
                )
            }
        ),
    )
    assert answer_problems(bridged, dataclasses.replace(answer, query_id=query_id(bridged))) == ()


def test_a_civil_clock_resolves_to_its_domain_id() -> None:
    utc_ns = CivilTime("utc", "unix", Fraction(1, 1_000_000_000))
    resolved = domain_id(utc_ns)
    assert resolved.startswith("rec:sha256:") and resolved == domain_id(
        CivilTime("utc", "unix", Fraction(2, 2_000_000_000))
    )
    assert domain_id(DomainClock(OTHER_CLOCK)) == OTHER_CLOCK


def test_a_gap_points_into_the_query() -> None:
    query, packet = golden_query("q07"), golden_packet("q07")  # one region, gap at /regions/0
    assert any(g.at == "/regions/0" for g in packet.gaps)
    wrong = Gap(GapCode.NOT_COVERED, "/regions/1", None, (), "no second region was asked for")
    assert answer_problems(query, dataclasses.replace(packet, gaps=(wrong,))) == (
        "a not_covered gap points at '/regions/1', which the query does not have",
    )
    for at in ("", "/budget/items", "/regions/0/frame/frame_id"):
        fine = Gap(GapCode.UNKNOWN, at, None, (), "x")
        assert answer_problems(query, dataclasses.replace(packet, gaps=(fine,))) == ()
    for at in ("/regions/00", "/regions/-1", "/nope", "/regions/0/frame/frame_id/x"):
        bad = Gap(GapCode.UNKNOWN, at, None, (), "x")
        assert answer_problems(query, dataclasses.replace(packet, gaps=(bad,))) != ()

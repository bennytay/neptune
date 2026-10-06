# mypy: disable-error-code="arg-type"
"""Trails in the packet contract (ADR 0010 §1): shape, agreement with items, strict decoding,
the JSON Schema, and the answer check against the query's explain clauses."""

from __future__ import annotations

import copy
import dataclasses
import json
from functools import cache
from typing import TYPE_CHECKING, Any

import pytest
from jsonschema import Draft202012Validator

import explain_fixtures_context as X
from neptune.model.knowledge import AssertionKind
from neptune_context.answer import answer_problems
from neptune_context.engine import LocalEngine
from neptune_context.explain import IndexedReader
from neptune_context.packets.codec import canonical_bytes, decode
from neptune_context.packets.findings import PacketError, PacketFindingCode, PacketRefused
from neptune_context.packets.schema import packet_schema
from neptune_context.packets.trails import (
    MAX_DIFF_CLAIMS,
    MAX_TRAILS,
    MAX_WHY_DEPTH,
    MAX_WHY_STEPS,
    Change,
    DiffChange,
    DiffTrail,
    Relation,
    TxPoint,
    WhyStep,
    WhyTrail,
    WorldPoint,
    trail_index,
)
from neptune_context.query import Budget, Query, Subject, Why
from neptune_context.query.model import Diff
from neptune_context.sdk import Client

if TYPE_CHECKING:
    from neptune_memory.schema.claim import Claim

    from neptune_context.packets.model import ContextPacket

Code = PacketFindingCode
REF = X.ref(X.REGISTER, 1)


def cid(n: int) -> str:
    return f"claim:sha256:{n:064x}"


def step(
    n: int,
    parent: int | None,
    depth: int,
    *,
    relation: Relation | None = None,
    repeat: bool = False,
) -> WhyStep:
    rel = relation or (Relation.ROOT if parent is None else Relation.CORROBORATES)
    finding = "finding:sha256:" + "a" * 64 if rel is Relation.CONFLICTS else None
    return WhyStep(
        cid(n),
        None if parent is None else cid(parent),
        rel,
        depth,
        AssertionKind.STATED,
        (REF,),
        finding,
        repeat,
    )


@cache
def packets() -> tuple[ContextPacket, ContextPacket, Query, Query]:
    engine = LocalEngine(IndexedReader(X.document()), X.Catalog())
    berth = sorted(c.id for c in X.document().resolution.claims if c.subject == X.USV)
    why = Query(include_inferred=True, budget=Budget(items=30), explain=(Why(berth[0]),))
    leg = Subject("machine", "asset-tag:LEG-9")
    diff = Query(include_inferred=True, budget=Budget(items=30), explain=(Diff(leg, 1, 3),))
    client = Client(engine)
    return client.query(why), client.query(diff), why, diff


def document(packet: ContextPacket) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(canonical_bytes(packet))
    return out


def refused(doc: dict[str, Any]) -> PacketRefused:
    out = decode(json.dumps(doc))
    assert isinstance(out, PacketRefused), out
    return out


# --- The model ---------------------------------------------------------------------------------


def test_a_packet_without_trails_writes_no_trails_member() -> None:
    why, *_ = packets()
    bare = dataclasses.replace(why, trails=())
    assert "trails" not in document(bare)
    assert "trails" in document(why)


def test_trails_round_trip_through_the_strict_reader() -> None:
    why, diff, *_ = packets()
    for packet in (why, diff):
        assert decode(canonical_bytes(packet)) == packet


def test_a_why_tree_is_pre_order_with_repeats_only_of_shown_claims() -> None:
    WhyTrail(
        "/explain/0",
        cid(1),
        (step(1, None, 0), step(2, 1, 1), step(3, 2, 2), step(1, 3, 3, repeat=True), step(4, 1, 1)),
    )
    bad_trees = [
        (step(2, None, 0),),  # the root is not the claim asked about
        (step(1, None, 0), step(3, 2, 1)),  # a parent never shown
        (step(1, None, 0), step(2, 1, 2)),  # too deep for its parent
        (step(1, None, 0), step(2, 1, 1), step(3, 2, 2), step(4, 2, 2), step(5, 3, 3)),
        (step(1, None, 0), step(2, 1, 1), step(2, 1, 1)),  # shown twice, not as a repeat
        (step(1, None, 0), step(2, 1, 1, repeat=True)),  # a repeat of nothing shown
        (step(1, None, 0), step(3, 2, 2, repeat=True)),
    ]
    for steps in bad_trees:
        with pytest.raises(PacketError):
            WhyTrail("/explain/0", cid(1), steps)


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (lambda: step(1, 1, 1), Code.BAD_VALUE),  # its own parent
        (lambda: step(1, None, 1), Code.BAD_VALUE),  # a root below depth 0
        (lambda: step(2, 1, 0), Code.BAD_VALUE),  # a non-root at depth 0
        (lambda: step(2, 1, MAX_WHY_DEPTH + 1), Code.BAD_VALUE),
        (
            lambda: dataclasses.replace(step(2, 1, 1), finding="finding:sha256:" + "a" * 64),
            Code.BAD_VALUE,
        ),  # only a conflict names a finding
        (
            lambda: dataclasses.replace(step(2, 1, 1, relation=Relation.CONFLICTS), finding=None),
            Code.BAD_VALUE,
        ),
        (lambda: dataclasses.replace(step(1, None, 0), repeat=True), Code.BAD_VALUE),
        (lambda: dataclasses.replace(step(1, None, 0), evidence=()), Code.BAD_VALUE),
        (lambda: dataclasses.replace(step(1, None, 0), evidence=(REF, REF)), Code.DUPLICATE),
        (lambda: dataclasses.replace(step(1, None, 0), assertion_kind="guessed"), Code.BAD_VALUE),
        (lambda: dataclasses.replace(step(1, None, 0), claim="claim:nope"), Code.BAD_VALUE),
    ],
)
def test_a_malformed_why_step_is_refused(make: Any, code: Code) -> None:
    with pytest.raises(PacketError) as raised:
        make()
    assert raised.value.code is code


def test_why_tree_bounds() -> None:
    steps = (step(0, None, 0), *(step(n, 0, 1) for n in range(1, MAX_WHY_STEPS)))
    assert len(WhyTrail("/explain/0", cid(0), steps).steps) == MAX_WHY_STEPS
    with pytest.raises(PacketError):
        WhyTrail("/explain/0", cid(0), (*steps, step(MAX_WHY_STEPS, 0, 1)))
    chain = (step(0, None, 0), *(step(n, n - 1, n) for n in range(1, MAX_WHY_DEPTH + 1)))
    assert WhyTrail("/explain/0", cid(0), chain).steps[-1].depth == MAX_WHY_DEPTH


@pytest.mark.parametrize("at", ["/explain/15", "/explain/0"])
def test_a_trail_points_at_one_of_sixteen_clauses(at: str) -> None:
    assert trail_index(at) in (0, 15)


@pytest.mark.parametrize(
    "at",
    [
        "",
        "/explain",
        "/explain/16",
        "/explain/01",
        "/explain/-1",
        "/subjects/0",
        "/explain/0/after",
    ],
)
def test_a_trail_pointing_elsewhere_is_refused(at: str) -> None:
    with pytest.raises(PacketError):
        WhyTrail(at, cid(1), (step(1, None, 0),))


def test_diff_changes_have_one_shape_each() -> None:
    DiffChange("located_at", Change.OPENED, (), (cid(1),))
    DiffChange("located_at", Change.CLOSED, (cid(1),), ())
    DiffChange("located_at", Change.SUPERSEDED, (cid(1),), (cid(2), cid(3)))
    bad = [
        ("located_at", Change.OPENED, (cid(1),), (cid(2),)),
        ("located_at", Change.OPENED, (), ()),
        ("located_at", Change.CLOSED, (), (cid(1),)),
        ("located_at", Change.SUPERSEDED, (cid(1),), ()),
        ("located_at", Change.CLOSED, (cid(1), cid(2)), ()),
        ("located_at", Change.SUPERSEDED, (cid(1),), (cid(1),)),
        ("located_at", Change.OPENED, (), (cid(2), cid(1))),  # unsorted
        ("Located At", Change.OPENED, (), (cid(1),)),
    ]
    for args in bad:
        with pytest.raises(PacketError):
            DiffChange(*args)


def test_a_diff_trail_compares_two_points_on_one_axis() -> None:
    def make(before: Any, after: Any, changes: tuple[DiffChange, ...] = ()) -> DiffTrail:
        return DiffTrail("/explain/0", X.LEG, (X.LEG,), before, after, changes)

    make(TxPoint(1), TxPoint(2))
    make(WorldPoint(X.UTC, 1), WorldPoint(X.UTC, 2))
    for before, after in [
        (TxPoint(2), TxPoint(2)),
        (TxPoint(3), TxPoint(2)),
        (TxPoint(1), WorldPoint(X.UTC, 2)),
        (WorldPoint(X.UTC, 1), WorldPoint(X.TRUCK_CLOCK, 2)),
    ]:
        with pytest.raises(PacketError):
            make(before, after)
    opened = DiffChange("located_at", Change.OPENED, (), (cid(1),))
    with pytest.raises(PacketError):  # opened twice
        make(TxPoint(1), TxPoint(2), (opened, DiffChange("zone_of", Change.OPENED, (), (cid(1),))))
    with pytest.raises(PacketError):  # unsorted
        make(TxPoint(1), TxPoint(2), (DiffChange("zone_of", Change.OPENED, (), (cid(2),)), opened))
    with pytest.raises(PacketError):  # the subject must be among the nodes
        DiffTrail("/explain/0", X.LEG, (X.AMR,), TxPoint(1), TxPoint(2), ())
    many = tuple(
        DiffChange("located_at", Change.OPENED, (), (cid(n),)) for n in range(MAX_DIFF_CLAIMS + 1)
    )
    with pytest.raises(PacketError):
        make(TxPoint(1), TxPoint(2), many)
    assert len(make(TxPoint(1), TxPoint(2), many[:-1]).changes) == MAX_DIFF_CLAIMS


def test_a_step_must_agree_with_the_claim_item_it_names() -> None:
    why, *_ = packets()
    (trail,) = why.trails
    assert isinstance(trail, WhyTrail)
    root = trail.steps[0]
    wrong = dataclasses.replace(
        root,
        assertion_kind=AssertionKind.OBSERVED
        if root.assertion_kind != AssertionKind.OBSERVED
        else AssertionKind.STATED,
    )
    with pytest.raises(PacketError) as raised:
        dataclasses.replace(
            why, trails=(dataclasses.replace(trail, steps=(wrong, *trail.steps[1:])),)
        )
    assert raised.value.code is Code.ASSERTION_MISMATCH


def test_an_inferred_step_in_a_packet_without_inference_is_refused() -> None:
    why, *_ = packets()
    inferred = WhyTrail(
        "/explain/1",
        cid(9),
        (dataclasses.replace(step(9, None, 0), assertion_kind="inferred"),),
    )
    with pytest.raises(PacketError) as raised:
        dataclasses.replace(why, inference_included=False, trails=(*why.trails, inferred))
    assert raised.value.code is Code.INFERENCE_EXCLUDED


def test_a_diff_change_must_agree_with_the_claim_items_it_names() -> None:
    _, diff, *_ = packets()
    (trail,) = diff.trails
    assert isinstance(trail, DiffTrail)
    first = trail.changes[0]
    claim_id = (*first.before, *first.after)[-1]
    assert claim_id in diff.claim_ids
    renamed = dataclasses.replace(first, predicate="zone_of")
    with pytest.raises(PacketError):
        dataclasses.replace(
            diff, trails=(dataclasses.replace(trail, changes=(renamed, *trail.changes[1:])),)
        )


def test_trails_are_one_per_clause_in_clause_order() -> None:
    why, *_ = packets()
    (trail,) = why.trails
    later = dataclasses.replace(trail, at="/explain/3")
    with pytest.raises(PacketError):
        dataclasses.replace(why, trails=(later, trail))
    with pytest.raises(PacketError):
        dataclasses.replace(why, trails=(trail, trail))
    assert MAX_TRAILS == 16


# --- Strict decoding --------------------------------------------------------------------------


def _mutate(packet: ContextPacket, edit: Any) -> dict[str, Any]:
    doc = copy.deepcopy(document(packet))
    edit(doc)
    return doc


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (lambda d: d.__setitem__("trails", []), Code.SHAPE),
        (lambda d: d["trails"][0].__setitem__("kind", "how"), Code.SHAPE),
        (lambda d: d["trails"][0].__setitem__("extra", 1), Code.SHAPE),
        (lambda d: d["trails"][0]["steps"][0].__setitem__("depth", True), Code.SHAPE),
        (lambda d: d["trails"][0]["steps"][0].__setitem__("repeat", "no"), Code.SHAPE),
        (lambda d: d["trails"][0]["steps"][0].__setitem__("relation", "causes"), Code.BAD_VALUE),
        (lambda d: d["trails"][0]["steps"][0].__setitem__("parent", cid(7)), Code.BAD_VALUE),
        (lambda d: d["trails"][0]["steps"][0]["evidence"].append({"source": "x"}), Code.SHAPE),
        (lambda d: d["trails"][0].__setitem__("at", "/explain/9"), None),  # id changes
    ],
)
def test_a_tampered_why_trail_is_refused(edit: Any, code: Code | None) -> None:
    why, *_ = packets()
    out = refused(_mutate(why, edit))
    if code is not None:
        assert out.findings[0].code is code
        assert out.findings[0].at.startswith("/trails") or code is Code.SHAPE


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d["trails"][0]["changes"][0].__setitem__("predicate", "drift"),
        lambda d: d["trails"][0]["changes"][0].__setitem__("change", "vanished"),
        lambda d: d["trails"][0]["before"].__setitem__("tx", "1"),
        lambda d: d["trails"][0].__setitem__("before", {"clock": X.UTC, "ticks": 1}),
        lambda d: d["trails"][0].__setitem__("before", {"tx": 1, "ticks": 1}),
        lambda d: d["trails"][0]["subject"].__setitem__("node_type", "spaceship"),
        lambda d: d["trails"][0].__setitem__("nodes", []),
    ],
)
def test_a_tampered_diff_trail_is_refused(edit: Any) -> None:
    _, diff, *_ = packets()
    refused(_mutate(diff, edit))


def test_a_predicate_beyond_the_pin_is_refused_by_name() -> None:
    _, diff, *_ = packets()
    out = refused(
        _mutate(diff, lambda d: d["trails"][0]["changes"][0].__setitem__("predicate", "drift"))
    )
    assert "pinned graph-schema" in out.findings[0].message


# --- Schema and answer checks -----------------------------------------------------------------


def test_trail_packets_validate_against_the_exported_schema() -> None:
    validator = Draft202012Validator(packet_schema())
    why, diff, *_ = packets()
    for packet in (why, diff):
        validator.validate(document(packet))
    with pytest.raises(Exception):  # noqa: B017
        validator.validate(_mutate(why, lambda d: d.__setitem__("trails", [])))


def test_answer_problems_hold_each_trail_to_its_clause() -> None:
    why, diff, why_query, diff_query = packets()
    assert answer_problems(why_query, why) == ()
    assert answer_problems(diff_query, diff) == ()
    (wtrail,) = why.trails
    (dtrail,) = diff.trails
    assert isinstance(dtrail, DiffTrail) and isinstance(wtrail, WhyTrail)
    other = dataclasses.replace(wtrail, claim=cid(5), steps=(step(5, None, 0),))
    swapped = dataclasses.replace(why, trails=(other,))
    assert any("does not explain" in p for p in answer_problems(why_query, swapped))
    shifted = dataclasses.replace(diff, trails=(dataclasses.replace(dtrail, after=TxPoint(4)),))
    assert any("not the diff" in p for p in answer_problems(diff_query, shifted))
    beyond = dataclasses.replace(why, trails=(dataclasses.replace(other, at="/explain/1"),))
    assert any("does not have" in p for p in answer_problems(why_query, beyond))
    kinds = dataclasses.replace(diff, trails=(dataclasses.replace(wtrail, at="/explain/0"),))
    assert any("not the diff" in p for p in answer_problems(diff_query, kinds))


def test_a_carried_claim_named_by_a_step_keeps_its_own_evidence_order() -> None:
    why, *_ = packets()
    (trail,) = why.trails
    assert isinstance(trail, WhyTrail)
    claims: dict[str, Claim] = {i.claim.id: i.claim for i in why.items if hasattr(i, "claim")}
    for s in trail.steps:
        if s.claim in claims:
            assert s.evidence == claims[s.claim].provenance.evidence

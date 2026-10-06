"""The channel interface and v0 fusion (ADR 0007 §2, §4): ranking, validation, RRF and the cut."""

from __future__ import annotations

from dataclasses import replace

import pytest

import retrieve_fixtures_context as F
from neptune.identity.canonical_json import dumps
from neptune.model.knowledge import NotApplicable, NotCovered
from neptune_context.packets.model import (
    BudgetUse,
    Channel,
    ChannelHit,
    ClaimItem,
    ConfigurationItem,
    Gap,
    GapCode,
    Limit,
    Limits,
    Relevance,
    measure,
)
from neptune_context.retrieve import (
    ChannelAnswer,
    RetrievalChannel,
    Snapshot,
    answer,
    cut,
    fuse,
)
from neptune_context.retrieve.graph import GraphChannel

ANY = Relevance(1.0, (ChannelHit(Channel.GRAPH, 1, 1.0),))


def claim_items(n: int) -> list[ClaimItem]:
    claims = sorted(F.claims(), key=lambda c: c.id)
    return [ClaimItem.of(c, ANY) for c in claims if c.is_current][:n]


def configuration(on: ClaimItem) -> ConfigurationItem:
    return ConfigurationItem(
        assertion_kind=on.assertion_kind,
        confidence=NotApplicable(),
        provenance=on.provenance,
        relevance=ANY,
        record=F.rec("configuration record"),
        record_kind="parameter_set",
        subject=NotCovered(),
        claims=(on.claim.id,),
    )


def test_answer_ranks_by_score_then_id_and_keeps_the_best_score_of_a_repeat() -> None:
    a, b, c = claim_items(3)
    got = answer(Channel.LEXICAL, [(0.5, a), (2.0, b), (0.5, c), (3.0, a)])
    assert [h.id for h in got.hits] == [a.id, b.id, *sorted([c.id])]
    assert [h.relevance.hits for h in got.hits] == [
        (ChannelHit(Channel.LEXICAL, 1, 3.0),),
        (ChannelHit(Channel.LEXICAL, 2, 2.0),),
        (ChannelHit(Channel.LEXICAL, 3, 0.5),),
    ]
    assert [h.relevance.score for h in got.hits] == [3.0, 2.0, 0.5]


def test_answer_sorts_and_deduplicates_gaps() -> None:
    late = Gap(GapCode.UNKNOWN, "/during", Channel.GRAPH, (), "late")
    early = Gap(GapCode.NOT_COVERED, "/text", Channel.GRAPH, (), "early")
    got = answer(Channel.GRAPH, [], gaps=[late, early, late])
    assert got.gaps == (early, late)


def test_a_channel_answer_refuses_what_fusion_could_not_trust() -> None:
    a, b = claim_items(2)
    ranked = answer(Channel.GRAPH, [(2.0, a), (1.0, b)]).hits
    with pytest.raises(ValueError, match="twice"):
        ChannelAnswer(Channel.GRAPH, (ranked[0], ranked[0]))
    with pytest.raises(ValueError, match="this channel's hit"):
        ChannelAnswer(Channel.LEXICAL, ranked)
    with pytest.raises(ValueError, match="rank"):
        ChannelAnswer(Channel.GRAPH, (ranked[1], ranked[0]))
    with pytest.raises(ValueError, match="does not carry"):
        answer(Channel.GRAPH, [(1.0, configuration(a))])
    with pytest.raises(ValueError, match="sort_key"):
        ChannelAnswer(
            Channel.GRAPH,
            gaps=(
                Gap(GapCode.UNKNOWN, "", None, (), "b"),
                Gap(GapCode.AMBIGUOUS, "", None, (), "a"),
            ),
        )
    with pytest.raises(TypeError):
        ChannelAnswer("graph")  # type: ignore[arg-type]


def test_rrf_merges_channels_by_item_and_keeps_every_hit() -> None:
    a, b, c = claim_items(3)
    graph = answer(Channel.GRAPH, [(1.0, a), (0.5, b)])
    lexical = answer(Channel.LEXICAL, [(9.0, b), (1.0, c)])
    fused = fuse([graph, lexical])
    by_id = {i.id: i for i in fused}
    assert by_id[b.id].relevance.hits == (
        ChannelHit(Channel.GRAPH, 2, 0.5),
        ChannelHit(Channel.LEXICAL, 1, 9.0),
    )
    assert by_id[b.id].relevance.score == pytest.approx(1 / 62 + 1 / 61)
    assert fused[0].id == b.id  # found by both


@pytest.mark.parametrize(
    "pair", [(Channel.GRAPH, Channel.LEXICAL), (Channel.VECTOR, Channel.GRAPH)]
)
def test_no_channel_is_privileged_by_construction(pair: tuple[Channel, Channel]) -> None:
    # Package rule 3: swap which channel found which item, or blow one channel's raw scores up a
    # thousandfold, and the fused order mirrors exactly; only ranks count.
    a, b = claim_items(2)
    left, right = pair
    one = fuse([answer(left, [(1.0, a)]), answer(right, [(1.0, b)])])
    two = fuse([answer(left, [(1.0, b)]), answer(right, [(1000.0, a)])])
    assert [i.relevance.score for i in one] == [i.relevance.score for i in two]
    assert {i.id for i in one} == {a.id, b.id}
    assert [i.id for i in one] == sorted([a.id, b.id])  # equal fused scores: id order
    assert fuse([answer(right, [(1.0, b)]), answer(left, [(1.0, a)])]) == one


def test_cut_keeps_everything_at_the_exact_limits() -> None:
    items = fuse(
        [answer(Channel.GRAPH, [(1.0 / (n + 1), i) for n, i in enumerate(claim_items(5))])]
    )
    size, tokens = measure(items)
    whole = cut(items, Limits(5, tokens=tokens, bytes=size))
    assert (len(whole.kept), whole.dropped, whole.exhausted) == (5, 0, ())
    BudgetUse.measured(Limits(5, tokens=tokens, bytes=size), whole.kept)


@pytest.mark.parametrize(
    ("limits", "exhausted"),
    [
        (lambda size, tokens: Limits(4), (Limit.ITEMS,)),
        (lambda size, tokens: Limits(5, bytes=size - 1), (Limit.BYTES,)),
        (lambda size, tokens: Limits(5, tokens=tokens - 1), (Limit.TOKENS,)),
    ],
)
def test_cut_one_below_a_limit_drops_the_last_item_and_says_which(
    limits: object, exhausted: tuple[Limit, ...]
) -> None:
    items = fuse(
        [answer(Channel.GRAPH, [(1.0 / (n + 1), i) for n, i in enumerate(claim_items(5))])]
    )
    size, tokens = measure(items)
    got = cut(items, limits(size, tokens))  # type: ignore[operator]
    assert (len(got.kept), got.dropped, got.exhausted) == (4, 1, exhausted)
    assert got.kept == items[:4]
    use = BudgetUse.measured(limits(size, tokens), got.kept, dropped=1, exhausted=exhausted)  # type: ignore[operator]
    assert use.bytes == len(dumps([i.to_json() for i in got.kept]))


def test_cut_never_leaves_a_configuration_without_its_claim() -> None:
    a, b = claim_items(2)
    config = configuration(b)
    items = fuse([answer(Channel.GRAPH, [(3.0, config), (2.0, a), (1.0, b)])])
    assert [type(i).__name__ for i in items] == ["ConfigurationItem", "ClaimItem", "ClaimItem"]
    got = cut(items, Limits(2))
    assert [i.id for i in got.kept] == [items[1].id]
    assert (got.dropped, got.exhausted) == (2, (Limit.ITEMS,))


def test_a_snapshot_is_ordered() -> None:
    Snapshot(2, 4, 1)  # type: ignore[arg-type]
    for bad in ((3, 2, 1), (2, 4, 3), (-1, 1, 0)):
        with pytest.raises((ValueError, TypeError)):
            Snapshot(*bad)  # type: ignore[arg-type]


def test_the_graph_channel_implements_the_interface() -> None:
    channel = GraphChannel(F.reader())
    assert isinstance(channel, RetrievalChannel)
    assert channel.channel is Channel.GRAPH
    assert replace(claim_items(1)[0], relevance=ANY).id == claim_items(1)[0].id

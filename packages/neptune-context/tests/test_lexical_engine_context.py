"""The lexical channel end to end through ``LocalEngine``, alone and beside the graph channel."""

from __future__ import annotations

from neptune_memory.schema.interval import ledger_tx

from lexical_fixtures_context import (
    ARM,
    FLEET,
    document,
    passage,
    passages,
    world,
)
from neptune_context.engine import LocalEngine
from neptune_context.packets.codec import canonical_bytes
from neptune_context.packets.model import Channel, ClaimItem, DocumentSpanItem, GapCode
from neptune_context.query.model import Budget, Query, Subject, TextChannel, TextClause, TextField
from neptune_context.retrieve.bm25 import Bm25Index
from neptune_context.retrieve.graph import GraphChannel
from neptune_context.retrieve.lexical import LexicalChannel, LexicalCorpus

LEXICAL = frozenset({TextChannel.LEXICAL})


def text_query(
    body: str,
    *,
    inferred: bool = False,
    fields: frozenset[TextField] | None = None,
    subjects: frozenset[Subject] = frozenset(),
) -> Query:
    return Query(
        include_inferred=inferred,
        budget=Budget(items=50),
        subjects=subjects,
        text=TextClause(body, fields or frozenset(TextField), LEXICAL),
    )


def test_text_queries_are_answered_with_cited_items_and_no_missing_channel_gap() -> None:
    w = world()
    packet = LocalEngine(w.reader, channels=[w.channel]).query(text_query("SN-A4471-9"))
    kinds = sorted(type(i).__name__ for i in packet.items)
    assert kinds == ["ClaimItem", "DocumentSpanItem"]
    assert all(i.provenance.evidence for i in packet.items)
    assert all([h.channel for h in i.relevance.hits] == [Channel.LEXICAL] for i in packet.items)
    assert packet.gaps == ()  # "no lexical or vector channel is attached" is gone


def test_without_a_lexical_channel_the_engine_still_says_text_was_not_searched() -> None:
    w = world()
    packet = LocalEngine(w.reader, channels=[GraphChannel(w.reader)]).query(
        text_query("SN-A4471-9")
    )
    assert [g.at for g in packet.gaps] == ["/text"] and packet.items == ()


def test_the_inference_gate_reaches_the_packet() -> None:
    w = world()
    engine = LocalEngine(w.reader, channels=[w.channel])
    off = engine.query(text_query("retrofit"))
    assert off.items == () and [g.code for g in off.gaps] == [GapCode.INFERRED_WITHHELD]
    assert not off.inference_included
    on = engine.query(text_query("retrofit", inferred=True))
    (item,) = on.items
    assert isinstance(item, ClaimItem) and item.claim.subject == FLEET and on.inference_included


def test_graph_and_lexical_hits_fuse_by_item_and_keep_both_channels() -> None:
    w = world()
    engine = LocalEngine(w.reader, channels=[GraphChannel(w.reader), w.channel])
    query = text_query(
        '"pad replaced"',
        fields=frozenset({TextField.CLAIM_TEXT}),
        subjects=frozenset({Subject("asset", ARM.node_id)}),
    )
    packet = engine.query(query)
    (item,) = [
        i for i in packet.items if isinstance(i, ClaimItem) and "replaced" in str(i.claim.object)
    ]
    assert {h.channel for h in item.relevance.hits} == {Channel.GRAPH, Channel.LEXICAL}
    assert all(g.channel is not Channel.LEXICAL or g.at == "/text" for g in packet.gaps)
    assert packet.items[0].relevance.score >= packet.items[-1].relevance.score


def test_packets_are_byte_identical_across_engines_and_rebuilt_corpora() -> None:
    query = text_query('"lock out the breaker" thruster SN-A4471-9', inferred=True)
    first = LocalEngine(world().reader, channels=[world().channel]).query(query)
    second = LocalEngine(world().reader, channels=[world().channel]).query(query)
    assert canonical_bytes(first) == canonical_bytes(second)
    assert any(isinstance(i, DocumentSpanItem) for i in first.items)


def test_the_channel_config_decides_produced_by() -> None:
    w = world()
    config = w.channel.config
    assert config["channel"] == "lexical" and config["tenant"] == "default"
    index = config["index"]
    assert isinstance(index, dict) and index["analyzer"] == "english"
    assert config == world().channel.config  # complete and deterministic

    def config_hash(channel: LexicalChannel) -> str:
        return LocalEngine(w.reader, channels=[channel]).produced_by.config_hash

    base = config_hash(w.channel)
    assert base == config_hash(world().channel)

    verbatim = LexicalCorpus(Bm25Index({"default": "verbatim"}))
    verbatim.add_claims(w.history, through=document().head)
    verbatim.add_passages(passages(), through=ledger_tx(4))
    assert config_hash(LexicalChannel(verbatim, w.reader)) != base  # another analyser

    other_tenant = LexicalCorpus(tenant="acme")
    other_tenant.add_claims(w.history, through=document().head)
    other_tenant.add_passages(passages(), through=ledger_tx(4))
    assert config_hash(LexicalChannel(other_tenant, w.reader)) != base

    grown = world()
    grown.corpus.add_passages((passage(TextField.DOCUMENT, "sop-new", "new text"),), through=4)
    assert config_hash(grown.channel) != base  # the indexed content is part of the config

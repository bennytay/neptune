"""The lexical channel answers a query's text clause with provenance-carrying, BM25-scored items.

The first four tests are the issue's: serial number lookup, topic name lookup, a phrase in an SOP,
and a term that appears only in an inferred summary (returned only when inference is allowed).
"""

from __future__ import annotations

import dataclasses
from typing import Any

from neptune_memory.schema.claim import ValueType
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES, Cardinality, PredicateSpec

from lexical_fixtures_context import (
    ARM,
    CAPTION,
    CHUNK_FINDING,
    FLEET,
    HUMANOID,
    REGISTER,
    REGISTER_OTHER,
    SOP,
    SOP_OTHER,
    TOPICS,
    UAV_BREACH,
    claim,
    document,
    node,
    passage,
    retrieval,
    world,
)
from neptune.identity.canonical_json import dumps
from neptune.model.knowledge import AssertionKind
from neptune_context.packets.model import (
    Channel,
    ClaimItem,
    DocumentSpanItem,
    GapCode,
    Item,
)
from neptune_context.query.model import (
    Budget,
    Query,
    SiteScope,
    Subject,
    TextChannel,
    TextClause,
    TextField,
)
from neptune_context.retrieve.bm25 import Bm25Index, SearchRequest, TextSource
from neptune_context.retrieve.channel import Retrieval, RetrievalChannel, Snapshot, answer
from neptune_context.retrieve.fusion import fuse
from neptune_context.retrieve.lexical import LexicalChannel, LexicalCorpus

D, R, F = TextField.DOCUMENT, TextField.RECORD, TextField.FINDING
CT, ID = TextField.CLAIM_TEXT, TextField.DECLARED_ID


def spans(items: tuple[Item, ...]) -> list[str]:
    return [i.document for i in items if isinstance(i, DocumentSpanItem)]


def claims_of(items: tuple[Item, ...]) -> list[Any]:
    return [i.claim for i in items if isinstance(i, ClaimItem)]


def only(*fields: TextField) -> frozenset[TextField]:
    return frozenset(fields)


# --- The issue's tests ----------------------------------------------------------------------


def test_a_serial_number_is_found_exactly_in_records_and_claims() -> None:
    reply = world().channel.retrieve(retrieval("SN-A4471-9"))
    assert spans(reply.hits) == [REGISTER.document]  # SN-A4471-7 is another unit
    assert [c.subject.node_id for c in claims_of(reply.hits)] == ["event_log:estop-0031"]
    assert reply.gaps == ()
    assert world().channel.retrieve(retrieval("SN-A4471")).hits == ()  # not a whole serial
    family = world().channel.retrieve(retrieval('"SN-A4471"'))  # a quoted phrase is a prefix
    assert set(spans(family.hits)) == {REGISTER.document, REGISTER_OTHER.document}


def test_a_topic_name_is_found_in_a_channel_record_and_in_a_claim() -> None:
    w = world()
    reply = w.channel.retrieve(retrieval("/uav21/imu/data"))
    assert spans(reply.hits) == [TOPICS.document]
    assert [c.subject for c in claims_of(reply.hits)] == [UAV_BREACH]
    camera = w.channel.retrieve(retrieval("/uav21/camera/front/image_raw", fields=only(R)))
    assert spans(camera.hits) == [TOPICS.document] and claims_of(camera.hits) == []


def test_a_phrase_in_an_sop_matches_only_that_sop() -> None:
    channel = world().channel
    quoted = channel.retrieve(retrieval('"lock out the breaker"', fields=only(D)))
    assert spans(quoted.hits) == [SOP.document]
    loose = channel.retrieve(retrieval("lock out the breaker", fields=only(D)))
    assert spans(loose.hits) == [
        SOP.document,
        SOP_OTHER.document,
    ]  # words match; phrase ranks first
    assert channel.retrieve(retrieval('"breaker the lock"', fields=only(D))).hits == ()


def test_a_term_only_in_an_inferred_summary_needs_inference_to_be_returned() -> None:
    w = world()
    (summary,) = [c for c in w.history if c.subject == FLEET]
    withheld = w.channel.retrieve(retrieval("retrofit", inferred=False))
    assert withheld.hits == ()
    (gap,) = withheld.gaps
    assert (gap.code, gap.channel, gap.at, gap.refs) == (
        GapCode.INFERRED_WITHHELD,
        Channel.LEXICAL,
        "/include_inferred",
        (summary.id,),
    )
    allowed = w.channel.retrieve(retrieval("retrofit", inferred=True))
    (item,) = allowed.hits
    assert isinstance(item, ClaimItem) and item.is_inferred and item.claim.id == summary.id
    assert item.provenance.model is not None and allowed.gaps == ()


# --- Provenance, scoring and snapshots --------------------------------------------------------


def test_every_hit_carries_provenance_a_lexical_score_and_its_rank() -> None:
    reply = world().channel.retrieve(
        retrieval("replaced thruster stalled sensor torque lock", inferred=True)
    )
    assert len(reply.hits) >= 4
    for rank, item in enumerate(reply.hits, start=1):
        assert item.provenance.evidence and item.provenance.transform
        assert item.relevance.score > 0
        (hit,) = item.relevance.hits
        assert (hit.channel, hit.rank, hit.score) == (Channel.LEXICAL, rank, item.relevance.score)
    scores = [i.relevance.score for i in reply.hits]
    assert scores == sorted(scores, reverse=True)


def test_document_spans_cite_their_own_anchor_and_carry_the_text_as_extracted() -> None:
    reply = world().channel.retrieve(retrieval('"lock out the breaker"', fields=only(D)))
    (item,) = reply.hits
    assert isinstance(item, DocumentSpanItem)
    assert item.evidence == SOP.evidence and item.evidence in item.provenance.evidence
    assert item.text.value == SOP.text  # type: ignore[union-attr]
    assert item.assertion_kind is AssertionKind.STATED and not item.is_inferred


def test_claims_are_presented_as_known_at_the_snapshot() -> None:
    channel = world().channel
    (now,) = claims_of(channel.retrieve(retrieval('"pad replaced"')).hits)
    assert now.subject == ARM and now.object.value == "gripper pad replaced and calibrated"
    (then,) = claims_of(channel.retrieve(retrieval("worn", as_of=2)).hits)
    assert then.object.value.startswith("gripper pad worn")
    assert then.is_current  # known as current then; later supersession is reported separately
    assert channel.retrieve(retrieval("worn", as_of=4)).hits == ()
    assert channel.retrieve(retrieval("worn", as_of=0)).hits == ()  # not yet recorded


def test_a_supersession_after_the_snapshot_is_reported_with_its_successor() -> None:
    w = world()
    at_three = Retrieval(
        retrieval("worn").query, Snapshot(ledger_tx(3), ledger_tx(4), ledger_tx(3))
    )
    reply = w.channel.retrieve(at_three)
    (item,) = reply.hits
    (successor,) = w.reader.claims(ARM, "maintenance_state", ledger_tx(4)).claims
    (superseded,) = reply.superseded
    assert isinstance(item, ClaimItem)
    assert (superseded.claim, superseded.superseded_at, superseded.by) == (
        item.claim.id,
        4,
        (successor.id,),
    )
    assert w.channel.retrieve(retrieval("replaced")).superseded == ()


def test_resolver_findings_that_name_a_hit_travel_with_it() -> None:
    winner = claim(ARM, "maintenance_state", "gripper pad replaced", tx=1, label="a")
    guess = claim(ARM, "maintenance_state", "gripper pad worn", tx=2, kind="inferred", label="b")
    w = world(doc=document((winner, guess), head=2), with_passages=False)
    reply = w.channel.retrieve(retrieval("gripper pad", inferred=True, head=2, fields=only(CT)))
    expected = w.reader.claims(ARM, "maintenance_state", ledger_tx(2)).findings
    assert expected and [f.id for f in reply.findings] == [f.id for f in expected]
    assert [c.id for c in claims_of(reply.hits)] == [winner.id]  # the guess lost on arrival
    plain = w.channel.retrieve(retrieval("gripper pad", head=2, fields=only(CT)))
    kept = w.reader.claims(ARM, "maintenance_state", ledger_tx(2), include_inferred=False)
    assert [f.id for f in plain.findings] == [f.id for f in kept.findings]  # the reader decides


def test_inferred_passages_follow_the_same_gate_and_name_their_record() -> None:
    channel = world().channel
    off = channel.retrieve(retrieval("cobalt-blue"))
    (gap,) = off.gaps
    assert off.hits == ()
    assert (gap.code, gap.refs) == (GapCode.INFERRED_WITHHELD, (CAPTION.document,))
    (item,) = channel.retrieve(retrieval("cobalt-blue", inferred=True)).hits
    assert isinstance(item, DocumentSpanItem) and item.is_inferred


def test_ingest_findings_are_searchable_by_code_and_message() -> None:
    reply = world().channel.retrieve(retrieval("truncated_chunk joint_states", fields=only(F)))
    assert spans(reply.hits) == [CHUNK_FINDING.document]


def test_a_passage_registered_after_the_snapshot_is_invisible_until_then() -> None:
    channel = world().channel
    assert channel.retrieve(retrieval("lanyard")).hits == ()
    later = Retrieval(
        retrieval("lanyard").query, Snapshot(ledger_tx(9), ledger_tx(9), ledger_tx(4))
    )
    assert len(channel.retrieve(later).hits) == 1


# --- Field scoping ------------------------------------------------------------------------------


def test_fields_scope_what_is_searched() -> None:
    channel = world().channel
    assert claims_of(channel.retrieve(retrieval("thruster stall", fields=only(CT))).hits)
    assert channel.retrieve(retrieval("thruster stall", fields=only(D, R, F))).hits == ()
    docs = channel.retrieve(retrieval("tag out", fields=only(D)))
    assert claims_of(docs.hits) == [] and spans(docs.hits)
    ids = channel.retrieve(retrieval("asset_tag:hx-02", fields=only(ID)))
    assert [c.subject for c in claims_of(ids.hits)] == [HUMANOID] and spans(ids.hits) == []
    assert channel.retrieve(retrieval("asset_tag:hx-02", fields=only(CT, D, R, F))).hits == ()


def test_declared_ids_find_the_claims_current_at_the_snapshot_that_name_them() -> None:
    channel = world().channel
    (now,) = claims_of(channel.retrieve(retrieval("asset_tag:ur10e-04", fields=only(ID))).hits)
    assert now.object.value.endswith("calibrated")
    (then,) = claims_of(
        channel.retrieve(retrieval("asset_tag:ur10e-04", fields=only(ID), as_of=2)).hits
    )
    assert then.object.value.startswith("gripper pad worn")


# --- Missingness is explicit ------------------------------------------------------------------


def test_a_query_without_text_or_without_the_lexical_channel_answers_nothing() -> None:
    channel = world().channel
    vector_only = retrieval("thruster", channels=frozenset({TextChannel.VECTOR}))
    assert channel.retrieve(vector_only) == answer(Channel.LEXICAL, ())
    by_subject = Query(
        include_inferred=True, budget=Budget(items=5), subjects=frozenset({Subject("machine")})
    )
    assert channel.retrieve(Retrieval(by_subject, vector_only.snapshot)) == answer(
        Channel.LEXICAL, ()
    )


def test_text_with_nothing_searchable_is_a_gap_not_an_empty_fact() -> None:
    reply = world().channel.retrieve(retrieval("??? ---"))
    (gap,) = reply.gaps
    assert reply.hits == () and (gap.code, gap.at) == (GapCode.NOT_COVERED, "/text/text")


def test_a_field_with_nothing_indexed_is_a_gap() -> None:
    w = world(with_passages=False)
    reply = w.channel.retrieve(retrieval("lock out", fields=only(D, CT)))
    (gap,) = reply.gaps
    assert (gap.code, gap.at, gap.channel) == (GapCode.NOT_COVERED, "/text/fields", Channel.LEXICAL)
    assert "document" in gap.detail
    empty = LexicalChannel(LexicalCorpus(), w.reader).retrieve(retrieval("lock out"))
    (gap,) = empty.gaps
    assert empty.hits == () and gap.detail.endswith(
        "claim_text, declared_id, document, finding, record"
    )


def test_memory_ahead_of_the_claim_index_is_a_gap() -> None:
    w = world()
    stale = LexicalChannel(w.corpus, w.reader)
    w.corpus.claims_through = 3
    reply = stale.retrieve(retrieval("replaced"))
    (gap,) = reply.gaps
    assert gap.code is GapCode.UNKNOWN and "indexed through transaction 3" in gap.detail


def test_claims_beyond_the_pin_are_named_never_carried() -> None:
    future = PredicateSpec(
        "future_note",
        1,
        frozenset({NodeType.EVENT}),
        frozenset({ValueType.TEXT}),
        Cardinality.MANY,
        "a predicate newer than Context's pin",
    )
    event = node(NodeType.EVENT, "event_log:future-1")
    beyond = claim(event, "future_note", "novel thruster note", tx=1)
    registry = CORE_PREDICATES.extend(future)
    w = world(doc=document((beyond,), head=1, registry=registry), with_passages=False)
    reply = w.channel.retrieve(retrieval("novel", head=1, fields=only(CT)))
    (gap,) = reply.gaps
    assert reply.hits == ()
    assert (gap.code, gap.refs) == (GapCode.NOT_COVERED, (beyond.id,))


# --- Tenants, determinism and the channel contract --------------------------------------------


def test_tenants_never_see_each_others_text() -> None:
    index = Bm25Index()
    w = world()
    ours, theirs = LexicalCorpus(index, tenant="acme"), LexicalCorpus(index, tenant="globex")
    for corpus in (ours, theirs):
        corpus.add_claims(w.history, through=ledger_tx(4))
    secret = passage(D, "globex-sop", "Globex quench line shutdown procedure")
    theirs.add_passages((secret,), through=4)
    acme, globex = LexicalChannel(ours, w.reader), LexicalChannel(theirs, w.reader)
    query = retrieval("quench shutdown", fields=only(D))
    assert acme.retrieve(query).hits == ()
    assert spans(globex.retrieve(query).hits) == [secret.document]
    assert [
        m.key
        for m in index.search(
            "acme", SearchRequest("quench", only(D), {TextSource.LEDGER: 9})
        ).matches
    ] == []


def test_the_channel_satisfies_the_retrieval_protocol_and_is_deterministic() -> None:
    w = world()
    assert isinstance(w.channel, RetrievalChannel) and w.channel.channel is Channel.LEXICAL
    request = retrieval("thruster stalled replaced SN-A4471-9 lock out", inferred=True)
    first = w.channel.retrieve(request)
    again = world().channel.retrieve(request)
    assert first == again
    assert [dumps(i.to_json()) for i in first.hits] == [dumps(i.to_json()) for i in again.hits]
    assert [i.id for i in first.hits] == [i.id for i in again.hits]


def test_a_huge_query_and_unicode_text_are_answered() -> None:
    channel = world().channel
    huge = " ".join(f"term{i}" for i in range(2000)) + " thruster"
    reply = channel.retrieve(retrieval(huge[:1990]))
    assert reply.gaps and reply.gaps[0].detail.startswith("query text beyond the bounds")
    fullwidth = "\uff33\uff34\uff21\uff2c\uff2c\uff25\uff24"
    assert channel.retrieve(retrieval(f"Thrusters {fullwidth}")).hits != ()
    assert channel.retrieve(retrieval("\U0001f916 机械臂")).hits == ()


def test_fusion_does_not_depend_on_the_order_of_channel_answers() -> None:
    w = world()
    lexical = w.channel.retrieve(retrieval("thruster replaced"))
    ids = [i.id for i in lexical.hits]
    other = answer(Channel.GRAPH, [(1.0, lexical.hits[-1])])
    assert [i.id for i in fuse([lexical, other])] == [i.id for i in fuse([other, lexical])]
    assert sorted(i.id for i in fuse([lexical, other])) == sorted(ids)
    fused = {i.id: i for i in fuse([lexical, other])}
    assert {h.channel for h in fused[lexical.hits[-1].id].relevance.hits} == {
        Channel.LEXICAL,
        Channel.GRAPH,
    }


def test_a_custom_backend_plugs_in_behind_the_index_protocol() -> None:
    class Recording(Bm25Index):
        calls = 0

        def search(self, tenant: str, request: Any) -> Any:
            Recording.calls += 1
            return super().search(tenant, request)

    w = world()
    corpus = LexicalCorpus(Recording())
    corpus.add_claims(w.history, through=ledger_tx(4))
    corpus.add_passages((SOP,), through=4)
    reply = LexicalChannel(corpus, w.reader).retrieve(retrieval("replaced"))
    assert reply.hits and Recording.calls == 2  # the search and the withheld-inference probe


# --- Review findings: consistency, cuts, staleness and unapplied clauses ------------------------


def test_a_conflicting_key_leaves_the_corpus_and_the_index_in_agreement() -> None:
    w = world()
    (version,) = [c for c in w.history if c.subject == HUMANOID]
    ended = dataclasses.replace(version, superseded_at=ledger_tx(4))
    findings = w.corpus.add_claims((ended,), through=ledger_tx(4))
    assert [f.code.value for f in findings] == ["conflicting_key"] * 2  # claim_text, declared_id
    still = w.channel.retrieve(retrieval("torque recalibration"))
    assert [c.id for c in claims_of(still.hits)] == [version.id]
    assert w.corpus.superseded({version.id}, 0, 4) == ([], [])  # the first version stands


def test_text_matches_are_not_restricted_by_subjects_time_or_site_and_the_gap_says_so() -> None:
    plain = world().channel.retrieve(retrieval("thruster"))
    assert plain.gaps == ()
    narrowed = Query(
        include_inferred=True,
        budget=Budget(items=10),
        subjects=frozenset({Subject("machine", "asset_tag:hx-02")}),
        site=SiteScope("site_registry:plant-7", frozenset()),
        text=TextClause("thruster", only(CT), frozenset({TextChannel.LEXICAL})),
    )
    reply = world().channel.retrieve(Retrieval(narrowed, retrieval("x").snapshot))
    (gap,) = reply.gaps
    assert (gap.code, gap.at) == (GapCode.NOT_COVERED, "/text")
    assert gap.detail == "text matches are not restricted by: subjects, site"
    unrestricted = world().channel.retrieve(retrieval("thruster", fields=only(CT), inferred=True))
    assert reply.hits == unrestricted.hits


def test_matches_below_the_read_cap_are_a_gap_not_a_silent_cut() -> None:
    w = world(with_passages=False)
    many = [passage(D, f"sop-{i}", f"quench valve procedure {i}") for i in range(130)]
    w.corpus.add_passages(many, through=4)
    reply = w.channel.retrieve(retrieval("quench", fields=only(D), items=1))
    (gap,) = reply.gaps
    assert len(reply.hits) == 100 and (gap.code, gap.at) == (GapCode.NOT_COVERED, "/budget/items")
    assert gap.detail.startswith("30 further match(es) below rank 100")


def test_record_text_indexed_before_the_snapshot_is_unknown_not_complete() -> None:
    w = world()
    stale = LexicalChannel(w.corpus, w.reader)
    w.corpus.passages_through = 2
    reply = stale.retrieve(retrieval("lock out", fields=only(D)))
    (gap,) = reply.gaps
    assert gap.code is GapCode.UNKNOWN and "catalog transaction 2" in gap.detail
    assert spans(reply.hits)  # what was indexed is still answered


def test_a_supersession_whose_successor_is_not_held_is_reported_as_unknown() -> None:
    w = world()
    corpus = LexicalCorpus()
    history = [c for c in w.history if not c.supersedes]  # the successors are not held
    assert len(history) < len(w.history)
    corpus.add_claims(history, through=ledger_tx(4))
    at_three = Retrieval(
        retrieval("worn", fields=only(CT)).query, Snapshot(ledger_tx(3), ledger_tx(4), ledger_tx(3))
    )
    reply = LexicalChannel(corpus, w.reader).retrieve(at_three)
    (gap,) = reply.gaps
    assert reply.superseded == () and gap.code is GapCode.UNKNOWN and gap.at == ""
    assert len(gap.refs) == 1


def test_withheld_inferred_matches_are_counted_beyond_the_named_refs() -> None:
    w = world(with_passages=False)
    extra = [
        claim(FLEET, "has_summary", f"retrofit report {i}", tx=3, kind="inferred", label=f"s{i}")
        for i in range(3)
    ]
    corpus = LexicalCorpus()
    corpus.add_claims(extra, through=ledger_tx(4))
    reply = LexicalChannel(corpus, w.reader).retrieve(retrieval("retrofit", fields=only(CT)))
    (gap,) = reply.gaps
    assert gap.code is GapCode.INFERRED_WITHHELD and gap.detail.startswith("3 inferred match(es)")

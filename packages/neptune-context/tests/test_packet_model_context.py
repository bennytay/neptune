"""The packet model refuses every packet that breaks the contract (ADR 0003 §2-§5).

Each case starts from a valid golden packet and changes one thing, so a failure names exactly
the rule that broke. Boundaries sit next to their violations.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any

import pytest
from neptune_memory.schema.claim import ClaimId, ModelRef
from neptune_memory.schema.interval import LedgerTx

from context_packet_helpers import golden, rescored, with_items
from neptune.model.ids import ContentId
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance
from neptune.model.time import INT64_MAX, INT64_MIN
from neptune_context.packets.codec import canonical_bytes
from neptune_context.packets.findings import PacketError, PacketFindingCode
from neptune_context.packets.model import (
    PACKET_VERSION,
    ArrowHandle,
    BudgetUse,
    Channel,
    ChannelHit,
    ClaimItem,
    ConfigurationItem,
    DocumentSpanItem,
    During,
    EvidenceItem,
    EvidenceStatus,
    FrameItem,
    Gap,
    GapCode,
    ItemProvenance,
    LedgerSnapshot,
    Limit,
    Limits,
    MemorySnapshot,
    Relevance,
    SceneItem,
    SeriesWindowItem,
    Superseded,
    Transform,
)

Code = PacketFindingCode
SOURCE = "sha256:" + "ab" * 32
CLAIM = ClaimId("claim:sha256:" + "cd" * 32)


def refuses(code: PacketFindingCode, build: Any) -> None:
    with pytest.raises(PacketError) as raised:
        build()
    assert raised.value.code is code, raised.value


def relevance(score: float = 0.5) -> Relevance:
    return Relevance(score, (ChannelHit(Channel.CATALOG, 1, 1.0),))


def series(**changes: Any) -> SeriesWindowItem:
    item = next(i for i in golden("q06").items if isinstance(i, SeriesWindowItem))
    return dataclasses.replace(item, **changes)


# --- Header ------------------------------------------------------------------------------------


def test_packet_version_is_one() -> None:
    assert PACKET_VERSION == 1


def test_a_malformed_query_id_is_refused() -> None:
    refuses(Code.BAD_VALUE, lambda: dataclasses.replace(golden("q01"), query_id="query:abc"))


def test_as_of_after_head_is_refused_and_equal_is_fine() -> None:
    packet = golden("q01")
    assert dataclasses.replace(packet, as_of=packet.head).as_of == packet.head
    refuses(Code.NOT_AS_OF, lambda: dataclasses.replace(packet, as_of=LedgerTx(packet.head + 1)))


def test_memory_may_trail_the_packet_but_not_lead_it() -> None:
    packet = golden("q02")  # as_of 3
    later = dataclasses.replace(packet.memory, as_of=LedgerTx(4))
    refuses(Code.NOT_AS_OF, lambda: dataclasses.replace(packet, memory=later))


def test_a_claim_recorded_after_memorys_snapshot_is_refused() -> None:
    packet = golden("q01")  # claims recorded at 1..4
    trailing = dataclasses.replace(packet.memory, as_of=LedgerTx(1))
    refuses(Code.NOT_AS_OF, lambda: dataclasses.replace(packet, memory=trailing))


@pytest.mark.parametrize("version", ["1.6", "v1.6.0", "01.6.0", ""])
def test_catalog_api_version_is_semver(version: str) -> None:
    refuses(Code.BAD_VALUE, lambda: LedgerSnapshot(version))


def test_memory_snapshot_bounds() -> None:
    refuses(Code.BAD_VALUE, lambda: MemorySnapshot(0, golden("q01").memory.generation, LedgerTx(1)))
    refuses(Code.BAD_VALUE, lambda: MemorySnapshot(1, "sha256:xyz", LedgerTx(1)))  # type: ignore[arg-type]
    refuses(
        Code.BAD_VALUE, lambda: MemorySnapshot(1, golden("q01").memory.generation, LedgerTx(-1))
    )


def test_during_is_non_empty_and_may_be_open() -> None:
    domain = golden("q05").during.domain_id  # type: ignore[union-attr]
    assert During(domain, INT64_MIN, INT64_MAX).end == INT64_MAX
    assert During(domain, 5, None).to_json()["end"] == "open"
    refuses(Code.BAD_VALUE, lambda: During(domain, 5, 5))
    refuses(Code.BAD_VALUE, lambda: During(domain, INT64_MAX + 1, None))


# --- Inference ---------------------------------------------------------------------------------


def test_an_inferred_item_in_a_packet_that_excludes_inference_is_refused() -> None:
    packet = golden("q03")
    assert packet.inference_included and any(i.is_inferred for i in packet.items)
    refuses(Code.INFERENCE_EXCLUDED, lambda: dataclasses.replace(packet, inference_included=False))


def test_an_inferred_item_names_its_model_and_a_bounded_confidence() -> None:
    item = series()
    model = ModelRef("golden-model", "1")
    inferred = dataclasses.replace(item.provenance, model=model)
    for confidence in (Known(0.0), Known(1.0), Unknown()):
        dataclasses.replace(
            item, assertion_kind="inferred", confidence=confidence, provenance=inferred
        )
    refuses(
        Code.ASSERTION_MISMATCH,
        lambda: dataclasses.replace(item, assertion_kind="inferred", confidence=Known(0.5)),
    )
    for bad in (Known(1.5), Known(-0.1), Known(1), NotApplicable(), NotCovered()):
        refuses(
            Code.ASSERTION_MISMATCH,
            lambda bad=bad: dataclasses.replace(
                item, assertion_kind="inferred", confidence=bad, provenance=inferred
            ),
        )


def test_deterministic_items_have_no_confidence_and_no_model() -> None:
    item = series()
    refuses(Code.ASSERTION_MISMATCH, lambda: dataclasses.replace(item, confidence=Known(0.9)))
    model = dataclasses.replace(item.provenance, model=ModelRef("m", "1"))
    refuses(Code.ASSERTION_MISMATCH, lambda: dataclasses.replace(item, provenance=model))


def test_an_unknown_assertion_kind_is_refused() -> None:
    refuses(Code.BAD_VALUE, lambda: dataclasses.replace(series(), assertion_kind="guessed"))  # type: ignore[arg-type]


def test_a_claim_items_envelope_must_equal_its_claim() -> None:
    item = next(i for i in golden("q01").items if isinstance(i, ClaimItem))
    stated = (
        AssertionKind.STATED
        if item.assertion_kind != AssertionKind.STATED
        else AssertionKind.OBSERVED
    )
    refuses(Code.ASSERTION_MISMATCH, lambda: dataclasses.replace(item, assertion_kind=stated))
    other = series().provenance
    refuses(Code.ASSERTION_MISMATCH, lambda: dataclasses.replace(item, provenance=other))


def test_a_superseded_claim_cannot_be_presented_as_current() -> None:
    item = next(i for i in golden("q01").items if isinstance(i, ClaimItem))
    old = dataclasses.replace(item.claim, superseded_at=LedgerTx(item.claim.recorded_at + 1))
    refuses(Code.NOT_AS_OF, lambda: ClaimItem.of(old, item.relevance))


def test_source_bytes_are_never_inferred() -> None:
    item = next(i for i in golden("q04").items if isinstance(i, EvidenceItem))
    inferred = dataclasses.replace(item.provenance, model=ModelRef("m", "1"))
    refuses(
        Code.ASSERTION_MISMATCH,
        lambda: dataclasses.replace(
            item, assertion_kind="inferred", confidence=Unknown(), provenance=inferred
        ),
    )


# --- Items ---------------------------------------------------------------------------------------


def test_item_ids_ignore_relevance_and_follow_content() -> None:
    item = series()
    assert rescored(item, 0.001).id == item.id
    assert series(end=item.end + 1).id != item.id


def test_an_evidence_item_cites_one_of_its_own_provenance_refs() -> None:
    item = next(i for i in golden("q04").items if isinstance(i, EvidenceItem))
    foreign = EvidenceRef(ContentId(SOURCE), (ByteRange(0, 1),))
    refuses(Code.BAD_VALUE, lambda: dataclasses.replace(item, evidence=foreign))


def test_an_unresolvable_source_has_no_known_size() -> None:
    item = next(i for i in golden("q04").items if isinstance(i, EvidenceItem))
    assert item.status is EvidenceStatus.UNRESOLVABLE
    refuses(Code.BAD_VALUE, lambda: dataclasses.replace(item, size=Known(10)))
    refuses(
        Code.BAD_VALUE,
        lambda: dataclasses.replace(item, status=EvidenceStatus.RESOLVED, size=Known(-1)),
    )


def test_item_knowledge_fields_inherit_the_items_provenance() -> None:
    item = next(i for i in golden("q10").items if isinstance(i, DocumentSpanItem))
    grounding = Provenance(item.evidence, "rec:sha256:" + "ee" * 32, AssertionKind.STATED)  # type: ignore[arg-type]
    refuses(Code.BAD_VALUE, lambda: dataclasses.replace(item, text=Known("x", grounding)))
    refuses(Code.BAD_VALUE, lambda: dataclasses.replace(item, text=KnownAbsent(grounding)))


def test_series_windows_are_non_empty_int64_tick_ranges() -> None:
    assert series(start=INT64_MIN, end=INT64_MAX).end == INT64_MAX
    refuses(Code.BAD_VALUE, lambda: series(start=10, end=10))
    refuses(Code.BAD_VALUE, lambda: series(end=INT64_MAX + 1))
    refuses(Code.BAD_VALUE, lambda: series(start=True))


def test_a_series_arrow_handle_points_at_its_streams_rows() -> None:
    item = series()
    wrong = ArrowHandle(item.arrow.package_id, "series/" + "0" * 64 + ".parquet")
    refuses(Code.BAD_VALUE, lambda: series(arrow=wrong))
    refuses(Code.BAD_VALUE, lambda: ArrowHandle("not-a-content-id", item.arrow.path))  # type: ignore[arg-type]


def test_a_scene_rests_on_something_and_its_claims_are_carried() -> None:
    packet = golden("q07")
    scene = next(i for i in packet.items if isinstance(i, SceneItem))
    refuses(Code.BAD_VALUE, lambda: dataclasses.replace(scene, records=()))
    with_claim = dataclasses.replace(scene, claims=(CLAIM,))
    others = [i for i in packet.items if i is not scene]
    refuses(Code.DANGLING_REFERENCE, lambda: with_items(packet, [*others, with_claim]))


def test_scene_nodes_and_refs_are_sorted_and_unique() -> None:
    scene = next(i for i in golden("q07").items if isinstance(i, SceneItem))
    records = scene.records
    refuses(Code.ORDER, lambda: dataclasses.replace(scene, records=tuple(reversed(records))))
    refuses(Code.DUPLICATE, lambda: dataclasses.replace(scene, records=records + records[:1]))


def test_configuration_record_kind_is_a_token() -> None:
    item = next(i for i in golden("q07").items if isinstance(i, ConfigurationItem))
    refuses(Code.BAD_VALUE, lambda: dataclasses.replace(item, record_kind="Calibration File"))


def test_provenance_cites_evidence_and_sorts_records() -> None:
    p = series().provenance
    refuses(Code.BAD_VALUE, lambda: dataclasses.replace(p, evidence=()))
    refuses(Code.DUPLICATE, lambda: dataclasses.replace(p, evidence=p.evidence + p.evidence))
    two = tuple(sorted(("rec:sha256:" + "11" * 32, "rec:sha256:" + "22" * 32)))
    refuses(Code.ORDER, lambda: dataclasses.replace(p, records=tuple(reversed(two))))  # type: ignore[arg-type]
    refuses(Code.BAD_VALUE, lambda: Transform("Bad Id", "1", p.transform.config_hash))


# --- Relevance and order -----------------------------------------------------------------------


@pytest.mark.parametrize("score", [1, -0.1, math.nan, math.inf])
def test_scores_are_finite_non_negative_floats(score: float) -> None:
    refuses(Code.BAD_VALUE, lambda: relevance(score))
    refuses(Code.BAD_VALUE, lambda: ChannelHit(Channel.GRAPH, 1, score))


def test_relevance_names_each_channel_once_in_order() -> None:
    graph, lexical = ChannelHit(Channel.GRAPH, 1, 1.0), ChannelHit(Channel.LEXICAL, 2, 1.0)
    assert Relevance(0.0, (graph, lexical)).score == 0.0
    refuses(Code.BAD_VALUE, lambda: Relevance(0.1, ()))
    refuses(Code.ORDER, lambda: Relevance(0.1, (lexical, graph)))
    refuses(Code.DUPLICATE, lambda: Relevance(0.1, (graph, graph)))
    refuses(Code.BAD_VALUE, lambda: ChannelHit(Channel.GRAPH, 0, 1.0))


def test_items_are_ordered_by_score_then_id_and_never_repeat() -> None:
    packet = golden("q09")
    items = list(packet.items)
    refuses(Code.ORDER, lambda: dataclasses.replace(packet, items=tuple(reversed(items))))
    refuses(Code.DUPLICATE, lambda: with_items(packet, [*items, items[0]]))


# --- Budget ------------------------------------------------------------------------------------


def test_budget_use_must_match_the_items() -> None:
    packet = golden("q08")
    b = packet.budget
    for changed in (
        dataclasses.replace(b, items=b.items + 1),
        dataclasses.replace(b, bytes=b.bytes - 1),
        dataclasses.replace(b, tokens=b.tokens + 1),
    ):
        refuses(Code.BUDGET, lambda changed=changed: dataclasses.replace(packet, budget=changed))


def test_a_packet_over_its_budget_is_refused_and_at_its_budget_is_fine() -> None:
    packet = golden("q08")
    b = packet.budget
    exact = Limits(items=b.items, tokens=b.tokens, bytes=b.bytes)
    assert with_items(packet, list(packet.items), limits=exact).budget.limits == exact
    for tight in (
        Limits(items=b.items - 1),
        Limits(items=b.items, tokens=b.tokens - 1),
        Limits(items=b.items, bytes=b.bytes - 1),
    ):
        refuses(
            Code.BUDGET, lambda tight=tight: with_items(packet, list(packet.items), limits=tight)
        )


def test_truncation_is_explicit() -> None:
    packet = golden("q06")
    assert packet.budget.dropped == 1 and packet.budget.exhausted == (Limit.ITEMS,)
    b = packet.budget
    refuses(Code.BUDGET, lambda: dataclasses.replace(b, exhausted=()))
    refuses(Code.BUDGET, lambda: dataclasses.replace(b, dropped=0))
    refuses(Code.BUDGET, lambda: dataclasses.replace(b, exhausted=(Limit.ITEMS, Limit.TOKENS)))
    refuses(Code.BUDGET, lambda: dataclasses.replace(b, tokenizer="tiktoken/cl100k"))


def test_limits_are_positive() -> None:
    refuses(Code.BAD_VALUE, lambda: Limits(items=0))
    refuses(Code.BAD_VALUE, lambda: Limits(items=1, latency_ms=0))
    assert Limits(items=1, latency_ms=1).to_json() == {"items": 1, "latency_ms": 1}


def test_budget_measured_counts_canonical_bytes_and_quarter_tokens() -> None:
    packet = golden("q01")
    used = BudgetUse.measured(packet.budget.limits, packet.items)
    assert (used.items, used.tokens) == (len(packet.items), -(-used.bytes // 4))


# --- Sections ----------------------------------------------------------------------------------


def test_superseded_since_names_carried_claims_inside_the_window() -> None:
    packet = golden("q02")  # as_of 3, head 5
    (entry,) = packet.superseded_since
    refuses(
        Code.DANGLING_REFERENCE,
        lambda: dataclasses.replace(
            packet, superseded_since=(dataclasses.replace(entry, claim=CLAIM),)
        ),
    )
    for at in (packet.as_of, packet.head + 1):
        moved = dataclasses.replace(entry, superseded_at=LedgerTx(at))
        refuses(
            Code.NOT_AS_OF,
            lambda moved=moved: dataclasses.replace(packet, superseded_since=(moved,)),
        )
    refuses(Code.BAD_VALUE, lambda: Superseded(entry.claim, entry.superseded_at, (entry.claim,)))
    refuses(Code.BAD_VALUE, lambda: Superseded(entry.claim, entry.superseded_at, ()))


def test_findings_must_be_active_at_the_snapshot_and_name_a_carried_claim() -> None:
    packet = golden("q01")
    finding = packet.findings[0]
    stale = dataclasses.replace(finding, superseded_at=LedgerTx(packet.as_of))
    refuses(Code.NOT_AS_OF, lambda: dataclasses.replace(packet, findings=(stale,)))
    orphan = dataclasses.replace(finding, claim=CLAIM, others=())
    refuses(Code.DANGLING_REFERENCE, lambda: dataclasses.replace(packet, findings=(orphan,)))
    refuses(
        Code.ORDER, lambda: dataclasses.replace(packet, findings=tuple(reversed(packet.findings)))
    )


def test_gaps_are_well_formed_and_ordered() -> None:
    refuses(Code.BAD_VALUE, lambda: Gap(GapCode.UNKNOWN, "subjects/0", None, (), "x"))
    refuses(Code.BAD_VALUE, lambda: Gap(GapCode.UNKNOWN, "", None, (), ""))
    refuses(Code.BAD_VALUE, lambda: Gap(GapCode.UNKNOWN, "", None, (), "x" * 2001))
    refuses(Code.ORDER, lambda: Gap(GapCode.UNKNOWN, "", None, ("b", "a"), "x"))
    packet = golden("q09")
    assert len(packet.gaps) == 2
    refuses(Code.ORDER, lambda: dataclasses.replace(packet, gaps=tuple(reversed(packet.gaps))))


# --- Identity and determinism ------------------------------------------------------------------


def test_packet_id_covers_the_header_and_the_ranking() -> None:
    packet = golden("q01")
    assert dataclasses.replace(packet, head=LedgerTx(packet.head + 1)).id != packet.id
    first, *rest = packet.items
    reranked = with_items(packet, [rescored(first, first.relevance.score + 1.0), *rest])
    assert reranked.id != packet.id
    assert [i.id for i in reranked.items] == [i.id for i in packet.items]


def test_canonical_bytes_are_stable() -> None:
    packet = golden("q07")
    copy = dataclasses.replace(packet)
    assert canonical_bytes(copy) == canonical_bytes(packet)


def test_evidence_refs_are_unique_in_first_mention_order() -> None:
    packet = golden("q01")
    refs = packet.evidence_refs()
    assert len(set(refs)) == len(refs)
    assert refs[0] == packet.items[0].provenance.evidence[0]


def test_an_item_needs_typed_provenance_and_relevance() -> None:
    item = series()
    refuses(Code.SHAPE, lambda: dataclasses.replace(item, provenance="p"))  # type: ignore[arg-type]
    refuses(Code.SHAPE, lambda: dataclasses.replace(item, relevance=0.5))  # type: ignore[arg-type]
    assert isinstance(item.provenance, ItemProvenance)
    assert isinstance(NotApplicable(), NotApplicable)


def test_a_frame_encoding_is_non_empty_text() -> None:
    item = next(i for i in golden("q08").items if isinstance(i, FrameItem))
    refuses(Code.BAD_VALUE, lambda: dataclasses.replace(item, encoding=Known("")))
    refuses(Code.BAD_VALUE, lambda: dataclasses.replace(item, encoding=Known("\ud800")))


def test_an_item_value_without_a_canonical_form_is_refused_at_construction() -> None:
    item = next(i for i in golden("q08").items if isinstance(i, FrameItem))
    ambiguous = Ambiguous((Candidate("png"), Candidate("\ud800")))
    refuses(Code.BAD_VALUE, lambda: dataclasses.replace(item, encoding=ambiguous))

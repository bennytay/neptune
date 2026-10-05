"""The graph channel through the local engine (ADR 0007 §3, §5), over the two-site fixture graph.

Every answer goes through the SDK ``Client``, so each packet is also checked against its query
(``answer_problems``) exactly as a consumer would check it.
"""

from __future__ import annotations

from fractions import Fraction

import pytest
from neptune_ledger.api import CatalogFinding, FrameWindow, Resolution, TimeWindow

import retrieve_fixtures_context as F
from neptune.model.knowledge import Known
from neptune_context.answer import answer_problems
from neptune_context.engine import ENGINE_ID, LocalEngine
from neptune_context.packets.codec import canonical_bytes, decode
from neptune_context.packets.conformance import check
from neptune_context.packets.model import (
    ClaimItem,
    ContextPacket,
    FrameItem,
    GapCode,
    SeriesWindowItem,
)
from neptune_context.query import (
    Box,
    Budget,
    CivilTime,
    ClockBridge,
    Direction,
    DomainClock,
    During,
    FrameRef,
    FrameRegion,
    GraphClause,
    Query,
    SiteScope,
    Sphere,
    Subject,
    TextChannel,
    TextClause,
    TextField,
    Why,
)
from neptune_context.retrieve import Retrieval, Snapshot
from neptune_context.retrieve.graph import GraphChannel, score
from neptune_context.sdk import Client, ErrorCode, SdkError

UTC = CivilTime("utc", "unix", Fraction(1, 1_000_000_000))
MARCH = During(UTC, F.MAR_1, F.APR_1)
AMR = Subject("machine", "asset-tag:AMR-07")


def ask(query: Query, *, catalog: object = None, reader: object = None) -> ContextPacket:
    engine = LocalEngine(reader or F.reader(), catalog)  # type: ignore[arg-type]
    return Client(engine).query(query)


def q(**members: object) -> Query:
    base: dict[str, object] = {"include_inferred": False, "budget": Budget(items=100)}
    base.update(members)
    return Query(**base)  # type: ignore[arg-type]


def said(packet: ContextPacket) -> set[tuple[str, str, str]]:
    """``(subject id, predicate, object)`` of every claim item."""
    out = set()
    for item in packet.items:
        if isinstance(item, ClaimItem):
            obj = item.claim.object
            text = getattr(obj, "node_id", None) or str(getattr(obj, "value", obj))
            out.add((item.claim.subject.node_id, item.claim.predicate, text))
    return out


def gaps(packet: ContextPacket, code: GapCode) -> list[tuple[str, tuple[str, ...]]]:
    return [(g.at, g.refs) for g in packet.gaps if g.code is code]


def claim_id(predicate: str, value: str) -> str:
    (found,) = [
        c.id
        for c in F.claims()
        if c.predicate == predicate and getattr(c.object, "value", None) == value
    ]
    return found


# --- The issue's three cases --------------------------------------------------------------------


def test_everything_about_amr_07_at_site_b_in_march() -> None:
    packet = ask(
        q(
            subjects=frozenset({Subject("machine", "asset-tag:AMR-07", same_as_depth=1)}),
            site=SiteScope("site-code:S-007"),
            during=MARCH,
            graph=GraphClause(None, 1, Direction.BOTH),
        )
    )
    facts = said(packet)
    run = F.RUN_3.node_id
    assert {
        ("asset-tag:AMR-07", "located_at", "site-code:S-007"),
        ("asset-tag:AMR-07", "has_configuration", "cfg:AMR-07-B"),
        ("asset-tag:AMR-07", "has_configuration", "cfg:AMR-07-C"),
        (run, "recorded_by", "asset-tag:AMR-07"),
        (F.INCIDENT.node_id, "involves", "asset-tag:AMR-07"),
        ("asset-tag:AMR-07", "same_as", "fleet-id:amr-7"),
        ("fleet-id:amr-7", "maintenance_state", "serviced"),  # through the declared identity
    } <= facts
    # Outside March, at the other site, or another robot: not here.
    assert ("asset-tag:AMR-07", "has_configuration", "cfg:AMR-07-A") not in facts
    assert (F.RUN_4.node_id, "recorded_by", "asset-tag:AMR-07") not in facts
    assert not {f for f in facts if f[0] in {"asset-tag:ARM-3A", "asset-tag:LEG-01"}}
    assert not {f for f in facts if f[0] == "asset-tag:AMR-08"}
    assert packet.during is not None and packet.during.domain_id == F.UTC
    assert packet.produced_by.engine_id == ENGINE_ID


def test_a_window_on_an_unmapped_clock_is_refused_with_a_finding() -> None:
    device_reading = claim_id("maintenance_state", "brake check due")
    plain = ask(q(subjects=frozenset({AMR}), during=MARCH))
    assert device_reading not in plain.claim_ids
    assert ("/during", (device_reading,)) in gaps(plain, GapCode.OTHER_CLOCK)

    # A bridge naming a mapping Memory does not hold is refused at that bridge.
    unknown = ClockBridge(F.UNKNOWN_MAPPING, DomainClock(F.DEVICE), UTC)
    refused = ask(q(subjects=frozenset({AMR}), during=MARCH, clock_bridges=frozenset({unknown})))
    assert device_reading not in refused.claim_ids
    assert [at for at, _ in gaps(refused, GapCode.UNKNOWN)] == ["/clock_bridges/0"]
    assert ("/during", (device_reading,)) in gaps(refused, GapCode.OTHER_CLOCK)

    # The bridge Memory holds places the reading, and the mapping it went through is shown.
    bridge = ClockBridge(F.MAPPING, DomainClock(F.DEVICE), UTC)
    placed = ask(q(subjects=frozenset({AMR}), during=MARCH, clock_bridges=frozenset({bridge})))
    assert device_reading in placed.claim_ids
    assert any(i.claim.predicate == "clock_map" for i in placed.items if isinstance(i, ClaimItem))
    assert not gaps(placed, GapCode.OTHER_CLOCK)


def test_a_bridged_reading_outside_the_window_stays_out() -> None:
    bridge = ClockBridge(F.MAPPING, DomainClock(F.DEVICE), UTC)
    february = During(UTC, F.FEB_1, F.MAR_1)  # the reading starts 100 ns after 1 March
    packet = ask(q(subjects=frozenset({AMR}), during=february, clock_bridges=frozenset({bridge})))
    assert claim_id("maintenance_state", "brake check due") not in packet.claim_ids
    assert not any(
        i.claim.predicate == "clock_map" for i in packet.items if isinstance(i, ClaimItem)
    )


def test_same_as_candidate_is_hidden_by_default() -> None:
    deep = ask(
        q(
            include_inferred=True,
            subjects=frozenset({Subject("machine", "asset-tag:AMR-07", same_as_depth=3)}),
            graph=GraphClause(None, 2, Direction.BOTH),
        )
    )
    facts = said(deep)
    assert not {f for f in facts if "asset-tag:AMR-70" in (f[0], f[2])}
    assert ("asset-tag:AMR-07", "same_as", "fleet-id:amr-7") in facts
    # Asked for by name, the candidate edge is shown (inferred, marked) but never widens identity.
    asked = ask(
        q(
            include_inferred=True,
            subjects=frozenset({Subject("machine", "asset-tag:AMR-07", same_as_depth=3)}),
            graph=GraphClause(frozenset({"same_as_candidate"}), 1, Direction.BOTH),
        )
    )
    (candidate,) = [
        i
        for i in asked.items
        if isinstance(i, ClaimItem) and i.claim.predicate == "same_as_candidate"
    ]
    assert candidate.claim.predicate == "same_as_candidate" and candidate.is_inferred
    assert ("asset-tag:AMR-70", "maintenance_state", "scrapped") not in said(asked)


# --- Walk rules -----------------------------------------------------------------------------------


def test_hops_bound_the_walk() -> None:
    one = said(ask(q(subjects=frozenset({AMR}), graph=GraphClause(None, 1, Direction.BOTH))))
    two = said(ask(q(subjects=frozenset({AMR}), graph=GraphClause(None, 2, Direction.BOTH))))
    other_robot = ("asset-tag:AMR-08", "located_at", "site-code:S-007")
    drone = ("asset-tag:UAV-5", "located_at", "site-code:S-007")
    assert other_robot not in one and drone not in one
    assert {other_robot, drone} <= two
    assert one < two


def test_direction_and_the_predicate_allow_list() -> None:
    out = said(ask(q(subjects=frozenset({AMR}), graph=GraphClause(None, 1, Direction.OUT))))
    assert all(subject == "asset-tag:AMR-07" for subject, _, _ in out)
    into = said(ask(q(subjects=frozenset({AMR}), graph=GraphClause(None, 1, Direction.IN))))
    assert into and all(obj == "asset-tag:AMR-07" for _, _, obj in into)
    only = said(
        ask(
            q(
                subjects=frozenset({AMR}),
                graph=GraphClause(frozenset({"located_at"}), 2, Direction.BOTH),
            )
        )
    )
    assert {p for _, p, _ in only} == {"located_at"}
    assert ("asset-tag:UAV-5", "located_at", "site-code:S-007") in only


def test_scores_decay_with_distance_and_weigh_inference() -> None:
    packet = ask(
        q(
            include_inferred=True,
            subjects=frozenset({AMR}),
            graph=GraphClause(None, 2, Direction.BOTH),
        )
    )
    by_fact = {}
    for item in packet.items:
        if isinstance(item, ClaimItem):
            (hit,) = item.relevance.hits
            by_fact[item.claim.predicate, item.claim.subject.node_id] = hit.score
    assert by_fact["located_at", "asset-tag:AMR-07"] == 1.0
    assert by_fact["located_at", "asset-tag:AMR-08"] == 0.5
    assert by_fact["configuration_candidate", F.RUN_3.node_id] == 0.35  # 0.7 confidence, level 2
    c = next(c for c in F.claims() if c.predicate == "configuration_candidate")
    assert score(c, 3) == pytest.approx(0.175)


def test_inferences_are_withheld_by_name_unless_included() -> None:
    candidate = next(c.id for c in F.claims() if c.predicate == "configuration_candidate")
    query = {"subjects": frozenset({AMR}), "graph": GraphClause(None, 2, Direction.BOTH)}
    excluded = ask(q(**query))
    assert candidate not in excluded.claim_ids
    assert gaps(excluded, GapCode.INFERRED_WITHHELD) == [("/include_inferred", (candidate,))]
    included = ask(q(include_inferred=True, **query))
    assert candidate in included.claim_ids
    assert not gaps(included, GapCode.INFERRED_WITHHELD)


def test_a_value_newer_than_the_pin_is_a_gap_never_an_item() -> None:
    drift = claim_id("drift", "lidar yaw 0.4 deg")
    packet = ask(q(subjects=frozenset({AMR})))
    assert drift not in packet.claim_ids
    ((at, refs),) = gaps(packet, GapCode.NOT_COVERED)
    assert (at, refs) == ("", (drift,))
    assert "graph-schema 1.6.0" in packet.gaps[0].detail or any(
        "1.6.0" in g.detail for g in packet.gaps
    )


def test_a_stale_snapshot_lists_what_memory_changed_since() -> None:
    old = claim_id("has_name", "AMR seven")
    new = claim_id("has_name", "AMR-07 Lift")
    stale = ask(q(as_of=2, subjects=frozenset({AMR})))
    assert (stale.as_of, stale.head, stale.memory.as_of) == (2, 4, 2)
    assert old in stale.claim_ids and new not in stale.claim_ids
    ((entry),) = stale.superseded_since
    assert (entry.claim, entry.superseded_at, entry.by) == (old, 3, (new,))
    head = ask(q(subjects=frozenset({AMR})))
    assert new in head.claim_ids and not head.superseded_since


def test_findings_come_with_the_claims_they_name() -> None:
    bridge = ClockBridge(F.MAPPING, DomainClock(F.DEVICE), UTC)
    packet = ask(
        q(
            subjects=frozenset({Subject("machine", "asset-tag:AMR-07", same_as_depth=1)}),
            during=MARCH,
            clock_bridges=frozenset({bridge}),
        )
    )
    ((finding),) = packet.findings
    assert finding.claim == claim_id("maintenance_state", "brake check due")


# --- Seeds ---------------------------------------------------------------------------------


def test_a_site_alone_seeds_the_walk_and_a_subject_elsewhere_is_named() -> None:
    site_only = said(ask(q(site=SiteScope("site-code:PLANT-2", frozenset({"zone-code:CELL-3"})))))
    assert ("asset-tag:ARM-3A", "located_at", "zone-code:CELL-3") in site_only
    assert ("asset-tag:LEG-01", "located_at", "site-code:PLANT-2") in site_only
    elsewhere = ask(
        q(
            subjects=frozenset({Subject("machine", "asset-tag:ARM-3A")}),
            site=SiteScope("site-code:S-007"),
        )
    )
    assert not elsewhere.items
    assert gaps(elsewhere, GapCode.NOT_COVERED) == [("/site", ("asset-tag:ARM-3A",))]


def test_seeds_memory_cannot_start_from_are_gaps() -> None:
    packet = ask(
        q(
            subjects=frozenset(
                {
                    Subject("machine", "asset-tag:NOBODY"),
                    Subject("asset"),
                }
            ),
            site=SiteScope("site-code:NOWHERE", frozenset({"zone-code:NONE"})),
        )
    )
    assert not packet.items
    pointers = sorted(at for at, _ in gaps(packet, GapCode.NOT_COVERED))
    assert pointers == ["/site", "/site/zones/0", "/subjects/0", "/subjects/1"]


def test_a_kind_wide_subject_filters_what_a_site_walk_keeps() -> None:
    packet = ask(
        q(
            subjects=frozenset({Subject("zone")}),
            site=SiteScope("site-code:S-007"),
            graph=GraphClause(None, 1, Direction.BOTH),
        )
    )
    assert said(packet) == {("zone-code:AISLE-3", "zone_of", "site-code:S-007")}


def test_the_walk_stops_at_its_node_limit_and_says_so() -> None:
    reader = F.reader()
    engine = LocalEngine(reader, channels=[GraphChannel(reader, max_nodes=1)])
    packet = Client(engine).query(
        q(subjects=frozenset({AMR}), graph=GraphClause(None, 3, Direction.BOTH))
    )
    assert ("/graph", ()) in gaps(packet, GapCode.NOT_COVERED)


# --- The Ledger's indexes ---------------------------------------------------------------------


def test_the_window_is_pushed_to_the_ledger_and_cited_records_come_back() -> None:
    catalog = F.FakeCatalog()
    packet = ask(
        q(subjects=frozenset({AMR}), during=MARCH, graph=GraphClause(None, 2, Direction.BOTH)),
        catalog=catalog,
    )
    (spec,) = [s for s in catalog.specs if s.window is not None]
    assert spec.window == TimeWindow(F.UTC, F.MAR_1, F.APR_1 - 1)
    assert spec.kinds == ("image", "stream") and spec.as_of == 4 and spec.frame is None
    (window,) = [i for i in packet.items if isinstance(i, SeriesWindowItem)]
    assert (window.stream, window.clock, window.start, window.end) == (
        F.STREAM,
        F.UTC,
        F.MAR_15,
        F.MAR_15 + F.HOUR,
    )
    assert window.arrow.path.startswith("series/") and window.arrow.package_id == F.PACKAGE
    (frame,) = [i for i in packet.items if isinstance(i, FrameItem)]
    assert isinstance(frame.at, Known) and frame.at.value.ticks == F.MAR_15 + F.HOUR // 2
    assert F.OTHER_STREAM not in {getattr(i, "stream", None) for i in packet.items}
    assert window.relevance.hits[0].score == 0.5  # half the run claim that cites its bag


def test_a_window_clips_the_series_to_the_asked_interval() -> None:
    late = During(UTC, F.MAR_15 + F.HOUR // 4, F.APR_1)
    packet = ask(
        q(subjects=frozenset({AMR}), during=late, graph=GraphClause(None, 2, Direction.BOTH)),
        catalog=F.FakeCatalog(),
    )
    (window,) = [i for i in packet.items if isinstance(i, SeriesWindowItem)]
    assert (window.start, window.end) == (F.MAR_15 + F.HOUR // 4, F.MAR_15 + F.HOUR)


def test_regions_go_to_the_frame_index_one_box_each() -> None:
    catalog = F.FakeCatalog()
    graph_id = F.FRAME_GRAPH
    regions = frozenset(
        {
            FrameRegion(FrameRef("map", graph_id), "m", Box((0.0, 0.0, 0.0), (10.0, 5.0, 3.0))),
            FrameRegion(FrameRef("map", graph_id), "m", Sphere((20.0, 0.0, 1.0), 2.0)),
        }
    )
    packet = ask(
        q(subjects=frozenset({AMR}), regions=regions, graph=GraphClause(None, 2, Direction.BOTH)),
        catalog=catalog,
    )
    frames = [spec.frame for spec in catalog.specs if spec.frame is not None]
    assert len(frames) == 2 and all(isinstance(f, FrameWindow) for f in frames)
    assert {(f.low, f.high) for f in frames} == {
        ((0.0, 0.0, 0.0), (10.0, 5.0, 3.0)),
        ((18.0, -2.0, -1.0), (22.0, 2.0, 3.0)),
    }
    assert [type(i) for i in packet.items if not isinstance(i, ClaimItem)] == [FrameItem]


def test_without_a_catalog_windows_and_regions_are_named() -> None:
    region = FrameRegion(FrameRef("map", F.FRAME_GRAPH), "m", Box((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)))
    packet = ask(q(subjects=frozenset({AMR}), during=MARCH, regions=frozenset({region})))
    pointers = [at for at, _ in gaps(packet, GapCode.NOT_COVERED)]
    assert "/during" in pointers and "/regions/0" in pointers


def test_a_bridged_window_is_not_carried_to_the_ledger_and_ledger_findings_are_gaps() -> None:
    bridge = ClockBridge(F.MAPPING, DomainClock(F.DEVICE), UTC)
    finding = CatalogFinding("budget_exceeded", "rows", "cut at 10000 rows")
    catalog = F.FakeCatalog(findings=(finding,))
    packet = ask(
        q(subjects=frozenset({AMR}), during=MARCH, clock_bridges=frozenset({bridge})),
        catalog=catalog,
    )
    details = [g.detail for g in packet.gaps if g.code is GapCode.NOT_COVERED]
    assert any("ADR 0016" in d for d in details)
    assert any("budget_exceeded" in d for d in details)


# --- The engine -----------------------------------------------------------------------------------


def test_the_same_snapshots_and_query_give_byte_identical_packets() -> None:
    query = q(
        include_inferred=True,
        subjects=frozenset({Subject("machine", "asset-tag:AMR-07", same_as_depth=2)}),
        during=MARCH,
        graph=GraphClause(None, 3, Direction.BOTH),
    )
    first = canonical_bytes(ask(query, catalog=F.FakeCatalog()))
    again = canonical_bytes(ask(query, catalog=F.FakeCatalog(), reader=F.reader()))
    assert first == again


def test_every_answer_is_a_valid_packet_for_its_query() -> None:
    queries = [
        q(subjects=frozenset({AMR}), during=MARCH, graph=GraphClause(None, 2, Direction.BOTH)),
        q(include_inferred=True, as_of=1, subjects=frozenset({AMR})),
        q(site=SiteScope("site-code:S-007")),
        q(
            budget=Budget(items=3, tokens=900),
            subjects=frozenset({AMR}),
            graph=GraphClause(None, 2, Direction.BOTH),
        ),
    ]
    for query in queries:
        packet = ask(query, catalog=F.FakeCatalog())
        assert answer_problems(query, packet) == ()
        assert check(canonical_bytes(packet)) == ()
        assert decode(canonical_bytes(packet)) == packet


def test_a_budget_cuts_a_flagged_prefix() -> None:
    packet = ask(
        q(
            budget=Budget(items=3),
            subjects=frozenset({AMR}),
            graph=GraphClause(None, 2, Direction.BOTH),
        )
    )
    assert len(packet.items) == 3
    assert packet.budget.dropped > 0 and [str(e) for e in packet.budget.exhausted] == ["items"]


def test_query_members_no_channel_serves_yet_are_gaps() -> None:
    text = TextClause("brake", frozenset({TextField.CLAIM_TEXT}), frozenset({TextChannel.LEXICAL}))
    packet = ask(
        q(subjects=frozenset({AMR}), text=text, explain=(Why(claim_id("has_name", "AMR-07 Lift")),))
    )
    pointers = [at for at, _ in gaps(packet, GapCode.NOT_COVERED)]
    assert "/text" in pointers and "/explain/0" in pointers


def test_the_engine_refuses_bad_input_with_structured_errors() -> None:
    engine = LocalEngine(F.reader())
    with pytest.raises(SdkError) as refused:
        engine.query(q(budget=Budget(items=0), subjects=frozenset({AMR})))
    assert refused.value.code is ErrorCode.QUERY_REFUSED and refused.value.findings
    with pytest.raises(SdkError) as beyond:
        engine.query(q(as_of=99, subjects=frozenset({AMR})))
    assert beyond.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(SdkError) as twice:
        LocalEngine(F.reader(), channels=[GraphChannel(F.reader()), GraphChannel(F.reader())])
    assert twice.value.code is ErrorCode.INVALID_ARGUMENT


def test_the_boundary_transactions_answer() -> None:
    zero = ask(q(as_of=0, subjects=frozenset({AMR})))
    assert (zero.as_of, zero.items) == (0, ())
    assert gaps(zero, GapCode.NOT_COVERED) == [("/subjects/0", ("asset-tag:AMR-07",))]
    last = ask(q(as_of=4, subjects=frozenset({AMR})))
    assert last.as_of == last.head == 4


def test_memory_trailing_the_ledger_is_read_at_its_own_head() -> None:
    packet = ask(q(subjects=frozenset({AMR})), catalog=F.FakeCatalog(head=6))
    assert (packet.as_of, packet.head, packet.memory.as_of) == (6, 6, 4)
    stale = ask(q(as_of=5, subjects=frozenset({AMR})), catalog=F.FakeCatalog(head=6))
    assert (stale.as_of, stale.head, stale.memory.as_of) == (5, 6, 4)
    with pytest.raises(SdkError) as beyond:
        ask(q(as_of=7, subjects=frozenset({AMR})), catalog=F.FakeCatalog(head=6))
    assert beyond.value.code is ErrorCode.NOT_FOUND


def test_hydrate_is_the_ledgers_resolve() -> None:
    packet = ask(q(subjects=frozenset({AMR})))
    ref = packet.evidence_refs()[0]
    resolution = Client(LocalEngine(F.reader(), F.FakeCatalog())).hydrate(ref, as_of=4)
    assert isinstance(resolution, Resolution)
    with pytest.raises(SdkError) as missing:
        Client(LocalEngine(F.reader())).hydrate(ref, as_of=4)
    assert missing.value.code is ErrorCode.UNAVAILABLE


def test_the_channel_reads_memory_at_the_snapshot_it_is_given() -> None:
    reader = F.reader()
    request = Retrieval(q(subjects=frozenset({AMR})), Snapshot(4, 4, 2))  # type: ignore[arg-type]
    got = GraphChannel(reader).retrieve(request)
    names = {
        getattr(i.claim.object, "value", None)
        for i in got.hits
        if isinstance(i, ClaimItem) and i.claim.predicate == "has_name"
    }
    assert names == {"AMR seven"}
    assert [s.superseded_at for s in got.superseded] == [3]

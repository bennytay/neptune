"""C1 gate (MVL-111): consumer personas drive the query and packet contracts through the SDK and
the MCP server, and the issue's attacks are run, not walked (docs/reviews/c1-stress-test.md).

Each persona asks its questions as typed queries, gets packets from the golden stub (or from an
engine that answers any query coherently), and checks the answer is unambiguous: which snapshot,
which clock, what is inferred, what was cut, what is missing. Each attack is a hostile engine or a
hostile packet document; the contract must refuse it at a boundary (query validation, the packet
reader, or the SDK's answer check), never let it through as data.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from fractions import Fraction
from typing import TYPE_CHECKING, Any

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session
from neptune_memory.schema.interval import LedgerTx

from neptune.identity.canonical_json import dumps
from neptune.model.ids import RecordId
from neptune_context import mcp as neptune_mcp
from neptune_context.answer import domain_id
from neptune_context.mcp import server as mcp_server
from neptune_context.packets.codec import decode
from neptune_context.packets.findings import PacketRefused
from neptune_context.packets.model import (
    BudgetUse,
    ClaimItem,
    ConfigurationItem,
    ContextPacket,
    During,
    EvidenceItem,
    GapCode,
    Limit,
    Limits,
    SceneItem,
    SeriesWindowItem,
)
from neptune_context.query import (
    Budget,
    CivilTime,
    ClockBridge,
    Diff,
    DomainClock,
    GraphClause,
    Instant,
    Query,
    Subject,
    query_id,
)
from neptune_context.query import During as QueryDuring
from neptune_context.render.citations import parse_citations, render_text
from neptune_context.sdk import AsyncClient, Client, ErrorCode, SdkError, StubEngine
from sdk_testing_context import (
    STEMS,
    LyingEngine,
    answering,
    golden_packet,
    golden_query,
    unresolvable,
)

if TYPE_CHECKING:
    from mcp.client.session import ClientSession

    from neptune.model.provenance import EvidenceRef
    from neptune_context.packets.model import Item

UTC_NS = CivilTime("utc", "unix", Fraction(1, 1_000_000_000))
DRONE_RUN = "record:rec:sha256:bbe0a997b0284bd7a1f3982de3dbddf3abcd70485034c4d1af9fe1709da3401b"


class GoldenOrCoherent:
    """The golden stub for the golden questions; any other valid query gets a coherent answer
    (``answering``), as C2's engine would. Remembers every query it was asked."""

    def __init__(self, resolutions: dict[EvidenceRef, Any] | None = None) -> None:
        self.stub = StubEngine.from_packets(
            (golden_packet(s) for s in STEMS), resolutions=resolutions
        )
        self.asked: list[Query] = []

    def query(self, query: Query) -> ContextPacket:
        self.asked.append(query)
        if query_id(query) in self.stub.query_ids:
            return self.stub.query(query)
        return answering(query)

    def hydrate(self, evidence: EvidenceRef, *, as_of: int | None) -> Any:
        return self.stub.hydrate(evidence, as_of=as_of)


def forged(packet: ContextPacket, **fields: Any) -> bytes:
    """The canonical bytes a hostile producer could write: ``packet`` with ``fields`` changed,
    every id and budget count recomputed, but none of the packet-level rules applied."""
    bad = object.__new__(ContextPacket)
    values = {f.name: getattr(packet, f.name) for f in dataclasses.fields(ContextPacket)}
    values.update(fields)
    items: tuple[Item, ...] = tuple(
        sorted(values["items"], key=lambda i: (-i.relevance.score, i.id))
    )
    values["items"] = items
    if "budget" not in fields:
        old = packet.budget
        values["budget"] = BudgetUse.measured(
            old.limits, items, dropped=old.dropped, exhausted=old.exhausted
        )
    for name, value in values.items():
        object.__setattr__(bad, name, value)
    return dumps(bad.to_json())


def refused_with(document: bytes) -> str:
    result = decode(document)
    assert isinstance(result, PacketRefused), "a hostile packet was read as data"
    return str(result.findings[0].code)


def sdk_error(call: Any) -> SdkError:
    with pytest.raises(SdkError) as raised:
        call()
    return raised.value


def lines(packet: ContextPacket) -> list[str]:
    return render_text(packet).split("\n")


# --- Fleet engineer: a night-time fault -----------------------------------------------------------


def test_fleet_engineer_night_window_is_answered_on_the_civil_clock_it_names() -> None:
    night = Query(
        include_inferred=True,
        budget=Budget(items=200, tokens=8000),
        subjects=frozenset({Subject("fleet", "fleet_registry:amr-north")}),
        during=QueryDuring(UTC_NS, 1_789_423_200_000_000_000, 1_789_452_000_000_000_000),
        graph=GraphClause(frozenset({"member_of_fleet", "runs_software"}), 2, "both"),  # type: ignore[arg-type]
    )
    packet = Client(GoldenOrCoherent()).query(night)
    assert packet.during is not None and packet.during.domain_id == domain_id(UTC_NS)
    assert (
        "World time: ticks [1789423200000000000, 1789452000000000000) on clock "
        f"{domain_id(UTC_NS)}." in lines(packet)
    )


def test_fleet_engineer_at_a_stale_snapshot_sees_what_changed_since() -> None:
    client = Client(GoldenOrCoherent())
    stale, now = client.query(golden_query("q02")), client.query(golden_query("q01"))
    (entry,) = stale.superseded_since
    text = lines(stale)
    assert f"Query {stale.query_id}, as of transaction 3 (head 5)." in text
    assert "Changed since transaction 3:" in text
    assert any(line.startswith(f"- {entry.claim} superseded at transaction 4") for line in text)
    assert now.as_of == now.head and not now.superseded_since


def test_attack_a_query_that_mixes_clocks_never_reaches_the_engine() -> None:
    engine = GoldenOrCoherent()
    device = DomainClock("rec:sha256:" + "5a" * 32)
    subject = Subject("machine", "vin:5yj3e1ea7kf317000")
    mixed = Query(
        include_inferred=False,
        budget=Budget(items=10),
        subjects=frozenset({subject}),
        explain=(Diff(subject, Instant(UTC_NS, 1), Instant(device, 2)),),
    )
    error = sdk_error(lambda: Client(engine).query(mixed))
    assert error.code is ErrorCode.QUERY_REFUSED
    assert [str(f.code) for f in error.findings] == ["cross_clock_without_mapping"]
    assert engine.asked == []
    bridge = ClockBridge("rec:sha256:" + "6b" * 32, device, UTC_NS)
    bridged = dataclasses.replace(mixed, clock_bridges=frozenset({bridge}))
    assert Client(engine).query(bridged).query_id == query_id(bridged)


def test_attack_an_engine_that_answers_on_another_clock_is_refused_at_the_sdk() -> None:
    # The engine was asked for the UTC night but hands back claims on the drone log's own clock,
    # with no bridge in the query to place them there.
    q05 = golden_packet("q05")
    night = Query(
        include_inferred=False,
        budget=Budget(items=20),
        subjects=frozenset({Subject("run", DRONE_RUN)}),
        graph=GraphClause(frozenset({"recorded_by"}), 1, "out"),  # type: ignore[arg-type]
        during=QueryDuring(UTC_NS, 0, None),
    )
    utc = During(RecordId(domain_id(UTC_NS)), 0, None)
    lying = dataclasses.replace(q05, query_id=query_id(night), during=utc)
    error = sdk_error(lambda: Client(LyingEngine(lambda q: lying)).query(night))
    assert error.code is ErrorCode.INVALID_RESPONSE
    assert "neither asked for nor bridged" in error.message
    # The honest answer names those claims in an other_clock gap and carries none of them.
    claims = tuple(sorted(i.claim.id for i in q05.items if isinstance(i, ClaimItem)))
    others = tuple(sorted({*claims, *q05.gaps[0].refs}))
    honest = dataclasses.replace(
        lying,
        items=(),
        budget=BudgetUse.measured(q05.budget.limits, ()),
        findings=(),
        gaps=(dataclasses.replace(q05.gaps[0], refs=others),),
    )
    assert Client(LyingEngine(lambda q: honest)).query(night).gaps[0].code is GapCode.OTHER_CLOCK


# --- Safety lead: preparing an audit --------------------------------------------------------------


def test_safety_lead_reads_inferences_marked_and_withheld_ones_named() -> None:
    client = Client(GoldenOrCoherent())
    included = client.query(golden_query("q03"))
    (inferred,) = (i for i in included.items if i.is_inferred)
    model = inferred.provenance.model
    assert model is not None
    assert any(
        "(INFERRED by " in line and model.model_id in line and inferred.id in line
        for line in lines(included)
    )
    excluded = client.query(golden_query("q06"))
    assert not any(i.is_inferred for i in excluded.items)
    assert "Inferred items: excluded." in lines(excluded)
    withheld = next(g for g in excluded.gaps if g.code is GapCode.INFERRED_WITHHELD)
    assert any(line.startswith("- inferred_withheld at ") for line in lines(excluded))
    assert all(ref in render_text(excluded) for ref in withheld.refs)


def test_attack_a_stated_configuration_resting_on_an_inferred_claim_is_refused() -> None:
    q03 = golden_packet("q03")
    (claim,) = (i for i in q03.items if isinstance(i, ClaimItem) and i.is_inferred)
    config = next(i for i in golden_packet("q07").items if isinstance(i, ConfigurationItem))
    disguised = dataclasses.replace(config, claims=(claim.claim.id,))  # stated, over inference
    assert refused_with(forged(q03, items=(*q03.items, disguised))) == "assertion_mismatch"
    scene = next(i for i in golden_packet("q07").items if isinstance(i, SceneItem))
    disguised_scene = dataclasses.replace(scene, claims=(claim.claim.id,))
    assert refused_with(forged(q03, items=(*q03.items, disguised_scene))) == "assertion_mismatch"


def test_attack_a_header_that_includes_inference_but_says_some_was_withheld_is_refused() -> None:
    q06 = golden_packet("q06")
    assert refused_with(forged(q06, inference_included=True)) == "inference_excluded"


# --- VLA policy at 10 Hz inference ----------------------------------------------------------------


def test_vla_policy_gets_a_truncated_packet_that_says_so() -> None:
    packet = Client(GoldenOrCoherent()).query(golden_query("q06"))
    assert packet.inference_included is False
    assert packet.budget.limits == Limits(items=2, latency_ms=100)
    assert (packet.budget.items, packet.budget.dropped) == (2, 1)
    assert packet.budget.exhausted == (Limit.ITEMS,)
    assert "Items: 2 of 3 found; cut by the items budget." in lines(packet)
    window = next(i for i in packet.items if isinstance(i, SeriesWindowItem))
    assert packet.during is not None and window.clock == packet.during.domain_id


def test_attack_a_packet_over_its_budget_is_refused_by_the_reader() -> None:
    q06 = golden_packet("q06")  # two items, items limit 2
    over = BudgetUse.measured(q06.budget.limits, q06.items, dropped=1, exhausted=(Limit.ITEMS,))
    object.__setattr__(over, "limits", Limits(items=1, latency_ms=100))  # BudgetUse refuses it
    assert refused_with(forged(q06, budget=over)) == "budget"


def test_attack_an_engine_that_widens_the_budget_is_refused_at_the_sdk() -> None:
    q06, query = golden_packet("q06"), golden_query("q06")
    wide = Limits(items=1000, latency_ms=100)
    lying = dataclasses.replace(
        q06,
        items=q06.items,
        budget=BudgetUse.measured(wide, q06.items),
        gaps=q06.gaps,
    )
    error = sdk_error(lambda: Client(LyingEngine(lambda q: lying)).query(query))
    assert error.code is ErrorCode.INVALID_RESPONSE
    assert error.message == "the packet's budget limits are not the query's"


# --- Simulator setup for a site -------------------------------------------------------------------


def test_simulator_gets_a_scene_with_nothing_invented() -> None:
    packet = Client(GoldenOrCoherent()).query(golden_query("q07"))
    scene = next(i for i in packet.items if isinstance(i, SceneItem))
    assert scene.nodes == () and scene.claims == ()
    assert any(" no nodes;" in line and scene.id in line for line in lines(packet))
    (gap,) = packet.gaps
    assert (gap.code, gap.at) == (GapCode.NOT_COVERED, "/regions/0")


def test_simulator_maps_a_packet_frame_into_a_query_frame_by_one_key() -> None:
    # ADR 0006 §7: the query's FrameRef says graph_id, the compiler's (in packets) frame_graph_id.
    query = json.loads(dumps(_q07_json()))
    scene = next(i for i in golden_packet("q07").items if isinstance(i, SceneItem))
    region_frame, scene_frame = query["regions"][0]["frame"], scene.frame.to_json()
    assert region_frame == {
        "frame_id": scene_frame["frame_id"],
        "graph_id": scene_frame["frame_graph_id"],
    }


def _q07_json() -> Any:
    from neptune_context.query import to_json

    return to_json(golden_query("q07"))


# --- Training-data curator ------------------------------------------------------------------------


def test_curator_cites_items_and_snapshot_because_a_pinned_replay_is_another_query() -> None:
    query = golden_query("q09")
    packet = Client(GoldenOrCoherent()).query(query)
    pinned = dataclasses.replace(query, as_of=packet.as_of)
    assert query_id(pinned) != packet.query_id  # so the replayed packet has another packet id
    replay = Client(GoldenOrCoherent()).query(pinned)
    assert replay.as_of == packet.as_of
    windows = [i for i in packet.items if isinstance(i, SeriesWindowItem)]
    assert windows and all(w.arrow.path.startswith("series/") for w in windows)
    assert any(g.code is GapCode.NOT_COVERED and g.at == "/graph" for g in packet.gaps)


# --- Auditor --------------------------------------------------------------------------------------


def test_auditor_sees_unresolvable_evidence_and_claims_on_another_clock_named() -> None:
    client = Client(GoldenOrCoherent())
    why = client.query(golden_query("q04"))
    assert any(g.code is GapCode.UNRESOLVABLE for g in why.gaps)
    assert parse_citations(render_text(why)) == why.evidence_refs()
    own_clock = client.query(golden_query("q05"))
    (gap,) = own_clock.gaps
    assert gap.code is GapCode.OTHER_CLOCK and gap.refs
    assert not {i.claim.id for i in own_clock.items if isinstance(i, ClaimItem)} & set(gap.refs)


def test_attack_a_stale_memory_snapshot_is_visible_and_its_supersessions_listed() -> None:
    q02 = golden_packet("q02")  # Memory at 3; a supersession at 4
    (entry,) = q02.superseded_since
    trailing = dataclasses.replace(q02, as_of=LedgerTx(entry.superseded_at))
    assert decode(dumps(trailing.to_json())) == trailing
    text = lines(trailing)
    assert "Claims as Memory knew them at transaction 3 (it trails the Ledger's 4)." in text
    assert "Changed since transaction 3:" in text
    # A supersession Memory had not made by its snapshot cannot be listed as one it had.
    early = dataclasses.replace(entry, superseded_at=LedgerTx(trailing.memory.as_of))
    assert refused_with(forged(trailing, superseded_since=(early,))) == "not_as_of"


def test_attack_a_gap_pointing_outside_the_query_is_refused_at_the_sdk() -> None:
    q07, query = golden_packet("q07"), golden_query("q07")
    stray = dataclasses.replace(q07.gaps[0], at="/site")
    lying = dataclasses.replace(q07, gaps=(stray,))
    error = sdk_error(lambda: Client(LyingEngine(lambda q: lying)).query(query))
    assert "points at '/site', which the query does not have" in error.message


# --- Demo v1: an incident-reconstruction agent over MCP -------------------------------------------


def _session(engine: GoldenOrCoherent, use: Any) -> Any:
    async def go() -> Any:
        server = neptune_mcp.build_server(AsyncClient(engine))
        async with create_connected_server_and_client_session(server) as session:
            return await use(session)

    return asyncio.run(go())


def _text(result: types.CallToolResult) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, types.TextContent))


def test_incident_agent_reconstructs_with_query_why_diff_and_evidence_links() -> None:
    evidence = next(i for i in golden_packet("q04").items if isinstance(i, EvidenceItem))
    engine = GoldenOrCoherent({evidence.evidence: unresolvable(evidence.evidence)})
    q10 = json.loads(dumps(_q10_json()))
    flag = q10.pop("include_inferred")
    claim = "claim:sha256:03ef80551292669e368d326b22bd44b2a3c5a6461298c94469f16ad71110ad4a"
    truck = {"kind": "machine", "declared_id": "vin:5yj3e1ea7kf317000"}
    vehicle = {"kind": "domain", "domain_id": "rec:sha256:" + "5a" * 32}
    utc = {
        "kind": "civil",
        "timescale": "utc",
        "epoch": "unix",
        "resolution": {"numerator": 1, "denominator": 1_000_000_000},
    }

    async def use(session: ClientSession) -> dict[str, Any]:
        out: dict[str, Any] = {}
        out["ask"] = await session.call_tool(
            "neptune_query", {"include_inferred": flag, "query": q10}
        )
        out["why"] = await session.call_tool(
            "neptune_why", {"claim_id": claim, "include_inferred": False}
        )
        link = next(
            b
            for b in out["why"].content
            if isinstance(b, types.ResourceLink)
            and mcp_server.parse_evidence_uri(str(b.uri))[0] == evidence.evidence
        )
        out["source"] = await session.read_resource(link.uri)
        diff = {
            "subject": truck,
            "before": {"clock": utc, "ticks": 1_788_080_400_000_000_000},
            "after": {"clock": vehicle, "ticks": 912_345_678_000},
            "include_inferred": True,
        }
        out["diff"] = await session.call_tool("neptune_diff", diff)
        bridged = {
            "budget": {"items": 100},
            "subjects": [truck],
            "explain": [
                {"kind": "diff", "subject": truck, **{k: diff[k] for k in ("before", "after")}}
            ],
            "clock_bridges": [
                {"mapping_id": "rec:sha256:" + "6b" * 32, "source": vehicle, "target": utc}
            ],
        }
        out["bridged"] = await session.call_tool(
            "neptune_query", {"include_inferred": True, "query": bridged}
        )
        return out

    out = _session(engine, use)
    assert not out["ask"].isError and "[E1]" in _text(out["ask"])
    assert _text(out["why"]).startswith(f"Context packet {golden_packet('q04').id}")
    (content,) = out["source"].contents
    assert json.loads(content.text)["status"] == "unresolvable"
    error = json.loads(_text(out["diff"]))["error"]
    assert out["diff"].isError and error["code"] == "query_refused"
    assert [f["code"] for f in error["findings"]] == ["cross_clock_without_mapping"]
    assert not out["bridged"].isError
    assert engine.asked[-1].clock_bridges and engine.asked[-1].explain


def test_incident_agent_cannot_skip_the_inference_choice() -> None:
    async def use(session: ClientSession) -> types.CallToolResult:
        return await session.call_tool("neptune_query", {"query": json.loads(dumps(_q10_json()))})

    result = _session(GoldenOrCoherent(), use)
    assert result.isError and json.loads(_text(result))["error"]["code"] == "invalid_argument"


def _q10_json() -> Any:
    from neptune_context.query import to_json

    return to_json(golden_query("q10"))


def test_every_golden_answer_is_unambiguous_about_its_scope() -> None:
    # Every persona's packet, rendered, states snapshot, inference policy and item count; a
    # windowed one states its clock.
    for stem in STEMS:
        packet = golden_packet(stem)
        text = lines(packet)
        assert text[1].startswith(f"Query {packet.query_id}, as of transaction {packet.as_of}")
        assert any(line.startswith("Inferred items: ") for line in text)
        assert any(line.startswith(f"Items: {packet.budget.items} of ") for line in text)
        assert (packet.during is not None) == any(line.startswith("World time:") for line in text)

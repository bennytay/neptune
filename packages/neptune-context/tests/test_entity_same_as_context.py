"""Ids memory states are one entity are one name (ADR 0018).

``neptune_entities`` and the planner's resolver list ids joined by evidence ``same_as`` claims
(stated or observed, directly or through each other) as one entity under its least declared id,
naming the others; ids nothing joins stay an ambiguity, and an inferred ``same_as`` never joins.
A query naming any joined id at the advertised ``same_as_depth``, and a comparison of runs
recorded by any of them, read the same claims.
"""

from __future__ import annotations

import asyncio
import dataclasses
import random
import re
from typing import TYPE_CHECKING, Any, Final

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session as connect
from neptune_memory.schema.claim import TypedLiteral, ValueType
from neptune_memory.schema.codec import GraphDocument
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.supersede import Resolution as History

import retrieve_fixtures_context as F
from neptune.model.knowledge import AssertionKind
from neptune_context.engine import read_graph_document
from neptune_context.mcp import build_server, query_from_arguments
from neptune_context.mcp.__main__ import local_client
from neptune_context.packets.model import ClaimItem
from neptune_context.query.plan import DeclaredIdentifierIndex, Entity, SameAs
from neptune_context.render.agent import render_entities, render_mentions
from neptune_context.sdk import entity_index
from neptune_context.sources import SourceIndex

if TYPE_CHECKING:
    from neptune_memory.schema.claim import Claim

    from neptune_context.packets.model import ContextPacket

HUB: Final = NodeRef(NodeType.MACHINE, "cmms:X1")
LEAF: Final = NodeRef(NodeType.MACHINE, "manifest:X1")
FAR: Final = NodeRef(NodeType.MACHINE, "syslog:X1")
TWIN: Final = NodeRef(NodeType.MACHINE, "asset:X1")  # same name, nothing joins it
RUN_A: Final = NodeRef(NodeType.RUN, "record:rec:sha256:" + "a" * 64)
RUN_B: Final = NodeRef(NodeType.RUN, "record:rec:sha256:" + "b" * 64)
WORK: Final = NodeRef(NodeType.EVENT, "record:rec:sha256:" + "c" * 64)


def _text(value: str) -> TypedLiteral:
    return TypedLiteral(ValueType.TEXT, value)


def joined_claims() -> list[Claim]:
    """Three ids of one machine (a hub stating the other two), a fourth id of the same name
    nothing joins, two runs recorded by the leaf id and a work order on the far one."""
    return [
        F.claim(HUB, "same_as", LEAF, F.FEB_1),
        F.claim(HUB, "same_as", FAR, F.FEB_1),
        F.claim(TWIN, "located_at", F.SITE_A, F.FEB_1),
        F.claim(RUN_A, "recorded_by", LEAF, F.MAR_1, F.MAR_1 + F.HOUR),
        F.claim(RUN_A, "has_name", "run-a", F.MAR_1, F.MAR_1 + F.HOUR),
        F.claim(RUN_B, "recorded_by", LEAF, F.MAR_20, F.MAR_20 + F.HOUR),
        F.claim(RUN_B, "has_name", "run-b", F.MAR_20, F.MAR_20 + F.HOUR),
        F.claim(WORK, "involves", FAR, F.MAR_10),
        F.claim(WORK, "event_kind", _text("maintenance"), F.MAR_10),
        F.claim(WORK, "has_name", "WO-1", F.MAR_10),
    ]


def document(claims: list[Claim]) -> GraphDocument:
    ordered = sorted(claims, key=lambda c: (c.recorded_at, c.id))
    return GraphDocument(History(tuple(ordered), ()), F.RESOLVER_CONFIG, F.HEAD)


def test_ids_stated_same_as_are_one_candidate_named_once() -> None:
    index = entity_index(document(joined_claims()))
    (mention,) = index.find("what happened to X1?", as_of=None)
    assert mention.ambiguous  # the joined machine, and an id of the same name nothing joins
    by_id = {c.declared_id: c for c in mention.candidates}
    assert set(by_id) == {"asset:X1", "cmms:X1"}
    joined = by_id["cmms:X1"].same_as
    assert isinstance(joined, SameAs)
    assert [o.declared_id for o in joined.others] == ["manifest:X1", "syslog:X1"]
    assert len(joined.claims) == 2 and joined.depth == 2  # leaf -> hub -> far
    assert by_id["asset:X1"].same_as is None
    listed = [e.declared_id for e in index.entities() if e.kind == "machine"]
    assert listed == ["asset:X1", "cmms:X1"]  # the joined ids are listed under their representative
    for member in ("manifest:X1", "syslog:X1"):  # every id is still declared on its own
        assert index.lookup(member, as_of=None) == Entity("machine", member)


def test_the_representative_is_the_least_id_whichever_way_the_claims_point() -> None:
    claims = joined_claims()
    flipped = [
        F.claim(LEAF, "same_as", HUB, F.FEB_1),
        F.claim(FAR, "same_as", HUB, F.FEB_1),
        *claims[2:],
    ]
    for variant in (claims, flipped):
        (mention,) = entity_index(document(variant)).find("X1", as_of=None)
        assert [c.declared_id for c in mention.candidates] == ["asset:X1", "cmms:X1"]


def test_ids_nothing_joins_stay_ambiguous() -> None:
    claims = [
        F.claim(NodeRef(NodeType.MACHINE, "tag:Y2"), "located_at", F.SITE_A, F.FEB_1),
        F.claim(NodeRef(NodeType.MACHINE, "crm:Y2"), "located_at", F.SITE_B, F.FEB_1),
    ]
    index = entity_index(document(claims))
    (mention,) = index.find("Y2", as_of=None)
    assert mention.ambiguous
    assert [c.declared_id for c in mention.candidates] == ["crm:Y2", "tag:Y2"]
    assert all(c.same_as is None for c in mention.candidates)
    assert "ambiguous between" in render_mentions("Y2", (mention,))


def test_an_inferred_same_as_never_joins_even_with_inferences_included() -> None:
    tag, crm = NodeRef(NodeType.MACHINE, "tag:Y2"), NodeRef(NodeType.MACHINE, "crm:Y2")
    claims = [
        F.claim(tag, "located_at", F.SITE_A, F.FEB_1),
        F.claim(crm, "located_at", F.SITE_B, F.FEB_1),
        F.claim(tag, "same_as", crm, F.FEB_1, inferred=0.97),
    ]
    index = entity_index(document(claims))
    for flag in (False, True):
        (mention,) = index.find("Y2", as_of=None, include_inferred=flag)
        assert mention.ambiguous, flag
        assert all(c.same_as is None for c in mention.candidates)


def test_an_observed_same_as_joins_and_a_superseded_one_does_not() -> None:
    tag, crm = NodeRef(NodeType.MACHINE, "tag:Y2"), NodeRef(NodeType.MACHINE, "crm:Y2")
    base = [
        F.claim(tag, "located_at", F.SITE_A, F.FEB_1),
        F.claim(crm, "located_at", F.SITE_B, F.FEB_1),
    ]
    observed = F.claim(tag, "same_as", crm, F.FEB_1, kind=AssertionKind.OBSERVED)
    (mention,) = entity_index(document([*base, observed])).find("Y2", as_of=None)
    assert not mention.ambiguous
    retracted = F.claim(tag, "same_as", crm, F.FEB_1, superseded_at=3)
    index = entity_index(document([*base, retracted]))
    assert not index.find("Y2", as_of=2)[0].ambiguous  # current at transaction 2
    assert index.find("Y2", as_of=None)[0].ambiguous  # superseded at 3: no longer joined


def test_same_as_between_two_kinds_joins_nothing() -> None:
    machine, sensor = NodeRef(NodeType.MACHINE, "tag:Z3"), NodeRef(NodeType.SENSOR, "crm:Z3")
    claims = [
        F.claim(machine, "located_at", F.SITE_A, F.FEB_1),
        F.claim(sensor, "mounted_on", F.ARM, F.FEB_1),
        F.claim(machine, "same_as", sensor, F.FEB_1),
    ]
    (mention,) = entity_index(document(claims)).find("Z3", as_of=None)
    assert mention.ambiguous


def test_joining_is_deterministic() -> None:
    claims = joined_claims()
    texts = set()
    for seed in range(5):
        shuffled = list(claims)
        random.Random(seed).shuffle(shuffled)
        index = entity_index(document(shuffled))
        texts.add(
            render_mentions("X1 and WO-1", index.find("X1 and WO-1", as_of=None))
            + render_entities(index.entities())
        )
    assert len(texts) == 1


def test_a_representative_names_declared_ids_once() -> None:
    other = Entity("machine", "b:Q")
    joined = SameAs((other,), ("claim:x",), (), 1)
    lead = dataclasses.replace(Entity("machine", "a:Q"), same_as=joined)
    with pytest.raises(ValueError, match="undeclared"):
        DeclaredIdentifierIndex([lead])
    with pytest.raises(ValueError, match="two representatives"):
        DeclaredIdentifierIndex(
            [lead, other, dataclasses.replace(Entity("machine", "c:Q"), same_as=joined)]
        )
    index = DeclaredIdentifierIndex([lead, other])
    assert [c.declared_id for m in index.find("Q", as_of=None) for c in m.candidates] == ["a:Q"]


def test_the_entity_line_names_the_other_ids_the_files_and_the_depth() -> None:
    index = entity_index(document(joined_claims()))
    (mention,) = index.find("X1", as_of=None)
    register = str(F.REGISTER)
    line = render_mentions("X1", (mention,), SourceIndex({register: ("sites/a/register.csv",)}))
    assert (
        '- "X1": ambiguous between machine "asset:X1" or machine "cmms:X1", also declared as'
        ' "manifest:X1", "syslog:X1" (one machine: 2 same_as claims in evidence join these ids,'
        ' cited from "sites/a/register.csv"; query it with "same_as_depth": 2 to read every id)'
    ) in line
    assert "cited from" not in render_mentions("X1", (mention,))  # no index: no file named


def _compare(document_: GraphDocument, before: NodeRef, after: NodeRef) -> ContextPacket:
    client = local_client(document_)
    arguments: dict[str, Any] = {
        "include_inferred": False,
        "before": {"kind": "run", "declared_id": before.node_id},
        "after": {"kind": "run", "declared_id": after.node_id},
    }
    return asyncio.run(client.query(query_from_arguments("neptune_compare_runs", arguments)))


def _claims(packet: ContextPacket) -> list[ClaimItem]:
    return [i for i in packet.items if isinstance(i, ClaimItem)]


def test_a_comparison_reads_maintenance_on_every_joined_id() -> None:
    # The runs name the leaf id; the work order names the far one, two same_as steps away.
    packet = _compare(document(joined_claims()), RUN_A, RUN_B)
    names = {
        str(getattr(i.claim.object, "value", ""))
        for i in _claims(packet)
        if i.claim.predicate == "has_name"
    }
    assert "WO-1" in names


def test_a_comparison_is_the_same_whichever_joined_id_the_runs_name() -> None:
    def recorded_by(machine: NodeRef) -> list[Claim]:
        return [
            F.claim(c.subject, c.predicate, machine, c.valid_from.ticks, c.valid_to.ticks)  # type: ignore[union-attr]
            if c.predicate == "recorded_by"
            else c
            for c in joined_claims()
        ]

    carried = set()
    for machine in (HUB, LEAF, FAR):
        packet = _compare(document(recorded_by(machine)), RUN_A, RUN_B)
        carried.add(tuple(sorted(i.claim.id for i in _claims(packet))))
    assert len(carried) == 1


# --- Demo v1: ARM-3A over Memory's acceptance snapshot ------------------------------------------


def _demo_entities(text: str) -> str:
    async def go() -> str:
        client = local_client(read_graph_document(F.MEMORY_SNAPSHOT))
        async with connect(build_server(client)) as session:
            result = await session.call_tool(
                "neptune_entities", {"include_inferred": False, "text": text}
            )
            (content,) = result.content
            assert isinstance(content, types.TextContent)
            return content.text

    return asyncio.run(go())


def test_arm_3a_is_one_machine_in_the_demo() -> None:
    names = _demo_entities("why did the calibration on ARM-3A pass before INC-C3-0011?")
    (line,) = [ln for ln in names.splitlines() if ln.startswith('- "ARM-3A"')]
    assert "ambiguous" not in line
    assert line.startswith('- "ARM-3A": machine "cmms.asset:ARM-3A", also declared as')
    for other in ("incident_report.machine", "manifest", "requalification.robot", "servicenow.ci"):
        assert f'"{other}:ARM-3A"' in line
    depth = int(re.findall(r'"same_as_depth": (\d+)', line)[0])
    assert depth == 2


def test_a_query_names_any_of_arm_3as_ids_and_reads_the_same_claims() -> None:
    ids = (
        "cmms.asset:ARM-3A",
        "incident_report.machine:ARM-3A",
        "manifest:ARM-3A",
        "requalification.robot:ARM-3A",
        "servicenow.ci:ARM-3A",
        "syslog.host:ARM-3A",
    )
    client = local_client(read_graph_document(F.MEMORY_SNAPSHOT))

    def ask(declared: str) -> tuple[tuple[str, ...], tuple[Any, ...]]:
        query = {
            "budget": {"items": 50, "tokens": 20000},
            "subjects": [{"kind": "machine", "declared_id": declared, "same_as_depth": 2}],
            "graph": {"hops": 2, "direction": "both", "predicates": "any"},
        }
        arguments = {"include_inferred": False, "query": query}
        packet = asyncio.run(client.query(query_from_arguments("neptune_query", arguments)))
        return tuple(i.id for i in packet.items), tuple(g.to_json() for g in packet.gaps)

    answers = {ask(declared) for declared in ids}
    assert len(answers) == 1

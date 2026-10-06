"""The planner through the SDK (ADR 0005, ADR 0009 §4): plan, choose, ask, entities, find."""

from __future__ import annotations

import asyncio

import pytest
from neptune_memory.schema.codec import GraphDocument
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.reference import ReferenceReader
from neptune_memory.schema.supersede import Resolution as History

import retrieve_fixtures_context as F
from agent_goldens_context import PLANNED, QUESTION, RECORDINGS, ScriptedModel
from neptune_context.engine import LocalEngine
from neptune_context.mcp.__main__ import AGENT_DEFAULTS
from neptune_context.query.plan import (
    Entity,
    PlanStatus,
    ReplayClient,
    load_recordings,
)
from neptune_context.sdk import (
    AsyncClient,
    Client,
    ErrorCode,
    NoModel,
    Planner,
    SdkError,
    entity_index,
)
from neptune_context.sdk.planning import ListingIndex
from sdk_testing_context import golden_stub


def demo_planner(model: object = None) -> Planner:
    client = ReplayClient(load_recordings(RECORDINGS)) if model is None else model
    return Planner(entity_index(F.demo_document()), AGENT_DEFAULTS, client)  # type: ignore[arg-type]


def demo_sdk(planner: Planner | None) -> Client:
    return Client(LocalEngine(ReferenceReader(F.demo_document())), planner=planner)


def test_ask_returns_the_plan_beside_the_packet_of_its_query() -> None:
    asked = demo_sdk(demo_planner()).ask(QUESTION)
    assert asked.plan.status is PlanStatus.READY
    assert asked.plan.assertion_kind == "inferred"
    assert asked.query == PLANNED
    assert asked.packet is not None and asked.packet.query_id == asked.plan.query_id


def test_an_unready_plan_is_returned_without_a_packet() -> None:
    asked = demo_sdk(demo_planner(NoModel())).ask(QUESTION)
    assert asked.plan.status is PlanStatus.FAILED
    assert asked.packet is None and asked.query is None


def test_a_client_without_a_planner_says_unavailable() -> None:
    client = Client(golden_stub())
    for call in (
        lambda: client.plan("why"),
        lambda: client.ask("why"),
        lambda: client.entities(),
        lambda: client.find("ARM-3A"),
    ):
        with pytest.raises(SdkError) as raised:
            call()
        assert raised.value.code is ErrorCode.UNAVAILABLE


def test_bad_planner_calls_are_invalid_arguments() -> None:
    client = demo_sdk(demo_planner())
    planned = client.plan(QUESTION)
    for call in (
        lambda: client.plan(42),  # type: ignore[arg-type]
        lambda: client.choose(planned, "ARM-3A", "asset-tag:ARM-3A"),  # not needs_choice
        lambda: Client(golden_stub(), planner="planner"),  # type: ignore[arg-type]
    ):
        with pytest.raises(SdkError) as raised:
            call()
        assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_a_planner_that_breaks_is_an_engine_error() -> None:
    class Broken:
        client_id = "broken"

        def complete(self, request: object) -> object:
            raise RuntimeError("boom")

    with pytest.raises(SdkError) as raised:
        demo_sdk(demo_planner(Broken())).plan(QUESTION)
    assert raised.value.code is ErrorCode.ENGINE_ERROR


def test_choose_settles_an_ambiguous_name_without_another_model_call() -> None:
    twins = [Entity("machine", "asset-tag:ARM-3A"), Entity("machine", "fleet-id:ARM-3A")]
    planner = Planner(ListingIndex(twins), AGENT_DEFAULTS, ScriptedModel())
    client = demo_sdk(planner)
    planned = client.plan(QUESTION)
    assert planned.status is PlanStatus.NEEDS_CHOICE
    settled = client.choose(planned, "ARM-3A", "asset-tag:ARM-3A")
    assert settled.status is PlanStatus.READY
    with pytest.raises(SdkError):
        client.choose(planned, "ARM-3A", "asset-tag:NOPE")


def test_the_async_client_plans_too() -> None:
    client = AsyncClient(LocalEngine(ReferenceReader(F.demo_document())), planner=demo_planner())

    async def go() -> tuple[object, ...]:
        asked = await client.ask(QUESTION)
        found = await client.find("ARM-3A in CELL-3")
        listed = await client.entities("site")
        chose = await client.plan(QUESTION)
        return asked, found, listed, chose

    asked, found, listed, chose = asyncio.run(go())
    assert asked.packet is not None  # type: ignore[attr-defined]
    assert [m.text for m in found] == ["ARM-3A", "CELL-3"]  # type: ignore[attr-defined]
    assert [e.declared_id for e in listed] == ["site-code:PLANT-2", "site-code:S-007"]  # type: ignore[attr-defined]
    assert chose == asked.plan  # type: ignore[attr-defined]


def test_the_entity_index_holds_declared_names_only() -> None:
    index = entity_index(F.demo_document())
    assert isinstance(index, ListingIndex)
    ids = [e.declared_id for e in index.entities()]
    assert "asset-tag:ARM-3A" in ids and "zone-code:CELL-3" in ids
    assert not any("sha256:" in i for i in ids)  # runs, events, clocks are content addresses
    assert ids == sorted(ids, key=lambda i: (index.lookup(i, as_of=None).kind, i))  # type: ignore[union-attr]
    assert index.lookup("asset-tag:ARM-3A", as_of=None) == Entity("machine", "asset-tag:ARM-3A")


def test_one_identifier_under_two_kinds_keeps_the_first() -> None:
    clash = [
        F.claim(NodeRef(NodeType.MACHINE, "tag:X1"), "located_at", F.SITE_A, F.FEB_1),
        F.claim(NodeRef(NodeType.SENSOR, "tag:X1"), "mounted_on", F.ARM, F.FEB_1, tx=2),
    ]
    claims = sorted(clash, key=lambda c: (c.recorded_at, c.id))
    document = GraphDocument(History(tuple(claims), ()), F.RESOLVER_CONFIG, F.HEAD)
    index = entity_index(document)
    assert index.lookup("tag:X1", as_of=None) == Entity("machine", "tag:X1")

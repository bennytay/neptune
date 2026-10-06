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
        lambda: client.entities(include_inferred=False),
        lambda: client.find("ARM-3A", include_inferred=False),
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
    # The snapshot declares ARM-3A under three source systems' names; the bare name is ambiguous.
    twins = [Entity(*name) for name in F.DEMO_ARM_NAMES]
    planner = Planner(ListingIndex(twins), AGENT_DEFAULTS, ScriptedModel())
    client = demo_sdk(planner)
    planned = client.plan("What changed on ARM-3A?")
    assert planned.status is PlanStatus.NEEDS_CHOICE
    settled = client.choose(planned, "ARM-3A", F.DEMO_ARM[1])
    assert settled.status is PlanStatus.READY
    with pytest.raises(SdkError):
        client.choose(planned, "ARM-3A", "servicenow.ci:NOPE")


def test_the_async_client_plans_too() -> None:
    client = AsyncClient(LocalEngine(ReferenceReader(F.demo_document())), planner=demo_planner())

    async def go() -> tuple[object, ...]:
        asked = await client.ask(QUESTION)
        found = await client.find("ARM-3A at PLANT-2", include_inferred=False)
        listed = await client.entities("site", include_inferred=False)
        chose = await client.plan(QUESTION)
        return asked, found, listed, chose

    asked, found, listed, chose = asyncio.run(go())
    assert asked.packet is not None  # type: ignore[attr-defined]
    assert [m.text for m in found] == ["ARM-3A", "PLANT-2"]  # type: ignore[attr-defined]
    assert [e.declared_id for e in listed] == ["manifest:PLANT-2", "manifest:S-007"]  # type: ignore[attr-defined]
    assert chose == asked.plan  # type: ignore[attr-defined]


def test_the_entity_index_holds_declared_names_only() -> None:
    index = entity_index(F.demo_document())
    ids = [e.declared_id for e in index.entities()]
    assert "servicenow.ci:ARM-3A" in ids and "manifest:PLANT-2" in ids
    assert not any("sha256:" in i for i in ids)  # runs, events, clocks are content addresses
    kinds = {e.declared_id: e.kind for e in index.entities()}
    assert ids == sorted(ids, key=lambda i: (kinds[i], i))
    assert index.lookup("manifest:ARM-3A", as_of=None) == Entity("machine", "manifest:ARM-3A")


def test_one_identifier_under_two_kinds_is_never_offered_as_a_name() -> None:
    clash = [
        F.claim(NodeRef(NodeType.MACHINE, "tag:X1"), "located_at", F.SITE_A, F.FEB_1),
        F.claim(NodeRef(NodeType.SENSOR, "tag:X1"), "mounted_on", F.ARM, F.FEB_1, tx=2),
    ]
    claims = sorted(clash, key=lambda c: (c.recorded_at, c.id))
    document = GraphDocument(History(tuple(claims), ()), F.RESOLVER_CONFIG, F.HEAD)
    index = entity_index(document)
    assert index.lookup("tag:X1", as_of=None) is None
    assert index.find("is X1 ok?", as_of=None) == ()
    assert index.conflicts() == ("tag:X1",)
    assert index.conflicts(1) == ()  # at transaction 1 only the machine claim existed
    assert index.lookup("tag:X1", as_of=1) == Entity("machine", "tag:X1")
    planner = Planner(index, AGENT_DEFAULTS)
    assert Client(golden_stub(), planner=planner).conflicts() == ("tag:X1",)


def snapshot_document() -> GraphDocument:
    """Names that appear only in inferred claims, only in superseded ones, or only later."""
    old = F.claim(
        NodeRef(NodeType.MACHINE, "asset-tag:OLD-1"),
        "located_at",
        F.SITE_A,
        F.FEB_1,
        superseded_at=3,
    )
    new = F.claim(
        NodeRef(NodeType.MACHINE, "asset-tag:NEW-1"),
        "located_at",
        F.SITE_A,
        F.FEB_1,
        tx=3,
        supersedes=(old.id,),
    )
    guess = F.claim(
        NodeRef(NodeType.MACHINE, "asset-tag:GUESS-9"),
        "located_at",
        F.SITE_B,
        F.FEB_1,
        inferred=0.6,
    )
    later = F.claim(NodeRef(NodeType.SENSOR, "serial:LATE-2"), "mounted_on", F.ARM, F.FEB_1, tx=4)
    claims = sorted([old, new, guess, later], key=lambda c: (c.recorded_at, c.id))
    return GraphDocument(History(tuple(claims), ()), F.RESOLVER_CONFIG, F.HEAD)


def test_names_are_those_current_and_declared_at_the_snapshot() -> None:
    index = entity_index(snapshot_document())

    def names(as_of: int | None = None, inferred: bool = False) -> set[str]:
        return {e.declared_id for e in index.entities(as_of, include_inferred=inferred)}

    head = names()
    assert "asset-tag:NEW-1" in head and "serial:LATE-2" in head
    assert "asset-tag:OLD-1" not in head  # only a superseded claim names it
    assert "asset-tag:GUESS-9" not in head  # only an inferred claim names it
    assert "asset-tag:GUESS-9" in names(inferred=True)
    assert "asset-tag:OLD-1" not in names(inferred=True)  # superseded is never current
    at_two = names(2)
    assert "asset-tag:OLD-1" in at_two and "asset-tag:NEW-1" not in at_two
    assert "serial:LATE-2" not in names(3)
    assert index.find("where is GUESS-9?", as_of=None) == ()
    assert [m.text for m in index.find("GUESS-9", as_of=None, include_inferred=True)] == ["GUESS-9"]
    with pytest.raises(SdkError) as raised:
        index.entities(99)
    assert raised.value.code is ErrorCode.NOT_FOUND


def test_the_planner_resolves_names_at_the_plans_snapshot() -> None:
    planner = Planner(entity_index(snapshot_document()), AGENT_DEFAULTS)
    client = Client(golden_stub(), planner=planner)
    assert client.find("OLD-1", as_of=2, include_inferred=False)
    assert not client.find("OLD-1", include_inferred=False)
    assert client.entities("sensor", as_of=3, include_inferred=False) == ()


def test_oversized_questions_are_refused_without_being_echoed() -> None:
    client = demo_sdk(demo_planner())
    marker = "SECRET-PAYLOAD-" + "x" * 2000
    for call in (
        lambda: client.plan(marker),
        lambda: client.ask(marker),
        lambda: client.find(marker, include_inferred=False),
    ):
        with pytest.raises(SdkError) as raised:
            call()
        assert raised.value.code is ErrorCode.INVALID_ARGUMENT
        assert "SECRET" not in raised.value.message and "2015 characters" in raised.value.message

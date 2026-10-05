"""The MCP server (ADR 0004 §6), in process: handshake, tool list, calls over fixtures, errors."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import jsonschema
import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from neptune_context import mcp as neptune_mcp
from neptune_context.mcp import server as mcp_server
from neptune_context.packets.model import EvidenceItem
from neptune_context.query import Budget, Query, Subject, Why, canonical_bytes, query_id
from neptune_context.render.citations import render_text
from neptune_context.sdk import NO_RETRY, AsyncClient, ErrorCode, RetryPolicy, SdkError, StubEngine
from sdk_testing_context import (
    UNAVAILABLE,
    LyingEngine,
    ScriptedEngine,
    answering,
    golden_packet,
    golden_query,
    golden_stub,
    unresolvable,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcp.client.session import ClientSession

    from neptune.model.provenance import EvidenceRef

T = TypeVar("T")
CLAIM = "claim:sha256:03ef80551292669e368d326b22bd44b2a3c5a6461298c94469f16ad71110ad4a"
AGV = {"kind": "machine", "declared_id": "asset_tag:agv-114"}


def run(client: AsyncClient, use: Callable[[ClientSession], Awaitable[T]]) -> T:
    async def go() -> T:
        server = neptune_mcp.build_server(client)
        async with create_connected_server_and_client_session(
            server, read_timeout_seconds=timedelta(seconds=20)
        ) as session:
            return await use(session)

    return asyncio.run(go())


def golden_client() -> AsyncClient:
    return AsyncClient(golden_stub())


def texts(result: types.CallToolResult) -> list[str]:
    return [b.text for b in result.content if isinstance(b, types.TextContent)]


def links(result: types.CallToolResult) -> list[types.ResourceLink]:
    return [b for b in result.content if isinstance(b, types.ResourceLink)]


def error_of(result: types.CallToolResult) -> dict[str, Any]:
    assert result.isError
    (text,) = texts(result)
    return json.loads(text)["error"]  # type: ignore[no-any-return]


def call(client: AsyncClient, tool: str, arguments: dict[str, Any]) -> types.CallToolResult:
    async def use(session: ClientSession) -> types.CallToolResult:
        return await session.call_tool(tool, arguments)

    return run(client, use)


def q_arguments(stem: str) -> dict[str, Any]:
    document = json.loads(canonical_bytes(golden_query(stem)))
    flag = document.pop("include_inferred")
    return {"include_inferred": flag, "query": document}


# --- Protocol -----------------------------------------------------------------------------------


def test_the_handshake_names_the_server_and_offers_tools_and_resources() -> None:
    async def use(session: ClientSession) -> types.InitializeResult:
        # create_connected_server_and_client_session has already initialised the session
        init = session.get_server_capabilities()
        assert init is not None
        assert init.tools is not None and init.resources is not None
        await session.send_ping()
        return await session.initialize()

    result = run(golden_client(), use)
    assert result.serverInfo.name == "neptune"
    assert result.instructions is not None
    assert "include_inferred" in result.instructions


def test_the_four_tools_are_listed_read_only_with_valid_schemas() -> None:
    async def use(session: ClientSession) -> list[types.Tool]:
        return (await session.list_tools()).tools

    tools = run(golden_client(), use)
    assert [t.name for t in tools] == [
        "neptune_query",
        "neptune_why",
        "neptune_diff",
        "neptune_hydrate",
    ]
    for tool in tools:
        jsonschema.Draft202012Validator.check_schema(tool.inputSchema)
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True
        assert tool.description
        assert tool.inputSchema["additionalProperties"] is False
        # a tool name any host accepts: letters, digits, underscore, dash
        assert tool.name.replace("_", "").isalnum()
    packet_tools = [t for t in tools if t.name != "neptune_hydrate"]
    for tool in packet_tools:
        assert "include_inferred" in tool.inputSchema["required"], tool.name
        assert tool.inputSchema["properties"]["include_inferred"]["type"] == "boolean"
        assert "default" not in tool.inputSchema["properties"]["include_inferred"]


def test_tool_schemas_are_the_query_contracts_own_definitions() -> None:
    from neptune_context.query import TextChannel
    from neptune_context.query.schema import query_schema

    schema: dict[str, Any] = mcp_server.input_schemas()["neptune_query"]
    own: dict[str, Any] = query_schema()  # type: ignore[assignment]
    assert schema["$defs"]["Text"] == own["$defs"]["Text"]
    assert schema["$defs"]["Text"]["properties"]["channels"]["items"]["enum"] == sorted(
        str(c) for c in TextChannel
    )
    assert "include_inferred" not in schema["$defs"]["Query"]["properties"]
    # nothing in the server names a retrieval channel
    assert mcp_server.__file__ is not None
    code = Path(mcp_server.__file__).read_text(encoding="utf-8")
    assert "lexical" in code  # only in the example, whose text is checked below
    assert "from neptune_context.retrieve" not in code


def test_the_documented_example_is_a_valid_query() -> None:
    query = mcp_server.query_from_arguments("neptune_query", mcp_server._EXAMPLE_QUERY)
    assert isinstance(query, Query)
    assert query.include_inferred is False


# --- Calls over the golden fixtures -------------------------------------------------------------


@pytest.mark.parametrize("stem", [f"q{n:02d}" for n in range(1, 11)])
def test_neptune_query_returns_the_cited_text_and_a_link_per_evidence_ref(stem: str) -> None:
    result = call(golden_client(), "neptune_query", q_arguments(stem))
    packet = golden_packet(stem)
    assert not result.isError
    assert texts(result) == [render_text(packet)]
    assert [link.name for link in links(result)] == [
        f"E{n}" for n in range(1, len(packet.evidence_refs()) + 1)
    ]
    assert [str(link.uri) for link in links(result)] == [
        mcp_server.evidence_uri(r, packet.as_of) for r in packet.evidence_refs()
    ]
    for link, ref in zip(links(result), packet.evidence_refs(), strict=True):
        assert mcp_server.parse_evidence_uri(str(link.uri)) == (ref, packet.as_of)


def test_inferred_items_stay_marked_through_the_tool() -> None:
    (text,) = texts(call(golden_client(), "neptune_query", q_arguments("q03")))
    assert "INFERRED by" in text


def test_neptune_why_asks_the_golden_q04_question() -> None:
    result = call(golden_client(), "neptune_why", {"claim_id": CLAIM, "include_inferred": False})
    assert texts(result) == [render_text(golden_packet("q04"))]


def test_neptune_diff_builds_a_diff_query_and_returns_its_packet() -> None:
    seen: list[Query] = []

    def make(query: Query) -> object:
        seen.append(query)
        return answering(query)

    client = AsyncClient(LyingEngine(make))
    result = call(
        client,
        "neptune_diff",
        {"subject": AGV, "before": 1500, "after": 1842, "include_inferred": False, "as_of": 1842},
    )
    assert not result.isError
    (query,) = seen
    from neptune_context.query import Diff

    subject = Subject("machine", "asset_tag:agv-114")
    assert query.explain == (Diff(subject, 1500, 1842),)
    assert query.subjects == frozenset({subject})
    assert query.as_of == 1842
    assert query.include_inferred is False
    assert texts(result) == [render_text(answering(query))]


def test_neptune_diff_takes_instants_on_named_clocks() -> None:
    clock = {
        "kind": "civil",
        "timescale": "utc",
        "epoch": "unix",
        "resolution": {"numerator": 1, "denominator": 1000000000},
    }
    other = {"kind": "domain", "domain_id": "rec:sha256:" + "7" * 64}
    seen: list[Query] = []

    def make(query: Query) -> object:
        seen.append(query)
        return answering(query)

    client = AsyncClient(LyingEngine(make))
    arguments: dict[str, Any] = {
        "subject": AGV,
        "before": {"clock": clock, "ticks": 1_788_080_400_000_000_000},
        "after": {"clock": clock, "ticks": 1_788_080_500_000_000_000},
        "include_inferred": True,
    }
    assert not call(client, "neptune_diff", arguments).isError
    assert len(seen) == 1
    # a diff across two clocks needs a named mapping (a clock bridge): neptune_query carries those
    arguments["after"] = {"clock": other, "ticks": 5}
    error = error_of(call(client, "neptune_diff", arguments))
    assert error["code"] == "query_refused"
    assert [f["code"] for f in error["findings"]] == ["cross_clock_without_mapping"]
    assert len(seen) == 1


def test_neptune_hydrate_returns_the_ledgers_resolution_and_resource_links_read_back() -> None:
    item = next(i for i in golden_packet("q04").items if isinstance(i, EvidenceItem))
    resolution = unresolvable(item.evidence)
    client = AsyncClient(StubEngine({}, {item.evidence: resolution}))

    async def use(session: ClientSession) -> tuple[types.CallToolResult, types.ReadResourceResult]:
        tool = await session.call_tool(
            "neptune_hydrate", {"evidence": item.evidence.to_json(), "as_of": 5}
        )
        read = await session.read_resource(mcp_server.evidence_uri(item.evidence))  # type: ignore[arg-type]
        return tool, read

    tool, read = run(client, use)
    from neptune_ledger.api import dumps as ledger_dumps

    expected = ledger_dumps(resolution).decode("utf-8")
    assert texts(tool) == [expected]
    (content,) = read.contents
    assert isinstance(content, types.TextResourceContents)
    assert content.text == expected
    assert content.mimeType == "application/json"


def test_resources_are_addressed_by_link_never_enumerated() -> None:
    async def use(session: ClientSession) -> tuple[list[Any], list[Any]]:
        return (
            (await session.list_resources()).resources,
            (await session.list_resource_templates()).resourceTemplates,
        )

    resources, templates = run(golden_client(), use)
    assert resources == []
    assert [t.uriTemplate for t in templates] == ["neptune://evidence/{ref}{?as_of}"]


def test_the_same_call_twice_returns_identical_content() -> None:
    a = call(golden_client(), "neptune_query", q_arguments("q06"))
    b = call(golden_client(), "neptune_query", q_arguments("q06"))
    assert a.model_dump_json() == b.model_dump_json()


def test_links_are_capped_and_the_rest_are_named() -> None:
    packet = golden_packet("q01")
    assert len(packet.evidence_refs()) > 1
    original = mcp_server.MAX_LINKS
    mcp_server.MAX_LINKS = 1  # type: ignore[misc]
    try:
        blocks = mcp_server.packet_content(packet)
    finally:
        mcp_server.MAX_LINKS = original  # type: ignore[misc]
    assert sum(isinstance(b, types.ResourceLink) for b in blocks) == 1
    last = blocks[-1]
    assert isinstance(last, types.TextContent) and "of 4" in last.text


# --- Choosing inference, and refusals -----------------------------------------------------------


def test_an_agent_must_choose_include_inferred() -> None:
    for tool, arguments in (
        ("neptune_query", {"query": {"budget": {"items": 5}, "subjects": [{"kind": "run"}]}}),
        ("neptune_why", {"claim_id": CLAIM}),
        ("neptune_diff", {"subject": AGV, "before": 1, "after": 2}),
    ):
        error = error_of(call(golden_client(), tool, arguments))
        assert error["code"] == "invalid_argument", tool
        assert "include_inferred" in error["message"], tool
    bad_flags: list[Any] = ["true", 1, None, [], {}]
    for bad in bad_flags:
        result = call(golden_client(), "neptune_why", {"claim_id": CLAIM, "include_inferred": bad})
        assert error_of(result)["code"] == "invalid_argument"


def test_a_query_that_contradicts_the_tool_parameter_is_refused() -> None:
    arguments = q_arguments("q01")
    arguments["query"]["include_inferred"] = not arguments["include_inferred"]
    error = error_of(call(golden_client(), "neptune_query", arguments))
    assert error["code"] == "invalid_argument"
    arguments["query"]["include_inferred"] = arguments["include_inferred"]  # agreeing is fine
    assert not call(golden_client(), "neptune_query", arguments).isError


def test_fixed_defaults_may_be_left_out_of_a_query() -> None:
    minimal = {
        "include_inferred": False,
        "query": {"budget": {"items": 10}, "explain": [{"kind": "why", "claim_id": CLAIM}]},
    }
    result = call(golden_client(), "neptune_query", minimal)
    assert texts(result) == [render_text(golden_packet("q04"))]


def test_a_refused_query_comes_back_with_its_findings() -> None:
    empty = {"include_inferred": False, "query": {"budget": {"items": 5}}}
    error = error_of(call(golden_client(), "neptune_query", empty))
    assert error["code"] == "query_refused"
    assert [f["code"] for f in error["findings"]] == ["empty_query"]
    assert error["retryable"] is False


def test_malformed_arguments_are_structured_errors_never_crashes() -> None:
    client = golden_client()
    cases: list[tuple[str, dict[str, Any]]] = [
        ("neptune_query", {}),
        ("neptune_query", {"include_inferred": False, "query": "text"}),
        ("neptune_query", {"include_inferred": False, "query": []}),
        ("neptune_query", {"include_inferred": False, "query": {}, "extra": 1}),
        ("neptune_query", {"include_inferred": False, "query": {"budget": 5}}),
        ("neptune_query", {"include_inferred": False, "query": {"budget": {"items": 0}}}),
        (
            "neptune_query",
            {"include_inferred": False, "query": {"budget": {"items": 1}, "subjects": "x"}},
        ),
        (
            "neptune_query",
            {"include_inferred": False, "query": {"budget": {"items": 1}, "explain": "x"}},
        ),
        ("neptune_why", {"include_inferred": False, "claim_id": 5}),
        ("neptune_why", {"include_inferred": False, "claim_id": "claim:sha256:xyz"}),
        ("neptune_why", {"include_inferred": False, "claim_id": CLAIM, "as_of": -1}),
        ("neptune_why", {"include_inferred": False, "claim_id": CLAIM, "as_of": True}),
        ("neptune_why", {"include_inferred": False, "claim_id": CLAIM, "max_items": 0}),
        ("neptune_why", {"include_inferred": False, "claim_id": CLAIM, "max_items": 10001}),
        ("neptune_why", {"include_inferred": False, "claim_id": CLAIM, "bogus": 1}),
        ("neptune_diff", {"include_inferred": False, "subject": "m", "before": 1, "after": 2}),
        ("neptune_diff", {"include_inferred": False, "subject": AGV, "before": 2, "after": 1}),
        ("neptune_diff", {"include_inferred": False, "subject": AGV, "before": {}, "after": 2}),
        ("neptune_hydrate", {}),
        ("neptune_hydrate", {"evidence": {}}),
        ("neptune_hydrate", {"evidence": "sha256:x"}),
        ("neptune_nope", {}),
    ]
    for tool, arguments in cases:
        result = call(client, tool, arguments)
        error = error_of(result)
        assert error["code"] in {"invalid_argument", "query_refused"}, (tool, arguments)
        assert error["retryable"] is False


def test_unknown_tools_name_the_real_ones() -> None:
    error = error_of(call(golden_client(), "neptune.query", {}))
    assert "neptune_query" in error["message"]


def test_engine_failures_surface_as_tool_errors_with_retryability() -> None:
    down = AsyncClient(ScriptedEngine(golden_stub(), [UNAVAILABLE]), retry=NO_RETRY)
    error = error_of(call(down, "neptune_query", q_arguments("q01")))
    assert (error["code"], error["retryable"]) == ("unavailable", True)

    async def instant(_: float) -> None:
        return None

    flaky = ScriptedEngine(golden_stub(), [UNAVAILABLE])  # the client's retry hides one blip
    retrying = AsyncClient(flaky, retry=RetryPolicy(attempts=2), sleep=instant)
    assert not call(retrying, "neptune_query", q_arguments("q01")).isError
    assert flaky.calls == 2
    missing = error_of(
        call(
            golden_client(),
            "neptune_why",
            {"claim_id": "claim:sha256:" + "0" * 64, "include_inferred": False},
        )
    )
    assert missing["code"] == "not_found"
    wrong = AsyncClient(LyingEngine(lambda q: golden_packet("q02")))
    assert error_of(call(wrong, "neptune_query", q_arguments("q01")))["code"] == "invalid_response"


# --- Evidence URIs ------------------------------------------------------------------------------


def test_evidence_uris_round_trip_and_refuse_everything_else() -> None:
    ref = golden_packet("q04").evidence_refs()[0]
    uri = mcp_server.evidence_uri(ref)
    assert uri.startswith("neptune://evidence/")
    assert "=" not in uri
    assert mcp_server.parse_evidence_uri(uri) == (ref, None)
    pinned = mcp_server.evidence_uri(ref, 5)
    assert pinned == uri + "?as_of=5"
    assert mcp_server.parse_evidence_uri(pinned) == (ref, 5)
    token = uri.removeprefix("neptune://evidence/")
    bad = [
        "",
        "neptune://evidence/",
        "neptune://other/" + token,
        "https://evidence/" + token,
        uri + "=",
        uri + "AAAA",
        "neptune://evidence/" + token[:-3],
        "neptune://evidence/" + "!!!!",
        "neptune://evidence/" + "A" * 20000,
        "neptune://evidence/" + token.replace("A", "B", 1),
        uri + "?as_of=",
        uri + "?as_of=-1",
        uri + "?as_of=05",
        uri + "?as_of=1&as_of=2",
        uri + "?as_of=1.5",
        uri + "?other=1",
        uri + "?as_of=\u0665",
    ]
    for candidate in bad:
        with pytest.raises(SdkError) as raised:
            mcp_server.parse_evidence_uri(candidate)
        assert raised.value.code is ErrorCode.INVALID_ARGUMENT, candidate[:40]


def test_reading_a_link_hydrates_at_the_snapshot_the_answer_was_made_at() -> None:
    item = next(i for i in golden_packet("q04").items if isinstance(i, EvidenceItem))
    asked: list[int | None] = []

    class Recording(StubEngine):
        def hydrate(self, evidence: EvidenceRef, *, as_of: int | None) -> Any:
            asked.append(as_of)
            return super().hydrate(evidence, as_of=as_of)

    engine = Recording(
        {query_id(golden_query("q04")): golden_packet("q04")},
        {item.evidence: unresolvable(item.evidence)},
    )

    async def use(session: ClientSession) -> None:
        answer = await session.call_tool(
            "neptune_why", {"claim_id": CLAIM, "include_inferred": False}
        )
        for link in links(answer):
            if mcp_server.parse_evidence_uri(str(link.uri))[0] == item.evidence:
                await session.read_resource(link.uri)

    run(AsyncClient(engine), use)
    assert asked == [golden_packet("q04").as_of]


def test_mcp_why_and_diff_build_the_queries_the_sdk_builds() -> None:
    from neptune_context.sdk import diff_query, why_query

    why = mcp_server.query_from_arguments(
        "neptune_why", {"claim_id": CLAIM, "include_inferred": True, "as_of": 7, "max_items": 3}
    )
    assert why == why_query(CLAIM, include_inferred=True, as_of=7, budget=Budget(items=3))
    assert mcp_server.query_from_arguments(
        "neptune_why", {"claim_id": CLAIM, "include_inferred": False}
    ) == why_query(CLAIM, include_inferred=False)
    diff = mcp_server.query_from_arguments(
        "neptune_diff",
        {"subject": AGV, "before": 1, "after": 2, "include_inferred": False, "as_of": 9},
    )
    assert diff == diff_query(
        Subject("machine", "asset_tag:agv-114"), 1, 2, include_inferred=False, as_of=9
    )


def test_each_tool_schema_carries_only_the_definitions_it_reaches() -> None:
    schemas = mcp_server.input_schemas()
    hydrate = set(schemas["neptune_hydrate"]["$defs"])
    assert "EvidenceRef" in hydrate
    assert not hydrate & {"Query", "Subject", "Budget", "During"}
    assert set(schemas["neptune_why"]["$defs"]) == {"ClaimId"}
    assert {"Query", "Budget", "Text", "Explain"} <= set(schemas["neptune_query"]["$defs"])
    for schema in schemas.values():  # every reference resolves inside its own schema
        refs = json.dumps(schema).split('"$ref": "#/$defs/')[1:]
        assert {r.split('"')[0] for r in refs} <= set(schema["$defs"])


def test_reading_a_bad_resource_is_a_protocol_error() -> None:
    async def use(session: ClientSession) -> str:
        try:
            await session.read_resource("neptune://evidence/AAAA")  # type: ignore[arg-type]
        except Exception as error:
            return type(error).__name__
        return "no error"

    assert run(golden_client(), use) != "no error"


def test_source_text_cannot_forge_a_citation_through_the_tool() -> None:
    # The renderer escapes line breaks in evidence-derived values (ADR 0003 §7); the tool adds none.
    (text,) = texts(call(golden_client(), "neptune_query", q_arguments("q08")))
    assert text == render_text(golden_packet("q08"))
    body, _, footer = text.rpartition("\nEvidence:\n")
    assert all(line.startswith("[E") for line in footer.splitlines())
    assert "\n[E" not in body.replace("\n\n", "\n")


def test_query_ids_in_the_text_name_the_asked_query() -> None:
    (text,) = texts(
        call(golden_client(), "neptune_why", {"claim_id": CLAIM, "include_inferred": False})
    )
    assert (
        query_id(Query(include_inferred=False, budget=Budget(items=10), explain=(Why(CLAIM),)))
        in text
    )

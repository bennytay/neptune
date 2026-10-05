"""The SDK clients (ADR 0004): golden round trips, refusals, verification, retries, determinism."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING

import pytest
from neptune_memory.schema.interval import ledger_tx

from neptune_context.packets.codec import canonical_bytes
from neptune_context.packets.model import ContextPacket, EvidenceItem
from neptune_context.query import Budget, Diff, Query, Subject, Why, query_id
from neptune_context.sdk import (
    NO_RETRY,
    AsyncClient,
    Client,
    ErrorCode,
    RetryPolicy,
    SdkError,
    StubEngine,
    diff_query,
    to_async,
    why_query,
)
from sdk_testing_context import (
    STEMS,
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
    from neptune.model.provenance import EvidenceRef

CLAIM = "claim:sha256:03ef80551292669e368d326b22bd44b2a3c5a6461298c94469f16ad71110ad4a"


def _no_sleep(_: float) -> None:
    return None


async def _no_asleep(_: float) -> None:
    return None


def _code(call: object) -> ErrorCode:
    with pytest.raises(SdkError) as raised:
        call()  # type: ignore[operator]
    return raised.value.code


# --- Golden round trips against a stub engine ----------------------------------------------------


@pytest.mark.parametrize("stem", STEMS)
def test_sync_client_round_trips_every_golden_packet(stem: str) -> None:
    packet = Client(golden_stub()).query(golden_query(stem))
    assert canonical_bytes(packet) == canonical_bytes(golden_packet(stem))


@pytest.mark.parametrize("stem", STEMS)
def test_async_client_round_trips_every_golden_packet(stem: str) -> None:
    packet = asyncio.run(AsyncClient(golden_stub()).query(golden_query(stem)))
    assert canonical_bytes(packet) == canonical_bytes(golden_packet(stem))


def test_sync_and_async_clients_return_equal_packets_and_equal_errors() -> None:
    stub = golden_stub()
    sync, other = Client(stub), AsyncClient(to_async(stub))
    query = golden_query("q05")
    assert sync.query(query) == asyncio.run(other.query(query))
    unknown = Query(include_inferred=False, budget=Budget(items=1), explain=(Why(CLAIM),))
    with pytest.raises(SdkError) as sync_raised:
        sync.query(unknown)
    with pytest.raises(SdkError) as async_raised:
        asyncio.run(other.query(unknown))
    assert sync_raised.value.to_json() == async_raised.value.to_json()
    assert sync_raised.value.code is ErrorCode.NOT_FOUND


def test_why_sends_the_golden_q04_query() -> None:
    q04 = golden_query("q04")
    assert why_query(CLAIM, include_inferred=False) == q04
    assert Client(golden_stub()).why(CLAIM, include_inferred=False) == golden_packet("q04")
    assert asyncio.run(AsyncClient(golden_stub()).why(CLAIM, include_inferred=False)) == (
        golden_packet("q04")
    )


def test_diff_builds_the_typed_query_a_caller_could_have_written() -> None:
    subject = Subject("machine", "asset_tag:agv-114")
    query = diff_query(subject, 1500, 1842, include_inferred=False, as_of=1842)
    assert query == Query(
        include_inferred=False,
        budget=Budget(items=10),
        subjects=frozenset({subject}),
        as_of=1842,
        explain=(Diff(subject, 1500, 1842),),
    )
    engine = LyingEngine(lambda q: answering(q))
    packet = Client(engine).diff(subject, 1500, 1842, include_inferred=False, as_of=1842)
    assert packet.query_id == query_id(query)
    assert (
        asyncio.run(
            AsyncClient(engine).diff(subject, 1500, 1842, include_inferred=False, as_of=1842)
        )
        == packet
    )


def test_include_inferred_is_never_defaulted() -> None:
    client = Client(golden_stub())
    with pytest.raises(TypeError):
        client.why(CLAIM)  # type: ignore[call-arg]
    assert _code(lambda: client.why(CLAIM, include_inferred=1)) is ErrorCode.INVALID_ARGUMENT  # type: ignore[arg-type]
    assert _code(lambda: client.why(CLAIM, include_inferred=None)) is ErrorCode.INVALID_ARGUMENT  # type: ignore[arg-type]


def test_the_same_call_twice_gives_byte_identical_packets() -> None:
    client = Client(golden_stub())
    first = canonical_bytes(client.query(golden_query("q03")))
    assert first == canonical_bytes(client.query(golden_query("q03")))


# --- Refusing and verifying ----------------------------------------------------------------------


def test_a_refused_query_never_reaches_the_engine_and_carries_its_findings() -> None:
    engine = ScriptedEngine(golden_stub(), [])
    empty = Query(include_inferred=False, budget=Budget(items=1))
    with pytest.raises(SdkError) as raised:
        Client(engine).query(empty)
    assert raised.value.code is ErrorCode.QUERY_REFUSED
    assert [str(f.code) for f in raised.value.findings] == ["empty_query"]
    assert engine.calls == 0
    with pytest.raises(SdkError) as again:
        asyncio.run(AsyncClient(engine).query(empty))
    assert again.value.to_json() == raised.value.to_json()
    assert engine.calls == 0


def test_a_non_query_is_an_invalid_argument() -> None:
    client = Client(golden_stub())
    assert _code(lambda: client.query({"include_inferred": False})) is ErrorCode.INVALID_ARGUMENT  # type: ignore[arg-type]


def test_an_answer_that_is_not_for_this_query_is_an_invalid_response() -> None:
    other = golden_packet("q02")
    cases = {
        "not a packet": lambda q: {"packet": 1},
        "different query": lambda q: other,
        "different snapshot": lambda q: dataclasses.replace(answering(q), as_of=ledger_tx(6)),
        "inference disagrees": lambda q: dataclasses.replace(
            answering(q), inference_included=not q.include_inferred
        ),
    }
    query = Query(include_inferred=False, budget=Budget(items=5), as_of=7, explain=(Why(CLAIM),))
    for name, make in cases.items():
        client = Client(LyingEngine(make))
        assert _code(lambda: client.query(query)) is ErrorCode.INVALID_RESPONSE, name  # noqa: B023


def test_a_pinned_snapshot_must_be_the_one_answered() -> None:
    pinned = Query(include_inferred=False, budget=Budget(items=5), as_of=7, explain=(Why(CLAIM),))
    good = Client(LyingEngine(lambda q: answering(q))).query(pinned)
    assert good.as_of == 7
    bad = Client(LyingEngine(lambda q: dataclasses.replace(answering(q), as_of=ledger_tx(6))))
    assert _code(lambda: bad.query(pinned)) is ErrorCode.INVALID_RESPONSE


def test_an_engine_that_raises_something_else_is_an_engine_error() -> None:
    class Broken:
        def query(self, query: Query) -> ContextPacket:
            raise RuntimeError("boom")

        def hydrate(self, evidence: EvidenceRef, *, as_of: int | None) -> object:
            raise KeyError("x")

    assert _code(lambda: Client(Broken()).query(golden_query("q01"))) is ErrorCode.ENGINE_ERROR  # type: ignore[arg-type]
    assert _code(lambda: asyncio.run(AsyncClient(Broken()).query(golden_query("q01")))) is (  # type: ignore[arg-type]
        ErrorCode.ENGINE_ERROR
    )


# --- Targets, auth ------------------------------------------------------------------------------


def test_local_mode_takes_no_token_and_a_remote_url_needs_a_safe_scheme() -> None:
    assert _code(lambda: Client(golden_stub(), token="t")) is ErrorCode.INVALID_ARGUMENT
    assert _code(lambda: Client(object())) is ErrorCode.INVALID_ARGUMENT  # type: ignore[arg-type]
    assert _code(lambda: Client("ftp://x")) is ErrorCode.INVALID_ARGUMENT
    assert _code(lambda: Client("http://example.org", token="secret")) is ErrorCode.INVALID_ARGUMENT
    assert Client("http://127.0.0.1:9", token="secret") is not None
    assert Client("https://example.org", token="secret") is not None


def test_a_token_never_appears_in_a_repr() -> None:
    client = Client("https://example.org", token="s3cret-token")
    assert "s3cret" not in repr(client)
    assert "s3cret" not in repr(AsyncClient("https://example.org", token="s3cret-token"))


def test_a_sync_engine_is_refused_by_neither_client_and_an_async_one_only_by_the_sync_client() -> (
    None
):
    class Async:
        async def query(self, query: Query) -> ContextPacket:
            return answering(query)

        async def hydrate(self, evidence: EvidenceRef, *, as_of: int | None) -> object:
            raise SdkError(ErrorCode.NOT_FOUND, "no")

    query = Query(include_inferred=False, budget=Budget(items=5), explain=(Why(CLAIM),))
    assert asyncio.run(AsyncClient(Async()).query(query)).query_id == query_id(query)  # type: ignore[arg-type]
    assert _code(lambda: Client(Async())) is ErrorCode.INVALID_ARGUMENT  # type: ignore[arg-type]


# --- Retries ------------------------------------------------------------------------------------


def test_transient_failures_are_retried_with_the_declared_schedule() -> None:
    engine = ScriptedEngine(golden_stub(), [UNAVAILABLE, SdkError(ErrorCode.TIMEOUT, "slow")])
    pauses: list[float] = []
    client = Client(
        engine, retry=RetryPolicy(attempts=3, backoff_s=(0.5, 2.0)), sleep=pauses.append
    )
    assert client.query(golden_query("q01")) == golden_packet("q01")
    assert (engine.calls, pauses) == (3, [0.5, 2.0])


def test_retries_stop_at_the_budget_and_never_cover_permanent_errors() -> None:
    always = ScriptedEngine(golden_stub(), [UNAVAILABLE] * 5)
    client = Client(always, retry=RetryPolicy(attempts=3), sleep=_no_sleep)
    assert _code(lambda: client.query(golden_query("q01"))) is ErrorCode.UNAVAILABLE
    assert always.calls == 3
    for error in (
        SdkError(ErrorCode.UNAUTHENTICATED, "no"),
        SdkError(ErrorCode.FORBIDDEN, "no"),
        SdkError(ErrorCode.NOT_FOUND, "no"),
        SdkError(ErrorCode.ENGINE_ERROR, "no"),
        RuntimeError("bug"),
    ):
        once = ScriptedEngine(golden_stub(), [error, error, error])
        permanent = Client(once, retry=RetryPolicy(attempts=3), sleep=_no_sleep)
        with pytest.raises(SdkError):
            permanent.query(golden_query("q01"))
        assert once.calls == 1, error


def test_the_async_client_retries_the_same_way_and_hydrate_too() -> None:
    engine = ScriptedEngine(golden_stub(), [UNAVAILABLE, UNAVAILABLE])
    pauses: list[float] = []

    async def record(seconds: float) -> None:
        pauses.append(seconds)

    client = AsyncClient(engine, retry=RetryPolicy(attempts=3, backoff_s=(0.1,)), sleep=record)
    assert asyncio.run(client.query(golden_query("q02"))) == golden_packet("q02")
    assert (engine.calls, pauses) == (3, [0.1, 0.1])
    item = next(i for i in golden_packet("q04").items if isinstance(i, EvidenceItem))
    resolution = unresolvable(item.evidence)
    engine = ScriptedEngine(StubEngine({}, {item.evidence: resolution}), [UNAVAILABLE])
    again = AsyncClient(engine, retry=RetryPolicy(attempts=2), sleep=_no_asleep)
    assert asyncio.run(again.hydrate(item)) == resolution
    assert engine.calls == 2


def test_local_clients_do_not_retry_unless_asked_and_remote_ones_do() -> None:
    for make in (Client, Client.local):
        engine = ScriptedEngine(golden_stub(), [UNAVAILABLE])
        assert _code(lambda: make(engine).query(golden_query("q01"))) is ErrorCode.UNAVAILABLE  # noqa: B023
        assert engine.calls == 1
    engine = ScriptedEngine(golden_stub(), [UNAVAILABLE])
    asked = Client(engine, retry=RetryPolicy(attempts=2), sleep=_no_sleep)
    assert asked.query(golden_query("q01")) == golden_packet("q01")
    assert NO_RETRY.attempts == 1
    assert Client("https://example.org")._retry == RetryPolicy()
    assert AsyncClient("https://example.org")._retry == RetryPolicy()
    assert AsyncClient(golden_stub())._retry == NO_RETRY


def test_retry_policy_bounds() -> None:
    for attempts in (0, 11, True):
        assert _code(lambda: RetryPolicy(attempts=attempts)) is ErrorCode.INVALID_ARGUMENT  # noqa: B023
    for backoff in ((), (-0.1,), (61.0,)):
        assert _code(lambda: RetryPolicy(backoff_s=backoff)) is ErrorCode.INVALID_ARGUMENT  # noqa: B023
    assert RetryPolicy(attempts=1).attempts == 1
    assert RetryPolicy(attempts=10, backoff_s=(0.0, 60.0)).pause(7) == 60.0


# --- hydrate ------------------------------------------------------------------------------------


def _evidence_item() -> EvidenceItem:
    return next(i for i in golden_packet("q04").items if isinstance(i, EvidenceItem))


def test_hydrate_takes_an_item_or_a_ref_and_returns_the_ledgers_resolution() -> None:
    item = _evidence_item()
    resolution = unresolvable(item.evidence)
    client = Client(StubEngine({}, {item.evidence: resolution}))
    assert client.hydrate(item, as_of=5) == resolution
    assert client.hydrate(item.evidence) == resolution
    assert asyncio.run(AsyncClient(StubEngine({}, {item.evidence: resolution})).hydrate(item)) == (
        resolution
    )


def test_hydrate_refuses_bad_arguments_and_wrong_answers() -> None:
    item = _evidence_item()
    client = Client(StubEngine({}, {item.evidence: unresolvable(item.evidence)}))
    assert _code(lambda: client.hydrate("sha256:ab")) is ErrorCode.INVALID_ARGUMENT  # type: ignore[arg-type]
    for bad in (-1, 2**63, True, 1.5, "5"):
        assert _code(lambda: client.hydrate(item, as_of=bad)) is ErrorCode.INVALID_ARGUMENT  # type: ignore[arg-type]  # noqa: B023
    assert client.hydrate(item, as_of=2**63 - 1) is not None
    assert client.hydrate(item, as_of=0) is not None
    other = next(r for r in golden_packet("q01").evidence_refs() if r != item.evidence)
    assert _code(lambda: client.hydrate(other)) is ErrorCode.NOT_FOUND
    wrong = Client(StubEngine({}, {other: unresolvable(item.evidence)}))
    assert _code(lambda: wrong.hydrate(other)) is ErrorCode.INVALID_RESPONSE
    # Same source, another locator: the answer is about other bytes (C1 gate review).
    moved = dataclasses.replace(
        unresolvable(item.evidence),
        evidence_ref=dataclasses.replace(
            unresolvable(item.evidence).evidence_ref, locator=({"kind": "whole"},)
        ),
    )
    elsewhere = Client(StubEngine({}, {item.evidence: moved}))
    assert _code(lambda: elsewhere.hydrate(item)) is ErrorCode.INVALID_RESPONSE


def test_a_sync_client_refuses_an_engine_whose_hydrate_is_async() -> None:
    class Half:
        def query(self, query: Query) -> ContextPacket:
            raise AssertionError("not called")

        async def hydrate(self, evidence: object, *, as_of: int | None) -> object:
            raise AssertionError("not called")

    assert _code(lambda: Client(Half())) is ErrorCode.INVALID_ARGUMENT  # type: ignore[arg-type]


# --- the stub -----------------------------------------------------------------------------------


def test_the_stub_answers_only_what_it_recorded() -> None:
    stub = golden_stub()
    assert len(stub.query_ids) == 10
    assert stub.query_ids == tuple(sorted(stub.query_ids))
    with pytest.raises(SdkError) as raised:
        stub.query(Query(include_inferred=True, budget=Budget(items=9), explain=(Why(CLAIM),)))
    assert raised.value.code is ErrorCode.NOT_FOUND


def test_the_stub_refuses_two_packets_for_one_query_and_bad_directories(tmp_path: object) -> None:
    packet = golden_packet("q01")
    assert _code(lambda: StubEngine.from_packets([packet, packet])) is ErrorCode.INVALID_ARGUMENT
    import pathlib

    root = pathlib.Path(str(tmp_path))
    assert _code(lambda: StubEngine.from_directory(root / "missing")) is ErrorCode.INVALID_ARGUMENT
    (root / "broken.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SdkError) as raised:
        StubEngine.from_directory(root)
    assert "broken.json is not a packet" in raised.value.message
    (root / "broken.json").unlink()
    assert StubEngine.from_directory(root).query_ids == ()


def test_the_stub_loads_the_golden_directory() -> None:
    from context_packet_goldens import PACKETS

    assert StubEngine.from_directory(PACKETS).query_ids == golden_stub().query_ids


def test_errors_survive_pickling_and_copying() -> None:
    import copy
    import pickle

    error = SdkError(ErrorCode.TIMEOUT, "slow")
    for clone in (pickle.loads(pickle.dumps(error)), copy.copy(error), copy.deepcopy(error)):
        assert clone.to_json() == error.to_json()
        assert str(clone) == "timeout: slow"


def test_error_messages_are_bounded_and_structured() -> None:
    error = SdkError(ErrorCode.UNAVAILABLE, "x" * 5000)
    assert len(error.message) == 500
    assert error.to_json() == {
        "code": "unavailable",
        "findings": [],
        "message": error.message,
        "retryable": True,
    }
    assert not SdkError(ErrorCode.QUERY_REFUSED, "no").retryable

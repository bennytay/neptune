"""Shared fixtures for the SDK and MCP tests: golden queries and packets, engines that fail."""

from __future__ import annotations

import dataclasses
import json
from functools import cache
from typing import TYPE_CHECKING

from neptune_ledger.api import EvidenceAnchor, Region, Resolution, TransactionKey
from neptune_ledger.api.types import CatalogFinding
from neptune_memory.schema.interval import ledger_tx

from context_packet_goldens import PACKETS, QUERIES
from neptune.identity.canonical_json import dumps
from neptune.model.ids import RecordId
from neptune.model.knowledge import Known, NotCovered
from neptune.model.provenance import evidence_ref_from_json
from neptune_context.answer import domain_id
from neptune_context.packets.codec import decode
from neptune_context.packets.model import (
    BudgetUse,
    ClaimItem,
    ContextPacket,
    During,
    Limit,
    Limits,
    SeriesWindowItem,
    measure,
)
from neptune_context.query import Query, loads, query_id
from neptune_context.sdk import ErrorCode, SdkError, StubEngine

if TYPE_CHECKING:
    from collections.abc import Callable

    from neptune.model.provenance import EvidenceRef


@cache
def golden_query(stem: str) -> Query:
    (path,) = sorted(QUERIES.glob(f"{stem}-*.json"))
    query = loads(path.read_bytes())
    assert isinstance(query, Query), query
    return query


@cache
def golden_packet(stem: str) -> ContextPacket:
    (path,) = sorted(PACKETS.glob(f"{stem}-*.json"))
    packet = decode(path.read_bytes())
    assert isinstance(packet, ContextPacket), packet
    return packet


STEMS = [f"q{n:02d}" for n in range(1, 11)]


def golden_stub() -> StubEngine:
    return StubEngine.from_packets(golden_packet(stem) for stem in STEMS)


def answering(query: Query, like: str = "q01") -> ContextPacket:
    """A valid packet that answers ``query`` (``answer_problems`` finds nothing): a golden
    packet re-addressed to it, with the query's budget and window, kept items cut to fit."""
    packet = golden_packet(like)
    as_of = query.as_of if isinstance(query.as_of, int) else packet.as_of
    b = query.budget
    limits = Limits(b.items, b.tokens, b.bytes, b.latency_ms)
    during = (
        None
        if query.during is None
        else During(RecordId(domain_id(query.during.clock)), query.during.start, query.during.end)
    )
    candidates = [
        i
        for i in packet.items
        if (query.include_inferred or not i.is_inferred)
        and (
            during is None
            or not isinstance(i, ClaimItem | SeriesWindowItem)
            or (i.claim.valid.domain_id if isinstance(i, ClaimItem) else i.clock)
            == during.domain_id
        )
    ]
    items, exhausted = candidates[: b.items], [Limit.ITEMS] if len(candidates) > b.items else []
    for limit, bound, index in ((Limit.BYTES, b.bytes, 0), (Limit.TOKENS, b.tokens, 1)):
        while bound is not None and items and measure(items)[index] > bound:
            items.pop()
            exhausted.append(limit)
    dropped = len(candidates) - len(items)
    claims = {i.claim.id for i in items if isinstance(i, ClaimItem)}
    return dataclasses.replace(
        packet,
        query_id=query_id(query),
        as_of=ledger_tx(max(as_of, packet.as_of)),
        head=ledger_tx(max(as_of, packet.head)),
        during=during,
        inference_included=query.include_inferred,
        budget=BudgetUse.measured(
            limits, items, dropped=dropped, exhausted=tuple(sorted(set(exhausted)))
        ),
        items=tuple(items),
        superseded_since=tuple(s for s in packet.superseded_since if s.claim in claims),
        findings=tuple(f for f in packet.findings if {f.claim, *f.others} & claims),
        gaps=(),
    )


def unresolvable(ref: EvidenceRef) -> Resolution:
    """The Ledger's answer for a source no package holds, for ``ref``'s source."""
    assert isinstance(ref.source, str)
    anchor = EvidenceAnchor(ref.source, tuple(step.to_json() for step in ref.locator))
    tx = TransactionKey(5, "2026-10-05T10:00:00.000000Z")
    return Resolution(
        evidence_ref=anchor,
        status="unresolvable",
        size=NotCovered(),
        region=Region("byte_range", anchor.locator[-1]),
        fetch=(),
        cited_by=(),
        as_of=Known(tx),
        findings=(
            CatalogFinding("unresolvable_evidence", ref.source, "no registered package holds it"),
        ),
    )


class ScriptedEngine:
    """Raises the scripted errors in order, then answers like ``inner``; counts every call."""

    def __init__(self, inner: StubEngine, errors: list[Exception]) -> None:
        self.inner = inner
        self.errors = list(errors)
        self.calls = 0

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)

    def query(self, query: Query) -> ContextPacket:
        self._maybe_fail()
        return self.inner.query(query)

    def hydrate(self, evidence: EvidenceRef, *, as_of: int | None) -> Resolution:
        self._maybe_fail()
        return self.inner.hydrate(evidence, as_of=as_of)


class LyingEngine:
    """Answers every query with ``make(query)``, whatever it says."""

    def __init__(self, make: Callable[[Query], object]) -> None:
        self.make = make

    def query(self, query: Query) -> ContextPacket:
        return self.make(query)  # type: ignore[return-value]

    def hydrate(self, evidence: EvidenceRef, *, as_of: int | None) -> Resolution:
        raise SdkError(ErrorCode.NOT_FOUND, "no")


UNAVAILABLE = SdkError(ErrorCode.UNAVAILABLE, "down")


# The server half of the wire (ADR 0004 §4), for the tests' loopback server: the package ships only
# the client's half, since engines behind the wire are Platform's.
_STATUS_BY_CODE = {
    ErrorCode.INVALID_ARGUMENT: 400,
    ErrorCode.QUERY_REFUSED: 422,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.UNAVAILABLE: 503,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.INVALID_RESPONSE: 502,
    ErrorCode.ENGINE_ERROR: 500,
}


def status_for(error: SdkError) -> int:
    return _STATUS_BY_CODE[error.code]


def error_body(error: SdkError) -> bytes:
    return dumps({"error": error.to_json()})


def parse_hydrate_request(body: bytes) -> tuple[EvidenceRef, int | None]:
    """Read a hydrate request strictly; ``SdkError(INVALID_ARGUMENT)`` on any defect."""
    try:
        document = json.loads(body.decode("utf-8"))
        if not isinstance(document, dict) or not {"evidence"} <= set(document) <= {
            "as_of",
            "evidence",
        }:
            raise ValueError("a hydrate request has evidence and, optionally, as_of")
        as_of = document.get("as_of")
        if as_of is not None and (
            isinstance(as_of, bool) or not isinstance(as_of, int) or not 0 <= as_of < 2**63
        ):
            raise ValueError("as_of is a Ledger transaction or null")
        return evidence_ref_from_json(document["evidence"]), as_of
    except (ValueError, TypeError, RecursionError) as error:
        raise SdkError(ErrorCode.INVALID_ARGUMENT, f"bad hydrate request: {error}") from error

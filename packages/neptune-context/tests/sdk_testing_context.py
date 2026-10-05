"""Shared fixtures for the SDK and MCP tests: golden queries and packets, engines that fail."""

from __future__ import annotations

import dataclasses
from functools import cache
from typing import TYPE_CHECKING

from neptune_ledger.api import EvidenceAnchor, Region, Resolution, TransactionKey
from neptune_ledger.api.types import CatalogFinding
from neptune_memory.schema.interval import ledger_tx

from context_packet_goldens import PACKETS, QUERIES
from neptune.model.knowledge import Known, NotCovered
from neptune_context.packets.codec import decode
from neptune_context.packets.model import ContextPacket
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
    """A valid packet that answers ``query``: a golden packet re-addressed to it."""
    packet = golden_packet(like)
    as_of = query.as_of if isinstance(query.as_of, int) else packet.as_of
    return dataclasses.replace(
        packet,
        query_id=query_id(query),
        as_of=ledger_tx(max(as_of, packet.as_of)),
        head=ledger_tx(max(as_of, packet.head)),
        inference_included=query.include_inferred,
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

"""The engine seam (ADR 0004 §1): what the SDK, the MCP server and Deploy's console call.

An engine answers a validated ``Query`` with a ``ContextPacket`` and resolves one evidence ref to
what the Ledger knows about it. The real engine (C2) will implement these in-process over a local
Ledger and Memory; ``HttpEngine`` implements them over the wire; ``StubEngine`` answers from
recorded packets so consumers can build before C2 lands. None is privileged: nothing here names a
retrieval channel.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from neptune_ledger.api import Resolution

    from neptune.model.provenance import EvidenceRef
    from neptune_context.packets.model import ContextPacket
    from neptune_context.query.model import Query


@runtime_checkable
class Engine(Protocol):
    """Synchronous engine. Raise ``SdkError`` for every failure; both calls are reads."""

    def query(self, query: Query) -> ContextPacket:
        """The packet answering ``query`` (already validated by the client)."""
        ...

    def hydrate(self, evidence: EvidenceRef, *, as_of: int | None) -> Resolution:
        """``CatalogApi.resolve`` for ``evidence`` at transaction ``as_of`` (``None``: head)."""
        ...


@runtime_checkable
class AsyncEngine(Protocol):
    """The same two calls, awaitable. Same types, same errors."""

    async def query(self, query: Query) -> ContextPacket: ...

    async def hydrate(self, evidence: EvidenceRef, *, as_of: int | None) -> Resolution: ...


class ThreadedEngine:
    """An ``AsyncEngine`` over a synchronous ``Engine``: each call runs in a worker thread."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    async def query(self, query: Query) -> ContextPacket:
        return await asyncio.to_thread(self._engine.query, query)

    async def hydrate(self, evidence: EvidenceRef, *, as_of: int | None) -> Resolution:
        return await asyncio.to_thread(self._engine.hydrate, evidence, as_of=as_of)


def to_async(engine: Engine) -> AsyncEngine:
    """``engine`` as an ``AsyncEngine``; use it to serve the async client from a sync engine."""
    return ThreadedEngine(engine)

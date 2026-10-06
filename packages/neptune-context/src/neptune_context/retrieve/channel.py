"""The retrieval channel interface (ADR 0007 §1): one shape for graph, lexical, vector and spatial.

A channel reads one snapshot and answers one validated query with *hits*: packet items it built,
each with that channel's raw score and rank, plus what it could not answer (``Gap``s), the
Memory resolver findings that qualify its claims, and the supersessions Memory made after its
snapshot. A channel never fuses, cuts to a budget or assembles a packet; the engine does, over
every channel's answer, through one function (``fusion``), so no channel is privileged by
construction (package rule 3).

- ``Snapshot``: the transactions every channel reads at, resolved once per query by the engine.
- ``Retrieval``: what a channel is asked: the validated query and the snapshot.
- ``ChannelAnswer``: what it returns; build it with ``answer`` so hits are ranked one way.
- ``RetrievalChannel``: the protocol a channel implements (``channel`` and ``retrieve``).

Channels are read-only and deterministic: the same snapshot and query give an equal answer. An
error inside a channel is a ``Gap`` in its answer, never an exception that loses the others.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from neptune_memory.schema.interval import LedgerTx, ledger_tx
from neptune_memory.schema.supersede import ResolutionFinding

from neptune_context.packets.model import (
    Channel,
    ChannelHit,
    ClaimItem,
    Gap,
    Item,
    Relevance,
    Superseded,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from neptune.model.jsonvalue import JsonObject
    from neptune_context.query.model import Query


@dataclass(frozen=True)
class Snapshot:
    """The transactions a query is answered at (ADR 0003 §2, ADR 0006 §4).

    ``as_of`` is the packet's: the query's integer ``as_of``, or ``head`` resolved. ``head`` is
    the latest transaction any source knows. ``memory_as_of`` is the snapshot Memory is read at:
    ``as_of`` or, when Memory trails the Ledger, Memory's own head. Every channel reads the same
    three numbers, so their items describe one moment.
    """

    as_of: LedgerTx
    head: LedgerTx
    memory_as_of: LedgerTx

    def __post_init__(self) -> None:
        for name in ("as_of", "head", "memory_as_of"):
            ledger_tx(getattr(self, name))
        if not self.memory_as_of <= self.as_of <= self.head:
            raise ValueError("a snapshot needs memory_as_of <= as_of <= head")


@dataclass(frozen=True)
class Retrieval:
    """One question for one channel: a validated query (``query.accept``) at a snapshot."""

    query: Query
    snapshot: Snapshot


@dataclass(frozen=True)
class ChannelAnswer:
    """One channel's answer: ranked hits and everything it could not or would not carry.

    - ``hits``: items in rank order (raw score descending, then item id), unique by item id; each
      item's ``relevance`` is this channel's alone: fused score = raw score, one ``ChannelHit``
      whose rank is its 1-based position here. Every claim a scene or configuration hit names is
      itself a hit.
    - ``gaps``: parts of the query this channel could not answer, in ``Gap.sort_key`` order.
    - ``findings``: Memory resolver findings active at the snapshot that name a hit's claim.
    - ``superseded``: supersessions in ``(memory_as_of, head]`` of claims among the hits.
    """

    channel: Channel
    hits: tuple[Item, ...] = ()
    gaps: tuple[Gap, ...] = ()
    findings: tuple[ResolutionFinding, ...] = ()
    superseded: tuple[Superseded, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.channel, Channel):
            raise TypeError(f"channel must be a Channel, got {self.channel!r}")
        ids = [hit.id for hit in self.hits]
        if len(set(ids)) != len(ids):
            raise ValueError("a channel answer names an item twice")
        for rank, hit in enumerate(self.hits, start=1):
            if hit.relevance.hits != (ChannelHit(self.channel, rank, hit.relevance.score),):
                raise ValueError(f"{hit.id} must carry this channel's hit at rank {rank} alone")
        claims = {hit.claim.id for hit in self.hits if isinstance(hit, ClaimItem)}
        for hit in self.hits:
            if not set(hit.claim_refs()) <= claims:
                raise ValueError(f"{hit.id} names claims this answer does not carry")
        order = [(-hit.relevance.score, hit.id) for hit in self.hits]
        if order != sorted(order):
            raise ValueError("hits are ordered by raw score (descending), then item id")
        if [g.sort_key() for g in self.gaps] != sorted(g.sort_key() for g in self.gaps):
            raise ValueError("gaps are in Gap.sort_key order")
        if not all(isinstance(f, ResolutionFinding) for f in self.findings):
            raise TypeError("findings are Memory ResolutionFindings")
        if not all(isinstance(s, Superseded) for s in self.superseded):
            raise TypeError("superseded entries are Superseded")


def answer(
    channel: Channel,
    scored: Iterable[tuple[float, Item]],
    *,
    gaps: Iterable[Gap] = (),
    findings: Iterable[ResolutionFinding] = (),
    superseded: Iterable[Superseded] = (),
) -> ChannelAnswer:
    """A ``ChannelAnswer`` from ``(raw score, item)`` pairs in any order.

    An item found twice keeps its best score. Items are ranked by score, then id, and each one's
    relevance is rewritten to this channel's hit at its rank; gaps are sorted and deduplicated,
    findings and supersessions deduplicated and put in their canonical orders.
    """
    best: dict[str, tuple[float, Item]] = {}
    for score, item in scored:
        held = best.get(item.id)
        if held is None or score > held[0]:
            best[item.id] = (score, item)
    ranked = sorted(best.values(), key=lambda pair: (-pair[0], pair[1].id))
    hits = tuple(
        replace(item, relevance=Relevance(score, (ChannelHit(channel, rank, score),)))
        for rank, (score, item) in enumerate(ranked, start=1)
    )
    unique_gaps = {g.sort_key(): g for g in gaps}
    unique_findings = {f.id: f for f in findings}
    unique_superseded = {s.claim: s for s in superseded}
    return ChannelAnswer(
        channel=channel,
        hits=hits,
        gaps=tuple(unique_gaps[k] for k in sorted(unique_gaps)),
        findings=tuple(
            sorted(
                unique_findings.values(),
                key=lambda f: (f.recorded_at, f.claim, str(f.code), f.others),
            )
        ),
        superseded=tuple(unique_superseded[k] for k in sorted(unique_superseded)),
    )


@runtime_checkable
class RetrievalChannel(Protocol):
    """A retrieval channel: graph (MVL-144), lexical (MVL-142), vector (MVL-143), spatial.

    ``retrieve`` must be deterministic, read-only and total: anything it cannot answer is a
    ``Gap`` naming the query member (a JSON pointer into the query's canonical JSON), never an
    exception. It may return more hits than the budget allows; the engine cuts after fusion.
    """

    @property
    def channel(self) -> Channel:
        """Which channel this is; the ``ChannelHit.channel`` of every hit it returns."""
        ...

    @property
    def config(self) -> JsonObject:
        """Every setting that decides its answers (it enters the engine's config hash, so two
        engines that may answer differently never share a ``produced_by``)."""
        ...

    def retrieve(self, request: Retrieval) -> ChannelAnswer:
        """Hits, gaps, findings and supersessions for ``request``."""
        ...

"""The reference ``MemoryReader``: in memory, built on ``resolve`` and ``as_of`` (ADR 0006 §8).

It defines the expected answer to every query, and ``contract.suite`` checks other readers
against it. It is not a store: it holds one resolved history and recomputes each snapshot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from neptune.model.knowledge import Known, NotCovered
from neptune_memory.schema import GRAPH_SCHEMA_VERSION
from neptune_memory.schema.claim import is_inferred
from neptune_memory.schema.nodes import NodeRef
from neptune_memory.schema.reader import (
    ClaimsResult,
    EpisodeFilter,
    EpisodeView,
    Neighbour,
    NeighboursResult,
    NodeView,
    SpatialView,
    check_as_of,
)
from neptune_memory.schema.supersede import Resolution
from neptune_memory.schema.supersede import as_of as snapshot_of

if TYPE_CHECKING:
    from collections.abc import Iterable

    from neptune.model.frames import FrameRef
    from neptune.model.ids import ConfigHash
    from neptune.model.knowledge import Knowledge
    from neptune_memory.schema.claim import Claim
    from neptune_memory.schema.codec import GraphDocument
    from neptune_memory.schema.interval import Interval, LedgerTx
    from neptune_memory.schema.supersede import ResolutionFinding


def _node_key(node: NodeRef) -> tuple[str, str]:
    return (str(node.node_type), node.node_id)


def _by_id(claims: Iterable[Claim]) -> tuple[Claim, ...]:
    return tuple(sorted(claims, key=lambda c: c.id))


def _naming(
    findings: Iterable[ResolutionFinding], claims: Iterable[Claim]
) -> tuple[ResolutionFinding, ...]:
    ids = {c.id for c in claims}
    return tuple(f for f in findings if f.claim in ids or ids.intersection(f.others))


class ReferenceReader:
    """A ``MemoryReader`` over one graph document (a resolved history and its generation)."""

    def __init__(self, document: GraphDocument) -> None:
        self._history: Resolution = document.resolution
        self._generation = document.generation
        self._head = document.head

    @property
    def graph_schema_version(self) -> int:
        return GRAPH_SCHEMA_VERSION

    @property
    def generation(self) -> ConfigHash:
        return self._generation

    @property
    def head(self) -> LedgerTx:
        return self._head

    def _snapshot(self, as_of: LedgerTx, include_inferred: bool = True) -> Resolution:
        snapshot = snapshot_of(self._history, check_as_of(as_of, self._head))
        if include_inferred:
            return snapshot
        kept = tuple(c for c in snapshot.claims if not is_inferred(c.assertion_kind))
        return Resolution(kept, _naming(snapshot.findings, kept))

    def node(self, node: NodeRef, as_of: LedgerTx) -> Knowledge[NodeView]:
        snapshot = self._snapshot(as_of)
        out = _by_id(c for c in snapshot.claims if c.subject == node)
        incoming = _by_id(c for c in snapshot.claims if c.object == node)
        if not out and not incoming:
            return NotCovered()
        findings = _naming(snapshot.findings, (*out, *incoming))
        return Known(NodeView(node, check_as_of(as_of, self._head), out, incoming, findings))

    def claims(
        self,
        subject: NodeRef,
        predicate: str | None,
        as_of: LedgerTx,
        during: Interval | None = None,
        *,
        include_inferred: bool = True,
    ) -> ClaimsResult:
        snapshot = self._snapshot(as_of, include_inferred)
        about = [
            c
            for c in snapshot.claims
            if c.subject == subject and (predicate is None or c.predicate == predicate)
        ]
        if during is None:
            matched, elsewhere = about, []
        else:
            same_clock = [c for c in about if c.valid.domain_id == during.domain_id]
            matched = [c for c in same_clock if c.valid.overlaps(during)]
            elsewhere = [c for c in about if c.valid.domain_id != during.domain_id]
        return ClaimsResult(
            as_of=check_as_of(as_of, self._head),
            claims=_by_id(matched),
            other_clocks=_by_id(elsewhere),
            findings=_naming(snapshot.findings, (*matched, *elsewhere)),
        )

    def neighbours(
        self, node: NodeRef, hops: int, as_of: LedgerTx, *, include_inferred: bool = True
    ) -> NeighboursResult:
        if isinstance(hops, bool) or not isinstance(hops, int) or hops < 0:
            raise ValueError(f"hops must be a non-negative int: {hops!r}")
        snapshot = self._snapshot(as_of, include_inferred)
        edges: dict[NodeRef, list[tuple[NodeRef, Claim]]] = {}
        for claim in _by_id(snapshot.claims):
            if isinstance(claim.object, NodeRef) and claim.object != claim.subject:
                edges.setdefault(claim.subject, []).append((claim.object, claim))
                edges.setdefault(claim.object, []).append((claim.subject, claim))
        # Breadth first, level by level; each level expands its nodes in key order and their
        # edges in claim-id order, so the reference's ``via`` is deterministic. Other readers may
        # return any shortest path (the contract checks depth and path validity, not the choice).
        paths: dict[NodeRef, tuple[Claim, ...]] = {node: ()}
        level = [node]
        for _ in range(hops):
            reached: list[NodeRef] = []
            for here in sorted(level, key=_node_key):
                for there, claim in edges.get(here, ()):
                    if there not in paths:
                        paths[there] = (*paths[here], claim)
                        reached.append(there)
            level = reached
        found = tuple(
            Neighbour(n, len(via), via)
            for n, via in sorted(paths.items(), key=lambda i: (len(i[1]), _node_key(i[0])))
            if n != node
        )
        via_claims = [c for n in found for c in n.via]
        return NeighboursResult(
            node,
            hops,
            check_as_of(as_of, self._head),
            found,
            _naming(snapshot.findings, via_claims),
        )

    def episodes(self, filter: EpisodeFilter) -> Knowledge[tuple[EpisodeView, ...]]:
        check_as_of(filter.as_of, self._head)
        return NotCovered()  # G3: episode structure is not built; never an empty "fact"

    def spatial(self, site: NodeRef, frame: FrameRef, as_of: LedgerTx) -> Knowledge[SpatialView]:
        check_as_of(as_of, self._head)
        return NotCovered()  # G3: spatial structure is not built; never an empty "fact"

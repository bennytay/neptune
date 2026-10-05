"""Query-time identity traversal over any ``MemoryReader`` (ADR 0003 §1.4, ADR 0008 §5).

Memory never merges nodes: two threads that name one robot stay two nodes joined by ``same_as``
edges, and a consumer that wants "everything about this robot" follows them. This is that walk,
built only on ``MemoryReader.node`` so it works over the reference reader and any store.

- It follows ``same_as`` in either direction (the edge is stored once, subject = the lower id).
- It follows ``same_as_candidate`` only when asked: a candidate is an ambiguity, never an identity.
- ``depth`` bounds the walk; each node comes back once, at its shortest depth, with the claims of
  one shortest path (``Neighbour.via``) so every hop carries its evidence and valid interval.
- It reads one snapshot (``as_of``) and filters nothing by valid time: the ``via`` claims say when
  each identity holds, on their own clocks, which are never compared here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from neptune.model.knowledge import Known
from neptune_memory.schema.nodes import NodeRef
from neptune_memory.schema.predicates import SAME_AS, SAME_AS_CANDIDATE
from neptune_memory.schema.reader import Neighbour

if TYPE_CHECKING:
    from neptune_memory.schema.claim import Claim
    from neptune_memory.schema.interval import LedgerTx
    from neptune_memory.schema.reader import MemoryReader

# Deep enough for a re-commissioned machine's chain of lineage records, small enough to bound a
# walk through a hub. Callers set their own.
DEFAULT_SAME_AS_DEPTH: Final = 8


def _order(node: NodeRef) -> tuple[str, str]:
    return (str(node.node_type), node.node_id)


def same_as_closure(
    reader: MemoryReader,
    node: NodeRef,
    as_of: LedgerTx,
    *,
    depth: int = DEFAULT_SAME_AS_DEPTH,
    include_candidates: bool = False,
    include_inferred: bool = True,
) -> tuple[Neighbour, ...]:
    """The nodes ``node`` is ``same_as`` within ``depth`` hops at ``as_of``, ``node`` excluded.

    Sorted by ``(depth, node_type, node_id)``. ``include_candidates`` also follows
    ``same_as_candidate`` edges (so a result may be a candidate, never an identity: its ``via``
    says which). ``include_inferred=False`` ignores inferred claims, which can only be candidates.
    A node the graph does not name has no identities: the result is empty.
    """
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
        raise ValueError(f"depth must be a non-negative int, got {depth!r}")
    follow = {SAME_AS, SAME_AS_CANDIDATE} if include_candidates else {SAME_AS}
    found: dict[NodeRef, Neighbour] = {}
    seen: set[NodeRef] = {node}
    frontier: list[tuple[NodeRef, tuple[Claim, ...]]] = [(node, ())]
    for level in range(1, depth + 1):
        reached: dict[NodeRef, tuple[Claim, ...]] = {}
        for here, path in frontier:
            view = reader.node(here, as_of, include_inferred=include_inferred)
            if not isinstance(view, Known):
                continue
            edges = (*view.value.claims, *view.value.incoming)
            for claim in sorted(edges, key=lambda c: c.id):
                if claim.predicate not in follow:
                    continue
                other = claim.object if claim.subject == here else claim.subject
                if isinstance(other, NodeRef) and other not in seen and other not in reached:
                    reached[other] = (*path, claim)
        if not reached:
            break
        frontier = sorted(reached.items(), key=lambda item: _order(item[0]))
        for other, via in frontier:
            seen.add(other)
            found[other] = Neighbour(other, level, via)
    return tuple(sorted(found.values(), key=lambda n: (n.depth, *_order(n.node))))

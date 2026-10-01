"""Neo4j 5 :class:`MemoryStore`: an interface stub, kept so the store stays swappable.

Neo4j lost the MVL-104 benchmark (ADR 0004). Every method raises ``NotImplementedError`` naming the
ADR. The Cypher the benchmark measured lives in ``packages/neptune-memory/bench/neo4j_bench.py`` and
is the starting point if the decision is ever revisited.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from neptune_memory.store.records import (
        AsOf,
        ClaimEmbedding,
        ClaimRecord,
        Neighbour,
        VectorHit,
    )

UNSUPPORTED = (
    "Neo4jStore is an interface stub: the claim graph store is PostgreSQL + pgvector "
    "(packages/neptune-memory/docs/adr/0004-claim-graph-store.md). Implement this class only "
    "with a superseding ADR."
)


def _unsupported() -> NoReturn:
    raise NotImplementedError(UNSUPPORTED)


class Neo4jStore:
    """Placeholder with the :class:`MemoryStore` shape; constructing it works, using it does not."""

    def __init__(self, uri: str = "bolt://localhost:7687", *, database: str = "neo4j") -> None:
        self.uri = uri
        self.database = database

    def write_claims(self, claims: Iterable[ClaimRecord]) -> int:
        _unsupported()

    def supersede(self, old_claim_id: int, new: ClaimRecord) -> None:
        _unsupported()

    def as_of_thread(self, subject: str, at: AsOf) -> list[ClaimRecord]:
        _unsupported()

    def neighbours(self, start: str, hops: int, at: AsOf) -> list[Neighbour]:
        _unsupported()

    def write_embeddings(self, embeddings: Iterable[ClaimEmbedding]) -> int:
        _unsupported()

    def vector_top_k(
        self,
        query: Sequence[float],
        k: int,
        *,
        within: tuple[str, int, AsOf] | None = None,
    ) -> list[VectorHit]:
        _unsupported()

    def rebuild(self) -> None:
        _unsupported()

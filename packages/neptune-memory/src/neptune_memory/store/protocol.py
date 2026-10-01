"""The ``MemoryStore`` seam: what Memory needs from a persistence engine, and nothing more.

Chosen implementation: :class:`neptune_memory.store.postgres.PostgresStore` (PostgreSQL 16 +
pgvector, queried with SQL). Kept as a swappable stub:
:class:`neptune_memory.store.neo4j.Neo4jStore`. The decision and the measurements behind it are
in ``docs/adr/0004-claim-graph-store.md``.

Provisional: this interface changes in G2 (MVL-105). Claim ids become ADR 0002's ``ClaimId``
(``claim:<sha256>``) in every signature, and ``supersede(old, new)`` is replaced by an atomic write
of one ADR 0002/0005 resolution (several closures plus new claims at one transaction time).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from neptune_memory.store.records import (
        AsOf,
        ClaimEmbedding,
        ClaimRecord,
        Neighbour,
        VectorHit,
    )


@runtime_checkable
class MemoryStore(Protocol):
    """Bi-temporal claim graph persistence.

    Every read takes an :class:`AsOf`: a claim is visible when its valid interval on the named clock
    contains ``valid_at`` and its transaction interval ``[recorded_at, superseded_at)`` contains
    ``known_at`` (:meth:`ClaimRecord.visible` is the reference). Writes are append-only except that
    superseding closes the old claim's transaction interval; nothing is deleted or rewritten.
    """

    def write_claims(self, claims: Iterable[ClaimRecord]) -> int:
        """Append claims in one transaction; return how many were written."""
        ...

    def supersede(self, old_claim_id: int, new: ClaimRecord) -> None:
        """Atomically append ``new`` and close ``old_claim_id`` at ``new.recorded_at``.

        Raises ``ValueError`` if ``new.supersedes != old_claim_id`` and ``LookupError`` if the old
        claim does not exist or is already superseded.
        """
        ...

    def as_of_thread(self, subject: str, at: AsOf) -> list[ClaimRecord]:
        """Every claim about ``subject`` visible at ``at``, ordered by predicate."""
        ...

    def neighbours(self, start: str, hops: int, at: AsOf) -> list[Neighbour]:
        """Entities within ``hops`` edges of ``start`` (either way) over edges visible at ``at``."""
        ...

    def write_embeddings(self, embeddings: Iterable[ClaimEmbedding]) -> int:
        """Append claim embeddings; return how many were written."""
        ...

    def vector_top_k(
        self,
        query: Sequence[float],
        k: int,
        *,
        within: tuple[str, int, AsOf] | None = None,
    ) -> list[VectorHit]:
        """Nearest ``k`` claim embeddings, optionally restricted to claims whose subject is within
        ``within = (anchor, hops, at)`` of ``anchor`` in the graph as of ``at``."""
        ...

    def rebuild(self) -> None:
        """Drop and recreate every derived structure (indexes, graph projection, vector index) from
        the claim and embedding tables, which are the only source of truth."""
        ...

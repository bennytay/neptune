"""Claim history by id (ADR 0010 §2): what ``why`` needs and Memory's reader does not offer.

``MemoryReader`` answers per node, per subject and by neighbourhood; it has no lookup by claim
id, and a claim id is a content hash, so nothing in it says where to look. ``why(claim_id)`` and
a transaction ``diff``'s supersession chains need one. ``ClaimHistory`` is that seam: the stored
version of a claim (with its real ``superseded_at``) and the versions that list it in
``supersedes``. ``IndexedReader`` is Memory's reference reader over a graph document with that
index added; it is built only from the document Memory's codec decoded (public schema types),
so it reads nothing Memory does not publish. A reader without the seam still answers queries;
``why`` then says, in a gap, that it cannot look a claim up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from neptune_memory.schema.reference import ReferenceReader

if TYPE_CHECKING:
    from neptune_memory.schema.claim import Claim
    from neptune_memory.schema.codec import GraphDocument


@runtime_checkable
class ClaimHistory(Protocol):
    """Claim versions by id, across every transaction the reader knows (read-only)."""

    def version(self, claim_id: str) -> Claim | None:
        """The stored version with ``claim_id``, ``superseded_at`` as recorded (not masked);
        ``None`` when Memory holds no such claim."""
        ...

    def superseded_by(self, claim_id: str) -> tuple[Claim, ...]:
        """The stored versions whose ``supersedes`` names ``claim_id``, sorted by id."""
        ...


class IndexedReader(ReferenceReader):
    """Memory's reference reader over ``document``, plus ``ClaimHistory`` by id."""

    def __init__(self, document: GraphDocument) -> None:
        super().__init__(document)
        claims = document.resolution.claims
        self._index: dict[str, Claim] = {c.id: c for c in claims}
        later: dict[str, list[Claim]] = {}
        for claim in claims:
            for old in claim.supersedes:
                later.setdefault(old, []).append(claim)
        self._later = {k: tuple(sorted(v, key=lambda c: c.id)) for k, v in later.items()}

    def version(self, claim_id: str) -> Claim | None:
        return self._index.get(claim_id)

    def superseded_by(self, claim_id: str) -> tuple[Claim, ...]:
        return self._later.get(claim_id, ())

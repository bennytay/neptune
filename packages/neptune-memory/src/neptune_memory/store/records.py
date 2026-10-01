"""Row shapes the store persists: one bi-temporal claim, one claim embedding, one traversal hit.

These mirror the Memory claim model's fields (subject, predicate, object, valid interval on a named
clock, transaction interval, assertion kind, provenance, supersedes) as flat persistence rows. The
claim model itself is ``neptune_memory.schema`` (ADR 0002); the store maps to it, never the other
way round. Gaps until that mapping lands (ADR 0004): integer claim ids, a single ``supersedes``.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Final, Literal

AssertionKind = Literal["observed", "stated", "inferred"]
ASSERTION_KINDS: tuple[AssertionKind, ...] = ("observed", "stated", "inferred")


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    """One claim as stored.

    Times are integers on a named clock and are never converted: ``valid_from``/``valid_to`` are on
    ``valid_clock``; ``recorded_at``/``superseded_at`` are on the store's transaction clock. The
    valid interval is half-open ``[valid_from, valid_to)``; ``valid_to is None`` means the claim is
    open-ended ("until further notice"), not that its end is unknown. ``superseded_at is None``
    means no later claim has replaced it. Exactly one of ``object_entity`` / ``object_value`` is
    set: an entity-valued claim is also a graph edge ``subject -> object_entity``.
    """

    claim_id: int
    subject: str
    predicate: str
    object_entity: str | None
    object_value: str | None
    valid_clock: str
    valid_from: int
    valid_to: int | None
    recorded_at: int
    superseded_at: int | None
    assertion_kind: AssertionKind
    source_id: str
    transform_id: str
    supersedes: int | None = None

    def __post_init__(self) -> None:
        if (self.object_entity is None) == (self.object_value is None):
            raise ValueError(f"claim {self.claim_id}: exactly one of object_entity/object_value")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError(f"claim {self.claim_id}: empty valid interval")
        if self.superseded_at is not None and self.superseded_at < self.recorded_at:
            raise ValueError(f"claim {self.claim_id}: superseded before it was recorded")
        if self.assertion_kind not in ASSERTION_KINDS:
            raise ValueError(f"claim {self.claim_id}: unknown assertion_kind")

    def visible(self, *, valid_clock: str, valid_at: int, known_at: int) -> bool:
        """Reference semantics of the bi-temporal as-of predicate every store must match."""
        return (
            self.valid_clock == valid_clock
            and self.valid_from <= valid_at
            and (self.valid_to is None or valid_at < self.valid_to)
            and self.recorded_at <= known_at
            and (self.superseded_at is None or known_at < self.superseded_at)
        )


#: Column order shared by the claim table, the benchmark CSV and ``ClaimRecord(*row)``.
CLAIM_COLUMNS: Final = tuple(f.name for f in fields(ClaimRecord))


@dataclass(frozen=True, slots=True)
class ClaimEmbedding:
    """A vector for one claim, keyed by the claim; ``subject`` is denormalised for graph filters."""

    claim_id: int
    subject: str
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class AsOf:
    """A bi-temporal coordinate: valid time on a named clock, and transaction ("known at") time."""

    valid_clock: str
    valid_at: int
    known_at: int


@dataclass(frozen=True, slots=True)
class Neighbour:
    """An entity reached by a traversal, its shortest depth, and the claim ids of one such path."""

    entity: str
    depth: int
    via: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class VectorHit:
    """One nearest-neighbour result: the claim and its distance to the query (smaller is nearer)."""

    claim_id: int
    distance: float

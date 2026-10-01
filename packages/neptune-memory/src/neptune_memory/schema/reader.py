"""``MemoryReader``: the read protocol Context, Deploy and Learn consume (ADR 0006).

Every answer is a snapshot *as of* one Ledger transaction, presented exactly as it was known
then: every claim and finding in it was current at ``as_of``, so each has ``superseded_at = OPEN``
(``schema.as_of`` masks later supersessions; ADR 0006 §6). Results are typed: a result's claims
are ``schema.Claim``s, so provenance (evidence, Ledger records, transform, model) is always
present, and the resolver findings active at ``as_of`` that name a returned claim come with them.

Nothing here assumes a store. ``schema.reference.ReferenceReader`` is the in-memory reference
built on ``resolve``/``as_of``; ``contract.suite`` checks any reader against it. ``MemoryStore``
(``store/``) is the provisional write-and-persist seam and is separate (ADR 0004 §5).

``episodes`` and ``spatial`` are provisional (G3): their signatures and result types are fixed,
and until G3 a reader answers ``NotCovered``, never an empty result that reads as a fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from neptune_memory.schema.interval import LedgerTx, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from neptune.model.frames import FrameRef
    from neptune.model.ids import ConfigHash
    from neptune.model.knowledge import Knowledge
    from neptune_memory.schema.claim import Claim
    from neptune_memory.schema.interval import Interval
    from neptune_memory.schema.supersede import ResolutionFinding


class AsOfBeyondHeadError(ValueError):
    """``as_of`` is later than every transaction the reader knows. A snapshot is answered only
    once it is complete, so the same ``as_of`` always gets the same answer."""

    def __init__(self, as_of: LedgerTx, head: LedgerTx) -> None:
        super().__init__(f"as_of {as_of} is beyond the reader's head transaction {head}")
        self.as_of = as_of
        self.head = head


def check_as_of(as_of: LedgerTx, head: LedgerTx) -> LedgerTx:
    """Validate a query's transaction: a ``LedgerTx`` no later than ``head``."""
    tx = ledger_tx(as_of)
    if tx > head:
        raise AsOfBeyondHeadError(tx, head)
    return tx


@dataclass(frozen=True)
class NodeView:
    """A node as of a transaction: the claims about it (subject) and pointing at it (object)."""

    node: NodeRef
    as_of: LedgerTx
    claims: tuple[Claim, ...]  # node is the subject; sorted by id
    incoming: tuple[Claim, ...]  # node is the object (edges into it); sorted by id
    findings: tuple[ResolutionFinding, ...]  # active at as_of, naming any claim above


@dataclass(frozen=True)
class ClaimsResult:
    """Claims about one subject as of a transaction.

    ``claims`` match the query; ``other_clocks`` match subject and predicate but are on a clock
    other than ``during``'s, so they could not be compared with it (never coerced, never
    dropped). Both are sorted by claim id. ``findings`` are the findings active at ``as_of`` that
    name any claim in either, in resolver order ``(recorded_at, claim, code, others)``.
    """

    as_of: LedgerTx
    claims: tuple[Claim, ...]
    other_clocks: tuple[Claim, ...]
    findings: tuple[ResolutionFinding, ...]


@dataclass(frozen=True)
class Neighbour:
    """A node reached by traversal, its shortest depth, and the claims of one shortest path."""

    node: NodeRef
    depth: int
    via: tuple[Claim, ...]  # len(via) == depth; via[0] touches the start node


@dataclass(frozen=True)
class NeighboursResult:
    """Nodes within ``hops`` edges of ``start`` (either direction) as of a transaction.

    Edges are claims whose object is a node. ``neighbours`` exclude ``start`` and are sorted by
    ``(depth, node_type, node_id)``; ``findings`` are those active at ``as_of`` naming a ``via``
    claim.
    """

    start: NodeRef
    hops: int
    as_of: LedgerTx
    neighbours: tuple[Neighbour, ...]
    findings: tuple[ResolutionFinding, ...]


@dataclass(frozen=True)
class EpisodeFilter:
    """Provisional (G3). Which episodes: of a run, executing a task, overlapping ``during``."""

    as_of: LedgerTx
    run: NodeRef | None = None
    task: NodeRef | None = None
    during: Interval | None = None

    def __post_init__(self) -> None:
        ledger_tx(self.as_of)
        if self.run is not None and self.run.node_type is not NodeType.RUN:
            raise ValueError(f"run must be a run node: {self.run!r}")
        if self.task is not None and self.task.node_type is not NodeType.TASK:
            raise ValueError(f"task must be a task node: {self.task!r}")


@dataclass(frozen=True)
class EpisodeView:
    """Provisional (G3). One episode node and the claims about it as of the filter's ``as_of``."""

    episode: NodeRef
    claims: tuple[Claim, ...]


@dataclass(frozen=True)
class SpatialView:
    """Provisional (G3). What is placed in ``frame`` at ``site`` as of a transaction."""

    site: NodeRef
    frame: FrameRef
    as_of: LedgerTx
    claims: tuple[Claim, ...]


@runtime_checkable
class MemoryReader(Protocol):
    """Read access to one generation of the claim graph (ADR 0006).

    Deterministic: the same reader state and arguments give equal results. Every ``as_of`` must
    be a ``LedgerTx`` no later than ``head`` (else ``AsOfBeyondHeadError``). A reader never
    returns a claim or finding that was not active at ``as_of``, and presents each as known then.
    """

    @property
    def graph_schema_version(self) -> int:
        """The graph-schema major this reader implements (``schema.GRAPH_SCHEMA_VERSION``)."""
        ...

    @property
    def generation(self) -> ConfigHash:
        """The resolver configuration hash the graph was resolved under (ADR 0006 §7). A change
        means a new store generation: claim and finding ids are not comparable across it."""
        ...

    @property
    def head(self) -> LedgerTx:
        """The latest Ledger transaction the reader knows."""
        ...

    def node(self, node: NodeRef, as_of: LedgerTx) -> Knowledge[NodeView]:
        """``Known(NodeView)`` when some claim active at ``as_of`` names ``node``; else
        ``NotCovered``: the graph says nothing about it, which is not the node's absence."""
        ...

    def claims(
        self,
        subject: NodeRef,
        predicate: str | None,
        as_of: LedgerTx,
        during: Interval | None = None,
        *,
        include_inferred: bool = True,
    ) -> ClaimsResult:
        """Claims about ``subject`` (with ``predicate``, or any) active at ``as_of``.

        ``during``: keep claims whose valid interval overlaps it on its clock; claims on other
        clocks go to ``other_clocks``. ``include_inferred=False`` keeps only observed and stated
        claims (and the findings naming them).
        """
        ...

    def neighbours(
        self, node: NodeRef, hops: int, as_of: LedgerTx, *, include_inferred: bool = True
    ) -> NeighboursResult:
        """Nodes within ``hops`` (>= 0) edges of ``node``, either direction, as of ``as_of``."""
        ...

    def episodes(self, filter: EpisodeFilter) -> Knowledge[tuple[EpisodeView, ...]]:
        """Provisional (G3): ``NotCovered`` until episode structure lands."""
        ...

    def spatial(self, site: NodeRef, frame: FrameRef, as_of: LedgerTx) -> Knowledge[SpatialView]:
        """Provisional (G3): ``NotCovered`` until spatial structure lands."""
        ...

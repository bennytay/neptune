"""The planner behind the SDK and the MCP tool ``neptune_plan`` (ADR 0005, ADR 0009 §4).

``Planner(resolver, defaults, model)`` binds the natural-language planner to one entity resolver,
one caller's declared defaults and one model client; ``Client(..., planner=...)`` exposes it as
``plan``, ``choose`` and ``ask``. A plan is a proposal, never an answer: ``ask`` returns the plan
beside the packet, and runs the planned query only when the plan is ``ready``.

``entity_index(document)`` is the Demo v1 resolver: a ``DeclaredIdentifierIndex`` over the
declared identifiers a Memory graph document names (``asset-tag:ARM-3A``, ``site-code:PLANT-2``),
because the catalog API cannot list declared identifiers yet (ADR 0005 §3). Content-addressed
node ids (runs, events, clocks: ``record:rec:sha256:...``) are not names anyone types and are left
out, and so is an identifier the graph declares under two kinds (never settled silently). Only
names that stated or observed claims current at the snapshot mention are offered.
``NoModel`` stands in when no model is configured: every plan is then a visible ``failed`` plan
with ``model_unavailable``, never a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from neptune_memory.schema.claim import is_inferred
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.nodes import NodeRef
from neptune_memory.schema.supersede import as_of as snapshot_of

from neptune_context.pinned import node_types
from neptune_context.query.model import HEAD, AsOf, Query
from neptune_context.query.plan import (
    DeclaredIdentifierIndex,
    Defaults,
    Entity,
    EntityResolver,
    Mention,
    ModelClient,
    ModelUnavailable,
    PlannedQuery,
    choose,
    plan,
)
from neptune_context.sdk.errors import ErrorCode, SdkError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from neptune_memory.schema.codec import GraphDocument

    from neptune_context.packets.model import ContextPacket
    from neptune_context.query.plan import ModelRequest, ModelResponse

MAX_ENTITIES: Final = 10_000
_SNAPSHOTS_CACHED: Final = 16
# A declared identifier: a namespace, a colon, a value; never a content address.
_DECLARED: Final = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*:[^\s:][^\s]*")


class NoModel:
    """A model client for a server started without a planner model: every request is
    ``ModelUnavailable``, so the plan says so instead of guessing."""

    client_id: Final = "none"

    def complete(self, request: ModelRequest) -> ModelResponse:
        raise ModelUnavailable(
            "no planner model is configured for this server; write the typed query and use "
            "neptune_query, or start the server with --planner anthropic"
        )


@dataclass(frozen=True)
class Planner:
    """The planner for one caller: names resolve through ``resolver``, ``defaults`` are the
    caller's declared defaults (stated back on every plan) and ``model`` is asked once per plan."""

    resolver: EntityResolver
    defaults: Defaults
    model: ModelClient = field(default_factory=NoModel)

    def plan(self, question: str, as_of: AsOf = HEAD) -> PlannedQuery:
        return plan(question, as_of, self.defaults, resolver=self.resolver, client=self.model)

    def choose(self, planned: PlannedQuery, mention: str, declared_id: str) -> PlannedQuery:
        try:
            return choose(
                planned, mention, declared_id, resolver=self.resolver, defaults=self.defaults
            )
        except ValueError as error:
            raise SdkError(ErrorCode.INVALID_ARGUMENT, str(error)) from None

    def find(
        self, text: str, *, as_of: int | None = None, include_inferred: bool = False
    ) -> tuple[Mention, ...]:
        """Every declared name in ``text`` with all its candidates at ``as_of`` (``None``: the
        head); ambiguity is never settled. Inferred-only names need ``include_inferred``."""
        if isinstance(self.resolver, GraphEntities):
            return self.resolver.find(text, as_of=as_of, include_inferred=include_inferred)
        return tuple(self.resolver.find(text, as_of=as_of))

    def conflicts(
        self, *, as_of: int | None = None, include_inferred: bool = False
    ) -> tuple[str, ...]:
        """Identifiers the resolver declines to offer as names (declared under several kinds)."""
        if isinstance(self.resolver, GraphEntities):
            return self.resolver.conflicts(as_of, include_inferred=include_inferred)
        return tuple(getattr(self.resolver, "conflicts", ()))

    def entities(
        self, kind: str | None = None, *, as_of: int | None = None, include_inferred: bool = False
    ) -> tuple[Entity, ...]:
        """The declared identities the resolver can list at ``as_of`` (by kind, then id);
        ``()`` when it cannot list (a resolver backed by a store with no listing call)."""
        found: tuple[Entity, ...]
        if isinstance(self.resolver, GraphEntities):
            found = self.resolver.entities(as_of, include_inferred=include_inferred)
        elif isinstance(self.resolver, ListingIndex):
            found = self.resolver.entities()
        else:
            found = ()
        return tuple(e for e in found if kind is None or e.kind == kind)


@dataclass(frozen=True)
class Asked:
    """``ask``'s answer: the plan, always, and the packet for its query when it was ready."""

    plan: PlannedQuery
    packet: ContextPacket | None

    @property
    def query(self) -> Query | None:
        return self.plan.query if self.packet is not None else None


def _declared(node: NodeRef) -> Entity | None:
    if str(node.node_type) not in node_types() or not _DECLARED.fullmatch(node.node_id):
        return None
    if "sha256:" in node.node_id:  # a content address, not a declared name
        return None
    return Entity(str(node.node_type), node.node_id)


def entity_index(document: GraphDocument) -> GraphEntities:
    """The declared identities a Memory graph document names, as an ``as_of``-aware resolver."""
    return GraphEntities(document)


class ListingIndex(DeclaredIdentifierIndex):
    """A ``DeclaredIdentifierIndex`` that can also list what it holds (``neptune_entities``),
    and the identifiers it left out because the graph declares them under several kinds."""

    def __init__(self, entities: Iterable[Entity], *, conflicts: Iterable[str] = ()) -> None:
        listed = tuple(entities)
        super().__init__(listed)
        self._listed: tuple[Entity, ...] = tuple(
            sorted(listed, key=lambda e: (e.kind, e.declared_id))
        )
        self.conflicts: tuple[str, ...] = tuple(sorted(conflicts))

    def entities(self) -> tuple[Entity, ...]:
        return self._listed


class GraphEntities:
    """An ``EntityResolver`` over one Memory graph document, at the snapshot each call names.

    An identifier is offered only when a claim **current at** ``as_of`` (recorded by then, not
    yet superseded) names it as subject or object, and that claim is ``stated`` or ``observed``.
    A name that only inferred claims mention is left out unless ``include_inferred`` is set; a
    name only superseded claims mention is not current and is never offered. Every node counts
    whatever the claim says: a declared identifier is a name, not a fact. An identifier under
    several node kinds at that snapshot is left out and named in ``conflicts``. At most
    ``MAX_ENTITIES`` per snapshot; beyond that is ``ValueError`` (a graph that size needs the
    Ledger's listing call, not a scan). ``as_of`` beyond the document's head is ``not_found``.
    """

    def __init__(self, document: GraphDocument) -> None:
        self._history = document.resolution
        self._head = int(document.head)
        self._cache: dict[tuple[int, bool], ListingIndex] = {}

    def index(self, as_of: int | None = None, *, include_inferred: bool = False) -> ListingIndex:
        tx = self._head if as_of is None else int(as_of)
        if tx > self._head:
            raise SdkError(
                ErrorCode.NOT_FOUND,
                f"as_of {tx} is beyond the latest transaction this graph holds ({self._head})",
            )
        key = (tx, include_inferred)
        found = self._cache.get(key)
        if found is None:
            if len(self._cache) >= _SNAPSHOTS_CACHED:
                self._cache.pop(next(iter(self._cache)))
            found = self._cache[key] = self._build(tx, include_inferred)
        return found

    def _build(self, tx: int, include_inferred: bool) -> ListingIndex:
        kinds: dict[str, set[str]] = {}
        for claim in snapshot_of(self._history, ledger_tx(tx)).claims:
            if is_inferred(claim.assertion_kind) and not include_inferred:
                continue
            for node in (claim.subject, claim.object):
                if isinstance(node, NodeRef) and (entity := _declared(node)) is not None:
                    kinds.setdefault(entity.declared_id, set()).add(entity.kind)
        if len(kinds) > MAX_ENTITIES:
            raise ValueError(f"more than {MAX_ENTITIES} declared identities; use a Ledger listing")
        # One identifier under two kinds is two identities memory has not told apart: offering
        # either would settle that silently, so neither is a name (they are listed as conflicts).
        conflicts = tuple(sorted(i for i, k in kinds.items() if len(k) > 1))
        single = (Entity(next(iter(k)), i) for i, k in sorted(kinds.items()) if len(k) == 1)
        return ListingIndex(single, conflicts=conflicts)

    # EntityResolver (the planner's seam): evidence only, at the plan's snapshot.
    def find(
        self, text: str, *, as_of: int | None, include_inferred: bool = False
    ) -> tuple[Mention, ...]:
        return self.index(as_of, include_inferred=include_inferred).find(text, as_of=as_of)

    def lookup(
        self, declared_id: str, *, as_of: int | None, include_inferred: bool = False
    ) -> Entity | None:
        index = self.index(as_of, include_inferred=include_inferred)
        return index.lookup(declared_id, as_of=as_of)

    def entities(
        self, as_of: int | None = None, *, include_inferred: bool = False
    ) -> tuple[Entity, ...]:
        return self.index(as_of, include_inferred=include_inferred).entities()

    def conflicts(
        self, as_of: int | None = None, *, include_inferred: bool = False
    ) -> tuple[str, ...]:
        return self.index(as_of, include_inferred=include_inferred).conflicts

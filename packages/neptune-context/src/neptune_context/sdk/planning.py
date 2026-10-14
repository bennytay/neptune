"""The planner behind the SDK and the MCP tool ``neptune_plan`` (ADR 0005, ADR 0009 §4).

``Planner(resolver, defaults, model)`` binds the natural-language planner to one entity resolver,
one caller's declared defaults and one model client; ``Client(..., planner=...)`` exposes it as
``plan``, ``choose`` and ``ask``. A plan is a proposal, never an answer: ``ask`` returns the plan
beside the packet, and runs the planned query only when the plan is ``ready``.

``entity_index(document)`` is the Demo v1 resolver: a ``DeclaredIdentifierIndex`` over the
declared identifiers a Memory graph document names (``servicenow.ci:ARM-3A``, ``manifest:PLANT-2``),
because the catalog API cannot list declared identifiers yet (ADR 0005 §3). Content-addressed
node ids (runs, events, clocks: ``record:rec:sha256:...``) are not names anyone types and are left
out, and so is an identifier the graph declares under two kinds (never settled silently). Only
names that stated or observed claims current at the snapshot mention are offered. Ids joined by
stated or observed ``same_as`` claims are one entity, listed once under its representative (the
least declared id) naming the others (Context ADR 0018); an inferred ``same_as`` joins nothing.
``NoModel`` stands in when no model is configured: every plan is then a visible ``failed`` plan
with ``model_unavailable``, never a guess.
"""

from __future__ import annotations

import dataclasses
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from neptune_memory.schema.claim import TypedLiteral, is_inferred
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
    SameAs,
    choose,
    plan,
)
from neptune_context.sdk.errors import ErrorCode, SdkError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from neptune_memory.schema.claim import Claim
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


_NAMED_KINDS: Final = frozenset({"event", "run"})
SAME_AS: Final = "same_as"


def _joined(entities: list[Entity], same_as: Iterable[Claim]) -> list[Entity]:
    """``entities`` with the ids ``same_as`` evidence joins made one (Context ADR 0018).

    ``same_as`` claims between two nodes of one kind join them; ``same_as`` is an identity, so
    joins are transitive, through nodes that are not offered as names too (an unnamed event).
    Two or more offered entities in one joined group are listed under their representative,
    the least declared id (deterministic, and the same whichever id a caller named), whose
    ``SameAs`` names the others, the group's claims and their sources, and the most
    ``same_as`` steps from an offered id to any node of the group: a query subject at that
    ``same_as_depth`` reads the same nodes whichever offered id it names. Every id stays
    declared on its own."""
    Key = tuple[str, str]
    neighbours: dict[Key, set[Key]] = defaultdict(set)
    edges: dict[str, tuple[Claim, Key]] = {}
    for claim in same_as:
        a, b = claim.subject, claim.object
        if not isinstance(b, NodeRef) or a == b or a.node_type != b.node_type:
            continue
        left, right = (str(a.node_type), a.node_id), (str(b.node_type), b.node_id)
        neighbours[left].add(right)
        neighbours[right].add(left)
        edges[claim.id] = (claim, left)
    group: dict[Key, frozenset[Key]] = {}
    for key in sorted(neighbours):
        if key not in group:
            members = frozenset(_reach(key, neighbours))
            group.update(dict.fromkeys(members, members))
    offered: dict[frozenset[Key], list[Entity]] = defaultdict(list)
    for entity in entities:
        found = group.get((entity.kind, entity.declared_id))
        if found is not None:
            offered[found].append(entity)
    out = {e.declared_id: e for e in entities}
    for members, named in offered.items():
        if len(named) < 2:
            continue
        lead, *rest = sorted(named, key=lambda e: e.declared_id)
        inside = sorted((c for c, k in edges.values() if k in members), key=lambda c: c.id)
        out[lead.declared_id] = dataclasses.replace(
            lead,
            same_as=SameAs(
                others=tuple(rest),
                claims=tuple(c.id for c in inside),
                sources=tuple(
                    sorted({str(r.source) for c in inside for r in c.provenance.evidence})
                ),
                depth=max(max(_reach((e.kind, e.declared_id), neighbours).values()) for e in named),
            ),
        )
    return [out[e.declared_id] for e in entities]


def _reach(
    start: tuple[str, str], neighbours: Mapping[tuple[str, str], set[tuple[str, str]]]
) -> dict[tuple[str, str], int]:
    """Every node joined to ``start``, with the fewest ``same_as`` steps to it."""
    steps = {start: 0}
    frontier = [start]
    while frontier:
        reached = sorted({n for f in frontier for n in neighbours[f]} - steps.keys())
        steps.update((n, steps[frontier[0]] + 1) for n in reached)
        frontier = reached
    return steps


def entity_index(document: GraphDocument) -> GraphEntities:
    """The declared identities a Memory graph document names, as an ``as_of``-aware resolver."""
    return GraphEntities(document)


class ListingIndex(DeclaredIdentifierIndex):
    """A ``DeclaredIdentifierIndex`` that can also list what it holds (``neptune_entities``),
    and the identifiers it left out because the graph declares them under several kinds. An id
    a representative's ``same_as`` names is listed under that representative, not on its own."""

    def __init__(self, entities: Iterable[Entity], *, conflicts: Iterable[str] = ()) -> None:
        listed = tuple(entities)
        super().__init__(listed)
        joined = {o.declared_id for e in listed if e.same_as for o in e.same_as.others}
        self._listed: tuple[Entity, ...] = tuple(
            sorted(
                (e for e in listed if e.declared_id not in joined),
                key=lambda e: (e.kind, e.declared_id),
            )
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
        current = snapshot_of(self._history, ledger_tx(tx)).claims
        kinds: dict[str, set[str]] = {}
        for claim in current:
            if is_inferred(claim.assertion_kind) and not include_inferred:
                continue
            for node in (claim.subject, claim.object):
                if isinstance(node, NodeRef) and (entity := _declared(node)) is not None:
                    kinds.setdefault(entity.declared_id, set()).add(entity.kind)
        # A run or an event is keyed by its record (a content address, never a declared name);
        # the name memory states for it (a run sheet's run name, a work order or incident
        # number: ``has_name``) finds it (ADR 0015).
        named: dict[str, tuple[str, set[str]]] = {}
        for claim in current:
            if claim.predicate != "has_name" or (
                is_inferred(claim.assertion_kind) and not include_inferred
            ):
                continue
            node, obj = claim.subject, claim.object
            if str(node.node_type) in _NAMED_KINDS and isinstance(obj, TypedLiteral):
                named.setdefault(node.node_id, (str(node.node_type), set()))[1].add(str(obj.value))
        if len(kinds) + len(named) > MAX_ENTITIES:
            raise ValueError(f"more than {MAX_ENTITIES} declared identities; use a Ledger listing")
        # One identifier under two kinds is two identities memory has not told apart: offering
        # either would settle that silently, so neither is a name (they are listed as conflicts).
        conflicts = tuple(sorted(i for i, k in kinds.items() if len(k) > 1))
        single = [Entity(next(iter(k)), i) for i, k in sorted(kinds.items()) if len(k) == 1]
        held = {e.declared_id for e in single}
        single += [
            Entity(kind, node_id, label=min(names), aliases=tuple(sorted(names - {min(names)})))
            for node_id, (kind, names) in sorted(named.items())
            if node_id not in held
        ]
        # Ids memory states are one entity are listed once (ADR 0018): evidence only, whatever
        # ``include_inferred`` says, so an inference never settles an identity.
        stated = (
            c for c in current if c.predicate == SAME_AS and not is_inferred(c.assertion_kind)
        )
        return ListingIndex(_joined(single, stated), conflicts=conflicts)

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

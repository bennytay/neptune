"""Does this packet answer this query? Checks only a holder of both halves can run (ADR 0006 §2).

A packet names its query only by ``query_id`` (ADR 0003 §1), so the packet model cannot see what
was asked. A caller that holds the query can, and a wrong answer must be loud at that boundary,
never a wrong fact downstream. ``answer_problems(query, packet)`` returns every way the packet
fails to answer the query, empty when it does:

- it names this query (``query_id``), the snapshot an integer ``as_of`` pinned, and the query's
  ``include_inferred``;
- it echoes the query's budget exactly, so an engine cannot widen a limit and stay "in budget";
- its ``during`` is the query's window on the query's clock (a civil time resolved to its
  ``CivilClock.domain_id``), or absent when the query set none;
- with a ``during``, every timed item (a claim's valid interval, a series window, a sensor
  sample's instant) is on that clock or on one the query's ``clock_bridges`` join to it; anything
  else is an ``other_clock`` gap, never an item (ADR 0002 §3);
- every gap points into the query's canonical JSON.

The SDK runs it on every answer (``invalid_response``); Deploy and Learn get it from
``neptune_context.contract``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from neptune_memory.schema.interval import CivilClock

from neptune.model.knowledge import Known
from neptune.model.time import Epoch, Timescale
from neptune_context.packets.model import (
    ClaimItem,
    ContextPacket,
    FrameItem,
    Item,
    Limits,
    SeriesWindowItem,
)
from neptune_context.query.codec import query_id, to_json
from neptune_context.query.model import Clock, DomainClock, Instant, Query

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue


def domain_id(clock: Clock) -> str:
    """The ``TimestampDomain`` id a query clock names: its own, or the civil clock's."""
    if isinstance(clock, DomainClock):
        return clock.domain_id
    return str(
        CivilClock(Timescale(clock.timescale), Epoch(clock.epoch), clock.resolution).domain_id
    )


def _limits(query: Query) -> Limits:
    budget = query.budget
    return Limits(budget.items, budget.tokens, budget.bytes, budget.latency_ms)


def _reachable(query: Query, start: str) -> frozenset[str]:
    """Clocks joined to ``start`` by a chain of the query's clock bridges (domain ids)."""
    edges = [(domain_id(b.source), domain_id(b.target)) for b in query.clock_bridges]
    seen, todo = {start}, [start]
    while todo:
        here = todo.pop()
        for left, right in edges:
            for a, b in ((left, right), (right, left)):
                if a == here and b not in seen:
                    seen.add(b)
                    todo.append(b)
    return frozenset(seen)


def _item_clock(item: Item) -> str | None:
    if isinstance(item, ClaimItem):
        return str(item.claim.valid.domain_id)
    if isinstance(item, SeriesWindowItem):
        return str(item.clock)
    if isinstance(item, FrameItem) and isinstance(item.at, Known):
        return str(item.at.value.domain_id)
    return None


def _resolves(document: JsonValue, pointer: str) -> bool:
    """``pointer`` (RFC 6901) names a member of ``document``."""
    if pointer == "":
        return True
    node = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and str(int(token)) == token:
            if int(token) >= len(node):
                return False
            node = node[int(token)]
        else:
            return False
    return True


def answer_problems(query: Query, packet: ContextPacket) -> tuple[str, ...]:
    """Every way ``packet`` fails to answer ``query`` (a validated query); ``()`` when it does."""
    problems: list[str] = []
    if packet.query_id != query_id(query):
        # Nothing else is comparable: the packet answers another question.
        return ("the packet answers a different query",)
    if isinstance(query.as_of, int) and packet.as_of != query.as_of:
        problems.append("the packet answers a different snapshot")
    if packet.inference_included != query.include_inferred:
        problems.append("the packet disagrees with the query on inferred items")
    if packet.budget.limits != _limits(query):
        problems.append("the packet's budget limits are not the query's")
    if query.during is None:
        if packet.during is not None:
            problems.append("the packet has a world-time window the query did not ask for")
    else:
        asked = query.during
        clock = domain_id(asked.clock)
        got = packet.during
        if got is None or (got.domain_id, got.start, got.end) != (clock, asked.start, asked.end):
            problems.append("the packet's world-time window is not the query's during")
        allowed = set(_reachable(query, clock))
        for explained in query.explain:
            for point in (getattr(explained, "before", None), getattr(explained, "after", None)):
                if isinstance(point, Instant):
                    allowed |= _reachable(query, domain_id(point.clock))
        for item in packet.items:
            on = _item_clock(item)
            if on is not None and on not in allowed:
                problems.append(
                    f"{item.id} is on clock {on}, which the query neither asked for nor bridged:"
                    " it belongs in an other_clock gap, not among the items"
                )
    document = to_json(query)
    for gap in packet.gaps:
        if not _resolves(document, gap.at):
            problems.append(f"a {gap.code} gap points at {gap.at!r}, which the query does not have")
    return tuple(problems)

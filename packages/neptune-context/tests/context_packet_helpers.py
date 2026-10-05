"""Shared helpers for the packet tests: load a golden, rebuild a packet around changed items."""

from __future__ import annotations

import dataclasses
from functools import cache
from typing import TYPE_CHECKING, Any

from context_packet_goldens import PACKETS
from neptune_context.packets.codec import decode
from neptune_context.packets.model import BudgetUse, ContextPacket, Item, Limit, Relevance

if TYPE_CHECKING:
    from pathlib import Path


@cache
def golden(stem: str) -> ContextPacket:
    """The golden packet whose file name starts with ``stem`` (``q01``, ``q03`` ...)."""
    (path,) = sorted(PACKETS.glob(f"{stem}-*.json"))
    packet = decode(path.read_bytes())
    assert isinstance(packet, ContextPacket), packet
    return packet


def golden_path(stem: str) -> Path:
    (path,) = sorted(PACKETS.glob(f"{stem}-*.json"))
    return path


def with_items(packet: ContextPacket, items: list[Item], **changes: Any) -> ContextPacket:
    """``packet`` with ``items`` (re-ordered canonically) and a budget measured for them."""
    ordered = sorted(items, key=lambda i: (-i.relevance.score, i.id))
    limits = changes.pop("limits", packet.budget.limits)
    dropped = changes.pop("dropped", 0)
    exhausted: tuple[Limit, ...] = changes.pop("exhausted", ())
    budget = BudgetUse.measured(limits, ordered, dropped=dropped, exhausted=exhausted)
    return dataclasses.replace(packet, items=tuple(ordered), budget=budget, **changes)


def rescored(item: Item, score: float) -> Item:
    return dataclasses.replace(item, relevance=Relevance(score, item.relevance.hits))

"""Fusion and the budget cut over channel answers (ADR 0007 §4): the v0 stand-in for MVL-145.

``fuse`` merges every channel's hits by item id with reciprocal-rank fusion (k = 60), the rule
the golden packets already use: an item's fused score is the sum over the channels that found it
of ``1 / (k + rank)``, and its relevance keeps every channel's hit. Ranks, not raw scores, are
added, so a channel whose scores run larger is not privileged. ``cut`` keeps the longest prefix of
the fused order that fits the budget and says which limits stopped it. MVL-145 replaces both
(MMR, reranking, explain traces) behind the same two signatures.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from neptune.identity.canonical_json import dumps
from neptune_context.packets.model import ChannelHit, ClaimItem, Limit, Limits, Relevance

if TYPE_CHECKING:
    from collections.abc import Sequence

    from neptune_context.packets.model import Item
    from neptune_context.retrieve.channel import ChannelAnswer

RRF_K: Final = 60


def fuse(answers: Sequence[ChannelAnswer], *, k: int = RRF_K) -> tuple[Item, ...]:
    """Every channel's hits merged by item id, ordered by fused score (descending), then id."""
    hits: dict[str, list[ChannelHit]] = {}
    items: dict[str, Item] = {}
    for channel_answer in answers:
        for item in channel_answer.hits:
            items.setdefault(item.id, item)
            hits.setdefault(item.id, []).extend(item.relevance.hits)
    fused = []
    for item_id, found in hits.items():
        found.sort(key=lambda h: str(h.channel))
        score = float(sum(1.0 / (k + h.rank) for h in found))
        fused.append(replace(items[item_id], relevance=Relevance(score, tuple(found))))
    return tuple(sorted(fused, key=lambda item: (-item.relevance.score, item.id)))


@dataclass(frozen=True)
class Cut:
    """What fits: the kept items, how many candidates were dropped and the limits that cut."""

    kept: tuple[Item, ...]
    dropped: int
    exhausted: tuple[Limit, ...]


def _size(items_bytes: int, count: int) -> int:
    """Bytes of a canonical JSON array of ``count`` members totalling ``items_bytes``."""
    return 2 + items_bytes + max(count - 1, 0)


def cut(items: Sequence[Item], limits: Limits) -> Cut:
    """The longest prefix of ``items`` within ``limits`` (items, bytes, tokens).

    Bytes count the kept items' canonical JSON array and tokens are ``ceil(bytes / 4)``, exactly
    as ``BudgetUse`` measures them. A scene or configuration item whose claims did not all fit is
    dropped with them, so a cut never leaves a dangling reference.
    """
    kept: list[Item] = []
    total = 0
    exhausted: set[Limit] = set()
    for item in items:
        width = len(dumps(item.to_json()))
        size = _size(total + width, len(kept) + 1)
        over = set()
        if len(kept) + 1 > limits.items:
            over.add(Limit.ITEMS)
        if limits.bytes is not None and size > limits.bytes:
            over.add(Limit.BYTES)
        if limits.tokens is not None and -(-size // 4) > limits.tokens:
            over.add(Limit.TOKENS)
        if over:
            exhausted = over
            break
        kept.append(item)
        total += width
    held = {i.claim.id for i in kept if isinstance(i, ClaimItem)}
    whole = tuple(i for i in kept if set(i.claim_refs()) <= held)
    # Channel answers carry every claim their items name, so an item loses a claim only when the
    # budget cut it: ``dropped > 0`` exactly when some limit is exhausted.
    return Cut(whole, len(items) - len(whole), tuple(sorted(exhausted, key=str)))

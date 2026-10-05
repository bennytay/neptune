"""Entity resolution for the planner (ADR 0005 §3): question names to declared identifiers.

The planner never picks an entity itself. ``EntityResolver`` is the injected seam to whatever
holds the Ledger's declared identifiers (``<namespace>:<value>``): ``find`` returns every name in
the question with all of its candidates, and ``lookup`` says whether a declared id exists and what
it declares (its primary clock, frames and the clock and frame relations it names). Several
candidates for one name is an ambiguity the planner reports, never a choice it makes.

``DeclaredIdentifierIndex`` is the in-memory implementation: built from entities a caller loaded
(from the Ledger catalog, a fixture or a test), matching whole tokens case-insensitively.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from neptune_context.query.codec import (
    clock_bridge_to_json,
    clock_to_json,
    frame_bridge_to_json,
    frame_to_json,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from neptune.model.jsonvalue import JsonObject, JsonValue
    from neptune_context.query.model import Clock, ClockBridge, FrameBridge, FrameRef


@dataclass(frozen=True)
class Entity:
    """One declared identity, as the Ledger declares it. Nothing here is inferred.

    ``label`` and ``aliases`` are other names the declaration (or its record) states for the
    entity. ``primary_clock`` is the clock the entity's own records are stamped on, when declared;
    ``frames`` and the two bridge sets are the frames and clock/frame relations declared for it.
    """

    kind: str
    declared_id: str
    label: str | None = None
    aliases: tuple[str, ...] = ()
    primary_clock: Clock | None = None
    frames: tuple[FrameRef, ...] = ()
    clock_bridges: tuple[ClockBridge, ...] = ()
    frame_bridges: tuple[FrameBridge, ...] = ()

    @property
    def value(self) -> str:
        return self.declared_id.split(":", 1)[1]

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {"declared_id": self.declared_id, "kind": self.kind}
        if self.label is not None:
            out["label"] = self.label
        if self.primary_clock is not None:
            out["primary_clock"] = clock_to_json(self.primary_clock)
        if self.frames:
            out["frames"] = [frame_to_json(f) for f in self.frames]
        if self.clock_bridges:
            out["clock_bridges"] = [clock_bridge_to_json(b) for b in self.clock_bridges]
        if self.frame_bridges:
            out["frame_bridges"] = [frame_bridge_to_json(b) for b in self.frame_bridges]
        return out


@dataclass(frozen=True)
class Mention:
    """A name found in the question and every entity it could mean (at least one).

    One candidate is a resolution; several are an ambiguity, shown and never settled here.
    """

    text: str
    candidates: tuple[Entity, ...]

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1

    def to_json(self) -> JsonObject:
        return {
            "candidates": [
                {"declared_id": c.declared_id, "kind": c.kind}
                | ({} if c.label is None else {"label": c.label})
                for c in self.candidates
            ],
            "resolution": "ambiguous" if self.ambiguous else "resolved",
            "text": self.text,
        }


@runtime_checkable
class EntityResolver(Protocol):
    """Names to declared identifiers. ``as_of`` is the Ledger transaction of the snapshot the
    plan is for, or ``None`` for the reader's head. Implementations must be deterministic."""

    def find(self, text: str, *, as_of: int | None) -> Sequence[Mention]:
        """Every entity name in ``text`` with all its candidates, in order of appearance."""
        ...

    def lookup(self, declared_id: str, *, as_of: int | None) -> Entity | None:
        """The entity declared under ``declared_id``, or ``None`` when nothing declares it."""
        ...


def _surface_pattern(surface: str) -> re.Pattern[str]:
    # Whole-token match: not glued to a word character or a hyphen on either side.
    return re.compile(rf"(?<![\w-]){re.escape(surface)}(?![\w-])", re.IGNORECASE)


class DeclaredIdentifierIndex:
    """An ``EntityResolver`` over a fixed set of entities, ignoring ``as_of`` (a snapshot's set).

    A name matches an entity by its declared value, its label or an alias, as whole tokens and
    ignoring case. Longer names are matched first and their span is consumed, so ``AMR-07``
    never also matches ``AMR-0``. Two entities sharing a name are candidates of one mention.
    """

    def __init__(self, entities: Iterable[Entity]) -> None:
        by_id: dict[str, Entity] = {}
        for entity in entities:
            if entity.declared_id in by_id:
                raise ValueError(f"declared id {entity.declared_id!r} is declared twice")
            by_id[entity.declared_id] = entity
        self._by_id = by_id
        surfaces: dict[str, list[Entity]] = {}
        for entity in sorted(by_id.values(), key=lambda e: (e.kind, e.declared_id)):
            for name in {entity.value, *(n for n in (entity.label, *entity.aliases) if n)}:
                bucket = surfaces.setdefault(name.casefold(), [])
                if entity not in bucket:
                    bucket.append(entity)
        self._surfaces = sorted(surfaces.items(), key=lambda kv: (-len(kv[0]), kv[0]))

    def lookup(self, declared_id: str, *, as_of: int | None) -> Entity | None:
        return self._by_id.get(declared_id)

    def find(self, text: str, *, as_of: int | None) -> tuple[Mention, ...]:
        working = text
        hits: list[tuple[int, Mention]] = []
        for surface, entities in self._surfaces:
            pattern = _surface_pattern(surface)
            for match in pattern.finditer(working):
                start, end = match.span()
                hits.append((start, Mention(text[start:end], tuple(entities))))
                working = working[:start] + "\0" * (end - start) + working[end:]
        hits.sort(key=lambda hit: hit[0])
        seen: set[tuple[str, ...]] = set()
        out: list[Mention] = []
        for _, mention in hits:
            key = (mention.text.casefold(), *(c.declared_id for c in mention.candidates))
            if key not in seen:  # a name repeated in the question is one mention
                seen.add(key)
                out.append(mention)
        return tuple(out)

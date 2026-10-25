"""The shared types of the site model (spec §3, §5). Frozen by #181: lanes import, never reshape."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

Kind = Literal["observed", "stated", "inferred"]
EntryType = Literal["fact", "seen", "confirm"]
State = Literal["known", "stale", "unknown", "ambiguous", "pending"]

KIND_RANK: dict[str, int] = {"observed": 1, "stated": 1, "inferred": 0}


@dataclass(frozen=True)
class Citation:
    """Where a fact comes from: corpus-relative file, a locator, and the text as written."""

    file: str
    locator: str
    quote: str | None = None


@dataclass(frozen=True)
class Ref:
    """An entity as an object: the fact is an edge."""

    ref: str


@dataclass(frozen=True)
class Value:
    """A literal object; ``unit`` exactly as the source writes it, or ``None``."""

    value: str | int | float | bool
    unit: str | None = None


@dataclass(frozen=True)
class Fact:
    """One cited assertion about the site (spec §3)."""

    subject: str
    predicate: str
    object: Ref | Value
    valid_from: str | None
    valid_to: str | None
    kind: Kind
    source: str
    citations: tuple[Citation, ...]
    model: str | None = None

    @property
    def id(self) -> str:
        digest = hashlib.sha256(canonical(self.to_json()).encode("utf-8")).hexdigest()
        return "fact:" + digest[:16]

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["object"] = (
            {"ref": self.object.ref}
            if isinstance(self.object, Ref)
            else {"value": self.object.value, "unit": self.object.unit}
        )
        data["citations"] = [asdict(c) for c in self.citations]
        return data

    @staticmethod
    def from_json(data: dict[str, Any]) -> Fact:
        obj = data["object"]
        return Fact(
            subject=data["subject"],
            predicate=data["predicate"],
            object=Ref(obj["ref"]) if "ref" in obj else Value(obj["value"], obj.get("unit")),
            valid_from=data["valid_from"],
            valid_to=data["valid_to"],
            kind=data["kind"],
            source=data["source"],
            citations=tuple(Citation(**c) for c in data["citations"]),
            model=data.get("model"),
        )


@dataclass(frozen=True)
class Seen:
    """A source reported at site time ``t``; freshness reads these."""

    source: str
    t: str


@dataclass(frozen=True)
class Confirm:
    """A person's yes or no on an inferred proposal (a fact id)."""

    proposal: str
    answer: Literal["yes", "no"]
    by: str


@dataclass(frozen=True)
class LogEntry:
    """One line of the fact log; ``seq`` is when Neptune knew it."""

    seq: int
    type: EntryType
    fact: Fact | None = None
    seen: Seen | None = None
    confirm: Confirm | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "type": self.type,
            "fact": self.fact.to_json() if self.fact else None,
            "seen": asdict(self.seen) if self.seen else None,
            "confirm": asdict(self.confirm) if self.confirm else None,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> LogEntry:
        return LogEntry(
            seq=data["seq"],
            type=data["type"],
            fact=Fact.from_json(data["fact"]) if data.get("fact") else None,
            seen=Seen(**data["seen"]) if data.get("seen") else None,
            confirm=Confirm(**data["confirm"]) if data.get("confirm") else None,
        )


def canonical(value: Any) -> str:
    """Sorted keys, no spaces, UTF-8 text: the one JSON form ids and log lines use."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

"""``Knowledge[T]``: what the evidence says about a field, even that it says nothing (ADR 0004).

Six states, one class each, closed under ``Knowledge[T]``. Pattern-match on them::

    match record.unit:
        case Known(value=unit): ...
        case Unknown() | NotCovered(): ...

or call ``known_or_raise()`` where anything but ``Known`` is a bug in the caller. JSON shape, the
provenance slot and the blank-field rules are in ADR 0011 and ``docs/adapter-contract.md``.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Generic, NoReturn, Protocol, TypeAlias, TypeVar

from neptune.model.jsonvalue import JsonObject, JsonValue

T = TypeVar("T")
U = TypeVar("U")


class KnowledgeState(StrEnum):
    """The tag written to JSON and to the state column of Parquet series (ADR 0004 §7)."""

    KNOWN = "known"
    KNOWN_ABSENT = "known_absent"
    UNKNOWN = "unknown"
    NOT_COVERED = "not_covered"
    NOT_APPLICABLE = "not_applicable"
    AMBIGUOUS = "ambiguous"


class Grounding(Protocol):
    """Where a state comes from. MVL-3's ``Provenance`` is the implementation; this is its shape."""

    def to_json(self) -> JsonObject: ...


@dataclass(frozen=True)
class Inherited:
    """The state's provenance is its enclosing record's (ADR 0006 §1). Omitted from JSON."""


INHERITED = Inherited()
ProvenanceSlot: TypeAlias = Grounding | Inherited


class NotKnownError(ValueError):
    """``known_or_raise()`` was called on a state that does not assert a value."""

    def __init__(self, state: KnowledgeState) -> None:
        super().__init__(f"expected a known value, got {state}")
        self.state = state


@dataclass(frozen=True)
class Known(Generic[T]):
    """The evidence asserts ``value``: never ``None``, NaN, infinite, or another state."""

    value: T
    provenance: ProvenanceSlot = INHERITED

    def __post_init__(self) -> None:
        _check_value(self.value)

    @property
    def state(self) -> KnowledgeState:
        return KnowledgeState.KNOWN

    def known_or_raise(self) -> T:
        return self.value

    def map(self, fn: Callable[[T], U]) -> "Known[U]":
        return Known(fn(self.value), self.provenance)


@dataclass(frozen=True)
class KnownAbsent:
    """The evidence asserts there is no value.

    ``provenance`` is required and must point at what makes the blank or token mean "none": the
    source's own definition or its format specification (ADR 0004 §5). Without one, use ``Unknown``.
    """

    provenance: Grounding

    def __post_init__(self) -> None:
        if isinstance(self.provenance, Inherited):
            raise ValueError("KnownAbsent needs explicit provenance for what defines the absence")

    @property
    def state(self) -> KnowledgeState:
        return KnowledgeState.KNOWN_ABSENT

    def known_or_raise(self) -> NoReturn:
        raise NotKnownError(self.state)

    def map(self, fn: Callable[[Any], object]) -> "KnownAbsent":
        return self


@dataclass(frozen=True)
class Unknown:
    """The evidence could have said and did not: a blank cell, a missing key.

    ``provenance`` is the determination: the transform that looked and, when there is one, where.
    """

    provenance: ProvenanceSlot = INHERITED

    @property
    def state(self) -> KnowledgeState:
        return KnowledgeState.UNKNOWN

    def known_or_raise(self) -> NoReturn:
        raise NotKnownError(self.state)

    def map(self, fn: Callable[[Any], object]) -> "Unknown":
        return self


@dataclass(frozen=True)
class NotCovered:
    """The evidence could not have said: it does not record this (e.g. a sentinel "no estimate")."""

    provenance: ProvenanceSlot = INHERITED

    @property
    def state(self) -> KnowledgeState:
        return KnowledgeState.NOT_COVERED

    def known_or_raise(self) -> NoReturn:
        raise NotKnownError(self.state)

    def map(self, fn: Callable[[Any], object]) -> "NotCovered":
        return self


@dataclass(frozen=True)
class NotApplicable:
    """The field has no meaning for this entity. A schema fact, so it carries no provenance."""

    @property
    def state(self) -> KnowledgeState:
        return KnowledgeState.NOT_APPLICABLE

    def known_or_raise(self) -> NoReturn:
        raise NotKnownError(self.state)

    def map(self, fn: Callable[[Any], object]) -> "NotApplicable":
        return self


@dataclass(frozen=True)
class Candidate(Generic[T]):
    """One reading the evidence supports, with where it comes from."""

    value: T
    provenance: ProvenanceSlot = INHERITED

    def __post_init__(self) -> None:
        _check_value(self.value)


@dataclass(frozen=True)
class Ambiguous(Generic[T]):
    """The evidence supports more than one reading. Never picks a winner.

    At least two candidates with distinct values, in the order the evidence presents them (which is
    deterministic because parsing is). The same value from two places is ``Known``, not ambiguous.
    """

    candidates: tuple[Candidate[T], ...]

    def __post_init__(self) -> None:
        if len(self.candidates) < 2:
            raise ValueError(f"Ambiguous needs at least two candidates, got {len(self.candidates)}")
        values: list[T] = []
        for candidate in self.candidates:
            if candidate.value in values:
                raise ValueError(f"Ambiguous candidates must differ: {candidate.value!r} repeats")
            values.append(candidate.value)

    @property
    def state(self) -> KnowledgeState:
        return KnowledgeState.AMBIGUOUS

    def known_or_raise(self) -> NoReturn:
        raise NotKnownError(self.state)

    def map(self, fn: Callable[[T], U]) -> "Ambiguous[U]":
        return Ambiguous(tuple(Candidate(fn(c.value), c.provenance) for c in self.candidates))


Knowledge: TypeAlias = Known[T] | KnownAbsent | Unknown | NotCovered | NotApplicable | Ambiguous[T]
_STATES = (Known, KnownAbsent, Unknown, NotCovered, NotApplicable, Ambiguous)


def _check_value(value: object) -> None:
    if value is None:
        raise ValueError("None is not a value; choose a Knowledge state instead (ADR 0004 §4)")
    if isinstance(value, _STATES):
        raise ValueError("Knowledge states do not nest")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{value!r} is not representable in canonical JSON (ADR 0002)")


# --- Adapter helper for blank and default handling (ADR 0004 §5) -------------------------------


def from_text(
    raw: str | None,
    parse: Callable[[str], T],
    *,
    absent_tokens: Mapping[str, Grounding] | None = None,
    provenance: ProvenanceSlot = INHERITED,
) -> "Knowledge[T]":
    """Map one textual source field to a state.

    - Missing, empty or whitespace-only ⇒ ``Unknown``. A blank is never "none".
    - Exactly one of ``absent_tokens`` ⇒ ``KnownAbsent``, grounded in the definition the caller
      supplies for that token (the register's legend, the format specification). Only tokens the
      source or its specification defines belong here; matching is exact, no case folding.
    - Anything else ⇒ ``Known(parse(raw))``. ``parse`` errors propagate: an unparseable value is a
      finding for the adapter to raise, not a state to guess.
    """
    if raw is None or not raw.strip():
        return Unknown(provenance)
    if absent_tokens is not None and raw in absent_tokens:
        return KnownAbsent(absent_tokens[raw])
    return Known(parse(raw), provenance)


# --- JSON -----------------------------------------------------------------------------------


def to_json(
    knowledge: "Knowledge[T]", encode_value: Callable[[T], JsonValue] | None = None
) -> JsonObject:
    """Serialise a state. ``encode_value`` is needed unless ``T`` is already a ``JsonValue``."""

    def encode(value: T) -> JsonValue:
        return encode_value(value) if encode_value is not None else value  # type: ignore[return-value]

    out: dict[str, JsonValue] = {"knowledge": str(knowledge.state)}
    match knowledge:
        case Known(value=value, provenance=provenance):
            out["value"] = encode(value)
            _put_provenance(out, provenance)
        case KnownAbsent(provenance=provenance):
            _put_provenance(out, provenance)
        case Unknown(provenance=provenance) | NotCovered(provenance=provenance):
            _put_provenance(out, provenance)
        case NotApplicable():
            pass
        case Ambiguous(candidates=candidates):
            rows: list[JsonValue] = []
            for candidate in candidates:
                row: dict[str, JsonValue] = {"value": encode(candidate.value)}
                _put_provenance(row, candidate.provenance)
                rows.append(row)
            out["candidates"] = rows
    return out


def from_json(
    data: JsonValue,
    decode_value: Callable[[JsonValue], T],
    decode_provenance: Callable[[JsonObject], Grounding],
) -> "Knowledge[T]":
    """Parse a state strictly: unknown tags, unexpected keys and missing fields are errors."""
    obj = _object(data, "knowledge")
    tag = obj.get("knowledge")
    if not isinstance(tag, str) or tag not in KnowledgeState.__members__.values():
        raise ValueError(f"unknown knowledge state: {tag!r}")
    state = KnowledgeState(tag)

    def slot(source: Mapping[str, JsonValue]) -> ProvenanceSlot:
        if "provenance" not in source:
            return INHERITED
        return decode_provenance(_object(source["provenance"], "provenance"))

    if state is KnowledgeState.KNOWN:
        _keys(obj, required={"knowledge", "value"}, optional={"provenance"})
        return Known(decode_value(obj["value"]), slot(obj))
    if state is KnowledgeState.KNOWN_ABSENT:
        _keys(obj, required={"knowledge", "provenance"})
        return KnownAbsent(decode_provenance(_object(obj["provenance"], "provenance")))
    if state is KnowledgeState.UNKNOWN:
        _keys(obj, required={"knowledge"}, optional={"provenance"})
        return Unknown(slot(obj))
    if state is KnowledgeState.NOT_COVERED:
        _keys(obj, required={"knowledge"}, optional={"provenance"})
        return NotCovered(slot(obj))
    if state is KnowledgeState.NOT_APPLICABLE:
        _keys(obj, required={"knowledge"})
        return NotApplicable()
    _keys(obj, required={"knowledge", "candidates"})
    rows = obj["candidates"]
    if not isinstance(rows, Sequence) or isinstance(rows, str):
        raise ValueError("candidates must be an array")
    candidates = []
    for row in rows:
        candidate = _object(row, "candidate")
        _keys(candidate, required={"value"}, optional={"provenance"})
        candidates.append(Candidate(decode_value(candidate["value"]), slot(candidate)))
    return Ambiguous(tuple(candidates))


def _put_provenance(out: dict[str, JsonValue], provenance: ProvenanceSlot) -> None:
    if not isinstance(provenance, Inherited):
        out["provenance"] = provenance.to_json()


def _object(data: JsonValue, what: str) -> Mapping[str, JsonValue]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{what} must be a JSON object, got {type(data).__name__}")
    return data


def _keys(
    obj: Mapping[str, JsonValue],
    *,
    required: set[str],
    optional: frozenset[str] | set[str] = frozenset(),
) -> None:
    missing = required - obj.keys()
    extra = obj.keys() - required - optional
    if missing or extra:
        raise ValueError(
            f"bad knowledge object: missing {sorted(missing)}, unexpected {sorted(extra)}"
        )

"""The typed query (ADR 0002): subjects, time, space, graph, text, budget and explain clauses.

A ``Query`` is plain data. It says what to retrieve and on which clock, frame and snapshot; it
never says how (the planner chooses channels, and no channel is privileged). ``validate`` decides
whether a query is answerable without silently mixing clocks, frames or units; ``codec`` gives
its canonical JSON and id, ``decode`` reads that JSON back, and ``schema`` exports its JSON Schema.

Set-like clauses are ``frozenset``s so that two queries that mean the same thing are equal and
encode to the same bytes; the codecs order them by their canonical JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal, TypeAlias

if TYPE_CHECKING:
    from fractions import Fraction

# The query document's wire version. Every query's JSON carries it as ``query_version`` and the
# textual form's header line is ``query <QUERY_VERSION>``; a reader refuses any other value.
# Raise it only for a change an older reader would misread (ADR 0002 §9).
QUERY_VERSION: Final = 1

# The transaction-time default: the reader's head when the query is planned. The planner resolves
# it to one Ledger transaction and records that number in the packet, so an answer is replayable.
HEAD: Final = "head"
AsOf: TypeAlias = int | Literal["head"]

# Bounds a query must stay inside (ADR 0002 §8); each one has a boundary test.
MAX_SUBJECTS: Final = 64
MAX_SAME_AS_DEPTH: Final = 3
MAX_HOPS: Final = 4
MAX_REGIONS: Final = 16
MAX_ZONES: Final = 64
MAX_BRIDGES: Final = 16
MAX_EXPLAIN: Final = 16
MAX_TEXT_CHARS: Final = 2000
MAX_ITEMS: Final = 10_000
MAX_TOKENS: Final = 1_000_000
MAX_BYTES: Final = 64 * 1024 * 1024
MAX_LATENCY_MS: Final = 600_000
MAX_DOCUMENT_BYTES: Final = 64 * 1024  # a query's JSON or textual form
INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1


class Direction(StrEnum):
    """Which way a graph hop follows a claim: subject to object (``out``), back, or both."""

    OUT = "out"
    IN = "in"
    BOTH = "both"


class TextChannel(StrEnum):
    """Where free text is routed. Peers: the planner fuses both and privileges neither."""

    LEXICAL = "lexical"  # BM25 over the scoped fields
    VECTOR = "vector"  # embedding similarity; a score, never a claim


class TextField(StrEnum):
    """What free text may match. Every field is evidence or a claim, never a rendered packet."""

    DECLARED_ID = "declared_id"  # node and thread ids, ``<namespace>:<value>``
    CLAIM_TEXT = "claim_text"  # text-valued claim objects (``has_summary``, ``maintenance_state``)
    RECORD = "record"  # text fields of Ledger catalog records
    DOCUMENT = "document"  # text the compiler extracted from documents
    FINDING = "finding"  # ingest and resolver finding codes and messages


class Caller(StrEnum):
    """Who asks. Only picks an explicit ``include_inferred``; the query records the choice."""

    AGENT = "agent"
    POLICY = "policy"


def default_include_inferred(caller: Caller) -> bool:
    """A control policy acts on evidence only; an agent may see inferences, labelled as such."""
    return caller is Caller.AGENT


# --- Clocks and instants --------------------------------------------------------------------


@dataclass(frozen=True)
class DomainClock:
    """One source clock: a compiler ``TimestampDomain`` record id (``rec:sha256:<hex>``)."""

    domain_id: str


@dataclass(frozen=True)
class CivilTime:
    """A civil timeline every source can share (Memory's ``CivilClock``): an absolute timescale
    (``utc``, ``tai``, ``gps``, ``posix``), an absolute epoch (``unix``, ``gps``) and an exact tick
    length in seconds. Two civil times with different resolutions are different clocks."""

    timescale: str
    epoch: str
    resolution: Fraction


Clock: TypeAlias = DomainClock | CivilTime


@dataclass(frozen=True)
class Instant:
    """``ticks`` on ``clock``; never a float, never a bare number."""

    clock: Clock
    ticks: int


@dataclass(frozen=True)
class During:
    """World (valid) time ``[start, end)`` in ticks on one clock; ``end=None`` is open."""

    clock: Clock
    start: int
    end: int | None


@dataclass(frozen=True)
class ClockBridge:
    """Permission to relate two clocks through one named ``ClockMapping`` record.

    The query states the mapping's id and the two clocks it joins; the planner checks both against
    the record and labels anything placed through an estimated mapping ``inferred``.
    """

    mapping_id: str
    source: Clock
    target: Clock


# --- Space ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameRef:
    """The compiler's ``FrameRef``: a frame id verbatim within one declared frame graph."""

    frame_id: str
    graph_id: str


Vec3: TypeAlias = tuple[float, float, float]


@dataclass(frozen=True)
class Box:
    """An axis-aligned box in its region's frame and unit: ``min < max`` on every axis."""

    min: Vec3
    max: Vec3


@dataclass(frozen=True)
class Sphere:
    center: Vec3
    radius: float


Shape: TypeAlias = Box | Sphere


@dataclass(frozen=True)
class FrameRegion:
    """A region in one frame, with its length unit declared (a canonical symbol such as ``m``)."""

    frame: FrameRef
    unit: str
    shape: Shape


@dataclass(frozen=True)
class FrameBridge:
    """Permission to relate two frames through one named ``FrameTransform`` record."""

    transform_id: str
    parent: FrameRef
    child: FrameRef


@dataclass(frozen=True)
class SiteScope:
    """A site and, optionally, some of its zones: topological, so it needs no frame."""

    site: str
    zones: frozenset[str] = frozenset()


# --- Subjects, graph, text, budget, explain -----------------------------------------------------


@dataclass(frozen=True)
class Subject:
    """An entity selector: every thread or node of ``kind``, or the one with ``declared_id``
    (``<namespace>:<value>``), widened along declared ``same_as`` edges up to ``same_as_depth``."""

    kind: str
    declared_id: str | None = None
    same_as_depth: int = 0


@dataclass(frozen=True)
class GraphClause:
    """Follow claims from the anchors: ``predicates=None`` follows every predicate."""

    predicates: frozenset[str] | None
    hops: int
    direction: Direction


@dataclass(frozen=True)
class TextClause:
    text: str
    fields: frozenset[TextField]
    channels: frozenset[TextChannel]


@dataclass(frozen=True)
class Budget:
    """Limits on the packet. ``items`` is mandatory; any other limit left ``None`` is unbounded."""

    items: int
    tokens: int | None = None
    bytes: int | None = None
    latency_ms: int | None = None


@dataclass(frozen=True)
class Why:
    """Explain one claim: its evidence, transform, supersession and findings."""

    claim_id: str


@dataclass(frozen=True)
class Diff:
    """What changed about ``subject`` between two points: two Ledger transactions (what was known
    then) or two world-time instants (what held then). Both points are on the same axis."""

    subject: Subject
    before: int | Instant
    after: int | Instant


Explain: TypeAlias = Why | Diff


@dataclass(frozen=True)
class Query:
    """One question. ``include_inferred`` and ``budget`` have no default: a caller always says."""

    include_inferred: bool
    budget: Budget
    subjects: frozenset[Subject] = frozenset()
    as_of: AsOf = HEAD
    during: During | None = None
    clock_bridges: frozenset[ClockBridge] = frozenset()
    regions: frozenset[FrameRegion] = frozenset()
    frame_bridges: frozenset[FrameBridge] = frozenset()
    site: SiteScope | None = None
    graph: GraphClause | None = None
    text: TextClause | None = None
    explain: tuple[Explain, ...] = ()

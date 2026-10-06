"""The graph channel (ADR 0007 §3): claims around the query's subjects, at a snapshot, in a window.

Seeds come from the query's declared subjects (widened along ``same_as`` to each subject's
depth, never along ``same_as_candidate``) or, when there are none, its site and zones. From the
seeds the channel walks Memory's claims breadth first: ``hops`` levels, the predicate allow-list,
the direction. A claim takes part only when it is current at Memory's snapshot, overlaps
``during`` on the window's clock (or on a clock a query bridge joins to it through a mapping
Memory holds), is not an inference the query excluded, and uses only vocabulary the pinned
graph-schema has. Everything else is named in a gap, never dropped silently and never coerced.

With a Ledger catalog the window and the regions go to ``CatalogApi.query`` (the interval and
R-tree indexes); stream and image records that the carried claims name or cite come back as
series windows and frames. Scores are graph distance and claim weight: ``weight * 0.5 **
(level - 1)``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Final

from neptune_memory.schema.claim import LedgerRecordRef, TypedLiteral, ValueType, is_inferred
from neptune_memory.schema.interval import OPEN, Interval, Open, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CLOCK_MAP, SAME_AS, SAME_AS_CANDIDATE

from neptune.model.knowledge import AssertionKind, Known, NotApplicable, Unknown
from neptune.model.provenance import evidence_ref_from_json
from neptune.model.time import INT64_MAX, INT64_MIN, Timestamp
from neptune_context import pinned
from neptune_context.answer import domain_id
from neptune_context.packets.model import (
    ArrowHandle,
    Channel,
    ChannelHit,
    ClaimItem,
    FrameItem,
    Gap,
    GapCode,
    Item,
    ItemProvenance,
    Relevance,
    SeriesWindowItem,
    Superseded,
    Transform,
    series_path,
)
from neptune_context.pins import GRAPH_SCHEMA_VERSION
from neptune_context.query.codec import clock_bridge_to_json, ordered, region_to_json
from neptune_context.query.codec import subject_to_json as _subject_json
from neptune_context.query.model import Box, Direction, Query
from neptune_context.retrieve.channel import ChannelAnswer, Retrieval, answer

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from neptune_ledger.api import CatalogApi, QueryRow
    from neptune_memory.schema.claim import Claim, ClaimId
    from neptune_memory.schema.clock_map import ClockMap
    from neptune_memory.schema.interval import LedgerTx
    from neptune_memory.schema.reader import MemoryReader
    from neptune_memory.schema.supersede import ResolutionFinding

    from neptune_context.query.model import FrameRegion

DECAY: Final = 0.5  # each further level halves a claim's score
UNKNOWN_CONFIDENCE_WEIGHT: Final = 0.5  # an inferred claim whose confidence is Unknown
SITE_CHECK_HOPS: Final = 2  # how far a subject may be from the scoped site or zones
DEFAULT_MAX_NODES: Final = 4096  # nodes a walk may expand before it stops (and says so)
DEFAULT_MAX_EDGES: Final = 1024  # claims followed from one node (by claim id), then a gap
DEFAULT_MAX_ROWS: Final = 10_000  # rows one Ledger window may return
LEDGER_KINDS: Final = ("image", "stream")  # record kinds the Ledger windows read


def _node_key(node: NodeRef) -> tuple[str, str]:
    return (str(node.node_type), node.node_id)


def weight(claim: Claim) -> float:
    """How much a claim counts: 1 when deterministic, its confidence when inferred."""
    if not is_inferred(claim.assertion_kind):
        return 1.0
    if isinstance(claim.confidence, Known):
        return float(claim.confidence.value)
    return UNKNOWN_CONFIDENCE_WEIGHT


def score(claim: Claim, level: int) -> float:
    """``weight * DECAY ** (level - 1)``: level 1 is a claim touching a seed."""
    return float(weight(claim) * DECAY ** (level - 1))


def _clamp(value: int) -> int:
    return max(INT64_MIN, min(INT64_MAX, value))


@dataclass(frozen=True)
class _Window:
    """The world-time window on one clock: ``[start, end)``, ``end`` ``None`` when open."""

    clock: str
    start: int
    end: int | None

    def interval(self) -> Interval:
        start = Timestamp(self.start, self.clock)  # type: ignore[arg-type]
        end = OPEN if self.end is None else Timestamp(self.end, self.clock)  # type: ignore[arg-type]
        return Interval(start, end)


def _slack(clock_map: ClockMap) -> Fraction:
    """The map's residual bound in target ticks when it states one, else 0."""
    residual = clock_map.residual_bound
    return Fraction(residual.value.ticks) if isinstance(residual, Known) else Fraction(0)


def _clip(a: _Window, b: _Window) -> _Window | None:
    """The overlap of two windows on one clock, or ``None`` when they do not meet."""
    start = max(a.start, b.start)
    ends = [e for e in (a.end, b.end) if e is not None]
    end = min(ends) if ends else None
    if end is not None and end <= start:
        return None
    return _Window(a.clock, start, end)


def _carry(
    window: _Window,
    affine: tuple[Fraction, Fraction],
    clock: str,
    *,
    forward: bool,
    slack: Fraction = Fraction(0),
) -> _Window:
    """``window`` carried exactly through ``target = rate * source + offset``: onto the target
    clock (``forward``) or back onto the source, widened by ``slack`` target ticks each side."""
    rate, offset = affine

    def there(ticks: int, low: bool) -> int:
        if forward:
            value = rate * ticks + offset + (-slack if low else slack)
        else:
            value = (ticks + (-slack if low else slack) - offset) / rate
        return _clamp(math.floor(value) if low else math.ceil(value))

    start = there(window.start, True)
    end = None if window.end is None else there(window.end, False)
    if end is not None and end <= start:
        end = min(start + 1, INT64_MAX)
    return _Window(clock, start, end)


@dataclass
class _State:
    """What one retrieval found and could not carry, accumulated across the walk."""

    hits: dict[ClaimId, tuple[int, Claim]] = field(default_factory=dict)
    withheld: set[str] = field(default_factory=set)
    beyond: dict[str, set[str]] = field(default_factory=dict)
    other_clock: set[str] = field(default_factory=set)
    findings: dict[str, ResolutionFinding] = field(default_factory=dict)
    gaps: list[Gap] = field(default_factory=list)
    truncated: set[str] = field(default_factory=set)  # pointers of walks cut by max_nodes
    crowded: dict[str, int] = field(default_factory=dict)  # node id -> claims it holds, when cut

    def hit(self, claim: Claim, level: int) -> None:
        held = self.hits.get(claim.id)
        if held is None or level < held[0]:
            self.hits[claim.id] = (level, claim)

    def gap(self, code: GapCode, at: str, refs: Iterable[str], detail: str) -> None:
        self.gaps.append(Gap(code, at, Channel.GRAPH, tuple(sorted(set(refs))), detail))


class GraphChannel:
    """The graph channel over a Memory reader and, optionally, a Ledger catalog."""

    def __init__(
        self,
        memory: MemoryReader,
        catalog: CatalogApi | None = None,
        *,
        max_nodes: int = DEFAULT_MAX_NODES,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_edges: int = DEFAULT_MAX_EDGES,
    ) -> None:
        self._memory = memory
        self._catalog = catalog
        self._max_nodes = max_nodes
        self._max_edges = max_edges
        self._max_rows = max_rows

    @property
    def channel(self) -> Channel:
        return Channel.GRAPH

    @property
    def config(self) -> dict[str, Any]:
        """The settings that decide its answers (part of the engine's config hash)."""
        return {
            "channel": str(Channel.GRAPH),
            "decay": DECAY,
            "ledger": self._catalog is not None,
            "ledger_kinds": list(LEDGER_KINDS),
            "max_edges": self._max_edges,
            "max_nodes": self._max_nodes,
            "max_rows": self._max_rows,
            "site_check_hops": SITE_CHECK_HOPS,
            "unknown_confidence_weight": UNKNOWN_CONFIDENCE_WEIGHT,
        }

    def retrieve(self, request: Retrieval) -> ChannelAnswer:
        return _Retrieve(self, request).run()


class _Retrieve:
    """One retrieval: seeds, bridges, the walk, the Ledger windows and the answer."""

    def __init__(self, channel: GraphChannel, request: Retrieval) -> None:
        self.memory: MemoryReader = channel._memory
        self.catalog: CatalogApi | None = channel._catalog
        self.max_nodes = channel._max_nodes
        self.max_rows = channel._max_rows
        self.max_edges = channel._max_edges
        self.current: dict[tuple[NodeRef, str, int], frozenset[str]] = {}
        self.query: Query = request.query
        self.snapshot = request.snapshot
        self.as_of: LedgerTx = request.snapshot.memory_as_of
        self.state = _State()
        during = self.query.during
        self.window = (
            None if during is None else _Window(domain_id(during.clock), during.start, during.end)
        )
        # bridged clock -> the window carried onto it, one piece per clock_map claim that covers
        # part of the window, each clipped to where that mapping holds
        self.placed: dict[str, list[tuple[_Window, Claim]]] = {}
        self.used: dict[str, Claim] = {}  # clock_map claims a carried claim was placed through
        self.views: dict[NodeRef, Any] = {}
        self.transforms: dict[str, Transform | None] = {}

    # --- Reading Memory ------------------------------------------------------------------------

    def view(self, node: NodeRef) -> Any:
        """``reader.node`` at Memory's snapshot, inferred claims included (filtered here)."""
        if node not in self.views:
            self.views[node] = self.memory.node(node, self.as_of, include_inferred=True)
        return self.views[node]

    def admit(self, claim: Claim, record: bool) -> bool:
        """Whether ``claim`` may be carried and walked through; why not, when ``record``."""
        reason = pinned.claim_beyond_pin(claim)
        if reason is not None:
            if record:
                self.state.beyond.setdefault(reason, set()).add(claim.id)
            return False
        if is_inferred(claim.assertion_kind) and not self.query.include_inferred:
            if record:
                self.state.withheld.add(claim.id)
            return False
        if self.window is None:
            return True
        clock = str(claim.valid.domain_id)
        if clock == self.window.clock:
            return claim.valid.overlaps(self.window.interval())
        pieces = self.placed.get(clock)
        if pieces is None:
            if record:
                self.state.other_clock.add(claim.id)
            return False
        return any(claim.valid.overlaps(w.interval()) for w, _ in pieces)

    def carry(self, claim: Claim, level: int) -> None:
        """Make ``claim`` a hit, noting the mapping pieces it was placed through, if any."""
        self.state.hit(claim, level)
        for window, mapping in self.placed.get(str(claim.valid.domain_id), ()):
            if claim.valid.overlaps(window.interval()):
                self.used[mapping.id] = mapping

    # --- Seeds ---------------------------------------------------------------------------------

    def seeds(self) -> tuple[dict[NodeRef, int], frozenset[NodeType]]:
        """Seed nodes (each at level 0) and the node types kind-wide subjects keep."""
        query, node_types = self.query, pinned.node_types()
        subjects = ordered(query.subjects, _subject_json)
        declared: dict[NodeRef, int] = {}
        groups: dict[NodeRef, list[NodeRef]] = {}  # a declared node and its same_as identities
        asked = False  # whether any subject names one node: then only those may seed the walk
        kinds: set[NodeType] = set()
        kind_wide: list[str] = []
        for index, subject in enumerate(subjects):
            at = f"/subjects/{index}"
            if subject.kind not in node_types:
                self.state.gap(
                    GapCode.NOT_COVERED,
                    at,
                    [subject.declared_id or subject.kind],
                    f"{subject.kind!r} is a Ledger thread kind; the graph channel reads Memory"
                    " nodes, and Memory has no thread index",
                )
                continue
            if subject.declared_id is None:
                kinds.add(NodeType(subject.kind))
                kind_wide.append(at)
                continue
            asked = True
            node = NodeRef(NodeType(subject.kind), subject.declared_id)
            if not isinstance(self.view(node), Known):
                self.state.gap(
                    GapCode.NOT_COVERED,
                    at,
                    [node.node_id],
                    f"Memory holds no claim about {node.node_type} {node.node_id!r} at"
                    f" transaction {self.as_of}",
                )
                continue
            declared[node] = 0
            groups[node] = [node, *self.same_as(node, subject.same_as_depth)]
            for other in groups[node]:
                declared.setdefault(other, 0)
        anchors = self.site_anchors()
        if not asked:
            seeds = anchors  # the site and zones are the anchors (ADR 0002 §5)
        elif declared and anchors:
            seeds = self.at_site(groups, anchors)
        else:
            # Subjects that name nodes were asked; none found is no seed, never the whole site.
            seeds = declared
        if not seeds:
            for at in kind_wide:
                self.state.gap(
                    GapCode.NOT_COVERED,
                    at,
                    [],
                    "a kind-wide subject needs a declared subject or a site to start from:"
                    " Memory's reader has no by-kind index",
                )
        return seeds, frozenset(kinds)

    def same_as(self, node: NodeRef, depth: int) -> list[NodeRef]:
        """Nodes ``node`` is declared ``same_as`` within ``depth``; their ``same_as`` claims are
        hits at level 1. Candidates are never followed: a candidate is not an identity."""
        found: list[NodeRef] = []
        seen, frontier = {node}, [node]
        for _ in range(depth):
            reached: list[NodeRef] = []
            for here in sorted(frontier, key=_node_key):
                view = self.view(here)
                if not isinstance(view, Known):
                    continue
                for claim in sorted((*view.value.claims, *view.value.incoming), key=lambda c: c.id):
                    if claim.predicate != SAME_AS or not self.admit(claim, record=True):
                        continue
                    other = claim.object if claim.subject == here else claim.subject
                    if isinstance(other, NodeRef) and other not in seen:
                        seen.add(other)
                        reached.append(other)
                        self.carry(claim, 1)
            found.extend(sorted(reached, key=_node_key))
            frontier = reached
        return found

    def site_anchors(self) -> dict[NodeRef, int]:
        site = self.query.site
        if site is None:
            return {}
        anchors: dict[NodeRef, int] = {}
        for at, node in (
            ("/site", NodeRef(NodeType.SITE, site.site)),
            *(
                (f"/site/zones/{i}", NodeRef(NodeType.ZONE, zone))
                for i, zone in enumerate(sorted(site.zones))
            ),
        ):
            if isinstance(self.view(node), Known):
                anchors[node] = 0
            else:
                self.state.gap(
                    GapCode.NOT_COVERED,
                    at,
                    [node.node_id],
                    f"Memory holds no claim about {node.node_type} {node.node_id!r} at"
                    f" transaction {self.as_of}",
                )
        return anchors

    def at_site(
        self, groups: dict[NodeRef, list[NodeRef]], anchors: dict[NodeRef, int]
    ) -> dict[NodeRef, int]:
        """The declared subjects (each with its ``same_as`` identities, kept or dropped together)
        the graph connects to the scoped site (or, given zones, to one of them) within
        ``SITE_CHECK_HOPS`` admitted claims; the rest are a gap at ``/site``."""
        zones = {n for n in anchors if n.node_type is NodeType.ZONE}
        targets = zones or set(anchors)
        kept: dict[NodeRef, int] = {}
        dropped: list[NodeRef] = []
        for root in sorted(groups, key=_node_key):
            reached = self.walk(
                dict.fromkeys(groups[root], 0),
                SITE_CHECK_HOPS,
                lambda p: p != SAME_AS_CANDIDATE,
                Direction.BOTH,
                record=False,
                at="/site",
            )
            if targets & reached:
                kept.update(dict.fromkeys(groups[root], 0))
            else:
                dropped.append(root)
        if dropped:
            self.state.gap(
                GapCode.NOT_COVERED,
                "/site",
                [n.node_id for n in dropped],
                f"not connected to the scoped {'zones' if zones else 'site'} within"
                f" {SITE_CHECK_HOPS} hops at transaction {self.as_of}",
            )
        return kept

    # --- Clock bridges -------------------------------------------------------------------------

    def bridges(self) -> None:
        """Carry the window onto each clock a query bridge joins to it, through the mapping
        Memory holds for that bridge; refuse (a gap) a bridge Memory cannot back."""
        window = self.window
        if window is None:
            return
        for index, bridge in enumerate(ordered(self.query.clock_bridges, clock_bridge_to_json)):
            at = f"/clock_bridges/{index}"
            source, target = domain_id(bridge.source), domain_id(bridge.target)
            if window.clock not in (source, target):
                self.state.gap(
                    GapCode.NOT_COVERED,
                    at,
                    [bridge.mapping_id],
                    "the graph channel places claims through one bridge from the window's"
                    " clock; this bridge does not touch it",
                )
                continue
            forward = window.clock == source
            other = target if forward else source
            self.bridge(at, bridge.mapping_id, window, source, target, other, forward=forward)

    def bridge(
        self,
        at: str,
        mapping_id: str,
        window: _Window,
        source: str,
        target: str,
        other: str,
        *,
        forward: bool,
    ) -> None:
        """Carry ``window`` onto ``other`` through every piece of ``mapping_id`` Memory holds.

        Memory splits a mapping into time-bounded ``clock_map`` claims, each valid over part of the
        source clock. A piece is used only where it holds: its validity (carried onto the window's
        clock when the window is on the target) is clipped to the window, and only that part is
        carried. Any part of the window no piece covers is an ``unknown`` gap at the bridge: a map
        is never extended past the interval its evidence states.
        """
        pieces = self.mapping(mapping_id, source, target)
        if not pieces:
            self.state.gap(
                GapCode.UNKNOWN,
                at,
                [mapping_id],
                f"Memory holds no clock_map from mapping {mapping_id} joining {source} to"
                f" {target} at transaction {self.as_of}; claims on the other clock stay apart",
            )
            return
        covered: list[Interval] = []
        placed: list[tuple[_Window, Claim]] = []
        unusable: list[str] = []
        for claim, clock_map in pieces:
            affine = clock_map.affine()
            valid = claim.valid
            if affine is None or str(valid.domain_id) != source:
                unusable.append(claim.id)  # composed or unknown map, or validity elsewhere
                continue
            holds = _Window(
                source, valid.start.ticks, None if isinstance(valid.end, Open) else valid.end.ticks
            )
            # Where the piece holds, on the window's clock.
            coverage = holds if forward else _carry(holds, affine, target, forward=True)
            part = _clip(window, coverage)
            if part is None:
                continue
            covered.append(coverage.interval())
            slack = _slack(clock_map)
            if forward:
                there: _Window | None = _carry(part, affine, other, forward=True, slack=slack)
            else:
                back = _carry(part, affine, other, forward=False, slack=slack)
                there = _clip(back, holds)
            if there is not None:
                placed.append((there, claim))
        if placed:
            self.placed[other] = placed
        uncovered = window.interval().minus(covered)
        if uncovered:
            spans = ", ".join(
                f"[{p.start.ticks}, {'open' if isinstance(p.end, Open) else p.end.ticks})"
                for p in uncovered[:4]
            )
            more = f" and {len(uncovered) - 4} more" if len(uncovered) > 4 else ""
            reason = (
                "; some pieces state no anchor and rate (a composed or unknown map)"
                if unusable
                else ""
            )
            self.state.gap(
                GapCode.UNKNOWN,
                at,
                [mapping_id, *unusable],
                f"no piece of mapping {mapping_id} holds over ticks {spans}{more} of the window"
                f" on {window.clock}{reason}: claims there on {other} stay apart",
            )

    def mapping(self, mapping_id: str, source: str, target: str) -> list[tuple[Claim, ClockMap]]:
        """Every ``clock_map`` claim (piece) on ``source`` onto ``target`` citing ``mapping_id``,
        by claim id."""
        clock = NodeRef(NodeType.CLOCK, source)
        result = self.memory.claims(clock, CLOCK_MAP, self.as_of, include_inferred=True)
        found: list[tuple[Claim, ClockMap]] = []
        for claim in result.claims:  # sorted by id
            obj = claim.object
            if not isinstance(obj, TypedLiteral) or obj.datatype is not ValueType.CLOCK_MAP:
                continue
            clock_map: ClockMap = obj.value  # type: ignore[assignment]
            if str(clock_map.target) != target:
                continue
            if mapping_id not in claim.provenance.records and mapping_id not in clock_map.chain:
                continue
            if pinned.claim_beyond_pin(claim) is not None:
                continue
            if is_inferred(claim.assertion_kind) and not self.query.include_inferred:
                self.state.withheld.add(claim.id)
                continue
            found.append((claim, clock_map))
        return found

    # --- The walk ------------------------------------------------------------------------------

    def walk(
        self,
        seeds: dict[NodeRef, int],
        hops: int,
        allowed: Callable[[str], bool],
        direction: Direction,
        *,
        record: bool,
        kinds: frozenset[NodeType] = frozenset(),
        at: str = "",
    ) -> set[NodeRef]:
        """Breadth first from ``seeds``; with ``record``, admitted claims become hits at the level
        they were reached. Returns every node reached (seeds included)."""
        reached = set(seeds)
        frontier = sorted(seeds, key=_node_key)
        expanded = 0
        for level in range(1, hops + 1):
            nxt: set[NodeRef] = set()
            for here in frontier:
                if expanded >= self.max_nodes:
                    self.state.truncated.add(at)  # where the walk that was cut is asked for
                    return reached
                expanded += 1
                view = self.view(here)
                if not isinstance(view, Known):
                    continue
                if record:
                    for finding in view.value.findings:
                        self.state.findings.setdefault(finding.id, finding)
                edges = sorted((*view.value.claims, *view.value.incoming), key=lambda c: c.id)
                if len(edges) > self.max_edges:
                    self.state.crowded[here.node_id] = len(edges)
                    edges = edges[: self.max_edges]
                for claim in edges:
                    outgoing = claim.subject == here
                    if direction is Direction.OUT and not outgoing:
                        continue
                    if direction is Direction.IN and claim.object != here:
                        continue
                    if not allowed(claim.predicate) or not self.admit(claim, record):
                        continue
                    other = claim.object if outgoing else claim.subject
                    if record and self.keep(claim, kinds, seeds):
                        self.carry(claim, level)
                    if isinstance(other, NodeRef) and other not in reached:
                        reached.add(other)
                        nxt.add(other)
            frontier = sorted(nxt, key=_node_key)
        return reached

    @staticmethod
    def keep(claim: Claim, kinds: frozenset[NodeType], seeds: dict[NodeRef, int]) -> bool:
        """With kind-wide subjects, a claim is kept when it touches a node of one of those kinds
        or joins two seeds; without, every admitted claim is kept."""
        if not kinds:
            return True
        ends = [claim.subject, claim.object]
        nodes = [n for n in ends if isinstance(n, NodeRef)]
        if any(n.node_type in kinds for n in nodes):
            return True
        return len(nodes) == 2 and all(n in seeds for n in nodes)

    # --- Supersessions -------------------------------------------------------------------------

    def present(self, claim: Claim, tx: int) -> tuple[Claim, ...]:
        """The claims about ``claim``'s subject and predicate current at ``tx``."""
        return self.memory.claims(
            claim.subject, claim.predicate, ledger_tx(tx), include_inferred=True
        ).claims

    def holds(self, claim: Claim, tx: int) -> bool:
        """Whether ``claim`` is current at ``tx``; one read per subject, predicate and
        transaction, shared by every claim of that group (a busy hub costs one read, not one
        per claim)."""
        key = (claim.subject, claim.predicate, tx)
        if key not in self.current:
            self.current[key] = frozenset(c.id for c in self.present(claim, tx))
        return claim.id in self.current[key]

    def superseded(self, claim: Claim) -> Superseded | None:
        """When, in ``(memory_as_of, head]``, Memory stopped holding ``claim``, and what by."""
        low, high = int(self.as_of), min(int(self.memory.head), int(self.snapshot.head))
        if low >= high or self.holds(claim, high):
            return None  # the usual case: one shared read at head for the whole group
        while high - low > 1:  # current at ``low``, gone at ``high``: only superseded claims
            middle = (low + high) // 2
            if self.holds(claim, middle):
                low = middle
            else:
                high = middle
        by = sorted(c.id for c in self.present(claim, high) if claim.id in c.supersedes)
        if not by:
            view = self.memory.node(claim.subject, ledger_tx(high), include_inferred=True)
            if isinstance(view, Known):
                edges = (*view.value.claims, *view.value.incoming)
                by = sorted({c.id for c in edges if claim.id in c.supersedes})
        if not by:
            self.state.gap(
                GapCode.UNKNOWN,
                "/as_of",
                [claim.id],
                f"Memory stopped holding this claim at transaction {high}; no version current"
                " then names it as superseded",
            )
            return None
        return Superseded(claim.id, high, tuple(by))  # type: ignore[arg-type]

    # --- Ledger windows ------------------------------------------------------------------------

    def ledger(self, claims: list[tuple[float, Claim]]) -> list[tuple[float, Item]]:
        """Series windows and frames in the query's window and regions that ``claims`` name or
        cite, read through the Ledger's interval and spatial indexes."""
        query = self.query
        regions = ordered(query.regions, region_to_json)
        if self.window is None and not regions:
            return []
        if self.catalog is None:
            if self.window is not None:
                self.state.gap(
                    GapCode.NOT_COVERED,
                    "/during",
                    [],
                    "no Ledger catalog is attached: series windows and frames in the window"
                    " are not read",
                )
            for index, _ in enumerate(regions):
                self.state.gap(
                    GapCode.NOT_COVERED,
                    f"/regions/{index}",
                    [],
                    "no Ledger catalog is attached: the frame index is not read",
                )
            return []
        for index, bridge in enumerate(ordered(query.clock_bridges, clock_bridge_to_json)):
            if domain_id(bridge.source) in self.placed or domain_id(bridge.target) in self.placed:
                self.state.gap(
                    GapCode.NOT_COVERED,
                    f"/clock_bridges/{index}",
                    [bridge.mapping_id],
                    "the Ledger reads series on the window's own clock only (Ledger ADR 0016"
                    " §9): records on the bridged clock are not read",
                )
        cited: dict[str, float] = {}
        named: dict[str, float] = {}
        for value, claim in claims:
            for ref in claim.provenance.evidence:
                if isinstance(ref.source, str):
                    cited[ref.source] = max(cited.get(ref.source, 0.0), value)
            records = [*claim.provenance.records]
            if isinstance(claim.object, LedgerRecordRef):
                records.append(claim.object.record_id)
            for record in records:
                named[str(record)] = max(named.get(str(record), 0.0), value)
        if not named and not cited:
            return []  # nothing carried names or cites a record: no window read is needed
        specs: list[tuple[str, FrameRegion | None]] = [
            (f"/regions/{i}", r) for i, r in enumerate(regions)
        ] or [("/during", None)]
        out: list[tuple[float, Item]] = []
        for at, region in specs:
            for row in self.rows(at, region):
                best = max(
                    named.get(str(row.record_id), 0.0),
                    cited.get(str(row.source_content_id or ""), 0.0),
                )
                if best <= 0.0:
                    continue
                item = self.item(row)
                if item is not None:
                    out.append((best * DECAY, item))
        return out

    def rows(self, at: str, region: FrameRegion | None) -> tuple[QueryRow, ...]:
        from neptune_ledger.api import (
            FrameReference,
            FrameWindow,
            QueryBudget,
            QuerySpec,
            TimeWindow,
            query_meta,
            query_rows,
        )

        window = None
        if self.window is not None:
            last = INT64_MAX if self.window.end is None else self.window.end - 1
            window = TimeWindow(self.window.clock, self.window.start, last)
        frame = None
        if region is not None:
            shape = region.shape
            if isinstance(shape, Box):
                low, high = tuple(shape.min), tuple(shape.max)
            else:
                low = tuple(c - shape.radius for c in shape.center)
                high = tuple(c + shape.radius for c in shape.center)
            frame = FrameWindow(
                FrameReference(region.frame.graph_id, region.frame.frame_id),
                region.unit,
                low,
                high,
            )
        spec = QuerySpec(
            kinds=LEDGER_KINDS,
            window=window,
            frame=frame,
            as_of=int(self.snapshot.as_of),
            budget=QueryBudget(max_rows=self.max_rows),
        )
        assert self.catalog is not None
        table = self.catalog.query(spec)
        meta = query_meta(table)
        for finding in meta.findings:
            self.state.gap(
                GapCode.NOT_COVERED,
                at,
                [],
                f"the Ledger answered {finding.code} ({finding.subject}): {finding.detail}"[:2000],
            )
        return tuple(query_rows(table))

    def item(self, row: QueryRow) -> Item | None:
        """A stream row as a series window clipped to the window, an image row as a frame;
        ``None`` (and a gap) when the Ledger cannot say what produced it."""
        transform = self.transform(row)
        if (
            transform is None
            or row.source_content_id is None
            or row.source_locator is None
            or row.assertion_kind is None
        ):
            self.state.gap(
                GapCode.UNKNOWN,
                "/during" if self.window is not None else "/regions",
                [str(row.record_id)],
                "the Ledger states no source, transform or assertion kind for this record",
            )
            return None
        evidence = evidence_ref_from_json(
            {"locator": json.loads(row.source_locator), "source": row.source_content_id}
        )
        provenance = ItemProvenance((evidence,), (row.record_id,), transform)  # type: ignore[arg-type]
        envelope: dict[str, Any] = {
            "assertion_kind": AssertionKind(row.assertion_kind),
            "confidence": NotApplicable(),
            "provenance": provenance,
            "relevance": _PROVISIONAL,
        }
        if row.kind == "stream":
            if row.world_clock is None or row.world_first is None or row.world_last is None:
                return None
            start, end = row.world_first, row.world_last + 1
            if self.window is not None:
                if row.world_clock != self.window.clock:
                    return None
                start = max(start, self.window.start)
                end = end if self.window.end is None else min(end, self.window.end)
            if start >= end:
                return None
            return SeriesWindowItem(
                **envelope,
                stream=row.record_id,  # type: ignore[arg-type]
                clock=row.world_clock,  # type: ignore[arg-type]
                start=start,
                end=end,
                arrow=ArrowHandle(row.package_id, series_path(row.record_id)),  # type: ignore[arg-type]
            )
        instant = (
            Known(Timestamp(row.world_first, row.world_clock))  # type: ignore[arg-type]
            if row.world_clock is not None
            and row.world_first is not None
            and row.world_first == row.world_last
            else Unknown()
        )
        if (
            self.window is not None
            and isinstance(instant, Known)
            and row.world_clock != self.window.clock
        ):
            return None
        return FrameItem(
            **envelope,
            stream=NotApplicable(),
            at=instant,
            evidence=evidence,
            encoding=Unknown(),
            frame=Unknown(),
        )

    def transform(self, row: QueryRow) -> Transform | None:
        """The adapter behind a row's transform, from the Ledger's lineage (once per transform)."""
        if row.transform_id is None or self.catalog is None:
            return None
        if row.transform_id not in self.transforms:
            found = None
            lineage = self.catalog.lineage(row.record_id, as_of=int(self.snapshot.as_of))
            for node in lineage.nodes:
                if node.transform_id == row.transform_id and isinstance(node.transform, Known):
                    info = node.transform.value
                    found = Transform(info.adapter_id, info.adapter_version, info.config_hash)  # type: ignore[arg-type]
            self.transforms[row.transform_id] = found
        return self.transforms[row.transform_id]

    # --- The answer ----------------------------------------------------------------------------

    def run(self) -> ChannelAnswer:
        query = self.query
        self.bridges()
        seeds, kinds = self.seeds()
        clause = query.graph
        hops = 1 if clause is None else clause.hops
        direction = Direction.BOTH if clause is None else Direction(clause.direction)
        names = None if clause is None else clause.predicates
        allowed: Callable[[str], bool] = (
            (lambda p: p != SAME_AS_CANDIDATE) if names is None else (lambda p: p in names)
        )
        if seeds:
            self.walk(
                seeds,
                hops,
                allowed,
                direction,
                record=True,
                kinds=kinds,
                at="/graph" if clause is not None else "",
            )
        for mapping_id in sorted(self.used):  # a placement shows the mapping it went through
            self.state.hit(self.used[mapping_id], 1)
        state = self.state
        claims = sorted(state.hits.values(), key=lambda pair: pair[1].id)
        scored: list[tuple[float, Item]] = [
            (score(claim, level), ClaimItem.of(claim, _PROVISIONAL)) for level, claim in claims
        ]
        try:
            scored += self.ledger([(score(c, lv), c) for lv, c in claims])
        except Exception as exc:  # the Ledger failing loses its items, never the claims
            self.state.gap(
                GapCode.NOT_COVERED,
                "/during" if self.window is not None else "/regions",
                [],
                f"the Ledger could not be read: {type(exc).__name__}: {exc}"[:2000],
            )
        held = set(state.hits)
        superseded = [s for _, c in claims if (s := self.superseded(c)) is not None]
        findings = [
            f
            for f in state.findings.values()
            if ({f.claim, *f.others} & held) and pinned.finding_beyond_pin(f) is None
        ]
        self.finish_gaps()
        return answer(
            Channel.GRAPH, scored, gaps=state.gaps, findings=findings, superseded=superseded
        )

    def finish_gaps(self) -> None:
        state = self.state
        if state.withheld:
            state.gap(
                GapCode.INFERRED_WITHHELD,
                "/include_inferred",
                state.withheld,
                "inferred claims on the walk, withheld because the query excludes inference",
            )
        if state.other_clock:
            state.gap(
                GapCode.OTHER_CLOCK,
                "/during",
                state.other_clock,
                "claims on a clock the query neither asked for nor bridged: named, not compared",
            )
        for reason in sorted(state.beyond):
            state.gap(
                GapCode.NOT_COVERED,
                "",
                state.beyond[reason],
                f"{reason} is not in Context's pinned graph-schema {GRAPH_SCHEMA_VERSION};"
                " the packet cannot describe it",
            )
        for at in sorted(state.truncated):
            what = "the site check" if at == "/site" else "the walk"
            state.gap(
                GapCode.NOT_COVERED,
                at,
                [],
                f"{what} stopped after expanding {self.max_nodes} nodes",
            )
        if state.crowded:
            state.gap(
                GapCode.NOT_COVERED,
                "/graph" if self.query.graph is not None else "",
                state.crowded,
                f"these nodes hold more than {self.max_edges} claims; only the first"
                f" {self.max_edges} by claim id were followed",
            )


# A provisional relevance for items before ``answer`` ranks them (it rewrites every one).
_PROVISIONAL: Final = Relevance(1.0, (ChannelHit(Channel.GRAPH, 1, 1.0),))

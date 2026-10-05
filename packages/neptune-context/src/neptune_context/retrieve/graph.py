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
from neptune_memory.schema.interval import OPEN, Interval, ledger_tx
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


def _carry(window: _Window, clock_map: ClockMap, clock: str, *, forward: bool) -> _Window | None:
    """``window`` carried exactly through a direct map: onto its target (``forward``) or back
    onto its source. Widened by the map's residual bound when it states one; ``None`` when the
    map states no affine parameters (a composed map, or an unknown anchor or rate)."""
    affine = clock_map.affine()
    if affine is None:
        return None
    rate, offset = affine
    residual = clock_map.residual_bound
    slack = Fraction(residual.value.ticks) if isinstance(residual, Known) else Fraction(0)

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
    truncated: bool = False

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
    ) -> None:
        self._memory = memory
        self._catalog = catalog
        self._max_nodes = max_nodes
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
            "max_nodes": self._max_nodes,
            "max_rows": self._max_rows,
            "site_check_hops": SITE_CHECK_HOPS,
            "unknown_confidence_weight": UNKNOWN_CONFIDENCE_WEIGHT,
        }

    def retrieve(self, request: Retrieval) -> ChannelAnswer:
        return _Retrieve(
            self._memory, self._catalog, self._max_nodes, self._max_rows, request
        ).run()


class _Retrieve:
    """One retrieval: seeds, bridges, the walk, the Ledger windows and the answer."""

    def __init__(
        self,
        memory: MemoryReader,
        catalog: CatalogApi | None,
        max_nodes: int,
        max_rows: int,
        request: Retrieval,
    ) -> None:
        self.memory = memory
        self.catalog = catalog
        self.max_nodes = max_nodes
        self.max_rows = max_rows
        self.query: Query = request.query
        self.snapshot = request.snapshot
        self.as_of: LedgerTx = request.snapshot.memory_as_of
        self.state = _State()
        during = self.query.during
        self.window = (
            None if during is None else _Window(domain_id(during.clock), during.start, during.end)
        )
        self.placed: dict[str, _Window] = {}  # bridged clock -> the window carried onto it
        self.mappings: dict[str, Claim] = {}  # bridged clock -> the clock_map claim carrying it
        self.used: set[str] = set()  # bridged clocks a carried claim was placed on
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
        window = self.window if clock == self.window.clock else self.placed.get(clock)
        if window is None:
            if record:
                self.state.other_clock.add(claim.id)
            return False
        return claim.valid.overlaps(window.interval())

    def carry(self, claim: Claim, level: int) -> None:
        """Make ``claim`` a hit, noting the bridged clock it was placed on, if any."""
        self.state.hit(claim, level)
        clock = str(claim.valid.domain_id)
        if clock in self.placed:
            self.used.add(clock)

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
            mapping = self.mapping(bridge.mapping_id, source, target)
            if mapping is None:
                self.state.gap(
                    GapCode.UNKNOWN,
                    at,
                    [bridge.mapping_id],
                    f"Memory holds no clock_map from mapping {bridge.mapping_id} joining"
                    f" {source} to {target} at transaction {self.as_of}; claims on the other"
                    " clock stay apart",
                )
                continue
            claim, clock_map = mapping
            forward = window.clock == source
            other = target if forward else source
            carried = _carry(window, clock_map, other, forward=forward)
            if carried is None:
                self.state.gap(
                    GapCode.UNKNOWN,
                    at,
                    [bridge.mapping_id, claim.id],
                    "the mapping states no anchor and rate (a composed or unknown map), so the"
                    " window cannot be carried exactly",
                )
                continue
            self.placed[other] = carried
            self.mappings[other] = claim

    def mapping(self, mapping_id: str, source: str, target: str) -> tuple[Claim, ClockMap] | None:
        """The ``clock_map`` claim on ``source`` onto ``target`` that cites ``mapping_id``."""
        clock = NodeRef(NodeType.CLOCK, source)
        result = self.memory.claims(clock, CLOCK_MAP, self.as_of, include_inferred=True)
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
            return claim, clock_map
        return None

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
                    self.state.truncated = True  # a site check cut short says so too
                    return reached
                expanded += 1
                view = self.view(here)
                if not isinstance(view, Known):
                    continue
                if record:
                    for finding in view.value.findings:
                        self.state.findings.setdefault(finding.id, finding)
                for claim in sorted((*view.value.claims, *view.value.incoming), key=lambda c: c.id):
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

    def superseded(self, claim: Claim) -> Superseded | None:
        """When, in ``(memory_as_of, head]``, Memory stopped holding ``claim``, and what by."""
        low, high = int(self.as_of), min(int(self.memory.head), int(self.snapshot.head))
        if low >= high or any(c.id == claim.id for c in self.present(claim, high)):
            return None
        while high - low > 1:  # current at ``low``, gone at ``high``
            middle = (low + high) // 2
            if any(c.id == claim.id for c in self.present(claim, middle)):
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
            self.walk(seeds, hops, allowed, direction, record=True, kinds=kinds)
        for clock in sorted(self.used):  # a placement shows the mapping it went through
            self.state.hit(self.mappings[clock], 1)
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
                f"{reason} is newer than Context's pinned graph-schema {GRAPH_SCHEMA_VERSION};"
                " the packet cannot describe it",
            )
        if state.truncated:
            state.gap(
                GapCode.NOT_COVERED,
                "/graph" if self.query.graph is not None else "",
                [],
                f"the walk stopped after expanding {self.max_nodes} nodes",
            )


# A provisional relevance for items before ``answer`` ranks them (it rewrites every one).
_PROVISIONAL: Final = Relevance(1.0, (ChannelHit(Channel.GRAPH, 1, 1.0),))

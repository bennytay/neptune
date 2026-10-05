"""Is this query answerable without a silent assumption? (ADR 0002 §7)

``validate`` returns every finding in a fixed order, and an empty tuple for an answerable query.
It checks values against the pinned upstream contracts (subject kinds from Memory's node types
and the Ledger's thread kinds, predicates from graph-schema 1's vocabulary, civil clocks by
Memory's ``CivilClock`` rule, units by the compiler's catalogue), and refuses any query that
could mix clocks or frames without naming the record that relates them:

- the two instants of one ``diff`` on different clocks need a chain of ``clock_bridges``;
- regions in different frames need a chain of ``frame_bridges``, and all regions one unit;
- a bridge must reach a clock (or frame) the query uses, so no bridge is silently ignored.

Evidence on a clock other than ``during``'s is never refused: Memory returns it apart, as
``other_clocks``, unless a bridge names the mapping that places it (graph-schema guarantee 8).
"""

from __future__ import annotations

import math
import re
from collections.abc import Hashable
from typing import TYPE_CHECKING, Final, Generic, TypeVar, get_args

from neptune_ledger.api import ThreadKind
from neptune_memory.schema.interval import CivilClock
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES, is_declared_value

from neptune.model.ids import check_token
from neptune.model.time import Epoch, Timescale
from neptune.model.units import Dimension, unit_from_json
from neptune_context.query.codec import (
    canonical_bytes,
    clock_bridge_to_json,
    frame_bridge_to_json,
    ordered,
    region_to_json,
    subject_to_json,
)
from neptune_context.query.findings import FindingCode, QueryFinding
from neptune_context.query.model import (
    INT64_MAX,
    INT64_MIN,
    MAX_BRIDGES,
    MAX_BYTES,
    MAX_EXPLAIN,
    MAX_HOPS,
    MAX_ITEMS,
    MAX_LATENCY_MS,
    MAX_REGIONS,
    MAX_SAME_AS_DEPTH,
    MAX_SUBJECTS,
    MAX_TEXT_CHARS,
    MAX_TOKENS,
    MAX_ZONES,
    Box,
    Clock,
    Diff,
    DomainClock,
    Explain,
    FrameRef,
    FrameRegion,
    Instant,
    Query,
    Subject,
    Why,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

N = TypeVar("N", bound=Hashable)

# Subject kinds: Memory's node types (graph-schema 1) and the Ledger's thread kinds (catalog-api).
SUBJECT_KINDS: Final = frozenset(str(t) for t in NodeType) | frozenset(get_args(ThreadKind))
# Graph predicates: the vocabulary of the pinned graph-schema major.
PREDICATES: Final = frozenset(spec.name for spec in CORE_PREDICATES.specs)
_RECORD_ID: Final = re.compile(r"rec:sha256:[0-9a-f]{64}")
_CLAIM_ID: Final = re.compile(r"claim:sha256:[0-9a-f]{64}")
_LENGTH: Final = Dimension(length=1)


def is_declared_id(text: str) -> bool:
    """``<namespace>:<value>``: a compiler token, then a value that is neither blank nor padded."""
    namespace, colon, value = text.partition(":")
    if not colon or not is_declared_value(value):
        return False
    try:
        check_token("namespace", namespace)
    except ValueError:
        return False
    return True


class _Validator:
    def __init__(self, query: Query) -> None:
        self.query = query
        self.findings: list[QueryFinding] = []
        self.instant_clocks: list[Clock] = []  # every clock a valid-time diff names

    def add(self, code: FindingCode, at: str, message: str) -> None:
        self.findings.append(QueryFinding(code, at, message))

    def bounded(self, value: int, low: int, high: int, at: str, what: str) -> bool:
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            self.add(FindingCode.OUT_OF_RANGE, at, f"{what} must be an integer in [{low}, {high}]")
            return False
        return True

    def count(self, members: Iterable[object], high: int, at: str, what: str) -> None:
        size = len(list(members))
        if size > high:
            self.add(FindingCode.OUT_OF_RANGE, at, f"at most {high} {what}, got {size}")

    def record_id(self, value: str, at: str, what: str) -> None:
        if not _RECORD_ID.fullmatch(value):
            self.add(FindingCode.BAD_IDENTIFIER, at, f"{what} is a record id rec:sha256:<hex>")

    def declared_id(self, value: str, at: str) -> None:
        if not is_declared_id(value):
            self.add(
                FindingCode.BAD_IDENTIFIER,
                at,
                "a declared id is <namespace>:<value>: a lowercase token, then a value that is "
                "neither blank nor padded with whitespace",
            )

    # Clauses.

    def clock(self, clock: Clock, at: str) -> None:
        if isinstance(clock, DomainClock):
            self.record_id(clock.domain_id, f"{at}/domain_id", "a clock's domain_id")
            return
        try:
            CivilClock(Timescale(clock.timescale), Epoch(clock.epoch), clock.resolution)
            if max(clock.resolution.numerator, clock.resolution.denominator) > INT64_MAX:
                raise ValueError("resolution terms beyond int64")
        except (ValueError, TypeError, AttributeError):
            self.add(
                FindingCode.BAD_CLOCK,
                at,
                "a civil time is an absolute timescale (gps, posix, tai, utc), an absolute epoch "
                "(gps, unix) and a positive resolution with int64 terms; a device's own clock is a "
                "domain clock",
            )

    def ticks(self, value: int, at: str) -> bool:
        return self.bounded(value, INT64_MIN, INT64_MAX, at, "ticks")

    def subject(self, subject: Subject, at: str) -> None:
        if subject.kind not in SUBJECT_KINDS:
            self.add(
                FindingCode.UNKNOWN_KIND,
                f"{at}/kind",
                "a subject kind is a graph-schema node type or a catalog-api thread kind",
            )
        if subject.declared_id is not None:
            self.declared_id(subject.declared_id, f"{at}/declared_id")
        if self.bounded(
            subject.same_as_depth, 0, MAX_SAME_AS_DEPTH, f"{at}/same_as_depth", "same_as_depth"
        ) and (subject.same_as_depth and subject.declared_id is None):
            self.add(
                FindingCode.SAME_AS_WITHOUT_ID,
                f"{at}/same_as_depth",
                "same_as widens one declared subject; a kind-wide selector has depth 0",
            )

    def frame(self, frame: FrameRef, at: str) -> None:
        if not frame.frame_id:
            self.add(FindingCode.BAD_IDENTIFIER, f"{at}/frame_id", "a frame id is not empty")
        self.record_id(frame.graph_id, f"{at}/graph_id", "a frame graph id")

    def region(self, region: FrameRegion, at: str) -> None:
        self.frame(region.frame, f"{at}/frame")
        try:
            unit = unit_from_json(region.unit)
        except (ValueError, TypeError):
            unit = None
        if unit is None or unit.dimension != _LENGTH:
            self.add(
                FindingCode.BAD_UNIT,
                f"{at}/unit",
                "a region's unit is a catalogued length symbol in canonical form, e.g. m or mm",
            )
        shape = region.shape
        at = f"{at}/shape"
        coordinates = (
            [*shape.min, *shape.max] if isinstance(shape, Box) else [*shape.center, shape.radius]
        )
        if not all(_is_number(c) and math.isfinite(c) for c in coordinates):
            self.add(FindingCode.BAD_REGION, at, "every coordinate is finite")
        elif isinstance(shape, Box):
            if not all(low < high for low, high in zip(shape.min, shape.max, strict=True)):
                self.add(FindingCode.BAD_REGION, at, "a box has min < max on every axis")
        elif not shape.radius > 0:
            self.add(FindingCode.BAD_REGION, at, "a sphere's radius is positive")

    def text(self) -> None:
        clause = self.query.text
        if clause is None:
            return
        body = clause.text
        if not body.strip() or len(body) > MAX_TEXT_CHARS:
            self.add(
                FindingCode.BAD_TEXT,
                "/text/text",
                f"text is not blank and at most {MAX_TEXT_CHARS} characters",
            )
        elif any(ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0 for c in body):
            self.add(FindingCode.BAD_TEXT, "/text/text", "text carries no control characters")
        else:
            try:
                body.encode("utf-8")
            except UnicodeEncodeError:
                self.add(FindingCode.BAD_TEXT, "/text/text", "text is valid Unicode")
        if not clause.fields:
            self.add(FindingCode.EMPTY, "/text/fields", "name at least one field to search")
        if not clause.channels:
            self.add(FindingCode.EMPTY, "/text/channels", "name at least one channel")

    def budget(self) -> None:
        budget = self.query.budget
        self.bounded(budget.items, 1, MAX_ITEMS, "/budget/items", "items")
        for name, high in (
            ("tokens", MAX_TOKENS),
            ("bytes", MAX_BYTES),
            ("latency_ms", MAX_LATENCY_MS),
        ):
            limit = getattr(budget, name)
            if limit is not None:
                self.bounded(limit, 1, high, f"/budget/{name}", name)

    def graph(self) -> None:
        clause = self.query.graph
        if clause is None:
            return
        if clause.predicates is not None:
            if not clause.predicates:
                self.add(FindingCode.EMPTY, "/graph/predicates", "name a predicate, or 'any'")
            for index, name in enumerate(sorted(clause.predicates)):
                if name not in PREDICATES:
                    self.add(
                        FindingCode.UNKNOWN_PREDICATE,
                        f"/graph/predicates/{index}",
                        f"{name!r} is not in the graph-schema 1 vocabulary",
                    )
        self.bounded(clause.hops, 1, MAX_HOPS, "/graph/hops", "hops")
        anchored = self.query.site is not None or any(
            s.declared_id is not None for s in self.query.subjects
        )
        if not anchored:
            self.add(
                FindingCode.GRAPH_WITHOUT_ANCHOR,
                "/graph",
                "graph hops start from a declared subject or a site",
            )

    def explain(self) -> list[tuple[Clock, str, Clock, str]]:
        """Check each item; return each valid-time diff's two clocks with their pointers."""
        pairs: list[tuple[Clock, str, Clock, str]] = []
        self.count(self.query.explain, MAX_EXPLAIN, "/explain", "explain items")
        seen: set[Explain] = set()
        for index, item in enumerate(self.query.explain):
            at = f"/explain/{index}"
            if item in seen:
                self.add(FindingCode.DUPLICATE, at, "repeats an earlier explain item")
            seen.add(item)
            if isinstance(item, Why):
                if not _CLAIM_ID.fullmatch(item.claim_id):
                    self.add(
                        FindingCode.BAD_IDENTIFIER,
                        f"{at}/claim_id",
                        "a claim id is claim:sha256:<hex>",
                    )
                continue
            pair = self.diff(item, at)
            if pair is not None:
                pairs.append(pair)
        return pairs

    def diff(self, item: Diff, at: str) -> tuple[Clock, str, Clock, str] | None:
        self.subject(item.subject, f"{at}/subject")
        if item.subject.declared_id is None:
            self.add(
                FindingCode.BAD_IDENTIFIER,
                f"{at}/subject",
                "a diff names one subject by its declared id",
            )
        before, after = item.before, item.after
        if isinstance(before, Instant) and isinstance(after, Instant):
            self.instant_clocks += [before.clock, after.clock]
            self.clock(before.clock, f"{at}/before/clock")
            self.clock(after.clock, f"{at}/after/clock")
            ok = self.ticks(before.ticks, f"{at}/before/ticks")
            ok = self.ticks(after.ticks, f"{at}/after/ticks") and ok
            if before.clock != after.clock:
                return before.clock, f"{at}/before/clock", after.clock, f"{at}/after/clock"
            if ok and not before.ticks < after.ticks:
                self.add(FindingCode.DIFF_NOT_ORDERED, at, "before is earlier than after")
            return None
        if isinstance(before, Instant) or isinstance(after, Instant):
            self.add(
                FindingCode.DIFF_MIXED_AXES,
                at,
                "a diff compares two transactions or two instants, never one of each",
            )
            return None
        ok = self.bounded(before, 0, INT64_MAX, f"{at}/before", "a transaction")
        ok = self.bounded(after, 0, INT64_MAX, f"{at}/after", "a transaction") and ok
        if ok and not before < after:
            self.add(FindingCode.DIFF_NOT_ORDERED, at, "before is earlier than after")
        as_of = self.query.as_of
        if ok and isinstance(as_of, int) and after > as_of:
            self.add(
                FindingCode.DIFF_BEYOND_AS_OF,
                f"{at}/after",
                "a diff cannot look past the query's as_of snapshot",
            )
        return None

    # The whole query.

    def run(self) -> list[QueryFinding]:
        query = self.query
        if query.as_of != "head":
            self.bounded(query.as_of, 0, INT64_MAX, "/as_of", "as_of")
        self.count(query.subjects, MAX_SUBJECTS, "/subjects", "subjects")
        for index, subject in enumerate(ordered(query.subjects, subject_to_json)):
            self.subject(subject, f"/subjects/{index}")
        if query.during is not None:
            during = query.during
            self.clock(during.clock, "/during/clock")
            ok = self.ticks(during.start, "/during/start")
            if during.end is not None:
                ok = self.ticks(during.end, "/during/end") and ok
                if ok and not during.start < during.end:
                    self.add(FindingCode.BAD_INTERVAL, "/during", "during needs start < end")
        bridges = ordered(query.clock_bridges, clock_bridge_to_json)
        self.count(bridges, MAX_BRIDGES, "/clock_bridges", "clock bridges")
        for index, bridge in enumerate(bridges):
            at = f"/clock_bridges/{index}"
            self.record_id(bridge.mapping_id, f"{at}/mapping_id", "a clock mapping id")
            self.clock(bridge.source, f"{at}/source")
            self.clock(bridge.target, f"{at}/target")
            if bridge.source == bridge.target:
                self.add(FindingCode.BAD_CLOCK, at, "a clock bridge joins two different clocks")
        regions = ordered(query.regions, region_to_json)
        self.count(regions, MAX_REGIONS, "/regions", "regions")
        for index, region in enumerate(regions):
            self.region(region, f"/regions/{index}")
        frame_bridges = ordered(query.frame_bridges, frame_bridge_to_json)
        self.count(frame_bridges, MAX_BRIDGES, "/frame_bridges", "frame bridges")
        for index, frame_bridge in enumerate(frame_bridges):
            at = f"/frame_bridges/{index}"
            self.record_id(frame_bridge.transform_id, f"{at}/transform_id", "a transform id")
            self.frame(frame_bridge.parent, f"{at}/parent")
            self.frame(frame_bridge.child, f"{at}/child")
            if frame_bridge.parent == frame_bridge.child:
                self.add(FindingCode.BAD_REGION, at, "a frame bridge joins two different frames")
        if query.site is not None:
            self.declared_id(query.site.site, "/site/site")
            self.count(query.site.zones, MAX_ZONES, "/site/zones", "zones")
            for index, zone in enumerate(sorted(query.site.zones)):
                self.declared_id(zone, f"/site/zones/{index}")
        self.graph()
        self.text()
        self.budget()
        diffs = self.explain()
        if not (query.subjects or query.regions or query.site or query.text or query.explain):
            self.add(
                FindingCode.EMPTY_QUERY,
                "/",
                "a query selects something: a subject, region, site, text or explain item",
            )
        self.clocks(diffs)
        self.frames(regions)
        return self.findings

    def clocks(self, diffs: list[tuple[Clock, str, Clock, str]]) -> None:
        """Each cross-clock diff is bridged, and every bridge reaches a clock the query uses."""
        links = _Components[Clock]()
        for bridge in self.query.clock_bridges:
            links.join(bridge.source, bridge.target)
        for before, before_at, after, after_at in diffs:
            if not links.joined(before, after):
                self.add(
                    FindingCode.CROSS_CLOCK_WITHOUT_MAPPING,
                    after_at,
                    f"this instant's clock differs from {before_at}; name the ClockMapping that "
                    "relates them in clock_bridges",
                )
        used = list(self.instant_clocks)
        if self.query.during is not None:
            used.append(self.query.during.clock)
        bridges = ordered(self.query.clock_bridges, clock_bridge_to_json)
        for index, bridge in enumerate(bridges):
            if not any(links.joined(bridge.source, clock) for clock in used):
                self.add(
                    FindingCode.DANGLING_CLOCK_BRIDGE,
                    f"/clock_bridges/{index}",
                    "this bridge reaches neither the during clock nor a diff's clocks",
                )

    def frames(self, regions: list[FrameRegion]) -> None:
        """All regions in one frame or joined by bridges, in one unit; no bridge left over."""
        links = _Components[FrameRef]()
        for bridge in self.query.frame_bridges:
            links.join(bridge.parent, bridge.child)
        for index, region in enumerate(regions[1:], start=1):
            if not links.joined(regions[0].frame, region.frame):
                self.add(
                    FindingCode.CROSS_FRAME_WITHOUT_TRANSFORM,
                    f"/regions/{index}/frame",
                    "this region's frame differs from /regions/0/frame; name the FrameTransform "
                    "that relates them in frame_bridges",
                )
            if region.unit != regions[0].unit:
                self.add(
                    FindingCode.MIXED_REGION_UNITS,
                    f"/regions/{index}/unit",
                    "every region is declared in /regions/0/unit; nothing converts units",
                )
        bridges = ordered(self.query.frame_bridges, frame_bridge_to_json)
        for index, bridge in enumerate(bridges):
            if not any(links.joined(bridge.parent, region.frame) for region in regions):
                self.add(
                    FindingCode.DANGLING_FRAME_BRIDGE,
                    f"/frame_bridges/{index}",
                    "this bridge reaches no region's frame",
                )


class _Components(Generic[N]):
    """Connected components of the bridges: which clocks (or frames) a chain of bridges joins."""

    def __init__(self) -> None:
        self._parent: dict[N, N] = {}

    def _root(self, node: N) -> N:
        while (parent := self._parent.get(node, node)) != node:
            node = parent
        return node

    def join(self, left: N, right: N) -> None:
        self._parent[self._root(left)] = self._root(right)

    def joined(self, left: N, right: N) -> bool:
        return left == right or self._root(left) == self._root(right)


def _is_number(value: object) -> bool:
    """A coordinate is an ``int`` or ``float`` (never a ``bool``) that converts to a float."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        float(value)
    except OverflowError:
        return False
    return True


def validate(query: Query) -> tuple[QueryFinding, ...]:
    """Every reason ``query`` cannot be answered as written; empty when it can.

    A query built in Python may hold what JSON cannot (a lone surrogate, an integer too large for
    a float coordinate); it is refused first, since it has no canonical bytes and so no id.
    """
    try:
        canonical_bytes(query)
    except (ValueError, TypeError, OverflowError, AttributeError):
        return (
            QueryFinding(
                FindingCode.SHAPE,
                "/",
                "the query has no canonical JSON: a value is not representable (lone surrogate, "
                "non-finite or oversized number, or a member of the wrong type)",
            ),
        )
    return tuple(_Validator(query).run())

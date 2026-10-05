"""Member types of a ``Query`` built in Python (ADR 0002 §7): wrong types are findings, not crashes.

``decode`` already refuses wrong types in JSON. A query constructed directly can still hold, say,
``Subject("machine", 5)``; ``shape_findings`` reports each such member as ``shape`` at its JSON
pointer so ``validate`` never raises on it. Integers exclude ``bool``; coordinates may be any
``int`` or ``float`` here (``validate`` judges their values).
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING

from neptune_context.query.codec import (
    clock_bridge_to_json,
    frame_bridge_to_json,
    ordered,
    region_to_json,
    subject_to_json,
)
from neptune_context.query.findings import FindingCode, QueryFinding
from neptune_context.query.model import (
    HEAD,
    Box,
    Budget,
    CivilTime,
    ClockBridge,
    Direction,
    DomainClock,
    During,
    FrameBridge,
    FrameRef,
    FrameRegion,
    GraphClause,
    Instant,
    SiteScope,
    Subject,
    TextChannel,
    TextClause,
    TextField,
    Why,
)

if TYPE_CHECKING:
    from neptune_context.query.model import Query


class _Shape:
    def __init__(self) -> None:
        self.findings: list[QueryFinding] = []

    def fail(self, at: str, expected: str) -> None:
        self.findings.append(QueryFinding(FindingCode.SHAPE, at, f"expected {expected}"))

    def is_(self, value: object, kind: type | tuple[type, ...], at: str, expected: str) -> bool:
        ok = isinstance(value, kind)
        if not ok:
            self.fail(at, expected)
        return ok

    def int_(self, value: object, at: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(at, "an integer")

    def str_(self, value: object, at: str) -> None:
        self.is_(value, str, at, "a string")

    def frozenset_(self, value: object, at: str) -> bool:
        return self.is_(value, frozenset, at, "a frozenset")

    def clock(self, value: object, at: str) -> None:
        if isinstance(value, DomainClock):
            self.str_(value.domain_id, f"{at}/domain_id")
        elif isinstance(value, CivilTime):
            self.str_(value.timescale, f"{at}/timescale")
            self.str_(value.epoch, f"{at}/epoch")
            self.is_(value.resolution, Fraction, f"{at}/resolution", "a Fraction")
        else:
            self.fail(at, "a DomainClock or CivilTime")

    def frame(self, value: object, at: str) -> None:
        if self.is_(value, FrameRef, at, "a FrameRef"):
            assert isinstance(value, FrameRef)
            self.str_(value.frame_id, f"{at}/frame_id")
            self.str_(value.graph_id, f"{at}/graph_id")

    def vec(self, value: object, at: str) -> None:
        if not (isinstance(value, tuple) and len(value) == 3):
            self.fail(at, "a tuple of three coordinates")
            return
        for index, coordinate in enumerate(value):
            self.is_(coordinate, (int, float), f"{at}/{index}", "a number")

    def subject(self, value: object, at: str) -> None:
        if not self.is_(value, Subject, at, "a Subject"):
            return
        assert isinstance(value, Subject)
        self.str_(value.kind, f"{at}/kind")
        if value.declared_id is not None:
            self.str_(value.declared_id, f"{at}/declared_id")
        self.int_(value.same_as_depth, f"{at}/same_as_depth")

    def point(self, value: object, at: str) -> None:
        if isinstance(value, Instant):
            self.clock(value.clock, f"{at}/clock")
            self.int_(value.ticks, f"{at}/ticks")
        else:
            self.int_(value, at)

    def strings(self, value: object, at: str) -> None:
        if self.frozenset_(value, at):
            assert isinstance(value, frozenset)
            for index, member in enumerate(sorted(value, key=repr)):
                self.str_(member, f"{at}/{index}")

    def enums(self, value: object, kind: type, at: str) -> None:
        if self.frozenset_(value, at):
            assert isinstance(value, frozenset)
            for index, member in enumerate(sorted(value, key=repr)):
                self.is_(member, kind, f"{at}/{index}", f"a {kind.__name__}")

    def query(self, query: Query) -> None:
        self.is_(query.include_inferred, bool, "/include_inferred", "true or false")
        if query.as_of != HEAD:
            self.int_(query.as_of, "/as_of")
        if self.is_(query.budget, Budget, "/budget", "a Budget"):
            self.int_(query.budget.items, "/budget/items")
            for name in ("tokens", "bytes", "latency_ms"):
                if getattr(query.budget, name) is not None:
                    self.int_(getattr(query.budget, name), f"/budget/{name}")
        if self.frozenset_(query.subjects, "/subjects"):
            for index, subject in enumerate(ordered(query.subjects, subject_to_json)):
                self.subject(subject, f"/subjects/{index}")
        if query.during is not None and self.is_(query.during, During, "/during", "a During"):
            self.clock(query.during.clock, "/during/clock")
            self.int_(query.during.start, "/during/start")
            if query.during.end is not None:
                self.int_(query.during.end, "/during/end")
        if self.frozenset_(query.clock_bridges, "/clock_bridges"):
            bridges = ordered(query.clock_bridges, clock_bridge_to_json)
            for index, bridge in enumerate(bridges):
                at = f"/clock_bridges/{index}"
                if self.is_(bridge, ClockBridge, at, "a ClockBridge"):
                    self.str_(bridge.mapping_id, f"{at}/mapping_id")
                    self.clock(bridge.source, f"{at}/source")
                    self.clock(bridge.target, f"{at}/target")
        if self.frozenset_(query.regions, "/regions"):
            for index, region in enumerate(ordered(query.regions, region_to_json)):
                at = f"/regions/{index}"
                if self.is_(region, FrameRegion, at, "a FrameRegion"):
                    self.frame(region.frame, f"{at}/frame")
                    self.str_(region.unit, f"{at}/unit")
                    shape = region.shape
                    if isinstance(shape, Box):
                        self.vec(shape.min, f"{at}/shape/min")
                        self.vec(shape.max, f"{at}/shape/max")
                    else:  # anything else has no canonical JSON: refused before this
                        self.vec(shape.center, f"{at}/shape/center")
                        self.is_(shape.radius, (int, float), f"{at}/shape/radius", "a number")
        if self.frozenset_(query.frame_bridges, "/frame_bridges"):
            frame_bridges = ordered(query.frame_bridges, frame_bridge_to_json)
            for index, frame_bridge in enumerate(frame_bridges):
                at = f"/frame_bridges/{index}"
                if self.is_(frame_bridge, FrameBridge, at, "a FrameBridge"):
                    self.str_(frame_bridge.transform_id, f"{at}/transform_id")
                    self.frame(frame_bridge.parent, f"{at}/parent")
                    self.frame(frame_bridge.child, f"{at}/child")
        if query.site is not None and self.is_(query.site, SiteScope, "/site", "a SiteScope"):
            self.str_(query.site.site, "/site/site")
            self.strings(query.site.zones, "/site/zones")
        if query.graph is not None and self.is_(query.graph, GraphClause, "/graph", "a graph"):
            if query.graph.predicates is not None:
                self.strings(query.graph.predicates, "/graph/predicates")
            self.int_(query.graph.hops, "/graph/hops")
            self.is_(query.graph.direction, Direction, "/graph/direction", "a Direction")
        if query.text is not None and self.is_(query.text, TextClause, "/text", "a TextClause"):
            self.str_(query.text.text, "/text/text")
            self.enums(query.text.fields, TextField, "/text/fields")
            self.enums(query.text.channels, TextChannel, "/text/channels")
        if self.is_(query.explain, tuple, "/explain", "a tuple"):
            for index, item in enumerate(query.explain):
                at = f"/explain/{index}"
                if isinstance(item, Why):
                    self.str_(item.claim_id, f"{at}/claim_id")
                else:  # a Diff; anything else has no canonical JSON and is refused before this
                    self.subject(item.subject, f"{at}/subject")
                    self.point(item.before, f"{at}/before")
                    self.point(item.after, f"{at}/after")


def shape_findings(query: Query) -> list[QueryFinding]:
    """Every member of ``query`` whose Python type is not the one the model declares."""
    checker = _Shape()
    checker.query(query)
    return checker.findings

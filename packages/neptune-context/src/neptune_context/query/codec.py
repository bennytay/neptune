"""The query's canonical JSON (ADR 0002 §6): ``to_json``, canonical bytes and ``query_id``.

``to_json`` writes every member in a fixed shape: lists always present (possibly empty), absent
single clauses omitted (canonical JSON has no ``null``), set-like members ordered by their own
canonical JSON, coordinates always floats. ``canonical_bytes`` is the compiler's canonical JSON
of that value, so the same query gives the same bytes on every machine, and ``query_id`` hashes
them. Reading is ``decode``.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final, TypeVar

from neptune.identity import canonical_json
from neptune_context.query.model import (
    QUERY_VERSION,
    Box,
    Clock,
    ClockBridge,
    DomainClock,
    Explain,
    FrameBridge,
    FrameRef,
    FrameRegion,
    Instant,
    Query,
    Shape,
    Subject,
    Vec3,
    Why,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from neptune.model.jsonvalue import JsonObject, JsonValue

T = TypeVar("T")
OPEN: Final = "open"
ANY: Final = "any"


# --- Encoding ---------------------------------------------------------------------------------


def clock_to_json(clock: Clock) -> JsonObject:
    if isinstance(clock, DomainClock):
        return {"domain_id": clock.domain_id, "kind": "domain"}
    return {
        "epoch": clock.epoch,
        "kind": "civil",
        "resolution": {
            "denominator": clock.resolution.denominator,
            "numerator": clock.resolution.numerator,
        },
        "timescale": clock.timescale,
    }


def _point_to_json(point: int | Instant) -> JsonValue:
    if isinstance(point, Instant):
        return {"clock": clock_to_json(point.clock), "ticks": point.ticks}
    return point


def frame_to_json(frame: FrameRef) -> JsonObject:
    return {"frame_id": frame.frame_id, "graph_id": frame.graph_id}


def _vec(values: Vec3) -> list[JsonValue]:
    return [float(v) for v in values]


def _shape_to_json(shape: Shape) -> JsonObject:
    if isinstance(shape, Box):
        return {"kind": "box", "max": _vec(shape.max), "min": _vec(shape.min)}
    return {"center": _vec(shape.center), "kind": "sphere", "radius": float(shape.radius)}


def clock_bridge_to_json(bridge: ClockBridge) -> JsonObject:
    return {
        "mapping_id": bridge.mapping_id,
        "source": clock_to_json(bridge.source),
        "target": clock_to_json(bridge.target),
    }


def region_to_json(region: FrameRegion) -> JsonObject:
    return {
        "frame": frame_to_json(region.frame),
        "shape": _shape_to_json(region.shape),
        "unit": region.unit,
    }


def frame_bridge_to_json(bridge: FrameBridge) -> JsonObject:
    return {
        "child": frame_to_json(bridge.child),
        "parent": frame_to_json(bridge.parent),
        "transform_id": bridge.transform_id,
    }


def ordered(members: Iterable[T], encode: Callable[[T], JsonObject]) -> list[T]:
    """Set members in the order of their canonical JSON bytes: one order on every machine.

    ``to_json`` writes sets in this order, so a finding's JSON pointer index names the member
    the canonical JSON shows at that index.
    """
    return sorted(members, key=lambda member: canonical_json.dumps(encode(member)))


def subject_to_json(subject: Subject) -> JsonObject:
    out: dict[str, JsonValue] = {"kind": subject.kind, "same_as_depth": subject.same_as_depth}
    if subject.declared_id is not None:
        out["declared_id"] = subject.declared_id
    return out


def _explain_to_json(item: Explain) -> JsonObject:
    if isinstance(item, Why):
        return {"claim_id": item.claim_id, "kind": "why"}
    return {
        "after": _point_to_json(item.after),
        "before": _point_to_json(item.before),
        "kind": "diff",
        "subject": subject_to_json(item.subject),
    }


def to_json(query: Query) -> JsonObject:
    """The query's canonical JSON value (draft 2020-12 schema: ``#/$defs/Query``)."""
    budget: dict[str, JsonValue] = {"items": query.budget.items}
    for name in ("tokens", "bytes", "latency_ms"):
        limit = getattr(query.budget, name)
        if limit is not None:
            budget[name] = limit
    out: dict[str, JsonValue] = {
        "as_of": query.as_of,
        "budget": budget,
        "clock_bridges": [
            clock_bridge_to_json(b) for b in ordered(query.clock_bridges, clock_bridge_to_json)
        ],
        "explain": [_explain_to_json(item) for item in query.explain],
        "frame_bridges": [
            frame_bridge_to_json(b) for b in ordered(query.frame_bridges, frame_bridge_to_json)
        ],
        "include_inferred": query.include_inferred,
        "query_version": QUERY_VERSION,
        "regions": [region_to_json(r) for r in ordered(query.regions, region_to_json)],
        "subjects": [subject_to_json(s) for s in ordered(query.subjects, subject_to_json)],
    }
    if query.during is not None:
        during = query.during
        out["during"] = {
            "clock": clock_to_json(during.clock),
            "end": OPEN if during.end is None else during.end,
            "start": during.start,
        }
    if query.site is not None:
        out["site"] = {"site": query.site.site, "zones": sorted(query.site.zones)}
    if query.graph is not None:
        graph = query.graph
        out["graph"] = {
            "direction": str(graph.direction),
            "hops": graph.hops,
            "predicates": ANY if graph.predicates is None else sorted(graph.predicates),
        }
    if query.text is not None:
        text = query.text
        out["text"] = {
            "channels": sorted(str(c) for c in text.channels),
            "fields": sorted(str(f) for f in text.fields),
            "text": text.text,
        }
    return out


def canonical_bytes(query: Query) -> bytes:
    """The compiler's canonical JSON of ``to_json(query)``: byte-identical on every machine."""
    return canonical_json.dumps(to_json(query))


def query_id(query: Query) -> str:
    """``query:sha256:<hex>`` of the canonical bytes; equal queries, equal ids."""
    return "query:sha256:" + hashlib.sha256(canonical_bytes(query)).hexdigest()

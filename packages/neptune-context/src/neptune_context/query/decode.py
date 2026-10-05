"""Reading a query (ADR 0002 §6): strict, bounded, and never raising on hostile input.

``loads`` takes JSON text (at most ``MAX_DOCUMENT_BYTES``, no duplicate keys, no NaN or
Infinity); ``from_json`` takes a parsed value. Both report every shape problem as a finding, then
run ``validate``; the result is the ``Query`` or ``Refused`` with every finding.
"""

from __future__ import annotations

import json
import math
from fractions import Fraction
from typing import TYPE_CHECKING, Any

from neptune_context.query.codec import ANY, OPEN
from neptune_context.query.findings import FindingCode, QueryFinding, Refused
from neptune_context.query.model import (
    HEAD,
    MAX_DOCUMENT_BYTES,
    QUERY_VERSION,
    AsOf,
    Box,
    Budget,
    CivilTime,
    Clock,
    ClockBridge,
    Diff,
    Direction,
    DomainClock,
    During,
    Explain,
    FrameBridge,
    FrameRef,
    FrameRegion,
    GraphClause,
    Instant,
    Query,
    Shape,
    SiteScope,
    Sphere,
    Subject,
    TextChannel,
    TextClause,
    TextField,
    Vec3,
    Why,
)
from neptune_context.query.validate import validate

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from neptune.model.jsonvalue import JsonValue


def _is_unicode(text: str) -> bool:
    """JSON lets ``\\ud800`` through as a lone surrogate; canonical JSON (UTF-8) cannot carry it."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class _Decoder:
    """Walks untrusted JSON, recording a finding at each wrong member and returning ``None``."""

    def __init__(self) -> None:
        self.findings: list[QueryFinding] = []

    def fail(self, at: str, message: str, code: FindingCode = FindingCode.SHAPE) -> None:
        self.findings.append(QueryFinding(code, at or "/", message))

    def obj(
        self, value: Any, at: str, required: tuple[str, ...], optional: tuple[str, ...] = ()
    ) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            self.fail(at, "expected an object")
            return None
        missing = [k for k in required if k not in value]
        extra = sorted(k for k in value if k not in required and k not in optional)
        for key in missing:
            self.fail(f"{at}/{key}", "missing member")
        for key in extra:
            self.fail(f"{at}/{key}", "unknown member")
        return None if missing or extra else value

    def string(self, value: Any, at: str) -> str | None:
        if not isinstance(value, str):
            self.fail(at, "expected a string")
            return None
        if not _is_unicode(value):
            self.fail(at, "a string is valid Unicode: no lone surrogate escapes")
            return None
        return value

    def integer(self, value: Any, at: str) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(at, "expected an integer")
            return None
        return value

    def boolean(self, value: Any, at: str) -> bool | None:
        if not isinstance(value, bool):
            self.fail(at, "expected true or false")
            return None
        return value

    def number(self, value: Any, at: str) -> float | None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            self.fail(at, "expected a number")
            return None
        try:
            number = float(value)
        except OverflowError:
            self.fail(at, "number is not finite", FindingCode.BAD_REGION)
            return None
        if not math.isfinite(number):
            self.fail(at, "number is not finite", FindingCode.BAD_REGION)
            return None
        return number

    def array(self, value: Any, at: str) -> list[Any] | None:
        if not isinstance(value, list):
            self.fail(at, "expected an array")
            return None
        return value

    def items(
        self, value: Any, at: str, read: Callable[[Any, str], Any], *, unique: bool
    ) -> list[Any] | None:
        """Each member read with ``read``; ``unique`` refuses repeats (sets have no order)."""
        array = self.array(value, at)
        if array is None:
            return None
        out, ok, seen = [], True, set()
        for index, member in enumerate(array):
            item = read(member, f"{at}/{index}")
            if item is None:
                ok = False
                continue
            if unique:
                if item in seen:
                    self.fail(f"{at}/{index}", "repeats an earlier member", FindingCode.DUPLICATE)
                    ok = False
                seen.add(item)
            out.append(item)
        return out if ok else None

    def enum(self, value: Any, at: str, choices: Iterable[str]) -> str | None:
        allowed = sorted(choices)
        if not isinstance(value, str) or value not in allowed:
            self.fail(at, f"expected one of {', '.join(allowed)}")
            return None
        return value

    # Clauses.

    def clock(self, value: Any, at: str) -> Clock | None:
        if isinstance(value, dict) and value.get("kind") == "domain":
            data = self.obj(value, at, ("domain_id", "kind"))
            if data is None:
                return None
            domain = self.string(data["domain_id"], f"{at}/domain_id")
            return None if domain is None else DomainClock(domain)
        if isinstance(value, dict) and value.get("kind") == "civil":
            data = self.obj(value, at, ("epoch", "kind", "resolution", "timescale"))
            if data is None:
                return None
            timescale = self.string(data["timescale"], f"{at}/timescale")
            epoch = self.string(data["epoch"], f"{at}/epoch")
            resolution = self.fraction(data["resolution"], f"{at}/resolution")
            if timescale is None or epoch is None or resolution is None:
                return None
            return CivilTime(timescale, epoch, resolution)
        self.fail(at, "expected a clock: kind 'domain' or 'civil'")
        return None

    def fraction(self, value: Any, at: str) -> Fraction | None:
        data = self.obj(value, at, ("denominator", "numerator"))
        if data is None:
            return None
        numerator = self.integer(data["numerator"], f"{at}/numerator")
        denominator = self.integer(data["denominator"], f"{at}/denominator")
        if numerator is None or denominator is None:
            return None
        if numerator <= 0 or denominator <= 0:
            self.fail(at, "a resolution is a positive fraction", FindingCode.BAD_CLOCK)
            return None
        resolution = Fraction(numerator, denominator)
        if (resolution.numerator, resolution.denominator) != (numerator, denominator):
            self.fail(at, "a resolution is a fraction in lowest terms", FindingCode.BAD_CLOCK)
            return None
        return resolution

    def point(self, value: Any, at: str) -> int | Instant | None:
        if isinstance(value, dict):
            data = self.obj(value, at, ("clock", "ticks"))
            if data is None:
                return None
            clock = self.clock(data["clock"], f"{at}/clock")
            ticks = self.integer(data["ticks"], f"{at}/ticks")
            return None if clock is None or ticks is None else Instant(clock, ticks)
        return self.integer(value, at)

    def during(self, value: Any, at: str) -> During | None:
        data = self.obj(value, at, ("clock", "end", "start"))
        if data is None:
            return None
        clock = self.clock(data["clock"], f"{at}/clock")
        start = self.integer(data["start"], f"{at}/start")
        end: int | None = None
        if data["end"] != OPEN:
            end = self.integer(data["end"], f"{at}/end")
            if end is None:
                return None
        return None if clock is None or start is None else During(clock, start, end)

    def clock_bridge(self, value: Any, at: str) -> ClockBridge | None:
        data = self.obj(value, at, ("mapping_id", "source", "target"))
        if data is None:
            return None
        mapping = self.string(data["mapping_id"], f"{at}/mapping_id")
        source = self.clock(data["source"], f"{at}/source")
        target = self.clock(data["target"], f"{at}/target")
        if mapping is None or source is None or target is None:
            return None
        return ClockBridge(mapping, source, target)

    def frame(self, value: Any, at: str) -> FrameRef | None:
        data = self.obj(value, at, ("frame_id", "graph_id"))
        if data is None:
            return None
        frame = self.string(data["frame_id"], f"{at}/frame_id")
        graph = self.string(data["graph_id"], f"{at}/graph_id")
        return None if frame is None or graph is None else FrameRef(frame, graph)

    def vec(self, value: Any, at: str) -> Vec3 | None:
        array = self.array(value, at)
        if array is None:
            return None
        if len(array) != 3:
            self.fail(at, "expected three coordinates")
            return None
        x, y, z = (self.number(v, f"{at}/{i}") for i, v in enumerate(array))
        return None if x is None or y is None or z is None else (x, y, z)

    def shape(self, value: Any, at: str) -> Shape | None:
        if isinstance(value, dict) and value.get("kind") == "box":
            data = self.obj(value, at, ("kind", "max", "min"))
            if data is None:
                return None
            low, high = self.vec(data["min"], f"{at}/min"), self.vec(data["max"], f"{at}/max")
            return None if low is None or high is None else Box(low, high)
        if isinstance(value, dict) and value.get("kind") == "sphere":
            data = self.obj(value, at, ("center", "kind", "radius"))
            if data is None:
                return None
            center = self.vec(data["center"], f"{at}/center")
            radius = self.number(data["radius"], f"{at}/radius")
            return None if center is None or radius is None else Sphere(center, radius)
        self.fail(at, "expected a shape: kind 'box' or 'sphere'")
        return None

    def region(self, value: Any, at: str) -> FrameRegion | None:
        data = self.obj(value, at, ("frame", "shape", "unit"))
        if data is None:
            return None
        frame = self.frame(data["frame"], f"{at}/frame")
        unit = self.string(data["unit"], f"{at}/unit")
        shape = self.shape(data["shape"], f"{at}/shape")
        if frame is None or unit is None or shape is None:
            return None
        return FrameRegion(frame, unit, shape)

    def frame_bridge(self, value: Any, at: str) -> FrameBridge | None:
        data = self.obj(value, at, ("child", "parent", "transform_id"))
        if data is None:
            return None
        transform = self.string(data["transform_id"], f"{at}/transform_id")
        parent = self.frame(data["parent"], f"{at}/parent")
        child = self.frame(data["child"], f"{at}/child")
        if transform is None or parent is None or child is None:
            return None
        return FrameBridge(transform, parent, child)

    def site(self, value: Any, at: str) -> SiteScope | None:
        data = self.obj(value, at, ("site", "zones"))
        if data is None:
            return None
        site = self.string(data["site"], f"{at}/site")
        zones = self.items(data["zones"], f"{at}/zones", self.string, unique=True)
        return None if site is None or zones is None else SiteScope(site, frozenset(zones))

    def subject(self, value: Any, at: str) -> Subject | None:
        data = self.obj(value, at, ("kind", "same_as_depth"), ("declared_id",))
        if data is None:
            return None
        kind = self.string(data["kind"], f"{at}/kind")
        depth = self.integer(data["same_as_depth"], f"{at}/same_as_depth")
        declared: str | None = None
        if "declared_id" in data:
            declared = self.string(data["declared_id"], f"{at}/declared_id")
            if declared is None:
                return None
        return None if kind is None or depth is None else Subject(kind, declared, depth)

    def graph(self, value: Any, at: str) -> GraphClause | None:
        data = self.obj(value, at, ("direction", "hops", "predicates"))
        if data is None:
            return None
        direction = self.enum(data["direction"], f"{at}/direction", (str(d) for d in Direction))
        hops = self.integer(data["hops"], f"{at}/hops")
        predicates: frozenset[str] | None = None
        if data["predicates"] != ANY:
            listed = self.items(data["predicates"], f"{at}/predicates", self.string, unique=True)
            if listed is None:
                return None
            predicates = frozenset(listed)
        if direction is None or hops is None:
            return None
        return GraphClause(predicates, hops, Direction(direction))

    def text(self, value: Any, at: str) -> TextClause | None:
        data = self.obj(value, at, ("channels", "fields", "text"))
        if data is None:
            return None
        text = self.string(data["text"], f"{at}/text")
        fields = self.items(
            data["fields"],
            f"{at}/fields",
            lambda v, p: self.enum(v, p, (str(f) for f in TextField)),
            unique=True,
        )
        channels = self.items(
            data["channels"],
            f"{at}/channels",
            lambda v, p: self.enum(v, p, (str(c) for c in TextChannel)),
            unique=True,
        )
        if text is None or fields is None or channels is None:
            return None
        return TextClause(
            text,
            frozenset(TextField(f) for f in fields),
            frozenset(TextChannel(c) for c in channels),
        )

    def budget(self, value: Any, at: str) -> Budget | None:
        data = self.obj(value, at, ("items",), ("bytes", "latency_ms", "tokens"))
        if data is None:
            return None
        items = self.integer(data["items"], f"{at}/items")
        limits: dict[str, int | None] = {}
        ok = items is not None
        for name in ("tokens", "bytes", "latency_ms"):
            limits[name] = None
            if name in data:
                limits[name] = self.integer(data[name], f"{at}/{name}")
                ok = ok and limits[name] is not None
        if not ok or items is None:
            return None
        return Budget(items, limits["tokens"], limits["bytes"], limits["latency_ms"])

    def explain(self, value: Any, at: str) -> Explain | None:
        if isinstance(value, dict) and value.get("kind") == "why":
            data = self.obj(value, at, ("claim_id", "kind"))
            if data is None:
                return None
            claim = self.string(data["claim_id"], f"{at}/claim_id")
            return None if claim is None else Why(claim)
        if isinstance(value, dict) and value.get("kind") == "diff":
            data = self.obj(value, at, ("after", "before", "kind", "subject"))
            if data is None:
                return None
            subject = self.subject(data["subject"], f"{at}/subject")
            before = self.point(data["before"], f"{at}/before")
            after = self.point(data["after"], f"{at}/after")
            if subject is None or before is None or after is None:
                return None
            return Diff(subject, before, after)
        self.fail(at, "expected an explain item: kind 'why' or 'diff'")
        return None

    def query(self, value: Any) -> Query | None:
        data = self.obj(
            value,
            "",
            (
                "as_of",
                "budget",
                "clock_bridges",
                "explain",
                "frame_bridges",
                "include_inferred",
                "query_version",
                "regions",
                "subjects",
            ),
            ("during", "graph", "site", "text"),
        )
        if data is None:
            return None
        version = data["query_version"]
        if isinstance(version, bool) or not isinstance(version, int) or version != QUERY_VERSION:
            self.fail(
                "/query_version",
                f"this reader reads query_version {QUERY_VERSION}",
                FindingCode.UNSUPPORTED_VERSION,
            )
            return None
        as_of: AsOf | None = HEAD
        if data["as_of"] != HEAD:
            as_of = self.integer(data["as_of"], "/as_of")
        include_inferred = self.boolean(data["include_inferred"], "/include_inferred")
        budget = self.budget(data["budget"], "/budget")
        subjects = self.items(data["subjects"], "/subjects", self.subject, unique=True)
        clock_bridges = self.items(
            data["clock_bridges"], "/clock_bridges", self.clock_bridge, unique=True
        )
        regions = self.items(data["regions"], "/regions", self.region, unique=True)
        frame_bridges = self.items(
            data["frame_bridges"], "/frame_bridges", self.frame_bridge, unique=True
        )
        explain = self.items(data["explain"], "/explain", self.explain, unique=False)
        during = self.during(data["during"], "/during") if "during" in data else None
        site = self.site(data["site"], "/site") if "site" in data else None
        graph = self.graph(data["graph"], "/graph") if "graph" in data else None
        text = self.text(data["text"], "/text") if "text" in data else None
        if (
            self.findings
            or as_of is None
            or include_inferred is None
            or budget is None
            or subjects is None
            or clock_bridges is None
            or regions is None
            or frame_bridges is None
            or explain is None
        ):
            return None
        return Query(
            include_inferred=include_inferred,
            budget=budget,
            subjects=frozenset(subjects),
            as_of=as_of,
            during=during,
            clock_bridges=frozenset(clock_bridges),
            regions=frozenset(regions),
            frame_bridges=frozenset(frame_bridges),
            site=site,
            graph=graph,
            text=text,
            explain=tuple(explain),
        )


def accept(query: Query) -> Query | Refused:
    """``query`` if ``validate`` finds nothing, else every finding."""
    findings = validate(query)
    return Refused(findings) if findings else query


def from_json(value: JsonValue) -> Query | Refused:
    """Read a query's JSON value (already parsed): shape first, then ``validate``."""
    decoder = _Decoder()
    query = decoder.query(value)
    if query is None:
        return Refused(tuple(decoder.findings))
    return accept(query)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate member {key!r}")
        out[key] = value
    return out


def _no_constants(token: str) -> Any:
    raise ValueError(f"{token} is not a JSON number")


def loads(document: bytes | str) -> Query | Refused:
    """Read a query from JSON text: bounded size, no duplicate keys, no NaN or Infinity."""
    if isinstance(document, str):
        if not _is_unicode(document):
            return Refused((QueryFinding(FindingCode.SYNTAX, "/", "not UTF-8 JSON"),))
        data = document.encode("utf-8")
    else:
        data = document
    if len(data) > MAX_DOCUMENT_BYTES:
        return Refused(
            (
                QueryFinding(
                    FindingCode.TOO_LARGE, "/", f"a query is at most {MAX_DOCUMENT_BYTES} bytes"
                ),
            )
        )
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_no_constants,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        message = "not UTF-8 JSON" if isinstance(error, UnicodeDecodeError) else "not JSON"
        return Refused((QueryFinding(FindingCode.SYNTAX, "/", message),))
    return from_json(value)

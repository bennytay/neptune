"""STL, binary and ASCII: facets counted and bounded; no unit, no up axis, no name in binary.

Binary STL is an 80-byte header, a little-endian ``uint32`` facet count and 50 bytes per facet
(normal, three vertices, attribute). The declared count is a property of its own
(``declared_facet_count``, stated); the measured one (``facet_count``) is what the file's size
holds, so a lying count or a truncated body is visible beside it and reported
(``geometry.count_mismatch``, ``geometry.truncated``). The 80 header bytes are free text, not a
name, so the name is Unknown. ASCII STL names its solid. STL has no unit: it stays Unknown.
"""

import struct
from typing import Final

from neptune.adapters.geometry._context import Bounds, Context, Problems, floats, text_of
from neptune.adapters.geometry._emit import Geometry, known, missing
from neptune.adapters.geometry._scan import LimitHit, Unreadable
from neptune.model.knowledge import AssertionKind, Known, Unknown
from neptune.model.world import SpatialCategory

HEADER: Final = 84
FACET: Final = struct.Struct("<12fH")
FACETS_PER_BLOCK: Final = 16384
MAX_NAME: Final = 4096
OBSERVED: Final = AssertionKind.OBSERVED
STATED: Final = AssertionKind.STATED


def read_binary(ctx: Context) -> Geometry:
    out, whole = ctx.out, ctx.whole
    if ctx.size < HEADER:
        raise Unreadable("a binary STL has an 84-byte header", 0, ctx.size)
    (declared,) = struct.unpack("<I", ctx.scan.read(80, 4))
    fit = (ctx.size - HEADER) // FACET.size
    present = min(declared, fit)
    count_at = ctx.span(80, 4)
    body = ctx.span(HEADER, present * FACET.size)
    if declared > fit:
        ctx.truncated(
            ctx.span(HEADER, ctx.size - HEADER),
            f"its facets: {declared} are declared and the file holds {fit}",
            {"declared": declared, "present": fit},
        )
    elif ctx.size - HEADER > declared * FACET.size:
        ctx.mismatch(
            count_at, "bytes after the declared facets", declared * FACET.size, ctx.size - HEADER
        )
    bounds, complete = Bounds(), True
    try:
        if present * 3 > ctx.max_vertices:
            raise LimitHit("max_vertices", ctx.max_vertices)
        block = FACET.size * FACETS_PER_BLOCK
        at = HEADER
        for data in ctx.scan.blocks(HEADER, HEADER + present * FACET.size, block):
            for index, facet in enumerate(FACET.iter_unpack(data)):
                bounds.add(facet[3], facet[4], facet[5], at + index * FACET.size)
                bounds.add(facet[6], facet[7], facet[8], at + index * FACET.size)
                bounds.add(facet[9], facet[10], facet[11], at + index * FACET.size)
            at += len(data)
    except LimitHit as hit:
        complete = False
        ctx.limit(hit, body)
    props = [
        known("encoding", ("binary",), whole, OBSERVED),
        known("declared_facet_count", (declared,), count_at, STATED),
        known("facet_count", (present,), body, OBSERVED),
        *bounds.props(ctx, body, complete=complete),
        missing("up_axis", "not_covered", whole, OBSERVED),
    ]
    return Geometry(
        "stl",
        SpatialCategory.MESH,
        Unknown(out.provenance(ctx.span(0, 80))),
        Unknown(out.provenance(whole)),
        tuple(props),
    )


def read_ascii(ctx: Context) -> Geometry:
    out, whole = ctx.out, ctx.whole
    bounds, problems = Bounds(), Problems()
    solids = ended = facets = vertices = 0
    name: Known[str] | Unknown = Unknown(out.provenance(whole))
    complete = True
    try:
        for line in ctx.scan.lines(0, ctx.size):
            if line.overlong:
                problems.add("lines longer than 65536 bytes were skipped", line.offset)
                continue
            tokens = line.data.split()
            if not tokens:
                continue
            keyword = tokens[0].lower()
            if keyword == b"solid":
                solids += 1
                if solids == 1:
                    name = _name(ctx, line.offset, line.data, problems)
            elif keyword == b"facet":
                facets += 1
            elif keyword == b"vertex":
                vertices += 1
                if vertices > ctx.max_vertices:
                    raise LimitHit("max_vertices", ctx.max_vertices)
                numbers = floats(tokens[1:4]) if len(tokens) == 4 else None
                if numbers is None:
                    problems.add("vertices without exactly three numbers were skipped", line.offset)
                else:
                    bounds.add(numbers[0], numbers[1], numbers[2], line.offset)
            elif keyword == b"endsolid":
                ended += 1
            elif keyword not in (b"outer", b"endloop", b"endfacet"):
                problems.add("lines that are not STL keywords", line.offset)
    except LimitHit as hit:
        complete = False
        ctx.limit(hit)
    problems.report(ctx)
    if complete:
        if vertices != 3 * facets:
            ctx.mismatch(whole, "vertices (three per facet)", 3 * facets, vertices)
        if ended < solids:
            ctx.truncated(ctx.span(max(ctx.size - 1, 0), 1), "a solid with no endsolid")
    props = [
        known("encoding", ("ascii",), whole, OBSERVED),
        known("facet_count", (facets,), whole, OBSERVED) if complete
        else missing("facet_count", "not_covered", whole, OBSERVED),
        *bounds.props(ctx, whole, complete=complete),
        missing("up_axis", "not_covered", whole, OBSERVED),
    ]  # fmt: skip
    return Geometry("stl", SpatialCategory.MESH, name, Unknown(out.provenance(whole)), tuple(props))


def _name(ctx: Context, offset: int, data: bytes, problems: Problems) -> Known[str] | Unknown:
    raw = data.strip()[len(b"solid") :].strip()
    if not raw:
        return Unknown(ctx.out.provenance(ctx.span(offset, len(data))))
    where = ctx.span(
        offset + data.lower().index(raw.lower(), data.lower().index(b"solid") + 5), len(raw)
    )
    text = text_of(raw[:MAX_NAME])
    prov = ctx.out.provenance(where, STATED)
    if text is None:
        problems.add("solid names that are not text", offset)
        return Unknown(prov)
    return Known(text, prov)

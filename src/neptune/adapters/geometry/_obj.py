"""Wavefront OBJ: vertices counted and bounded, objects and material libraries named.

OBJ is text with no header, no unit and no up axis. One pass counts ``v`` (vertices), ``f``
(faces), ``vt``, ``vn`` and ``o`` (objects), takes the bounds of every ``v`` that is finite
(observed), and reads each ``mtllib`` as a dependency citing its exact bytes. A face index of 0 or
past the vertices read so far, a vertex with fewer than three numbers and an unknown statement are
counted and reported once each. Free-form curve and surface statements are not decoded
(``geometry.not_covered``). The material library's name is the rest of its line, one reference
(the OBJ specification allows several names; exporters write one, spaces included).
"""

from typing import Final

from neptune.adapters.geometry._context import (
    Bounds,
    Context,
    Problems,
    bytes_text,
    floats,
    measured_count,
    operand,
)
from neptune.adapters.geometry._detect import OBJ_KEYWORDS
from neptune.adapters.geometry._emit import (
    NOT_COVERED,
    OBSERVED,
    REFERENCE_UNSAFE,
    STATED,
    Dep,
    Geometry,
    missing,
)
from neptune.adapters.geometry._scan import LimitHit, parse_index
from neptune.model.knowledge import Known, Unknown
from neptune.model.world import SpatialCategory

FREE_FORM: Final = frozenset(
    {
        b"bevel", b"bmat", b"c_interp", b"con", b"cstype", b"ctech", b"curv", b"curv2", b"d_interp",
        b"deg", b"end", b"hole", b"lod", b"mg", b"parm", b"scrv", b"sp", b"step", b"stech", b"surf",
        b"trace_obj", b"trim", b"shadow_obj",
    }
)  # fmt: skip


def read(ctx: Context) -> Geometry:
    out, whole = ctx.out, ctx.whole
    observed = OBSERVED
    bounds, problems = Bounds(), Problems()
    vertices = faces = objects = 0
    name: Known[str] | Unknown = Unknown(out.provenance(whole))
    deps: list[Dep] = []
    free_form: dict[bytes, int] = {}
    first_free: int | None = None
    complete = True
    try:
        for line in ctx.scan.lines(0, ctx.size):
            if line.overlong:
                problems.add("lines longer than 65536 bytes were skipped", line.offset)
                continue
            data = line.data.split(b"#", 1)[0]
            tokens = data.split()
            if not tokens:
                continue
            keyword = tokens[0]
            if keyword == b"v":
                vertices += 1
                if vertices > ctx.max_vertices:
                    raise LimitHit("max_vertices", ctx.max_vertices)
                numbers = floats(tokens[1:4]) if len(tokens) >= 4 else None
                if numbers is None:
                    problems.add(
                        "vertices whose x, y and z are not three numbers were skipped", line.offset
                    )
                else:
                    bounds.add(numbers[0], numbers[1], numbers[2], line.offset)
            elif keyword == b"f":
                faces += 1
                for corner in tokens[1:]:
                    index = parse_index(corner.split(b"/", 1)[0])
                    if index is None:
                        problems.add("face corners that are not an index", line.offset)
                        break
                    if index == 0 or abs(index) > vertices:
                        problems.add("face corners naming no vertex read so far", line.offset)
                        break
            elif keyword == b"o":
                objects += 1
                if objects == 1:
                    name = _name(ctx, line.offset, data, problems)
            elif keyword == b"mtllib":
                if len(deps) >= ctx.max_entries:
                    raise LimitHit("max_entries", ctx.max_entries)
                _library(ctx, line.offset, data, deps)
            elif keyword in FREE_FORM:
                free_form[keyword] = free_form.get(keyword, 0) + 1
                if first_free is None:
                    first_free = line.offset
            elif keyword not in OBJ_KEYWORDS:
                problems.add("statements that are not OBJ keywords", line.offset)
    except LimitHit as hit:
        complete = False
        ctx.limit(hit)
    problems.report(ctx)
    if free_form and first_free is not None:
        out.finding(
            NOT_COVERED,
            ctx.span(first_free, 1),
            "free-form curve and surface statements are not decoded",
            {"statements": {k.decode(): v for k, v in sorted(free_form.items())}},
        )
    props = [
        measured_count("vertex_count", vertices, whole, complete=complete, kind=observed),
        measured_count("face_count", faces, whole, complete=complete, kind=observed),
        measured_count("object_count", objects, whole, complete=complete, kind=observed),
        *bounds.props(ctx, whole, complete=complete),
        missing("up_axis", "not_covered", whole, observed),
    ]
    category = (
        SpatialCategory.POINT_CLOUD if complete and vertices and not faces else SpatialCategory.MESH
    )
    return Geometry(
        "obj", category, name, Unknown(out.provenance(whole)), tuple(props), tuple(deps)
    )


def _name(ctx: Context, offset: int, data: bytes, problems: Problems) -> Known[str] | Unknown:
    raw, start = operand(data, len(b"o"))
    if not raw:
        return Unknown(ctx.out.provenance(ctx.span(offset, len(data))))
    prov = ctx.out.provenance(ctx.span(offset + start, len(raw)), STATED)
    text = bytes_text(raw, ctx.max_value_bytes)
    if text is None:
        problems.add("object names that are not usable text", offset + start)
        return Unknown(prov)
    return Known(text, prov)


def _library(ctx: Context, offset: int, data: bytes, deps: list[Dep]) -> None:
    raw, start = operand(data, len(b"mtllib"))
    where = ctx.span(offset + start, len(raw))
    try:
        target = raw.decode("utf-8")
    except UnicodeDecodeError:
        target = ""
    if not target.strip():
        ctx.out.finding(
            REFERENCE_UNSAFE,
            ctx.span(offset, max(len(data), 1)),
            "a material library reference is empty or not text",
            {"kind": "material_library"},
        )
        return
    deps.append(Dep("material_library", target, where))

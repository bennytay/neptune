"""PLY: the header read as declared, the vertices read as stored, header lies reported.

A PLY header declares its encoding, each element's name and count and each property's type. The
declared counts are stated (``declared_vertex_count``, ``declared_face_count``); the vertex data is
then read and the bounds are of the vertices actually there (observed). A header that declares
more rows than the file holds (``geometry.truncated``), or fewer than it holds
(``geometry.count_mismatch``), is reported with both numbers kept. Variable-size elements before
the vertices make the vertex offset unknowable in binary: the bounds are then NotCovered. PLY has
no unit, up axis or name; a ``comment TextureFile`` line (the Blender and MeshLab convention) is a
texture dependency. Faces are counted from the header (and, in ASCII, by their lines), never read.
"""

import re
import struct
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.geometry._context import Bounds, Context, Problems, floats
from neptune.adapters.geometry._emit import NOT_COVERED, Dep, Geometry, known, missing
from neptune.adapters.geometry._scan import LimitHit, Unreadable
from neptune.model.knowledge import AssertionKind, Unknown
from neptune.model.world import SpatialCategory

OBSERVED: Final = AssertionKind.OBSERVED
STATED: Final = AssertionKind.STATED
SCALARS: Final = {
    "char": "b", "int8": "b", "uchar": "B", "uint8": "B", "short": "h", "int16": "h",
    "ushort": "H", "uint16": "H", "int": "i", "int32": "i", "uint": "I", "uint32": "I",
    "float": "f", "float32": "f", "double": "d", "float64": "d",
}  # fmt: skip
FORMATS: Final = {"ascii": "", "binary_little_endian": "<", "binary_big_endian": ">"}
END_HEADER: Final = re.compile(rb"(?:^|\n)[ \t]*end_header[ \t]*\r?\n")
BLOCK_ROWS: Final = 16384


@dataclass
class Element:
    name: str
    count: int
    offset: int  # of its header line
    length: int
    properties: list[tuple[str, str, str | None]] = field(
        default_factory=list
    )  # name, type, list count type

    @property
    def has_list(self) -> bool:
        return any(count is not None for _, _, count in self.properties)

    def stride(self) -> int | None:
        """Bytes per row if every property is a fixed-size scalar, else ``None``."""
        if self.has_list or not self.properties:
            return None
        return sum(struct.calcsize(SCALARS[kind]) for _, kind, _ in self.properties)

    def least(self) -> int:
        """The fewest bytes one row can take: lists as empty lists."""
        return sum(
            struct.calcsize(SCALARS[count if count is not None else kind])
            for _, kind, count in self.properties
        )


def read(ctx: Context) -> Geometry:
    out, whole = ctx.out, ctx.whole
    head = ctx.scan.read(0, min(ctx.size, ctx.max_header_bytes))
    end = END_HEADER.search(head)
    if end is None:
        if ctx.size > ctx.max_header_bytes:
            raise LimitHit("max_header_bytes", ctx.max_header_bytes)
        raise Unreadable("the header has no end_header line", 0, ctx.size)
    data_start = end.end()
    problems = Problems()
    form, version, elements, deps = _header(ctx, head[:data_start], problems)
    problems.report(ctx)
    props = []
    if form is None:
        raise Unreadable("the header declares no format line", 0, data_start)
    props.append(known("format_version", (version,), ctx.span(0, data_start), STATED))
    props.append(known("encoding", (form,), ctx.span(0, data_start), STATED))
    by_name = {element.name: element for element in elements}
    vertex, face = by_name.get("vertex"), by_name.get("face")
    for element, prefix in ((vertex, "vertex"), (face, "face")):
        if element is not None:
            where = ctx.span(element.offset, element.length)
            props.append(known(f"declared_{prefix}_count", (element.count,), where, STATED))
    measured = _measure(ctx, form, data_start, elements, vertex, face)
    props.extend(measured)
    props.append(missing("up_axis", "not_covered", whole, OBSERVED))
    category = (
        SpatialCategory.MESH if face is not None and face.count > 0 else SpatialCategory.POINT_CLOUD
    )
    return Geometry(
        "ply", category, Unknown(out.provenance(whole)), Unknown(out.provenance(whole)),
        tuple(props), tuple(deps),
    )  # fmt: skip


def _header(
    ctx: Context, header: bytes, problems: Problems
) -> tuple[str | None, str, list[Element], list[Dep]]:
    form: str | None = None
    version = ""
    elements: list[Element] = []
    deps: list[Dep] = []
    offset = 0
    for index, raw in enumerate(header.split(b"\n")):
        at, offset = offset, offset + len(raw) + 1
        line = raw.rstrip(b"\r")
        words = line.split()
        if index == 0:
            if words != [b"ply"]:
                raise Unreadable("a PLY file starts with a line holding only ply", 0, len(raw))
            continue
        if not words:
            continue
        keyword = words[0]
        if keyword == b"format" and len(words) == 3 and form is None:
            kind = words[1].decode("latin-1")
            if kind in FORMATS and words[2] == b"1.0":
                form, version = kind, "1.0"
            else:
                problems.add("format lines that are not ascii or binary 1.0", at)
        elif keyword == b"element" and len(words) == 3 and words[2].isdigit():
            if len(elements) >= ctx.max_entries:
                raise LimitHit("max_entries", ctx.max_entries)
            name = words[1].decode("latin-1")
            elements.append(Element(name, int(words[2]), at, len(line)))
        elif keyword == b"property" and elements and _property(elements[-1], words):
            continue
        elif keyword == b"end_header":
            continue
        elif keyword == b"comment":
            _comment(ctx, line, at, deps)
        elif keyword in (b"obj_info", b"format", b"element", b"property"):
            problems.add("header lines that do not parse", at)
        else:
            problems.add("header lines that are not PLY keywords", at)
    return form, version, elements, deps


def _property(element: Element, words: list[bytes]) -> bool:
    names = [word.decode("latin-1") for word in words]
    if len(names) == 3 and names[1] in SCALARS:
        element.properties.append((names[2], names[1], None))
        return True
    if len(names) == 5 and names[1] == "list" and names[2] in SCALARS and names[3] in SCALARS:
        element.properties.append((names[4], names[3], names[2]))
        return True
    return False


def _comment(ctx: Context, line: bytes, at: int, deps: list[Dep]) -> None:
    words = line.split(None, 2)
    if len(words) == 3 and words[1] == b"TextureFile":
        target = words[2].strip()
        start = at + line.index(target, len(words[0]) + len(words[1]))
        try:
            deps.append(Dep("texture", target.decode("utf-8"), ctx.span(start, len(target))))
        except UnicodeDecodeError:
            ctx.out.finding(
                "geometry.reference_unsafe", ctx.span(at, len(line)),
                "a texture reference is not text", {"kind": "texture"},
            )  # fmt: skip


def _measure(
    ctx: Context,
    form: str,
    start: int,
    elements: list[Element],
    vertex: Element | None,
    face: Element | None,
) -> list:  # type: ignore[type-arg]
    """The properties measured from the data section: vertex count, faces, bounds."""
    data = ctx.span(start, ctx.size - start)
    if vertex is None:
        return [
            missing("vertex_count", "not_applicable", data, OBSERVED),
            *Bounds().props(ctx, data, complete=True),
        ]
    # A file that cannot hold its declared rows is truncated, whatever else it says.
    least = start + sum(element.count * element.least() for element in elements)
    if form != "ascii" and ctx.size < least:
        ctx.truncated(
            data,
            f"its declared elements: they need at least {least - start} bytes of data",
            {"declared_minimum": least - start, "present": ctx.size - start},
        )
    if form == "ascii":
        return _ascii(ctx, start, elements, vertex)
    return _binary(ctx, form, start, elements, vertex, face)


def _positions(vertex: Element) -> tuple[int, int, int] | None:
    names = [name for name, _, _ in vertex.properties]
    try:
        return names.index("x"), names.index("y"), names.index("z")
    except ValueError:
        return None


def _binary(
    ctx: Context,
    form: str,
    start: int,
    elements: list[Element],
    vertex: Element,
    face: Element | None,
) -> list:  # type: ignore[type-arg]
    before = 0
    for element in elements:
        if element is vertex:
            break
        stride = element.stride()
        if stride is None:
            before = -1
            break
        before += element.count * stride
    columns = _positions(vertex)
    stride = vertex.stride()
    unknown = before < 0 or columns is None or stride is None
    first = start + before
    props: list = []  # type: ignore[type-arg]
    if face is not None:
        props.append(
            missing("face_count", "not_covered", ctx.span(start, ctx.size - start), OBSERVED)
        )
    if unknown or stride is None or columns is None:
        ctx.out.finding(
            NOT_COVERED,
            ctx.span(vertex.offset, vertex.length),
            "the vertex data is not located: a variable-size element precedes it, or it has no"
            " x, y and z properties; the vertex count and bounds are NotCovered",
        )
        where = ctx.span(start, ctx.size - start)
        return [
            missing("vertex_count", "not_covered", where, OBSERVED),
            *Bounds().props(ctx, where, complete=False),
            *props,
        ]
    room = max(ctx.size - first, 0)
    present = min(vertex.count, room // stride)
    where = ctx.span(first, present * stride)
    if vertex.count > room // stride:
        ctx.mismatch(ctx.span(vertex.offset, vertex.length), "vertex rows", vertex.count, present)
    bounds, complete = Bounds(), True
    try:
        if present > ctx.max_vertices:
            raise LimitHit("max_vertices", ctx.max_vertices)
        row = struct.Struct(
            FORMATS[form] + "".join(SCALARS[kind] for _, kind, _ in vertex.properties)
        )
        ix, iy, iz = columns
        at = first
        for block in ctx.scan.blocks(first, first + present * stride, stride * BLOCK_ROWS):
            for index, values in enumerate(row.iter_unpack(block)):
                bounds.add(
                    float(values[ix]), float(values[iy]), float(values[iz]), at + index * stride
                )
            at += len(block)
    except LimitHit as hit:
        complete = False
        ctx.limit(hit, where)
    return [
        known("vertex_count", (present,), where, OBSERVED) if complete
        else missing("vertex_count", "not_covered", where, OBSERVED),
        *bounds.props(ctx, where, complete=complete),
        *props,
    ]  # fmt: skip


def _ascii(ctx: Context, start: int, elements: list[Element], vertex: Element) -> list:  # type: ignore[type-arg]
    columns = _positions(vertex)
    scalars = not vertex.has_list
    bounds, problems = Bounds(), Problems()
    seen = {element.name: 0 for element in elements}
    first = last = start
    complete = True
    index = 0
    try:
        if vertex.count > ctx.max_vertices:
            raise LimitHit("max_vertices", ctx.max_vertices)
        for line in ctx.scan.lines(start, ctx.size):
            if line.overlong:
                problems.add("lines longer than 65536 bytes were skipped", line.offset)
                continue
            tokens = line.data.split()
            if not tokens:
                continue
            while index < len(elements) and seen[elements[index].name] >= elements[index].count:
                index += 1
            if index >= len(elements):
                problems.add("rows after the declared elements", line.offset)
                continue
            element = elements[index]
            seen[element.name] += 1
            if element is vertex:
                if seen[element.name] == 1:
                    first = line.offset
                last = line.offset + len(line.data)
                numbers = None
                if columns is not None and scalars and len(tokens) >= len(vertex.properties):
                    numbers = floats(tokens[column] for column in columns)
                if numbers is None:
                    problems.add("vertex rows whose x, y and z do not parse", line.offset)
                else:
                    bounds.add(numbers[0], numbers[1], numbers[2], line.offset)
    except LimitHit as hit:
        complete = False
        ctx.limit(hit)
    problems.report(ctx)
    where = ctx.span(first, last - first)
    if complete:
        for element in elements:
            if seen[element.name] < element.count:
                ctx.mismatch(
                    ctx.span(element.offset, element.length),
                    f"{element.name} rows",
                    element.count,
                    seen[element.name],
                )
    if columns is None:
        complete = False
    return [
        known("vertex_count", (seen["vertex"],), where, OBSERVED) if complete
        else missing("vertex_count", "not_covered", where, OBSERVED),
        *bounds.props(ctx, where, complete=complete),
        *(
            [known("face_count", (seen["face"],), ctx.span(last, 0), OBSERVED)]
            if complete and "face" in seen
            else []
        ),
    ]  # fmt: skip

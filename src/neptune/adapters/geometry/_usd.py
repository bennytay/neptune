"""USD: the ASCII layer's header block read as declared; a binary crate identified, not decoded.

A ``.usda`` layer starts ``#usda 1.0`` and may open a parenthesised metadata block. That block, and
nothing after it, is read: ``upAxis`` and ``metersPerUnit`` (stated), ``defaultPrim`` (the
artifact's name) and each ``subLayers`` asset (a dependency citing its exact bytes). The block is
tokenised with strings, ``@asset@`` paths and comments recognised, bounded by ``max_header_bytes``
and ``max_json_depth`` nesting. ``metersPerUnit`` is mapped to a unit only for the standard lengths
(1, 0.01, 0.001, 0.0254, 0.3048, 1000, 1e-6); any other scale leaves the unit Unknown with the
number kept. A file with no ``metersPerUnit`` has no declared unit: the USD fallback is not
assumed. Prims, their meshes and their references are not read (``geometry.not_covered``), so the
counts and bounds are NotCovered. A binary ``.usdc`` crate is recognised by its ``PXR-USDC``
signature and version bytes and its content is NotCovered.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.geometry._context import Context, Problems
from neptune.adapters.geometry._emit import (
    NOT_COVERED,
    UNIT_UNMAPPED,
    Dep,
    Geometry,
    Prop,
    known,
    missing,
)
from neptune.adapters.geometry._scan import LimitHit, Unreadable
from neptune.model.knowledge import AssertionKind, Knowledge, Known, NotCovered, Unknown
from neptune.model.provenance import Locator
from neptune.model.units import Unit, unit_from_text
from neptune.model.world import SpatialCategory

OBSERVED: Final = AssertionKind.OBSERVED
STATED: Final = AssertionKind.STATED
CRATE_MAGIC: Final = b"PXR-USDC"
USDA_MAGIC: Final = re.compile(rb"#usda ([0-9]+\.[0-9]+)[ \t]*\r?(?:\n|$)")
STANDARD_LENGTHS: Final = {1.0: "m", 0.01: "cm", 0.001: "mm", 0.0254: "in", 0.3048: "ft",
                           1000.0: "km", 1e-06: "um"}  # fmt: skip
_TOKEN: Final = re.compile(
    rb"""
    (?P<ws>\s+)
  | (?P<comment>\#[^\n]*)
  | (?P<text>\"\"\"(?:[^"\\]|\\.|"(?!""))*\"\"\"|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
  | (?P<asset>@@@.*?@@@|@[^@\n]*@)
  | (?P<number>[-+]?(?:[0-9]+\.?[0-9]*(?:[eE][-+]?[0-9]+)?|\.[0-9]+(?:[eE][-+]?[0-9]+)?))
  | (?P<name>[A-Za-z_][A-Za-z0-9_:.]*)
  | (?P<punct>[()\[\]{},=;])
    """,
    re.VERBOSE | re.DOTALL,
)
Token = tuple[str, bytes, int]  # kind, text, offset
Found = tuple[bytes, int]  # a value's text and where it starts
Where = tuple[Locator, ...]


def read_crate(ctx: Context) -> Geometry:
    out, whole = ctx.out, ctx.whole
    if ctx.size < 16:
        raise Unreadable("a USD crate has a 16-byte signature and version", 0, ctx.size)
    head = ctx.scan.read(0, 16)
    major, minor, patch = head[8], head[9], head[10]
    out.finding(
        NOT_COVERED,
        ctx.span(0, 16),
        "a binary USD crate is identified by its signature and version; its table of contents"
        " and values are not decoded, so name, unit, up axis, counts and bounds are NotCovered",
    )
    props = [
        known("encoding", ("usdc",), whole, OBSERVED),
        known("format_version", (f"{major}.{minor}.{patch}",), ctx.span(8, 3), STATED),
        *(missing(name, "not_covered", whole, OBSERVED) for name in _UNREAD),
    ]
    prov = out.provenance(whole)
    return Geometry("usdc", SpatialCategory.SCENE, NotCovered(prov), NotCovered(prov), tuple(props))


_UNREAD: Final = ("up_axis", "meters_per_unit", "vertex_count", "bounds_min", "bounds_max")


def read_layer(ctx: Context) -> Geometry:
    out, whole = ctx.out, ctx.whole
    head = ctx.scan.read(0, min(ctx.size, ctx.max_header_bytes))
    first = USDA_MAGIC.match(head)
    if first is None:
        raise Unreadable("a usda layer starts #usda 1.0", 0, min(ctx.size, 16))
    problems = Problems()
    layer, end = _metadata(ctx, head, first.end(), problems)
    block = ctx.span(first.end(), end - first.end())
    prov = out.provenance(block, STATED)
    props: list[Prop] = [
        known("encoding", ("usda",), whole, OBSERVED),
        known("format_version", (first.group(1).decode(),), ctx.span(0, first.end()), STATED),
        _declared(ctx, "up_axis", layer.up_axis, block),
    ]
    unit: Knowledge[Unit] = Unknown(prov)
    scale = _number(layer.meters)
    if layer.meters is None:
        props.append(missing("meters_per_unit", "unknown", block, STATED))
    else:
        where = ctx.span(layer.meters[1], len(layer.meters[0]))
        if scale is None:
            problems.add("metersPerUnit values that are not numbers", layer.meters[1])
            props.append(missing("meters_per_unit", "unknown", where, STATED))
        else:
            props.append(known("meters_per_unit", (scale,), where, STATED))
            symbol = STANDARD_LENGTHS.get(scale)
            if symbol is None:
                out.finding(
                    UNIT_UNMAPPED,
                    where,
                    "metersPerUnit is not one of the standard lengths",
                    {"meters_per_unit": scale},
                )
            else:
                unit = unit_from_text(symbol, provenance=out.provenance(where, STATED))
    problems.report(ctx)
    name: Knowledge[str] = Unknown(prov)
    if layer.prim is not None and layer.prim[0]:
        text, at = layer.prim
        name = Known(
            text.decode("utf-8", "replace"), out.provenance(ctx.span(at, len(text)), STATED)
        )
    deps = tuple(
        Dep("sublayer", text.decode("utf-8", "replace"), ctx.span(at, len(text)))
        for text, at in layer.sublayers
    )
    out.finding(
        NOT_COVERED,
        ctx.span(min(end, ctx.size - 1), 1),
        "USD prims, meshes and references after the layer metadata are not read: the vertex"
        " count and bounds are NotCovered",
    )
    props.extend(
        missing(n, "not_covered", whole, OBSERVED)
        for n in ("vertex_count", "bounds_min", "bounds_max")
    )
    return Geometry("usda", SpatialCategory.SCENE, name, unit, tuple(props), deps)


def _number(found: Found | None) -> float | None:
    if found is None:
        return None
    try:
        return float(found[0])
    except ValueError:
        return None


def _declared(ctx: Context, name: str, found: Found | None, block: Where) -> Prop:
    """A text value the layer states, as written, or Unknown when it does not."""
    if found is None:
        return missing(name, "unknown", block, STATED)
    text, at = found
    return known(name, (text.decode("utf-8", "replace"),), ctx.span(at, len(text)), STATED)


def _tokens(data: bytes, start: int) -> Iterator[Token]:
    position = start
    while position < len(data):
        found = _TOKEN.match(data, position)
        if found is None:
            yield "bad", data[position : position + 1], position
            position += 1
            continue
        kind = found.lastgroup or "bad"
        if kind not in ("ws", "comment"):
            yield kind, found.group(), position
        position = found.end()


def _unquote(token: bytes) -> Found:
    """A string or asset token's text without its delimiters, and how far in that text starts."""
    for quote in (b'"""', b"@@@", b'"', b"'", b"@"):
        if token.startswith(quote) and token.endswith(quote) and len(token) >= 2 * len(quote):
            return token[len(quote) : -len(quote)], len(quote)
    return token, 0


@dataclass
class Layer:
    """The layer metadata's values, each as ``(text, offset)``."""

    up_axis: Found | None = None
    meters: Found | None = None
    prim: Found | None = None
    sublayers: list[Found] = field(default_factory=list)


def _metadata(ctx: Context, data: bytes, start: int, problems: Problems) -> tuple[Layer, int]:
    """The layer metadata's values, and the offset where the block ends (``start`` without one)."""
    layer = Layer()
    tokens = _tokens(data, start)
    opening = next(tokens, None)
    if opening is None or opening[:2] != ("punct", b"("):
        return layer, start
    depth, key, end, closed = 1, None, len(data), False
    for kind, text, at in tokens:
        if kind == "bad":
            problems.add("bytes the USD grammar does not allow in the layer metadata", at)
        elif kind == "punct":
            if text in (b"(", b"[", b"{"):
                depth += 1
                if depth > ctx.max_json_depth:
                    raise LimitHit("max_json_depth", ctx.max_json_depth)
            elif text in (b")", b"]", b"}"):
                depth -= 1
                if depth == 0:
                    end, closed = at + 1, True
                    break
        elif depth == 1 and kind == "name":
            key = text.decode("latin-1")
        elif depth == 1 and kind == "text" and key in ("upAxis", "defaultPrim"):
            inner, skip = _unquote(text)
            found = (inner, at + skip)
            if key == "upAxis":
                layer.up_axis = found
            else:
                layer.prim = found
            key = None
        elif depth == 1 and kind == "number" and key == "metersPerUnit":
            layer.meters = (text, at)
            key = None
        elif depth == 2 and kind == "asset" and key == "subLayers":
            inner, skip = _unquote(text)
            layer.sublayers.append((inner, at + skip))
    if not closed:
        if len(data) >= ctx.max_header_bytes and ctx.size > ctx.max_header_bytes:
            raise LimitHit("max_header_bytes", ctx.max_header_bytes)
        ctx.truncated(ctx.span(start, len(data) - start), "its layer metadata block")
    return layer, end

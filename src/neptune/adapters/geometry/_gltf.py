"""glTF 2.0 and GLB: the JSON read, the buffers never opened.

A ``.gltf`` is one JSON document; a GLB wraps it in a 12-byte header and a JSON chunk, then an
optional binary chunk this adapter never reads. Only the JSON is parsed, after its depth is
counted outside strings and its size is checked, so a nesting bomb costs a byte scan.

The glTF specification, not the file, defines the unit (metres) and the up axis (+Y): both are
stated, citing the file's ``asset``. Bounds are the union of the ``min`` and ``max`` that every
``POSITION`` accessor must declare (stated, as written, node transforms not applied), and
``vertex_count`` the sum of those accessors' counts. Counts of nodes, meshes, materials, textures,
images, buffers and animations are array lengths (observed). Every ``buffers[].uri`` and
``images[].uri`` that is not a ``data:`` URI is a dependency; embedded ones are counted.
"""

import json
import math
import re
import struct
from typing import Any, Final

from neptune.adapters.geometry._context import Context, Problems, clean_text
from neptune.adapters.geometry._emit import (
    OBSERVED,
    STATED,
    Dep,
    Geometry,
    Prop,
    known,
    missing,
)
from neptune.adapters.geometry._refs import EMBEDDED, scope_of
from neptune.adapters.geometry._scan import LimitHit, Unreadable
from neptune.model.knowledge import Known, Unknown
from neptune.model.provenance import JsonPointer, Locator
from neptune.model.units import unit_from_text
from neptune.model.world import SpatialCategory

INT64_MAX: Final = 2**63 - 1
GLB_MAGIC: Final = b"glTF"
JSON_CHUNK: Final = 0x4E4F534A
_SPECIAL: Final = re.compile(rb'[\\"\[\]{}]')
COUNTED: Final = (
    ("node_count", "nodes"), ("mesh_count", "meshes"), ("material_count", "materials"),
    ("texture_count", "textures"), ("image_count", "images"), ("buffer_count", "buffers"),
    ("animation_count", "animations"),
)  # fmt: skip


def read(ctx: Context, *, glb: bool) -> Geometry:
    region = _region(ctx, glb)
    offset, length = (20, region) if glb else (0, ctx.size)
    document = _parse(ctx, offset, length)
    prefix: tuple[Locator, ...] = ctx.span(offset, length) if glb else ()
    return _geometry(ctx, document, prefix, glb)


def _region(ctx: Context, glb: bool) -> int:
    """The length of the JSON text: the whole file, or the GLB's first chunk."""
    if not glb:
        if ctx.size > ctx.max_json_bytes:
            raise LimitHit("max_json_bytes", ctx.max_json_bytes)
        return ctx.size
    if ctx.size < 20:
        raise Unreadable("a GLB has a 12-byte header and a chunk header", 0, ctx.size)
    _, version, total, chunk, kind = struct.unpack("<4sIIII", ctx.scan.read(0, 20))
    if version != 2:
        raise Unreadable(f"GLB version {version} is not read; only 2", 4, 4)
    if kind != JSON_CHUNK:
        raise Unreadable("a GLB's first chunk is JSON", 16, 4)
    if total != ctx.size:
        ctx.mismatch(ctx.span(8, 4), "the GLB's total length", total, ctx.size)
    if chunk > ctx.max_json_bytes:
        raise LimitHit("max_json_bytes", ctx.max_json_bytes)
    if 20 + chunk > ctx.size:
        ctx.truncated(
            ctx.span(12, 4), "its JSON chunk", {"declared": chunk, "present": ctx.size - 20}
        )
        raise Unreadable("the JSON chunk is cut short", 12, 4)
    return int(chunk)


def _parse(ctx: Context, offset: int, length: int) -> Any:
    raw = ctx.scan.read(offset, length)
    _check_depth(raw, ctx.max_json_depth)
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise Unreadable(f"the JSON does not parse ({type(exc).__name__})", offset, length) from exc


def _check_depth(raw: bytes, limit: int) -> None:
    """Raise ``LimitHit`` if the JSON nests deeper than ``limit``, counting brackets outside
    strings in one linear pass (no regex over strings: an unterminated one would cost n squared)."""
    depth = 0
    inside = False
    escaped = -1
    for found in _SPECIAL.finditer(raw):
        at, char = found.start(), found.group()
        if at == escaped:
            continue  # the character after a backslash
        if inside:
            if char == b"\\":
                escaped = at + 1
            elif char == b'"':
                inside = False
        elif char == b'"':
            inside = True
        elif char in (b"[", b"{"):
            depth += 1
            if depth > limit:
                raise LimitHit("max_json_depth", limit)
        elif char in (b"]", b"}"):
            depth -= 1


def _reject(constant: str) -> Any:
    raise ValueError(f"{constant} is not JSON")


def _at(prefix: tuple[Locator, ...], *pointer: str | int) -> tuple[Locator, ...]:
    return (*prefix, JsonPointer("".join(f"/{part}" for part in pointer)))


def _list(document: dict[str, Any], key: str) -> list[Any] | None:
    value = document.get(key)
    return value if isinstance(value, list) else None


def _geometry(ctx: Context, document: Any, prefix: tuple[Locator, ...], glb: bool) -> Geometry:
    out, whole = ctx.out, ctx.whole
    if not isinstance(document, dict) or not isinstance(document.get("asset"), dict):
        raise Unreadable("a glTF document is an object with an asset", 0, ctx.size)
    problems = Problems()
    asset = document["asset"]
    asset_at = _at(prefix, "asset")
    props: list[Prop] = [known("encoding", ("glb" if glb else "gltf",), whole, OBSERVED)]
    version = asset.get("version")
    if isinstance(version, str) and version:
        props.append(known("format_version", (version,), _at(prefix, "asset", "version"), STATED))
    else:
        props.append(missing("format_version", "unknown", asset_at, STATED))
        problems.add("assets with no version string", 0, asset_at)
    props.append(known("up_axis", ("Y",), asset_at, STATED))
    for label, key in COUNTED:
        items = _list(document, key)
        if items is not None:
            props.append(known(label, (len(items),), _at(prefix, key), OBSERVED))
    deps, embedded = _dependencies(ctx, document, prefix, problems)
    if embedded:
        props.append(known("embedded_resource_count", (embedded,), _at(prefix), OBSERVED))
    props.extend(_positions(ctx, document, prefix, problems))
    scenes = _list(document, "scenes")
    chosen = document.get("scene")
    scene = None
    if scenes:
        index = chosen if isinstance(chosen, int) and 0 <= chosen < len(scenes) else 0
        scene = (index, scenes[index])
    name: Known[str] | Unknown = Unknown(
        out.provenance(_at(prefix, "scenes") if scenes else asset_at)
    )
    if scene is not None and isinstance(scene[1], dict) and "name" in scene[1]:
        where = _at(prefix, "scenes", scene[0], "name")
        raw = scene[1]["name"]
        text = clean_text(raw, ctx.max_value_bytes) if isinstance(raw, str) else None
        if text is not None:
            name = Known(text, out.provenance(where, STATED))
        else:
            problems.add("scene names that are not usable text", 0, where)
    problems.report(ctx)
    unit = unit_from_text("m", provenance=out.provenance(asset_at, STATED))
    return Geometry("gltf", SpatialCategory.MESH, name, unit, tuple(props), tuple(deps))


def _dependencies(
    ctx: Context, document: dict[str, Any], prefix: tuple[Locator, ...], problems: Problems
) -> tuple[list[Dep], int]:
    deps: list[Dep] = []
    embedded = 0
    for key, kind in (("buffers", "buffer"), ("images", "texture")):
        for index, item in enumerate(_list(document, key) or ()):
            if len(deps) >= ctx.max_entries:
                raise LimitHit("max_entries", ctx.max_entries)
            uri = item.get("uri") if isinstance(item, dict) else None
            if uri is None:
                continue
            if not isinstance(uri, str):
                problems.add("uri values that are not text", 0, _at(prefix, key, index, "uri"))
                continue
            if scope_of(uri, percent_encoded=True) == EMBEDDED:
                embedded += 1
                continue
            deps.append(Dep(kind, uri, _at(prefix, key, index, "uri"), percent_encoded=True))
    return deps, embedded


def _positions(
    ctx: Context, document: dict[str, Any], prefix: tuple[Locator, ...], problems: Problems
) -> list[Prop]:
    """Bounds and vertex count from the POSITION accessors every mesh primitive names."""
    accessors = _list(document, "accessors")
    where = _at(prefix, "accessors")
    wanted: set[int] = set()
    for mesh in _list(document, "meshes") or ():
        for primitive in (mesh.get("primitives") if isinstance(mesh, dict) else None) or ():
            attributes = primitive.get("attributes") if isinstance(primitive, dict) else None
            index = attributes.get("POSITION") if isinstance(attributes, dict) else None
            if isinstance(index, int) and not isinstance(index, bool):
                wanted.add(index)
            if len(wanted) > ctx.max_entries:
                raise LimitHit("max_entries", ctx.max_entries)
    if not wanted:
        return [
            missing("vertex_count", "not_applicable", where, STATED),
            missing("bounds_min", "not_applicable", where, STATED),
            missing("bounds_max", "not_applicable", where, STATED),
        ]
    low: list[float] = []
    high: list[float] = []
    count = 0
    ranged = True
    for index in sorted(wanted):
        accessor = (
            accessors[index] if accessors is not None and 0 <= index < len(accessors) else None
        )
        if not isinstance(accessor, dict):
            problems.add("POSITION accessors that do not exist", 0, where)
            ranged = False
            continue
        total = accessor.get("count")
        here = _at(prefix, "accessors", index)
        if isinstance(total, int) and not isinstance(total, bool) and 0 <= total <= INT64_MAX:
            count += total
        else:
            problems.add("POSITION accessors without a count", 0, here)
            ranged = False
        lo, hi = _numbers(accessor.get("min")), _numbers(accessor.get("max"))
        if lo is not None and hi is not None:
            low = [min(a, b) for a, b in zip(low, lo, strict=True)] if low else lo
            high = [max(a, b) for a, b in zip(high, hi, strict=True)] if high else hi
        else:
            problems.add("POSITION accessors without a min and max of three numbers", 0, here)
            ranged = False
    ranged = ranged and count <= INT64_MAX
    props = (
        [known("vertex_count", (count,), where, STATED)]
        if ranged
        else [missing("vertex_count", "unknown", where, STATED)]
    )
    if ranged and low:
        props += [known("bounds_min", low, where, STATED), known("bounds_max", high, where, STATED)]
    else:
        props += [missing("bounds_min", "unknown", where, STATED),
                  missing("bounds_max", "unknown", where, STATED)]  # fmt: skip
    return props


def _numbers(value: Any) -> list[float] | None:
    """Three finite numbers as floats, or ``None``: a JSON integer may be too large for a float."""
    if not isinstance(value, list) or len(value) != 3:
        return None
    found: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int | float):
            return None
        try:
            number = float(item)
        except OverflowError:
            return None
        if not math.isfinite(number):
            return None
        found.append(number)
    return found

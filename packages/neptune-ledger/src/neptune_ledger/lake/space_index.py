"""The spatial index: records by the frame or CRS they declare, with their extents (ADR 0015 §2).

Registration writes one ``spatial_extent`` row per spatial reference a record states as Known,
from its verified line read with the compiler's own reader:

| Kind | Reference (pointer) | Extent, in that reference |
|---|---|---|
| ``frame`` | ``/ref`` | — |
| ``frame_binding`` | ``/parent``, ``/child`` | — |
| ``frame_transform`` | ``/parent``, ``/child`` | on the target frame of a Known ``direction``: the source frame's origin, the declared translation (``/value/translation/values``, or column 3 of a matrix of Known ``layout``, ``/value/values``) |
| ``hardware_component`` | ``/frame/value`` | — |
| ``spatial_artifact`` | ``/frame/value``, ``/crs/value`` | — (its geometry stays in the file) |
| ``site``, ``asset`` | ``/location/value/crs/value`` | the declared point: longitude as x, latitude as y (``/location/value``) |
| ``image``, ``video`` | ``/capture/position/value/crs/value`` | the declared capture point (``/capture/position/value``) |

Coordinates are stored as declared, with the unit the record states (NULL when it is not Known).
Nothing is converted, reprojected or carried between frames, and there is no default world
frame: a reference that is not Known gives no row. ``read_within`` answers a box query in one
named reference and one named unit; members of that reference it cannot compare are returned as
unplaced, each with the reason, never dropped and never guessed into the box.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import psycopg

from neptune.identity import canonical_json
from neptune.model.frames import FrameRef, HomogeneousMatrix, MatrixLayout, Pose, TransformDirection
from neptune.model.kinds import RECORD_KINDS
from neptune.model.knowledge import Knowledge, Known
from neptune.model.spatial import CrsCode, GeodeticPosition
from neptune_ledger.api.types import CatalogFinding, TransactionKey

Conn = psycopg.Connection[tuple[Any, ...]]
ReferenceKind = Literal["crs", "frame"]
Reason = Literal["dimensions", "no_extent", "unit"]

# The kinds whose records can state a spatial reference, in name order.
SPATIAL_KINDS: Final = (
    "asset",
    "frame",
    "frame_binding",
    "frame_transform",
    "hardware_component",
    "image",
    "site",
    "spatial_artifact",
    "video",
)
# Where a 4x4 matrix holds its translation, by declared layout.
_MATRIX_TRANSLATION: Final = {
    MatrixLayout.ROW_MAJOR: (3, 7, 11),
    MatrixLayout.COLUMN_MAJOR: (12, 13, 14),
}


@dataclass(frozen=True)
class ExtentRow:
    """One ``spatial_extent`` row without tenant, package and registration key."""

    kind: str
    record_id: str
    pointer: str
    reference_kind: ReferenceKind
    reference: str
    extent_pointer: str | None = None
    unit: str | None = None
    low: tuple[float, ...] | None = None
    high: tuple[float, ...] | None = None


def extent_rows(lines: Mapping[str, Sequence[bytes]]) -> tuple[ExtentRow, ...]:
    """The rows of a package's verified record lines, sorted by record id and pointer."""
    out: list[ExtentRow] = []
    for kind in SPATIAL_KINDS:
        _, read = RECORD_KINDS[kind]
        for line in lines.get(kind, ()):
            out.extend(_rows(kind, read(canonical_json.loads(line))))
    return tuple(sorted(out, key=lambda r: (r.record_id, r.pointer)))


def _rows(kind: str, record: Any) -> list[ExtentRow]:
    rid = record.id
    match kind:
        case "frame":
            return [_frame(kind, rid, "/ref", record.ref)]
        case "frame_binding":
            return [
                _frame(kind, rid, "/parent", record.parent),
                _frame(kind, rid, "/child", record.child),
            ]
        case "frame_transform":
            return _transform(kind, rid, record)
        case "hardware_component":
            return _known_frame(kind, rid, "/frame/value", record.frame)
        case "spatial_artifact":
            rows = _known_frame(kind, rid, "/frame/value", record.frame)
            if isinstance(record.crs, Known):
                rows.append(_crs(kind, rid, "/crs/value", record.crs.value))
            return rows
        case "site" | "asset":
            return _position(kind, rid, "/location/value", record.location)
        case _:  # image, video
            return _position(kind, rid, "/capture/position/value", record.capture.position)


def _frame(
    kind: str,
    rid: str,
    pointer: str,
    ref: FrameRef,
    extent: tuple[str, str | None, tuple[float, ...]] | None = None,
) -> ExtentRow:
    reference = canonical_json.dumps(ref.to_json()).decode("utf-8")
    if extent is None:
        return ExtentRow(kind, rid, pointer, "frame", reference)
    at, unit, point = extent
    return ExtentRow(kind, rid, pointer, "frame", reference, at, unit, point, point)


def _known_frame(kind: str, rid: str, pointer: str, frame: Any) -> list[ExtentRow]:
    return [_frame(kind, rid, pointer, frame.value)] if isinstance(frame, Known) else []


def _crs(
    kind: str,
    rid: str,
    pointer: str,
    crs: CrsCode,
    extent: tuple[str, str | None, tuple[float, ...]] | None = None,
) -> ExtentRow:
    reference = canonical_json.dumps(crs.to_json()).decode("utf-8")
    if extent is None:
        return ExtentRow(kind, rid, pointer, "crs", reference)
    at, unit, point = extent
    return ExtentRow(kind, rid, pointer, "crs", reference, at, unit, point, point)


def _transform(kind: str, rid: str, record: Any) -> list[ExtentRow]:
    """``/parent`` and ``/child``; the target frame's row carries the source frame's origin.

    ``child_to_parent`` maps child coordinates into the parent, so its translation is the child's
    origin in the parent frame; ``parent_to_child`` is the parent's origin in the child frame.
    """
    point = _translation(record.value)
    target = None
    if isinstance(record.direction, Known) and point is not None:
        forward = record.direction.value is TransformDirection.CHILD_TO_PARENT
        target = "/parent" if forward else "/child"
    rows = []
    for pointer, ref in (("/parent", record.parent), ("/child", record.child)):
        rows.append(_frame(kind, rid, pointer, ref, point if pointer == target else None))
    return rows


def _translation(value: Any) -> tuple[str, str | None, tuple[float, ...]] | None:
    """Where a transform states its translation, its unit and its three values, or None."""
    if isinstance(value, Pose):
        unit = value.translation.unit
        symbol = unit.value.symbol if isinstance(unit, Known) else None
        return "/value/translation/values", symbol, tuple(value.translation.values)
    assert isinstance(value, HomogeneousMatrix)
    if not isinstance(value.layout, Known):
        return None
    unit = value.translation_unit
    symbol = unit.value.symbol if isinstance(unit, Known) else None
    at = _MATRIX_TRANSLATION[value.layout.value]
    return "/value/values", symbol, tuple(value.values[i] for i in at)


def _position(kind: str, rid: str, at: str, position: Any) -> list[ExtentRow]:
    """A declared geodetic point in its Known CRS, longitude as x and latitude as y. Height is
    not indexed: what it is measured from varies (ADR 0015 §2)."""
    if not isinstance(position, Known):
        return []
    value: GeodeticPosition = position.value
    if not isinstance(value.crs, Known):
        return []
    unit = value.angle_unit.value.symbol if isinstance(value.angle_unit, Known) else None
    point = (value.longitude, value.latitude)
    return [_crs(kind, rid, f"{at}/crs/value", value.crs.value, (at, unit, point))]


# --- Reads -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameReference:
    """A frame as a query names it: its declared id in one frame graph (``FrameRef``)."""

    frame_graph_id: str
    frame_id: str


@dataclass(frozen=True)
class CrsReference:
    """A coordinate reference system as a query names it, verbatim (``CrsCode``)."""

    authority: str
    code: str


Reference = FrameReference | CrsReference


@dataclass(frozen=True)
class SpatialBox:
    """An axis-aligned box: ``low[i] <= high[i]`` on 2 or 3 axes, both ends inclusive. A 2-axis
    box compares x and y and holds any z."""

    low: tuple[float, ...]
    high: tuple[float, ...]


@dataclass(frozen=True)
class SpatialEntry:
    """One record's row in the queried reference: where it states the reference and, if it
    states coordinates there, where, in which unit, and the extent as declared."""

    kind: str
    record_id: str
    package_id: str
    pointer: str
    registration_key: int
    extent_pointer: str | None = None
    unit: str | None = None
    low: tuple[float, ...] | None = None
    high: tuple[float, ...] | None = None


@dataclass(frozen=True)
class Unplaced:
    """A member of the queried reference that the box cannot be compared with, and why:
    ``no_extent`` (it states no coordinates there), ``unit`` (its unit is another, or not
    Known) or ``dimensions`` (a 3-axis box and a 2-axis extent)."""

    entry: SpatialEntry
    reason: Reason


@dataclass(frozen=True)
class SpatialResult:
    """The answer to a box query in one reference and unit at catalog point ``as_of``.

    ``placed`` holds the entries whose extent meets the box, ordered by extent then identity;
    ``unplaced`` every other member of the reference the box cannot be compared with. Members
    whose comparable extent misses the box are in neither.
    """

    outcome: Literal["answered", "refused"]
    reference: Reference
    unit: str
    box: SpatialBox
    as_of: Knowledge[TransactionKey]
    placed: tuple[SpatialEntry, ...]
    unplaced: tuple[Unplaced, ...]
    findings: tuple[CatalogFinding, ...]


def reference_text(reference: Reference) -> tuple[ReferenceKind, str]:
    """The stored ``(reference_kind, reference)`` of a validated reference."""
    if isinstance(reference, FrameReference):
        ref = FrameRef(reference.frame_id, reference.frame_graph_id)  # type: ignore[arg-type]
        return "frame", canonical_json.dumps(ref.to_json()).decode("utf-8")
    crs = CrsCode(reference.authority, reference.code)
    return "crs", canonical_json.dumps(crs.to_json()).decode("utf-8")


_MEMBERS: Final = """
SELECT kind, record_id, package_id, pointer, registration_key, extent_pointer, dims, unit,
       min_x, min_y, min_z, max_x, max_y, max_z
  FROM spatial_extent
 WHERE tenant_id = %(tenant)s AND reference_kind = %(kind)s AND reference = %(reference)s
   AND registration_key <= %(as_of)s
"""
# The R-tree finds candidates; PostgreSQL's box operators compare with a tolerance, so the exact
# float8 comparisons on the stored extent decide.
_PLACED: Final = (
    _MEMBERS
    + """
   AND unit = %(unit)s AND xy && box(point(%(x0)s, %(y0)s), point(%(x1)s, %(y1)s))
   AND min_x <= %(x1)s AND max_x >= %(x0)s AND min_y <= %(y1)s AND max_y >= %(y0)s
"""
)
_PLACED_3D: Final = _PLACED + "   AND dims = 3 AND min_z <= %(z1)s AND max_z >= %(z0)s\n"
_UNPLACED: Final = (
    _MEMBERS + "   AND (dims IS NULL OR unit IS DISTINCT FROM %(unit)s OR (%(three)s AND dims = 2))\n"
)


def read_within(
    conn: Conn, tenant: str, reference: Reference, unit: str, box: SpatialBox, limit: int
) -> tuple[tuple[SpatialEntry, ...], tuple[Unplaced, ...]]:
    """The placed and unplaced members of a validated box query at ``limit`` (a tx_seq)."""
    kind, text = reference_text(reference)
    three = len(box.low) == 3
    params: dict[str, Any] = {
        "tenant": tenant,
        "kind": kind,
        "reference": text,
        "as_of": limit,
        "unit": unit,
        "three": three,
        "x0": box.low[0],
        "y0": box.low[1],
        "x1": box.high[0],
        "y1": box.high[1],
    }
    if three:
        params |= {"z0": box.low[2], "z1": box.high[2]}
    placed = [_entry(row)[0] for row in conn.execute(_PLACED_3D if three else _PLACED, params)]
    unplaced = []
    for row in conn.execute(_UNPLACED, params):
        entry, dims = _entry(row)
        reason: Reason = (
            "no_extent" if dims is None else "unit" if entry.unit != unit else "dimensions"
        )
        unplaced.append(Unplaced(entry, reason))
    placed.sort(key=lambda e: (e.low, e.high, *_identity(e)))
    unplaced.sort(key=lambda u: (u.reason, *_identity(u.entry)))
    return tuple(placed), tuple(unplaced)


def _identity(entry: SpatialEntry) -> tuple[bytes, bytes, bytes]:
    return (
        entry.record_id.encode("utf-8"),
        entry.package_id.encode("utf-8"),
        entry.pointer.encode("utf-8"),
    )


def _entry(row: Any) -> tuple[SpatialEntry, int | None]:
    kind, record, package, pointer, seq, at, dims, unit, *bounds = row
    low = high = None
    if dims is not None:
        n = int(dims)
        low = tuple(float(v) for v in bounds[:3][:n])
        high = tuple(float(v) for v in bounds[3:][:n])
    entry = SpatialEntry(
        kind=str(kind),
        record_id=str(record),
        package_id=str(package),
        pointer=str(pointer),
        registration_key=int(seq),
        extent_pointer=None if at is None else str(at),
        unit=None if unit is None else str(unit),
        low=low,
        high=high,
    )
    return entry, None if dims is None else int(dims)

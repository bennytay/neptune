"""Measuring a decoded geometry: its type, how many positions it holds, and their bounds.

The geometry stays in the source bytes; this reads it once to say what is there, and what is wrong
with it, without ever changing it. Nothing is reprojected, wrapped at the antimeridian or closed:

- **Bounds** are the plain minimum and maximum of the finite coordinates, axis by axis, in the
  CRS's own numbers (``x`` and ``y`` are the first two, ``z`` the third where a position has one).
- **Positions** are arrays of two or more numbers. Anything else is ``geometry_invalid``. A NaN,
  an infinity or a number too large for a double is ``non_finite_coordinate`` and is left out.
- **Range** (longitude within [-180, 180], latitude within [-90, 90]) is checked only when the CRS
  is known to be geographic.
- **Rings** (RFC 7946 §3.1.6) have four or more positions and end where they start; where the CRS
  is geographic the exterior ring is counterclockwise and a hole clockwise, in x and y.
- **Walking is bounded**: no recursion (a stack), at most ``max_positions`` positions, at most
  ``max_depth`` levels of ``GeometryCollection``. A coordinate array nested deeper than its type
  allows is not descended: its first element is not a number, so it is ``geometry_invalid``.
"""

import math
from dataclasses import dataclass, field

from neptune.adapters.geojson._common import GEOMETRY_TYPES, Limits

_LON, _LAT = 180.0, 90.0


class _Budget(Exception):
    pass


@dataclass
class Measured:
    type: str | None = None
    positions: int | None = 0  # None when the budget stopped the walk
    x0: float | None = None
    y0: float | None = None
    x1: float | None = None
    y1: float | None = None
    z0: float | None = None
    z1: float | None = None
    problems: dict[str, str] = field(default_factory=dict)
    nested_crs: bool = False

    @property
    def bounded(self) -> bool:
        """Whether the bounds are the geometry's: it was walked whole and fits its type."""
        return not (self.problems.keys() & {"geometry_invalid", "position_budget", "too_deep"})


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _flat(item: object) -> bool:
    return isinstance(item, list) and all(_number(v) for v in item)


class _Walk:
    def __init__(self, geographic: bool, limits: Limits) -> None:
        self.out = Measured()
        self.geographic = geographic
        self.limits = limits
        self.count = 0

    def problem(self, code: str, message: str) -> None:
        self.out.problems.setdefault(code, message)

    def position(self, item: object) -> tuple[float, float] | None:
        """Count one position; its finite ``x`` and ``y``, or ``None``."""
        if not (isinstance(item, list) and len(item) >= 2 and all(_number(v) for v in item)):
            self.problem("geometry_invalid", "a position is not an array of two or more numbers")
            return None
        self.count += 1
        if self.count > self.limits.max_positions:
            raise _Budget
        try:
            values = [float(v) for v in item[:3]]
        except OverflowError:
            self.problem("non_finite_coordinate", "an integer coordinate too large for a double")
            return None
        if not all(math.isfinite(v) for v in values):
            self.problem("non_finite_coordinate", "a coordinate is NaN or infinite")
            return None
        x, y = values[0], values[1]
        out = self.out
        out.x0 = x if out.x0 is None else min(out.x0, x)
        out.x1 = x if out.x1 is None else max(out.x1, x)
        out.y0 = y if out.y0 is None else min(out.y0, y)
        out.y1 = y if out.y1 is None else max(out.y1, y)
        if len(values) > 2:
            z = values[2]
            out.z0 = z if out.z0 is None else min(out.z0, z)
            out.z1 = z if out.z1 is None else max(out.z1, z)
        if self.geographic and not (-_LON <= x <= _LON and -_LAT <= y <= _LAT):
            self.problem(
                "coordinate_out_of_range",
                f"a position ({x!r}, {y!r}) lies outside longitude and latitude's range",
            )
        return x, y

    def positions(self, items: object, minimum: int = 0) -> list[tuple[float, float] | None]:
        if not isinstance(items, list):
            self.problem("geometry_invalid", "coordinates are not an array of positions")
            return []
        if len(items) < minimum:
            self.problem("geometry_invalid", f"a line has fewer than {minimum} positions")
        return [self.position(item) for item in items]

    def ring(self, items: object, exterior: bool) -> None:
        found = self.positions(items)
        if not isinstance(items, list) or not items:
            return
        if len(items) < 4:
            self.problem("ring_too_short", "a ring has fewer than four positions")
        first, last = items[0], items[-1]
        if _flat(first) and _flat(last) and first != last:  # never compare what nests
            self.problem("ring_not_closed", "a ring's first and last positions differ")
        if not self.geographic or len(found) < 3 or any(p is None for p in found):
            return
        points = [p for p in found if p is not None]
        twice = 0.0
        for (x0, y0), (x1, y1) in zip(points, [*points[1:], points[0]], strict=True):
            twice += x0 * y1 - x1 * y0
        if math.isfinite(twice) and twice != 0.0 and (twice > 0.0) != exterior:
            self.problem(
                "ring_winding",
                "an exterior ring is not counterclockwise, or a hole is not clockwise",
            )

    def polygon(self, rings: object) -> None:
        if not isinstance(rings, list):
            self.problem("geometry_invalid", "a polygon's coordinates are not an array of rings")
            return
        for index, ring in enumerate(rings):
            self.ring(ring, exterior=index == 0)

    def shape(self, kind: str, coordinates: object) -> None:
        if kind == "Point":
            self.position(coordinates)
        elif kind == "MultiPoint":
            self.positions(coordinates)
        elif kind == "LineString":
            self.positions(coordinates, 2)
        elif kind == "MultiLineString":
            if isinstance(coordinates, list):
                for line in coordinates:
                    self.positions(line, 2)
            else:
                self.problem("geometry_invalid", "coordinates are not an array of lines")
        elif kind == "Polygon":
            self.polygon(coordinates)
        elif isinstance(coordinates, list):  # MultiPolygon
            for polygon in coordinates:
                self.polygon(polygon)
        else:
            self.problem("geometry_invalid", "coordinates are not an array of polygons")


def measure(geometry: object, *, geographic: bool, limits: Limits) -> Measured:
    """What one decoded geometry holds, and what is wrong with it."""
    walk = _Walk(geographic, limits)
    out = walk.out
    stack: list[tuple[object, int]] = [(geometry, 1)]
    try:
        while stack:
            item, depth = stack.pop()
            if not isinstance(item, dict):
                walk.problem("geometry_invalid", "a geometry is not an object")
                continue
            kind = item.get("type")
            if depth == 1:
                out.type = kind if isinstance(kind, str) else None
            if "crs" in item:
                out.nested_crs = True
            if kind not in GEOMETRY_TYPES:
                walk.problem("geometry_invalid", "a geometry's type is not a GeoJSON geometry type")
            elif kind == "GeometryCollection":
                members = item.get("geometries")
                if not isinstance(members, list):
                    walk.problem("geometry_invalid", "a GeometryCollection has no geometries array")
                elif depth >= limits.max_depth:
                    walk.problem("too_deep", "GeometryCollections nest deeper than max_depth")
                else:
                    stack.extend((member, depth + 1) for member in reversed(members))
            elif "coordinates" not in item:
                walk.problem("geometry_invalid", "a geometry has no coordinates")
            else:
                walk.shape(str(kind), item["coordinates"])
    except _Budget:
        walk.problem(
            "position_budget", f"more than max_positions ({limits.max_positions}) positions"
        )
        out.positions = None
        return out
    out.positions = walk.count
    return out

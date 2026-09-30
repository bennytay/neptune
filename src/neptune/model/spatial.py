"""Earth-referenced positions as the evidence declares them (ADR 0015 §6).

A position keeps its numbers as declared and wraps what they mean (CRS, units, what the height is
measured from) in ``Knowledge``. Nothing here assumes WGS 84, degrees or ellipsoidal height, and
nothing converts between CRSs, geoids or height references: those are derived transforms.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from neptune.model._fields import (
    check_type,
    check_unit,
    enum_decoder,
    exact_object,
    json_str,
    unit_json,
)
from neptune.model.frames import ANGLE, LENGTH
from neptune.model.ids import check_text
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import Grounding, Knowledge, from_json, to_json
from neptune.model.units import Unit, unit_from_json

# Bound on hostile or absurd authority names and codes. Real ones are far shorter.
MAX_TEXT_LENGTH: Final = 64


@dataclass(frozen=True)
class CrsCode:
    """A coordinate reference system by registry code, verbatim: ``CrsCode("EPSG", "4326")``.

    Nothing is case-folded or looked up. Whether ``EPSG:4326`` and ``OGC:CRS84`` describe the same
    datum (they differ in axis order) is a derived question.
    """

    authority: str
    code: str

    def __post_init__(self) -> None:
        for name, text in (("authority", self.authority), ("code", self.code)):
            if not isinstance(text, str):
                raise TypeError(f"{name} must be a str, got {type(text).__name__}")
            check_text(name, text)
            if len(text) > MAX_TEXT_LENGTH:
                raise ValueError(f"{name} is longer than {MAX_TEXT_LENGTH} characters")

    def to_json(self) -> JsonObject:
        return {"authority": self.authority, "code": self.code}


def crs_code_from_json(data: JsonValue) -> CrsCode:
    obj = exact_object(data, "crs code", {"authority", "code"})
    return CrsCode(json_str(obj["authority"], "authority"), json_str(obj["code"], "code"))


class HeightReference(StrEnum):
    """What a height is measured from."""

    ELLIPSOID = "ellipsoid"  # the CRS's ellipsoid: raw GNSS height
    MEAN_SEA_LEVEL = "mean_sea_level"  # orthometric, above a geoid model (the CRS names which)
    HOME = "home"  # a home or take-off point: MAVLink relative_alt
    GROUND = "ground"  # the terrain below: a rangefinder's height above ground


def _check_coordinate(name: str, value: float) -> None:
    if not isinstance(value, float):
        raise TypeError(f"{name} must be a float, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{name} {value!r} is not finite")


@dataclass(frozen=True)
class GeodeticPosition:
    """Latitude, longitude and height as declared, e.g. from ``sensor_msgs/NavSatFix``.

    The adapter maps the source's own named fields to ``latitude`` and ``longitude``; it never
    relies on a CRS's axis order to do so. A source with no height has ``height`` ``NotCovered``.
    Ranges are not checked, because the unit may be unknown.
    """

    latitude: float
    longitude: float
    height: Knowledge[float]
    crs: Knowledge[CrsCode]
    angle_unit: Knowledge[Unit]
    height_unit: Knowledge[Unit]
    height_reference: Knowledge[HeightReference]

    def __post_init__(self) -> None:
        _check_coordinate("latitude", self.latitude)
        _check_coordinate("longitude", self.longitude)
        check_type("height", self.height, float)
        check_type("crs", self.crs, CrsCode)
        check_unit("angle unit", self.angle_unit, ANGLE)
        check_unit("height unit", self.height_unit, LENGTH)
        check_type("height_reference", self.height_reference, HeightReference)

    def to_json(self) -> JsonObject:
        return {
            "angle_unit": to_json(self.angle_unit, unit_json),
            "crs": to_json(self.crs, CrsCode.to_json),
            "height": to_json(self.height),
            "height_reference": to_json(self.height_reference, str),
            "height_unit": to_json(self.height_unit, unit_json),
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


def _float(data: JsonValue) -> float:
    if not isinstance(data, float):
        raise ValueError(f"expected a float, got {data!r}")
    return data


def geodetic_position_from_json(
    data: JsonValue, decode_provenance: Callable[[JsonObject], Grounding]
) -> GeodeticPosition:
    """Parse strictly: unexpected or missing keys and ints for floats are errors."""
    obj = exact_object(
        data,
        "geodetic position",
        {
            "angle_unit",
            "crs",
            "height",
            "height_reference",
            "height_unit",
            "latitude",
            "longitude",
        },
    )
    return GeodeticPosition(
        latitude=_float(obj["latitude"]),
        longitude=_float(obj["longitude"]),
        height=from_json(obj["height"], _float, decode_provenance),
        crs=from_json(obj["crs"], crs_code_from_json, decode_provenance),
        angle_unit=from_json(obj["angle_unit"], unit_from_json, decode_provenance),
        height_unit=from_json(obj["height_unit"], unit_from_json, decode_provenance),
        height_reference=from_json(
            obj["height_reference"], enum_decoder(HeightReference), decode_provenance
        ),
    )

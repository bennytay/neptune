from dataclasses import dataclass, replace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.identity import canonical_json
from neptune.model.jsonvalue import JsonObject
from neptune.model.knowledge import Ambiguous, Candidate, Known, NotCovered, Unknown
from neptune.model.spatial import (
    MAX_TEXT_LENGTH,
    CrsCode,
    GeodeticPosition,
    HeightReference,
    crs_code_from_json,
    geodetic_position_from_json,
)
from neptune.model.units import unit_from_json


@dataclass(frozen=True)
class Cite:
    """Stand-in for MVL-3's Provenance: anything with ``to_json``."""

    where: str

    def to_json(self) -> JsonObject:
        return {"where": self.where}


def cite(data: JsonObject) -> Cite:
    where = data["where"]
    assert isinstance(where, str)
    return Cite(where)


NAVSATFIX = Cite("sensor_msgs/NavSatFix.msg")
WGS84 = CrsCode("EPSG", "4326")


def navsatfix(**overrides: Any) -> GeodeticPosition:
    fields: dict[str, Any] = {
        "latitude": 47.397742,
        "longitude": 8.545594,
        "height": Known(488.1),
        "crs": Known(WGS84, NAVSATFIX),
        "angle_unit": Known(unit_from_json("deg"), NAVSATFIX),
        "height_unit": Known(unit_from_json("m"), NAVSATFIX),
        "height_reference": Known(HeightReference.ELLIPSOID, NAVSATFIX),
    }
    fields.update(overrides)
    return GeodeticPosition(**fields)


def round_trip(position: GeodeticPosition) -> None:
    data = canonical_json.dumps(position.to_json())
    assert geodetic_position_from_json(canonical_json.loads(data), cite) == position


def test_crs_codes_are_verbatim() -> None:
    assert len({WGS84, CrsCode("epsg", "4326"), CrsCode("OGC", "CRS84")}) == 3
    assert crs_code_from_json(canonical_json.loads(canonical_json.dumps(WGS84.to_json()))) == WGS84


@pytest.mark.parametrize(
    ("authority", "code", "error"),
    [
        ("", "4326", ValueError),
        ("EPSG", "", ValueError),
        ("EPSG", 4326, TypeError),
        ("EPSG", "\udc80", ValueError),
        ("E" * (MAX_TEXT_LENGTH + 1), "4326", ValueError),
    ],
)
def test_bad_crs_codes_are_rejected(authority: Any, code: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        CrsCode(authority, code)


def test_nothing_is_assumed_about_an_undeclared_position() -> None:
    position = navsatfix(
        height=NotCovered(),
        crs=Unknown(),
        angle_unit=Unknown(),
        height_unit=NotCovered(),
        height_reference=NotCovered(),
    )
    round_trip(position)


def test_values_are_kept_as_declared() -> None:
    # MAVLink GLOBAL_POSITION_INT: degE7 and mm above home. The catalogue has no degE7 yet, so the
    # angle unit is Unknown and the integers are kept, not rescaled.
    position = navsatfix(
        latitude=473977420.0,
        longitude=85455940.0,
        height=Known(12_000.0),
        crs=Unknown(),
        angle_unit=Unknown(),
        height_unit=Known(unit_from_json("mm")),
        height_reference=Known(HeightReference.HOME),
    )
    assert (position.latitude, position.longitude) == (473977420.0, 85455940.0)
    round_trip(position)


def test_ambiguous_height_reference_is_representable() -> None:
    reference = Ambiguous(
        (
            Candidate(HeightReference.ELLIPSOID, Cite("receiver manual")),
            Candidate(HeightReference.MEAN_SEA_LEVEL, Cite("NMEA GGA field 9")),
        )
    )
    round_trip(navsatfix(height_reference=reference))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("latitude", 47),
        ("latitude", float("nan")),
        ("longitude", float("-inf")),
        ("height", Known(488)),
        ("crs", Known("EPSG:4326")),
        ("angle_unit", Known(unit_from_json("m"))),
        ("height_unit", Known(unit_from_json("deg"))),
        ("height_reference", Known("ellipsoid")),
    ],
)
def test_bad_fields_are_rejected(field: str, value: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        navsatfix(**{field: value})


@pytest.mark.parametrize(
    ("key", "value"),
    [("latitude", 47), ("extra", 1.0), ("height", {"knowledge": "known", "value": 488})],
)
def test_json_parsing_is_strict(key: str, value: Any) -> None:
    data = dict(navsatfix().to_json())
    data[key] = value
    with pytest.raises(ValueError):
        geodetic_position_from_json(data, cite)


finite = st.floats(allow_nan=False, allow_infinity=False)


@given(latitude=finite, longitude=finite, height=finite)
def test_any_finite_position_round_trips_exactly(
    latitude: float, longitude: float, height: float
) -> None:
    round_trip(replace(navsatfix(), latitude=latitude, longitude=longitude, height=Known(height)))

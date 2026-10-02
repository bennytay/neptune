"""Build the GeoJSON adapter's fixtures: site and route files across embodiments, and broken ones.

Run ``uv run python tests/fixtures/geojson/make_geojson_fixtures.py`` to rewrite every file under
``tests/fixtures/geojson/``; ``--check`` compares the committed files to ``FILES`` and exits 1 if
one differs. Every file is hand-written text, so its bytes are what the test expects.
"""

import sys
from pathlib import Path
from typing import Final

HERE: Final = Path(__file__).parent

# A warehouse mobile base (AMR): RFC 7946, no crs member. A site polygon (counterclockwise), two
# charging docks as assets (one with an altitude, ids as string and as integer), a restricted
# zone line with a nested property and a null, a non-ASCII name.
WAREHOUSE_AMR: Final = (
    b'{"type": "FeatureCollection", "name": "Warehouse 7 floor map", "features": [\n'
    b'{"type": "Feature", "id": "wh7-outline", "geometry": {"type": "Polygon", "coordinates":'
    b" [[[103.6001, 1.3501], [103.6011, 1.3501], [103.6011, 1.3511], [103.6001, 1.3511],"
    b' [103.6001, 1.3501]]]}, "properties": {"site_id": "WH-7", "name": "Warehouse 7",'
    b' "operator": {"name": "Tuas Logistics", "contact": null}}},\n'
    b'{"type": "Feature", "id": 101, "geometry": {"type": "Point", "coordinates":'
    b' [103.6004, 1.3504, 4.5]}, "properties": {"asset_id": "DOCK-01", "site_id": "WH-7",'
    b' "category": "charging dock", "name": "Dock 1"}},\n'
    b'{"type": "Feature", "geometry": {"type": "Point", "coordinates": [103.6008, 1.3508]},'
    b' "properties": {"asset_id": 17, "name": "Dock 2 \xc2\xb7 north"}},\n'
    b'{"type": "Feature", "id": "zone-a", "geometry": {"type": "LineString", "coordinates":'
    b' [[103.6002, 1.3502], [103.6006, 1.3506]]}, "properties": {"restricted": true,'
    b' "speed_limit_mps": 0.5}}\n'
    b"]}\n"
)

# An autonomous vehicle's route: a legacy (GeoJSON 2008) crs member naming OGC:CRS84 by URN.
AV_ROUTE: Final = (
    b'{"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name":'
    b' "urn:ogc:def:crs:OGC:1.3:CRS84"}}, "features": [\n'
    b'{"type": "Feature", "id": "route-12", "geometry": {"type": "LineString", "coordinates":'
    b" [[-122.4194, 37.7749], [-122.4180, 37.7755], [-122.4172, 37.7761]]},"
    b' "properties": {"route_id": "R-12", "speed_limit_kph": 40}},\n'
    b'{"type": "Feature", "id": "depot", "geometry": {"type": "Point", "coordinates":'
    b' [-122.4194, 37.7749]}, "properties": {"site_id": "DEPOT-SF", "name": "SF depot"}}\n'
    b"]}\n"
)

# A marine survey area: a legacy crs member naming a projected CRS (UTM 48N). Not geographic: no
# range check, no location, whatever the numbers look like.
MARINE_SURVEY: Final = (
    b'{"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name":'
    b' "EPSG:32648"}}, "features": [\n'
    b'{"type": "Feature", "id": "survey-box", "geometry": {"type": "Polygon", "coordinates":'
    b" [[[366000.0, 143000.0], [367000.0, 143000.0], [367000.0, 144000.0], [366000.0, 144000.0],"
    b' [366000.0, 143000.0]]]}, "properties": {"site_id": "SURVEY-3", "name": "Strait survey 3",'
    b' "depth_m": [12.5, 18.0]}},\n'
    b'{"type": "Feature", "id": "buoy-9", "geometry": {"type": "Point", "coordinates":'
    b' [366500.0, 143500.0]}, "properties": {"asset_id": "BUOY-9", "site_id": "SURVEY-3",'
    b' "category": "marker buoy"}}\n'
    b"]}\n"
)

# An agricultural field boundary as one Feature: a clockwise exterior ring (RFC 7946 advises
# counterclockwise: an info finding, not an error) and a clockwise hole.
FARM_FIELD: Final = (
    b'{"type": "Feature", "id": "field-north-40", "geometry": {"type": "Polygon", "coordinates":'
    b" [[[149.1000, -35.2000], [149.1030, -35.2000], [149.1030, -35.2020], [149.1000, -35.2020],"
    b" [149.1000, -35.2000]], [[149.1010, -35.2005], [149.1015, -35.2005], [149.1015, -35.2010],"
    b' [149.1010, -35.2010], [149.1010, -35.2005]]]}, "properties": {"site_id": "FARM-N40",'
    b' "name": "North 40", "crop": "wheat"}}\n'
)

# A drone geofence: a polygon with a ceiling, an inclusion zone and a launch point at altitude.
DRONE_GEOFENCE: Final = (
    b'{"type": "FeatureCollection", "features": [\n'
    b'{"type": "Feature", "id": "fence-1", "geometry": {"type": "Polygon", "coordinates":'
    b" [[[8.5400, 47.3700], [8.5500, 47.3700], [8.5500, 47.3780], [8.5400, 47.3780],"
    b' [8.5400, 47.3700]]]}, "properties": {"max_altitude_m": 120, "kind": "inclusion"}},\n'
    b'{"type": "Feature", "id": "launch", "geometry": {"type": "Point", "coordinates":'
    b' [8.5450, 47.3710, 408.0]}, "properties": {"asset_id": "LAUNCH-ZRH-1",'
    b' "category": "launch pad", "name": "North pad"}}\n'
    b"]}\n"
)

# A bare geometry: no Feature around it.
BARE_POINT: Final = b'{"type": "Point", "coordinates": [-0.1276, 51.5072]}\n'

# No crs member, and positions no longitude or latitude could be: RFC 7946's default does not hold.
UTM_NO_CRS: Final = (
    b'{"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type":'
    b' "Point", "coordinates": [366500.0, 143500.0]}, "properties": {"asset_id": "BUOY-1"}}]}\n'
)

# crs members the file cannot be read by: a link, a null, and two that disagree.
CRS_LINK: Final = (
    b'{"type": "FeatureCollection", "crs": {"type": "link", "properties": {"href":'
    b' "http://example.org/crs.wkt", "type": "ogcwkt"}}, "features": [{"type": "Feature",'
    b' "geometry": {"type": "Point", "coordinates": [1.0, 2.0]}, "properties": {}}]}\n'
)
CRS_NULL: Final = (
    b'{"type": "FeatureCollection", "crs": null, "features": [{"type": "Feature", "geometry":'
    b' {"type": "Point", "coordinates": [1.0, 2.0]}, "properties": {}}]}\n'
)
CRS_AMBIGUOUS: Final = (
    b'{"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name":'
    b' "EPSG:4326"}}, "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},'
    b' "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates":'
    b' [1.0, 2.0]}, "properties": {}}]}\n'
)

# Truncated inside the third feature.
TRUNCATED: Final = WAREHOUSE_AMR[: WAREHOUSE_AMR.index(b'"Dock 2')]

# Geometries that are wrong in each way the adapter checks, in one file (in a geographic CRS the
# file states): an unclosed ring, a short ring, a position of one number, an unknown type, a
# longitude of 200, a NaN, no coordinates, a null geometry.
BAD_GEOMETRY: Final = (
    b'{"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name":'
    b' "urn:ogc:def:crs:EPSG::4326"}}, "features": [\n'
    b'{"type": "Feature", "id": "unclosed", "geometry": {"type": "Polygon", "coordinates":'
    b' [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]]}, "properties": null},\n'
    b'{"type": "Feature", "id": "short", "geometry": {"type": "Polygon", "coordinates":'
    b' [[[0.0, 0.0], [1.0, 0.0], [0.0, 0.0]]]}, "properties": null},\n'
    b'{"type": "Feature", "id": "one-number", "geometry": {"type": "Point", "coordinates":'
    b' [5.0]}, "properties": null},\n'
    b'{"type": "Feature", "id": "unknown-type", "geometry": {"type": "Circle", "coordinates":'
    b' [5.0, 6.0]}, "properties": null},\n'
    b'{"type": "Feature", "id": "far-east", "geometry": {"type": "Point", "coordinates":'
    b' [200.0, 10.0]}, "properties": {"asset_id": "A-1"}},\n'
    b'{"type": "Feature", "id": "nan", "geometry": {"type": "LineString", "coordinates":'
    b' [[1.0, 2.0], [NaN, 3.0], [4.0, 5.0]]}, "properties": null},\n'
    b'{"type": "Feature", "id": "no-coordinates", "geometry": {"type": "Point"},'
    b' "properties": null},\n'
    b'{"type": "Feature", "id": "null-geometry", "geometry": null, "properties": null}\n'
    b"]}\n"
)

# A feature whose bytes are not UTF-8 (a Latin-1 e-acute in a name), between two good ones.
BAD_UTF8: Final = (
    b'{"type": "FeatureCollection", "features": [\n'
    b'{"type": "Feature", "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},'
    b' "properties": {"site_id": "S-1", "name": "ok"}},\n'
    b'{"type": "Feature", "geometry": {"type": "Point", "coordinates": [3.0, 4.0]},'
    b' "properties": {"site_id": "S-2", "name": "caf\xe9"}},\n'
    b'{"type": "Feature", "geometry": {"type": "Point", "coordinates": [5.0, 6.0]},'
    b' "properties": {"site_id": "S-3", "name": "ok too"}}\n'
    b"]}\n"
)

FILES: Final[dict[str, bytes]] = {
    "warehouse_amr_site.geojson": WAREHOUSE_AMR,
    "av_route_legacy_crs.geojson": AV_ROUTE,
    "marine_survey_projected.geojson": MARINE_SURVEY,
    "farm_field_boundary.geojson": FARM_FIELD,
    "drone_geofence.geojson": DRONE_GEOFENCE,
    "bare_point.geojson": BARE_POINT,
    "utm_no_crs.geojson": UTM_NO_CRS,
    "crs_link.geojson": CRS_LINK,
    "crs_null.geojson": CRS_NULL,
    "crs_ambiguous.geojson": CRS_AMBIGUOUS,
    "truncated.geojson": TRUNCATED,
    "bad_geometry.geojson": BAD_GEOMETRY,
    "bad_utf8.geojson": BAD_UTF8,
    "empty.geojson": b"",
}


def main(argv: list[str]) -> int:
    check = "--check" in argv
    failed = False
    for name, data in FILES.items():
        path = HERE / name
        if check:
            if not path.exists() or path.read_bytes() != data:
                sys.stderr.write(f"differs: {name}\n")
                failed = True
        else:
            path.write_bytes(data)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

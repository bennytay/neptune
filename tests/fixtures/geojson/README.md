# GeoJSON fixtures

Real small GeoJSON files for the `geojson` adapter (`neptune.adapters.geojson`, ADR 0057), one per
embodiment and one per case the adapter checklist in `docs/adapter-contract.md` asks for.
`make_geojson_fixtures.py` writes every file (`--check` compares the committed ones);
`tests/unit/adapters/test_geojson_adapter.py` checks them against it and against the standard library.

| File | Case | What the adapter must do |
|---|---|---|
| `warehouse_amr_site.geojson` | valid RFC 7946: a mobile base's site, docks, a zone; non-ASCII, ids as string and integer | default CRS stated by the spec; Site, Assets; a Point is a location |
| `av_route_legacy_crs.geojson` | valid: an autonomous vehicle's route, a 2008 `crs` member naming OGC:CRS84 | CRS `Known` as written, `crs_legacy` |
| `marine_survey_projected.geojson` | valid: a survey area in UTM 48N (`EPSG:32648`) | CRS `Known`, no range check, no location |
| `farm_field_boundary.geojson` | valid: one Feature, a clockwise exterior ring and a hole | `ring_winding` info |
| `drone_geofence.geojson` | valid: a polygon with a ceiling and a launch point with altitude | height from the third number |
| `bare_point.geojson` | valid: a bare geometry, no Feature | one feature, row 0 |
| `utm_no_crs.geojson` | no `crs` member, positions no longitude or latitude could be | CRS `Unknown`, never WGS 84 |
| `crs_link.geojson`, `crs_null.geojson` | `crs` that is a link (never followed) or null | CRS `Unknown`, `crs_unknown` |
| `crs_ambiguous.geojson` | two different `crs` members | CRS `Ambiguous`, each cited |
| `truncated.geojson` | cut inside the third feature | two features; `json_truncated`; CRS `Unknown` |
| `bad_geometry.geojson` | unclosed and short rings, one-number position, unknown type, longitude 200, NaN, no coordinates, null geometry | one finding per code; the rest land |
| `bad_utf8.geojson` | a feature with a Latin-1 byte between two good ones | `invalid_utf8`; that feature has no record |
| `empty.geojson` | zero bytes | `not_geojson`, no records |

Hostile sizes (200,000-deep arrays, 400-digit integers, 300,000-position lines, 12,000 features past the
reader's window) are built in the tests, not committed.

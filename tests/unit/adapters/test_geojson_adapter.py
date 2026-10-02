"""The GeoJSON adapter: probing, records, the CRS rules, validation, identity hints and damage.

The oracle for every citation is the standard library: a cell's evidence names bytes of the file,
and decoding those bytes with ``json`` must give the cell's value.
"""

import importlib.util
import json
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters import geojson
from neptune.adapters.builtin import default_registry
from neptune.adapters.contract import PROBE_HEAD_SIZE, VERIFIED, ProbeHints
from neptune.adapters.geojson import GeoJsonAdapter
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.discovery.probe import ProbeEngine
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.ids import LogicalId
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import ByteRange
from neptune.model.scalars import NonFinite
from neptune.model.spatial import CrsCode, GeodeticPosition
from neptune.model.world import Asset, Site, SpatialArtifact, StructuredRecord, StructuredTable

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "geojson"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(data: bytes, **config: Any) -> SourceOutput:
    return ingest_source(GeoJsonAdapter(), BytesReader(data), config)


def probe(data: bytes, name: str = "x") -> float:
    return GeoJsonAdapter().probe(data[:PROBE_HEAD_SIZE], ProbeHints(name, len(data))).confidence


def codes(output: SourceOutput) -> list[str]:
    return sorted(f.code.removeprefix("geojson.") for f in output.findings())


def kinds(output: SourceOutput, kind: type) -> list[Any]:
    return [r for r in output.records() if isinstance(r, kind)]


def artifact(output: SourceOutput) -> Any:
    (found,) = kinds(output, SpatialArtifact)
    return found


def table(output: SourceOutput, name: str) -> Any:
    (found,) = [t for t in kinds(output, StructuredTable) if getattr(t.name, "value", "") == name]
    return found


def rows(output: SourceOutput, name: str) -> list[Any]:
    tables = [t for t in kinds(output, StructuredTable) if getattr(t.name, "value", "") == name]
    if not tables:
        return []
    wanted = tables[0].id
    found = [r for r in kinds(output, StructuredRecord) if r.table == wanted]
    return sorted(found, key=lambda r: (r.row, r.id))


def feature_rows(output: SourceOutput) -> dict[int, Any]:
    return {r.row: r for r in rows(output, "features")}


def as_bytes(output: SourceOutput) -> bytes:
    return b"".join(canonical_json.dumps(r.to_json()) + b"\n" for r in output.package_records())


def Any_(value: Any) -> Any:
    return value


def prov(knowledge: Any) -> Any:
    return knowledge.provenance


def at(data: bytes, evidence: Any) -> bytes:
    (step, *_) = evidence.locator
    assert isinstance(step, ByteRange)
    return data[step.offset : step.offset + step.length]


def json_at(data: bytes, evidence: Any) -> Any:
    return json.loads(at(data, evidence).decode("utf-8"))


def by_ns(record: Asset | Site) -> dict[str, str]:
    found = {}
    for known in record.identifiers:
        assert isinstance(known, Known)
        found[known.value.namespace] = known.value.value
    return found


# --- Fixtures and probing ------------------------------------------------------------------------

VALID: Final = (
    "warehouse_amr_site.geojson",
    "av_route_legacy_crs.geojson",
    "marine_survey_projected.geojson",
    "farm_field_boundary.geojson",
    "drone_geofence.geojson",
    "bare_point.geojson",
)


def test_committed_fixtures_match_their_generator() -> None:
    spec = importlib.util.spec_from_file_location(
        "make_geojson_fixtures", FIXTURES / "make_geojson_fixtures.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, data in module.FILES.items():
        assert fixture(name) == data, name


@pytest.mark.parametrize("name", VALID)
def test_valid_geojson_is_verified_and_wins_the_registry_by_content(name: str) -> None:
    data = fixture(name)
    assert probe(data) == VERIFIED
    engine = ProbeEngine(default_registry())
    for filename in (name, "site.json", "notes.txt", "site"):  # the name never decides
        assert engine.probe(BytesReader(data), filename).adapter == "geojson", filename


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"[]",
        b'{"type": "Feature"}',  # no geometry member
        b'{"type": "FeatureCollection"}',
        b'{"type": "Point"}',
        b'{"type": "Polygon", "geometries": []}',
        b'{"type": "lidar", "rate": 10}',
        b'{"features": [], "type": "something"}',
        b'{"coordinates": [1, 2]}',  # no type
        b'["type", "Point", "coordinates"]',
        b"\x00\x01\x02{",
    ],
)
def test_other_content_is_declined(data: bytes) -> None:
    assert probe(data) == 0.0


def test_a_truncated_file_is_still_geojson_by_its_head() -> None:
    assert probe(fixture("truncated.geojson")) == VERIFIED
    assert probe(b'{"type": "FeatureCollection", "features": [{"type": "Fea') == VERIFIED
    assert probe(b"\xef\xbb\xbf" + fixture("bare_point.geojson")) == VERIFIED


def test_a_type_after_a_features_array_longer_than_the_head_is_not_seen() -> None:
    features = b'{"type": "Feature", "geometry": null, "properties": null},' * 3000
    data = (
        b'{"features": ['
        + features
        + b'{"type": "Feature", "geometry": null}], "type": "FeatureCollection"}'
    )
    assert len(data) > PROBE_HEAD_SIZE and probe(data) == 0.0


def test_inspect_reads_the_head_only() -> None:
    data = fixture("av_route_legacy_crs.geojson")
    summary = (
        GeoJsonAdapter()
        .inspect(BytesReader(data), ingest_source(GeoJsonAdapter(), BytesReader(data)).config)
        .summary
    )
    assert summary == {
        "bom": False,
        "crs_member": True,
        "root_type": "FeatureCollection",
        "size": len(data),
    }


# --- A feature collection, cell by cell ----------------------------------------------------------


def test_warehouse_site_features_bounds_and_citations() -> None:
    data = fixture("warehouse_amr_site.geojson")
    output = run(data)
    assert codes(output) == ["crs_defaulted"]
    features = feature_rows(output)
    assert sorted(features) == [0, 1, 2, 3]
    expected = json.loads(data.decode("utf-8"))["features"]
    for index, row in features.items():
        feature = expected[index]
        geometry = feature["geometry"]
        # the row cites the feature's bytes
        assert json_at(data, row.provenance.evidence) == feature
        cells = row.cells
        assert (
            at(data, prov(cells[1]).evidence) == json.dumps(geometry).encode()
            or json_at(data, prov(cells[1]).evidence) == geometry
        )
        assert cells[1] == Known(geometry["type"], prov(cells[1]))
        points = _positions(geometry["coordinates"])
        assert cells[2].value == len(points)
        assert cells[3].value == min(p[0] for p in points)
        assert cells[4].value == min(p[1] for p in points)
        assert cells[5].value == max(p[0] for p in points)
        assert cells[6].value == max(p[1] for p in points)
        assert all(prov(c).assertion_kind is AssertionKind.OBSERVED for c in cells[2:7])
        assert prov(cells[1]).assertion_kind is AssertionKind.STATED
    assert features[1].cells[7].value == 4.5 and features[1].cells[8].value == 4.5
    assert isinstance(features[0].cells[7], NotCovered)  # no position has a third number
    # ids as declared: a string, an integer, none
    assert features[0].cells[0].value == "wh7-outline"
    assert features[1].cells[0].value == 101 and isinstance(features[1].cells[0].value, int)
    assert isinstance(features[2].cells[0], Unknown)


def _positions(coordinates: Any) -> list[list[float]]:
    if coordinates and isinstance(coordinates[0], (int, float)):
        return [coordinates]
    return [p for part in coordinates for p in _positions(part)]


def test_every_cell_of_every_table_resolves_to_its_value() -> None:
    for name in VALID:
        data = fixture(name)
        output = run(data)
        for record in kinds(output, StructuredRecord):
            for cell in record.cells:
                if isinstance(cell, Known | KnownAbsent):
                    evidence = prov(cell).evidence
                    (step,) = evidence.locator
                    assert isinstance(step, ByteRange) and step.offset + step.length <= len(data)
        for row in rows(output, "properties"):
            feature, key, value = row.cells
            text = json_at(data, prov(value).evidence)
            if isinstance(value, Known):
                assert text == value.value
            elif isinstance(value, KnownAbsent):
                assert text is None
            cited = json_at(data, prov(key).evidence)
            if key.value.rsplit("/", 1)[-1].isdigit():  # an array element is cited by its value
                assert cited == text
            else:
                assert cited == _last_name(key.value)
            assert feature.value >= 0


def _last_name(pointer: str) -> str:
    """The member name a pointer ends in (a key cell cites the name's bytes, quotes included)."""
    token = pointer.rsplit("/", 1)[-1]
    return token.replace("~1", "/").replace("~0", "~") if not token.isdigit() else token


def test_properties_are_rows_by_pointer_with_nulls_and_nesting() -> None:
    data = fixture("warehouse_amr_site.geojson")
    output = run(data)
    leaves = {}
    for row in rows(output, "properties"):
        feature, key, value = row.cells
        leaves[(feature.value, key.value)] = value
    assert leaves[(0, "/site_id")].value == "WH-7"
    assert leaves[(0, "/operator/name")].value == "Tuas Logistics"
    assert isinstance(leaves[(0, "/operator/contact")], KnownAbsent)  # JSON null, citing itself
    assert leaves[(1, "/asset_id")].value == "DOCK-01"
    assert leaves[(2, "/name")].value == "Dock 2 · north"  # non-ASCII, exact byte citation
    assert leaves[(3, "/restricted")].value is True
    assert leaves[(3, "/speed_limit_mps")].value == 0.5
    assert all(v.provenance.assertion_kind is AssertionKind.STATED for v in leaves.values())


# --- Sites, assets and identity hints ------------------------------------------------------------


def test_sites_and_assets_carry_declared_ids_as_hints() -> None:
    output = run(fixture("warehouse_amr_site.geojson"))
    (site,) = kinds(output, Site)
    assert by_ns(site) == {"geojson.feature_id": "wh7-outline", "site_id": "WH-7"}
    assert site.name == Known("Warehouse 7", site.name.provenance)
    assert isinstance(site.location, Unknown)  # a polygon: never a centroid
    assets = {by_ns(a)["asset_id"]: a for a in kinds(output, Asset)}
    assert sorted(assets) == ["17", "DOCK-01"]  # an integer id as written, never converted
    dock = assets["DOCK-01"]
    assert by_ns(dock) == {"asset_id": "DOCK-01", "geojson.feature_id": "101", "site_id": "WH-7"}
    assert dock.site == Known(LogicalId("site_id", "WH-7"), dock.site.provenance)
    assert dock.category.value == "charging dock" and dock.name.value == "Dock 1"
    other = assets["17"]
    assert isinstance(other.site, Unknown) and isinstance(other.category, Unknown)
    assert other.name.value == "Dock 2 · north"


def test_a_point_in_a_geographic_crs_is_a_location_and_nothing_else_is() -> None:
    output = run(fixture("warehouse_amr_site.geojson"))
    dock = next(a for a in kinds(output, Asset) if by_ns(a)["asset_id"] == "DOCK-01")
    assert isinstance(dock.location, Known)
    position: Any = dock.location.value
    assert isinstance(position, GeodeticPosition)
    assert (position.longitude, position.latitude) == (103.6004, 1.3504)  # GeoJSON's order
    assert position.height == Known(4.5, prov(position.height))
    assert Any_(position.crs).value == CrsCode("OGC", "CRS84")
    # RFC 7946 states these for its default CRS, citing the root type
    assert prov(position.angle_unit).assertion_kind is AssertionKind.STATED
    other = next(a for a in kinds(output, Asset) if by_ns(a)["asset_id"] == "17")
    assert isinstance(Any_(other.location.value).height, NotCovered)


def test_ids_are_never_resolved_or_merged() -> None:
    feature = (
        b'{"type": "Feature", "id": "same", "geometry": {"type": "Point", "coordinates": [1, 2]},'
        b' "properties": {"asset_id": "A-1", "site_id": "S"}}'
    )
    data = b'{"type": "FeatureCollection", "features": [' + feature + b", " + feature + b"]}"
    output = run(data)
    assets = kinds(output, Asset)
    assert len(assets) == 2 and len({a.id for a in assets}) == 2
    assert len(rows(output, "features")) == 2


def test_unusable_hints_are_findings_not_ids() -> None:
    data = (
        b'{"type": "FeatureCollection", "features": [{"type": "Feature", "id": [1],'
        b' "geometry": null, "properties": {"asset_id": true, "site_id": "S-9"}},'
        b' {"type": "Feature", "geometry": null, "properties": {"name": "no id at all"}}]}'
    )
    output = run(data)
    assert "identity_hint_ignored" in codes(output)
    (site,) = kinds(output, Site)
    assert by_ns(site) == {"site_id": "S-9"}  # the unusable asset_id and id are not kept
    assert not kinds(output, Asset)
    assert isinstance(feature_rows(output)[0].cells[0], Unknown)


def test_a_repeated_name_has_no_reading() -> None:
    data = (
        b'{"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": null,'
        b' "properties": {"site_id": "A", "site_id": "B", "x": 1}}]}'
    )
    output = run(data)
    assert not kinds(output, Site)
    assert "duplicate_member" in codes(output)


# --- The CRS -------------------------------------------------------------------------------------


def test_default_crs_is_the_specifications_and_cited_at_the_root_type() -> None:
    data = fixture("drone_geofence.geojson")
    output = run(data)
    crs = artifact(output).crs
    assert isinstance(crs, Known) and crs.value == CrsCode("OGC", "CRS84")
    assert prov(crs).assertion_kind is AssertionKind.STATED
    assert at(data, prov(crs).evidence) == b'"FeatureCollection"'
    assert isinstance(artifact(output).unit, NotApplicable)
    assert codes(output) == ["crs_defaulted"]


def test_a_legacy_crs_member_is_stated_as_written() -> None:
    data = fixture("av_route_legacy_crs.geojson")
    output = run(data)
    crs = artifact(output).crs
    assert isinstance(crs, Known) and crs.value == CrsCode("OGC", "CRS84")
    assert json_at(data, prov(crs).evidence)["properties"]["name"].endswith("CRS84")
    assert codes(output) == ["crs_legacy"]
    depot = next(s for s in kinds(output, Site))
    assert isinstance(depot.location, Known)
    # the file states a CRS but not its units: they stay Unknown rather than being looked up
    assert isinstance(Any_(depot.location.value).angle_unit, Unknown)


def test_a_projected_crs_is_stated_and_never_range_checked_or_located() -> None:
    output = run(fixture("marine_survey_projected.geojson"))
    assert artifact(output).crs.value == CrsCode("EPSG", "32648")
    assert codes(output) == ["crs_legacy"]  # no coordinate_out_of_range for 366000.0
    assert all(
        isinstance(r.location, Unknown) for r in [*kinds(output, Asset), *kinds(output, Site)]
    )
    assert feature_rows(output)[0].cells[3].value == 366000.0


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("EPSG:4326", ("EPSG", "4326")),
        ("urn:ogc:def:crs:EPSG::3857", ("EPSG", "3857")),
        ("urn:ogc:def:crs:EPSG:6.6:4326", ("EPSG", "4326")),
        ("urn:ogc:def:crs:OGC:1.3:CRS84", ("OGC", "CRS84")),
        ("http://www.opengis.net/def/crs/EPSG/0/32633", ("EPSG", "32633")),
        ("epsg:4326", ("epsg", "4326")),  # verbatim: nothing is case-folded
    ],
)
def test_crs_names_are_read_verbatim(name: str, reason: tuple[str, str]) -> None:
    data = (
        b'{"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "'
        + name.encode()
        + b'"}}, "features": []}'
    )
    assert artifact(run(data)).crs.value == CrsCode(*reason)


def test_the_old_epsg_object_form_is_epsg_n() -> None:
    data = (
        b'{"type": "FeatureCollection", "crs": {"type": "EPSG", "properties": {"code": 4326}},'
        b' "features": []}'
    )
    assert artifact(run(data)).crs.value == CrsCode("EPSG", "4326")


@pytest.mark.parametrize("name", ["crs_link", "crs_null"])
def test_a_crs_nobody_can_read_is_unknown_with_a_finding(name: str) -> None:
    output = run(fixture(f"{name}.geojson"))
    assert isinstance(artifact(output).crs, Unknown)
    assert codes(output) == ["crs_unknown"]
    (found,) = output.findings()
    assert "never followed" in found.message or "no CRS can be assumed" in found.message


@pytest.mark.parametrize("bad", ["not a crs", "urn:ogc:def:crs", "EPSG", "x" * 100 + ":1"])
def test_an_unrecognised_crs_name_is_unknown(bad: str) -> None:
    data = (
        b'{"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "'
        + bad.encode()
        + b'"}}, "features": []}'
    )
    output = run(data)
    assert isinstance(artifact(output).crs, Unknown) and codes(output) == ["crs_unknown"]


def test_two_different_crs_are_ambiguous_each_cited() -> None:
    data = fixture("crs_ambiguous.geojson")
    output = run(data)
    crs = artifact(output).crs
    assert isinstance(crs, Ambiguous)
    assert [c.value for c in crs.candidates] == [CrsCode("EPSG", "4326"), CrsCode("EPSG", "3857")]
    assert [json_at(data, prov(c).evidence)["properties"]["name"] for c in crs.candidates] == [
        "EPSG:4326",
        "EPSG:3857",
    ]
    assert codes(output) == ["crs_ambiguous", "duplicate_member"]


def test_the_same_crs_twice_is_one_crs() -> None:
    crs = b'"crs": {"type": "name", "properties": {"name": "EPSG:4326"}}, '
    data = b'{"type": "FeatureCollection", ' + crs + crs + b'"features": []}'
    output = run(data)
    assert artifact(output).crs.value == CrsCode("EPSG", "4326")
    assert "duplicate_member" in codes(output)


@pytest.mark.parametrize(
    "members",
    [
        b'"crs": null, "crs": {"type": "name", "properties": {"name": "EPSG:4326"}}',
        b'"crs": {"type": "name", "properties": {"name": "EPSG:4326"}}, "crs": null',
        b'"crs": {"type": "name", "properties": {"name": "EPSG:4326"}}, "crs": 5',
        b'"crs": {"type": "link", "properties": {"href": "x"}}, '
        b'"crs": {"type": "name", "properties": {"name": "EPSG:4326"}}',
    ],
)
def test_a_repeated_crs_with_an_unreadable_member_is_unknown_not_stated(members: bytes) -> None:
    output = run(b'{"type": "FeatureCollection", ' + members + b', "features": []}')
    assert isinstance(artifact(output).crs, Unknown)
    assert {"crs_unknown", "duplicate_member"} <= set(codes(output))
    assert "crs_legacy" not in codes(output)


def test_no_crs_member_and_positions_no_geographic_crs_holds_is_unknown_not_wgs84() -> None:
    output = run(fixture("utm_no_crs.geojson"))
    assert isinstance(artifact(output).crs, Unknown)
    assert codes(output) == ["crs_unknown"]
    assert all(isinstance(a.location, Unknown) for a in kinds(output, Asset))
    assert feature_rows(output)[0].cells[3].value == 366500.0  # the numbers stay as written


def test_a_crs_on_a_feature_makes_the_default_unavailable() -> None:
    data = (
        b'{"type": "FeatureCollection", "features": [{"type": "Feature", "crs": {"type": "name",'
        b' "properties": {"name": "EPSG:3857"}}, "geometry": {"type": "Point", "coordinates":'
        b' [1.0, 2.0]}, "properties": {"asset_id": "A"}}]}'
    )
    output = run(data)
    assert isinstance(artifact(output).crs, Unknown)
    assert codes(output) == ["crs_unknown", "nested_crs"]
    assert isinstance(next(iter(kinds(output, Asset))).location, Unknown)


def test_a_crs_member_after_the_features_is_read() -> None:
    data = (
        b'{"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type":'
        b' "Point", "coordinates": [500000.0, 4000000.0]}, "properties": null}], "crs": {"type":'
        b' "name", "properties": {"name": "EPSG:32633"}}}'
    )
    output = run(data)
    assert artifact(output).crs.value == CrsCode("EPSG", "32633")
    assert "coordinate_out_of_range" not in codes(output)


def test_a_file_that_breaks_off_cannot_rule_out_a_crs_member() -> None:
    output = run(fixture("truncated.geojson"))
    assert isinstance(artifact(output).crs, Unknown)
    assert codes(output) == ["crs_unknown", "json_truncated"]
    assert sorted(feature_rows(output)) == [0, 1]  # the whole features before the break survive


# --- Roots other than a collection ---------------------------------------------------------------


def test_a_single_feature_is_row_zero() -> None:
    data = fixture("farm_field_boundary.geojson")
    output = run(data)
    (row,) = rows(output, "features")
    assert row.row == 0 and row.cells[0].value == "field-north-40"
    (site,) = kinds(output, Site)
    assert by_ns(site)["site_id"] == "FARM-N40"
    assert codes(output) == ["crs_defaulted", "ring_winding"]  # a clockwise exterior ring
    assert [r.cells[1].value for r in rows(output, "properties")] == ["/site_id", "/name", "/crop"]


def test_a_bare_geometry_is_one_feature_without_properties() -> None:
    output = run(fixture("bare_point.geojson"))
    (row,) = rows(output, "features")
    assert row.cells[1].value == "Point" and row.cells[3].value == -0.1276
    assert not rows(output, "properties") and not kinds(output, Site)
    assert artifact(output).crs.value == CrsCode("OGC", "CRS84")


def test_a_collection_without_features_has_an_artifact_and_a_finding() -> None:
    output = run(b'{"type": "FeatureCollection", "name": "empty"}')
    assert codes(output) == ["crs_unknown", "features_missing"]
    assert artifact(output).name.value == "empty"
    assert not kinds(output, StructuredTable)


def test_a_non_geojson_object_has_no_records_and_a_finding() -> None:
    output = run(b'{"type": "lidar", "rate": 10}')
    assert codes(output) == ["not_geojson"]
    assert not output.records()


@pytest.mark.parametrize("data", [b"", b"[]", b"   ", b"not json", b"\x00\xff\x00"])
def test_a_non_object_root_has_a_finding_only(data: bytes) -> None:
    output = run(data)
    assert not output.records() and codes(output) == ["not_geojson"]


def test_a_byte_order_mark_is_skipped_and_cited_exactly() -> None:
    body = fixture("drone_geofence.geojson")
    data = b"\xef\xbb\xbf" + body
    output = run(data)
    assert "json_bom" in codes(output)
    row = feature_rows(output)[0]
    assert json_at(data, row.provenance.evidence)["id"] == "fence-1"
    plain = {r.cells[0].value for r in rows(run(body), "features")}
    assert {r.cells[0].value for r in rows(output, "features")} == plain


# --- Validation ----------------------------------------------------------------------------------


def test_each_kind_of_bad_geometry_is_a_finding_and_the_rest_land() -> None:
    data = fixture("bad_geometry.geojson")
    output = run(data)
    assert artifact(output).crs.value == CrsCode("EPSG", "4326")
    assert codes(output) == [
        "coordinate_out_of_range",
        "crs_legacy",
        "geometry_invalid",
        "non_finite_coordinate",
        "ring_not_closed",
        "ring_too_short",
    ]
    features = feature_rows(output)
    assert sorted(features) == list(range(8))
    # a one-number position, an unknown type, no coordinates: bounds are Unknown
    for index in (2, 3, 6):
        assert all(isinstance(c, Unknown) for c in features[index].cells[3:])
    assert features[3].cells[1].value == "Circle"  # declared types are kept as declared
    # out-of-range numbers stay as written, and are not a location
    assert features[4].cells[3].value == 200.0
    asset = kinds(output, Asset)[0]
    assert isinstance(asset.location, Unknown)
    # NaN is out of the bounds, the finite rest remain
    assert (features[5].cells[3].value, features[5].cells[5].value) == (1.0, 4.0)
    assert features[5].cells[2].value == 3
    # a null geometry is declared absent, citing the null
    assert all(isinstance(c, KnownAbsent) for c in features[7].cells[1:])
    assert at(data, prov(features[7].cells[1]).evidence) == b"null"


def test_winding_is_not_judged_without_a_geographic_crs() -> None:
    cw = b"[[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0]]"
    body = (
        b'"features": [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": ['
        + cw
        + b']}, "properties": null}]}'
    )
    stated = (
        b'{"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "%s"}}, '
    )
    assert "ring_winding" in codes(run(stated % b"EPSG:4326" + body))
    assert "ring_winding" not in codes(run(stated % b"EPSG:3857" + body))


def test_the_bad_utf8_feature_has_no_record_and_the_others_land() -> None:
    output = run(fixture("bad_utf8.geojson"))
    assert "invalid_utf8" in codes(output)
    assert sorted(feature_rows(output)) == [0, 2]
    assert sorted(by_ns(s)["site_id"] for s in kinds(output, Site)) == ["S-1", "S-3"]


# --- Hostile input -------------------------------------------------------------------------------


def collection(*features: bytes) -> bytes:
    return b'{"type": "FeatureCollection", "features": [' + b", ".join(features) + b"]}"


GOOD_POINT: Final = (
    b'{"type": "Feature", "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},'
    b' "properties": {"site_id": "OK"}}'
)


@pytest.mark.parametrize(
    ("depth", "expected"),
    [
        (50, {"geometry_invalid"}),  # decoded, but a position is not numbers
        (5_000, {"geometry_invalid", "too_deep"}),  # whichever the interpreter's limit makes it
        (200_000, {"too_deep"}),  # past any limit: skipped by counting brackets
    ],
)
def test_a_coordinate_depth_bomb_is_a_finding_and_the_rest_land(
    depth: int, expected: set[str]
) -> None:
    bomb = b"[" * depth + b"]" * depth
    bad = b'{"type": "Feature", "geometry": {"type": "LineString", "coordinates": ' + bomb
    output = run(collection(bad + b'}, "properties": null}', GOOD_POINT))
    features = feature_rows(output)
    assert 1 in features and features[1].cells[2].value == 1
    found = set(codes(output))
    assert found & expected


def test_a_properties_depth_bomb_is_a_finding() -> None:
    bomb = b"[" * 100_000 + b"]" * 100_000
    bad = b'{"type": "Feature", "geometry": null, "properties": {"a": ' + bomb + b"}}"
    output = run(collection(bad, GOOD_POINT))
    assert "too_deep" in codes(output) and 1 in feature_rows(output)


def test_nested_properties_past_max_depth_are_cut_not_recursed() -> None:
    nested = b'{"a": ' * 40 + b"1" + b"}" * 40
    bad = b'{"type": "Feature", "geometry": null, "properties": ' + nested + b"}"
    output = run(collection(bad))
    assert "too_deep" in codes(output)
    assert len(rows(output, "properties")) == 1  # the cut leaf, kept as an Unknown cell


def test_a_depth_bomb_in_a_root_member_is_a_finding() -> None:
    bomb = b"[" * 100_000 + b"]" * 100_000
    output = run(b'{"type": "FeatureCollection", "x": ' + bomb + b', "features": []}')
    assert "too_deep" in codes(output)
    assert artifact(output)


def test_huge_and_non_finite_numbers_never_raise() -> None:
    huge = b"1" + b"0" * 400
    data = collection(
        b'{"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[1e999, 1],'
        b" [" + huge + b", 2], [NaN, Infinity], [-Infinity, 3], [4, 5]]}, "
        b'"properties": {"a": 1e999, "b": ' + huge + b', "c": NaN}}'
    )
    output = run(data)
    row = feature_rows(output)[0]
    assert row.cells[2].value == 5
    assert (row.cells[3].value, row.cells[5].value) == (4.0, 4.0)  # only [4, 5] is finite
    assert "non_finite_coordinate" in codes(output)
    values = {r.cells[1].value: r.cells[2] for r in rows(output, "properties")}
    assert values["/a"].value is NonFinite.POSITIVE_INFINITY
    assert values["/b"].value == "1" + "0" * 400  # outside int64: its literal text
    assert values["/c"].value is NonFinite.NAN


def test_an_integer_literal_over_the_digit_limit_skips_that_value_not_the_file() -> None:
    digits = b"9" * 5000
    wide = b'{"type": "Feature", "geometry": null, "properties": {"a": ' + digits + b', "b": 2}}'
    point = b'{"type": "Feature", "geometry": {"type": "Point", "coordinates": [%s, 1]}}' % digits
    nested = b'{"type": "Feature", "geometry": null, "properties": {"o": {"n": [' + digits + b"]}}}"
    output = run(collection(GOOD_POINT, wide, point, nested, GOOD_POINT))
    assert len(feature_rows(output)) == 5  # every feature after the long literal is read
    assert "json_syntax" not in codes(output)
    named: list[Any] = []
    for found in output.findings():
        if found.code.endswith("number_too_long"):
            named += list(found.details["features"])  # type: ignore[arg-type]
    assert sorted(named) == [1, 2, 3]  # each feature is named
    crs = artifact(output).crs
    assert isinstance(crs, Known) and crs.value == CrsCode("OGC", "CRS84")  # not Unknown
    values = {(r.cells[0].value, r.cells[1].value): r.cells[2] for r in rows(output, "properties")}
    assert isinstance(values[(1, "/a")], NotCovered)
    assert values[(1, "/b")].value == 2  # the rest of its own properties is read
    assert isinstance(values[(3, "/o/n/0")], NotCovered)
    geometry = feature_rows(output)[2].cells[1:]
    assert all(isinstance(cell, NotCovered) for cell in geometry)


def test_an_integer_literal_over_the_digit_limit_in_the_root_leaves_the_crs_alone() -> None:
    data = (
        b'{"type": "FeatureCollection", "big": ' + b"9" * 5000 + b", "
        b'"crs": {"type": "name", "properties": {"name": "EPSG:3857"}}, "features": ['
        + GOOD_POINT
        + b"]}"
    )
    output = run(data)
    assert artifact(output).crs.value == CrsCode("EPSG", "3857")
    assert "number_too_long" in codes(output)
    assert len(feature_rows(output)) == 1


def test_an_integer_literal_over_the_digit_limit_in_an_id_is_not_covered() -> None:
    data = collection(
        b'{"type": "Feature", "id": ' + b"9" * 5000 + b', "geometry": null, "properties": {}}'
    )
    output = run(data)
    assert isinstance(feature_rows(output)[0].cells[0], NotCovered)
    assert "number_too_long" in codes(output)


def test_millions_of_positions_are_a_budget_and_a_finding() -> None:
    points = b",".join(b"[%d.5, %d.25]" % (i % 179, i % 89) for i in range(300_000))
    big = (
        b'{"type": "Feature", "geometry": {"type": "LineString", "coordinates": ['
        + points
        + b']}, "properties": null}'
    )
    assert len(big) > 3 * 1024 * 1024  # larger than the reader's first window
    output = run(collection(big, GOOD_POINT), max_positions=100_000)
    features = feature_rows(output)
    assert sorted(features) == [0, 1]
    assert isinstance(features[0].cells[2], Unknown) and isinstance(features[0].cells[3], Unknown)
    assert "position_budget" in codes(output)
    whole = run(collection(big, GOOD_POINT))
    assert feature_rows(whole)[0].cells[2].value == 300_000  # within the default budget
    assert feature_rows(whole)[0].cells[5].value == 178.5


def test_a_feature_over_the_cap_stops_the_read_and_says_so() -> None:
    big = b'{"type": "Feature", "geometry": null, "properties": {"k": "' + b"x" * 2_000_000 + b'"}}'
    output = run(collection(GOOD_POINT, big, GOOD_POINT), max_feature_bytes=1_000_000)
    assert sorted(feature_rows(output)) == [0]
    assert "feature_too_large" in codes(output)
    assert isinstance(artifact(output).crs, Unknown)  # not read to the end


def test_the_feature_and_source_limits() -> None:
    many = collection(*[GOOD_POINT] * 30)
    output = run(many, max_features=10)
    assert sorted(feature_rows(output)) == list(range(10)) and "feature_limit" in codes(output)
    output = run(many, max_source_bytes=100)
    assert not output.records() and codes(output) == ["source_too_large"]
    props = b",".join(b'"k%d": %d' % (i, i) for i in range(50))
    output = run(
        collection(b'{"type": "Feature", "geometry": null, "properties": {' + props + b"}}"),
        max_properties=10,
    )
    assert len(rows(output, "properties")) == 10 and "properties_truncated" in codes(output)


def test_a_geometry_collection_nesting_past_max_depth_is_cut() -> None:
    inner = b'{"type": "Point", "coordinates": [1, 2]}'
    for _ in range(40):
        inner = b'{"type": "GeometryCollection", "geometries": [' + inner + b"]}"
    output = run(collection(b'{"type": "Feature", "geometry": ' + inner + b', "properties": null}'))
    assert "too_deep" in codes(output)
    assert isinstance(feature_rows(output)[0].cells[3], Unknown)
    shallow = run(
        collection(
            b'{"type": "Feature", "geometry": {"type": "GeometryCollection", "geometries": ['
            b'{"type": "Point", "coordinates": [1, 2]}, {"type": "LineString", "coordinates":'
            b" [[3, 4], [5, 6]]}]},"
            b' "properties": null}'
        )
    )
    cells = feature_rows(shallow)[0].cells
    assert (cells[1].value, cells[2].value, cells[3].value, cells[5].value) == (
        "GeometryCollection",
        3,
        1.0,
        5.0,
    )


def test_truncation_anywhere_keeps_what_was_whole_and_never_raises() -> None:
    data = fixture("warehouse_amr_site.geojson")
    whole = {r.cells[0].value for r in rows(run(data), "features") if isinstance(r.cells[0], Known)}
    for cut in range(1, len(data), 11):
        output = run(data[:cut])
        for record in kinds(output, StructuredRecord):
            for cell in record.cells:
                if isinstance(cell, Known):
                    assert at(data[:cut], prov(cell).evidence)
        found = {
            r.cells[0].value for r in rows(output, "features") if isinstance(r.cells[0], Known)
        }
        assert found <= whole


def test_malformed_json_is_a_syntax_finding_after_the_last_whole_feature() -> None:
    data = collection(GOOD_POINT, b'{"type": "Feature" "geometry": null}', GOOD_POINT)
    output = run(data)
    assert sorted(feature_rows(output)) == [0] and "json_syntax" in codes(output)


def test_elements_that_are_not_features() -> None:
    data = collection(b"1", b'{"type": "Point", "coordinates": [1, 2]}', b"null", GOOD_POINT)
    output = run(data)
    assert sorted(feature_rows(output)) == [3] and codes(output).count("feature_invalid") == 1


# --- Determinism, chunking and lineage -----------------------------------------------------------


def test_the_same_source_gives_byte_identical_output() -> None:
    for name in VALID:
        assert as_bytes(run(fixture(name))) == as_bytes(run(fixture(name)))


def test_blocks_cut_a_big_collection_without_changing_a_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    features = [
        b'{"type": "Feature", "id": %d, "geometry": {"type": "Point", "coordinates": [%d.5, 1.5]},'
        b' "properties": {"asset_id": "A%d", "n": %d}}' % (i, i % 170, i, i)
        for i in range(4200)
    ]
    data = collection(*features)
    wide = run(data)
    assert len(wide.plan.chunks) == 1 + 3  # 2,048 features a block
    assert sorted(feature_rows(wide)) == list(range(4200))
    assert [r.cells[0].value for r in rows(wide, "features")] == list(range(4200))
    monkeypatch.setattr(geojson, "BLOCK_FEATURES", 7)
    narrow = run(data)
    assert len(narrow.plan.chunks) > len(wide.plan.chunks)
    assert [r.to_json() for r in narrow.records()] == [r.to_json() for r in wide.records()]


def test_a_setting_or_version_change_is_new_lineage() -> None:
    data = fixture("drone_geofence.geojson")
    first, second = run(data), run(data, max_depth=31)
    assert not {r.id for r in first.records()} & {r.id for r in second.records()}
    assert first.config.transform.id != second.config.transform.id


def test_features_found_in_a_window_longer_than_a_megabyte_are_whole() -> None:
    features = [GOOD_POINT.replace(b"OK", b"S%d" % i) for i in range(12_000)]
    data = collection(*features)
    assert len(data) > 1024 * 1024  # the reader's first window
    output = run(data)
    assert len(rows(output, "features")) == 12_000
    assert sorted(by_ns(s)["site_id"] for s in kinds(output, Site))[:2] == ["S0", "S1"]


def test_lone_surrogate_escapes_are_unknown_not_a_crash() -> None:
    data = collection(
        b'{"type": "Feature", "id": "\\ud800", "geometry": null, "properties": {"site_id":'
        b' "\\ud800", "name": "\\ud800x", "k": "\\ud800"}}'
    )
    output = run(data)
    assert not kinds(output, Site)
    assert isinstance(feature_rows(output)[0].cells[0], Unknown)

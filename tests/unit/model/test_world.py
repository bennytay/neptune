"""World and record-context records: what each field may hold, and MVL-69's acceptance (ADR 0020).

The sources are small and real enough to check citations against: a CSV site register with blank
cells, a PDF page's extracted text, a Markdown file. Every cited cell, span and region is resolved
back to the text it names.
"""

import csv
import io
from dataclasses import replace
from typing import Any

import pytest

from neptune.derived.provenance import InferredProvenance
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.provenance import (
    check_evidence_record_id,
    evidence_record_id,
    transform_record,
)
from neptune.model.frames import FrameRef
from neptune.model.ids import LogicalId, RecordId
from neptune.model.kinds import KIND_SINCE
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import (
    NO_HEADER,
    ByteRange,
    EvidenceRef,
    ImageRegion,
    JsonPointer,
    Locator,
    ObjectLocator,
    Page,
    PageRegion,
    Provenance,
    Row,
    RowCell,
    Span,
    VideoFrame,
)
from neptune.model.record import SCHEMA_VERSION, Family, SchemaVersionError
from neptune.model.scalars import NonFinite
from neptune.model.spatial import CrsCode, GeodeticPosition, HeightReference
from neptune.model.time import Duration, Timestamp
from neptune.model.units import unit_from_json
from neptune.model.world import (
    Asset,
    BlockRole,
    Capture,
    DocumentBlock,
    DocumentPage,
    DocumentRecord,
    Image,
    Site,
    SpatialArtifact,
    SpatialCategory,
    StructuredRecord,
    StructuredTable,
    Video,
    asset_from_json,
    document_block_from_json,
    document_record_from_json,
    image_from_json,
    site_from_json,
    spatial_artifact_from_json,
    structured_record_from_json,
    structured_table_from_json,
    video_from_json,
)

REGISTER_BYTES = (
    b"site_id,name,aka,latitude,longitude,defects\r\n"
    b"S-007,North Plant,NP;Plant 3,-33.8651,151.2099,none\r\n"
    b"S-008,Berth 4,,-33.8612,151.2111,\r\n"
    b"S-009,,,,,N/A\r\n"
)
LEGEND_BYTES = b"Defects: N/A means the site has none recorded.\n"
PDF_BYTES = b"%PDF-1.7\n" + bytes(900)
PAGE_TEXT = "Inspection procedure\nClose valve V-12 before entering the pump room."
MARKDOWN_BYTES = b"## Safety\n\nWear a helmet near **Pump 2**.\n"
JPEG_BYTES = b"\xff\xd8\xff\xe1" + bytes(2048)
MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + bytes(4096)
GLTF_BYTES = b"glTF\x02\x00\x00\x00" + bytes(256)
GEOJSON_BYTES = b'{"type":"FeatureCollection","features":[]}'

SOURCES = {
    name: content_id(data)
    for name, data in (
        ("csv", REGISTER_BYTES),
        ("legend", LEGEND_BYTES),
        ("pdf", PDF_BYTES),
        ("markdown", MARKDOWN_BYTES),
        ("jpeg", JPEG_BYTES),
        ("mp4", MP4_BYTES),
        ("gltf", GLTF_BYTES),
        ("geojson", GEOJSON_BYTES),
    )
}
ADAPTERS = {
    name: transform_record(adapter_id=name, adapter_version="1.0.0", config={"header": True})
    for name in SOURCES
}
TRANSFORMS = {transform.id: transform for transform in ADAPTERS.values()}
OBSERVED, STATED = AssertionKind.OBSERVED, AssertionKind.STATED
HEADER = ("site_id", "name", "aka", "latitude", "longitude", "defects")
PARSED = list(csv.reader(io.StringIO(REGISTER_BYTES.decode())))


def cite(adapter: str, *steps: Locator, kind: AssertionKind = OBSERVED) -> Provenance:
    return Provenance(EvidenceRef(SOURCES[adapter], steps), ADAPTERS[adapter].id, kind)


def record_id_of(kind: str, provenance: Provenance) -> RecordId:
    return evidence_record_id(kind, provenance.evidence, TRANSFORMS[provenance.transform])


def cell(row: int, column: int, *steps: Locator) -> Provenance:
    """A register cell, stated: the register asserts things about sites."""
    return cite("csv", RowCell(row, column, HEADER[column]), *steps, kind=STATED)


def clock(adapter: str, *steps: Locator) -> RecordId:
    return record_id_of("timestamp_domain", cite(adapter, *steps))


EXIF_CLOCK = clock("jpeg", ByteRange(4, 200), JsonPointer("/Exif/DateTimeOriginal"))
TRACK_CLOCK = clock("mp4", ByteRange(1024, 512), JsonPointer("/mdhd/timescale"))
DEG, M = Known(unit_from_json("deg")), Known(unit_from_json("m"))


def position(latitude: float, longitude: float) -> GeodeticPosition:
    """A register's latitude and longitude, with nothing declared about their datum or units."""
    return GeodeticPosition(
        latitude=latitude,
        longitude=longitude,
        height=NotCovered(),
        crs=Unknown(),
        angle_unit=Unknown(),
        height_unit=NotApplicable(),
        height_reference=NotApplicable(),
    )


# --- One record of each kind -------------------------------------------------------------------


def register_table(**changes: Any) -> StructuredTable:
    at = cite("csv", ByteRange(0, len(REGISTER_BYTES)), kind=STATED)
    table = StructuredTable(
        id=record_id_of("structured_table", at),
        provenance=at,
        name=NotCovered(),  # a CSV file has no place for a table name
        header=Known(HEADER, cite("csv", Row(0), kind=STATED)),
    )
    return replace(table, **changes)


def register_row(row: int, **changes: Any) -> StructuredRecord:
    """One register row, each cell as declared: blank is Unknown, "none" is the text "none"."""
    at = cite("csv", Row(row), kind=STATED)
    legend = cite("legend", ByteRange(0, len(LEGEND_BYTES)), Span(9, 12), kind=STATED)
    cells: list[Any] = []
    for text in PARSED[row]:
        if not text:
            cells.append(Unknown())
        elif text == "N/A":  # the register's legend defines it as "none recorded"
            cells.append(KnownAbsent(legend))
        else:
            cells.append(Known(text))
    record = StructuredRecord(
        id=record_id_of("structured_record", at),
        provenance=at,
        table=register_table().id,
        row=row,
        cells=tuple(cells),
    )
    return replace(record, **changes)


def north_plant(**changes: Any) -> Site:
    at = cite("csv", Row(1), kind=STATED)
    site = Site(
        id=record_id_of("site", at),
        provenance=at,
        identifiers=(Known(LogicalId("register", "S-007"), cell(1, 0)),),
        name=Known("North Plant", cell(1, 1)),
        # The adapter documents that "aka" separates names with ';': each alias cites its span.
        aliases=(
            Known("NP", cell(1, 2, Span(0, 2))),
            Known("Plant 3", cell(1, 2, Span(3, 10))),
        ),
        parent=NotCovered(),
        # Two cells make one position, so it cites their row; each cell stays citable in the row.
        location=Known(position(-33.8651, 151.2099)),
    )
    return replace(site, **changes)


def pump(**changes: Any) -> Asset:
    at = cite("geojson", ByteRange(0, len(GEOJSON_BYTES)), JsonPointer("/features/0"))
    asset = Asset(
        id=record_id_of("asset", at),
        provenance=at,
        identifiers=(
            Known(LogicalId("cmms", "PUMP-0042"), cite("geojson", JsonPointer("/features/0/id"))),
        ),
        name=Known("Cooling pump 2"),
        aliases=(),
        category=Known("centrifugal pump"),
        site=Known(LogicalId("register", "S-007")),
        parent=Known(LogicalId("cmms", "LOOP-3")),
        location=Known(
            GeodeticPosition(
                latitude=-33.86512,
                longitude=151.20991,
                height=NotCovered(),
                crs=Known(CrsCode("OGC", "CRS84")),  # RFC 7946 fixes it, citing the format
                angle_unit=DEG,
                height_unit=M,
                height_reference=Known(HeightReference.ELLIPSOID),
            )
        ),
    )
    return replace(asset, **changes)


def gltf_mesh(**changes: Any) -> SpatialArtifact:
    at = cite("gltf", ByteRange(0, len(GLTF_BYTES)))
    graph = record_id_of("frame_graph", at)
    artifact = SpatialArtifact(
        id=record_id_of("spatial_artifact", at),
        provenance=at,
        category=SpatialCategory.MESH,
        name=Known("pump_room"),
        unit=M,  # glTF 2.0 defines metres, citing the bytes that make it glTF
        crs=NotCovered(),
        frame=Known(FrameRef("gltf", graph)),
    )
    return replace(artifact, **changes)


def capture(**changes: Any) -> Capture:
    exif = cite("jpeg", ByteRange(4, 200))
    value = Capture(
        time=Known(Timestamp(1_790_762_400, EXIF_CLOCK), exif),
        position=Known(
            replace(position(-33.86515, 151.20985), angle_unit=DEG, crs=Unknown()), exif
        ),
        device_manufacturer=Known("DJI", exif),
        device_model=Known("FC3582", exif),
        device_identifiers=(Known(LogicalId("exif.body_serial", "1581F5FHD23"), exif),),
    )
    return replace(value, **changes)


def photo(**changes: Any) -> Image:
    at = cite("jpeg", ByteRange(0, len(JPEG_BYTES)))
    image = Image(
        id=record_id_of("image", at),
        provenance=at,
        width=4032,
        height=3024,
        encoding="jpeg",
        orientation=Known(6),  # rotate 90° to view; kept, never applied
        capture=capture(),
    )
    return replace(image, **changes)


def video_track(**changes: Any) -> Video:
    at = cite("mp4", ByteRange(1024, 512))
    video = Video(
        id=record_id_of("video", at),
        provenance=at,
        track=0,
        width=3840,
        height=2160,
        encoding="avc1",
        clock=TRACK_CLOCK,
        frame_count=Known(1800),
        duration=Known(Duration(921_600, TRACK_CLOCK)),
        capture=capture(device_identifiers=()),
    )
    return replace(video, **changes)


def procedure(**changes: Any) -> DocumentRecord:
    at = cite("pdf", ByteRange(0, len(PDF_BYTES)))
    document = DocumentRecord(
        id=record_id_of("document_record", at),
        provenance=at,
        format="pdf",
        title=Known("Pump room SOP", cite("pdf", ByteRange(700, 40))),
        pages=(
            DocumentPage(
                label=Known("iv", cite("pdf", ByteRange(760, 30))),
                width=Known(612.0, cite("pdf", Page(0))),
                height=Known(792.0, cite("pdf", Page(0))),
                rotation=Known(90, cite("pdf", Page(0))),
            ),
        ),
    )
    return replace(document, **changes)


def pdf_block(start: int, end: int, position: int, /, **changes: Any) -> DocumentBlock:
    at = cite("pdf", Page(0), Span(start, end))
    block = DocumentBlock(
        id=record_id_of("document_block", at),
        provenance=at,
        document=procedure().id,
        order=position,
        role=Unknown(),  # an untagged PDF declares no roles; a heading guess is derived
        level=Unknown(),
        text=Known(PAGE_TEXT[start:end]),
        region=Known(
            EvidenceRef(SOURCES["pdf"], (Page(0), PageRegion(0, 72.0, 700.0, 300.0, 716.0)))
        ),
    )
    return replace(block, **changes)


def markdown_heading(**changes: Any) -> DocumentBlock:
    text = MARKDOWN_BYTES.decode()
    at = cite("markdown", Span(3, 9))
    document = cite("markdown", ByteRange(0, len(MARKDOWN_BYTES)))
    block = DocumentBlock(
        id=record_id_of("document_block", at),
        provenance=at,
        document=record_id_of("document_record", document),
        order=0,
        role=Known(BlockRole.HEADING),  # CommonMark: '##' opens a level-2 heading
        level=Known(2),
        text=Known(text[3:9]),
        region=NotApplicable(),  # an unpaged document has no page regions
    )
    return replace(block, **changes)


RECORDS: list[tuple[Any, Any]] = [
    (north_plant(), site_from_json),
    (pump(), asset_from_json),
    (gltf_mesh(), spatial_artifact_from_json),
    (photo(), image_from_json),
    (video_track(), video_from_json),
    (procedure(), document_record_from_json),
    (pdf_block(0, 20, 0), document_block_from_json),
    (markdown_heading(), document_block_from_json),
    (register_table(), structured_table_from_json),
    (register_row(1), structured_record_from_json),
    (register_row(3), structured_record_from_json),
]
IDS = [
    "site",
    "asset",
    "mesh",
    "photo",
    "video",
    "document",
    "pdf block",
    "markdown block",
    "table",
    "row",
    "row with a defined none",
]


# --- What makes them records -------------------------------------------------------------------


@pytest.mark.parametrize(("record", "read"), RECORDS, ids=IDS)
def test_world_records_round_trip_byte_identically(record: Any, read: Any) -> None:
    line = canonical_json.dumps(record.to_json())
    assert read(canonical_json.loads(line)) == record
    assert canonical_json.dumps(read(canonical_json.loads(line)).to_json()) == line
    data = canonical_json.loads(line)
    assert isinstance(data, dict)
    assert (data["kind"], data["schema_version"]) == (record.kind, KIND_SINCE[record.kind])
    assert record.family is Family.WORLD
    check_evidence_record_id(record, TRANSFORMS[record.provenance.transform])


@pytest.mark.parametrize(("record", "read"), RECORDS, ids=IDS)
def test_their_json_is_read_strictly(record: Any, read: Any) -> None:
    data = record.to_json()
    with pytest.raises(SchemaVersionError, match="newer"):
        read({**data, "schema_version": SCHEMA_VERSION + 1, "added_later": 1})
    first = sorted(key for key in data if key not in {"id", "kind", "provenance", "schema_version"})
    for broken in (
        {**data, "confidence": 0.9},
        {key: value for key, value in data.items() if key != first[0]},
        {**data, "kind": "machine"},
        {**data, first[0]: None},
    ):
        with pytest.raises(ValueError):
            read(broken)


# --- Acceptance: every value cites its exact cell, span, page region or image region -----------


def test_every_register_cell_cites_its_exact_cell() -> None:
    table = register_table()
    for row in (1, 2, 3):
        record = register_row(row)
        for column, value in enumerate(record.cells):
            evidence = record.cell_evidence(table, column)
            assert evidence.locator == (RowCell(row, column, HEADER[column]),)
            step = evidence.locator[0]
            assert isinstance(step, RowCell)
            if isinstance(value, Known):
                assert PARSED[step.row][step.column] == value.value


def test_a_blank_cell_is_unknown_and_none_is_what_the_source_says() -> None:
    assert register_row(2).cells[5] == Unknown()  # blank: the register could have said
    assert register_row(1).cells[5] == Known("none")  # the text "none", defined by nothing
    absent = register_row(3).cells[5]
    assert isinstance(absent, KnownAbsent)  # "N/A", defined by the legend as none
    assert absent.provenance == cite(
        "legend", ByteRange(0, len(LEGEND_BYTES)), Span(9, 12), kind=STATED
    )
    # The state cites the legend that defines "N/A"; the cell's place comes from its row.
    assert register_row(3).cell_evidence(register_table(), 5).locator == (RowCell(3, 5, "defects"),)
    with pytest.raises(ValueError):
        register_row(2, cells=(Known(""),))  # a blank never becomes a value


def test_site_ids_and_names_cite_their_cells_and_spans() -> None:
    site = north_plant()
    identifier, name = site.identifiers[0], site.name
    assert isinstance(identifier, Known) and isinstance(name, Known)
    assert identifier.provenance == cell(1, 0)
    for alias in site.aliases:
        assert isinstance(alias, Known) and isinstance(alias.provenance, Provenance)
        row_cell, span = alias.provenance.evidence.locator
        assert isinstance(row_cell, RowCell) and isinstance(span, Span)
        assert PARSED[row_cell.row][row_cell.column][span.start : span.end] == alias.value


def test_a_block_is_the_exact_span_it_cites_and_where_it_is_drawn() -> None:
    heading, paragraph = pdf_block(0, 20, 0), pdf_block(21, 69, 1)
    for block in (heading, paragraph):
        page, span = block.provenance.evidence.locator
        assert page == Page(0) and isinstance(span, Span)
        assert PAGE_TEXT[span.start : span.end] == block.text.known_or_raise()
        region = block.region.known_or_raise()
        assert isinstance(region.locator[-1], PageRegion)
    assert paragraph.text == Known("Close valve V-12 before entering the pump room.")
    block = markdown_heading()
    span = block.provenance.evidence.locator[0]
    assert isinstance(span, Span)
    assert MARKDOWN_BYTES.decode()[span.start : span.end] == "Safety"


def test_image_and_video_regions_are_cited_in_the_stored_raster() -> None:
    region = EvidenceRef(SOURCES["jpeg"], (ImageRegion(1200, 800, 1600, 1100),))
    assert region.locator[0] == ImageRegion(1200, 800, 1600, 1100)
    frame = VideoFrame(0, 42, Timestamp(42 * 512, TRACK_CLOCK))
    in_video = EvidenceRef(SOURCES["mp4"], (frame, ImageRegion(0, 0, 640, 360)))
    assert in_video.locator[0] == frame
    assert frame.track == video_track().track and frame.pts.domain_id == video_track().clock


# --- Acceptance: images, geometry and tables keep their own structure --------------------------


def test_images_geometry_and_tables_are_structure_not_text() -> None:
    image = photo().to_json()
    assert (image["width"], image["height"], image["encoding"]) == (4032, 3024, "jpeg")
    assert "text" not in image and "caption" not in image
    mesh = gltf_mesh().to_json()
    assert mesh["category"] == "mesh"
    assert set(mesh) >= {"unit", "crs", "frame"}
    # A spreadsheet's typed cells stay typed; a CSV's text stays text, with nothing inferred.
    typed = register_row(
        1, cells=(Known(3), Known(3.5), Known(True), Known(NonFinite.NAN), Known("3.5"))
    )
    assert structured_record_from_json(typed.to_json()) == typed
    cells = typed.to_json()["cells"]
    assert isinstance(cells, list)
    assert [c["value"] for c in cells] == [3, 3.5, True, {"non_finite": "nan"}, "3.5"]


# --- Sites and assets --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"identifiers": (), "name": Unknown()}, ValueError),  # declares nothing nameable
        ({"identifiers": ()}, None),  # a stated name is enough for a place
        ({"aliases": (Known("Plant 3"), Known("NP"))}, ValueError),  # unsorted
        ({"aliases": (Known("NP"), Known("NP", cell(1, 2)))}, ValueError),
        ({"aliases": (Unknown(),)}, ValueError),
        ({"aliases": (Known(""),)}, ValueError),
        ({"aliases": (Ambiguous((Candidate("NP"), Candidate("N.P."))),)}, None),
        ({"parent": Known("campus")}, ValueError),
        ({"location": Known((-33.8651, 151.2099))}, ValueError),
        ({"provenance": InferredProvenance((cell(1, 0).evidence,), ADAPTERS["csv"].id)}, TypeError),
    ],
)
def test_site_fields_are_typed(change: dict[str, Any], error: type | None) -> None:
    if error is None:
        site = north_plant(**change)
        assert site_from_json(site.to_json()) == site
        return
    with pytest.raises(error):
        north_plant(**change)


def test_an_asset_names_its_site_and_parent_by_declared_ids() -> None:
    asset = pump()
    assert asset.site == Known(LogicalId("register", "S-007"))
    assert asset.parent == Known(LogicalId("cmms", "LOOP-3"))
    for change in ({"site": Known("North Plant")}, {"category": Known("")}):
        with pytest.raises(ValueError):
            pump(**change)


# --- Geometry ----------------------------------------------------------------------------------


def test_geometry_keeps_its_declared_unit_crs_and_frame() -> None:
    at = cite("geojson", ByteRange(0, len(GEOJSON_BYTES)))
    features = SpatialArtifact(
        id=record_id_of("spatial_artifact", at),
        provenance=at,
        category=SpatialCategory.VECTOR_MAP,
        name=Unknown(),
        unit=NotApplicable(),  # degrees for longitude and latitude, metres for height
        crs=Known(CrsCode("OGC", "CRS84")),
        frame=NotApplicable(),
    )
    assert spatial_artifact_from_json(features.to_json()) == features
    object_in_mesh = EvidenceRef(SOURCES["gltf"], (ByteRange(0, 64), ObjectLocator("valve_V12")))
    assert object_in_mesh.locator[-1] == ObjectLocator("valve_V12")


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"unit": Known(unit_from_json("s"))}, ValueError),  # coordinates are lengths or angles
        ({"unit": DEG}, None),
        ({"category": "mesh"}, TypeError),
        ({"crs": Known("EPSG:4326")}, ValueError),
        ({"frame": Known("map")}, ValueError),
    ],
)
def test_spatial_artifact_fields_are_typed(change: dict[str, Any], error: type | None) -> None:
    if error is None:
        artifact = gltf_mesh(**change)
        assert spatial_artifact_from_json(artifact.to_json()) == artifact
        return
    with pytest.raises(error):
        gltf_mesh(**change)


# --- Images and video --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"width": 0}, ValueError),
        ({"height": 3024.0}, TypeError),
        ({"width": True}, TypeError),
        ({"encoding": ""}, ValueError),
        ({"orientation": Known(9)}, ValueError),
        ({"orientation": Known(True)}, ValueError),
        ({"orientation": NotCovered()}, None),  # a PNG has no EXIF orientation
    ],
)
def test_image_fields_are_typed(change: dict[str, Any], error: type | None) -> None:
    if error is None:
        image = photo(**change)
        assert image_from_json(image.to_json()) == image
        return
    with pytest.raises(error):
        photo(**change)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"duration": Known(Duration(10, EXIF_CLOCK))}, ValueError),  # not the track's clock
        ({"frame_count": Known(-1)}, ValueError),
        ({"track": -1}, ValueError),
        ({"clock": "mdhd"}, ValueError),
        ({"frame_count": Unknown(), "duration": Unknown()}, None),
    ],
)
def test_video_fields_are_typed(change: dict[str, Any], error: type | None) -> None:
    if error is None:
        video = video_track(**change)
        assert video_from_json(video.to_json()) == video
        return
    with pytest.raises(error):
        video_track(**change)


def test_capture_fields_are_typed() -> None:
    with pytest.raises(ValueError):
        capture(device_identifiers=(Known("1581F5FHD23"),))
    with pytest.raises(ValueError):
        capture(time=Known(1_790_762_400))  # ticks mean nothing without their clock
    assert capture().device_identifiers[0] == Known(
        LogicalId("exif.body_serial", "1581F5FHD23"), cite("jpeg", ByteRange(4, 200))
    )


# --- Documents ---------------------------------------------------------------------------------


def test_an_unpaged_document_has_no_pages() -> None:
    at = cite("markdown", ByteRange(0, len(MARKDOWN_BYTES)))
    notes = DocumentRecord(
        id=record_id_of("document_record", at),
        provenance=at,
        format="markdown",
        title=Unknown(),
        pages=(),
    )
    assert document_record_from_json(notes.to_json()) == notes
    assert markdown_heading().document == notes.id


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"role": Known("heading")}, ValueError),  # the enum, not its text
        ({"level": Known(0)}, ValueError),  # depths start at 1
        ({"text": Known("")}, ValueError),
        ({"text": NotApplicable(), "role": Known(BlockRole.FIGURE)}, None),
        ({"region": Known(PageRegion(0, 1.0, 1.0, 2.0, 2.0))}, ValueError),  # a region is cited
        ({"order": -1}, ValueError),
        ({"document": "sop.pdf"}, ValueError),
    ],
)
def test_document_block_fields_are_typed(change: dict[str, Any], error: type | None) -> None:
    if error is None:
        block = pdf_block(0, 20, 0, **change)
        assert document_block_from_json(block.to_json()) == block
        return
    with pytest.raises(error):
        pdf_block(0, 20, 0, **change)


@pytest.mark.parametrize(
    ("page", "error"),
    [
        ({"width": Known(612)}, ValueError),  # 612 and 612.0 are different canonical JSON
        ({"rotation": Known(90.0)}, ValueError),
        ({"rotation": Known(True)}, ValueError),
        ({"label": Known("")}, ValueError),
        ({"label": NotCovered()}, None),
    ],
)
def test_document_pages_are_typed(page: dict[str, Any], error: type | None) -> None:
    base = procedure().pages[0]
    if error is None:
        document = procedure(pages=(replace(base, **page),))
        assert document_record_from_json(document.to_json()) == document
        return
    with pytest.raises(error):
        replace(base, **page)


# --- Tables ------------------------------------------------------------------------------------


def test_a_table_without_a_header_row_cites_cells_without_names() -> None:
    table = register_table(header=NotApplicable())
    record = register_row(1, table=table.id)
    assert record.cell_evidence(table, 0).locator == (RowCell(1, 0, NO_HEADER),)
    assert register_table(header=Unknown()).column_name(0) is NO_HEADER
    assert register_table().column_name(9) is NO_HEADER  # a cell past the header
    with pytest.raises(ValueError, match="no cell 6"):
        record.cell_evidence(table, 6)
    other = cite("csv", ByteRange(0, 44), kind=STATED)
    with pytest.raises(ValueError, match="not in table"):
        record.cell_evidence(replace(table, id=record_id_of("structured_table", other)), 0)


def test_a_row_cited_another_way_gives_each_cell_its_own_provenance() -> None:
    at = cite("geojson", JsonPointer("/features/0/properties"))
    base = dict(id=record_id_of("structured_record", at), provenance=at)
    inherited = (Known("PUMP-0042"),)
    with pytest.raises(ValueError, match="own provenance"):
        register_row(1, cells=inherited, **base)
    own = (Known("PUMP-0042", cite("geojson", JsonPointer("/features/0/properties/tag"))),)
    record = register_row(1, cells=own, **base)
    assert record.cell_evidence(register_table(), 0).locator == (
        JsonPointer("/features/0/properties/tag"),
    )


@pytest.mark.parametrize(
    "cells",
    [
        lambda: (Known(float("inf")),),  # write NonFinite.POSITIVE_INFINITY
        lambda: (Known((1, 2)),),
        lambda: (Known(b"S-007"),),
        lambda: [Known("S-007")],
    ],
)
def test_cells_hold_text_ints_bools_or_reals(cells: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        register_row(1, cells=cells())


@pytest.mark.parametrize(
    "header",
    [Known(()), Known(("site_id", 7)), Known(("site_id", "\ud800")), Known("site_id")],
)
def test_headers_are_verbatim_cells(header: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        register_table(header=header)


def test_a_blank_header_cell_is_kept_as_blank() -> None:
    table = register_table(header=Known(("site_id", "")))
    assert table.column_name(1) == ""
    assert structured_table_from_json(table.to_json()) == table


def test_a_row_resolves_through_its_table_only() -> None:
    record = register_row(2)
    assert record.cell_evidence(register_table(), 1).locator == (RowCell(2, 1, "name"),)

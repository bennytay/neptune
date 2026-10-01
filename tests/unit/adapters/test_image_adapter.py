"""The image adapter on real files: what it records, how it cites it, and that it is repeatable.

The fixtures are written by ``tests/fixtures/image/make_images.py`` for robots of every kind (a pipe
crawler, a warehouse AMR, an ROV, a field rover, a humanoid, a quadruped, a manipulator's wrist).
Agreement with an independent reader is in ``test_image_fixtures.py``; hostile input is in
``test_image_hostile.py``.
"""

import calendar
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.contract import PROBE_HEAD_SIZE, ProbeHints
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.image import DESCRIPTOR, ImageAdapter
from neptune.discovery.reader import BytesReader
from neptune.identity.canonical_json import dumps
from neptune.model.finding import Severity
from neptune.model.knowledge import AssertionKind, Known, NotCovered, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef, ImageRegion
from neptune.model.reference import TimestampDomain
from neptune.model.time import Timescale
from neptune.model.units import unit_from_text
from neptune.model.world import Image, StructuredRecord, StructuredTable

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "image"


def data_of(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(data: bytes | str, **config: Any) -> SourceOutput:
    raw = data_of(data) if isinstance(data, str) else data
    return ingest_source(ImageAdapter(), BytesReader(raw), config)


def images(output: SourceOutput) -> list[Image]:
    return sorted(
        (r for r in output.records() if isinstance(r, Image)),
        key=lambda image: image.provenance.evidence.locator[0].offset,  # type: ignore[union-attr]
    )


def codes(output: SourceOutput) -> set[str]:
    return {finding.code for finding in output.findings()}


def known(state: Any) -> Any:
    assert isinstance(state, Known), state
    return state.value


# --- What each format yields ------------------------------------------

EXPECTED: Final = {
    "crawler_inspection.jpg": [("jpeg", 64, 48)],
    "crawler_inspection": [("jpeg", 64, 48)],
    "amr_dock.png": [("png", 32, 24)],
    "rov_survey.tif": [("tiff", 16, 12), ("tiff", 8, 6)],
    "rover_raw.dng": [("dng", 16, 12), ("dng", 32, 24)],
    "survey_tile.tif": [("tiff", 8, 8)],
    "humanoid_headcam.webp": [("webp", 1, 1)],
    "quadruped_lossy.webp": [("webp", 1, 1)],
    "floor_map.bmp": [("bmp", 8, 6)],  # stored bottom-up (height -6): rows still count from the top
    "legacy_cam.bmp": [("bmp", 4, 3)],
    "wrist_depth.pgm": [("pgm", 16, 12)],
    "thermal.ppm": [("ppm", 8, 6)],
    "gripper_mask.pbm": [("pbm", 8, 4)],
    "gripper.pam": [("pam", 4, 3)],
}


@pytest.mark.parametrize(("name", "expected"), sorted(EXPECTED.items()))
def test_each_stored_raster_is_one_image_with_its_declared_size(
    name: str, expected: list[tuple[str, int, int]]
) -> None:
    output = run(name)
    assert [(i.encoding, i.width, i.height) for i in images(output)] == expected
    assert codes(output) <= {"image.value_not_copied"}  # a MakerNote is cited, not copied


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_record_cites_this_source_and_bytes_inside_it(name: str) -> None:
    reader = BytesReader(data_of(name))
    output = ingest_source(ImageAdapter(), reader, {})
    for record in output.records():
        evidence = record.provenance.evidence
        assert evidence.source == reader.content_id
        first = evidence.locator[0]
        assert isinstance(first, ByteRange)
        assert first.offset + first.length <= reader.size


def test_a_tiff_image_cites_its_ifd_and_any_other_image_its_whole_file() -> None:
    whole = ByteRange(0, len(data_of("amr_dock.png")))
    (png,) = images(run("amr_dock.png"))
    assert png.provenance.evidence.locator == (whole,)
    first, second = images(run("rov_survey.tif"))
    assert first.provenance.evidence.locator != second.provenance.evidence.locator
    assert all(
        isinstance(s, ByteRange) and s.length < len(data_of("rov_survey.tif"))
        for s in (first.provenance.evidence.locator[0], second.provenance.evidence.locator[0])
    )


def test_a_region_is_the_images_citation_then_an_image_region_of_the_stored_raster() -> None:
    for name in ("crawler_inspection.jpg", "rover_raw.dng", "floor_map.bmp"):
        for image in images(run(name)):
            evidence = image.provenance.evidence
            region = ImageRegion(0, 0, image.width // 2, image.height)
            ref = EvidenceRef(evidence.source, (*evidence.locator, region))
            assert ref.locator[:-1] == evidence.locator
            assert ref.locator[-1] == region
            full = ImageRegion(0, 0, image.width, image.height)
            assert EvidenceRef(evidence.source, (*evidence.locator, full)) != ref


# --- Declared metadata, as declared ------------------------------------------


def test_exif_orientation_is_kept_and_never_applied() -> None:
    (image,) = images(run("crawler_inspection.jpg"))
    assert known(image.orientation) == 6  # rotate 90 clockwise to display
    assert (image.width, image.height) == (64, 48)  # the stored raster, not the displayed one


def test_a_capture_time_with_a_stated_offset_is_an_exact_instant() -> None:
    (image,) = images(run("crawler_inspection.jpg"))
    stamp = known(image.capture.time)
    # 2026:09:14 10:21:07.042 at +10:00 is 00:21:07.042 UTC: the offset the file states, to ms.
    assert stamp.ticks == (calendar.timegm((2026, 9, 14, 0, 21, 7)) * 1000) + 42
    (domain,) = [
        r for r in run("crawler_inspection.jpg").records() if isinstance(r, TimestampDomain)
    ]
    assert stamp.domain_id == domain.id
    assert known(domain.timescale) is Timescale.POSIX
    assert domain.field == "DateTimeOriginal"


def test_a_capture_time_with_no_zone_stays_civil_and_its_clock_unknown() -> None:
    (domain,) = [r for r in run("rover_raw.dng").records() if isinstance(r, TimestampDomain)]
    assert isinstance(domain.timescale, Unknown)  # no UTC conversion, no assumed zone
    for image in images(run("rover_raw.dng")):
        # 2026:05:19 07:44:10 counted as seconds of its own civil clock
        assert known(image.capture.time).ticks == calendar.timegm((2026, 5, 19, 7, 44, 10))


def test_gps_position_and_device_identity_are_known_with_their_units() -> None:
    (image,) = images(run("crawler_inspection.jpg"))
    position = known(image.capture.position)
    assert position.latitude == pytest.approx(-(23 + 50 / 60 + 26.04 / 3600))
    assert position.longitude == pytest.approx(151 + 15 / 60 + 33.12 / 3600)
    assert known(position.angle_unit) == known(unit_from_text("deg"))
    assert known(position.height_unit) == known(unit_from_text("m"))
    assert isinstance(position.crs, Unknown)  # GPSMapDatum is text; no CRS is assumed
    assert known(image.capture.device_manufacturer) == "Ridgeback Robotics"
    assert known(image.capture.device_model) == "PipeCrawler C2 camera"
    (identifier,) = image.capture.device_identifiers
    serial = known(identifier)
    assert (serial.namespace, serial.value) == ("exif.body_serial", "RC2-00417")


def test_a_format_with_no_place_for_metadata_is_not_covered_and_a_bare_raster_unknown() -> None:
    for name in ("floor_map.bmp", "wrist_depth.pgm", "gripper.pam"):
        (image,) = images(run(name))
        assert isinstance(image.capture.time, NotCovered)
        assert isinstance(image.orientation, NotCovered)
    for name in ("quadruped_lossy.webp", "survey_tile.tif"):
        (image,) = images(run(name))
        assert isinstance(image.capture.time, Unknown)  # the format could say; this file does not
        assert isinstance(image.orientation, Unknown)


def test_a_dng_records_its_preview_and_its_raw_raster_with_the_cameras_serial() -> None:
    preview, raw = images(run("rover_raw.dng"))
    assert (preview.width, preview.height, raw.width, raw.height) == (16, 12, 32, 24)
    for image in (preview, raw):
        (serial,) = image.capture.device_identifiers
        assert known(serial).namespace == "dng.camera_serial"
        assert known(serial).value == "AGR-7731"


def test_the_color_profile_is_a_cited_table_of_the_profiles_own_fields() -> None:
    output = run("amr_dock.png")
    names = {known(t.name): t for t in output.records() if isinstance(t, StructuredTable)}
    assert {"IHDR", "ICC header", "ICC tags", "XMP", "IFD0", "Exif"} <= set(names)
    header = names["ICC header"]
    assert known(header.header)[:2] == ("size", "cmm")
    (row,) = [
        r for r in output.records() if isinstance(r, StructuredRecord) and r.table == header.id
    ]
    assert known(row.cells[0]) == 352  # the profile's own size field
    # the profile was inflated from iCCP: its citation passes through the payload step
    assert any(not isinstance(step, ByteRange) for step in header.provenance.evidence.locator)


def test_netpbm_headers_are_read_as_the_specification_names_them() -> None:
    def row(name: str, table: str) -> dict[str, Any]:
        output = run(name)
        (t,) = [
            t for t in output.records() if isinstance(t, StructuredTable) and known(t.name) == table
        ]
        (r,) = [x for x in output.records() if isinstance(x, StructuredRecord) and x.table == t.id]
        return dict(zip(known(t.header), (known(c) for c in r.cells), strict=True))

    assert row("gripper.pam", "PNM header") == {
        "magic": "P7", "width": 4, "height": 3, "depth": 4, "maxval": 255, "tupltype": "RGB_ALPHA",
    }  # fmt: skip
    pgm = row("wrist_depth.pgm", "PNM header")
    assert (pgm["width"], pgm["height"], pgm["maxval"]) == (16, 12, 65535)


# --- The adapter's own contract ------------------------------------------


def test_each_source_is_one_chunk_and_inspect_summarises_without_records() -> None:
    adapter, reader = ImageAdapter(), BytesReader(data_of("rov_survey.tif"))
    output = ingest_source(adapter, reader, {})
    assert len(output.plan.chunks) == 1
    summary = adapter.inspect(reader, output.config).summary
    listed = summary["images"]
    assert isinstance(listed, list)
    assert sorted(listed, key=str) == sorted(
        [
            {"encoding": "tiff", "height": 6, "width": 8},
            {"encoding": "tiff", "height": 12, "width": 16},
        ],
        key=str,
    )
    assert summary["size"] == reader.size


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_probe_recognises_the_bytes_whatever_the_name(name: str) -> None:
    data = data_of(name)
    result = ImageAdapter().probe(data[:PROBE_HEAD_SIZE], ProbeHints("", len(data)))
    assert result.confidence > 0.0, name


def test_probe_declines_text_and_other_binary() -> None:
    adapter = ImageAdapter()
    for head in (b"hello, robot\n", b"\x7fELF" + bytes(60), b"", b"\x89PNG"):
        assert adapter.probe(head, ProbeHints("x", len(head))).confidence == 0.0


def test_every_finding_code_the_adapter_makes_is_documented_with_its_severity() -> None:
    documented = {d.name for d in DESCRIPTOR.finding_codes}
    for name in sorted(EXPECTED):
        assert codes(run(name)) <= documented
    for name in ("truncated.jpg", "bomb.png", "empty.png", "exif_loop.jpg", "zlib_bomb.png"):
        output = run(name)
        assert codes(output) <= documented
        assert all(f.severity in set(Severity) for f in output.findings())


# --- Determinism ------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_the_same_bytes_give_byte_identical_records_and_findings(name: str) -> None:
    def dump(output: SourceOutput) -> bytes:
        return dumps([r.to_json() for r in output.package_records()])

    assert dump(run(name)) == dump(run(name))
    assert dump(run(name)) == dump(run(data_of(name)))  # the name plays no part


def test_a_changed_config_value_is_a_new_transform_and_new_ids_for_the_same_bytes() -> None:
    first, second = run("amr_dock.png"), run("amr_dock.png", max_value_bytes=64)
    assert first.config.transform.id != second.config.transform.id
    assert {r.id for r in first.records()}.isdisjoint({r.id for r in second.records()})


def test_one_changed_byte_changes_the_source_and_every_citing_record() -> None:
    data = bytearray(data_of("wrist_depth.pgm"))
    data[-1] ^= 0xFF  # a pixel: nothing the adapter reads
    first, second = run("wrist_depth.pgm"), run(bytes(data))
    assert {r.id for r in first.records()}.isdisjoint({r.id for r in second.records()})


# --- Stated and observed ----------------------------------------------


def kinds_of(name: str) -> dict[str, set[AssertionKind]]:
    """Each table's name to the assertion kinds of the table and of all its rows."""
    output = run(name)
    records = output.records()
    found: dict[str, set[AssertionKind]] = {}
    for table in (r for r in records if isinstance(r, StructuredTable)):
        rows = [r for r in records if isinstance(r, StructuredRecord) and r.table == table.id]
        assert rows
        kinds = {table.provenance.assertion_kind, *(r.provenance.assertion_kind for r in rows)}
        found.setdefault(str(known(table.name)), set()).update(kinds)
    return found


STATED: Final = {AssertionKind.STATED}
OBSERVED: Final = {AssertionKind.OBSERVED}


def test_what_a_camera_or_writer_declares_is_stated_and_what_was_measured_is_observed() -> None:
    jpeg = kinds_of("crawler_inspection.jpg")
    for declared in ("IFD0", "IFD1", "Exif", "GPS", "Interop", "XMP", "ICC header", "ICC tags",
                     "JFIF", "COM"):  # fmt: skip
        assert jpeg[declared] == STATED, declared
    for measured in ("SOF0", "SOF0 components"):
        assert jpeg[measured] == OBSERVED, measured
    png = kinds_of("amr_dock.png")
    for declared in ("tEXt", "zTXt", "iTXt", "eXIf", "iCCP", "gAMA", "cHRM", "pHYs", "sRGB",
                     "tIME", "IFD0"):  # fmt: skip
        if declared in png:
            assert png[declared] == STATED, declared
    assert png["IHDR"] == OBSERVED
    assert kinds_of("wrist_depth.pgm")["PNM header"] == OBSERVED
    bmp = kinds_of("floor_map.bmp")
    assert bmp["BITMAPFILEHEADER"] == OBSERVED
    assert bmp["BITMAPV5HEADER"] == STATED  # density, colour endpoints and intent are declared
    assert kinds_of("legacy_cam.bmp")["BITMAPINFOHEADER"] == STATED
    assert kinds_of("floor_map.bmp")["ICC header"] == STATED
    assert kinds_of("humanoid_headcam.webp")["VP8X"] == OBSERVED
    assert kinds_of("rover_raw.dng")["IFD0"] == STATED


def test_an_images_geometry_is_observed_and_its_capture_is_stated() -> None:
    output = run("crawler_inspection.jpg")
    (image,) = images(output)
    assert image.provenance.assertion_kind is AssertionKind.OBSERVED
    capture = image.capture
    for state in (
        capture.time,
        capture.position,
        capture.device_manufacturer,
        capture.device_model,
        image.orientation,
        *capture.device_identifiers,
    ):
        assert isinstance(state, Known)
        assert state.provenance.assertion_kind is AssertionKind.STATED  # type: ignore[union-attr]
    position = known(capture.position)
    for part in (position.height, position.angle_unit, position.height_unit):
        assert part.provenance.assertion_kind is AssertionKind.STATED
    (domain,) = [r for r in output.records() if isinstance(r, TimestampDomain)]
    assert domain.provenance.assertion_kind is AssertionKind.STATED

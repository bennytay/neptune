"""Hostile and damaged images: truncation, lying sizes, bombs, bad offsets and loops, limits.

Every case is a finding, never an exception, and the work done is bounded by the config's limits.
``ingest_source`` runs the contract's checks on every output, so each run here also proves that no
record or finding is repeated and that every citation is inside the source.
"""

import importlib.util
import random
import struct
import time
import tracemalloc
import zlib
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.image import ImageAdapter
from neptune.discovery.reader import BytesReader
from neptune.model.finding import FindingCategory, Severity
from neptune.model.knowledge import Known, NotCovered
from neptune.model.world import Image, StructuredRecord, StructuredTable

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "image"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_images", FIXTURES / "make_images.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR: Final = _generator()
VALID: Final = (
    "crawler_inspection.jpg", "amr_dock.png", "rov_survey.tif", "rover_raw.dng",
    "survey_tile.tif", "humanoid_headcam.webp", "floor_map.bmp", "legacy_cam.bmp",
    "wrist_depth.pgm", "thermal.ppm", "gripper_mask.pbm", "gripper.pam",
)  # fmt: skip


def known(state: Any) -> Any:
    assert isinstance(state, Known), state
    return state.value


def data_of(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(data: bytes | str, **config: Any) -> SourceOutput:
    raw = data_of(data) if isinstance(data, str) else data
    return ingest_source(ImageAdapter(), BytesReader(raw), config)


def codes(output: SourceOutput) -> set[str]:
    return {finding.code for finding in output.findings()}


def images(output: SourceOutput) -> list[Image]:
    return [r for r in output.records() if isinstance(r, Image)]


def png_with(data: bytes, width: int, height: int) -> bytes:
    """``data`` with its IHDR rewritten (CRC fixed): the file now lies about its size."""
    out = bytearray(data)
    out[16:24] = struct.pack(">II", width, height)
    out[29:33] = struct.pack(">I", zlib.crc32(bytes(out[12:29])))
    return bytes(out)


# --- The damaged-file corpus ------------------------------------------

CORPUS: Final = {
    # name: (findings it must make, images it still yields)
    "truncated.jpg": ({"image.truncated"}, 1),
    "truncated.png": ({"image.truncated"}, 1),
    "truncated.webp": ({"image.truncated"}, 1),
    "truncated.bmp": ({"image.raster_truncated"}, 1),
    "truncated.tif": ({"image.truncated", "image.bad_offset"}, 1),
    "bad_crc.png": ({"image.crc_mismatch"}, 1),
    "bomb.png": ({"image.pixel_limit", "image.raster_truncated"}, 1),
    "zlib_bomb.png": (
        {"image.value_not_copied"},
        1,
    ),  # a text cell inflates to max_value_bytes only
    "exif_loop.jpg": ({"image.ifd_loop", "image.bad_offset"}, 1),
    "bad_gps.jpg": ({"image.value_unreadable"}, 1),
    "xmp_bomb.jpg": ({"image.xmp_unreadable"}, 1),
    "subifd_cycle.tif": ({"image.ifd_loop"}, 2),
    "wrong_first.png": ({"image.unreadable"}, 0),
    "empty.png": ({"image.unreadable"}, 0),
    "not_a_bitmap.bmp": ({"image.unreadable"}, 0),
}


@pytest.mark.parametrize(("name", "expected"), sorted(CORPUS.items()))
def test_a_damaged_file_gives_findings_and_whatever_image_it_still_holds(
    name: str, expected: tuple[set[str], int]
) -> None:
    wanted, count = expected
    output = run(name)
    assert wanted <= codes(output), codes(output)
    assert len(images(output)) == count


def test_one_corrupt_image_does_not_hide_the_good_ones_in_the_same_source() -> None:
    output = run("subifd_cycle.tif")
    assert len(images(output)) == 2 and "image.ifd_loop" in codes(output)


def test_an_unreadable_image_is_an_error_and_partial_damage_a_warning() -> None:
    (error,) = run("empty.png").findings()
    assert (error.severity, error.category) == (Severity.ERROR, FindingCategory.CORRUPT)
    assert {f.severity for f in run("truncated.jpg").findings() if f.code == "image.truncated"} == {
        Severity.WARNING
    }


# --- Truncation at every length, and mutation ------------------------------------------


@pytest.mark.parametrize("name", VALID)
def test_a_file_cut_anywhere_never_raises_and_never_cites_past_its_end(name: str) -> None:
    data = data_of(name)
    step = max(1, len(data) // 150)
    cuts = [*range(0, min(len(data), 160)), *range(160, len(data), step)]
    for cut in cuts:
        output = run(data[:cut])
        for image in images(output):
            assert image.width >= 1 and image.height >= 1
        if cut == 0:
            assert codes(output) == {"image.unreadable"}


@pytest.mark.parametrize("name", VALID)
def test_bytes_overwritten_at_random_never_raise(name: str) -> None:
    data = data_of(name)
    rng = random.Random(name)  # fixed per file: the same mutations on every run
    for _ in range(60):
        mutated = bytearray(data)
        for _ in range(rng.choice((1, 2, 4, 16))):
            mutated[rng.randrange(len(mutated))] = rng.choice(
                (0x00, 0xFF, 0x7F, rng.randrange(256))
            )
        run(bytes(mutated))


@pytest.mark.parametrize("name", VALID)
def test_a_header_followed_by_noise_never_raises(name: str) -> None:
    data = data_of(name)
    rng = random.Random(f"noise {name}")
    for length in (4, 12, 40):
        run(data[:length] + rng.randbytes(300))


# --- Lying dimensions ------------------------------------------


def test_a_jpeg_frame_header_of_65535_by_65535_is_recorded_with_a_pixel_limit_finding() -> None:
    data = bytearray(data_of("crawler_inspection.jpg"))
    sof = data.index(b"\xff\xc0")
    data[sof + 5 : sof + 9] = struct.pack(">HH", 65535, 65535)
    output = run(bytes(data))
    (image,) = images(output)
    assert (image.width, image.height) == (65535, 65535)  # as declared, never decoded
    (finding,) = [f for f in output.findings() if f.code == "image.pixel_limit"]
    assert finding.details["width"] == 65535 and image.id in finding.records


def test_a_jpeg_of_zero_pixels_is_unreadable_and_a_png_of_zero_pixels_too() -> None:
    data = bytearray(data_of("crawler_inspection.jpg"))
    sof = data.index(b"\xff\xc0")
    data[sof + 5 : sof + 9] = bytes(4)
    assert "image.unreadable" in codes(run(bytes(data)))
    png = data_of("amr_dock.png")
    for width, height in ((0, 24), (32, 0)):
        output = run(png_with(png, width, height))
        assert codes(output) == {"image.unreadable"} and not images(output)


def test_a_png_larger_than_its_data_could_hold_is_a_raster_finding_not_an_allocation() -> None:
    lying = png_with(data_of("amr_dock.png"), 2**31 - 1, 2**31 - 1)  # the format's maximum
    output = run(lying)
    assert {"image.pixel_limit", "image.raster_truncated"} <= codes(output)
    assert len(images(output)) == 1
    assert "image.unreadable" in codes(run(png_with(data_of("amr_dock.png"), 2**31, 24)))


def test_a_png_that_lies_small_is_still_a_valid_png_to_the_adapter() -> None:
    (image,) = images(run(png_with(data_of("amr_dock.png"), 64, 48)))
    assert (image.width, image.height) == (64, 48)  # as declared; no decoder is here to disagree


def test_a_bmp_and_a_netpbm_header_claiming_a_huge_raster_over_a_tiny_file() -> None:
    bmp = bytearray(data_of("legacy_cam.bmp"))
    bmp[18:26] = struct.pack("<ii", 100_000, 100_000)
    assert {"image.pixel_limit", "image.raster_truncated"} <= codes(run(bytes(bmp)))
    assert {"image.pixel_limit", "image.raster_truncated"} <= codes(
        run(b"P5\n100000 100000\n255\nabc")
    )
    bmp[18:26] = struct.pack("<ii", -(2**31), 3)
    assert codes(run(bytes(bmp))) == {"image.unreadable"}
    assert codes(run(b"P5\n0 5\n255\n")) == {"image.unreadable"}


def test_a_riff_size_that_lies_is_a_truncation_or_an_unreadable_file() -> None:
    webp = bytearray(data_of("humanoid_headcam.webp"))
    webp[4:8] = struct.pack("<I", len(webp) + 100)
    output = run(bytes(webp))
    assert "image.truncated" in codes(output) and len(images(output)) == 1
    webp[4:8] = struct.pack("<I", 4)
    assert "image.unreadable" in codes(run(bytes(webp)))


# --- Decompression bombs ------------------------------------------


def test_a_zlib_bomb_costs_at_most_a_text_cell() -> None:
    data = data_of("zlib_bomb.png")
    tracemalloc.start()
    try:
        output = run(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert "image.value_not_copied" in codes(output)  # the text is cut to max_value_bytes
    assert peak < 16 * 1024 * 1024  # the bomb inflates to far more; only 4 KiB is produced


def test_the_inflate_limit_is_the_configured_one() -> None:
    quiet = run("amr_dock.png")
    assert "image.malformed" not in codes(quiet)
    # iCCP holds 180 bytes and inflates to 352; the 1330-byte iTXt (XMP) is over the limit unread
    tight = run("amr_dock.png", max_metadata_bytes=200)
    assert {"image.malformed", "image.value_not_copied"} <= codes(tight)
    assert len(images(tight)) == 1


def test_an_xml_bomb_in_xmp_is_refused_without_expansion() -> None:
    output = run("xmp_bomb.jpg")
    assert "image.xmp_unreadable" in codes(output)
    assert len(images(output)) == 1  # the image is still recorded


def test_a_png_declaring_a_giant_chunk_is_a_truncation_not_a_read() -> None:
    data = bytearray(data_of("amr_dock.png"))
    data[33:37] = struct.pack(">I", 2**31 - 1)  # the first chunk after IHDR claims 2 GiB
    output = run(bytes(data))
    assert codes(output) & {"image.truncated", "image.malformed"}


# --- Bad offsets and loops ------------------------------------------


def test_a_tiff_whose_first_ifd_is_past_the_end_has_no_image_and_a_bad_offset() -> None:
    data = bytearray(data_of("rov_survey.tif"))
    data[4:8] = struct.pack("<I", 10**6)
    output = run(bytes(data))
    assert {"image.bad_offset", "image.unreadable"} <= codes(output) and not images(output)


def test_an_ifd_that_points_to_itself_is_read_once() -> None:
    data = bytearray(data_of("rov_survey.tif"))
    ifd = struct.unpack("<I", data[4:8])[0]
    entries = struct.unpack("<H", data[ifd : ifd + 2])[0]
    follow = ifd + 2 + 12 * entries
    data[follow : follow + 4] = struct.pack("<I", ifd)
    output = run(bytes(data))
    assert "image.ifd_loop" in codes(output)
    assert len(images(output)) == 1


def test_an_entry_whose_count_cannot_fit_is_a_bad_offset_with_no_allocation() -> None:
    data = bytearray(data_of("rov_survey.tif"))
    ifd = struct.unpack("<I", data[4:8])[0]
    data[ifd + 6 : ifd + 10] = struct.pack("<I", 0xFFFFFFFF)  # the first entry's count
    assert "image.bad_offset" in codes(run(bytes(data)))


def test_exif_in_a_jpeg_with_a_loop_or_bad_offsets_keeps_the_image_and_the_rest() -> None:
    output = run("exif_loop.jpg")
    assert {"image.ifd_loop", "image.bad_offset"} <= codes(output)
    (image,) = images(output)
    assert (image.width, image.height) == (16, 16)


def test_an_exif_orientation_outside_one_to_eight_is_unknown_and_a_finding() -> None:
    data = bytearray(data_of("crawler_inspection.jpg"))
    at = data.index(b"\x12\x01\x03\x00\x01\x00\x00\x00")  # Orientation, SHORT, count 1
    for value in (0, 9, 65535):
        data[at + 8 : at + 10] = struct.pack("<H", value)
        output = run(bytes(data))
        (image,) = images(output)
        assert type(image.orientation).__name__ == "Unknown"
        assert "image.value_unreadable" in codes(output)
    for value in range(1, 9):
        data[at + 8 : at + 10] = struct.pack("<H", value)
        (image,) = images(run(bytes(data)))
        assert image.orientation.value == value  # type: ignore[union-attr]


def test_a_date_that_is_not_a_date_is_unknown_never_a_guess() -> None:
    data = bytearray(data_of("crawler_inspection.jpg"))
    at = data.index(b"2026:09:14 10:21:07")
    data[at : at + 19] = b"2026:13:45 99:99:99"
    output = run(bytes(data))
    (image,) = images(output)
    assert type(image.capture.time).__name__ == "Unknown"
    assert "image.value_unreadable" in codes(output)


# --- The limits ------------------------------------------


def test_max_pixels_is_exact_at_the_boundary() -> None:
    at = run("amr_dock.png", max_pixels=32 * 24)
    assert "image.pixel_limit" not in codes(at)
    over = run("amr_dock.png", max_pixels=32 * 24 - 1)
    assert "image.pixel_limit" in codes(over) and len(images(over)) == 1


@pytest.mark.parametrize("name", ["crawler_inspection.jpg", "amr_dock.png", "rov_survey.tif",
                                  "humanoid_headcam.webp", "floor_map.bmp"])  # fmt: skip
@pytest.mark.parametrize(("option", "value"), [("max_entries", 3), ("max_structures", 8)])
def test_a_limit_stops_parsing_with_one_finding_and_keeps_the_image(
    name: str, option: str, value: int
) -> None:
    output = run(name, **{option: value})
    limits = [f for f in output.findings() if f.code == "image.limit_exceeded"]
    if limits:  # a format with fewer structures than the limit simply finishes
        assert len(limits) == 1 and limits[0].details["option"] == option
    assert images(output), f"{name} lost its image to {option}"
    assert "image.raster_truncated" not in codes(output)  # a limit is not a short raster


def test_a_jpeg_whose_metadata_exhausts_the_budget_still_finds_its_frame_header() -> None:
    output = run("crawler_inspection.jpg", max_structures=8)
    assert "image.limit_exceeded" in codes(output)
    (image,) = images(output)
    assert (image.width, image.height) == (64, 48)


def test_limits_bound_how_much_a_source_emits() -> None:
    small = run("crawler_inspection.jpg", max_entries=20)
    assert len(small.records()) < len(run("crawler_inspection.jpg").records()) // 2


def test_max_value_bytes_cites_a_long_value_instead_of_copying_it() -> None:
    output = run("amr_dock.png", max_value_bytes=4)
    assert "image.value_not_copied" in codes(output)
    assert len(images(output)) == 1


# --- Regressions from review: time, memory and exceptions on hostile headers --------------------


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png_with_chunk(kind: bytes, data: bytes) -> bytes:
    """``amr_dock.png`` with one more chunk right after IHDR."""
    png = data_of("amr_dock.png")
    return png[:33] + chunk(kind, data) + png[33:]


def tiff_of(entries: list[tuple[int, int, int, int]], tail: bytes = b"") -> bytes:
    """A little-endian TIFF with one IFD of ``(tag, type, count, value-or-offset)`` entries."""
    ifd = struct.pack("<H", len(entries))
    for tag, kind, count, value in entries:
        ifd += struct.pack("<HHII", tag, kind, count, value)
    return b"II*\x00" + struct.pack("<I", 8) + ifd + struct.pack("<I", 0) + tail


def timed(data: bytes, seconds: float = 20.0, **config: Any) -> SourceOutput:
    started = time.monotonic()
    output = run(data, **config)
    assert time.monotonic() - started < seconds
    return output


def test_a_netpbm_header_of_comment_marks_or_spaces_costs_time_linear_in_its_length() -> None:
    assert codes(timed(b"P5 " + b"#" * 40)) == {"image.unreadable"}
    assert codes(timed(b"P5 " + b"# \n" * 20_000 + b"1")) == {"image.unreadable"}
    assert codes(timed(b"P7\nWIDTH" + b" " * 8000)) == {"image.unreadable"}
    assert codes(timed(b"P7\n" + b"# x\n" * 20_000)) == {"image.unreadable"}


def test_a_netpbm_number_of_thousands_of_digits_is_unreadable_not_an_exception() -> None:
    assert codes(run(b"P5 " + b"9" * 5000 + b" 1 255\n")) == {"image.unreadable"}
    assert codes(run(b"P7\nWIDTH " + b"9" * 5000 + b"\nENDHDR\n")) == {"image.unreadable"}


def test_a_netpbm_side_of_ten_digits_is_an_image_with_a_pixel_limit_and_longer_is_refused() -> None:
    output = run(b"P5\n9999999999 1\n255\nabc")
    (image,) = images(output)
    assert image.width == 9_999_999_999
    assert {"image.pixel_limit", "image.raster_truncated"} <= codes(output)
    assert codes(run(b"P5\n99999999999 1\n255\nabc")) == {"image.unreadable"}


def test_a_bigtiff_side_of_two_to_the_64_minus_one_is_a_finding_not_an_exception() -> None:
    # BigTIFF header, one IFD at 16 with ImageWidth (LONG8) = 2^64-1 and ImageLength = 1
    entries = struct.pack("<HHQQ", 256, 16, 1, 2**64 - 1) + struct.pack("<HHQQ", 257, 4, 1, 1)
    data = b"II+\x00" + struct.pack("<HHQ", 8, 0, 16) + struct.pack("<Q", 2) + entries
    output = run(data + struct.pack("<Q", 0))
    assert not images(output) and "image.malformed" in codes(output)


def test_a_tiff_ifd_declaring_65535_entries_is_read_entry_by_entry_within_the_budget() -> None:
    data = b"II*\x00" + struct.pack("<IH", 8, 65535) + bytes(12 * 65535 + 4)
    output = timed(data, max_entries=2000)
    assert "image.limit_exceeded" in codes(output)


def test_strip_arrays_are_charged_to_the_budget_and_the_image_is_still_recorded() -> None:
    count = 2000
    base = 8 + 2 + 12 * 6 + 4
    entries = [
        (256, 3, 1, 8), (257, 3, 1, 8), (273, 4, count, base), (277, 3, 1, 1),
        (278, 3, 1, 1), (279, 4, count, base + 4 * count),
    ]  # fmt: skip
    tail = struct.pack(f"<{count}I", *([8] * count)) + struct.pack(f"<{count}I", *([4] * count))
    data = tiff_of(entries, tail)
    assert "image.limit_exceeded" not in codes(run(data))  # 4000 items: within 20000 * 256
    output = run(data, max_entries=10)  # 10 * 256 = 2560 < 4000
    assert "image.limit_exceeded" in codes(output) and len(images(output)) == 1


def test_one_xmp_packet_that_many_tags_point_at_is_parsed_once() -> None:
    packet = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF'
        b' xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:tiff="http://ns.adobe.com/tiff/1.0/" tiff:Make="Acme"/>'
        b"</rdf:RDF></x:xmpmeta>"
    )
    base = 8 + 2 + 12 * 5 + 4
    entries = [(256, 3, 1, 1), (257, 3, 1, 1)] + [(700, 1, len(packet), base)] * 3
    output = run(tiff_of(entries, packet))
    xmp = [r for r in output.records() if isinstance(r, StructuredTable) and "XMP" in str(r.name)]
    assert len(xmp) == 1 and "image.overlap" in codes(output)


def test_an_os2_bitmap_with_huffman_compression_is_not_judged_by_an_uncompressed_size() -> None:
    header = struct.pack("<IiiHHIIiiII", 64, 16, 16, 1, 1, 3, 0, 0, 0, 2, 2) + bytes(24)
    file_header = b"BM" + struct.pack("<IHHI", 14 + 64 + 8, 0, 0, 14 + 64)
    output = run(file_header + header + bytes(8))
    assert len(images(output)) == 1 and "image.raster_truncated" not in codes(output)
    plain = bytearray(file_header + header + bytes(8))
    plain[14 + 16 : 14 + 20] = bytes(4)  # compression 0: 16 rows of 4 bytes cannot fit in 8
    assert "image.raster_truncated" in codes(run(bytes(plain)))


def not_covered_cells(output: SourceOutput) -> list[tuple[StructuredTable, StructuredRecord, int]]:
    records = output.records()
    tables = {r.id: r for r in records if isinstance(r, StructuredTable)}
    return [
        (tables[r.table], r, column)
        for r in records
        if isinstance(r, StructuredRecord)
        for column, c in enumerate(r.cells)
        if isinstance(c, NotCovered)
    ]


def test_a_long_png_text_is_not_copied_its_cell_is_not_covered_and_the_finding_cites_it() -> None:
    output = run(png_with_chunk(b"tEXt", b"Comment\x00" + b"a" * 5000))
    (table, row, column), *_ = [
        c for c in not_covered_cells(output) if str(known(c[0].name)) == "tEXt"
    ]
    assert known(table.header)[column] == "text"
    assert not any(  # no prefix of it is a Known cell anywhere
        isinstance(c, Known) and isinstance(c.value, str) and c.value.startswith("aaaa")
        for r in output.records()
        if isinstance(r, StructuredRecord)
        for c in r.cells
    )
    (finding,) = [f for f in output.findings() if f.code == "image.value_not_copied"]
    step = finding.subject.locator[-1]  # type: ignore[union-attr]
    assert (step.row, step.column, step.column_name) == (0, column, "text")  # type: ignore[union-attr]
    assert finding.subject.locator[:-1] == row.provenance.evidence.locator[:-1]  # type: ignore[union-attr]
    assert finding.details["count"] == 1 and finding.details["cells"] == [
        {"column": column, "length": 5000, "row": 0}
    ]
    wide = run(png_with_chunk(b"tEXt", b"Comment\x00" + b"a" * 5000), max_value_bytes=8192)
    assert "image.value_not_copied" not in codes(wide)


def test_a_png_chunk_over_max_metadata_bytes_is_not_read_at_all() -> None:
    big = png_with_chunk(b"tEXt", b"Comment\x00" + b"a" * 5000)
    output = run(big, max_metadata_bytes=1024)
    (finding,) = [f for f in output.findings() if f.details.get("bytes") == 8 + 5000]
    assert finding.code == "image.value_not_copied" and len(images(output)) == 1


def test_a_png_giant_text_chunk_costs_bounded_memory() -> None:
    data = png_with_chunk(b"tEXt", b"Comment\x00" + b"a" * (40 * 1024 * 1024))
    tracemalloc.start()
    try:
        output = run(data, max_metadata_bytes=1024 * 1024)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert "image.value_not_copied" in codes(output)
    assert peak < 8 * 1024 * 1024


def test_a_png_profile_with_no_or_an_unknown_compression_method_is_a_finding() -> None:
    assert "image.malformed" in codes(run(png_with_chunk(b"iCCP", b"profile\x00")))
    odd = run(png_with_chunk(b"iCCP", b"profile\x00\x01" + zlib.compress(b"x")))
    assert "image.malformed" in codes(odd)
    ztxt = run(png_with_chunk(b"zTXt", b"Comment\x00\x07" + zlib.compress(b"hello")))
    assert "image.malformed" in codes(ztxt)


def test_a_chunk_with_the_wrong_size_for_its_structure_is_not_read_whole() -> None:
    data = bytearray(data_of("amr_dock.png"))
    data[8:12] = struct.pack(">I", 1 << 30)  # IHDR claims a gigabyte, the file holds 3 KiB
    output = run(bytes(data))
    assert codes(output) & {"image.truncated", "image.malformed", "image.unreadable"}


def test_zero_padded_netpbm_numbers_parse_and_only_the_value_is_bounded() -> None:
    (image,) = images(run(b"P5 0000000640 000480 255\n" + bytes(640 * 480)))
    assert (image.width, image.height) == (640, 480)
    assert images(run(b"P5 " + b"0" * 5000 + b"2 1 255\n\x00\x00"))
    pam = b"P7\nWIDTH 000000000002\nHEIGHT 1\nDEPTH 1\nMAXVAL 255\nTUPLTYPE GRAYSCALE\nENDHDR\n"
    assert images(run(pam + b"\x00\x00"))


def test_a_pam_header_ends_its_lines_at_lf_only() -> None:
    crlf = b"P7\r\nWIDTH 2\r\nHEIGHT 1\r\nDEPTH 1\r\nMAXVAL 255\r\nENDHDR\r\n\x00\x00"
    assert codes(run(crlf)) == {"image.unreadable"}
    odd = b"P7\nWIDTH 2\x0b\nHEIGHT 1\nDEPTH 1\nMAXVAL 255\nENDHDR\n\x00\x00"
    assert codes(run(odd)) == {"image.unreadable"}


def test_an_oversize_first_exif_chunk_does_not_hide_a_valid_second_one() -> None:
    exif = data_of("amr_dock.png")[data_of("amr_dock.png").index(b"eXIf") + 4 :][:150]
    first = chunk(b"eXIf", exif + bytes(3000))
    png = data_of("amr_dock.png")
    cut = png.index(b"eXIf") - 4  # drop the original, then add an oversize one and a valid one
    original_end = cut + 12 + 150
    data = png[:cut] + first + chunk(b"eXIf", exif) + png[original_end:]
    output = run(data, max_metadata_bytes=1024)
    assert "image.value_not_copied" in codes(output) and "image.repeated" not in codes(output)


def test_strip_arrays_past_the_offset_cap_are_not_charged_to_the_budget() -> None:
    entries = [(256, 3, 1, 8), (257, 3, 1, 8), (273, 4, 0xFFFFFFFF, 8), (279, 4, 0xFFFFFFFF, 8)]
    output = run(tiff_of(entries, bytes(64)), max_entries=10)
    assert "image.limit_exceeded" not in codes(output) and len(images(output)) == 1


def many_ztxt(count: int, inflated: int) -> bytes:
    text = b"Comment\x00\x00" + zlib.compress(b"a" * inflated, 9)
    png = data_of("amr_dock.png")
    return png[:33] + b"".join(chunk(b"zTXt", text) for _ in range(count)) + png[33:]


def test_many_zlib_texts_each_inflating_a_thousandfold_cost_a_text_cell_each() -> None:
    data = many_ztxt(24, 16 * 1024 * 1024)  # each: ~16 KB of zlib for 16 MiB of text
    assert len(data) < 512 * 1024
    tracemalloc.start()
    try:
        output = run(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert "image.value_not_copied" in codes(output) and len(images(output)) == 1
    assert peak < 24 * 1024 * 1024  # not 24 x 16 MiB


def xmp_png(depth: int, ancestor: int, leaf: int, leaves: int) -> bytes:
    """A PNG whose zlib-compressed iTXt XMP nests ``depth`` properties of ``ancestor`` characters
    and ends in ``leaves`` properties of ``leaf`` characters: paths of depth x ancestor."""
    names = [f"n{i:04d}".ljust(ancestor, "x") for i in range(depth)]
    body = "".join(f"<p:{n}>" for n in names)
    body += "".join(f"<p:{f'l{i:05d}'.ljust(leaf, 'y')}>v</p:{f'l{i:05d}'.ljust(leaf, 'y')}>"
                    for i in range(leaves))  # fmt: skip
    body += "".join(f"</p:{n}>" for n in reversed(names))
    packet = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF'
        ' xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description'
        f' xmlns:p="http://example.com/p/">{body}</rdf:Description></rdf:RDF></x:xmpmeta>'
    ).encode()
    text = b"XML:com.adobe.xmp\x00\x01\x00\x00\x00" + zlib.compress(packet, 9)
    return png_with_chunk(b"iTXt", text)


def test_xmp_paths_of_deep_long_names_are_cut_and_the_total_text_is_bounded() -> None:
    data = xmp_png(depth=60, ancestor=1000, leaf=100, leaves=4000)
    assert len(data) < 512 * 1024
    tracemalloc.start()
    try:
        output = run(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert "image.value_not_copied" in codes(output)
    assert peak < 160 * 1024 * 1024  # un-capped, these paths alone were ~900 MB
    assert not any(  # nothing over the cap is a Known cell
        isinstance(c, Known) and isinstance(c.value, str) and len(c.value.encode()) > 4096
        for r in output.records()
        if isinstance(r, StructuredRecord)
        for c in r.cells
    )
    counts: list[Any] = [
        (f.details["count"], f.details["cells"])
        for f in output.findings()
        if f.code == "image.value_not_copied"
    ]
    big = [(count, cells) for count, cells in counts if count > 1]
    assert len(big) == 1 and big[0][0] > 16 and len(big[0][1]) == 16


def test_a_tree_of_130_kb_ancestor_names_costs_about_its_names() -> None:
    data = xmp_png(depth=60, ancestor=130_000, leaf=10, leaves=3)
    assert len(data) < 64 * 1024
    tracemalloc.start()
    try:
        output = run(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(images(output)) == 1 and "image.value_not_copied" in codes(output)
    assert peak < 72 * 1024 * 1024  # the names ~8 MB, a few copies; paths of 8 MB each were 270 MB
    assert all(
        known(t.header)[c] in ("path", "value", "namespace")
        for t, _, c in not_covered_cells(output)
        if str(known(t.name)) == "XMP"
    )


def test_total_text_of_a_source_is_bounded_by_max_metadata_bytes() -> None:
    output = run(xmp_png(depth=2, ancestor=50, leaf=50, leaves=8000), max_metadata_bytes=1 << 20)
    assert "image.limit_exceeded" in codes(output)


def test_a_declared_position_outside_the_globe_is_not_a_valid_position() -> None:
    maker = GENERATOR
    for lat, lon, why in (
        ((200, 1), (151, 1), "latitude 200"),
        ((23, 1), (181, 1), "longitude 181"),
        ((23, 1), (151, 1), "ok"),
    ):
        gps = [
            maker.Tag(1, maker.ASCII, maker.ascii_value("N")),
            maker.Tag(2, maker.RATIONAL, [lat, (0, 1), (0, 1)]),
            maker.Tag(3, maker.ASCII, maker.ascii_value("E")),
            maker.Tag(4, maker.RATIONAL, [lon, (0, 1), (0, 1)]),
        ]
        exif = maker.exif_block([maker.Tag(271, maker.ASCII, maker.ascii_value("X"))], None, gps)
        data = maker.jpeg(8, 8, 1, [maker.segment(0xE1, b"Exif\x00\x00" + exif)])
        (image,) = images(run(data))
        valid = isinstance(image.capture.position, Known)
        assert valid == (why == "ok"), why
        if not valid:
            assert "image.value_unreadable" in codes(run(data))  # the raw rows stay in the GPS IFD
    minutes = maker.exif_block(
        [maker.Tag(271, maker.ASCII, maker.ascii_value("X"))],
        None,
        [
            maker.Tag(1, maker.ASCII, maker.ascii_value("N")),
            maker.Tag(2, maker.RATIONAL, [(23, 1), (75, 1), (0, 1)]),
            maker.Tag(3, maker.ASCII, maker.ascii_value("E")),
            maker.Tag(4, maker.RATIONAL, [(151, 1), (0, 1), (0, 1)]),
        ],
    )
    data = maker.jpeg(8, 8, 1, [maker.segment(0xE1, b"Exif\x00\x00" + minutes)])
    assert not isinstance(images(run(data))[0].capture.position, Known)


def test_a_netpbm_header_is_accepted_up_to_what_the_probe_sees_and_no_further() -> None:
    fits = b"P5\n#" + b"x" * (65536 - 20) + b"\n2 1\n255\n"
    assert len(fits) <= 65536 and images(run(fits + b"\0\0"))
    too_long = b"P5\n#" + b"x" * 65536 + b"\n2 1\n255\n\0\0"
    assert codes(run(too_long)) == {"image.unreadable"}


def test_many_inflate_bombs_spend_the_sources_inflate_total() -> None:
    bomb = b"XML:com.adobe.xmp\x00\x01\x00\x00\x00" + zlib.compress(b"\x00" * (8 << 20), 9)
    png = data_of("amr_dock.png")
    data = png[:33] + b"".join(chunk(b"iTXt", bomb) for _ in range(40)) + png[33:]
    tracemalloc.start()
    try:
        output = run(data, max_metadata_bytes=1 << 20)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    refused = [f for f in output.findings() if f.code == "image.malformed"]
    assert len(refused) >= 40 and len(images(output)) == 1
    assert any("inflate total" in f.message for f in refused)  # the total, not the single limit
    assert peak < 16 * 1024 * 1024


def test_an_xmp_value_over_the_cap_is_not_copied_whatever_its_characters() -> None:
    packet = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF'
        ' xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description xmlns:t="http://example.com/t/" t:Note="' + "漢" * 4096 + '"'
        ' t:Short="漢" t:Edge="' + "漢" * 1365 + '"/>'  # 4095 bytes: copied
        "</rdf:RDF></x:xmpmeta>"
    ).encode()
    output = run(png_with_chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + packet))
    (table, row, column), *_ = [
        c for c in not_covered_cells(output) if str(known(c[0].name)) == "XMP"
    ]
    assert known(table.header)[column] == "value"
    kept = [
        c.value
        for r in output.records()
        if isinstance(r, StructuredRecord) and len(r.cells) == 3
        for c in r.cells
        if isinstance(c, Known) and isinstance(c.value, str) and "漢" in c.value
    ]
    assert sorted(len(v) for v in kept) == [1, 1365]
    (finding,) = [
        f
        for f in output.findings()
        if f.code == "image.value_not_copied"
        and f.details["cells"] == [{"column": 2, "length": 3 * 4096, "row": row.row}]
    ]
    assert finding.subject.locator[-1].column == 2  # type: ignore[union-attr]


def test_every_cell_a_not_copied_finding_names_is_in_a_row_that_exists() -> None:
    names = [f"n{i:02d}".ljust(60, "x") for i in range(58)]  # a path of ~3.5 KB: copied
    leaves = "".join(
        f"<p:l{i:04d}>{'v' * (5000 if i % 50 == 0 else 1)}</p:l{i:04d}>" for i in range(3000)
    )
    body = (
        "".join(f"<p:{n}>" for n in names) + leaves + "".join(f"</p:{n}>" for n in reversed(names))
    )
    packet = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF'
        ' xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description'
        f' xmlns:p="http://example.com/p/">{body}</rdf:Description></rdf:RDF></x:xmpmeta>'
    ).encode()
    text = b"XML:com.adobe.xmp\x00\x01\x00\x00\x00" + zlib.compress(packet, 9)
    output = run(png_with_chunk(b"iTXt", text), max_metadata_bytes=1 << 20)
    assert "image.limit_exceeded" in codes(output)  # the row text ran out part-way
    records = output.records()
    tables = [r for r in records if isinstance(r, StructuredTable) and known(r.name) == "XMP"]
    count = {
        t.id: sum(1 for r in records if isinstance(r, StructuredRecord) and r.table == t.id)
        for t in tables
    }
    xmp = max(tables, key=lambda t: count[t.id])  # the fixture PNG has an XMP packet of its own
    rows = {r.row for r in records if isinstance(r, StructuredRecord) and r.table == xmp.id}
    named: list[Any] = [
        f
        for f in output.findings()
        if f.code == "image.value_not_copied"
        and f.subject.locator[:-1] == xmp.provenance.evidence.locator  # type: ignore[union-attr]
    ]
    assert named
    for finding in named:
        assert finding.subject.locator[-1].row in rows
        assert {cell["row"] for cell in finding.details["cells"]} <= rows


def test_an_oversize_text_that_is_also_corrupt_says_so() -> None:
    data = bytearray(b"Comment\x00" + b"a" * 5000)
    data[100] = 0xFF  # a latin-1 tEXt cannot be corrupt; use an iTXt (UTF-8)
    itxt = b"Comment\x00\x00\x00\x00\x00" + b"a" * 100 + b"\xff" + b"a" * 5000
    output = run(png_with_chunk(b"iTXt", itxt))
    assert {"image.value_not_copied", "image.value_unreadable"} <= codes(output)

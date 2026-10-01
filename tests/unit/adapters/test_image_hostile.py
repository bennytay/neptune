"""Hostile and damaged images: truncation, lying sizes, bombs, bad offsets and loops, limits.

Every case is a finding, never an exception, and the work done is bounded by the config's limits.
``ingest_source`` runs the contract's checks on every output, so each run here also proves that no
record or finding is repeated and that every citation is inside the source.
"""

import random
import struct
import tracemalloc
import zlib
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.image import ImageAdapter
from neptune.discovery.reader import BytesReader
from neptune.model.finding import FindingCategory, Severity
from neptune.model.world import Image

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "image"
VALID: Final = (
    "crawler_inspection.jpg", "amr_dock.png", "rov_survey.tif", "rover_raw.dng",
    "survey_tile.tif", "humanoid_headcam.webp", "floor_map.bmp", "legacy_cam.bmp",
    "wrist_depth.pgm", "thermal.ppm", "gripper_mask.pbm", "gripper.pam",
)  # fmt: skip


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


# --- The damaged-file corpus -----------------------------------------------------------------------

CORPUS: Final = {
    # name: (findings it must make, images it still yields)
    "truncated.jpg": ({"image.truncated"}, 1),
    "truncated.png": ({"image.truncated"}, 1),
    "truncated.webp": ({"image.truncated"}, 1),
    "truncated.bmp": ({"image.raster_truncated"}, 1),
    "truncated.tif": ({"image.truncated", "image.bad_offset"}, 1),
    "bad_crc.png": ({"image.crc_mismatch"}, 1),
    "bomb.png": ({"image.pixel_limit", "image.raster_truncated"}, 1),
    "zlib_bomb.png": ({"image.malformed"}, 1),
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


# --- Truncation at every length, and mutation ----------------------------------------------------


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
            mutated[rng.randrange(len(mutated))] = rng.choice((0x00, 0xFF, 0x7F, rng.randrange(256)))
        run(bytes(mutated))


@pytest.mark.parametrize("name", VALID)
def test_a_header_followed_by_noise_never_raises(name: str) -> None:
    data = data_of(name)
    rng = random.Random(f"noise {name}")
    for length in (4, 12, 40):
        run(data[:length] + rng.randbytes(300))


# --- Lying dimensions --------------------------------------------------------------------------------


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


# --- Decompression bombs -------------------------------------------------------------------------------


def test_a_zlib_bomb_costs_at_most_max_metadata_bytes() -> None:
    data = data_of("zlib_bomb.png")
    tracemalloc.start()
    try:
        output = run(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert "image.malformed" in codes(output)
    assert peak < 64 * 1024 * 1024  # the default limit is 16 MiB; the bomb inflates far past it


def test_the_inflate_limit_is_the_configured_one() -> None:
    quiet = run("amr_dock.png")
    assert "image.malformed" not in codes(quiet)
    tight = run("amr_dock.png", max_metadata_bytes=64)
    assert {"image.malformed"} <= codes(tight) and len(images(tight)) == 1


def test_an_xml_bomb_in_xmp_is_refused_without_expansion() -> None:
    output = run("xmp_bomb.jpg")
    assert "image.xmp_unreadable" in codes(output)
    assert len(images(output)) == 1  # the image is still recorded


def test_a_png_declaring_a_giant_chunk_is_a_truncation_not_a_read() -> None:
    data = bytearray(data_of("amr_dock.png"))
    data[33:37] = struct.pack(">I", 2**31 - 1)  # the first chunk after IHDR claims 2 GiB
    output = run(bytes(data))
    assert codes(output) & {"image.truncated", "image.malformed"}


# --- Bad offsets and loops ---------------------------------------------------------------------------


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


# --- The limits -------------------------------------------------------------------------------------------


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

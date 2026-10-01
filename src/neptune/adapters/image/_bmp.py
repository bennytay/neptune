"""BMP: the file header and the DIB header as one-row tables; the pixels are never read.

The DIB header's size names its version (``BITMAPINFOHEADER``, ``BITMAPV5HEADER``, …), and its
fields are the table's columns, as stored: fixed-point endpoints and gammas stay their integers,
the colour space type its 32-bit value. A negative height declares a top-down raster: the image's
height is its magnitude, and regions still count rows from the top. A V5 header's embedded
profile (``PROFILE_EMBEDDED``) is read as ICC; a linked one names a file and is never followed.
An uncompressed raster the file is too short for is ``image.raster_truncated``. BMP has no place
for capture metadata or orientation: both are ``NotCovered``.
"""

import struct
from typing import Final

import neptune.adapters.image._blocks as _blocks
from neptune.adapters.image._context import Context
from neptune.adapters.image._emit import BAD_OFFSET, RASTER_TRUNCATED, UNREADABLE
from neptune.adapters.image._space import Space
from neptune.adapters.image._still import NOT_COVERED, Still

_FILE: Final = ("type", "file_size", "reserved1", "reserved2", "pixel_offset")
_CORE: Final = ("header_size", "width", "height", "planes", "bit_count")
_INFO: Final = (
    "header_size", "width", "height", "planes", "bit_count", "compression", "image_size",
    "x_pixels_per_metre", "y_pixels_per_metre", "colours_used", "colours_important",
)  # fmt: skip
_V2: Final = (*_INFO, "red_mask", "green_mask", "blue_mask")
_V3: Final = (*_V2, "alpha_mask")
_V4: Final = (
    *_V3, "colour_space_type", "red_x", "red_y", "red_z", "green_x", "green_y", "green_z",
    "blue_x", "blue_y", "blue_z", "gamma_red", "gamma_green", "gamma_blue",
)  # fmt: skip
_V5: Final = (*_V4, "intent", "profile_data", "profile_size", "reserved")
# Header size -> (name, layout, columns, bytes the table cites).
HEADERS: Final[dict[int, tuple[str, str, tuple[str, ...], int]]] = {
    12: ("BITMAPCOREHEADER", "<IHHHH", _CORE, 12),
    40: ("BITMAPINFOHEADER", "<IiiHHIIiiII", _INFO, 40),
    52: ("BITMAPV2INFOHEADER", "<IiiHHIIiiIIIII", _V2, 52),
    56: ("BITMAPV3INFOHEADER", "<IiiHHIIiiIIIIII", _V3, 56),
    64: ("OS22XBITMAPHEADER", "<IiiHHIIiiII", _INFO, 40),
    108: ("BITMAPV4HEADER", "<IiiHHIIiiIIIIIII9i3I", _V4, 108),
    124: ("BITMAPV5HEADER", "<IiiHHIIiiIIIIIII9i3I4I", _V5, 124),
}
PROFILE_EMBEDDED: Final = 0x4D424544  # 'MBED'
_UNCOMPRESSED: Final = frozenset({0, 3, 6})  # BI_RGB, BI_BITFIELDS, BI_ALPHABITFIELDS
_OS2_UNCOMPRESSED: Final = frozenset({0})  # an OS/2 2.x header's 3 and 4 are Huffman and RLE24


def header_size(head: bytes) -> int | None:
    """The DIB header size after a ``BM`` file header, if it is one BMP defines."""
    if len(head) < 18 or head[:2] != b"BM":
        return None
    (size,) = struct.unpack("<I", head[14:18])
    return size if size in HEADERS else None


def read(ctx: Context, space: Space) -> list[Still]:
    """The BMP file that is all of ``space``."""
    out = ctx.out
    head = space.read(0, min(space.size, 18))
    size = header_size(head)
    if size is None or not space.fits(14, size):
        out.finding(UNREADABLE, space.whole(), "the file has no BMP file header and DIB header")
        return []
    file_values = struct.unpack("<2sIHHI", space.read(0, 14))
    ctx.structure(space.cite(0, 14), "BITMAPFILEHEADER", _FILE, ["BM", *file_values[1:]])
    name, layout, columns, cited = HEADERS[size]
    values = struct.unpack(layout, space.read(14, struct.calcsize(layout)))
    ctx.structure(space.cite(14, cited), name, columns, list(values))
    fields = dict(zip(columns, values, strict=True))
    width, height = fields["width"], fields["height"]
    if width < 1 or height == 0:
        out.finding(
            UNREADABLE,
            space.cite(14, cited),
            f"the DIB header declares {width} x {height} pixels",
            {"height": height, "width": width},
        )
        return []
    if size == 124 and fields["colour_space_type"] == PROFILE_EMBEDDED:
        at, length = 14 + fields["profile_data"], fields["profile_size"]
        if space.fits(at, length) and length <= ctx.max_metadata_bytes:
            _blocks.icc(ctx, space.window(at, length), "the BMP V5 header's profile")
        else:
            out.finding(
                BAD_OFFSET,
                space.cite(14, cited),
                f"the embedded profile is declared at [{at}, {at + length}) of {space.size} bytes",
                {"offset": at, "size": length},
            )
    compression = fields.get("compression", 0)
    stored_raw = _OS2_UNCOMPRESSED if size == 64 else _UNCOMPRESSED
    if compression in stored_raw and fields["bit_count"]:
        row = (fields["bit_count"] * width + 31) // 32 * 4
        needed = file_values[4] + row * abs(height)
        if needed > space.size:
            out.finding(
                RASTER_TRUNCATED,
                space.whole(),
                f"the declared raster ends at byte {needed}; the file holds {space.size}",
                {"needed": needed, "size": space.size},
            )
    return [Still(space.whole(), width, abs(height), "bmp", None, NOT_COVERED)]

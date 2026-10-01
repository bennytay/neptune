"""Which image format the first bytes start, and how sure the bytes alone make that.

PNG, JPEG, TIFF (and BigTIFF) and WebP have signatures: matching one is ``SIGNATURE``, and a first
structure that also parses (``IHDR`` with its CRC, a JPEG segment, a TIFF IFD offset inside the
file, a WebP bitstream chunk) is ``VERIFIED``. BMP's ``BM`` and Netpbm's ``P1`` to ``P7`` are too
short to be signatures (text starts that way), so those formats are claimed only when their whole
header parses: ``VERIFIED``, or ``STRUCTURE`` for a plain-text Netpbm header. The name is never
consulted.
"""

import struct
import zlib
from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import SIGNATURE, STRUCTURE, VERIFIED
from neptune.adapters.image import _bmp, _pnm
from neptune.adapters.image._png import SIGNATURE as PNG_SIGNATURE

PNG: Final = "png"
JPEG: Final = "jpeg"
TIFF: Final = "tiff"
WEBP: Final = "webp"
BMP: Final = "bmp"
PNM: Final = "pnm"
TIFF_MAGIC: Final = (b"II*\x00", b"MM\x00*")
BIGTIFF_MAGIC: Final = (b"II+\x00", b"MM\x00+")
JPEG_MAGIC: Final = b"\xff\xd8\xff"
_JPEG_STANDALONE: Final = frozenset({0x00, 0x01, 0xFF, *range(0xD0, 0xDA)})


@dataclass(frozen=True)
class Detected:
    format: str
    confidence: float
    reason: str


def detect(head: bytes, size: int) -> Detected | None:
    """The format ``head`` (the first bytes of a ``size``-byte source) starts, or ``None``."""
    if head.startswith(PNG_SIGNATURE):
        verified = (
            head[8:16] == b"\x00\x00\x00\x0dIHDR"
            and len(head) >= 33
            and zlib.crc32(head[12:29]) == struct.unpack(">I", head[29:33])[0]
        )
        return _found(PNG, verified, "the PNG signature", "an IHDR chunk with its CRC")
    if head.startswith(JPEG_MAGIC):
        verified = len(head) >= 6 and head[3] not in _JPEG_STANDALONE
        if verified:
            (length,) = struct.unpack(">H", head[4:6])
            verified = length >= 2 and 4 + length <= size
        return _found(JPEG, verified, "the JPEG SOI marker", "a well-formed first segment")
    if head[:4] in TIFF_MAGIC or head[:4] in BIGTIFF_MAGIC:
        return _found(TIFF, _tiff_verified(head, size), "a TIFF header", "IFD0 inside the file")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        verified = head[12:16] in (b"VP8 ", b"VP8L", b"VP8X")
        return _found(WEBP, verified, "a RIFF WEBP header", "a VP8, VP8L or VP8X chunk")
    if _bmp_verified(head):
        return Detected(BMP, VERIFIED, "a BMP file header and DIB header that parse")
    header = _pnm.parse_header(head)
    if header is not None:
        plain = header.magic in _pnm.PLAIN
        kind = "plain" if plain else "binary"
        return Detected(PNM, STRUCTURE if plain else VERIFIED, f"a {kind} Netpbm header")
    return None


def _found(name: str, verified: bool, signature: str, structure: str) -> Detected:
    if verified:
        return Detected(name, VERIFIED, f"{signature} and {structure}")
    return Detected(name, SIGNATURE, signature)


def _tiff_verified(head: bytes, size: int) -> bool:
    order = "<" if head[:2] == b"II" else ">"
    if head[:4] in TIFF_MAGIC and len(head) >= 8:
        first: int = struct.unpack(order + "I", head[4:8])[0]
        return first >= 8 and first + 2 <= size
    if len(head) >= 16:
        fields: tuple[int, int, int] = struct.unpack(order + "HHQ", head[4:16])
        width, zero, start = fields
        return width == 8 and zero == 0 and start >= 16 and start + 8 <= size
    return False


def _bmp_verified(head: bytes) -> bool:
    size = _bmp.header_size(head)
    if size is None:
        return False
    if size == 12:
        if len(head) < 26:
            return False
        values: tuple[int, int, int] = struct.unpack("<HHH", head[18:24])
    else:
        if len(head) < 28:
            return False
        values = struct.unpack("<iiH", head[18:28])
    width, height, planes = values
    return planes == 1 and width >= 1 and height != 0

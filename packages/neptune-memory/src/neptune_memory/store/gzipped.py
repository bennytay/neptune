"""Deterministic gzip for graph documents that travel as files (the acceptance snapshot), and a
bounded reader for them.

``deterministic_gzip`` writes one gzip member (RFC 1952) whose header is fixed: no file name,
comment or extra field, ``MTIME`` 0, ``XFL`` 2 (best compression) and ``OS`` 255 (unknown). So the
bytes depend only on the input, the level and the deflate implementation (``zlib``), never on the
host's clock, file name or platform, which ``gzip.compress`` would write.

``gunzip`` reads one member and refuses what a hostile file could hide: output past ``limit``
(a decompression bomb), a truncated stream, a bad checksum, and bytes after the member.
"""

from __future__ import annotations

import struct
import zlib
from typing import Final

MAGIC: Final = b"\x1f\x8b"
LEVEL: Final = 9
_HEADER: Final = MAGIC + b"\x08\x00" + b"\x00\x00\x00\x00" + b"\x02\xff"
_GZIP_WBITS: Final = 16 + zlib.MAX_WBITS  # a gzip header and trailer around deflate


class GzipError(ValueError):
    """Bytes that are not one complete gzip member within the limit."""


def deterministic_gzip(data: bytes, level: int = LEVEL) -> bytes:
    """``data`` as one gzip member with a fixed header (see the module docstring)."""
    deflate = zlib.compressobj(level, zlib.DEFLATED, -zlib.MAX_WBITS)
    body = deflate.compress(data) + deflate.flush()
    trailer = struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF)
    return _HEADER + body + trailer


def is_gzip(data: bytes) -> bool:
    return data[:2] == MAGIC


def gunzip(data: bytes, limit: int) -> bytes:
    """The content of the one gzip member ``data``, at most ``limit`` bytes (``GzipError``)."""
    stream = zlib.decompressobj(_GZIP_WBITS)
    out = bytearray()
    pending = data
    try:
        while not stream.eof:
            chunk = stream.decompress(pending, limit + 1 - len(out))
            out += chunk
            if len(out) > limit:
                raise GzipError(f"the gzip content is larger than {limit} bytes")
            pending = stream.unconsumed_tail
            if not chunk and not pending:
                break
    except zlib.error as exc:  # a bad header, deflate stream or checksum
        raise GzipError(f"not a valid gzip stream ({exc})") from exc
    if not stream.eof:
        raise GzipError("the gzip stream is truncated")
    if stream.unused_data:
        raise GzipError("bytes follow the gzip stream")
    return bytes(out)

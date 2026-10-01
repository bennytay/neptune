"""Metadata blocks every container carries the same way: EXIF, XMP and ICC.

A container reader finds the block's bytes; these read them with the shared parsers, so EXIF in a
JPEG APP1 segment, a PNG ``eXIf`` chunk or a WebP ``EXIF`` chunk becomes the same tables.
"""

from collections.abc import Callable

import neptune.adapters.image._icc as _icc
import neptune.adapters.image._xmp as _xmp
from neptune.adapters.image._capture import Tags
from neptune.adapters.image._context import Context
from neptune.adapters.image._emit import LIMIT_EXCEEDED
from neptune.adapters.image._space import Space
from neptune.adapters.image._tiff import ICC_TAG, XMP_TAG, Entry, Tiff


def embedded(ctx: Context, what: str) -> Callable[[Entry, Space], None]:
    """Reads an XMP packet or an ICC profile a TIFF tag holds."""

    def read(entry: Entry, window: Space) -> None:
        if entry.tag == XMP_TAG:
            _xmp.read(ctx, window, f"tag {XMP_TAG} of {what}")
        elif entry.tag == ICC_TAG:
            _icc.read(ctx, window, f"tag {ICC_TAG} of {what}")

    return read


def exif(ctx: Context, space: Space, what: str) -> Tags | None:
    """The EXIF block that is all of ``space`` (a TIFF stream): its IFDs as tables, and its tags."""
    tiff = Tiff(ctx, space, "EXIF", embedded(ctx, what))
    if not tiff.read_header():
        return None
    chain = tiff.chain()
    return Tags((chain[0],)) if chain else None


def xmp(ctx: Context, space: Space, what: str) -> None:
    """The XMP packet that is all of ``space``, unless it is larger than ``max_metadata_bytes``."""
    if space.size > ctx.max_metadata_bytes:
        ctx.out.finding(
            LIMIT_EXCEEDED,
            space.whole(),
            f"the XMP packet in {what} is {space.size} bytes, over max_metadata_bytes; not read",
            {"bytes": space.size, "max_metadata_bytes": ctx.max_metadata_bytes},
        )
        return
    _xmp.read(ctx, space, what)


def icc(ctx: Context, space: Space, what: str) -> None:
    _icc.read(ctx, space, what)

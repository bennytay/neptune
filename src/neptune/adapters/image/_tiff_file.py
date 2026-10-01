"""TIFF, BigTIFF and DNG files: every IFD that declares a raster is an ``Image``.

The IFD chain (``IFD0``, ``IFD1``, … the pages) and the ``SubIFD``s they declare (a DNG's raw
image under its preview) are read as tables by ``_tiff``. Each IFD with ImageWidth and
ImageLength is one ``Image`` citing that IFD's bytes, encoding ``dng`` when IFD0 declares a
DNGVersion and ``tiff`` otherwise. Its tags are its own IFD's, then (for a SubIFD) the IFDs above
it, then their Exif and GPS IFDs. Strip and tile offsets are checked against the file, so a
raster the file cannot hold is ``image.raster_truncated``; the strips are never read.
"""

from typing import Final

import neptune.adapters.image._blocks as _blocks
from neptune.adapters.image._capture import Tags
from neptune.adapters.image._context import Context
from neptune.adapters.image._emit import MALFORMED, RASTER_TRUNCATED, UNREADABLE
from neptune.adapters.image._space import Space
from neptune.adapters.image._still import TAGS, Still
from neptune.adapters.image._tiff import (
    DNG_VERSION,
    IMAGE_LENGTH,
    IMAGE_WIDTH,
    STRIP_BYTE_COUNTS,
    STRIP_OFFSETS,
    TILE_BYTE_COUNTS,
    TILE_OFFSETS,
    Ifd,
    Tiff,
)

_MAX_OFFSETS: Final = 1_000_000


def read(ctx: Context, space: Space) -> list[Still]:
    """The TIFF file that is all of ``space``."""
    out = ctx.out
    tiff = Tiff(ctx, space, "TIFF", _blocks.embedded(ctx, "the TIFF file"))
    if not tiff.read_header():
        out.finding(UNREADABLE, space.whole(), "the file has no readable TIFF header")
        return []
    chain = tiff.chain()
    if not chain:
        out.finding(UNREADABLE, space.whole(), "the file has no readable IFD0")
        return []
    encoding = "dng" if chain[0].get(DNG_VERSION) is not None else "tiff"
    stills: list[Still] = []
    for root in chain:
        for ifd, tags in _images(root, (root,)):
            size = _dimension(ifd, IMAGE_WIDTH), _dimension(ifd, IMAGE_LENGTH)
            if size[0] is None or size[1] is None:
                if ifd is root:
                    out.finding(
                        MALFORMED,
                        ifd.locator,
                        f"TIFF IFD {ifd.name} declares no ImageWidth and ImageLength of at least 1",
                    )
                continue
            _raster(ctx, tiff, ifd)
            stills.append(Still(ifd.locator, size[0], size[1], encoding, tags, TAGS))
    return stills


def _images(ifd: Ifd, above: tuple[Ifd, ...]) -> list[tuple[Ifd, Tags]]:
    found = [(ifd, Tags(above))]
    for child in ifd.children:
        if child.name == "SubIFD":
            found.extend(_images(child, (child, *above)))
    return found


def _dimension(ifd: Ifd, tag: int) -> int | None:
    entry = ifd.get(tag)
    items = (entry.items or ()) if entry is not None else ()
    if len(items) == 1 and isinstance(items[0], int) and items[0] >= 1:
        return items[0]
    return None


def _raster(ctx: Context, tiff: Tiff, ifd: Ifd) -> None:
    for offsets_tag, counts_tag in (
        (STRIP_OFFSETS, STRIP_BYTE_COUNTS),
        (TILE_OFFSETS, TILE_BYTE_COUNTS),
    ):
        offsets_entry, counts_entry = ifd.get(offsets_tag), ifd.get(counts_tag)
        if offsets_entry is None or counts_entry is None:
            continue
        offsets = tiff.numbers(offsets_entry, _MAX_OFFSETS)
        counts = tiff.numbers(counts_entry, _MAX_OFFSETS)
        if offsets is None or counts is None:
            continue
        if len(offsets) != len(counts):
            ctx.out.finding(
                MALFORMED,
                ifd.locator,
                f"TIFF IFD {ifd.name} declares {len(offsets)} offsets and {len(counts)}"
                " byte counts",
            )
            continue
        for index, (offset, count) in enumerate(zip(offsets, counts, strict=True)):
            if not tiff.space.fits(offset, count):
                ctx.out.finding(
                    RASTER_TRUNCATED,
                    ifd.locator,
                    f"TIFF IFD {ifd.name} declares image data at [{offset}, {offset + count}),"
                    f" past the {tiff.space.size} bytes of the file",
                    {"count": count, "index": index, "offset": offset, "size": tiff.space.size},
                )
                break

"""A stored still as the container declares it, and the ``Image`` record that holds it.

Every container reader returns ``Still``s; ``emit`` turns each into one ``Image``: dimensions and
encoding from the container header, ``capture`` and ``orientation`` from the tags. The Image
cites the bytes that hold it (the whole file, or a TIFF IFD), and a region of it is that citation
followed by an ``ImageRegion`` of the stored raster (ADR 0041 §3). A raster declared larger than
``max_pixels`` is still recorded, with an ``image.pixel_limit`` finding for any later decoder.
"""

from dataclasses import dataclass
from typing import Final

from neptune.adapters.image._capture import CaptureReader, Tags, not_covered, unknown
from neptune.adapters.image._context import Context
from neptune.adapters.image._emit import OVERLAP, PIXEL_LIMIT
from neptune.model.ids import RecordId
from neptune.model.provenance import Locator
from neptune.model.world import Image

# Where capture metadata comes from: tags the file holds, a format that could hold them and
# holds none, or a format with no place for them.
TAGS: Final = "tags"
NONE: Final = "none"
NOT_COVERED: Final = "not_covered"


@dataclass(frozen=True)
class Still:
    """One stored raster: the bytes that hold it, its declared size and encoding, its tags."""

    locator: tuple[Locator, ...]
    width: int
    height: int
    encoding: str
    tags: Tags | None
    metadata: str


def emit(ctx: Context, still: Still) -> RecordId | None:
    """The ``Image`` for ``still``, with its capture clock; ``None`` if its bytes are taken."""
    out = ctx.out
    image_id = out.record_id(Image.kind, still.locator)
    if out.taken(image_id):
        out.finding(OVERLAP, still.locator, "two images are declared at the same bytes")
        return None
    if still.tags is not None:
        capture, orientation = CaptureReader(ctx, still.tags).read()
    elif still.metadata == NOT_COVERED:
        capture, orientation = not_covered()
    else:
        capture, orientation = unknown()
    image = Image(
        id=image_id,
        provenance=out.provenance(still.locator),
        width=still.width,
        height=still.height,
        encoding=still.encoding,
        orientation=orientation,
        capture=capture,
    )
    out.add(image)
    pixels = still.width * still.height
    if pixels > ctx.max_pixels:
        out.finding(
            PIXEL_LIMIT,
            still.locator,
            f"the {still.encoding} image declares {still.width} x {still.height} pixels, more than"
            f" max_pixels ({ctx.max_pixels})",
            {"height": still.height, "max_pixels": ctx.max_pixels, "width": still.width},
            (image_id,),
        )
    return image_id

"""Still images: PNG, JPEG, TIFF, BigTIFF, DNG, WebP, BMP and Netpbm, read as declared (ADR 0041).

An image is visual evidence, kept as pixels and never turned into a caption. This adapter reads
what the container declares about it and decodes no pixel:

- one ``Image`` per stored raster: its declared width, height and encoding, its EXIF
  ``Orientation`` kept and never applied, and ``capture`` from its EXIF or TIFF tags (time, GPS
  position, make, model and serial numbers; the rules are in ``_capture``). A TIFF or DNG has one
  per IFD that declares a raster, citing that IFD; every other format one, citing the file;
- one ``TimestampDomain`` per capture clock (``DateTimeOriginal``);
- ``StructuredTable`` and ``StructuredRecord`` for everything else the file declares, each table
  citing the exact bytes it reads: every TIFF/EXIF IFD (``tag``, ``type``, ``count``, then the
  values as stored: EXIF, GPS, TIFF and DNG tags alike, MakerNotes recorded opaque), every XMP
  packet (``namespace``, ``path``, ``value``), every ICC profile (``ICC header``, ``ICC tags``),
  and each fixed header structure (``IHDR``, ``SOF0``, ``VP8X``, ``BITMAPV5HEADER``, ``PNM
  header``, …) as one row whose columns are the specification's field names.

A region of an image is the ``Image``'s citation followed by an ``ImageRegion`` of the stored
raster: origin top-left, x right, y down, orientation not applied. Declared camera geometry (EXIF
FocalLength and focal-plane resolution, an XMP camera model) stays in its cited rows: which of
them form a calibration is a reading for MVL-26, and nothing is inferred here.

Every file is read in one chunk: the container's structures are small beside its pixels, which
are counted, scanned for markers or bounds-checked and never inflated. Hostile files cost bounded
work: offsets and sizes are checked before any read, IFDs are read once, zlib payloads inflate to
``max_metadata_bytes``, XMP is parsed without a DTD, and ``max_structures`` and ``max_entries``
bound what one source may walk and emit. Every problem is a finding.
"""

from typing import TYPE_CHECKING, Final

import neptune.adapters.image._bmp as _bmp
import neptune.adapters.image._jpeg as _jpeg
import neptune.adapters.image._png as _png
import neptune.adapters.image._pnm as _pnm
import neptune.adapters.image._tiff_file as _tiff_file
import neptune.adapters.image._webp as _webp
from neptune.adapters.contract import (
    ABI_VERSION,
    PROBE_HEAD_SIZE,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    ConfigOption,
    Documented,
    FormatSpec,
    InspectResult,
    Magic,
    Plan,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    Resources,
    SourceReader,
    make_chunk,
)
from neptune.adapters.image._context import Context
from neptune.adapters.image._detect import BMP, JPEG, PNG, PNM, TIFF, WEBP, detect
from neptune.adapters.image._emit import FINDINGS, UNREADABLE, Emitter
from neptune.adapters.image._space import PAYLOAD_STEP, Budget, LimitHit, Space, Truncated
from neptune.adapters.image._still import Still, emit
from neptune.model.world import Image

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject

DEFAULT_MAX_ENTRIES: Final = 20_000
DEFAULT_MAX_METADATA_BYTES: Final = 16 * 1024 * 1024
DEFAULT_MAX_PIXELS: Final = 1 << 28
DEFAULT_MAX_STRUCTURES: Final = 100_000
DEFAULT_MAX_VALUE_BYTES: Final = 4096

_READERS: Final = {
    BMP: _bmp.read,
    JPEG: _jpeg.read,
    PNG: _png.read,
    PNM: _pnm.read,
    TIFF: _tiff_file.read,
    WEBP: _webp.read,
}

DESCRIPTOR: Final = AdapterDescriptor(
    id="image",
    version="0.1.0",
    abi=ABI_VERSION,
    summary="Still images: dimensions, EXIF, XMP and ICC as declared, cited; no pixel decoded.",
    formats=(
        FormatSpec("BMP", media_types=("image/bmp",), extensions=(".bmp", ".dib")),
        FormatSpec("BigTIFF", magic=(Magic(0, b"II+\x00"),)),
        FormatSpec("BigTIFF (big-endian)", magic=(Magic(0, b"MM\x00+"),)),
        FormatSpec(
            "JPEG",
            media_types=("image/jpeg",),
            extensions=(".jfif", ".jpe", ".jpeg", ".jpg"),
            magic=(Magic(0, b"\xff\xd8\xff"),),
        ),
        FormatSpec(
            "Netpbm",
            media_types=(
                "image/x-portable-anymap",
                "image/x-portable-arbitrarymap",
                "image/x-portable-bitmap",
                "image/x-portable-graymap",
                "image/x-portable-pixmap",
            ),
            extensions=(".pam", ".pbm", ".pgm", ".pnm", ".ppm"),
        ),
        FormatSpec(
            "PNG",
            media_types=("image/apng", "image/png"),
            extensions=(".apng", ".png"),
            magic=(Magic(0, _png.SIGNATURE),),
        ),
        FormatSpec(
            "TIFF",
            media_types=("image/dng", "image/tiff"),
            extensions=(".dng", ".tif", ".tiff"),
            magic=(Magic(0, b"II*\x00"),),
        ),
        FormatSpec("TIFF (big-endian)", magic=(Magic(0, b"MM\x00*"),)),
        FormatSpec(
            "WebP",
            media_types=("image/webp",),
            extensions=(".webp",),
            magic=(Magic(0, b"RIFF"), Magic(8, b"WEBP")),
        ),
    ),
    record_kinds=("image", "structured_record", "structured_table", "timestamp_domain"),
    config=(
        ConfigOption(
            "max_entries",
            DEFAULT_MAX_ENTRIES,
            "table rows one source may emit (IFD entries, XMP values, ICC tags, structure rows);"
            " past it parsing stops with image.limit_exceeded",
        ),
        ConfigOption(
            "max_metadata_bytes",
            DEFAULT_MAX_METADATA_BYTES,
            "the largest metadata block read or inflated (an XMP packet, an ICC profile, a zlib"
            " text or profile payload)",
        ),
        ConfigOption(
            "max_pixels",
            DEFAULT_MAX_PIXELS,
            "a declared raster of more pixels is recorded with image.pixel_limit, so a later"
            " decoder refuses or bounds it",
        ),
        ConfigOption(
            "max_structures",
            DEFAULT_MAX_STRUCTURES,
            "container structures one source may walk (chunks, segments, IFDs, XML elements);"
            " past it parsing stops with image.limit_exceeded (a JPEG still looks for its frame"
            " header within that many more segments, so the image is recorded)",
        ),
        ConfigOption(
            "max_value_bytes",
            DEFAULT_MAX_VALUE_BYTES,
            "the largest IFD value copied into its row; a larger one stays cited in the bytes",
        ),
    ),
    libraries=(),
    finding_codes=tuple(
        Documented(code, f"{description} ({category}, {severity})")
        for code, (category, severity, description) in sorted(FINDINGS.items())
    ),
    locator_steps=(
        Documented(
            PAYLOAD_STEP,
            "the decoded payload of the structure the previous step cites: decode=inflate is its"
            " zlib data inflated (PNG iCCP, zTXt, compressed iTXt); decode=join is the data of"
            " the JPEG APP2 ICC_PROFILE segments in the cited range, joined in sequence order",
        ),
    ),
    conventions=(
        Documented(
            "capture",
            "Make, Model, BodySerialNumber (exif.body_serial) and DNG CameraSerialNumber"
            " (dng.camera_serial); DateTimeOriginal as civil seconds (posix with"
            " OffsetTimeOriginal, finer with SubSecTimeOriginal); GPS degrees, minutes and seconds"
            " read exactly, altitude in metres above mean sea level; crs Unknown (GPSMapDatum is"
            " text); NotCovered for BMP and Netpbm",
        ),
        Documented(
            "chunks",
            "one chunk per source, context {part: image}: a container's structures are read in"
            " one call and its pixels never decoded",
        ),
        Documented(
            "ifd_tables",
            "one table per TIFF/EXIF IFD citing its bytes, named for the pointer that declares it"
            " (IFD0, IFD1, Exif, GPS, Interop, SubIFD); one row per entry in stored order: tag,"
            " type, count, then the values (rationals as numerator, denominator; ASCII one cell"
            " per string); values over max_value_bytes and MakerNotes are not copied",
        ),
        Documented(
            "images",
            "one Image per stored raster, encoding png, jpeg, tiff, dng, webp, bmp, pbm, pgm,"
            " ppm or pam; a TIFF/DNG IFD with ImageWidth and ImageLength cites the IFD, any"
            " other image the whole file",
        ),
        Documented(
            "regions",
            "a region is the Image's citation then ImageRegion(x0, y0, x1, y1) of the stored"
            " raster: origin top-left, x right, y down, EXIF orientation not applied; a BMP's"
            " bottom-up rows still count from the top",
        ),
        Documented(
            "structure_tables",
            "a fixed header structure (IHDR, SOFn, JFIF, Adobe, VP8X, VP8, VP8L, a BMP, Netpbm or"
            " ICC header, a PNG text chunk) is a table named for it, citing its bytes, whose"
            " header is the specification's field names and whose one row is its values",
        ),
        Documented(
            "xmp_tables",
            "one table per XMP packet citing its bytes: namespace URI, XMP path with the packet's"
            " prefixes (prop, struct/field, array[i], prop/?xml:lang), value text as written",
        ),
    ),
    resources=Resources(max_memory=256 * 1024 * 1024, streaming=True),
    security=(
        "Decodes no pixels: image data is counted, scanned for markers or bounds-checked.",
        "Checks every offset and size before reading; reads each IFD once (loops are findings).",
        "Inflates zlib payloads to at most max_metadata_bytes.",
        "Parses XMP with expat, refusing any DTD (no entity expansion, no external entities),"
        " and nesting past 64 elements.",
        "Bounds structures walked and rows emitted per source (max_structures, max_entries).",
        "Never opens a file a source names (a BMP V5 linked profile).",
    ),
)


def _context(source: SourceReader, config: AdapterConfig) -> Context:
    budget = Budget(config.integer("max_structures"), config.integer("max_entries"))
    return Context(
        out=Emitter(source, config),
        budget=budget,
        whole=Space.file(source).whole(),
        max_value_bytes=config.integer("max_value_bytes"),
        max_metadata_bytes=config.integer("max_metadata_bytes"),
        max_pixels=config.integer("max_pixels"),
    )


def _read(source: SourceReader, config: AdapterConfig) -> Context:
    """Every record and finding of ``source``: the whole of this adapter's work."""
    ctx = _context(source, config)
    space = Space.file(source)
    found = detect(space.read(0, min(space.size, PROBE_HEAD_SIZE)), space.size)
    if found is None:
        ctx.out.finding(
            UNREADABLE, ctx.whole, "the bytes do not start any image format this adapter reads"
        )
        return ctx
    stills: list[Still] = []
    try:
        stills = _READERS[found.format](ctx, space)
    except LimitHit as hit:
        ctx.stopped(hit)
    except Truncated as exc:
        ctx.cut(exc, ctx.whole, f"a {found.format} structure")
    for still in stills:
        emit(ctx, still)
    return ctx


class ImageAdapter:
    """The still-image adapter. It has no planning granularity: every source is one chunk."""

    descriptor = DESCRIPTOR

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        found = detect(head, hints.size)
        if found is None:
            reason = ProbeReason("image.no_signature", "the head starts no image format read here")
            return ProbeResult(0.0, (reason,))
        return ProbeResult(found.confidence, (ProbeReason(f"image.{found.format}", found.reason),))

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        ctx = _read(source, config)
        images = [r for r in ctx.out.records if isinstance(r, Image)]
        summary: JsonObject = {
            "findings": sorted({finding.code for finding in ctx.out.findings}),
            "images": [
                {"encoding": image.encoding, "height": image.height, "width": image.width}
                for image in images
            ],
            "records": len(ctx.out.records),
            "size": source.size,
        }
        return InspectResult(summary, ctx.out.findings)

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        return Plan((make_chunk(source, config, {"part": "image"}, source.size),))

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        ctx = _read(source, config)
        return ChunkOutput(records=ctx.out.records, findings=ctx.out.findings)

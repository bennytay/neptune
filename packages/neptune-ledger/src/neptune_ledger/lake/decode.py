"""Decoders that extract a cited region from verified source bytes (ADR 0014 §4-§5).

A locator's later steps address inside what a transform decoded from the step before (root ADR
0016 §1). The Ledger decodes with its own decoders, never the compiler's adapters, so what it
extracts is a **derivative**: the ``ExtractionTransform`` (decoder, decoder version, and the
version of every library whose output it depends on) is part of each artefact's identity, and a
new library version is a new extraction lineage.

One variant per kind of artefact, chosen by the locator's innermost step:

========== ================================= ==========================================
Variant    Innermost step                    Artefact
========== ================================= ==========================================
frame      ``record_range`` (MCAP, ROS 2)    the one image message in the range, as PNG
image_region ``image_region``                the box of a stored image or frame, as PNG
page       ``page`` / ``page_region``        the page rendered by PDFium, or its box, PNG
row        ``row`` / ``row_cell``            the CSV or Parquet row or cell, as JSON
value      ``json_pointer`` / ``span``       the JSON/YAML value as JSON, or the text
========== ================================= ==========================================

Before each inner step a gzip stream is decompressed (bounded); no other container is
unwrapped. A nested ``byte_range`` slices the scope it is in and must lie inside it. Every
decoder is deterministic: same bytes, same decoder and library versions, same output bytes.
Every failure is a ``MediaFinding``: ``undecodable`` bytes, ``no_decoder`` for a step or
encoding this version cannot decode, ``invalid_request`` for a citation the bytes do not hold,
``unsafe_entry`` for a decompression or pixel bomb.
"""

import codecs
import functools
import hashlib
import io
import json
import math
import re
import threading
import zlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from importlib.metadata import version as library_version
from typing import Any, Final, Literal, TypeAlias

from neptune.identity import canonical_json
from neptune.model.provenance import (
    NO_HEADER,
    ByteRange,
    ImageRegion,
    JsonPointer,
    Locator,
    Page,
    PageRegion,
    RecordRange,
    Row,
    RowCell,
    Span,
)
from neptune_ledger.lake.evidence import MediaFinding, MediaFindingCode, ReadFailure, SourceReader

Variant: TypeAlias = Literal["bytes", "frame", "image_region", "page", "row", "value"]
VARIANTS: Final[tuple[Variant, ...]] = ("bytes", "frame", "image_region", "page", "row", "value")
DECODER_VERSION: Final = "1.0.0"
# Pages render at 2 pixels per PDF point (144 dpi).
PAGE_SCALE: Final = 2.0

_ENDS: Final[Mapping[str, tuple[type, ...]]] = {
    "frame": (RecordRange,),
    "image_region": (ImageRegion,),
    "page": (Page, PageRegion),
    "row": (Row, RowCell),
    "value": (JsonPointer, Span),
}
_LIBRARIES: Final[Mapping[str, tuple[str, ...]]] = {
    "bytes": (),
    "frame": ("lz4", "mcap", "mcap-ros2-support", "pillow", "zstandard"),
    "image_region": ("lz4", "mcap", "mcap-ros2-support", "pillow", "zstandard"),
    "page": ("pillow", "pypdfium2"),
    "row": ("pyarrow",),
    "value": ("pyyaml",),
}
_MCAP_MAGIC: Final = b"\x89MCAP0\r\n"
_IMAGE_FORMATS: Final = ("BMP", "GIF", "JPEG", "PNG", "PPM", "TIFF", "WEBP")
# ROS ``sensor_msgs/Image`` encodings: Pillow mode of the stored bytes, bytes per pixel, and the
# mode the PNG is written in.
_RAW: Final[Mapping[str, tuple[str, int, str]]] = {
    "rgb8": ("RGB", 3, "RGB"),
    "bgr8": ("BGR;24", 3, "RGB"),
    "rgba8": ("RGBA", 4, "RGBA"),
    "bgra8": ("BGRA", 4, "RGBA"),
    "mono8": ("L", 1, "L"),
    "8UC1": ("L", 1, "L"),
    "mono16": ("I;16", 2, "I;16"),
    "16UC1": ("I;16", 2, "I;16"),
}
# PDFium is not thread-safe; every call into it holds this lock.
_PDFIUM: Final = threading.Lock()


@dataclass(frozen=True)
class Limits:
    """Bounds on what one hydration may decode (hostile input, ADR 0014 §5)."""

    max_decoded_bytes: int = 256 * 1024 * 1024
    max_pixels: int = 64 * 1024 * 1024


DEFAULT_LIMITS: Final = Limits()


@dataclass(frozen=True)
class ExtractionTransform:
    """What produced an artefact: decoder id and version, and its libraries' versions."""

    decoder: str
    version: str
    libraries: tuple[tuple[str, str], ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "decoder": self.decoder,
            "libraries": dict(self.libraries),
            "version": self.version,
        }

    @property
    def id(self) -> str:
        return "sha256:" + hashlib.sha256(canonical_json.dumps(self.to_json())).hexdigest()


@functools.cache
def transform_for(variant: Variant) -> ExtractionTransform:
    """The transform this Ledger extracts ``variant`` with, from the installed libraries."""
    libraries = tuple((name, library_version(name)) for name in _LIBRARIES[variant])
    return ExtractionTransform(f"neptune_ledger.media.{variant}", DECODER_VERSION, libraries)


@dataclass(frozen=True)
class Decoded:
    """An extracted artefact before it is stored: media type, bytes, and facts about it."""

    media_type: str
    data: bytes
    metadata: dict[str, Any]


class DecodeFailure(Exception):
    """A step could not be decoded (internal; hydration reports ``finding``)."""

    def __init__(self, code: MediaFindingCode, subject: str, detail: str) -> None:
        super().__init__(detail)
        self.finding = MediaFinding(code, subject, detail[:500])


def check_variant(variant: str, steps: tuple[Locator, ...], subject: str) -> MediaFinding | None:
    """Why ``variant`` cannot be extracted from these inner steps, or None."""
    if variant not in VARIANTS:
        return MediaFinding(
            "invalid_request", subject, f"no variant {variant!r}; one of {VARIANTS}"
        )
    if variant == "bytes":
        return None
    ends = _ENDS[variant]
    if not steps or not isinstance(steps[-1], ends):
        names = " or ".join(cls.kind for cls in ends)  # type: ignore[attr-defined]
        detail = f"the {variant} variant needs a locator whose innermost step is {names}"
        return MediaFinding("invalid_request", subject, detail)
    return None


# --- Scopes ------------------------------------------------------------------------------------


class _Scope:
    """Bytes a step addresses: the verified source span, lazily, or bytes decoded from it."""

    def __init__(self, reader: SourceReader | None, data: bytes | None, limits: Limits) -> None:
        self._reader, self._data, self._limits = reader, data, limits

    @property
    def size(self) -> int:
        return self._reader.size if self._reader is not None else len(self._data or b"")

    def file(self) -> io.BufferedIOBase:
        if self._reader is not None:
            return self._reader.file(self._limits.max_decoded_bytes)
        return io.BytesIO(self._data or b"")

    def head(self, n: int) -> bytes:
        with self.file() as f:
            return f.read(n)

    def tail(self, n: int) -> bytes:
        with self.file() as f:
            f.seek(max(0, self.size - n))
            return f.read(n)

    def whole(self, subject: str, what: str) -> bytes:
        if self._data is not None:
            return self._data
        if self.size > self._limits.max_decoded_bytes:
            limit = self._limits.max_decoded_bytes
            detail = f"{what} of {self.size} bytes exceeds the {limit}-byte limit"
            raise DecodeFailure("unsafe_entry", subject, detail)
        with self.file() as f:
            return f.read()

    def decoded(self, subject: str) -> "_Scope":
        """The scope with a gzip stream decompressed; any other bytes as they are."""
        if self.head(2) != b"\x1f\x8b":
            return self
        limit = self._limits.max_decoded_bytes
        inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = bytearray()
        try:
            with self.file() as f:
                while block := f.read(min(1 << 20, max(1, limit))):
                    out += inflate.decompress(block, limit + 1 - len(out))
                    if len(out) > limit or inflate.unconsumed_tail:
                        detail = f"the gzip stream inflates past the {limit}-byte limit"
                        raise DecodeFailure("unsafe_entry", subject, detail)
                    if inflate.eof:
                        break
                out += inflate.flush()
        except zlib.error as exc:
            raise DecodeFailure("undecodable", subject, f"not a gzip stream: {exc}") from exc
        if not inflate.eof:
            raise DecodeFailure("undecodable", subject, "the gzip stream is truncated")
        return _Scope(None, bytes(out), self._limits)

    def slice(self, step: ByteRange, subject: str) -> "_Scope":
        if step.offset + step.length > self.size:
            detail = (
                f"byte range [{step.offset}, +{step.length}) lies outside its"
                f" {self.size}-byte scope"
            )
            raise DecodeFailure("invalid_request", subject, detail)
        if (
            self._reader is not None
        ):  # still lazy: an MCAP inside an archive member is not read whole
            return _Scope(self._reader.narrow(step.offset, step.length), None, self._limits)
        data = (self._data or b"")[step.offset : step.offset + step.length]
        return _Scope(None, data, self._limits)


# --- Entry point -------------------------------------------------------------------------------


def decode(
    variant: Variant,
    steps: tuple[Locator, ...],
    reader: SourceReader,
    subject: str,
    limits: Limits = DEFAULT_LIMITS,
) -> Decoded:
    """Extract ``variant`` from the span ``reader`` serves by ``steps`` (the inner steps).

    Raises ``DecodeFailure`` with the finding, or the reader's ``ReadFailure``.
    """
    scope = _Scope(reader, None, limits)
    image: tuple[Any, dict[str, Any]] | None = None
    for at, step in enumerate(steps[:-1]):
        if isinstance(step, ByteRange):
            scope = scope.decoded(subject).slice(step, subject)
        elif (
            isinstance(step, RecordRange)
            and at == len(steps) - 2
            and isinstance(steps[-1], ImageRegion)
        ):
            image = _frame(scope.decoded(subject), step, subject, limits)
        else:
            detail = f"this Ledger decodes no {step.kind} step with further steps inside it"
            raise DecodeFailure("no_decoder", subject, detail)
    last = steps[-1]
    if isinstance(last, RecordRange):
        picture, facts = _frame(scope.decoded(subject), last, subject, limits)
        return _png(picture, facts)
    if isinstance(last, ImageRegion):
        if image is None:
            image = _still(scope.decoded(subject), subject, limits)
        return _png(*_crop(*image, last, subject))
    if isinstance(last, (Page, PageRegion)):
        return _page(scope.decoded(subject), last, subject, limits)
    if isinstance(last, (Row, RowCell)):
        return _row(scope.decoded(subject), last, subject, limits)
    if isinstance(last, JsonPointer):
        return _pointer(scope.decoded(subject), last, subject)
    if isinstance(last, Span):
        return _span(scope.decoded(subject), last, subject)
    detail = f"this Ledger decodes no {last.kind} step"
    raise DecodeFailure("no_decoder", subject, detail)


def guarded(subject: str, what: str, call: Callable[[], Any]) -> Any:
    """Run a library decoder on hostile bytes: anything it raises is ``undecodable``.

    Third-party parsers raise far beyond their documented errors on hostile input (root ADR
    0029), so every exception becomes a finding; a changed source chunk and our own failures
    pass through.
    """
    try:
        return call()
    except (DecodeFailure, ReadFailure):
        raise
    except Exception as exc:
        detail = (
            f"{what}: {type(exc).__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}"
        )
        raise DecodeFailure("undecodable", subject, detail) from exc


# --- Images ------------------------------------------------------------------------------------


def _normalised(image: Any) -> Any:
    """The raster in a mode PNG stores without loss of what was decoded."""
    if image.mode in ("L", "RGB", "RGBA", "I;16"):
        return image
    if image.mode == "1":
        return image.convert("L")
    if image.mode in ("LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        return image.convert("RGBA")
    return image.convert("RGB")


def _png(image: Any, facts: dict[str, Any]) -> Decoded:
    out = io.BytesIO()
    image.save(out, format="PNG", compress_level=9, optimize=False)
    metadata = {**facts, "height": image.height, "mode": image.mode, "width": image.width}
    return Decoded("image/png", out.getvalue(), metadata)


def _open_image(data: bytes, subject: str, limits: Limits) -> Any:
    from PIL import Image

    def opened() -> Any:
        image = Image.open(io.BytesIO(data), formats=_IMAGE_FORMATS)
        width, height = image.size
        if width * height > limits.max_pixels:
            detail = f"a {width} x {height} image exceeds the {limits.max_pixels}-pixel limit"
            raise DecodeFailure("unsafe_entry", subject, detail)
        frames = getattr(image, "n_frames", 1)
        if frames != 1:
            detail = f"a {image.format} with {frames} frames; no frame is chosen silently"
            raise DecodeFailure("no_decoder", subject, detail)
        image.load()
        return image

    return guarded(subject, "not a decodable image", opened)


def _still(scope: _Scope, subject: str, limits: Limits) -> tuple[Any, dict[str, Any]]:
    image = _open_image(scope.whole(subject, "an image"), subject, limits)
    return _normalised(image), {"format": image.format}


def _crop(
    image: Any, facts: dict[str, Any], region: ImageRegion, subject: str
) -> tuple[Any, dict[str, Any]]:
    if region.x1 > image.width or region.y1 > image.height:
        detail = f"region {region.to_json()} lies outside the {image.width} x {image.height} raster"
        raise DecodeFailure("invalid_request", subject, detail)
    if region.x0 == region.x1 or region.y0 == region.y1:
        raise DecodeFailure("invalid_request", subject, "an empty region has no pixels to store")
    box = (region.x0, region.y0, region.x1, region.y1)
    made = {
        **facts,
        "region": list(box),
        "source_height": image.height,
        "source_width": image.width,
    }
    return image.crop(box), made


# --- Frames from MCAP --------------------------------------------------------------------------


def _frame(
    scope: _Scope, step: RecordRange, subject: str, limits: Limits
) -> tuple[Any, dict[str, Any]]:
    head = scope.head(len(_MCAP_MAGIC))
    if head != _MCAP_MAGIC:
        what = "a ROS 1 bag" if head.startswith(b"#ROSBAG") else "not an MCAP file"
        code: MediaFindingCode = "no_decoder" if head.startswith(b"#ROSBAG") else "undecodable"
        raise DecodeFailure(code, subject, f"a record_range frame needs MCAP; this is {what}")
    from mcap_ros2.decoder import DecoderFactory

    def messages() -> list[Any]:
        with scope.file() as f:
            return _mcap_messages(f, step, subject, limits.max_decoded_bytes)

    found = guarded(subject, "not a readable MCAP file", messages)
    if len(found) != 1:
        count = "no message" if not found else "more than one message"
        detail = (
            f"{step.channel} holds {count} in [{step.start.ticks}, {step.end.ticks});"
            " a frame is one message: cite [t, t + 1)"
        )
        raise DecodeFailure("invalid_request", subject, detail)
    schema, channel, message = found[0]
    if schema is None or channel.message_encoding != "cdr" or schema.encoding != "ros2msg":
        encoding = channel.message_encoding
        detail = f"{step.channel} is {encoding!r}; this Ledger decodes ROS 2 CDR image messages"
        raise DecodeFailure("no_decoder", subject, detail)

    def decoded() -> Any:
        return DecoderFactory().decoder_for(channel.message_encoding, schema)(message.data)  # type: ignore[misc]

    body = guarded(subject, "not a decodable ROS 2 message", decoded)
    facts: dict[str, Any] = {
        "channel": step.channel,
        "log_time": message.log_time,
        "schema": schema.name,
    }
    name = schema.name.replace("/msg/", "/")
    if name == "sensor_msgs/CompressedImage":
        image = _open_image(bytes(body.data), subject, limits)
        return _normalised(image), {**facts, "encoding": str(body.format)}
    if name == "sensor_msgs/Image":
        return _raw_image(body, subject, limits), {**facts, "encoding": str(body.encoding)}
    detail = f"{step.channel} carries {schema.name}, not an image message"
    raise DecodeFailure("no_decoder", subject, detail)


def _mcap_messages(f: Any, step: RecordRange, subject: str, limit: int) -> list[Any]:
    """Up to two ``(schema, channel, message)`` of ``step.channel`` logged in ``[start, end)``.

    With a summary, only the chunks its index says overlap the range and hold the channel are
    read; without one (a truncated recording), the file is read through once. Every chunk is
    decompressed here, bounded by ``limit`` and by its stated size, so a hostile chunk header
    cannot make the library allocate without bound.
    """
    from mcap.data_stream import ReadDataStream
    from mcap.reader import SeekingReader
    from mcap.records import Channel, Chunk, Message, Schema
    from mcap.stream_reader import StreamReader

    first, end = step.start.ticks, step.end.ticks
    found: list[Any] = []
    reader = SeekingReader(f, record_size_limit=limit)
    summary = reader.get_summary()
    if summary is not None and summary.chunk_indexes:
        wanted = {key for key, channel in summary.channels.items() if channel.topic == step.channel}
        indexes = sorted(summary.chunk_indexes, key=lambda index: index.chunk_start_offset)
        for index in indexes:
            if index.message_end_time < first or index.message_start_time >= end:
                continue
            held = index.message_index_offsets.keys()  # empty when the writer kept no index
            if not wanted or (held and not wanted & held):
                continue
            if index.chunk_length > limit:
                detail = (
                    f"an MCAP chunk of {index.chunk_length} bytes exceeds the {limit}-byte limit"
                )
                raise DecodeFailure("unsafe_entry", subject, detail)
            f.seek(index.chunk_start_offset + 1 + 8)
            for record in _chunk_records(Chunk.read(ReadDataStream(f)), subject, limit):
                if not isinstance(record, Message) or record.channel_id not in wanted:
                    continue
                if first <= record.log_time < end:
                    channel = summary.channels[record.channel_id]
                    found.append((summary.schemas.get(channel.schema_id), channel, record))
                    if len(found) > 1:
                        return found
        return found
    f.seek(0)
    schemas: dict[int, Any] = {}
    channels: dict[int, Any] = {}
    for item in StreamReader(f, emit_chunks=True, record_size_limit=limit).records:
        records = _chunk_records(item, subject, limit) if isinstance(item, Chunk) else [item]
        for record in records:
            if isinstance(record, Schema):
                schemas[record.id] = record
            elif isinstance(record, Channel):
                channels[record.id] = record
            elif isinstance(record, Message) and first <= record.log_time < end:
                known = channels.get(record.channel_id)
                if known is not None and known.topic == step.channel:
                    found.append((schemas.get(known.schema_id), known, record))
                    if len(found) > 1:
                        return found
    return found


def _chunk_records(chunk: Any, subject: str, limit: int) -> list[Any]:
    """The records of one MCAP chunk, decompressed to exactly its stated size, at most ``limit``."""
    import dataclasses

    from mcap.stream_reader import breakup_chunk

    size = chunk.uncompressed_size
    if size > limit:
        detail = f"an MCAP chunk inflating to {size} bytes exceeds the {limit}-byte limit"
        raise DecodeFailure("unsafe_entry", subject, detail)
    if chunk.compression == "":
        data = bytes(chunk.data)
    elif chunk.compression == "zstd":
        import zstandard

        out = bytearray()
        with zstandard.ZstdDecompressor().stream_reader(bytes(chunk.data)) as stream:
            while len(out) <= size and (block := stream.read(min(1 << 20, size + 1 - len(out)))):
                out += block
        data = bytes(out)
    elif chunk.compression == "lz4":
        import lz4.frame

        data = lz4.frame.LZ4FrameDecompressor().decompress(bytes(chunk.data), max_length=size + 1)
    else:
        detail = f"an MCAP chunk compressed with {chunk.compression!r}; no decoder"
        raise DecodeFailure("no_decoder", subject, detail)
    if len(data) != size:
        detail = f"an MCAP chunk inflates to {len(data)}+ bytes, not its stated {size}"
        raise DecodeFailure("undecodable", subject, detail)
    return list(breakup_chunk(dataclasses.replace(chunk, compression="", data=data)))


def _raw_image(body: Any, subject: str, limits: Limits) -> Any:
    from PIL import Image

    encoding = str(body.encoding)
    if encoding not in _RAW:
        raise DecodeFailure("no_decoder", subject, f"no decoder for image encoding {encoding!r}")
    raw_mode, depth, mode = _RAW[encoding]
    width, height, step = int(body.width), int(body.height), int(body.step)
    data = bytes(body.data)
    if width * height > limits.max_pixels:
        detail = f"a {width} x {height} image exceeds the {limits.max_pixels}-pixel limit"
        raise DecodeFailure("unsafe_entry", subject, detail)
    if not width or not height or step < width * depth or len(data) < step * height:
        detail = f"{len(data)} bytes cannot hold {height} rows of {width} {encoding} pixels"
        raise DecodeFailure("undecodable", subject, detail)
    if depth == 2 and body.is_bigendian:
        raw_mode = "I;16B"
    rows = b"".join(data[y * step : y * step + width * depth] for y in range(height))
    return Image.frombytes(mode, (width, height), rows, "raw", raw_mode)


# --- Pages from PDF ----------------------------------------------------------------------------


def _page(scope: _Scope, step: Page | PageRegion, subject: str, limits: Limits) -> Decoded:
    data = scope.whole(subject, "a PDF")
    if not data.startswith(b"%PDF-"):
        raise DecodeFailure("undecodable", subject, "a page step needs a PDF; this is not one")
    import pypdfium2 as pdfium

    index = step.index if isinstance(step, Page) else step.page

    def render() -> tuple[Any, dict[str, Any]]:
        with _PDFIUM:
            document = pdfium.PdfDocument(data)
            try:
                if index >= len(document):
                    detail = f"page {index} of a {len(document)}-page document"
                    raise DecodeFailure("invalid_request", subject, detail)
                page = document[index]
                try:
                    made: tuple[Any, dict[str, Any]] = _render(page, step, subject, limits)
                    return made
                finally:
                    page.close()
            finally:
                document.close()

    image, facts = guarded(subject, "not a renderable PDF", render)
    return _png(image, facts)


def _render(
    page: Any, step: Page | PageRegion, subject: str, limits: Limits
) -> tuple[Any, dict[str, Any]]:
    width_pt, height_pt = page.get_size()
    pixels = math.ceil(width_pt * PAGE_SCALE) * math.ceil(height_pt * PAGE_SCALE)
    if pixels > limits.max_pixels:
        detail = f"a {width_pt} x {height_pt} pt page exceeds the {limits.max_pixels}-pixel limit"
        raise DecodeFailure("unsafe_entry", subject, detail)
    rotation = int(page.get_rotation())
    image = page.render(scale=PAGE_SCALE, rotation=0, fill_color=(255, 255, 255, 255)).to_pil()
    image = image.convert("RGB")
    facts: dict[str, Any] = {
        "page": step.index if isinstance(step, Page) else step.page,
        "rotation": rotation,
        "scale": PAGE_SCALE,
    }
    if isinstance(step, Page):
        return image, facts
    left, bottom, right, top = page.get_mediabox()
    if rotation or tuple(page.get_cropbox()) != (left, bottom, right, top):
        detail = "a page_region on a rotated or cropped page is not decoded by this version"
        raise DecodeFailure("no_decoder", subject, detail)
    if step.x0 < left or step.x1 > right or step.y0 < bottom or step.y1 > top:
        mediabox = [left, bottom, right, top]
        detail = f"region {step.to_json()} lies outside the page's box {mediabox}"
        raise DecodeFailure("invalid_request", subject, detail)
    box = (
        math.floor((step.x0 - left) * PAGE_SCALE),
        math.floor((top - step.y1) * PAGE_SCALE),
        min(image.width, math.ceil((step.x1 - left) * PAGE_SCALE)),
        min(image.height, math.ceil((top - step.y0) * PAGE_SCALE)),
    )
    if box[0] >= box[2] or box[1] >= box[3]:
        raise DecodeFailure("invalid_request", subject, "an empty region has no pixels to store")
    region = [step.x0, step.y0, step.x1, step.y1]
    return image.crop(box), {**facts, "pixels": list(box), "region": region}


# --- Rows from CSV and Parquet -----------------------------------------------------------------

# The compiler's CSV grammar, mirrored because no member may import its adapters: records end at
# an LF outside quotes (a CR before it belongs to the ending), a line holding nothing is not a
# record, a field starting with '"' is quoted, a leading UTF-8 byte-order mark is skipped, and
# the delimiter is sniffed from the first 64 KiB by the fixed rule below. A row index counts
# records, the header included (``Row``).
_CSV_SNIFFED: Final = (",", "\t", ";")
_CSV_SNIFF_RECORDS: Final = 64
_CSV_SNIFF_BYTES: Final = 64 * 1024
_BOM: Final = b"\xef\xbb\xbf"
_QUOTE, _LF, _CR = 0x22, 0x0A, 0x0D
_START, _UNQUOTED, _QUOTED, _AFTER_QUOTE = range(4)


@dataclass(frozen=True)
class _CsvRecord:
    content: bytes  # without its ending
    fields: int
    ended: bool  # by an LF, not by the end of the bytes
    unterminated: bool  # the bytes end inside a quoted field


def _csv_records(data: bytes, delimiter: str) -> Iterator[_CsvRecord]:
    mark = ord(delimiter)
    stop = re.compile(b"[" + re.escape(delimiter.encode()) + b"\n]")
    start, fields, state, i, n = 0, 1, _START, 0, len(data)
    while i < n:
        if state == _QUOTED:
            j = data.find(b'"', i)
            if j < 0:
                break
            state, i = _AFTER_QUOTE, j + 1
            continue
        if state == _UNQUOTED:
            found = stop.search(data, i)
            if found is None:
                break
            i = found.start()
        byte = data[i]
        if byte == _LF:
            end = i - 1 if i > start and data[i - 1] == _CR else i
            if end > start:
                yield _CsvRecord(data[start:end], fields, True, False)
            start, fields, state = i + 1, 1, _START
        elif byte == mark:
            fields, state = fields + 1, _START
        elif byte == _QUOTE:
            state = _QUOTED
        else:
            state = _UNQUOTED
        i += 1
    if n > start:
        yield _CsvRecord(data[start:], fields, False, state == _QUOTED)


def _csv_split(content: bytes, delimiter: str) -> list[bytes]:
    """A record's fields: quoted fields unquoted, text after a closing quote kept."""
    mark = delimiter.encode()
    if b'"' not in content:
        return content.split(mark)
    fields: list[bytes] = []
    i, n = 0, len(content)
    while True:
        if i < n and content[i] == _QUOTE:
            held = bytearray()
            i += 1
            while True:
                j = content.find(b'"', i)
                if j < 0:  # unterminated: the field runs to the end
                    fields.append(bytes(held + content[i:]))
                    return fields
                held += content[i:j]
                if j + 1 < n and content[j + 1] == _QUOTE:
                    held += b'"'
                    i = j + 2
                    continue
                i = j + 1
                break
            k = content.find(mark, i)
            held += content[i : n if k < 0 else k]
            fields.append(bytes(held))
        else:
            k = content.find(mark, i)
            fields.append(content[i:] if k < 0 else content[i:k])
        if k < 0:
            return fields
        i = k + 1


def _csv_sniff(data: bytes) -> tuple[str, str]:
    """The delimiter the head's records agree on, as the compiler sniffs it, and the rule used."""
    head = data[:_CSV_SNIFF_BYTES]
    complete = len(head) == len(data)
    sample = head[len(_BOM) :] if head.startswith(_BOM) else head
    best: tuple[str, int] | None = None
    for delimiter in _CSV_SNIFFED:
        counts: list[int] = []
        for record in _csv_records(sample, delimiter):
            if record.unterminated or (not record.ended and not complete):
                break
            counts.append(record.fields)
            if len(counts) == _CSV_SNIFF_RECORDS:
                break
        agree = len(counts) >= 2 and counts[0] >= 2 and len(set(counts)) == 1
        if agree and (best is None or counts[0] > best[1]):
            best = (delimiter, counts[0])
    return (best[0], "sniffed") if best else (",", "default")


def _row(scope: _Scope, step: Row | RowCell, subject: str, limits: Limits) -> Decoded:
    if scope.size >= 12 and scope.head(4) == b"PAR1" and scope.tail(4) == b"PAR1":
        made = guarded(
            subject,
            "not a readable Parquet file",
            lambda: _parquet_row(scope, step, subject, limits),
        )
    else:
        made = _csv_row(scope.whole(subject, "a table"), step, subject)
    if isinstance(made, MediaFinding):
        raise DecodeFailure(made.code, made.subject, made.detail)
    facts = {k: made[k] for k in ("delimiter", "delimiter_rule", "format") if k in made}
    return Decoded("application/json", canonical_json.dumps(made), facts)


def _cell_column(
    step: Row | RowCell, cells: int, names: list[str | None], subject: str
) -> MediaFinding | None:
    """A row_cell's column must be in the row, and a stated name must be the column's name."""
    if not isinstance(step, RowCell):
        return None
    if step.column >= cells:
        return MediaFinding(
            "invalid_request", subject, f"no column {step.column} in row {step.row}"
        )
    if step.column_name is not NO_HEADER and (
        step.column >= len(names) or step.column_name != names[step.column]
    ):
        named = repr(names[step.column]) if step.column < len(names) else "nothing"
        detail = f"column {step.column} is named {named}, the citation says {step.column_name!r}"
        return MediaFinding("invalid_request", subject, detail)
    return None


def _csv_text(field: bytes) -> str | None:
    try:
        return field.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _csv_row(data: bytes, step: Row | RowCell, subject: str) -> dict[str, Any] | MediaFinding:
    if b"\x00" in data:  # the compiler's tabular probe claims text only
        return MediaFinding("undecodable", subject, "neither Parquet nor text: it holds NUL bytes")
    delimiter, rule = _csv_sniff(data)
    body = data[len(_BOM) :] if data.startswith(_BOM) else data
    header: list[str | None] = []
    for index, record in enumerate(_csv_records(body, delimiter)):
        cells = _csv_split(record.content, delimiter)
        if index == 0:
            header = [_csv_text(cell) for cell in cells]
        if index < step.row:
            continue
        problem = _cell_column(step, len(cells), header, subject)
        if problem is not None:
            return problem
        out: dict[str, Any] = {
            "delimiter": delimiter,
            "delimiter_rule": rule,
            "format": "csv",
            "row": step.row,
        }
        texts = [_csv_text(cell) for cell in cells]
        shown = [
            {"hex": cell.hex()} if text is None else text
            for cell, text in zip(cells, texts, strict=True)
        ]
        if isinstance(step, RowCell):
            out |= {"cell": shown[step.column], "column": step.column}
        else:
            out["cells"] = shown
        return out
    return MediaFinding("invalid_request", subject, f"the table has no row {step.row}")


def _parquet_row(
    scope: _Scope, step: Row | RowCell, subject: str, limits: Limits
) -> dict[str, Any] | MediaFinding:
    import pyarrow.parquet as pq

    with scope.file() as f:
        parquet = pq.ParquetFile(f)
        meta = parquet.metadata
        if step.row >= meta.num_rows:
            return MediaFinding("invalid_request", subject, f"the table has no row {step.row}")
        names: list[str | None] = list(parquet.schema_arrow.names)
        problem = _cell_column(step, len(names), names, subject)
        if problem is not None:
            return problem
        first = 0
        for group in range(meta.num_row_groups):
            rows = meta.row_group(group).num_rows
            if step.row < first + rows:
                break
            first += rows
        else:
            detail = f"the footer states {meta.num_rows} rows; its row groups hold {first}"
            return MediaFinding("undecodable", subject, detail)
        size, limit = meta.row_group(group).total_byte_size, limits.max_decoded_bytes
        if size > limit:
            detail = f"row group {group} of {size} bytes exceeds the {limit}-byte limit"
            return MediaFinding("unsafe_entry", subject, detail)
        table = parquet.read_row_group(group)
        if table.num_rows != rows:
            detail = f"row group {group} holds {table.num_rows} rows, its footer states {rows}"
            return MediaFinding("undecodable", subject, detail)
        record = table.slice(step.row - first, 1)
        columns = [step.column] if isinstance(step, RowCell) else range(len(names))
        cells = []
        for column in columns:
            field = table.schema.field(column)
            value = record.column(column).to_pylist()[0]
            cell: dict[str, Any] = {"column": column, "name": field.name, "type": str(field.type)}
            cell |= {"null": True} if value is None else {"value": _json_value(value)}
            cells.append(cell)
    return {"cells": cells, "format": "parquet", "row": step.row}


def _json_value(value: Any) -> Any:
    """A Parquet or YAML value as canonical JSON: no null, no non-finite float."""
    if value is None:
        return {"null": True}
    if isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"float": repr(value)}
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, list | tuple):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    return {"text": str(value)}


# --- Values from JSON or YAML, and text spans --------------------------------------------------


# Byte-order marks, longest first (a UTF-32 LE mark begins with the UTF-16 LE one), as the
# compiler's text and structured readers take them: the mark names the encoding and is not text.
_MARKS: Final = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def _text(data: bytes, subject: str) -> str:
    """The text of a document: UTF-8 unless a byte-order mark names another encoding."""
    encoding, skip = "utf-8", 0
    for mark, named in _MARKS:
        if data.startswith(mark):
            encoding, skip = named, len(mark)
            break
    try:
        return data[skip:].decode(encoding)
    except UnicodeDecodeError as exc:
        raise DecodeFailure("undecodable", subject, f"not {encoding} text: {exc}") from exc


class _Keys(dict[str, Any]):
    """A JSON object; ``repeated`` names the keys it holds more than once."""

    repeated: frozenset[str] = frozenset()


def _pairs(pairs: list[tuple[str, Any]]) -> _Keys:
    made = _Keys(pairs)
    if len(made) != len(pairs):
        names = [name for name, _ in pairs]
        made.repeated = frozenset(name for name in made if names.count(name) > 1)
    return made


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _pointer(scope: _Scope, step: JsonPointer, subject: str) -> Decoded:
    """A JSON value as canonical JSON, or a YAML node as the text it is written as.

    JSON is typed by its grammar, so its value is re-encoded canonically. A YAML scalar's type
    depends on the YAML version and schema a reader assumes, so a YAML node is returned verbatim:
    the document's own text from the node's first character to its last. Keys are matched by
    their text, as the compiler cites them. A key held twice on the path is ambiguous.
    """
    text = _text(scope.whole(subject, "a document"), subject)
    tokens = [t.replace("~1", "/").replace("~0", "~") for t in step.pointer.split("/")[1:]]
    try:
        document = json.loads(text, object_pairs_hook=_pairs, parse_constant=_refuse_constant)
    except ValueError:
        return _yaml_pointer(text, tokens, step.pointer, subject)
    except RecursionError as exc:
        raise DecodeFailure("undecodable", subject, "a JSON document nested too deep") from exc
    value = document
    for name in tokens:
        if isinstance(value, _Keys) and name in value.repeated:
            detail = f"{step.pointer} passes key {name!r}, which the object holds more than once"
            raise DecodeFailure("invalid_request", subject, detail)
        if isinstance(value, dict) and name in value:
            value = value[name]
        elif isinstance(value, list) and _is_index(name) and int(name) < len(value):
            value = value[int(name)]
        else:
            detail = f"{step.pointer} does not resolve in the json document"
            raise DecodeFailure("invalid_request", subject, detail)
    made = canonical_json.dumps(_json_value(value))
    return Decoded("application/json", made, {"format": "json", "pointer": step.pointer})


def _is_index(token: str) -> bool:
    return token.isascii() and token.isdigit() and (token == "0" or token[0] != "0")


def _yaml_pointer(text: str, tokens: list[str], pointer: str, subject: str) -> Decoded:
    import yaml

    class _NoAliases(yaml.SafeLoader):
        """Composes nodes only, never constructs a value, and refuses aliases."""

        def compose_node(self, parent: Any, index: Any) -> Any:
            if self.check_event(yaml.AliasEvent):
                raise yaml.YAMLError("aliases are refused")
            return super().compose_node(parent, index)

    def compose() -> Any:
        return yaml.compose(text, Loader=_NoAliases)

    node = guarded(subject, "neither JSON nor single-document YAML", compose)
    for name in tokens:
        if isinstance(node, yaml.MappingNode):
            held = [v for k, v in node.value if isinstance(k, yaml.ScalarNode) and k.value == name]
            if len(held) > 1:
                detail = f"{pointer} passes key {name!r}, which the mapping holds more than once"
                raise DecodeFailure("invalid_request", subject, detail)
            node = held[0] if held else None
        elif (
            isinstance(node, yaml.SequenceNode) and _is_index(name) and int(name) < len(node.value)
        ):
            node = node.value[int(name)]
        else:
            node = None
        if node is None:
            break
    if node is None:
        raise DecodeFailure(
            "invalid_request", subject, f"{pointer} does not resolve in the yaml document"
        )
    written = text[node.start_mark.index : node.end_mark.index]
    metadata = {"format": "yaml", "pointer": pointer, "tag": str(node.tag)}
    return Decoded("application/yaml", written.encode("utf-8"), metadata)


def _span(scope: _Scope, step: Span, subject: str) -> Decoded:
    text = _text(scope.whole(subject, "a text"), subject)
    if step.end > len(text):
        detail = f"span [{step.start}, {step.end}) is past the text's {len(text)} code points"
        raise DecodeFailure("invalid_request", subject, detail)
    made = text[step.start : step.end].encode("utf-8")
    metadata = {"end": step.end, "format": "text", "start": step.start}
    return Decoded("text/plain; charset=utf-8", made, metadata)

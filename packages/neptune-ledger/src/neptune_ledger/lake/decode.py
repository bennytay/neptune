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

import bz2
import codecs
import decimal
import functools
import hashlib
import io
import json
import lzma
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
    # A frame is cited by its time (record_range) or, as the compiler cites a message, by the
    # byte_range of its Message record (inside a Chunk's records when chunked).
    "frame": (RecordRange, ByteRange),
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
    # A JSON or YAML document a pointer is resolved in: the compiler's own limit for parsed
    # documents (its config and calibration adapters' ``max_bytes``).
    max_document_bytes: int = 8 * 1024 * 1024
    # It nests at most as deep as the compiler's config adapter reads (``max_depth``).
    max_document_depth: int = 200
    # PyYAML's pure-Python parser yields about 120 000 events a second, so a YAML document of
    # more events than this is refused rather than walked for over ~10 s. JSON's walk of a
    # whole 8 MiB document takes under 4 s, so it needs no budget.
    max_yaml_events: int = 1_000_000


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
    """Why ``variant`` cannot be extracted by a locator of these steps (all of them), or None."""
    if variant not in VARIANTS:
        return MediaFinding(
            "invalid_request", subject, f"no variant {variant!r}; one of {VARIANTS}"
        )
    if variant == "bytes":
        beyond = [step.kind for step in steps if not isinstance(step, ByteRange)]
        if beyond:
            detail = (
                f"bytes serves what byte_range steps address; this locator also has {beyond[0]},"
                " which a decoding variant reads"
            )
            return MediaFinding("invalid_request", subject, detail)
        return None
    ends = _ENDS[variant]
    if not steps or not isinstance(steps[-1], ends):
        names = " or ".join(cls.kind for cls in ends)  # type: ignore[attr-defined]
        detail = f"the {variant} variant needs a locator whose innermost step is {names}"
        return MediaFinding("invalid_request", subject, detail)
    return None


# --- Scopes ------------------------------------------------------------------------------------


class _Scope:
    """Bytes a step addresses: the verified source span, lazily, or bytes decoded from it.

    ``origin`` is the whole source: when it is an MCAP file, a scope that is exactly one Chunk
    record is that chunk's records once decoded (root ADR 0034: the compiler cites a record
    inside a chunk as a byte_range of the Chunk record, then one in its uncompressed records).
    ``inflated`` names each decompression applied on the way here.
    """

    def __init__(
        self,
        reader: SourceReader | None,
        data: bytes | None,
        limits: Limits,
        *,
        origin: "_Origin | None" = None,
        inflated: tuple[str, ...] = (),
    ) -> None:
        self._reader, self._data, self._limits = reader, data, limits
        self.origin, self.inflated = origin, inflated

    @property
    def reader(self) -> SourceReader | None:
        """The verified reader when no decompression has happened, else None."""
        return self._reader

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

    def whole(self, subject: str, what: str, limit: int | None = None) -> bytes:
        limit = self._limits.max_decoded_bytes if limit is None else limit
        if self.size > limit:
            detail = f"{what} of {self.size} bytes exceeds the {limit}-byte limit"
            raise DecodeFailure("unsafe_entry", subject, detail)
        if self._data is not None:
            return self._data
        with self.file() as f:
            return f.read()

    def _made(self, data: bytes, how: str) -> "_Scope":
        return _Scope(None, data, self._limits, origin=self.origin, inflated=(*self.inflated, how))

    def decoded(self, subject: str) -> "_Scope":
        """The scope a further step addresses inside: an MCAP chunk's records, or a gzip, bzip2,
        xz or zstd stream decompressed (bounded by ``max_decoded_bytes``); else as it is."""
        head = self.head(min(self.size, 4096))
        if head[:1] == b"\x06" and self.origin is not None and self.origin.is_mcap:
            chunk = _chunk_head(head, self.size)
            if chunk is not None:
                return self._chunk(chunk, subject)
        for magic, how in _STREAMS:
            if head.startswith(magic):
                return self._made(self._inflate_stream(how, subject), how)
        return self

    def _chunk(self, chunk: "_ChunkHead", subject: str) -> "_Scope":
        limit = self._limits.max_decoded_bytes
        if chunk.uncompressed_size > limit:
            detail = (
                f"an MCAP chunk inflating to {chunk.uncompressed_size} bytes exceeds the"
                f" {limit}-byte limit"
            )
            raise DecodeFailure("unsafe_entry", subject, detail)
        if chunk.compression == "":
            if chunk.records_length != chunk.uncompressed_size:
                detail = "an uncompressed MCAP chunk whose records are not its stated size"
                raise DecodeFailure("undecodable", subject, detail)
            # Still lazy and verified per source chunk: the records are a range of the scope.
            return self.slice(ByteRange(chunk.records_offset, chunk.records_length), subject)
        stored = self.slice(ByteRange(chunk.records_offset, chunk.records_length), subject)
        data = stored.whole(subject, "an MCAP chunk's stored records")
        records = _inflate(chunk.compression, data, chunk.uncompressed_size, subject)
        if chunk.uncompressed_crc and zlib.crc32(records) != chunk.uncompressed_crc:
            detail = "an MCAP chunk's records do not match its stated CRC"
            raise DecodeFailure("undecodable", subject, detail)
        return self._made(records, f"mcap-chunk:{chunk.compression}")

    def _inflate_stream(self, how: str, subject: str) -> bytes:
        limit = self._limits.max_decoded_bytes
        if how == "gzip":
            inflate: Any = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif how == "bzip2":
            inflate = bz2.BZ2Decompressor()
        elif how == "xz":
            inflate = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
        else:
            inflate = None
        if inflate is None:  # zstd: one frame, bounded
            data = self.whole(subject, "a zstd stream")
            return _inflate_zstd(data, limit, subject, exact=None)
        out = bytearray()
        try:
            with self.file() as f:
                while block := f.read(min(1 << 20, max(1, limit))):
                    if how == "gzip":
                        out += inflate.decompress(block, limit + 1 - len(out))
                        pending = bool(inflate.unconsumed_tail)
                    else:
                        out += inflate.decompress(block, max_length=limit + 1 - len(out))
                        pending = not inflate.needs_input and not inflate.eof
                    if len(out) > limit or pending:
                        detail = f"the {how} stream inflates past the {limit}-byte limit"
                        raise DecodeFailure("unsafe_entry", subject, detail)
                    if inflate.eof:
                        break
                if how == "gzip":
                    out += inflate.flush()
        except (zlib.error, OSError, lzma.LZMAError, EOFError) as exc:
            raise DecodeFailure("undecodable", subject, f"not a {how} stream: {exc}") from exc
        if not inflate.eof:
            raise DecodeFailure("undecodable", subject, f"the {how} stream is truncated")
        return bytes(out)

    def slice(self, step: ByteRange, subject: str) -> "_Scope":
        if step.offset + step.length > self.size:
            detail = (
                f"byte range [{step.offset}, +{step.length}) lies outside its"
                f" {self.size}-byte scope"
            )
            raise DecodeFailure("invalid_request", subject, detail)
        if self._reader is not None:  # still lazy: an MCAP inside an archive member is not read
            narrowed = self._reader.narrow(step.offset, step.length)
            return _Scope(narrowed, None, self._limits, origin=self.origin, inflated=self.inflated)
        data = (self._data or b"")[step.offset : step.offset + step.length]
        return _Scope(None, data, self._limits, origin=self.origin, inflated=self.inflated)


class _Origin:
    """The whole source a span lies in, read only when a decoder needs more than the span."""

    def __init__(self, reader: SourceReader, limits: Limits) -> None:
        self.scope = _Scope(reader, None, limits)

    @functools.cached_property
    def is_mcap(self) -> bool:
        return self.scope.head(len(_MCAP_MAGIC)) == _MCAP_MAGIC


# Streams decompressed before a further step, by their magic.
_STREAMS: Final = (
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
)


@dataclass(frozen=True)
class _ChunkHead:
    """An MCAP Chunk record's fields before its records (MCAP spec, opcode 0x06)."""

    uncompressed_size: int
    uncompressed_crc: int
    compression: str
    records_offset: int
    records_length: int


def _chunk_head(head: bytes, size: int) -> _ChunkHead | None:
    """The Chunk record a scope of ``size`` bytes is exactly, from its first bytes, or None."""
    if len(head) < 9 + 8 * 3 + 4 + 4 or head[0] != 0x06:
        return None
    if int.from_bytes(head[1:9], "little") != size - 9:
        return None
    at = 9 + 16
    uncompressed = int.from_bytes(head[at : at + 8], "little")
    crc = int.from_bytes(head[at + 8 : at + 12], "little")
    name_length = int.from_bytes(head[at + 12 : at + 16], "little")
    at += 16
    if at + name_length + 8 > len(head):
        return None
    try:
        compression = head[at : at + name_length].decode("utf-8")
    except UnicodeDecodeError:
        return None
    at += name_length
    records_length = int.from_bytes(head[at : at + 8], "little")
    if at + 8 + records_length != size:
        return None
    return _ChunkHead(uncompressed, crc, compression, at + 8, records_length)


def _inflate(compression: str, data: bytes, size: int, subject: str) -> bytes:
    """An MCAP chunk's records: exactly ``size`` bytes, never more than ``size + 1`` produced."""
    if compression == "zstd":
        return _inflate_zstd(data, size, subject, exact=size)
    if compression == "lz4":
        import lz4.frame

        def run() -> bytes:
            made: bytes = lz4.frame.LZ4FrameDecompressor().decompress(data, max_length=size + 1)
            return made

        out = guarded(subject, "not an lz4 frame", run)
    elif compression == "":
        out = data
    else:
        detail = f"an MCAP chunk compressed with {compression!r}; no decoder"
        raise DecodeFailure("no_decoder", subject, detail)
    if len(out) != size:
        more = "+" if len(out) > size else ""
        detail = f"an MCAP chunk inflates to {len(out)}{more} bytes, not its stated {size}"
        raise DecodeFailure("undecodable", subject, detail)
    return bytes(out)


def _inflate_zstd(data: bytes, limit: int, subject: str, *, exact: int | None) -> bytes:
    import zstandard

    def run() -> bytes:
        out = bytearray()
        with zstandard.ZstdDecompressor().stream_reader(data) as stream:
            while len(out) <= limit and (block := stream.read(min(1 << 20, limit + 1 - len(out)))):
                out += block
        return bytes(out)

    out = guarded(subject, "not a zstd stream", run)
    if exact is None and len(out) > limit:
        detail = f"the zstd stream inflates past the {limit}-byte limit"
        raise DecodeFailure("unsafe_entry", subject, detail)
    if exact is not None and len(out) != exact:
        more = "+" if len(out) > exact else ""
        detail = f"an MCAP chunk inflates to {len(out)}{more} bytes, not its stated {exact}"
        raise DecodeFailure("undecodable", subject, detail)
    return out  # type: ignore[no-any-return]


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
    origin = _Origin(reader.whole(), limits)
    scope = _Scope(reader, None, limits, origin=origin)
    if variant == "frame" and (not steps or isinstance(steps[-1], ByteRange)):
        picture, facts = _message_frame(_walk(scope, steps, subject), origin, subject, limits)
        return _png(picture, facts)
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
        if image is None and _message_head(scope) is not None and origin.is_mcap:
            image = _message_frame(scope, origin, subject, limits)
        elif image is None:
            image = _still(scope.decoded(subject), subject, limits)
        return _png(*_crop(*image, last, subject))
    if isinstance(last, (Page, PageRegion)):
        return _page(scope.decoded(subject), last, subject, limits)
    if isinstance(last, (Row, RowCell)):
        return _row(scope.decoded(subject), last, subject, limits)
    if isinstance(last, JsonPointer):
        return _pointer(scope.decoded(subject), last, subject, limits)
    if isinstance(last, Span):
        return _span(scope.decoded(subject), last, subject)
    detail = f"this Ledger decodes no {last.kind} step"
    raise DecodeFailure("no_decoder", subject, detail)


def _walk(scope: _Scope, steps: tuple[Locator, ...], subject: str) -> _Scope:
    """The scope a chain of byte_range steps addresses, each inside what the last decodes to."""
    for step in steps:
        if not isinstance(step, ByteRange):
            detail = f"only byte_range steps are followed here, not {step.kind}"
            raise DecodeFailure("no_decoder", subject, detail)
        scope = scope.decoded(subject).slice(step, subject)
    return scope


@dataclass(frozen=True)
class CitedBytes:
    """The bytes every byte_range step of a locator addresses (the ``bytes`` variant).

    ``reader`` serves them lazily from the source when no step needed decompression; ``data``
    holds them when one did, and ``inflated`` names each decompression (``mcap-chunk:zstd``,
    ``gzip``), so a reader knows they are decoded from the stored bytes, not stored as such.
    """

    reader: SourceReader | None
    data: bytes | None
    inflated: tuple[str, ...]


def cited_bytes(
    steps: tuple[Locator, ...], reader: SourceReader, subject: str, limits: Limits = DEFAULT_LIMITS
) -> CitedBytes:
    """Follow every inner byte_range step; raises ``DecodeFailure`` or ``ReadFailure``."""
    origin = _Origin(reader.whole(), limits)
    scope = _walk(_Scope(reader, None, limits, origin=origin), steps, subject)
    if scope.reader is not None:
        return CitedBytes(scope.reader, None, scope.inflated)
    return CitedBytes(None, scope.whole(subject, "the cited bytes"), scope.inflated)


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
    return _image_message(schema, channel, bytes(message.data), message.log_time, subject, limits)


def _image_message(
    schema: Any, channel: Any, payload: bytes, log_time: int, subject: str, limits: Limits
) -> tuple[Any, dict[str, Any]]:
    """A ROS 2 CDR ``sensor_msgs`` image message's raster, and facts about it."""
    from mcap_ros2.decoder import DecoderFactory

    if schema is None or channel.message_encoding != "cdr" or schema.encoding != "ros2msg":
        encoding = channel.message_encoding
        detail = f"{channel.topic} is {encoding!r}; this Ledger decodes ROS 2 CDR image messages"
        raise DecodeFailure("no_decoder", subject, detail)

    def decoded() -> Any:
        return DecoderFactory().decoder_for(channel.message_encoding, schema)(payload)  # type: ignore[misc]

    body = guarded(subject, "not a decodable ROS 2 message", decoded)
    facts: dict[str, Any] = {"channel": channel.topic, "log_time": log_time, "schema": schema.name}
    name = schema.name.replace("/msg/", "/")
    if name == "sensor_msgs/CompressedImage":
        image = _open_image(bytes(body.data), subject, limits)
        return _normalised(image), {**facts, "encoding": str(body.format)}
    if name == "sensor_msgs/Image":
        return _raw_image(body, subject, limits), {**facts, "encoding": str(body.encoding)}
    detail = f"{channel.topic} carries {schema.name}, not an image message"
    raise DecodeFailure("no_decoder", subject, detail)


@dataclass(frozen=True)
class _MessageHead:
    channel_id: int
    log_time: int


def _message_head(scope: _Scope) -> _MessageHead | None:
    """The Message record (MCAP opcode 0x05) a scope is exactly, or None."""
    head = scope.head(31)
    if len(head) < 31 or head[0] != 0x05 or int.from_bytes(head[1:9], "little") != scope.size - 9:
        return None
    return _MessageHead(int.from_bytes(head[9:11], "little"), int.from_bytes(head[15:23], "little"))


def _message_frame(
    scope: _Scope, origin: "_Origin", subject: str, limits: Limits
) -> tuple[Any, dict[str, Any]]:
    """The image in the Message record a scope is, decoded with its channel's schema, which is
    looked up in the whole recording: its summary, else one bounded pass over its records."""
    message = _message_head(scope)
    if message is None or not origin.is_mcap:
        detail = "a frame cited by byte ranges must address one MCAP Message record"
        raise DecodeFailure("invalid_request", subject, detail)
    payload = scope.whole(subject, "an MCAP message")[31:]

    def declared() -> tuple[Any, Any]:
        with origin.scope.file() as f:
            return _declarations(f, message.channel_id, subject, limits.max_decoded_bytes)

    schema, channel = guarded(subject, "not a readable MCAP file", declared)
    if channel is None:
        detail = f"the recording declares no channel {message.channel_id}"
        raise DecodeFailure("undecodable", subject, detail)
    return _image_message(schema, channel, payload, message.log_time, subject, limits)


def _declarations(f: Any, channel_id: int, subject: str, limit: int) -> tuple[Any, Any]:
    """``(schema, channel)`` of one channel id: from the summary, else the first declarations."""
    from mcap.reader import SeekingReader
    from mcap.records import Channel, Chunk, Schema
    from mcap.stream_reader import StreamReader

    summary = SeekingReader(f, record_size_limit=limit).get_summary()
    if summary is not None and channel_id in summary.channels:
        channel = summary.channels[channel_id]
        return summary.schemas.get(channel.schema_id), channel
    f.seek(0)
    schemas: dict[int, Any] = {}
    found: Any = None
    for item in StreamReader(f, emit_chunks=True, record_size_limit=limit).records:
        records = _chunk_records(item, subject, limit) if isinstance(item, Chunk) else [item]
        for record in records:
            if isinstance(record, Schema):
                schemas.setdefault(record.id, record)
            elif isinstance(record, Channel) and record.id == channel_id and found is None:
                found = record
            if found is not None and (found.schema_id == 0 or found.schema_id in schemas):
                return schemas.get(found.schema_id), found
    return (schemas.get(found.schema_id) if found is not None else None), found


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
    data = _inflate(chunk.compression, bytes(chunk.data), size, subject)
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
    if scope.size >= 12 and scope.head(4) == b"PAR1" and scope.tail(4) in (b"PAR1", b"PARE"):
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


# The compiler's Parquet guards (root ADR 0042), mirrored: a footer is bounded and every column
# chunk read must lie before it and decode to at most ``max_decoded_bytes`` as the footer states.
_PARQUET_MAGIC: Final = b"PAR1"
_PARQUET_ENCRYPTED: Final = b"PARE"
_MAX_FOOTER_BYTES: Final = 16 * 1024 * 1024
_MAX_LEAF_COLUMNS: Final = 16384
# Rows are decoded in batches of this many, only the cited row group's cited columns, so a row
# costs at most one batch of those columns, never the whole group.
_PARQUET_BATCH_ROWS: Final = 1024


def _parquet_row(
    scope: _Scope, step: Row | RowCell, subject: str, limits: Limits
) -> dict[str, Any] | MediaFinding:
    """A row (one cell per leaf column, as the compiler's header lists them) or one cell."""
    import pyarrow.parquet as pq

    size = scope.size
    tail = scope.tail(8)
    declared = int.from_bytes(tail[:4], "little")
    if tail[4:] == _PARQUET_ENCRYPTED:
        return MediaFinding("no_decoder", subject, "an encrypted Parquet footer is not read")
    if declared > size - 12:
        detail = f"the footer declares {declared} bytes, more than the file holds before it"
        return MediaFinding("undecodable", subject, detail)
    if declared > _MAX_FOOTER_BYTES:
        detail = f"the footer declares {declared} bytes, over {_MAX_FOOTER_BYTES}"
        return MediaFinding("unsafe_entry", subject, detail)
    footer_start = size - 8 - declared
    with scope.file() as f:
        parquet = pq.ParquetFile(
            f,
            pre_buffer=False,
            buffer_size=1 << 20,
            thrift_string_size_limit=_MAX_FOOTER_BYTES,
            arrow_extensions_enabled=False,
        )
        meta = parquet.metadata
        leaves: list[str | None] = [meta.schema.column(i).path for i in range(meta.num_columns)]
        if len(leaves) > _MAX_LEAF_COLUMNS:
            detail = f"the schema declares {len(leaves)} leaf columns, over {_MAX_LEAF_COLUMNS}"
            return MediaFinding("unsafe_entry", subject, detail)
        if step.row >= meta.num_rows:
            return MediaFinding("invalid_request", subject, f"the table has no row {step.row}")
        problem = _cell_column(step, len(leaves), leaves, subject)
        if problem is not None:
            return problem
        first = 0
        for group in range(meta.num_row_groups):
            rows = meta.row_group(group).num_rows
            if rows < 0:
                return MediaFinding(
                    "undecodable", subject, f"row group {group} declares {rows} rows"
                )
            if step.row < first + rows:
                break
            first += rows
        else:
            detail = f"the footer states {meta.num_rows} rows; its row groups hold {first}"
            return MediaFinding("undecodable", subject, detail)
        columns = [step.column] if isinstance(step, RowCell) else list(range(len(leaves)))
        tops = {str(leaves[c]).split(".")[0] for c in columns}
        read = [i for i, leaf in enumerate(leaves) if str(leaf).split(".")[0] in tops]
        problem = _check_group(meta.row_group(group), group, read, footer_start, subject, limits)
        if problem is None:
            problem = _check_batch(meta, group, read, rows, subject, limits)
        if problem is not None:
            return problem
        # Byte arrays stay dictionary-encoded, so a value repeated over many rows is held once,
        # never once per row of a batch; only the cited row is decoded.
        dictionary = [
            str(leaves[i]) for i in read if meta.schema.column(i).physical_type == "BYTE_ARRAY"
        ]
        parquet = pq.ParquetFile(
            f,
            metadata=meta,
            read_dictionary=dictionary or None,
            pre_buffer=False,
            buffer_size=1 << 20,
            thrift_string_size_limit=_MAX_FOOTER_BYTES,
            arrow_extensions_enabled=False,
        )
        record = _parquet_record(
            parquet, group, rows, step.row - first, columns, leaves, subject, limits
        )
        if isinstance(record, MediaFinding):
            return record
    return {"cells": record, "format": "parquet", "row": step.row}


def _check_group(
    meta: Any, group: int, columns: list[int], footer_start: int, subject: str, limits: Limits
) -> MediaFinding | None:
    if meta.num_columns <= max(columns, default=-1):
        detail = f"row group {group} declares {meta.num_columns} column chunks"
        return MediaFinding("undecodable", subject, detail)
    total = 0
    for column in columns:
        chunk = meta.column(column)
        dictionary = chunk.dictionary_page_offset if chunk.has_dictionary_page else None
        start = (
            dictionary if isinstance(dictionary, int) and dictionary > 0 else chunk.data_page_offset
        )
        end = start + chunk.total_compressed_size
        if start < len(_PARQUET_MAGIC) or chunk.total_compressed_size < 0 or end > footer_start:
            detail = (
                f"row group {group}'s column {column} declares bytes [{start}, {end}), outside"
                f" the data before the footer at {footer_start}"
            )
            return MediaFinding("undecodable", subject, detail)
        total += max(0, chunk.total_uncompressed_size)
    if total > limits.max_decoded_bytes:
        detail = (
            f"row group {group}'s cited columns decode to {total} bytes as the footer states,"
            f" over the {limits.max_decoded_bytes}-byte limit"
        )
        return MediaFinding("unsafe_entry", subject, detail)
    return None


# What one value of a physical type decodes to; a byte array is read as a dictionary, so each
# value is one 4-byte index into it.
_PARQUET_WIDTH: Final = {
    "BOOLEAN": 1,
    "INT32": 4,
    "FLOAT": 4,
    "BYTE_ARRAY": 4,
    "INT64": 8,
    "DOUBLE": 8,
    "INT96": 12,
}


def _check_batch(
    meta: Any, group: int, columns: list[int], rows: int, subject: str, limits: Limits
) -> MediaFinding | None:
    """A dictionary or run-length page states many values in few bytes, so what one batch of
    the cited columns decodes to is bounded as the footer states it, before a page is read: a
    flat leaf holds one value a row, a repeated leaf at most all of its chunk's values."""
    total = 0
    for column in columns:
        leaf = meta.schema.column(column)
        kind = leaf.physical_type
        width = max(0, leaf.length or 0) if kind == "FIXED_LEN_BYTE_ARRAY" else None
        width = _PARQUET_WIDTH.get(kind, 8) if width is None else width
        flat = leaf.max_repetition_level == 0
        values = (
            min(_PARQUET_BATCH_ROWS, rows)
            if flat
            else meta.row_group(group).column(column).num_values
        )
        total += max(0, values) * width
    if total > limits.max_decoded_bytes:
        detail = (
            f"a batch of row group {group}'s cited columns decodes to {total} bytes as the"
            f" footer states, over the {limits.max_decoded_bytes}-byte limit"
        )
        return MediaFinding("unsafe_entry", subject, detail)
    return None


def _parquet_record(
    parquet: Any,
    group: int,
    rows: int,
    index: int,
    columns: list[int],
    leaves: list[str | None],
    subject: str,
    limits: Limits,
) -> list[dict[str, Any]] | MediaFinding:
    """The cited cells of row ``index`` of a row group, decoded a batch at a time (each cited
    leaf's top-level column, whose every leaf ``_check_group`` has bounded)."""
    tops = sorted({str(leaves[c]).split(".")[0] for c in columns})
    seen = 0
    for batch in parquet.iter_batches(
        batch_size=_PARQUET_BATCH_ROWS, row_groups=[group], columns=tops, use_threads=False
    ):
        if batch.nbytes > limits.max_decoded_bytes:
            detail = (
                f"a batch of row group {group} decodes to {batch.nbytes} bytes, over the"
                f" {limits.max_decoded_bytes}-byte limit"
            )
            return MediaFinding("unsafe_entry", subject, detail)
        if seen + batch.num_rows > index:
            row = batch.slice(index - seen, 1)
            return [_parquet_cell(row, c, str(leaves[c])) for c in columns]
        seen += batch.num_rows
        if seen > rows:
            break
    detail = f"row group {group} does not hold the {rows} rows its footer states"
    return MediaFinding("undecodable", subject, detail)


def _parquet_cell(row: Any, column: int, path: str) -> dict[str, Any]:
    """One leaf's value in a one-row batch: a struct path is followed to the leaf; the stored
    integer of a date, time, timestamp or duration (its unit and zone are in ``type``); a
    decimal's exact text. A leaf inside a list or map is not decoded, as in the compiler."""
    import pyarrow as pa
    import pyarrow.types as pt

    parts = path.split(".")
    array = row.column(row.schema.get_field_index(parts[0]))
    for part in parts[1:]:
        if not pt.is_struct(array.type) or array.type.get_field_index(part) < 0:
            return {"column": column, "decoded": False, "name": path, "type": str(array.type)}
        array = array.field(part)
    kind = array.type
    if pt.is_dictionary(kind):
        array = array.dictionary_decode()
        kind = array.type
    if pt.is_timestamp(kind) or pt.is_date(kind) or pt.is_time(kind) or pt.is_duration(kind):
        array = array.view(pa.int64() if kind.bit_width == 64 else pa.int32())
    if pt.is_list(kind) or pt.is_large_list(kind) or pt.is_map(kind):
        return {"column": column, "decoded": False, "name": path, "type": str(kind)}
    value = array.to_pylist()[0]
    cell: dict[str, Any] = {"column": column, "name": path, "type": str(kind)}
    return cell | ({"null": True} if value is None else {"value": _json_value(value)})


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
    if isinstance(value, decimal.Decimal):
        return {"decimal": format(value, "f")}
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


def _pointer(scope: _Scope, step: JsonPointer, subject: str, limits: Limits) -> Decoded:
    """The JSON or YAML node a pointer names, as the text it is written as.

    The document is at most ``max_document_bytes`` (the compiler's own limit), and it is
    walked as a stream of tokens or events: nothing is built from it, so memory is the
    document's text plus its nesting, which is at most ``max_document_depth``. A YAML document
    of more than ``max_yaml_events`` parser events is refused. The node is returned verbatim,
    so no reader's typing of a scalar (YAML 1.1 or 1.2, a float's precision) is assumed. Keys
    are matched by their text, as the compiler cites them; a key held twice on the path is
    ambiguous.
    """
    data = scope.whole(subject, "a JSON or YAML document", limits.max_document_bytes)
    text = _text(data, subject)
    del data
    tokens = tuple(t.replace("~1", "/").replace("~0", "~") for t in step.pointer.split("/")[1:])
    try:
        found, kind = _json_node(text, tokens, step.pointer, subject, limits), "json"
    except _NotJson:
        found, kind = _yaml_node(text, tokens, step.pointer, subject, limits), "yaml"
    if found is None:
        detail = f"{step.pointer} does not resolve in the {kind} document"
        raise DecodeFailure("invalid_request", subject, detail)
    start, end = found
    media = "application/json" if kind == "json" else "application/yaml"
    metadata = {"format": kind, "pointer": step.pointer}
    return Decoded(media, text[start:end].encode("utf-8"), metadata)


def _is_index(token: str) -> bool:
    return token.isascii() and token.isdigit() and (token == "0" or token[0] != "0")


class _NotJson(Exception):
    """The text is not one JSON value (internal: it is then read as YAML)."""


@dataclass(eq=False)
class _Open:
    """One open container of a streamed walk."""

    mapping: bool
    start: int
    matched: bool  # its own path is the pointer's prefix, so a child may lead to the target
    target: bool  # it is the target
    as_key: bool = False  # a YAML collection used as a key: skipped whole
    index: int = 0  # a sequence's next item
    leads: bool = False  # its current child is on the pointer's path
    expect_key: bool = True  # a mapping's next node is a key
    hits: int = 0  # how often its keys matched the next token


class _Tracker:
    """Follows a pointer through a document fed as structural events, building nothing.

    It records the span of the value the pointer names; a container on the path holding the
    next token twice makes the pointer ambiguous (``invalid_request``).
    """

    def __init__(self, tokens: tuple[str, ...], pointer: str, subject: str, limits: Limits) -> None:
        self.tokens, self.pointer, self.subject, self.limits = tokens, pointer, subject, limits
        self.stack: list[_Open] = []
        self.found: tuple[int, int] | None = None
        self.in_key = 0  # open collections used as keys: inside one, nothing is on the path
        self.events = 0

    def tick(self) -> None:
        """Count one YAML parser event against the budget (``max_yaml_events``)."""
        self.events += 1
        if self.events > self.limits.max_yaml_events:
            budget = self.limits.max_yaml_events
            detail = f"the YAML document holds over {budget} events; it is not walked"
            raise DecodeFailure("unsafe_entry", self.subject, detail)

    def _push(self, frame: _Open) -> None:
        if len(self.stack) >= self.limits.max_document_depth:
            depth = self.limits.max_document_depth
            detail = f"the document nests deeper than {depth}; it is not walked"
            raise DecodeFailure("unsafe_entry", self.subject, detail)
        self.stack.append(frame)
        self.in_key += frame.as_key

    def _place(self) -> tuple[bool, bool]:
        """Is the value starting now on the pointer's path, and is it the target?"""
        on = not self.stack or (self.stack[-1].matched and self.stack[-1].leads)
        return on, on and len(self.stack) == len(self.tokens) and self.found is None

    def _child(self, frame: _Open, token: str) -> bool:
        """Does ``frame`` (the innermost container) lead on to the target through ``token``?"""
        depth = len(self.stack) - 1
        return frame.matched and depth < len(self.tokens) and self.tokens[depth] == token

    def scalar(self, start: int, end: int) -> None:
        _, target = self._place()
        if target:
            self.found = (start, end)
        self._done()

    def open(self, mapping: bool, start: int) -> None:
        on, target = self._place()
        matched = on and len(self.stack) < len(self.tokens)
        frame = _Open(mapping, start, matched, target)
        self._push(frame)
        if not mapping:
            frame.leads = self._child(frame, "0")

    def open_key(self, mapping: bool, start: int) -> None:
        self._push(_Open(mapping, start, False, False, as_key=True))

    def key(self, name: str) -> None:
        frame = self.stack[-1]
        frame.leads = self._child(frame, name)
        frame.expect_key = False
        if frame.leads:
            frame.hits += 1
            if frame.hits > 1:
                detail = f"{self.pointer} passes key {name!r}, which its mapping holds twice"
                raise DecodeFailure("invalid_request", self.subject, detail)

    def close(self, end: int) -> None:
        frame = self.stack.pop()
        self.in_key -= frame.as_key
        if frame.as_key:
            parent = self.stack[-1]
            parent.expect_key, parent.leads = False, False
            return
        if frame.target and self.found is None:
            self.found = (frame.start, end)
        self._done()

    def _done(self) -> None:
        if not self.stack:
            return
        frame = self.stack[-1]
        if frame.mapping:
            frame.expect_key, frame.leads = True, False
        else:
            frame.index += 1
            frame.leads = self._child(frame, str(frame.index))


_WS: Final = re.compile(r"[ \t\n\r]*")
_STRING: Final = re.compile(r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"')
_SCALAR: Final = re.compile(
    r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"'
    r"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?(?![0-9.eE+-])"
    r"|(?:true|false|null)(?![A-Za-z0-9_])"
)


def _json_node(
    text: str, tokens: tuple[str, ...], pointer: str, subject: str, limits: Limits
) -> tuple[int, int] | None:
    """The span of the value ``tokens`` names (RFC 8259), walked without building any value;
    ``_NotJson`` when the text is not exactly one JSON value."""
    track = _Tracker(tokens, pointer, subject, limits)

    def ws(at: int) -> int:
        return _WS.match(text, at).end()  # type: ignore[union-attr]

    at, state = ws(0), "value"
    while True:
        char = text[at : at + 1]
        if state == "value":
            if char in ("{", "["):
                track.open(char == "{", at)
                at = ws(at + 1)
                if text[at : at + 1] == ("}" if char == "{" else "]"):
                    at += 1
                    track.close(at)
                    state = "after"
                else:
                    state = "key" if char == "{" else "value"
                continue
            match = _SCALAR.match(text, at)
            if match is None:
                raise _NotJson
            track.scalar(at, match.end())
            at, state = match.end(), "after"
        elif state == "key":
            match = _STRING.match(text, at)
            if match is None:
                raise _NotJson
            track.key(json.loads(match.group()))
            at = ws(match.end())
            if text[at : at + 1] != ":":
                raise _NotJson
            at, state = ws(at + 1), "value"
        else:
            at = ws(at)
            if not track.stack:
                if at != len(text):
                    raise _NotJson
                return track.found
            mapping = track.stack[-1].mapping
            char = text[at : at + 1]
            if char == ",":
                at, state = ws(at + 1), "key" if mapping else "value"
            elif char == ("}" if mapping else "]"):
                at += 1
                track.close(at)
            else:
                raise _NotJson


def _yaml_node(
    text: str, tokens: tuple[str, ...], pointer: str, subject: str, limits: Limits
) -> tuple[int, int] | None:
    """The span of the node ``tokens`` names in a single YAML document, walked as parser events
    (PyYAML's pure-Python parser, as the compiler reads YAML): nothing is composed or
    constructed, and an alias is refused."""
    import yaml

    track = _Tracker(tokens, pointer, subject, limits)

    def walk() -> tuple[int, int] | None:
        documents = 0
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            track.tick()
            if isinstance(event, yaml.DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise yaml.YAMLError("more than one document")
                continue
            if isinstance(event, yaml.AliasEvent):
                raise yaml.YAMLError("aliases are refused")
            start = event.start_mark.index if event.start_mark is not None else 0
            end = event.end_mark.index if event.end_mark is not None else 0
            if isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                # A block collection ends where the next token starts: its text ends before
                # the whitespace between them.
                if track.stack and track.stack[-1].target:
                    first = track.stack[-1].start
                    while end > first and text[end - 1] in " \t\r\n":
                        end -= 1
                track.close(end)
                continue
            if not isinstance(event, (yaml.ScalarEvent, yaml.CollectionStartEvent)):
                continue
            parent = track.stack[-1] if track.stack else None
            mapping = isinstance(event, yaml.MappingStartEvent)
            if parent is not None and parent.mapping and parent.expect_key:
                if isinstance(event, yaml.ScalarEvent):
                    if track.in_key:
                        parent.expect_key = False
                    else:
                        track.key(event.value)
                else:
                    track.open_key(mapping, start)
                continue
            if isinstance(event, yaml.ScalarEvent):
                track.scalar(start, end)
            else:
                track.open(mapping, start)
        return track.found

    return guarded(subject, "neither JSON nor single-document YAML", walk)  # type: ignore[no-any-return]


def _span(scope: _Scope, step: Span, subject: str) -> Decoded:
    text = _text(scope.whole(subject, "a text"), subject)
    if step.end > len(text):
        detail = f"span [{step.start}, {step.end}) is past the text's {len(text)} code points"
        raise DecodeFailure("invalid_request", subject, detail)
    made = text[step.start : step.end].encode("utf-8")
    metadata = {"end": step.end, "format": "text", "start": step.start}
    return Decoded("text/plain; charset=utf-8", made, metadata)

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

import csv
import hashlib
import io
import json
import math
import threading
import zlib
from collections.abc import Callable, Mapping
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
from neptune_ledger.lake.evidence import MediaFinding, MediaFindingCode, SourceReader

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
    "frame": ("mcap", "mcap-ros2-support", "pillow"),
    "image_region": ("mcap", "mcap-ros2-support", "pillow"),
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
            return self._reader.file()
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
                while block := f.read(1 << 20):
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
        with self.file() as f:
            f.seek(step.offset)
            data = f.read(step.length)
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

    Raises ``DecodeFailure`` with the finding, or the reader's ``SourceChanged``.
    """
    scope = _Scope(reader, None, limits)
    image: tuple[Any, dict[str, Any]] | None = None
    for at, step in enumerate(steps[:-1]):
        if isinstance(step, ByteRange):
            scope = scope.decoded(subject).slice(step, subject)
        elif isinstance(step, RecordRange) and at == len(steps) - 2:
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
        return _row(scope.decoded(subject), last, subject)
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
    from neptune_ledger.lake.evidence import SourceChanged

    try:
        return call()
    except (DecodeFailure, SourceChanged):
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
    from mcap.reader import make_reader
    from mcap_ros2.decoder import DecoderFactory

    def messages() -> list[Any]:
        with scope.file() as f:
            reader = make_reader(f, decoder_factories=[DecoderFactory()])  # type: ignore[arg-type]
            found = []
            for item in reader.iter_messages(
                topics=[step.channel],
                start_time=step.start.ticks,
                end_time=step.end.ticks,
                log_time_order=True,
            ):
                found.append(item)
                if len(found) > 1:
                    break
            return found

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


def _row(scope: _Scope, step: Row | RowCell, subject: str) -> Decoded:
    if scope.size >= 12 and scope.head(4) == b"PAR1" and scope.tail(4) == b"PAR1":
        made = guarded(
            subject, "not a readable Parquet file", lambda: _parquet_row(scope, step, subject)
        )
    else:
        made = _csv_row(scope.whole(subject, "a table"), step, subject)
    if isinstance(made, MediaFinding):
        raise DecodeFailure(made.code, made.subject, made.detail)
    return Decoded("application/json", canonical_json.dumps(made), {"format": made["format"]})


def _cell_column(
    step: Row | RowCell, cells: int, names: list[str], subject: str
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


def _csv_row(data: bytes, step: Row | RowCell, subject: str) -> dict[str, Any] | MediaFinding:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return MediaFinding("undecodable", subject, f"a CSV table must be UTF-8: {exc}")
    header: list[str] = []
    try:
        for index, cells in enumerate(csv.reader(io.StringIO(text, newline=""), strict=True)):
            if index == 0:
                header = cells
            if index == step.row:
                problem = _cell_column(step, len(cells), header, subject)
                if problem is not None:
                    return problem
                out: dict[str, Any] = {"format": "csv", "row": step.row}
                if isinstance(step, RowCell):
                    out |= {"cell": cells[step.column], "column": step.column}
                else:
                    out["cells"] = cells
                return out
    except csv.Error as exc:
        return MediaFinding("undecodable", subject, f"not a readable CSV table: {exc}")
    return MediaFinding("invalid_request", subject, f"the table has no row {step.row}")


def _parquet_row(scope: _Scope, step: Row | RowCell, subject: str) -> dict[str, Any] | MediaFinding:
    import pyarrow.parquet as pq

    with scope.file() as f:
        parquet = pq.ParquetFile(f)
        meta = parquet.metadata
        if step.row >= meta.num_rows:
            return MediaFinding("invalid_request", subject, f"the table has no row {step.row}")
        names = list(parquet.schema_arrow.names)
        problem = _cell_column(step, len(names), names, subject)
        if problem is not None:
            return problem
        first = 0
        for group in range(meta.num_row_groups):
            rows = meta.row_group(group).num_rows
            if step.row < first + rows:
                table = parquet.read_row_group(group)
                break
            first += rows
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


def _pointer(scope: _Scope, step: JsonPointer, subject: str) -> Decoded:
    data = scope.whole(subject, "a document")
    try:
        document, kind = json.loads(data.decode("utf-8")), "json"
    except (UnicodeDecodeError, ValueError):
        document, kind = _yaml(data, subject), "yaml"
    value = document
    for token in step.pointer.split("/")[1:]:
        name = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and name in value:
            value = value[name]
        elif isinstance(value, list) and name.isdigit() and (name == "0" or name[0] != "0"):
            if int(name) >= len(value):
                raise DecodeFailure("invalid_request", subject, f"{step.pointer} is past the array")
            value = value[int(name)]
        else:
            detail = f"{step.pointer} does not resolve in the {kind} document"
            raise DecodeFailure("invalid_request", subject, detail)
    made = canonical_json.dumps(_json_value(value))
    return Decoded("application/json", made, {"format": kind, "pointer": step.pointer})


def _yaml(data: bytes, subject: str) -> Any:
    import yaml

    class _NoAliases(yaml.SafeLoader):
        """A safe loader that refuses aliases, so a document cannot expand without bound."""

        def compose_node(self, parent: Any, index: Any) -> Any:
            if self.check_event(yaml.AliasEvent):
                raise yaml.YAMLError("aliases are refused")
            return super().compose_node(parent, index)

    def load() -> Any:
        return yaml.load(data, Loader=_NoAliases)

    return guarded(subject, "neither JSON nor single-document YAML", load)


def _span(scope: _Scope, step: Span, subject: str) -> Decoded:
    try:
        text = scope.whole(subject, "a text").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DecodeFailure("undecodable", subject, f"a span needs UTF-8 text: {exc}") from exc
    if step.end > len(text):
        detail = f"span [{step.start}, {step.end}) is past the text's {len(text)} code points"
        raise DecodeFailure("invalid_request", subject, detail)
    made = text[step.start : step.end].encode("utf-8")
    metadata = {"end": step.end, "format": "text", "start": step.start}
    return Decoded("text/plain; charset=utf-8", made, metadata)

"""Media around an event, asked of a package without decoding a run (ADR 0056).

::

    from neptune.sdk import read_package
    from neptune.sdk.media import FileSource, Hydrator, media_window

    window = media_window(read_package(path), "log_time", t, t + 10 * 10**9)
    with FileSource(source_path) as source:
        frames = Hydrator(source)
        for frame in window.frames:
            print(frame.stream, frame.seq, frame.times, len(frames.payload(frame)))

``media_window`` reads the package's ``media_stream`` lines and, for each media stream with the
named clock, the rows of its series whose ticks on that clock fall in ``[start, end]``. It reads
only the series row groups whose statistics overlap the window, and no source byte. Each
``Frame`` keeps every clock the stream declares, as ticks on that clock (never converted), and
its handle: the row's ``EvidenceRef``, where the frame's bytes are.

``Hydrator`` turns a handle into the frame's payload, reading only the record (or the one chunk)
that holds it. ``header_stamp``, ``image_thumbnail`` and ``point_cloud_ref`` read a hydrated
payload's declared header, a raw image's pixels and a point cloud's declared fields; none decodes
points, and nothing needs a codec the standard library lacks.
"""

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Final, Self

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from neptune.adapters.contract import SourceReader
from neptune.adapters.mcap.records import MESSAGE_FIELDS, RECORD_HEADER, Opcode, record_header
from neptune.adapters.mcap.scan import ChunkProblem, TopRecord, open_chunk
from neptune.derived.media import (
    MEDIA_ID,
    MEDIA_VERSION,
    DerivativeState,
    Media,
    MediaState,
    MediaStream,
)
from neptune.derived.sessions import read_derived
from neptune.identity.provenance import transform_record
from neptune.model.ids import ContentId, RecordId
from neptune.model.knowledge import KnowledgeState
from neptune.model.provenance import ByteRange, EvidenceRef, TransformRecord
from neptune.model.reference import TimestampDomain
from neptune.model.run import Stream
from neptune.model.series import cell_state, state_column, ticks_of, time_column
from neptune.sdk.errors import InvalidRequestError, PackageInvalidError, UnsupportedError
from neptune.store.package import IngestPackage

MAX_FRAMES: Final = 100_000  # frames one window returns across its streams
MAX_FRAME_BYTES: Final = 256 << 20  # a frame's record or chunk the hydrator holds


@dataclass(frozen=True)
class FrameTime:
    """A frame's ticks on one clock the stream declares, exactly as the series holds them."""

    clock: RecordId  # the TimestampDomain
    field: str  # the clock's declared name (``log_time``, ``publish_time``)
    ticks: int | None  # ``None`` exactly when ``state`` is not ``known``
    state: KnowledgeState


@dataclass(frozen=True)
class Frame:
    """One media message: its stream, position, times on every declared clock, and handle."""

    stream: RecordId
    media: Media
    seq: int
    times: tuple[FrameTime, ...]
    handle: EvidenceRef
    hydrator: str | None
    message_encoding: str | None

    def ticks(self, clock: str) -> int | None:
        """The frame's ticks on ``clock`` (a field name or a domain id), if known."""
        for time in self.times:
            if clock in (time.field, time.clock):
                return time.ticks
        return None

    @property
    def length(self) -> int:
        """The bytes of the record the handle names, innermost step."""
        step = self.handle.locator[-1]
        return step.length if isinstance(step, ByteRange) else 0


@dataclass(frozen=True)
class StreamWindow:
    """One media stream's part of a window.

    ``state`` is ``known`` (``frames`` are every row in the window), ``not_covered`` (the stream
    was past the package's frame budget, or the window past ``max_frames``: ``matched`` says how
    many rows fell in it, and no frame is listed), or ``not_applicable`` (the stream has no clock
    of that name).
    """

    media: MediaStream
    stream: Stream
    clock: TimestampDomain | None
    state: KnowledgeState
    matched: int
    frames: tuple[Frame, ...]
    reason: str | None = None


@dataclass(frozen=True)
class MediaWindow:
    clock: str
    start: int
    end: int
    streams: tuple[StreamWindow, ...]

    @property
    def frames(self) -> tuple[Frame, ...]:
        """Every listed frame, by its ticks on the window's clock, then stream, then seq."""
        found = [frame for window in self.streams for frame in window.frames]
        return tuple(sorted(found, key=lambda f: (f.ticks(self.clock) or 0, f.stream, f.seq)))


def media_streams(package: IngestPackage) -> tuple[MediaStream, ...]:
    """The package's ``media_stream`` lines, by id; empty without media (or before ADR 0056)."""
    try:
        lines = read_derived(package.derived)
    except (ValueError, TypeError, KeyError) as exc:
        raise PackageInvalidError(f"the package's derived tables cannot be read: {exc}") from exc
    return tuple(sorted((r for r in lines if isinstance(r, MediaStream)), key=lambda r: r.id))


def _columns(file: Any) -> dict[str, int]:
    meta = file.metadata
    if meta.num_row_groups == 0:
        return {}
    group = meta.row_group(0)
    return {group.column(i).path_in_schema: i for i in range(group.num_columns)}


def _groups(file: Any, column: str, start: int, end: int) -> list[int]:
    """Row groups whose statistics for ``column`` may overlap ``[start, end]``."""
    index = _columns(file).get(column)
    chosen = []
    for group in range(file.metadata.num_row_groups):
        stats = None if index is None else file.metadata.row_group(group).column(index).statistics
        if stats is not None and stats.has_min_max and (stats.max < start or stats.min > end):
            continue
        chosen.append(group)
    return chosen


def _mask(table: Any, column: str, start: int, end: int) -> Any:
    values = table.column(column)
    return pc.fill_null(pc.and_(pc.greater_equal(values, start), pc.less_equal(values, end)), False)


def _window(
    stream: Stream, line: MediaStream, source: Any, index: int, start: int, end: int, budget: int
) -> tuple[int, tuple[Frame, ...] | None]:
    """The rows in the window: their count, and the frames when at most ``budget``."""
    file = pq.ParquetFile(pa.BufferReader(source) if isinstance(source, bytes) else source)
    column = time_column(index)
    groups = _groups(file, column, start, end)
    matched = 0
    hits: list[int] = []
    for group in groups:  # count first, reading the clock column only
        found = pc.sum(_mask(file.read_row_group(group, columns=[column]), column, start, end))
        count = int(found.as_py() or 0)
        matched += count
        if count:
            hits.append(group)
    if matched > budget:
        return matched, None
    frames: list[Frame] = []
    names = [time_column(i) for i in range(len(stream.clocks))]
    for group in hits:
        table = file.read_row_group(group)
        for row in table.filter(_mask(table, column, start, end)).to_pylist():
            times = []
            for clock, name in zip(stream.clocks, names, strict=True):
                state = cell_state(row, name) if name in row else KnowledgeState.NOT_COVERED
                ticks = ticks_of(row, name) if state is KnowledgeState.KNOWN else None
                times.append(FrameTime(clock, "", ticks, state))
            frames.append(
                Frame(
                    stream=stream.id,
                    media=line.media,
                    seq=int(row["seq"]),
                    times=tuple(times),
                    handle=stream.series.evidence(row),
                    hydrator=line.hydrator,
                    message_encoding=line.message_encoding,
                )
            )
    return matched, tuple(frames)


def media_window(
    package: IngestPackage,
    clock: str,
    start: int,
    end: int,
    *,
    media: Sequence[Media | str] | None = None,
    streams: Sequence[RecordId] | None = None,
    max_frames: int = MAX_FRAMES,
) -> MediaWindow:
    """Every media frame whose ticks on ``clock`` lie in ``[start, end]`` (module docstring).

    ``clock`` is a clock's declared name (``log_time``) or a ``TimestampDomain`` id; ticks are on
    that clock, as its domain declares them. ``media`` and ``streams`` narrow the streams asked.
    Past ``max_frames`` (all streams together, in stream id order), a stream is ``not_covered``
    with its count, and no frame of it is listed.
    """
    for name, value in (("start", start), ("end", end), ("max_frames", max_frames)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidRequestError(f"{name} must be an integer, got {value!r}")
    if start > end:
        raise InvalidRequestError(f"the window starts after it ends: {start} > {end}")
    if max_frames < 0:
        raise InvalidRequestError(f"max_frames must not be negative, got {max_frames}")
    try:
        wanted = None if media is None else {Media(m) for m in media}
    except ValueError:
        known = ", ".join(str(m) for m in Media)
        raise InvalidRequestError(f"{media!r} names a media that is not one of: {known}") from None
    records = {r.id: r for r in package.records if isinstance(r, Stream | TimestampDomain)}
    found: list[StreamWindow] = []
    budget = max_frames
    for line in media_streams(package):
        if (wanted is not None and line.media not in wanted) or (
            streams is not None and line.stream not in streams
        ):
            continue
        stream = records.get(line.stream)
        if not isinstance(stream, Stream):
            raise PackageInvalidError(f"media line {line.id} names a stream the package lacks")
        domains = [records.get(c) for c in stream.clocks]
        index = next(
            (
                i
                for i, d in enumerate(domains)
                if isinstance(d, TimestampDomain) and clock in (d.field, d.id)
            ),
            None,
        )
        if index is None:
            found.append(
                StreamWindow(line, stream, None, KnowledgeState.NOT_APPLICABLE, 0, (), "no_clock")
            )
            continue
        domain = domains[index]
        assert isinstance(domain, TimestampDomain)
        if line.state is MediaState.NOT_COVERED:
            found.append(
                StreamWindow(
                    line, stream, domain, KnowledgeState.NOT_COVERED, 0, (), "frame_budget"
                )
            )
            continue
        source = package.series.get(stream.id)
        if source is None:
            raise PackageInvalidError(f"stream {stream.id} has no series in the package")
        matched, frames = _window(stream, line, source, index, start, end, budget)
        if frames is None:
            found.append(
                StreamWindow(
                    line, stream, domain, KnowledgeState.NOT_COVERED, matched, (), "max_frames"
                )
            )
            continue
        budget -= len(frames)
        named = {
            d.id: d.field for d in domains if isinstance(d, TimestampDomain)
        }  # name each clock as its domain declares it
        frames = tuple(
            Frame(
                f.stream,
                f.media,
                f.seq,
                tuple(
                    FrameTime(t.clock, named.get(t.clock, ""), t.ticks, t.state) for t in f.times
                ),
                f.handle,
                f.hydrator,
                f.message_encoding,
            )
            for f in frames
        )
        found.append(StreamWindow(line, stream, domain, KnowledgeState.KNOWN, matched, frames))
    return MediaWindow(clock, start, end, tuple(found))


# --- Hydration ---------------------------------------------------------------------------------


class FileSource:
    """A source file read in place, counting the bytes it serves (``bytes_read``)."""

    def __init__(self, path: Path | str, content: ContentId) -> None:
        self._file: BinaryIO = open(path, "rb")  # noqa: SIM115 - closed by ``close``
        self._content = content
        self._size = self._file.seek(0, 2)
        self.bytes_read = 0

    @property
    def content_id(self) -> ContentId:
        return self._content

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0:
            raise ValueError(f"a read is a non-negative offset and length: {offset}, {length}")
        self._file.seek(offset)
        data = self._file.read(length)
        self.bytes_read += len(data)
        return data

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()


class HydrationError(UnsupportedError):
    """A frame's bytes cannot be read: no hydrator, another source, or a malformed record."""


@dataclass
class Hydrator:
    """Reads frames' payloads from one source, holding the last chunk it opened, so frames of one
    chunk cost one read and one decompression."""

    source: SourceReader
    max_bytes: int = MAX_FRAME_BYTES
    _chunk: tuple[tuple[int, int], bytes] | None = field(default=None, repr=False)

    def payload(self, frame: Frame) -> bytes:
        """The message's payload: its bytes as the source encodes them, after the record's
        fields. Raises ``HydrationError`` when the frame cannot be read here."""
        if frame.hydrator != "mcap_message":
            raise HydrationError(f"no hydrator reads frames of {frame.stream}")
        if frame.handle.source != self.source.content_id:
            raise HydrationError(f"the frame is in {frame.handle.source}, not this source")
        steps = frame.handle.locator
        if not 1 <= len(steps) <= 2 or not all(isinstance(s, ByteRange) for s in steps):
            raise HydrationError("an MCAP frame's handle is one or two byte ranges")
        ranges = [(s.offset, s.length) for s in steps if isinstance(s, ByteRange)]
        if any(length > self.max_bytes for _, length in ranges):
            raise HydrationError(f"the frame's record passes {self.max_bytes} bytes")
        (offset, length), *inner = ranges
        if not inner:
            record = self.source.read(offset, length)
        else:
            data = self._open(offset, length)
            start, size = inner[0]
            record = data[start : start + size]
        if len(record) < RECORD_HEADER + MESSAGE_FIELDS or record[0] != Opcode.MESSAGE:
            raise HydrationError("the frame's handle does not name an MCAP Message record")
        return record[RECORD_HEADER + MESSAGE_FIELDS :]

    def _open(self, offset: int, length: int) -> bytes:
        if self._chunk is not None and self._chunk[0] == (offset, length):
            return self._chunk[1]
        _, declared = record_header(self.source.read(offset, RECORD_HEADER))
        cut = RECORD_HEADER + declared > length
        record = TopRecord(
            offset, Opcode.CHUNK, declared, None, cut=cut, present=length - RECORD_HEADER
        )
        try:
            opened = open_chunk(self.source, record, self.max_bytes)
        except ChunkProblem as exc:
            raise HydrationError(f"the frame's chunk cannot be read: {exc.reason}") from None
        self._chunk = ((offset, length), opened.data)
        return opened.data


# --- Reading a hydrated payload's declared fields ---------------------------------------------


class _Cursor:
    """Reads ROS 1 serialisation (little-endian, packed) or CDR (aligned, either endianness)."""

    def __init__(self, data: bytes, encoding: str | None) -> None:
        if encoding == "cdr":
            if len(data) < 4 or data[0] != 0 or data[1] not in (0, 1):
                raise ValueError("not a plain CDR encapsulation")
            self.order, self.aligned, self.base, self.at = ("<" if data[1] else ">"), True, 4, 4
        elif encoding == "ros1":
            self.order, self.aligned, self.base, self.at = "<", False, 0, 0
        else:
            raise ValueError(f"message encoding {encoding!r} is not read")
        self.data = data

    def take(self, code: str) -> Any:
        size = struct.calcsize(code)
        if self.aligned:
            self.at += (-(self.at - self.base)) % size
        if self.at + size > len(self.data):
            raise ValueError("the payload ends early")
        (value,) = struct.unpack_from(self.order + code, self.data, self.at)
        self.at += size
        return value

    def blob(self) -> tuple[int, int]:
        """A ``uint8[]`` or string's (offset, length) in the payload, without copying it."""
        length = self.take("I")
        if self.at + length > len(self.data):
            raise ValueError("the payload ends early")
        start, self.at = self.at, self.at + length
        return start, length

    def text(self) -> str:
        start, length = self.blob()
        raw = self.data[start : start + length]
        return (
            raw.removesuffix(b"\0").decode("utf-8", "replace")
            if self.aligned
            else raw.decode("utf-8", "replace")
        )

    def header(self) -> tuple[int, int, str]:
        if not self.aligned:
            self.take("I")  # ROS 1 Header.seq
        sec = self.take("i" if self.aligned else "I")
        nanosec = self.take("I")
        return sec, nanosec, self.text()


@dataclass(frozen=True)
class HeaderStamp:
    """A message's ``header.stamp`` and ``frame_id``, as declared: never converted."""

    sec: int
    nanosec: int
    frame_id: str


def header_stamp(payload: bytes, message_encoding: str | None) -> HeaderStamp:
    """The header of a ROS ``Image``, ``CompressedImage`` or ``PointCloud2`` payload (each starts
    with ``std_msgs/Header``). Raises ``ValueError`` for another encoding or a short payload."""
    return HeaderStamp(*_Cursor(payload, message_encoding).header())


@dataclass(frozen=True)
class PointField:
    name: str
    offset: int
    datatype: int
    count: int


@dataclass(frozen=True)
class PointCloudRef:
    """A ``PointCloud2`` as declared: its shape, its fields and where its point bytes are in the
    payload (``data_offset``, ``data_length``). No point is decoded."""

    header: HeaderStamp
    height: int
    width: int
    fields: tuple[PointField, ...]
    is_bigendian: bool
    point_step: int
    row_step: int
    data_offset: int
    data_length: int


MAX_POINT_FIELDS: Final = 1024


def point_cloud_ref(payload: bytes, message_encoding: str | None) -> PointCloudRef:
    """Read a ``sensor_msgs/PointCloud2`` payload's declared fields (``ValueError`` if it cannot)."""
    cursor = _Cursor(payload, message_encoding)
    header = HeaderStamp(*cursor.header())
    height, width = cursor.take("I"), cursor.take("I")
    count = cursor.take("I")
    if count > MAX_POINT_FIELDS:
        raise ValueError(f"{count} point fields, more than {MAX_POINT_FIELDS}")
    fields = []
    for _ in range(count):
        name = cursor.text()
        fields.append(PointField(name, cursor.take("I"), cursor.take("B"), cursor.take("I")))
    big = bool(cursor.take("B"))
    point_step, row_step = cursor.take("I"), cursor.take("I")
    start, length = cursor.blob()
    return PointCloudRef(
        header, height, width, tuple(fields), big, point_step, row_step, start, length
    )


# Raw pixel encodings a thumbnail is made from: bytes per pixel, channels, and their order.
_PIXELS: Final[Mapping[str, tuple[int, str]]] = {
    "mono8": (1, "L"),
    "8UC1": (1, "L"),
    "mono16": (2, "L16"),
    "16UC1": (2, "L16"),
    "rgb8": (3, "RGB"),
    "bgr8": (3, "BGR"),
    "rgba8": (4, "RGBA"),
    "bgra8": (4, "BGRA"),
}
THUMBNAIL_SIDE: Final = 64


@dataclass(frozen=True)
class Thumbnail:
    """A thumbnail derivative: a PGM or PPM image, and how it was made.

    ``transform`` names the procedure and its config; ``evidence`` is the frame it was made from.
    Made on request, never stored in the package; the same frame always gives the same bytes.
    """

    state: KnowledgeState  # ``known`` (made) or ``not_covered`` (``reason`` says why)
    data: bytes | None
    media_type: str | None
    width: int
    height: int
    evidence: EvidenceRef
    transform: TransformRecord
    reason: str | None = None


def thumbnail_transform(media: MediaStream, side: int = THUMBNAIL_SIDE) -> TransformRecord:
    return transform_record(
        adapter_id=f"{MEDIA_ID}.thumbnail",
        adapter_version=MEDIA_VERSION,
        config={"max_side": side, "sampling": "nearest", "formats": ["pgm", "ppm"]},
        upstream=[media.transform],
    )


def image_thumbnail(
    frame: Frame, payload: bytes, media: MediaStream, side: int = THUMBNAIL_SIDE
) -> Thumbnail:
    """A nearest-neighbour thumbnail of a raw ``sensor_msgs/Image`` payload, at most ``side``
    pixels on its long side. ``not_covered`` (with a reason) for compressed or video frames, an
    encoding not in the table, or a payload that does not hold its declared pixels."""
    transform = thumbnail_transform(media, side)

    def refused(reason: str) -> Thumbnail:
        return Thumbnail(
            KnowledgeState.NOT_COVERED, None, None, 0, 0, frame.handle, transform, reason
        )

    if media.thumbnail.state is not DerivativeState.ON_REQUEST:
        return refused(media.thumbnail.reason or "not_covered")
    try:
        cursor = _Cursor(payload, frame.message_encoding)
        cursor.header()
        height, width = cursor.take("I"), cursor.take("I")
        encoding = cursor.text()
        big = bool(cursor.take("B"))
        step = cursor.take("I")
        start, length = cursor.blob()
    except ValueError:
        return refused("payload_malformed")
    if encoding not in _PIXELS:
        return refused("pixel_encoding_not_covered")
    size, layout = _PIXELS[encoding]
    if not height or not width or step < width * size or length < step * height:
        return refused("payload_malformed")
    scale = max(1, -(-max(width, height) // side))
    out_w, out_h = -(-width // scale), -(-height // scale)
    out = bytearray()
    for y in range(0, height, scale):
        row = start + y * step
        for x in range(0, width, scale):
            at = row + x * size
            if layout == "L":
                out.append(payload[at])
            elif layout == "L16":
                value = int.from_bytes(payload[at : at + 2], "big" if big else "little")
                out += value.to_bytes(2, "big")
            elif layout.startswith("RGB"):
                out += payload[at : at + 3]
            else:  # BGR, BGRA
                out += bytes((payload[at + 2], payload[at + 1], payload[at]))
    if layout in ("L", "L16"):
        magic, top, kind = b"P5", (255 if layout == "L" else 65535), "image/x-portable-graymap"
    else:
        magic, top, kind = b"P6", 255, "image/x-portable-pixmap"
    data = magic + f"\n{out_w} {out_h}\n{top}\n".encode() + bytes(out)
    return Thumbnail(DerivativeState.ON_REQUEST, data, kind, out_w, out_h, frame.handle, transform)

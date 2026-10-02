"""Tiny writers for the worked examples' binary sources: MCAP, CDR, ULog, ROS 1 bags and PNG.

Each writer returns the file's bytes and a layout: the byte range ``(offset, length)`` of every
record it wrote, by name, so an example can cite exact bytes and a test can check them. The
output is deterministic. Every format was checked once against its official reader (``mcap``,
``rosbags``, ``pyulog``, ``Pillow``); see README.md.
"""

import hashlib
import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

Layout = dict[str, tuple[int, int]]


@dataclass
class _Writer:
    """Bytes plus the byte range of every named piece appended."""

    data: bytearray = field(default_factory=bytearray)
    layout: Layout = field(default_factory=dict)

    def add(self, name: str | None, chunk: bytes) -> tuple[int, int]:
        span = (len(self.data), len(chunk))
        if name is not None:
            if name in self.layout:
                raise ValueError(f"layout name repeats: {name}")
            self.layout[name] = span
        self.data += chunk
        return span


# --- MCAP (https://mcap.dev/spec) --------------------------------------------------------------

MCAP_MAGIC: Final = b"\x89MCAP0\r\n"


def _mcap_string(text: str) -> bytes:
    data = text.encode()
    return struct.pack("<I", len(data)) + data


def _mcap_bytes(data: bytes) -> bytes:
    return struct.pack("<I", len(data)) + data


def _mcap_record(opcode: int, content: bytes) -> bytes:
    return struct.pack("<BQ", opcode, len(content)) + content


@dataclass(frozen=True)
class McapSchema:
    id: int
    name: str
    encoding: str
    data: bytes


@dataclass(frozen=True)
class McapChannel:
    id: int
    schema_id: int
    topic: str
    message_encoding: str
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class McapMessage:
    channel_id: int
    sequence: int
    log_time: int
    publish_time: int
    data: bytes


def mcap(
    profile: str,
    schemas: Sequence[McapSchema],
    channels: Sequence[McapChannel],
    messages: Sequence[McapMessage],
    metadata: Sequence[tuple[str, Sequence[tuple[str, str]]]] = (),
) -> tuple[bytes, Layout]:
    """An unchunked MCAP file with a summary section (Schemas, Channels, Statistics), and a
    Metadata record per ``(name, entries)`` of ``metadata`` after the messages."""

    def schema(s: McapSchema) -> bytes:
        body = _mcap_string(s.name) + _mcap_string(s.encoding) + _mcap_bytes(s.data)
        return _mcap_record(0x03, struct.pack("<H", s.id) + body)

    def channel(c: McapChannel) -> bytes:
        entries = b"".join(_mcap_string(k) + _mcap_string(v) for k, v in sorted(c.metadata))
        body = _mcap_string(c.topic) + _mcap_string(c.message_encoding) + _mcap_bytes(entries)
        return _mcap_record(0x04, struct.pack("<HH", c.id, c.schema_id) + body)

    out = _Writer()
    out.add("magic", MCAP_MAGIC)
    out.add("header", _mcap_record(0x01, _mcap_string(profile) + _mcap_string("neptune-example/1")))
    for s in schemas:
        out.add(f"schema:{s.id}", schema(s))
    for c in channels:
        out.add(f"channel:{c.id}", channel(c))
    for i, m in enumerate(messages):
        fields = struct.pack("<HIQQ", m.channel_id, m.sequence, m.log_time, m.publish_time)
        out.add(f"message:{i}", _mcap_record(0x05, fields + m.data))
    for i, (name, entries) in enumerate(metadata):
        pairs = b"".join(_mcap_string(k) + _mcap_string(v) for k, v in entries)
        out.add(f"metadata:{i}", _mcap_record(0x0C, _mcap_string(name) + _mcap_bytes(pairs)))
    out.add("data_end", _mcap_record(0x0F, struct.pack("<I", 0)))
    summary_start = len(out.data)
    for s in schemas:
        out.add(f"summary_schema:{s.id}", schema(s))
    for c in channels:
        out.add(f"summary_channel:{c.id}", channel(c))
    log_times = [m.log_time for m in messages]
    counts = {c.id: sum(1 for m in messages if m.channel_id == c.id) for c in channels}
    fields = struct.pack(
        "<QHIIIIQQ",
        len(messages),
        len(schemas),
        len(channels),
        0,
        0,
        0,
        min(log_times),
        max(log_times),
    )
    entries = b"".join(struct.pack("<HQ", channel_id, n) for channel_id, n in counts.items())
    out.add("statistics", _mcap_record(0x0B, fields + _mcap_bytes(entries)))
    footer = struct.pack("<BQQQ", 0x02, 20, summary_start, 0)
    crc = zlib.crc32(bytes(out.data[summary_start:]) + footer)
    out.add("footer", footer + struct.pack("<I", crc))
    out.add("magic_end", MCAP_MAGIC)
    return bytes(out.data), out.layout


# --- CDR, as ROS 2 serialises messages ---------------------------------------------------------


class Cdr:
    """Little-endian CDR with its encapsulation header; alignment counts from after the header."""

    def __init__(self) -> None:
        self.body = bytearray()

    def _align(self, size: int) -> None:
        self.body += bytes(-len(self.body) % size)

    def int32(self, value: int) -> "Cdr":
        self._align(4)
        self.body += struct.pack("<i", value)
        return self

    def uint32(self, value: int) -> "Cdr":
        self._align(4)
        self.body += struct.pack("<I", value)
        return self

    def float64(self, value: float) -> "Cdr":
        self._align(8)
        self.body += struct.pack("<d", value)
        return self

    def string(self, text: str) -> "Cdr":
        data = text.encode() + b"\x00"
        self.uint32(len(data))
        self.body += data
        return self

    def strings(self, texts: Sequence[str]) -> "Cdr":
        self.uint32(len(texts))
        for text in texts:
            self.string(text)
        return self

    def float64s(self, values: Sequence[float]) -> "Cdr":
        self.uint32(len(values))
        for value in values:
            self.float64(value)
        return self

    def octets(self, data: bytes) -> "Cdr":
        self.uint32(len(data))
        self.body += data
        return self

    def header(self, stamp_ns: int, frame_id: str) -> "Cdr":
        sec, nanosec = divmod(stamp_ns, 10**9)
        return self.int32(sec).uint32(nanosec).string(frame_id)

    def bytes(self) -> bytes:
        return b"\x00\x01\x00\x00" + bytes(self.body)


# --- ULog (https://docs.px4.io/main/en/dev_log/ulog_file_format.html) --------------------------

ULOG_MAGIC: Final = b"ULog\x01\x12\x35"


def _ulog_message(kind: bytes, payload: bytes) -> bytes:
    return struct.pack("<HB", len(payload), kind[0]) + payload


def _ulog_key_value(key: str, value: bytes) -> bytes:
    encoded = key.encode()
    return struct.pack("<B", len(encoded)) + encoded + value


@dataclass(frozen=True)
class UlogTopic:
    name: str
    fields: tuple[tuple[str, str], ...]  # (ULog type, field name), in order
    multi_id: int
    msg_id: int


_ULOG_PACK: Final = {"uint64_t": "Q", "int32_t": "i", "uint32_t": "I", "float": "f"}


def ulog(
    start_us: int,
    infos: Sequence[tuple[str, bytes]],
    parameters: Sequence[tuple[str, bytes]],
    topics: Sequence[UlogTopic],
    data: Sequence[tuple[int, tuple[float | int, ...]]],
    dropouts: Sequence[tuple[int, int]] = (),
) -> tuple[bytes, Layout]:
    """A ULog file: header, flag bits, formats, infos, parameters, subscriptions, then data.

    ``infos`` and ``parameters`` are ``(key with type, value bytes)``. ``data`` is
    ``(msg_id, values)``. ``dropouts`` are ``(position in data, duration ms)``.
    """
    out = _Writer()
    out.add("header", ULOG_MAGIC + struct.pack("<BQ", 1, start_us))
    out.add("flag_bits", _ulog_message(b"B", bytes(16) + struct.pack("<QQQ", 0, 0, 0)))
    for topic in topics:
        body = ";".join(f"{kind} {name}" for kind, name in topic.fields) + ";"
        out.add(f"format:{topic.name}", _ulog_message(b"F", f"{topic.name}:{body}".encode()))
    for key, value in infos:
        out.add(f"info:{key.split()[-1]}", _ulog_message(b"I", _ulog_key_value(key, value)))
    for key, value in parameters:
        out.add(f"parameter:{key.split()[-1]}", _ulog_message(b"P", _ulog_key_value(key, value)))
    for topic in topics:
        payload = struct.pack("<BH", topic.multi_id, topic.msg_id) + topic.name.encode()
        out.add(f"subscription:{topic.msg_id}", _ulog_message(b"A", payload))
    by_id = {topic.msg_id: topic for topic in topics}
    dropped = dict(dropouts)
    for i, (msg_id, values) in enumerate(data):
        if i in dropped:
            out.add(f"dropout:{i}", _ulog_message(b"O", struct.pack("<H", dropped[i])))
        pack = "<" + "".join(_ULOG_PACK[kind] for kind, _ in by_id[msg_id].fields)
        out.add(
            f"data:{i}", _ulog_message(b"D", struct.pack("<H", msg_id) + struct.pack(pack, *values))
        )
    return bytes(out.data), out.layout


# --- ROS 1 bag, format 2.0 (http://wiki.ros.org/Bags/Format/2.0) -------------------------------

BAG_MAGIC: Final = b"#ROSBAG V2.0\n"


def _bag_header(fields: Sequence[tuple[str, bytes]]) -> bytes:
    body = b"".join(
        struct.pack("<I", len(name) + 1 + len(value)) + name.encode() + b"=" + value
        for name, value in fields
    )
    return struct.pack("<I", len(body)) + body


def _bag_record(fields: Sequence[tuple[str, bytes]], data: bytes) -> bytes:
    return _bag_header(fields) + struct.pack("<I", len(data)) + data


def _bag_time(ns: int) -> bytes:
    sec, nsec = divmod(ns, 10**9)
    return struct.pack("<II", sec, nsec)


@dataclass(frozen=True)
class BagConnection:
    id: int
    topic: str
    type: str
    md5sum: str
    definition: str


def ros1_md5(definition_for_md5: str) -> str:
    """ROS 1's message md5: over the definition with embedded types replaced by their md5s."""
    return hashlib.md5(definition_for_md5.encode()).hexdigest()


def ros1_bag(
    connections: Sequence[BagConnection], messages: Sequence[tuple[int, int, bytes]]
) -> tuple[bytes, Layout]:
    """A one-chunk, uncompressed bag. ``messages`` are ``(connection id, record time ns, data)``."""

    def connection(c: BagConnection) -> bytes:
        fields = [("op", b"\x07"), ("conn", struct.pack("<I", c.id)), ("topic", c.topic.encode())]
        # A connection's data is the connection header: its fields, without a leading length.
        data = _bag_header(
            [
                ("topic", c.topic.encode()),
                ("type", c.type.encode()),
                ("md5sum", c.md5sum.encode()),
                ("message_definition", c.definition.encode()),
            ]
        )[4:]
        return _bag_record(fields, data)

    chunk = _Writer()
    offsets: dict[int, list[tuple[int, int]]] = {}
    for c in connections:
        chunk.add(f"chunk_connection:{c.id}", connection(c))
    for i, (conn, time_ns, data) in enumerate(messages):
        fields = [("op", b"\x02"), ("conn", struct.pack("<I", conn)), ("time", _bag_time(time_ns))]
        offset, _ = chunk.add(f"message:{i}", _bag_record(fields, data))
        offsets.setdefault(conn, []).append((time_ns, offset))
    chunk_fields = [
        ("op", b"\x05"),
        ("compression", b"none"),
        ("size", struct.pack("<I", len(chunk.data))),
    ]
    chunk_record = _bag_record(chunk_fields, bytes(chunk.data))
    index_records = [
        (
            f"index:{conn}",
            _bag_record(
                [
                    ("op", b"\x04"),
                    ("ver", struct.pack("<I", 1)),
                    ("conn", struct.pack("<I", conn)),
                    ("count", struct.pack("<I", len(entries))),
                ],
                b"".join(_bag_time(t) + struct.pack("<I", off) for t, off in entries),
            ),
        )
        for conn, entries in sorted(offsets.items())
    ]
    chunk_pos = len(BAG_MAGIC) + 4096
    index_pos = chunk_pos + len(chunk_record) + sum(len(r) for _, r in index_records)

    out = _Writer()
    out.add("magic", BAG_MAGIC)
    header = _bag_header(
        [
            ("op", b"\x03"),
            ("index_pos", struct.pack("<Q", index_pos)),
            ("conn_count", struct.pack("<I", len(connections))),
            ("chunk_count", struct.pack("<I", 1)),
        ]
    )
    padding = 4096 - len(header) - 4  # the whole bag header record is 4096 bytes
    out.add("bag_header", header + struct.pack("<I", padding) + b" " * padding)
    out.add("chunk", chunk_record)
    chunk_data_at = chunk_pos + len(chunk_record) - len(chunk.data)
    for name, (offset, length) in chunk.layout.items():
        out.layout[name] = (chunk_data_at + offset, length)
    for name, record in index_records:
        out.add(name, record)
    for c in connections:
        out.add(f"connection:{c.id}", connection(c))
    times = [time_ns for _, time_ns, _ in messages]
    info_fields = [
        ("op", b"\x06"),
        ("ver", struct.pack("<I", 1)),
        ("chunk_pos", struct.pack("<Q", chunk_pos)),
        ("start_time", _bag_time(min(times))),
        ("end_time", _bag_time(max(times))),
        ("count", struct.pack("<I", len(offsets))),
    ]
    info_data = b"".join(struct.pack("<II", conn, len(e)) for conn, e in sorted(offsets.items()))
    out.add("chunk_info", _bag_record(info_fields, info_data))
    return bytes(out.data), out.layout


class Ros1:
    """ROS 1 serialisation: little-endian, packed, strings and arrays with a uint32 length."""

    def __init__(self) -> None:
        self.body = bytearray()

    def uint32(self, value: int) -> "Ros1":
        self.body += struct.pack("<I", value)
        return self

    def float64(self, value: float) -> "Ros1":
        self.body += struct.pack("<d", value)
        return self

    def string(self, text: str) -> "Ros1":
        data = text.encode()
        self.body += struct.pack("<I", len(data)) + data
        return self

    def header(self, seq: int, stamp_ns: int, frame_id: str) -> "Ros1":
        self.uint32(seq)
        self.body += _bag_time(stamp_ns)
        return self.string(frame_id)

    def bytes(self) -> bytes:
        return bytes(self.body)


# --- PNG with EXIF (PNG 1.2 + the 2017 eXIf extension; EXIF 2.32) ------------------------------


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


_BYTE, _ASCII, _LONG, _RATIONAL = 1, 2, 4, 5


def _ifd_size(entries: Sequence[tuple[int, int, bytes, int]]) -> int:
    """Bytes an IFD takes: its directory plus the values too long to sit in an entry."""
    extra = sum(len(value) + len(value) % 2 for _, _, value, _ in entries if len(value) > 4)
    return 2 + 12 * len(entries) + 4 + extra


def _ifd(entries: Sequence[tuple[int, int, bytes, int]], at: int) -> bytes:
    """One big-endian IFD at offset ``at``: ``(tag, type, value bytes, count)``, sorted by tag,
    followed by the values too long to sit in an entry."""
    directory = struct.pack(">H", len(entries))
    extra = b""
    extra_at = at + 2 + 12 * len(entries) + 4
    for tag, kind, value, count in sorted(entries):
        if len(value) <= 4:
            directory += struct.pack(">HHI", tag, kind, count) + value.ljust(4, b"\x00")
        else:
            directory += struct.pack(">HHII", tag, kind, count, extra_at + len(extra))
            extra += value + b"\x00" * (len(value) % 2)
    return directory + struct.pack(">I", 0) + extra


def _ascii(tag: int, text: str) -> tuple[int, int, bytes, int]:
    data = text.encode() + b"\x00"
    return tag, _ASCII, data, len(data)


def _rationals(tag: int, pairs: Sequence[tuple[int, int]]) -> tuple[int, int, bytes, int]:
    return tag, _RATIONAL, b"".join(struct.pack(">II", n, d) for n, d in pairs), len(pairs)


@dataclass(frozen=True)
class Exif:
    make: str
    model: str
    body_serial: str
    taken: str  # "YYYY:MM:DD HH:MM:SS", as EXIF writes it
    latitude: tuple[str, tuple[tuple[int, int], ...]]  # ref ("N"/"S"), degrees-minutes-seconds
    longitude: tuple[str, tuple[tuple[int, int], ...]]


def exif(tags: Exif) -> bytes:
    """A TIFF-structured EXIF block: IFD0 (make, model, pointers), the Exif IFD and the GPS IFD."""
    exif_entries = [_ascii(0x9003, tags.taken), _ascii(0xA431, tags.body_serial)]
    gps_entries = [
        (0x0000, _BYTE, bytes([2, 3, 0, 0]), 4),  # GPSVersionID 2.3.0.0
        _ascii(0x0001, tags.latitude[0]),
        _rationals(0x0002, tags.latitude[1]),
        _ascii(0x0003, tags.longitude[0]),
        _rationals(0x0004, tags.longitude[1]),
    ]
    named = [_ascii(0x010F, tags.make), _ascii(0x0110, tags.model)]
    ifd0_at = 8
    exif_at = ifd0_at + _ifd_size(
        [*named, (0x8769, _LONG, bytes(4), 1), (0x8825, _LONG, bytes(4), 1)]
    )
    gps_at = exif_at + _ifd_size(exif_entries)
    pointers = [
        (0x8769, _LONG, struct.pack(">I", exif_at), 1),
        (0x8825, _LONG, struct.pack(">I", gps_at), 1),
    ]
    return (
        b"MM\x00\x2a"
        + struct.pack(">I", ifd0_at)
        + _ifd([*named, *pointers], ifd0_at)
        + _ifd(exif_entries, exif_at)
        + _ifd(gps_entries, gps_at)
    )


def png(width: int, height: int, pixel: tuple[int, int, int], tags: Exif) -> tuple[bytes, Layout]:
    """An RGB PNG of one colour, with an eXIf chunk before the image data."""
    out = _Writer()
    out.add("signature", b"\x89PNG\r\n\x1a\n")
    out.add("IHDR", _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
    out.add("eXIf", _png_chunk(b"eXIf", exif(tags)))
    rows = b"".join(b"\x00" + bytes(pixel) * width for _ in range(height))
    out.add("IDAT", _png_chunk(b"IDAT", zlib.compress(rows, 9)))
    out.add("IEND", _png_chunk(b"IEND", b""))
    return bytes(out.data), out.layout

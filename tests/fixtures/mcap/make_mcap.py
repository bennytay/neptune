"""Generate the MCAP adapter's fixtures: one recording, written several ways, and damaged copies.

Run ``uv run python tests/fixtures/mcap/make_mcap.py`` to rewrite every fixture, then
``uv run python tests/fixtures/mcap/make_mcap.py --oracle`` to read each one with the official
``mcap`` reader (fetched by ``uv`` for that run only, never a dependency) and record what it reads
in ``oracle.json``. Output is deterministic for the pinned ``zstandard`` and ``lz4``;
``tests/unit/adapters/test_mcap_fixtures.py`` checks that every committed file is what
``build()`` gives and that ``oracle.json`` agrees with the adapter.

The recording (format version 0, profile ``ros2``):

- schemas 1 ``sensor_msgs/msg/Imu`` (ros2msg, its full definition) and 2 ``fixture.Battery``
  (jsonschema); channels 1 ``/imu`` (cdr, with QoS metadata), 2 ``/battery`` (json),
  3 ``/diagnostics`` (json, no schema) and 4 ``/imu_rear`` (declared, never written to);
- 18 messages with log and publish times that differ; one ``/imu`` message logged late (out of
  log-time order) and two sharing a log time; CDR payloads are real ``Imu`` messages;
- three chunks of 7, 6 and 5 messages, each followed by its Message Index records; an attachment
  (``calibration.yaml``) after the first chunk and a Metadata record after the second;
- a summary: schemas, channels, chunk, attachment and metadata indexes, statistics, summary
  offsets; every CRC computed.

Files (see ``README.md`` for what each one is for): ``robot.mcap`` (zstd chunks),
``robot_lz4.mcap``, ``robot_plain.mcap`` (uncompressed chunks), ``unchunked.mcap``,
``no_summary.mcap``, ``no_message_index.mcap``, ``empty.mcap``, ``unknown_encoding.mcap``, and the
damaged ``truncated.mcap``, ``bad_crc.mcap``, ``bad_summary_crc.mcap``, ``overlapping_index.mcap``,
``lying_index.mcap`` and ``unknown_compression.mcap``.
"""

import json
import struct
import subprocess
import sys
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import lz4.frame
import zstandard

HERE: Final = Path(__file__).parent
MAGIC: Final = b"\x89MCAP0\r\n"
MS: Final = 10**6
T0: Final = 1_790_000_000 * 10**9  # the recording's start, ns since the Unix epoch

IMU_DEFINITION: Final = """std_msgs/Header header
geometry_msgs/Quaternion orientation
float64[9] orientation_covariance
geometry_msgs/Vector3 angular_velocity
float64[9] angular_velocity_covariance
geometry_msgs/Vector3 linear_acceleration
float64[9] linear_acceleration_covariance
================================================================================
MSG: std_msgs/Header
builtin_interfaces/Time stamp
string frame_id
================================================================================
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec
================================================================================
MSG: geometry_msgs/Quaternion
float64 x 0
float64 y 0
float64 z 0
float64 w 1
================================================================================
MSG: geometry_msgs/Vector3
float64 x
float64 y
float64 z
"""
BATTERY_SCHEMA: Final = {
    "type": "object",
    "properties": {"voltage": {"type": "number"}, "percentage": {"type": "number"}},
}
QOS: Final = "- history: 1\n  depth: 10\n  reliability: 1\n  durability: 2\n"
CALIBRATION: Final = b"imu:\n  bias: [0.01, -0.02, 0.0]\n  frame: imu_link\n"


# --- Encoding ------------------------------------------------------------------------------------


def _string(text: str | bytes) -> bytes:
    data = text.encode() if isinstance(text, str) else text
    return struct.pack("<I", len(data)) + data


def _bytes32(data: bytes) -> bytes:
    return struct.pack("<I", len(data)) + data


def _record(opcode: int, content: bytes) -> bytes:
    return struct.pack("<BQ", opcode, len(content)) + content


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _string_map(entries: dict[str, str]) -> bytes:
    return _bytes32(b"".join(_string(k) + _string(v) for k, v in entries.items()))


def _cdr_imu(stamp: int, frame: str, ax: float, ay: float, az: float) -> bytes:
    """A ``sensor_msgs/msg/Imu`` in little-endian CDR, aligned as ROS 2 writes it."""
    out = bytearray(b"\x00\x01\x00\x00")  # encapsulation: CDR_LE

    def align(size: int) -> None:
        while (len(out) - 4) % size:
            out.append(0)

    sec, nanosec = divmod(stamp, 10**9)
    out += struct.pack("<iI", sec, nanosec)
    name = frame.encode() + b"\x00"
    out += struct.pack("<I", len(name)) + name
    align(8)
    out += struct.pack("<4d", 0.0, 0.0, 0.0, 1.0)
    out += struct.pack("<9d", *([0.0] * 9))
    out += struct.pack("<3d", 0.0, 0.0, 0.01)
    out += struct.pack("<9d", *([0.0] * 9))
    out += struct.pack("<3d", ax, ay, az)
    out += struct.pack("<9d", *([0.0] * 9))
    return bytes(out)


@dataclass(frozen=True)
class Message:
    channel: int
    sequence: int
    log_time: int
    publish_time: int
    data: bytes

    def content(self) -> bytes:
        fields = struct.pack("<HIQQ", self.channel, self.sequence, self.log_time, self.publish_time)
        return fields + self.data


def _imu(sequence: int, log_ms: int, delay_ms: int, ax: float) -> Message:
    stamp = T0 + (log_ms - 2 * delay_ms) * MS
    data = _cdr_imu(stamp, "imu_link", ax, -0.1, 9.81)
    return Message(1, sequence, T0 + log_ms * MS, T0 + (log_ms - delay_ms) * MS, data)


def _battery(sequence: int, log_ms: int, voltage: float, percentage: float | None) -> Message:
    data = _json({"percentage": percentage, "voltage": voltage})
    return Message(2, sequence, T0 + log_ms * MS, T0 + log_ms * MS - 250_000, data)


def _diagnostic(sequence: int, log_ms: int, level: str) -> Message:
    return Message(3, sequence, T0 + log_ms * MS, T0 + log_ms * MS, _json({"level": level}))


MESSAGES: Final = (
    _imu(0, 10, 1, 0.10),
    _battery(0, 12, 24.1, 0.83),
    _imu(1, 20, 1, 0.12),
    _diagnostic(0, 21, "ok"),
    _imu(2, 30, 1, 0.14),
    _imu(3, 30, 2, 0.15),  # shares its log time with the one before
    _battery(1, 32, 24.0, None),
    _imu(4, 25, 1, 0.13),  # logged late: out of log-time order
    _imu(5, 40, 1, 0.16),
    _diagnostic(1, 41, "warn"),
    _imu(6, 50, 1, 0.17),
    _battery(2, 52, 23.9, 0.81),
    _imu(7, 60, 1, 0.18),
    _imu(8, 70, 1, 0.19),
    _diagnostic(2, 71, "ok"),
    _imu(9, 80, 1, 0.20),
    _battery(3, 82, 23.8, 0.80),
    _imu(10, 90, 1, 0.21),
)
CHUNKS: Final = ((0, 7), (7, 13), (13, 18))


@dataclass(frozen=True)
class Schema:
    id: int
    name: str
    encoding: str
    data: bytes

    def record(self) -> bytes:
        body = _string(self.name) + _string(self.encoding) + _bytes32(self.data)
        return _record(0x03, struct.pack("<H", self.id) + body)


@dataclass(frozen=True)
class Channel:
    id: int
    schema_id: int
    topic: str
    encoding: str
    metadata: dict[str, str] = field(default_factory=dict)

    def record(self) -> bytes:
        body = _string(self.topic) + _string(self.encoding) + _string_map(self.metadata)
        return _record(0x04, struct.pack("<HH", self.id, self.schema_id) + body)


SCHEMAS: Final = (
    Schema(1, "sensor_msgs/msg/Imu", "ros2msg", IMU_DEFINITION.encode()),
    Schema(2, "fixture.Battery", "jsonschema", _json(BATTERY_SCHEMA)),
)
CHANNELS: Final = (
    Channel(1, 1, "/imu", "cdr", {"offered_qos_profiles": QOS}),
    Channel(2, 2, "/battery", "json"),
    Channel(3, 0, "/diagnostics", "json", {"source": "fixture"}),
    Channel(4, 1, "/imu_rear", "cdr"),
)


@dataclass(frozen=True)
class Options:
    """How the recording is written; the damage is applied afterwards."""

    compression: str = "zstd"
    chunked: bool = True
    summary: bool = True
    message_index: bool = True
    schemas: tuple[Schema, ...] = SCHEMAS
    channels: tuple[Channel, ...] = CHANNELS
    messages: tuple[Message, ...] = MESSAGES
    chunks: tuple[tuple[int, int], ...] = CHUNKS
    attachment: bool = True
    metadata: bool = True
    drop_index_entry: bool = False  # chunk 0's /imu Message Index and the statistics lie alike
    renamed: tuple[tuple[int, str], ...] = ()  # chunks that declare a compression they don't use
    statistics: bool = True
    summary_declarations: bool = True  # the summary repeats the Schema and Channel records
    unindexed: tuple[int, ...] = ()  # chunks the summary's chunk index leaves out


def _compress(compression: str, data: bytes) -> bytes:
    if compression == "zstd":
        return zstandard.ZstdCompressor(level=3, write_content_size=True).compress(data)
    if compression == "lz4":
        return bytes(lz4.frame.compress(data, compression_level=0, content_checksum=False))
    return data


class Writer:
    """Writes records and remembers where each one starts, by label."""

    def __init__(self) -> None:
        self.out = bytearray(MAGIC)
        self.at: dict[str, tuple[int, int]] = {}

    def add(self, label: str, record: bytes) -> int:
        offset = len(self.out)
        self.out += record
        self.at[label] = (offset, len(record))
        return offset


def write(options: Options) -> tuple[bytes, dict[str, tuple[int, int]]]:
    """The recording's bytes, and where each labelled record is."""
    writer = Writer()
    writer.add("header", _record(0x01, _string("ros2") + _string("neptune-fixture/1")))
    declared: set[tuple[str, int]] = set()
    chunk_indexes: list[bytes] = []
    counts: dict[int, int] = {}
    times = [m.log_time for m in options.messages]
    by_id = {s.id: s for s in options.schemas}

    def declarations(channel: Channel) -> bytes:
        out = b""
        if channel.schema_id and ("schema", channel.schema_id) not in declared:
            declared.add(("schema", channel.schema_id))
            out += by_id[channel.schema_id].record()
        if ("channel", channel.id) not in declared:
            declared.add(("channel", channel.id))
            out += channel.record()
        return out

    unused = [c for c in options.channels if all(m.channel != c.id for m in options.messages)]
    for channel in unused:  # declared at top level, before the first chunk
        writer.add(f"declare:{channel.id}", declarations(channel))
    channels = {c.id: c for c in options.channels}
    for number, (first, last) in enumerate(options.chunks if options.chunked else ((0, 0),)):
        if options.chunked:
            records = bytearray()
            entries: dict[int, list[tuple[int, int]]] = {}
            for message in options.messages[first:last]:
                records += declarations(channels[message.channel])
                entries.setdefault(message.channel, []).append((message.log_time, len(records)))
                records += _record(0x05, message.content())
                counts[message.channel] = counts.get(message.channel, 0) + 1
            stored = _compress(options.compression, bytes(records))
            name = dict(options.renamed).get(number, options.compression)
            span = [m.log_time for m in options.messages[first:last]]
            fields = struct.pack("<QQQI", min(span), max(span), len(records), zlib.crc32(records))
            content = fields + _string(name) + struct.pack("<Q", len(stored))
            start = writer.add(f"chunk:{number}", _record(0x06, content + stored))
            chunk_length = len(writer.out) - start
            offsets: dict[int, int] = {}
            if options.message_index:
                for channel_id in sorted(entries):
                    found = entries[channel_id]
                    if options.drop_index_entry and number == 0 and channel_id == 1:
                        found = found[:-1]
                    body = b"".join(struct.pack("<QQ", t, o) for t, o in found)
                    offsets[channel_id] = writer.add(
                        f"message_index:{number}:{channel_id}",
                        _record(0x07, struct.pack("<H", channel_id) + _bytes32(body)),
                    )
            index_length = len(writer.out) - start - chunk_length
            pairs = b"".join(struct.pack("<HQ", c, o) for c, o in sorted(offsets.items()))
            chunk_indexes.append(
                _record(
                    0x08,
                    struct.pack("<QQQQ", min(span), max(span), start, chunk_length)
                    + _bytes32(pairs)
                    + struct.pack("<Q", index_length)
                    + _string(name)
                    + struct.pack("<QQ", len(stored), len(records)),
                )
            )
        else:
            for index, message in enumerate(options.messages):
                writer.add(f"declare:m{index}", declarations(channels[message.channel]))
                writer.add(f"message:{index}", _record(0x05, message.content()))
                counts[message.channel] = counts.get(message.channel, 0) + 1
                if index == 6 and options.attachment:
                    _attachment(writer)
                if index == 12 and options.metadata:
                    _metadata(writer)
        if options.chunked and number == 0 and options.attachment:
            _attachment(writer)
        if options.chunked and number == 1 and options.metadata:
            _metadata(writer)
    data_end = len(writer.out)
    writer.add("data_end", _record(0x0F, struct.pack("<I", zlib.crc32(writer.out))))
    if not options.summary:
        footer = struct.pack("<BQQQ", 0x02, 20, 0, 0) + struct.pack("<I", 0)
        writer.add("footer", footer)
        return bytes(writer.out + MAGIC), writer.at
    if options.drop_index_entry:
        counts[1] -= 1
    summary_start = len(writer.out)
    groups: list[tuple[int, int, int]] = []

    def group(opcode: int, records: list[bytes]) -> None:
        if records:
            start = len(writer.out)
            for i, record in enumerate(records):
                writer.add(f"summary:{opcode}:{i}", record)
            groups.append((opcode, start, len(writer.out) - start))

    if options.summary_declarations:
        group(0x03, [s.record() for s in options.schemas])
        group(0x04, [c.record() for c in options.channels])
    group(0x08, [index for i, index in enumerate(chunk_indexes) if i not in options.unindexed])
    attachments, metadata = [], []
    if "attachment" in writer.at:
        offset, length = writer.at["attachment"]
        attachments.append(
            _record(
                0x0A,
                struct.pack("<QQQQQ", offset, length, T0 + 15 * MS, T0, len(CALIBRATION))
                + _string("calibration.yaml")
                + _string("application/yaml"),
            )
        )
    if "metadata" in writer.at:
        offset, length = writer.at["metadata"]
        metadata.append(_record(0x0D, struct.pack("<QQ", offset, length) + _string("recording")))
    group(0x0A, attachments)
    group(0x0D, metadata)
    channel_counts = b"".join(struct.pack("<HQ", c, n) for c, n in sorted(counts.items()))
    statistics = struct.pack(
        "<QHIIIIQQ",
        sum(counts.values()),
        len(options.schemas),
        len(options.channels),
        len(attachments),
        len(metadata),
        len(chunk_indexes),
        min(times) if times else 0,
        max(times) if times else 0,
    ) + _bytes32(channel_counts)
    if options.statistics:
        group(0x0B, [_record(0x0B, statistics)])
    offsets_start = len(writer.out)
    for opcode, start, length in groups:
        writer.add(
            f"summary_offset:{opcode}", _record(0x0E, struct.pack("<BQQ", opcode, start, length))
        )
    footer = struct.pack("<BQQQ", 0x02, 20, summary_start, offsets_start)
    crc = zlib.crc32(bytes(writer.out[summary_start:]) + footer)
    writer.add("footer", footer + struct.pack("<I", crc))
    assert data_end < summary_start
    return bytes(writer.out + MAGIC), writer.at


def _attachment(writer: Writer) -> None:
    fields = (
        struct.pack("<QQ", T0 + 15 * MS, T0)
        + _string("calibration.yaml")
        + _string("application/yaml")
        + struct.pack("<Q", len(CALIBRATION))
        + CALIBRATION
    )
    writer.add("attachment", _record(0x09, fields + struct.pack("<I", zlib.crc32(fields))))


def _metadata(writer: Writer) -> None:
    entries = {"operator": "ci", "robot_id": "arm-7", "site": ""}
    writer.add("metadata", _record(0x0C, _string("recording") + _string_map(entries)))


# --- Damage --------------------------------------------------------------------------------------


def _patch(data: bytes, offset: int, value: bytes) -> bytes:
    return data[:offset] + value + data[offset + len(value) :]


def _refresh_summary_crc(data: bytes) -> bytes:
    """Recompute the footer's summary CRC after a summary record was edited."""
    footer = len(data) - 8 - 29
    summary_start = struct.unpack_from("<Q", data, footer + 9)[0]
    crc = zlib.crc32(data[summary_start : footer + 25])
    return _patch(data, footer + 25, struct.pack("<I", crc))


def truncated() -> bytes:
    data, at = write(Options(compression=""))
    offset, length = at["chunk:2"]
    return data[: offset + length // 2]


def bad_crc() -> bytes:
    data, at = write(Options())
    offset, _ = at["chunk:1"]
    crc_at = offset + 9 + 24  # after start_time, end_time and uncompressed_size
    (crc,) = struct.unpack_from("<I", data, crc_at)
    return _patch(data, crc_at, struct.pack("<I", crc ^ 0xFFFFFFFF))


def bad_summary_crc() -> bytes:
    data, _ = write(Options())
    crc_at = len(data) - 8 - 4
    (crc,) = struct.unpack_from("<I", data, crc_at)
    return _patch(data, crc_at, struct.pack("<I", crc ^ 0x5A5A5A5A))


def overlapping_index() -> bytes:
    data, at = write(Options())
    second, _ = at["summary:8:1"]
    first_chunk, _ = at["chunk:0"]
    # Chunk index 1 claims chunk 1 starts where chunk 0 does.
    data = _patch(data, second + 9 + 16, struct.pack("<Q", first_chunk))
    return _refresh_summary_crc(data)


def unknown_encoding() -> bytes:
    schemas = (*SCHEMAS, Schema(3, "fixture.Pose", "neptune-test-idl", b"pose { x y z }"))
    channels = (*CHANNELS, Channel(5, 3, "/pose", "neptune-test"))
    messages = (*MESSAGES, Message(5, 0, T0 + 95 * MS, T0 + 95 * MS, b"\x01\x02\x03"))
    chunks = ((0, 7), (7, 13), (13, 19))
    data, _ = write(Options(schemas=schemas, channels=channels, messages=messages, chunks=chunks))
    return data


FILES: Final[dict[str, Callable[[], bytes]]] = {
    "robot.mcap": lambda: write(Options())[0],
    "robot_lz4.mcap": lambda: write(Options(compression="lz4"))[0],
    "robot_plain.mcap": lambda: write(Options(compression=""))[0],
    "unchunked.mcap": lambda: write(Options(chunked=False))[0],
    "no_summary.mcap": lambda: write(Options(summary=False))[0],
    "no_message_index.mcap": lambda: write(Options(message_index=False))[0],
    "empty.mcap": lambda: write(
        Options(channels=(), schemas=(), messages=(), chunks=(), attachment=False, metadata=False)
    )[0],
    "unknown_encoding.mcap": unknown_encoding,
    "truncated.mcap": truncated,
    "bad_crc.mcap": bad_crc,
    "bad_summary_crc.mcap": bad_summary_crc,
    "overlapping_index.mcap": overlapping_index,
    "lying_index.mcap": lambda: write(Options(drop_index_entry=True))[0],
    "unknown_compression.mcap": lambda: write(Options(compression="", renamed=((1, "brotli"),)))[0],
}


def build() -> dict[str, bytes]:
    """Every fixture's bytes, by file name."""
    return {name: make() for name, make in FILES.items()}


# --- The official reader as an oracle ------------------------------------------------------------

_ORACLE: Final = r"""
import json, sys
from pathlib import Path
from mcap.reader import make_reader
from mcap.stream_reader import StreamReader
from mcap.records import Channel, Message

def streamed(path):
    channels, messages = {}, []
    try:
        with path.open("rb") as stream:
            for record in StreamReader(stream, skip_magic=False, validate_crcs=True).records:
                if isinstance(record, Channel):
                    channels[record.id] = record.topic
                elif isinstance(record, Message):
                    messages.append([channels.get(record.channel_id, ""), record.sequence,
                                     record.log_time, record.publish_time, len(record.data)])
        return {"error": None, "messages": messages}
    except Exception as exc:
        return {"error": type(exc).__name__, "messages": messages}

def indexed(path):
    out = {"error": None, "messages": [], "statistics": None}
    try:
        with path.open("rb") as stream:
            reader = make_reader(stream, validate_crcs=True)
            summary = reader.get_summary()
            if summary is not None and summary.statistics is not None:
                stats = summary.statistics
                out["statistics"] = {
                    "channel_message_counts": {str(k): v for k, v in sorted(
                        stats.channel_message_counts.items())},
                    "message_count": stats.message_count,
                    "message_end_time": stats.message_end_time,
                    "message_start_time": stats.message_start_time,
                }
            for _, channel, message in reader.iter_messages(log_time_order=False):
                out["messages"].append([channel.topic, message.sequence, message.log_time,
                                        message.publish_time, len(message.data)])
    except Exception as exc:
        out["error"] = type(exc).__name__
    return out

out = {}
for path in sorted(Path(sys.argv[1]).glob("*.mcap")):
    out[path.name] = {"indexed": indexed(path), "streamed": streamed(path)}
print(json.dumps(out, indent=1, sort_keys=True))
"""


def oracle() -> str:
    """What the official ``mcap`` reader reads from every committed fixture, in file order."""
    command = ["uv", "run", "--no-project", "--with", "mcap==1.5.0", "python", "-c", _ORACLE]
    result = subprocess.run(
        [*command, str(HERE)], check=True, capture_output=True, text=True, cwd="/tmp"
    )
    return result.stdout


if __name__ == "__main__":
    if sys.argv[1:] == ["--oracle"]:
        (HERE / "oracle.json").write_text(oracle())
    else:
        for name, data in build().items():
            (HERE / name).write_bytes(data)

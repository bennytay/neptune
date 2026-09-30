"""Generate ``imu.mcap``: a small, spec-valid MCAP file whose messages carry several clocks.

Run ``uv run python tests/fixtures/series/make_imu_mcap.py`` to rewrite the fixture. The output is
byte-for-byte deterministic, and ``tests/integration/test_series_provenance.py`` checks that the
committed file is exactly what ``build()`` returns.

Layout (MCAP version 0, no chunks, JSON messages):

- magic, Header, Schema 1 (``fixture.Imu``, JSON Schema), Channel 1 ``/imu`` (schema 1, with ROS 2
  style QoS metadata), Channel 2 ``/battery`` (schema 0: no schema);
- six Messages. Every message has a log time and a publish time; ``/imu`` messages also carry a
  ``header.stamp``. One ``/imu`` message was recorded late (out of log-time order), two share a
  log time, and one ``/battery`` message has no ``percentage``;
- DataEnd (data section CRC 0: "not available"), then a summary section (Schema, Channels,
  Statistics), Footer with the summary CRC, magic.
"""

import json
import struct
import zlib
from pathlib import Path
from typing import Final

MAGIC: Final = b"\x89MCAP0\r\n"
MS: Final = 10**6  # nanoseconds
T0: Final = 1_700_000_000 * 10**9  # the recording's zero, ns since the Unix epoch
IMU_SCHEMA: Final = {
    "type": "object",
    "properties": {
        "header": {
            "type": "object",
            "properties": {
                "stamp": {
                    "type": "object",
                    "description": "when the sample was taken: sec + nanosec",
                    "properties": {"sec": {"type": "integer"}, "nanosec": {"type": "integer"}},
                },
                "frame_id": {"type": "string"},
            },
        },
        "linear_acceleration": {
            "type": "object",
            "properties": {axis: {"type": "number"} for axis in ("x", "y", "z")},
        },
    },
}
IMU_QOS: Final = "- history: 1\n  depth: 10\n  reliability: 1\n  durability: 2\n"


def _string(text: str) -> bytes:
    data = text.encode()
    return struct.pack("<I", len(data)) + data


def _prefixed(body: bytes) -> bytes:
    return struct.pack("<I", len(body)) + body


def _record(opcode: int, content: bytes) -> bytes:
    return struct.pack("<BQ", opcode, len(content)) + content


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _schema() -> bytes:
    body = _string("fixture.Imu") + _string("jsonschema") + _prefixed(_json(IMU_SCHEMA))
    return _record(0x03, struct.pack("<H", 1) + body)


def _channel(channel_id: int, schema_id: int, topic: str, metadata: dict[str, str]) -> bytes:
    entries = b"".join(_string(key) + _string(value) for key, value in sorted(metadata.items()))
    body = _string(topic) + _string("json") + _prefixed(entries)
    return _record(0x04, struct.pack("<HH", channel_id, schema_id) + body)


def _message(
    channel_id: int, sequence: int, log_time: int, publish_time: int, data: object
) -> bytes:
    fields = struct.pack("<HIQQ", channel_id, sequence, log_time, publish_time)
    return _record(0x05, fields + _json(data))


def _imu(stamp: int, x: float, y: float) -> dict[str, object]:
    sec, nanosec = divmod(stamp, 10**9)
    return {
        "header": {"frame_id": "imu_link", "stamp": {"nanosec": nanosec, "sec": sec}},
        "linear_acceleration": {"x": x, "y": y, "z": 9.81},
    }


MESSAGES: Final = (
    (1, 0, T0 + 10 * MS, T0 + 9 * MS, _imu(T0 + 8 * MS, 0.1, 0.0)),
    (2, 0, T0 + 15 * MS, T0 + 15 * MS, {"percentage": 0.83, "voltage": 24.1}),
    (1, 1, T0 + 20 * MS, T0 + 19 * MS, _imu(T0 + 18 * MS, 0.2, -0.1)),
    (1, 2, T0 + 20 * MS, T0 + 19 * MS + MS // 2, _imu(T0 + 19 * MS, 0.3, -0.1)),  # a log-time tie
    (1, 3, T0 + 15 * MS, T0 + 14 * MS, _imu(T0 + 13 * MS, 0.15, 0.05)),  # recorded late
    (2, 1, T0 + 30 * MS, T0 + 30 * MS, {"percentage": None, "voltage": 24.0}),  # not reported
)


def _statistics() -> bytes:
    log_times = [log_time for _, _, log_time, _, _ in MESSAGES]
    counts = {channel: sum(1 for m in MESSAGES if m[0] == channel) for channel in (1, 2)}
    fields = struct.pack("<QHIIIIQQ", len(MESSAGES), 1, 2, 0, 0, 0, min(log_times), max(log_times))
    entries = b"".join(struct.pack("<HQ", channel, count) for channel, count in counts.items())
    return _record(0x0B, fields + _prefixed(entries))


def build() -> bytes:
    """The fixture's bytes."""
    declarations = (
        _schema()
        + _channel(1, 1, "/imu", {"offered_qos_profiles": IMU_QOS})
        + _channel(2, 0, "/battery", {})
    )
    data = (
        MAGIC
        + _record(0x01, _string("") + _string("neptune-fixture/1"))
        + declarations
        + b"".join(_message(*message) for message in MESSAGES)
        + _record(0x0F, struct.pack("<I", 0))
    )
    summary = declarations + _statistics()
    # The summary CRC covers the summary section and the footer up to its own field.
    footer = struct.pack("<BQQQ", 0x02, 20, len(data), 0)
    crc = zlib.crc32(summary + footer)
    return data + summary + footer + struct.pack("<I", crc) + MAGIC


if __name__ == "__main__":
    Path(__file__).with_name("imu.mcap").write_bytes(build())

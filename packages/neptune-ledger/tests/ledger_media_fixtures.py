"""Frozen source files for the media tests (MVL-96, ADR 0014), and the script that made them.

Every file in ``tests/fixtures/media/`` was written once by ``python ledger_media_fixtures.py``
from this directory and committed; the tests read the committed bytes, never regenerate them, so
a library upgrade cannot change a fixture silently. Each file is a real, small instance of its
format, checked with that format's own reader (``mcap``, Pillow, pypdfium2, pyarrow, tarfile):

- ``wrist_camera.mcap``: a manipulator's wrist camera, three ROS 2
  ``sensor_msgs/msg/CompressedImage`` frames (PNG, 16 x 12) on ``/wrist_camera/image/compressed``.
- ``head_camera.mcap``: a quadruped's head camera, ``sensor_msgs/msg/Image`` frames (``rgb8``,
  8 x 6), the last two sharing one log tick.
- ``inspection_report.pdf``: a drone's two-page inspection report; page 1 has ``/Rotate 90``.
- ``mission.txt``: a drone mission note in UTF-8 with characters outside ASCII.
- ``gimbal.mp4``: a drone gimbal clip's ISO BMFF boxes (``ftyp``, ``free``, ``mdat``); only its
  bytes are read, never decoded.
- ``sites.parquet``: a mobile robot's site register as Parquet, two row groups.
- ``leg_calibration.tar``: a quadruped's calibration bundle: ``intrinsics.yaml`` and the gzip
  member ``extrinsics.json.gz``.

Frame pixels are a function of position and frame index (``pixel``), so a test can check a
decoded frame without a golden image.
"""

import gzip
import io
import json
import struct
import sys
import tarfile
import zlib
from pathlib import Path
from typing import Any, Final

FIXTURES: Final = Path(__file__).parent / "fixtures" / "media"
START: Final = 1_790_762_401_000_000_000  # ns on each recording's log clock
PERIOD: Final = 33_333_333  # 30 Hz

WRIST_TOPIC: Final = "/wrist_camera/image/compressed"
HEAD_TOPIC: Final = "/head_camera/image_raw"
WRIST_SIZE: Final = (16, 12)
HEAD_SIZE: Final = (8, 6)

_TIME = "int32 sec\nuint32 nanosec\n"
_HEADER = "builtin_interfaces/Time stamp\nstring frame_id\n"
_SEP = "=" * 80
COMPRESSED_IMAGE_DEF: Final = (
    f"std_msgs/Header header\nstring format\nuint8[] data\n{_SEP}\nMSG: std_msgs/Header\n"
    f"{_HEADER}{_SEP}\nMSG: builtin_interfaces/Time\n{_TIME}"
)
IMAGE_DEF: Final = (
    "std_msgs/Header header\nuint32 height\nuint32 width\nstring encoding\nuint8 is_bigendian\n"
    f"uint32 step\nuint8[] data\n{_SEP}\nMSG: std_msgs/Header\n{_HEADER}{_SEP}\n"
    f"MSG: builtin_interfaces/Time\n{_TIME}"
)


def pixel(x: int, y: int, frame: int) -> tuple[int, int, int]:
    """The RGB value of every synthetic frame at ``(x, y)``: a gradient that moves per frame."""
    return ((x * 16 + frame * 40) % 256, (y * 20 + frame * 7) % 256, (x * y + frame) % 256)


def rgb_rows(size: tuple[int, int], frame: int) -> bytes:
    width, height = size
    return bytes(c for y in range(height) for x in range(width) for c in pixel(x, y, frame))


def png(size: tuple[int, int], raw_rgb: bytes) -> bytes:
    """A PNG written with the standard library: one IDAT, filter 0, zlib level 9."""
    width, height = size

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    rows = b"".join(b"\x00" + raw_rgb[y * width * 3 : (y + 1) * width * 3] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def _stamp(at: int) -> dict[str, int]:
    return {"sec": at // 1_000_000_000, "nanosec": at % 1_000_000_000}


def wrist_camera() -> bytes:
    from mcap.writer import CompressionType
    from mcap_ros2.writer import Writer

    out = io.BytesIO()
    writer = Writer(out, compression=CompressionType.NONE)
    schema = writer.register_msgdef("sensor_msgs/msg/CompressedImage", COMPRESSED_IMAGE_DEF)
    for frame in range(3):
        at = START + frame * PERIOD
        message = {
            "header": {"stamp": _stamp(at), "frame_id": "wrist_camera_optical"},
            "format": "png",
            "data": png(WRIST_SIZE, rgb_rows(WRIST_SIZE, frame)),
        }
        writer.write_message(WRIST_TOPIC, schema, message, log_time=at, publish_time=at)
    writer.finish()  # type: ignore[no-untyped-call]
    return out.getvalue()


def head_camera() -> bytes:
    from mcap.writer import CompressionType
    from mcap_ros2.writer import Writer

    out = io.BytesIO()
    writer = Writer(out, compression=CompressionType.NONE)
    schema = writer.register_msgdef("sensor_msgs/msg/Image", IMAGE_DEF)
    width, height = HEAD_SIZE
    # Frames 0, 1 and 2; frame 3 is logged on frame 2's tick, so [t2, t2 + 1) holds two.
    ticks = [START + i * PERIOD for i in range(3)] + [START + 2 * PERIOD]
    for frame, at in enumerate(ticks):
        message = {
            "header": {"stamp": _stamp(at), "frame_id": "head_camera_optical"},
            "height": height,
            "width": width,
            "encoding": "rgb8",
            "is_bigendian": 0,
            "step": width * 3,
            "data": rgb_rows(HEAD_SIZE, frame),
        }
        writer.write_message(HEAD_TOPIC, schema, message, log_time=at, publish_time=at)
    writer.finish()  # type: ignore[no-untyped-call]
    return out.getvalue()


def _pdf(objects: list[bytes]) -> bytes:
    """A PDF 1.4 file of ``objects`` (numbered from 1), with an exact cross-reference table."""
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % at for at in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def _stream(content: bytes) -> bytes:
    return b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"


def inspection_report() -> bytes:
    """Page 0: 200 x 100 pt, a red box at [20, 120) x [10, 60) and a label. Page 1: rotated."""
    page0 = (
        b"1 0 0 rg 20 10 100 50 re f 0 0 1 rg 150 70 30 20 re f"
        b" BT /F1 10 Tf 20 80 Td (Blade 2) Tj ET"
    )
    page1 = b"0 0.5 0 rg 10 10 40 40 re f"
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Contents 5 0 R"
            b" /Resources << /Font << /F1 7 0 R >> >> >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 60] /Rotate 90 /Contents 6 0 R >>",
            _stream(page0),
            _stream(page1),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
    )


MISSION: Final = "Mission 7 — façade survey\nWaypoint 3: hover 4 m from the north façade\n"


def sites_parquet() -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table(
        {
            "site_id": ["S-007", "S-008", "S-009"],
            "name": ["North Plant", "Berth 4", "Charging bay"],
            "latitude": [-33.8651, -33.8612, None],
            "dock": [1, None, 3],
        }
    )
    out = io.BytesIO()
    pq.write_table(table, out, row_group_size=2, compression="NONE")
    return out.getvalue()


INTRINSICS: Final = (
    b"camera: head\nfx: 412.5\nfy: 412.5\ncx: 4.0\ncy: 3.0\ndistortion: [0.01, -0.002]\n"
)
EXTRINSICS: Final = {"child": "head_camera_optical", "parent": "body", "x": 0.21, "z": 0.05}


def leg_calibration() -> bytes:
    members = {
        "intrinsics.yaml": INTRINSICS,
        "extrinsics.json.gz": gzip.compress(
            json.dumps(EXTRINSICS, sort_keys=True).encode(), compresslevel=9, mtime=0
        ),
    }
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 1_790_762_400, 0o644
            info.uname = info.gname = "robot"
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


def gimbal_clip() -> bytes:
    def box(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", 8 + len(body)) + kind + body

    payload = bytes((i * 37) % 256 for i in range(4096))
    return (
        box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41")
        + box(b"free", b"")
        + box(b"mdat", payload)
    )


def build() -> dict[str, bytes]:
    return {
        "wrist_camera.mcap": wrist_camera(),
        "head_camera.mcap": head_camera(),
        "inspection_report.pdf": inspection_report(),
        "mission.txt": MISSION.encode("utf-8"),
        "gimbal.mp4": gimbal_clip(),
        "sites.parquet": sites_parquet(),
        "leg_calibration.tar": leg_calibration(),
    }


def rewrite_mcap(data: bytes, **options: Any) -> bytes:
    """The recording's messages written again with other writer options (same bytes per message)."""
    from mcap.reader import make_reader
    from mcap.writer import Writer

    out = io.BytesIO()
    writer = Writer(out, **options)
    writer.start(profile="ros2", library="neptune-ledger tests")
    schemas: dict[int, int] = {}
    channels: dict[int, int] = {}
    for schema, channel, message in make_reader(io.BytesIO(data)).iter_messages(
        log_time_order=False
    ):
        assert schema is not None
        if schema.id not in schemas:
            schemas[schema.id] = writer.register_schema(schema.name, schema.encoding, schema.data)
        if channel.id not in channels:
            channels[channel.id] = writer.register_channel(
                channel.topic, channel.message_encoding, schemas[schema.id], channel.metadata
            )
        writer.add_message(
            channels[channel.id], message.log_time, message.data, message.publish_time
        )
    writer.finish()  # type: ignore[no-untyped-call]
    return out.getvalue()


def fixture(name: str) -> bytes:
    """A committed fixture's bytes."""
    return (FIXTURES / name).read_bytes()


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, data in build().items():
        (FIXTURES / name).write_bytes(data)
        sys.stderr.write(f"{name}: {len(data)} bytes\n")

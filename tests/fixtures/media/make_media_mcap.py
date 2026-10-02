"""Generate ``media.mcap``: four robots' media streams in one small MCAP, and the hour-long run
the acceptance test writes to a temporary directory (never committed).

Run ``uv run python tests/fixtures/media/make_media_mcap.py`` to rewrite ``media.mcap``. Output is
deterministic for the pinned ``zstandard``; ``tests/integration/test_media_job.py`` checks the
committed file is exactly what ``build()`` returns.

``media.mcap`` (profile ``ros2``, zstd chunks, CDR payloads that are real ROS 2 messages):

- ``/arm/wrist_camera/image_raw``: a manipulator's wrist camera, ``sensor_msgs/msg/Image``,
  ``rgb8`` 8x6;
- ``/quadruped/depth/image_rect``: a quadruped's depth camera, ``sensor_msgs/msg/Image``,
  ``16UC1`` 8x6, big-endian samples;
- ``/av/lidar/points``: an autonomous vehicle's lidar, ``sensor_msgs/msg/PointCloud2``, x, y, z
  and intensity as float32, 16 points;
- ``/drone/gimbal/image/compressed``: a drone gimbal's ``sensor_msgs/msg/CompressedImage`` (JPEG
  bytes, never decoded) and ``/drone/gimbal/video``: its ``foxglove_msgs/msg/CompressedVideo``
  H.264 packets;
- ``/arm/joint_states``: a ``sensor_msgs/msg/JointState`` that is not media.

Every message has a log time, a publish time 2 ms before it and a ``header.stamp`` 5 ms before
that, all different, so a test can tell the clocks apart. Messages are spread over 4 chunks.
"""

import functools
import importlib.util
import struct
from pathlib import Path
from types import ModuleType
from typing import Final

HERE: Final = Path(__file__).resolve().parent


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mcap = _load("make_mcap_for_media", HERE.parent / "mcap" / "make_mcap.py")

MS: Final = 10**6
T0: Final = 1_700_000_000 * 10**9
_SEP: Final = "=" * 80
TIME: Final = "MSG: builtin_interfaces/Time\nint32 sec\nuint32 nanosec\n"
HEADER: Final = (
    f"MSG: std_msgs/Header\nbuiltin_interfaces/Time stamp\nstring frame_id\n{_SEP}\n{TIME}"
)
IMAGE_MSG: Final = (
    "std_msgs/Header header\nuint32 height\nuint32 width\nstring encoding\nuint8 is_bigendian\n"
    f"uint32 step\nuint8[] data\n{_SEP}\n{HEADER}"
)
COMPRESSED_MSG: Final = f"std_msgs/Header header\nstring format\nuint8[] data\n{_SEP}\n{HEADER}"
CLOUD_MSG: Final = (
    "std_msgs/Header header\nuint32 height\nuint32 width\nsensor_msgs/PointField[] fields\n"
    "bool is_bigendian\nuint32 point_step\nuint32 row_step\nuint8[] data\nbool is_dense\n"
    f"{_SEP}\nMSG: sensor_msgs/PointField\nstring name\nuint32 offset\nuint8 datatype\n"
    f"uint32 count\n{_SEP}\n{HEADER}"
)
VIDEO_MSG: Final = f"builtin_interfaces/Time timestamp\nstring frame_id\nuint8[] data\nstring format\n{_SEP}\n{TIME}"
JOINTS_MSG: Final = (
    "std_msgs/Header header\nstring[] name\nfloat64[] position\nfloat64[] velocity\n"
    f"float64[] effort\n{_SEP}\n{HEADER}"
)


class Cdr:
    """Little-endian CDR, aligned as ROS 2 writes it."""

    def __init__(self) -> None:
        self.out = bytearray(b"\x00\x01\x00\x00")

    def align(self, size: int) -> None:
        while (len(self.out) - 4) % size:
            self.out.append(0)

    def put(self, code: str, *values: object) -> "Cdr":
        self.align(struct.calcsize(code[0]))
        self.out += struct.pack("<" + code, *values)
        return self

    def text(self, value: str) -> "Cdr":
        data = value.encode() + b"\0"
        return self.put("I", len(data)).raw(data)

    def blob(self, data: bytes) -> "Cdr":
        return self.put("I", len(data)).raw(data)

    def raw(self, data: bytes) -> "Cdr":
        self.out += data
        return self

    def header(self, stamp: int, frame: str) -> "Cdr":
        return self.put("iI", *divmod(stamp, 10**9)).text(frame)


@functools.cache
def _pixels(width: int, height: int, size: int) -> bytes:
    return bytes(
        (x * 7 + y * 13 + i) % 251 for y in range(height) for x in range(width) for i in range(size)
    )


def image(stamp: int, frame: str, encoding: str, width: int, height: int, size: int) -> bytes:
    big = encoding == "16UC1"
    pixels = _pixels(width, height, size)
    cdr = Cdr().header(stamp, frame).put("I", height).put("I", width).text(encoding)
    return bytes(cdr.put("B", int(big)).put("I", width * size).blob(pixels).out)


def compressed(stamp: int, frame: str, body: bytes) -> bytes:
    return bytes(
        Cdr().header(stamp, frame).text("jpeg").blob(b"\xff\xd8\xff\xe0" + body + b"\xff\xd9").out
    )


def cloud(stamp: int, frame: str, points: int) -> bytes:
    cdr = Cdr().header(stamp, frame).put("I", 1).put("I", points).put("I", 4)
    for offset, name in enumerate(("x", "y", "z", "intensity")):
        cdr.text(name).put("I", offset * 4).put("B", 7).put("I", 1)
    data = b"".join(struct.pack("<4f", i, -i, 0.5 * i, 100.0) for i in range(points))
    return bytes(cdr.put("B", 0).put("I", 16).put("I", 16 * points).blob(data).put("B", 1).out)


def video(stamp: int, frame: str, key: bool) -> bytes:
    nal = b"\x00\x00\x00\x01" + (b"\x65" if key else b"\x41") + bytes(range(24))
    cdr = Cdr().put("iI", *divmod(stamp, 10**9)).text(frame).blob(nal)
    return bytes(cdr.text("h264").out)


def joints(stamp: int) -> bytes:
    cdr = Cdr().header(stamp, "arm_base").put("I", 1).text("shoulder")
    for _ in range(3):
        cdr.put("I", 1).put("d", 0.25)
    return bytes(cdr.out)


SCHEMAS: Final = (
    mcap.Schema(1, "sensor_msgs/msg/Image", "ros2msg", IMAGE_MSG.encode()),
    mcap.Schema(2, "sensor_msgs/msg/PointCloud2", "ros2msg", CLOUD_MSG.encode()),
    mcap.Schema(3, "sensor_msgs/msg/CompressedImage", "ros2msg", COMPRESSED_MSG.encode()),
    mcap.Schema(4, "foxglove_msgs/msg/CompressedVideo", "ros2msg", VIDEO_MSG.encode()),
    mcap.Schema(5, "sensor_msgs/msg/JointState", "ros2msg", JOINTS_MSG.encode()),
)
CHANNELS: Final = (
    mcap.Channel(1, 1, "/arm/wrist_camera/image_raw", "cdr"),
    mcap.Channel(2, 1, "/quadruped/depth/image_rect", "cdr"),
    mcap.Channel(3, 2, "/av/lidar/points", "cdr"),
    mcap.Channel(4, 3, "/drone/gimbal/image/compressed", "cdr"),
    mcap.Channel(5, 4, "/drone/gimbal/video", "cdr"),
    mcap.Channel(6, 5, "/arm/joint_states", "cdr"),
)


def _payload(channel: int, stamp: int, n: int) -> bytes:
    if channel == 1:
        return image(stamp, "wrist_camera", "rgb8", 8, 6, 3)
    if channel == 2:
        return image(stamp, "depth_optical", "16UC1", 8, 6, 2)
    if channel == 3:
        return cloud(stamp, "lidar_top", 16)
    if channel == 4:
        return compressed(stamp, "gimbal_optical", bytes(range(n % 7, n % 7 + 32)))
    if channel == 5:
        return video(stamp, "gimbal_optical", key=n % 4 == 0)
    return joints(stamp)


def messages() -> tuple[object, ...]:
    """Each channel at 10 Hz for 1 s, interleaved in log-time order."""
    found = []
    for n in range(10):
        for channel in range(1, 7):
            log = T0 + n * 100 * MS + channel * MS
            publish = log - 2 * MS
            found.append(
                mcap.Message(channel, n, log, publish, _payload(channel, publish - 5 * MS, n))
            )
    return tuple(found)


def build() -> bytes:
    found = messages()
    chunks = tuple((i, min(i + 15, len(found))) for i in range(0, len(found), 15))
    options = mcap.Options(
        schemas=SCHEMAS,
        channels=CHANNELS,
        messages=found,
        chunks=chunks,
        attachment=False,
        metadata=False,
    )
    data, _ = mcap.write(options)
    return bytes(data)


def hour(path: Path, *, seconds: int = 3600, rate: int = 5, side: int = 32) -> int:
    """Write an hour-long run of one wrist camera (``rgb8``, ``side`` x ``side``) at ``rate`` Hz
    and a lidar at 1 Hz, one zstd chunk a second; return its size."""
    found = []
    for s in range(seconds):
        for k in range(rate):
            log = T0 + s * 10**9 + k * (10**9 // rate)
            stamp = log - 7 * MS
            pixels = image(stamp, "wrist_camera", "rgb8", side, side, 3)
            found.append(mcap.Message(1, s * rate + k, log, log - 2 * MS, pixels))
        log = T0 + s * 10**9 + 500 * MS
        found.append(mcap.Message(3, s, log, log - 2 * MS, cloud(log - 7 * MS, "lidar_top", 64)))
    per = rate + 1
    chunks = tuple((i, i + per) for i in range(0, len(found), per))
    options = mcap.Options(
        schemas=SCHEMAS[:2],
        channels=(CHANNELS[0], CHANNELS[2]),
        messages=tuple(found),
        chunks=chunks,
        attachment=False,
        metadata=False,
        compression="",
    )
    data, _ = mcap.write(options)
    path.write_bytes(data)
    return len(data)


if __name__ == "__main__":
    (HERE / "media.mcap").write_bytes(build())

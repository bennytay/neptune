"""Generates the rosbag2 fixtures: one mobile-base recording written to both storage backends.

    uv run --no-project --with mcap --with rosbags python tests/fixtures/rosbag2/make_rosbag2.py
    ... make_rosbag2.py --oracle

The first form writes the bags next to this file; the second prints what the official readers
(``rosbags``' rosbag2 reader and the ``mcap`` package) read from them, which is committed as
``oracle.json``. Neither package is a project dependency. The recording is a mobile base driving:
velocity commands, a battery voltage and a status text, 18 messages in all, with the QoS profile
text rosbag2 writes. The sqlite3 bag and the MCAP bag hold the same messages; ``split_sqlite3``
holds them as two parts of one bag.
"""

import json
import sqlite3
import struct
import sys
from pathlib import Path

HERE = Path(__file__).parent
START = 1_700_000_000_000_000_000
STEP = 100_000_000  # 10 Hz
QOS = (
    "- history: 3\n  depth: 0\n  reliability: 1\n  durability: 2\n"
    "  deadline:\n    sec: 9223372036\n"
    "    nsec: 854775807\n  lifespan:\n    sec: 9223372036\n    nsec: 854775807\n  liveliness: 1\n"
    "  liveliness_lease_duration:\n    sec: 9223372036\n    nsec: 854775807\n"
    "  avoid_ros_namespace_conventions: false\n"
)
TWIST = (
    "Vector3  linear\nVector3  angular\n\n"
    + "=" * 80
    + "\nMSG: geometry_msgs/Vector3\nfloat64 x\nfloat64 y\nfloat64 z\n"
)
TOPICS = (
    # name, type, definition
    ("/cmd_vel", "geometry_msgs/msg/Twist", TWIST),
    ("/battery_voltage", "std_msgs/msg/Float32", "float32 data\n"),
    ("/status", "std_msgs/msg/String", "string data\n"),
)
CDR = b"\x00\x01\x00\x00"


def messages() -> list[tuple[int, str, bytes]]:
    """(timestamp, topic, CDR payload) in timestamp order: the recording."""
    out = []
    for i in range(10):
        twist = struct.pack("<6d", 0.2 + 0.01 * i, 0, 0, 0, 0, 0.1 * (i % 3))
        out.append((START + i * STEP, "/cmd_vel", CDR + twist))
    for i in range(5):
        out.append(
            (
                START + i * 2 * STEP + 5_000_000,
                "/battery_voltage",
                CDR + struct.pack("<f", 12.5 - 0.1 * i),
            )
        )
    for i, text in enumerate(("idle", "driving", "docking")):
        raw = text.encode() + b"\0"
        out.append(
            (START + i * 4 * STEP + 7_000_000, "/status", CDR + struct.pack("<I", len(raw)) + raw)
        )
    return sorted(out, key=lambda m: (m[0], m[1]))


def metadata(storage: str, parts: list[tuple[str, list[tuple[int, str, bytes]]]]) -> str:
    """The metadata.yaml rosbag2 (Humble, version 5) writes for these parts."""
    every = [m for _, part in parts for m in part]
    first, last = every[0][0], every[-1][0]
    counts = {name: sum(1 for m in every if m[1] == name) for name, _, _ in TOPICS}
    q = json.dumps(QOS)
    lines = [
        "rosbag2_bagfile_information:",
        "  version: 5",
        f"  storage_identifier: {storage}",
        "  relative_file_paths:",
        *(f"    - {name}" for name, _ in parts),
        "  duration:",
        f"    nanoseconds: {last - first}",
        "  starting_time:",
        f"    nanoseconds_since_epoch: {first}",
        f"  message_count: {len(every)}",
        "  topics_with_message_count:",
    ]
    for name, kind, _ in TOPICS:
        lines += [
            "    - topic_metadata:",
            f"        name: {name}",
            f"        type: {kind}",
            "        serialization_format: cdr",
            f"        offered_qos_profiles: {q}",
            f"      message_count: {counts[name]}",
        ]
    lines += ['  compression_format: ""', '  compression_mode: ""', "  files:"]
    for name, part in parts:
        lines += [
            f"    - path: {name}",
            "      starting_time:",
            f"        nanoseconds_since_epoch: {part[0][0]}",
            "      duration:",
            f"        nanoseconds: {part[-1][0] - part[0][0]}",
            f"      message_count: {len(part)}",
        ]
    return "\n".join(lines) + "\n"


def write_sqlite(path: Path, part: list[tuple[int, str, bytes]]) -> None:
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.executescript(
        """
        PRAGMA page_size = 4096;
        CREATE TABLE schema(schema_version INTEGER PRIMARY KEY, ros_distro TEXT NOT NULL);
        CREATE TABLE topics(id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
            serialization_format TEXT NOT NULL, offered_qos_profiles TEXT NOT NULL,
            type_description_hash TEXT NOT NULL);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, topic_id INTEGER NOT NULL,
            timestamp INTEGER NOT NULL, data BLOB NOT NULL);
        CREATE INDEX timestamp_idx ON messages (timestamp ASC);
        CREATE TABLE message_definitions(id INTEGER PRIMARY KEY, topic_type TEXT NOT NULL,
            encoding TEXT NOT NULL, encoded_message_definition TEXT NOT NULL,
            type_description_hash TEXT NOT NULL);
        """
    )
    db.execute("INSERT INTO schema VALUES (4, 'jazzy')")
    ids = {}
    for number, (name, kind, definition) in enumerate(TOPICS, 1):
        ids[name] = number
        db.execute("INSERT INTO topics VALUES (?, ?, ?, 'cdr', ?, '')", (number, name, kind, QOS))
        db.execute(
            "INSERT INTO message_definitions(topic_type, encoding, encoded_message_definition,"
            " type_description_hash) VALUES (?, 'ros2msg', ?, '')",
            (kind, definition),
        )
    db.executemany(
        "INSERT INTO messages(topic_id, timestamp, data) VALUES (?, ?, ?)",
        [(ids[topic], stamp, data) for stamp, topic, data in part],
    )
    db.commit()
    db.execute("VACUUM")
    db.close()


def write_mcap(path: Path, part: list[tuple[int, str, bytes]]) -> None:
    from mcap.writer import CompressionType, Writer

    with path.open("wb") as stream:
        writer = Writer(stream, compression=CompressionType.ZSTD)
        writer.start(profile="ros2", library="rosbag2_storage_mcap")
        schemas = {
            kind: writer.register_schema(kind, "ros2msg", text.encode()) for _, kind, text in TOPICS
        }
        channels = {
            name: writer.register_channel(
                name, "cdr", schemas[kind], metadata={"offered_qos_profiles": QOS}
            )
            for name, kind, _ in TOPICS
        }
        for sequence, (stamp, topic, data) in enumerate(part):
            writer.add_message(channels[topic], stamp, data, stamp, sequence)
        writer.finish()


def build() -> None:
    every = messages()
    bags = {
        "mobile_base_sqlite3": ("sqlite3", [("mobile_base_sqlite3_0.db3", every)]),
        "mobile_base_mcap": ("mcap", [("mobile_base_mcap_0.mcap", every)]),
        "split_sqlite3": (
            "sqlite3",
            [("split_sqlite3_0.db3", every[:9]), ("split_sqlite3_1.db3", every[9:])],
        ),
    }
    for directory, (storage, parts) in bags.items():
        folder = HERE / directory
        folder.mkdir(exist_ok=True)
        (folder / "metadata.yaml").write_text(metadata(storage, parts))
        for name, part in parts:
            (write_sqlite if storage == "sqlite3" else write_mcap)(folder / name, part)


def oracle() -> str:
    """What the official readers read: rosbags for every bag, mcap for the MCAP files."""
    from mcap.reader import make_reader
    from rosbags.rosbag2 import Reader

    out: dict[str, object] = {}
    for folder in sorted(p for p in HERE.iterdir() if p.is_dir()):
        with Reader(folder) as reader:
            out[folder.name] = {
                "start": reader.start_time,
                "duration": reader.duration,
                "message_count": reader.message_count,
                "connections": [
                    {
                        "topic": c.topic,
                        "msgtype": c.msgtype,
                        "count": c.msgcount,
                        "serialization_format": c.ext.serialization_format,
                        "qos_profiles": len(c.ext.offered_qos_profiles),
                        "qos_reliability": c.ext.offered_qos_profiles[0].reliability.name,
                    }
                    for c in sorted(reader.connections, key=lambda c: c.topic)
                ],
                "messages": [
                    [c.topic, t, len(raw)]
                    for c, t, raw in sorted(reader.messages(), key=lambda m: (m[1], m[0].topic))
                ],
            }
    mcap_check = {}
    for path in sorted(HERE.glob("*/*.mcap")):
        with path.open("rb") as stream:
            reader = make_reader(stream)
            summary = reader.get_summary()
            assert summary is not None and summary.statistics is not None
            mcap_check[path.name] = {
                "messages": summary.statistics.message_count,
                "channels": sorted(c.topic for c in summary.channels.values()),
            }
    out["_mcap"] = mcap_check
    return json.dumps(out, indent=1, sort_keys=True) + "\n"


if __name__ == "__main__":
    if "--oracle" in sys.argv:
        (HERE / "oracle.json").write_text(oracle())
    else:
        build()

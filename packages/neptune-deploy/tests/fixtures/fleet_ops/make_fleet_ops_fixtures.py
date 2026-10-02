"""Regenerates the binary and compiler-written fleet-ops fixtures (Deploy ADR 0010, §9).

Everything else under this folder is hand-written JSON. This script writes the rest:

- ``rmf/rmf_logs.db``: an Open-RMF api-server style SQLite database (task and fleet states stored as
  JSON text in ``data``), holding the same tasks as ``rmf/tasks.json`` for a second warehouse shift.
- ``diagnostics/arm_cell_bag/``: a rosbag2 (sqlite3) of a manipulator cell with a ``/diagnostics``
  topic. The payload bytes are placeholders: nothing under test decodes them, because the compiler
  does not decode messages into packages yet (ADR 0010 §7). The bag's metadata is what the
  compiler reads.
- ``diagnostics/packages/legged_patrol`` and ``.../arm_cell_bag``: the compiler's own package for
  each, written by ``neptune ingest``, without ``volatile/``. Deploy's tests read them and never
  run ingestion, so a compiler change reaches them only through a deliberate regeneration PR.

Run from the repository root::

    uv run python packages/neptune-deploy/tests/fixtures/fleet_ops/make_fleet_ops_fixtures.py
"""

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
QOS = "- history: 3\n  depth: 0\n  reliability: 1\n  durability: 2\n"
START = 1_772_366_400_000_000_000  # an arm cell's morning shift, nanoseconds
STEP = 1_000_000_000
CDR = b"\x00\x01\x00\x00"
TOPICS = (
    (
        "/diagnostics",
        "diagnostic_msgs/msg/DiagnosticArray",
        "std_msgs/Header header\nDiagnosticStatus[] status\n",
    ),
    ("/joint_states", "sensor_msgs/msg/JointState", "string[] name\nfloat64[] position\n"),
)


def write_rmf_database() -> None:
    path = HERE / "rmf" / "rmf_logs.db"
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE task_state(id_ TEXT PRIMARY KEY, data TEXT NOT NULL,
            unix_millis_start_time INTEGER, unix_millis_finish_time INTEGER);
        CREATE TABLE fleet_state(name TEXT PRIMARY KEY, data TEXT NOT NULL);
        """
    )
    tasks = [
        ("delivery.dispatch-21", "Delivery", "AMR-12", 1_772_366_400_000, 1_772_367_300_000),
        ("loop.dispatch-22", "Loop", "AMR-12", 1_772_367_600_000, None),
    ]
    for task_id, category, robot, start, finish in tasks:
        data = {
            "booking": {"id": task_id, "priority": 0},
            "category": category,
            "assigned_to": {"group": "tinyRobot", "name": robot},
            "status": "completed" if finish else "underway",
        }
        db.execute(
            "INSERT INTO task_state VALUES (?, ?, ?, ?)",
            (task_id, json.dumps(data, sort_keys=True), start, finish),
        )
    fleet = {"name": "tinyRobot", "robots": {"AMR-12": {"status": "working", "battery": 0.77}}}
    db.execute(
        "INSERT INTO fleet_state VALUES ('tinyRobot', ?)", (json.dumps(fleet, sort_keys=True),)
    )
    db.commit()
    db.execute("VACUUM")
    db.close()


def write_bag() -> Path:
    folder = HERE / "diagnostics" / "arm_cell_bag"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    messages = []
    for i in range(6):
        messages.append((START + i * STEP, "/diagnostics", CDR + bytes(16 + i)))
        messages.append((START + i * STEP + 500_000_000, "/joint_states", CDR + bytes(24)))
    messages.sort()
    db = sqlite3.connect(folder / "arm_cell_bag_0.db3")
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
        [(ids[topic], stamp, data) for stamp, topic, data in messages],
    )
    db.commit()
    db.execute("VACUUM")
    db.close()
    q = json.dumps(QOS)
    lines = [
        "rosbag2_bagfile_information:",
        "  version: 5",
        "  storage_identifier: sqlite3",
        "  relative_file_paths:",
        "    - arm_cell_bag_0.db3",
        "  duration:",
        f"    nanoseconds: {messages[-1][0] - messages[0][0]}",
        "  starting_time:",
        f"    nanoseconds_since_epoch: {messages[0][0]}",
        f"  message_count: {len(messages)}",
        "  topics_with_message_count:",
    ]
    for name, kind, _ in TOPICS:
        lines += [
            "    - topic_metadata:",
            f"        name: {name}",
            f"        type: {kind}",
            "        serialization_format: cdr",
            f"        offered_qos_profiles: {q}",
            f"      message_count: {sum(1 for m in messages if m[1] == name)}",
        ]
    lines += ['  compression_format: ""', '  compression_mode: ""', "  files:"]
    lines += [
        "    - path: arm_cell_bag_0.db3",
        "      starting_time:",
        f"        nanoseconds_since_epoch: {messages[0][0]}",
        "      duration:",
        f"        nanoseconds: {messages[-1][0] - messages[0][0]}",
        f"      message_count: {len(messages)}",
    ]
    (folder / "metadata.yaml").write_text("\n".join(lines) + "\n")
    return folder


def ingest(source: Path, out: Path) -> None:
    command = Path(sys.executable).parent / "neptune"
    shutil.rmtree(out, ignore_errors=True)
    out.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as workspace:
        subprocess.run(
            [str(command), "ingest", str(source), "--out", str(out), "-w", workspace], check=True
        )
    shutil.rmtree(out / "volatile", ignore_errors=True)
    sys.stdout.write(f"wrote {out.relative_to(HERE)}\n")


def main() -> int:
    write_rmf_database()
    bag = write_bag()
    packages = HERE / "diagnostics" / "packages"
    ingest(HERE / "diagnostics" / "legged_patrol", packages / "legged_patrol")
    ingest(bag, packages / "arm_cell_bag")
    return 0


if __name__ == "__main__":
    sys.exit(main())

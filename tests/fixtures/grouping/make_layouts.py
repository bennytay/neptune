"""Messy folder layouts for run/session grouping (MVL-13, ADR 0036), generated at test time.

Grouping reads names and directories only, so every file is a few bytes: its own path, so that
no two are equal except the duplicates a layout asks for. ``build(root, name)`` writes one layout
below ``root``; ``build_all(root)`` writes every layout side by side, the messy tree the end-to-end
test ingests. Symlinks are made here because git does not carry them portably, and the
non-UTF-8 name only where the filesystem takes one. Deterministic: the same call writes the same
tree. Run as a script to look at one: ``python make_layouts.py OUT``.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final


@dataclass(frozen=True)
class File:
    path: bytes
    content: bytes | None = None  # None: the path itself, so every file differs

    def data(self) -> bytes:
        return self.content if self.content is not None else self.path + b"\n"


@dataclass(frozen=True)
class Link:
    path: bytes
    target: bytes


Entry = File | Link
SAME: Final = b"one flight log, copied twice\n"

LAYOUTS: Final[dict[str, tuple[Entry, ...]]] = {
    # Two rosbag2 directories (one split across two .db3 files), a note beside them.
    "ros2_bags": (
        File(b"ros2_bags/rosbag2_2024_05_01-12_30_00/metadata.yaml"),
        File(b"ros2_bags/rosbag2_2024_05_01-12_30_00/rosbag2_2024_05_01-12_30_00_0.db3"),
        File(b"ros2_bags/rosbag2_2024_05_01-12_30_00/rosbag2_2024_05_01-12_30_00_1.db3"),
        File(b"ros2_bags/rosbag2_2024_05_01-14_00_00/metadata.yaml"),
        File(b"ros2_bags/rosbag2_2024_05_01-14_00_00/rosbag2_2024_05_01-14_00_00_0.mcap"),
        File(b"ros2_bags/notes.txt"),
    ),
    # PX4's log/<date>/<time>.ulg: a date folder is a day, never a session; each log is a flight.
    "px4": (
        File(b"px4/log/2024-05-01/12_30_00.ulg"),
        File(b"px4/log/2024-05-01/13_45_10.ulg"),
        File(b"px4/log/2024-05-02/09_00_00.ulg"),
        File(b"px4/log/desktop.ini"),
    ),
    # Session-named directories, a README above them, a stray inside one.
    "runs": (
        File(b"runs/README.md"),
        File(b"runs/run_001/robot.mcap"),
        File(b"runs/run_001/config.yaml"),
        File(b"runs/run_001/camera/front.mp4"),
        File(b"runs/run_002/robot.mcap"),
        File(b"runs/run_002/config.yaml"),
        File(b"runs/run_002/Thumbs.db"),
    ),
    # rosbag1 --split parts sharing a start time (part 2 missing), and another recording.
    "split": (
        File(b"split/patrol_2024-05-01-12-30-00_0.bag"),
        File(b"split/patrol_2024-05-01-12-30-00_1.bag"),
        File(b"split/patrol_2024-05-01-12-30-00_3.bag"),
        File(b"split/patrol_2024-05-01-15-00-00_0.bag"),
    ),
    # Numbered parts with no start time: one recording split, or two? Contested.
    "parts": (
        File(b"parts/x_0.mcap"),
        File(b"parts/x_1.mcap"),
        File(b"parts/x.yaml"),
    ),
    # Numbered by a session keyword: two episodes, never one.
    "episodes": (
        File(b"episodes/episode_1.mcap"),
        File(b"episodes/episode_2.mcap"),
    ),
    # rosbag1 ``-O run_3 --split``: the keyword names the recording, not each part. Contested.
    "named_split": (
        File(b"named_split/run_3_0.bag"),
        File(b"named_split/run_3_1.bag"),
    ),
    # A flat dump of media named by start time: two sessions, no recording among them.
    "dump": (
        File(b"dump/2024-05-01_12-30-00_front.mp4"),
        File(b"dump/2024-05-01_12-30-00_imu.csv"),
        File(b"dump/2024-05-01_14-02-11_front.mp4"),
        File(b"dump/2024-05-01_14-02-11_imu.csv"),
    ),
    # One session directory, two recordings two days apart: contested.
    "session_dir": (
        File(b"session_04/2024-05-01_10-00-00.mcap"),
        File(b"session_04/2024-05-03_09-00-00.mcap"),
    ),
    # The same bytes in two runs: two sessions, each saying so.
    "copies": (
        File(b"copies/run_1/flight.ulg", SAME),
        File(b"copies/run_2/flight.ulg", SAME),
    ),
    # Two trials 25 s apart: one session started in steps, or two? Contested.
    "trials": (
        File(b"trials/trial_2024-05-01_12-30-00.mcap"),
        File(b"trials/trial_2024-05-01_12-30-25.mcap"),
    ),
    # A campaign directory holding two runs is a collection, not a session.
    "campaign": (
        File(b"campaign_2024-05-01_08-00-00/plan.pdf"),
        File(b"campaign_2024-05-01_08-00-00/run_1/a.mcap"),
        File(b"campaign_2024-05-01_08-00-00/run_2/b.mcap"),
    ),
    # A session directory holding one timestamped directory and a recording of its own.
    "drive": (
        File(b"drive_07/robot.mcap"),
        File(b"drive_07/camera_2024-05-01_12-30-00/frame_0001.png"),
        File(b"drive_07/camera_2024-05-01_12-30-00/frame_0002.png"),
    ),
    # Two recordings with no time and no session directory: two sessions, never one.
    "flat": (
        File(b"flat/front.mcap"),
        File(b"flat/front.yaml"),
        File(b"flat/rear.mcap"),
        File(b"flat/robot.yaml"),
    ),
    # Unicode names, kept byte for byte.
    "unicode": (
        File("séance_2024-05-01T12-00-00/données.mcap".encode()),
        File("séance_2024-05-01T12-00-00/notes_ü.txt".encode()),
    ),
    # Links: an alias to a run, one inside a run, one leaving the root; and a stray at the top.
    "links": (
        File(b"shared/calib.yaml"),
        File(b".DS_Store"),
        Link(b"latest", b"runs/run_002"),
        Link(b"runs/run_001/calib.yaml", b"../../shared/calib.yaml"),
        Link(b"escape", b"/etc/passwd"),
    ),
}

# A directory whose name is not UTF-8, holding a run; only on filesystems that take one.
RAW: Final = (File(b"raw/\xffrun_3\xfe/log.ulg"),)


def build(root: Path, name: str) -> None:
    """Write layout ``name`` below ``root`` (``RAW`` as ``"raw"``, where the filesystem allows)."""
    entries = RAW if name == "raw" else LAYOUTS[name]
    for entry in entries:
        # fsdecode keeps bytes that are not UTF-8 as surrogate escapes, which Path writes back.
        path = root / os.fsdecode(entry.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(entry, Link):
            path.symlink_to(os.fsdecode(entry.target))
        else:
            path.write_bytes(entry.data())


def raw_names_supported(root: Path) -> bool:
    """Whether the filesystem under ``root`` stores a name that is not UTF-8."""
    probe = root / os.fsdecode(b"\xff-probe")
    try:
        probe.mkdir()
    except OSError:
        return False
    probe.rmdir()
    return True


def build_all(root: Path) -> None:
    """Every layout side by side under ``root``; the raw-named one where possible."""
    for name in LAYOUTS:
        build(root, name)
    if raw_names_supported(root):
        build(root, "raw")


if __name__ == "__main__":
    target = Path(sys.argv[1])
    target.mkdir(parents=True, exist_ok=False)
    build_all(target)

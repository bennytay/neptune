"""The ROS 1 bag fixtures are what their generator writes, and the official reader agrees."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "rosbag1"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MAKE: Final = _load("make_rosbag1")
ORACLE: Final = json.loads((FIXTURES / "oracle.json").read_text())
FILES: Final = sorted(path.name for path in FIXTURES.iterdir() if path.suffix in (".bag", ".mcap"))


def test_the_committed_fixtures_are_what_the_generator_writes() -> None:
    on_disk = {name: (FIXTURES / name).read_bytes() for name in FILES}
    # On failure: `uv run python tests/fixtures/rosbag1/make_rosbag1.py`, then `... --oracle`. A
    # lz4 or bz2 upgrade may change the compressed bytes, never what they decompress to.
    assert on_disk == MAKE.build()
    assert all(len(data) < 512 * 1024 for data in on_disk.values())


def test_the_oracle_read_every_bag_and_says_how_each_damaged_one_fails() -> None:
    assert set(ORACLE) == {name for name in FILES if name.endswith(".bag")}
    valid = {"robot_none.bag", "robot_bz2.bag", "robot_lz4.bag", "empty.bag"}
    valid |= {"lying_chunk_info.bag", "connection_collision.bag"}  # it trusts the index
    for name in valid:
        assert ORACLE[name]["error"] is None, name
    for name in ("unclosed.bag", "truncated.bag", "unknown_compression.bag"):
        assert ORACLE[name]["error"] is not None, name  # the official reader refuses these
    robot = ORACLE["robot_bz2.bag"]
    assert len(robot["messages"]) == len(MAKE.MESSAGES) == 17
    assert [c["topic"] for c in robot["connections"]] == [c.topic for c in MAKE.CONNECTIONS]
    assert [c["md5sum"] for c in robot["connections"]] == [c.md5sum for c in MAKE.CONNECTIONS]

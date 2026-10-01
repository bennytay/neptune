"""The MCAP fixtures are what their generator writes, and the official reader agrees about them."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "mcap"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MAKE: Final = _load("make_mcap")
ORACLE: Final = json.loads((FIXTURES / "oracle.json").read_text())


def test_the_committed_fixtures_are_what_the_generator_writes() -> None:
    on_disk = {path.name: path.read_bytes() for path in FIXTURES.glob("*.mcap")}
    # On failure: `uv run python tests/fixtures/mcap/make_mcap.py`, then `... --oracle`. A
    # zstandard or lz4 upgrade may change the compressed bytes, never what they decompress to.
    assert on_disk == MAKE.build()
    assert all(len(data) < 512 * 1024 for data in on_disk.values())


def test_the_oracle_read_every_fixture_and_says_how_each_damaged_one_fails() -> None:
    assert set(ORACLE) == {path.name for path in FIXTURES.glob("*.mcap")}
    streamed = {name: entry["streamed"]["error"] for name, entry in ORACLE.items()}
    assert streamed["bad_crc.mcap"] == "CRCValidationError"
    assert streamed["truncated.mcap"] == "EndOfFile"
    valid = {"robot.mcap", "robot_lz4.mcap", "robot_plain.mcap", "unchunked.mcap", "empty.mcap"}
    valid |= {"no_summary.mcap", "no_message_index.mcap", "unknown_encoding.mcap"}
    for name in valid:
        assert ORACLE[name]["streamed"]["error"] is None, name
        assert ORACLE[name]["indexed"]["error"] is None, name
    # The official reader trusts an index that lies: it reads one message twice.
    assert len(ORACLE["overlapping_index.mcap"]["indexed"]["messages"]) == 19
    robot = ORACLE["robot.mcap"]["indexed"]["statistics"]
    assert robot["message_count"] == 18 == len(MAKE.MESSAGES)

"""MVL-21's acceptance: downstream code asks what a run contains without decoding a message.

A job over the MCAP and rosbag2 fixtures (an IMU and a JSON Schema battery on a robot; a mobile
base's velocity command, battery voltage and status, in sqlite3 and in MCAP storage), plus an MCAP
whose schemas are hostile, is read back through the SDK only.
"""

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from neptune.derived.schemas import LayoutState, PathKind
from neptune.derived.semantics import KNOWN_TYPE, KNOWN_TYPE_UNCHECKED, Semantic, SemanticState
from neptune.model.knowledge import Known
from neptune.sdk import Neptune, RunContents, StreamContents, Workspace, run_contents
from neptune.store.package import read_package

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MCAP: Final = _load("make_mcap_introspection", FIXTURES / "mcap" / "make_mcap.py")
SEP: Final = "=" * 80
HOSTILE: Final = (
    ("pkg/msg/Broken", "ros2msg", b"float64\nnot a field line at all\n"),
    ("pkg/msg/Tree", "ros2msg", f"Node root\n{SEP}\nMSG: pkg/Node\nNode[] children\n".encode()),
    ("fixture.Deep", "jsonschema", b'{"properties":' * 200 + b"{}" + b"}" * 200),
    ("sensor_msgs/msg/Imu", "ros2msg", b"float32 temperature\n"),  # the name, not the shape
    ("foxglove.PoseInFrame", "protobuf", b"\x0a\x0bPoseInFrame"),
    ("legged/msg/FootForces", "ros2msg", b"string[] name\nfloat64[] position\n"),
)


def hostile_mcap() -> bytes:
    schemas = tuple(
        MCAP.Schema(index, name, encoding, data)
        for index, (name, encoding, data) in enumerate(HOSTILE, 1)
    )
    channels = tuple(
        MCAP.Channel(index, index, f"/hostile_{index}", "cdr") for index in range(1, 7)
    )
    data, _ = MCAP.write(MCAP.Options(schemas=schemas, channels=channels, messages=(), chunks=()))
    return bytes(data)


def corpus(root: Path) -> Path:
    root.mkdir()
    shutil.copyfile(FIXTURES / "mcap" / "robot.mcap", root / "robot.mcap")
    shutil.copyfile(FIXTURES / "mcap" / "unknown_encoding.mcap", root / "unknown_encoding.mcap")
    for bag in ("mobile_base_sqlite3", "mobile_base_mcap"):
        shutil.copytree(FIXTURES / "rosbag2" / bag, root / bag)
    (root / "hostile.mcap").write_bytes(hostile_mcap())
    return root


@pytest.fixture(scope="module")
def ingested(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, tuple[RunContents, ...]]:
    work = tmp_path_factory.mktemp("introspection")
    root = corpus(work / "root")
    result = Neptune(workspace=Workspace(work / "home")).ingest(root, work / "package")
    assert result.committed
    return work, result.contents()


def streams(runs: tuple[RunContents, ...]) -> list[StreamContents]:
    return [stream for run in runs for stream in run.streams]


def by_topic(runs: tuple[RunContents, ...], topic: str) -> list[StreamContents]:
    return [stream for stream in streams(runs) if stream.topic == topic]


def test_every_stream_has_a_layout_and_a_semantic(
    ingested: tuple[Path, tuple[RunContents, ...]],
) -> None:
    _, runs = ingested
    assert streams(runs)
    for stream in streams(runs):
        assert stream.layout is not None and stream.semantic is not None, stream.topic
        # The layout cites the very definition the stream declares; it never re-reads messages.
        definition = stream.stream.schema_definition
        if isinstance(definition, Known):
            assert stream.layout.definition == definition.value


def test_a_run_answers_which_streams_carry_what(
    ingested: tuple[Path, tuple[RunContents, ...]],
) -> None:
    _, runs = ingested
    robot = next(run for run in runs if run.topic("/imu"))
    semantics = robot.semantics()
    assert [s.topic for s in semantics[Semantic.IMU]] == ["/imu", "/imu_rear"]
    assert [s.topic for s in semantics[Semantic.BATTERY]] == ["/battery"]
    (imu, _) = robot.carrying("imu")
    paths = [field.path for field in imu.fields]
    assert paths[:3] == ["header.stamp.sec", "header.stamp.nanosec", "header.frame_id"]
    assert "angular_velocity.z" in paths and "linear_acceleration.x" in paths
    assert imu.semantic is not None
    best = imu.semantic.candidates[0]
    assert best.rules[0].rule == KNOWN_TYPE and best.confidence == 0.9
    assert {(u.field, u.unit) for u in best.units} >= {("angular_velocity", "rad/s")}
    (battery,) = robot.topic("/battery")
    assert battery.schema_encoding == "jsonschema"
    assert [(f.path, f.type) for f in battery.fields] == [  # in the schema's own key order
        ("percentage", "number"),
        ("voltage", "number"),
    ]
    assert battery.semantic is not None and battery.semantic.candidates[0].rules[0].rule == (
        "shape_battery"
    )
    (diagnostics,) = robot.topic("/diagnostics")
    assert diagnostics.layout_state is LayoutState.KNOWN_ABSENT
    assert diagnostics.semantic_state is SemanticState.UNKNOWN


def test_both_rosbag2_storages_read_the_same(
    ingested: tuple[Path, tuple[RunContents, ...]],
) -> None:
    _, runs = ingested
    command = by_topic(runs, "/cmd_vel")
    assert len(command) == 2  # sqlite3 and MCAP storage
    for stream in command:
        assert stream.carries(Semantic.TWIST)
        assert [f.path for f in stream.fields] == [
            "linear.x",
            "linear.y",
            "linear.z",
            "angular.x",
            "angular.y",
            "angular.z",
        ]
    for stream in by_topic(runs, "/battery_voltage"):
        # A bare float on a battery-named topic: topics are never read, so nothing is claimed.
        assert stream.schema_name == "std_msgs/msg/Float32"
        assert stream.semantic_state is SemanticState.UNKNOWN
        assert not stream.may_carry(Semantic.BATTERY)


def test_hostile_and_uncovered_definitions_are_findings_not_failures(
    ingested: tuple[Path, tuple[RunContents, ...]],
) -> None:
    work, runs = ingested
    hostile = {s.topic: s for s in streams(runs) if (s.topic or "").startswith("/hostile_")}
    broken, tree, deep, imu, foxglove, legged = (hostile[f"/hostile_{i}"] for i in range(1, 7))
    assert broken.layout is not None and broken.layout.problem is not None
    assert (broken.layout_state, broken.layout.problem.reason) == (LayoutState.UNKNOWN, "malformed")
    assert [(f.path, f.kind) for f in tree.fields] == [("root.children[]", PathKind.RECURSIVE)]
    assert deep.layout is not None and deep.layout.problem is not None
    assert deep.layout.problem.reason == "nesting_limit"
    assert imu.semantic_state is SemanticState.UNKNOWN  # its definition contradicts its name
    assert foxglove.layout_state is LayoutState.NOT_COVERED
    assert foxglove.semantic is not None and foxglove.carries(Semantic.POSE)
    assert foxglove.semantic.candidates[0].rules[0].rule == KNOWN_TYPE_UNCHECKED
    assert legged.carries(Semantic.JOINT_STATE)
    (pose,) = by_topic(runs, "/pose")  # unknown_encoding.mcap's own IDL
    assert pose.layout_state is LayoutState.NOT_COVERED
    codes = {f.code for f in read_package(work / "package").receipt.findings}
    assert {
        "neptune.introspection.definition_malformed",
        "neptune.introspection.definition_limit",
        "neptune.introspection.type_contradicts_layout",
        "neptune.introspection.encoding_not_covered",
    } <= codes


def test_introspection_is_deterministic(
    ingested: tuple[Path, tuple[RunContents, ...]], tmp_path: Path
) -> None:
    work, runs = ingested
    again = tmp_path / "again"
    result = Neptune(workspace=Workspace(tmp_path / "home")).ingest(
        corpus(tmp_path / "root"), again
    )
    first, second = read_package(work / "package"), read_package(again)
    assert first.derived == second.derived
    assert first.id == second.id
    assert run_contents(second) == runs == result.contents()
    lines = [json.dumps(line, sort_keys=True) for line in first.derived["stream_semantic"]]
    assert lines == sorted(lines, key=lambda line: json.loads(line)["id"])

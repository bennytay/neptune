"""The status fixtures (ADR 0071): the committed files are what their generator writes, the records
the adapters write agree with what the official readers read (``oracle.json``), and every record's
evidence resolves to the exact bytes of its message, or of its status inside the message."""

import importlib.util
import json
import struct
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest
import zstandard

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.discovery.reader import BytesReader
from neptune.model.knowledge import INHERITED, Known
from neptune.model.provenance import ByteRange, Provenance
from neptune.model.status import SafetyState, StatusReport

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "status"
ORACLE: Final = json.loads((FIXTURES / "oracle.json").read_text())
ADAPTERS: Final = {adapter.descriptor.id: adapter for adapter in builtin_adapters()}
READERS: Final = {
    "arm_cell.mcap": "mcap",
    "mobile_base.bag": "rosbag1",
    "av_shuttle.db3": "rosbag2",
    "quad_killswitch.ulg": "flightlog",
    "boat_failsafe.bin": "flightlog",
}
ROS: Final = ("arm_cell.mcap", "mobile_base.bag", "av_shuttle.db3")


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "make_status_fixtures_check", FIXTURES / "make_status_fixtures.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GENERATOR: Final = _generator()
BUILT: Final = GENERATOR.build()


def ingest(name: str, nominal: bool = False, **config: Any) -> SourceOutput:
    values = {"nominal_status_records": nominal, **config}
    return ingest_source(ADAPTERS[READERS[name]], BytesReader(BUILT[name]), values)


def statuses(output: SourceOutput) -> list[StatusReport]:
    return [r for r in output.records() if isinstance(r, StatusReport)]


def safeties(output: SourceOutput) -> list[SafetyState]:
    return [r for r in output.records() if isinstance(r, SafetyState)]


def topics(output: SourceOutput) -> dict[str, str]:
    return {r.id: r.topic.value for r in output.records() if r.kind == "stream"}


def clock0(record: StatusReport | SafetyState) -> int | None:
    time = record.times[0]
    return time.value.ticks if isinstance(time, Known) else None


def value(state: Any) -> Any:
    return state.value if isinstance(state, Known) else None


@pytest.mark.parametrize("name", sorted(BUILT))
def test_the_committed_file_is_what_the_generator_writes(name: str) -> None:
    assert (FIXTURES / name).read_bytes() == BUILT[name]


def test_every_fixture_is_small_and_has_an_oracle() -> None:
    assert all(p.stat().st_size < 512 * 1024 for p in FIXTURES.iterdir())
    assert set(ORACLE) == set(BUILT)


# --- The official readers agree -----------------------------------------------------------------


def _ros_expected(name: str, nominal: bool) -> tuple[set[Any], set[Any]]:
    """What the oracle says the records are: statuses and safety states, keyed by topic and
    clock-0 time; at the OK level and at a normal value only when ``nominal``."""
    normal = {"e_stopped.val": 0, "in_error.val": 0, "mode": 1, "state": 1, "e_stop": False}
    found_statuses: set[Any] = set()
    found_safety: set[Any] = set()
    for topic, time, entry in ORACLE[name]:
        for level, label, message, hardware, pairs in entry.get("statuses", []):
            if nominal or level != 0:
                pairs_key = tuple(tuple(pair) for pair in pairs)
                found_statuses.add(
                    (topic, time, level, label, message, hardware or None, pairs_key)
                )
        for field, raw in entry.items():
            if field != "statuses" and (nominal or raw != normal[field]):
                found_safety.add((topic, time, field, raw))
    return found_statuses, found_safety


def _ros_found(output: SourceOutput) -> tuple[set[Any], set[Any]]:
    names = topics(output)
    found_statuses = {
        (
            names[r.stream],
            clock0(r),
            value(r.level),
            value(r.name),
            value(r.message),
            value(r.hardware_id),
            tuple((pair.key, pair.value) for pair in value(r.values)),
        )
        for r in statuses(output)
    }
    found_safety = {(names[r.stream], clock0(r), r.field, value(r.value)) for r in safeties(output)}
    return found_statuses, found_safety


@pytest.mark.parametrize("name", ROS)
@pytest.mark.parametrize("nominal", [False, True])
def test_ros_records_are_what_the_official_readers_decode(name: str, nominal: bool) -> None:
    output = ingest(name, nominal)
    assert _ros_found(output) == _ros_expected(name, nominal)
    counted = len(statuses(output)) + len(safeties(output))
    assert counted == sum(len(group) for group in _ros_expected(name, nominal))


def test_level_and_value_names_are_the_definitions_constants() -> None:
    arm = ingest("arm_cell.mcap", nominal=True)
    names = {value(r.level): value(r.level_names) for r in statuses(arm)}
    assert names == {0: ("OK",), 1: ("WARN",), 2: ("ERROR",), 3: ("STALE",)}
    gripper = [r for r in statuses(arm) if value(r.name) == "gripper"]
    assert {value(r.level_names) for r in gripper} == {("OK",), ("STALE",)}  # ros2idl constants
    by_field = {(r.field, value(r.value)): value(r.value_names) for r in safeties(arm)}
    assert by_field[("e_stopped.val", 1)] == ("CLOSED", "ENABLED", "HIGH", "ON", "TRUE")
    assert by_field[("e_stopped.val", 0)] == ("DISABLED", "FALSE", "LOW", "OFF", "OPEN")
    assert by_field[("mode", 3)] == ("PROTECTIVE_STOP",)
    av = ingest("av_shuttle.db3")
    assert {value(r.value_names) for r in safeties(av)} == {("MRM_OPERATING",), ("MRM_SUCCEEDED",)}
    husky = safeties(ingest("mobile_base.bag"))
    assert {r.value_names.state.value for r in husky} == {"not_applicable"}


def test_flight_logs_are_what_pyulog_and_pymavlink_read() -> None:
    quad = ingest("quad_killswitch.ulg", nominal=True)
    oracle = ORACLE["quad_killswitch.ulg"]
    logged = set()
    for r in statuses(quad):
        pairs = value(r.values)
        tag = pairs[0].value if pairs else None
        logged.add((clock0(r), value(r.level), value(r.message), tag))
    expected = {(t, level, text, None) for t, level, text in oracle["logged"]}
    expected |= {(t, level, text, tag) for t, level, tag, text in oracle["tagged"]}
    assert logged == expected
    kill = {(clock0(r), value(r.value)) for r in safeties(quad)}
    assert kill == {tuple(row) for row in oracle["actuator_armed"]}
    engaged = {(clock0(r), value(r.value)) for r in safeties(ingest("quad_killswitch.ulg"))}
    assert engaged == {tuple(row) for row in oracle["actuator_armed"] if row[1]}
    boat = statuses(ingest("boat_failsafe.bin"))
    rows = set()
    for r in boat:
        if r.convention.value == "ardupilot_message":
            rows.add(("MSG", clock0(r), value(r.message)))
        else:
            rows.add(("ERR", clock0(r), *(pair.value for pair in value(r.values))))
    assert rows == {tuple(row) for row in ORACLE["boat_failsafe.bin"]}


def test_ulog_levels_are_the_specifications_names() -> None:
    names = {value(r.level): value(r.level_names) for r in statuses(ingest("quad_killswitch.ulg"))}
    assert names == {ord("6"): ("INFO",), ord("2"): ("CRIT",), ord("4"): ("WARNING",)}


# --- Evidence resolves to the exact bytes -------------------------------------------------------


def _mcap_chunk(record: bytes) -> bytes:
    """An MCAP Chunk record's records, decompressed (the specification's layout, read here)."""
    at = 1 + 8 + 8 + 8
    size = struct.unpack_from("<Q", record, at)[0]
    at += 8 + 4
    (name_length,) = struct.unpack_from("<I", record, at)
    compression = record[at + 4 : at + 4 + name_length].decode()
    at += 4 + name_length + 8
    stored = record[at:]
    if compression == "zstd":
        return zstandard.ZstdDecompressor().decompress(stored, max_output_size=size)
    assert compression == ""
    return stored


def _bag_chunk(record: bytes) -> bytes:
    """A ROS 1 Chunk record's data, stored uncompressed in this fixture."""
    (header_length,) = struct.unpack_from("<I", record, 0)
    assert b"compression=none" in record[4 : 4 + header_length]
    return record[4 + header_length + 4 :]


def resolve(name: str, locator: tuple[Any, ...]) -> bytes:
    """The bytes a locator cites: a step inside a chunk is inside its records, decompressed; a
    step inside anything else is a range of its bytes."""
    data: bytes = BUILT[name]
    for depth, step in enumerate(locator):
        assert isinstance(step, ByteRange)
        if depth == 1 and name == "arm_cell.mcap":
            data = _mcap_chunk(data)
        elif depth == 1 and name == "mobile_base.bag":
            data = _bag_chunk(data)
        assert step.offset + step.length <= len(data)
        data = data[step.offset : step.offset + step.length]
    return data


def _payloads(name: str) -> Mapping[tuple[str, int], bytes]:
    """Each message's payload, by topic and recorder time, as the generator wrote it."""
    if name == "arm_cell.mcap":
        return {(topic, at): data for at, topic, data in GENERATOR.arm_messages()}
    if name == "av_shuttle.db3":
        return {(topic, at): data for at, topic, data in GENERATOR.av_messages()}
    connections = {c.id: c.topic for c in GENERATOR.MOBILE_CONNECTIONS}
    return {(connections[m.conn], m.time): m.data for m in GENERATOR.mobile_messages()}


ROW_STEPS: Final = {"arm_cell.mcap": 2, "mobile_base.bag": 2, "av_shuttle.db3": 1}


@pytest.mark.parametrize("name", ROS)
def test_every_ros_record_resolves_to_its_message_and_its_exact_item(name: str) -> None:
    """The row's steps cite the whole message (its payload last); one more step cites the item's
    bytes inside it: a status of an array, or a safety field. The times cite the message."""
    output = ingest(name, nominal=True)
    names, payloads = topics(output), _payloads(name)
    found: list[StatusReport | SafetyState] = [*statuses(output), *safeties(output)]
    for record in found:
        locator = record.provenance.evidence.locator
        row = locator[: ROW_STEPS[name]]
        message = resolve(name, row)
        time = clock0(record)
        assert time is not None
        payload = payloads[(names[record.stream], time)]
        assert message.endswith(payload)
        first = record.times[0]
        assert isinstance(first, Known)
        if len(locator) == len(row):  # a lone DiagnosticStatus: the message is the status
            assert first.provenance is INHERITED  # the record's own: the message
            continue
        cited = first.provenance
        assert isinstance(cited, Provenance) and cited.evidence.locator == row
        item = resolve(name, locator)
        inner = locator[-1]
        assert isinstance(inner, ByteRange)
        assert inner.offset >= len(message) - len(payload)  # inside the payload
        assert message[inner.offset : inner.offset + inner.length] == item
        if isinstance(record, StatusReport):
            assert value(record.name).encode() in item
            assert value(record.message).encode() in item
            assert item[0] == value(record.level) & 0xFF  # a status starts with its level byte
        else:
            assert len(item) == 1 and item[0] == int(value(record.value)) & 0xFF


def test_flight_log_records_cite_their_whole_message() -> None:
    for name in ("quad_killswitch.ulg", "boat_failsafe.bin"):
        output = ingest(name, nominal=True)
        found: list[StatusReport | SafetyState] = [*statuses(output), *safeties(output)]
        assert found
        for record in found:
            (step,) = record.provenance.evidence.locator
            cited = resolve(name, (step,))
            if isinstance(record, StatusReport) and isinstance(record.message, Known):
                assert record.message.value.encode() in cited
            if name.endswith(".ulg"):
                assert cited[2:3] in (b"L", b"C", b"D")  # a ULog logging or data message
            else:
                assert cited[:2] == b"\xa3\x95"  # a DataFlash record


# --- Determinism ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(BUILT))
def test_output_is_byte_identical_run_to_run(name: str) -> None:
    first = [json.dumps(r.to_json(), sort_keys=True) for r in ingest(name).package_records()]
    second = [json.dumps(r.to_json(), sort_keys=True) for r in ingest(name).package_records()]
    assert first == second

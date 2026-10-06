"""Status and safety-state records from declared types (ADR 0071): definitions' constants,
recognition by shape, whole-value decoding against hostile payloads, the record writer's rules,
and the adapters' findings for malformed, truncated and huge inputs."""

import importlib.util
import json
import struct
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.contract import AdapterConfig, configure
from neptune.adapters.flightlog import FlightLogAdapter
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.mcap import McapAdapter
from neptune.adapters.rosbag1 import Rosbag1Adapter
from neptune.adapters.rosmsg.codec import DecodeLimits, Malformed, Struct, decode_value
from neptune.adapters.rosmsg.definitions import ConstantDef, parse_definition
from neptune.adapters.rosmsg.status import (
    SafetyType,
    Sample,
    StatusType,
    StatusWriter,
    Unrecognised,
    recognise,
)
from neptune.discovery.reader import BytesReader
from neptune.identity.hashing import content_id
from neptune.model.ids import RecordId
from neptune.model.knowledge import Known, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.status import SafetyState, StatusReport

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "status"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "make_status_fixtures_records", FIXTURES / "make_status_fixtures.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


G: Final = _generator()
ARRAY: Final = parse_definition(
    G.DIAGNOSTIC_ARRAY2.encode(), "ros2msg", "diagnostic_msgs/msg/DiagnosticArray"
)
LIMITS: Final = DecodeLimits()
STREAM: Final = RecordId("rec:sha256:" + "5" * 64)


# --- Constants -----------------------------------------------------------------------------------


def test_msg_constants_are_kept_on_their_type_as_integers_and_booleans() -> None:
    text = (
        "byte OK=0\nint8 UNKNOWN=-1\nuint8 NORMAL = 1\nint32 CODE=5 # a comment\n"
        "bool ENABLED=True\nstring LABEL=hello # kept verbatim, not a number\n"
        "float64 PI=3.14\nfloat64 TWO=2\nstring<=8 CODE=3\nint32 HEX=0x10\nuint8 level\n"
    )
    root = parse_definition(text.encode(), "ros2msg", "pkg/msg/Thing").root_type
    assert root.constants == (
        ConstantDef("OK", 0),
        ConstantDef("UNKNOWN", -1),
        ConstantDef("NORMAL", 1),
        ConstantDef("CODE", 5),
        ConstantDef("ENABLED", True),
    )
    assert [f.name for f in root.fields] == ["level"]


def test_idl_constants_are_kept_on_the_type_their_module_names() -> None:
    gripper = parse_definition(
        G.GRIPPER_STATUS_IDL.encode(), "ros2idl", "diagnostic_msgs/msg/DiagnosticStatus"
    )
    assert gripper.root_type.constants == (
        ConstantDef("OK", 0),
        ConstantDef("WARN", 1),
        ConstantDef("ERROR", 2),
        ConstantDef("STALE", 3),
    )
    assert gripper.types["diagnostic_msgs/KeyValue"].constants == ()


# --- Recognising by declared type and shape ------------------------------------------------------


def _status_definition(status_text: str, root: str = "DiagnosticArray") -> Any:
    text = G.joined(
        "std_msgs/Header header\nDiagnosticStatus[] status\n"
        if root == "DiagnosticArray"
        else status_text,
        ("diagnostic_msgs/DiagnosticStatus", status_text),
        G.KEY_VALUE,
        *G.HEADER2,
    )
    return parse_definition(text.encode(), "ros2msg", f"diagnostic_msgs/msg/{root}")


def test_diagnostics_are_recognised_with_their_level_names() -> None:
    kind = recognise(ARRAY)
    assert isinstance(kind, StatusType) and kind.array and kind.ok == 0
    assert kind.names == {0: ("OK",), 1: ("WARN",), 2: ("ERROR",), 3: ("STALE",)}
    bare = _status_definition(
        "byte level\nstring name\nstring message\nstring hardware_id\nKeyValue[] values\n"
    )
    found = recognise(bare)
    assert isinstance(found, StatusType) and found.names is None and found.ok is None


@pytest.mark.parametrize(
    "status_text",
    [
        "byte level\nstring name\nstring message\nKeyValue[] values\n",  # no hardware_id
        "byte level\nstring name\nstring message\nstring hardware_id\nKeyValue[3] values\n",
        "float32 level\nstring name\nstring message\nstring hardware_id\nKeyValue[] values\n",
        "byte level\nstring name\nstring message\nstring hardware_id\nKeyValue[] values\n"
        "string extra\n",
    ],
)
def test_a_status_type_without_its_shape_is_unrecognised(status_text: str) -> None:
    found = recognise(_status_definition(status_text))
    assert isinstance(found, Unrecognised)
    assert found.type == "diagnostic_msgs/DiagnosticArray"


def test_a_safety_type_needs_its_field_as_declared() -> None:
    robot = parse_definition(G.ROBOT_STATUS.encode(), "ros2msg", "industrial_msgs/msg/RobotStatus")
    found = recognise(robot)
    assert isinstance(found, SafetyType)
    assert [r.spec.path for r in found.fields] == [("e_stopped", "val"), ("in_error", "val")]
    assert all(r.normal == 0 for r in found.fields)
    flat = parse_definition(
        b"int8 e_stopped\nint8 in_error\n", "ros2msg", "industrial_msgs/RobotStatus"
    )
    assert isinstance(recognise(flat), Unrecognised)
    bool_mode = parse_definition(b"bool mode\n", "ros2msg", "ur_dashboard_msgs/msg/SafetyMode")
    assert isinstance(recognise(bool_mode), Unrecognised)
    plain = parse_definition(b"bool data\n", "ros2msg", "std_msgs/msg/Bool")
    assert recognise(plain) is None  # an /estop topic of std_msgs/Bool says nothing declared


def test_a_safety_type_without_its_normal_constant_records_every_value() -> None:
    mode = parse_definition(b"uint8 mode\n", "ros2msg", "ur_dashboard_msgs/msg/SafetyMode")
    found = recognise(mode)
    assert isinstance(found, SafetyType)
    assert found.fields[0].normal is None and found.fields[0].names is None


# --- Whole values against hostile payloads -------------------------------------------------------


def _array(*statuses: Any) -> bytes:
    return bytes(G.cdr_array(5 * 10**9, "", tuple(statuses)))


STATUS: Final = (2, "arm/joint_3", "following error", "J3", (("error_deg", "4.2"),))


def test_a_payload_is_read_whole_with_every_items_span() -> None:
    payload = _array((0, "a", "b", "", ()), STATUS)
    value = decode_value(ARRAY, payload, True, LIMITS)
    items = value.fields["status"]
    assert isinstance(items, tuple) and len(items) == 2
    first, second = items
    assert isinstance(first, Struct) and isinstance(second, Struct)
    assert first.end <= second.start and second.end <= len(payload)
    assert payload[second.start] == 2  # a status's bytes start at its level


@pytest.mark.parametrize(
    ("payload", "reason", "limit"),
    [
        (_array(STATUS)[:-6], "short", False),  # truncated
        (_array(STATUS) + b"\x00" * 8, "trailing_bytes", False),
        (b"\x01\x00" + _array(STATUS)[2:], "encapsulation", False),
        (_array()[:-4] + struct.pack("<I", 1000), "short", False),  # a count the bytes lie about
        (_array()[:-4] + struct.pack("<I", 2**31), "array_limit", True),  # past max_array_items
        (_array()[:-4] + struct.pack("<I", 70_000) + bytes(70_000 * 20), "array_limit", True),
        (_array((1, "x", "y", "z", ()))[:-8] + b"zz\x01\x00\x00\x00\x00\x00", None, False),
    ],
)
def test_hostile_payloads_are_malformed_never_a_crash(
    payload: bytes, reason: str | None, limit: bool
) -> None:
    with pytest.raises(Malformed) as raised:
        decode_value(ARRAY, payload, True, LIMITS)
    if reason is not None:
        assert raised.value.reason == reason and raised.value.limit is limit


def test_a_type_that_holds_itself_is_refused() -> None:
    looped = parse_definition(b"Node[] children\nint8 level\n", "ros2msg", "pkg/Node")
    payload = b"\x00\x01\x00\x00" + struct.pack("<I", 1) + struct.pack("<I", 0) + b"\x01\x02"
    with pytest.raises(Malformed, match="unsupported"):
        decode_value(looped, payload, True, LIMITS)


# --- The record writer ---------------------------------------------------------------------------


def _sample(payload: bytes) -> Sample:
    return Sample(content_id(payload), (ByteRange(0, len(payload)),), 0, (Unknown(),), ("place",))


def _config(**values: Any) -> AdapterConfig:
    from neptune.adapters.mcap import DESCRIPTOR

    return configure(DESCRIPTOR, values)


def _writer(**values: Any) -> StatusWriter:
    return StatusWriter(_config(**values))


def test_ok_statuses_are_rows_only_unless_the_config_asks() -> None:
    kind = recognise(ARRAY)
    assert isinstance(kind, StatusType)
    payload = _array((0, "fine", "ok", "", ()), STATUS)
    quiet, everything = _writer(), _writer(nominal_status_records=True)
    for writer in (quiet, everything):
        writer.add(kind, STREAM, None, _sample(payload), payload, True, LIMITS)
    assert [r.level for r in quiet.records] == [Known(2)]  # type: ignore[union-attr]
    assert [r.level for r in everything.records] == [Known(0), Known(2)]  # type: ignore[union-attr]
    assert all(isinstance(r.level_names, Unknown) for r in quiet.records)  # type: ignore[union-attr]


def test_text_that_is_not_utf8_is_unknown_in_its_field_only() -> None:
    kind = recognise(ARRAY)
    assert isinstance(kind, StatusType)
    cdr = G.Cdr()
    cdr.stamp(0)
    cdr.string("")
    cdr.put("I", 1)
    cdr.put("B", 1)
    cdr.string(b"\xff\xfe")
    cdr.string("hot")
    cdr.string("J3")
    cdr.put("I", 1)
    cdr.string("k")
    cdr.string(b"\xc3")
    payload = cdr.bytes()
    writer = _writer()
    writer.add(kind, STREAM, None, _sample(payload), payload, True, LIMITS)
    (record,) = writer.records
    assert isinstance(record, StatusReport)
    assert record.name == Unknown() and record.message == Known("hot")
    assert record.values == Unknown()


def test_a_call_writes_at_most_max_status_records_and_says_so() -> None:
    kind = recognise(ARRAY)
    assert isinstance(kind, StatusType)
    payload = _array(STATUS, STATUS, STATUS)
    writer = _writer(max_status_records=2)
    writer.add(kind, STREAM, None, _sample(payload), payload, True, LIMITS)
    assert len(writer.records) == 2
    ((stream, place, report),) = writer.reports()
    assert (stream, place) == (STREAM, ("place",))
    assert report.code == "status_not_recorded" and report.details["counts"] == {"record_limit": 1}
    assert report.category.value == "limit"


def _size(record: StatusReport | SafetyState) -> int:
    return len(json.dumps(record.to_json(), separators=(",", ":"), ensure_ascii=True))


def test_records_stop_at_the_byte_budget_and_later_messages_are_counted_unread() -> None:
    kind = recognise(ARRAY)
    assert isinstance(kind, StatusType)
    payload = _array(STATUS, STATUS, STATUS)
    probe = _writer()
    probe.add(kind, STREAM, None, _sample(payload), payload, True, LIMITS)
    sizes = [_size(record) for record in probe.records]
    budget = sizes[0] + sizes[1]  # exactly two records: the boundary is inclusive
    writer = StatusWriter(_config(), budget)
    writer.add(kind, STREAM, None, _sample(payload), payload, True, LIMITS)
    assert writer.records == probe.records[:2] and writer.bytes == budget
    corrupt = _array(STATUS)[:-5]  # past the bound a message is counted, never decoded
    writer.add(kind, STREAM, None, _sample(corrupt), corrupt, True, LIMITS)
    writer.add(kind, STREAM, None, _sample(payload), payload, True, LIMITS)
    assert len(writer.records) == 2
    ((_, place, report),) = writer.reports()
    assert place == ("place",) and report.category.value == "limit"
    assert report.details["counts"] == {"byte_limit": 1, "past_limit": 2}
    assert report.details["max_status_record_bytes"] == budget


def test_one_byte_under_the_budget_writes_one_record_fewer() -> None:
    kind = recognise(ARRAY)
    assert isinstance(kind, StatusType)
    payload = _array(STATUS, STATUS)
    probe = _writer()
    probe.add(kind, STREAM, None, _sample(payload), payload, True, LIMITS)
    budget = sum(_size(record) for record in probe.records) - 1
    writer = StatusWriter(_config(), budget)
    writer.add(kind, STREAM, None, _sample(payload), payload, True, LIMITS)
    assert writer.records == probe.records[:1]
    ((_, _, report),) = writer.reports()
    assert report.details["counts"] == {"byte_limit": 1}


def test_messages_past_max_status_records_are_counted_unread() -> None:
    kind = recognise(ARRAY)
    assert isinstance(kind, StatusType)
    payload = _array(STATUS, STATUS)
    writer = _writer(max_status_records=2)
    writer.add(kind, STREAM, None, _sample(payload), payload, True, LIMITS)
    corrupt = payload[:-5]
    writer.add(kind, STREAM, None, _sample(corrupt), corrupt, True, LIMITS)
    ((_, _, report),) = writer.reports()
    assert report.details["counts"] == {"past_limit": 1} and report.category.value == "limit"


def test_a_payload_that_does_not_read_is_counted_never_raised() -> None:
    kind = recognise(ARRAY)
    assert isinstance(kind, StatusType)
    payload = _array(STATUS)[:-5]
    writer = _writer()
    writer.add(kind, STREAM, None, _sample(payload), payload, True, LIMITS)
    assert writer.records == []
    ((_, _, report),) = writer.reports()
    assert report.details["counts"] == {"short": 1} and report.category.value == "corrupt"


def test_items_of_one_message_get_distinct_ids_from_their_spans() -> None:
    kind = recognise(ARRAY)
    assert isinstance(kind, StatusType)
    payload = _array(STATUS, STATUS)  # the same status twice: two statements, two records
    writer = _writer()
    sample = _sample(payload)
    writer.add(
        kind, STREAM, EvidenceRef(sample.source, (ByteRange(0, 4),)), sample, payload, True, LIMITS
    )
    assert len(writer.records) == 2
    assert writer.records[0].id != writer.records[1].id


# --- The adapters on malformed, truncated and huge inputs ----------------------------------------


def _mcap(messages: list[tuple[int, str, bytes]], topics: Any = None) -> bytes:
    topics = topics or G.ARM_TOPICS[:1]
    ids = {name: n for n, (name, *_) in enumerate(topics, 1)}
    schemas = tuple(G.MCAP.Schema(ids[n], k, e, t.encode()) for n, k, e, t in topics)
    channels = tuple(G.MCAP.Channel(ids[n], ids[n], n, "cdr") for n, *_ in topics)
    built = tuple(
        G.MCAP.Message(ids[topic], i, at, at, data) for i, (at, topic, data) in enumerate(messages)
    )
    data, _ = G.MCAP.write(
        G.MCAP.Options(
            compression="",
            schemas=schemas,
            channels=channels,
            messages=built,
            chunks=((0, len(built)),),
            attachment=False,
            metadata=False,
        )
    )
    return bytes(data)


def _codes(output: SourceOutput) -> list[str]:
    return sorted(f.code for f in output.findings())


def test_mcap_with_huge_and_truncated_status_arrays_gives_findings() -> None:
    good = _array(STATUS)
    huge = _array()[:-4] + struct.pack("<I", 2**31 - 1)
    data = _mcap(
        [(10, "/diagnostics", good), (20, "/diagnostics", huge), (30, "/diagnostics", good[:-7])]
    )
    output = ingest_source(McapAdapter(), BytesReader(data))
    records = [r for r in output.records() if isinstance(r, StatusReport)]
    assert len(records) == 1
    codes = _codes(output)
    assert "mcap.payload_undecodable" in codes and "mcap.status_not_recorded" in codes
    (finding,) = [f for f in output.findings() if f.code == "mcap.status_not_recorded"]
    assert finding.details["counts"] == {"array_limit": 1, "short": 1}


def test_an_mcap_cut_short_keeps_the_records_before_the_cut() -> None:
    whole = (FIXTURES / "arm_cell.mcap").read_bytes()

    def said(data: bytes) -> set[Any]:
        """What each record states, without the ids its source's content id is part of."""
        config = {"nominal_status_records": True}
        output = ingest_source(McapAdapter(), BytesReader(data), config)
        found = set()
        for record in output.records():
            if isinstance(record, StatusReport | SafetyState):
                first = record.times[0]
                ticks = first.value.ticks if isinstance(first, Known) else None
                level = record.level if isinstance(record, StatusReport) else record.value
                found.add((record.kind, ticks, str(level)))
        return found

    cut, complete = said(whole[:1700]), said(whole)  # inside the second of two chunks
    assert cut and cut < complete


def test_an_unrecognised_definition_is_a_finding_and_no_records() -> None:
    status = "byte level\nstring name\nstring message\nKeyValue[] values\n"  # no hardware_id
    text = G.joined(
        "std_msgs/Header header\nDiagnosticStatus[] status\n",
        ("diagnostic_msgs/DiagnosticStatus", status),
        G.KEY_VALUE,
        *G.HEADER2,
    )
    topics = (("/diagnostics", "diagnostic_msgs/msg/DiagnosticArray", "ros2msg", text),)
    cdr = G.Cdr()
    cdr.stamp(0)
    cdr.string("")
    cdr.put("I", 0)
    output = ingest_source(
        McapAdapter(), BytesReader(_mcap([(5, "/diagnostics", cdr.bytes())], topics))
    )
    assert "mcap.status_definition_unrecognised" in _codes(output)
    assert not [r for r in output.records() if isinstance(r, StatusReport)]


@pytest.mark.parametrize(
    ("adapter", "name"),
    [
        (McapAdapter(chunk_bytes=1024, max_rows=3), "arm_cell.mcap"),
        (Rosbag1Adapter(chunk_bytes=1024, max_rows=3), "mobile_base.bag"),
        (FlightLogAdapter(chunk_bytes=64, max_rows=2), "quad_killswitch.ulg"),
        (FlightLogAdapter(chunk_bytes=64, max_rows=2), "boat_failsafe.bin"),
    ],
)
def test_records_do_not_depend_on_how_the_plan_cuts_the_source(adapter: Any, name: str) -> None:
    data = (FIXTURES / name).read_bytes()
    small = ingest_source(adapter, BytesReader(data))
    default = ingest_source(type(adapter)(), BytesReader(data))
    assert len(small.plan.chunks) > len(default.plan.chunks)

    def kept(output: SourceOutput) -> list[Any]:
        return [
            r.to_json() for r in output.records() if r.kind in ("status_report", "safety_state")
        ]

    assert kept(small) == kept(default) and kept(default)


def test_a_ulog_actuator_armed_without_its_kill_switch_is_a_finding() -> None:
    u = G.ULOG
    broken = "actuator_armed:uint64_t timestamp;bool armed;bool lockdown;"
    data = b"".join(
        [
            u.header(),
            u.flag_bits(),
            u.fmt(broken),
            u.subscribe(0, 0, "actuator_armed"),
            u.data(0, struct.pack("<Q2?", u.BOOT + 10, True, True)),
            u.logged(3, u.BOOT + 20, "bad \xff".encode("latin1").decode("latin1")),
            u.msg("L", struct.pack("<BQ", ord("3"), u.BOOT + 30) + b"\xff\xfe"),
        ]
    )
    output = ingest_source(FlightLogAdapter(), BytesReader(data))
    assert "flightlog.status_definition_unrecognised" in _codes(output)
    assert not [r for r in output.records() if isinstance(r, SafetyState)]
    messages = [r.message for r in output.records() if isinstance(r, StatusReport)]
    assert Unknown() in messages  # the text that is not UTF-8


def test_a_dataflash_err_without_integer_codes_is_a_finding_and_an_untimed_msg_has_no_time() -> (
    None
):
    log = G.DF.Log()
    G.DF.header(log, {"ERR": ("QZZ", "TimeUS,Subsys,ECode"), "MSG": ("Z", "Message")}, units=False)
    log.add("ERR", 10, b"radio", b"lost")
    log.add("MSG", b"untimed text")
    output = ingest_source(FlightLogAdapter(), BytesReader(log.bytes()))
    assert "flightlog.status_definition_unrecognised" in _codes(output)
    (record,) = [r for r in output.records() if isinstance(r, StatusReport)]
    assert record.message == Known("untimed text") and record.times[0].state.value == "not_covered"

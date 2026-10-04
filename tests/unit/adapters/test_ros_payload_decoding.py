"""ROS payloads decoded by the stream adapters, against an independent reader (ADR 0068 §1).

``tests/fixtures/frames/oracle.json`` is what ``rosbags`` (fetched by ``uv`` for that run only,
never a project dependency) decodes from every ROS payload of the frame fixtures (``ros2msg`` and
``ros2idl`` MCAP, one big-endian message), the ROS 1 bag, the rosbag2 sqlite3 bag and the MCAP
fixture's ``/imu``, flattened to the decoder's column paths. Every decoded cell must equal it; a
payload the official reader refuses must have every value ``unknown`` and a finding.
"""

import importlib.util
import json
import struct
import time
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.contract import AdapterConfig, configure
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.mcap import McapAdapter
from neptune.adapters.rosbag1 import Rosbag1Adapter
from neptune.adapters.rosbag2 import Rosbag2Adapter
from neptune.adapters.rosmsg import streams as ros_streams
from neptune.adapters.rosmsg.codec import (
    MAX_LAYOUT_NODES,
    DecodeLimits,
    Decoder,
    Malformed,
    compile_layout,
)
from neptune.adapters.rosmsg.definitions import DefinitionError, parse_definition
from neptune.adapters.rosmsg.streams import (
    LIMIT_REASONS,
    LISTED_LEFT_OUT,
    Decoding,
    NotDecoded,
    decode_row,
    plan_stream,
)
from neptune.discovery.reader import BytesReader
from neptune.model.knowledge import Known
from neptune.model.run import Stream
from neptune.model.series import row_order

FIXTURES: Final = Path(__file__).parents[2] / "fixtures"
ORACLE: Final = json.loads((FIXTURES / "frames" / "oracle.json").read_text())
SOURCES: Final = {
    "arm_cell.mcap": (McapAdapter(), FIXTURES / "frames" / "arm_cell.mcap"),
    "quadruped_walk.mcap": (McapAdapter(), FIXTURES / "frames" / "quadruped_walk.mcap"),
    "usv_survey.mcap": (McapAdapter(), FIXTURES / "frames" / "usv_survey.mcap"),
    "mcap/robot.mcap": (McapAdapter(), FIXTURES / "mcap" / "robot.mcap"),
    "rosbag1/robot_none.bag": (Rosbag1Adapter(), FIXTURES / "rosbag1" / "robot_none.bag"),
    "rosbag2/mobile_base_sqlite3": (
        Rosbag2Adapter(),
        FIXTURES / "rosbag2" / "mobile_base_sqlite3" / "mobile_base_sqlite3_0.db3",
    ),
}
# Columns a decoded row holds that are not the payload's own.
_OWN: Final = {"value/sequence", "value/message_id", "value/data_bytes"}


def run(name: str, **config: Any) -> SourceOutput:
    adapter, path = SOURCES[name]
    return ingest_source(adapter, BytesReader(path.read_bytes()), config)  # type: ignore[arg-type]


def by_topic(output: SourceOutput) -> dict[str, tuple[Stream, list[dict[str, object]]]]:
    streams = {r.id: r for r in output.records() if isinstance(r, Stream)}
    found: dict[str, tuple[Stream, list[dict[str, object]]]] = {}
    for stream_id, batches in output.series().items():
        stream = streams[stream_id]
        topic = stream.topic.value if isinstance(stream.topic, Known) else ""
        rows = sorted((row for batch in batches for row in batch.rows()), key=row_order)
        found[str(topic)] = (stream, rows)
    return found


def decoded(row: dict[str, object]) -> dict[str, object]:
    """A row's payload values as the oracle writes them: lists for list cells."""
    return {
        name.removeprefix("value/"): list(value) if isinstance(value, tuple) else value
        for name, value in row.items()
        if name.startswith("value/") and name not in _OWN
    }


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_every_payload_decodes_to_what_the_official_reader_reads(name: str) -> None:
    output = run(name)
    found = by_topic(output)
    for topic, expected in ORACLE[name].items():
        _stream, rows = found[topic]
        assert len(rows) == len(expected), topic
        for row, message in zip(rows, expected, strict=True):
            if message is None:  # the official reader refuses it: every value unknown
                states = {v for k, v in row.items() if k.startswith("state/value/")}
                assert states == {"unknown"}, topic
                continue
            assert decoded(row) == message, (topic, row["seq"])


def test_a_payload_that_breaks_its_layout_is_unknown_and_reported() -> None:
    output = run("usv_survey.mcap")
    (finding,) = [f for f in output.findings() if f.code == "mcap.payload_undecodable"]
    assert finding.details["counts"] == {"short": 1}
    stream, rows = by_topic(output)["/sonar/range"]
    assert finding.records == (stream.id,)
    assert [row["state/time/2"] for row in rows] == ["known", "known", "unknown", "known"]


def test_a_leading_header_is_a_clock_of_its_own() -> None:
    output = run("quadruped_walk.mcap")
    stream, rows = by_topic(output)["/imu"]
    assert len(stream.clocks) == 3  # log_time, publish_time, header.stamp
    for row in rows:
        stamp = row["value/header.stamp.sec"] * 10**9 + row["value/header.stamp.nanosec"]  # type: ignore[operator]
        assert row["time/2"] == stamp
    tf_stream, _ = by_topic(output)["/tf"]
    assert len(tf_stream.clocks) == 2  # a TFMessage has a header per transform, none of its own


def test_decoding_can_be_turned_off_and_says_so() -> None:
    output = run("arm_cell.mcap", decode_payloads=False)
    reasons = {
        f.details["reason"] for f in output.findings() if f.code == "mcap.payload_not_decoded"
    }
    assert reasons == {"disabled"}
    for stream, rows in by_topic(output).values():
        assert len(stream.clocks) == 2
        assert not any(k.startswith("value/") and k != "value/sequence" for k in rows[0])


def test_a_layout_past_the_column_limit_keeps_only_its_header() -> None:
    output = run("usv_survey.mcap", max_decoded_columns=8)
    partly = {
        f.details["type"]: f.details["mode"]
        for f in output.findings()
        if f.code == "mcap.payload_partly_decoded"
    }
    assert partly["sensor_msgs/Imu"] == "header_only"
    _stream, rows = by_topic(output)["/imu"]
    assert {k for k in rows[0] if k.startswith("value/") and k != "value/sequence"} == {
        "value/header.frame_id",
        "value/header.stamp.nanosec",
        "value/header.stamp.sec",
    }
    not_decoded = {
        f.details["reason"] for f in output.findings() if f.code == "mcap.payload_not_decoded"
    }
    assert "layout_column_limit" in not_decoded  # TFMessage has no header to fall back on


def test_an_array_past_its_limit_is_not_covered() -> None:
    output = run("arm_cell.mcap", max_array_items=5)  # six joints, five transforms
    stream, rows = by_topic(output)["/joint_states"]
    (finding,) = [
        f
        for f in output.findings()
        if f.code == "mcap.payload_undecodable" and f.records == (stream.id,)
    ]
    assert (finding.category, finding.details["counts"]) == ("limit", {"array_limit": 4})
    assert {row["state/value/name[]"] for row in rows} == {"not_covered"}
    _, transforms = by_topic(output)["/tf"]
    assert {row["state/value/transforms[].child_frame_id"] for row in transforms} == {"known"}


# --- The decoder on its own ---------------------------------------------------------------------

IMAGE: Final = b"""std_msgs/Header header
uint32 height
uint32 width
string encoding
uint8 is_bigendian
uint32 step
uint8[] data
================================================================================
MSG: std_msgs/Header
builtin_interfaces/Time stamp
string frame_id
================================================================================
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec
"""


def cdr(*parts: bytes) -> bytes:
    return b"\x00\x01\x00\x00" + b"".join(parts)


def test_byte_arrays_are_walked_without_a_column() -> None:
    layout = compile_layout(
        parse_definition(IMAGE, "ros2msg", "sensor_msgs/msg/Image"), DecodeLimits()
    )
    assert [(item.path, item.reason) for item in layout.left_out] == [("data[]", "byte_array")]
    payload = cdr(
        b"\x05\x00\x00\x00\x06\x00\x00\x00",  # stamp
        b"\x04\x00\x00\x00cam\x00",  # frame_id
        b"\x02\x00\x00\x00\x03\x00\x00\x00",  # height, width
        b"\x05\x00\x00\x00mono\x00",  # encoding
        b"\x00",  # is_bigendian
        b"\x00\x00",  # padding: step is aligned to four
        b"\x03\x00\x00\x00",  # step
        b"\x06\x00\x00\x00" + bytes(6),  # data: its count, then its bytes
    )
    cells = Decoder(layout, True, DecodeLimits()).decode(payload)
    assert dict(zip((c.path for c in layout.columns), cells, strict=True)) == {
        "header.stamp.sec": 5,
        "header.stamp.nanosec": 6,
        "header.frame_id": "cam",
        "height": 2,
        "width": 3,
        "encoding": "mono",
        "is_bigendian": 0,
        "step": 3,
    }


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"", "short"),
        (b"\x01\x00\x00\x00", "encapsulation"),
        (cdr(b"\x01\x00\x00\x00"), "short"),
        (cdr(b"\xe8\x03\x00\x00"), "short"),  # a count no bytes left could hold
        (cdr(b"\xff\xff\xff\x7f"), "array_limit"),  # a count past the limit, never allocated
        (cdr(b"\x00\x00\x00\x00", b"\x00" * 16), "trailing_bytes"),
    ],
)
def test_hostile_payloads_are_malformed_never_a_crash(payload: bytes, reason: str) -> None:
    definition = parse_definition(b"float64[] values\n", "ros2msg", "pkg/msg/Values")
    decoder = Decoder(compile_layout(definition, DecodeLimits()), True, DecodeLimits())
    with pytest.raises(Malformed) as caught:
        decoder.decode(payload)
    assert caught.value.reason == reason


def test_strings_that_are_not_utf8_leave_only_their_cell_unknown() -> None:
    definition = parse_definition(b"string a\nint32 b\n", "ros2msg", "pkg/msg/Pair")
    decoder = Decoder(compile_layout(definition, DecodeLimits()), True, DecodeLimits())
    cells = decoder.decode(cdr(b"\x03\x00\x00\x00\xff\xfe\x00", b"\x00", b"\x07\x00\x00\x00"))
    assert cells[1] == 7 and cells[0] is not None and not isinstance(cells[0], str)


@pytest.mark.parametrize(
    ("text", "encoding", "reason"),
    [
        (b"Missing thing\n", "ros2msg", "malformed"),
        (b"int32 a\nint32 a\n", "ros2msg", "malformed"),
        (b"int32 a 3\n", "ros1msg", "malformed"),  # a ROS 1 field has no default
        (b"\xff\xfe", "ros2msg", "malformed"),
        (b"module pkg { module msg { union U { }; }; };", "ros2idl", "unsupported"),
        (
            b"module pkg { module msg { struct Values { long double x; }; }; };",
            "ros2idl",
            "unsupported",
        ),
        (b"x" * ((1 << 20) + 1), "ros2msg", "too_large"),
        (b"int32 a\n", "protobuf", "unsupported"),
    ],
)
def test_definitions_that_cannot_be_read_say_why(text: bytes, encoding: str, reason: str) -> None:
    with pytest.raises(DefinitionError) as caught:
        parse_definition(text, encoding, "pkg/msg/Values")
    assert caught.value.reason == reason


def test_constants_and_defaults_take_no_bytes() -> None:
    definition = parse_definition(
        b"uint8 OK=0  # a constant\nstring NAME=a # b\nfloat64 x 1.5\n", "ros2msg", "pkg/msg/C"
    )
    assert [f.name for f in definition.root_type.fields] == ["x"]


def test_a_type_that_holds_itself_is_refused() -> None:
    definition = parse_definition(b"Node[] children\n", "ros2msg", "pkg/msg/Node")
    with pytest.raises(DefinitionError):
        compile_layout(definition, DecodeLimits())


def test_idl_typedefs_sequences_bounds_and_annotations() -> None:
    text = b"""// comment
#include "x.idl"
module pkg {
  module msg {
    typedef double double__3[3];
    module Values_Constants { const uint8 OK = 0; };
    @verbatim (language="comment", text="a (paren) and \\"quote\\"")
    struct Values {
      @default (value=1.5)
      double__3 xyz;
      sequence<string<8>, 4> names;
      unsigned long long big;
      pkg::msg::Inner inner;
    };
  };
};
================================================================================
IDL: pkg/msg/Inner
module pkg { module msg { struct Inner { boolean flag; octet raw[2]; }; }; };
"""
    definition = parse_definition(text, "ros2idl", "pkg/msg/Values")
    layout = compile_layout(definition, DecodeLimits())
    assert [(c.path, str(c.type), c.repeated) for c in layout.columns] == [
        ("xyz[]", "float64", True),
        ("names[]", "string", True),
        ("big", "uint64", False),
        ("inner.flag", "bool", False),
    ]
    assert [(i.path, i.reason) for i in layout.left_out] == [("inner.raw[]", "byte_array")]


def config(**values: Any) -> AdapterConfig:
    return configure(McapAdapter.descriptor, values)


def test_a_stream_is_decoded_only_by_what_it_declares() -> None:
    def plan(**kwargs: Any) -> Decoding | NotDecoded:
        given = {
            "message_encoding": "cdr",
            "schema_encoding": "ros2msg",
            "schema_name": "pkg/msg/Values",
            "definition": b"float64 x\n",
            **kwargs,
        }
        return plan_stream(config=config(), **given)

    assert isinstance(plan(), Decoding)
    for kwargs, reason in (
        ({"message_encoding": "json"}, "message_encoding"),
        ({"schema_encoding": "ros1msg"}, "schema_encoding"),  # ROS 1 text for CDR bytes
        ({"definition": None}, "definition_absent"),
        ({"schema_name": "not a type"}, "definition_malformed"),
    ):
        found = plan(**kwargs)
        assert isinstance(found, NotDecoded) and found.reason == reason


def test_a_field_named_like_an_adapter_column_gets_no_second_column() -> None:
    found = plan_stream(
        config=config(),
        message_encoding="cdr",
        schema_encoding="ros2msg",
        schema_name="pkg/msg/Counter",
        definition=b"uint32 sequence\nfloat64 value\n",
        reserved=frozenset({"sequence"}),
    )
    assert isinstance(found, Decoding) and found.mode == "partial"
    assert [(i.path, i.reason) for i in found.left_out] == [("sequence", "name_taken")]
    assert [name for name, _, _ in found.series_columns()] == [
        "state/value/value",
        "value/value",
    ]
    cells = found.decoder.decode(cdr(b"\x07\x00\x00\x00", b"\x00" * 4, struct.pack("<d", 2.5)))
    assert cells == [2.5]


def test_a_ros1_header_whose_stamp_is_a_time_message_reads_sec_and_nanosec() -> None:
    text = (
        b"Header header\n"
        + b"=" * 80
        + b"\nMSG: std_msgs/Header\nbuiltin_interfaces/Time stamp\nstring frame_id\n"
        + b"=" * 80
        + b"\nMSG: builtin_interfaces/Time\nint32 sec\nuint32 nanosec\n"
    )
    found = plan_stream(
        config=config(),
        message_encoding="ros1",
        schema_encoding="ros1msg",
        schema_name="pkg/Stamped",
        definition=text,
    )
    assert isinstance(found, Decoding) and found.has_header
    payload = struct.pack("<iI", 3, 5) + struct.pack("<I", 1) + b"x"
    cells = found.decoder.decode(payload)
    assert found.decoder.stamp(cells) == 3 * 10**9 + 5


def test_a_payload_past_the_message_limit_is_never_read() -> None:
    found = plan_stream(
        config=config(max_message_bytes=8),
        message_encoding="cdr",
        schema_encoding="ros2msg",
        schema_name="pkg/msg/Values",
        definition=b"float64 x\n",
    )
    assert isinstance(found, Decoding)

    def unread() -> bytes:
        raise AssertionError("the payload is read")

    row = decode_row(found, unread, size=1 << 30)
    assert row.problem is not None and row.problem.reason == "message_limit"
    assert row.cells["state/value/x"] == "not_covered"


def test_a_bounded_string_in_an_idl_sequence_keeps_its_bound() -> None:
    text = b"module pkg { module msg { struct Names { sequence<string<2>> names; }; }; };"
    definition = parse_definition(text, "ros2idl", "pkg/msg/Names")
    decoder = Decoder(compile_layout(definition, DecodeLimits()), True, DecodeLimits())
    assert decoder.decode(cdr(b"\x01\x00\x00\x00", b"\x03\x00\x00\x00ab\x00")) == [("ab",)]
    with pytest.raises(Malformed) as caught:
        decoder.decode(cdr(b"\x01\x00\x00\x00", b"\x04\x00\x00\x00abc\x00"))
    assert caught.value.reason == "string_bound"


# --- Work bounded by more than bytes ------------------------------------------------------------

SEPARATOR: Final = "=" * 80


def msgs(root: str, **types: str) -> bytes:
    """A ``ros1msg``/``ros2msg`` definition: ``root``'s fields, then each ``MSG:`` section."""
    sections = [root, *(f"{SEPARATOR}\nMSG: {name}\n{body}" for name, body in types.items())]
    return "\n".join(sections).encode()


def _frames_writer() -> Any:
    spec = importlib.util.spec_from_file_location(
        "make_frames_writer", FIXTURES / "frames" / "make_frames.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ros2_mcap(
    schemas: dict[str, str], channels: dict[str, str], payloads: dict[str, bytes]
) -> bytes:
    """A small unchunked ROS 2 MCAP: ``channels`` topic to type, one message per topic."""
    writer = _frames_writer()
    listed = [
        writer.Channel(i + 1, topic, kind) for i, (topic, kind) in enumerate(channels.items())
    ]
    ids = {channel.topic: channel.id for channel in listed}
    messages = [
        writer.Message(ids[topic], 1_000 + ids[topic], data) for topic, data in payloads.items()
    ]
    return writer.write_mcap("ros2msg", schemas, listed, messages)  # type: ignore[no-any-return]


def test_arrays_of_a_type_with_no_bytes_walk_nothing_and_cost_no_source() -> None:
    """625 bytes of MCAP once cost a 60 s sandbox kill: 65,536 x 65,536 empty elements walked
    for a payload of four bytes, and the good channel beside it was lost with the source."""
    data = ros2_mcap(
        {
            "pkg/msg/Outer": "pkg/Inner[65536] a\n"
            f"{SEPARATOR}\nMSG: pkg/Inner\npkg/Empty[65536] e\n{SEPARATOR}\nMSG: pkg/Empty\n",
            "std_msgs/msg/String": "string data\n",
        },
        {"/nested": "pkg/msg/Outer", "/chatter": "std_msgs/msg/String"},
        {"/nested": cdr(), "/chatter": cdr(b"\x06\x00\x00\x00hello\x00")},
    )
    started = time.monotonic()
    output = ingest_source(McapAdapter(), BytesReader(data), {})
    assert time.monotonic() - started < 10
    topics = by_topic(output)
    _, chatter = topics["/chatter"]
    assert [(row["value/data"], row["state/value/data"]) for row in chatter] == [("hello", "known")]
    _, nested = topics["/nested"]
    assert len(nested) == 1  # decoded: the type has no bytes, so the payload is its header
    assert not [f for f in output.findings() if f.code == "mcap.payload_undecodable"]


def test_a_walk_past_its_budget_is_a_limit_of_that_message_only() -> None:
    definition = parse_definition(
        msgs("pkg/Inner[] a", **{"pkg/Inner": "pkg/Empty[] e", "pkg/Empty": ""}),
        "ros2msg",
        "pkg/msg/Outer",
    )
    limits = DecodeLimits()
    decoder = Decoder(compile_layout(definition, limits), True, limits)
    full = struct.pack("<I", 65_536)
    assert decoder.decode(cdr(struct.pack("<I", 3), full * 3)) == []  # 196,611 elements
    started = time.monotonic()
    with pytest.raises(Malformed) as caught:  # 100 x 65,536 elements in 404 bytes
        decoder.decode(cdr(struct.pack("<I", 100), full * 100))
    assert time.monotonic() - started < 5
    assert (caught.value.reason, caught.value.limit) == ("walk_limit", True)
    assert "walk_limit" in LIMIT_REASONS  # a limit finding, not corruption


def _doubling(depth: int, header: bool = False) -> bytes:
    """``T0`` .. ``T{depth}``, each two fields of the next, the last a byte array: 2^depth paths
    from a definition of a few hundred bytes."""
    root = ("Header header\n" if header else "") + "pkg/T1 a\npkg/T1 b"
    types = {f"pkg/T{i}": f"pkg/T{i + 1} a\npkg/T{i + 1} b" for i in range(1, depth)}
    types[f"pkg/T{depth}"] = "uint8[] x"
    if header:
        types["std_msgs/Header"] = "uint32 seq\ntime stamp\nstring frame_id"
    return msgs(root, **types)


def test_a_layout_that_unrolls_past_its_node_budget_is_refused_quickly() -> None:
    """A definition of a few kilobytes (763 bytes with short separators) once compiled to
    1,048,576 left-out paths: 822 MiB, 7.7 s."""
    text = _doubling(20)
    assert len(text) < 4096
    started = time.monotonic()
    with pytest.raises(DefinitionError) as caught:
        compile_layout(parse_definition(text, "ros1msg", "pkg/T0"), DecodeLimits())
    assert caught.value.reason == "node_limit"
    found = plan_stream(
        config=config(),
        message_encoding="ros1",
        schema_encoding="ros1msg",
        schema_name="pkg/T0",
        definition=text,
    )
    assert isinstance(found, NotDecoded) and found.reason == "layout_node_limit"
    assert time.monotonic() - started < 5
    headed = plan_stream(
        config=config(),
        message_encoding="ros1",
        schema_encoding="ros1msg",
        schema_name="pkg/T0",
        definition=_doubling(20, header=True),
    )
    assert isinstance(headed, Decoding) and headed.mode == "header_only"
    small = compile_layout(parse_definition(_doubling(4), "ros1msg", "pkg/T0"), DecodeLimits())
    assert len(small.left_out) == 2**4 < MAX_LAYOUT_NODES


def test_a_finding_lists_a_bounded_number_of_left_out_paths() -> None:
    text = "".join(f"uint8[] b{i:03}\n" for i in range(100)).encode()
    found = plan_stream(
        config=config(),
        message_encoding="cdr",
        schema_encoding="ros2msg",
        schema_name="pkg/msg/Blobs",
        definition=text,
    )
    assert isinstance(found, Decoding) and found.mode == "partial"
    details = found.details()
    assert details["left_out_count"] == 100
    assert isinstance(details["left_out"], list) and len(details["left_out"]) == LISTED_LEFT_OUT


def test_planned_definitions_are_kept_by_digest_and_bounded() -> None:
    too_large = b"float64 x\n" + b"#" * (1 << 20)
    before = dict(ros_streams._PLANNED)
    found = plan_stream(
        config=config(),
        message_encoding="cdr",
        schema_encoding="ros2msg",
        schema_name="pkg/msg/Big",
        definition=too_large,
    )
    assert isinstance(found, NotDecoded) and found.reason == "definition_too_large"
    assert before == ros_streams._PLANNED  # refused before the cache: nothing kept
    for i in range(80):
        plan_stream(
            config=config(),
            message_encoding="cdr",
            schema_encoding="ros2msg",
            schema_name="pkg/msg/Values",
            definition=f"float64 x{i}\n".encode(),
        )
    assert len(ros_streams._PLANNED) <= 64
    assert all(len(key[0]) == 32 for key in ros_streams._PLANNED)  # a digest, not the bytes


@pytest.mark.parametrize("over", [0, 1])
def test_an_mcap_payload_of_exactly_the_message_limit_decodes(over: int) -> None:
    payload = cdr(struct.pack("<I", 2), bytes(4), struct.pack("<2d", 1.5, 2.5))
    data = ros2_mcap(
        {"pkg/msg/Values": "float64[] values\n"},
        {"/values": "pkg/msg/Values"},
        {"/values": payload},
    )
    output = ingest_source(
        McapAdapter(), BytesReader(data), {"max_message_bytes": len(payload) - over}
    )
    (row,) = by_topic(output)["/values"][1]
    if over:
        assert row["state/value/values[]"] == "not_covered"
    else:
        assert (row["value/values[]"], row["state/value/values[]"]) == ((1.5, 2.5), "known")

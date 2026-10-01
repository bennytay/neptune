"""The schema registry: declared definitions parsed into layouts, bounded on hostile input."""

import json

import pytest

from neptune.derived.schemas import (
    ROOT_NAME,
    ArrayKind,
    Layout,
    LayoutState,
    PathKind,
    SchemaLimits,
    StreamLayout,
    canonical_type_name,
    layout_id,
    parse_definition,
    stream_layout_from_json,
)
from neptune.identity import canonical_json
from neptune.model.ids import ContentId, RecordId
from neptune.model.provenance import ByteRange, EvidenceRef

LIMITS = SchemaLimits()
SEP = "=" * 80
IMU = f"""# This message holds data from an IMU
std_msgs/Header header
geometry_msgs/Quaternion orientation
float64[9] orientation_covariance # row major
geometry_msgs/Vector3 angular_velocity
geometry_msgs/Vector3 linear_acceleration
{SEP}
MSG: std_msgs/Header
builtin_interfaces/Time stamp
string frame_id
{SEP}
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec
{SEP}
MSG: geometry_msgs/Quaternion
float64 x 0
float64 y 0
float64 z 0
float64 w 1
{SEP}
MSG: geometry_msgs/Vector3
float64 x
float64 y
float64 z
"""
TRANSFORM = f"""geometry_msgs/TransformStamped[] transforms
{SEP}
MSG: geometry_msgs/TransformStamped
Header header
string child_frame_id
Transform transform
{SEP}
MSG: std_msgs/Header
uint32 seq
time stamp
string frame_id
{SEP}
MSG: geometry_msgs/Transform
Vector3 translation
Quaternion rotation
{SEP}
MSG: geometry_msgs/Vector3
float64 x
float64 y
float64 z
{SEP}
MSG: geometry_msgs/Quaternion
float64 x
float64 y
float64 z
float64 w
"""


def known(
    encoding: str, name: str | None, text: str | bytes, limits: SchemaLimits = LIMITS
) -> Layout:
    data = text.encode() if isinstance(text, str) else text
    parsed = parse_definition(encoding, name, data, limits)
    assert parsed.state is LayoutState.KNOWN, parsed.problem
    assert parsed.layout is not None
    return parsed.layout


def paths(layout: Layout) -> list[tuple[str, str]]:
    return [(p.path, p.type) for p in layout.paths]


# --- ros2msg / ros1msg ---------------------------------------------------------------------------


def test_a_ros2_definition_flattens_in_declaration_order_through_its_dependencies() -> None:
    layout = known("ros2msg", "sensor_msgs/msg/Imu", IMU)
    assert layout.root == "sensor_msgs/Imu"
    assert paths(layout)[:4] == [
        ("header.stamp.sec", "int32"),
        ("header.stamp.nanosec", "uint32"),
        ("header.frame_id", "string"),
        ("orientation.x", "float64"),
    ]
    assert ("orientation_covariance[]", "float64") in paths(layout)
    assert all(p.kind is PathKind.PRIMITIVE and p.unit is None for p in layout.paths)
    quaternion = layout.type("geometry_msgs/Quaternion")
    assert quaternion is not None and quaternion.fields[3].default == "1"
    covariance = layout.type("sensor_msgs/Imu").field("orientation_covariance")  # type: ignore[union-attr]
    assert covariance is not None and covariance.array is not None
    assert (covariance.array.kind, covariance.array.length) == (ArrayKind.FIXED, 9)


def test_a_ros1_definition_resolves_header_and_names_in_the_users_package() -> None:
    layout = known("ros1msg", "tf2_msgs/TFMessage", TRANSFORM)
    assert ("transforms[].header.stamp", "time") in paths(layout)
    assert ("transforms[].transform.translation.x", "float64") in paths(layout)
    assert ("transforms[].transform.rotation.w", "float64") in paths(layout)
    stamped = layout.type("geometry_msgs/TransformStamped")
    assert stamped is not None and stamped.field("header").type == "std_msgs/Header"  # type: ignore[union-attr]


def test_ros2_bounds_constants_and_comments_are_kept_verbatim() -> None:
    text = (
        "uint8 MODE_IDLE=0  # idle\n"
        "string GREETING=hello # world\n"
        "string<=8 name\n"
        "int32[<=4] values\n"
        "int32[] samples\n"
        'string label "a b"\n'
    )
    layout = known("ros2msg", "pkg/msg/Thing", text)
    root = layout.type("pkg/Thing")
    assert root is not None
    by_name = {f.name: f for f in root.fields}
    assert by_name["MODE_IDLE"].constant == "0"
    assert by_name["GREETING"].constant == "hello # world"  # a string constant keeps its '#'
    assert by_name["name"].bound == 8
    assert by_name["values"].array.kind is ArrayKind.BOUNDED  # type: ignore[union-attr]
    assert by_name["samples"].array.kind is ArrayKind.UNBOUNDED  # type: ignore[union-attr]
    assert by_name["label"].default == '"a b"'
    assert [p.path for p in layout.paths] == ["name", "values[]", "samples[]", "label"]


def test_names_with_and_without_msg_are_one_type() -> None:
    assert canonical_type_name("sensor_msgs/msg/Imu") == "sensor_msgs/Imu"
    assert canonical_type_name("sensor_msgs/Imu") == "sensor_msgs/Imu"
    assert canonical_type_name("foxglove.PoseInFrame") is None
    text = f"geometry_msgs/msg/Vector3 v\n{SEP}\nMSG: geometry_msgs/Vector3\nfloat64 x\n"
    assert paths(known("ros2msg", "a/B", text)) == [("v.x", "float64")]


@pytest.mark.parametrize(
    ("encoding", "text", "reason", "line"),
    [
        ("ros2msg", "float64\n", "malformed", 1),
        ("ros2msg", "float64 1x\n", "malformed", 1),
        ("ros2msg", "flo@t x\n", "malformed", 1),
        ("ros2msg", "float64 x\nfloat64 x\n", "malformed", 2),
        ("ros2msg", f"float64 x\n{SEP}\nnot a header\n", "malformed", 3),
        ("ros2msg", f"float64 x\n{SEP}\n", "malformed", None),
        ("ros2msg", "a/b/c/d x\n", "malformed", 1),
        ("ros2msg", "pkg/srv/X x\n", "malformed", 1),
        ("ros2msg", "int32[<=] x\n", "malformed", 1),
        ("ros2msg", "int32[99999999999999999999] x\n", "malformed", 1),
        ("ros2msg", "float64<=3 x\n", "malformed", 1),
        ("ros2msg", "Vec V=1\n", "malformed", 1),
        ("ros2msg", "int32 X=\n", "malformed", 1),
        ("ros1msg", "float64 x 0\n", "malformed", 1),
        ("ros1msg", "string<=3 x\n", "malformed", 1),
        ("ros1msg", "int32[<=3] x\n", "malformed", 1),
        (
            "ros2msg",
            f"A a\n{SEP}\nMSG: pkg/A\nint32 x\n{SEP}\nMSG: pkg/A\nint64 x\n",
            "malformed",
            None,
        ),
    ],
)
def test_malformed_msg_text_is_unknown_with_a_reason_never_an_exception(
    encoding: str, text: str, reason: str, line: int | None
) -> None:
    parsed = parse_definition(encoding, "pkg/Root", text.encode(), LIMITS)
    assert parsed.state is LayoutState.UNKNOWN and parsed.layout is None
    assert parsed.problem is not None
    assert (parsed.problem.reason, parsed.problem.line) == (reason, line)


def test_a_dependency_repeated_identically_is_one_type() -> None:
    text = f"A a\n{SEP}\nMSG: pkg/A\nint32 x\n{SEP}\nMSG: pkg/msg/A\nint32 x\n"
    layout = known("ros2msg", "pkg/Root", text)
    assert [t.name for t in layout.types] == ["pkg/Root", "pkg/A"]


def test_an_undefined_dependency_ends_its_path_unresolved() -> None:
    layout = known("ros2msg", "pkg/Root", "geometry_msgs/Point p\nint8 k\n")
    assert [(p.path, p.kind) for p in layout.paths] == [
        ("p", PathKind.UNRESOLVED),
        ("k", PathKind.PRIMITIVE),
    ]


def test_a_self_referencing_definition_stops_at_the_cycle() -> None:
    text = (
        f"Node root\n{SEP}\nMSG: pkg/Node\nint32 v\nNode[] children\nOther o\n"
        f"{SEP}\nMSG: pkg/Other\nNode back\n"
    )
    layout = known("ros2msg", "pkg/Tree", text)
    assert [(p.path, p.kind) for p in layout.paths] == [
        ("root.v", PathKind.PRIMITIVE),
        ("root.children[]", PathKind.RECURSIVE),
        ("root.o.back", PathKind.RECURSIVE),
    ]


def test_an_empty_dependency_is_a_path_of_its_own() -> None:
    text = f"Empty e\n{SEP}\nMSG: pkg/Empty\nuint8 ONLY_A_CONSTANT=1\n"
    layout = known("ros2msg", "pkg/Root", text)
    assert [(p.path, p.kind) for p in layout.paths] == [("e", PathKind.EMPTY)]


def chain(depth: int) -> str:
    """``T0`` holds ``T1`` holds … ``T{depth}``, which holds a float."""
    sections = ["T1 next\n"]
    for index in range(1, depth):
        sections.append(f"MSG: pkg/T{index}\nT{index + 1} next\n")
    sections.append(f"MSG: pkg/T{depth}\nfloat64 value\n")
    return f"{SEP}\n".join(sections)


def test_deep_nesting_stops_at_the_depth_limit() -> None:
    limits = SchemaLimits(max_depth=8)
    layout = known("ros2msg", "pkg/T0", chain(40), limits)
    (path,) = layout.paths
    assert path.kind is PathKind.DEPTH
    assert path.path.count(".") == 7
    assert known("ros2msg", "pkg/T0", chain(5), limits).paths[0].kind is PathKind.PRIMITIVE


def test_an_exponential_fan_out_is_cut_at_the_path_limit() -> None:
    # Each level holds the next twice: 2**30 paths if flattened whole.
    sections = ["L1 a\nL1 b\n"]
    for index in range(1, 30):
        sections.append(f"MSG: pkg/L{index}\nL{index + 1} a\nL{index + 1} b\n")
    sections.append("MSG: pkg/L30\nfloat32 x\n")
    layout = known("ros2msg", "pkg/L0", f"{SEP}\n".join(sections), SchemaLimits(max_paths=100))
    assert len(layout.paths) == 100 and layout.truncated


@pytest.mark.parametrize(
    ("limits", "text", "reason"),
    [
        (SchemaLimits(max_definition_bytes=16), "float64 x\n" * 4, "definition_too_large"),
        (SchemaLimits(max_types=3), chain(5), "type_limit"),
    ],
)
def test_limits_make_a_definition_unknown(limits: SchemaLimits, text: str, reason: str) -> None:
    parsed = parse_definition("ros2msg", "pkg/Root", text.encode(), limits)
    assert parsed.state is LayoutState.UNKNOWN and parsed.problem is not None
    assert parsed.problem.reason == reason


def test_too_many_fields_is_a_field_limit() -> None:
    text = "".join(f"float64 f{index}\n" for index in range(11))
    parsed = parse_definition("ros2msg", "pkg/Root", text.encode(), SchemaLimits(max_fields=10))
    assert parsed.problem is not None and parsed.problem.reason == "field_limit"


def test_a_huge_definition_within_the_limit_parses_in_bounded_paths() -> None:
    text = "".join(f"float64 f{index}\n" for index in range(16384))  # ~250 KB
    layout = known("ros2msg", "pkg/Wide", text)
    assert len(layout.paths) == LIMITS.max_paths and layout.truncated


def test_bytes_that_are_not_utf8_are_unknown() -> None:
    parsed = parse_definition("ros2msg", "pkg/Root", b"float64 \xff\n", LIMITS)
    assert parsed.problem is not None and parsed.problem.reason == "invalid_utf8"


@pytest.mark.parametrize("encoding", ["protobuf", "flatbuffer", "ros2idl", "omgidl", "neptune-x"])
def test_other_encodings_are_not_covered(encoding: str) -> None:
    parsed = parse_definition(encoding, "foxglove.PoseInFrame", b"\x00\x01", LIMITS)
    assert (parsed.state, parsed.layout, parsed.problem) == (LayoutState.NOT_COVERED, None, None)


# --- jsonschema ----------------------------------------------------------------------------------


def schema(value: object) -> bytes:
    return json.dumps(value).encode()


def test_a_json_schema_gives_properties_items_refs_and_declared_units() -> None:
    document = {
        "$defs": {
            "Vec": {
                "type": "object",
                "properties": {"x": {"type": "number", "unit": "m"}, "y": {"type": "number"}},
            }
        },
        "type": "object",
        "properties": {
            "voltage": {"type": "number", "unit": "V"},
            "cells": {"type": "array", "items": {"type": "number", "unit": "V"}, "maxItems": 12},
            "position": {"$ref": "#/$defs/Vec"},
            "pose": {"type": "object", "properties": {"at": {"$ref": "#/$defs/Vec"}}},
            "maybe": {"type": ["number", "null"]},
            "remote": {"$ref": "https://example.com/x.json"},
            "loose": {},
        },
    }
    layout = known("jsonschema", "fixture.Battery", schema(document))
    assert [(p.path, p.type, p.unit) for p in layout.paths] == [
        ("voltage", "number", "V"),
        ("cells[]", "number", "V"),
        ("position.x", "number", "m"),
        ("position.y", "number", None),
        ("pose.at.x", "number", "m"),
        ("pose.at.y", "number", None),
        ("maybe", "number|null", None),
        ("remote", "https://example.com/x.json", None),
        ("loose", "any", None),
    ]
    assert layout.paths[7].kind is PathKind.UNRESOLVED
    cells = layout.type("fixture.Battery").field("cells")  # type: ignore[union-attr]
    assert cells is not None and cells.array is not None and cells.array.length == 12


def test_a_recursive_json_schema_stops_at_the_cycle() -> None:
    document = {"properties": {"value": {"type": "number"}, "child": {"$ref": "#"}}}
    layout = known("jsonschema", "tree", schema(document))
    assert [(p.path, p.kind) for p in layout.paths] == [
        ("value", PathKind.PRIMITIVE),
        ("child", PathKind.RECURSIVE),
    ]


def test_json_field_names_with_path_characters_are_escaped() -> None:
    layout = known("jsonschema", "x", schema({"properties": {"a.b[0]": {"type": "string"}}}))
    assert layout.paths[0].path == "a\\.b\\[0\\]"


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"[" * 100_000, "nesting_limit"),
        (b'{"a":' * 1000 + b"1" + b"}" * 1000, "nesting_limit"),
        (b"{not json", "malformed"),
        (b"[1, 2]", "malformed"),
        (b'{"properties": [1]}', "malformed"),
        (b'{"x": 1' + b"0" * 5000 + b"}", "malformed"),
    ],
)
def test_hostile_json_schemas_are_unknown(data: bytes, reason: str) -> None:
    parsed = parse_definition("jsonschema", "x", data, LIMITS)
    assert parsed.state is LayoutState.UNKNOWN and parsed.problem is not None
    assert parsed.problem.reason == reason


def test_brackets_inside_json_strings_do_not_count_as_nesting() -> None:
    data = schema({"properties": {"a": {"type": "string", "description": "[" * 500 + '\\"{'}}})
    assert known("jsonschema", "x", data).paths[0].path == "a"


def test_a_root_name_that_looks_like_a_pointer_is_not_used() -> None:
    layout = known(
        "jsonschema", "#/properties/a", schema({"properties": {"a": {"properties": {}}}})
    )
    assert layout.root == ROOT_NAME


# --- determinism and the record ------------------------------------------------------------------

STREAM = RecordId("rec:sha256:" + "1" * 64)
TRANSFORM_ID = RecordId("rec:sha256:" + "2" * 64)
REF = EvidenceRef(ContentId("sha256:" + "3" * 64), (ByteRange(10, 20),))


def test_the_same_bytes_parse_to_the_same_layout() -> None:
    first = parse_definition("ros1msg", "tf2_msgs/TFMessage", TRANSFORM.encode(), LIMITS)
    second = parse_definition("ros1msg", "tf2_msgs/TFMessage", TRANSFORM.encode(), LIMITS)
    assert first == second


@pytest.mark.parametrize(
    ("encoding", "text"),
    [("ros2msg", IMU), ("ros2msg", "float64\n"), ("protobuf", "x"), ("jsonschema", "{}")],
)
def test_a_layout_line_round_trips_canonically(encoding: str, text: str) -> None:
    parsed = parse_definition(encoding, "sensor_msgs/msg/Imu", text.encode(), LIMITS)
    line = StreamLayout(
        id=layout_id(TRANSFORM_ID, STREAM),
        transform=TRANSFORM_ID,
        stream=STREAM,
        schema_name="sensor_msgs/msg/Imu",
        schema_encoding=encoding,
        definition=REF,
        state=parsed.state,
        layout=parsed.layout,
        problem=parsed.problem,
    )
    data = canonical_json.loads(canonical_json.dumps(line.to_json()))
    assert isinstance(data, dict)
    assert stream_layout_from_json(data) == line
    assert data["assertion_kind"] == "observed"


def test_a_layout_line_is_read_strictly() -> None:
    parsed = parse_definition("ros2msg", "pkg/A", b"int8 x\n", LIMITS)
    line = StreamLayout(
        layout_id(TRANSFORM_ID, STREAM), TRANSFORM_ID, STREAM, None, None, None,
        parsed.state, parsed.layout, None,
    )  # fmt: skip
    good = dict(line.to_json())
    for broken in (
        {**good, "assertion_kind": "inferred"},
        {**good, "id": "rec:sha256:" + "9" * 64},
        {**good, "state": "unknown"},
        {**good, "extra": 1},
        {k: v for k, v in good.items() if k != "types"},
    ):
        with pytest.raises((ValueError, TypeError)):
            stream_layout_from_json(broken)
    with pytest.raises(ValueError, match="exactly when"):
        StreamLayout(
            layout_id(TRANSFORM_ID, STREAM), TRANSFORM_ID, STREAM, None, None, None,
            LayoutState.UNKNOWN, parsed.layout, None,
        )  # fmt: skip


def test_limits_are_positive_integers() -> None:
    with pytest.raises(ValueError):
        SchemaLimits(max_paths=0)
    with pytest.raises(ValueError):
        SchemaLimits(max_depth=True)

"""The schema registry: declared definitions parsed into layouts, bounded on hostile input."""

import json
import time
import tracemalloc
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING

import pytest

from neptune.derived import schemas
from neptune.derived.schemas import (
    LIMIT_REASONS,
    ROOT_NAME,
    ArrayKind,
    DefinitionLayout,
    Layout,
    LayoutState,
    PathKind,
    SchemaLimits,
    StreamLayout,
    canonical_type_name,
    definition_layout_from_json,
    definition_layout_id,
    layout_id,
    parse_definition,
    problem,
    stream_layout_from_json,
)
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.model.ids import ContentId, RecordId
from neptune.model.provenance import ByteRange, EvidenceRef

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject, JsonValue

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
    assert path.path.count(".") == 8  # a path crosses at most max_depth message fields
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
def test_limits_make_a_definition_not_covered(limits: SchemaLimits, text: str, reason: str) -> None:
    parsed = parse_definition("ros2msg", "pkg/Root", text.encode(), limits)
    assert parsed.state is LayoutState.NOT_COVERED and parsed.layout is None
    assert parsed.problem is not None and parsed.problem.reason == reason


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
def test_hostile_json_schemas_are_unknown_or_past_a_limit(data: bytes, reason: str) -> None:
    parsed = parse_definition("jsonschema", "x", data, LIMITS)
    limit = reason in LIMIT_REASONS
    assert parsed.state is (LayoutState.NOT_COVERED if limit else LayoutState.UNKNOWN)
    assert parsed.problem is not None and parsed.problem.reason == reason


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


def definition_line(encoding: str, name: str, text: str) -> DefinitionLayout:
    parsed = parse_definition(encoding, name, text.encode(), LIMITS)
    assert parsed.layout is not None
    content = content_id(text.encode())
    line_id = definition_layout_id(TRANSFORM_ID, content, encoding, parsed.layout.root)
    return DefinitionLayout(line_id, TRANSFORM_ID, content, encoding, REF, parsed.layout)


def test_a_definition_layout_line_round_trips_canonically() -> None:
    line = definition_line("ros2msg", "sensor_msgs/msg/Imu", IMU)
    data = canonical_json.loads(canonical_json.dumps(line.to_json()))
    assert isinstance(data, dict)
    assert definition_layout_from_json(data) == line
    assert data["assertion_kind"] == "observed" and data["root"] == "sensor_msgs/Imu"
    assert data["definition"] == REF.to_json()  # where the bytes sit
    for broken in (
        {**data, "assertion_kind": "inferred"},
        {**data, "root": "other/Root"},  # the id no longer recomputes
        {**data, "content": "sha256:" + "4" * 64},
        {**data, "extra": 1},
        {k: v for k, v in data.items() if k != "types"},
        {k: v for k, v in data.items() if k != "definition"},
    ):
        with pytest.raises((ValueError, TypeError)):
            definition_layout_from_json(broken)


def test_a_definition_layout_is_keyed_by_content_encoding_and_root() -> None:
    text = "int8 x\n"
    first = definition_line("ros2msg", "pkg/msg/A", text)
    assert definition_line("ros2msg", "pkg/A", text).id == first.id  # the same root
    assert definition_line("ros1msg", "pkg/A", text).id != first.id
    assert definition_line("ros2msg", "pkg/B", text).id != first.id
    assert definition_line("ros2msg", "pkg/A", text + "\n").id != first.id


@pytest.mark.parametrize(
    ("state", "layout", "found"),
    [
        (LayoutState.KNOWN, "definition", None),
        (LayoutState.KNOWN_ABSENT, None, None),
        (LayoutState.NOT_COVERED, None, None),
        (LayoutState.NOT_COVERED, None, problem("layout_limit", "big", None, {"bytes": 9})),
        (LayoutState.UNKNOWN, None, problem("malformed", "bad", 3)),
    ],
)
def test_a_stream_layout_line_round_trips_canonically(
    state: LayoutState, layout: str | None, found: object
) -> None:
    line = StreamLayout(
        id=layout_id(TRANSFORM_ID, STREAM),
        transform=TRANSFORM_ID,
        stream=STREAM,
        schema_name="sensor_msgs/msg/Imu",
        schema_encoding="ros2msg",
        definition=REF,
        state=state,
        layout=None if layout is None else definition_line("ros2msg", "a/B", IMU).id,
        problem=found,  # type: ignore[arg-type]
    )
    data = canonical_json.loads(canonical_json.dumps(line.to_json()))
    assert isinstance(data, dict)
    assert stream_layout_from_json(data) == line
    assert data["assertion_kind"] == "observed"
    assert "types" not in data and "paths" not in data  # the layout is the definition's line


def test_a_stream_layout_line_is_read_strictly() -> None:
    definition = definition_line("ros2msg", "pkg/A", "int8 x\n").id
    line = StreamLayout(
        layout_id(TRANSFORM_ID, STREAM), TRANSFORM_ID, STREAM, None, None, REF,
        LayoutState.KNOWN, definition, None,
    )  # fmt: skip
    good = dict(line.to_json())
    no_counts: JsonValue = {"message": "m", "reason": "r", "counts": {}}
    broken: JsonObject
    for broken in (
        {**good, "assertion_kind": "inferred"},
        {**good, "id": "rec:sha256:" + "9" * 64},
        {**good, "state": "unknown"},
        {**good, "extra": 1},
        {**good, "types": []},
        {**good, "problem": no_counts},
        {k: v for k, v in good.items() if k != "layout"},
    ):
        with pytest.raises((ValueError, TypeError)):
            stream_layout_from_json(broken)
    with pytest.raises(ValueError, match="exactly when"):
        StreamLayout(
            layout_id(TRANSFORM_ID, STREAM), TRANSFORM_ID, STREAM, None, None, REF,
            LayoutState.UNKNOWN, definition, None,
        )  # fmt: skip
    with pytest.raises(ValueError, match="says why"):
        StreamLayout(
            layout_id(TRANSFORM_ID, STREAM), TRANSFORM_ID, STREAM, None, None, REF,
            LayoutState.UNKNOWN, None, None,
        )  # fmt: skip
    with pytest.raises(ValueError, match="only an unknown or not covered"):
        StreamLayout(
            layout_id(TRANSFORM_ID, STREAM), TRANSFORM_ID, STREAM, None, None, None,
            LayoutState.KNOWN_ABSENT, None, problem("malformed", "x"),
        )  # fmt: skip


def test_limits_are_positive_integers() -> None:
    with pytest.raises(ValueError):
        SchemaLimits(max_paths=0)
    with pytest.raises(ValueError):
        SchemaLimits(max_depth=True)


def test_a_ros2_default_keeps_a_quoted_hash_and_drops_the_comment() -> None:
    layout = known("ros2msg", "pkg/A", "string s \"a#b\"  # the comment\nstring t 'c#' #x\n")
    root = layout.type("pkg/A")
    assert root is not None
    assert [f.default for f in root.fields] == ['"a#b"', "'c#'"]


def test_a_max_depth_of_one_still_expands_root_fields() -> None:
    layout = known("ros2msg", "pkg/T0", chain(3), SchemaLimits(max_depth=1))
    assert [(p.path, p.kind) for p in layout.paths] == [("next.next", PathKind.DEPTH)]


@pytest.mark.parametrize("token", ["\u00b2", "1" + "0" * 3000, "01x", ""])
def test_hostile_pointer_tokens_leave_a_reference_unresolved(token: str) -> None:
    document = '{"allOf":[{}],"properties":{"a":{"$ref":"#/allOf/' + token + '"}}}'
    layout = known("jsonschema", "x", document)
    assert [(p.path, p.kind) for p in layout.paths] == [("a", PathKind.UNRESOLVED)]


def test_lone_surrogates_in_a_json_schema_are_unknown_not_a_failed_package() -> None:
    for document in (
        '{"properties":{"\\ud800":{"type":"number"}}}',
        '{"properties":{"a":{"type":"number","unit":"\\udfff"}}}',
        '{"properties":{"a":{"$ref":"#/\\ud800"}}}',
    ):
        parsed = parse_definition("jsonschema", "x", document.encode(), LIMITS)
        assert parsed.problem is not None and parsed.problem.reason == "invalid_utf8", document


def test_a_reference_to_a_primitive_is_that_primitive() -> None:
    document = {
        "definitions": {
            "num": {"type": "number", "unit": "m"},
            "alias": {"$ref": "#/definitions/num"},
        },
        "properties": {"x": {"$ref": "#/definitions/num"}, "y": {"$ref": "#/definitions/alias"}},
    }
    layout = known("jsonschema", "x", schema(document))
    assert [(p.path, p.type, p.kind, p.unit) for p in layout.paths] == [
        ("x", "number", PathKind.PRIMITIVE, "m"),
        ("y", "number", PathKind.PRIMITIVE, "m"),
    ]


def test_a_reference_cycle_without_an_object_is_bounded() -> None:
    document = {"a": {"$ref": "#/b"}, "b": {"$ref": "#/a"}, "properties": {"p": {"$ref": "#/a"}}}
    layout = known("jsonschema", "x", schema(document))
    assert [(p.path, p.kind) for p in layout.paths] == [("p", PathKind.UNRESOLVED)]


def test_a_root_reference_is_followed_and_a_root_composition_is_not_parsed() -> None:
    document = {
        "$ref": "#/$defs/P",
        "$defs": {"P": {"properties": {"x": {"type": "number"}, "q": {"properties": {}}}}},
        "properties": {"q": {"type": "string"}},  # beside a root $ref: not the root's
    }
    layout = known("jsonschema", "pkg.P", schema(document))
    assert [(p.path, p.kind) for p in layout.paths] == [
        ("x", PathKind.PRIMITIVE),
        ("q", PathKind.EMPTY),
    ]
    assert len({t.name for t in layout.types}) == len(layout.types)
    composed = schema({"allOf": [{"properties": {"x": {"type": "number"}}}]})
    parsed = parse_definition("jsonschema", "pkg.P", composed, LIMITS)
    assert parsed.problem is not None and parsed.problem.reason == "composition_not_parsed"
    nested = schema({"properties": {"c": {"oneOf": [{"type": "number"}, {"type": "string"}]}}})
    assert known("jsonschema", "x", nested).paths[0].kind is PathKind.UNRESOLVED


# --- hostile size: names, pointers, layout bytes (review of PR #66) -----------------------------


def peak_bytes(run: Callable[[], object]) -> int:
    tracemalloc.start()
    try:
        run()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def fan_out(name: str, fields: int = 4096) -> str:
    """A root of ``fields`` fields of one type whose single field is called ``name``: every path
    repeats ``name``, so the paths are ``fields`` times its length."""
    root = "\n".join(f"pkg/B f{index}" for index in range(fields))
    return f"{root}\n{SEP}\nMSG: pkg/B\nfloat64 {name}\n"


@pytest.mark.parametrize(
    ("encoding", "text"),
    [
        ("ros2msg", fan_out("a" + "b" * 40_000)),  # a field name: the repro's 930x MCAP
        ("ros2msg", "pkg/" + "T" * 2000 + " x\n"),  # a field type
        ("ros2msg", "int8 " + "C" * 2000 + "=1\n"),  # a constant name
        ("ros2msg", f"pkg/B b\n{SEP}\nMSG: pkg/" + "B" * 2000 + "\nint8 x\n"),  # a type name
        ("jsonschema", json.dumps({"properties": {"p" * 2000: {"type": "number"}}})),
    ],
)
def test_a_name_past_the_limit_is_not_covered_never_shortened(encoding: str, text: str) -> None:
    parsed = parse_definition(encoding, "pkg/A", text.encode(), LIMITS)
    assert (parsed.state, parsed.layout) == (LayoutState.NOT_COVERED, None)
    assert parsed.problem is not None and parsed.problem.reason == "name_limit"
    assert dict(parsed.problem.counts)["limit"] == LIMITS.max_name_bytes
    assert len(parsed.problem.message) < 200  # a message quotes no hostile name whole


def test_a_root_name_past_the_limit_is_not_covered() -> None:
    parsed = parse_definition("ros2msg", "pkg/" + "A" * 2000, b"int8 x\n", LIMITS)
    assert parsed.problem is not None and parsed.problem.reason == "name_limit"


def test_a_pointer_past_the_limit_is_not_covered_and_split_never() -> None:
    # The repro: 14k properties naming one definition whose own $ref is a 600 KB pointer took
    # 16 s and held 10.8 GB of type text; it is one pointer_limit now.
    long_pointer = "#/" + "a/" * 300_000
    document = {
        "properties": {f"p{index}": {"$ref": "#/$defs/d0"} for index in range(14_000)},
        "$defs": {"d0": {"$ref": long_pointer}},
    }
    data = json.dumps(document, separators=(",", ":")).encode()
    assert len(data) <= LIMITS.max_definition_bytes
    started = time.perf_counter()
    parsed = parse_definition("jsonschema", "pkg/A", data, LIMITS)
    assert time.perf_counter() - started < 5
    assert parsed.state is LayoutState.NOT_COVERED
    assert parsed.problem is not None and parsed.problem.reason == "pointer_limit"


def test_each_pointer_is_resolved_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    resolve = schemas._resolve

    def counting(root: Mapping[str, object], pointer: str) -> object:
        calls.append(pointer)
        return resolve(root, pointer)

    monkeypatch.setattr(schemas, "_resolve", counting)
    document = {
        "properties": {f"p{index}": {"$ref": "#/$defs/a"} for index in range(5000)},
        "$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"type": "number", "unit": "m"}},
    }
    layout = known("jsonschema", "pkg/A", json.dumps(document))
    assert len(layout.paths) == 4096 and layout.paths[0].unit == "m"
    assert sorted(calls) == ["#/$defs/a", "#/$defs/b"]


def test_a_layout_past_its_byte_limit_is_not_covered_with_counts_in_bounded_memory() -> None:
    # Names under the limit still multiply: 4096 paths x 2 x ~2 KB is 16 MB of path text.
    name = "n" * 1000
    text = fan_out(name).replace(
        f"float64 {name}\n", f"pkg/C {name}\n{SEP}\nMSG: pkg/C\nfloat64 {name}\nfloat64 m{name}\n"
    )
    found: list[object] = []
    peak = peak_bytes(
        lambda: found.append(parse_definition("ros2msg", "pkg/A", text.encode(), LIMITS))
    )
    (parsed,) = found
    assert isinstance(parsed, schemas.Parsed)
    assert (parsed.state, parsed.layout) == (LayoutState.NOT_COVERED, None)
    assert parsed.problem is not None and parsed.problem.reason == "layout_limit"
    counts = dict(parsed.problem.counts)
    assert counts["limit"] == LIMITS.max_layout_bytes < counts["bytes"]
    assert 0 < counts["paths"] < 4096 and counts["types"] == 3
    assert peak < 3 * LIMITS.max_layout_bytes  # stopped as the paths reached the limit


def test_types_past_the_layout_limit_stop_before_flattening() -> None:
    # 16k properties all typed by one 3 KB unresolved pointer: 48 MB of type text when written.
    document = {
        "properties": {f"p{index}": {"$ref": "#/$defs/d0"} for index in range(16_000)},
        "$defs": {"d0": {"$ref": "#/" + "a/" * 1500}},
    }
    parsed = parse_definition("jsonschema", "pkg/A", json.dumps(document).encode(), LIMITS)
    assert parsed.problem is not None and parsed.problem.reason == "layout_limit"
    assert dict(parsed.problem.counts)["paths"] == 0


def test_a_small_layout_limit_applies_and_is_config() -> None:
    small = SchemaLimits(max_layout_bytes=200)
    parsed = parse_definition("ros2msg", "sensor_msgs/Imu", IMU.encode(), small)
    assert parsed.problem is not None and parsed.problem.reason == "layout_limit"
    assert small.to_json()["max_layout_bytes"] == 200
    assert {"max_name_bytes", "max_pointer_bytes"} <= set(small.to_json())


def test_flatten_reads_each_type_s_fields_once() -> None:
    # The review's flatten_cpu repro: 4096 visits to a type of 12k constants re-scanned them all
    # on every visit. Each type's fields are now read once, however often it is visited.
    reads: dict[str, int] = {}

    class Counted(tuple[schemas.Field, ...]):
        name = ""

        def __iter__(self) -> Iterator[schemas.Field]:
            reads[self.name] = reads.get(self.name, 0) + 1
            return super().__iter__()

    def counted(declared: schemas.MessageType) -> schemas.MessageType:
        fields = Counted(declared.fields)
        fields.name = declared.name
        return schemas.MessageType(declared.name, fields)

    text = fan_out("v").replace(
        "float64 v\n", "".join(f"int8 C{i}=1\n" for i in range(12_000)) + "float64 v\n"
    )
    layout = known("ros2msg", "pkg/A", text)
    types = [counted(t) for t in layout.types]
    flattened = schemas._flatten("pkg/A", types, LIMITS)
    assert len(flattened.paths) == 4096
    assert max(reads.values()) <= 2  # once for its size, once for its members


def test_messages_quote_hostile_text_briefly() -> None:
    parsed = parse_definition("ros2msg", "pkg/A", b"f@" + b"x" * 900 + b" y\n", LIMITS)
    assert parsed.problem is not None and parsed.problem.reason == "malformed"
    assert len(parsed.problem.message) < 200


@pytest.mark.parametrize(
    "text",
    [
        "float64 " + "!" * 2000 + "\n",  # a field name
        "pkg/" + "!" * 2000 + " x\n",  # a field type
        f"int8 x\n{SEP}\nMSG: pkg/" + "!" * 2000 + "\nint8 y\n",  # a type name
    ],
)
def test_a_long_corrupt_token_is_malformed_not_a_limit(text: str) -> None:
    parsed = parse_definition("ros2msg", "pkg/A", text.encode(), LIMITS)
    assert parsed.state is LayoutState.UNKNOWN
    assert parsed.problem is not None and parsed.problem.reason == "malformed"
    assert len(parsed.problem.message) < 200


def test_a_resolved_type_name_past_the_limit_is_not_covered() -> None:
    # Each part is under the limit; the ``pkg/Name`` the layout would write is not.
    package, name = "p" * 700, "N" * 700
    text = f"{name} x\n{SEP}\nMSG: {package}/{name}\nint8 y\n"
    parsed = parse_definition("ros2msg", f"{package}/Root", text.encode(), LIMITS)
    assert parsed.problem is not None and parsed.problem.reason == "name_limit"
    assert dict(parsed.problem.counts)["bytes"] == len(package) + 1 + len(name)


def test_escaped_text_counts_as_written() -> None:
    # 900 control characters are 5400 bytes once escaped: 4096 paths of them pass 4 MiB as
    # written although their characters alone do not.
    unit = "\u0001" * 900
    document = {
        "$defs": {"v": {"type": "number", "unit": unit}},
        "properties": {f"p{i}": {"$ref": "#/$defs/v"} for i in range(4096)},
    }
    data = json.dumps(document).encode()
    assert 4096 * len(unit) < LIMITS.max_layout_bytes
    parsed = parse_definition("jsonschema", "pkg/A", data, LIMITS)
    assert parsed.problem is not None and parsed.problem.reason == "layout_limit"

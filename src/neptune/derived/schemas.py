"""Declared message layouts: the schema registry and the ``stream_layout`` kind (ADR 0049).

A ``Stream`` keeps its schema as declared: a name, an encoding and an ``EvidenceRef`` to the
definition's bytes (ADR 0018). This module reads those bytes, and nothing else, into the types the
definition declares and the field paths a message of the stream holds. It decodes no message.

- ``ros1msg`` and ``ros2msg``: ``.msg`` text, the root type first and every type it depends on
  after a line of ``=`` and a ``MSG: <name>`` line (as MCAP and rosbag2 write them). Arrays (fixed,
  bounded, unbounded), bounded strings, constants and ROS 2 defaults are kept verbatim; a name
  without a package is resolved in the package of the type that uses it, and ROS 1's ``Header`` is
  ``std_msgs/Header``. Names are compared as ``pkg/Name``, so ``pkg/msg/Name`` is the same type.
- ``jsonschema``: properties of objects, items of arrays, local ``$ref`` (``#/...``), and a
  property's ``unit`` keyword, the one place a parsed schema declares a unit.
- Every other encoding (``protobuf``, ``flatbuffer``, ``ros2idl``, ``omgidl``, …) is
  ``not_covered``: its declared name is still a fact the semantic rules read.

What the layout says is ``observed``: it is a decoding of the definition's bytes, cited by the
stream's ``schema_definition``. It is still a derivative under its own transform, so it lives in a
package's ``derived/`` tables and never changes a canonical ``Stream``. ``.msg`` has no syntax for
units, so a ``.msg`` field's unit is ``None``: the schema declares none. Units by convention are an
inference (``neptune.derived.semantics``).

All input is hostile: every parser is iterative and bounded by ``SchemaLimits`` (bytes, types,
fields, JSON nesting, path depth and count). A definition past a limit, malformed or not UTF-8 is
``unknown`` with a reason and a ``Problem``; nothing here raises on bad bytes.
"""

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final

from neptune.derived.provenance import DERIVED_SCHEMA_VERSION, derived_object
from neptune.identity.ids import record_id
from neptune.model._fields import exact_object, json_array, json_bool, json_int, json_str
from neptune.model.ids import RecordId, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import AssertionKind
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json

LAYOUT_KIND: Final = "stream_layout"
OBSERVED: Final = str(AssertionKind.OBSERVED)
PARSED_ENCODINGS: Final = frozenset({"jsonschema", "ros1msg", "ros2msg"})
ROOT_NAME: Final = "<root>"  # the root type's name when the stream declares no usable name


@dataclass(frozen=True)
class SchemaLimits:
    """Bounds on what one definition may cost. They are the introspection transform's config."""

    max_definition_bytes: int = 1 << 20
    max_types: int = 1024
    max_fields: int = 16384  # across every type of one definition
    max_json_depth: int = 64
    max_depth: int = 32  # nested message fields one path may cross
    max_paths: int = 4096

    def __post_init__(self) -> None:
        for name, value in self.to_json().items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")

    def to_json(self) -> JsonObject:
        return {
            "max_definition_bytes": self.max_definition_bytes,
            "max_depth": self.max_depth,
            "max_fields": self.max_fields,
            "max_json_depth": self.max_json_depth,
            "max_paths": self.max_paths,
            "max_types": self.max_types,
        }


class LayoutState(StrEnum):
    """The missingness of a stream's layout (non-negotiable 3)."""

    KNOWN = "known"  # the definition was parsed
    KNOWN_ABSENT = "known_absent"  # the stream declares it has no schema
    NOT_COVERED = "not_covered"  # declared in an encoding this registry does not parse
    UNKNOWN = "unknown"  # no definition to read, or it could not be read or parsed (``reason``)


class ArrayKind(StrEnum):
    FIXED = "fixed"  # ``T[3]``
    BOUNDED = "bounded"  # ``T[<=3]``, or a JSON array with ``maxItems``
    UNBOUNDED = "unbounded"  # ``T[]``


class PathKind(StrEnum):
    """What ends a field path."""

    PRIMITIVE = "primitive"  # a value of a primitive type: the path's ``type``
    UNRESOLVED = "unresolved"  # a message type the definition names but does not define
    RECURSIVE = "recursive"  # a type that holds itself: flattening stops at the cycle
    EMPTY = "empty"  # a message type with no fields
    DEPTH = "depth"  # ``max_depth`` nested message fields: flattening stops


_NAME: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Array:
    kind: ArrayKind
    length: int | None  # the fixed length or the bound; ``None`` when unbounded

    def __post_init__(self) -> None:
        if (self.length is None) != (self.kind is ArrayKind.UNBOUNDED):
            raise ValueError(f"a {self.kind} array has a length exactly when it is not unbounded")
        if self.length is not None and (isinstance(self.length, bool) or self.length < 0):
            raise ValueError(f"an array length is a non-negative integer, got {self.length!r}")

    def to_json(self) -> JsonObject:
        return _present({"kind": str(self.kind), "length": self.length})


def _array_from_json(data: JsonValue) -> Array:
    obj = _some(data, "array", {"kind"}, {"length"})
    return Array(ArrayKind(json_str(obj["kind"], "kind")), _optional_int(obj, "length"))


def _present(entries: Mapping[str, "JsonValue | None"]) -> JsonObject:
    """``entries`` without the absent ones: canonical JSON has no null (ADR 0004)."""
    return {key: value for key, value in entries.items() if value is not None}


def _some(
    data: JsonValue, what: str, required: set[str], optional: set[str]
) -> Mapping[str, JsonValue]:
    """A JSON object with every ``required`` key and any of the ``optional`` ones, no other."""
    present = set(data) & optional if isinstance(data, Mapping) else set()
    return exact_object(data, what, required | present)


def _optional_str(obj: Mapping[str, JsonValue], key: str) -> str | None:
    return json_str(obj[key], key) if key in obj else None


def _optional_int(obj: Mapping[str, JsonValue], key: str) -> int | None:
    return json_int(obj[key], key) if key in obj else None


@dataclass(frozen=True)
class Field:
    """One field as its type declares it.

    ``type`` is a primitive's name (``float64``, ``number``) or a message type's (``pkg/Name``,
    or a JSON pointer for a nested JSON Schema object). ``bound`` is a bounded string's bound;
    ``constant`` and ``default`` are a constant's value and a ROS 2 default, verbatim; ``unit`` is
    a unit the schema itself declares for the field, verbatim, else ``None``.
    """

    name: str
    type: str
    primitive: bool
    array: Array | None = None
    bound: int | None = None
    constant: str | None = None
    default: str | None = None
    unit: str | None = None

    def to_json(self) -> JsonObject:
        return _present(
            {
                "array": None if self.array is None else self.array.to_json(),
                "bound": self.bound,
                "constant": self.constant,
                "default": self.default,
                "name": self.name,
                "primitive": self.primitive,
                "type": self.type,
                "unit": self.unit,
            }
        )


def _field_from_json(data: JsonValue) -> Field:
    obj = _some(
        data,
        "field",
        {"name", "primitive", "type"},
        {"array", "bound", "constant", "default", "unit"},
    )
    return Field(
        name=json_str(obj["name"], "name"),
        type=json_str(obj["type"], "type"),
        primitive=json_bool(obj["primitive"]),
        array=_array_from_json(obj["array"]) if "array" in obj else None,
        bound=_optional_int(obj, "bound"),
        constant=_optional_str(obj, "constant"),
        default=_optional_str(obj, "default"),
        unit=_optional_str(obj, "unit"),
    )


@dataclass(frozen=True)
class MessageType:
    """A type the definition declares, its fields in declaration order (constants included)."""

    name: str
    fields: tuple[Field, ...]

    def field(self, name: str) -> Field | None:
        """The non-constant field called ``name``, if the type has one."""
        for field in self.fields:
            if field.name == name and field.constant is None:
                return field
        return None

    def to_json(self) -> JsonObject:
        return {"fields": [field.to_json() for field in self.fields], "name": self.name}


def _type_from_json(data: JsonValue) -> MessageType:
    obj = exact_object(data, "type", {"fields", "name"})
    return MessageType(
        json_str(obj["name"], "name"),
        tuple(_field_from_json(f) for f in json_array(obj["fields"], "fields")),
    )


@dataclass(frozen=True)
class FieldPath:
    """One path from the root to where flattening stopped: ``orientation.x``,
    ``points[].positions[]`` (``[]`` marks an array level). A segment holding ``.``, ``[``, ``]``
    or ``\\`` has them escaped with ``\\``."""

    path: str
    type: str
    kind: PathKind
    unit: str | None = None

    def to_json(self) -> JsonObject:
        return _present(
            {"kind": str(self.kind), "path": self.path, "type": self.type, "unit": self.unit}
        )


def _path_from_json(data: JsonValue) -> FieldPath:
    obj = _some(data, "path", {"kind", "path", "type"}, {"unit"})
    return FieldPath(
        json_str(obj["path"], "path"),
        json_str(obj["type"], "type"),
        PathKind(json_str(obj["kind"], "kind")),
        _optional_str(obj, "unit"),
    )


@dataclass(frozen=True)
class Layout:
    """What a definition declares: its root type, every type, and the flattened paths."""

    root: str
    types: tuple[MessageType, ...]
    paths: tuple[FieldPath, ...]
    truncated: bool  # ``max_paths`` paths were listed and more exist

    def type(self, name: str) -> MessageType | None:
        for declared in self.types:
            if declared.name == name:
                return declared
        return None


@dataclass(frozen=True)
class Problem:
    """Why a definition is ``unknown``: a reason code, a line (``.msg``), and a message."""

    reason: str
    message: str
    line: int | None = None

    def to_json(self) -> JsonObject:
        return _present({"line": self.line, "message": self.message, "reason": self.reason})


@dataclass(frozen=True)
class Parsed:
    """A parse's outcome: a layout exactly when ``state`` is ``known``."""

    state: LayoutState
    layout: Layout | None = None
    problem: Problem | None = None


class _Malformed(Exception):
    def __init__(self, reason: str, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.problem = Problem(reason, message, line)


# --- ROS .msg -----------------------------------------------------------------------------------

_ROS2_PRIMITIVES: Final = frozenset(
    {
        "bool",
        "byte",
        "char",
        "float32",
        "float64",
        "int8",
        "uint8",
        "int16",
        "uint16",
        "int32",
        "uint32",
        "int64",
        "uint64",
        "string",
        "wstring",
    }
)
_ROS1_PRIMITIVES: Final = (_ROS2_PRIMITIVES - {"wstring"}) | {"time", "duration"}
_SEPARATOR: Final = re.compile(r"={3,}")
_TYPE: Final = re.compile(
    r"(?P<base>[A-Za-z][A-Za-z0-9_]*(?:/[A-Za-z][A-Za-z0-9_]*){0,2})"
    r"(?:<=(?P<bound>[0-9]+))?"
    r"(?:\[(?P<array>(?:<=)?[0-9]*)\])?"
)
_CONSTANT: Final = re.compile(r"(?P<name>[A-Za-z][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)")
_DIGITS: Final = 18  # a length beyond 10**18 is not a length any reader allocates


def _canonical(name: str, line: int | None) -> str:
    """``pkg/msg/Name`` and ``pkg/Name`` are one type; anything else with slashes is malformed."""
    parts = name.split("/")
    if len(parts) == 3:
        if parts[1] != "msg":
            raise _Malformed("malformed", f"{name!r} is not a message type name", line)
        return f"{parts[0]}/{parts[2]}"
    if len(parts) > 3 or not all(_NAME.fullmatch(part) for part in parts):
        raise _Malformed("malformed", f"{name!r} is not a message type name", line)
    return name


def canonical_type_name(name: str) -> str | None:
    """``pkg/Name`` for a ROS message type name (``pkg/msg/Name`` or ``pkg/Name``), the name
    itself when it has no package, and ``None`` when it is not a ROS type name at all."""
    try:
        return _canonical(name, None)
    except _Malformed:
        return None


def _root_name(schema_name: str | None) -> str:
    if schema_name is None or not schema_name or schema_name.startswith("#"):
        return ROOT_NAME  # "#..." would collide with a JSON Schema pointer's type name
    try:
        return _canonical(schema_name, None)
    except _Malformed:
        return schema_name


def _number(text: str, line: int) -> int:
    if len(text) > _DIGITS:
        raise _Malformed("malformed", f"length {text[:24]}… is out of range", line)
    return int(text)


def _ros_type(
    token: str, package: str | None, ros2: bool, line: int
) -> tuple[str, bool, Array | None, int | None]:
    match = _TYPE.fullmatch(token)
    if match is None:
        raise _Malformed("malformed", f"{token!r} is not a field type", line)
    base, bound_text, array_text = match["base"], match["bound"], match["array"]
    primitives = _ROS2_PRIMITIVES if ros2 else _ROS1_PRIMITIVES
    bound = None
    if bound_text is not None:
        if not ros2 or base not in {"string", "wstring"}:
            raise _Malformed("malformed", f"only a ROS 2 string has a bound: {token!r}", line)
        bound = _number(bound_text, line)
    array = None
    if array_text is not None:
        if array_text == "":
            array = Array(ArrayKind.UNBOUNDED, None)
        elif array_text.startswith("<="):
            if not ros2 or array_text == "<=":
                raise _Malformed("malformed", f"{token!r} is not an array type", line)
            array = Array(ArrayKind.BOUNDED, _number(array_text[2:], line))
        else:
            array = Array(ArrayKind.FIXED, _number(array_text, line))
    if base in primitives:
        return base, True, array, bound
    if not ros2 and base == "Header":
        return "std_msgs/Header", False, array, bound
    if "/" not in base:
        return (f"{package}/{base}" if package else base), False, array, bound
    return _canonical(base, line), False, array, bound


def _ros_field(text: str, package: str | None, ros2: bool, line: int) -> Field:
    parts = text.split(None, 1)
    if len(parts) < 2:
        raise _Malformed("malformed", "a field line is a type and a name", line)
    head, rest = parts[0], parts[1].strip()
    kind, primitive, array, bound = _ros_type(head, package, ros2, line)
    constant = _CONSTANT.fullmatch(rest)
    if constant is not None:
        if not primitive or array is not None:
            raise _Malformed("malformed", "only a primitive scalar may be a constant", line)
        value = constant["value"].strip()
        if kind not in {"string", "wstring"}:
            value = value.split("#", 1)[0].strip()  # a string constant keeps its '#'
        if not value:
            raise _Malformed("malformed", f"constant {constant['name']} has no value", line)
        return Field(constant["name"], kind, True, None, bound, constant=value)
    words = rest.split("#", 1)[0].split(None, 1)
    if not words or not _NAME.fullmatch(words[0]):
        raise _Malformed("malformed", f"{rest[:40]!r} is not a field name", line)
    default = words[1].strip() if len(words) > 1 else None
    if default is not None and not ros2:
        raise _Malformed("malformed", "a ROS 1 field has no default value", line)
    return Field(words[0], kind, primitive, array, bound, default=default)


def _parse_msg(text: str, root: str, ros2: bool, limits: SchemaLimits) -> list[MessageType]:
    sections: list[tuple[str, list[tuple[int, str]]]] = [(root, [])]
    expect_name = False
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if _SEPARATOR.fullmatch(line):
            expect_name = True
            continue
        if not line or line.startswith("#"):
            continue
        if expect_name:
            if not line.startswith("MSG:"):
                raise _Malformed("malformed", "a dependency starts with 'MSG: <name>'", number)
            sections.append((_canonical(line[4:].strip(), number), []))
            if len(sections) > limits.max_types:
                raise _Malformed("type_limit", f"more than {limits.max_types} types", number)
            expect_name = False
            continue
        sections[-1][1].append((number, line))
    if expect_name:
        raise _Malformed("malformed", "a separator is not followed by a 'MSG: <name>' line")
    types: dict[str, MessageType] = {}
    count = 0
    for name, lines in sections:
        package = name.split("/", 1)[0] if "/" in name else None
        fields: list[Field] = []
        seen: set[str] = set()
        for number, line in lines:
            count += 1
            if count > limits.max_fields:
                raise _Malformed("field_limit", f"more than {limits.max_fields} fields", number)
            field = _ros_field(line, package, ros2, number)
            if field.name in seen:
                raise _Malformed("malformed", f"{name} declares {field.name} twice", number)
            seen.add(field.name)
            fields.append(field)
        declared = MessageType(name, tuple(fields))
        if name in types and types[name] != declared:
            raise _Malformed("malformed", f"{name} is defined twice, differently")
        types.setdefault(name, declared)
    return list(types.values())


# --- JSON Schema --------------------------------------------------------------------------------

_JSON_PRIMITIVES: Final = frozenset({"boolean", "integer", "null", "number", "string"})


def _json_depth(text: str, limit: int) -> bool:
    """Whether ``text`` nests arrays and objects deeper than ``limit`` (outside strings): checked
    before ``json.loads``, whose recursion a hostile document could exhaust."""
    depth = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > limit:
                return True
        elif char in "]}":
            depth -= 1
    return False


def _pointer_token(name: str) -> str:
    return name.replace("~", "~0").replace("/", "~1")


def _resolve(root: Mapping[str, object], pointer: str) -> object:
    """The value at a local JSON pointer (``#/a/b``), or ``None`` when there is none."""
    node: object = root
    for token in pointer[2:].split("/") if pointer != "#" else ():
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return None
    return node


def _parse_json_schema(text: str, root_name: str, limits: SchemaLimits) -> list[MessageType]:
    if _json_depth(text, limits.max_json_depth):
        raise _Malformed("nesting_limit", f"nested deeper than {limits.max_json_depth}")
    try:
        document = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise _Malformed("malformed", f"not JSON: {exc}") from None
    if not isinstance(document, Mapping):
        raise _Malformed("malformed", "a JSON Schema is an object")
    names: dict[int, str] = {}  # each object schema visited, by identity, to its type name
    pending: list[tuple[str, Mapping[str, object]]] = []
    types: list[MessageType] = []
    count = 0

    def name_of(schema: Mapping[str, object], name: str) -> str:
        if id(schema) not in names:
            if len(names) >= limits.max_types:
                raise _Malformed("type_limit", f"more than {limits.max_types} types")
            names[id(schema)] = name
            pending.append((name, schema))
        return names[id(schema)]

    def field(name: str, schema: object, pointer: str, array: bool = False) -> Field:
        if not isinstance(schema, Mapping):
            return Field(name, "any", True)
        unit = schema.get("unit")
        unit = unit if isinstance(unit, str) else None
        ref = schema.get("$ref")
        if isinstance(ref, str):
            target = _resolve(document, ref) if ref.startswith("#") else None
            if isinstance(target, Mapping):
                return Field(name, name_of(target, ref), False, unit=unit)
            return Field(name, ref, False, unit=unit)
        kind = schema.get("type")
        if isinstance(kind, list) and all(isinstance(k, str) for k in kind):
            text_kinds = [k for k in kind if isinstance(k, str)]
            if set(text_kinds) <= _JSON_PRIMITIVES:
                return Field(name, "|".join(text_kinds), True, unit=unit)
            kind = next((k for k in text_kinds if k != "null"), None)
        if kind == "object" or (kind is None and isinstance(schema.get("properties"), Mapping)):
            return Field(name, name_of(schema, pointer), False, unit=unit)
        if kind == "array" and not array:
            items = schema.get("items")
            inner = field(name, items, pointer + "/items", array=True)
            limit = schema.get("maxItems")
            shape = (
                Array(ArrayKind.BOUNDED, limit)
                if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0
                else Array(ArrayKind.UNBOUNDED, None)
            )
            return Field(name, inner.type, inner.primitive, shape, unit=inner.unit or unit)
        if isinstance(kind, str) and (kind in _JSON_PRIMITIVES or kind == "array"):
            return Field(name, kind, True, unit=unit)
        return Field(name, "any", True, unit=unit)

    name_of(document, root_name)
    done = 0
    while done < len(pending):
        name, schema = pending[done]
        done += 1
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise _Malformed("malformed", f"{name}'s properties are not an object")
        pointer = "#" if name == root_name else name
        fields = []
        for key, value in properties.items():
            count += 1
            if count > limits.max_fields:
                raise _Malformed("field_limit", f"more than {limits.max_fields} fields")
            fields.append(field(key, value, f"{pointer}/properties/{_pointer_token(key)}"))
        types.append(MessageType(name, tuple(fields)))
    return types


# --- flattening ---------------------------------------------------------------------------------

_ESCAPE: Final = re.compile(r"([.\[\]\\])")


def _segment(field: Field) -> str:
    text = _ESCAPE.sub(r"\\\1", field.name)
    return text + "[]" if field.array is not None else text


def flatten(root: str, types: Iterable[MessageType], limits: SchemaLimits) -> Layout:
    """Every path from ``root`` to a primitive, or to where flattening had to stop, in
    declaration order, depth first. At most ``max_paths`` paths (``truncated`` says more exist)
    and ``max_depth`` nested message fields; a cycle ends its path as ``recursive``."""
    declared = tuple(types)
    by_name = {t.name: t for t in declared}
    paths: list[FieldPath] = []
    # One frame per type being flattened: its remaining fields, path prefix, depth, ancestors.
    Frame = tuple[Iterator[Field], str, int, tuple[str, ...]]
    stack: list[Frame] = [(iter(by_name[root].fields), "", 0, (root,))]
    while stack:
        fields, prefix, depth, ancestors = stack[-1]
        field = next(fields, None)
        if field is None:
            stack.pop()
            continue
        if field.constant is not None:
            continue
        path = prefix + _segment(field)
        ending: PathKind | None = None
        if field.primitive:
            ending = PathKind.PRIMITIVE
        elif field.type not in by_name:
            ending = PathKind.UNRESOLVED
        elif field.type in ancestors:
            ending = PathKind.RECURSIVE
        elif not any(f.constant is None for f in by_name[field.type].fields):
            ending = PathKind.EMPTY
        elif depth + 1 >= limits.max_depth:
            ending = PathKind.DEPTH
        if ending is None:
            inner = by_name[field.type]
            stack.append((iter(inner.fields), path + ".", depth + 1, (*ancestors, inner.name)))
            continue
        if len(paths) >= limits.max_paths:
            return Layout(root, declared, tuple(paths), True)
        paths.append(FieldPath(path, field.type, ending, field.unit))
    return Layout(root, declared, tuple(paths), False)


def parse_definition(
    encoding: str, schema_name: str | None, data: bytes, limits: SchemaLimits
) -> Parsed:
    """Parse one definition's bytes declared in ``encoding``. Never raises on bad bytes."""
    if encoding not in PARSED_ENCODINGS:
        return Parsed(LayoutState.NOT_COVERED)
    if len(data) > limits.max_definition_bytes:
        problem = Problem(
            "definition_too_large",
            f"{len(data)} bytes, more than the {limits.max_definition_bytes} a definition may be",
        )
        return Parsed(LayoutState.UNKNOWN, problem=problem)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return Parsed(LayoutState.UNKNOWN, problem=Problem("invalid_utf8", str(exc)))
    root = _root_name(schema_name)
    try:
        if encoding == "jsonschema":
            types = _parse_json_schema(text, root, limits)
        else:
            types = _parse_msg(text, root, encoding == "ros2msg", limits)
    except _Malformed as exc:
        return Parsed(LayoutState.UNKNOWN, problem=exc.problem)
    return Parsed(LayoutState.KNOWN, flatten(root, types, limits))


# --- the record ---------------------------------------------------------------------------------


def layout_id(transform: RecordId, stream: RecordId) -> RecordId:
    return record_id(LAYOUT_KIND, {"stream": stream, "transform": transform})


@dataclass(frozen=True)
class StreamLayout:
    """A stream's declared layout as a derived table line (ADR 0049 §2), ``observed``.

    ``definition`` is the bytes it was read from (the stream's ``schema_definition``), its
    provenance. ``schema_name`` and ``schema_encoding`` repeat the stream's as declared (``None``
    where the stream's is not ``Known``). ``layout`` is present exactly when ``state`` is
    ``known``; ``problem`` says why it is ``unknown``.
    """

    kind: ClassVar[str] = LAYOUT_KIND
    id: RecordId
    transform: RecordId
    stream: RecordId
    schema_name: str | None
    schema_encoding: str | None
    definition: EvidenceRef | None
    state: LayoutState
    layout: Layout | None
    problem: Problem | None

    def __post_init__(self) -> None:
        if self.id != layout_id(self.transform, self.stream):
            raise ValueError(f"{self.id} is not the id of this stream's layout")
        if (self.layout is not None) != (self.state is LayoutState.KNOWN):
            raise ValueError("a layout is present exactly when its state is known")
        if self.problem is not None and self.state is not LayoutState.UNKNOWN:
            raise ValueError("only an unknown layout has a problem")

    @property
    def assertion_kind(self) -> str:
        return OBSERVED

    @property
    def paths(self) -> tuple[FieldPath, ...]:
        return () if self.layout is None else self.layout.paths

    def to_json(self) -> JsonObject:
        layout = self.layout
        return _present(
            {
                "assertion_kind": OBSERVED,
                "definition": None if self.definition is None else self.definition.to_json(),
                "id": self.id,
                "kind": LAYOUT_KIND,
                "paths": None if layout is None else [p.to_json() for p in layout.paths],
                "problem": None if self.problem is None else self.problem.to_json(),
                "root": None if layout is None else layout.root,
                "schema_encoding": self.schema_encoding,
                "schema_name": self.schema_name,
                "schema_version": DERIVED_SCHEMA_VERSION,
                "state": str(self.state),
                "stream": self.stream,
                "transform": self.transform,
                "truncated": None if layout is None else layout.truncated,
                "types": None if layout is None else [t.to_json() for t in layout.types],
            }
        )


_LAYOUT_REQUIRED: Final = {"id", "state", "stream", "transform"}
_LAYOUT_OPTIONAL: Final = {
    "definition",
    "paths",
    "problem",
    "root",
    "schema_encoding",
    "schema_name",
    "truncated",
    "types",
}
_LAYOUT_PARTS: Final = {"paths", "root", "truncated", "types"}


def stream_layout_from_json(data: JsonValue) -> StreamLayout:
    """Parse strictly; the id must recompute from the stream and transform."""
    present = set(data) & _LAYOUT_OPTIONAL if isinstance(data, Mapping) else set()
    obj = derived_object(data, LAYOUT_KIND, _LAYOUT_REQUIRED | present, frozenset({OBSERVED}))
    state = LayoutState(json_str(obj["state"], "state"))
    layout = None
    if state is LayoutState.KNOWN:
        if missing := _LAYOUT_PARTS - present:
            raise ValueError(f"a known layout lacks {sorted(missing)}")
        layout = Layout(
            json_str(obj["root"], "root"),
            tuple(_type_from_json(t) for t in json_array(obj["types"], "types")),
            tuple(_path_from_json(p) for p in json_array(obj["paths"], "paths")),
            json_bool(obj["truncated"]),
        )
    elif present & _LAYOUT_PARTS:
        raise ValueError(f"a {state} layout has no types or paths")
    problem = None
    if "problem" in obj:
        entry = _some(obj["problem"], "problem", {"message", "reason"}, {"line"})
        problem = Problem(
            json_str(entry["reason"], "reason"),
            json_str(entry["message"], "message"),
            _optional_int(entry, "line"),
        )
    return StreamLayout(
        id=parse_record_id(json_str(obj["id"], "id")),
        transform=parse_record_id(json_str(obj["transform"], "transform")),
        stream=parse_record_id(json_str(obj["stream"], "stream")),
        schema_name=_optional_str(obj, "schema_name"),
        schema_encoding=_optional_str(obj, "schema_encoding"),
        definition=evidence_ref_from_json(obj["definition"]) if "definition" in obj else None,
        state=state,
        layout=layout,
        problem=problem,
    )

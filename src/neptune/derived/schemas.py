"""Declared message layouts: the schema registry, the ``definition_layout`` and ``stream_layout``
kinds (ADR 0049).

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
package's ``derived/`` tables and never changes a canonical ``Stream``. A layout is written once per
distinct definition (a ``definition_layout``, keyed by the bytes' content id, encoding and root
name); each stream's ``stream_layout`` line names it, so channels sharing a schema share one
layout. ``.msg`` has no syntax for units, so a ``.msg`` field's unit is ``None``: the schema
declares none. Units by convention are an inference (``neptune.derived.semantics``).

All input is hostile: every parser is iterative and bounded by ``SchemaLimits`` (bytes, types,
fields, JSON nesting, name and pointer length, path depth and count, and the bytes a layout may
emit). A definition past a limit is ``not_covered`` and a malformed or non-UTF-8 one ``unknown``,
each with a reason and a ``Problem``; nothing is cut short into a ``known`` layout, and nothing here
raises on bad bytes.
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
from neptune.model.ids import ContentId, RecordId, parse_content_id, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import AssertionKind
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json

LAYOUT_KIND: Final = "stream_layout"
DEFINITION_KIND: Final = "definition_layout"
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
    max_name_bytes: int = 1024  # a type, field or property name
    max_pointer_bytes: int = 4096  # a JSON pointer: a ``$ref``, or a nested object's type name
    max_layout_bytes: int = 4 << 20  # one definition's layout line, as emitted

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
            "max_layout_bytes": self.max_layout_bytes,
            "max_name_bytes": self.max_name_bytes,
            "max_paths": self.max_paths,
            "max_pointer_bytes": self.max_pointer_bytes,
            "max_types": self.max_types,
        }


class LayoutState(StrEnum):
    """The missingness of a stream's layout (non-negotiable 3)."""

    KNOWN = "known"  # the definition was parsed: its layout is a ``definition_layout``
    KNOWN_ABSENT = "known_absent"  # the stream declares it has no schema
    # declared in an encoding this registry does not parse, or past a limit (``problem``)
    NOT_COVERED = "not_covered"
    UNKNOWN = "unknown"  # no definition to read, or it could not be read or parsed (``problem``)


# The problem reasons that are a limit or budget: the definition is ``not_covered``, not unknown.
LIMIT_REASONS: Final = frozenset(
    {
        "definition_too_large",
        "field_limit",
        "introspection_budget",
        "layout_limit",
        "name_limit",
        "nesting_limit",
        "output_budget",
        "pointer_limit",
        "type_limit",
    }
)


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
    """Why a definition is ``unknown`` or past a limit: a reason code, a line (``.msg``), a
    message, and for a limit the ``counts`` it was measured by (bytes, paths, the limit…)."""

    reason: str
    message: str
    line: int | None = None
    counts: tuple[tuple[str, int], ...] = ()  # sorted by name

    def to_json(self) -> JsonObject:
        counts: JsonObject | None = dict(self.counts) if self.counts else None
        return _present(
            {"counts": counts, "line": self.line, "message": self.message, "reason": self.reason}
        )


def problem(
    reason: str, message: str, line: int | None = None, counts: Mapping[str, int] | None = None
) -> Problem:
    return Problem(reason, message, line, tuple(sorted((counts or {}).items())))


def _problem_from_json(data: JsonValue) -> Problem:
    entry = _some(data, "problem", {"message", "reason"}, {"counts", "line"})
    counts = entry.get("counts", {})
    if not isinstance(counts, Mapping) or ("counts" in entry and not counts):
        raise ValueError("a problem's counts are a non-empty JSON object")
    return problem(
        json_str(entry["reason"], "reason"),
        json_str(entry["message"], "message"),
        _optional_int(entry, "line"),
        {key: json_int(value, key) for key, value in counts.items()},
    )


@dataclass(frozen=True)
class Parsed:
    """A parse's outcome: a layout exactly when ``state`` is ``known``; a problem when it is
    ``unknown``, or ``not_covered`` past a limit."""

    state: LayoutState
    layout: Layout | None = None
    problem: Problem | None = None


def failed(found: Problem) -> Parsed:
    """The outcome for ``found``: ``not_covered`` past a limit, else ``unknown``."""
    limit = found.reason in LIMIT_REASONS
    return Parsed(LayoutState.NOT_COVERED if limit else LayoutState.UNKNOWN, problem=found)


class _Malformed(Exception):
    def __init__(
        self,
        reason: str,
        message: str,
        line: int | None = None,
        counts: Mapping[str, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.problem = problem(reason, message, line, counts)


_SHOWN: Final = 64  # how much of a hostile name a message quotes


def _shown(text: str) -> str:
    """``text`` as a message quotes it: at most ``_SHOWN`` characters, so a message stays small
    whatever the input. It is message text, never a value."""
    return repr(text if len(text) <= _SHOWN else text[:_SHOWN] + "…")


def _size(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def _cap(text: str, limit: int, reason: str, what: str, line: int | None = None) -> str:
    """``text`` when it is at most ``limit`` UTF-8 bytes; past it, the whole definition is past
    a limit (``reason``), never a shortened name."""
    if len(text) > limit or (len(text) * 4 > limit and _size(text) > limit):
        raise _Malformed(
            reason,
            f"a {what} of {_size(text)} bytes is longer than the {limit} it may be",
            line,
            {"bytes": _size(text), "limit": limit},
        )
    return text


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
            raise _Malformed("malformed", f"{_shown(name)} is not a message type name", line)
        return f"{parts[0]}/{parts[2]}"
    if len(parts) > 3 or not all(_NAME.fullmatch(part) for part in parts):
        raise _Malformed("malformed", f"{_shown(name)} is not a message type name", line)
    return name


def canonical_type_name(name: str) -> str | None:
    """``pkg/Name`` for a ROS message type name (``pkg/msg/Name`` or ``pkg/Name``), the name
    itself when it has no package, and ``None`` when it is not a ROS type name at all."""
    try:
        return _canonical(name, None)
    except _Malformed:
        return None


def root_name(schema_name: str | None) -> str:
    """The root type's name a definition declared as ``schema_name`` is parsed under: streams
    whose names give the same root share a parse."""
    return _root_name(schema_name)


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
        raise _Malformed("malformed", f"{_shown(token)} is not a field type", line)
    base, bound_text, array_text = match["base"], match["bound"], match["array"]
    primitives = _ROS2_PRIMITIVES if ros2 else _ROS1_PRIMITIVES
    bound = None
    if bound_text is not None:
        if not ros2 or base not in {"string", "wstring"}:
            raise _Malformed("malformed", f"only a ROS 2 string has a bound: {_shown(token)}", line)
        bound = _number(bound_text, line)
    array = None
    if array_text is not None:
        if array_text == "":
            array = Array(ArrayKind.UNBOUNDED, None)
        elif array_text.startswith("<="):
            if not ros2 or array_text == "<=":
                raise _Malformed("malformed", f"{_shown(token)} is not an array type", line)
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


def _uncommented(text: str) -> str:
    """``text`` up to its first ``#`` outside a quoted default (``string s "a#b"``)."""
    quote = None
    for index, char in enumerate(text):
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#":
            return text[:index]
    return text


def _ros_field(
    text: str,
    package: str | None,
    ros2: bool,
    line: int,
    limits: SchemaLimits,
    names: dict[str, str],
) -> Field:
    """One field line. ``names`` holds each type name once, so a long package name that many
    fields resolve into is one string, not one per field."""
    parts = text.split(None, 1)
    if len(parts) < 2:
        raise _Malformed("malformed", "a field line is a type and a name", line)
    head, rest = parts[0], parts[1].strip()
    kind, primitive, array, bound = _ros_type(head, package, ros2, line)
    # Syntax first, then length: a corrupt token is malformed, a well-formed long one a limit.
    # The name capped is the resolved one (``pkg/Name``), as the layout writes it.
    kind = names.setdefault(
        kind, _cap(kind, limits.max_name_bytes, "name_limit", "type name", line)
    )
    constant = _CONSTANT.fullmatch(rest)
    if constant is not None:
        if not primitive or array is not None:
            raise _Malformed("malformed", "only a primitive scalar may be a constant", line)
        name = _cap(constant["name"], limits.max_name_bytes, "name_limit", "constant name", line)
        value = constant["value"].strip()
        if kind not in {"string", "wstring"}:
            value = value.split("#", 1)[0].strip()  # a string constant keeps its '#'
        if not value:
            raise _Malformed("malformed", f"constant {_shown(name)} has no value", line)
        return Field(name, kind, True, None, bound, constant=value)
    words = _uncommented(rest).split(None, 1)
    if not words or not _NAME.fullmatch(words[0]):
        raise _Malformed("malformed", f"{_shown(rest)} is not a field name", line)
    _cap(words[0], limits.max_name_bytes, "name_limit", "field name", line)
    default = words[1].strip() if len(words) > 1 else None
    if default is not None and not ros2:
        raise _Malformed("malformed", "a ROS 1 field has no default value", line)
    return Field(words[0], kind, primitive, array, bound, default=default)


def _parse_msg(text: str, root: str, ros2: bool, limits: SchemaLimits) -> list[MessageType]:
    names: dict[str, str] = {}
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
            name = _canonical(line[4:].strip(), number)
            sections.append(
                (_cap(name, limits.max_name_bytes, "name_limit", "type name", number), [])
            )
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
            field = _ros_field(line, package, ros2, number, limits, names)
            if field.name in seen:
                raise _Malformed(
                    "malformed", f"{_shown(name)} declares {_shown(field.name)} twice", number
                )
            seen.add(field.name)
            fields.append(field)
        declared = MessageType(name, tuple(fields))
        if name in types and types[name] != declared:
            raise _Malformed("malformed", f"{_shown(name)} is defined twice, differently")
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
    if pointer == "#":
        return root
    if not pointer.startswith("#/"):
        return None
    node: object = root
    for token in pointer[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and token in node:
            node = node[token]
        elif (
            isinstance(node, list)
            and token.isascii()
            and token.isdigit()
            and len(token) <= _DIGITS
            and int(token) < len(node)
        ):
            node = node[int(token)]
        else:
            return None
    return node


_COMPOSITION: Final = ("allOf", "anyOf", "oneOf")
_REF_HOPS: Final = 32  # ``$ref`` to ``$ref`` …: a chain longer than this is left unresolved


def _json_text(value: str, what: str) -> str:
    """``value`` if it is text canonical JSON can hold: ``json.loads`` lets lone surrogates
    (``"\\ud800"``) through, which no UTF-8 line can carry."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _Malformed("invalid_utf8", f"a {what} is not valid Unicode") from None
    return value


def _is_object(schema: Mapping[str, object]) -> bool:
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if isinstance(k, str) and k != "null"), None)
    return kind == "object" or (kind is None and isinstance(schema.get("properties"), Mapping))


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
    # (type name, schema, the schema's own JSON pointer): a type is named by its pointer, the
    # root by the stream's type name
    pending: list[tuple[str, Mapping[str, object], str]] = []
    types: list[MessageType] = []
    count = 0
    # Each ``$ref`` pointer is checked and resolved once, and each schema's chain followed once:
    # thousands of properties naming one definition cost one resolution, not one each.
    resolved: dict[str, object] = {}
    targets: dict[int, tuple[Mapping[str, object] | None, str | None]] = {}

    def pointer_name(pointer: str) -> str:
        return _cap(pointer, limits.max_pointer_bytes, "pointer_limit", "JSON pointer")

    def name_of(schema: Mapping[str, object], name: str, pointer: str | None = None) -> str:
        if id(schema) not in names:
            if len(names) >= limits.max_types:
                raise _Malformed("type_limit", f"more than {limits.max_types} types")
            names[id(schema)] = pointer_name(name) if name.startswith("#") else name
            pending.append((name, schema, pointer or name))
        return names[id(schema)]

    def follow(pointer: str) -> object:
        if pointer not in resolved:
            _json_text(pointer_name(pointer), "$ref")
            resolved[pointer] = _resolve(document, pointer)
        return resolved[pointer]

    def target(schema: Mapping[str, object]) -> tuple[Mapping[str, object] | None, str | None]:
        """The schema a ``$ref`` chain ends at (or ``schema``), and the last pointer followed;
        ``None`` for a remote, missing or too long chain."""
        if not isinstance(schema.get("$ref"), str):
            return schema, None
        if id(schema) in targets:
            return targets[id(schema)]
        start, pointer = schema, None
        result: tuple[Mapping[str, object] | None, str | None] | None = None
        for _ in range(_REF_HOPS):
            ref = schema.get("$ref")
            if not isinstance(ref, str):
                result = schema, pointer
                break
            pointer = ref
            found = follow(ref)
            if not isinstance(found, Mapping):
                result = None, pointer
                break
            schema = found
        targets[id(start)] = result = result or (None, pointer)
        return result

    def field(name: str, schema: object, pointer: str, array: bool = False) -> Field:
        if not isinstance(schema, Mapping):
            return Field(name, "any", True)
        unit = schema.get("unit")
        unit = _json_text(unit, "unit") if isinstance(unit, str) else None
        resolved, ref = target(schema)
        if resolved is None:  # an unresolved reference: a type this definition does not hold
            return Field(name, ref or "any", False, unit=unit)
        if ref is not None:
            pointer = ref
            other = resolved.get("unit")
            unit = unit or (_json_text(other, "unit") if isinstance(other, str) else None)
        kind = resolved.get("type")
        if isinstance(kind, list) and kind and all(isinstance(k, str) for k in kind):
            if set(kind) <= _JSON_PRIMITIVES:
                return Field(name, "|".join(kind), True, unit=unit)
            kind = next((k for k in kind if k != "null"), None)
        if _is_object(resolved):
            return Field(name, name_of(resolved, pointer), False, unit=unit)
        if kind == "array" and not array:
            inner = field(name, resolved.get("items"), pointer + "/items", array=True)
            limit = resolved.get("maxItems")
            shape = (
                Array(ArrayKind.BOUNDED, limit)
                if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0
                else Array(ArrayKind.UNBOUNDED, None)
            )
            return Field(name, inner.type, inner.primitive, shape, unit=inner.unit or unit)
        if isinstance(kind, str) and (kind in _JSON_PRIMITIVES or kind == "array"):
            return Field(name, kind, True, unit=unit)
        if kind is None and any(key in resolved for key in _COMPOSITION):
            return Field(name, "composition", False, unit=unit)  # not parsed: unresolved
        return Field(name, "any", True, unit=unit)

    root, root_pointer = target(document)
    if root is None or not _is_object(root):
        composed = root is not None and any(key in root for key in _COMPOSITION)
        raise _Malformed(
            "composition_not_parsed" if composed else "malformed",
            "the root schema is a composition (allOf, anyOf, oneOf), which is not parsed"
            if composed
            else "the root schema is not an object schema",
        )
    name_of(root, root_name, root_pointer or "#")
    done = 0
    while done < len(pending):
        name, schema, pointer = pending[done]
        done += 1
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise _Malformed("malformed", f"{_shown(name)}'s properties are not an object")
        fields = []
        for key, value in properties.items():
            count += 1
            if count > limits.max_fields:
                raise _Malformed("field_limit", f"more than {limits.max_fields} fields")
            key = _json_text(
                _cap(key, limits.max_name_bytes, "name_limit", "property name"), "property name"
            )
            fields.append(field(key, value, f"{pointer}/properties/{_pointer_token(key)}"))
        types.append(MessageType(name, tuple(fields)))
    return types


# --- flattening ---------------------------------------------------------------------------------

_ESCAPE: Final = re.compile(r"([.\[\]\\])")


def _segment(field: Field) -> str:
    text = _ESCAPE.sub(r"\\\1", field.name)
    return text + "[]" if field.array is not None else text


# Lower bounds on the JSON a field, a type and a path add to a layout line besides their strings:
# ``{"name":,"primitive":true,"type":}``, ``{"fields":[],"name":}``, ``{"kind":,"path":,"type":}``.
# Strings are counted as written: quoted, escaped, UTF-8. The emitted line's exact size is checked
# again when it is built.
_FIELD_JSON: Final = 34
_TYPE_JSON: Final = 21
_PATH_JSON: Final = 25


def _json_bytes(text: str | None) -> int:
    """The bytes ``text`` takes as a canonical JSON string; 0 for ``None``."""
    if text is None:
        return 0
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8", "surrogatepass"))


def _types_bytes(types: Iterable[MessageType]) -> int:
    """At least the bytes ``types`` take in a layout line (names repeat there as text)."""
    total = 0
    for declared in types:
        total += _TYPE_JSON + _json_bytes(declared.name)
        for field in declared.fields:
            total += _FIELD_JSON + _json_bytes(field.name) + _json_bytes(field.type)
            for text in (field.constant, field.default, field.unit):
                total += _json_bytes(text)
    return total


def _flatten(root: str, types: Iterable[MessageType], limits: SchemaLimits) -> Layout:
    """Every path from ``root`` to a primitive, or to where flattening had to stop, in
    declaration order, depth first. At most ``max_paths`` paths (``truncated`` says more exist)
    and ``max_depth`` nested message fields; a cycle ends its path as ``recursive``. A layout
    whose types and paths pass ``max_layout_bytes`` is a ``layout_limit``: flattening stops as
    the paths reach it, so a definition never builds much more text than the limit."""
    declared = tuple(types)
    spent = _types_bytes(declared)
    counts = {"limit": limits.max_layout_bytes, "types": len(declared)}
    if spent > limits.max_layout_bytes:
        raise _Malformed(
            "layout_limit",
            f"the types alone are at least {spent} bytes, more than the"
            f" {limits.max_layout_bytes} a layout may be",
            counts={**counts, "bytes": spent, "paths": 0},
        )
    # Each type's fields that a path follows (constants dropped, segments escaped), once per type.
    members = {
        t.name: tuple((f, _segment(f)) for f in t.fields if f.constant is None) for t in declared
    }
    paths: list[FieldPath] = []
    # One frame per type being flattened: its remaining fields, path prefix, depth, ancestors.
    Frame = tuple[Iterator[tuple[Field, str]], str, int, tuple[str, ...]]
    stack: list[Frame] = [(iter(members[root]), "", 0, (root,))]
    while stack:
        fields, prefix, depth, ancestors = stack[-1]
        entry = next(fields, None)
        if entry is None:
            stack.pop()
            continue
        field, segment = entry
        path = prefix + segment
        ending: PathKind | None = None
        if field.primitive:
            ending = PathKind.PRIMITIVE
        elif field.type not in members:
            ending = PathKind.UNRESOLVED
        elif field.type in ancestors:
            ending = PathKind.RECURSIVE
        elif not members[field.type]:
            ending = PathKind.EMPTY
        elif depth >= limits.max_depth:
            ending = PathKind.DEPTH
        if ending is None:
            stack.append(
                (iter(members[field.type]), path + ".", depth + 1, (*ancestors, field.type))
            )
            continue
        if len(paths) >= limits.max_paths:
            return Layout(root, declared, tuple(paths), True)
        spent += _PATH_JSON + len(ending) + _json_bytes(path) + _json_bytes(field.type)
        spent += _json_bytes(field.unit)
        if spent > limits.max_layout_bytes:
            raise _Malformed(
                "layout_limit",
                f"the layout is at least {spent} bytes after {len(paths)} paths, more than the"
                f" {limits.max_layout_bytes} it may be",
                counts={**counts, "bytes": spent, "paths": len(paths)},
            )
        paths.append(FieldPath(path, field.type, ending, field.unit))
    return Layout(root, declared, tuple(paths), False)


def parse_definition(
    encoding: str, schema_name: str | None, data: bytes, limits: SchemaLimits
) -> Parsed:
    """Parse one definition's bytes declared in ``encoding``. Never raises on bad bytes."""
    if encoding not in PARSED_ENCODINGS:
        return Parsed(LayoutState.NOT_COVERED)
    if len(data) > limits.max_definition_bytes:
        return failed(
            problem(
                "definition_too_large",
                f"{len(data)} bytes, more than the {limits.max_definition_bytes} a definition"
                " may be",
                counts={"bytes": len(data), "limit": limits.max_definition_bytes},
            )
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return failed(problem("invalid_utf8", str(exc)))
    root = _root_name(schema_name)
    try:
        _cap(root, limits.max_name_bytes, "name_limit", "type name")
        if encoding == "jsonschema":
            types = _parse_json_schema(text, root, limits)
        else:
            types = _parse_msg(text, root, encoding == "ros2msg", limits)
        return Parsed(LayoutState.KNOWN, _flatten(root, types, limits))
    except _Malformed as exc:
        return failed(exc.problem)


# --- the records --------------------------------------------------------------------------------


def definition_layout_id(
    transform: RecordId, content: ContentId, encoding: str, root: str
) -> RecordId:
    """A definition's layout is keyed by its bytes (their content id), the encoding they are
    read in, and the root type name they are read under: the three things a parse reads."""
    return record_id(
        DEFINITION_KIND,
        {"content": content, "encoding": encoding, "root": root, "transform": transform},
    )


@dataclass(frozen=True)
class DefinitionLayout:
    """One definition's layout as a derived table line (ADR 0049 §2), ``observed``, written once
    however many streams declare the definition.

    ``content`` is the content id of the definition's bytes: the layout is a decoding of exactly
    those bytes. ``definition`` cites where they sit: the citation of the first stream, by id,
    that declares them (every citation of the same content holds the same bytes). Each
    ``stream_layout`` naming this line cites its own stream's copy as well.
    """

    kind: ClassVar[str] = DEFINITION_KIND
    id: RecordId
    transform: RecordId
    content: ContentId
    encoding: str
    definition: EvidenceRef
    layout: Layout

    def __post_init__(self) -> None:
        expected = definition_layout_id(
            self.transform, self.content, self.encoding, self.layout.root
        )
        if self.id != expected:
            raise ValueError(f"{self.id} is not the id of this definition's layout")

    @property
    def assertion_kind(self) -> str:
        return OBSERVED

    @property
    def paths(self) -> tuple[FieldPath, ...]:
        return self.layout.paths

    def to_json(self) -> JsonObject:
        layout = self.layout
        return {
            "assertion_kind": OBSERVED,
            "content": self.content,
            "definition": self.definition.to_json(),
            "encoding": self.encoding,
            "id": self.id,
            "kind": DEFINITION_KIND,
            "paths": [p.to_json() for p in layout.paths],
            "root": layout.root,
            "schema_version": DERIVED_SCHEMA_VERSION,
            "transform": self.transform,
            "truncated": layout.truncated,
            "types": [t.to_json() for t in layout.types],
        }


def definition_layout_from_json(data: JsonValue) -> DefinitionLayout:
    """Parse strictly; the id must recompute from the content, encoding, root and transform."""
    keys = {
        "content",
        "definition",
        "encoding",
        "id",
        "paths",
        "root",
        "transform",
        "truncated",
        "types",
    }
    obj = derived_object(data, DEFINITION_KIND, keys, frozenset({OBSERVED}))
    return DefinitionLayout(
        id=parse_record_id(json_str(obj["id"], "id")),
        transform=parse_record_id(json_str(obj["transform"], "transform")),
        content=parse_content_id(json_str(obj["content"], "content")),
        encoding=json_str(obj["encoding"], "encoding"),
        definition=evidence_ref_from_json(obj["definition"]),
        layout=Layout(
            json_str(obj["root"], "root"),
            tuple(_type_from_json(t) for t in json_array(obj["types"], "types")),
            tuple(_path_from_json(p) for p in json_array(obj["paths"], "paths")),
            json_bool(obj["truncated"]),
        ),
    )


def layout_id(transform: RecordId, stream: RecordId) -> RecordId:
    return record_id(LAYOUT_KIND, {"stream": stream, "transform": transform})


@dataclass(frozen=True)
class StreamLayout:
    """A stream's declared layout as a derived table line (ADR 0049 §2), ``observed``.

    ``definition`` is the bytes it was read from (the stream's ``schema_definition``), its
    provenance. ``schema_name`` and ``schema_encoding`` repeat the stream's as declared (``None``
    where the stream's is not ``Known``). ``layout`` is the ``definition_layout`` line the bytes
    parse to, present exactly when ``state`` is ``known``; ``problem`` says why the layout is
    ``unknown``, or ``not_covered`` past a limit.
    """

    kind: ClassVar[str] = LAYOUT_KIND
    id: RecordId
    transform: RecordId
    stream: RecordId
    schema_name: str | None
    schema_encoding: str | None
    definition: EvidenceRef | None
    state: LayoutState
    layout: RecordId | None
    problem: Problem | None

    def __post_init__(self) -> None:
        if self.id != layout_id(self.transform, self.stream):
            raise ValueError(f"{self.id} is not the id of this stream's layout")
        if (self.layout is not None) != (self.state is LayoutState.KNOWN):
            raise ValueError("a layout is present exactly when its state is known")
        if self.layout is not None and self.definition is None:
            raise ValueError("a known layout cites the definition it was read from")
        if self.problem is not None and self.state not in _WITH_PROBLEM:
            raise ValueError("only an unknown or not covered layout has a problem")
        if self.state is LayoutState.UNKNOWN and self.problem is None:
            raise ValueError("an unknown layout says why")

    @property
    def assertion_kind(self) -> str:
        return OBSERVED

    def to_json(self) -> JsonObject:
        return _present(
            {
                "assertion_kind": OBSERVED,
                "definition": None if self.definition is None else self.definition.to_json(),
                "id": self.id,
                "kind": LAYOUT_KIND,
                "layout": self.layout,
                "problem": None if self.problem is None else self.problem.to_json(),
                "schema_encoding": self.schema_encoding,
                "schema_name": self.schema_name,
                "schema_version": DERIVED_SCHEMA_VERSION,
                "state": str(self.state),
                "stream": self.stream,
                "transform": self.transform,
            }
        )


_WITH_PROBLEM: Final = frozenset({LayoutState.UNKNOWN, LayoutState.NOT_COVERED})
_LAYOUT_REQUIRED: Final = {"id", "state", "stream", "transform"}
_LAYOUT_OPTIONAL: Final = {
    "definition",
    "layout",
    "problem",
    "schema_encoding",
    "schema_name",
}


def stream_layout_from_json(data: JsonValue) -> StreamLayout:
    """Parse strictly; the id must recompute from the stream and transform."""
    present = set(data) & _LAYOUT_OPTIONAL if isinstance(data, Mapping) else set()
    obj = derived_object(data, LAYOUT_KIND, _LAYOUT_REQUIRED | present, frozenset({OBSERVED}))
    return StreamLayout(
        id=parse_record_id(json_str(obj["id"], "id")),
        transform=parse_record_id(json_str(obj["transform"], "transform")),
        stream=parse_record_id(json_str(obj["stream"], "stream")),
        schema_name=_optional_str(obj, "schema_name"),
        schema_encoding=_optional_str(obj, "schema_encoding"),
        definition=evidence_ref_from_json(obj["definition"]) if "definition" in obj else None,
        state=LayoutState(json_str(obj["state"], "state")),
        layout=parse_record_id(json_str(obj["layout"], "layout")) if "layout" in obj else None,
        problem=_problem_from_json(obj["problem"]) if "problem" in obj else None,
    )

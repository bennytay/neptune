"""Message definitions as a stream declares them: ROS ``.msg`` text and ROS 2 IDL (ADR 0068 §1).

A definition is parsed into the types it declares, each a list of fields, so a decoder can walk a
payload. Nothing is looked up: a type the text does not define is an error, never filled in from
a bundled copy, because the definition the stream declares is the only one that says how its
bytes are laid out.

- ``ros1msg`` and ``ros2msg``: the root type's fields, then each dependency after a line of
  ``=`` and a ``MSG: <name>`` line, as MCAP, rosbag1 and rosbag2 write them. A name without a
  package resolves in the package of the type that uses it; ROS 1's ``Header`` is
  ``std_msgs/Header``; ``pkg/msg/Name`` and ``pkg/Name`` are one type. Constants are dropped
  (they take no bytes); ROS 2 defaults are ignored (the payload holds every field).
- ``ros2idl``: each type after a line of ``=`` and an ``IDL: <name>`` line (MCAP's layout of the
  IDL rosidl generates): modules, structs, typedefs (with array declarators), sequences, bounded
  strings and the IDL primitive names. Annotations, constants and preprocessor lines are skipped;
  enums, unions, ``long double``, ``wchar`` and multi-dimensional arrays are refused.

Every parser is bounded by ``Limits``: the definition's bytes, its types and fields, and the
length of a name. A definition past one raises ``DefinitionError`` with a reason the adapter
reports; so does anything malformed. Nothing here raises anything else on hostile text.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# ROS primitive names as a definition writes them, mapped to the wire type each is encoded as.
# ROS 1's ``byte`` is a signed 8-bit integer and ``char`` an unsigned one (both deprecated
# aliases); ROS 2's ``byte`` is an octet and ``char`` an unsigned 8-bit integer.
_ROS1: Final = {
    "bool": "bool",
    "int8": "int8",
    "uint8": "uint8",
    "byte": "int8",
    "char": "uint8",
    "int16": "int16",
    "uint16": "uint16",
    "int32": "int32",
    "uint32": "uint32",
    "int64": "int64",
    "uint64": "uint64",
    "float32": "float32",
    "float64": "float64",
    "string": "string",
    "time": "time",
    "duration": "duration",
}
_ROS2: Final = {
    "bool": "bool",
    "byte": "uint8",
    "char": "uint8",
    "int8": "int8",
    "uint8": "uint8",
    "int16": "int16",
    "uint16": "uint16",
    "int32": "int32",
    "uint32": "uint32",
    "int64": "int64",
    "uint64": "uint64",
    "float32": "float32",
    "float64": "float64",
    "string": "string",
    "wstring": "wstring",
}
_IDL: Final = {
    "boolean": "bool",
    "octet": "uint8",
    "char": "uint8",
    "int8": "int8",
    "uint8": "uint8",
    "int16": "int16",
    "uint16": "uint16",
    "int32": "int32",
    "uint32": "uint32",
    "int64": "int64",
    "uint64": "uint64",
    "short": "int16",
    "unsigned short": "uint16",
    "long": "int32",
    "unsigned long": "uint32",
    "long long": "int64",
    "unsigned long long": "uint64",
    "float": "float32",
    "double": "float64",
    "string": "string",
    "wstring": "wstring",
}
# The declared names whose arrays are bytes: blobs (an image, a point cloud) the row cites whole.
BYTE_NAMES: Final = frozenset({"uint8", "byte", "char", "octet"})
ENCODINGS: Final = frozenset({"ros1msg", "ros2msg", "ros2idl"})

_NAME: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_SEPARATOR: Final = re.compile(r"={3,}")
_TYPE: Final = re.compile(
    r"(?P<base>[A-Za-z][A-Za-z0-9_]*(?:/[A-Za-z][A-Za-z0-9_]*){0,2})"
    r"(?:<=(?P<bound>[0-9]+))?"
    r"(?:\[(?P<array>(?:<=)?[0-9]*)\])?"
)
_CONSTANT: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]*\s*=")
_DIGITS: Final = 12  # a length beyond 10**12 is not one any payload holds


class ArrayKind(StrEnum):
    FIXED = "fixed"  # ``T[3]``: no count on the wire
    BOUNDED = "bounded"  # ``T[<=3]``: a count, at most the bound
    UNBOUNDED = "unbounded"  # ``T[]``: a count


@dataclass(frozen=True)
class FieldDef:
    """One field. ``wire`` is the primitive's wire type (``float64``, ``string``, ``time``) or
    ``None`` for a message type, then named by ``type`` (``pkg/Name``); ``declared`` is the
    primitive's name as written (``byte``, ``octet``). ``length`` is a fixed array's length or a
    bounded one's bound; ``bound`` a bounded string's."""

    name: str
    type: str
    wire: str | None
    declared: str
    array: ArrayKind | None = None
    length: int | None = None
    bound: int | None = None


@dataclass(frozen=True)
class MessageDef:
    name: str
    fields: tuple[FieldDef, ...]


@dataclass(frozen=True)
class Definition:
    """The root type's name and every type the text declares, by name."""

    encoding: str
    root: str
    types: dict[str, MessageDef]

    @property
    def root_type(self) -> MessageDef:
        return self.types[self.root]


@dataclass(frozen=True)
class Limits:
    max_definition_bytes: int = 1 << 20
    max_types: int = 1024
    max_fields: int = 16384
    max_name_bytes: int = 1024


class DefinitionError(ValueError):
    """The definition cannot be read: ``reason`` is ``malformed``, ``unsupported`` or a limit
    (``too_large``, ``type_limit``, ``field_limit``, ``name_limit``)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _shown(text: str) -> str:
    return repr(text if len(text) <= 64 else text[:64] + "…")


def canonical_name(name: str) -> str | None:
    """``pkg/Name`` for ``pkg/msg/Name`` or ``pkg/Name``; ``None`` for anything else."""
    parts = name.split("/")
    if len(parts) == 3 and parts[1] == "msg":
        parts = [parts[0], parts[2]]
    if len(parts) != 2 or not all(_NAME.fullmatch(part) for part in parts):
        return None
    return "/".join(parts)


def parse_definition(
    data: bytes, encoding: str, schema_name: str, limits: Limits | None = None
) -> Definition:
    """The types ``data`` declares, in ``encoding``, rooted at ``schema_name``."""
    limits = limits or Limits()
    if encoding not in ENCODINGS:
        raise DefinitionError("unsupported", f"definitions in {encoding!r} are not read")
    if len(data) > limits.max_definition_bytes:
        raise DefinitionError(
            "too_large",
            f"the definition is {len(data)} bytes, more than {limits.max_definition_bytes}",
        )
    root = canonical_name(schema_name)
    if root is None:
        raise DefinitionError("malformed", f"{_shown(schema_name)} is not a message type name")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DefinitionError("malformed", "the definition is not UTF-8") from exc
    if encoding == "ros2idl":
        types = _parse_idl(text, limits)
    else:
        types = _parse_msg(text, root, encoding == "ros2msg", limits)
    if root not in types:
        raise DefinitionError("malformed", f"the definition does not declare {root}")
    for message in types.values():
        for field in message.fields:
            if field.wire is None and field.type not in types:
                raise DefinitionError(
                    "malformed", f"{message.name} uses {field.type}, which it does not define"
                )
    return Definition(encoding, root, types)


def _cap(text: str, limits: Limits, what: str) -> str:
    if len(text.encode()) > limits.max_name_bytes:
        raise DefinitionError("name_limit", f"a {what} is longer than {limits.max_name_bytes}")
    return text


def _number(text: str) -> int:
    if not text.isdigit() or len(text) > _DIGITS:
        raise DefinitionError("malformed", f"{_shown(text)} is not a length")
    return int(text)


# --- .msg ---------------------------------------------------------------------------------------


def _uncommented(text: str) -> str:
    """``text`` up to its first ``#`` outside a quoted default."""
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


def _msg_field(line: str, package: str, ros2: bool, limits: Limits) -> FieldDef | None:
    """One line's field, or ``None`` for a constant."""
    parts = line.split(None, 1)
    if len(parts) < 2:
        raise DefinitionError("malformed", f"{_shown(line)} is not a type and a name")
    token, rest = parts[0], parts[1].strip()
    match = _TYPE.fullmatch(token)
    if match is None:
        raise DefinitionError("malformed", f"{_shown(token)} is not a field type")
    if _CONSTANT.match(rest):
        return None  # a constant takes no bytes on the wire
    words = _uncommented(rest).split()
    if not words or not _NAME.fullmatch(words[0]):
        raise DefinitionError("malformed", f"{_shown(rest)} is not a field name")
    if len(words) > 1 and not ros2:
        raise DefinitionError("malformed", "a ROS 1 field has no default value")
    name = _cap(words[0], limits, "field name")
    base, bound_text, array_text = match["base"], match["bound"], match["array"]
    primitives = _ROS2 if ros2 else _ROS1
    bound = None
    if bound_text is not None:
        if not ros2 or base not in ("string", "wstring"):
            raise DefinitionError("malformed", f"only a ROS 2 string has a bound: {token}")
        bound = _number(bound_text)
    array, length = None, None
    if array_text is not None:
        if array_text == "":
            array = ArrayKind.UNBOUNDED
        elif array_text.startswith("<="):
            if not ros2:
                raise DefinitionError("malformed", f"{_shown(token)} is not a ROS 1 array type")
            array, length = ArrayKind.BOUNDED, _number(array_text[2:])
        else:
            array, length = ArrayKind.FIXED, _number(array_text)
    if base in primitives:
        return FieldDef(name, base, primitives[base], base, array, length, bound)
    if not ros2 and base == "Header":
        kind = "std_msgs/Header"
    elif "/" not in base:
        kind = f"{package}/{base}"
    else:
        found = canonical_name(base)
        if found is None:
            raise DefinitionError("malformed", f"{_shown(base)} is not a message type name")
        kind = found
    return FieldDef(name, _cap(kind, limits, "type name"), None, base, array, length, bound)


def _parse_msg(text: str, root: str, ros2: bool, limits: Limits) -> dict[str, MessageDef]:
    sections: list[tuple[str, list[str]]] = [(root, [])]
    expect_name = False
    for raw in text.splitlines():
        line = raw.strip()
        if _SEPARATOR.fullmatch(line):
            expect_name = True
            continue
        if not line or line.startswith("#"):
            continue
        if expect_name:
            if not line.startswith("MSG:"):
                raise DefinitionError("malformed", "a dependency starts with 'MSG: <name>'")
            name = canonical_name(line[4:].strip())
            if name is None:
                raise DefinitionError("malformed", f"{_shown(line)} names no message type")
            sections.append((_cap(name, limits, "type name"), []))
            if len(sections) > limits.max_types:
                raise DefinitionError("type_limit", f"more than {limits.max_types} types")
            expect_name = False
            continue
        sections[-1][1].append(line)
    if expect_name:
        raise DefinitionError("malformed", "a separator is not followed by 'MSG: <name>'")
    types: dict[str, MessageDef] = {}
    count = 0
    for name, lines in sections:
        package = name.split("/", 1)[0]
        fields: list[FieldDef] = []
        for line in lines:
            count += 1
            if count > limits.max_fields:
                raise DefinitionError("field_limit", f"more than {limits.max_fields} fields")
            field = _msg_field(line, package, ros2, limits)
            if field is None:
                continue
            if any(f.name == field.name for f in fields):
                raise DefinitionError("malformed", f"{name} declares {field.name} twice")
            fields.append(field)
        declared = MessageDef(name, tuple(fields))
        if name in types and types[name] != declared:
            raise DefinitionError("malformed", f"{name} is defined twice, differently")
        types.setdefault(name, declared)
    return types


# --- IDL ----------------------------------------------------------------------------------------

_TOKEN: Final = re.compile(
    r"""\s+|//[^\n]*|/\*.*?\*/|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'"""
    r"""|::|[A-Za-z_][A-Za-z0-9_]*|[0-9][0-9A-Za-z_.+-]*|[{}()<>\[\];,=@:+\-*/|&^~%.]""",
    re.DOTALL,
)
_SKIPPED: Final = re.compile(r"\s+|//[^\n]*|/\*.*?\*/", re.DOTALL)


def _tokens(text: str) -> list[str]:
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    body = "\n".join(lines)
    found: list[str] = []
    at = 0
    while at < len(body):
        match = _TOKEN.match(body, at)
        if match is None:
            raise DefinitionError("malformed", f"unexpected text {_shown(body[at : at + 16])}")
        at = match.end()
        if not _SKIPPED.fullmatch(match.group()):
            found.append(match.group())
    return found


class _Idl:
    """A recursive-descent reader over one section's tokens; nesting is bounded by its depth."""

    MAX_DEPTH: Final = 32

    def __init__(self, tokens: list[str], limits: Limits, types: dict[str, MessageDef]) -> None:
        self.tokens = tokens
        self.at = 0
        self.limits = limits
        self.types = types
        self.typedefs: dict[str, FieldDef] = {}
        self.fields = 0

    def peek(self, ahead: int = 0) -> str | None:
        at = self.at + ahead
        return self.tokens[at] if at < len(self.tokens) else None

    def take(self, expected: str | None = None) -> str:
        token = self.peek()
        if token is None or (expected is not None and token != expected):
            raise DefinitionError("malformed", f"expected {expected or 'more'}, found {token!r}")
        self.at += 1
        return token

    def name(self) -> str:
        token = self.take()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
            raise DefinitionError("malformed", f"{_shown(token)} is not a name")
        return _cap(token, self.limits, "name")

    def skip_annotations(self) -> None:
        while self.peek() == "@":
            self.take("@")
            self.name()
            if self.peek() == "(":
                self.balanced("(", ")")

    def balanced(self, opening: str, closing: str) -> None:
        depth = 0
        while True:
            token = self.take()
            if token == opening:
                depth += 1
            elif token == closing:
                depth -= 1
                if depth == 0:
                    return

    def skip_to_semicolon(self) -> None:
        while self.take() != ";":
            pass

    def definitions(self, scope: tuple[str, ...], depth: int) -> None:
        if depth > self.MAX_DEPTH:
            raise DefinitionError("malformed", "modules nest too deeply")
        while (token := self.peek()) is not None and token != "}":
            self.skip_annotations()
            token = self.peek()
            if token is None or token == "}":
                break
            if token == "module":
                self.take()
                name = self.name()
                self.take("{")
                self.definitions((*scope, name), depth + 1)
                self.take("}")
                self.take(";")
            elif token == "struct":
                self.struct(scope)
            elif token == "typedef":
                self.typedef(scope)
            elif token == "const":
                self.skip_to_semicolon()
            elif token in ("enum", "union"):
                raise DefinitionError("unsupported", f"an IDL {token} is not read")
            else:
                raise DefinitionError("malformed", f"unexpected {_shown(token)}")

    def type_spec(self, scope: tuple[str, ...], depth: int = 0) -> FieldDef:
        """A type as a field without a name: primitive, string, sequence or scoped name."""
        if depth > self.MAX_DEPTH:
            raise DefinitionError("malformed", "sequences nest too deeply")
        token = self.take()
        if token == "sequence":
            self.take("<")
            element = self.type_spec(scope, depth + 1)
            bound = None
            if self.peek() == ",":
                self.take(",")
                bound = _number(self.take())
            self.take(">")
            if element.array is not None:
                raise DefinitionError("unsupported", "a sequence of arrays is not read")
            kind = ArrayKind.UNBOUNDED if bound is None else ArrayKind.BOUNDED
            return FieldDef("", element.type, element.wire, element.declared, kind, bound)
        if token in ("string", "wstring"):
            bound = None
            if self.peek() == "<":
                self.take("<")
                bound = _number(self.take())
                self.take(">")
            return FieldDef("", token, token, token, None, None, bound)
        if token == "unsigned":
            words = [token, self.take()]
            if words[1] == "long" and self.peek() == "long":
                words.append(self.take())
            token = " ".join(words)
        elif token == "long" and self.peek() in ("long", "double"):
            token = f"long {self.take()}"
        if token in _IDL:
            return FieldDef("", token, _IDL[token], token)
        if token in ("long double", "wchar", "fixed", "any"):
            raise DefinitionError("unsupported", f"IDL {token} is not read")
        parts = [] if token == "::" else [token]
        if token == "::":
            parts.append(self.name())
        while self.peek() == "::":
            self.take("::")
            parts.append(self.name())
        if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts):
            raise DefinitionError("malformed", f"{_shown('::'.join(parts))} is not a type")
        return self.resolve(parts, scope)

    def resolve(self, parts: list[str], scope: tuple[str, ...]) -> FieldDef:
        """A scoped name: a typedef, searched from the innermost scope outward, else a struct.

        A struct may be defined by a later section, so it is named, not looked up: the first
        scope, innermost first, under which the name reads as ``pkg::msg::Name``. Whether some
        section defines it is checked once every section is read."""
        for depth in range(len(scope), -1, -1):
            full = "::".join((*scope[:depth], *parts))
            if full in self.typedefs:
                return self.typedefs[full]
        for depth in range(len(scope), -1, -1):
            message = _idl_message_name("::".join((*scope[:depth], *parts)))
            if message is not None:
                return FieldDef("", message, None, message)
        raise DefinitionError("malformed", f"{_shown('::'.join(parts))} names no type")

    def declarator(self, base: FieldDef) -> FieldDef:
        name = self.name()
        if self.peek() != "[":
            return FieldDef(
                name, base.type, base.wire, base.declared, base.array, base.length, base.bound
            )
        self.take("[")
        length = _number(self.take())
        self.take("]")
        if self.peek() == "[" or base.array is not None:
            raise DefinitionError("unsupported", "a multi-dimensional array is not read")
        return FieldDef(
            name, base.type, base.wire, base.declared, ArrayKind.FIXED, length, base.bound
        )

    def typedef(self, scope: tuple[str, ...]) -> None:
        self.take("typedef")
        base = self.type_spec(scope)
        while True:
            alias = self.declarator(base)
            self.typedefs["::".join((*scope, alias.name))] = alias
            if self.peek() != ",":
                break
            self.take(",")
        self.take(";")

    def struct(self, scope: tuple[str, ...]) -> None:
        self.take("struct")
        name = self.name()
        if self.peek() == ";":  # a forward declaration
            self.take(";")
            return
        self.take("{")
        fields: list[FieldDef] = []
        while self.peek() != "}":
            self.skip_annotations()
            if self.peek() == "}":
                break
            base = self.type_spec((*scope, name))
            while True:
                field = self.declarator(base)
                self.fields += 1
                if self.fields > self.limits.max_fields:
                    raise DefinitionError(
                        "field_limit", f"more than {self.limits.max_fields} fields"
                    )
                if any(f.name == field.name for f in fields):
                    raise DefinitionError("malformed", f"{name} declares {field.name} twice")
                fields.append(field)
                if self.peek() != ",":
                    break
                self.take(",")
            self.take(";")
        self.take("}")
        self.take(";")
        full = _idl_message_name("::".join((*scope, name)))
        if full is None:
            raise DefinitionError("malformed", f"{_shown(name)} is not in a package's module")
        declared = MessageDef(full, tuple(fields))
        if full in self.types and self.types[full] != declared:
            raise DefinitionError("malformed", f"{full} is defined twice, differently")
        self.types[full] = declared
        if len(self.types) > self.limits.max_types:
            raise DefinitionError("type_limit", f"more than {self.limits.max_types} types")


def _idl_message_name(scoped: str) -> str | None:
    """``pkg/Name`` for ``pkg::msg::Name`` (or ``pkg::Name``)."""
    return canonical_name(scoped.replace("::", "/"))


def _parse_idl(text: str, limits: Limits) -> dict[str, MessageDef]:
    sections: list[list[str]] = [[]]
    for raw in text.splitlines():
        line = raw.strip()
        if _SEPARATOR.fullmatch(line):
            sections.append([])
            continue
        if line.startswith("IDL:") and not any(text.strip() for text in sections[-1]):
            continue
        sections[-1].append(raw)
    types: dict[str, MessageDef] = {}
    for lines in sections:
        reader = _Idl(_tokens("\n".join(lines)), limits, types)
        reader.definitions((), 0)
        if reader.peek() is not None:
            raise DefinitionError("malformed", f"unexpected {reader.peek()!r} at the top level")
    return types

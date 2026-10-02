"""Reading a manifest's bytes into a tree, as hostile input (ADR 0047 §2).

A manifest is a file the user writes, so it is read like any other untrusted bytes: bounded in
size, depth and node count, and refused whole at the first thing outside the grammar, with the
line (YAML) or JSON pointer that says where. Nothing here raises anything but ``ManifestError``.

Two syntaxes give the same tree:

- **JSON** (a ``.json`` manifest), strict: no duplicate keys, no ``NaN`` or ``Infinity``. A
  number or boolean keeps its literal too, so text fields read it exactly as YAML does.
- **A strict YAML subset** (anything else): block mappings and sequences indented with spaces,
  plain, single- and double-quoted scalars, flow collections (``[a, b]``, ``{path: x}``),
  ``#`` comments and one leading ``---``. Refused, each by name: anchors and aliases (``&``,
  ``*``: no alias bombs), tags (``!``), directives (``%``), several documents, block scalars
  (``|``, ``>``), complex keys (``?``), tabs in indentation, duplicate keys, and plain values that
  continue on the next line. A YAML 1.2 parser reads what this one accepts as the same tree.

A plain YAML scalar keeps the text as written beside its YAML 1.2 core-schema value, so a field
that wants text reads ``version: 1.10`` as ``"1.10"``, never as the number ``1.1``. The core
schema's other forms (``.inf``, ``.nan``, ``0o17``, ``0x1F``) and numbers no float can hold
(``1e999``) are refused with a request to quote them: YAML parsers disagree on them, and none is a
value a manifest needs.
"""

import json
import math
import re
from dataclasses import dataclass
from typing import Final, TypeAlias

MAX_BYTES: Final = 256 * 1024
MAX_DEPTH: Final = 16
MAX_NODES: Final = 20_000
MAX_SCALAR: Final = 4096

ScalarValue: TypeAlias = str | int | float | bool | None

_INT: Final = re.compile(r"[-+]?[0-9]+")
_FLOAT: Final = re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?")
# YAML 1.2 core-schema forms this reader does not resolve: refused, so no parser reads them apart.
_SPECIAL: Final = re.compile(r"[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)|0o[0-7]+|0x[0-9a-fA-F]+")
_TRUE: Final = frozenset({"true", "True", "TRUE"})
_FALSE: Final = frozenset({"false", "False", "FALSE"})
_NULL: Final = frozenset({"null", "Null", "NULL", "~"})
# Characters that cannot start a plain scalar (YAML 1.2 §7.3.3): each means something we refuse.
_INDICATORS: Final = {
    "&": "anchors (&) are not supported",
    "*": "aliases (*) are not supported",
    "!": "tags (!) are not supported",
    "|": "block scalars (|) are not supported; write the value quoted on one line",
    ">": "block scalars (>) are not supported; write the value quoted on one line",
    "%": "directives (%) are not supported",
    "?": "complex keys (?) are not supported",
    "@": "'@' is reserved in YAML; quote the value",
    "`": "'`' is reserved in YAML; quote the value",
}
_ESCAPES: Final = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}
_HEX_ESCAPES: Final = {"x": 2, "u": 4, "U": 8}


class ManifestError(ValueError):
    """A manifest that cannot be used; the message says where (line or pointer) and why."""


@dataclass(frozen=True)
class Scalar:
    """A scalar: its value, and for a plain YAML scalar the text as written (else ``None``)."""

    value: ScalarValue
    text: str | None
    line: int | None


@dataclass(frozen=True)
class Seq:
    items: tuple["Node", ...]
    line: int | None


@dataclass(frozen=True)
class Map:
    items: tuple[tuple[str, "Node"], ...]
    line: int | None


Node: TypeAlias = Scalar | Seq | Map


def read_tree(data: bytes, *, json_syntax: bool) -> Node:
    """``data`` as a tree; ``ManifestError`` for anything outside the limits or the grammar."""
    if len(data) > MAX_BYTES:
        raise ManifestError(f"the manifest is larger than {MAX_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError(f"byte {exc.start} is not UTF-8") from None
    text = text.removeprefix("﻿")
    for index, char in enumerate(text):
        if (ord(char) < 0x20 and char not in "\t\n\r") or char == "\x7f":
            line = text.count("\n", 0, index) + 1
            raise ManifestError(f"line {line}: control character U+{ord(char):04X}")
    budget = _Budget()
    if json_syntax:
        return _read_json(text, budget)
    return _Yaml(text, budget).document()


class _Budget:
    """Counts nodes so no document, however it nests or repeats, builds more than the cap."""

    def __init__(self) -> None:
        self.nodes = 0

    def take(self, line: int | None) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            where = f"line {line}: " if line is not None else ""
            raise ManifestError(f"{where}more than {MAX_NODES} values in the manifest")


def _depth(depth: int, line: int | None) -> None:
    if depth > MAX_DEPTH:
        where = f"line {line}: " if line is not None else ""
        raise ManifestError(f"{where}nested more than {MAX_DEPTH} levels deep")


def _scalar_text(text: str, line: int | None) -> str:
    if len(text) > MAX_SCALAR:
        where = f"line {line}: " if line is not None else ""
        raise ManifestError(f"{where}a value is longer than {MAX_SCALAR} characters")
    return text


# --- JSON ------------------------------------------------------------------------------------


def _read_json(text: str, budget: _Budget) -> Node:
    depth = deepest = 0
    in_string = escaped = False
    for char in text:  # bound the depth before json recurses into it
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
            deepest = max(deepest, depth)
        elif char in "]}":
            depth -= 1
    _depth(deepest, None)

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        keys = [key for key, _ in items]
        if len(set(keys)) != len(keys):
            twice = sorted({key for key in keys if keys.count(key) > 1})
            raise ManifestError(f"duplicate keys {twice}")
        return dict(items)

    def constant(name: str) -> object:
        raise ManifestError(f"{name} is not a JSON number")

    try:
        value = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_int=lambda literal: _Number(int(literal), literal),
            parse_float=lambda literal: _Number(float(literal), literal),
        )
    except ManifestError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ManifestError(f"not JSON: {exc}") from None
    return _from_json(value, budget)


def _unicode(text: str) -> str:
    """JSON escapes can spell a lone surrogate, which is no character: refused."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ManifestError(
            "a JSON string escapes a lone surrogate, which is not a character"
        ) from None
    return text


@dataclass(frozen=True)
class _Number:
    """A JSON number with its literal, so a text field reads ``1.10`` as written, as in YAML."""

    value: int | float
    literal: str


def _from_json(value: object, budget: _Budget) -> Node:
    budget.take(None)
    if isinstance(value, _Number):
        number = _finite(value.value, None) if isinstance(value.value, float) else value.value
        return Scalar(number, _scalar_text(value.literal, None), None)
    if isinstance(value, bool):
        return Scalar(value, "true" if value else "false", None)
    if isinstance(value, dict):
        items = tuple((_unicode(key), _from_json(item, budget)) for key, item in value.items())
        return Map(items, None)
    if isinstance(value, list):
        return Seq(tuple(_from_json(item, budget) for item in value), None)
    if isinstance(value, str):
        return Scalar(_scalar_text(_unicode(value), None), None, None)
    if value is None:
        return Scalar(None, None, None)
    raise ManifestError(f"not a JSON value: {value!r}")  # pragma: no cover - json gives no other


# --- The YAML subset ---------------------------------------------------------------------------


@dataclass
class _Line:
    number: int
    indent: int
    content: str  # without indentation, comment or trailing spaces


def _resolve(text: str, line: int | None) -> ScalarValue:
    """A plain scalar's YAML 1.2 core-schema value."""
    if _SPECIAL.fullmatch(text):
        raise ManifestError(
            f"line {line}: {text!r} means different things to different YAML parsers; quote it"
        )
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    if text in _NULL:
        return None
    if _INT.fullmatch(text):
        return int(text)
    if _FLOAT.fullmatch(text):
        return _finite(float(text), line)
    return text


def _finite(value: float, line: int | None) -> float:
    if not math.isfinite(value):
        where = f"line {line}: " if line is not None else ""
        raise ManifestError(f"{where}a number too large for a float; quote it if it is text")
    return value


def _is_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


class _Yaml:
    def __init__(self, text: str, budget: _Budget) -> None:
        self.budget = budget
        self.lines: list[_Line] = []
        for number, raw in enumerate(text.split("\n"), start=1):
            content = _strip_comment(raw.removesuffix("\r"), number)
            stripped = content.lstrip(" ")
            if not stripped.strip():
                continue
            if stripped.startswith("\t"):
                raise ManifestError(f"line {number}: a tab in indentation; indent with spaces")
            self.lines.append(_Line(number, len(content) - len(stripped), stripped.rstrip(" \t")))
        if self.lines and self.lines[0].indent == 0 and self.lines[0].content == "---":
            self.lines.pop(0)
        for line in self.lines:
            if line.indent == 0 and line.content in ("---", "..."):
                raise ManifestError(f"line {line.number}: a manifest is one YAML document")
            if line.content.startswith("%"):
                raise ManifestError(f"line {line.number}: directives (%) are not supported")
        self.at = 0

    def peek(self) -> _Line | None:
        return self.lines[self.at] if self.at < len(self.lines) else None

    def document(self) -> Node:
        first = self.peek()
        if first is None:
            return Map((), None)  # an empty manifest: its schema says what is missing
        node = self.block(first.indent, 1)
        if (left := self.peek()) is not None:
            raise ManifestError(f"line {left.number}: unexpected indentation")
        return node

    def block(self, indent: int, depth: int) -> Node:
        line = self.peek()
        assert line is not None
        _depth(depth, line.number)
        if _is_item(line.content):
            return self.sequence(indent, depth)
        return self.mapping(indent, depth)

    def nested(self, indent: int, depth: int, line: int, *, allow_item: bool) -> Node:
        """The value of a key or item with nothing after it: a deeper block, or null."""
        following = self.peek()
        if following is not None and following.indent > indent:
            return self.block(following.indent, depth)
        if (
            allow_item
            and following is not None
            and following.indent == indent
            and _is_item(following.content)  # "key:" then "- item" at the key's indentation
        ):
            return self.sequence(indent, depth)
        self.budget.take(line)
        return Scalar(None, "", line)

    def sequence(self, indent: int, depth: int) -> Seq:
        start = self.peek()
        assert start is not None
        self.budget.take(start.number)
        items: list[Node] = []
        while (line := self.peek()) is not None and line.indent == indent:
            if not _is_item(line.content):
                break
            self.at += 1
            rest = line.content[1:].lstrip(" ")
            inner = indent + len(line.content) - len(rest)
            if not rest:
                items.append(self.nested(indent, depth + 1, line.number, allow_item=False))
            elif _is_item(rest) or _key_split(rest, line.number) is not None:
                self.lines.insert(self.at, _Line(line.number, inner, rest))  # "- key: v" or "- - v"
                items.append(self.block(inner, depth + 1))
            else:
                items.append(self.inline(rest, line.number, depth + 1))
        self.dedented(indent)
        return Seq(tuple(items), start.number)

    def mapping(self, indent: int, depth: int) -> Map:
        start = self.peek()
        assert start is not None
        self.budget.take(start.number)
        items: list[tuple[str, Node]] = []
        seen: set[str] = set()
        while (line := self.peek()) is not None and line.indent == indent:
            if _is_item(line.content):
                raise ManifestError(f"line {line.number}: a list item where a key was expected")
            self.at += 1
            split = _key_split(line.content, line.number)
            if split is None:
                raise ManifestError(
                    f"line {line.number}: expected 'key: value' (a value that continues on the"
                    " next line must be quoted on one line)"
                )
            key, rest = split
            if key in seen:
                raise ManifestError(f"line {line.number}: duplicate key {key!r}")
            seen.add(key)
            if rest:
                value = self.inline(rest, line.number, depth + 1)
            else:
                value = self.nested(indent, depth + 1, line.number, allow_item=True)
            items.append((key, value))
        self.dedented(indent)
        return Map(tuple(items), start.number)

    def dedented(self, indent: int) -> None:
        if (line := self.peek()) is not None and line.indent > indent:
            raise ManifestError(f"line {line.number}: unexpected indentation")

    def inline(self, text: str, line: int, depth: int) -> Node:
        _depth(depth, line)
        if text[0] in "[{":
            flow = _Flow(text, line, self.budget)
            node = flow.value(depth)
            flow.end()
            return node
        self.budget.take(line)
        if text[0] in "\"'":
            value, end = _quoted(text, 0, line)
            if text[end:].strip():
                raise ManifestError(f"line {line}: text after a quoted value")
            return Scalar(value, None, line)
        return _plain(text, line)


def _plain(text: str, line: int) -> Scalar:
    if problem := _INDICATORS.get(text[0]):
        raise ManifestError(f"line {line}: {problem}")
    if text[0] in "-:," and (len(text) == 1 or text[1] == " ") and text != "-":
        raise ManifestError(f"line {line}: a value cannot start with {text[0]!r}; quote it")
    if ": " in text or text.endswith(":"):
        raise ManifestError(f"line {line}: a plain value holding ': ' must be quoted")
    if " #" in text or "\t#" in text:  # pragma: no cover - comments are stripped before
        raise ManifestError(f"line {line}: a comment inside a value")
    text = _scalar_text(text, line)
    return Scalar(_resolve(text, line), text, line)


def _key_split(content: str, line: int) -> tuple[str, str] | None:
    """``(key, rest)`` if ``content`` is ``key: rest`` (or ``key:``); else ``None``."""
    if content[0] in "\"'":
        key, end = _quoted(content, 0, line)
        rest = content[end:]
        if rest == ":" or rest.startswith(": ") or rest.startswith(":\t"):
            return key, rest[1:].strip(" \t")
        if rest.strip():
            raise ManifestError(f"line {line}: expected ':' after a quoted key")
        return None
    if content[0] in "[{":
        return None
    index = 0
    while True:
        index = content.find(":", index)
        if index < 0:
            return None
        if index + 1 == len(content) or content[index + 1] in " \t":
            break
        index += 1
    key = content[:index].rstrip(" \t")
    if not key:
        raise ManifestError(f"line {line}: an empty key")
    if problem := _INDICATORS.get(key[0]):
        raise ManifestError(f"line {line}: {problem}")
    if key[0] in "[]{},#":
        raise ManifestError(f"line {line}: a key cannot start with {key[0]!r}; quote it")
    return _scalar_text(key, line), content[index + 1 :].strip(" \t")


def _quoted(text: str, start: int, line: int) -> tuple[str, int]:
    """The quoted scalar starting at ``text[start]`` and the index just past its closing quote."""
    quote = text[start]
    out: list[str] = []
    index = start + 1
    while index < len(text):
        char = text[index]
        if quote == "'" and char == "'":
            if text[index + 1 : index + 2] == "'":
                out.append("'")
                index += 2
                continue
            return _scalar_text("".join(out), line), index + 1
        if quote == '"' and char == '"':
            return _scalar_text("".join(out), line), index + 1
        if quote == '"' and char == "\\":
            code = text[index + 1 : index + 2]
            if code in _HEX_ESCAPES:
                width = _HEX_ESCAPES[code]
                digits = text[index + 2 : index + 2 + width]
                if len(digits) != width or not all(c in "0123456789abcdefABCDEF" for c in digits):
                    raise ManifestError(f"line {line}: a bad \\{code} escape")
                point = int(digits, 16)
                if point > 0x10FFFF or 0xD800 <= point <= 0xDFFF:
                    raise ManifestError(f"line {line}: \\{code}{digits} is not a character")
                out.append(chr(point))
                index += 2 + width
                continue
            if code not in _ESCAPES:
                raise ManifestError(f"line {line}: an unknown escape \\{code}")
            out.append(_ESCAPES[code])
            index += 2
            continue
        out.append(char)
        index += 1
    raise ManifestError(f"line {line}: a quoted value is not closed on its line")


def _strip_comment(raw: str, line: int) -> str:
    """``raw`` less a ``#`` comment, which starts a line or follows a space outside quotes.

    A quote opens a quoted scalar only where a scalar can start: at the start of the content,
    or after ``- ``, ``: ``, ``[``, ``{`` or ``,``. Elsewhere (``it's``) it is a character.
    """
    index, previous = 0, " "
    while index < len(raw):
        char = raw[index]
        if char == "#" and previous in " \t":
            return raw[:index]
        if char in "\"'" and _scalar_start(raw, index):
            _, index = _quoted(raw, index, line)
            previous = raw[index - 1]
            continue
        previous = char
        index += 1
    return raw


def _scalar_start(raw: str, index: int) -> bool:
    before = raw[:index].rstrip(" \t")
    if not before or before[-1] in "[{,":
        return True
    return before[-1] in "-:" and index > len(before)  # "- 'x'" or "key: 'x'"


class _Flow:
    """A flow collection on one line: ``[a, "b"]`` or ``{path: x, adapter: y}``, nested."""

    def __init__(self, text: str, line: int, budget: _Budget) -> None:
        self.text, self.line, self.budget, self.at = text, line, budget, 0

    def fail(self, problem: str) -> ManifestError:
        return ManifestError(f"line {self.line}: {problem}")

    def skip(self) -> None:
        while self.at < len(self.text) and self.text[self.at] in " \t":
            self.at += 1

    def end(self) -> None:
        self.skip()
        if self.at != len(self.text):
            raise self.fail("text after a flow collection")

    def value(self, depth: int) -> Node:
        _depth(depth, self.line)
        self.skip()
        if self.at >= len(self.text):
            raise self.fail("a flow collection ends early")
        char = self.text[self.at]
        if char == "[":
            return self.sequence(depth)
        if char == "{":
            return self.mapping(depth)
        return self.scalar()

    def scalar(self) -> Scalar:
        self.budget.take(self.line)
        if self.at >= len(self.text):
            raise self.fail("a flow collection ends early")
        if self.text[self.at] in "\"'":
            value, self.at = _quoted(self.text, self.at, self.line)
            return Scalar(value, None, self.line)
        start = self.at
        while self.at < len(self.text) and self.text[self.at] not in ",]}":
            if self.text[self.at] == ":" and self.text[self.at + 1 : self.at + 2] in (" ", ""):
                break
            self.at += 1
        text = self.text[start : self.at].strip(" \t")
        if not text:
            raise self.fail("an empty value in a flow collection")
        if text[0] in "[{":
            raise self.fail("a flow collection ends early")
        return _plain(text, self.line)

    def sequence(self, depth: int) -> Seq:
        self.budget.take(self.line)
        self.at += 1
        items: list[Node] = []
        self.skip()
        if self.text[self.at : self.at + 1] == "]":
            self.at += 1
            return Seq((), self.line)
        while True:
            items.append(self.value(depth + 1))
            self.skip()
            char = self.text[self.at : self.at + 1]
            self.at += 1
            if char == "]":
                return Seq(tuple(items), self.line)
            if char != ",":
                raise self.fail("expected ',' or ']' in a flow sequence")

    def mapping(self, depth: int) -> Map:
        self.budget.take(self.line)
        self.at += 1
        items: list[tuple[str, Node]] = []
        seen: set[str] = set()
        self.skip()
        if self.text[self.at : self.at + 1] == "}":
            self.at += 1
            return Map((), self.line)
        while True:
            self.skip()
            key = self.scalar()
            if not isinstance(key.value, str) and key.text is None:
                raise self.fail("a flow mapping's key is text")
            name = key.text if key.text is not None else str(key.value)
            if name in seen:
                raise self.fail(f"duplicate key {name!r}")
            seen.add(name)
            self.skip()
            if self.text[self.at : self.at + 1] != ":":
                raise self.fail("expected ':' after a key in a flow mapping")
            self.at += 1
            items.append((name, self.value(depth + 1)))
            self.skip()
            char = self.text[self.at : self.at + 1]
            self.at += 1
            if char == "}":
                return Map(tuple(items), self.line)
            if char != ",":
                raise self.fail("expected ',' or '}' in a flow mapping")

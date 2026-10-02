"""Reading JSON by its bytes: where every member and element starts and ends, without a tree.

``json`` decides what is valid and what a token means. Its C scanner (``raw_decode``) also says
where a value ends, which is all a byte citation needs, so a document is walked member by member
and element by element, each value decoded once at C speed and its place kept.

Two readers share the rules. The plain functions walk one ``str`` (one feature, one block). A
``Reader`` walks a source through a window of its bytes, read as Latin-1 so that a character is a
byte and an offset is exact whatever the text holds; strings read that way are only good for
ASCII structure (names, a CRS code), and values that matter are decoded again from the UTF-8 of
their own span. A window grows when a value does not fit, up to a cap, so memory is bounded by the
cap and not by the source.

Nesting is bounded by the interpreter's own limit: a value nested deeper raises ``RecursionError``
in ``json``, and its end is then found by ``bracket_end``, which counts brackets without recursion.
"""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.contract import SourceReader

_WS: Final = " \t\n\r"
_DECODER: Final = json.JSONDecoder()
_BRACKETS: Final = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"|[\[\]{}]', re.DOTALL)
WINDOW: Final = 1024 * 1024  # bytes a window starts with
_SLACK: Final = 8192  # an error this close to a window's end may be the window's


class Deep:
    """The value of a member or element nested deeper than ``json`` reads."""


DEEP: Final = Deep()


class ScanError(Exception):
    """The text breaks off or breaks its grammar. ``kind`` is ``truncated``, ``syntax``,
    ``number`` (an integer literal over the interpreter's digit limit) or ``large`` (a value that
    does not fit the cap); ``offset`` is where, counted in the text read."""

    def __init__(self, kind: str, offset: int, reason: str) -> None:
        super().__init__(f"{kind} at {offset}: {reason}")
        self.kind = kind
        self.offset = offset
        self.reason = reason


def skip_ws(text: str, i: int) -> int:
    size = len(text)
    while i < size and text[i] in _WS:
        i += 1
    return i


def _decode_error(text: str, error: json.JSONDecodeError) -> ScanError:
    at = error.pos
    cut = at >= len(text.rstrip(_WS)) or error.msg.startswith("Unterminated")
    return ScanError("truncated" if cut else "syntax", at, error.msg)


def decode_value(text: str, i: int) -> tuple[object, int]:
    """The JSON value at ``i`` and where it ends. A value nested too deeply is ``DEEP``, its end
    found by counting brackets; ``ScanError`` for a grammar error."""
    try:
        return _DECODER.raw_decode(text, i)
    except json.JSONDecodeError as exc:
        raise _decode_error(text, exc) from exc
    except RecursionError:
        end = bracket_end(text, i)
        if end is None:
            raise ScanError("truncated", len(text), "a nested value is not closed") from None
        return DEEP, end
    except ValueError as exc:  # an integer literal over sys.get_int_max_str_digits()
        raise ScanError("number", i, str(exc)) from exc


def bracket_end(text: str, i: int) -> int | None:
    """Where the array or object at ``i`` ends, counting brackets outside strings; ``None`` if
    it is not closed in ``text``. Iterative, so any depth costs time and not stack."""
    depth = 0
    for match in _BRACKETS.finditer(text, i):
        char = match.group()
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth <= 0:
                return match.end()
    return None


@dataclass(frozen=True)
class Member:
    name: str
    name_start: int
    name_end: int  # the same as ``name_start`` for an array's element, which has no name
    start: int  # the value's first character
    end: int
    value: object


def read_string(text: str, i: int) -> tuple[str, int]:
    """The string literal at ``i`` (its opening quote) and where it ends."""
    try:
        value, end = _DECODER.raw_decode(text, i)
    except json.JSONDecodeError as exc:
        raise _decode_error(text, exc) from exc
    if not isinstance(value, str):
        raise ScanError("syntax", i, "expected a string")
    return value, end


def object_members(text: str, i: int) -> tuple[list[Member], int]:
    """Every member, repeated names included, of the object whose ``{`` is at ``i``."""
    found: list[Member] = []
    i = skip_ws(text, i + 1)
    if text[i : i + 1] == "}":
        return found, i + 1
    while True:
        if text[i : i + 1] != '"':
            raise ScanError("syntax", i, "expected a member name")
        name_start = i
        name, i = read_string(text, i)
        name_end = i
        i = skip_ws(text, i)
        if text[i : i + 1] != ":":
            raise ScanError("syntax", i, "expected ':'")
        i = skip_ws(text, i + 1)
        value, end = decode_value(text, i)
        found.append(Member(name, name_start, name_end, i, end, value))
        i = skip_ws(text, end)
        char = text[i : i + 1]
        if char == "}":
            return found, i + 1
        if char != ",":
            raise ScanError("truncated" if not char else "syntax", i, "expected ',' or '}'")
        i = skip_ws(text, i + 1)


def array_items(text: str, i: int) -> tuple[list[Member], int]:
    """Every element of the array whose ``[`` is at ``i``, as members named by position."""
    found: list[Member] = []
    i = skip_ws(text, i + 1)
    if text[i : i + 1] == "]":
        return found, i + 1
    while True:
        value, end = decode_value(text, i)
        found.append(Member(str(len(found)), i, i, i, end, value))
        i = skip_ws(text, end)
        char = text[i : i + 1]
        if char == "]":
            return found, i + 1
        if char != ",":
            raise ScanError("truncated" if not char else "syntax", i, "expected ',' or ']'")
        i = skip_ws(text, i + 1)


@dataclass
class Element:
    start: int  # byte offsets in the source
    end: int
    value: object  # decoded from Latin-1 text; ``DEEP`` when nested too deeply


@dataclass
class ArrayState:
    """What an element walk found at its end."""

    closed: bool = False  # the closing bracket was read
    end: int = 0  # the first byte after the array, when closed
    broken: ScanError | None = None  # where the text stopped being JSON; offsets are the source's
    full: bool = False  # an element was longer than the cap: nothing after it was read
    at: int = 0  # the offset of the element that was longer
    notes: list[str] = field(default_factory=list)


class Reader:
    """A cursor over ``source[start:end]`` with a window of Latin-1 text that grows to fit."""

    def __init__(self, source: SourceReader, start: int, end: int, cap: int) -> None:
        self._source = source
        self._end = min(end, source.size)
        self._cap = max(cap, WINDOW)
        self._base = start
        self._text = ""
        self._i = 0
        self._size = WINDOW
        self._load(start)

    def _load(self, at: int, size: int | None = None) -> None:
        want = self._size if size is None else size
        data = self._source.read(at, max(0, min(want, self._end - at)))
        self._base, self._text, self._i = at, data.decode("latin-1"), 0

    @property
    def offset(self) -> int:
        return self._base + self._i

    @property
    def at_end(self) -> bool:
        return self.offset >= self._end

    def _more(self) -> bool:
        return self._base + len(self._text) < self._end

    def peek(self) -> str:
        """The next character after whitespace, or ``""`` at the end."""
        while True:
            self._i = skip_ws(self._text, self._i)
            if self._i < len(self._text):
                return self._text[self._i]
            if not self._more():
                return ""
            self._load(self._base + self._i)

    def advance(self, count: int = 1) -> None:
        self._i += count

    def _grow(self) -> bool:
        """Read a larger window from the cursor if there is more to read and room under the cap."""
        have = len(self._text) - self._i
        if not self._more() or have >= self._cap:
            return False
        self._size = min(self._cap, max(self._size, 2 * have))
        self._load(self.offset, self._size)
        return True

    def string(self) -> str:
        """The string at the cursor (a member's name); the cursor moves past it."""
        while True:
            try:
                value, end = read_string(self._text, self._i)
            except ScanError as exc:
                if self._grow():
                    continue
                raise ScanError(exc.kind, self._base + exc.offset, exc.reason) from None
            self._i = end
            return value

    def value(self) -> tuple[object, int, int]:
        """The value at the cursor, its first byte and the first byte after it."""
        while True:
            self.peek()
            start = self.offset
            try:
                value, end = decode_value(self._text, self._i)
            except ScanError as exc:
                # an error in the last few KiB may be the window's end, not the text's
                near_end = exc.kind == "truncated" or exc.offset >= len(self._text) - _SLACK
                if exc.kind != "number" and near_end and self._more():
                    if self._grow():
                        continue
                    raise ScanError("large", start, "a value longer than the window cap") from None
                raise ScanError(exc.kind, self._base + exc.offset, exc.reason) from None
            if end >= len(self._text) and self._more() and self._grow():
                continue  # a number may be cut at the window's end
            self._i = end
            return value, start, self._base + end

    def elements(self, state: ArrayState, *, until_end: bool = False) -> Iterator[Element]:
        """The elements of an array whose ``[`` has been read, to its ``]`` (or, with
        ``until_end``, to the end of the range: a block of elements, commas between)."""
        first, comma = True, False
        while True:
            char = self.peek()
            if char == "]" and comma:
                state.broken = ScanError("syntax", self.offset, "a comma before ']'")
                return
            if not char:
                if until_end:
                    return
                state.broken = ScanError("truncated", self.offset, "the array is not closed")
                return
            if char == "]" and (first or not until_end):
                self.advance()
                state.closed, state.end = True, self.offset
                return
            try:
                value, start, end = self.value()
            except ScanError as exc:
                if exc.kind == "large":
                    state.full, state.at = True, exc.offset
                else:
                    state.broken = exc
                return
            comma = False
            yield Element(start, end, value)
            first = False
            char = self.peek()
            if char == ",":
                self.advance()
                comma = True
                continue
            if char == "]" and not until_end:
                continue
            if not char and until_end:
                return
            state.broken = ScanError(
                "truncated" if not char else "syntax", self.offset, "expected ',' or ']'"
            )
            return

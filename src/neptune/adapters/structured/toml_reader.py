"""TOML 1.0 through the standard library's ``tomllib``, with spans and comments.

``tomllib`` decides what is valid and what every value is; a TOML document with a repeated key is
invalid and so not read at all, as every TOML reader refuses it. ``tomllib`` keeps no positions
or comments, so a second pass over the accepted text finds where each value is written (a
table's header, an inline value's span) and every comment. It runs only over text ``tomllib``
accepted; where it cannot place a value, the value cites its pointer alone.
"""

import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Final

from neptune.adapters.structured.scalars import floating, integer
from neptune.adapters.structured.text import offset_of
from neptune.adapters.structured.tree import (
    Collection,
    Document,
    Issue,
    Limits,
    Node,
    NodeValue,
    Parse,
    Problem,
    SkippedEntry,
    Spot,
    TooDeep,
    Unreadable,
    Value,
    issues_for,
)
from neptune.model.configuration import CollectionType, ConfigFormat, ConfigScalar, Path, ScalarType

_BARE: Final = re.compile(r"[A-Za-z0-9_-]+")
_BASIC: Final = re.compile(r'"(?:[^"\\\n]|\\.)*"')
_LITERAL: Final = re.compile(r"'[^'\n]*'")
_DATETIME: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:\d{2})?)?"
    r"|\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
)
_BARE_VALUE: Final = re.compile(r"[A-Za-z0-9_+.\-]+")
_ESCAPE: Final = re.compile(r"\\(?:u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8})|(.))", re.DOTALL)
_ESCAPES: Final = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}
_POSITION: Final = re.compile(r"\(at line (\d+), column (\d+)\)$")


def _unescape(body: str) -> str:
    def one(match: re.Match[str]) -> str:
        code = match[1] or match[2]
        return chr(int(code, 16)) if code else _ESCAPES.get(match[3], match[3])

    return _ESCAPE.sub(one, body)


@dataclass
class _Open:
    """An array or inline table being scanned: its path, where it starts, its last position."""

    array: bool
    path: Path
    start: int
    count: int = 0


@dataclass
class Located:
    """Where the scan found each value and comment."""

    spans: dict[Path, Spot] = field(default_factory=dict)
    comments: list[Spot] = field(default_factory=list)


class _Scanner:
    """One pass over a TOML document that ``tomllib`` accepted. Iterative: nesting costs no
    recursion."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.size = len(text)
        self.found = Located({(): (0, len(text))})
        self.arrays: dict[Path, int] = {}  # arrays of tables and how many tables each has

    def _space(self, i: int) -> int:
        while i < self.size and self.text[i] in " \t":
            i += 1
        return i

    def _comment(self, i: int) -> int:
        end = self.text.find("\n", i)
        end = self.size if end < 0 else end
        stop = end - 1 if end > i and self.text[end - 1] == "\r" else end
        self.found.comments.append((i, stop))
        return end

    def _trivia(self, i: int) -> int:
        """Whitespace, line breaks and comments, as arrays may hold between values."""
        while i < self.size:
            char = self.text[i]
            if char in " \t\r\n":
                i += 1
            elif char == "#":
                i = self._comment(i)
            else:
                break
        return i

    def _key(self, i: int) -> tuple[tuple[str, ...], int]:
        keys: list[str] = []
        while True:
            i = self._space(i)
            char = self.text[i]
            match = (
                _BASIC.match(self.text, i)
                if char == '"'
                else _LITERAL.match(self.text, i)
                if char == "'"
                else _BARE.match(self.text, i)
            )
            if match is None:
                raise ValueError(f"no key at {i}")
            raw = match.group()
            keys.append(_unescape(raw[1:-1]) if char == '"' else raw[1:-1] if char == "'" else raw)
            i = self._space(match.end())
            if i < self.size and self.text[i] == ".":
                i += 1
                continue
            return tuple(keys), i

    def _resolve(self, keys: tuple[str, ...]) -> Path:
        """A header's keys as a path: through an array of tables, its last table."""
        path: Path = ()
        for key in keys:
            path = (*path, key)
            if path in self.arrays:
                path = (*path, self.arrays[path] - 1)
        return path

    def _multiline(self, i: int, quote: str) -> int:
        """The end of a multi-line string starting at ``i``: up to two more quotes belong to it."""
        search = i + 3
        while True:
            close = self.text.find(quote * 3, search)
            if close < 0:
                return self.size
            slashes = 0
            while (
                quote == '"'
                and close - slashes - 1 >= search
                and (self.text[close - slashes - 1] == "\\")
            ):
                slashes += 1
            if slashes % 2:
                search = close + 1
                continue
            end = close + 3
            while end < self.size and self.text[end] == quote and end - close - 3 < 2:
                end += 1
            return end

    def _scalar_end(self, i: int) -> int:
        text = self.text
        if text.startswith('"""', i) or text.startswith("'''", i):
            return self._multiline(i, text[i])
        match = (
            _BASIC.match(text, i)
            if text[i] == '"'
            else _LITERAL.match(text, i)
            if text[i] == "'"
            else _DATETIME.match(text, i) or _BARE_VALUE.match(text, i)
        )
        if match is None:
            raise ValueError(f"no value at {i}")
        return match.end()

    def _value(self, i: int, path: Path) -> int:
        frames: list[_Open] = []
        expect = True
        while True:
            if expect:
                char = self.text[i]
                if char in "[{":
                    frames.append(_Open(char == "[", path, i))
                    i = self._trivia(i + 1) if char == "[" else self._space(i + 1)
                    if self.text[i] in "]}":
                        i = self._close(frames, i + 1)
                        expect = False
                    elif char == "[":
                        path = (*path, 0)
                    else:
                        keys, i = self._key(i)
                        i = self._space(i + 1)
                        path = (*frames[-1].path, *keys)
                    continue
                end = self._scalar_end(i)
                self.found.spans[path] = (i, end)
                i, expect = end, False
                continue
            if not frames:
                return i
            frame = frames[-1]
            i = self._trivia(i) if frame.array else self._space(i)
            if self.text[i] != ",":
                i = self._close(frames, i + 1)
                continue
            i = self._trivia(i + 1) if frame.array else self._space(i + 1)
            if frame.array and self.text[i] == "]":  # a trailing comma
                i = self._close(frames, i + 1)
                continue
            if frame.array:
                frame.count += 1
                path = (*frame.path, frame.count)
            else:
                keys, i = self._key(i)
                i = self._space(i + 1)
                path = (*frame.path, *keys)
            expect = True

    def _close(self, frames: list[_Open], end: int) -> int:
        frame = frames.pop()
        self.found.spans[frame.path] = (frame.start, end)
        return end

    def scan(self) -> Located:
        table: Path = ()
        i = 0
        while i < self.size:
            i = self._space(i)
            if i >= self.size:
                break
            char = self.text[i]
            if char in "\r\n":
                i += 1
            elif char == "#":
                i = self._comment(i)
            elif char == "[":
                start, double = i, self.text.startswith("[[", i)
                keys, i = self._key(i + (2 if double else 1))
                i += 2 if double else 1
                if double:
                    array = (*self._resolve(keys[:-1]), keys[-1])
                    count = self.arrays.get(array, 0)
                    self.arrays[array] = count + 1
                    table = (*array, count)
                else:
                    table = self._resolve(keys)
                self.found.spans[table] = (start, i)
            else:
                keys, i = self._key(i)
                i = self._space(i + 1)  # past '='
                i = self._value(i, (*table, *keys))
        return self.found


def locate(text: str) -> Located:
    """Spans and comments of a document ``tomllib`` accepted; empty if the scan cannot follow."""
    try:
        return _Scanner(text).scan()
    except (IndexError, ValueError):
        return Located()


def _problem(text: str, exc: Exception) -> Problem:
    message = str(exc)
    found = _POSITION.search(message)
    if found:
        offset = offset_of(text, int(found[1]), int(found[2]))
        message = message[: found.start()].rstrip()
    elif message.endswith("(at end of document)"):
        offset, message = len(text), message.removesuffix("(at end of document)").rstrip()
    else:
        offset = 0
    return Problem(message, offset, 0, 0)


def _datetime(value: datetime | date | time) -> ConfigScalar:
    if isinstance(value, datetime):
        kind = ScalarType.LOCAL_DATETIME if value.tzinfo is None else ScalarType.OFFSET_DATETIME
    elif isinstance(value, date):
        kind = ScalarType.LOCAL_DATE
    else:
        kind = ScalarType.LOCAL_TIME
    return ConfigScalar(kind, value.isoformat())


def _scalar(item: Any, raw: str | None, limits: Limits) -> tuple[str | None, NodeValue]:
    """A scalar's declared text (a string's content, any other value's token) and reading."""
    if isinstance(item, str):
        if len(item) > limits.max_scalar:
            return None, Unreadable(Issue.SCALAR_TOO_LARGE, "a string over max_scalar_length")
        return item, Value((ConfigScalar(ScalarType.STRING, item),))
    if raw is not None and len(raw) > limits.max_scalar:
        return None, Unreadable(Issue.SCALAR_TOO_LARGE, "a value over max_scalar_length")
    if isinstance(item, bool):
        return raw or ("true" if item else "false"), Value((ConfigScalar(ScalarType.BOOL, item),))
    if isinstance(item, int):
        return raw, integer(item)
    if isinstance(item, float):
        return raw, floating(item, raw if raw is not None else repr(item))
    return raw, Value((_datetime(item),))


def read_toml(text: str, limits: Limits) -> Parse:
    """The one document a TOML text holds, or the problem that stopped ``tomllib``."""
    extent = (0, len(text))
    try:
        data = tomllib.loads(text)
    except RecursionError:
        return Parse(ConfigFormat.TOML, [], too_deep=[TooDeep(0, extent)])
    except ValueError as exc:  # TOMLDecodeError, or an integer past Python's digit limit
        return Parse(ConfigFormat.TOML, [], _problem(text, exc))
    located = locate(text)
    nodes: list[Node] = []
    skipped: list[SkippedEntry] = []
    pending: list[tuple[Any, Path, int, int]] = [(data, (), 0, -1)]
    while pending:
        item, path, order, parent = pending.pop()
        if len(path) > limits.max_depth:
            return Parse(ConfigFormat.TOML, [], too_deep=[TooDeep(0, extent)])
        span = located.spans.get(path)
        index = len(nodes)
        if isinstance(item, dict):
            children: list[tuple[Any, Path, int, int]] = []
            for position, (key, value) in enumerate(item.items()):
                if len(key) > limits.max_scalar:
                    where = located.spans.get((*path, key), span or extent)
                    skipped.append(SkippedEntry(index, where, "a key over max_scalar_length"))
                    continue
                children.append((value, (*path, key), position, index))
            nodes.append(Node(path, order, parent, Collection(CollectionType.MAPPING, len(item))))
        elif isinstance(item, list):
            children = [(value, (*path, i), i, index) for i, value in enumerate(item)]
            nodes.append(Node(path, order, parent, Collection(CollectionType.SEQUENCE, len(item))))
        else:
            raw = text[span[0] : span[1]] if span is not None else None
            declared, reading = _scalar(item, raw, limits)
            issues = issues_for(reading)
            nodes.append(Node(path, order, parent, reading, declared, None, span, False, issues))
            continue
        nodes[index].span = span
        pending.extend(reversed(children))
    document = Document(0, nodes, extent, located.comments, skipped=skipped)
    return Parse(ConfigFormat.TOML, [document])

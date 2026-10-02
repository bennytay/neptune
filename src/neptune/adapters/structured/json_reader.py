"""JSON (RFC 8259) through the standard library's ``json``, with the span of every value.

``json`` decides what is valid and what every token means; three hooks keep what it would
otherwise drop: an object's members in order with repeated names (``object_pairs_hook``), and a
number's exact text (``parse_int``, ``parse_float``). ``NaN`` and ``Infinity``, which ``json``
accepts and RFC 8259 does not, are read as declared and reported. A second, simple pass finds
where each value is written; it runs only over text ``json`` accepted, and if it ever disagrees
with ``json`` about how many values there are, the values cite their pointers alone.
"""

import json
import re
from dataclasses import dataclass
from typing import Final

from neptune.adapters.structured.scalars import decimal_integer, floating
from neptune.adapters.structured.tree import (
    Collection,
    Document,
    Issue,
    Limits,
    Node,
    NodeValue,
    Null,
    Parse,
    Problem,
    SkippedEntry,
    Spot,
    TooDeep,
    Unreadable,
    Value,
    issues_for,
    mark_repeats,
)
from neptune.model.configuration import CollectionType, ConfigFormat, ConfigScalar, Path, ScalarType
from neptune.model.scalars import NonFinite

_WHITESPACE: Final = " \t\n\r"
_STRING: Final = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_TOKEN_END: Final = re.compile(r"[ \t\n\r,\]}:]")
_CONSTANTS: Final = {
    "NaN": NonFinite.NAN,
    "Infinity": NonFinite.POSITIVE_INFINITY,
    "-Infinity": NonFinite.NEGATIVE_INFINITY,
}


class _Members(list[tuple[str, object]]):
    """An object's members in source order, a repeated name kept every time it appears."""


@dataclass(frozen=True)
class _Number:
    text: str  # the token as written
    kind: str  # "int", "float" or "constant"


def _int(text: str) -> _Number:
    return _Number(text, "int")


def _float(text: str) -> _Number:
    return _Number(text, "float")


def _constant(text: str) -> _Number:
    return _Number(text, "constant")


def spans(text: str) -> list[Spot]:
    """Every value's code points ``[start, end)``, in pre-order, in text ``json`` accepted."""
    found: list[list[int]] = []
    open_: list[int] = []
    position, size = 0, len(text)
    while position < size:
        char = text[position]
        if char in _WHITESPACE or char in ",:":
            position += 1
        elif char in "{[":
            open_.append(len(found))
            found.append([position, -1])
            position += 1
        elif char in "}]":
            found[open_.pop()][1] = position + 1
            position += 1
        elif char == '"':
            match = _STRING.match(text, position)
            end = match.end() if match else size
            after = end
            while after < size and text[after] in _WHITESPACE:
                after += 1
            if after < size and text[after] == ":":  # a member's name, not a value
                position = after + 1
                continue
            found.append([position, end])
            position = end
        else:
            match = _TOKEN_END.search(text, position)
            end = match.start() if match else size
            found.append([position, end])
            position = end
    return [(start, end) for start, end in found]


def _string(value: str, limits: Limits) -> tuple[str | None, NodeValue, tuple[Issue, ...]]:
    if len(value) > limits.max_scalar:
        return None, Unreadable(Issue.SCALAR_TOO_LARGE, "a string over max_scalar_bytes"), ()
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        reason = "a string with an unpaired surrogate escape"
        return None, Unreadable(Issue.UNREPRESENTABLE, reason), ()
    return value, Value((ConfigScalar(ScalarType.STRING, value),)), ()


def _scalar(item: object, limits: Limits) -> tuple[str | None, NodeValue, tuple[Issue, ...]]:
    """A scalar's declared text, its reading and anything to report about it."""
    if isinstance(item, str):
        return _string(item, limits)
    if item is None:
        return "null", Null(), ()
    if isinstance(item, bool):
        return ("true" if item else "false"), Value((ConfigScalar(ScalarType.BOOL, item),)), ()
    assert isinstance(item, _Number)
    if len(item.text) > limits.max_scalar:
        return None, Unreadable(Issue.SCALAR_TOO_LARGE, "a number over max_scalar_bytes"), ()
    if item.kind == "constant":
        reading = Value((ConfigScalar(ScalarType.FLOAT, _CONSTANTS[item.text]),))
        return item.text, reading, (Issue.NONSTANDARD_JSON,)
    if item.kind == "int":
        return item.text, decimal_integer(item.text), ()
    return item.text, floating(float(item.text), item.text), ()


def _refused_name(name: str, limits: Limits) -> str:
    """Why a member's name cannot be a path key, or ``""`` if it can."""
    if len(name) > limits.max_scalar:
        return "a member name over max_scalar_bytes"
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return "a member name with an unpaired surrogate escape"
    return ""


def _size(item: object) -> int:
    """How many values ``item`` holds, itself included: the spans a skipped member covers."""
    count, pending = 0, [item]
    while pending:
        current = pending.pop()
        count += 1
        if isinstance(current, _Members):
            pending.extend(value for _, value in current)
        elif isinstance(current, list):
            pending.extend(current)
    return count


def read_json(text: str, limits: Limits) -> Parse:
    """The one document a JSON text holds, or the problem that stopped ``json``."""
    extent = (0, len(text))
    try:
        data = json.loads(
            text,
            object_pairs_hook=_Members,
            parse_int=_int,
            parse_float=_float,
            parse_constant=_constant,
        )
    except json.JSONDecodeError as exc:
        return Parse(ConfigFormat.JSON, [], Problem(exc.msg, exc.pos, 0, 0))
    except RecursionError:
        return Parse(ConfigFormat.JSON, [], too_deep=[TooDeep(0, extent)])
    located = spans(text)
    nodes: list[Node] = []
    members: dict[int, list[int]] = {}
    skipped: list[SkippedEntry] = []
    cursor = 0  # the next span: values are visited in pre-order, as the span pass lists them
    # (value, path, order, parent, why not held): a member whose name no record can hold is
    # visited only to step over its spans.
    pending: list[tuple[object, Path, int, int, str]] = [(data, (), 0, -1, "")]
    while pending:
        item, path, order, parent, refused = pending.pop()
        span = located[cursor] if cursor < len(located) else None
        if refused:
            skipped.append(SkippedEntry(parent, span or extent, refused))
            cursor += _size(item)
            continue
        cursor += 1
        if len(path) > limits.max_depth:
            return Parse(ConfigFormat.JSON, [], too_deep=[TooDeep(0, extent)])
        index = len(nodes)
        if parent in members:
            members[parent].append(index)
        if isinstance(item, _Members):
            kind, length = CollectionType.MAPPING, len(item)
            members[index] = []
            children = [
                (value, (*path, name), position, index, _refused_name(name, limits))
                for position, (name, value) in enumerate(item)
            ]
        elif isinstance(item, list):
            kind, length = CollectionType.SEQUENCE, len(item)
            children = [(value, (*path, i), i, index, "") for i, value in enumerate(item)]
        else:
            text_value, reading, issues = _scalar(item, limits)
            issues = issues_for(reading, issues)
            nodes.append(Node(path, order, parent, reading, text_value, None, span, False, issues))
            continue
        nodes.append(Node(path, order, parent, Collection(kind, length), span=span))
        pending.extend(reversed(children))
    if cursor != len(located):  # the span pass disagrees with json: cite pointers alone
        for node in nodes:
            node.span = None
    for children_of in members.values():
        mark_repeats(nodes, children_of)
    return Parse(ConfigFormat.JSON, [Document(0, nodes, extent, skipped=skipped)])

"""Strict JSON in, deterministic text out, for what an API says and what a snapshot holds (ADR 0008).

Reading is hostile-input safe: duplicate object keys, ``NaN`` and ``Infinity``, a body that is not
UTF-8, nesting past ``MAX_DEPTH`` and absurd numbers are each refused, never repaired. A number
keeps the text the API wrote (``1.50`` stays ``1.50``, ``1e2`` stays ``1e2``), so a snapshot says
what the system said and not what a float rounds to.

Writing is one byte form: keys sorted by code point, no insignificant whitespace, UTF-8 without
``\\u`` escapes beyond what JSON requires, numbers as read. A lone surrogate has no UTF-8 form and
is refused (``JsonTextError``).
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

MAX_DEPTH: Final = 64
MAX_NUMBER_CHARS: Final = 400


class JsonTextError(ValueError):
    """The text is not JSON this module accepts, or the value has no deterministic form."""


class Number(str):
    """A JSON number as written. Compared and printed as its text."""

    __slots__ = ()


def _number(text: str) -> Number:
    if len(text) > MAX_NUMBER_CHARS:
        raise JsonTextError("a number is absurdly long")
    return Number(text)


def _refuse_constant(name: str) -> Any:
    raise JsonTextError(f"{name} is not JSON")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise JsonTextError("an object repeats a key")
        out[key] = value
    return out


def loads(data: bytes) -> Any:
    """The value in ``data``, a UTF-8 JSON document. Numbers are ``Number``; nothing is repaired."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JsonTextError("not UTF-8") from exc
    try:
        value = json.loads(
            text.removeprefix("﻿"),
            object_pairs_hook=_object,
            parse_constant=_refuse_constant,
            parse_int=_number,
            parse_float=_number,
        )
    except RecursionError as exc:
        raise JsonTextError("nested too deeply") from exc
    except json.JSONDecodeError as exc:
        raise JsonTextError("not JSON") from exc
    _check_depth(value)
    return value


def _check_depth(root: Any) -> None:
    """Iterative, so a hostile document cannot recurse here either."""
    stack: list[tuple[Any, int]] = [(root, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, dict | list):
            if depth > MAX_DEPTH:
                raise JsonTextError("nested too deeply")
            children = value.values() if isinstance(value, dict) else value
            stack.extend((child, depth + 1) for child in children)


def dumps(value: Any) -> bytes:
    """The deterministic UTF-8 text of ``value`` (dicts, lists, str, bool, None, ``Number``, int)."""
    out: list[str] = []
    _write(value, out, 1)
    try:
        return "".join(out).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise JsonTextError("a string holds a lone surrogate") from exc


_ESCAPES: Final = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _string(text: str) -> str:
    return (
        '"' + "".join(_ESCAPES.get(c) or (f"\\u{ord(c):04x}" if c < " " else c) for c in text) + '"'
    )


def _write(value: Any, out: list[str], depth: int) -> None:
    if depth > MAX_DEPTH:
        raise JsonTextError("nested too deeply")
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, Number) or isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, Mapping):
        out.append("{")
        for index, key in enumerate(sorted(value)):
            if not isinstance(key, str):
                raise JsonTextError("an object key is not text")
            out.append(("," if index else "") + _string(key) + ":")
            _write(value[key], out, depth + 1)
        out.append("}")
    elif isinstance(value, Sequence):
        out.append("[")
        for index, item in enumerate(value):
            out.append("," if index else "")
            _write(item, out, depth + 1)
        out.append("]")
    else:
        raise JsonTextError(f"{type(value).__name__} is not JSON")


def pointer(value: Any, path: str) -> Any:
    """The value at JSON pointer ``path`` (RFC 6901) or ``MISSING``. ``""`` is ``value`` itself."""
    if path == "":
        return value
    if not path.startswith("/"):
        raise JsonTextError(f"not a JSON pointer: {path!r}")
    current = value
    for raw in path[1:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif (
            isinstance(current, list)
            and part.isdigit()
            and (part == "0" or not part.startswith("0"))
            and int(part) < len(current)
        ):
            current = current[int(part)]
        else:
            return MISSING
    return current


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Final = _Missing()


def cell(value: Any) -> str:
    """A scalar as a CSV cell: text as is, ``null`` and missing as blank, numbers as written,
    booleans as ``true``/``false``, an object or array as its deterministic JSON text."""
    if value is None or value is MISSING:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return str(value)  # a Number is a str; its text is what was written
    if isinstance(value, int):
        return str(value)
    return dumps(value).decode("utf-8")

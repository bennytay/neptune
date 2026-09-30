"""Canonical JSON (ADR 0002): the single byte form used for both hashing and storage.

- UTF-8, object keys sorted by Unicode code point, no insignificant whitespace.
- Strings are emitted as given (no Unicode normalisation). Only what JSON requires is escaped:
  ``"`` and ``\\`` as ``\\"`` / ``\\\\``; ``\\b \\t \\n \\f \\r`` in short form; other controls
  below U+0020 as ``\\u00xx`` (lowercase hex). This matches RFC 8785 (JCS).
- Integers are exact decimal of any magnitude up to Python's 4300-digit ``int``/``str`` limit;
  larger values are rejected rather than converted.
- Floats use the shortest digits that round-trip to the same IEEE-754 double, in Python's
  ``repr`` layout: positional for ``1e-4 <= |x| < 1e16``, else ``d.ddde±XX``. The text always
  contains ``.`` or ``e``, so a float never reads back as an int. ``-0.0`` is preserved.
- ``null``, NaN and ±Infinity are forbidden. So are lone surrogates, non-string keys, and any
  type outside ``JsonValue``. Tuples encode as arrays.
"""

import json
import math
from collections.abc import Mapping

from neptune.model.jsonvalue import JsonValue


class CanonicalJsonError(ValueError):
    """The value cannot be encoded canonically, or the bytes are not canonical JSON."""


def dumps(value: JsonValue) -> bytes:
    """Encode ``value`` as canonical JSON bytes."""
    out: list[str] = []
    try:
        _encode(value, out)
    except RecursionError as exc:
        raise CanonicalJsonError("value is nested too deeply") from exc
    return "".join(out).encode("utf-8")


def loads(data: bytes) -> JsonValue:
    """Decode canonical JSON, rejecting anything that is valid JSON but not canonical."""
    try:
        text = data.decode("utf-8")
        value: JsonValue = json.loads(
            text, object_pairs_hook=_object_without_duplicates, parse_constant=_nan
        )
    except RecursionError as exc:
        raise CanonicalJsonError("document is nested too deeply") from exc
    except ValueError as exc:  # UnicodeDecodeError, JSONDecodeError, int digit limit, our hooks
        raise CanonicalJsonError(f"not canonical JSON: {exc}") from exc
    if dumps(value) != data:
        raise CanonicalJsonError("valid JSON but not in canonical form")
    return value


def _encode(value: object, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(_encode_str(value))
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        try:
            out.append(int.__repr__(value))
        except ValueError as exc:  # beyond sys.get_int_max_str_digits()
            raise CanonicalJsonError(f"integer too large to encode: {exc}") from exc
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalJsonError(f"{value!r} is not representable in canonical JSON")
        out.append(float.__repr__(value))
    elif isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalJsonError(f"object keys must be str, got {type(key).__name__}")
        out.append("{")
        for index, key in enumerate(sorted(value)):
            if index:
                out.append(",")
            out.append(_encode_str(key))
            out.append(":")
            _encode(value[key], out)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _encode(item, out)
        out.append("]")
    elif value is None:
        raise CanonicalJsonError("null is forbidden; missingness is Knowledge (ADR 0004)")
    else:
        raise CanonicalJsonError(f"type {type(value).__name__} is not a canonical JSON value")


def _encode_str(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalJsonError(f"string is not valid Unicode: {value!r}") from exc
    return json.dumps(value, ensure_ascii=False)


def _object_without_duplicates(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise CanonicalJsonError("duplicate object key")
    return result


def _nan(token: str) -> JsonValue:
    raise CanonicalJsonError(f"{token} is forbidden in canonical JSON")

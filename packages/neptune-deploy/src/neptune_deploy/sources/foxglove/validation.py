"""Checks every value read from an API response goes through before it is used (ADR 0007 §8).

A response is hostile input: ids, names and times are bounded printable text, an absent field is
absent (the API's ``null`` is dropped, never read as a value), and anything off-shape is ``Invalid``
with the finding code that says why, so one bad entry costs that entry and nothing else.
"""

import hashlib
from typing import Final

from neptune.identity.canonical_json import CanonicalJsonError
from neptune.identity.canonical_json import dumps as canonical_dumps
from neptune.model.jsonvalue import JsonValue

MAX_DOCUMENT_BYTES: Final = (
    1024 * 1024
)  # canonical JSON of one recording or device; more is not used
MAX_TEXT: Final = 4096  # characters in a declared value (a path, a key, a device name)
MAX_TOKEN_PART: Final = 64  # characters in a time string or status that enters a revision token


class Invalid(Exception):
    """A recording or declared object that is not the documented shape (``reason`` is a code)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def text(value: object, limit: int = MAX_TEXT, *, empty: bool = False) -> str:
    """Printable text of 1 (or, with ``empty``, 0) to ``limit`` characters, valid Unicode; else
    ``Invalid``."""
    if (
        not isinstance(value, str)
        or not (0 if empty else 1) <= len(value) <= limit
        or not value.isprintable()
    ):
        raise Invalid("record_invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise Invalid("record_invalid") from exc
    return value


def strip_nulls(value: JsonValue) -> JsonValue:
    """``value`` without null-valued keys: the API omits or nulls an absent field, and canonical
    JSON has no null (missingness is a ``Knowledge`` state, not a value)."""
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value if v is not None]
    return value


def json_pointer(*parts: str | int) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def stable_name(value: object) -> bytes:
    """Stable bytes naming an entry that has no usable id: its sorted JSON, cut short."""
    try:
        encoded = canonical_dumps(value)  # type: ignore[arg-type]
    except (CanonicalJsonError, TypeError):
        encoded = repr(type(value).__name__).encode()
    return encoded[:256]


def item_digest(value: object) -> str:
    """A digest of the whole of ``value`` (canonical JSON where it has one), for noticing a page
    that is served again."""
    try:
        encoded = canonical_dumps(value)  # type: ignore[arg-type]
    except (CanonicalJsonError, TypeError):
        encoded = repr(value).encode("utf-8", "backslashreplace")
    return hashlib.sha256(encoded).hexdigest()

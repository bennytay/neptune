"""Small helpers every system's page reader shares: typed access to hostile JSON (ADR 0008)."""

import re
from collections.abc import Callable
from typing import Any

from neptune_deploy.sources.records.config import RecordConfigError
from neptune_deploy.sources.records.http import ResponseInvalid
from neptune_deploy.sources.records.jsontext import Number


def obj(value: Any) -> dict[str, Any]:
    """``value`` as a JSON object, or the response is not what the system documents."""
    if not isinstance(value, dict):
        raise ResponseInvalid("an object was expected")
    return value


def array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ResponseInvalid("an array was expected")
    return value


def text(value: Any) -> str | None:
    """A JSON string, else ``None`` (a ``Number`` is a number, not text)."""
    return value if isinstance(value, str) and not isinstance(value, Number) else None


def whole(value: Any) -> int | None:
    """A non-negative integer from a JSON integer or a string of digits, else ``None``."""
    if isinstance(value, Number):
        value = str(value)
    elif isinstance(value, str):
        pass
    else:
        return None
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 18 else None


def text_or_whole(value: Any) -> str | None:
    """An id the system writes as text or as an integer: its text, else ``None``."""
    if isinstance(value, Number):
        value = str(value)
        return value if value.isascii() and value.isdigit() and len(value) <= 18 else None
    return value if isinstance(value, str) else None


def flag(value: Any) -> bool:
    """A declared option that is ``true`` or ``false``."""
    if not isinstance(value, bool):
        raise RecordConfigError("this option is true or false")
    return value


def names(
    pattern: re.Pattern[str], what: str, *, limit: int = 200
) -> Callable[[Any], tuple[str, ...]]:
    """A checker for a declared list of names matching ``pattern``, sorted and de-duplicated."""

    def check(value: Any) -> tuple[str, ...]:
        if (
            not isinstance(value, list)
            or not value
            or len(value) > limit
            or not all(isinstance(v, str) and pattern.fullmatch(v) for v in value)
        ):
            raise RecordConfigError(f"{what} is a non-empty list of names, at most {limit}")
        return tuple(sorted(set(value)))

    return check

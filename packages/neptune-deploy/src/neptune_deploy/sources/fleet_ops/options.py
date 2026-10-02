"""Declared, closed options: an unknown option is refused, never ignored (ADR 0010 §6).

Every fleet-ops factory takes a mapping of options. A typo that was ignored would silently change
what a run read, so each factory names the options it accepts and everything else is an error.
"""

from collections.abc import Mapping
from typing import Final

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.object_store.transport import MAX_TIMEOUT

MAX_LIST: Final = 256


class FleetOpsConfigError(ValueError):
    """An option, URL or credential is not one this connector accepts. Never carries a secret."""


def closed(
    options: Mapping[str, JsonValue] | None, allowed: frozenset[str]
) -> Mapping[str, JsonValue]:
    """``options``, or ``{}``; ``FleetOpsConfigError`` if it names an option not in ``allowed``."""
    if options is None:
        return {}
    if not isinstance(options, Mapping):
        raise FleetOpsConfigError("options are a mapping of names to values")
    unknown = sorted(str(name) for name in options if name not in allowed)
    if unknown:
        raise FleetOpsConfigError(f"unknown options: {unknown}; accepted: {sorted(allowed)}")
    return options


def integer(options: Mapping[str, JsonValue], name: str, default: int, low: int, high: int) -> int:
    value = options.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise FleetOpsConfigError(f"{name} is an integer from {low} to {high}")
    return value


def seconds(options: Mapping[str, JsonValue], name: str, default: float) -> float:
    """A timeout: above 0 and at most the transport's ``MAX_TIMEOUT``. A larger one is an
    ``OverflowError`` in the socket layer, and ``inf`` and ``nan`` are no number of seconds."""
    value = options.get(name, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 < value <= MAX_TIMEOUT
    ):
        raise FleetOpsConfigError(f"{name} is a number of seconds from 0 to {MAX_TIMEOUT}")
    return float(value)


def text(options: Mapping[str, JsonValue], name: str, default: str | None) -> str | None:
    value = options.get(name, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 512 or not value.isprintable():
        raise FleetOpsConfigError(f"{name} is non-empty printable text of at most 512 characters")
    return value


def texts(options: Mapping[str, JsonValue], name: str) -> tuple[str, ...]:
    """A list of non-empty printable texts, at most ``MAX_LIST``, in the order declared."""
    value = options.get(name, [])
    if not isinstance(value, list | tuple) or len(value) > MAX_LIST:
        raise FleetOpsConfigError(f"{name} is a list of at most {MAX_LIST} texts")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 512 or not item.isprintable():
            raise FleetOpsConfigError(f"{name} holds non-empty printable text of at most 512")
        out.append(item)
    return tuple(out)


def flag(options: Mapping[str, JsonValue], name: str, default: bool) -> bool:
    value = options.get(name, default)
    if not isinstance(value, bool):
        raise FleetOpsConfigError(f"{name} is true or false")
    return value

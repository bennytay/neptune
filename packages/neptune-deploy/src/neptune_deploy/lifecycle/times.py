"""Declared date and time formats, read into ticks by root ADR 0023 §2 (ADR 0002 §5).

A format is a pattern of fixed-width numeric directives and literal text:

- ``%Y`` four digits; ``%m``, ``%d``, ``%H``, ``%M``, ``%S`` two digits; ``%f`` one to nine digits
  of a second; ``%z`` an offset (``Z``, ``+02:00`` or ``+0200``); ``%%`` a percent sign.
- ``%Y``, ``%m`` and ``%d`` are required; a time of day needs ``%H`` and ``%M``, ``%S`` needs
  ``%M``, ``%f`` needs ``%S``, and ``%z`` needs a time of day.

The text must match the whole pattern. No locale, no month names, no two-digit years, no guessing
between day-first and month-first: the mapping file says which. Reading:

- with an offset, the text names an instant: POSIX ticks from 1970-01-01T00:00:00Z;
- without one, ticks count from 1970-01-01T00:00:00 of the text's own civil clock, never moved to
  UTC (the zone the mapping declares is recorded, not applied);
- a date alone counts days (resolution 86,400 s); a date-time counts seconds, or the fraction
  ``%f`` states.
"""

import re
from dataclasses import dataclass
from datetime import date, time
from fractions import Fraction
from functools import cache
from typing import Final

_DIRECTIVES: Final = {
    "Y": r"(?P<Y>[0-9]{4})",
    "m": r"(?P<m>[0-9]{2})",
    "d": r"(?P<d>[0-9]{2})",
    "H": r"(?P<H>[0-9]{2})",
    "M": r"(?P<M>[0-9]{2})",
    "S": r"(?P<S>[0-9]{2})",
    "f": r"(?P<f>[0-9]{1,9})",
    "z": r"(?P<z>Z|[+-][0-9]{2}:?[0-9]{2})",
}
_NEEDS: Final = {"M": "H", "H": "M", "S": "M", "f": "S", "z": "H"}
_EPOCH: Final = date(1970, 1, 1).toordinal()
DAY: Final = Fraction(86400)
MAX_TEXT: Final = 64


@dataclass(frozen=True)
class Reading:
    """A time as ticks: ``resolution`` seconds per tick; ``instant`` when an offset was stated."""

    ticks: int
    resolution: Fraction
    instant: bool


@cache
def _compile(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char != "%":
            out.append(re.escape(char))
            i += 1
            continue
        if i + 1 == len(pattern):
            raise ValueError(f"{pattern!r} ends inside a directive")
        directive = pattern[i + 1]
        i += 2
        if directive == "%":
            out.append("%")
            continue
        if directive not in _DIRECTIVES:
            raise ValueError(f"%{directive} is not a directive: {sorted(_DIRECTIVES)}")
        if directive in seen:
            raise ValueError(f"%{directive} appears twice in {pattern!r}")
        seen.add(directive)
        out.append(_DIRECTIVES[directive])
    if not {"Y", "m", "d"} <= seen:
        raise ValueError(f"{pattern!r} needs %Y, %m and %d")
    for directive, needed in _NEEDS.items():
        if directive in seen and needed not in seen:
            raise ValueError(f"{pattern!r}: %{directive} needs %{needed}")
    return re.compile("".join(out))


def check_format(pattern: str) -> None:
    """Raise ``ValueError`` if ``pattern`` is not a format this module reads."""
    _compile(pattern)


def read_time(text: str, patterns: tuple[str, ...]) -> Reading | None:
    """``text`` read by the first pattern it matches whole; ``None`` if none reads it."""
    if len(text) > MAX_TEXT:
        return None
    for pattern in patterns:
        match = _compile(pattern).fullmatch(text)
        if match is None:
            continue
        reading = _reading({key: match.groupdict().get(key) for key in _DIRECTIVES})
        if reading is not None:
            return reading
    return None


def _reading(parts: dict[str, str | None]) -> Reading | None:
    try:
        day = date(int(parts["Y"] or 0), int(parts["m"] or 0), int(parts["d"] or 0))
    except ValueError:
        return None
    days = day.toordinal() - _EPOCH
    if parts["H"] is None:
        return Reading(days, DAY, instant=False)
    try:
        clock = time(int(parts["H"]), int(parts["M"] or 0), int(parts["S"] or 0))
    except ValueError:
        return None
    seconds = days * 86400 + clock.hour * 3600 + clock.minute * 60 + clock.second
    digits = parts["f"] or ""
    scale = 10 ** len(digits)
    ticks = seconds * scale + (int(digits) if digits else 0)
    offset = parts["z"]
    if offset is not None and offset != "Z":
        sign = -1 if offset[0] == "-" else 1
        hours, minutes = int(offset[1:3]), int(offset[-2:])
        if hours > 23 or minutes > 59:
            return None
        ticks -= sign * (hours * 3600 + minutes * 60) * scale
    return Reading(ticks, Fraction(1, scale), instant=offset is not None)

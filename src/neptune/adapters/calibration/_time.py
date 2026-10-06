"""A calibration time as the file writes it, counted by ADR 0023 §2 (ADR 0073 §3).

Two forms are read, and nothing else:

- ISO 8601's extended date-time: ``YYYY-MM-DD``, ``T`` or one space, ``HH:MM:SS``, an optional
  fraction of up to nine digits and an optional zone (``Z``, ``+HH:MM`` or ``+HHMM``, which may
  follow one space, as ``date '+%F %T %z'`` and git write it).
- The C locale's ``%c`` as glibc writes it, which is what OpenCV's own calibration samples store
  (they never set a locale): ``Thu Oct  1 12:00:00 2020``. Its weekday must be the date's.

A date-time with a zone names an instant: POSIX seconds from 1970-01-01T00:00:00Z, timescale
``posix``; the offset stays in the cited text. One without counts the same way on its own civil
clock, timescale ``Unknown``. Nothing assumes a zone. Locale-dependent forms (``08/19/11
20:44:38``, a two-digit year whose century the text does not give; a zone abbreviation such as
``EDT``) are not read.
"""

import calendar
import re
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from typing import Final

from neptune.model.time import INT64_MAX

_ISO: Final = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[T ]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?"
    r"(?: ?(Z|[+-][0-9]{2}:?[0-9]{2}))?",
    re.ASCII,
)
_DAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS: Final = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_C_LOCALE: Final = re.compile(
    rf"({'|'.join(_DAYS)}) ({'|'.join(_MONTHS)}) ([ 0-9][0-9])"
    r" ([0-9]{2}):([0-9]{2}):([0-9]{2}) ([0-9]{4})",
    re.ASCII,
)
_INT64_MIN: Final = -INT64_MAX - 1


@dataclass(frozen=True)
class Civil:
    """Ticks of the domain, seconds per tick, and whether the text names an instant."""

    ticks: int
    resolution: Fraction
    instant: bool


def read_time(text: str) -> Civil | str:
    """The date-time ``text`` states, or why it is not read."""
    iso = _ISO.fullmatch(text)
    if iso is not None:
        year, month, day, hour, minute, second = (int(part) for part in iso.groups()[:6])
        return _count(year, month, day, hour, minute, second, iso[7] or "", iso[8])
    c_locale = _C_LOCALE.fullmatch(text)
    if c_locale is None:
        return (
            "it is neither an ISO 8601 date-time (YYYY-MM-DD HH:MM:SS[.fraction][zone]) nor the"
            " C locale's date and time (Thu Oct  1 12:00:00 2020)"
        )
    weekday, month_name, day_text, *clock, year_text = c_locale.groups()
    year, month, day = int(year_text), _MONTHS.index(month_name) + 1, int(day_text.strip())
    hour, minute, second = (int(part) for part in clock)
    counted = _count(year, month, day, hour, minute, second, "", None)
    if isinstance(counted, Civil) and _DAYS[datetime(year, month, day).weekday()] != weekday:
        return f"its weekday {weekday} is not the weekday of {year:04}-{month:02}-{day:02}"
    return counted


def _count(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int,
    digits: str,
    zone: str | None,
) -> Civil | str:
    if second == 60:
        return "it is a leap second, which ticks of 86,400-second days (ADR 0023 §2) cannot count"
    try:
        datetime(year, month, day, hour, minute, second)  # range checks only: no zone is implied
    except ValueError as exc:
        return f"it is no date and time of the calendar ({exc})"
    offset = 0
    if zone is not None and zone != "Z":
        hours, minutes = int(zone[1:3]), int(zone[-2:])
        if hours > 23 or minutes > 59:
            return f"its offset {zone} is not one of a clock"
        offset = (-1 if zone[0] == "-" else 1) * (hours * 3600 + minutes * 60)
    scale = 10 ** len(digits)
    seconds = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0))
    ticks = (seconds - offset) * scale + int(digits or "0")
    if not _INT64_MIN <= ticks <= INT64_MAX:
        return "it does not fit 64-bit ticks"
    return Civil(ticks, Fraction(1, scale), zone is not None)

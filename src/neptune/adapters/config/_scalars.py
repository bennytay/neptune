"""Scalars as their format's schema reads them, never coerced further (ADR 0037 §6).

JSON and TOML define their scalar types in their grammars, so their readers already know a
scalar's type; this module only checks that the value fits a record. YAML types a plain scalar by
its text, and the rules depend on the YAML version: what YAML 1.1's type repository reads as a
boolean (``on``, ``yes``, ``y``), an octal integer (``0777``), a sexagesimal number (``1:30``) or a
timestamp (``2001-12-14``), YAML 1.2's core schema reads as text, and back (``1e3`` is text in 1.1
and a float in 1.2). ``resolve`` gives the reading of one version; the adapter compares both.
"""

import base64
import binascii
import math
import re
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from typing import Final, Literal, TypeAlias

from neptune.adapters.config._tree import Issue, Null, Reading, Unreadable, Value
from neptune.model.configuration import ConfigScalar, ScalarType
from neptune.model.scalars import real

YamlVersion: TypeAlias = Literal["1.1", "1.2"]

# Canonical JSON writes an integer's decimal digits, and Python's int/str conversion stops at
# 4,300 digits. 14,000 bits is about 4,215 digits: any integer below it can be written.
INT_BITS: Final = 14_000
# A base 60 number's places beyond which it is over INT_BITS bits: 60 ** (places - 1) > 2 ** 14,000
# from 2,372 places on; any fewer are summed in linear time.
SEXAGESIMAL_PLACES: Final = 2_372

_CORE: Final = "tag:yaml.org,2002:"

# --- YAML 1.1: the type repository (yaml.org/type), as of 2005 ---------------------------------

_V11_TRUE: Final = frozenset(
    ["y", "Y", "yes", "Yes", "YES", "true", "True", "TRUE", "on", "On", "ON"]
)
_V11_FALSE: Final = frozenset(
    ["n", "N", "no", "No", "NO", "false", "False", "FALSE", "off", "Off", "OFF"]
)
_V11_NULL: Final = frozenset(("~", "null", "Null", "NULL", ""))
_V11_INT: Final = re.compile(
    r"[-+]?0b[0-1_]+"  # base 2
    r"|[-+]?0[0-7_]+"  # base 8
    r"|[-+]?(?:0|[1-9][0-9_]*)"  # base 10
    r"|[-+]?0x[0-9a-fA-F_]+"  # base 16
    r"|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+"  # base 60
)
# The repository's base-10 pattern admits several points (``1.2.3``); a number has one.
_V11_FLOAT: Final = re.compile(
    r"[-+]?(?:[0-9][0-9_]*\.[0-9_]*|\.[0-9][0-9_]*)(?:[eE][-+][0-9]+)?"  # base 10
    r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*"  # base 60
    r"|[-+]?\.(?:inf|Inf|INF)"
    r"|\.(?:nan|NaN|NAN)"
)
_V11_DATE: Final = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_V11_TIMESTAMP: Final = re.compile(
    r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})"
    r"(?:[Tt]|[ \t]+)([0-9]{1,2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]*))?"
    r"(?:[ \t]*(Z|([-+])([0-9]{1,2})(?::([0-9]{2}))?))?"
)

# --- YAML 1.2: the core schema (YAML 1.2.2 §10.3) ----------------------------------------------

_V12_TRUE: Final = frozenset(("true", "True", "TRUE"))
_V12_FALSE: Final = frozenset(("false", "False", "FALSE"))
_V12_NULL: Final = frozenset(("~", "null", "Null", "NULL", ""))
_V12_INT: Final = re.compile(r"[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+")
_V12_FLOAT: Final = re.compile(
    r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
    r"|[-+]?\.(?:inf|Inf|INF)"
    r"|\.(?:nan|NaN|NAN)"
)


def integer(value: int) -> Reading:
    """An integer that fits a record, or why not."""
    if value.bit_length() > INT_BITS:
        return Unreadable(Issue.UNREPRESENTABLE, f"an integer of {value.bit_length()} bits")
    return Value((ConfigScalar(ScalarType.INT, value),))


def decimal_integer(text: str) -> Reading:
    """A base-10 integer from its digits (an optional sign, digits, no separators)."""
    digits = len(text.lstrip("+-"))
    if digits > 4_000:
        return Unreadable(Issue.UNREPRESENTABLE, f"an integer of {digits} digits")
    return integer(int(text, 10))


def floating(value: float, text: str) -> Reading:
    """A float the text spells; one that overflows binary64 cannot be held."""
    if math.isinf(value) and "inf" not in text.lower():
        return Unreadable(Issue.UNREPRESENTABLE, "a number beyond binary64's range")
    return Value((ConfigScalar(ScalarType.FLOAT, real(value)),))


def _special_float(text: str) -> float | None:
    """YAML's ``.inf``, ``-.inf`` and ``.nan`` spellings, which Python's ``float`` refuses."""
    body = text.lstrip("+-").lower()
    if body == ".inf":
        return -math.inf if text.startswith("-") else math.inf
    if body == ".nan":
        return math.nan
    return None


def _sign(text: str) -> tuple[int, str]:
    if text[:1] in "+-":
        return (-1 if text[0] == "-" else 1), text[1:]
    return 1, text


def _sexagesimal(text: str) -> float | int | Unreadable:
    """A base 60 number (``1:30:00``), or why it cannot be held. Its first place is at least 1,
    so past ``SEXAGESIMAL_PLACES`` places it is over ``INT_BITS`` bits, and beyond binary64 too,
    without being computed: a long one costs nothing, never quadratic time."""
    sign, body = _sign(text)
    parts = body.split(":")
    if len(parts) > SEXAGESIMAL_PLACES or len(parts[0]) > 4_000:
        return Unreadable(Issue.UNREPRESENTABLE, f"a base 60 number of {len(parts)} places")
    total: float | int = 0
    for part in parts:
        total = total * 60 + (float(part) if "." in part else int(part))
    return sign * total


def _v11_int(text: str) -> Reading:
    clean = text.replace("_", "")
    sign, body = _sign(clean)
    if ":" in body:
        value = _sexagesimal(clean)
        if isinstance(value, Unreadable):
            return value
        assert isinstance(value, int)
        return integer(value)
    if body.startswith("0b"):
        return integer(sign * int(body[2:], 2))
    if body.startswith("0x"):
        return integer(sign * int(body[2:], 16))
    if len(body) > 1 and body.startswith("0"):
        return integer(sign * int(body[1:], 8))
    return decimal_integer(clean)


def _v11_float(text: str) -> Reading:
    special = _special_float(text)
    if special is not None:
        return floating(special, text)
    clean = text.replace("_", "")
    if ":" in clean:
        value = _sexagesimal(clean)
        return value if isinstance(value, Unreadable) else floating(float(value), text)
    return floating(float(clean), text)


def _v12_int(text: str) -> Reading:
    if text.startswith("0o"):
        return integer(int(text[2:], 8))
    if text.startswith("0x"):
        return integer(int(text[2:], 16))
    return decimal_integer(text)


def _v12_float(text: str) -> Reading:
    special = _special_float(text)
    return floating(float(text) if special is None else special, text)


def _datetime_text(value: datetime | date | time) -> str:
    return value.isoformat()


def timestamp(text: str) -> Reading | None:
    """A YAML 1.1 timestamp's reading, with the fields it states and no zone assumed.

    The repository reads a zone-less time as UTC; that is a default, not a declaration, so it is
    a local date-time here and the text still says what was written. ``None``: not a timestamp.
    """
    if _V11_DATE.fullmatch(text):
        year, month, day = (int(part) for part in text.split("-"))
        return _civil(ScalarType.LOCAL_DATE, lambda: date(year, month, day))
    match = _V11_TIMESTAMP.fullmatch(text)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(match[i]) for i in range(1, 7))
    fraction = (match[7] or "")[:6].ljust(6, "0")
    micro = int(fraction)
    if match[8] is None:
        return _civil(
            ScalarType.LOCAL_DATETIME,
            lambda: datetime(year, month, day, hour, minute, second, micro),
        )
    offset = timedelta(0)
    if match[8] != "Z":
        offset = timedelta(hours=int(match[10]), minutes=int(match[11] or 0))
        if match[9] == "-":
            offset = -offset
    return _civil(
        ScalarType.OFFSET_DATETIME,
        lambda: datetime(year, month, day, hour, minute, second, micro, timezone(offset)),
    )


def _civil(kind: ScalarType, build: Callable[[], date | datetime | time]) -> Reading:
    try:
        value = build()
    except (ValueError, OverflowError) as exc:
        return Unreadable(Issue.INVALID_VALUE, f"not a valid date or time ({exc})")
    return Value((ConfigScalar(kind, _datetime_text(value)),))


def binary(text: str) -> Reading:
    """``!!binary``: base64, any whitespace ignored; the reading is its canonical spelling."""
    compact = "".join(text.split())
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return Unreadable(Issue.INVALID_VALUE, "not base64")
    return Value((ConfigScalar(ScalarType.BINARY, base64.b64encode(data).decode("ascii")),))


def _string(text: str) -> Reading:
    return Value((ConfigScalar(ScalarType.STRING, text),))


def _boolean(value: bool) -> Reading:
    return Value((ConfigScalar(ScalarType.BOOL, value),))


def _number(convert: Callable[[str], Reading], text: str) -> Reading:
    """A number the type's pattern admits, which may still have no digits (``0x_``) or be beyond
    what binary64 holds (a base 60 float of many places overflows as it is summed)."""
    try:
        return convert(text)
    except ArithmeticError:
        return Unreadable(Issue.UNREPRESENTABLE, "a number beyond binary64's range")
    except ValueError:
        return Unreadable(Issue.INVALID_VALUE, "the pattern matches, but it has no digits")


def implicit(text: str, version: YamlVersion) -> Reading:
    """A plain, untagged scalar: the type its text has under ``version``'s schema."""
    if version == "1.1":
        if text in _V11_NULL:
            return Null()
        if text in _V11_TRUE or text in _V11_FALSE:
            return _boolean(text in _V11_TRUE)
        if _V11_INT.fullmatch(text):
            return _number(_v11_int, text)
        if _V11_FLOAT.fullmatch(text):
            return _number(_v11_float, text)
        found = timestamp(text)
        return _string(text) if found is None else found
    if text in _V12_NULL:
        return Null()
    if text in _V12_TRUE or text in _V12_FALSE:
        return _boolean(text in _V12_TRUE)
    if _V12_INT.fullmatch(text):
        return _number(_v12_int, text)
    if _V12_FLOAT.fullmatch(text):
        return _number(_v12_float, text)
    return _string(text)


def _invalid(tag: str) -> Unreadable:
    return Unreadable(Issue.INVALID_VALUE, f"the text is not a {tag.removeprefix(_CORE)}")


def tagged(text: str, tag: str, version: YamlVersion) -> Reading:
    """A scalar with an explicit tag: the tag's type, if the text is written as one."""
    if tag in ("!", f"{_CORE}str"):
        return _string(text)
    if tag == f"{_CORE}binary":
        return binary(text)
    if tag == f"{_CORE}timestamp":
        return timestamp(text) or _invalid(tag)
    v11 = version == "1.1"
    nulls, trues, falses = (
        (_V11_NULL, _V11_TRUE, _V11_FALSE) if v11 else (_V12_NULL, _V12_TRUE, _V12_FALSE)
    )
    name = tag.removeprefix(_CORE)
    if tag == f"{_CORE}null":
        return Null() if text in nulls else _invalid(tag)
    if tag == f"{_CORE}bool":
        return _boolean(text in trues) if text in trues or text in falses else _invalid(tag)
    if tag == f"{_CORE}int":
        pattern, convert = (_V11_INT, _v11_int) if v11 else (_V12_INT, _v12_int)
        return _number(convert, text) if pattern.fullmatch(text) else _invalid(tag)
    if tag == f"{_CORE}float":
        pattern, convert = (_V11_FLOAT, _v11_float) if v11 else (_V12_FLOAT, _v12_float)
        return _number(convert, text) if pattern.fullmatch(text) else _invalid(tag)
    if tag.startswith(_CORE) and name in ("map", "seq", "omap", "set", "pairs"):
        return Unreadable(Issue.INVALID_VALUE, f"a scalar cannot be a {name}")
    return Unreadable(Issue.UNRESOLVED_TAG, f"the tag {tag} is the application's to read")


def combine(first: Reading, second: Reading) -> Reading:
    """Two versions' readings of one scalar: one if they agree, both if they are two values.

    A reading only one version can give (the other finds the text invalid for its tag) is no
    reading: which version applies is exactly what the document does not say.
    """
    if first == second:
        return first
    if isinstance(first, Value) and isinstance(second, Value):
        return Value((*first.readings, *second.readings))
    for reading in (first, second):
        if isinstance(reading, Unreadable):
            return reading
    return Unreadable(Issue.AMBIGUOUS_TYPE, "null in one YAML version and a value in the other")

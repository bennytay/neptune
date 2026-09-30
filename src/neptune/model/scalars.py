"""Real numbers as sources write them, including the ones canonical JSON cannot (ADR 0017 §8).

Canonical JSON forbids NaN and ±Infinity (ADR 0002), but sources contain them and sometimes mean
something by them: a URDF or config ``inf`` for an unlimited joint, a laser range ``+inf`` for no
return, a calibration ``.nan`` left unset. A record field whose values come from such data is
typed ``Real``: a finite ``float``, or a ``NonFinite`` value that says which one the source held.
NaN sign and payload bits are not kept; the field's locator still addresses the source bytes.

Parquet series store IEEE values natively and do not use this type.
"""

import math
from enum import StrEnum
from typing import TypeAlias

from neptune.model._fields import exact_object, json_str
from neptune.model.jsonvalue import JsonValue


class NonFinite(StrEnum):
    NAN = "nan"
    POSITIVE_INFINITY = "inf"
    NEGATIVE_INFINITY = "-inf"


Real: TypeAlias = float | NonFinite


def real(value: float) -> Real:
    """A decoded IEEE double as a ``Real``: finite values unchanged, the others as ``NonFinite``."""
    if not isinstance(value, float):
        # 1 and 1.0 are different canonical JSON; the adapter decides once, with float().
        raise TypeError(f"expected a float, got {value!r}")
    if math.isnan(value):
        return NonFinite.NAN
    if math.isinf(value):
        return NonFinite.POSITIVE_INFINITY if value > 0 else NonFinite.NEGATIVE_INFINITY
    return value


def real_to_json(value: Real) -> JsonValue:
    """A finite float as a JSON number; a ``NonFinite`` as ``{"non_finite": "nan"}``.

    The object form cannot be mistaken for a number or for text a source wrote.
    """
    if isinstance(value, NonFinite):
        return {"non_finite": str(value)}
    if not isinstance(value, float) or not math.isfinite(value):
        raise ValueError(f"a Real is a finite float or a NonFinite, got {value!r}")
    return value


def real_from_json(data: JsonValue) -> Real:
    if isinstance(data, float):
        return data
    obj = exact_object(data, "non-finite real", {"non_finite"})
    return NonFinite(json_str(obj["non_finite"], "non_finite"))

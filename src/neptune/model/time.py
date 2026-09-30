"""Time as the evidence records it: integer ticks in a named clock domain (ADR 0005, ADR 0012).

A ``Timestamp`` is ``(ticks, domain_id)``. Nothing here converts ticks to seconds, UTC, another
timescale or another resolution. Ordering and arithmetic are defined only within one domain;
mixing domains raises ``DomainMismatchError``. Relating two domains is a ``ClockAlignment``
record (MVL-36), never an operator.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Final, overload

from neptune.model._fields import (
    check_type,
    enum_decoder,
    exact_object,
    is_int,
    json_bool,
    json_str,
    values_of,
)
from neptune.model.ids import RecordId, check_text, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import Grounding, Knowledge, from_json, to_json

INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1

# Seconds per tick for the resolutions formats commonly declare.
SECOND: Final = Fraction(1)
MILLISECOND: Final = Fraction(1, 10**3)
MICROSECOND: Final = Fraction(1, 10**6)
NANOSECOND: Final = Fraction(1, 10**9)


class DomainMismatchError(TypeError):
    """Two values from different clock domains were ordered or combined (ADR 0005 §6)."""

    def __init__(self, left: RecordId, right: RecordId) -> None:
        super().__init__(
            f"cannot compare or combine times from different domains ({left} vs {right});"
            " relate them through a ClockAlignment record"
        )


def _check_ticks(ticks: int) -> None:
    if isinstance(ticks, bool) or not isinstance(ticks, int):
        raise TypeError(f"ticks must be an int, got {type(ticks).__name__}")
    if not INT64_MIN <= ticks <= INT64_MAX:
        raise ValueError(f"ticks {ticks} do not fit a signed 64-bit integer (ADR 0005 §1)")


def _ticks_of(domain_id: RecordId, other: object, kind: "type[Timestamp | Duration]") -> int:
    """``other``'s ticks, if it is a ``kind`` in the same domain; otherwise raise."""
    if not isinstance(other, kind):
        raise TypeError(f"expected a {kind.__name__}, got {type(other).__name__}")
    if other.domain_id != domain_id:
        raise DomainMismatchError(domain_id, other.domain_id)
    return other.ticks


@dataclass(frozen=True)
class Duration:
    """A signed number of ticks in one domain. Only meaningful in that domain's resolution."""

    ticks: int
    domain_id: RecordId

    def __post_init__(self) -> None:
        _check_ticks(self.ticks)
        parse_record_id(self.domain_id)

    def __add__(self, other: "Duration") -> "Duration":
        return Duration(self.ticks + _ticks_of(self.domain_id, other, Duration), self.domain_id)

    def __sub__(self, other: "Duration") -> "Duration":
        return Duration(self.ticks - _ticks_of(self.domain_id, other, Duration), self.domain_id)

    def __neg__(self) -> "Duration":
        return Duration(-self.ticks, self.domain_id)

    def __lt__(self, other: "Duration") -> bool:
        return self.ticks < _ticks_of(self.domain_id, other, Duration)

    def __le__(self, other: "Duration") -> bool:
        return self.ticks <= _ticks_of(self.domain_id, other, Duration)

    def __gt__(self, other: "Duration") -> bool:
        return self.ticks > _ticks_of(self.domain_id, other, Duration)

    def __ge__(self, other: "Duration") -> bool:
        return self.ticks >= _ticks_of(self.domain_id, other, Duration)

    def to_json(self) -> JsonObject:
        return {"domain_id": self.domain_id, "ticks": self.ticks}


@dataclass(frozen=True)
class Timestamp:
    """An instant as ``ticks`` of the clock ``domain_id`` names, exactly as the source encodes it.

    ``==`` compares records: the same ticks in the same domain. It is not simultaneity, and
    timestamps in different domains are unequal without being "at different times" (ADR 0012 §4).
    """

    ticks: int
    domain_id: RecordId

    def __post_init__(self) -> None:
        _check_ticks(self.ticks)
        parse_record_id(self.domain_id)

    @overload
    def __sub__(self, other: "Timestamp") -> Duration: ...
    @overload
    def __sub__(self, other: Duration) -> "Timestamp": ...
    def __sub__(self, other: "Timestamp | Duration") -> "Duration | Timestamp":
        if isinstance(other, Timestamp):
            return Duration(
                self.ticks - _ticks_of(self.domain_id, other, Timestamp), self.domain_id
            )
        return Timestamp(self.ticks - _ticks_of(self.domain_id, other, Duration), self.domain_id)

    def __add__(self, other: Duration) -> "Timestamp":
        return Timestamp(self.ticks + _ticks_of(self.domain_id, other, Duration), self.domain_id)

    def __lt__(self, other: "Timestamp") -> bool:
        return self.ticks < _ticks_of(self.domain_id, other, Timestamp)

    def __le__(self, other: "Timestamp") -> bool:
        return self.ticks <= _ticks_of(self.domain_id, other, Timestamp)

    def __gt__(self, other: "Timestamp") -> bool:
        return self.ticks > _ticks_of(self.domain_id, other, Timestamp)

    def __ge__(self, other: "Timestamp") -> bool:
        return self.ticks >= _ticks_of(self.domain_id, other, Timestamp)

    def to_json(self) -> JsonObject:
        return {"domain_id": self.domain_id, "ticks": self.ticks}


def timestamp_from_json(data: JsonValue) -> Timestamp:
    ticks, domain_id = _ticks_and_domain(data, "timestamp")
    return Timestamp(ticks, domain_id)


def duration_from_json(data: JsonValue) -> Duration:
    ticks, domain_id = _ticks_and_domain(data, "duration")
    return Duration(ticks, domain_id)


# --- Clock domains ----------------------------------------------------------------------------


class ClockRole(StrEnum):
    """What event a clock's ticks mark (ADR 0012 §2). Which physical clock is timescale + epoch."""

    RECEIVE = "receive"  # a recorder received or logged the data: MCAP log_time, rosbag record time
    PUBLISH = "publish"  # the producer sent it: MCAP publish_time
    SAMPLE = "sample"  # the time the producer attributes to the data: header.stamp, PX4 timestamp
    DOCUMENT = "document"  # a time written in a document or register: an inspection date


class Epoch(StrEnum):
    """What tick zero is."""

    UNIX = "unix"  # 1970-01-01T00:00:00 on the domain's timescale
    GPS = "gps"  # 1980-01-06T00:00:00 GPS time
    BOOT = "boot"  # the recording device's boot
    FIRST_SAMPLE = "first_sample"  # the domain's own first value, e.g. a CSV whose t starts at 0
    SIMULATION_START = "simulation_start"  # the simulator's clock origin


class Timescale(StrEnum):
    """How the clock counts, in particular whether and how it counts leap seconds."""

    UTC = "utc"
    TAI = "tai"
    GPS = "gps"
    POSIX = "posix"  # UTC without leap seconds, as Unix time counts
    MONOTONIC = "monotonic"  # a steady local counter with no civil meaning
    SIMULATED = "simulated"


@dataclass(frozen=True)
class TimestampDomain:
    """One clock of one source (ADR 0005 §2, §3): where its ticks come from and what they mean.

    ``field`` and ``scope`` say which time field of which part of the source the ticks are read
    from, verbatim: ``field="log_time"``, ``scope=("/imu",)``; ``scope=()`` for the whole source.
    The adapter always knows these, so they are structural. Everything that interprets the ticks
    is ``Knowledge``-wrapped and filled only from evidence. Record provenance and
    ``schema_version`` arrive with the entity envelope (MVL-1, MVL-3).
    """

    id: RecordId
    field: str
    scope: tuple[str, ...]
    role: Knowledge[ClockRole]
    resolution: Knowledge[Fraction]  # seconds per tick, exact and positive
    epoch: Knowledge[Epoch]
    timescale: Knowledge[Timescale]
    declared_monotonic: Knowledge[bool]  # as declared; observed violations are findings

    def __post_init__(self) -> None:
        parse_record_id(self.id)
        check_text("field", self.field)
        for part in self.scope:
            check_text("scope part", part)
        check_type("role", self.role, ClockRole)
        check_type("resolution", self.resolution, Fraction)
        if any(value <= 0 for value in values_of(self.resolution)):
            raise ValueError(f"resolution must be positive: {self.resolution}")
        check_type("epoch", self.epoch, Epoch)
        check_type("timescale", self.timescale, Timescale)
        check_type("declared_monotonic", self.declared_monotonic, bool)

    def to_json(self) -> JsonObject:
        return {
            "declared_monotonic": to_json(self.declared_monotonic),
            "epoch": to_json(self.epoch, str),
            "field": self.field,
            "id": self.id,
            "resolution": to_json(self.resolution, resolution_to_json),
            "role": to_json(self.role, str),
            "scope": list(self.scope),
            "timescale": to_json(self.timescale, str),
        }


def timestamp_domain_from_json(
    data: JsonValue, decode_provenance: Callable[[JsonObject], Grounding]
) -> TimestampDomain:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj = exact_object(
        data,
        "timestamp domain",
        {
            "declared_monotonic",
            "epoch",
            "field",
            "id",
            "resolution",
            "role",
            "scope",
            "timescale",
        },
    )
    scope = obj["scope"]
    if not isinstance(scope, list | tuple) or not all(isinstance(p, str) for p in scope):
        raise ValueError("scope must be an array of strings")
    return TimestampDomain(
        id=parse_record_id(json_str(obj["id"], "id")),
        field=json_str(obj["field"], "field"),
        scope=tuple(scope),
        role=from_json(obj["role"], enum_decoder(ClockRole), decode_provenance),
        resolution=from_json(obj["resolution"], resolution_from_json, decode_provenance),
        epoch=from_json(obj["epoch"], enum_decoder(Epoch), decode_provenance),
        timescale=from_json(obj["timescale"], enum_decoder(Timescale), decode_provenance),
        declared_monotonic=from_json(obj["declared_monotonic"], json_bool, decode_provenance),
    )


def resolution_to_json(resolution: Fraction) -> JsonObject:
    return {"denominator": resolution.denominator, "numerator": resolution.numerator}


def resolution_from_json(data: JsonValue) -> Fraction:
    """Exact seconds per tick. Must be positive and in lowest terms, so its bytes are canonical."""
    obj = exact_object(data, "resolution", {"denominator", "numerator"})
    numerator, denominator = obj["numerator"], obj["denominator"]
    if not is_int(numerator) or not is_int(denominator) or numerator <= 0 or denominator <= 0:
        raise ValueError(f"resolution needs positive integers: {numerator!r}/{denominator!r}")
    resolution = Fraction(numerator, denominator)
    if resolution.numerator != numerator:
        raise ValueError(f"resolution is not in lowest terms: {numerator}/{denominator}")
    return resolution


def _ticks_and_domain(data: JsonValue, what: str) -> tuple[int, RecordId]:
    obj = exact_object(data, what, {"domain_id", "ticks"})
    ticks = obj["ticks"]
    if not is_int(ticks):
        raise ValueError(f"{what} ticks must be an integer, got {ticks!r}")
    return ticks, parse_record_id(json_str(obj["domain_id"], "domain_id"))

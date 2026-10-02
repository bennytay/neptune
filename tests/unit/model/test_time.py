from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.identity import canonical_json
from neptune.identity.ids import record_id
from neptune.model import time
from neptune.model.jsonvalue import JsonValue
from neptune.model.time import (
    INT64_MAX,
    INT64_MIN,
    DomainMismatchError,
    Duration,
    Timestamp,
    duration_from_json,
    resolution_from_json,
    resolution_to_json,
    timestamp_from_json,
)

# The TimestampDomain record's tests are in test_reference.py.
LOG_TIME = record_id("timestamp_domain", {"source": "a.mcap", "field": "log_time"})
PUBLISH_TIME = record_id("timestamp_domain", {"source": "a.mcap", "field": "publish_time"})


# --- Acceptance: no conversion, no cross-domain comparison, no clamping ----------------------


def test_timestamp_holds_ticks_exactly_as_encoded() -> None:
    # 2^63 - 1 ns: a float would round this to 2^63; ticks must not move.
    stamp = Timestamp(INT64_MAX, LOG_TIME)
    assert stamp.ticks == 9_223_372_036_854_775_807
    assert canonical_json.dumps(stamp.to_json()).endswith(b'"ticks":9223372036854775807}')


def test_no_conversion_api_exists() -> None:
    public = {name for name in dir(Timestamp(0, LOG_TIME)) if not name.startswith("_")}
    assert public == {"domain_id", "ticks", "to_json"}
    assert {name for name in dir(Duration(0, LOG_TIME)) if not name.startswith("_")} == public
    module_api = {name.lower() for name in dir(time) if not name.startswith("_")}
    for word in ("utc", "to_seconds", "datetime", "convert", "float", "rescale"):
        assert not any(word in name for name in module_api), word


@pytest.mark.parametrize(
    "operation",
    [
        lambda a, b: a < b,
        lambda a, b: a <= b,
        lambda a, b: a > b,
        lambda a, b: a >= b,
        lambda a, b: a - b,
        lambda a, b: sorted([a, b]),
    ],
)
def test_cross_domain_ordering_and_subtraction_raise(operation: object) -> None:
    log, publish = Timestamp(5, LOG_TIME), Timestamp(5, PUBLISH_TIME)
    with pytest.raises(DomainMismatchError, match="ClockMapping"):
        operation(log, publish)  # type: ignore[operator]
    assert issubclass(DomainMismatchError, TypeError)


def test_cross_domain_duration_arithmetic_raises() -> None:
    with pytest.raises(DomainMismatchError):
        _ = Timestamp(5, LOG_TIME) + Duration(1, PUBLISH_TIME)
    with pytest.raises(DomainMismatchError):
        _ = Duration(1, LOG_TIME) + Duration(1, PUBLISH_TIME)
    with pytest.raises(DomainMismatchError):
        _ = Duration(1, LOG_TIME) < Duration(1, PUBLISH_TIME)


def test_equality_is_record_identity_not_simultaneity() -> None:
    assert Timestamp(5, LOG_TIME) == Timestamp(5, LOG_TIME)
    assert Timestamp(5, LOG_TIME) != Timestamp(5, PUBLISH_TIME)
    # Hashable, so mixed-domain sets and dicts work; only ordering is refused.
    assert len({Timestamp(5, LOG_TIME), Timestamp(5, PUBLISH_TIME)}) == 2


@pytest.mark.parametrize("ticks", [INT64_MAX + 1, INT64_MIN - 1, 2**100])
def test_out_of_range_ticks_are_rejected_not_clamped(ticks: int) -> None:
    with pytest.raises(ValueError, match="64-bit"):
        Timestamp(ticks, LOG_TIME)
    with pytest.raises(ValueError, match="64-bit"):
        Duration(ticks, LOG_TIME)


def test_arithmetic_overflow_is_rejected_not_wrapped() -> None:
    with pytest.raises(ValueError, match="64-bit"):
        _ = Timestamp(INT64_MAX, LOG_TIME) + Duration(1, LOG_TIME)
    with pytest.raises(ValueError, match="64-bit"):
        _ = Timestamp(INT64_MAX, LOG_TIME) - Timestamp(INT64_MIN, LOG_TIME)


@pytest.mark.parametrize("ticks", [1.0, 1.5, True, "1", Fraction(1)])
def test_ticks_must_be_int(ticks: object) -> None:
    with pytest.raises(TypeError):
        Timestamp(ticks, LOG_TIME)  # type: ignore[arg-type]


@pytest.mark.parametrize("domain", ["", "log_time", "sha256:" + "0" * 64])
def test_domain_must_be_a_record_id(domain: str) -> None:
    with pytest.raises(ValueError):
        Timestamp(0, domain)  # type: ignore[arg-type]


# --- Within one domain -----------------------------------------------------------------------


def test_same_domain_arithmetic_and_ordering() -> None:
    a, b = Timestamp(100, LOG_TIME), Timestamp(250, LOG_TIME)
    step = b - a
    assert step == Duration(150, LOG_TIME)
    assert a + step == b
    assert b - step == a
    assert a < b <= b and b > a >= a
    assert -step == Duration(-150, LOG_TIME)
    assert step + step - step == step
    assert Duration(1, LOG_TIME) < step
    assert sorted([b, a]) == [a, b]


def test_negative_ticks_are_ordinary() -> None:
    # Pre-epoch and boot-relative clocks can be negative; nothing treats <0 as missing.
    assert Timestamp(-1, LOG_TIME) < Timestamp(0, LOG_TIME)


def test_timestamps_and_durations_do_not_mix() -> None:
    with pytest.raises(TypeError):
        _ = Timestamp(1, LOG_TIME) < Duration(1, LOG_TIME)  # type: ignore[operator]
    with pytest.raises(TypeError):
        _ = Duration(1, LOG_TIME) + Timestamp(1, LOG_TIME)  # type: ignore[operator]
    with pytest.raises(TypeError):
        _ = Timestamp(1, LOG_TIME) + Timestamp(1, LOG_TIME)  # type: ignore[operator]
    with pytest.raises(TypeError):
        _ = Timestamp(1, LOG_TIME) + 1  # type: ignore[operator]


# --- JSON ------------------------------------------------------------------------------------


def test_timestamp_and_duration_json() -> None:
    stamp = Timestamp(-42, LOG_TIME)
    encoded = canonical_json.dumps(stamp.to_json())
    assert encoded == b'{"domain_id":"' + LOG_TIME.encode() + b'","ticks":-42}'
    assert timestamp_from_json(canonical_json.loads(encoded)) == stamp
    assert duration_from_json(Duration(7, LOG_TIME).to_json()) == Duration(7, LOG_TIME)


@pytest.mark.parametrize(
    "data",
    [
        None,
        {"ticks": 1},
        {"ticks": 1.0, "domain_id": LOG_TIME},
        {"ticks": True, "domain_id": LOG_TIME},
        {"ticks": "1", "domain_id": LOG_TIME},
        {"ticks": 2**63, "domain_id": LOG_TIME},
        {"ticks": 1, "domain_id": "x"},
        {"ticks": 1, "domain_id": LOG_TIME, "unit": "ns"},
    ],
)
def test_timestamp_from_json_is_strict(data: JsonValue) -> None:
    with pytest.raises((ValueError, TypeError)):
        timestamp_from_json(data)


@pytest.mark.parametrize(
    "data",
    [
        {"numerator": 2, "denominator": 2_000_000_000},
        {"numerator": 0, "denominator": 1},
        {"numerator": -1, "denominator": 1000},
        {"numerator": 1, "denominator": 0},
        {"numerator": 1.0, "denominator": 1000},
        {"numerator": True, "denominator": 1000},
        {"numerator": 1},
        "1/1000",
        1e-9,
    ],
)
def test_resolution_from_json_is_strict(data: JsonValue) -> None:
    with pytest.raises(ValueError):
        resolution_from_json(data)


# --- Determinism -----------------------------------------------------------------------------

domain_ids = st.sampled_from([LOG_TIME, PUBLISH_TIME])
ticks = st.integers(INT64_MIN, INT64_MAX)
resolutions = st.fractions(min_value=Fraction(1, 10**12), max_value=Fraction(3600)).filter(
    lambda f: f > 0
)


@given(ticks, domain_ids)
def test_timestamp_round_trip_property(value: int, domain: str) -> None:
    stamp = Timestamp(value, domain)  # type: ignore[arg-type]
    encoded = canonical_json.dumps(stamp.to_json())
    assert timestamp_from_json(canonical_json.loads(encoded)) == stamp


@given(resolutions)
def test_resolution_round_trip_property(resolution: Fraction) -> None:
    encoded = canonical_json.dumps(resolution_to_json(resolution))
    assert resolution_from_json(canonical_json.loads(encoded)) == resolution

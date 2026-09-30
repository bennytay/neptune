from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.identity import canonical_json
from neptune.identity.ids import record_id
from neptune.model import time
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    Ambiguous,
    Candidate,
    Known,
    KnownAbsent,
    NotApplicable,
    Unknown,
)
from neptune.model.time import (
    INT64_MAX,
    INT64_MIN,
    MICROSECOND,
    NANOSECOND,
    ClockRole,
    DomainMismatchError,
    Duration,
    Epoch,
    Timescale,
    Timestamp,
    TimestampDomain,
    duration_from_json,
    resolution_from_json,
    resolution_to_json,
    timestamp_domain_from_json,
    timestamp_from_json,
)


@dataclass(frozen=True)
class Cite:
    """Stand-in for MVL-3's Provenance: anything with ``to_json``."""

    where: str

    def to_json(self) -> JsonObject:
        return {"where": self.where}


def cite(data: JsonObject) -> Cite:
    where = data["where"]
    assert isinstance(where, str)
    return Cite(where)


LOG_TIME = record_id("timestamp_domain", {"source": "a.mcap", "field": "log_time"})
PUBLISH_TIME = record_id("timestamp_domain", {"source": "a.mcap", "field": "publish_time"})
MCAP_SPEC = Cite("mcap spec: log_time, uint64 nanoseconds")


def mcap_log_time() -> TimestampDomain:
    """What an MCAP adapter can honestly say about log_time: ns resolution, epoch unstated."""
    return TimestampDomain(
        id=LOG_TIME,
        field="log_time",
        scope=(),
        role=Known(ClockRole.RECEIVE, MCAP_SPEC),
        resolution=Known(NANOSECOND, MCAP_SPEC),
        epoch=Unknown(MCAP_SPEC),
        timescale=Unknown(MCAP_SPEC),
        declared_monotonic=Unknown(),
    )


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
    with pytest.raises(DomainMismatchError, match="ClockAlignment"):
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


# --- TimestampDomain -------------------------------------------------------------------------


def test_domain_properties_are_evidence_only() -> None:
    domain = mcap_log_time()
    assert domain.epoch == Unknown(MCAP_SPEC)  # never assumed to be Unix
    assert domain.timescale == Unknown(MCAP_SPEC)  # never assumed to be UTC


def test_naive_text_timestamp_has_no_assumed_zone() -> None:
    # "2026-09-14 10:32" in operator notes: no zone, so tick zero is neither UTC nor host-local
    # midnight 1970 (ADR 0005 §5). The adapter records what it cannot know, plus a finding.
    no_zone = Cite("notes.xlsx#Sheet1!B2: no zone or offset stated")
    domain = replace(
        mcap_log_time(),
        field="Inspection time",
        scope=("notes.xlsx", "Sheet1"),
        role=Known(ClockRole.DOCUMENT),
        resolution=Known(Fraction(60)),
        epoch=Unknown(no_zone),
        timescale=Unknown(no_zone),
    )
    assert domain.epoch.state == domain.timescale.state == "unknown"


def test_conflicting_declarations_stay_ambiguous() -> None:
    # A driver README says µs, the CSV header says ms: both are kept, neither wins.
    resolution = Ambiguous(
        (Candidate(MICROSECOND, Cite("README.md")), Candidate(Fraction(1, 1000), Cite("csv#h")))
    )
    domain = replace(mcap_log_time(), resolution=resolution)
    assert [c.value for c in domain.resolution.candidates] == [MICROSECOND, Fraction(1, 1000)]  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "change",
    [
        {"resolution": Known(1e-9)},
        {"resolution": Known(1)},
        {"resolution": Known(Fraction(0))},
        {"resolution": Known(Fraction(-1, 1000))},
        {"resolution": Ambiguous((Candidate(NANOSECOND), Candidate(-MICROSECOND)))},
        {"epoch": Known("unix")},
        {"timescale": Known(Epoch.GPS)},
        {"role": Known("receive")},
        {"declared_monotonic": Known(1)},
        {"field": ""},
        {"scope": ("",)},
        {"id": "log_time"},
    ],
)
def test_domain_rejects_malformed_properties(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(mcap_log_time(), **change)


# --- JSON ------------------------------------------------------------------------------------


def test_timestamp_and_duration_json() -> None:
    stamp = Timestamp(-42, LOG_TIME)
    encoded = canonical_json.dumps(stamp.to_json())
    assert encoded == b'{"domain_id":"' + LOG_TIME.encode() + b'","ticks":-42}'
    assert timestamp_from_json(canonical_json.loads(encoded)) == stamp
    assert duration_from_json(Duration(7, LOG_TIME).to_json()) == Duration(7, LOG_TIME)


def test_domain_json_shape() -> None:
    encoded = canonical_json.dumps(mcap_log_time().to_json())
    assert canonical_json.loads(encoded) == {
        "declared_monotonic": {"knowledge": "unknown"},
        "epoch": {"knowledge": "unknown", "provenance": MCAP_SPEC.to_json()},
        "field": "log_time",
        "id": LOG_TIME,
        "resolution": {
            "knowledge": "known",
            "provenance": MCAP_SPEC.to_json(),
            "value": {"denominator": 1_000_000_000, "numerator": 1},
        },
        "role": {"knowledge": "known", "provenance": MCAP_SPEC.to_json(), "value": "receive"},
        "scope": [],
        "timescale": {"knowledge": "unknown", "provenance": MCAP_SPEC.to_json()},
    }
    assert b"null" not in encoded


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


def mutate(key: str, value: JsonValue) -> JsonValue:
    data = dict(canonical_json.loads(canonical_json.dumps(mcap_log_time().to_json())))  # type: ignore[arg-type]
    data[key] = value
    return data


@pytest.mark.parametrize(
    "data",
    [
        mutate("epoch", {"knowledge": "known", "value": "UNIX"}),
        mutate("epoch", {"knowledge": "known", "value": "local"}),
        mutate("role", "receive"),
        mutate("declared_monotonic", {"knowledge": "known", "value": 1}),
        mutate("scope", "/imu"),
        mutate("scope", [1]),
        mutate("field", 3),
        mutate("confidence", 0.9),
        {k: v for k, v in mcap_log_time().to_json().items() if k != "epoch"},
    ],
)
def test_domain_from_json_is_strict(data: JsonValue) -> None:
    with pytest.raises(ValueError):
        timestamp_domain_from_json(data, cite)


# --- Determinism -----------------------------------------------------------------------------

domain_ids = st.sampled_from([LOG_TIME, PUBLISH_TIME])
ticks = st.integers(INT64_MIN, INT64_MAX)
provenance = st.sampled_from([Cite("a"), Cite("b")])


def states(values: st.SearchStrategy[object]) -> st.SearchStrategy[object]:
    return st.one_of(
        st.builds(Known, values, provenance),
        st.builds(KnownAbsent, provenance),
        st.builds(Unknown, provenance),
        st.just(NotApplicable()),
        st.lists(values, min_size=2, max_size=3, unique=True).map(
            lambda vs: Ambiguous(tuple(Candidate(v) for v in vs))
        ),
    )


resolutions = st.fractions(min_value=Fraction(1, 10**12), max_value=Fraction(3600)).filter(
    lambda f: f > 0
)
text = st.text(st.characters(codec="utf-8"), min_size=1, max_size=8)
domains = st.builds(
    TimestampDomain,
    id=domain_ids,
    field=text,
    scope=st.lists(text, max_size=3).map(tuple),
    role=states(st.sampled_from(ClockRole)),
    resolution=states(resolutions),
    epoch=states(st.sampled_from(Epoch)),
    timescale=states(st.sampled_from(Timescale)),
    declared_monotonic=states(st.booleans()),
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


@given(domains)
def test_domain_round_trip_is_byte_identical(domain: TimestampDomain) -> None:
    encoded = canonical_json.dumps(domain.to_json())
    decoded = timestamp_domain_from_json(canonical_json.loads(encoded), cite)
    assert decoded == domain
    assert canonical_json.dumps(decoded.to_json()) == encoded

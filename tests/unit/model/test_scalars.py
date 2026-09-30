"""Non-finite reals: kept as what the source wrote, never as bare NaN tokens (ADR 0017 §8)."""

import math
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.identity import canonical_json
from neptune.model.knowledge import Known, from_json, to_json
from neptune.model.provenance import provenance_from_json
from neptune.model.scalars import NonFinite, Real, real, real_from_json, real_to_json


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (float("nan"), NonFinite.NAN),
        (-float("nan"), NonFinite.NAN),  # the sign of a NaN is not kept
        (float("inf"), NonFinite.POSITIVE_INFINITY),
        (-float("inf"), NonFinite.NEGATIVE_INFINITY),
        (1.5, 1.5),
        (-0.0, -0.0),
    ],
)
def test_decoded_doubles_become_reals(value: float, expected: Real) -> None:
    assert real(value) == expected
    if isinstance(expected, float):
        assert math.copysign(1.0, real(value)) == math.copysign(1.0, expected)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [1, True, "nan", None])
def test_only_floats_are_decoded(value: Any) -> None:
    with pytest.raises(TypeError):
        real(value)


def test_json_form_cannot_be_mistaken_for_a_number_or_text() -> None:
    assert real_to_json(NonFinite.POSITIVE_INFINITY) == {"non_finite": "inf"}
    assert real_to_json(NonFinite.NEGATIVE_INFINITY) == {"non_finite": "-inf"}
    assert real_to_json(NonFinite.NAN) == {"non_finite": "nan"}
    assert real_to_json(0.25) == 0.25
    for value in NonFinite:
        encoded = canonical_json.dumps(real_to_json(value))
        assert real_from_json(canonical_json.loads(encoded)) is value


def test_an_unlimited_joint_is_known_infinity_not_unknown() -> None:
    # A URDF or config that writes `inf` for a velocity limit states "unlimited".
    state = Known(real(float("inf")))
    encoded = canonical_json.dumps(to_json(state, real_to_json))
    assert encoded == b'{"knowledge":"known","value":{"non_finite":"inf"}}'
    assert from_json(canonical_json.loads(encoded), real_from_json, provenance_from_json) == state


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1, "1.0"])
def test_bare_non_finite_or_non_float_values_are_not_encoded(value: Any) -> None:
    with pytest.raises(ValueError):
        real_to_json(value)


@pytest.mark.parametrize(
    "data",
    [1, True, "nan", "inf", None, {"non_finite": "NaN"}, {"non_finite": "infinity"}, {"x": "nan"}],
)
def test_json_is_parsed_strictly(data: Any) -> None:
    with pytest.raises(ValueError):
        real_from_json(data)


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_finite_reals_round_trip_exactly(value: float) -> None:
    encoded = canonical_json.dumps(real_to_json(real(value)))
    decoded = real_from_json(canonical_json.loads(encoded))
    assert isinstance(decoded, float)
    assert decoded == value and math.copysign(1.0, decoded) == math.copysign(1.0, value)

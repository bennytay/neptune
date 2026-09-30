import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.identity.canonical_json import CanonicalJsonError, dumps, loads
from neptune.model.jsonvalue import JsonValue

# Valid Unicode text only: canonical JSON rejects lone surrogates by design.
text = st.text(st.characters(codec="utf-8"))
json_values: st.SearchStrategy[JsonValue] = st.recursive(
    st.booleans()
    | st.integers()
    | st.integers(min_value=-(2**200), max_value=2**200)
    | st.floats(allow_nan=False, allow_infinity=False)
    | text,
    lambda children: st.lists(children) | st.dictionaries(text, children),
    max_leaves=30,
)


def test_no_whitespace_and_keys_sorted() -> None:
    assert (
        dumps({"b": [1, 2], "a": {"d": True, "c": False}})
        == b'{"a":{"c":false,"d":true},"b":[1,2]}'
    )


def test_keys_sort_by_code_point_not_utf16() -> None:
    # U+FB01 < U+1F600 by code point; under UTF-16 ordering (JCS) the surrogate 0xD83D sorts first.
    assert dumps({"\U0001f600": 1, "ﬁ": 2}) == '{"ﬁ":2,"\U0001f600":1}'.encode()


def test_strings_are_emitted_as_given() -> None:
    # No NFC: precomposed and decomposed e-acute stay distinct.
    assert dumps("é") != dumps("é")
    unusual = "\u00e9/" + chr(0x2028) + chr(0x7F)
    assert dumps(unusual) == ('"' + unusual + '"').encode()


def test_only_required_escapes_in_short_form() -> None:
    assert dumps('"\\\b\t\n\f\r\x00\x1f') == b'"\\"\\\\\\b\\t\\n\\f\\r\\u0000\\u001f"'


@pytest.mark.parametrize("value", [2**53 + 1, 2**64 + 1, -(2**100)])
def test_integers_are_exact(value: int) -> None:
    assert dumps(value) == str(value).encode()
    assert loads(dumps(value)) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, b"1.0"),
        (-0.0, b"-0.0"),
        (0.1, b"0.1"),
        (1e16, b"1e+16"),
        (1e-5, b"1e-05"),
        (5e-324, b"5e-324"),
        (1.7976931348623157e308, b"1.7976931348623157e+308"),
    ],
)
def test_floats_shortest_round_trip_and_stay_floats(value: float, expected: bytes) -> None:
    assert dumps(value) == expected
    decoded = loads(expected)
    assert isinstance(decoded, float)
    assert math.copysign(1.0, decoded) == math.copysign(1.0, value)
    assert decoded == value


def test_bool_is_not_int() -> None:
    assert dumps([True, False, 1, 0]) == b"[true,false,1,0]"


def test_tuple_encodes_as_array() -> None:
    assert dumps((1, "a")) == dumps([1, "a"])


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"a": None},
        [float("nan")],
        float("inf"),
        float("-inf"),
        b"bytes",
        {1: "non-str key"},
        {"k": object()},
        "\ud800",
        {"\udfff": 1},
    ],
)
def test_rejects_non_canonical_values(value: object) -> None:
    with pytest.raises(CanonicalJsonError):
        dumps(value)  # type: ignore[arg-type]


def test_rejects_integers_beyond_digit_limit() -> None:
    with pytest.raises(CanonicalJsonError):
        dumps(10**5000)


def test_rejects_excessive_nesting() -> None:
    value: JsonValue = []
    for _ in range(100_000):
        value = [value]
    with pytest.raises(CanonicalJsonError):
        dumps(value)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"null",
        b'{"a":null}',
        b"NaN",
        b"[Infinity]",
        b'{"a":1,"a":2}',
        b'{"b":1,"a":2}',
        b'{"a": 1}',
        b"[1]\n",
        b"\xef\xbb\xbf[1]",
        b"1E5",
        b"1e5",
        b"1.50",
        b'"\\u00e9"',
        b'"\\ud800"',
        b'"\xff"',
        b"[" * 100_000,
        b"1" * 5000,
    ],
)
def test_loads_rejects_non_canonical_bytes(data: bytes) -> None:
    with pytest.raises(CanonicalJsonError):
        loads(data)


@given(json_values)
def test_round_trip_is_byte_identical(value: JsonValue) -> None:
    encoded = dumps(value)
    assert dumps(loads(encoded)) == encoded


@given(st.dictionaries(text, st.integers()))
def test_insertion_order_does_not_matter(value: dict[str, int]) -> None:
    reordered = dict(reversed(list(value.items())))
    assert dumps(value) == dumps(reordered)

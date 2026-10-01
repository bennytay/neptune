"""Xacro's ``${...}`` expressions without ``eval``: Python's results, bounded, closed (ADR 0039 §4).

The oracle for arithmetic is Python itself, run on expressions the test generates (never on
input): the evaluator must agree with it exactly, value and type, or refuse.
"""

import math
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from neptune.adapters.urdf.expression import (
    MAX_EXPRESSION_CHARS,
    ExpressionError,
    Undefined,
    Unsupported,
    Value,
    boolean,
    evaluate,
    literal,
)

SYMBOLS: Final[dict[str, Value]] = {"r": 0.05, "n": 4, "name": "wheel", "flag": True}


def lookup(name: str) -> Value:
    if name in SYMBOLS:
        return SYMBOLS[name]
    raise Undefined(name)


def ev(text: str) -> Value:
    return evaluate(text, lookup)


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1 + 1", 2),
        ("r * 2", 0.1),
        ("n / 2", 2.0),
        ("7 // 2", 3),
        ("-7 % 3", 2),
        ("2 ** 10", 1024),
        ("pi / 2", math.pi / 2),
        ("math.pi", math.pi),
        ("radians(180)", math.pi),
        ("math.cos(0)", 1.0),
        ("sqrt(16)", 4.0),
        ("atan2(1, 1)", math.atan2(1, 1)),
        ("abs(-3)", 3),
        ("min(3, n, 9)", 3),
        ("max(r, 1)", 1),
        ("round(2.567, 2)", 2.57),
        ("int('7')", 7),
        ("float(n)", 4.0),
        ("str(n) + '_' + name", "4_wheel"),
        ("len(name)", 5),
        ("name == 'wheel'", True),
        ("1 < n <= 4", True),
        ("1 < n < 4", False),
        ("not flag", False),
        ("flag and n", 4),
        ("0 or name", "wheel"),
        ("'a' if n > 3 else 'b'", "a"),
        ("-r", -0.05),
        ("+n", 4),
        ("1e-7", 1e-07),
        ("inf", math.inf),
    ],
)
def test_expressions_evaluate_as_python_does(text: str, value: Value) -> None:
    result = ev(text)
    assert result == value and type(result) is type(value)


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("undefined_name", Undefined),
        ("__import__('os')", Unsupported),
        ("().__class__", Unsupported),
        ("[1, 2][0]", Unsupported),
        ("{'a': 1}", Unsupported),
        ("(lambda: 1)()", Unsupported),
        ("[x for x in range(3)]", Unsupported),
        ("open('/etc/passwd')", Unsupported),
        ("max(n, key=abs)", Unsupported),
        ("max(*[1, 2])", Unsupported),
        ("'a' * 3", Unsupported),
        ("'%999999999d' % 1", Unsupported),
        ("'%.2147483646f' % 1.0", Unsupported),
        ("name % n", Unsupported),
        ("1j", Unsupported),
        ("None", Unsupported),
        ("math.factorial(5)", Unsupported),
        ("n.real", Unsupported),
        ("1 in [1]", Unsupported),
        ("1 is 1", Unsupported),
        ("~n", Unsupported),
        ("9 ** 9 ** 9", ExpressionError),
        ("2 ** 64", ExpressionError),
        ("10 ** 300 * 1.0", ExpressionError),
        ("math.exp(1000)", ExpressionError),
        ("1 / 0", ExpressionError),
        ("1 % 0", ExpressionError),
        ("'a' < 1", ExpressionError),
        ("sqrt(-1)", ExpressionError),
        ("(-1) ** 0.5", Unsupported),
        ("1 +", ExpressionError),
        ("(" * 300 + "1" + ")" * 300, ExpressionError),
        ("+".join(["1"] * 100), ExpressionError),
        ("1" * (MAX_EXPRESSION_CHARS + 1), ExpressionError),
        ("\x00", ExpressionError),
    ],
)
def test_what_is_not_evaluated_is_refused(text: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        ev(text)


def test_an_integer_too_large_for_64_bits_is_refused_at_every_step() -> None:
    assert ev("2 ** 62 + (2 ** 62 - 1)") == 2**63 - 1
    with pytest.raises(ExpressionError):
        ev("2 ** 62 + 2 ** 62")
    with pytest.raises(ExpressionError):
        ev("round(1e300)")


def test_strings_are_bounded() -> None:
    assert ev("str(n) + str(n)") == "44"
    long = "x" * 40_000

    def big(name: str) -> Value:
        return long

    with pytest.raises(ExpressionError):
        evaluate("a + a", big)


# --- Xacro's literal and boolean readings ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1", 1),
        ("-2.5", -2.5),
        ("1e3", 1000.0),
        ("'01'", "01"),
        ("1_000", "1_000"),
        ("true", True),
        ("False", False),
        ("left", "left"),
        ("0.1 0.2", "0.1 0.2"),
        (" 7 ", 7),
    ],
)
def test_property_text_reads_as_xacro_reads_it(text: str, value: Value) -> None:
    result = literal(text)
    assert result == value and type(result) is type(value)


@pytest.mark.parametrize(
    ("value", "result"),
    [("true", True), ("True", True), ("false", False), ("1", True), ("0", False), (2.0, True)],
)
def test_conditions_read_as_xacro_reads_them(value: Value, result: bool) -> None:
    assert boolean(value) is result


@pytest.mark.parametrize("value", ["yes", "1.0", "", "TRUE"])
def test_other_conditions_are_not_booleans(value: str) -> None:
    with pytest.raises(ExpressionError):
        boolean(value)


# --- Python as the oracle ----------------------------------------------------------------------

numbers = st.one_of(
    st.integers(-1000, 1000), st.floats(-1e3, 1e3, allow_nan=False, allow_infinity=False)
)
operators = st.sampled_from(["+", "-", "*", "/", "//", "%"])


@st.composite
def arithmetic(draw: st.DrawFn, depth: int = 3) -> str:
    if depth == 0 or draw(st.booleans()):
        return repr(draw(numbers))
    left, right = draw(arithmetic(depth - 1)), draw(arithmetic(depth - 1))
    return f"({left} {draw(operators)} {right})"


@settings(max_examples=300, deadline=None)
@given(arithmetic())
def test_arithmetic_agrees_with_python(text: str) -> None:
    try:
        expected = eval(text, {"__builtins__": {}})  # generated arithmetic only
    except ArithmeticError:
        with pytest.raises(ExpressionError):
            ev(text)
        return
    if isinstance(expected, int) and abs(expected) >= 2**63:
        return
    result = ev(text)
    assert type(result) is type(expected)
    assert result == expected or (math.isnan(result) and math.isnan(expected))  # type: ignore[arg-type]

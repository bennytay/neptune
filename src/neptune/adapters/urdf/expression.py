"""Xacro's ``${...}`` expressions, evaluated without ``eval`` (ADR 0039 §4).

Xacro evaluates expressions as Python with a few builtins and ``math``. Hostile text must never
reach ``eval``, so this module parses the expression with ``ast`` and walks a closed set of nodes:
literals, names, arithmetic, comparisons, boolean logic, conditional expressions, ``math`` and a
few builtins. Anything else (attribute access beyond ``math.``, subscripts, comprehensions,
lambdas, keywords, starred arguments) is ``Unsupported``. Every value is bounded: an integer to
64 bits, a string to ``MAX_STRING``, the expression's text and tree to fixed sizes, so a crafted
``9**9**9`` or ``'a'*10**9`` is an error, never a hang.

Names resolve through the caller's ``lookup`` first (properties and macro parameters), then
``math``'s constants and functions, then the builtins, as xacro resolves them.
"""

import ast
import math
from collections.abc import Callable
from typing import Final, TypeAlias

Value: TypeAlias = int | float | str | bool

MAX_EXPRESSION_CHARS: Final = 1024
MAX_NODES: Final = 256
MAX_DEPTH: Final = 32
MAX_STRING: Final = 64 * 1024
_INT_BOUND: Final = 2**63


class ExpressionError(Exception):
    """The expression is invalid: bad syntax, a type error, a failing operation, a bound hit."""


class Unsupported(ExpressionError):
    """The expression uses Python that Neptune does not evaluate."""


class Undefined(ExpressionError):
    """A name the expression uses is not defined."""

    def __init__(self, name: str) -> None:
        super().__init__(f"{name!r} is not defined")
        self.name = name


def _math_functions() -> dict[str, Callable[..., Value]]:
    names = (
        "acos acosh asin asinh atan atan2 atanh ceil copysign cos cosh degrees erf erfc exp expm1"
        " fabs floor fmod hypot isclose isfinite isinf isnan ldexp log log10 log1p log2 radians"
        " remainder sin sinh sqrt tan tanh trunc"
    )
    return {name: getattr(math, name) for name in names.split()}


MATH_FUNCTIONS: Final = _math_functions()
MATH_CONSTANTS: Final[dict[str, Value]] = {
    "e": math.e,
    "inf": math.inf,
    "nan": math.nan,
    "pi": math.pi,
    "tau": math.tau,
}


def _bounded_pow(base: Value, exponent: Value) -> Value:
    integers = isinstance(base, int) and isinstance(exponent, int)
    if integers and abs(exponent) > 64 and abs(base) > 1:  # type: ignore[arg-type]
        raise ExpressionError("an integer power is too large")
    return _check(base**exponent)  # type: ignore[operator]


def _bounded_round(number: Value, digits: Value | None = None) -> Value:
    # round(int, -n) computes 10**n first; past 64 digits an int in range rounds to 0 anyway.
    if isinstance(digits, int) and abs(digits) > 64:
        raise ExpressionError("round's digit count is too large")
    return _check(round(number, digits))  # type: ignore[arg-type]


BUILTINS: Final[dict[str, Callable[..., Value]]] = {
    "abs": abs,
    "bool": bool,
    "float": float,
    "int": int,
    "len": len,
    "max": max,
    "min": min,
    "pow": _bounded_pow,
    "round": _bounded_round,
    "str": str,
}

_BINARY: Final[dict[type[ast.operator], Callable[[Value, Value], Value]]] = {
    ast.Add: lambda a, b: a + b,  # type: ignore[operator]
    ast.Sub: lambda a, b: a - b,  # type: ignore[operator]
    ast.Mult: lambda a, b: a * b,  # type: ignore[operator]
    ast.Div: lambda a, b: a / b,  # type: ignore[operator]
    ast.FloorDiv: lambda a, b: a // b,  # type: ignore[operator]
    ast.Mod: lambda a, b: a % b,  # type: ignore[operator]
    ast.Pow: _bounded_pow,
}
_COMPARE: Final[dict[type[ast.cmpop], Callable[[Value, Value], bool]]] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,  # type: ignore[operator]
    ast.LtE: lambda a, b: a <= b,  # type: ignore[operator]
    ast.Gt: lambda a, b: a > b,  # type: ignore[operator]
    ast.GtE: lambda a, b: a >= b,  # type: ignore[operator]
}


def _check(value: object) -> Value:
    """``value`` if it is a bounded int, float, str or bool; otherwise an ``ExpressionError``."""
    if isinstance(value, bool | float):
        return value
    if isinstance(value, int):
        if abs(value) >= _INT_BOUND:
            raise ExpressionError("an integer is out of the 64-bit range")
        return value
    if isinstance(value, str):
        if len(value) > MAX_STRING:
            raise ExpressionError("a string is too long")
        return value
    raise Unsupported(f"a value of type {type(value).__name__}")


def _shape(tree: ast.AST) -> None:
    """Refuse trees too large or too deep to walk safely."""
    stack, count = [(tree, 0)], 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise ExpressionError("the expression is too large")
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))


def parse(text: str) -> ast.expr:
    """The expression's tree, or ``ExpressionError``."""
    if len(text) > MAX_EXPRESSION_CHARS:
        raise ExpressionError("the expression is too long")
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        raise ExpressionError("the expression is not valid") from exc
    _shape(tree)
    return tree.body


class _Evaluator:
    def __init__(self, lookup: Callable[[str], Value]) -> None:
        self.lookup = lookup

    def name(self, name: str) -> Value:
        if name.startswith("__"):
            raise Unsupported(f"the name {name!r}")
        try:
            return self.lookup(name)
        except Undefined:
            if name in MATH_CONSTANTS:
                return MATH_CONSTANTS[name]
            raise

    def function(self, node: ast.expr) -> Callable[..., Value]:
        if isinstance(node, ast.Name):
            if node.id in MATH_FUNCTIONS:
                return MATH_FUNCTIONS[node.id]
            if node.id in BUILTINS:
                return BUILTINS[node.id]
            raise Unsupported(f"the function {node.id!r}")
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "math"
            and node.attr in MATH_FUNCTIONS
        ):
            return MATH_FUNCTIONS[node.attr]
        raise Unsupported("a call to something other than math or a builtin")

    def evaluate(self, node: ast.expr) -> Value:
        match node:
            case ast.Constant(value=value) if isinstance(value, int | float | str | bool):
                return _check(value)
            case ast.Name(id=name):
                return self.name(name)
            case ast.Attribute(value=ast.Name(id="math"), attr=attr) if attr in MATH_CONSTANTS:
                return MATH_CONSTANTS[attr]
            case ast.UnaryOp(op=ast.USub(), operand=operand):
                return _check(-self.evaluate(operand))  # type: ignore[operator]
            case ast.UnaryOp(op=ast.UAdd(), operand=operand):
                return _check(+self.evaluate(operand))  # type: ignore[operator]
            case ast.UnaryOp(op=ast.Not(), operand=operand):
                return not self.evaluate(operand)
            case ast.BinOp(left=left, op=op, right=right) if type(op) in _BINARY:
                a, b = self.evaluate(left), self.evaluate(right)
                if isinstance(op, ast.Mult) and (isinstance(a, str) or isinstance(b, str)):
                    raise Unsupported("repeating a string")
                if isinstance(op, ast.Mod) and isinstance(a, str):
                    raise Unsupported("formatting a string with %")
                return _check(_BINARY[type(op)](a, b))
            case ast.BoolOp(op=logic, values=operands):
                result = self.evaluate(operands[0])
                for operand in operands[1:]:
                    if bool(result) if isinstance(logic, ast.Or) else not result:
                        break
                    result = self.evaluate(operand)
                return result
            case ast.Compare(left=left, ops=ops, comparators=comparators):
                a = self.evaluate(left)
                for comparison, comparator in zip(ops, comparators, strict=True):
                    if type(comparison) not in _COMPARE:
                        raise Unsupported(f"the comparison {type(comparison).__name__}")
                    b = self.evaluate(comparator)
                    if not _COMPARE[type(comparison)](a, b):
                        return False
                    a = b
                return True
            case ast.IfExp(test=test, body=body, orelse=orelse):
                return self.evaluate(body if self.evaluate(test) else orelse)
            case ast.Call(func=func, args=args, keywords=[]) if not any(
                isinstance(arg, ast.Starred) for arg in args
            ):
                function = self.function(func)
                return _check(function(*(self.evaluate(arg) for arg in args)))
        raise Unsupported(f"the construct {type(node).__name__}")


def evaluate(text: str, lookup: Callable[[str], Value]) -> Value:
    """The value of the expression ``text``; names resolve through ``lookup`` first.

    ``lookup`` raises ``Undefined`` for a name it does not know. Raises ``ExpressionError``
    (``Unsupported``, ``Undefined``) for an expression that cannot be evaluated.
    """
    tree = parse(text)
    try:
        return _Evaluator(lookup).evaluate(tree)
    except ExpressionError:
        raise
    except (ArithmeticError, TypeError, ValueError, MemoryError) as exc:
        raise ExpressionError(f"evaluating it failed: {type(exc).__name__}") from exc


def literal(value: Value) -> Value:
    """Xacro's reading of a property's text: quoted text, a number, a boolean, or the text."""
    if not isinstance(value, str):
        return value
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1]
    if "_" in value:
        return value
    for convert in (int, float):
        try:
            return _check(convert(value))
        except (ValueError, ExpressionError):
            pass
    try:
        return boolean(value)
    except ExpressionError:
        return value


def boolean(value: Value) -> bool:
    """Xacro's reading of a condition: ``true``/``True``, ``false``/``False``, or an integer."""
    if isinstance(value, str):
        if value in ("true", "True"):
            return True
        if value in ("false", "False"):
            return False
        try:
            return bool(int(value))
        except ValueError as exc:
            raise ExpressionError("the condition is not a boolean") from exc
    return bool(value)

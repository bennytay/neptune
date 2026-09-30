"""Units as the evidence declares them, with an exact map to SI (ADR 0013).

A ``Unit`` is a product of catalogued atoms, each optionally SI-prefixed, with integer exponents:
``mm`` stays ``mm`` and ``km/h`` stays ``km.h^-1``. Nothing here defaults a unit: a field with no
declared unit is ``Unknown``. ``to_si`` is the exact normalisation that derived transforms use to
produce a separate, provenanced SI value; it never replaces the declared one.

Declared text becomes a unit only through ``unit_from_text`` and the shared catalogue below, so
every adapter reads "rad/sec" or "g" the same way. Changing the catalogue changes adapter output:
bump ``CATALOGUE_VERSION`` (ADR 0013 §6).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from fractions import Fraction
from types import MappingProxyType
from typing import Final, NoReturn

from neptune.model.ids import check_text
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import (
    INHERITED,
    Ambiguous,
    Candidate,
    Knowledge,
    Known,
    ProvenanceSlot,
    Unknown,
)

CATALOGUE_VERSION: Final = 1
# Bounds on hostile or absurd text. No real unit needs more; both keep parsing cheap.
MAX_TEXT_LENGTH: Final = 64
MAX_EXPONENT: Final = 9


class UncataloguedUnitError(ValueError):
    """Declared text the catalogue cannot read. The adapter emits a finding and ``Unknown``."""

    def __init__(self, text: str, reason: str) -> None:
        super().__init__(f"unit {text!r} is not catalogued: {reason}")
        self.text = text
        self.reason = reason


@dataclass(frozen=True)
class Dimension:
    """Exponents over the seven SI base quantities plus plane and solid angle.

    Angles are dimensions here, unlike in SI, so rad/s is not Hz and sr is not rad² (ADR 0013 §2).
    """

    length: int = 0
    mass: int = 0
    time: int = 0
    current: int = 0
    temperature: int = 0
    amount: int = 0
    luminous_intensity: int = 0
    angle: int = 0
    solid_angle: int = 0

    def __mul__(self, other: "Dimension") -> "Dimension":
        return Dimension(*(a + b for a, b in zip(self.exponents, other.exponents, strict=True)))

    def __pow__(self, exponent: int) -> "Dimension":
        return Dimension(*(a * exponent for a in self.exponents))

    @property
    def exponents(self) -> tuple[int, ...]:
        return tuple(getattr(self, f.name) for f in fields(self))


@dataclass(frozen=True)
class ExactReal:
    """``rational * π**pi_power``, exactly. Every catalogued scale and every SI value has this form.

    Zero is always ``ExactReal(0)``, so equal values have equal records.
    """

    rational: Fraction
    pi_power: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.rational, Fraction):
            raise TypeError(f"rational must be a Fraction, got {type(self.rational).__name__}")
        if isinstance(self.pi_power, bool) or not isinstance(self.pi_power, int):
            raise TypeError(f"pi_power must be an int, got {type(self.pi_power).__name__}")
        if self.rational == 0 and self.pi_power != 0:
            raise ValueError("zero is ExactReal(0); pi_power must be 0")

    def __mul__(self, other: "ExactReal") -> "ExactReal":
        rational = self.rational * other.rational
        return ExactReal(rational, self.pi_power + other.pi_power if rational else 0)

    def __pow__(self, exponent: int) -> "ExactReal":
        return ExactReal(self.rational**exponent, self.pi_power * exponent)


@dataclass(frozen=True)
class Atom:
    """One catalogue entry: what one of this unit is in the coherent SI unit of its dimension.

    ``offset`` is non-zero only for affine temperature scales (°C, °F): SI = scale * value + offset.
    """

    dimension: Dimension
    scale: ExactReal
    offset: Fraction = Fraction(0)
    prefixable: bool = False


def _atom(
    dimension: Dimension,
    scale: Fraction | int = 1,
    *,
    pi: int = 0,
    offset: Fraction = Fraction(0),
    prefixable: bool = False,
) -> Atom:
    return Atom(dimension, ExactReal(Fraction(scale), pi), offset, prefixable)


# --- Catalogue ---------------------------------------------------------------------------------

_L, _M, _T = Dimension(length=1), Dimension(mass=1), Dimension(time=1)
_I, _TEMP, _ANGLE = Dimension(current=1), Dimension(temperature=1), Dimension(angle=1)
_FORCE = _M * _L * _T**-2
_ENERGY = _FORCE * _L
_POWER = _ENERGY * _T**-1
_CHARGE = _I * _T
_VOLT = _POWER * _I**-1

_POUND = Fraction("0.45359237")  # kg, exact by the 1959 international agreement
_STANDARD_GRAVITY = Fraction("9.80665")  # m/s², exact by definition (CGPM 1901)
_INCH = Fraction("0.0254")  # m, exact

# Canonical atom ids. Prefixable atoms also appear as prefix + id: "mm", "kPa", "uT".
ATOMS: Final[Mapping[str, Atom]] = MappingProxyType(
    {
        # SI base units, and the angle "bases" (ADR 0013 §2)
        "m": _atom(_L, prefixable=True),
        "g": _atom(_M, Fraction(1, 1000), prefixable=True),
        "s": _atom(_T, prefixable=True),
        "A": _atom(_I, prefixable=True),
        "K": _atom(_TEMP, prefixable=True),
        "mol": _atom(Dimension(amount=1), prefixable=True),
        "cd": _atom(Dimension(luminous_intensity=1), prefixable=True),
        "rad": _atom(_ANGLE, prefixable=True),
        "sr": _atom(Dimension(solid_angle=1)),
        # SI coherent derived units with special names
        "Hz": _atom(_T**-1, prefixable=True),
        "N": _atom(_FORCE, prefixable=True),
        "Pa": _atom(_FORCE * _L**-2, prefixable=True),
        "J": _atom(_ENERGY, prefixable=True),
        "W": _atom(_POWER, prefixable=True),
        "C": _atom(_CHARGE, prefixable=True),
        "V": _atom(_VOLT, prefixable=True),
        "F": _atom(_CHARGE * _VOLT**-1, prefixable=True),
        "Ohm": _atom(_VOLT * _I**-1, prefixable=True),
        "S": _atom(_I * _VOLT**-1, prefixable=True),
        "Wb": _atom(_VOLT * _T, prefixable=True),
        "T": _atom(_VOLT * _T * _L**-2, prefixable=True),
        "H": _atom(_VOLT * _T * _I**-1, prefixable=True),
        "lm": _atom(Dimension(luminous_intensity=1, solid_angle=1)),
        "lx": _atom(Dimension(length=-2, luminous_intensity=1, solid_angle=1)),
        "degC": _atom(_TEMP, offset=Fraction("273.15")),
        # Accepted for use with SI
        "min": _atom(_T, 60),
        "h": _atom(_T, 3600),
        "d": _atom(_T, 86400),
        "deg": _atom(_ANGLE, Fraction(1, 180), pi=1),
        "L": _atom(_L**3, Fraction(1, 1000), prefixable=True),
        "bar": _atom(_FORCE * _L**-2, 100_000, prefixable=True),
        "%": _atom(Dimension(), Fraction(1, 100)),
        # Other units robotics sources declare
        "rev": _atom(_ANGLE, 2, pi=1),
        "ft": _atom(_L, _INCH * 12),
        "in": _atom(_L, _INCH),
        "mi": _atom(_L, _INCH * 12 * 5280),
        "nmi": _atom(_L, 1852),
        "kn": _atom(_L * _T**-1, Fraction(1852, 3600)),
        "lb": _atom(_M, _POUND),
        "lbf": _atom(_FORCE, _POUND * _STANDARD_GRAVITY),
        "psi": _atom(_FORCE * _L**-2, _POUND * _STANDARD_GRAVITY / _INCH**2),
        "g_n": _atom(_L * _T**-2, _STANDARD_GRAVITY),
        "gauss": _atom(_VOLT * _T * _L**-2, Fraction(1, 10_000), prefixable=True),
        "degF": _atom(_TEMP, Fraction(5, 9), offset=Fraction("459.67") * Fraction(5, 9)),
    }
)

PREFIXES: Final[Mapping[str, Fraction]] = MappingProxyType(
    {
        "p": Fraction(1, 10**12),
        "n": Fraction(1, 10**9),
        "u": Fraction(1, 10**6),
        "m": Fraction(1, 10**3),
        "c": Fraction(1, 10**2),
        "d": Fraction(1, 10),
        "h": Fraction(10**2),
        "k": Fraction(10**3),
        "M": Fraction(10**6),
        "G": Fraction(10**9),
    }
)
# Other spellings of a canonical prefix, accepted in declared text only.
PREFIX_SPELLINGS: Final[Mapping[str, str]] = MappingProxyType({"µ": "u", "μ": "u"})

# The coherent SI unit of each dimension is the product of these, one per Dimension field.
_SI_BASE: Final = ("m", "kg", "s", "A", "K", "mol", "cd", "rad", "sr")


# --- Unit --------------------------------------------------------------------------------------

_EXPONENT = re.compile(r"-?[1-9][0-9]*")


def _resolve(symbol: str) -> tuple[Atom, Fraction]:
    """A canonical atom symbol's catalogue entry and prefix factor. Exact ids win over prefixes."""
    if symbol in ATOMS:
        return ATOMS[symbol], Fraction(1)
    prefix, rest = symbol[:1], symbol[1:]
    atom = ATOMS.get(rest)
    if prefix in PREFIXES and atom is not None and atom.prefixable:
        return atom, PREFIXES[prefix]
    raise ValueError(f"not a catalogued unit symbol: {symbol!r}")


def _canonical(factors: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    """Positive exponents first, then negative; each group by symbol; zero exponents dropped."""
    return tuple(sorted(((s, e) for s, e in factors.items() if e), key=lambda f: (f[1] < 0, f[0])))


@dataclass(frozen=True)
class Unit:
    """A unit exactly as declared: catalogued atoms, optionally SI-prefixed, with exponents.

    ``factors`` must be canonical (see ``_canonical``); build units with ``unit_from_text`` or
    ``unit_from_json`` rather than by hand. ``Unit(())`` is the dimensionless unit "1". Equality is
    of the declared unit, not of the quantity: ``km/h`` and ``m/s`` are different units of one
    dimension, and ``to_si`` relates them.
    """

    factors: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        for symbol, exponent in self.factors:
            _resolve(symbol)
            if isinstance(exponent, bool) or not isinstance(exponent, int):
                raise TypeError(f"exponent of {symbol} must be an int, got {exponent!r}")
            if abs(exponent) > MAX_EXPONENT:
                raise ValueError(f"exponent {exponent} of {symbol} exceeds ±{MAX_EXPONENT}")
        symbols = [symbol for symbol, _ in self.factors]
        if len(set(symbols)) != len(symbols) or self.factors != _canonical(dict(self.factors)):
            raise ValueError(f"factors are not canonical: {self.factors}")
        if any(abs(e) > MAX_EXPONENT for e in self.dimension.exponents):
            # Bounding the dimension too means every unit has a representable SI unit.
            raise ValueError(f"dimension exponent exceeds ±{MAX_EXPONENT}: {self.dimension}")
        affine = [s for s in symbols if _resolve(s)[0].offset]
        if affine and self.factors != ((affine[0], 1),):
            raise ValueError(
                f"{affine[0]} has an offset, so it cannot be prefixed, raised or combined"
            )

    @property
    def symbol(self) -> str:
        """The canonical rendering, e.g. ``kg.m^2.s^-2``; ``1`` if dimensionless."""
        if not self.factors:
            return "1"
        return ".".join(s if e == 1 else f"{s}^{e}" for s, e in self.factors)

    @property
    def dimension(self) -> Dimension:
        dimension = Dimension()
        for symbol, exponent in self.factors:
            dimension = dimension * _resolve(symbol)[0].dimension ** exponent
        return dimension

    @property
    def scale(self) -> ExactReal:
        """What one of this unit is in the coherent SI unit of its dimension, exactly."""
        scale = ExactReal(Fraction(1))
        for symbol, exponent in self.factors:
            atom, prefix = _resolve(symbol)
            scale = scale * (ExactReal(prefix) * atom.scale) ** exponent
        return scale

    @property
    def offset(self) -> Fraction:
        """Non-zero only for a lone °C or °F."""
        return sum((_resolve(s)[0].offset for s, _ in self.factors), Fraction(0))

    def to_json(self) -> str:
        return self.symbol


def unit_from_json(data: JsonValue) -> Unit:
    """Parse the canonical symbol strictly: catalogue ids only, canonical order, no aliases."""
    if not isinstance(data, str):
        raise ValueError(f"unit must be a string, got {type(data).__name__}")
    if data == "1":
        return Unit(())
    factors: list[tuple[str, int]] = []
    for part in data.split("."):
        symbol, caret, exponent = part.partition("^")
        if caret and (not _EXPONENT.fullmatch(exponent) or exponent == "1"):
            raise ValueError(f"bad exponent in unit {data!r}: {part!r}")
        factors.append((symbol, int(exponent) if caret else 1))
    unit = Unit(tuple(factors))
    if unit.symbol != data:
        raise ValueError(f"unit is not in canonical form: {data!r} (want {unit.symbol!r})")
    return unit


def si_unit(dimension: Dimension) -> Unit:
    """The coherent SI unit of a dimension, e.g. ``m.s^-2``; plane angle in rad, solid in sr."""
    return Unit(_canonical(dict(zip(_SI_BASE, dimension.exponents, strict=True))))


# --- Declared text -----------------------------------------------------------------------------


def _u(symbol: str) -> Unit:
    return unit_from_json(symbol)


# Declared spellings that are not canonical ids, matched exactly (case- and code point-sensitive).
# A symbol with more than one established meaning lists every reading, in this order, and is read
# as Ambiguous: context such as "this column is an acceleration" is interpretation (ADR 0013 §4).
ALIASES: Final[Mapping[str, tuple[Unit, ...]]] = MappingProxyType(
    {
        "1": (_u("1"),),
        "sec": (_u("s"),),
        "hr": (_u("h"),),
        "second": (_u("s"),),
        "seconds": (_u("s"),),
        "meter": (_u("m"),),
        "meters": (_u("m"),),
        "metre": (_u("m"),),
        "metres": (_u("m"),),
        "radian": (_u("rad"),),
        "radians": (_u("rad"),),
        "°": (_u("deg"),),
        "degree": (_u("deg"),),
        "degrees": (_u("deg"),),
        "°C": (_u("degC"),),
        "℃": (_u("degC"),),
        "°F": (_u("degF"),),
        "℉": (_u("degF"),),
        "Ω": (_u("Ohm"),),  # GREEK CAPITAL LETTER OMEGA
        "\u2126": (_u("Ohm"),),  # OHM SIGN
        "ohm": (_u("Ohm"),),
        "l": (_u("L"),),
        "knot": (_u("kn"),),
        "knots": (_u("kn"),),
        "kph": (_u("km.h^-1"),),
        "mph": (_u("mi.h^-1"),),
        "rpm": (_u("rev.min^-1"),),
        "Ah": (_u("A.h"),),
        "mAh": (_u("h.mA"),),
        "Wh": (_u("W.h"),),
        "kWh": (_u("h.kW"),),
        "C": (_u("C"), _u("degC")),  # coulomb, or Celsius written without the degree sign
        "F": (_u("F"), _u("degF")),  # farad, or Fahrenheit written without the degree sign
        "g": (_u("g"), _u("g_n")),  # gram, or standard gravity in accelerometer data
        "G": (_u("gauss"), _u("g_n")),  # gauss, or standard gravity
        "lb": (_u("lb"), _u("lbf")),  # pound mass, or pound-force
    }
)

_MULTIPLY: Final = frozenset("*.·⋅")
_SUPERSCRIPTS: Final = MappingProxyType(dict(zip("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-", strict=True)))
_NOT_IN_NAME: Final = _MULTIPLY | frozenset("/()^") | _SUPERSCRIPTS.keys()

# A factor is its alternative readings (each a factor map) and an exponent.
_Readings = tuple[Mapping[str, int], ...]


class _TextParser:
    """``unit := product ('/' (factor | '(' product ')'))*``; ``factor := name exponent?``.

    ``a/b/c`` is a·b⁻¹·c⁻¹. ``a/b*c`` is rejected: readers disagree on what it means.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def parse(self) -> list[tuple[_Readings, int]]:
        factors = self.product()
        while self.peek() == "/":
            self.pos += 1
            if self.peek() == "(":
                self.pos += 1
                group = self.product()
                self.expect(")")
            else:
                group = [self.factor()]
            factors.extend((readings, -exponent) for readings, exponent in group)
        if self.pos != len(self.text):
            self.fail(f"unexpected {self.text[self.pos]!r} at position {self.pos}")
        return factors

    def product(self) -> list[tuple[_Readings, int]]:
        factors = [self.factor()]
        while self.peek() in _MULTIPLY:
            self.pos += 1
            factors.append(self.factor())
        return factors

    def factor(self) -> tuple[_Readings, int]:
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in _NOT_IN_NAME:
            self.pos += 1
        if self.pos == start:
            self.fail(f"expected a unit name at position {start}")
        return _readings(self.text, self.text[start : self.pos]), self.exponent()

    def exponent(self) -> int:
        if self.peek() == "^":
            self.pos += 1
            start = self.pos
            if self.peek() == "-":
                self.pos += 1
            while self.peek().isascii() and self.peek().isdigit():
                self.pos += 1
            digits = self.text[start : self.pos]
        else:
            start = self.pos
            while self.peek() in _SUPERSCRIPTS:
                self.pos += 1
            if self.pos == start:
                return 1
            digits = "".join(_SUPERSCRIPTS[c] for c in self.text[start : self.pos])
        if not _EXPONENT.fullmatch(digits):
            self.fail(f"bad exponent {digits!r}")
        return int(digits)

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def expect(self, char: str) -> None:
        if self.peek() != char:
            self.fail(f"expected {char!r} at position {self.pos}")
        self.pos += 1

    def fail(self, reason: str) -> NoReturn:
        raise UncataloguedUnitError(self.text, reason)


def _readings(text: str, name: str) -> _Readings:
    if name in ALIASES:
        return tuple(dict(unit.factors) for unit in ALIASES[name])
    for spelling, prefix in PREFIX_SPELLINGS.items():
        if name.startswith(spelling):
            name = prefix + name[len(spelling) :]
    try:
        _resolve(name)
    except ValueError:
        raise UncataloguedUnitError(text, f"{name!r} is not in the unit catalogue") from None
    return ({name: 1},)


def _interpret(text: str) -> tuple[Unit, ...]:
    """Every catalogued reading of ``text``, in catalogue order. Raises if there is none."""
    check_text("unit", text)
    if len(text) > MAX_TEXT_LENGTH:
        raise UncataloguedUnitError(text, f"longer than {MAX_TEXT_LENGTH} characters")
    readings: list[dict[str, int]] = [{}]
    for alternatives, exponent in _TextParser(text).parse():
        combined: list[dict[str, int]] = []
        for reading in readings:
            for alternative in alternatives:
                merged = dict(reading)
                for symbol, power in alternative.items():
                    merged[symbol] = merged.get(symbol, 0) + power * exponent
                    if abs(merged[symbol]) > MAX_EXPONENT:
                        raise UncataloguedUnitError(text, f"exponent exceeds ±{MAX_EXPONENT}")
                if merged not in combined:
                    combined.append(merged)
        readings = combined
    units: list[Unit] = []
    for reading in readings:
        try:
            unit = Unit(_canonical(reading))
        except ValueError as exc:
            # One unsupported reading makes the text unreadable; dropping it would silently
            # resolve an ambiguity by our own limitation.
            raise UncataloguedUnitError(text, str(exc)) from None
        if unit not in units:
            units.append(unit)
    return tuple(units)


def unit_from_text(raw: str | None, *, provenance: ProvenanceSlot = INHERITED) -> Knowledge[Unit]:
    """Read one declared unit (ADR 0013 §4).

    - Missing, empty or whitespace-only ⇒ ``Unknown``. Nothing defaults to SI or to anything else.
    - One catalogued reading ⇒ ``Known``; several (``g``, ``C``) ⇒ ``Ambiguous``, each candidate
      citing ``provenance``.
    - Text the catalogue cannot read raises ``UncataloguedUnitError``: the adapter emits a finding
      and ``Unknown``, as ``from_text`` does for unparseable values.

    ``raw`` is the unit text alone; extracting it ("m/s" from "speed [m/s]") is the adapter's
    documented syntax. Matching is exact: no case folding, no trimming inside the text.
    """
    if raw is None or not raw.strip():
        return Unknown(provenance)
    units = _interpret(raw)
    if len(units) == 1:
        return Known(units[0], provenance)
    return Ambiguous(tuple(Candidate(unit, provenance) for unit in units))


# --- SI normalisation --------------------------------------------------------------------------


@dataclass(frozen=True)
class SIValue:
    """An exact value in the coherent SI unit of the declared unit's dimension."""

    value: ExactReal
    unit: Unit


def to_si(value: int | Fraction, unit: Unit, *, difference: bool = False) -> SIValue:
    """Normalise one value exactly. For derived transforms only (ADR 0013 §5).

    The result is a new, provenanced value; the declared value and unit are never overwritten.
    Pass floats as ``Fraction(x)``, which is exact. ``difference=True`` marks a temperature
    difference, which takes the scale of °C/°F but not the offset. Rounding to a float, if a
    consumer needs it, is a documented step of that consumer's transform.
    """
    if isinstance(value, bool) or not isinstance(value, int | Fraction):
        raise TypeError(f"value must be an int or Fraction, got {type(value).__name__}")
    if not isinstance(unit, Unit):
        raise TypeError(f"only a Known unit can be normalised, got {type(unit).__name__}")
    scaled = ExactReal(Fraction(value)) * unit.scale
    if unit.offset and not difference:
        scaled = ExactReal(scaled.rational + unit.offset)  # affine atoms have pi_power 0
    return SIValue(scaled, si_unit(unit.dimension))

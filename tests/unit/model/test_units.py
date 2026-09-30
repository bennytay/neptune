import hashlib
from dataclasses import dataclass
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.identity import canonical_json
from neptune.model import units
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    Ambiguous,
    Candidate,
    Known,
    Unknown,
    from_json,
    to_json,
)
from neptune.model.units import (
    ALIASES,
    ATOMS,
    CATALOGUE_VERSION,
    MAX_EXPONENT,
    MAX_TEXT_LENGTH,
    PREFIX_SPELLINGS,
    PREFIXES,
    Dimension,
    ExactReal,
    SIValue,
    UncataloguedUnitError,
    Unit,
    si_unit,
    to_si,
    unit_from_json,
    unit_from_text,
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


HEADER = Cite("log.csv#row=0,col=speed [km/h]")
IMU_MSG = Cite("sensor_msgs/Imu.msg: angular_velocity  # rad/sec")


def u(symbol: str) -> Unit:
    return unit_from_json(symbol)


def si(unit: str, value: int | Fraction = 1, **kwargs: bool) -> ExactReal:
    return to_si(value, u(unit), **kwargs).value


# --- Acceptance: stored as declared, or Unknown; nothing defaults to SI -----------------------


@pytest.mark.parametrize("raw", [None, "", " ", "\t\n"])
def test_missing_unit_is_unknown_never_si(raw: str | None) -> None:
    assert unit_from_text(raw, provenance=HEADER) == Unknown(HEADER)


@pytest.mark.parametrize(
    ("raw", "symbol"),
    [
        ("mm", "mm"),
        ("km/h", "km.h^-1"),
        ("kph", "km.h^-1"),
        ("ft", "ft"),
        ("deg", "deg"),
        ("°C", "degC"),
        ("mAh", "h.mA"),
        ("rpm", "rev.min^-1"),
        ("hPa", "hPa"),
    ],
)
def test_declared_unit_is_kept_not_converted(raw: str, symbol: str) -> None:
    assert unit_from_text(raw, provenance=HEADER) == Known(u(symbol), HEADER)


def test_normalising_leaves_the_declared_value_alone() -> None:
    declared = Known(u("km.h^-1"), HEADER)
    result = to_si(36, declared.known_or_raise())
    assert result == SIValue(ExactReal(Fraction(10)), u("m.s^-1"))
    assert declared == Known(u("km.h^-1"), HEADER)  # frozen; the SI form is a separate value


@pytest.mark.parametrize("not_a_unit", [Unknown(), "m", None])
def test_only_a_known_unit_can_be_normalised(not_a_unit: object) -> None:
    with pytest.raises(TypeError):
        to_si(1, not_a_unit)  # type: ignore[arg-type]


def test_no_float_or_default_api_exists() -> None:
    module_api = {name.lower() for name in dir(units) if not name.startswith("_")}
    for word in ("float", "default", "round", "approx"):
        assert not any(word in name for name in module_api), word


# --- Acceptance: exact SI form for every catalogued unit --------------------------------------


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("ft", ExactReal(Fraction("0.3048"))),
        ("in", ExactReal(Fraction("0.0254"))),
        ("mi", ExactReal(Fraction("1609.344"))),
        ("kn", ExactReal(Fraction(1852, 3600))),
        ("lb", ExactReal(Fraction("0.45359237"))),
        ("lbf", ExactReal(Fraction("4.4482216152605"))),
        ("psi", ExactReal(Fraction("4.4482216152605") / Fraction("0.0254") ** 2)),
        ("g_n", ExactReal(Fraction("9.80665"))),
        ("h.mA", ExactReal(Fraction("3.6"))),
        ("h.kW", ExactReal(Fraction(3_600_000))),
        ("hPa", ExactReal(Fraction(100))),
        ("uT", ExactReal(Fraction(1, 10**6))),
        ("gauss", ExactReal(Fraction(1, 10**4))),
        ("mL", ExactReal(Fraction(1, 10**6))),
        ("%", ExactReal(Fraction(1, 100))),
        ("deg", ExactReal(Fraction(1, 180), 1)),
        ("rev.min^-1", ExactReal(Fraction(1, 30), 1)),
        ("deg.s^-1", ExactReal(Fraction(1, 180), 1)),
        ("deg^2", ExactReal(Fraction(1, 180**2), 2)),
    ],
)
def test_si_scale_is_exact(unit: str, expected: ExactReal) -> None:
    assert si(unit) == expected


def test_affine_temperatures_are_exact() -> None:
    assert to_si(20, u("degC")) == SIValue(ExactReal(Fraction("293.15")), u("K"))
    assert si("degF", 32) == ExactReal(Fraction("273.15"))
    assert si("degF", 212) == ExactReal(Fraction("373.15"))
    assert si("degF", -40) == si("degC", -40)


def test_temperature_difference_takes_scale_not_offset() -> None:
    assert si("degC", 5, difference=True) == ExactReal(Fraction(5))
    assert si("degF", 9, difference=True) == ExactReal(Fraction(5))
    assert si("km", 2, difference=True) == si("km", 2)  # no offset, no effect


def test_zero_is_canonical_even_with_pi() -> None:
    assert si("deg", 0) == ExactReal(Fraction(0))
    assert si("deg", 0).pi_power == 0
    with pytest.raises(ValueError, match="zero"):
        ExactReal(Fraction(0), 1)


@pytest.mark.parametrize("value", [1.5, True, "1", None])
def test_value_must_be_exact(value: object) -> None:
    with pytest.raises(TypeError):
        to_si(value, u("m"))  # type: ignore[arg-type]


def test_float_input_goes_through_fraction_exactly() -> None:
    # 0.1 as a binary float is not 1/10; converting it exactly keeps what the bytes encode.
    assert si("km", Fraction(0.1)) == ExactReal(Fraction(0.1) * 1000)
    assert si("km", Fraction(0.1)) != ExactReal(Fraction(100))


def test_angle_is_a_dimension_so_rpm_is_not_hz() -> None:
    assert u("rev.min^-1").dimension == Dimension(angle=1, time=-1)
    assert u("Hz").dimension == Dimension(time=-1)
    assert to_si(1, u("rev.min^-1")).unit == u("rad.s^-1")
    assert u("sr").dimension != u("rad^2").dimension


@pytest.mark.parametrize(
    ("unit", "coherent"),
    [
        ("N", "kg.m.s^-2"),
        ("psi", "kg.m^-1.s^-2"),
        ("Ohm", "kg.m^2.A^-2.s^-3"),
        ("lx", "cd.sr.m^-2"),
        ("%", "1"),
        ("degF", "K"),
    ],
)
def test_si_unit_is_the_coherent_product_of_bases(unit: str, coherent: str) -> None:
    assert to_si(1, u(unit)).unit == u(coherent)


def test_coherent_si_units_have_scale_one() -> None:
    for symbol, atom in ATOMS.items():
        coherent = si_unit(atom.dimension)
        assert coherent.scale == ExactReal(Fraction(1)), symbol
        assert coherent.offset == 0


def _unit_or_nothing(factors: list[tuple[str, int]]) -> st.SearchStrategy[Unit]:
    try:
        return st.just(Unit(tuple(sorted(factors, key=lambda f: (f[1] < 0, f[0])))))
    except ValueError:  # dimension beyond the exponent bound
        return st.nothing()


units_st = st.lists(
    st.tuples(
        st.sampled_from(
            [s for s, a in ATOMS.items() if not a.offset]
            + [p + s for p in PREFIXES for s, a in ATOMS.items() if a.prefixable]
        ),
        st.integers(-3, 3).filter(bool),
    ),
    max_size=4,
    unique_by=lambda f: f[0],
).flatmap(_unit_or_nothing)
exact_st = st.fractions(max_denominator=10**6) | st.integers(-(10**12), 10**12)


@given(units_st, exact_st)
def test_normalisation_is_exact(unit: Unit, value: Fraction | int) -> None:
    result = to_si(value, unit)
    assert result.value == ExactReal(Fraction(value)) * unit.scale
    assert result.unit.dimension == unit.dimension


# --- Declared text ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "symbol"),
    [
        ("m/s^2", "m.s^-2"),
        ("m/s²", "m.s^-2"),
        ("m·s⁻²", "m.s^-2"),
        ("m/s/s", "m.s^-2"),
        ("m*s^-2", "m.s^-2"),
        ("rad/sec", "rad.s^-1"),
        ("kg·m²/s²", "kg.m^2.s^-2"),
        ("kg⋅m^2", "kg.m^2"),
        ("J/(kg.K)", "J.K^-1.kg^-1"),
        ("1/s", "s^-1"),
        ("1", "1"),
        ("m/m", "1"),
        ("m.m", "m^2"),
        ("µm", "um"),
        ("μs", "us"),
        ("Ω", "Ohm"),
        ("\u2126", "Ohm"),  # OHM SIGN, distinct from GREEK CAPITAL LETTER OMEGA
        ("kOhm", "kOhm"),
        ("°/s", "deg.s^-1"),
        ("mph", "mi.h^-1"),
        ("N.m", "N.m"),
        ("g_n", "g_n"),
        ("kg", "kg"),
    ],
)
def test_declared_text_forms(raw: str, symbol: str) -> None:
    assert unit_from_text(raw) == Known(u(symbol))


@pytest.mark.parametrize(
    ("raw", "readings"),
    [
        ("g", ["g", "g_n"]),
        ("C", ["C", "degC"]),
        ("F", ["F", "degF"]),
        ("G", ["gauss", "g_n"]),
        ("lb", ["lb", "lbf"]),
        ("m/g", ["m.g^-1", "m.g_n^-1"]),
        ("g.g", ["g^2", "g.g_n", "g_n^2"]),
    ],
)
def test_symbols_with_several_meanings_are_ambiguous(raw: str, readings: list[str]) -> None:
    assert unit_from_text(raw, provenance=HEADER) == Ambiguous(
        tuple(Candidate(u(r), HEADER) for r in readings)
    )


def test_spec_declared_unit_cites_the_spec() -> None:
    # sensor_msgs/Imu states rad/sec in its definition: Known, grounded in the definition.
    assert unit_from_text("rad/sec", provenance=IMU_MSG) == Known(u("rad.s^-1"), IMU_MSG)


@pytest.mark.parametrize(
    "raw",
    [
        "furlong",
        "M",
        "Nm",  # newton metre or nanometre-typo: not guessed
        "m s",  # whitespace is not multiplication
        " m",
        "M/S",  # no case folding
        "kΩ",  # aliases are not prefixable
        "dB",  # logarithmic: not catalogued
        "px",  # no SI mapping
        "m/s*s",  # (m/s)·s or m/(s·s)? readers disagree
        "m/s.s",
        "/s",
        "m/",
        "m//s",
        "m^",
        "m^0",
        "m^01",
        "m^+2",
        "m^1.5",
        "m^^2",
        "m⁻",
        "(m)",
        "m/(s",
        "m/(s))",
        "m/()",
        "kdegC",
        "degC^2",
        "degC/s",
        "°C/s",
        "C/s",  # one reading (°C/s) is unsupported, so the text is unreadable, not coulomb/s
        "m^10",
        "m^5.m^5",
        "W^-4",
        "g" + ".g" * 10,
        "m" * (MAX_TEXT_LENGTH + 1),
        "\ud800",
    ],
)
def test_unreadable_text_raises_for_a_finding(raw: str) -> None:
    with pytest.raises(ValueError) as caught:
        unit_from_text(raw)
    if caught.type is UncataloguedUnitError:
        assert isinstance(caught.value, UncataloguedUnitError)
        assert caught.value.text == raw


def test_uncatalogued_error_says_why() -> None:
    with pytest.raises(UncataloguedUnitError, match="'furlong' is not in the unit catalogue"):
        unit_from_text("furlong/fortnight")


@given(st.text(max_size=20))
def test_arbitrary_text_is_read_or_rejected_never_crashes(raw: str) -> None:
    try:
        result = unit_from_text(raw)
    except ValueError:
        return
    assert isinstance(result, Known | Ambiguous | Unknown)


# --- Unit type and JSON -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "factors",
    [
        (("s", -2), ("m", 1)),  # not canonical order
        (("m", 1), ("m", 2)),  # repeated atom
        (("m", 0),),
        (("m", MAX_EXPONENT + 1),),
        (("furlong", 1),),
        (("um", 1), ("µm", 1)),
        (("degC", 1), ("s", -1)),
        (("degC", 2),),
        (("W", -4),),  # its SI unit would need s^12
    ],
)
def test_unit_rejects_non_canonical_factors(factors: tuple[tuple[str, int], ...]) -> None:
    with pytest.raises(ValueError):
        Unit(factors)


@pytest.mark.parametrize("exponent", [1.0, True, "2"])
def test_unit_exponent_must_be_int(exponent: object) -> None:
    with pytest.raises(TypeError):
        Unit((("m", exponent),))  # type: ignore[arg-type]


@given(units_st)
def test_symbol_round_trips(unit: Unit) -> None:
    assert unit_from_json(unit.to_json()) == unit
    # Read as text, a canonical symbol never becomes a different unit. Alias symbols (g, C, …) are
    # Ambiguous, or unreadable when one reading is unsupported ("C.m" could be °C·m).
    try:
        result = unit_from_text(unit.symbol)
    except UncataloguedUnitError:
        assert any(symbol in ALIASES for symbol, _ in unit.factors)
        return
    match result:
        case Known(value=value):
            assert value == unit
        case Ambiguous(candidates=candidates):
            assert unit in [c.value for c in candidates]
        case other:
            raise AssertionError(other)


@pytest.mark.parametrize(
    "data",
    ["s^-2.m", "m^1", "m.m", "sec", "µm", "m/s", "m s", "", "1.m", "m^02", 1, ["m"], {"m": 1}],
)
def test_unit_from_json_is_strict(data: JsonValue) -> None:
    with pytest.raises(ValueError):
        unit_from_json(data)


def test_knowledge_wrapped_unit_round_trips() -> None:
    for knowledge in (
        unit_from_text("km/h", provenance=HEADER),
        unit_from_text("g", provenance=HEADER),
        unit_from_text(None),
    ):
        data = to_json(knowledge, Unit.to_json)
        assert from_json(data, unit_from_json, cite) == knowledge
    assert canonical_json.dumps(to_json(Known(u("km.h^-1")), Unit.to_json)) == (
        b'{"knowledge":"known","value":"km.h^-1"}'
    )


def test_reading_is_deterministic() -> None:
    first = [to_json(unit_from_text(t), Unit.to_json) for t in ("m/s²", "g", "J/(kg.K)")]
    again = [to_json(unit_from_text(t), Unit.to_json) for t in ("m/s²", "g", "J/(kg.K)")]
    assert canonical_json.dumps(first) == canonical_json.dumps(again)


# --- Catalogue --------------------------------------------------------------------------------


def test_prefixed_forms_never_collide_with_ids_or_aliases() -> None:
    for prefix in (*PREFIXES, *PREFIX_SPELLINGS):
        for symbol, atom in ATOMS.items():
            if atom.prefixable:
                assert prefix + symbol not in ATOMS, prefix + symbol
                assert prefix + symbol not in ALIASES, prefix + symbol


def test_affine_atoms_are_unprefixed_and_rational() -> None:
    for symbol, atom in ATOMS.items():
        if atom.offset:
            assert not atom.prefixable, symbol
            assert atom.scale.pi_power == 0, symbol


def test_aliases_are_not_canonical_ids_except_ambiguous_ones() -> None:
    # An alias that shadows an id must list that id's own reading first.
    for alias, readings in ALIASES.items():
        if alias in ATOMS:
            assert readings[0] == u(alias) and len(readings) > 1, alias


def _catalogue_json() -> JsonValue:
    def exact(value: ExactReal) -> JsonValue:
        r = value.rational
        return [r.numerator, r.denominator, value.pi_power]

    return {
        "aliases": {k: [unit.symbol for unit in v] for k, v in ALIASES.items()},
        "atoms": {
            symbol: {
                "dimension": list(atom.dimension.exponents),
                "offset": [atom.offset.numerator, atom.offset.denominator],
                "prefixable": atom.prefixable,
                "scale": exact(atom.scale),
            }
            for symbol, atom in ATOMS.items()
        },
        "prefix_spellings": dict(PREFIX_SPELLINGS),
        "prefixes": {k: [v.numerator, v.denominator] for k, v in PREFIXES.items()},
    }


def test_catalogue_changes_bump_the_version() -> None:
    # The catalogue decides what adapters emit, so it is an output-affecting library version
    # (ADR 0006 §4, ADR 0013 §6). Changing an entry or alias without bumping it breaks this test:
    # bump CATALOGUE_VERSION, then update the digest.
    digest = hashlib.sha256(canonical_json.dumps(_catalogue_json())).hexdigest()
    assert (CATALOGUE_VERSION, digest) == (
        1,
        "e48b9578c9246e3c3afcfb8f6b72be0115076bb0d7a0bf21e56de5eee774dc82",
    )

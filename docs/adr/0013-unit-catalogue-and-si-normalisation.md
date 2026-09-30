# 0013 — Units: declared-unit catalogue and exact SI normalisation

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-61 (sub-issue of MVL-4)

## Context

Non-negotiable 4 and the MVL-4 scope note fix the rule: units are stored as the source declares them, and SI
is a derived form with its own provenance. What the rule leaves open is how "as declared" is typed, which
spellings mean which unit, and how the SI form is computed without losing exactness. Every adapter that emits
a quantity depends on the answers, so they are a long-lived contract.

Robotics sources declare units in many ways. Examples are `rad/sec` in a `sensor_msgs` definition, `m/s²` in a
datasheet, `speed [kph]` in a CSV header and `g` in an accelerometer log. Some spellings have more than one
established meaning: `g` is a gram or standard gravity, and `C` is a coulomb or Celsius without its degree
sign. If each adapter kept its own table, the same text would become different units in different formats.

## Decision

1. **`Unit` is the declared unit**, in `neptune.model.units`. It is a canonical product of catalogued atoms,
   each optionally SI-prefixed, with non-zero integer exponents: `mm`, `km.h^-1`, `kg.m^2.s^-2`, `1`.
   - Stored as declared: `mm` stays `mm`, `kph` becomes `km.h^-1`, never `m.s^-1`. Only the spelling is made
     canonical. Repeated atoms combine (`m.m` is `m^2`; `m/m` is `1`).
   - Equality compares declared units, not quantities: `km/h` ≠ `m/s`. `to_si` relates them.
   - JSON is the canonical symbol string. Order: positive exponents first, then negative, each sorted by
     symbol. `unit_from_json` accepts only that form.
   - Fields holding a unit are `Knowledge[Unit]` (ADR 0004 §3). Nothing defaults to SI or to anything else.
2. **`Dimension` has nine exponents**: the seven SI base quantities plus plane angle and solid angle. SI treats
   the radian as 1. Here angle is a dimension, so rad/s is not Hz, rpm cannot become Hz, and sr is not rad².
   Converting between them needs a stated interpretation, which is a derived transform.
3. **The catalogue** (`ATOMS`, `PREFIXES`, `ALIASES`) gives each atom a dimension and an exact SI scale of the
   form `rational × π^k`, plus a rational offset for °C and °F only.
   - Atoms: SI base and named derived units; min, h, d, deg, L, bar, %; rev, ft, in, mi, nmi, kn, lb, lbf,
     psi, standard gravity `g_n`, gauss, degF. Imperial factors are the exact 1959 definitions.
   - Prefixes p…G apply only to atoms marked prefixable. No prefixed form may equal an atom id or an alias
     (tested).
   - Units with an offset (°C, °F) must stand alone: no prefix, exponent or product. `°C/s` is not
     catalogued in v0.
   - Logarithmic units (dB), pixels and counts have no SI mapping and are not catalogued.
4. **Reading declared text.** Only `unit_from_text` maps text to a unit, and only through the shared catalogue.
   - Adapters extract the unit text (`m/s` from `speed [m/s]`) using a syntax their descriptor documents.
     They never keep private text-to-unit tables. A new spelling is a catalogue PR.
   - Blank or missing ⇒ `Unknown`.
   - One reading ⇒ `Known`. Several readings (`g`, `C`, `F`, `G`, `lb`) ⇒ `Ambiguous`, in catalogue order.
     Choosing by context ("this column is an acceleration") is interpretation, so it belongs in `derived/`.
   - Text the catalogue cannot read raises `UncataloguedUnitError`. The adapter emits a finding and `Unknown`,
     as `from_text` does for unparseable values. If any reading of an ambiguous text is unsupported (`C/s`
     could be °C/s), the whole text is unreadable. Dropping that reading would resolve the ambiguity silently.
   - Grammar: `product ('/' (factor | '(' product ')'))*`. Multiplication is `*`, `.`, `·` or `⋅`. Exponents
     are `^n` or superscripts. `a/b/c` means a·b⁻¹·c⁻¹. `a/b*c` is rejected because readers disagree on it.
     Whitespace is never multiplication. Matching is exact, with no case folding.
   - Text over 64 characters or exponents beyond ±9 are rejected. The bound also applies to a unit's
     dimension, so every `Unit` has a representable SI unit.
5. **Grounds for `Known`**, each cited by the state's provenance:
   - unit text in the source, in a place the source or format designates for units;
   - the format specification or message definition for that field, such as `sensor_msgs/Imu` or the URDF
     spec;
   - a user manifest entry (MVL-14).

   These are **not** grounds: community convention (REP-103 for a custom message, as in ADR 0007), plausibility
   of the values, fragments of field names (`depth_mm`), or "the rest of this file is SI". Conflicting
   declarations, such as a spec saying `m` and a header saying `mm`, are `Ambiguous` with both cited.
6. **SI normalisation is exact and derived.**
   - `to_si(value, unit)` takes an `int` or `Fraction` and returns an `ExactReal` (`rational × π^k`) in the
     coherent SI unit of the unit's dimension. Floats pass through `Fraction(x)`, which is exact.
   - It accepts only a `Unit`. `Unknown` and `Ambiguous` cannot be normalised; picking a candidate is
     inference.
   - `difference=True` marks a temperature difference: scale without offset.
   - The result is a new record from a normalisation transform. Its provenance cites the declared value, and
     the declared value is never overwritten. It inherits the input's `assertion_kind`, because exact
     arithmetic under a `Known` unit adds no interpretation. Where these records live is MVL-3/MVL-5's to
     define.
   - Rounding to a float, if needed, is a documented step of the consuming transform. It gets a shared helper
     when the first such transform lands.
7. **`CATALOGUE_VERSION`.** A catalogue change changes adapter output: an `Unknown` becomes `Known`, or a
   `Known` becomes `Ambiguous`. The version is therefore an output-affecting library version in the adapter's
   `TransformRecord` (ADR 0006 §4). A test pins the catalogue's digest to the version. Adding entries is
   additive. Changing an existing entry's meaning needs an ADR.

## Alternatives considered

- **UCUM as the vocabulary and grammar.** It is the most rigorous standard. Its grammar reads `m2` and `s-2`
  and treats `.` and `/` differently from how datasheets use them. It has no answer for ambiguous spellings
  such as `g` and `C`, which are the main risk in robotics data. The canonical form borrows its `.`/`^`
  notation. Adopting UCUM wholesale was left for a later ADR if interchange needs it.
- **A closed list of whole units** (`m/s`, `m/s^2`, …). Easy to review, but covariance fields need squared
  units of everything, and the list explodes.
- **`pint` or another units library.** A dependency whose registry changes between releases would change
  adapter output without an adapter version bump. It uses floats, and it maps `g` to gram without
  ambiguity.
- **Keep the verbatim spelling on the value** (`DeclaredUnit(text, unit)`). The spelling is already
  recoverable from the provenance locator, and a second field on every quantity makes the wrapper heavier
  (ADR 0004 risk #3).
- **Float scale factors.** 0.3048 has no exact binary form, and deg→rad needs π. `rational × π^k` makes every
  catalogued conversion exact.
- **Angle as dimensionless, as SI has it.** rad/s would then equal Hz, and a 2π error would pass every
  dimension check.

## Consequences

- Every adapter reads a given unit text identically, and a new spelling is reviewed once.
- Quantity fields can be dimension-checked (`unit.dimension == Dimension(length=1)`) without converting.
  MVL-63 uses this for translation and angle units.
- Units that cannot be catalogued yet (pixels in camera intrinsics, dB, counts, °C/s) become `Unknown` plus a
  finding. MVL-26 will need pixels, and that needs an ADR for non-SI dimensions.
- Revisit if findings show many real spellings being rejected, or if interchange requires UCUM codes.

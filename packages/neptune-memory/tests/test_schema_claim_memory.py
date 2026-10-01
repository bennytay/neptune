"""Claims, objects, provenance, intervals, clocks and nodes: construction, refusal, determinism."""

from dataclasses import replace
from fractions import Fraction
from typing import Any

import pytest

from memory_schema_builders import (
    BOOT_CLOCK,
    CONFIG,
    DAYS,
    INFERRED,
    OBSERVED,
    SECONDS,
    STATED,
    at,
    claim,
    evidence,
    node,
    provenance,
)
from neptune.identity.canonical_json import dumps
from neptune.identity.ids import record_id
from neptune.model.knowledge import Known, NotApplicable, NotCovered, Unknown
from neptune.model.scalars import NonFinite
from neptune.model.time import DomainMismatchError, Epoch, Timescale
from neptune.model.units import unit_from_text
from neptune_memory.schema.claim import (
    Claim,
    ClaimObject,
    ClaimProvenance,
    LedgerRecordRef,
    TypedLiteral,
    ValueType,
    object_type,
    parse_claim_id,
)
from neptune_memory.schema.interval import OPEN, CivilClock, Interval, ledger_tx
from neptune_memory.schema.nodes import (
    CONTEXT_TYPES,
    ENTITY_TYPES,
    TIER_OF,
    NodeRef,
    NodeType,
    Tier,
)

ARM = node(NodeType.MACHINE, "ur10e-07")
CELL = node(NodeType.SITE, "assembly-cell-2")
RECORD = record_id("run", {"test": "a run record"})


def located(**overrides: Any) -> Claim:
    base = claim(ARM, "located_at", CELL, 10, tx=1)
    return replace(base, **overrides) if overrides else base


# --- Nodes ------------------------------------------------------------------------------------


def test_every_node_type_has_exactly_one_tier_and_episode_tier_has_no_nodes() -> None:
    assert set(TIER_OF) == set(NodeType)
    assert set(NodeType) == ENTITY_TYPES | CONTEXT_TYPES
    assert {NodeType.DEPLOYMENT, NodeType.FLEET, NodeType.PROGRAMME} == CONTEXT_TYPES
    assert Tier.EPISODE not in TIER_OF.values()
    assert node(NodeType.EPISODE, "ep-1").tier is Tier.ENTITY


@pytest.mark.parametrize("bad", ["", "\ud800"])
def test_node_ids_are_non_empty_unicode(bad: str) -> None:
    with pytest.raises(ValueError):
        NodeRef(NodeType.MACHINE, bad)


def test_node_type_must_be_the_enum() -> None:
    with pytest.raises(TypeError):
        NodeRef("machine", "x")  # type: ignore[arg-type]


# --- Intervals and clocks ---------------------------------------------------------------------


def test_interval_is_half_open_and_open_ended() -> None:
    closed = Interval(at(10), at(20))
    assert closed.contains(at(10)) and closed.contains(at(19))
    assert not closed.contains(at(20)) and not closed.contains(at(9))
    assert Interval(at(10), OPEN).contains(at(10**12))
    assert closed.overlaps(Interval(at(19), OPEN))
    assert not closed.overlaps(Interval(at(20), OPEN))
    assert not Interval(at(20), OPEN).overlaps(closed)


@pytest.mark.parametrize(("start", "end"), [(10, 10), (10, 9)])
def test_empty_intervals_are_refused(start: int, end: int) -> None:
    with pytest.raises(ValueError, match="start < end"):
        Interval(at(start), at(end))


def test_intervals_never_compare_across_clocks() -> None:
    with pytest.raises(DomainMismatchError):
        Interval(at(1), at(5, BOOT_CLOCK))
    civil = Interval(at(1), OPEN)
    with pytest.raises(DomainMismatchError):
        civil.overlaps(Interval(at(1, BOOT_CLOCK), OPEN))
    with pytest.raises(DomainMismatchError):
        civil.contains(at(3, DAYS))


def test_civil_clock_ids_depend_only_on_the_definition() -> None:
    again = CivilClock(Timescale.POSIX, Epoch.UNIX, Fraction(1))
    assert again.domain_id == SECONDS.domain_id
    assert DAYS.domain_id != SECONDS.domain_id
    assert CivilClock(Timescale.TAI, Epoch.UNIX, Fraction(1)).domain_id != SECONDS.domain_id


@pytest.mark.parametrize(
    ("timescale", "epoch", "resolution"),
    [
        (Timescale.MONOTONIC, Epoch.UNIX, Fraction(1)),
        (Timescale.SIMULATED, Epoch.UNIX, Fraction(1)),
        (Timescale.POSIX, Epoch.BOOT, Fraction(1)),
        (Timescale.POSIX, Epoch.UNIX, Fraction(0)),
        (Timescale.POSIX, Epoch.UNIX, 1),
    ],
)
def test_civil_clocks_are_absolute(timescale: Timescale, epoch: Epoch, resolution: Any) -> None:
    with pytest.raises(ValueError):
        CivilClock(timescale, epoch, resolution)


@pytest.mark.parametrize("bad", [-1, 2**63, True, 1.0])
def test_ledger_transactions_are_int64_sequence_numbers(bad: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        ledger_tx(bad)


# --- Claims -----------------------------------------------------------------------------------


def test_claim_id_is_deterministic_and_ignores_bookkeeping() -> None:
    first = located()
    assert first.id == located().id
    parse_claim_id(first.id)
    later = replace(first, recorded_at=ledger_tx(9), superseded_at=ledger_tx(12))
    assert later.id == first.id
    assert replace(first, valid_to=at(30)).id != first.id
    assert replace(first, provenance=provenance(1)).id != first.id
    assert dumps(first.to_json()) == dumps(located().to_json())


def test_claim_json_is_canonical_for_every_object_kind() -> None:
    kg = unit_from_text("kg")
    objects: list[ClaimObject] = [
        CELL,
        LedgerRecordRef(RECORD),
        TypedLiteral(ValueType.TEXT, "thruster 3 fault"),
        TypedLiteral(ValueType.INTEGER, 3),
        TypedLiteral(ValueType.REAL, NonFinite.POSITIVE_INFINITY),
        TypedLiteral(ValueType.BOOLEAN, False),
        TypedLiteral(ValueType.QUANTITY, 12.5, kg),
        TypedLiteral(ValueType.QUANTITY, 7, Unknown()),
        TypedLiteral(ValueType.INSTANT, at(5, BOOT_CLOCK)),
    ]
    for obj in objects:
        built = replace(located(), object=obj)
        assert dumps(built.to_json())
    assert object_type(LedgerRecordRef(RECORD)) is ValueType.RECORD
    assert object_type(CELL) is NodeType.SITE


@pytest.mark.parametrize(
    ("datatype", "value", "unit"),
    [
        (ValueType.TEXT, 3, NotApplicable()),
        (ValueType.INTEGER, True, NotApplicable()),
        (ValueType.INTEGER, 1.0, NotApplicable()),
        (ValueType.REAL, float("nan"), NotApplicable()),
        (ValueType.REAL, 1, NotApplicable()),
        (ValueType.BOOLEAN, 1, NotApplicable()),
        (ValueType.INSTANT, 5, NotApplicable()),
        (ValueType.QUANTITY, 1.0, NotApplicable()),  # a quantity always says what its unit is
        (ValueType.QUANTITY, 1.0, NotCovered()),
        (ValueType.QUANTITY, 1.0, Known("kg")),  # a unit is a Unit, never bare text
        (ValueType.TEXT, "x", Unknown()),  # only quantities have units
        (ValueType.RECORD, "x", NotApplicable()),
    ],
)
def test_literals_refuse_values_and_units_their_type_cannot_hold(
    datatype: ValueType, value: Any, unit: Any
) -> None:
    with pytest.raises((TypeError, ValueError)):
        TypedLiteral(datatype, value, unit)


def test_confidence_is_absent_for_deterministic_claims_and_bounded_for_inferred() -> None:
    for kind in (OBSERVED, STATED):
        with pytest.raises(ValueError, match="deterministic"):
            claim(ARM, "located_at", CELL, 0, tx=0, kind=kind, confidence=Known(1.0))
    assert claim(ARM, "located_at", CELL, 0, tx=0, kind=INFERRED, confidence=Unknown())
    bad: Any
    for bad in (NotApplicable(), Known(1.5), Known(1), Known(-0.1)):
        with pytest.raises(ValueError):
            claim(ARM, "located_at", CELL, 0, tx=0, kind=INFERRED, confidence=bad)


@pytest.mark.parametrize("bad", ["guessed", "observed", "INFERRED", None])
def test_assertion_kind_is_observed_stated_or_inferred(bad: Any) -> None:
    with pytest.raises(ValueError, match="assertion_kind"):
        replace(located(), assertion_kind=bad)


@pytest.mark.parametrize(
    "overrides",
    [
        {"predicate": "Located At"},
        {"subject": "ur10e-07"},
        {"object": "assembly-cell-2"},
        {"valid_to": at(10)},
        {"valid_to": at(20, BOOT_CLOCK)},
        {"recorded_at": -1},
        {"superseded_at": ledger_tx(0)},  # before recorded_at = 1
        {"provenance": "manifest.yaml"},
        {"supersedes": ["claim:sha256:" + "0" * 64]},
        {"supersedes": ("claim:sha256:" + "1" * 64, "claim:sha256:" + "0" * 64)},
        {"supersedes": ("claim:sha256:" + "0" * 64,) * 2},
        {"supersedes": ("rec:sha256:" + "0" * 64,)},
    ],
)
def test_malformed_claims_are_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        located(**overrides)


def test_a_claim_cannot_supersede_itself() -> None:
    first = located()
    with pytest.raises(ValueError, match="itself"):
        replace(first, supersedes=(first.id,))


@pytest.mark.parametrize(
    "fields",
    [
        {"evidence": ()},
        {"evidence": (evidence(0), evidence(0))},
        {"evidence": ("sha256:" + "0" * 64,)},
        {"records": (record_id("run", {"b": 1}), record_id("run", {"b": 1}))},
        {"records": [RECORD]},
        {"consolidator_id": "Fleet Manifest"},
        {"consolidator_version": ""},
        {"config_hash": "md5:abc"},
    ],
)
def test_provenance_refuses_what_cannot_be_cited(fields: dict[str, Any]) -> None:
    base: dict[str, Any] = {
        "evidence": (evidence(0),),
        "records": (),
        "consolidator_id": "memory.test",
        "consolidator_version": "1",
        "config_hash": CONFIG,
    }
    with pytest.raises((TypeError, ValueError)):
        ClaimProvenance(**(base | fields))


def test_records_must_be_sorted() -> None:
    a, b = sorted([record_id("run", {"n": 1}), record_id("run", {"n": 2})])
    assert ClaimProvenance((evidence(0),), (a, b), "memory.test", "1", CONFIG)
    with pytest.raises(ValueError, match="sorted"):
        ClaimProvenance((evidence(0),), (b, a), "memory.test", "1", CONFIG)


def test_valid_and_current_views() -> None:
    first = located()
    assert first.valid == Interval(at(10), OPEN)
    assert first.is_current
    assert not replace(first, superseded_at=ledger_tx(1)).is_current
    assert STATED.value == "stated"

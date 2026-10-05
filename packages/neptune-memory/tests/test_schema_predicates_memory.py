"""Vocabulary governance: registered predicates, domain/range checks, widening-only versions."""

from dataclasses import replace
from typing import Any

import pytest

from memory_schema_builders import INFERRED, MODEL, OBSERVED, STATED, claim, node, provenance
from neptune.identity.ids import record_id
from neptune.model.knowledge import AssertionKind, Known
from neptune.model.units import unit_from_text
from neptune_memory.schema.claim import TypedLiteral, ValueType
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.predicates import (
    CORE_PREDICATES,
    VOCABULARY_VERSION,
    Cardinality,
    ClaimSchemaError,
    PredicateRegistry,
    PredicateSpec,
    SchemaViolation,
    UnknownPredicateError,
    ViolationCode,
    check_claim,
    violations,
)

QUADRUPED = node(NodeType.MACHINE, "spot-03")
YARD = node(NodeType.SITE, "substation-yard")
RUN = node(NodeType.RUN, "run-2026-09-14-a")
OPERATOR = node(NodeType.PERSON, "badge:4411")  # a declared identifier: <namespace>:<value>
BADGE_RECORD = record_id("test.record", {"badge": 4411})


def codes(found: tuple[SchemaViolation, ...]) -> set[ViolationCode]:
    return {v.code for v in found}


def test_core_vocabulary_is_versioned_sorted_and_covers_every_node_type() -> None:
    assert VOCABULARY_VERSION == 8
    names = [spec.name for spec in CORE_PREDICATES.specs]
    assert names == sorted(set(names))
    covered = set().union(*(spec.domain for spec in CORE_PREDICATES.specs))
    assert covered == set(NodeType)
    assert CORE_PREDICATES.spec("located_at").cardinality is Cardinality.ONE
    assert CORE_PREDICATES.to_json()["predicates"]


def test_a_conforming_claim_has_no_violations() -> None:
    fact = claim(QUADRUPED, "located_at", YARD, 0, tx=0)
    assert violations(fact, CORE_PREDICATES) == ()
    check_claim(fact, CORE_PREDICATES)
    payload = TypedLiteral(ValueType.QUANTITY, 14.0, unit_from_text("kg"))
    check_claim(claim(QUADRUPED, "rated_payload", payload, 0, tx=0), CORE_PREDICATES)


def test_unknown_predicates_are_refused() -> None:
    fact = claim(QUADRUPED, "teleports_to", YARD, 0, tx=0)
    assert codes(violations(fact, CORE_PREDICATES)) == {ViolationCode.UNKNOWN_PREDICATE}
    with pytest.raises(ClaimSchemaError) as raised:
        check_claim(fact, CORE_PREDICATES)
    assert raised.value.violations[0].code is ViolationCode.UNKNOWN_PREDICATE
    with pytest.raises(UnknownPredicateError):
        CORE_PREDICATES.spec("teleports_to")


def test_subject_and_object_types_are_checked() -> None:
    backwards = claim(YARD, "located_at", QUADRUPED, 0, tx=0)
    assert codes(violations(backwards, CORE_PREDICATES)) == {
        ViolationCode.SUBJECT_TYPE,
        ViolationCode.OBJECT_TYPE,
    }
    text_payload = claim(QUADRUPED, "rated_payload", TypedLiteral(ValueType.TEXT, "14kg"), 0, tx=0)
    assert codes(violations(text_payload, CORE_PREDICATES)) == {ViolationCode.OBJECT_TYPE}


def test_people_are_declared_only() -> None:
    cited = provenance(0, records=(BADGE_RECORD,))
    declared = replace(claim(RUN, "operated_by", OPERATOR, 0, tx=0, kind=STATED), provenance=cited)
    assert violations(declared, CORE_PREDICATES) == ()
    observed = replace(declared, assertion_kind=OBSERVED)
    assert violations(observed, CORE_PREDICATES) == ()
    inferred = replace(
        declared,
        assertion_kind=INFERRED,
        confidence=Known(0.9),
        provenance=replace(cited, model=MODEL),
    )
    assert codes(violations(inferred, CORE_PREDICATES)) == {ViolationCode.DECLARED_ONLY}


@pytest.mark.parametrize("kind", [OBSERVED, STATED])
def test_observed_and_stated_claims_name_people_only_by_a_declared_record(
    kind: AssertionKind,
) -> None:
    cited = provenance(0, records=(BADGE_RECORD,))
    base = replace(claim(RUN, "operated_by", OPERATOR, 0, tx=0, kind=STATED), provenance=cited)
    base = replace(base, assertion_kind=kind)
    uncited = replace(base, provenance=provenance(0))  # no Ledger record declares the person
    # ADR 0006 §9: a blank or whitespace-padded value is not a declared identifier.
    padded = ("badge: 4411", "badge:4411 ", "badge: ", "badge:\t", "badge:\u00a04411")
    for name in ("operator-badge-4411", ":4411", "Badge:4411", "badge:", *padded):
        undeclared = replace(base, object=node(NodeType.PERSON, name))
        assert codes(violations(undeclared, CORE_PREDICATES)) == {ViolationCode.UNDECLARED_PERSON}
    assert codes(violations(uncited, CORE_PREDICATES)) == {ViolationCode.UNDECLARED_PERSON}
    with pytest.raises(ClaimSchemaError, match="undeclared_person"):
        check_claim(uncited, CORE_PREDICATES)


def spec(
    version: int, domain: set[NodeType], range_: set[NodeType | ValueType], card: Cardinality
) -> PredicateSpec:
    return PredicateSpec("docked_at", version, frozenset(domain), frozenset(range_), card, "dock")


def test_the_vocabulary_extends_and_versions_only_widen() -> None:
    v1 = spec(1, {NodeType.MACHINE}, {NodeType.ASSET}, Cardinality.ONE)
    extended = CORE_PREDICATES.extend(v1)
    assert "docked_at" in extended and "docked_at" not in CORE_PREDICATES
    wider = spec(2, {NodeType.MACHINE, NodeType.ASSET}, {NodeType.ASSET}, Cardinality.ONE)
    assert extended.extend(wider).spec("docked_at").version == 2
    for bad in (
        spec(1, {NodeType.MACHINE, NodeType.ASSET}, {NodeType.ASSET}, Cardinality.ONE),  # same v
        spec(2, {NodeType.ASSET}, {NodeType.ASSET}, Cardinality.ONE),  # narrows the domain
        spec(2, {NodeType.MACHINE}, {NodeType.SITE}, Cardinality.ONE),  # replaces the range
        spec(2, {NodeType.MACHINE}, {NodeType.ASSET}, Cardinality.MANY),  # changes cardinality
    ):
        with pytest.raises(ValueError, match="widen"):
            extended.extend(bad)


@pytest.mark.parametrize(
    "args",
    [
        ("Docked At", 1, {NodeType.MACHINE}, {NodeType.ASSET}, Cardinality.ONE, "d"),
        ("docked_at", 0, {NodeType.MACHINE}, {NodeType.ASSET}, Cardinality.ONE, "d"),
        ("docked_at", True, {NodeType.MACHINE}, {NodeType.ASSET}, Cardinality.ONE, "d"),
        ("docked_at", 1, set(), {NodeType.ASSET}, Cardinality.ONE, "d"),
        ("docked_at", 1, {"machine"}, {NodeType.ASSET}, Cardinality.ONE, "d"),
        ("docked_at", 1, {NodeType.MACHINE}, set(), Cardinality.ONE, "d"),
        ("docked_at", 1, {NodeType.MACHINE}, {"asset"}, Cardinality.ONE, "d"),
        ("docked_at", 1, {NodeType.MACHINE}, {NodeType.ASSET}, "one", "d"),
        ("docked_at", 1, {NodeType.MACHINE}, {NodeType.ASSET}, Cardinality.ONE, ""),
    ],
)
def test_malformed_predicate_specs_are_refused(args: tuple[Any, ...]) -> None:
    name, version, domain, range_, card, description = args
    with pytest.raises((TypeError, ValueError)):
        PredicateSpec(name, version, frozenset(domain), frozenset(range_), card, description)


def test_registries_refuse_duplicate_or_unsorted_names() -> None:
    a = spec(1, {NodeType.MACHINE}, {NodeType.ASSET}, Cardinality.ONE)
    with pytest.raises(ValueError, match="unique and sorted"):
        PredicateRegistry((a, a))
    located = CORE_PREDICATES.spec("located_at")
    with pytest.raises(ValueError, match="unique and sorted"):
        PredicateRegistry((located, a))

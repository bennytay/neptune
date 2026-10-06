"""Declared names and values (ADR 0025): the ``declared_value`` literal and ``memory.declared``.

The records are the compiler's own, cut from the acceptance corpus's Ledger export
(``fixtures/declared_cell3.records.jsonl``): the CAL-ARM3A-0818 and -0911 hand-eye snapshots and
their values, the vision PC's OpenCV calibration, their source revisions and the run sheet's
declarations of the 2026-09-09 and 2026-09-14 runs. Where the configuration and run consolidators
place those nodes is given as their claims, so each test states the placement it relies on.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from memory_catalog_threads import anchored, catalog, package_id, thread_id
from memory_schema_builders import claim
from neptune.model.ids import RecordId
from neptune.model.knowledge import NotApplicable, Unknown
from neptune.model.scalars import NonFinite
from neptune.model.units import unit_from_text
from neptune_memory.consolidate.base import run_consolidator
from neptune_memory.consolidate.declared import DeclaredConsolidator
from neptune_memory.schema.claim import (
    DeclaredType,
    DeclaredValue,
    LedgerRecordRef,
    TypedLiteral,
    ValueType,
)
from neptune_memory.schema.codec import object_from_json
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from neptune_memory.consolidate.base import Consolidation
    from neptune_memory.schema.claim import Claim

FIXTURE: Final = Path(__file__).parent / "fixtures" / "declared_cell3.records.jsonl"
RECORDS: Final[list[dict[str, Any]]] = [
    json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()
]
CAL_0818: Final = "rec:sha256:a9b42a8930b9e7a007bcb97a798d3561037cff49693b8c36e5677c5896b69dbc"
CAL_0911: Final = "rec:sha256:853510ade5ecebf41d2be75511f189701d74d3f724e4295154c107b6c7214bed"
OPENCV: Final = "rec:sha256:37001c5b23f5fd2dd7442b0d2e8dc1bb620b321d8ab5e80650c14152b6e2a6b7"
RUN_0909: Final = RecordId(
    "rec:sha256:746836797fb54529ed8c096eec02daeb48f2174b9eb8a4d5fcbb817a1e8f7379"
)
PACKAGE: Final = package_id("cell3")


def record(rid: str) -> dict[str, Any]:
    return next(r for r in RECORDS if r.get("id") == rid)


def config_node(rid: str) -> NodeRef:
    """The configuration node a snapshot opens: its anchored Ledger thread (ADR 0024)."""
    return NodeRef(
        NodeType.CONFIGURATION, "thread:" + thread_id(anchored("configuration", record(rid)))
    )


RUN_NODE: Final = NodeRef(NodeType.RUN, f"record:{RUN_0909}")


def placements(*nodes: NodeRef) -> list[Claim]:
    """The 0909 run (two runs of placements for 0818 and the OpenCV file) as the configuration and
    run consolidators place them."""
    out = [
        claim(
            RUN_NODE,
            "evidenced_by",
            LedgerRecordRef(RUN_0909),
            100,
            200,
            tx=1,
            consolidator="memory.runs",
        ),
    ]
    for node in nodes:
        for start, end in ((100, 200), (300, 400)):
            out.append(
                claim(
                    RUN_NODE,
                    "configuration_active_during",
                    node,
                    start,
                    end,
                    tx=1,
                    consolidator="memory.configuration",
                )
            )
    return out


def run(records: list[dict[str, Any]], previous: list[Claim]) -> Consolidation:
    return run_consolidator(
        DeclaredConsolidator(), catalog({PACKAGE: records}), previous, {}, recorded_at=ledger_tx(2)
    )


def values(result: Consolidation, node: NodeRef, *path: str | int) -> list[Claim]:
    return [
        c
        for c in result.claims
        if c.subject == node
        and c.predicate == "declared_value"
        and isinstance(c.object, TypedLiteral)
        and isinstance(c.object.value, DeclaredValue)
        and c.object.value.path == path
    ]


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


# --- The literal --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "unit"),
    [
        (DeclaredValue(("transformation", "z"), DeclaredType.REAL, 0.0745), Unknown()),
        (
            DeclaredValue(("runs", 0, "name"), DeclaredType.TEXT, "cell3-2026-09-09"),
            NotApplicable(),
        ),
        (DeclaredValue(("eye_on_hand",), DeclaredType.BOOLEAN, True), NotApplicable()),
        (DeclaredValue(("count",), DeclaredType.INTEGER, 24), unit_from_text("m")),
        (DeclaredValue(("t",), DeclaredType.REALS, (0.0334, NonFinite("inf"))), Unknown()),
    ],
)
def test_a_declared_value_round_trips_through_the_codec(value: DeclaredValue, unit: Any) -> None:
    literal = TypedLiteral(ValueType.DECLARED_VALUE, value, unit)
    assert object_from_json(literal.to_json()) == literal


@pytest.mark.parametrize(
    ("make", "error"),
    [
        (lambda: DeclaredValue((), DeclaredType.TEXT, "x"), ValueError),
        (lambda: DeclaredValue((-1,), DeclaredType.TEXT, "x"), TypeError),
        (lambda: DeclaredValue((True,), DeclaredType.TEXT, "x"), TypeError),
        (lambda: DeclaredValue(("a",), DeclaredType.INTEGER, True), TypeError),
        (lambda: DeclaredValue(("a",), DeclaredType.REAL, float("nan")), TypeError),
        (lambda: DeclaredValue(("a",), DeclaredType.REALS, ()), ValueError),
        (lambda: DeclaredValue(("a",), DeclaredType.TEXT, NonFinite("inf")), TypeError),
        (lambda: DeclaredValue(("a",) * 65, DeclaredType.TEXT, "x"), ValueError),
        # Text has no unit; a number's unit inherits the claim's provenance and is never absent.
        (
            lambda: TypedLiteral(
                ValueType.DECLARED_VALUE, DeclaredValue(("a",), DeclaredType.TEXT, "x"), Unknown()
            ),
            ValueError,
        ),
        (
            lambda: TypedLiteral(
                ValueType.DECLARED_VALUE, DeclaredValue(("a",), DeclaredType.REAL, 1.0)
            ),
            ValueError,
        ),
    ],
)
def test_a_malformed_declared_value_is_refused(make: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        make()


@pytest.mark.parametrize(
    "encoded",
    [
        {"path": [], "type": "text", "value": "x"},
        {"path": ["a"], "type": "real", "value": 1},
        {"path": ["a"], "type": "boolean", "value": 1},
        {"path": ["a"], "type": "reals", "value": [1]},
        {"path": ["a"], "type": "text", "value": "x", "unit": "m"},
        {"path": ["a"], "type": "complex", "value": "x"},
    ],
)
def test_the_codec_refuses_a_malformed_declared_value(encoded: dict[str, Any]) -> None:
    literal: dict[str, Any] = {
        "datatype": "declared_value",
        "kind": "literal",
        "unit": {"knowledge": "not_applicable"},
        "value": encoded,
    }
    with pytest.raises((ValueError, TypeError)):
        object_from_json(literal)


# --- The consolidator ---------------------------------------------------------------------------


def test_configuration_values_are_claimed_as_stated_where_the_configuration_is_placed() -> None:
    node = config_node(CAL_0818)
    result = run(RECORDS, placements(node))
    z = values(result, node, "transformation", "z")
    assert len(z) == 2  # one per placement, each over the placement's interval
    assert {(c.valid_from.ticks, c.valid_to.ticks) for c in z} == {(100, 200), (300, 400)}  # type: ignore[union-attr]
    first = z[0]
    assert first.object == TypedLiteral(
        ValueType.DECLARED_VALUE,
        DeclaredValue(("transformation", "z"), DeclaredType.REAL, 0.0745),
        Unknown(),  # YAML declares no unit: never assumed metres
    )
    assert first.assertion_kind.value == "observed"  # type: ignore[union-attr]
    # Cited at the value's own span in CAL-ARM3A-0818.yaml, with its record and snapshot.
    (cited,) = first.provenance.evidence
    assert [step.to_json() for step in cited.locator] == [
        {"end": 404, "kind": "span", "start": 398}
    ]
    assert CAL_0818 in first.provenance.records
    # Collections are not values; their leaves are. 0818 declares 16 scalars.
    scalars = [c for c in result.claims if c.subject == node and c.predicate == "declared_value"]
    assert len(scalars) == 2 * 16
    name = [c for c in result.claims if c.subject == node and c.predicate == "has_name"]
    assert {c.object.value for c in name} == {"sites/PLANT-2/cell3/calibration/CAL-ARM3A-0818.yaml"}  # type: ignore[union-attr]
    assert {c.assertion_kind.value for c in name} == {"observed"}  # type: ignore[union-attr]


def test_calibration_parameters_are_claimed_with_their_declared_units() -> None:
    node = config_node(OPENCV)
    result = run(RECORDS, placements(node))
    error = values(result, node, "reprojection_error")
    assert {c.object for c in error} == {
        TypedLiteral(
            ValueType.DECLARED_VALUE,
            DeclaredValue(("reprojection_error",), DeclaredType.REALS, (1.86,)),
            Unknown(),
        )
    }
    board = values(result, node, "board")
    assert {c.object.value.value for c in board} == {"ChArUco 7x5, 30 mm (substitute)"}  # type: ignore[union-attr]
    calibration_id = values(result, node, "calibration_id")
    assert {c.object.value.value for c in calibration_id} == {"CAL-ARM3A-0911"}  # type: ignore[union-attr]
    translation = values(result, node, "t_cam2gripper/data")
    assert {c.object.value.value for c in translation} == {(0.0334, -0.0103, 0.0702)}  # type: ignore[union-attr]


def test_a_run_is_named_by_its_declaration() -> None:
    result = run(RECORDS, placements())
    named = [c for c in result.claims if c.subject == RUN_NODE and c.predicate == "has_name"]
    assert [(c.object.value, c.assertion_kind.value) for c in named] == [  # type: ignore[union-attr]
        ("cell3-2026-09-09", "stated")
    ]
    (cited,) = named[0].provenance.evidence
    assert cited.locator[0].to_json() == {"kind": "json_pointer", "pointer": "/runs/1/name"}


def test_an_unplaced_configuration_states_nothing_and_says_how_much_waits() -> None:
    result = run(RECORDS, [])
    assert not result.claims
    (unplaced,) = [f for f in result.findings if f.code == "declared.unplaced"]
    assert unplaced.details["node_count"] == 3  # 0818, 0911 and the OpenCV file
    assert unplaced.details["count"] == 3 * 1 + 16 + 16 + 23  # three names and every value


def test_a_value_with_no_value_or_no_locator_is_a_counted_finding_never_a_fact() -> None:
    records = copy.deepcopy(RECORDS)
    z = next(
        r
        for r in records
        if r["kind"] == "configuration_value"
        and r["snapshot"] == CAL_0818
        and r["path"] == ["transformation", "z"]
    )
    z["value"] = {"knowledge": "unknown"}  # a blank the format could have filled
    z["text"] = {"knowledge": "unknown"}
    opencv = next(r for r in records if r.get("id") == OPENCV)
    error = next(p for p in opencv["parameters"] if p["name"] == "reprojection_error")
    del error["value"]["provenance"]  # the number cites no place of its own
    node, cal = config_node(CAL_0818), config_node(OPENCV)
    result = run(records, placements(node, cal))
    assert values(result, node, "transformation", "z") == []
    assert values(result, cal, "reprojection_error") == []
    assert values(result, node, "transformation", "x")  # the rest is unaffected
    found = {f.code: f for f in result.findings}
    assert found["declared.value_unstated"].details["count"] == 1
    assert found["declared.value_unlocated"].records == (OPENCV,)


def test_a_malformed_record_is_a_finding_and_the_rest_is_claimed() -> None:
    records = copy.deepcopy(RECORDS)
    broken = next(r for r in records if r["kind"] == "configuration_value")
    broken["path"] = "not a path"
    result = run(records, placements(config_node(CAL_0818)))
    assert "declared.malformed_record" in codes(result)
    assert values(result, config_node(CAL_0818), "transformation", "z")


def test_two_declarations_naming_one_run_differently_name_it_nothing() -> None:
    records = copy.deepcopy(RECORDS)
    other = copy.deepcopy(next(r for r in records if r.get("run") == RUN_0909))
    other["id"] = "rec:sha256:" + "0" * 64
    other["logical_id"]["value"]["value"] = "cell3-reference"
    records.append(other)
    result = run(records, placements())
    assert not [c for c in result.claims if c.subject == RUN_NODE]
    assert "declared.name_conflict" in codes(result)


def test_a_configuration_without_a_thread_names_no_node() -> None:
    ledger = catalog({PACKAGE: RECORDS}, without=[CAL_0911])
    result = run_consolidator(
        DeclaredConsolidator(), ledger, placements(), {}, recorded_at=ledger_tx(2)
    )
    (unthreaded,) = [f for f in result.findings if f.code == "declared.unthreaded"]
    assert unthreaded.records == (CAL_0911,)


def test_the_output_is_deterministic_whatever_the_record_order() -> None:
    node = config_node(CAL_0818)
    first = run(RECORDS, placements(node))
    second = run(list(reversed(RECORDS)), list(reversed(placements(node))))
    assert [c.id for c in first.claims] == [c.id for c in second.claims]
    assert [f.id for f in first.findings] == [f.id for f in second.findings]

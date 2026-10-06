"""The ``delta`` value type (ADR 0014 §4): what it accepts, what it refuses, and its JSON."""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from memory_identity_records import STATED, TRANSFORM, cite
from memory_schema_builders import OBSERVED, claim, node
from neptune.identity import canonical_json
from neptune.model.frames import FrameRef, TransformDirection
from neptune.model.ids import RecordId
from neptune.model.knowledge import Known, NotApplicable, Unknown
from neptune.model.provenance import Provenance
from neptune.model.units import unit_from_text
from neptune_memory.schema.claim import (
    DeclaredTransform,
    Delta,
    DeltaAdjustment,
    DeltaQuantity,
    TypedLiteral,
    ValueType,
)
from neptune_memory.schema.codec import claim_from_json, delta_from_json
from neptune_memory.schema.export import graph_schema
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES, DRIFT, violations

EARLIER = RecordId("rec:sha256:" + "1" * 64)
LATER = RecordId("rec:sha256:" + "2" * 64)
GRAPH = RecordId("rec:sha256:" + "3" * 64)
EDGE = (FrameRef("tool0", GRAPH), FrameRef("wrist_camera", GRAPH))
MM = unit_from_text("mm")
CAMERA = node(NodeType.SENSOR, "serial:CAM-7731")
SCHEMA = graph_schema()


def parameter(values: tuple[float, ...] = (0.02,)) -> Delta:
    return Delta(EARLIER, LATER, DeltaQuantity.PARAMETER, "values", values, name="focal_length")


SIDE = DeclaredTransform("tool0", "wrist_camera", TransformDirection.CHILD_TO_PARENT)


def rotation(
    values: tuple[float, ...] = (0.0, 0.0, 0.01, -5e-05),
    adjustment: DeltaAdjustment = DeltaAdjustment.LATER_NEGATED,
) -> Delta:
    return Delta(
        EARLIER,
        LATER,
        DeltaQuantity.ROTATION,
        "quaternion",
        values,
        edge=EDGE,
        transform=SIDE,
        adjustment=adjustment,
    )


def _validate(name: str, value: Any) -> None:
    Draft202012Validator({**SCHEMA, "anyOf": [{"$ref": f"#/$defs/{name}"}]}).validate(value)


@pytest.mark.parametrize(
    ("make", "error"),
    [
        (
            lambda: Delta(EARLIER, EARLIER, DeltaQuantity.PARAMETER, "values", (1.0,), name="k"),
            ValueError,
        ),
        (
            lambda: Delta(EARLIER, LATER, DeltaQuantity.PARAMETER, "quaternion", (1.0,), name="k"),
            ValueError,
        ),
        (
            lambda: Delta(
                EARLIER, LATER, DeltaQuantity.ROTATION, "quaternion", (1.0, 2.0), edge=EDGE
            ),
            ValueError,
        ),
        (
            lambda: Delta(EARLIER, LATER, DeltaQuantity.PARAMETER, "values", (), name="k"),
            ValueError,
        ),
        (
            lambda: Delta(
                EARLIER, LATER, DeltaQuantity.PARAMETER, "values", (float("nan"),), name="k"
            ),
            TypeError,
        ),
        (
            lambda: Delta(EARLIER, LATER, DeltaQuantity.PARAMETER, "values", (1,), name="k"),
            TypeError,
        ),
        (
            lambda: Delta(EARLIER, LATER, DeltaQuantity.PARAMETER, "values", (1.0,), edge=EDGE),
            ValueError,
        ),
        (
            lambda: Delta(
                EARLIER,
                LATER,
                DeltaQuantity.TRANSLATION,
                "translation",
                (1.0, 0.0, 0.0),
                transform=SIDE,
            ),
            ValueError,
        ),
        (
            lambda: Delta(
                EARLIER,
                LATER,
                DeltaQuantity.TRANSLATION,
                "translation",
                (1.0, 0.0, 0.0),
                edge=(EDGE[0], FrameRef("cam", "rec:sha256:" + "4" * 64)),  # type: ignore[arg-type]
                transform=SIDE,
            ),
            ValueError,
        ),
        (lambda: rotation(adjustment=DeltaAdjustment.WRAPPED), ValueError),
        (
            lambda: Delta(
                EARLIER,
                LATER,
                DeltaQuantity.ROTATION,
                "quaternion",
                (0.0,) * 4,
                edge=EDGE,
                transform=SIDE,
            ),
            ValueError,
        ),
        (
            lambda: Delta(
                EARLIER,
                LATER,
                DeltaQuantity.TRANSLATION,
                "translation",
                (1.0, 0.0, 0.0),
                edge=EDGE,
                transform=SIDE,
                adjustment=DeltaAdjustment.NONE,
            ),
            ValueError,
        ),
        (
            lambda: Delta(
                EARLIER, LATER, DeltaQuantity.TRANSLATION, "translation", (1.0, 0.0, 0.0), edge=EDGE
            ),
            TypeError,
        ),
        (
            lambda: Delta(
                EARLIER, LATER, DeltaQuantity.PARAMETER, "values", (0.0,) * 100_001, name="k"
            ),
            ValueError,
        ),
    ],
)
def test_a_delta_refuses_what_it_cannot_mean(make: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        make()


def test_a_deltas_unit_is_the_declared_one_or_none_for_a_form_without_one() -> None:
    TypedLiteral(ValueType.DELTA, parameter(), MM)
    TypedLiteral(ValueType.DELTA, rotation(), NotApplicable())
    with pytest.raises(ValueError, match="Known"):
        TypedLiteral(ValueType.DELTA, parameter(), Unknown())  # never between unstated units
    with pytest.raises(ValueError, match="NotApplicable"):
        TypedLiteral(ValueType.DELTA, rotation(), MM)
    cited = Known(MM.value, Provenance(cite("unit"), TRANSFORM.id, STATED))  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        TypedLiteral(ValueType.DELTA, parameter(), cited)
    with pytest.raises(TypeError):
        TypedLiteral(ValueType.DELTA, 0.5, MM)
    with pytest.raises(TypeError):
        TypedLiteral(ValueType.QUANTITY, parameter(), MM)


@pytest.mark.parametrize(
    "literal",
    [
        TypedLiteral(ValueType.DELTA, parameter(), MM),
        TypedLiteral(ValueType.DELTA, rotation(), NotApplicable()),
    ],
    ids=["parameter", "rotation"],
)
def test_a_drift_claim_round_trips_and_validates_against_the_published_schema(
    literal: TypedLiteral,
) -> None:
    drift = claim(CAMERA, DRIFT, literal, 200, 400, tx=1, kind=OBSERVED)
    assert not violations(drift, CORE_PREDICATES)
    data = canonical_json.loads(canonical_json.dumps(drift.to_json()))
    _validate("Claim", data)
    again = claim_from_json(data)
    assert again == drift and again.id == drift.id


def test_the_strict_reader_and_the_schema_refuse_a_mixed_delta() -> None:
    data: dict[str, Any] = canonical_json.loads(canonical_json.dumps(parameter().to_json()))  # type: ignore[assignment]
    with pytest.raises(ValueError):
        delta_from_json({**data, "parent": EDGE[0].to_json()})
    with pytest.raises(ValueError):
        delta_from_json({**data, "values": [{"non_finite": "nan"}]})
    with pytest.raises(ValidationError):
        _validate("Delta", {**data, "parent": EDGE[0].to_json()})
    with pytest.raises(ValidationError):
        _validate("Delta", {**data, "values": []})


def test_drift_takes_only_a_delta() -> None:
    quantity = claim(CAMERA, DRIFT, TypedLiteral(ValueType.QUANTITY, 0.02, MM), 200, 400, tx=1)
    assert [v.code for v in violations(quantity, CORE_PREDICATES)] == ["object_type"]

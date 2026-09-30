from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.identity import canonical_json
from neptune.identity.ids import record_id
from neptune.model.frames import (
    MAX_TEXT_LENGTH,
    STATIC,
    AxisConvention,
    EulerAngles,
    EulerMode,
    EulerSequence,
    FrameRef,
    Handedness,
    HomogeneousMatrix,
    MatrixLayout,
    Pose,
    Quaternion,
    QuaternionConvention,
    QuaternionOrder,
    Rotation,
    RotationMatrix,
    RotationVector,
    Translation,
    frame_ref_from_json,
    rotation_from_json,
    transform_value_from_json,
    validity_from_json,
    validity_to_json,
)
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    NotApplicable,
    Unknown,
)
from neptune.model.time import Timestamp
from neptune.model.units import unit_from_json


@dataclass(frozen=True)
class Cite:
    """Stand-in for ``Provenance``: any evidence-layer ``Grounding``."""

    where: str
    assertion_kind: AssertionKind = AssertionKind.OBSERVED

    def to_json(self) -> JsonObject:
        return {"where": self.where}


def cite(data: JsonObject) -> Cite:
    where = data["where"]
    assert isinstance(where, str)
    return Cite(where)


TF = record_id("frame_graph", {"source": "a.mcap", "scope": ["/tf", "/tf_static"]})
URDF = record_id("frame_graph", {"source": "robot.urdf"})
CLOCK = record_id("timestamp_domain", {"source": "a.mcap", "field": "log_time"})
M, MM, RAD, DEG = (Known(unit_from_json(u)) for u in ("m", "mm", "rad", "deg"))
XYZW = Known(QuaternionOrder.XYZW, Cite("geometry_msgs/Quaternion.msg"))
HAMILTON = Known(QuaternionConvention.HAMILTON, Cite("tf2 docs"))
ROW = Known(MatrixLayout.ROW_MAJOR)
IDENTITY_4X4 = tuple(float(i == j) for i in range(4) for j in range(4))


def base_link(graph: str = TF, frame_id: str = "base_link") -> FrameRef:
    return FrameRef(frame_id, graph)  # type: ignore[arg-type]


def tf_pose(rotation: Rotation | None = None) -> Pose:
    return Pose(
        Translation((0.1, 0.0, -0.0), M),
        rotation or Quaternion((0.0, 0.0, 0.0, 1.0), XYZW, HAMILTON),
    )


def round_trip(value: Any, decode: Any) -> None:
    data = canonical_json.dumps(value.to_json())
    assert decode(canonical_json.loads(data)) == value
    assert canonical_json.dumps(decode(canonical_json.loads(data)).to_json()) == data


# --- FrameRef: names verbatim, graphs source-scoped -----------------------------------------


def test_frame_ids_are_kept_verbatim() -> None:
    names = ["base_link", "/base_link", "Base_Link", " base_link", "base_linḱ"]
    refs = {base_link(frame_id=name) for name in names}
    assert len(refs) == len(names)
    assert [base_link(frame_id=name).frame_id for name in names] == names


def test_equal_names_in_two_graphs_are_two_frames() -> None:
    assert base_link(TF) != base_link(URDF)


@pytest.mark.parametrize(
    ("frame_id", "graph", "error"),
    [
        ("", TF, ValueError),
        ("\ud800", TF, ValueError),
        ("x" * (MAX_TEXT_LENGTH + 1), TF, ValueError),
        (b"base_link", TF, TypeError),
        ("base_link", "base_graph", ValueError),
        ("base_link", TF.upper(), ValueError),
    ],
)
def test_bad_frame_refs_are_rejected(frame_id: Any, graph: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        FrameRef(frame_id, graph)


def test_longest_frame_id_is_accepted() -> None:
    assert len(base_link(frame_id="x" * MAX_TEXT_LENGTH).frame_id) == MAX_TEXT_LENGTH


# --- Conventions -----------------------------------------------------------------------------

_AXIS = {
    "e": (1, 0, 0),
    "w": (-1, 0, 0),
    "n": (0, 1, 0),
    "s": (0, -1, 0),
    "f": (1, 0, 0),
    "b": (-1, 0, 0),
    "l": (0, 1, 0),
    "r": (0, -1, 0),
    "u": (0, 0, 1),
    "d": (0, 0, -1),
}


@pytest.mark.parametrize("convention", list(AxisConvention))
def test_handedness_matches_the_axis_directions(convention: AxisConvention) -> None:
    x, y, z = (_AXIS[letter] for letter in convention.value)
    cross = (x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0])
    assert cross in (z, tuple(-c for c in z))
    assert convention.handedness is (Handedness.RIGHT if cross == z else Handedness.LEFT)


# --- Acceptance: nothing is normalised at construction ----------------------------------------


def test_quaternion_components_are_kept_in_declared_order() -> None:
    values = (0.5, -0.5, 0.25, 0.8)
    xyzw = Quaternion(values, XYZW, HAMILTON)
    wxyz = Quaternion(values, Known(QuaternionOrder.WXYZ), HAMILTON)
    assert xyzw.values == wxyz.values == values
    assert xyzw != wxyz


def test_values_are_not_normalised() -> None:
    # Not unit-norm, not orthonormal, bottom row not [0 0 0 1]: validation's job, not ours.
    Quaternion((1.0, 2.0, 3.0, 4.0), XYZW, HAMILTON)
    Quaternion((0.0, 0.0, 0.0, 0.0), XYZW, HAMILTON)
    RotationMatrix(tuple(float(i) for i in range(9)), ROW)
    matrix = HomogeneousMatrix(tuple(float(i) for i in range(16)), ROW, MM)
    assert matrix.values == tuple(float(i) for i in range(16))


def test_units_are_kept_as_declared() -> None:
    assert Translation((100.0, 0.0, 0.0), MM).unit == MM
    assert EulerAngles((0.0, 0.0, 90.0), Known(EulerSequence.XYZ), Known(EulerMode.EXTRINSIC), DEG)


# --- Acceptance: undeclared is Unknown / Ambiguous, and the numbers survive -----------------------


def test_undeclared_quaternion_order_keeps_the_numbers() -> None:
    values = (0.1, 0.2, 0.3, 0.9)
    unknown = Quaternion(values, Unknown(), Unknown())
    ambiguous = Quaternion(
        values,
        Ambiguous((Candidate(QuaternionOrder.XYZW), Candidate(QuaternionOrder.WXYZ))),
        HAMILTON,
    )
    assert unknown.values == ambiguous.values == values
    for rotation in (unknown, ambiguous):
        round_trip(rotation, lambda d: rotation_from_json(d, cite))


def test_unknown_units_and_layouts_are_representable() -> None:
    euler = EulerAngles((1.0, 2.0, 3.0), Unknown(), Unknown(), Unknown())
    matrix = HomogeneousMatrix(IDENTITY_4X4, Unknown(), Unknown())
    ambiguous_unit = Ambiguous((Candidate(unit_from_json("deg")), Candidate(unit_from_json("rad"))))
    vector = RotationVector((0.0, 0.0, 1.5), ambiguous_unit)
    for rotation in (euler, vector):
        round_trip(rotation, lambda d: rotation_from_json(d, cite))
    round_trip(matrix, lambda d: transform_value_from_json(d, cite))


# --- Malformed input and boundaries ------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "error"),
    [
        ((0.0, 0.0, 1.0), ValueError),  # three components
        ((0.0, 0.0, 0.0, 1.0, 0.0), ValueError),
        ([0.0, 0.0, 0.0, 1.0], TypeError),  # a list is mutable
        ((0, 0, 0, 1), TypeError),  # ints: 1 and 1.0 are different JSON
        ((0.0, 0.0, 0.0, True), TypeError),
        ((0.0, 0.0, 0.0, float("nan")), ValueError),
        ((0.0, 0.0, 0.0, float("inf")), ValueError),
    ],
)
def test_bad_components_are_rejected(values: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        Quaternion(values, XYZW, HAMILTON)


@pytest.mark.parametrize(
    ("build", "unit"),
    [
        (lambda u: Translation((0.0, 0.0, 0.0), u), "rad"),
        (lambda u: Translation((0.0, 0.0, 0.0), u), "m.s^-1"),
        (lambda u: EulerAngles((0.0, 0.0, 0.0), Unknown(), Unknown(), u), "m"),
        (lambda u: RotationVector((0.0, 0.0, 0.0), u), "1"),
        (lambda u: HomogeneousMatrix(IDENTITY_4X4, ROW, u), "deg"),
    ],
)
def test_units_of_the_wrong_dimension_are_rejected(build: Any, unit: str) -> None:
    with pytest.raises(ValueError, match="dimension"):
        build(Known(unit_from_json(unit)))
    ambiguous = Ambiguous((Candidate(unit_from_json(unit)), Candidate(unit_from_json("mm"))))
    with pytest.raises(ValueError, match="dimension"):
        build(ambiguous)


def test_a_bare_unit_string_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unit"):
        Translation((0.0, 0.0, 0.0), Known("m"))  # type: ignore[arg-type]


def test_pose_rejects_a_translation_in_place_of_a_rotation() -> None:
    with pytest.raises(TypeError):
        Pose(Translation((0.0, 0.0, 0.0), M), Translation((0.0, 0.0, 0.0), M))  # type: ignore[arg-type]


# --- JSON: shape, round trip, strictness, determinism -----------------------------------------


@pytest.mark.parametrize(
    "rotation",
    [
        Quaternion((0.0, 0.0, 0.0, 1.0), XYZW, HAMILTON),
        RotationMatrix(
            (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0), Known(MatrixLayout.COLUMN_MAJOR)
        ),
        EulerAngles((0.0, 0.0, 1.57), Known(EulerSequence.XYZ), Known(EulerMode.EXTRINSIC), RAD),
        RotationVector((0.0, 0.0, 1.57), RAD),
    ],
)
def test_every_rotation_round_trips(rotation: Rotation) -> None:
    round_trip(tf_pose(rotation), lambda d: tf_pose(rotation_from_json(d["rotation"], cite)))
    round_trip(tf_pose(rotation), lambda d: transform_value_from_json(d, cite))


def test_validity_is_static_or_stamped() -> None:
    assert validity_to_json(STATIC) == {"kind": "static"}
    stamp = Timestamp(-1, CLOCK)
    assert validity_to_json(stamp) == {"kind": "stamped", "stamp": stamp.to_json()}
    for validity in (STATIC, stamp):
        data = canonical_json.loads(canonical_json.dumps(validity_to_json(validity)))
        assert validity_from_json(data) == validity
    bad: list[JsonValue] = [{"kind": "interval"}, {"kind": "static", "stamp": 1}, "static"]
    for data in bad:
        with pytest.raises(ValueError):
            validity_from_json(data)
    round_trip(base_link(), frame_ref_from_json)


finite = st.floats(allow_nan=False, allow_infinity=False)


@given(
    values=st.tuples(finite, finite, finite, finite),
    order=st.sampled_from(QuaternionOrder),
    translation=st.tuples(finite, finite, finite),
)
def test_any_finite_pose_round_trips_exactly(
    values: tuple[float, ...], order: QuaternionOrder, translation: tuple[float, ...]
) -> None:
    pose = Pose(Translation(translation, MM), Quaternion(values, Known(order), NotApplicable()))
    round_trip(pose, lambda d: transform_value_from_json(d, cite))

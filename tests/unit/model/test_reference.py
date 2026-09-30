"""Clocks and frames as evidence records: provenance, ids, envelope and strict JSON (ADR 0017).

The values inside these records (ticks, rotations, poses) are tested in test_time.py and
test_frames.py. This file tests what makes them records.
"""

from dataclasses import replace
from fractions import Fraction
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.derived.provenance import InferredProvenance
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.provenance import (
    check_evidence_record_id,
    evidence_record_id,
    transform_record,
)
from neptune.model.frames import (
    STATIC,
    AxisConvention,
    FrameRef,
    Handedness,
    HomogeneousMatrix,
    MatrixLayout,
    Pose,
    Quaternion,
    QuaternionConvention,
    QuaternionOrder,
    TransformDirection,
    Translation,
)
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    KnownAbsent,
    NotApplicable,
    Unknown,
)
from neptune.model.provenance import (
    ByteRange,
    EvidenceRef,
    Locator,
    Provenance,
    RowCell,
    TransformRecord,
    adapter_locator,
)
from neptune.model.record import SCHEMA_VERSION, Family, SchemaVersionError
from neptune.model.reference import (
    Frame,
    FrameGraph,
    FrameTransform,
    TimestampDomain,
    frame_from_json,
    frame_graph_from_json,
    frame_transform_from_json,
    timestamp_domain_from_json,
)
from neptune.model.time import MICROSECOND, NANOSECOND, ClockRole, Epoch, Timescale, Timestamp
from neptune.model.units import unit_from_json

LOG_BYTES = b"\x89MCAP0\r\n" + bytes(56)
URDF_BYTES = b"<robot name='arm'><link name='base_link'/><link name='tool0'/></robot>"
CSV_BYTES = b"# t in microseconds\nt [ms],x\n0,1\n"
LOG, URDF, CSV = content_id(LOG_BYTES), content_id(URDF_BYTES), content_id(CSV_BYTES)
MCAP = transform_record(adapter_id="mcap", adapter_version="1.0.0", config={})
URDF_ADAPTER = transform_record(adapter_id="urdf", adapter_version="1.0.0", config={})
CSV_ADAPTER = transform_record(adapter_id="csv", adapter_version="1.0.0", config={})
TRANSFORMS = {t.id: t for t in (MCAP, URDF_ADAPTER, CSV_ADAPTER)}
SOURCE_OF = {MCAP.id: LOG, URDF_ADAPTER.id: URDF, CSV_ADAPTER.id: CSV}


def cite(transform: TransformRecord, *steps: Locator) -> Provenance:
    """Evidence in the source ``transform`` reads, observed by it."""
    return Provenance(
        EvidenceRef(SOURCE_OF[transform.id], steps), transform.id, AssertionKind.OBSERVED
    )


def record_id_of(kind: str, provenance: Provenance) -> RecordId:
    """The id an evidence record must have: ADR 0003's formula over its record-level evidence."""
    return evidence_record_id(kind, provenance.evidence, TRANSFORMS[provenance.transform])


# The magic bytes establish that this is MCAP, so what the MCAP specification defines (log_time
# is a receive time in nanoseconds) cites them. The two clocks it declares get finer locators.
MAGIC = ByteRange(0, 8)
LOG_TIME_AT = cite(MCAP, MAGIC, adapter_locator("mcap:time_field", {"name": "log_time"}))
PUBLISH_TIME_AT = cite(MCAP, MAGIC, adapter_locator("mcap:time_field", {"name": "publish_time"}))
LOG_TIME = record_id_of("timestamp_domain", LOG_TIME_AT)
TF_AT = cite(MCAP, ByteRange(0, len(LOG_BYTES)))
TF = record_id_of("frame_graph", TF_AT)
URDF_AT = cite(URDF_ADAPTER, ByteRange(0, len(URDF_BYTES)))
URDF_GRAPH = record_id_of("frame_graph", URDF_AT)
M, MM = Known(unit_from_json("m")), Known(unit_from_json("mm"))


def mcap_log_time(**changes: Any) -> TimestampDomain:
    """What an MCAP adapter can honestly say about log_time: ns resolution, epoch unstated."""
    domain = TimestampDomain(
        id=LOG_TIME,
        provenance=LOG_TIME_AT,
        field="log_time",
        scope=(),
        role=Known(ClockRole.RECEIVE),
        resolution=Known(NANOSECOND),
        epoch=Unknown(),
        timescale=Unknown(),
        declared_monotonic=Unknown(),
    )
    return replace(domain, **changes)


def tf_graph() -> FrameGraph:
    return FrameGraph(id=TF, provenance=TF_AT, scope=())


def ref(frame_id: str, graph: RecordId = TF) -> FrameRef:
    return FrameRef(frame_id, graph)


def base_link(**changes: Any) -> Frame:
    at = cite(URDF_ADAPTER, ByteRange(18, 24))
    frame = Frame(
        id=record_id_of("frame", at),
        provenance=at,
        ref=ref("base_link", URDF_GRAPH),
        axes=Unknown(),
        handedness=Unknown(),
    )
    return replace(frame, **changes)


def tf_pose() -> Pose:
    return Pose(
        Translation((0.1, 0.0, -0.0), M),
        Quaternion(
            (0.0, 0.0, 0.0, 1.0), Known(QuaternionOrder.XYZW), Known(QuaternionConvention.HAMILTON)
        ),
    )


def tf_transform(**changes: Any) -> FrameTransform:
    at = cite(MCAP, ByteRange(8, 48), adapter_locator("mcap:transform", {"index": 0}))
    transform = FrameTransform(
        id=record_id_of("frame_transform", at),
        provenance=at,
        parent=ref("odom"),
        child=ref("base_link"),
        direction=Known(TransformDirection.CHILD_TO_PARENT),
        value=tf_pose(),
        validity=Timestamp(1_700_000_000_000_000_000, LOG_TIME),
    )
    return replace(transform, **changes)


def round_trip(record: Any, decode: Any) -> JsonObject:
    data = canonical_json.dumps(record.to_json())
    decoded = decode(canonical_json.loads(data))
    assert decoded == record
    assert canonical_json.dumps(decoded.to_json()) == data
    parsed = canonical_json.loads(data)
    assert isinstance(parsed, dict)
    return parsed


RECORDS: list[tuple[Any, Any]] = [
    (mcap_log_time(), timestamp_domain_from_json),
    (tf_graph(), frame_graph_from_json),
    (base_link(), frame_from_json),
    (tf_transform(), frame_transform_from_json),
]
KINDS = [record.kind for record, _ in RECORDS]


# --- What makes them records -------------------------------------------------------------------


@pytest.mark.parametrize(("record", "decode"), RECORDS, ids=KINDS)
def test_reference_records_round_trip_with_their_envelope(record: Any, decode: Any) -> None:
    data = round_trip(record, decode)
    assert data["kind"] == record.kind
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["provenance"] == record.provenance.to_json()
    assert record.family is Family.REFERENCE


@pytest.mark.parametrize(("record", "decode"), RECORDS, ids=KINDS)
def test_every_id_is_derived_from_the_record_level_evidence(record: Any, decode: Any) -> None:
    transform = TRANSFORMS[record.provenance.transform]
    check_evidence_record_id(record, transform)
    other_kind = replace(
        record, id=evidence_record_id("stream", record.provenance.evidence, transform)
    )
    with pytest.raises(ValueError, match="does not match"):
        check_evidence_record_id(other_kind, transform)
    with pytest.raises(ValueError, match="not produced by"):
        check_evidence_record_id(record, CSV_ADAPTER)


def test_two_clocks_from_one_structure_get_distinct_ids() -> None:
    publish = mcap_log_time(
        id=record_id_of("timestamp_domain", PUBLISH_TIME_AT),
        provenance=PUBLISH_TIME_AT,
        field="publish_time",
        role=Known(ClockRole.PUBLISH),
    )
    assert publish.id != mcap_log_time().id


@pytest.mark.parametrize(
    "provenance",
    [
        InferredProvenance((LOG_TIME_AT.evidence,), MCAP.id),
        Known(1, LOG_TIME_AT),
        LOG_TIME_AT.to_json(),
    ],
)
def test_record_level_provenance_must_be_canonical(provenance: Any) -> None:
    with pytest.raises(TypeError, match="Provenance"):
        FrameGraph(id=TF, provenance=provenance, scope=())


@pytest.mark.parametrize(("record", "decode"), RECORDS, ids=KINDS)
def test_other_schema_versions_are_refused_before_anything_else(record: Any, decode: Any) -> None:
    newer = {**record.to_json(), "schema_version": SCHEMA_VERSION + 1, "field_from_v2": 1}
    with pytest.raises(SchemaVersionError, match="newer"):
        decode(newer)
    for broken in (
        {k: v for k, v in record.to_json().items() if k != "schema_version"},
        {**record.to_json(), "kind": "stream"},
        {**record.to_json(), "confidence": 0.9},
        {k: v for k, v in record.to_json().items() if k != "provenance"},
        {**record.to_json(), "id": LOG},  # a content id is not a record id
    ):
        with pytest.raises(ValueError):
            decode(broken)


# --- TimestampDomain ---------------------------------------------------------------------------


def test_domain_properties_are_evidence_only() -> None:
    domain = mcap_log_time()
    assert domain.epoch == Unknown()  # never assumed to be Unix
    assert domain.timescale == Unknown()  # never assumed to be UTC


def test_naive_text_timestamp_has_no_assumed_zone() -> None:
    # "2026-09-14 10:32" in a notes column: no zone, so tick zero is neither UTC nor host-local
    # midnight 1970 (ADR 0005 §5). The adapter records what it cannot know, plus a finding.
    column = cite(CSV_ADAPTER, RowCell(1, 0, "t [ms]"))
    domain = TimestampDomain(
        id=record_id_of("timestamp_domain", column),
        provenance=column,
        field="t [ms]",
        scope=(),
        role=Known(ClockRole.DOCUMENT),
        resolution=Known(Fraction(60)),
        epoch=Unknown(),
        timescale=Unknown(),
        declared_monotonic=Unknown(),
    )
    assert domain.epoch.state == domain.timescale.state == "unknown"


def test_conflicting_declarations_stay_ambiguous() -> None:
    # The file's comment says µs, its header says ms: both are kept, neither wins.
    comment, header = (
        cite(CSV_ADAPTER, ByteRange(0, 20)),
        cite(CSV_ADAPTER, RowCell(1, 0, "t [ms]")),
    )
    resolution = Ambiguous((Candidate(MICROSECOND, comment), Candidate(Fraction(1, 1000), header)))
    domain = mcap_log_time(resolution=resolution)
    assert [c.value for c in domain.resolution.candidates] == [MICROSECOND, Fraction(1, 1000)]  # type: ignore[union-attr]
    round_trip(domain, timestamp_domain_from_json)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"resolution": Known(1e-9)}, ValueError),
        ({"resolution": Known(1)}, ValueError),
        ({"resolution": Known(Fraction(0))}, ValueError),
        ({"resolution": Known(Fraction(-1, 1000))}, ValueError),
        ({"resolution": Ambiguous((Candidate(NANOSECOND), Candidate(-MICROSECOND)))}, ValueError),
        ({"epoch": Known("unix")}, ValueError),
        ({"timescale": Known(Epoch.GPS)}, ValueError),
        ({"role": Known("receive")}, ValueError),
        ({"declared_monotonic": Known(1)}, ValueError),
        ({"field": ""}, ValueError),
        ({"scope": ("",)}, ValueError),
        ({"scope": ["/imu"]}, TypeError),
        ({"id": "log_time"}, ValueError),
        ({"provenance": None}, TypeError),
    ],
)
def test_domain_rejects_malformed_properties(
    change: dict[str, Any], error: type[Exception]
) -> None:
    with pytest.raises(error):
        mcap_log_time(**change)


def test_domain_json_shape() -> None:
    encoded = canonical_json.dumps(mcap_log_time().to_json())
    assert canonical_json.loads(encoded) == {
        "declared_monotonic": {"knowledge": "unknown"},
        "epoch": {"knowledge": "unknown"},
        "field": "log_time",
        "id": LOG_TIME,
        "kind": "timestamp_domain",
        "provenance": LOG_TIME_AT.to_json(),
        "resolution": {
            "knowledge": "known",
            "value": {"denominator": 1_000_000_000, "numerator": 1},
        },
        "role": {"knowledge": "known", "value": "receive"},
        "schema_version": SCHEMA_VERSION,
        "scope": [],
        "timescale": {"knowledge": "unknown"},
    }
    assert b"null" not in encoded


def mutate(key: str, value: JsonValue) -> JsonValue:
    data = dict(canonical_json.loads(canonical_json.dumps(mcap_log_time().to_json())))  # type: ignore[arg-type]
    data[key] = value
    return data


@pytest.mark.parametrize(
    "data",
    [
        mutate("epoch", {"knowledge": "known", "value": "UNIX"}),
        mutate("epoch", {"knowledge": "known", "value": "local"}),
        mutate("role", "receive"),
        mutate("declared_monotonic", {"knowledge": "known", "value": 1}),
        mutate("resolution", {"knowledge": "known", "provenance": {"where": "spec"}, "value": 1}),
        mutate("scope", "/imu"),
        mutate("scope", [1]),
        mutate("field", 3),
        mutate("confidence", 0.9),
        {k: v for k, v in mcap_log_time().to_json().items() if k != "epoch"},
    ],
)
def test_domain_from_json_is_strict(data: JsonValue) -> None:
    with pytest.raises(ValueError):
        timestamp_domain_from_json(data)


field_provenance = st.sampled_from([LOG_TIME_AT, PUBLISH_TIME_AT])


def states(values: st.SearchStrategy[object]) -> st.SearchStrategy[object]:
    return st.one_of(
        st.builds(Known, values, field_provenance),
        st.builds(KnownAbsent, field_provenance),
        st.builds(Unknown, field_provenance),
        st.just(NotApplicable()),
        st.lists(values, min_size=2, max_size=3, unique=True).map(
            lambda vs: Ambiguous(tuple(Candidate(v) for v in vs))
        ),
    )


resolutions = st.fractions(min_value=Fraction(1, 10**12), max_value=Fraction(3600)).filter(
    lambda f: f > 0
)
text = st.text(st.characters(codec="utf-8"), min_size=1, max_size=8)
domains = st.builds(
    TimestampDomain,
    id=st.just(LOG_TIME),
    provenance=st.just(LOG_TIME_AT),
    field=text,
    scope=st.lists(text, max_size=3).map(tuple),
    role=states(st.sampled_from(ClockRole)),
    resolution=states(resolutions),
    epoch=states(st.sampled_from(Epoch)),
    timescale=states(st.sampled_from(Timescale)),
    declared_monotonic=states(st.booleans()),
)


@given(domains)
def test_domain_round_trip_is_byte_identical(domain: TimestampDomain) -> None:
    round_trip(domain, timestamp_domain_from_json)


# --- FrameGraph ---------------------------------------------------------------------------------


def test_graph_scope_names_the_part_of_the_source() -> None:
    world = cite(URDF_ADAPTER, ByteRange(0, 10))
    one_model = FrameGraph(id=record_id_of("frame_graph", world), provenance=world, scope=("arm",))
    assert round_trip(one_model, frame_graph_from_json)["scope"] == ["arm"]
    assert tf_graph().scope == ()
    for bad, error in ((("",), ValueError), (["arm"], TypeError), (("\ud800",), ValueError)):
        with pytest.raises(error):
            FrameGraph(id=TF, provenance=TF_AT, scope=bad)  # type: ignore[arg-type]


# --- Frame --------------------------------------------------------------------------------------


def test_a_frame_has_no_default_convention() -> None:
    with pytest.raises(TypeError):
        Frame(id=TF, provenance=TF_AT, ref=ref("base_link"))  # type: ignore[call-arg]
    assert base_link().axes == Unknown()


def test_handedness_can_be_declared_without_axes() -> None:
    docs = cite(URDF_ADAPTER, ByteRange(0, 6))
    frame = base_link(handedness=Known(Handedness.LEFT, docs))
    assert frame.handedness == Known(Handedness.LEFT, docs)


def test_contradictory_axes_and_handedness_are_rejected() -> None:
    base_link(axes=Known(AxisConvention.NED), handedness=Known(Handedness.RIGHT))
    with pytest.raises(ValueError, match="right-handed"):
        base_link(axes=Known(AxisConvention.NED), handedness=Known(Handedness.LEFT))


def test_conflicting_declarations_are_ambiguous_not_resolved() -> None:
    axes: Ambiguous[AxisConvention] = Ambiguous(
        (
            Candidate(AxisConvention.FLU, cite(URDF_ADAPTER, ByteRange(0, 6))),
            Candidate(AxisConvention.FRD, cite(URDF_ADAPTER, ByteRange(6, 6))),
        )
    )
    round_trip(base_link(axes=axes), frame_from_json)


def test_frame_rejects_bare_strings_for_enums() -> None:
    with pytest.raises(ValueError, match="AxisConvention"):
        base_link(axes=Known("enu"))


# --- FrameTransform -----------------------------------------------------------------------------


def test_direction_is_kept_as_declared() -> None:
    parent_to_child = Known(TransformDirection.PARENT_TO_CHILD)
    assert tf_transform(direction=parent_to_child).direction == parent_to_child


def test_undeclared_direction_is_ambiguous_with_both_readings() -> None:
    name = cite(URDF_ADAPTER, ByteRange(0, 9))  # a calibration's "T_cam_imu" says neither way
    direction: Ambiguous[TransformDirection] = Ambiguous(
        (
            Candidate(TransformDirection.CHILD_TO_PARENT, name),
            Candidate(TransformDirection.PARENT_TO_CHILD, name),
        )
    )
    round_trip(tf_transform(direction=direction, validity=STATIC), frame_transform_from_json)


def test_stamped_static_and_matrix_transforms_round_trip() -> None:
    matrix = HomogeneousMatrix(
        tuple(float(i == j) for i in range(4) for j in range(4)), Known(MatrixLayout.ROW_MAJOR), MM
    )
    for transform in (
        tf_transform(),
        tf_transform(validity=STATIC),
        tf_transform(value=matrix, validity=Timestamp(-1, LOG_TIME)),
    ):
        round_trip(transform, frame_transform_from_json)


def test_transform_between_graphs_is_alignment_not_a_transform() -> None:
    with pytest.raises(ValueError, match="different frame graphs"):
        tf_transform(parent=ref("odom", URDF_GRAPH))


def test_a_frame_cannot_be_its_own_parent() -> None:
    with pytest.raises(ValueError, match="own parent"):
        tf_transform(parent=ref("base_link"))
    tf_transform(parent=ref("/base_link"))  # verbatim names differ


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("parent", "odom"),
        ("value", tf_pose().rotation),
        ("validity", 1_700_000_000),
        ("direction", Known("child_to_parent")),
    ],
)
def test_transform_rejects_wrong_types(field: str, value: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        tf_transform(**{field: value})


def test_transform_json_shape() -> None:
    data = canonical_json.dumps(tf_transform(validity=STATIC).to_json())
    assert data.startswith(b'{"child":{"frame_graph_id":"rec:sha256:')
    assert b'"direction":{"knowledge":"known","value":"child_to_parent"}' in data
    assert b'"kind":"frame_transform"' in data
    assert b'"validity":{"kind":"static"}' in data
    assert (
        b'"translation":{"unit":{"knowledge":"known","value":"m"},"values":[0.1,0.0,-0.0]}' in data
    )
    assert canonical_json.dumps(tf_transform().to_json()) == canonical_json.dumps(
        tf_transform().to_json()
    )


def _mutate(data: JsonValue, path: tuple[str, ...], change: Any) -> JsonValue:
    obj = dict(data)  # type: ignore[arg-type]
    if len(path) == 1:
        change(obj, path[0])
    else:
        obj[path[0]] = _mutate(obj[path[0]], path[1:], change)
    return obj


def _set(value: Any) -> Any:
    return lambda obj, key: obj.__setitem__(key, value)


@pytest.mark.parametrize(
    ("path", "change"),
    [
        (("extra",), _set(1)),
        (("direction",), lambda obj, key: obj.pop(key)),
        (("value", "kind"), _set("twist")),
        (("value", "rotation", "kind"), _set("axis_angle")),
        (("value", "rotation", "values"), _set([0, 0, 0, 1])),
        (("value", "rotation", "values"), _set([0.0, 0.0, 1.0])),
        (("value", "rotation", "order"), _set({"knowledge": "known", "value": "zyxw"})),
        (("value", "translation", "unit"), _set({"knowledge": "known", "value": "rad"})),
        (("value", "translation", "confidence"), _set(0.9)),
        (("validity", "kind"), _set("interval")),
        (("child", "frame_graph_id"), _set(URDF_GRAPH)),
        (("parent",), _set("odom")),
        (("provenance", "assertion_kind"), _set("inferred")),
    ],
)
def test_json_parsing_is_strict(path: tuple[str, ...], change: Any) -> None:
    data = _mutate(tf_transform(validity=STATIC).to_json(), path, change)
    with pytest.raises((TypeError, ValueError)):
        frame_transform_from_json(data)

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.derived.provenance import InferredProvenance
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.ids import record_id
from neptune.model.frames import FrameRef
from neptune.model.ids import ExternalObjectRef, RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Knowledge,
    Known,
    KnownAbsent,
    Unknown,
    from_json,
    to_json,
)
from neptune.model.provenance import (
    NO_HEADER,
    AdapterLocator,
    ByteRange,
    EvidenceRef,
    FrameLocator,
    ImageRegion,
    JsonPointer,
    Locator,
    ObjectLocator,
    Page,
    PageRegion,
    Provenance,
    RecordRange,
    Row,
    RowCell,
    Span,
    TransformRecord,
    VideoFrame,
    adapter_locator,
    evidence_ref_from_json,
    locator_from_json,
    provenance_from_json,
    transform_record_from_json,
)
from neptune.model.record import SCHEMA_VERSION
from neptune.model.time import INT64_MAX, Timestamp

SOURCE = content_id(b"a small source")
TRANSFORM = record_id("transform_record", {"adapter_id": "csv"})
LOG_TIME = record_id("timestamp_domain", {"source": "a.mcap", "field": "log_time"})
PTS = record_id("timestamp_domain", {"source": "a.mp4", "track": 0})
GRAPH = record_id("frame_graph", {"source": "robot.urdf"})
CONFIG_HASH = "sha256:" + "ab" * 32


def evidence(*steps: Locator) -> EvidenceRef:
    return EvidenceRef(SOURCE, steps or (ByteRange(0, 14),))


def provenance(*steps: Locator, kind: AssertionKind = AssertionKind.OBSERVED) -> Provenance:
    return Provenance(evidence(*steps), TRANSFORM, kind)


EVERY_STEP: list[Locator] = [
    ByteRange(0, 0),
    ByteRange(1024, 512),
    RecordRange("/imu", Timestamp(100, LOG_TIME), Timestamp(101, LOG_TIME)),
    RecordRange("", Timestamp(5, LOG_TIME), Timestamp(5, LOG_TIME)),
    Page(0),
    PageRegion(2, 72.0, 90.5, 540.0, 720.0),
    Span(10, 40),
    Row(0),
    RowCell(3, 1, "max_velocity"),
    RowCell(3, 1, ""),
    RowCell(3, 1, NO_HEADER),
    ImageRegion(0, 0, 640, 480),
    VideoFrame(0, 120, Timestamp(4004, PTS)),
    JsonPointer(""),
    JsonPointer("/joints/0/limit~1max/a~0b"),
    FrameLocator(FrameRef("base_link", GRAPH)),
    ObjectLocator("Wall-3"),
    adapter_locator("mcap:message", {"chunk": 3, "index": 7}),
    adapter_locator("xlsx:sheet", {"name": "Defects", "position": 1, "hidden": False}),
]


# --- Locator steps ----------------------------------------------------------------------------


@pytest.mark.parametrize("step", EVERY_STEP, ids=lambda s: s.kind)
def test_every_step_round_trips_through_canonical_json(step: Locator) -> None:
    data = canonical_json.loads(canonical_json.dumps(step.to_json()))
    assert locator_from_json(data) == step


def test_json_shapes_are_flat_and_tagged() -> None:
    assert RecordRange("/imu", Timestamp(1, LOG_TIME), Timestamp(9, LOG_TIME)).to_json() == {
        "channel": "/imu",
        "domain_id": LOG_TIME,
        "end": 9,
        "kind": "record_range",
        "start": 1,
    }
    assert adapter_locator("mcap:message", {"index": 7, "chunk": 3}).to_json() == {
        "chunk": 3,
        "index": 7,
        "kind": "mcap:message",
    }


def test_row_cell_without_header_omits_the_name_and_differs_from_a_blank_header() -> None:
    assert RowCell(1, 0, NO_HEADER).to_json() == {"column": 0, "kind": "row_cell", "row": 1}
    assert RowCell(1, 0, "") != RowCell(1, 0, NO_HEADER)


@pytest.mark.parametrize(
    "build",
    [
        lambda: ByteRange(-1, 4),
        lambda: ByteRange(0, -1),
        lambda: ByteRange(INT64_MAX, 1),
        lambda: Span(5, 4),
        lambda: Page(-1),
        lambda: Row(-1),
        lambda: ImageRegion(10, 0, 9, 5),
        lambda: ImageRegion(0, 5, 10, 4),
        lambda: PageRegion(0, 1.0, 0.0, 0.5, 1.0),
        lambda: PageRegion(0, float("nan"), 0.0, 1.0, 1.0),
        lambda: RecordRange("/imu", Timestamp(9, LOG_TIME), Timestamp(8, LOG_TIME)),
        lambda: RecordRange("/imu", Timestamp(1, LOG_TIME), Timestamp(2, PTS)),
        lambda: JsonPointer("joints/0"),
        lambda: JsonPointer("/a~2"),
        lambda: JsonPointer("/a~"),
        lambda: RowCell(0, 0, "\ud800"),
        lambda: adapter_locator("message", {"index": 1}),
        lambda: adapter_locator("MCAP:message", {"index": 1}),
        lambda: adapter_locator("mcap:message", {"kind": 1}),
        lambda: adapter_locator("mcap:message", {"Index": 1}),
        lambda: adapter_locator("mcap:message", {"x": float("inf")}),
        lambda: AdapterLocator("mcap:message", (("b", 1), ("a", 2))),
    ],
)
def test_malformed_steps_are_rejected(build: Any) -> None:
    with pytest.raises(ValueError):
        build()


@pytest.mark.parametrize(
    "build",
    [
        lambda: ByteRange(True, 1),
        lambda: Span(0, 1.0),  # type: ignore[arg-type]
        lambda: PageRegion(0, 0, 0.0, 1.0, 1.0),
        lambda: RowCell(0, 0, None),  # type: ignore[arg-type]
        lambda: FrameLocator("base_link"),  # type: ignore[arg-type]
        lambda: adapter_locator("mcap:message", {"index": [1]}),  # type: ignore[dict-item]
        lambda: VideoFrame(0, 1, 4004),  # type: ignore[arg-type]
    ],
)
def test_wrongly_typed_steps_are_rejected(build: Any) -> None:
    with pytest.raises(TypeError):
        build()


def test_boundaries_are_inclusive_of_empty_ranges_and_the_int64_limit() -> None:
    assert ByteRange(INT64_MAX, 0).offset == INT64_MAX
    assert ByteRange(0, INT64_MAX).length == INT64_MAX
    assert Span(7, 7).start == 7
    assert ImageRegion(3, 3, 3, 3).x1 == 3


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "byte_range", "offset": 0},
        {"kind": "byte_range", "offset": 0, "length": 1, "extra": 1},
        {"kind": "byte_range", "offset": 0, "length": 1.0},
        {"kind": "byte_range", "offset": False, "length": 1},
        {"kind": "page_region", "page": 0, "x0": 0, "y0": 0.0, "x1": 1.0, "y1": 1.0},
        {"kind": "row_cell", "row": 0, "column": 0, "column_name": 3},
        {"kind": "row_cell", "row": 0},
        {"kind": "bytes", "offset": 0, "length": 1},
        {"kind": "mcap:message", "index": [1]},
        {"offset": 0, "length": 1},
        [0, 1],
    ],
)
def test_locator_json_is_parsed_strictly(data: JsonValue) -> None:
    with pytest.raises(ValueError):
        locator_from_json(data)


@given(offset=st.integers(0, INT64_MAX), length=st.integers(0, INT64_MAX))
def test_byte_ranges_round_trip_or_are_rejected(offset: int, length: int) -> None:
    if offset + length > INT64_MAX:
        with pytest.raises(ValueError):
            ByteRange(offset, length)
        return
    step = ByteRange(offset, length)
    assert locator_from_json(canonical_json.loads(canonical_json.dumps(step.to_json()))) == step


# --- Evidence refs ----------------------------------------------------------------------------


def test_evidence_ref_is_a_non_empty_path_outermost_first() -> None:
    nested = EvidenceRef(SOURCE, (ByteRange(0, 90), JsonPointer("/limits/0")))
    assert nested.to_json() == {
        "locator": [
            {"kind": "byte_range", "length": 90, "offset": 0},
            {"kind": "json_pointer", "pointer": "/limits/0"},
        ],
        "source": SOURCE,
    }
    assert evidence_ref_from_json(canonical_json.loads(canonical_json.dumps(nested.to_json()))) == (
        nested
    )
    with pytest.raises(ValueError, match="non-empty"):
        EvidenceRef(SOURCE, ())
    with pytest.raises(ValueError, match="non-empty"):
        EvidenceRef(SOURCE, [ByteRange(0, 1)])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        EvidenceRef(SOURCE, ({"kind": "byte_range"},))  # type: ignore[arg-type]


def test_evidence_source_is_a_content_id_or_an_unfetched_external_object() -> None:
    external = ExternalObjectRef("s3", "bucket/run.mcap", "etag-1")
    ref = EvidenceRef(external, (ByteRange(0, 8),))
    assert evidence_ref_from_json(canonical_json.loads(canonical_json.dumps(ref.to_json()))) == ref
    with pytest.raises(ValueError):
        EvidenceRef("sha256:ABC", (ByteRange(0, 8),))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        EvidenceRef("robot.urdf", (ByteRange(0, 8),))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        evidence_ref_from_json({"locator": [], "source": {"kind": "local", "path": "a"}})


# --- Provenance -------------------------------------------------------------------------------


def test_provenance_json_shape() -> None:
    assert provenance(ByteRange(0, 4)).to_json() == {
        "assertion_kind": "observed",
        "evidence": {
            "locator": [{"kind": "byte_range", "length": 4, "offset": 0}],
            "source": SOURCE,
        },
        "transform": TRANSFORM,
    }


@pytest.mark.parametrize("kind", list(AssertionKind))
def test_provenance_round_trips(kind: AssertionKind) -> None:
    original = provenance(RowCell(4, 2, "defects"), kind=kind)
    assert provenance_from_json(canonical_json.loads(canonical_json.dumps(original.to_json()))) == (
        original
    )


def test_inferred_is_unrepresentable_in_model_provenance() -> None:
    assert "inferred" not in {str(kind) for kind in AssertionKind}
    with pytest.raises(TypeError, match="derived"):
        Provenance(evidence(), TRANSFORM, "inferred")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Provenance(evidence(), TRANSFORM, "observed")  # type: ignore[arg-type]
    data = dict(provenance().to_json())
    data["assertion_kind"] = "inferred"
    with pytest.raises(ValueError, match="observed or stated"):
        provenance_from_json(data)


def test_knowledge_states_carry_provenance_and_round_trip() -> None:
    cell = provenance(RowCell(2, 1, "max_velocity"))
    legend = provenance(Page(0), kind=AssertionKind.STATED)
    states: list[Knowledge[Any]] = [
        Known(90, cell),
        KnownAbsent(legend),
        Unknown(cell),
        Ambiguous((Candidate("a", cell), Candidate("b", legend))),
    ]
    for state in states:
        data = canonical_json.loads(canonical_json.dumps(to_json(state)))
        assert from_json(data, lambda v: v, provenance_from_json) == state


def test_knowledge_rejects_inferred_provenance() -> None:
    inferred = InferredProvenance((evidence(),), TRANSFORM)
    # The ignores are load-bearing: mypy --strict warns on unused ignores, so these lines also
    # prove the type checker rejects inferred provenance on canonical states.
    with pytest.raises(TypeError, match="derived"):
        Known(1, inferred)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="derived"):
        KnownAbsent(inferred)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="derived"):
        Unknown(inferred)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="derived"):
        Candidate(1, inferred)  # type: ignore[arg-type]


# --- Transform records ------------------------------------------------------------------------

TRANSFORM_JSON: dict[str, JsonValue] = {
    "adapter_id": "csv",
    "adapter_version": "1.0.0",
    "config": {"delimiter": ","},
    "config_hash": CONFIG_HASH,
    "id": TRANSFORM,
    "kind": "transform_record",
    "libraries": {"neptune.units-catalogue": "1"},
    "schema_version": SCHEMA_VERSION,
    "upstream": [],
}


def test_transform_record_json_round_trips_and_hashes_by_id() -> None:
    record = transform_record_from_json(TRANSFORM_JSON)
    assert record.to_json() == TRANSFORM_JSON
    assert record.libraries == (("neptune.units-catalogue", "1"),)
    assert hash(record) == hash(TRANSFORM)
    # The id covers the content only: neither the id itself nor the envelope.
    assert {"id", "kind", "schema_version"}.isdisjoint(record.content_json())


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("adapter_id", "CSV"),
        ("adapter_version", ""),
        ("config_hash", "sha256:xyz"),
        ("config", [1]),
        ("libraries", ["mcap"]),
        ("upstream", [TRANSFORM]),
        ("upstream", ["sha256:" + "0" * 64]),
        ("hostname", "ci-runner-7"),
        ("kind", "transform"),
        ("schema_version", SCHEMA_VERSION + 1),
    ],
)
def test_malformed_transform_records_are_rejected(key: str, value: JsonValue) -> None:
    with pytest.raises(ValueError):
        transform_record_from_json({**TRANSFORM_JSON, key: value})


def test_transform_record_field_rules() -> None:
    fields: dict[str, Any] = {
        "id": TRANSFORM,
        "adapter_id": "csv",
        "adapter_version": "1.0.0",
        "config_hash": CONFIG_HASH,
        "config": {},
        "libraries": (),
        "upstream": (),
    }
    other = RecordId(record_id("transform_record", {"adapter_id": "tsv"}))
    with pytest.raises(ValueError, match="sorted"):
        TransformRecord(**{**fields, "libraries": (("b", "1"), ("a", "1"))})
    with pytest.raises(ValueError, match="sorted"):
        TransformRecord(**{**fields, "libraries": (("a", "1"), ("a", "2"))})
    with pytest.raises(ValueError, match="repeat"):
        TransformRecord(**{**fields, "upstream": (other, other)})
    with pytest.raises(ValueError, match="own output"):
        TransformRecord(**{**fields, "upstream": (TRANSFORM,)})

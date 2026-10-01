"""The envelope every record kind shares (ADR 0017), checked across all kinds at once.

MVL-66 acceptance: every record kind round-trips byte-identically through canonical JSON, and a
record written by another schema version is refused with a version error, never a key error.
"""

from collections.abc import Callable
from typing import Any, TypeVar

import pytest

from neptune.identity import canonical_json
from neptune.identity.findings import ingest_finding
from neptune.identity.hashing import content_id
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.identity.revisions import SourceLedger
from neptune.model import record
from neptune.model.finding import FindingCategory, IngestFinding, Severity, ingest_finding_from_json
from neptune.model.frames import (
    STATIC,
    FrameRef,
    Pose,
    Quaternion,
    TransformDirection,
    Translation,
)
from neptune.model.jsonvalue import JsonValue
from neptune.model.kinds import KIND_SINCE, RECORD_KINDS
from neptune.model.knowledge import AssertionKind, Known, Unknown
from neptune.model.provenance import (
    ByteRange,
    EvidenceRef,
    Provenance,
    TransformRecord,
    transform_record_from_json,
)
from neptune.model.record import (
    ENVELOPE_KEYS,
    OLDEST_READABLE_VERSION,
    SCHEMA_VERSION,
    Family,
    SchemaVersionError,
    envelope,
    record_object,
)
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
from neptune.model.run import Run, Stream, run_from_json, stream_from_json
from neptune.model.series import SeriesProvenance, step_template
from neptune.model.source import (
    LocalPath,
    SourceAbsence,
    SourceArtifact,
    SourceRevision,
    source_absence_from_json,
    source_artifact_from_json,
    source_revision_from_json,
)
from neptune.model.time import NANOSECOND, ClockRole, Timestamp
from neptune.model.units import unit_from_json

URDF_BYTES = b"<robot name='arm'><link name='base_link'/><link name='tool0'/></robot>"
URDF = content_id(URDF_BYTES)
ADAPTER = transform_record(adapter_id="urdf", adapter_version="1.0.0", config={})


def artifact(data: bytes) -> SourceArtifact:
    digest = content_id(data)
    return SourceArtifact(digest, len(data), 8 * 2**20, (digest,) if data else ())


def ledger() -> tuple[SourceRevision, SourceAbsence]:
    book = SourceLedger()
    book.observe(LocalPath("robot.urdf"), artifact(URDF_BYTES))
    absence = book.mark_absent(LocalPath("robot.urdf"))
    assert absence is not None
    return book.revisions()[0], absence


def at(offset: int, length: int) -> Provenance:
    return Provenance(
        EvidenceRef(URDF, (ByteRange(offset, length),)), ADAPTER.id, AssertionKind.OBSERVED
    )


R = TypeVar("R")


def evidence_record(build: Callable[..., R], kind: str, where: Provenance, **fields: Any) -> R:
    """A record whose id follows the rule: derived from its record-level evidence (ADR 0017)."""
    return build(id=evidence_record_id(kind, where.evidence, ADAPTER), provenance=where, **fields)


GRAPH = evidence_record(FrameGraph, "frame_graph", at(0, len(URDF_BYTES)), scope=())
STAMP = evidence_record(
    TimestampDomain,
    "timestamp_domain",
    at(0, 6),
    field="stamp",
    scope=(),
    role=Known(ClockRole.SAMPLE),
    resolution=Known(NANOSECOND),
    epoch=Unknown(),
    timescale=Unknown(),
    declared_monotonic=Unknown(),
)
RUN = evidence_record(
    Run,
    "run",
    at(0, len(URDF_BYTES)),
    logical_id=Unknown(),
    machine=Unknown(),
    first=Known(Timestamp(0, STAMP.id)),
    last=Unknown(),
)
M = Known(unit_from_json("m"))
POSE = Pose(Translation((0.0, 0.0, 0.1), M), Quaternion((0.0, 0.0, 0.0, 1.0), Unknown(), Unknown()))
REVISION, ABSENCE = ledger()


def samples() -> list[tuple[Any, Callable[[JsonValue], Any]]]:
    """One record of every kind that exists so far, with its strict reader."""
    return [
        (artifact(URDF_BYTES), source_artifact_from_json),
        (REVISION, source_revision_from_json),
        (ABSENCE, source_absence_from_json),
        (ADAPTER, transform_record_from_json),
        (STAMP, timestamp_domain_from_json),
        (GRAPH, frame_graph_from_json),
        (
            evidence_record(
                Frame,
                "frame",
                at(18, 24),
                ref=FrameRef("base_link", GRAPH.id),
                axes=Unknown(),
                handedness=Unknown(),
            ),
            frame_from_json,
        ),
        (
            evidence_record(
                FrameTransform,
                "frame_transform",
                at(42, 20),
                parent=FrameRef("base_link", GRAPH.id),
                child=FrameRef("tool0", GRAPH.id),
                direction=Known(TransformDirection.CHILD_TO_PARENT),
                value=POSE,
                validity=STATIC,
            ),
            frame_transform_from_json,
        ),
        (RUN, run_from_json),
        (
            evidence_record(
                Stream,
                "stream",
                at(6, 12),
                run=RUN.id,
                topic=Known("/joint_states"),
                schema_name=Unknown(),
                schema_encoding=Unknown(),
                schema_definition=Unknown(),
                message_encoding=Unknown(),
                metadata=(),
                clocks=(STAMP.id,),
                message_count=Unknown(),
                first=Unknown(),
                last=Unknown(),
                series=SeriesProvenance(
                    URDF, (step_template("row", per_row=["row"]),), AssertionKind.OBSERVED
                ),
            ),
            stream_from_json,
        ),
        (
            ingest_finding(
                code="urdf.mesh_missing",
                category=FindingCategory.MISSING,
                severity=Severity.WARNING,
                subject=EvidenceRef(URDF, (ByteRange(42, 20),)),
                transform=ADAPTER,
                message="link tool0 names no mesh",
            ),
            ingest_finding_from_json,
        ),
    ]


SAMPLES = samples()
KINDS = [type(sample) for sample, _ in SAMPLES]
IDS = [cls.kind for cls in KINDS]


@pytest.mark.parametrize(("sample", "decode"), SAMPLES, ids=IDS)
def test_every_record_kind_round_trips_byte_identically(sample: Any, decode: Any) -> None:
    line = canonical_json.dumps(sample.to_json())
    decoded = decode(canonical_json.loads(line))
    assert decoded == sample
    assert canonical_json.dumps(decoded.to_json()) == line
    data = canonical_json.loads(line)
    assert isinstance(data, dict)
    assert data["kind"] == sample.kind
    assert data["schema_version"] == KIND_SINCE[sample.kind]


@pytest.mark.parametrize(("sample", "decode"), SAMPLES, ids=IDS)
def test_a_newer_record_is_refused_with_a_version_error(sample: Any, decode: Any) -> None:
    # A v1 record may have keys this reader has never seen; the version is what it must report.
    newer = {**sample.to_json(), "schema_version": SCHEMA_VERSION + 1, "added_in_v1": True}
    with pytest.raises(SchemaVersionError, match="newer than this reader"):
        decode(newer)


@pytest.mark.parametrize(("sample", "decode"), SAMPLES, ids=IDS)
def test_an_older_record_reads_as_it_is(
    sample: Any, decode: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model only grows (ADR 0023): a newer reader reads an older record unchanged.
    data = sample.to_json()
    monkeypatch.setattr(record, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    assert decode(data) == sample


@pytest.mark.parametrize(("sample", "decode"), SAMPLES, ids=IDS)
def test_a_record_from_before_the_gate_is_refused(sample: Any, decode: Any) -> None:
    draft = {**sample.to_json(), "schema_version": 0}
    with pytest.raises(SchemaVersionError, match="predates the M1 gate"):
        decode(draft)


@pytest.mark.parametrize(("sample", "decode"), SAMPLES, ids=IDS)
def test_the_envelope_is_checked_strictly(sample: Any, decode: Any) -> None:
    data = sample.to_json()
    for broken in (
        {k: v for k, v in data.items() if k != "schema_version"},
        {k: v for k, v in data.items() if k != "kind"},
        {**data, "schema_version": "0"},
        {**data, "schema_version": True},
        {**data, "schema_version": -1},
        {**data, "kind": next(kind for kind in IDS if kind != sample.kind)},
        {**data, "kind": sample.kind.upper()},
        {**data, "confidence": 0.9},
    ):
        with pytest.raises(ValueError):
            decode(broken)


def test_kinds_are_unique_tokens_and_each_has_one_family() -> None:
    classes: list[Any] = [cls for cls, _ in RECORD_KINDS.values()]
    kinds = [cls.kind for cls in classes]
    assert len(set(kinds)) == len(kinds) and set(KINDS) <= set(classes)
    for cls in classes:
        assert cls.kind == cls.kind.lower() and " " not in cls.kind
        assert isinstance(cls.family, Family)
    families = {cls.kind: str(cls.family) for cls in classes}
    assert families == {
        "source_artifact": "source",
        "source_revision": "source",
        "source_absence": "source",
        "transform_record": "lineage",
        "ingest_finding": "finding",
        "timestamp_domain": "reference",
        "frame_graph": "reference",
        "frame": "reference",
        "frame_transform": "reference",
        "run": "run",
        "stream": "run",
        "machine": "machine",
        "hardware_configuration": "machine",
        "hardware_component": "machine",
        "software_configuration": "machine",
        "calibration": "machine",
        "configuration_snapshot": "machine",
        "configuration_value": "machine",
        "hardware_specification": "machine",
        "description_extension": "machine",
        "description_expansion": "machine",
        "site": "world",
        "asset": "world",
        "spatial_artifact": "world",
        "image": "world",
        "video": "world",
        "document_record": "world",
        "document_block": "world",
        "structured_table": "world",
        "structured_record": "world",
    }


def test_the_four_design_contract_domains_are_families() -> None:
    assert {"machine", "world", "task", "run"} <= {str(family) for family in Family}


def test_evidence_records_carry_provenance_and_ledger_records_do_not() -> None:
    evidence = {TimestampDomain, FrameGraph, Frame, FrameTransform, Run, Stream}
    for sample, _ in SAMPLES:
        assert ("provenance" in sample.to_json()) is (type(sample) in evidence)
    ledger_kinds = {SourceArtifact, SourceRevision, SourceAbsence, TransformRecord, IngestFinding}
    assert set(KINDS) == evidence | ledger_kinds


def test_envelope_keys_cannot_be_record_fields() -> None:
    assert envelope("frame", {"ref": 1}) == {
        "kind": "frame",
        "ref": 1,
        "schema_version": OLDEST_READABLE_VERSION,
    }
    for key in ENVELOPE_KEYS:
        with pytest.raises(ValueError, match="envelope"):
            envelope("frame", {key: 1})


def test_record_object_reports_the_version_before_the_keys() -> None:
    frame: dict[str, JsonValue] = {
        "kind": "frame",
        "schema_version": OLDEST_READABLE_VERSION,
        "ref": 1,
    }
    assert record_object(frame, "frame", {"ref"})
    with pytest.raises(SchemaVersionError):
        record_object({"kind": "frame_v2", "schema_version": 5, "other": 1}, "frame", {"ref"})
    with pytest.raises(ValueError, match="kind"):
        record_object({**frame, "kind": "frames"}, "frame", {"ref"})
    with pytest.raises(ValueError, match="JSON object"):
        record_object([{"kind": "frame"}], "frame", {"ref"})

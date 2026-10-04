"""The generated JSON Schema: no drift, valid, complete, and faithful to what ``to_json`` writes.

The worked examples (tests/integration/test_worked_examples.py) validate every golden line. This
file adds the values the examples do not use: every locator step, rotation, version kind, finding
subject and knowledge state, and the lines the schema must refuse.
"""

import importlib
import inspect
import json
import pkgutil
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

import neptune.model
from neptune.identity import canonical_json
from neptune.identity.findings import ingest_finding
from neptune.identity.hashing import content_id
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.finding import FindingCategory, Severity
from neptune.model.frames import (
    FrameRef,
    HomogeneousMatrix,
    MatrixLayout,
    Pose,
    RotationMatrix,
    RotationVector,
    TransformDirection,
    Translation,
)
from neptune.model.ids import ExternalObjectRef, LogicalId, RecordId
from neptune.model.kinds import KIND_SINCE
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.lists import LIST_STATES_SINCE
from neptune.model.machine import (
    Calibration,
    CalibrationParameter,
    Machine,
    SoftwareConfiguration,
    SoftwareItem,
)
from neptune.model.provenance import (
    NO_HEADER,
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
    VideoFrame,
    adapter_locator,
)
from neptune.model.record import SCHEMA_VERSION
from neptune.model.reference import FrameTransform
from neptune.model.scalars import NonFinite
from neptune.model.schema import RECORD_KINDS, SCHEMA_ID, canonical_schema, render
from neptune.model.source import LocalPath, RawLocalPath
from neptune.model.time import Timestamp
from neptune.model.units import unit_from_json
from neptune.model.versions import (
    BuildId,
    ContainerImageDigest,
    DeclaredVersion,
    FirmwareVersion,
    GitCommit,
    HashAlgorithm,
    ModelCheckpointHash,
    SemanticVersion,
)

COMMITTED = Path(__file__).parents[3] / "docs" / "schema" / "canonical.schema.json"
SCHEMA = canonical_schema()
VALIDATOR = Draft202012Validator(SCHEMA)
SOURCE = content_id(b"a source")
ADAPTER = transform_record(adapter_id="zoo", adapter_version="1.0.0", config={"limit": 3})
CLOCK = RecordId("rec:sha256:" + "1" * 64)
GRAPH = RecordId("rec:sha256:" + "2" * 64)


def cite(*steps: Locator, source: Any = SOURCE) -> Provenance:
    return Provenance(EvidenceRef(source, steps), ADAPTER.id, AssertionKind.OBSERVED)


def record_id(kind: str, provenance: Provenance) -> RecordId:
    return evidence_record_id(kind, provenance.evidence, ADAPTER)


def valid(record: Any) -> list[str]:
    data = canonical_json.loads(canonical_json.dumps(record.to_json()))
    return [error.message for error in VALIDATOR.iter_errors(data)]


# --- The file ----------------------------------------------------------------------------------


def test_the_committed_schema_is_what_the_model_generates() -> None:
    # On failure, run `make schema` and commit the result with the change that caused it.
    assert COMMITTED.read_text(encoding="utf-8") == render()


def test_it_is_a_valid_draft_2020_12_schema_for_this_schema_version() -> None:
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$id"] == SCHEMA_ID
    assert SCHEMA_ID.endswith(f":{SCHEMA_VERSION}")


def test_every_record_kind_in_the_model_is_in_the_schema() -> None:
    kinds = set()
    for module in pkgutil.iter_modules(neptune.model.__path__):
        loaded = importlib.import_module(f"neptune.model.{module.name}")
        for _, cls in inspect.getmembers(loaded, inspect.isclass):
            if cls.__module__ == loaded.__name__ and hasattr(cls, "family"):
                kinds.add(cls)
    assert kinds == set(RECORD_KINDS)
    defs = SCHEMA["$defs"]
    assert isinstance(defs, dict)
    for kind in RECORD_KINDS:
        properties = defs[kind.__name__]["properties"]
        assert properties["kind"] == {"const": getattr(kind, "kind")}  # noqa: B009
        since = KIND_SINCE[kind.kind]  # type: ignore[attr-defined]
        if isinstance(getattr(kind, "schema_version", None), property):
            # A kind whose lists may hold states is written at their version too (ADR 0061 §6).
            assert properties["schema_version"] == {"enum": [since, LIST_STATES_SINCE]}
        else:
            assert properties["schema_version"] == {"const": since}


# --- Values the examples do not use ------------------------------------------------------------

LOCATORS: list[Locator] = [
    ByteRange(0, 8),
    RecordRange("/imu", Timestamp(1, CLOCK), Timestamp(2, CLOCK)),
    Page(3),
    PageRegion(3, 72.0, 700.0, 300.0, 716.0),
    Span(10, 40),
    Row(4),
    RowCell(4, 2, "serial"),
    RowCell(4, 2, NO_HEADER),
    ImageRegion(0, 0, 64, 48),
    VideoFrame(0, 42, Timestamp(21_504, CLOCK)),
    JsonPointer("/robots/0"),
    FrameLocator(FrameRef("base_link", GRAPH)),
    ObjectLocator("valve_V12"),
    adapter_locator("mcap:message", {"channel": 1, "sequence": 7, "topic": "/imu"}),
]


def test_every_locator_step_and_knowledge_state_validates() -> None:
    external = ExternalObjectRef("s3", "bucket/key.mcap", "etag-1")
    at = cite(ByteRange(0, 8))
    ids = tuple(Known(LogicalId("zoo", f"{i:02d}"), cite(step)) for i, step in enumerate(LOCATORS))
    machine = Machine(
        id=record_id("machine", at),
        provenance=at,
        identifiers=(
            *ids,
            Ambiguous(
                (
                    Candidate(LogicalId("zoo.serial", "A")),
                    Candidate(LogicalId("zoo.serial", "B"), cite(ByteRange(0, 1), source=external)),
                )
            ),
        ),
        manufacturer=KnownAbsent(cite(Span(0, 4))),
        model=Unknown(cite(Page(0))),
    )
    assert valid(machine) == []


def test_every_version_kind_and_non_finite_parameter_validates() -> None:
    at = cite(ByteRange(0, 8))
    items = (
        SoftwareItem(
            name=Known("policy"),
            device=NotApplicable(),
            commit=Known(GitCommit("9fceb02")),
            release=Known(SemanticVersion("2.4.1-rc.1+b5")),
            build=Known(BuildId("build-4812")),
            digest=Known(ModelCheckpointHash(HashAlgorithm.SHA256, "ab" * 32)),
        ),
        SoftwareItem(
            name=Known("image"),
            device=Unknown(),
            commit=NotCovered(),
            release=Known(FirmwareVersion("0x010E")),
            build=Unknown(),
            digest=Known(ContainerImageDigest("sha256:" + "cd" * 32)),
        ),
        SoftwareItem(
            name=Ambiguous((Candidate("nav"), Candidate("nav2"))),
            device=Unknown(),
            commit=Unknown(),
            release=Known(DeclaredVersion("humble")),
            build=Unknown(),
            digest=Unknown(),
        ),
    )
    software = SoftwareConfiguration(
        id=record_id("software_configuration", at),
        provenance=at,
        machine=Unknown(),
        software=items,
    )
    calibration = Calibration(
        id=record_id("calibration", at),
        provenance=at,
        machine=Unknown(),
        hardware_revision=Unknown(),
        subject=Known("cam0"),
        performed=Known(Timestamp(20_359, CLOCK)),
        valid_from=Unknown(),
        valid_until=Unknown(),
        parameters=(
            CalibrationParameter("limits", Known((1.0, NonFinite.POSITIVE_INFINITY)), Unknown()),
            CalibrationParameter("unset", Known((NonFinite.NAN,)), Known(unit_from_json("m.s^-2"))),
        ),
        extrinsics=(),
    )
    assert valid(software) == []
    assert valid(calibration) == []


def test_every_rotation_and_validity_validates() -> None:
    matrix = HomogeneousMatrix(
        tuple(float(i == j) for i in range(4) for j in range(4)),
        layout=Known(MatrixLayout.ROW_MAJOR),
        translation_unit=Known(unit_from_json("mm")),
    )
    rotations = (
        RotationMatrix(tuple(float(i == j) for i in range(3) for j in range(3)), Unknown()),
        RotationVector((0.0, 0.0, 0.1), Known(unit_from_json("rad"))),
    )
    values: list[Pose | HomogeneousMatrix] = [
        matrix,
        *(Pose(Translation((0.0, 0.0, 1.0), Unknown()), rotation) for rotation in rotations),
    ]
    for i, value in enumerate(values):
        step = cite(ByteRange(i, 1))
        transform = FrameTransform(
            id=record_id("frame_transform", step),
            provenance=step,
            parent=FrameRef("map", GRAPH),
            child=FrameRef("base_link", GRAPH),
            direction=Known(TransformDirection.PARENT_TO_CHILD),
            value=value,
            validity=Timestamp(5, CLOCK),
        )
        assert valid(transform) == []


def test_every_finding_subject_validates() -> None:
    subjects: list[Any] = [
        LocalPath("logs/unreadable"),
        RawLocalPath(b"caf\xe9.bag"),
        ExternalObjectRef("s3", "bucket/key.mcap", "etag-1"),
        EvidenceRef(SOURCE, (ByteRange(0, 8),)),
    ]
    for subject in subjects:
        finding = ingest_finding(
            code="zoo.skipped",
            category=FindingCategory.SKIPPED,
            severity=Severity.INFO,
            subject=subject,
            transform=ADAPTER,
            message="not read",
            details={"reason": "zoo", "limits": [1, 2.5, "x"]},
        )
        assert valid(finding) == []
    assert valid(ADAPTER) == []


# --- Lines the schema refuses ------------------------------------------------------------------


def machine_json() -> dict[str, Any]:
    at = cite(ByteRange(0, 8))
    machine = Machine(
        id=record_id("machine", at),
        provenance=at,
        identifiers=(Known(LogicalId("serial", "A-1")),),
        manufacturer=Unknown(),
        model=NotCovered(),
    )
    data = json.loads(canonical_json.dumps(machine.to_json()))
    assert isinstance(data, dict)
    return data


@pytest.mark.parametrize(
    "change",
    [
        {"confidence": 0.9},  # no key beyond the record's own
        {"kind": "asset"},  # a machine line is a machine
        {"schema_version": SCHEMA_VERSION + 1},
        {"id": "rec:sha256:" + "A" * 64},  # ids are lowercase hex
        {"model": {"knowledge": "known"}},  # a known state holds its value
        {"model": {"knowledge": "ambiguous", "candidates": [{"value": "x"}]}},  # two or more
        {"model": {"knowledge": "maybe"}},
        {"model": "Spot"},  # a bare value is never a state
        {"identifiers": [{"knowledge": "known", "value": {"namespace": "serial"}}]},
    ],
)
def test_the_schema_refuses_what_readers_refuse(change: dict[str, Any]) -> None:
    assert not VALIDATOR.is_valid({**machine_json(), **change})


def test_the_schema_refuses_missing_keys() -> None:
    data = machine_json()
    for key in data:
        assert not VALIDATOR.is_valid({k: v for k, v in data.items() if k != key}), key

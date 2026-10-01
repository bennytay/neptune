"""The robot-description kinds of schema version 2 (ADR 0039): shape, rules, JSON and schema."""

from dataclasses import replace
from typing import Any, Final

import pytest
from jsonschema import Draft202012Validator

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.ids import ContentId, RecordId
from neptune.model.knowledge import AssertionKind, Known, NotApplicable, NotCovered, Unknown
from neptune.model.machine import (
    CalibrationParameter,
    DeclaredParameter,
    DescriptionExpansion,
    DescriptionExtension,
    HardwareSpecification,
    description_expansion_from_json,
    description_extension_from_json,
    hardware_specification_from_json,
)
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, adapter_locator
from neptune.model.record import SCHEMA_VERSION, Family, SchemaVersionError
from neptune.model.scalars import NonFinite
from neptune.model.schema import canonical_schema
from neptune.model.units import unit_from_json

DATA: Final = b'<robot name="r"><link name="a"/><joint name="j" type="revolute"/></robot>'
SOURCE: Final = content_id(DATA)
URDF: Final = transform_record(adapter_id="urdf", adapter_version="0.1.0", config={})
COMPONENT: Final = RecordId("rec:sha256:" + "3" * 64)
CONFIGURATION: Final = RecordId("rec:sha256:" + "4" * 64)
VALIDATOR: Final = Draft202012Validator(canonical_schema())


def cite(offset: int, length: int, *inner: Any) -> Provenance:
    evidence = EvidenceRef(SOURCE, (ByteRange(offset, length), *inner))
    return Provenance(evidence, URDF.id, AssertionKind.OBSERVED)


def numbers(name: str, *values: float | NonFinite, unit: str = "rad") -> DeclaredParameter:
    return DeclaredParameter(name, Known(values), Known(unit_from_json(unit), cite(0, len(DATA))))


def text(name: str, value: str) -> DeclaredParameter:
    return DeclaredParameter(name, Known(value), NotApplicable())


def specification(*parameters: DeclaredParameter) -> HardwareSpecification:
    provenance = cite(32, 37)
    return HardwareSpecification(
        id=evidence_record_id(HardwareSpecification.kind, provenance.evidence, URDF),
        provenance=provenance,
        subject=COMPONENT,
        parameters=parameters,
    )


def extension(*parameters: DeclaredParameter) -> DescriptionExtension:
    provenance = cite(16, 16)
    return DescriptionExtension(
        id=evidence_record_id(DescriptionExtension.kind, provenance.evidence, URDF),
        provenance=provenance,
        configuration=CONFIGURATION,
        element="gazebo",
        parameters=parameters,
    )


def expansion(*arguments: DeclaredParameter, size: int = 10) -> DescriptionExpansion:
    provenance = cite(0, len(DATA))
    return DescriptionExpansion(
        id=evidence_record_id(DescriptionExpansion.kind, provenance.evidence, URDF),
        provenance=provenance,
        language="xacro",
        digest=content_id(b"<robot/>"),
        size=size,
        arguments=arguments,
    )


SAMPLES: Final = (
    (
        specification(
            numbers("limit/lower", -1.5),
            numbers("limit/upper", NonFinite.POSITIVE_INFINITY),
            text("type", "revolute"),
            DeclaredParameter("visual/0/geometry/mesh/filename", NotCovered(), NotApplicable()),
        ),
        hardware_specification_from_json,
    ),
    (
        extension(text("plugin/0/filename", "libgazebo_ros_camera.so"), text("reference", "a")),
        description_extension_from_json,
    ),
    (extension(), description_extension_from_json),
    (
        expansion(
            DeclaredParameter("namespace", Known("uav0", cite(10, 5)), NotApplicable()),
            DeclaredParameter("port", NotCovered(cite(20, 5)), NotApplicable()),
        ),
        description_expansion_from_json,
    ),
)


@pytest.mark.parametrize(("record", "read"), SAMPLES)
def test_each_kind_round_trips_and_validates_against_the_schema(record: Any, read: Any) -> None:
    data = canonical_json.loads(canonical_json.dumps(record.to_json()))
    assert read(data) == record
    assert isinstance(data, dict)
    assert (data["kind"], data["schema_version"]) == (record.kind, SCHEMA_VERSION)
    assert type(record).family is Family.MACHINE
    assert not list(VALIDATOR.iter_errors(data))


@pytest.mark.parametrize(("record", "read"), SAMPLES)
def test_a_newer_record_is_refused_by_its_version(record: Any, read: Any) -> None:
    with pytest.raises(SchemaVersionError):
        read({**record.to_json(), "schema_version": SCHEMA_VERSION + 1, "added_later": 1})


def test_schema_version_2_still_reads_version_1_records() -> None:
    from neptune.model.machine import hardware_configuration_from_json

    line = (
        '{"id":"rec:sha256:8f6ee2cc76048b1d40463b67b8b409f732645d1708c33db799b79928dc056fe1",'
        '"kind":"hardware_configuration","machine":{"knowledge":"not_covered"},'
        '"name":{"knowledge":"known","value":"quadruped"},"provenance":{"assertion_kind":'
        '"observed","evidence":{"locator":[{"kind":"byte_range","length":750,"offset":22}],'
        '"source":"sha256:9f8f2aaf7b5965b8524e66d8e865b4d8fc9f1f608fa4454fc7b940f32d381291"},'
        '"transform":"rec:sha256:163827bfaac771e9ba9f51d5b630984a2aa8eb3d64ff852bfe24bf0303285e07"},'
        '"revision":{"knowledge":"not_covered"},"schema_version":1}'
    )
    record = hardware_configuration_from_json(canonical_json.loads(line.encode()))
    assert record.name == Known("quadruped")


def test_a_calibration_parameter_is_a_declared_parameter() -> None:
    assert CalibrationParameter is DeclaredParameter
    assert text("a", "b").to_json() == {
        "name": "a",
        "unit": {"knowledge": "not_applicable"},
        "value": {"knowledge": "known", "value": "b"},
    }


@pytest.mark.parametrize(
    "build",
    [
        lambda: specification(),  # a specification states something
        lambda: specification(text("b", "x"), text("a", "y")),  # sorted
        lambda: specification(text("a", "x"), text("a", "y")),  # unique
        lambda: replace(specification(text("a", "x")), subject=RecordId("not an id")),
        lambda: replace(extension(), element=""),
        lambda: replace(extension(), configuration=RecordId("nope")),
        lambda: expansion(size=-1),
        lambda: replace(expansion(), digest=ContentId("sha256:short")),
        lambda: replace(expansion(), language=""),
        lambda: expansion(numbers("scale", 2.0, unit="1")),  # an argument is text
        lambda: DeclaredParameter("t", Known("text"), Known(unit_from_json("m"))),
    ],
)
def test_what_the_kinds_refuse(build: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        build()


def test_an_expansion_citation_steps_into_the_expansion() -> None:
    inner = cite(
        0, len(DATA), adapter_locator("urdf:expansion", {"language": "xacro"}), ByteRange(3, 9)
    )
    record = HardwareSpecification(
        id=evidence_record_id(HardwareSpecification.kind, inner.evidence, URDF),
        provenance=inner,
        subject=COMPONENT,
        parameters=(DeclaredParameter("type", Unknown(), NotApplicable()),),
    )
    data = canonical_json.loads(canonical_json.dumps(record.to_json()))
    assert hardware_specification_from_json(data) == record
    assert not list(VALIDATOR.iter_errors(data))

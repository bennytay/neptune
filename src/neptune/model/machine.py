"""Machine context: the machines evidence declares, their hardware, software and calibration.

Every record here is an evidence record (ADR 0017) of the ``machine`` family, specified by ADR 0019:

- ``Machine``: a robot or vehicle that one piece of evidence declares by at least one identifier (a
  manifest's robot entry, a flight log's hardware UUID, a fleet register's row). Never inferred
  from content: a URDF names a robot *model*, and two robots may share one.
- ``HardwareConfiguration``: what one declaration says a machine is made of (a URDF, a manifest's
  hardware entry). ``HardwareComponent`` records name the configuration that declares them: links,
  joints, sensors, actuators, computers, power, payloads and tools, each with its frame. A
  configuration is never edited, so a tool change is another declaration and another record.
- ``SoftwareConfiguration``: what one declaration says ran on a machine, item by item, each with
  the version identities the evidence gives it (ADR 0014). An identity it does not give stays
  ``Unknown``, and the adapter reports a finding.
- ``Calibration``: what one declaration states about calibrating one subject: its parameters, what
  they apply to and when. Its extrinsics are ``FrameTransform`` records (ADR 0015) it lists.

A robot description (ADR 0039) adds three kinds: a ``HardwareSpecification`` holds what a
description states about one component or configuration beyond its name, category and frame (a
joint's type and limits, a link's inertia and geometry, a sensor's settings), a
``DescriptionExtension`` keeps a block for another tool opaque (a URDF's ``<gazebo>``), and a
``DescriptionExpansion`` identifies the document a macro source (Xacro) expands to.

Which of these applied to which run is a binding (MVL-38), never a field here.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final, TypeAlias

from neptune.model._fields import (
    Identifiers,
    check_identifiers,
    check_text_values,
    check_type,
    enum_decoder,
    exact_object,
    identifiers_from_json,
    identifiers_to_json,
    json_array,
    json_int,
    json_str,
    text_decoder,
    unit_json,
    values_of,
)
from neptune.model.frames import FrameRef, frame_ref_from_json
from neptune.model.ids import (
    ContentId,
    LogicalId,
    RecordId,
    check_text,
    logical_id_from_json,
    parse_content_id,
    parse_record_id,
)
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import Knowledge, NotApplicable, from_json, to_json
from neptune.model.provenance import (
    Provenance,
    check_evidence_record,
    evidence_record_json,
    evidence_record_object,
    provenance_from_json,
)
from neptune.model.record import Family
from neptune.model.scalars import NonFinite, Real, real_from_json, real_to_json
from neptune.model.time import Timestamp, timestamp_from_json
from neptune.model.units import Unit, unit_from_json
from neptune.model.versions import (
    BuildId,
    ContainerImageDigest,
    DeclaredVersion,
    FirmwareVersion,
    GitCommit,
    ModelCheckpointHash,
    SemanticVersion,
    VersionPrimitive,
    version_from_json,
    version_to_json,
)

# A release label: what the source calls the version. Commits, builds and digests have their own
# fields, so a value never sits in two of them.
Release: TypeAlias = SemanticVersion | DeclaredVersion | FirmwareVersion
# A digest the source states for the artifact that ran: a checkpoint's or a container image's.
ArtifactDigest: TypeAlias = ModelCheckpointHash | ContainerImageDigest


def _record_ids(field: str, ids: tuple[RecordId, ...]) -> None:
    if not isinstance(ids, tuple):
        raise TypeError(f"{field} must be a tuple of record ids, got {type(ids).__name__}")
    for record_id in ids:
        parse_record_id(record_id)
    if list(ids) != sorted(set(ids)):
        raise ValueError(f"{field} must be unique and sorted: {ids}")


def _wrong_kind(what: str, version: VersionPrimitive) -> ValueError:
    return ValueError(f"{what} cannot be a {version.kind}")


def _declared_version(data: JsonValue) -> DeclaredVersion:
    version = version_from_json(data)
    if isinstance(version, DeclaredVersion):
        return version
    raise _wrong_kind("a hardware revision", version)


def _git_commit(data: JsonValue) -> GitCommit:
    version = version_from_json(data)
    if isinstance(version, GitCommit):
        return version
    raise _wrong_kind("a commit", version)


def _release(data: JsonValue) -> Release:
    version = version_from_json(data)
    if isinstance(version, SemanticVersion | DeclaredVersion | FirmwareVersion):
        return version
    raise _wrong_kind("a release", version)


def _build(data: JsonValue) -> BuildId:
    version = version_from_json(data)
    if isinstance(version, BuildId):
        return version
    raise _wrong_kind("a build", version)


def _digest(data: JsonValue) -> ArtifactDigest:
    version = version_from_json(data)
    if isinstance(version, ModelCheckpointHash | ContainerImageDigest):
        return version
    raise _wrong_kind("a digest", version)


# --- Machine -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Machine:
    """A robot or vehicle as one piece of evidence declares it (ADR 0019 §1).

    ``provenance`` cites the declaration: a manifest's robot entry or a fleet register's row
    (``stated``), a flight log's vehicle information (``observed``). A declaration without an
    identifier declares no ``Machine``; the other machine-context records then say ``machine`` is
    ``Unknown``.

    - ``identifiers``: every id the declaration gives the machine, each with its own citation:
      ``("manifest", "spot-07")``, ``("serial", "BD-10470012")``, ``("px4.sys_uuid", "0002…")``.
      Ids that share a declaration are the evidence identity resolution (MVL-35) links by; equal
      content, names or folders never are.
    - ``manufacturer`` and ``model``: the declared maker and product (``Clearpath``, ``A200``).
    """

    kind: ClassVar[str] = "machine"
    family: ClassVar[Family] = Family.MACHINE
    id: RecordId
    provenance: Provenance
    identifiers: Identifiers
    manufacturer: Knowledge[str]
    model: Knowledge[str]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        check_identifiers("identifiers", self.identifiers)
        if not self.identifiers:
            raise ValueError("a machine is declared by at least one identifier")
        check_text_values("manufacturer", self.manufacturer)
        check_text_values("model", self.model)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "identifiers": identifiers_to_json(self.identifiers),
                "manufacturer": to_json(self.manufacturer),
                "model": to_json(self.model),
            },
        )


def machine_from_json(data: JsonValue) -> Machine:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data, Machine.kind, {"identifiers", "manufacturer", "model"}
    )
    return Machine(
        id=record_id,
        provenance=provenance,
        identifiers=identifiers_from_json(obj["identifiers"], provenance_from_json),
        manufacturer=from_json(
            obj["manufacturer"], text_decoder("manufacturer"), provenance_from_json
        ),
        model=from_json(obj["model"], text_decoder("model"), provenance_from_json),
    )


# --- Hardware ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class HardwareConfiguration:
    """What one declaration says a machine is physically made of (ADR 0019 §3).

    ``provenance`` cites the declaration: a URDF's ``<robot>`` element, a manifest's hardware entry.
    Its parts are ``HardwareComponent`` records that name it; its kinematics are the frames and
    transforms of the declaring source's ``FrameGraph``.

    - ``machine``: the declared id of the machine it describes. A URDF describes a model that many
      robots may share and has no place to name one, so a URDF's configuration has ``machine``
      ``NotCovered``, whatever its ``<robot name>`` looks like.
    - ``name``: the name the declaration gives the description (URDF ``<robot name="ur5e">``).
    - ``revision``: the hardware revision it declares (``rev-C``), as text.

    A configuration is a snapshot. A tool change, a new payload or a swapped sensor is another
    declaration and so another record; nothing is edited or merged. Which configuration applied
    to which run, and when, is a binding (MVL-38).
    """

    kind: ClassVar[str] = "hardware_configuration"
    family: ClassVar[Family] = Family.MACHINE
    id: RecordId
    provenance: Provenance
    machine: Knowledge[LogicalId]
    name: Knowledge[str]
    revision: Knowledge[DeclaredVersion]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        check_type("machine", self.machine, LogicalId)
        check_text_values("name", self.name)
        check_type("revision", self.revision, DeclaredVersion)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "machine": to_json(self.machine, LogicalId.to_json),
                "name": to_json(self.name),
                "revision": to_json(self.revision, version_to_json),
            },
        )


def hardware_configuration_from_json(data: JsonValue) -> HardwareConfiguration:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data, HardwareConfiguration.kind, {"machine", "name", "revision"}
    )
    return HardwareConfiguration(
        id=record_id,
        provenance=provenance,
        machine=from_json(obj["machine"], logical_id_from_json, provenance_from_json),
        name=from_json(obj["name"], text_decoder("name"), provenance_from_json),
        revision=from_json(obj["revision"], _declared_version, provenance_from_json),
    )


class ComponentCategory(StrEnum):
    """What kind of part the declaration says a component is. Structural: the adapter knows which
    list or element it read (a URDF ``<joint>``, a manifest's ``payloads``)."""

    LINK = "link"  # a rigid body of a kinematic description: a URDF, SDF or MJCF link
    JOINT = "joint"  # a kinematic joint between two links
    SENSOR = "sensor"  # a camera, lidar, IMU, GNSS receiver, encoder, force-torque sensor
    ACTUATOR = "actuator"  # a motor, servo, ESC, hydraulic or pneumatic drive
    COMPUTER = "computer"  # a flight controller, onboard computer, motor-controller board
    POWER = "power"  # a battery, power-distribution board, tether
    PAYLOAD = "payload"  # something carried: a gimbal, a sensor pod, a cargo mount
    TOOL = "tool"  # an end effector: a gripper, a vacuum cup, a drill


@dataclass(frozen=True)
class HardwareComponent:
    """One part a hardware configuration declares (ADR 0019 §4).

    ``provenance`` cites the part's own declaration (a URDF ``<link>`` element, a manifest's payload
    entry), and ``configuration`` is the ``HardwareConfiguration`` the same transform says it
    belongs to.

    - ``name``: the declared name, verbatim (``shoulder_pan_joint``, ``front_lidar``).
    - ``model``: the declared make or model (``Velodyne VLP-16``, ``Robotiq 2F-85``).
    - ``identifiers``: ids the declaration gives this part (a sensor's serial, a PX4 device id).
    - ``frame``: the frame the part defines or is mounted at, in a declared ``FrameGraph``. A URDF
      link defines its link frame; a joint's frame is its child link's.

    What only one category has (a joint's type and limits, a link's inertia and geometry, a
    sensor's rate) arrives as new record kinds that name the component, never as new fields here.
    """

    kind: ClassVar[str] = "hardware_component"
    family: ClassVar[Family] = Family.MACHINE
    id: RecordId
    provenance: Provenance
    configuration: RecordId
    category: ComponentCategory
    name: Knowledge[str]
    model: Knowledge[str]
    identifiers: Identifiers
    frame: Knowledge[FrameRef]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        parse_record_id(self.configuration)
        if not isinstance(self.category, ComponentCategory):
            raise TypeError(f"category must be a ComponentCategory, got {self.category!r}")
        check_text_values("name", self.name)
        check_text_values("model", self.model)
        check_identifiers("identifiers", self.identifiers)
        check_type("frame", self.frame, FrameRef)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "category": str(self.category),
                "configuration": self.configuration,
                "frame": to_json(self.frame, FrameRef.to_json),
                "identifiers": identifiers_to_json(self.identifiers),
                "model": to_json(self.model),
                "name": to_json(self.name),
            },
        )


def hardware_component_from_json(data: JsonValue) -> HardwareComponent:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        HardwareComponent.kind,
        {"category", "configuration", "frame", "identifiers", "model", "name"},
    )
    return HardwareComponent(
        id=record_id,
        provenance=provenance,
        configuration=parse_record_id(json_str(obj["configuration"], "configuration")),
        category=enum_decoder(ComponentCategory)(obj["category"]),
        name=from_json(obj["name"], text_decoder("name"), provenance_from_json),
        model=from_json(obj["model"], text_decoder("model"), provenance_from_json),
        identifiers=identifiers_from_json(obj["identifiers"], provenance_from_json),
        frame=from_json(obj["frame"], frame_ref_from_json, provenance_from_json),
    )


# --- Software ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SoftwareItem:
    """One piece of software a declaration says ran, with the identities it gives it (ADR 0019 §5).

    Each identity has its own field, so a value never sits in two and every kind keeps its type:

    - ``commit``: the source revision it was built from (a PX4 log's ``ver_sw``).
    - ``release``: the version label the source gives it: SemVer only where the source's format
      makes it SemVer, a firmware version for a device's firmware, otherwise declared text.
    - ``build``: the build or CI id.
    - ``digest``: a digest the source states for the artifact: a checkpoint's or an image's.

    ``name`` is the declared name (``PX4``, ``nav2``, ``grasp-policy``); ``device`` is the device
    it ran on, as the declaration names it (firmware per device: ``esc0``, ``gps0``).
    """

    name: Knowledge[str]
    device: Knowledge[str]
    commit: Knowledge[GitCommit]
    release: Knowledge[Release]
    build: Knowledge[BuildId]
    digest: Knowledge[ArtifactDigest]

    def __post_init__(self) -> None:
        check_text_values("name", self.name)
        check_text_values("device", self.device)
        check_type("commit", self.commit, GitCommit)
        check_type("release", self.release, SemanticVersion | DeclaredVersion | FirmwareVersion)
        check_type("build", self.build, BuildId)
        check_type("digest", self.digest, ModelCheckpointHash | ContainerImageDigest)

    def to_json(self) -> JsonObject:
        return {
            "build": to_json(self.build, version_to_json),
            "commit": to_json(self.commit, version_to_json),
            "device": to_json(self.device),
            "digest": to_json(self.digest, version_to_json),
            "name": to_json(self.name),
            "release": to_json(self.release, version_to_json),
        }


def software_item_from_json(data: JsonValue) -> SoftwareItem:
    obj = exact_object(
        data, "software item", {"build", "commit", "device", "digest", "name", "release"}
    )
    decode = provenance_from_json
    return SoftwareItem(
        name=from_json(obj["name"], text_decoder("name"), decode),
        device=from_json(obj["device"], text_decoder("device"), decode),
        commit=from_json(obj["commit"], _git_commit, decode),
        release=from_json(obj["release"], _release, decode),
        build=from_json(obj["build"], _build, decode),
        digest=from_json(obj["digest"], _digest, decode),
    )


@dataclass(frozen=True)
class SoftwareConfiguration:
    """The software one declaration says ran on a machine (ADR 0019 §5).

    ``provenance`` cites the declaration: a flight log's version information, a build-info file, a
    deployment manifest. ``machine`` is the declared id of the machine it ran on. ``software`` lists
    every item the declaration names, in its order.

    Missing identity is never blank: an item whose identity the evidence does not give holds
    ``Unknown``, and the adapter reports it as a finding.
    """

    kind: ClassVar[str] = "software_configuration"
    family: ClassVar[Family] = Family.MACHINE
    id: RecordId
    provenance: Provenance
    machine: Knowledge[LogicalId]
    software: tuple[SoftwareItem, ...]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        check_type("machine", self.machine, LogicalId)
        if not isinstance(self.software, tuple):
            raise TypeError(f"software must be a tuple, got {type(self.software).__name__}")
        if not self.software:
            raise ValueError("a software configuration names at least one item")
        for item in self.software:
            if not isinstance(item, SoftwareItem):
                raise TypeError(f"software must hold SoftwareItems, got {item!r}")
        if len(set(self.software)) != len(self.software):
            raise ValueError("software items repeat")

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "machine": to_json(self.machine, LogicalId.to_json),
                "software": [item.to_json() for item in self.software],
            },
        )


def software_configuration_from_json(data: JsonValue) -> SoftwareConfiguration:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data, SoftwareConfiguration.kind, {"machine", "software"}
    )
    return SoftwareConfiguration(
        id=record_id,
        provenance=provenance,
        machine=from_json(obj["machine"], logical_id_from_json, provenance_from_json),
        software=tuple(
            software_item_from_json(item) for item in json_array(obj["software"], "software")
        ),
    )


# --- Calibration -------------------------------------------------------------------------------

# The declared numbers of a parameter in source order, or the declared text of a setting.
ParameterValue: TypeAlias = str | tuple[Real, ...]


def _check_parameter_value(name: str, value: ParameterValue) -> None:
    if isinstance(value, str):
        check_text(name, value)
        return
    if not isinstance(value, tuple):
        raise TypeError(f"{name} must be text or a tuple of reals, got {type(value).__name__}")
    for number in value:
        if not isinstance(number, float | NonFinite):
            # 1 and 1.0 are different canonical JSON; the adapter decides once, with float().
            raise TypeError(f"{name} numbers must be floats or NonFinite, got {number!r}")
        real_to_json(number)  # a bare non-finite float must be a NonFinite


def _parameter_value_json(value: ParameterValue) -> JsonValue:
    if isinstance(value, str):
        return value
    return [real_to_json(number) for number in value]


def _parameter_value_from_json(data: JsonValue) -> ParameterValue:
    if isinstance(data, str):
        return data
    return tuple(real_from_json(number) for number in json_array(data, "parameter value"))


@dataclass(frozen=True)
class DeclaredParameter:
    """One named value a declaration states, exactly as declared (ADR 0019 §6, ADR 0039 §2).

    A calibration's parameters, and a hardware specification's, extension's or expansion's.
    ``name`` is the declared key, verbatim; a nested key is its path, as the adapter documents it
    (``camera_matrix/data``, ``limit/effort``), and each innermost array of a nested array is its
    own parameter. ``value`` holds the declared numbers in source order, integers read with
    ``float()``, or the declared text of a setting (``distortion_model: radtan``). ``unit`` is the
    numbers' declared unit; text has none, so its unit is ``NotApplicable``. What the numbers mean
    (which is ``fx``) is the declared model's, read by consumers or derived transforms, never
    reordered here.
    """

    name: str
    value: Knowledge[ParameterValue]
    unit: Knowledge[Unit]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError(f"parameter name must be a str, got {type(self.name).__name__}")
        check_text("parameter name", self.name)
        check_type(self.name, self.value, str | tuple)
        for value in values_of(self.value):
            _check_parameter_value(self.name, value)
        check_type(f"{self.name} unit", self.unit, Unit)
        texts = any(isinstance(value, str) for value in values_of(self.value))
        if texts and not isinstance(self.unit, NotApplicable):
            raise ValueError(f"{self.name} is text, so its unit is NotApplicable")

    def to_json(self) -> JsonObject:
        return {
            "name": self.name,
            "unit": to_json(self.unit, unit_json),
            "value": to_json(self.value, _parameter_value_json),
        }


def declared_parameter_from_json(data: JsonValue) -> DeclaredParameter:
    obj = exact_object(data, "declared parameter", {"name", "unit", "value"})
    return DeclaredParameter(
        name=json_str(obj["name"], "parameter name"),
        value=from_json(obj["value"], _parameter_value_from_json, provenance_from_json),
        unit=from_json(obj["unit"], unit_from_json, provenance_from_json),
    )


# A calibration's parameters were the first declared parameters (ADR 0019 §6); the JSON is the same.
CalibrationParameter = DeclaredParameter
calibration_parameter_from_json = declared_parameter_from_json


def _check_parameters(field: str, parameters: tuple[DeclaredParameter, ...]) -> None:
    """Declared parameters: a tuple of them, sorted by name, each name once."""
    if not isinstance(parameters, tuple):
        raise TypeError(f"{field} must be a tuple, got {type(parameters).__name__}")
    for parameter in parameters:
        if not isinstance(parameter, DeclaredParameter):
            raise TypeError(f"not a DeclaredParameter: {parameter!r}")
    names = [parameter.name for parameter in parameters]
    if names != sorted(set(names)):
        raise ValueError(f"{field} names must be unique and sorted: {names}")


def _parameters_from_json(data: JsonValue, field: str) -> tuple[DeclaredParameter, ...]:
    return tuple(declared_parameter_from_json(item) for item in json_array(data, field))


@dataclass(frozen=True)
class Calibration:
    """What one declaration states about calibrating one subject (ADR 0019 §6).

    ``provenance`` cites the declaration: one camera's entry in a Kalibr camchain, a ROS
    ``camera_info`` file, a hand-eye result, a flight log's calibration parameters for one sensor.

    - What it applies to, as declared: ``machine`` (a declared machine id), ``hardware_revision``
      (a calibration for revision ``rev-C`` only) and ``subject`` (the declared name of what was
      calibrated: ``cam0``, ``narrow_stereo``, ``accel0``). Binding it to hardware and runs is
      MVL-38's; alignment of its frames with a URDF's is MVL-37's.
    - When, as declared: ``performed`` (when it was measured), ``valid_from`` and ``valid_until``
      (the window it states for itself). Not checked against each other.
    - What it states: ``parameters`` (intrinsics and other values, sorted by name) and
      ``extrinsics`` (the ``FrameTransform`` records it declares, in its own ``FrameGraph``, by id).
    """

    kind: ClassVar[str] = "calibration"
    family: ClassVar[Family] = Family.MACHINE
    id: RecordId
    provenance: Provenance
    machine: Knowledge[LogicalId]
    hardware_revision: Knowledge[DeclaredVersion]
    subject: Knowledge[str]
    performed: Knowledge[Timestamp]
    valid_from: Knowledge[Timestamp]
    valid_until: Knowledge[Timestamp]
    parameters: tuple[DeclaredParameter, ...]
    extrinsics: tuple[RecordId, ...]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        check_type("machine", self.machine, LogicalId)
        check_type("hardware_revision", self.hardware_revision, DeclaredVersion)
        check_text_values("subject", self.subject)
        for name in ("performed", "valid_from", "valid_until"):
            check_type(name, getattr(self, name), Timestamp)
        _check_parameters("parameter", self.parameters)
        _record_ids("extrinsics", self.extrinsics)
        if not self.parameters and not self.extrinsics:
            raise ValueError("a calibration states at least one parameter or extrinsic")

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "extrinsics": list(self.extrinsics),
                "hardware_revision": to_json(self.hardware_revision, version_to_json),
                "machine": to_json(self.machine, LogicalId.to_json),
                "parameters": [parameter.to_json() for parameter in self.parameters],
                "performed": to_json(self.performed, Timestamp.to_json),
                "subject": to_json(self.subject),
                "valid_from": to_json(self.valid_from, Timestamp.to_json),
                "valid_until": to_json(self.valid_until, Timestamp.to_json),
            },
        )


def calibration_from_json(data: JsonValue) -> Calibration:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        Calibration.kind,
        {
            "extrinsics",
            "hardware_revision",
            "machine",
            "parameters",
            "performed",
            "subject",
            "valid_from",
            "valid_until",
        },
    )
    return Calibration(
        id=record_id,
        provenance=provenance,
        machine=from_json(obj["machine"], logical_id_from_json, provenance_from_json),
        hardware_revision=from_json(
            obj["hardware_revision"], _declared_version, provenance_from_json
        ),
        subject=from_json(obj["subject"], text_decoder("subject"), provenance_from_json),
        performed=from_json(obj["performed"], timestamp_from_json, provenance_from_json),
        valid_from=from_json(obj["valid_from"], timestamp_from_json, provenance_from_json),
        valid_until=from_json(obj["valid_until"], timestamp_from_json, provenance_from_json),
        parameters=_parameters_from_json(obj["parameters"], "parameters"),
        extrinsics=tuple(
            parse_record_id(json_str(extrinsic, "extrinsic"))
            for extrinsic in json_array(obj["extrinsics"], "extrinsics")
        ),
    )


# --- Robot descriptions (ADR 0039) ------------------------------------------------------------

# The schema version that added the robot-description kinds; their records are written at it
# (ADR 0037 §1).
DESCRIPTION_SINCE: Final = 5


@dataclass(frozen=True)
class HardwareSpecification:
    """What one declaration states about one component or configuration beyond its name, category
    and frame (ADR 0019 §4's extension rule, ADR 0039 §2).

    ``provenance`` cites the element that declares the subject (a URDF ``<joint>``, ``<link>`` or
    ``<sensor>``), and ``subject`` is the ``HardwareComponent`` or ``HardwareConfiguration`` the
    same transform made from it. ``parameters`` are what the declaration states, by the adapter's
    documented names (``type``, ``limit/effort``, ``visual/0/geometry/mesh/filename``), each with
    its numbers in source order or its text, and its unit; each value cites the element it was
    read from. What the file does not state is not a parameter: a format's defaults are its
    specification's, applied by consumers or derived transforms, never written here.
    """

    kind: ClassVar[str] = "hardware_specification"
    family: ClassVar[Family] = Family.MACHINE
    since: ClassVar[int] = DESCRIPTION_SINCE
    id: RecordId
    provenance: Provenance
    subject: RecordId
    parameters: tuple[DeclaredParameter, ...]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        parse_record_id(self.subject)
        _check_parameters("parameter", self.parameters)
        if not self.parameters:
            raise ValueError("a hardware specification states at least one parameter")

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "parameters": [parameter.to_json() for parameter in self.parameters],
                "subject": self.subject,
            },
            self.since,
        )


def hardware_specification_from_json(data: JsonValue) -> HardwareSpecification:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data, HardwareSpecification.kind, {"parameters", "subject"}, HardwareSpecification.since
    )
    return HardwareSpecification(
        id=record_id,
        provenance=provenance,
        subject=parse_record_id(json_str(obj["subject"], "subject")),
        parameters=_parameters_from_json(obj["parameters"], "parameters"),
    )


@dataclass(frozen=True)
class DescriptionExtension:
    """A block a robot description declares for another tool, kept opaque (ADR 0039 §3).

    A URDF's ``<gazebo>`` or ``<ros2_control>`` element, or any top-level element the format does
    not define. ``provenance`` cites the whole block, which stays in the source's bytes; nothing in
    it is interpreted. ``configuration`` is the ``HardwareConfiguration`` of the description that
    holds it, ``element`` the block's element name verbatim, and ``parameters`` the block's own
    attributes and the plugins it names, as text, by the adapter's documented names.
    """

    kind: ClassVar[str] = "description_extension"
    family: ClassVar[Family] = Family.MACHINE
    since: ClassVar[int] = DESCRIPTION_SINCE
    id: RecordId
    provenance: Provenance
    configuration: RecordId
    element: str
    parameters: tuple[DeclaredParameter, ...]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        parse_record_id(self.configuration)
        if not isinstance(self.element, str):
            raise TypeError(f"element must be a str, got {type(self.element).__name__}")
        check_text("element", self.element)
        _check_parameters("parameter", self.parameters)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "configuration": self.configuration,
                "element": self.element,
                "parameters": [parameter.to_json() for parameter in self.parameters],
            },
            self.since,
        )


def description_extension_from_json(data: JsonValue) -> DescriptionExtension:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        DescriptionExtension.kind,
        {"configuration", "element", "parameters"},
        DescriptionExtension.since,
    )
    return DescriptionExtension(
        id=record_id,
        provenance=provenance,
        configuration=parse_record_id(json_str(obj["configuration"], "configuration")),
        element=json_str(obj["element"], "element"),
        parameters=_parameters_from_json(obj["parameters"], "parameters"),
    )


@dataclass(frozen=True)
class DescriptionExpansion:
    """The document a macro language's source expands to, by its digest (ADR 0039 §4).

    ``provenance`` cites the whole source (a Xacro file). The expansion is what the transform
    decoded from it: records read from the expansion cite it through the adapter's expansion step,
    and ``digest`` and ``size`` identify its bytes, which re-running the transform reproduces.
    ``language`` names the macro language (``xacro``). ``arguments`` are the arguments the source
    declares, as text, with the value the expansion used: a declared default, or ``NotCovered``
    where only the environment could give one.
    """

    kind: ClassVar[str] = "description_expansion"
    family: ClassVar[Family] = Family.MACHINE
    since: ClassVar[int] = DESCRIPTION_SINCE
    id: RecordId
    provenance: Provenance
    language: str
    digest: ContentId
    size: int
    arguments: tuple[DeclaredParameter, ...]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        if not isinstance(self.language, str):
            raise TypeError(f"language must be a str, got {type(self.language).__name__}")
        check_text("language", self.language)
        parse_content_id(self.digest)
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError(f"size must be a non-negative integer, got {self.size!r}")
        _check_parameters("argument", self.arguments)
        for argument in self.arguments:
            if any(not isinstance(value, str) for value in values_of(argument.value)):
                raise ValueError(f"argument {argument.name} is text")

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "arguments": [argument.to_json() for argument in self.arguments],
                "digest": self.digest,
                "language": self.language,
                "size": self.size,
            },
            self.since,
        )


def description_expansion_from_json(data: JsonValue) -> DescriptionExpansion:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        DescriptionExpansion.kind,
        {"arguments", "digest", "language", "size"},
        DescriptionExpansion.since,
    )
    return DescriptionExpansion(
        id=record_id,
        provenance=provenance,
        language=json_str(obj["language"], "language"),
        digest=parse_content_id(json_str(obj["digest"], "digest")),
        size=json_int(obj["size"], "size"),
        arguments=_parameters_from_json(obj["arguments"], "arguments"),
    )

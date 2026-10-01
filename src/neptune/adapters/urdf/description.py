"""A URDF document's elements as canonical records (ADR 0019 §3 and §4, ADR 0039).

``describe`` reads the ``<robot>`` element of a parsed (or expanded) document. Every record cites
the element it comes from through ``cite``, which the adapter builds for the document's bytes:
the source's own for a URDF, the expansion's for a Xacro file.

- ``<robot>``: a ``HardwareConfiguration`` (``machine`` and ``revision`` ``NotCovered``: a URDF
  has no place for either), a ``FrameGraph`` of the whole source, and a ``HardwareSpecification``
  of the robot's other attributes and its top-level materials.
- ``<link>``: a ``link`` component and its ``Frame``; ``<joint>``: a ``joint`` component, framed
  at its child link, and the ``FrameTransform`` of its origin. A ``HardwareSpecification`` holds
  each one's declared detail.
- ``<transmission>``: an ``actuator`` component per ``<actuator>``; ``<sensor>``: a ``sensor``
  component framed at its parent link.
- ``<gazebo>``, ``<ros2_control>`` and any other top-level element: a ``DescriptionExtension``,
  kept opaque; a ``<sensor>`` inside a ``<gazebo>`` block is also a ``sensor`` component, framed
  at the block's ``reference``.

Units are the URDF specification's (metres, radians, kilograms, newtons, seconds), ``Known``
and citing the ``<robot>`` element: the bytes that establish the format (ADR 0017 §6). Nothing
is converted. Joint limits are in radians or metres as the joint's type says; for another type
their unit is ``Unknown``. A joint without ``<origin>`` is placed by the specification's identity
transform, which is how the specification reads that element.
"""

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, TypeAlias

from neptune.adapters.urdf.xacro import NOT_COVERED
from neptune.adapters.urdf.xmltree import Element
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.frames import (
    STATIC,
    EulerAngles,
    EulerMode,
    EulerSequence,
    FrameRef,
    Pose,
    TransformDirection,
    Translation,
)
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import (
    INHERITED,
    AssertionKind,
    Knowledge,
    Known,
    NotApplicable,
    NotCovered,
    ProvenanceSlot,
    Unknown,
)
from neptune.model.machine import (
    ComponentCategory,
    DeclaredParameter,
    DescriptionExtension,
    HardwareComponent,
    HardwareConfiguration,
    HardwareSpecification,
    ParameterValue,
)
from neptune.model.provenance import EvidenceRef, Provenance, TransformRecord
from neptune.model.reference import Frame, FrameGraph, FrameTransform
from neptune.model.scalars import real
from neptune.model.units import Unit, unit_from_json

Record: TypeAlias = (
    HardwareConfiguration
    | HardwareComponent
    | HardwareSpecification
    | DescriptionExtension
    | FrameGraph
    | Frame
    | FrameTransform
)

_NUMBER: Final = re.compile(
    r"[+-]?(?:(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|[iI][nN][fF](?:[iI][nN][iI][tT][yY])?|[nN][aA][nN])"
)
# Elements the URDF specification lets repeat inside a parent, by the parent's element; each
# occurrence is numbered. ``plugin`` is numbered anywhere.
REPEATED: Final[Mapping[str, frozenset[str]]] = {
    "actuator": frozenset({"hardwareInterface"}),
    "joint": frozenset({"hardwareInterface"}),
    "link": frozenset({"collision", "visual"}),
    "robot": frozenset({"material"}),
    "transmission": frozenset({"actuator", "joint"}),
}
_ANGULAR: Final = frozenset({"continuous", "revolute"})
_LINEAR: Final = frozenset({"prismatic"})

# A numeric parameter's unit: a unit symbol, ``?`` for numbers the specification gives no unit,
# or a joint-type rule: ``position`` (rad | m), ``effort`` (N.m | N), ``velocity``, ``damping``,
# ``friction``.
_JOINT_RULES: Final[Mapping[str, tuple[str, str]]] = {
    "position": ("rad", "m"),
    "effort": ("N.m", "N"),
    "velocity": ("rad.s^-1", "m.s^-1"),
    "damping": ("N.m.s.rad^-1", "N.s.m^-1"),
    "friction": ("N.m", "N"),
}


def _geometry(prefix: str) -> dict[str, str]:
    return {
        f"{prefix}/origin/xyz": "m",
        f"{prefix}/origin/rpy": "rad",
        f"{prefix}/geometry/box/size": "m",
        f"{prefix}/geometry/cylinder/radius": "m",
        f"{prefix}/geometry/cylinder/length": "m",
        f"{prefix}/geometry/sphere/radius": "m",
        f"{prefix}/geometry/capsule/radius": "m",
        f"{prefix}/geometry/capsule/length": "m",
        f"{prefix}/geometry/mesh/scale": "1",
    }


LINK_UNITS: Final[Mapping[str, str]] = {
    "inertial/origin/xyz": "m",
    "inertial/origin/rpy": "rad",
    "inertial/mass/value": "kg",
    **{f"inertial/inertia/i{axes}": "kg.m^2" for axes in ("xx", "xy", "xz", "yy", "yz", "zz")},
    **_geometry("visual/#"),
    "visual/#/material/color/rgba": "1",
    **_geometry("collision/#"),
}
JOINT_UNITS: Final[Mapping[str, str]] = {
    "origin/xyz": "m",
    "origin/rpy": "rad",
    "axis/xyz": "1",
    "limit/lower": "position",
    "limit/upper": "position",
    "limit/effort": "effort",
    "limit/velocity": "velocity",
    "dynamics/damping": "damping",
    "dynamics/friction": "friction",
    "calibration/rising": "position",
    "calibration/falling": "position",
    "calibration/reference_position": "position",
    "mimic/multiplier": "1",
    "mimic/offset": "position",
    "safety_controller/soft_lower_limit": "position",
    "safety_controller/soft_upper_limit": "position",
    "safety_controller/k_position": "?",
    "safety_controller/k_velocity": "?",
}
SENSOR_UNITS: Final[Mapping[str, str]] = {
    "update_rate": "Hz",
    "origin/xyz": "m",
    "origin/rpy": "rad",
    "camera/image/width": "?",
    "camera/image/height": "?",
    "camera/image/hfov": "rad",
    "camera/image/near": "m",
    "camera/image/far": "m",
    **{
        f"ray/{direction}/{name}": unit
        for direction in ("horizontal", "vertical")
        for name, unit in (
            ("samples", "?"),
            ("resolution", "?"),
            ("min_angle", "rad"),
            ("max_angle", "rad"),
        )
    },
}
ACTUATOR_UNITS: Final[Mapping[str, str]] = {"mechanicalReduction": "1"}
ROBOT_UNITS: Final[Mapping[str, str]] = {"material/#/color/rgba": "1"}


def _pattern(name: str) -> str:
    return "/".join("#" if part.isdigit() else part for part in name.split("/"))


@dataclass
class Description:
    """What ``describe`` read: records and findings, each once."""

    records: list[Record] = field(default_factory=list)
    findings: dict[RecordId, IngestFinding] = field(default_factory=dict)


class _Reader:
    def __init__(
        self, root: Element, cite: Callable[[Element], EvidenceRef], transform: TransformRecord
    ) -> None:
        self.root = root
        self.cite = cite
        self.transform = transform
        self.out = Description()
        self.spec = self.provenance(root)
        self.graph = self.record_id(FrameGraph.kind, root)
        self.configuration = self.record_id(HardwareConfiguration.kind, root)
        self.links: dict[str, Element] = {}
        self.joints: dict[str, Element] = {}

    # Citations

    def provenance(self, element: Element) -> Provenance:
        return Provenance(self.cite(element), self.transform.id, AssertionKind.OBSERVED)

    def record_id(self, kind: str, element: Element) -> RecordId:
        return evidence_record_id(kind, self.cite(element), self.transform)

    def finding(
        self,
        name: str,
        category: FindingCategory,
        severity: Severity,
        element: Element,
        message: str,
        details: Mapping[str, JsonValue] | None = None,
        related: tuple[Element, ...] = (),
        records: tuple[RecordId, ...] = (),
    ) -> None:
        finding = ingest_finding(
            code=f"urdf.{name}",
            category=category,
            severity=severity,
            subject=self.cite(element),
            transform=self.transform,
            message=message,
            details=details,
            related=tuple(self.cite(other) for other in related),
            records=records,
        )
        self.out.findings.setdefault(finding.id, finding)

    def unit(self, symbol: str) -> Knowledge[Unit]:
        return Known(unit_from_json(symbol), self.spec)

    # Values

    def state(
        self, element: Element, key: str, slot: ProvenanceSlot
    ) -> NotCovered | Unknown | None:
        """``NotCovered`` or ``Unknown`` for a value an expansion could not resolve, else None."""
        unresolved = element.unresolved.get(key)
        if unresolved is None:
            return None
        return NotCovered(slot) if unresolved == NOT_COVERED else Unknown(slot)

    def text(self, element: Element, name: str, slot: ProvenanceSlot) -> Knowledge[str]:
        """An attribute as declared text: ``Unknown`` if absent or blank."""
        value = element.attribute(name)
        unresolved = self.state(element, name, slot)
        if unresolved is not None:
            return unresolved
        if value is None or not value.strip():
            return Unknown(slot)
        return Known(value, slot)

    def frame(
        self, name: Knowledge[str], element: Element, slot: ProvenanceSlot = INHERITED
    ) -> Knowledge[FrameRef]:
        if not isinstance(name, Known):
            return Unknown(INHERITED)
        try:
            return Known(FrameRef(name.value, self.graph), slot)
        except ValueError:
            self.finding(
                "name_unrepresentable",
                FindingCategory.UNREPRESENTABLE,
                Severity.WARNING,
                element,
                "a link name is too long to name a frame; the frame is unknown",
                {"length": len(name.value)},
            )
            return Unknown(INHERITED)

    def numbers(
        self, element: Element, name: str, raw: str, slot: ProvenanceSlot
    ) -> Knowledge[ParameterValue]:
        tokens = raw.split()
        if not tokens:
            return Unknown(slot)
        if not all(_NUMBER.fullmatch(token) for token in tokens):
            self.finding(
                "value_unparsable",
                FindingCategory.CORRUPT,
                Severity.WARNING,
                element,
                f"{name[-128:]} must be numbers by the URDF specification and is not;"
                " it is unknown",
                {"parameter": name[-128:]},
            )
            return Unknown(slot)
        return Known(tuple(real(float(token)) for token in tokens), slot)

    def parameter(
        self,
        name: str,
        element: Element,
        key: str,
        raw: str,
        units: Mapping[str, str],
        joint_type: str | None,
        subject: Element,
    ) -> DeclaredParameter:
        slot: ProvenanceSlot = INHERITED if element is subject else self.provenance(element)
        rule = units.get(_pattern(name))
        unresolved = self.state(element, key, slot)
        if rule is None:
            declared: ParameterValue = raw
            text: Knowledge[ParameterValue] = (
                unresolved
                if unresolved is not None
                else (Known(declared, slot) if raw.strip() else Unknown(slot))
            )
            return DeclaredParameter(name, text, NotApplicable())
        unit: Knowledge[Unit]
        if rule == "?":
            unit = Unknown(slot)
        elif rule in _JOINT_RULES:
            angular, linear = _JOINT_RULES[rule]
            if joint_type in _ANGULAR:
                unit = self.unit(angular)
            elif joint_type in _LINEAR:
                unit = self.unit(linear)
            else:
                unit = Unknown(slot)
        else:
            unit = self.unit(rule)
        value: Knowledge[ParameterValue] = (
            unresolved if unresolved is not None else self.numbers(element, name, raw, slot)
        )
        return DeclaredParameter(name, value, unit)

    def parameters(
        self,
        subject: Element,
        units: Mapping[str, str],
        joint_type: str | None = None,
        skip: frozenset[str] = frozenset({"name"}),
        prefix: str = "",
        descend: Callable[[Element], bool] = lambda _: True,
        number_repeats: bool = False,
    ) -> list[DeclaredParameter]:
        """The subject's declared detail, flattened to named parameters (ADR 0039 §2).

        Attributes are ``<path>/<attribute>`` and an element's text is ``<path>``, where the path
        joins element names from the subject down. An element the specification lets repeat is
        numbered (``visual/0``); another that repeats is reported and left out, or numbered when
        ``number_repeats`` (a dialect Neptune does not specify, such as Gazebo's). ``descend``
        chooses which of the subject's own children are read.
        """
        found: list[DeclaredParameter] = []

        def visit(element: Element, path: str) -> None:
            for key, value in element.attributes:
                if element is subject and (key in skip or key.startswith("xmlns")):
                    continue
                name = f"{path}/{key}" if path else key
                found.append(self.parameter(name, element, key, value, units, joint_type, subject))
            text = element.text()
            if element is not subject and (text.strip() or "#text" in element.unresolved):
                found.append(
                    self.parameter(path, element, "#text", text.strip(), units, None, subject)
                )
            counts: dict[str, int] = {}
            for child in element.elements():
                counts[child.tag] = counts.get(child.tag, 0) + 1
            index: dict[str, int] = {}
            repeated = REPEATED.get(element.tag, frozenset()) | {"plugin"}
            for child in element.elements():
                if element is subject and not descend(child):
                    continue
                tag = child.tag
                if tag in repeated or (number_repeats and counts[tag] > 1):
                    number = index.get(tag, 0)
                    index[tag] = number + 1
                    segment = f"{tag}/{number}"
                elif counts[tag] > 1:
                    if index.get(tag) is None:
                        index[tag] = 0
                        self.finding(
                            "element_repeated",
                            FindingCategory.INCONSISTENT,
                            Severity.WARNING,
                            child,
                            f"<{tag[:64]}> appears more than once where the URDF specification"
                            " allows one; none of them is recorded as a parameter",
                        )
                    continue
                else:
                    segment = tag
                visit(child, f"{path}/{segment}" if path else segment)

        visit(subject, prefix)
        unique: dict[str, DeclaredParameter] = {}
        for parameter in found:
            if parameter.name in unique:
                continue  # an attribute and a child's text with one name: the first is kept
            unique[parameter.name] = parameter
        return [unique[name] for name in sorted(unique)]

    def specification(
        self, subject: RecordId, element: Element, parameters: list[DeclaredParameter]
    ) -> None:
        if parameters:
            self.out.records.append(
                HardwareSpecification(
                    id=self.record_id(HardwareSpecification.kind, element),
                    provenance=self.provenance(element),
                    subject=subject,
                    parameters=tuple(parameters),
                )
            )

    def component(
        self,
        element: Element,
        category: ComponentCategory,
        name: Knowledge[str],
        frame: Knowledge[FrameRef],
    ) -> RecordId:
        record = HardwareComponent(
            id=self.record_id(HardwareComponent.kind, element),
            provenance=self.provenance(element),
            configuration=self.configuration,
            category=category,
            name=name,
            model=NotCovered(),
            identifiers=(),
            frame=frame,
        )
        self.out.records.append(record)
        return record.id

    def required(self, element: Element, what: str) -> None:
        self.finding(
            "required_missing",
            FindingCategory.MISSING,
            Severity.WARNING,
            element,
            f"the {element.tag[:64]} declares no {what}, which the URDF specification requires",
            {"missing": what},
        )

    def named(self, element: Element, seen: dict[str, Element]) -> Knowledge[str]:
        name = self.text(element, "name", INHERITED)
        if isinstance(name, Known):
            first = seen.setdefault(name.value, element)
            if first is not element:
                self.finding(
                    "name_repeated",
                    FindingCategory.INCONSISTENT,
                    Severity.WARNING,
                    element,
                    f"another {element.tag[:64]} has the same name; both are recorded",
                    related=(first,),
                )
        elif not isinstance(name, NotCovered) and element.attribute("name") is None:
            self.required(element, "name")
        return name

    # Elements

    def read(self) -> Description:
        root = self.root
        children = root.elements()
        if not any(child.tag == "link" for child in children):
            self.finding(
                "no_links",
                FindingCategory.MISSING,
                Severity.WARNING,
                root,
                "the robot declares no link, so it describes no hardware: a macro library or an"
                " empty description; no configuration is recorded",
            )
            return self.out
        if root.attribute("name") is None:
            self.required(root, "name")
        self.out.records.append(FrameGraph(self.graph, self.spec, ()))
        self.out.records.append(
            HardwareConfiguration(
                id=self.configuration,
                provenance=self.spec,
                machine=NotCovered(),
                name=self.text(root, "name", INHERITED),
                revision=NotCovered(),
            )
        )
        materials = [child for child in root.elements() if child.tag == "material"]
        robot = self.parameters(root, ROBOT_UNITS, descend=lambda child: child in materials)
        self.specification(self.configuration, root, robot)
        for element in children:
            if element.tag == "link":
                self.link(element)
        for element in children:
            match element.tag:
                case "link" | "material":
                    pass
                case "joint":
                    self.joint(element)
                case "transmission":
                    self.transmission(element)
                case "sensor":
                    self.sensor(element)
                case _:
                    self.extension(element)
        return self.out

    def link(self, element: Element) -> None:
        name = self.named(element, self.links)
        frame = self.frame(name, element)
        component = self.component(element, ComponentCategory.LINK, name, frame)
        if isinstance(frame, Known):
            self.out.records.append(
                Frame(
                    id=self.record_id(Frame.kind, element),
                    provenance=self.provenance(element),
                    ref=frame.value,
                    axes=Unknown(),
                    handedness=Unknown(),
                )
            )
        self.specification(component, element, self.parameters(element, LINK_UNITS))

    def link_ref(self, joint: Element, role: str) -> tuple[Knowledge[FrameRef], Element | None]:
        """A joint's ``<parent>`` or ``<child>`` link, as a frame of this graph."""
        found = [child for child in joint.elements() if child.tag == role]
        if not found:
            self.required(joint, f"{role} link")
            return Unknown(INHERITED), None
        element = found[0]
        name = self.text(element, "link", self.provenance(element))
        if isinstance(name, Known) and name.value not in self.links:
            self.finding(
                "link_undeclared",
                FindingCategory.INCONSISTENT,
                Severity.WARNING,
                element,
                f"the joint's {role} names a link this description does not declare",
            )
        if not isinstance(name, Known) and element.attribute("link") is None:
            self.required(element, "link")
        return self.frame(name, element), element

    def joint(self, element: Element) -> None:
        name = self.named(element, self.joints)
        kind = self.text(element, "type", INHERITED)
        if not isinstance(kind, Known | NotCovered) and element.attribute("type") is None:
            self.required(element, "type")
        joint_type = kind.value if isinstance(kind, Known) else None
        parent, _ = self.link_ref(element, "parent")
        child, _ = self.link_ref(element, "child")
        component = self.component(element, ComponentCategory.JOINT, name, child)
        limits = [c for c in element.elements() if c.tag == "limit"]
        if joint_type in ("revolute", "prismatic") and not limits:
            self.required(element, "limit")
        if isinstance(parent, Known) and isinstance(child, Known):
            self.transform_of(element, parent.value, child.value)
        self.specification(component, element, self.parameters(element, JOINT_UNITS, joint_type))

    def transform_of(self, joint: Element, parent: FrameRef, child: FrameRef) -> None:
        origins = [c for c in joint.elements() if c.tag == "origin"]
        if len(origins) > 1:
            return  # reported as element_repeated with the joint's parameters
        values: list[tuple[float, ...]] = []
        for key in ("xyz", "rpy"):
            origin = origins[0] if origins else None
            raw = "0 0 0" if origin is None else origin.attribute(key)
            if origin is not None and key in origin.unresolved:
                return  # the expansion reported it; the transform is not recorded
            numbers = self.vector(raw if raw is not None else "0 0 0")
            if numbers is None:
                assert origin is not None
                self.finding(
                    "origin_invalid",
                    FindingCategory.CORRUPT,
                    Severity.WARNING,
                    origin,
                    f"the origin's {key} is not three finite numbers; the joint's transform is"
                    " not recorded",
                    {"attribute": key},
                )
                return
            values.append(numbers)
        if parent == child:
            self.finding(
                "joint_invalid",
                FindingCategory.INCONSISTENT,
                Severity.WARNING,
                joint,
                "the joint's parent and child are one link; its transform is not recorded",
            )
            return
        spec = self.spec
        self.out.records.append(
            FrameTransform(
                id=self.record_id(FrameTransform.kind, joint),
                provenance=self.provenance(joint),
                parent=parent,
                child=child,
                direction=Known(TransformDirection.CHILD_TO_PARENT, spec),
                value=Pose(
                    Translation(values[0], self.unit("m")),
                    EulerAngles(
                        values[1],
                        sequence=Known(EulerSequence.XYZ, spec),
                        mode=Known(EulerMode.EXTRINSIC, spec),
                        unit=self.unit("rad"),
                    ),
                ),
                validity=STATIC,
            )
        )

    @staticmethod
    def vector(raw: str) -> tuple[float, ...] | None:
        tokens = raw.split()
        if len(tokens) != 3 or not all(_NUMBER.fullmatch(token) for token in tokens):
            return None
        numbers = tuple(float(token) for token in tokens)
        return numbers if all(math.isfinite(number) for number in numbers) else None

    def transmission(self, element: Element) -> None:
        actuators = [child for child in element.elements() if child.tag == "actuator"]
        if not actuators:
            self.required(element, "actuator")
            return
        shared = self.parameters(
            element,
            {},
            skip=frozenset({"xmlns"}),
            prefix="transmission",
            descend=lambda child: child.tag in ("joint", "type"),
        )
        for actuator in actuators:
            name = self.text(actuator, "name", INHERITED)
            if not isinstance(name, Known | NotCovered) and actuator.attribute("name") is None:
                self.required(actuator, "name")
            component = self.component(actuator, ComponentCategory.ACTUATOR, name, NotCovered())
            own = self.parameters(actuator, ACTUATOR_UNITS)
            merged = {parameter.name: parameter for parameter in (*shared, *own)}
            self.specification(component, actuator, [merged[n] for n in sorted(merged)])

    def sensor(self, element: Element) -> None:
        name = self.text(element, "name", INHERITED)
        parents = [child for child in element.elements() if child.tag == "parent"]
        frame: Knowledge[FrameRef] = Unknown(INHERITED)
        if parents:
            link = self.text(parents[0], "link", self.provenance(parents[0]))
            frame = self.frame(link, parents[0])
        component = self.component(element, ComponentCategory.SENSOR, name, frame)
        self.specification(component, element, self.parameters(element, SENSOR_UNITS))

    def extension(self, element: Element) -> None:
        plugins: list[DeclaredParameter] = []
        index = 0
        stack = [element]
        while stack:  # plugins named anywhere in the block, outside its sensors
            current = stack.pop(0)
            for child in current.elements():
                if child.tag == "plugin":
                    plugins += self.parameters(
                        child, {}, skip=frozenset(), prefix=f"plugin/{index}", descend=_none
                    )
                    index += 1
                elif child.tag != "sensor":
                    stack.append(child)
        own = self.parameters(element, {}, skip=frozenset(), descend=_none)
        merged = {parameter.name: parameter for parameter in (*own, *plugins)}
        self.out.records.append(
            DescriptionExtension(
                id=self.record_id(DescriptionExtension.kind, element),
                provenance=self.provenance(element),
                configuration=self.configuration,
                element=element.tag,
                parameters=tuple(merged[name] for name in sorted(merged)),
            )
        )
        if element.tag == "gazebo":
            reference = self.text(element, "reference", self.provenance(element))
            for sensor in (child for child in element.elements() if child.tag == "sensor"):
                self.gazebo_sensor(sensor, reference, element)

    def gazebo_sensor(self, sensor: Element, reference: Knowledge[str], block: Element) -> None:
        name = self.text(sensor, "name", INHERITED)
        frame = self.frame(reference, block, self.provenance(block))
        if isinstance(reference, Known) and reference.value not in self.links:
            self.finding(
                "link_undeclared",
                FindingCategory.INCONSISTENT,
                Severity.WARNING,
                block,
                "the sensor's gazebo reference names a link this description does not declare",
            )
        component = self.component(sensor, ComponentCategory.SENSOR, name, frame)
        self.specification(component, sensor, self.parameters(sensor, {}, number_repeats=True))


def _none(_: Element) -> bool:
    return False


def describe(
    root: Element, cite: Callable[[Element], EvidenceRef], transform: TransformRecord
) -> Description:
    """The records and findings of a ``<robot>`` document; ``cite`` locates an element."""
    return _Reader(root, cite, transform).read()

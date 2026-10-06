"""The URDF/Xacro adapter on real-shaped robots, malformed and hostile input, bounds and lineage.

The oracles are independent readings committed under ``tests/fixtures/urdf/oracle/``:
``urdf_parser_py``'s view of each robot and real xacro's expansion of the quadrotor (see the
fixture README). The adapter must agree with them on topology, placements, axes and limits.
"""

import importlib.util
import json
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.contract import (
    NAME_ONLY,
    PROBE_HEAD_SIZE,
    STRUCTURE,
    ProbeHints,
    configure,
)
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.registry import AdapterRegistry
from neptune.adapters.text import TextAdapter
from neptune.adapters.urdf import DESCRIPTOR, EXPANSION_STEP, UrdfAdapter
from neptune.adapters.urdf import xacro as xacro_module
from neptune.adapters.urdf.xmltree import parse, serialize
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.model.frames import EulerAngles, EulerMode, EulerSequence, Pose, TransformDirection
from neptune.model.knowledge import Known, NotApplicable, NotCovered, Unknown
from neptune.model.machine import (
    ComponentCategory,
    DeclaredParameter,
    DescriptionExpansion,
    DescriptionExtension,
    HardwareComponent,
    HardwareConfiguration,
    HardwareSpecification,
    Machine,
)
from neptune.model.provenance import AdapterLocator, ByteRange, EvidenceRef
from neptune.model.reference import Frame, FrameGraph, FrameTransform
from neptune.model.scalars import NonFinite
from neptune.model.units import unit_from_json

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "urdf"
ROBOTS: Final = ("arm6", "diff_drive", "quadrotor")


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_urdf", FIXTURES / "make_urdf.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(data: bytes, **config: Any) -> SourceOutput:
    return ingest_source(UrdfAdapter(), BytesReader(data), config)


def records(output: SourceOutput, kind: type) -> list[Any]:
    return [record for record in output.records() if isinstance(record, kind)]


def codes(output: SourceOutput) -> list[str]:
    return sorted(finding.code for finding in output.findings())


def components(output: SourceOutput, category: ComponentCategory) -> dict[str, HardwareComponent]:
    found = records(output, HardwareComponent)
    return {c.name.value: c for c in found if c.category is category and isinstance(c.name, Known)}


def spec(output: SourceOutput, subject: Any) -> dict[str, DeclaredParameter]:
    (found,) = [r for r in records(output, HardwareSpecification) if r.subject == subject.id]
    return {parameter.name: parameter for parameter in found.parameters}


def cited(data: bytes, evidence: EvidenceRef) -> bytes:
    (step,) = evidence.locator
    assert isinstance(step, ByteRange)
    return data[step.offset : step.offset + step.length]


def as_bytes(output: SourceOutput) -> bytes:
    rows = [record.to_json() for record in output.package_records()]
    return b"".join(canonical_json.dumps(row) + b"\n" for row in rows)


def unit(symbol: str) -> Any:
    return unit_from_json(symbol)


# --- Fixtures ----------------------------------------------------------------------------------


def test_the_committed_fixtures_are_what_the_generator_writes() -> None:
    for relative, data in _generator().build().items():
        assert fixture(relative) == data, relative
        assert len(data) < 512 * 1024


# --- Probe and inspect -------------------------------------------------------------------------


def probe(data: bytes, name: str = "f") -> tuple[float, list[str]]:
    result = UrdfAdapter().probe(data[:PROBE_HEAD_SIZE], ProbeHints(name, len(data)))
    return result.confidence, [reason.code for reason in result.reasons]


def test_a_robot_root_is_structure_whatever_the_name() -> None:
    for robot in ROBOTS:
        assert probe(fixture(f"robots/{robot}.urdf")) == (STRUCTURE, ["urdf.robot_root"])
    assert probe(fixture("renamed/robot_description")) == (STRUCTURE, ["urdf.robot_root"])
    xacro = fixture("xacro/quadrotor.urdf.xacro")
    assert probe(xacro, "q.txt") == (STRUCTURE, ["urdf.robot_root", "urdf.xacro"])


def test_the_prolog_is_skipped_to_find_the_root() -> None:
    head = (
        b'\xef\xbb\xbf<?xml version="1.0"?>\n<!-- a > comment -->\n'
        b'<!DOCTYPE robot [ <!ENTITY a "b"> ]>\n<robot name="r"/>'
    )
    assert probe(head)[0] == STRUCTURE
    assert probe(b"<!-- never closed")[0] == 0.0
    assert probe(b"<robotic/>")[0] == 0.0


def test_other_roots_are_not_claimed_unless_only_the_name_says_so() -> None:
    assert probe(fixture("corrupt/not_robot.urdf"), "model.sdf") == (0.0, ["urdf.not_robot"])
    assert probe(fixture("corrupt/not_robot.urdf"), "not_robot.urdf") == (
        NAME_ONLY,
        ["urdf.name_only"],
    )
    assert probe(b"\x00\x01binary", "arm.xacro")[0] == NAME_ONLY
    assert probe(b"", "empty.urdf")[0] == 0.0
    assert probe(b"PK\x03\x04", "archive.zip")[0] == 0.0


def test_a_description_is_selected_over_plain_text() -> None:
    registry = AdapterRegistry([TextAdapter(), UrdfAdapter()])
    data = fixture("renamed/robot_description")
    selection = registry.select(data[:PROBE_HEAD_SIZE], ProbeHints("robot_description", len(data)))
    assert selection.adapter == "urdf"


def test_inspect_summarises_from_the_head_only() -> None:
    class HeadOnly(BytesReader):
        def read(self, offset: int, length: int) -> bytes:
            assert offset + length <= PROBE_HEAD_SIZE
            return super().read(offset, length)

    source = HeadOnly(fixture("xacro/quadrotor.urdf.xacro") + b" " * (2 * PROBE_HEAD_SIZE))
    result = UrdfAdapter().inspect(source, configure(DESCRIPTOR))
    assert result.summary == {"root": "robot", "size": source.size, "xacro": True}


def test_the_plan_is_one_chunk_with_a_stable_id() -> None:
    source, config = BytesReader(fixture("robots/arm6.urdf")), configure(DESCRIPTOR)
    first, second = UrdfAdapter().plan(source, config), UrdfAdapter().plan(source, config)
    assert len(first.chunks) == 1 and first.chunks == second.chunks and not first.findings
    assert first.chunks[0].context == {"part": "description"}


# --- The robots against urdf_parser_py ---------------------------------------------------------


@pytest.mark.parametrize("robot", ROBOTS)
def test_topology_placements_axes_and_limits_match_the_official_reader(robot: str) -> None:
    oracle = json.loads(fixture(f"oracle/{robot}.json"))
    output = run(fixture(f"robots/{robot}.urdf"))
    assert not output.findings()
    check_against_oracle(output, oracle)


def check_against_oracle(output: SourceOutput, oracle: dict[str, Any]) -> None:
    (configuration,) = records(output, HardwareConfiguration)
    assert configuration.name == Known(oracle["name"])
    assert configuration.machine == NotCovered() and configuration.revision == NotCovered()
    links = components(output, ComponentCategory.LINK)
    assert sorted(links) == sorted(oracle["links"])
    frames = {frame.ref.frame_id for frame in records(output, Frame)}
    assert frames == set(oracle["links"])
    joints = components(output, ComponentCategory.JOINT)
    assert sorted(joints) == sorted(joint["name"] for joint in oracle["joints"])
    transforms = {t.child.frame_id: t for t in records(output, FrameTransform)}
    for joint in oracle["joints"]:
        component = joints[joint["name"]]
        assert component.frame == Known(links[joint["child"]].frame.known_or_raise())
        parameters = spec(output, component)
        assert parameters["type"].value == Known(joint["type"])
        assert parameters["parent/link"].value.known_or_raise() == joint["parent"]
        transform = transforms[joint["child"]]
        assert transform.parent.frame_id == joint["parent"]
        origin = joint["origin"] or {"xyz": [0.0, 0.0, 0.0], "rpy": [0.0, 0.0, 0.0]}
        assert isinstance(transform.value, Pose)
        assert transform.value.translation.values == tuple(origin["xyz"])
        assert transform.value.rotation.values == tuple(origin["rpy"])
        if joint["axis"] is None:
            assert "axis/xyz" not in parameters
        else:
            assert values(parameters["axis/xyz"]) == tuple(joint["axis"])
        limit = joint["limit"]
        if limit is None:
            assert not any(name.startswith("limit/") for name in parameters)
            continue
        for key in ("effort", "velocity", "lower", "upper"):
            if f"limit/{key}" in parameters:
                assert values(parameters[f"limit/{key}"]) == (float(limit[key]),)
            else:  # unstated: the reader applies the specification's default, Neptune does not
                assert limit[key] == 0


def values(parameter: DeclaredParameter) -> Any:
    assert isinstance(parameter.value, Known)
    return parameter.value.value


def test_the_quadrotor_xacro_reads_like_the_official_expansion() -> None:
    oracle = json.loads(fixture("oracle/quadrotor.xacro.json"))
    output = run(fixture("xacro/quadrotor.urdf.xacro"))
    assert not output.findings()
    check_against_oracle(output, oracle)


# --- What the records hold ---------------------------------------------------------------------


def test_every_record_cites_the_element_it_comes_from() -> None:
    data = fixture("robots/arm6.urdf")
    output = run(data)
    starts: dict[type, tuple[bytes, ...]] = {
        FrameGraph: (b"<robot ",),
        HardwareConfiguration: (b"<robot ",),
        Frame: (b"<link ",),
        FrameTransform: (b"<joint ",),
        DescriptionExtension: (b"<gazebo", b"<ros2_control "),
    }
    for record in output.records():
        text = cited(data, record.provenance.evidence)
        assert text.endswith(b">")
        if type(record) in starts:
            assert text.startswith(starts[type(record)]), (record.kind, text[:40])
        if isinstance(record, HardwareComponent):
            expected = {"link": b"<link ", "joint": b"<joint ", "actuator": b"<actuator "}
            assert text.startswith(expected.get(record.category, b"<sensor "))


def test_units_direction_and_rotation_cite_the_specification_through_the_robot_element() -> None:
    data = fixture("robots/diff_drive.urdf")
    output = run(data)
    (configuration,) = records(output, HardwareConfiguration)
    robot = configuration.provenance
    transforms = {t.child.frame_id: t for t in records(output, FrameTransform)}
    wheel = transforms["left_wheel_link"]
    assert wheel.direction == Known(TransformDirection.CHILD_TO_PARENT, robot)
    assert isinstance(wheel.value, Pose)
    assert wheel.value.translation.values == (0.0, 0.1755, 0.0508)
    assert wheel.value.translation.unit == Known(unit("m"), robot)
    rotation = wheel.value.rotation
    assert isinstance(rotation, EulerAngles)
    assert rotation.sequence == Known(EulerSequence.XYZ, robot)
    assert rotation.mode == Known(EulerMode.EXTRINSIC, robot)
    assert rotation.unit == Known(unit("rad"), robot)
    assert cited(data, wheel.provenance.evidence).startswith(b'<joint name="left_wheel_joint"')


def test_joint_detail_takes_its_units_from_the_joint_type() -> None:
    output = run(fixture("robots/diff_drive.urdf"))
    joints = components(output, ComponentCategory.JOINT)
    wheel = spec(output, joints["left_wheel_joint"])
    assert values(wheel["limit/effort"]) == (10.0,)
    assert wheel["limit/effort"].unit.known_or_raise() == unit("N.m")
    assert wheel["limit/velocity"].unit.known_or_raise() == unit("rad.s^-1")
    assert wheel["dynamics/damping"].unit.known_or_raise() == unit("N.m.s.rad^-1")
    assert wheel["dynamics/friction"].unit.known_or_raise() == unit("N.m")
    assert wheel["axis/xyz"].unit.known_or_raise() == unit("1")
    assert wheel["type"] == DeclaredParameter("type", Known("continuous"), NotApplicable())
    arm = run(fixture("robots/arm6.urdf"))
    lift = spec(arm, components(arm, ComponentCategory.JOINT)["shoulder_lift_joint"])
    assert lift["limit/lower"].unit.known_or_raise() == unit("rad")
    assert isinstance(lift["safety_controller/k_position"].unit, Unknown)


def test_links_hold_inertia_geometry_materials_and_mesh_references_verbatim() -> None:
    output = run(fixture("robots/arm6.urdf"))
    base = spec(output, components(output, ComponentCategory.LINK)["base_link"])
    assert values(base["inertial/mass/value"]) == (4.0,)
    assert base["inertial/mass/value"].unit.known_or_raise() == unit("kg")
    assert base["inertial/inertia/ixx"].unit.known_or_raise() == unit("kg.m^2")
    mesh = base["visual/0/geometry/mesh/filename"]
    assert values(mesh) == "package://arm6_description/meshes/visual/base_link.dae"
    assert isinstance(mesh.unit, NotApplicable)
    assert values(base["collision/0/geometry/mesh/scale"]) == (0.001, 0.001, 0.001)
    assert values(base["visual/0/material/name"]) == "grey"
    (configuration,) = records(output, HardwareConfiguration)
    robot = spec(output, configuration)
    assert values(robot["material/0/name"]) == "grey"
    assert values(robot["material/0/color/rgba"]) == (0.6, 0.6, 0.6, 1.0)
    texture = values(robot["material/1/texture/filename"])
    assert texture == "package://arm6_description/textures/carbon.png"


def test_sensors_are_placed_at_their_links() -> None:
    output = run(fixture("robots/diff_drive.urdf"))
    sensors = components(output, ComponentCategory.SENSOR)
    placed = {name: sensor.frame.known_or_raise().frame_id for name, sensor in sensors.items()}
    assert placed == {"front_lidar": "laser_link", "front_camera": "camera_link", "imu": "imu_link"}
    lidar = spec(output, sensors["front_lidar"])
    assert values(lidar["type"]) == "ray" and values(lidar["update_rate"]) == "10"
    assert values(lidar["ray/scan/horizontal/samples"]) == "360"
    assert values(lidar["plugin/0/filename"]) == "libgazebo_ros_ray_sensor.so"
    assert values(lidar["plugin/0/frame_name"]) == "laser_link"
    quad = run(fixture("robots/quadrotor.urdf"))
    camera = components(quad, ComponentCategory.SENSOR)["down_camera"]
    assert camera.frame.known_or_raise().frame_id == "base_link"
    detail = spec(quad, camera)
    assert values(detail["origin/xyz"]) == (0.0, 0.0, -0.05)
    assert detail["camera/image/hfov"].unit.known_or_raise() == unit("rad")
    assert detail["update_rate"].unit.known_or_raise() == unit("Hz")


def test_transmissions_give_actuators_and_extensions_stay_opaque() -> None:
    output = run(fixture("robots/arm6.urdf"))
    actuators = components(output, ComponentCategory.ACTUATOR)
    assert len(actuators) == 6
    motor = spec(output, actuators["elbow_motor"])
    assert values(motor["mechanicalReduction"]) == (101.0,)
    assert values(motor["transmission/joint/0/name"]) == "elbow_joint"
    assert values(motor["transmission/type"]) == "transmission_interface/SimpleTransmission"
    assert isinstance(actuators["elbow_motor"].frame, NotCovered)
    extensions = {(e.element, e.parameters[0].name) for e in records(output, DescriptionExtension)}
    assert ("ros2_control", "name") in extensions
    control = next(e for e in records(output, DescriptionExtension) if e.element == "ros2_control")
    found = {p.name: p.value for p in control.parameters}
    assert found["plugin/0"].known_or_raise() == "arm6_driver/Arm6HardwareInterface"
    assert found["type"].known_or_raise() == "system"


def test_a_description_names_no_machine() -> None:
    output = run(fixture("robots/arm6.urdf"))
    assert not records(output, Machine)
    (configuration,) = records(output, HardwareConfiguration)
    assert isinstance(configuration.machine, NotCovered)


# --- Xacro -------------------------------------------------------------------------------------


def test_a_xacro_records_its_expansion_and_cites_into_it() -> None:
    data = fixture("xacro/quadrotor.urdf.xacro")
    output = run(data)
    (expansion,) = records(output, DescriptionExpansion)
    root = parse(data, max_depth=64, max_elements=50_000)
    expanded = serialize(
        xacro_module.expand(root, max_depth=64, max_elements=50_000, max_chars=1 << 24).root
    )
    assert (expansion.digest, expansion.size) == (content_id(expanded), len(expanded))
    assert cited(data, expansion.provenance.evidence) == data
    arguments = {a.name: a.value for a in expansion.arguments}
    assert arguments["namespace"].known_or_raise() == "uav0"
    for record in output.records():
        if record is expansion:
            continue
        whole, step, inner = record.provenance.evidence.locator
        assert whole == ByteRange(0, len(data)) and step == EXPANSION_STEP
        assert isinstance(step, AdapterLocator) and isinstance(inner, ByteRange)
        text = expanded[inner.offset : inner.offset + inner.length]
        assert text.startswith(b"<") and text.endswith(b">")


def _assertion_kinds(value: Any) -> set[str]:
    if isinstance(value, dict):
        own = {value["assertion_kind"]} if "assertion_kind" in value else set()
        return own.union(*(_assertion_kinds(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_assertion_kinds(v) for v in value))
    return set()


@pytest.mark.parametrize("name", ["robots/arm6.urdf", "xacro/diff_drive.urdf.xacro"])
def test_what_the_file_declares_is_stated_and_only_the_expansion_is_observed(name: str) -> None:
    output = run(fixture(name))
    for record in output.records():
        data = record.to_json()
        if isinstance(record, DescriptionExpansion):
            assert data["provenance"]["assertion_kind"] == "observed"  # the adapter's digest
            assert _assertion_kinds(data["arguments"]) == {"stated"}
        else:
            assert _assertion_kinds(data) == {"stated"}, record.kind


def test_what_needs_ros_or_another_file_is_not_covered_never_guessed() -> None:
    output = run(fixture("xacro/diff_drive.urdf.xacro"))
    assert codes(output) == [
        "urdf.xacro_include_not_followed",
        "urdf.xacro_not_covered",
        "urdf.xacro_not_covered",
        "urdf.xacro_not_covered",
        "urdf.xacro_not_covered",
        "urdf.xacro_undefined",
        "urdf.xacro_undefined",
    ]
    links = components(output, ComponentCategory.LINK)
    assert sorted(links) == [
        "base_footprint",
        "base_link",
        "imu_link",
        "left_wheel_link",
        "right_wheel_link",
    ]
    wheel = spec(output, links["left_wheel_link"])
    assert isinstance(wheel["visual/0/geometry/mesh/filename"].value, NotCovered)
    assert values(wheel["collision/0/geometry/cylinder/radius"]) == (0.0508,)
    (expansion,) = records(output, DescriptionExpansion)
    arguments = {a.name: a.value for a in expansion.arguments}
    assert isinstance(arguments["serial_port"], NotCovered)
    # Declared empty: canonical text is never blank, so the stated "" is Unknown, not Known("").
    assert isinstance(arguments["prefix"], Unknown)
    assert arguments["use_sim"].known_or_raise() == "false"
    joints = components(output, ComponentCategory.JOINT)
    imu = spec(output, joints["imu_joint"])
    assert isinstance(imu["origin/xyz"].value, Unknown)  # ${caster_height} is undefined here
    assert "imu_link" not in {t.child.frame_id for t in records(output, FrameTransform)}
    (control,) = records(output, DescriptionExtension)
    found = {p.name: p.value for p in control.parameters}
    assert found["plugin/0"].known_or_raise() == "diffbot_hardware/DiffBotSystem"


def test_a_link_named_from_the_environment_has_a_frame_not_covered_too() -> None:
    data = (
        b'<robot name="r" xmlns:xacro="http://www.ros.org/wiki/xacro"><xacro:arg name="ns"/>'
        b'<link name="$(arg ns)base"/></robot>'
    )
    (link,) = records(run(data), HardwareComponent)
    assert isinstance(link.name, NotCovered) and isinstance(link.frame, NotCovered)


# --- Malformed input ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("corrupt/empty.urdf", "urdf.xml_malformed"),
        ("corrupt/truncated.urdf", "urdf.xml_malformed"),
        ("corrupt/invalid_utf8.urdf", "urdf.xml_malformed"),
        ("corrupt/latin1.urdf", "urdf.encoding_unsupported"),
        ("corrupt/not_robot.urdf", "urdf.not_robot"),
    ],
)
def test_an_unreadable_document_is_one_finding(name: str, code: str) -> None:
    output = run(fixture(name))
    assert not output.records()
    assert codes(output) == [code]


def test_bad_values_are_findings_and_the_rest_is_recorded() -> None:
    data = fixture("corrupt/bad_values.urdf")
    output = run(data)
    assert codes(output) == [
        "urdf.element_repeated",
        "urdf.joint_invalid",
        "urdf.link_undeclared",
        "urdf.name_repeated",
        "urdf.origin_invalid",
        "urdf.required_missing",
        "urdf.required_missing",
        "urdf.required_missing",
        "urdf.required_missing",
        "urdf.value_unparsable",
    ]
    links = [c for c in records(output, HardwareComponent) if c.category is ComponentCategory.LINK]
    assert len(links) == 4  # both base_links, arm, and the nameless one
    assert sum(isinstance(c.name, Unknown) for c in links) == 1
    joints = components(output, ComponentCategory.JOINT)
    slider = spec(output, joints["slider"])
    assert values(slider["limit/lower"]) == (NonFinite.NEGATIVE_INFINITY,)
    assert isinstance(slider["limit/effort"].value, Unknown)
    assert isinstance(slider["limit/velocity"].value, Unknown)
    assert slider["limit/lower"].unit.known_or_raise() == unit("m")
    assert "type" not in spec(output, joints["no_type"])  # absent, and reported
    children = {t.child.frame_id for t in records(output, FrameTransform)}
    assert children == {"arm"}  # no_type and slider; ghost has a nan origin, loop_joint a loop


# --- Hostile input -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("hostile/billion_laughs.urdf", "urdf.doctype_refused"),
        ("hostile/external_entity.urdf", "urdf.doctype_refused"),
        ("hostile/deep_nesting.urdf", "urdf.limit_exceeded"),
        ("hostile/huge_attribute.urdf", "urdf.limit_exceeded"),
        ("hostile/macro_bomb.urdf.xacro", "urdf.limit_exceeded"),
        ("hostile/recursive_macro.urdf.xacro", "urdf.limit_exceeded"),
    ],
)
def test_an_attack_is_one_finding_inside_the_limits(name: str, code: str) -> None:
    start = time.perf_counter()
    output = run(fixture(name))
    assert time.perf_counter() - start < 5
    assert not output.records()
    assert codes(output) == [code]


def test_hostile_expressions_are_refused_and_nothing_runs(tmp_path: Path) -> None:
    output = run(fixture("hostile/expression_attacks.urdf.xacro"))
    assert codes(output) == ["urdf.xacro_invalid"] * 4 + ["urdf.xacro_unsupported"] * 5
    assert not Path("/tmp/pwned").exists()
    links = components(output, ComponentCategory.LINK)
    assert values(spec(output, links["ok"])["note"]) == "1024"
    assert isinstance(spec(output, links["power"])["note"].value, Unknown)


def test_cycles_and_blowups_in_xacro_are_findings() -> None:
    circular = run(fixture("hostile/circular_property.urdf.xacro"))
    assert codes(circular) == ["urdf.xacro_invalid"]
    (link,) = components(circular, ComponentCategory.LINK).values()
    assert isinstance(spec(circular, link)["inertial/mass/value"].value, Unknown)
    blowup = run(fixture("hostile/property_blowup.urdf.xacro"))
    assert codes(blowup) == ["urdf.xacro_invalid"]
    included = run(fixture("hostile/self_include.urdf.xacro"))
    assert codes(included) == ["urdf.xacro_include_not_followed"] * 2


# --- Bounds --------------------------------------------------------------------------------------


def nested(depth: int) -> bytes:
    return b'<robot name="r"><link name="l"/>' + b"<a>" * depth + b"</a>" * depth + b"</robot>"


def test_depth_element_and_size_limits_are_exact() -> None:
    assert not run(nested(63), max_depth=64).findings()  # robot and 63 more: 64 levels
    assert codes(run(nested(64), max_depth=64)) == ["urdf.limit_exceeded"]
    links = b'<robot name="r">' + b'<link name="l%d"/>' % 0 + b"</robot>"
    assert not run(links, max_elements=2).findings()
    assert codes(run(links, max_elements=1)) == ["urdf.limit_exceeded"]
    data = fixture("robots/quadrotor.urdf")
    assert not run(data, max_bytes=len(data)).findings()
    assert codes(run(data, max_bytes=len(data) - 1)) == ["urdf.too_large"]


def test_an_expansion_is_bounded_against_its_source_exactly() -> None:
    calls = b"".join(b'<xacro:wheel n="%d"/>' % i for i in range(8))
    data = (
        b'<robot name="r" xmlns:xacro="http://www.ros.org/wiki/xacro">'
        b'<xacro:macro name="wheel" params="n"><link name="wheel_${n}" note="'
        + b"w" * 400
        + b'"/></xacro:macro>'
        + calls
        + b"</robot>"
    )
    (expansion,) = records(run(data, max_expansion_ratio=1000), DescriptionExpansion)
    assert expansion.size > 2 * len(data)  # a real amplification
    fits = -(-expansion.size // len(data))  # the smallest ratio that holds it
    assert not run(data, max_expansion_ratio=fits).findings()
    (finding,) = run(data, max_expansion_ratio=fits - 1).findings()
    assert finding.code == "urdf.limit_exceeded"
    assert finding.details["limit"] == (fits - 1) * len(data)
    assert "max_expansion_ratio" in finding.message
    # max_bytes still bounds it when it is the smaller.
    (finding,) = run(data, max_bytes=len(data), max_expansion_ratio=1000).findings()
    assert finding.code == "urdf.limit_exceeded" and "max_bytes" in finding.message


def name_bomb(body: bytes, levels: int) -> bytes:
    """``body`` doubled through ``levels`` nested macros: 2**(levels-1) copies of it."""
    macros = [b'<xacro:macro name="m0" params=""><link name="l"/>' + body + b"</xacro:macro>"]
    macros += [
        b'<xacro:macro name="m%d" params=""><xacro:m%d/><xacro:m%d/></xacro:macro>'
        % (level, level - 1, level - 1)
        for level in range(1, levels)
    ]
    return (
        b'<robot name="r" xmlns:xacro="http://www.ros.org/wiki/xacro">'
        + b"".join(macros)
        + b"<xacro:m%d/></robot>" % (levels - 1)
    )


@pytest.mark.parametrize(
    "body", [b"<t" + b"a" * 30_000 + b"/>", b"<x " + b"a" * 30_000 + b'="1"/>'], ids=["tag", "attr"]
)
def test_long_names_count_against_the_expansion_bound_and_memory_stays_near_it(body: bytes) -> None:
    import tracemalloc

    data = name_bomb(body, 12)  # 31 KB of source; unbounded, a 123 MB expansion
    tracemalloc.start()
    try:
        output = run(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert codes(output) == ["urdf.limit_exceeded"] and not output.records()
    bound = 64 * len(data)  # the default max_expansion_ratio's bound, about 2 MB
    assert peak < 4 * bound


def test_serialising_stops_as_soon_as_it_passes_its_budget() -> None:
    from neptune.adapters.urdf.xmltree import TooLarge

    root = parse(fixture("robots/arm6.urdf"), max_depth=64, max_elements=10_000)
    size = len(serialize(root))
    assert len(serialize(root, budget=size)) == size
    with pytest.raises(TooLarge):
        serialize(root, budget=size - 1)


def test_an_attribute_at_the_limit_is_read_and_one_past_it_is_not() -> None:
    from neptune.adapters.urdf.xmltree import MAX_VALUE_CHARS

    def robot(length: int) -> bytes:
        return b'<robot name="r"><link name="l" note="' + b"x" * length + b'"/></robot>'

    assert not run(robot(MAX_VALUE_CHARS)).findings()
    assert codes(run(robot(MAX_VALUE_CHARS + 1))) == ["urdf.limit_exceeded"]


def test_a_robot_with_no_link_records_no_configuration() -> None:
    output = run(b'<robot name="macros_only"/>')
    assert not output.records() and codes(output) == ["urdf.no_links"]


# --- Determinism and lineage -------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["robots/arm6.urdf", "xacro/quadrotor.urdf.xacro", "xacro/diff_drive.urdf.xacro"]
)
def test_ingesting_twice_gives_identical_bytes(name: str) -> None:
    assert as_bytes(run(fixture(name))) == as_bytes(run(fixture(name)))


def test_another_config_is_another_lineage() -> None:
    data = fixture("robots/diff_drive.urdf")
    first, second = run(data), run(data, max_depth=32)
    assert {r.id for r in first.records()}.isdisjoint({r.id for r in second.records()})

    def shape(output: SourceOutput) -> list[tuple[str, bytes]]:
        return sorted(
            (r.kind, canonical_json.dumps(r.provenance.evidence.to_json()))
            for r in output.records()
        )

    assert shape(first) == shape(second)


def test_every_record_reads_back_from_its_json() -> None:
    from neptune.model.kinds import RECORD_KINDS

    for name in ("robots/arm6.urdf", "xacro/diff_drive.urdf.xacro"):
        for record in run(fixture(name)).records():
            assert RECORD_KINDS[record.kind][1](record.to_json()) == record


def test_max_depth_is_never_more_than_the_ceiling() -> None:
    from neptune.adapters.urdf import MAX_DEPTH

    assert not run(nested(MAX_DEPTH - 1), max_depth=10_000).findings()
    assert codes(run(nested(MAX_DEPTH), max_depth=10_000)) == ["urdf.limit_exceeded"]


def test_a_long_text_run_is_refused_however_expat_cuts_it() -> None:
    from neptune.adapters.urdf.xmltree import MAX_VALUE_CHARS

    lines = ("y" * 99 + "\n") * (MAX_VALUE_CHARS // 100 + 1)
    data = b'<robot name="r"><link name="l"/><note>' + lines.encode() + b"</note></robot>"
    assert codes(run(data)) == ["urdf.limit_exceeded"]
    fits = ("y" * 99 + "\n") * (MAX_VALUE_CHARS // 100 - 1)
    data = b'<robot name="r"><link name="l"/><note>' + fits.encode() + b"</note></robot>"
    assert not run(data).findings()

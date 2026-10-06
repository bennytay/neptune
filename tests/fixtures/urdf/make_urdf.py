"""Generate the URDF and Xacro fixtures: three robots, two Xacro sources, corrupt and hostile files.

``python tests/fixtures/urdf/make_urdf.py`` rewrites every fixture from ``build()``; a test checks
the committed files are exactly what it gives. ``--oracle`` also runs the official readers, once,
outside the project (``uv run --no-project --with xacro --with urdf-parser-py``), and commits what
they read under ``oracle/``: ``urdf_parser_py``'s reading of each robot and real xacro's expansion
of ``xacro/quadrotor.urdf.xacro``. Tests compare the adapter against those files offline.

Every file is deterministic text, shaped like a real description, and far below 512 KB.
"""

import subprocess
import sys
from pathlib import Path
from typing import Final

HERE: Final = Path(__file__).parent
DECLARATION: Final = '<?xml version="1.0"?>\n'


def element(tag: str, attributes: dict[str, str] | None = None, *children: str) -> str:
    attrs = "".join(f' {k}="{v}"' for k, v in (attributes or {}).items())
    if not children:
        return f"<{tag}{attrs}/>"
    inner = "".join("\n" + "\n".join("  " + line for line in c.split("\n")) for c in children)
    return f"<{tag}{attrs}>{inner}\n</{tag}>"


def leaf(tag: str, text: str) -> str:
    return f"<{tag}>{text}</{tag}>"


def origin(xyz: str = "0 0 0", rpy: str = "0 0 0") -> str:
    return element("origin", {"xyz": xyz, "rpy": rpy})


def inertial(mass: str, xyz: str, inertia: tuple[str, str, str]) -> str:
    ixx, iyy, izz = inertia
    return element(
        "inertial",
        None,
        element("origin", {"xyz": xyz, "rpy": "0 0 0"}),
        element("mass", {"value": mass}),
        element(
            "inertia",
            {"ixx": ixx, "ixy": "0", "ixz": "0", "iyy": iyy, "iyz": "0", "izz": izz},
        ),
    )


def mesh(path: str, scale: str | None = None) -> str:
    attributes = {"filename": path, **({"scale": scale} if scale else {})}
    return element("geometry", None, element("mesh", attributes))


def joint(
    name: str,
    kind: str,
    parent: str,
    child: str,
    xyz: str,
    rpy: str,
    *extra: str,
) -> str:
    return element(
        "joint",
        {"name": name, "type": kind},
        element("parent", {"link": parent}),
        element("child", {"link": child}),
        origin(xyz, rpy),
        *extra,
    )


def robot(name: str, *parts: str) -> bytes:
    return (DECLARATION + element("robot", {"name": name}, *parts) + "\n").encode()


# --- A 6-DoF arm -------------------------------------------------------------------------------

ARM_JOINTS: Final = (
    # name, parent, child, xyz, rpy, lower, upper, effort, velocity, mass
    ("shoulder_pan", "base_link", "shoulder_link", "0 0 0.1625", "0 0 0", "-6.2832", "6.2832",
     "150.0", "3.1416", "3.761"),
    ("shoulder_lift", "shoulder_link", "upper_arm_link", "0 0 0", "1.570796327 0 0", "-6.2832",
     "6.2832", "150.0", "3.1416", "8.058"),
    ("elbow", "upper_arm_link", "forearm_link", "-0.425 0 0", "0 0 0", "-3.1416", "3.1416",
     "150.0", "3.1416", "2.846"),
    ("wrist_1", "forearm_link", "wrist_1_link", "-0.3922 0 0.1333", "0 0 0", "-6.2832", "6.2832",
     "28.0", "6.2832", "1.37"),
    ("wrist_2", "wrist_1_link", "wrist_2_link", "0 -0.0997 -2.044e-11", "1.570796327 0 0",
     "-6.2832", "6.2832", "28.0", "6.2832", "1.3"),
    ("wrist_3", "wrist_2_link", "wrist_3_link", "0 0.0996 -2.042e-11",
     "1.570796327 3.141592653589793 3.141592653589793", "-6.2832", "6.2832", "28.0", "6.2832",
     "0.365"),
)  # fmt: skip


def arm_link(name: str, mass: str) -> str:
    return element(
        "link",
        {"name": name},
        inertial(mass, "0 0 0.05", ("0.0102", "0.0102", "0.0067")),
        element(
            "visual",
            None,
            origin(),
            mesh(f"package://arm6_description/meshes/visual/{name}.dae"),
            element("material", {"name": "grey"}),
        ),
        element(
            "collision",
            None,
            origin(),
            mesh(f"package://arm6_description/meshes/collision/{name}.stl", "0.001 0.001 0.001"),
        ),
    )


def transmission(name: str) -> str:
    return element(
        "transmission",
        {"name": f"{name}_transmission"},
        leaf("type", "transmission_interface/SimpleTransmission"),
        element(
            "joint",
            {"name": f"{name}_joint"},
            leaf("hardwareInterface", "hardware_interface/PositionJointInterface"),
        ),
        element(
            "actuator",
            {"name": f"{name}_motor"},
            leaf("hardwareInterface", "hardware_interface/PositionJointInterface"),
            leaf("mechanicalReduction", "101"),
        ),
    )


def arm6() -> bytes:
    parts = [
        element("material", {"name": "grey"}, element("color", {"rgba": "0.6 0.6 0.6 1.0"})),
        element(
            "material",
            {"name": "carbon"},
            element("texture", {"filename": "package://arm6_description/textures/carbon.png"}),
        ),
        element("link", {"name": "world"}),
        joint("world_joint", "fixed", "world", "base_link", "0 0 0", "0 0 0"),
        arm_link("base_link", "4.0"),
    ]
    for name, parent, child, xyz, rpy, lower, upper, effort, velocity, mass in ARM_JOINTS:
        parts.append(arm_link(child, mass))
        parts.append(
            joint(
                f"{name}_joint",
                "revolute",
                parent,
                child,
                xyz,
                rpy,
                element("axis", {"xyz": "0 0 1"}),
                element(
                    "limit",
                    {"lower": lower, "upper": upper, "effort": effort, "velocity": velocity},
                ),
                element("dynamics", {"damping": "0.5", "friction": "0.1"}),
                element(
                    "safety_controller",
                    {
                        "soft_lower_limit": lower,
                        "soft_upper_limit": upper,
                        "k_position": "20",
                        "k_velocity": "0.5",
                    },
                ),
            )
        )
    parts += [
        element("link", {"name": "flange"}),
        joint("flange_joint", "fixed", "wrist_3_link", "flange", "0 0 0", "0 -1.5708 -1.5708"),
        element("link", {"name": "tool0"}),
        joint("tool0_joint", "fixed", "flange", "tool0", "0 0 0", "1.5708 0 1.5708"),
        element("link", {"name": "wrist_camera_link"}),
        joint(
            "wrist_camera_joint",
            "fixed",
            "wrist_3_link",
            "wrist_camera_link",
            "0 0.05 0.03",
            "0 0 0",
        ),
        *(transmission(name) for name, *_ in ARM_JOINTS),
        element(
            "ros2_control",
            {"name": "arm6_system", "type": "system"},
            element(
                "hardware",
                None,
                leaf("plugin", "arm6_driver/Arm6HardwareInterface"),
                element("param", {"name": "robot_ip"}).replace("/>", ">192.168.1.10</param>"),
            ),
            *(
                element(
                    "joint",
                    {"name": f"{name}_joint"},
                    element("command_interface", {"name": "position"}),
                    element("state_interface", {"name": "position"}),
                    element("state_interface", {"name": "velocity"}),
                )
                for name, *_ in ARM_JOINTS
            ),
        ),
        element(
            "gazebo",
            {"reference": "wrist_camera_link"},
            element(
                "sensor",
                {"name": "wrist_camera", "type": "camera"},
                leaf("always_on", "true"),
                leaf("update_rate", "30"),
                element(
                    "camera",
                    None,
                    leaf("horizontal_fov", "1.047"),
                    element(
                        "image",
                        None,
                        leaf("width", "640"),
                        leaf("height", "480"),
                        leaf("format", "R8G8B8"),
                    ),
                    element("clip", None, leaf("near", "0.05"), leaf("far", "3.0")),
                ),
                element(
                    "plugin",
                    {"name": "wrist_camera_controller", "filename": "libgazebo_ros_camera.so"},
                ),
            ),
        ),
        element(
            "gazebo",
            None,
            element(
                "plugin",
                {"name": "gazebo_ros2_control", "filename": "libgazebo_ros2_control.so"},
                leaf("parameters", "$(find arm6_description)/config/controllers.yaml"),
            ),
        ),
    ]
    return robot("arm6", *parts)


# --- A differential-drive base with sensors ----------------------------------------------------


def wheel(side: str, y: str) -> list[str]:
    name = f"{side}_wheel"
    return [
        element(
            "link",
            {"name": f"{name}_link"},
            inertial("0.6", "0 0 0", ("0.000466", "0.000466", "0.000774")),
            element(
                "visual",
                None,
                origin("0 0 0", "1.5708 0 0"),
                element(
                    "geometry", None, element("cylinder", {"radius": "0.0508", "length": "0.04"})
                ),
                element("material", {"name": "black"}),
            ),
            element(
                "collision",
                None,
                origin("0 0 0", "1.5708 0 0"),
                element(
                    "geometry", None, element("cylinder", {"radius": "0.0508", "length": "0.04"})
                ),
            ),
        ),
        joint(
            f"{name}_joint",
            "continuous",
            "base_link",
            f"{name}_link",
            f"0 {y} 0.0508",
            "0 0 0",
            element("axis", {"xyz": "0 1 0"}),
            element("limit", {"effort": "10.0", "velocity": "20.0"}),
            element("dynamics", {"damping": "0.01", "friction": "0.0"}),
        ),
    ]


def gazebo_sensor(reference: str, name: str, kind: str, rate: str, *body: str) -> str:
    return element(
        "gazebo",
        {"reference": reference},
        element(
            "sensor",
            {"name": name, "type": kind},
            leaf("always_on", "true"),
            leaf("update_rate", rate),
            leaf("pose", "0 0 0 0 0 0"),
            *body,
        ),
    )


def diff_drive() -> bytes:
    lidar = element(
        "ray",
        None,
        element(
            "scan",
            None,
            element(
                "horizontal",
                None,
                leaf("samples", "360"),
                leaf("resolution", "1"),
                leaf("min_angle", "-3.14159"),
                leaf("max_angle", "3.14159"),
            ),
        ),
        element(
            "range", None, leaf("min", "0.12"), leaf("max", "12.0"), leaf("resolution", "0.015")
        ),
    )
    parts = [
        element("material", {"name": "orange"}, element("color", {"rgba": "1.0 0.42 0.04 1.0"})),
        element("material", {"name": "black"}, element("color", {"rgba": "0 0 0 1"})),
        element("link", {"name": "base_footprint"}),
        joint("base_joint", "fixed", "base_footprint", "base_link", "0 0 0.0102", "0 0 0"),
        element(
            "link",
            {"name": "base_link"},
            inertial("6.0", "0 0 0.09", ("0.0642", "0.1044", "0.1440")),
            element(
                "visual",
                None,
                origin("0 0 0.09"),
                element("geometry", None, element("box", {"size": "0.42 0.31 0.18"})),
                element("material", {"name": "orange"}),
            ),
            element(
                "collision",
                None,
                origin("0 0 0.09"),
                element("geometry", None, element("box", {"size": "0.42 0.31 0.18"})),
            ),
        ),
        *wheel("left", "0.1755"),
        *wheel("right", "-0.1755"),
        element(
            "link",
            {"name": "caster_link"},
            element(
                "visual", None, element("geometry", None, element("sphere", {"radius": "0.025"}))
            ),
            element(
                "collision", None, element("geometry", None, element("sphere", {"radius": "0.025"}))
            ),
        ),
        joint("caster_joint", "fixed", "base_link", "caster_link", "-0.15 0 0.025", "0 0 0"),
        element(
            "link",
            {"name": "laser_link"},
            element(
                "visual",
                None,
                element(
                    "geometry", None, element("cylinder", {"radius": "0.035", "length": "0.04"})
                ),
            ),
        ),
        joint("laser_joint", "fixed", "base_link", "laser_link", "0.1 0 0.2", "0 0 0"),
        element("link", {"name": "imu_link"}),
        joint("imu_joint", "fixed", "base_link", "imu_link", "0 0 0.1", "0 0 0"),
        element(
            "link",
            {"name": "camera_link"},
            element(
                "visual",
                None,
                mesh("package://diffbot_description/meshes/d435.dae", "1 1 1"),
            ),
        ),
        joint("camera_joint", "fixed", "base_link", "camera_link", "0.2 0 0.15", "0 0.1 0"),
        element("link", {"name": "camera_optical_link"}),
        joint(
            "camera_optical_joint",
            "fixed",
            "camera_link",
            "camera_optical_link",
            "0 0 0",
            "-1.5708 0 -1.5708",
        ),
        element("gazebo", {"reference": "base_link"}, leaf("material", "Gazebo/Orange")),
        gazebo_sensor(
            "laser_link",
            "front_lidar",
            "ray",
            "10",
            lidar,
            element(
                "plugin",
                {"name": "lidar_driver", "filename": "libgazebo_ros_ray_sensor.so"},
                leaf("output_type", "sensor_msgs/LaserScan"),
                leaf("frame_name", "laser_link"),
            ),
        ),
        gazebo_sensor(
            "imu_link",
            "imu",
            "imu",
            "100",
            element("plugin", {"name": "imu_driver", "filename": "libgazebo_ros_imu_sensor.so"}),
        ),
        gazebo_sensor(
            "camera_link",
            "front_camera",
            "camera",
            "30",
            element(
                "camera",
                None,
                leaf("horizontal_fov", "1.211"),
                element("image", None, leaf("width", "848"), leaf("height", "480")),
            ),
            element("plugin", {"name": "camera_driver", "filename": "libgazebo_ros_camera.so"}),
        ),
        element(
            "gazebo",
            None,
            element(
                "plugin",
                {"name": "diff_drive", "filename": "libgazebo_ros_diff_drive.so"},
                leaf("left_joint", "left_wheel_joint"),
                leaf("right_joint", "right_wheel_joint"),
                leaf("wheel_separation", "0.351"),
                leaf("wheel_diameter", "0.1016"),
            ),
        ),
    ]
    return robot("diffbot", *parts)


# --- A quadrotor -------------------------------------------------------------------------------

ROTORS: Final = (  # index, x, y, turning direction
    ("0", "0.13", "-0.22", "ccw"),
    ("1", "-0.13", "0.2", "ccw"),
    ("2", "0.13", "0.22", "cw"),
    ("3", "-0.13", "-0.2", "cw"),
)


def quadrotor() -> bytes:
    parts = [
        element(
            "link",
            {"name": "base_link"},
            inertial("1.5", "0 0 0", ("0.029125", "0.029125", "0.055225")),
            element(
                "visual",
                None,
                mesh("package://quadrotor_description/meshes/iris.stl"),
            ),
            element(
                "collision",
                None,
                element("geometry", None, element("box", {"size": "0.47 0.47 0.11"})),
            ),
        ),
    ]
    for index, x, y, _ in ROTORS:
        parts.append(
            element(
                "link",
                {"name": f"rotor_{index}"},
                inertial("0.005", "0 0 0", ("9.75e-07", "4.17041e-05", "4.26041e-05")),
                element(
                    "visual",
                    None,
                    element(
                        "geometry", None, element("cylinder", {"radius": "0.1", "length": "0.005"})
                    ),
                ),
            )
        )
        parts.append(
            joint(
                f"rotor_{index}_joint",
                "continuous",
                "base_link",
                f"rotor_{index}",
                f"{x} {y} 0.023",
                "0 0 0",
                element("axis", {"xyz": "0 0 1"}),
            )
        )
    parts += [
        element("link", {"name": "imu_link"}),
        joint("imu_joint", "fixed", "base_link", "imu_link", "0 0 0", "0 0 0"),
        element(
            "sensor",
            {"name": "down_camera", "type": "camera", "update_rate": "20"},
            element("parent", {"link": "base_link"}),
            origin("0 0 -0.05", "0 1.5708 0"),
            element(
                "camera",
                None,
                element(
                    "image",
                    {
                        "width": "640",
                        "height": "480",
                        "format": "RGB8",
                        "hfov": "1.3962634",
                        "near": "0.01",
                        "far": "50.0",
                    },
                ),
            ),
        ),
        gazebo_sensor(
            "imu_link",
            "imu",
            "imu",
            "250",
            element("plugin", {"name": "imu_plugin", "filename": "libgazebo_imu_plugin.so"}),
        ),
        element(
            "gazebo",
            None,
            *(
                element(
                    "plugin",
                    {"name": f"rotor_{index}_model", "filename": "libgazebo_motor_model.so"},
                    leaf("jointName", f"rotor_{index}_joint"),
                    leaf("turningDirection", turning),
                    leaf("maxRotVelocity", "1100"),
                )
                for index, _, _, turning in ROTORS
            ),
        ),
    ]
    return robot("quadrotor", *parts)


# --- Xacro -------------------------------------------------------------------------------------

QUADROTOR_XACRO: Final = """<?xml version="1.0"?>
<robot xmlns:xacro="http://www.ros.org/wiki/xacro" name="quadrotor">
  <!-- Properties, arithmetic on constants and math functions. -->
  <xacro:property name="arm_x" value="0.13"/>
  <xacro:property name="arm_y" value="${arm_x * 1.6923}"/>
  <xacro:property name="rotor_radius" value="0.1"/>
  <xacro:property name="body_mass" value="1.5"/>
  <xacro:property name="tilt" value="${radians(5)}"/>
  <xacro:arg name="namespace" default="uav0"/>
  <xacro:arg name="with_camera" default="true"/>
  <xacro:property name="cylinder_inertia">
    <inertia ixx="9.75e-07" ixy="0" ixz="0" iyy="4.17041e-05" iyz="0" izz="4.26041e-05"/>
  </xacro:property>
  <xacro:macro name="rotor" params="index x y direction mass:=0.005 *joint_origin">
    <link name="rotor_${index}">
      <inertial>
        <origin xyz="0 0 0" rpy="0 0 0"/>
        <mass value="${mass}"/>
        <xacro:insert_block name="cylinder_inertia"/>
      </inertial>
      <visual>
        <geometry>
          <cylinder radius="${rotor_radius}" length="${rotor_radius / 20}"/>
        </geometry>
      </visual>
    </link>
    <joint name="rotor_${index}_joint" type="continuous">
      <parent link="base_link"/>
      <child link="rotor_${index}"/>
      <xacro:insert_block name="joint_origin"/>
      <xacro:if value="${direction == 'cw'}">
        <axis xyz="0 0 -1"/>
      </xacro:if>
      <xacro:unless value="${direction == 'cw'}">
        <axis xyz="0 0 1"/>
      </xacro:unless>
    </joint>
  </xacro:macro>
  <link name="base_link">
    <inertial>
      <origin xyz="0 0 0" rpy="0 0 0"/>
      <mass value="${body_mass}"/>
      <inertia ixx="${body_mass * (0.47**2 + 0.11**2) / 12}" ixy="0" ixz="0"
               iyy="${body_mass * (0.47**2 + 0.11**2) / 12}" iyz="0"
               izz="${body_mass * (0.47**2 + 0.47**2) / 12}"/>
    </inertial>
    <collision>
      <geometry>
        <box size="0.47 0.47 0.11"/>
      </geometry>
    </collision>
  </link>
  <xacro:rotor index="0" x="${arm_x}" y="${-arm_y}" direction="ccw">
    <origin xyz="${arm_x} ${-arm_y} 0.023" rpy="0 ${tilt} 0"/>
  </xacro:rotor>
  <xacro:rotor index="1" x="${-arm_x}" y="${arm_y}" direction="ccw">
    <origin xyz="${-arm_x} ${arm_y} 0.023" rpy="0 ${-tilt} 0"/>
  </xacro:rotor>
  <xacro:rotor index="2" x="${arm_x}" y="${arm_y}" direction="cw" mass="0.006">
    <origin xyz="${arm_x} ${arm_y} 0.023" rpy="0 ${tilt} 0"/>
  </xacro:rotor>
  <xacro:rotor index="3" x="${-arm_x}" y="${-arm_y}" direction="cw">
    <origin xyz="${-arm_x} ${-arm_y} 0.023" rpy="0 ${-tilt} 0"/>
  </xacro:rotor>
  <link name="$(arg namespace)_imu_link"/>
  <joint name="imu_joint" type="fixed">
    <parent link="base_link"/>
    <child link="$(arg namespace)_imu_link"/>
  </joint>
  <xacro:if value="$(arg with_camera)">
    <link name="camera_link"/>
    <joint name="camera_joint" type="fixed">
      <parent link="base_link"/>
      <child link="camera_link"/>
      <origin xyz="0.1 0 ${-0.05}" rpy="0 ${pi / 2} 0"/>
    </joint>
    <gazebo reference="camera_link">
      <sensor name="down_camera" type="camera">
        <update_rate>${10 * 2}</update_rate>
        <plugin name="camera_driver" filename="libgazebo_ros_camera.so"/>
      </sensor>
    </gazebo>
  </xacro:if>
  <gazebo>
    <plugin name="multirotor" filename="libgazebo_multirotor_base_plugin.so">
      <robotNamespace>$(arg namespace)</robotNamespace>
      <linkName>base_link</linkName>
    </plugin>
  </gazebo>
</robot>
"""

DIFF_DRIVE_XACRO: Final = """<?xml version="1.0"?>
<robot xmlns:xacro="http://www.ros.org/wiki/xacro" name="diffbot">
  <xacro:include filename="$(find diffbot_description)/urdf/sensors.xacro"/>
  <xacro:arg name="prefix" default=""/>
  <xacro:arg name="use_sim" default="false"/>
  <xacro:property name="wheel_radius" value="0.0508"/>
  <xacro:property name="wheel_separation" value="0.351"/>
  <xacro:property name="chassis" value="0.42 0.31 0.18"/>
  <xacro:macro name="wheel" params="side reflect *joint_origin">
    <link name="$(arg prefix)${side}_wheel_link">
      <visual>
        <origin xyz="0 0 0" rpy="${pi / 2} 0 0"/>
        <geometry>
          <mesh filename="$(find diffbot_description)/meshes/wheel.stl"/>
        </geometry>
      </visual>
      <collision>
        <origin xyz="0 0 0" rpy="${pi / 2} 0 0"/>
        <geometry>
          <cylinder radius="${wheel_radius}" length="0.04"/>
        </geometry>
      </collision>
    </link>
    <joint name="$(arg prefix)${side}_wheel_joint" type="continuous">
      <parent link="$(arg prefix)base_link"/>
      <child link="$(arg prefix)${side}_wheel_link"/>
      <xacro:insert_block name="joint_origin"/>
      <axis xyz="0 ${reflect} 0"/>
      <limit effort="10.0" velocity="${20.0 * wheel_radius / wheel_radius}"/>
    </joint>
  </xacro:macro>
  <link name="$(arg prefix)base_footprint"/>
  <link name="$(arg prefix)base_link">
    <visual>
      <geometry>
        <box size="${chassis}"/>
      </geometry>
    </visual>
  </link>
  <joint name="$(arg prefix)base_joint" type="fixed">
    <parent link="$(arg prefix)base_footprint"/>
    <child link="$(arg prefix)base_link"/>
    <origin xyz="0 0 ${wheel_radius - 0.0406}" rpy="0 0 0"/>
  </joint>
  <xacro:wheel side="left" reflect="1">
    <origin xyz="0 ${wheel_separation / 2} ${wheel_radius}" rpy="0 0 0"/>
  </xacro:wheel>
  <xacro:wheel side="right" reflect="-1">
    <origin xyz="0 ${-wheel_separation / 2} ${wheel_radius}" rpy="0 0 0"/>
  </xacro:wheel>
  <xacro:lidar_sensor parent="$(arg prefix)base_link" xyz="0.1 0 0.2"/>
  <link name="$(arg prefix)imu_link"/>
  <joint name="$(arg prefix)imu_joint" type="fixed">
    <parent link="$(arg prefix)base_link"/>
    <child link="$(arg prefix)imu_link"/>
    <origin xyz="0 0 ${caster_height}" rpy="0 0 0"/>
  </joint>
  <xacro:arg name="serial_port"/>
  <ros2_control name="diffbot_system" type="system">
    <hardware>
      <xacro:if value="$(arg use_sim)">
        <plugin>gazebo_ros2_control/GazeboSystem</plugin>
      </xacro:if>
      <xacro:unless value="$(arg use_sim)">
        <plugin>diffbot_hardware/DiffBotSystem</plugin>
        <param name="serial_port">$(arg serial_port)</param>
        <param name="home">$(env HOME)</param>
      </xacro:unless>
    </hardware>
  </ros2_control>
</robot>
"""


# --- Corrupt and hostile -----------------------------------------------------------------------

BAD_VALUES: Final = """<?xml version="1.0"?>
<robot name="bad_values">
  <link name="base_link">
    <inertial>
      <mass value="heavy"/>
      <inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0" izz="1"/>
    </inertial>
    <inertial>
      <mass value="2.0"/>
    </inertial>
    <visual>
      <geometry>
        <box size="0.1 0.1"/>
      </geometry>
    </visual>
  </link>
  <link name="base_link"/>
  <link name="arm"/>
  <link/>
  <joint name="no_type">
    <parent link="base_link"/>
    <child link="arm"/>
  </joint>
  <joint name="ghost_joint" type="revolute">
    <parent link="base_link"/>
    <child link="ghost"/>
    <origin xyz="0 0 nan" rpy="0 0 0"/>
  </joint>
  <joint name="loop_joint" type="fixed">
    <parent link="arm"/>
    <child link="arm"/>
  </joint>
  <joint name="slider" type="prismatic">
    <parent link="base_link"/>
    <child link="arm"/>
    <origin xyz="0 0 1e3" rpy="0 0 0"/>
    <limit lower="-inf" upper="0.5" effort="0x10" velocity=""/>
  </joint>
  <transmission name="no_actuator">
    <type>transmission_interface/SimpleTransmission</type>
  </transmission>
</robot>
"""

BILLION_LAUGHS: Final = """<?xml version="1.0"?>
<!DOCTYPE robot [
  <!ENTITY lol "lol">
  <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
  <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
  <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
  <!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">
  <!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">
  <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">
]>
<robot name="&lol9;">
  <link name="base_link"/>
</robot>
"""

EXTERNAL_ENTITY: Final = """<?xml version="1.0"?>
<!DOCTYPE robot [
  <!ENTITY secret SYSTEM "file:///etc/passwd">
]>
<robot name="leak">
  <link name="&secret;"/>
</robot>
"""

MACRO_BOMB: Final = (
    '<?xml version="1.0"?>\n<robot xmlns:xacro="http://www.ros.org/wiki/xacro" name="bomb">\n'
    '  <xacro:macro name="m0"><link name="l"/></xacro:macro>\n'
    + "".join(
        f'  <xacro:macro name="m{level}">' + f"<xacro:m{level - 1}/>" * 10 + "</xacro:macro>\n"
        for level in range(1, 10)
    )
    + '  <link name="base_link"/>\n  <xacro:m9/>\n</robot>\n'
)

RECURSIVE_MACRO: Final = """<?xml version="1.0"?>
<robot xmlns:xacro="http://www.ros.org/wiki/xacro" name="recursion">
  <xacro:macro name="forever" params="n">
    <xacro:forever n="${n + 1}"/>
  </xacro:macro>
  <link name="base_link"/>
  <xacro:forever n="0"/>
</robot>
"""

CIRCULAR_PROPERTY: Final = """<?xml version="1.0"?>
<robot xmlns:xacro="http://www.ros.org/wiki/xacro" name="circular">
  <xacro:property name="a" value="${b + 1}"/>
  <xacro:property name="b" value="${a + 1}"/>
  <link name="base_link">
    <inertial>
      <mass value="${a}"/>
    </inertial>
  </link>
</robot>
"""

EXPRESSION_ATTACKS: Final = (
    """<?xml version="1.0"?>
<robot xmlns:xacro="http://www.ros.org/wiki/xacro" name="attacks">
  <link name="base_link"/>
  <link name="power" note="${9**9**9}"/>
  <link name="repeat" note="${'a' * 10**9}"/>
  <link name="dunder" note="${__import__('os').system('touch /tmp/pwned')}"/>
  <link name="class" note="${().__class__.__bases__[0].__subclasses__()}"/>
  <link name="lambda" note="${(lambda: 1)()}"/>
  <link name="comprehension" note="${[x for x in range(10**9)]}"/>
  <link name="parens" note="${"""
    + "(" * 400
    + "1"
    + ")" * 400
    + """}"/>
  <link name="sum" note="${"""
    + "+".join(["1"] * 600)
    + """}"/>
  <link name="divide" note="${1 / 0}"/>
  <link name="ok" note="${2 ** 10}"/>
</robot>
"""
)

SELF_INCLUDE: Final = """<?xml version="1.0"?>
<robot xmlns:xacro="http://www.ros.org/wiki/xacro" name="self_include">
  <xacro:include filename="self_include.urdf.xacro"/>
  <xacro:include filename="../../../../etc/passwd"/>
  <link name="base_link"/>
</robot>
"""

PROPERTY_BLOWUP: Final = (
    '<?xml version="1.0"?>\n<robot xmlns:xacro="http://www.ros.org/wiki/xacro" name="blowup">\n'
    '  <xacro:property name="p0" value="aaaaaaaaaaaaaaaa"/>\n'
    + "".join(
        f'  <xacro:property name="p{n}" value="${{p{n - 1}}}${{p{n - 1}}}"/>\n'
        for n in range(1, 24)
    )
    + '  <link name="base_link" note="${p23}"/>\n</robot>\n'
)


NOT_ROBOT: Final = '<sdf version="1.9"><model name="m"/></sdf>\n'
LATIN1: Final = '<robot name="r\xe9"><link name="a"/></robot>\n'


def deep_nesting(depth: int) -> bytes:
    return (
        DECLARATION + '<robot name="deep"><link name="base_link"/>' + "<a>" * depth
        + "</a>" * depth + "</robot>\n"
    ).encode()  # fmt: skip


def build() -> dict[str, bytes]:
    """Every fixture, by path relative to this directory."""
    arm, base, quad = arm6(), diff_drive(), quadrotor()
    flipped = bytearray(base)
    flipped[base.index(b"left_wheel_link") + 2] = 0xFF
    files = {
        "robots/arm6.urdf": arm,
        "robots/diff_drive.urdf": base,
        "robots/quadrotor.urdf": quad,
        "renamed/robot_description": base,
        "xacro/quadrotor.urdf.xacro": QUADROTOR_XACRO.encode(),
        "xacro/diff_drive.urdf.xacro": DIFF_DRIVE_XACRO.encode(),
        "corrupt/empty.urdf": b"",
        "corrupt/truncated.urdf": base[: len(base) * 6 // 10],
        "corrupt/invalid_utf8.urdf": bytes(flipped),
        "corrupt/not_robot.urdf": (DECLARATION + NOT_ROBOT).encode(),
        "corrupt/latin1.urdf": ('<?xml version="1.0" encoding="ISO-8859-1"?>\n' + LATIN1).encode(
            "latin-1"
        ),
        "corrupt/bad_values.urdf": BAD_VALUES.encode(),
        "hostile/billion_laughs.urdf": BILLION_LAUGHS.encode(),
        "hostile/external_entity.urdf": EXTERNAL_ENTITY.encode(),
        "hostile/deep_nesting.urdf": deep_nesting(5000),
        "hostile/huge_attribute.urdf": (
            DECLARATION + '<robot name="huge"><link name="' + "x" * 100_000 + '"/></robot>\n'
        ).encode(),
        "hostile/macro_bomb.urdf.xacro": MACRO_BOMB.encode(),
        "hostile/recursive_macro.urdf.xacro": RECURSIVE_MACRO.encode(),
        "hostile/circular_property.urdf.xacro": CIRCULAR_PROPERTY.encode(),
        "hostile/expression_attacks.urdf.xacro": EXPRESSION_ATTACKS.encode(),
        "hostile/self_include.urdf.xacro": SELF_INCLUDE.encode(),
        "hostile/property_blowup.urdf.xacro": PROPERTY_BLOWUP.encode(),
    }
    return files


def run_oracle() -> None:
    """Run the official readers outside the project and commit what they read (``oracle/``)."""
    command = [
        "uv", "run", "--no-project", "--with", "xacro==2.1.1", "--with", "urdf-parser-py==0.0.4",
        "python", str(HERE / "urdf_oracle.py"), str(HERE),
    ]  # fmt: skip
    subprocess.run(command, check=True, cwd=HERE)


if __name__ == "__main__":
    for relative, data in build().items():
        path = HERE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if "--oracle" in sys.argv:
        run_oracle()

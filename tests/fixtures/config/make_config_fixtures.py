"""Write the configuration fixtures: real-world-shaped robot configs and their corruptions.

Run ``uv run python tests/fixtures/config/make_config_fixtures.py``; it rewrites every file below
from the text in this script, byte for byte, so a fixture's bytes are reviewable here. Each file
is small (all of them together are under 100 KB). ``README.md`` says what each one exercises.
"""

import codecs
import json
from pathlib import Path
from typing import Final

HERE: Final = Path(__file__).parent

# --- ROS 2: Nav2 parameters, as a robot's bringup package ships them ---------------------------

NAV2: Final = """\
# Nav2 parameters for the AMR-7 field robot (bringup/params/nav2_params.yaml).
amcl:
  ros__parameters:
    use_sim_time: False
    alpha1: 0.2
    alpha2: 0.2
    alpha3: 0.2
    alpha4: 0.2
    alpha5: 0.2
    base_frame_id: "base_footprint"
    beam_skip_distance: 0.5
    do_beamskip: false
    global_frame_id: "map"
    laser_likelihood_max_dist: 2.0
    laser_max_range: 100.0
    laser_min_range: -1.0
    laser_model_type: "likelihood_field"
    max_beams: 60
    max_particles: 2000
    min_particles: 500
    odom_frame_id: "odom"
    pf_err: 0.05
    pf_z: 0.99
    resample_interval: 1
    robot_model_type: "nav2_amcl::DifferentialMotionModel"
    save_pose_rate: 0.5
    tf_broadcast: true
    transform_tolerance: 1.0
    update_min_a: 0.2
    update_min_d: 0.25
    scan_topic: scan

controller_server:
  ros__parameters:
    use_sim_time: False
    controller_frequency: 20.0
    min_x_velocity_threshold: 0.001
    min_theta_velocity_threshold: 0.001
    failure_tolerance: 0.3
    progress_checker_plugin: "progress_checker"
    goal_checker_plugins: ["general_goal_checker"] # "precise_goal_checker"
    controller_plugins: ["FollowPath"]

    # Progress checker parameters
    progress_checker:
      plugin: "nav2_controller::SimpleProgressChecker"
      required_movement_radius: 0.5
      movement_time_allowance: 10.0
    general_goal_checker:
      stateful: True
      plugin: "nav2_controller::SimpleGoalChecker"
      xy_goal_tolerance: 0.25
      yaw_goal_tolerance: 0.25
    FollowPath:
      plugin: "dwb_core::DWBLocalPlanner"
      min_vel_x: 0.0
      max_vel_x: 0.26
      max_vel_theta: 1.0
      acc_lim_x: 2.5
      acc_lim_theta: 3.2
      decel_lim_x: -2.5
      decel_lim_theta: -3.2
      vx_samples: 20
      vtheta_samples: 20
      sim_time: 1.7
      transform_tolerance: 0.2
      critics: ["RotateToGoal", "Oscillation", "BaseObstacle", "GoalAlign", "PathAlign"]
      BaseObstacle.scale: 0.02
      PathAlign.scale: 32.0
      GoalAlign.scale: 24.0
      RotateToGoal.scale: 32.0
      RotateToGoal.lookahead_time: -1.0

local_costmap:
  local_costmap:
    ros__parameters:
      update_frequency: 5.0
      publish_frequency: 2.0
      global_frame: odom
      robot_base_frame: base_link
      rolling_window: true
      width: 3
      height: 3
      resolution: 0.05
      robot_radius: 0.22
      plugins: ["voxel_layer", "inflation_layer"]
      inflation_layer:
        plugin: "nav2_costmap_2d::InflationLayer"
        cost_scaling_factor: 3.0
        inflation_radius: 0.55
      voxel_layer:
        plugin: "nav2_costmap_2d::VoxelLayer"
        enabled: True
        z_voxels: 16
        observation_sources: scan
        scan:
          topic: /scan
          max_obstacle_height: 2.0
          clearing: True
          marking: True
          data_type: "LaserScan"
      always_send_full_costmap: True
"""

# Two runs' controller parameters: run B retunes the planner, reorders and requotes keys, and
# rewrites comments. What changed is what compare_configurations must find, and nothing else.
RUN_A: Final = """\
# Field trial 2026-09-28, run A.
controller_server:
  ros__parameters:
    use_sim_time: False
    controller_frequency: 20.0
    FollowPath:
      plugin: "dwb_core::DWBLocalPlanner"
      max_vel_x: 0.26
      max_vel_theta: 1.0
      sim_time: 1.7
      critics: ["RotateToGoal", "Oscillation", "BaseObstacle"]
      debug_trajectory_details: True
"""
RUN_B: Final = """\
# Field trial 2026-09-28, run B: faster planner.
controller_server:
  ros__parameters:
    controller_frequency: 20.0   # unchanged, moved
    use_sim_time: false
    FollowPath:
      plugin: dwb_core::DWBLocalPlanner
      max_vel_x: 0.31
      max_vel_theta: 1.0
      sim_time: 1.5
      critics: ["RotateToGoal", "BaseObstacle", "Oscillation"]
      xy_goal_tolerance: 0.25
"""

# --- PX4: a vehicle's parameter snapshot as JSON --------------------------------------------------

PX4_PARAMETERS: Final = (
    ("BAT1_N_CELLS", "INT32", 4),
    ("BAT1_V_CHARGED", "FLOAT", 4.05),
    ("BAT1_V_EMPTY", "FLOAT", 3.5),
    ("CAL_ACC0_ID", "INT32", 1310988),
    ("CAL_ACC0_XOFF", "FLOAT", 0.0123),
    ("CAL_ACC0_YOFF", "FLOAT", -0.0071),
    ("CAL_ACC0_ZOFF", "FLOAT", 0.1432),
    ("CAL_GYRO0_ID", "INT32", 2359306),
    ("CAL_MAG0_ID", "INT32", 396825),
    ("CAL_MAG0_ROT", "INT32", 0),
    ("COM_ARM_WO_GPS", "INT32", 1),
    ("COM_DISARM_LAND", "FLOAT", 2.0),
    ("COM_OBL_RC_ACT", "INT32", 0),
    ("COM_RC_LOSS_T", "FLOAT", 0.5),
    ("EKF2_BARO_DELAY", "FLOAT", 0.0),
    ("EKF2_GPS_DELAY", "FLOAT", 110.0),
    ("EKF2_HGT_REF", "INT32", 1),
    ("EKF2_IMU_POS_X", "FLOAT", 0.0),
    ("EKF2_IMU_POS_Y", "FLOAT", 0.0),
    ("EKF2_IMU_POS_Z", "FLOAT", 0.0),
    ("MC_PITCHRATE_P", "FLOAT", 0.15),
    ("MC_PITCH_P", "FLOAT", 6.5),
    ("MC_ROLLRATE_P", "FLOAT", 0.15),
    ("MC_ROLL_P", "FLOAT", 6.5),
    ("MC_YAWRATE_P", "FLOAT", 0.2),
    ("MPC_TILTMAX_AIR", "FLOAT", 45.0),
    ("MPC_XY_VEL_MAX", "FLOAT", 12.0),
    ("MPC_Z_VEL_MAX_DN", "FLOAT", 1.5),
    ("MPC_Z_VEL_MAX_UP", "FLOAT", 3.0),
    ("NAV_DLL_ACT", "INT32", 0),
    ("NAV_RCL_ACT", "INT32", 2),
    ("RTL_RETURN_ALT", "FLOAT", 60.0),
    ("SENS_BOARD_ROT", "INT32", 0),
    ("SYS_AUTOSTART", "INT32", 4001),
    *((f"PWM_MAIN_FUNC{i}", "INT32", 100 + i) for i in range(1, 9)),
    *((f"PWM_MAIN_MIN{i}", "INT32", 1000) for i in range(1, 9)),
    *((f"PWM_MAIN_MAX{i}", "INT32", 2000) for i in range(1, 9)),
)


def px4_params() -> str:
    document = {
        "vehicle": {
            "autopilot": "PX4",
            "airframe": 4001,
            "firmware": "v1.14.3",
            "sys_id": 1,
            "comp_id": 1,
        },
        "parameters": [
            {"name": name, "type": kind, "value": value} for name, kind, value in PX4_PARAMETERS
        ],
    }
    return json.dumps(document, indent=2) + "\n"


# --- TOML: an end effector's tool configuration ------------------------------------------------

TOOL: Final = """\
# End-effector configuration for the UR5e pick cell (tool changer slot 2).
name = "Robotiq 2F-85"
serial = "2F85-SN-0047"
slot = 2

[mount]
frame = "tool0"
offset_xyz = [0.0, 0.0, 0.1493]   # metres, as the vendor sheet states
offset_rpy = [0.0, 0.0, 1.5707963]

[limits]
stroke = 0.085
force = { min = 20.0, max = 235.0 }
speed = { min = 0.02, max = 0.15 }

[calibration]
performed = 2026-08-14T09:30:00+08:00
checked = 2026-09-01 07:15:00
operator = "kt"
zero_offset = -4e-4
valid_until = 2027-02-14
shift_start = 07:30:00

[[fingertips]]
name = "standard"
material = "silicone"
width_mm = 22

[[fingertips]]
name = "narrow"
material = "polyurethane"
width_mm = 0x0C

[controller]
ip = "192.168.1.11"
port = 63_352
modbus = true
"literal key" = 'C:\\tools\\robotiq'
note = \"\"\"
Keep the "fingertips" clean.\"\"\"
"""

# --- YAML corners -------------------------------------------------------------------------------

YAML_TYPES: Final = """\
# Plain scalars that YAML 1.1 and YAML 1.2 read differently, and ones they agree on.
switches:
  motors: on           # 1.1 bool, 1.2 text
  lights: off
  armed: yes
  answer: n
permissions: 0755      # 1.1 octal 493, 1.2 decimal 755
octal_12: 0o17         # 1.1 text, 1.2 octal 15
binary: 0b1010         # 1.1 int 10, 1.2 text
grouped: 1_000         # 1.1 int, 1.2 text
sexagesimal: 1:30      # 1.1 int 90, 1.2 text
scientific: 1e3        # 1.1 text, 1.2 float
signed_exp: 1.0e+3     # both floats
calibrated: 2026-09-01 # 1.1 local date, 1.2 text
stamp: 2026-09-01T07:15:00Z
agree:
  int: 42
  negative: -17
  hex: 0x1F
  float: 0.25
  inf: .inf
  nan: .NaN
  bool: true
  null_word: null
  tilde: ~
  empty:
  text: base_link
quoted:
  single: 'on'
  double: "0755"
  folded: >
    two
    lines
  literal: |
    kept
    as is
tagged:
  str: !!str 0755
  int: !!int "42"
  float: !!float 0.5
  bool: !!bool true
  null: !!null ~
  binary: !!binary aGVsbG8gcm9ib3Q=
  timestamp: !!timestamp 2001-12-14t21:59:43.10-05:00
  application: !include other.yaml
  bad_int: !!int forty-two
"""

ANCHORS: Final = """\
defaults: &defaults
  rate: 50.0
  frame: base_link
left_arm:
  <<: *defaults
  joint_count: 6
right_arm:
  <<: *defaults
  rate: 25.0
same: *defaults
dangling: *nowhere
? [a, b]
: complex key
*defaults : alias of a mapping as a key
"""

MULTI: Final = """\
%YAML 1.1
---
# Document 0: YAML 1.1 declared.
gripper: on
mode: 0755
...
%YAML 1.2
---
# Document 1: YAML 1.2 declared.
gripper: on
mode: 0755
---
# Document 2: no version declared.
- on
- 0755
- plain
"""

BILLION_LAUGHS: Final = """\
a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]
b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]
c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]
d: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c]
e: &e [*d,*d,*d,*d,*d,*d,*d,*d,*d]
f: &f [*e,*e,*e,*e,*e,*e,*e,*e,*e]
g: &g [*f,*f,*f,*f,*f,*f,*f,*f,*f]
h: &h [*g,*g,*g,*g,*g,*g,*g,*g,*g]
i: &i [*h,*h,*h,*h,*h,*h,*h,*h,*h]
"""

DUPLICATES_JSON: Final = '{"rate": 10, "frame": "map", "rate": 20, "nested": {"x": 1, "x": 2}}\n'
DUPLICATES_YAML: Final = "rate: 10\nframe: map\nrate: 20\nnested:\n  x: 1\n  x: 2\n"
DUPLICATES_TOML: Final = 'rate = 10\nframe = "map"\nrate = 20\n'

NONFINITE: Final = (
    '{"nan": NaN, "inf": Infinity, "ninf": -Infinity, "overflow": 1e400,'
    ' "underflow": 1e-400, "negative_zero": -0.0, "exact": 1.0,'
    ' "big": ' + "9" * 5000 + ', "surrogate": "\\ud800", "\\udfff": "key"}\n'
)


def files() -> dict[str, bytes]:
    nav2 = NAV2.encode()
    px4 = px4_params().encode()
    tool = TOOL.encode()
    cut = nav2.index(b'"Oscillation"') + 6
    return {
        # Real-world shaped, valid.
        "nav2_params.yaml": nav2,
        "px4_params.json": px4,
        "gripper_tool.toml": tool,
        "run_a_params.yaml": RUN_A.encode(),
        "run_b_params.yaml": RUN_B.encode(),
        # YAML's own corners.
        "yaml_types.yaml": YAML_TYPES.encode(),
        "anchors.yaml": ANCHORS.encode(),
        "multi_document.yaml": MULTI.encode(),
        # Renamed: no extension, a UTF-8 byte-order mark and CR LF line breaks.
        "robot_config": codecs.BOM_UTF8 + RUN_A.replace("\n", "\r\n").encode(),
        "utf16.yaml": codecs.BOM_UTF16_LE + RUN_A.encode("utf-16-le"),
        "mixed_endings.yaml": b"a: 1\r\nb: 2\nc: 3\rd: 4\n",
        # Truncated, corrupted, empty.
        "truncated_px4.json": px4[: len(px4) * 3 // 5],
        "truncated_nav2.yaml": nav2[:cut],
        "corrupted_tool.toml": tool.replace(b"max = 235.0 }", b"max = }"),
        "corrupted_nav2.yaml": nav2.replace(b"odom_frame_id", b"odom_\xff_frame_id"),
        "empty.yaml": b"",
        "comments_only.toml": b"# Nothing configured here.\n# Still nothing.\n",
        # Hostile.
        "duplicates.json": DUPLICATES_JSON.encode(),
        "duplicates.yaml": DUPLICATES_YAML.encode(),
        "duplicates.toml": DUPLICATES_TOML.encode(),
        "billion_laughs.yaml": BILLION_LAUGHS.encode(),
        "deep.json": b"[" * 1000 + b"]" * 1000 + b"\n",
        "deep.yaml": b"".join(b" " * i + b"k:\n" for i in range(300)) + b" " * 300 + b"v\n",
        "huge_scalar.yaml": b"note: " + b"x" * 100_000 + b"\nrate: 1\n",
        "nonfinite.json": NONFINITE.encode(),
    }


if __name__ == "__main__":
    for name, data in files().items():
        (HERE / name).write_bytes(data)

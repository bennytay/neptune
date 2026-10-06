# Calibration fixtures

Files for the `calibration` adapter (`neptune.adapters.calibration`, ADR 0055, 0073). Written by
hand in the shapes the tools write them (easy_handeye's `yaml.dump`, easy_handeye2's
`message_to_yaml`, MoveIt Calibration's `saveCameraPoseBtnClicked`, read from their sources); the
numbers are plausible, not measured. Embodiments:

| File | Embodiment | Format |
|---|---|---|
| `wrist_camera_info.yaml` | manipulator wrist camera | ROS `camera_info` YAML |
| `lidar_camera_autoware.yml` | mobile-base lidar-camera extrinsic | OpenCV FileStorage YAML (Autoware names) |
| `quadruped_camchain_imucam.yaml`, `quadruped_imu.yaml`, `quadruped_imu_calibrated.yaml` | quadruped head IMU-camera rig | Kalibr |
| `rov_camera.xml` | marine ROV fisheye camera | OpenCV FileStorage XML |
| `aerial_camera_info.json` | aerial gimbal camera | `sensor_msgs/CameraInfo` as JSON |
| `stereo_unnamed_extrinsics.yml` | stereo rig, `R`/`T` without frame names | OpenCV FileStorage YAML |
| `chain_missing_extrinsics.yaml` | camera chain with gaps | Kalibr |
| `arm_wrist_easy_handeye.yaml` | arm wrist camera, eye-in-hand | easy_handeye 0.3+ (`parameters`) |
| `humanoid_head_easy_handeye_legacy.yaml` | humanoid head camera, eye-on-base | easy_handeye before 0.3 (flat) |
| `mobile_manipulator_easy_handeye2.calib` | mobile-manipulator base camera, eye-on-base | easy_handeye2 (ROS 2) |
| `arm_wrist_moveit_camera_pose.launch` | arm wrist camera, eye-in-hand | MoveIt Calibration launch |
| `arm_wrist_opencv_handeye.yml` | arm wrist camera with `camera_name` and an ISO time | OpenCV FileStorage YAML |
| `legged_head_opencv_sample.yml` | legged robot head camera, C-locale `%c` time | OpenCV `calibration.cpp` output |
| `invented_handeye_shape.yaml` | the MVL-181 corpus's invented hand-eye shape: not claimed | none |

Corrupt and hostile: `truncated_camchain.yaml`, `empty.yaml`, `alias_bomb_camera_info.yaml`,
`nan_camera_info.yaml`, `short_matrix_camera_info.yaml`, `rov_dtd.xml`, `rov_truncated.xml`.
Larger and nested inputs, and malformed, partial, NaN and non-unit hand-eye results, are generated
in the tests from the files above.

The OpenCV files are accepted by OpenCV 4's own `cv2.FileStorage` (checked with
`uv run --no-project --with opencv-python-headless --with numpy`), which is the oracle for the format.

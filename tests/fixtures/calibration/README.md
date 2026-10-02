# Calibration fixtures

Files for the `calibration` adapter (`neptune.adapters.calibration`, ADR 0055). Written by hand in
the shapes the tools write them; the numbers are plausible, not measured. Four embodiments:

| File | Embodiment | Format |
|---|---|---|
| `wrist_camera_info.yaml` | manipulator wrist camera | ROS `camera_info` YAML |
| `lidar_camera_autoware.yml` | mobile-base lidar-camera extrinsic | OpenCV FileStorage YAML (Autoware names) |
| `quadruped_camchain_imucam.yaml`, `quadruped_imu.yaml`, `quadruped_imu_calibrated.yaml` | quadruped head IMU-camera rig | Kalibr |
| `rov_camera.xml` | marine ROV fisheye camera | OpenCV FileStorage XML |
| `aerial_camera_info.json` | aerial gimbal camera | `sensor_msgs/CameraInfo` as JSON |
| `stereo_unnamed_extrinsics.yml` | stereo rig, `R`/`T` without frame names | OpenCV FileStorage YAML |
| `chain_missing_extrinsics.yaml` | camera chain with gaps | Kalibr |

Corrupt and hostile: `truncated_camchain.yaml`, `empty.yaml`, `alias_bomb_camera_info.yaml`,
`nan_camera_info.yaml`, `short_matrix_camera_info.yaml`, `rov_dtd.xml`, `rov_truncated.xml`.
Larger and nested inputs are generated in the tests.

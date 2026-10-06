# 0073 — Hand-eye calibration results, and an OpenCV calibration's subject and time as stated

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-207
- Amends: ADR 0055 §2 (three more formats are claimed) and §3 (an OpenCV file's `camera_name` is its
  subject and its `calibration_time` its performed time; a hand-eye result's transform is a
  `FrameTransform`)

## Context

A hand-eye calibration says where a camera sits on a robot. It is the one calibration that every arm,
mobile manipulator and humanoid with a camera has. ADR 0055 reads none of the files that hold one.
The MVL-181 corpus showed it: four of five calibration files were declined as `calibration.no_keys`
and landed as configuration snapshots. The one OpenCV file kept `subject` and `performed` `Unknown`,
although it states `camera_name` and `calibration_time`, because ADR 0055 §3 left both `Unknown`.

The tools that compute hand-eye results write them in documented shapes:

- ROS `easy_handeye`, read from its source. Releases from 0.3 write `parameters` (`eye_on_hand`,
  `robot_base_frame`, `robot_effector_frame`, `tracking_base_frame`, `tracking_marker_frame`, the
  MoveIt group) and `transformation` (`x`, `y`, `z`, `qx`, `qy`, `qz`, `qw`) with `yaml.dump`.
  Earlier releases write the same keys flat, with only the robot frame the mode uses.
- ROS 2 `easy_handeye2` writes its `HandeyeCalibration` message as YAML (`.calib`). That is
  `parameters` with `calibration_type` (`eye_in_hand` or `eye_on_base`) and the same frames, then
  `transform` with `translation` (`x`, `y`, `z`) and `rotation` (`x`, `y`, `z`, `w`).
- MoveIt Calibration's "Save camera pose" writes a `<launch>` file. It holds one `tf2_ros`
  `static_transform_publisher` node named `camera_link_broadcaster`, whose `args` are
  `x y z qx qy qz qw frame_id child_frame_id`.

Each tool publishes its result as a tf transform. Its parent (`header.frame_id`) is the effector for
eye-in-hand, the robot base for eye-on-base, and MoveIt's `frame_id`. Its child is the camera's
frame. None of these files states a sample count or a residual. OpenCV's own calibration samples
write `calibration_time` (and `nframes` or `nr_of_frames` and `avg_reprojection_error`). The value
is `strftime("%c")` in the C locale, since the samples never set a locale.

## Decision

1. **Three formats, claimed by their tools' keys** (adds to ADR 0055 §2). The rules below are exact,
   never a name, and claimed at `VERIFIED` as before.
   - `easy_handeye` is a `transformation` mapping plus `eye_on_hand`, `tracking_base_frame` and a
     robot frame, either under `parameters` or at the root.
   - `easy_handeye2` is `parameters` with `calibration_type`, `tracking_base_frame` and a robot frame,
     plus a `transform` mapping.
   - `moveit_handeye` is a `launch` root whose only element is that node, with `args`.
   - A file that resembles them but lacks the keys is not claimed. The MVL-181 corpus's invented
     shape has `translation` and no rotation, so it stays with `config`. No shape is special-cased.
2. **The transform as stated: one `FrameTransform`, a `Pose`, in the file's `FrameGraph`.**
   - Parent and child are the tf frames the tool publishes (above). The direction is
     `Known(child_to_parent)`, citing the transform, because a tf transform is the child's pose in
     the parent. This follows ADR 0055's reasoning for Kalibr: the tool's own definition is the
     format speaking. ADR 0007 §3's `Ambiguous` stays for files with no defining tool.
   - The translation is `x, y, z`. The quaternion keeps the file's order. That order is
     `Known(xyzw)` or `Known(wxyz)` from the key names or from `static_transform_publisher`'s
     argument order. The algebra is `Unknown`. Any other written order is not reordered: the
     transform is not emitted (`extrinsic_not_read`).
   - The translation unit is `Known(m)` for MoveIt, because `static_transform_publisher`'s arguments
     are documented in metres. It is `Unknown` for easy_handeye, because `geometry_msgs/Transform`
     states no unit and REP-103 is a community convention (ADR 0007 §4).
   - Nothing is normalised. A quaternion more than 1e-4 from norm 1 (MoveIt prints six significant
     digits) is kept and reported as `quaternion_not_unit`.
   - The subject is the camera's frame: `tracking_base_frame`, or MoveIt's `child_frame_id`. This is
     the same as Kalibr's `cam0`, so a sensor's calibrations can be found by its name.
   - The transformation is consumed, as Kalibr's matrices are. Every other key stays a parameter
     under ADR 0055 §3's rules, mode and frames included.
   - If the mode is not a boolean every YAML version agrees on, or not one of the two
     `calibration_type`s, or a needed frame is not a non-empty name of at most 256 characters, or
     both frames are the same, the finding is `frame_unresolved`. If a component is missing,
     repeated, not a number or not finite, or `args` does not hold nine values, the finding is
     `extrinsic_not_read`. In both cases no transform is emitted and the values stay parameters.
     MoveIt's `args` text is then the parameter `node/args`, which is `Unknown` with
     `value_not_read` past `max_scalar_length`.
   - A sample count or error is a parameter only where a file states one. None of these formats
     does, and nothing is computed.
3. **An OpenCV file's subject and time, stated** (amends ADR 0055 §3).
   - `camera_name` is `subject`.
   - `calibration_time` (or the old tutorial's `calibration_Time`) is `performed`, on a
     `TimestampDomain` of its own: field the key, scope `()`, role `document`, epoch `unix`.
   - Both are `stated` (the file's writer declares them, as an image's EXIF time is). ROS and Kalibr
     values keep ADR 0055's citations.
   - The time is counted by ADR 0023 §2, never converted. Two forms are read:
     - ISO 8601 extended form, with `T` or a space, up to nine fraction digits, and an optional `Z`,
       `+HH:MM` or `+HHMM`, which may follow one space (the form of `date '+%F %T %z'` and git);
     - glibc's C-locale `%c` (`Thu Oct  1 14:02:37 2026`), whose weekday must match its date.
   - With a zone, the time names an instant (timescale `posix`), and the offset stays in the cited
     text. Without one, it counts on its own civil clock (timescale `Unknown`). The resolution is
     the finest field stated.
   - Locale-dependent forms are not read: a two-digit year whose century is not written, or a zone
     abbreviation such as `EDT`. A leap second and a date that is not in the calendar are not read
     either. Each gives `time_not_read` and `Unknown`. The text stays a parameter in every case.
     A null time (`calibration_time:` with no value) is `Unknown` with no finding.
4. **No schema change.** `Calibration`, `FrameTransform` with `Pose` and `Quaternion`, and
   `TimestampDomain` all exist. The adapter adds `timestamp_domain` to its record kinds.
   - Calibration adapter 0.2.0 is new lineage (ADR 0003). Its records for files 0.1.0 read change
     only for OpenCV files that state `camera_name` or `calibration_time`, or whose XML quotes a
     string. Its libraries drop `expat`'s build version, as MVL-206 does, so its output does not
     depend on the CPython patch release.
   - XML elements now keep their attributes for MoveIt's `args`, and the XML probe also reads a
     whole `launch` file.
   - An OpenCV XML string in double quotes is read without them, as OpenCV's reader does. OpenCV
     quotes any string holding a space, so `"Thu Oct  1 14:02:37 2026"` is otherwise unreadable. This
     follows ADR 0055's rule that XML values are typed as OpenCV reads them. It changes those text
     parameters' values, for example `rov_down_cam` for the `"rov_down_cam"` the bytes hold.

## Alternatives considered

- *Read the corpus's invented shape* (`translation` with a `unit`, no rotation). No tool writes it.
  Claiming it would make a guessed format part of the contract. Platform rewrites the corpus as real
  easy_handeye output instead.
- *`Ambiguous` direction for hand-eye results, as in the manipulator worked example.* That example
  is a file from no named tool. Each tool here publishes the transform as a tf parent→child, which
  states the direction.
- *Subject `Unknown`, because a hand-eye result calibrates a pair of frames.* The pair is in the
  transform. The thing whose pose was estimated is the camera, and consumers (Memory's calibration
  history) find calibrations by the sensor's declared name.
- *Normalise near-unit quaternions, or reorder to xyzw.* Both are derived transforms (ADR 0007 §5).
- *Read any `static_transform_publisher` in any launch file.* A launch file declares many static
  transforms that are not calibrations. Only MoveIt's exact output is a calibration result.
- *Parse every `strftime` locale.* Locale-dependent output is ambiguous without the locale.
  Guessing would make the record depend on the parser's choices, not on the file.

## Consequences

- Hand-eye results from easy_handeye, easy_handeye2 and MoveIt Calibration become calibrations
  with transforms in their own frame graph. MVL-37 alignment and Memory's drift consolidator
  (MVL-128) can use them. A drift between two easy_handeye results has no declared translation unit.
  A manifest or a derived transform must supply one.
- MoveIt Calibration for ROS 2 (a Python launch file) and MoveIt's samples YAML (raw pose pairs, not
  a result) are not read. Adding a format is a matcher in `_handeye.py`.
- Revisit if a tool states a sample count, a residual or a quaternion algebra in its result. That
  maps to a parameter or a `Known` convention by the same rule.

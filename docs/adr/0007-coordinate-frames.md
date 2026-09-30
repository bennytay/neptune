# 0007 — Coordinate-frame semantics

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-55

## Context

Frames are the second-largest source of silent robotics bugs, after clocks. Common failure modes include:

- **Conflicting axis conventions.** ROS uses REP-103 (FLU body, ENU world). PX4/aerospace uses FRD/NED. Camera
  optical frames are z-forward, x-right, y-down. Game engines are often left-handed.
- **Quaternion order.** ROS uses xyzw. Eigen and many calibration tools use wxyz.
- **Transform direction.** Extrinsics published as "camera to lidar" often mean the opposite.
- **Frame-name drift.** ROS 1 tf allows a leading `/` (`/base_link`); ROS 2 does not.
- **Same name, different frame.** Two robots' `base_link`, or two URDF revisions, share a name but are
  different frames.

Resolving any of these by assumption at parse time produces data that looks correct and is wrong by a rotation
or an inversion, which is the worst kind of error to discover downstream.

## Decision

1. **`FrameRef = (frame_id, frame_graph_id)`.**
   - `frame_id` is the string **exactly as the source declares it**. There is no stripping of leading `/`, no
     case folding, and no prefix removal. Reconciling `/base_link` with `base_link` is alignment (MVL-37).
   - `frame_graph_id` identifies the frame graph the frame belongs to.
2. **Frame graphs are scoped to one source by default.** Examples: a URDF, the tf stream of one log, one
   calibration file.
   - Equal frame names in two graphs are two frames until an alignment record says otherwise.
   - This mirrors timestamp domains (ADR 0005), and frame-graph identity follows tier-2 rules (ADR 0003).
3. **Transforms are records, stored as declared.** A frame transform record carries:
   - parent and child `FrameRef`s;
   - the transform values in the source's representation (quaternion or matrix or Euler, with the component order
     declared);
   - translation units and angle units, `Knowledge`-wrapped (ADR 0004);
   - **direction**: which frame's coordinates it maps into which;
   - validity: static, or timestamped in a domain (ADR 0005);
   - provenance.

   If the source does not declare direction, as with many calibration files, the direction is `Ambiguous` with
   both readings as candidates. It is not guessed. If the component order is not declared by the source or its
   format specification, it is `Unknown` or `Ambiguous`.
4. **Conventions are properties of frames, not global defaults.** Axis convention (ENU, NED, FLU, FRD, optical,
   or other) and handedness are `Knowledge`-wrapped properties of each frame.
   - **`Known`** only when the source, or a specification the format makes normative, declares it. Examples: a
     PX4 uORB topic definition documenting NED; the `sensor_msgs/CameraInfo` definition of the optical frame;
     URDF's specification of metres and radians. Provenance then cites the data or the format specification.
   - **`Unknown`** when only community convention applies. REP-103 for an arbitrary ROS frame is guidance that
     real systems violate, so it is not declared evidence.
   - There is **no** project-wide default convention.
5. **No normalisation at parse time.** Converting NED to ENU, reordering quaternions, inverting transforms or
   converting units happens only as a derived transform with its own provenance. The stored original is never
   replaced.
6. **Frame-graph assembly and cross-source alignment** (joining tf trees, URDFs and calibrations; detecting cycles
   and conflicts) is MVL-37. Parse-time adapters only emit what each source declares, plus findings for internal
   contradictions such as a frame with two parents in one static graph.

The Python types for rotations and poses, the geometry primitives and the list of named conventions are
MVL-4's.

## Alternatives considered

- **Normalise everything to one convention** (REP-103 ENU/FLU, metres, xyzw) at parse time. Convenient for
  consumers. It requires knowing each source's true convention, which is exactly what is often undeclared, so it
  would bake in guesses invisibly. A normalised view is a derived product.
- **Global frame names** (`base_link` means the same thing everywhere within a run). Simpler joins. It falsely
  merges frames across robots, URDF revisions and sessions. Merges are cheap to add with evidence and impossible
  to undo after the fact.
- **Canonicalise frame ids** (strip `/`, lower-case). It removes real distinctions in some systems and hides
  ROS 1/ROS 2 drift that alignment should surface. The canonicalised view is an alignment output.
- **Treat REP-103 as declared for all ROS data.** Most ROS data follows it, but calibration and third-party
  drivers are the common exceptions, and those exceptions are exactly where errors hurt.

## Consequences

- Consumers cannot compose transforms across sources until alignment has run or the conventions are declared.
  That friction is intended.
- Every ambiguity in a team's frame data surfaces as `Ambiguous`/`Unknown` plus findings, instead of as a wrong
  pose later.
- Adapters must know their format's normative conventions and cite them. That is part of the adapter's
  documentation burden.
- Frame-graph counts grow with sources; MVL-37 carries the merge logic.
- Revisit if alignment (MVL-37) shows that source-scoped graphs make common queries impractical even with
  evidence-backed merges.

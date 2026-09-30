# 0015 — Frames, rotations and frame transforms: types and named conventions

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-63 (sub-issue of MVL-4)

## Context

ADR 0007 fixed the rules for frames: names are verbatim, graphs are scoped to a source, transforms are stored as
declared, conventions are per-frame `Knowledge`, and nothing is normalised at parse time. It left these to MVL-4:
the Python types for rotations and poses, the geometry primitives, and the list of named conventions.

One shape question decides the rest. ADR 0007 §3 needs "component order undeclared" to be `Unknown` or
`Ambiguous`. If a rotation were a single `Knowledge`-wrapped value whose type fixes the order, an undeclared
order could only be `Unknown` for the whole rotation, and the numbers would be lost. Or it would be an
`Ambiguous` list of every reading, which says that the evidence supports each one. Usually it supports none in
particular.

## Decision

1. **Numbers are structural; interpretations are `Knowledge`.** Every rotation, translation and pose type keeps
   its components as a tuple of finite floats, in the source's order, as the adapter parsed them. Each property
   that says what the numbers mean is its own `Knowledge` field. An undeclared order is then `Unknown` and the
   numbers survive. This is the `TimestampDomain` pattern (ADR 0012 §2).
   - Components are `float` (IEEE-754 binary64). Ints are rejected, because `1` and `1.0` are different
     canonical JSON. NaN and ±Infinity are rejected (ADR 0002), so the adapter emits a finding instead.
   - Text sources are parsed with `float()`. The declared spelling stays recoverable through the locator, as it
     does for units (ADR 0013).
   - Norms, orthonormality and a homogeneous matrix's bottom row are **not** checked. Wrong values are
     declared evidence, and flagging them is a validation concern.
2. **Frames** (`neptune.model.frames`):
   - `FrameRef(frame_id, frame_graph_id)`. `frame_id` is verbatim text of at most 256 characters.
     `frame_graph_id` is a tier-2 `RecordId`, derived by the adapter from the source revision and the part of the
     source the graph covers.
   - `Frame(ref, axes: Knowledge[AxisConvention], handedness: Knowledge[Handedness])`. There is no default.
     Handedness may be declared on its own. If both fields are `Known` and disagree, construction raises, and the
     adapter records a finding and `Ambiguous`.
   - Named conventions: the letters give +x, +y, +z. E/N/W/S/U/D are earth directions; F/B/L/R/U/D are body
     directions. Handedness follows from the name (tested against the cross product).

     | Convention | Used by | Handedness |
     |---|---|---|
     | `enu` | ROS map/odom (REP-103), GIS | right |
     | `ned` | PX4, aerospace | right |
     | `nwu` | some ROS drivers, marine | right |
     | `flu` | ROS `base_link` (REP-103) | right |
     | `frd` | PX4 and aerospace body | right |
     | `rdf` | camera optical: ROS `*_optical_frame`, OpenCV | right |
     | `rub` | OpenGL, Blender, ARKit, NeRF cameras | right |
     | `ruf` | Unity | left |
     | `fru` | Unreal Engine | left |

     A convention outside this list is `Unknown` plus a finding until an ADR adds it. Adding one is additive.
3. **Rotation types.** `Rotation` is the union of:

   | Type | Components | `Knowledge` fields |
   |---|---|---|
   | `Quaternion` | 4 | `order` (`xyzw` / `wxyz`), `convention` (`hamilton` / `jpl`) |
   | `RotationMatrix` | 9 | `layout` (`row_major` / `column_major`) |
   | `EulerAngles` | 3 | `sequence` (12 axis sequences, `values[i]` about `sequence[i]`), `mode` (`intrinsic` / `extrinsic`), `unit` |
   | `RotationVector` | 3 | `unit`: axis × angle, as in OpenCV's `rvec` |

   The quaternion algebra is a field because Hamilton and JPL quaternions with the same four numbers are
   inverse rotations. Some VIO and calibration tools use JPL.
4. **Poses.** `Translation(values[3], unit)`. `Pose(translation, rotation)` holds a translation and rotation that
   are declared separately (tf, URDF `xyz`/`rpy`). `HomogeneousMatrix(values[16], layout, translation_unit)` holds
   a declared 4x4 transform (Kalibr's `T_cam_imu`). `TransformValue` is `Pose | HomogeneousMatrix`. Unit fields are
   dimension-checked: angle for rotations, length for translations. Every `Ambiguous` candidate is checked too.
5. **`FrameTransform(parent, child, direction, value, validity)`**. This is the frame-transform record of ADR 0007
   §3. It is **not** called `TransformRecord`, which is ADR 0006's provenance record for an adapter run.
   - `direction: Knowledge[TransformDirection]`. `child_to_parent` means the values map coordinates in the child
     into the parent, which is the child's pose in the parent: ROS tf and URDF joint origins. The alternative is
     `parent_to_child`.
   - `parent` and `child` are the roles the source gives the frames. If the source has no hierarchy (a
     calibration file), the adapter's descriptor documents which named frame fills which slot. `direction`
     still carries what the source does or does not say.
   - Both frames must be in one graph, because relating graphs is alignment (MVL-37). A frame cannot be its own
     parent.
   - `validity` is `STATIC` (the source gives no time) or a `Timestamp` in a declared domain. How long a static
     calibration stays valid is not decided here.
   - Record provenance arrives with the entity envelope (MVL-1, MVL-3), as for `TimestampDomain`.
6. **Earth-referenced positions** (`neptune.model.spatial`):
   - `CrsCode(authority, code)`, verbatim, for example `EPSG:4326`. It is not looked up and not case-folded.
   - `GeodeticPosition(latitude, longitude, height, crs, angle_unit, height_unit, height_reference)`.
     Latitude and longitude are floats as declared. `height` is `Knowledge[float]`, so a 2D fix has it
     `NotCovered`. Everything else is `Knowledge`.
   - `HeightReference` is one of `ellipsoid`, `mean_sea_level` (the CRS names the geoid), `home` (MAVLink
     `relative_alt`) or `ground`.
   - The adapter maps the source's named fields to latitude and longitude. It never relies on a CRS's axis order.
     Ranges are not checked, because the unit may be unknown.
7. **JSON.** Every type has `to_json` and a strict `*_from_json`, which rejects unknown kinds, missing or extra
   keys, and ints in place of floats. Rotations and transform values are tagged with `"kind"`. Validity is
   `{"kind":"static"}` or `{"kind":"stamped","stamp":{…}}`.

## Alternatives considered

- **One wrapped value per rotation** (`Knowledge[QuaternionXYZW]`). The type checker would carry the order, but
  an undeclared order would lose the numbers or overstate the evidence as `Ambiguous` (see Context).
- **Store components in one canonical order** (always xyzw, row-major). This is exactly the reordering that
  ADR 0007 §5 forbids at parse time, and it cannot be done when the order is unknown.
- **Exact decimals (`Fraction` or `Decimal`) for components.** Most sources hold binary floats, so a decimal
  form would be longer and no more faithful for them. Shortest-repr JSON already round-trips every decimal text
  of up to 15 significant digits.
- **Named conventions as free axis triples** (any of 48 signed permutations). Only the named ones are declared
  in real specifications, and a closed list keeps "Known" tied to a citation. Triples can be added if a source
  declares one.
- **Allowing transforms across graphs.** This would let an adapter join two sources' frames silently, which is
  MVL-37's decision and needs evidence.
- **WKT or PROJ strings for CRSs.** No v0 adapter needs them. A source that gives only WKT has `crs` `Unknown`
  plus a finding until a type is added.

## Consequences

- Consumers cannot compose, invert or reorder a transform without checking `order`, `layout`, `direction` and
  units. That friction is intended.
- A sensor stream of poses is Parquet (ADR 0002), not JSON. MVL-1 lays out the columns. The per-column
  interpretation (order, direction, units) is written once per stream with these types.
- Scaled integer units such as MAVLink's degE7 are not in the unit catalogue, so those positions keep their
  numbers with `angle_unit` `Unknown` until ADR 0013's catalogue gains them.
- Changing a convention's meaning, a JSON shape or a validation rule changes adapter output and needs a new ADR.

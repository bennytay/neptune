# 0039 — Robot descriptions: URDF and Xacro as hardware, frames and declared parameters

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-24
- Extends: ADR 0019 §4 (the extension rule), ADR 0023 §1 and ADR 0037 §1 (growth by addition);
  schema version 5

## Context

MVL-24 makes a robot's embodiment first-class machine context: a downstream system must read a
robot's topology and sensor placements without parsing XML or Xacro again. ADR 0019 already maps a
description to a `HardwareConfiguration`, a `HardwareComponent` per part and the `FrameGraph` and
`FrameTransform`s of its kinematics, and says that what only one category has (a joint's type
and limits, a link's inertia and geometry, a sensor's settings) arrives as new record kinds that
name the component. Until now that detail stayed in the bytes.

The forces:

- **The model is frozen and grows only by addition** (ADR 0023 §1). Every kind added here is
  kept forever, and SDF and MJCF (MVL-25) lay the same facts out differently.
- **One source per adapter call** (ADR 0024). An adapter sees one artifact's bytes; its output
  must depend only on those bytes, the chunk and the config (law 1), and cite only that source
  (law 8). Xacro's `xacro:include`, `$(find pkg)` and `$(env)` reach outside the file.
- **All input is hostile.** XML brings entity expansion (billion laughs), external entities,
  deep nesting and huge attributes; Xacro evaluates Python expressions (`${...}`) and can recurse
  or explode through macros.
- **Declared, not assumed.** URDF is SI by its specification, and the specification gives
  defaults (an absent `<origin>` is the identity, an absent `<axis>` is `1 0 0`).

## Decision

1. **One adapter, `urdf`, reads URDF and Xacro** (`neptune.adapters.urdf`).
   - A document whose root element is `<robot>` is `STRUCTURE`; a head that shows no root but a
     name ending `.urdf` or `.xacro` is `NAME_ONLY`, so a damaged description is still read and
     reported. A document is Xacro when it declares or uses the `xacro:` prefix.
   - One chunk per source (`{part: description}`): a description is read whole, which is small.
2. **What a description becomes, in existing kinds** (ADR 0019 §3–§4):
   - `<robot>`: a `HardwareConfiguration` (`machine` and `revision` `NotCovered`: URDF has no
     place for either) and a `FrameGraph` of scope `()`, both citing the `<robot>` element. A
     robot with no `<link>` describes no hardware (a macro library): no configuration, a
     `urdf.no_links` finding.
   - `<link>`: a `link` component and its `Frame` (axes and handedness `Unknown`: URDF does not
     declare them; REP-103 is convention). `<joint>`: a `joint` component framed at its child
     link, and a `FrameTransform` from its `<origin>`: child to parent, translation in m, `rpy` as
     extrinsic XYZ Euler angles in rad, each `Known` citing the `<robot>` element as the bytes
     that establish the format (ADR 0017 §6). An absent `<origin>`, or an absent `xyz` or `rpy`,
     is the specification's identity, which is how the specification reads that joint element;
     the parameters (§3) show it was not stated.
   - A transmission's `<actuator>`, a URDF `<sensor>` (framed at its `<parent link>`) and a
     `<sensor>` inside a `<gazebo reference="…">` block (framed at that link) are `actuator` and
     `sensor` components. Sensors get no frames of their own: inventing a frame name is a guess,
     so a placement is the link's frame plus the declared pose parameters.
3. **`hardware_specification`: what a declaration states about one component or configuration**
   (the extension rule's companion kind; this and §4's and §5's kinds are `since` 3). `subject` names the `HardwareComponent` or
   `HardwareConfiguration` it adds to; `parameters` are `DeclaredParameter`s, sorted, unique, at
   least one. `CalibrationParameter` is generalised as `DeclaredParameter` (same JSON; the old
   name stays an alias).
   - Names are paths: `<element>/…/<attribute>` for an attribute, the element path for its
     stripped text, from the subject down. Elements the URDF specification lets repeat (`visual`,
     `collision`, top-level `material`, `plugin`, transmission `joint` and `actuator`,
     `hardwareInterface`) are numbered from 0 in document order; another element that repeats is
     an `element_repeated` finding and none of its occurrences is a parameter. Gazebo sensors,
     an SDF dialect Neptune does not specify, number any repeated element.
   - Values are numbers only where the URDF specification makes them numbers (origins, axes,
     limits, dynamics, mimic, safety controller, calibration, inertia, mass, geometry sizes,
     scale, rgba, mechanical reduction, URDF sensor settings), read with a strict decimal grammar
     (`inf` and `nan` kept as `NonFinite`); a value that does not parse is `Unknown` plus
     `urdf.value_unparsable`. Everything else, including mesh and texture references and all of a
     Gazebo block, is text verbatim.
   - Units are the specification's, `Known` citing `<robot>`: m, rad, kg, kg.m^2, `1` for axis,
     scale and rgba, Hz for a sensor's rate. A joint's limits, calibration, mimic offset and
     safety limits are rad (revolute, continuous) or m (prismatic); effort N.m or N; velocity
     rad.s^-1 or m.s^-1; damping N.m.s.rad^-1 or N.s.m^-1; friction N.m or N. For any other joint
     type, and for values the specification gives no unit (`k_position`, image width), the unit
     is `Unknown`. Nothing is converted.
   - **Only what the file states.** The specification's defaults are not written: a written
     default is indistinguishable from a stated value, and applying a default is normalisation,
     which is a derived transform's.
   - Each value cites the element holding it; the subject's own attributes inherit the record's.
4. **`description_extension`: a block for another tool, kept opaque.** `<gazebo>`,
   `<ros2_control>` and any top-level element URDF does not define are one record each:
   `configuration`, `element` (the name verbatim) and `parameters`: the block's own attributes
   and every `plugin` it names outside its sensors (`plugin/<i>/<attribute>`, `plugin/<i>` for a
   ros2_control class), as text. The block stays in the bytes, cited whole.
5. **Xacro is expanded as far as the file alone decides, and never guessed beyond.**
   - Expanded, following xacro 2.x (ROS 2): properties (lazy, `default`, `scope`, block
     properties), arguments with their declared defaults, `${…}`, `$(arg)`, `$(eval)`, macros
     (plain, `:=` defaults, `^` and `^|` forwarding, `*` and `**` blocks, dynamic scope),
     `xacro:call`, `insert_block`, `if` and `unless`.
   - Expressions are parsed with `ast` and walked as a closed set of nodes (literals, names,
     arithmetic, comparisons, boolean logic, conditionals, `math` and a few builtins); `eval` is
     never called. Integers stay within 64 bits, strings within 64 KiB, an expression within 1,024
     characters, 256 nodes and depth 32.
   - `$(find)`, `$(env)`, `$(optenv)`, `$(dirname)`, `$(cwd)` and an argument with no declared
     default need a ROS installation or the environment: the value keeps its declared text and is
     `NotCovered`, with `urdf.xacro_not_covered`. An undefined property, macro or block, an
     invalid expression or call, and an unsupported construct keep their text as `Unknown`, or
     are dropped (a call, a block, an undecidable conditional's content), with a finding naming
     which. Findings cite the source element that holds the problem.
   - **`xacro:include` is never followed**, and is `urdf.xacro_include_not_followed`. The ABI
     gives an adapter one source: reading a sibling file would make output depend on bytes no
     chunk id covers (cache and resume would serve stale output), and its records would cite
     another source. The included file in the same ingest root is ingested as its own source.
   - Bounds: macro nesting 64, 500,000 expansion steps, the configured `max_elements`,
     `max_depth` and `max_bytes` for the expansion, and a nesting budget: each nested element,
     call or conditional and each property evaluated inside another is counted at the Python
     frames it may take, against 640. So the expander never reaches Python's recursion limit,
     and where a bound is met depends on the document alone, never on the stack it runs on.
     Past a bound the expansion is `urdf.limit_exceeded` and nothing of it is recorded; a
     property past the budget is `Unknown` with `urdf.xacro_invalid`.
   - **`description_expansion`** records what was expanded: `language` (`xacro`), the expanded
     document's `digest` (sha256) and `size`, and `arguments`: each declared argument with the text
     the expansion used, `NotCovered` without a default, citing its `xacro:arg`. It cites the
     whole source. The expansion is written by a fixed canonical serializer and is reproduced by
     re-running the transform; it is not stored, as a decompressed member is not.
   - Records read from an expansion cite into it with the adapter step `urdf:expansion`:
     `[ByteRange(0, size), urdf:expansion{language: xacro}, ByteRange(element)]`. The raw Xacro is
     the source artifact; the expansion is the decoded scope the inner step addresses.
6. **Hostile XML is refused, not defused.** The standard library's expat parses UTF-8 only (the
   declared encoding must be UTF-8 or ASCII). A DOCTYPE stops the read (`urdf.doctype_refused`),
   so no entity is ever declared, expanded or fetched; URDF never needs one. Nesting deeper than
   `max_depth` (64), more than `max_elements` (50,000) elements, a source over `max_bytes`
   (16 MiB) and an attribute or text run over 64 KiB are `urdf.limit_exceeded` or
   `urdf.too_large`. `max_depth` is never more than 128, for the same reason as the budget. No `defusedxml`: its protection is these same expat handlers, refusing a
   DOCTYPE outright is stricter, and ADR 0001 §4 keeps dependencies to what a format needs. No
   expat text enters a finding, so output does not depend on expat's version and the transform
   declares no library.
7. **References are recorded, not resolved.** Mesh, texture and plugin references are parameters,
   verbatim (`package://…`, relative paths, file names). Resolving them against the ingest root
   relates two sources, which is validation's or a binding's (MVL-37, MVL-38); MVL-32 ingests the
   geometry itself.
8. **A description names a model, never a machine** (ADR 0003, ADR 0019 §1). No `Machine` is
   emitted. Identical files are one artifact read once (ADR 0009); two robots that ship it stay
   two robots, because nothing in the configuration names a machine.
9. **Findings** are `urdf.<name>`, documented in the descriptor, each once per source; past 1,000
   the first 1,000 are kept and `urdf.findings_capped` counts the rest.

## Alternatives considered

- **Typed kinds per category** (`JointLimits`, `LinkInertia`, `Geometry`, `SensorSettings`).
  Frozen forever and URDF-shaped: SDF and MJCF state limits, inertia and geometry in other
  layouts, and choosing one canonical layout at parse time is what ADR 0019 declined for camera
  intrinsics. Named, cited parameters keep every value; a typed view can be derived or added
  later without a migration.
- **No new kinds: leave the detail in the bytes.** Fails MVL-24's acceptance: a consumer could not
  read a joint's type, axis or limits, or a sensor's settings, without parsing XML.
- **Follow includes by reading sibling files in the sandbox.** Reads work there, but the output
  would depend on bytes outside the chunk id (stale cache, wrong resume), records would cite
  another source (law 8), and a relative path is resolved against a location the adapter is never
  told. Revisit with a multi-source ABI (ADR 0024's revisit trigger).
- **Run real xacro, or ROS.** Needs an installation or a network, depends on the environment
  (`$(find)`, `$(env)`), and evaluates hostile text with `eval`. It is the fixtures' oracle only.
- **Python `eval` with restricted globals, as xacro does.** Escapes through attributes
  (`().__class__…`) are well known, and `9**9**9` or `'a'*10**9` hang or exhaust memory.
- **`defusedxml` or `lxml`.** A dependency for what four expat handlers do; `lxml` brings native
  code and a second XML stack.
- **Write the specification's defaults as parameters.** Indistinguishable from stated values, so
  "the file said 0" and "the file said nothing" would read the same.
- **Store the expansion's text in a record or a blob.** The ABI emits records, not blobs, and a
  record holding the whole document duplicates it; a digest plus a reproducible transform is how
  every decoded scope (a gzip member, an MCAP chunk) is cited.
- **A separate `xacro` adapter.** Adapters never import each other, so it would duplicate the URDF
  reader, and two adapters would tie on every Xacro file.
- **A frame per sensor, named after the sensor.** A frame name the source never declared.

## Consequences

- A consumer reads topology from `FrameTransform` parents and children and each joint's
  specification (type, axis, limits), and a sensor's placement from its component's frame and its
  specification, with no XML. `tests/unit/adapters/test_urdf_adapter.py` checks this against
  `urdf_parser_py` and real xacro on three robots.
- Schema version 5: three kinds (`since` 5, after ADR 0051's lifecycle kinds at 4), and one
  locator step of the adapter's own. As ADR 0037 §1 has it, their records are written at version
  5 and every other record at its own kind's version, so no existing record, worked example or
  golden document changes; a package holding description records is a version 5 package.
- SDF and MJCF (MVL-25) reuse `hardware_specification`, `description_extension` and
  `description_expansion` with their own parameter names.
- A Xacro robot split across files is partly `NotCovered` until includes can be followed.
- Revisit: when the ABI lets an adapter read a set of sources (includes), when consumers need
  typed joint, inertia or geometry views (derive them), or if a real description needs a
  DOCTYPE.

# 0068 — Payload decoding and frame alignment: declared definitions into columns, run trees, links and comparable references

- Status: Accepted
- Date: 2026-10-03
- Issue: MVL-37
- Amends: ADR 0034 §4, ADR 0045 §4 and ADR 0046 §3 (payloads are not decoded: now they are, by the
  stream's declared definition, and a leading header's stamp is a clock)
- Extends: ADR 0007 §6 (frame-graph assembly and cross-source alignment), ADR 0018 §4 (value
  columns), ADR 0050 §6 (bindings join frame groups), ADR 0060 §2 (header stamps as anchors)

## Context

A recording's frames live in its messages: `tf2_msgs/TFMessage` on `/tf` and `/tf_static`, and a
`header.frame_id` on nearly every sensor message. Until now no ROS payload was decoded (the D1
gate's warehouse AMR emitted 21 `mcap.payload_not_decoded` findings), so a run had no frame graph
and no stream said which frame its values are in. Declared graphs (Kalibr's file-local graph, ADR
0055; a URDF's, MVL-24) name frames too, and GeoJSON states CRSs (ADR 0057). Downstream must be able
to ask: are values in this frame and in that one, or in this frame and that CRS, comparable, and
why. Forces:

- **Decoding is evidence.** A decoded field is the message's own value, `observed`, cited by its
  row; it belongs to the adapters, inside the sandbox, never to a pass that re-reads source bytes.
- **Never a bundled definition.** A type's layout is what the stream declares; filling in a
  standard `.msg` from a library would assume a version (ROS 1 `BatteryState` gained fields).
- **Hostile payloads.** Counts, lengths and nesting come from the bytes and the definition.
- **TF is a stream.** A recording at 100 Hz with 30 frames states 3,000 transforms a second; one
  canonical `FrameTransform` each is unworkable, and ADR 0015 already keeps pose streams in Parquet.
- **Frames are per graph (ADR 0007).** Equal names in two graphs are two frames until evidence says
  otherwise; REP-103 (metres, FLU, Hamilton quaternions) is convention, not evidence.
- **No new canonical kind** where a derived table will do; package-schema is contended.

## Decision

1. **Decoding by the declared definition** (`neptune.adapters.rosmsg`, shared like
   `neptune.adapters.structured`, ADR 0055, and allowed to the stream adapters by the leaf rule).
   - Definitions: `ros1msg` and `ros2msg` text with `MSG:` dependencies, and `ros2idl` with `IDL:`
     sections (modules, structs, typedefs with array declarators, sequences, bounded strings,
     annotations and constants skipped; enums, unions, `long double`, `wchar` and
     multi-dimensional arrays refused). CDR payloads decode by `ros2msg`/`ros2idl`, ROS 1 payloads
     by `ros1msg`; any other pairing, no definition, or one that does not parse is
     `payload_not_decoded` with its `reason`. Every type is decoded, not a list of types.
   - Columns are `value/<field path>`, the path as ADR 0049's layouts write it (`header.stamp.sec`,
     `transforms[].child_frame_id`). A path under one array level is a list column; a byte array
     (`uint8`, `byte`, `char`, `octet`), a path under two array levels and a field named as the
     adapter's own value column (MCAP's `sequence`, rosbag2's `message_id`) are walked without a
     column (`payload_partly_decoded` lists them); ROS 1 `time` and `duration` are `<path>.secs`
     and `<path>.nsecs`. Each value column has its state column: `unknown` where the payload does
     not hold its layout (CDR encapsulation and XCDR1 alignment, either byte order; ROS 1 packed
     and exact) or its text is not UTF-8 (that cell only), `not_covered` past a limit or where a
     rosbag2 payload spills out of its cell's page (the row cites the cell, not the overflow).
   - Limits are config options of each stream adapter (`decode_payloads`, `max_decoded_columns`
     512, `max_array_items` 65,536, `max_message_bytes` 16 MiB). A layout past the column limit, or
     with a construct the decoder does not read, keeps only a leading header. Every count is
     checked against the bytes left before anything is allocated. Payloads that do not decode
     cost one `payload_undecodable` finding per stream and chunk, never the chunk.
   - Two fixed bounds of the decoder, not config (changing one is a new adapter version), because
     a definition is a graph of types and bytes alone do not bound the work. Compiling a layout
     visits at most 16,384 fields, columns and left-out paths alike (`MAX_LAYOUT_NODES`: 21 types
     of two fields each unroll to 2^21 paths); past it the layout keeps only a leading header, else
     the stream is `payload_not_decoded` with `layout_node_limit`. Decoding one message walks at
     most 2^22 array elements one at a time (`max_walk_items`; a packed primitive array is one
     step), each costing one whatever its size, so arrays of a type with no bytes (a ROS 1 empty
     message, `T[0]`) cannot turn a few bytes into millions of steps; past it the message is
     `not_covered` (`walk_limit`, a limit, not corruption). Parts of a layout that take no bytes
     read and store nothing and are never walked. `payload_partly_decoded` lists at most 64
     left-out paths and counts them all (`left_out_count`); a stream adapter keeps the plans of at
     most 64 distinct definitions, keyed by digest, and refuses an over-large definition before.
     The layout's field count is taken over the type graph before anything is unrolled, so a
     definition past the cap costs its own size.
   - An empty message is one byte under CDR (rosidl's `uint8 structure_needs_at_least_one_member`,
     which the `.msg` text does not show: read, no column) and nothing under ROS 1, as `rosbags`
     serialises them.
   - A source's definitions share one budget: 16 MiB of definition bytes read and 2^20 layout
     fields taken, its distinct definitions taken in stream id order (MCAP channel id, ROS 1
     connection id, rosbag2 topic row id), never in file order. A definition is taken when it fits
     what is left; else its streams are `payload_not_decoded` with `layout_budget` (a `limit`
     warning), and a later, smaller definition may still fit. Every call decides alike: the MCAP
     and ROS 1 planners decide once and name the streams past the budget in their chunks'
     contexts (`over_budget`, only when there are any), and rosbag2, whose every call reads every
     topic, decides in each call.
   - MCAP, rosbag1 and rosbag2 become version 0.2.0: new lineage, nothing rewritten.
2. **A leading `std_msgs/Header` is a clock.** Where the root's first field is a header whose
   stamp and frame id are ROS's (checked on the definition, not the name), the stream gains a
   `TimestampDomain` `header.stamp` (scope `(topic,)`, nanosecond resolution stated by the
   definition, role, epoch and timescale `Unknown`) and a `time/<n>` column, `sec * 10^9 +
   nanosec`, `unknown` where nanoseconds are out of range. ADR 0060's `stream.co_recorded` rule
   then anchors it against the stream's clock 0 like any other clock (a zero stamp is no anchor).
3. **The frame-alignment pass, `neptune.frames`**, runs in the assemble phase after the clocks
   pass (stage 9d), over the admitted records and committed series, reading only the frame and
   transform columns. It writes five derived tables, every line `inferred`, and runs only when
   something is spatial:
   - `frame_tree {run, namespace, streams, rule: ros.run_frames}`: the frames one run's TF
     streams (`tf2_msgs/TFMessage`, `tf/tfMessage`) and header streams name under one tf namespace
     are one graph, the line's id their `frame_graph_id`. ROS names frames per tf tree, which is a
     convention, so it is inferred. Frame identity is scoped by the publishing tf topic's
     namespace: the topic without its last segment (`/robot1/tf` is `/robot1`; `/tf` and
     `/tf_static` share the root, `/`). A header stream joins the tree of the longest tf namespace
     its topic is under, else the root's. Same-named frames of two namespaces are never merged (a
     fleet's robots each publish `base_link`, the nav2 multi-robot pattern): they are two frames,
     the run has one `frame_name_ambiguous` finding listing them, and `compare` across them is
     never `same_frame` (no step joins them: `disconnected`).
     A declared graph (a calibration, a URDF) links into a run only where its frame names resolve
     in exactly one of the run's tf namespaces. Where they resolve in several, it links into none
     of them and the run has a `link_ambiguous` finding: nothing states which robot it describes,
     and one declared frame linked into two trees would join two robots through links
     (`compare(links=True)` stays `disconnected`).
   - `frame_edge {tree, stream, parent, child, persistence, direction, translation_unit,
     quaternion_convention, samples, first, last}`: one per pair one TF stream states. `static` on
     tf2's static topic (`tf_static` under any namespace). `direction` `Known(child_to_parent)`
     (tf2's definition of `TransformStamped`); `translation_unit` and `quaternion_convention`
     `Unknown`. `first`/`last` are the stream's clock-0 instants of its samples; the values stay
     in the series. Nothing is composed, inverted or converted.
   - `frame_link {left, right, rule}`: `same_name` (a declared graph's frame and a run tree's,
     one verbatim name) or `leading_slash` (`/x` and `x`, within a tree or across). Never between
     two run trees (two recordings' `base_link` may be two robots), never a merge.
   - `frame_group {members, origin, earth, dynamic}`: frames joined by declared transforms, tree
     edges and stated `FrameBinding`s (ADR 0050 §6: the binding's edge and its transform's frames
     are one), never by a link. `origin` is the one root (`Known`), several (`Ambiguous`) or none in
     a loop (`Unknown`); `earth` is `Unknown`: no supported source places a frame graph on the
     earth.
   - `spatial_reference {subject, frames, unset, crs, geodetic}`: per header stream, the frames
     its rows name with counts (odometry's child frame too) and the rows naming none; per spatial
     artifact, its declared frame and CRS; per located site or asset, its position's CRS. A CRS is
     copied as its state without the source's provenance (a stated absence is `NotApplicable`).
     `geodetic` names a type whose definition makes values geodetic (`sensor_msgs/NavSatFix`); no
     CRS code is invented for it.
4. **Findings** (`neptune.frames.*`): `disconnected` (a run's frames in more than one group, the
   groups listed), `multiple_parents`, `loop`, `static_changed` (a static pair restated with other
   values), `frame_unset`, `name_variants`, `frame_name_ambiguous` (one frame name in two tf
   namespaces' trees of a run), `link_ambiguous` (a declared graph whose names resolve in two
   namespaces of a run), `origin_unknown` (a subject with no frame, CRS or
   geodetic type), `untimed_transforms`, `frame_unrepresentable`.
5. **Comparability is a query** (`neptune.derived.frames.FrameIndex.compare`, built by
   `frame_index(records, derived)`), like ADR 0060's `ClockGraph.align`:
   - Two frames: the same frame is `same_frame`; otherwise breadth-first through evidence steps
     (declared transforms, bindings, edges), then, only when asked (`links=True`), through links,
     which makes the answer `inferred`. `Comparable` gives the path (each step's kind, record or
     line id, direction of travel), and the `caveats` a consumer composing it must resolve
     (`direction_unknown`/`ambiguous`, `translation_unit_unknown`, `rotation_order_unknown`,
     `quaternion_convention_unknown`, `time_dependent`). Given an instant, every dynamic edge must
     have samples spanning it on its clock, reached through the clock graph when the instant is on
     another clock (`outside_coverage`, `unsynchronised`).
   - Earth references: one CRS code, verbatim, is `same_crs`; two codes are `crs_differs` (Neptune
     never reprojects); an `Unknown` or `Ambiguous` CRS says so; two values of one geodetic type
     are `same_definition`; a geodetic type against a CRS is `crs_unknown`.
   - A frame against an earth reference is `no_georeference`; a subject with no reference is
     `frame_unknown` (`compare_subjects`).
6. **No canonical kind and no schema bump.** The tables are derived (ADR 0036 §8, derived schema
   version 1), so package-schema and every contract stay as they are.

## Alternatives considered

- **Canonical `FrameTransform` per tf message.** Millions of records per hour of recording, and a
  dynamic edge's value is a series, which ADR 0015 already puts in Parquet.
- **Decode in a derived pass that reads source bytes.** The runtime would parse untrusted bytes
  outside the sandbox, and a decoded value is evidence, not interpretation.
- **Bundle the standard definitions** (`tf2_msgs`, `sensor_msgs`): decodes bags without
  `message_definitions`, but assumes a version the stream never stated.
- **Decode only a list of types.** The same decoder serves every type; header frames of images,
  scans and point clouds are what frame alignment needs most.
- **Let a name match join groups**, or link two recordings' trees: two robots' `base_link` become
  one frame silently, which ADR 0007 forbids; a proposal kept apart costs a flag.
- **One tree per run**, whatever the tf topics' namespaces: a multi-robot recording's
  `/robot1/tf` and `/robot2/tf` with unprefixed `base_link` become one frame, with no finding and a
  `same_frame` answer: the silent identity merge ADR 0007 forbids.
- **Compose paths into a pose.** Needs the unit, quaternion algebra and direction that are
  `Unknown` for tf, and floats chosen for the consumer; the path and its caveats are the evidence.
- **An EPSG code for `NavSatFix`** (4326 or 4979): the definition names an ellipsoid, not a
  registry code; choosing one is inventing a CRS (ADR 0057).

## Consequences

- ROS payloads become queryable columns; most `payload_not_decoded` findings disappear, and
  packages with ROS sources change id (new adapter versions, columns, header clocks, more clock
  mappings, frame tables). Recordings without header streams or TF change only by decoding.
- Downstream can tell whether two spatial observations are comparable, through which records,
  with which open conventions, at which instant, or why not, without reading a payload.
- A URDF graph (MVL-24) joins groups through its `FrameTransform`s and links to run trees by name
  with no change here; MVL-38's snapshot bindings may later restrict links to the calibration a
  run used.
- Revisit when a source states a georeference (a map origin in a CRS), when nested arrays need
  columns, when a consumer needs composed poses, or when tf's per-transform stamps need a clock of
  their own.

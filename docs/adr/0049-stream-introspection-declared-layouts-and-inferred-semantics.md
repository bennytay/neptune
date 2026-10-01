# 0049 — Stream introspection: declared layouts and inferred semantics as derived tables

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-21
- Builds on: ADR 0018 (streams), ADR 0036 §8 (derived tables in the package)

## Context

A `Stream` (ADR 0018) keeps its schema as its source declares it: a type name
(`sensor_msgs/msg/Imu`), an encoding (`ros2msg`, `jsonschema`, `protobuf`, …) and an
`EvidenceRef` to the definition's bytes. Those bytes are a byte range in the source, not a copy.
Nobody downstream can tell what a run holds without parsing that text. Nor can they tell which
stream is the IMU without guessing from topic names.

MVL-21 asks for a schema registry, field paths and types, units where declared, and common
robotics semantics with a confidence and a source. Its acceptance is that downstream code can ask
what a run contains without decoding every message. The audit splits it by assertion kind. The
declared schema and its parse are facts. "This is an IMU" is `inferred`, so it never goes on a
`Stream` (non-negotiable 8).

The package-schema contract (`contracts/package-schema`) is in flux: 2.0.0 is held in an open PR
and another follows it. A new canonical record kind or field would collide with both.

## Decision

1. **No canonical change.** `Stream` already holds every declared fact: name, encoding and the
   definition's citation. This work adds two **derived** kinds (derived schema version 1, ADR 0036
   §8). The store checks their structure only, the package schema and its contract do not change,
   and `records/` holds no interpretation.
2. **`stream_layout`** (`neptune.derived.schemas`): one line per stream, `assertion_kind`
   `observed`. Its provenance is the stream's `schema_definition` (`definition`) under the
   `neptune.introspection` transform. The line is a decoding of the declared bytes, and so a
   provenanced derivative, which is why it sits in `derived/` and not on the `Stream`.
   - `state`:
     - `known`: parsed.
     - `known_absent`: the stream declares no schema.
     - `not_covered`: an encoding the registry does not parse, such as `protobuf`, `flatbuffer`,
       `ros2idl` or `omgidl`.
     - `unknown`: there is no definition, or it is unreadable, malformed or past a limit. A
       `problem` gives the reason and, for `.msg`, the line.
   - `types`: every type the definition declares, fields in declaration order. A field records:
     - its type (a primitive, or `pkg/Name`);
     - its array kind and length (`fixed`, `bounded` or `unbounded`);
     - a string bound;
     - a constant or a ROS 2 default, verbatim;
     - a declared `unit`.
   - `paths`: every path from the root, depth first in declaration order (`orientation.x`,
     `points[].positions[]`), each with its leaf type and why it ends. The endings are
     `primitive`, `unresolved` (the type is not defined), `recursive` (a cycle), `empty` and
     `depth` (the path has crossed `max_depth` nested message fields). `truncated` says that
     more paths exist.
   - Parsed encodings:
     - `ros1msg` and `ros2msg`: `MSG:` sections, `pkg/msg/Name` ≡ `pkg/Name`, unqualified names
       resolved in the user's package, ROS 1 `Header` → `std_msgs/Header`, and string constants
       keeping their `#`.
     - `jsonschema`: properties, items, `maxItems`, local `$ref` chains (also at the root, up
       to 32 hops), and a property's `unit` keyword. `allOf`, `anyOf` and `oneOf` are not
       parsed: a property using one ends `unresolved`, and a root using one is `unknown`
       (`composition_not_parsed`). Text that canonical JSON cannot hold (lone surrogates) is
       `invalid_utf8`.
   - `.msg` has no unit syntax, so a `.msg` field has no unit. Nothing is guessed from comments.
3. **`stream_semantic`** (`neptune.derived.semantics`): one line per stream, `inferred`. Its
   `candidates` are listed most confident first. Each candidate has a confidence band (a ranking,
   not a probability), every rule that fired with the facts it read, and any conventional units.
   Its `evidence` is the definition (when parsed) and the declared type name's citation.
   - The rules read type names and field shapes, never topics. Topics are names a person chose.
     | Rule | Fires when | Band |
     |---|---|---|
     | `known_type` | the declared type is in the table (ROS 1/2, PX4, Foxglove) and the parsed root has that type's fields | 0.9 |
     | `known_type_unchecked` | the type is in the table and no layout was parsed | 0.8 |
     | `shape_odometry` | root `pose` and `twist` fields of pose and twist shape | 0.65 |
     | `shape_<semantic>` | the root type has the semantic's field shape, whatever its name | 0.6 |
   - Semantics: `pose`, `twist`, `odometry`, `imu`, `wrench`, `transform`, `battery`, `gnss`,
     `image`, `compressed_image`, `point_cloud`, `laser_scan`, `joint_state`,
     `joint_trajectory_command`, `drive_command`, `gripper_command`, `vehicle_command`. Together
     they cover arms, grippers, mobile and car-like bases, legged and aerial platforms. A
     `geometry_msgs/Twist` is a `twist`, not a command, because the shape cannot tell a command
     from a measurement.
   - `state`:
     - `known` when one candidate leads;
     - `ambiguous` when two or more tie at the top, with none chosen and an
       `neptune.introspection.semantic_ambiguous` finding;
     - `unknown` when no rule fired.
   - Units come only from `known_type` rules, as field-path prefixes (REP 103: `m`, `m/s`,
     `rad/s`, `m/s2`, `V`, `A.h`, `deg`, `N`, `N.m`), stored as text and never converted. Joint
     states, trajectories and gripper commands get none, because the unit depends on whether a
     joint is revolute or prismatic.
   - A definition that lacks a known type's fields contradicts its name. `known_type` does not
     fire, and the driver reports `neptune.introspection.type_contradicts_layout`.
4. **In the job** (`neptune.derived.introspection.introspect`), at the start of `assemble`:
   - It runs over the streams of the admitted sources, before staging. It reads only the cited
     definition ranges, through the job's verified `LocalReader`. Pure-Python parsers are bounded
     like grouping, so no adapter call and no sandbox is needed.
   - Streams that share a definition are read and parsed once.
   - Limits are the transform's config, so changing one is a new lineage: 1 MiB per definition,
     64 MiB per package, 1024 types, 16384 fields, JSON nesting 64, path depth 32, 4096 paths.
     Every parser is iterative, and JSON nesting is checked before `json.loads`.
   - Findings:
     - `definition_malformed`: corrupt or unrepresentable bytes, or a parser that raised
       (`parser_failed`, caught per definition), warning;
     - `definition_not_covered`: a JSON Schema composition at the root, warning;
     - `classification_failed`: a rule that raised, caught per stream, warning;
     - `definition_limit`: warning;
     - `definition_unreadable`: warning;
     - `encoding_not_covered`: unsupported, info;
     - `paths_truncated`: limit, info;
     - plus the two in §3.

     There is one finding per definition, naming every stream it affects.
   - `upstream` names the adapters' transforms whose streams it read.
   - A package with no stream gets no introspection: no transform and no tables. A
     `streams_introspected` event carries the counts. The nine phases stay.
5. **The query surface**:
   - `neptune.sdk.run_contents(package)` and `IngestResult.contents()` return `RunContents` per
     run.
   - Each `RunContents` lists `StreamContents`: the record, topic, type, encodings, declared
     count, `fields`, `layout_state`, `semantic_state`, `carries(...)` and `may_carry(...)`
     (ties included). A run also offers `topic(...)`, `carrying(...)` and `semantics()`.
   - The surface reads records and derived tables only. It reads no series, no source and no
     payload.
   - A package without the tables gives `None` rather than a guess.

## Alternatives considered

- **Field paths and units as new fields or a new kind on `Stream`.** This is what the scope note
  suggests. It would reopen the package-schema contract while 2.0.0 is held, and it would make
  every adapter re-derive the same parse. The parse is a function of bytes the `Stream` already
  cites, so a derivative under its own transform loses nothing. Moving it to a canonical
  `stream_schema` kind later, in a package-schema bump, would be a new lineage of the same lines.
- **Parse at query time from the source.** The package stores nothing extra, but packages
  reference their sources by default (ADR 0022 §5). A package moved without its sources could
  no longer answer, which fails the acceptance.
- **Classify by topic name** (`/imu`, `/battery`). Topics are free text. The fixtures publish a
  `std_msgs/Float32` on `/battery_voltage`, which carries no battery semantics.
- **Resolve ties by rule order.** That would be a silent choice (non-negotiable 4). A tie is
  `ambiguous`, with every reading kept.
- **Units from `.msg` comments** (`# m/s`). They are free text and no grammar governs them.
  Conventional units come only with a known type, as an inference.
- **Parse in the sandbox.** Each definition would cost a fork per call for bounded pure-Python
  text parsing that reads at most 64 MiB of cited ranges. Grouping already parses names
  in-process on the same terms.

## Consequences

- Every job package with streams gains `derived/stream_layout.jsonl`,
  `derived/stream_semantic.jsonl` and the `neptune.introspection` transform, so those package ids
  change once. The MCAP golden package gains the transform; evidence ids do not change.
- Downstream code (MVL-22's lazy hydration, retrieval, memory) can pick streams by semantic and
  field without decoding, and can see why each was classified.
- `protobuf`, `flatbuffer`, `ros2idl` and `omgidl` layouts stay `not_covered` until a parser
  lands as a new transform version. Their type names are still classified (`known_type_unchecked`).
- Revisit if consumers need the layout as evidence across package-schema versions (promote it
  to a canonical kind), if the rule table needs vendor types that shapes cannot reach, or if
  messages turn out to declare units in-band (MVL-22).

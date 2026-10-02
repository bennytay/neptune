# 0055 — The calibration adapter: ROS, Kalibr and OpenCV files as calibrations, extrinsics and a file-local frame graph, over shared structured readers

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-26

## Context

A calibration is machine context that later systems must be able to cite and tell apart: which intrinsics, which
extrinsics, from which file, with which conventions. Robots write them in a handful of file shapes (ROS
`camera_info` YAML, Kalibr camchain and IMU YAML, OpenCV `FileStorage` YAML and XML) for arms, mobile bases,
legged platforms, boats and aircraft alike. All of them are JSON, YAML or XML that the `config` adapter (ADR
0037) already reads, or could, so a second adapter must not copy its parsers, and adapters never import each
other (ADR 0008 §4). The model is frozen apart from serialised schema bumps (ADR 0037 §1): `Calibration`,
`FrameGraph` and `FrameTransform` exist (ADR 0007, 0015, 0019) and no new kind or field is needed.

## Decision

1. **Shared readers, no adapter imports.** The readers, limits and whole-file findings of `config` move to
   `neptune.adapters.structured` (decode, format sniffing, JSON/TOML/YAML events with spans, depth, size,
   path-budget and scalar caps, `load`, `problems`). It is not an adapter: it imports only the model, identity
   and the contract, and `tests/unit/test_package.py` lets format packages import it and nothing else. `config`
   behaves as before; its descriptor and goldens are unchanged. XML is read by `calibration` alone, with
   `xml.parsers.expat` (a DTD or entity is refused, nesting and element counts are capped).
2. **One adapter, `calibration`, five formats, claimed by required keys.** `ros_camera_info`
   (`camera_matrix{data}` plus one of `distortion_model`, `distortion_coefficients`, `projection_matrix`,
   `image_width`), `ros_camera_info_message` (`K`, `P`, `distortion_model`), `kalibr` (a `camN` with
   `camera_model` and `intrinsics`, an `imuN` with a noise density, or a flat `imu.yaml` with both),
   `opencv_yaml` and `opencv_xml` (the `%YAML:1.0` header, an `opencv-matrix` or an `opencv_storage` root, and a
   camera-matrix or distortion name). A file whose whole text parses and holds a format's keys is `VERIFIED`,
   which beats `config`'s `STRUCTURE` claim on the same bytes; the head of a file over 64 KiB is `SIGNATURE`,
   still above it. A lookalike (`camera_matrix` alone, a `cam0` without `camera_model`) is not claimed and stays
   with `config`. Never from a name.
3. **Records: `calibration`, `frame_graph`, `frame_transform`; nothing new.**
   - One `Calibration` per calibrated subject (a ROS file, an OpenCV file, a Kalibr `camN` or `imuN`), cited as
     the entry's span (XML: its byte range). Subject: ROS `camera_name` (message form: `header.frame_id`), Kalibr
     the entry key, else `Unknown`. Machine, hardware revision and times are `Unknown`: binding is MVL-38's.
   - Parameters are named by key path (RFC 6901 escaped, `/`-joined); a sequence of numbers is one parameter in
     source order, any other sequence is its items by position; ints are read with `float()`; text is kept;
     `null` is `KnownAbsent`; an alias, an application tag or a number beyond binary64 is `Unknown` with a
     finding; where YAML 1.1 and 1.2 read a scalar differently the parameter is `Ambiguous`. Number units are
     `Unknown`, text units `NotApplicable`. NaN and infinities are kept as `NonFinite` with a finding.
   - Extrinsics are `FrameTransform`s only where the file names both frames and the mapping. Kalibr's
     `T_cam_imu` and `T_cn_cnm1` qualify: the entry is the parent, the frame the key names (`imu`, or the camera
     before it, if the file has it) the child. Kalibr documents `T_a_b` as mapping `b`'s coordinates into `a`'s,
     so direction is `Known(child_to_parent)` citing the matrix; the matrix is a `HomogeneousMatrix` of sixteen
     floats, row-major by its nesting, translation unit `Unknown`, validity static. Nothing is split into
     rotation and translation, normalised, inverted or converted at parse time. All of a file's transforms are
     in one `FrameGraph` (scope `()`, cited as the whole file). OpenCV's `R`, `T`, `CameraExtrinsicMat` and
     Kalibr's `T_i_b` name no frame, or no frame the file declares, so they stay parameters and a
     `frame_unresolved` finding says so. No `Frame` records: no supported format declares axes.
   - Two calibrations for one subject (two YAML documents, a repeated `cam0`) are two records and a
     `duplicate_subject` finding, never merged.
4. **Checks the file allows itself, as findings.** A matrix that does not hold `rows x cols x channels`
   numbers, a camera without the extrinsic its siblings have, a transform to an undeclared camera, a pair joined
   twice, a loop (frames joined by more than one path), a graph in separate groups. Whether two transforms
   agree, whether a distortion model has the right coefficient count, and which calibration or graph applied
   to a run are computed across records and bindings: `validate/`, `derived/` and MVL-37/38. A snapshot is the
   source plus this transform: its identity is the file's content id and the `FrameGraph` id, and each
   calibration cites its own entry, so a re-ingested or edited file never mutates an earlier record.
5. **Not here.** TF from bags and MCAP: the stream adapters expose `/tf` and `/tf_static` as undecoded payload
   rows, so there is nothing to build a graph from until MVL-21 decodes them; that is left to MVL-21 and
   MVL-37. URDF-declared joints are MVL-38's.
6. **Hostile input** costs findings. `max_bytes`, `max_depth`, `max_path_ratio`, `max_scalar_length` and
   `yaml_version` are `config`'s options with its defaults; `max_items` (200,000 values or elements) and
   `max_array_values` (100,000 numbers per array) are new, as XML and flat parsing would otherwise multiply
   memory by the file size. Aliases are never expanded. One chunk per file: a graph is read whole and a
   calibration file is small.

## Alternatives considered

- *Teach `config` the calibration formats.* A configuration snapshot is a value per node; a calibration is a
  subject with parameters and frames. Mixing them would put two kinds' lineage in one adapter version.
- *Copy `config`'s readers into `calibration`.* Two parsers to keep hostile-safe and byte-identical; the
  reason adapters share nothing is imports of each other, which a neutral package avoids.
- *Direction `Ambiguous` for Kalibr.* Kalibr's own documentation states `T_a_b`'s meaning, which is the format
  speaking, not a community convention; `Ambiguous` stays for sources that do not.
- *`Frame` records with `Unknown` axes.* They would add a record per frame that says nothing.
- *Compose Kalibr's chain and compare.* A computed result is an inference: `derived/`, not `model/` (AGENTS.md 8).

## Consequences

Any adapter reading JSON, TOML or YAML can use `structured` and stay a leaf. Adding a calibration dialect
is a matcher in `_formats.py` and, if it declares extrinsics with named frames, a transform reader in
`_emit.py`. A deployment that stores TF in bags gets no graph until the stream decoders land. Revisit if a
format declares axes (add `Frame` records) or if two supported formats need different parent/child slots
than "the entry is the parent".

# 0019 — Machine context: machines, hardware, software and calibration

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-68 (sub-issue of MVL-1)

## Context

ADR 0017 fixed the record envelope and named the `machine` family. MVL-68 defines its records: the
machines evidence declares, what they are made of, what software ran on them and how they were
calibrated. Earlier ADRs constrain it:

- ADR 0003: a machine's identity is a tier-3 logical id, declared and never inferred. Two robots
  with byte-identical URDFs are two robots.
- ADR 0004: identities, versions and calibration values are always `Knowledge`-wrapped.
- ADR 0007 and ADR 0015: frames are declared per source graph, and transforms are
  `FrameTransform` records with their direction wrapped.
- ADR 0014: every kind of software identity has its own type, and kinds never meet.
- ADR 0017 §5: one record-level `EvidenceRef` per record; anything combining sources is derived.
- ADR 0017 §7: after the M1 gate, adding a field to a record kind is a schema change whose
  migration must be lossless. Old records cannot say what a field added later would have held.

The sources vary widely. A PX4 log observes its vehicle's hardware UUID, board, sensor device ids,
firmware commit and sensor calibration. A URDF describes a robot model with links and joints and
names no machine. A manifest states a robot's fleet id, serial, payloads and software. A Kalibr
camchain states camera intrinsics and camera-to-IMU transforms, and a hand-eye file states one
transform without saying which way it maps. The records must hold all of this as declared, keep a
tool change or a recalibration visible, and never make a machine out of a model name.

## Decision

1. **`Machine`: a robot or vehicle that one piece of evidence declares by at least one identifier.**
   Kind `machine`, family `machine`.
   - The declaration is a manifest's robot entry or a fleet register's row (`stated`), or the
     vehicle information a log records about the machine that wrote it (`observed`).
   - `identifiers` lists every id the declaration gives the machine (§2), and must not be empty.
     A declaration with no identifier declares no `Machine`: the other records then say `machine`
     is `Unknown` or `NotCovered`.
   - `manufacturer` and `model: Knowledge[str]` are the declared maker and product.
   - Never from content. A URDF's `<robot name>`, a hostname, a topic prefix and a folder name name
     no machine. Equal ids in two declarations are two records; linking them is MVL-35's.
2. **Declared identifiers.** `Identifiers = tuple[Knowledge[LogicalId], ...]`, shared with `Site` and
   `Asset` (MVL-69).
   - Each element is `Known`, citing where the id is stated, or `Ambiguous` where the evidence gives
     conflicting readings of one id (two serials for one robot in one file).
   - No id repeats. Elements are sorted by namespace, then value (for `Ambiguous`, its first
     candidate's), so equal declarations serialise identically.
   - Ids that share a declaration are the evidence identity resolution links by: a manifest that
     gives `("manifest", "spot-07")` and `("serial", "BD-10470012")` states that they are one robot.
3. **`HardwareConfiguration`: what one declaration says a machine is physically made of.** Kind
   `hardware_configuration`, family `machine`.
   - The declaration is a URDF, SDF or MJCF description, or a manifest's hardware entry.
   - `machine: Knowledge[LogicalId]` is the machine it describes. A URDF has no place to name one,
     so a URDF's configuration has it `NotCovered`.
   - `name: Knowledge[str]` is the name the description gives itself (`<robot name="ur5e">`).
   - `revision: Knowledge[DeclaredVersion]` is the hardware revision it declares, as text. Text on
     both sides lets a calibration's revision match a configuration's (§6); SemVer would make equal
     text unequal (ADR 0014 §2).
   - A configuration is a snapshot and is never edited. A tool change, a new payload or a swapped
     sensor is another declaration and so another record, beside the first. Which configuration
     applied to which run, and from when, is a binding (MVL-38).
4. **`HardwareComponent`: one part a configuration declares.** Kind `hardware_component`.
   - The record-level provenance cites the part's own declaration (a URDF `<link>` element, a
     manifest's payload entry). `configuration: RecordId` is the configuration the same transform
     says it belongs to, as a stream names its run (ADR 0018 §2).
   - `category` is structural: the adapter knows which element or list it read. It is one of
     `link`, `joint`, `sensor`, `actuator`, `computer`, `power`, `payload` or `tool`.
   - `name` and `model: Knowledge[str]` are the declared name and make or model. `identifiers`
     (§2, possibly empty) holds ids such as a camera's serial or a PX4 device id.
   - `frame: Knowledge[FrameRef]` is the frame the part defines or is mounted at, in a declared
     graph. Kinematics stay where ADR 0015 put them: a URDF joint's origin is a `FrameTransform` in
     the URDF's `FrameGraph`, whose child is the joint's frame.
   - **Extension rule.** What only one category has (a joint's type and limits, a link's inertia
     and geometry, a sensor's rate or field of view) arrives as a new record kind that names the
     component, never as new fields here. A new kind needs no migration, because old packages
     simply have none (ADR 0017 §7).
5. **`SoftwareConfiguration`: the software one declaration says ran on a machine.** Kind
   `software_configuration`.
   - The declaration is a log's version information, a build-info file or a deployment manifest.
     `machine: Knowledge[LogicalId]` is the machine it ran on. `software` lists every item the
     declaration names, at least one, in its order, with no repeats.
   - A `SoftwareItem` has `name` and `device` (`Knowledge[str]`: firmware per device is an item per
     device) and one field per kind of identity:
     - `commit: Knowledge[GitCommit]`, the source revision it was built from;
     - `release: Knowledge[SemanticVersion | DeclaredVersion | FirmwareVersion]`, the version label;
     - `build: Knowledge[BuildId]`;
     - `digest: Knowledge[ModelCheckpointHash | ContainerImageDigest]`, a digest the source states
       for the artifact that ran.

     A value never sits in two fields, and every kind keeps its type.
   - **Missing identity is explicit.** An identity the evidence does not give is `Unknown`, citing
     where the adapter looked, or `NotCovered` where the format has no place for it. The adapter
     reports the gap as a finding (`<adapter>.software_identity_missing`, category `missing`,
     severity `warning`). Text is never blank.
6. **`Calibration`: what one declaration states about calibrating one subject.** Kind
   `calibration`.
   - The declaration is one camera's entry in a Kalibr camchain, a ROS `camera_info` file, a
     hand-eye result, or a log's calibration parameters for one sensor.
   - What it applies to, as declared: `machine`, `hardware_revision: Knowledge[DeclaredVersion]` (a
     calibration for revision `rev-C` only) and `subject: Knowledge[str]` (`cam0`, `accel0`).
     Recalibrating, or calibrating for another revision, is another record.
   - When, as declared: `performed`, `valid_from` and `valid_until`, each `Knowledge[Timestamp]` in
     whatever clock the declaration uses. They are not checked against each other.
   - `parameters`: `CalibrationParameter(name, value, unit)`, sorted by name, unique.
     - `name` is the declared key, verbatim; a nested key is its path as the adapter documents it
       (`camera_matrix/data`), and each innermost array of a nested array is its own parameter.
     - `value: Knowledge[str | tuple[Real, ...]]` is the declared numbers in source order, read with
       `float()`, or the declared text of a setting (`distortion_model: radtan`). Non-finite numbers
       are `Real` (ADR 0017 §8).
     - `unit: Knowledge[Unit]`; text has no unit, so its unit is `NotApplicable`.
     - Nothing is reordered or renamed. Which number is `fx` is the declared model's to say, read by
       consumers or by a derived transform.
   - `extrinsics: tuple[RecordId, ...]` lists the `FrameTransform` records the calibration declares,
     in its own `FrameGraph`, sorted. A hand-eye file that does not say which way its transform
     maps has its direction `Ambiguous` (ADR 0007 §3).
   - A calibration states at least one parameter or extrinsic.
7. **Not here.**
   - Bindings of configurations and calibrations to runs, including the ones a log declares about
     itself, are MVL-38's, as a record kind of their own. Nothing here points at a run.
   - Configuration snapshots (parameter files, launch arguments) are MVL-23's.
   - Identity resolution across declarations is MVL-35's.
   - Aligning a calibration's frames with a URDF's is MVL-37's.
   - Typed camera models (named intrinsics) can be added by MVL-26 as a new kind (§4).

## Alternatives considered

- **A machine from any robot-looking name** (URDF `<robot name>`, a hostname, a topic prefix).
  Convenient, but a URDF is shared by every robot of one model and a hostname is reused, so every
  such join would be a guess (ADR 0003).
- **One identifier per machine**, as `Run.machine` holds. A declaration often gives several: a
  fleet id and a serial, or several MAC addresses. Keeping only one discards exactly the evidence
  that links them.
- **One `Knowledge` field per identifier role** (`serial_number`, `fleet_id`, `mac`). Roles differ by
  source and a machine may have two ids of one role. The namespace already says which scheme an id
  belongs to.
- **Components nested inside the configuration.** One line per configuration, but a humanoid's
  forty links and joints would each need its own citation inside it, and later per-category
  records would have nothing to name. Software items stay nested: there are few, they are never
  referred to alone, and each identity field carries its own citation.
- **A mutable configuration with a history of changes.** A tool change would edit a record. That
  breaks immutability (non-negotiable 6) and confuses what one piece of evidence said.
- **Fixed software fields per identity kind, with no items** (one commit, one release, one digest
  per configuration). A robot that runs a firmware, a navigation stack and a policy has three
  commits.
- **A single `version: Knowledge[VersionPrimitive]` per software item.** A PX4 log states a commit
  and a release for the same firmware, and a policy has a release and a checkpoint digest.
- **Typed camera intrinsics now** (`fx`, `fy`, `cx`, `cy`, a distortion enum). Every format lays
  them out differently, and choosing a canonical layout at parse time is the reordering ADR 0015
  forbids. A typed kind can be derived or added later without a migration.
- **Calibration applicability by record id** (a `configuration: RecordId`). A calibration file does
  not know the tier-2 id of a URDF's configuration, and tier-2 ids do not survive lineages
  (ADR 0003). Declared machine ids and revision text work within one source and across sources.
- **A `platform` field** (drone, quadruped, manipulator, mobile robot). No source in scope declares
  it in a form we could hold without a vocabulary of our own; it is a derived classification.

## Consequences

- The machine context of a drone, a quadruped, a manipulator and a mobile robot is representable
  from the sources each typically has. `tests/unit/model/test_machine.py` builds all four.
- A tool change, a recalibration and a firmware update are each a new record beside the old ones.
  Binding runs to them is MVL-38's job, and the records make every binding checkable.
- `Site` and `Asset` (MVL-69) reuse the declared-identifier list.
- Adapters must know which of their fields are identifiers and document their namespaces.
- Per-category detail waits for new record kinds, so a consumer reading a URDF component gets its
  name, category and frame but not its joint limits until MVL-24 adds them.
- Revisit if a source declares one part in two configurations and consumers need one record for
  it, if software items need to be referred to individually, or if a calibration's parameters need
  structure beyond named arrays of numbers and text.

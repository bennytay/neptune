# 0014 — The calibration history and drift consolidator

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-128

## Context

"Did the wrist camera's calibration move before the incident, and by how much?" needs three things the compiler states
separately and as declared: which sensor a `Calibration` is of, when it applied, and what it measured. A `Calibration`
(root ADR 0019 §6) names a machine by declared id and its subject by declared name (`cam0`, `wrist_camera`); it keys
no sensor thread (Ledger ADR 0003 §2, example B). Its numbers keep their declared order and units, which are
`Unknown` when the file states none (root ADR 0055 §3). A `FrameBinding` (root ADR 0050 §6) says which description
edge a calibration's extrinsic measures. Rotations are stored as declared, and composing, inverting or converting
them is a derived transform (root ADR 0015).

Getting this wrong is worse than saying nothing. Pairing two calibrations by a guessed sensor, subtracting a value
in `deg/s` from one in `rad/s`, filling an unstated `valid_from` from `performed`, or flagging "drift exceeds
tolerance" would each state something no record does, and a deployment decision would rest on it.

## Decision

### 1. Reading

`consolidate/calibration_records.py` parses with the compiler's strict readers (`calibration`,
`hardware_configuration`, `hardware_component` of category `sensor`, `frame_transform`, `frame_binding` of basis
`calibration`, `maintenance_event`, `requalification_record`, `timestamp_domain`); `consolidate/calibration.py`
(`memory.calibration`, version `1`, no configuration) decides. Malformed, inferred and conflicting records are
`calibration.malformed_record`, `inferred_record` and `record_conflict`, as in ADR 0010 §1. Declared text (a
subject, a component name, a revision) is read as its readings: one for `Known`, every candidate for `Ambiguous`,
none otherwise. Nodes are the identity consolidator's (`identity.node_threads`); a calibration's node is its anchored
configuration thread (ADR 0010 §1), and `unthreaded_id` or `ambiguous_anchor` when there is not exactly one.

### 2. Placement: the sensor, through identity and configuration chains

A calibration's **instant** is its `valid_from`, else its `performed`, when `Known` (the Ledger's thread order,
Ledger ADR 0003 §3). The sensor is found in three steps, each through stated values only:

1. **Machine** (identity chain): each machine its `machine` names that a Ledger thread declares.
2. **Configurations** (configuration chain): the `HardwareConfiguration`s whose anchor is cited by a configuration
   node that `memory.configuration` places on the machine (`has_configuration`, or one reading of
   `configuration_candidate`) over an interval containing the calibration's instant on its clock. Only where the
   chain places none of them are the `HardwareConfiguration`s that declare the machine read instead: they state no
   time, so a swapped sensor's old configuration would otherwise make every later calibration ambiguous. A configuration whose declared revision shares no reading with the calibration's
   `hardware_revision` is excluded (root ADR 0019 §3, §6); one where only some readings match is one reading.
3. **Sensor**: the sensor components of those configurations whose declared name is the calibration's subject,
   verbatim. A name selects only among one machine's configurations; it never keys a node. The sensor's nodes are
   the sensor threads its declared identifiers key.

The calibration is placed definitely when every route is through `Known` values and decided chain spans, and every
matching component has the same nodes. Otherwise each node is a reading: `calibration_candidate` claims and
`calibration.ambiguous_sensor`. No machine, no subject, no configuration, no matching sensor or a sensor with no
identifier thread are `calibration.unplaced`, `no_configuration`, `sensor_not_in_configuration` and
`unthreaded_sensor`, and no claim. `memory.calibration` runs after `memory.configuration` (ADR 0003 §4).

### 3. Frame-graph consistency and history

For each sensor frame (in its configuration's graph) and each binding naming the calibration as `Known` with an edge
in that graph, the binding **contradicts** the graph when its edge does not touch the sensor's frame, names a frame
the graph does not declare, or gives the child a parent other than the one the graph declares. A graph that states
nothing about the edge does not contradict it, and an edge of another graph (a calibration file's own) says nothing
about this one (root ADR 0007). A contradicted calibration is a `calibration_candidate` citing the binding and the
description's own edges, with `calibration.frame_disagreement`. A binding that names the calibration only as one
`Ambiguous` candidate is `ambiguous_binding` and is not read.

Placed calibrations form a **series** per sensor node and **kind**: the parameter names they declare.
Recalibrating the same quantities is the same kind; a hand-eye result and a camera-intrinsics file for one camera
are two series. Each series is ordered by instant on one clock (`clock_split` across clocks). Definite calibrations
order it; calibrations at one instant are `same_instant` candidates and never ordered by record id. A candidate is
placed in time but orders nothing: it ends no calibration, and two definite calibrations with a candidate between
them are not known to be consecutive (`drift_undecided`). An untimed calibration is `calibration.untimed` and in no
order.

`calibrated_with(sensor → calibration)` holds from a stated `valid_from` only (`validity_unstated`, or
`ambiguous_validity`, and no interval otherwise; `performed` never becomes a validity bound). It ends at a stated
`valid_until`, is `open` where the calibration states it has none (`KnownAbsent`), and otherwise ends at the stated
`valid_from` of the next definite calibration of its series, as a machine's configuration span ends at the next
placement (ADR 0010 §2); the last is `open`. A next calibration that states no `valid_from` leaves the end unstated:
`end_unstated`, and no interval. An `Ambiguous` `valid_until` gives one `calibration_candidate` per reading. An end not after its start is
`untimeable_window`. `assertion_kind` is the calibration's.

### 4. Drift

Between consecutive calibrations of a series (single at both instants), every value both declare alike gives one
`drift(sensor → delta)` claim over `[earlier instant, later instant)`, `observed` (a fact about two records, as
`not_covered_by_authorisation` is, ADR 0010 §5), citing both calibrations (and, for an extrinsic, both bindings and
transforms). The **`delta`** value type (`Delta`) holds `earlier` and `later` (the two record ids), what was compared
and `values`: `later - earlier` component by component, by IEEE-754 subtraction (correctly rounded, so
deterministic), in declared order.

- **Parameters**: same name, `Known` numbers of equal length, finite, and `Known` equal units. The unit is the
  literal's.
- **Extrinsics**: the transforms both bind to one description edge (edges only one binds are not compared), with the same frame names, `Known` equal
  direction and the same form: a `Pose`'s translation (equal `Known` units) and rotation (same kind and `Known` equal
  order and convention, layout, or sequence and mode; equal units for Euler angles and rotation vectors); a
  `HomogeneousMatrix`'s nine rotation entries and three translation entries by its `Known` layout (equal
  `translation_unit`). Quaternion and matrix entries have no unit: the literal's unit is `not_applicable`.
- **Never converted.** Different units are `calibration.unit_mismatch` and no delta: the compiler records no
  declared conversion between units (ADR 0013 §6 leaves SI normalisation records undefined), so there is nothing to
  apply. Unstated units, values, shapes, non-finite numbers, different interpretations and changed text settings
  (`setting_changed`: the numbers may follow another model, so no parameter delta for that pair) are findings.
- **No judgement.** A zero delta is stated like any other; "exceeds tolerance" is a Deploy evidence-pack rule or a
  G4 inference.

A component-wise rotation difference is not a rotation angle: a quaternion and its negation are one rotation with a
large difference. The angle needs composition and inversion, which root ADR 0015 leaves to derived transforms.

### 5. `calibrated_by`

A `maintenance_event` whose `configuration` (the as-maintained configuration it states resulted, root ADR 0051 §2)
or a `requalification_record` whose `configuration` (the one it is bound to) names a calibration's configuration
node gives `calibrated_by(calibration → record)`, `stated`, from its `performed`, `open`. `related` is not read: it
names what a record relates to (an incident, a faulty calibration), not what it produced. An `Ambiguous`
configuration naming one is `ambiguous_producer`; no `performed` is `untimed_producer`; neither is claimed.

### 6. Vocabulary and contract

`calibrated_with` and `calibration_candidate` (sensor → configuration), `calibrated_by` (configuration → record)
and `drift` (sensor → delta) join `CORE_PREDICATES`, all `many`. `has_calibration` (`one`) stays for a single
calibration in force; it cannot widen to `many`, and a sensor holds several series at once. `ValueType.DELTA` and
`#/$defs/Delta` join the schema. `VOCABULARY_VERSION = 9` (5 and 6 are the run threads' and the time-domain registry's, ADR 0009 and 0011; 7 and 8 are taken by vocabularies released first) and
graph-schema **1.7.0**, a minor release: every earlier golden validates and passes the suite, and the golden plan is
unchanged.

## Alternatives considered

- **A sensor from the calibration's frame binding alone.** Kalibr binds `cam0 → imu` with the camera as parent, so
  the frame says which pair, not which sensor; intrinsics would be attributed to the IMU.
- **A series per machine and subject name, with no sensor.** Names would key history across configurations and
  swapped sensors, which the Ledger refuses (example B); the sensor's declared identifier does not.
- **`has_calibration` as the history predicate.** `one` would make the resolver close a camera's intrinsics with its
  hand-eye result.
- **`performed` as the start of validity.** A calibration measured on Monday may be put into service on Friday; the
  Ledger orders by it, which is all this does with it.
- **Differences in SI.** Converting `deg/s` to `rad/s` is a derived normalisation (ADR 0013 §6) Memory has no record
  of; a delta between unconverted numbers in different units is meaningless.
- **One drift claim per component, as a `quantity`.** A claim has no qualifier to say which parameter or edge a
  number is; the delta names both records and what it compares.
- **Relative rotation angle.** Needs composition, inversion and trigonometry over floats: derived (root ADR 0015).

## Consequences

- Context and Deploy can answer "which calibration was in force, since when, produced by what, and how did it move"
  from claims, with every refusal visible as a finding.
- The compiler's calibration adapter states no machine, units or times (root ADR 0055 §3), so a bare Kalibr or ROS
  file is `calibration.unplaced` until a source or manifest declares its machine, and gives no unit-bearing delta or
  interval until one states them. A URDF sensor with no declared identifier has no sensor thread and so no history.
- When the compiler records declared unit conversions, `unit_mismatch` becomes a delta through them (a new version of
  this consolidator); when MVL-130 maps clocks, `clock_split` series join.

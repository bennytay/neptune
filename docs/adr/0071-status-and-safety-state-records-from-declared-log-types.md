# 0071 — Status and safety-state records from declared log types

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-204
- Extends: ADR 0018 (runs and streams), ADR 0068 §1 (decoding by the declared definition), ADR 0048
  §3 (flight-log streams), ADR 0023 §1 and ADR 0037 §1 (growth by addition); schema version 10

## Context

Memory's event index (MVL-134, Memory ADR 0013) turns stated events into graph nodes, but reads
records only. A bag's `/diagnostics`, a PX4 log's logged messages and an ArduPilot `ERR` are series
rows, so the e-stop and the collision inside the acceptance corpus's incident bag (MVL-181) never
reach it. Since ADR 0068 the ROS payloads decode into columns, but a `DiagnosticArray`'s key/values
sit under two array levels and have no column, and nothing says which rows are statuses.

The forces:

- **Evidence, not interpretation.** A status is what the message says; reading "this topic is an
  e-stop" from `/estop` is a guess (non-negotiable 8). What a type *declares* (its name, its
  fields, its constants) is in the source's own definition.
- **Exact citations.** Memory's events must resolve to the message's bytes, and one array holds
  many statuses (ADR 0017 §5: finer locators, never counters).
- **Clocks.** A bag carries a recorder clock and a publisher's header stamp that may disagree by
  minutes (the corpus's cell PC runs 96.7 s ahead). Nothing may pick one or convert to UTC.
- **Volume.** A diagnostics aggregator publishes hundreds of statuses a second, nearly all `OK`;
  a safety topic at 10 Hz is the same state over and over. One adapter call's reply is bounded
  (ADR 0030, 64 MiB).
- **Every robot.** Arms, mobile bases, autonomous vehicles, aerial and marine vehicles all log
  statuses; no morphology may be assumed.

## Decision

1. **Two `run`-family kinds, since schema version 10**, both `observed`, citing the message:
   - `status_report {stream, convention, times, level, level_names, name, message, hardware_id,
     values}`: one status a message reports. `convention` is `ros_diagnostic_status` (a
     `diagnostic_msgs/DiagnosticStatus`, alone or an item of a `DiagnosticArray`),
     `px4_logged_message` (ULog `L` and tagged `C`), `ardupilot_message` (DataFlash `MSG`) or
     `ardupilot_error` (`ERR`).
   - `safety_state {stream, declared_type, field, condition, times, value, value_names}`: one
     sample of a field its declared type defines as a stop or safety state; `condition` is
     `emergency_stop`, `fault` or `safety_mode`.
   Version 9 is the manifest run declarations (ADR 0072, MVL-205); a package with no status record
   keeps its lower version (ADR 0037 §1).
2. **Recognised by declared type and shape, never by name.** The stream's declared schema name
   selects; its parsed definition must then have exactly the fields the type defines, or the
   stream gives no records and one `status_definition_unrecognised` finding (unsupported, info):
   - ROS (MCAP, rosbag1, rosbag2 sqlite3; ros1msg, ros2msg, ros2idl):
     `diagnostic_msgs/DiagnosticArray` (a leading `std_msgs/Header`, then `DiagnosticStatus[]`),
     `diagnostic_msgs/DiagnosticStatus` (`level` int8/uint8, `name`, `message`, `hardware_id`,
     `KeyValue[] values` of `key`, `value`).
   - A fixed catalogue of declared safety types (`neptune.adapters.rosmsg.status.SAFETY_TYPES`,
     part of the adapters' transform): `industrial_msgs/RobotStatus` `e_stopped.val`
     (emergency_stop) and `in_error.val` (fault), `ur_dashboard_msgs/SafetyMode` `mode`,
     `autoware_auto_system_msgs/EmergencyState` `state` (safety_mode), `husky_msgs/HuskyStatus`
     `e_stop`, and PX4's `actuator_armed` `manual_lockdown` (the kill switch). An arm, two arms'
     safety controllers, an autonomous vehicle, a mobile base, and every PX4 vehicle.
   - PX4 ULog logged messages by the ULog specification; ArduPilot `MSG` (a text `Message`) and
     `ERR` (integer `Subsys`, `ECode`) by their `FMT` labels and format characters.
   - A topic's name is never read: `/estop` of `std_msgs/Bool` gives nothing. A name-based proposal
     would be a derived table; none is built. A manifest binding of a topic to a safety field needs
     manifest stream bindings, which the manifest schema does not have yet: a follow-up.
3. **Values as stated.** `level` is the integer on the wire. `level_names` and `value_names` are the
   names the stream's own definition declares for that value as constants (`byte ERROR=2`; IDL
   constants in rosidl's `<Name>_Constants` module), sorted, every synonym kept (TriState's
   `TRUE`, `ON`, `ENABLED`, ...), cited `stated` to the definition: `Known(())` where it names
   other values only, `Unknown` where it names none; ULog's level names are its specification's
   (`'3'` is `ERR`). Text is verbatim; a blank or non-UTF-8 field is `Unknown`, and a key/value
   list holding one is `Unknown` whole. A field the format has no place for is `NotCovered`
   (ArduPilot states no level; ULog no name or hardware id). Integer fields of a format without
   key/values are its `values` by label (`ERR`'s `Subsys`, `ECode`; a `C` message's `tag`).
   Booleans stay booleans; integers stay integers. To make it possible, `rosmsg.definitions`
   keeps integer and boolean constants on their type (`MessageDef.constants`); nothing else of a
   definition changes, and constants take no bytes.
4. **Times as the row holds them.** `times` is the sample's time on each of its stream's clocks,
   in the stream's order (MCAP: `log_time`, `publish_time`, a leading header's stamp; rosbag1 and
   rosbag2: the bag's time and the stamp; flight logs: the boot clock), each its own
   `TimestampDomain`, `Unknown` or `NotCovered` as the row's state is. Nothing is converted or
   chosen.
5. **Exact evidence.** A record cites its row's message: the same locator steps. A status inside an
   array, or one field of a safety type, adds one `byte_range` step: its bytes inside the bytes the
   row's last step cites (the Message record, the bag's message record, the sqlite cell). Its
   `times` then cite the message. Payloads are read whole by the declared definition
   (`codec.decode_value`), under the decoder's bounds (`max_array_items`, `max_message_bytes`, the
   walk budget, nesting), inside the sandbox, with the column decoding (ADR 0068 §1).
6. **Volume.** A status at the level its definition names `OK`, and a safety state at the value its
   definition names normal (`FALSE`, `NORMAL`; a boolean's `false`), stays a row only; config
   `nominal_status_records` (default off) writes them too. A definition that names no normal
   value has every sample written. A ROS adapter call writes at most `max_status_records` (8,192;
   about 20 MB of reply); past it, and for a payload that does not read whole, a
   `status_not_recorded` finding per stream and call counts what was left out by reason (limit or
   corrupt, warning): the one place the records depend on the plan's cuts, and only past the
   bound. Flight logs plan status rows at a table row's weight, so a piece's records stay bounded
   without a cap.
7. **New lineage.** `mcap`, `rosbag1` and `rosbag2` become 0.3.0, `flightlog` 0.2.0.

## Alternatives considered

- **A pass over committed series.** Key/values have no column (two array levels), and a pass that
  re-reads payloads would parse untrusted bytes outside the sandbox (ADR 0068's rejection).
- **Topic-name rules as canonical records** (`/estop`, `*/safety*`): inference on `model/`.
- **Every `OK` status a record by default.** Hundreds of records a second from an aggregator,
  replies past the sandbox's bound; the OK statuses stay rows (`status[].level` is a column) and
  are one config flag away.
- **Records on change only.** Needs state across calls, so the output would depend on where the
  plan cuts the source (ADR 0034 §6).
- **One canonical clock for `times`.** Choosing `header.stamp` or `log_time` is the silent
  assumption the corpus's 96.7 s skew is built to catch.
- **A `level` enum mapped from names** (`ERROR` → error). Vendor vocabularies differ; Memory maps
  declared values by type and value in config (Memory ADR 0013 §3).
- **rosout (`rcl_interfaces/Log`, `rosgraph_msgs/Log`) too.** Log lines, not statuses, at a far
  higher rate; their fields are already scalar columns. Revisit if a consumer needs them as
  records.

## Consequences

- Memory's event index can read statuses and safety states as stated events with exact citations
  (a follow-up there: it reads neither kind yet); Context's vocabulary and the Ledger's projection
  need the kinds too.
- Packages with ROS or flight-log sources change id (new adapter versions and config options);
  only those with a recognised status write records of version 10.
- The safety catalogue is maintained with the adapters; adding a type is a new adapter version.
- The Ledger's schema-version registry gains 10 after 9's. `status_report.stream` and
  `safety_state.stream` are its first `stream` hot filter: generated migration 0012 adds the
  `stream_ids` column (Ledger ADR 0009).
- Revisit when a manifest can bind a topic to a safety field, when a consumer needs OK statuses
  as records by default, or when a status type nests deeper than these.

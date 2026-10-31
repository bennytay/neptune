# ruff: noqa: E501, RUF001
"""Harbor DC's mapping file, ``static/neptune.mappings.yaml`` (#200).

Harbor is shaped unlike Plant 7: a nested JSON CMMS (``rows:``), a roster that splits date from time
(templated ``time`` / ``until``), multi-value cells (``split:``) and the ``opcua`` and
``ros2_diagnostics`` decoders. All of it is mapping format; no mapper code names Harbor.
"""

MAPPINGS = """\
# Harbor DC: how every export becomes facts. No code reads Harbor; this file does.
mappings: 1

types:
  - {contains: robot, type: robot}
  - {type: machine}
tags:
  table: cmms/assets.json
  rows: assets
  name: name
  type: type

tables:
  - file: cmms/assets.json
    rows: assets
    source: cmms
    subject: "{type|type}:{tag}"
    facts:
      - {predicate: serial, column: serial}
      - {predicate: name, column: name}
  - file: cmms/work_orders.json
    rows: workOrders
    source: cmms
    subject: "work_order:{id}"
    time: opened
    facts:
      - {subject: "{asset.tag|tag}", predicate: work_order, column: asset.tag, ref: "work_order:{id}"}
      - {predicate: wo_problem, column: summary}
      - {predicate: wo_action, column: action}
      - {predicate: wo_cause, column: cause}
      - {predicate: wo_by, column: technician, ref: "person:{technician|lower}"}
      - {predicate: wo_loto, column: lockout}
      - {predicate: wo_note, column: notes}
  - file: changes/change_log.xlsx
    source: changes
    subject: "change:{Change ID}"
    time: Date
    facts:
      - {subject: "{Equipment|tag}", predicate: change, column: Equipment, ref: "change:{Change ID}"}
      - {predicate: name, column: What changed}
      - {predicate: change_from, column: From}
      - {predicate: change_to, column: To}
      - {predicate: change_by, column: Approved by, ref: "person:{Approved by|lower}"}
      - {predicate: change_reason, column: Reason}
  - file: roster/roster.csv
    source: roster
    subject: "person:{Employee|lower}"
    facts:
      - {predicate: name, column: Employee, once: Employee}
      - {predicate: has_role, column: Position, ref: "role:{Position|slug}", once: Employee}
      - {predicate: holds_qualification, column: Certifications, split: "|", ref: "qualification:{item}", once: Employee}
      - {predicate: on_shift, column: Crew, ref: "shift:{Crew}", time: "{Shift date}T{Clock in}", until: "{Shift date}T{Clock out}"}
      - {predicate: on_break, column: Break from, value: break, time: "{Shift date}T{Break from}", until: "{Shift date}T{Break to}"}

documents:
  - file: docs/*.pdf
    source: docs
    id: "document:{stem|until:_}"
    rule:
      pattern: '^§?\\s*(\\d+(?:\\.\\d+)+)\\s+(.+)$'
      id: "rule:{id|lower}-{1}"
      text: 2
    lift:
      - {pattern: '\\d+\\s?[–-]\\s?\\d+\\s?°C', predicate: states_limit, about: document}
      - {terms: {table: roster/roster.csv, column: Certifications, split: "|"},
         predicate: requires_qualification, ref: qualification, about: rule}

extractions:
  - file: photos/*.extraction.json
    source: photos
    predicate: note

feeds:
  - source: plc.sort1
    decoder: opcua
    subject: machine:sort-1
    fields:
      Sorter1.RunState: mode
      Sorter1.FaultCode: health
      Sorter1.DivertRateSetpoint: setpoint
      Sorter1.TotesSorted: {predicate: parts_out, subject: cell:sortation}
  - source: vendor.humanoid
    decoder: json
    subject: "robot:{payload.id}"
    fields:
      serial: serial
      firmware: firmware
      status: mode
      battery_pct: {predicate: battery, unit: "%"}
      zone: {predicate: in_zone, ref: zone}
      faults: health
      task.id: {predicate: current_task, state: task.state}
  - source: rmf.fleet
    decoder: json
    subject: "robot:{payload.robot.name}"
    fields:
      extra.serial_number: serial
      extra.software_version: firmware
      robot.mode.mode: mode
      robot.battery_percent: {predicate: battery, unit: "%"}
      robot.location.level_name: {predicate: in_zone, ref: zone}
      robot.task_id: {predicate: current_task, state: extra.task_state}
  - source: ros.insp1
    decoder: ros2_diagnostics
    subject: robot:insp-1
    fields:
      insp1/locomotion/gait: mode
      insp1/locomotion/zone: {predicate: in_zone, ref: zone}
      insp1/locomotion/level: health
      insp1/power/battery_pct: {predicate: battery, unit: "%"}
      "insp1/thermal_scan */max_temp_c": {predicate: temperature, subject: "machine:{hardware_id}", unit: "°C"}
"""

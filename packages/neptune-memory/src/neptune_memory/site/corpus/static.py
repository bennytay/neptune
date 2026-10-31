# ruff: noqa: E501, RUF001
"""The static half of the Plant 7 corpus: layout, mapping file, CMMS, change log, roster (spec §9).

Every file is plain text written verbatim, so the bytes are the same on every host. The
column headers are the contract with the mappers (#184).
"""

LAYOUT = """\
# Plant 7 assembly hall: the declared site. Everything here is stated.
site:
  id: site:p7
  name: Plant 7 assembly hall
clock:
  name: P7 local
  zone: Europe/Berlin
  note: stated by the site; times are never converted
areas:
  - {id: area:inbound, name: Inbound, part_of: site:p7}
  - {id: area:assembly, name: Assembly, part_of: site:p7}
cells:
  - {id: cell:c3, name: Cell C3, part_of: area:assembly}
zones:
  - {id: zone:aisle-a, name: Aisle A, part_of: area:assembly}
  - {id: zone:aisle-b, name: Aisle B, part_of: area:assembly}
  - {id: zone:c3-fence, name: Cell C3 fence, part_of: area:assembly}
  - {id: zone:charging, name: Charging bay, part_of: area:inbound}
machines:
  - {id: machine:cv-2, name: Conveyor CV-2}
robots:
  - {id: robot:arm-c3, name: Arm C3, located_in: cell:c3}
  - {id: robot:amr-01, name: AMR 01}
  - {id: robot:amr-02, name: AMR 02}
  - {id: robot:amr-03, name: AMR 03}
topology:
  - {subject: machine:cv-2, predicate: feeds, object: cell:c3}
  - {subject: cell:c3, predicate: feeds, object: zone:aisle-a}
  - {subject: robot:arm-c3, predicate: depends_on, object: machine:cv-2}
  - {subject: robot:amr-01, predicate: depends_on, object: cell:c3}
  - {subject: robot:amr-02, predicate: depends_on, object: cell:c3}
  - {subject: robot:amr-03, predicate: depends_on, object: cell:c3}
sources:
  - {id: plc.cv2, name: CV-2 PLC edge node (Sparkplug B style), report_interval: 5}
  - {id: fleet, name: AMR fleet manager (VDA 5050 style state), report_interval: 5}
  - {id: arm.c3, name: Arm C3 controller log, report_interval: 5}
  - {id: cmms, name: Maintenance system export}
  - {id: roster, name: Shift roster and qualifications}
  - {id: docs, name: Controlled documents}
  - {id: photos, name: "Shift photos, recorded model extraction"}
"""

MAPPINGS = r"""# How Plant 7's files become facts (spec §12). The mappers know no site; this file does.
mappings: 1

types:
  - {contains: robot, type: robot}
  - {type: machine}
tags:
  table: cmms/assets.csv
  name: Name
  type: Type

tables:
  - file: cmms/assets.csv
    source: cmms
    subject: "{Type|type}:{Asset}"
    facts:
      - {predicate: name, column: Name}
      - {predicate: serial, column: Serial}
  - file: cmms/work_orders.csv
    source: cmms
    subject: "work_order:{WO}"
    time: Opened
    facts:
      - {subject: "{Equipment|tag}", predicate: work_order, column: Equipment, ref: "work_order:{WO}"}
      - {predicate: wo_opened, column: Opened}
      - {predicate: wo_closed, column: Closed}
      - {predicate: wo_problem, column: Problem}
      - {predicate: wo_action, column: Action}
      - {predicate: wo_cause, column: Cause}
      - {predicate: wo_loto, column: LOTO}
      - {predicate: wo_note, column: Note}
      - {predicate: wo_by, column: Done by, ref: "person:{Done by|lower}"}
  - file: changes/changes.csv
    source: cmms
    subject: "change:{Change}"
    time: When
    facts:
      - {subject: "{Equipment|tag}", predicate: change, column: Change, ref: "change:{Change}"}
      - {predicate: name, column: Parameter}
      - {predicate: change_from, column: From, unit: "{Unit}"}
      - {predicate: change_to, column: To, unit: "{Unit}"}
      - {predicate: change_by, column: Requested by, ref: "person:{Requested by|lower}"}
      - {predicate: change_reason, column: Reason}
  - file: roster/shift_roster.csv
    source: roster
    subject: "person:{Person|lower}"
    facts:
      - {predicate: name, column: Person, once: Person}
      - {predicate: has_role, column: Role, ref: "role:{Role|slug}", once: Person}
      - {predicate: on_shift, column: Shift, ref: "shift:{Shift}", time: Start, until: End}
      - {predicate: on_break, column: Break start, value: break, time: Break start, until: Break end}
  - file: roster/qualifications.csv
    source: roster
    subject: "person:{Person|lower}"
    time: Granted
    facts:
      - {predicate: holds_qualification, column: Qualification, ref: "qualification:{Qualification}"}

documents:
  - file: docs/*.pdf
    source: docs
    id: "document:{stem|until:_}"
    rule:
      pattern: '^§?\s*(\d+(?:\.\d+)+)\s+(.+)$'
      id: "rule:{id|lower}-{1}"
      text: 2
    lift:
      - pattern: '(?<![\w.–-])\d+(?:\.\d+)?\s?[–-]\s?\d+(?:\.\d+)?\s?(?:N|Nm|mm|cm|m|m/s|kg|bar|°C|%)(?![\w/])'
        predicate: states_limit
        about: document
      - terms: {table: roster/qualifications.csv, column: Qualification}
        predicate: requires_qualification
        ref: qualification
        about: rule

extractions:
  - file: photos/*.extraction.json
    source: photos
    predicate: note

feeds:
  - source: plc.cv2
    decoder: sparkplug
    subject: machine:cv-2
    fields:
      state: mode
      alarm: health
      speed_setpoint: setpoint
      "*/parts_out": {predicate: parts_out, subject: cell:c3}
  - source: fleet
    decoder: vda5050
    subject: "robot:{topic[-2]}"
    fields:
      serialNumber: serial
      version: firmware
      operatingMode: mode
      batteryState.batteryCharge: {predicate: battery, unit: "%"}
      errors: health
      zone: {predicate: in_zone, ref: zone}
      orderId: {predicate: current_task, state: orderState}
  - source: arm.c3
    decoder: controller_log
    subject: robot:arm-c3
    fields:
      mode: mode
      fault: health
"""

ASSETS = """\
Asset,Name,Type,Serial,Location
MR-1141,Conveyor CV-2,conveyor,SN-31107,Assembly hall
MR-1142,Mobile robot 2,mobile robot,SN-88412,Assembly hall
MR-1143,Arm C3,robot arm,SN-52090,Cell C3
"""

WORK_ORDERS = """\
WO,Asset,Equipment,Opened,Closed,Problem,Action,Cause,Done by,LOTO,Note
WO-5521,MR-1141,CV-2,2026-10-19T15:20:00,2026-10-19T16:05:00,Belt edge fraying,Belt replaced,Belt worn at splice,Okafor,yes,tension per SOP-CV-04 rev B
WO-5530,MR-1141,CV-2,2026-10-20T10:08:00,2026-10-20T10:41:00,CV-2 stopped; jam at PE3,Cleared jam at PE3,belt tracking off,Lindqvist,yes,
"""

CHANGES = """\
Change,Equipment,Parameter,From,To,Unit,When,Requested by,Reason
CHG-311,CV-2,speed setpoint,0.80,0.95,m/s,2026-10-20T05:40:00,Patel,Raise throughput for shift A
"""

SHIFT_ROSTER = """\
Person,Role,Shift,Start,End,Break start,Break end
Okafor,maintenance tech,A,2026-10-19T06:00:00,2026-10-19T14:00:00,,
Lindqvist,maintenance tech,A,2026-10-19T06:00:00,2026-10-19T14:00:00,,
Patel,line lead,A,2026-10-19T06:00:00,2026-10-19T14:00:00,,
Okafor,maintenance tech,A,2026-10-20T06:00:00,2026-10-20T14:00:00,2026-10-20T10:30:00,2026-10-20T11:00:00
Lindqvist,maintenance tech,A,2026-10-20T06:00:00,2026-10-20T14:00:00,,
Patel,line lead,A,2026-10-20T06:00:00,2026-10-20T14:00:00,,
"""

QUALIFICATIONS = """\
Person,Qualification,Granted
Okafor,arm-reset-c3,2025-03-12
Okafor,conveyor-lockout,2024-11-04
Lindqvist,conveyor-lockout,2025-06-23
"""

MISSION = """\
# Plant 7 mission: what the hall is for, and what any change must respect. Everything here is stated.
goals:
  - {id: goal:c3-output, name: Cell C3 output, metric: parts out of cell C3 per shift, target: ">= 2400"}
  - {id: goal:amr-uptime, name: AMR fleet availability, metric: share of shift each AMR is in AUTOMATIC, target: ">= 95 %"}
  - {id: goal:safe-recovery, name: Every stop cleared safely, metric: jams cleared under lockout by a qualified person, target: "100 %"}
constraints:
  - {id: constraint:headcount, name: "No added headcount; cross-training and shift or break changes are allowed"}
  - {id: constraint:floor-space, name: "No new floor space; layout changes stay inside the existing areas"}
  - {id: constraint:capex, name: "No new equipment purchases this quarter"}
  - {id: constraint:safety, name: "Never bypass or change a safety function (e-stops, interlocks, fences, speed and separation limits)"}
"""

FILES: dict[str, str] = {
    "static/layout.yaml": LAYOUT,
    "static/mission.yaml": MISSION,
    "static/neptune.mappings.yaml": MAPPINGS,
    "static/cmms/assets.csv": ASSETS,
    "static/cmms/work_orders.csv": WORK_ORDERS,
    "static/changes/changes.csv": CHANGES,
    "static/roster/shift_roster.csv": SHIFT_ROSTER,
    "static/roster/qualifications.csv": QUALIFICATIONS,
}

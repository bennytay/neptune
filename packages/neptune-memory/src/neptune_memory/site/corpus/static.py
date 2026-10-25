# ruff: noqa: E501
"""The static half of the Plant 7 corpus: layout, CMMS, change log, roster (spec §9).

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
  - {id: photos, name: Shift photos, recorded model extraction}
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

FILES: dict[str, str] = {
    "static/layout.yaml": LAYOUT,
    "static/cmms/assets.csv": ASSETS,
    "static/cmms/work_orders.csv": WORK_ORDERS,
    "static/changes/changes.csv": CHANGES,
    "static/roster/shift_roster.csv": SHIFT_ROSTER,
    "static/roster/qualifications.csv": QUALIFICATIONS,
}

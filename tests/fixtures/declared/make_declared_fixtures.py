"""Write the context fixtures (ADR 0063): site manifests, registers, procedures, briefs, orders.

Run ``uv run python tests/fixtures/declared/make_declared_fixtures.py`` to rewrite them. Every file
is written from the literals below, so the bytes never change unless this script does. They span
embodiments: a warehouse AMR fleet's site manifest and asset register, a manipulator cell's SOP,
an inspection drone's task brief, a marine ROV's requirements, an AMR work order; and the
malformed shapes the declared-records pass must turn into findings, never failures.
"""

from pathlib import Path
from typing import Final

HERE: Final = Path(__file__).parent

FILES: Final[dict[str, str]] = {
    # A warehouse AMR site manifest: one site, its nested assets, and a root-level task.
    "warehouse_amr_site.yaml": """\
site:
  id: WH-3
  name: Riverside fulfilment centre
  aliases: [Riverside DC, Building 3]
  location:
    latitude: -33.8121
    longitude: 151.0034
    crs: "EPSG:4326"
  assets:
    - id: DOCK-2
      name: Charging dock 2
      category: charger
    - id: AISLE-14
      name: Aisle 14
      category: aisle
tasks:
  - id: PICK-NIGHT
    name: Night replenishment
    objective: Move full pallets from receiving to aisle 14 before 06:00.
    site: WH-3
    assets: [AISLE-14, DOCK-2]
    machines: [AMR-07, AMR-09]
    requirements:
      - id: PICK-R1
        text: Robots shall keep 0.5 m from people in shared aisles.
""",
    # The same fleet's asset register: header declared through csv_header=first_row.
    "warehouse_amr_assets.csv": (
        "asset_id,name,category,site_id,parent_id,aliases,serial_number,cmms_id\r\n"
        "AMR-07,Tugger 7,autonomous mobile robot,WH-3,,Otto 7; T7,OTTO-1500-0042,EQ-4410\r\n"
        "AMR-09,Tugger 9,autonomous mobile robot,WH-3,,,OTTO-1500-0057,EQ-4411\r\n"
        "RACK-A1,Rack A1,pallet rack,WH-3,AISLE-14,,,\r\n"
        ",,unlabelled,WH-3,,,,\r\n"
    ),
    # A manipulator cell's SOP: labelled steps, one unlabelled numbered heading (a candidate).
    "manipulator_cell_sop.md": """\
# Cell 4 gripper change

Procedure ID: SOP-CELL-04
Site: PLANT-2
Asset: ARM-4

## Step 1: Lock out the cell

Press the e-stop and apply the lockout tag to the cell door.

## Step 2: Change the gripper

- Remove the four M6 bolts from the tool flange.
- Requirement CELL-SAF-3: The operator shall wear cut-resistant gloves.

### Torque

Tighten to 9 N·m.

## 3. Restore power

The cell must be cleared before the door closes.

```text
Step 9: not a step, a code sample
```
""",
    # An inspection drone's task brief: assets as a list, requirements by label.
    "inspection_drone_brief.md": """\
# Thermal inspection brief

Task ID: TB-2026-117
Title: String inspection, solar farm 2
Site: SOLAR-FARM-2
Assets: STR-14, STR-15
Robot: UAV-M30-03
Objective: Capture radiometric images of strings 14 and 15 at noon.

Requirement INS-1: The aircraft shall hold 25 m above the panels.
Requirement INS-2: Each panel shall appear in at least two images.

The pilot must keep visual line of sight.
""",
    # A marine ROV requirements document: a requirements table and a labelled line.
    "marine_rov_requirements.md": """\
# Hull survey requirements, ROV Kestrel

Task ID: HULL-SURVEY-9
Site: BERTH-12

| Requirement ID | Text | Task ID |
|----------------|------|---------|
| ROV-R1 | The ROV shall stay 1 m off the hull. | HULL-SURVEY-9 |
| ROV-R2 | Survey speed shall not exceed 0.3 m/s. | HULL-SURVEY-9 |

Requirement ROV-R3: Footage shall be recorded with a depth overlay.
""",
    # An AMR work order document: the request, not the work done.
    "amr_work_order.md": """\
# Replace drive wheel, Tugger 7

Work Order: WO-5531
Status: Open
Site: WH-3
Asset: AMR-07
Task ID: PICK-NIGHT
""",
    # Malformed: a section that is a scalar, an entry with neither id nor name, a non-decimal
    # latitude, and Neptune's own manifest shape is not read (see the second file).
    "context_malformed_manifest.yaml": """\
site:
  id: FIELD-7
  name: North paddock
  location:
    latitude: 51°30'N
    longitude: -0.12
assets: not a list
work_orders:
  - status: Open
  - id: WO-1
    title: Clear the fence line
""",
    "context_neptune_manifest.yaml": """\
neptune: 1
sites:
  - id: SHOULD-NOT-BE-READ
""",
    # Malformed: a label stated twice, differently; a requirement with no statement.
    "context_malformed_brief.md": """\
# Orchard row survey

Task ID: ORCH-5
Site: ORCHARD-NORTH
Site: ORCHARD-SOUTH

Requirement AG-1:
""",
}


def main() -> None:
    for name, text in FILES.items():
        (HERE / name).write_bytes(text.encode("utf-8"))


if __name__ == "__main__":
    main()

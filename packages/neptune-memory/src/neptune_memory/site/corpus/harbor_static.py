# ruff: noqa: E501, RUF001
"""The static half of the Harbor DC corpus: layout, JSON CMMS, XLSX change log, roster, documents, photo.

Harbor is deliberately shaped unlike Plant 7: the CMMS exports JSON (nested, one file for the asset
register and one for work orders), the change log is a workbook, the roster is one row per person per
day with times split from dates and certifications pipe-joined in one cell, and the plant regulation is
the "HSR" series. Every file is written verbatim, so the bytes are the same on every host.
"""

import hashlib
import json
import struct
import zlib

from neptune_memory.site.corpus import docs, harbor_workbook
from neptune_memory.site.corpus.harbor_mappings import MAPPINGS
from neptune_memory.site.derived.photos import PROMPT

LAYOUT = """\
# Harbor DC: the declared site. Everything here is stated.
site:
  id: site:harbor
  name: Harbor DC
clock:
  name: Harbor local
  zone: Europe/Rotterdam
  note: stated by the site; times are never converted
areas:
  - {id: area:sortation, name: Sortation hall, part_of: site:harbor}
  - {id: area:outbound, name: Outbound docks, part_of: site:harbor}
  - {id: area:storage, name: Goods-to-person storage, part_of: site:harbor}
cells:
  - {id: cell:sortation, name: Sortation line, part_of: area:sortation}
  - {id: cell:tote-pickup, name: Tote pickup station, part_of: area:sortation}
zones:
  - {id: zone:sort-hall, name: Sort hall floor, part_of: area:sortation}
  - {id: zone:dock-4, name: Dock 4, part_of: area:outbound}
  - {id: zone:dock-staging, name: Dock staging, part_of: area:outbound}
  - {id: zone:pick-aisle, name: Pick aisle, part_of: area:storage}
  - {id: zone:charging, name: Charging bay, part_of: area:storage}
machines:
  - {id: machine:sort-1, name: Sortation conveyor SORT-1, located_in: cell:sortation}
robots:
  - {id: robot:insp-1, name: Inspector 1 (legged)}
  - {id: robot:hum-01, name: Humanoid 01, located_in: cell:tote-pickup}
  - {id: robot:hum-02, name: Humanoid 02, located_in: cell:tote-pickup}
  - {id: robot:gtp-01, name: GTP 01}
  - {id: robot:gtp-02, name: GTP 02}
  - {id: robot:gtp-03, name: GTP 03}
topology:
  - {subject: machine:sort-1, predicate: feeds, object: cell:tote-pickup}
  - {subject: cell:tote-pickup, predicate: feeds, object: zone:dock-staging}
  - {subject: robot:hum-01, predicate: depends_on, object: machine:sort-1}
  - {subject: robot:hum-02, predicate: depends_on, object: machine:sort-1}
  - {subject: robot:gtp-01, predicate: depends_on, object: cell:tote-pickup}
  - {subject: robot:gtp-02, predicate: depends_on, object: cell:tote-pickup}
  - {subject: robot:gtp-03, predicate: depends_on, object: cell:tote-pickup}
sources:
  - {id: plc.sort1, name: SORT-1 PLC (OPC UA node values), report_interval: 10}
  - {id: vendor.humanoid, name: Humanoid vendor state API, report_interval: 10}
  - {id: rmf.fleet, name: GTP fleet (Open-RMF fleet states), report_interval: 10}
  - {id: ros.insp1, name: Inspector 1 ROS 2 diagnostics, report_interval: 30}
  - {id: cmms, name: HarborMX maintenance export}
  - {id: changes, name: Change log workbook}
  - {id: roster, name: Crew roster}
  - {id: docs, name: Controlled documents}
  - {id: photos, name: "Shift photos, recorded model extraction"}
"""

# Entities in the asset register are named the way the CMMS names them (AMR-17), not the way the fleet does
# (gtp-03); the shared serial is what lets Neptune propose that they are the same robot.
ASSETS = {
    "assets": [
        {
            "tag": "SORT-1",
            "name": "Sortation conveyor SORT-1",
            "type": "conveyor",
            "serial": "SRT-30055",
            "location": "Sortation hall",
        },
        {
            "tag": "INSP-1",
            "name": "Legged inspector INSP-1",
            "type": "legged robot",
            "serial": "LG-1180",
            "location": "Sortation hall",
        },
        {
            "tag": "AMR-17",
            "name": "Goods-to-person robot AMR-17",
            "type": "mobile robot",
            "serial": "GTP-4471",
            "location": "Storage",
        },
    ]
}

WORK_ORDERS = {
    "system": "HarborMX",
    "workOrders": [
        {
            "id": "WO-88219",
            "asset": {"tag": "SORT-1"},
            "summary": "Diverter D4 photo-eye misaligned",
            "opened": "2026-11-03T13:10:00",
            "closed": "2026-11-03T13:50:00",
            "technician": "Nowak",
            "cause": "sensor bracket loose",
            "action": "Realigned photo-eye and torqued bracket",
            "lockout": True,
            "notes": "per SOP-SRT-01 rev C",
        },
        {
            "id": "WO-88231",
            "asset": {"tag": "SORT-1"},
            "summary": "Inspector flagged hot drive motor M2",
            "opened": "2026-11-04T08:40:00",
            "closed": "2026-11-04T10:38:00",
            "technician": "Reyes",
            "cause": "motor M2 cooling fan fouled, thermal overload tripped",
            "action": "Cleaned fan, reset overload, restarted sorter",
            "lockout": True,
            "notes": "",
        },
    ],
}

CHANGE_HEADER = [
    "Date",
    "Change ID",
    "Equipment",
    "What changed",
    "From",
    "To",
    "Approved by",
    "Reason",
]
CHANGE_ROWS = [
    [
        "2026-11-03T16:30:00",
        "CHG-741",
        "INSP-1",
        "thermal camera calibration",
        "2026-08",
        "2026-11",
        "Haddad",
        "Quarterly calibration",
    ],
    [
        "2026-11-04T06:55:00",
        "CHG-742",
        "SORT-1",
        "divert rate setpoint",
        "7200",
        "8400",
        "Haddad",
        "Peak volume before the 11:00 vessel",
    ],
]

ROSTER = """\
Employee,Position,Crew,Shift date,Clock in,Clock out,Break from,Break to,Certifications
Reyes,maintenance electrician,B,2026-11-03,07:00:00,15:00:00,,,electrical-isolation|sorter-reset
Nowak,maintenance tech,B,2026-11-03,07:00:00,15:00:00,,,sorter-reset
Haddad,shift supervisor,B,2026-11-03,07:00:00,15:00:00,,,
Kim,dock lead,B,2026-11-03,07:00:00,15:00:00,,,
Reyes,maintenance electrician,B,2026-11-04,07:00:00,15:00:00,10:00:00,10:30:00,electrical-isolation|sorter-reset
Nowak,maintenance tech,B,2026-11-04,07:00:00,15:00:00,,,sorter-reset
Haddad,shift supervisor,B,2026-11-04,07:00:00,15:00:00,,,
Kim,dock lead,B,2026-11-04,07:00:00,15:00:00,,,
"""


def sop_srt01() -> bytes:
    return docs.tagged_pdf(
        "SOP-SRT-01 rev C",
        "SOP-SRT-01 Sortation conveyor SORT-1 drive service, revision C",
        [
            "Normal drive motor surface temperature is 40–70 °C; above 70 °C raise a maintenance work order.",
            "The thermal overload trips at 85 °C and stops the sorter with fault OVERTEMP_M2.",
            "Restarting a sorter after a thermal trip requires the sorter-reset qualification.",
            "Isolate the drive and verify zero energy before inspecting the motor.",
        ],
    )


def hsr_7() -> bytes:
    return docs.tagged_pdf(
        "HSR-7 excerpt",
        "HSR-7 Harbor safety regulation (excerpt)",
        [
            "3.4 After a thermal-overload trip, a powered sorter drive is inspected and reset only by "
            "a person qualified for electrical isolation.",
            "Qualification code: electrical-isolation.",
        ],
    )


# --- The whiteboard photo and its recorded extraction ----------------------------------------

WIDTH, HEIGHT = 640, 360
REGION = (90, 150, 420, 205)
MODEL = "claude-opus-5-5"
NOTE = "SORT-1: M2 running hot since Mon — watch temp"


def whiteboard_png() -> bytes:
    """A grey board with a dark frame and a marker scrawl inside the extracted region."""
    x0, y0, x1, y1 = REGION
    rows = []
    for y in range(HEIGHT):
        row = bytearray([225] * WIDTH)
        if y < 8 or y >= HEIGHT - 8:
            row[:] = bytes([70] * WIDTH)
        row[:8] = bytes([70] * 8)
        row[-8:] = bytes([70] * 8)
        if y0 <= y < y1 and (y - y0) % 14 < 4:
            for x in range(x0, x1):
                if (x * 5 + y * 3) % 13 < 9:
                    row[x] = 30
        rows.append(b"\x00" + bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


def whiteboard_extraction() -> str:
    record = {
        "image": "whiteboard_2026-11-04.png",
        "model": MODEL,
        "prompt": PROMPT,
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "regions": [{"id": 1, "box": list(REGION), "text": NOTE}],
    }
    return json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def files() -> dict[str, bytes]:
    return {
        "static/layout.yaml": LAYOUT.encode(),
        "static/neptune.mappings.yaml": MAPPINGS.encode(),
        "static/cmms/assets.json": _json(ASSETS),
        "static/cmms/work_orders.json": _json(WORK_ORDERS),
        "static/changes/change_log.xlsx": harbor_workbook.workbook(CHANGE_HEADER, CHANGE_ROWS),
        "static/roster/roster.csv": ROSTER.encode(),
        "static/docs/SOP-SRT-01_rev-C.pdf": sop_srt01(),
        "static/docs/HSR-7_excerpt.pdf": hsr_7(),
        "static/photos/whiteboard_2026-11-04.png": whiteboard_png(),
        "static/photos/whiteboard_2026-11-04.extraction.json": whiteboard_extraction().encode(),
    }

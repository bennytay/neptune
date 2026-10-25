"""A small hand-built slice of Plant 7 on 2026-10-20 (spec §9) for the query tests.

``sample()`` folds these facts and ``seen`` entries with the real views (#185), in memory.
"""

from __future__ import annotations

from neptune_memory.site.facts import Citation, Fact, Kind, LogEntry, Ref, Seen, Value
from neptune_memory.site.views import SiteModel, fold


def _day(t: str | None) -> str | None:
    return None if t is None else f"2026-10-20T{t}"


def fact(
    subject: str,
    predicate: str,
    obj: str | float,
    t: str | None,
    kind: Kind,
    source: str,
    file: str,
    locator: str,
    quote: str | None,
    *,
    until: str | None = None,
    unit: str | None = None,
    model: str | None = None,
) -> Fact:
    is_ref = isinstance(obj, str) and ":" in obj and predicate not in ("health", "rule_text")
    return Fact(
        subject=subject,
        predicate=predicate,
        object=Ref(str(obj)) if is_ref else Value(obj, unit),
        valid_from=_day(t),
        valid_to=_day(until),
        kind=kind,
        source=source,
        citations=(Citation(file, locator, quote),),
        model=model,
    )


PLC, ARM, FLEET, LAYOUT = (
    "feed/plc_cv2.jsonl",
    "feed/arm_c3.jsonl",
    "feed/fleet.jsonl",
    "static/layout.yaml",
)

FACTS: tuple[Fact, ...] = (
    fact("machine:cv-2", "feeds", "cell:c3", None, "stated", "layout", LAYOUT, "line 31", "cv-2"),
    fact("cell:c3", "feeds", "zone:aisle-a", None, "stated", "layout", LAYOUT, "line 32", "c3"),
    fact(
        "robot:arm-c3", "located_in", "cell:c3", None, "stated", "layout", LAYOUT, "line 20", "c3"
    ),
    fact(
        "machine:cv-2",
        "setpoint",
        0.95,
        "05:40:00",
        "observed",
        "plc.cv2",
        PLC,
        "line 1",
        '"speed_setpoint": 0.95',
        unit="m/s",
    ),
    fact(
        "machine:cv-2",
        "mode",
        "running",
        "06:00:00",
        "observed",
        "plc.cv2",
        PLC,
        "line 2",
        '"state": "running"',
    ),
    fact(
        "machine:cv-2",
        "health",
        "ok",
        "06:00:00",
        "observed",
        "plc.cv2",
        PLC,
        "line 2",
        '"alarm": null',
    ),
    fact(
        "machine:cv-2",
        "mode",
        "stopped",
        "10:05:02",
        "observed",
        "plc.cv2",
        PLC,
        "line 812",
        '"state": "stopped"',
    ),
    fact(
        "machine:cv-2",
        "health",
        "fault:JAM_PE3",
        "10:05:02",
        "observed",
        "plc.cv2",
        PLC,
        "line 812",
        '"alarm": "JAM_PE3"',
    ),
    fact(
        "machine:cv-2",
        "mode",
        "running",
        "10:41:00",
        "observed",
        "plc.cv2",
        PLC,
        "line 1240",
        '"state": "running"',
    ),
    fact(
        "robot:arm-c3",
        "mode",
        "running",
        "06:00:00",
        "observed",
        "arm.c3",
        ARM,
        "line 3",
        "MODE running",
    ),
    fact(
        "robot:arm-c3",
        "mode",
        "idle_starved",
        "10:05:30",
        "observed",
        "arm.c3",
        ARM,
        "line 88",
        "MODE idle_starved",
    ),
    fact(
        "robot:arm-c3",
        "mode",
        "protective_stop",
        "10:41:20",
        "observed",
        "arm.c3",
        ARM,
        "line 97",
        "PSTOP awaiting reset",
    ),
    fact(
        "robot:amr-01",
        "in_zone",
        "zone:aisle-a",
        "10:06:00",
        "observed",
        "fleet",
        FLEET,
        "line 5003",
        '"zone": "aisle-a"',
    ),
    fact(
        "robot:amr-01",
        "current_task",
        "task:T-8812",
        "10:06:00",
        "observed",
        "fleet",
        FLEET,
        "line 5003",
        '"orderId": "T-8812"',
    ),
    fact(
        "task:T-8812",
        "task_state",
        "waiting",
        "10:06:00",
        "observed",
        "fleet",
        FLEET,
        "line 5003",
        '"actionStatus": "WAITING"',
    ),
    fact(
        "robot:amr-03",
        "battery",
        22,
        "09:40:05",
        "observed",
        "fleet",
        FLEET,
        "line 4410",
        '"batteryCharge": 22',
        unit="%",
    ),
    fact(
        "robot:amr-02",
        "serial",
        "SN-88412",
        "06:00:00",
        "observed",
        "fleet",
        FLEET,
        "line 7",
        '"serialNumber": "SN-88412"',
    ),
    fact(
        "robot:MR-1142",
        "serial",
        "SN-88412",
        None,
        "stated",
        "cmms",
        "static/cmms/assets.csv",
        'row 3 col "Serial"',
        "SN-88412",
    ),
    Fact(
        subject="robot:amr-02",
        predicate="same_as",
        object=Ref("robot:MR-1142"),
        valid_from=None,
        valid_to=None,
        kind="inferred",
        source="fleet",
        citations=(
            Citation(FLEET, "line 7", '"serialNumber": "SN-88412"'),
            Citation("static/cmms/assets.csv", 'row 3 col "Serial"', "SN-88412"),
        ),
    ),
    fact(
        "person:okafor",
        "holds_qualification",
        "qualification:arm-reset-c3",
        None,
        "stated",
        "roster",
        "static/roster/qualifications.csv",
        'row 2 col "Qualification"',
        "arm-reset-c3",
    ),
    fact(
        "person:okafor",
        "on_break",
        "break",
        "10:30:00",
        "stated",
        "roster",
        "static/roster/shift_roster.csv",
        'row 2 col "Break"',
        "10:30-11:00",
        until="11:00:00",
    ),
    fact(
        "document:SOP-C3-02",
        "requires_qualification",
        "qualification:arm-reset-c3",
        None,
        "stated",
        "docs",
        "static/docs/SOP-C3-02.pdf",
        "page 1 block 4",
        "arm-reset-c3",
    ),
    fact(
        "machine:cv-2",
        "work_order",
        "work_order:WO-5530",
        "10:08:00",
        "stated",
        "cmms",
        "static/cmms/work_orders.csv",
        'row 3 col "WO"',
        "WO-5530",
    ),
    fact(
        "work_order:WO-5530",
        "wo_cause",
        "belt tracking off",
        "10:08:00",
        "stated",
        "cmms",
        "static/cmms/work_orders.csv",
        'row 3 col "Cause"',
        "belt tracking off",
    ),
    fact(
        "machine:cv-2",
        "note",
        "PE3 dirty — clean every shift",
        "06:00:00",
        "inferred",
        "photos",
        "static/photos/whiteboard_2026-10-20.png",
        "region 120,40,380,90",
        "CV-2: PE3 dirty — clean every shift",
        model="claude-demo@0123456789ab",
    ),
)

INTERVALS: tuple[Fact, ...] = tuple(
    fact(
        f"source:{name}",
        "report_interval",
        5,
        None,
        "stated",
        "layout",
        LAYOUT,
        "line 9",
        "5",
        unit="s",
    )
    for name in ("plc.cv2", "arm.c3", "fleet")
)

SEEN: tuple[Seen, ...] = (
    Seen("fleet/amr-03", "2026-10-20T09:40:05"),
    Seen("plc.cv2", "2026-10-20T10:35:00"),
    Seen("arm.c3", "2026-10-20T10:35:00"),
    Seen("fleet/amr-01", "2026-10-20T10:35:00"),
    Seen("fleet/amr-02", "2026-10-20T10:35:00"),
    Seen("plc.cv2", "2026-10-20T11:15:00"),
)


def sample() -> SiteModel:
    """The real views folded over ``INTERVALS``, ``FACTS`` and then ``SEEN``."""
    entries = [LogEntry(n, "fact", fact=f) for n, f in enumerate(INTERVALS + FACTS, start=1)]
    entries += [LogEntry(len(entries) + n, "seen", seen=s) for n, s in enumerate(SEEN, start=1)]
    return fold(entries)

# ruff: noqa: E501
"""The four live feeds of Harbor DC (a different site): every payload is its own dialect.

Two windows are written: the last ten minutes of the 2026-11-03 crew-B shift, and 2026-11-04
07:30-12:00, every 10 s (the inspector every 30 s). Every state is a pure function of the site
time, so the bytes never vary.

Envelope, one JSON line per message: ``{"t", "source", "topic", "payload"}``.
 - ``plc.sort1``: OPC UA style, payload ``{"sequence", "nodes": [{"name", "value", "eu"?}]}`` with
   ``Sorter1.RunState`` (RUN|FAULT), ``Sorter1.FaultCode`` ("" or a code), ``Sorter1.DivertRateSetpoint``
   (tote/h) and ``Sorter1.TotesSorted`` (cumulative counter).
 - ``vendor.humanoid``: the vendor state API, topic ``GET /api/v2/robots/<id>/state``; payload ``id``,
   ``serial``, ``firmware``, ``status`` (WORKING|IDLE), ``battery_pct``, ``zone``, ``task`` (or null),
   ``idle_reason`` and ``faults``.
 - ``rmf.fleet``: Open-RMF fleet states, one robot per message, topic ``fleet_states/goods_fleet/<robot>``;
   payload ``name``, ``robot`` (``name``, ``model``, ``task_id``, ``mode.mode``, ``battery_percent``,
   ``location.level_name``) and the fleet adapter's ``extra`` (``serial_number``, ``software_version``,
   ``task_state``).
 - ``ros.insp1``: ROS 2 ``/diagnostics`` DiagnosticArray from the legged inspector: ``header.stamp`` and
   ``status`` entries (``level`` 0 OK / 1 WARN / 2 ERROR, ``name``, ``message``, ``hardware_id``, key/value
   ``values``). Thermal scans of the sorter motor arrive only while the inspector patrols the sort hall.
"""

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

STEP = timedelta(seconds=10)
WINDOWS = [
    (datetime(2026, 11, 3, 14, 50), datetime(2026, 11, 3, 15, 0)),
    (datetime(2026, 11, 4, 7, 30), datetime(2026, 11, 4, 12, 0)),
]
DAY_START = {3: datetime(2026, 11, 3, 7), 4: datetime(2026, 11, 4, 7)}

FAULT = datetime(2026, 11, 4, 10, 12, 10)
RESTART = datetime(2026, 11, 4, 10, 42)
HUM_IDLE = {
    "hum-01": (datetime(2026, 11, 4, 10, 12, 40), datetime(2026, 11, 4, 10, 43)),
    "hum-02": (datetime(2026, 11, 4, 10, 13), datetime(2026, 11, 4, 10, 47)),
}
HUM2_FIRMWARE = datetime(2026, 11, 4, 7, 50)
GTP_WAIT = datetime(2026, 11, 4, 10, 13)
GTP_RESUME = datetime(2026, 11, 4, 10, 55)
GTP3_LAST = datetime(2026, 11, 4, 9, 50, 10)

# The sorter counts one tote per 0.5 s at 7200/h and per 0.43 s at 8400/h while it runs.
PRODUCING = [
    (datetime(2026, 11, 3, 7), datetime(2026, 11, 3, 15), 7200 / 3600),
    (datetime(2026, 11, 4, 7), FAULT, 8400 / 3600),
    (RESTART, datetime(2026, 11, 4, 15), 8400 / 3600),
]
TOTES_BASE = 52000

# (start, end, zone) of the legged inspector's patrol on 2026-11-04; docked afterwards.
PATROL = [
    (datetime(2026, 11, 4, 7, 30), datetime(2026, 11, 4, 8, 0), "dock-4"),
    (datetime(2026, 11, 4, 8, 0), datetime(2026, 11, 4, 8, 45), "sort-hall"),
    (datetime(2026, 11, 4, 8, 45), datetime(2026, 11, 4, 9, 30), "pick-aisle"),
    (datetime(2026, 11, 4, 9, 30), datetime(2026, 11, 4, 10, 50), "sort-hall"),
]
DOCKED_FROM = datetime(2026, 11, 4, 10, 50)
SCAN_START = datetime(2026, 11, 4, 8, 0)
ADVISORY_C, TRIP_C = 70.0, 85.0

HUMANOIDS = {"hum-01": ("TM-2201", 0), "hum-02": ("TM-2202", 1)}
GTPS = {"gtp-01": ("GTP-4469", 0), "gtp-02": ("GTP-4470", 1), "gtp-03": ("GTP-4471", 2)}


def totes_sorted(t: datetime) -> int:
    total = float(TOTES_BASE)
    for start, end, per_second in PRODUCING:
        if t > start:
            total += per_second * (min(t, end) - start).total_seconds()
    return int(total)


def motor_temp(t: datetime) -> float:
    """Surface temperature of sorter motor M2: a ramp to 78.4 C by 08:15, a slow climb to the 85 C trip
    at the fault, then a cool-down."""
    minutes = (t - SCAN_START).total_seconds() / 60
    trip = (FAULT - SCAN_START).total_seconds() / 60
    if minutes <= 15:
        value = 66.0 + 12.4 * minutes / 15
    elif minutes <= trip:
        value = 78.4 + (TRIP_C - 78.4) * (minutes - 15) / (trip - 15)
    else:
        value = max(50.0, TRIP_C - 0.9 * (minutes - trip))
    return round(value, 1)


def _grid() -> list[datetime]:
    out: list[datetime] = []
    for start, end in WINDOWS:
        t = start
        while t <= end:
            out.append(t)
            t += STEP
    return out


def _line(t: datetime, source: str, topic: str, payload: Mapping[str, object]) -> str:
    envelope = {"t": t.isoformat(), "source": source, "topic": topic, "payload": payload}
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def sorter_plc() -> list[str]:
    topic = "opc.tcp://sort1-plc:4840/Sorter1"
    lines = []
    for sequence, t in enumerate(_grid()):
        faulted = FAULT <= t < RESTART
        nodes = [
            {"name": "Sorter1.RunState", "value": "FAULT" if faulted else "RUN"},
            {"name": "Sorter1.FaultCode", "value": "OVERTEMP_M2" if faulted else ""},
            {
                "name": "Sorter1.DivertRateSetpoint",
                "value": 8400 if t >= datetime(2026, 11, 4, 6, 55) else 7200,
                "eu": "tote/h",
            },
            {"name": "Sorter1.TotesSorted", "value": totes_sorted(t), "eu": "count"},
        ]
        lines.append(_line(t, "plc.sort1", topic, {"sequence": sequence % 65536, "nodes": nodes}))
    return lines


def _work_seconds(name: str, t: datetime) -> int:
    """Seconds the humanoid has spent working since the shift began (idle time does not count)."""
    seconds = (t - DAY_START[t.day]).total_seconds()
    start, end = HUM_IDLE[name]
    if t.day == 4 and t > start:
        seconds -= (min(t, end) - start).total_seconds()
    return int(seconds)


def humanoids() -> list[str]:
    lines = []
    for t in _grid():
        for name, (serial, index) in HUMANOIDS.items():
            start, end = HUM_IDLE[name]
            idle = start <= t < end
            worked = _work_seconds(name, t)
            task = None
            if not idle:
                base = 40000 + 5000 * index + (0 if t.day == 3 else 1000)
                task = {
                    "id": f"TOTE-{base + worked // 120}",
                    "kind": "tote_move",
                    "state": "in_progress",
                }
            firmware = "3.5.0" if name == "hum-01" or t >= HUM2_FIRMWARE else "3.4.1"
            payload: dict[str, object] = {
                "id": name,
                "serial": serial,
                "firmware": firmware,
                "status": "IDLE" if idle else "WORKING",
                "battery_pct": int(98 - worked / 600),
                "zone": "sort-hall",
                "task": task,
                "idle_reason": "no totes at pickup" if idle else "",
                "faults": [],
            }
            lines.append(_line(t, "vendor.humanoid", f"GET /api/v2/robots/{name}/state", payload))
    return lines


def _gtp_order(name: str, index: int, t: datetime) -> tuple[str, str]:
    """(task id, task state): gtp-01 and gtp-02 each wait on their own pick from 10:13 to 10:55."""
    if name != "gtp-03" and GTP_WAIT <= t < GTP_RESUME:
        return f"PK-773{1 + index}", "waiting"
    if name != "gtp-03" and t >= GTP_RESUME:
        return (
            f"PK-{8500 + 100 * index + int((t - GTP_RESUME).total_seconds() // 300)}",
            "executing",
        )
    day_base = 7000 if t.day == 3 else 7200
    return (
        f"PK-{day_base + 100 * index + int((t - DAY_START[t.day]).total_seconds() // 300)}",
        "executing",
    )


def _gtp_battery(name: str, index: int, t: datetime) -> float:
    if name == "gtp-03":
        if t.day == 3:
            return 55.0
        start = datetime(2026, 11, 4, 7, 30)
        span = (GTP3_LAST - start).total_seconds()
        return round(78 - 59 * min((t - start).total_seconds(), span) / span, 1)
    minutes = int((t - datetime(2026, 11, 3)).total_seconds() // 300)
    return float(90 - (minutes + 5 * index) % 12 * 2)


def rmf_fleet() -> list[str]:
    lines = []
    for sequence, t in enumerate(_grid()):
        for name, (serial, index) in GTPS.items():
            if name == "gtp-03" and t > GTP3_LAST:
                continue  # gtp-03 goes silent: its last values turn stale
            task, state = _gtp_order(name, index, t)
            seconds = int((t - DAY_START[t.day]).total_seconds())
            payload: dict[str, object] = {
                "name": "goods_fleet",
                "robot": {
                    "name": name,
                    "model": "GTP-600",
                    "task_id": task,
                    "seq": sequence,
                    "mode": {"mode": "MODE_WAITING" if state == "waiting" else "MODE_MOVING"},
                    "battery_percent": _gtp_battery(name, index, t),
                    "location": {
                        "x": round(2 + seconds % 600 / 20, 1),
                        "y": round(5 + seconds // 7 % 40 / 4, 1),
                        "level_name": "dock-staging" if state == "waiting" else "pick-aisle",
                    },
                },
                "extra": {
                    "serial_number": serial,
                    "software_version": "6.2.0",
                    "task_state": state,
                },
            }
            lines.append(_line(t, "rmf.fleet", f"fleet_states/goods_fleet/{name}", payload))
    return lines


def _patrol(t: datetime) -> tuple[str, str]:
    """(zone, gait) of the inspector at ``t``."""
    if t.day == 4:
        for start, end, zone in PATROL:
            if start <= t < end:
                return zone, "WALK"
    return "charging", "DOCKED"


def _inspector_battery(t: datetime) -> int:
    if t.day == 3:
        return 100
    if t >= DOCKED_FROM:
        return min(100, 60 + int((t - DOCKED_FROM).total_seconds() // 60))
    return int(100 - (t - datetime(2026, 11, 4, 7, 30)).total_seconds() / 300)


def _status(
    level: int, name: str, message: str, hardware_id: str, **values: object
) -> dict[str, object]:
    return {
        "level": level,
        "name": name,
        "message": message,
        "hardware_id": hardware_id,
        "values": [{"key": key, "value": str(value)} for key, value in values.items()],
    }


def inspector_diag() -> list[str]:
    lines = []
    for t in _grid():
        if t.second % 30:
            continue
        zone, gait = _patrol(t)
        statuses = [
            _status(0, "insp1/locomotion", "ok", "insp-1", gait=gait, zone=zone),
            _status(0, "insp1/power", "ok", "insp-1", battery_pct=_inspector_battery(t)),
        ]
        scanning = zone == "sort-hall" and t.minute % 5 == 0 and t.second == 0
        if scanning:
            temp = motor_temp(t)
            level = 2 if temp >= TRIP_C else 1 if temp >= ADVISORY_C else 0
            message = (
                f"surface {temp} C above {ADVISORY_C:.0f} C advisory"
                if level
                else f"surface {temp} C"
            )
            statuses.append(
                _status(
                    level,
                    "insp1/thermal_scan sort-1/motor-m2",
                    message,
                    "sort-1",
                    max_temp_c=temp,
                    advisory_c=f"{ADVISORY_C:.0f}",
                )
            )
        payload = {"header": {"stamp": t.isoformat(), "frame_id": "map"}, "status": statuses}
        lines.append(_line(t, "ros.insp1", "/diagnostics", payload))
    return lines


FEEDS: dict[str, Callable[[], list[str]]] = {
    "feed/sorter_plc.jsonl": sorter_plc,
    "feed/humanoids.jsonl": humanoids,
    "feed/rmf_fleet.jsonl": rmf_fleet,
    "feed/inspector_diag.jsonl": inspector_diag,
}

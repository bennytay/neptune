"""The three live feeds of Plant 7: conveyor PLC, AMR fleet manager, arm controller.

Two windows are written: the last ten minutes of the 2026-10-19 shift A, and 2026-10-20
05:30-11:30, every 5 s. Every state below is a pure function of the site time, so the bytes
never vary.

Envelope, one JSON line per message: ``{"t", "source", "topic", "payload"}``.
 - ``plc.cv2``: payload ``{"seq", "metrics": [{"name", "value", "unit"?}]}`` with metrics ``state``
   (running|idle|stopped), ``alarm`` ("" or a code), ``speed_setpoint`` (m/s) and
   ``cell_c3/parts_out`` (cumulative counter).
 - ``fleet``: topic ``uagv/v2/p7/<robot>/state``; payload ``serialNumber``, ``version`` (firmware),
   ``operatingMode``, ``batteryState.batteryCharge``, ``errors``, ``orderId`` ("" = none),
   ``orderState`` (driving|waiting|idle) and ``zone``.
 - ``arm.c3``: topic ``c3/controller/log``; payload ``event`` (heartbeat|mode_change|
   protective_stop|reset), ``mode``, ``fault`` ("" or a code), ``user`` (reset only), ``text``.
"""

import json
import math
from collections.abc import Callable
from datetime import datetime, timedelta

STEP = timedelta(seconds=5)
SHIFT_START = {19: datetime(2026, 10, 19, 6), 20: datetime(2026, 10, 20, 6)}
WINDOWS = [
    (datetime(2026, 10, 19, 13, 50), datetime(2026, 10, 19, 14, 0)),
    (datetime(2026, 10, 20, 5, 30), datetime(2026, 10, 20, 11, 30)),
]

SETPOINT_CHANGE = datetime(2026, 10, 20, 5, 40)
FIRMWARE_CHANGE = datetime(2026, 10, 20, 5, 50)
JAM = datetime(2026, 10, 20, 10, 5, 2)
ARM_STARVED = datetime(2026, 10, 20, 10, 5, 30)
AMR_WAIT = datetime(2026, 10, 20, 10, 6)
CV2_RESTART = datetime(2026, 10, 20, 10, 41)
ARM_PSTOP = datetime(2026, 10, 20, 10, 41, 20)
ARM_RESET = datetime(2026, 10, 20, 11, 4)
AMR_RESUME = datetime(2026, 10, 20, 11, 10)
AMR3_LAST = datetime(2026, 10, 20, 9, 40, 5)
AMR3_CHARGING = datetime(2026, 10, 20, 9, 20)

# parts_out counts one part per 40 s while both the conveyor and the arm are producing.
PRODUCING = [
    (datetime(2026, 10, 19, 6), datetime(2026, 10, 19, 14)),
    (datetime(2026, 10, 20, 6), ARM_STARVED),
    (ARM_RESET, datetime(2026, 10, 20, 14)),
]
PART_SECONDS = 40
PARTS_BASE = 1000

SERIALS = {"amr-01": "SN-77101", "amr-02": "SN-88412", "amr-03": "SN-77103"}


def parts_out(t: datetime) -> int:
    total = PARTS_BASE
    for start, end in PRODUCING:
        top = min(t, end)
        if top <= start:
            continue
        origin = SHIFT_START[start.day]
        total += int((top - origin).total_seconds()) // PART_SECONDS
        total -= int((start - origin).total_seconds()) // PART_SECONDS
    return total


def cv2_state(t: datetime) -> str:
    if t.day == 19:
        return "running"
    if t < SHIFT_START[20]:
        return "idle"
    return "stopped" if JAM <= t < CV2_RESTART else "running"


def arm_mode(t: datetime) -> str:
    if t.day == 19:
        return "running"
    if t < SHIFT_START[20]:
        return "idle"
    if t < ARM_STARVED:
        return "running"
    if t < ARM_PSTOP:
        return "idle_starved"
    return "protective_stop" if t < ARM_RESET else "running"


def _grid() -> list[datetime]:
    out: list[datetime] = []
    for start, end in WINDOWS:
        t = start
        while t <= end:
            out.append(t)
            t += STEP
    return out


def _line(t: datetime, source: str, topic: str, payload: dict[str, object]) -> str:
    envelope = {"t": t.isoformat(), "source": source, "topic": topic, "payload": payload}
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def plc_cv2() -> list[str]:
    topic = "spBv1.0/P7/DDATA/cv2-edge/cv2"
    lines = []
    for seq, t in enumerate(sorted({*_grid(), JAM})):
        alarm = "JAM_PE3" if JAM <= t < CV2_RESTART else ""
        metrics = [
            {"name": "state", "value": cv2_state(t)},
            {"name": "alarm", "value": alarm},
            {
                "name": "speed_setpoint",
                "value": 0.95 if t >= SETPOINT_CHANGE else 0.80,
                "unit": "m/s",
            },
            {"name": "cell_c3/parts_out", "value": parts_out(t), "unit": "count"},
        ]
        lines.append(_line(t, "plc.cv2", topic, {"seq": seq % 256, "metrics": metrics}))
    return lines


def _amr_order(name: str, t: datetime) -> tuple[str, str]:
    """(orderId, orderState); AMR 01 and 02 both wait on T-8812 from 10:06 to 11:10."""
    if name == "amr-03":
        if t >= AMR3_CHARGING:
            return "", "idle"
        return f"T-{8612 - math.ceil((AMR_WAIT - t).total_seconds() / 600)}", "driving"
    shift = 0 if name == "amr-01" else 100
    if AMR_WAIT <= t < AMR_RESUME:
        return "T-8812", "waiting"
    if t < AMR_WAIT:
        return f"T-{8812 - math.ceil((AMR_WAIT - t).total_seconds() / 600) - shift}", "driving"
    return f"T-{8813 + int((t - AMR_RESUME).total_seconds() // 600) + shift}", "driving"


def _amr_battery(name: str, t: datetime) -> float:
    if name == "amr-03":
        start = datetime(2026, 10, 20, 5, 30)
        span = (AMR3_LAST - start).total_seconds()
        return round(80 - 58 * min((t - start).total_seconds(), span) / span, 1)
    minutes = int((t - datetime(2026, 10, 19)).total_seconds() // 300)
    return float(90 - (minutes + (0 if name == "amr-01" else 5)) % 12 * 2)


def fleet() -> list[str]:
    lines = []
    for header, t in enumerate(_grid()):
        for name in ("amr-01", "amr-02", "amr-03"):
            if name == "amr-03" and t > AMR3_LAST:
                continue  # amr-03 goes silent: its last values turn stale
            order, order_state = _amr_order(name, t)
            charging_bay = name == "amr-03" and t >= AMR3_CHARGING
            payload = {
                "headerId": header,
                "serialNumber": SERIALS[name],
                "version": "4.1.0" if name == "amr-03" and t < FIRMWARE_CHANGE else "4.2.0",
                "operatingMode": "AUTOMATIC",
                "batteryState": {"batteryCharge": _amr_battery(name, t), "charging": False},
                "errors": [],
                "orderId": order,
                "orderState": order_state,
                "zone": "zone:charging" if charging_bay else "zone:aisle-a",
            }
            lines.append(_line(t, "fleet", f"uagv/v2/p7/{name}/state", payload))
    return lines


# event, mode, fault, user, text
ARM_EVENTS: dict[datetime, tuple[str, str, str, str | None, str]] = {
    SHIFT_START[20]: ("mode_change", "running", "", None, "cycle start"),
    ARM_STARVED: (
        "mode_change",
        "idle_starved",
        "",
        None,
        "no part at pick position for 28 s; waiting for infeed",
    ),
    ARM_PSTOP: (
        "protective_stop",
        "protective_stop",
        "PSTOP_C3",
        None,
        "protective stop: part presence lost at pick as the conveyor restarted; awaiting reset",
    ),
    ARM_RESET: (
        "reset",
        "running",
        "",
        "okafor",
        "reset acknowledged by user okafor; cycle resumed",
    ),
}


def arm_c3() -> list[str]:
    lines = []
    for t in _grid():
        if t in ARM_EVENTS:
            event, mode, fault, user, text = ARM_EVENTS[t]
        else:
            event, mode, user, text = "heartbeat", arm_mode(t), None, "status"
            fault = "PSTOP_C3" if mode == "protective_stop" else ""
        payload: dict[str, object] = {
            "event": event,
            "mode": mode,
            "fault": fault,
            "user": user,
            "text": text,
        }
        lines.append(_line(t, "arm.c3", "c3/controller/log", payload))
    return lines


FEEDS: dict[str, Callable[[], list[str]]] = {
    "feed/plc_cv2.jsonl": plc_cv2,
    "feed/fleet.jsonl": fleet,
    "feed/arm_c3.jsonl": arm_c3,
}

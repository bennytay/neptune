"""Feed messages become observed state intervals, emitted only on change (spec §4, §9).

A message is anything with the ``FeedMessage`` fields (#182): ``t``, ``source``, ``topic``,
``payload``, plus ``file`` (corpus-relative) and ``line`` (1-based) for the citation
``line N``. ``valid_from`` is the message time. Pass one ``last`` dict through a whole build or
replay: a fact is emitted only when its value differs from the last one for
(subject, predicate, source); for a ``many`` predicate (``assigned_to``), only when that object is
new. Without ``last`` every value is emitted.

Payloads, per source:

- ``plc.cv2`` (Sparkplug-like DDATA): ``{"metrics": [{"name": ..., "value": ..., "unit": ...}]}``;
  ``state`` -> ``mode``, ``alarm`` -> ``health`` (``""`` = ``ok``, else ``fault:<alarm>``),
  ``speed_setpoint`` -> ``setpoint``, all on ``machine:cv-2``; ``cell_c3/parts_out`` (any
  ``.../parts_out``) -> ``parts_out`` on ``cell:c3``.
- ``fleet`` (VDA 5050-like ``state``), topic ``.../<robot>/state``: ``operatingMode`` -> ``mode``,
  ``batteryState.batteryCharge`` -> ``battery`` (%), ``errors`` -> ``health``, ``orderId`` ->
  ``current_task`` (``"none"`` when empty) and ``assigned_to``, ``orderState`` -> the task's
  ``task_state``, ``zone`` (``zone:aisle-a``, or a bare id) -> ``in_zone``, ``serialNumber`` ->
  ``serial``, ``version`` -> ``firmware``.
- ``arm.c3`` (controller log): ``{"event": "heartbeat"|"mode_change"|"protective_stop"|"reset",
  "mode": ..., "fault": "" | code, "user": ..., "text": ...}`` -> ``mode``, and ``health``
  (``ok`` or ``fault:<code>``) on ``robot:arm-c3``; an event's quote is its ``text``.
"""

from __future__ import annotations

from typing import Any, Protocol

from neptune_memory.site.facts import Citation, Fact, Ref, Value
from neptune_memory.site.vocab import is_one


class FeedMessage(Protocol):
    """The fields of #182's ``FeedMessage`` that mapping reads."""

    @property
    def t(self) -> str: ...
    @property
    def source(self) -> str: ...
    @property
    def topic(self) -> str: ...
    @property
    def payload(self) -> dict[str, Any]: ...
    @property
    def file(self) -> str: ...
    @property
    def line(self) -> int: ...


Last = dict[tuple[str, ...], Ref | Value]
_Reading = tuple[str, str, Ref | Value, Any]  # subject, predicate, object, the value as written

# Which site entity each feed device reports for: the corpus's edge configuration.
PLC_SUBJECTS = {"plc.cv2": "machine:cv-2"}
PLC_CELL = {"plc.cv2": "cell:c3"}  # the cell counter wired to that PLC
ARM_SUBJECTS = {"arm.c3": "robot:arm-c3"}


def map_message(msg: FeedMessage, last: Last | None = None) -> list[Fact]:
    """The observed facts in one feed message whose value changed since ``last``."""
    if msg.source in PLC_SUBJECTS:
        readings = _plc(msg)
    elif msg.source in ARM_SUBJECTS:
        readings = _arm(msg)
    elif msg.source == "fleet":
        readings = _fleet(msg)
    else:
        return []
    last = {} if last is None else last
    cite_line = f"line {msg.line}"
    out: list[Fact] = []
    for subject, predicate, obj, quote in readings:
        key: tuple[str, ...] = (subject, predicate, msg.source)
        if not is_one(predicate):
            key += (obj.ref if isinstance(obj, Ref) else str(obj.value),)
        if key in last and last[key] == obj:
            continue
        last[key] = obj
        cite = Citation(msg.file, cite_line, str(quote))
        out.append(Fact(subject, predicate, obj, msg.t, None, "observed", msg.source, (cite,)))
    return out


def _health(code: str) -> Value:
    return Value(f"fault:{code}" if code else "ok")


def _plc(msg: FeedMessage) -> list[_Reading]:
    machine, cell = PLC_SUBJECTS[msg.source], PLC_CELL[msg.source]
    out: list[_Reading] = []
    for metric in msg.payload.get("metrics", []):
        name, value = metric["name"], metric["value"]
        if name == "state":
            out.append((machine, "mode", Value(value), value))
        elif name == "alarm":
            out.append((machine, "health", _health(value), value))
        elif name == "speed_setpoint":
            out.append((machine, "setpoint", Value(value, metric.get("unit")), value))
        elif name.rsplit("/", 1)[-1] == "parts_out":
            out.append((cell, "parts_out", Value(value, metric.get("unit")), value))
    return out


def _fleet(msg: FeedMessage) -> list[_Reading]:
    robot = "robot:" + msg.topic.rstrip("/").split("/")[-2]
    p = msg.payload
    out: list[_Reading] = []
    if "serialNumber" in p:
        out.append((robot, "serial", Value(p["serialNumber"]), p["serialNumber"]))
    if "version" in p:
        out.append((robot, "firmware", Value(p["version"]), p["version"]))
    if "operatingMode" in p:
        out.append((robot, "mode", Value(p["operatingMode"]), p["operatingMode"]))
    charge = p.get("batteryState", {}).get("batteryCharge")
    if charge is not None:
        out.append((robot, "battery", Value(charge, "%"), charge))
    if "errors" in p:
        code = p["errors"][0].get("errorType", "error") if p["errors"] else ""
        out.append((robot, "health", _health(code), code or "[]"))
    if "zone" in p:
        zone = p["zone"] if p["zone"].startswith("zone:") else f"zone:{p['zone']}"
        out.append((robot, "in_zone", Ref(zone), p["zone"]))
    if "orderId" in p:
        order = p["orderId"]
        if order:
            task = f"task:{order}"
            out.append((robot, "current_task", Ref(task), order))
            out.append((task, "assigned_to", Ref(robot), order))
            if p.get("orderState"):
                out.append((task, "task_state", Value(p["orderState"]), p["orderState"]))
        else:
            out.append((robot, "current_task", Value("none"), '""'))
    return out


def _arm(msg: FeedMessage) -> list[_Reading]:
    robot = ARM_SUBJECTS[msg.source]
    p = msg.payload
    event = p.get("event")
    out: list[_Reading] = []
    if p.get("mode"):
        quote = p["mode"] if event == "heartbeat" else p.get("text", p["mode"])
        out.append((robot, "mode", Value(p["mode"]), quote))
    if "fault" in p:
        fault = p["fault"] or ""
        quote = (fault or '""') if event == "heartbeat" else p.get("text", fault)
        out.append((robot, "health", _health(fault), quote))
    return out

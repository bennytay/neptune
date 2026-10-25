"""Feed messages become observed state intervals, emitted only on change (spec §4, §9)."""

from dataclasses import dataclass
from typing import Any

from neptune_memory.site.facts import Citation, Fact, Ref, Value
from neptune_memory.site.mappers import Last, map_message


@dataclass(frozen=True)
class Msg:
    t: str
    source: str
    topic: str
    payload: dict[str, Any]
    file: str
    line: int


PLC_TOPIC = "spBv1.0/P7/DDATA/cv2-edge/cv2"


def _plc(line: int, t: str, state: str, alarm: str, parts: int, setpoint: float = 0.95) -> Msg:
    metrics = [
        {"name": "state", "value": state},
        {"name": "alarm", "value": alarm},
        {"name": "speed_setpoint", "value": setpoint, "unit": "m/s"},
        {"name": "parts_out", "value": parts},
    ]
    return Msg(t, "plc.cv2", PLC_TOPIC, {"metrics": metrics}, "feed/plc_cv2.jsonl", line)


def _fleet(line: int, t: str, robot: str, **payload: Any) -> Msg:
    return Msg(t, "fleet", f"uagv/v2/P7/{robot}/state", payload, "feed/fleet.jsonl", line)


def _arm(line: int, t: str, **payload: Any) -> Msg:
    return Msg(t, "arm.c3", "controller/arm-c3/log", payload, "feed/arm_c3.jsonl", line)


def _triples(facts: list[Fact]) -> set[tuple[str, str, Any]]:
    return {(f.subject, f.predicate, f.object) for f in facts}


def test_plc_emits_only_when_a_value_changes() -> None:
    last: Last = {}
    first = map_message(_plc(1, "2026-10-20T10:04:57", "running", "", 412), last)
    assert _triples(first) == {
        ("machine:cv-2", "mode", Value("running")),
        ("machine:cv-2", "health", Value("ok")),
        ("machine:cv-2", "setpoint", Value(0.95, "m/s")),
        ("cell:c3", "parts_out", Value(412)),
    }
    assert map_message(_plc(2, "2026-10-20T10:05:00", "running", "", 412), last) == []
    jam = map_message(_plc(3, "2026-10-20T10:05:02", "stopped", "JAM_PE3", 412), last)
    assert _triples(jam) == {
        ("machine:cv-2", "mode", Value("stopped")),
        ("machine:cv-2", "health", Value("fault:JAM_PE3")),
    }
    mode = next(f for f in jam if f.predicate == "mode")
    assert (mode.kind, mode.source, mode.valid_from, mode.valid_to) == (
        "observed",
        "plc.cv2",
        "2026-10-20T10:05:02",
        None,
    )
    assert mode.citations == (Citation("feed/plc_cv2.jsonl", "line 3", "stopped"),)
    # Without a carried state every value is emitted.
    assert len(map_message(_plc(2, "2026-10-20T10:05:00", "running", "", 412))) == 4


def test_fleet_state_maps_robot_task_zone_and_identity_fields() -> None:
    last: Last = {}
    state = {
        "serialNumber": "SN-88412",
        "version": "4.1.0",
        "operatingMode": "AUTOMATIC",
        "batteryState": {"batteryCharge": 64.0},
        "errors": [],
        "orderId": "T-8812",
        "orderState": "waiting",
        "zone": "aisle-a",
    }
    facts = map_message(_fleet(7, "2026-10-20T10:06:00", "amr-02", **state), last)
    assert _triples(facts) == {
        ("robot:amr-02", "serial", Value("SN-88412")),
        ("robot:amr-02", "firmware", Value("4.1.0")),
        ("robot:amr-02", "mode", Value("AUTOMATIC")),
        ("robot:amr-02", "battery", Value(64.0, "%")),
        ("robot:amr-02", "health", Value("ok")),
        ("robot:amr-02", "current_task", Ref("task:T-8812")),
        ("robot:amr-02", "in_zone", Ref("zone:aisle-a")),
        ("task:T-8812", "assigned_to", Ref("robot:amr-02")),
        ("task:T-8812", "task_state", Value("waiting")),
    }
    assert {f.kind for f in facts} == {"observed"}
    # A second robot on the same order: a new assignment, but the task's state has not changed.
    other = map_message(_fleet(8, "2026-10-20T10:06:00", "amr-01", **state), last)
    assert ("task:T-8812", "assigned_to", Ref("robot:amr-01")) in _triples(other)
    assert not [f for f in other if f.predicate == "task_state"]
    drained = {**state, "batteryState": {"batteryCharge": 63.5}}
    again = map_message(_fleet(9, "2026-10-20T10:06:05", "amr-02", **drained), last)
    assert _triples(again) == {("robot:amr-02", "battery", Value(63.5, "%"))}


def test_arm_controller_log_maps_mode_and_health() -> None:
    last: Last = {}
    starved = _arm(4, "2026-10-20T10:05:30", event="mode", mode="idle_starved", msg="no part")
    stop = _arm(5, "2026-10-20T10:41:20", event="protective_stop", msg="protective stop")
    reset = _arm(6, "2026-10-20T11:04:00", event="reset", mode="running", user="okafor")
    assert _triples(map_message(starved, last)) == {("robot:arm-c3", "mode", Value("idle_starved"))}
    assert _triples(map_message(stop, last)) == {
        ("robot:arm-c3", "mode", Value("protective_stop")),
        ("robot:arm-c3", "health", Value("fault:protective_stop")),
    }
    assert _triples(map_message(reset, last)) == {
        ("robot:arm-c3", "mode", Value("running")),
        ("robot:arm-c3", "health", Value("ok")),
    }

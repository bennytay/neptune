"""The plant, one site second at a time: the conveyor, the arm, the AMR fleet and the people.

``Engine.step()`` moves the plant one second and returns the feed envelopes the plant's systems
publish at that second, in the formats the corpus uses: the CV-2 edge node (Sparkplug
style, every 5 s and on change), the fleet manager (VDA 5050 style state per robot every 5 s) and
the arm controller's log (every 5 s and on each event). ``Engine.command()`` takes what Neptune
sends back: VDA 5050 ``instantActions`` and ``order`` to a robot, a Sparkplug ``DCMD`` to the PLC.

Randomness comes from one seeded ``random.Random`` drawn in a fixed order, so a seed and the same
commands at the same site seconds give the same plant, byte for byte.
"""

from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

from neptune_memory.site.sim import plant
from neptune_memory.site.sim.plant import Plan, Truth

SECOND = timedelta(seconds=1)
REPORT_EVERY = 5
STARVE_AFTER = 28  # s without a part (or a tote) before the arm reports it
LOAD_WAIT_AFTER = 60  # s at the pickup without a part before the robot reports waiting
UNLOAD_S = 20
SHIFT = (time(6), time(14))

PLC_TOPIC = "spBv1.0/P7/DDATA/cv2-edge/cv2"
ARM_TOPIC = "c3/controller/log"
FLEET_TOPIC = "uagv/v2/p7/{robot}/state"
SERIALS = {"amr-01": "SN-77101", "amr-02": "SN-88412", "amr-03": "SN-77103"}

Envelope = dict[str, Any]


def on_shift(t: datetime) -> bool:
    return SHIFT[0] <= t.time() < SHIFT[1]


# --- People -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Shift:
    start: datetime
    end: datetime
    breaks: tuple[tuple[datetime, datetime], ...] = ()


@dataclass(frozen=True)
class PersonSpec:
    """One person as the roster states them (``static/roster/``)."""

    name: str
    role: str
    qualifications: frozenset[str]
    shifts: tuple[Shift, ...]


@dataclass
class Job:
    kind: str  # clear_jam | reset_arm | clean_pe3 | rescue_amr
    need: str | None  # qualification
    at: tuple[float, float]
    seconds: float
    created: datetime
    target: str = ""
    person: str | None = None
    started: datetime | None = None
    left: float = 0.0


@dataclass
class Person:
    spec: PersonSpec
    x: float
    y: float
    yaw: float = 0.0
    activity: str = "off_shift"
    present: bool = False
    job: Job | None = None

    @property
    def id(self) -> str:
        return f"person:{self.spec.name.lower()}"

    def shift(self, t: datetime) -> Shift | None:
        return next((s for s in self.spec.shifts if s.start <= t < s.end), None)

    def on_break(self, t: datetime) -> bool:
        current = self.shift(t)
        return current is not None and any(a <= t < b for a, b in current.breaks)

    def walk(self, to: tuple[float, float], speed: float = plant.WALK_SPEED) -> bool:
        """One second towards ``to``; True once there."""
        dx, dy = to[0] - self.x, to[1] - self.y
        dist = math.hypot(dx, dy)
        if dist <= speed:
            self.x, self.y = to
            return True
        self.yaw = math.atan2(dy, dx)
        self.x += dx / dist * speed
        self.y += dy / dist * speed
        return False


# --- Machines ---------------------------------------------------------------------------------


@dataclass
class Part:
    id: int
    s: float
    jammed: bool = False


@dataclass
class Conveyor:
    setpoint: float = 0.80
    state: str = "idle"
    alarm: str = ""
    dirt: float = 0.0
    travel: float = 0.0
    cleaning: bool = False
    parts: list[Part] = field(default_factory=list)  # front (largest s) first
    spawn_in: float = 0.0
    next_id: int = 1

    @property
    def jammed(self) -> bool:
        return self.alarm != ""

    def at_pick(self) -> bool:
        front = self.parts[0] if self.parts else None
        return front is not None and not front.jammed and front.s >= plant.BELT_LENGTH - 1e-6

    def step(self, line_on: bool, rng: random.Random, truth: Truth) -> bool:
        """Run one second; True if a part jammed at PE3."""
        running = line_on and not self.jammed and not self.cleaning
        self.state = "running" if running else ("stopped" if self.jammed else "idle")
        if not running:
            return False
        v = self.setpoint
        self.travel += v
        self.dirt = min(truth.dirt_max, self.dirt + truth.dirt_per_hour / 3600)
        limit = plant.BELT_LENGTH
        for part in self.parts:
            new = min(part.s + v, limit)
            if part.s < plant.PE3_AT <= new:
                chance = truth.jam_base * math.exp(truth.jam_speed_k * (v - 0.80))
                if rng.random() < chance * (1 + self.dirt):
                    part.s, part.jammed = plant.PE3_AT, True
                    self.alarm, self.state = "JAM_PE3", "stopped"
                    return True
            part.s = max(part.s, new)
            limit = part.s - plant.PART_GAP
        self.spawn_in -= 1
        if self.spawn_in <= 0 and (not self.parts or self.parts[-1].s >= plant.PART_GAP):
            self.parts.append(Part(self.next_id, 0.0))
            self.next_id += 1
            self.spawn_in = plant.PART_SPACING / v
        return False

    def clear(self) -> None:
        self.parts = [p for p in self.parts if not p.jammed]
        self.alarm = ""


ARM_PHASES = (
    ("reach", 6, "above_pick", "pick"),
    ("grip", 2, "pick", "pick"),
    ("lift", 3, "pick", "above_pick"),
    ("move", 8, "above_pick", "above_place"),
    ("release", 2, "above_place", "place"),
    ("return", 6, "place", "above_pick"),
)


@dataclass
class Arm:
    mode: str = "idle"
    fault: str = ""
    phase: str = "wait"
    phase_t: float = 0.0
    holding: bool = False
    waited: float = 0.0


# --- Fleet ------------------------------------------------------------------------------------


@dataclass
class Amr:
    id: str
    node: str
    battery: float
    firmware: str = "4.2.0"
    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0
    path: list[str] = field(default_factory=list)
    mode: str = "AUTOMATIC"
    errors: list[dict[str, str]] = field(default_factory=list)
    order_id: str = ""
    order_state: str = "idle"
    task: str | None = None  # pickup | deliver | charge | park | rescue
    forced: bool = False  # charging because the battery fell below the threshold
    held: bool = False  # parked by a person's order: the fleet manager leaves it alone
    paused: bool = False
    tote: int = 0
    work: float = 0.0
    blocked: float = 0.0
    zone: str = "zone:charging"
    charging: bool = False

    @property
    def stranded(self) -> bool:
        return bool(self.errors)

    def reads(self, truth: Truth) -> float:
        return truth.reads(self.id, self.battery)

    @property
    def goal(self) -> str:
        return self.path[-1] if self.path else self.node


def route(start: str, goal: str) -> list[str]:
    """Shortest lane path from ``start`` to ``goal``, ``start`` excluded."""
    best = {start: 0.0}
    queue: list[tuple[float, str, list[str]]] = [(0.0, start, [])]
    while queue:
        cost, node, path = heapq.heappop(queue)
        if node == goal:
            return path
        for nxt in plant.EDGES[node]:
            step = math.dist(plant.NODES[node], plant.NODES[nxt])
            if cost + step < best.get(nxt, math.inf):
                best[nxt] = cost + step
                heapq.heappush(queue, (cost + step, nxt, [*path, nxt]))
    return []


# --- The engine -------------------------------------------------------------------------------


@dataclass
class Kpis:
    parts_out: int = 0
    delivered: int = 0
    jams: int = 0
    jam_clear_s: list[float] = field(default_factory=list)
    conveyor_stopped_s: int = 0
    arm_starved_s: int = 0
    arm_blocked_s: int = 0
    arm_pstop_s: int = 0
    pstops: int = 0
    amr_stranded: int = 0

    def summary(self) -> dict[str, Any]:
        clears = self.jam_clear_s
        return {
            "parts_out": self.parts_out,
            "delivered": self.delivered,
            "jams": self.jams,
            "mean_jam_clear_min": round(sum(clears) / len(clears) / 60, 1) if clears else None,
            "conveyor_stopped_min": round(self.conveyor_stopped_s / 60, 1),
            "arm_starved_min": round(self.arm_starved_s / 60, 1),
            "arm_blocked_min": round(self.arm_blocked_s / 60, 1),
            "arm_pstop_min": round(self.arm_pstop_s / 60, 1),
            "protective_stops": self.pstops,
            "amr_stranded": self.amr_stranded,
        }


@dataclass(frozen=True)
class Maintenance:
    """A preventive maintenance row (``static/cmms/pm_schedule.csv``)."""

    id: str
    equipment: str
    task: str
    every_hours: float


class Engine:
    def __init__(
        self,
        start: datetime,
        people: list[PersonSpec],
        *,
        seed: int = 7,
        truth: Truth | None = None,
        plan: Plan | None = None,
        first_work_order: int = 5531,
    ) -> None:
        self.t = start
        self.rng = random.Random(seed)
        self.truth = truth or Truth()
        self.plan = plan or Plan()
        self.conveyor = Conveyor()
        self.arm = Arm()
        self.amrs = [
            Amr("amr-01", "CHARGER_1", 90.0),
            Amr("amr-02", "CHARGER_2", 85.0),
            Amr("amr-03", "PARK_A", 80.0),
        ]
        self.occupied: dict[str, str] = {}
        for amr in self.amrs:
            amr.x, amr.y = plant.NODES[amr.node]
            amr.zone = plant.zone_of(amr.x, amr.y) or amr.zone
            self.occupied[amr.node] = amr.id
        self.people: dict[str, Person] = {}
        self.set_roster(people)
        self.maintenance: list[Maintenance] = []
        self.pm_last: dict[str, datetime] = {}
        self.jobs: list[Job] = []
        self.claim: str | None = None  # the robot the pickup station is held for
        self.next_claim: str | None = None  # the robot staged to take over (Plan.prestage)
        self.loaded_at: dict[str, float] = {}
        self.parts_out = 1000
        self.orders = 9000
        self.work_order = first_work_order
        self.work_orders: list[dict[str, str]] = []  # rows not yet written to the CMMS export
        self.kpis: dict[str, Kpis] = {}
        self.events: list[str] = []  # since the last frame
        self.news: list[str] = []  # since the runner last read them
        self._plc_seq = 0
        self._header = 0
        self._plc_last: tuple[str, str, float] | None = None
        self._arm_said = False
        self._arm_out: list[Envelope] = []  # controller lines raised by a person's job
        self._scripted: list[tuple[datetime, str, Any]] = []

    # --- Inputs -------------------------------------------------------------------------------

    def set_roster(self, specs: list[PersonSpec]) -> None:
        """The roster or qualifications changed: everyone stays where they are, on the new terms."""
        for spec in specs:
            person = self.people.get(spec.name)
            if person is None:
                x, y = plant.PLACES["entrance"]
                self.people[spec.name] = Person(spec, x, y)
            else:
                person.spec = spec

    def set_maintenance(self, rows: list[Maintenance]) -> None:
        self.maintenance = rows

    def at(self, t: datetime, what: str, value: Any) -> None:
        """Script a change the site made at ``t`` (``setpoint`` or ``firmware:<robot>``)."""
        self._scripted.append((t, what, value))

    def jump(self, t: datetime) -> None:
        """Skip to ``t`` (overnight): the night crew charged every robot still standing; nobody is
        in the hall."""
        self.t = t
        for amr in self.amrs:
            if not amr.stranded:
                amr.battery = 100.0
        for person in self.people.values():
            if person.job is None:
                person.present, person.activity = False, "off_shift"
                person.x, person.y = plant.PLACES["entrance"]

    def command(self, topic: str, payload: dict[str, Any]) -> str:
        """Apply what Neptune (or anyone) sent the plant; returns what it did."""
        parts = topic.split("/")
        if "DCMD" in parts:
            for metric in payload.get("metrics", []):
                if metric.get("name") == "speed_setpoint":
                    self.conveyor.setpoint = float(metric["value"])
                    return f"CV-2 setpoint {self.conveyor.setpoint:.2f} m/s"
            return "DCMD ignored"
        amr = next((a for a in self.amrs if a.id == parts[-2]), None) if len(parts) > 1 else None
        if amr is None:
            return f"no robot for {topic}"
        if parts[-1] == "order":
            zone = str(payload["nodes"][-1]["nodeId"])
            goal = self._free_charger(amr) if zone == "zone:charging" else plant.PARKING.get(zone)
            if goal is None:
                return f"{amr.id}: no place in {zone}"
            self._release_claim(amr)
            amr.held, amr.task, amr.forced = True, "park", False
            amr.order_id, amr.order_state = str(payload.get("orderId", "")), "driving"
            self._go(amr, goal)
            return f"{amr.id} sent to {zone}"
        done = []
        for item in payload.get("actions", []):
            kind = item.get("actionType")
            if kind == "startPause":
                amr.paused, amr.mode = True, "PAUSED"
            elif kind == "stopPause":
                amr.paused, amr.mode = False, "AUTOMATIC"
            elif kind == "cancelOrder":
                self._release_claim(amr)
                amr.held, amr.task, amr.order_id, amr.order_state = False, None, "", "idle"
                amr.path = (
                    amr.path[:1] if amr.path and self.occupied.get(amr.path[0]) == amr.id else []
                )
            done.append(str(kind))
        return f"{amr.id}: {', '.join(done)}"

    # --- The step -----------------------------------------------------------------------------

    def step(self) -> list[Envelope]:
        self.t += SECOND
        out: list[Envelope] = []
        line_on = on_shift(self.t)
        kpi = self.kpis.setdefault(self.t.date().isoformat(), Kpis())
        for when, what, value in [s for s in self._scripted if s[0] <= self.t]:
            self._scripted.remove((when, what, value))
            if what == "setpoint":
                self.conveyor.setpoint = float(value)
            elif what.startswith("firmware:"):
                next(a for a in self.amrs if a.id == what.split(":")[1]).firmware = str(value)
        if self.conveyor.step(line_on, self.rng, self.truth):
            kpi.jams += 1
            self._note("CV-2 jam at PE3")
            lo, hi = self.truth.clear_jam_s
            self._job("clear_jam", "conveyor-lockout", self.rng.uniform(lo, hi), "machine:cv-2")
        out += self._arm(line_on, kpi)
        self._fleet(line_on, kpi)
        self._maintenance(line_on)
        self._people(line_on, kpi)
        out += self._arm_out
        self._arm_out = []
        if self.conveyor.state == "stopped":
            kpi.conveyor_stopped_s += 1
        out += self._plc()
        if self.t.second % REPORT_EVERY == 0:
            out += self._fleet_reports()
            if not self._arm_said:
                out.append(self._arm_line("heartbeat", "status"))
        self._arm_said = False
        return out

    # --- Arm ----------------------------------------------------------------------------------

    def _arm(self, line_on: bool, kpi: Kpis) -> list[Envelope]:
        arm = self.arm
        if arm.mode == "protective_stop":
            kpi.arm_pstop_s += 1
            return []
        if not line_on:
            if arm.mode != "idle" and arm.phase == "wait":
                arm.mode = "idle"
                return [self._arm_line("mode_change", "end of shift; cycle stopped")]
            if arm.phase == "wait":
                return []
        elif arm.mode == "idle":
            arm.mode, arm.waited = "running", 0.0
            return [self._arm_line("mode_change", "cycle start")]
        out: list[Envelope] = []
        if arm.phase == "wait":
            if self.conveyor.at_pick():
                arm.phase, arm.phase_t, arm.waited = "reach", 0.0, 0.0
                if arm.mode == "idle_starved":
                    arm.mode = "running"
                    out.append(self._arm_line("mode_change", "part at pick; cycle resumed"))
            else:
                arm.waited += 1
                if arm.mode == "idle_starved":
                    kpi.arm_starved_s += 1
                elif arm.mode == "running" and arm.waited >= STARVE_AFTER:
                    arm.mode = "idle_starved"
                    text = f"no part at pick position for {STARVE_AFTER} s; waiting for infeed"
                    out.append(self._arm_line("mode_change", text))
                    self._note("arm C3 starved")
            return out
        name, seconds = arm.phase, next(p[1] for p in ARM_PHASES if p[0] == arm.phase)
        if name == "move" and arm.phase_t == 0 and not self._tote_ready():
            arm.waited += 1
            if arm.mode == "blocked":
                kpi.arm_blocked_s += 1
            elif arm.waited >= STARVE_AFTER:
                arm.mode = "blocked"
                out.append(
                    self._arm_line("mode_change", "no tote at the pickup station; holding part")
                )
                self._note("arm C3 blocked: no tote")
            return out
        if arm.mode == "blocked":
            arm.mode = "running"
            out.append(self._arm_line("mode_change", "tote at the pickup station; cycle resumed"))
        arm.waited = 0.0
        if name == "grip" and arm.phase_t == 0:
            self.conveyor.parts.pop(0)
            arm.holding = True
        arm.phase_t += 1
        if arm.phase_t < seconds:
            return out
        if name == "release":
            arm.holding = False
            docked = self._docked()
            if docked is not None:
                docked.tote += 1
                self.loaded_at[docked.id] = 0.0
            self.parts_out += 1
            kpi.parts_out += 1
        order = [p[0] for p in ARM_PHASES]
        nxt = order.index(name) + 1
        arm.phase, arm.phase_t = (order[nxt], 0.0) if nxt < len(order) else ("wait", 0.0)
        return out

    def _arm_line(self, event: str, text: str, user: str | None = None) -> Envelope:
        self._arm_said = True
        payload = {"event": event, "mode": self.arm.mode, "fault": self.arm.fault}
        return self._envelope("arm.c3", ARM_TOPIC, payload | {"user": user, "text": text})

    def arm_pose(self) -> tuple[list[float], list[float]]:
        """The arm's joint angles and tool position now (for the timeline)."""
        arm = self.arm
        spec = next((p for p in ARM_PHASES if p[0] == arm.phase), None)
        if spec is None:
            a = b = "above_pick"
            k = 0.0
        else:
            _, seconds, a, b = spec
            k = min(1.0, arm.phase_t / seconds)
            k = k * k * (3 - 2 * k)
        joints = [x + (y - x) * k for x, y in zip(plant.POSES[a], plant.POSES[b], strict=True)]

        def tcp(pose: str) -> tuple[float, float, float]:
            x, y, z = plant.PICK if "pick" in pose else plant.PLACE
            return (x, y, z + (0.45 if pose.startswith("above") else 0.0))

        ta, tb = tcp(a), tcp(b)
        return [round(j, 4) for j in joints], [
            round(p + (q - p) * k, 3) for p, q in zip(ta, tb, strict=True)
        ]

    # --- PLC ----------------------------------------------------------------------------------

    def _plc(self) -> list[Envelope]:
        c = self.conveyor
        now = (c.state, c.alarm, round(c.setpoint, 2))
        if self.t.second % REPORT_EVERY and now == self._plc_last:
            return []
        self._plc_last = now
        metrics = [
            {"name": "state", "value": c.state},
            {"name": "alarm", "value": c.alarm},
            {"name": "speed_setpoint", "value": round(c.setpoint, 2), "unit": "m/s"},
            {"name": "cell_c3/parts_out", "value": self.parts_out, "unit": "count"},
        ]
        seq = self._plc_seq
        self._plc_seq = (seq + 1) % 256
        return [self._envelope("plc.cv2", PLC_TOPIC, {"seq": seq, "metrics": metrics})]

    # --- Fleet --------------------------------------------------------------------------------

    def _docked(self) -> Amr | None:
        return next(
            (a for a in self.amrs if a.node == "PICKUP" and a.task == "pickup" and not a.path), None
        )

    def _tote_ready(self) -> bool:
        docked = self._docked()
        return docked is not None and docked.tote < plant.TOTE_SIZE

    def _release_claim(self, amr: Amr) -> None:
        if self.claim == amr.id:
            self.claim = None
        if self.next_claim == amr.id:
            self.next_claim = None

    def _free_charger(self, amr: Amr) -> str | None:
        taken = {a.goal for a in self.amrs if a is not amr}
        free = [c for c in plant.CHARGERS if c not in taken]
        if amr.goal in free:
            return amr.goal
        return min(free, key=lambda c: len(route(amr.node, c)), default=None)

    def _go(self, amr: Amr, goal: str) -> None:
        if amr.path and self.occupied.get(amr.path[0]) == amr.id:
            first = amr.path[0]
            amr.path = [first, *route(first, goal)] if first != goal else [first]
        else:
            amr.path = route(amr.node, goal) if amr.node != goal else []

    def _order(self, amr: Amr, task: str, goal: str, *, forced: bool = False) -> None:
        self.orders += 1
        amr.task, amr.forced = task, forced
        amr.order_id, amr.order_state = f"T-{self.orders}", "driving"
        amr.charging = False
        self._go(amr, goal)

    def _fleet(self, line_on: bool, kpi: Kpis) -> None:
        for amr in self.amrs:
            self._arrive(amr)
        self._dispatch(line_on)
        for amr in self.amrs:
            self._drive(amr, kpi)

    def _arrive(self, amr: Amr) -> None:
        if amr.path or amr.stranded:
            return
        if amr.task == "pickup" and amr.node == "PICKUP":
            idle = self.loaded_at[amr.id] = self.loaded_at.get(amr.id, 0.0) + 1
            amr.order_state = "waiting" if idle >= LOAD_WAIT_AFTER else "loading"
            if amr.tote >= plant.TOTE_SIZE:
                self.claim = None
                self._order(amr, "deliver", "DROP")
        elif amr.task == "deliver" and amr.node == "DROP":
            amr.order_state = "unloading"
            amr.work += 1
            if amr.work >= UNLOAD_S:
                self.kpis[self.t.date().isoformat()].delivered += amr.tote
                amr.tote, amr.work, amr.task = 0, 0.0, None
                amr.order_id, amr.order_state = "", "idle"
        elif amr.task == "rescue" and amr.node in plant.CHARGERS:
            amr.mode, amr.task = "AUTOMATIC", "charge"
            amr.forced, amr.order_id, amr.order_state = True, "", "idle"
        elif amr.task == "stage" and amr.node == "STAGE":
            amr.order_state = "waiting"
        elif amr.task in ("charge", "park"):
            amr.order_id, amr.order_state = "", "idle"
        amr.charging = amr.node in plant.CHARGERS and amr.task == "charge" and amr.mode != "MANUAL"
        if amr.charging and amr.reads(self.truth) >= self.plan.charge_until:
            amr.forced = False

    def _free(self, amr: Amr) -> bool:
        """The fleet manager may give this robot a new order."""
        if amr.stranded or amr.paused or amr.held or amr.mode == "MANUAL" or amr.path:
            return False
        return amr.task is None or (amr.task in ("charge", "park") and not amr.forced)

    def _dispatch(self, line_on: bool) -> None:
        plan = self.plan
        for amr in self.amrs:
            if self._free(amr) and amr.reads(self.truth) < plan.charge_below:
                goal = self._free_charger(amr)
                if goal is None:  # both chargers taken: wait where it is
                    if amr.task is None:
                        amr.order_state = "waiting"
                    continue
                if amr.node == goal:
                    amr.task, amr.forced = "charge", True
                else:
                    self._order(amr, "charge", goal, forced=True)
        if line_on and self.claim is None and self.next_claim is not None:
            amr = next(a for a in self.amrs if a.id == self.next_claim)
            self.claim, self.next_claim = amr.id, None
            self.loaded_at[amr.id] = 0.0
            amr.task = "pickup"
            amr.order_state = "driving"
            self._go(amr, "PICKUP")
        docked = self._docked()
        if (
            line_on
            and plan.prestage
            and self.next_claim is None
            and docked is not None
            and docked.tote >= plan.prestage_at
        ):
            ready = [
                a for a in self.amrs if self._free(a) and a.reads(self.truth) >= plan.charge_below
            ]
            if ready:
                amr = max(ready, key=lambda a: (a.reads(self.truth), a.id))
                self.next_claim = amr.id
                self._order(amr, "stage", "STAGE")
        if line_on and self.claim is None:
            ready = [
                a for a in self.amrs if self._free(a) and a.reads(self.truth) >= plan.charge_below
            ]
            if ready:
                amr = max(ready, key=lambda a: (a.reads(self.truth), a.id))
                self.claim = amr.id
                self.loaded_at[amr.id] = 0.0
                self._order(amr, "pickup", "PICKUP")
        for amr in self.amrs:
            if not self._free(amr) or amr.task == "charge":
                continue
            top_up = plan.top_up_below
            charging = any(a.task == "charge" for a in self.amrs if a is not amr)
            if top_up is not None and not charging and amr.reads(self.truth) < top_up:
                goal = self._free_charger(amr)
                if goal is not None:
                    self._order(amr, "charge", goal)
                    continue
            if amr.task is None:
                park = next(
                    (p for p in plant.PARKING.values() if self.occupied.get(p) in (None, amr.id)),
                    None,
                )
                if park is not None:
                    self._order(amr, "park", park)

    def _drive(self, amr: Amr, kpi: Kpis) -> None:
        moving = False
        if amr.path and not amr.paused and not amr.stranded:
            nxt = amr.path[0]
            holder = self.occupied.get(nxt)
            if holder not in (None, amr.id):
                amr.blocked += 1
                if amr.blocked > 10 and amr.order_state == "driving":
                    amr.order_state = "blocked"
            else:
                self.occupied[nxt] = amr.id
                amr.blocked = 0.0
                if amr.order_state == "blocked":
                    amr.order_state = "driving"
                speed = plant.AMR_MANUAL_SPEED if amr.mode == "MANUAL" else plant.AMR_SPEED
                tx, ty = plant.NODES[nxt]
                dx, dy = tx - amr.x, ty - amr.y
                dist = math.hypot(dx, dy)
                moving = True
                if dist > 1e-9:
                    amr.yaw = math.atan2(dy, dx)
                if dist <= speed:
                    amr.x, amr.y = tx, ty
                    if self.occupied.get(amr.node) == amr.id and amr.node != nxt:
                        del self.occupied[amr.node]
                    amr.node = nxt
                    amr.path.pop(0)
                else:
                    amr.x += dx / dist * speed
                    amr.y += dy / dist * speed
        amr.zone = plant.zone_of(amr.x, amr.y) or amr.zone
        if amr.mode == "MANUAL":
            return
        if amr.charging:
            amr.battery = min(100.0, amr.battery + 100 / (50 * 60))
            return
        drain = (100 / (4 * 3600) if moving else 100 / (12 * 3600)) * self.truth.drain_of(amr.id)
        amr.battery = max(0.0, amr.battery - drain)
        if moving and amr.battery <= plant.STOP_BATTERY and not amr.stranded:
            amr.errors = [
                {
                    "errorType": "batteryLow",
                    "errorLevel": "FATAL",
                    "errorDescription": "pack voltage under cut-off; stopped",
                }
            ]
            amr.order_state = "stopped"
            kpi.amr_stranded += 1
            self._release_claim(amr)
            self._note(f"{amr.id} stopped in {amr.zone}: battery flat")
            self._job("rescue_amr", None, self.truth.rescue_s, f"robot:{amr.id}", at=(amr.x, amr.y))

    def _fleet_reports(self) -> list[Envelope]:
        out = []
        for amr in self.amrs:
            self._header += 1
            payload: dict[str, Any] = {
                "headerId": self._header,
                "serialNumber": SERIALS[amr.id],
                "version": amr.firmware,
                "operatingMode": amr.mode,
                "batteryState": {
                    "batteryCharge": round(amr.reads(self.truth), 1),
                    "charging": amr.charging,
                },
                "errors": amr.errors,
                "orderId": amr.order_id,
                "orderState": amr.order_state,
                "zone": amr.zone,
                "agvPosition": {
                    "x": round(amr.x, 2),
                    "y": round(amr.y, 2),
                    "theta": round(amr.yaw, 3),
                    "mapId": "p7",
                },
            }
            if amr.paused:
                payload["paused"] = True
            out.append(self._envelope("fleet", FLEET_TOPIC.format(robot=amr.id), payload))
        return out

    # --- People and jobs ----------------------------------------------------------------------

    def _job(
        self,
        kind: str,
        need: str | None,
        seconds: float,
        target: str,
        at: tuple[float, float] | None = None,
    ) -> None:
        where = at or plant.WORK_AT[kind]
        self.jobs.append(Job(kind, need, where, seconds, self.t, target, left=seconds))

    def _maintenance(self, line_on: bool) -> None:
        if not line_on:
            return
        for pm in self.maintenance:
            last = self.pm_last.get(pm.id)
            due = last is None or (self.t - last).total_seconds() >= pm.every_hours * 3600
            open_job = any(j.kind == "clean_pe3" for j in self.jobs)
            if due and not open_job:
                self.pm_last[pm.id] = self.t
                self._job("clean_pe3", "conveyor-lockout", self.truth.clean_pe3_s, pm.id)

    def _people(self, line_on: bool, kpi: Kpis) -> None:
        t = self.t
        for job in self.jobs:
            if job.person is not None:
                continue
            free = [
                p
                for p in self.people.values()
                if p.job is None
                and p.shift(t) is not None
                and not p.on_break(t)
                and p.spec.role == "maintenance tech"
                and (job.need is None or job.need in p.spec.qualifications)
            ]
            if free:
                person = min(free, key=lambda p: (math.dist((p.x, p.y), job.at), p.spec.name))
                person.job, job.person = job, person.spec.name
        jam_open = self.conveyor.jammed
        for person in sorted(self.people.values(), key=lambda p: p.spec.name):
            work = person.job
            if work is not None:
                person.present = True
                if work.started is None:
                    if person.walk(work.at):
                        work.started = t
                        if work.kind == "clean_pe3":
                            self.conveyor.cleaning = True
                    person.activity = "walking"
                    continue
                person.activity = {
                    "clear_jam": "clearing_jam",
                    "reset_arm": "resetting_arm",
                    "clean_pe3": "cleaning_pe3",
                    "rescue_amr": "rescuing_amr",
                }[work.kind]
                work.left -= 1
                if work.left <= 0:
                    self._done(work, person, kpi)
                    person.job = None
                continue
            current = person.shift(t)
            if current is None:
                if person.present and person.walk(plant.PLACES["entrance"]):
                    person.present = False
                person.activity = "off_shift" if not person.present else "leaving"
                continue
            if not person.present:
                person.present = True
                person.x, person.y = plant.PLACES["entrance"]
            if person.on_break(t):
                person.walk(plant.PLACES["break_room"])
                person.activity = "on_break"
            elif person.spec.role == "line lead" and jam_open:
                person.activity = (
                    "watching" if person.walk(plant.WORK_AT["watch_jam"]) else "walking"
                )
            else:
                home = plant.PLACES["office" if person.spec.role == "line lead" else "shop"]
                there = person.walk(home)
                person.activity = (
                    ("supervising" if person.spec.role == "line lead" else "idle")
                    if there
                    else "walking"
                )

    def _done(self, job: Job, person: Person, kpi: Kpis) -> None:
        name = person.spec.name
        self.jobs.remove(job)
        if job.kind == "clear_jam":
            self.conveyor.clear()
            kpi.jam_clear_s.append((self.t - job.created).total_seconds())
            cause = (
                "PE3 lens dirty"
                if self.conveyor.dirt > 0.7
                else self.rng.choice(["belt tracking off", "part skewed at PE3"])
            )
            self._work_order(job, name, "CV-2 stopped; jam at PE3", "Cleared jam at PE3", cause)
            self._note(f"CV-2 jam cleared by {name}")
            if on_shift(self.t) and self.rng.random() < self.truth.pstop_on_restart:
                self.arm.mode, self.arm.fault = "protective_stop", "PSTOP_C3"
                kpi.pstops += 1
                text = (
                    "protective stop: part presence lost at pick as the conveyor restarted; "
                    "awaiting reset"
                )
                self._arm_out.append(self._arm_line("protective_stop", text))
                self._note("arm C3 protective stop")
                self._job("reset_arm", "arm-reset-c3", self.truth.reset_arm_s, "robot:arm-c3")
        elif job.kind == "reset_arm":
            self.arm.mode, self.arm.fault = "running", ""
            self.arm.waited = 0.0
            text = f"reset acknowledged by user {name.lower()}; cycle resumed"
            self._arm_out.append(self._arm_line("reset", text, user=name.lower()))
            self._note(f"arm C3 reset by {name}")
        elif job.kind == "clean_pe3":
            self.conveyor.dirt, self.conveyor.cleaning = 0.0, False
            self._work_order(job, name, "PM: PE3 photo-eye", "Cleaned PE3 lens", "preventive")
            self._note(f"PE3 cleaned by {name}")
        elif job.kind == "rescue_amr":
            amr = next(a for a in self.amrs if f"robot:{a.id}" == job.target)
            amr.errors, amr.mode, amr.task = [], "MANUAL", "rescue"
            amr.order_id, amr.order_state = "", "idle"
            goal = self._free_charger(amr) or plant.CHARGERS[0]
            self._go(amr, goal)
            self._note(f"{amr.id} pushed to charge by {name}")

    def _work_order(self, job: Job, name: str, problem: str, action: str, cause: str) -> None:
        self.work_order += 1
        opened = job.started or job.created
        self.work_orders.append(
            {
                "WO": f"WO-{self.work_order}",
                "Asset": "MR-1141",
                "Equipment": "CV-2",
                "Opened": opened.isoformat(timespec="seconds"),
                "Closed": self.t.isoformat(timespec="seconds"),
                "Problem": problem,
                "Action": action,
                "Cause": cause,
                "Done by": name,
                "LOTO": "yes",
                "Note": "",
            }
        )

    def _note(self, text: str) -> None:
        self.events.append(text)
        self.news.append(f"{self.t:%Y-%m-%d %H:%M:%S} {text}")

    # --- Output -------------------------------------------------------------------------------

    def _envelope(self, source: str, topic: str, payload: dict[str, Any]) -> Envelope:
        return {
            "t": self.t.isoformat(timespec="seconds"),
            "source": source,
            "topic": topic,
            "payload": payload,
        }

    def frame(self) -> dict[str, Any]:
        """Everything a renderer draws at this second (one ``sim/timeline.jsonl`` line)."""
        c = self.conveyor
        joints, tcp = self.arm_pose()
        events, self.events = self.events, []
        return {
            "t": self.t.isoformat(timespec="seconds"),
            "conveyor": {
                "state": c.state,
                "alarm": c.alarm,
                "setpoint": round(c.setpoint, 2),
                "travel": round(c.travel, 2),
                "lens_dirt": round(c.dirt, 3),
                "parts": [
                    {
                        "id": p.id,
                        "s": round(p.s, 2),
                        "xy": list(plant.belt_xy(p.s)),
                        "jammed": p.jammed,
                    }
                    for p in c.parts
                ],
            },
            "arm": {
                "mode": self.arm.mode,
                "phase": self.arm.phase,
                "holding": self.arm.holding,
                "joints": joints,
                "tcp": tcp,
            },
            "amrs": [
                {
                    "id": f"robot:{a.id}",
                    "x": round(a.x, 3),
                    "y": round(a.y, 3),
                    "yaw": round(a.yaw, 3),
                    "mode": a.mode,
                    "battery": round(a.reads(self.truth), 1),
                    "true_battery": round(a.battery, 1),
                    "charging": a.charging,
                    "tote": a.tote,
                    "task": a.task,
                    "order_state": a.order_state,
                    "stranded": a.stranded,
                }
                for a in self.amrs
            ],
            "people": [
                {
                    "id": p.id,
                    "role": p.spec.role,
                    "x": round(p.x, 3),
                    "y": round(p.y, 3),
                    "yaw": round(p.yaw, 3),
                    "activity": p.activity,
                    "present": p.present,
                }
                for p in sorted(self.people.values(), key=lambda p: p.spec.name)
            ],
            "events": events,
        }

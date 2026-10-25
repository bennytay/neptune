"""A small hand-built site model for the query tests: a slice of Plant 7 on 2026-10-20 (spec §9).

``FakeModel`` offers what ``neptune_memory.site.query.SiteModel`` reads, folded naively from a
list of facts, so the query tests do not wait on the real views (#185).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from neptune_memory.site.facts import KIND_RANK, Citation, Fact, Kind, Ref, Seen, State, Value

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

ONE = {"mode", "health", "battery", "in_zone", "current_task", "setpoint", "task_state", "serial"}
TOPOLOGY = {"feeds", "depends_on", "located_in", "part_of", "assigned_to", "covers_zone"}
EXPECTED = {"machine": ("belt_tension",)}
REPORT_INTERVAL = 5  # seconds, every feed source


@dataclass(frozen=True)
class FakeCell:
    subject: str
    predicate: str
    state: State
    facts: tuple[Fact, ...]


@dataclass(frozen=True)
class FakeInterval:
    fact: Fact
    start: str | None
    end: str | None


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
        "fleet.amr-03",
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

SEEN: tuple[Seen, ...] = (
    Seen("fleet.amr-03", "2026-10-20T09:40:05"),
    Seen("plc.cv2", "2026-10-20T10:35:00"),
    Seen("arm.c3", "2026-10-20T10:35:00"),
    Seen("fleet", "2026-10-20T10:35:00"),
    Seen("plc.cv2", "2026-10-20T11:15:00"),
)


def _seconds(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)) / timedelta(seconds=1)


class FakeModel:
    """A naive fold of ``FACTS`` and ``SEEN``: the spec §6 views, small and slow."""

    def __init__(self, facts: Iterable[Fact] = FACTS, seen: Iterable[Seen] = SEEN) -> None:
        self._facts = {f.id: f for f in facts}
        self._seen = tuple(seen)

    @property
    def as_of_seq(self) -> int:
        return len(self._facts) + len(self._seen)

    @property
    def facts(self) -> Mapping[str, Fact]:
        return self._facts

    @property
    def seen(self) -> Sequence[Seen]:
        return self._seen

    def _current(self, t: str) -> list[Fact]:
        return [
            f
            for f in self._facts.values()
            if (f.valid_from is None or f.valid_from <= t)
            and (f.valid_to is None or t < f.valid_to)
        ]

    def _stale(self, f: Fact, t: str) -> bool:
        last = max((s.t for s in self._seen if s.source == f.source and s.t <= t), default=None)
        return last is not None and _seconds(last, t) > 3 * REPORT_INTERVAL

    def state_at(self, t: str) -> list[FakeCell]:
        cells: list[FakeCell] = []
        latest: dict[tuple[str, str], list[Fact]] = {}
        for f in self._current(t):
            if f.predicate not in ONE:
                cells.append(FakeCell(f.subject, f.predicate, "known", (f,)))
                continue
            held = latest.setdefault((f.subject, f.predicate), [])
            if held and (held[0].valid_from or "") < (f.valid_from or ""):
                held.clear()
            if not held or held[0].valid_from == f.valid_from:
                held.append(f)
        for (subject, predicate), held in latest.items():
            top = max(KIND_RANK[f.kind] for f in held)
            winners = tuple(f for f in held if KIND_RANK[f.kind] == top)
            state: State = "ambiguous" if len({w.object for w in winners}) > 1 else "known"
            if state == "known" and self._stale(winners[0], t):
                state = "stale"
            cells.append(
                FakeCell(
                    subject, predicate, state, winners[:1] if state != "ambiguous" else winners
                )
            )
        for entity in {f.subject for f in self._facts.values()}:
            for predicate in EXPECTED.get(entity.split(":", 1)[0], ()):
                if (entity, predicate) not in latest:
                    cells.append(FakeCell(entity, predicate, "unknown", ()))
        return cells

    def history(self, subject: str) -> list[FakeInterval]:
        out: list[FakeInterval] = []
        mine = sorted(
            (f for f in self._facts.values() if f.subject == subject),
            key=lambda f: (f.predicate, f.valid_from or ""),
        )
        for i, f in enumerate(mine):
            nxt = mine[i + 1] if i + 1 < len(mine) else None
            end = f.valid_to
            if f.predicate in ONE and nxt is not None and nxt.predicate == f.predicate:
                end = nxt.valid_from
            out.append(FakeInterval(f, f.valid_from, end))
        return sorted(out, key=lambda i: i.start or "")

    def topology(self) -> list[Fact]:
        return [f for f in self._facts.values() if f.predicate in TOPOLOGY]

    def identity(self, entity: str) -> frozenset[str]:
        group = {entity}
        for f in self._facts.values():
            if (
                f.predicate == "same_as"
                and f.kind != "inferred"
                and isinstance(f.object, Ref)
                and (f.subject in group or f.object.ref in group)
            ):
                group |= {f.subject, f.object.ref}
        return frozenset(group)

    def pending(self) -> list[Fact]:
        return [
            f for f in self._facts.values() if f.kind == "inferred" and f.predicate == "same_as"
        ]

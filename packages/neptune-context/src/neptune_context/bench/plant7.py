"""The Plant 7 benchmark set: questions whose answers come from the simulator, never from Neptune.

``generate`` runs the seeded plant (``site/sim``) second by second, exactly as ``sim run`` does, so
the feed bytes are the same, and watches the engine as it goes. The answer key is the engine's own
state: which fault it raised, who was on shift, on break and qualified at that second, what the
counters said. This module imports nothing from ``neptune_memory.site`` outside ``sim/``.

- **First-fault response** (``F*``): two minutes after each fault (a CV-2 jam, an arm protective
  stop, an AMR stopped with a flat battery), what is wrong, who can fix it right now and what to
  do. "Who" is graded against the roster at that second: a maintenance tech on shift, not on
  break, holding the qualification the fix needs.
- **Unsure** (``U*``, ``S*``, ``D*``): questions whose true answer is unknown, stale or pending,
  so an answer that gives a value made it up.
- **Lookups** (``L*``): facts about the first day, asked of a 1-day and a 7-day site alike, so
  tokens per answer can be compared as the evidence grows.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from neptune_memory.site.sim import run as sim
from neptune_memory.site.sim.engine import REPORT_EVERY

if TYPE_CHECKING:
    from pathlib import Path

    from neptune_memory.site.sim.engine import Engine

SEED = 7
DAYS = {"plant7-1d": 1, "plant7-7d": 7}
ASK_AFTER = timedelta(minutes=2)
FIX = {
    "jam": ("conveyor-lockout", "clear the jam at photo-eye PE3 under lockout (LOTO)"),
    "pstop": ("arm-reset-c3", "reset arm C3's protective stop so its cycle resumes"),
    "flat": (
        None,
        "it needs charging: recover it to a charger (saying no recovery procedure is documented "
        "is fine)",
    ),
}
ASK = (
    "It is {t} (site time). What is faulted on the line right now, who on site can fix it "
    "right now, and what should they do?"
)


@dataclass
class Fault:
    kind: str  # jam | pstop | flat
    target: str  # machine:cv-2 | robot:arm-c3 | robot:amr-0N
    at: datetime
    zone: str = ""


def _when(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _fault(news: str, t: datetime) -> Fault | None:
    text = news[20:]
    if text == "CV-2 jam at PE3":
        return Fault("jam", "machine:cv-2", t)
    if text == "arm C3 protective stop":
        return Fault("pstop", "robot:arm-c3", t)
    if text.endswith(": battery flat"):
        robot, _, rest = text.partition(" stopped in ")
        return Fault("flat", f"robot:{robot}", t, rest.split(":")[0] + ":" + rest.split(":")[1])
    return None


def _still_on(engine: Engine, fault: Fault) -> bool:
    if fault.kind == "jam":
        return engine.conveyor.jammed
    if fault.kind == "pstop":
        return engine.arm.mode == "protective_stop"
    amr = next(a for a in engine.amrs if f"robot:{a.id}" == fault.target)
    return amr.stranded


def _able(engine: Engine, need: str | None, t: datetime) -> list[str]:
    """Who the roster says can do it at ``t``: maintenance techs on shift, off break, qualified."""
    return sorted(
        p.spec.name
        for p in engine.people.values()
        if p.spec.role == "maintenance tech"
        and p.shift(t) is not None
        and not p.on_break(t)
        and (need is None or need in p.spec.qualifications)
    )


def _next_able(engine: Engine, need: str | None, t: datetime) -> str:
    """The first second after ``t`` that someone becomes able, as "<name> from <time>"."""
    for minutes in range(1, 24 * 60):
        later = t + timedelta(minutes=minutes)
        names = _able(engine, need, later.replace(second=0))
        if names:
            return f"{', '.join(names)} from about {later:%H:%M}"
    return "nobody within a day"


def _first_fault_question(n: int, engine: Engine, fault: Fault) -> dict[str, Any]:
    t = engine.t
    need, action = FIX[fault.kind]
    names = _able(engine, need, t)
    who_state = "known"
    if fault.kind == "flat":
        # No site file says who may recover a robot: the simulator sends any tech, the evidence
        # names nobody, so the honest answer is that it is not documented.
        who_state = "unknown"
        who = "not documented: no qualification or procedure names who may recover a robot"
    elif names:
        who = f"at least one of {', '.join(names)}, and nobody off shift, on break or unqualified"
    else:
        later = _next_able(engine, need, t)
        who = f"nobody right now (no qualified tech is on shift and off break); {later}"
    if fault.kind == "jam":
        what = {"subject": "machine:cv-2", "predicate": "health", "object": "fault:JAM_PE3"}
    elif fault.kind == "pstop":
        what = {"subject": "robot:arm-c3", "predicate": "mode", "object": "protective_stop"}
    else:
        what = {
            "subject": fault.target,
            "predicate": "health",
            "object": f"stopped with a flat battery (batteryLow) in {fault.zone}",
        }
    return {
        "id": f"F{n:02d}",
        "question": ASK.format(t=_when(t)),
        "kind": "first_fault",
        "fault": fault.kind,
        "expect": [
            {**what, "state": "known"},
            {
                "subject": fault.target,
                "predicate": "who_can_fix_now",
                "object": who,
                "state": who_state,
            },
            {
                "subject": fault.target,
                "predicate": "what_to_do",
                "object": action,
                "state": "known",
            },
        ],
    }


def _unsure(days: int) -> list[dict[str, Any]]:
    """Questions whose answer is unknown, stale or pending in this site's evidence."""
    end = sim.windows(days)[-1][0].date()
    after = (end + timedelta(days=1)).isoformat()
    out = [
        {
            "id": "U1",
            "question": "What firmware version is arm C3's controller running?",
            "expect": [{"subject": "robot:arm-c3", "predicate": "firmware", "state": "unknown"}],
        },
        {
            "id": "U2",
            "question": f"What is CV-2's drive motor temperature at {end} 10:00:00?",
            "expect": [
                {"subject": "machine:cv-2", "predicate": "motor_temperature", "state": "unknown"}
            ],
        },
        {
            "id": "U3",
            "question": f"Who is on shift on {after}, and who can reset arm C3 that day?",
            "expect": [
                {"subject": "shift:A", "predicate": f"roster on {after}", "state": "unknown"}
            ],
        },
        {
            "id": "U4",
            "question": f"How many parts did cell C3 put out on {after}?",
            "expect": [
                {"subject": "cell:c3", "predicate": f"parts_out on {after}", "state": "unknown"}
            ],
        },
        {
            "id": "D1",
            "question": "Is fleet robot amr-02 the same machine as CMMS asset MR-1142? "
            "(No operator has confirmed anything yet.)",
            "expect": [
                {
                    "subject": "robot:amr-02",
                    "predicate": "same_as",
                    "object": "robot:MR-1142",
                    "state": "pending",
                }
            ],
        },
    ]
    for n, (start, _) in enumerate(sim.windows(days)):
        robot = f"amr-0{n % 3 + 1}"
        at = start.replace(hour=18, minute=0, second=0)
        out.append(
            {
                "id": f"S{n + 1}",
                "question": f"It is {_when(at)} (site time). What is {robot}'s battery level "
                "right now, and is CV-2 running?",
                "expect": [
                    {"subject": f"robot:{robot}", "predicate": "battery", "state": "stale"},
                    {"subject": "machine:cv-2", "predicate": "mode", "state": "stale"},
                ],
            }
        )
    return [{**q, "kind": "unsure"} for q in out]


def _lookups(day1: dict[str, Any]) -> list[dict[str, Any]]:
    d = day1["date"]
    out = [
        {
            "id": "L1",
            "question": f"How many times did CV-2 jam on {d}, and who cleared each jam?",
            "expect": [
                {
                    "subject": "machine:cv-2",
                    "predicate": f"jams on {d}",
                    "object": str(len(day1["jams"])),
                    "state": "known",
                },
                {
                    "subject": "machine:cv-2",
                    "predicate": "cleared by",
                    "object": ", ".join(day1["jams"]),
                    "state": "known",
                },
            ],
        },
        {
            "id": "L2",
            "question": f"How many parts did cell C3 put out on {d}?",
            "expect": [
                {
                    "subject": "cell:c3",
                    "predicate": f"parts_out on {d}",
                    "object": f"{day1['parts_out']} (within 2)",
                    "state": "known",
                }
            ],
        },
    ]
    if day1["pstops"]:
        at, who = day1["pstops"][0]
        out.append(
            {
                "id": "L3",
                "question": f"When did arm C3 first go into a protective stop on {d}, "
                "and who reset it?",
                "expect": [
                    {
                        "subject": "robot:arm-c3",
                        "predicate": "first protective stop",
                        "object": at,
                        "state": "known",
                    },
                    {
                        "subject": "robot:arm-c3",
                        "predicate": "reset by",
                        "object": who,
                        "state": "known",
                    },
                ],
            }
        )
    if day1["flat"]:
        at, robot, zone = day1["flat"][0]
        out.append(
            {
                "id": "L4",
                "question": f"Which mobile robot ran its battery flat on {d}, where and when?",
                "expect": [
                    {
                        "subject": f"robot:{robot}",
                        "predicate": "battery flat",
                        "object": f"first reported at {at} in {zone}",
                        "state": "known",
                    },
                ],
            }
        )
    return [{**q, "kind": "lookup"} for q in out]


def _holdout(kpis: dict[str, Any], resets: list[str], flats: list[str]) -> list[dict[str, Any]]:
    """Whole-period aggregates, a kind of question the tools were not designed around: a check
    that a gain on the other questions is not tuning to them."""
    days = sorted(kpis)
    span = f"from {days[0]} to {days[-1]}" if len(days) > 1 else f"on {days[0]}"
    clears = [c for d in days for c in kpis[d]["jam_clear_s"]]
    best = max(days, key=lambda d: kpis[d]["parts_out"])
    out = [
        {
            "id": "H1",
            "question": f"How many times did CV-2 jam {span}, and what was the longest time from "
            "a jam to its clearing?",
            "expect": [
                {
                    "subject": "machine:cv-2",
                    "predicate": "jams",
                    "object": str(sum(kpis[d]["jams"] for d in days)),
                    "state": "known",
                },
                {
                    "subject": "machine:cv-2",
                    "predicate": "longest jam to clear",
                    "object": f"about {max(clears) / 60:.0f} minutes (within 2)",
                    "state": "known",
                },
            ],
        },
        {
            "id": "H3",
            "question": f"How many protective stops did arm C3 have {span}, and who reset them?",
            "expect": [
                {
                    "subject": "robot:arm-c3",
                    "predicate": "protective stops",
                    "object": str(len(resets)),
                    "state": "known",
                },
                {
                    "subject": "robot:arm-c3",
                    "predicate": "reset by",
                    "object": ", ".join(sorted(set(resets))) or "nobody",
                    "state": "known",
                },
            ],
        },
    ]
    if len(days) > 1:
        out.append(
            {
                "id": "H2",
                "question": f"On which day {span} did cell C3 put out the most parts, "
                "and how many?",
                "expect": [
                    {
                        "subject": "cell:c3",
                        "predicate": "best day",
                        "object": f"{best}, {kpis[best]['parts_out']} parts (within 2)",
                        "state": "known",
                    },
                ],
            }
        )
    if flats:
        top = max(sorted(set(flats)), key=flats.count)
        out.append(
            {
                "id": "H4",
                "question": f"Which mobile robot ran its battery flat most often {span}, "
                "and how many times?",
                "expect": [
                    {
                        "subject": top,
                        "predicate": "battery flat",
                        "object": f"{flats.count(top)} times",
                        "state": "known",
                    },
                ],
            }
        )
    return [{**q, "kind": "holdout"} for q in out]


def generate(site_dir: Path, days: int, seed: int = SEED) -> list[dict[str, Any]]:
    """Write the simulated site into ``site_dir`` and return its questions with their answers."""
    shutil.rmtree(site_dir, ignore_errors=True)
    sim.prepare(site_dir, days=days)
    engine = sim.make_engine(site_dir, seed)
    outputs = sim._Outputs(site_dir, frames=0)
    handles = {}
    for source, rel in sim.FEEDS.items():
        (site_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        handles[source] = (site_dir / rel).open("w", encoding="utf-8")
    pending: list[Fault] = []
    asked: list[dict[str, Any]] = []
    day1: dict[str, Any] = {"jams": [], "pstops": [], "flat": []}
    first_day = sim.windows(days)[0][0].date().isoformat()
    resets: list[str] = []
    flats: list[str] = []
    try:
        for start, end in sim.windows(days):
            if engine.t < start:
                engine.jump(start)
            while engine.t < end:
                for envelope in engine.step():
                    handles[envelope["source"]].write(sim._dumps(envelope) + "\n")
                for news in engine.news:
                    fault = _fault(news, engine.t)
                    if fault is not None:
                        pending.append(fault)
                    text_all = news[20:]
                    if text_all.startswith("arm C3 reset by "):
                        resets.append(text_all.rsplit(" ", 1)[1])
                    elif fault is not None and fault.kind == "flat":
                        flats.append(fault.target)
                    if news.startswith(first_day):
                        text = news[20:]
                        if text.startswith("CV-2 jam cleared by "):
                            day1["jams"].append(text.rsplit(" ", 1)[1])
                        elif text.startswith("arm C3 reset by "):
                            day1["pstops"][-1] = (day1["pstops"][-1][0], text.rsplit(" ", 1)[1])
                        elif text == "arm C3 protective stop":
                            day1["pstops"].append((news[11:19], "?"))
                        elif fault is not None and fault.kind == "flat":
                            seen = engine.t + timedelta(seconds=-engine.t.second % REPORT_EVERY)
                            day1["flat"].append(
                                (f"{seen:%H:%M:%S}", fault.target.split(":")[1], fault.zone)
                            )
                for fault in [f for f in pending if engine.t >= f.at + ASK_AFTER]:
                    pending.remove(fault)
                    if _still_on(engine, fault):
                        asked.append(_first_fault_question(len(asked) + 1, engine, fault))
                outputs.after_step(engine)
    finally:
        for handle in handles.values():
            handle.close()
    summary = outputs.close(engine, seed, days)
    day1["date"] = first_day
    day1["parts_out"] = summary["kpis"][first_day]["parts_out"]
    return (
        asked
        + _unsure(days)
        + _lookups(day1)
        + _holdout(
            {
                d: {"parts_out": k.parts_out, "jams": k.jams, "jam_clear_s": k.jam_clear_s}
                for d, k in engine.kpis.items()
            },
            resets,
            flats,
        )
    )


def write(site_dir: Path, key: Path, days: int, seed: int = SEED) -> None:
    """Generate the site and write its questions to ``key`` (outside the site folder)."""
    questions = generate(site_dir, days, seed)
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text(
        json.dumps({"seed": seed, "days": days, "questions": questions}, indent=1) + "\n",
        encoding="utf-8",
    )

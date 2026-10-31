"""Run the plant: offline into feed files, or live over MQTT beside ``live``.

- ``prepare`` writes Plant 7's static corpus without its scripted feed: the work-order export keeps
  only the history before the simulated days, and the roster gets one shift A row per person per
  day (Okafor's break 10:30-11:00 every day).
- ``run`` steps the plant through each day's window (05:30-14:30; nights are skipped) and writes
  ``feed/plc_cv2.jsonl``, ``feed/fleet.jsonl``, ``feed/arm_c3.jsonl``, so ``build`` reads them like
  the corpus. Commands recorded by a live run (``--commands``) are applied at the same site second,
  so a live run can be reproduced offline byte for byte.
- ``run_live`` publishes each envelope on its topic at ``speed`` x site time, takes commands on the
  robots' VDA 5050 topics and the PLC's Sparkplug ``DCMD`` topic, and records them in
  ``sim/commands.jsonl``. It re-reads the roster, qualifications and ``cmms/pm_schedule.csv`` when
  they change, so a person or schedule change in ``static/`` reaches the plant.

Both append the plant's work orders to ``static/cmms/work_orders.csv`` as they close, and write
``sim/scene.json`` (the floor plan), ``sim/timeline.jsonl`` (one frame per ``frames`` site
seconds, for a renderer) and ``sim/summary.json`` (KPIs per day: the ground truth, which Neptune
never reads).
"""

from __future__ import annotations

import csv
import json
import queue
import shutil
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from neptune_memory.site import corpus
from neptune_memory.site.corpus import static
from neptune_memory.site.sim import plant
from neptune_memory.site.sim.engine import Engine, Maintenance, PersonSpec, Shift

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

DAY1 = date(2026, 10, 20)
WINDOW = (timedelta(hours=5, minutes=30), timedelta(hours=14, minutes=30))
FEEDS = {
    "plc.cv2": "feed/plc_cv2.jsonl",
    "fleet": "feed/fleet.jsonl",
    "arm.c3": "feed/arm_c3.jsonl",
}
SIM_DIR = "sim"
ROSTER = "static/roster/shift_roster.csv"
QUALIFICATIONS = "static/roster/qualifications.csv"
WORK_ORDERS = "static/cmms/work_orders.csv"
PM_SCHEDULE = "static/cmms/pm_schedule.csv"
COMMAND_TOPICS = ("uagv/v2/p7/+/instantActions", "uagv/v2/p7/+/order", "spBv1.0/P7/DCMD/+/+")


def windows(days: int, until: str | None = None) -> list[tuple[datetime, datetime]]:
    """Each simulated day's window, cut at site time ``until``."""
    last = datetime.fromisoformat(until) if until else datetime.max
    out = []
    for n in range(days):
        midnight = datetime.combine(DAY1 + timedelta(days=n), datetime.min.time())
        start, end = midnight + WINDOW[0], min(midnight + WINDOW[1], last)
        if start < end:
            out.append((start, end))
    return out


def prepare(site_dir: Path, days: int = 1) -> None:
    """Plant 7's static corpus for ``days`` simulated days, and no feed yet."""
    site_dir = Path(site_dir)
    corpus.generate(site_dir, site="p7")
    for path in (site_dir / "feed").glob("*.jsonl"):
        path.unlink()
    for name in ("feed/live", "actions", SIM_DIR, ".neptune"):
        shutil.rmtree(site_dir / name, ignore_errors=True)
    history = static.WORK_ORDERS.splitlines()[:2]  # header, WO-5521 (the day before)
    (site_dir / WORK_ORDERS).write_text("\n".join(history) + "\n", encoding="utf-8")
    rows = [line for line in static.SHIFT_ROSTER.splitlines() if "2026-10-20" not in line]
    for start, _ in windows(days):
        day = start.date().isoformat()
        for person, role, brk in (
            ("Okafor", "maintenance tech", ("10:30", "11:00")),
            ("Lindqvist", "maintenance tech", None),
            ("Patel", "line lead", None),
        ):
            b0, b1 = (f"{day}T{brk[0]}:00", f"{day}T{brk[1]}:00") if brk else ("", "")
            rows.append(f"{person},{role},A,{day}T06:00:00,{day}T14:00:00,{b0},{b1}")
    (site_dir / ROSTER).write_text("\n".join(rows) + "\n", encoding="utf-8")


def read_people(site_dir: Path) -> list[PersonSpec]:
    """The roster and qualifications as the plant's people follow them."""
    quals: dict[str, set[str]] = {}
    with (site_dir / QUALIFICATIONS).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            quals.setdefault(row["Person"], set()).add(row["Qualification"])
    roles: dict[str, str] = {}
    shifts: dict[str, list[Shift]] = {}
    with (site_dir / ROSTER).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            name = row["Person"]
            roles.setdefault(name, row["Role"])
            breaks: tuple[tuple[datetime, datetime], ...] = ()
            if row.get("Break start") and row.get("Break end"):
                breaks = (
                    (
                        datetime.fromisoformat(row["Break start"]),
                        datetime.fromisoformat(row["Break end"]),
                    ),
                )
            start, end = datetime.fromisoformat(row["Start"]), datetime.fromisoformat(row["End"])
            shifts.setdefault(name, []).append(Shift(start, end, breaks))
    return [
        PersonSpec(name, roles[name], frozenset(quals.get(name, ())), tuple(shifts[name]))
        for name in sorted(roles)
    ]


def read_maintenance(site_dir: Path) -> list[Maintenance]:
    path = site_dir / PM_SCHEDULE
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            Maintenance(row["PM"], row["Equipment"], row["Task"], float(row["Every hours"]))
            for row in csv.DictReader(handle)
            if "PE3" in row["Task"]
        ]


def make_engine(site_dir: Path, seed: int) -> Engine:
    start = windows(1)[0][0]
    engine = Engine(start, read_people(site_dir), seed=seed)
    engine.set_maintenance(read_maintenance(site_dir))
    engine.amrs[2].firmware = "4.1.0"
    engine.at(start.replace(hour=5, minute=40), "setpoint", 0.95)  # CHG-311
    engine.at(start.replace(hour=5, minute=50), "firmware:amr-03", "4.2.0")
    return engine


@dataclass(frozen=True)
class Command:
    t: str
    topic: str
    payload: dict[str, Any]


def read_commands(path: Path) -> list[Command]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            data = json.loads(line)
            out.append(Command(data["t"], data["topic"], data["payload"]))
    return out


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class _Outputs:
    """The files a run writes besides the feed: work orders, timeline, scene, summary."""

    def __init__(self, site_dir: Path, frames: int) -> None:
        self.site_dir = site_dir
        self.frames = frames
        sim = site_dir / SIM_DIR
        sim.mkdir(parents=True, exist_ok=True)
        (sim / "scene.json").write_text(
            json.dumps(plant.scene(), indent=1) + "\n", encoding="utf-8"
        )
        self.timeline: TextIO | None = (
            (sim / "timeline.jsonl").open("w", encoding="utf-8") if frames else None
        )

    def after_step(self, engine: Engine) -> None:
        if self.timeline is not None and int(engine.t.timestamp()) % self.frames == 0:
            self.timeline.write(_dumps(engine.frame()) + "\n")
        elif self.timeline is None:
            engine.events.clear()
        engine.news.clear()
        if engine.work_orders:
            with (self.site_dir / WORK_ORDERS).open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, list(engine.work_orders[0]), lineterminator="\n")
                writer.writerows(engine.work_orders)
            engine.work_orders = []

    def close(self, engine: Engine, seed: int, days: int) -> dict[str, Any]:
        if self.timeline is not None:
            self.timeline.close()
        summary = {
            "seed": seed,
            "days": days,
            "kpis": {day: k.summary() for day, k in sorted(engine.kpis.items())},
        }
        path = self.site_dir / SIM_DIR / "summary.json"
        path.write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
        return summary


def run(
    site_dir: Path,
    *,
    days: int = 1,
    seed: int = 7,
    commands: Path | None = None,
    frames: int = 1,
    until: str | None = None,
) -> dict[str, Any]:
    """Simulate ``days`` offline into ``feed/`` (up to site time ``until``, if given); returns the
    summary. Call ``prepare`` first."""
    site_dir = Path(site_dir)
    engine = make_engine(site_dir, seed)
    pending = read_commands(commands) if commands else []
    outputs = _Outputs(site_dir, frames)
    handles = {}
    for source, rel in FEEDS.items():
        (site_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        handles[source] = (site_dir / rel).open("w", encoding="utf-8")
    try:
        for start, end in windows(days, until):
            if engine.t < start:
                engine.jump(start)
            while engine.t < end:
                now = engine.t.isoformat(timespec="seconds")
                while pending and pending[0].t <= now:
                    command = pending.pop(0)
                    engine.command(command.topic, command.payload)
                for envelope in engine.step():
                    handles[envelope["source"]].write(_dumps(envelope) + "\n")
                outputs.after_step(engine)
    finally:
        for handle in handles.values():
            handle.close()
    return outputs.close(engine, seed, days)


def run_live(
    site_dir: Path,
    broker: str,
    *,
    speed: float = 60.0,
    days: int = 1,
    seed: int = 7,
    frames: int = 1,
    until: str | None = None,
    stop: threading.Event | None = None,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run the plant at ``speed`` x site time over MQTT, answering commands as they arrive."""
    from neptune_memory.site.live import _client, _connect

    site_dir = Path(site_dir)
    engine = make_engine(site_dir, seed)
    outputs = _Outputs(site_dir, frames)
    inbox: queue.Queue[tuple[str, bytes]] = queue.Queue()
    client = _client("neptune-sim")
    client.on_connect = lambda c, *_: [c.subscribe(t, qos=1) for t in COMMAND_TOPICS]
    client.on_message = lambda _c, _u, m: inbox.put((m.topic, m.payload))
    _connect(client, broker)
    client.loop_start()
    watched = [site_dir / p for p in (ROSTER, QUALIFICATIONS, PM_SCHEDULE)]
    stamps = [p.stat().st_mtime_ns if p.exists() else 0 for p in watched]
    recorded = (site_dir / SIM_DIR / "commands.jsonl").open("a", encoding="utf-8")
    try:
        for start, end in windows(days, until):
            if engine.t < start:
                engine.jump(start)
            say(f"sim: {start:%Y-%m-%d} shift window, {speed:g}x")
            wall0, site0 = time.monotonic(), engine.t
            while engine.t < end and not (stop and stop.is_set()):
                while not inbox.empty():
                    topic, raw = inbox.get()
                    payload = json.loads(raw)
                    done = engine.command(topic, payload)
                    now = engine.t.isoformat(timespec="seconds")
                    recorded.write(_dumps({"t": now, "topic": topic, "payload": payload}) + "\n")
                    recorded.flush()
                    say(f"sim {now}: command {topic}: {done}")
                for envelope in engine.step():
                    raw_env = _dumps(envelope).encode("utf-8")
                    client.publish(envelope["topic"], raw_env, qos=1)
                for news in engine.news:
                    say(f"sim {news}")
                engine.news = []
                outputs.after_step(engine)
                if engine.t.second == 0:
                    now_stamps = [p.stat().st_mtime_ns if p.exists() else 0 for p in watched]
                    if now_stamps != stamps:
                        stamps = now_stamps
                        engine.set_roster(read_people(site_dir))
                        engine.set_maintenance(read_maintenance(site_dir))
                        say(f"sim {engine.t:%H:%M:%S}: roster or PM schedule changed; reloaded")
                due = wall0 + (engine.t - site0).total_seconds() / speed - time.monotonic()
                if due > 0:
                    sleep(due)
    finally:
        recorded.close()
        client.loop_stop()
        client.disconnect()
    return outputs.close(engine, seed, days)

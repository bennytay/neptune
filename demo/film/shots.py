"""The shot list, found from what happened in the run (the timeline's events), so any seed films.

Each shot names a stretch of site time, how fast it plays, and a camera move (eye and target
keyframes in hall metres). ``auto`` builds the story of one day: the hall at work, the line, the
first jam and the starved arm, the jam being cleared, a protective stop and its reset, a robot
stopping on the lane and its rescue, and the whole shift as a time-lapse. A shot whose event did not
happen is left out. ``load`` reads a hand-written list instead (``--shots my_shots.json``).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from motion import Shot, Timeline

if TYPE_CHECKING:
    from pathlib import Path


def _first(timeline: Timeline, text: str, after: datetime | None = None) -> datetime | None:
    return next(
        (t for t, e in timeline.events if text in e and (after is None or t >= after)), None
    )


def _amr_at(timeline: Timeline, t: datetime, robot: str) -> tuple[float, float]:
    frame, _, _ = timeline.at(t)
    amr = next(a for a in frame["amrs"] if a["id"] == robot)
    return amr["x"], amr["y"]


def auto(timeline: Timeline, scene: dict[str, Any]) -> list[Shot]:
    first = timeline.first
    assert first is not None
    day = first.replace(hour=0, minute=0, second=0)
    shift = (day.replace(hour=6), day.replace(hour=14))
    px, py = scene["conveyor"]["pe3"]
    bx, by = scene["arm"]["base"]
    gx, gy = scene["work_at"]["reset_arm"]
    belt_x0 = scene["conveyor"]["start"][0]
    s = timedelta(seconds=1)

    jam = _first(timeline, "jam at PE3")
    calm = (jam - 600 * s) if jam else shift[0] + 3600 * s
    shots = [
        Shot(
            "01_hall",
            calm,
            9,
            6,
            eye=[(6, -8, 14), (20, -10, 15), (34, -8, 14)],
            target=[(16, 12, 0), (20, 12, 0), (24, 12, 0)],
            focal=24,
            note="the hall at work",
        ),
        Shot(
            "02_line",
            calm + 120 * s,
            7,
            1,
            eye=[(belt_x0 + 2, 11.6, 1.5), (belt_x0 + 9, 11.9, 1.6)],
            target=[(belt_x0 + 6, 14, 0.9), (px + 2, 14, 1.0)],
            focal=32,
            note="parts on CV-2",
        ),
        Shot(
            "03_arm",
            calm + 200 * s,
            7,
            1,
            eye=[(bx + 6.5, by - 8.0, 3.0), (bx + 5.0, by - 7.6, 2.6)],
            target=[(bx + 0.2, by - 0.9, 1.0), (bx + 0.2, by - 1.1, 1.0)],
            focal=30,
            note="arm C3 loading an AMR's tote",
        ),
    ]
    if jam:
        cleared = _first(timeline, "jam cleared", jam)
        starved = _first(timeline, "starved", jam)
        shots.append(
            Shot(
                "04_jam",
                jam - 4 * s,
                8,
                1,
                eye=[(px - 1.6, py - 2.6, 1.7), (px - 1.1, py - 2.1, 1.5)],
                target=[(px, py, 0.9), (px + 0.2, py, 0.9)],
                focal=35,
                note="a part jams at PE3; the belt stops",
            )
        )
        if starved:
            shots.append(
                Shot(
                    "05_starved",
                    starved - 3 * s,
                    6,
                    1,
                    eye=[(bx + 3.5, by - 4.5, 2.6), (bx + 3.0, by - 4.0, 2.4)],
                    target=[(bx - 0.8, by + 0.6, 1.0), (bx - 0.8, by + 0.6, 1.0)],
                    focal=30,
                    note="arm C3 starved; the AMR waits",
                )
            )
        if cleared:
            span = (cleared - jam).total_seconds() + 60
            shots.append(
                Shot(
                    "06_clear",
                    jam + 5 * s,
                    12,
                    span / 12,
                    eye=[(px - 6, py - 9, 9), (px - 3, py - 9.5, 8)],
                    target=[(px - 2, py + 2, 0.5), (px, py + 1, 0.5)],
                    focal=24,
                    note="a technician walks over and clears the jam under lockout",
                )
            )
    pstop = _first(timeline, "protective stop")
    reset = _first(timeline, "reset by", pstop) if pstop else None
    if pstop and reset:
        span = (reset - pstop).total_seconds() + 30
        shots.append(
            Shot(
                "07_reset",
                pstop - 3 * s,
                9,
                max(1.0, span / 9),
                eye=[(gx + 4, gy - 4, 2.6), (gx + 3, gy - 3.4, 2.2)],
                target=[(gx - 1.5, gy, 1.2), (gx - 1.0, gy, 1.2)],
                focal=28,
                note="protective stop; the one qualified person resets the arm",
            )
        )
    flat = next(((t, e) for t, e in timeline.events if "battery flat" in e), None)
    if flat:
        t, text = flat
        robot = "robot:" + text.split()[0]
        x, y = _amr_at(timeline, t, robot)
        pushed = _first(timeline, "pushed to charge", t)
        shots.append(
            Shot(
                "08_stranded",
                t - 20 * s,
                9,
                4,
                eye=[(x - 7, y - 8, 6), (x - 5, y - 7, 5)],
                target=[(x, y, 0.3), (x, y, 0.3)],
                focal=28,
                note=f"{robot.split(':')[1].upper()} stops on the lane, battery flat;"
                " the robots behind it queue",
            )
        )
        if pushed:
            span = (pushed - t).total_seconds() + 180
            shots.append(
                Shot(
                    "09_rescue",
                    t + 10 * s,
                    10,
                    span / 10,
                    eye=[(-6, -7, 6), (-5, -6, 5)],
                    target=[(0, 0, 0.3), (0, 0, 0.3)],
                    focal=24,
                    follow=robot,
                    follow_eye=True,
                    note="pushed to a charger by hand",
                )
            )
    shots.append(
        Shot(
            "10_timelapse",
            shift[0] - 300 * s,
            20,
            (shift[1] - shift[0]).total_seconds() / 20 + 30,
            eye=[(20, -14, 30), (34, -6, 30)],
            target=[(20, 11, 0), (20, 11, 0)],
            focal=24,
            note="the whole shift",
        )
    )
    return shots


def load(path: Path) -> list[Shot]:
    """``[{"name", "start": "2026-10-20T10:57:00", "seconds", "speed", "eye": [[x,y,z],...],
    "target": [...], "focal"?, "follow"?, "follow_eye"?, "note"?}, ...]``"""
    out = []
    for item in json.loads(path.read_text(encoding="utf-8")):
        item = dict(item)
        item["start"] = datetime.fromisoformat(item["start"])
        item["eye"] = [tuple(v) for v in item["eye"]]
        item["target"] = [tuple(v) for v in item["target"]]
        out.append(Shot(**item))
    return out


def dump(shots: list[Shot], path: Path) -> None:
    """Write the list ``load`` reads: edit it to reframe a shot, then build with ``--shots``."""
    items = []
    for shot in shots:
        item = dict(shot.__dict__)
        item["start"] = shot.start.isoformat(timespec="seconds")
        items.append(item)
    path.write_text(json.dumps(items, indent=1) + "\n", encoding="utf-8")

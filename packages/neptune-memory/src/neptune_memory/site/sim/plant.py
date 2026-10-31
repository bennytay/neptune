"""Plant 7 as the simulator builds it: the floor plan in metres, and the truth Neptune never sees.

The floor plan is what a renderer needs (``scene()``): zones and areas as rectangles named with the
layout's ids, the conveyor's line, the arm's base and poses, the AMR lane graph and the people's
places. x runs east, y north, z up; the hall is 40 x 24 m.

``Truth`` holds why things go wrong. None of it is in a file Neptune reads: Neptune has to find it
from the evidence the plant leaves (feeds, work orders, the controller log).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

HALL = (40.0, 24.0)

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1

AREAS: dict[str, Rect] = {
    "area:inbound": (0.0, 0.0, 12.0, 24.0),
    "area:assembly": (12.0, 0.0, 40.0, 24.0),
}
ZONES: dict[str, Rect] = {
    "zone:charging": (0.0, 0.0, 11.5, 6.0),
    "zone:aisle-b": (12.0, 0.0, 40.0, 4.0),
    "zone:aisle-a": (12.0, 7.0, 40.0, 11.0),
    "zone:c3-fence": (19.0, 11.0, 25.0, 17.0),
}
CELLS: dict[str, Rect] = {"cell:c3": (19.0, 11.0, 25.0, 17.0)}

# Conveyor CV-2 runs east along y = 14 from the inbound dock to the arm's pick position.
BELT_START = (4.0, 14.0)
BELT_LENGTH = 16.5
BELT_HEIGHT = 0.8
PE3_AT = 13.5  # metres along the belt: photo-eye 3, where parts jam
PART_GAP = 0.7  # minimum spacing of parts queued at the belt's end
PART_SPACING = 32.0  # infeed: one part per PART_SPACING / speed seconds


def belt_xy(s: float) -> tuple[float, float]:
    return BELT_START[0] + s, BELT_START[1]


# Arm C3: a large industrial arm on the fence's south side, reaching the belt's end and the tote on
# the AMR docked at the pickup station just outside the fence.
ARM_BASE = (21.5, 12.4)
PICK = (BELT_START[0] + BELT_LENGTH, BELT_START[1], BELT_HEIGHT + 0.1)
PLACE = (22.5, 10.2, 0.75)

# AMR lanes: a one-way loop (east along aisle A, west along aisle B) with bypass stations, so a
# robot loading, unloading, charging or parked never blocks the lane. A robot stopped on the lane
# does.
NODES: dict[str, tuple[float, float]] = {
    "A0": (14.0, 8.5),
    "A1": (18.0, 8.5),
    "STAGE": (19.5, 10.2),
    "A2": (21.0, 8.5),
    "PICKUP": (22.5, 10.2),
    "A3": (24.0, 8.5),
    "A4": (30.0, 8.5),
    "PARK_A": (33.0, 10.2),
    "A5": (36.0, 8.5),
    "A6": (38.0, 8.5),
    "B6": (38.0, 2.0),
    "PARK_B": (35.0, 0.8),
    "B5": (33.0, 2.0),
    "DROP": (29.0, 0.8),
    "B4": (27.0, 2.0),
    "B3": (22.0, 2.0),
    "B0": (14.0, 2.0),
    "CH_IN": (11.0, 2.0),
    "CHARGER_1": (7.0, 3.2),
    "CHARGER_2": (4.0, 3.2),
    "CH_OUT": (11.0, 5.0),
}
EDGES: dict[str, tuple[str, ...]] = {
    "A0": ("A1",),
    "A1": ("STAGE", "A2"),
    "STAGE": ("A2",),
    "A2": ("PICKUP", "A3"),
    "PICKUP": ("A3",),
    "A3": ("A4",),
    "A4": ("PARK_A", "A5"),
    "PARK_A": ("A5",),
    "A5": ("A6",),
    "A6": ("B6",),
    "B6": ("PARK_B", "B5"),
    "PARK_B": ("B5",),
    "B5": ("DROP", "B4"),
    "DROP": ("B4",),
    "B4": ("B3",),
    "B3": ("B0",),
    "B0": ("A0", "CH_IN"),
    "CH_IN": ("CHARGER_1", "CHARGER_2"),
    "CHARGER_1": ("CH_OUT",),
    "CHARGER_2": ("CH_OUT",),
    "CH_OUT": ("A0",),
}
CHARGERS = ("CHARGER_1", "CHARGER_2")
PARKING = {"zone:aisle-a": "PARK_A", "zone:aisle-b": "PARK_B"}
AMR_SPEED = 1.2  # m/s
AMR_MANUAL_SPEED = 0.6  # pushed by a person
TOTE_SIZE = 6

# People: where they stand when not on a job.
PLACES: dict[str, tuple[float, float]] = {
    "shop": (9.0, 20.0),
    "break_room": (3.0, 20.0),
    "office": (31.0, 17.5),
    "entrance": (1.0, 23.0),
}
WORK_AT: dict[str, tuple[float, float]] = {
    "clear_jam": (BELT_START[0] + PE3_AT, 13.2),
    "clean_pe3": (BELT_START[0] + PE3_AT, 13.2),
    "reset_arm": (25.6, 14.0),  # the fence gate's reset panel
    "watch_jam": (BELT_START[0] + PE3_AT + 1.5, 12.4),
}
WALK_SPEED = 1.3

# Joint angles (radians) of a six-axis arm at its waypoints; joint 1 turns to face the target.
_REACH = (-0.6, 1.3, -2.2, -1.57, 0.0)
_RAISED = (-1.2, 1.4, -1.8, -1.57, 0.0)


def _facing(x: float, y: float) -> float:
    return math.atan2(y - ARM_BASE[1], x - ARM_BASE[0])


POSES: dict[str, tuple[float, ...]] = {
    "home": (_facing(21.5, 10.0), -1.57, 1.57, -1.57, -1.57, 0.0),
    "pick": (_facing(PICK[0], PICK[1]), *_REACH),
    "above_pick": (_facing(PICK[0], PICK[1]), *_RAISED),
    "place": (_facing(PLACE[0], PLACE[1]), *_REACH),
    "above_place": (_facing(PLACE[0], PLACE[1]), *_RAISED),
}


@dataclass(frozen=True)
class Truth:
    """What actually drives the plant. Neptune must infer this; it is never written to the site."""

    jam_base: float = 4e-4  # chance a part jams passing PE3, at 0.80 m/s with a clean lens
    jam_speed_k: float = 10.7  # x e^(k (speed - 0.80)): 0.95 m/s jams about five times as often
    dirt_per_hour: float = 0.15  # the PE3 lens fouls while the belt runs; x (1 + dirt)
    dirt_max: float = 1.5
    clear_jam_s: tuple[float, float] = (1500.0, 2100.0)  # 25-35 min under lockout
    pstop_on_restart: float = 0.6  # part presence lost at pick as the belt restarts
    reset_arm_s: float = 180.0
    clean_pe3_s: float = 360.0
    rescue_s: float = 360.0
    drain: dict[str, float] | None = None  # per robot; amr-03's battery has aged
    gauge: dict[str, tuple[float, float]] | None = None  # per robot: reads offset + scale x true

    def drain_of(self, robot: str) -> float:
        return (self.drain or {"amr-03": 1.3}).get(robot, 1.0)

    def reads(self, robot: str, true: float) -> float:
        """The charge a robot reports: amr-03's aged pack reads about 22 % when it is empty."""
        offset, scale = (self.gauge or {"amr-03": (18.0, 0.82)}).get(robot, (0.0, 1.0))
        return min(100.0, offset + scale * true)


@dataclass
class Plan:
    """How the site is run today: the fleet manager's and the line's settings.

    These start as the site has them; commands and the files Neptune can change move them."""

    charge_below: float = 20.0  # % at which the fleet manager sends a robot to charge
    charge_until: float = 95.0
    top_up_below: float | None = None  # idle robots charge one at a time below this (stagger)
    prestage: bool = False  # send the next robot to wait beside the pickup before the tote is full
    prestage_at: int = 3  # parts in the docked robot's tote when the next one is sent


STOP_BATTERY = 5.0  # true charge at which a robot stops where it is


def zone_of(x: float, y: float) -> str | None:
    for zone, (x0, y0, x1, y1) in ZONES.items():
        if x0 <= x <= x1 and y0 <= y <= y1:
            return zone
    return None


def scene() -> dict[str, Any]:
    """The static scene for a renderer (``sim/scene.json``). Truth is not in it."""
    return {
        "units": "m",
        "hall": {"width": HALL[0], "depth": HALL[1]},
        "areas": {k: list(v) for k, v in AREAS.items()},
        "zones": {k: list(v) for k, v in ZONES.items()},
        "cells": {k: list(v) for k, v in CELLS.items()},
        "conveyor": {
            "id": "machine:cv-2",
            "start": list(BELT_START),
            "end": list(belt_xy(BELT_LENGTH)),
            "height": BELT_HEIGHT,
            "pe3": list(belt_xy(PE3_AT)),
        },
        "arm": {
            "id": "robot:arm-c3",
            "base": list(ARM_BASE),
            "pick": list(PICK),
            "place": list(PLACE),
            "poses": {k: list(v) for k, v in POSES.items()},
        },
        "lanes": {
            "nodes": {k: list(v) for k, v in NODES.items()},
            "edges": {k: list(v) for k, v in EDGES.items()},
            "chargers": list(CHARGERS),
            "pickup": "PICKUP",
            "stage": "STAGE",
            "drop": "DROP",
            "parking": dict(PARKING),
        },
        "places": {k: list(v) for k, v in PLACES.items()},
        "work_at": {k: list(v) for k, v in WORK_AT.items()},
        "plan": asdict(Plan()),
    }

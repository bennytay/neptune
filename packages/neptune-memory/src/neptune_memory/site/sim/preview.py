"""A top-down replay of the simulated plant: one self-contained HTML page (spec §17).

``preview(site_dir, start, end, every)`` reads ``sim/scene.json`` and ``sim/timeline.jsonl`` and
writes ``sim/preview.html``: the floor plan drawn on a canvas, the frames between ``start`` and
``end`` (one per ``every`` s) embedded, and a player that interpolates between them. It is a check
of what the plant does before the photoreal render, not part of Neptune.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PAGE = Path(__file__).with_name("preview.html")


def _slim(frame: dict[str, Any]) -> dict[str, Any]:
    c, arm = frame["conveyor"], frame["arm"]
    return {
        "t": frame["t"],
        "c": [c["state"], c["alarm"], c["setpoint"], c["travel"], c["lens_dirt"]],
        "p": [[p["s"], int(p["jammed"])] for p in c["parts"]],
        "a": [arm["mode"], arm["phase"], int(arm["holding"]), arm["tcp"]],
        "r": [
            [
                a["x"],
                a["y"],
                a["yaw"],
                a["mode"],
                a["battery"],
                a["tote"],
                a["task"] or "",
                a["order_state"],
                int(a["stranded"]),
                int(a["charging"]),
            ]
            for a in frame["amrs"]
        ],
        "h": [[p["x"], p["y"], p["activity"], int(p["present"])] for p in frame["people"]],
        "e": frame["events"],
    }


def preview(site_dir: Path, start: str, end: str, every: int = 2) -> Path:
    sim = Path(site_dir) / "sim"
    scene = json.loads((sim / "scene.json").read_text(encoding="utf-8"))
    frames: list[dict[str, Any]] = []
    events: list[str] = []
    ids: list[str] = []
    people: list[list[str]] = []
    with (sim / "timeline.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            t = line[6:25]
            if t < start:
                continue
            if t > end:
                break
            frame = json.loads(line)
            events += frame["events"]
            if not ids:
                ids = [a["id"] for a in frame["amrs"]]
                people = [[p["id"], p["role"]] for p in frame["people"]]
            if int(t[-2:]) % every == 0:
                slim = _slim(frame)
                slim["e"], events = events, []
                frames.append(slim)
    data = {"scene": scene, "frames": frames, "amrs": ids, "people": people, "every": every}
    out = sim / "preview.html"
    page = PAGE.read_text(encoding="utf-8")
    out.write_text(page.replace("/*DATA*/null", json.dumps(data, separators=(",", ":"))), "utf-8")
    return out

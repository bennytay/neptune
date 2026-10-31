"""Build the film's USD stages from a simulator run: ``plant.usda`` plus one file per shot.

    python demo/film/build_stage.py site-sim --out film

Run it with Isaac Sim's Python (``./python.sh``), or any Python with ``pip install usd-core``. It
reads ``<site>/sim/scene.json`` and ``<site>/sim/timeline.jsonl`` (``make site-sim-run`` writes
them) and writes, under ``--out``:

- ``plant.usda``: the hall and everything in it, at rest;
- ``shots/<NN_name>.usdc``: each shot's motion and camera, frame by frame at ``--fps``;
- ``shots.json``: the shot list used. Edit it and pass ``--shots film/shots.json`` to reframe.

Nothing here needs a GPU; ``render_isaac.py`` renders the shots.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import factory
import shots as shot_list
from motion import Timeline, write_shot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("site_dir", type=Path, help="a simulated site, e.g. site-sim")
    parser.add_argument("--out", type=Path, default=Path("film"))
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--day", help="which simulated day, e.g. 2026-10-21 (default: the first)")
    parser.add_argument(
        "--shots", type=Path, help="a shot list to use instead of the automatic one"
    )
    parser.add_argument("--only", nargs="*", help="build only these shots (names or prefixes)")
    parser.add_argument("--light-scale", type=float, default=1.0, help="scale every light")
    args = parser.parse_args()

    sim = args.site_dir / "sim"
    scene = json.loads((sim / "scene.json").read_text(encoding="utf-8"))
    timeline = Timeline.load(sim / "timeline.jsonl", args.day)
    if args.day is None:
        assert timeline.first is not None
        day = timeline.first.date().isoformat()
        timeline = Timeline.load(sim / "timeline.jsonl", day)
    first = next(iter(timeline.frames.values()))
    amrs = [a["id"] for a in first["amrs"]]
    people = [(p["id"], p["role"]) for p in first["people"]]

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "shots").mkdir(exist_ok=True)
    plant = args.out / "plant.usda"
    factory.build(str(plant), scene, amrs, people, args.light_scale)
    sys.stdout.write(f"wrote {plant}\n")

    chosen = shot_list.load(args.shots) if args.shots else shot_list.auto(timeline, scene)
    if args.only:
        chosen = [s for s in chosen if any(s.name.startswith(p) for p in args.only)]
    shot_list.dump(chosen, args.out / "shots.json")
    for shot in chosen:
        path, worst = write_shot(
            args.out / "shots" / f"{shot.name}.usdc",
            "../plant.usda",
            shot,
            timeline,
            scene,
            args.fps,
        )
        frames = shot.frames(args.fps)
        sys.stdout.write(
            f"wrote {path}: {frames} frames, site {shot.start:%H:%M:%S} at {shot.speed:g}x"
            f" ({shot.note}); arm reach error {worst * 1000:.1f} mm\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

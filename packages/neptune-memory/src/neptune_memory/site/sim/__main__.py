"""``python -m neptune_memory.site.sim <command>``: prepare, run offline, or run live (spec §17)."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from neptune_memory.site.sim import run

if TYPE_CHECKING:
    from collections.abc import Sequence


def _say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m neptune_memory.site.sim")
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="write Plant 7's static corpus for the simulated days")
    offline = sub.add_parser("run", help="prepare, then simulate offline into feed/")
    online = sub.add_parser("live", help="simulate live over MQTT (run prepare and live first)")
    for p in (prep, offline, online):
        p.add_argument("site_dir", type=Path)
        p.add_argument("--days", type=int, default=1)
    for p in (offline, online):
        p.add_argument("--seed", type=int, default=7)
        p.add_argument(
            "--frames", type=int, default=1, help="site seconds per timeline frame; 0: none"
        )
    offline.add_argument("--commands", type=Path, help="a live run's sim/commands.jsonl to replay")
    offline.add_argument("--keep-static", action="store_true", help="don't re-prepare static/")
    online.add_argument("--broker", default="mqtt://127.0.0.1:1883")
    online.add_argument("--speed", type=float, default=60.0)
    args = parser.parse_args(argv)

    if args.command == "prepare":
        run.prepare(args.site_dir, args.days)
        sys.stdout.write(f"prepared Plant 7 for {args.days} simulated day(s) in {args.site_dir}\n")
        return 0
    if args.command == "run":
        commands = args.commands
        if commands is not None and not args.keep_static:
            kept = commands.read_bytes()  # prepare clears sim/, where a recording usually lives
            run.prepare(args.site_dir, args.days)
            commands = args.site_dir / run.SIM_DIR / "replayed-commands.jsonl"
            commands.parent.mkdir(parents=True, exist_ok=True)
            commands.write_bytes(kept)
        elif not args.keep_static:
            run.prepare(args.site_dir, args.days)
        summary = run.run(
            args.site_dir, days=args.days, seed=args.seed, commands=commands, frames=args.frames
        )
    else:
        with contextlib.suppress(KeyboardInterrupt):
            summary = run.run_live(
                args.site_dir,
                args.broker,
                speed=args.speed,
                days=args.days,
                seed=args.seed,
                frames=args.frames,
                say=_say,
            )
            sys.stdout.write(json.dumps(summary["kpis"], indent=1) + "\n")
        return 0
    sys.stdout.write(json.dumps(summary["kpis"], indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

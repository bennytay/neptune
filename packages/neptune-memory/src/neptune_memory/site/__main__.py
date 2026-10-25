"""``python -m neptune_memory.site <command>``: the site model's commands (spec §2).

Each lane adds its own subcommand; don't reorder the others.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


def _generate(args: argparse.Namespace) -> int:
    from neptune_memory.site import corpus

    corpus.generate(Path(args.out))
    sys.stdout.write(f"generated the Plant 7 corpus in {args.out}\n")
    return 0


def _build(args: argparse.Namespace) -> int:
    from neptune_memory.site.build import build

    build(args.site_dir, until=args.until)
    return 0


def _replay(args: argparse.Namespace) -> int:
    from neptune_memory.site.build import replay

    replay(args.site_dir, speed=args.speed, until=args.until)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m neptune_memory.site")
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="write the deterministic Plant 7 corpus")
    generate.add_argument("out", help="output directory (static/ and feed/ go under it)")
    generate.set_defaults(run=_generate)

    build = commands.add_parser(
        "build", help="write the site's fact log: static files, then the feed"
    )
    build.add_argument("site_dir", type=Path)
    build.add_argument("--until", help="stop the feed after this site time (YYYY-MM-DDTHH:MM:SS)")
    build.set_defaults(run=_build)

    replay = commands.add_parser(
        "replay", help="build, live: feed messages arrive at SPEED x site time"
    )
    replay.add_argument("site_dir", type=Path)
    replay.add_argument("--speed", type=float, default=60.0)
    replay.add_argument("--until", help="stop the feed after this site time (YYYY-MM-DDTHH:MM:SS)")
    replay.set_defaults(run=_replay)

    args = parser.parse_args(argv)
    code: int = args.run(args)
    return code


if __name__ == "__main__":
    sys.exit(main())

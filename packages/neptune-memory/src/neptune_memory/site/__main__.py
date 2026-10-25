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


def _build(args: argparse.Namespace) -> int:
    from neptune_memory.site.build import build

    build(args.site_dir, until=args.until)
    return 0


def _replay(args: argparse.Namespace) -> int:
    from neptune_memory.site.build import replay

    replay(args.site_dir, speed=args.speed, until=args.until)
    return 0


def _say(line: str) -> None:
    sys.stdout.write(line + "\n")


def _confirm(args: argparse.Namespace) -> int:
    from neptune_memory.site import confirm as queue
    from neptune_memory.site.identity import propose

    propose(args.site_dir)
    if args.yes:
        entry = queue.confirm(args.site_dir, args.yes, "yes", by=args.by)
        _say(f"{args.yes}: yes (log seq {entry.seq})")
    if args.no:
        entry = queue.confirm(args.site_dir, args.no, "no", by=args.by)
        _say(f"{args.no}: no (log seq {entry.seq})")
    pending = queue.pending(args.site_dir)
    _say(f"{len(pending)} pending")
    for fact in pending:
        other = getattr(fact.object, "ref", fact.object)
        _say(f"  {fact.id}  {fact.subject} same_as {other}")
        for cite in fact.citations:
            _say(f"    {cite.file} {cite.locator}: {cite.quote}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m neptune_memory.site")
    commands = parser.add_subparsers(dest="command", required=True)

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

    confirm = commands.add_parser(
        "confirm", help="list pending identity proposals; --yes ID / --no ID answers one"
    )
    confirm.add_argument("site_dir", type=Path)
    confirm.add_argument("--yes", metavar="ID", help="proposal id to confirm")
    confirm.add_argument("--no", metavar="ID", help="proposal id to reject")
    confirm.add_argument("--by", default="operator")
    confirm.set_defaults(run=_confirm)

    args = parser.parse_args(argv)
    code: int = args.run(args)
    return code


if __name__ == "__main__":
    sys.exit(main())

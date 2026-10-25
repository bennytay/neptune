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
    from neptune_memory.site.log import last_seq

    log = build(args.site_dir, until=args.until)
    sys.stdout.write(f"built {log} ({last_seq(args.site_dir)} entries)\n")
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
    from neptune_memory.site.log import SEGMENT_SIZE, segment_path

    propose(args.site_dir)
    pending = queue.pending(args.site_dir)
    _say(f"{len(pending)} pending")
    for fact in pending:
        other = getattr(fact.object, "ref", fact.object)
        _say(f"  {fact.id}  {fact.subject} same_as {other} · {fact.kind}")
        for cite in fact.citations:
            _say(f"    {cite.file} {cite.locator}: {cite.quote}")
    answers = [(pid, "yes") for pid in args.yes or []] + [(pid, "no") for pid in args.no or []]
    if args.yes_all:
        answers += [(fact.id, "yes") for fact in pending]
    for pid, answer in answers:
        entry = queue.confirm(args.site_dir, pid, answer, by=args.by)  # type: ignore[arg-type]
        where = segment_path(Path(".neptune/log"), entry.seq).as_posix()
        line = (entry.seq - 1) % SEGMENT_SIZE + 1
        _say(f"{pid}: {answer} by {args.by} (log seq {entry.seq}, {where} confirm line {line})")
    if answers:
        _say(f"{len(queue.pending(args.site_dir))} pending")
    return 0


def _draft_mapping(args: argparse.Namespace) -> int:
    from neptune_memory.site.draft import draft_mapping

    sys.stdout.write(draft_mapping(args.site_dir, args.file))
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

    confirm = commands.add_parser(
        "confirm", help="list pending identity proposals; --yes ID / --no ID answers one"
    )
    confirm.add_argument("site_dir", type=Path)
    confirm.add_argument("--yes", metavar="ID", action="append", help="proposal id to confirm")
    confirm.add_argument("--no", metavar="ID", action="append", help="proposal id to reject")
    confirm.add_argument("--yes-all", action="store_true", help="confirm every pending proposal")
    confirm.add_argument("--by", default="operator")
    confirm.set_defaults(run=_confirm)

    draft = commands.add_parser(
        "draft-mapping", help="print a draft mapping entry for an unmapped CSV or JSONL file"
    )
    draft.add_argument("site_dir", type=Path)
    draft.add_argument("file", type=Path, help="the file, relative to site_dir or absolute")
    draft.set_defaults(run=_draft_mapping)

    args = parser.parse_args(argv)
    code: int = args.run(args)
    return code


if __name__ == "__main__":
    sys.exit(main())

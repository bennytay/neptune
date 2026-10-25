"""``python -m neptune_memory.site <command>``: the site model's commands (spec §2).

Each lane adds its own subcommand; don't reorder the others.
"""

from __future__ import annotations

import argparse
import contextlib
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


def _generate(args: argparse.Namespace) -> int:
    from neptune_memory.site import corpus

    corpus.generate(Path(args.out), site=args.site)
    sys.stdout.write(f"generated the {corpus.SITES[args.site]} corpus in {args.out}\n")
    return 0


def _extract_photos(args: argparse.Namespace) -> int:
    import os

    from neptune_memory.site.derived.photos import extract_photos

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.stdout.write("ANTHROPIC_API_KEY is not set: keeping the recorded extractions\n")
        return 0
    try:
        written = extract_photos(args.site_dir)
    except ImportError:
        sys.stdout.write("the anthropic SDK is missing: uv sync --extra anthropic\n")
        return 1
    for path in written:
        sys.stdout.write(f"recorded {path}\n")
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
    sys.stdout.flush()


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


DEFAULT_BROKER = "mqtt://127.0.0.1:1883"  # live.DEFAULT_BROKER, without importing the compiler


def _publish(args: argparse.Namespace) -> int:
    from neptune_memory.site.live import publish

    with contextlib.suppress(KeyboardInterrupt):
        n = publish(
            args.site_dir, args.broker, speed=args.speed, start=args.start, until=args.until
        )
        _say(f"published {n} messages to {args.broker}")
    return 0


def _interruptible() -> None:
    """Ctrl-C stops a command even when a shell started it with ``&`` (which ignores SIGINT)."""
    signal.signal(signal.SIGINT, signal.default_int_handler)


def _live(args: argparse.Namespace) -> int:
    from neptune_memory.site.live import live

    _interruptible()
    live(args.site_dir, args.broker)
    return 0


def _broker(args: argparse.Namespace) -> int:
    from neptune_memory.site.live import broker

    _interruptible()
    _say(f"broker: {args.broker} (amqtt, anonymous; Ctrl-C stops it)")
    with contextlib.suppress(KeyboardInterrupt):
        broker(args.broker)
    return 0


def _insights(args: argparse.Namespace) -> int:
    from neptune_memory.site.insights import append, insights
    from neptune_memory.site.render import render
    from neptune_memory.site.views import load

    new = append(args.site_dir)
    _say(f"{len(new)} new findings appended to the log")
    sys.stdout.write(render(insights(load(args.site_dir))))
    return 0


def _web(args: argparse.Namespace) -> int:
    from neptune_memory.site.web import serve

    _interruptible()
    server = serve(args.site_dir, args.host, args.port)
    url = f"http://{args.host}:{server.server_port}/"
    _say(f"web: {url} (the site model of {args.site_dir}; Ctrl-C stops it)")
    if args.open:
        import webbrowser

        webbrowser.open(url)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    server.server_close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m neptune_memory.site")
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="write a deterministic demo corpus")
    generate.add_argument("--site", choices=("p7", "harbor"), default="p7")
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

    publish = commands.add_parser(
        "publish", help="replay the corpus feed to an MQTT broker on its real topics"
    )
    publish.add_argument("site_dir", type=Path)
    publish.add_argument("--broker", default=DEFAULT_BROKER)
    publish.add_argument("--speed", type=float, default=60.0)
    publish.add_argument("--start", help="publish messages before this site time at once")
    publish.add_argument("--until", help="stop after this site time (YYYY-MM-DDTHH:MM:SS)")
    publish.set_defaults(run=_publish)

    live = commands.add_parser(
        "live", help="subscribe; record and log each message, and new static files, as they land"
    )
    live.add_argument("site_dir", type=Path)
    live.add_argument("--broker", default=DEFAULT_BROKER)
    live.set_defaults(run=_live)

    broker = commands.add_parser("broker", help="run a local MQTT broker (amqtt) for the demo")
    broker.add_argument("--broker", default=DEFAULT_BROKER, help="where to listen")
    broker.set_defaults(run=_broker)

    insights = commands.add_parser(
        "insights", help="append the deterministic findings (inferred) to the log; idempotent"
    )
    insights.add_argument("site_dir", type=Path)
    insights.set_defaults(run=_insights)

    photos = commands.add_parser(
        "extract-photos", help="send each photo to Claude and record its .extraction.json"
    )
    photos.add_argument("site_dir", type=Path)
    photos.set_defaults(run=_extract_photos)

    web = commands.add_parser(
        "web", help="serve the live web view: map, states, timeline, findings, sources"
    )
    web.add_argument("site_dir", type=Path)
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--open", action="store_true", help="open it in the default browser")
    web.set_defaults(run=_web)

    args = parser.parse_args(argv)
    code: int = args.run(args)
    return code


if __name__ == "__main__":
    sys.exit(main())

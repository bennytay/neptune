"""``python -m neptune_memory.site <command>``: the site model's command line (spec §2)."""

import argparse
import sys
from pathlib import Path

from neptune_memory.site import corpus


def _generate(args: argparse.Namespace) -> int:
    corpus.generate(Path(args.out))
    sys.stdout.write(f"generated the Plant 7 corpus in {args.out}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m neptune_memory.site")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="write the deterministic Plant 7 corpus")
    generate.add_argument("out", help="output directory (static/ and feed/ go under it)")
    generate.set_defaults(run=_generate)
    args = parser.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    sys.exit(main())

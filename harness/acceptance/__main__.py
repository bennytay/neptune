"""``python -m harness.acceptance``: build, lock, check or resolve the acceptance corpus.

build DIR             write the corpus under DIR (replacing it)
lock                  rewrite corpus.lock.json from a fresh build (after bumping VERSION)
check                 exit 1 unless a fresh build matches the lock and every file is small
resolve PACKAGE       print the gold evidence resolved against a compiled package, as JSON
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from harness import acceptance
from harness.acceptance import resolve

if TYPE_CHECKING:
    from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness.acceptance", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build").add_argument("dir", type=Path)
    commands.add_parser("lock")
    commands.add_parser("check")
    commands.add_parser("resolve").add_argument("package", type=Path)
    args = parser.parse_args(argv)
    if args.command == "build":
        acceptance.materialise(args.dir)
        sys.stdout.write(f"wrote {acceptance.NAME} {acceptance.VERSION} to {args.dir}\n")
    elif args.command == "lock":
        acceptance.LOCK.write_text(acceptance.render_lock(acceptance.build()), encoding="utf-8")
        sys.stdout.write(f"locked {acceptance.label()}\n")
    elif args.command == "check":
        found = acceptance.lock_problems(acceptance.build())
        for problem in found:
            sys.stderr.write(f"harness.acceptance: {problem}\n")
        if found:
            return 1
        sys.stdout.write(f"{acceptance.label()} matches its lock\n")
    else:
        gold = json.loads(acceptance.GOLD.read_text(encoding="utf-8"))
        resolved = resolve.resolve(args.package, gold)
        sys.stdout.write(json.dumps(resolved, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""``python -m neptune_deploy map <package> --mapping <file>... --out <dir>`` (ADR 0002 §8)."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from neptune.store.package import PackageError
from neptune_deploy.lifecycle import PRESETS, MappingError, load_mapping, map_package, preset


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m neptune_deploy",
        description="Neptune Deploy: deployment lifecycle records from compiler packages.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    mapper = commands.add_parser(
        "map",
        help="map a package's tables into lifecycle records, in a new package",
        description=(
            "Read a compiler package, apply declared mapping files to its tables, and write a new"
            " package of lifecycle records citing the source cells. The base package is not"
            " changed; what does not map is a finding in the new package's receipt."
        ),
    )
    mapper.add_argument("package", type=Path, help="the compiler package to map")
    mapper.add_argument(
        "-m",
        "--mapping",
        type=Path,
        action="append",
        default=[],
        help="a mapping file (repeatable)",
    )
    mapper.add_argument(
        "-p",
        "--preset",
        action="append",
        default=[],
        choices=PRESETS,
        help="a shipped mapping file by name (repeatable)",
    )
    mapper.add_argument("-o", "--out", type=Path, required=True, help="where to write the package")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        mappings = [load_mapping(path) for path in args.mapping]
        mappings += [preset(name) for name in args.preset]
        if not mappings:
            raise MappingError("name at least one --mapping or --preset")
        package = map_package(args.package, mappings, args.out)
    except (MappingError, PackageError, OSError) as exc:
        sys.stderr.write(f"neptune-deploy map: {exc}\n")
        return 2 if isinstance(exc, MappingError) else 1
    sys.stdout.write(f"wrote {args.out}\n  package  {package}\n")
    return 0

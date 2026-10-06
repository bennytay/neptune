"""``python -m neptune_deploy map <package> --mapping <file>... --out <dir>`` (ADR 0002 §8), and
``python -m neptune_deploy pack ...`` (``packs.cli``, ADR 0013 §10)."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from neptune.store.package import PackageError
from neptune_deploy.lifecycle import (
    PRESETS,
    TEMPLATE_PRESETS,
    MappingError,
    TemplateRegistry,
    load_mapping,
    map_package,
    preset,
    template_preset,
)
from neptune_deploy.packs import cli as pack_cli


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m neptune_deploy",
        description=(
            "Neptune Deploy: deployment lifecycle records from compiler packages, and evidence"
            " packs from Memory snapshots."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    mapper = commands.add_parser(
        "map",
        help="map a package's tables and documents into lifecycle records, in a new package",
        description=(
            "Read a compiler package, apply declared mapping files to its tables and declared"
            " document templates to its documents, and write a new package of lifecycle records"
            " citing the source cells and spans. The base package is not changed; what does not"
            " map is a finding in the new package's receipt."
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
    mapper.add_argument(
        "-t",
        "--template",
        type=Path,
        action="append",
        default=[],
        help="a document template file, or a directory of them (repeatable)",
    )
    mapper.add_argument(
        "-T",
        "--template-preset",
        action="append",
        default=[],
        choices=TEMPLATE_PRESETS,
        help="a shipped document template by name (repeatable)",
    )
    mapper.add_argument("-o", "--out", type=Path, required=True, help="where to write the package")
    pack_cli.add_parser(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "pack":
        return pack_cli.run(args)
    try:
        mappings = [load_mapping(path) for path in args.mapping]
        mappings += [preset(name) for name in args.preset]
        registry = TemplateRegistry.from_paths(args.template)
        for name in args.template_preset:
            registry.add(template_preset(name))
        templates = registry.templates()
        if not mappings and not templates:
            raise MappingError("name at least one --mapping, --preset or --template")
        package = map_package(args.package, mappings, args.out, templates)
    except (MappingError, PackageError, OSError) as exc:
        sys.stderr.write(f"neptune-deploy map: {exc}\n")
        return 2 if isinstance(exc, MappingError) else 1
    sys.stdout.write(f"wrote {args.out}\n  package  {package}\n")
    return 0

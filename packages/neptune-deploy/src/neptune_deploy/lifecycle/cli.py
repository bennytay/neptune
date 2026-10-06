"""``python -m neptune_deploy map <package> --mapping <file>... --out <dir>`` (ADR 0002 §8), and
``python -m neptune_deploy pack ...`` (``packs.cli``, ADR 0013 §10)."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from neptune.store.package import PackageError
from neptune_deploy import eventlogs
from neptune_deploy.lifecycle import (
    PRESETS,
    TEMPLATE_PRESETS,
    MappingError,
    TemplateRegistry,
    load_mapping,
    map_package,
    presets,
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
        choices=sorted({*PRESETS, *eventlogs.PRESETS}),
        help="a shipped mapping file by name, lifecycle or event log (repeatable)",
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
    mapper.add_argument(
        "--source-zone",
        nargs=3,
        action="append",
        default=[],
        metavar=("PRESET", "SOURCE", "ZONE"),
        help=(
            "the civil zone (an IANA name, or 'unstated') a site declares for one source a preset"
            " maps, by the source's path in the package (repeatable; ADR 0017)"
        ),
    )
    mapper.add_argument("-o", "--out", type=Path, required=True, help="where to write the package")
    pack_cli.add_parser(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "pack":
        return pack_cli.run(args)
    try:
        zones: dict[tuple[str, str], str] = {}
        for name, source, zone in args.source_zone:
            if zones.setdefault((name, source), zone) != zone:
                raise MappingError(f"--source-zone gives {name} {source} two zones")
        named, logs = presets(args.preset, zones)
        mappings = [load_mapping(path) for path in args.mapping] + named
        registry = TemplateRegistry.from_paths(args.template)
        for name in dict.fromkeys(args.template_preset):
            shipped = template_preset(name)
            known = registry.get(shipped.id, shipped.version)
            if known is None or known.sha256 != shipped.sha256:
                registry.add(shipped)  # a different file under the same id and version is refused
        templates = registry.templates()
        if not mappings and not templates and not logs:
            raise MappingError(
                "name at least one --mapping, --preset, --template or --template-preset"
            )
        package = map_package(args.package, mappings, args.out, templates, event_logs=logs)
    except (MappingError, PackageError, OSError) as exc:
        sys.stderr.write(f"neptune-deploy map: {exc}\n")
        return 2 if isinstance(exc, MappingError) else 1
    sys.stdout.write(f"wrote {args.out}\n  package  {package}\n")
    return 0

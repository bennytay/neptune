"""``python -m neptune_deploy pack --spec <file> --snapshot <file> --out <dir>`` (ADR 0013 §10)."""

import argparse
import sys
from pathlib import Path
from typing import Any

from neptune_deploy.packs.compile import compile_pack
from neptune_deploy.packs.errors import PackError
from neptune_deploy.packs.render import render_json, render_pdf
from neptune_deploy.packs.snapshot import load_snapshot
from neptune_deploy.packs.spec import load_spec


def add_parser(commands: "argparse._SubParsersAction[Any]") -> None:
    parser = commands.add_parser(
        "pack",
        help="compile an evidence pack from a spec and a frozen Memory snapshot",
        description=(
            "Compile a pack spec over a Memory graph document (graph-schema 1) and write"
            " pack.json (canonical JSON) and pack.pdf to the output directory. The same spec and"
            " snapshot always give the same bytes; existing files with other bytes are refused."
        ),
    )
    parser.add_argument("--spec", type=Path, required=True, help="the pack spec (JSON)")
    parser.add_argument(
        "--snapshot", type=Path, required=True, help="the Memory graph document the spec names"
    )
    parser.add_argument("-o", "--out", type=Path, required=True, help="where to write the pack")


def _write(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise PackError("output_refused", f"{path} is a symlink")
    if path.exists():
        if path.read_bytes() == data:
            return
        raise PackError("output_refused", f"{path} exists with other bytes; packs never change")
    path.write_bytes(data)


def run(args: argparse.Namespace) -> int:
    try:
        spec = load_spec(args.spec.read_bytes())
        snapshot = load_snapshot(args.snapshot.read_bytes())
        pack = compile_pack(spec, snapshot)
        documents = {"pack.json": render_json(pack), "pack.pdf": render_pdf(pack)}
        args.out.mkdir(parents=True, exist_ok=True)
        for name, data in documents.items():
            _write(args.out / name, data)
    except (PackError, OSError) as exc:
        sys.stderr.write(f"neptune-deploy pack: {exc}\n")
        return 2 if isinstance(exc, PackError) else 1
    sys.stdout.write(f"wrote {args.out}\n  pack  {pack.id}\n")
    return 0

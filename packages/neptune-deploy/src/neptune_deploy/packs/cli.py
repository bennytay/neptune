"""``python -m neptune_deploy pack --spec <file> --snapshot <file> --out <dir>`` (ADR 0013 §10,
ADR 0014): writes ``pack.json``, ``pack.pdf`` and ``claims.json``."""

import argparse
import sys
from pathlib import Path
from typing import Any

from neptune_deploy.packs.compile import compile_pack
from neptune_deploy.packs.errors import PackError
from neptune_deploy.packs.render import render_claims, render_json, render_pdf
from neptune_deploy.packs.snapshot import MAX_SNAPSHOT_BYTES, load_snapshot
from neptune_deploy.packs.spec import MAX_SPEC_BYTES, load_spec


def add_parser(commands: "argparse._SubParsersAction[Any]") -> None:
    parser = commands.add_parser(
        "pack",
        help="compile an evidence pack from a spec and a frozen Memory snapshot",
        description=(
            "Compile a pack spec over a Memory graph document (graph-schema 1) and write"
            " pack.json (canonical JSON), pack.pdf and claims.json (the cited claims as a"
            " graph-schema ClaimsResult) to the output directory. The same spec and"
            " snapshot always give the same bytes; existing files with other bytes are refused."
        ),
    )
    parser.add_argument("--spec", type=Path, required=True, help="the pack spec (JSON)")
    parser.add_argument(
        "--snapshot", type=Path, required=True, help="the Memory graph document the spec names"
    )
    parser.add_argument(
        "--snapshot-schema-version",
        metavar="X.Y.Z",
        help=(
            "the graph-schema version Memory built the snapshot under (the document names only"
            " the major). A newer minor of the pinned major has its unknown keys reported as"
            " snapshot_key_unread, not refused (ADR 0015); omitted, any unknown key is refused"
        ),
    )
    parser.add_argument("-o", "--out", type=Path, required=True, help="where to write the pack")


def _read(path: Path, limit: int) -> bytes:
    """At most ``limit + 1`` bytes: an oversized input is refused by its reader, never loaded."""
    with path.open("rb") as stream:
        return stream.read(limit + 1)


def _check(path: Path, data: bytes) -> bool:
    """Whether ``path`` needs writing; refuses a symlink or a file with other bytes."""
    if path.is_symlink():
        raise PackError("output_refused", f"{path} is a symlink")
    if not path.exists():
        return True
    if path.stat().st_size != len(data) or path.read_bytes() != data:
        raise PackError("output_refused", f"{path} exists with other bytes; packs never change")
    return False


def run(args: argparse.Namespace) -> int:
    try:
        spec = load_spec(_read(args.spec, MAX_SPEC_BYTES))
        snapshot = load_snapshot(
            _read(args.snapshot, MAX_SNAPSHOT_BYTES), schema_version=args.snapshot_schema_version
        )
        pack = compile_pack(spec, snapshot)
        documents = {
            "claims.json": render_claims(pack),
            "pack.json": render_json(pack),
            "pack.pdf": render_pdf(pack),
        }
        # Every refusal before any write; each file appears whole (written aside, then renamed).
        pending = {name: data for name, data in documents.items() if _check(args.out / name, data)}
        args.out.mkdir(parents=True, exist_ok=True)
        for name, data in pending.items():
            staged = args.out / f".{name}.partial"
            staged.unlink(missing_ok=True)
            staged.write_bytes(data)
            staged.replace(args.out / name)
    except (PackError, OSError) as exc:
        sys.stderr.write(f"neptune-deploy pack: {exc}\n")
        return 2 if isinstance(exc, PackError) else 1
    sys.stdout.write(f"wrote {args.out}\n  pack  {pack.id}\n")
    return 0

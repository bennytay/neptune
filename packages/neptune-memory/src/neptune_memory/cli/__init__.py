"""``memory``: consolidate a Ledger snapshot into a tenant's graph, rebuild it, dump it (ADR 0016).

Rule: thin wrappers over the library; no logic of their own, and no direct package access.

- ``consolidate --snapshot N``: run the registered deterministic consolidators over the Ledger as
  of transaction ``N`` and fold their builds into the tenant's graph (incremental: earlier claims
  keep their first recording, and what the builds no longer emit is withdrawn at ``N``). Prints
  the ``MemorySnapshot``. Consolidating the snapshot the graph already holds, with the same
  result, changes nothing.
- ``rebuild --snapshot N``: drop the tenant's graph and consolidate ``N`` from scratch.
- ``dump [--as-of TX]``: every claim version of the tenant's graph (or the graph as of ``TX``) as
  canonical JSON Lines, ordered by claim id, to ``--out`` or stdout.

``--ledger`` is a Ledger export (``neptune_memory.ledger.LedgerExport``), ``--graphs`` the root of
the tenants' graph directories (``neptune_memory.store.graphs``). Exit status: 0 done, 1 refused
(a snapshot that does not follow the graph, a dropped consolidator, a lineage that cannot be
ordered, a bad tenant directory), 2 usage or unreadable input.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final, TextIO

from neptune.identity import canonical_json
from neptune_memory.consolidate.snapshot import (
    GraphExtendError,
    PlanError,
    consolidate,
    default_registrations,
    extend,
)
from neptune_memory.ledger import ledger_export_from_json
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.reader import AsOfBeyondHeadError
from neptune_memory.schema.supersede import LineageError, as_of
from neptune_memory.store.graphs import GraphStoreError, TenantGraphs

if TYPE_CHECKING:
    from collections.abc import Sequence

    from neptune_memory.ledger import LedgerExport

OK: Final = 0
REFUSED: Final = 1
USAGE: Final = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memory", description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--graphs", type=Path, required=True, help="root of the tenants' graphs")
    parser.add_argument("--tenant", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("consolidate", "rebuild"):
        command = commands.add_parser(name)
        command.add_argument("--ledger", type=Path, required=True, help="a Ledger export")
        command.add_argument("--snapshot", type=int, required=True, help="a Ledger transaction")
    dump = commands.add_parser("dump")
    dump.add_argument("--as-of", type=int, default=None, dest="as_of")
    dump.add_argument("--out", type=Path, default=None)
    return parser


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a JSON object repeats a key")
    return dict(pairs)


def _constant(token: str) -> object:
    raise ValueError(f"{token} is not JSON")


def _ledger(path: Path) -> LedgerExport:
    """Any strict JSON (no repeated keys, no NaN), canonical or not."""
    text = path.read_bytes().decode("utf-8")
    return ledger_export_from_json(
        json.loads(text, object_pairs_hook=_unique, parse_constant=_constant)
    )


def _consolidate(
    graphs: TenantGraphs, tenant: str, ledger: LedgerExport, snapshot: int, *, rebuild: bool
) -> bytes:
    run = consolidate(ledger.at(snapshot), default_registrations(), snapshot)
    if rebuild:  # only once the run exists: a refused rebuild keeps the old graph
        graphs.drop(tenant)
    current = graphs.load(tenant)
    if current is not None and current.head == run.snapshot.ledger_snapshot:
        recorded = graphs.snapshots(tenant)
        if recorded and recorded[-1] == {
            "findings": run.findings_json(),
            "snapshot": run.snapshot.to_json(),
        }:
            return canonical_json.dumps(run.snapshot.to_json())  # already consolidated: no-op
    graphs.save(tenant, extend(current, run), run)
    return canonical_json.dumps(run.snapshot.to_json())


def _dump(graphs: TenantGraphs, tenant: str, at: int | None, out: TextIO) -> None:
    document = graphs.load(tenant)
    if document is None:
        raise GraphStoreError(f"tenant {tenant!r} has no graph")
    resolution = document.resolution
    if at is not None:
        tx = ledger_tx(at)
        if tx > document.head:
            raise AsOfBeyondHeadError(tx, document.head)
        resolution = as_of(resolution, tx)
    for claim in sorted(resolution.claims, key=lambda c: c.id):
        out.write(canonical_json.dumps(claim.to_json()).decode("utf-8") + "\n")


def main(
    argv: Sequence[str] | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return USAGE if exc.code else OK
    graphs = TenantGraphs(args.graphs)
    try:
        if args.command == "dump":
            if args.out is None:
                _dump(graphs, args.tenant, args.as_of, out)
            else:
                with args.out.open("w", encoding="utf-8", newline="\n") as handle:
                    _dump(graphs, args.tenant, args.as_of, handle)
            return OK
        ledger = _ledger(args.ledger)
        rebuild = args.command == "rebuild"
        printed = _consolidate(graphs, args.tenant, ledger, args.snapshot, rebuild=rebuild)
        out.write(printed.decode("utf-8") + "\n")
        return OK
    except (OSError, UnicodeDecodeError) as exc:
        err.write(f"memory: cannot read input: {exc}\n")
        return USAGE
    except (GraphExtendError, GraphStoreError, LineageError, PlanError, AsOfBeyondHeadError) as exc:
        err.write(f"memory: refused: {exc}\n")
        return REFUSED
    except (TypeError, ValueError) as exc:
        err.write(f"memory: invalid input: {exc}\n")
        return USAGE

"""``memory``: consolidate a Ledger snapshot into a tenant's graph, rebuild it, dump it (ADR 0016).

Rule: thin wrappers over the library; no logic of their own, and no direct package access.

- ``consolidate --snapshot N``: run the registered deterministic consolidators over the Ledger as
  of transaction ``N`` and fold their builds into the tenant's graph (incremental: earlier claims
  keep their first recording, and what the builds no longer emit is withdrawn at ``N``). Prints
  the ``MemorySnapshot``. Consolidating the snapshot the graph already holds, with the same
  result, changes nothing.
- ``rebuild --snapshot N``: consolidate ``N`` from scratch and replace the tenant's graph with it
  (the old one stays if the rebuild is refused).
- ``--with-estimates`` (``consolidate`` and ``rebuild``): also register ``memory.time_estimates``
  (``derived.clocks``, ADR 0011 §4, ADR 0017), so the compiler's estimated clock mappings become
  ``inferred`` claims beside the deterministic ones. Off by default; a graph built with it is
  extended only with it (dropping a consolidator takes a rebuild).
- ``--config FILE`` (``consolidate`` and ``rebuild``): a JSON object of consolidator configs by
  consolidator id, e.g. ``{"memory.events": {"tables": [...], "vendors": {...}}}`` (ADR 0013 §5).
  Each is resolved as that consolidator's contract says (defaults filled in), so its hash, in every
  claim's provenance and in the ``MemorySnapshot``, is the same however it is spelled. An id that
  is not registered is a usage error; a key a consolidator does not take is its own finding. A
  changed config is new lineage: claims made under the old one are withdrawn, not rewritten.
- ``dump [--as-of TX]``: every claim version of the tenant's graph (or the graph as of ``TX``) as
  canonical JSON Lines, ordered by claim id, to ``--out`` or stdout.
- ``verify GRAPH``: check a graph document file someone else holds (a consumer's fixture) with the
  codec: every claim and finding id against its content, canonical order, ``generation``, and the
  rest of what ``graph_from_json`` checks. One line per problem on stdout and exit 1; a one-line
  summary and exit 0 when it decodes. Needs no ``--graphs`` or ``--tenant``.

``--ledger`` is a Ledger export (``neptune_memory.ledger.LedgerExport``), ``--graphs`` the root of
the tenants' graph directories (``neptune_memory.store.graphs``). Exit status: 0 done, 1 refused
(a snapshot that does not follow the graph, a dropped consolidator, a lineage that cannot be
ordered, a bad tenant directory, a graph document that does not verify), 2 usage or unreadable
input.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Final, TextIO

from neptune.identity import canonical_json
from neptune_memory.consolidate.event_records import resolve_config as event_config
from neptune_memory.consolidate.events import EVENTS_CONSOLIDATOR_ID
from neptune_memory.consolidate.snapshot import (
    GraphExtendError,
    PlanError,
    Registration,
    consolidate,
    default_registrations,
    extend,
)
from neptune_memory.derived.clocks import CLOCKS_MODEL, EstimatedClocksConsolidator
from neptune_memory.ledger import ledger_export_from_json
from neptune_memory.schema import GRAPH_SCHEMA_VERSION
from neptune_memory.schema.codec import graph_from_json, graph_problems
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.reader import AsOfBeyondHeadError
from neptune_memory.schema.supersede import LineageError, as_of
from neptune_memory.store.graphs import GraphStoreError, TenantGraphs

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.ledger import LedgerExport

OK: Final = 0
REFUSED: Final = 1
USAGE: Final = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memory", description=(__doc__ or "").splitlines()[0])
    # Required by every command but ``verify``, which reads one file and no tenant.
    parser.add_argument("--graphs", type=Path, help="root of the tenants' graphs")
    parser.add_argument("--tenant")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("consolidate", "rebuild"):
        command = commands.add_parser(name)
        command.add_argument("--ledger", type=Path, required=True, help="a Ledger export")
        command.add_argument("--snapshot", type=int, required=True, help="a Ledger transaction")
        command.add_argument(
            "--with-estimates",
            action="store_true",
            dest="with_estimates",
            help="also relay the compiler's estimated clock mappings as inferred claims",
        )
        command.add_argument(
            "--config",
            type=Path,
            default=None,
            help="a JSON object of consolidator configs by consolidator id",
        )
    dump = commands.add_parser("dump")
    dump.add_argument("--as-of", type=int, default=None, dest="as_of")
    dump.add_argument("--out", type=Path, default=None)
    verify = commands.add_parser("verify")
    verify.add_argument("graph", type=Path, help="a graph document (graph.json)")
    return parser


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a JSON object repeats a key")
    return dict(pairs)


def _constant(token: str) -> object:
    raise ValueError(f"{token} is not JSON")


def _strict_json(path: Path, what: str) -> object:
    """Any strict JSON (no repeated keys, no NaN), canonical or not."""
    text = path.read_bytes().decode("utf-8")
    try:
        return json.loads(text, object_pairs_hook=_unique, parse_constant=_constant)
    except RecursionError as exc:
        raise ValueError(f"the {what} is nested too deeply") from exc


def _ledger(path: Path) -> LedgerExport:
    return ledger_export_from_json(_strict_json(path, "Ledger export"))


class UndecodableError(ValueError):
    """A graph document the codec could not finish decoding (nested too deeply, or a reader
    failing in a way no problem line describes): a usage error, never a traceback."""


def _verify(path: Path, out: TextIO) -> int:
    """``memory verify``: one line per problem and ``REFUSED``, or a summary line and ``OK``."""
    data = _strict_json(path, "graph document")
    try:
        problems = graph_problems(data)  # type: ignore[arg-type]  # any JSON value; it checks
        document = None if problems else graph_from_json(data)  # type: ignore[arg-type]
    except Exception as exc:  # hostile input: whatever the decoder raises is unreadable input
        raise UndecodableError(
            f"the graph document cannot be decoded ({type(exc).__name__})"
        ) from exc
    for problem in problems:
        out.write(f"{path}: {problem}\n")
    if document is None:
        return REFUSED
    out.write(
        f"{path}: ok: graph-schema {GRAPH_SCHEMA_VERSION} document, head {document.head}, "
        f"{len(document.resolution.claims)} claims, {len(document.resolution.findings)} "
        f"findings, {len(document.builds)} builds, generation {document.generation}\n"
    )
    return OK


Configs = dict[str, dict[str, "JsonValue"]]


def _estimates_config(given: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The model is always in the config (ADR 0017); naming another one is refused by the runner."""
    return {"model": CLOCKS_MODEL.to_json(), **given}


# How a given config is resolved, by consolidator id. A consolidator that takes no config gets it
# as given, and reports its keys as ``unknown_config``.
RESOLVERS: Final[Mapping[str, Callable[[Mapping[str, JsonValue]], dict[str, JsonValue]]]] = {
    EVENTS_CONSOLIDATOR_ID: event_config,
    EstimatedClocksConsolidator().consolidator_id: _estimates_config,
}


def registrations(
    *, with_estimates: bool, configs: Mapping[str, Mapping[str, JsonValue]] | None = None
) -> tuple[Registration, ...]:
    """The deterministic consolidators, and ``memory.time_estimates`` when asked (ADR 0017), each
    with its resolved config: the one ``configs`` gives for its id, else its default. ``configs``
    naming an id that is not registered is a ``ValueError``."""
    estimates = Registration(EstimatedClocksConsolidator(), _estimates_config({}))
    registered = (*default_registrations(), *((estimates,) if with_estimates else ()))
    given = dict(configs or {})
    unknown = sorted(given.keys() - {r.consolidator_id for r in registered})
    if unknown:
        raise ValueError(f"--config names consolidators that are not registered: {unknown}")
    return tuple(
        r
        if r.consolidator_id not in given
        else replace(
            r,
            config=RESOLVERS.get(r.consolidator_id, dict)(given[r.consolidator_id]),
        )
        for r in registered
    )


def read_configs(path: Path) -> Configs:
    """``--config FILE``: strict JSON, an object of objects keyed by consolidator id."""
    document = _strict_json(path, "consolidator config")
    if not isinstance(document, dict) or not all(
        isinstance(value, dict) for value in document.values()
    ):
        raise ValueError("the consolidator config must be an object of objects by consolidator id")
    return document


def _consolidate(
    graphs: TenantGraphs,
    tenant: str,
    ledger: LedgerExport,
    snapshot: int,
    *,
    rebuild: bool,
    with_estimates: bool,
    configs: Configs | None = None,
) -> bytes:
    registered = registrations(with_estimates=with_estimates, configs=configs)
    run = consolidate(ledger.at(snapshot), registered, snapshot)
    printed = canonical_json.dumps(run.snapshot.to_json())
    if rebuild:  # the new graph exists before the old one is replaced: a refusal keeps it
        graphs.save(tenant, extend(None, run), run, fresh=True)
        return printed
    current = graphs.load(tenant)
    if current is not None and current.head == run.snapshot.ledger_snapshot:
        record = {"findings": run.findings_json(), "snapshot": run.snapshot.to_json()}
        if graphs.snapshot(tenant, current.head) == record:
            return printed  # this snapshot is already consolidated, identically: nothing to do
    graphs.save(tenant, extend(current, run), run)
    return printed


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
    try:
        if args.command == "verify":
            return _verify(args.graph, out)
        if args.graphs is None or args.tenant is None:
            err.write(f"memory: {args.command} needs --graphs and --tenant\n")
            return USAGE
        graphs = TenantGraphs(args.graphs)
        if args.command == "dump":
            if args.out is None:
                _dump(graphs, args.tenant, args.as_of, out)
            else:
                with args.out.open("w", encoding="utf-8", newline="\n") as handle:
                    _dump(graphs, args.tenant, args.as_of, handle)
            return OK
        ledger = _ledger(args.ledger)
        configs = None if args.config is None else read_configs(args.config)
        rebuild = args.command == "rebuild"
        printed = _consolidate(
            graphs,
            args.tenant,
            ledger,
            args.snapshot,
            rebuild=rebuild,
            with_estimates=args.with_estimates,
            configs=configs,
        )
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

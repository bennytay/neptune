"""The memory stage, real (Platform ADR 0011): the Ledger's packages consolidated by Memory.

What the ledger stage registered becomes a Ledger export (``neptune_memory.ledger``'s
``LedgerExport``, the ``memory`` command's input): each registered package's records in file
order, read back verified, at the transaction the catalog registered it at; a compiled package also
gives its ``derived/clock_mapping`` lines (the compiler's estimated clock fits, ``inferred``); and
the catalog's own ``threads_of`` answer for every record, asked of the catalog the ledger stage kept
open. ``python -m neptune_memory.cli rebuild --with-estimates`` then consolidates it at the head
transaction, with the case's Memory declaration (``Case.memory``) as ``--config``, and
``memory verify`` checks the graph document it writes.

This is how Memory's own acceptance snapshot generator builds
``packages/neptune-memory/tests/fixtures/acceptance_corpus.graph.json.gz``; nothing is
reimplemented here but the export's assembly, through Memory's public names. A case that names
Memory's committed snapshot (``Case.snapshot``) must give the same document byte for byte: the
determinism check of the whole pipeline. The graph is written to ``work/memory/graph.json``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections import Counter
from typing import TYPE_CHECKING, Final

from harness.corpus import REPO
from harness.stages import LEDGER_TENANT, Context, Json, Outcome

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from neptune_memory.ledger import ExportedPackage, LedgerExport, ThreadsOf

# The tenant Memory's graphs are kept under (a directory name; it is in no document).
MEMORY_TENANT: Final = "acceptance"
# The derived/ table a compiled package contributes beside its records (root ADR 0060).
DERIVED_KINDS: Final = ("clock_mapping",)
# The bookkeeping fields of a catalog answer: when and by which version it was answered, not what
# it says (Memory ADR 0018 §1).
BOOKKEEPING: Final = ("api_version", "as_of", "findings")
MEMORY_TIMEOUT_S: Final = 900  # the acceptance corpus consolidates in a few seconds
MAX_SNAPSHOT_BYTES: Final = 1 << 30


def graph_path(ctx: Context) -> Path:
    """Where the memory stage writes the graph document the context stage answers over."""
    return ctx.work / "memory" / "graph.json"


def _lines(path: Path) -> list[Mapping[str, object]]:
    from neptune.identity import canonical_json

    out: list[Mapping[str, object]] = []
    for line in path.read_bytes().splitlines():
        record = canonical_json.loads(line)
        if not isinstance(record, dict):
            raise TypeError(f"a line of {path.name} is not a JSON object")
        out.append(record)
    return out


def exported(root: Path, registered_at: int, derived: tuple[str, ...] = ()) -> ExportedPackage:
    """The package at ``root``, read back verified, as the Ledger holds it."""
    from neptune_memory.ledger import ExportedPackage

    from neptune.store.package import read_package

    package = read_package(root)  # every file, id and the receipt, verified
    records: list[Mapping[str, object]] = []
    for kind, _ in sorted(package.manifest.tables):
        records.extend(_lines(root / "records" / f"{kind}.jsonl"))
    for kind in derived:
        if (root / "derived" / f"{kind}.jsonl").is_file():
            records.extend(_lines(root / "derived" / f"{kind}.jsonl"))
    return ExportedPackage(str(package.id), package.manifest.version, registered_at, tuple(records))


def _threads(uri: str, record_ids: list[str]) -> list[ThreadsOf]:
    """The catalog's ``threads_of`` for each record, in its wire form, read by Memory's parser."""
    from neptune_ledger.api import CATALOG_API_VERSION, codec
    from neptune_ledger.catalog.registry import PostgresCatalog
    from neptune_memory.ledger import threads_of_from_json

    out: list[ThreadsOf] = []
    with PostgresCatalog(uri, LEDGER_TENANT, package_roots=None) as catalog:
        for record_id in record_ids:
            answer = json.loads(codec.dumps(catalog.threads_of(record_id)))
            if answer.get("api_version") != CATALOG_API_VERSION:
                raise RuntimeError(f"threads_of({record_id}) is not a catalog-api answer")
            out.append(
                threads_of_from_json({k: v for k, v in answer.items() if k not in BOOKKEEPING})
            )
    return out


def _registered(ctx: Context) -> list[tuple[str, str, int, Path]]:
    """(label, stage, transaction, root) of every package the ledger stage registered, in
    transaction order."""
    out = []
    for row in ctx.upstream.get("ledger", {}).get("cases") or []:
        if row.get("registration") != "registered" or not isinstance(row.get("tx_seq"), int):
            continue
        label, stage = str(row["case"]), str(row.get("stage", "compiler"))
        root = ctx.deploy_root(label.removesuffix(".deploy")) if stage == "deploy" else None
        out.append((label, stage, int(row["tx_seq"]), root or ctx.package_root(label)))
    return sorted(out, key=lambda item: item[2])


def ledger_export(ctx: Context) -> LedgerExport:
    """Every registered package, with the catalog's thread membership of each record."""
    from neptune_ledger.api import CATALOG_API_VERSION
    from neptune_memory.ledger import LedgerExport

    registered = _registered(ctx)
    if ctx.ledger_uri is None:
        raise RuntimeError("the ledger stage kept no catalog to ask")
    packages = [
        exported(root, tx, DERIVED_KINDS if stage == "compiler" else ())
        for _, stage, tx, root in registered
    ]
    ids = sorted(
        {str(r["id"]) for p in packages for r in p.records if isinstance(r.get("id"), str)}
    )
    return LedgerExport(
        max(tx for _, _, tx, _ in registered),
        CATALOG_API_VERSION,
        tuple(sorted(packages, key=lambda p: p.package_id)),
        tuple(_threads(ctx.ledger_uri, ids)),
    )


def _declaration(ctx: Context) -> tuple[Path | None, list[str]]:
    """The one Memory declaration the run's cases name (``Case.memory``), or none."""
    declared = sorted({case.memory for case in ctx.cases if case.memory is not None})
    if len(declared) > 1:
        return None, ["the cases name more than one Memory declaration; one graph takes one"]
    if declared and not declared[0].is_file():
        return None, [f"the Memory declaration {_shown(declared[0])} does not exist"]
    return (declared[0] if declared else None), []


def _shown(path: Path) -> str:
    return path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else path.name


def _memory(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "neptune_memory.cli", *argv],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=MEMORY_TIMEOUT_S,
        check=False,
    )


def _compare(graph: bytes, snapshot: Path) -> str:
    """``equal`` when ``snapshot`` (Memory's committed gzip) holds exactly ``graph``."""
    from neptune_memory.store.gzipped import GzipError, gunzip

    if not snapshot.is_file():
        return "missing"
    try:
        held = gunzip(snapshot.read_bytes(), MAX_SNAPSHOT_BYTES)
    except GzipError:
        return "unreadable"
    return "equal" if held == graph else "differs"


def memory_real(ctx: Context) -> Outcome:
    """Consolidate what the Ledger holds into one graph document, verify it, and compare it with
    Memory's committed snapshot where the case names one."""
    from neptune.identity import canonical_json

    if not _registered(ctx):
        return Outcome({}, ("the ledger stage registered no package to consolidate",))
    config, problems = _declaration(ctx)
    if problems:
        return Outcome({}, tuple(problems))
    export = ledger_export(ctx)
    scratch = ctx.work / "memory"
    scratch.mkdir(parents=True, exist_ok=True)
    ledger = scratch / "ledger.json"
    ledger.write_bytes(canonical_json.dumps(export.to_json()) + b"\n")  # type: ignore[arg-type]
    argv = ["--graphs", str(scratch / "graphs"), "--tenant", MEMORY_TENANT, "rebuild"]
    argv += ["--ledger", str(ledger), "--snapshot", str(export.head), "--with-estimates"]
    argv += ["--config", str(config)] if config is not None else []
    done = _memory(argv)
    if done.returncode != 0:
        last = (done.stderr.strip().splitlines() or ["no message"])[-1]
        return Outcome({}, (f"memory rebuild exited {done.returncode}: {last}",))
    built = scratch / "graphs" / MEMORY_TENANT / "graph.json"
    graph = built.read_bytes()
    graph_path(ctx).write_bytes(graph)
    checked = _memory(["verify", str(graph_path(ctx))])
    document = json.loads(graph)
    claims = document.get("claims", [])
    output: Json = {
        "assertion_kinds": dict(sorted(Counter(c["assertion_kind"] for c in claims).items())),
        "claims": len(claims),
        "config": _shown(config) if config is not None else None,
        "generation": document.get("generation"),
        "graph_schema": document.get("graph_schema"),
        "head": document.get("head"),
        "packages": [
            {"case": label, "registered_at": tx, "stage": stage}
            for label, stage, tx, _ in _registered(ctx)
        ],
        "sha256": "sha256:" + hashlib.sha256(graph).hexdigest(),
        "verify": "ok" if checked.returncode == 0 else "refused",
    }
    out: list[str] = []
    if checked.returncode != 0:
        out.append("memory verify refused the graph document")
    snapshots = sorted({case.snapshot for case in ctx.cases if case.snapshot is not None})
    for snapshot in snapshots:
        verdict = _compare(graph, snapshot)
        output["snapshot"] = {"path": _shown(snapshot), "verdict": verdict}
        if verdict != "equal":
            out.append(
                f"the graph is not Memory's committed snapshot {_shown(snapshot)} ({verdict}):"
                " the pipeline is not deterministic, or the snapshot is stale"
                " (regenerate it with Memory's acceptance_corpus_snapshot.py)"
            )
    return Outcome(output, tuple(out))

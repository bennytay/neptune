"""Memory's graph of the acceptance corpus (Platform ADR 0007, MVL-181), made by the real pipeline.

``acceptance_corpus.graph.json`` (next to this file) is what ``memory rebuild --with-estimates``
writes for the acceptance corpus. Nothing in it is hand-written:

1. ``harness.acceptance`` generates the corpus (versioned and locked by Platform).
2. The compiler's SDK ingests it, as the harness's compiler stage does, into one package, and reads
   the package back verified.
3. Deploy's lifecycle mapper (``python -m neptune_deploy map``, a subprocess: Memory never imports
   Deploy) maps that package's tables with the presets in ``DEPLOY_PRESETS`` into a second package
   of lifecycle records: change records, maintenance events, authorisation envelopes, incidents.
   ``deploy_map`` is the one call to swap for the harness's Deploy stage when it lands.
4. A real Ledger catalog (``neptune_ledger``'s ``PostgresCatalog`` on a throwaway PostgreSQL 16
   from the ``pgserver`` wheel, as the Ledger's own tests run it) registers the compiler's package
   at transaction 1 and Deploy's at 2. Its ``threads_of`` answer for every record id goes into the
   export (catalog-api 1.7.0, without the bookkeeping ``api_version``, ``as_of`` and ``findings``:
   ADR 0018 §1). Memory reads thread membership only from these; it imports no Ledger code.
5. Both packages become a Ledger export (``neptune_memory.ledger.LedgerExport``, the ``memory``
   CLI's input; ADR 0016 §4). Each holds its id, ``schema_version`` and the lines of every
   ``records/<kind>.jsonl`` its manifest lists, in file order: what the Ledger catalogs. The
   compiler package also contributes its ``derived/clock_mapping`` lines (the compiler's estimated
   clock fits, ``assertion_kind: inferred``, root ADR 0060), which the catalog does not expose
   yet (``threads_of`` answers ``unknown_record`` for them); no other ``derived/`` table is read.
6. ``memory rebuild --snapshot 2 --with-estimates`` consolidates it with the deterministic
   consolidators and ``memory.time_estimates`` (ADR 0017) and writes the graph document with the
   codec; that file is copied byte for byte. Estimates become ``inferred`` claims only.

Deterministic: the corpus, the compiler, Deploy and Memory read no clock, randomness or network. The
bytes also depend on the library versions the compiler's adapters record in their transform records
(provenance): the calibration adapter records the expat that Python is built with, so another
CPython patch release changes transform and finding record ids, and every claim citing them.
``acceptance_corpus.environment.json`` records those libraries. Where they match, a regeneration is
byte-identical. Elsewhere, where only ``expat`` or ``python`` differ, it states the same facts under
other record ids. ``tests/test_acceptance_snapshot_memory.py`` checks both. The committed files are
made under the Python CI installs (``uv python install`` from ``.python-version``).

From the repository root, with ``G=packages/neptune-memory/tests/fixtures/<this file>``::

    uv run --all-packages --all-groups python $G
    uv run --all-packages --all-groups python $G --check --export /tmp/acceptance.ledger.json

``--check`` compares instead of writing (exit 1 on a difference); ``--export`` also keeps the Ledger
export, for ``memory rebuild`` by hand.
"""

from __future__ import annotations

import argparse
import importlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

import psycopg
from neptune_ledger.api import CATALOG_API_VERSION, codec
from neptune_ledger.catalog.migrate import apply_migrations
from neptune_ledger.catalog.registry import PostgresCatalog
from pgserver.postgres_server import get_server

from neptune.identity import canonical_json
from neptune.sdk import Neptune
from neptune.sdk.result import read_package
from neptune_memory.cli import OK, main
from neptune_memory.ledger import ExportedPackage, LedgerExport, ThreadsOf, threads_of_from_json

if TYPE_CHECKING:
    from collections.abc import Mapping

    from neptune.model.jsonvalue import JsonValue

HERE: Final = Path(__file__).resolve().parent
REPO: Final = HERE.parents[3]
SNAPSHOT: Final = HERE / "acceptance_corpus.graph.json"
ENVIRONMENT: Final = HERE / "acceptance_corpus.environment.json"
TENANT: Final = "acceptance"
COMPILED_AT: Final = 1  # the compiler's package: the first registration in a fresh catalog
MAPPED_AT: Final = 2  # Deploy's lifecycle package, registered after it
HEAD: Final = MAPPED_AT
# Deploy's shipped mappings for the corpus's CMMS, ServiceNow, zone-register and ticket exports.
# Deploy's arm-cell incident template joins as a --template once Deploy ships it.
DEPLOY_PRESETS: Final = ("cmms_generic", "jira_json", "register_zone", "servicenow_csv")
# The derived/ table read beside records/: the compiler's estimated clock mappings (inferred).
DERIVED_KINDS: Final = ("clock_mapping",)


def acceptance() -> Any:
    """``harness.acceptance``, Platform's corpus module (the repository root is not on the path of
    a package's tests; it is imported, never edited)."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    return importlib.import_module("harness.acceptance")


def corpus_label() -> str:
    """``acceptance <version> (tree sha256:...)``: the corpus the snapshot was made from."""
    return str(acceptance().label())


def _lines(path: Path, kind: str) -> list[Mapping[str, object]]:
    out: list[Mapping[str, object]] = []
    for line in path.read_bytes().splitlines():
        record = canonical_json.loads(line)
        if not isinstance(record, dict):
            raise TypeError(f"a {kind} line is not a JSON object")
        out.append(record)
    return out


def exported(root: Path, registered_at: int, derived: tuple[str, ...] = ()) -> ExportedPackage:
    """The package at ``root``, read back verified, as the Ledger would hold it."""
    package = read_package(root)  # every file, id and the receipt, verified
    records: list[Mapping[str, object]] = []
    for kind, _ in sorted(package.manifest.tables):
        records.extend(_lines(root / "records" / f"{kind}.jsonl", kind))
    for kind in derived:
        records.extend(_lines(root / "derived" / f"{kind}.jsonl", kind))
    return ExportedPackage(str(package.id), package.manifest.version, registered_at, tuple(records))


def deploy_map(package: Path, out: Path) -> Path:
    """Deploy's lifecycle package for ``package``: its CLI, in a subprocess (the harness's Deploy
    stage replaces this call)."""
    argv = [sys.executable, "-m", "neptune_deploy", "map", str(package), "--out", str(out)]
    argv += [arg for name in DEPLOY_PRESETS for arg in ("--preset", name)]
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"neptune_deploy map exited {done.returncode}: {done.stderr[-2000:]}")
    return out


# The bookkeeping fields of a catalog answer: when and by which version it was answered (the
# export's head and catalog_api_version say both), not what it says (ADR 0018 §1).
BOOKKEEPING: Final = ("api_version", "as_of", "findings")


def _wire(data: bytes) -> dict[str, Any]:
    """A catalog response's wire form (its JSON), as a client receives it."""
    document = json.loads(data)
    if not isinstance(document, dict):
        raise TypeError("a catalog response is a JSON object")
    return document


def catalog_threads(work: Path, roots: tuple[Path, ...], record_ids: list[str]) -> list[ThreadsOf]:
    """Register ``roots`` in order in a fresh Ledger catalog and ask it ``threads_of`` for each
    record id: the Ledger's own answers, read back through Memory's strict parser."""
    server = get_server(work / "pgdata", cleanup_mode="stop")
    try:
        admin_uri = str(server.get_uri())
        with psycopg.connect(admin_uri, autocommit=True) as admin:
            admin.execute(  # the catalog requires byte-order collation (Ledger ADR 0005 §4)
                "CREATE DATABASE catalog TEMPLATE template0 ENCODING 'UTF8'"
                " LC_COLLATE 'C' LC_CTYPE 'C'"
            )
        uri = admin_uri.replace("/postgres?", "/catalog?", 1)
        with psycopg.connect(uri, autocommit=True) as conn:
            apply_migrations(conn, TENANT)
        with PostgresCatalog(uri, TENANT, package_roots=None) as catalog:
            for tx, root in enumerate(roots, start=1):
                registration = _wire(codec.dumps(catalog.register(root)))
                key = registration["registration_key"]
                if (
                    registration["outcome"] != "registered"
                    or key.get("value", {}).get("tx_seq") != tx
                ):
                    raise RuntimeError(f"the Ledger did not register {root} at {tx}: {key}")
            answers: list[ThreadsOf] = []
            for record_id in record_ids:
                answer = _wire(codec.dumps(catalog.threads_of(record_id)))
                if answer.get("api_version") != CATALOG_API_VERSION:
                    raise RuntimeError(f"threads_of({record_id}) is not catalog-api answer")
                kept = {k: v for k, v in answer.items() if k not in BOOKKEEPING}
                answers.append(threads_of_from_json(kept))
            return answers
    finally:
        server.cleanup()


def ledger_export(work: Path) -> LedgerExport:
    """The acceptance corpus compiled, then mapped by Deploy: a two-package Ledger export, with
    the Ledger catalog's thread membership of every record."""
    corpus = acceptance()
    root = corpus.materialise(work / "corpus" / f"{corpus.NAME}-{corpus.VERSION}")
    result = Neptune(work / "workspace").ingest(root, work / "package")
    if not result.committed or result.destination is None:
        raise RuntimeError(f"the compiler did not commit the acceptance corpus: {result.state}")
    lifecycle = deploy_map(result.destination, work / "lifecycle")
    packages = (
        exported(result.destination, COMPILED_AT, DERIVED_KINDS),
        exported(lifecycle, MAPPED_AT),
    )
    ids = sorted(
        {str(r["id"]) for p in packages for r in p.records if isinstance(r.get("id"), str)}
    )
    threads = catalog_threads(work, (result.destination, lifecycle), ids)
    return LedgerExport(
        HEAD,
        CATALOG_API_VERSION,
        tuple(sorted(packages, key=lambda p: p.package_id)),
        tuple(threads),
    )


def environment(export: LedgerExport) -> bytes:
    """The corpus and every library the compiler's transforms recorded, by adapter and version:
    what a byte-identical regeneration needs to match."""
    transforms: dict[str, JsonValue] = {}
    for package in export.packages:
        for record in package.records:
            if record.get("kind") == "transform_record":
                key = f"{record['adapter_id']} {record['adapter_version']}"
                transforms[key] = cast("JsonValue", record["libraries"])
    document: JsonValue = {"corpus": corpus_label(), "transforms": transforms}
    return canonical_json.dumps(document) + b"\n"


def build(work: Path, export: Path | None = None) -> tuple[bytes, bytes]:
    """The snapshot's bytes (``memory rebuild`` over the corpus's Ledger export, in ``work``) and
    its environment's."""
    ledger = export if export is not None else work / "ledger.json"
    exported = ledger_export(work)
    ledger.write_bytes(canonical_json.dumps(cast("JsonValue", exported.to_json())) + b"\n")
    graphs = work / "graphs"
    argv = ["--graphs", str(graphs), "--tenant", TENANT, "rebuild", "--ledger", str(ledger)]
    argv += ["--snapshot", str(HEAD), "--with-estimates"]
    status = main(argv, stdout=io.StringIO())
    if status != OK:
        raise RuntimeError(f"memory rebuild exited {status}")
    return (graphs / TENANT / "graph.json").read_bytes(), environment(exported)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare, do not write")
    parser.add_argument("--export", type=Path, help="also write the Ledger export here")
    parser.add_argument("--out", type=Path, default=SNAPSHOT)
    parser.add_argument("--environment-out", type=Path, default=ENVIRONMENT)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as scratch:
        built = build(Path(scratch), args.export)
    status = 0
    for path, data in zip((args.out, args.environment_out), built, strict=True):
        if args.check:
            same = path.is_file() and path.read_bytes() == data
            sys.stdout.write(f"{path}: {'up to date' if same else 'differs from a regeneration'}\n")
            status = status or (0 if same else 1)
        else:
            path.write_bytes(data)
            sys.stdout.write(f"{path}: {len(data)} bytes from {corpus_label()}\n")
    return status


if __name__ == "__main__":
    sys.exit(run())

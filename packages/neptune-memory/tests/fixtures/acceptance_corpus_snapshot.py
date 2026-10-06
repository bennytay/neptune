"""Memory's graph of the acceptance corpus (Platform ADR 0007, MVL-181), made by the real pipeline.

``acceptance_corpus.graph.json.gz`` (next to this file) is what ``memory rebuild --with-estimates``
writes for the acceptance corpus, gzipped deterministically (``neptune_memory.store.gzipped``:
fixed header, level 9) so the fixture stays under the repository's 512 KB limit. Nothing in it is
hand-written:

1. ``harness.acceptance`` generates the corpus (versioned and locked by Platform).
2. The harness's own ``compiler`` and ``deploy`` stages run over it (``harness.run.run_stage`` with
   ``harness.stages.STAGES``; Platform ADR 0008), exactly as ``python -m harness`` runs them. The
   compiler stage writes package ``<case>`` and checks it; the deploy stage maps it with the
   presets and templates the corpus declares (``harness/acceptance/deploy.json``) into package
   ``<case>.deploy`` of lifecycle records: change records, maintenance events, authorisation
   envelopes, incidents. Both must run real and ok, or nothing is written. Memory chooses no
   preset: a Deploy mapping joins the snapshot when the corpus declares it.
3. A real Ledger catalog (``neptune_ledger``'s ``PostgresCatalog`` on a throwaway PostgreSQL 16
   from the ``pgserver`` wheel, as the Ledger's own tests run it) registers ``<case>`` at
   transaction 1 and ``<case>.deploy`` at 2, the order the harness's ledger stage registers them.
   The harness's ledger stage registers and verifies but keeps no catalog to ask, so this one is
   Memory's own. Its ``threads_of`` answer for every record id goes into the
   export (catalog-api 1.7.0, without the bookkeeping ``api_version``, ``as_of`` and ``findings``:
   ADR 0018 §1). Memory reads thread membership only from these; it imports no Ledger code.
4. Both packages become a Ledger export (``neptune_memory.ledger.LedgerExport``, the ``memory``
   CLI's input; ADR 0016 §4). Each holds its id, ``schema_version`` and the lines of every
   ``records/<kind>.jsonl`` its manifest lists, in file order: what the Ledger catalogs. The
   compiler package also contributes its ``derived/clock_mapping`` lines (the compiler's estimated
   clock fits, ``assertion_kind: inferred``, root ADR 0060), which the catalog does not expose
   yet (``threads_of`` answers ``unknown_record`` for them); no other ``derived/`` table is read.
5. ``memory rebuild --snapshot 2 --with-estimates --config CONFIG`` consolidates it with the
   deterministic consolidators and ``memory.time_estimates`` (ADR 0017) and writes the graph
   document with the codec; that file is copied byte for byte. Estimates become ``inferred``
   claims only. ``CONFIG`` (``acceptance_corpus.memory_config.json``) is Memory's declaration
   for this corpus (ADR 0013 §5): Deploy's ``syslog events`` table as an event table, keyed by its
   RFC 5424 ``MsgID``, and the vendor kinds of what the corpus states (``PSTOP``,
   ``ESTOP``, a CMMS ``Protective stop``). A code that names no registered kind stays unmapped.

Deterministic: the corpus, the harness, the compiler, Deploy and Memory read no clock, randomness or
network, and no transform records a host-bound library version (compiler #139 dropped expat), so
the same code gives the same bytes on any host. What the bytes still depend on is pinned by the
repository: the corpus version, adapter versions, the libraries ``uv.lock`` pins and the Python
minor version ``.python-version`` pins, all of which transform records name.
``acceptance_corpus.environment.json`` records them, and the ``zlib`` that deflated the file, so a
failing regeneration says which moved.
``tests/test_acceptance_snapshot_memory.py`` regenerates and compares, byte for byte.

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
import sys
import tempfile
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from neptune.identity import canonical_json
from neptune.store.package import read_package
from neptune_memory.cli import OK, main
from neptune_memory.ledger import ExportedPackage, LedgerExport, ThreadsOf, threads_of_from_json
from neptune_memory.store.gzipped import LEVEL, deterministic_gzip

if TYPE_CHECKING:
    from collections.abc import Mapping

    from neptune.model.jsonvalue import JsonValue

HERE: Final = Path(__file__).resolve().parent
REPO: Final = HERE.parents[3]
SNAPSHOT: Final = HERE / "acceptance_corpus.graph.json.gz"
ENVIRONMENT: Final = HERE / "acceptance_corpus.environment.json"
CONFIG: Final = HERE / "acceptance_corpus.memory_config.json"
TENANT: Final = "acceptance"
COMPILED_AT: Final = 1  # the compiler's package: the first registration in a fresh catalog
MAPPED_AT: Final = 2  # Deploy's lifecycle package, registered after it
HEAD: Final = MAPPED_AT
# The harness stages that make the packages, in order: the compiler's, then Deploy's mapping of it.
HARNESS_STAGES: Final = ("compiler", "deploy")
# The derived/ table read beside records/: the compiler's estimated clock mappings (inferred).
DERIVED_KINDS: Final = ("clock_mapping",)


def harness(module: str) -> Any:
    """``harness.<module>``, Platform's (the repository root is not on the path of a package's
    tests; it is imported, never edited)."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    return importlib.import_module(f"harness.{module}")


def acceptance() -> Any:
    """``harness.acceptance``, Platform's corpus module."""
    return harness("acceptance")


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


def harness_packages(work: Path) -> tuple[Path, Path]:
    """The acceptance corpus's packages, ``<case>`` and ``<case>.deploy``, as the harness's
    compiler and deploy stages write them under ``work``. A stage that runs as a stub, or reports
    a problem, stops the snapshot: it is never made from goldens or from a partial mapping. So
    does a corpus that declares no Deploy mapping (the deploy stage passes it over, ok): the
    snapshot's head is Deploy's registration."""
    corpus, stages, run = harness("corpus"), harness("stages"), harness("run")
    _, cases = corpus.select(into=work / "corpus")
    if len(cases) != 1:
        raise RuntimeError(f"the acceptance corpus is {len(cases)} cases, not one")
    ctx = stages.Context(registry=harness("contracts").registry(), work=work, cases=cases)
    by_id = {stage.id: stage for stage in stages.STAGES}
    for name in HARNESS_STAGES:
        entry = run.run_stage(by_id[name], ctx, services_up=False, upstream_ok=True)
        if entry["mode"] != "real" or entry["status"] != "ok":
            why = "; ".join(entry["problems"]) or entry["reason"]
            raise RuntimeError(
                f"the harness's {name} stage ran {entry['mode']}, {entry['status']}: {why}"
            )
    (case,) = cases
    (mapped,) = ctx.upstream["deploy"]["cases"]
    if mapped.get("state") != "committed":
        raise RuntimeError(f"the harness's deploy stage mapped nothing for {case.id}: {mapped}")
    return ctx.package_root(case.id), ctx.deploy_root(case.id)


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
    record id: the Ledger's own answers, read back through Memory's strict parser. The Ledger
    and its test server are imported here, so reading the committed snapshot needs neither."""
    import psycopg
    from neptune_ledger.api import CATALOG_API_VERSION, codec
    from neptune_ledger.catalog.migrate import apply_migrations
    from neptune_ledger.catalog.registry import PostgresCatalog
    from pgserver.postgres_server import get_server

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
    """The harness's two packages of the acceptance corpus (compiled, then mapped by Deploy) as a
    Ledger export, with the Ledger catalog's thread membership of every record."""
    compiled, lifecycle = harness_packages(work)
    packages = (
        exported(compiled, COMPILED_AT, DERIVED_KINDS),
        exported(lifecycle, MAPPED_AT),
    )
    ids = sorted(
        {str(r["id"]) for p in packages for r in p.records if isinstance(r.get("id"), str)}
    )
    threads = catalog_threads(work, (compiled, lifecycle), ids)
    from neptune_ledger.api import CATALOG_API_VERSION

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
    document: JsonValue = {
        "corpus": corpus_label(),
        "gzip": {"level": LEVEL, "zlib": zlib.ZLIB_RUNTIME_VERSION},
        "transforms": transforms,
    }
    return canonical_json.dumps(document) + b"\n"


def build(work: Path, export: Path | None = None) -> tuple[bytes, bytes]:
    """The snapshot's gzip bytes (``memory rebuild`` over the corpus's Ledger export, in ``work``,
    deterministically gzipped) and its environment's."""
    ledger = export if export is not None else work / "ledger.json"
    exported = ledger_export(work)
    ledger.write_bytes(canonical_json.dumps(cast("JsonValue", exported.to_json())) + b"\n")
    graphs = work / "graphs"
    argv = ["--graphs", str(graphs), "--tenant", TENANT, "rebuild", "--ledger", str(ledger)]
    argv += ["--snapshot", str(HEAD), "--with-estimates", "--config", str(CONFIG)]
    status = main(argv, stdout=io.StringIO())
    if status != OK:
        raise RuntimeError(f"memory rebuild exited {status}")
    graph = (graphs / TENANT / "graph.json").read_bytes()
    return deterministic_gzip(graph), environment(exported)


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

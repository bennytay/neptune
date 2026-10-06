"""Memory's graph of the acceptance corpus (Platform ADR 0007, MVL-181), made by the real pipeline.

``acceptance_corpus.graph.json`` (next to this file) is what ``memory rebuild`` writes for the
acceptance corpus. Nothing in it is hand-written:

1. ``harness.acceptance`` generates the corpus (versioned and locked by Platform).
2. The compiler's SDK ingests it, as the harness's compiler stage does, into one package, and reads
   the package back verified.
3. The package becomes a one-package Ledger export (``neptune_memory.ledger.LedgerExport``, the
   ``memory`` CLI's input until Memory adopts the catalog API; ADR 0016 §4): its id, its
   ``schema_version`` and the lines of every ``records/<kind>.jsonl`` the manifest lists, in file
   order. That is what the Ledger catalogs on registration; ``derived/`` is not catalogued. It is
   registered at transaction 1, as in a fresh catalog.
4. ``memory rebuild --snapshot 1`` consolidates it with the default consolidators and writes the
   graph document with the codec; that file is copied byte for byte.

Deterministic: the corpus, the compiler and Memory read no clock, randomness or network. The bytes
also depend on the library versions the compiler's adapters record in their transform records
(provenance): the calibration adapter records the expat that Python is built with, so another
CPython patch release changes transform and finding record ids, and every claim citing them.
``acceptance_corpus.environment.json`` records those libraries. Where they match, a regeneration is
byte-identical. Elsewhere, where only ``expat`` or ``python`` differ, it states the same facts under
other record ids.
``tests/test_acceptance_snapshot_memory.py`` checks both. The committed files are made under the
Python CI installs (``uv python install`` from ``.python-version``).

From the repository root, with ``G=packages/neptune-memory/tests/fixtures/<this file>``::

    uv run --all-packages python $G
    uv run --all-packages python $G --check --export /tmp/acceptance.ledger.json

``--check`` compares instead of writing (exit 1 on a difference); ``--export`` also keeps the Ledger
export, for ``memory rebuild`` by hand.
"""

from __future__ import annotations

import argparse
import importlib
import io
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from neptune.identity import canonical_json
from neptune.sdk import Neptune
from neptune_memory.cli import OK, main
from neptune_memory.ledger import ExportedPackage, LedgerExport

if TYPE_CHECKING:
    from collections.abc import Mapping

    from neptune.model.jsonvalue import JsonValue

HERE: Final = Path(__file__).resolve().parent
REPO: Final = HERE.parents[3]
SNAPSHOT: Final = HERE / "acceptance_corpus.graph.json"
ENVIRONMENT: Final = HERE / "acceptance_corpus.environment.json"
TENANT: Final = "acceptance"
REGISTERED_AT: Final = 1  # the first registration in a fresh catalog
CATALOG_API: Final = "package-records"  # not a catalog-api version: the export is made here


def acceptance() -> Any:
    """``harness.acceptance``, Platform's corpus module (the repository root is not on the path of
    a package's tests; it is imported, never edited)."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    return importlib.import_module("harness.acceptance")


def corpus_label() -> str:
    """``acceptance <version> (tree sha256:...)``: the corpus the snapshot was made from."""
    return str(acceptance().label())


def ledger_export(work: Path) -> LedgerExport:
    """The acceptance corpus compiled into one package, as a one-package Ledger export."""
    corpus = acceptance()
    root = corpus.materialise(work / "corpus" / f"{corpus.NAME}-{corpus.VERSION}")
    result = Neptune(work / "workspace").ingest(root, work / "package")
    if not result.committed or result.package is None or result.destination is None:
        raise RuntimeError(f"the compiler did not commit the acceptance corpus: {result.state}")
    package = result.read_package()  # every file, id and the receipt, verified
    records: list[Mapping[str, object]] = []
    for kind, _ in sorted(package.manifest.tables):
        data = (result.destination / "records" / f"{kind}.jsonl").read_bytes()
        for line in data.splitlines():
            record = canonical_json.loads(line)
            if not isinstance(record, dict):
                raise TypeError(f"a {kind} line is not a JSON object")
            records.append(record)
    exported = ExportedPackage(
        str(result.package), package.manifest.version, REGISTERED_AT, tuple(records)
    )
    return LedgerExport(REGISTERED_AT, CATALOG_API, (exported,))


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
    status = main([*argv, "--snapshot", str(REGISTERED_AT)], stdout=io.StringIO())
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

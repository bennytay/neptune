"""A package of any size, made lazily, to measure the package writers (ADR 0065).

``scale_records(rows)`` grows the mobile robot example's site register (``tests/fixtures/model``)
to ``rows`` rows, generated one at a time and never held: each row is a ``structured_record`` with
known, unknown and (every tenth row) ambiguous cells, and every other row has a warning finding
that cites it, as a CMMS export's blank list cells do. Ids are hashes, so records arrive in no
particular id order and the writer must sort them.

``python make_scale_package.py ROWS OUT SCRATCH [--memory]`` writes the package into ``OUT`` (the
streaming writer, spilling under ``SCRATCH``; or with ``--memory`` the in-memory ``package_files``
then ``write_package``) and prints one JSON line: rows, seconds spent writing, this process's peak
resident memory in MiB, and the package id. Each run is its own process, so each peak is its own.
"""

import json
import resource
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from neptune.identity import canonical_json
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id
from neptune.model.finding import FindingCategory, Severity
from neptune.model.kinds import RECORD_KINDS
from neptune.model.knowledge import Ambiguous, Candidate, Known, Unknown
from neptune.model.provenance import EvidenceRef, Row

EXAMPLE: Final = Path(__file__).resolve().parents[1] / "model" / "mobile_robot" / "records"


def _example() -> list[Any]:
    records: list[Any] = []
    for path in sorted(EXAMPLE.glob("*.jsonl")):
        _, read = RECORD_KINDS[path.stem]
        records += [read(canonical_json.loads(line)) for line in path.read_bytes().splitlines()]
    return records


def scale_records(rows: int) -> Iterator[Any]:
    """The example's records, its register grown to ``rows`` rows, with a finding every other
    row and an ambiguous cell every tenth: generated lazily, in no id order."""
    base = _example()
    template = next(r for r in base if r.kind == "structured_record")
    (transform,) = [
        r for r in base if r.kind == "transform_record" and r.id == template.provenance.transform
    ]
    source = template.provenance.evidence.source
    yield from (r for r in base if r.kind != "structured_record")
    for n in range(1, rows + 1):
        evidence = EvidenceRef(source, (Row(n),))
        cells = [
            Known(f"S-{n:07d}"),
            Known(f"Berth {n % 97}"),
            Unknown(),
            Known(f"{-33.8 - n / 1e7:.7f}"),
            Known(f"{151.2 + n / 1e7:.7f}"),
            Ambiguous((Candidate(f"D{n % 7}"), Candidate(f"E{n % 5}")))
            if n % 10 == 0
            else Unknown(),
        ]
        record = replace(
            template,
            id=evidence_record_id("structured_record", evidence, transform),
            provenance=replace(template.provenance, evidence=evidence),
            row=n,
            cells=tuple(cells),
        )
        yield record
        if n % 2 == 0:
            yield ingest_finding(
                code="tabular.list_cell_blank",
                category=FindingCategory.MISSING,
                severity=Severity.WARNING,
                subject=evidence,
                transform=transform,
                message=f"row {n}: the dock cell is blank, so no dock is stated",
                details={"column": "dock", "row": n},
                records=(record.id,),
            )


def main(argv: list[str]) -> int:
    from neptune.store.package import package_files, write_package

    rows, out, scratch = int(argv[0]), Path(argv[1]), Path(argv[2])
    start = time.perf_counter()
    if "--memory" in argv:
        identity = write_package(out, package_files(scale_records(rows)))
    else:
        from neptune.store.writer import write_package_stream

        identity = write_package_stream(out, scale_records(rows), scratch=scratch)
    seconds = time.perf_counter() - start
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KiB on Linux
    result = {"rows": rows, "seconds": round(seconds, 2), "peak_mib": round(peak), "id": identity}
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

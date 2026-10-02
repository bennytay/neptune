"""Scale and hostile-input measurements of the lifecycle mapper for the D1 gate (MVL-116).

``python stress_lifecycle_mapper.py [ROWS ...]`` maps grown copies of the warehouse fleet's
committed base package, in memory, and prints one line per case: wall time, peak traced Python
memory, the new package's bytes and its findings. Nothing is ingested (members may not) and
nothing is written. ``docs/reviews/d1-gate.md`` records its output.
"""

import sys
import time
import tracemalloc
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from neptune.identity.provenance import evidence_record_id
from neptune.model.knowledge import Known
from neptune.model.provenance import EvidenceRef, Page, Row, Span
from neptune.store.package import IngestPackage, read_package
from neptune_deploy.lifecycle import TemplateRegistry, map_files, preset

HERE: Final = Path(__file__).resolve().parent
BASE: Final = read_package(HERE / "packages" / "warehouse_amr_fleet")
TEMPLATES: Final = tuple(
    TemplateRegistry.from_paths(
        [HERE / "declared" / "warehouse_amr_fleet" / "incident_report.json"]
    ).templates()
)


def _transform(record: Any) -> Any:
    (found,) = [
        t
        for t in BASE.records
        if t.kind == "transform_record" and t.id == record.provenance.transform
    ]
    return found


def work_orders(count: int) -> IngestPackage:
    """The CMMS export grown to ``count`` rows, cycling its own rows at new row numbers."""
    (table,) = [
        t
        for t in BASE.records
        if t.kind == "structured_table"
        and isinstance(t.header, Known)
        and "WO Number" in t.header.value
    ]
    rows = sorted(
        (r for r in BASE.records if r.kind == "structured_record" and r.table == table.id),
        key=lambda r: r.row,
    )
    transform = _transform(rows[0])
    source = rows[0].provenance.evidence.source
    grown = []
    for n in range(count):
        evidence = EvidenceRef(source, (Row(n + 1),))
        grown.append(
            replace(
                rows[n % len(rows)],
                id=evidence_record_id("structured_record", evidence, transform),
                provenance=replace(rows[0].provenance, evidence=evidence),
                row=n + 1,
            )
        )
    kept = [r for r in BASE.records if not (r.kind == "structured_record" and r.table == table.id)]
    return replace(BASE, records=(*kept, *grown))


def paragraphs(texts: Sequence[str]) -> IngestPackage:
    """The first incident report with ``texts`` as more paragraphs on a page of their own."""
    (site,) = [
        b
        for b in BASE.records
        if b.kind == "document_block" and b.text == Known("Site: S-007", b.text.provenance)
    ]
    transform = _transform(site)
    extra, offset = [], 0
    for n, text in enumerate(texts):
        span = Span(offset, offset + len(text))
        evidence = EvidenceRef(site.provenance.evidence.source, (Page(9), span))
        offset += len(text) + 1
        extra.append(
            replace(
                site,
                id=evidence_record_id("document_block", evidence, transform),
                provenance=replace(site.provenance, evidence=evidence),
                order=10**7 + n,
                text=Known(text, site.text.provenance),
            )
        )
    return replace(BASE, records=(*BASE.records, *extra))


def cell(column: str, value: str) -> IngestPackage:
    """The zone register with ``column`` of its first row set to ``value``."""
    (table,) = [
        t
        for t in BASE.records
        if t.kind == "structured_table" and isinstance(t.header, Known) and column in t.header.value
    ]
    index = table.header.value.index(column)
    out = []
    for record in BASE.records:
        if record.kind == "structured_record" and record.table == table.id and record.row == 1:
            cells = list(record.cells)
            cells[index] = Known(value)
            record = replace(record, cells=tuple(cells))
        out.append(record)
    return replace(BASE, records=tuple(out))


def measure(name: str, base: IngestPackage, presets: Sequence[str] = (), docs: bool = False) -> str:
    tracemalloc.start()
    start = time.perf_counter()
    files = map_files(base, [preset(p) for p in presets], TEMPLATES if docs else ())
    seconds = time.perf_counter() - start
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    size = sum(len(data) for data in files.values())
    findings = files["records/ingest_finding.jsonl"].count(b"\n")
    return (
        f"{name:<44} {seconds:7.2f} s  {peak / 2**20:7.1f} MiB peak"
        f"  {size / 2**20:7.2f} MiB out  {findings:5d} findings"
    )


def main(argv: Sequence[str]) -> int:
    rows = [int(arg) for arg in argv] or [10_000, 100_000]
    lines = [
        measure(f"CMMS export, {n:,} work orders", work_orders(n), ["cmms_generic"]) for n in rows
    ]
    for n in (4_000, 32_000):
        texts = [f"Site: S-{k}" for k in range(n)]
        lines.append(
            measure(f"incident report, {n:,} labelled paragraphs", paragraphs(texts), docs=True)
        )
    megabyte = 1024 * 1024
    lines.append(
        measure("register cell, 1 MiB of ';'", cell("Robots", ";" * megabyte), ["register_zone"])
    )
    repeats = "; ".join(["AMR-05"] * (megabyte // 8))
    lines.append(
        measure(
            "register cell, 1 MiB of one repeated id", cell("Robots", repeats), ["register_zone"]
        )
    )
    parts = ";".join(["a"] * (megabyte // 2))
    lines.append(
        measure(
            "register cell, 1 MiB of one-letter missions",
            cell("Missions", parts),
            ["register_zone"],
        )
    )
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

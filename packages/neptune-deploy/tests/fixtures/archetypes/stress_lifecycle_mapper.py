"""Scale and hostile-input measurements of the lifecycle mapper for the D1 gate (MVL-116).

``python stress_lifecycle_mapper.py [ROWS ...]`` maps grown copies of the warehouse fleet's
committed base package, each case in its own process, and prints one line per case: the time to map
and write the package, peak resident memory, the package's size and its findings. The grown base
is built in memory (nothing is ingested; members may not) and the package is written to a
temporary directory the way ``map_package`` writes it: streamed through the compiler's
``write_package_stream`` (ADR 0012 §3), or, where ``iter_records`` does not exist (the mapper
before ADR 0012), mapped to lists, ``package_files`` and ``write_package``. The base's own rows
are in the figure either way. ``docs/reviews/d1-gate.md`` records its output.
"""

import resource
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from neptune.identity.provenance import evidence_record_id
from neptune.model.knowledge import Known
from neptune.model.provenance import EvidenceRef, Page, Row, Span
from neptune.store.package import IngestPackage, package_files, read_package, write_package
from neptune_deploy.lifecycle import TemplateRegistry, preset
from neptune_deploy.lifecycle import run as lifecycle_run

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


def _write(
    base: IngestPackage, mappings: Sequence[Any], templates: Sequence[Any], out: Path
) -> Any:
    """The mapped package into ``out``; returns ``(bytes written, findings)``."""
    if hasattr(lifecycle_run, "iter_records"):
        from neptune.store.writer import write_package_stream

        records = lifecycle_run.iter_records(base, mappings, templates)
        write_package_stream(out, records, scratch=out.parent)
    else:
        write_package(out, package_files(lifecycle_run.map_records(base, mappings, templates)))
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    findings = (out / "records" / "ingest_finding.jsonl").read_bytes().count(b"\n")
    return size, findings


def measure(name: str, base: IngestPackage, presets: Sequence[str] = (), docs: bool = False) -> str:
    """One case: wall time to map and write the package, this process's peak resident memory,
    and what came out."""
    start = time.perf_counter()
    with tempfile.TemporaryDirectory() as scratch:
        size, findings = _write(
            base, [preset(p) for p in presets], TEMPLATES if docs else (), Path(scratch) / "out"
        )
        shutil.rmtree(scratch, ignore_errors=True)
    elapsed = time.perf_counter() - start
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KiB on Linux
    return (
        f"{name:<44} map+write {elapsed:7.2f} s  {peak:7.0f} MiB RSS"
        f"  {size / 2**20:7.2f} MiB out  {findings:5d} findings"
    )


def case(name: str, size: int) -> str:
    megabyte = 1024 * 1024
    match name:
        case "cmms":
            return measure(
                f"CMMS export, {size:,} work orders", work_orders(size), ["cmms_generic"]
            )
        case "paragraphs":
            texts = [f"Site: S-{k}" for k in range(size)]
            return measure(
                f"incident report, {size:,} labelled paragraphs", paragraphs(texts), docs=True
            )
        case "separators":
            base = cell("Robots", ";" * megabyte)
            return measure("register cell, 1 MiB of ';'", base, ["register_zone"])
        case "repeats":
            base = cell("Robots", "; ".join(["AMR-05"] * (megabyte // 8)))
            return measure("register cell, 1 MiB of one repeated id", base, ["register_zone"])
        case "missions":
            base = cell("Missions", ";".join(["a"] * (megabyte // 2)))
            return measure("register cell, 1 MiB of one-letter missions", base, ["register_zone"])
    raise ValueError(name)


def main(argv: Sequence[str]) -> int:
    """Each case in a process of its own, so each peak is its own."""
    if argv and argv[0] == "--case":
        sys.stdout.write(case(argv[1], int(argv[2])) + "\n")
        return 0
    rows = [int(arg) for arg in argv] or [10_000, 100_000]
    cases = [("cmms", n) for n in rows] + [("paragraphs", 4_000), ("paragraphs", 32_000)]
    cases += [("separators", 0), ("repeats", 0), ("missions", 0)]
    for name, size in cases:
        command = [sys.executable, __file__, "--case", name, str(size)]
        done = subprocess.run(command, check=True, capture_output=True, text=True)
        sys.stdout.write(done.stdout)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

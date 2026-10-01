"""MVL-28 acceptance, end to end: PDFs and Markdown through a real job, each call in the sandbox.

A folder of procedures, datasheets, manifests and runbooks, with their corruptions and the hostile
PDFs, is ingested by ``IngestJob`` with the default isolation (a confined child per call, ADR
0030). Every source lands as records or findings, never as a failed job or a crashed call; every
block cites exact text a learner can find again; and a second job writes the same package. The
golden package (``tests/golden/documents/``) pins the output for the SOP in both formats.
"""

import hashlib
import importlib.util
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.builtin import default_registry
from neptune.model.finding import IngestFinding
from neptune.model.knowledge import Known, NotApplicable
from neptune.model.provenance import Page, Span, TransformRecord
from neptune.model.source import LocalPath, SourceArtifact, SourceRevision
from neptune.model.world import DocumentBlock, StructuredRecord, StructuredTable
from neptune.runtime import IngestJob, JobOptions, JobOutcome, JobState
from neptune.store.package import MANIFEST, read_package
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

TESTS: Final = Path(__file__).parents[1]
FIXTURES: Final = TESTS / "fixtures"
GOLDEN: Final = TESTS / "golden" / "documents"
STRIDE: Final = 1 << 24


def _golden() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_documents", GOLDEN / "make_documents.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_documents"] = module
    spec.loader.exec_module(module)
    return module


def test_the_golden_package_is_what_a_job_writes_today() -> None:
    # On failure, run tests/golden/documents/make_documents.py and explain the diff in the PR.
    built = _golden().build()
    committed = {
        path.relative_to(GOLDEN).as_posix(): path.read_bytes()
        for path in sorted(GOLDEN.rglob("*"))
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    }
    assert built == committed


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "site"
    for folder in ("pdf", "markdown"):
        (root / folder).mkdir(parents=True)
        for path in (FIXTURES / folder).iterdir():
            if path.is_file() and path.name not in ("README.md", "make_pdfs.py"):
                shutil.copy(path, root / folder / path.name)
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    return root


def digests(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def ingest(root: Path, tmp_path: Path, name: str) -> tuple[JobOutcome, Any]:
    workspace = Workspace(tmp_path / f"{name}-home")
    job = IngestJob(root, tmp_path / name, workspace, default_registry(), JobOptions())
    outcome = job.run()
    assert outcome.state is JobState.COMMITTED
    return outcome, read_package(tmp_path / name)


def test_documents_ingest_in_the_sandbox_and_every_citation_resolves(
    corpus: Path, tmp_path: Path
) -> None:
    before = digests(corpus)
    outcome, package = ingest(corpus, tmp_path, "package")
    assert digests(corpus) == before  # sources are never written

    # No call crashed, hung or hit a sandbox limit: hostile files are the adapter's findings.
    findings = [r for r in package.records if isinstance(r, IngestFinding)]
    codes = [finding.code for finding in findings]
    assert not outcome.findings  # the job's own findings: none
    assert not [code for code in codes if code.startswith("neptune.runtime.")]
    for expected in (
        "pdf.content_limit",  # the decompression bomb
        "pdf.content_unreadable",  # the recursion bomb, the corrupt and the truncated page
        "pdf.embedded_files",
        "pdf.encrypted",
        "pdf.javascript",
        "pdf.repaired",
        "pdf.unreadable",  # the looping page tree
        "markdown.invalid_utf8",
        "markdown.nesting_limit",
    ):
        assert expected in codes, expected

    # Each source was read by the adapter its bytes call for, renamed files included.
    artifacts = [r for r in package.records if isinstance(r, SourceArtifact)]
    paths: dict[str, object] = {
        r.location.path: r.content_id
        for r in package.records
        if isinstance(r, SourceRevision) and isinstance(r.location, LocalPath)
    }
    assert {a.content_id for a in artifacts} == set(paths.values())
    readers: dict[object, set[object]] = defaultdict(set)
    for record in package.records:
        if isinstance(record, DocumentBlock):
            transform = record.provenance.transform
            readers[record.provenance.evidence.source].add(transform)
    adapters: dict[object, str] = {
        t.id: t.adapter_id for t in package.records if isinstance(t, TransformRecord)
    }
    assert {adapters[t] for t in readers[paths["pdf/renamed_datasheet"]]} == {"pdf"}
    assert {adapters[t] for t in readers[paths["markdown/runbook"]]} == {"markdown"}
    assert {adapters[t] for t in readers[paths["notes.txt"]]} == {"text"}

    # Every block cites text that is exactly where it says.
    texts = {path: (corpus / path).read_bytes() for path in paths}
    sources: dict[object, bytes] = {content: texts[path] for path, content in paths.items()}
    pdf_pages: dict[tuple[object, int], list[DocumentBlock]] = defaultdict(list)
    for block in (r for r in package.records if isinstance(r, DocumentBlock)):
        adapter = adapters[block.provenance.transform]
        locator = block.provenance.evidence.locator
        if adapter == "pdf":
            page, span = locator
            assert isinstance(page, Page) and isinstance(span, Span)
            pdf_pages[(block.provenance.evidence.source, page.index)].append(block)
        elif isinstance(block.text, Known):
            (span,) = locator
            assert isinstance(span, Span)
            data = sources[block.provenance.evidence.source]
            text = data.removeprefix(b"\xef\xbb\xbf").decode("utf-8", errors="replace")
            assert text[span.start : span.end] == block.text.value
    for found in pdf_pages.values():
        position = 0
        for block in sorted(found, key=lambda b: b.order):
            _, span = block.provenance.evidence.locator
            assert isinstance(span, Span) and span.start == position
            if isinstance(block.text, Known):
                assert len(block.text.value) == span.end - span.start
            elif isinstance(block.text, NotApplicable):
                assert span.end - span.start == 1
            position = span.end + 1

    # Tables declared by tags or by GFM syntax are structured, cell by cell.
    tables = [r for r in package.records if isinstance(r, StructuredTable)]
    rows = [r for r in package.records if isinstance(r, StructuredRecord)]
    assert {state.value for state in (t.header for t in tables) if isinstance(state, Known)} >= {
        ("Bolt", "Torque", "Unit"),
        ("Asset", "Name", "Category", "Bay"),
    }
    assert all(row.table in {t.id for t in tables} for row in rows)


def test_a_second_job_writes_the_same_package(corpus: Path, tmp_path: Path) -> None:
    _, first = ingest(corpus, tmp_path, "first")
    _, second = ingest(corpus, tmp_path, "second")
    manifests = [(tmp_path / name / MANIFEST).read_bytes() for name in ("first", "second")]
    assert manifests[0] == manifests[1]
    assert first.records == second.records

"""MVL-75 acceptance: the hostile suite cannot read outside the root or exhaust disk or memory,
every hostile fixture is a finding with provenance, and unrelated sources still ingest."""

import gzip
import io
import tracemalloc
import zipfile
import zlib
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from neptune.discovery.archive import ArchiveLimits, inspect_archive, sniff
from neptune.discovery.policy import DISCOVERY_TRANSFORM, SYMLINK_NOT_FOLLOWED, TRUNCATED
from neptune.discovery.scan import scan
from neptune.discovery.scratch import scratch_space
from neptune.discovery.source import LocalSource
from neptune.discovery.verify import verify_artifact
from neptune.identity.hashing import content_id
from neptune.identity.revisions import SourceLedger
from neptune.model.provenance import EvidenceRef
from neptune.model.source import LocalPath, SourceRevision

if TYPE_CHECKING:
    from neptune.model.finding import IngestFinding

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hostile"
LIMITS = ArchiveLimits(max_members=1000)  # so many_members.zip is hostile too
BENIGN_ARCHIVES = {"mixed.zip"}
MiB = 1 << 20


def listing(root: Path) -> list[tuple[str, int, bytes]]:
    rows = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        data = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
        rows.append((path.relative_to(root).as_posix(), info.st_mode, data))
    return rows


def same_content(name: str, left: bytes, right: bytes) -> bool:
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(left)) as a, zipfile.ZipFile(io.BytesIO(right)) as b:
            key = [(i.orig_filename, i.file_size, i.CRC, i.compress_type) for i in a.infolist()]
            return key == [
                (i.orig_filename, i.file_size, i.CRC, i.compress_type) for i in b.infolist()
            ]
    inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
    partial_left = inflate.decompress(left)
    inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
    return partial_left == inflate.decompress(right)


def test_committed_fixtures_match_the_generator_and_stay_small(hostile: ModuleType) -> None:
    built = hostile.build()
    assert set(built) == set(hostile.FIXTURES)
    committed_names = {p.name for p in FIXTURES.iterdir() if p.is_file()}
    assert committed_names == {*built, "make_hostile.py", "README.md"}
    for name, data in built.items():
        committed = (FIXTURES / name).read_bytes()
        assert len(committed) < 512 * 1024, name
        if name in hostile.COMPRESSED:
            assert same_content(name, committed, data), name
            assert (
                gzip.decompress(data) if name.endswith(".gz") and "truncated" not in name else True
            )
        else:
            assert committed == data, name
    assert hostile.build() == built  # deterministic


@pytest.fixture
def corpus(tmp_path: Path, hostile: ModuleType) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    hostile.build_tree(root, tmp_path / "outside")
    return root


def test_hostile_tree_yields_findings_and_benign_sources_still_ingest(
    corpus: Path, hostile: ModuleType
) -> None:
    before = listing(corpus)
    ledger = SourceLedger()
    result = scan(LocalSource(corpus), ledger)
    assert listing(corpus) == before  # the source tree is immutable

    ingested = {
        head.location.path
        for head in ledger.heads()
        if isinstance(head, SourceRevision) and isinstance(head.location, LocalPath)
    }
    assert "benign.txt" in ingested and "real/run.bin" in ingested
    assert {f"archives/{name}" for name in hostile.FIXTURES} <= ingested
    assert {f"names/{name}" for name in hostile.ODD_NAMES} <= ingested
    assert content_id(hostile.CANARY) not in {a.content_id for a in ledger.artifacts()}

    links = [f for f in result.findings if f.code == SYMLINK_NOT_FOLLOWED]
    assert len(links) == len(result.symlinks) == 11
    assert sum(1 for f in links if not f.details["inside_root"]) == 3
    for finding in result.findings:
        assert finding.transform == DISCOVERY_TRANSFORM.id == result.transform.id
        assert isinstance(finding.subject, LocalPath) and finding.subject.path != "."


def test_every_hostile_archive_is_a_finding_with_provenance_and_scratch_is_clean(
    corpus: Path, hostile: ModuleType, tmp_path: Path
) -> None:
    ledger = SourceLedger()
    source = LocalSource(corpus)
    scan(source, ledger)
    before = listing(corpus)
    private = tmp_path / "workspace" / "scratch"
    findings: dict[str, tuple[IngestFinding, ...]] = {}
    tracemalloc.start()
    try:
        for name in hostile.FIXTURES:
            location = LocalPath(f"archives/{name}")
            head = ledger.head(location)
            assert isinstance(head, SourceRevision)
            artifact = ledger.artifact(head.content_id)
            assert artifact is not None
            with source.open(location) as stream:
                assert sniff(stream.read(512)) is not None
                with scratch_space(private, ingest_root=corpus) as scratch:
                    report = inspect_archive(
                        stream,
                        source=artifact.content_id,
                        size=artifact.size,
                        scratch=scratch,
                        limits=LIMITS,
                    )
            findings[name] = report.findings
            for finding in report.findings:
                assert isinstance(finding.subject, EvidenceRef)
                assert finding.subject.source == artifact.content_id
                assert finding.transform == LIMITS.transform().id
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 16 * MiB  # 48 MiB bombs, 4 GiB declared members: none of it is inflated
    assert {name for name, found in findings.items() if not found} == BENIGN_ARCHIVES
    assert listing(corpus) == before
    assert private.is_dir() and list(private.iterdir()) == []


def test_a_source_truncated_after_hashing_is_a_finding_not_a_read(corpus: Path) -> None:
    ledger = SourceLedger()
    source = LocalSource(corpus)
    scan(source, ledger)
    head = ledger.head(LocalPath("real/run.bin"))
    assert isinstance(head, SourceRevision)
    artifact = ledger.artifact(head.content_id)
    assert artifact is not None and artifact.size == 1024
    (corpus / "real/run.bin").write_bytes((corpus / "real/run.bin").read_bytes()[:700])
    with source.open(LocalPath("real/run.bin")) as stream:
        [finding] = verify_artifact(stream, artifact)
    assert finding.code == TRUNCATED
    assert finding.details == {"declared_size": 1024, "actual_size": 700, "missing_bytes": 324}
    assert finding.subject == EvidenceRef(artifact.content_id, finding.subject.locator)  # type: ignore[union-attr]

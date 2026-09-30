"""MVL-2 acceptance, end to end: walk a real directory, hash each file, record it in the ledger."""

from pathlib import Path

import pytest

from neptune.discovery.source import LocalSource, SourceEntry
from neptune.identity import canonical_json
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import Observation, SourceLedger
from neptune.model.source import LocalPath

pytestmark = pytest.mark.integration


def scan(root: Path, ledger: SourceLedger) -> list[Observation]:
    source = LocalSource(root)
    observations = []
    for entry in source.walk():
        if isinstance(entry, SourceEntry):
            with source.open(entry.location) as stream:
                observations.append(ledger.observe(entry.location, digest_stream(stream)))
    return observations


def snapshot(ledger: SourceLedger) -> bytes:
    rows = [a.to_json() for a in ledger.artifacts()] + [r.to_json() for r in ledger.revisions()]
    return b"".join(canonical_json.dumps(row) + b"\n" for row in rows)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs/run1.mcap").write_bytes(b"\x89MCAP0\r\n" + bytes(range(256)) * 64)
    (tmp_path / "robot.urdf").write_bytes(b"<robot name='spot'/>")
    (tmp_path / "copy_of_robot.urdf").write_bytes(b"<robot name='spot'/>")
    return tmp_path


def test_identical_bytes_ingested_twice_resolve_idempotently(corpus: Path) -> None:
    ledger = SourceLedger()
    scan(corpus, ledger)
    before = snapshot(ledger)
    second = scan(corpus, ledger)
    assert not any(o.new_artifact or o.new_revision for o in second)
    assert snapshot(ledger) == before
    # A fresh ledger over the same bytes produces the same bytes.
    fresh = SourceLedger()
    scan(corpus, fresh)
    assert snapshot(fresh) == before


def test_changed_file_creates_revision_without_mutating_history(corpus: Path) -> None:
    ledger = SourceLedger()
    scan(corpus, ledger)
    original = ledger.head(LocalPath("runs/run1.mcap"))
    assert original is not None
    before = set(ledger.revisions())

    (corpus / "runs/run1.mcap").write_bytes(b"\x89MCAP0\r\n truncated")
    scan(corpus, ledger)

    head = ledger.head(LocalPath("runs/run1.mcap"))
    assert head is not None and head.supersedes == (original.id,)
    assert before < set(ledger.revisions())
    assert ledger.artifact(original.content_id) is not None


def test_rename_or_move_creates_no_new_evidence(corpus: Path) -> None:
    ledger = SourceLedger()
    scan(corpus, ledger)
    artifacts = ledger.artifacts()

    (corpus / "archive").mkdir()
    (corpus / "runs/run1.mcap").rename(corpus / "archive/2026-09-30.mcap")
    observations = scan(corpus, ledger)

    assert not any(o.new_artifact for o in observations)
    assert ledger.artifacts() == artifacts
    moved = ledger.head(LocalPath("archive/2026-09-30.mcap"))
    old = ledger.head(LocalPath("runs/run1.mcap"))
    assert moved is not None and old is not None
    assert moved.content_id == old.content_id


def test_duplicate_files_are_one_artifact(corpus: Path) -> None:
    ledger = SourceLedger()
    scan(corpus, ledger)
    assert len(ledger.artifacts()) == 2
    assert len(ledger.revisions()) == 3

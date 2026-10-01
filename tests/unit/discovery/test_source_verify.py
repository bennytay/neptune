"""A source re-read against its artifact: truncated, grown or changed chunks become findings."""

import io

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.discovery.policy import (
    CHUNK_CHANGED,
    DISCOVERY_TRANSFORM,
    GROWN,
    SHORT_READ,
    TRUNCATED,
)
from neptune.discovery.verify import short_read_finding, verify_artifact
from neptune.identity import canonical_json
from neptune.identity.findings import check_ingest_finding
from neptune.identity.hashing import digest_stream
from neptune.model.finding import FindingCategory, Severity, ingest_finding_from_json
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.source import SourceArtifact

DATA = bytes(range(256)) * 10  # 2560 bytes: chunks of 1024, 1024 and 512
CHUNK = 1024


def artifact(data: bytes = DATA) -> SourceArtifact:
    return digest_stream(io.BytesIO(data), chunk_size=CHUNK)


def flipped(data: bytes, *positions: int) -> bytes:
    out = bytearray(data)
    for position in positions:
        out[position] ^= 0xFF
    return bytes(out)


def test_an_intact_source_has_no_findings() -> None:
    assert verify_artifact(io.BytesIO(DATA), artifact()) == ()
    assert verify_artifact(io.BytesIO(b""), artifact(b"")) == ()


def test_truncation_cites_the_missing_range() -> None:
    [finding] = verify_artifact(io.BytesIO(DATA[:1500]), artifact())
    assert (finding.code, finding.severity, finding.category) == (
        TRUNCATED,
        Severity.ERROR,
        FindingCategory.CORRUPT,
    )
    assert finding.subject == EvidenceRef(artifact().content_id, (ByteRange(1500, 1060),))
    assert finding.details == {"declared_size": 2560, "actual_size": 1500, "missing_bytes": 1060}
    assert finding.transform == DISCOVERY_TRANSFORM.id


def test_growth_is_a_warning_citing_the_extra_bytes() -> None:
    [finding] = verify_artifact(io.BytesIO(DATA + b"more"), artifact())
    assert (finding.code, finding.severity) == (GROWN, Severity.WARNING)
    assert finding.subject == EvidenceRef(artifact().content_id, (ByteRange(2560, 4),))
    assert finding.details == {"declared_size": 2560, "actual_size": 2564, "extra_bytes": 4}
    [finding] = verify_artifact(io.BytesIO(b"x"), artifact(b""))
    assert finding.code == GROWN


def test_a_changed_chunk_is_cited_exactly() -> None:
    [finding] = verify_artifact(io.BytesIO(flipped(DATA, 1100)), artifact())
    assert (finding.code, finding.severity) == (CHUNK_CHANGED, Severity.ERROR)
    assert finding.subject == EvidenceRef(artifact().content_id, (ByteRange(1024, 1024),))
    assert finding.details == {"first_chunk": 1, "last_chunk": 1, "chunk_size": CHUNK}
    [finding] = verify_artifact(io.BytesIO(flipped(DATA, 2559)), artifact())
    assert finding.subject == EvidenceRef(artifact().content_id, (ByteRange(2048, 512),))


def test_consecutive_changed_chunks_are_one_finding() -> None:
    [finding] = verify_artifact(io.BytesIO(flipped(DATA, 10, 1100)), artifact())
    assert finding.details["first_chunk"] == 0 and finding.details["last_chunk"] == 1
    assert finding.subject == EvidenceRef(artifact().content_id, (ByteRange(0, 2048),))
    assert finding.message.startswith("chunks 0 to 1 ")
    first, second = verify_artifact(io.BytesIO(flipped(DATA, 10, 2100)), artifact())
    assert (first.details["first_chunk"], second.details["first_chunk"]) == (0, 2)


def test_changed_then_truncated_reports_both_and_not_the_cut_chunk() -> None:
    changed, truncated = verify_artifact(io.BytesIO(flipped(DATA, 10)[:1500]), artifact())
    assert (changed.code, truncated.code) == (CHUNK_CHANGED, TRUNCATED)
    assert changed.details["last_chunk"] == 0  # chunk 1 is cut, not changed


class ShortReads(io.RawIOBase):
    """Returns at most ``step`` bytes per read, like a pipe."""

    def __init__(self, data: bytes, step: int) -> None:
        self._data = data
        self._pos = 0
        self._step = step

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        limit = self._step if size < 0 else min(size, self._step)
        block = self._data[self._pos : self._pos + limit]
        self._pos += len(block)
        return block


def test_short_reads_do_not_change_the_verdict() -> None:
    data = flipped(DATA, 1100)[:2000]
    assert verify_artifact(ShortReads(data, 7), artifact()) == verify_artifact(  # type: ignore[arg-type]
        io.BytesIO(data), artifact()
    )


@given(st.integers(0, len(DATA) - 1))
def test_any_cut_is_exactly_one_truncation(cut: int) -> None:
    [finding] = verify_artifact(io.BytesIO(DATA[:cut]), artifact())
    assert finding.code == TRUNCATED
    assert finding.details["missing_bytes"] == len(DATA) - cut


def test_findings_recompute_and_are_deterministic() -> None:
    first = verify_artifact(io.BytesIO(flipped(DATA, 5)[:2000]), artifact())
    second = verify_artifact(io.BytesIO(flipped(DATA, 5)[:2000]), artifact())
    assert first == second
    for finding in first:
        line = canonical_json.dumps(finding.to_json())
        assert check_ingest_finding(ingest_finding_from_json(canonical_json.loads(line))) == finding


@pytest.mark.parametrize("chunk_size", [1, 7, 1024, 4096])
def test_any_chunk_size_works(chunk_size: int) -> None:
    declared = digest_stream(io.BytesIO(DATA), chunk_size=chunk_size)
    assert verify_artifact(io.BytesIO(DATA), declared) == ()
    [finding] = verify_artifact(io.BytesIO(flipped(DATA, 1500)), declared)
    assert isinstance(finding.subject, EvidenceRef)
    [step] = finding.subject.locator
    assert isinstance(step, ByteRange) and step.offset <= 1500 < step.offset + step.length


def test_a_short_read_is_a_finding_citing_the_unserved_range() -> None:
    declared = artifact()
    finding = short_read_finding(declared.content_id, 1500, 1060)
    assert (finding.code, finding.severity, finding.category) == (
        SHORT_READ,
        Severity.ERROR,
        FindingCategory.CORRUPT,
    )
    assert finding.subject == EvidenceRef(declared.content_id, (ByteRange(1500, 1060),))
    assert finding.details == {"offset": 1500, "unread_bytes": 1060}
    assert finding.transform == DISCOVERY_TRANSFORM.id
    assert finding == short_read_finding(declared.content_id, 1500, 1060)
    line = canonical_json.dumps(finding.to_json())
    assert check_ingest_finding(ingest_finding_from_json(canonical_json.loads(line))) == finding
    [truncated] = verify_artifact(io.BytesIO(DATA[:1500]), declared)
    assert truncated.subject == finding.subject  # the full account cites the same range

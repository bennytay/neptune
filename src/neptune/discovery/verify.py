"""Checking a source's bytes against the artifact hashed from them (ADR 0028 §3).

A ``SourceArtifact`` declares a size and a hash per chunk. Before an adapter reads a source, or
when a read comes up short, the runtime can re-read the file and learn exactly what differs:
fewer bytes than declared (truncated), more (grown, as a log still being written), or a chunk
whose bytes changed. Each outcome is a finding citing the affected range of the declared
artifact, never an exception: a source that changed under Neptune is evidence of that.

A short read is the same fact seen from inside an adapter: ``neptune.adapters.contract.read_pieces``
raises ``ShortReadError`` when a reader serves no bytes inside the size it declares, and
``short_read_finding`` records that as it happened. ``verify_artifact`` then says exactly what
differs.

Memory is bounded: the stream is read in 1 MiB blocks and one chunk digest is held at a time.
"""

import hashlib
from typing import BinaryIO, Final

from neptune.discovery.policy import (
    CHUNK_CHANGED,
    DISCOVERY_TRANSFORM,
    GROWN,
    SHORT_READ,
    TRUNCATED,
)
from neptune.identity.findings import ingest_finding
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ContentId
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.source import SourceArtifact

_READ_SIZE: Final = 1 << 20


def verify_artifact(stream: BinaryIO, artifact: SourceArtifact) -> tuple[IngestFinding, ...]:
    """Findings describing how ``stream`` differs from ``artifact``; empty when it is intact.

    Reads from the stream's current position to EOF. Findings are in byte order: changed chunk
    runs first, then the truncation or growth at the declared end. Consecutive changed chunks are
    one finding (one finding per affected range, never per sample).
    """
    chunk_size = artifact.chunk_size
    declared = artifact.size
    runs: list[list[int]] = []
    read_total = 0
    for index, expected in enumerate(artifact.chunks):
        want = min(chunk_size, declared - index * chunk_size)
        digest = hashlib.sha256()
        got = 0
        while got < want:
            block = stream.read(min(_READ_SIZE, want - got))
            if not block:
                break
            digest.update(block)
            got += len(block)
        read_total += got
        if got < want:
            break  # the cut chunk is part of the truncation, not a change
        if ContentId("sha256:" + digest.hexdigest()) != expected:
            if runs and runs[-1][1] == index - 1:
                runs[-1][1] = index
            else:
                runs.append([index, index])

    findings = [_changed(artifact, first, last) for first, last in runs]
    if read_total < declared:
        findings.append(_truncated(artifact, read_total))
    else:
        extra = 0
        while block := stream.read(_READ_SIZE):
            extra += len(block)
        if extra:
            findings.append(_grown(artifact, extra))
    return tuple(findings)


def _changed(artifact: SourceArtifact, first: int, last: int) -> IngestFinding:
    start = first * artifact.chunk_size
    end = min(artifact.size, (last + 1) * artifact.chunk_size)
    count = last - first + 1
    which = f"chunk {first}" if count == 1 else f"chunks {first} to {last}"
    return ingest_finding(
        code=CHUNK_CHANGED,
        category=FindingCategory.INCONSISTENT,
        severity=Severity.ERROR,
        subject=EvidenceRef(artifact.content_id, (ByteRange(start, end - start),)),
        transform=DISCOVERY_TRANSFORM,
        message=f"{which} no longer hash as when the source was recorded; not read",
        details={"first_chunk": first, "last_chunk": last, "chunk_size": artifact.chunk_size},
    )


def _truncated(artifact: SourceArtifact, actual: int) -> IngestFinding:
    missing = artifact.size - actual
    return ingest_finding(
        code=TRUNCATED,
        category=FindingCategory.CORRUPT,
        severity=Severity.ERROR,
        subject=EvidenceRef(artifact.content_id, (ByteRange(actual, missing),)),
        transform=DISCOVERY_TRANSFORM,
        message=f"{missing} of {artifact.size} declared bytes are missing; the source is truncated",
        details={"declared_size": artifact.size, "actual_size": actual, "missing_bytes": missing},
    )


def _grown(artifact: SourceArtifact, extra: int) -> IngestFinding:
    return ingest_finding(
        code=GROWN,
        category=FindingCategory.INCONSISTENT,
        severity=Severity.WARNING,
        subject=EvidenceRef(artifact.content_id, (ByteRange(artifact.size, extra),)),
        transform=DISCOVERY_TRANSFORM,
        message=f"{extra} bytes follow the {artifact.size} declared; the source has grown",
        details={
            "declared_size": artifact.size,
            "actual_size": artifact.size + extra,
            "extra_bytes": extra,
        },
    )


def short_read_finding(source: ContentId, offset: int, length: int) -> IngestFinding:
    """The finding for a ``ShortReadError``: no bytes at ``offset`` though ``length`` were declared.

    The subject is the declared range that was not served. The runtime emits this where the read
    failed (a plan, a chunk) so the rest of the job goes on, and may call ``verify_artifact`` on
    the source for the full account.
    """
    return ingest_finding(
        code=SHORT_READ,
        category=FindingCategory.CORRUPT,
        severity=Severity.ERROR,
        subject=EvidenceRef(source, (ByteRange(offset, length),)),
        transform=DISCOVERY_TRANSFORM,
        message=(
            f"the reader served no bytes at offset {offset}; {length} declared bytes were not"
            " read, so the source is shorter than its artifact or changed under the reader"
        ),
        details={"offset": offset, "unread_bytes": length},
    )

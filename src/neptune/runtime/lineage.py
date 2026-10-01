"""The runtime as a producer: its transform record and the findings it makes (ADR 0028 §4).

What an adapter could not say about a source, the runtime says: an adapter crashed on a chunk, a
plan could not be made, a source's chunks broke a cross-chunk law, a file changed under the job or
could not be opened, a walk entry was not read. Each is an ``IngestFinding`` from the runtime's
own ``TransformRecord`` (``neptune.runtime`` at ``RUNTIME_VERSION``, with the job's retry policy
as its config), so a package records who said it and under what policy. The transform enters a
package only with its findings, so a package with none is independent of the runtime's version.

Messages and details hold ids, codes and exception class names: never an exception's text, which
may name a path or an address.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from neptune.adapters.contract import Documented
from neptune.discovery.source import SkipReason
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ContentId
from neptune.model.provenance import ByteRange, EvidenceRef, TransformRecord
from neptune.model.source import LocalPath, RawLocalPath

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue

RUNTIME_ID: Final = "neptune.runtime"
RUNTIME_VERSION: Final = "0.1.0"

CHUNK_FAILED: Final = f"{RUNTIME_ID}.chunk_failed"
ENTRY_SKIPPED: Final = f"{RUNTIME_ID}.entry_skipped"
OUTPUT_INVALID: Final = f"{RUNTIME_ID}.output_invalid"
PLAN_FAILED: Final = f"{RUNTIME_ID}.plan_failed"
SOURCE_CHANGED: Final = f"{RUNTIME_ID}.source_changed"
SOURCE_UNREADABLE: Final = f"{RUNTIME_ID}.source_unreadable"

FINDING_CODES: Final[tuple[Documented, ...]] = (
    Documented(
        CHUNK_FAILED,
        "an adapter raised, or broke the contract, on a chunk after every attempt; the source is"
        " not in this package (failed, error)",
    ),
    Documented(
        ENTRY_SKIPPED,
        "a walk entry was not read: not a regular file (info), vanished (warning), or unreadable"
        " (error) (skipped)",
    ),
    Documented(
        OUTPUT_INVALID,
        "a source's committed chunk outputs break a cross-chunk law of the adapter contract; the"
        " source is not in this package (failed, error)",
    ),
    Documented(
        PLAN_FAILED,
        "an adapter raised, or broke the contract, while planning a source; the source is not in"
        " this package (failed, error)",
    ),
    Documented(
        SOURCE_CHANGED,
        "a file's bytes changed after it was fingerprinted; it was not read, and the next job"
        " fingerprints it again (inconsistent, error)",
    ),
    Documented(
        SOURCE_UNREADABLE,
        "a fingerprinted file could not be opened when it was time to read it (skipped, error)",
    ),
)

_SKIP_SEVERITY: Final = {
    SkipReason.NOT_REGULAR_FILE: Severity.INFO,
    SkipReason.MISSING: Severity.WARNING,
    SkipReason.SYMLINK: Severity.WARNING,
    SkipReason.UNREADABLE: Severity.ERROR,
}


def runtime_transform(attempts: int) -> TransformRecord:
    """The runtime's transform under one retry policy: ``attempts`` tries per chunk."""
    return transform_record(
        adapter_id=RUNTIME_ID, adapter_version=RUNTIME_VERSION, config={"attempts": attempts}
    )


def _whole(source: ContentId, size: int) -> EvidenceRef:
    return EvidenceRef(source, (ByteRange(0, size),))


def chunk_failed(
    transform: TransformRecord,
    source: ContentId,
    size: int,
    adapter_id: str,
    chunk: str,
    error: str,
    attempts: int,
    problem: str | None = None,
) -> IngestFinding:
    """``adapter_id`` failed on ``chunk`` with an exception of class ``error``, every attempt.

    ``problem`` is the contract law broken, when the failure was a ``ContractError``.
    """
    details: dict[str, JsonValue] = {
        "adapter": adapter_id,
        "attempts": attempts,
        "chunk": chunk,
        "error": error,
    }
    if problem is not None:
        details["problem"] = problem
    tries = "attempt" if attempts == 1 else "attempts"
    return ingest_finding(
        code=CHUNK_FAILED,
        category=FindingCategory.FAILED,
        severity=Severity.ERROR,
        subject=_whole(source, size),
        transform=transform,
        message=f"{adapter_id} failed on chunk {chunk} after {attempts} {tries} ({error});"
        " the source is not in this package",
        details=details,
    )


def plan_failed(
    transform: TransformRecord,
    source: ContentId,
    size: int,
    adapter_id: str,
    error: str,
    problem: str | None = None,
) -> IngestFinding:
    """``adapter_id`` raised ``error`` while planning the source, or its plan broke the contract."""
    details: dict[str, JsonValue] = {"adapter": adapter_id, "error": error}
    if problem is not None:
        details["problem"] = problem
    return ingest_finding(
        code=PLAN_FAILED,
        category=FindingCategory.FAILED,
        severity=Severity.ERROR,
        subject=_whole(source, size),
        transform=transform,
        message=f"{adapter_id} could not plan the source ({error}); it is not in this package",
        details=details,
    )


def output_invalid(
    transform: TransformRecord,
    source: ContentId,
    size: int,
    adapter_id: str,
    problems: Sequence[str],
) -> IngestFinding:
    """The source's committed chunk outputs, taken together, break the listed laws."""
    if not problems:
        raise ValueError("an output_invalid finding names at least one problem")
    count = len(problems)
    first = problems[0] if len(problems[0]) <= 600 else problems[0][:597] + "..."
    laws = "law" if count == 1 else "laws"
    return ingest_finding(
        code=OUTPUT_INVALID,
        category=FindingCategory.FAILED,
        severity=Severity.ERROR,
        subject=_whole(source, size),
        transform=transform,
        message=f"{adapter_id}'s output breaks {count} cross-chunk {laws}: {first}",
        details={"adapter": adapter_id, "problems": list(problems)},
    )


def source_changed(
    transform: TransformRecord, location: LocalPath | RawLocalPath, source: ContentId
) -> IngestFinding:
    """The file at ``location`` no longer holds the bytes it was fingerprinted as."""
    return ingest_finding(
        code=SOURCE_CHANGED,
        category=FindingCategory.INCONSISTENT,
        severity=Severity.ERROR,
        subject=location,
        transform=transform,
        message="the file changed after it was fingerprinted; it was not read, and the next job"
        " fingerprints it again",
        details={"source": source},
    )


def source_unreadable(
    transform: TransformRecord, location: LocalPath | RawLocalPath, reason: SkipReason, detail: str
) -> IngestFinding:
    """A fingerprinted file could not be opened when the job came to read it."""
    return ingest_finding(
        code=SOURCE_UNREADABLE,
        category=FindingCategory.SKIPPED,
        severity=Severity.ERROR,
        subject=location,
        transform=transform,
        message=f"the file could not be opened for reading ({reason}: {detail})",
        details={"reason": str(reason)},
    )


def entry_skipped(
    transform: TransformRecord, location: LocalPath | RawLocalPath, reason: SkipReason, detail: str
) -> IngestFinding:
    """A walk entry discovery did not read, and why."""
    return ingest_finding(
        code=ENTRY_SKIPPED,
        category=FindingCategory.SKIPPED,
        severity=_SKIP_SEVERITY[reason],
        subject=location,
        transform=transform,
        message=f"not read: {reason} ({detail})",
        details={"reason": str(reason)},
    )

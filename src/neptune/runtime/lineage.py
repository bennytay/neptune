"""The runtime as a producer: its transform record and the findings it makes (ADR 0028 §4).

What an adapter could not say about a source, the runtime says: an adapter raised, crashed or hit
a sandbox limit on a chunk or a plan, a source's chunks broke a cross-chunk law, a file changed
under the job or could not be opened, a walk entry was not read. Each is an ``IngestFinding`` from
the runtime's own ``TransformRecord`` (``neptune.runtime`` at ``RUNTIME_VERSION``, with the job's
retry and isolation policy as its config: attempts, isolation and, when sandboxed, the limits), so
a package records who said it and under what policy (ADR 0028 §4, ADR 0030). The transform enters
a package only with its findings, so a package with none is independent of the runtime's version.

Messages and details hold ids, codes, versions, limits, signal names and exception class names:
never an exception's text, which may name a path or an address.
"""

from collections.abc import Mapping, Sequence
from typing import Final

from neptune.adapters.contract import Documented
from neptune.discovery.source import SkipReason
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ContentId
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import ByteRange, EvidenceRef, TransformRecord
from neptune.model.source import LocalPath, RawLocalPath
from neptune.runtime.sandbox import DEFAULT_LIMITS, Isolation, Limits

RUNTIME_ID: Final = "neptune.runtime"
RUNTIME_VERSION: Final = "0.2.0"

ADAPTER_CRASHED: Final = f"{RUNTIME_ID}.adapter_crashed"
CHUNK_FAILED: Final = f"{RUNTIME_ID}.chunk_failed"
ENTRY_SKIPPED: Final = f"{RUNTIME_ID}.entry_skipped"
LIMIT_EXCEEDED: Final = f"{RUNTIME_ID}.limit_exceeded"
OUTPUT_INVALID: Final = f"{RUNTIME_ID}.output_invalid"
PLAN_FAILED: Final = f"{RUNTIME_ID}.plan_failed"
SOURCE_CHANGED: Final = f"{RUNTIME_ID}.source_changed"
SOURCE_UNREADABLE: Final = f"{RUNTIME_ID}.source_unreadable"

FINDING_CODES: Final[tuple[Documented, ...]] = (
    Documented(
        ADAPTER_CRASHED,
        "the sandboxed process running an adapter's plan or a chunk's ingest died without a"
        " reply (a signal, an exit, or a reply that does not decode), after every attempt; the"
        " source is not in this package (failed, error)",
    ),
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
        LIMIT_EXCEEDED,
        "an adapter's plan or a chunk's ingest was stopped at a sandbox limit (cpu_seconds,"
        " wall_seconds, memory_bytes, reply_bytes); never retried in the job; the source is not"
        " in this package (failed, error)",
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


def runtime_transform(
    attempts: int, isolation: Isolation = Isolation.SUBPROCESS, limits: Limits = DEFAULT_LIMITS
) -> TransformRecord:
    """The runtime's transform under one policy: ``attempts`` tries per chunk, adapter calls run
    with ``isolation``, and bounded by ``limits`` when that is the sandbox."""
    config: dict[str, JsonValue] = {"attempts": attempts, "isolation": str(isolation)}
    if isolation is Isolation.SUBPROCESS:
        config |= limits.to_json()
    return transform_record(adapter_id=RUNTIME_ID, adapter_version=RUNTIME_VERSION, config=config)


def _whole(source: ContentId, size: int) -> EvidenceRef:
    return EvidenceRef(source, (ByteRange(0, size),))


def _tries(attempts: int) -> str:
    return f"{attempts} attempt" if attempts == 1 else f"{attempts} attempts"


def _call(chunk: str | None) -> tuple[str, dict[str, JsonValue]]:
    """Which call failed, in words and as details: a chunk's ``ingest``, or ``plan`` (no chunk)."""
    if chunk is None:
        return "while planning the source", {"call": "plan"}
    return f"on chunk {chunk}", {"call": "ingest", "chunk": chunk}


def chunk_failed(
    transform: TransformRecord,
    source: ContentId,
    size: int,
    adapter_id: str,
    version: str,
    chunk: str,
    error: str,
    attempts: int,
    problem: str | None = None,
) -> IngestFinding:
    """``adapter_id`` at ``version`` raised an ``error`` on ``chunk``, every attempt.

    ``problem`` is the contract law broken, when the failure was a ``ContractError``.
    """
    details: dict[str, JsonValue] = {
        "adapter": adapter_id,
        "attempts": attempts,
        "chunk": chunk,
        "error": error,
        "version": version,
    }
    if problem is not None:
        details["problem"] = problem
    return ingest_finding(
        code=CHUNK_FAILED,
        category=FindingCategory.FAILED,
        severity=Severity.ERROR,
        subject=_whole(source, size),
        transform=transform,
        message=f"{adapter_id} {version} failed on chunk {chunk} after {_tries(attempts)}"
        f" ({error}); the source is not in this package",
        details=details,
    )


def plan_failed(
    transform: TransformRecord,
    source: ContentId,
    size: int,
    adapter_id: str,
    version: str,
    error: str,
    problem: str | None = None,
) -> IngestFinding:
    """``adapter_id`` raised ``error`` while planning the source, or its plan broke the contract."""
    details: dict[str, JsonValue] = {"adapter": adapter_id, "error": error, "version": version}
    if problem is not None:
        details["problem"] = problem
    return ingest_finding(
        code=PLAN_FAILED,
        category=FindingCategory.FAILED,
        severity=Severity.ERROR,
        subject=_whole(source, size),
        transform=transform,
        message=f"{adapter_id} {version} could not plan the source ({error});"
        " it is not in this package",
        details=details,
    )


_CRASH_CAUSES: Final = frozenset({"exit_status", "reply", "signal"})


def _death(cause: Mapping[str, JsonValue]) -> str:
    if "signal" in cause:
        return f"killed by {cause['signal']}"
    if "exit_status" in cause:
        return f"exit status {cause['exit_status']}"
    return "a reply that does not decode"


def adapter_crashed(
    transform: TransformRecord,
    source: ContentId,
    size: int,
    adapter_id: str,
    version: str,
    chunk: str | None,
    cause: Mapping[str, JsonValue],
    attempts: int,
) -> IngestFinding:
    """The sandboxed process running ``adapter_id`` died without a reply, every attempt.

    ``chunk`` is the chunk it was ingesting, ``None`` for ``plan``. ``cause`` is exactly one of
    ``{"signal": "SIGSEGV"}``, ``{"exit_status": 3}`` or ``{"reply": "malformed"}``.
    """
    if len(cause) != 1 or not cause.keys() <= _CRASH_CAUSES:
        raise ValueError(f"a crash has one cause: a signal, an exit status or a reply: {cause}")
    where, call = _call(chunk)
    details: dict[str, JsonValue] = {"adapter": adapter_id, "attempts": attempts}
    details |= {"version": version, **call, **cause}
    return ingest_finding(
        code=ADAPTER_CRASHED,
        category=FindingCategory.FAILED,
        severity=Severity.ERROR,
        subject=_whole(source, size),
        transform=transform,
        message=f"{adapter_id} {version} crashed {where} after {_tries(attempts)}"
        f" ({_death(cause)}); the source is not in this package",
        details=details,
    )


def limit_exceeded(
    transform: TransformRecord,
    source: ContentId,
    size: int,
    adapter_id: str,
    version: str,
    chunk: str | None,
    limit: str,
    value: int,
) -> IngestFinding:
    """``adapter_id`` was stopped at the sandbox's ``limit`` of ``value`` (no chunk: ``plan``).

    Never retried in the job: the same bytes under the same limit hit it again. The next job, or
    a higher limit (another runtime config, so another lineage), tries again.
    """
    where, call = _call(chunk)
    details: dict[str, JsonValue] = {
        "adapter": adapter_id,
        "limit": limit,
        "value": value,
        "version": version,
    }
    return ingest_finding(
        code=LIMIT_EXCEEDED,
        category=FindingCategory.FAILED,
        severity=Severity.ERROR,
        subject=_whole(source, size),
        transform=transform,
        message=f"{adapter_id} {version} was stopped {where} at its {limit} limit ({value});"
        " the source is not in this package",
        details=details | call,
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
